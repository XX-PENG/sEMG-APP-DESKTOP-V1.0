import sys


def run_gui(config_path: str | None = None, source=None) -> int:
    import pyqtgraph as pg
    from PySide6.QtWidgets import QApplication

    from . import theme as T
    from .main_window import MainWindow

    pg.setConfigOptions(background=T.C["bg1"], foreground=T.C["muted"], antialias=True)
    app = QApplication.instance() or QApplication(sys.argv)
    T.apply(app)
    win = MainWindow(config_path=config_path, source=source)
    win.show()
    return app.exec()