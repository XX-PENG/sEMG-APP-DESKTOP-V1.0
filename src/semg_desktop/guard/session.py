"""课次汇总：把一次训练压缩成可存档、可比较的报告数据。

实时快照是"现在"，课次汇总是"这一次"。两者分开，报告才能在停止采集后仍然完整可读，
也才能跨课次比较（这次不对称比上次好了多少）。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from .model import MUSCLES, GuardSnapshot


@dataclass
class MuscleSummary:
    id: str
    label: str
    mean_act: float = 0.0
    peak_act: float = 0.0
    mdf_start: float = 0.0
    mdf_end: float = 0.0
    mdf_slope: float = 0.0


@dataclass
class SessionSummary:
    athlete_id: str = ""
    athlete_name: str = ""
    number: int = 0
    position: str = ""
    drill: str = ""
    started_wall: float = field(default_factory=time.time)
    duration_seconds: float = 0.0
    strides: int = 0
    sprint_bouts: int = 0
    high_speed_seconds: float = 0.0
    load_au: float = 0.0
    chronic_load: float = 0.0
    acwr: float = 0.0
    mean_asym: float = 0.0
    max_asym: float = 0.0
    min_hq: float = 0.0
    max_fatigue: float = 0.0
    mean_mdf_slope: float = 0.0
    final_score: float = 0.0
    peak_score: float = 0.0
    events_high: int = 0
    events_mid: int = 0
    events_ack: int = 0
    muscles: list[MuscleSummary] = field(default_factory=list)
    source: str = ""
    note: str = ""

    @property
    def verdict(self) -> str:
        if self.peak_score >= 65 or self.events_high >= 3:
            return "本次课次出现高危段落，建议复核离心力量与跑动技术后再上强度。"
        if self.peak_score >= 35 or self.max_asym >= 15:
            return "本次课次中等风险，建议补充单侧离心训练并监控下一次课次。"
        return "本次课次风险可控，可按计划继续当前训练负荷。"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict
        return d


def build_summary(engine, snap: GuardSnapshot, meta: dict) -> SessionSummary:
    s = SessionSummary(**{k: v for k, v in meta.items() if k in SessionSummary.__dataclass_fields__})
    act = engine.activation_history()
    if act:
        asym_series = []
        hq_series = []
        for row in act:
            hl, hr, ql, qr = row[1], row[2], row[3], row[4]
            peak = max(hl, hr)
            if peak > 1:
                asym_series.append(abs(hl - hr) / peak * 100.0)
            if ql > 1 and qr > 1:
                hq_series.append(min(hl / ql, hr / qr))
        s.mean_asym = sum(asym_series) / len(asym_series) if asym_series else 0.0
        s.max_asym = max(asym_series) if asym_series else 0.0
        s.min_hq = min(hq_series) if hq_series else 0.0
    s.strides = snap.stride_count
    s.sprint_bouts = snap.sprint_bouts
    s.high_speed_seconds = snap.high_speed_seconds
    s.load_au = snap.load_au
    s.acwr = snap.acwr
    s.max_fatigue = max(s.max_fatigue, snap.fatigue_index)
    s.final_score = snap.score
    s.mean_mdf_slope = snap.mdf_slope

    for m in MUSCLES:
        hist = engine.mdf_history(m.id)
        acts = engine.muscle_activation_history(m.id)
        ms = MuscleSummary(id=m.id, label=m.label)
        if hist:
            ms.mdf_start = hist[0][1]
            ms.mdf_end = hist[-1][1]
        if acts:
            vals = [v for _t, v in acts]
            ms.mean_act = sum(vals) / len(vals)
            ms.peak_act = max(vals)
        rd = snap.muscles.get(m.id)
        if rd:
            ms.mdf_slope = rd.mdf_slope
        s.muscles.append(ms)

    events = engine.events
    s.events_high = sum(1 for e in events if e.level == "高")
    s.events_mid = sum(1 for e in events if e.level == "中")
    s.events_ack = sum(1 for e in events if e.acknowledged)
    return s