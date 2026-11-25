from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

try:
    from PIL import Image, TiffTags, UnidentifiedImageError
except ModuleNotFoundError:  # isolated viewer may intentionally omit Pillow
    Image = None
    TiffTags = None
    UnidentifiedImageError = OSError

from ..models import ImageFrame, ImageMetadata
from .tiff_minimal import (
    MinimalTiffCorrupt,
    MinimalTiffUnsupported,
    decode_current_tvx_tiff,
)


class TiffReaderError(RuntimeError):
    pass


class UnsupportedTiffError(TiffReaderError):
    pass


class CorruptImageError(TiffReaderError):
    pass


def read_tvx_tiff(path,*,dataset=None,frame_index=None):
    """Backward-compatible public name for the content-driven detector reader."""
    from .detector_io import read_frame
    return read_frame(path,dataset=dataset,frame_index=frame_index)


def probe_tvx_tiff(path):
    from .detector_io import read_frame,SourceSelectionRequired
    try:
        frame=read_frame(path,frame_index=0)
        return TiffProbe(Path(path),True,frame.metadata.format_id,frame.metadata.support_level,
            'decoded detector profile; native raster and frame identity retained')
    except SourceSelectionRequired as exc:
        return TiffProbe(Path(path),True,'hdf5_detector_v1','selection_required',str(exc))
    except (OSError,TiffReaderError) as exc:
        return TiffProbe(Path(path),False,None,'corrupt_or_unknown',str(exc))


@dataclass(frozen=True, slots=True)
class TiffProbe:
    path: Path
    recognized: bool
    format_id: str | None
    support_level: str
    reason: str


_PIXEL_SIZE = re.compile(
    r"^#\s*Pixel_size\s+([0-9.eE+-]+)\s*m\s*x\s*([0-9.eE+-]+)\s*m\s*$",
    re.MULTILINE,
)
_FLOAT_FIELDS = {
    "exposure_time_s": re.compile(r"^#\s*Exposure_time\s+([0-9.eE+-]+)\s*s\s*$", re.MULTILINE),
    "exposure_period_s": re.compile(r"^#\s*Exposure_period\s+([0-9.eE+-]+)\s*s\s*$", re.MULTILINE),
}
_INTEGER_FIELDS = {
    "count_cutoff": re.compile(r"^#\s*Count_cutoff\s+(\d+)\s+counts\s*$", re.MULTILINE),
    "excluded_pixel_count": re.compile(r"^#\s*N_excluded_pixels\s*=\s*(\d+)\s*$", re.MULTILINE),
}
_EXCLUDED_FILE = re.compile(r"^#\s*Excluded_pixels:\s*(.+?)\s*$", re.MULTILINE)


def _path_for_os(path: str | os.PathLike[str]) -> tuple[Path, str]:
    source = Path(path).expanduser().absolute()
    native = str(source)
    if os.name == "nt" and len(native) >= 240 and not native.startswith("\\\\?\\"):
        if native.startswith("\\\\"):
            native = "\\\\?\\UNC\\" + native[2:]
        else:
            native = "\\\\?\\" + native
    return source, native


def _tag_value(tags: dict[str, Any], name: str, default: Any = None) -> Any:
    value = tags.get(name, default)
    if isinstance(value, tuple) and len(value) == 1:
        return value[0]
    return value


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).rstrip("\x00 ")


def _parse_description(description: str | None) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    if not description:
        return parsed
    pixel = _PIXEL_SIZE.search(description)
    if pixel:
        parsed["pixel_size_x_m"] = float(pixel.group(1))
        parsed["pixel_size_y_m"] = float(pixel.group(2))
    for key, pattern in _FLOAT_FIELDS.items():
        match = pattern.search(description)
        if match:
            parsed[key] = float(match.group(1))
    for key, pattern in _INTEGER_FIELDS.items():
        match = pattern.search(description)
        if match:
            parsed[key] = int(match.group(1))
    excluded = _EXCLUDED_FILE.search(description)
    if excluded:
        parsed["excluded_pixels_file"] = excluded.group(1).strip().rstrip("\x00")
    return parsed


def _named_tags(image: Image.Image) -> dict[str, Any]:
    return {str(TiffTags.TAGS.get(tag_id, tag_id)): value for tag_id, value in image.tag_v2.items()}


def _validate_current_tvx(image: Image.Image, tags: dict[str, Any]) -> None:
    errors: list[str] = []
    if image.format != "TIFF":
        errors.append(f"format={image.format!r}")
    if getattr(image, "n_frames", 1) != 1:
        errors.append(f"n_frames={getattr(image, 'n_frames', None)!r}")
    if int(_tag_value(tags, "BitsPerSample", -1)) != 32:
        errors.append(f"BitsPerSample={tags.get('BitsPerSample')!r}")
    if int(_tag_value(tags, "SampleFormat", -1)) != 2:
        errors.append(f"SampleFormat={tags.get('SampleFormat')!r}")
    if int(_tag_value(tags, "Compression", -1)) != 1:
        errors.append(f"Compression={tags.get('Compression')!r}")
    if image.mode != "I":
        errors.append(f"mode={image.mode!r}")
    software = _clean_text(tags.get("Software")) or ""
    if not software.startswith("TVX TIFF"):
        errors.append(f"Software={software!r}")
    if errors:
        raise UnsupportedTiffError("Not the validated TVX signed-int32 TIFF profile: " + ", ".join(errors))


