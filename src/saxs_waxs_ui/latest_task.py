"""Single-worker, bounded latest-request queue for expensive UI computations."""
from PySide6 import QtCore


class PreviewBackgroundWorkspace:
    """One bounded background cache, used only by the serialized preview worker."""

    def __init__(self):
        self.key = None
        self.air = None
        self.dependencies = ()
        self.sample = None
        self.recipe = None
        self.result = None

    def apply(self, sample, recipe, path=None, selection=None):
        from pathlib import Path
        from saxs_waxs_core.background import apply_background, BackgroundMode, BackgroundError
        from saxs_waxs_core.readers.detector_io import read_frame, source_catalog, dependency_snapshot

        selection = dict(selection or {})
        key = None
        if recipe.mode is BackgroundMode.AUTO_AIR:
            if path is None:
                raise BackgroundError("请选择背景文件，不能回退为原图积分")
            path = Path(path).resolve()
            if path == Path(sample.metadata.source_path).resolve():
                raise BackgroundError("背景不能与当前样品是同一文件")
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns, tuple(sorted(selection.items())))
            dependencies = (tuple((p, Path(p).stat().st_size, Path(p).stat().st_mtime_ns)
                                  for p, _, _ in self.dependencies) if key == self.key else ())
            if key != self.key or dependencies != self.dependencies:
                # Include external HDF5 storage files in cache invalidation.
                snapshots = dependency_snapshot(source_catalog(path))
                self.air = read_frame(path, **selection)
                self.dependencies = tuple((s['path'], s['size_bytes'], s['mtime_ns']) for s in snapshots)
                self.key = key
                self.sample = None
        else:
            self.air = None
            self.key = None
            self.dependencies = ()
        if self.sample is sample and self.recipe == recipe and self.result is not None:
            return self.result
        result = apply_background(sample, self.air, recipe)
        self.sample, self.recipe, self.result = sample, recipe, result
        return result


class PreviewMaskWorkspace:
    """Keep only the last rasterized user MASK; all IO stays in the worker."""
    def __init__(self):
        self.key = None
        self.layers = ()

    def recipe(self, job):
        from dataclasses import replace
        import json
        from pathlib import Path
        from saxs_waxs_core.masking import user_mask_layers_from_descriptors, load_external_mask_layer
        if 'manual_descriptors' not in job:
            return job['mask']
        shape = job['frame'].data.shape
        path = job.get('external_mask_path')
        identity = None
        if path is not None:
            path = Path(path).resolve()
            stat = path.stat()
            identity = (str(path), stat.st_size, stat.st_mtime_ns)
        key = (shape, json.dumps(job['manual_descriptors'], sort_keys=True), identity)
        if key != self.key:
            layers = list(user_mask_layers_from_descriptors(shape, job['manual_descriptors']))
            if path is not None:
                layers.insert(0, load_external_mask_layer(path, shape))
            self.layers, self.key = tuple(layers), key
        return replace(job['mask'], user_layers=self.layers)


def integrate_previews(jobs, failures, workspace=None, background_workspace=None,
                       mask_workspace=None, progress=None):
    from dataclasses import replace
    from saxs_waxs_core.processing import execute_integration_stage
    completed=[]
    errors=list(failures)
    background_workspace = background_workspace or PreviewBackgroundWorkspace()
    mask_workspace = mask_workspace or PreviewMaskWorkspace()
    for number, job in enumerate(jobs, 1):
        def stage(text):
            if progress is not None: progress(f'{text} {number}/{len(jobs)}')
        try:
            stage('背景处理')
            background=background_workspace.apply(job["frame"],job["background_recipe"],
                job.get("background_path"),job.get("background_selection"))
            stage('MASK准备')
            mask_recipe = mask_workspace.recipe(job)
            integration_recipe=replace(job["recipe"],input_unit=background.output_unit)
            stage('数值积分')
            result=execute_integration_stage(job["frame"],job["geometry"],background,
                mask_recipe,integration_recipe,workspace=workspace)
            completed.append((job,result))
        except Exception as exc:
            errors.append(f"{job['name']}: {type(exc).__name__}: {exc}")
    return completed,errors


