"""Versioned detector geometry and analytical untilted coordinate transforms.

The canonical continuous detector coordinate starts at the outer top-left pixel
edge.  Therefore array pixel ``image[row, column]`` has its centre at
``(x, y) = (column + 0.5, row + 0.5)``.  The visible azimuth convention is
Xpolar-compatible: 0° up, 90° right, 180° down and 270° left.

Only the untilted flat-detector model is evaluated analytically here.  Non-zero
rotations use a versioned dual representation: the public geometry keeps the
direct-beam distance and intersection familiar from FIT2D, while a derived PONI
record is the calculation truth consumed by the audited pyFAI adapter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import math
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np


GEOMETRY_SCHEMA = "saxs_waxs_geometry/2"
LEGACY_GEOMETRY_SCHEMA = "saxs_waxs_geometry/1"
CANONICAL_COORDINATE_CONVENTION = "top_left_edge_xy_right_down_pixel_center_plus_0p5"
VISIBLE_AZIMUTH_CONVENTION = "zero_up_clockwise_0_360"
DISTANCE_CONVENTION = "direct_beam_distance_along_incident_beam"
CENTER_CONVENTION = "direct_beam_intersection_canonical_xy"
PONI_CONVENTION = "pyfai_dist_normal_to_detector_and_poni_axes_1_2"


class GeometryError(ValueError):
    """Base error for invalid or unsupported geometry state."""


class GeometryConfigError(GeometryError):
    """A versioned geometry document is malformed or unsupported."""


class TiltedGeometryUnavailable(GeometryError):
    """The dependency-free analytical path cannot evaluate detector rotations."""


class BeamCenterConvention(str, Enum):
    """Explicit external beam-centre coordinate conventions."""

    CANONICAL_TOP_LEFT_EDGE = "canonical_top_left_edge"
    ZERO_BASED_PIXEL_CENTERS = "zero_based_pixel_centers"
    ONE_BASED_PIXEL_CENTERS = "one_based_pixel_centers"


def _finite(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise GeometryError(f"{name} must be finite")
    return number


def _positive(name: str, value: float) -> float:
    number = _finite(name, value)
    if number <= 0:
        raise GeometryError(f"{name} must be positive")
    return number


def pixel_index_to_continuous(row: Any, column: Any) -> tuple[np.ndarray, np.ndarray]:
    """Convert zero-based array indices to canonical continuous pixel centres."""

    row_array = np.asarray(row, dtype=np.float64)
    column_array = np.asarray(column, dtype=np.float64)
    return column_array + 0.5, row_array + 0.5


def beam_center_to_canonical(
    x_px: float,
    y_px: float,
    convention: BeamCenterConvention | str,
) -> tuple[float, float]:
    """Convert an explicitly identified external centre to canonical pixels."""

    try:
        convention = BeamCenterConvention(convention)
    except ValueError as exc:
        raise GeometryError(f"unsupported beam-center convention: {convention!r}") from exc
    x = _finite("beam_center_x_px", x_px)
    y = _finite("beam_center_y_px", y_px)
    if convention is BeamCenterConvention.CANONICAL_TOP_LEFT_EDGE:
        return x, y
    if convention is BeamCenterConvention.ZERO_BASED_PIXEL_CENTERS:
        return x + 0.5, y + 0.5
    if convention is BeamCenterConvention.ONE_BASED_PIXEL_CENTERS:
        return x - 0.5, y - 0.5
    raise AssertionError("unreachable convention")


@dataclass(frozen=True, slots=True)
class GeometryProvenance:
    method: str
    source_application: str | None = None
    source_path: str | None = None
    source_convention: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if not self.method.strip():
            raise GeometryError("provenance method cannot be empty")


@dataclass(frozen=True, slots=True)
class DetectorGeometry:
    """User-facing direct-beam geometry in SI units.

    ``detector_distance_m`` is the distance along the incident beam to its
    intersection with the detector plane. ``beam_center_*`` is that direct-beam
    intersection in canonical continuous pixels.  For a tilted detector these
    values differ from pyFAI's plane-normal ``dist`` and ``poni1/poni2``; use
    :func:`poni_parameters` for the calculation representation.
    """

    wavelength_m: float
    detector_distance_m: float
    pixel_size_x_m: float
    pixel_size_y_m: float
    beam_center_x_px: float
    beam_center_y_px: float
    image_width_px: int | None = None
    image_height_px: int | None = None
    rot1_rad: float = 0.0
    rot2_rad: float = 0.0
    rot3_rad: float = 0.0
    provenance: GeometryProvenance = field(
        default_factory=lambda: GeometryProvenance(method="manual")
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "wavelength_m",
            "detector_distance_m",
            "pixel_size_x_m",
            "pixel_size_y_m",
        ):
            object.__setattr__(self, name, _positive(name, getattr(self, name)))
        for name in ("rot1_rad", "rot2_rad", "rot3_rad"):
            object.__setattr__(self, name, _finite(name, getattr(self, name)))
        for name in ("beam_center_x_px", "beam_center_y_px"):
            object.__setattr__(self, name, _finite(name, getattr(self, name)))
        for name in ("image_width_px", "image_height_px"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
                raise GeometryError(f"{name} must be a positive integer when supplied")
        # A tilted detector can legitimately have its extrapolated direct beam
        # outside the active raster (the official pyFAI examples do). Preserve
        # the stricter legacy/UI check for the common untilted case.
        if self.is_untilted:
            if self.beam_center_x_px < 0 or self.beam_center_y_px < 0:
                raise GeometryError("untilted beam center cannot be negative")
            if self.image_width_px is not None and self.beam_center_x_px > self.image_width_px:
                raise GeometryError("beam_center_x_px lies beyond the supplied image width")
            if self.image_height_px is not None and self.beam_center_y_px > self.image_height_px:
                raise GeometryError("beam_center_y_px lies beyond the supplied image height")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def is_untilted(self) -> bool:
        return self.rot1_rad == 0.0 and self.rot2_rad == 0.0 and self.rot3_rad == 0.0


@dataclass(frozen=True, slots=True)
class PONIParameters:
    """pyFAI-native calculation geometry."""

    dist_m: float
    poni1_m: float
    poni2_m: float
    rot1_rad: float
    rot2_rad: float
    rot3_rad: float


@dataclass(frozen=True, slots=True)
class Fit2DParameters:
    """Human-facing FIT2D-equivalent geometry derived from the same state."""

    direct_distance_m: float
    center_x_px: float
    center_y_px: float
    tilt_deg: float
    tilt_plane_rotation_deg: float


def _tilt_components(rot1_rad: float, rot2_rad: float) -> tuple[float, float, float, float]:
    """Return cos/sin tilt and cos/sin tilt-plane rotation.

    The equations are independently implemented from pyFAI's published MIT
    FIT2D conversion module so persisted configuration does not depend on a
    private runtime object.
    """

    cos_tilt = math.cos(rot1_rad) * math.cos(rot2_rad)
    if cos_tilt <= 0.0:
        raise GeometryError(
            "detector rotation places the direct-beam intersection behind or parallel "
            "to the detector plane"
        )
    sin_tilt = math.sqrt(max(0.0, 1.0 - cos_tilt * cos_tilt))
    if sin_tilt == 0.0:
        return 1.0, 0.0, 1.0, 0.0
    cos_tpr = max(
        -1.0,
        min(1.0, -math.cos(rot2_rad) * math.sin(rot1_rad) / sin_tilt),
    )
    sin_tpr = max(-1.0, min(1.0, math.sin(rot2_rad) / sin_tilt))
    norm = math.hypot(cos_tpr, sin_tpr)
    if norm == 0.0:
        raise GeometryError("tilt-plane rotation is numerically undefined")
    return cos_tilt, sin_tilt, cos_tpr / norm, sin_tpr / norm


def fit2d_parameters(geometry: DetectorGeometry) -> Fit2DParameters:
    """Derive the auditable FIT2D-style representation."""

    cos_tilt, _sin_tilt, cos_tpr, sin_tpr = _tilt_components(
        geometry.rot1_rad,
        geometry.rot2_rad,
    )
    tilt_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_tilt))))
    tilt_plane_rotation_deg = (
        0.0 if tilt_deg == 0.0 else math.degrees(math.atan2(sin_tpr, cos_tpr))
    )
    return Fit2DParameters(
        direct_distance_m=geometry.detector_distance_m,
        center_x_px=geometry.beam_center_x_px,
        center_y_px=geometry.beam_center_y_px,
        tilt_deg=tilt_deg,
        tilt_plane_rotation_deg=tilt_plane_rotation_deg,
    )


def poni_parameters(geometry: DetectorGeometry) -> PONIParameters:
    """Convert the direct-beam representation to pyFAI PONI parameters."""

    cos_tilt, sin_tilt, cos_tpr, sin_tpr = _tilt_components(
        geometry.rot1_rad,
        geometry.rot2_rad,
    )
    direct_distance = geometry.detector_distance_m
    return PONIParameters(
        dist_m=direct_distance * cos_tilt,
        poni1_m=(
            geometry.beam_center_y_px * geometry.pixel_size_y_m
            - direct_distance * sin_tilt * sin_tpr
        ),
        poni2_m=(
            geometry.beam_center_x_px * geometry.pixel_size_x_m
            - direct_distance * sin_tilt * cos_tpr
        ),
        rot1_rad=geometry.rot1_rad,
        rot2_rad=geometry.rot2_rad,
        rot3_rad=geometry.rot3_rad,
    )


@dataclass(frozen=True, slots=True)
class ScatteringCoordinates:
    two_theta_rad: np.ndarray
    q_nm_inv: np.ndarray
    s_nm_inv: np.ndarray
    azimuth_deg: np.ndarray
    radius_m: np.ndarray


def scattering_coordinates(
    geometry: DetectorGeometry,
    x_px: Any,
    y_px: Any,
) -> ScatteringCoordinates:
    """Evaluate untilted flat-detector scattering coordinates.

    ``s`` is explicitly versioned here as ``2 sin(theta) / wavelength`` and is
    therefore ``q / (2*pi)``.  The beam-centre azimuth is undefined and returned
    as NaN rather than silently labelled 0°.
    """

    if not geometry.is_untilted:
        raise TiltedGeometryUnavailable(
            "non-zero detector rotations require the audited pyFAI geometry adapter"
        )
    x_array, y_array = np.broadcast_arrays(
        np.asarray(x_px, dtype=np.float64), np.asarray(y_px, dtype=np.float64)
    )
    dx_m = (x_array - geometry.beam_center_x_px) * geometry.pixel_size_x_m
    dy_m = (y_array - geometry.beam_center_y_px) * geometry.pixel_size_y_m
    radius_m = np.hypot(dx_m, dy_m)
    two_theta = np.arctan2(radius_m, geometry.detector_distance_m)
    q_m_inv = 4.0 * np.pi * np.sin(0.5 * two_theta) / geometry.wavelength_m
    q_nm_inv = q_m_inv * 1e-9
    s_nm_inv = q_nm_inv / (2.0 * np.pi)
    # y grows down on the displayed image. atan2(dx, -dy) gives the accepted
    # visible convention: up=0, right=90, down=180, left=270.
    azimuth = np.mod(np.degrees(np.arctan2(dx_m, -dy_m)), 360.0)
    azimuth = np.where(radius_m == 0.0, np.nan, azimuth)
    return ScatteringCoordinates(two_theta, q_nm_inv, s_nm_inv, azimuth, radius_m)


def detector_coordinate_arrays(
    geometry: DetectorGeometry,
    shape: tuple[int, int] | None = None,
) -> ScatteringCoordinates:
    """Evaluate every array pixel centre without changing source orientation."""

    if shape is None:
        if geometry.image_height_px is None or geometry.image_width_px is None:
            raise GeometryError("shape is required when image dimensions are absent")
        shape = (geometry.image_height_px, geometry.image_width_px)
    if len(shape) != 2 or any(not isinstance(value, int) or value <= 0 for value in shape):
        raise GeometryError("shape must contain two positive integers")
    row, column = np.indices(shape, dtype=np.float64)
    x_px, y_px = pixel_index_to_continuous(row, column)
    return scattering_coordinates(geometry, x_px, y_px)


def q_nm_inv_from_two_theta(two_theta_rad: Any, wavelength_m: float) -> np.ndarray:
    wavelength = _positive("wavelength_m", wavelength_m)
    angle = np.asarray(two_theta_rad, dtype=np.float64)
    if np.any(~np.isfinite(angle)) or np.any(angle < 0) or np.any(angle >= np.pi):
        raise GeometryError("two_theta_rad must be finite in [0, pi)")
    return 4.0 * np.pi * np.sin(0.5 * angle) / wavelength * 1e-9


def two_theta_rad_from_q_nm_inv(q_nm_inv: Any, wavelength_m: float) -> np.ndarray:
    wavelength = _positive("wavelength_m", wavelength_m)
    q = np.asarray(q_nm_inv, dtype=np.float64)
    if np.any(~np.isfinite(q)) or np.any(q < 0):
        raise GeometryError("q_nm_inv must be finite and non-negative")
    argument = q * 1e9 * wavelength / (4.0 * np.pi)
    if np.any(argument > 1.0):
        raise GeometryError("q_nm_inv is outside the physical domain for this wavelength")
    return 2.0 * np.arcsin(argument)


def geometry_to_document(geometry: DetectorGeometry) -> dict[str, Any]:
    poni = poni_parameters(geometry)
    fit2d = fit2d_parameters(geometry)
    return {
        "schema": GEOMETRY_SCHEMA,
        "conventions": {
            "coordinate": CANONICAL_COORDINATE_CONVENTION,
            "azimuth": VISIBLE_AZIMUTH_CONVENTION,
            "distance": DISTANCE_CONVENTION,
            "center": CENTER_CONVENTION,
            "calculation_geometry": PONI_CONVENTION,
            "q_unit": "nm^-1",
            "s_definition": "s=q/(2*pi)=2*sin(theta)/wavelength",
        },
        "detector": {
            "wavelength_m": geometry.wavelength_m,
            "pixel_size_x_m": geometry.pixel_size_x_m,
            "pixel_size_y_m": geometry.pixel_size_y_m,
            "image_width_px": geometry.image_width_px,
            "image_height_px": geometry.image_height_px,
        },
        "direct_beam": {
            "direct_distance_m": fit2d.direct_distance_m,
            "center_x_px": fit2d.center_x_px,
            "center_y_px": fit2d.center_y_px,
            "tilt_deg": fit2d.tilt_deg,
            "tilt_plane_rotation_deg": fit2d.tilt_plane_rotation_deg,
        },
        "poni": {
            "dist_m": poni.dist_m,
            "poni1_m": poni.poni1_m,
            "poni2_m": poni.poni2_m,
            "rot1_rad": poni.rot1_rad,
            "rot2_rad": poni.rot2_rad,
            "rot3_rad": poni.rot3_rad,
        },
        "provenance": asdict(geometry.provenance),
        "metadata": dict(geometry.metadata),
    }


def geometry_to_json(geometry: DetectorGeometry, *, indent: int = 2) -> str:
    return json.dumps(geometry_to_document(geometry), ensure_ascii=False, indent=indent) + "\n"


def _require_fields(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        actual = sorted(value) if isinstance(value, Mapping) else type(value).__name__
        raise GeometryConfigError(
            f"{label} fields differ: expected {sorted(expected)}, got {actual}"
        )
    return value


def _provenance_and_metadata(document: Mapping[str, Any]) -> tuple[GeometryProvenance, dict[str, Any]]:
    raw_provenance = _require_fields(
        document["provenance"],
        {"method", "source_application", "source_path", "source_convention", "notes"},
        "provenance",
    )
    metadata = document["metadata"]
    if not isinstance(metadata, Mapping):
        raise GeometryConfigError("metadata must be a JSON object")
    try:
        provenance = GeometryProvenance(**dict(raw_provenance))
    except (TypeError, GeometryError) as exc:
        raise GeometryConfigError(f"invalid provenance values: {exc}") from exc
    return provenance, dict(metadata)


def _geometry_from_v1(document: Mapping[str, Any]) -> DetectorGeometry:
    expected_top = {"schema", "conventions", "geometry", "provenance", "metadata"}
    if set(document) != expected_top:
        raise GeometryConfigError(
            f"geometry document fields differ: expected {sorted(expected_top)}, got {sorted(document)}"
        )
    conventions = document["conventions"]
    expected_conventions = {
        "coordinate": CANONICAL_COORDINATE_CONVENTION,
        "azimuth": VISIBLE_AZIMUTH_CONVENTION,
        "q_unit": "nm^-1",
        "s_definition": "s=q/(2*pi)=2*sin(theta)/wavelength",
    }
    if conventions != expected_conventions:
        raise GeometryConfigError("geometry conventions do not match schema version 1")
    raw_geometry = _require_fields(
        document["geometry"],
        {
            "wavelength_m",
            "detector_distance_m",
            "pixel_size_x_m",
            "pixel_size_y_m",
            "beam_center_x_px",
            "beam_center_y_px",
            "image_width_px",
            "image_height_px",
            "rot1_rad",
            "rot2_rad",
            "rot3_rad",
        },
        "geometry v1",
    )
    if any(float(raw_geometry[name]) != 0.0 for name in ("rot1_rad", "rot2_rad", "rot3_rad")):
        raise GeometryConfigError(
            "schema version 1 tilted geometry is ambiguous and cannot be migrated; "
            "supply explicit PONI and direct-beam representations in schema version 2"
        )
    provenance, metadata = _provenance_and_metadata(document)
    try:
        return DetectorGeometry(
            **dict(raw_geometry),
            provenance=provenance,
            metadata=metadata,
        )
    except (TypeError, GeometryError) as exc:
        raise GeometryConfigError(f"invalid geometry values: {exc}") from exc


def _assert_close(label: str, actual: float, expected: float, *, abs_tol: float) -> None:
    if not math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=abs_tol):
        raise GeometryConfigError(
            f"schema version 2 dual geometry is inconsistent for {label}: "
            f"stored={actual!r}, derived={expected!r}"
        )


def _geometry_from_v2(document: Mapping[str, Any]) -> DetectorGeometry:
    expected_top = {
        "schema",
        "conventions",
        "detector",
        "direct_beam",
        "poni",
        "provenance",
        "metadata",
    }
    if set(document) != expected_top:
        raise GeometryConfigError(
            f"geometry document fields differ: expected {sorted(expected_top)}, got {sorted(document)}"
        )
    expected_conventions = {
        "coordinate": CANONICAL_COORDINATE_CONVENTION,
        "azimuth": VISIBLE_AZIMUTH_CONVENTION,
        "distance": DISTANCE_CONVENTION,
        "center": CENTER_CONVENTION,
        "calculation_geometry": PONI_CONVENTION,
        "q_unit": "nm^-1",
        "s_definition": "s=q/(2*pi)=2*sin(theta)/wavelength",
    }
    if document["conventions"] != expected_conventions:
        raise GeometryConfigError("geometry conventions do not match schema version 2")
    detector = _require_fields(
        document["detector"],
        {
            "wavelength_m",
            "pixel_size_x_m",
            "pixel_size_y_m",
            "image_width_px",
            "image_height_px",
        },
        "detector",
    )
    direct = _require_fields(
        document["direct_beam"],
        {
            "direct_distance_m",
            "center_x_px",
            "center_y_px",
            "tilt_deg",
            "tilt_plane_rotation_deg",
        },
        "direct_beam",
    )
    poni = _require_fields(
        document["poni"],
        {"dist_m", "poni1_m", "poni2_m", "rot1_rad", "rot2_rad", "rot3_rad"},
        "poni",
    )
    provenance, metadata = _provenance_and_metadata(document)
    try:
        geometry = DetectorGeometry(
            wavelength_m=detector["wavelength_m"],
            detector_distance_m=direct["direct_distance_m"],
            pixel_size_x_m=detector["pixel_size_x_m"],
            pixel_size_y_m=detector["pixel_size_y_m"],
            beam_center_x_px=direct["center_x_px"],
            beam_center_y_px=direct["center_y_px"],
            image_width_px=detector["image_width_px"],
            image_height_px=detector["image_height_px"],
            rot1_rad=poni["rot1_rad"],
            rot2_rad=poni["rot2_rad"],
            rot3_rad=poni["rot3_rad"],
            provenance=provenance,
            metadata=metadata,
        )
        derived_poni = poni_parameters(geometry)
        derived_fit2d = fit2d_parameters(geometry)
    except (TypeError, GeometryError) as exc:
        raise GeometryConfigError(f"invalid geometry values: {exc}") from exc
    for name in ("dist_m", "poni1_m", "poni2_m", "rot1_rad", "rot2_rad", "rot3_rad"):
        _assert_close(name, poni[name], getattr(derived_poni, name), abs_tol=1e-12)
    for name in (
        "direct_distance_m",
        "center_x_px",
        "center_y_px",
        "tilt_deg",
        "tilt_plane_rotation_deg",
    ):
        tolerance = 1e-9 if name.endswith("_px") or name.endswith("_deg") else 1e-12
        _assert_close(name, direct[name], getattr(derived_fit2d, name), abs_tol=tolerance)
    return geometry


def geometry_from_document(document: Mapping[str, Any]) -> DetectorGeometry:
    schema = document.get("schema")
    if schema == LEGACY_GEOMETRY_SCHEMA:
        return _geometry_from_v1(document)
    if schema == GEOMETRY_SCHEMA:
        return _geometry_from_v2(document)
    raise GeometryConfigError(f"unsupported geometry schema: {schema!r}")


def geometry_from_json(payload: str) -> DetectorGeometry:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise GeometryConfigError(f"invalid geometry JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise GeometryConfigError("geometry JSON root must be an object")
    return geometry_from_document(document)


def load_geometry(path: str | os.PathLike[str]) -> DetectorGeometry:
    source = Path(path).expanduser().absolute()
    return geometry_from_json(source.read_text(encoding="utf-8"))


def save_geometry(
    geometry: DetectorGeometry,
    path: str | os.PathLike[str],
    *,
    protected_roots: tuple[str | os.PathLike[str], ...],
    overwrite: bool = False,
) -> Path:
    """Atomically write a config outside caller-supplied protected roots."""

    destination = Path(path).expanduser().absolute()
    resolved_destination = destination.resolve(strict=False)
    for protected in protected_roots:
        protected_path = Path(protected).expanduser().absolute().resolve(strict=False)
        if resolved_destination == protected_path or protected_path in resolved_destination.parents:
            raise GeometryConfigError(f"refusing to write inside protected root: {protected_path}")
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(geometry_to_json(geometry))
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination
