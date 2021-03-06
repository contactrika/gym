import uuid

import numpy as np
from mujoco_py import const as mj_const

from gym.envs.robotics import rotations


def quat_angle_diff(quat_a, quat_b):
    quat_diff = rotations.quat_mul(quat_a, rotations.quat_conjugate(quat_b))
    angle_diff = 2 * np.arccos(np.clip(quat_diff[..., 0], -1., 1.))
    return np.abs(rotations.normalize_angles(angle_diff))


def pose_to_mat(pose: np.ndarray) -> np.ndarray:
    rot_mat = rotations.quat2mat(pose[..., 3:])
    mat = np.zeros(pose.shape[:-1] + (4, 4))
    mat[..., :3, :3] = rot_mat
    mat[..., :3, 3] = pose[..., :3]
    mat[..., 3, 3] = 1.0
    return mat


def mat_to_pose(mat: np.ndarray) -> np.ndarray:
    rot_mat = mat[..., :3, :3]
    quat = rotations.mat2quat(rot_mat)
    pos = mat[..., :3, 3]
    return np.concatenate([pos, quat], axis=-1)


def get_tf(world_to_b: np.ndarray, world_to_a: np.ndarray) -> np.ndarray:
    """Returns a_to_b pose"""
    pos_tf = world_to_b[..., :3] - world_to_a[..., :3]
    q = rotations.quat_mul(rotations.quat_identity(), rotations.quat_conjugate(world_to_a[..., 3:]))
    pos_tf = rotations.quat_rot_vec(q, pos_tf)
    quat_tf = rotations.quat_mul(world_to_b[..., 3:], rotations.quat_conjugate(world_to_a[..., 3:]))
    return np.concatenate([pos_tf, quat_tf], axis=-1)


def apply_tf_old(a_to_b: np.ndarray, world_to_a: np.ndarray) -> np.ndarray:
    """Returns world_to_b pose"""
    a_to_b_mat = pose_to_mat(a_to_b)
    world_to_a_mat = pose_to_mat(world_to_a)
    world_to_b_mat = world_to_a_mat @ a_to_b_mat
    return mat_to_pose(world_to_b_mat)


def apply_tf(a_to_b: np.ndarray, world_to_a: np.ndarray) -> np.ndarray:
    """Returns world_to_b pose"""
    pos_only = False
    if world_to_a.size == 3:
        world_to_a = np.r_[world_to_a, 1., 0., 0., 0.]
        pos_only = True
    if a_to_b.size == 3:
        a_to_b = np.r_[a_to_b, 1., 0., 0., 0.]
    new_pos = world_to_a[:3] + rotations.quat_rot_vec(world_to_a[3:], a_to_b[:3])
    # new_quat = rotations.quat_mul(a_to_b[3:], world_to_a[3:])
    new_quat = rotations.quat_mul(world_to_a[3:], a_to_b[3:])
    if pos_only:
        return new_pos
    return np.concatenate([new_pos, new_quat], axis=-1)


def render_pose(pose: np.ndarray, viewer, label="", size=0.2, unique_id=None, unique_label=False):

    if viewer is None:
        return

    rgba = np.r_[np.random.uniform(0.2, 1.0, 3), 1.]
    extra_kwargs = dict()
    if unique_label:
        unique_id = hash(label) % np.iinfo(np.int32).max
    if unique_id is not None:
        extra_kwargs['dataid'] = unique_id
        with viewer._gui_lock:
            existing = [i for i, m in enumerate(viewer._markers) if m.get('dataid') == unique_id]
            if len(existing) == 1:
                rgba = viewer._markers[existing[0]]['rgba']
            for i in existing[::-1]:
                del viewer._markers[i]

    pos = pose[:3]
    if pose.size == 6:
        quat = rotations.euler2quat(pose[3:])
    elif pose.size == 7:
        quat = pose[3:]
    else:
        viewer.add_marker(
            pos=pos, label=label, type=mj_const.GEOM_SPHERE, size=np.ones(3)*0.01,
            rgba=rgba, specular=0., **extra_kwargs
        )
        return
    for i in range(3):
        rgba = np.zeros(4)
        rgba[[i, 3]] = 1.
        tf = [np.r_[0., np.pi/2, 0.], np.r_[np.pi/2, np.pi/1, 0.], np.r_[0., 0., 0.]][i]
        rot = rotations.quat_mul(quat, rotations.euler2quat(tf))
        viewer.add_marker(
            pos=pos, mat=rotations.quat2mat(rot).flatten(), label=(label if i == 0 else ""),
            type=mj_const.GEOM_ARROW, size=np.r_[0.01, 0.01, size], rgba=rgba, specular=0., **extra_kwargs
        )


def render_box(viewer, bounds: np.ndarray=None, pose: np.ndarray=None, size: np.ndarray=None,
               label="", opacity=0.2, unique_id=None, unique_label=False):

    if viewer is None:
        return

    extra_kwargs = dict()
    if unique_label:
        unique_id = hash(label) % np.iinfo(np.int32).max
    if unique_id is not None:
        extra_kwargs['dataid'] = unique_id
        with viewer._gui_lock:
            existing = [i for i, m in enumerate(viewer._markers) if m['dataid'] == unique_id]
            for i in existing[::-1]:
                del viewer._markers[i]

    if bounds is not None:
        assert size is None
        assert bounds.shape == (3, 2)
        pose = pose or np.zeros(3)
        pose[:3] = bounds.mean(axis=1)
        size = np.abs(bounds[:, 1] - bounds[:, 0]) / 2.0
    else:
        assert pose is not None
        assert size is not None
    pos = pose[:3]
    if pose.size == 6:
        mat = rotations.euler2mat(pose[3:])
    elif pose.size == 7:
        mat = rotations.quat2mat(pose[3:])
    else:
        mat = np.eye(3)
    viewer.add_marker(
        pos=pos, mat=mat.flatten(), label=label, type=mj_const.GEOM_BOX, size=size,
        rgba=np.r_[1., 0., 0., opacity], specular=0., **extra_kwargs
    )


class TFDebugger:

    i = 0
    ob1_pose = None
    ob2_pose = None
    unique_ids = None
    tf = None

    def __init__(self):
        raise NotImplementedError

    @classmethod
    def reset(cls):
        cls.i = 0
        cls.ob1_pose = np.r_[0.025, 0.025, 0.825, 1, 0, 0, 0]
        cls.ob2_pose = np.r_[0.025, 0.25, 0.825, 1, 0, 0, 0]
        cls.tf = None
        cls.unique_ids = np.random.randint(1_000, 1_000_000_000, size=3, dtype=int)

    @classmethod
    def step(cls, viewer):

        q1 = rotations.euler2quat(np.r_[0.01, 0.01, 0.05])
        q0 = cls.ob1_pose[3:].copy()
        q_common = rotations.quat_mul(q0, q1)

        cls.ob1_pose[3:] = q_common.copy()
        render_pose(cls.ob1_pose, viewer, unique_id=cls.unique_ids[0].item())

        cls.ob2_pose[3:] = q_common.copy()
        render_pose(cls.ob2_pose, viewer, unique_id=cls.unique_ids[1].item())

        if cls.i < 37:
            cls.tf = get_tf(cls.ob1_pose, cls.ob2_pose)  # 2_to_1
            cls.i += 1
        else:
            ob1_pose_back = apply_tf(cls.tf, cls.ob2_pose)
            ob1_pose_back[2] -= 0.0
            render_pose(apply_tf(cls.tf, cls.ob2_pose), viewer, unique_id=cls.unique_ids[2].item())
