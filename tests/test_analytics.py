import numpy as np
import pytest

from semg_desktop.analytics import EnvelopeEstimator, attitude_deg, channel_metrics, psd_db


def sine(fs, dur, f, amp=1.0, phase=0.0):
    t = np.arange(int(fs * dur)) / fs
    return t, amp * np.sin(2 * np.pi * f * t + phase)


def test_envelope_tracks_rectified_sine_mean():
    fs = 2000.0
    t, x = sine(fs, 2.0, 80.0, amp=1000.0)
    est = EnvelopeEstimator(cutoff_hz=4.0, output_rate_hz=200.0)
    t_env, env = est.update(t, x)
    assert t_env.size > 100
    steady = env[env.size // 3 :]
    expected = 2.0 / np.pi * 1000.0  # 全波整流正弦均值
    assert np.mean(steady[-100:]) == pytest.approx(expected, rel=0.08)


def test_envelope_output_rate_is_decimated():
    fs = 2000.0
    t, x = sine(fs, 1.0, 100.0)
    est = EnvelopeEstimator(cutoff_hz=4.0, output_rate_hz=200.0)
    t_env, env = est.update(t, x)
    dt = np.diff(t_env)
    assert np.median(dt) == pytest.approx(1.0 / 200.0, rel=0.05)


def test_envelope_survives_frame_by_frame_delivery():
    fs = 2000.0
    t, x = sine(fs, 0.5, 90.0, amp=500.0)
    est = EnvelopeEstimator(cutoff_hz=4.0, output_rate_hz=200.0)
    total = 0
    for i in range(0, x.size, 40):  # 20 ms 一帧
        _te, env = est.update(t[i : i + 40], x[i : i + 40])
        total += env.size
    assert total > 80
    assert np.isfinite(total)


def test_metrics_basic_values():
    t, x = sine(2000.0, 1.0, 100.0, amp=2000.0)
    m = channel_metrics(t, x.astype(np.float32))
    assert m.count == x.size
    assert m.rate == pytest.approx(2000.0, rel=0.01)
    assert m.p2p == pytest.approx(4000.0, rel=0.01)
    assert m.rms == pytest.approx(2000.0 / np.sqrt(2), rel=0.02)
    assert m.iemg > 0
    assert m.saturated == 0


def test_metrics_counts_int16_saturation():
    x = np.array([32767.0, -32768.0, 5.0, 6.0])
    t = np.arange(4, dtype=np.float64)
    m = channel_metrics(t, x.astype(np.float32))
    assert m.saturated == 2


def test_metrics_empty():
    m = channel_metrics(np.empty(0), np.empty(0))
    assert m.count == 0 and m.rate == 0.0


def test_psd_peak_at_signal_frequency():
    fs = 2000.0
    rng = np.random.default_rng(1)
    t, x = sine(fs, 2.0, 120.0, amp=1000.0)
    x = x + rng.normal(0, 100, x.size)
    freqs, db = psd_db(x, fs, nperseg=1024)
    band = (freqs > 20) & (freqs < 500)
    peak = freqs[band][np.argmax(db[band])]
    assert peak == pytest.approx(120.0, abs=5.0)


def test_psd_too_short_returns_empty():
    f, p = psd_db(np.zeros(8), 2000.0)
    assert f.size == 0


def test_attitude_flat_is_zero_degrees():
    accel = np.tile(np.array([0.0, 0.0, 1.0], dtype=np.float32), (10, 1))
    roll, pitch = attitude_deg(accel)
    assert roll[-1] == pytest.approx(0.0, abs=1e-3)
    assert pitch[-1] == pytest.approx(0.0, abs=1e-3)


def test_attitude_90_degree_tilt():
    accel = np.tile(np.array([0.0, 1.0, 0.0], dtype=np.float32), (5, 1))
    roll, _pitch = attitude_deg(accel)
    assert roll[-1] == pytest.approx(90.0, abs=1e-2)


def test_attitude_pitch_sign():
    accel = np.tile(np.array([1.0, 0.0, 1.0], dtype=np.float32), (5, 1))
    _roll, pitch = attitude_deg(accel)
    assert pitch[-1] == pytest.approx(-45.0, abs=1e-2)
