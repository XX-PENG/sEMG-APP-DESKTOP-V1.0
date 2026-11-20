"""EMG / IMU 节点应用固件的 BLE OTA。

两阶段，与 sEMG-OTA-ESP32-V1.0 的双固件设计一一对应：

1. 触发  —— 向应用固件的 GATT 特征 0xFFF1 写 4 字节魔术字 "OTA!"。节点把当前运行槽记进
   NVS（`ota_ctrl`/`last_slot`）后重启到 factory 分区里的共享接收固件。
2. 传输  —— 连上接收固件，命令特征 0x8022 发 `01 00 <固件长度:u32le>`，数据按
   `<扇区:u16le><包号:u8> + 净载` 写 0x8020，只有扇区末包回 20 字节 ACK；末包净载尾部再补
   2 字节 trailer（设备只剥离不校验）。全部写完发 `02 00`，设备重启进新槽并挂上旧槽回滚。

与工厂脚本 tools/ble_ota_send.py 的协议完全一致，区别是这里嵌进上位机：进度回调、可取消、
按 BLE 地址锁定节点。地址锁定是必须的——接收固件对所有板子广播同一个名字
`nimble-ble-ota`，光比名字分不清是哪块板。好消息是网关 connected 行里的
`mac=` 就是节点完整 BLE 地址，接收固件又沿用同一份 eFuse 公版地址，所以两个阶段都能对上。

两条硬性约束来自固件本身，界面必须讲清楚：

* 应用固件只在**没有连接**时广播（bleprph 只在断开/连接失败后重新 advertise）。节点被网关连着的时候
  上位机搜不到它，也就写不进触发字——升级前先让节点单独上电或把网关断开。
* 接收固件阶段不受这条约束影响：网关只连 `EMG-*` / `IMU-*`（兼容旧 `sEMG-*`），不会去抢 `nimble-ble-ota`。
"""

from __future__ import annotations

import asyncio
import importlib.util
import struct
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path


def uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


TRIGGER_CHAR = uuid16(0xFFF1)  # 应用固件里的 OTA 触发特征（服务 0xFFF0）
VERSION_CHAR = uuid16(0xFFF2)  # 应用固件里的版本特征：version[32] + elf_sha256[32]
RECV_FW_CHAR = uuid16(0x8020)  # 接收固件：固件数据
COMMAND_CHAR = uuid16(0x8022)  # 接收固件：开始 / 结束命令

TRIGGER_MAGIC = b"OTA!"
CMD_START = b"\x01\x00"
CMD_STOP = b"\x02\x00"
STATUS_OK = 0x00
STATUS_RETRY = 0x02

