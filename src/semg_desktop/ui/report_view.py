"""会话报告页：一次课次的结论与可导出存档。

停止采集后报告仍然完整可读：汇总量来自课次历史而不是最后一帧，所以"这次比上次好在哪"
可以跨课次比较。导出 JSON 供长期存档、CSV 供表格分析、文本供贴进训练日志。
"""

from __future__ import annotations

import csv
import json
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..guard.model import MUSCLES, GuardSnapshot
from ..guard.session import SessionSummary
from . import theme as T
from .widgets import BilateralBars, StatTile, card

MUS_COLS = ["肌肉", "平均激活 %MVC", "峰值 %MVC", "MDF 起", "MDF 止", "斜率 %/min"]


class ReportView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.summary: SessionSummary | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        f_head, l_head = card("课次报告", "")
        self.lbl_title = QLabel("尚未结束课次")
        self.lbl_title.setObjectName("CardTitle")
        self.lbl_title.setStyleSheet(f"font-size:19px; color:{T.C['text_warm']}; background:transparent;")
        l_head.addWidget(self.lbl_title)
        self.lbl_sub = QLabel("开始一次课次并停止后，这里生成完整报告。")
        self.lbl_sub.setObjectName("Muted")
        self.lbl_sub.setWordWrap(True)
        l_head.addWidget(self.lbl_sub)
        self.lbl_verdict = QLabel("")
        self.lbl_verdict.setWordWrap(True)
        self.lbl_verdict.setStyleSheet(
            f"color:{T.C['text_warm']}; background:rgba(18,224,196,22);"
            f"border:1px solid {T.C['border_soft']}; border-radius:10px; padding:10px;")
        l_head.addWidget(self.lbl_verdict)
        root.addWidget(f_head)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.t_stride = StatTile("总步数", "步", "blue")
        self.t_bout = StatTile("冲刺段", "段", "turf")
        self.t_hs = StatTile("高速跑", "s", "blue")
        self.t_load = StatTile("课次负荷", "AU", "turf")
        self.t_acwr = StatTile("急性负荷比", "", "lime")
        self.t_peak = StatTile("峰值风险", "/100", "danger")
        for t in (self.t_stride, self.t_bout, self.t_hs, self.t_load, self.t_acwr, self.t_peak):
            tiles.addWidget(t)
        root.addLayout(tiles)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(12)
        f_mus, l_mus = card("逐肌肉汇总", "整次课次均值 / 峰值")
        self.tbl = QTableWidget(0, len(MUS_COLS))
        self.tbl.setHorizontalHeaderLabels(MUS_COLS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        l_mus.addWidget(self.tbl)
        lv.addWidget(f_mus, 3)
        f_asym, l_asym = card("课次平均左右对比", "")
        self.bars = BilateralBars()
        l_asym.addWidget(self.bars)
        lv.addWidget(f_asym, 2)
        split.addWidget(left)

        right = QWidget()
        right.setFixedWidth(360)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(12)
        f_ev, l_ev = card("事件与处置", "")
        self.lbl_ev = QLabel("")
        self.lbl_ev.setObjectName("Muted")
        self.lbl_ev.setWordWrap(True)
        l_ev.addWidget(self.lbl_ev)
        rv.addWidget(f_ev)
        f_rec, l_rec = card("建议", "")
        self.lbl_rec = QLabel("")
        self.lbl_rec.setObjectName("Muted")
        self.lbl_rec.setWordWrap(True)
        l_rec.addWidget(self.lbl_rec)
        rv.addWidget(f_rec, 1)
        f_note, l_note = card("教练备注", "")
        self.ed_note = QPlainTextEdit()
        self.ed_note.setMaximumHeight(90)
        self.ed_note.setPlaceholderText("补充本次课次的背景与处置…")
        l_note.addWidget(self.ed_note)
        row = QHBoxLayout()
        self.btn_json = QPushButton("导出 JSON")
        self.btn_json.setObjectName("Primary")
        self.btn_json.clicked.connect(lambda: self.export("json"))
        self.btn_csv = QPushButton("CSV")
        self.btn_csv.setObjectName("Ghost")
        self.btn_csv.clicked.connect(lambda: self.export("csv"))
        self.btn_txt = QPushButton("文本")
        self.btn_txt.setObjectName("Ghost")
        self.btn_txt.clicked.connect(lambda: self.export("txt"))
        for b in (self.btn_json, self.btn_csv, self.btn_txt):
            row.addWidget(b)
        l_note.addLayout(row)
        rv.addWidget(f_note)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setSizes([880, 360])
        root.addWidget(split, 1)

    def set_summary(self, s: SessionSummary | None) -> None:
        self.summary = s
        if s is None:
            return
        self.lbl_title.setText(f"{s.athlete_name} #{s.number} · {s.position} · {s.drill}")
        self.lbl_sub.setText(
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(s.started_wall))} · "
            f"时长 {s.duration_seconds / 60:.1f} min · 数据源 {s.source}")
        self.lbl_verdict.setText(s.verdict)
        self.t_stride.set_value(f"{s.strides}", "")
        self.t_bout.set_value(f"{s.sprint_bouts}", "")
        self.t_hs.set_value(f"{s.high_speed_seconds:.0f}", "")
        self.t_load.set_value(f"{s.load_au:.0f}", f"慢性 {s.chronic_load:.0f}")
        self.t_acwr.set_value(f"{s.acwr:.2f}", "")
        self.t_peak.set_value(f"{s.peak_score:.0f}", f"结束 {s.final_score:.0f}",
                              T.risk_color(s.peak_score))
        self.tbl.setRowCount(len(s.muscles))
        for r, ms in enumerate(s.muscles):
            m = next(x for x in MUSCLES if x.id == ms.id)
            vals = [ms.label, f"{ms.mean_act:.1f}", f"{ms.peak_act:.1f}",
                    f"{ms.mdf_start:.0f}" if ms.mdf_start else "—",
                    f"{ms.mdf_end:.0f}" if ms.mdf_end else "—", f"{ms.mdf_slope:+.1f}"]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 0:
                    item.setForeground(QColor(m.color))
                self.tbl.setItem(r, c, item)
        snap = GuardSnapshot()
        for ms in s.muscles:
            from ..guard.model import MuscleReading
            snap.muscles[ms.id] = MuscleReading(id=ms.id, activation=ms.mean_act, online=True)
        snap.ham_l = (snap.muscles["bf_l"].activation + snap.muscles["st_l"].activation) / 2
        snap.ham_r = (snap.muscles["bf_r"].activation + snap.muscles["st_r"].activation) / 2
        peak = max(snap.ham_l, snap.ham_r)
        snap.asym = abs(snap.ham_l - snap.ham_r) / peak * 100 if peak > 1 else 0
        self.bars.set_snapshot(snap)
        self.lbl_ev.setText(
            f"高危 {s.events_high} 条 · 中危 {s.events_mid} 条 · 已确认 {s.events_ack} 条\n"
            f"平均不对称 {s.mean_asym:.1f}% · 峰值 {s.max_asym:.1f}%\n"
            f"最低 H:Q {s.min_hq:.2f} · 峰值疲劳 {s.max_fatigue:.0f}")
        self.lbl_rec.setText(self._recommend(s))
        self.ed_note.setPlainText(s.note)

    def _recommend(self, s: SessionSummary) -> str:
        rec = []
        if s.max_asym >= 15:
            rec.append(f"• 左右不对称峰值 {s.max_asym:.0f}%：每周 2 次单侧 Nordic / 单腿硬拉，优先弱侧。")
        if s.min_hq and s.min_hq < 0.6:
            rec.append(f"• H:Q 最低 {s.min_hq:.2f}：加强腘绳肌离心力量，跑动前做激活唤醒。")
        if s.max_fatigue >= 60:
            rec.append("• 疲劳指数偏高：缩短高强度段落、延长组间休息，复测频谱后再上量。")
        if s.acwr > 1.3:
            rec.append(f"• 急性负荷比 {s.acwr:.2f}：下一次课次负荷下调 20–30%。")
        if not rec:
            rec.append("• 各项指标在安全区：维持当前负荷渐进（每周 +10% 以内）。")
        rec.append("• 冲刺课目前做 3 组渐进加速跑，建立末端摆动预激活。")
        return "\n".join(rec)

    def export(self, kind: str) -> None:
        if self.summary is None:
            QMessageBox.information(self, "无报告", "先结束一次课次再导出。")
            return
        self.summary.note = self.ed_note.toPlainText().strip()
        s = self.summary
        base = f"report_{s.athlete_id}_{time.strftime('%Y%m%d_%H%M', time.localtime(s.started_wall))}"
        if kind == "json":
            path, _ = QFileDialog.getSaveFileName(self, "导出报告", base + ".json", "JSON (*.json)")
            if path:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(s.to_dict(), fh, indent=2, ensure_ascii=False)
        elif kind == "csv":
            path, _ = QFileDialog.getSaveFileName(self, "导出报告", base + ".csv", "CSV (*.csv)")
            if path:
                with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                    w = csv.writer(fh)
                    w.writerow(["muscle", "mean_activation_pctMVC", "peak_activation_pctMVC",
                                "mdf_start_hz", "mdf_end_hz", "mdf_slope_pct_per_min"])
                    for ms in s.muscles:
                        w.writerow([ms.id, f"{ms.mean_act:.2f}", f"{ms.peak_act:.2f}",
                                    f"{ms.mdf_start:.1f}", f"{ms.mdf_end:.1f}", f"{ms.mdf_slope:.2f}"])
        else:
            path, _ = QFileDialog.getSaveFileName(self, "导出报告", base + ".txt", "文本 (*.txt)")
            if path:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self.as_text())

    def as_text(self) -> str:
        s = self.summary
        if s is None:
            return ""
        lines = [
            f"腘绳肌损伤预防课次报告 · {s.athlete_name} #{s.number} {s.position}",
            f"课目 {s.drill} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(s.started_wall))}"
            f" · 时长 {s.duration_seconds / 60:.1f} min",
            f"结论：{s.verdict}",
            f"步数 {s.strides} · 冲刺段 {s.sprint_bouts} · 高速 {s.high_speed_seconds:.0f}s"
            f" · 负荷 {s.load_au:.0f}AU · ACWR {s.acwr:.2f}",
            f"峰值风险 {s.peak_score:.0f} · 平均不对称 {s.mean_asym:.1f}% · 最低H:Q {s.min_hq:.2f}"
            f" · 峰值疲劳 {s.max_fatigue:.0f}",
            f"事件 高{s.events_high}/中{s.events_mid} · 已确认 {s.events_ack}",
            "",
        ]
        for ms in s.muscles:
            lines.append(f"{ms.label}: 平均 {ms.mean_act:.1f}%MVC 峰值 {ms.peak_act:.1f}%MVC "
                         f"MDF {ms.mdf_start:.0f}→{ms.mdf_end:.0f}Hz 斜率 {ms.mdf_slope:+.1f}%/min")
        lines += ["", self._recommend(s)]
        if s.note:
            lines += ["", f"教练备注：{s.note}"]
        return "\n".join(lines)