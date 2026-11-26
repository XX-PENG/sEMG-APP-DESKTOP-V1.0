"""传感器布置页：贴点布置、通道与肌肉映射、设备在线状态。

链路健康、原始信号和固件升级由主窗口的"工程调试"页承载，场边布置页只保留教练需要的
传感器操作。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..guard.model import (
    MUSCLES,
    GuardSnapshot,
    Thresholds,
    channel_of,
    muscle_assignment,
    muscle_by_channel,
    muscle_by_id,
    set_muscle_assignment,
)
from . import theme as T
from .widgets import _leg_paths, card
from .analysis_view import AnalysisView
from .emg_view import EmgView
from .imu_view import ImuView
from .status_panel import StatusPanel

MAP_COLS = ["通道", "肌肉", "设备 MAC", "状态", "速率", "帧数", "丢帧"]

_TOP_RX, _TOP_RY = 56.0, 42.0     # 大腿横断面设计半径
# 解剖槽位（横断面设计坐标，前侧朝上；x 以外侧为正，乘 side 系数镜像）：
# RF 前正中、VL 外侧偏前、BF 后外侧、ST 后内侧，与大腿横断面解剖一致。
_SLOTS = {
    "rf": (0.0, -26.0, 14.0, 11.0),
    "vl": (33.0, -8.0, 15.0, 14.0),
    "bf": (26.0, 24.0, 15.0, 12.0),
    "st": (-16.0, 28.0, 12.0, 11.0),
}
_IMU_SLOT = (-24.0, 4.0)          # 股骨内侧空白（x 以外侧为正，乘 side 系数镜像），IMU 吸附位


def _slot_x(side: str, g: str) -> float:
    return _SLOTS[g][0] * (-1.0 if side == "L" else 1.0)


def _posterior_xy(g: str, side: str) -> tuple[float, float]:
    """后面观贴点 = 俯视图槽位的投影：横轴=内外侧位置，纵深轴折到竖直方向。

    后面观不可拖动，位置完全由俯视图槽位推导；腘绳肌（后侧）在上，股四头肌（前侧投影）在下。
    """
    x = _slot_x(side, g) * 0.62
    y = 59.0 - _SLOTS[g][1] * 0.8
    y = min(110.0, max(8.0, y))
    half_w = 34.0 * (1.12 - 0.60 * y / 118.0) - 10.0
    return min(half_w, max(-half_w, x)), y


class SensorMap(QWidget):
    """传感器布置：左大腿俯视图 | 后面观 | 右大腿俯视图。

    贴点吸附在俯视图的四个肌肉槽位上，拖到另一槽位即交换两个传感器，通道→肌肉的数据
    映射同步改写（引擎/波形/清单全部跟随）；后面观由槽位自动投影，不可拖动。
    IMU 吸附横断面中央空白，拖到另一条腿即切换佩戴侧。
    """

    assignment_changed = Signal()
    worn_side_changed = Signal(str)

    def __init__(self, th: Thresholds, parent=None) -> None:
        super().__init__(parent)
        self.th = th
        self.rows: list[dict] = []
        self.snap: GuardSnapshot | None = None
        self._drag: tuple[str, str] | None = None      # ("emg", 肌肉id) / ("imu", 佩戴侧)
        self._drag_pos: QPointF | None = None          # 拖拽幽灵点（控件坐标）
        self._drag_target: str | None = None           # 交换目标槽位 id / IMU 目标侧
        self._hover: tuple[str, str] | None = None
        self.setMinimumHeight(300)
        self.setMouseTracking(True)

    def set_data(self, rows: list[dict], snap: GuardSnapshot | None) -> None:
        self.rows = rows
        self.snap = snap
        self.update()

    def reset_mapping(self) -> None:
        set_muscle_assignment({m.id: m.channel for m in MUSCLES})
        self.assignment_changed.emit()
        self.update()

    # ── 几何 ────────────────────────────────────────────────────────────────

    def _layout(self) -> dict:
        w, h = self.width(), self.height()
        pw = w / 3.0
        s1 = max(0.4, min(pw / 220.0, (h - 70) / 250.0))
        pcx = w / 2.0
        s2 = max(0.45, min((pw - 16) / 112.0, (h - 90) / 142.0, 2.0))
        return {"w": w, "h": h, "pw": pw, "s1": s1, "top1": 40.0,
                "cx1": {"L": pcx - 46 * s1, "R": pcx + 46 * s1},
                "s2": s2, "cy2": (h - 18) / 2.0,
                "cx2": {"L": pw / 2.0, "R": w - pw / 2.0}}

    def _slot_pos(self, mid: str, lay: dict) -> QPointF:
        m = muscle_by_id(mid)
        g = m.id.split("_")[0]
        s = lay["s2"]
        return QPointF(lay["cx2"][m.side] + _slot_x(m.side, g) * s,
                       lay["cy2"] + _SLOTS[g][1] * s)

    def _imu_pos(self, side: str, lay: dict) -> QPointF:
        s = lay["s2"]
        outer = -1.0 if side == "L" else 1.0
        return QPointF(lay["cx2"][side] + _IMU_SLOT[0] * outer * s,
                       lay["cy2"] + _IMU_SLOT[1] * s)

    def _hit(self, pos: QPointF) -> tuple[str, str] | None:
        """只命中俯视图：贴点 / 中央 IMU。后面观仅供悬停读数，不参与拖拽。"""
        lay = self._layout()
        s = lay["s2"]
        best: tuple[str, str] | None = None
        best_d = 1.0
        p = self._imu_pos(self.th.worn_side, lay)
        d = math.hypot((pos.x() - p.x()) / max(15 * s, 11), (pos.y() - p.y()) / max(10 * s, 8))
        if d <= best_d:
            best, best_d = ("imu", self.th.worn_side), d
        for m in MUSCLES:
            p = self._slot_pos(m.id, lay)
            d = math.hypot(pos.x() - p.x(), pos.y() - p.y()) / max(10 * s, 9)
            if d <= best_d:
                best, best_d = ("emg", m.id), d
        return best

    def _hover_info(self, pos: QPointF) -> tuple[str, str] | None:
        hit = self._hit(pos)
        if hit:
            return hit
        lay = self._layout()
        s = lay["s1"]
        for m in MUSCLES:
            x, y = _posterior_xy(m.id.split("_")[0], m.side)
            px = lay["cx1"][m.side] + x * s
            py = lay["top1"] + y * s
            if math.hypot(pos.x() - px, pos.y() - py) <= max(10 * s, 8):
                return ("post", m.id)
        return None

    # ── 拖拽交换 ────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        hit = self._hit(event.position())
        if hit:
            self._drag = hit
            self._drag_pos = event.position()
            self._drag_target = None
            self.setCursor(Qt.ClosedHandCursor)
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if self._drag is None:
            hover = self._hover_info(pos)
            if hover != self._hover:
                self._hover = hover
                draggable = hover is not None and hover[0] != "post"
                self.setCursor(Qt.PointingHandCursor if draggable else Qt.ArrowCursor)
                self.update()
            return
        self._drag_pos = pos
        lay = self._layout()
        kind, key = self._drag
        if kind == "imu":
            self._drag_target = ("L" if pos.x() < lay["pw"] else
                                 "R" if pos.x() > 2 * lay["pw"] else None)
            if self._drag_target == self.th.worn_side:
                self._drag_target = None
        else:
            s = lay["s2"]
            best, best_d = None, max(20 * s, 16)
            for m in MUSCLES:
                if m.id == key:
                    continue
                p = self._slot_pos(m.id, lay)
                d = math.hypot(pos.x() - p.x(), pos.y() - p.y())
                if d <= best_d:
                    best, best_d = m.id, d
            self._drag_target = best
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag is None:
            return
        kind, key = self._drag
        target = self._drag_target
        if kind == "emg" and target:
            mapping = muscle_assignment()
            mapping[key], mapping[target] = mapping[target], mapping[key]
            set_muscle_assignment(mapping)
            self.assignment_changed.emit()
        elif kind == "imu" and target and target != self.th.worn_side:
            self.th.worn_side = target
            self.worn_side_changed.emit(target)
        self._drag = None
        self._drag_pos = None
        self._drag_target = None
        self.setCursor(Qt.ArrowCursor)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = None
        if self._drag is None:
            self.setCursor(Qt.ArrowCursor)
        self.update()

    # ── 绘制 ────────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        lay = self._layout()
        w, h, pw = lay["w"], lay["h"], lay["pw"]
        online = {r["index"]: r for r in self.rows if r["kind"] == "semg" and r["index"] is not None}
        imu_row = next((r for r in self.rows if r["kind"] == "imu"), None)
        imu_ok = bool(imu_row and imu_row["online"])

        p.setFont(T.font(11, QFont.DemiBold))
        p.setPen(QColor(T.C["muted"]))
        p.drawText(QRectF(0, 6, pw, 18), Qt.AlignCenter, "左大腿 · 俯视图（前侧朝上）")
        p.drawText(QRectF(pw, 6, pw, 18), Qt.AlignCenter, "后面观 · 自动投影（不可拖动）")
        p.drawText(QRectF(2 * pw, 6, pw, 18), Qt.AlignCenter, "右大腿 · 俯视图（前侧朝上）")
        p.setPen(QPen(QColor(30, 68, 83, 120), 1, Qt.DashLine))
        p.drawLine(QPointF(pw, 30), QPointF(pw, h - 28))
        p.drawLine(QPointF(2 * pw, 30), QPointF(2 * pw, h - 28))

        self._paint_posterior(p, lay, online, imu_ok)
        self._paint_top(p, lay, "L", online, imu_ok)
        self._paint_top(p, lay, "R", online, imu_ok)
        self._paint_drag(p, lay, online)
        self._paint_info(p, lay)
        p.end()

    def _draw_dot(self, p, x: float, y: float, s: float, col: QColor, text: str,
                  active: bool) -> None:
        r = min((10.5 if active else 9.0) * s, 13.0)
        if active:
            halo = QColor(col)
            halo.setAlpha(70)
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            p.drawEllipse(QPointF(x, y), r + 4, r + 4)
        p.setPen(QPen(col, 2))
        p.setBrush(QColor(5, 12, 17, 230))
        p.drawEllipse(QPointF(x, y), r, r)
        p.setFont(T.font(max(8, int(min(9 * s, 12))), QFont.Bold, mono=True))
        p.setPen(col)
        p.drawText(QRectF(x - r, y - r, 2 * r, 2 * r), Qt.AlignCenter, text)

    def _draw_imu(self, p, x: float, y: float, s: float, ok: bool, active: bool) -> None:
        col = QColor(T.C["turf"] if ok else T.C["danger"])
        rw, rh = min((14.0 if active else 13.0) * s, 18.0), min((9.0 if active else 8.0) * s, 11.0)
        if active:
            halo = QColor(col)
            halo.setAlpha(70)
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            p.drawRoundedRect(QRectF(x - rw - 3, y - rh - 3, 2 * (rw + 3), 2 * (rh + 3)), 7, 7)
        p.setPen(QPen(col, 2))
        p.setBrush(QColor(5, 12, 17, 230))
        p.drawRoundedRect(QRectF(x - rw, y - rh, 2 * rw, 2 * rh), 4, 4)
        p.setFont(T.font(max(7, int(min(8 * s, 11))), QFont.Bold))
        p.setPen(col)
        p.drawText(QRectF(x - rw, y - rh, 2 * rw, 2 * rh), Qt.AlignCenter, "IMU")

    def _paint_posterior(self, p, lay, online, imu_ok) -> None:
        s, top, h = lay["s1"], lay["top1"], lay["h"]
        pcx = lay["w"] / 2.0
        pelvis = QRectF(pcx - 80 * s, top - 24 * s, 160 * s, 28 * s)
        p.setPen(QPen(QColor(30, 68, 83, 200), 1.3))
        p.setBrush(QColor(18, 44, 55, 150))
        p.drawRoundedRect(pelvis, 8 * s, 8 * s)

        for side in ("L", "R"):
            cx = lay["cx1"][side]
            paths = _leg_paths(cx, top, s, side)
            p.setPen(QPen(QColor(38, 82, 99, 210), 1.4))
            p.setBrush(QColor(11, 28, 36, 190))
            p.drawPath(paths["thigh"])
            p.drawPath(paths["shank"])

            for m in MUSCLES:
                if m.side != side:
                    continue
                g = m.id.split("_")[0]
                x, y = _posterior_xy(g, side)
                px, py = cx + x * s, top + y * s
                ch = channel_of(m.id)
                row = online.get(ch)
                is_online = bool(row and row["online"])
                col = QColor(m.color if is_online else T.C["danger"])
                self._draw_dot(p, px, py, s, col, str(ch + 1), self._hover == ("post", m.id))
                p.setFont(T.font(max(7, int(8 * s)), QFont.DemiBold))
                p.setPen(QColor(m.color if is_online else T.C["dim"]))
                ly = py - 21 * s if g in ("bf", "st") else py + 11 * s
                p.drawText(QRectF(px - 15 * s, ly, 30 * s, 11 * s), Qt.AlignCenter, m.abbr)

            if self.th.worn_side == side:
                x, y = _posterior_xy_imu(side)
                self._draw_imu(p, cx + x * s, top + y * s, s, imu_ok, False)
            p.setFont(T.font(11, QFont.DemiBold))
            p.setPen(QColor(T.C["muted"]))
            p.drawText(QRectF(cx - 30 * s, h - 42, 60 * s, 16), Qt.AlignCenter,
                       "左腿" if side == "L" else "右腿")

    def _paint_top(self, p, lay, side: str, online, imu_ok) -> None:
        s, cx, cy, pw = lay["s2"], lay["cx2"][side], lay["cy2"], lay["pw"]
        h = lay["h"]
        x0 = 0.0 if side == "L" else 2 * pw
        rx, ry = _TOP_RX, _TOP_RY
        outer = -1.0 if side == "L" else 1.0

        # IMU 跨腿拖放目标高亮（落点提示在底部信息条，避免与肌肉名称引线重叠）
        if self._drag and self._drag[0] == "imu" and self._drag_target == side:
            p.setPen(QPen(QColor(T.C["turf"]), 1.8, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), (rx + 6) * s, (ry + 6) * s)

        p.setPen(QPen(QColor(38, 82, 99, 220), 1.5))
        p.setBrush(QColor(11, 28, 36, 190))
        p.drawEllipse(QPointF(cx, cy), rx * s, ry * s)
        p.setPen(QPen(QColor(30, 68, 83, 140), 1, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), (rx - 6) * s, (ry - 6) * s)
        # 股骨：断面中央骨性标志（IMU 吸附在其内侧空白，不覆盖股骨）
        p.setPen(QPen(QColor(130, 163, 175, 150), 1.2))
        p.setBrush(QColor(18, 44, 55, 200))
        p.drawEllipse(QPointF(cx, cy + 2 * s), 9 * s, 9 * s)
        if 9 * s >= 10:
            p.setFont(T.font(max(6, int(7 * s))))
            p.setPen(QColor(T.C["dim"]))
            p.drawText(QRectF(cx - 10 * s, cy - 4 * s, 20 * s, 12 * s), Qt.AlignCenter, "股骨")

        # 四个肌肉槽位 + 解剖名称引线（RF 前中、VL 外前、BF 后外、ST 后内）
        callouts = {
            "rf": (QPointF(cx, cy - (ry + 13) * s), QPointF(cx, cy - 36 * s)),
            "vl": (QPointF(cx + outer * 34 * s, cy - (ry + 29) * s),
                   QPointF(cx + outer * 40 * s, cy - 15 * s)),
            "bf": (QPointF(cx + outer * 30 * s, cy + (ry + 13) * s),
                   QPointF(cx + outer * 31 * s, cy + 33 * s)),
            "st": (QPointF(cx - outer * 30 * s, cy + (ry + 29) * s),
                   QPointF(cx - outer * 20 * s, cy + 36 * s)),
        }
        for g, (bx, by, bw, bh) in _SLOTS.items():
            m = muscle_by_id(f"{g}_{side.lower()}")
            bx *= outer
            ch = channel_of(m.id)
            row = online.get(ch)
            is_online = bool(row and row["online"])
            base = QColor(m.color)
            fill = QColor(base)
            fill.setAlpha(52 if is_online else 26)
            edge = QColor(base)
            edge.setAlpha(150 if is_online else 70)
            p.setBrush(fill)
            p.setPen(QPen(edge, 1.3))
            p.drawEllipse(QPointF(cx + bx * s, cy + by * s), bw * s, bh * s)

            anchor, blob_edge = callouts[g]
            leader = QColor(base)
            leader.setAlpha(110)
            p.setPen(QPen(leader, 1))
            p.drawLine(blob_edge, anchor)
            p.setFont(T.font(max(9, int(min(10 * s, 11))), QFont.DemiBold))
            p.setPen(QColor(m.color))
            p.drawText(QRectF(anchor.x() - 55, anchor.y() - 8, 110, 16),
                       Qt.AlignCenter, f"{m.name} {m.abbr}")

        # 交换目标槽位高亮
        if self._drag and self._drag[0] == "emg" and self._drag_target:
            tm = muscle_by_id(self._drag_target)
            if tm.side == side:
                tg = tm.id.split("_")[0]
                tx, ty, tw, th_ = _SLOTS[tg]
                p.setPen(QPen(QColor(T.C["turf"]), 2, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(cx + tx * outer * s, cy + ty * s),
                              (tw + 4) * s, (th_ + 4) * s)

        for g in _SLOTS:
            m = muscle_by_id(f"{g}_{side.lower()}")
            pos = self._slot_pos(m.id, lay)
            ch = channel_of(m.id)
            row = online.get(ch)
            is_online = bool(row and row["online"])
            col = QColor(m.color if is_online else T.C["danger"])
            dragging = self._drag == ("emg", m.id)
            if dragging:
                # 原位留虚线空槽，幽灵点随指针
                p.setPen(QPen(col, 1.5, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                r = min(9 * s, 13)
                p.drawEllipse(pos, r, r)
                continue
            active = self._hover == ("emg", m.id)
            self._draw_dot(p, pos.x(), pos.y(), s, col, str(ch + 1), active)

        if self.th.worn_side == side and not (self._drag and self._drag[0] == "imu"):
            pos = self._imu_pos(side, lay)
            self._draw_imu(p, pos.x(), pos.y(), s, imu_ok, self._hover == ("imu", side))

        p.setFont(T.font(8))
        p.setPen(QColor(T.C["dim"]))
        compass = "前↑ 后↓ 外← 内→" if side == "L" else "前↑ 后↓ 外→ 内←"
        p.drawText(QRectF(x0 + 6, h - 40, pw - 12, 12), Qt.AlignCenter, compass)

    def _paint_drag(self, p, lay, online) -> None:
        if not self._drag or self._drag_pos is None:
            return
        kind, key = self._drag
        pos = self._drag_pos
        s = lay["s2"]
        if kind == "imu":
            imu_row = next((r for r in self.rows if r["kind"] == "imu"), None)
            self._draw_imu(p, pos.x(), pos.y(), s, bool(imu_row and imu_row["online"]), True)
        else:
            m = muscle_by_id(key)
            ch = channel_of(key)
            row = online.get(ch)
            col = QColor(m.color if row and row["online"] else T.C["danger"])
            self._draw_dot(p, pos.x(), pos.y(), s, col, str(ch + 1), True)

    def _paint_info(self, p, lay) -> None:
        w, h = lay["w"], lay["h"]
        text = "俯视图拖动贴点到另一肌肉位置即交换 · 数据映射同步更新 · IMU 可拖到另一条腿"
        col = T.C["dim"]
        if self._drag:
            kind, key = self._drag
            if kind == "imu":
                if self._drag_target:
                    text = (f"松开 → {'左' if self._drag_target == 'L' else '右'}腿"
                            " · 佩戴侧与步态判读随之换侧")
                else:
                    text = "拖到另一条腿的横断面即换侧 · 松开空白处还原"
                col = T.C["turf"]
            else:
                m = muscle_by_id(key)
                ch = channel_of(key)
                if self._drag_target:
                    tm = muscle_by_id(self._drag_target)
                    text = (f"松开交换：CH{ch + 1} {m.full_label} ↔ "
                            f"CH{channel_of(self._drag_target) + 1} {tm.full_label}")
                    col = T.C["turf"]
                else:
                    text = f"CH{ch + 1} · {m.full_label} — 拖到另一肌肉位置交换，松开空白处还原"
                    col = m.color
        elif self._hover:
            kind, key = self._hover
            if kind == "imu":
                text = (f"IMU · 大腿{'左' if self.th.worn_side == 'L' else '右'}侧"
                        " — 吸附横断面中央空白，拖到另一条腿即换侧")
                col = T.C["turf"]
            else:
                m = muscle_by_id(key)
                text = f"CH{channel_of(key) + 1} · {m.full_label} — {m.role}"
                col = m.color
        p.setFont(T.font(10))
        p.setPen(QColor(col))
        fm = p.fontMetrics()
        p.drawText(QRectF(8, h - 22, w - 16, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(text, Qt.ElideRight, int(w - 16)))


def _posterior_xy_imu(side: str) -> tuple[float, float]:
    outer = -1.0 if side == "L" else 1.0
    x = _IMU_SLOT[0] * outer * 0.62
    y = 59.0 - _IMU_SLOT[1] * 0.8
    half_w = 34.0 * (1.12 - 0.60 * y / 118.0) - 13.0
    return min(half_w, max(-half_w, x)), y


class DevicesView(QWidget):
    def __init__(self, cfg, th: Thresholds, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.th = th
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        place = QWidget()
        pv = QVBoxLayout(place)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(12)
        heading = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("传感器布置")
        title.setObjectName("PageTitle")
        hint = QLabel("按图示贴好传感器，再开始课次")
        hint.setObjectName("PageHint")
        title_box.addWidget(title)
        title_box.addWidget(hint)
        heading.addLayout(title_box)
        heading.addStretch(1)
        online = QLabel("EMG ×8 + 大腿 IMU")
        online.setObjectName("CardHint")
        heading.addWidget(online)
        pv.addLayout(heading)
        hbox = QHBoxLayout()
        hbox.setSpacing(10)
        f_map, l_map = card("贴点位置", "拖动圆点可交换两个位置")
        self.sensor_map = SensorMap(th)
        l_map.addWidget(self.sensor_map, 1)
        row_map = QHBoxLayout()
        instruction = QLabel("多数情况使用默认布置；位置不一致时再拖动交换。")
        instruction.setObjectName("Dim")
        row_map.addWidget(instruction)
        row_map.addStretch(1)
        self.btn_reset_place = QPushButton("恢复默认")
        self.btn_reset_place.setObjectName("Ghost")
        self.btn_reset_place.setToolTip("恢复通道与肌肉贴点的默认对应")
        self.btn_reset_place.clicked.connect(self.sensor_map.reset_mapping)
        row_map.addWidget(self.btn_reset_place)
        l_map.addLayout(row_map)
        hbox.addWidget(f_map, 1)
        f_side, l_side = card("通道对应", "在线状态实时更新")
        self.place_labels: dict[str, QLabel] = {}
        for m in MUSCLES:
            lab = QLabel(f"CH{channel_of(m.id) + 1} · {m.full_label}")
            lab.setStyleSheet(f"color:{m.color}; background:transparent; font-size:12px;")
            self.place_labels[m.id] = lab
            l_side.addWidget(lab)
        self.lbl_imu_place = QLabel("IMU · 大腿矢状面")
        self.lbl_imu_place.setStyleSheet(f"color:{T.C['turf']}; background:transparent; font-size:12px;")
        l_side.addWidget(self.lbl_imu_place)
        l_side.addStretch(1)
        self.lbl_place_hint = QLabel("圆点编号对应采集通道。只有实际贴错位置时才拖动交换；IMU 可拖到另一条腿。"
                                     "完整链路信息和诊断数据位于左侧“工程调试”。")
        self.lbl_place_hint.setObjectName("Dim")
        self.lbl_place_hint.setWordWrap(True)
        l_side.addWidget(self.lbl_place_hint)
        f_side.setFixedWidth(280)
        hbox.addWidget(f_side)
        pv.addLayout(hbox, 7)
        f_tbl, l_tbl = card("设备与通道", "离线设备请检查电源或连接")
        self.tbl = QTableWidget(0, len(MAP_COLS))
        self.tbl.setHorizontalHeaderLabels(MAP_COLS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setMinimumHeight(140)
        l_tbl.addWidget(self.tbl)
        pv.addWidget(f_tbl, 3)
        root.addWidget(place)

        self.status = StatusPanel(cfg)
        self.debug = QTabWidget()
        self.emg = EmgView(cfg)
        self.imu = ImuView(cfg)
        self.analysis = AnalysisView(cfg)
        self.debug.addTab(self.emg, "原始 sEMG ×8")
        self.debug.addTab(self.imu, "IMU 九分量")
        self.debug.addTab(self.analysis, "频谱 / 统计")

    def update_data(self, st, rows, status_log, snap) -> None:
        self.status.update_data(st, rows, status_log)
        self.sensor_map.set_data(rows, snap)
        by_ch = {r["index"]: r for r in rows if r["kind"] == "semg" and r["index"] is not None}
        for m in MUSCLES:
            ch = channel_of(m.id)
            r = by_ch.get(ch)
            ok = bool(r and r["online"])
            rate = f"{r['rate']:,.0f}/s" if r else "—"
            self.place_labels[m.id].setText(
                f"CH{ch + 1} · {m.full_label} · {'在线' if ok else '离线'} {rate}")
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
            m = muscle_by_channel(row["index"]) if row["index"] is not None else None
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