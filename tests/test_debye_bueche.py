import unittest

import numpy as np

from saxs_waxs_core.debye_bueche import fit_debye_bueche


class DebyeBuecheTests(unittest.TestCase):
    def test_single_known_correlation_length_with_background(self):
        q = np.linspace(.01, .8, 250)
        y = 430 / (1 + (q * 12)**2)**2 + 4.5
        result = fit_debye_bueche(q, y, model="single", q_range=(.01, .8), q_unit="nm^-1")
        self.assertAlmostEqual(result.correlation_lengths[0], 12, places=4)
        self.assertAlmostEqual(result.constant_background, 4.5, places=4)
        self.assertAlmostEqual(result.amplitudes[0], 430, places=3)
        self.assertEqual(result.length_unit, "nm")
        self.assertTrue(result.background_fitted)
        self.assertLess(result.rmse, 1e-5)
        self.assertFalse(result.diagnostic_flags)

    def test_double_components_and_fixed_zero_background(self):
        q = np.linspace(.005, 1.0, 450)
        y = 700 / (1 + (q * 35)**2)**2 + 75 / (1 + (q * 3.5)**2)**2
        result = fit_debye_bueche(q, y, model="double", q_range=(.005, 1),
                                  q_unit="nm^-1", constant_background=False)
        np.testing.assert_allclose(result.correlation_lengths, (35, 3.5), rtol=1e-4)
        np.testing.assert_allclose(result.amplitudes, (700, 75), rtol=1e-4)
        self.assertEqual(result.constant_background, 0)
        self.assertFalse(result.background_fitted)
        self.assertFalse(result.diagnostic_flags)

    def test_rejects_missing_and_negative_data(self):
        q = np.linspace(.01, .8, 30)
        y = np.ones_like(q)
        y[5] = np.nan
        with self.assertRaisesRegex(ValueError, "finite"):
            fit_debye_bueche(q, y, model="single", q_range=(.01, .8), q_unit="nm^-1")
        y[5] = -.1
        with self.assertRaisesRegex(ValueError, "negative"):
            fit_debye_bueche(q, y, model="single", q_range=(.01, .8), q_unit="nm^-1")

    def test_requires_explicit_range_and_model(self):
        q = np.linspace(.01, .8, 30)
        y = np.ones_like(q)
        with self.assertRaisesRegex(ValueError, "model"):
            fit_debye_bueche(q, y, model="unknown", q_range=(.01, .8), q_unit="nm^-1")
        with self.assertRaisesRegex(ValueError, "q range"):
            fit_debye_bueche(q, y, model="single", q_range=(.8, .01), q_unit="nm^-1")

    def test_double_cannot_cancel_components_with_negative_background(self):
        q = np.linspace(.25, 1.0, 150)
        y = 120 / (1 + (q * 22)**2)**2 + 2.5
        result = fit_debye_bueche(q, y, model="double", q_range=(.25, 1),
                                  q_unit="nm^-1")
        self.assertGreaterEqual(result.constant_background, 0)
        self.assertIn("range_does_not_span_knee", result.diagnostic_flags)


if __name__ == "__main__":
    unittest.main()
