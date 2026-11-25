import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import csv,hashlib,threading,time,tempfile,unittest,weakref
from pathlib import Path
from unittest.mock import patch
import h5py
import numpy as np
from PySide6 import QtCore,QtWidgets
from saxs_waxs_ui.app import MainWindow
from saxs_waxs_core.geometry import DetectorGeometry
from saxs_waxs_ui.checked_preview import integrate_checked
from saxs_waxs_ui.latest_task import PreviewBackgroundWorkspace,PreviewMaskWorkspace
from saxs_waxs_core.integration import IntegrationWorkspace


class CheckedCurvePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'artifacts')
        self.root=Path(self.tmp.name)
        self.paths=[]
        for index in range(12):
            path=self.root/f'sample-{index:02d}.h5';self.paths.append(path)
            with h5py.File(path,'w') as f:
                values=np.stack([np.full((64,80),12+index+frame,dtype='uint32') for frame in range(3)])
                values[:,3:7,3:7]=9000
                f['entry/data/data']=values
        self.background=self.root/'background.h5'
        with h5py.File(self.background,'w') as f:f['entry/data/data']=np.full((64,80),2,dtype='uint32')
        self.hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in [*self.paths,self.background]}
        self.w=MainWindow(settings=QtCore.QSettings(str(self.root/'settings.ini'),QtCore.QSettings.Format.IniFormat))
        self.w.browser.set_folder(self.root)
        self.w._file_selected(self.paths[0],dict(dataset='/entry/data/data',frame_index=0))
        self.g=DetectorGeometry(wavelength_m=1.03e-10,detector_distance_m=.4,
            pixel_size_x_m=75e-6,pixel_size_y_m=75e-6,beam_center_x_px=40,beam_center_y_px=32,
            image_width_px=80,image_height_px=64)
        self.w.geometry_panel.set_geometry(self.g);self.w.geometry_panel.apply_button.click()
        self.w.background_panel.import_paths([self.background])
        self.w.active_pane.viewer.set_manual_mask_descriptors(
            [dict(kind='rectangle',pos=(0.,0.),size=(10.,10.))],emit=False)
        self.w.browser.select_paths(tuple(self.paths))
        self.w._detector_frame_choices={str(p.resolve()):(1,) for p in self.paths}
        self.w.integration_panel.radial_npt.setValue(64)

    def wait(self,condition):
        deadline=time.monotonic()+25
        while not condition() and time.monotonic()<deadline:self.app.processEvents();time.sleep(.003)
        self.assertTrue(condition(),self.w.integration_panel.status_label.text())

    def tearDown(self):
        self.w._integration_preview_timer.stop()
        for q in (self.w._integration_tasks,self.w._background_tasks,self.w._mask_tasks,self.w._overlay_tasks):
            q.invalidate();q.pool.waitForDone(25000)
        for pane in self.w.panes:pane.frame_player.pause();pane.frame_player.pool.waitForDone(15000)
        self.app.processEvents();self.w.close();self.app.processEvents()
        for p,digest in self.hashes.items():self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),digest)
        self.tmp.cleanup()

    def test_more_than_nine_checked_files_use_one_pane_and_visibility_never_reintegrates(self):
        from saxs_waxs_ui import checked_preview
        original=checked_preview.read_frame
        refs=[];threads=[]
        def read(*args,**kwargs):
            frame=original(*args,**kwargs);refs.append(weakref.ref(frame.data));threads.append(threading.get_ident());return frame
        panel=self.w.integration_panel
        panel.curve_scope_combo.setCurrentIndex(panel.curve_scope_combo.findData('checked'))
        self.w._integration_preview_timer.stop()
        initial_frame=self.w.active_pane.frame
        with patch.object(checked_preview,'read_frame',side_effect=read),patch.object(self.w,'_create_pane',side_effect=AssertionError('extra pane')):
            self.w._preview_integration()
            self.wait(lambda:not self.w._integration_tasks.busy and len(panel._available_curves)==12)
        self.assertEqual(len(self.w.panes),1)
        self.assertIs(self.w.active_pane.frame,initial_frame)
        self.assertNotIn(threading.get_ident(),threads)
        self.assertLessEqual(sum(ref() is not None for ref in refs),1)
        for index,(name,curve) in enumerate(panel._available_curves):
            self.assertIn('帧0002',name)
            np.testing.assert_allclose(curve.intensity[curve.count>0],11+index,rtol=1e-5)
        before=self.w.browser.selected_paths
        panel._choose_visible_curves();dialog=panel._curve_selection_dialog
        with patch.object(self.w._integration_workspace,'integrate',side_effect=AssertionError('reintegrated')):
            dialog.items.item(0).setCheckState(QtCore.Qt.CheckState.Unchecked)
            self.assertEqual(len(panel._curve_items),11)
            self.assertEqual(self.w.browser.selected_paths,before)
            exported=panel.export_displayed_data(self.root/'visible.csv')
            with exported.open(encoding='utf-8-sig',newline='') as f:header=next(csv.reader(f))
            self.assertEqual(len(header),22)
            self.assertFalse(any('sample-00.h5' in name for name in header))
            dialog.search.setText('sample-03')
            self.assertEqual(sum(not dialog.items.item(i).isHidden() for i in range(dialog.items.count())),1)
            dialog.none_button.click();self.assertEqual(len(panel._curve_items),0)
            dialog.all_button.click();self.assertEqual(len(panel._curve_items),12)
        curves=list(panel._available_curves);contexts=list(panel._available_contexts)
        dialog.items.item(0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        panel.show_curves([],[]);panel.show_curves(curves,contexts)
        self.assertEqual(len(panel._curve_items),11)

    def test_all_frames_and_azimuthal_mode_are_available_without_new_panes(self):
        panel=self.w.integration_panel
        self.w.browser.select_paths((self.paths[0],))
        self.w._detector_frame_choices={}
        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData('azimuthal'))
        panel.azimuth_lower.setValue(.001);panel.azimuth_upper.setValue(.25)
        panel.curve_scope_combo.setCurrentIndex(panel.curve_scope_combo.findData('checked'))
        self.w._integration_preview_timer.stop();self.w._preview_integration()
        self.wait(lambda:not self.w._integration_tasks.busy and len(panel._available_curves)==3)
        self.assertEqual(len(self.w.panes),1)
        self.assertTrue(all(curve.kind=='azimuthal_1d' for _,curve in panel._available_curves))
        self.assertEqual([context['processing']['source_selection']['frame_index'] for context in panel._available_contexts],[0,1,2])

    def test_cancellation_stops_between_frames_and_bad_source_is_reported(self):
        from saxs_waxs_core.background import BackgroundRecipe,BackgroundMode
        from saxs_waxs_core.integration import RadialIntegrationRecipe
        from saxs_waxs_core.masking import MaskRecipe
        snapshot=dict(geometry=self.g,recipe=RadialIntegrationRecipe(npt=32),mask=MaskRecipe(),
            background_recipe=BackgroundRecipe(mode=BackgroundMode.NO_BACKGROUND),
            manual_descriptors=[],external_mask_path=None)
        result=integrate_checked(snapshot,[self.paths[0],self.root/'missing.h5'],{}, {},
            IntegrationWorkspace(),PreviewBackgroundWorkspace(),PreviewMaskWorkspace(),lambda _:None,lambda:False)
        self.assertEqual(len(result['curves']),3)
        self.assertTrue(any('missing.h5' in error for error in result['failures']))
        stop=threading.Event()
        def progress(stage):
            if stage.startswith('读取勾选帧 2/'):stop.set()
        result=integrate_checked(snapshot,[self.paths[0]],{}, {},IntegrationWorkspace(),
            PreviewBackgroundWorkspace(),PreviewMaskWorkspace(),progress,stop.is_set)
        self.assertEqual(len(result['curves']),1)
        self.assertTrue(result['cancelled'])

    def test_many_curves_avoid_oversized_legend_and_reuse_hover_transforms(self):
        from saxs_waxs_core.integration import IntegrationCurve
        panel=self.w.integration_panel
        x=np.linspace(.01,2.,1000)
        curve=IntegrationCurve(x,np.ones(1000),None,np.ones(1000),'q_nm^-1','counts','radial_1d',{})
        panel.show_curves([(f'curve-{i}',curve) for i in range(40)])
        self.assertEqual(len(panel._plot_items),40)
        self.assertFalse(panel.legend.isVisible())
        with patch.object(panel,'_transformed_curves',side_effect=AssertionError('repeated transforms')):
            panel._nearest_curve_point(panel.plot.sceneBoundingRect().center())
        panel._choose_visible_curves()
        dialog=panel._curve_selection_dialog
        dialog.none_button.click()
        dialog.items.item(0).setCheckState(QtCore.Qt.CheckState.Checked)
        self.assertEqual(len(panel._plot_items),1)
        self.assertFalse(panel._outside_legend.isHidden())

    def test_raw_header_geometry_does_not_override_current_template(self):
        from dataclasses import replace
        from saxs_waxs_core.geometry import geometry_to_document
        from saxs_waxs_core.readers.detector_io import read_frame
        from saxs_waxs_core.background import BackgroundRecipe,BackgroundMode
        from saxs_waxs_core.integration import RadialIntegrationRecipe
        from saxs_waxs_core.masking import MaskRecipe
        alternate=geometry_to_document(replace(self.g,detector_distance_m=.8))
        source=self.paths[0]
        snapshot=dict(geometry=self.g,recipe=RadialIntegrationRecipe(npt=32),mask=MaskRecipe(),
            background_recipe=BackgroundRecipe(mode=BackgroundMode.NO_BACKGROUND),manual_descriptors=[])
        for processed,distance in ((False,.4),(True,.8)):
            def load(*args,**kwargs):
                frame=read_frame(*args,**kwargs)
                return replace(frame,metadata=replace(frame.metadata,geometry_document=alternate,
                    embedded_processing={'geometry':alternate,'intensity_unit':'detector_count','background_applied':False} if processed else {}))
            with patch('saxs_waxs_ui.checked_preview.read_frame',side_effect=load):
                result=integrate_checked(snapshot,[source],{}, {str(source.resolve()):(0,)},IntegrationWorkspace(),
                    PreviewBackgroundWorkspace(),PreviewMaskWorkspace(),lambda _:None,lambda:False)
            self.assertFalse(result['failures'])
            self.assertEqual(result['curves'][0][1].audit['geometry_poni']['dist_m'],distance)
