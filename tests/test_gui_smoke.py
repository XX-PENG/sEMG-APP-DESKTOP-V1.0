import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from semg_desktop.sources import SimulateSource  # noqa: E402
from semg_desktop.ui.analysis_view import AnalysisView  # noqa: E402
from semg_desktop.ui.connect_dialog import ConnectDialog  # noqa: E402
from semg_desktop.ui.emg_view import EmgView  # noqa: E402
from semg_desktop.ui.imu_view import ImuView  # noqa: E402
from semg_desktop.ui.main_window import MainWindow  # noqa: E402
from semg_desktop.ui.status_panel import StatusPanel  # noqa: E402

_KEEP: list = []


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def cfg():
    from semg_desktop.config import load_config

    return load_config(None)


def test_views_render_without_data(qapp, cfg):
    views = [EmgView(cfg), ImuView(cfg), AnalysisView(cfg), StatusPanel(cfg)]
    for view in views:
        view.show()
        if isinstance(view, EmgView):
            view.update_data({}, {}, 0.0)
        if isinstance(view, AnalysisView):
            view.update_data(0.0)
        qapp.processEvents()
        view.close()
    _KEEP.append(views)  # 让 Qt 对象活到会话结束：跨用例 GC 掉顶层窗口会在 offscreen 下踩坏


def test_connect_dialog_builds_sources(qapp, cfg):
    dlg = ConnectDialog(cfg)
    _KEEP.append(dlg)
    dlg.rb_sim.setChecked(True)
    assert isinstance(dlg.source(), SimulateSource)
    dlg.rb_tcp.setChecked(True)
    assert dlg.source().describe().startswith("TCP")
    dlg.rb_serial.setChecked(True)
    assert "串口" in dlg.source().describe()
    dlg.rb_replay.setChecked(True)
    assert dlg.source().name == "replay"
    dlg.close()


def test_main_window_receives_simulated_stream(qapp):
    win = MainWindow(source=SimulateSource(frame_period=0.01))
    _KEEP.append(win)
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    st = win.acq.stats()
    rows = win.acq.device_rows()
    assert st.error is None, st.error
    assert st.frames_per_second > 0
    assert len([r for r in rows if r["kind"] == "semg"]) == 8
    assert st.parse.semg_frames > 0 and st.parse.imu_frames > 0

    for tab in (win.emg, win.imu, win.analysis, win.status, win.ota):
        win.tabs.setCurrentWidget(tab)
        win.refresh()
        qapp.processEvents()
    win.close()
    assert win.acq is None


def test_main_window_record_then_export(qapp, tmp_path, monkeypatch):
    win = MainWindow(source=SimulateSource(frame_period=0.01))
    monkeypatch.setattr(win, "records_root", lambda: tmp_path)
    win.toggle_recording()
    assert win.rec is not None
    deadline = time.monotonic() + 0.8
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    win.toggle_recording()
    assert win.rec is None
    sessions = list(tmp_path.glob("rec-*"))
    assert sessions
    assert (sessions[0] / "raw.log.gz").is_file()
    assert (sessions[0] / "parsed" / "data.npz").is_file()
    win.disconnect_source()
    win.close()
