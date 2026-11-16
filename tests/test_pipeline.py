import gzip
from pathlib import Path

import numpy as np
import pytest

from semg_desktop.config import load_config
from semg_desktop.pipeline import Acquisition, ChannelMap
from semg_desktop.recorder import IMU_FIELDS, Recording, export_csv
from semg_desktop.sources import ReplaySource, SimulateSource, SourceError

FRAMES = 50


@pytest.fixture()
def sim_file(tmp_path):
    path = tmp_path / "sim.log"
    path.write_bytes(SimulateSource().render(FRAMES))
    return path


def drain(acq: Acquisition, max_bytes: int = 65536) -> int:
    total = 0
    while True:
        try:
            got = acq.pump(max_bytes)
        except SourceError:
            return total
        total += got
        if got == 0:
            return total


def test_pipeline_discovers_eight_semg_and_one_imu(sim_file):
    cfg = load_config(None)
    acq = Acquisition(ReplaySource(sim_file, pace=False), cfg)
    acq.source.open()
    drain(acq)
    rows = acq.device_rows()
    semg = [r for r in rows if r["kind"] == "semg"]
    imu = [r for r in rows if r["kind"] == "imu"]
    assert len(semg) == 8 and len(imu) == 1
    assert sorted(r["index"] for r in semg) == list(range(8))
    assert imu[0]["index"] is None
    assert all(r["samples"] == FRAMES * 40 for r in semg)
    assert imu[0]["samples"] == FRAMES * 10
    assert acq.parser.stats.parse_errors == 0


def test_unpaced_replay_falls_back_to_nominal_rate(sim_file):
    acq = Acquisition(ReplaySource(sim_file, pace=False), load_config(None))
    acq.source.open()
    drain(acq)
    rows = [r for r in acq.device_rows() if r["kind"] == "semg"]
    assert rows[0]["measured_period"] == pytest.approx(0.02, rel=1e-6)
    assert rows[0]["rate"] == pytest.approx(2000.0, rel=0.01)


def test_unpaced_ingest_does_not_compress_timeline(sim_file):
    """全速灌文件时到达时刻几乎不前进，时标必须仍按 seq 铺满，否则窗口查询会失真。"""
    acq = Acquisition(ReplaySource(sim_file, pace=False), load_config(None))
    acq.source.open()
    drain(acq)
    _t_now = acq.now()
    t, v = acq.emg_windows(_t_now + 10.0)[0]
    assert v.size == FRAMES * 40
    assert np.all(np.diff(t) > 0)
    assert t[-1] - t[0] == pytest.approx((FRAMES * 40 - 1) / 2000.0, rel=1e-6)
    assert all(r["forward_skips"] > 0 for r in acq.device_rows())


def test_windows_and_metrics(sim_file):
    acq = Acquisition(ReplaySource(sim_file, pace=False), load_config(None))
    acq.source.open()
    drain(acq)
    t_now = acq.now()
    windows = acq.emg_windows(t_now + 1.0)
    assert len(windows) == 8
    assert all(v.size == FRAMES * 40 for _t, v in windows.values())
    metrics = acq.metrics(t_now + 1.0)
    assert metrics[0].count == FRAMES * 40
    assert metrics[0].rate == pytest.approx(2000.0, rel=0.01)

    t_imu, samples = acq.imu_window(t_now + 1.0)
    assert samples.shape == (FRAMES * 10, 9)
    latest = acq.imu_latest()
    assert latest is not None and latest.size == 9
    assert np.allclose(latest[:3], samples[-1][:3])


def test_seq_gap_is_reported_as_lost_frames(sim_file, tmp_path):
    text = (
        "1/9 semg A4:CF:01 seq=100 cnt=40 drop=0 " + " ".join(["7"] * 40) + "\n"
        "1/9 semg A4:CF:01 seq=101 cnt=40 drop=0 " + " ".join(["7"] * 40) + "\n"
        "1/9 semg A4:CF:01 seq=106 cnt=40 drop=250 " + " ".join(["7"] * 40) + "\n"
    )
    path = tmp_path / "gap.log"
    path.write_text(text)
    acq = Acquisition(ReplaySource(path, pace=False), load_config(None))
    acq.source.open()
    drain(acq)
    row = next(r for r in acq.device_rows() if r["kind"] == "semg")
    assert row["seq_gaps"] == 4
    assert row["lost_frames"] == 4
    assert row["drop_total"] == 250
    assert row["restarts"] == 0


