import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from saxs_waxs_core.auto_azimuth import fit_all_automatic_azimuth, fit_automatic_azimuth
import saxs_waxs_core.auto_azimuth as automatic
from saxs_waxs_core.peak_fitting import PeakFitError
from saxs_waxs_core.herman import from_single_peak_fit


def curve(peaks, masked=None):
    x = np.arange(0.5, 360, 1.0)
    y = np.full_like(x, 4.0)
    for center, amplitude, width in peaks:
        delta = (x - center + 180) % 360 - 180
        y += amplitude * np.exp(-0.5 * (delta / width) ** 2)
    count = np.ones_like(x)
    if masked is not None:
        count[masked] = 0
    return SimpleNamespace(kind="azimuthal_1d", x_unit="deg", intensity_unit="counts",
                           x=x, intensity=y, count=count)


class AutomaticAzimuthTests(unittest.TestCase):
    def test_comparable_peaks_share_one_model(self):
        data = curve([(88, 100, 3), (268, 90, 5)])
        fits, failures = fit_all_automatic_azimuth(data, shared_model=True)
        self.assertFalse(failures)
        self.assertEqual(len(fits), 2)
        self.assertEqual(len({item.result.model for item in fits}), 1)
        factors = [from_single_peak_fit(data, item.result, domain="model_full_90").f_fit for item in fits]
        self.assertGreater(factors[0], factors[1])
    def test_multiple_arbitrary_and_cross_zero_peaks(self):
        data = curve([(0, 100, 7), (137, 80, 9)])
        result, failures = fit_all_automatic_azimuth(data)
        self.assertFalse(failures)
        self.assertEqual(len(result), 2)
        centers = [fit.result.parameters["center"].value % 360 for fit in result]
        self.assertTrue(any(abs((x + 180) % 360 - 180) < 1 for x in centers))
        self.assertTrue(any(abs(x - 137) < 1 for x in centers))
        cross = next(fit for fit in result if abs((fit.peak_angle + 180) % 360 - 180) < 1)
        self.assertIsNotNone(from_single_peak_fit(data, cross.result, domain="selected").f_fit)

    def test_constant_curve_requires_manual_review(self):
        with self.assertRaises(PeakFitError):
            fit_all_automatic_azimuth(curve([]))

    def test_occluded_peak_is_reported_not_filled(self):
        data = curve([(90, 100, 8), (270, 70, 8)], masked=slice(87, 93))
        results, failures = fit_all_automatic_azimuth(data)
        self.assertTrue(any("遮挡" in reason for _, reason in failures))
        self.assertTrue(any(abs(fit.peak_angle - 270) < 2 for fit in results))

    def test_peak_threshold_can_reveal_weaker_peak(self):
        data = curve([(90, 100, 8), (250, 2, 8)])
        default, _ = fit_all_automatic_azimuth(data)
        sensitive, _ = fit_all_automatic_azimuth(data, prominence_fraction=0.01)
        self.assertEqual(len(default), 1)
        self.assertEqual(len(sensitive), 2)

    def test_shared_detection_matches_individual_peak_results(self):
        data = curve([(15, 100, 7), (137, 80, 9), (260, 70, 6)])
        individual = [fit_automatic_azimuth(data, peak_rank=i) for i in range(3)]
        with patch.object(automatic, "_prepare_azimuth", wraps=automatic._prepare_azimuth) as prepare:
            results, failures = fit_all_automatic_azimuth(data)
        self.assertEqual(prepare.call_count, 1)
        self.assertFalse(failures)
        for expected, actual in zip(individual, results):
            self.assertEqual(expected.peak_angle, actual.peak_angle)
            self.assertEqual(expected.result.model, actual.result.model)
            np.testing.assert_array_equal(expected.result.best_fit_y, actual.result.best_fit_y)

    def test_selected_fit_warnings_are_preserved_for_automatic_export(self):
        candidate = SimpleNamespace(model="gaussian_area", metrics={"success": True, "aic": 1.0},
                                    warnings=("parameter center is at its upper bound",))
        comparison = SimpleNamespace(ranked_by_aic=[candidate])
        with patch.object(automatic, "compare_peak_models", return_value=comparison):
            fit = fit_automatic_azimuth(curve([(90, 100, 8)]))
        self.assertTrue(any("center" in warning and "bound" in warning for warning in fit.warnings))


if __name__ == "__main__":
    unittest.main()
