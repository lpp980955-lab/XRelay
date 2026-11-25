import os
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
import h5py
import numpy as np
from PySide6 import QtCore,QtWidgets
from saxs_waxs_core.geometry import DetectorGeometry,GeometryProvenance,save_geometry,load_geometry
from saxs_waxs_core.calibration import CalibrationError,fit_manual_calibrant_ring
from saxs_waxs_core.center_finding import suggest_center_only
from saxs_waxs_ui.app import MainWindow
from saxs_waxs_ui.geometry_panel import GeometryPanel


def geometry():
    return DetectorGeometry(wavelength_m=1.03e-10,detector_distance_m=.4,
        pixel_size_x_m=75e-6,pixel_size_y_m=90e-6,
        beam_center_x_px=250,beam_center_y_px=250,image_width_px=512,image_height_px=512,
        provenance=GeometryProvenance(method="test_known_parameters"))


class CenterOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_three_points_recover_physical_center_without_changing_known_parameters(self):
        initial=geometry()
        angles=np.array([.3,2.1,4.8])
        points=np.column_stack((283.25+.008*np.cos(angles)/initial.pixel_size_x_m,
                                231.75+.008*np.sin(angles)/initial.pixel_size_y_m))
        estimate=fit_manual_calibrant_ring(initial,None,1,points)
        candidate=estimate.candidate_geometry
        self.assertAlmostEqual(candidate.beam_center_x_px,283.25,places=6)
        self.assertAlmostEqual(candidate.beam_center_y_px,231.75,places=6)
        for field in ("detector_distance_m","wavelength_m","pixel_size_x_m","pixel_size_y_m"):
            self.assertEqual(getattr(candidate,field),getattr(initial,field))
        self.assertIsNone(estimate.d_angstrom)
        self.assertIsNone(candidate.metadata["calibrant_id"])
        with self.assertRaises(CalibrationError):
            fit_manual_calibrant_ring(initial,None,1,[(1,2),(2,3),(3,4)])

    def test_symmetry_center_on_masked_displaced_ring_and_reject_flat_signal(self):
        yy,xx=np.mgrid[:512,:512]+.5
        radius=np.hypot(xx-283.25,(yy-231.75)*1.2)
        image=10+500*np.exp(-.5*((radius-120)/3)**2)
        mask=np.zeros(image.shape,bool)
        mask[:,245:255]=True
        mask[250:260,:]=True
        image[mask]=4294967295
        original=image.copy()
        result=suggest_center_only(image,geometry(),mask)
        self.assertAlmostEqual(result.beam_center_x_px,283.25,delta=.4)
        self.assertAlmostEqual(result.beam_center_y_px,231.75,delta=.4)
        self.assertEqual(result.detector_distance_m,.4)
        np.testing.assert_array_equal(image,original)
        with self.assertRaises(CalibrationError):
            suggest_center_only(np.ones((512,512)),geometry())

    def test_mode_roundtrips_and_panel_never_requests_calibrant(self):
        panel=GeometryPanel()
        try:
            panel.set_geometry(geometry())
            panel.calibrant_combo.setCurrentIndex(panel.calibrant_combo.findData("none"))
            self.assertTrue(panel.center_only)
            self.assertTrue(panel.manual_ring_combo.isHidden())
            self.assertFalse(panel.preview_check.isEnabled())
            requested=[]
            panel.manual_ring_pick_requested.connect(lambda *args:requested.append(args))
            panel.manual_ring_button.click()
            self.assertIsNone(requested[0][1])
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/"artifacts/test_tmp") as tmp:
                path=Path(tmp)/"geometry.json"
                save_geometry(panel.geometry(),path,protected_roots=(Path(__file__).resolve().parents[1]/"data",))
                restored=load_geometry(path)
                panel.calibrant_combo.setCurrentIndex(panel.calibrant_combo.findData("AgBh"))
                panel.set_geometry(restored)
                self.assertTrue(panel.center_only)
                self.assertEqual(panel.geometry().detector_distance_m,.4)
        finally:
            panel.close()

    def test_main_window_three_points_and_background_auto_center_at_fixed_distance(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/"artifacts/test_tmp") as tmp:
            path=Path(tmp)/"ring.h5"
            yy,xx=np.mgrid[:512,:512]+.5
            image=10+500*np.exp(-.5*((np.hypot(xx-283.25,yy-231.75)-120)/3)**2)
            with h5py.File(path,"w") as file:
                file["entry/data/data"]=image
            window=MainWindow(settings=QtCore.QSettings(str(Path(tmp)/"settings.ini"),QtCore.QSettings.Format.IniFormat))
            try:
                window._file_selected(path)
                panel=window.geometry_panel
                panel.set_geometry(replace(geometry(),pixel_size_y_m=75e-6,metadata={"preview_calibrant":"none"}))
                panel.manual_ring_button.click()
                for angle in (.2,2.2,4.5):
                    window._manual_ring_point_picked(window.active_pane.viewer,
                        283.25+120*np.cos(angle),231.75+120*np.sin(angle))
                self.assertAlmostEqual(panel.geometry().beam_center_x_px,283.25,places=6)
                self.assertEqual(panel.geometry().detector_distance_m,.4)
                self.assertIn("SDD保持",panel.status_label.text())
                panel.apply_button.click()
                self.assertEqual(window.current_geometry.detector_distance_m,.4)
                panel.suggest_button.click()
                deadline=time.monotonic()+10
                while window._calibration_thread is not None and time.monotonic()<deadline:
                    self.app.processEvents()
                    time.sleep(.005)
                self.assertIsNone(window._calibration_thread)
                self.assertEqual(panel.geometry().provenance.method,"radial_edge_center_only_candidate")
                self.assertAlmostEqual(panel.geometry().beam_center_x_px,283.25,delta=.4)
                self.assertEqual(panel.geometry().detector_distance_m,.4)
                self.assertTrue(panel.residual_group.isHidden())
            finally:
                window.close()
                self.app.processEvents()
