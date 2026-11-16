"""IMU 九分量曲线 + 姿态视图。"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .attitude import AttitudeWidget

GROUPS = [
    ("加速度 (g)", ["ax", "ay", "az"], 3, 0),
    ("角速度 (dps)", ["gx", "gy", "gz"], 3, 3),
    ("磁力计 (原始 int16，未标定)", ["mx", "my", "mz"], 3, 6),
]
XYZ = [pg.mkPen("#e6194b", width=1), pg.mkPen("#3cb44b", width=1), pg.mkPen("#4363d8", width=1)]


class ImuView(QWidget):
    def __init__(self, cfg, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.span = cfg.display_span_seconds
        self.gfx = pg.GraphicsLayoutWidget()
        self.gfx.setBackground("k")

        self.plots: list[pg.PlotItem] = []
        self.curves: list[list[pg.PlotCurveItem]] = []
        for title, names, count, offset in GROUPS:
            p = self.gfx.addPlot(row=len(self.plots), col=0)
            p.setLabel("bottom", "相对时间", units="s")
            p.setLabel("left", title)
            p.showGrid(x=True, y=True, alpha=0.25)
            p.disableAutoRange()
            if self.plots:
                p.setXLink(self.plots[0])
            self.plots.append(p)
            self.curves.append(
                [
                    p.plot(pen=XYZ[j], name=names[j], autoDownsample=True, clipToView=True, downsampleMethod="peak")
                    for j in range(count)
                ]
            )

        self.attitude = AttitudeWidget(smoothing=0.9)
        self.tbl = QTableWidget(9, 2)
        self.tbl.setHorizontalHeaderLabels(["分量", "当前值"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.setMaximumHeight(230)
        labels = ["ax (g)", "ay (g)", "az (g)", "gx (dps)", "gy (dps)", "gz (dps)", "mx", "my", "mz"]
        for i, text in enumerate(labels):
            self.tbl.setItem(i, 0, QTableWidgetItem(text))
            self.tbl.setItem(i, 1, QTableWidgetItem("—"))

        self.chk_mag = QCheckBox("显示磁力计")
        self.chk_mag.setChecked(cfg.imu.show_magnetometer)
        self.chk_mag.toggled.connect(self._toggle_mag)
        self.chk_auto_y = QCheckBox("自适应纵向")
        self.chk_auto_y.setChecked(True)
        self.chk_attitude = QCheckBox("姿态视图")
        self.chk_attitude.setChecked(True)
        self.chk_attitude.toggled.connect(self.attitude.setVisible)

        box = QGroupBox("IMU 读数")
        grid = QGridLayout(box)
        grid.addWidget(self.chk_mag, 0, 0)
        grid.addWidget(self.chk_auto_y, 0, 1)
        grid.addWidget(self.chk_attitude, 1, 0)
        self.lbl_hint = QLabel(
            "换算依据网关侧还原：ax/ay/az = raw/1000 (g)，gx/gy/gz = raw/10 (dps)，"
            "磁力计为原始 int16。roll/pitch 由重力向量求得，不含航向。"
        )
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color:#aaa")

        side = QVBoxLayout()
        side.addWidget(box)
        side.addWidget(self.attitude, 1)
        side.addWidget(self.tbl)
        side.addWidget(self.lbl_hint)

        root = QHBoxLayout(self)
        root.addWidget(self.gfx, 3)
        root.addLayout(side, 1)
        self._toggle_mag(self.chk_mag.isChecked())

    def _toggle_mag(self, on: bool) -> None:
        self.plots[2].setVisible(on)

    def update_data(self, t: np.ndarray, samples: np.ndarray, t_now: float) -> None:
        x = t - t_now if t.size else np.empty(0)
        for gi, (title, _names, count, offset) in enumerate(GROUPS):
            for j in range(count):
                col = samples[:, offset + j] if samples.size else np.empty(0)
                self.curves[gi][j].setData(x, col)
            if self.chk_auto_y.isChecked() and samples.size:
                block = samples[:, offset : offset + count]
                lo, hi = float(np.min(block)), float(np.max(block))
                pad = max((hi - lo) * 0.12, 0.05)
                self.plots[gi].setYRange(lo - pad, hi + pad, padding=0)
        if not self.chk_auto_y.isChecked():
            for gi, p in enumerate(self.plots):
                p.setYRange(-2.0, 2.0, padding=0)
        for p in self.plots:
            p.setXRange(-self.span, 0.05, padding=0)

    def set_latest(self, vec: np.ndarray | None) -> None:
        if vec is None:
            return
        for i in range(9):
            fmt = f"{vec[i]:.3f}" if i < 6 else f"{vec[i]:.0f}"
            self.tbl.setItem(i, 1, QTableWidgetItem(fmt))
        self.attitude.set_sample(vec[:3], vec[3:6])
