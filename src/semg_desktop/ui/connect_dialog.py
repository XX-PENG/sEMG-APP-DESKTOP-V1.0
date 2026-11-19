"""数据源选择对话框。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..sources import (
    AthleteSimSource,
    DataSource,
    ReplaySource,
    SerialSource,
    SimulateSource,
    TcpSource,
    list_serial_ports,
)

BAUD_CHOICES = [9600, 115200, 230400, 460800, 921600, 1_000_000, 2_000_000]


class ConnectDialog(QDialog):
    def __init__(self, cfg, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("选择数据源")
        self.setMinimumWidth(520)

        self.rb_serial = QRadioButton("USB 串口")
        self.rb_tcp = QRadioButton("TCP 透传")
        self.rb_replay = QRadioButton("回放原始日志")
        self.rb_sim = QRadioButton("内置模拟信号")
        self.rb_athlete = QRadioButton("运动员模拟（步态锁定）")
        self.rb_athlete.setChecked(True)
        for rb in (self.rb_serial, self.rb_tcp, self.rb_replay, self.rb_sim, self.rb_athlete):
            rb.toggled.connect(self._switch)

        radios = QVBoxLayout()
        for rb in (self.rb_athlete, self.rb_serial, self.rb_tcp, self.rb_replay, self.rb_sim):
            radios.addWidget(rb)

        self.ed_port = QLineEdit(cfg.serial.port or "")
        self.cb_ports = QPushButton("刷新串口列表")
        self.cb_ports.clicked.connect(self._refresh_ports)
        self.sp_baud = QSpinBox()
        self.sp_baud.setRange(1200, 4_000_000)
        self.sp_baud.setValue(cfg.serial.baudrate)
        self.sp_baud.setSingleStep(1000)
        self.lbl_ports = QLabel("")
        self.lbl_ports.setWordWrap(True)
        self.lbl_ports.setStyleSheet("color:#aaa")
        serial_panel = QWidget()
        f = QFormLayout(serial_panel)
        row = QHBoxLayout()
        row.addWidget(self.ed_port, 1)
        row.addWidget(self.cb_ports)
        f.addRow("端口", row)
        f.addRow("", self.lbl_ports)
        f.addRow("波特率", self.sp_baud)

        self.ed_host = QLineEdit(cfg.tcp.host)
        self.sp_port = QSpinBox()
        self.sp_port.setRange(1, 65535)
        self.sp_port.setValue(cfg.tcp.port)
        tcp_panel = QWidget()
        f = QFormLayout(tcp_panel)
        f.addRow("地址", self.ed_host)
        f.addRow("端口", self.sp_port)

        self.ed_file = QLineEdit()
        self.btn_file = QPushButton("浏览…")
        self.btn_file.clicked.connect(self._pick_file)
        self.sp_speed = QDoubleSpinBox()
        self.sp_speed.setRange(0.05, 20.0)
        self.sp_speed.setValue(1.0)
        self.chk_nopace = QCheckBox("全速灌入（不节流）")
        replay_panel = QWidget()
        f = QFormLayout(replay_panel)
        row = QHBoxLayout()
        row.addWidget(self.ed_file, 1)
        row.addWidget(self.btn_file)
        f.addRow("文件", row)
        f.addRow("倍速", self.sp_speed)
        f.addRow("", self.chk_nopace)

        sim_panel = QWidget()
        f = QFormLayout(sim_panel)
        f.addRow("", QLabel("按网关文本协议生成 8 路 sEMG + 1 路 IMU，\n用于无硬件联调、界面演示与链路自检。"))

        athlete_panel = QWidget()
        f = QFormLayout(athlete_panel)
        f.addRow("", QLabel("EMG 爆发与步态相位锁定、频谱随疲劳下移、\n"
                            "左右不对称按档案推演。无硬件时演示完整判读闭环。\n"
                            "输出仍是网关文本协议，解析/时标/录制与真实硬件一致。"))

        self.stack = QStackedWidget()
        for w in (serial_panel, tcp_panel, replay_panel, sim_panel, athlete_panel):
            self.stack.addWidget(w)

        left = QVBoxLayout()
        left.addLayout(radios)
        left.addWidget(self.stack, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(left)
        root.addWidget(buttons)
        self._switch()

    def _switch(self) -> None:
        self.stack.setCurrentIndex(
            0 if self.rb_serial.isChecked()
            else 1 if self.rb_tcp.isChecked()
            else 2 if self.rb_replay.isChecked()
            else 3 if self.rb_sim.isChecked()
            else 4
        )

    def _refresh_ports(self) -> None:
        ports = list_serial_ports()
        lines = "\n".join(f"{d}  {desc}" for d, desc in ports[:12]) or "未发现串口设备"
        self.lbl_ports.setText(lines)
        if ports:
            self.ed_port.setText(ports[0][0])

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择原始日志", str(Path("records").resolve()), "日志 (*.log *.txt *.gz);;所有文件 (*)"
        )
        if path:
            self.ed_file.setText(path)

    def source(self) -> DataSource:
        if self.rb_tcp.isChecked():
            return TcpSource(self.ed_host.text().strip() or self.cfg.tcp.host, self.sp_port.value(),
                             capacity_bytes_per_second=self.cfg.link_capacity["tcp"])
        if self.rb_replay.isChecked():
            return ReplaySource(self.ed_file.text().strip(), speed=self.sp_speed.value(),
                                pace=not self.chk_nopace.isChecked())
        if self.rb_sim.isChecked():
            return SimulateSource()
        if self.rb_athlete.isChecked():
            return AthleteSimSource()
        return SerialSource(self.ed_port.text().strip(), baudrate=self.sp_baud.value(),
                           timeout=self.cfg.serial.timeout_seconds)

