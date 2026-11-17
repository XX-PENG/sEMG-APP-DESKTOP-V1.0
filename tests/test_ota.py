"""BLE OTA 的分包/ACK/身份匹配单测（不碰蓝牙硬件）。"""

import asyncio
import os
import struct
import threading
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402

from semg_desktop import ota  # noqa: E402


# ── CRC 与 ACK ───────────────────────────────────────────────────────────────


def test_crc16_matches_xmodem_check_value():
    assert ota.crc16_ccitt(b"123456789") == 0x31C3
    assert ota.crc16_ccitt(b"") == 0x0000


def test_make_ack_roundtrip():
    ack = ota.make_ack(ota.STATUS_OK)
    assert len(ack) == ota.ACK_LEN
    assert ota.ack_crc_ok(ack)
    assert ota.ack_status(ack) == ota.STATUS_OK
    assert ota.ack_status(ota.make_ack(ota.STATUS_RETRY)) == ota.STATUS_RETRY


def test_ack_rejects_short_and_corrupt():
    assert not ota.ack_crc_ok(b"\x01\x00\x00")
    assert not ota.ack_crc_ok(ota.make_ack(0)[:-1])
    bad = bytearray(ota.make_ack(0))
    bad[5] ^= 0xFF
    assert not ota.ack_crc_ok(bytes(bad))
    assert ota.ack_status(b"") is None


# ── 分包 ─────────────────────────────────────────────────────────────────────


def test_packet_payload_leaves_room_for_headers_and_trailer():
    assert ota.packet_payload_bytes(517) == 517 - 3 - 3 - 2 == 509
    assert ota.packet_payload_bytes(23) == 15
    assert ota.packet_payload_bytes(1) == 1  # 再小也不能给出 0，否则死循环


def test_split_sectors_boundaries():
    assert ota.split_sectors(b"") == []
    assert [len(s) for s in ota.split_sectors(b"12345678")] == [8]
    fw = bytes(ota.SECTOR_SIZE * 2 + 100)
    assert [len(s) for s in ota.split_sectors(fw)] == [4096, 4096, 100]


def test_sector_packets_layout_and_reassembly():
    fw = bytes(range(256)) * 40  # 10240 B -> 3 扇区
    sectors = ota.split_sectors(fw)
    payload = 500
    rebuilt = bytearray()
    for sector, data in enumerate(sectors):
        packets = list(ota.sector_packets(sector, data, payload))
        assert packets[-1][1] is True  # 最后一包必须标末包
        assert sum(1 for _, last in packets if last) == 1
        for value, is_last in packets:
            got_sector, pkt = struct.unpack("<HB", value[:3])
            body = value[3:-2] if is_last else value[3:]
            assert got_sector == sector
            if is_last:
                assert pkt == 0xFF
                assert value[-2:] == ota.PACKET_TRAILER
            else:
                assert pkt < 0xFF
            rebuilt += body
        # 扇区号在 u16 里回绕，超过 65536 扇区（256 MB）才会用到，这里只验证截断行为
    assert bytes(rebuilt) == fw


def test_sector_packets_sector_wraps_at_u16():
    value, _ = next(iter(ota.sector_packets(0x1_0005, b"1234", 2)))
    assert value[:3] == struct.pack("<HB", 5, 0)


def test_load_firmware_checks(tmp_path, monkeypatch):
    with pytest.raises(ota.OtaError, match="不存在"):
        ota.load_firmware(tmp_path / "nope.bin")

    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with pytest.raises(ota.OtaError, match="空的"):
        ota.load_firmware(empty)

    big = tmp_path / "big.bin"
    big.write_bytes(b"\xe9" + bytes(1024))
    monkeypatch.setattr(ota, "OTA_SLOT_BYTES", 512)
    with pytest.raises(ota.OtaError, match="超过 OTA 分区上限"):
        ota.load_firmware(big)

    monkeypatch.setattr(ota, "OTA_SLOT_BYTES", 0x200_000)
    data = ota.load_firmware(big)
    assert data.startswith(b"\xe9")
    assert ota.firmware_warnings(data) == []
    assert ota.firmware_warnings(b"\x7f" + bytes(8))  # 不是 ESP 镜像


# ── 身份匹配 ─────────────────────────────────────────────────────────────────


def test_node_from_gateway_row_uses_full_mac_as_address():
    row = {"kind": "semg", "mac": "A4:CF:01", "full_mac": "AA:BB:CC:A4:CF:01", "label": "CH3"}
    node = ota.node_from_row(row)
    assert node.kind == "semg"
    assert node.address == "AA:BB:CC:A4:CF:01"
    assert node.app_name == "sEMG-ESP32-A4CF01"
    assert node.key == "semg@a4cf01"


def test_node_key_respects_ui_uid():
    node = ota.OtaNode(kind="semg", suffix="A4:CF:01", uid="A4:CF:01")
    assert node.key == "A4:CF:01"


def test_node_from_advert_derives_kind_and_suffix():
    node = ota.node_from_advert(ota.Advert(name="IMU-ESP32-123490", address="AA:BB:CC:12:34:90"))
    assert node is not None and node.kind == "imu"
    assert node.suffix == "12:34:90"
    assert ota.node_from_advert(ota.Advert(name="nimble-ble-ota", address="AA:BB:CC:12:34:90")) is None


