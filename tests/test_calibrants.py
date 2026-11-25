from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saxs_waxs_core.calibrants import (  # noqa: E402
    CalibrantLibraryError,
    load_custom_calibrant,
    load_calibrant_library,
    ring_table,
    visible_rings,
)
from saxs_waxs_core.geometry import DetectorGeometry, GeometryError  # noqa: E402


def supplied_geometry(**updates):
    values = {
        "wavelength_m": 0.124e-9,
        "detector_distance_m": 1.634,
        "pixel_size_x_m": 0.172e-3,
        "pixel_size_y_m": 0.172e-3,
        "beam_center_x_px": 381.3,
        "beam_center_y_px": 528.2,
        "image_width_px": 981,
        "image_height_px": 1043,
    }
    values.update(updates)
    return DetectorGeometry(**values)


class CalibrantLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = load_calibrant_library()

    def test_offline_library_has_source_pinned_common_calibrants(self):
        counts = {item.id: len(item.peaks) for item in self.library.calibrants}
        self.assertGreaterEqual(len(counts), 35)
        for calibrant_id in (
            "AgBh",
            "CeO2",
            "Si",
            "LaB6",
            "NaCl",
            "alpha_Al2O3",
            "Au",
            "Pt",
            "TiO2",
            "ZnO",
            "quartz",
        ):
            self.assertIn(calibrant_id, counts)
            self.assertGreater(counts[calibrant_id], 0)
        self.assertNotIn("mock", counts)
        self.assertTrue(all(len(item.source_blob_sha) == 40 for item in self.library.calibrants))

    def test_alias_resolution_is_case_insensitive(self):
        self.assertEqual(self.library.resolve("agbh").id, "AgBh")
        self.assertEqual(self.library.resolve("NIST SRM 674b").id, "CeO2")
        self.assertEqual(self.library.resolve("silicon").id, "Si")
        with self.assertRaises(CalibrantLibraryError):
            self.library.resolve("not-a-calibrant")

    def test_first_reference_spacings_and_hkl(self):
        agbh = self.library.resolve("AgBh")
        ceo2 = self.library.resolve("CeO2")
        silicon = self.library.resolve("Si")
        self.assertEqual(agbh.peaks[0].d_angstrom, 58.38)
        self.assertEqual(agbh.peaks[0].hkl, (0, 0, 1))
        self.assertEqual(ceo2.peaks[0].d_angstrom, 3.12441816)
        self.assertEqual(ceo2.peaks[0].hkl, (1, 1, 1))
        self.assertEqual(silicon.peaks[0].d_angstrom, 3.13570166)
        self.assertEqual(silicon.peaks[0].hkl, (1, 1, 1))

    def test_agbh_ring_table_matches_geometry_truth(self):
        rings = ring_table(self.library.resolve("AgBh"), supplied_geometry())
        self.assertAlmostEqual(rings[0].radius_x_px, 201.81557655958323, places=10)
        self.assertAlmostEqual(rings[0].q_nm_inv, 2 * math.pi / 5.838, places=12)
        self.assertEqual(rings[0].radius_x_px, rings[0].radius_y_px)

    def test_anisotropic_pixel_size_produces_pixel_ellipse(self):
        geometry = supplied_geometry(pixel_size_y_m=0.344e-3)
        first = ring_table(self.library.resolve("AgBh"), geometry)[0]
        self.assertAlmostEqual(first.radius_x_px / first.radius_y_px, 2.0, places=12)

    def test_visible_ring_filter_uses_detector_rectangle(self):
        geometry = supplied_geometry()
        visible = visible_rings(self.library.resolve("AgBh"), geometry)
        self.assertGreaterEqual(len(visible), 3)
        self.assertLess(len(visible), 49)
        self.assertEqual([ring.ordinal for ring in visible[:3]], [1, 2, 3])

    def test_circular_ring_preview_rejects_tilted_geometry(self):
        tilted = supplied_geometry(rot1_rad=0.01)
        with self.assertRaisesRegex(GeometryError, "requires zero tilt"):
            visible_rings(self.library.resolve("AgBh"), tilted)

    def test_custom_d_spacing_file_is_content_identified(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(dir=ROOT / "artifacts") as directory:
            source = Path(directory) / "my-standard.D"
            source.write_text(
                "# Calibrant: My standard\n5.0 # (1 0 0)\n2.5\n",
                encoding="utf-8",
            )
            calibrant = load_custom_calibrant(source)
            self.assertEqual(calibrant.name, "My standard")
            self.assertEqual([peak.d_angstrom for peak in calibrant.peaks], [5.0, 2.5])
            self.assertEqual(calibrant.peaks[0].hkl, (1, 0, 0))
            self.assertIsNone(calibrant.peaks[1].hkl)
            self.assertTrue(calibrant.id.startswith("custom:my-standard:"))

    def test_custom_d_spacing_rejects_unsorted_values(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(dir=ROOT / "artifacts") as directory:
            source = Path(directory) / "bad.D"
            source.write_text("2.0\n3.0\n", encoding="utf-8")
            with self.assertRaisesRegex(CalibrantLibraryError, "non-increasing"):
                load_custom_calibrant(source)


if __name__ == "__main__":
    unittest.main()
