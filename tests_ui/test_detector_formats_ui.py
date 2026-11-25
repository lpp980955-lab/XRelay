import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import json
import numpy as np
from PySide6 import QtWidgets
from fabio.edfimage import EdfImage
from saxs_waxs_ui.app import MainWindow
from saxs_waxs_core.batch import run_batch_plan
from saxs_waxs_core.processing import batch_processing_plan_to_document,batch_processing_plan_from_document
import test_detector_formats as fixtures


class DetectorFormatsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        for widget in self.app.topLevelWidgets():widget.close()
        self.app.processEvents()

    def test_nexus_preview_frame_selection_and_every_frame_in_batch(self):
        base=fixtures.ROOT/'artifacts'/'test_tmp';base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            root=Path(tmp);path=root/'sample.nxs'
            a=np.arange(360,dtype='uint32').reshape(3,10,12)
            fixtures.nexus(path,a)
            window=MainWindow();window.show();window.browser.set_folder(root)
            window._file_selected(path)
            self.assertEqual(window._active_pane.frame.metadata.frame_count,3)
            window._active_pane.frame_spin.setValue(3)
            self.app.processEvents()
            np.testing.assert_array_equal(window._active_pane.frame.data,a[2])
            self.assertIsNotNone(window.current_geometry)
            window.background_panel.mode_combo.setCurrentIndex(window.background_panel.mode_combo.findData('no_background'))
            window.browser.select_paths((path,))
            window.integration_panel.radial_npt.setValue(32)
            plan=window._batch_plan_for_current_selection()
            self.assertEqual(len(plan.sample_sources),3)
            self.assertEqual([s['frame_index'] for s in plan.metadata['source_selections']],[0,1,2])
            restored=batch_processing_plan_from_document(batch_processing_plan_to_document(plan))
            self.assertEqual(restored.metadata['source_selections'],plan.metadata['source_selections'])
            result=run_batch_plan(restored,root/'integrated',protected_roots=())
            self.assertEqual(result.summary['failed'],0)
            document=json.loads(result.manifest_path.read_text(encoding='utf-8'))
            self.assertEqual(len(document['items']),3)
            self.assertEqual(len({row['data_file'] for row in document['items']}),3)
            window._batch_plan_draft=plan
            self.assertEqual(window._recipe_revision_indices('selected'),(0,1,2))
            self.assertEqual(window._recipe_revision_indices('from_current'),(2,))
            # Explicit manual correction must override the automatically read
            # header until the user deliberately reopens the source.
            from dataclasses import replace
            from saxs_waxs_core.geometry import GeometryProvenance
            changed=replace(window.current_geometry,detector_distance_m=.4,
                provenance=GeometryProvenance(method='manual_candidate'))
            window.geometry_panel.set_geometry(changed);window._apply_candidate_geometry(changed)
            window._activate_pane(window._active_pane)
            self.assertEqual(window.current_geometry.detector_distance_m,.4)
            self.assertFalse(window._processing_recipe_for_active_pane().metadata['prefer_embedded_geometry'])

    def test_background_edf_frame_choice_and_geometry_header_are_not_calibrants(self):
        base=fixtures.ROOT/'artifacts'/'test_tmp'
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            root=Path(tmp);path=root/'sample.h5'
            a=np.arange(120,dtype='uint32').reshape(10,12)+100
            fixtures.nexus(path,a)
            air=root/'air.edf';image=EdfImage(np.full((10,12),10),header={'exposure_time_s':'2'})
            image.append_frame(data=np.full((10,12),20),header={'exposure_time_s':'3'});image.write(str(air))
            window=MainWindow();window.show();window.browser.set_folder(root);window._file_selected(path)
            panel=window.background_panel
            self.assertEqual(panel.air_frame_spin.maximum(),2)
            panel.air_frame_spin.setValue(2)
            self.assertEqual(panel.source_selection,{'frame_index':1})
            panel.preview_button.click();self.app.processEvents()
            result=window._active_pane.background_result
            self.assertIsNotNone(result)
            np.testing.assert_allclose(result.data,a/2-20/3)
            window.browser.select_paths((path,))
            plan=window._batch_plan_for_current_selection()
            self.assertEqual(len(plan.sample_sources),1)
            self.assertEqual(plan.recipe.background.metadata['source_selection'],{'frame_index':1})
            panel.set_configuration(plan.recipe.background.mode,air,source_selection={'frame_index':1})
            self.assertEqual(panel.air_frame_spin.value(),2)
            # Configuration schedules background reads. Finish them before the
            # temporary EDF is removed on Windows, where open files are locked.
            for queue in (window._integration_tasks,window._overlay_tasks,window._background_tasks,window._mask_tasks):
                queue.invalidate()
                self.assertTrue(queue.pool.waitForDone(30000))
            self.app.processEvents()
            window.close()

    def test_gfrm_and_explicit_ambiguous_hdf_dataset_open_in_same_viewer(self):
        base=fixtures.ROOT/'artifacts'/'test_tmp'
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            root=Path(tmp);gfrm=root/'native.gfrm'
            a=np.arange(120,dtype='uint16').reshape(10,12);fixtures.bruker86(gfrm,a)
            window=MainWindow();window.show();window._file_selected(gfrm)
            np.testing.assert_array_equal(window._active_pane.frame.data,a)
            path=root/'multiple.h5'
            import h5py
            with h5py.File(path,'w') as f:f['A/data']=a;f['B/data']=a+100
            with patch.object(QtWidgets.QInputDialog,'getItem',side_effect=lambda *args:(args[3][1],True)):
                window._file_selected(path)
            self.assertEqual(window._active_pane.frame.metadata.dataset_path,'/B/data')
            np.testing.assert_array_equal(window._active_pane.frame.data,a+100)
