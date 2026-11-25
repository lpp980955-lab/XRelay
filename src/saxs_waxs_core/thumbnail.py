from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import matplotlib
import numpy as np
from PIL import Image

from .display import DisplayPreset, normalize_for_display
from .models import ImageFrame
from .colormap_data import STATIC_PALETTES, load_colorcet_rgb


THUMBNAIL_SCHEMA = "thumbnail_cache/1"
RECOMMENDED_COLORMAPS = (
    "gray",
    "viridis",
    "plasma",
    "inferno",
    "magma",
    "cividis",
    "turbo",
    "jet",
    "hot",
    "coolwarm",
    "RdBu",
)


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    path: Path
    cache_key: str
    cache_hit: bool
    width_px: int
    height_px: int


def validate_colormap(name: str) -> None:
    if name == "custom" or name in STATIC_PALETTES:
        return
    try:
        matplotlib.colormaps[name]
    except KeyError as exc:
        if name.startswith("CET-"):
            try:
                load_colorcet_rgb(name)
                return
            except ValueError as resource_exc:
                raise ValueError(f"ColorCET colormap is unavailable: {name}") from resource_exc
        raise ValueError(f"Unknown colormap: {name}") from exc


def _matplotlib_colormap(preset: DisplayPreset):
    from matplotlib.colors import LinearSegmentedColormap

    if preset.colormap == "custom":
        colors = preset.custom_colors
    elif preset.colormap in STATIC_PALETTES:
        colors = STATIC_PALETTES[preset.colormap]
    elif preset.colormap.startswith("CET-"):
        colors = load_colorcet_rgb(preset.colormap)
    else:
        return matplotlib.colormaps[preset.effective_colormap]
    if preset.invert_colormap:
        colors = tuple(reversed(colors))
    return LinearSegmentedColormap.from_list(f"saxs_waxs_{preset.colormap}", colors)


def _source_identity(path: Path) -> dict[str, object]:
    absolute = path.expanduser().absolute()
    native = str(absolute)
    if os.name == "nt" and len(native) >= 240 and not native.startswith("\\\\?\\"):
        native = "\\\\?\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\\\?\\" + native
    stat = os.stat(native)
    return {
        "path": str(absolute),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def thumbnail_cache_key(
    source_path: Path,
    preset: DisplayPreset,
    maximum_size: tuple[int, int],
) -> str:
    payload = {
        "schema": THUMBNAIL_SCHEMA,
        "source": _source_identity(source_path),
        "preset": asdict(preset),
        "maximum_size": list(maximum_size),
        "renderer": "matplotlib-colormap+pillow-lanczos",
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def render_thumbnail(
    frame: ImageFrame,
    preset: DisplayPreset,
    maximum_size: tuple[int, int] = (320, 320),
    mask: np.ndarray | None = None,
) -> Image.Image:
    if maximum_size[0] <= 0 or maximum_size[1] <= 0:
        raise ValueError("Thumbnail maximum dimensions must be positive")
    validate_colormap(preset.effective_colormap)
    normalized = normalize_for_display(frame.data, preset, mask)
    values = np.nan_to_num(normalized.values, nan=0.0, posinf=1.0, neginf=0.0)
    color_map = _matplotlib_colormap(preset)
    rgba = color_map(values, bytes=True)
    rgba[..., 3] = np.where(normalized.valid, 255, 0).astype(np.uint8)
    image = Image.fromarray(rgba, mode="RGBA")
    image.thumbnail(maximum_size, Image.Resampling.LANCZOS)
    return image


class ThumbnailCache:
    def __init__(self, cache_directory: str | os.PathLike[str]):
        self.cache_directory = Path(cache_directory).expanduser().absolute()

    def get_or_create(
        self,
        frame: ImageFrame,
        preset: DisplayPreset,
        maximum_size: tuple[int, int] = (320, 320),
        mask: np.ndarray | None = None,
    ) -> ThumbnailResult:
        key = thumbnail_cache_key(frame.metadata.source_path, preset, maximum_size)
        destination = self.cache_directory / key[:2] / f"{key}.png"
        if destination.is_file():
            with Image.open(destination) as existing:
                width, height = existing.size
            return ThumbnailResult(destination, key, True, width, height)

        destination.parent.mkdir(parents=True, exist_ok=True)
        image = render_thumbnail(frame, preset, maximum_size, mask)
        temporary = destination.with_suffix(".tmp.png")
        image.save(temporary, format="PNG", optimize=False)
        os.replace(temporary, destination)
        return ThumbnailResult(destination, key, False, image.width, image.height)

