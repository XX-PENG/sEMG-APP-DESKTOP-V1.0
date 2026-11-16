"""蓝牙网关串口文本协议解析。

网关侧（sEMG-GATEWAY-ESP32-V1.0/main/app_task_com.c）用 printf 输出，无长度字段、
无校验，行即帧边界，因此本模块的职责是：

1. 字节流 -> 行（跨 read 边界拼接，容忍 \\r）
2. 行 -> 结构化事件：sEMG 单行帧 / IMU 头部行 + N 个数据行 / 链路状态行
3. 在没有校验位的前提下，用"字段数、取值范围、cnt 一致性"做可检测性统计

协议事实（已对照网关源码核实）：

    sEMG 帧（1 行）
        3/9 semg A4:CF:FF seq=1234 cnt=40 drop=0 12 -3 145 ...
    IMU 帧（1 + cnt 行）
        9/9 imu 12:34:56 seq=77 cnt=10 drop=2
        0.012 -0.004 0.998 1.200 -0.300 0.000 123 -45 68
        ...
    状态行（可能带 ESP_LOG 前缀与 ANSI 颜色）
        I (1234) GW: Sensor connected: slot=2 conn=3 type=sEMG mac=AA:BB:CC:DD:EE:FF
        I (1234) GW: Sensor disconnected: slot=2 conn=3
        W (1234) GW: print queue overflow(total)=17
        ping_rtt conn=3 us=45120

数据行里的 MAC 只有后 3 字节，等于 connected 行完整 MAC 的最后 3 段，两者可直接对照。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

HEADER_RE = re.compile(
    r"^(?P<slot>\d+)/(?P<active>\d+)[ \t]+(?P<kind>semg|imu)[ \t]+"
    r"(?P<mac>(?:[0-9A-Fa-f]{2}:){2}[0-9A-Fa-f]{2})"
    r"[ \t]+seq=(?P<seq>\d+)[ \t]+cnt=(?P<cnt>\d+)[ \t]+drop=(?P<drop>\d+)(?P<rest>.*)$"
)
LOG_PREFIX_RE = re.compile(r"^[EWIDV][ \t]+\(\s*\d+\s*\)[ \t]+[^\s:]{1,32}:[ \t]*")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CONNECTED_RE = re.compile(
    r"^Sensor connected:[ \t]*slot=(?P<slot>-?\d+)[ \t]+conn=(?P<conn>\d+)"
    r"[ \t]+type=(?P<type>\w+)[ \t]+mac=(?P<fullmac>(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})"
)
DISCONNECTED_RE = re.compile(r"^Sensor disconnected:[ \t]*slot=(?P<slot>-?\d+)[ \t]+conn=(?P<conn>\d+)")
OVERFLOW_RE = re.compile(r"^print queue overflow\(total\)=(?P<n>\d+)")
PING_RE = re.compile(r"^ping_rtt conn=(?P<conn>-?\d+)[ \t]+us=(?P<us>-?\d+)")

IMU_FIELD_COUNT = 9
MAX_LINE_BYTES = 2048  # 网关 FRAME_LINE_MAX=1024，留一倍余量；超出即判定为失步
_INT16_LO, _INT16_HI = -32768, 32767
_ACCEL_LIMIT_G, _GYRO_LIMIT_DPS = 50.0, 4000.0

EventCallback = Callable[["FrameEvent"], None]
StatusCallback = Callable[["StatusEvent"], None]


@dataclass
class SemgFrame:
    kind: str
    mac: str
    slot: int
    active: int
    seq: int
    cnt: int
    drop: int
    values: np.ndarray  # float32, int16 原始 ADC 定点值
    t_arrival: float


@dataclass
class ImuFrame:
    kind: str
    mac: str
    slot: int
    active: int
    seq: int
    cnt: int
    drop: int
    samples: np.ndarray  # float32 (n, 9) -> ax ay az gx gy gz mx my mz，前 6 项已换算为 g / dps
    t_arrival: float
    truncated: bool = False


FrameEvent = SemgFrame | ImuFrame


@dataclass
class StatusEvent:
    kind: str  # connected / disconnected / overflow / ping_rtt / garbage
    text: str
    slot: int | None = None
    conn: int | None = None
    device_type: str | None = None
    full_mac: str | None = None
    overflow: int | None = None
    rtt_us: int | None = None


@dataclass
class DeviceInfo:
    key: str
    kind: str
    mac: str
    slot: int | None = None
    conn: int | None = None
    full_mac: str | None = None
    online: bool = False
    frames: int = 0
    samples: int = 0
    drop_total: int = 0
    last_seq: int | None = None
    last_t: float | None = None

    @staticmethod
    def make_key(kind: str, mac: str) -> str:
        return f"{kind}@{mac.replace(':', '').lower()}"


@dataclass
class ParserStats:
    bytes_fed: int = 0
    lines: int = 0
    semg_frames: int = 0
    imu_frames: int = 0
    imu_sample_lines: int = 0
    imu_block_truncated: int = 0
    imu_out_of_range: int = 0
    semg_short: int = 0
    semg_overflow_cnt: int = 0
    semg_saturated: int = 0
    bad_token_lines: int = 0
    garbage_lines: int = 0
    oversize_lines: int = 0
    status_lines: int = 0

    @property
    def parse_errors(self) -> int:
        return (
            self.imu_block_truncated
            + self.imu_out_of_range
            + self.semg_short
            + self.bad_token_lines
            + self.garbage_lines
            + self.oversize_lines
        )


class GatewayParser:
    """增量式文本帧解析器。

    feed(data, t_first, t_last) 里一行样本的到达时刻按字节偏移在 [t_first, t_last]
    内线性插值：一次 read 攒满缓冲区说明这段字节确实是连续到达的，直接取 t_last 会把
    整段的时标压到同一个点上。
    """

    def __init__(self, on_event: EventCallback, on_status: StatusCallback | None = None) -> None:
        self.on_event = on_event
        self.on_status = on_status
        self.stats = ParserStats()
        self.devices: dict[str, DeviceInfo] = {}
        self._buf = bytearray()
        self._pending: tuple[str, str, int, int, int, int, int, float] | None = None
        self._pending_rows: list[list[float]] = []

    # ── 公共入口 ────────────────────────────────────────────────────────────

    def feed(self, data: bytes, t_first: float, t_last: float | None = None) -> None:
        if not data:
            return
        self.stats.bytes_fed += len(data)
        self._buf += data
        t_end = t_last if t_last is not None and t_last >= t_first else t_first
        start = 0
        while True:
            nl = self._buf.find(b"\n", start)
            if nl < 0:
                break
            chunk = bytes(self._buf[start : nl + 1])
            del self._buf[: nl + 1]
            start = 0
            frac = 1.0 if not self._buf else len(chunk) / (len(chunk) + len(self._buf))
            t = t_first + (t_end - t_first) * min(1.0, frac)
            self._handle_line(chunk, t)
        if len(self._buf) > MAX_LINE_BYTES:
            self.stats.oversize_lines += 1
            self._buf.clear()
            self._reset_pending()

    def close(self) -> None:
        """流结束时丢弃半行与未完成 IMU 块（回放文件末尾用）。"""
        self._buf.clear()
        self._reset_pending()

    # ── 行处理 ──────────────────────────────────────────────────────────────

    def _reset_pending(self) -> None:
        # 重新绑定而不是 clear()：_flush_pending 会先取出列表引用再复位，就地清空会把它一起抹掉
        self._pending = None
        self._pending_rows = []

    def _handle_line(self, raw: bytes, t: float) -> None:
        self.stats.lines += 1
        text = ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).strip("\r\n \t")
        if not text:
            return
        text = LOG_PREFIX_RE.sub("", text)

        m = HEADER_RE.match(text)
        if m:
            self._flush_pending(truncated=True, t=t)
            self._handle_header(m, t)
            return

        if self._pending is not None:
            handled = self._maybe_imu_sample(text, t)
            if handled:
                return

        self._handle_status_line(text, t)

    def _handle_header(self, m: re.Match[str], t: float) -> None:
        kind = m.group("kind")
        mac = m.group("mac").upper()
        slot = int(m.group("slot"))
        active = int(m.group("active"))
        seq = int(m.group("seq"))
        cnt = int(m.group("cnt"))
        drop = int(m.group("drop"))
        dev = self._device(kind, mac, slot, active, t)

        if kind == "semg":
            tokens = m.group("rest").split()
            values = np.empty(len(tokens), dtype=np.float32)
            for i, tok in enumerate(tokens):
                try:
                    values[i] = int(tok, 10)
                except ValueError:
                    self.stats.bad_token_lines += 1
                    values = values[:i]
                    break
            if values.size and (values.max() > _INT16_HI or values.min() < _INT16_LO):
                self.stats.bad_token_lines += 1
                values = np.clip(values, _INT16_LO, _INT16_HI)
            sent = int(values.size and np.count_nonzero((values == _INT16_HI) | (values == _INT16_LO)))
            self.stats.semg_saturated += sent
            if values.size < cnt:
                self.stats.semg_short += 1
            elif values.size > cnt:
                self.stats.semg_overflow_cnt += 1
            self.stats.semg_frames += 1
            dev.frames += 1
            dev.samples += int(values.size)
            dev.drop_total += drop
            dev.last_seq = seq
            dev.last_t = t
            self.on_event(
                SemgFrame(
                    kind="semg", mac=mac, slot=slot, active=active, seq=seq,
                    cnt=cnt, drop=drop, values=values, t_arrival=t,
                )
            )
        else:
            self._pending = (kind, mac, slot, active, seq, cnt, drop, t)
            self._pending_rows = []

    def _maybe_imu_sample(self, text: str, t: float) -> bool:
        parts = text.split()
        if len(parts) != IMU_FIELD_COUNT:
            return False
        try:
            row = [float(p) for p in parts]
        except ValueError:
            self.stats.bad_token_lines += 1
            return False
        if any(abs(v) > _ACCEL_LIMIT_G for v in row[0:3]) or any(abs(v) > _GYRO_LIMIT_DPS for v in row[3:6]):
            self.stats.imu_out_of_range += 1
            return True  # 已识别为数据行，只是丢弃该样本
        self.stats.imu_sample_lines += 1
        self._pending_rows.append(row)
        _, _, _, _, _, cnt, _, _ = self._pending
        if len(self._pending_rows) >= cnt:
            self._flush_pending(truncated=False, t=t)
        return True

    def _flush_pending(self, truncated: bool, t: float) -> None:
        if self._pending is None:
            return
        kind, mac, slot, active, seq, cnt, drop, t_frame = self._pending
        rows = self._pending_rows
        self._reset_pending()
        if not rows:
            if truncated:
                self.stats.imu_block_truncated += 1
            return
        if truncated:
            self.stats.imu_block_truncated += 1
        samples = np.asarray(rows, dtype=np.float32)
        if samples.shape[0] < cnt:
            truncated = True
        dev = self._device(kind, mac, slot, active, t_frame)
        dev.frames += 1
        dev.samples += int(samples.shape[0])
        dev.drop_total += drop
        dev.last_seq = seq
        dev.last_t = t_frame
        self.stats.imu_frames += 1
        self.on_event(
            ImuFrame(
                kind="imu", mac=mac, slot=slot, active=active, seq=seq, cnt=cnt,
                drop=drop, samples=samples, t_arrival=t_frame, truncated=truncated,
            )
        )

    def _handle_status_line(self, text: str, t: float) -> None:
        m = CONNECTED_RE.match(text)
        if m:
            kind = "imu" if m.group("type").lower() == "imu" else "semg"
            full = m.group("fullmac").upper()
            suffix = ":".join(full.split(":")[3:])
            dev = self._device(kind, suffix, int(m.group("slot")) + 1, 0, t)
            dev.online = True
            dev.conn = int(m.group("conn"))
            dev.full_mac = full
            dev.slot = int(m.group("slot")) + 1
            self.stats.status_lines += 1
            self._emit_status(StatusEvent("connected", text, slot=int(m.group("slot")),
                                          conn=dev.conn, device_type=kind, full_mac=full))
            return
        m = DISCONNECTED_RE.match(text)
        if m:
            slot = int(m.group("slot"))
            for dev in self.devices.values():
                if dev.conn == int(m.group("conn")) or (dev.slot == slot + 1 and dev.conn is None):
                    dev.online = False
            self.stats.status_lines += 1
            self._emit_status(StatusEvent("disconnected", text, slot=slot, conn=int(m.group("conn"))))
            return
        m = OVERFLOW_RE.match(text)
        if m:
            self.stats.status_lines += 1
            self._emit_status(StatusEvent("overflow", text, overflow=int(m.group("n"))))
            return
        m = PING_RE.match(text)
        if m:
            self.stats.status_lines += 1
            self._emit_status(StatusEvent("ping_rtt", text, conn=int(m.group("conn")), rtt_us=int(m.group("us"))))
            return
        self.stats.garbage_lines += 1
        self._emit_status(StatusEvent("garbage", text))

    def _emit_status(self, ev: StatusEvent) -> None:
        if self.on_status is not None:
            self.on_status(ev)

    def _device(self, kind: str, mac: str, slot: int, active: int, t: float) -> DeviceInfo:
        key = DeviceInfo.make_key(kind, mac)
        dev = self.devices.get(key)
        if dev is None:
            dev = DeviceInfo(key=key, kind=kind, mac=mac.upper(), slot=slot, online=True)
            self.devices[key] = dev
        dev.slot = slot
        dev.last_t = t
        if dev.conn is None:
            dev.online = True
        return dev
