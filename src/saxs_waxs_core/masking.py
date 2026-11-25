"""Immutable, auditable detector and user mask composition.

The canonical convention matches pyFAI: ``True``/non-zero means excluded.
Masking never rewrites detector intensities and never fills missing regions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.stats import binned_statistic

from .models import ImageFrame


MASK_AUDIT_SCHEMA = "saxs_waxs_mask_audit/1"
MASK_CONVENTION = "true_means_excluded"
EXTERNAL_MASK_FORMATS = (".npy", ".tif", ".tiff", ".edf", ".cbf", ".msk")


class MaskError(ValueError):
    """Raised when a mask cannot be applied without ambiguity."""


@dataclass(frozen=True, slots=True)
class AutomaticMaskSuggestion:
    """Editable beamstop/support descriptors proposed from one detector frame."""

    descriptors: Sequence[Mapping[str, Any]]
    confidence: str
    audit: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "descriptors",
            tuple(MappingProxyType(dict(item)) for item in self.descriptors),
        )
        object.__setattr__(self, "audit", MappingProxyType(dict(self.audit)))


@dataclass(frozen=True, slots=True)
class UserMaskLayer:
    name: str
    mask: np.ndarray
    kind: str = "user"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise MaskError("mask layer name must not be empty")
        array = np.array(self.mask, dtype=bool, order="C", copy=True)
        if array.ndim != 2:
            raise MaskError("mask layer must be a 2D array")
        array.setflags(write=False)
        object.__setattr__(self, "mask", array)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class MaskRecipe:
    mask_nonfinite: bool = True
    mask_tvx_negative_sentinels: bool = True
    mask_count_cutoff: bool = True
    user_layers: Sequence[UserMaskLayer] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_layers", tuple(self.user_layers))


@dataclass(frozen=True, slots=True)
class MaskResult:
    excluded: np.ndarray
    recipe: MaskRecipe
    layer_counts: Mapping[str, int]
    audit: Mapping[str, Any]

    def __post_init__(self) -> None:
        array = np.array(self.excluded, dtype=bool, order="C", copy=True)
        if array.ndim != 2:
            raise MaskError("combined mask must be a 2D array")
        array.setflags(write=False)
        object.__setattr__(self, "excluded", array)
        object.__setattr__(self, "layer_counts", MappingProxyType(dict(self.layer_counts)))
        object.__setattr__(self, "audit", MappingProxyType(dict(self.audit)))

    @property
    def excluded_pixel_count(self) -> int:
        return int(np.count_nonzero(self.excluded))

    @property
    def valid_pixel_count(self) -> int:
        return int(self.excluded.size - self.excluded_pixel_count)

    def as_pyfai_mask(self) -> np.ndarray:
        """Return a writable int8 copy; non-zero pixels are excluded by pyFAI."""

        return self.excluded.astype(np.int8, copy=True)


def circle_mask(
    shape: tuple[int, int], center_xy_px: tuple[float, float], radius_px: float
) -> np.ndarray:
    if len(shape) != 2 or min(shape) <= 0:
        raise MaskError("circle mask shape must contain two positive dimensions")
    if not np.isfinite(radius_px) or radius_px < 0.0:
        raise MaskError("circle mask radius must be finite and non-negative")
    center_x, center_y = (float(value) for value in center_xy_px)
    if not np.isfinite([center_x, center_y]).all():
        raise MaskError("circle mask center must be finite")
    rows, columns = np.indices(shape, dtype=np.float64)
    return (columns - center_x) ** 2 + (rows - center_y) ** 2 <= radius_px**2


def rectangle_mask(
    shape: tuple[int, int], x0_px: float, y0_px: float, x1_px: float, y1_px: float
) -> np.ndarray:
    if len(shape) != 2 or min(shape) <= 0:
        raise MaskError("rectangle mask shape must contain two positive dimensions")
    coordinates = np.asarray([x0_px, y0_px, x1_px, y1_px], dtype=np.float64)
    if not np.isfinite(coordinates).all():
        raise MaskError("rectangle mask coordinates must be finite")
    left, right = sorted((float(x0_px), float(x1_px)))
    top, bottom = sorted((float(y0_px), float(y1_px)))
    rows, columns = np.indices(shape, dtype=np.float64)
    return (columns >= left) & (columns <= right) & (rows >= top) & (rows <= bottom)


def polygon_mask(
    shape: tuple[int, int], vertices_xy_px: Sequence[tuple[float, float]]
) -> np.ndarray:
    """Return pixels whose centers are inside or on a simple polygon boundary."""

    if len(shape) != 2 or min(shape) <= 0:
        raise MaskError("polygon mask shape must contain two positive dimensions")
    vertices = np.asarray(vertices_xy_px, dtype=np.float64)
    if vertices.ndim != 2 or vertices.shape[1:] != (2,) or len(vertices) < 3:
        raise MaskError("polygon mask requires at least three (x, y) vertices")
    if not np.isfinite(vertices).all():
        raise MaskError("polygon mask vertices must be finite")

    rows, columns = np.indices(shape, dtype=np.float64)
    inside = np.zeros(shape, dtype=bool)
    boundary = np.zeros(shape, dtype=bool)
    for start, end in zip(vertices, np.roll(vertices, -1, axis=0)):
        x0, y0 = start
        x1, y1 = end
        crosses = (y0 > rows) != (y1 > rows)
        x_cross = (x1 - x0) * (rows - y0) / (y1 - y0 + np.finfo(float).eps) + x0
        inside ^= crosses & (columns < x_cross)

        dx = x1 - x0
        dy = y1 - y0
        cross = (columns - x0) * dy - (rows - y0) * dx
        tolerance = np.finfo(float).eps * max(1.0, abs(dx), abs(dy)) * 32.0
        on_line = np.abs(cross) <= tolerance
        within = (
            (columns >= min(x0, x1))
            & (columns <= max(x0, x1))
            & (rows >= min(y0, y1))
            & (rows <= max(y0, y1))
        )
        boundary |= on_line & within
    return inside | boundary


def suggest_beamstop_support_mask(
    data: np.ndarray,
    center_xy_px: tuple[float, float],
    *,
    deficit_ratio: float = 0.35,
) -> AutomaticMaskSuggestion:
    """Suggest an editable central beamstop and its one-sided support shadow.

    This is deliberately a candidate generator, not an unconditional mask.  It
    compares every pixel with the robust median at the same detector radius,
    then looks for a low-intensity angular ray leaving a low central disk.  The
    result is returned as portable circle/polygon descriptors so the user can
    inspect and adjust it before integration.
    """

    array = np.asarray(data, dtype=np.float64)
    if array.ndim != 2 or min(array.shape) <= 0:
        raise MaskError("automatic mask suggestion requires a non-empty 2D array")
    center_x, center_y = (float(value) for value in center_xy_px)
    if not np.isfinite([center_x, center_y]).all():
        raise MaskError("automatic mask centre must be finite")
    height, width = array.shape
    if not (0.0 <= center_x <= width and 0.0 <= center_y <= height):
        raise MaskError("automatic mask centre lies outside the detector")
    if not np.isfinite(deficit_ratio) or not 0.05 <= deficit_ratio <= 0.8:
        raise MaskError("automatic mask deficit_ratio must be between 0.05 and 0.8")

    rows, columns = np.indices(array.shape, dtype=np.float64)
    x = columns + 0.5
    y = rows + 0.5
    dx = x - center_x
    dy = y - center_y
    radius = np.hypot(dx, dy)
    valid = np.isfinite(array) & (array >= 0.0)
    if int(np.count_nonzero(valid)) < 100:
        raise MaskError("too few finite non-negative pixels for automatic mask suggestion")

    central_scale = max(6.0, 0.015 * min(array.shape))
    reference_annulus = valid & (radius >= 1.5 * central_scale) & (
        radius <= 3.2 * central_scale
    )
    if int(np.count_nonzero(reference_annulus)) < 32:
        raise MaskError("insufficient pixels around the beam centre")
    central_reference = float(np.median(array[reference_annulus]))
    if not np.isfinite(central_reference) or central_reference <= 0.0:
        raise MaskError("central neighbourhood has no positive intensity reference")
    central_low = valid & (array <= 0.30 * central_reference)

    radial_index = np.floor(radius).astype(np.int32)
    max_bin = int(radial_index[valid].max())
    edges = np.arange(max_bin + 2, dtype=np.float64)
    radial_median, _, _ = binned_statistic(
        radius[valid],
        array[valid],
        statistic="median",
        bins=edges,
    )
    finite_bins = np.flatnonzero(np.isfinite(radial_median))
    if finite_bins.size < 2:
        raise MaskError("radial reference profile could not be estimated")
    radial_median = np.interp(
        np.arange(radial_median.size, dtype=np.float64),
        finite_bins.astype(np.float64),
        radial_median[finite_bins],
    )
    expected = radial_median[np.minimum(radial_index, radial_median.size - 1)]
    deficit = valid & (expected > 0.0) & (array <= deficit_ratio * expected)

    central_limit = int(math.ceil(3.0 * central_scale))
    radial_total = np.bincount(
        radial_index[(valid) & (radial_index <= central_limit)],
        minlength=central_limit + 1,
    )
    radial_low = np.bincount(
        radial_index[(central_low) & (radial_index <= central_limit)],
        minlength=central_limit + 1,
    )
    coverage = np.divide(
        radial_low,
        radial_total,
        out=np.zeros_like(radial_low, dtype=np.float64),
        where=radial_total > 0,
    )
    last_disk_bin = -1
    misses = 0
    for index, fraction in enumerate(coverage):
        if fraction >= 0.45:
            last_disk_bin = index
            misses = 0
        elif last_disk_bin >= 0:
            misses += 1
            if misses >= 3:
                break
    if last_disk_bin < 2:
        raise MaskError(
            "未识别到可靠的中心beamstop；请检查中心点或保留手动MASK"
        )
    beamstop_radius = float(last_disk_bin + 2)

    arm_domain = valid & (radius >= beamstop_radius + 3.0)
    angle = np.mod(np.arctan2(dy, dx), 2.0 * np.pi)
    angle_bins = 720
    angle_index = np.minimum(
        (angle * angle_bins / (2.0 * np.pi)).astype(np.int32),
        angle_bins - 1,
    )
    angular_total = np.bincount(
        angle_index[arm_domain], minlength=angle_bins
    ).astype(np.float64)
    angular_low = np.bincount(
        angle_index[arm_domain & deficit], minlength=angle_bins
    ).astype(np.float64)
    angular_score = np.divide(
        angular_low,
        angular_total,
        out=np.zeros(angle_bins, dtype=np.float64),
        where=angular_total >= 8,
    )
    kernel_size = 9
    half_kernel = kernel_size // 2
    padded = np.r_[
        angular_score[-half_kernel:],
        angular_score,
        angular_score[:half_kernel],
    ]
    smoothed = np.convolve(padded, np.ones(kernel_size) / kernel_size, mode="valid")
    smoothed = smoothed[:angle_bins]
    peak_index = int(np.argmax(smoothed))
    peak_score = float(smoothed[peak_index])
    baseline_score = float(np.median(smoothed))

    descriptors: list[dict[str, Any]] = [
        {
            "kind": "circle",
            "pos": (center_x - beamstop_radius, center_y - beamstop_radius),
            "size": (2.0 * beamstop_radius, 2.0 * beamstop_radius),
        }
    ]
    support_detected = peak_score >= max(0.30, baseline_score + 0.16)
    support_angle_deg: float | None = None
    support_width_px: float | None = None
    if support_detected:
        support_angle = (peak_index + 0.5) * 2.0 * np.pi / angle_bins
        axis = np.array([math.cos(support_angle), math.sin(support_angle)])
        normal = np.array([-axis[1], axis[0]])
        angular_delta = np.abs(
            np.angle(np.exp(1j * (angle - support_angle)))
        )
        support_pixels = arm_domain & deficit & (angular_delta <= math.radians(4.0))
        offsets = np.column_stack((dx[support_pixels], dy[support_pixels]))
        projections = offsets @ axis
        perpendicular = offsets @ normal
        keep = projections > beamstop_radius
        projections = projections[keep]
        perpendicular = perpendicular[keep]
        if projections.size >= 20:
            half_width = float(np.percentile(np.abs(perpendicular), 90.0) + 2.0)
            half_width = min(max(half_width, 2.0), 0.08 * min(array.shape))
            edge_limits: list[float] = []
            if axis[0] > 1e-12:
                edge_limits.append((width - center_x) / axis[0])
            elif axis[0] < -1e-12:
                edge_limits.append((0.0 - center_x) / axis[0])
            if axis[1] > 1e-12:
                edge_limits.append((height - center_y) / axis[1])
            elif axis[1] < -1e-12:
                edge_limits.append((0.0 - center_y) / axis[1])
            arm_length = min(value for value in edge_limits if value > 0.0)
            start = np.array([center_x, center_y])
            end = start + axis * arm_length
            corners = (
                start - normal * half_width,
                start + normal * half_width,
                end + normal * half_width,
                end - normal * half_width,
            )
            descriptors.append(
                {
                    "kind": "polygon",
                    "points": tuple((float(point[0]), float(point[1])) for point in corners),
                }
            )
            support_angle_deg = math.degrees(support_angle) % 360.0
            support_width_px = 2.0 * half_width
        else:
            support_detected = False

    confidence = "high" if support_detected and peak_score >= 0.65 else "medium"
    audit = {
        "method": "radial_median_deficit_and_angular_persistence/1",
        "center_xy_px": (center_x, center_y),
        "beamstop_radius_px": beamstop_radius,
        "central_reference": central_reference,
        "deficit_ratio": float(deficit_ratio),
        "support_detected": support_detected,
        "support_angle_image_deg": support_angle_deg,
        "support_width_px": support_width_px,
        "support_peak_score": peak_score,
        "support_baseline_score": baseline_score,
        "candidate_pixel_count": int(np.count_nonzero(deficit)),
    }
    return AutomaticMaskSuggestion(tuple(descriptors), confidence, audit)


def user_mask_layers_from_descriptors(
    shape: tuple[int, int],
    descriptors: Sequence[Mapping[str, Any]],
) -> tuple[UserMaskLayer, ...]:
    """Rasterize portable ROI descriptors using the canonical pixel rules.

    The descriptors contain geometry, never a serialized detector-sized bitmap.
    This keeps a processing recipe inspectable and allows the same definition to
    be used by the interactive preview and a future batch worker.
    """

    if len(shape) != 2 or min(shape) <= 0:
        raise MaskError("mask descriptor shape must contain two positive dimensions")
    layers: list[UserMaskLayer] = []
    for index, raw in enumerate(descriptors, start=1):
        if not isinstance(raw, Mapping):
            raise MaskError(f"manual mask descriptor {index} must be an object")
        descriptor = dict(raw)
        kind = str(descriptor.get("kind", ""))
        try:
            if kind in {"circle", "rectangle", "occlusion_rectangle"}:
                if set(descriptor) != {"kind", "pos", "size"}:
                    raise MaskError(
                        f"{kind} descriptor fields must be kind, pos and size"
                    )
                pos_x, pos_y = (float(value) for value in descriptor["pos"])
                size_x, size_y = (float(value) for value in descriptor["size"])
                if not np.isfinite([pos_x, pos_y, size_x, size_y]).all():
                    raise MaskError(f"{kind} descriptor values must be finite")
                if size_x <= 0.0 or size_y <= 0.0:
                    raise MaskError(f"{kind} descriptor sizes must be positive")
                if kind == "circle":
                    center = (pos_x + size_x / 2.0, pos_y + size_y / 2.0)
                    mask = circle_mask(shape, center, min(size_x, size_y) / 2.0)
                else:
                    mask = rectangle_mask(
                        shape, pos_x, pos_y, pos_x + size_x, pos_y + size_y
                    )
            elif kind in {"polygon", "occlusion_polygon"}:
                if set(descriptor) != {"kind", "points"}:
                    raise MaskError(
                        f"{kind} descriptor fields must be kind and points"
                    )
                points = tuple(
                    (float(point[0]), float(point[1]))
                    for point in descriptor["points"]
                )
                mask = polygon_mask(shape, points)
            else:
                raise MaskError(f"unsupported manual mask descriptor kind: {kind!r}")
        except (TypeError, ValueError, KeyError) as exc:
            raise MaskError(f"invalid manual mask descriptor {index}: {exc}") from exc
        layers.append(
            UserMaskLayer(
                f"{kind}-{index}",
                mask,
                kind=kind,
                metadata=descriptor,
            )
        )
    return tuple(layers)


def load_external_mask_layer(
    path: str | Path,
    expected_shape: tuple[int, int],
    *,
    expected_size_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> UserMaskLayer:
    """Read a 2D non-zero-is-excluded mask without reorientation or resizing."""

    source = Path(path).expanduser().absolute()
    suffix = source.suffix.casefold()
    if suffix not in EXTERNAL_MASK_FORMATS:
        raise MaskError(
            f"unsupported external mask suffix {suffix!r}; supported: "
            + ", ".join(EXTERNAL_MASK_FORMATS)
        )
    if not source.is_file():
        raise MaskError(f"external mask file does not exist: {source}")
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    sha256 = digest.hexdigest()
    if expected_size_bytes is not None and size != expected_size_bytes:
        raise MaskError("external mask file size differs from the saved recipe")
    if expected_sha256 is not None and sha256 != expected_sha256.lower():
        raise MaskError("external mask SHA-256 differs from the saved recipe")

    try:
        if suffix == ".npy":
            values = np.load(source, allow_pickle=False)
            format_id = "numpy_npy"
        else:
            import fabio

            image = fabio.open(str(source))
            try:
                values = np.asarray(image.data)
                format_id = type(image).__name__
            finally:
                image.close()
    except Exception as exc:
        raise MaskError(f"cannot read external mask {source.name}: {exc}") from exc
    if values.ndim != 2:
        raise MaskError(f"external mask must be 2D, got shape {values.shape!r}")
    if tuple(values.shape) != tuple(expected_shape):
        raise MaskError(
            "external mask shape differs from the detector image; "
            f"no transpose or resize was applied: {values.shape!r} != {expected_shape!r}"
        )
    if not np.isfinite(values).all():
        raise MaskError("external mask contains NaN or Inf")
    excluded = np.not_equal(values, 0)
    return UserMaskLayer(
        source.name,
        excluded,
        kind="external_file",
        metadata={
            "path": str(source),
            "size_bytes": size,
            "sha256": sha256,
            "format_id": format_id,
            "source_dtype": str(values.dtype),
            "convention": "nonzero_means_excluded",
            "orientation_transform": "none",
            "excluded_pixel_count": int(np.count_nonzero(excluded)),
        },
    )


def build_mask(frame: ImageFrame, recipe: MaskRecipe | None = None) -> MaskResult:
    """Compose mask layers from the original frame without changing its values."""

    recipe = recipe or MaskRecipe()
    expected_shape = (frame.metadata.height_px, frame.metadata.width_px)
    if frame.data.shape != expected_shape:
        raise MaskError(
            "image metadata dimensions do not match array shape: "
            f"{expected_shape!r} != {frame.data.shape!r}"
        )
    combined = np.zeros(frame.data.shape, dtype=bool)
    layer_counts: dict[str, int] = {}
    skipped: dict[str, str] = {}

    def add_layer(name: str, values: np.ndarray) -> None:
        layer = np.asarray(values, dtype=bool)
        if layer.shape != frame.data.shape:
            raise MaskError(
                f"mask layer {name!r} shape differs from image: "
                f"{layer.shape!r} != {frame.data.shape!r}"
            )
        layer_counts[name] = int(np.count_nonzero(layer))
        np.logical_or(combined, layer, out=combined)

    if frame.metadata.detector_mask is not None:
        add_layer('detector_metadata',frame.metadata.detector_mask)

    if recipe.mask_nonfinite or frame.metadata.embedded_processing:
        add_layer("nonfinite", ~np.isfinite(frame.data))

    if recipe.mask_tvx_negative_sentinels:
        is_raw_tvx_integer = (
            frame.metadata.format_id.startswith("tvx_tiff")
            and np.issubdtype(frame.data.dtype, np.signedinteger)
        )
        if is_raw_tvx_integer:
            add_layer("tvx_negative_sentinel", frame.data < 0)
        else:
            skipped["tvx_negative_sentinel"] = (
                "requires an original signed-integer tvx_tiff frame"
            )

    cutoff = frame.metadata.count_cutoff
    if recipe.mask_count_cutoff:
        if cutoff is None:
            skipped["count_cutoff"] = "metadata count_cutoff is unavailable"
        elif cutoff <= 0:
            raise MaskError("metadata count_cutoff must be positive")
        else:
            add_layer("count_cutoff", frame.data >= cutoff)

    for index, layer in enumerate(recipe.user_layers, start=1):
        add_layer(f"user:{index}:{layer.name}", layer.mask)

    external_layers = [
        dict(layer.metadata)
        for layer in recipe.user_layers
        if layer.kind == "external_file"
    ]
    source_mask_file_applied = bool(
        frame.metadata.excluded_pixels_file
        and any(
            Path(str(item.get("path", ""))).name
            == frame.metadata.excluded_pixels_file
            for item in external_layers
        )
    )

    audit = {
        "schema": MASK_AUDIT_SCHEMA,
        "convention": MASK_CONVENTION,
        "source": str(frame.metadata.source_path),
        "source_format": frame.metadata.format_id,
        "shape": list(frame.data.shape),
        "layer_counts_before_union": dict(layer_counts),
        "skipped_layers": skipped,
        "excluded_pixel_count": int(np.count_nonzero(combined)),
        "valid_pixel_count": int(combined.size - np.count_nonzero(combined)),
        "source_excluded_pixel_count_metadata": frame.metadata.excluded_pixel_count,
        "source_excluded_pixels_file": frame.metadata.excluded_pixels_file,
        "source_mask_file_applied": source_mask_file_applied,
        "external_mask_file_applied": bool(external_layers),
        "external_mask_files": external_layers,
        "negative_background_values_masked": False,
        "notes": recipe.notes,
    }
    return MaskResult(combined, recipe, layer_counts, audit)
