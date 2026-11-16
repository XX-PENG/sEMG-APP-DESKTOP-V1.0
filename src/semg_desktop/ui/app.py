import sys


def run_gui(config_path: str | None = None, source=None) -> int:
    import pyqtgraph as pg
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    pg.setConfigOptions(background="k", foreground="w", antialias=False)
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(config_path=config_path, source=source)
    win.show()
    return app.exec()
