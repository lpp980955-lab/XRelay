"""Preview-only relative regions for replaying overlays across image sizes.

Relative coordinates are deliberately isolated from processing recipes.  They
are a viewer convenience, not a quantitative integration or MASK definition.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence


RELATIVE_PREVIEW_REGION_SCHEMA = "saxs_waxs_relative_preview_region/1"
_BOX_KINDS = {"circle", "rectangle", "occlusion_rectangle"}
_POLYGON_KINDS = {"polygon", "occlusion_polygon"}


class RelativeRegionError(ValueError):
    """Raised when a preview-only relative region is invalid."""


def _shape(value: Sequence[int]) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise RelativeRegionError("image shape must contain height and width")
    height, width = value
    if (
        not isinstance(height, int)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or isinstance(width, bool)
        or height <= 0
        or width <= 0
    ):
        raise RelativeRegionError("image shape dimensions must be positive integers")
    return height, width


def _pair(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise RelativeRegionError(f"{label} must contain exactly two values")
    result = (float(value[0]), float(value[1]))
    if not all(math.isfinite(item) for item in result):
        raise RelativeRegionError(f"{label} values must be finite")
    return result


@dataclass(frozen=True, slots=True)
class RelativePreviewRegion:
    """Portable normalized overlay whose only permitted use is preview."""

    kind: str
    pos: tuple[float, float] | None = None
    size: tuple[float, float] | None = None
    points: tuple[tuple[float, float], ...] = ()
    use: str = "preview_only"
    schema: str = RELATIVE_PREVIEW_REGION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RELATIVE_PREVIEW_REGION_SCHEMA:
            raise RelativeRegionError("unsupported relative preview region schema")
        if self.use != "preview_only":
            raise RelativeRegionError("relative regions are restricted to preview_only")
        if self.kind in _BOX_KINDS:
            if self.pos is None or self.size is None or self.points:
                raise RelativeRegionError(
                    f"{self.kind} requires pos and size and must not contain points"
                )
            if self.size[0] <= 0.0 or self.size[1] <= 0.0:
                raise RelativeRegionError("relative region size must be positive")
            values = (*self.pos, *self.size)
        elif self.kind in _POLYGON_KINDS:
            if self.pos is not None or self.size is not None or len(self.points) < 3:
                raise RelativeRegionError(
                    f"{self.kind} requires at least three points and no pos/size"
                )
            values = tuple(item for point in self.points for item in point)
        else:
            raise RelativeRegionError(f"unsupported relative region kind: {self.kind!r}")
        if not all(math.isfinite(item) and 0.0 <= item <= 1.0 for item in values):
            raise RelativeRegionError("relative coordinates must be finite and within [0, 1]")


def pixel_descriptor_to_relative_preview(
    descriptor: Mapping[str, Any], image_shape: Sequence[int]
) -> RelativePreviewRegion:
    """Normalize a pixel overlay for preview on another image size."""

    if not isinstance(descriptor, Mapping):
        raise RelativeRegionError("pixel descriptor must be an object")
    height, width = _shape(image_shape)
    kind = str(descriptor.get("kind", ""))
    if kind in _BOX_KINDS:
        if set(descriptor) != {"kind", "pos", "size"}:
            raise RelativeRegionError(f"{kind} fields must be kind, pos and size")
        x, y = _pair(descriptor["pos"], "pos")
        size_x, size_y = _pair(descriptor["size"], "size")
        return RelativePreviewRegion(
            kind=kind,
            pos=(x / width, y / height),
            size=(size_x / width, size_y / height),
        )
    if kind in _POLYGON_KINDS:
        if set(descriptor) != {"kind", "points"}:
            raise RelativeRegionError(f"{kind} fields must be kind and points")
        raw_points = descriptor["points"]
        if not isinstance(raw_points, (list, tuple)):
            raise RelativeRegionError("polygon points must be a sequence")
        points = tuple(_pair(point, "polygon point") for point in raw_points)
        return RelativePreviewRegion(
            kind=kind,
            points=tuple((x / width, y / height) for x, y in points),
        )
    raise RelativeRegionError(f"unsupported relative region kind: {kind!r}")


def relative_preview_to_pixel_descriptor(
    region: RelativePreviewRegion, image_shape: Sequence[int]
) -> Mapping[str, Any]:
    """Materialize a preview overlay in the target image's pixel coordinates."""

    if not isinstance(region, RelativePreviewRegion):
        raise RelativeRegionError("region must be a RelativePreviewRegion")
    height, width = _shape(image_shape)
    if region.kind in _BOX_KINDS:
        assert region.pos is not None and region.size is not None
        return MappingProxyType(
            {
                "kind": region.kind,
                "pos": (region.pos[0] * width, region.pos[1] * height),
                "size": (region.size[0] * width, region.size[1] * height),
            }
        )
    return MappingProxyType(
        {
            "kind": region.kind,
            "points": tuple((x * width, y * height) for x, y in region.points),
        }
    )


def relative_preview_region_to_document(region: RelativePreviewRegion) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": region.schema,
        "use": region.use,
        "kind": region.kind,
    }
    if region.kind in _BOX_KINDS:
        document["pos"] = list(region.pos or ())
        document["size"] = list(region.size or ())
    else:
        document["points"] = [list(point) for point in region.points]
    return document


def relative_preview_region_from_document(document: Mapping[str, Any]) -> RelativePreviewRegion:
    if not isinstance(document, Mapping):
        raise RelativeRegionError("relative preview region document must be an object")
    kind = str(document.get("kind", ""))
    expected = (
        {"schema", "use", "kind", "pos", "size"}
        if kind in _BOX_KINDS
        else {"schema", "use", "kind", "points"}
    )
    if set(document) != expected:
        raise RelativeRegionError("relative preview region document fields are invalid")
    if kind in _BOX_KINDS:
        return RelativePreviewRegion(
            schema=str(document["schema"]),
            use=str(document["use"]),
            kind=kind,
            pos=_pair(document["pos"], "pos"),
            size=_pair(document["size"], "size"),
        )
    raw_points = document.get("points")
    if not isinstance(raw_points, (list, tuple)):
        raise RelativeRegionError("polygon points must be a sequence")
    return RelativePreviewRegion(
        schema=str(document.get("schema", "")),
        use=str(document.get("use", "")),
        kind=kind,
        points=tuple(_pair(point, "polygon point") for point in raw_points),
    )
