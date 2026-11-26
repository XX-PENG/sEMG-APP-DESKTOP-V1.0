"""克制的中性运动工作台：深灰蓝底、低饱和强调色、清晰的信息层级。"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication

C = {
    "bg0": "#11171D",
    "bg1": "#171F27",
    "surface": "#1C252E",
    "surface2": "#26323D",
    "overlay": "#303E4B",
    "border": "#3A4A58",
    "border_soft": "#2C3944",
    "text": "#F1F3F5",
    "text_warm": "#FAFBFC",
    "muted": "#A8B4BF",
    "dim": "#778895",
    "turf": "#8BC4B5",
    "turf_deep": "#527F74",
    "blue": "#7EACCF",
    "magenta": "#C87991",
    "orange": "#D19A6A",
    "lime": "#A8BC78",
    "safe": "#79B99A",
    "warn": "#D5AD66",
    "danger": "#D97979",
    "info": "#7EACCF",
}

MUSCLE_COLORS = {
    "bf_l": "#C87991", "st_l": "#D19A6A", "vl_l": "#7EACCF", "rf_l": "#8BC4B5",
    "bf_r": "#DBA2AF", "st_r": "#E1B58E", "vl_r": "#A7C6DD", "rf_r": "#B0D5CA",
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
QMainWindow, QDialog {{ background: {C['bg1']}; }}
QToolTip {{
    background: {C['surface2']}; color: {C['text']};
    border: 1px solid {C['border']}; padding: 6px 9px; border-radius: 6px;
}}

/* ── 外壳：用 app 级 ID 规则而非 widget.setStyleSheet，
   否则父级 widget 样式表会让子按钮的 app 级背景整片失效 ── */
QWidget#NavRail {{ background: {C['bg0']}; border-right: 1px solid {C['border_soft']}; }}
QWidget#TopBar {{ background: {C['bg0']}; border-bottom: 1px solid {C['border_soft']}; }}
QTabWidget#EngineeringTabs {{ padding: 12px; background: {C['bg1']}; }}

/* ── 卡片 ── */
QFrame#Card, QWidget#Card {{
    background: {C['surface']};
    border: 1px solid {C['border_soft']};
    border-radius: 10px;
}}
QFrame#CardFlat {{ background: transparent; border: none; }}
QLabel#CardTitle {{
    color: {C['text']}; font-size: 14px; font-weight: 700;
    background: transparent;
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
    color: {C['muted']}; font-size: 11px; font-weight: 700;
    background: transparent;
}}
QLabel#PageTitle {{ color: {C['text_warm']}; font-size: 24px; font-weight: 700; background: transparent; }}
QLabel#PageHint {{ color: {C['muted']}; font-size: 12px; background: transparent; }}
QLabel#SourceHealthy {{ color: {C['safe']}; background: transparent; font-size: 12px; }}
QLabel#SourceWarning {{ color: {C['warn']}; background: transparent; font-size: 12px; }}
QLabel#SourceOffline {{ color: {C['muted']}; background: transparent; font-size: 12px; }}

/* ── 按钮 ── */
QPushButton {{
    background: {C['surface2']};
    border: 1px solid {C['border']};
    border-radius: 7px;
    padding: 7px 14px;
    min-height: 20px;
    color: {C['text']};
    font-weight: 600;
}}
QPushButton:hover {{ background: {C['overlay']}; border-color: {C['turf_deep']}; }}
QPushButton:pressed {{ background: {C['turf_deep']}; }}
QPushButton:disabled {{ color: {C['dim']}; background: {C['surface']}; border-color: {C['border_soft']}; }}
QPushButton#Primary {{
    background: {C['turf']};
    color: #13201E; border: none; font-weight: 700;
}}
QPushButton#Primary:hover {{ background: #A5D3C7; }}
QPushButton#Primary:pressed {{ background: {C['turf_deep']}; color: {C['text_warm']}; }}
QPushButton#Primary:disabled {{ background: {C['surface2']}; color: {C['dim']}; }}
QPushButton#Danger {{
    background: #503237; border: 1px solid {C['danger']}; color: #F4C6C6;
}}
QPushButton#Danger:hover {{ background: #663D43; }}
QPushButton#Ghost {{ background: transparent; border: 1px solid {C['border']}; color: {C['muted']}; }}
QPushButton#Ghost:hover {{ color: {C['text']}; border-color: {C['turf_deep']}; }}
QPushButton#NavButton {{
    background: transparent; border: none; border-radius: 6px;
    padding: 10px 12px; text-align: left; color: {C['muted']}; font-weight: 500;
}}
QPushButton#NavButton:hover {{ background: {C['surface']}; color: {C['text']}; }}
QPushButton#NavButton[active="true"] {{
    background: {C['surface2']};
    border-left: 3px solid {C['turf']};
    color: {C['text_warm']};
}}
QPushButton#Chip {{
    background: transparent; border: 1px solid {C['border']};
    border-radius: 6px; padding: 4px 10px; color: {C['muted']}; font-size: 12px;
}}
QPushButton#Chip:checked {{ background: {C['surface2']}; border-color: {C['turf_deep']}; color: {C['text']}; }}
QPushButton#Chip:hover {{ color: {C['text']}; }}

/* ── 输入 ── */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {C['bg1']}; border: 1px solid {C['border']};
    border-radius: 6px; padding: 6px 9px; color: {C['text']};
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
    height: 4px; border-radius: 2px;
    background: {C['surface2']};
}}
QSlider::sub-page:horizontal {{
    height: 4px; border-radius: 2px;
    background: {C['turf_deep']};
}}
QSlider::handle:horizontal {{
    width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
    background: {C['text']}; border: 2px solid {C['turf_deep']};
}}
QSlider::handle:horizontal:hover {{ background: {C['turf']}; }}
QSlider::groove:vertical {{ width: 5px; border-radius: 3px; background: {C['surface2']}; }}
QSlider::handle:vertical {{
    width: 15px; height: 15px; margin: 0 -5px; border-radius: 8px;
    background: {C['text']}; border: 2px solid {C['turf_deep']};
}}

/* ── 表格 ── */
QTableWidget, QTableView, QListWidget, QTreeWidget {{
    background: {C['bg1']};
    alternate-background-color: {C['surface']};
    border: 1px solid {C['border_soft']}; border-radius: 7px;
    gridline-color: {C['border_soft']};
    outline: none;
}}
QTableWidget::item, QListWidget::item {{ padding: 6px 7px; border: none; }}
QTableWidget::item:selected, QListWidget::item:selected {{
    background: {C['surface2']}; color: {C['text_warm']};
}}
QHeaderView::section {{
    background: {C['surface2']}; color: {C['muted']};
    border: none; border-right: 1px solid {C['border_soft']};
    border-bottom: 1px solid {C['border']};
    padding: 7px 6px; font-weight: 600; font-size: 11px;
}}
QTableCornerButton::section {{ background: {C['surface2']}; border: none; }}

/* ── 滚动区：视口与内容都要透底，否则 Fusion 默认浅色会整块发白 ── */
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QAbstractScrollArea::viewport {{ background: transparent; }}

/* ── 滚动条：用本系统自己的色族，不留浏览器/系统默认灰 ── */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {C['border']}; min-height: 32px; border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{ background: {C['dim']}; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {C['border']}; min-width: 32px; border-radius: 4px;
}}
QScrollBar::handle:horizontal:hover {{ background: {C['dim']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ── 分页 / 分组 / 进度 ── */
QTabWidget::pane {{ border: 1px solid {C['border_soft']}; border-radius: 7px; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {C['dim']}; padding: 9px 15px;
    border-bottom: 2px solid transparent; font-weight: 500;
}}
QTabBar::tab:selected {{ color: {C['text_warm']}; border-bottom: 2px solid {C['turf']}; }}
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
    background: {C['bg0']}; border: 1px solid {C['border_soft']};
    border-radius: 6px; color: {C['muted']};
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