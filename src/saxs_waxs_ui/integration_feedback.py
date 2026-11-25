"""Bounded stage logs and elapsed feedback for frozen-machine diagnosis."""
import json
import logging
from logging.handlers import RotatingFileHandler
import time
from PySide6 import QtCore
from .branding import artifacts_directory


class IntegrationFeedback(QtCore.QObject):
    def __init__(self,queue,panel,parent=None):
        super().__init__(parent)
        self.queue,self.panel=queue,panel
        self.started=None
        self.stage='等待工作线程'
        self.timer=QtCore.QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        queue.progress.connect(self._progress)
        queue.ready.connect(lambda _value:self._finish('completed'))
        queue.failed.connect(lambda _error:self._finish('failed'))
        queue.idle.connect(self._idle)
        self.logger=logging.getLogger('xrelay.integration')
        self.logger.propagate=False
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            try:
                folder=artifacts_directory()/'logs'
                folder.mkdir(parents=True,exist_ok=True)
                handler=RotatingFileHandler(folder/'integration.jsonl',maxBytes=1024*1024,
                    backupCount=1,encoding='utf-8')
                self.logger.addHandler(handler)
            except OSError:
                self.logger.addHandler(logging.NullHandler())

    def _log(self,event,**values):
        self.logger.info(json.dumps(dict(time=time.time(),event=event,**values),ensure_ascii=False))

    def start(self,count,*,long_running=False):
        self.started=time.monotonic()
        self.long_running=long_running
        self.stage='等待工作线程'
        self._log('submitted',panes=count)
        self.timer.start()

    def _progress(self,message):
        self.stage=str(message)
        self._log('stage',stage=self.stage)
        self._tick()

    def _tick(self):
        if self.started is None:return
        elapsed=time.monotonic()-self.started
        slow=elapsed>=30 and not getattr(self,'long_running',False)
        if slow:
            text=f'积分耗时异常：{self.stage}，已用 {elapsed:.0f} 秒。诊断记录位于 XRelay/logs。'
        else:
            text=f'正在后台积分：{self.stage}，{elapsed:.0f} 秒…'
        self.panel.set_status(text,error=slow)

    def _finish(self,event):
        if self.started is not None:
            self._log(event,elapsed_seconds=round(time.monotonic()-self.started,3),stage=self.stage)
        self.started=None
        self.timer.stop()

    def _idle(self):
        # An invalidated result is deliberately discarded by LatestTask. Do not
        # leave its old "integrating" status on screen after the worker exits.
        if self.started is not None:
            self._finish('superseded')
            if '后台积分' in self.panel.status_label.text():
                self.panel.set_status('参数已变化，当前积分结果已丢弃；等待新的积分请求。')
