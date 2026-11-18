"""主窗口：串起采集管线与四个视图。"""

from __future__ import annotations

import json
import webbrowser
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..config import Config, load_config, save_channel_map
from ..pipeline import Acquisition, ChannelMap
from ..recorder import Recording, new_session_dir
from ..sources import SourceError
from .analysis_view import AnalysisView
from .connect_dialog import ConnectDialog
from .emg_view import EmgView
from .imu_view import ImuView
from .ota_view import OtaView
from .status_panel import StatusPanel

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class MainWindow(QMainWindow):
    def __init__(self, config_path: str | None = None, source=None, parent=None) -> None:
        super().__init__(parent)
        self.cfg: Config = load_config(config_path)
        self.config_path = config_path
        self.channel_map = self._channel_map_for(source)
        self.acq: Acquisition | None = None
        self.rec: Recording | None = None
        self._last_error: str | None = None

        self.setWindowTitle("sEMG / IMU 实时采集")
        self.resize(1480, 860)

        self.tabs = QTabWidget()
        self.emg = EmgView(self.cfg)
        self.imu = ImuView(self.cfg)
        self.analysis = AnalysisView(self.cfg)
        self.status = StatusPanel(self.cfg)
        self.ota = OtaView(self.cfg)
        self.tabs.addTab(self.emg, "实时曲线 (sEMG ×8)")
        self.tabs.addTab(self.imu, "IMU")
        self.tabs.addTab(self.analysis, "分析")
        self.tabs.addTab(self.status, "状态与链路")
        self.tabs.addTab(self.ota, "固件升级 (OTA)")
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.tabs)
        self.setCentralWidget(central)
        self.emg.region_selected.connect(self.analysis.set_selection)

        bar = QToolBar("主工具栏")
        bar.setMovable(False)
        self.addToolBar(bar)
        self.act_connect = QAction("连接…", self)
        self.act_connect.setShortcut("Ctrl+K")
        self.act_connect.triggered.connect(self.open_connect)
        self.act_disconnect = QAction("断开", self)
        self.act_disconnect.triggered.connect(self.disconnect_source)
        self.act_record = QAction("开始录制", self)
        self.act_record.setShortcut("Ctrl+R")
        self.act_record.triggered.connect(self.toggle_recording)
        self.act_records = QAction("录制目录", self)
        self.act_records.triggered.connect(self.open_records)
        self.act_replay = QAction("回放日志…", self)
        self.act_replay.triggered.connect(self.open_replay)
        for act in (self.act_connect, self.act_disconnect, self.act_record, self.act_replay, self.act_records):
            bar.addAction(act)
        self.act_disconnect.setEnabled(False)

        self.lbl_status = QLabel("未连接")
        self.statusBar().addPermanentWidget(self.lbl_status)

        self.timer = QTimer(self)
        self.timer.setInterval(max(10, int(1000 / max(self.cfg.target_fps, 1))))
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

        if source is not None:
            self.connect_source(source)

    # ── 连接 ────────────────────────────────────────────────────────────────

    def open_connect(self) -> None:
        dlg = ConnectDialog(self.cfg, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.disconnect_source()
        self.connect_source(dlg.source())

    def open_replay(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择原始日志", str(self.records_root()), "*.gz;;*.log;;*.txt")
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
        # 模拟源 MAC 是假的：不继承真实设备的预留槽位（否则假 MAC 溢出到越界号，首页整片空白），
        # 也不把越界的旧映射带进来
        if getattr(source, "name", "") == "simulate":
            return ChannelMap()
        return ChannelMap(
            {k: v for k, v in self.cfg.channel_map.items() if v < self.cfg.semg.channel_count}
        )

    def connect_source(self, source) -> None:
        self.channel_map = self._channel_map_for(source)
        acq = Acquisition(source, self.cfg, self.channel_map)
        try:
            acq.start()
        except SourceError as exc:
            QMessageBox.warning(self, "连接失败", str(exc))
            return
        except Exception as exc:  # pyserial/socket 在 open 里的非 OSError 异常
            QMessageBox.warning(self, "连接失败", f"{type(exc).__name__}: {exc}")
            return
        self.acq = acq
        self.analysis.acq = acq
        self.act_disconnect.setEnabled(True)
        self.act_replay.setEnabled(False)
        self.setWindowTitle(f"sEMG / IMU 实时采集 — {source.describe()}")
        self.lbl_status.setText(source.describe())
        self._last_error = None

    def disconnect_source(self) -> None:
        if self.acq is None:
            return
        self.stop_recording()
        self.acq.stop()
        rows = self.acq.device_rows()
        # 模拟源的 MAC 是假的，写回配置会把真实通道位置挤到看不见的越界号上
        if getattr(self.acq.source, "name", "") != "simulate":
            self.cfg.channel_map = self.channel_map.as_dict()
            if self.config_path:
                try:
                    save_channel_map(self.config_path, self.cfg.channel_map)
                except OSError as exc:
                    self.lbl_status.setText(f"通道映射写入失败：{exc}")
        self.lbl_status.setText(
            f"已断开：{len([r for r in rows if r['kind'] == 'semg'])} 路 sEMG、"
            f"{len([r for r in rows if r['kind'] == 'imu'])} 路 IMU"
        )
        self.acq = None
        self.analysis.acq = None
        self.act_disconnect.setEnabled(False)

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
        rec = Recording(
            root=new_session_dir(self.records_root()),
            source_description=self.acq.source.describe(),
            config=cfg_dict,
            channel_map=self.channel_map.as_dict(),
        )
        self.acq.start_recording(rec)
        self.rec = rec
        self.act_record.setText("停止录制")
        self.lbl_status.setText(f"录制中 → {rec.root}")

    def stop_recording(self) -> None:
        if self.rec is None or self.acq is None:
            self.rec = None
            return
        written = self.acq.stop_recording()
        self.rec = None
        self.act_record.setText("开始录制")
        files = "，".join(p.name for p in written)
        self.lbl_status.setText(f"录制结束：{files or '无文件'}")

    # ── 刷新 ────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        if self.acq is None:
            return
        st = self.acq.stats()
        t_now = self.acq.now()
        rows = self.acq.device_rows()
        current = self.tabs.currentWidget()

        if current is self.emg:
            span = self.emg.span
            self.emg.update_data(self.acq.emg_windows(span), self.acq.env_windows(span), t_now)
        elif current is self.imu:
            t, samples = self.acq.imu_window(self.cfg.display_span_seconds)
            self.imu.update_data(t, samples, t_now)
            self.imu.set_latest(self.acq.imu_latest())
            self.imu.set_attitude(self.acq.imu_attitude())
        elif current is self.analysis:
            self.analysis.update_data(t_now)
        elif current is self.ota:
            self.ota.set_devices(rows)
        self.status.update_data(st, rows, self.acq.status_log())

        if st.error and st.error != self._last_error:
            self._last_error = st.error
            self.setWindowTitle(f"sEMG / IMU 实时采集 — 采集停止：{st.error}")
        if st.replay_progress is not None:
            self.lbl_status.setText(f"{st.source}　进度 {st.replay_progress * 100:.1f}%")
        elif not self.rec:
            self.lbl_status.setText(
                f"{st.source}　{st.bytes_per_second:,.0f} B/s　{st.frames_per_second:,.0f} 帧/s"
            )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self.ota.busy and QMessageBox.question(
                self, "正在升级", "固件还在传输中，关闭会中断这次写入。\n"
                                 "没写完的新槽不会成为启动目标，旧槽仍可正常启动。\n\n确定关闭？"
        ) != QMessageBox.Yes:
            event.ignore()
            return
        self.timer.stop()
        self.ota.shutdown()
        self.disconnect_source()
        super().closeEvent(event)

