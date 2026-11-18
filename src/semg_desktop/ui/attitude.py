"""IMU 姿态的简易 3D 显示。

不引入 PyOpenGL/VisPy：一个方框 + 三根轴，QPainter 做正交投影就够，代价是十几行矩阵。
姿态由 ahrs.MahonyAHRS 在采集线程按样本率解算后送进来，这里只负责画，不再做平滑——
之前按 UI 帧率做的 EMA 是显示延迟的主要来源。
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

_BOX = np.array(
    [
        [-0.5, -0.35, -0.08], [0.5, -0.35, -0.08], [0.5, 0.35, -0.08], [-0.5, 0.35, -0.08],
        [-0.5, -0.35, 0.08], [0.5, -0.35, 0.08], [0.5, 0.35, 0.08], [-0.5, 0.35, 0.08],
    ],
    dtype=np.float64,
)
_EDGES = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
_FACES = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 3, 7, 4)]
_AXES = [(np.array([0.85, 0.0, 0.0]), QColor(230, 60, 60)),
         (np.array([0.0, 0.85, 0.0]), QColor(60, 200, 90)),
         (np.array([0.0, 0.0, 0.85]), QColor(80, 140, 250))]
_VIEW_YAW = np.radians(-32.0)
_VIEW_PITCH = np.radians(-22.0)


def _rot(axis: int, angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    if axis == 0:
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)
    if axis == 1:
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


def _wrap_deg(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


class AttitudeWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._roll = 0.0
        self._pitch = 0.0
        self._yaw = 0.0
        self._yaw0 = 0.0

    def set_attitude(self, roll: float, pitch: float, yaw: float) -> None:
        self._roll, self._pitch, self._yaw = float(roll), float(pitch), float(yaw)
        self.update()

    def reset_yaw(self) -> None:
        """把当前航向记为零点。磁力计未标定，yaw 靠陀螺积分会漂，只能人工归零。"""
        self._yaw0 = self._yaw
        self.update()

    def reset(self) -> None:
        self._roll = self._pitch = self._yaw = self._yaw0 = 0.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(20, 22, 26))
        cx, cy = w / 2.0, h / 2.0
        scale = 0.38 * min(w, h)

        yaw = _wrap_deg(self._yaw - self._yaw0)
        body = (_rot(2, np.radians(yaw))
                @ _rot(1, np.radians(self._pitch))
                @ _rot(0, np.radians(self._roll)))
        view = _rot(0, _VIEW_PITCH) @ _rot(2, _VIEW_YAW)

        def screen(world: np.ndarray) -> np.ndarray:
            cam = world @ view.T
            out = np.empty((len(cam), 2))
            out[:, 0] = cx + cam[:, 0] * scale
            out[:, 1] = cy - cam[:, 2] * scale
            return out

        verts = screen(_BOX @ body.T)
        order = sorted(range(len(_FACES)), key=lambda k: np.mean([verts[i][1] for i in _FACES[k]]))
        for idx in order:
            path = QPainterPath()
            pts = verts[np.asarray(_FACES[idx])]
            path.moveTo(*pts[0])
            for q in pts[1:]:
                path.lineTo(*q)
            path.closeSubpath()
            p.setBrush(QColor(70, 130, 200, 90))
            p.setPen(QPen(QColor(150, 190, 230, 150), 1))
            p.drawPath(path)

        origin = screen(np.zeros((1, 3)))[0]
        for vec, color in _AXES:
            end = screen((vec @ body.T).reshape(1, 3))[0]
            p.setPen(QPen(color, 2))
            p.drawLine(int(origin[0]), int(origin[1]), int(end[0]), int(end[1]))

        # 重力参考线固定在世界系，不能跟着 body 转，否则失去参考意义
        p.setPen(QPen(QColor(220, 220, 220, 140), 1, Qt.DashLine))
        g = screen(np.array([[0.0, 0.0, -1.05]]))[0]
        p.drawLine(int(origin[0]), int(origin[1]), int(g[0]), int(g[1]))

        p.setPen(QColor(230, 230, 230))
        f = p.font()
        f.setPointSize(9)
        p.setFont(f)
        p.drawText(8, 18, f"roll  {self._roll:+7.1f}°")
        p.drawText(8, 34, f"pitch {self._pitch:+7.1f}°")
        p.drawText(8, 50, f"yaw   {yaw:+7.1f}°")
        p.end()