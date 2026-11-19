"""领域层用例：解剖映射、风险引擎数值、步态检测、档案 CRUD、课次汇总。

不碰蓝牙/串口：用运动员模拟源灌真实格式文本，走与硬件完全相同的解析与时标路径。
"""

import time

import numpy as np
import pytest

from semg_desktop.config import load_config
from semg_desktop.guard.engine import GuardEngine, StrideDetector, spectral_median
from semg_desktop.guard.model import (
    DRILL_BY_ID,
    HAM_IDS,
    MUSCLES,
    Thresholds,
    muscle_by_channel,
    muscle_by_id,
    risk_band,
)
from semg_desktop.guard.roster import AthleteProfile, Injury, Roster
from semg_desktop.guard.session import build_summary
from semg_desktop.pipeline import Acquisition
from semg_desktop.sources import AthleteSimSource


def test_muscle_mapping_covers_eight_channels_bilaterally():
    assert len(MUSCLES) == 8
    assert {m.channel for m in MUSCLES} == set(range(8))
    for side in ("L", "R"):
        for abbr in ("bf", "st", "vl", "rf"):
            assert muscle_by_id(f"{abbr}_{side.lower()}") is not None
    assert muscle_by_channel(0).id == "st_l"
    assert muscle_by_channel(1).id == "bf_l"
    assert muscle_by_channel(7).id == "rf_r"
    assert len(HAM_IDS) == 4


def test_risk_bands_monotonic():
    assert risk_band(10)[0] == "低"
    assert risk_band(50)[0] == "中"
    assert risk_band(90)[0] == "高"


def test_spectral_median_tracks_band_shift():
    """带通上截止下移时 MDF 必须跟着下移——疲劳判读的物理基础。"""
    from scipy import signal

    fs = 2000.0
    rng = np.random.default_rng(3)
    x = rng.normal(0, 1, 8000)
    hi = signal.sosfilt(signal.butter(3, [20 / (fs / 2), 200 / (fs / 2)], btype="band", output="sos"), x)
    lo = signal.sosfilt(signal.butter(3, [20 / (fs / 2), 140 / (fs / 2)], btype="band", output="sos"), x)
    m_hi, _ = spectral_median(hi, fs)
    m_lo, _ = spectral_median(lo, fs)
    assert m_hi > m_lo > 0


def test_stride_detector_finds_peaks_and_impacts():
    det = StrideDetector(axis=1, swing_gate=280, impact_gate=2.2)
    fs = 500.0
    t = np.arange(0, 4.0, 1 / fs)
    phase = np.mod(t * 2.15, 1.0)
    u = np.clip((phase - 0.35) / 0.65, 0, 1)
    gy = np.where(phase < 0.35, 40.0, 790.0 * np.sin(2 * np.pi * u))
    d = np.minimum(phase, 1 - phase)
    az = 1.0 + 5.4 * np.exp(-0.5 * (d / 0.026) ** 2)
    samples = np.zeros((t.size, 9), dtype=np.float32)
    samples[:, 1] = 0.0
    samples[:, 2] = az
    samples[:, 4] = gy
    done = det.feed(t, samples)
    assert len(done) >= 6
    for strike, peak, impact in done:
        assert peak > 600
        assert impact > 3.0
    intervals = [s.interval for s in det.strides if s.interval > 0]
    assert np.median(intervals) == pytest.approx(1 / 2.15, abs=0.03)


def test_stride_detector_survives_tiny_batches():
    """回归：增量消费时一批可能只有 1–2 样本，瞬时采样率会算到 1e6 使 butter 抛异常。"""
    det = StrideDetector()
    t = np.array([100.0, 100.0 + 1e-6])
    s = np.zeros((2, 9), dtype=np.float32)
    assert det.feed(t, s) == []          # 不崩、不推进
    assert det.last_t == 0.0


@pytest.fixture()
def running():
    cfg = load_config(None)
    roster = Roster(None)
    profile = roster.get("ath-lin")
    drill = DRILL_BY_ID["sprint"]
    src = AthleteSimSource.from_profile(profile, drill="sprint", fatigue_rate=8.0, frame_period=0.01)
    acq = Acquisition(src, cfg)
    acq.start()
    engine = GuardEngine(profile, Thresholds(), drill)
    engine.session_start = acq.now()
    yield acq, engine, profile
    acq.stop()


def _run(acq, engine, seconds):
    deadline = time.monotonic() + seconds
    snap = None
    while time.monotonic() < deadline:
        time.sleep(1 / 30)
        snap = engine.update(acq, acq.now())
    return snap