SECTOR_SIZE = 4096
PACKET_HEADER_BYTES = 3        # <sector:u16le><pkt:u8>
PACKET_TRAILER = b"\x00\x00"   # 只跟在扇区末包后面
ATT_OVERHEAD = 3               # write-command 的 opcode + handle
PACKET_INDEX_LIMIT = 0xFF      # 包号只有 1 字节，且 0xFF 被末包占用，所以每扇区最多 255 包
MIN_PACKET_PAYLOAD_BYTES = -(-SECTOR_SIZE // PACKET_INDEX_LIMIT)  # 17 B，再小就要求 >255 包
ACK_LEN = 20
ACK_STATUS_AT = 2
ACK_CRC_AT = 18

APP_NAME_PREFIX = {"semg": "EMG", "imu": "IMU"}
# 识别时兼容旧前缀：sEMG-（含更旧的 sEMG-ESP32-），升级过渡期新旧固件都能认
APP_NAME_ALIASES = {"semg": ("EMG", "sEMG"), "imu": ("IMU",)}
RECEIVER_NAME_PREFIX = "nimble-ble-ota"
OTA_SLOT_BYTES = 0x200_000     # partitions.csv 里 ota_0 / ota_1 各 2 MB
ESP_IMAGE_MAGIC = 0xE9


class OtaError(RuntimeError):
    pass


class OtaCancelled(OtaError):
    pass


# ── 纯协议部分：不碰 BLE，可单测 ─────────────────────────────────────────────


def crc16_ccitt(data: bytes) -> int:
    """多项式 0x1021、初值 0、无反射，与设备侧 ACK 校验一致。"""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


def ack_crc_ok(ack: bytes) -> bool:
    if len(ack) < ACK_LEN:
        return False
    return crc16_ccitt(ack[:ACK_CRC_AT]) == (ack[ACK_CRC_AT] | (ack[ACK_CRC_AT + 1] << 8))


def ack_status(ack: bytes) -> int | None:
    return ack[ACK_STATUS_AT] if len(ack) > ACK_STATUS_AT else None


def make_ack(status: int, header: bytes = b"\x01\x00") -> bytes:
    """拼一条合法 ACK，给单测与模拟用。"""
    body = header[:2] + bytes([status]) + bytes(15)
    crc = crc16_ccitt(body[:ACK_CRC_AT])
    return body + bytes([crc & 0xFF, crc >> 8])


def packet_payload_bytes(mtu: int) -> int:
    """单包能装的固件字节数。给末包预留 2 字节 trailer，扇区内包长因此保持一致。"""
    return max(1, mtu - ATT_OVERHEAD - PACKET_HEADER_BYTES - len(PACKET_TRAILER))


def min_mtu() -> int:
    """还能谈得下来的最小 MTU：再小就要求每扇区 >255 包，包号放不下。"""
    return MIN_PACKET_PAYLOAD_BYTES + ATT_OVERHEAD + PACKET_HEADER_BYTES + len(PACKET_TRAILER)


def split_sectors(fw: bytes) -> list[bytes]:
    return [fw[i:i + SECTOR_SIZE] for i in range(0, len(fw), SECTOR_SIZE)]


def sector_packets(sector: int, data: bytes, payload_bytes: int) -> Iterator[tuple[bytes, bool]]:
    """产出写进 0x8020 的完整字节与"是否扇区末包"。扇区号按 u16 截断，末包包号固定 0xFF。"""
    pos, pkt = 0, 0
    while pos < len(data):
        piece = data[pos:pos + payload_bytes]
        pos += len(piece)
        is_last = pos >= len(data)
        if not is_last and pkt >= PACKET_INDEX_LIMIT:
            raise OtaError(f"扇区 {sector} 需要超过 {PACKET_INDEX_LIMIT} 包：包号只有 1 字节，装不下")
        header = struct.pack("<HB", sector & 0xFFFF, 0xFF if is_last else pkt)
        yield header + piece + (PACKET_TRAILER if is_last else b""), is_last
        pkt += 1


def load_firmware(path: str | Path) -> bytes:
    p = Path(path)
    if not p.is_file():
        raise OtaError(f"固件文件不存在：{p}")
    data = p.read_bytes()
    if not data:
        raise OtaError(f"固件文件是空的：{p}")
    if len(data) > OTA_SLOT_BYTES:
        raise OtaError(f"固件 {len(data):,} B 超过 OTA 分区上限 {OTA_SLOT_BYTES:,} B")
    return data


def firmware_warnings(data: bytes) -> list[str]:
    if data[0] != ESP_IMAGE_MAGIC:
        return [f"首字节 0x{data[0]:02X}，不是 ESP 应用镜像（应为 0xE9），确认它是 idf.py build 的产物"]
    return []


# ── 镜像描述符与版本校验 ─────────────────────────────────────────────────────

APP_DESC_OFFSET = 0x20          # esp_app_desc_t 在 .bin 里的固定偏移
APP_DESC_MAGIC = 0xABCD5432
APP_DESC_SIZE = 256


@dataclass(frozen=True)
class AppDesc:
    version: str
    project: str
    built: str
    sha256: str                 # app_elf_sha256 的 hex，精确到这一份二进制


def parse_app_desc(fw: bytes) -> AppDesc | None:
    """从 .bin 读 esp_app_desc_t（布局见 IDF esp_app_desc.h）。不是 ESP 镜像就返回 None。"""
    if len(fw) < APP_DESC_OFFSET + APP_DESC_SIZE:
        return None
    d = fw[APP_DESC_OFFSET:APP_DESC_OFFSET + APP_DESC_SIZE]
    if struct.unpack_from("<I", d, 0)[0] != APP_DESC_MAGIC:
        return None

    def text(off: int, n: int) -> str:
        return d[off:off + n].split(b"\x00")[0].decode("utf-8", "replace")

    return AppDesc(version=text(16, 32), project=text(48, 32),
                   built=f"{text(96, 16)} {text(80, 16)}", sha256=d[144:176].hex())


def parse_version_payload(raw: bytes) -> tuple[str, str] | None:
    """应用固件 0xFFF2 的读回值：version[32] + elf_sha256[32]。"""
    if len(raw) < 64:
        return None
    return raw[:32].split(b"\x00")[0].decode("utf-8", "replace"), raw[32:64].hex()


# ── 节点身份 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Advert:
    """一条广播：名字 + 地址。"""

    name: str
    address: str

    @property
    def suffix(self) -> str:
        parts = self.address.split(":")
        return ":".join(parts[3:]) if len(parts) == 6 else ""

    @property
    def kind(self) -> str | None:
        upper = self.name.upper()
        for kind, prefixes in APP_NAME_ALIASES.items():
            if any(upper.startswith(p.upper()) for p in prefixes):
                return kind
        return None

    def is_receiver(self, prefix: str = RECEIVER_NAME_PREFIX) -> bool:
        return self.name.upper().startswith(prefix.upper())


@dataclass
class OtaNode:
    """一个可升级对象。address 未知时只能靠广播名认，多台同型号在 OTA 模式时会分不清。"""

    kind: str                     # "semg" | "imu" | "" （只认到接收固件时类型未知）
    suffix: str = ""              # MAC 尾段，如 "A4:CF:01"，供旧固件短广播名兜底匹配
    address: str | None = None    # 完整 6 字节，如 "7C:DF:A1:A4:CF:01"
    label: str = ""               # 界面显示名："CH3" / "IMU"
    source: str = ""              # "网关" / "扫描" / "网关+扫描"
    uid: str = ""                 # 界面行标识，空则按 kind+suffix 生成

    @property
    def key(self) -> str:
        if self.uid:
            return self.uid
        raw = (self.suffix or self.address or "").replace(":", "").lower()
        return f"{self.kind}@{raw}"

    @property
    def app_name(self) -> str:
        prefix = APP_NAME_PREFIX.get(self.kind, "")
        if not prefix:
            return ""
        raw = (self.address or self.suffix).replace(":", "")
        return f"{prefix}-{raw}" if raw else prefix

    @property
    def identity(self) -> str:
        return self.address or self.app_name or "身份未知"


def node_from_row(row: dict) -> OtaNode:
    """由 pipeline.device_rows() 的一行构造。mac 为完整 6 字节，suffix 供旧固件广播名兜底匹配。"""
    mac = row["mac"]
    return OtaNode(kind=row["kind"], suffix=":".join(mac.split(":")[3:]),
                   address=row.get("full_mac") or mac,
                   label=row.get("label", ""), source="网关")


def node_from_advert(adv: Advert) -> OtaNode | None:
    kind = adv.kind
    if kind is None:
        return None
    return OtaNode(kind=kind, suffix=adv.suffix or _suffix_from_name(adv.name, kind),
                   address=adv.address, label="", source="扫描")


def _suffix_from_name(name: str, kind: str) -> str:
    """从广播名还原 MAC 尾段，供地址未知时兜底匹配。

    新名字带全 6 字节 MAC（EMG-DCB4D9281246），旧名字 sEMG-ESP32-xxxxxx 只带尾段，都兼容。
    """
    upper = name.upper()
    tail = ""
    for p in APP_NAME_ALIASES.get(kind, ()):
        if upper.startswith(p.upper()):
            tail = name[len(p):].lstrip("-_")
            break
    if tail.upper().startswith("ESP32-"):
        tail = tail[len("ESP32-"):]
    digits = "".join(c for c in tail if c.isalnum())
    if len(digits) == 12:
        digits = digits[6:]
    if len(digits) == 6:
        return ":".join(digits[i:i + 2].upper() for i in (0, 2, 4))
    return ""


def match_app(adverts: Iterable[Advert], node: OtaNode, prefix: str = RECEIVER_NAME_PREFIX) -> Advert | None:
    """认应用固件：地址优先，广播名兜底。

    必须先把接收固件排除掉 —— 接收固件沿用同一份 eFuse 公网地址，只比地址会把"已经在 OTA 模式"
    的那台当成应用固件，然后往 0xFFF1 上写触发字（接收固件根本没这个特征）。
    """
    advs = [a for a in adverts if not a.is_receiver(prefix)]
    if node.address:
        for a in advs:
            if a.address.upper() == node.address.upper():
                return a
    # 名字兜底：比"名里的 MAC 尾段"，新旧名字格式（全 MAC / 旧短名）都认得
    if node.suffix:
        for a in advs:
            if a.kind == node.kind and _suffix_from_name(a.name, node.kind) == node.suffix:
                return a
    return None


def match_receiver(adverts: Iterable[Advert], node: OtaNode, prefix: str) -> tuple[Advert | None, int]:
    """认接收固件，同时返回在场接收固件台数。

    地址能对上就锁定；没地址时要求全场只有一台在 OTA 模式，否则不猜——猜错会把 A 板的固件
    灌进 B 板，而 B 板此时没有可用的旧镜像兜底。
    """
    advs = list(adverts)
    hits = [a for a in advs if a.is_receiver(prefix)]
    if node.address:
        for a in hits:
            if a.address.upper() == node.address.upper():
                return a, len(hits)
        return None, len(hits)
    return (hits[0] if len(hits) == 1 else None), len(hits)


# ── 选项与回调 ───────────────────────────────────────────────────────────────


@dataclass
class OtaOptions:
    scan_timeout: float = 8.0        # 单轮 BLE 扫描时长（扫到目标会提前收工，这是上限）
    trigger_wait: float = 60.0       # 阶段1 最多等多久：节点被网关占着时不广播，留出搬网关的时间
    reboot_wait: float = 1.5         # 触发后的固定等待，剩下的交给阶段2 边扫边等
    settle_seconds: float = 1.5      # 连上接收固件后的静置：见 _send_firmware 里的说明
    receiver_deadline: float = 30.0  # 触发后多久内必须搜到接收固件
    ack_timeout: float = 15.0
    connect_timeout: float = 12.0
    verify_deadline: float = 30.0    # 传完后等节点回到应用固件并读回版本的上限
    receiver_prefix: str = RECEIVER_NAME_PREFIX

    @staticmethod
    def from_config(cfg) -> "OtaOptions":
        ota = cfg.ota
        return OtaOptions(scan_timeout=ota.scan_timeout_seconds, trigger_wait=ota.trigger_wait_seconds,
                          reboot_wait=ota.reboot_wait_seconds, settle_seconds=ota.settle_seconds,
                          ack_timeout=ota.ack_timeout_seconds, connect_timeout=ota.connect_timeout_seconds,
                          receiver_prefix=ota.receiver_name_prefix)


@dataclass
class OtaReporter:
    """升级过程的出口。回调都在工作线程里被调用，界面侧用 Qt 信号转回主线程。"""

    on_log: Callable[[str], None] = lambda msg: None
    on_progress: Callable[[int, int], None] = lambda done, total: None
    on_state: Callable[[str, str], None] = lambda key, text: None
    cancel: threading.Event = field(default_factory=threading.Event)

    def log(self, msg: str) -> None:
        self.on_log(msg)

    def progress(self, done: int, total: int) -> None:
        self.on_progress(done, total)

    def state(self, key: str, text: str) -> None:
        self.on_state(key, text)

    def check(self) -> None:
        if self.cancel.is_set():
            raise OtaCancelled("已取消")


# ── bleak 适配层 ─────────────────────────────────────────────────────────────


def bleak_available() -> bool:
    try:
        return importlib.util.find_spec("bleak") is not None
    except (ImportError, ValueError):  # pragma: no cover - 包装损坏时 find_spec 自己抛
        return False


def _bleak():
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:  # pragma: no cover - 只有真机路径会走到
        raise OtaError("缺少 bleak 依赖（pip install bleak），且本机要有可用蓝牙适配器") from exc
    return BleakClient, BleakScanner


async def _discover(timeout: float, stop_when: Callable[[Advert], bool] | None = None) -> list[Advert]:
    """扫一轮广播。

    `stop_when` 命中就立刻收工 —— 原来每次都把 scan_timeout 等满，两台板子的两轮扫描光等待
    就吃掉十几秒，而目标往往第一秒就出现在空气里了。
    """
    _, scanner_cls = _bleak()
    found: dict[str, Advert] = {}
    done = asyncio.Event()

    def on_adv(dev, adv) -> None:
        # 名字优先取广播帧里的：bleak 自己就说了第一帧可能还没有扫描响应，而 dev.name 可能是
        # Windows 按地址缓存的旧名字 —— 双固件同一个地址，缓存名会把"还在跑应用固件"骗认成接收固件
        live = (adv.local_name if adv is not None else "") or ""
        addr = dev.address.upper()
        prev = found.get(addr)
        name = live or (prev.name if prev else "") or (dev.name or "")
        item = Advert(name=name, address=addr)
        found[addr] = item
        if stop_when is not None and stop_when(item):
            done.set()

    async with scanner_cls(on_adv):
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            pass
    return sorted(found.values(), key=lambda a: (a.name, a.address))


def scan_target(node: OtaNode, prefix: str, want_app: bool) -> Callable[[Advert], bool] | None:
    """能否提前结束扫描的判据。

    地址未知的接收固件不能提前停：必须扫满一轮数清楚场上有几台，只有一台才敢认。
    """
    if want_app:
        return lambda a: match_app((a,), node, prefix) is not None
    if not node.address:
        return None
    return lambda a: match_receiver((a,), node, prefix)[0] is not None


def scan_adverts(timeout: float = 8.0) -> list[Advert]:
    """给界面用的一次性扫描。阻塞，必须放在工作线程里调。"""
    return asyncio.run(_discover(timeout))


async def _sleep(seconds: float, rep: OtaReporter) -> None:
    end = time.monotonic() + seconds
    while True:
        rep.check()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.25, remaining))


