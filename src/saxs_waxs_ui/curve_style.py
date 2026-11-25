"""One appearance model shared by preview, image export and editor shortcuts."""
import html,json,math,weakref
from pathlib import Path
from PySide6 import QtCore,QtGui,QtWidgets
import pyqtgraph as pg

DEFAULTS=dict(font='Arial',label_size=11.,tick_size=9.5,legend_size=8.5,
    label_bold=True,tick_bold=True,legend_bold=True,line_width=1.6,line_style='solid',
    axis_width=1.25,tick_width=1.,major_length=6.,minor_length=3.,minor_count=1,
    ticks_in=True,minor_ticks=True,ticks_bottom=True,ticks_left=True,ticks_top=False,ticks_right=False,
    x_step=0.,y_step=0.,axis_color='#262626',
    legend_mode='auto',legend_position='outside',legend_columns=1,legend_frame=False,
    short_names=True,title='',x_title='',y_title='',frame=False,grid=False)
QT_LINES=dict(solid=QtCore.Qt.PenStyle.SolidLine,dash=QtCore.Qt.PenStyle.DashLine,
    dot=QtCore.Qt.PenStyle.DotLine,dashdot=QtCore.Qt.PenStyle.DashDotLine)
MPL_LINES=dict(solid='-',dash='--',dot=':',dashdot='-.')

class EditableLegend(pg.LegendItem):
    def __init__(self,panel,**kwargs):
        super().__init__(**kwargs);self.panel=weakref.proxy(panel)

    def mouseDragEvent(self,event):
        super().mouseDragEvent(event)
        if event.isFinish() and event.button()==QtCore.Qt.MouseButton.LeftButton:
            bounds=self.parentItem().boundingRect()
            if bounds.width()>0 and bounds.height()>0:
                self.panel._legend_manual_anchor=(self.pos().x()/bounds.width(),1-self.pos().y()/bounds.height())
                self.panel._legend_anchor_position=self.panel._curve_style['legend_position']
                self.panel.plot_changed.emit()


def default_style(settings):
    style=dict(DEFAULTS)
    try:
        saved=json.loads(settings.value('curvePlot/default_style','{}'))
        if isinstance(saved,dict):
            for key,value in saved.items():
                if key not in DEFAULTS:continue
                default=DEFAULTS[key]
                if isinstance(default,bool) and isinstance(value,bool):style[key]=value
                elif isinstance(default,(float,int)) and not isinstance(default,bool) and isinstance(value,(float,int)) and math.isfinite(value) and 0<=value<=10000:style[key]=value
                elif isinstance(default,str) and isinstance(value,str):style[key]=value
        if isinstance(saved,dict) and isinstance(saved.get('top_ticks'),bool):
            for key in ('ticks_top','ticks_right'):
                if key not in saved:style[key]=saved['top_ticks'] and style['frame']
        if style['line_style'] not in QT_LINES:style['line_style']='solid'
        if style['legend_mode'] not in ('auto','show','hide'):style['legend_mode']='auto'
        if style['legend_position'] not in ('outside','upper right','upper left','lower right','lower left'):style['legend_position']='outside'
        style['legend_columns']=max(1,min(4,int(style['legend_columns'])))
        style['minor_count']=max(1,min(9,int(style['minor_count'])))
    except (TypeError,ValueError):return dict(DEFAULTS)
    return style


def curve_appearance(panel,index):
    key=panel._visible_curve_keys[index]
    original=panel._available_keys.index(key)
    values=dict(color=panel._curve_colors[original%len(panel._curve_colors)],
        width=panel._curve_style['line_width'],style=panel._curve_style['line_style'],alias='')
    values.update(panel._curve_overrides.get(key,{}))
    return values


def curve_label(panel,index):
    values=curve_appearance(panel,index)
    if values.get('alias'):return values['alias']
    name=panel._curve_items[index][0]
    if not panel._curve_style['short_names']:return name
    base=name.split(' [/')[0]
    source,separator,frame=base.partition(' · 帧')
    if '.' in source:source=Path(source).stem
    source=source.removesuffix('_master')
    return source+(' · 帧'+frame if separator else '')


def curve_pen(panel,index,focused=False):
    values=curve_appearance(panel,index)
    return pg.mkPen(values['color'],width=values['width']*(1.5 if focused else 1.),style=QT_LINES[values['style']])


def legend_visible(panel):
    mode=panel._curve_style['legend_mode']
    return bool(panel._curve_items) and (mode=='show' or mode=='auto' and len(panel._curve_items)<=8)


