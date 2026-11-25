import os
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from pathlib import Path
import tempfile
import threading
import time
import unittest
import h5py
import numpy as np
from PySide6 import QtCore,QtWidgets
from saxs_waxs_ui.latest_task import LatestTask
from saxs_waxs_ui.latest_task import PreviewMaskWorkspace
from saxs_waxs_ui.radial_units import convert_radial_values
from saxs_waxs_ui.app import MainWindow
from saxs_waxs_core.geometry import DetectorGeometry,GeometryProvenance


class InteractiveTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def wait(self,condition):
        deadline=time.monotonic()+15
        while not condition() and time.monotonic()<deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.assertTrue(condition())

    def test_latest_queue_is_bounded_and_ui_remains_responsive(self):
        queue=LatestTask()
        entered,release=threading.Event(),threading.Event()
        def slow():
            entered.set()
            release.wait(3)
            return "old"
        shown=[]
        queue.ready.connect(shown.append)
        queue.submit(slow)
        self.wait(entered.is_set)
        heartbeats=[]
        QtCore.QTimer.singleShot(0,lambda:heartbeats.append(1))
        queue.submit(lambda:"discard")
        queue.submit(lambda:"latest")
        self.wait(lambda:bool(heartbeats))
        release.set()
        self.wait(lambda:shown==["latest"])
        self.assertFalse(queue.busy)
        queue.deleteLater()

    def test_radial_unit_roundtrip_preserves_physical_boundaries(self):
        for unit in ("q_nm^-1","q_A^-1","s_nm^-1","2th_deg"):
            converted=convert_radial_values([.1,1.2],"q_nm^-1",unit,1.03e-10)
            restored=convert_radial_values(converted,unit,"q_nm^-1",1.03e-10)
            np.testing.assert_allclose(restored,[.1,1.2],rtol=1e-14)

    def test_stale_worker_progress_cannot_replace_latest_status_and_idle_is_reported(self):
        queue=LatestTask()
        entered,release=threading.Event(),threading.Event()
        stages=[];results=[];idle=[]
        queue.progress.connect(stages.append);queue.ready.connect(results.append)
        queue.idle.connect(lambda:idle.append(True))
        def old(progress):
            entered.set();release.wait(3);progress('obsolete');return 'old'
        queue.submit(old,with_progress=True)
        self.wait(entered.is_set)
        queue.submit(lambda progress:(progress('current'),'new')[1],with_progress=True)
        release.set()
        self.wait(lambda:bool(idle))
        self.assertEqual(stages,['current'])
        self.assertEqual(results,['new'])

    def test_mask_cache_invalidates_on_descriptor_shape_and_external_file_changes(self):
        from types import SimpleNamespace
        from saxs_waxs_core.masking import MaskRecipe,user_mask_layers_from_descriptors
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'artifacts') as tmp:
            path=Path(tmp)/'mask.npy';np.save(path,np.zeros((64,80),dtype='uint8'))
            job=dict(frame=SimpleNamespace(data=np.zeros((64,80))),mask=MaskRecipe(),
                manual_descriptors=[dict(kind='rectangle',pos=(2.,3.),size=(4.,5.))],external_mask_path=path)
            cache=PreviewMaskWorkspace()
            first=cache.recipe(job)
            again=cache.recipe(job)
            self.assertIs(first.user_layers[1],again.user_layers[1])
            expected=user_mask_layers_from_descriptors((64,80),job['manual_descriptors'])
            np.testing.assert_array_equal(first.user_layers[1].mask,expected[0].mask)
            changed=np.ones((64,80),dtype='uint8');np.save(path,changed)
            stat=path.stat();os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000000))
            second=cache.recipe(job)
            self.assertTrue(second.user_layers[0].mask.all())
            job['manual_descriptors']=[dict(kind='rectangle',pos=(12.,3.),size=(4.,5.))]
            third=cache.recipe(job)
            self.assertFalse(np.array_equal(second.user_layers[1].mask,third.user_layers[1].mask))
            job.update(external_mask_path=None,frame=SimpleNamespace(data=np.zeros((72,96))))
            self.assertEqual(cache.recipe(job).user_layers[0].mask.shape,(72,96))

    def test_preview_mask_rasterization_runs_in_worker_and_matches_formal_path(self):
        from unittest.mock import patch
        from saxs_waxs_core.background import BackgroundMode
        from saxs_waxs_core.masking import user_mask_layers_from_descriptors
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'artifacts') as tmp:
            path=Path(tmp)/'sample.h5'
            with h5py.File(path,'w') as f:f['entry/data/data']=np.arange(64*80,dtype='uint32').reshape(64,80)%200
            window=MainWindow(settings=QtCore.QSettings(str(Path(tmp)/'test.ini'),QtCore.QSettings.Format.IniFormat))
            try:
                window._file_selected(path)
                g=DetectorGeometry(wavelength_m=1.03e-10,detector_distance_m=.4,
                    pixel_size_x_m=75e-6,pixel_size_y_m=75e-6,beam_center_x_px=40,beam_center_y_px=32,
                    image_width_px=80,image_height_px=64)
                window.geometry_panel.set_geometry(g);window.geometry_panel.apply_button.click()
                window.background_panel.mode_combo.setCurrentIndex(window.background_panel.mode_combo.findData(BackgroundMode.NO_BACKGROUND.value))
                window.active_pane.viewer.add_manual_mask_region('rectangle')
                descriptors=window.active_pane.viewer.manual_mask_descriptors()
                threads=[]
                def raster(*args):
                    threads.append(threading.get_ident())
                    return user_mask_layers_from_descriptors(*args)
                with patch('saxs_waxs_core.masking.user_mask_layers_from_descriptors',side_effect=raster):
                    window._preview_integration()
                    self.wait(lambda:window.active_pane.integration_result is not None)
                self.assertTrue(threads)
                self.assertNotIn(threading.get_ident(),threads)
                actual=window.active_pane.mask_result
                layers=user_mask_layers_from_descriptors((64,80),descriptors)
                self.assertTrue(np.any(actual.excluded & layers[0].mask))
                self.assertTrue(np.all(actual.excluded[layers[0].mask]))
                from saxs_waxs_core.masking import build_mask
                from saxs_waxs_core.integration import integrate_radial_1d
                formal_mask=build_mask(window.active_pane.frame,window.mask_panel.recipe(layers))
                curve=window.active_pane.integration_result
                expected=integrate_radial_1d(window.active_pane.frame.data,g,
                    window.integration_panel.radial_recipe(curve.intensity_unit,window.active_pane.integration_region),
                    mask=formal_mask.excluded)
                np.testing.assert_array_equal(actual.excluded,formal_mask.excluded)
                np.testing.assert_allclose(curve.intensity,expected.intensity,rtol=1e-12,atol=1e-12,equal_nan=True)
                np.testing.assert_array_equal(curve.count,expected.count)
            finally:
                window._integration_preview_timer.stop()
                window._integration_tasks.invalidate();window._integration_tasks.pool.waitForDone(15000)
                self.app.processEvents();window.close();self.app.processEvents()

    def test_numeric_typing_commits_once_and_tilted_readout_avoids_full_detector_map(self):
        from dataclasses import replace
        from unittest.mock import patch
        from PySide6 import QtTest
        from saxs_waxs_ui.integration_panel import IntegrationPanel
        from saxs_waxs_ui.app import ImageViewerWidget
        from saxs_waxs_core.integration import detector_radial_array
        panel=IntegrationPanel()
        panel.sector_combo.setCurrentIndex(panel.sector_combo.findData('single'))
        values=[]
        panel.sector_center.valueChanged.connect(values.append)
        spin=panel.sector_center
        spin.lineEdit().selectAll()
        QtTest.QTest.keyClicks(spin.lineEdit(),'268.95')
        self.assertEqual(values,[])
        QtTest.QTest.keyClick(spin,QtCore.Qt.Key.Key_Return)
        self.assertEqual(values,[268.95])
        viewer=ImageViewerWidget()
        viewer.set_data(np.zeros((64,80)))
        g=DetectorGeometry(wavelength_m=1.03e-10,detector_distance_m=.4,
            pixel_size_x_m=75e-6,pixel_size_y_m=75e-6,beam_center_x_px=40,beam_center_y_px=32,
            image_width_px=80,image_height_px=64,rot1_rad=.02,rot2_rad=-.01)
        viewer.set_inspection_geometry(g)
        for unit in ('q_nm^-1','q_A^-1','s_nm^-1','2th_deg'):
            expected=detector_radial_array(g,(64,80),unit)[21,32]
            with patch('saxs_waxs_ui.app.detector_radial_array',side_effect=AssertionError('full map')):
                actual=viewer._inspection_radial_value(21,32,unit)
            self.assertAlmostEqual(actual,float(expected),places=9)
        panel.close();viewer.close()

    def test_invalidated_job_cannot_publish_and_failed_job_can_recover(self):
        queue=LatestTask()
        entered,release=threading.Event(),threading.Event()
        shown,errors=[],[]
        queue.ready.connect(shown.append)
        queue.failed.connect(errors.append)
        def old():
            entered.set()
            release.wait(3)
            return "stale"
        queue.submit(old)
        self.wait(entered.is_set)
        queue.invalidate()
        release.set()
        self.wait(lambda:not queue.busy)
        self.assertEqual(shown,[])
        queue.submit(lambda:1/0)
        self.wait(lambda:bool(errors))
        queue.submit(lambda:"recovered")
        self.wait(lambda:shown==["recovered"])
        queue.deleteLater()

    def test_tree_frame_comparison_range_batch_and_async_integrations(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/"artifacts/test_tmp") as tmp:
            path=Path(tmp)/"stack.h5"
            with h5py.File(path,"w") as f:
                f["entry/data/data"]=np.arange(40*64*80,dtype="uint32").reshape(40,64,80)%1000+1
            window=MainWindow(settings=QtCore.QSettings(str(Path(tmp)/"settings.ini"),QtCore.QSettings.Format.IniFormat))
            try:
                window.browser.set_folder(tmp)
                geometry=DetectorGeometry(wavelength_m=1.03e-10,detector_distance_m=.4,
                    pixel_size_x_m=75e-6,pixel_size_y_m=75e-6,beam_center_x_px=40,beam_center_y_px=32,
                    image_width_px=80,image_height_px=64,provenance=GeometryProvenance(method="test"))
                window.geometry_panel.set_geometry(geometry)
                window.geometry_panel.apply_button.click()
                tree=window.browser.file_list
                parent=tree.item(0)
                parent.setExpanded(True)
                self.assertEqual(parent.childCount(),40)
                tree.clearSelection()
                tree.setCurrentItem(parent.child(19))
                self.wait(lambda:window.active_pane.frame.metadata.frame_index==19)
                self.assertNotIn(str(path.resolve()),window._detector_frame_choices)
                for i in range(40):
                    parent.child(i).setCheckState(0,QtCore.Qt.CheckState.Checked if 7<=i<=31 else QtCore.Qt.CheckState.Unchecked)
                self.assertEqual(window._detector_frame_choices[str(path.resolve())],list(range(7,32)))
                self.assertIn("已选25帧",parent.text(0))
                from saxs_waxs_core.background import BackgroundMode
                window.background_panel.mode_combo.setCurrentIndex(window.background_panel.mode_combo.findData(BackgroundMode.NO_BACKGROUND.value))
                plan=window._batch_plan_for_current_selection()
                self.assertEqual(len(plan.sample_sources),25)
                tree.clearSelection()
                parent.child(7).setSelected(True)
                parent.child(23).setSelected(True)
                window._compare_browser_frames()
                self.wait(lambda:len(window.panes)==3 and [p.frame.metadata.frame_index for p in window.panes[1:]]==[7,23])
                window._preview_integration()
                self.wait(lambda:window.active_pane.integration_result is not None)
                self.assertEqual(window.active_pane.integration_result.kind,"radial_1d")
                overlay=window.active_pane.viewer.integration_overlay
                overlay._drag_state(True)
                window._schedule_integration_preview()
                self.assertFalse(window._integration_preview_timer.isActive())
                overlay._drag_state(False)
                self.assertTrue(window._integration_preview_timer.isActive())
                self.assertEqual(window._integration_preview_timer.interval(),0)
                window._integration_preview_timer.stop()
                from saxs_waxs_ui.peak_fit_panel import PeakFitPanel
                fit_panel=PeakFitPanel()
                fit_panel._background_fitting=True
                fit_panel.auto_check.setChecked(False)
                fit_panel.set_curves([("frame24",window.active_pane.integration_result)])
                self.wait(lambda:not fit_panel._interactive_tasks.busy)
                self.assertIsNotNone(fit_panel.current_result)
                fit_panel._interactive_tasks.invalidate()
                fit_panel.close()
                window.integration_panel.mode_combo.setCurrentIndex(1)
                self.wait(lambda:window.active_pane.integration_result is not None and window.active_pane.integration_result.kind=="azimuthal_1d")
                before=window.active_pane.integration_region.copy()
                combo=window.integration_panel.unit_combo
                combo.setCurrentIndex(combo.findData("2th_deg"))
                after=window.active_pane.integration_region.copy()
                expected=convert_radial_values([before["radial_lower"],before["radial_upper"]],before["radial_unit"],"2th_deg",geometry.wavelength_m)
                np.testing.assert_allclose([after["radial_lower"],after["radial_upper"]],expected)
                frame=window.active_pane.frame
                window._preview_background(BackgroundMode.NO_BACKGROUND,None)
                self.wait(lambda:window.active_pane.background_result is not None)
                np.testing.assert_array_equal(window.active_pane.background_result.data,frame.data)
                air_path=Path(tmp)/"air.h5"
                with h5py.File(air_path,"w") as f:
                    f["entry/data/data"]=np.ones((64,80),dtype="uint32")
                previous=window.active_pane.background_result
                window.background_panel.mode_combo.setCurrentIndex(window.background_panel.mode_combo.findData(BackgroundMode.AUTO_AIR.value))
                window._preview_background(BackgroundMode.AUTO_AIR,air_path)
                self.wait(lambda:window.active_pane.background_result is not previous)
                np.testing.assert_array_equal(window.active_pane.background_result.data,frame.data.astype(float)-1)
                window._preview_mask(window.mask_panel.recipe(()))
                self.wait(lambda:window.active_pane.mask_result is not None)
                self.assertEqual(window.active_pane.mask_result.excluded.shape,frame.data.shape)
                # Switching frame/sample must apply the recipe again to the new
                # raw pixels, including manual and external masks (not old buffers).
                window.background_panel.set_configuration(BackgroundMode.AUTO_AIR,air_path)
                descriptors=({'kind':'rectangle','pos':(5.,6.),'size':(9.,8.)},)
                window.active_pane.viewer.set_manual_mask_descriptors(descriptors,emit=False)
                from PIL import Image
                external_path=Path(tmp)/'external_mask.tif'
                external=np.zeros((64,80),dtype='uint8'); external[20:23,30:34]=1
                Image.fromarray(external).save(external_path)
                window.mask_panel.set_external_mask_path(external_path,emit=False)
                other_path=Path(tmp)/'other.h5'
                with h5py.File(other_path,'w') as f:
                    f['entry/data/data']=np.full((3,64,80),100,dtype='uint32')
                from saxs_waxs_core.processing import execute_processing_recipe
                from saxs_waxs_core.readers.detector_io import read_frame,frame_label
                for source,index in ((path,26),(other_path,1)):
                    window._file_selected(source,{'dataset':'/entry/data/data','frame_index':index})
                    pane=window.active_pane
                    self.assertIsNone(pane.background_result)
                    self.assertEqual(pane.viewer.manual_mask_region_count,1)
                    for mode in (0,1):
                        window.integration_panel.mode_combo.setCurrentIndex(mode)
                        window._integration_preview_timer.stop()
                        pane.integration_result=None
                        window._preview_integration()
                        self.wait(lambda:pane.integration_result is not None)
                        reference=execute_processing_recipe(pane.frame,read_frame(air_path),
                            window._processing_recipe_for_active_pane())
                        np.testing.assert_allclose(pane.integration_result.intensity,
                            reference.integration.intensity,rtol=1e-12,atol=0,equal_nan=True)
                        np.testing.assert_array_equal(pane.mask_result.excluded,reference.mask.excluded)
                        self.assertTrue(pane.mask_result.excluded[20:23,30:34].all())
                        label=frame_label(pane.frame)
                        self.assertIn(f'帧{index+1:04d}',label)
                        self.assertIn(label,[name for name,_ in window.integration_panel._curve_items])
                from openpyxl import load_workbook
                workbook=Path(tmp)/'frame_preview.xlsx'
                window.integration_panel.export_displayed_data(workbook)
                book=load_workbook(workbook,read_only=True)
                try:
                    headers=next(book['曲线数据'].values)
                    self.assertTrue(any(label in str(value) for value in headers))
                    summary=list(book['参数汇总'].values)
                    self.assertTrue(any(label in str(value) for row in summary for value in row))
                finally:
                    book.close()
            finally:
                window._integration_preview_timer.stop()
                queues=[window._integration_tasks,window._overlay_tasks,window._background_tasks,window._mask_tasks]
                for queue in queues:
                    queue.invalidate()
                    queue.pool.waitForDone(15000)
                for pane in window.panes:
                    pane.frame_player.pause()
                    pane.frame_player.pool.waitForDone(15000)
                self.app.processEvents()
                window.close()
                self.app.processEvents()
