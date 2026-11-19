"""风险事件页：把越限变成可处置的记录。

事件不是日志噪声：每条带因子、实测值、阈值、发生侧、课目与可执行建议，可确认、可写
备注、可删除（带撤销）、可导出。筛选 / 搜索 / 排序全部即时生效。
"""

from __future__ import annotations

import csv
import json
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..guard.model import FACTOR_META, RiskEvent
from . import theme as T
from .widgets import card

COLS = ["课次时刻", "因子", "等级", "实测值", "阈值", "侧", "课目", "状态", "备注"]


class EventsView(QWidget):
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.engine = None
        self._deleted: list[list[tuple[int, RiskEvent]]] = []
        self._visible: list[RiskEvent] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索因子 / 建议 / 备注…")
        self.ed_search.textChanged.connect(self._refill)
        bar.addWidget(self.ed_search, 2)
        self.cb_level = QComboBox()
        self.cb_level.addItems(["全部等级", "高", "中"])
        self.cb_level.currentIndexChanged.connect(self._refill)
        bar.addWidget(self.cb_level)
        self.cb_factor = QComboBox()
        self.cb_factor.addItem("全部因子")
        for fid, meta in FACTOR_META.items():
            self.cb_factor.addItem(meta[0], fid)
        self.cb_factor.currentIndexChanged.connect(self._refill)
        bar.addWidget(self.cb_factor)
        self.cb_sort = QComboBox()
        self.cb_sort.addItems(["时间 ↓", "时间 ↑", "严重度 ↓", "因子"])
        self.cb_sort.currentIndexChanged.connect(self._refill)
        bar.addWidget(self.cb_sort)
        self.chk_unack = QComboBox()
        self.chk_unack.addItems(["全部状态", "仅未确认", "仅已确认"])
        self.chk_unack.currentIndexChanged.connect(self._refill)
        bar.addWidget(self.chk_unack)
        root.addLayout(bar)

        bar2 = QHBoxLayout()
        self.btn_ack = QPushButton("确认所选")
        self.btn_ack.setObjectName("Primary")
        self.btn_ack.clicked.connect(self.ack_selected)
        self.btn_del = QPushButton("删除所选")
        self.btn_del.setObjectName("Danger")
        self.btn_del.clicked.connect(self.delete_selected)
        self.btn_undo = QPushButton("撤销删除")
        self.btn_undo.setObjectName("Ghost")
        self.btn_undo.clicked.connect(self.undo_delete)
        self.btn_csv = QPushButton("导出 CSV")
        self.btn_csv.setObjectName("Ghost")
        self.btn_csv.clicked.connect(lambda: self.export("csv"))
        self.btn_json = QPushButton("导出 JSON")
        self.btn_json.setObjectName("Ghost")
        self.btn_json.clicked.connect(lambda: self.export("json"))
        for b in (self.btn_ack, self.btn_del, self.btn_undo, self.btn_csv, self.btn_json):
            bar2.addWidget(b)
        bar2.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("Dim")
        bar2.addWidget(self.lbl_count)
        root.addLayout(bar2)

        split = QSplitter(Qt.Horizontal)
        self.tbl = QTableWidget(0, len(COLS))
        self.tbl.setHorizontalHeaderLabels(COLS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.setSelectionMode(QTableWidget.ExtendedSelection)
        self.tbl.itemSelectionChanged.connect(self._show_detail)
        split.addWidget(self.tbl)

        right = QWidget()
        right.setFixedWidth(340)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        f, l = card("事件明细", "")
        self.lbl_head = QLabel("选择一条事件")
        self.lbl_head.setObjectName("CardTitle")
        self.lbl_head.setWordWrap(True)
        l.addWidget(self.lbl_head)
        self.lbl_meta = QLabel("")
        self.lbl_meta.setObjectName("Muted")
        self.lbl_meta.setWordWrap(True)
        l.addWidget(self.lbl_meta)
        self.lbl_advice = QLabel("")
        self.lbl_advice.setWordWrap(True)
        self.lbl_advice.setStyleSheet(
            f"color:{T.C['text_warm']}; background:rgba(18,224,196,20);"
            f"border:1px solid {T.C['border_soft']}; border-radius:9px; padding:9px;")
        l.addWidget(self.lbl_advice)
        l.addWidget(QLabel("处置备注"))
        self.ed_note = QPlainTextEdit()
        self.ed_note.setPlaceholderText("记录处置：降强度 / 轮换 / 补离心…")
        self.ed_note.setMaximumHeight(110)
        l.addWidget(self.ed_note)
        row = QHBoxLayout()
        self.btn_save_note = QPushButton("保存备注")
        self.btn_save_note.clicked.connect(self.save_note)
        self.btn_ack_one = QPushButton("确认此条")
        self.btn_ack_one.setObjectName("Primary")
        self.btn_ack_one.clicked.connect(self.ack_one)
        row.addWidget(self.btn_save_note)
        row.addWidget(self.btn_ack_one)
        l.addLayout(row)
        l.addStretch(1)
        rv.addWidget(f)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setSizes([880, 340])
        root.addWidget(split, 1)
        self._current: RiskEvent | None = None

    def set_engine(self, engine) -> None:
        self.engine = engine
        self._refill()

    def _filtered(self) -> list[RiskEvent]:
        if self.engine is None:
            return []
        needle = self.ed_search.text().strip().lower()
        level = self.cb_level.currentText()
        fid = self.cb_factor.currentData()
        state = self.chk_unack.currentIndex()
        rows = []
        for e in self.engine.events:
            if level != "全部等级" and e.level != level:
                continue
            if fid and e.factor != fid:
                continue
            if state == 1 and e.acknowledged:
                continue
            if state == 2 and not e.acknowledged:
                continue
            if needle and needle not in (e.name + e.advice + e.note + e.drill).lower():
                continue
            rows.append(e)
        mode = self.cb_sort.currentIndex()
        if mode == 0:
            rows.sort(key=lambda e: -e.t)
        elif mode == 1:
            rows.sort(key=lambda e: e.t)
        elif mode == 2:
            rows.sort(key=lambda e: -e.severity)
        else:
            rows.sort(key=lambda e: (e.factor, -e.t))
        return rows

    def _refill(self, *_a) -> None:
        self._visible = self._filtered()
        self.tbl.setRowCount(len(self._visible))
        for r, e in enumerate(self._visible):
            vals = [f"{e.t:7.1f}s", e.name, e.level, f"{e.value:.1f}", f"{e.threshold:.1f}",
                    e.side, e.drill, "已确认" if e.acknowledged else "待处置", e.note or "—"]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if c in (0, 3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 2:
                    item.setForeground(QColor(T.C["danger"] if e.level == "高" else T.C["warn"]))
                if c == 7:
                    item.setForeground(QColor(T.C["safe"] if e.acknowledged else T.C["muted"]))
                self.tbl.setItem(r, c, item)
        total = len(self.engine.events) if self.engine else 0
        unack = sum(1 for e in (self.engine.events if self.engine else []) if not e.acknowledged)
        self.lbl_count.setText(f"显示 {len(self._visible)} / 共 {total} · 待处置 {unack}")
        self.btn_undo.setEnabled(bool(self._deleted))

    def refresh(self) -> None:
        self._refill()

    def _selected(self) -> list[RiskEvent]:
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()})
        return [self._visible[r] for r in rows if r < len(self._visible)]

    def _show_detail(self) -> None:
        sel = self._selected()
        e = sel[0] if sel else None
        self._current = e
        if e is None:
            self.lbl_head.setText("选择一条事件")
            self.lbl_meta.setText("")
            self.lbl_advice.setText("")
            self.ed_note.setPlainText("")
            return
        self.lbl_head.setText(f"#{e.id} {e.name} · {e.level}风险")
        self.lbl_meta.setText(
            f"课次时刻 {e.t:.1f}s\n实测 {e.value:.2f}{FACTOR_META[e.factor][2]} / "
            f"阈值 {e.threshold:.2f}\n严重度 {e.severity:.2f} · 当时总分 {e.score:.0f}\n"
            f"发生侧 {e.side} · 课目 {e.drill}\n"
            f"记录于 {time.strftime('%H:%M:%S', time.localtime(e.wall))}")
        self.lbl_advice.setText(e.advice)
        self.ed_note.setPlainText(e.note)

    def ack_selected(self) -> None:
        for e in self._selected():
            e.acknowledged = True
        self._refill()
        self.changed.emit()

    def ack_one(self) -> None:
        if self._current:
            self._current.acknowledged = True
            self._refill()
            self.changed.emit()

    def save_note(self) -> None:
        if self._current:
            self._current.note = self.ed_note.toPlainText().strip()
            self._refill()
            self.changed.emit()

    def delete_selected(self) -> None:
        sel = self._selected()
        if not sel or self.engine is None:
            return
        if QMessageBox.question(self, "删除事件", f"删除 {len(sel)} 条事件？可撤销。") != QMessageBox.Yes:
            return
        batch: list[tuple[int, RiskEvent]] = []
        for e in sel:
            if e in self.engine.events:
                idx = self.engine.events.index(e)
                batch.append((idx, e))
                self.engine.events.remove(e)
        if batch:
            self._deleted.append(batch)
        self._refill()
        self.changed.emit()

    def undo_delete(self) -> None:
        if not self._deleted or self.engine is None:
            return
        batch = self._deleted.pop()
        for idx, e in sorted(batch, key=lambda x: x[0]):
            self.engine.events.insert(min(idx, len(self.engine.events)), e)
        self.engine.events.sort(key=lambda x: x.id)
        self._refill()
        self.changed.emit()

    def export(self, kind: str) -> None:
        from PySide6.QtWidgets import QFileDialog

        rows = self._visible
        if not rows:
            QMessageBox.information(self, "无事件", "当前筛选下没有可导出的事件。")
            return
        if kind == "csv":
            path, _ = QFileDialog.getSaveFileName(self, "导出事件 CSV", "hamstring_events.csv", "CSV (*.csv)")
            if not path:
                return
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["id", "session_seconds", "factor", "level", "value", "threshold",
                            "severity", "score", "side", "drill", "acknowledged", "note", "advice"])
                for e in rows:
                    w.writerow([e.id, f"{e.t:.2f}", e.factor, e.level, f"{e.value:.3f}",
                                f"{e.threshold:.3f}", f"{e.severity:.3f}", f"{e.score:.1f}",
                                e.side, e.drill, int(e.acknowledged), e.note, e.advice])
        else:
            path, _ = QFileDialog.getSaveFileName(self, "导出事件 JSON", "hamstring_events.json", "JSON (*.json)")
            if not path:
                return
            payload = [dict(id=e.id, t=e.t, factor=e.factor, level=e.level, value=e.value,
                            threshold=e.threshold, severity=e.severity, score=e.score, side=e.side,
                            drill=e.drill, acknowledged=e.acknowledged, note=e.note, advice=e.advice)
                       for e in rows]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)