def test_restart_counted_separately_from_frame_gaps(tmp_path):
    """seq 从 58560 归零：这是节点重启，不能报成丢了 6976 帧。"""
    body = " ".join(["7"] * 40)
    text = (
        f"1/9 semg A4:CF:01 seq=58560 cnt=40 drop=0 {body}\n"
        f"1/9 semg A4:CF:01 seq=1 cnt=40 drop=0 {body}\n"
        f"1/9 semg A4:CF:01 seq=2 cnt=40 drop=0 {body}\n"
    )
    path = tmp_path / "restart.log"
    path.write_text(text)
    acq = Acquisition(ReplaySource(path, pace=False), load_config(None))
    acq.source.open()
    drain(acq)
    row = next(r for r in acq.device_rows() if r["kind"] == "semg")
    assert row["restarts"] == 1
    assert row["seq_gaps"] == 0
    assert row["samples"] == 120


def test_channel_map_is_stable_across_sessions(sim_file):
    cfg = load_config(None)
    first = Acquisition(ReplaySource(sim_file, pace=False), cfg)
    first.source.open()
    drain(first)
    mapping = first.channel_map.as_dict()
    assert len(mapping) == 8

    second = Acquisition(ReplaySource(sim_file, pace=False), cfg, ChannelMap(mapping))
    second.source.open()
    drain(second)
    rows = {r["mac"].replace(":", "").lower(): r["index"] for r in second.device_rows() if r["kind"] == "semg"}
    assert rows == {k.split("@")[1]: v for k, v in mapping.items()}


def test_recording_and_replay_roundtrip(sim_file, tmp_path):
    cfg = load_config(None)
    acq = Acquisition(ReplaySource(sim_file, pace=False), cfg)
    acq.source.open()
    rec = Recording(root=tmp_path / "session", source_description="pytest", config={"x": 1})
    acq.start_recording(rec)
    drain(acq)
    files = acq.stop_recording()
    assert rec.bytes_written == sim_file.stat().st_size
    names = {Path(p).name for p in files}
    assert {"raw.log.gz", "manifest.json", "data.npz"} <= names

    data = np.load(tmp_path / "session" / "parsed" / "data.npz")
    semg_keys = [k for k in data.files if k.startswith("semg@")]
    assert len(semg_keys) == 8
    assert data[semg_keys[0]].size == FRAMES * 40
    imu_field_keys = [k for k in data.files if k.split("@")[0] in IMU_FIELDS]
    assert len(imu_field_keys) == 9

    with gzip.open(tmp_path / "session" / "raw.log.gz", "rb") as fh:
        assert fh.read() == sim_file.read_bytes()

    replay = Acquisition(ReplaySource(tmp_path / "session" / "raw.log.gz", pace=False), load_config(None))
    replay.source.open()
    drain(replay)
    again = {r["mac"]: r["samples"] for r in replay.device_rows()}
    assert again == {r["mac"]: r["samples"] for r in acq.device_rows()}


def test_export_csv_from_session(sim_file, tmp_path):
    acq = Acquisition(ReplaySource(sim_file, pace=False), load_config(None))
    acq.source.open()
    rec = Recording(root=tmp_path / "session2", source_description="pytest")
    acq.start_recording(rec)
    drain(acq)
    acq.stop_recording()
    out = export_csv(tmp_path / "session2")
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "t_seconds,device_mac_suffix,signal,value"
    assert len(lines) - 1 == FRAMES * 40 * 8 + FRAMES * 10 * 9


def test_source_error_stops_cleanly(tmp_path):
    empty = tmp_path / "empty.log"
    empty.write_bytes(b"")
    acq = Acquisition(ReplaySource(empty, pace=False), load_config(None))
    acq.source.open()
    with pytest.raises(SourceError):
        acq.pump(1024)
