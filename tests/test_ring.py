import numpy as np
import pytest

from semg_desktop.ring import SeriesRing


def test_append_and_window():
    r = SeriesRing(100)
    t = np.arange(50, dtype=np.float64)
    v = np.arange(50, dtype=np.float32)
    r.append(t, v)
    tw, vw = r.window(10.0, 20.0)
    assert tw[0] == 10.0 and tw[-1] == 20.0
    assert np.allclose(vw, tw)
    assert len(r) == 50


def test_window_wraps_around_capacity():
    r = SeriesRing(16)
    t = np.arange(30, dtype=np.float64)
    v = np.arange(30, dtype=np.float32) * 2
    r.append(t, v)
    tw, vw = r.window(24.0)
    assert list(tw) == [24.0, 25.0, 26.0, 27.0, 28.0, 29.0]
    assert list(vw) == [48.0, 50.0, 52.0, 54.0, 56.0, 58.0]
    assert r.overwritten == 14


def test_batch_larger_than_capacity_keeps_tail():
    r = SeriesRing(16)
    t = np.arange(100, dtype=np.float64)
    v = np.arange(100, dtype=np.float32)
    r.append(t, v)
    tw, vw = r.window(0.0)
    assert tw[0] == 84.0 and len(tw) == 16
    assert r.overwritten == 84


def test_multi_field_ring():
    r = SeriesRing(32, n_fields=9)
    t = np.arange(4, dtype=np.float64)
    v = np.arange(36, dtype=np.float32).reshape(4, 9)
    r.append(t, v)
    tw, vw = r.window(1.0)
    assert vw.shape == (3, 9)
    assert vw[0, 0] == 9.0


def test_latest_time_and_empty_window():
    r = SeriesRing(16)
    assert r.latest_time == 0.0
    t, v = r.window(0.0)
    assert t.size == 0 and v.size == 0
    r.append(np.arange(4.0), np.arange(4.0, dtype=np.float32))
    assert r.latest_time == 3.0
    t, v = r.window(100.0)
    assert t.size == 0


def test_tail_returns_most_recent():
    r = SeriesRing(16)
    r.append(np.arange(10.0), np.arange(10.0, dtype=np.float32))
    t, v = r.tail(3)
    assert list(t) == [7.0, 8.0, 9.0]


def test_window_matches_full_scan_reference():
    """分段二分定位必须与"全量拷出来再筛选"逐点等价，含跨容量回绕的各种边界。"""
    r = SeriesRing(37, n_fields=3)
    rng = np.random.default_rng(0)
    t_end = 0.0
    for _ in range(25):
        n = int(rng.integers(1, 15))
        times = t_end + np.arange(1, n + 1) * 0.01
        r.append(times, rng.normal(size=(n, 3)).astype(np.float32))
        t_end = float(times[-1])

        tt, vv = r.tail(len(r))
        span = tt[-1] - tt[0]
        for frac in (0.0, 0.25, 0.5, 0.9, 1.0):
            t_from = tt[0] + span * frac
            for t_to in (None, t_from + 0.05, float(tt[-1]), t_from - 1.0):
                mask = tt >= t_from
                if t_to is not None:
                    mask &= tt <= t_to
                tw, vw = r.window(t_from, t_to)
                assert np.array_equal(tw, tt[mask]), (t_from, t_to)
                assert np.array_equal(vw, vv[mask]), (t_from, t_to)
                assert vw.shape == (int(mask.sum()), 3)
