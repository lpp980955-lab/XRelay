from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saxs_waxs_core.display import (  # noqa: E402
    DisplayPreset,
    normalize_for_display,
    resolve_display_limits,
)


class DisplayTests(unittest.TestCase):
    def test_buffer_reuse_preserves_reference_normalization_for_all_limit_modes(self):
        data = np.random.default_rng(7).normal(20,15,(43,51))
        data[0,0] = np.nan
        original = data.copy()
        mask = np.zeros(data.shape,dtype=bool)
        mask[::4,::3] = True
        for scale in ("linear","log"):
            for mode in ("minmax","percentile","manual"):
                with self.subTest(scale=scale,mode=mode):
                    preset=DisplayPreset(scale=scale,limit_mode=mode,
                        manual_vmin=2,manual_vmax=40)
                    valid=np.isfinite(data)&~mask
                    if scale=="log":
                        valid &= data>0
                    values=data[valid]
                    if mode=="manual":
                        low,high=2,40
                    elif mode=="minmax":
                        low,high=values.min(),values.max()
                    else:
                        low,high=np.percentile(values,[1,99.5])
                    expected=np.full(data.shape,np.nan)
                    if scale=="linear":
                        expected[valid]=np.clip((values-low)/(high-low),0,1)
                    else:
                        expected[valid]=np.clip((np.log(values)-np.log(low))/(np.log(high)-np.log(low)),0,1)
                    result=normalize_for_display(data,preset,mask)
                    np.testing.assert_array_equal(result.values,expected)
                    np.testing.assert_array_equal(data,original)

    def test_linear_normalization_does_not_mutate_input(self):
        data = np.array([[-2, -1, 0], [1, 2, 3]], dtype=np.int32)
        original = data.copy()
        result = normalize_for_display(data, DisplayPreset(limit_mode="minmax"))
        np.testing.assert_array_equal(data, original)
        self.assertEqual((result.vmin, result.vmax), (-2.0, 3.0))
        self.assertFalse(result.values.flags.writeable)
        self.assertAlmostEqual(float(result.values[0, 0]), 0.0)
        self.assertAlmostEqual(float(result.values[-1, -1]), 1.0)

    def test_log_scale_marks_nonpositive_pixels_invalid(self):
        data = np.array([[-2.0, 0.0, 1.0], [10.0, 100.0, np.nan]])
        result = normalize_for_display(data, DisplayPreset(scale="log", limit_mode="minmax"))
        np.testing.assert_array_equal(result.valid, [[False, False, True], [True, True, False]])
        self.assertTrue(np.isnan(result.values[0, 0]))
        self.assertAlmostEqual(float(result.values[0, 2]), 0.0)
        self.assertAlmostEqual(float(result.values[1, 1]), 1.0)

    def test_mask_is_excluded_from_auto_limits(self):
        data = np.array([[1.0, 2.0], [3.0, 9999.0]])
        mask = np.array([[False, False], [False, True]])
        self.assertEqual(resolve_display_limits(data, DisplayPreset(limit_mode="minmax"), mask), (1.0, 3.0))

    def test_percentile_limits(self):
        data = np.arange(101, dtype=np.float64).reshape(1, 101)
        preset = DisplayPreset(limit_mode="percentile", lower_percentile=10, upper_percentile=90)
        self.assertEqual(resolve_display_limits(data, preset), (10.0, 90.0))

    def test_manual_log_rejects_nonpositive_lower_limit(self):
        with self.assertRaises(ValueError):
            DisplayPreset(scale="log", limit_mode="manual", manual_vmin=0.0, manual_vmax=10.0)

    def test_constant_image_gets_nonzero_display_span(self):
        data = np.full((4, 4), 7.0)
        vmin, vmax = resolve_display_limits(data, DisplayPreset(limit_mode="minmax"))
        self.assertLess(vmin, 7.0)
        self.assertGreater(vmax, 7.0)

    def test_colormap_inversion_is_explicit(self):
        preset = DisplayPreset(colormap="magma", invert_colormap=True)
        self.assertEqual(preset.effective_colormap, "magma_r")

    def test_custom_colormap_is_auditable_and_validated(self):
        preset = DisplayPreset(
            colormap="custom",
            custom_colors=("#000000", "#3366CC", "#FFFFFF"),
            invert_colormap=True,
        )
        self.assertEqual(preset.effective_colormap, "custom")
        self.assertEqual(preset.custom_colors[1], "#3366CC")
        with self.assertRaisesRegex(ValueError, "at least two"):
            DisplayPreset(colormap="custom", custom_colors=("#000000",))
        with self.assertRaisesRegex(ValueError, "#RRGGBB"):
            DisplayPreset(colormap="custom", custom_colors=("black", "#FFFFFF"))
        with self.assertRaisesRegex(ValueError, "only valid"):
            DisplayPreset(colormap="viridis", custom_colors=("#000000", "#FFFFFF"))


if __name__ == "__main__":
    unittest.main()

