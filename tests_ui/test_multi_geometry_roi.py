"""Shared physical sectors retain per-pane detector calibration."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace
import numpy as np
from PySide6 import QtCore
from saxs_waxs_ui.app import MainWindow, create_application
from saxs_waxs_core.models import ImageFrame, ImageMetadata
from saxs_waxs_core.geometry import DetectorGeometry, GeometryProvenance

class MultiGeometryRoiTests(unittest.TestCase):
    def test_different_shapes_share_angles_but_keep_geometry_in_overlay_and_jobs(self):
        app = create_application([])
        w = MainWindow()
        try:
            panes = [w.active_pane, w.open_comparison_window()]
            geometries = []
            for i, (pane, shape) in enumerate(zip(panes, [(40, 50), (60, 70)])):
                h, width = shape
                path = Path(f'fixture-{i}.tif')
                frame = ImageFrame(np.ones(shape), ImageMetadata(path, 'fixture', 'test', width, h, 'float64'))
                pane.set_frame(path, frame)
                g = DetectorGeometry(wavelength_m=1e-10, detector_distance_m=.2+i*.3,
                    pixel_size_x_m=1e-4, pixel_size_y_m=1e-4,
                    beam_center_x_px=width*.4, beam_center_y_px=h*.6,
                    image_width_px=width, image_height_px=h,
                    provenance=GeometryProvenance(method='test'))
                pane.geometry_override = g
                geometries.append(g)
            w._activate_pane(panes[0])
            w.current_geometry = geometries[0]
            w.sync_display_check.setChecked(False)
            panes[0].integration_region = dict(mode='radial', sector_mode='single', center_deg=35., width_deg=25.)
            panes[1].integration_region = dict(mode='radial', sector_mode='single', center_deg=80., width_deg=40.)
            w.sync_display_check.setChecked(True)
            self.assertEqual(panes[0].integration_region, panes[1].integration_region)
            descriptor = dict(mode='radial', sector_mode='single', center_deg=35., width_deg=25.)
            w._integration_panel_region_changed(descriptor)
            for pane, g in zip(panes, geometries):
                self.assertEqual(pane.integration_region, descriptor)
                self.assertIs(pane.viewer.integration_overlay._geometry, g)
                self.assertEqual(pane.viewer.integration_overlay.handle_count, 2)
            handle = panes[0].viewer.integration_overlay._direction_handle
            handle.setPos(geometries[0].beam_center_x_px+10, geometries[0].beam_center_y_px)
            self.assertEqual(panes[0].integration_region, panes[1].integration_region)
            self.assertAlmostEqual(panes[1].integration_region['center_deg'], 90.)
            w._integration_preview_timer.stop()
            with patch('saxs_waxs_ui.latest_task.integrate_previews', return_value=None) as integrate:
                with patch.object(w._integration_tasks, 'submit', side_effect=lambda work, **kw: work(None)):
                    w._preview_integration()
            jobs, failures = integrate.call_args.args[:2]
            self.assertEqual(failures, [])
            self.assertEqual(len(jobs), 2)
            self.assertEqual([j['geometry'] for j in jobs], geometries)
            # A completed tilted map is valid even if its pane is not active.
            tilted = replace(geometries[1], rot1_rad=.01)
            panes[1].geometry_override = tilted
            key = w._integration_cache_key((60, 70), 'q_nm^-1', tilted)
            values = np.ones((60, 70))
            w._overlay_map_ready((key, values))
            self.assertIs(w._integration_radial_cache[key], values)
        finally:
            w._integration_preview_timer.stop()
            w.close()
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()

if __name__ == '__main__': unittest.main()