def _probe_current_tvx_tiff(path: str | os.PathLike[str]) -> TiffProbe:
    source, native = _path_for_os(path)
    if Image is None:
        try:
            decode_current_tvx_tiff(native)
        except FileNotFoundError as exc:
            return TiffProbe(source, False, None, "unreadable", f"{type(exc).__name__}: {exc}")
        except MinimalTiffUnsupported as exc:
            return TiffProbe(source, True, "tiff", "unsupported_profile", str(exc))
        except MinimalTiffCorrupt as exc:
            return TiffProbe(source, False, None, "corrupt_or_unknown", str(exc))
        return TiffProbe(
            source,
            True,
            "tvx_tiff_signed_int32_v1",
            "quantitative_candidate",
            "validated profile match (minimal decoder)",
        )
    try:
        with Image.open(native) as image:
            tags = _named_tags(image)
            _validate_current_tvx(image, tags)
    except (FileNotFoundError, PermissionError) as exc:
        return TiffProbe(source, False, None, "unreadable", f"{type(exc).__name__}: {exc}")
    except (UnidentifiedImageError, OSError) as exc:
        return TiffProbe(source, False, None, "corrupt_or_unknown", f"{type(exc).__name__}: {exc}")
    except UnsupportedTiffError as exc:
        return TiffProbe(source, True, "tiff", "unsupported_profile", str(exc))
    return TiffProbe(source, True, "tvx_tiff_signed_int32_v1", "quantitative_candidate", "validated profile match")


def _read_current_tvx_tiff(path: str | os.PathLike[str]) -> ImageFrame:
    """Read the current project's validated TVX TIFF profile without reorientation."""

    source, native = _path_for_os(path)
    if Image is None:
        try:
            decoded = decode_current_tvx_tiff(native)
        except MinimalTiffUnsupported as exc:
            raise UnsupportedTiffError(str(exc)) from exc
        except MinimalTiffCorrupt as exc:
            raise CorruptImageError(f"Cannot decode {source}: {exc}") from exc
        tags = decoded.tags
        description = _clean_text(tags.get("ImageDescription"))
        parsed = _parse_description(description)
        metadata = ImageMetadata(
            source_path=source,
            format_id="tvx_tiff_signed_int32_v1",
            support_level="quantitative_candidate",
            width_px=decoded.width,
            height_px=decoded.height,
            dtype=str(decoded.data.dtype),
            detector_model=_clean_text(tags.get("Model")),
            software=_clean_text(tags.get("Software")),
            pixel_size_x_m=parsed.get("pixel_size_x_m"),
            pixel_size_y_m=parsed.get("pixel_size_y_m"),
            exposure_time_s=parsed.get("exposure_time_s"),
            exposure_period_s=parsed.get("exposure_period_s"),
            count_cutoff=parsed.get("count_cutoff"),
            excluded_pixel_count=parsed.get("excluded_pixel_count"),
            excluded_pixels_file=parsed.get("excluded_pixels_file"),
            raw_description=description,
            raw_tags=tags,
        )
        return ImageFrame(data=decoded.data, metadata=metadata)
    try:
        with Image.open(native) as image:
            tags = _named_tags(image)
            _validate_current_tvx(image, tags)
            description = _clean_text(tags.get("ImageDescription"))
            parsed = _parse_description(description)
            data = np.array(image, dtype=np.int32, copy=True)
            width, height = image.size
    except UnsupportedTiffError:
        raise
    except (FileNotFoundError, PermissionError) as exc:
        raise TiffReaderError(f"Cannot read {source}: {exc}") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise CorruptImageError(f"Cannot decode {source}: {exc}") from exc

    if data.shape != (height, width):
        raise CorruptImageError(
            f"Decoded array shape {data.shape} does not match TIFF size {(height, width)}"
        )
    metadata = ImageMetadata(
        source_path=source,
        format_id="tvx_tiff_signed_int32_v1",
        support_level="quantitative_candidate",
        width_px=width,
        height_px=height,
        dtype=str(data.dtype),
        detector_model=_clean_text(tags.get("Model")),
        software=_clean_text(tags.get("Software")),
        pixel_size_x_m=parsed.get("pixel_size_x_m"),
        pixel_size_y_m=parsed.get("pixel_size_y_m"),
        exposure_time_s=parsed.get("exposure_time_s"),
        exposure_period_s=parsed.get("exposure_period_s"),
        count_cutoff=parsed.get("count_cutoff"),
        excluded_pixel_count=parsed.get("excluded_pixel_count"),
        excluded_pixels_file=parsed.get("excluded_pixels_file"),
        raw_description=description,
        raw_tags=tags,
    )
    return ImageFrame(data=data, metadata=metadata)
