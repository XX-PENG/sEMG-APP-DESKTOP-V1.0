"""肌肉与步态页：8 路 sEMG 按解剖学排布 + 逐步离心负荷表。

波形不再叫 CH1..CH8：行是肌肉对（ST/BF/VL/RF，按通道序），列是左右腿，同一块肌肉的两侧上下相邻，
不对称在波形幅度上直接可见。右侧逐步表把每一步的末端摆动负荷摊开，供回放复核。
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..guard.model import MUSCLES, GuardSnapshot, Thresholds, channel_of
from . import theme as T
from .widgets import StatTile, card

STRIDE_COLS = ["课次s", "间隔s", "峰dps", "冲击g", "末端%", "提前ms", "负荷"]


class MuscleView(QWidget):
    def __init__(self, cfg, th: Thresholds, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.th = th
        self.span = 2.0
        self.frozen = False
        self._curves: dict[str, pg.PlotCurveItem] = {}
        self._env: dict[str, pg.PlotCurveItem] = {}
        self._plots: dict[str, pg.PlotItem] = {}

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(10)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("窗口"))
        self.sp_span = QDoubleSpinBox()
        self.sp_span.setRange(0.2, 12.0)
        self.sp_span.setSingleStep(0.5)
        self.sp_span.setValue(self.span)
        self.sp_span.setSuffix(" s")
        self.sp_span.valueChanged.connect(self._set_span)
        ctrl.addWidget(self.sp_span)
        ctrl.addWidget(QLabel("排布"))
        self.cb_layout = QComboBox()
        self.cb_layout.addItems(["肌肉对 4×2", "单列堆叠"])
        self.cb_layout.currentIndexChanged.connect(self._rebuild)
        ctrl.addWidget(self.cb_layout)
        self.chk_env = QCheckBox("叠加包络")
        self.chk_env.setChecked(True)
        self.chk_env.toggled.connect(self._rebuild)
        ctrl.addWidget(self.chk_env)
        self.chk_freeze = QCheckBox("冻结读数")
        self.chk_freeze.toggled.connect(self._set_frozen)
        ctrl.addWidget(self.chk_freeze)
        ctrl.addStretch(1)
        self.lbl_info = QLabel("")
        self.lbl_info.setObjectName("Dim")
        ctrl.addWidget(self.lbl_info)
        lv.addLayout(ctrl)

        self.gfx = pg.GraphicsLayoutWidget()
        self.gfx.setBackground(T.C["bg1"])
        lv.addWidget(self.gfx, 1)
        self._rebuild()

        right = QWidget()
        right.setFixedWidth(470)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(10)
        f_cc, l_cc = card("共激活与配比", "腘绳肌 / 股四头肌同时收缩程度")
        row = QHBoxLayout()
        self.t_cc_l = StatTile("左共激活", "%", "blue")
        self.t_cc_r = StatTile("右共激活", "%", "blue")
        row.addWidget(self.t_cc_l)
        row.addWidget(self.t_cc_r)
        l_cc.addLayout(row)
        row2 = QHBoxLayout()
        self.t_bf_l = StatTile("左 BF 占比", "%", "magenta")
        self.t_bf_r = StatTile("右 BF 占比", "%", "magenta")
        row2.addWidget(self.t_bf_l)
        row2.addWidget(self.t_bf_r)
        l_cc.addLayout(row2)
        rv.addWidget(f_cc)

        f_str, l_str = card("逐步离心负荷", "最近 24 步 · 末端摆动窗 140 ms")
        self.tbl = QTableWidget(0, len(STRIDE_COLS))
        self.tbl.setHorizontalHeaderLabels(STRIDE_COLS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setStyleSheet("QTableWidget::item{padding:2px 4px;} QHeaderView::section{padding:7px 4px;}")
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        l_str.addWidget(self.tbl, 1)
        rv.addWidget(f_str, 1)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setSizes([900, 470])
        root.addWidget(split)

    def _set_span(self, v: float) -> None:
        self.span = float(v)

    def _set_frozen(self, on: bool) -> None:
        self.frozen = on

    def _rebuild(self, *_a) -> None:
        self.gfx.clear()
        self._curves, self._env, self._plots = {}, {}, {}
        grid = self.cb_layout.currentIndex() == 0
        show_env = self.chk_env.isChecked()
        pairs = [("st", 0), ("bf", 1), ("vl", 2), ("rf", 3)]
        for abbr, row in pairs:
            for side, col in (("L", 0), ("R", 1)):
                mid = f"{abbr}_{side.lower()}"
                m = next(x for x in MUSCLES if x.id == mid)
                if grid:
                    p = self.gfx.addPlot(row=row, col=col)
                else:
                    p = self.gfx.addPlot(row=row * 2 + col, col=0)
                p.setTitle(m.full_label, color=m.color, size="11px")
                p.showGrid(x=True, y=True, alpha=0.14)
                p.setLabel("bottom", "s", color=T.C["dim"])
                p.setLabel("left", "ADC", color=T.C["dim"])
                p.getViewBox().setMouseMode(pg.ViewBox.PanMode)
                p.disableAutoRange()
                for ax in ("bottom", "left"):
                    p.getAxis(ax).setPen(pg.mkPen(T.C["border"]))
                    p.getAxis(ax).setTextPen(pg.mkPen(T.C["dim"]))
                if self._plots:
                    p.setXLink(next(iter(self._plots.values())))
                self._plots[mid] = p
                curve = p.plot(pen=pg.mkPen(m.color, width=1), autoDownsample=True,
                               clipToView=True, downsampleMethod="peak")
                self._curves[mid] = curve
                if show_env:
                    self._env[mid] = p.plot(pen=pg.mkPen(T.C["text_warm"], width=1.4,
                                                         style=Qt.DashLine),
                                            autoDownsample=True, clipToView=True)

    def update_data(self, windows: dict, envs: dict, t_now: float) -> None:
        if self.frozen:
            return
        by_channel = {channel_of(m.id): m.id for m in MUSCLES}
        active = 0
        for ch, (t, v) in windows.items():
            mid = by_channel.get(ch)
            if mid is None or mid not in self._curves:
                continue
            x = t - t_now if t.size else np.empty(0)
            self._curves[mid].setData(x, v)
            if v.size:
                active += 1
                lo, hi = float(np.min(v)), float(np.max(v))
                pad = max((hi - lo) * 0.12, 200.0)
                self._plots[mid].setYRange(lo - pad, hi + pad, padding=0)
            if mid in self._env:
                te, ee = envs.get(ch, (np.empty(0), np.empty(0)))
                self._env[mid].setData(te - t_now if te.size else np.empty(0), ee)
        for p in self._plots.values():
            p.setXRange(-self.span, 0.05, padding=0)
        self.lbl_info.setText(f"有数据 {active}/8 · 窗口 {self.span:.1f}s"
                              + (" · 已冻结" if self.frozen else ""))

    def update_snapshot(self, snap: GuardSnapshot, t0: float = 0.0) -> None:
        self.t_cc_l.set_value(f"{snap.cci_l:.0f}", "2·min/(H+Q)")
        self.t_cc_r.set_value(f"{snap.cci_r:.0f}", "2·min/(H+Q)")
        self.t_bf_l.set_value(f"{snap.bf_share_l:.0f}", "BF/(BF+ST)")
        self.t_bf_r.set_value(f"{snap.bf_share_r:.0f}", "BF/(BF+ST)")
        rows = list(reversed(snap.strides[-24:]))
        self.tbl.setRowCount(len(rows))
        for r, s in enumerate(rows):
            hot = s.peak_dps > self.th.swing_warn
            vals = [f"{s.t - t0:.1f}", f"{s.interval:.3f}", f"{s.peak_dps:.0f}", f"{s.impact_g:.2f}",
                    f"{s.terminal_ham:.0f}", f"{s.lead_ms:.0f}", f"{s.load_index:.0f}"]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if hot and c in (2, 6):
                    item.setForeground(QColor(T.C["danger"]))
                if c == 5 and s.lead_ms < self.th.lead_warn:
                    item.setForeground(QColor(T.C["warn"]))
                self.tbl.setItem(r, c, item)