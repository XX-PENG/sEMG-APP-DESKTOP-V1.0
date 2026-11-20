import pytest

from semg_desktop.protocol import GatewayParser, ParserStats

SEMG_LINE = (
    "3/9 semg 7C:DF:A1:A4:CF:01 seq=1234 cnt=8 drop=0 101 -98 105 110 -108 100 96 99"
)
IMU_HEAD = "9/9 imu 7C:DF:A1:12:34:90 seq=57 cnt=2 drop=1"
IMU_ROW1 = "0.012 -0.004 0.998 1.200 -0.300 0.000 123 -45 68"
IMU_ROW2 = "0.013 -0.005 0.997 1.300 -0.200 0.100 124 -44 68"
CONNECT = "I (1234) GW: Sensor connected: slot=2 conn=3 type=sEMG mac=7C:DF:A1:A4:CF:01"
CONNECT_IMU = "I (2000) GW: Sensor connected: slot=8 conn=8 type=IMU mac=7C:DF:A1:12:34:90"
DISCONNECT = "I (3000) GW: Sensor disconnected: slot=2 conn=3"


def make_parser():
    events, statuses = [], []
    return GatewayParser(events.append, statuses.append), events, statuses


def feed(parser, text: str, t: float = 1.0):
    parser.feed((text + "\n").encode(), t, t)


def test_semg_single_line_frame():
    parser, events, _ = make_parser()
    feed(parser, SEMG_LINE)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "semg" and ev.mac == "7C:DF:A1:A4:CF:01" and ev.slot == 3 and ev.active == 9
    assert ev.seq == 1234 and ev.cnt == 8 and ev.drop == 0
    assert list(ev.values[:3]) == [101.0, -98.0, 105.0]
    assert ev.values.size == 8
    assert parser.stats.semg_short == 0


def test_semg_short_frame_is_counted():
    parser, events, _ = make_parser()
    feed(parser, "1/1 semg 7C:DF:A1:A4:CF:01 seq=1 cnt=40 drop=0 1 2 3")
    assert events[0].values.size == 3
    assert parser.stats.semg_short == 1


def test_semg_more_values_than_cnt_is_counted():
    parser, events, _ = make_parser()
    feed(parser, "1/1 semg 7C:DF:A1:A4:CF:01 seq=1 cnt=2 drop=0 1 2 3 4")
    assert events[0].values.size == 4
    assert parser.stats.semg_overflow_cnt == 1


def test_int16_range_violation_is_counted_and_clipped():
    parser, events, _ = make_parser()
    feed(parser, "1/1 semg 7C:DF:A1:A4:CF:01 seq=1 cnt=2 drop=0 40000 -40000")
    assert parser.stats.bad_token_lines == 1
    assert list(events[0].values) == [32767.0, -32768.0]


def test_imu_multiline_block():
    parser, events, _ = make_parser()
    for line in (IMU_HEAD, IMU_ROW1, IMU_ROW2):
        feed(parser, line)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "imu" and ev.samples.shape == (2, 9)
    assert ev.samples[0, 0] == pytest.approx(0.012)
    assert ev.samples[1, 6] == pytest.approx(124.0)
    assert parser.stats.imu_sample_lines == 2
    assert parser.stats.imu_block_truncated == 0


def test_imu_block_survives_byte_level_split():
    parser, events, _ = make_parser()
    blob = "\n".join((IMU_HEAD, IMU_ROW1, IMU_ROW2)) + "\n"
    raw = blob.encode()
    for i in range(len(raw)):
        parser.feed(raw[i : i + 1], 1.0 + i * 1e-6, 1.0 + i * 1e-6)
    assert len(events) == 1
    assert events[0].samples.shape == (2, 9)


def test_imu_block_truncated_by_next_header():
    parser, events, _ = make_parser()
    feed(parser, IMU_HEAD)
    feed(parser, IMU_ROW1)
    feed(parser, "3/9 semg 7C:DF:A1:A4:CF:01 seq=10 cnt=1 drop=0 5")
    assert [e.kind for e in events] == ["imu", "semg"]
    assert events[0].samples.shape[0] == 1
    assert events[0].truncated is True
    assert parser.stats.imu_block_truncated == 1


def test_interleaved_log_line_does_not_break_imu_block():
    parser, events, _ = make_parser()
    feed(parser, IMU_HEAD)
    feed(parser, "W (99) GW: print queue overflow(total)=17")
    feed(parser, IMU_ROW1)
    feed(parser, IMU_ROW2)
    assert len(events) == 1
    assert events[0].samples.shape == (2, 9)
    assert parser.stats.status_lines == 1


def test_ansi_and_log_prefix_are_stripped():
    parser, events, _ = make_parser()
    colored = "\x1b[0;32mI (\x1b[0;33m402565\x1b[0;32m) GW: \x1b[0m" + SEMG_LINE
    feed(parser, colored)
    assert len(events) == 1
    assert events[0].seq == 1234


def test_unknown_line_counted_as_garbage():
    parser, events, statuses = make_parser()
    feed(parser, "ets Jul 29 2019 r1-gpioenable@0")
    feed(parser, "I (25) esp_ps: SPI RAM enabled")
    assert events == []
    assert parser.stats.garbage_lines == 2


def test_connect_line_marks_device_online_with_full_mac():
    parser, events, statuses = make_parser()
    feed(parser, CONNECT)
    feed(parser, CONNECT_IMU)
    kinds = {d.kind: d for d in parser.devices.values()}
    assert kinds["semg"].full_mac == "7C:DF:A1:A4:CF:01"
    assert kinds["semg"].online is True
    assert kinds["semg"].slot == 3  # 头部行里的 channel_id = slot 索引 + 1
    assert kinds["imu"].key == "imu@7cdfa1123490"
    assert [s.kind for s in statuses] == ["connected", "connected"]


def test_disconnect_line_marks_offline():
    parser, events, statuses = make_parser()
    feed(parser, CONNECT)
    feed(parser, SEMG_LINE)
    feed(parser, DISCONNECT)
    assert parser.devices["semg@7cdfa1a4cf01"].online is False
    assert [s.kind for s in statuses] == ["connected", "disconnected"]


def test_ping_rtt_and_overflow_status_events():
    parser, events, statuses = make_parser()
    feed(parser, "ping_rtt conn=3 us=45120")
    feed(parser, "W (123) GW: print queue overflow(total)=3")
    assert statuses[0].rtt_us == 45120
    assert statuses[1].overflow == 3


def test_arrival_time_is_interpolated_within_chunk():
    parser, events, _ = make_parser()
    blob = (SEMG_LINE + "\n") * 3
    parser.feed(blob.encode(), 10.0, 10.1)
    ts = [e.t_arrival for e in events]
    assert ts[0] < ts[1] < ts[2] <= 10.1
    assert ts[2] == pytest.approx(10.1, abs=0.02)


def test_oversize_buffer_is_resynced():
    parser, events, _ = make_parser()
    parser.feed(b"x" * 4000, 1.0, 1.0)
    assert parser.stats.oversize_lines >= 1
    feed(parser, SEMG_LINE)
    assert len(events) == 1


def test_carriage_returns_tolerated():
    parser, events, _ = make_parser()
    parser.feed((SEMG_LINE + "\r\n" + IMU_HEAD + "\r\n" + IMU_ROW1 + "\r\n" + IMU_ROW2 + "\r\n").encode(), 1.0, 1.0)
    assert [e.kind for e in events] == ["semg", "imu"]


def test_stats_starts_empty():
    assert ParserStats().parse_errors == 0
