"""解剖学映射、课目与风险阈值。

通道号到肌肉的对应关系是这套系统的语义地基：8 路 sEMG 不是 "CH1..CH8"，而是双侧
腘绳肌（股二头肌 BF / 半腱肌 ST）与双侧股四头肌（股外侧肌 VL / 股直肌 RF）。
对应关系可在传感器布局页拖拽交换（_ASSIGNMENT），判读始终取当前映射。
腘绳肌用暖色、股四头肌用冷色，左侧满彩、右侧提亮——同一块肌肉的左右两侧靠色族与
明度成对，而不是靠八个互不相干的色相，这样"左右不对称"在图上一眼能看出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Muscle:
    id: str
    channel: int
    name: str
    abbr: str
    side: str          # "L" / "R"
    group: str         # "ham" 腘绳肌 / "quad" 股四头肌
    color: str
    role: str

    @property
    def label(self) -> str:
        return f"{'左' if self.side == 'L' else '右'}·{self.abbr}"

    @property
    def full_label(self) -> str:
        return f"{'左' if self.side == 'L' else '右'}·{self.name}"


# 按通道号排序；出厂贴点默认 CH1/CH5=半腱肌、CH2/CH6=股二头肌（与硬件接线一致），
# 布局页拖拽交换改写 _ASSIGNMENT，判读始终取当前映射。
MUSCLES: tuple[Muscle, ...] = (
    Muscle("st_l", 0, "半腱肌", "ST", "L", "ham", "#FF8A3D",
           "内侧腘绳肌，与 BF 分担离心负荷"),
    Muscle("bf_l", 1, "股二头肌长头", "BF", "L", "ham", "#FF4D8D",
           "末端摆动离心制动主力，拉伤最常发生的位置"),
    Muscle("vl_l", 2, "股外侧肌", "VL", "L", "quad", "#3D9BFF",
           "伸膝主力，H:Q 比的分母"),
    Muscle("rf_l", 3, "股直肌", "RF", "L", "quad", "#12E0C4",
           "跨髋膝双关节，摆动末期减速"),
    Muscle("st_r", 4, "半腱肌", "ST", "R", "ham", "#FFC48A",
           "内侧腘绳肌，与 BF 分担离心负荷"),
    Muscle("bf_r", 5, "股二头肌长头", "BF", "R", "ham", "#FFA8C8",
           "末端摆动离心制动主力，拉伤最常发生的位置"),
    Muscle("vl_r", 6, "股外侧肌", "VL", "R", "quad", "#9CC9FF",
           "伸膝主力，H:Q 比的分母"),
    Muscle("rf_r", 7, "股直肌", "RF", "R", "quad", "#7FF0E2",
           "跨髋膝双关节，摆动末期减速"),
)

_BY_ID = {m.id: m for m in MUSCLES}
HAM_IDS = tuple(m.id for m in MUSCLES if m.group == "ham")
QUAD_IDS = tuple(m.id for m in MUSCLES if m.group == "quad")

# 贴点 → 采集通道的当前对应：MUSCLES.channel 只是出厂默认，布局页拖拽交换贴点后
# 在这里改写，引擎 / 波形 / 清单全部经 channel_of / muscle_by_channel 取当前映射，
# 保证"数据跟着贴点走"。
_ASSIGNMENT: dict[str, int] = {m.id: m.channel for m in MUSCLES}


def muscle_assignment() -> dict[str, int]:
    return dict(_ASSIGNMENT)


def set_muscle_assignment(mapping: dict) -> None:
    """空映射 = 恢复出厂对应；否则只接受 8 贴点 × 8 通道的一一对应，非法输入整体忽略。"""
    if not mapping:
        _ASSIGNMENT.clear()
        _ASSIGNMENT.update({m.id: m.channel for m in MUSCLES})
        return
    clean: dict[str, int] = {}
    for mid, ch in (mapping or {}).items():
        if mid in _BY_ID and isinstance(ch, int) and 0 <= ch < len(MUSCLES):
            clean[mid] = ch
    if len(clean) == len(MUSCLES) and sorted(clean.values()) == list(range(len(MUSCLES))):
        _ASSIGNMENT.clear()
        _ASSIGNMENT.update(clean)


def channel_of(mid: str) -> int:
    return _ASSIGNMENT.get(mid, _BY_ID[mid].channel)


def muscle_by_channel(channel: int) -> Muscle | None:
    for mid, ch in _ASSIGNMENT.items():
        if ch == int(channel):
            return _BY_ID[mid]
    return None


def muscle_by_id(mid: str) -> Muscle | None:
    return _BY_ID.get(mid)


@dataclass(frozen=True)
class Drill:
    id: str
    name: str
    short: str
    stride_hz: float       # 单腿步频，用于模拟源与步态判读基准
    intensity: float       # 0..1 课目强度
    eccentric: float       # 离心占比权重
    purpose: str
    watch: str             # 这个课目最该盯的风险因子 id

    @property
    def cadence(self) -> float:
        return self.stride_hz * 120.0   # 双腿步/分


DRILLS: tuple[Drill, ...] = (
    Drill("warmup", "热身激活", "热身", 1.35, 0.42, 0.30,
          "低强度跑动 + 动态拉伸，建立基线激活与步频", "timing"),
    Drill("sprint", "直线冲刺", "冲刺", 2.15, 1.00, 0.85,
          "30 m 全力冲刺，末端摆动离心负荷峰值课目", "swing"),
    Drill("cut", "变向切入", "变向", 1.85, 0.88, 0.78,
          "侧向切入与急停，左右不对称最容易被放大", "asym"),
    Drill("nordic", "Nordic 离心", "Nordic", 0.55, 0.92, 1.00,
          "跪姿前倒离心训练，腘绳肌力量干预的金标准", "hq"),
    Drill("match", "比赛模拟", "比赛", 1.95, 0.95, 0.80,
          "间歇高强度跑动，疲劳累积与急性负荷监控", "fatigue"),
    Drill("recover", "恢复慢跑", "恢复", 1.55, 0.35, 0.20,
          "低强度有氧，观察疲劳后频谱是否回升", "fatigue"),
)

DRILL_BY_ID = {d.id: d for d in DRILLS}


@dataclass
class Thresholds:
    """场边可实时调整的判读阈值。每一项都直接改变风险指数与提示，不是装饰。"""

    asym_warn: float = 10.0        # 左右激活不对称 %
    asym_danger: float = 20.0
    hq_warn: float = 0.65          # 腘绳肌/股四头肌激活比下限
    hq_danger: float = 0.50
    mdf_warn: float = -6.0         # 中位频率下降速率 %/min
    mdf_danger: float = -12.0
    swing_warn: float = 700.0      # 末端摆动大腿角速度 dps
    swing_danger: float = 950.0
    lead_warn: float = 90.0        # BF 触地前激活提前量 ms（越小越晚）
    lead_danger: float = 55.0
    acwr_warn: float = 1.30        # 急性/慢性负荷比
    acwr_danger: float = 1.60
    sustain_seconds: float = 0.8   # 因子需持续多久才记事件
    cooldown_seconds: float = 10.0
    smoothing_seconds: float = 1.2  # 严重度 EMA 时间常数
    auto_stop: bool = True         # 高危持续时给出停练建议
    sound: bool = True
    mvc_normalize: bool = True
    window_seconds: float = 2.0    # 激活/频谱计算窗
    trend_seconds: float = 45.0    # 疲劳回归窗
    sagittal_axis: int = 1         # IMU 矢状面轴 0=X 1=Y 2=Z
    worn_side: str = "R"           # IMU 佩戴侧
    swing_gate_dps: float = 280.0  # 摆动峰识别门限
    impact_gate_g: float = 2.2     # 触地冲击门限
    fatigue_rate: float = 6.0      # 疲劳推演倍率，仅模拟源生效

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    def load(self, data: dict) -> None:
        for k, v in (data or {}).items():
            if k in self.__dataclass_fields__ and v is not None:
                setattr(self, k, type(getattr(self, k))(v))


# 风险因子权重：合成为 0..100 的总指数，权重公开显示在界面上，不做黑箱
FACTOR_WEIGHTS = {
    "asym": 0.24,
    "hq": 0.22,
    "fatigue": 0.20,
    "swing": 0.18,
    "timing": 0.10,
    "load": 0.06,
}

FACTOR_META = {
    "asym": ("左右不对称", "双侧腘绳肌激活差", "%"),
    "hq": ("H:Q 激活比", "腘绳肌 / 股四头肌", ""),
    "fatigue": ("频谱疲劳", "中位频率下降速率", "%/min"),
    "swing": ("末端摆动负荷", "摆动末期大腿角速度", "dps"),
    "timing": ("激活时序", "BF 触地前激活提前量", "ms"),
    "load": ("急性负荷比", "本次 / 近 7 日均值", ""),
}

RISK_BANDS = (
    (35.0, "低", "#2FD98A", "可继续当前课目"),
    (65.0, "中", "#FFB020", "降低强度并复核动作"),
    (101.0, "高", "#FF3B5C", "建议立即停止高强度跑动"),
)


def risk_band(score: float) -> tuple[str, str, str]:
    for ceiling, name, color, advice in RISK_BANDS:
        if score < ceiling:
            return name, color, advice
    return RISK_BANDS[-1][1:]


DEFAULT_MVC = {m.id: 2600.0 for m in MUSCLES}


def default_mvc() -> dict[str, float]:
    return dict(DEFAULT_MVC)


@dataclass
class FactorReading:
    id: str
    value: float
    severity: float          # 0..1，已平滑
    raw_severity: float
    weight: float
    contribution: float      # 对总分的贡献（分）
    detail: str = ""
    available: bool = True

    @property
    def name(self) -> str:
        return FACTOR_META[self.id][0]

    @property
    def unit(self) -> str:
        return FACTOR_META[self.id][2]


@dataclass
class MuscleReading:
    id: str
    activation: float = 0.0     # %MVC
    rms: float = 0.0
    mdf: float = 0.0            # Hz
    mnf: float = 0.0
    mdf_slope: float = 0.0      # %/min
    env_peak: float = 0.0
    saturated: int = 0
    online: bool = False
    samples: int = 0


@dataclass
class Stride:
    t: float                    # 触地时刻（设备重建时标）
    interval: float             # 与上一次触地的间隔 s
    peak_dps: float             # 摆动峰角速度
    impact_g: float
    terminal_ham: float         # 末端摆动腘绳肌激活 %MVC
    lead_ms: float              # BF 激活提前量
    load_index: float           # 离心负荷指数


@dataclass
class RiskEvent:
    id: int
    t: float                    # 会话相对秒
    wall: float                 # time.time()
    factor: str
    severity: float
    score: float
    value: float
    threshold: float
    side: str
    drill: str
    advice: str
    acknowledged: bool = False
    note: str = ""

    @property
    def level(self) -> str:
        return "高" if self.severity >= 0.75 else "中"

    @property
    def name(self) -> str:
        return FACTOR_META.get(self.factor, (self.factor, "", ""))[0]


@dataclass
class GuardSnapshot:
    t: float = 0.0
    muscles: dict[str, MuscleReading] = field(default_factory=dict)
    ham_l: float = 0.0
    ham_r: float = 0.0
    quad_l: float = 0.0
    quad_r: float = 0.0
    asym: float = 0.0
    hq_l: float = 0.0
    hq_r: float = 0.0
    cci_l: float = 0.0
    cci_r: float = 0.0
    bf_share_l: float = 0.0
    bf_share_r: float = 0.0
    cadence: float = 0.0
    stride_time: float = 0.0
    peak_dps: float = 0.0
    impact_g: float = 0.0
    terminal_ham: float = 0.0
    lead_ms: float = 0.0
    phase: float = 0.0
    stance_ratio: float = 0.35
    strides: list[Stride] = field(default_factory=list)
    stride_count: int = 0
    fatigue_index: float = 0.0
    mdf_slope: float = 0.0
    rms_drift: float = 0.0
    score: float = 0.0
    factors: list[FactorReading] = field(default_factory=list)
    sprint_bouts: int = 0
    high_speed_seconds: float = 0.0
    load_au: float = 0.0
    acwr: float = 0.0
    active_seconds: float = 0.0
    data_ok: bool = False
    online_muscles: int = 0
    imu_online: bool = False

    @property
    def band(self) -> tuple[str, str, str]:
        return risk_band(self.score)

    def top_factor(self) -> FactorReading | None:
        live = [f for f in self.factors if f.available]
        return max(live, key=lambda f: f.contribution) if live else None