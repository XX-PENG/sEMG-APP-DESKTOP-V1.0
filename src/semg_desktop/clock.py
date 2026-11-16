"""由 seq 重建设备时标。

协议里没有时间戳，只有 uint16 帧序号。直接用主机到达时刻画曲线，会把 BLE 轮转和网关打印
队列的抖动写进采样间隔，波形出现假性疏密；因此把每个设备的时标建模成

    host_time = offset + device_time,  device_time 按 seq 增量累加

device_time 用累加而不是 seq * period：周期是自估的，一旦乘上不断增长的 seq，估计噪声会被
放大成几十毫秒的时标畸变。周期用 2 s 长基线平均（网关轮转打印造成成串到达，逐帧间隔的
中位数会被突发拉偏），offset 用只追赶正误差的小增益 PLL 跟随传输时延。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

_UINT16 = 1 << 16


@dataclass
class ClockStats:
    resyncs: int = 0
    jumps: int = 0
    forward_skips: int = 0
    duplicates: int = 0
    gaps: int = 0
    lost_frames: int = 0


class DeviceClock:
    def __init__(
        self,
        nominal_period: float = 0.02,
        pll_gain: float = 0.02,
        resync_threshold: float = 0.30,
        max_gap: int = 256,
        baseline_seconds: float = 2.0,
        min_frames: int = 20,
    ) -> None:
        self.nominal_period = nominal_period
        self.pll_gain = pll_gain
        self.resync_threshold = resync_threshold
        self.max_gap = max_gap
        self.baseline_seconds = baseline_seconds
        self.min_frames = min_frames
        self.stats = ClockStats()
        self._anchors: deque[tuple[int, float]] = deque()
        self._period = nominal_period
        self._offset = 0.0
        self._dev_time = 0.0
        self._seq_ext = 0
        self._last_seq: int | None = None
        self._last_t: float | None = None
        self._latest = 0.0

    @property
    def frame_period(self) -> float:
        return self._period

    @property
    def latest_time(self) -> float:
        return self._latest

    @property
    def ready(self) -> bool:
        return self._last_seq is not None

    def reset(self) -> None:
        self._last_seq = None
        self._last_t = None
        self._dev_time = 0.0
        self._seq_ext = 0
        self._anchors.clear()
        self._period = self.nominal_period

    def sample_rate(self, samples_per_frame: float) -> float:
        if self._period <= 0:
            return 0.0
        return samples_per_frame / self._period

    def place(self, seq: int, n: int, t_arrival: float) -> np.ndarray:
        """返回本帧 n 个样本的重建时刻（float64，等间隔铺满一个帧周期）。"""
        if n <= 0:
            return np.empty(0, dtype=np.float64)

        if self._last_seq is None:
            self._dev_time = 0.0
            self._seq_ext = seq
            self._offset = t_arrival
            self._last_seq = seq
            self._last_t = t_arrival
            self._anchors.append((seq, t_arrival))
            return self._times(n)

        delta = (seq - self._last_seq) & (_UINT16 - 1)
        if delta == 0:
            self.stats.duplicates += 1
            delta = 1

        # seq 大幅跳变在 uint16 回绕下最可能是节点重启（seq 归零后被算成 +65536-old），
        # 不是真的丢了几千帧。按一帧推进设备时间，并清空周期估计窗口重新基线。
        restarted = delta > self.max_gap
        if restarted:
            self.stats.resyncs += 1
            delta = 1
            self._anchors.clear()

        self._seq_ext += delta
        self._dev_time += delta * self._period
        self._observe_period(t_arrival)

        if restarted:
            self._offset = t_arrival - self._dev_time
        else:
            if delta > 1:
                self.stats.gaps += 1
                self.stats.lost_frames += delta - 1
            err = t_arrival - (self._offset + self._dev_time)
            if err > self.resync_threshold:
                # 到达落后于设备时钟：传输停顿或重连，重新锚定
                self.stats.jumps += 1
                self._offset += err
            elif err < -self.resync_threshold:
                # 到达超前于设备时钟：只可能是全速灌文件，链路不可能比发送端还快，
                # 此时绝不回退时标，否则整段数据被挤到一起并破坏单调性
                self.stats.forward_skips += 1
            elif err > 0:
                # 传输延迟只会让到达变晚，PLL 只追赶正误差；
                # 负误差交给周期估计消化，回拉会把时间轴整体压短
                self._offset += self.pll_gain * err

        self._last_seq = seq
        self._last_t = t_arrival
        return self._times(n)

    def _observe_period(self, t_arrival: float) -> None:
        """长基线平均估帧周期：cand = 到达跨度 / seq 跨度。

        基线里混进长时间断流时比值会超出名义带宽，直接丢弃这次观测、沿用上一个好估计，
        所以断流既不会拉长周期估计，也不会被突发到达压短。
        """
        self._anchors.append((self._seq_ext, t_arrival))
        while len(self._anchors) > 2 and t_arrival - self._anchors[0][1] > self.baseline_seconds:
            self._anchors.popleft()
        if len(self._anchors) < 2:
            return
        span_frames = self._seq_ext - self._anchors[0][0]
        if span_frames < self.min_frames:
            return
        cand = (t_arrival - self._anchors[0][1]) / span_frames
        lo, hi = self.nominal_period * 0.5, self.nominal_period * 2.0
        if lo <= cand <= hi:
            self._period = cand

    def _times(self, n: int) -> np.ndarray:
        t0 = self._offset + self._dev_time
        out = t0 + np.arange(n, dtype=np.float64) * (self._period / n)
        self._latest = float(out[-1])
        return out
