"""陀螺仪 + 加速度计的 Mahony 互补滤波姿态解算。

航向由陀螺积分得到：磁力计是未标定原始 int16，不能参与修正，所以 yaw 会随零偏缓慢漂移，
UI 侧提供航向归零。roll/pitch 由重力方向持续校正，长期不漂。

在采集线程里按样本率（200 Hz）逐样本调用，因此用 math 标量而非 numpy，避免每样本的
数组构造开销。四元数约定为 body->world 旋转，欧拉角按 ZYX(yaw-pitch-roll) 提取，与
analytics.attitude_deg 的重力向量符号一致。
"""

from __future__ import annotations

import math

import numpy as np

_DPS_TO_RAD = math.pi / 180.0
_RAD_TO_DEG = 180.0 / math.pi
_ACCEL_MIN_G, _ACCEL_MAX_G = 0.75, 1.25  # 模长超出此区间视为有线加速度，本轮只积分陀螺
_MAX_GAP_S = 0.1


class MahonyAHRS:
    def __init__(self, cutoff_hz: float = 0.3, bias_hz: float = 0.01) -> None:
        self._kp = 2.0 * math.pi * max(cutoff_hz, 0.0)
        self._ki = 2.0 * math.pi * max(bias_hz, 0.0)
        self._q = (1.0, 0.0, 0.0, 0.0)
        self._bias = [0.0, 0.0, 0.0]
        self._aligned = False

    @property
    def aligned(self) -> bool:
        return self._aligned

    def update(self, ax: float, ay: float, az: float,
               gx: float, gy: float, gz: float, dt: float) -> None:
        """accel 单位 g，gyro 单位 dps，dt 单位 s。"""
        if dt <= 0.0:
            return
        gx, gy, gz = gx * _DPS_TO_RAD, gy * _DPS_TO_RAD, gz * _DPS_TO_RAD

        norm = math.sqrt(ax * ax + ay * ay + az * az)
        usable = _ACCEL_MIN_G <= norm <= _ACCEL_MAX_G
        if usable:
            ax, ay, az = ax / norm, ay / norm, az / norm
            if not self._aligned:
                self._align(ax, ay, az)

        if not self._aligned:
            return

        if usable:
            w, x, y, z = self._q
            # 估计的重力方向在机体系的分量，即 R^T @ [0,0,1]
            vx = 2.0 * (x * z - w * y)
            vy = 2.0 * (w * x + y * z)
            vz = w * w - x * x - y * y + z * z
            ex, ey, ez = ay * vz - az * vy, az * vx - ax * vz, ax * vy - ay * vx
            self._bias[0] += self._ki * ex * dt
            self._bias[1] += self._ki * ey * dt
            self._bias[2] += self._ki * ez * dt
            gx += self._kp * ex + self._bias[0]
            gy += self._kp * ey + self._bias[1]
            gz += self._kp * ez + self._bias[2]

        w, x, y, z = self._q
        h = 0.5 * dt
        w, x, y, z = (
            w + h * (-x * gx - y * gy - z * gz),
            x + h * (w * gx + y * gz - z * gy),
            y + h * (w * gy - x * gz + z * gx),
            z + h * (w * gz + x * gy - y * gx),
        )
        n = math.sqrt(w * w + x * x + y * y + z * z)
        if n > 0.0:
            self._q = (w / n, x / n, y / n, z / n)

    def _align(self, ax: float, ay: float, az: float) -> None:
        """首帧直接用重力摆正 roll/pitch，航向置零，省掉开机时那段收敛摆动。"""
        roll = math.atan2(ay, az)
        pitch = math.atan2(-ax, math.hypot(ay, az))
        cr, sr = math.cos(roll * 0.5), math.sin(roll * 0.5)
        cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
        self._q = (cp * cr, cp * sr, sp * cr, -sp * sr)
        self._aligned = True

    @property
    def euler_deg(self) -> tuple[float, float, float]:
        w, x, y, z = self._q
        roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
        pitch = math.asin(max(-1.0, min(1.0, 2.0 * (w * y - x * z))))
        yaw = math.atan2(2.0 * (x * y + w * z), 1.0 - 2.0 * (y * y + z * z))
        return roll * _RAD_TO_DEG, pitch * _RAD_TO_DEG, yaw * _RAD_TO_DEG


class AttitudeTracker:
    """按重建时标喂样本，替 UI 维护一个设备的最新姿态。

    时标来自 DeviceClock，重连/重同步时会跳变，所以 dt 落在合理区间外就退回名义步长，
    不让一次积分把姿态甩飞。
    """

    def __init__(self, nominal_dt: float, cutoff_hz: float = 0.3) -> None:
        self._nominal_dt = max(nominal_dt, 1e-4)
        self._ahrs = MahonyAHRS(cutoff_hz=cutoff_hz)
        self._last_t: float | None = None

    def feed(self, times: np.ndarray, samples: np.ndarray) -> None:
        n = len(times)
        if n == 0:
            return
        # 帧内样本等间隔铺满一个帧周期，用实测跨度当步长兜底，不依赖配置里的名义采样率
        fallback = (float(times[-1]) - float(times[0])) / (n - 1) if n > 1 else self._nominal_dt
        rows = samples.tolist()
        last_t = self._last_t
        for t, row in zip(times, rows):
            t = float(t)
            dt = t - last_t if last_t is not None else fallback
            if not 0.0 < dt <= _MAX_GAP_S:
                dt = fallback
            last_t = t
            # JY931 加速度输出的是重力向量（静置 az≈-1）而非比力（az≈+1），融合前整体取反；
            # 陀螺仍是右手系不动。只翻加速度而不翻陀螺，否则 pitch/yaw 转向会反向
            self._ahrs.update(-row[0], -row[1], -row[2], row[3], row[4], row[5], dt)
        self._last_t = last_t

    @property
    def attitude(self) -> tuple[float, float, float] | None:
        return self._ahrs.euler_deg if self._ahrs.aligned else None