class _AckWaiter:
    """按特征分队的 ACK 队列，过滤掉 CRC 不符的通知。"""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[bytes]] = {}

    def queue(self, char: str) -> asyncio.Queue[bytes]:
        if char not in self._queues:
            self._queues[char] = asyncio.Queue()
        return self._queues[char]

    def handler(self, char: str):
        def _on_notify(_sender, data: bytearray) -> None:
            self.queue(char).put_nowait(bytes(data))
        return _on_notify

    async def wait(self, char: str, expect: int | None, timeout: float, rep: OtaReporter,
                   label: str) -> bytes:
        q = self.queue(char)
        deadline = time.monotonic() + timeout
        while True:
            rep.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OtaError(f"{label} 超时：{timeout:.0f}s 内没等到有效 ACK")
            try:
                ack = await asyncio.wait_for(q.get(), timeout=min(0.5, remaining))
            except asyncio.TimeoutError:
                continue
            if not ack_crc_ok(ack):
                rep.log(f"{label} CRC 不符，丢弃：{ack.hex()}")
                continue
            status = ack_status(ack)
            if status == STATUS_RETRY or expect is None or status == expect:
                return ack


def scan_summary(node: OtaNode, advs: list[Advert], prefix: str) -> str:
    """把"没搜到"拆成能据此行动的判断：适配器没工作、地址在但名字不对、还是节点根本没广播。"""
    if not advs:
        return "本轮 BLE 扫描一个广播都没收到 —— 先确认本机蓝牙适配器已开启、驱动正常"
    same_addr = [a for a in advs if node.address and a.address.upper() == node.address.upper()]
    if same_addr:
        return (f"{node.address} 在广播，但名字是 {same_addr[0].name!r}、不是 "
                f"{node.app_name!r} —— 固件里的设备名和这里对不上")
    peers = [a for a in advs if a.kind == node.kind]
    receivers = [a for a in advs if a.is_receiver(prefix)]
    parts = [f"扫描到 {len(advs)} 个广播，里面没有 {node.app_name or node.identity}"]
    if peers:
        parts.append(f"同类型节点在广播的有 {len(peers)} 台：" + ", ".join(a.name for a in peers[:6]))
    if receivers:
        parts.append(f"另有 {len(receivers)} 台已在接收固件模式")
    parts.append("这个节点不在广播，最可能是它正被网关连着（应用固件被连上后就停止广播了）")
    return "；".join(parts)


