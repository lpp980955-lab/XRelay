"""Explicit, auditable 1D radial and 360-degree azimuthal integration.

This module delegates pixel geometry and histogramming to the audited pyFAI
backend while keeping project conventions, MASK composition, units and region
selection explicit.  Preview and future batch processing call the same API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import threading
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import numpy as np

from .geometry import DetectorGeometry, VISIBLE_AZIMUTH_CONVENTION, poni_parameters
from .pyfai_adapter import pyfai_version, to_pyfai_integrator


INTEGRATION_AUDIT_SCHEMA = "saxs_waxs_integration_audit/1"


class IntegrationError(ValueError):
    """Raised when an integration recipe is incomplete or inconsistent."""


class IntegrationWorkspace:
    """One serialized geometry cache, retaining pyFAI's safe=True validation.

    Geometry caches survive recipe/range changes. No scientific arrays or output
    recipes are downsampled. A new geometry evicts the preceding backend.
    """

    def __init__(self):
        self.lock=threading.RLock()
        self.key=None
        self.backend=None
        self.backends={}
        self.azimuth=None

    @staticmethod
    def geometry_key(geometry):
        return tuple(getattr(geometry,name) for name in (
            "wavelength_m","detector_distance_m","pixel_size_x_m","pixel_size_y_m",
            "beam_center_x_px","beam_center_y_px","image_width_px","image_height_px",
            "rot1_rad","rot2_rad","rot3_rad"))

    def integrate(self,data,geometry,recipe,mask=None):
        with self.lock:
            shape=tuple(np.shape(data))
            key=(self.geometry_key(geometry),shape)
            if key!=self.key:
                self.backends.clear()
                self.key=key
                self.azimuth=None
            radial=isinstance(recipe,RadialIntegrationRecipe)
            mode="radial" if radial else "azimuthal"
            # Their chi discontinuities differ. Separate backend caches prevent
            # a 0..360 azimuthal call from changing a later radial split-pixel
            # result. Both caches are evicted together when geometry changes.
            if mode not in self.backends:
                self.backends[mode]=to_pyfai_integrator(geometry)
            self.backend=self.backends[mode]
            if radial:
                if recipe.sectors and self.azimuth is None:
                    self.azimuth=_project_azimuth_array(self.backend,shape)
                    self.azimuth.setflags(write=False)
                return integrate_radial_1d(data,geometry,recipe,mask=mask,
                    _backend=self.backend,_azimuth=self.azimuth)
            return integrate_azimuthal_1d(data,geometry,recipe,mask=mask,_backend=self.backend)


class RadialUnit(str, Enum):
    Q_NM_INV = "q_nm^-1"
    Q_ANGSTROM_INV = "q_A^-1"
    S_NM_INV = "s_nm^-1"
    TWO_THETA_DEG = "2th_deg"


class PixelSplitting(str, Enum):
    NONE = "no"
    BOUNDING_BOX = "bbox"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class AzimuthSector:
    center_deg: float
    width_deg: float

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.center_deg)):
            raise IntegrationError("sector center must be finite")
        width = float(self.width_deg)
        if not math.isfinite(width) or width <= 0.0 or width > 360.0:
            raise IntegrationError("sector width must be in (0, 360]")
        object.__setattr__(self, "center_deg", float(self.center_deg) % 360.0)
        object.__setattr__(self, "width_deg", width)


def _validate_range(values: tuple[float, float] | None, label: str) -> None:
    if values is None:
        return
    lower, upper = (float(value) for value in values)
    if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
        raise IntegrationError(f"{label} must be finite with lower < upper")


@dataclass(frozen=True, slots=True)
class RadialIntegrationRecipe:
    npt: int = 1000
    unit: RadialUnit | str = RadialUnit.Q_NM_INV
    radial_range: tuple[float, float] | None = None
    sectors: Sequence[AzimuthSector] = ()
    pixel_splitting: PixelSplitting | str = PixelSplitting.BOUNDING_BOX
    correct_solid_angle: bool = False
    polarization_factor: float | None = None
    normalization_factor: float = 1.0
    input_unit: str = "detector_count/s"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.npt, int) or isinstance(self.npt, bool) or self.npt < 2:
            raise IntegrationError("npt must be an integer >= 2")
        try:
            object.__setattr__(self, "unit", RadialUnit(self.unit))
            object.__setattr__(self, "pixel_splitting", PixelSplitting(self.pixel_splitting))
        except ValueError as exc:
            raise IntegrationError(str(exc)) from exc
        _validate_range(self.radial_range, "radial_range")
        object.__setattr__(self, "sectors", tuple(self.sectors))
        _validate_common_recipe(self)


@dataclass(frozen=True, slots=True)
class AzimuthalIntegrationRecipe:
    radial_range: tuple[float, float]
    radial_unit: RadialUnit | str = RadialUnit.Q_NM_INV
    npt_azimuth: int = 360
    npt_radial: int = 100
    pixel_splitting: PixelSplitting | str = PixelSplitting.BOUNDING_BOX
    correct_solid_angle: bool = False
    polarization_factor: float | None = None
    normalization_factor: float = 1.0
    input_unit: str = "detector_count/s"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_range(self.radial_range, "radial_range")
        if self.radial_range is None:
            raise IntegrationError("azimuthal integration requires radial_range")
        for name in ("npt_azimuth", "npt_radial"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 2:
                raise IntegrationError(f"{name} must be an integer >= 2")
        try:
            object.__setattr__(self, "radial_unit", RadialUnit(self.radial_unit))
            object.__setattr__(self, "pixel_splitting", PixelSplitting(self.pixel_splitting))
        except ValueError as exc:
            raise IntegrationError(str(exc)) from exc
        _validate_common_recipe(self)


def _validate_common_recipe(recipe: Any) -> None:
    normalization = float(recipe.normalization_factor)
    if not math.isfinite(normalization) or normalization <= 0.0:
        raise IntegrationError("normalization_factor must be finite and positive")
    polarization = recipe.polarization_factor
    if polarization is not None and (
        not math.isfinite(float(polarization)) or not -1.0 <= float(polarization) <= 1.0
    ):
        raise IntegrationError("polarization_factor must be None or within [-1, 1]")
    if not isinstance(recipe.input_unit, str) or not recipe.input_unit.strip():
        raise IntegrationError("input_unit must be a non-empty string")
    object.__setattr__(recipe, "normalization_factor", normalization)
    object.__setattr__(recipe, "metadata", MappingProxyType(dict(recipe.metadata)))


@dataclass(frozen=True, slots=True)
class IntegrationCurve:
    x: np.ndarray
    intensity: np.ndarray
    sigma: np.ndarray | None
    count: np.ndarray
    x_unit: str
    intensity_unit: str
    kind: str
    audit: Mapping[str, Any]

    def __post_init__(self) -> None:
        length = len(self.x)
        if self.intensity.shape != (length,) or self.count.shape != (length,):
            raise IntegrationError("integration curve arrays must be one-dimensional and aligned")
        if self.sigma is not None and self.sigma.shape != (length,):
            raise IntegrationError("sigma must align with the integration curve")
        for value in (self.x, self.intensity, self.count, self.sigma):
            if value is not None:
                value.setflags(write=False)
        object.__setattr__(self, "audit", MappingProxyType(dict(self.audit)))


def _validate_inputs(
    data: np.ndarray, geometry: DetectorGeometry, mask: np.ndarray | None
) -> tuple[np.ndarray, np.ndarray]:
    values = np.array(data, dtype=np.float64, order="C", copy=True)
    if values.ndim != 2:
        raise IntegrationError("integration data must be a 2D array")
    expected = (geometry.image_height_px, geometry.image_width_px)
    if None not in expected and values.shape != expected:
        raise IntegrationError(
            f"geometry dimensions do not match data: {expected!r} != {values.shape!r}"
        )
    excluded = np.zeros(values.shape, dtype=bool)
    if mask is not None:
        excluded = np.array(mask, dtype=bool, order="C", copy=True)
        if excluded.shape != values.shape:
            raise IntegrationError("mask shape differs from integration data")
    invalid = ~np.isfinite(values)
    if np.any(invalid & ~excluded):
        raise IntegrationError("unmasked integration data contain non-finite values")
    values[excluded] = 0.0
    return values, excluded


def _pyfai_unit(unit: RadialUnit) -> tuple[str, float]:
    if unit is RadialUnit.S_NM_INV:
        return "q_nm^-1", 1.0 / (2.0 * math.pi)
    return unit.value, 1.0


def _pyfai_range(
    values: tuple[float, float] | None, unit: RadialUnit
) -> tuple[float, float] | None:
    if values is None:
        return None
    if unit is RadialUnit.S_NM_INV:
        return tuple(float(value) * 2.0 * math.pi for value in values)
    return tuple(float(value) for value in values)


def detector_radial_array(
    geometry: DetectorGeometry,
    shape: tuple[int, int],
    unit: RadialUnit | str = RadialUnit.Q_NM_INV,
) -> np.ndarray:
    """Return the integration radial coordinate at every pixel centre.

    The UI uses this read-only array to draw the exact radial boundaries used
    by the backend, including for tilted detector geometries.
    """

    if (
        len(shape) != 2
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in shape
        )
    ):
        raise IntegrationError("shape must contain two positive integers")
    expected = (geometry.image_height_px, geometry.image_width_px)
    if None not in expected and shape != expected:
        raise IntegrationError(
            f"geometry dimensions do not match radial array: {expected!r} != {shape!r}"
        )
    try:
        radial_unit = RadialUnit(unit)
    except ValueError as exc:
        raise IntegrationError(str(exc)) from exc
    backend_unit, scale = _pyfai_unit(radial_unit)
    values = np.array(
        to_pyfai_integrator(geometry).array_from_unit(shape, unit=backend_unit),
        dtype=np.float64,
        order="C",
        copy=True,
    )
    values *= scale
    values.setflags(write=False)
    return values


def _method(split: PixelSplitting) -> tuple[str, str, str]:
    return (split.value, "histogram", "cython")


def _project_azimuth_array(backend: Any, shape: tuple[int, int]) -> np.ndarray:
    chi_deg = np.asarray(backend.center_array(shape, unit="chi_deg"), dtype=np.float64)
    return np.mod(chi_deg + 90.0, 360.0)


def _sector_exclusion(
    backend: Any, shape: tuple[int, int], sectors: Sequence[AzimuthSector],
    cached_azimuth: np.ndarray | None = None,
) -> np.ndarray:
    if not sectors or any(sector.width_deg == 360.0 for sector in sectors):
        return np.zeros(shape, dtype=bool)
    azimuth = _project_azimuth_array(backend, shape) if cached_azimuth is None else cached_azimuth
    selected = np.zeros(shape, dtype=bool)
    for sector in sectors:
        delta = (azimuth - sector.center_deg + 180.0) % 360.0 - 180.0
        selected |= np.abs(delta) <= sector.width_deg / 2.0
    return ~selected


def _array(value: Any, *, dtype: Any = np.float64) -> np.ndarray:
    return np.array(value, dtype=dtype, order="C", copy=True)


def _base_audit(
    geometry: DetectorGeometry,
    excluded: np.ndarray,
    recipe: Any,
    kind: str,
) -> dict[str, Any]:
    poni = poni_parameters(geometry)
    return {
        "schema": INTEGRATION_AUDIT_SCHEMA,
        "kind": kind,
        "backend": "pyFAI",
        "backend_version": pyfai_version(),
        "method": [recipe.pixel_splitting.value, "histogram", "cython"],
        "correct_solid_angle": bool(recipe.correct_solid_angle),
        "polarization_factor": recipe.polarization_factor,
        "normalization_factor": recipe.normalization_factor,
        "input_unit": recipe.input_unit,
        "mask_convention": "true_means_excluded",
        "excluded_pixel_count": int(np.count_nonzero(excluded)),
        "geometry_poni": {
            "dist_m": poni.dist_m,
            "poni1_m": poni.poni1_m,
            "poni2_m": poni.poni2_m,
            "rot1_rad": poni.rot1_rad,
            "rot2_rad": poni.rot2_rad,
            "rot3_rad": poni.rot3_rad,
            "wavelength_m": geometry.wavelength_m,
        },
        "geometry_summary": {
            "beam_center_px": {
                "x": geometry.beam_center_x_px,
                "y": geometry.beam_center_y_px,
            },
            "sample_detector_distance_mm": geometry.detector_distance_m * 1e3,
            "wavelength_nm": geometry.wavelength_m * 1e9,
            "pixel_size_um": {
                "x": geometry.pixel_size_x_m * 1e6,
                "y": geometry.pixel_size_y_m * 1e6,
            },
            "image_size_px": {
                "width": geometry.image_width_px,
                "height": geometry.image_height_px,
            },
        },
        "metadata": dict(recipe.metadata),
    }


def integrate_radial_1d(
    data: np.ndarray,
    geometry: DetectorGeometry,
    recipe: RadialIntegrationRecipe | None = None,
    *,
    mask: np.ndarray | None = None,
    _backend: Any = None,
    _azimuth: np.ndarray | None = None,
) -> IntegrationCurve:
    """Return I(q/s/2theta), optionally from one or more arbitrary sectors."""

    recipe = recipe or RadialIntegrationRecipe()
    values, excluded = _validate_inputs(data, geometry, mask)
    backend = to_pyfai_integrator(geometry) if _backend is None else _backend
    sector_excluded = _sector_exclusion(backend, values.shape, recipe.sectors,_azimuth)
    combined = excluded | sector_excluded
    unit, axis_scale = _pyfai_unit(recipe.unit)
    radial_range = _pyfai_range(recipe.radial_range, recipe.unit)
    if radial_range is not None and recipe.pixel_splitting is PixelSplitting.NONE:
        lower, upper = radial_range
        radius = backend.center_array(values.shape, unit=unit)
        # The Cython histogram can truncate negative bin offsets to bin zero.
        combined |= (radius < lower) | (radius > upper)
    result = backend.integrate1d_ng(
        values,
        recipe.npt,
        correctSolidAngle=recipe.correct_solid_angle,
        radial_range=radial_range,
        mask=combined.astype(np.int8),
        polarization_factor=recipe.polarization_factor,
        method=_method(recipe.pixel_splitting),
        unit=unit,
        safe=True,
        normalization_factor=recipe.normalization_factor,
        metadata=dict(recipe.metadata),
    )
    x = _array(result.radial) * axis_scale
    intensity = _array(result.intensity)
    sigma = None if result.sigma is None else _array(result.sigma)
    count = _array(result.count)
    audit = _base_audit(geometry, combined, recipe, "radial_1d")
    audit.update(
        {
            "npt": recipe.npt,
            "x_unit": recipe.unit.value,
            "radial_range": recipe.radial_range,
            "sectors": [
                {"center_deg": sector.center_deg, "width_deg": sector.width_deg}
                for sector in recipe.sectors
            ],
            "sector_boundary_rule": "pixel_center_inclusive",
            "visible_azimuth_convention": VISIBLE_AZIMUTH_CONVENTION,
        }
    )
    return IntegrationCurve(
        x,
        intensity,
        sigma,
        count,
        recipe.unit.value,
        recipe.input_unit,
        "radial_1d",
        audit,
    )


def integrate_azimuthal_1d(
    data: np.ndarray,
    geometry: DetectorGeometry,
    recipe: AzimuthalIntegrationRecipe,
    *,
    mask: np.ndarray | None = None,
    _backend: Any = None,
) -> IntegrationCurve:
    """Return a full 0..360 degree I(phi) curve in the visible convention."""

    values, excluded = _validate_inputs(data, geometry, mask)
    backend = to_pyfai_integrator(geometry) if _backend is None else _backend
    backend.setChiDiscAtZero()
    radial_unit, _axis_scale = _pyfai_unit(recipe.radial_unit)
    result = backend.integrate_radial(
        values,
        recipe.npt_azimuth,
        npt_rad=recipe.npt_radial,
        correctSolidAngle=recipe.correct_solid_angle,
        radial_range=_pyfai_range(recipe.radial_range, recipe.radial_unit),
        azimuth_range=(0.0, 360.0),
        mask=excluded.astype(np.int8),
        polarization_factor=recipe.polarization_factor,
        method=_method(recipe.pixel_splitting),
        unit="chi_deg",
        radial_unit=radial_unit,
        normalization_factor=recipe.normalization_factor,
    )
    visible = np.mod(_array(result.radial) + 90.0, 360.0)
    order = np.argsort(visible, kind="stable")
    x = visible[order]
    intensity = _array(result.intensity)[order]
    sigma = None if result.sigma is None else _array(result.sigma)[order]
    count = _array(result.count)[order]
    audit = _base_audit(geometry, excluded, recipe, "azimuthal_1d")
    audit.update(
        {
            "npt_azimuth": recipe.npt_azimuth,
            "npt_radial": recipe.npt_radial,
            "x_unit": "deg",
            "radial_unit": recipe.radial_unit.value,
            "radial_range": recipe.radial_range,
            "visible_azimuth_convention": VISIBLE_AZIMUTH_CONVENTION,
            "full_360_requested": True,
        }
    )
    return IntegrationCurve(
        x,
        intensity,
        sigma,
        count,
        "deg",
        recipe.input_unit,
        "azimuthal_1d",
        audit,
    )
