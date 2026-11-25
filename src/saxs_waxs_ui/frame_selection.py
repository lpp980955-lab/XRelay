"""Compact batch-frame selection with a virtual, checkable frame list."""
from PySide6 import QtCore, QtWidgets


class FrameListModel(QtCore.QAbstractListModel):
    def __init__(self, count, checked, parent=None):
        super().__init__(parent)
        self.count = count
        self.checked = set(checked)

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else self.count

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return str(index.row() + 1)
        if role == QtCore.Qt.ItemDataRole.CheckStateRole:
            return (QtCore.Qt.CheckState.Checked if index.row() in self.checked
                    else QtCore.Qt.CheckState.Unchecked)

    def flags(self, index):
        return super().flags(index) | QtCore.Qt.ItemFlag.ItemIsUserCheckable

    def setData(self, index, value, role=QtCore.Qt.ItemDataRole.EditRole):
        if role != QtCore.Qt.ItemDataRole.CheckStateRole or not index.isValid():
            return False
        if value == QtCore.Qt.CheckState.Checked.value:
            self.checked.add(index.row())
        else:
            self.checked.discard(index.row())
        self.dataChanged.emit(index, index, [role])
        return True

    def set_checked(self, rows):
        self.checked = set(rows)
        self.dataChanged.emit(self.index(0, 0), self.index(self.count-1, 0),
                              [QtCore.Qt.ItemDataRole.CheckStateRole])


class FrameSelectionDialog(QtWidgets.QDialog):
    def __init__(self, count, selected=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("批次帧")
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["全部帧", "区间与步长", "勾选帧"])
        self.first = QtWidgets.QSpinBox()
        self.last = QtWidgets.QSpinBox()
        self.step = QtWidgets.QSpinBox()
        for field in (self.first, self.last, self.step):
            field.setRange(1, count)
        self.last.setValue(count)
        self.model = FrameListModel(count, range(count) if selected is None else selected, self)
        self.frames = QtWidgets.QListView()
        self.frames.setModel(self.model)
        self.frames.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.frames.setMinimumHeight(160)
        self.selection_actions = QtWidgets.QWidget()
        actions = QtWidgets.QHBoxLayout(self.selection_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        select_all = QtWidgets.QPushButton("全部勾选")
        check_selected = QtWidgets.QPushButton("勾选所选")
        clear = QtWidgets.QPushButton("清空勾选")
        select_all.clicked.connect(lambda: self.model.set_checked(range(count)))
        check_selected.clicked.connect(self._check_highlighted)
        clear.clicked.connect(lambda: self.model.set_checked(()))
        for button in (select_all, check_selected, clear):
            actions.addWidget(button)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        note = QtWidgets.QLabel(f"共 {count} 帧。编号从 1 开始，按数据集及帧索引排序。")
        note.setWordWrap(True)
        form = QtWidgets.QFormLayout()
        form.addRow("选择方式", self.mode)
        form.addRow("起始帧", self.first)
        form.addRow("结束帧", self.last)
        form.addRow("步长", self.step)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(self.selection_actions)
        layout.addWidget(self.frames)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.mode.setCurrentIndex(0 if selected is None else 2)
        self._mode_changed()
        self.resize(360, 360)

    def _mode_changed(self):
        ranged = self.mode.currentIndex() == 1
        for field in (self.first, self.last, self.step):
            field.setEnabled(ranged)
        self.frames.setVisible(self.mode.currentIndex() == 2)
        self.selection_actions.setVisible(self.mode.currentIndex() == 2)

    def _check_highlighted(self):
        selected = {index.row() for index in self.frames.selectedIndexes()}
        self.model.set_checked(self.model.checked | selected)

    def selected_frames(self):
        mode = self.mode.currentIndex()
        if mode == 0:
            return None
        if mode == 1:
            return list(range(self.first.value()-1, self.last.value(), self.step.value()))
        return sorted(self.model.checked)

    def _accept(self):
        selected = self.selected_frames()
        if selected is not None and not selected:
            self.status.setText("请至少选择一帧，并检查区间顺序。")
            return
        self.accept()
