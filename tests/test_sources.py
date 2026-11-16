import gzip

import pytest

from semg_desktop.protocol import HEADER_RE, GatewayParser
from semg_desktop.sources import ReplaySource, SerialSource, SimulateSource, SourceError, TcpSource


def collect(text: bytes):
    events = []
    parser = GatewayParser(events.append)
    parser.feed(text, 1.0, 1.0)
    parser.close()
    return events, parser


def test_simulator_output_matches_gateway_line_format():
    src = SimulateSource()
    src.open()
    handshake, _t0, _t1 = src.read(4096)
    assert b"Sensor connected: slot=0 conn=0 type=sEMG" in handshake
    assert b"type=IMU" in src.read(4096)[0] or b"type=IMU" in handshake
    blob = src.render(3)
    lines = blob.decode().splitlines()
    headers = [ln for ln in lines if HEADER_RE.match(ln)]
    assert len(headers) == 9 * 3  # 每帧 8 个 sEMG 头部行 + 1 个 IMU 头部行
    kinds = [HEADER_RE.match(ln).group("kind") for ln in headers]
    assert kinds.count("semg") == 24 and kinds.count("imu") == 3


def test_simulator_frame_content_counts():
    events, parser = collect(SimulateSource().render(5))
    semg = [e for e in events if e.kind == "semg"]
    imu = [e for e in events if e.kind == "imu"]
    assert len(semg) == 40 and len(imu) == 5
    assert all(e.values.size == 40 for e in semg)
    assert all(e.samples.shape == (10, 9) for e in imu)
    assert parser.stats.parse_errors == 0
    assert parser.stats.imu_sample_lines == 50


def test_simulator_imu_values_are_in_physical_range():
    events, _ = collect(SimulateSource().render(2))
    imu = next(e for e in events if e.kind == "imu")
    assert abs(imu.samples[:, 2]).max() < 2.0  # az 约 1 g
    assert abs(imu.samples[:, 3:6]).max() < 10.0


def test_replay_source_reads_gzip(tmp_path):
    blob = SimulateSource().render(2)
    path = tmp_path / "raw.log.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(blob)
    src = ReplaySource(path, pace=False)
    src.open()
    got = b""
    while True:
        try:
            chunk, _a, _b = src.read(256)
        except SourceError:
            break
        got += chunk
    assert got == blob
    assert src.progress == 1.0


def test_replay_source_missing_file_raises(tmp_path):
    src = ReplaySource(tmp_path / "nope.log")
    with pytest.raises(SourceError):
        src.open()


def test_serial_source_bad_port_raises():
    src = SerialSource("COM-not-exists-12345")
    with pytest.raises(SourceError):
        src.open()


def test_tcp_source_refused_raises():
    src = TcpSource("127.0.0.1", 1, connect_timeout=0.5)
    with pytest.raises(SourceError):
        src.open()


def test_tcp_source_streams_gateway_text(tmp_path):
    """本地起一个假 WIFI 网关：连上就把原始文本推过去，验证 TCP 源与解析链路真的能对接。"""
    import socket
    import threading

    blob = SimulateSource().render(4)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        with conn:
            conn.sendall(blob)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    src = TcpSource("127.0.0.1", port)
    src.open()
    got = b""
    while len(got) < len(blob):
        chunk, _t0, _t1 = src.read(1024)
        got += chunk
    src.close()
    server.close()
    thread.join(timeout=1.0)
    assert got == blob
    events, parser = collect(got)
    assert len([e for e in events if e.kind == "semg"]) == 32
    assert parser.stats.parse_errors == 0


def test_tcp_source_capacity_is_uart1_bound():
    assert TcpSource("127.0.0.1").capacity_bytes_per_second == 11_520
