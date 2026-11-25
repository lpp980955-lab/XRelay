from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from saxs_waxs_ui.image_export_dialog import ImageExportDialog  # noqa: E402


class ImageExportDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    def test_compact_settings_create_expected_preset(self):
        dialog = ImageExportDialog(batch_available=True)
        dialog.scope_combo.setCurrentIndex(1)
        dialog.batch_source_combo.setCurrentIndex(1)
        dialog.format_combo.setCurrentIndex(1)
        dialog.range_combo.setCurrentIndex(1)
        dialog.axis_mode_combo.setCurrentIndex(1)
        dialog.width_spin.setValue(90.0)
        dialog.height_spin.setValue(60.0)
        dialog.dpi_spin.setValue(300)
        dialog.colorbar_check.setChecked(False)
        self.assertFalse(dialog.advanced_widget.isVisible())
        dialog.advanced_button.setChecked(True)
        dialog.font_combo.setCurrentText("DejaVu Serif")
        dialog.label_font_spin.setValue(11.5)
        dialog.tick_font_spin.setValue(7.5)
        dialog.line_width_spin.setValue(1.1)
        preset = dialog.export_preset()
        self.assertEqual(dialog.scope, "selected")
        self.assertEqual(dialog.batch_source, "processed_recipe")
        self.assertEqual(preset.detector_image_format, "tiff")
        self.assertEqual(preset.detector_range_mode, "shared_manual")
        self.assertEqual(preset.detector_axis_mode, "detector_mm")
        self.assertEqual((preset.image_width_mm, preset.image_height_mm), (90.0, 60.0))
        self.assertEqual(preset.image_dpi, 300)
        self.assertFalse(preset.detector_include_colorbar)
        self.assertEqual(preset.detector_font_family, "DejaVu Serif")
        self.assertEqual(preset.detector_label_font_size_pt, 11.5)
        self.assertEqual(preset.detector_tick_font_size_pt, 7.5)
        self.assertEqual(preset.detector_line_width_pt, 1.1)
        dialog.close()

    def test_export_content_is_explicit_and_raw_is_default(self):
        dialog = ImageExportDialog(batch_available=True)
        self.assertEqual(dialog.scope, "current")
        self.assertEqual(dialog.batch_source, "raw_array")
        self.assertIn("跳过全部处理", dialog.batch_source_combo.currentText())
        self.assertGreaterEqual(
            dialog.batch_source_combo.findData("current_view"), 0
        )
        dialog.scope_combo.setCurrentIndex(1)
        self.assertEqual(dialog.batch_source, "raw_array")
        self.assertGreaterEqual(
            dialog.batch_source_combo.findData("processed_recipe"), 0
        )
        self.assertEqual(dialog.batch_source_combo.findData("current_view"), -1)
        dialog.close()

    def test_new_export_defaults_to_preview_matching_not_stale_fixed_limits(self):
        from saxs_waxs_core.presets import ExportPreset

        dialog = ImageExportDialog(
            batch_available=True,
            initial_preset=ExportPreset(detector_range_mode="shared_manual"),
        )
        self.assertEqual(
            dialog.range_combo.currentData(),
            "independent",
        )
        self.assertIn("当前预览规则", dialog.range_combo.currentText())
        dialog.close()

    def test_batch_scope_is_disabled_without_selection(self):
        dialog = ImageExportDialog(batch_available=False)
        item = dialog.scope_combo.model().item(1)
        self.assertIsNotNone(item)
        self.assertFalse(item.isEnabled())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
