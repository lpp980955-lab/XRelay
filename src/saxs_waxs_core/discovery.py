from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_IMAGE_SUFFIXES = frozenset({".img", ".tif", ".tiff", ".gfrm", ".sfrm", ".cbf", ".edf", ".h5", ".hdf5", ".nxs", ".nx5"})


def has_detector_signature(path):
    """Cheap hints for detector counter filenames; decoding is still mandatory."""
    try:
        with Path(path).open('rb') as stream:head=stream.read(256)
    except OSError:
        return False
    if head.startswith((b'II*\0',b'MM\0*',b'II+\0',b'MM\0+',b'\x89HDF\r\n\x1a\n',b'###CBF:')):
        return True
    if head.startswith(b'FORMAT') and b':' in head[:9]:return True
    stripped=head.lstrip()
    return stripped.startswith(b'{') and (b'HEADER_BYTES=' in head or any(
        key in head for key in (b'HeaderID',b'EDF_',b'Dim_1',b'ByteOrder')))


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    path: Path
    relative_path: Path
    suffix: str
    size_bytes: int


def _path_for_os(path: str | os.PathLike[str]) -> tuple[Path, str]:
    source = Path(path).expanduser().absolute()
    native = str(source)
    if os.name == "nt" and len(native) >= 240 and not native.startswith("\\\\?\\"):
        if native.startswith("\\\\"):
            native = "\\\\?\\UNC\\" + native[2:]
        else:
            native = "\\\\?\\" + native
    return source, native


def discover_image_files(
    root: str | os.PathLike[str],
    *,
    recursive: bool = True,
    suffixes: Iterable[str] = DEFAULT_IMAGE_SUFFIXES,
) -> tuple[DiscoveredFile, ...]:
    """Discover candidate detector files without opening or modifying them."""

    source_root, native_root = _path_for_os(root)
    normalized_suffixes = {
        suffix.casefold() if suffix.startswith(".") else "." + suffix.casefold()
        for suffix in suffixes
    }
    if not os.path.isdir(native_root):
        raise NotADirectoryError(source_root)

    found: list[DiscoveredFile] = []
    if recursive:
        iterator = os.walk(native_root)
    else:
        iterator = [(native_root, [], os.listdir(native_root))]

    for native_directory, _directories, filenames in iterator:
        for filename in filenames:
            native_path = os.path.join(native_directory, filename)
            if not os.path.isfile(native_path):
                continue
            suffix = Path(filename).suffix.casefold()
            compressed_detector=(suffix in {'.gz','.bz2'} and Path(Path(filename).stem).suffix.casefold()
                in normalized_suffixes-{'.h5','.hdf5','.nxs','.nx5'})
            content_candidate=frozenset(normalized_suffixes)==DEFAULT_IMAGE_SUFFIXES and has_detector_signature(native_path)
            if suffix not in normalized_suffixes and not compressed_detector and not content_candidate:
                continue
            relative = Path(os.path.relpath(native_path, native_root))
            display_path = source_root / relative
            found.append(
                DiscoveredFile(
                    path=display_path,
                    relative_path=relative,
                    suffix=suffix,
                    size_bytes=os.path.getsize(native_path),
                )
            )
    found.sort(key=lambda item: item.relative_path.as_posix().casefold())
    return tuple(found)

