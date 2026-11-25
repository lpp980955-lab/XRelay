"""Strict pyFAI bridge for the frozen canonical detector convention.

The project persists both the user-facing direct-beam/FIT2D representation and
the calculation-facing PONI representation.  This adapter always constructs
pyFAI from the derived PONI parameters and therefore supports non-zero detector
rotations without overloading the meaning of centre or distance.

pyFAI evaluates pixel indices and applies its own half-pixel centre offset.
Canonical coordinates map as ``d1 = y_px - 0.5`` and
``d2 = x_px - 0.5``.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import numpy as np

from .geometry import (
    DetectorGeometry,
    Fit2DParameters,
    GeometryError,
    GeometryProvenance,
    fit2d_parameters,
    poni_parameters,
)


class PyFAIAdapterError(GeometryError):
    """The canonical geometry cannot be represented by this adapter version."""


class PyFAIUnavailable(PyFAIAdapterError):
    """pyFAI is not installed in the active environment."""


@dataclass(frozen=True, slots=True)
class PyFAIScatteringCoordinates:
    """Coordinates evaluated independently by pyFAI in project units."""

    two_theta_rad: np.ndarray
    q_nm_inv: np.ndarray
    s_nm_inv: np.ndarray
    azimuth_deg: np.ndarray


def pyfai_version() -> str:
    """Return the installed distribution version without relying on private APIs."""

    try:
        return version("pyFAI")
    except PackageNotFoundError as exc:
        raise PyFAIUnavailable("pyFAI is not installed") from exc


def _pyfai_types() -> tuple[type[Any], type[Any]]:
    try:
        from pyFAI.detectors import Detector
        from pyFAI.geometry import Geometry
    except ImportError as exc:
        raise PyFAIUnavailable("pyFAI could not be imported") from exc
    return Detector, Geometry


def canonical_points_to_pyfai_indices(
    x_px: Any,
    y_px: Any,
) -> tuple[np.ndarray, np.ndarray]:
    """Convert canonical continuous ``(x, y)`` to pyFAI ``(d1, d2)`` indices."""

    x, y = np.broadcast_arrays(
        np.asarray(x_px, dtype=np.float64),
        np.asarray(y_px, dtype=np.float64),
    )
    if np.any(~np.isfinite(x)) or np.any(~np.isfinite(y)):
        raise PyFAIAdapterError("canonical coordinates must be finite")
    return y - 0.5, x - 0.5


def to_pyfai_geometry(geometry: DetectorGeometry) -> Any:
    """Build a pyFAI geometry from the versioned PONI calculation truth."""

    Detector, Geometry = _pyfai_types()
    max_shape = None
    if geometry.image_height_px is not None and geometry.image_width_px is not None:
        max_shape = (geometry.image_height_px, geometry.image_width_px)
    detector = Detector(
        pixel1=geometry.pixel_size_y_m,
        pixel2=geometry.pixel_size_x_m,
        max_shape=max_shape,
        orientation=3,
    )
    poni = poni_parameters(geometry)
    return Geometry(
        dist=poni.dist_m,
        poni1=poni.poni1_m,
        poni2=poni.poni2_m,
        rot1=poni.rot1_rad,
        rot2=poni.rot2_rad,
        rot3=poni.rot3_rad,
        detector=detector,
        wavelength=geometry.wavelength_m,
    )


def to_pyfai_integrator(geometry: DetectorGeometry) -> Any:
    """Build an integration-capable pyFAI object from the same PONI truth."""

    try:
        from pyFAI.detectors import Detector
        from pyFAI.integrator.azimuthal import AzimuthalIntegrator
    except ImportError as exc:
        raise PyFAIUnavailable("pyFAI could not be imported") from exc
    max_shape = None
    if geometry.image_height_px is not None and geometry.image_width_px is not None:
        max_shape = (geometry.image_height_px, geometry.image_width_px)
    detector = Detector(
        pixel1=geometry.pixel_size_y_m,
        pixel2=geometry.pixel_size_x_m,
        max_shape=max_shape,
        orientation=3,
    )
    poni = poni_parameters(geometry)
    return AzimuthalIntegrator(
        dist=poni.dist_m,
        poni1=poni.poni1_m,
        poni2=poni.poni2_m,
        rot1=poni.rot1_rad,
        rot2=poni.rot2_rad,
        rot3=poni.rot3_rad,
        detector=detector,
        wavelength=geometry.wavelength_m,
    )


def from_fit2d_parameters(
    parameters: Fit2DParameters,
    *,
    wavelength_m: float,
    pixel_size_x_m: float,
    pixel_size_y_m: float,
    image_width_px: int | None = None,
    image_height_px: int | None = None,
    provenance: GeometryProvenance | None = None,
    metadata: dict[str, Any] | None = None,
) -> DetectorGeometry:
    """Import explicit FIT2D values using pyFAI's audited public converter."""

    try:
        from pyFAI.detectors import Detector
        from pyFAI.geometry.fit2d import Fit2dGeometry, convert_from_Fit2d
    except ImportError as exc:
        raise PyFAIUnavailable("pyFAI could not be imported") from exc
    detector = Detector(
        pixel1=float(pixel_size_y_m),
        pixel2=float(pixel_size_x_m),
        max_shape=(image_height_px, image_width_px)
        if image_height_px is not None and image_width_px is not None
        else None,
        orientation=3,
    )
    converted = convert_from_Fit2d(
        Fit2dGeometry(
            directDist=float(parameters.direct_distance_m) * 1e3,
            centerX=float(parameters.center_x_px),
            centerY=float(parameters.center_y_px),
            tilt=float(parameters.tilt_deg),
            tiltPlanRotation=float(parameters.tilt_plane_rotation_deg),
            detector=detector,
            pixelX=float(pixel_size_x_m) * 1e6,
            pixelY=float(pixel_size_y_m) * 1e6,
            wavelength=float(wavelength_m) * 1e10,
        )
    )
    geometry = DetectorGeometry(
        wavelength_m=wavelength_m,
        detector_distance_m=parameters.direct_distance_m,
        pixel_size_x_m=pixel_size_x_m,
        pixel_size_y_m=pixel_size_y_m,
        beam_center_x_px=parameters.center_x_px,
        beam_center_y_px=parameters.center_y_px,
        image_width_px=image_width_px,
        image_height_px=image_height_px,
        rot1_rad=converted.rot1,
        rot2_rad=converted.rot2,
        rot3_rad=converted.rot3,
        provenance=provenance
        or GeometryProvenance(
            method="fit2d_import",
            source_application="FIT2D-compatible",
            source_convention="direct-beam/FIT2D",
        ),
        metadata=metadata or {},
    )
    # Guard against upstream/API drift by checking our independent conversion
    # equations against the PONI object returned by pyFAI.
    expected = poni_parameters(geometry)
    for name, actual in (
        ("dist", converted.dist),
        ("poni1", converted.poni1),
        ("poni2", converted.poni2),
        ("rot1", converted.rot1),
        ("rot2", converted.rot2),
        ("rot3", converted.rot3),
    ):
        derived = getattr(expected, f"{name}_m" if name in {"dist", "poni1", "poni2"} else f"{name}_rad")
        if not np.isclose(actual, derived, rtol=2e-12, atol=2e-12):
            raise PyFAIAdapterError(
                f"FIT2D to PONI conversion disagrees for {name}: "
                f"pyFAI={actual!r}, project={derived!r}"
            )
    return geometry


