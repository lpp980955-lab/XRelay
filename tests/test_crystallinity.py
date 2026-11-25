import unittest
from dataclasses import replace
import math
import numpy as np
from saxs_waxs_core.crystallinity import crystallinity_from_fit
from saxs_waxs_core.peak_fitting import PeakFitResult, FitParameter, fit_multi_peak


class CrystallinityTests(unittest.TestCase):
    def setUp(self):
        x = np.linspace(10, 30, 101)
        components = {"peak_1": np.full_like(x, 2.0), "peak_2": np.full_like(x, 3.0)}
        self.fit = PeakFitResult(model="gaussian_area", background="constant",
            x_unit="deg", y_unit="count", requested_range=(10, 30),
            used_x=x, observed_y=np.full_like(x, 15), best_fit_y=np.full_like(x, 15),
            residual_y=np.zeros_like(x), parameters={}, metrics={},
            input_counts={"excluded_nonfinite_in_range": 0}, warnings=(), components=components)

    def test_finite_component_area_excludes_background(self):
        result = crystallinity_from_fit(self.fit, {"peak_1": "crystalline", "peak_2": "amorphous"})
        self.assertAlmostEqual(result.fraction, 0.4)
        self.assertAlmostEqual(result.crystalline_area, 40)
        self.assertAlmostEqual(result.amorphous_area, 60)
        self.assertEqual(result.integration_range, (10, 30))
        self.assertEqual(result.method_id, "F-002:component_area_ratio")

    def test_fit_warnings_follow_crystallinity_candidate(self):
        fit = replace(self.fit, warnings=("parameter peak_1_center is at its bound",))
        result = crystallinity_from_fit(fit, {"peak_1": "crystalline", "peak_2": "amorphous"})
        self.assertTrue(any("peak_1_center" in warning for warning in result.warnings))

    def test_missing_or_invalid_classification_rejected(self):
        for labels in ({"peak_1": "crystalline"},
                       {"peak_1": "crystalline", "peak_2": "crystalline"},
                       {"peak_1": "crystalline", "peak_2": "unknown"}):
            with self.assertRaises(ValueError):
                crystallinity_from_fit(self.fit, labels)

    def test_missing_data_rejected(self):
        self.fit.input_counts["excluded_nonfinite_in_range"] = 1
        with self.assertRaises(ValueError):
            crystallinity_from_fit(self.fit, {"peak_1": "crystalline", "peak_2": "amorphous"})

    def test_failed_fit_or_crossed_centers_rejected(self):
        labels = {"peak_1": "crystalline", "peak_2": "amorphous"}
        with self.assertRaises(ValueError):
            crystallinity_from_fit(replace(self.fit, metrics={"success": False}), labels)
        crossed = replace(self.fit, parameters={"peak_1_center": FitParameter(20, None),
                                                 "peak_2_center": FitParameter(10, None)})
        with self.assertRaisesRegex(ValueError, "crossed"):
            crystallinity_from_fit(crossed, labels)

    def test_actual_multi_peak_fit_is_accepted(self):
        x = np.linspace(5, 35, 301)
        y = 2 + 8 * np.exp(-0.5 * ((x - 12) / 1) ** 2) + 3 * np.exp(-0.5 * ((x - 25) / 2) ** 2)
        fit = fit_multi_peak(x, y, peak_count=2, model="gaussian_height", background="constant",
                             x_unit="deg", y_unit="count")
        result = crystallinity_from_fit(fit, {"peak_1": "crystalline", "peak_2": "amorphous"})
        self.assertAlmostEqual(result.fraction, 8 / (8 + 6), places=5)
        self.assertEqual(result.area_method, "adaptive_model_quadrature")

    def test_sparse_sampled_peaks_use_fitted_function_area(self):
        x = np.linspace(-5, 5, 11)
        narrow = np.exp(-0.5 * (x / .2) ** 2)
        parameters = {
            "peak_1_height": FitParameter(1, None), "peak_1_center": FitParameter(0, None),
            "peak_1_sigma": FitParameter(.2, None),
            "peak_2_height": FitParameter(.5, None), "peak_2_center": FitParameter(1, None),
            "peak_2_sigma": FitParameter(2, None),
        }
        # The broad component is deliberately centred at 1, so model parameters
        # and sampled component arrays must describe the same curve.
        broad = .5 * np.exp(-0.5 * ((x - 1) / 2) ** 2)
        fit = replace(self.fit, model="gaussian_height_x2", used_x=x,
                      observed_y=narrow + broad, best_fit_y=narrow + broad,
                      residual_y=np.zeros_like(x), parameters=parameters,
                      components={"peak_1": narrow, "peak_2": broad},
                      requested_range=(-5, 5))
        result = crystallinity_from_fit(fit, {"peak_1": "crystalline", "peak_2": "amorphous"})
        narrow_area = .2 * math.sqrt(2 * math.pi) * math.erf(5 / (.2 * math.sqrt(2)))
        broad_area = (.5 * 2 * math.sqrt(math.pi / 2) *
                      (math.erf((5 - 1) / (2 * math.sqrt(2))) -
                       math.erf((-5 - 1) / (2 * math.sqrt(2)))))
        self.assertAlmostEqual(result.fraction, narrow_area / (narrow_area + broad_area), places=7)
        self.assertEqual(result.area_method, "adaptive_model_quadrature")
        self.assertGreater(abs(float(np.trapezoid(narrow, x) / np.trapezoid(narrow + broad, x))
                               - result.fraction), .05)

    def test_mixed_gaussian_lorentzian_finite_areas(self):
        x = np.linspace(5, 35, 31)
        gaussian = 3 * np.exp(-.5 * ((x - 12) / .5) ** 2)
        lorentzian = 4 * 1.5 / (math.pi * ((x - 25) ** 2 + 1.5 ** 2))
        parameters = {
            "peak_1_height": FitParameter(3, None), "peak_1_center": FitParameter(12, None),
            "peak_1_sigma": FitParameter(.5, None),
            "peak_2_area": FitParameter(4, None), "peak_2_center": FitParameter(25, None),
            "peak_2_sigma": FitParameter(1.5, None),
        }
        fit = replace(self.fit, model="mixed:gaussian_height,lorentzian_area", used_x=x,
                      observed_y=gaussian + lorentzian, best_fit_y=gaussian + lorentzian,
                      residual_y=np.zeros_like(x), parameters=parameters,
                      components={"peak_1": gaussian, "peak_2": lorentzian},
                      requested_range=(5, 35))
        result = crystallinity_from_fit(fit, {"peak_1": "crystalline", "peak_2": "amorphous"})
        gaussian_area = (3 * .5 * math.sqrt(math.pi / 2) *
                         (math.erf((35 - 12) / (.5 * math.sqrt(2))) -
                          math.erf((5 - 12) / (.5 * math.sqrt(2)))))
        lorentzian_area = 4 / math.pi * (math.atan((35 - 25) / 1.5) -
                                         math.atan((5 - 25) / 1.5))
        self.assertAlmostEqual(result.crystalline_area, gaussian_area, places=8)
        self.assertAlmostEqual(result.amorphous_area, lorentzian_area, places=8)

    def test_parameter_component_disagreement_rejected(self):
        x = np.linspace(-2, 2, 21)
        parameters = {
            "peak_1_height": FitParameter(1, None), "peak_1_center": FitParameter(0, None),
            "peak_1_sigma": FitParameter(1, None),
            "peak_2_height": FitParameter(1, None), "peak_2_center": FitParameter(1, None),
            "peak_2_sigma": FitParameter(1, None),
        }
        fit = replace(self.fit, model="gaussian_height_x2", used_x=x,
                      components={"peak_1": np.ones_like(x), "peak_2": np.ones_like(x)},
                      parameters=parameters)
        with self.assertRaisesRegex(ValueError, "disagree"):
            crystallinity_from_fit(fit, {"peak_1": "crystalline", "peak_2": "amorphous"})
