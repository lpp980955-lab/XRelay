import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from PySide6 import QtCore,QtWidgets
from saxs_waxs_ui.app import MainWindow
from saxs_waxs_ui.widgets import CollapsibleSection
from saxs_waxs_ui.adaptive_layout import ResponsiveScrollArea,recommended_sizes
from saxs_waxs_ui import runtime_diagnostics as diagnostics


class AdaptiveLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.w=MainWindow(settings=QtCore.QSettings(str(self.root/'settings.ini'),QtCore.QSettings.Format.IniFormat))

    def settle(self):
        end=time.monotonic()+.3
        while time.monotonic()<end:self.app.processEvents();time.sleep(.005)

    def tearDown(self):
        self.w.close();self.w.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete);self.app.processEvents();self.tmp.cleanup()

    def test_space_allocation_is_logical_and_center_is_preserved(self):
        for width in (720,960,1280,1920,2560):
            sizes=recommended_sizes(width,12)
            self.assertEqual(sum(sizes),width)
            self.assertGreaterEqual(sizes[1],width*.44)
            self.assertGreater(sizes[0],0);self.assertGreater(sizes[2],0)

    def test_all_processing_pages_reflow_and_controls_are_scroll_reachable(self):
        self.w.resize(1024,650);self.w.show();self.w._set_preview_visible(True);self.settle()
        self.assertGreaterEqual(self.w.main_splitter.sizes()[1],440)
        for page in (self.w.geometry_panel,self.w.background_panel,self.w.mask_panel,self.w.integration_panel,self.w.recipe_panel):
            self.w.processing_tabs.setCurrentWidget(page)
            for section in page.findChildren(CollapsibleSection):
                if section is not self.w.integration_panel.plot_options_section:section.set_expanded(True)
            self.settle()
            scrolls=[s for s in page.findChildren(ResponsiveScrollArea) if s.isVisible()]
            self.assertTrue(scrolls)
            for scroll in scrolls:
                self.assertTrue(scroll.compact)
                self.assertGreater(scroll.viewport().width(),200)
                for field in scroll.widget().findChildren(QtWidgets.QAbstractSpinBox):
                    if not field.isVisible():continue
                    self.assertGreaterEqual(field.height(),field.minimumSizeHint().height())
                    scroll.ensureWidgetVisible(field);self.app.processEvents()
                    rect=field.mapTo(scroll.viewport(),QtCore.QPoint(0,0))
                    self.assertLess(rect.y(),scroll.viewport().height())
                    self.assertGreater(rect.y()+field.height(),0)

    def test_dragging_is_respected_then_reset_restores_auto(self):
        self.w.resize(1280,800);self.w.show();self.w._set_preview_visible(True);self.settle()
        self.w.main_splitter.moveSplitter(110,1);self.settle()
        self.assertFalse(self.w.adaptive_layout.automatic)
        self.assertFalse(self.w.adaptive_layout.action.isChecked())
        before=self.w.main_splitter.sizes();self.w.adaptive_layout.apply()
        self.assertEqual(self.w.main_splitter.sizes(),before)
        self.w.adaptive_layout.reset_action.trigger();self.settle()
        self.assertTrue(self.w.adaptive_layout.automatic)
        self.assertTrue(self.w.adaptive_layout.action.isChecked())

    def test_screen_change_fits_window_without_changing_parameters(self):
        self.w.show();self.settle()
        settings=self.w.integration_panel.radial_npt.value()
        self.w.resize(2200,1600);self.w.adaptive_layout.display_changed();self.settle()
        area=self.w.screen().availableGeometry()
        self.assertLessEqual(self.w.width(),area.width())
        self.assertLessEqual(self.w.height(),area.height())
        self.assertEqual(self.w.integration_panel.radial_npt.value(),settings)

    def test_diagnostic_archive_contains_only_technical_display_context(self):
        self.w.resize(1024,650);self.w.show();self.w._set_preview_visible(True);self.settle()
        self.w.setWindowTitle('PRIVATE_EXPERIMENT_NAME')
        self.w.adaptive_layout.report()
        with patch.object(diagnostics,'artifacts_directory',return_value=self.root):
            path=diagnostics.export_report(self.root/'report.zip')
        with zipfile.ZipFile(path) as archive:
            raw=archive.read('display-layout.json');display=json.loads(raw)
            self.assertTrue(display['screens'])
            self.assertIn('device_pixel_ratio',display['screens'][0])
            self.assertEqual(display['layout']['panels'],self.w.main_splitter.sizes())
            self.assertNotIn('PRIVATE_EXPERIMENT_NAME',raw.decode())
            self.assertNotIn(str(self.root),raw.decode())
