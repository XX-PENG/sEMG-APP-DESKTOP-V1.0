"""自绘控件：把运动医学量画成场边一眼能读的形状。

这些不是装饰性图表，每个形状都绑定一个真实判读量：肌肉热力图的填充度是 %MVC，
步态相位带的品红段是末端摆动（拉伤发生时相），镜像条的左右长度差就是不对称。
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..guard.model import MUSCLES, GuardSnapshot, risk_band
from . import theme as T


def card(title: str = "", hint: str = "", parent=None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(parent)
    frame.setObjectName("Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(16, 13, 16, 14)
    lay.setSpacing(9)
    if title:
        head = QHBoxLayout()
        lab = QLabel(title)
        lab.setObjectName("CardTitle")
        head.addWidget(lab)
        head.addStretch(1)
        if hint:
            h = QLabel(hint)
            h.setObjectName("CardHint")
            head.addWidget(h)
        lay.addLayout(head)
    return frame, lay


class StatTile(QFrame):
    """一个指标块：标题 / 大数字 / 单位 / 脚注，左侧色条表明归属。"""

    def __init__(self, title: str, unit: str = "", accent: str = "turf", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._accent = T.C.get(accent, accent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 11, 12, 11)
        lay.setSpacing(2)
        self.lbl_title = QLabel(title)
        self.lbl_title.setObjectName("Dim")
        row = QHBoxLayout()
        row.setSpacing(5)
        self.lbl_value = QLabel("—")
        self.lbl_value.setObjectName("Metric")
        self.lbl_unit = QLabel(unit)
        self.lbl_unit.setObjectName("MetricUnit")
        self.lbl_unit.setAlignment(Qt.AlignBottom)
        row.addWidget(self.lbl_value)
        row.addWidget(self.lbl_unit)
        row.addStretch(1)
        self.lbl_sub = QLabel("")
        self.lbl_sub.setObjectName("CardHint")
        self.lbl_sub.setWordWrap(True)
        lay.addWidget(self.lbl_title)
        lay.addLayout(row)
        lay.addWidget(self.lbl_sub)
        self.setMinimumHeight(96)

    def set_value(self, text: str, sub: str = "", color: str | None = None) -> None:
        self.lbl_value.setText(text)
        self.lbl_value.setStyleSheet(
            f"color:{color or self._accent}; font-family:'{T.MONO_FAMILY}';"
            f"font-size:29px; font-weight:700; background:transparent;"
        )
        if sub:
            self.lbl_sub.setText(sub)

    def set_accent(self, accent: str) -> None:
        self._accent = T.C.get(accent, accent)


class ShellBackground(QWidget):
    """纯色工作区，避免装饰背景干扰数据判读。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor(T.C["bg1"]))
        self.setPalette(palette)


