"""Dependency-free decoder for the project's validated TVX TIFF profile.

This is deliberately not a general TIFF reader.  It accepts only a single-IFD,
strip-based, uncompressed, single-channel signed-int32 image whose Software tag
starts with ``TVX TIFF``.  Other profiles must be handled by a separately
validated reader instead of silently passing through this fallback.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np


class MinimalTiffError(ValueError):
    """Base error for the deliberately restricted TIFF decoder."""


class MinimalTiffUnsupported(MinimalTiffError):
    """The file is TIFF, but not the validated TVX signed-int32 profile."""


class MinimalTiffCorrupt(MinimalTiffError):
    """The file is malformed or truncated."""


@dataclass(frozen=True, slots=True)
class MinimalTiffFrame:
    data: np.ndarray
    width: int
    height: int
    tags: dict[str, Any]


_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4}
_TYPE_CODE = {1: "B", 3: "H", 4: "I"}
_TAG_NAMES = {
    256: "ImageWidth",
    257: "ImageLength",
    258: "BitsPerSample",
    259: "Compression",
    262: "PhotometricInterpretation",
    270: "ImageDescription",
    272: "Model",
    273: "StripOffsets",
    277: "SamplesPerPixel",
    278: "RowsPerStrip",
    279: "StripByteCounts",
    305: "Software",
    306: "DateTime",
    315: "Artist",
    339: "SampleFormat",
}


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    payload = stream.read(count)
    if len(payload) != count:
        raise MinimalTiffCorrupt(f"Truncated TIFF: expected {count} bytes, found {len(payload)}")
    return payload


def _payload(
    stream: BinaryIO,
    *,
    endian: str,
    field_type: int,
    count: int,
    value_or_offset: bytes,
) -> bytes:
    size = _TYPE_SIZE.get(field_type)
    if size is None:
        raise MinimalTiffUnsupported(f"Unsupported TIFF field type {field_type}")
    byte_count = size * count
    if byte_count <= 4:
        return value_or_offset[:byte_count]
    offset = struct.unpack(endian + "I", value_or_offset)[0]
    position = stream.tell()
    stream.seek(offset)
    result = _read_exact(stream, byte_count)
    stream.seek(position)
    return result


def _decode_value(endian: str, field_type: int, count: int, payload: bytes) -> Any:
    if field_type == 2:
        return payload.decode("latin-1").rstrip("\x00")
    code = _TYPE_CODE.get(field_type)
    if code is None:
        raise MinimalTiffUnsupported(f"Unsupported TIFF field type {field_type}")
    values = struct.unpack(endian + str(count) + code, payload)
    return values[0] if count == 1 else values


def _as_tuple(value: Any) -> tuple[int, ...]:
    if isinstance(value, tuple):
        return tuple(int(item) for item in value)
    return (int(value),)


def decode_current_tvx_tiff(path: str | os.PathLike[str]) -> MinimalTiffFrame:
    """Decode only the current validated TVX TIFF profile without reorientation."""

    source = Path(path)
    try:
        with open(path, "rb") as stream:
            byte_order = _read_exact(stream, 2)
            if byte_order == b"II":
                endian, numpy_dtype = "<", "<i4"
            elif byte_order == b"MM":
                endian, numpy_dtype = ">", ">i4"
            else:
                raise MinimalTiffCorrupt("Not a TIFF byte-order marker")
            if struct.unpack(endian + "H", _read_exact(stream, 2))[0] != 42:
                raise MinimalTiffCorrupt("Invalid classic TIFF magic")
            ifd_offset = struct.unpack(endian + "I", _read_exact(stream, 4))[0]
            stream.seek(ifd_offset)
            entry_count = struct.unpack(endian + "H", _read_exact(stream, 2))[0]
            entries: dict[int, Any] = {}
            for _ in range(entry_count):
                raw = _read_exact(stream, 12)
                tag, field_type, count = struct.unpack(endian + "HHI", raw[:8])
                if tag not in _TAG_NAMES:
                    continue
                payload = _payload(
                    stream,
                    endian=endian,
                    field_type=field_type,
                    count=count,
                    value_or_offset=raw[8:12],
                )
                entries[tag] = _decode_value(endian, field_type, count, payload)
            next_ifd = struct.unpack(endian + "I", _read_exact(stream, 4))[0]
            if next_ifd != 0:
                raise MinimalTiffUnsupported("Only single-IFD TIFF is supported")

            def required(tag: int) -> Any:
                if tag not in entries:
                    raise MinimalTiffCorrupt(f"Missing required TIFF tag {tag}")
                return entries[tag]

            width = int(required(256))
            height = int(required(257))
            bits = int(_as_tuple(required(258))[0])
            compression = int(required(259))
            samples = int(entries.get(277, 1))
            sample_format = int(_as_tuple(required(339))[0])
            software = str(required(305)).rstrip("\x00 ")
            if not software.startswith("TVX TIFF"):
                raise MinimalTiffUnsupported(f"Software={software!r} is not TVX TIFF")
            if not (bits == 32 and compression == 1 and samples == 1 and sample_format == 2):
                raise MinimalTiffUnsupported(
                    "Expected uncompressed single-channel signed-int32 TIFF; "
                    f"got bits={bits}, compression={compression}, samples={samples}, "
                    f"sample_format={sample_format}"
                )
            offsets = _as_tuple(required(273))
            byte_counts = _as_tuple(required(279))
            if len(offsets) != len(byte_counts):
                raise MinimalTiffCorrupt("StripOffsets and StripByteCounts length mismatch")
            strips: list[bytes] = []
            for offset, byte_count in zip(offsets, byte_counts):
                stream.seek(offset)
                strips.append(_read_exact(stream, byte_count))
    except (OSError, OverflowError, struct.error) as exc:
        raise MinimalTiffCorrupt(f"Cannot decode {source}: {exc}") from exc

    raw_pixels = b"".join(strips)
    expected_bytes = width * height * 4
    if len(raw_pixels) != expected_bytes:
        raise MinimalTiffCorrupt(
            f"Expected {expected_bytes} pixel bytes, found {len(raw_pixels)}"
        )
    data = np.frombuffer(raw_pixels, dtype=numpy_dtype).reshape(height, width)
    data = data.astype(np.int32, copy=False)
    data.setflags(write=False)
    named_tags = {_TAG_NAMES[tag]: value for tag, value in entries.items()}
    return MinimalTiffFrame(data=data, width=width, height=height, tags=named_tags)
