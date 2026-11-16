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