def prepare_navigation_previews(jobs, workspace):
    from dataclasses import replace
    from saxs_waxs_core.masking import build_mask,user_mask_layers_from_descriptors,load_external_mask_layer
    from saxs_waxs_core.display import normalize_for_display
    results=[];errors=[]
    for job in jobs:
        try:
            background=(workspace.apply(job['frame'],job['background_recipe'],job['background_path'],
                        job['background_selection']) if job['background_enabled'] else None)
            mask=None
            if job['mask_enabled']:
                layers=list(user_mask_layers_from_descriptors(job['frame'].data.shape,job['manual_descriptors']))
                if job['external_path'] is not None:
                    layers.insert(0,load_external_mask_layer(job['external_path'],job['frame'].data.shape))
                mask=build_mask(job['frame'],replace(job['mask_recipe'],user_layers=tuple(layers)))
            data=background.data if background is not None else job['frame'].data
            excluded=mask.excluded if mask is not None else job['frame'].metadata.detector_mask
            normalized=normalize_for_display(data,job['preset'],excluded)
            results.append((job,background,mask,normalized))
        except Exception as exc:
            errors.append(f"{job['frame'].metadata.source_path.name}: {exc}")
    return {'navigation':True,'results':results,'errors':errors}


class _Signals(QtCore.QObject):
    done=QtCore.Signal(object,object,object)
    progress=QtCore.Signal(object,object)


class _Task(QtCore.QRunnable):
    def __init__(self,version,work,with_progress=False,name='preview'):
        super().__init__()
        self.version=version
        self.work=work
        self.with_progress=with_progress
        self.name=name
        self.signals=_Signals()

    def run(self):
        import time
        from .runtime_diagnostics import record,_stack
        import sys
        started=time.monotonic()
        record('task_started',operation=self.name,version=self.version)
        result,error=None,None
        try:
            result=(self.work(lambda message:self.signals.progress.emit(self,message))
                    if self.with_progress else self.work())
        except Exception as exc:
            error=f"{type(exc).__name__}: {exc}"
            record('task_failed',operation=self.name,type=type(exc).__name__,stack=_stack(sys._getframe()))
        record('task_finished',operation=self.name,version=self.version,
            elapsed_seconds=round(time.monotonic()-started,3),failed=error is not None)
        self.signals.done.emit(self,result,error)


class LatestTask(QtCore.QObject):
    ready=QtCore.Signal(object)
    failed=QtCore.Signal(str)
    progress=QtCore.Signal(object)
    idle=QtCore.Signal()

    def __init__(self,parent=None):
        super().__init__(parent)
        self.pool=QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.version=0
        self.active=None
        self.pending=None

    @property
    def busy(self):
        return self.active is not None

    def invalidate(self):
        self.version+=1
        self.pending=None

    def submit(self,work,*,with_progress=False):
        self.invalidate()
        self.pending=(self.version,work,with_progress)
        if not self.busy:
            self._start()

    def _start(self):
        version,work,with_progress=self.pending
        self.pending=None
        name=self.objectName() or (type(self.parent()).__name__ if self.parent() else 'preview')
        task=_Task(version,work,with_progress,name)
        task.signals.done.connect(self._done)
        task.signals.progress.connect(self._progress)
        self.active=task
        self.pool.start(task)

    @QtCore.Slot(object,object)
    def _progress(self,task,message):
        if task.version==self.version:
            self.progress.emit(message)

    @QtCore.Slot(object,object,object)
    def _done(self,task,result,error):
        self.active=None
        task.signals.deleteLater()
        if task.version==self.version:
            if error is None:
                self.ready.emit(result)
            else:
                self.failed.emit(error)
        else:
            from .runtime_diagnostics import record
            record('task_superseded',operation=task.name,version=task.version,current_version=self.version)
        if self.pending is not None and not self.busy:
            self._start()
        if not self.busy:
            self.idle.emit()