class RiskGauge(QWidget):
    """0–100 风险仪表：绿→琥珀→红的弧，高危时外圈呼吸。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.score = 0.0
        self.available = False
        self.setMinimumSize(210, 178)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_score(self, score: float, available: bool = True) -> None:
        if abs(score - self.score) > 0.05 or available != self.available:
            self.score = score
            self.available = available
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h * 0.60
        r = min(w * 0.40, h * 0.50)
        start, span = 210.0, -240.0     # 从左下逆时针扫到右下

        if self.score >= 65 and self.available:
            halo = QColor(T.C["danger"])
            halo.setAlpha(30)
            p.setBrush(halo)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(cx - r * 1.55, cy - r * 1.55,
                                 r * 3.1, r * 3.1))

        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        p.setPen(QPen(QColor(T.C["border"]), 13, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(rect, int(start * 16), int(span * 16))

        for i in range(0, 11):
            ang = math.radians(start + span * i / 10.0)
            r1, r2 = r - 12, r - 18
            tick = QColor(T.C["dim"])
            tick.setAlpha(150)
            p.setPen(QPen(tick, 1.4))
            p.drawLine(QPointF(cx + r1 * math.cos(ang), cy - r1 * math.sin(ang)),
                       QPointF(cx + r2 * math.cos(ang), cy - r2 * math.sin(ang)))

        if self.available and self.score > 0.5:
            frac = min(1.0, self.score / 100.0)
            p.setPen(QPen(QColor(T.risk_color(self.score)), 13, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, int(start * 16), int(span * frac * 16))
            ang = math.radians(start + span * frac)
            ex, ey = cx + r * math.cos(ang), cy - r * math.sin(ang)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T.risk_color(self.score)))
            p.drawEllipse(QPointF(ex, ey), 5.5, 5.5)

        name, col, _ = risk_band(self.score)
        p.setPen(QColor(col if self.available else T.C["dim"]))
        f = T.font(42, QFont.DemiBold, mono=True)
        p.setFont(f)
        p.drawText(QRectF(cx - r, cy - 34, 2 * r, 48), Qt.AlignCenter,
                   f"{self.score:.0f}" if self.available else "—")
        p.setFont(T.font(12, QFont.DemiBold))
        p.setPen(QColor(col if self.available else T.C["dim"]))
        p.drawText(QRectF(cx - r, cy + 14, 2 * r, 20), Qt.AlignCenter,
                   f"{name}风险" if self.available else "等待数据")
        p.setFont(T.font(10))
        p.setPen(QColor(T.C["dim"]))
        p.drawText(QRectF(0, h - 17, w, 15), Qt.AlignCenter, "腘绳肌拉伤综合指数 0–100")
        p.end()


def _leg_paths(cx: float, top: float, scale: float, side: str) -> dict[str, QPainterPath]:
    """一条腿的大腿/小腿轮廓与四块肌肉的示意区域（后面观 BF/ST，前面观 VL/RF）。"""
    s = scale
    thigh_w, thigh_h = 34 * s, 118 * s
    outer = -1.0 if side == "L" else 1.0     # 后面观：左腿外侧朝画面左
    paths: dict[str, QPainterPath] = {}

    thigh = QPainterPath()
    thigh.moveTo(cx - thigh_w, top)
    thigh.cubicTo(cx - thigh_w * 1.12, top + thigh_h * 0.45,
                  cx - thigh_w * 0.72, top + thigh_h * 0.82,
                  cx - thigh_w * 0.52, top + thigh_h)
    thigh.lineTo(cx + thigh_w * 0.52, top + thigh_h)
    thigh.cubicTo(cx + thigh_w * 0.72, top + thigh_h * 0.82,
                  cx + thigh_w * 1.12, top + thigh_h * 0.45,
                  cx + thigh_w, top)
    thigh.closeSubpath()
    paths["thigh"] = thigh

    shank = QPainterPath()
    kt = top + thigh_h + 7 * s
    shank.moveTo(cx - thigh_w * 0.50, kt)
    shank.cubicTo(cx - thigh_w * 0.62, kt + 40 * s, cx - thigh_w * 0.34, kt + 78 * s,
                  cx - thigh_w * 0.26, kt + 96 * s)
    shank.lineTo(cx + thigh_w * 0.26, kt + 96 * s)
    shank.cubicTo(cx + thigh_w * 0.34, kt + 78 * s, cx + thigh_w * 0.62, kt + 40 * s,
                  cx + thigh_w * 0.50, kt)
    shank.closeSubpath()
    paths["shank"] = shank

    def belly(x_off: float, w: float, y0: float, y1: float) -> QPainterPath:
        path = QPainterPath()
        x = cx + x_off * s
        path.moveTo(x - w * s, top + y0 * s)
        path.cubicTo(x - w * 1.25 * s, top + (y0 + (y1 - y0) * 0.4) * s,
                     x - w * 0.85 * s, top + (y1 - 12) * s,
                     x, top + y1 * s)
        path.cubicTo(x + w * 0.85 * s, top + (y1 - 12) * s,
                     x + w * 1.25 * s, top + (y0 + (y1 - y0) * 0.4) * s,
                     x + w * s, top + y0 * s)
        path.closeSubpath()
        return path

    paths["bf"] = belly(outer * 17, 15, 12, 96)
    paths["st"] = belly(-outer * 15, 13, 16, 104)
    paths["vl"] = belly(outer * 18, 15, 10, 92)
    paths["rf"] = belly(-outer * 12, 12, 8, 100)
    return paths


class BodyMap(QWidget):
    """双腿肌肉激活热力图：后面观画腘绳肌，前面观画股四头肌。

    填充透明度 = 该肌肉当前激活 %MVC，所以"哪条腿的哪块肌肉在偷懒"不用读数字就能看出来。
    伤史侧加虚线环与"伤"标记。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.snap: GuardSnapshot | None = None
        self.injury_side: str | None = None
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._hover: str | None = None
        self.setMouseTracking(True)

    def set_snapshot(self, snap: GuardSnapshot, injury_side: str | None = None) -> None:
        self.snap = snap
        self.injury_side = injury_side
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        if w < 80 or h < 80:
            p.end()
            return
        snap = self.snap
        half = w / 2.0
        scale = min(half / 190.0, (h - 52) / 250.0)
        scale = max(0.42, scale)
        top = 40.0

        for idx, (view, groups) in enumerate((("后面观 · 腘绳肌", ("bf", "st")),
                                              ("前面观 · 股四头肌", ("vl", "rf")))):
            x0 = idx * half
            p.setFont(T.font(11, QFont.DemiBold))
            p.setPen(QColor(T.C["muted"]))
            p.drawText(QRectF(x0, 6, half, 18), Qt.AlignCenter, view)
            if idx == 1:
                p.setPen(QPen(QColor(30, 68, 83, 150), 1, Qt.DashLine))
                p.drawLine(QPointF(x0, 28), QPointF(x0, h - 8))

            # 骨盆/短裤
            pelvis = QPainterPath()
            pw = 62 * scale
            pcx = x0 + half / 2.0
            pelvis.addRoundedRect(QRectF(pcx - pw, top - 26 * scale, pw * 2, 30 * scale),
                                  9 * scale, 9 * scale)
            p.setPen(QPen(QColor(30, 68, 83, 200), 1.3))
            p.setBrush(QColor(18, 44, 55, 150))
            p.drawPath(pelvis)

            for side in ("L", "R"):
                cx = pcx + (-38 if side == "L" else 38) * scale
                paths = _leg_paths(cx, top, scale, side)
                p.setPen(QPen(QColor(38, 82, 99, 210), 1.4))
                p.setBrush(QColor(11, 28, 36, 190))
                p.drawPath(paths["thigh"])
                p.drawPath(paths["shank"])
                p.setBrush(QColor(18, 44, 55, 160))
                p.drawEllipse(QPointF(cx, top + 122 * scale), 15 * scale, 9 * scale)

                for g in groups:
                    mid = f"{g}_{side.lower()}"
                    m = next((x for x in MUSCLES if x.id == mid), None)
                    if m is None:
                        continue
                    act = snap.muscles[mid].activation if snap and mid in snap.muscles else 0.0
                    online = snap.muscles[mid].online if snap and mid in snap.muscles else False
                    frac = max(0.0, min(1.0, act / 85.0))
                    base = QColor(m.color)
                    fill = QColor(base)
                    fill.setAlpha(int(26 + 205 * frac) if online else 16)
                    p.setBrush(fill)
                    edge = QColor(base)
                    edge.setAlpha(int(120 + 135 * frac) if online else 50)
                    p.setPen(QPen(edge, 1.5))
                    p.drawPath(paths[g])
                    if frac > 0.62 and online:
                        glow = QColor(base)
                        glow.setAlpha(int(70 * (frac - 0.62) / 0.38))
                        p.setPen(QPen(glow, 5.0))
                        p.setBrush(Qt.NoBrush)
                        p.drawPath(paths[g])

                    ly = top + (52 if g in ("bf", "vl") else 78) * scale
                    outer = -1.0 if side == "L" else 1.0
                    off = {"bf": outer * 17, "st": -outer * 15,
                           "vl": outer * 18, "rf": -outer * 12}[g]
                    lx = cx + off * scale
                    p.setFont(T.font(max(8, int(10 * scale)), QFont.Bold, mono=True))
                    p.setPen(QColor(T.C["text_warm"]) if online else QColor(T.C["dim"]))
                    p.drawText(QRectF(lx - 22 * scale, ly - 8, 44 * scale, 15),
                               Qt.AlignCenter, f"{act:.0f}%" if online else "离线")

                if self.injury_side == side:
                    p.setPen(QPen(QColor(T.C["danger"]), 1.6, Qt.DashLine))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(QPointF(cx, top + 58 * scale), 42 * scale, 62 * scale)
                    p.setFont(T.font(9, QFont.Bold))
                    p.setPen(QColor(T.C["danger"]))
                    p.drawText(QRectF(cx - 12 * scale, top + 126 * scale, 24 * scale, 14),
                               Qt.AlignCenter, "伤史")

                p.setFont(T.font(11, QFont.DemiBold))
                p.setPen(QColor(T.C["muted"]))
                p.drawText(QRectF(cx - 30 * scale, h - 22, 60 * scale, 16), Qt.AlignCenter,
                           "左腿" if side == "L" else "右腿")

        if w > 460:
            p.setFont(T.font(10))
            p.setPen(QColor(T.C["dim"]))
            p.drawText(QRectF(8, h - 18, w - 16, 14), Qt.AlignRight, "填充 = %MVC")
        p.end()


