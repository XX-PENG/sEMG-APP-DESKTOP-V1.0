"""链路健康与设备状态面板。

无校验的文本协议一定要把"我们没能发现什么"显示出来，所以这里同时列三类量：
链路速率 vs 容量、解析器异常计数、每设备 seq 连续性。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme as T

DEVICE_COLUMNS = ["通道", "MAC", "类型", "槽位", "状态", "帧", "样本", "实测速率", "最后seq",
                  "drop", "丢帧缺口", "节点重启", "重锚", "缓冲样本", "距今ms"]
BAD = T.color("danger")
WARN = T.color("warn")
GOOD = T.color("safe")


class StatusPanel(QWidget):
    def __init__(self, cfg, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg

        self.tbl = QTableWidget(0, len(DEVICE_COLUMNS))
        self.tbl.setHorizontalHeaderLabels(DEVICE_COLUMNS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)

        self.lbl_source = QLabel("未连接")
        self.lbl_rate = QLabel("—")
        self.bar = QProgressBar()
        self.bar.setRange(0, 200)
        self.bar.setFormat("%v%")
        self.bar.setMaximumHeight(18)
        self.lbl_lines = QLabel("—")
        self.lbl_frames = QLabel("—")
        link = QFormLayout()
        link.addRow("数据源", self.lbl_source)
        link.addRow("实测字节", self.lbl_rate)
        link.addRow("容量占用", self.bar)
        link.addRow("行/帧速率", self.lbl_lines)
        link.addRow("总字节", self.lbl_frames)
        box_link = QGroupBox("链路")
        box_link.setLayout(link)

        self.parse_labels: dict[str, QLabel] = {}
        keys = [
            ("lines", "解析行数"),
            ("semg_frames", "sEMG 帧"),
            ("imu_frames", "IMU 帧"),
            ("imu_sample_lines", "IMU 数据行"),
            ("imu_block_truncated", "IMU 块截断"),
            ("imu_out_of_range", "IMU 越界"),
            ("semg_short", "sEMG 短帧"),
            ("semg_overflow_cnt", "sEMG 样本数>cnt"),
            ("semg_saturated", "int16 削顶"),
            ("bad_token_lines", "坏 token 行"),
            ("garbage_lines", "未识别行"),
            ("oversize_lines", "超长行"),
        ]
        grid = QFormLayout()
        for attr, title in keys:
            lab = QLabel("0")
            self.parse_labels[attr] = lab
            grid.addRow(title, lab)
        box_parse = QGroupBox("解析计数")
        box_parse.setLayout(grid)

        self.lbl_advice = QLabel("—")
        self.lbl_advice.setWordWrap(True)
        box_adv = QGroupBox("判读提示")
        v = QVBoxLayout(box_adv)
        v.addWidget(self.lbl_advice)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        self.log.setStyleSheet("font-family: Consolas, Menlo, monospace;")
        box_log = QGroupBox("网关状态行")
        vl = QVBoxLayout(box_log)
        vl.addWidget(self.log)

        left = QWidget()
        lf = QVBoxLayout(left)
        lf.setContentsMargins(0, 0, 0, 0)
        lf.addWidget(box_link)
        lf.addWidget(box_parse)
        lf.addWidget(box_adv, 1)

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.tbl)
        right.addWidget(box_log)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(split)
        self._seen_status: set[str] = set()

    def update_data(self, st, rows: list[dict], status_log: list[dict]) -> None:
        self.lbl_source.setText(st.source or "未连接")
        cap = st.capacity_bytes_per_second
        self.lbl_rate.setText(f"{st.bytes_per_second:,.0f} B/s" + (f" / 容量 {cap:,.0f} B/s" if cap else ""))
        self.bar.setValue(int(min(200, st.utilization * 100)))
        self.bar.setFormat(f"{st.utilization * 100:.0f}%")
        self.bar.setStyleSheet(
            "QProgressBar::chunk{background:%s}" % (T.C["danger"] if st.utilization > 0.85 else
                                                   T.C["warn"] if st.utilization > 0.6 else T.C["safe"])
        )
        self.lbl_lines.setText(f"{st.lines_per_second:,.0f} 行/s，{st.frames_per_second:,.0f} 帧/s")
        self.lbl_frames.setText(f"{st.bytes_total:,} B，回放进度 " + (
            f"{st.replay_progress * 100:.1f}%" if st.replay_progress is not None else "—"))

        for attr, lab in self.parse_labels.items():
            value = getattr(st.parse, attr, 0)
            lab.setText(f"{value:,}")
            bad = attr in ("imu_block_truncated", "imu_out_of_range", "semg_short", "semg_overflow_cnt",
                           "bad_token_lines", "garbage_lines", "oversize_lines")
            lab.setStyleSheet("color:%s" % (T.C["danger"] if bad and value else T.C["text"]))

        self._fill_table(rows)
        self.lbl_advice.setText(self._advice(st, rows))
        self._append_status(status_log)
        if st.error:
            self.log.appendPlainText(f"[采集停止] {st.error}")

    def _fill_table(self, rows: list[dict]) -> None:
        self.tbl.setRowCount(len(rows))
        for r_i, row in enumerate(rows):
            if row["kind"] == "semg":
                name = f"CH{row['index'] + 1}" if row["index"] is not None else "未映射"
            else:
                name = "IMU"
            age = row["age"]
            values = [
                name,
                row["mac"],
                row["kind"],
                str((row["slot"] - 1) if row["slot"] is not None else "—"),
                "在线" if row["online"] else "离线",
                f"{row['frames']:,}",
                f"{row['samples']:,}",
                f"{row['rate']:,.0f}/s",
                str(row["last_seq"] if row["last_seq"] is not None else "—"),
                f"{row['drop_total']:,}",
                f"{row['seq_gaps']:,}",
                f"{row['restarts']}",
                f"{row['resyncs']}",
                f"{row['buffered']:,}",
                f"{age * 1000:.0f}" if age is not None else "—",
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 4:
                    item.setForeground(GOOD if row["online"] else BAD)
                if c in (9, 10, 11, 12) and (row["seq_gaps"] + row["restarts"] + row["drop_total"]
                                             + row["resyncs"]) > 0:
                    item.setForeground(WARN if row["online"] else BAD)
                self.tbl.setItem(r_i, c, item)

    def _advice(self, st, rows: list[dict]) -> str:
        notes: list[str] = []
        if st.utilization > 0.85 and st.capacity_bytes_per_second:
            notes.append(
                "文本流量已逼近链路容量：网关打印任务跟不上时会丢帧并打印 print queue overflow。"
                "可降低打印密度或改二进制协议。"
            )
        if st.parse.imu_block_truncated:
            notes.append(f"IMU 块截断 {st.parse.imu_block_truncated} 次：数据行数少于 cnt，"
                         "要么发送端整帧样本没到齐，要么串口丢了行。")
        if st.parse.garbage_lines or st.parse.oversize_lines:
            notes.append(f"未识别行 {st.parse.garbage_lines}、超长行 {st.parse.oversize_lines}："
                         "多为字节丢失导致的断行，解析器已按行重同步。")
        if st.parse.semg_saturated:
            notes.append(f"int16 削顶 {st.parse.semg_saturated} 个样本：模拟前端或缩放已饱和。")
        lost = sum(r["lost_frames"] for r in rows)
        if lost:
            notes.append(f"seq 缺口累计 {lost} 帧（发送端 drop 累计 "
                         f"{sum(r['drop_total'] for r in rows)}）：缺口在时间轴上按原时长留空。")
        restarts = sum(r["restarts"] for r in rows)
        if restarts:
            notes.append(f"节点重启 {restarts} 次：seq 大幅跳变在 uint16 回绕下会被当成上千帧缺口，"
                         "这里按重启计、不计入丢帧。要查的是掉电、看门狗还是 BLE 断连后重新初始化。")
        if not rows:
            notes.append("还没解析到任何设备帧。确认网关已连上传感器、串口参数与设备节点选择正确。")
        return "\n".join(f"• {n}" for n in notes) if notes else "• 未发现问题计数"

    def _append_status(self, status_log: list[dict]) -> None:
        for item in status_log:
            tag = f"{item['t']:.3f}:{item['text']}"
            if tag in self._seen_status:
                continue
            self._seen_status.add(tag)
            self.log.appendPlainText(item["text"])
            if len(self._seen_status) > 2000:
                self._seen_status.clear()
