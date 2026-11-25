"""Per-user named geometry candidates; loading never applies a calibration."""
import json
from PySide6 import QtCore,QtWidgets
from saxs_waxs_core.geometry import geometry_to_document,geometry_from_document


def install_geometry_library(panel,layout,*,settings=None):
    settings=settings if settings is not None else QtCore.QSettings('XRelay','GeometryLibrary')
    try:entries=json.loads(settings.value('parameters','{}'))
    except (TypeError,ValueError):entries={}
    if not isinstance(entries,dict):entries={}
    widget=QtWidgets.QGroupBox('几何参数库')
    form=QtWidgets.QVBoxLayout(widget)
    first=QtWidgets.QHBoxLayout();combo=QtWidgets.QComboBox();combo.addItems(entries)
    combo.setMinimumContentsLength(8);first.addWidget(combo,1)
    load=QtWidgets.QPushButton('套用');first.addWidget(load);form.addLayout(first)
    buttons=QtWidgets.QHBoxLayout();form.addLayout(buttons)
    panel.geometry_library_combo=combo
    def status(text,*,error=False):
        panel.status_label.setText(text)
    def persist():settings.setValue('parameters',json.dumps(entries,ensure_ascii=False));settings.sync()
    def load_current():
        name=combo.currentText()
        if name not in entries:return
        try:
            geometry=geometry_from_document(entries[name])
            panel.set_geometry(geometry)
            status(f'已载入“{name}”；请核对当前探测器尺寸、中心、波长与SDD，再点击“应用几何参数”。')
        except (ValueError,TypeError,KeyError) as exc:status(f'参数载入失败：{exc}',error=True)
    def save():
        n=1
        while f'参数{n}' in entries:n+=1
        name,ok=QtWidgets.QInputDialog.getText(panel,'保存几何参数','名称',text=f'参数{n}')
        name=name.strip()
        if not ok or not name:return
        try:document=geometry_to_document(panel.geometry())
        except ValueError as exc:status(str(exc),error=True);return
        entries[name]=document;persist()
        if combo.findText(name)<0:combo.addItem(name)
        combo.setCurrentText(name)
    def update():
        name=combo.currentText()
        if name not in entries:return
        try:entries[name]=geometry_to_document(panel.geometry());persist()
        except ValueError as exc:status(str(exc),error=True)
    def rename():
        old=combo.currentText()
        if old not in entries:return
        name,ok=QtWidgets.QInputDialog.getText(panel,'重命名参数','名称',text=old)
        name=name.strip()
        if ok and name and name!=old and name not in entries:
            entries[name]=entries.pop(old);persist();combo.setItemText(combo.currentIndex(),name)
    def delete():
        name=combo.currentText()
        if name in entries:entries.pop(name);persist();combo.removeItem(combo.currentIndex())
    for text,callback in (('另存参数',save),('更新',update),('重命名',rename),('删除',delete)):
        button=QtWidgets.QPushButton(text);button.clicked.connect(callback);buttons.addWidget(button)
    load.clicked.connect(load_current)
    layout.insertWidget(1,widget)

    return widget
