import unittest
import numpy as np
from tests.test_integration import SHAPE, geometry, expected_q
from saxs_waxs_core.integration import (AzimuthalIntegrationRecipe, PixelSplitting,
                                        detector_radial_array, integrate_azimuthal_1d)
from saxs_waxs_core.ruland_integration import (DEFAULT_RULAND_Q_POINTS, integrate_ruland_rings,
                                                suggest_ruland_sampling)
from dataclasses import replace


class RulandIntegrationTests(unittest.TestCase):
    def test_q_step_grid_preserves_requested_step_without_forced_endpoint(self):
        from saxs_waxs_core.ruland_integration import ruland_q_grid
        np.testing.assert_allclose(ruland_q_grid(.1, .31, .06), [.1, .16, .22, .28])
        np.testing.assert_allclose(ruland_q_grid(.1, .3, .1), [.1, .2, .3])
        for args in ((0, .3, .1), (.1, .3, 0), (.1, .3, .2), (.1, .3, .000001)):
            with self.assertRaises(ValueError):
                ruland_q_grid(*args)
    def test_sampling_suggestion_tracks_geometry_mask_and_supported_preference(self):
        first = geometry()
        second = replace(first, detector_distance_m=first.detector_distance_m / 4,
                         beam_center_x_px=first.beam_center_x_px + 13,
                         pixel_size_x_m=first.pixel_size_x_m * 1.3)
        q_first = detector_radial_array(first, SHAPE)
        q_second = detector_radial_array(second, SHAPE)
        mask = np.zeros(SHAPE, dtype=bool)
        mask[:, :SHAPE[1] // 3] = True
        low_1, high_1, width_1, source_1 = suggest_ruland_sampling(q_first, mask)
        low_2, high_2, width_2, source_2 = suggest_ruland_sampling(q_second, mask)
        self.assertGreater(low_2, low_1)
        self.assertGreater(high_2, high_1)
        self.assertGreater(width_2, width_1)
        self.assertEqual(source_1, source_2)
        preferred = (float(np.quantile(q_first[~mask], .25)),
                     float(np.quantile(q_first[~mask], .75)))
        self.assertEqual(suggest_ruland_sampling(q_first, mask, preferred=preferred)[:2], preferred)
        unsupported = suggest_ruland_sampling(q_second, mask, preferred=(.0001, .0002))
        self.assertNotEqual(unsupported[:2], (.0001, .0002))
        self.assertIn("preferred_unsupported", unsupported[3])
        self.assertGreater(low_1 - width_1 / 2, 0)
        self.assertLessEqual(width_1, (high_1 - low_1) / (DEFAULT_RULAND_Q_POINTS - 1))

    def test_multi_ring_matches_independent_integration_and_preserves_inputs(self):
        q = np.array([expected_q(60), expected_q(70)])
        width = expected_q(8)
        image = np.full(SHAPE, 5.0)
        mask = np.zeros(SHAPE, dtype=bool)
        mask[20:30, 20:30] = True
        original = image.copy()
        recipe = AzimuthalIntegrationRecipe(radial_range=(q[0]-width/2, q[0]+width/2),
            npt_azimuth=90, npt_radial=20, pixel_splitting=PixelSplitting.NONE)
        rings = integrate_ruland_rings(image, geometry(), recipe, q_centers=q, q_width=width, mask=mask)
        self.assertEqual(len(rings), 2)
        for ring in rings:
            self.assertIsNone(ring.error)
            independent = integrate_azimuthal_1d(image, geometry(),
                replace(recipe, radial_range=ring.radial_range), mask=mask)
            np.testing.assert_allclose(ring.curve.intensity, independent.intensity)
            self.assertEqual(ring.curve.audit["radial_range"], ring.radial_range)
        np.testing.assert_array_equal(image, original)

    def test_invalid_rings_are_rejected(self):
        recipe = AzimuthalIntegrationRecipe(radial_range=(.1, .2))
        with self.assertRaises(ValueError):
            integrate_ruland_rings(np.ones(SHAPE), geometry(), recipe, q_centers=[.1, .1], q_width=.01)
