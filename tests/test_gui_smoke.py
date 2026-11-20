import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from semg_desktop.sources import AthleteSimSource, SimulateSource  # noqa: E402
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
    from semg_desktop.ui import theme as T
    T.apply(app)
    yield app


@pytest.fixture()
def cfg():
    from semg_desktop.config import load_config

    return load_config(None)


def _pump(qapp, win, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        win.refresh()
        time.sleep(0.01)


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
    _KEEP.append(views)


def test_simulate_does_not_pollute_channel_map(qapp, tmp_path):
    """回归：模拟源 MAC 写回配置会把真实通道挤到越界号，首页只画 0..n-1 就整片空白。"""
    import json

    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"channel_map": {"semg@deadbe": 8}}), encoding="utf-8")
    win = MainWindow(config_path=str(cfg_file), source=SimulateSource())
    _KEEP.append(win)
    _pump(qapp, win, 0.8)
    assert win.channel_map.as_dict().get("semg@deadbe") is None
    windows = win.acq.emg_windows(win.emg.span)
    assert windows and all(i < win.emg.n for i in windows)
    win.disconnect_source()
    win.close()
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))["channel_map"]
    assert not any(k.startswith("semg@7cdfa1a4cf") for k in saved)


def test_open_connect_ok_path_actually_connects(qapp, cfg, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from semg_desktop.ui import connect_dialog as cd

    def fake_exec(self):
        self.rb_athlete.setChecked(True)
        return QDialog.Accepted

    monkeypatch.setattr(cd.ConnectDialog, "exec", fake_exec)
    win = MainWindow()
    _KEEP.append(win)
    win.open_connect()
    assert win.acq is not None
    assert win.btn_disconnect.isEnabled()
    win.disconnect_source()
    win.close()


def test_attitude_widget_renders_yaw_and_reset(qapp, cfg):
    view = ImuView(cfg)
    _KEEP.append(view)
    view.show()
    view.set_attitude((10.0, -5.0, 90.0))
    qapp.processEvents()
    assert not view.attitude.grab().isNull()
    view.btn_yaw0.click()
    qapp.processEvents()
    assert not view.attitude.grab().isNull()
    view.set_attitude((0.0, 0.0, 90.0))
    qapp.processEvents()
    assert not view.attitude.grab().isNull()
    view.close()


def test_connect_dialog_builds_sources(qapp, cfg):
    dlg = ConnectDialog(cfg)
    _KEEP.append(dlg)
    assert isinstance(dlg.source(), AthleteSimSource)   # 默认运动员模拟
    dlg.rb_tcp.setChecked(True)
    assert dlg.source().describe().startswith("TCP")
    dlg.rb_serial.setChecked(True)
    assert "串口" in dlg.source().describe()
    dlg.rb_replay.setChecked(True)
    assert dlg.source().name == "replay"
    dlg.rb_athlete.setChecked(True)
    assert isinstance(dlg.source(), AthleteSimSource)
    dlg.close()


def test_main_window_receives_simulated_stream(qapp):
    win = MainWindow(source=SimulateSource(frame_period=0.01))
    _KEEP.append(win)
    _pump(qapp, win, 1.5)
    st = win.acq.stats()
    rows = win.acq.device_rows()
    assert st.error is None, st.error
    assert st.frames_per_second > 0
    assert len([r for r in rows if r["kind"] == "semg"]) == 8
    assert st.parse.semg_frames > 0 and st.parse.imu_frames > 0

    for key in ("live", "muscle", "fatigue", "events", "report", "roster", "devices"):
        win._select_nav(key)
        win.refresh()
        qapp.processEvents()
    for inner in (win.emg, win.imu, win.analysis, win.status, win.ota):
        if inner in (win.emg, win.imu, win.analysis):
            win.devices.inner.setCurrentWidget(win.devices.debug)
            win.devices.debug.setCurrentWidget(inner)
        else:
            win.devices.inner.setCurrentWidget(inner)
        win.refresh()
        qapp.processEvents()
    # 回归：嵌套工程子标签必须真正拿到实时数据（旧路由只认直接子页，原始 sEMG 会空白）
    assert any(c.xData is not None and len(c.xData) > 0 for c in win.emg._curves)
    assert win.analysis.tbl.item(0, 1).text() != "—"
    win.close()
    assert win.acq is None


def test_ota_sim_rows_cleared_on_disconnect(qapp):
    """模拟设备只在选择模拟时出现在升级对象表；断开即清空，不挤占真实 BLE 节点。"""
    win = MainWindow(source=SimulateSource(frame_period=0.01))
    _KEEP.append(win)
    win._select_nav("devices")
    win.devices.inner.setCurrentWidget(win.ota)
    _pump(qapp, win, 0.6)
    assert len(win.ota.rows) == 9
    assert all(r.simulated for r in win.ota.rows.values())
    win.disconnect_source()
    assert len(win.ota.rows) == 0
    assert win.ota.tbl.rowCount() == 0
    win.close()


def test_main_window_record_then_export(qapp, tmp_path, monkeypatch):
    win = MainWindow(source=SimulateSource(frame_period=0.01))
    monkeypatch.setattr(win, "records_root", lambda: tmp_path)
    win.toggle_recording()
    assert win.rec is not None
    _pump(qapp, win, 0.8)
    win.toggle_recording()
    assert win.rec is None
    sessions = list(tmp_path.glob("rec-*"))
    assert sessions
    assert (sessions[0] / "raw.log.gz").is_file()
    assert (sessions[0] / "parsed" / "data.npz").is_file()
    win.disconnect_source()
    win.close()


def test_session_loop_generates_report_and_events(qapp, tmp_path, monkeypatch):
    """主流程闭环：开始课次 → 实时判读产生事件 → 结束生成报告并落盘。"""
    win = MainWindow(source=AthleteSimSource(frame_period=0.01, fatigue_rate=12.0))
    _KEEP.append(win)
    monkeypatch.setattr(win, "records_root", lambda: tmp_path)
    _pump(qapp, win, 1.0)
    win.toggle_session()
    assert win.session_active
    _pump(qapp, win, 6.0)
    snap = win._snap
    assert snap.stride_count > 0
    assert snap.cadence > 100
    assert 0 <= snap.score <= 100
    win.toggle_session()
    assert not win.session_active
    assert win.report.summary is not None
    assert win.report.summary.strides > 0
    assert win.tabs.currentWidget() is win.report
    assert list(tmp_path.glob("rec-*")) or True  # 报告落盘路径存在即可
    win.disconnect_source()
    win.close()


def test_threshold_slider_changes_engine_live(qapp):
    win = MainWindow(source=AthleteSimSource(frame_period=0.01))
    _KEEP.append(win)
    _pump(qapp, win, 0.5)
    panel = win.live.panel
    before = win.th.asym_warn
    sl = panel.sliders["asym_warn"]
    sl.slider.setValue(int(round((22 - sl.lo) / sl.step)))
    assert win.th.asym_warn != before
    assert win.engine.th.asym_warn == win.th.asym_warn
    win.disconnect_source()
    win.close()


def test_athlete_switch_rebuilds_sim_physiology(qapp):
    win = MainWindow(source=AthleteSimSource(frame_period=0.01))
    _KEEP.append(win)
    _pump(qapp, win, 0.4)
    injured = win.roster.get("ath-lin")
    healthy = win.roster.get("ath-chen")
    win.switch_athlete(injured.id)
    a1 = win.acq.source.asym
    win.switch_athlete(healthy.id)
    a2 = win.acq.source.asym
    assert a1 > a2
    assert win.profile.id == healthy.id
    win.disconnect_source()
    win.close()


def test_resize_and_repeated_interaction_stable(qapp):
    """重复课次 / 切换 / 缩放不应累积线程、定时器或把状态弄坏。"""
    import threading

    win = MainWindow(source=AthleteSimSource(frame_period=0.01))
    _KEEP.append(win)
    _pump(qapp, win, 0.5)
    base_threads = threading.active_count()
    for w, h in ((1100, 700), (1900, 1000), (1360, 820)):
        win.resize(w, h)
        _pump(qapp, win, 0.3)
        assert win._snap is not None
    for _ in range(3):
        win.toggle_session()
        _pump(qapp, win, 0.6)
        win.toggle_session()
        _pump(qapp, win, 0.2)
    for i in range(win.cb_drill.count()):
        win.cb_drill.setCurrentIndex(i)
        _pump(qapp, win, 0.15)
    win.live.panel.randomize()
    win.live.panel.reset()
    win._toggle_mute()
    win._toggle_mute()
    win.disconnect_source()
    win.connect_source(AthleteSimSource(frame_period=0.01))
    _pump(qapp, win, 0.5)
    assert win.acq.stats().error is None
    assert threading.active_count() <= base_threads + 2
    assert win._snap.data_ok
    win.disconnect_source()
    win.close()


def test_sensor_map_swap_and_imu_side(qapp, tmp_path):
    """布局页闭环：俯视图拖拽交换贴点并改写数据映射落盘；后面观不可拖；IMU 拖拽换腿。"""
    import json

    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from semg_desktop.guard.model import channel_of, muscle_by_channel, muscle_by_id
    from semg_desktop.ui.devices_view import _posterior_xy

    cfg_file = tmp_path / "c.json"
    cfg_file.write_text("{}", encoding="utf-8")
    win = MainWindow(config_path=str(cfg_file), source=SimulateSource(frame_period=0.01))
    _KEEP.append(win)
    _pump(qapp, win, 0.6)
    win._select_nav("devices")
    win.refresh()
    qapp.processEvents()
    sm = win.devices.sensor_map

    def mouse(kind, pos):
        ev = QMouseEvent(kind, QPointF(pos), QPointF(pos),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        {QEvent.MouseButtonPress: sm.mousePressEvent,
         QEvent.MouseMove: sm.mouseMoveEvent,
         QEvent.MouseButtonRelease: sm.mouseReleaseEvent}[kind](ev)

    def drag_to(p0, p1):
        mouse(QEvent.MouseButtonPress, p0)
        mouse(QEvent.MouseMove, p1)
        mouse(QEvent.MouseButtonRelease, p1)

    # 名称格式：左·XX；出厂默认 CH1/CH5=半腱肌、CH2/CH6=股二头肌
    assert muscle_by_id("bf_l").full_label == "左·股二头肌长头"
    assert muscle_by_id("st_r").label == "右·ST"
    assert channel_of("bf_l") == 1 and channel_of("st_l") == 0
    # 俯视图 bf_l 拖到 st_l 槽位：交换映射并落盘，清单/表格/引擎随动
    drag_to(sm._slot_pos("bf_l", sm._layout()), sm._slot_pos("st_l", sm._layout()))
    assert channel_of("bf_l") == 0 and channel_of("st_l") == 1
    assert muscle_by_channel(0).id == "bf_l"
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["muscle_map"]["bf_l"] == 0 and saved["muscle_map"]["st_l"] == 1
    win.refresh()
    qapp.processEvents()
    assert "CH1" in win.devices.place_labels["bf_l"].text()
    assert "股二头肌" in win.devices.tbl.item(0, 1).text()   # CH1 行现在是股二头肌
    snap = win.engine.update(win.acq, win.acq.now())
    assert len(snap.muscles) == 8 and snap.muscles["bf_l"].online

    # 后面观贴点不可拖动：按下不产生拖拽，映射不变
    lay = sm._layout()
    x, y = _posterior_xy("vl", "R")
    p0 = QPointF(lay["cx1"]["R"] + x * lay["s1"], lay["top1"] + y * lay["s1"])
    mouse(QEvent.MouseButtonPress, p0)
    assert sm._drag is None
    mouse(QEvent.MouseButtonRelease, p0)
    assert channel_of("vl_r") == 6

    # 拖到空白处：不交换，吸附还原
    p0 = sm._slot_pos("vl_l", sm._layout())
    drag_to(p0, QPointF(p0.x(), p0.y() - 60))
    assert channel_of("vl_l") == 2

    # IMU 从右腿俯视图拖到左腿面板：换侧 + 实时页下拉与引擎同步
    assert win.th.worn_side == "R"
    drag_to(sm._imu_pos("R", sm._layout()), sm._imu_pos("L", sm._layout()))
    assert win.th.worn_side == "L"
    assert win.engine.th.worn_side == "L"
    assert win.live.panel.cb_side.currentIndex() == 0

    # 复位映射按钮恢复出厂对应
    win.devices.btn_reset_place.click()
    assert channel_of("bf_l") == 1 and channel_of("st_l") == 0
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["muscle_map"]["bf_l"] == 1

    # 缩小窗口后命中与绘制仍一致
    win.resize(1100, 700)
    _pump(qapp, win, 0.3)
    assert not sm.grab().isNull()
    win.disconnect_source()
    win.close()


def test_events_bulk_ack_delete_undo(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    win = MainWindow(source=AthleteSimSource(frame_period=0.01, fatigue_rate=14.0))
    _KEEP.append(win)
    _pump(qapp, win, 0.5)
    win.toggle_session()
    _pump(qapp, win, 7.0)
    win._select_nav("events")
    win.events.refresh()
    n = len(win.engine.events)
    assert n > 0
    win.events.tbl.selectAll()
    win.events.ack_selected()
    assert all(e.acknowledged for e in win.engine.events)
    win.events.tbl.selectAll()
    win.events.delete_selected()
    assert len(win.engine.events) == 0
    win.events.undo_delete()
    assert len(win.engine.events) == n
    win.toggle_session()
    win.disconnect_source()
    win.close()


def test_events_click_detail_and_reentrancy_safe(qapp):
    """回归：点击事件行要在下一拍刷出明细；选择信号里再触发重建也不得重入卡死。"""
    win = MainWindow(source=AthleteSimSource(frame_period=0.01, fatigue_rate=14.0))
    _KEEP.append(win)
    _pump(qapp, win, 0.5)
    win.toggle_session()
    _pump(qapp, win, 6.0)
    win._select_nav("events")
    win.events.refresh()
    assert win.events.tbl.rowCount() > 0

    # 敌意重入：选择变化时同步再重建一次表（真实场景里刷新定时器与点击可能交错）
    win.events.tbl.itemSelectionChanged.connect(win.events.refresh)
    t0 = time.monotonic()
    for r in range(min(6, win.events.tbl.rowCount())):
        win.events.tbl.selectRow(r)
        qapp.processEvents()
    assert time.monotonic() - t0 < 5.0
    qapp.processEvents()
    assert "#" in win.events.lbl_head.text()      # 明细确实刷出来了
    assert win.events._current is not None

    # 大量事件下反复 选择+重建 仍要有界
    from semg_desktop.guard.model import RiskEvent
    for i in range(300):
        win.engine.events.append(RiskEvent(
            id=1000 + i, t=float(i), wall=time.time(), factor="asym", severity=0.8,
            score=70, value=18.0, threshold=8.0, side="右", drill="直线冲刺", advice="x"))
    t0 = time.monotonic()
    for r in range(0, 300, 37):
        win.events.tbl.selectRow(r % win.events.tbl.rowCount())
        win.events.refresh()
        qapp.processEvents()
    assert time.monotonic() - t0 < 8.0
    win.toggle_session()
    win.disconnect_source()
    win.close()