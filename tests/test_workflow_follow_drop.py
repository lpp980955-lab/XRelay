import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import h5py
import numpy as np
from PySide6 import QtCore,QtGui,QtWidgets
from saxs_waxs_ui.app import MainWindow
from saxs_waxs_core.geometry import DetectorGeometry,GeometryProvenance


class WorkflowFollowDropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        root=Path(__file__).resolve().parents[1]
        self.tmp=tempfile.TemporaryDirectory(dir=root/'artifacts')
        self.root=Path(self.tmp.name)
        self.bg=self.root/'BG-3S';self.bg.mkdir()
        self.chunk=self.bg/'background_data.h5'
        with h5py.File(self.chunk,'w') as f:
            f['data']=np.stack([np.full((64,80),n,dtype='uint32') for n in (2,4)])
        self.master=self.bg/'background_master.h5'
        with h5py.File(self.master,'w') as f:
            f['entry/data/data']=h5py.ExternalLink(self.chunk.name,'/data')
        self.sample=self.root/'sample.h5'
        self.other=self.root/'other.h5'
        for path in (self.sample,self.other):
            with h5py.File(path,'w') as f:
                y,x=np.indices((64,80));image=20+100*np.exp(-((np.hypot(x-40,y-32)-15)/3)**2)
                f['entry/data/data']=np.stack([image,image+10]).astype('float32')
        self.hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.sample,self.other,self.master,self.chunk)}
        self.window=MainWindow(settings=QtCore.QSettings(str(self.root/'settings.ini'),QtCore.QSettings.Format.IniFormat))

    def wait(self,condition):
        end=time.monotonic()+15
        while time.monotonic()<end and not condition():
            self.app.processEvents();time.sleep(.004)
        self.assertTrue(condition())

    def tearDown(self):
        w=self.window
        w._integration_preview_timer.stop()
        for queue in (w._integration_tasks,w._background_tasks,w._mask_tasks,w._overlay_tasks):
            queue.invalidate();queue.pool.waitForDone(15000)
        for pane in w.panes:
            pane.frame_player.pause();pane.frame_player.pool.waitForDone(15000)
        for win in w._peak_fit_windows:
            win.fit_panel._interactive_tasks.invalidate()
            win.fit_panel._interactive_tasks.pool.waitForDone(15000)
        self.app.processEvents();w.close();self.app.processEvents()
        for p,digest in self.hashes.items():self.assertEqual(digest,hashlib.sha256(p.read_bytes()).hexdigest())
        self.tmp.cleanup()

    def drop(self,widget,paths):
        mime=QtCore.QMimeData();mime.setUrls([QtCore.QUrl.fromLocalFile(str(p)) for p in paths])
        enter=QtGui.QDragEnterEvent(QtCore.QPoint(5,5),QtCore.Qt.DropAction.CopyAction,mime,
            QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(widget,enter);self.assertTrue(enter.isAccepted())
        event=QtGui.QDropEvent(QtCore.QPointF(5,5),QtCore.Qt.DropAction.CopyAction,mime,
            QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(widget,event);self.assertTrue(event.isAccepted())

    def test_real_qt_folder_drops_and_background_master_candidate(self):
        w=self.window
        self.drop(w.browser.path_edit,[self.root])
        self.assertEqual(w.background_panel.background_path.resolve(),self.master.resolve())
        self.assertEqual(w.background_panel.air_frame_spin.maximum(),2)
        self.drop(w.background_panel.background_combo,[self.bg])
        self.assertEqual(w.background_panel.background_path.resolve(),self.master.resolve())
        self.assertEqual(w.background_panel.air_frame_spin.maximum(),2)
        w.background_panel.air_frame_spin.setValue(2)
        w.background_panel.set_files([self.sample,self.other])
        self.assertEqual(w.background_panel.background_path.resolve(),self.master.resolve())
        self.assertEqual(w.background_panel.source_selection['frame_index'],1)
        mask=self.root/'mask.npy';np.save(mask,np.zeros((64,80),dtype='uint8'))
        self.drop(w.mask_panel.external_mask_button,[mask])
        self.assertEqual(w.mask_panel.external_mask_path.resolve(),mask.resolve())
        self.assertFalse(any('空气' in label.text() for label in w.background_panel.findChildren(QtWidgets.QLabel)))

    def test_raw_navigation_stays_raw_until_steps_enabled_then_auto_updates_all(self):
        w=self.window
        w.browser.set_folder(self.root)
        w._file_selected(self.sample,{'dataset':'/entry/data/data','frame_index':0})
        w._file_selected(self.other,{'dataset':'/entry/data/data','frame_index':1})
        pane=w.active_pane
        np.testing.assert_array_equal(pane.viewer._data,pane.frame.data)
        self.assertIsNone(pane.background_result)
        self.assertFalse(w._background_tasks.busy)
        w.background_panel.import_paths([self.bg])
        w.background_panel.air_frame_spin.setValue(2)
        w._preview_background(w.background_panel.mode,w.background_panel.background_path)
        self.wait(lambda:pane.background_result is not None)
        pane.viewer.set_manual_mask_descriptors(({'kind':'rectangle','pos':(5.,6.),'size':(9.,8.)},),emit=False)
        w._preview_mask(w.mask_panel.recipe())
        self.wait(lambda:pane.mask_result is not None)
        geometry=DetectorGeometry(wavelength_m=1e-10,detector_distance_m=.4,
            pixel_size_x_m=75e-6,pixel_size_y_m=75e-6,beam_center_x_px=40,beam_center_y_px=32,
            image_width_px=80,image_height_px=64,provenance=GeometryProvenance(method='test'))
        w.geometry_panel.set_geometry(geometry);w.geometry_panel.apply_button.click()
        w.integration_panel.curve_scope_combo.setCurrentIndex(w.integration_panel.curve_scope_combo.findData('current'))
        w._preview_integration()
        self.wait(lambda:pane.integration_result is not None and not w._background_tasks.busy)
        curves=list(w.integration_panel._curve_items);contexts=list(w.integration_panel._curve_parameter_contexts)
        w._open_peak_fit_workspace(curves,contexts)
        fit=w._peak_fit_windows[-1].fit_panel
        fit.auto_check.setChecked(False)
        fit.range_lower.setValue(.05);fit.range_upper.setValue(.2)
        for source,index in ((self.sample,1),(self.other,0)):
            w._file_selected(source,{'dataset':'/entry/data/data','frame_index':index})
            current=pane.frame
            self.wait(lambda:pane.background_result is not None and pane.integration_result is not None
                and getattr(pane,'_follow_display_frame',None) is current)
            np.testing.assert_array_equal(pane.viewer._data,current.data.astype(float)-4)
            self.assertTrue(pane.viewer._mask_overlay_item.isVisible())
            self.assertTrue(pane.mask_result.excluded[7:12,6:10].all())
            self.assertIn(f'帧{index+1:04d}',fit._curves[0][0])
            self.assertEqual((fit.range_lower.value(),fit.range_upper.value()),(.05,.2))
            self.assertFalse(fit.auto_check.isChecked())

    def test_geometry_recipe_curve_drops_reuse_importers_and_ambiguous_background_asks(self):
        w=self.window
        folder=self.root/'geometry';folder.mkdir()
        geometry=folder/'geometry.json';geometry.write_text('{}',encoding='utf-8')
        with patch.object(w.geometry_panel,'load_from_path') as load:
            self.drop(w.geometry_panel.load_button,[folder])
            load.assert_called_once_with(str(geometry))
        with patch.object(w,'load_processing_recipe_into_ui') as load:
            self.drop(w.recipe_panel.load_recipe_button,[geometry])
            load.assert_called_once_with(str(geometry))
        curve_folder=self.root/'curves';curve_folder.mkdir()
        csv=curve_folder/'curve.csv';csv.write_text('q,I\n0.1,10\n0.2,20\n',encoding='utf-8')
        with patch.object(w,'_choose_import_curves') as load:
            self.drop(w,[csv])
            load.assert_called_once_with([str(csv)])
        with patch.object(w,'_choose_import_curves') as load:
            self.drop(w,[curve_folder])
            load.assert_called_once_with([str(csv)])
        with patch.object(QtWidgets.QInputDialog,'getItem',return_value=(str(self.master.resolve()),True)) as choose:
            w.background_panel.import_paths([self.root])
            choose.assert_called_once()
            self.assertEqual(w.background_panel.background_path.resolve(),self.master.resolve())