class StrideRibbon(QWidget):
    """步态相位带：支撑 / 摆动 / 末端摆动随时间向左滚动。

    末端摆动（触地前 140 ms）单独染色——腘绳肌拉伤几乎都发生在这一段，把它画出来
    比画一条角速度曲线更能说明"什么时候危险"。
    """

    TERMINAL = 0.14

    def __init__(self, seconds: float = 8.0, parent=None) -> None:
        super().__init__(parent)
        self.seconds = seconds
        self.snap: GuardSnapshot | None = None
        self.swing_warn = 700.0
        self.setMinimumHeight(104)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_snapshot(self, snap: GuardSnapshot, swing_warn: float = 700.0) -> None:
        self.snap = snap
        self.swing_warn = swing_warn
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        if w < 60:
            p.end()
            return
        snap = self.snap
        top, band_h = 26.0, 40.0
        t_now = snap.t if snap else 0.0
        t_from = t_now - self.seconds

        p.setFont(T.font(10))
        p.setPen(QColor(T.C["dim"]))
        for i in range(int(self.seconds) + 1):
            x = w - (i / self.seconds) * w
            p.drawLine(QPointF(x, top - 5), QPointF(x, top - 1))
            if i % 2 == 0:
                p.drawText(QRectF(x - 16, 4, 32, 14), Qt.AlignCenter, f"-{i}s")

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(8, 22, 29, 200))
        p.drawRoundedRect(QRectF(0, top, w, band_h), 7, 7)

        if snap and snap.strides:
            for s in snap.strides:
                if s.interval <= 0.1 or s.t < t_from - 1.0:
                    continue
                start = s.t - s.interval
                if s.t < t_from or start > t_now:
                    continue
                x0 = max(0.0, (start - t_from) / self.seconds * w)
                x1 = min(w, (s.t - t_from) / self.seconds * w)
                if x1 - x0 < 1:
                    continue
                stance_end = start + s.interval * snap.stance_ratio
                xs = min(x1, max(x0, (stance_end - t_from) / self.seconds * w))
                xt = min(x1, max(x0, (s.t - self.TERMINAL - t_from) / self.seconds * w))
                p.setBrush(QColor(61, 155, 255, 96))
                p.drawRect(QRectF(x0, top + 3, max(0.0, xs - x0), band_h - 6))
                p.setBrush(QColor(18, 224, 196, 66))
                p.drawRect(QRectF(xs, top + 3, max(0.0, xt - xs), band_h - 6))
                hot = s.peak_dps > self.swing_warn
                g = QLinearGradient(xt, top, x1, top)
                g.setColorAt(0.0, QColor(T.C["danger"] if hot else T.C["magenta"]))
                c = QColor(T.C["magenta"])
                c.setAlpha(110)
                g.setColorAt(1.0, c)
                p.setBrush(QBrush(g))
                p.drawRect(QRectF(xt, top + 3, max(0.0, x1 - xt), band_h - 6))

                p.setPen(QPen(QColor(T.C["text_warm"] if hot else T.C["muted"]), 1.4))
                p.drawLine(QPointF(x1, top + 1), QPointF(x1, top + band_h - 1))
                if hot:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(T.C["danger"]))
                    p.drawEllipse(QPointF(x1, top - 7), 3.4, 3.4)
                p.setPen(Qt.NoPen)

        p.setPen(QPen(QColor(T.C["turf"]), 2))
        p.drawLine(QPointF(w - 1, top - 6), QPointF(w - 1, top + band_h + 6))

        legend = (("支撑", T.C["blue"]), ("摆动", T.C["turf"]),
                  ("末端摆动 140ms", T.C["magenta"]), ("超阈值触地", T.C["danger"]))
        x = 4.0
        p.setFont(T.font(10))
        for name, col in legend:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(col))
            p.drawRoundedRect(QRectF(x, top + band_h + 12, 9, 9), 2, 2)
            p.setPen(QColor(T.C["muted"]))
            p.drawText(QRectF(x + 13, top + band_h + 8, 110, 16), Qt.AlignLeft | Qt.AlignVCenter, name)
            x += 17 + p.fontMetrics().horizontalAdvance(name) + 14

        if snap and w > 620:
            p.setFont(T.font(11, QFont.Bold, mono=True))
            p.setPen(QColor(T.C["text_warm"]))
            p.drawText(QRectF(w - 190, top + band_h + 8, 186, 16), Qt.AlignRight,
                       f"步频 {snap.cadence:.0f} 步/分 · {snap.stride_count} 步")
        p.end()


