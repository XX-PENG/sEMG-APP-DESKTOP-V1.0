"""定长时间序列环形缓冲。"""

from __future__ import annotations

import numpy as np


class SeriesRing:
    """按到达顺序写入 (times, values)，按时间窗读出。

    times 必须是单调递增的（clock.DeviceClock 的重建结果满足该前提），否则窗口查询
    的二分定位不成立。
    """

    def __init__(self, capacity: int, n_fields: int = 1) -> None:
        self.capacity = max(int(capacity), 16)
        self.n_fields = n_fields
        self._t = np.zeros(self.capacity, dtype=np.float64)
        self._v = (
            np.zeros(self.capacity, dtype=np.float32)
            if n_fields == 1
            else np.zeros((self.capacity, n_fields), dtype=np.float32)
        )
        self._head = 0
        self._count = 0
        self.overwritten = 0

    def __len__(self) -> int:
        return self._count

    @property
    def latest_time(self) -> float:
        if self._count == 0:
            return 0.0
        return float(self._t[(self._head - 1) % self.capacity])

    def append(self, times: np.ndarray, values: np.ndarray) -> None:
        n = len(times)
        if n == 0:
            return
        if n > self.capacity:
            keep = n - self.capacity
            self.overwritten += keep
            times = times[keep:]
            values = values[keep:]
            n = self.capacity
        idx = (np.arange(self._head, self._head + n) % self.capacity)
        self._t[idx] = times
        self._v[idx] = values
        self._head = (self._head + n) % self.capacity
        new_count = self._count + n
        self.overwritten += max(0, new_count - self.capacity)
        self._count = min(new_count, self.capacity)

    def window(self, t_from: float, t_to: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        if self._count == 0:
            empty = np.empty(0, dtype=np.float64)
            v_shape = (0,) if self.n_fields == 1 else (0, self.n_fields)
            return empty, np.empty(v_shape, dtype=np.float32)

        start = (self._head - self._count) % self.capacity
        idx = (start + np.arange(self._count)) % self.capacity
        t = self._t[idx]
        lo = int(np.searchsorted(t, t_from, side="left"))
        hi = self._count if t_to is None else int(np.searchsorted(t, t_to, side="right"))
        if hi <= lo:
            empty = np.empty(0, dtype=np.float64)
            v_shape = (0,) if self.n_fields == 1 else (0, self.n_fields)
            return empty, np.empty(v_shape, dtype=np.float32)
        take = idx[lo:hi]
        return self._t[take], self._v[take]

    def tail(self, count: int) -> tuple[np.ndarray, np.ndarray]:
        count = min(count, self._count)
        start = (self._head - count) % self.capacity
        idx = (start + np.arange(count)) % self.capacity
        return self._t[idx], self._v[idx]

    def clear(self) -> None:
        self._head = 0
        self._count = 0
        self.overwritten = 0
