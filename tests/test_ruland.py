import unittest
import numpy as np
from scipy.stats import linregress
from saxs_waxs_core.ruland import fit_ruland, check_ruland_range_sensitivity


class RulandTests(unittest.TestCase):
    def test_cauchy_and_gaussian_truth(self):
        q = np.linspace(0.08, 0.42, 18)
        c = 0.041 + 2 * np.pi / (86 * q)
        g = np.sqrt((1 / (93 * q)) ** 2 + 0.036**2)
        for model, widths, length, angle in (("cauchy", c, 86, .041), ("gaussian", g, 93, .036)):
            result = fit_ruland(q / 10, np.degrees(widths), model=model,
                                width_definition="source_B", q_unit="A^-1", width_unit="deg")
            self.assertAlmostEqual(result.length_nm, length, places=9)
            self.assertAlmostEqual(result.misorientation_rad, angle, places=9)
            self.assertEqual(len(result.q_nm_inv), 18)

    def test_selection_is_explicit_and_invalid_not_silently_dropped(self):
        q = np.array([.1, .2, .3, .4])
        widths = np.array([1.0, .5, .4, -1])
        with self.assertRaises(ValueError):
            fit_ruland(q, widths, model="cauchy", width_definition="FWHM", q_unit="nm^-1", width_unit="rad")
        result = fit_ruland(q, widths, selected=np.array([True, True, True, False]),
                            model="cauchy", width_definition="FWHM", q_unit="nm^-1", width_unit="rad")
        self.assertEqual(len(result.q_nm_inv), 3)
        self.assertEqual(result.width_definition, "FWHM")

    def test_width_convention_changes_length_and_angle(self):
        q = np.linspace(.08, .42, 18)
        for model, widths in (("cauchy", .041 + 2*np.pi/(86*q)),
                              ("gaussian", np.sqrt((1/(93*q))**2 + .036**2))):
            full = fit_ruland(q, widths, model=model, width_definition="FWHM",
                              q_unit="nm^-1", width_unit="rad")
            half = fit_ruland(q, widths / 2, model=model, width_definition="HWHM",
                              q_unit="nm^-1", width_unit="rad")
            self.assertAlmostEqual(half.length_nm, full.length_nm * 2, places=8)
            self.assertAlmostEqual(half.misorientation_rad, full.misorientation_rad / 2, places=8)

    def test_pauw_integral_breadth_uses_distinct_gaussian_coefficient(self):
        q = np.linspace(.08, .42, 18)
        widths = np.sqrt((2*np.pi/(93*q))**2 + .036**2)
        result = fit_ruland(q, widths, model="gaussian", width_definition="integral_breadth",
                            q_unit="nm^-1", width_unit="rad", formula_version="pauw_2010")
        self.assertAlmostEqual(result.length_nm, 93, places=8)
        with self.assertRaises(ValueError):
            fit_ruland(q, widths, model="gaussian", width_definition="FWHM",
                       q_unit="nm^-1", width_unit="rad", formula_version="pauw_2010")

    def test_regression_uncertainty_matches_scipy_and_derivatives(self):
        q = np.linspace(.08, .4, 18)
        width = .04 + 2*np.pi/(86*q) + .001*np.sin(np.arange(len(q)))
        result = fit_ruland(q, width, model="cauchy", width_definition="FWHM",
                            q_unit="nm^-1", width_unit="rad")
        reference = linregress(1/q, width)
        self.assertAlmostEqual(result.slope_stderr, reference.stderr, places=12)
        self.assertAlmostEqual(result.intercept_stderr, reference.intercept_stderr, places=12)
        self.assertAlmostEqual(result.length_stderr_nm,
                               result.length_nm * reference.stderr / reference.slope, places=10)
        self.assertAlmostEqual(result.misorientation_stderr_rad, reference.intercept_stderr, places=12)

    def test_range_sensitivity_compares_without_changing_input(self):
        q = np.linspace(.08, .4, 8)
        width = .04 + 2*np.pi/(86*q)
        fit = fit_ruland(q, width, model="cauchy", width_definition="FWHM",
                         q_unit="nm^-1", width_unit="rad")
        checks = check_ruland_range_sensitivity(fit)
        self.assertEqual([item.point_count for item in checks], [8, 7, 7, 6])
        self.assertTrue(all(abs(item.result.length_nm - 86) < 1e-8 for item in checks))
        np.testing.assert_array_equal(fit.q_nm_inv, q)
        small = fit_ruland(q[:3], width[:3], model="cauchy", width_definition="FWHM",
                           q_unit="nm^-1", width_unit="rad")
        self.assertTrue(all(item.error for item in check_ruland_range_sensitivity(small)[1:]))
