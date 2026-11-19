"""设备与固件页：传感器布置 / 链路健康 / 工程调试 / OTA。

把工程向的东西收在这一页，业务页不被串口计数和固件进度打扰；但一条都不丢——链路容量、
解析异常、seq 缺口、OTA 两阶段升级都还在，只是换了位置。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..guard.model import MUSCLES, GuardSnapshot, Thresholds
from . import theme as T
from .widgets import _leg_paths, card
from .analysis_view import AnalysisView
from .emg_view import EmgView
from .imu_view import ImuView
from .ota_view import OtaView
from .status_panel import StatusPanel

MAP_COLS = ["通道", "肌肉", "MAC 后 3 字节", "状态", "实测速率", "帧", "丢帧缺口"]


class SensorMap(QWidget):
    """传感器布置示意：8 个 EMG 贴点 + 大腿 IMU，在线与否一眼可见。"""

    def __init__(self, th: Thresholds, parent=None) -> None:
        super().__init__(parent)
        self.th = th
        self.rows: list[dict] = []
        self.snap: GuardSnapshot | None = None
        self.setMinimumHeight(280)

    def set_data(self, rows: list[dict], snap: GuardSnapshot | None) -> None:
        self.rows = rows
        self.snap = snap
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        scale = min(w / 300.0, (h - 40) / 250.0)
        scale = max(0.4, scale)
        top = 34.0
        pcx = w / 2.0
        online = {r["index"]: r for r in self.rows if r["kind"] == "semg" and r["index"] is not None}

        p.setFont(T.font(11, QFont.DemiBold))
        p.setPen(QColor(T.C["muted"]))
        p.drawText(QRectF(0, 6, w, 18), Qt.AlignCenter, "贴点布置 · 后面观（VL/RF 为前面观贴点，投影显示）")
        pelvis = QRectF(pcx - 62 * scale, top - 24 * scale, 124 * scale, 28 * scale)
        p.setPen(QPen(QColor(30, 68, 83, 200), 1.3))
        p.setBrush(QColor(18, 44, 55, 150))
        p.drawRoundedRect(pelvis, 8 * scale, 8 * scale)

        for side in ("L", "R"):
            cx = pcx + (-38 if side == "L" else 38) * scale
            paths = _leg_paths(cx, top, scale, side)
            p.setBrush(QColor(11, 28, 36, 190))
            p.drawPath(paths["thigh"])
            p.drawPath(paths["shank"])
            for g, dy in (("bf", 40), ("st", 66), ("vl", 40), ("rf", 66)):
                mid = f"{g}_{side.lower()}"
                m = next(x for x in MUSCLES if x.id == mid)
                row = online.get(m.channel)
                is_online = bool(row and row["online"])
                ox = (-17 if g in ("bf", "vl") else 15) * (-1 if side == "L" else 1)
                if g in ("vl", "rf"):
                    ox = -ox
                x = cx + ox * scale
                y = top + dy * scale
                col = QColor(m.color if is_online else T.C["danger"])
                p.setPen(QPen(col, 2))
                p.setBrush(QColor(5, 12, 17, 230))
                p.drawEllipse(QPointF(x, y), 9 * scale, 9 * scale)
                p.setFont(T.font(max(8, int(9 * scale)), QFont.Bold, mono=True))
                p.setPen(col)
                p.drawText(QRectF(x - 9 * scale, y - 7 * scale, 18 * scale, 14 * scale),
                           Qt.AlignCenter, str(m.channel + 1))
            if self.th.worn_side == side:
                y = top + 100 * scale
                imu_row = next((r for r in self.rows if r["kind"] == "imu"), None)
                ok = bool(imu_row and imu_row["online"])
                col = QColor(T.C["turf"] if ok else T.C["danger"])
                p.setPen(QPen(col, 2))
                p.setBrush(QColor(5, 12, 17, 230))
                p.drawRoundedRect(QRectF(cx - 12 * scale, y - 8 * scale, 24 * scale, 16 * scale), 4, 4)
                p.setFont(T.font(max(8, int(8 * scale)), QFont.Bold))
                p.setPen(col)
                p.drawText(QRectF(cx - 12 * scale, y - 8 * scale, 24 * scale, 16 * scale),
                           Qt.AlignCenter, "IMU")
            p.setFont(T.font(11, QFont.DemiBold))
            p.setPen(QColor(T.C["muted"]))
            p.drawText(QRectF(cx - 30 * scale, h - 20, 60 * scale, 16), Qt.AlignCenter,
                       "左腿" if side == "L" else "右腿")
        p.end()


class DevicesView(QWidget):
    def __init__(self, cfg, th: Thresholds, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.th = th
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        self.inner = QTabWidget()
        root.addWidget(self.inner)

        place = QWidget()
        pv = QVBoxLayout(place)
        pv.setContentsMargins(0, 10, 0, 0)
        pv.setSpacing(10)
        hbox = QHBoxLayout()
        hbox.setSpacing(10)
        f_map, l_map = card("传感器布置", "圆点编号 = 采集通道")
        self.sensor_map = SensorMap(th)
        l_map.addWidget(self.sensor_map, 1)
        hbox.addWidget(f_map, 1)
        f_side, l_side = card("贴点清单", "EMG ×8 + 大腿 IMU")
        self.place_labels: dict[str, QLabel] = {}
        for m in MUSCLES:
            lab = QLabel(f"CH{m.channel + 1} · {m.full_label}")
            lab.setStyleSheet(f"color:{m.color}; background:transparent; font-size:12px;")
            self.place_labels[m.id] = lab
            l_side.addWidget(lab)
        self.lbl_imu_place = QLabel("IMU · 大腿矢状面")
        self.lbl_imu_place.setStyleSheet(f"color:{T.C['turf']}; background:transparent; font-size:12px;")
        l_side.addWidget(self.lbl_imu_place)
        l_side.addStretch(1)
        self.lbl_place_hint = QLabel("佩戴侧与矢状面轴在实时守护页的判读控制区调整。")
        self.lbl_place_hint.setObjectName("Dim")
        self.lbl_place_hint.setWordWrap(True)
        l_side.addWidget(self.lbl_place_hint)
        f_side.setFixedWidth(280)
        hbox.addWidget(f_side)
        pv.addLayout(hbox, 3)
        f_tbl, l_tbl = card("通道映射", "MAC 定位置，重连不串台")
        self.tbl = QTableWidget(0, len(MAP_COLS))
        self.tbl.setHorizontalHeaderLabels(MAP_COLS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        l_tbl.addWidget(self.tbl)
        pv.addWidget(f_tbl, 2)
        self.inner.addTab(place, "传感器布置")

        self.status = StatusPanel(cfg)
        self.inner.addTab(self.status, "链路与解析")

        debug = QTabWidget()
        self.emg = EmgView(cfg)
        self.imu = ImuView(cfg)
        self.analysis = AnalysisView(cfg)
        debug.addTab(self.emg, "原始 sEMG ×8")
        debug.addTab(self.imu, "IMU 九分量")
        debug.addTab(self.analysis, "频谱 / 统计")
        self.inner.addTab(debug, "工程调试")

        self.ota = OtaView(cfg)
        self.inner.addTab(self.ota, "固件升级 (OTA)")

    def update_data(self, st, rows, status_log, snap) -> None:
        self.status.update_data(st, rows, status_log)
        self.sensor_map.set_data(rows, snap)
        by_ch = {r["index"]: r for r in rows if r["kind"] == "semg" and r["index"] is not None}
        for m in MUSCLES:
            r = by_ch.get(m.channel)
            ok = bool(r and r["online"])
            rate = f"{r['rate']:,.0f}/s" if r else "—"
            self.place_labels[m.id].setText(
                f"CH{m.channel + 1} · {m.full_label} · {'在线' if ok else '离线'} {rate}")
            self.place_labels[m.id].setStyleSheet(
                f"color:{m.color if ok else T.C['danger']}; background:transparent; font-size:12px;")
        imu = next((r for r in rows if r["kind"] == "imu"), None)
        self.lbl_imu_place.setText(
            f"IMU · 大腿{'左' if self.th.worn_side == 'L' else '右'}侧 · "
            f"{'在线' if imu and imu['online'] else '离线'} · 轴 {'XYZ'[self.th.sagittal_axis]}")
        self.tbl.setRowCount(len([r for r in rows if r["kind"] == "semg"]))
        r_i = 0
        for row in rows:
            if row["kind"] != "semg":
                continue
            m = next((x for x in MUSCLES if x.channel == row["index"]), None)
            vals = [f"CH{row['index'] + 1}" if row["index"] is not None else "未映射",
                    m.full_label if m else "—", row["mac"],
                    "在线" if row["online"] else "离线",
                    f"{row['rate']:,.0f}/s", f"{row['frames']:,}", f"{row['seq_gaps']:,}"]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if c >= 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 3:
                    item.setForeground(QColor(T.C["safe"] if row["online"] else T.C["danger"]))
                if c == 1 and m:
                    item.setForeground(QColor(m.color))
                self.tbl.setItem(r_i, c, item)
            r_i += 1