def test_suffix_from_name_when_address_missing():
    # 广播名是唯一线索时也要还原成 "A4:CF:01" 形式
    assert ota._suffix_from_name("sEMG-ESP32-a4cf01", "semg") == "A4:CF:01"
    assert ota._suffix_from_name("sEMG-ESP32", "semg") == ""
    assert ota._suffix_from_name("其他设备", "semg") == ""


def test_match_app_prefers_address_over_name():
    node = ota.OtaNode(kind="semg", suffix="A4:CF:01", address="AA:BB:CC:A4:CF:01")
    advs = [
        ota.Advert(name="sEMG-ESP32-A4CF01", address="11:22:33:A4:CF:01"),   # 同名不同址，干扰项
        ota.Advert(name="IMU-ESP32-123490", address="AA:BB:CC:A4:CF:01"),    # 同址，真身
    ]
    assert ota.match_app(advs, node).address == "AA:BB:CC:A4:CF:01"


def test_match_app_falls_back_to_name():
    node = ota.OtaNode(kind="semg", suffix="A4:CF:01")
    assert ota.match_app([ota.Advert(name="sEMG-ESP32-A4CF01", address="AA:BB:CC:A4:CF:01")], node)
    assert ota.match_app([ota.Advert(name="sEMG-ESP32-999999", address="1")], node) is None


def test_match_receiver_locks_by_address_and_refuses_to_guess():
    node = ota.OtaNode(kind="semg", suffix="A4:CF:01", address="AA:BB:CC:A4:CF:01")
    others = ota.Advert(name="nimble-ble-ota", address="DD:EE:FF:11:22:33")
    mine = ota.Advert(name="nimble-ble-ota", address="AA:BB:CC:A4:CF:01")
    hit, seen = ota.match_receiver([others, mine], node, "nimble-ble-ota")
    assert hit is mine and seen == 2

    hit, seen = ota.match_receiver([others], node, "nimble-ble-ota")
    assert hit is None and seen == 1

    no_addr = ota.OtaNode(kind="", suffix="A4:CF:01")
    assert ota.match_receiver([mine], no_addr, "nimble-ble-ota")[0] is mine
    # 两台都在 OTA 模式又不知道目标地址，绝不能挑一台灌
    assert ota.match_receiver([mine, others], no_addr, "nimble-ble-ota")[0] is None


# ── 编排 ─────────────────────────────────────────────────────────────────────


def _reporter(log):
    return ota.OtaReporter(on_log=log.append, on_state=lambda k, t: log.append(f"state:{k}:{t}"))


def test_match_app_ignores_receiver_sharing_the_same_address():
    """接收固件和应用固件是同一块板、同一个 BLE 地址，只比地址会把接收态误认成应用态。"""
    node = ota.OtaNode(kind="imu", suffix="34:2B:FA", address="AA:BB:CC:34:2B:FA")
    receiver_only = [ota.Advert(name="nimble-ble-ota", address="AA:BB:CC:34:2B:FA")]
    assert ota.match_app(receiver_only, node) is None
    both = [ota.Advert(name="nimble-ble-ota", address="AA:BB:CC:34:2B:FA"),
            ota.Advert(name="IMU-ESP32-342BFA", address="AA:BB:CC:34:2B:FA")]
    assert ota.match_app(both, node).name == "IMU-ESP32-342BFA"


def _fw_with_desc(version: str = "test-1.0", sha: bytes = bytes(range(32))) -> bytes:
    """造一份带合法 esp_app_desc_t 的镜像：0x20 处放描述符。"""
    desc = bytearray(256)
    struct.pack_into("<I", desc, 0, ota.APP_DESC_MAGIC)
    desc[16:16 + len(version)] = version.encode()
    desc[48:56] = b"bleprph\x00"
    desc[80:88] = b"11:48:18\x00"
    desc[96:108] = b"Nov 17 2026\x00"
    desc[144:176] = sha
    return b"\xe9" + bytes(0x1F) + bytes(desc) + bytes(1024)


def test_parse_app_desc_reads_version_and_fingerprint():
    sha = bytes((i * 3) & 0xFF for i in range(32))
    desc = ota.parse_app_desc(_fw_with_desc("235c4e7-dirty", sha))
    assert desc is not None
    assert desc.version == "235c4e7-dirty"
    assert desc.project == "bleprph"
    assert desc.sha256 == sha.hex()
    assert "Nov 17 2026" in desc.built and "11:48:18" in desc.built


def test_parse_app_desc_rejects_non_esp_images():
    assert ota.parse_app_desc(b"") is None
    assert ota.parse_app_desc(bytes(512)) is None          # 魔数不对
    assert ota.parse_app_desc(b"\xe9" + bytes(64)) is None  # 太短


def test_parse_version_payload():
    raw = b"1.2.3".ljust(32, b"\x00") + bytes(range(32))
    assert ota.parse_version_payload(raw) == ("1.2.3", bytes(range(32)).hex())
    assert ota.parse_version_payload(b"short") is None


class FakeApp:
    """升级后回到应用固件的替身：可控制是否暴露版本特征、读回什么。"""

    def __init__(self, address, payload=None, has_char=True):
        self.address = address
        self.payload = payload
        self.services = [_Svc([ota.VERSION_CHAR] if has_char else [ota.TRIGGER_CHAR])]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def read_gatt_char(self, char):
        return self.payload


