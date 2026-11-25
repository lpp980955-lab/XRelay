from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

import numpy as np


Scale = Literal["linear", "log"]
LimitMode = Literal["minmax", "percentile", "manual"]
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


@dataclass(frozen=True, slots=True)
class DisplayPreset:
    """Display-only choices; none of these values alter scientific data."""

    colormap: str = "viridis"
    scale: Scale = "linear"
    limit_mode: LimitMode = "percentile"
    lower_percentile: float = 1.0
    upper_percentile: float = 99.5
    manual_vmin: float | None = None
    manual_vmax: float | None = None
    invert_colormap: bool = False
    custom_colors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.scale not in {"linear", "log"}:
            raise ValueError(f"Unsupported display scale: {self.scale}")
        if self.limit_mode not in {"minmax", "percentile", "manual"}:
            raise ValueError(f"Unsupported limit mode: {self.limit_mode}")
        if not 0.0 <= self.lower_percentile < self.upper_percentile <= 100.0:
            raise ValueError("Percentiles must satisfy 0 <= lower < upper <= 100")
        if self.limit_mode == "manual":
            if self.manual_vmin is None or self.manual_vmax is None:
                raise ValueError("Manual limits require manual_vmin and manual_vmax")
            if not np.isfinite(self.manual_vmin) or not np.isfinite(self.manual_vmax):
                raise ValueError("Manual limits must be finite")
            if self.manual_vmin >= self.manual_vmax:
                raise ValueError("manual_vmin must be smaller than manual_vmax")
        if self.scale == "log" and self.limit_mode == "manual" and self.manual_vmin <= 0:
            raise ValueError("Log display requires manual_vmin > 0")
        if self.colormap == "custom":
            if len(self.custom_colors) < 2:
                raise ValueError("Custom colormap requires at least two colors")
            if any(_HEX_COLOR.fullmatch(color) is None for color in self.custom_colors):
                raise ValueError("Custom colormap colors must use #RRGGBB")
        elif self.custom_colors:
            raise ValueError("custom_colors are only valid when colormap='custom'")

    @property
    def effective_colormap(self) -> str:
        if self.colormap == "custom":
            return "custom"
        if self.invert_colormap and not self.colormap.endswith("_r"):
            return self.colormap + "_r"
        return self.colormap


@dataclass(frozen=True, slots=True)
class NormalizedDisplay:
    values: np.ndarray
    valid: np.ndarray
    vmin: float
    vmax: float

    def __post_init__(self) -> None:
        self.values.setflags(write=False)
        self.valid.setflags(write=False)


def _valid_values(data: np.ndarray, mask: np.ndarray | None, scale: Scale) -> tuple[np.ndarray, np.ndarray]:
    array = np.asarray(data)
    if array.ndim != 2:
        raise ValueError(f"Display input must be 2D, got {array.ndim}D")
    valid = np.isfinite(array)
    if mask is not None:
        mask_array = np.asarray(mask, dtype=bool)
        if mask_array.shape != array.shape:
            raise ValueError("Mask shape must match data shape")
        valid &= ~mask_array
    if scale == "log":
        valid &= array > 0
    values = array[valid].astype(np.float64, copy=False)
    if values.size == 0:
        requirement = "positive finite unmasked" if scale == "log" else "finite unmasked"
        raise ValueError(f"No {requirement} pixels available for display limits")
    return values, valid


def resolve_display_limits(
    data: np.ndarray,
    preset: DisplayPreset,
    mask: np.ndarray | None = None,
) -> tuple[float, float]:
    values, _valid = _valid_values(data, mask, preset.scale)
    return _limits_from_values(values, preset)


def _limits_from_values(values: np.ndarray, preset: DisplayPreset) -> tuple[float, float]:
    if preset.limit_mode == "manual":
        vmin = float(preset.manual_vmin)
        vmax = float(preset.manual_vmax)
    elif preset.limit_mode == "minmax":
        vmin = float(values.min())
        vmax = float(values.max())
    else:
        vmin, vmax = (
            float(value)
            for value in np.percentile(values, [preset.lower_percentile, preset.upper_percentile])
        )
    if vmin == vmax:
        half_span = max(abs(vmin) * 1e-6, 0.5)
        vmin -= half_span
        vmax += half_span
        if preset.scale == "log" and vmin <= 0:
            vmin = max(float(np.nextafter(0.0, 1.0)), vmax * 1e-6)
    if preset.scale == "log" and vmin <= 0:
        positive = values[values > 0]
        if positive.size == 0:
            raise ValueError("Log display has no positive pixels")
        vmin = float(positive.min())
    if not (np.isfinite(vmin) and np.isfinite(vmax) and vmin < vmax):
        raise ValueError(f"Invalid display limits: vmin={vmin}, vmax={vmax}")
    return vmin, vmax


def normalize_for_display(
    data: np.ndarray,
    preset: DisplayPreset,
    mask: np.ndarray | None = None,
) -> NormalizedDisplay:
    """Return a 0..1 display buffer and validity mask without modifying input."""

    array = np.asarray(data)
    values, valid = _valid_values(array, mask, preset.scale)
    vmin, vmax = _limits_from_values(values, preset)
    output = np.full(array.shape, np.nan, dtype=np.float64)
    if preset.scale == "linear":
        values -= vmin
        values /= vmax - vmin
    else:
        log_min = np.log(vmin)
        log_max = np.log(vmax)
        np.log(values, out=values)
        values -= log_min
        values /= log_max - log_min
    np.clip(values, 0.0, 1.0, out=values)
    output[valid] = values
    return NormalizedDisplay(values=output, valid=valid.copy(), vmin=vmin, vmax=vmax)

