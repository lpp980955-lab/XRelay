import unittest

import numpy as np
from scipy.integrate import quad

from saxs_waxs_core.herman import calculate_herman, from_single_peak_fit, compare_peaks
from saxs_waxs_core.peak_fitting import fit_peak
from types import SimpleNamespace
from dataclasses import replace


class HermanTests(unittest.TestCase):
    def test_model_domain_matches_independent_quadrature_and_is_not_preview_range(self):
        from saxs_waxs_core.peak_fitting import FitParameter
        x = np.linspace(70, 110, 81)
        y = 10 + 100*np.exp(-.5*((x-89)/2.8)**2)
        fit = fit_peak(x, y, model="gaussian_height", background="constant", x_unit="deg")
        curve = SimpleNamespace(kind="azimuthal_1d", x=x, intensity=y, count=np.ones(x.size))
        result = from_single_peak_fit(curve, fit, domain="model_full_90")
        self.assertIsNotNone(result.f_fit)
        self.assertIsNone(result.f_points)
        self.assertIn("模型外推", result.message)
        w, fraction = 6.56390108, .834231802
        params = dict(fit.parameters)
        for key, value in (("area", 15257.111), ("fwhm", w), ("fraction", fraction)):
            params[key] = FitParameter(value, None)
        mixed = replace(fit, model="pseudo_voigt_area", parameters=params)
        actual = from_single_peak_fit(curve, mixed, domain="model_full_90").f_fit
        def profile(t):
            a = np.degrees(t)
            return ((1-fraction)*2*np.sqrt(np.log(2))/(w*np.sqrt(np.pi))*np.exp(-4*np.log(2)*(a/w)**2)
                    + fraction*(w/2)/(np.pi*(a*a+(w/2)**2)))
        den = quad(lambda t: profile(t)*np.sin(t), 0, np.pi/2, epsabs=1e-11)[0]
        num = quad(lambda t: profile(t)*np.sin(t)*np.cos(t)**2, 0, np.pi/2, epsabs=1e-11)[0]
        self.assertAlmostEqual(actual, (3*num/den-1)/2, places=6)
        self.assertAlmostEqual(actual, .693027097, places=6)

    def setUp(self):
        self.x = np.arange(-90.0, 90.01, 0.1)
        self.peak = np.ones_like(self.x)
        self.bg = np.full_like(self.x, 2.0)

    def run_case(self, x, raw, fit, bg):
        return calculate_herman(x, raw, fit, bg, center_deg=0.0, max_gap_deg=0.100001)

    def test_isotropic_distribution_is_zero(self):
        result = self.run_case(self.x, self.peak + self.bg, self.peak, self.bg)
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.f_fit, 0.0, places=5)
        self.assertAlmostEqual(result.f_points, 0.0, places=5)

    def test_gap_and_negative_points_are_not_hidden(self):
        raw = self.peak + self.bg
        raw[800] = np.nan
        self.assertEqual(self.run_case(self.x, raw, self.peak, self.bg).status, "insufficient_domain")
        raw[800] = 1.0
        result = self.run_case(self.x, raw, self.peak, self.bg)
        self.assertEqual(result.status, "negative_intensity")
        self.assertIsNotNone(result.f_fit)
        self.assertIsNone(result.f_points)
        self.assertEqual(result.negative_points, 1)

    def test_periodic_peak_center(self):
        x = (self.x + 359.0) % 360.0
        result = calculate_herman(x, self.peak + self.bg, self.peak, self.bg,
                                  center_deg=359.0, max_gap_deg=0.100001)
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.f_points, 0.0, places=5)

    def test_selected_domain_is_explicit_and_matches_integral(self):
        x = np.arange(-30.0, 30.01, 0.1)
        signal = np.ones_like(x)
        result = calculate_herman(x, signal, signal, np.zeros_like(x),
                                  center_deg=0.0, max_gap_deg=0.100001,
                                  domain="selected")
        c = np.cos(np.deg2rad(30.0))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.domain, "selected")
        self.assertAlmostEqual(result.f_fit, (c + c*c) / 2, places=5)
        self.assertEqual(calculate_herman(x, signal, signal, np.zeros_like(x),
                         center_deg=0.0, max_gap_deg=0.100001).status, "insufficient_domain")

    def test_selected_domain_outside_90_is_not_silently_trimmed(self):
        x = np.arange(-95., 90.1, 1.)
        signal = np.ones_like(x)
        result = calculate_herman(x, signal, signal, np.zeros_like(x),
                                  center_deg=0., max_gap_deg=1.01, domain="selected")
        self.assertEqual(result.status, "outside_domain")
        self.assertIsNone(result.f_fit)
        self.assertEqual(result.coverage_deg, (-95., 90.))

    def test_fit_adapter_preserves_masked_bin(self):
        x = np.arange(60.0, 120.1, 1.0)
        y = 2.0 + 20.0 * np.exp(-0.5 * ((x - 90.0) / 8.0) ** 2)
        count = np.ones_like(x)
        fit = fit_peak(x, y, x_range=(60.0, 120.0), x_unit="deg")
        curve = SimpleNamespace(kind="azimuthal_1d", x=x, intensity=y, count=count)
        self.assertEqual(from_single_peak_fit(curve, fit).status, "ok")
        count[20] = 0
        self.assertEqual(from_single_peak_fit(curve, fit).status, "insufficient_domain")
        curve.kind = "radial_1d"
        with self.assertRaises(ValueError):
            from_single_peak_fit(curve, fit)
        curve.kind = "azimuthal_1d"
        with self.assertRaises(ValueError):
            from_single_peak_fit(curve, replace(fit, metrics={"success": False}))

    def test_duplicate_angles_are_rejected(self):
        x = np.array([-30.0, -10.0, 0.0, 0.0, 10.0, 30.0])
        signal = np.ones_like(x)
        result = calculate_herman(x, signal, signal, np.zeros_like(x),
                                  center_deg=0.0, max_gap_deg=20.1,
                                  domain="selected")
        self.assertEqual(result.status, "duplicate_angles")
        self.assertIsNone(result.f_fit)

    def test_comparison_requires_explicit_average_and_preserves_failure(self):
        x = np.arange(-30.0, 30.01, 0.1)
        a = np.ones_like(x)
        b = np.exp(-0.5 * (x / 12.0) ** 2)
        first = calculate_herman(x, a, a, np.zeros_like(x), center_deg=0,
                                  max_gap_deg=0.100001, domain="selected")
        second = calculate_herman(x, b, b, np.zeros_like(x), center_deg=0,
                                   max_gap_deg=0.100001, domain="selected")
        self.assertIsNone(compare_peaks(first, second).mean_fit)
        compared = compare_peaks(first, second, average=True)
        self.assertAlmostEqual(compared.mean_fit, (first.f_fit + second.f_fit) / 2)
        failed = replace(second, status="insufficient_domain", f_fit=None, f_points=None)
        self.assertIsNone(compare_peaks(first, failed, average=True).mean_fit)

    def test_asymmetric_selected_peak_matches_independent_quadrature(self):
        x = np.linspace(-35.0, 55.0, 9001)
        signal = np.exp(-0.5 * (x / np.where(x < 0, 9.0, 17.0)) ** 2)
        result = calculate_herman(x, signal, signal, np.zeros_like(x),
                                  center_deg=0.0, max_gap_deg=0.010001,
                                  domain="selected")
        def integrand(angle, numerator):
            width = 9.0 if angle < 0 else 17.0
            theta = np.deg2rad(abs(angle))
            intensity = np.exp(-0.5 * (angle / width) ** 2)
            return intensity * np.sin(theta) * (np.cos(theta) ** 2 if numerator else 1.0)
        num = quad(lambda angle: integrand(angle, True), -35, 55, points=[0])[0]
        den = quad(lambda angle: integrand(angle, False), -35, 55, points=[0])[0]
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.f_fit, (3 * num / den - 1) / 2, places=7)

    def test_center_between_bins_uses_zero_weight_node(self):
        x = np.arange(-89.5, 90.0, 1.0)
        signal = np.ones_like(x)
        result = calculate_herman(x, signal, signal, np.zeros_like(x),
                                  center_deg=0, max_gap_deg=1.001, domain="full_90")
        c = np.cos(np.deg2rad(89.5))
        exact = (c + c*c) / 2
        theta = np.deg2rad(np.abs(x))
        grid = np.insert(x, len(x) // 2, 0.0)
        weighted = np.insert(np.sin(theta), len(x) // 2, 0.0)
        num = np.insert(np.sin(theta) * np.cos(theta) ** 2, len(x) // 2, 0.0)
        expected = (3 * np.trapezoid(num, grid) / np.trapezoid(weighted, grid) - 1) / 2
        self.assertEqual(result.status, "ok")
        self.assertAlmostEqual(result.f_fit, expected, places=12)
        self.assertLess(abs(result.f_fit - exact), 3e-5)


if __name__ == "__main__":
    unittest.main()
