"""视觉语言：夜间泛光灯下的曲棍球场 + 运动医学仪表。

色族来自场景本身而不是通用深色模板：深水蓝绿是场地与夜色（最大面积），腘绳肌用暖色
（品红 BF / 橙 ST）、股四头肌用冷色（电蓝 VL / 青 RF），左右两侧靠明度成对；语义色
单独一套（安全绿 / 警示琥珀 / 危险红 / 信息蓝），不与肌肉身份色混用。
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication

C = {
    "bg0": "#050C11",
    "bg1": "#08161D",
    "surface": "#0D2029",
    "surface2": "#122C37",
    "overlay": "#173845",
    "border": "#1E4453",
    "border_soft": "#15333F",
    "text": "#E9F4F4",
    "text_warm": "#F6EEE3",
    "muted": "#82A3AF",
    "dim": "#54737F",
    "turf": "#12E0C4",
    "turf_deep": "#0A8F80",
    "blue": "#3D9BFF",
    "magenta": "#FF4D8D",
    "orange": "#FF8A3D",
    "lime": "#B6FF3D",
    "safe": "#2FD98A",
    "warn": "#FFB020",
    "danger": "#FF3B5C",
    "info": "#3D9BFF",
}

MUSCLE_COLORS = {
    "bf_l": "#FF4D8D", "st_l": "#FF8A3D", "vl_l": "#3D9BFF", "rf_l": "#12E0C4",
    "bf_r": "#FFA8C8", "st_r": "#FFC48A", "vl_r": "#9CC9FF", "rf_r": "#7FF0E2",
}

UI_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"


def font(size: int = 13, weight: int = QFont.Normal, mono: bool = False) -> QFont:
    f = QFont(MONO_FAMILY if mono else UI_FAMILY)
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(weight))
    if mono:
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setFixedPitch(True)
    return f


def color(key: str, alpha: int = 255) -> QColor:
    c = QColor(C.get(key, key))
    c.setAlpha(alpha)
    return c


def mix(a: str, b: str, t: float) -> QColor:
    ca, cb = QColor(C.get(a, a)), QColor(C.get(b, b))
    return QColor(
        int(ca.red() + (cb.red() - ca.red()) * t),
        int(ca.green() + (cb.green() - ca.green()) * t),
        int(ca.blue() + (cb.blue() - ca.blue()) * t),
    )


def risk_color(score: float) -> str:
    if score < 35:
        return C["safe"]
    if score < 65:
        return C["warn"]
    return C["danger"]


def qss() -> str:
    return f"""
QWidget {{
    color: {C['text']};
    font-family: "{UI_FAMILY}";
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {C['bg0']}; }}
QToolTip {{
    background: {C['overlay']}; color: {C['text']};
    border: 1px solid {C['border']}; padding: 6px 9px; border-radius: 6px;
}}

/* ── 外壳：用 app 级 ID 规则而非 widget.setStyleSheet，
   否则父级 widget 样式表会让子按钮的 app 级背景整片失效 ── */
QWidget#NavRail {{ background: rgba(5, 12, 17, 205); border-right: 1px solid {C['border_soft']}; }}
QWidget#TopBar {{ background: rgba(8, 22, 29, 215); border-bottom: 1px solid {C['border_soft']}; }}

/* ── 卡片 ── */
QFrame#Card, QWidget#Card {{
    background: rgba(13, 32, 41, 232);
    border: 1px solid {C['border_soft']};
    border-radius: 14px;
}}
QFrame#CardFlat {{ background: transparent; border: none; }}
QLabel#CardTitle {{
    color: {C['text']}; font-size: 14px; font-weight: 700;
    letter-spacing: 1px; background: transparent;
}}
QLabel#CardHint {{ color: {C['dim']}; font-size: 11px; background: transparent; }}
QLabel#Muted {{ color: {C['muted']}; background: transparent; }}
QLabel#Dim {{ color: {C['dim']}; font-size: 11px; background: transparent; }}
QLabel#Metric {{
    color: {C['text_warm']}; font-family: "{MONO_FAMILY}";
    font-size: 30px; font-weight: 700; background: transparent;
}}
QLabel#MetricUnit {{ color: {C['dim']}; font-size: 12px; background: transparent; }}
QLabel#SectionHead {{
    color: {C['turf']}; font-size: 12px; font-weight: 700;
    letter-spacing: 2px; background: transparent;
}}

