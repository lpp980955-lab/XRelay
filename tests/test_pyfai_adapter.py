from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saxs_waxs_core.geometry import (  # noqa: E402
    DetectorGeometry,
    Fit2DParameters,
    GeometryProvenance,
    fit2d_parameters,
    poni_parameters,
    scattering_coordinates,
)
from saxs_waxs_core.pyfai_adapter import (  # noqa: E402
    canonical_points_to_pyfai_indices,
    from_fit2d_parameters,
    pyfai_fit2d_parameters,
    pyfai_scattering_coordinates,
    pyfai_version,
    to_pyfai_geometry,
    to_pyfai_integrator,
)


def reference_geometry(**updates) -> DetectorGeometry:
    values = {
        "wavelength_m": 0.124e-9,
        "detector_distance_m": 1.634,
        "pixel_size_x_m": 0.172e-3,
        "pixel_size_y_m": 0.168e-3,
        "beam_center_x_px": 381.3,
        "beam_center_y_px": 528.2,
        "image_width_px": 981,
        "image_height_px": 1043,
        "provenance": GeometryProvenance(method="pyfai_crosscheck_fixture"),
    }
    values.update(updates)
    return DetectorGeometry(**values)


class PyFAIAdapterTests(unittest.TestCase):
    def test_distribution_version_is_the_audited_install(self):
        self.assertEqual(pyfai_version(), "2026.9.0")

    def test_integration_backend_uses_same_poni_truth(self):
        geometry = reference_geometry()
        expected = poni_parameters(geometry)
        backend = to_pyfai_integrator(geometry)
        self.assertAlmostEqual(backend.dist, expected.dist_m, places=14)
        self.assertAlmostEqual(backend.poni1, expected.poni1_m, places=14)
        self.assertAlmostEqual(backend.poni2, expected.poni2_m, places=14)
        self.assertEqual(backend.detector.max_shape, (1043, 981))

    def test_half_pixel_conversion_is_explicit(self):
        d1, d2 = canonical_points_to_pyfai_indices([0.5, 4.5], [0.5, 3.5])
        np.testing.assert_array_equal(d1, [0.0, 3.0])
        np.testing.assert_array_equal(d2, [0.0, 4.0])

    def test_poni_and_detector_axes_match_canonical_geometry(self):
        geometry = reference_geometry()
        backend = to_pyfai_geometry(geometry)
        self.assertAlmostEqual(backend.poni1, 528.2 * 0.168e-3, places=15)
        self.assertAlmostEqual(backend.poni2, 381.3 * 0.172e-3, places=15)
        self.assertAlmostEqual(backend.detector.pixel1, 0.168e-3, places=15)
        self.assertAlmostEqual(backend.detector.pixel2, 0.172e-3, places=15)
        self.assertEqual(backend.detector.max_shape, (1043, 981))
        fit2d = backend.getFit2D()
        self.assertAlmostEqual(fit2d["centerX"], 381.3, places=12)
        self.assertAlmostEqual(fit2d["centerY"], 528.2, places=12)

    def test_untilted_q_two_theta_and_azimuth_match_analytical_truth(self):
        geometry = reference_geometry()
        cx, cy = geometry.beam_center_x_px, geometry.beam_center_y_px
        x = np.array([cx, cx, cx + 1, cx, cx - 1, 823.5, 0.5])
        y = np.array([cy, cy - 1, cy, cy + 1, cy, 77.5, 1042.5])
        expected = scattering_coordinates(geometry, x, y)
        actual = pyfai_scattering_coordinates(geometry, x, y)
        np.testing.assert_allclose(actual.two_theta_rad, expected.two_theta_rad, rtol=2e-13, atol=2e-15)
        np.testing.assert_allclose(actual.q_nm_inv, expected.q_nm_inv, rtol=2e-13, atol=2e-14)
        np.testing.assert_allclose(actual.s_nm_inv, expected.s_nm_inv, rtol=2e-13, atol=2e-15)
        np.testing.assert_allclose(
            actual.azimuth_deg[1:], expected.azimuth_deg[1:], rtol=0, atol=2e-12
        )
        self.assertTrue(math.isnan(actual.azimuth_deg[0]))

    def test_agbh_first_ring_q_matches_reference(self):
        geometry = reference_geometry(pixel_size_y_m=0.172e-3)
        radius_px = 201.81557655958323
        actual = pyfai_scattering_coordinates(
            geometry,
            geometry.beam_center_x_px + radius_px,
            geometry.beam_center_y_px,
        )
        self.assertAlmostEqual(float(actual.q_nm_inv), 2 * math.pi / 5.838, places=12)

    def test_tilted_direct_beam_and_poni_representations_match_pyfai(self):
        geometry = reference_geometry(
            detector_distance_m=0.315,
            beam_center_x_px=692.56,
            beam_center_y_px=927.34,
            image_width_px=2048,
            image_height_px=2048,
            rot1_rad=-0.012,
            rot2_rad=0.007,
            rot3_rad=0.003,
        )
        backend = to_pyfai_geometry(geometry)
        expected_poni = poni_parameters(geometry)
        self.assertAlmostEqual(backend.dist, expected_poni.dist_m, places=15)
        self.assertAlmostEqual(backend.poni1, expected_poni.poni1_m, places=15)
        self.assertAlmostEqual(backend.poni2, expected_poni.poni2_m, places=15)
        expected_fit2d = fit2d_parameters(geometry)
        actual_fit2d = pyfai_fit2d_parameters(geometry)
        self.assertAlmostEqual(
            actual_fit2d.direct_distance_m,
            expected_fit2d.direct_distance_m,
            places=12,
        )
        self.assertAlmostEqual(actual_fit2d.center_x_px, expected_fit2d.center_x_px, places=9)
        self.assertAlmostEqual(actual_fit2d.center_y_px, expected_fit2d.center_y_px, places=9)
        self.assertAlmostEqual(actual_fit2d.tilt_deg, expected_fit2d.tilt_deg, places=10)
        self.assertAlmostEqual(
            actual_fit2d.tilt_plane_rotation_deg,
            expected_fit2d.tilt_plane_rotation_deg,
            places=9,
        )

    def test_fit2d_manual_nonzero_tilt_values_round_trip(self):
        # FIT2D manual control-form example: 315 mm, centre 692.560/927.340,
        # tilt-plane 29.046 degrees and detector tilt 0.736 degrees.
        parameters = Fit2DParameters(
            direct_distance_m=0.315,
            center_x_px=692.560,
            center_y_px=927.340,
            tilt_deg=0.736,
            tilt_plane_rotation_deg=29.046,
        )
        geometry = from_fit2d_parameters(
            parameters,
            wavelength_m=0.987536e-10,
            pixel_size_x_m=100e-6,
            pixel_size_y_m=100e-6,
            image_width_px=2048,
            image_height_px=2048,
            provenance=GeometryProvenance(
                method="fit2d_manual_numeric_example",
                source_application="FIT2D manual",
            ),
        )
        recovered = pyfai_fit2d_parameters(geometry)
        self.assertAlmostEqual(recovered.direct_distance_m, parameters.direct_distance_m, places=12)
        self.assertAlmostEqual(recovered.center_x_px, parameters.center_x_px, places=9)
        self.assertAlmostEqual(recovered.center_y_px, parameters.center_y_px, places=9)
        self.assertAlmostEqual(recovered.tilt_deg, parameters.tilt_deg, places=10)
        self.assertAlmostEqual(
            recovered.tilt_plane_rotation_deg,
            parameters.tilt_plane_rotation_deg,
            places=9,
        )


if __name__ == "__main__":
    unittest.main()
