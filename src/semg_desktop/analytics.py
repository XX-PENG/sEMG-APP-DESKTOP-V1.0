"""sEMG / IMU 派生量计算。

流式部分只有包络估计（整流 + 二阶低通，状态跨帧保持），其余都是从时间窗快照上算的纯函数，
便于单测和回放复用。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal


@dataclass
class ChannelMetrics:
    count: int = 0
    rate: float = 0.0
    p2p: float = 0.0
    rms: float = 0.0
    mav: float = 0.0
    iemg: float = 0.0
    env_peak: float = 0.0
    saturated: int = 0


def channel_metrics(t: np.ndarray, x: np.ndarray, env: np.ndarray | None = None) -> ChannelMetrics:
    m = ChannelMetrics()
    if x.size == 0 or t.size < 2:
        return m
    span = float(t[-1] - t[0])
    m.count = int(x.size)
    m.rate = (x.size - 1) / span if span > 0 else 0.0
    m.p2p = float(np.max(x) - np.min(x))
    m.rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    m.iemg = float(np.sum(np.abs(x, dtype=np.float64)) * (span / max(x.size - 1, 1)))
    m.saturated = int(np.count_nonzero((x >= 32767) | (x <= -32768)))
    if env is not None and env.size:
        m.mav = float(np.mean(env))
        m.env_peak = float(np.max(env))
    return m


class EnvelopeEstimator:
    """全波整流 + Butterworth 低通，按固定输出速率抽稀。

    采样率取自 DeviceClock 的重建结果，所以节点侧 CONFIG_FREERTOS_HZ 改成 100 之后
    滤波器截止频率仍然落在 4 Hz，不会跟着漂。
    """

    def __init__(self, cutoff_hz: float = 4.0, output_rate_hz: float = 200.0, order: int = 2) -> None:
        self.cutoff_hz = cutoff_hz
        self.output_rate_hz = output_rate_hz
        self.order = order
        self._fs = 0.0
        self._sos: np.ndarray | None = None
        self._zi: np.ndarray | None = None

    def reset(self) -> None:
        self._fs = 0.0
        self._sos = None
        self._zi = None

    def update(self, t: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if x.size == 0 or t.size != x.size:
            return np.empty(0), np.empty(0)
        fs = self._measure_rate(t)
        if fs <= 0:
            return np.empty(0), np.empty(0)
        if self._sos is None or abs(fs - self._fs) > 0.05 * self._fs:
            self._build(fs)
        nyq = fs / 2.0
        if self.cutoff_hz >= nyq:
            return t[:: max(1, int(round(fs / self.output_rate_hz)))], np.abs(x)[:: max(1, int(round(fs / self.output_rate_hz)))]

        y, self._zi = signal.sosfilt(self._sos, np.abs(x), zi=self._zi)
        k = max(1, int(round(fs / self.output_rate_hz)))
        blocks = y.size // k
        if blocks == 0:
            return np.empty(0), np.empty(0)
        env = y[: blocks * k].reshape(blocks, k).mean(axis=1)
        t_env = t[k - 1 :: k][:blocks]
        return t_env, env.astype(np.float32)

    def _measure_rate(self, t: np.ndarray) -> float:
        if t.size < 2:
            return self._fs or 0.0
        span = float(t[-1] - t[0])
        if span <= 0:
            return self._fs or 0.0
        return (t.size - 1) / span

    def _build(self, fs: float) -> None:
        nyq = fs / 2.0
        self._sos = signal.butter(self.order, self.cutoff_hz / nyq, btype="low", output="sos")
        self._zi = signal.sosfilt_zi(self._sos) * 0.0
        self._fs = fs


def psd_db(x: np.ndarray, fs: float, nperseg: int = 2048) -> tuple[np.ndarray, np.ndarray]:
    """单边功率谱，dB/Hz。窗口不够长时自动缩短 nperseg。"""
    if x.size < 16 or fs <= 0:
        return np.empty(0), np.empty(0)
    nperseg = int(min(nperseg, x.size))
    freqs, p = signal.welch(x, fs=fs, window="hann", nperseg=nperseg, detrend="constant")
    return freqs, 10.0 * np.log10(np.maximum(p, 1e-20))


def attitude_deg(accel: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """由加速度求 roll / pitch（deg），采用重力向量约定，不含航向。

    磁力计未做硬磁/软磁标定，且网关输出的是原始 int16，所以这里不给 yaw。
    """
    if accel.size == 0:
        return np.empty(0), np.empty(0)
    ax, ay, az = accel[:, 0].astype(np.float64), accel[:, 1].astype(np.float64), accel[:, 2].astype(np.float64)
    roll = np.degrees(np.arctan2(ay, az))
    denom = np.sqrt(ay * ay + az * az)
    pitch = np.degrees(np.arctan2(-ax, np.maximum(denom, 1e-6)))
    return roll.astype(np.float32), pitch.astype(np.float32)


def smooth_mean(x: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or x.size == 0:
        return x
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(x, kernel, mode="same").astype(np.float32)
