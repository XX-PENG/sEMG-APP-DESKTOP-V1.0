"""肌电分析视图：量化指标表、包络分层、功率谱。

统计窗口有两种来源：自己设的固定窗口，或者沿用波形视图拖出来的选区。选区换算成
(span, offset) 交给管线取数，offset 是窗口右端距当前时刻的秒数。
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
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..analytics import psd_db
from ..guard.model import muscle_by_channel

COLUMNS = ["通道", "样本", "实测速率", "峰峰值", "RMS", "MAV", "iEMG", "int16削顶"]


class AnalysisView(QWidget):
    def __init__(self, cfg, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.n = cfg.semg.channel_count
        self.span = cfg.analytics.stats_window_seconds
        self.acq = None
        self._selection: tuple[float, float] | None = None
        self._psd_step = 0

        self.tbl = QTableWidget(self.n, len(COLUMNS))
        self.tbl.setHorizontalHeaderLabels(COLUMNS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(False)
        self.tbl.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)

        self.env_plot = pg.PlotWidget()
        self.env_plot.setLabel("bottom", "相对时间", units="s")
        self.env_plot.setLabel("left", "包络（整流 + 4 Hz 低通）")
        self.env_plot.showGrid(x=True, y=True, alpha=0.25)
        self.env_plot.addLegend(offset=(-8, 8), labelTextColor="w")
        self.env_curves = [
            self.env_plot.plot(pen=pg.mkPen(self._ch_color(i), width=1), name=self._ch_name(i))
            for i in range(self.n)
        ]

        self.psd_plot = pg.PlotWidget(logX=True)
        self.psd_plot.setLabel("bottom", "频率", units="Hz")
        self.psd_plot.setLabel("left", "PSD", units="dB/Hz")
        self.psd_plot.showGrid(x=True, y=True, alpha=0.25)
        self.psd_curve = self.psd_plot.plot(pen=pg.mkPen("#7fd1ff", width=1))

        self.cb_span = QDoubleSpinBox()
        self.cb_span.setRange(0.05, 30.0)
        self.cb_span.setDecimals(2)
        self.cb_span.setSuffix(" s")
        self.cb_span.setValue(self.span)
        self.cb_span.valueChanged.connect(self._set_span)

        self.cb_channel = QComboBox()
        self.cb_channel.addItems([self._ch_name(i) for i in range(self.n)])

        self.chk_selection = QCheckBox("沿用波形选区")
        self.chk_selection.toggled.connect(self._toggle_selection)
        self.lbl_selection = QLabel("选区：未启用")
        self.lbl_selection.setStyleSheet("color:#aaa")

        form = QFormLayout()
        form.addRow("统计窗口", self.cb_span)
        form.addRow("谱通道", self.cb_channel)
        form.addRow("", self.chk_selection)
        form.addRow("", self.lbl_selection)
        box = QGroupBox("分析设置")
        box.setLayout(form)

        left = QVBoxLayout()
        left.addWidget(QLabel("通道指标（整窗）"))
        left.addWidget(self.tbl, 1)
        left.addWidget(box)
        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setMinimumWidth(380)

        stack = QSplitter(Qt.Vertical)
        stack.addWidget(self.env_plot)
        stack.addWidget(self.psd_plot)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left_widget)
        split.addWidget(stack)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(split)

    # ── 窗口选择 ────────────────────────────────────────────────────────────

    def set_selection(self, lo: float, hi: float) -> None:
        if not self.chk_selection.isChecked():
            return
        self._selection = (float(lo), float(hi))
        self.lbl_selection.setText(f"选区：{lo:.2f} … {hi:.2f} s")

    def _toggle_selection(self, on: bool) -> None:
        if not on:
            self._selection = None
            self.lbl_selection.setText("选区：未启用")

    def _set_span(self, value: float) -> None:
        self.span = float(value)

    def window(self) -> tuple[float, float]:
        """返回 (span, offset)，offset 为窗口右端相对当前时刻的回退秒数。"""
        if self._selection is not None:
            lo, hi = self._selection
            return max(0.02, hi - lo), max(0.0, -hi)
        return self.span, 0.0

    def _ch_color(self, i: int) -> str:
        m = muscle_by_channel(i)
        return m.color if m else self.cfg.semg.colors[i % len(self.cfg.semg.colors)]

    def _ch_name(self, i: int) -> str:
        m = muscle_by_channel(i)
        return f"CH{i + 1} · {m.label}" if m else f"CH{i + 1}"

    def refresh_channels(self) -> None:
        """贴点映射交换后，按当前肌肉身份重刷包络颜色/图例与谱通道名。"""
        for c in self.env_curves:
            self.env_plot.removeItem(c)
        self.env_curves = [
            self.env_plot.plot(pen=pg.mkPen(self._ch_color(i), width=1), name=self._ch_name(i))
            for i in range(self.n)
        ]
        for i in range(self.n):
            self.cb_channel.setItemText(i, self._ch_name(i))

    # ── 刷新 ────────────────────────────────────────────────────────────────

    def update_data(self, t_now: float) -> None:
        if self.acq is None:
            return
        span, offset = self.window()
        metrics = self.acq.metrics(span, offset)
        envs = self.acq.env_windows(span, offset)

        for i in range(self.n):
            m = metrics.get(i)
            ch_name = self._ch_name(i)
            if m is None or not getattr(m, "count", 0):
                cells = [ch_name] + ["—"] * 7
            else:
                cells = [
                    ch_name,
                    f"{m.count:,}",
                    f"{m.rate:,.0f}/s",
                    f"{m.p2p:,.0f}",
                    f"{m.rms:,.0f}",
                    f"{m.mav:,.0f}",
                    f"{m.iemg:.3e}",
                    f"{m.saturated}",
                ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 0:
                    item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                    item.setForeground(QColor(self._ch_color(i)))
                self.tbl.setItem(i, c, item)

        top = max((float(np.max(ee)) for _t, ee in envs.values() if ee.size), default=1.0)
        top = max(top, 1.0)
        step = top * 1.15
        for i, curve in enumerate(self.env_curves):
            te, ee = envs.get(i, (np.empty(0), np.empty(0)))
            x = te - (t_now - offset) if te.size else np.empty(0)
            curve.setData(x, ee + step * i)
        self.env_plot.setXRange(-span - 0.02, 0.02, padding=0)
        self.env_plot.setYRange(-top * 0.1, step * (self.n - 1) + top, padding=0.02)

        self._psd_step += 1
        if self._psd_step % 5 == 0:
            self._update_psd(span, offset)

    def _update_psd(self, span: float, offset: float) -> None:
        if self.acq is None:
            return
        index = self.cb_channel.currentIndex()
        t, v = self.acq.raw_window(index, span, offset)
        if v.size < 64 or t.size < 2:
            return
        span_sec = float(t[-1] - t[0])
        fs = (v.size - 1) / span_sec if span_sec > 0 else self.cfg.protocol.semg_nominal_rate
        freqs, db = psd_db(v.astype(np.float64), fs, nperseg=self.cfg.analytics.psd_samples)
        if freqs.size:
            self.psd_curve.setData(freqs, db)
            lo, hi = float(db.min()), float(db.max())
            pad = max((hi - lo) * 0.12, 3.0)
            self.psd_plot.disableAutoRange(axis=1)
            self.psd_plot.setYRange(lo - pad, hi + pad * 2, padding=0)
