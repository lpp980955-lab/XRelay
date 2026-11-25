import struct
import tempfile
import unittest
from pathlib import Path
import numpy as np
from saxs_waxs_core.readers.tvx_tiff import read_tvx_tiff, probe_tvx_tiff, TiffReaderError


class RigakuTiffTests(unittest.TestCase):
    def test_unsigned_pixels_and_orientation_without_rows_per_strip_tag(self):
        data = np.array([[0, 4, 2**31+1], [7, 8, 2**32-1]], dtype='<u4')
        pixels = data.tobytes()
        offset = 8+len(pixels)
        description = b'Rigaku 2D image\x00'
        entries = [(256,3,1,3), (257,3,1,2), (258,3,1,32), (259,3,1,1),
                   (262,3,1,0), (270,2,len(description),offset+2+9*12+4),
                   (273,4,1,8), (277,3,1,1), (279,4,1,len(pixels))]
        payload = b'II*\x00'+struct.pack('<I',offset)+pixels+struct.pack('<H',len(entries))
        payload += b''.join(struct.pack('<HHII',*entry) for entry in entries)+b'\x00'*4+description
        root = Path(__file__).resolve().parents[1]/'artifacts'/'test_tmp'
        root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            path = Path(tmp)/'中文 样品.tif'
            path.write_bytes(payload)
            frame = read_tvx_tiff(path)
            np.testing.assert_array_equal(frame.data,data)
            self.assertEqual(frame.metadata.format_id,'rigaku_tiff_uint32_v1')
            self.assertEqual(probe_tvx_tiff(path).format_id,'rigaku_tiff_uint32_v1')
            self.assertFalse(frame.data.flags.writeable)
            self.assertIsNone(frame.metadata.exposure_time_s)
            invalid = Path(tmp)/'unknown.tif'
            invalid.write_bytes(b'not an image')
            with self.assertRaises(TiffReaderError):
                read_tvx_tiff(invalid)
