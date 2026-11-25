"""Auditable two-dimensional air-background correction.

Air scattering is a sample-environment background, not detector electronic
dark current.  This module therefore resolves and records it before the pyFAI
integration stage instead of silently passing it as pyFAI ``dark`` data.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np

from .models import ImageFrame, ImageMetadata


BACKGROUND_FORMULA_VERSION = "air_background_rate/1"
BACKGROUND_AUDIT_SCHEMA = "saxs_waxs_background_audit/1"


class BackgroundError(ValueError):
    """Raised when a requested background operation is not well-defined."""


class BackgroundMode(str, Enum):
    """User-visible business choices, with distinct audit meanings."""

    AUTO_AIR = "auto_air"
    NO_BACKGROUND = "no_background"
    UPSTREAM_SUBTRACTED = "upstream_subtracted"


@dataclass(frozen=True, slots=True)
class BackgroundRecipe:
    """Frozen inputs controlling one background operation.

    Exposure overrides represent explicit user input when reliable metadata is
    unavailable.  They never mutate the source ``ImageMetadata``.
    """

    mode: BackgroundMode | str = BackgroundMode.AUTO_AIR
    input_unit: str = "detector_count"
    sample_exposure_override_s: float | None = None
    background_exposure_override_s: float | None = None
    notes: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            mode = BackgroundMode(self.mode)
        except ValueError as exc:
            raise BackgroundError(f"unsupported background mode: {self.mode!r}") from exc
        object.__setattr__(self, "mode", mode)
        if not isinstance(self.input_unit, str) or not self.input_unit.strip():
            raise BackgroundError("input_unit must be a non-empty string")
        _validate_optional_exposure(
            self.sample_exposure_override_s, "sample exposure override"
        )
        _validate_optional_exposure(
            self.background_exposure_override_s, "background exposure override"
        )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class BackgroundResult:
    """Read-only processed array plus sufficient provenance to reproduce it."""

    data: np.ndarray
    recipe: BackgroundRecipe
    output_unit: str
    sample_exposure_s: float | None
    background_exposure_s: float | None
    background_scale_to_sample: float | None
    audit: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.data.ndim != 2:
            raise BackgroundError("background result must be a 2D array")
        self.data.setflags(write=False)
        object.__setattr__(self, "audit", MappingProxyType(dict(self.audit)))

    def to_sample_exposure_counts(self) -> np.ndarray:
        """Return an explicit copy on the sample-exposure count scale.

        Only the quantitative ``AUTO_AIR`` result has the exposure semantics
        needed for this conversion.
        """

        if self.recipe.mode is not BackgroundMode.AUTO_AIR:
            raise BackgroundError(
                "sample-exposure counts are defined only for auto_air results"
            )
        if self.audit.get("formula_version") == "air_background_equal_exposure_counts/1":
            return np.array(self.data, copy=True)
        assert self.sample_exposure_s is not None
        return np.array(self.data * self.sample_exposure_s, copy=True)


def _validate_optional_exposure(value: float | None, label: str) -> None:
    if value is None:
        return
    if not math.isfinite(float(value)) or float(value) <= 0.0:
        raise BackgroundError(f"{label} must be finite and greater than zero")


def _resolved_exposure(
    metadata_value: float | None,
    override_value: float | None,
    label: str,
) -> tuple[float, str]:
    if override_value is not None:
        value = float(override_value)
        source = "recipe_override"
    else:
        if metadata_value is None:
            raise BackgroundError(
                f"{label} exposure is missing; provide an explicit override or "
                "choose a non-subtracting mode"
            )
        value = float(metadata_value)
        source = "image_metadata"
    if not math.isfinite(value) or value <= 0.0:
        raise BackgroundError(f"{label} exposure must be finite and greater than zero")
    return value, source


def _same_optional_text(left: str | None, right: str | None, label: str) -> None:
    if left is not None and right is not None and left != right:
        raise BackgroundError(f"sample/background {label} differ: {left!r} != {right!r}")


def _same_optional_float(left: float | None, right: float | None, label: str) -> None:
    if left is None or right is None:
        return
    if not math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=0.0):
        raise BackgroundError(f"sample/background {label} differ: {left!r} != {right!r}")


def _validate_frame_metadata_shape(frame: ImageFrame, label: str) -> None:
    expected = (frame.metadata.height_px, frame.metadata.width_px)
    if frame.data.shape != expected:
        raise BackgroundError(
            f"{label} metadata dimensions do not match its array shape: "
            f"{expected!r} != {frame.data.shape!r}"
        )


def _validate_compatible_frames(sample: ImageFrame, background: ImageFrame) -> None:
    def family(metadata):
        value=metadata.embedded_processing.get('native_raster_format',metadata.format_id)
        return 'dtrek' if value.startswith('dtrek') else 'rigaku_tiff' if value.startswith('rigaku_tiff') else value
    if {family(sample.metadata),family(background.metadata)} == {
        "dtrek", "rigaku_tiff"
    }:
        raise BackgroundError("IMG 与 Rigaku TIFF 的像素方向可能不同，请选择与样品同格式的空气背景。")
    _validate_frame_metadata_shape(sample, "sample")
    _validate_frame_metadata_shape(background, "background")
    if sample.data.shape != background.data.shape:
        raise BackgroundError(
            "sample/background array shapes differ: "
            f"{sample.data.shape!r} != {background.data.shape!r}"
        )
    _same_optional_text(
        sample.metadata.detector_model,
        background.metadata.detector_model,
        "detector models",
    )
    _same_optional_float(
        sample.metadata.pixel_size_x_m,
        background.metadata.pixel_size_x_m,
        "horizontal pixel sizes",
    )
    _same_optional_float(
        sample.metadata.pixel_size_y_m,
        background.metadata.pixel_size_y_m,
        "vertical pixel sizes",
    )


def _float_copy(data: np.ndarray,excluded=None) -> np.ndarray:
    if data.dtype.kind in 'iu':
        excessive=(data>2**53) | ((data<-(2**53)) if data.dtype.kind=='i' else False)
        if excluded is not None:excessive &= ~excluded
        if np.any(excessive):
            raise BackgroundError('Integer counts exceed the exact float64 processing range; original pixels remain readable without rounding')
    return np.array(data, dtype=np.float64, order="C", copy=True)


def _source_path(metadata: ImageMetadata) -> str:
    return str(metadata.source_path)


def apply_background(
    sample: ImageFrame,
    background: ImageFrame | None,
    recipe: BackgroundRecipe | None = None,
) -> BackgroundResult:
    """Apply one explicit background recipe without mutating either input.

    ``AUTO_AIR`` returns the quantitative count-rate difference
    ``sample/t_sample - air/t_air``.  The two pass-through modes preserve the
    numeric input values but still create distinct audit histories.
    """

    recipe = recipe or BackgroundRecipe()
    embedded=sample.metadata.embedded_processing
    if embedded:
        if recipe.mode is BackgroundMode.AUTO_AIR and embedded.get('background_applied'):
            raise BackgroundError('This file already contains background-corrected intensities; choose upstream-subtracted mode')
        recipe=replace(recipe,input_unit=embedded['intensity_unit'])
    elif sample.metadata.intensity_unit is not None:
        recipe=replace(recipe,input_unit=sample.metadata.intensity_unit)
    sample_values = _float_copy(sample.data,sample.metadata.detector_mask)

    if recipe.mode is not BackgroundMode.AUTO_AIR:
        audit = {
            "schema": BACKGROUND_AUDIT_SCHEMA,
            "formula_version": BACKGROUND_FORMULA_VERSION,
            "mode": recipe.mode.value,
            "formula": "identity",
            "sample_source": _source_path(sample.metadata),
            "background_source": None,
            "sample_exposure_s": sample.metadata.exposure_time_s,
            "background_exposure_s": None,
            "exposure_sources": None,
            "background_scale_to_sample": None,
            "input_unit": recipe.input_unit,
            "output_unit": recipe.input_unit,
            "negative_values_preserved": True,
            "notes": recipe.notes,
            "metadata": dict(recipe.metadata),
        }
        return BackgroundResult(
            data=sample_values,
            recipe=recipe,
            output_unit=recipe.input_unit,
            sample_exposure_s=sample.metadata.exposure_time_s,
            background_exposure_s=None,
            background_scale_to_sample=None,
            audit=audit,
        )

    if background is None:
        raise BackgroundError("auto_air mode requires an explicit air-background frame")
    if sample.metadata.intensity_unit is not None and sample.metadata.intensity_unit not in ('detector_count','count','counts','ADU','adu','photon_count'):
        raise BackgroundError('Stored normalized intensity units require no-background/upstream mode; do not apply exposure normalization again')
    if recipe.input_unit.endswith('/s') or background.metadata.embedded_processing.get('intensity_unit','').endswith('/s'):
        raise BackgroundError('Air subtraction requires exposure-count inputs; do not normalize a stored rate twice')
    _validate_compatible_frames(sample, background)
    background_values = _float_copy(background.data,background.metadata.detector_mask)
    if not np.isfinite(sample_values).all() and not (
            embedded and not np.isinf(sample_values).any() or sample.metadata.detector_mask is not None
            and np.all(np.isfinite(sample_values)|sample.metadata.detector_mask)):
        raise BackgroundError("sample array contains non-finite values")
    if not np.isfinite(background_values).all() and not (
            background.metadata.embedded_processing and not np.isinf(background_values).any() or background.metadata.detector_mask is not None
            and np.all(np.isfinite(background_values)|background.metadata.detector_mask)):
        raise BackgroundError("background array contains non-finite values")

    # This policy is an explicit batch assumption, never a fabricated duration.
    # Recipes without it retain the original strict exposure requirements.
    sample_time = recipe.sample_exposure_override_s or sample.metadata.exposure_time_s
    air_time = recipe.background_exposure_override_s or background.metadata.exposure_time_s
    if recipe.metadata.get("assume_equal_exposure_if_missing") is True and (
        sample_time is None or air_time is None
    ):
        _validate_optional_exposure(sample_time, "sample exposure")
        _validate_optional_exposure(air_time, "background exposure")
        audit = {
            "schema": BACKGROUND_AUDIT_SCHEMA,
            "formula_version": "air_background_equal_exposure_counts/1",
            "mode": recipe.mode.value,
            "formula": "sample - air",
            "sample_source": _source_path(sample.metadata),
            "background_source": _source_path(background.metadata),
            "sample_exposure_s": sample_time,
            "background_exposure_s": air_time,
            "exposure_sources": {"policy": "assume_equal_exposure_if_missing"},
            "background_scale_to_sample": 1.0,
            "input_unit": recipe.input_unit,
            "output_unit": recipe.input_unit,
            "negative_values_preserved": True,
            "notes": recipe.notes,
            "metadata": dict(recipe.metadata),
        }
        return BackgroundResult(
            sample_values - background_values, recipe, recipe.input_unit,
            sample_time, air_time, 1.0, audit,
        )

    sample_exposure, sample_exposure_source = _resolved_exposure(
        sample.metadata.exposure_time_s,
        recipe.sample_exposure_override_s,
        "sample",
    )
    background_exposure, background_exposure_source = _resolved_exposure(
        background.metadata.exposure_time_s,
        recipe.background_exposure_override_s,
        "background",
    )
    background_scale = sample_exposure / background_exposure
    intensity_rate = sample_values / sample_exposure - background_values / background_exposure
    output_unit = f"{recipe.input_unit}/s"
    audit = {
        "schema": BACKGROUND_AUDIT_SCHEMA,
        "formula_version": BACKGROUND_FORMULA_VERSION,
        "mode": recipe.mode.value,
        "formula": "sample/t_sample - air/t_air",
        "equivalent_sample_count_formula": "sample - air*t_sample/t_air",
        "sample_source": _source_path(sample.metadata),
        "background_source": _source_path(background.metadata),
        "sample_exposure_s": sample_exposure,
        "background_exposure_s": background_exposure,
        "exposure_sources": {
            "sample": sample_exposure_source,
            "background": background_exposure_source,
        },
        "background_scale_to_sample": background_scale,
        "input_unit": recipe.input_unit,
        "output_unit": output_unit,
        "negative_values_preserved": True,
        "notes": recipe.notes,
        "metadata": dict(recipe.metadata),
    }
    return BackgroundResult(
        data=intensity_rate,
        recipe=recipe,
        output_unit=output_unit,
        sample_exposure_s=sample_exposure,
        background_exposure_s=background_exposure,
        background_scale_to_sample=background_scale,
        audit=audit,
    )
