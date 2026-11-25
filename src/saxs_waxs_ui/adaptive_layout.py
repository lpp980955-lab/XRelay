"""Display-aware layout in Qt logical pixels; no changes to scientific state."""
import weakref
from PySide6 import QtCore,QtGui,QtWidgets


def recommended_sizes(width,em,*,browser=True,processing=True):
    """Reserve a useful central workspace and share the remainder with sidebars."""
    total=max(0,int(width));em=max(7.,float(em))
    left=min(total*.19,22*em) if browser else 0
    right=min(total*.35,38*em) if processing else 0
    # Keep every requested region reachable even on a narrow logical desktop.
    return [round(left),max(0,total-round(left)-round(right)),round(right)]


class ExportDialogLayout(QtCore.QObject):
    """Fit independent export windows; resizing does not change render quality."""
    def __init__(self,dialog,sidebar):
        super().__init__(dialog);self.dialog=weakref.proxy(dialog);self.sidebar=sidebar
        # A horizontally Ignored sidebar reports a zero size hint even with a
        # fixed width. QBoxLayout can then place the preview over the controls.
        sidebar.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,QtWidgets.QSizePolicy.Policy.Expanding)
        self._screen=None
        self._report_timer=QtCore.QTimer(self);self._report_timer.setSingleShot(True);self._report_timer.setInterval(500)
        self._report_timer.timeout.connect(self.report)
        dialog.installEventFilter(self)
        self.fit()

    def fit(self,*_):
        d=self.dialog;screen=d.screen()
        if screen:
            area=screen.availableGeometry()
            d.setMinimumSize(min(d.minimumWidth(),max(360,area.width()-24)),min(d.minimumHeight(),max(220,area.height()-48)))
            if not d.isMaximized():d.resize(min(d.width(),area.width()-24),min(d.height(),area.height()-48))
        self.reflow()

    def reflow(self):
        d=self.dialog;stacked=d.width()<560
        layout=d.layout()
        if isinstance(layout,QtWidgets.QBoxLayout):
            direction=QtWidgets.QBoxLayout.Direction.TopToBottom if stacked else QtWidgets.QBoxLayout.Direction.LeftToRight
            if layout.direction()!=direction:layout.setDirection(direction)
        width=max(180,d.width()-22) if stacked else max(220,min(460,round(d.width()*.36)))
        self.sidebar.setMaximumHeight(max(70,round(d.height()*.40)) if stacked else 16777215)
        if self.sidebar.width()!=width:self.sidebar.setFixedWidth(width)
        if isinstance(self.sidebar,ResponsiveScrollArea):self.sidebar.reflow()
        self._report_timer.start()

    def report(self):
        from .runtime_diagnostics import record,update_display_context
        d=self.dialog;layout=d.layout()
        areas=[layout.itemAt(i).widget().geometry() for i in range(layout.count()) if layout.itemAt(i).widget() is not None]
        overlap=any(a.intersects(b) for i,a in enumerate(areas) for b in areas[i+1:])
        state=dict(kind=d.metaObject().className(),window=[d.width(),d.height()],
                   regions=[[r.x(),r.y(),r.width(),r.height()] for r in areas],overlap=overlap)
        update_display_context(dialog=state)
        record('export_dialog_layout',**state)

    def eventFilter(self,obj,event):
        if event.type()==QtCore.QEvent.Type.Show:
            handle=self.dialog.windowHandle()
            if handle and not self._screen:
                handle.screenChanged.connect(self.fit);self._screen=True
            self.fit()
        elif event.type()==QtCore.QEvent.Type.Resize:self.reflow()
        return False


