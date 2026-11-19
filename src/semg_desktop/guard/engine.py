"""实时风险引擎：从 sEMG/IMU 快照算出腘绳肌损伤预防量。

分工与采集管线一致——重活按各自的节拍做，不跟着界面帧率跑：
* 激活（包络 / MVC）每帧算，200 Hz 包络窗只有几百点；
* 频谱中位频率每 0.5 s 算一次 Welch，疲劳斜率在 30 s 窗上回归；
* 步态是增量状态机，只消费上次之后的新 IMU 样本，绝不重复检测同一个摆动峰。

风险指数是六个因子严重度的加权和，权重与每个因子的贡献都对外暴露——场边教练要能
回答"为什么现在是 72 分"，而不是接受一个黑箱数字。
"""

from __future__ import annotations

import math
import time
from collections import deque

import numpy as np
from scipy import signal

from .model import (
    FACTOR_WEIGHTS,
    HAM_IDS,
    MUSCLES,
    QUAD_IDS,
    Drill,
    FactorReading,
    GuardSnapshot,
    MuscleReading,
    RiskEvent,
    Stride,
    Thresholds,
    muscle_by_id,
)
from .roster import AthleteProfile

_EPS = 1e-9
_PSD_BAND = (20.0, 450.0)


def _ramp_up(value: float, warn: float, danger: float) -> float:
    """越高越危险：warn 处 0.25，danger 处 1.0，warn 以下留一段软膝。"""
    lo = warn * 0.7
    if value <= lo:
        return 0.0
    if value <= warn:
        return 0.25 * (value - lo) / max(warn - lo, _EPS)
    return 0.25 + 0.75 * min(1.0, (value - warn) / max(danger - warn, _EPS))


def _ramp_down(value: float, warn: float, danger: float) -> float:
    """越低越危险（H:Q、激活提前量）。"""
    hi = warn + (warn - danger) * 0.4
    if value >= hi:
        return 0.0
    if value >= warn:
        return 0.25 * (hi - value) / max(hi - warn, _EPS)
    return 0.25 + 0.75 * min(1.0, (warn - value) / max(warn - danger, _EPS))


def spectral_median(x: np.ndarray, fs: float, nperseg: int = 1024) -> tuple[float, float]:
    """sEMG 中位频率与平均频率（Hz），只取 20–450 Hz 生理带宽。

    窗要够长：2 s / nperseg 512 时 MDF 逐次抖动可达 ±10 Hz，疲劳回归会把这种抖动
    读成 ±20%/min 的假趋势。4 s / nperseg 1024 能平均到 7 段，估计才站得住。
    """
    if x.size < 64 or fs <= 0:
        return 0.0, 0.0
    nperseg = int(min(nperseg, x.size))
    freqs, p = signal.welch(x, fs=fs, window="hann", nperseg=nperseg, detrend="constant")
    band = (freqs >= _PSD_BAND[0]) & (freqs <= min(_PSD_BAND[1], fs / 2 - 1))
    f, pw = freqs[band], p[band]
    if f.size < 4 or pw.sum() <= 0:
        return 0.0, 0.0
    cum = np.cumsum(pw)
    mdf = float(np.interp(cum[-1] / 2.0, cum, f))
    mnf = float(np.sum(f * pw) / np.sum(pw))
    return mdf, mnf


