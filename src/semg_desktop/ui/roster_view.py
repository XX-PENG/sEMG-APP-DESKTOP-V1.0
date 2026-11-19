"""运动员档案页：名单 CRUD + MVC 基线 + 伤史。

档案是判读参数的一部分，所以这里不只是通讯录：MVC 基线决定激活百分比的分母，伤史决定
重点侧，慢性负荷决定急性负荷比的分母。MVC 可以从实时采集当场标定。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..guard.model import MUSCLES
from ..guard.roster import POSITIONS, AthleteProfile, Injury, Roster
from . import theme as T
from .widgets import card

COLS = ["号码", "姓名", "位置", "年龄", "身高/体重", "优势腿", "伤史", "复出周", "慢性负荷 AU", "MVC 基线"]


class AthleteDialog(QDialog):
    def __init__(self, profile: AthleteProfile, parent=None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle(f"编辑档案 · {profile.name}")
        self.setMinimumWidth(620)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_name = QLineEdit(profile.name)
        self.sp_number = QSpinBox()
        self.sp_number.setRange(1, 99)
        self.sp_number.setValue(profile.number)
        self.cb_pos = QComboBox()
        self.cb_pos.addItems(POSITIONS)
        self.cb_pos.setCurrentText(profile.position)
        self.sp_age = QSpinBox()
        self.sp_age.setRange(12, 60)
        self.sp_age.setValue(profile.age)
        self.sp_height = QSpinBox()
        self.sp_height.setRange(120, 220)
        self.sp_height.setValue(profile.height_cm)
        self.sp_mass = QDoubleSpinBox()
        self.sp_mass.setRange(35, 140)
        self.sp_mass.setValue(profile.mass_kg)
        self.cb_leg = QComboBox()
        self.cb_leg.addItems(["左 L", "右 R"])
        self.cb_leg.setCurrentIndex(0 if profile.dominant_leg == "L" else 1)
        self.sp_chronic = QDoubleSpinBox()
        self.sp_chronic.setRange(20, 1200)
        self.sp_chronic.setValue(profile.chronic_load)
        self.sp_return = QSpinBox()
        self.sp_return.setRange(0, 104)
        self.sp_return.setSpecialValueText("无")
        self.sp_return.setValue(profile.return_week or 0)
        form.addRow("姓名", self.ed_name)
        row = QHBoxLayout()
        row.addWidget(self.sp_number)
        row.addWidget(self.cb_pos)
        row.addWidget(self.sp_age)
        form.addRow("号码 / 位置 / 年龄", row)
        row2 = QHBoxLayout()
        row2.addWidget(self.sp_height)
        row2.addWidget(self.sp_mass)
        row2.addWidget(self.cb_leg)
        form.addRow("身高 cm / 体重 kg / 优势腿", row2)
        form.addRow("近 7 日慢性负荷 AU", self.sp_chronic)
        form.addRow("伤后复出周", self.sp_return)
        root.addLayout(form)

        root.addWidget(QLabel("MVC 基线（包络定点值，决定 %MVC 分母）"))
        self.mvc_spins: dict[str, QDoubleSpinBox] = {}
        grid = QHBoxLayout()
        cols = (QVBoxLayout(), QVBoxLayout())
        for i, m in enumerate(MUSCLES):
            sp = QDoubleSpinBox()
            sp.setRange(200, 20000)
            sp.setValue(profile.mvc.get(m.id, 2600.0))
            self.mvc_spins[m.id] = sp
            lab = QLabel(m.label)
            lab.setStyleSheet(f"color:{m.color};")
            lab.setFixedWidth(52)
            r = QHBoxLayout()
            r.addWidget(lab)
            r.addWidget(sp)
            cols[0 if i < 4 else 1].addLayout(r)
        for c in cols:
            grid.addLayout(c)
        root.addLayout(grid)

        root.addWidget(QLabel("伤史"))
        self.tbl_inj = QTableWidget(0, 5)
        self.tbl_inj.setHorizontalHeaderLabels(["日期", "肌肉", "侧", "分级", "停训周"])
        self.tbl_inj.verticalHeader().setVisible(False)
        self.tbl_inj.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_inj.setMaximumHeight(120)
        for inj in profile.injuries:
            self._add_inj_row(inj)
        root.addWidget(self.tbl_inj)
        rowi = QHBoxLayout()
        b_add = QPushButton("加一行")
        b_add.setObjectName("Ghost")
        b_add.clicked.connect(lambda: self._add_inj_row(Injury("2026-01-01", "股二头肌长头", "R", "I 级", 2)))
        b_del = QPushButton("删选中行")
        b_del.setObjectName("Ghost")
        b_del.clicked.connect(self._del_inj_row)
        rowi.addWidget(b_add)
        rowi.addWidget(b_del)
        rowi.addStretch(1)
        root.addLayout(rowi)

        self.ed_note = QLineEdit(profile.note)
        self.ed_note.setPlaceholderText("备注：复出限制、训练重点…")
        root.addWidget(self.ed_note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _add_inj_row(self, inj: Injury) -> None:
        r = self.tbl_inj.rowCount()
        self.tbl_inj.insertRow(r)
        for c, text in enumerate([inj.date, inj.muscle, inj.side, inj.grade, str(inj.weeks_out)]):
            self.tbl_inj.setItem(r, c, QTableWidgetItem(text))

    def _del_inj_row(self) -> None:
        rows = sorted({i.row() for i in self.tbl_inj.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tbl_inj.removeRow(r)

    def result_profile(self) -> AthleteProfile:
        p = self.profile
        p.name = self.ed_name.text().strip() or p.name
        p.number = self.sp_number.value()
        p.position = self.cb_pos.currentText()
        p.age = self.sp_age.value()
        p.height_cm = self.sp_height.value()
        p.mass_kg = self.sp_mass.value()
        p.dominant_leg = "L" if self.cb_leg.currentIndex() == 0 else "R"
        p.chronic_load = self.sp_chronic.value()
        p.return_week = self.sp_return.value() or None
        p.mvc = {mid: sp.value() for mid, sp in self.mvc_spins.items()}
        p.note = self.ed_note.text().strip()
        injuries = []
        for r in range(self.tbl_inj.rowCount()):
            cells = [self.tbl_inj.item(r, c).text() if self.tbl_inj.item(r, c) else "" for c in range(5)]
            if not cells[0]:
                continue
            try:
                weeks = int(cells[4])
            except ValueError:
                weeks = 0
            injuries.append(Injury(cells[0], cells[1], cells[2] or "R", cells[3], weeks))
        p.injuries = injuries
        return p


class RosterView(QWidget):
    selection_changed = Signal(str)
    roster_changed = Signal()
    calibrate_requested = Signal()

    def __init__(self, roster: Roster, parent=None) -> None:
        super().__init__(parent)
        self.roster = roster
        self.current_id = roster.athletes[0].id if roster.athletes else ""
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        bar = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索姓名 / 号码 / 备注…")
        self.ed_search.textChanged.connect(self._refill)
        bar.addWidget(self.ed_search, 2)
        self.cb_pos = QComboBox()
        self.cb_pos.addItem("全部位置")
        self.cb_pos.addItems(POSITIONS)
        self.cb_pos.currentIndexChanged.connect(self._refill)
        bar.addWidget(self.cb_pos)
        self.cb_sort = QComboBox()
        self.cb_sort.addItems(["号码", "姓名", "负荷", "伤史", "年龄"])
        self.cb_sort.currentIndexChanged.connect(self._refill)
        bar.addWidget(self.cb_sort)
        root.addLayout(bar)

        bar2 = QHBoxLayout()
        self.btn_new = QPushButton("新建档案")
        self.btn_new.setObjectName("Primary")
        self.btn_new.clicked.connect(self.new_athlete)
        self.btn_edit = QPushButton("编辑")
        self.btn_edit.clicked.connect(self.edit_selected)
        self.btn_del = QPushButton("删除")
        self.btn_del.setObjectName("Danger")
        self.btn_del.clicked.connect(self.delete_selected)
        self.btn_undo = QPushButton("撤销删除")
        self.btn_undo.setObjectName("Ghost")
        self.btn_undo.clicked.connect(self.undo_delete)
        self.btn_set = QPushButton("设为主监护")
        self.btn_set.setObjectName("Ghost")
        self.btn_set.clicked.connect(self.set_current)
        self.btn_cal = QPushButton("标定 MVC")
        self.btn_cal.setObjectName("Ghost")
        self.btn_cal.setToolTip("用最近 3 s 的包络峰值更新当前运动员的 MVC 基线")
        self.btn_cal.clicked.connect(self.calibrate_requested)
        for b in (self.btn_new, self.btn_edit, self.btn_del, self.btn_undo, self.btn_set, self.btn_cal):
            bar2.addWidget(b)
        bar2.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("Dim")
        bar2.addWidget(self.lbl_count)
        root.addLayout(bar2)

        f, l = card("队内名单", "双击编辑 · 选中后设为主监护")
        self.tbl = QTableWidget(0, len(COLS))
        self.tbl.setHorizontalHeaderLabels(COLS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl.doubleClicked.connect(self.edit_selected)
        l.addWidget(self.tbl, 1)
        self.lbl_detail = QLabel("")
        self.lbl_detail.setObjectName("Muted")
        self.lbl_detail.setWordWrap(True)
        l.addWidget(self.lbl_detail)
        root.addWidget(f, 1)
        self._refill()

    def _rows(self) -> list[AthleteProfile]:
        pos = self.cb_pos.currentText()
        return self.roster.query(self.ed_search.text(), "全部" if pos == "全部位置" else pos,
                                 self.cb_sort.currentText())

    def _refill(self, *_a) -> None:
        rows = self._rows()
        self.tbl.setRowCount(len(rows))
        for r, a in enumerate(rows):
            mvc_mean = sum(a.mvc.values()) / max(len(a.mvc), 1)
            vals = [str(a.number), a.name, a.position, str(a.age),
                    f"{a.height_cm}/{a.mass_kg:.0f}", "左" if a.dominant_leg == "L" else "右",
                    a.injury_summary, str(a.return_week) if a.return_week else "—",
                    f"{a.chronic_load:.0f}", f"{mvc_mean:.0f}"]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if c in (0, 3, 7, 8, 9):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 1 and a.id == self.current_id:
                    item.setForeground(QColor(T.C["turf"]))
                if c == 6 and a.injuries:
                    item.setForeground(QColor(T.C["warn"]))
                self.tbl.setItem(r, c, item)
        self.lbl_count.setText(f"{len(rows)} / {len(self.roster.athletes)} 人")
        self.btn_undo.setEnabled(self.roster.can_undo)
        self._show_detail()

    def _selected(self) -> AthleteProfile | None:
        rows = self.tbl.selectedIndexes()
        if not rows:
            return None
        r = rows[0].row()
        visible = self._rows()
        return visible[r] if r < len(visible) else None

    def _show_detail(self) -> None:
        a = self._selected() or self.roster.get(self.current_id)
        if a is None:
            self.lbl_detail.setText("")
            return
        self.lbl_detail.setText(
            f"{a.name} #{a.number} · BMI {a.bmi:.1f} · {a.injury_summary}"
            + (f" · 复出第 {a.return_week} 周" if a.return_week else "")
            + f"\n{a.note}")

    def new_athlete(self) -> None:
        dlg = AthleteDialog(self.roster.new_blank(), self)
        if dlg.exec() == QDialog.Accepted:
            p = self.roster.add(dlg.result_profile())
            self.current_id = p.id
            self._refill()
            self.selection_changed.emit(self.current_id)
            self.roster_changed.emit()

    def edit_selected(self) -> None:
        a = self._selected()
        if a is None:
            return
        dlg = AthleteDialog(a, self)
        if dlg.exec() == QDialog.Accepted:
            self.roster.update(dlg.result_profile())
            self._refill()
            self.roster_changed.emit()

    def delete_selected(self) -> None:
        a = self._selected()
        if a is None:
            return
        if QMessageBox.question(self, "删除档案", f"删除 {a.name} 的档案？可撤销。") != QMessageBox.Yes:
            return
        self.roster.delete(a.id)
        if self.current_id == a.id:
            self.current_id = self.roster.athletes[0].id if self.roster.athletes else ""
            self.selection_changed.emit(self.current_id)
        self._refill()
        self.roster_changed.emit()

    def undo_delete(self) -> None:
        a = self.roster.undo_delete()
        if a:
            self._refill()
            self.roster_changed.emit()

    def set_current(self) -> None:
        a = self._selected()
        if a:
            self.current_id = a.id
            self._refill()
            self.selection_changed.emit(a.id)

    def calibrate_mvc(self, mvc: dict[str, float]) -> None:
        a = self.roster.get(self.current_id)
        if a is None or not mvc:
            return
        a.mvc.update(mvc)
        self.roster.update(a)
        self._refill()
        self.roster_changed.emit()