# ── 阶段 1：触发进接收固件 ───────────────────────────────────────────────────


async def _phase1_trigger(node: OtaNode, opts: OtaOptions, rep: OtaReporter) -> bool:
    """返回是否真的发了触发字（节点已在 OTA 模式时不必再发）。

    这里按 trigger_wait 轮询而不是扫一轮就报错：节点被网关连着时不广播，而"把网关抱远"这种动作
    本来就要几十秒，轮询期间网关一旦因监督超时（固件里是 4s）断开，节点立刻恢复广播，就能被抢到。
    """
    client_cls, _ = _bleak()
    start = time.monotonic()
    deadline = start + max(opts.scan_timeout, opts.trigger_wait)
    next_hint = start + 10.0
    stop_when = scan_target(node, opts.receiver_prefix, want_app=True)
    advs: list[Advert] = []
    while True:
        rep.check()
        budget = max(0.5, deadline - time.monotonic())
        advs = await _discover(min(opts.scan_timeout, budget), stop_when=stop_when)
        target = match_app(advs, node, opts.receiver_prefix)
        if target is not None:
            rep.log(f"阶段1 连接应用固件 {target.name} [{target.address}] "
                    f"（扫描用时 {time.monotonic() - start:.1f}s）")
            async with client_cls(target.address, timeout=opts.connect_timeout) as client:
                # 无响应写：设备收到后立即 esp_restart()，带响应的写在 WinRT 上会挂到超时
                await client.write_gatt_char(TRIGGER_CHAR, TRIGGER_MAGIC, response=False)
            rep.log(f"阶段1 已触发，等节点重启进接收固件（先等 {opts.reboot_wait:.1f}s，之后边扫边等）")
            await _sleep(opts.reboot_wait, rep)
            return True

        already, _ = match_receiver(advs, node, opts.receiver_prefix)
        if already is not None:
            rep.log(f"阶段1 跳过：{already.address} 已在接收固件模式（上次中断的升级，直接续传）")
            return False

        now = time.monotonic()
        if now >= deadline:
            break
        if now >= next_hint:
            rep.log(f"等节点空闲广播已 {now - start:.0f}s：它多半正被网关连着；"
                    "把网关断电或搬远都行，4s 监督超时后节点就空闲了")
            next_hint = now + 10.0
        await _sleep(0.2, rep)
    raise OtaError(scan_summary(node, advs, opts.receiver_prefix) + "。"
                   f"已经等了 {opts.trigger_wait:.0f}s；把网关断电或搬远（4s 监督超时后节点就空闲了），"
                   "或让该节点单独上电，再重试")


