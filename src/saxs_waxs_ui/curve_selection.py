"""Searchable live visibility controls for already calculated curves."""
from PySide6 import QtCore,QtWidgets


class CurveSelectionDialog(QtWidgets.QDialog):
    def __init__(self,panel):
        super().__init__(panel)
        self.panel=panel
        self.setWindowTitle('选择预览曲线')
        self.resize(650,480)
        self.search=QtWidgets.QLineEdit()
        self.search.setPlaceholderText('搜索样品名、帧号或数据集')
        self.items=QtWidgets.QListWidget()
        self.count=QtWidgets.QLabel()
        self.all_button=QtWidgets.QPushButton('全选')
        self.none_button=QtWidgets.QPushButton('清空')
        buttons=QtWidgets.QHBoxLayout()
        buttons.addWidget(self.all_button);buttons.addWidget(self.none_button);buttons.addStretch()
        buttons.addWidget(self.count)
        close=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.close)
        layout=QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel('只控制右侧曲线显示与当前曲线导出，不改变左侧批处理勾选。'))
        layout.addWidget(self.search);layout.addLayout(buttons);layout.addWidget(self.items,1);layout.addWidget(close)
        self.search.textChanged.connect(self._filter)
        self.items.itemChanged.connect(self._changed)
        self.all_button.clicked.connect(lambda:self._set_all(True))
        self.none_button.clicked.connect(lambda:self._set_all(False))
        self.refresh()

    def refresh(self):
        blocker=QtCore.QSignalBlocker(self.items)
        self.items.clear()
        for key,(name,_curve),context in zip(self.panel._available_keys,self.panel._available_curves,self.panel._available_contexts):
            item=QtWidgets.QListWidgetItem(name)
            item.setData(QtCore.Qt.ItemDataRole.UserRole,key)
            item.setToolTip(str(context.get('source',''))+'\n'+name)
            item.setFlags(item.flags()|QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if self.panel._curve_visibility[key] else QtCore.Qt.CheckState.Unchecked)
            self.items.addItem(item)
        del blocker
        self._filter()
        self.count.setText(f'{len(self.panel._curve_items)} / {len(self.panel._available_curves)} 条')

    def _filter(self,*_):
        query=self.search.text().strip().casefold()
        for index in range(self.items.count()):
            item=self.items.item(index);item.setHidden(query not in item.text().casefold())

    def _changed(self,item):
        key=item.data(QtCore.Qt.ItemDataRole.UserRole)
        self.panel._curve_visibility[key]=item.checkState()==QtCore.Qt.CheckState.Checked
        self.panel._apply_curve_selection()
        self.count.setText(f'{len(self.panel._curve_items)} / {len(self.panel._available_curves)} 条')

    def _set_all(self,checked):
        self.panel._curve_visibility.update(dict.fromkeys(self.panel._available_keys,checked))
        self.panel._apply_curve_selection();self.refresh()