def _patch_app_scan(monkeypatch, client):
    adv = ota.Advert(name="IMU-ESP32-342BFA", address="DC:B4:D9:34:2B:FA")

    async def fake_discover(timeout, stop_when=None):
        return [adv]

    monkeypatch.setattr(ota, "_discover", fake_discover)
    monkeypatch.setattr(ota, "_bleak", lambda: (lambda a, timeout=30.0: client, object()))


def _verify_node():
    return ota.OtaNode(kind="imu", suffix="34:2B:FA", address="DC:B4:D9:34:2B:FA")


def _verify_opts():
    return ota.OtaOptions(scan_timeout=0.01, verify_deadline=1.0, connect_timeout=1.0)


def test_verify_app_passes_when_fingerprint_matches(monkeypatch):
    sha = bytes(range(32))
    fw = _fw_with_desc("v9", sha)
    payload = b"v9".ljust(32, b"\x00") + sha
    _patch_app_scan(monkeypatch, FakeApp("DC:B4:D9:34:2B:FA", payload))
    log: list[str] = []
    asyncio.run(ota._verify_app(_verify_node(), fw, _verify_opts(), _quiet_reporter(log)))
    assert any("校验通过" in m for m in log)


def test_verify_app_fails_when_device_kept_the_old_image(monkeypatch):
    """坏镜像被拒收/回滚时设备跑的还是旧固件 —— 必须判失败，不能报成功。"""
    fw = _fw_with_desc("v9", bytes(range(32)))
    old = b"v8".ljust(32, b"\x00") + bytes(32)
    _patch_app_scan(monkeypatch, FakeApp("DC:B4:D9:34:2B:FA", old))
    with pytest.raises(ota.OtaError, match="拒收或回滚"):
        asyncio.run(ota._verify_app(_verify_node(), fw, _verify_opts(), _quiet_reporter([])))


def test_verify_app_degrades_gracefully_on_old_firmware(monkeypatch):
    """旧应用固件没有 0xFFF2：跳过校验而不是把升级判失败。"""
    _patch_app_scan(monkeypatch, FakeApp("DC:B4:D9:34:2B:FA", has_char=False))
    log: list[str] = []
    asyncio.run(ota._verify_app(_verify_node(), _fw_with_desc(), _verify_opts(), _quiet_reporter(log)))
    assert any("校验跳过" in m for m in log)


def test_verify_app_retries_when_windows_serves_a_stale_table(monkeypatch):
    """设备刚重启时 Windows 可能端着旧 GATT 表：不能查一次就放弃，要刷缓存重连再查。"""
    sha = bytes(range(32))
    fw = _fw_with_desc("v9", sha)
    payload = b"v9".ljust(32, b"\x00") + sha
    made = []

    class StaleThenFresh:
        def __init__(self, address, timeout=30.0):
            self.first = not made
            made.append(self)
            self.services = [_Svc([ota.TRIGGER_CHAR] if self.first else [ota.VERSION_CHAR])]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def read_gatt_char(self, char):
            return payload

    adv = ota.Advert(name="IMU-ESP32-342BFA", address="DC:B4:D9:34:2B:FA")

    async def fake_discover(timeout, stop_when=None):
        return [adv]

    monkeypatch.setattr(ota, "_discover", fake_discover)
    monkeypatch.setattr(ota, "_bleak", lambda: (StaleThenFresh, object()))
    log: list[str] = []
    asyncio.run(ota._verify_app(_verify_node(), fw, _verify_opts(), _quiet_reporter(log)))
    assert len(made) == 2 and any("校验通过" in m for m in log)


def test_verify_app_fails_when_node_never_returns(monkeypatch):
    async def nothing(timeout, stop_when=None):
        return []

    monkeypatch.setattr(ota, "_discover", nothing)
    monkeypatch.setattr(ota, "_bleak", lambda: (FakeApp, object()))
    with pytest.raises(ota.OtaError, match="没有回到应用固件"):
        asyncio.run(ota._verify_app(_verify_node(), _fw_with_desc(), _verify_opts(), _quiet_reporter([])))


def test_scan_summary_turns_not_found_into_actionable_diagnosis():
    node = ota.OtaNode(kind="imu", suffix="34:2B:FA", address="AA:BB:CC:34:2B:FA")
    assert "适配器" in ota.scan_summary(node, [], "nimble-ble-ota")

    wrong_name = [ota.Advert(name="IMU-OLDNAME-342BFA", address="AA:BB:CC:34:2B:FA")]
    assert "名字是" in ota.scan_summary(node, wrong_name, "nimble-ble-ota")

    crowd = [
        ota.Advert(name="IMU-ESP32-111111", address="11:22:33:11:11:11"),
        ota.Advert(name="sEMG-ESP32-A4CF01", address="11:22:33:A4:CF:01"),
        ota.Advert(name="nimble-ble-ota", address="11:22:33:DE:AD:BE"),
    ]
    text = ota.scan_summary(node, crowd, "nimble-ble-ota")
    assert "扫描到 3 个广播" in text
    assert "同类型节点在广播的有 1 台：IMU-ESP32-111111" in text  # 只点名同类，不被 sEMG 干扰
    assert "另有 1 台已在接收固件模式" in text
    assert "被网关连着" in text