# ── 阶段 2：传输固件 ─────────────────────────────────────────────────────────


def has_ota_profile(client) -> bool:
    """连上了不等于连对了：只有接收固件的 GATT 表里才有 0x8020 / 0x8022。"""
    try:
        chars = {c.uuid.lower() for svc in client.services for c in svc.characteristics}
    except Exception:  # 拿不到服务表就当没认出来，交给上层重试
        return False
    return RECV_FW_CHAR in chars and COMMAND_CHAR in chars


async def _refresh_gatt_cache(client) -> bool:
    """让 Windows 别再把缓存的 GATT 表端给我们。

    双固件设计里同一个 BLE 地址先后挂着两套服务表（应用固件 → 接收固件），而 WinRT 的 GATT 缓存
    是按地址存的。不主动用 UNCACHED 读一次，bleak 建服务表时拿到的还是应用固件那张，
    0x8020 就"不存在"。非 Windows 或拿不到底层句柄时安静返回 False。
    """
    requester = getattr(getattr(client, "_backend", client), "_requester", None)
    if requester is None:
        return False
    try:
        from winrt.windows.devices.bluetooth import BluetoothCacheMode

        await requester.get_gatt_services_async(BluetoothCacheMode.UNCACHED)
        return True
    except Exception:
        return False


