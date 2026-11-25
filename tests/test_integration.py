from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saxs_waxs_core.geometry import DetectorGeometry, GeometryProvenance  # noqa: E402
from saxs_waxs_core.integration import (  # noqa: E402
    AzimuthSector,
    AzimuthalIntegrationRecipe,
    IntegrationError,
    PixelSplitting,
    RadialIntegrationRecipe,
    RadialUnit,
    detector_radial_array,
    integrate_azimuthal_1d,
    integrate_radial_1d,
)
from validation.generate_truth_sets import build_truth_arrays  # noqa: E402


ARRAYS, EXPECTED = build_truth_arrays()
SHAPE = tuple(EXPECTED["shape"])
CENTER_XY = tuple(EXPECTED["center_xy_px"])
PIXEL_M = 100e-6
DISTANCE_M = 0.5
WAVELENGTH_M = 0.124e-9


def geometry() -> DetectorGeometry:
    return DetectorGeometry(
        wavelength_m=WAVELENGTH_M,
        detector_distance_m=DISTANCE_M,
        pixel_size_x_m=PIXEL_M,
        pixel_size_y_m=PIXEL_M,
        beam_center_x_px=CENTER_XY[0] + 0.5,
        beam_center_y_px=CENTER_XY[1] + 0.5,
        image_width_px=SHAPE[1],
        image_height_px=SHAPE[0],
        provenance=GeometryProvenance(method="integration_truth"),
    )


def expected_q(radius_px: float) -> float:
    two_theta = math.atan(radius_px * PIXEL_M / DISTANCE_M)
    return 4.0 * math.pi * math.sin(two_theta / 2.0) / (WAVELENGTH_M * 1e9)


def project_azimuth() -> np.ndarray:
    rows, columns = np.indices(SHAPE, dtype=np.float64)
    dx = columns - CENTER_XY[0]
    dy = rows - CENTER_XY[1]
    return np.mod(np.degrees(np.arctan2(dx, -dy)), 360.0)


