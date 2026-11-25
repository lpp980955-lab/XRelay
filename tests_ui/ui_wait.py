"""Bounded event-loop waits for the current asynchronous GUI contract."""
import time
from PySide6 import QtWidgets
from saxs_waxs_ui.latest_task import LatestTask


def drain(app, timeout=30):
    deadline=time.monotonic()+timeout
    quiet=0
    while time.monotonic()<deadline:
        app.processEvents()
        widgets=app.allWidgets()
        queues={q for w in widgets for q in w.findChildren(LatestTask)}
        timers=[getattr(w,name,None) for w in widgets
                for name in ('_integration_preview_timer',)]
        busy=any(q.busy for q in queues) or any(t is not None and t.isActive() for t in timers)
        quiet=0 if busy else quiet+1
        if quiet>=3:return
        time.sleep(.005)
    raise AssertionError('GUI background queues did not settle within timeout')