class StrideDetector:
    """单侧大腿 IMU 的步态状态机。

    摆动峰（矢状面角速度正向峰）→ 触地冲击（加速度模长峰）配成一步。末端摆动窗取
    触地前 140 ms，这正是腘绳肌离心负荷最大、拉伤最集中发生的时相。
    """

    def __init__(self, axis: int = 1, swing_gate: float = 280.0, impact_gate: float = 2.2) -> None:
        self.axis = axis
        self.swing_gate = swing_gate
        self.impact_gate = impact_gate
        self.strides: deque[Stride] = deque(maxlen=400)
        self.last_t = 0.0
        self.phase = 0.0
        self.stance_ratio = 0.35
        self.stride_count = 0
        self._sos: np.ndarray | None = None
        self._zi: np.ndarray | None = None
        self._fs = 0.0
        self._in_peak = False
        self._peak_val = 0.0
        self._peak_t = 0.0
        self._last_peak_t = -10.0
        self._pending: tuple[float, float] | None = None
        self._in_impact = False
        self._impact_val = 0.0
        self._impact_t = 0.0
        self._impact_done: tuple[float, float] | None = None
        self._last_strike: float | None = None
        self._stance_acc = 0.0
        self._span_acc = 0.0
        self._last_sample_t: float | None = None

    def reset(self) -> None:
        self.strides.clear()
        self.last_t = 0.0
        self.phase = 0.0
        self.stride_count = 0
        self._sos = None
        self._zi = None
        self._fs = 0.0
        self._in_peak = False
        self._pending = None
        self._in_impact = False
        self._impact_val = 0.0
        self._impact_done = None
        self._last_strike = None
        self._last_peak_t = -10.0
        self._last_sample_t = None
        self._stance_acc = 0.0
        self._span_acc = 0.0

    def feed(self, t: np.ndarray, samples: np.ndarray) -> list[tuple[float, float, float]]:
        """返回本批新完成的步 (触地时刻, 摆动峰 dps, 冲击 g)。"""
        if t.size < 4 or samples.shape[0] != t.size:
            return []
        span = float(t[-1] - t[0])
        if span <= 0:
            return []
        fs = (t.size - 1) / span
        # 增量消费时一批可能只有一两个样本，瞬时"采样率"会算到 1e6，据此设计低通会把
        # 极点推到 z=1（scipy 直接抛 ValueError）。 rate 不可信就沿用已建好的滤波器。
        if 20.0 <= fs <= 20000.0 and (self._sos is None or abs(fs - self._fs) > 0.1 * self._fs):
            wn = float(np.clip(min(14.0, fs / 2 - 1) / (fs / 2), 0.002, 0.99))
            self._sos = signal.butter(2, wn, btype="low", output="sos")
            self._zi = signal.sosfilt_zi(self._sos) * 0.0
            self._fs = fs
        if self._sos is None:
            return []
        axis = min(max(self.axis, 0), 2)
        gyro = samples[:, 3 + axis].astype(np.float64)
        smooth, self._zi = signal.sosfilt(self._sos, gyro, zi=self._zi)
        accel = np.sqrt(np.sum(np.square(samples[:, 0:3].astype(np.float64)), axis=1))

        done: list[tuple[float, float, float]] = []
        for i in range(t.size):
            ti, v, a = float(t[i]), float(smooth[i]), float(accel[i])
            dt = ti - self._last_sample_t if self._last_sample_t is not None else 0.0
            self._last_sample_t = ti
            if 0 < dt < 0.5:
                self._span_acc += dt
                if abs(v) < 90.0:
                    self._stance_acc += dt

            if v > self.swing_gate:
                if not self._in_peak or v > self._peak_val:
                    self._peak_val, self._peak_t = v, ti
                self._in_peak = True
            elif self._in_peak:
                self._in_peak = False
                if ti - self._last_peak_t > 0.24:
                    self._pending = (self._peak_t, self._peak_val)
                    self._last_peak_t = self._peak_t
                self._peak_val = 0.0

            # 触地取冲击峰而不是过门限的那一刻：门限交叉点比真实触地早约 20 ms，
            # 末端摆动窗只有 140 ms，这点偏差足以把离心负荷峰值整段错过去。
            if a > self.impact_gate:
                if not self._in_impact or a > self._impact_val:
                    self._impact_val, self._impact_t = a, ti
                self._in_impact = True
            elif self._in_impact:
                self._in_impact = False
                self._impact_done = (self._impact_t, self._impact_val)
                self._impact_val = 0.0

            if (self._pending is not None and self._impact_done is not None
                    and ti - self._pending[0] > 0.04
                    and (self._last_strike is None or self._impact_done[0] - self._last_strike > 0.24)):
                peak_t, peak_dps = self._pending
                strike_t, impact_g = self._impact_done
                self._pending = None
                self._impact_done = None
                interval = strike_t - self._last_strike if self._last_strike is not None else 0.0
                self._last_strike = strike_t
                self.stride_count += 1
                if self._span_acc > 0.2:
                    self.stance_ratio = float(np.clip(self._stance_acc / self._span_acc, 0.15, 0.75))
                    self._stance_acc = self._span_acc = 0.0
                self.strides.append(Stride(t=strike_t, interval=interval, peak_dps=peak_dps,
                                           impact_g=impact_g, terminal_ham=0.0, lead_ms=0.0, load_index=0.0))
                done.append((strike_t, peak_dps, impact_g))

        self.last_t = float(t[-1])
        if self._last_strike is not None and self.strides:
            recent = [s.interval for s in list(self.strides)[-4:] if s.interval > 0.15]
            expect = float(np.median(recent)) if recent else 0.5
            self.phase = float(np.clip((self.last_t - self._last_strike) / max(expect, 0.2), 0.0, 1.0))
        return done


