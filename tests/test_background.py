from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saxs_waxs_core.background import (  # noqa: E402
    BACKGROUND_FORMULA_VERSION,
    BackgroundError,
    BackgroundMode,
    BackgroundRecipe,
    apply_background,
)
from saxs_waxs_core.models import ImageFrame, ImageMetadata  # noqa: E402
from validation.generate_truth_sets import build_truth_arrays  # noqa: E402


def frame(
    data,
    *,
    name: str,
    exposure: float | None,
    detector_model: str | None = "TVX",
    pixel_size_x_m: float | None = 172e-6,
) -> ImageFrame:
    array = np.array(data)
    height, width = array.shape
    return ImageFrame(
        data=array,
        metadata=ImageMetadata(
            source_path=Path(name),
            format_id="tvx_tiff",
            support_level="validated_profile",
            width_px=width,
            height_px=height,
            dtype=str(array.dtype),
            detector_model=detector_model,
            pixel_size_x_m=pixel_size_x_m,
            pixel_size_y_m=172e-6,
            exposure_time_s=exposure,
        ),
    )


class BackgroundTests(unittest.TestCase):
    def test_mt013_exposure_linear_truth_matches_for_both_sample_exposures(self):
        arrays, expected = build_truth_arrays()
        air = frame(
            arrays["mt013_air_counts_0p5s"],
            name="air_0p5.tif",
            exposure=0.5,
        )
        sample_half = frame(
            arrays["mt013_sample_counts_0p5s"],
            name="sample_0p5.tif",
            exposure=0.5,
        )
        sample_one = frame(
            arrays["mt013_sample_counts_1p0s"],
            name="sample_1p0.tif",
            exposure=1.0,
        )

        np.testing.assert_allclose(
            apply_background(sample_half, air).data,
            arrays["mt013_signal_rate"],
            rtol=0.0,
            atol=4e-15,
        )
        np.testing.assert_allclose(
            apply_background(sample_one, air).data,
            arrays["mt013_signal_rate"],
            rtol=0.0,
            atol=4e-15,
        )
        self.assertEqual(
            expected["truth"]["mt013"]["formula_version"],
            BACKGROUND_FORMULA_VERSION,
        )

    def test_equal_exposure_returns_rate_and_preserves_negative_values(self):
        sample = frame([[8, 4], [2, 1]], name="sample.tif", exposure=0.5)
        air = frame([[2, 2], [2, 2]], name="air.tif", exposure=0.5)
        sample_before = sample.data.copy()
        air_before = air.data.copy()

        result = apply_background(sample, air)

        np.testing.assert_array_equal(result.data, [[12.0, 4.0], [0.0, -2.0]])
        np.testing.assert_array_equal(sample.data, sample_before)
        np.testing.assert_array_equal(air.data, air_before)
        self.assertFalse(result.data.flags.writeable)
        self.assertEqual(result.output_unit, "detector_count/s")
        self.assertEqual(result.background_scale_to_sample, 1.0)
        self.assertEqual(result.audit["formula_version"], BACKGROUND_FORMULA_VERSION)

    def test_mixed_exposure_recovers_one_rate_truth(self):
        signal_rate = np.array([[8.0, 3.0], [1.0, -0.5]])
        air_rate = np.array([[4.0, 2.0], [3.0, 2.0]])
        air = frame(air_rate * 0.5, name="air.tif", exposure=0.5)
        sample_half = frame(
            (signal_rate + air_rate) * 0.5,
            name="sample_0p5.tif",
            exposure=0.5,
        )
        sample_one = frame(
            (signal_rate + air_rate) * 1.0,
            name="sample_1p0.tif",
            exposure=1.0,
        )

        half_result = apply_background(sample_half, air)
        one_result = apply_background(sample_one, air)

        np.testing.assert_allclose(half_result.data, signal_rate, rtol=0.0, atol=1e-15)
        np.testing.assert_allclose(one_result.data, signal_rate, rtol=0.0, atol=1e-15)
        np.testing.assert_allclose(
            one_result.to_sample_exposure_counts(), signal_rate, rtol=0.0, atol=1e-15
        )
        self.assertEqual(one_result.background_scale_to_sample, 2.0)

    def test_explicit_exposure_overrides_are_traced(self):
        sample = frame([[12]], name="sample.tif", exposure=None)
        air = frame([[2]], name="air.tif", exposure=None)
        recipe = BackgroundRecipe(
            sample_exposure_override_s=1.0,
            background_exposure_override_s=0.5,
        )

        result = apply_background(sample, air, recipe)

        np.testing.assert_array_equal(result.data, [[8.0]])
        self.assertEqual(
            result.audit["exposure_sources"],
            {"sample": "recipe_override", "background": "recipe_override"},
        )

    def test_missing_or_invalid_exposure_is_rejected(self):
        missing = frame([[1]], name="sample.tif", exposure=None)
        valid_air = frame([[1]], name="air.tif", exposure=0.5)
        with self.assertRaisesRegex(BackgroundError, "exposure is missing"):
            apply_background(missing, valid_air)
        with self.assertRaisesRegex(BackgroundError, "greater than zero"):
            BackgroundRecipe(sample_exposure_override_s=0.0)

    def test_explicit_equal_exposure_policy_preserves_counts_and_unknown_times(self):
        sample = frame([[10, 1]], name="renamed sample.tif", exposure=None)
        air = frame([[3, 2]], name="another background.tif", exposure=None)
        recipe = BackgroundRecipe(metadata={"assume_equal_exposure_if_missing": True})
        result = apply_background(sample, air, recipe)
        np.testing.assert_array_equal(result.data, [[7, -1]])
        np.testing.assert_array_equal(result.to_sample_exposure_counts(), [[7, -1]])
        self.assertEqual(result.output_unit, "detector_count")
        self.assertIsNone(result.sample_exposure_s)
        self.assertIsNone(result.background_exposure_s)
        self.assertEqual(result.audit["formula"], "sample - air")
        self.assertIsNone(sample.metadata.exposure_time_s)
        # Known unequal durations always take precedence over the fallback.
        known = apply_background(
            frame([[10]], name="sample.tif", exposure=2),
            frame([[3]], name="air.tif", exposure=1), recipe,
        )
        np.testing.assert_array_equal(known.data, [[2]])
        self.assertEqual(known.output_unit, "detector_count/s")

    def test_auto_air_rejects_incompatible_or_nonfinite_frames(self):
        sample = frame([[1, 2]], name="sample.tif", exposure=0.5)
        wrong_shape = frame([[1], [2]], name="air.tif", exposure=0.5)
        with self.assertRaisesRegex(BackgroundError, "shapes differ"):
            apply_background(sample, wrong_shape)

        wrong_detector = frame(
            [[1, 2]], name="air.tif", exposure=0.5, detector_model="other"
        )
        with self.assertRaisesRegex(BackgroundError, "detector models differ"):
            apply_background(sample, wrong_detector)

        wrong_pixel_size = frame(
            [[1, 2]], name="air.tif", exposure=0.5, pixel_size_x_m=100e-6
        )
        with self.assertRaisesRegex(BackgroundError, "horizontal pixel sizes differ"):
            apply_background(sample, wrong_pixel_size)

        nonfinite = frame([[1.0, np.nan]], name="air.tif", exposure=0.5)
        with self.assertRaisesRegex(BackgroundError, "non-finite"):
            apply_background(sample, nonfinite)

        inconsistent = ImageFrame(
            data=np.array([[1, 2]]),
            metadata=ImageMetadata(
                source_path=Path("air.tif"),
                format_id="tvx_tiff",
                support_level="validated_profile",
                width_px=3,
                height_px=1,
                dtype="int64",
                detector_model="TVX",
                pixel_size_x_m=172e-6,
                pixel_size_y_m=172e-6,
                exposure_time_s=0.5,
            ),
        )
        with self.assertRaisesRegex(BackgroundError, "metadata dimensions"):
            apply_background(sample, inconsistent)

    def test_pass_through_modes_are_distinct_and_do_not_need_background(self):
        sample = frame([[-1, 2]], name="processed.tif", exposure=None)
        no_background = apply_background(
            sample,
            None,
            BackgroundRecipe(mode=BackgroundMode.NO_BACKGROUND),
        )
        upstream = apply_background(
            sample,
            None,
            BackgroundRecipe(
                mode=BackgroundMode.UPSTREAM_SUBTRACTED,
                input_unit="arbitrary_unit",
            ),
        )

        np.testing.assert_array_equal(no_background.data, sample.data)
        np.testing.assert_array_equal(upstream.data, sample.data)
        self.assertEqual(no_background.audit["mode"], "no_background")
        self.assertEqual(upstream.audit["mode"], "upstream_subtracted")
        self.assertEqual(upstream.output_unit, "arbitrary_unit")
        with self.assertRaisesRegex(BackgroundError, "only for auto_air"):
            upstream.to_sample_exposure_counts()

    def test_auto_air_requires_explicit_background(self):
        sample = frame([[1]], name="sample.tif", exposure=0.5)
        with self.assertRaisesRegex(BackgroundError, "requires an explicit"):
            apply_background(sample, None)


if __name__ == "__main__":
    unittest.main()
