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


def test_simulate_does_not_pollute_channel_map(qapp, tmp_path):
    """回归：模拟源 MAC 写回配置会把真实通道挤到越界号，首页只画 0..n-1 就整片空白。"""
    import json

    from semg_desktop.sources import SimulateSource
    from semg_desktop.ui.main_window import MainWindow

    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"channel_map": {"semg@deadbe": 8}}), encoding="utf-8")
    win = MainWindow(config_path=str(cfg_file), source=SimulateSource())
    _KEEP.append(win)
    deadline = time.monotonic() + 0.8
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert win.channel_map.as_dict().get("semg@deadbe") is None  # 越界旧映射被剔除
    windows = win.acq.emg_windows(win.emg.span)
    assert windows and all(i < win.emg.n for i in windows)  # 数据落在可见通道上
    win.disconnect_source()
    win.close()
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))["channel_map"]
    assert not any(k.startswith("semg@a4cf") for k in saved)


def test_open_connect_ok_path_actually_connects(qapp, cfg, monkeypatch):
    """回归：open_connect 曾漏导入 QDialog，OK 返回后抛 NameError 被 Qt 事件循环吞掉，
    表现为对话框关闭但连接没建立、标题不变、无弹窗。"""
    from PySide6.QtWidgets import QDialog

    from semg_desktop.ui import connect_dialog as cd
    from semg_desktop.ui.main_window import MainWindow

    def fake_exec(self):
        self.rb_sim.setChecked(True)
        return QDialog.Accepted

    monkeypatch.setattr(cd.ConnectDialog, "exec", fake_exec)
    win = MainWindow()
    _KEEP.append(win)
    win.open_connect()
    assert win.acq is not None
    assert win.act_disconnect.isEnabled()
    win.disconnect_source()
    win.close()


def test_attitude_widget_renders_yaw_and_reset(qapp, cfg):
    view = ImuView(cfg)
    _KEEP.append(view)
    view.show()
    view.set_attitude((10.0, -5.0, 90.0))
    qapp.processEvents()
    assert not view.attitude.grab().isNull()  # 触发含 yaw 的 paintEvent
    view.btn_yaw0.click()
    qapp.processEvents()
    assert not view.attitude.grab().isNull()
    view.set_attitude((0.0, 0.0, 90.0))  # 归零后同一航向应画成 0°
    qapp.processEvents()
    assert not view.attitude.grab().isNull()
    view.close()


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