/* ── 按钮 ── */
QPushButton {{
    background: {C['surface2']};
    border: 1px solid {C['border']};
    border-radius: 9px;
    padding: 7px 14px;
    color: {C['text']};
    font-weight: 600;
}}
QPushButton:hover {{ background: {C['overlay']}; border-color: {C['turf_deep']}; }}
QPushButton:pressed {{ background: {C['turf_deep']}; }}
QPushButton:disabled {{ color: {C['dim']}; background: {C['surface']}; border-color: {C['border_soft']}; }}
QPushButton#Primary {{
    background: {C['turf']};
    color: #04211D; border: none; font-weight: 800;
}}
QPushButton#Primary:hover {{ background: #5FF0DC; }}
QPushButton#Primary:pressed {{ background: {C['turf_deep']}; color: #E9F4F4; }}
QPushButton#Primary:disabled {{ background: {C['surface2']}; color: {C['dim']}; }}
QPushButton#Danger {{
    background: rgba(255, 59, 92, 32); border: 1px solid {C['danger']}; color: #FFC2CD;
}}
QPushButton#Danger:hover {{ background: rgba(255, 59, 92, 70); }}
QPushButton#Ghost {{ background: transparent; border: 1px solid {C['border']}; color: {C['muted']}; }}
QPushButton#Ghost:hover {{ color: {C['text']}; border-color: {C['turf_deep']}; }}
QPushButton#NavButton {{
    background: transparent; border: none; border-radius: 10px;
    padding: 10px 12px; text-align: left; color: {C['muted']}; font-weight: 600;
}}
QPushButton#NavButton:hover {{ background: rgba(18, 224, 196, 22); color: {C['text']}; }}
QPushButton#NavButton[active="true"] {{
    background: rgba(18, 224, 196, 40);
    border-left: 3px solid {C['turf']};
    color: {C['turf']};
}}
QPushButton#Chip {{
    background: {C['surface2']}; border: 1px solid {C['border']};
    border-radius: 13px; padding: 4px 12px; color: {C['muted']}; font-size: 12px;
}}
QPushButton#Chip:checked {{ background: rgba(18, 224, 196, 40); border-color: {C['turf']}; color: {C['turf']}; }}
QPushButton#Chip:hover {{ color: {C['text']}; }}

/* ── 输入 ── */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {C['bg1']}; border: 1px solid {C['border']};
    border-radius: 8px; padding: 5px 9px; color: {C['text']};
    selection-background-color: {C['turf_deep']};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {C['turf']}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{
    image: none; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {C['muted']}; margin-right: 7px;
}}
QComboBox QAbstractItemView {{
    background: {C['overlay']}; border: 1px solid {C['border']};
    selection-background-color: {C['turf_deep']}; outline: none; padding: 4px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background: {C['surface2']}; border: none; width: 15px;
}}
QCheckBox {{ spacing: 7px; color: {C['muted']}; background: transparent; }}
QCheckBox::indicator {{
    width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid {C['border']}; background: {C['bg1']};
}}
QCheckBox::indicator:checked {{ background: {C['turf']}; border-color: {C['turf']}; }}
QCheckBox:hover {{ color: {C['text']}; }}

/* ── 滑杆 ── */
QSlider::groove:horizontal {{
    height: 5px; border-radius: 3px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(18, 44, 55, 255), stop:1 rgba(23, 56, 69, 255));
}}
QSlider::sub-page:horizontal {{
    height: 5px; border-radius: 3px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(10, 143, 128, 255), stop:1 rgba(18, 224, 196, 255));
}}
QSlider::handle:horizontal {{
    width: 15px; height: 15px; margin: -6px 0; border-radius: 8px;
    background: {C['text_warm']}; border: 2px solid {C['turf']};
}}
QSlider::handle:horizontal:hover {{ background: {C['turf']}; }}
QSlider::groove:vertical {{ width: 5px; border-radius: 3px; background: {C['surface2']}; }}
QSlider::handle:vertical {{
    width: 15px; height: 15px; margin: 0 -6px; border-radius: 8px;
    background: {C['text_warm']}; border: 2px solid {C['turf']};
}}

