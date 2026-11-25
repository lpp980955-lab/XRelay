"""Independent asynchronous export preview and per-user named style library."""
import json
from pathlib import Path
import tempfile
from PySide6 import QtCore,QtGui,QtWidgets
from saxs_waxs_core.presets import export_preset_to_document,export_preset_from_document
from .latest_task import LatestTask


class PreviewCanvas(QtWidgets.QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.item=self.scene().addPixmap(QtGui.QPixmap())
        self.fit_mode=True
        self.setMinimumSize(200,180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,QtWidgets.QSizePolicy.Policy.Expanding)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        self.setBackgroundBrush(QtGui.QColor('#e8edf2'))
    def sizeHint(self):return QtCore.QSize(660,600)
    def show_image(self,pixmap):
        self.item.setPixmap(pixmap);self.scene().setSceneRect(self.item.boundingRect())
        if self.fit_mode:self.fitInView(self.item,QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.fit_mode and not self.item.pixmap().isNull():self.fitInView(self.item,QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    def fit_page(self):
        self.fit_mode=True;self.show_image(self.item.pixmap())
    def actual_pixels(self):
        self.fit_mode=False;self.resetTransform();self.scale(1/self.devicePixelRatioF(),1/self.devicePixelRatioF())


def enable_export_preview(dialog,provider,*,settings=None):
    if 'Microsoft YaHei UI' in QtGui.QFontDatabase.families():
        dialog.setFont(QtGui.QFont('Microsoft YaHei UI',10))
    settings=settings if settings is not None else QtCore.QSettings('XRelay','ExportStyles')
    try:styles=json.loads(settings.value('styles','{}'))
    except (ValueError,TypeError):styles={}
    if not isinstance(styles,dict):styles={}
    row=QtWidgets.QHBoxLayout()
    combo=QtWidgets.QComboBox();combo.addItems(styles)
    row.addWidget(QtWidgets.QLabel('图片样式'));row.addWidget(combo,1)
    layout=dialog.layout()
    layout.insertLayout(0,row)
    style_buttons=QtWidgets.QHBoxLayout();layout.insertLayout(1,style_buttons)
    label=PreviewCanvas();dialog.preview_canvas=label
    status=QtWidgets.QLabel('正在准备高清预览…');status.setWordWrap(True)
    status.setFixedHeight(54)
    preview=QtWidgets.QWidget();pl=QtWidgets.QVBoxLayout(preview)
    toolbar=QtWidgets.QHBoxLayout()
    baseline=[None];current=[None]
    for text,fn in (('适合窗口',label.fit_page),('1:1 像素',label.actual_pixels),
                    ('设为对比基准',lambda:baseline.__setitem__(0,current[0]))):
        b=QtWidgets.QPushButton(text);b.clicked.connect(fn);toolbar.addWidget(b)
    compare=QtWidgets.QPushButton('按住对比')
    compare.pressed.connect(lambda:label.show_image(baseline[0]) if baseline[0] is not None else None)
    compare.released.connect(lambda:label.show_image(current[0]) if current[0] is not None else None)
    toolbar.addWidget(compare);pl.addLayout(toolbar);pl.addWidget(status)
    pl.addWidget(label,1)
    # Move the existing controls into their own column, keeping this preview
    # wholly inside the export dialog rather than borrowing the image viewer.
    controls=QtWidgets.QWidget();controls.setLayout(layout)
    for field in controls.findChildren(QtWidgets.QComboBox):
        field.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        field.setMinimumContentsLength(1)
    for field in controls.findChildren(QtWidgets.QAbstractSpinBox):
        field.setMinimumWidth(0)
        field.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Fixed)
    layout.setSpacing(8)
    layout.insertStretch(layout.count()-1,1)
    from .widgets import scrollable_content
    scroll=scrollable_content(controls)
    outer=QtWidgets.QHBoxLayout(dialog);outer.addWidget(scroll);outer.addWidget(preview,1)
    dialog.resize(1160,780)
    from .adaptive_layout import ExportDialogLayout
    dialog._display_layout=ExportDialogLayout(dialog,scroll)
    task=LatestTask(dialog);dialog._export_preview_tasks=task
    timer=QtCore.QTimer(dialog);timer.setSingleShot(True);timer.setInterval(250)
    dialog._export_preview_timer=timer
    def persist():
        settings.setValue('styles',json.dumps(styles,ensure_ascii=False));settings.sync()
    def apply_style(name):
        if name not in styles:return
        document=styles[name]
        dialog.set_export_preset(export_preset_from_document(document))
        dialog.range_combo.setCurrentIndex(dialog.range_combo.findData(document['detector_range_mode']))
        settings.setValue('last_style',name);timer.start()
    def save():
        n=1
        while f'样式{n}' in styles:n+=1
        name,ok=QtWidgets.QInputDialog.getText(dialog,'保存图片样式','名称',text=f'样式{n}')
        name=name.strip()
        if not ok or not name:return
        styles[name]=export_preset_to_document(dialog.export_preset());persist()
        if combo.findText(name)<0:combo.addItem(name)
        combo.setCurrentText(name);settings.setValue('last_style',name)
    def update():
        name=combo.currentText()
        if name in styles:styles[name]=export_preset_to_document(dialog.export_preset());persist()
    def rename():
        old=combo.currentText()
        if old not in styles:return
        name,ok=QtWidgets.QInputDialog.getText(dialog,'重命名样式','名称',text=old)
        name=name.strip()
        if ok and name and name!=old and name not in styles:
            styles[name]=styles.pop(old);persist();combo.setItemText(combo.currentIndex(),name);settings.setValue('last_style',name)
    def delete():
        name=combo.currentText()
        if name in styles:styles.pop(name);persist();combo.removeItem(combo.currentIndex())
    for text,fn in (('另存样式',save),('更新',update),('重命名',rename),('删除',delete)):
        button=QtWidgets.QPushButton(text);button.clicked.connect(fn);style_buttons.addWidget(button)
    combo.activated.connect(lambda _:apply_style(combo.currentText()))
    def render():
        try:
            preset=dialog.export_preset()
            data,mask,display,geometry=provider(dialog.batch_source=='raw_array')
            def work():
                from saxs_waxs_core.image_export import export_2d_image
                # Fixed high-resolution canvas: typography changes never change
                # sampling quality. Final output DPI remains an export setting.
                dpi=max(1,round(2800/(max(preset.image_width_mm,preset.image_height_mm)/25.4)))
                with tempfile.TemporaryDirectory() as temp:
                    path=Path(temp)/'preview.png'
                    export_2d_image(data,path,display,mask=mask,
                        width_mm=preset.image_width_mm,height_mm=preset.image_height_mm,dpi=dpi,
                        include_axes=preset.detector_include_axes,include_colorbar=preset.detector_include_colorbar,
                        font_family=preset.detector_font_family,label_font_size_pt=preset.detector_label_font_size_pt,
                        tick_font_size_pt=preset.detector_tick_font_size_pt,line_width_pt=preset.detector_line_width_pt,
                        axis_mode=preset.detector_axis_mode,geometry=geometry,write_parameter_file=False)
                    return path.read_bytes()
            task.submit(work)
        except Exception as exc:status.setText(str(exc))
    def ready(data):
        pixmap=QtGui.QPixmap();pixmap.loadFromData(data)
        current[0]=pixmap;label.show_image(pixmap)
        p=dialog.export_preset()
        status.setText(f'预览已更新 · 高清 {pixmap.width()}×{pixmap.height()} · 输出 {round(p.image_width_mm/25.4*p.image_dpi)}×{round(p.image_height_mm/25.4*p.image_dpi)} px；1:1 可拖动查看字体细节')
    task.ready.connect(ready);task.failed.connect(status.setText);timer.timeout.connect(render)
    def schedule(*_):
        task.invalidate()
        status.setText('正在更新高清预览…')
        timer.start()
    for widget in controls.findChildren(QtWidgets.QWidget):
        if isinstance(widget,(QtWidgets.QDoubleSpinBox,QtWidgets.QSpinBox)):widget.valueChanged.connect(schedule)
        elif isinstance(widget,QtWidgets.QComboBox):widget.currentTextChanged.connect(schedule)
        elif isinstance(widget,QtWidgets.QCheckBox):widget.toggled.connect(schedule)
    dialog.finished.connect(lambda *_:(timer.stop(),task.invalidate(),task.pool.waitForDone(30000)))
    last=settings.value('last_style','')
    if last in styles:combo.setCurrentText(last);apply_style(last)
    timer.start()
