"""实时守护页：场边第一屏。

左侧肌肉热力图 + 步态相位带回答"现在哪块肌肉、哪个时相在冒险"；中间风险仪表 + 因子
分解回答"为什么是这个分数"；右侧常驻阈值控制区让教练当场改判读口径，所有滑杆动一下
立刻生效，不藏进设置弹窗。
"""

from __future__ import annotations

import json
import random

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..guard.engine import cue_for
from ..guard.model import MUSCLES, Drill, GuardSnapshot, Thresholds
from ..guard.roster import AthleteProfile
from . import theme as T
from .widgets import (
    BilateralBars,
    BodyMap,
    CueBanner,
    FactorBars,
    RiskGauge,
    StatTile,
    StrideRibbon,
    card,
)


class LabeledSlider(QWidget):
    changed = Signal(float)

    def __init__(self, title: str, lo: float, hi: float, step: float, value: float,
                 fmt: str = "{:.0f}", unit: str = "", parent=None) -> None:
        super().__init__(parent)
        self.fmt, self.unit = fmt, unit
        self.lo, self.hi, self.step = lo, hi, step
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        head = QHBoxLayout()
        self.lbl = QLabel(title)
        self.lbl.setObjectName("Muted")
        self.val = QLabel("")
        self.val.setObjectName("Dim")
        self.val.setStyleSheet(f"color:{T.C['turf']}; font-family:'{T.MONO_FAMILY}'; font-size:11px;")
        head.addWidget(self.lbl)
        head.addStretch(1)
        head.addWidget(self.val)
        lay.addLayout(head)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, int(round((hi - lo) / step)))
        self.slider.setValue(int(round((value - lo) / step)))
        self.slider.valueChanged.connect(self._on)
        lay.addWidget(self.slider)
        self._refresh(value)

    def _on(self, idx: int) -> None:
        v = self.lo + idx * self.step
        self._refresh(v)
        self.changed.emit(v)

    def _refresh(self, v: float) -> None:
        self.val.setText(self.fmt.format(v) + self.unit)

    def set_value(self, v: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(int(round((v - self.lo) / self.step)))
        self.slider.blockSignals(False)
        self._refresh(v)


class ThresholdPanel(QWidget):
    """常驻判读控制区：14 个实时控件 + 复位 / 随机 / 导出。"""

    changed = Signal()

    def __init__(self, th: Thresholds, parent=None) -> None:
        super().__init__(parent)
        self.th = th
        self.setFixedWidth(292)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame, lay = card("判读控制", "改动立即生效")
        outer.addWidget(frame)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        form = QVBoxLayout(inner)
        form.setContentsMargins(2, 2, 6, 2)
        form.setSpacing(9)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        self.sliders: dict[str, LabeledSlider] = {}

        def slider(key, title, lo, hi, step, fmt="{:.0f}", unit=""):
            s = LabeledSlider(title, lo, hi, step, getattr(th, key), fmt, unit)
            s.changed.connect(lambda v, k=key: self._set(k, v))
            self.sliders[key] = s
            form.addWidget(s)
            return s

        slider("asym_warn", "左右不对称阈值", 4, 30, 1, unit=" %")
        slider("hq_warn", "H:Q 激活比下限", 0.30, 1.00, 0.01, "{:.2f}")
        slider("mdf_warn", "疲劳 MDF 下降阈值", -24, -1, 1, unit=" %/min")
        slider("swing_warn", "末端摆动角速度上限", 400, 1400, 20, unit=" dps")
        slider("lead_warn", "BF 激活提前量下限", 30, 200, 5, unit=" ms")
        slider("acwr_warn", "急性负荷比上限", 1.0, 2.5, 0.05, "{:.2f}")
        slider("smoothing_seconds", "严重度平滑", 0.2, 4.0, 0.1, "{:.1f}", " s")
        slider("window_seconds", "激活计算窗", 0.5, 4.0, 0.25, "{:.2f}", " s")
        slider("sustain_seconds", "事件持续判定", 0.2, 3.0, 0.1, "{:.1f}", " s")

        self.chk_mvc = QCheckBox("按 MVC 归一化激活")
        self.chk_mvc.setChecked(th.mvc_normalize)
        self.chk_mvc.toggled.connect(lambda v: self._set("mvc_normalize", v))
        form.addWidget(self.chk_mvc)
        self.chk_sound = QCheckBox("场边提示音")
        self.chk_sound.setChecked(th.sound)
        self.chk_sound.toggled.connect(lambda v: self._set("sound", v))
        form.addWidget(self.chk_sound)
        self.chk_autostop = QCheckBox("高危时给出停练建议")
        self.chk_autostop.setChecked(th.auto_stop)
        self.chk_autostop.toggled.connect(lambda v: self._set("auto_stop", v))
        form.addWidget(self.chk_autostop)

        row = QHBoxLayout()
        row.addWidget(QLabel("矢状面轴"))
        self.cb_axis = QComboBox()
        self.cb_axis.addItems(["X", "Y", "Z"])
        self.cb_axis.setCurrentIndex(th.sagittal_axis)
        self.cb_axis.currentIndexChanged.connect(lambda i: self._set("sagittal_axis", i))
        row.addWidget(self.cb_axis, 1)
        form.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("IMU 佩戴侧"))
        self.cb_side = QComboBox()
        self.cb_side.addItems(["左 L", "右 R"])
        self.cb_side.setCurrentIndex(0 if th.worn_side == "L" else 1)
        self.cb_side.currentIndexChanged.connect(lambda i: self._set("worn_side", "L" if i == 0 else "R"))
        row2.addWidget(self.cb_side, 1)
        form.addLayout(row2)

        self.sl_fatigue = LabeledSlider("疲劳推演倍率（仅模拟源）", 0, 20, 1, 6, "{:.0f}", "×")
        self.sl_fatigue.changed.connect(lambda v: self._set("fatigue_rate", v))
        form.addWidget(self.sl_fatigue)
        self.lbl_fatigue_hint = QLabel("真实硬件忽略此项")
        self.lbl_fatigue_hint.setObjectName("Dim")
        form.addWidget(self.lbl_fatigue_hint)

        btns = QHBoxLayout()
        self.btn_reset = QPushButton("复位")
        self.btn_reset.setObjectName("Ghost")
        self.btn_reset.clicked.connect(self.reset)
        self.btn_rand = QPushButton("随机")
        self.btn_rand.setObjectName("Ghost")
        self.btn_rand.setToolTip("在合理范围内随机扰动阈值，用于观察判读口径的敏感性")
        self.btn_rand.clicked.connect(self.randomize)
        self.btn_export = QPushButton("导出")
        self.btn_export.setObjectName("Ghost")
        self.btn_export.clicked.connect(self.export)
        for b in (self.btn_reset, self.btn_rand, self.btn_export):
            btns.addWidget(b)
        lay.addLayout(btns)

    def _set(self, key: str, value) -> None:
        setattr(self.th, key, value)
        self.changed.emit()

    def sync(self) -> None:
        for key, s in self.sliders.items():
            s.set_value(getattr(self.th, key))
        self.chk_mvc.setChecked(self.th.mvc_normalize)
        self.chk_sound.setChecked(self.th.sound)
        self.chk_autostop.setChecked(self.th.auto_stop)
        self.cb_axis.setCurrentIndex(self.th.sagittal_axis)
        self.cb_side.setCurrentIndex(0 if self.th.worn_side == "L" else 1)
        self.sl_fatigue.set_value(self.th.fatigue_rate)

    def reset(self) -> None:
        fresh = Thresholds()
        for k in fresh.__dataclass_fields__:
            setattr(self.th, k, getattr(fresh, k))
        self.sync()
        self.changed.emit()

    def randomize(self) -> None:
        rng = random.Random()
        self.th.asym_warn = round(rng.uniform(6, 18), 0)
        self.th.hq_warn = round(rng.uniform(0.45, 0.85), 2)
        self.th.mdf_warn = -round(rng.uniform(3, 15), 0)
        self.th.swing_warn = round(rng.uniform(550, 1000), 0)
        self.th.lead_warn = round(rng.uniform(60, 140), 0)
        self.th.acwr_warn = round(rng.uniform(1.1, 1.8), 2)
        self.sync()
        self.changed.emit()

    def export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出判读阈值", "hamstring_thresholds.json",
                                              "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.th.as_dict(), fh, indent=2, ensure_ascii=False)


