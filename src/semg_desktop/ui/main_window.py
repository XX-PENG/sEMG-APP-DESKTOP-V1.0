"""主窗口：腘绳肌损伤预防监护台的外壳与课次生命周期。

采集 / 协议 / 时标 / 录制 / OTA 全部沿用原有管线，这里只负责把领域引擎的快照分发到七个
业务视图，并管理"选运动员 → 选课目 → 开始课次 → 实时反馈 → 处置事件 → 结束出报告"这条
主流程。导航固定不随内容滚动，顶栏的运动员 / 课目 / 课次 / 录制 / 风险 / 静音始终可达。
"""

from __future__ import annotations

import json
import time
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import Config, load_config, save_channel_map
from ..guard.engine import GuardEngine, cue_for
from ..guard.model import DRILLS, DRILL_BY_ID, Thresholds
from ..guard.roster import Roster
from ..guard.session import build_summary
from ..pipeline import Acquisition, ChannelMap
from ..recorder import Recording, new_session_dir
from ..sources import AthleteSimSource, SourceError
from . import theme as T
from .connect_dialog import ConnectDialog
from .cues import CueAudio
from .devices_view import DevicesView
from .events_view import EventsView
from .fatigue_view import FatigueView
from .live_view import LiveView
from .muscle_view import MuscleView
from .report_view import ReportView
from .roster_view import RosterView
from .widgets import LevelPill, ShellBackground

PROJECT_ROOT = Path(__file__).resolve().parents[3]

NAV = [
    ("live", "实时守护"),
    ("muscle", "肌肉与步态"),
    ("fatigue", "疲劳与负荷"),
    ("events", "风险事件"),
    ("report", "会话报告"),
    ("roster", "运动员档案"),
    ("devices", "设备与固件"),
]


class LogoMark(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(34, 34)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(T.C["turf"]), 3, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(4, 4, 26, 26, 200 * 16, -250 * 16)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(T.C["magenta"]))
        p.drawEllipse(20, 6, 9, 9)
        p.setBrush(QColor(T.C["blue"]))
        p.drawEllipse(7, 21, 6, 6)
        p.end()


