from __future__ import annotations

import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saxs_waxs_core.background import BackgroundRecipe
from saxs_waxs_core.geometry import DetectorGeometry
from saxs_waxs_core.integration import RadialIntegrationRecipe
from saxs_waxs_core.masking import MaskRecipe
from saxs_waxs_core.processing import ProcessingError, ProcessingRecipe
from saxs_waxs_core.relative_regions import (
    RELATIVE_PREVIEW_REGION_SCHEMA,
    RelativePreviewRegion,
    RelativeRegionError,
    pixel_descriptor_to_relative_preview,
    relative_preview_region_from_document,
    relative_preview_region_to_document,
    relative_preview_to_pixel_descriptor,
)


class RelativePreviewRegionTests(unittest.TestCase):
    def test_box_roundtrip_and_cross_shape_scaling(self):
        source = {"kind": "rectangle", "pos": (20.0, 10.0), "size": (40.0, 30.0)}
        region = pixel_descriptor_to_relative_preview(source, (100, 200))
        self.assertEqual(region.pos, (0.1, 0.1))
        self.assertEqual(region.size, (0.2, 0.3))
        self.assertEqual(dict(relative_preview_to_pixel_descriptor(region, (200, 400))), {
            "kind": "rectangle",
            "pos": (40.0, 20.0),
            "size": (80.0, 60.0),
        })

    def test_polygon_document_roundtrip_is_strict(self):
        source = {
            "kind": "occlusion_polygon",
            "points": ((0.0, 0.0), (50.0, 25.0), (100.0, 50.0)),
        }
        region = pixel_descriptor_to_relative_preview(source, (50, 100))
        document = relative_preview_region_to_document(region)
        self.assertEqual(document["schema"], RELATIVE_PREVIEW_REGION_SCHEMA)
        self.assertEqual(document["use"], "preview_only")
        self.assertEqual(relative_preview_region_from_document(document), region)
        with self.assertRaises(RelativeRegionError):
            relative_preview_region_from_document({**document, "unexpected": True})

    def test_quantitative_or_out_of_bounds_relative_region_is_rejected(self):
        with self.assertRaisesRegex(RelativeRegionError, "preview_only"):
            RelativePreviewRegion(
                kind="circle", pos=(0.1, 0.1), size=(0.2, 0.2), use="quantitative"
            )
        with self.assertRaisesRegex(RelativeRegionError, "within"):
            RelativePreviewRegion(
                kind="circle", pos=(1.1, 0.1), size=(0.2, 0.2)
            )

    def test_relative_document_cannot_be_silently_used_as_processing_mask(self):
        relative_document = relative_preview_region_to_document(
            RelativePreviewRegion(kind="circle", pos=(0.1, 0.1), size=(0.2, 0.2))
        )
        with self.assertRaises(ProcessingError):
            ProcessingRecipe(
                geometry=DetectorGeometry(
                    wavelength_m=0.124e-9,
                    detector_distance_m=1.0,
                    pixel_size_x_m=1e-4,
                    pixel_size_y_m=1e-4,
                    beam_center_x_px=50.0,
                    beam_center_y_px=50.0,
                    image_width_px=100,
                    image_height_px=100,
                ),
                background=BackgroundRecipe(),
                mask=MaskRecipe(),
                integration=RadialIntegrationRecipe(npt=16),
                manual_mask_descriptors=(relative_document,),
            )


if __name__ == "__main__":
    unittest.main()
