from __future__ import annotations

import unittest

import numpy as np

from validation.generate_truth_sets import build_truth_arrays, wrapped_delta_deg


class TruthSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.arrays, cls.expected = build_truth_arrays()

    def test_signed_values_are_preserved(self):
        array = self.arrays["mt001_signed_int32"]
        self.assertEqual(array.dtype, np.dtype(np.int32))
        np.testing.assert_array_equal(array[0, :5], [-2, -1, 0, 1, 2])

    def test_background_equal_exposure(self):
        result = self.arrays["mt002_sample_0p5s"] - self.arrays["mt002_air_0p5s"]
        np.testing.assert_allclose(result, self.arrays["mt002_signal"], rtol=0.0, atol=2e-15)

    def test_background_exposure_normalization(self):
        result = self.arrays["mt002_sample_1p0s"] - 2.0 * self.arrays["mt002_air_0p5s"]
        np.testing.assert_allclose(result, self.arrays["mt002_signal"], rtol=0.0, atol=4e-15)

    def test_ring_peak_radius(self):
        radius = self.arrays["mt003_radius_px"]
        ring = self.arrays["mt003_isotropic_ring"]
        peak_radius = float(radius.flat[int(np.argmax(ring))])
        self.assertLessEqual(abs(peak_radius - 63.5), 0.02)

    def test_wrapped_angle_crosses_zero(self):
        values = wrapped_delta_deg(np.array([359.0, 0.0, 1.0]), 0.0)
        np.testing.assert_allclose(values, [-1.0, 0.0, 1.0])

    def test_masks_are_boolean_and_nonempty(self):
        mask = self.arrays["mt006_combined_mask"]
        self.assertEqual(mask.dtype, np.dtype(bool))
        self.assertGreater(int(mask.sum()), 0)
        self.assertLess(int(mask.sum()), mask.size)

    def test_isotropic_herman_is_zero(self):
        value = self.expected["truth"]["mt009"]["isotropic_herman"]
        self.assertAlmostEqual(value, 0.0, places=8)

    def test_cauchy_ruland_parameters_recover(self):
        q = self.arrays["mt010_q_nm_inv"]
        bobs = self.arrays["mt010_cauchy_bobs_rad"]
        slope, intercept = np.polyfit(1.0 / q, bobs, 1)
        lf = 2.0 * np.pi / slope
        self.assertAlmostEqual(float(lf), 86.0, places=10)
        self.assertAlmostEqual(float(intercept), 0.041, places=12)

    def test_gaussian_ruland_parameters_recover(self):
        q = self.arrays["mt010_q_nm_inv"]
        bobs = self.arrays["mt011_gaussian_bobs_rad"]
        slope, intercept = np.polyfit(1.0 / q**2, bobs**2, 1)
        lf = 1.0 / np.sqrt(slope)
        self.assertAlmostEqual(float(lf), 93.0, places=10)
        self.assertAlmostEqual(float(np.sqrt(intercept)), 0.036, places=12)

    def test_crystallinity_matches_component_areas(self):
        truth = self.expected["truth"]["mt012"]
        crystal = sum(truth["crystalline_areas"])
        expected = crystal / (crystal + truth["amorphous_area"])
        self.assertAlmostEqual(truth["crystallinity"], expected, places=15)


if __name__ == "__main__":
    unittest.main()