class MainWindow(QMainWindow):
    def __init__(self, config_path: str | None = None, source=None, parent=None) -> None:
        super().__init__(parent)
        self.cfg: Config = load_config(config_path)
        self.config_path = config_path
        self.roster = Roster(self._roster_path())
        self.profile = self.roster.athletes[0] if self.roster.athletes else None
        self.th = self.profile.thresholds_for(Thresholds()) if self.profile else Thresholds()
        self.drill = DRILL_BY_ID["sprint"]
        self.engine = GuardEngine(self.profile, self.th, self.drill) if self.profile else None
        self.audio = CueAudio()
        self.audio.set_enabled(self.th.sound)
        self.acq: Acquisition | None = None
        self.rec: Recording | None = None
        self.channel_map = ChannelMap()
        self.session_active = False
        self.session_start_wall = 0.0
        self.session_start_device = 0.0
        self.peak_score = 0.0
        self._last_error: str | None = None
        self._last_cue = "calm"
        self._last_cue_play = 0.0
        self._last_event_count = 0
        self._snap = None

        self.setWindowTitle("HAMSTRING GUARD · 曲棍球腘绳肌损伤预防监护台")
        self.resize(1560, 900)

        shell = ShellBackground()
        root = QHBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_rail())
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        rv.addWidget(self._build_topbar())
        self.tabs = QStackedWidget()
        rv.addWidget(self.tabs, 1)
        root.addWidget(right, 1)
        self.setCentralWidget(shell)

        self.live = LiveView(self.th)
        self.muscle = MuscleView(self.cfg, self.th)
        self.fatigue = FatigueView(self.th)
        self.events = EventsView()
        self.report = ReportView()
        self.roster_view = RosterView(self.roster)
        self.devices = DevicesView(self.cfg, self.th)
        for key, view in (("live", self.live), ("muscle", self.muscle), ("fatigue", self.fatigue),
                          ("events", self.events), ("report", self.report),
                          ("roster", self.roster_view), ("devices", self.devices)):
            self.tabs.addWidget(view)
        # 工程视图与 OTA 的真实归属在设备页内，保留引用供刷新与测试
        self.emg = self.devices.emg
        self.imu = self.devices.imu
        self.analysis = self.devices.analysis
        self.status = self.devices.status
        self.ota = self.devices.ota

        self.events.set_engine(self.engine)
        self.roster_view.selection_changed.connect(self.switch_athlete)
        self.roster_view.roster_changed.connect(self._roster_changed)
        self.roster_view.calibrate_requested.connect(self.calibrate_mvc)
        self.live.panel.changed.connect(self._thresholds_changed)
        self.emg.region_selected.connect(self.analysis.set_selection)

        self.lbl_status = QLabel("未连接")
        self.statusBar().addPermanentWidget(self.lbl_status)

        self.timer = QTimer(self)
        self.timer.setInterval(max(10, int(1000 / max(self.cfg.target_fps, 1))))
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

        self._select_nav("live")
        if source is not None:
            self.connect_source(source)
        else:
            self.connect_source(self._default_source())

    # ── 外壳 ────────────────────────────────────────────────────────────────

    def _roster_path(self) -> Path:
        base = Path(self.config_path).parent if self.config_path else PROJECT_ROOT / "config"
        return base / "athletes.json"

    def _build_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("NavRail")
        rail.setFixedWidth(206)
        lay = QVBoxLayout(rail)
        lay.setContentsMargins(14, 16, 14, 14)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.addWidget(LogoMark())
        title = QLabel("HAMSTRING\nGUARD")
        title.setStyleSheet(f"color:{T.C['text_warm']}; font-weight:800; font-size:15px;"
                            f"letter-spacing:2px; background:transparent;")
        head.addWidget(title)
        head.addStretch(1)
        lay.addLayout(head)
        sub = QLabel("曲棍球 · 腘绳肌损伤预防")
        sub.setObjectName("Dim")
        lay.addWidget(sub)
        lay.addSpacing(14)
        self._nav_buttons: dict[str, QPushButton] = {}
        for key, label in NAV:
            b = QPushButton(label)
            b.setObjectName("NavButton")
            b.setProperty("active", "false")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=key: self._select_nav(k))
            self._nav_buttons[key] = b
            lay.addWidget(b)
        lay.addStretch(1)
        self.lbl_link = QLabel("链路未连接")
        self.lbl_link.setObjectName("Dim")
        self.lbl_link.setWordWrap(True)
        lay.addWidget(self.lbl_link)
        return rail

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(66)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(10)

        lay.addWidget(QLabel("运动员"))
        self.cb_athlete = QComboBox()
        self.cb_athlete.setMinimumWidth(150)
        self._fill_athletes()
        self.cb_athlete.currentIndexChanged.connect(self._athlete_combo)
        lay.addWidget(self.cb_athlete)
        lay.addWidget(QLabel("课目"))
        self.cb_drill = QComboBox()
        for d in DRILLS:
            self.cb_drill.addItem(d.name, d.id)
        self.cb_drill.setCurrentIndex(1)
        self.cb_drill.currentIndexChanged.connect(self._drill_combo)
        lay.addWidget(self.cb_drill)

        self.btn_session = QPushButton("开始课次")
        self.btn_session.setObjectName("Primary")
        self.btn_session.clicked.connect(self.toggle_session)
        lay.addWidget(self.btn_session)
        self.lbl_clock = QLabel("00:00")
        self.lbl_clock.setStyleSheet(f"font-family:'{T.MONO_FAMILY}'; font-size:20px;"
                                     f"font-weight:700; color:{T.C['text_warm']}; background:transparent;")
        self.lbl_clock.setFixedWidth(64)
        lay.addWidget(self.lbl_clock)
        self.btn_record = QPushButton("录制")
        self.btn_record.clicked.connect(self.toggle_recording)
        lay.addWidget(self.btn_record)

        lay.addStretch(1)
        self.pill_risk = LevelPill("风险 —", "muted")
        lay.addWidget(self.pill_risk)
        self.btn_mute = QPushButton("提示音 开")
        self.btn_mute.setObjectName("Chip")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setChecked(False)
        self.btn_mute.clicked.connect(self._toggle_mute)
        lay.addWidget(self.btn_mute)
        self.btn_connect = QPushButton("连接…")
        self.btn_connect.setObjectName("Ghost")
        self.btn_connect.setShortcut("Ctrl+K")
        self.btn_connect.clicked.connect(self.open_connect)
        lay.addWidget(self.btn_connect)
        self.btn_disconnect = QPushButton("断开")
        self.btn_disconnect.setObjectName("Ghost")
        self.btn_disconnect.clicked.connect(self.disconnect_source)
        lay.addWidget(self.btn_disconnect)
        return bar

    def _fill_athletes(self) -> None:
        self.cb_athlete.blockSignals(True)
        self.cb_athlete.clear()
        for a in self.roster.athletes:
            self.cb_athlete.addItem(f"{a.name} #{a.number}", a.id)
        if self.profile:
            idx = next((i for i in range(self.cb_athlete.count())
                        if self.cb_athlete.itemData(i) == self.profile.id), 0)
            self.cb_athlete.setCurrentIndex(idx)
        self.cb_athlete.blockSignals(False)

    def _select_nav(self, key: str) -> None:
        index = next(i for i, (k, _l) in enumerate(NAV) if k == key)
        self.tabs.setCurrentIndex(index)
        for k, b in self._nav_buttons.items():
            b.setProperty("active", "true" if k == key else "false")
            b.style().unpolish(b)
            b.style().polish(b)
        if key == "events":
            self.events.refresh()

    # ── 上下文切换 ──────────────────────────────────────────────────────────

    def _athlete_combo(self, index: int) -> None:
        aid = self.cb_athlete.itemData(index)
        if aid and aid != (self.profile.id if self.profile else None):
            self.switch_athlete(aid)

    def switch_athlete(self, aid: str) -> None:
        profile = self.roster.get(aid)
        if profile is None:
            return
        self.profile = profile
        self.th = profile.thresholds_for(Thresholds())
        self.live.th = self.th
        self.live.panel.th = self.th
        self.live.panel.sync()
        self.muscle.th = self.th
        self.fatigue.th = self.th
        self.devices.th = self.th
        self.devices.sensor_map.th = self.th
        self.audio.set_enabled(self.th.sound)
        self._sync_mute_button()
        if self.engine:
            self.engine.set_profile(profile)
            self.engine.th = self.th
            self.engine.reset_events()
        self.events.set_engine(self.engine)
        self.roster_view.current_id = aid
        self._fill_athletes()
        was_active = self.session_active
        if was_active:
            self.stop_session()
        if isinstance(self.acq.source if self.acq else None, AthleteSimSource):
            self.disconnect_source()
            self.connect_source(self._default_source())
        self.live.set_context(profile, self.drill)
        self.lbl_status.setText(f"主监护：{profile.name} #{profile.number}")

    def _drill_combo(self, index: int) -> None:
        did = self.cb_drill.itemData(index)
        self.drill = DRILL_BY_ID.get(did, self.drill)
        if self.engine:
            self.engine.set_drill(self.drill)
        if self.acq and isinstance(self.acq.source, AthleteSimSource):
            self.acq.source.set_drill(self.drill.id)
        self.live.set_context(self.profile, self.drill)
        self.lbl_status.setText(f"课目：{self.drill.name} · {self.drill.purpose}")

    def _roster_changed(self) -> None:
        self._fill_athletes()
        if self.profile and self.roster.get(self.profile.id) is None:
            self.profile = self.roster.athletes[0] if self.roster.athletes else None
        if self.profile:
            self.engine.set_profile(self.profile)
            self.live.set_context(self.profile, self.drill)

    def _thresholds_changed(self) -> None:
        if self.engine:
            self.engine.th = self.th
            self.engine.sync_detector()
        self.audio.set_enabled(self.th.sound)
        self._sync_mute_button()
        if self.acq and isinstance(self.acq.source, AthleteSimSource):
            self.acq.source.fatigue_rate = self.th.fatigue_rate

    def _toggle_mute(self) -> None:
        muted = self.btn_mute.isChecked()
        self.th.sound = not muted
        self.audio.set_enabled(self.th.sound)
        self.live.panel.chk_sound.setChecked(self.th.sound)
        self._sync_mute_button()

    def _sync_mute_button(self) -> None:
        self.btn_mute.blockSignals(True)
        self.btn_mute.setChecked(not self.th.sound)
        self.btn_mute.setText("提示音 关" if not self.th.sound else "提示音 开")
        self.btn_mute.blockSignals(False)

    # ── 连接 ────────────────────────────────────────────────────────────────

    def _default_source(self):
        if self.profile is None:
            return AthleteSimSource()
        return AthleteSimSource.from_profile(self.profile, drill=self.drill.id,
                                             fatigue_rate=self.th.fatigue_rate)

    def open_connect(self) -> None:
        dlg = ConnectDialog(self.cfg, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.disconnect_source()
        self.connect_source(dlg.source())

    def open_replay(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择原始日志", str(self.records_root()),
                                              "*.gz;;*.log;;*.txt")
        if path:
            from ..sources import ReplaySource
            self.disconnect_source()
            self.connect_source(ReplaySource(path, speed=1.0))

    def records_root(self) -> Path:
        root = PROJECT_ROOT / "records"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def open_records(self) -> None:
        webbrowser.open(self.records_root().as_uri())

    def _channel_map_for(self, source) -> ChannelMap:
        if getattr(source, "name", "") in ("simulate", "athlete"):
            return ChannelMap()
        return ChannelMap({k: v for k, v in self.cfg.channel_map.items()
                           if v < self.cfg.semg.channel_count})

    def connect_source(self, source) -> None:
        self.channel_map = self._channel_map_for(source)
        acq = Acquisition(source, self.cfg, self.channel_map)
        try:
            acq.start()
        except SourceError as exc:
            QMessageBox.warning(self, "连接失败", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "连接失败", f"{type(exc).__name__}: {exc}")
            return
        self.acq = acq
        self.analysis.acq = acq
        self.btn_disconnect.setEnabled(True)
        if self.engine:
            self.engine.reset_metrics()
            self.engine.session_start = acq.now()
        self.live.set_context(self.profile, self.drill)
        self._last_error = None
        self.lbl_link.setText(source.describe())
        self.lbl_status.setText(source.describe())
        if isinstance(source, AthleteSimSource):
            source.fatigue_rate = self.th.fatigue_rate

    def disconnect_source(self) -> None:
        if self.acq is None:
            return
        if self.session_active:
            self.stop_session()
        self.stop_recording()
        self.acq.stop()
        if getattr(self.acq.source, "name", "") not in ("simulate", "athlete"):
            self.cfg.channel_map = self.channel_map.as_dict()
            if self.config_path:
                try:
                    save_channel_map(self.config_path, self.cfg.channel_map)
                except OSError as exc:
                    self.lbl_status.setText(f"通道映射写入失败：{exc}")
        self.acq = None
        self.analysis.acq = None
        self.btn_disconnect.setEnabled(False)
        self.lbl_link.setText("链路未连接")
        self.lbl_status.setText("已断开")

    # ── 课次 ────────────────────────────────────────────────────────────────

    def toggle_session(self) -> None:
        if self.session_active:
            self.stop_session()
            return
        if self.acq is None:
            QMessageBox.information(self, "未连接", "先连上数据源再开始课次。")
            return
        self.session_active = True
        self.session_start_wall = time.time()
        self.session_start_device = self.acq.now()
        self.peak_score = 0.0
        if self.engine:
            self.engine.reset_metrics()
            self.engine.reset_events()
            self.engine.session_start = self.session_start_device
        self.events.refresh()
        self.fatigue.reset_session()
        self.btn_session.setText("结束课次")
        self.btn_session.setObjectName("Danger")
        self.btn_session.style().unpolish(self.btn_session)
        self.btn_session.style().polish(self.btn_session)
        self.audio.play("start")
        self.lbl_status.setText(f"课次开始 · {self.drill.name}")

    def stop_session(self) -> None:
        if not self.session_active:
            return
        self.session_active = False
        duration = time.time() - self.session_start_wall
        snap = self._snap
        summary = None
        if self.engine and snap is not None and self.profile:
            meta = dict(athlete_id=self.profile.id, athlete_name=self.profile.name,
                        number=self.profile.number, position=self.profile.position,
                        drill=self.drill.name, started_wall=self.session_start_wall,
                        duration_seconds=duration, chronic_load=self.profile.chronic_load,
                        source=self.acq.source.describe() if self.acq else "",
                        peak_score=self.peak_score)
            summary = build_summary(self.engine, snap, meta)
            summary.note = ""
            self.report.set_summary(summary)
            self._persist_summary(summary)
        self.btn_session.setText("开始课次")
        self.btn_session.setObjectName("Primary")
        self.btn_session.style().unpolish(self.btn_session)
        self.btn_session.style().polish(self.btn_session)
        self.lbl_clock.setText("00:00")
        self.audio.play("stoprec")
        self.lbl_status.setText(f"课次结束 · {duration / 60:.1f} min · 报告已生成")
        self._select_nav("report")

    def _persist_summary(self, summary) -> None:
        try:
            root = self.rec.root if self.rec else new_session_dir(self.records_root())
            (root / "session_summary.json").write_text(
                json.dumps(summary.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def calibrate_mvc(self) -> None:
        if self.acq is None or self.engine is None:
            QMessageBox.information(self, "未连接", "先连上数据源，让运动员做最大自主收缩再标定。")
            return
        mvc = self.engine.capture_mvc(self.acq, seconds=3.0)
        if not mvc:
            QMessageBox.information(self, "无数据", "最近 3 s 没有足够包络样本，无法标定。")
            return
        self.roster_view.calibrate_mvc(mvc)
        self.engine.set_profile(self.profile)
        self.audio.play("ack")
        self.lbl_status.setText(f"MVC 已标定 {len(mvc)} 块肌肉")

    # ── 录制 ────────────────────────────────────────────────────────────────

    def toggle_recording(self) -> None:
        if self.rec is not None:
            self.stop_recording()
            return
        if self.acq is None:
            QMessageBox.information(self, "未连接", "先连上数据源再开始录制。")
            return
        cfg_dict = {}
        if self.config_path and Path(self.config_path).is_file():
            cfg_dict = json.loads(Path(self.config_path).read_text(encoding="utf-8"))
        rec = Recording(root=new_session_dir(self.records_root()),
                        source_description=self.acq.source.describe(),
                        config=cfg_dict, channel_map=self.channel_map.as_dict())
        self.acq.start_recording(rec)
        self.rec = rec
        self.btn_record.setText("停止录制")
        self.btn_record.setObjectName("Danger")
        self.btn_record.style().unpolish(self.btn_record)
        self.btn_record.style().polish(self.btn_record)
        self.lbl_status.setText(f"录制中 → {rec.root}")

    def stop_recording(self) -> None:
        if self.rec is None or self.acq is None:
            self.rec = None
            return
        written = self.acq.stop_recording()
        self.rec = None
        self.btn_record.setText("录制")
        self.btn_record.setObjectName("")
        self.btn_record.style().unpolish(self.btn_record)
        self.btn_record.style().polish(self.btn_record)
        self.lbl_status.setText(f"录制结束：{'，'.join(p.name for p in written) or '无文件'}")

    # ── 刷新 ────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        if self.acq is None or self.engine is None:
            return
        st = self.acq.stats()
        t_now = self.acq.now()
        snap = self.engine.update(self.acq, t_now)
        self._snap = snap
        if self.session_active:
            self.peak_score = max(self.peak_score, snap.score)
            elapsed = time.time() - self.session_start_wall
            self.lbl_clock.setText(f"{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}")

        name, col, _ = snap.band
        self.pill_risk.setText(f"风险 {snap.score:.0f} · {name}" if snap.data_ok else "风险 —",
                               {"低": "safe", "中": "warn", "高": "danger"}[name] if snap.data_ok else "muted")

        current = self.tabs.currentWidget()
        if current is self.live:
            self.live.update_snapshot(snap)
        elif current is self.muscle:
            span = self.muscle.span
            self.muscle.update_data(self.acq.emg_windows(span), self.acq.env_windows(span), t_now)
            t0 = self.session_start_device if self.session_active else (
                snap.strides[0].t if snap.strides else t_now)
            self.muscle.update_snapshot(snap, t0)
        elif current is self.fatigue:
            self.fatigue.update_data(self.engine, snap, t_now - self.session_start_device)
        elif current is self.devices:
            self.devices.update_data(st, self.acq.device_rows(), self.acq.status_log(), snap)
            inner = self.devices.inner.currentWidget()
            if inner is self.emg:
                self.emg.update_data(self.acq.emg_windows(self.emg.span),
                                     self.acq.env_windows(self.emg.span), t_now)
            elif inner is self.imu:
                t, samples = self.acq.imu_window(self.cfg.display_span_seconds)
                self.imu.update_data(t, samples, t_now)
                self.imu.set_latest(self.acq.imu_latest())
                self.imu.set_attitude(self.acq.imu_attitude())
            elif inner is self.analysis:
                self.analysis.update_data(t_now)
            elif inner is self.ota:
                self.ota.set_devices(self.acq.device_rows())

        if len(self.engine.events) != self._last_event_count:
            self._last_event_count = len(self.engine.events)
            if current is self.events:
                self.events.refresh()
            self.audio.play("ack")

        level, _title, _text = cue_for(snap, self.profile) if self.profile else ("calm", "", "")
        now = time.monotonic()
        if level != self._last_cue:
            self._last_cue = level
            if level in ("warn", "stop") and now - self._last_cue_play > 4.0:
                self._last_cue_play = now
                self.audio.play(level)
        elif level == "stop" and now - self._last_cue_play > 12.0:
            self._last_cue_play = now
            self.audio.play("stop")

        if st.error and st.error != self._last_error:
            self._last_error = st.error
            self.lbl_status.setText(f"采集停止：{st.error}")
        elif st.replay_progress is not None:
            self.lbl_status.setText(f"{st.source}　进度 {st.replay_progress * 100:.1f}%")
        elif not self.rec and not self.session_active:
            self.lbl_status.setText(f"{st.source}　{st.bytes_per_second:,.0f} B/s　"
                                    f"{st.frames_per_second:,.0f} 帧/s")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.ota.busy and QMessageBox.question(
                self, "正在升级", "固件还在传输中，关闭会中断这次写入。\n"
                                 "没写完的新槽不会成为启动目标，旧槽仍可正常启动。\n\n确定关闭？"
        ) != QMessageBox.Yes:
            event.ignore()
            return
        self.timer.stop()
        self.ota.shutdown()
        self.audio.shutdown()
        self.disconnect_source()
        self.roster.save()
        super().closeEvent(event)