async def _phase2_connect(node: OtaNode, opts: OtaOptions, rep: OtaReporter):
    client_cls, _ = _bleak()
    start = time.monotonic()
    deadline = start + opts.reboot_wait + opts.receiver_deadline
    stop_when = scan_target(node, opts.receiver_prefix, want_app=False)
    last_seen = 0
    attempts = 0
    while True:
        rep.check()
        budget = max(0.5, deadline - time.monotonic())
        advs = await _discover(min(opts.scan_timeout, budget), stop_when=stop_when)
        target, last_seen = match_receiver(advs, node, opts.receiver_prefix)
        if target is not None:
            client = client_cls(target.address, timeout=opts.connect_timeout)
            await client.connect()
            if has_ota_profile(client):
                if not node.address:
                    rep.log("警告：该节点完整 BLE 地址未知，只能按广播名认接收固件")
                extra = f"，重连 {attempts} 次后认出" if attempts else ""
                rep.log(f"阶段2 连上接收固件 {target.address}"
                        f"（等重启 + 扫描 + 建表共 {time.monotonic() - start:.1f}s{extra}）")
                return client
            attempts += 1
            refreshed = await _refresh_gatt_cache(client)
            await client.disconnect()
            rep.log(f"阶段2 第 {attempts} 次连上 {target.address} 却没有 OTA 特征："
                    + ("已强制 Windows 用 UNCACHED 重读 GATT 表，断开重连"
                       if refreshed else "本机拿不到 GATT 缓存句柄，直接断开重连")
                    + "（设备可能还在重启，也可能 Windows 端的是旧服务表）")
        if time.monotonic() >= deadline:
            if attempts:
                # 连得上但服务表不对：先说这个，别报成"搜不到该地址"误导人去查广播
                raise OtaError(f"连上了 {node.address} 但始终看不到 OTA 特征（试了 {attempts} 次）。"
                               "多半是 Windows 的 GATT 缓存还停在应用固件那张表：把蓝牙适配器"
                               "停用再启用（或重启机器）后重试；也可能是设备没重启完")
            if last_seen and node.address:
                raise OtaError(f"有 {last_seen} 台接收固件在场，但没有一台是 {node.address}；"
                               "对不上地址就不灌，避免把固件写进别的板子")
            raise OtaError(f"{opts.receiver_deadline:.0f}s 内没等到 OTA 接收固件"
                           "（节点可能没进 factory，或本机蓝牙适配器不可用）")
        await _sleep(0.2, rep)


