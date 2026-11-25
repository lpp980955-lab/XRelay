"""Offline calibrant library with source-pinned d-spacings."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys
import re
from types import MappingProxyType
from typing import Any, Mapping

from .geometry import DetectorGeometry, GeometryError


CALIBRANT_SCHEMA = "saxs_waxs_calibrant_library/1"
DEFAULT_LIBRARY_PATH = (
    Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    / "resources"
    / "calibrants"
    / "pyfai_reference_v1.json"
)


class CalibrantLibraryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CalibrantPeak:
    d_angstrom: float
    hkl: tuple[int, int, int] | None
    multiplicity: int | None = None
    relative_intensity: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.d_angstrom) or self.d_angstrom <= 0:
            raise CalibrantLibraryError("d_angstrom must be finite and positive")
        if self.hkl is not None and (
            len(self.hkl) != 3 or any(not isinstance(value, int) for value in self.hkl)
        ):
            raise CalibrantLibraryError("hkl must contain exactly three integers")
        if self.multiplicity is not None and self.multiplicity <= 0:
            raise CalibrantLibraryError("multiplicity must be positive when supplied")
        if self.relative_intensity is not None and (
            not math.isfinite(self.relative_intensity) or self.relative_intensity < 0
        ):
            raise CalibrantLibraryError("relative_intensity must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class Calibrant:
    id: str
    name: str
    aliases: tuple[str, ...]
    source_path: str
    source_blob_sha: str
    source_url: str
    peaks: tuple[CalibrantPeak, ...]

    def __post_init__(self) -> None:
        if not self.id or not self.name or not self.peaks:
            raise CalibrantLibraryError("calibrant id, name and peaks are required")
        if len(self.source_blob_sha) != 40:
            raise CalibrantLibraryError("source_blob_sha must be a Git blob SHA-1")
        d = [peak.d_angstrom for peak in self.peaks]
        if any(left < right for left, right in zip(d, d[1:])):
            raise CalibrantLibraryError(f"{self.id} d-spacings must be non-increasing")


@dataclass(frozen=True, slots=True)
class CalibrantRing:
    ordinal: int
    peak: CalibrantPeak
    two_theta_rad: float
    q_nm_inv: float
    radius_m: float
    radius_x_px: float
    radius_y_px: float


@dataclass(frozen=True, slots=True)
class CalibrantLibrary:
    calibrants: tuple[Calibrant, ...]
    source_repository: str
    retrieved_date: str
    _aliases: Mapping[str, Calibrant]

    def __post_init__(self) -> None:
        aliases: dict[str, Calibrant] = {}
        for calibrant in self.calibrants:
            for alias in (calibrant.id, calibrant.name, *calibrant.aliases):
                key = alias.casefold().strip()
                previous = aliases.get(key)
                if previous is not None and previous is not calibrant:
                    raise CalibrantLibraryError(
                        f"ambiguous calibrant alias {alias!r}: {previous.id} and {calibrant.id}"
                    )
                aliases[key] = calibrant
        object.__setattr__(self, "_aliases", MappingProxyType(aliases))

    def resolve(self, name_or_alias: str) -> Calibrant:
        try:
            return self._aliases[name_or_alias.casefold().strip()]
        except KeyError as exc:
            raise CalibrantLibraryError(f"unknown calibrant: {name_or_alias!r}") from exc

    def with_calibrant(self, calibrant: Calibrant) -> "CalibrantLibrary":
        """Return a new library containing one additional session calibrant."""

        return CalibrantLibrary(
            calibrants=(*self.calibrants, calibrant),
            source_repository=self.source_repository,
            retrieved_date=self.retrieved_date,
            _aliases=MappingProxyType({}),
        )


def _load_peak(raw: Mapping[str, Any]) -> CalibrantPeak:
    expected = {"d_angstrom", "hkl", "multiplicity", "relative_intensity"}
    if set(raw) != expected:
        raise CalibrantLibraryError("calibrant peak fields do not match schema")
    return CalibrantPeak(
        d_angstrom=float(raw["d_angstrom"]),
        hkl=None if raw["hkl"] is None else tuple(raw["hkl"]),
        multiplicity=raw["multiplicity"],
        relative_intensity=raw["relative_intensity"],
    )


def load_calibrant_library(path: str | Path = DEFAULT_LIBRARY_PATH) -> CalibrantLibrary:
    source = Path(path).expanduser().absolute()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrantLibraryError(f"cannot read calibrant library {source}: {exc}") from exc
    expected_top = {"schema", "source_repository", "retrieved_date", "calibrants"}
    if not isinstance(document, Mapping) or set(document) != expected_top:
        raise CalibrantLibraryError("calibrant library root does not match schema")
    if document["schema"] != CALIBRANT_SCHEMA:
        raise CalibrantLibraryError(f"unsupported calibrant schema: {document['schema']!r}")
    calibrants: list[Calibrant] = []
    expected_fields = {
        "id",
        "name",
        "aliases",
        "source_path",
        "source_blob_sha",
        "source_url",
        "peaks",
    }
    for raw in document["calibrants"]:
        if not isinstance(raw, Mapping) or set(raw) != expected_fields:
            raise CalibrantLibraryError("calibrant fields do not match schema")
        calibrants.append(
            Calibrant(
                id=str(raw["id"]),
                name=str(raw["name"]),
                aliases=tuple(str(item) for item in raw["aliases"]),
                source_path=str(raw["source_path"]),
                source_blob_sha=str(raw["source_blob_sha"]),
                source_url=str(raw["source_url"]),
                peaks=tuple(_load_peak(peak) for peak in raw["peaks"]),
            )
        )
    return CalibrantLibrary(
        calibrants=tuple(calibrants),
        source_repository=str(document["source_repository"]),
        retrieved_date=str(document["retrieved_date"]),
        _aliases=MappingProxyType({}),
    )


_HKL_PATTERN = re.compile(
    r"\(\s*(-?\d+)\s*[, ]\s*(-?\d+)\s*[, ]\s*(-?\d+)\s*\)"
)


def load_custom_calibrant(path: str | Path) -> Calibrant:
    """Load a user-supplied decreasing d-spacing list without modifying it.

    The accepted format follows pyFAI/FIT2D ``.D`` files: one positive
    d-spacing in angstrom per non-comment line, optionally followed by a
    ``# (h k l)`` annotation.  The source content hash is part of the stable
    session id so a changed file cannot silently impersonate an earlier one.
    """

    source = Path(path).expanduser().absolute()
    try:
        payload = source.read_bytes()
        text = payload.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise CalibrantLibraryError(f"cannot read custom calibrant {source}: {exc}") from exc

    peaks: list[CalibrantPeak] = []
    display_name = source.stem
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if stripped.casefold().startswith("# calibrant:"):
                candidate = stripped.partition(":")[2].strip()
                if candidate:
                    display_name = candidate
            continue
        value_text, _, annotation = raw_line.partition("#")
        fields = value_text.replace(",", " ").split()
        if len(fields) != 1:
            raise CalibrantLibraryError(
                f"custom calibrant line {line_number} must contain one d-spacing"
            )
        try:
            d_angstrom = float(fields[0])
        except ValueError as exc:
            raise CalibrantLibraryError(
                f"custom calibrant line {line_number} is not a number"
            ) from exc
        match = _HKL_PATTERN.search(annotation)
        hkl = tuple(int(match.group(index)) for index in (1, 2, 3)) if match else None
        peaks.append(
            CalibrantPeak(
                d_angstrom=d_angstrom,
                hkl=hkl,
                multiplicity=None,
                relative_intensity=None,
            )
        )
    if not peaks:
        raise CalibrantLibraryError("custom calibrant contains no d-spacings")

    digest = hashlib.sha256(payload).hexdigest()
    git_blob_sha = hashlib.sha1(
        b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
    ).hexdigest()
    return Calibrant(
        id=f"custom:{source.stem}:{digest[:12]}",
        name=display_name,
        aliases=(source.stem,),
        source_path=str(source),
        source_blob_sha=git_blob_sha,
        source_url="",
        peaks=tuple(peaks),
    )


def ring_table(calibrant: Calibrant, geometry: DetectorGeometry) -> tuple[CalibrantRing, ...]:
    """Return physically allowed flat-detector rings for one wavelength."""

    if not geometry.is_untilted:
        raise GeometryError(
            "circular calibrant ring preview currently requires zero tilt; "
            "use the pyFAI coordinate path for tilted detector geometry"
        )

    rings: list[CalibrantRing] = []
    wavelength_a = geometry.wavelength_m * 1e10
    for ordinal, peak in enumerate(calibrant.peaks, start=1):
        bragg_argument = wavelength_a / (2.0 * peak.d_angstrom)
        if bragg_argument > 1.0:
            continue
        theta = math.asin(bragg_argument)
        two_theta = 2.0 * theta
        radius_m = math.tan(two_theta) * geometry.detector_distance_m
        rings.append(
            CalibrantRing(
                ordinal=ordinal,
                peak=peak,
                two_theta_rad=two_theta,
                q_nm_inv=2.0 * math.pi / (peak.d_angstrom * 0.1),
                radius_m=radius_m,
                radius_x_px=radius_m / geometry.pixel_size_x_m,
                radius_y_px=radius_m / geometry.pixel_size_y_m,
            )
        )
    return tuple(rings)


def visible_rings(
    calibrant: Calibrant,
    geometry: DetectorGeometry,
) -> tuple[CalibrantRing, ...]:
    """Return rings whose physical ellipse intersects the supplied image rectangle."""

    if geometry.image_width_px is None or geometry.image_height_px is None:
        raise GeometryError("image dimensions are required to determine visible rings")
    cx = geometry.beam_center_x_px
    cy = geometry.beam_center_y_px
    width = float(geometry.image_width_px)
    height = float(geometry.image_height_px)
    dx_near = 0.0 if 0.0 <= cx <= width else min(abs(cx), abs(cx - width))
    dy_near = 0.0 if 0.0 <= cy <= height else min(abs(cy), abs(cy - height))
    minimum_radius_m = math.hypot(
        dx_near * geometry.pixel_size_x_m,
        dy_near * geometry.pixel_size_y_m,
    )
    maximum_radius_m = max(
        math.hypot(
            (x - cx) * geometry.pixel_size_x_m,
            (y - cy) * geometry.pixel_size_y_m,
        )
        for x in (0.0, width)
        for y in (0.0, height)
    )
    return tuple(
        ring
        for ring in ring_table(calibrant, geometry)
        if minimum_radius_m <= ring.radius_m <= maximum_radius_m
    )