def test_upgrade_nodes_runs_sequentially_and_reports(monkeypatch):
    calls = []
    monkeypatch.setattr(ota, "upgrade_node",
                        lambda node, fw, opts, rep: calls.append((node.key, len(fw))))
    nodes = [ota.OtaNode(kind="semg", suffix="A4:CF:0%d" % i, uid="A4:CF:0%d" % i) for i in (1, 2)]
    log = []
    results = ota.upgrade_nodes([(nodes[0], b"123"), (nodes[1], b"45")], ota.OtaOptions(), _reporter(log))
    assert calls == [("A4:CF:01", 3), ("A4:CF:02", 2)]
    assert [err for _, err in results] == ["", ""]
    assert any(s.startswith("state:A4:CF:02:完成") for s in log)


def test_upgrade_nodes_keeps_going_after_a_failure(monkeypatch):
    def boom(node, fw, opts, rep):
        if node.key == "A4:CF:01":
            raise ota.OtaError("没搜到节点")

    monkeypatch.setattr(ota, "upgrade_node", boom)
    nodes = [ota.OtaNode(kind="semg", suffix=s, uid=s) for s in ("A4:CF:01", "A4:CF:02")]
    log = []
    results = ota.upgrade_nodes([(nodes[0], b"x"), (nodes[1], b"y")], ota.OtaOptions(), _reporter(log))
    assert results[0][1] == "没搜到节点"
    assert results[1][1] == ""


def test_upgrade_nodes_survives_unexpected_bleak_errors(monkeypatch):
    """bleak 抛的异常种类很杂，一块板的意外不能把整队带走。"""

    def boom(node, fw, opts, rep):
        if node.key == "A4:CF:01":
            raise ConnectionResetError("链路断了")

    monkeypatch.setattr(ota, "upgrade_node", boom)
    nodes = [ota.OtaNode(kind="semg", suffix=s, uid=s) for s in ("A4:CF:01", "A4:CF:02")]
    log = []
    results = ota.upgrade_nodes([(nodes[0], b"x"), (nodes[1], b"y")], ota.OtaOptions(), _reporter(log))
    assert results[0][1] == "ConnectionResetError: 链路断了"
    assert results[1][1] == ""


def test_upgrade_nodes_cancel_skips_remaining(monkeypatch):
    cancel = threading.Event()

    def once(node, fw, opts, rep):
        cancel.set()  # 第一台做完就把取消打开，模拟用户按了取消

    monkeypatch.setattr(ota, "upgrade_node", once)
    nodes = [ota.OtaNode(kind="semg", suffix=s, uid=s) for s in ("A4:CF:01", "A4:CF:02")]
    log = []
    rep = ota.OtaReporter(on_log=log.append, on_state=lambda k, t: None, cancel=cancel)
    results = ota.upgrade_nodes([(n, b"x") for n in nodes], ota.OtaOptions(), rep)
    assert results[0][1] == ""
    assert "取消" in results[1][1]


def test_options_from_config():
    from semg_desktop.config import load_config

    opts = ota.OtaOptions.from_config(load_config(None))
    assert opts.receiver_prefix == "nimble-ble-ota"
    assert opts.scan_timeout > 0 and opts.reboot_wait > 0 and opts.ack_timeout > 0
    assert opts.trigger_wait > 0  # 阶段1 的"等节点空闲"窗口，网关不断电就靠它


class _TriggerClient:
    """只用来记录触发字写了什么，不碰蓝牙。"""

    writes: list[tuple[str, bytes, object]] = []

    def __init__(self, address, timeout=30.0):
        self.address = address

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def write_gatt_char(self, char, data, response=None):
        _TriggerClient.writes.append((char, bytes(data), response))


def _patch_ble(monkeypatch, rounds):
    calls: list[int] = []

    async def fake_discover(timeout, stop_when=None):
        found = rounds[min(len(calls), len(rounds) - 1)]
        calls.append(1)
        return found

    monkeypatch.setattr(ota, "_discover", fake_discover)
    monkeypatch.setattr(ota, "_bleak", lambda: (_TriggerClient, object()))
    return calls


def test_phase1_polls_until_the_node_goes_free(monkeypatch):
    """前两轮空手（节点还被网关占着），第三轮网关断开 -> 自动抢下来触发，不用人重跑。"""
    app = ota.Advert(name="IMU-ESP32-342BFA", address="AA:BB:CC:34:2B:FA")
    calls = _patch_ble(monkeypatch, [[], [], [app]])
    _TriggerClient.writes = []
    node = ota.OtaNode(kind="imu", suffix="34:2B:FA", address="AA:BB:CC:34:2B:FA")
    opts = ota.OtaOptions(trigger_wait=5.0, reboot_wait=0.0, scan_timeout=0.01)

    sent = asyncio.run(ota._phase1_trigger(node, opts, _quiet_reporter([])))
    assert sent is True
    assert len(calls) == 3
    assert _TriggerClient.writes == [(ota.TRIGGER_CHAR, ota.TRIGGER_MAGIC, False)]


