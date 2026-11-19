"""疲劳与负荷页：频谱下移 + 激活上升 = 疲劳；课次负荷 / 慢性负荷 = 急性负荷比。

MDF 趋势画四条腘绳肌曲线而不是八条全画：疲劳判读只关心腘绳肌，股四头肌作为对照放在
激活趋势里。负荷侧把高速时长、冲刺段与 ACWR 摊开，回答"今天还能不能上强度"。
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..guard.model import HAM_IDS, MUSCLES, GuardSnapshot, Thresholds
from . import theme as T
from .widgets import Sparkline, StatTile, card

MDF_COLS = ["肌肉", "MDF", "基线", "变化", "斜率%/min"]


class FatigueView(QWidget):
    def __init__(self, th: Thresholds, parent=None) -> None:
        super().__init__(parent)
        self.th = th
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.t_fat = StatTile("疲劳指数", "/100", "warn")
        self.t_mdf = StatTile("MDF 下降速率", "%/min", "magenta")
        self.t_rms = StatTile("RMS 漂移", "%", "orange")
        self.t_hs = StatTile("高速跑时长", "s", "blue")
        self.t_bout = StatTile("冲刺段", "段", "turf")
        self.t_acwr = StatTile("急性负荷比", "", "lime")
        for t in (self.t_fat, self.t_mdf, self.t_rms, self.t_hs, self.t_bout, self.t_acwr):
            tiles.addWidget(t)
        root.addLayout(tiles)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(12)

        f_mdf, l_mdf = card("腘绳肌中位频率趋势", "下移 = 疲劳 · 窗 45 s 回归")
        self.gfx_mdf = pg.GraphicsLayoutWidget()
        self.gfx_mdf.setBackground(T.C["bg1"])
        self.p_mdf = self.gfx_mdf.addPlot()
        self.p_mdf.showGrid(x=True, y=True, alpha=0.14)
        self.p_mdf.setLabel("bottom", "课次时间", units="s", color=T.C["dim"])
        self.p_mdf.setLabel("left", "MDF", units="Hz", color=T.C["dim"])
        self.p_mdf.addLegend(offset=(-8, 8), labelTextColor=T.C["text"])
        for ax in ("bottom", "left"):
            self.p_mdf.getAxis(ax).setPen(pg.mkPen(T.C["border"]))
            self.p_mdf.getAxis(ax).setTextPen(pg.mkPen(T.C["dim"]))
        self._mdf_curves = {}
        for mid in HAM_IDS:
            m = next(x for x in MUSCLES if x.id == mid)
            self._mdf_curves[mid] = self.p_mdf.plot(pen=pg.mkPen(m.color, width=1.6), name=m.label)
        l_mdf.addWidget(self.gfx_mdf, 1)
        lv.addWidget(f_mdf, 3)

        f_act, l_act = card("激活趋势", "腘绳肌 vs 股四头肌 %MVC")
        self.gfx_act = pg.GraphicsLayoutWidget()
        self.gfx_act.setBackground(T.C["bg1"])
        self.p_act = self.gfx_act.addPlot()
        self.p_act.showGrid(x=True, y=True, alpha=0.14)
        self.p_act.setLabel("bottom", "课次时间", units="s", color=T.C["dim"])
        self.p_act.setLabel("left", "%MVC", color=T.C["dim"])
        self.p_act.addLegend(offset=(-8, 8), labelTextColor=T.C["text"])
        for ax in ("bottom", "left"):
            self.p_act.getAxis(ax).setPen(pg.mkPen(T.C["border"]))
            self.p_act.getAxis(ax).setTextPen(pg.mkPen(T.C["dim"]))
        self._act_curves = {
            "ham_l": self.p_act.plot(pen=pg.mkPen(T.C["magenta"], width=1.6), name="左·腘绳肌"),
            "ham_r": self.p_act.plot(pen=pg.mkPen("#FFA8C8", width=1.6), name="右·腘绳肌"),
            "quad_l": self.p_act.plot(pen=pg.mkPen(T.C["blue"], width=1.4), name="左·股四头肌"),
            "quad_r": self.p_act.plot(pen=pg.mkPen("#9CC9FF", width=1.4), name="右·股四头肌"),
        }
        l_act.addWidget(self.gfx_act, 1)
        lv.addWidget(f_act, 2)
        split.addWidget(left)

        right = QWidget()
        right.setFixedWidth(392)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(12)
        f_tbl, l_tbl = card("逐肌肉频谱", "基线 = 课次开始稳定值")
        self.tbl = QTableWidget(0, len(MDF_COLS))
        self.tbl.setHorizontalHeaderLabels(MDF_COLS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        l_tbl.addWidget(self.tbl)
        rv.addWidget(f_tbl)

        f_sp, l_sp = card("负荷累积", "课次负荷 AU")
        self.spark_load = Sparkline("turf")
        l_sp.addWidget(self.spark_load)
        self.lbl_load = QLabel("")
        self.lbl_load.setObjectName("Muted")
        self.lbl_load.setWordWrap(True)
        l_sp.addWidget(self.lbl_load)
        rv.addWidget(f_sp)

        f_note, l_note = card("判读", "")
        self.lbl_note = QLabel("")
        self.lbl_note.setObjectName("Muted")
        self.lbl_note.setWordWrap(True)
        l_note.addWidget(self.lbl_note)
        rv.addWidget(f_note, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setSizes([880, 392])
        root.addWidget(split, 1)
        self._load_hist: list[float] = []

    def update_data(self, engine, snap: GuardSnapshot, session_t: float) -> None:
        self.t_fat.set_value(f"{snap.fatigue_index:.0f}", "0–100", T.risk_color(snap.fatigue_index))
        self.t_mdf.set_value(f"{snap.mdf_slope:+.1f}", f"阈值 {self.th.mdf_warn:.0f}",
                             T.C["danger"] if snap.mdf_slope < self.th.mdf_warn else T.C["safe"])
        self.t_rms.set_value(f"{snap.rms_drift:+.0f}", "同等输出下上升 = 疲劳")
        self.t_hs.set_value(f"{snap.high_speed_seconds:.0f}", f"角速度 > {self.th.swing_warn:.0f} dps")
        self.t_bout.set_value(f"{snap.sprint_bouts}", "≥2 s 高速段落")
        self.t_acwr.set_value(f"{snap.acwr:.2f}", f"上限 {self.th.acwr_warn:.2f}",
                              T.C["danger"] if snap.acwr > self.th.acwr_warn else T.C["safe"])

        t0 = snap.t - session_t
        for mid, curve in self._mdf_curves.items():
            hist = list(engine.mdf_history(mid))
            if len(hist) >= 2:
                t = np.array([p[0] for p in hist]) - t0
                v = np.array([p[1] for p in hist])
                curve.setData(t[::2], v[::2])
        act = list(engine.activation_history())
        if len(act) >= 2:
            t = np.array([p[0] for p in act]) - t0
            for key, idx in (("ham_l", 1), ("ham_r", 2), ("quad_l", 3), ("quad_r", 4)):
                if len(act[0]) > idx:
                    v = np.array([p[idx] for p in act])
                    self._act_curves[key].setData(t[::3], v[::3])

        self.tbl.setRowCount(len(MUSCLES))
        for r, m in enumerate(MUSCLES):
            rd = snap.muscles.get(m.id)
            base = engine.baseline_mdf(m.id)
            cur = rd.mdf if rd else 0.0
            delta = (cur - base) / base * 100.0 if base > 0 and cur > 0 else 0.0
            slope = rd.mdf_slope if rd else 0.0
            vals = [m.label, f"{cur:.0f}" if cur else "—", f"{base:.0f}" if base else "—",
                    f"{delta:+.1f}%" if cur else "—", f"{slope:+.1f}"]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter if c else Qt.AlignLeft | Qt.AlignVCenter)
                if c == 4 and slope < self.th.mdf_warn:
                    item.setForeground(QColor(T.C["danger"]))
                if c == 0:
                    item.setForeground(QColor(m.color))
                self.tbl.setItem(r, c, item)

        self._load_hist.append(snap.load_au)
        self.spark_load.set_values(self._load_hist)
        self.lbl_load.setText(
            f"本次 {snap.load_au:.0f} AU · 近 7 日均值 {engine.profile.chronic_load:.0f} AU · "
            f"活动 {snap.active_seconds / 60:.1f} min")
        self.lbl_note.setText(self._note(snap))

    def _note(self, snap: GuardSnapshot) -> str:
        lines = []
        if snap.mdf_slope < self.th.mdf_warn and snap.rms_drift > 5:
            lines.append("• MDF 下移伴 RMS 上升：典型外周疲劳图谱，建议轮换下场。")
        elif snap.mdf_slope < self.th.mdf_warn:
            lines.append("• MDF 下移但 RMS 未升：可能是募集策略改变而非疲劳，复核动作。")
        if snap.acwr > self.th.acwr_warn:
            lines.append(f"• 急性负荷比 {snap.acwr:.2f} 超上限：本次课次负荷已高于近期承受量。")
        if snap.fatigue_index < 25 and snap.acwr <= self.th.acwr_warn:
            lines.append("• 频谱与负荷均在安全区，可继续当前课目。")
        if not lines:
            lines.append("• 观察中：疲劳与负荷尚未越限。")
        return "\n".join(lines)

    def reset_session(self) -> None:
        self._load_hist.clear()
        for c in self._mdf_curves.values():
            c.setData([], [])
        for c in self._act_curves.values():
            c.setData([], [])