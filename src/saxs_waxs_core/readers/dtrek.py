"""Read the uncompressed unsigned-32 d*TREK profile without reorientation."""
from pathlib import Path
import numpy as np
from ..models import ImageFrame, ImageMetadata


def read_dtrek(path):
    from .tvx_tiff import TiffReaderError, UnsupportedTiffError
    try:
        import fabio
        image = fabio.open(str(path))
        try:
            header = dict(image.header)
            data = np.array(image.data, copy=True)
            recognized = type(image).__name__ == "DtrekImage"
        finally:
            image.close()
        if (not recognized or header.get("DIM") != "2"
                or header.get("Data_type") != "unsigned long int"
                or header.get("COMPRESSION", "None").lower() != "none"
                or data.dtype.kind != "u" or data.dtype.itemsize != 4
                or data.shape != (int(header["SIZE2"]), int(header["SIZE1"]))):
            raise UnsupportedTiffError("Unsupported d*TREK image profile")
    except TiffReaderError:
        raise
    except Exception as exc:
        raise TiffReaderError(f"Cannot decode d*TREK IMG: {exc}") from exc
    return ImageFrame(data, ImageMetadata(source_path=Path(path).absolute(),
        format_id="dtrek_uint32_v1", support_level="quantitative_candidate",
        width_px=data.shape[1], height_px=data.shape[0], dtype=str(data.dtype),
        raw_tags=header, raw_description="d*TREK; native array orientation retained"))
