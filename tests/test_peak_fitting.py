from __future__ import annotations

import math
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saxs_waxs_core.peak_fitting import (  # noqa: E402
    PeakFitError,
    PeakModelComparison,
    compare_peak_models,
    fit_multi_peak,
    fit_peak,
)


class PeakFittingTests(unittest.TestCase):
    def test_gaussian_height_recovers_truth_and_uncertainty_contract(self):
        x = np.linspace(-4.0, 5.0, 901)
        y = 2.5 + 0.12 * (x - 0.5) + 17.0 * np.exp(
            -0.5 * ((x - 0.8) / 0.65) ** 2
        )
        result = fit_peak(
            x,
            y,
            model="gaussian_height",
            background="linear",
            x_range=(-2.5, 3.5),
            x_unit="deg",
            y_unit="count/s",
        )
        self.assertTrue(result.metrics["success"])
        self.assertAlmostEqual(result.parameters["center"].value, 0.8, places=10)
        self.assertAlmostEqual(result.parameters["sigma"].value, 0.65, places=10)
        self.assertAlmostEqual(result.parameters["height"].value, 17.0, places=10)
        self.assertAlmostEqual(
            result.parameters["fwhm"].value,
            2.0 * math.sqrt(2.0 * math.log(2.0)) * 0.65,
            places=10,
        )
        self.assertEqual(result.input_counts["excluded_as_outlier"], 0)
        self.assertEqual(result.to_document()["scientific_status"], "technical_fit_only")

    def test_nonfinite_points_are_explicitly_counted_not_interpolated(self):
        x = np.linspace(0.0, 10.0, 201)
        y = 1.0 + 8.0 * np.exp(-0.5 * ((x - 4.0) / 0.7) ** 2)
        y[30:34] = np.nan
        result = fit_peak(x, y, x_range=(1.0, 8.0))
        self.assertEqual(result.input_counts["excluded_nonfinite_in_range"], 4)
        self.assertTrue(any("non-finite" in item for item in result.warnings))
        self.assertFalse(np.any(np.isin(result.used_x, x[30:34])))

    def test_candidate_comparison_ranks_but_does_not_claim_scientific_choice(self):
        x = np.linspace(-6.0, 6.0, 601)
        sigma = 0.9
        area = 24.0
        y = 0.7 + area * sigma / (math.pi * ((x - 1.1) ** 2 + sigma**2))
        comparison = compare_peak_models(x, y, background="constant")
        self.assertEqual(comparison.ranked_by_aic[0].model, "lorentzian_area")
        self.assertEqual(len(comparison.results), 4)

    def test_multi_peak_comparison_keeps_peak_count_and_constraints(self):
        x = np.linspace(5.0, 35.0, 301)
        y = (2.0 + 8.0 * np.exp(-0.5 * ((x - 12.0) / 1.0) ** 2)
             + 6.0 * np.exp(-0.5 * ((x - 25.0) / 1.5) ** 2))
        comparison = compare_peak_models(x, y, models=("gaussian_height", "lorentzian_area"),
            peak_count=2, background="constant", x_range=(7.0, 31.0),
            center_bounds=((10.0, 14.0), (23.0, 27.0)), x_unit="q_nm^-1", y_unit="count")
        self.assertEqual(len(comparison.results), 2)
        self.assertEqual(comparison.ranked_by_aic[0].model, "gaussian_height_x2")
        for result in comparison.results:
            self.assertEqual(result.requested_range, (7.0, 31.0))
            self.assertEqual(len(result.components), 2)
            self.assertEqual(result.x_unit, "q_nm^-1")
        with self.assertRaises(PeakFitError):
            compare_peak_models(x, y, peak_count=0)

    def test_comparison_does_not_rank_nonconvergence_ahead_of_valid_fit(self):
        x = np.linspace(-4, 4, 101)
        y = 1 + 10*np.exp(-.5*(x/.8)**2)
        good = fit_peak(x, y, model="gaussian_height")
        failed = replace(good, model="unconverged_candidate",
            metrics={**good.metrics, "success": False, "aic": -1000.0})
        nonfinite = replace(good, model="nonfinite_candidate",
            metrics={**good.metrics, "aic": math.nan})
        comparison = PeakModelComparison((failed, nonfinite, good), {})
        self.assertEqual([item.model for item in comparison.ranked_by_aic],
                         [good.model, nonfinite.model, failed.model])

    def test_invalid_range_and_nonpeak_are_rejected(self):
        x = np.linspace(0.0, 1.0, 20)
        with self.assertRaises(PeakFitError):
            fit_peak(x, np.ones_like(x), x_range=(1.0, 0.0))
        with self.assertRaises(PeakFitError):
            fit_peak(x, np.zeros_like(x), background="none")

    def test_added_asymmetric_and_pearson_models_converge(self):
        x = np.linspace(-6.0, 6.0, 601)
        y = 1.2 + 15.0 * np.exp(-0.5 * ((x - 0.4) / 0.8) ** 2)
        for model in (
            "pearson7_area",
            "split_gaussian_area",
            "split_lorentzian_area",
        ):
            result = fit_peak(x, y, model=model, background="constant")
            self.assertTrue(result.metrics["success"], model)
            self.assertIn("fwhm", result.parameters)

    def test_two_peak_decomposition_reports_each_peak_and_components(self):
        x = np.linspace(5.0, 35.0, 1201)
        y = (
            3.0
            + 55.0 * np.exp(-0.5 * ((x - 14.0) / 0.75) ** 2)
            + 36.0 * np.exp(-0.5 * ((x - 24.0) / 1.15) ** 2)
        )
        result = fit_multi_peak(
            x,
            y,
            peak_count=2,
            model="gaussian_height",
            background="constant",
            x_unit="deg",
            y_unit="count/s",
        )
        self.assertTrue(result.metrics["success"])
        self.assertAlmostEqual(result.parameters["peak_1_center"].value, 14.0, places=4)
        self.assertAlmostEqual(result.parameters["peak_2_center"].value, 24.0, places=4)
        self.assertIn("peak_1_fwhm", result.parameters)
        self.assertEqual(set(result.components), {"peak_1", "peak_2"})
        self.assertFalse(any("FWHM intervals overlap" in warning for warning in result.warnings))

    def test_overlapping_multi_peak_components_require_area_review(self):
        x = np.linspace(5.0, 18.0, 651)
        y = (2.0 + 20.0 * np.exp(-0.5 * ((x - 10.0) / 1.3) ** 2)
             + 16.0 * np.exp(-0.5 * ((x - 12.0) / 1.3) ** 2))
        result = fit_multi_peak(x, y, peak_count=2, model="gaussian_height",
            center_bounds=((9.5, 10.5), (11.5, 12.5)), background="constant")
        self.assertTrue(result.metrics["success"])
        self.assertTrue(any("FWHM intervals overlap" in warning for warning in result.warnings))

    def test_mixed_peak_models_recover_known_components(self):
        x = np.linspace(5, 35, 1001)
        y = 2 + 20*np.exp(-.5*((x-12)/.7)**2) + 15*1.3/(np.pi*((x-25)**2+1.3**2))
        result = fit_multi_peak(x, y, peak_count=2,
                               models=("gaussian_height", "lorentzian_area"))
        self.assertTrue(result.metrics["success"])
        self.assertEqual(result.model, "mixed:gaussian_height,lorentzian_area")
        self.assertAlmostEqual(result.parameters["peak_1_center"].value, 12, places=3)
        self.assertAlmostEqual(result.parameters["peak_2_center"].value, 25, places=3)
        with self.assertRaises(ValueError):
            fit_multi_peak(x, y, peak_count=2, models=("gaussian_height",))

    def test_per_peak_center_bounds_and_invalid_bounds(self):
        x = np.linspace(5, 35, 1001)
        y = 2 + 20*np.exp(-.5*((x-12)/.7)**2) + 15*1.3/(np.pi*((x-25)**2+1.3**2))
        fit = fit_multi_peak(x, y, peak_count=2, models=("gaussian_height", "lorentzian_area"),
                             center_bounds=((10, 14), (23, 27)))
        self.assertAlmostEqual(fit.parameters["peak_1_center"].value, 12, places=3)
        self.assertEqual(fit.parameters["peak_1_center"].lower_bound, 10)
        self.assertEqual(fit.parameters["peak_2_center"].upper_bound, 27)
        constrained = fit_multi_peak(x, y, peak_count=2,
                                     models=("gaussian_height", "lorentzian_area"),
                                     center_bounds=((10, 11), (23, 27)))
        self.assertTrue(any("peak_1_center" in warning and "bounds" in warning
                            for warning in constrained.warnings))
        with self.assertRaises(ValueError):
            fit_multi_peak(x, y, peak_count=2, center_bounds=((2, 8), (23, 27)))

    def test_fwhm_constraints_use_model_specific_width(self):
        x = np.linspace(5, 35, 1001)
        y = 2 + 20*np.exp(-.5*((x-12)/.7)**2) + 15*1.3/(np.pi*((x-25)**2+1.3**2))
        fit = fit_multi_peak(x, y, peak_count=2, models=("gaussian_height", "lorentzian_area"),
                             center_bounds=((10, 14), (23, 27)), fwhm_bounds=((1.0, 2.0), (2.0, 3.0)))
        self.assertAlmostEqual(fit.parameters["peak_1_fwhm"].value, 2.354820045*.7, places=3)
        self.assertAlmostEqual(fit.parameters["peak_2_fwhm"].value, 2.6, places=3)
        with self.assertRaises(ValueError):
            fit_multi_peak(x, y, peak_count=2, models=("voigt_area", "gaussian_area"),
                           fwhm_bounds=((1, 2), None))


if __name__ == "__main__":
    unittest.main()
