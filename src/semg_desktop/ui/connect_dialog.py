"""数据源选择对话框：先选连接方式，再填写必要参数。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
    TcpSource,
    list_serial_ports,
)

BAUD_CHOICES = [9600, 115200, 230400, 460800, 921600, 1_000_000, 2_000_000]


class ConnectDialog(QDialog):
    def __init__(self, cfg, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("连接设备")
        self.setMinimumWidth(580)
        intro = QLabel("选择接收数据的设备。现场训练通常使用 USB 连接；模拟与回放用于演示和复盘。")
        intro.setObjectName("PageHint")
        intro.setWordWrap(True)

        self.rb_serial = QRadioButton("USB 设备")
        self.rb_tcp = QRadioButton("网络接收器")
        self.rb_replay = QRadioButton("历史记录回放")
        self.rb_athlete = QRadioButton("运动员模拟")
        self.rb_serial.setChecked(True)
        for rb in (self.rb_serial, self.rb_tcp, self.rb_replay, self.rb_athlete):
            rb.toggled.connect(self._switch)

        radios = QVBoxLayout()
        for rb in (self.rb_serial, self.rb_tcp, self.rb_replay, self.rb_athlete):
            radios.addWidget(rb)

        self.ed_port = QLineEdit(cfg.serial.port or "")
        self.ed_port.setPlaceholderText("例如 COM7")
        self.cb_ports = QPushButton("刷新串口列表")
        self.cb_ports.clicked.connect(self._refresh_ports)
        self.cb_baud = QComboBox()
        for baud in BAUD_CHOICES:
            self.cb_baud.addItem(f"{baud:,}", baud)
        index = self.cb_baud.findData(cfg.serial.baudrate)
        if index < 0:
            self.cb_baud.addItem(f"{cfg.serial.baudrate:,}", cfg.serial.baudrate)
            index = self.cb_baud.count() - 1
        self.cb_baud.setCurrentIndex(index)
        self.lbl_ports = QLabel("")
        self.lbl_ports.setWordWrap(True)
        self.lbl_ports.setObjectName("Muted")
        serial_panel = QWidget()
        f = QFormLayout(serial_panel)
        row = QHBoxLayout()
        row.addWidget(self.ed_port, 1)
        row.addWidget(self.cb_ports)
        f.addRow("端口", row)
        f.addRow("", self.lbl_ports)
        f.addRow("波特率", self.cb_baud)

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

        athlete_panel = QWidget()
        f = QFormLayout(athlete_panel)
        note = QLabel("用内置信号演示完整的监测流程，不需要连接硬件。\n"
                      "设备状态会明确标注为模拟，不会写入真实设备通道。")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        f.addRow(note)

        self.stack = QStackedWidget()
        for w in (serial_panel, tcp_panel, replay_panel, athlete_panel):
            self.stack.addWidget(w)

        left = QVBoxLayout()
        left.addLayout(radios)
        left.addWidget(self.stack, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("连接")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)
        root.addWidget(intro)
        content = QFrame()
        content.setObjectName("Card")
        content_lay = QVBoxLayout(content)
        content_lay.setContentsMargins(16, 14, 16, 14)
        content_lay.addLayout(left)
        root.addWidget(content)
        root.addWidget(buttons)
        self._switch()
        self._refresh_ports()

    def _switch(self) -> None:
        self.stack.setCurrentIndex(
            0 if self.rb_serial.isChecked()
            else 1 if self.rb_tcp.isChecked()
            else 2 if self.rb_replay.isChecked()
            else 3
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
        if self.rb_athlete.isChecked():
            return AthleteSimSource()
        return SerialSource(self.ed_port.text().strip(), baudrate=self.cb_baud.currentData(),
                           timeout=self.cfg.serial.timeout_seconds)

