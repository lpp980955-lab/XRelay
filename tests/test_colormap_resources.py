from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saxs_waxs_core.colormap_data import COLORCET_RESOURCE_DIR, load_colorcet_rgb  # noqa: E402


class ColormapResourceTests(unittest.TestCase):
    def test_selected_colorcet_files_match_audited_manifest(self):
        manifest = json.loads((COLORCET_RESOURCE_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["license"], "CC-BY-4.0")
        self.assertEqual(
            manifest["source_wheel_sha256"],
            "7abb7c3e17362add64f8711b474dffac5e7b0e9245abdf992e9a44119b7aa4f5",
        )
        for name, evidence in manifest["files"].items():
            with self.subTest(name=name):
                payload = (COLORCET_RESOURCE_DIR / name).read_bytes()
                self.assertEqual(len(payload), evidence["bytes"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), evidence["sha256"])
        for name in ("CET-L3", "CET-D1", "CET-C1"):
            table = load_colorcet_rgb(name)
            self.assertEqual(len(table), 256)
            self.assertTrue(all(len(row) == 3 for row in table))


if __name__ == "__main__":
    unittest.main()