class GuardEngine:
    def __init__(self, profile: AthleteProfile, thresholds: Thresholds, drill: Drill) -> None:
        self.profile = profile
        self.th = thresholds
        self.drill = drill
        self.detector = StrideDetector(thresholds.sagittal_axis, thresholds.swing_gate_dps,
                                       thresholds.impact_gate_g)
        self.events: list[RiskEvent] = []
        self._event_seq = 0
        self._mdf_hist: dict[str, deque] = {m.id: deque(maxlen=400) for m in MUSCLES}
        self._rms_hist: dict[str, deque] = {m.id: deque(maxlen=400) for m in MUSCLES}
        self._act_hist: deque = deque(maxlen=1200)
        self._act_muscle_hist: dict[str, deque] = {m.id: deque(maxlen=3600) for m in MUSCLES}
        self._sev = {k: 0.0 for k in FACTOR_WEIGHTS}
        self._above_since: dict[str, float | None] = {k: None for k in FACTOR_WEIGHTS}
        self._last_event_t: dict[str, float] = {}
        self._last_psd_t = 0.0
        self._last_tick: float | None = None
        self._dt = 0.033
        self._mdf: dict[str, float] = {}
        self._mnf: dict[str, float] = {}
        self._rms: dict[str, float] = {}
        self._sat: dict[str, int] = {}
        self._mdf_ema: dict[str, float] = {}
        self._mdf_warm: dict[str, list] = {}
        self._act_ema: dict[str, float] = {}
        self._baseline_mdf: dict[str, float] = {}
        self._baseline_rms: dict[str, float] = {}
        self._bout_active = False
        self._bout_start = 0.0
        self._bout_quiet = 0.0
        self.sprint_bouts = 0
        self.high_speed_seconds = 0.0
        self.load_au = 0.0
        self.active_seconds = 0.0
        self.session_start: float | None = None
        self.snap = GuardSnapshot()

    # ── 配置 ────────────────────────────────────────────────────────────────

    def set_profile(self, profile: AthleteProfile) -> None:
        self.profile = profile
        self.reset_metrics()

    def set_drill(self, drill: Drill) -> None:
        self.drill = drill

    def reset_metrics(self) -> None:
        self.detector.reset()
        for d in self._mdf_hist.values():
            d.clear()
        for d in self._rms_hist.values():
            d.clear()
        self._act_hist.clear()
        for d in self._act_muscle_hist.values():
            d.clear()
        self._mdf.clear()
        self._mnf.clear()
        self._rms.clear()
        self._sat.clear()
        self._mdf_ema.clear()
        self._mdf_warm.clear()
        self._act_ema.clear()
        self._baseline_mdf.clear()
        self._baseline_rms.clear()
        self._sev = {k: 0.0 for k in self._sev}
        self._above_since = {k: None for k in self._above_since}
        self._last_psd_t = 0.0
        self._last_tick = None
        self.sprint_bouts = 0
        self.high_speed_seconds = 0.0
        self.load_au = 0.0
        self.active_seconds = 0.0
        self._bout_active = False
        self.snap = GuardSnapshot()

    def reset_events(self) -> None:
        self.events.clear()
        self._event_seq = 0
        self._last_event_t.clear()
        self._above_since = {k: None for k in self._above_since}

    def sync_detector(self) -> None:
        self.detector.axis = self.th.sagittal_axis
        self.detector.swing_gate = self.th.swing_gate_dps
        self.detector.impact_gate = self.th.impact_gate_g

    # ── 主更新 ──────────────────────────────────────────────────────────────

    def update(self, acq, t_now: float) -> GuardSnapshot:
        snap = GuardSnapshot(t=t_now)
        self._dt = 0.033 if self._last_tick is None else min(0.5, max(0.001, t_now - self._last_tick))
        self.sync_detector()
        rows = acq.device_rows()
        online = {r["index"]: r["online"] for r in rows if r["kind"] == "semg" and r["index"] is not None}
        rates = {r["index"]: r["rate"] for r in rows if r["kind"] == "semg" and r["index"] is not None}
        snap.imu_online = any(r["kind"] == "imu" and r["online"] for r in rows)

        span = max(0.2, self.th.window_seconds)
        envs = acq.env_windows(span)
        do_psd = (t_now - self._last_psd_t) >= 0.5
        raws = acq.emg_windows(4.0) if do_psd else {}
        if do_psd:
            self._last_psd_t = t_now

        for m in MUSCLES:
            reading = MuscleReading(id=m.id, online=bool(online.get(m.channel, False)))
            te, ev = envs.get(m.channel, (np.empty(0), np.empty(0)))
            if ev.size:
                reading.samples = int(ev.size)
                reading.env_peak = float(np.max(ev))
                mean_env = float(np.mean(ev))
                mvc = max(self.profile.mvc.get(m.id, 2600.0), 1.0)
                raw_act = (mean_env / mvc * 100.0) if self.th.mvc_normalize else mean_env
                # 1 s 窗只覆盖两步，整流均值方差足以让不对称在 1%↔15% 之间乱跳。
                # 激活是"当前募集水平"而不是瞬时值，按 τ=1.5 s 平滑才是可读的量。
                prev = self._act_ema.get(m.id, raw_act)
                alpha = 1.0 - math.exp(-self._dt / 1.5)
                reading.activation = prev + (raw_act - prev) * alpha
                self._act_ema[m.id] = reading.activation
            if do_psd:
                tr, rv = raws.get(m.channel, (np.empty(0), np.empty(0)))
                # 窗必须装满，且窗内实测速率要与设备时钟速率一致：PLL 暖机/追赶期间
                # 时标会被压缩或拉伸，窗内 (n-1)/span 能算出 1000~3000 Hz 的假速率，
                # 据此做 Welch 会把 MDF 算错一倍并污染疲劳回归。
                expected = rates.get(m.channel, 0.0)
                usable = rv.size > 64 and float(tr[-1] - tr[0]) >= 3.8 and expected > 0
                if usable:
                    win_fs = (rv.size - 1) / max(float(tr[-1] - tr[0]), _EPS)
                    usable = abs(win_fs - expected) / expected <= 0.12
                if usable:
                    fs = expected
                    self._rms[m.id] = float(np.sqrt(np.mean(np.square(rv, dtype=np.float64))))
                    self._sat[m.id] = int(np.count_nonzero((rv >= 32767) | (rv <= -32768)))
                    mdf, mnf = spectral_median(rv, fs)
                    if mdf > 0:
                        # 暖机：前 3 个有效读数取中位数作为 EMA 初值。直接拿第一个读数
                        # 当初值会把 Welch 的偶然偏高固化成一段假的下移/上移趋势。
                        warm = self._mdf_warm.setdefault(m.id, [])
                        if len(warm) < 3:
                            warm.append(mdf)
                        if len(warm) >= 3:
                            prev = self._mdf_ema.get(m.id, float(np.median(warm)))
                            smooth = prev + (mdf - prev) * 0.08   # τ≈6 s
                            self._mdf_ema[m.id] = smooth
                            self._mdf[m.id], self._mnf[m.id] = smooth, mnf
                            self._mdf_hist[m.id].append((t_now, smooth))
                            self._rms_hist[m.id].append((t_now, self._rms[m.id]))
                            self._baseline_mdf.setdefault(m.id, smooth)
                            self._baseline_rms.setdefault(m.id, self._rms[m.id])
            reading.rms = self._rms.get(m.id, 0.0)
            reading.saturated = self._sat.get(m.id, 0)
            reading.mdf = self._mdf.get(m.id, 0.0)
            reading.mnf = self._mnf.get(m.id, 0.0)
            reading.mdf_slope = self._slope(self._mdf_hist[m.id], t_now)
            self._act_muscle_hist[m.id].append((t_now, reading.activation))
            snap.muscles[m.id] = reading

        snap.online_muscles = sum(1 for r in snap.muscles.values() if r.online and r.samples)
        snap.data_ok = snap.online_muscles >= 4

        def group(ids: tuple[str, ...]) -> float:
            vals = [snap.muscles[i].activation for i in ids if snap.muscles[i].samples]
            return float(np.mean(vals)) if vals else 0.0

        snap.ham_l = group(("bf_l", "st_l"))
        snap.ham_r = group(("bf_r", "st_r"))
        snap.quad_l = group(("vl_l", "rf_l"))
        snap.quad_r = group(("vl_r", "rf_r"))
        peak_side = max(snap.ham_l, snap.ham_r)
        snap.asym = abs(snap.ham_l - snap.ham_r) / max(peak_side, _EPS) * 100.0 if peak_side > 1 else 0.0
        snap.hq_l = snap.ham_l / max(snap.quad_l, _EPS) if snap.quad_l > 1 else 0.0
        snap.hq_r = snap.ham_r / max(snap.quad_r, _EPS) if snap.quad_r > 1 else 0.0
        snap.cci_l = _cci(snap.ham_l, snap.quad_l)
        snap.cci_r = _cci(snap.ham_r, snap.quad_r)
        snap.bf_share_l = _share(snap.muscles["bf_l"].activation, snap.muscles["st_l"].activation)
        snap.bf_share_r = _share(snap.muscles["bf_r"].activation, snap.muscles["st_r"].activation)
        self._act_hist.append((t_now, snap.ham_l, snap.ham_r, snap.quad_l, snap.quad_r))

        self._update_gait(acq, snap, t_now)
        self._update_fatigue(snap, t_now)
        self._update_workload(snap, t_now)
        self._update_risk(snap, t_now)
        self.snap = snap
        return snap

    # ── 步态 ────────────────────────────────────────────────────────────────

    def _update_gait(self, acq, snap: GuardSnapshot, t_now: float) -> None:
        since = self.detector.last_t if self.detector.last_t > 0 else max(0.0, t_now - 1.0)
        t, samples = acq.imu_since(since + 1e-6)
        completed = self.detector.feed(t, samples) if t.size else []
        snap.strides = list(self.detector.strides)
        snap.stride_count = self.detector.stride_count
        snap.phase = self.detector.phase
        snap.stance_ratio = self.detector.stance_ratio

        side = self.th.worn_side
        bf = f"bf_{side.lower()}"
        st = f"st_{side.lower()}"
        for strike_t, peak_dps, impact_g in completed:
            envs = self._stride_envelopes(acq, (bf, st), strike_t)
            ham_vals = []
            for mid, (te, ev) in envs.items():
                if ev.size == 0:
                    continue
                sel = (te >= strike_t - 0.14) & (te <= strike_t)
                if not np.any(sel):
                    continue
                mvc = max(self.profile.mvc.get(mid, 2600.0), 1.0)
                ham_vals.append(float(np.mean(ev[sel])) / mvc * 100.0)
            terminal = float(np.mean(ham_vals)) if ham_vals else 0.0
            lead = self._onset_lead(envs.get(bf), strike_t)
            stride = self.detector.strides[-1] if self.detector.strides else None
            if stride is not None and abs(stride.t - strike_t) < 1e-6:
                stride.terminal_ham = terminal
                stride.lead_ms = lead
                stride.load_index = (peak_dps / 1000.0) * (terminal / 100.0) * 100.0

        recent = snap.strides[-6:]
        if recent:
            intervals = [s.interval for s in recent if s.interval > 0.15]
            snap.stride_time = float(np.median(intervals)) if intervals else 0.0
            snap.cadence = 120.0 / snap.stride_time if snap.stride_time > 0 else 0.0
            snap.peak_dps = float(np.median([s.peak_dps for s in recent]))
            snap.impact_g = float(np.median([s.impact_g for s in recent]))
            term = [s.terminal_ham for s in recent if s.terminal_ham > 0]
            snap.terminal_ham = float(np.median(term)) if term else 0.0
            leads = [s.lead_ms for s in recent if s.lead_ms > 0]
            snap.lead_ms = float(np.median(leads)) if leads else 0.0

    def _stride_envelopes(self, acq, mids: tuple[str, ...], strike_t: float) -> dict:
        """步态级快包络：20 Hz 零相位低通，绝对时标窗。

        显示用的 4 Hz 包络群延迟约 80 ms，而末端摆动窗只有 140 ms——直接拿它做步态
        时序会把离心负荷峰值整段推到触地之后。这里改用 filtfilt 零相位，且窗口按绝对
        设备时标取，不随 now() 前进而漂移。
        """
        out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for mid in mids:
            m = muscle_by_id(mid)
            if m is None:
                continue
            t, x = acq.emg_abs(m.channel, strike_t - 0.40, strike_t + 0.06)
            if x.size < 64 or t.size != x.size:
                out[mid] = (np.empty(0), np.empty(0))
                continue
            fs = (x.size - 1) / max(float(t[-1] - t[0]), _EPS)
            if not (60.0 <= fs <= 20000.0):
                out[mid] = (np.empty(0), np.empty(0))
                continue
            sos = signal.butter(2, min(20.0, fs / 2 - 1) / (fs / 2), btype="low", output="sos")
            env = signal.sosfiltfilt(sos, np.abs(x.astype(np.float64)))
            out[mid] = (t, env)
        return out

    def _onset_lead(self, series: tuple[np.ndarray, np.ndarray] | None, strike_t: float) -> float:
        """BF 在触地前多久开始激活。越晚（提前量越小）离心保护越差。"""
        if not series:
            return 0.0
        te, ev = series
        if ev.size < 8:
            return 0.0
        sel = (te >= strike_t - 0.35) & (te <= strike_t - 0.005)
        if not np.any(sel):
            return 0.0
        ts, vs = te[sel], ev[sel]
        gate = float(np.max(vs)) * 0.25
        above = np.flatnonzero(vs > gate)
        if above.size == 0:
            return 0.0
        onset_idx = int(above[-1])
        while onset_idx > 0 and vs[onset_idx - 1] > gate:
            onset_idx -= 1
        return max(0.0, (strike_t - float(ts[onset_idx])) * 1000.0)

    # ── 疲劳 ────────────────────────────────────────────────────────────────

    def _slope(self, hist: deque, t_now: float) -> float:
        """最近 trend_seconds 窗内中位频率的线性回归斜率，换算成 %/min。

        至少要 15 s 跨度才出数，并设 1%/min 死区：短窗回归会把 Welch 抖动放大成
        假疲劳，而死区以下的斜率在生理上没有判读意义。
        """
        if len(hist) < 8:
            return 0.0
        pts = [(t, v) for t, v in hist if t_now - t <= self.th.trend_seconds]
        if len(pts) < 8:
            return 0.0
        t = np.array([p[0] for p in pts]) - pts[0][0]
        v = np.array([p[1] for p in pts])
        if float(np.ptp(t)) < 15.0 or float(np.mean(v)) <= 0:
            return 0.0
        slope = float(np.polyfit(t, v, 1)[0])          # Hz/s
        pct = slope / float(np.mean(v)) * 6000.0       # %/min
        return pct if abs(pct) >= 1.0 else 0.0

    def _update_fatigue(self, snap: GuardSnapshot, t_now: float) -> None:
        slopes = [snap.muscles[i].mdf_slope for i in HAM_IDS if snap.muscles[i].mdf > 0]
        snap.mdf_slope = float(np.mean(slopes)) if slopes else 0.0
        drifts = []
        for mid in HAM_IDS:
            base = self._baseline_rms.get(mid, 0.0)
            hist = self._rms_hist[mid]
            if base > 0 and len(hist) >= 4:
                recent = [v for t, v in hist if t_now - t <= 5.0]
                if recent:
                    drifts.append((float(np.mean(recent)) - base) / base * 100.0)
        snap.rms_drift = float(np.mean(drifts)) if drifts else 0.0
        fatigue = 0.62 * min(1.0, max(0.0, -snap.mdf_slope) / 14.0)
        fatigue += 0.38 * min(1.0, max(0.0, snap.rms_drift) / 28.0)
        snap.fatigue_index = float(np.clip(fatigue, 0.0, 1.0) * 100.0)

    # ── 负荷 ────────────────────────────────────────────────────────────────

    def _update_workload(self, snap: GuardSnapshot, t_now: float) -> None:
        dt = self._dt
        self._last_tick = t_now
        if not snap.data_ok or dt <= 0:
            return
        ham = (snap.ham_l + snap.ham_r) / 2.0
        quad = (snap.quad_l + snap.quad_r) / 2.0
        moving = snap.cadence > 60.0
        if moving:
            self.active_seconds += dt
            self.load_au += (ham / 100.0 * 0.62 + quad / 100.0 * 0.18) * dt * (0.6 + self.drill.intensity)
        fast = moving and snap.peak_dps > self.th.swing_warn and snap.cadence > 140.0
        if fast:
            self.high_speed_seconds += dt
            if not self._bout_active:
                self._bout_active, self._bout_start, self._bout_quiet = True, t_now, 0.0
        elif self._bout_active:
            self._bout_quiet += dt
            if self._bout_quiet > 1.5:
                if t_now - self._bout_start >= 2.0:
                    self.sprint_bouts += 1
                self._bout_active = False
        snap.sprint_bouts = self.sprint_bouts + (1 if self._bout_active else 0)
        snap.high_speed_seconds = self.high_speed_seconds
        snap.load_au = self.load_au
        snap.active_seconds = self.active_seconds
        snap.acwr = self.load_au / max(self.profile.chronic_load, 1.0)

    # ── 风险 ────────────────────────────────────────────────────────────────

    def _update_risk(self, snap: GuardSnapshot, t_now: float) -> None:
        th = self.th
        side = "左" if snap.ham_l < snap.ham_r else "右"
        weaker = "L" if snap.ham_l < snap.ham_r else "R"
        raw: dict[str, tuple[float, float, bool, str]] = {}

        raw["asym"] = (snap.asym, _ramp_up(snap.asym, th.asym_warn, th.asym_danger),
                       snap.ham_l > 1 and snap.ham_r > 1,
                       f"{'左' if weaker == 'L' else '右'}侧偏低，差 {abs(snap.ham_l - snap.ham_r):.0f}%MVC")
        hq = min(snap.hq_l, snap.hq_r) if (snap.hq_l > 0 and snap.hq_r > 0) else max(snap.hq_l, snap.hq_r)
        hq_side = "L" if snap.hq_l <= snap.hq_r else "R"
        raw["hq"] = (hq, _ramp_down(hq, th.hq_warn, th.hq_danger), hq > 0,
                     f"{'左' if hq_side == 'L' else '右'}侧 {hq:.2f}，腘绳肌相对股四头肌激活不足")
        raw["fatigue"] = (snap.mdf_slope, _ramp_up(-snap.mdf_slope, -th.mdf_warn, -th.mdf_danger),
                          snap.mdf_slope != 0,
                          f"腘绳肌 MDF {snap.mdf_slope:+.1f}%/min，RMS {snap.rms_drift:+.0f}%")
        raw["swing"] = (snap.peak_dps, _ramp_up(snap.peak_dps, th.swing_warn, th.swing_danger),
                        snap.peak_dps > 0,
                        f"末端摆动 {snap.terminal_ham:.0f}%MVC，离心负荷指数 "
                        f"{(snap.peak_dps / 1000.0) * (snap.terminal_ham / 100.0) * 100.0:.0f}")
        raw["timing"] = (snap.lead_ms, _ramp_down(snap.lead_ms, th.lead_warn, th.lead_danger),
                         snap.lead_ms > 0,
                         f"BF 在触地前 {snap.lead_ms:.0f} ms 才开始激活")
        raw["load"] = (snap.acwr, _ramp_up(snap.acwr, th.acwr_warn, th.acwr_danger),
                       snap.load_au > 1,
                       f"本次 {snap.load_au:.0f} AU / 慢性 {self.profile.chronic_load:.0f} AU")

        alpha = 1.0 - math.exp(-self._dt / max(th.smoothing_seconds, 0.05))
        total_w = 0.0
        total = 0.0
        factors: list[FactorReading] = []
        for fid, weight in FACTOR_WEIGHTS.items():
            value, severity, available, detail = raw[fid]
            if available:
                self._sev[fid] += (severity - self._sev[fid]) * alpha
            else:
                self._sev[fid] *= (1.0 - alpha)
            sev = self._sev[fid]
            contribution = weight * sev * 100.0
            if available:
                total_w += weight
                total += contribution
            factors.append(FactorReading(id=fid, value=value, severity=sev, raw_severity=severity,
                                         weight=weight, contribution=contribution, detail=detail,
                                         available=available))
        snap.factors = factors
        snap.score = float(np.clip(total / max(total_w, _EPS), 0.0, 100.0))
        self._emit_events(snap, t_now, side)

    def _emit_events(self, snap: GuardSnapshot, t_now: float, side: str) -> None:
        th = self.th
        thresholds = {
            "asym": th.asym_warn, "hq": th.hq_warn, "fatigue": th.mdf_warn,
            "swing": th.swing_warn, "timing": th.lead_warn, "load": th.acwr_warn,
        }
        for f in snap.factors:
            if not f.available or f.severity < 0.5:
                self._above_since[f.id] = None
                continue
            if self._above_since[f.id] is None:
                self._above_since[f.id] = t_now
                continue
            held = t_now - self._above_since[f.id]
            last = self._last_event_t.get(f.id, -1e9)
            if held < th.sustain_seconds or t_now - last < th.cooldown_seconds:
                continue
            self._last_event_t[f.id] = t_now
            self._event_seq += 1
            rel = t_now - self.session_start if self.session_start is not None else t_now
            self.events.append(RiskEvent(
                id=self._event_seq, t=rel, wall=time.time(), factor=f.id, severity=f.severity,
                score=snap.score, value=f.value, threshold=thresholds[f.id], side=side,
                drill=self.drill.name, advice=advice_for(f.id, snap, self.profile),
            ))
            del self.events[:-400]

    # ── 历史访问（供趋势图） ────────────────────────────────────────────────

    def mdf_history(self, mid: str) -> list[tuple[float, float]]:
        return list(self._mdf_hist.get(mid, ()))

    def activation_history(self) -> list[tuple]:
        return list(self._act_hist)

    def muscle_activation_history(self, mid: str) -> list[tuple[float, float]]:
        return list(self._act_muscle_hist.get(mid, ()))

    def baseline_mdf(self, mid: str) -> float:
        return self._baseline_mdf.get(mid, 0.0)

    # ── MVC 标定 ────────────────────────────────────────────────────────────

    def capture_mvc(self, acq, seconds: float = 3.0) -> dict[str, float]:
        """把最近若干秒的包络峰值当作该肌肉的 MVC 参考。"""
        envs = acq.env_windows(seconds)
        out: dict[str, float] = {}
        for m in MUSCLES:
            _t, ev = envs.get(m.channel, (np.empty(0), np.empty(0)))
            if ev.size >= 8:
                out[m.id] = round(float(np.percentile(ev, 97)), 1)
        return out


