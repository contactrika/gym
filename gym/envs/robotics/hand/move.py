import os
import pickle
from typing import Sequence

import numpy as np

from gym import utils, error
from gym.envs.robotics import rotations, hand_env
from gym.envs.robotics.utils import robot_get_obs, reset_mocap_welds, reset_mocap2body_xpos

try:
    import mujoco_py
except ImportError as e:
    raise error.DependencyNotInstalled("{}. (HINT: you need to install mujoco_py, "
                                       "and also perform the setup instructions here: "
                                       "https://github.com/openai/mujoco-py/.)".format(e))

HAND_PICK_AND_PLACE_XML = os.path.join('hand', 'pick_and_place.xml')
HAND_MOVE_AND_REACH_XML = os.path.join('hand', 'move_and_reach.xml')

FINGERTIP_BODY_NAMES = [
    'robot0:ffdistal',
    'robot0:mfdistal',
    'robot0:rfdistal',
    'robot0:lfdistal',
    'robot0:thdistal',
]

OBJECTS = dict(
    original=dict(type='ellipsoid', size='0.03 0.03 0.04'),
    small_box=dict(type='box', size='0.022 0.022 0.022'),
    box=dict(type='box', size='0.03 0.03 0.03'),
    sphere=dict(type='ellipsoid', size='0.028 0.028 0.028'),
    small_sphere=dict(type='ellipsoid', size='0.024 0.024 0.024'),
    teapot=dict(type='mesh', mesh='object_mesh:teapot_vhacd_m', mesh_parts=6, mass=[0.01, 0.01, 0.01, 0.5, 0.01, 0.01]),
)


def _goal_distance(goal_a, goal_b, ignore_target_rotation):
    assert goal_a.shape == goal_b.shape
    assert goal_a.shape[-1] == 7

    delta_pos = goal_a[..., :3] - goal_b[..., :3]
    d_pos = np.linalg.norm(delta_pos, axis=-1)
    d_rot = np.zeros_like(goal_b[..., 0])

    if not ignore_target_rotation:
        quat_a, quat_b = goal_a[..., 3:], goal_b[..., 3:]
        # Subtract quaternions and extract angle between them.
        quat_diff = rotations.quat_mul(quat_a, rotations.quat_conjugate(quat_b))
        angle_diff = 2 * np.arccos(np.clip(quat_diff[..., 0], -1., 1.))
        d_rot = angle_diff
    assert d_pos.shape == d_rot.shape
    return d_pos, d_rot


def _check_range(a, a_min, a_max, include_bounds=True):
    if include_bounds:
        return np.all((a_min <= a) & (a <= a_max))
    else:
        return np.all((a_min < a) & (a < a_max))


