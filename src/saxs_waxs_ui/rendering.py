"""Serialize Matplotlib rendering, including its process-wide rc settings."""
from contextlib import contextmanager
import threading


_render_lock = threading.RLock()


@contextmanager
def publication_render_context(style):
    """Keep style changes and drawing in one critical section across windows.

    Matplotlib's rc_context restores global state; overlapping contexts in
    separate threads otherwise restore each other's temporary settings.
    """
    import matplotlib as mpl
    # Never park the Qt event loop behind a long background image batch.
    acquired = _render_lock.acquire(blocking=threading.current_thread() is not threading.main_thread())
    if not acquired:
        raise ValueError("其他窗口正在导出图片，请稍后重试。")
    try:
        with mpl.rc_context(style):
            yield
    finally:
        _render_lock.release()
