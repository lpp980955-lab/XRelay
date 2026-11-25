import unittest
from dataclasses import replace
import numpy as np
from saxs_waxs_core.integration import IntegrationCurve
from saxs_waxs_core.ruland_series import fit_ruland_series, measured_integral_breadth


class RulandSeriesTests(unittest.TestCase):
    def test_integral_breadth_requires_full_valid_domain(self):
        x = np.arange(.5, 360, 1)
        y = 3 + 2*np.exp(-.5*((x-90)/10)**2)
        curve = IntegrationCurve(kind="azimuthal_1d", x=x, intensity=y,
            sigma=np.full(x.size, np.nan), count=np.ones(x.size), x_unit="deg",
            intensity_unit="count", audit={})
        breadth = measured_integral_breadth(curve, 90, 3)
        self.assertAlmostEqual(breadth, np.sqrt(2*np.pi)*np.radians(10), delta=.001)
        count = curve.count.copy()
        count[20] = 0
        with self.assertRaises(ValueError):
            measured_integral_breadth(replace(curve, count=count), 90, 3)
        with self.assertRaises(ValueError):
            measured_integral_breadth(curve, 90, 3.1)
    def test_gaussian_slices_recover_length_and_report_failure(self):
        q = np.linspace(.15, .4, 8)
        x = np.arange(.5, 360, 1)
        slices = []
        for i, qi in enumerate(q):
            width = np.sqrt((1 / (100 * qi))**2 + .03**2)
            sigma = np.degrees(width) / (2 * np.sqrt(2 * np.log(2)))
            y = 1 + 20 * np.exp(-.5 * ((x - 90) / sigma)**2)
            curve = IntegrationCurve(kind="azimuthal_1d", x=x, intensity=y,
                sigma=np.full(x.size, np.nan), count=np.ones(x.size), x_unit="deg",
                intensity_unit="count", audit={})
            slices.append((str(i), qi, curve))
        result = fit_ruland_series(slices, peak_center_deg=90, half_window_deg=25,
            model="gaussian", width_definition="FWHM")
        self.assertIsNone(result.error)
        self.assertAlmostEqual(result.result.length_nm, 100, places=4)
        count = slices[2][2].count.copy()
        count[90] = 0
        slices[2] = (slices[2][0], slices[2][1], replace(slices[2][2], count=count))
        failed = fit_ruland_series(slices, peak_center_deg=90, half_window_deg=25,
            model="gaussian", width_definition="FWHM")
        self.assertIsNone(failed.result)
        self.assertIsNotNone(failed.slices[2].error)