def test_phase1_skips_trigger_when_receiver_is_already_up(monkeypatch):
    """上次中断后节点卡在接收固件：不必再触发，直接进阶段2 续传。"""
    rcv = ota.Advert(name="nimble-ble-ota", address="AA:BB:CC:34:2B:FA")
    _patch_ble(monkeypatch, [[rcv]])
    _TriggerClient.writes = []
    node = ota.OtaNode(kind="imu", suffix="34:2B:FA", address="AA:BB:CC:34:2B:FA")
    opts = ota.OtaOptions(trigger_wait=5.0, scan_timeout=0.01)

    assert asyncio.run(ota._phase1_trigger(node, opts, _quiet_reporter([]))) is False
    assert _TriggerClient.writes == []


def test_phase1_gives_up_with_diagnosis(monkeypatch):
    async def nothing(timeout, stop_when=None):
        return []

    monkeypatch.setattr(ota, "_discover", nothing)
    monkeypatch.setattr(ota, "_bleak", lambda: (_TriggerClient, object()))
    node = ota.OtaNode(kind="imu", suffix="34:2B:FA")
    opts = ota.OtaOptions(trigger_wait=0.05, scan_timeout=0.01)
    with pytest.raises(ota.OtaError, match="适配器"):
        asyncio.run(ota._phase1_trigger(node, opts, _quiet_reporter([])))


def test_phase2_retries_until_the_ota_profile_is_visible(monkeypatch):
    """连上后必须先确认这就是接收固件：设备没重启完、或 Windows 端来缓存的旧服务表时，
    0x8020 根本不存在，硬往下走只会抛 BleakCharacteristicNotFoundError。"""
    rcv = ota.Advert(name="nimble-ble-ota", address="AA:BB:CC:34:2B:FA")

    async def fake_discover(timeout, stop_when=None):
        return [rcv]

    made: list[FakeReceiver] = []

    def factory(address, timeout=30.0):
        client = (AppLikeReceiver if not made else FakeReceiver)(address, timeout)
        made.append(client)
        return client

    monkeypatch.setattr(ota, "_discover", fake_discover)
    monkeypatch.setattr(ota, "_bleak", lambda: (factory, object()))
    node = ota.OtaNode(kind="imu", suffix="34:2B:FA", address="AA:BB:CC:34:2B:FA")
    opts = ota.OtaOptions(reboot_wait=0.0, receiver_deadline=3.0, scan_timeout=0.01)
    log: list[str] = []

    client = asyncio.run(ota._phase2_connect(node, opts, _quiet_reporter(log)))
    assert client is made[1]
    assert made[0].connected is False          # 认错的那次被断开重来
    assert any("没有 OTA 特征" in m for m in log)
    assert any("重连 1 次后认出" in m for m in log)


def test_phase2_gives_up_with_gatt_cache_hint(monkeypatch):
    rcv = ota.Advert(name="nimble-ble-ota", address="AA:BB:CC:34:2B:FA")

    async def fake_discover(timeout, stop_when=None):
        return [rcv]

    monkeypatch.setattr(ota, "_discover", fake_discover)
    monkeypatch.setattr(ota, "_bleak", lambda: (AppLikeReceiver, object()))
    node = ota.OtaNode(kind="imu", suffix="34:2B:FA", address="AA:BB:CC:34:2B:FA")
    opts = ota.OtaOptions(reboot_wait=0.0, receiver_deadline=0.2, scan_timeout=0.01)
    with pytest.raises(ota.OtaError, match="GATT 缓存"):
        asyncio.run(ota._phase2_connect(node, opts, _quiet_reporter([])))


def test_refresh_gatt_cache_reaches_through_the_backend():
    """bleak 顶层 BleakClient 只是包装器，_requester 挂在 _backend 上。

    取错层会静默返回 False，UNCACHED 强刷等于没做（实机日志里表现为"拿不到 GATT 缓存句柄"）。
    """
    pytest.importorskip("winrt.windows.devices.bluetooth")
    calls = []

    class Requester:
        async def get_gatt_services_async(self, mode):
            calls.append(mode)

    class Backend:
        _requester = Requester()

    class Wrapper:
        _backend = Backend()

    assert asyncio.run(ota._refresh_gatt_cache(Wrapper())) is True
    assert len(calls) == 1
    assert asyncio.run(ota._refresh_gatt_cache(object())) is False


def test_has_ota_profile_needs_both_characteristics():
    assert ota.has_ota_profile(FakeReceiver("AA:BB:CC:34:2B:FA"))
    assert not ota.has_ota_profile(AppLikeReceiver("AA:BB:CC:34:2B:FA"))

    class NoServices:
        @property
        def services(self):
            raise RuntimeError("bleak 还没建好表")

    assert not ota.has_ota_profile(NoServices())


class _FakeDev:
    def __init__(self, name, address):
        self.name, self.address = name, address


class _FakeAdv:
    def __init__(self, local_name):
        self.local_name = local_name


class _FakeScanner:
    """bleak.BleakScanner 替身：一进上下文就吐一条广播。"""

    def __init__(self, callback, *args, **kwargs):
        self._callback = callback

    async def __aenter__(self):
        self._callback(_FakeDev("IMU-ESP32-342BFA", "AA:BB:CC:34:2B:FA"), _FakeAdv("IMU-ESP32-342BFA"))
        return self

    async def __aexit__(self, *exc):
        return None