class MovingHandEnv(hand_env.HandEnv, utils.EzPickle):
    def __init__(self, model_path, reward_type, initial_qpos=None, relative_control=False, has_object=False,
                 randomize_initial_arm_pos=False, randomize_initial_object_pos=True, ignore_rotation_ctrl=False,
                 distance_threshold=0.05, rotation_threshold=0.1, n_substeps=20, ignore_target_rotation=False,
                 success_on_grasp_only=False, grasp_state=None, grasp_state_reset_p=0.0, target_in_the_air_p=0.5,
                 object_id='original', object_cage=False, cage_opacity=0.1, weld_fingers=False):

        self.target_in_the_air_p = target_in_the_air_p
        self.has_object = has_object
        self.ignore_target_rotation = ignore_target_rotation
        self.randomize_initial_arm_pos = randomize_initial_arm_pos
        self.randomize_initial_object_pos = randomize_initial_object_pos
        self.ignore_rotation_ctrl = ignore_rotation_ctrl
        self.distance_threshold = distance_threshold
        self.rotation_threshold = rotation_threshold
        self.reward_type = reward_type
        self.success_on_grasp_only = success_on_grasp_only
        self.object_id = object_id
        self.forearm_bounds = (np.r_[0.65, 0.3, 0.42], np.r_[1.75, 1.2, 1.0])
        self.table_safe_bounds = (np.r_[1.10, 0.43], np.r_[1.49, 1.05])
        self._initial_arm_mocap_pose = np.r_[1.05, 0.75, 0.65, rotations.euler2quat(np.r_[0., 1.59, 1.57])]

        if isinstance(grasp_state, bool) and grasp_state:
            suffix = "" if object_id == 'original' else f'_{object_id}'
            p = os.path.join(os.path.dirname(__file__), f'../assets/states/grasp_state{suffix}.pkl')
            if not os.path.exists(p):
                raise IOError('File {} does not exist'.format(p))
            grasp_state = pickle.load(open(p, 'rb'))

        if grasp_state is not None and grasp_state_reset_p <= 0.0:
            raise ValueError('grasp_state_reset_p must be greater than zero if grasp_state is specified!')

        self.grasp_state = grasp_state
        self.grasp_state_reset_p = grasp_state_reset_p

        if ignore_rotation_ctrl and not ignore_target_rotation:
            raise ValueError('Target rotation must be ignored if arm cannot rotate! Set ignore_target_rotation=True')

        if success_on_grasp_only:
            if reward_type != 'sparse':
                raise ValueError('Parameter success_on_grasp_only requires sparse rewards!')
            if not has_object:
                raise ValueError('Parameter success_on_grasp_only requires object to be grasped!')

        default_qpos = dict()
        xml_format = None
        if self.has_object:
            default_qpos['object:joint'] = [1.25, 0.53, 0.4, 1., 0., 0., 0.]
            xml_format = dict(
                object_geom='<geom name="{name}" {props} material="material:object" condim="4"'
                            'friction="1 0.95 0.01" solimp="0.99 0.99 0.01" solref="0.01 1"/>',
                target_geom='<geom name="{name}" {props} material="material:target" condim="4" group="2"'
                            'contype="0" conaffinity="0"/>',
            )
            obj = dict(OBJECTS[object_id])
            if 'mass' not in obj.keys():
                obj['mass'] = 0.2
            mesh_parts = obj.get('mesh_parts')
            if mesh_parts is not None and isinstance(mesh_parts, int):
                del obj['mesh_parts']
                object_geom, target_geom = '', ''
                if isinstance(obj['mass'], Sequence):
                    masses = list(obj['mass'])
                    assert len(masses) == mesh_parts
                    del obj['mass']
                else:
                    masses = [obj['mass']] * mesh_parts
                for i in range(mesh_parts):
                    obj_part = dict(obj)
                    obj_part['mesh'] += f'_part{i}'
                    obj_part['mass'] = masses[i]
                    props = " ".join([f'{k}="{v}"' for k, v in obj_part.items()])
                    object_geom += xml_format['object_geom'].format(name=f'object_part{i}', props=props)
                    target_geom += xml_format['target_geom'].format(name=f'target_part{i}', props=props)
                xml_format = dict(object_geom=object_geom, target_geom=target_geom)
            else:
                props = " ".join([f'{k}="{v}"' for k, v in obj.items()])
                xml_format = dict(
                    object_geom=xml_format['object_geom'].format(name='object', props=props),
                    target_geom=xml_format['target_geom'].format(name='target', props=props),
                )
            if object_cage:
                rgba = f"1 0 0 {cage_opacity}"
                xml_format['cage'] = '''
                <geom pos="0 0.55 0.4" quat="0.924 0.3826 0 0" size="1 0.75 0.01" type="box" mass="200" rgba="{}" solimp="0.99 0.99 0.01" solref="0.01 1"/>
                <geom pos="0 -0.55 0.4" quat="0.924 -0.3826 0 0" size="1 0.75 0.01" type="box" mass="200" rgba="{}" solimp="0.99 0.99 0.01" solref="0.01 1"/>
                <geom pos="-0.45 0 0.4" quat="0.924 0 0.3826 0" size="0.75 1 0.01" type="box" mass="200" rgba="{}" solimp="0.99 0.99 0.01" solref="0.01 1"/>
                <geom pos="0.45 0 0.4" quat="0.924 0 -0.3826 0" size="0.75 1 0.01" type="box" mass="200" rgba="{}" solimp="0.99 0.99 0.01" solref="0.01 1"/>
                '''.format(*[rgba]*4)
            else:
                xml_format['cage'] = ''

        if weld_fingers:
            mocap_tp = '''
            <body mocap="true" name="{}:mocap" pos="0 0 0">
                <geom conaffinity="0" contype="0" pos="0 0 0" rgba="0 0.5 0 0.7" size="0.005 0.005 0.005" type="box"/>
                <geom conaffinity="0" contype="0" pos="0 0 0" rgba="0 0.5 0 0.1" size="1 0.005 0.005" type="box"/>
                <geom conaffinity="0" contype="0" pos="0 0 0" rgba="0 0.5 0 0.1" size="0.005 1 0.001" type="box"/>
                <geom conaffinity="0" contype="0" pos="0 0 0" rgba="0 0.5 0 0.1" size="0.005 0.005 1" type="box"/>
            </body>
            '''
            weld_tp = '<weld body1="{}:mocap" body2="{}" solimp="0.9 0.95 0.001" solref="0.02 1"/>'
            fingertip_names = [x.replace('robot0:', '') for x in FINGERTIP_BODY_NAMES]
            xml_format['finger_mocaps'] = '\n'.join([mocap_tp.format(n) for n in fingertip_names])
            xml_format['finger_welds'] = '\n'.join([weld_tp.format(m, f) for m, f in zip(fingertip_names, FINGERTIP_BODY_NAMES)])
        else:
            xml_format['finger_welds'] = ''
            xml_format['finger_mocaps'] = ''

        initial_qpos = initial_qpos or default_qpos
        hand_env.HandEnv.__init__(self, model_path, n_substeps=n_substeps, initial_qpos=initial_qpos,
                                  relative_control=relative_control, arm_control=True, xml_format=xml_format)
        utils.EzPickle.__init__(self)

    def get_object_contact_points(self, other_body='robot0:'):
        if not self.has_object:
            raise NotImplementedError("Cannot get object contact points in an environment without objects!")

        sim = self.sim
        object_name = 'object'
        object_pos = self.sim.data.get_body_xpos(object_name)
        object_rot = self.sim.data.get_body_xmat(object_name)
        contact_points = []

        # Partially from: https://gist.github.com/machinaut/209c44e8c55245c0d0f0094693053158
        for i in range(sim.data.ncon):
            # Note that the contact array has more than `ncon` entries,
            # so be careful to only read the valid entries.
            contact = sim.data.contact[i]
            body_name_1 = sim.model.body_id2name(sim.model.geom_bodyid[contact.geom1])
            body_name_2 = sim.model.body_id2name(sim.model.geom_bodyid[contact.geom2])

            if other_body in body_name_1 and body_name_2 == object_name or \
               other_body in body_name_2 and body_name_1 == object_name:

                c_force = np.zeros(6, dtype=np.float64)
                mujoco_py.functions.mj_contactForce(sim.model, sim.data, i, c_force)

                # Compute contact point position wrt the object
                rel_contact_pos = object_rot.T @ (contact.pos - object_pos)

                contact_points.append(dict(
                    body1=body_name_1,
                    body2=body_name_2,
                    relative_pos=rel_contact_pos,
                    force=c_force
                ))

        return contact_points

    def _get_body_pose(self, body_name, no_rot=False, euler=False):
        if no_rot:
            rot = np.zeros(4)
        else:
            rot = self.sim.data.get_body_xquat(body_name)
            if euler:
                rot = rotations.quat2euler(rot)
        return np.r_[self.sim.data.get_body_xpos(body_name), rot]

    def _get_site_pose(self, site_name, no_rot=False):
        if no_rot:
            quat = np.zeros(4)
        else:
            # this is very inefficient, avoid computation when possible
            quat = rotations.mat2quat(self.sim.data.get_site_xmat(site_name))
        return np.r_[self.sim.data.get_site_xpos(site_name), quat]

    def _get_palm_pose(self, no_rot=False):
        return self._get_site_pose('robot0:palm_center', no_rot)

    def _get_grasp_center_pose(self, no_rot=False):
        return self._get_site_pose('robot0:grasp_center', no_rot)

    def _get_object_pose(self):
        return self._get_body_pose('object')

    def _get_achieved_goal(self):
        palm_pose = self._get_palm_pose(no_rot=self.ignore_target_rotation)

        if self.has_object:
            pose = self._get_object_pose()
        else:
            pose = palm_pose

        if self.ignore_target_rotation:
            pose[3:] = 0.0

        if self.success_on_grasp_only:
            d = np.linalg.norm(palm_pose[:3] - pose[:3])
            return np.r_[pose, d]

        return pose

    def _set_arm_pose(self, pose: np.ndarray):
        assert pose.size == 7 or pose.size == 3
        reset_mocap2body_xpos(self.sim)
        self.sim.data.mocap_pos[0, :] = np.clip(pose[:3], *self.forearm_bounds)
        if pose.size == 7:
            self.sim.data.mocap_quat[0, :] = pose[3:]

    def get_table_surface_pose(self):
        pose = np.r_[
            self.sim.data.get_body_xpos('table0'),
            self.sim.data.get_body_xquat('table0'),
        ]
        geom = self.sim.model.geom_name2id('table0_geom')
        size = self.sim.model.geom_size[geom].copy()
        pose[2] += size[2]
        return pose

    # GoalEnv methods
    # ----------------------------

    def compute_reward(self, achieved_goal: np.ndarray, goal: np.ndarray, info: dict):
        if self.reward_type == 'sparse':
            success = self._is_success(achieved_goal, goal).astype(np.float32)
            weights = (info or dict()).get('weights')
            if weights is not None:
                success *= weights
            return success - 1.
        else:
            d_pos, d_rot = _goal_distance(achieved_goal, goal, self.ignore_target_rotation)
            # We weigh the difference in position to avoid that `d_pos` (in meters) is completely
            # dominated by `d_rot` (in radians).
            return -(10. * d_pos + d_rot)

    # RobotEnv methods
    # ----------------------------

    def _set_action(self, action):

        assert action.shape == self.action_space.shape
        hand_ctrl = action[:20]
        forearm_ctrl = action[20:] * 0.1

        # set hand action
        hand_env.HandEnv._set_action(self, hand_ctrl)

        # set forearm action
        assert self.sim.model.nmocap == 1
        pos_delta = forearm_ctrl[:3]
        quat_delta = forearm_ctrl[3:]

        if self.ignore_rotation_ctrl:
            quat_delta *= 0.0

        new_pos = self.sim.data.mocap_pos[0] + pos_delta
        new_quat = self.sim.data.mocap_quat[0] + quat_delta
        self._set_arm_pose(np.r_[new_pos, new_quat])

    def _is_success(self, achieved_goal: np.ndarray, desired_goal: np.ndarray):
        d_pos, d_rot = _goal_distance(achieved_goal[..., :7], desired_goal[..., :7], self.ignore_target_rotation)
        achieved_pos = (d_pos < self.distance_threshold).astype(np.float32)
        achieved_rot = (d_rot < self.rotation_threshold).astype(np.float32)
        achieved_all = achieved_pos * achieved_rot
        if self.success_on_grasp_only:
            assert achieved_goal.shape[-1] == 8
            d_palm = achieved_goal[..., 7]
            achieved_grasp = (d_palm < 0.08).astype(np.float32)
            achieved_all *= achieved_grasp
        return achieved_all

    def _env_setup(self, initial_qpos):
        for name, value in initial_qpos.items():
            self.sim.data.set_joint_qpos(name, value)
        reset_mocap_welds(self.sim)
        self.sim.forward()

        # Move end effector into position.
        self._set_arm_pose(self._initial_arm_mocap_pose.copy())
        for _ in range(10):
            self.sim.step()

        if self.has_object:
            self.height_offset = self.sim.data.get_site_xpos('object:center')[2]

    def _viewer_setup(self):
        body_id = self.sim.model.body_name2id('table0') #'robot0:forearm')
        lookat = self.sim.data.body_xpos[body_id]
        for idx, value in enumerate(lookat):
            self.viewer.cam.lookat[idx] = value
        self.viewer.cam.distance = 1.4
        self.viewer.cam.azimuth = 180. #132.
        self.viewer.cam.elevation = -38.

    def _reset_sim(self):
        reset_to_grasp_state = self.grasp_state_reset_p > self.np_random.uniform()

        while True:
            if reset_to_grasp_state:
                assert self.has_object
                self.sim.set_state(self.grasp_state)
                # Fix hand ctrl so that fingers stay close while we update the arm position later
                rel_ctrl = self.relative_control
                self.relative_control = True
                self._set_action(np.zeros(self.action_space.shape))
                self.relative_control = rel_ctrl
            else:
                self.sim.set_state(self.initial_state)

            # Reset initial position of arm.
            new_arm_pose = self._initial_arm_mocap_pose.copy()
            if self.randomize_initial_arm_pos:
                new_arm_pose[:2] += self.np_random.uniform(-0.2, 0.2, size=2)
            self._set_arm_pose(new_arm_pose)
            for _ in range(10):
                self.sim.step()

            # Randomize initial position of object.
            if self.has_object and not reset_to_grasp_state:
                object_qpos = self.sim.data.get_joint_qpos('object:joint').copy()

                if self.randomize_initial_object_pos:
                    object_xpos = self.np_random.uniform(*self.table_safe_bounds)
                else:
                    object_xpos = self._get_palm_pose(no_rot=True)[:2]
                    object_xpos += self.np_random.uniform(-0.005, 0.005, size=2)  # always add small amount of noise

                object_qpos[:2] = object_xpos
                self.sim.data.set_joint_qpos('object:joint', object_qpos)

            self.sim.forward()
            if not self.has_object:
                break
            else:
                object_pos = self._get_object_pose()[:3]
                object_vel = self.sim.data.get_joint_qvel('object:joint')
                object_still = np.linalg.norm(object_vel) < 0.8
                if reset_to_grasp_state:
                    palm_pos = self._get_palm_pose(no_rot=True)[:3]
                    object_on_palm = np.linalg.norm(object_pos - palm_pos) < 0.08
                    if object_still and object_on_palm:
                        break
                else:
                    object_on_table = _check_range(object_pos[:2], *self.table_safe_bounds)
                    if object_still and object_on_table:
                        break
        return True

    def _sample_goal(self):
        goal = np.r_[self.np_random.uniform(*self.table_safe_bounds), 0.0]
        if self.has_object:
            goal[2] = self.height_offset
        if self.np_random.uniform() < self.target_in_the_air_p:
            goal[2] += self.np_random.uniform(0, 0.45)
        goal = np.r_[goal, np.zeros(4)]
        if self.success_on_grasp_only:
            goal = np.r_[goal, 0.]
        return goal

    def _render_callback(self):
        # Assign current state to target object but offset a bit so that the actual object
        # is not obscured.
        goal = self.goal.copy()[:7]
        assert goal.shape == (7,)
        self.sim.data.set_joint_qpos('target:joint', goal)
        self.sim.data.set_joint_qvel('target:joint', np.zeros(6))
        self.sim.forward()

    def _get_obs(self):
        robot_qpos, robot_qvel = robot_get_obs(self.sim)

        dt = self.sim.nsubsteps * self.sim.model.opt.timestep
        forearm_pose = self._get_body_pose('robot0:forearm', euler=True)
        forearm_velp = self.sim.data.get_body_xvelp('robot0:forearm') * dt
        palm_pos = self._get_palm_pose(no_rot=True)[:3]

        object_pose = np.zeros(0)
        object_vel = np.zeros(0)
        object_rel_pos = np.zeros(0)
        if self.has_object:
            object_vel = self.sim.data.get_joint_qvel('object:joint')
            object_pose = self._get_body_pose('object', euler=True)
            object_rel_pos = object_pose[:3] - palm_pos

        observation = np.concatenate([
            forearm_pose, forearm_velp, palm_pos, object_rel_pos,
            robot_qpos, robot_qvel, object_pose, object_vel
        ])
        return {
            'observation': observation,
            'achieved_goal': self._get_achieved_goal().ravel(),
            'desired_goal': self.goal.ravel().copy(),
        }


class HandPickAndPlaceEnv(MovingHandEnv):
    def __init__(self, reward_type='sparse', **kwargs):
        super(HandPickAndPlaceEnv, self).__init__(
            model_path=HAND_PICK_AND_PLACE_XML,
            reward_type=reward_type,
            has_object=True, **kwargs
        )


class MovingHandReachEnv(MovingHandEnv):
    def __init__(self, reward_type='sparse', **kwargs):
        super(MovingHandReachEnv, self).__init__(
            model_path=HAND_MOVE_AND_REACH_XML,
            reward_type=reward_type,
            has_object=False, **kwargs
        )