def apply_axes(panel):
    s=panel._curve_style
    for side in ('top','right'):
        panel.plot.showAxis(side,show=bool(panel.four_frame_check.isChecked() or s['ticks_'+side]))
        panel._plot_axes[side].setStyle(showValues=False)
    font=QtGui.QFont(s['font']);font.setPointSizeF(s['tick_size']);font.setBold(s['tick_bold'])
    for side,axis in panel._plot_axes.items():
        axis.setPen(pg.mkPen(s['axis_color'],width=s['axis_width']))
        draw=s['ticks_'+side]
        axis.setTickPen(pg.mkPen(s['axis_color'],width=s['tick_width']) if draw or axis.grid else pg.mkPen(None))
        axis.setTextPen(pg.mkPen(s['axis_color']));axis.setTickFont(font)
        length=int(round(s['major_length']))*(-1 if s['ticks_in'] else 1) if draw else 0
        axis.setStyle(tickLength=length,maxTickLevel=1 if s['minor_ticks'] else 0)
        axis._curve_minor_length=s['minor_length']
        axis._curve_minor_count=s['minor_count'] if s['minor_ticks'] else 0
        step=s['x_step'] if side in ('bottom','top') else s['y_step']
        axis.setTickSpacing(major=step,minor=step/(s['minor_count']+1)) if step>0 else axis.setTickSpacing()
    panel.plot.getViewBox().setBorder(pg.mkPen(s['axis_color'],width=s['axis_width']) if panel.four_frame_check.isChecked() else None)


def render_legend(panel):
    s=panel._curve_style
    visible=legend_visible(panel)
    outside=visible and s['legend_position']=='outside'
    panel.legend.setVisible(visible and not outside)
    if visible and not outside:
        anchors={'upper right':((1,0),(1,0),(-8,8)),'upper left':((0,0),(0,0),(8,8)),
                 'lower right':((1,1),(1,1),(-8,-8)),'lower left':((0,1),(0,1),(8,-8))}
        item,parent,offset=anchors[s['legend_position']]
        manual=getattr(panel,'_legend_manual_anchor',None)
        if getattr(panel,'_legend_anchor_position',None)!=s['legend_position']:
            manual=None;panel._legend_manual_anchor=None
        if manual:
            panel.legend.anchor(itemPos=(0,0),parentPos=(manual[0],1-manual[1]),offset=(0,0))
        else:panel.legend.anchor(itemPos=item,parentPos=parent,offset=offset)
        panel.legend.setColumnCount(s['legend_columns'])
        panel.legend.setPen(pg.mkPen(s['axis_color']) if s['legend_frame'] else None)
    font=QtGui.QFont(s['font']);font.setPointSizeF(s['legend_size']);font.setBold(s['legend_bold'])
    for _sample,label in panel.legend.items:
        label.setFont(font)
        label.setText(label.text,color=s['axis_color'],size=f'{s["legend_size"]}pt',bold=s['legend_bold'])
    layout=panel._outside_legend_layout
    while layout.count():
        widget=layout.takeAt(0).widget()
        if widget is not None:widget.deleteLater()
    if outside:
        for index in range(len(panel._curve_items)):
            button=QtWidgets.QPushButton(curve_label(panel,index));button.setFlat(True);button.setFont(font)
            button.setToolTip(panel._curve_items[index][0]+'\n在“曲线样式”中修改图例文字。')
            color=curve_appearance(panel,index)['color']
            pixmap=QtGui.QPixmap(30,12);pixmap.fill(QtCore.Qt.GlobalColor.transparent)
            painter=QtGui.QPainter(pixmap);painter.setPen(curve_pen(panel,index));painter.drawLine(1,6,29,6);painter.end()
            button.setIcon(QtGui.QIcon(pixmap));button.setStyleSheet(f'text-align:left;color:{s["axis_color"]};padding:1px;')
            button.clicked.connect(lambda _checked=False,i=index:panel._set_curve_focus(None if panel._focused_curve_index==i else i))
            layout.addWidget(button,index//s['legend_columns'],index%s['legend_columns'])
        rows=math.ceil(len(panel._curve_items)/s['legend_columns'])
        height=min(180,rows*(QtGui.QFontMetrics(font).height()+8)+8)
        panel._outside_legend.setFixedHeight(height)
        panel._outside_legend.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel if s['legend_frame'] else QtWidgets.QFrame.Shape.NoFrame)
    panel._outside_legend.setVisible(outside)


