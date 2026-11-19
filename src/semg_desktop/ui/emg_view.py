"""sEMG 8 通道实时波形。

x 轴用相对秒（右端 0），因为时标是按 seq 重建设备时间，绝对数值没有阅读价值。
曲线一律开 peak 降采样 + clipToView：8 通道 2 kHz 全量约 3.2 万点/秒，直接喂给 QPainter
会在窗口拉到 10 s 以上时掉帧。
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal

from ..guard.model import muscle_by_channel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

STACKED = "堆叠"
OVERLAY = "叠加"
GRID = "2x4 网格"
_EPS = 1e-9


class EmgView(QWidget):
    region_selected = Signal(float, float)

    def __init__(self, cfg, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.n = cfg.semg.channel_count
        self.span = cfg.display_span_seconds
        self.frozen = False
        self._curves: list[pg.PlotCurveItem] = []
        self._env_curves: list[pg.PlotCurveItem] = []
        self._plots: list[pg.PlotItem] = []
        self._region: pg.LinearRegionItem | None = None

        self.gfx = pg.GraphicsLayoutWidget()
        self.gfx.setBackground("k")

        self.cb_span = QDoubleSpinBox()
        self.cb_span.setRange(0.1, 30.0)
        self.cb_span.setSingleStep(0.5)
        self.cb_span.setDecimals(2)
        self.cb_span.setSuffix(" s")
        self.cb_span.setValue(self.span)
        self.cb_span.valueChanged.connect(self._set_span)

        self.cb_mode = QComboBox()
        self.cb_mode.addItems([STACKED, OVERLAY, GRID])
        self.cb_mode.currentTextChanged.connect(self._rebuild)

        self.chk_env = QCheckBox("叠加包络")
        self.chk_env.toggled.connect(self._rebuild)
        self.chk_auto_y = QCheckBox("自适应纵向")
        self.chk_auto_y.setChecked(True)
        self.chk_region = QCheckBox("分析选区")
        self.chk_region.toggled.connect(self._toggle_region)
        self.chk_freeze = QCheckBox("冻结画面")
        self.chk_freeze.toggled.connect(self._set_frozen)

        form = QFormLayout()
        form.addRow("窗口", self.cb_span)
        form.addRow("排布", self.cb_mode)
        form.addRow("", self.chk_env)
        form.addRow("", self.chk_auto_y)
        form.addRow("", self.chk_region)
        form.addRow("", self.chk_freeze)
        box = QGroupBox("显示")
        box.setLayout(form)

        self.lbl_info = QLabel("等待数据…")
        self.lbl_info.setWordWrap(True)
        side = QVBoxLayout()
        side.addWidget(box)
        side.addWidget(self.lbl_info)
        side.addStretch(1)

        root = QHBoxLayout(self)
        root.addWidget(self.gfx, 1)
        root.addLayout(side)

        self._rebuild()

    # ── 控件 ────────────────────────────────────────────────────────────────

    def _set_span(self, value: float) -> None:
        self.span = float(value)

    def _set_frozen(self, on: bool) -> None:
        self.frozen = on
        self.lbl_info.setText("画面已冻结" if on else "跟随实时")

    def _toggle_region(self, on: bool) -> None:
        if self.mode == GRID:
            on = False
            self.chk_region.setChecked(False)
        if on:
            self._add_region(self._plots[0])
        else:
            self._remove_region()
        if on:
            self._emit_region()

    def _add_region(self, plot: pg.PlotItem) -> None:
        if self._region is not None:
            return
        # 只靠 setVisible(False) 关不掉两个拖拽手柄，它们是被单独加进 plot 的 item，
        # 所以关掉时真的要 removeItem
        self._region = pg.LinearRegionItem(values=[-0.5, 0.0], movable=True,
                                           brush=pg.mkBrush(120, 120, 255, 40))
        self._region.sigRegionChangeFinished.connect(self._emit_region)
        plot.addItem(self._region)

    def _remove_region(self) -> None:
        if self._region is None:
            return
        for plot in self._plots:
            plot.removeItem(self._region)
        self._region.sigRegionChangeFinished.disconnect(self._emit_region)
        self._region = None

    def _emit_region(self) -> None:
        if self._region is not None:
            lo, hi = self._region.getRegion()
            self.region_selected.emit(float(lo), float(hi))

    @property
    def mode(self) -> str:
        return self.cb_mode.currentText()

    def _ch_color(self, i: int) -> str:
        m = muscle_by_channel(i)
        return m.color if m else self.cfg.semg.colors[i % len(self.cfg.semg.colors)]

    def _ch_name(self, i: int) -> str:
        m = muscle_by_channel(i)
        return f"CH{i + 1} · {m.label}" if m else f"CH{i + 1}"

    def refresh_channels(self) -> None:
        """贴点映射交换后，按当前肌肉身份重刷颜色与通道名。"""
        self._rebuild()

    # ── 布局 ────────────────────────────────────────────────────────────────

    def _rebuild(self, *_a) -> None:
        self.gfx.clear()
        self._curves, self._env_curves, self._plots = [], [], []
        self._region = None
        show_env = self.chk_env.isChecked()
        mode = self.mode

        if mode == GRID:
            for i in range(self.n):
                p = self.gfx.addPlot(row=i % 4, col=i // 4)
                self._style(p, ylabel="ADC 定点值")
                p.setTitle(self._ch_name(i), color=self._ch_color(i), size="11px")
                if self._plots:
                    p.setXLink(self._plots[0])
                self._plots.append(p)
                self._curves.append(self._add_curve(p, i, name=self._ch_name(i)))
                if show_env:
                    self._env_curves.append(self._add_curve(p, i, env=True))
            return

        p = self.gfx.addPlot(row=0, col=0)
        self._style(p, ylabel="ADC 定点值 + 通道偏移" if mode == STACKED else "ADC 定点值")
        p.addLegend(offset=(-8, 8), labelTextColor="w")
        self._plots = [p]
        for i in range(self.n):
            self._curves.append(self._add_curve(p, i, name=self._ch_name(i)))
        if show_env:
            self._env_curves = [self._add_curve(p, i, env=True) for i in range(self.n)]
        if self.chk_region.isChecked():
            self._add_region(p)

    def _style(self, p: pg.PlotItem, ylabel: str) -> None:
        p.setLabel("bottom", "相对时间", units="s")
        p.setLabel("left", ylabel)
        p.showGrid(x=True, y=True, alpha=0.25)
        p.getViewBox().setMouseMode(pg.ViewBox.PanMode)
        p.disableAutoRange()

    def _add_curve(self, plot: pg.PlotItem, index: int, name: str | None = None, env: bool = False):
        color = self._ch_color(index)
        pen = pg.mkPen(color, width=1, style=Qt.DashLine if env else Qt.SolidLine)
        curve = plot.plot(
            pen=pen,
            name=name,
            autoDownsample=True,
            clipToView=True,
            downsampleMethod="peak",
        )
        curve.setZValue(10 if env else 1)
        return curve

    # ── 刷新 ────────────────────────────────────────────────────────────────

    def update_data(self, windows: dict[int, tuple[np.ndarray, np.ndarray]],
                    envs: dict[int, tuple[np.ndarray, np.ndarray]], t_now: float) -> None:
        if self.frozen:
            return
        stacked = self.mode == STACKED
        spacing = self._spacing(windows) if stacked else 0.0

        for i, curve in enumerate(self._curves):
            t, v = windows.get(i, (np.empty(0), np.empty(0)))
            x = t - t_now if t.size else np.empty(0)
            y = v + spacing * i if (stacked and v.size) else v
            curve.setData(x, y)

        if self.chk_env.isChecked():
            for i, curve in enumerate(self._env_curves):
                te, ee = envs.get(i, (np.empty(0), np.empty(0)))
                x = te - t_now if te.size else np.empty(0)
                y = ee + spacing * i if (stacked and ee.size) else ee
                curve.setData(x, y)

        if self.mode == GRID:
            for i, p in enumerate(self._plots):
                p.setXRange(-self.span, 0.05, padding=0)
                if self.chk_auto_y.isChecked():
                    self._fit_y(p, windows.get(i, (np.empty(0), np.empty(0)))[1])
        else:
            p = self._plots[0]
            p.setXRange(-self.span, 0.05, padding=0)
            if self.chk_auto_y.isChecked():
                if stacked:
                    amp = max(spacing, _EPS)
                    p.setYRange(-amp, spacing * (self.n - 1) + amp, padding=0.02)
                else:
                    allv = np.concatenate([v for _t, v in windows.values() if v.size]) if any(
                        v.size for _t, v in windows.values()) else np.empty(0)
                    self._fit_y(p, allv)
        self.lbl_info.setText(self._info_text(windows, spacing))

    def _fit_y(self, plot: pg.PlotItem, v: np.ndarray) -> None:
        if v.size == 0:
            return
        lo, hi = float(np.min(v)), float(np.max(v))
        pad = max((hi - lo) * 0.12, 200.0)
        plot.setYRange(lo - pad, hi + pad, padding=0)

    def _spacing(self, windows) -> float:
        if not self.chk_auto_y.isChecked():
            return float(self.cfg.semg.baseline_offset)
        peak = 0.0
        for _t, v in windows.values():
            if v.size:
                peak = max(peak, float(np.max(np.abs(v))))
        return max(1000.0, 2.6 * peak)

    def _info_text(self, windows, spacing: float) -> str:
        active = sum(1 for _t, v in windows.values() if v.size)
        total = sum(v.size for _t, v in windows.values())
        return (
            f"有数据通道 {active}/{self.n}\n窗口样本 {total:,}\n"
            f"{'通道间距 ' + format(spacing, ',.0f') if self.mode == STACKED else '纵向自适应' if self.chk_auto_y.isChecked() else '纵向固定'}"
        )