def test_discover_returns_as_soon_as_the_target_shows_up(monkeypatch):
    """超时是上限不是固定开销：原来每台光扫描就要等满两轮 scan_timeout。"""
    monkeypatch.setattr(ota, "_bleak", lambda: (object, _FakeScanner))
    node = ota.OtaNode(kind="imu", suffix="34:2B:FA", address="AA:BB:CC:34:2B:FA")
    stop = ota.scan_target(node, "nimble-ble-ota", want_app=True)
    t0 = time.monotonic()
    advs = asyncio.run(ota._discover(5.0, stop_when=stop))
    assert time.monotonic() - t0 < 1.0
    assert [a.address for a in advs] == ["AA:BB:CC:34:2B:FA"]
    assert [a.name for a in advs] == ["IMU-ESP32-342BFA"]


def test_discover_without_stop_waits_the_full_timeout(monkeypatch):
    monkeypatch.setattr(ota, "_bleak", lambda: (object, _FakeScanner))
    t0 = time.monotonic()
    asyncio.run(ota._discover(0.4))
    assert time.monotonic() - t0 >= 0.35


def test_scan_target_refuses_to_early_exit_on_ambiguous_receivers():
    node = ota.OtaNode(kind="imu", suffix="34:2B:FA")  # 地址未知
    assert ota.scan_target(node, "nimble-ble-ota", want_app=False) is None
    with_addr = ota.OtaNode(kind="imu", suffix="34:2B:FA", address="AA:BB:CC:34:2B:FA")
    stop = ota.scan_target(with_addr, "nimble-ble-ota", want_app=False)
    assert stop(ota.Advert(name="nimble-ble-ota", address="AA:BB:CC:34:2B:FA"))
    assert not stop(ota.Advert(name="nimble-ble-ota", address="11:22:33:44:55:66"))
    assert ota.scan_target(with_addr, "nimble-ble-ota", want_app=True)(
        ota.Advert(name="IMU-ESP32-342BFA", address="AA:BB:CC:34:2B:FA"))


# ── 用假接收固件跑通整条链路（不需要蓝牙） ───────────────────────────────────


class _Char:
    def __init__(self, uuid):
        self.uuid = uuid


class _Svc:
    def __init__(self, uuids):
        self.characteristics = [_Char(u) for u in uuids]


class FakeReceiver:
    """按协议回 ACK 的接收固件替身：收下的净载按扇区重组，最后应与源固件逐字节相同。

    顺带模拟两类真实故障：某个扇区先 NACK 一次（考验整扇区重传），以及结束命令不回 ACK
    （设备直接重启，不能据此判失败）。
    """

    nack_sector = 1

    def __init__(self, address: str, timeout: float = 30.0, mtu: int = 517) -> None:
        self.address = address
        self.timeout = timeout
        self.mtu = mtu
        self.sectors: dict[int, bytearray] = {}
        self.notifiers: dict = {}
        self.writes: list[tuple[str, bytes]] = []
        self.nacked_once: set[int] = set()
        self.fw_len = 0
        self.stopped = False
        self.connected = False
        self.services = [_Svc([ota.RECV_FW_CHAR, ota.COMMAND_CHAR, ota.VERSION_CHAR])]

    async def read_gatt_char(self, char):
        return b"fake-ver".ljust(32, b"\x00") + bytes(32)

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    @property
    def mtu_size(self) -> int:
        return self.mtu

    async def start_notify(self, char, callback) -> None:
        self.notifiers[char] = callback

    def _ack(self, char, status, header=b"\x00\x00") -> None:
        self.notifiers[char](None, bytearray(ota.make_ack(status, header)))

    async def write_gatt_char(self, char, data, response=None) -> None:
        data = bytes(data)
        self.writes.append((char, data))
        if char == ota.TRIGGER_CHAR:  # 应用固件的触发字，不该被当成固件数据
            return
        if char == ota.COMMAND_CHAR:
            if data[:1] == b"\x01":
                self.fw_len = struct.unpack("<I", data[2:6])[0]
                self._ack(char, 0x01, b"\x01\x00")
            else:
                self.stopped = True  # 收到结束命令就重启，ACK 发不出去
            return
        sector, pkt = struct.unpack("<HB", data[:3])
        body = data[3:-2] if pkt == 0xFF else data[3:]
        # 末扇区可能只有一包且直接标 0xFF，从不出现包号 0，所以缓冲区不能只靠 0 来初始化
        buf = bytearray() if pkt == 0 else self.sectors.get(sector, bytearray())
        buf += body
        self.sectors[sector] = buf
        if pkt != 0xFF:
            return
        if sector == self.nack_sector and sector not in self.nacked_once:
            self.nacked_once.add(sector)
            self.sectors[sector] = bytearray()  # 设备 NACK 时把扇区偏移清零，重传才不会叠加
            self._ack(char, ota.STATUS_RETRY)
        else:
            self._ack(char, ota.STATUS_OK)

    def firmware(self) -> bytes:
        return b"".join(bytes(self.sectors[s]) for s in sorted(self.sectors))


class AppLikeReceiver(FakeReceiver):
    """GATT 表里还没有 OTA 特征：设备没重启完，或 Windows 端来了应用固件那张缓存表。"""

    def __init__(self, address, timeout=30.0, mtu=517):
        super().__init__(address, timeout, mtu)
        self.services = [_Svc([ota.TRIGGER_CHAR])]


def _quiet_reporter(log):
    return ota.OtaReporter(on_log=log.append, on_progress=lambda d, t: None,
                           on_state=lambda k, t: log.append(f"state:{k}:{t}"))