def _cci(ham: float, quad: float) -> float:
    total = ham + quad
    return 2.0 * min(ham, quad) / total * 100.0 if total > 1 else 0.0


def _share(a: float, b: float) -> float:
    total = a + b
    return a / total * 100.0 if total > 1 else 0.0


def advice_for(factor: str, snap: GuardSnapshot, profile: AthleteProfile) -> str:
    weaker = "左" if snap.ham_l < snap.ham_r else "右"
    injured = profile.risk_side
    focus = ("左" if injured == "L" else "右") if injured else weaker
    table = {
        "asym": f"{weaker}侧腘绳肌激活偏低：本组降到 70% 强度，补 {focus}侧单腿离心 2×6 次后再冲刺。",
        "hq": "腘绳肌相对股四头肌激活不足：先做 2 组 Nordic 或臀桥唤醒，再回到跑动课目。",
        "fatigue": "腘绳肌频谱持续下移：建议轮换下场 3–5 分钟，补水并复测激活后再上强度。",
        "swing": "末端摆动角速度过高：缩短步幅、降速 10%，避免髋过屈伴膝过伸的末端姿态。",
        "timing": "BF 触地前激活偏晚：做 3 次高抬腿 + 弹力带后勾唤醒，重新建立预激活。",
        "load": "本次负荷已超近 7 日均值：结束高强度段落，转入恢复慢跑并记录课次负荷。",
    }
    return table.get(factor, "复核动作质量并降低强度。")


def cue_for(snap: GuardSnapshot, profile: AthleteProfile) -> tuple[str, str, str]:
    """返回 (等级, 标题, 指令)。等级 ∈ calm / watch / warn / stop。"""
    top = snap.top_factor()
    if not snap.data_ok:
        return "calm", "等待传感器", "确认 8 路 sEMG 与大腿 IMU 在线后开始课次。"
    if snap.score >= 65 and top is not None:
        return "stop", f"{top.name}超限", advice_for(top.id, snap, profile)
    if snap.score >= 35 and top is not None:
        return "warn", f"{top.name}需关注", advice_for(top.id, snap, profile)
    if top is not None and top.severity >= 0.25:
        return "watch", f"{top.name}接近阈值", top.detail
    return "calm", "动作质量良好", f"保持当前节奏，步频 {snap.cadence:.0f} 步/分，继续监控。"