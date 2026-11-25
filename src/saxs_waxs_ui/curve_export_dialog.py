"""Resizable, localized curve export with an independent high-resolution preview."""
import threading,uuid
from pathlib import Path
from PySide6 import QtCore,QtGui,QtWidgets
from .export_preview import PreviewCanvas
from .latest_task import LatestTask
from .curve_render import snapshot_plot,preview_png,save_curve_export


class CurveExportDialog(QtWidgets.QDialog):
    def __init__(self,panel):
        super().__init__(panel)
        self.panel=panel;self.settings=panel._curve_style_settings;self._closed=False
        self.setWindowTitle('导出积分曲线');self.setSizeGripEnabled(True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint,True)
        self.setMinimumSize(640,420);self.resize(1120,760)
        screen=self.screen()
        if screen:self.resize(min(1120,screen.availableGeometry().width()-40),min(760,screen.availableGeometry().height()-60))
        self.content_combo=QtWidgets.QComboBox();self.content_combo.addItem('曲线数据','data');self.content_combo.addItem('曲线图片','images')
        self.format_combo=QtWidgets.QComboBox()
        self.width_spin=QtWidgets.QDoubleSpinBox();self.height_spin=QtWidgets.QDoubleSpinBox()
        for field,key,default in ((self.width_spin,'width_mm',180.),(self.height_spin,'height_mm',130.)):
            field.setRange(30.,500.);field.setDecimals(1);field.setSuffix(' mm');field.setKeyboardTracking(False)
            try:field.setValue(float(self.settings.value('curveExport/'+key,default)))
            except (ValueError,TypeError):field.setValue(default)
        self.dpi_spin=QtWidgets.QSpinBox();self.dpi_spin.setRange(72,1200);self.dpi_spin.setKeyboardTracking(False)
        try:self.dpi_spin.setValue(int(self.settings.value('curveExport/dpi',600)))
        except (ValueError,TypeError):self.dpi_spin.setValue(600)
        self.note=QtWidgets.QLabel();self.note.setWordWrap(True)
        self.preview_status=QtWidgets.QLabel();self.preview_status.setWordWrap(True);self.preview_status.setFixedHeight(55)
        self.status=QtWidgets.QLabel();self.status.setWordWrap(True)
        self.style_button=QtWidgets.QPushButton('绘图选项…');self.style_button.clicked.connect(panel._choose_curve_style)
        choose=QtWidgets.QPushButton('选择显示曲线…');choose.clicked.connect(panel._choose_visible_curves)
        self.export_button=QtWidgets.QPushButton('选择保存位置并导出…');self.export_button.clicked.connect(self._choose_export)
        close=QtWidgets.QPushButton('关闭');close.clicked.connect(self.reject)
        form=QtWidgets.QFormLayout();form.addRow('导出内容',self.content_combo);form.addRow('文件格式',self.format_combo)
        self.page_widget=QtWidgets.QWidget();page=QtWidgets.QFormLayout(self.page_widget);page.setContentsMargins(0,0,0,0)
        page.addRow('宽度',self.width_spin);page.addRow('高度',self.height_spin)
        self.dpi_label=QtWidgets.QLabel('DPI');page.addRow(self.dpi_label,self.dpi_spin)
        controls=QtWidgets.QWidget();left=QtWidgets.QVBoxLayout(controls)
        left.addLayout(form);left.addWidget(self.page_widget);left.addWidget(choose);left.addWidget(self.style_button)
        left.addWidget(self.note);left.addStretch();left.addWidget(self.status);left.addWidget(self.export_button);left.addWidget(close)
        from .widgets import scrollable_content
        scroll=scrollable_content(controls)
        self.canvas=PreviewCanvas();self.table=QtWidgets.QTableWidget()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_stack=QtWidgets.QStackedWidget();self.preview_stack.addWidget(self.table);self.preview_stack.addWidget(self.canvas)
        fit=QtWidgets.QPushButton('适合窗口');fit.clicked.connect(self.canvas.fit_page)
        pixels=QtWidgets.QPushButton('1:1 像素');pixels.clicked.connect(self.canvas.actual_pixels)
        bar=QtWidgets.QHBoxLayout();bar.addWidget(fit);bar.addWidget(pixels);bar.addStretch()
        right=QtWidgets.QWidget();right_layout=QtWidgets.QVBoxLayout(right)
        right_layout.addLayout(bar);right_layout.addWidget(self.preview_status);right_layout.addWidget(self.preview_stack,1)
        layout=QtWidgets.QHBoxLayout(self);layout.addWidget(scroll);layout.addWidget(right,1)
        from .adaptive_layout import ExportDialogLayout
        self._display_layout=ExportDialogLayout(self,scroll)
        self._export_preview_tasks=LatestTask(self);self._export_preview_tasks.setObjectName('curve_export_preview')
        self._export_tasks=LatestTask(self);self._export_tasks.setObjectName('curve_export')
        self._export_preview_timer=QtCore.QTimer(self);self._export_preview_timer.setSingleShot(True);self._export_preview_timer.setInterval(200)
        self._export_preview_timer.timeout.connect(self._render_preview)
        self._export_preview_tasks.ready.connect(self._image_ready);self._export_preview_tasks.failed.connect(self.preview_status.setText)
        self._export_tasks.ready.connect(self._saved);self._export_tasks.failed.connect(self._failed)
        self._stop=threading.Event()
        self.content_combo.currentIndexChanged.connect(self._content_changed)
        self.format_combo.currentIndexChanged.connect(self.schedule)
        for field in (self.width_spin,self.height_spin):field.valueChanged.connect(self.schedule)
        self.dpi_spin.valueChanged.connect(self._page_status)
        panel.plot_changed.connect(self.schedule)
        self.finished.connect(lambda _value:self.request_stop())
        self._content_changed()

    def _content_changed(self,*_):
        images=self.content_combo.currentData()=='images'
        self.format_combo.blockSignals(True);self.format_combo.clear()
        self.format_combo.addItems(['PNG','TIFF','PDF','SVG'] if images else ['XLSX','CSV','TSV'])
        self.format_combo.blockSignals(False)
        self.page_widget.setVisible(images);self.preview_stack.setCurrentIndex(1 if images else 0)
        self.schedule()

    def schedule(self,*_):
        if self._closed:return
        self._export_preview_tasks.invalidate()
        self._export_preview_timer.start()

    def _render_preview(self):
        try:
            snapshot=snapshot_plot(self.panel)
            count=len(snapshot['curves'])
            self.note.setText(f'导出当前显示的 {count} 条曲线。图片沿用绘图选项。XLSX沿用原积分／显示坐标规则；CSV/TSV仅导出显示坐标。')
            self.export_button.setEnabled(not self._export_tasks.busy)
            images=self.content_combo.currentData()=='images'
            vector=self.format_combo.currentText() in ('PDF','SVG')
            self.dpi_spin.setVisible(not vector);self.dpi_label.setVisible(not vector)
            if images:
                w,h=self.width_spin.value(),self.height_spin.value()
                self.preview_status.setText('正在更新高清预览…')
                self._export_preview_tasks.submit(lambda:preview_png(snapshot,w,h))
            else:
                self._data_preview(snapshot)
        except ValueError as exc:
            self.preview_status.setText(str(exc));self.export_button.setEnabled(False)

    def _page_status(self,*_):
        w,h=self.width_spin.value(),self.height_spin.value();dpi=self.dpi_spin.value()
        self.preview_status.setText(f'高清排版预览 · {w:g}×{h:g} mm · 输出 {round(w/25.4*dpi)}×{round(h/25.4*dpi)} px（{dpi} DPI）')

    def _image_ready(self,data):
        pixmap=QtGui.QPixmap();pixmap.loadFromData(data);self.canvas.show_image(pixmap);self._page_status()

    def _data_preview(self,snapshot):
        curves=snapshot['curves'][:4];xlsx=self.format_combo.currentText()=='XLSX';pairs=[]
        import numpy as np
        for index,(name,curve,x,y) in enumerate(curves):
            if xlsx:
                pairs.append((snapshot['legend_labels'][index]+' · '+curve.x_unit,curve.x,curve.intensity))
                if np.array_equal(curve.x,x,equal_nan=True) and np.array_equal(curve.intensity,y,equal_nan=True):continue
            pairs.append((snapshot['legend_labels'][index]+' · '+snapshot['labels'][0],x,y))
        self.table.setRowCount(15);self.table.setColumnCount(len(pairs)*2)
        self.table.setHorizontalHeaderLabels([label for name,_x,_y in pairs for label in (name,'I')])
        for pair,(_name,x,y) in enumerate(pairs):
            for row in range(15):
                for col,values in enumerate((x,y)):
                    text=f'{values[row]:.7g}' if row<len(values) and np.isfinite(values[row]) else ''
                    self.table.setItem(row,pair*2+col,QtWidgets.QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        self.preview_status.setText('仅预览前4条曲线的前15行；导出包含全部当前显示曲线。')

    def _choose_export(self):
        fmt=self.format_combo.currentText().lower();images=self.content_combo.currentData()=='images'
        directory=str(self.settings.value('curveExport/last_dir',''))
        initial=str(Path(directory)/(self.panel._suggested_export_stem()+'.'+fmt))
        path,_=QtWidgets.QFileDialog.getSaveFileName(self,'导出积分曲线',initial,f'{fmt.upper()} (*.{fmt})')
        if path:self.export_to(path)

    def export_to(self,path):
        if self._export_tasks.busy:return
        snapshot=snapshot_plot(self.panel);target=Path(path)
        content=self.content_combo.currentData()
        # Build metadata from a non-existent path, avoiding a synchronous hash of
        # an existing large file before an overwrite.
        temporary=target.with_name('.xrelay-'+uuid.uuid4().hex)
        document=self.panel._export_parameter_document(temporary,
            export_type='curve_plot_image' if content=='images' else 'curve_data',axis_labels=snapshot['labels'])
        w,h,dpi=self.width_spin.value(),self.height_spin.value(),self.dpi_spin.value()
        self._stop.clear();self.export_button.setEnabled(False);self.status.setText('正在后台导出…')
        def work():
            if self._stop.is_set():raise ValueError('导出已取消')
            return save_curve_export(snapshot,target,content=content,width_mm=w,height_mm=h,dpi=dpi,document=document)
        self._export_tasks.submit(work)
        self.settings.setValue('curveExport/last_dir',str(target.parent))
        for key,value in (('width_mm',w),('height_mm',h),('dpi',dpi)):self.settings.setValue('curveExport/'+key,value)

    def _saved(self,path):self.export_button.setEnabled(True);self.status.setText(f'已导出：{Path(path).name}')
    def _failed(self,error):self.export_button.setEnabled(True);self.status.setText(error)

    def request_stop(self):
        self._closed=True
        self._stop.set();self._export_preview_timer.stop();self._export_preview_tasks.invalidate()

    def reject(self):
        if self._export_tasks.busy:self.status.setText('正在导出，请完成后再关闭。');return
        self.request_stop();super().reject()

    def closeEvent(self,event):
        if self._export_tasks.busy:event.ignore();self.status.setText('正在导出，请完成后再关闭。');return
        self.request_stop();super().closeEvent(event)
