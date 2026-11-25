"""Excluded detector sentinels must not control preview colors."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
import numpy as np
from saxs_waxs_ui.app import ImageViewerWidget,create_application
from saxs_waxs_core.display import DisplayPreset

class MaskDisplayLimitsTests(unittest.TestCase):
    def test_mask_excludes_high_sentinels_without_mutating_source_or_view(self):
        app=create_application([])
        viewer=ImageViewerWidget()
        values=np.arange(400,dtype=np.float64).reshape(20,20)+1
        values[:5]=4294967295
        original=values.copy()
        viewer.set_data(values)
        viewer.set_preset(DisplayPreset(scale='log',upper_percentile=99.5))
        before=viewer._normalized.vmax
        view=viewer.image_view.getView().viewRange()
        mask=values>65000
        viewer.set_display_mask(mask)
        self.assertGreater(before,1e9)
        self.assertLess(viewer._normalized.vmax,500)
        self.assertTrue(np.isnan(viewer._normalized.values[mask]).all())
        np.testing.assert_array_equal(values,original)
        self.assertEqual(viewer.image_view.getView().viewRange(),view)
        with self.assertRaises(ValueError):viewer.set_display_mask(np.zeros((2,2),bool))
        viewer.clear_mask_overlay()
        self.assertIsNone(viewer.mask)
        self.assertGreater(viewer._normalized.vmax,1e9)
        viewer.set_display_mask(mask)
        viewer.set_data(values)
        self.assertGreater(viewer._normalized.vmax,1e9)
        viewer.close();app.processEvents()

if __name__=='__main__':unittest.main()
