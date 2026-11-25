"""Explicit, offline frozen-runtime verification; never runs on normal startup."""
from pathlib import Path
import hashlib
import json
import os
import time


def main(directory):
    os.environ['QT_QPA_PLATFORM']='offscreen'
    from PySide6 import QtCore,QtWidgets
    import numpy as np
    import h5py
    import hdf5plugin
    from .app import MainWindow,create_application
    from .branding import TITLE,resource_path,artifacts_directory
    from saxs_waxs_core.geometry import DetectorGeometry,GeometryProvenance
    from saxs_waxs_core.processing import execute_processing_recipe
    from openpyxl import load_workbook
    root=Path(directory).resolve();root.mkdir(parents=True,exist_ok=False)
    app=create_application([])
    window=None
    report={'title':TITLE,'checks':[],'scientific_acceptance':False}
    def wait(condition):
        deadline=time.monotonic()+40
        while not condition() and time.monotonic()<deadline:
            app.processEvents();time.sleep(.005)
        assert condition(),'Background operation timed out'
    def check(name,condition=True):
        assert condition,name
        report['checks'].append(name)
    def shot(name):
        app.processEvents();window.grab().save(str(root/f'{name}.png'))
    def tab(label):
        for tabs in window.findChildren(QtWidgets.QTabWidget):
            for i in range(tabs.count()):
                if tabs.tabText(i)==label:tabs.setCurrentIndex(i)
        app.processEvents()
    try:
        from .runtime_diagnostics import install_ui_monitor,export_report
        install_ui_monitor(app)
        diagnostic=export_report(root/'support-report.zip')
        import zipfile
        with zipfile.ZipFile(diagnostic) as archive:
            check('runtime_monitor_and_diagnostic_export',hasattr(app,'_xrelay_runtime_monitor') and 'environment.json' in archive.namelist())
        inputs=root/'示例数据';inputs.mkdir()
        sample=inputs/'sample_master.h5';background=inputs/'BG.h5'
        y,x=np.indices((256,320));r=np.hypot(x-160,y-128)
        values=(20+600*np.exp(-((r-70)/5)**2)+200*np.exp(-((r-110)/8)**2)).astype('float32')
        with h5py.File(sample,'w') as f:
            f['entry/data/data']=np.stack([values+i for i in range(4)])
        with h5py.File(background,'w') as f:
            f.create_dataset('entry/data/data',data=np.full((2,256,320),5,dtype='float32'),**hdf5plugin.Bitshuffle(cname='zstd'))
        hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (sample,background)}
        window=MainWindow(settings=QtCore.QSettings(str(root/'settings.ini'),QtCore.QSettings.Format.IniFormat))
        window.resize(1440,960);window.show()
        window._set_preview_visible(True)
        window.browser.set_folder(inputs)
        window._file_selected(sample,{'dataset':'/entry/data/data','frame_index':0})
        window.browser.set_current_path(sample)
        check('branded_window',window.windowTitle()==TITLE and not window.windowIcon().isNull())
        check('resources',resource_path('calibrants/pyfai_reference_v1.json').is_file())
        import sys
        if getattr(sys,'frozen',False):
            from .agent_setup import connection_config
            config=connection_config(inputs,root/'agent-output')['mcpServers']['saxs-waxs-workbench']
            check('agent_launcher',Path(config['command']).name=='XRelay-Agent.exe'
                  and Path(config['command']).is_file())
        report['default_output_directory']=str(artifacts_directory())
        shot('01-import')
        geometry=DetectorGeometry(wavelength_m=1.54e-10,detector_distance_m=.2,
            pixel_size_x_m=75e-6,pixel_size_y_m=75e-6,beam_center_x_px=160,beam_center_y_px=128,
            image_width_px=320,image_height_px=256,provenance=GeometryProvenance(method='synthetic_release_check'))
        window.geometry_panel.set_geometry(geometry);window.geometry_panel.apply_button.click()
        tab('几何/标定');shot('02-geometry')
        window.background_panel.import_paths([background])
        window._preview_background(window.background_panel.mode,background)
        wait(lambda:window.active_pane.background_result is not None)
        tab('背景');shot('03-background')
        window.active_pane.viewer.set_manual_mask_descriptors(({'kind':'rectangle','pos':(10.,12.),'size':(22.,18.)},),emit=False)
        window._preview_mask(window.mask_panel.recipe());wait(lambda:window.active_pane.mask_result is not None)
        tab('MASK');shot('04-mask')
        window._preview_integration();wait(lambda:window.active_pane.integration_result is not None)
        tab('积分');shot('05-integration')
        window._file_selected(sample,{'dataset':'/entry/data/data','frame_index':2})
        wait(lambda:window.active_pane.integration_result is not None and window.active_pane.background_result is not None)
        pane=window.active_pane
        check('multiframe_follow',pane.frame.metadata.frame_index==2)
        np.testing.assert_array_equal(pane.viewer._data,pane.frame.data-5)
        check('processed_2d_follow')
        from saxs_waxs_core.readers.detector_io import read_frame
        ref=execute_processing_recipe(pane.frame,read_frame(background,**window.background_panel.source_selection),window._processing_recipe_for_active_pane())
        np.testing.assert_allclose(pane.integration_result.intensity,ref.integration.intensity,rtol=1e-12,equal_nan=True)
        check('preview_formal_equivalence')
        workbook=root/'积分数据.xlsx';window.integration_panel.export_displayed_data(workbook)
        book=load_workbook(workbook,read_only=True);check('workbook', '曲线数据' in book.sheetnames);book.close()
        window._open_peak_fit_workspace(window.integration_panel._curve_items,window.integration_panel._curve_parameter_contexts)
        fit=window._peak_fit_windows[-1].fit_panel
        wait(lambda:not fit._interactive_tasks.busy)
        fit.window().grab().save(str(root/'06-fit.png'))
        check('fit_workspace',bool(fit._curves))
        check('fit_computed',fit.current_result is not None)
        window.browser.select_paths((sample,))
        window._detector_frame_choices[str(sample.resolve())]=[0,2]
        from saxs_waxs_core.batch import run_batch_plan
        batch=run_batch_plan(window._batch_plan_for_current_selection(),root/'批处理结果')
        check('batch_selected_frames',batch.summary['passed']==2)
        import csv
        with next((root/'批处理结果').glob('all_samples_*.csv')).open(encoding='utf-8-sig') as stream:
            headers=next(csv.reader(stream))
        check('frame_headers',len(set(headers))==4 and any('帧0003' in name for name in headers))
        tree=window.browser.file_list
        row=next(i for i,s in enumerate(window.browser.files) if s.path==sample)
        window._register_browser_frames(sample,pane.frame)
        parent=tree.item(row);tree._expand(parent)
        parent.setCheckState(QtCore.Qt.CheckState.Unchecked)
        check('file_uncheck_clears_frames',not parent.frame_selection)
        parent.child(1).setCheckState(0,QtCore.Qt.CheckState.Checked)
        check('child_frame_enters_batch',len(window._batch_plan_for_current_selection().sample_sources)==1
              and window._detector_frame_choices[str(sample.resolve())]==[1])
        from .image_export_dialog import ImageExportDialog
        from .export_preview import enable_export_preview
        preview=ImageExportDialog(batch_available=True)
        prefs=QtCore.QSettings(str(root/'export-styles.ini'),QtCore.QSettings.Format.IniFormat)
        enable_export_preview(preview,lambda raw:(values,None,pane.viewer.preset,None),settings=prefs)
        preview.show()
        wait(lambda:not preview._export_preview_timer.isActive() and not preview._export_preview_tasks.busy)
        check('export_high_resolution_preview',preview.preview_canvas.item.pixmap().width()>2700)
        size=preview.size();viewport=preview.preview_canvas.size()
        preview.advanced_button.setChecked(True);app.processEvents()
        check('export_preview_stable_layout',preview.size()==size and preview.preview_canvas.size()==viewport)
        preview.grab().save(str(root/'08-export.png'));preview.reject();app.processEvents()
        from .geometry_panel import GeometryPanel
        from saxs_waxs_core.geometry import geometry_to_document
        gprefs=QtCore.QSettings(str(root/'geometry-library.ini'),QtCore.QSettings.Format.IniFormat)
        gprefs.setValue('parameters',json.dumps({'参数1':geometry_to_document(window.current_geometry)}))
        gp=GeometryPanel(library_settings=gprefs);applied=[];gp.apply_requested.connect(applied.append)
        next(b for b in gp.findChildren(QtWidgets.QPushButton) if b.text()=='套用').click()
        check('geometry_library_load_without_apply',gp.geometry()==window.current_geometry and not applied)
        gp.resize(480,850);gp.show();app.processEvents();gp.grab().save(str(root/'09-geometry-library.png'));gp.close()
        from .export_naming import validate_name
        validate_name('LFY 一维')
        check('export_naming_module')
        from saxs_waxs_core.image_export_batch import RawImageExportPlan,run_image_export_batch
        from saxs_waxs_core.processing import FileIdentity
        from saxs_waxs_core.presets import ExportPreset
        probe=inputs/'invalid-pixels.h5';probe_data=np.full((64,80),10,dtype='uint32');probe_data[:4]=np.iinfo('uint32').max
        with h5py.File(probe,'w') as f:
            f['entry/data/data']=probe_data
            detector=f.create_group('entry/instrument/detector');detector.attrs['NX_class']='NXdetector'
            detector['pixel_mask']=(probe_data==np.iinfo('uint32').max).astype('uint32')
        exported=run_image_export_batch(RawImageExportPlan((FileIdentity.from_path(probe),)),root/'原图导出',pane.viewer.preset,ExportPreset(image_dpi=80))
        record=json.loads(exported.manifest_path.read_text(encoding='utf-8'))
        check('raw_2d_invalid_pixel_limits',record['items'][0]['resolved_limits']['vmax']<1000)
        tab('配方');shot('07-batch')
        for p,digest in hashes.items():check('source_hash_'+p.name,hashlib.sha256(p.read_bytes()).hexdigest()==digest)
        check('manual',resource_path('manual/index.html').is_file())
        checked_folder=root/'勾选预览示例';checked_folder.mkdir()
        checked_paths=[]
        for index in range(12):
            path=checked_folder/f'样品{index+1:02d}.h5'
            with h5py.File(path,'w') as f:f['entry/data/data']=values+index
            checked_paths.append(path)
        pane_count=len(window.panes)
        window.browser.set_folder(checked_folder)
        window.browser.select_paths(tuple(checked_paths))
        window.integration_panel.curve_scope_combo.setCurrentIndex(window.integration_panel.curve_scope_combo.findData('checked'))
        window._integration_preview_timer.stop();window._preview_integration()
        wait(lambda:not window._integration_tasks.busy and len(window.integration_panel._available_curves)==12)
        check('checked_curves_without_extra_2d_panes',len(window.panes)==pane_count)
        tab('积分');shot('10-checked-curves')
        window.integration_panel._choose_visible_curves()
        chooser=window.integration_panel._curve_selection_dialog
        chooser.items.item(0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        check('curve_visibility_filters_without_new_calculation',len(window.integration_panel._curve_items)==11)
        app.processEvents();chooser.grab().save(str(root/'11-curve-selection.png'));chooser.close()
        report['status']='passed'
    except Exception as exc:
        import traceback
        report['status']='failed';report['error']=str(exc);report['traceback']=traceback.format_exc()
    finally:
        if window is not None:
            window._integration_preview_timer.stop()
            for q in (window._integration_tasks,window._background_tasks,window._mask_tasks,window._overlay_tasks):
                q.invalidate();q.pool.waitForDone(40000)
            for pane in window.panes:
                pane.frame_player.pause();pane.frame_player.pool.waitForDone(40000)
            for fitwindow in window._peak_fit_windows:
                fitwindow.fit_panel._interactive_tasks.invalidate();fitwindow.fit_panel._interactive_tasks.pool.waitForDone(40000)
            app.processEvents();window.close();app.processEvents()
        (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    import sys
    raise SystemExit(main(sys.argv[1]))
