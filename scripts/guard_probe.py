"""无硬件探针：用运动员模拟源跑一段课次，打印领域量，核对判读是否成立。

    python scripts/guard_probe.py --seconds 12 --drill sprint
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--drill", default="sprint")
    ap.add_argument("--asym", type=float, default=0.14)
    ap.add_argument("--athlete", default="ath-lin")
    ap.add_argument("--rate", type=float, default=1.0, help="疲劳推演倍率")
    args = ap.parse_args()

    from semg_desktop.config import load_config
    from semg_desktop.guard.engine import GuardEngine, cue_for
    from semg_desktop.guard.model import DRILL_BY_ID, Thresholds
    from semg_desktop.guard.roster import Roster
    from semg_desktop.pipeline import Acquisition
    from semg_desktop.sources import AthleteSimSource

    cfg = load_config(None)
    roster = Roster(None)
    profile = roster.get(args.athlete) or roster.athletes[0]
    drill = DRILL_BY_ID[args.drill]
    src = AthleteSimSource.from_profile(profile, drill=args.drill, fatigue_rate=args.rate)
    print(f"运动员 {profile.name} · {drill.name} · 疲劳推演 ×{args.rate:g} · "
          f"asym={src.asym:.3f} T={src.fatigue_seconds:.0f}s")
    acq = Acquisition(src, cfg)
    acq.start()
    engine = GuardEngine(profile, Thresholds(), drill)
    engine.session_start = acq.now()

    t_end = time.monotonic() + args.seconds
    last_print = 0.0
    snap = None
    while time.monotonic() < t_end:
        time.sleep(1 / 30)
        snap = engine.update(acq, acq.now())
        if snap.t - last_print >= 2.0:
            last_print = snap.t
            print(
                f"t={snap.t:6.1f} 腘绳L/R={snap.ham_l:5.1f}/{snap.ham_r:5.1f}% "
                f"股四L/R={snap.quad_l:5.1f}/{snap.quad_r:5.1f}% "
                f"不对称={snap.asym:5.1f}% H:Q={min(snap.hq_l, snap.hq_r):.2f} "
                f"步频={snap.cadence:5.1f} 摆动={snap.peak_dps:6.0f}dps "
                f"提前={snap.lead_ms:5.0f}ms MDF斜率={snap.mdf_slope:+6.1f}%/min "
                f"疲劳={snap.fatigue_index:5.1f} 风险={snap.score:5.1f} 步数={snap.stride_count}"
            )
    st = acq.stats()
    print("\n--- 末帧 ---")
    if snap:
        for m, r in snap.muscles.items():
            print(f"{m:6s} 激活={r.activation:6.1f}%MVC MDF={r.mdf:6.1f}Hz "
                  f"斜率={r.mdf_slope:+6.1f}%/min RMS={r.rms:7.0f} 在线={r.online}")
        print()
        for f in snap.factors:
            print(f"{f.name:10s} 值={f.value:8.2f}{f.unit:6s} 严重度={f.severity:.2f} "
                  f"贡献={f.contribution:5.1f} 可用={f.available}  {f.detail}")
        print(f"\n总分 {snap.score:.1f}  等级 {snap.band[0]}  负荷 {snap.load_au:.0f}AU  "
              f"ACWR {snap.acwr:.2f}  冲刺段 {snap.sprint_bouts}  高速 {snap.high_speed_seconds:.1f}s")
        print("末端摆动窗明细（最近 5 步）:")
        for s in snap.strides[-5:]:
            print(f"  触地t={s.t:.3f} 间隔={s.interval:.3f}s 峰={s.peak_dps:.0f}dps "
                  f"冲击={s.impact_g:.2f}g 末端腘绳={s.terminal_ham:.1f}%MVC "
                  f"提前={s.lead_ms:.0f}ms 负荷指数={s.load_index:.1f}")
        print("提示:", cue_for(snap, profile))
        print(f"事件 {len(engine.events)} 条")
        for e in engine.events[:8]:
            print(f"  #{e.id} t={e.t:.1f}s {e.name} 值={e.value:.1f} 严重度={e.severity:.2f} {e.advice}")
    print(f"\n链路 {st.bytes_per_second:,.0f} B/s  帧 {st.frames_per_second:.0f}/s  "
          f"解析异常 {st.parse.parse_errors}  错误 {st.error}")
    acq.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())