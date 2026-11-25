"""Asynchronous latest-request frame navigation with two prepared-frame buffers."""
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6 import QtCore

from saxs_waxs_core.display import normalize_for_display
from saxs_waxs_core.readers.detector_io import (
    dependency_snapshot, source_catalog, verify_dependencies,
)
from saxs_waxs_core.readers.tvx_tiff import read_tvx_tiff


@dataclass(frozen=True)
class FramePreview:
    frame: object
    normalized: object
    preset: object


class _ReadSignals(QtCore.QObject):
    finished = QtCore.Signal(object, object, object)


class _ReadFrame(QtCore.QRunnable):
    def __init__(self, source, index, dependencies, loader):
        super().__init__()
        self.source = source
        self.index = index
        self.dependencies = dependencies
        self.loader = loader
        self.signals = _ReadSignals()

    def run(self):
        preview, error = None, None
        try:
            path, dataset, _, preset, _ = self.source
            selection = {"frame_index": self.index}
            if dataset is not None:
                selection["dataset"] = dataset
            verify_dependencies(self.dependencies)
            frame = self.loader(path, **selection)
            normalized = normalize_for_display(frame.data, preset, frame.metadata.detector_mask)
            verify_dependencies(self.dependencies)
            preview = FramePreview(frame, normalized, preset)
        except Exception as exc:
            error = str(exc)
        self.signals.finished.emit(self, preview, error)


class FramePlayer(QtCore.QObject):
    ready = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    playing_changed = QtCore.Signal(bool)

    def __init__(self, parent=None, *, loader=read_tvx_tiff):
        super().__init__(parent)
        self.loader = loader
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._advance)
        # Advance as soon as the event loop can present another frame. The single
        # reader and bounded prefetch provide backpressure instead of a fixed fps.
        self.interval_ms = 1
        self.playing = False
        self.current_index = 0
        self.source = None
        self.dependencies = []
        self.cache = OrderedDict()
        self._task = None
        self._wanted = None

    @property
    def busy(self):
        return self._task is not None

    @property
    def pending_index(self):
        return self._wanted

    def configure(self, frame, preset, normalized=None):
        metadata = frame.metadata
        if metadata.frame_count < 2:
            self.pause()
            self.source = None
            self.cache.clear()
            return
        path = Path(metadata.source_path).resolve()
        dependencies = dependency_snapshot(source_catalog(path))
        if not any(item["path"]==str(path) for item in dependencies):
            stat=path.stat()
            dependencies.append(dict(path=str(path),size_bytes=stat.st_size,mtime_ns=stat.st_mtime_ns))
        identity = tuple((item["path"], item["size_bytes"], item["mtime_ns"])
                         for item in dependencies)
        source = (path, metadata.dataset_path, metadata.frame_count, preset, identity)
        if source != self.source:
            self.pause()
            self.cache.clear()
            self.source = source
            self.dependencies = dependencies
        self.current_index = metadata.frame_index
        if normalized is not None:
            self.cache[self.current_index] = FramePreview(frame,normalized,preset)
            self.cache.move_to_end(self.current_index)
            while len(self.cache)>2:
                self.cache.popitem(last=False)

    def play(self):
        if self.source is None or self.source[2] < 2:
            return
        self.playing = True
        self.playing_changed.emit(True)
        self._request(0 if self.current_index == self.source[2]-1 else self.current_index+1)

    def pause(self):
        self.timer.stop()
        self._wanted = None
        if self.playing:
            self.playing = False
            self.playing_changed.emit(False)

    def jump(self, index):
        self.pause()
        self._request(index)

    def _advance(self):
        if self.playing:
            self._request(self.current_index+1)

    def _request(self, index):
        if self.source is None or not 0 <= index < self.source[2]:
            return
        try:
            verify_dependencies(self.dependencies)
        except Exception as exc:
            self.pause()
            self.cache.clear()
            self.failed.emit(str(exc))
            return
        self._wanted = index
        if index in self.cache:
            self._show(self.cache[index])
        elif not self.busy:
            self._read(index)

    def _read(self, index):
        task = _ReadFrame(self.source, index, self.dependencies, self.loader)
        task.signals.finished.connect(self._received)
        self._task = task
        self.pool.start(task)

    def _show(self, preview):
        self._wanted = None
        self.current_index = preview.frame.metadata.frame_index
        # Prepare the next frame while the UI presents this one, rather than
        # starting disk/normalization work only after presentation has returned.
        following = self.current_index+1
        if not self.busy and following < self.source[2] and following not in self.cache:
            self._read(following)
        self.ready.emit(preview)
        if self.playing:
            if self.current_index == self.source[2]-1:
                self.pause()
            else:
                self.timer.start(self.interval_ms)

    @QtCore.Slot(object, object, object)
    def _received(self, task, preview, error):
        self._task = None
        task.signals.deleteLater()
        if task.source == self.source:
            if error is not None and self._wanted == task.index:
                self.pause()
                self.failed.emit(f"帧 {task.index+1}：{error}")
            elif preview is not None:
                self.cache[task.index] = preview
                self.cache.move_to_end(task.index)
                while len(self.cache) > 2:
                    self.cache.popitem(last=False)
        if self._wanted is not None:
            if self._wanted in self.cache:
                self._show(self.cache[self._wanted])
            else:
                self._read(self._wanted)
