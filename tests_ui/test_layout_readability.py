import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from PySide6 import QtWidgets,QtTest
from saxs_waxs_ui.app import MainWindow
from saxs_waxs_ui.widgets import CollapsibleSection


class LayoutReadabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_expanded_processing_pages_scroll_without_flattening_controls(self):
        window = MainWindow()
        window.resize(1280, 800)
        window.show()
        window.preview_panel.show()
        window.main_splitter.setSizes([180, 600, 500])
        for page in (window.geometry_panel, window.background_panel, window.mask_panel, window.integration_panel, window.recipe_panel):
            window.processing_tabs.setCurrentWidget(page)
            for section in page.findChildren(CollapsibleSection):
                section.set_expanded(True)
            # Responsive forms coalesce resize/font changes before reflowing.
            QtTest.QTest.qWait(180)
            scroll = getattr(page, "content_scroll", getattr(page, "controls_scroll", None))
            if scroll is None:
                scroll = page.findChild(QtWidgets.QScrollArea)
            self.assertIsNotNone(scroll)
            checked = 0
            for widget in scroll.widget().findChildren(QtWidgets.QWidget):
                if isinstance(widget, (QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox, QtWidgets.QPushButton)) and widget.isVisible():
                    self.assertGreaterEqual(widget.height(), widget.minimumSizeHint().height(), type(widget).__name__)
                    checked += 1
            self.assertGreater(checked, 2)
            self.assertGreaterEqual(scroll.widget().height(), scroll.widget().minimumSizeHint().height())
            if scroll.widget().minimumSizeHint().height() > scroll.viewport().height():
                self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        window.close()

    def test_fit_settings_expand_into_scroll_area_without_covering_plot(self):
        from saxs_waxs_ui.peak_fit_panel import PeakFitPanel
        panel = PeakFitPanel()
        panel.resize(900, 650)
        panel.show()
        panel.fit_settings_section.set_expanded(True)
        panel.result_section.set_expanded(True)
        panel.comparison_section.set_expanded(True)
        for _ in range(3):
            self.app.processEvents()
        for control in (panel.model_combo, panel.background_combo, panel.peak_count_spin):
            self.assertGreaterEqual(control.height(), control.minimumSizeHint().height())
        self.assertGreater(panel.fit_controls_scroll.verticalScrollBar().maximum(), 0)
        self.assertGreater(panel.workspace_splitter.height(), 200)
        panel.close()