async def _drive_send(fw: bytes, mtu: int = 517):
    client = FakeReceiver("AA:BB:CC:11:22:33", mtu=mtu)
    log: list[str] = []
    await ota._send_firmware(client, fw, ota.OtaOptions(ack_timeout=1.0, settle_seconds=0.0), _quiet_reporter(log))
    return client, log


def test_send_firmware_reassembles_exactly():
    fw = bytes((i * 7) & 0xFF for i in range(9000))  # 3 个扇区，末扇区不满
    client, log = asyncio.run(_drive_send(fw))
    assert client.firmware() == fw
    assert client.fw_len == len(fw)
    assert client.stopped
    assert len(client.sectors) == 3
    assert any("NACK" in m for m in log)       # 扇区 1 先被拒过一次
    assert any("结束命令没送到" in m for m in log)  # 设备不回结束 ACK，不能判失败


def test_send_firmware_treats_device_reboot_as_success():
    """设备收满字节就自己重启，结束命令那次写会抛 bleak 的断链异常（不是 OtaError）。

    这条是回归用例：曾经因为只兜了 OtaError，把"固件其实已经写完"报成升级失败。
    """

    class DropOnStop(FakeReceiver):
        async def write_gatt_char(self, char, data, response=None):
            if char == ota.COMMAND_CHAR and bytes(data)[:1] == b"\x02":
                raise ConnectionResetError("disconnected")  # 形如 bleak.BleakError 的非 OtaError
            await super().write_gatt_char(char, data, response)

    client = DropOnStop("AA:BB:CC:11:22:33")
    fw = bytes(ota.SECTOR_SIZE + 8)
    log: list[str] = []
    asyncio.run(ota._send_firmware(client, fw, ota.OtaOptions(ack_timeout=1.0, settle_seconds=0.0),
                                   _quiet_reporter(log)))
    assert client.firmware() == fw
    assert any("按完成处理" in m for m in log)
    assert not any("失败" in m for m in log)


def test_send_firmware_survives_min_mtu():
    """MTU 压到协议下限：每扇区 241 包，包号刚好还够用。"""
    fw = bytes(ota.SECTOR_SIZE + 5)
    client, log = asyncio.run(_drive_send(fw, mtu=ota.min_mtu()))
    assert client.firmware() == fw
    assert not any("太小" in m for m in log)


def test_send_firmware_refuses_mtu_below_packet_index_limit():
    """包号只有 1 字节且 0xFF 被末包占用，MTU 再小就必须挡住而不是崩在 struct.pack。"""
    with pytest.raises(ota.OtaError, match="每扇区会超过"):
        asyncio.run(_drive_send(bytes(64), mtu=23))
    assert ota.packet_payload_bytes(ota.min_mtu()) * ota.PACKET_INDEX_LIMIT >= ota.SECTOR_SIZE


def test_sector_packets_raises_instead_of_overflowing_the_index():
    with pytest.raises(ota.OtaError, match="包号"):
        list(ota.sector_packets(0, bytes(ota.SECTOR_SIZE), 8))


def test_upgrade_nodes_end_to_end(monkeypatch):
    fw = bytes((i * 13) & 0xFF for i in range(5000))
    node = ota.OtaNode(kind="semg", suffix="A4:CF:01", address="AA:BB:CC:A4:CF:01", uid="A4:CF:01")
    app_adv = ota.Advert(name="sEMG-ESP32-A4CF01", address="AA:BB:CC:A4:CF:01")
    rcv_adv = ota.Advert(name="nimble-ble-ota", address="AA:BB:CC:A4:CF:01")
    rounds = [[app_adv], [rcv_adv], [rcv_adv], [app_adv]]  # 最后一轮：传完后回到应用固件做校验
    calls: list[int] = []
    clients: list[FakeReceiver] = []

    async def fake_discover(timeout, stop_when=None):
        found = rounds[min(len(calls), len(rounds) - 1)]
        calls.append(len(calls))
        return found

    def fake_client(address, timeout=30.0):
        client = FakeReceiver(address, timeout)
        clients.append(client)
        return client

    monkeypatch.setattr(ota, "_discover", fake_discover)
    monkeypatch.setattr(ota, "_bleak", lambda: (fake_client, object()))

    log: list[str] = []
    opts = ota.OtaOptions(reboot_wait=0.0, ack_timeout=1.0, scan_timeout=0.01, settle_seconds=0.0,
                          verify_deadline=1.0)
    # upgrade_nodes 自己起事件循环，这里必须同步调用，不能再套一层 asyncio.run
    results = ota.upgrade_nodes([(node, fw)], opts, _quiet_reporter(log))
    assert [err for _, err in results] == [""]
    assert "state:A4:CF:01:完成" in log
    # 阶段1 的触发字写在应用固件那条连接上，固件字节只进接收固件的连接
    assert any(char == ota.TRIGGER_CHAR and data == ota.TRIGGER_MAGIC
               for char, data in clients[0].writes)
    assert clients[0].firmware() == b""
    assert clients[1].firmware() == fw


# ── 界面 ─────────────────────────────────────────────────────────────────────


