import math

import numpy as np
import pytest

from semg_desktop.ahrs import AttitudeTracker, MahonyAHRS
from semg_desktop.analytics import attitude_deg

RATE = 200.0
DT = 1.0 / RATE


def rx(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def ry(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rotvec(w):
    """罗德里格斯公式：绕 w 转 |w| 弧度的精确旋转矩阵。"""
    th = float(np.linalg.norm(w))
    if th < 1e-12:
        return np.eye(3)
    k = w / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * (K @ K)


def euler_of(R):
    roll = math.degrees(math.atan2(R[2, 1], R[2, 2]))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -R[2, 0]))))
    yaw = math.degrees(math.atan2(R[1, 0], R[0, 0]))
    return roll, pitch, yaw


def assert_angles_close(got, want, tol):
    """角度比较必须走最短弧，否则 +180 与 -180 会被判成差 360°。"""
    for g, w in zip(got, want):
        assert abs((g - w + 180.0) % 360.0 - 180.0) <= tol, (got, want)


def simulate(R0, omega_dps, seconds, accel_noise=0.0):
    """按真实姿态生成自洽的加速度与角速度，喂给滤波器。"""
    f = MahonyAHRS(cutoff_hz=0.3)
    w = np.radians(omega_dps)
    rng = np.random.default_rng(7)
    steps = int(seconds * RATE)
    for i in range(steps):
        R = R0 @ rotvec(w * (i * DT))
        a = R.T @ np.array([0.0, 0.0, 1.0])
        if accel_noise:
            a = a + rng.normal(0, accel_noise, 3)
        f.update(float(a[0]), float(a[1]), float(a[2]), *omega_dps, DT)
    return f.euler_deg, euler_of(R0 @ rotvec(w * seconds))


def test_static_alignment_matches_gravity_convention():
    """与 analytics.attitude_deg 同一套符号，否则曲线和模型会互相矛盾。"""
    cases = [(0.0, 0.0), (30.0, 0.0), (0.0, 20.0), (-45.0, -25.0), (170.0, 10.0)]
    accel = []
    for roll_deg, pitch_deg in cases:
        r, p = math.radians(roll_deg), math.radians(pitch_deg)
        a = (ry(p) @ rx(r)).T @ np.array([0.0, 0.0, 1.0])
        accel.append(a)
        f = MahonyAHRS()
        f.update(float(a[0]), float(a[1]), float(a[2]), 0.0, 0.0, 0.0, DT)
        roll, pitch, yaw = f.euler_deg
        assert roll == pytest.approx(roll_deg, abs=1e-6)
        assert pitch == pytest.approx(pitch_deg, abs=1e-6)
        assert yaw == pytest.approx(0.0, abs=1e-6)
    ref_roll, ref_pitch = attitude_deg(np.asarray(accel, dtype=np.float32))
    for i, (roll_deg, pitch_deg) in enumerate(cases):
        assert float(ref_roll[i]) == pytest.approx(roll_deg, abs=1e-3)
        assert float(ref_pitch[i]) == pytest.approx(pitch_deg, abs=1e-3)


def test_yaw_follows_gyro_about_gravity_axis():
    """绕重力轴转时重力向量不变，只靠加速度计的方案在这里完全不动——这正是要修的。"""
    est, truth = simulate(np.eye(3), (0.0, 0.0, 90.0), 1.0)
    assert est[2] == pytest.approx(90.0, abs=1.0)
    assert abs(est[0]) < 0.5 and abs(est[1]) < 0.5
    assert est[2] == pytest.approx(truth[2], abs=1.0)

    est, _ = simulate(np.eye(3), (0.0, 0.0, -45.0), 2.0)
    assert est[2] == pytest.approx(-90.0, abs=1.0)


def test_tilted_yaw_keeps_roll_and_pitch():
    est, truth = simulate(ry(math.radians(-15)) @ rx(math.radians(30)), (0.0, 0.0, 90.0), 2.0)
    assert_angles_close(est, truth, 1.5)


@pytest.mark.parametrize("omega", [(45.0, 0.0, 0.0), (0.0, 60.0, 0.0), (30.0, -40.0, 90.0)])
def test_tracks_body_rate_rotation(omega):
    est, truth = simulate(ry(math.radians(-15)) @ rx(math.radians(20)), omega, 3.0)
    assert_angles_close(est, truth, 1.5)


def test_noisy_accel_does_not_wreck_attitude():
    est, truth = simulate(ry(math.radians(-15)) @ rx(math.radians(30)), (0.0, 0.0, 90.0), 2.0,
                          accel_noise=0.05)
    assert_angles_close(est, truth, 2.0)


def test_linear_acceleration_is_rejected():
    """模长远离 1g 时只做陀螺积分，不能让线加速度把姿态拽歪。"""
    f = MahonyAHRS()
    for _ in range(200):
        f.update(0.0, 0.0, 1.0, 0.0, 0.0, 0.0, DT)
    for _ in range(200):
        f.update(0.0, 0.0, 3.0, 0.0, 0.0, 90.0, DT)
    roll, pitch, yaw = f.euler_deg
    assert yaw == pytest.approx(90.0, abs=1.0)
    assert abs(roll) < 0.5 and abs(pitch) < 0.5


def test_nonpositive_dt_is_noop():
    f = MahonyAHRS()
    f.update(0.0, 0.0, 1.0, 0.0, 0.0, 90.0, DT)
    before = f.euler_deg
    f.update(0.0, 0.0, 1.0, 0.0, 0.0, 90.0, 0.0)
    f.update(0.0, 0.0, 1.0, 0.0, 0.0, 90.0, -1.0)
    assert f.euler_deg == before


def test_unaligned_without_gravity():
    f = MahonyAHRS()
    assert f.aligned is False
    assert f.euler_deg == (0.0, 0.0, 0.0)
    f.update(0.0, 0.0, 0.0, 0.0, 0.0, 90.0, DT)
    assert f.aligned is False


def test_tracker_uses_reconstructed_timestamps():
    tracker = AttitudeTracker(nominal_dt=DT, cutoff_hz=0.3)
    assert tracker.attitude is None
    n = 200
    times = np.arange(n, dtype=np.float64) * DT
    samples = np.tile(np.array([0, 0, 1, 0, 0, 90, 0, 0, 0], dtype=np.float32), (n, 1))
    tracker.feed(times, samples)
    roll, pitch, yaw = tracker.attitude
    assert yaw == pytest.approx(90.0, abs=1.0)
    assert abs(roll) < 0.5 and abs(pitch) < 0.5


def test_tracker_clamps_timestamp_jump():
    """重连/重同步会让时标跳变，一次大 dt 不能把姿态甩飞。"""
    tracker = AttitudeTracker(nominal_dt=DT, cutoff_hz=0.3)
    row = np.array([[0, 0, 1, 0, 0, 90, 0, 0, 0]], dtype=np.float32)
    tracker.feed(np.array([0.0]), row)
    tracker.feed(np.array([0.005]), row)
    steady = tracker.attitude[2]
    tracker.feed(np.array([5.0]), row)  # 断流 5 s 后恢复
    assert tracker.attitude[2] - steady == pytest.approx(90.0 * DT, abs=0.5)


def test_tracker_empty_frame_is_noop():
    tracker = AttitudeTracker(nominal_dt=DT)
    tracker.feed(np.empty(0), np.empty((0, 9), dtype=np.float32))
    assert tracker.attitude is None