class IntegrationTests(unittest.TestCase):
    def test_non_split_radial_range_excludes_outside_pixel_centers(self):
        rows, columns = np.indices(SHAPE)
        radius = np.hypot(columns - CENTER_XY[0], rows - CENTER_XY[1])
        inner, outer = 40.25, 70.25
        inside = (radius >= inner) & (radius <= outer)
        data = np.where(inside, 3.0, 1000.0)
        ranges = {
            RadialUnit.Q_NM_INV: tuple(expected_q(r) for r in (inner, outer)),
            RadialUnit.Q_ANGSTROM_INV: tuple(expected_q(r) / 10 for r in (inner, outer)),
            RadialUnit.S_NM_INV: tuple(expected_q(r) / (2 * math.pi) for r in (inner, outer)),
            RadialUnit.TWO_THETA_DEG: tuple(
                math.degrees(math.atan(r * PIXEL_M / DISTANCE_M)) for r in (inner, outer)
            ),
        }
        for unit, radial_range in ranges.items():
            with self.subTest(unit=unit):
                recipe = RadialIntegrationRecipe(
                    npt=12, unit=unit, radial_range=radial_range,
                    pixel_splitting=PixelSplitting.NONE, correct_solid_angle=False,
                )
                result = integrate_radial_1d(data, geometry(), recipe)
                self.assertEqual(result.count.sum(), np.count_nonzero(inside))
                np.testing.assert_allclose(result.intensity[result.count > 0], 3.0)

    def test_constant_radial_mean_and_masked_outlier(self):
        data = np.full(SHAPE, 2.0)
        data[20, 20] = 1e6
        mask = np.zeros(SHAPE, dtype=bool)
        mask[20, 20] = True
        recipe = RadialIntegrationRecipe(
            npt=180,
            pixel_splitting=PixelSplitting.NONE,
            correct_solid_angle=False,
        )
        masked = integrate_radial_1d(data, geometry(), recipe, mask=mask)
        unmasked = integrate_radial_1d(data, geometry(), recipe)
        occupied = masked.count > 0
        np.testing.assert_allclose(masked.intensity[occupied], 2.0, rtol=0, atol=1e-12)
        self.assertGreater(float(np.nanmax(unmasked.intensity)), 100.0)
        self.assertEqual(masked.audit["excluded_pixel_count"], 1)
        self.assertFalse(masked.intensity.flags.writeable)

    def test_mt003_ring_peak_matches_geometry_q(self):
        recipe = RadialIntegrationRecipe(
            npt=500,
            pixel_splitting=PixelSplitting.NONE,
            correct_solid_angle=False,
        )
        result = integrate_radial_1d(
            ARRAYS["mt003_isotropic_ring"], geometry(), recipe
        )
        peak_q = float(result.x[int(np.nanargmax(result.intensity))])
        bin_width = float(np.median(np.diff(result.x)))
        self.assertLessEqual(abs(peak_q - expected_q(63.5)), 1.5 * bin_width)

    def test_arbitrary_wrapped_sector_uses_visible_project_convention(self):
        azimuth = project_azimuth()
        delta = (azimuth - 350.0 + 180.0) % 360.0 - 180.0
        selected = np.abs(delta) <= 12.0
        data = np.where(selected, 5.0, 100.0)
        recipe = RadialIntegrationRecipe(
            npt=120,
            sectors=(AzimuthSector(350.0, 24.0),),
            pixel_splitting=PixelSplitting.NONE,
            correct_solid_angle=False,
        )
        result = integrate_radial_1d(data, geometry(), recipe)
        occupied = result.count > 0
        np.testing.assert_allclose(result.intensity[occupied], 5.0, rtol=0, atol=1e-12)
        self.assertEqual(result.audit["sectors"][0]["center_deg"], 350.0)

    def test_s_axis_is_exact_q_over_two_pi(self):
        q_result = integrate_radial_1d(
            ARRAYS["mt003_isotropic_ring"],
            geometry(),
            RadialIntegrationRecipe(npt=160, unit=RadialUnit.Q_NM_INV),
        )
        s_result = integrate_radial_1d(
            ARRAYS["mt003_isotropic_ring"],
            geometry(),
            RadialIntegrationRecipe(npt=160, unit=RadialUnit.S_NM_INV),
        )
        np.testing.assert_allclose(s_result.x, q_result.x / (2.0 * math.pi), rtol=0, atol=1e-15)
        np.testing.assert_allclose(s_result.intensity, q_result.intensity, equal_nan=True)

    def test_detector_radial_array_matches_units_and_is_read_only(self):
        q = detector_radial_array(geometry(), SHAPE, RadialUnit.Q_NM_INV)
        s = detector_radial_array(geometry(), SHAPE, RadialUnit.S_NM_INV)
        self.assertEqual(q.shape, SHAPE)
        self.assertFalse(q.flags.writeable)
        np.testing.assert_allclose(s, q / (2.0 * math.pi), rtol=0, atol=1e-15)
        with self.assertRaisesRegex(IntegrationError, "dimensions"):
            detector_radial_array(geometry(), (10, 10))

    def test_full_azimuth_constant_is_constant_in_visible_zero_to_360_axis(self):
        q_center = expected_q(70.0)
        result = integrate_azimuthal_1d(
            np.full(SHAPE, 3.0),
            geometry(),
            AzimuthalIntegrationRecipe(
                radial_range=(q_center * 0.9, q_center * 1.1),
                npt_azimuth=360,
                npt_radial=60,
                pixel_splitting=PixelSplitting.NONE,
                correct_solid_angle=False,
            ),
        )
        occupied = result.count > 0
        self.assertTrue(np.all(np.diff(result.x) >= 0))
        self.assertGreaterEqual(float(result.x.min()), 0.0)
        self.assertLess(float(result.x.max()), 360.0)
        np.testing.assert_allclose(result.intensity[occupied], 3.0, rtol=0, atol=1e-12)

    def test_azimuth_peak_at_350_degrees_is_not_mirrored(self):
        radius = ARRAYS["mt003_radius_px"]
        azimuth = project_azimuth()
        angular_delta = (azimuth - 350.0 + 180.0) % 360.0 - 180.0
        data = np.exp(-0.5 * ((radius - 70.0) / 4.0) ** 2) * np.exp(
            -0.5 * (angular_delta / 5.0) ** 2
        )
        q_center = expected_q(70.0)
        result = integrate_azimuthal_1d(
            data,
            geometry(),
            AzimuthalIntegrationRecipe(
                radial_range=(expected_q(62.0), expected_q(78.0)),
                npt_azimuth=360,
                npt_radial=80,
                pixel_splitting=PixelSplitting.NONE,
                correct_solid_angle=False,
            ),
        )
        peak = float(result.x[int(np.nanargmax(result.intensity))])
        delta = (peak - 350.0 + 180.0) % 360.0 - 180.0
        self.assertLess(abs(delta), 1.5)
        self.assertEqual(result.audit["visible_azimuth_convention"], "zero_up_clockwise_0_360")

    def test_linked_occlusion_keeps_missing_azimuth_bins_empty_without_mirroring(self):
        data = np.full(SHAPE, 7.0)
        before = data.copy()
        mask = np.zeros(SHAPE, dtype=bool)
        mask[:, int(math.ceil(SHAPE[1] / 2.0)) :] = True
        result = integrate_azimuthal_1d(
            data,
            geometry(),
            AzimuthalIntegrationRecipe(
                radial_range=(expected_q(62.0), expected_q(78.0)),
                npt_azimuth=360,
                npt_radial=80,
                pixel_splitting=PixelSplitting.NONE,
                correct_solid_angle=False,
            ),
            mask=mask,
        )
        missing = result.count <= 0
        occupied = result.count > 0
        self.assertGreater(int(np.count_nonzero(missing)), 120)
        self.assertGreater(int(np.count_nonzero(occupied)), 120)
        np.testing.assert_allclose(result.intensity[occupied], 7.0, rtol=0, atol=1e-12)
        self.assertTrue(np.all(result.intensity[missing] == 0.0))
        np.testing.assert_array_equal(data, before)

    def test_invalid_recipe_and_unmasked_nonfinite_data_are_rejected(self):
        with self.assertRaises(IntegrationError):
            AzimuthSector(0.0, 0.0)
        with self.assertRaises(IntegrationError):
            RadialIntegrationRecipe(npt=1)
        bad = np.ones(SHAPE)
        bad[0, 0] = np.nan
        with self.assertRaisesRegex(IntegrationError, "unmasked"):
            integrate_radial_1d(bad, geometry())
        mask = np.zeros(SHAPE, dtype=bool)
        mask[0, 0] = True
        result = integrate_radial_1d(bad, geometry(), mask=mask)
        self.assertEqual(result.audit["excluded_pixel_count"], 1)


if __name__ == "__main__":
    unittest.main()
