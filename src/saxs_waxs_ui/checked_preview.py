"""Sequential, cancellable checked-frame previews with bounded detector memory."""
from dataclasses import replace
from pathlib import Path
from saxs_waxs_core.background import BackgroundMode
from saxs_waxs_core.processed_data import embedded_geometry
from saxs_waxs_core.readers.detector_io import (inspect_sources,_selections,
    filter_frame_selections,dependency_snapshot,verify_dependencies,read_frame,frame_label)
from .latest_task import integrate_previews


def integrate_checked(snapshot,paths,datasets,frames,workspace,background_workspace,
                      mask_workspace,progress,cancelled):
    progress('准备勾选样品清单')
    sources,catalogs,errors,_excluded=inspect_sources(paths)
    entries=[];failures=[]
    for path in sources:
        if cancelled():break
        try:
            if str(path) in errors:raise errors[str(path)]
            choices=_selections(catalogs[str(path)],datasets.get(str(path)))
            choices=filter_frame_selections(choices,frames.get(str(path)))
            dependencies=dependency_snapshot(catalogs[str(path)])
            stat=path.stat();identity=(stat.st_size,stat.st_mtime_ns)
            entries.extend((path,choice,dependencies,identity) for choice in choices)
        except Exception as exc:failures.append(f'{path.name}: {type(exc).__name__}: {exc}')
    curves=[];contexts=[]
    for index,(path,selection,dependencies,identity) in enumerate(entries,1):
        if cancelled():break
        try:
            progress(f'读取勾选帧 {index}/{len(entries)}')
            stat=path.stat()
            if identity!=(stat.st_size,stat.st_mtime_ns):raise ValueError('样品文件在预览过程中发生变化')
            verify_dependencies(dependencies)
            frame=read_frame(path,**selection)
            if cancelled():break
            # Instrument header values are candidates; explicit current geometry
            # governs raw checked samples. Processed inputs retain saved context.
            geometry=(embedded_geometry(frame) if frame.metadata.embedded_processing else None) or snapshot['geometry']
            background=snapshot['background_recipe']
            stored=frame.metadata.intensity_unit or frame.metadata.embedded_processing.get('intensity_unit')
            if stored is not None:background=replace(background,input_unit=stored)
            if frame.metadata.embedded_processing:
                mode=(BackgroundMode.UPSTREAM_SUBTRACTED if frame.metadata.embedded_processing.get('background_applied')
                    else BackgroundMode.NO_BACKGROUND)
                background=replace(background,mode=mode)
            job=dict(snapshot,frame=frame,geometry=geometry,background_recipe=background,name=frame_label(frame))
            progress(f'积分勾选帧 {index}/{len(entries)}')
            completed,errors=integrate_previews([job],[],workspace,background_workspace,mask_workspace)
            verify_dependencies(dependencies)
            after=path.stat()
            if identity!=(after.st_size,after.st_mtime_ns):raise ValueError('样品文件在积分过程中发生变化')
            failures.extend(errors)
            if completed:
                result=completed[0][1]
                curves.append((frame_label(frame),result.integration))
                contexts.append(dict(source=str(path),processing=dict(result.audit),selection='left_checked_frame'))
                # Do not retain detector arrays/MASKs for every checked frame.
                del result
            del completed,job,frame
        except Exception as exc:
            failures.append(f'{path.name} · 帧{selection.get("frame_index",0)+1:04d}: {type(exc).__name__}: {exc}')
    return dict(checked_preview=True,curves=curves,contexts=contexts,failures=failures,cancelled=cancelled())
