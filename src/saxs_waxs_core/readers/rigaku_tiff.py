"""Narrow Rigaku unsigned-32 TIFF profile, decoded by installed FabIO."""
from pathlib import Path
import numpy as np
from ..models import ImageFrame, ImageMetadata


def read_rigaku_tiff(path):
    from .tvx_tiff import UnsupportedTiffError, CorruptImageError
    try:
        import fabio
    except ImportError as exc:
        raise UnsupportedTiffError("Rigaku TIFF requires the configured FabIO dependency") from exc
    try:
        image = fabio.open(str(path))
        try:
            if image.nframes!=1:
                raise UnsupportedTiffError('Rigaku multi-page TIFF uses the generic frame-aware adapter')
            tags = dict(image.header)
            data = np.array(image.data, copy=True)
        finally:
            image.close()
    except UnsupportedTiffError:
        raise
    except Exception as exc:
        raise CorruptImageError(f"Rigaku TIFF decode failed: {exc}") from exc
    if (tags.get("imageDescription", "").strip("\x00 ") != "Rigaku 2D image"
            or tags.get("nBits") != 32 or tags.get("compression_type") != 1
            or data.dtype.kind != "u" or data.dtype.itemsize != 4 or data.ndim != 2):
        raise UnsupportedTiffError("Not the supported uncompressed Rigaku uint32 TIFF profile")
    height, width = data.shape
    if (height, width) != (tags.get("nRows"), tags.get("nColumns")):
        raise CorruptImageError("Rigaku image shape differs from header")
    return ImageFrame(data=data, metadata=ImageMetadata(
        source_path=Path(path).absolute(), format_id="rigaku_tiff_uint32_v1",
        support_level="quantitative_candidate", width_px=width, height_px=height,
        dtype=str(data.dtype), software=tags.get("software"),
        raw_description=tags["imageDescription"], raw_tags=tags))
