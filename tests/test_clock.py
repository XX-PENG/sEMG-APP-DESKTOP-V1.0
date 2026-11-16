import numpy as np
import pytest

from semg_desktop.clock import DeviceClock

P = 0.02


def seq_times(n=5, period=P, start=0.0, jitter=0.0, rng=None):
    out = []
    for i in range(n):
        t = start + i * period
        if jitter:
            t += rng.uniform(-jitter, jitter)
        out.append((1 + i, t))
    return out


def test_perfect_stream_gives_uniform_sample_spacing():
    clk = DeviceClock(nominal_period=P)
    times = []
    for seq, t in seq_times(6):
        times.append(clk.place(seq, 40, t))
    all_t = np.concatenate(times)
    d = np.diff(all_t)
    assert np.all(d > 0)
    assert np.max(np.abs(d - P / 40)) < 1e-9  # 帧内等间隔
    assert np.allclose(all_t[-1], all_t[0] + (len(all_t) - 1) * (P / 40))


def test_arrival_jitter_does_not_wobble_sample_spacing():
    rng = np.random.default_rng(3)
    clk = DeviceClock(nominal_period=P, pll_gain=0.01)
    all_t = []
    for seq, _t in seq_times(400, jitter=0.004, rng=rng):
        all_t.append(clk.place(seq, 40, _t))
    d = np.diff(np.concatenate(all_t))
    # 允许 PLL 跟随造成的轻微漂移，但绝不该出现毫秒量级的到达抖动
    assert np.max(np.abs(d - P / 40)) < 5e-4
    assert clk.frame_period == pytest.approx(P, rel=0.05)


def test_seq_gap_leaves_time_gap_and_counts_loss():
    clk = DeviceClock(nominal_period=P)
    for seq, t in seq_times(3):
        clk.place(seq, 40, t)
    t_before = clk.latest_time
    # seq 从 3 跳到 7（丢了 3 帧），到达时刻也相应延后
    clk.place(7, 40, 0.0 + 6 * P)
    t_after = clk.latest_time
    assert (t_after - t_before) == pytest.approx((7 - 3) * P - P / 40, rel=0.02)
    assert clk.stats.gaps == 1
    assert clk.stats.lost_frames == 3


def test_uint16_wraparound():
    clk = DeviceClock(nominal_period=P)
    clk.place(65534, 40, 0.0)
    clk.place(65535, 40, P)
    t = clk.place(0, 40, 2 * P)
    assert t[0] > 0
    assert clk.stats.gaps == 0
    assert clk.stats.lost_frames == 0


def test_large_jump_triggers_resync_not_gap_accumulation():
    clk = DeviceClock(nominal_period=P, max_gap=256)
    clk.place(100, 40, 0.0)
    clk.place(101, 40, P)
    clk.place(50_000, 40, 30.0)
    assert clk.stats.resyncs == 1
    assert clk.stats.lost_frames == 0


def test_clock_skew_is_absorbed_by_pll():
    clk = DeviceClock(nominal_period=P, pll_gain=0.05)
    drift = 2e-5  # 每帧 20 us 的晶振偏差
    t = 0.0
    for i in range(600):
        seq = 1 + i
        clk.place(seq, 40, t)
        t += P + drift
    err = abs(clk.latest_time - (t - P / 40))
    assert err < 0.01  # 长时间累积的时标不脱开


def test_duplicate_seq_is_counted_and_stays_non_decreasing():
    clk = DeviceClock(nominal_period=P)
    a = clk.place(5, 40, 0.1)
    b = clk.place(5, 40, 0.12)
    assert clk.stats.duplicates == 1
    assert np.all(np.diff(b) > 0)
    assert b[0] >= a[0] - 1e-9


def test_sample_rate_estimate():
    clk = DeviceClock(nominal_period=P)
    for seq, t in seq_times(30):
        clk.place(seq, 40, t)
    assert clk.sample_rate(40) == pytest.approx(2000.0, rel=0.03)


def test_bursty_arrival_still_estimates_true_period():
    """网关按槽位轮转打印：同一通道的帧会成串到达再空一段。

    这是接上真实蓝牙网关后实测到的行为（逐帧间隔中位数被拉到 27-38 ms，真值 20 ms），
    长基线平均必须把它压回真值。
    """
    clk = DeviceClock(nominal_period=P)
    t = 0.0
    seq = 0
    for _burst in range(60):
        for _ in range(3):  # 3 帧挤在一起
            seq += 1
            clk.place(seq & 0xFFFF, 40, t)
            t += 0.0004
        t += 0.0588  # 空一段：3 帧真实耗时 60 ms
    assert clk.frame_period == pytest.approx(P, rel=0.02)


def test_arrival_stall_reanchors_without_distorting_period():
    """到达整体停 5 s（seq 正常步进）：PLL 重锚一次，长基线周期估计不受污染。"""
    clk = DeviceClock(nominal_period=P)
    t = 0.0
    for seq in range(1, 151):
        clk.place(seq, 40, t)
        t += P
    t += 5.0
    for seq in range(151, 300):
        clk.place(seq, 40, t)
        t += P
    assert clk.stats.jumps == 1
    assert clk.stats.lost_frames == 0
    assert clk.frame_period == pytest.approx(P, rel=0.02)


def test_seq_restart_is_not_counted_as_thousands_of_lost_frames():
    """节点重启后 seq 归零，uint16 回绕会把它读成 +6976 这种巨大步进。

    实测遇到过：8 秒窗口里一路 sEMG 出现 6975 的“缺口帧”，但按 50 帧/s 根本不可能
    丢这么多，真实事件是那台板子重启了。
    """
    clk = DeviceClock(nominal_period=P, max_gap=256)
    t = 0.0
    for seq in range(58555, 58561):
        clk.place(seq, 40, t)
        t += P
    before = clk.latest_time
    after = clk.place(1, 40, t)  # 重启：seq 回到 1
    assert clk.stats.resyncs == 1
    assert clk.stats.gaps == 0
    assert clk.stats.lost_frames == 0
    # 设备时间只前进一帧（帧尾到下一帧头差 P/40），不能被 6976 帧撑出 139 秒空洞
    assert after[0] - before == pytest.approx(P / 40, rel=0.05)


def test_zero_samples_returns_empty():
    clk = DeviceClock()
    assert clk.place(1, 0, 0.0).size == 0