class BilateralBars(QWidget):
    """左右镜像激活条：中轴两侧长度差即不对称，阈值处画刻度。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.snap: GuardSnapshot | None = None
        self.asym_warn = 10.0
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_snapshot(self, snap: GuardSnapshot, asym_warn: float = 10.0) -> None:
        self.snap = snap
        self.asym_warn = asym_warn
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        snap = self.snap
        rows = [("ST", "st_l", "st_r", "半腱肌"), ("BF", "bf_l", "bf_r", "股二头肌"),
                ("VL", "vl_l", "vl_r", "股外侧肌"), ("RF", "rf_l", "rf_r", "股直肌")]
        n = len(rows)
        row_h = (h - 22) / n
        cx = w / 2.0
        half = cx - 62
        full = 100.0

        p.setFont(T.font(10))
        p.setPen(QColor(T.C["dim"]))
        p.drawText(QRectF(0, 2, cx - 8, 14), Qt.AlignRight, "左 %MVC")
        p.drawText(QRectF(cx + 8, 2, cx - 8, 14), Qt.AlignLeft, "右 %MVC")
        p.setPen(QPen(QColor(30, 68, 83, 190), 1))
        p.drawLine(QPointF(cx, 18), QPointF(cx, h - 4))

        for i, (abbr, lid, rid, name) in enumerate(rows):
            y = 20 + i * row_h
            bh = min(17.0, row_h - 9)
            lv = snap.muscles[lid].activation if snap and lid in snap.muscles else 0.0
            rv = snap.muscles[rid].activation if snap and rid in snap.muscles else 0.0
            lo = snap.muscles[lid].online if snap else False
            ro = snap.muscles[rid].online if snap else False
            ml = next(m for m in MUSCLES if m.id == lid)
            mr = next(m for m in MUSCLES if m.id == rid)

            p.setPen(Qt.NoPen)
            p.setBrush(QColor(18, 44, 55, 130))
            p.drawRoundedRect(QRectF(cx - half, y, half, bh), 4, 4)
            p.drawRoundedRect(QRectF(cx, y, half, bh), 4, 4)

            for val, col, online, left in ((lv, ml.color, lo, True), (rv, mr.color, ro, False)):
                frac = max(0.0, min(1.0, val / full))
                bw = half * frac
                if bw < 1:
                    continue
                rect = QRectF(cx - bw if left else cx, y, bw, bh)
                g = QLinearGradient(rect.left(), y, rect.right(), y)
                c = QColor(col)
                c.setAlpha(235 if online else 60)
                c2 = QColor(col)
                c2.setAlpha(120 if online else 30)
                if left:
                    g.setColorAt(0.0, c2)
                    g.setColorAt(1.0, c)
                else:
                    g.setColorAt(0.0, c)
                    g.setColorAt(1.0, c2)
                p.setBrush(QBrush(g))
                p.drawRoundedRect(rect, 4, 4)

            p.setFont(T.font(11, QFont.Bold))
            p.setPen(QColor(T.C["text"]))
            p.drawText(QRectF(cx - 26, y - 1, 52, bh + 2), Qt.AlignCenter, abbr)
            p.setFont(T.font(10, mono=True))
            p.setPen(QColor(ml.color if lo else T.C["dim"]))
            p.drawText(QRectF(2, y - 1, cx - half - 6, bh + 2), Qt.AlignRight | Qt.AlignVCenter,
                       f"{lv:.0f}" if lo else "—")
            p.setPen(QColor(mr.color if ro else T.C["dim"]))
            p.drawText(QRectF(cx + half + 6, y - 1, cx - half - 8, bh + 2),
                       Qt.AlignLeft | Qt.AlignVCenter, f"{rv:.0f}" if ro else "—")

        if snap and snap.ham_l > 1 and snap.ham_r > 1:
            worse = snap.asym >= self.asym_warn
            p.setFont(T.font(11, QFont.Bold, mono=True))
            p.setPen(QColor(T.C["danger"] if worse else T.C["safe"]))
            p.drawText(QRectF(0, h - 17, w, 15), Qt.AlignCenter,
                       f"腘绳肌左右不对称 {snap.asym:.1f}%　阈值 {self.asym_warn:.0f}%")
        p.end()


class FactorBars(QWidget):
    """风险因子贡献分解：总分是怎么来的，逐项摊开。"""

    factor_clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.snap: GuardSnapshot | None = None
        self._rows: list[tuple[str, QRectF]] = []
        self.setMinimumHeight(188)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)

    def set_snapshot(self, snap: GuardSnapshot) -> None:
        self.snap = snap
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        for fid, rect in self._rows:
            if rect.contains(event.position()):
                self.factor_clicked.emit(fid)
                return

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        snap = self.snap
        factors = snap.factors if snap else []
        if not factors:
            p.setPen(QColor(T.C["dim"]))
            p.setFont(T.font(12))
            p.drawText(self.rect(), Qt.AlignCenter, "等待因子数据…")
            p.end()
            return
        row_h = h / len(factors)
        narrow = w < 340
        bar_x = 96.0 if narrow else 132.0
        bar_w = max(30.0, w - bar_x - 70)
        self._rows = []
        for i, f in enumerate(factors):
            y = i * row_h
            self._rows.append((f.id, QRectF(0, y, w, row_h)))
            sev = max(0.0, min(1.0, f.severity))
            col = QColor(T.C["safe"] if sev < 0.35 else T.C["warn"] if sev < 0.7 else T.C["danger"])
            if not f.available:
                col = QColor(T.C["dim"])
            p.setFont(T.font(11 if narrow else 12, QFont.DemiBold))
            p.setPen(QColor(T.C["text"] if f.available else T.C["dim"]))
            p.drawText(QRectF(2, y + 2, bar_x - 6, row_h - 4), Qt.AlignLeft | Qt.AlignVCenter, f.name)

            p.setPen(Qt.NoPen)
            p.setBrush(QColor(18, 44, 55, 150))
            p.drawRoundedRect(QRectF(bar_x, y + row_h * 0.30, bar_w, row_h * 0.30), 4, 4)
            if sev > 0.01:
                g = QLinearGradient(bar_x, 0, bar_x + bar_w, 0)
                c2 = QColor(col)
                c2.setAlpha(120)
                g.setColorAt(0.0, c2)
                g.setColorAt(1.0, col)
                p.setBrush(QBrush(g))
                p.drawRoundedRect(QRectF(bar_x, y + row_h * 0.30, bar_w * sev, row_h * 0.30), 4, 4)
            wx = bar_x + bar_w * 0.25
            p.setPen(QPen(QColor(233, 244, 244, 90), 1, Qt.DashLine))
            p.drawLine(QPointF(wx, y + row_h * 0.24), QPointF(wx, y + row_h * 0.66))

            p.setFont(T.font(11, QFont.Bold, mono=True))
            p.setPen(col)
            p.drawText(QRectF(w - 64, y + 2, 60, row_h - 4),
                       Qt.AlignRight | Qt.AlignVCenter, f"+{f.contribution:.1f}")
            if not narrow:
                p.setFont(T.font(9))
                p.setPen(QColor(T.C["dim"]))
                p.drawText(QRectF(bar_x, y + row_h * 0.62, bar_w, row_h * 0.34),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           f"{f.value:.1f}{f.unit} · 权重 {f.weight * 100:.0f}%"
                           + ("" if f.available else " · 无数据"))
        p.end()


class CueBanner(QWidget):
    """优先显示一句可执行建议，次要说明按需显示。"""

    LEVELS = {
        "calm": ("safe", "状态平稳"),
        "watch": ("info", "留意"),
        "warn": ("warn", "需要干预"),
        "stop": ("danger", "建议停止"),
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.level = "calm"
        self.title = "等待传感器"
        self.text = ""
        self.setMinimumHeight(78)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_cue(self, level: str, title: str, text: str) -> None:
        if (level, title, text) != (self.level, self.title, self.text):
            self.level, self.title, self.text = level, title, text
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        key, tag = self.LEVELS.get(self.level, self.LEVELS["calm"])
        col = QColor(T.C[key])
        rect = QRectF(0.5, 0.5, w - 1, h - 1)
        fill = QColor(T.C["surface"])
        fill.setAlpha(245)
        p.setBrush(fill)
        border = QColor(col)
        border.setAlpha(130)
        p.setPen(QPen(border, 1))
        p.drawRoundedRect(rect, 8, 8)

        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(0.5, 8, 4, h - 16), 2, 2)

        p.setFont(T.font(11, QFont.DemiBold))
        p.setPen(col)
        p.drawText(QRectF(18, 10, 100, 16), Qt.AlignLeft | Qt.AlignVCenter, tag)
        p.setFont(T.font(18, QFont.Bold))
        p.setPen(QColor(T.C["text_warm"]))
        p.drawText(QRectF(18, 28, w - 36, 26), Qt.AlignLeft | Qt.AlignVCenter, self.title)
        if self.text:
            p.setFont(T.font(12))
            p.setPen(QColor(T.C["muted"]))
            p.drawText(QRectF(18, 55, w - 36, h - 61), Qt.AlignLeft | Qt.AlignVCenter, self.text)
        p.end()


class Sparkline(QWidget):
    """迷你趋势：带渐变填充与末点高亮，用于疲劳/负荷这类慢变量。"""

    def __init__(self, accent: str = "turf", parent=None) -> None:
        super().__init__(parent)
        self.accent = T.C.get(accent, accent)
        self.values: list[float] = []
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_values(self, values) -> None:
        self.values = list(values)[-160:]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(T.C["bg1"]))
        p.drawRoundedRect(QRectF(0, 0, w, h), 6, 6)
        if len(self.values) < 2:
            p.setPen(QColor(T.C["dim"]))
            p.setFont(T.font(10))
            p.drawText(self.rect(), Qt.AlignCenter, "采集中…")
            p.end()
            return
        v = np.asarray(self.values, dtype=float)
        lo, hi = float(np.min(v)), float(np.max(v))
        if hi - lo < 1e-6:
            hi = lo + 1.0
        pad = 6.0
        xs = np.linspace(pad, w - pad, v.size)
        ys = h - pad - (v - lo) / (hi - lo) * (h - 2 * pad)
        path = QPainterPath()
        path.moveTo(xs[0], ys[0])
        for x, y in zip(xs[1:], ys[1:]):
            path.lineTo(x, y)
        fill = QPainterPath(path)
        fill.lineTo(xs[-1], h - 1)
        fill.lineTo(xs[0], h - 1)
        fill.closeSubpath()
        g = QLinearGradient(0, 0, 0, h)
        c = QColor(self.accent)
        c.setAlpha(80)
        g.setColorAt(0.0, c)
        c.setAlpha(0)
        g.setColorAt(1.0, c)
        p.setBrush(QBrush(g))
        p.setPen(Qt.NoPen)
        p.drawPath(fill)
        p.setPen(QPen(QColor(self.accent), 1.8))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.accent))
        p.drawEllipse(QPointF(xs[-1], ys[-1]), 3.0, 3.0)
        p.setFont(T.font(9, mono=True))
        p.setPen(QColor(T.C["dim"]))
        p.drawText(QRectF(4, 2, w - 8, 12), Qt.AlignRight, f"{v[-1]:.1f}")
        p.end()


class LevelPill(QWidget):
    """小徽章：在线/离线、风险等级、课目标记。"""

    def __init__(self, text: str = "", tone: str = "muted", parent=None) -> None:
        super().__init__(parent)
        self.text = text
        self.tone = tone
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._w = 60
        self.setText(text)

    def setText(self, text: str, tone: str | None = None) -> None:  # noqa: N802
        self.text = text
        if tone:
            self.tone = tone
        f = T.font(11, QFont.DemiBold)
        from PySide6.QtGui import QFontMetrics
        self._w = QFontMetrics(f).horizontalAdvance(text) + 22
        self.setFixedWidth(max(44, self._w))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        col = QColor(T.C.get(self.tone, self.tone))
        bg = QColor(col)
        bg.setAlpha(38)
        p.setBrush(bg)
        border = QColor(col)
        border.setAlpha(130)
        p.setPen(QPen(border, 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 11, 11)
        p.setFont(T.font(11, QFont.DemiBold))
        p.setPen(col)
        p.drawText(self.rect(), Qt.AlignCenter, self.text)
        p.end()