@pytest.fixture()
def cfg():
    from semg_desktop.config import load_config

    return load_config(None)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_ota_view_lists_gateway_nodes_and_firmware_defaults(qapp, cfg, tmp_path):
    from semg_desktop.ui.ota_view import OtaView

    fw = tmp_path / "bleprph.bin"
    fw.write_bytes(b"\xe9" + bytes(1000))
    cfg.ota.default_bins = {"semg": str(fw), "imu": ""}
    view = OtaView(cfg)
    view.show()
    qapp.processEvents()

    view.set_devices([
        {"kind": "semg", "mac": "A4:CF:01", "full_mac": "AA:BB:CC:A4:CF:01", "label": "CH1"},
        {"kind": "imu", "mac": "12:34:90", "full_mac": "AA:BB:CC:12:34:90", "label": "IMU 12:34:90"},
    ])
    assert set(view.rows) == {"A4:CF:01", "12:34:90"}
    assert view.tbl.rowCount() == 2
    assert view.firmware_for(view.rows["A4:CF:01"]) == str(fw)
    assert view.firmware_for(view.rows["12:34:90"]) == ""

    # IMU 没选固件 -> 挡住并开始不了；EMG 就绪
    rows = [view.rows["A4:CF:01"], view.rows["12:34:90"]]
    problems, jobs = view._build_jobs(rows)
    assert len(jobs) == 1 and problems and "没有指定固件" in problems[0]
    view.close()
    _KEEP.append(view)


def test_ota_view_merges_scan_into_same_row(qapp, cfg):
    from semg_desktop import ota as ota_mod
    from semg_desktop.ui.ota_view import OtaView

    view = OtaView(cfg)
    view.set_devices([{"kind": "semg", "mac": "A4:CF:01", "full_mac": "AA:BB:CC:A4:CF:01",
                       "label": "CH1"}])
    view._on_scanned([ota_mod.Advert(name="sEMG-ESP32-A4CF01", address="AA:BB:CC:A4:CF:01")])
    assert len(view.rows) == 1
    assert "扫描" in view.rows["A4:CF:01"].source

    # 只剩接收固件在广播的一块板：仍归到同一行，并标出"接收中"
    view._on_scanned([ota_mod.Advert(name="nimble-ble-ota", address="AA:BB:CC:DE:AD:BE")])
    assert len(view.rows) == 2
    stuck = view.rows["DE:AD:BE"]
    assert stuck.receiver and stuck.kind == ""
    assert "接收中" in view.tbl.item(view._index["DE:AD:BE"], 1).text()
    problems, jobs = view._build_jobs([stuck])
    assert not jobs and "没有指定固件" in problems[0]
    view.close()
    _KEEP.append(view)


def test_ota_view_threaded_scan_and_upgrade(qapp, cfg, tmp_path, monkeypatch):
    """扫描与升级都走后台线程，这里验证线程->信号->界面这条回填链路不依赖真实蓝牙。"""
    from semg_desktop.ui import ota_view
    from semg_desktop.ui.ota_view import OtaView

    fw = tmp_path / "bleprph.bin"
    fw.write_bytes(b"\xe9" + bytes(2048))
    cfg.ota.default_bins = {"semg": str(fw), "imu": ""}
    monkeypatch.setattr(ota, "scan_adverts",
                        lambda timeout=8.0: [ota.Advert(name="sEMG-ESP32-A4CF01",
                                                         address="AA:BB:CC:A4:CF:01")])

    def fake_upgrade(jobs, opts, rep):
        node = jobs[0][0]
        rep.state(node.key, "传输中")
        rep.progress(1024, 2048)
        rep.state(node.key, "完成")
        return [(node, "")]

    monkeypatch.setattr(ota, "upgrade_nodes", fake_upgrade)
    monkeypatch.setattr(ota_view.QMessageBox, "question",
                        staticmethod(lambda *a, **k: ota_view.QMessageBox.Yes))

    view = OtaView(cfg)
    view.start_scan()
    _drain(qapp, view)
    assert "A4:CF:01" in view.rows, view.rows
    assert view.tbl.isEnabled()  # 线程结束后控件重新可用

    view.rows["A4:CF:01"].checked = True
    view._rebuild()
    view.start_upgrade()
    _drain(qapp, view)
    assert view.rows["A4:CF:01"].state == "完成"
    assert view.tbl.item(view._index["A4:CF:01"], 6).text() == "完成"
    assert view.bar.value() == 50  # 1024/2048
    view.close()
    _KEEP.append(view)


def _drain(qapp, view, timeout: float = 5.0) -> None:
    """等后台线程跑完并把排队信号投递到主线程。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if view._job is None:
            qapp.processEvents()
            return
        time.sleep(0.01)
    raise AssertionError("后台任务没在超时内结束")


def test_ota_view_keeps_selection_across_rebuild(qapp, cfg):
    from semg_desktop.ui.ota_view import OtaView

    view = OtaView(cfg)
    view.set_devices([{"kind": "semg", "mac": "A4:CF:01", "full_mac": "AA:BB:CC:A4:CF:01",
                       "label": "CH1"}])
    item = view.tbl.item(0, 0)
    item.setCheckState(Qt.Checked)
    assert view.rows["A4:CF:01"].checked
    view.set_devices([{"kind": "imu", "mac": "12:34:90", "full_mac": "AA:BB:CC:12:34:90",
                       "label": "IMU"}])  # 新设备出现会整表重绘
    assert [r.suffix for r in view._checked_rows()] == ["A4:CF:01"]
    view.close()
    _KEEP.append(view)


_KEEP: list = []
