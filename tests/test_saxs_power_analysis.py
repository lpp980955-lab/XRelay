import unittest
import numpy as np
from scipy.stats import linregress

from saxs_waxs_core.saxs_power_analysis import fit_saxs_power


class PowerAnalysisTests(unittest.TestCase):
    def test_guinier_truth_and_range_warning(self):
        q = np.linspace(0.01, 0.15, 100)
        intensity = 100 * np.exp(-(q * 8) ** 2 / 3)
        result = fit_saxs_power(q, intensity, method="guinier", q_range=(0.01, 0.15), q_unit="nm^-1")
        self.assertAlmostEqual(result.derived_value, 8, places=10)
        self.assertAlmostEqual(result.r_squared, 1)

    def test_porod_truth(self):
        q = np.linspace(0.1, 1, 100)
        result = fit_saxs_power(q, 3 * q ** -4, method="porod", q_range=(0.1, 1), q_unit="A^-1")
        self.assertAlmostEqual(result.derived_value, 4, places=10)
        np.testing.assert_allclose(result.residual, 0, atol=1e-10)

    def test_invalid_or_nonpositive_input_is_not_silently_removed(self):
        q = np.linspace(0.1, 1, 10)
        intensity = np.ones(10)
        intensity[3] = 0
        with self.assertRaises(ValueError):
            fit_saxs_power(q, intensity, method="guinier", q_range=(0.1, 1), q_unit="nm^-1")
        with self.assertRaises(ValueError):
            fit_saxs_power(q, np.ones(10), method="porod", q_range=(0.1, 1), q_unit="2theta")

    def test_guinier_uncertainty_and_zero_intensity(self):
        q = np.linspace(.01, .15, 100)
        intensity = 100 * np.exp(-(q * 8)**2/3 + .001*np.sin(np.arange(len(q))))
        result = fit_saxs_power(q, intensity, method="guinier", q_range=(.01, .15), q_unit="nm^-1")
        reference = linregress(q*q, np.log(intensity))
        self.assertAlmostEqual(result.slope_stderr, reference.stderr)
        self.assertAlmostEqual(result.zero_intensity, np.exp(reference.intercept))
        self.assertAlmostEqual(result.derived_stderr, 3*reference.stderr/(2*result.derived_value))
        self.assertAlmostEqual(result.zero_intensity_stderr, result.zero_intensity*reference.intercept_stderr)
