from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    """Normalized metadata with raw tags retained for audit."""

    source_path: Path
    format_id: str
    support_level: str
    width_px: int
    height_px: int
    dtype: str
    detector_model: str | None = None
    software: str | None = None
    pixel_size_x_m: float | None = None
    pixel_size_y_m: float | None = None
    exposure_time_s: float | None = None
    exposure_period_s: float | None = None
    count_cutoff: int | None = None
    excluded_pixel_count: int | None = None
    excluded_pixels_file: str | None = None
    raw_description: str | None = None
    raw_tags: Mapping[str, Any] = field(default_factory=dict)
    embedded_processing: Mapping[str, Any] = field(default_factory=dict)
    geometry_document: Mapping[str, Any] = field(default_factory=dict)
    detector_mask: np.ndarray | None = None
    dataset_path: str | None = None
    frame_index: int = 0
    frame_count: int = 1
    intensity_unit: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_tags", MappingProxyType(dict(self.raw_tags)))
        object.__setattr__(self, "embedded_processing", MappingProxyType(dict(self.embedded_processing)))
        object.__setattr__(self, "geometry_document", MappingProxyType(dict(self.geometry_document)))
        if self.detector_mask is not None:
            mask=np.array(self.detector_mask,dtype=bool,copy=True)
            if mask.shape!=(self.height_px,self.width_px):
                raise ValueError('detector mask dimensions differ from image')
            mask.setflags(write=False)
            object.__setattr__(self,'detector_mask',mask)


@dataclass(frozen=True, slots=True)
class ImageFrame:
    """A read-only 2D detector frame in source array orientation."""

    data: np.ndarray
    metadata: ImageMetadata

    def __post_init__(self) -> None:
        if self.data.ndim != 2:
            raise ValueError(f"ImageFrame requires a 2D array, got {self.data.ndim}D")
        self.data.setflags(write=False)