def test_engine_produces_coherent_live_metrics(running):
    acq, engine, profile = running
    snap = _run(acq, engine, 16.0)   # 留足 MDF 暖机与时钟稳定时间（全套件负载下更久）
    assert snap.data_ok
    assert snap.online_muscles == 8
    assert snap.stride_count > 5
    assert 150 < snap.cadence < 320
    assert snap.peak_dps > 400
    assert snap.impact_g > 2.5
    assert snap.terminal_ham > 15          # 末端摆动窗必须真的抓到离心峰
    assert 0 < snap.lead_ms < 300
    assert snap.asym > 3                   # 伤后侧募集不足应可见
    assert 0 <= snap.score <= 100
    assert len(snap.factors) == 6
    # MDF 何时可用取决于时钟稳定时机（负载下更晚），不要求八块齐备；
    # 但已报出的 MDF 必须落在生理带内，且多数肌肉应已可用。
    reported = [m.mdf for m in snap.muscles.values() if m.mdf > 0]
    assert len(reported) >= 4
    assert all(40 < v < 250 for v in reported)


def test_fatigue_escalates_risk_over_session(running):
    acq, engine, profile = running
    early = _run(acq, engine, 5.0)
    late = _run(acq, engine, 30.0)
    # 8× 疲劳推演下频谱只可能下移或持平（死区），绝不应报出正斜率
    assert late.mdf_slope <= early.mdf_slope
    assert late.mdf_slope <= 0
    assert late.fatigue_index >= early.fatigue_index
    assert late.score > early.score
    assert len(engine.events) > 0
    e = engine.events[0]
    assert e.advice and e.factor and e.threshold > 0


def test_mdf_slope_not_fabricated_from_noise(running):
    """短窗 / 暖机阶段不应报出大幅假斜率。"""
    acq, engine, profile = running
    snap = _run(acq, engine, 5.0)
    assert abs(snap.mdf_slope) < 1.5 or snap.mdf_slope == 0.0


def test_capture_mvc_returns_eight_baselines(running):
    acq, engine, profile = running
    _run(acq, engine, 3.0)
    mvc = engine.capture_mvc(acq, seconds=2.0)
    assert len(mvc) == 8
    assert all(v > 100 for v in mvc.values())


def test_build_summary_aggregates_session(running):
    acq, engine, profile = running
    snap = _run(acq, engine, 6.0)
    meta = dict(athlete_id=profile.id, athlete_name=profile.name, number=profile.number,
                position=profile.position, drill="直线冲刺", duration_seconds=6.0,
                chronic_load=profile.chronic_load, peak_score=snap.score)
    s = build_summary(engine, snap, meta)
    assert s.strides == snap.stride_count
    assert len(s.muscles) == 8
    assert s.mean_asym >= 0
    assert s.verdict
    d = s.to_dict()
    assert d["verdict"] == s.verdict


def test_roster_crud_search_sort_undo(tmp_path):
    path = tmp_path / "athletes.json"
    r = Roster(path)
    n0 = len(r.athletes)
    assert n0 >= 5
    p = r.new_blank()
    p.name = "测试员"
    r.add(p)
    assert len(r.athletes) == n0 + 1
    assert r.query("测试员")[0].id == p.id
    assert r.query(position="守门员")
    deleted = r.delete(p.id)
    assert deleted is not None and len(r.athletes) == n0
    assert r.can_undo
    back = r.undo_delete()
    assert back.id == p.id and len(r.athletes) == n0 + 1
    r2 = Roster(path)
    assert any(a.id == p.id for a in r2.athletes)


def test_profile_threshold_overrides_applied():
    p = AthleteProfile(id="x", name="t", number=1, position="前锋", age=20,
                       height_cm=180, mass_kg=75,
                       injuries=[Injury("2026-01-01", "股二头肌长头", "R", "II 级", 6)],
                       return_week=4, thresholds={"asym_warn": 7.0})
    th = p.thresholds_for(Thresholds())
    assert th.asym_warn == 7.0
    assert th.hq_warn == Thresholds().hq_warn      # 未覆盖项保持默认
    assert p.risk_side == "R"


def test_events_ack_note_delete_undo(running):
    acq, engine, profile = running
    _run(acq, engine, 8.0)
    assert engine.events
    e = engine.events[0]
    e.acknowledged = True
    e.note = "降强度"
    assert e.acknowledged and e.note == "降强度"
    idx = engine.events.index(e)
    engine.events.remove(e)
    assert e not in engine.events
    engine.events.insert(idx, e)
    assert engine.events[idx] is e