class ResponsiveScrollArea(QtWidgets.QScrollArea):
    """Reflow forms and action rows before resorting to horizontal scrolling."""
    def __init__(self,content):
        super().__init__()
        self.setWidgetResizable(True);self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setMinimumSize(0,0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Expanding)
        self.setWidget(content)
        self._boxes={};self._grids={};self._updating=False;self.compact=False
        self._timer=QtCore.QTimer(self);self._timer.setSingleShot(True);self._timer.setInterval(30)
        self._timer.timeout.connect(self.reflow)
        self.viewport().installEventFilter(self);content.installEventFilter(self)
        self.protect_heights()

    def protect_heights(self):
        for widget in self.widget().findChildren(QtWidgets.QWidget):
            if isinstance(widget,(QtWidgets.QAbstractSpinBox,QtWidgets.QComboBox,QtWidgets.QAbstractButton)):
                widget.setMinimumHeight(max(0,widget.minimumSizeHint().height()))

    def eventFilter(self,obj,event):
        if not getattr(self,'_updating',True) and event.type() in (QtCore.QEvent.Type.Resize,QtCore.QEvent.Type.Show,
                QtCore.QEvent.Type.FontChange,QtCore.QEvent.Type.LanguageChange):
            self._timer.start()
        return super().eventFilter(obj,event)

    def reflow(self):
        if self._updating:return
        self._updating=True
        try:
            content=self.widget();width=max(80,self.viewport().width()-16)
            self.protect_heights()
            em=max(7,self.fontMetrics().horizontalAdvance('M'))
            self.compact=width<38*em
            for combo in content.findChildren(QtWidgets.QComboBox):
                combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                combo.setMinimumContentsLength(1);combo.setMinimumWidth(0)
                combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,QtWidgets.QSizePolicy.Policy.Fixed)
            for form in content.findChildren(QtWidgets.QFormLayout):
                policy=(QtWidgets.QFormLayout.RowWrapPolicy.WrapAllRows if self.compact else
                        QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
                if form.rowWrapPolicy()!=policy:form.setRowWrapPolicy(policy)
                form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for box in content.findChildren(QtWidgets.QHBoxLayout):
                if box not in self._boxes:self._boxes[box]=box.direction()
                children=[box.itemAt(i) for i in range(box.count())]
                needed=sum(max(0,item.minimumSize().width()) for item in children)+max(0,len(children)-1)*max(0,box.spacing())
                direction=QtWidgets.QBoxLayout.Direction.TopToBottom if needed>width else self._boxes[box]
                if box.direction()!=direction:box.setDirection(direction)
            for grid in content.findChildren(QtWidgets.QGridLayout):
                if grid not in self._grids:
                    entries=[(grid.itemAt(i).widget(),grid.getItemPosition(i)) for i in range(grid.count())]
                    if entries and all(isinstance(w,QtWidgets.QAbstractButton) for w,_ in entries):self._grids[grid]=entries
                if grid not in self._grids:continue
                entries=self._grids[grid]
                needed=sum(max(w.minimumSizeHint().width() for w,pos in entries if pos[1]==col)
                           for col in sorted({pos[1] for _,pos in entries}))+max(0,grid.horizontalSpacing())*(grid.columnCount()-1)
                narrow=needed>width
                if bool(grid.property('adaptiveStacked'))==narrow:continue
                for widget,_ in entries:grid.removeWidget(widget)
                for i,(widget,pos) in enumerate(entries):grid.addWidget(widget,i,0) if narrow else grid.addWidget(widget,*pos)
                grid.setProperty('adaptiveStacked',narrow)
            content.layout().activate()
            content.setMinimumHeight(content.minimumSizeHint().height())
            content.updateGeometry()
        finally:self._updating=False


class AdaptiveLayout(QtCore.QObject):
    """Rebalance on real display/window changes, but leave manual dragging alone."""
    def __init__(self,window):
        super().__init__(window);self.window=weakref.proxy(window);self.automatic=True
        self._applying=False;self._started=False;self._screen=None;self._connections=[]
        self._timer=QtCore.QTimer(self);self._timer.setSingleShot(True);self._timer.setInterval(120)
        self._timer.timeout.connect(self.apply)
        self._report_timer=QtCore.QTimer(self);self._report_timer.setSingleShot(True);self._report_timer.setInterval(500)
        self._report_timer.timeout.connect(self.report)
        self.action=QtGui.QAction('自动适配窗口布局',window);self.action.setCheckable(True);self.action.setChecked(True)
        self.action.setToolTip('手动拖动分隔线后暂停自动分栏；重新勾选可恢复。')
        self.action.toggled.connect(self.set_automatic)
        self.reset_action=QtGui.QAction('重新适配当前屏幕',window);self.reset_action.triggered.connect(self.reset)
        window.installEventFilter(self)
        window.main_splitter.splitterMoved.connect(self._dragged)
        window.integration_panel.integration_splitter.splitterMoved.connect(self._dragged)
        window.processing_tabs.currentChanged.connect(self.schedule)

    def eventFilter(self,obj,event):
        if event.type()==QtCore.QEvent.Type.Show and not self._started:
            self._started=True
            handle=self.window.windowHandle()
            if handle:handle.screenChanged.connect(self.bind_screen)
            self.bind_screen(self.window.screen())
            # Do not override a size explicitly supplied by caller/user.
            if self.window.size()==QtCore.QSize(1200,760):self.fit_window(initial=True)
        elif event.type() in (QtCore.QEvent.Type.Resize,QtCore.QEvent.Type.FontChange):self.schedule()
        return False

    def bind_screen(self,screen):
        for signal,slot in self._connections:
            try:signal.disconnect(slot)
            except (RuntimeError,TypeError):pass
        first=self._screen is None
        self._connections=[];self._screen=screen
        if screen:
            for signal in (screen.availableGeometryChanged,screen.logicalDotsPerInchChanged,screen.geometryChanged):
                signal.connect(self.display_changed);self._connections.append((signal,self.display_changed))
        self.schedule() if first else self.display_changed()

    def display_changed(self,*_):
        if not self._started:return
        self.action.setChecked(True);self.automatic=True
        self.fit_window();self.schedule()

    def fit_window(self,initial=False):
        screen=self._screen or self.window.screen()
        if screen is None or self.window.isMaximized() or self.window.isFullScreen():return
        area=screen.availableGeometry()
        margins=self.window.frameGeometry().size()-self.window.size()
        maxw=max(320,area.width()-max(16,margins.width()))
        maxh=max(240,area.height()-max(32,margins.height()))
        width=min(1600,round(maxw*.96)) if initial else min(self.window.width(),maxw)
        height=min(1000,round(maxh*.96)) if initial else min(self.window.height(),maxh)
        self.window.resize(width,height)
        frame=self.window.frameGeometry()
        x=min(max(frame.x(),area.left()),max(area.left(),area.right()-frame.width()+1))
        y=min(max(frame.y(),area.top()),max(area.top(),area.bottom()-frame.height()+1))
        self.window.move(x,y)

    def schedule(self,*_):
        if not self._applying:self._timer.start()

    def set_automatic(self,enabled):
        self.automatic=enabled
        if enabled:self.schedule()
        else:self._report_timer.start()

    def reset(self,*_):
        self.action.setChecked(True);self.automatic=True;self.fit_window();self.apply()

    def _dragged(self,*_):
        if not self._applying:
            self.action.setChecked(False);self._report_timer.start()

    def apply(self):
        self._applying=True
        try:
            w=self.window
            narrow=w.right_panel.width()<620
            w.shared_panel_label.setVisible(not narrow)
            w.add_pane_button.setText('＋' if narrow else '＋ 添加图像窗格')
            w.add_pane_button.setToolTip('添加内嵌图像窗格')
            for box in w.browser.findChildren(QtWidgets.QHBoxLayout):
                needed=sum(box.itemAt(i).minimumSize().width() for i in range(box.count()))+max(0,box.count()-1)*max(0,box.spacing())
                box.setDirection(QtWidgets.QBoxLayout.Direction.TopToBottom if needed>w.browser.width()-20 else QtWidgets.QBoxLayout.Direction.LeftToRight)
            if self.automatic:
                total=w.main_splitter.width()-w.main_splitter.handleWidth()*max(0,sum(not w.main_splitter.widget(i).isHidden() for i in range(3))-1)
                em=w.fontMetrics().horizontalAdvance('M')
                w.main_splitter.setSizes(recommended_sizes(total,em,browser=not w.browser.isHidden(),processing=not w.preview_panel.isHidden()))
                panel=w.integration_panel;available=max(180,panel.height()-55)
                panel.plot.setMinimumHeight(min(240,max(100,round(available*.32))))
                panel.controls_scroll.setMinimumHeight(65)
                panel.integration_splitter.setSizes([round(available*.45),round(available*.55)])
            for scroll in w.preview_panel.findChildren(ResponsiveScrollArea):scroll.reflow()
        finally:self._applying=False
        self._report_timer.start()

    def report(self):
        from .runtime_diagnostics import update_display_context,record
        w=self.window
        scrolls=[]
        for scroll in w.preview_panel.findChildren(ResponsiveScrollArea):
            if not scroll.isVisible():continue
            scrolls.append(dict(viewport=[scroll.viewport().width(),scroll.viewport().height()],
                content_minimum=[scroll.widget().minimumWidth(),scroll.widget().minimumHeight()],
                horizontal_scroll=scroll.horizontalScrollBar().maximum(),vertical_scroll=scroll.verticalScrollBar().maximum(),compact=scroll.compact))
        screens=QtWidgets.QApplication.screens()
        layout=dict(automatic=self.automatic,window=[w.width(),w.height()],panels=w.main_splitter.sizes(),
                    current_screen=screens.index(w.screen()) if w.screen() in screens else None,device_pixel_ratio=w.devicePixelRatioF(),
                    processing_page=w.processing_tabs.currentIndex(),scrolls=scrolls)
        update_display_context(layout)
        record('display_layout',**layout)
