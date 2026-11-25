"""Local, bounded runtime diagnostics. Never collect experimental arrays/files."""
import ctypes
import faulthandler
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import sys
import threading
import time
import traceback
import zipfile
from .branding import VERSION, artifacts_directory

_logger=None
_fault_file=None
_display_context={}


def update_display_context(layout=None,dialog=None):
    """GUI thread only. Cache technical geometry, never titles or data paths."""
    from PySide6 import QtWidgets
    app=QtWidgets.QApplication.instance()
    if app is None:return
    screens=[]
    for index,screen in enumerate(app.screens()):
        area=screen.availableGeometry();geometry=screen.geometry()
        screens.append(dict(index=index,geometry=[geometry.x(),geometry.y(),geometry.width(),geometry.height()],
            available=[area.x(),area.y(),area.width(),area.height()],device_pixel_ratio=screen.devicePixelRatio(),
            logical_dpi=round(screen.logicalDotsPerInch(),2),physical_dpi=round(screen.physicalDotsPerInch(),2)))
    global _display_context
    from PySide6 import QtCore
    values=dict(qt_platform=app.platformName(),qt_version=QtCore.qVersion(),screens=screens,
                application_font=dict(family=app.font().family(),point_size=app.font().pointSizeF()))
    if layout is not None:values['layout']=layout
    elif 'layout' in _display_context:values['layout']=_display_context['layout']
    if dialog is not None:values['export_dialog']=dialog
    elif 'export_dialog' in _display_context:values['export_dialog']=_display_context['export_dialog']
    _display_context=values


def record(event,**values):
    if _logger is not None:
        _logger.info(json.dumps(dict(time=time.time(),pid=os.getpid(),event=event,**values),ensure_ascii=False))


def _stack(frame):
    # Keep code locations only: no locals, arguments, data paths or window titles.
    return [dict(file=Path(row.filename).name,line=row.lineno,function=row.name)
            for row in traceback.extract_stack(frame)[-14:]]


def memory_status():
    values=dict(cpu_seconds=round(time.process_time(),3))
    if os.name=='nt':
        class Memory(ctypes.Structure):
            _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(name,ctypes.c_ulonglong)
                for name in ('total','available','total_page','available_page','total_virtual','available_virtual','extended')]
        state=Memory();state.length=ctypes.sizeof(state)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            values.update(ram_gb=round(state.total/2**30,2),available_ram_gb=round(state.available/2**30,2))
    return values


def environment():
    values=dict(version=VERSION,os=platform.platform(),architecture=platform.machine(),
        logical_processors=os.cpu_count(),python=platform.python_version(),**memory_status())
    values['display']=dict(_display_context)
    if os.name=='nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                values['cpu']=winreg.QueryValueEx(key,'ProcessorNameString')[0].strip()
        except OSError:pass
    if getattr(sys,'frozen',False):
        try:
            with open(sys.executable,'rb') as stream:values['exe_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
        except OSError:pass
    return values


def install_runtime_diagnostics():
    global _logger,_fault_file
    if _logger is not None:return
    try:
        folder=artifacts_directory()/'logs';folder.mkdir(parents=True,exist_ok=True)
        # Do not let restarts accumulate unbounded per-process files.
        for pattern,keep in (('runtime-*.jsonl*',6),('fault-*.txt',3)):
            for old in sorted(folder.glob(pattern),key=lambda p:p.stat().st_mtime,reverse=True)[keep:]:
                try:old.unlink()
                except OSError:pass
        logger=logging.getLogger('xrelay.runtime');logger.setLevel(logging.INFO);logger.propagate=False
        logger.addHandler(RotatingFileHandler(folder/f'runtime-{os.getpid()}.jsonl',
            maxBytes=512*1024,backupCount=1,encoding='utf-8'))
        _logger=logger
        _fault_file=(folder/f'fault-{os.getpid()}.txt').open('w',encoding='utf-8')
        faulthandler.enable(file=_fault_file,all_threads=True)
        previous=sys.excepthook
        def exception(kind,value,tb):
            frames=[]
            while tb is not None:
                frames.append(dict(file=Path(tb.tb_frame.f_code.co_filename).name,
                    function=tb.tb_frame.f_code.co_name,line=tb.tb_lineno));tb=tb.tb_next
            record('unhandled_exception',type=kind.__name__,stack=frames)
            if sys.stderr is not None:previous(kind,value,value.__traceback__)
        sys.excepthook=exception
        previous_thread=threading.excepthook
        def thread_exception(args):
            exception(args.exc_type,args.exc_value,args.exc_traceback)
            if sys.stderr is not None:previous_thread(args)
        threading.excepthook=thread_exception
        record('session_started',environment=environment())
    except OSError:
        # A read-only/unavailable profile must not prevent processing.
        pass


def install_ui_monitor(app):
    from PySide6 import QtCore
    if hasattr(app,'_xrelay_runtime_monitor'):return
    install_runtime_diagnostics()
    update_display_context()
    record('display_detected',**_display_context)
    state=dict(last=time.monotonic(),reported=False)
    stop=threading.Event()
    timer=QtCore.QTimer(app);timer.setInterval(200)
    class Inputs(QtCore.QObject):
        last=0.
        def eventFilter(self,watched,event):
            if event.type() in (QtCore.QEvent.Type.MouseButtonRelease,QtCore.QEvent.Type.KeyPress,QtCore.QEvent.Type.Wheel):
                now=time.monotonic()
                if now-self.last>=.25:
                    self.last=now
                    record('ui_input',event_type=int(event.type()),widget=watched.metaObject().className())
            return False
    inputs=Inputs(app);app.installEventFilter(inputs)
    def pulse():
        now=time.monotonic();gap=now-state['last']
        if gap>=2:record('ui_recovered',gap_seconds=round(gap,3))
        state.update(last=now,reported=False)
    def watch():
        next_resources=time.monotonic()+15
        while not stop.wait(.5):
            now=time.monotonic();gap=now-state['last']
            if gap>=2 and not state['reported']:
                state['reported']=True
                frame=sys._current_frames().get(threading.main_thread().ident)
                record('ui_unresponsive',gap_seconds=round(gap,3),stack=_stack(frame) if frame else [],**memory_status())
            if now>=next_resources:
                record('resources',**memory_status());next_resources=now+15
    def finish():
        stop.set();timer.stop();record('session_closed')
    timer.timeout.connect(pulse);timer.start()
    app.aboutToQuit.connect(finish)
    watcher=threading.Thread(target=watch,name='xrelay-diagnostics',daemon=True);watcher.start()
    app._xrelay_runtime_monitor=(timer,stop,watcher,inputs)
    record('ui_monitor_started',qt_platform=app.platformName())


def export_report(destination=None):
    folder=artifacts_directory()/'logs'
    if destination is None:
        reports=artifacts_directory()/'diagnostic-reports';reports.mkdir(parents=True,exist_ok=True)
        destination=reports/f'XRelay-diagnostics-{time.strftime("%Y%m%d-%H%M%S")}-{os.getpid()}.zip'
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('environment.json',json.dumps(environment(),ensure_ascii=False,indent=2))
        archive.writestr('display-layout.json',json.dumps(_display_context,ensure_ascii=False,indent=2))
        for path in sorted(folder.glob('*')) if folder.exists() else ():
            if path.is_file() and path.resolve()!=destination.resolve() and path.name.startswith(('runtime-','integration.','fault-')):
                # A snapshot is bounded even if another running process writes.
                with path.open('rb') as stream:data=stream.read(2*1024*1024)
                archive.writestr('logs/'+path.name,data)
    return destination
