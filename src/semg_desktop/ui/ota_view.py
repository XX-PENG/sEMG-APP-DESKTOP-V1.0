"""固件升级页签：EMG / IMU 节点应用固件的 BLE 两阶段 OTA。

界面只做三件事：认出设备（网关设备表 + BLE 扫描合并）、选固件、把 ota.py 的回调转成进度与日志。
bleak 是 asyncio 的，所以扫描和升级都丢给后台线程，主线程只收信号。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import ota
from ..config import Config

COLUMNS = ["升级", "类型", "通道", "MAC 后 3 字节", "BLE 地址", "固件", "状态"]
KIND_TITLES = {"semg": "EMG", "imu": "IMU"}
BAD = QColor(255, 120, 120)
WARN = QColor(255, 200, 110)
GOOD = QColor(150, 220, 150)
DIM = QColor(200, 200, 200)


@dataclass
class OtaRow:
    """一台板子。行按 MAC 后 3 字节归并：应用固件和接收固件是同一块板，不该占两行。"""

    suffix: str
    kind: str = ""
    address: str | None = None
    label: str = ""
    source: str = ""
    firmware: str = ""                 # 逐行覆盖，空则用该类型的默认固件
    state: str = "空闲"
    receiver: bool = False             # 此刻正广播接收固件（上次升级没做完）
    checked: bool = False
    busy: bool = False

    @property
    def title(self) -> str:
        return self.label or self.suffix or self.address or "未知设备"

    def node(self) -> ota.OtaNode:
        return ota.OtaNode(kind=self.kind, suffix=self.suffix, address=self.address,
                           label=self.title, source=self.source, uid=self.suffix)


class _JobSignals(QObject):
    log = Signal(str)
    progress = Signal(str, int, int)   # 行 key，已完成字节，总字节
    state = Signal(str, str)
    scanned = Signal(object)           # list[ota.Advert]
    failed = Signal(str)
    finished = Signal()


class _JobThread(threading.Thread):
    """bleak 的工作线程：自带事件循环，取消靠 Event，进度靠信号回主线程。"""

    def __init__(self, fn, sig: _JobSignals) -> None:
        super().__init__(daemon=True, name="semg-ota")
        self._fn = fn
        self._sig = sig
        self.cancel = threading.Event()

    def run(self) -> None:
        # 进度回调只带字节数，当前是哪个节点靠最近一次状态变更记住
        last = {"key": ""}

        def on_state(key: str, text: str) -> None:
            last["key"] = key
            self._sig.state.emit(key, text)

        rep = ota.OtaReporter(on_log=self._sig.log.emit,
                              on_progress=lambda d, t: self._sig.progress.emit(last["key"], d, t),
                              on_state=on_state, cancel=self.cancel)
        try:
            self._fn(rep)
        except ota.OtaCancelled:
            self._sig.log.emit("已取消")
        except Exception as exc:  # 线程兜底：不让异常把界面进度卡在半路
            self._sig.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self._sig.finished.emit()


class OtaView(QWidget):
    def __init__(self, cfg: Config, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.rows: dict[str, OtaRow] = {}
        self.defaults: dict[str, str] = {k: str(v or "") for k, v in (cfg.ota.default_bins or {}).items()}
        self._job: _JobThread | None = None
        self._sig = _JobSignals(self)
        self._sig.log.connect(self._on_log)
        self._sig.progress.connect(self._on_progress)
        self._sig.state.connect(self._on_state)
        self._sig.scanned.connect(self._on_scanned)
        self._sig.failed.connect(lambda msg: self._on_log(f"异常：{msg}"))
        self._sig.finished.connect(self._on_job_finished)
        self._index: dict[str, int] = {}
        self._by_index: dict[int, str] = {}
        self._desc_cache: dict[tuple, ota.AppDesc | None] = {}

        self.tbl = QTableWidget(0, len(COLUMNS))
        self.tbl.setHorizontalHeaderLabels(COLUMNS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.itemChanged.connect(self._on_item_changed)

        self.btn_scan = QPushButton("扫描 BLE 节点")
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_all_emg = QPushButton("全选 EMG")
        self.btn_all_emg.clicked.connect(lambda: self._check_by_kind("semg"))
        self.btn_all_imu = QPushButton("全选 IMU")
        self.btn_all_imu.clicked.connect(lambda: self._check_by_kind("imu"))
        self.btn_none = QPushButton("清空选择")
        self.btn_none.clicked.connect(lambda: self._set_all_checked(False))
        sel = QHBoxLayout()
        for b in (self.btn_scan, self.btn_all_emg, self.btn_all_imu, self.btn_none):
            sel.addWidget(b)
        box_dev = QGroupBox("升级对象")
        dv = QVBoxLayout(box_dev)
        dv.addWidget(self.tbl)
        dv.addLayout(sel)

        self.ed_emg = QLineEdit(self.defaults.get("semg", ""))
        self.ed_imu = QLineEdit(self.defaults.get("imu", ""))
        for line in (self.ed_emg, self.ed_imu):
            line.editingFinished.connect(lambda _=None, ln=line: self._defaults_edited())
        self.btn_emg = QPushButton("EMG 固件…")
        self.btn_emg.clicked.connect(lambda: self._pick(self.ed_emg))
        self.btn_imu = QPushButton("IMU 固件…")
        self.btn_imu.clicked.connect(lambda: self._pick(self.ed_imu))
        self.btn_row_fw = QPushButton("为选中行单独指定固件…")
        self.btn_row_fw.clicked.connect(self._pick_for_checked)
        self.lbl_fw = QLabel("未选择固件")
        self.lbl_fw.setWordWrap(True)
        self.lbl_fw.setStyleSheet("color:#d0d0d0")
        fw = QGroupBox("应用固件（idf.py build 产物，各 ≤ 2 MB）")
        fv = QVBoxLayout(fw)
        row = QHBoxLayout()
        row.addWidget(QLabel("EMG"))
        row.addWidget(self.ed_emg, 1)
        row.addWidget(self.btn_emg)
        fv.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("IMU"))
        row.addWidget(self.ed_imu, 1)
        row.addWidget(self.btn_imu)
        fv.addLayout(row)
        fv.addWidget(self.btn_row_fw)
        fv.addWidget(self.lbl_fw)

        self.ed_prefix = QLineEdit(cfg.ota.receiver_name_prefix)
        self.sp_scan = QDoubleSpinBox()
        self.sp_scan.setRange(1.0, 60.0)
        self.sp_scan.setValue(cfg.ota.scan_timeout_seconds)
        self.sp_wait = QDoubleSpinBox()
        self.sp_wait.setRange(5.0, 600.0)
        self.sp_wait.setValue(cfg.ota.trigger_wait_seconds)
        self.sp_wait.setToolTip("阶段1 会一直扫到这里超时；期间把网关断电或搬远，节点一空下来就立刻抢下并触发")
        self.sp_reboot = QDoubleSpinBox()
        self.sp_reboot.setRange(0.0, 60.0)
        self.sp_reboot.setSingleStep(0.5)
        self.sp_reboot.setValue(cfg.ota.reboot_wait_seconds)
        self.sp_reboot.setToolTip("触发后的固定等待。阶段2 会边扫边等节点，这里不用留太久")
        self.sp_ack = QDoubleSpinBox()
        self.sp_ack.setRange(1.0, 120.0)
        self.sp_ack.setValue(cfg.ota.ack_timeout_seconds)
        opts = QGroupBox("参数")
        of = QFormLayout(opts)
        of.addRow("接收固件名", self.ed_prefix)
        of.addRow("扫描秒数", self.sp_scan)
        of.addRow("等节点空闲", self.sp_wait)
        of.addRow("重启等待", self.sp_reboot)
        of.addRow("ACK 超时", self.sp_ack)

        self.btn_start = QPushButton("开始升级")
        self.btn_start.clicked.connect(self.start_upgrade)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.cancel_job)
        self.btn_cancel.setEnabled(False)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.lbl_current = QLabel("空闲")
        run = QGroupBox("执行")
        rv = QVBoxLayout(run)
        row = QHBoxLayout()
        row.addWidget(self.btn_start, 1)
        row.addWidget(self.btn_cancel)
        rv.addLayout(row)
        rv.addWidget(self.bar)
        rv.addWidget(self.lbl_current)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setStyleSheet("font-family: Consolas, Menlo, monospace;")
        box_log = QGroupBox("日志")
        lv = QVBoxLayout(box_log)
        lv.addWidget(self.log)

        right = QVBoxLayout()
        right.addWidget(fw)
        right.addWidget(opts)
        right.addWidget(run)
        right_w = QWidget()
        right_w.setLayout(right)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(box_dev)
        split.addWidget(right_w)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        outer = QSplitter(Qt.Vertical)
        outer.addWidget(split)
        outer.addWidget(box_log)
        outer.setStretchFactor(0, 3)
        outer.setStretchFactor(1, 1)

        root = QVBoxLayout(self)
        root.addWidget(outer, 1)

        if not ota.bleak_available():
            self._set_enabled(False)
            self._on_log("缺少 bleak 依赖：pip install bleak（同时本机要有可用蓝牙适配器）")

    # ── 设备表 ────────────────────────────────────────────────────────────────

    def set_devices(self, rows: list[dict]) -> None:
        """由 MainWindow.refresh() 喂进来：网关文本流里看到的节点，完整 MAC 来自 connected 行。"""
        if self._job is not None:
            return
        changed = False
        for raw in rows:
            node = ota.node_from_row(raw)
            key = node.suffix or (node.address or "")
            if not key:
                continue
            row = self.rows.get(key)
            if row is None:
                self.rows[key] = OtaRow(suffix=key, kind=node.kind, address=node.address,
                                       label=node.label, source="网关")
                changed = True
                continue
            if row.kind != node.kind or row.address != node.address or row.label != node.label:
                row.kind, row.address, row.label = node.kind, node.address, node.label
                changed = True
            if "网关" not in row.source:
                row.source = "+".join([s for s in (row.source, "网关") if s])
                changed = True
        if changed:
            self._rebuild()

    def _on_scanned(self, advs: list[ota.Advert]) -> None:
        changed = False
        for adv in advs:
            node = ota.node_from_advert(adv)
            if node is not None:  # 应用固件：带类型与后 3 字节
                key = node.suffix or adv.address
                row = self.rows.get(key)
                if row is None:
                    self.rows[key] = OtaRow(suffix=key, kind=node.kind, address=node.address,
                                           label=node.app_name, source="扫描")
                    changed = True
                    continue
                if row.address != node.address or row.kind != node.kind:
                    row.address, row.kind = node.address, node.kind
                    changed = True
                if "扫描" not in row.source:
                    row.source = "+".join([s for s in (row.source, "扫描") if s])
                    changed = True
                if row.receiver:
                    row.receiver = False  # 又能连上应用固件，说明它不在接收模式了
                    changed = True
            elif adv.is_receiver(self.ed_prefix.text().strip() or ota.RECEIVER_NAME_PREFIX):
                # 接收固件不带类型，按地址尾巴归并到同一块板；纯接收态只能单独指定固件
                key = adv.suffix or adv.address
                row = self.rows.get(key)
                if row is None:
                    self.rows[key] = OtaRow(suffix=key, address=adv.address, source="扫描",
                                            label=adv.address, receiver=True)
                    changed = True
                else:
                    row.address, row.receiver = adv.address, True
                    row.source = "+".join([s for s in (row.source, "扫描") if s])
                    changed = True
        if changed:
            self._rebuild()
        prefix = self.ed_prefix.text().strip() or ota.RECEIVER_NAME_PREFIX
        apps = [a for a in advs if a.kind]
        receivers = [a for a in advs if a.is_receiver(prefix)]
        self._on_log(f"扫描到 {len(advs)} 个广播：应用固件 {len(apps)} 台、接收固件 {len(receivers)} 台；"
                     f"表格里共 {len(self.rows)} 个对象")
        for a in apps[:12]:
            self._on_log(f"  · {a.name}  {a.address}")
        for a in receivers[:6]:
            self._on_log(f"  · {a.address} 已在接收固件模式（可直接续传）")
        if not apps and not receivers:
            self._on_log("  场上没有 EMG-* / IMU-* 在广播：它们被网关连着时不广播，"
                         "请先断开网关或让节点单独上电")

    def _rebuild(self) -> None:
        """整表重绘，保留勾选与状态。只有在设备身份集合变化时才走到这里。"""
        self.tbl.blockSignals(True)
        order = sorted(self.rows.values(), key=lambda r: (r.kind != "semg", r.kind, r.suffix))
        self.tbl.setRowCount(len(order))
        self._index, self._by_index = {}, {}
        for i, row in enumerate(order):
            self._index[row.suffix] = i
            self._by_index[i] = row.suffix
            checks = QTableWidgetItem()
            checks.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            checks.setCheckState(Qt.Checked if row.checked else Qt.Unchecked)
            self.tbl.setItem(i, 0, checks)
            kind = KIND_TITLES.get(row.kind, "未知")
            if row.receiver:
                kind += " · 接收中"
            cells = [kind, row.label or "—", row.suffix or "—", row.address or "未知",
                     self._firmware_text(row), row.state]
            for c, text in enumerate(cells, start=1):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if c == 4:
                    item.setForeground(GOOD if Path(self.firmware_for(row)).is_file() else BAD
                                       if self.firmware_for(row) else WARN)
                self.tbl.setItem(i, c, item)
        self.tbl.blockSignals(False)
        self._update_summary()

    def _firmware_text(self, row: OtaRow) -> str:
        path = self.firmware_for(row)
        if not path:
            return "未指定"
        p = Path(path)
        if not p.is_file():
            return f"{p.name} 文件不存在"
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        if key not in self._desc_cache:
            self._desc_cache[key] = ota.parse_app_desc(p.read_bytes())
        desc = self._desc_cache[key]
        mark = "（自定）" if row.firmware else ""
        ver = f" [{desc.version}]" if desc else ""
        return f"{p.name}{mark}{ver} {st.st_size:,} B"

    def firmware_for(self, row: OtaRow) -> str:
        return row.firmware or self.defaults.get(row.kind, "")

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """勾选只存在表格里，重绘前必须写回行模型，否则新设备一出现就把选择吞掉。"""
        if item.column() != 0:
            return
        row = self.rows.get(self._by_index.get(item.row(), ""))
        if row is not None:
            row.checked = item.checkState() == Qt.Checked

    def _checked_rows(self) -> list[OtaRow]:
        out = []
        for key, i in self._index.items():
            item = self.tbl.item(i, 0)
            if item is not None and item.checkState() == Qt.Checked:
                out.append(self.rows[key])
        return out

    def _set_all_checked(self, value: bool) -> None:
        for row in self.rows.values():
            row.checked = value
        self._rebuild()

    def _check_by_kind(self, kind: str) -> None:
        for row in self.rows.values():
            row.checked = row.kind == kind
        self._rebuild()

    def _update_summary(self) -> None:
        rows = list(self.rows.values())
        ready = [r for r in rows if Path(self.firmware_for(r)).is_file()]
        self.lbl_fw.setText(
            f"已识别 {len(rows)} 个对象，其中 {len(ready)} 个固件就绪；"
            f"每块板写入的是与上次正常槽相反的槽，旧槽留给 bootloader 回滚。"
        )

    # ── 固件选择 ──────────────────────────────────────────────────────────────

    def _pick(self, line: QLineEdit) -> None:
        start = line.text().strip() or self.cfg.ota.firmware_dir or str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, "选择应用固件 .bin", start,
                                              "ESP 固件 (*.bin);;所有文件 (*)")
        if not path:
            return
        line.setText(path)
        self._defaults_edited()

    def _pick_for_checked(self) -> None:
        """给"只剩接收固件在广播、类型认不出来"的板子单独指定固件，否则它没法续传。"""
        rows = self._checked_rows()
        if not rows:
            QMessageBox.information(self, "没选行", "先勾上要用这个固件的行。")
            return
        path, _ = QFileDialog.getOpenFileName(self, f"给 {len(rows)} 个节点指定固件",
                                              self.ed_emg.text().strip() or self.cfg.ota.firmware_dir
                                              or str(Path.cwd()), "ESP 固件 (*.bin);;所有文件 (*)")
        if not path:
            return
        for row in rows:
            row.firmware = path
        self._rebuild()

    def _defaults_edited(self) -> None:
        self.defaults = {"semg": self.ed_emg.text().strip(), "imu": self.ed_imu.text().strip()}
        if self._job is None:
            self._rebuild()

    # ── 任务执行 ──────────────────────────────────────────────────────────────

    def options(self) -> ota.OtaOptions:
        opts = ota.OtaOptions.from_config(self.cfg)
        opts.scan_timeout = self.sp_scan.value()
        opts.trigger_wait = self.sp_wait.value()
        opts.reboot_wait = self.sp_reboot.value()
        opts.ack_timeout = self.sp_ack.value()
        opts.receiver_prefix = self.ed_prefix.text().strip() or ota.RECEIVER_NAME_PREFIX
        return opts

    def _spawn(self, fn) -> None:
        if self._job is not None:
            return
        if not ota.bleak_available():
            QMessageBox.warning(self, "缺少依赖", "未安装 bleak：pip install bleak，"
                                                  "并确认本机蓝牙适配器可用。")
            return
        self._job = _JobThread(fn, self._sig)
        self._set_enabled(False)
        self.btn_cancel.setEnabled(True)
        self._job.start()

    def start_scan(self) -> None:
        opts = self.options()

        def job(rep: ota.OtaReporter) -> None:
            self._sig.log.emit(f"扫描 BLE（{opts.scan_timeout:.0f}s）…")
            self._sig.scanned.emit(ota.scan_adverts(opts.scan_timeout))

        self._spawn(job)

    def start_upgrade(self) -> None:
        rows = self._checked_rows()
        if not rows:
            QMessageBox.information(self, "没选对象", "先勾上要升级的节点，或点扫描把正在广播的节点列出来。")
            return
        problems, jobs = self._build_jobs(rows)
        if problems:
            QMessageBox.warning(self, "还不能开始", "\n".join(problems))
            return
        opts = self.options()
        text = "\n".join(f"• {row.title}  {row.address or '只认广播名'}  ←  {Path(path).name}"
                         for row, _, path in jobs)
        gateway = (
            "升级前请先关闭网关（拔掉它的 USB 就行）：\n"
            "  应用固件被网关连着时不广播，阶段 1 搜不到它，也就写不进触发字。\n"
            f"不想断电：点开始之后再把它搬远也可以，阶段 1 会持续等 {opts.trigger_wait:.0f}s，\n"
            "  网关 4s 监督超时一断开，节点恢复广播就会自动被抢下。\n"
            "进入阶段 2 后网关随时可以恢复供电 —— 它不认 nimble-ble-ota，不会来抢这条连接。\n\n"
        )
        if QMessageBox.question(self, "确认升级",
                                f"将依次升级 {len(jobs)} 个节点：\n{text}\n\n{gateway}"
                                "升级期间对应通道波形中断；新固件启动自检失败时由 bootloader 回滚到旧槽。\n\n开始？"
                                ) != QMessageBox.Yes:
            return
        self.log.clear()
        for row, _, _ in jobs:
            row.state = "排队中"
        self._rebuild()

        def job(rep: ota.OtaReporter) -> None:
            results = ota.upgrade_nodes([(row.node(), data) for row, data, _ in jobs], opts, rep)
            ok = sum(1 for _, err in results if not err)
            self._sig.log.emit(f"结束：{ok} 成功 / {len(results) - ok} 失败（原因见上面各行）")

        self._spawn(job)
        self._on_log(f"提示：阶段1 每台最多等 {opts.trigger_wait:.0f}s 让节点空闲 —— 现在去把网关搬远或断电。")

    def _build_jobs(self, rows: list[OtaRow]) -> tuple[list[str], list[tuple[OtaRow, bytes, str]]]:
        """校验并读盘。固件在这里就读进来，免得起手之后文件被 build 覆盖。"""
        problems: list[str] = []
        jobs: list[tuple[OtaRow, bytes, str]] = []
        for row in rows:
            path = self.firmware_for(row)
            if not path:
                problems.append(f"{row.title}：没有指定固件文件")
                continue
            try:
                data = ota.load_firmware(path)
            except ota.OtaError as exc:
                problems.append(f"{row.title}：{exc}")
                continue
            if not row.address and not row.kind:
                problems.append(f"{row.title}：类型和完整 BLE 地址都不知道，认不出这块板")
                continue
            for warn in ota.firmware_warnings(data):
                self._on_log(f"提示：{row.title} — {warn}")
            jobs.append((row, data, path))
        return problems, jobs

    def cancel_job(self) -> None:
        if self._job is not None:
            self._job.cancel.set()
            self._on_log("取消请求已发出，等当前这一包/扇区结束")

    @property
    def busy(self) -> bool:
        return self._job is not None

    def shutdown(self) -> None:
        """退出前收尾：取消并等工作线程把当前这一包发完。

        既不留半截 BLE 会话，也避免界面析构后线程还往已销毁的 QObject 上发信号。
        """
        if self._job is None:
            return
        self._job.cancel.set()
        self._job.join(30.0)
        self._sig.blockSignals(True)

    def _on_job_finished(self) -> None:
        self._job = None
        self._set_enabled(True)
        self.btn_cancel.setEnabled(False)
        self.lbl_current.setText("空闲")

    def _set_enabled(self, enabled: bool) -> None:
        for w in (self.btn_scan, self.btn_start, self.tbl, self.ed_emg, self.ed_imu,
                  self.btn_emg, self.btn_imu, self.btn_row_fw, self.btn_all_emg, self.btn_all_imu,
                  self.btn_none, self.ed_prefix, self.sp_scan, self.sp_wait, self.sp_reboot, self.sp_ack):
            w.setEnabled(enabled)

    # ── 回调落地 ──────────────────────────────────────────────────────────────

    def _on_log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    def _on_progress(self, key: str, done: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(done * 100 / total)
        self.bar.setValue(pct)
        row = self.rows.get(key)
        self.lbl_current.setText(f"{row.title if row else key}　{done:,}/{total:,} B")
        if row is not None and not row.state.startswith(("完成", "失败")):
            row.state = f"传输中 {pct}%"
            self._state_cell(row)

    def _on_state(self, key: str, text: str) -> None:
        row = self.rows.get(key)
        if row is None:
            self._on_log(f"状态：{key} → {text}")
            return
        row.state = text
        self._state_cell(row)

    def _state_cell(self, row: OtaRow) -> None:
        i = self._index.get(row.suffix)
        if i is None:
            return
        item = self.tbl.item(i, 6)
        if item is None:
            item = QTableWidgetItem()
            self.tbl.setItem(i, 6, item)
        item.setText(row.state)
        color = GOOD if row.state.startswith("完成") else BAD if row.state.startswith("失败") else \
            WARN if row.state != "空闲" else DIM
        item.setForeground(color)
