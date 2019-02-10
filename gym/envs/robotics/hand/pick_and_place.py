import os
import numpy as np

from gym import utils, error
from gym.envs.robotics import rotations, hand_env
from gym.envs.robotics.utils import robot_get_obs

try:
    import mujoco_py
except ImportError as e:
    raise error.DependencyNotInstalled("{}. (HINT: you need to install mujoco_py, "
                                       "and also perform the setup instructions here: "
                                       "https://github.com/openai/mujoco-py/.)".format(e))


HAND_PICK_AND_PLACE_XML = os.path.join('hand', 'pick_and_place.xml')


def quat_from_angle_and_axis(angle, axis):
    assert axis.shape == (3,)
    axis /= np.linalg.norm(axis)
    quat = np.concatenate([[np.cos(angle / 2.)], np.sin(angle / 2.) * axis])
    quat /= np.linalg.norm(quat)
    return quat


class _PickAndPlaceEnv(hand_env.HandEnv, utils.EzPickle):
    def __init__(self, model_path, reward_type, initial_qpos=None,
                 randomize_initial_position=True, randomize_initial_rotation=True,
                 distance_threshold=0.01, rotation_threshold=0.1, n_substeps=20):

        self.object_range = 0.15
        self.target_range = 0.15
        self.target_in_the_air = True
        self.has_object = True
        self.ignore_target_rotation = False
        self.randomize_initial_rotation = randomize_initial_rotation
        self.randomize_initial_position = randomize_initial_position
        self.distance_threshold = distance_threshold
        self.rotation_threshold = rotation_threshold
        self.reward_type = reward_type

        initial_qpos = initial_qpos or {
            'object:joint': [1.25, 0.53, 0.4, 1., 0., 0., 0.],
            'robot0:ARM_Tx': 0.405,
            'robot0:ARM_Ty': 0.48,
            'robot0:ARM_Tz': 0.0,
            'robot0:ARM_Rx': np.pi,
        }

        hand_env.HandEnv.__init__(self, model_path, n_substeps=n_substeps, initial_qpos=initial_qpos or dict(),
                                  relative_control=False, arm_control=True)
        utils.EzPickle.__init__(self)

    def _get_achieved_goal(self):
        if self.has_object:
            qpos = self.sim.data.get_joint_qpos('object:joint')
        else:
            # FIXME
            qpos = self.sim.data.get_joint_qpos('robot0:wall_mount')
        assert qpos.shape == (7,)
        return qpos.copy()

    def _goal_distance(self, goal_a, goal_b):
        assert goal_a.shape == goal_b.shape
        assert goal_a.shape[-1] == 7

        delta_pos = goal_a[..., :3] - goal_b[..., :3]
        d_pos = np.linalg.norm(delta_pos, axis=-1)
        d_rot = np.zeros_like(goal_b[..., 0])

        if not self.ignore_target_rotation:
            quat_a, quat_b = goal_a[..., 3:], goal_b[..., 3:]
            # Subtract quaternions and extract angle between them.
            quat_diff = rotations.quat_mul(quat_a, rotations.quat_conjugate(quat_b))
            angle_diff = 2 * np.arccos(np.clip(quat_diff[..., 0], -1., 1.))
            d_rot = angle_diff
        assert d_pos.shape == d_rot.shape
        return d_pos, d_rot

    # GoalEnv methods
    # ----------------------------

    def compute_reward(self, achieved_goal: np.ndarray, goal: np.ndarray, info: dict):
        if self.reward_type == 'sparse':
            success = self._is_success(achieved_goal, goal).astype(np.float32)
            return success - 1.
        else:
            d_pos, d_rot = self._goal_distance(achieved_goal, goal)
            # We weigh the difference in position to avoid that `d_pos` (in meters) is completely
            # dominated by `d_rot` (in radians).
            return -(10. * d_pos + d_rot)

    # RobotEnv methods
    # ----------------------------

    def _is_success(self, achieved_goal: np.ndarray, desired_goal: np.ndarray):
        d_pos, d_rot = self._goal_distance(achieved_goal, desired_goal)
        achieved_pos = (d_pos < self.distance_threshold).astype(np.float32)
        achieved_rot = (d_rot < self.rotation_threshold).astype(np.float32)
        achieved_both = achieved_pos * achieved_rot
        return achieved_both

    def _env_setup(self, initial_qpos):
        for name, value in initial_qpos.items():
            self.sim.data.set_joint_qpos(name, value)
        self.sim.forward()

        for _ in range(10):
            self.sim.step()

        self.initial_arm_xpos = self.sim.data.get_site_xpos('robot0:wall_mount:site').copy()
        if self.has_object:
            self.height_offset = self.sim.data.get_site_xpos('object:center')[2]

    def _viewer_setup(self):
        body_id = self.sim.model.body_name2id('robot0:wall_mount')
        lookat = self.sim.data.body_xpos[body_id]
        for idx, value in enumerate(lookat):
            self.viewer.cam.lookat[idx] = value
        self.viewer.cam.distance = 2.5
        self.viewer.cam.azimuth = 132.
        self.viewer.cam.elevation = -14.

    def _reset_sim(self):
        self.sim.set_state(self.initial_state)

        # Randomize initial position of object.
        if self.has_object:
            object_qpos = self.sim.data.get_joint_qpos('object:joint').copy()

            # object_xpos = self.initial_arm_xpos[:2]
            # while np.linalg.norm(object_xpos - self.initial_arm_xpos[:2]) < 0.1:
            #     offset = self.np_random.uniform(-self.object_range, self.object_range, size=2)
            #     object_xpos = self.initial_arm_xpos[:2] + offset
            #
            # object_qpos[:2] = object_xpos
            self.sim.data.set_joint_qpos('object:joint', object_qpos)

        self.sim.forward()
        return True

    def _sample_goal(self):
        goal = self.initial_arm_xpos[:3] + self.np_random.uniform(-self.target_range, self.target_range, size=3)
        if self.has_object:
            goal[2] = self.height_offset
            if self.target_in_the_air and self.np_random.uniform() < 0.5:
                goal[2] += self.np_random.uniform(0, 0.45)
        goal = np.r_[goal, np.zeros(4)]
        return goal

    def _render_callback(self):
        # Assign current state to target object but offset a bit so that the actual object
        # is not obscured.
        goal = self.goal.copy()
        assert goal.shape == (7,)
        self.sim.data.set_joint_qpos('target:joint', goal)
        self.sim.data.set_joint_qvel('target:joint', np.zeros(6))

        if 'object_hidden' in self.sim.model.geom_names:
            hidden_id = self.sim.model.geom_name2id('object_hidden')
            self.sim.model.geom_rgba[hidden_id, 3] = 1.
        self.sim.forward()

    def _get_obs(self):
        robot_qpos, robot_qvel = robot_get_obs(self.sim)
        object_qvel = np.zeros(0)
        if self.has_object:
            object_qvel = self.sim.data.get_joint_qvel('object:joint')
        achieved_goal = self._get_achieved_goal().ravel()
        observation = np.concatenate([robot_qpos, robot_qvel, object_qvel, achieved_goal])
        return {
            'observation': observation,
            'achieved_goal': achieved_goal,
            'desired_goal': self.goal.ravel().copy(),
        }


class HandPickAndPlaceEnv(_PickAndPlaceEnv):
    def __init__(self, reward_type='sparse'):
        super(HandPickAndPlaceEnv, self).__init__(model_path=HAND_PICK_AND_PLACE_XML, reward_type=reward_type)
