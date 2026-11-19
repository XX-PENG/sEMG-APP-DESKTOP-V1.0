"""运动员档案：MVC 基线、伤史与慢性负荷，跨会话持久化。

风险判读必须相对"这个人"而不是相对全体：MVC 基线决定激活百分比，伤史决定哪一侧要
重点盯，近 7 日慢性负荷决定急性负荷比的分母。所以档案不是通讯录，是判读参数的一部分。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .model import MUSCLES, Thresholds, default_mvc

POSITIONS = ("前锋", "中场", "后卫", "守门员")


@dataclass
class Injury:
    date: str
    muscle: str
    side: str
    grade: str
    weeks_out: int
    note: str = ""


@dataclass
class AthleteProfile:
    id: str
    name: str
    number: int
    position: str
    age: int
    height_cm: int
    mass_kg: float
    dominant_leg: str = "R"
    mvc: dict[str, float] = field(default_factory=default_mvc)
    chronic_load: float = 320.0        # 近 7 日平均课次负荷 AU
    injuries: list[Injury] = field(default_factory=list)
    return_week: int | None = None     # 伤后复出第几周
    thresholds: dict = field(default_factory=dict)
    note: str = ""
    created: float = field(default_factory=time.time)

    @property
    def initials(self) -> str:
        return self.name[:1] if self.name else "?"

    @property
    def risk_side(self) -> str | None:
        """有伤史的一侧优先作为重点侧。"""
        return self.injuries[0].side if self.injuries else None

    @property
    def injury_summary(self) -> str:
        if not self.injuries:
            return "无腘绳肌伤史"
        inj = self.injuries[0]
        side = "左" if inj.side == "L" else "右"
        return f"{inj.date} {side}{inj.muscle} {inj.grade}"

    @property
    def bmi(self) -> float:
        h = self.height_cm / 100.0
        return self.mass_kg / (h * h) if h > 0 else 0.0

    def thresholds_for(self, base: Thresholds) -> Thresholds:
        t = Thresholds(**base.as_dict())
        t.load(self.thresholds)
        return t

    def to_dict(self) -> dict:
        d = asdict(self)
        d["injuries"] = [asdict(i) if isinstance(i, Injury) else i for i in self.injuries]
        return d

    @staticmethod
    def from_dict(d: dict) -> AthleteProfile:
        injuries = [Injury(**i) if isinstance(i, dict) else i for i in d.get("injuries", [])]
        known = {f for f in AthleteProfile.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known and k != "injuries"}
        kwargs["injuries"] = injuries
        mvc = dict(default_mvc())
        mvc.update(kwargs.get("mvc") or {})
        kwargs["mvc"] = mvc
        return AthleteProfile(**kwargs)


def _seed() -> list[AthleteProfile]:
    """一支曲棍球队的真实感名单：伤后复出、左右失衡、高负荷前锋各有代表。"""
    def mvc(scale_l: float, scale_r: float, ham_r: float = 1.0, quad_r: float = 1.0) -> dict:
        out = {}
        for m in MUSCLES:
            base = 2600.0 if m.group == "ham" else 3100.0
            s = scale_l if m.side == "L" else scale_r
            if m.side == "R":
                s *= ham_r if m.group == "ham" else quad_r
            out[m.id] = round(base * s, 1)
        return out

    return [
        AthleteProfile(
            id="ath-lin", name="林致远", number=9, position="前锋", age=24,
            height_cm=178, mass_kg=72.5, dominant_leg="R",
            mvc=mvc(1.02, 1.0, ham_r=0.99),
            chronic_load=412.0,
            injuries=[Injury("2026-03-14", "股二头肌长头", "R", "II 级", 6,
                             "冲刺末端摆动拉伤，MRI 示肌腱交界处水肿")],
            return_week=6,
            thresholds={"asym_warn": 8.0, "asym_danger": 16.0, "swing_danger": 880.0},
            note="伤后复出第 6 周，右侧离心力量仍低于左侧，冲刺课目需限制在 80% 强度。",
        ),
        AthleteProfile(
            id="ath-zhao", name="赵梦琪", number=7, position="中场", age=22,
            height_cm=169, mass_kg=61.0, dominant_leg="L",
            mvc=mvc(0.94, 1.02),
            chronic_load=386.0,
            injuries=[Injury("2025-11-02", "半腱肌", "L", "I 级", 3, "变向切入时牵拉伤")],
            return_week=42,
            note="跑动量全队最高，重点关注疲劳累积与急性负荷比。",
        ),
        AthleteProfile(
            id="ath-chen", name="陈昊", number=4, position="后卫", age=27,
            height_cm=183, mass_kg=79.0, dominant_leg="R",
            mvc=mvc(1.12, 1.10),
            chronic_load=298.0,
            note="力量基础好，H:Q 稳定，作为队内基线参照。",
        ),
        AthleteProfile(
            id="ath-su", name="苏雨桐", number=11, position="前锋", age=20,
            height_cm=172, mass_kg=64.5, dominant_leg="L",
            mvc=mvc(0.88, 0.96, ham_r=0.97),
            chronic_load=255.0,
            injuries=[Injury("2026-07-21", "股二头肌长头", "L", "I 级", 2, "季前冲刺营")],
            return_week=3,
            thresholds={"lead_warn": 190.0, "lead_danger": 130.0},
            note="复出第 3 周，激活时序偏晚，先做唤醒再上强度。",
        ),
        AthleteProfile(
            id="ath-guo", name="郭子航", number=1, position="守门员", age=29,
            height_cm=186, mass_kg=84.0, dominant_leg="R",
            mvc=mvc(1.02, 1.05),
            chronic_load=186.0,
            note="扑救以侧向蹬伸为主，冲刺负荷低，重点看左右不对称。",
        ),
    ]


class Roster:
    """档案集合，带搜索 / 过滤 / 排序与撤销删除。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.athletes: list[AthleteProfile] = []
        self._trash: list[tuple[int, AthleteProfile]] = []
        self.load()

    def load(self) -> None:
        if self.path and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.athletes = [AthleteProfile.from_dict(d) for d in data.get("athletes", [])]
            except (OSError, ValueError, TypeError):
                self.athletes = []
        if not self.athletes:
            self.athletes = _seed()
            self.save()

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"athletes": [a.to_dict() for a in self.athletes], "saved": time.time()}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def get(self, aid: str) -> AthleteProfile | None:
        return next((a for a in self.athletes if a.id == aid), None)

    def add(self, profile: AthleteProfile) -> AthleteProfile:
        if not profile.id:
            profile.id = f"ath-{uuid.uuid4().hex[:8]}"
        self.athletes.append(profile)
        self.save()
        return profile

    def update(self, profile: AthleteProfile) -> None:
        for i, a in enumerate(self.athletes):
            if a.id == profile.id:
                self.athletes[i] = profile
                break
        self.save()

    def delete(self, aid: str) -> AthleteProfile | None:
        for i, a in enumerate(self.athletes):
            if a.id == aid:
                self._trash.append((i, a))
                del self.athletes[i]
                self.save()
                return a
        return None

    def undo_delete(self) -> AthleteProfile | None:
        if not self._trash:
            return None
        index, profile = self._trash.pop()
        self.athletes.insert(min(index, len(self.athletes)), profile)
        self.save()
        return profile

    @property
    def can_undo(self) -> bool:
        return bool(self._trash)

    def query(self, text: str = "", position: str = "全部", sort: str = "号码") -> list[AthleteProfile]:
        needle = (text or "").strip().lower()
        rows = [
            a for a in self.athletes
            if (position in ("全部", "") or a.position == position)
            and (not needle or needle in a.name.lower() or needle in str(a.number)
                 or needle in a.position or needle in a.note.lower())
        ]
        keys = {
            "号码": lambda a: a.number,
            "姓名": lambda a: a.name,
            "负荷": lambda a: -a.chronic_load,
            "伤史": lambda a: (0 if a.injuries else 1, a.return_week or 999),
            "年龄": lambda a: -a.age,
        }
        rows.sort(key=keys.get(sort, keys["号码"]))
        return rows

    def new_blank(self) -> AthleteProfile:
        used = {a.number for a in self.athletes}
        number = next((n for n in range(1, 100) if n not in used), 99)
        return AthleteProfile(
            id="", name="新运动员", number=number, position="中场", age=22,
            height_cm=175, mass_kg=68.0, chronic_load=300.0,
        )