class CurveStyleDialog(QtWidgets.QDialog):
    def __init__(self,panel):
        super().__init__(panel)
        self.panel=panel;self.controls={}
        self.setWindowTitle('绘图选项');self.resize(760,650)
        self.timer=QtCore.QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(120)
        self.timer.timeout.connect(self.apply)
        tabs=QtWidgets.QTabWidget()
        general=QtWidgets.QWidget();form=QtWidgets.QFormLayout(general)
        font=QtWidgets.QFontComboBox();font.setCurrentFont(QtGui.QFont(panel._curve_style['font']))
        font.currentFontChanged.connect(lambda _:self.timer.start());self.controls['font']=font;form.addRow('字体',font)
        def spin(key,label,low,high,decimals=1):
            widget=QtWidgets.QDoubleSpinBox();widget.setRange(low,high);widget.setDecimals(decimals)
            widget.setKeyboardTracking(False);widget.setValue(panel._curve_style[key]);widget.valueChanged.connect(lambda _:self.timer.start())
            self.controls[key]=widget;form.addRow(label,widget)
        def check(key,label):
            widget=QtWidgets.QCheckBox();widget.setChecked(panel._curve_style[key]);widget.toggled.connect(lambda _:self.timer.start())
            self.controls[key]=widget;form.addRow(label,widget)
        def combo(key,label,choices):
            widget=QtWidgets.QComboBox()
            for text,value in choices:widget.addItem(text,value)
            widget.setCurrentIndex(max(0,widget.findData(panel._curve_style[key])));widget.currentIndexChanged.connect(lambda _:self.timer.start())
            self.controls[key]=widget;form.addRow(label,widget)
        def text(key,label):
            widget=QtWidgets.QLineEdit(panel._curve_style[key]);widget.setPlaceholderText('留空使用当前坐标名称')
            widget.textChanged.connect(lambda _:self.timer.start());self.controls[key]=widget;form.addRow(label,widget)
        spin('line_width','默认曲线线宽',.2,12.)
        combo('line_style','默认线型',[('实线','solid'),('虚线','dash'),('点线','dot'),('点划线','dashdot')])
        text('title','图标题');text('x_title','横轴标题');text('y_title','纵轴标题')
        spin('label_size','标题字号 / pt',5.,40.);check('label_bold','标题加粗')
        spin('tick_size','刻度字号 / pt',5.,40.);check('tick_bold','刻度文字加粗')
        from .widgets import scrollable_content
        tabs.addTab(scrollable_content(general),'曲线与文字')
        axes=QtWidgets.QWidget();form=QtWidgets.QFormLayout(axes)
        spin('axis_width','坐标轴线宽',.2,8.);spin('tick_width','刻度线宽',.2,8.)
        spin('major_length','主刻度长度',1.,20.);spin('minor_length','次刻度长度',0.,20.)
        check('ticks_in','刻度向内');check('minor_ticks','显示次刻度')
        spin('minor_count','每个主刻度间的次刻度数',1.,9.,0)
        spin('x_step','横轴主刻度间隔（0为自动）',0.,10000.,4)
        spin('y_step','纵轴主刻度间隔（0为自动）',0.,10000.,4)
        for side,label in (('bottom','下轴刻度线'),('left','左轴刻度线'),('top','上轴刻度线'),('right','右轴刻度线')):
            check('ticks_'+side,label)
        note=QtWidgets.QLabel('四个方向分别控制刻度线；不隐藏坐标文字。开启上／右刻度会显示对应轴线。');note.setWordWrap(True);form.addRow(note)
        color=QtWidgets.QPushButton(panel._curve_style['axis_color']);self.controls['axis_color']=color
        def axis_color():
            chosen=QtWidgets.QColorDialog.getColor(QtGui.QColor(color.text()),self)
            if chosen.isValid():color.setText(chosen.name());self.timer.start()
        color.clicked.connect(axis_color);form.addRow('轴线与文字颜色',color)
        tabs.addTab(scrollable_content(axes),'坐标轴与刻度')
        legend=QtWidgets.QWidget();form=QtWidgets.QFormLayout(legend)
        combo('legend_mode','图例显示',[('自动（超过8条收起）','auto'),('显示','show'),('隐藏','hide')])
        combo('legend_position','图例位置',[('图外下方（避免遮挡）','outside'),('右上','upper right'),('左上','upper left'),('右下','lower right'),('左下','lower left')])
        spin('legend_size','图例字号 / pt',5.,32.);check('legend_bold','图例加粗')
        spin('legend_columns','图例列数',1.,4.,0);check('legend_frame','图例边框');check('short_names','默认缩短文件名，省略数据集路径')
        form.addRow(QtWidgets.QLabel('图内图例可拖动；逐条图例文字、颜色和线型在“各条曲线”中修改。'))
        tabs.addTab(scrollable_content(legend),'图例')
        self.table=QtWidgets.QTableWidget();self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(['完整曲线身份','图例文字','颜色（双击选择）','线宽','线型'])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self._curve_changed);self.table.cellDoubleClicked.connect(self._color_clicked)
        tabs.addTab(self.table,'各条曲线')
        self.refresh_curves()
        save=QtWidgets.QPushButton('保存为默认样式');reset=QtWidgets.QPushButton('恢复初始样式')
        save.clicked.connect(self._save_default);reset.clicked.connect(self._reset)
        close=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close);close.rejected.connect(self.close)
        buttons=QtWidgets.QHBoxLayout();buttons.addWidget(save);buttons.addWidget(reset);buttons.addStretch();buttons.addWidget(close)
        caption=QtWidgets.QLabel('仅修改图形外观；积分参数、原始数据及数据表的来源名称保持不变。');caption.setWordWrap(True)
        layout=QtWidgets.QVBoxLayout(self);layout.addWidget(caption)
        layout.addWidget(tabs,1);layout.addLayout(buttons)
        self.setSizeGripEnabled(True)
        if self.screen():
            area=self.screen().availableGeometry();self.resize(min(760,area.width()-24),min(650,area.height()-48))

    def refresh_curves(self):
        blocker=QtCore.QSignalBlocker(self.table);self.table.setRowCount(len(self.panel._curve_items))
        for index,(name,_curve) in enumerate(self.panel._curve_items):
            values=curve_appearance(self.panel,index)
            from .i18n import language_manager
            line_names=dict(solid='实线',dash='虚线',dot='点线',dashdot='点划线') if language_manager().language=='zh' else dict(solid='Solid',dash='Dashed',dot='Dotted',dashdot='Dash-dot')
            for column,value in enumerate((name,curve_label(self.panel,index),values['color'],str(values['width']),line_names[values['style']])):
                item=QtWidgets.QTableWidgetItem(value);item.setData(QtCore.Qt.ItemDataRole.UserRole,self.panel._visible_curve_keys[index])
                if column==0:item.setFlags(item.flags()&~QtCore.Qt.ItemFlag.ItemIsEditable)
                if column==2:item.setBackground(QtGui.QColor(value))
                self.table.setItem(index,column,item)
        del blocker

    def retranslate_curve_ui(self,language):
        blocker=QtCore.QSignalBlocker(self.table)
        names=dict(solid='实线',dash='虚线',dot='点线',dashdot='点划线') if language=='zh' else dict(solid='Solid',dash='Dashed',dot='Dotted',dashdot='Dash-dot')
        for index in range(min(self.table.rowCount(),len(self.panel._curve_items))):
            self.table.item(index,4).setText(names[curve_appearance(self.panel,index)['style']])
        del blocker

    def _color_clicked(self,row,column):
        if column!=2:return
        item=self.table.item(row,column)
        chosen=QtWidgets.QColorDialog.getColor(QtGui.QColor(item.text()),self)
        if chosen.isValid():item.setText(chosen.name())

    def _curve_changed(self,item):
        key=item.data(QtCore.Qt.ItemDataRole.UserRole);column=item.column();value=item.text().strip()
        try:
            if column==1:field='alias'
            elif column==2:
                if not QtGui.QColor(value).isValid():raise ValueError()
                field='color';value=QtGui.QColor(value).name()
            elif column==3:
                field='width';value=float(value)
                if not math.isfinite(value) or not .2<=value<=12:raise ValueError()
            elif column==4:
                field='style'
                value={'实线':'solid','虚线':'dash','点线':'dot','点划线':'dashdot',
                    'Solid':'solid','Dashed':'dash','Dotted':'dot','Dash-dot':'dashdot'}.get(value,value)
                if value not in QT_LINES:raise ValueError()
            else:return
            self.panel._curve_overrides.setdefault(key,{})[field]=value
            self.timer.start()
            item.setToolTip('')
        except ValueError:item.setToolTip('线宽为0.2～12；线型可填solid、dash、dot或dashdot；颜色使用有效颜色名称或#RRGGBB。')

    def apply(self):
        for key,widget in self.controls.items():
            if isinstance(widget,QtWidgets.QFontComboBox):value=widget.currentFont().family()
            elif isinstance(widget,QtWidgets.QComboBox):value=widget.currentData()
            elif isinstance(widget,QtWidgets.QCheckBox):value=widget.isChecked()
            elif isinstance(widget,QtWidgets.QDoubleSpinBox):value=widget.value()
            else:value=widget.text()
            if key in ('minor_count','legend_columns'):value=int(value)
            self.panel._curve_style[key]=value
        self.panel._render_curves()

    def _save_default(self):
        self.apply()
        self.panel._curve_style_settings.setValue('curvePlot/default_style',json.dumps(self.panel._curve_style,ensure_ascii=False))
        self.panel._curve_style_settings.sync()

    def _reset(self):
        self.timer.stop();self.panel._curve_style=dict(DEFAULTS);self.panel._curve_overrides.clear();self.panel._render_curves()
        self.close();self.deleteLater();self.panel._curve_style_dialog=None;self.panel._choose_curve_style()

    def closeEvent(self,event):
        if self.timer.isActive():self.timer.stop();self.apply()
        super().closeEvent(event)
