"""Small numerical-data export dialog and cancellable background worker."""
import threading
from PySide6 import QtCore,QtWidgets
from saxs_waxs_core.processed_data import export_processed_batch


class ProcessedDataExportDialog(QtWidgets.QDialog):
    def __init__(self,parent=None,*,has_geometry=False,subtract_background=True):
        super().__init__(parent)
        self.setWindowTitle('导出处理后二维数据')
        self.scope_combo=QtWidgets.QComboBox()
        self.scope_combo.addItem('当前样品','current')
        self.scope_combo.addItem('左侧勾选样品','checked')
        self.format_combo=QtWidgets.QComboBox()
        self.format_combo.addItem('原格式优先，必要时无损TIFF（记录转换）','auto')
        self.format_combo.addItem('严格原格式，不能可靠保存时报错','source')
        self.format_combo.addItem('统一无损TIFF','tiff')
        self.format_combo.addItem('统一NeXus（含参数与MASK）','nexus')
        self.background_check=QtWidgets.QCheckBox('扣背景（使用背景页设置）')
        self.background_check.setChecked(subtract_background)
        self.mask_check=QtWidgets.QCheckBox('应用当前MASK及手动区域')
        self.mask_check.setChecked(True)
        self.geometry_check=QtWidgets.QCheckBox('附带当前几何（保留人工修正）')
        self.geometry_check.setChecked(has_geometry)
        self.geometry_check.setEnabled(has_geometry)
        note=QtWidgets.QLabel('导出可继续计算的二维数值数据。\n'
            'TIFF保留64位浮点；d*TREK IMG为32位浮点，并记录存储误差。\n'
            'GFRM/压缩CBF等不能可靠保存浮点MASK时，明确转换为TIFF。\n'
            '当前导出预览帧；勾选批量时使用批次帧选择（默认全部帧）。\n'
            'MASK像素写为NaN，有限负值保留。几何未知时明确记录未知。\n'
            '本软件重开自动恢复；其他软件需支持浮点、NaN及对应几何头。\n'
            '整批仅附一份处理说明，无逐文件JSON。')
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,QtWidgets.QSizePolicy.Policy.Minimum)
        self.note=note
        form=QtWidgets.QFormLayout()
        form.addRow('范围',self.scope_combo)
        form.addRow('文件格式',self.format_combo)
        layout=QtWidgets.QVBoxLayout(self)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        layout.addLayout(form)
        for item in (self.background_check,self.mask_check,self.geometry_check,note):
            layout.addWidget(item)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText('选择保存位置…')
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(520,self.sizeHint().height())


class ProcessedDataWorker(QtCore.QObject):
    progress=QtCore.Signal(object)
    finished=QtCore.Signal(object)
    failed=QtCore.Signal(str)

    def __init__(self,paths,directory,options):
        super().__init__()
        self.paths=paths
        self.directory=directory
        self.options=options
        self.stop=threading.Event()

    def request_stop(self):
        self.stop.set()

    @QtCore.Slot()
    def run(self):
        try:
            result=export_processed_batch(self.paths,self.directory,**self.options,
                stop_requested=self.stop.is_set,progress=self.progress.emit)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)
