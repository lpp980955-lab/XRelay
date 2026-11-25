import tempfile
import unittest
from pathlib import Path

import numpy as np

from saxs_waxs_core.readers.tvx_tiff import read_tvx_tiff, probe_tvx_tiff


class DtrekTests(unittest.TestCase):
    def test_content_detection_retains_native_unsigned_pixels(self):
        data = np.array([[0, 2**31+3, 8], [9, 10, 2**32-1]], dtype='<u4')
        header = ("{\nHEADER_BYTES=4096;\nDIM=2;\nBYTE_ORDER=little_endian;\n"
                  "SIZE1=3;\nSIZE2=2;\nData_type=unsigned long int;\nCOMPRESSION=None;\n}\n")
        payload = header.encode().ljust(4096, b' ') + data.tobytes()
        root = Path(__file__).resolve().parents[1]/'artifacts'/'test_tmp'
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            path = Path(tmp)/'不同名字.img'
            path.write_bytes(payload)
            result = read_tvx_tiff(path)
            np.testing.assert_array_equal(result.data, data)
            self.assertFalse(result.data.flags.writeable)
            self.assertIsNone(result.metadata.exposure_time_s)
            self.assertEqual(probe_tvx_tiff(path).format_id, 'dtrek_uint32_v1')
            self.assertEqual(path.read_bytes(), payload)