async def _send_firmware(client, fw: bytes, opts: OtaOptions, rep: OtaReporter) -> None:
    mtu = getattr(client, "mtu_size", None) or 23
    payload_bytes = packet_payload_bytes(mtu)
    if payload_bytes < MIN_PACKET_PAYLOAD_BYTES:
        raise OtaError(f"协商到的 MTU 只有 {mtu}：每扇区会超过 {PACKET_INDEX_LIMIT} 包，"
                       f"包号（1 字节，0xFF 留给末包）装不下。至少需要 MTU {min_mtu()}，"
                       "接收固件按 517 配置")
    sectors = split_sectors(fw)
    total = len(fw)
    # 接收固件在 host 任务里一开始广播，app_main 却还要往后跑两行才注册数据回调、才建 ota_task。
    # 扫描改成"扫到就连"之后，我们正好可能落在这个窗口里：连得上、写进去的数据没人收，开始命令
    # 的 ACK 也等不来。所以连上之后先静置一下再动 GATT。
    if opts.settle_seconds > 0:
        rep.log(f"阶段2 连上后静置 {opts.settle_seconds:.1f}s，等接收固件把回调和 ota_task 建好")
        await _sleep(opts.settle_seconds, rep)
    rep.log(f"阶段2 MTU={mtu}，净载 {payload_bytes} B/包，共 {len(sectors)} 扇区 / {total:,} B")
    if payload_bytes < 100:
        rep.log("警告：MTU 没谈下来（接收固件期望 517），逐包 3 字节头会让传输慢一个数量级")

    waiter = _AckWaiter()
    for char in (RECV_FW_CHAR, COMMAND_CHAR):
        await client.start_notify(char, waiter.handler(char))

    await client.write_gatt_char(COMMAND_CHAR, CMD_START + struct.pack("<I", total), response=True)
    await waiter.wait(COMMAND_CHAR, 0x01, opts.ack_timeout, rep, "开始命令")

    retries = 0
    packets = 0
    t_write = t_ack = 0.0
    t_send = time.monotonic()
    for sector, data in enumerate(sectors):
        while True:
            rep.check()
            nacked = False
            for value, is_last in sector_packets(sector, data, payload_bytes):
                t0 = time.monotonic()
                await client.write_gatt_char(RECV_FW_CHAR, value, response=True)
                t_write += time.monotonic() - t0
                packets += 1
                if not is_last:
                    continue
                t1 = time.monotonic()
                ack = await waiter.wait(RECV_FW_CHAR, None, opts.ack_timeout, rep, f"扇区 {sector}")
                t_ack += time.monotonic() - t1
                status = ack_status(ack)
                if status == STATUS_RETRY:
                    retries += 1
                    nacked = True
                    break
                if status != STATUS_OK:
                    raise OtaError(f"扇区 {sector} 的 ACK 状态异常：{status!r}")
            if not nacked:
                break
            rep.log(f"扇区 {sector} 被 NACK，整扇区重传（累计 {retries} 次）")
        done = min((sector + 1) * SECTOR_SIZE, total)
        rep.progress(done, total)

    dt = time.monotonic() - t_send
    rate = total / dt / 1024 if dt > 0 else 0.0
    nsec = max(len(sectors), 1)
    rep.log(f"阶段2 传完 {total:,} B 用时 {dt:.1f}s（{rate:.0f} KB/s，重传 {retries} 次）")
    if packets:
        rep.log(f"  拆开看：{packets} 次带应答写 {t_write:.1f}s（均 {t_write / packets * 1000:.0f} ms/包）"
                f"，等 {nsec} 次扇区 ACK {t_ack:.1f}s（均 {t_ack / nsec * 1000:.0f} ms/扇区）")
        rep.log("  写占大头 = 连接间隔/适配器限制，上位机侧无解；ACK 占大头 = 设备侧闪存背压或指示确认慢")

    # 完成与否的判据是"每个扇区都被 ACK 过"，不是这条结束命令。设备收满 fw_len 字节就自己
    # esp_restart() 了（ota_task 里 recv_len >= fw_length 直接 break），所以这一步几乎必然
    # 撞上断链；bleak 抛的是 BleakError/OSError 之类，不能当成升级失败。
    try:
        await client.write_gatt_char(COMMAND_CHAR, CMD_STOP, response=True)
        await waiter.wait(COMMAND_CHAR, 0x02, min(0.5, opts.ack_timeout), rep, "结束命令")
    except OtaCancelled:
        raise
    except Exception as exc:
        rep.log(f"结束命令没送到/没确认（{type(exc).__name__}）—— 设备收满 {total:,} B 后会自行重启，"
                f"{len(sectors)} 个扇区已全部 ACK，按完成处理")


# ── 对外入口 ─────────────────────────────────────────────────────────────────