class LiveView(QWidget):
    def __init__(self, th: Thresholds, parent=None) -> None:
        super().__init__(parent)
        self.th = th
        self.profile: AthleteProfile | None = None
        self.drill: Drill | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        self.cue = CueBanner()
        root.addWidget(self.cue)

        mid = QHBoxLayout()
        mid.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(12)
        f_body, l_body = card("肌肉激活热力图", "后面观腘绳肌 / 前面观股四头肌")
        self.body = BodyMap()
        l_body.addWidget(self.body, 1)
        left.addWidget(f_body, 5)
        f_stride, l_stride = card("步态相位带", "末端摆动 = 拉伤发生时相")
        self.ribbon = StrideRibbon()
        l_stride.addWidget(self.ribbon)
        left.addWidget(f_stride, 2)
        mid.addLayout(left, 5)

        center = QVBoxLayout()
        center.setSpacing(12)
        f_gauge, l_gauge = card("拉伤风险", "六因子加权")
        self.gauge = RiskGauge()
        l_gauge.addWidget(self.gauge)
        self.lbl_top = QLabel("")
        self.lbl_top.setObjectName("Muted")
        self.lbl_top.setWordWrap(True)
        l_gauge.addWidget(self.lbl_top)
        center.addWidget(f_gauge, 3)
        f_fac, l_fac = card("分数从哪里来", "点行看明细")
        self.factors = FactorBars()
        l_fac.addWidget(self.factors)
        center.addWidget(f_fac, 4)
        mid.addLayout(center, 3)

        self.panel = ThresholdPanel(th)
        mid.addWidget(self.panel)
        root.addLayout(mid, 1)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.t_asym = StatTile("左右不对称", "%", "magenta")
        self.t_hq = StatTile("H:Q 激活比", "", "orange")
        self.t_cad = StatTile("步频", "步/分", "blue")
        self.t_swing = StatTile("末端摆动", "dps", "magenta")
        self.t_fat = StatTile("疲劳指数", "/100", "warn")
        self.t_load = StatTile("课次负荷", "AU", "turf")
        for t in (self.t_asym, self.t_hq, self.t_cad, self.t_swing, self.t_fat, self.t_load):
            tiles.addWidget(t)
        root.addLayout(tiles)

    def set_context(self, profile: AthleteProfile, drill: Drill) -> None:
        self.profile, self.drill = profile, drill

    def update_snapshot(self, snap: GuardSnapshot) -> None:
        self.body.set_snapshot(snap, self.profile.risk_side if self.profile else None)
        self.ribbon.set_snapshot(snap, self.th.swing_warn)
        self.gauge.set_score(snap.score, snap.data_ok)
        self.factors.set_snapshot(snap)
        level, title, text = cue_for(snap, self.profile) if self.profile else ("calm", "等待", "")
        if level == "stop" and not self.th.auto_stop:
            level, title = "warn", title + "（未启用停练建议）"
        self.cue.set_cue(level, title, text)

        top = snap.top_factor()
        self.lbl_top.setText(f"主导因子：{top.name} · {top.detail}" if top else "等待传感器数据…")

        warn = self.th.asym_warn
        self.t_asym.set_value(f"{snap.asym:.1f}",
                              f"阈值 {warn:.0f}% · {'左' if snap.ham_l < snap.ham_r else '右'}侧偏低",
                              T.C["danger"] if snap.asym >= warn else T.C["safe"])
        hq = min(snap.hq_l, snap.hq_r) if snap.hq_l and snap.hq_r else max(snap.hq_l, snap.hq_r)
        self.t_hq.set_value(f"{hq:.2f}", f"下限 {self.th.hq_warn:.2f}",
                            T.C["danger"] if hq < self.th.hq_warn else T.C["safe"])
        self.t_cad.set_value(f"{snap.cadence:.0f}", f"{snap.stride_count} 步 · 支撑比 {snap.stance_ratio:.0%}")
        self.t_swing.set_value(f"{snap.peak_dps:.0f}",
                               f"末端腘绳 {snap.terminal_ham:.0f}%MVC",
                               T.C["danger"] if snap.peak_dps > self.th.swing_warn else T.C["turf"])
        self.t_fat.set_value(f"{snap.fatigue_index:.0f}",
                             f"MDF {snap.mdf_slope:+.1f}%/min · RMS {snap.rms_drift:+.0f}%",
                             T.risk_color(snap.fatigue_index))
        self.t_load.set_value(f"{snap.load_au:.0f}",
                              f"ACWR {snap.acwr:.2f} · 冲刺段 {snap.sprint_bouts}")