def pyfai_fit2d_parameters(geometry: DetectorGeometry) -> Fit2DParameters:
    """Return FIT2D parameters reported by pyFAI for cross-checking."""

    values = to_pyfai_geometry(geometry).getFit2D()
    return Fit2DParameters(
        direct_distance_m=float(values["directDist"]) * 1e-3,
        center_x_px=float(values["centerX"]),
        center_y_px=float(values["centerY"]),
        tilt_deg=float(values["tilt"]),
        tilt_plane_rotation_deg=float(values["tiltPlanRotation"]),
    )


def pyfai_scattering_coordinates(
    geometry: DetectorGeometry,
    x_px: Any,
    y_px: Any,
) -> PyFAIScatteringCoordinates:
    """Evaluate points with pyFAI and translate chi to the visible convention.

    pyFAI chi is zero to detector-right and increases toward detector-down for
    orientation 3.  The project convention is zero up and clockwise, hence
    ``azimuth = (degrees(chi) + 90) mod 360``.
    """

    backend = to_pyfai_geometry(geometry)
    d1, d2 = canonical_points_to_pyfai_indices(x_px, y_px)
    # pyFAI's compiled scalar path currently returns a one-element vector;
    # normalize it back to the broadcast input shape so our API is stable.
    target_shape = d1.shape
    two_theta = np.asarray(backend.tth(d1, d2), dtype=np.float64).reshape(target_shape)
    q_nm_inv = np.asarray(backend.qFunction(d1, d2), dtype=np.float64).reshape(target_shape)
    chi_rad = np.asarray(backend.chi(d1, d2), dtype=np.float64).reshape(target_shape)
    azimuth = np.mod(np.degrees(chi_rad) + 90.0, 360.0)
    at_center = (
        np.asarray(x_px, dtype=np.float64) == geometry.beam_center_x_px
    ) & (
        np.asarray(y_px, dtype=np.float64) == geometry.beam_center_y_px
    )
    azimuth = np.where(at_center, np.nan, azimuth)
    return PyFAIScatteringCoordinates(
        two_theta_rad=two_theta,
        q_nm_inv=q_nm_inv,
        s_nm_inv=q_nm_inv / (2.0 * np.pi),
        azimuth_deg=azimuth,
    )