/* ── 表格 ── */
QTableWidget, QTableView, QListWidget, QTreeWidget {{
    background: rgba(8, 22, 29, 210);
    alternate-background-color: rgba(18, 44, 55, 130);
    border: 1px solid {C['border_soft']}; border-radius: 10px;
    gridline-color: rgba(30, 68, 83, 130);
    outline: none;
}}
QTableWidget::item, QListWidget::item {{ padding: 5px 7px; border: none; }}
QTableWidget::item:selected, QListWidget::item:selected {{
    background: rgba(18, 224, 196, 52); color: {C['text_warm']};
}}
QHeaderView::section {{
    background: {C['surface2']}; color: {C['muted']};
    border: none; border-right: 1px solid {C['border_soft']};
    border-bottom: 1px solid {C['border']};
    padding: 7px 6px; font-weight: 700; font-size: 11px;
}}
QTableCornerButton::section {{ background: {C['surface2']}; border: none; }}

/* ── 滚动区：视口与内容都要透底，否则 Fusion 默认浅色会整块发白 ── */
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QAbstractScrollArea::viewport {{ background: transparent; }}

/* ── 滚动条：用本系统自己的色族，不留浏览器/系统默认灰 ── */
QScrollBar:vertical {{
    background: {C['bg1']}; width: 11px; margin: 2px; border-radius: 5px;
}}
QScrollBar::handle:vertical {{
    background: {C['turf_deep']}; min-height: 32px; border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{ background: {C['turf']}; }}
QScrollBar:horizontal {{
    background: {C['bg1']}; height: 11px; margin: 2px; border-radius: 5px;
}}
QScrollBar::handle:horizontal {{
    background: {C['turf_deep']}; min-width: 32px; border-radius: 5px;
}}
QScrollBar::handle:horizontal:hover {{ background: {C['turf']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ── 分页 / 分组 / 进度 ── */
QTabWidget::pane {{ border: 1px solid {C['border_soft']}; border-radius: 10px; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {C['dim']}; padding: 8px 16px;
    border-bottom: 2px solid transparent; font-weight: 600;
}}
QTabBar::tab:selected {{ color: {C['turf']}; border-bottom: 2px solid {C['turf']}; }}
QTabBar::tab:hover {{ color: {C['text']}; }}
QGroupBox {{
    border: 1px solid {C['border_soft']}; border-radius: 10px;
    margin-top: 12px; padding-top: 10px; font-weight: 700; color: {C['muted']};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 5px; }}
QProgressBar {{
    background: {C['bg1']}; border: 1px solid {C['border_soft']};
    border-radius: 6px; height: 12px; text-align: center; color: {C['text']}; font-size: 10px;
}}
QProgressBar::chunk {{ border-radius: 5px; background: {C['turf']}; }}
QSplitter::handle {{ background: {C['border_soft']}; }}
QSplitter::handle:horizontal {{ width: 2px; }}
QSplitter::handle:vertical {{ height: 2px; }}
QPlainTextEdit, QTextEdit {{
    background: rgba(5, 12, 17, 220); border: 1px solid {C['border_soft']};
    border-radius: 9px; color: {C['muted']};
    font-family: "{MONO_FAMILY}"; font-size: 11px;
}}
QStatusBar {{ background: {C['bg0']}; color: {C['dim']}; border-top: 1px solid {C['border_soft']}; }}
QStatusBar QLabel {{ color: {C['muted']}; font-size: 11px; }}
QMenuBar {{ background: {C['bg0']}; color: {C['muted']}; }}
QMenuBar::item:selected {{ background: {C['surface2']}; }}
QMenu {{ background: {C['overlay']}; border: 1px solid {C['border']}; padding: 5px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {C['turf_deep']}; }}
QDialogButtonBox QPushButton {{ min-width: 84px; }}
"""


def ensure_fonts() -> None:
    """离屏 / 精简环境可能一个字体都没有（QFontDatabase.families() 为空），文字全成方框。

    正常桌面不会触发；触发时从系统字体目录注册 YaHei 与 Consolas，保证截图与无显示器
    自检看到的界面和真实桌面一致。
    """
    from PySide6.QtGui import QFontDatabase

    families = set(QFontDatabase.families())
    if UI_FAMILY in families or MONO_FAMILY in families:
        return
    candidates = [
        "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/consolab.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            QFontDatabase.addApplicationFont(path)
        except Exception:
            continue


def apply(app: QApplication) -> None:
    ensure_fonts()
    app.setStyle("Fusion")
    app.setStyleSheet(qss())
    app.setFont(font(13))