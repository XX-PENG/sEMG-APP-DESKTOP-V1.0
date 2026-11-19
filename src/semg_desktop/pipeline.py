"""采集管线：source -> parser -> clock -> ring / analytics / recorder。

一个后台线程独占读源与解析，UI 只通过带锁的快照接口取数，两边解耦，因此串口刷到
160 KB/s 时界面帧率不会跟着抖。分析量（包络）在采集线程里流式算，功率谱这类按需计算
的留在 UI 线程从快照上做。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .ahrs import AttitudeTracker
from .analytics import EnvelopeEstimator, channel_metrics
from .clock import DeviceClock
from .config import Config
from .protocol import (
    DeviceInfo,
    GatewayParser,
    ImuFrame,
    ParserStats,
    SemgFrame,
    StatusEvent,
)
from .recorder import Recording
from .ring import SeriesRing
from .sources import DataSource, SourceError

SEMG_FIELDS = 1
IMU_FIELDS = 9
_UINT16_MASK = 0xFFFF


@dataclass
class DeviceState:
    info: DeviceInfo
    index: int | None
    clock: DeviceClock
    ring: SeriesRing
    env_ring: SeriesRing
    env_est: EnvelopeEstimator
    frames: int = 0
    samples: int = 0
    drop_total: int = 0
    last_seq: int | None = None
    seq_gap_frames: int = 0
    seq_resyncs: int = 0
    restarts: int = 0
    stale: bool = False
    imu_latest: np.ndarray | None = None
    imu_tracker: AttitudeTracker | None = None
    imu_attitude: tuple[float, float, float] | None = None
    last_status_t: float = 0.0

    @property
    def key(self) -> str:
        return self.info.key

    @property
    def label(self) -> str:
        if self.index is None:
            return f"IMU {self.info.mac}"
        if self.info.kind == "imu":
            return f"IMU {self.info.mac}"
        return f"CH{self.index + 1} {self.info.mac}"


@dataclass
class LinkStats:
    running: bool = False
    source: str = ""
    error: str | None = None
    bytes_total: int = 0
    bytes_per_second: float = 0.0
    lines_per_second: float = 0.0
    frames_per_second: float = 0.0
    capacity_bytes_per_second: int = 0
    utilization: float = 0.0
    parse: ParserStats = field(default_factory=ParserStats)
    now: float = 0.0
    replay_progress: float | None = None


class ChannelMap:
    """MAC 后缀 -> 显示通道位置。槽位号会随连接顺序变，MAC 不会，所以按 MAC 定位置。"""

    def __init__(self, initial: dict[str, int] | None = None) -> None:
        self._map = dict(initial or {})

    def assign(self, key: str, max_channels: int) -> int | None:
        if key in self._map:
            return int(self._map[key])
        used = set(self._map.values())
        for i in range(max_channels):
            if i not in used:
                self._map[key] = i
                return i
        # 接进来的 sEMG 比配置的通道数还多时，宁可往后排也不能让数据看不见
        index = max(self._map.values(), default=-1) + 1
        self._map[key] = index
        return index

    def as_dict(self) -> dict[str, int]:
        return dict(self._map)


class Acquisition:
    def __init__(self, source: DataSource, cfg: Config, channel_map: ChannelMap | None = None) -> None:
        self.cfg = cfg
        self.source = source
        self.channel_map = channel_map or ChannelMap(cfg.channel_map)
        self.lock = threading.Lock()
        self.devices: dict[str, DeviceState] = {}
        self.parser = GatewayParser(on_event=self._on_event, on_status=self._on_status)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: str | None = None
        self._recording: Recording | None = None
        self._rate_bytes: deque[tuple[float, int]] = deque(maxlen=256)
        self._rate_lines: deque[tuple[float, int]] = deque(maxlen=256)
        self._rate_frames: deque[tuple[float, int]] = deque(maxlen=256)
        self._frames_total = 0
        self._bytes_total = 0
        self._lines_total = 0
        self._now = 0.0
        self._recent_status: list[dict] = []
        self._capacity = int(getattr(source, "capacity_bytes_per_second", 0) or 0)

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self.source.open()
        self._stop.clear()
        self._error = None
        self._thread = threading.Thread(target=self._loop, name="semg-acquire", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        self.source.close()
        self.stop_recording()

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._stop.is_set()

    # ── 采集线程 ────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        read_bytes = max(512, int(self.cfg.serial.read_bytes))
        while not self._stop.is_set():
            try:
                self.pump(read_bytes)
            except SourceError as exc:
                self._error = str(exc)
                self._stop.set()
                break
            except Exception as exc:  # 兜住 socket/serial 的意外异常，不让线程静默死掉
                self._error = f"{type(exc).__name__}: {exc}"
                self._stop.set()
                break

    def pump(self, max_bytes: int = 8192) -> int:
        """同步读一段并喂给解析器，返回收到的字节数。采集线程和离线文件分析共用这条路。"""
        chunk, t_first, t_last = self.source.read(max_bytes)
        if not chunk:
            return 0
        now = time.monotonic()
        self._bytes_total += len(chunk)
        self._rate_bytes.append((now, len(chunk)))
        with self.lock:
            rec = self._recording
        if rec is not None:
            rec.write_raw(chunk)
        before = self.parser.stats.lines
        self.parser.feed(chunk, t_first, t_last)
        lines = self.parser.stats.lines - before
        self._lines_total += lines
        self._rate_lines.append((now, lines))
        return len(chunk)

    # ── 解析回调 ────────────────────────────────────────────────────────────

    def _on_event(self, ev) -> None:
        if isinstance(ev, SemgFrame):
            values, n_fields, samples = ev.values, SEMG_FIELDS, ev.values.size
        elif isinstance(ev, ImuFrame):
            values, n_fields, samples = ev.samples, IMU_FIELDS, int(ev.samples.shape[0])
        else:  # pragma: no cover
            return
        if samples == 0:
            return

        dev = self._device_state(ev, n_fields)
        times = dev.clock.place(ev.seq, samples, ev.t_arrival)
        dev.ring.append(times, values)
        dev.frames += 1
        dev.samples += samples
        dev.drop_total += ev.drop
        dev.last_status_t = time.monotonic()
        dev.stale = False
        if dev.last_seq is not None:
            delta = (ev.seq - dev.last_seq) & _UINT16_MASK
            if delta > self.cfg.protocol.max_frame_gap:
                dev.restarts += 1          # 节点重启，不是丢帧（与 DeviceClock 同一判定）
            else:
                dev.seq_gap_frames += max(0, delta - 1)
        dev.last_seq = ev.seq

        if isinstance(ev, ImuFrame):
            dev.imu_latest = values[-1]
            dev.imu_tracker.feed(times, values)
            dev.imu_attitude = dev.imu_tracker.attitude
        else:
            t_env, env = dev.env_est.update(times, values)
            if env.size:
                dev.env_ring.append(t_env, env)

        self._frames_total += 1
        self._rate_frames.append((time.monotonic(), 1))
        rec = self._recording
        if rec is not None:
            rec.record(ev, times)

    def _device_state(self, ev, n_fields: int) -> DeviceState:
        key = DeviceInfo.make_key(ev.kind, ev.mac)
        with self.lock:
            dev = self.devices.get(key)
            if dev is not None:
                dev.info.slot = ev.slot
                return dev
            nominal = self.cfg.protocol.frame_period_seconds
            is_imu = ev.kind == "imu"
            clock = DeviceClock(
                nominal_period=nominal,
                pll_gain=self.cfg.protocol.pll_gain,
                resync_threshold=self.cfg.protocol.resync_threshold_seconds,
                max_gap=self.cfg.protocol.max_frame_gap,
            )
            capacity = max(1024, int(self.cfg.buffer_seconds * (
                self.cfg.protocol.imu_nominal_rate if is_imu else self.cfg.protocol.semg_nominal_rate)))
            env_capacity = max(256, int(self.cfg.buffer_seconds * self.cfg.analytics.output_rate_hz))
            index = None if is_imu else self.channel_map.assign(key, self.cfg.semg.channel_count)
            dev = DeviceState(
                info=DeviceInfo(key=key, kind=ev.kind, mac=ev.mac.upper(), slot=ev.slot, online=True),
                index=index,
                clock=clock,
                ring=SeriesRing(capacity, n_fields=n_fields),
                env_ring=SeriesRing(env_capacity),
                env_est=EnvelopeEstimator(
                    cutoff_hz=self.cfg.analytics.envelope_cutoff_hz,
                    output_rate_hz=self.cfg.analytics.output_rate_hz,
                ),
                imu_tracker=(
                    AttitudeTracker(
                        nominal_dt=1.0 / self.cfg.protocol.imu_nominal_rate,
                        cutoff_hz=self.cfg.imu.attitude_cutoff_hz,
                    )
                    if is_imu
                    else None
                ),
            )
            self.devices[key] = dev
            return dev

    def _on_status(self, ev: StatusEvent) -> None:
        entry = {"t": time.time(), "kind": ev.kind, "text": ev.text}
        with self.lock:
            self._recent_status.append(entry)
            del self._recent_status[:-200]

    # ── 录制 ────────────────────────────────────────────────────────────────

    def start_recording(self, rec: Recording) -> None:
        with self.lock:
            self._recording = rec

    def stop_recording(self) -> list:
        with self.lock:
            rec, self._recording = self._recording, None
        if rec is None:
            return []
        with self.lock:
            rec.channel_map = self.channel_map.as_dict()
        return rec.close()

    # ── UI 接口 ─────────────────────────────────────────────────────────────

    def now(self) -> float:
        with self.lock:
            times = [d.ring.latest_time for d in self.devices.values()]
        self._now = max(times) if times else 0.0
        return self._now

    def emg_windows(self, span: float, offset: float = 0.0) -> dict[int, tuple[np.ndarray, np.ndarray]]:
        t_to = self.now() - offset
        t_from = t_to - span
        out: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        with self.lock:
            for dev in self.devices.values():
                if dev.index is None or dev.info.kind != "semg":
                    continue
                out[dev.index] = dev.ring.window(t_from, t_to)
        return out

    def env_windows(self, span: float, offset: float = 0.0) -> dict[int, tuple[np.ndarray, np.ndarray]]:
        t_to = self.now() - offset
        t_from = t_to - span
        out: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        with self.lock:
            for dev in self.devices.values():
                if dev.index is None or dev.info.kind != "semg":
                    continue
                out[dev.index] = dev.env_ring.window(t_from, t_to)
        return out

    def imu_window(self, span: float, offset: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        t_to = self.now() - offset
        with self.lock:
            for dev in self.devices.values():
                if dev.info.kind == "imu":
                    return dev.ring.window(t_to - span, t_to)
        return np.empty(0), np.empty((0, IMU_FIELDS), dtype=np.float32)

    def imu_since(self, t_from: float) -> tuple[np.ndarray, np.ndarray]:
        """取 t_from 之后的 IMU 样本，供步态状态机增量消费（不重复处理已看过的样本）。"""
        with self.lock:
            for dev in self.devices.values():
                if dev.info.kind == "imu":
                    return dev.ring.window(t_from, None)
        return np.empty(0), np.empty((0, IMU_FIELDS), dtype=np.float32)

    def imu_latest(self) -> np.ndarray | None:
        with self.lock:
            for dev in self.devices.values():
                if dev.info.kind == "imu" and dev.imu_latest is not None:
                    return np.array(dev.imu_latest, dtype=np.float32)
        return None

    def imu_attitude(self) -> tuple[float, float, float] | None:
        """最新 roll/pitch/yaw（deg）。元组不可变，可跨线程直接交给 UI。"""
        with self.lock:
            for dev in self.devices.values():
                if dev.info.kind == "imu" and dev.imu_attitude is not None:
                    return dev.imu_attitude
        return None

    def metrics(self, span: float, offset: float = 0.0) -> dict[int, object]:
        emg = self.emg_windows(span, offset)
        env = self.env_windows(span, offset)
        out = {}
        for index, (t, v) in emg.items():
            _te, e = env.get(index, (np.empty(0), np.empty(0)))
            out[index] = channel_metrics(t, v, e)
        return out

    def raw_window(self, index: int, span: float, offset: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        t_to = self.now() - offset
        with self.lock:
            for dev in self.devices.values():
                if dev.index == index and dev.info.kind == "semg":
                    return dev.ring.window(t_to - span, t_to)
        return np.empty(0), np.empty(0, dtype=np.float32)

    def emg_abs(self, index: int, t_from: float, t_to: float) -> tuple[np.ndarray, np.ndarray]:
        """按绝对设备时标取原始 sEMG 窗。

        步态锁定分析必须用绝对窗：span/offset 形式内部会重取 now()，而 now() 在分析
        过程中还在前进，窗口会跟着漂几十毫秒——正好是末端摆动窗的量级。
        """
        with self.lock:
            for dev in self.devices.values():
                if dev.index == index and dev.info.kind == "semg":
                    return dev.ring.window(t_from, t_to)
        return np.empty(0), np.empty(0, dtype=np.float32)

    def env_abs(self, index: int, t_from: float, t_to: float) -> tuple[np.ndarray, np.ndarray]:
        with self.lock:
            for dev in self.devices.values():
                if dev.index == index and dev.info.kind == "semg":
                    return dev.env_ring.window(t_from, t_to)
        return np.empty(0), np.empty(0, dtype=np.float32)

    def device_rows(self) -> list[dict]:
        t_now_mono = time.monotonic()
        rows: list[dict] = []
        with self.lock:
            for dev in self.devices.values():
                ck = dev.clock.stats
                rate = dev.clock.sample_rate(dev.samples / max(dev.frames, 1)) if dev.frames else 0.0
                age = t_now_mono - dev.last_status_t if dev.last_status_t else None
                rows.append(
                    {
                        "key": dev.key,
                        "label": dev.label,
                        "kind": dev.info.kind,
                        "mac": dev.info.mac,
                        "full_mac": dev.info.full_mac,
                        "slot": dev.info.slot,
                        "index": dev.index,
                        "online": dev.info.online and (age is None or age < 2.0),
                        "age": age,
                        "frames": dev.frames,
                        "samples": dev.samples,
                        "last_seq": dev.last_seq,
                        "drop_total": dev.drop_total,
                        "seq_gaps": dev.seq_gap_frames,
                        "restarts": dev.restarts,
                        "resyncs": ck.resyncs + ck.jumps,
                        "forward_skips": ck.forward_skips,
                        "lost_frames": ck.lost_frames,
                        "rate": rate,
                        "measured_period": dev.clock.frame_period,
                        "buffered": len(dev.ring),
                    }
                )
        rows.sort(key=lambda r: (r["kind"] != "semg", r["index"] if r["index"] is not None else 99))
        return rows

    def stats(self) -> LinkStats:
        now = time.monotonic()
        st = LinkStats(
            running=self.running,
            source=self.source.describe(),
            error=self._error,
            bytes_total=self._bytes_total,
            lines_per_second=_rate(self._rate_lines, now, key=1),
            frames_per_second=_rate(self._rate_frames, now, key=1),
            capacity_bytes_per_second=self._capacity,
            now=self._now,
            parse=self.parser.stats,
        )
        st.bytes_per_second = _rate(self._rate_bytes, now, key=1)
        st.utilization = (
            st.bytes_per_second / st.capacity_bytes_per_second if st.capacity_bytes_per_second else 0.0
        )
        prog = getattr(self.source, "progress", None)
        st.replay_progress = float(prog) if isinstance(prog, (int, float)) else None
        return st

    def recent_status(self, count: int = 12) -> list[dict]:
        with self.lock:
            return list(self._recent_status[-count:])

    def status_log(self) -> list[dict]:
        return self.recent_status(200)


def _rate(samples: deque, now: float, key: int = 1) -> float:
    if not samples:
        return 0.0
    horizon = 2.0
    window = [(t, n) for t, n in samples if now - t <= horizon]
    if len(window) < 2:
        return 0.0
    dt = window[-1][0] - window[0][0]
    total = sum(n for _, n in window[1:])
    if dt <= 0:
        return 0.0
    return total / dt