async def _verify_app(node: OtaNode, fw: bytes, opts: OtaOptions, rep: OtaReporter) -> None:
    """传完后回到应用固件读版本特征，确认跑的确实是我们灌进去的那一份。

    设备收满字节就自行重启，"校验失败回退"和"正常完成"在 PC 侧看到的断链一模一样，
    不回读比对就会把拒收报成成功（坏镜像实机测试暴露过这个盲区）。
    """
    expected = parse_app_desc(fw)
    client_cls, _ = _bleak()
    stop_when = scan_target(node, opts.receiver_prefix, want_app=True)
    deadline = time.monotonic() + opts.verify_deadline
    raw: bytes | None = None
    while True:
        rep.check()
        budget = max(0.5, deadline - time.monotonic())
        advs = await _discover(min(opts.scan_timeout, budget), stop_when=stop_when)
        target = match_app(advs, node, opts.receiver_prefix)
        if target is None:
            if time.monotonic() >= deadline:
                raise OtaError(f"升级后 {opts.verify_deadline:.0f}s 内节点没有回到应用固件"
                               "（可能没启动起来，或还在接收模式）")
            await _sleep(0.3, rep)
            continue
        async with client_cls(target.address, timeout=opts.connect_timeout) as client:
            chars = {c.uuid.lower() for svc in client.services for c in svc.characteristics}
            if VERSION_CHAR in chars:
                raw = bytes(await client.read_gatt_char(VERSION_CHAR))
                break
            # 设备刚重启时 Windows 可能还端着旧 GATT 表：强刷一次，断开重连再查
            await _refresh_gatt_cache(client)
        if time.monotonic() >= deadline:
            rep.log("校验跳过：应用固件未暴露版本特征 0xFFF2（旧固件，或 GATT 缓存一直没刷过来），"
                    "无法确认跑的是哪一份")
            return
        await _sleep(0.5, rep)

    got = parse_version_payload(raw)
    if got is None:
        rep.log(f"校验跳过：版本特征长度异常（{len(raw)} B，期望 ≥64）")
        return
    version, sha = got
    if expected is None:
        rep.log(f"校验跳过：固件里读不到镜像描述符；设备当前 version={version}")
        return
    if sha != expected.sha256:
        raise OtaError(f"设备拒收或回滚了：设备 sha={sha[:16]}… ≠ 期望 {expected.sha256[:16]}…"
                       f"（设备 version={version}，期望 {expected.version}）")
    rep.log(f"校验通过：version={version}，sha={sha[:16]}…，built={expected.built}")


async def _upgrade_one(node: OtaNode, fw: bytes, opts: OtaOptions, rep: OtaReporter) -> None:
    await _phase1_trigger(node, opts, rep)
    rep.state(node.key, "传输中")
    client = await _phase2_connect(node, opts, rep)
    try:
        await _send_firmware(client, fw, opts, rep)
    finally:
        await client.disconnect()
    rep.state(node.key, "校验中")
    await _verify_app(node, fw, opts, rep)


def upgrade_node(node: OtaNode, fw: bytes, opts: OtaOptions, rep: OtaReporter) -> None:
    rep.state(node.key, "触发中")
    asyncio.run(_upgrade_one(node, fw, opts, rep))


def upgrade_nodes(jobs: Sequence[tuple[OtaNode, bytes]], opts: OtaOptions,
                  rep: OtaReporter) -> list[tuple[OtaNode, str]]:
    """顺序升级，返回每个节点的失败原因（空串表示成功）。中途取消则后面的节点不再开始。"""
    results: list[tuple[OtaNode, str]] = []
    for i, (node, fw) in enumerate(jobs, 1):
        if rep.cancel.is_set():
            rep.state(node.key, "未开始")
            results.append((node, "已取消，未开始"))
            continue
        rep.log(f"── [{i}/{len(jobs)}] {node.label or node.app_name} {node.identity}"
                f"（{len(fw):,} B）")
        t0 = time.monotonic()
        try:
            upgrade_node(node, fw, opts, rep)
        except OtaCancelled as exc:
            rep.state(node.key, "已取消")
            rep.log(f"已取消：{node.identity}")
            results.append((node, str(exc)))
        except Exception as exc:
            # bleak 的异常谱系很杂（BleakError/OSError/TimeoutError/KeyError…），
            # 一块板的意外不能把后面几块一起带走
            reason = str(exc) if isinstance(exc, OtaError) else f"{type(exc).__name__}: {exc}"
            rep.state(node.key, "失败")
            rep.log(f"失败：{node.identity} — {reason}")
            results.append((node, reason))
        else:
            rep.state(node.key, "完成")
            rep.progress(len(fw), len(fw))
            rep.log(f"完成：{node.identity} 已重启进新固件，整机 {time.monotonic() - t0:.1f}s")
            results.append((node, ""))
    return results
