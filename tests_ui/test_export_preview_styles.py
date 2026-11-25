import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PIL import Image
from PySide6 import QtCore,QtWidgets
from saxs_waxs_ui.app import FolderBrowserWidget,create_application
from saxs_waxs_ui.image_export_dialog import ImageExportDialog
from saxs_waxs_ui.export_preview import enable_export_preview
from saxs_waxs_core.display import DisplayPreset


class ExportPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=create_application([])

    def test_four_files_individual_frames_and_parent_toggle(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for n in range(4):Image.fromarray(np.ones((8,9),dtype='uint16')).save(root/f's{n}.tif')
            browser=FolderBrowserWidget();browser.set_folder(root)
            tree=browser.file_list;chosen={}
            tree.frames_checked.connect(lambda p,s:chosen.update({str(p):s}))
            for row in range(4):
                path=browser.files[row].path;tree.register(row,path,10,None)
                parent=tree.item(row);tree._expand(parent)
                parent.setCheckState(QtCore.Qt.CheckState.Checked)
                self.assertEqual(parent.frame_selection,set(range(10)))
                parent.setCheckState(QtCore.Qt.CheckState.Unchecked)
                self.assertFalse(parent.frame_selection)
                self.assertTrue(all(parent.child(i).checkState(0)==QtCore.Qt.CheckState.Unchecked for i in range(10)))
                parent.child(4).setCheckState(0,QtCore.Qt.CheckState.Checked)
                self.assertEqual(parent.checkState(),QtCore.Qt.CheckState.PartiallyChecked)
                # Repeated count/label refresh must not erase the child choice.
                tree.register(row,path,10,[4])
                self.assertEqual(parent.frame_selection,{4})
            self.assertEqual(len(browser.selected_paths),4)
            self.assertEqual(list(chosen.values()),[[4]]*4)
            browser.close()

    def test_geometry_library_persists_and_loads_without_applying(self):
        from saxs_waxs_ui.geometry_panel import GeometryPanel
        with tempfile.TemporaryDirectory() as temp:
            settings=QtCore.QSettings(str(Path(temp)/'geometry.ini'),QtCore.QSettings.Format.IniFormat)
            panel=GeometryPanel(library_settings=settings)
            panel.distance_spin.setValue(400);panel.wavelength_spin.setValue(.10332)
            panel.center_x_spin.setValue(234);panel.center_y_spin.setValue(345)
            save=next(b for b in panel.findChildren(QtWidgets.QPushButton) if b.text()=='另存参数')
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('参数1',True)):save.click()
            panel.distance_spin.setValue(800)
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('参数2',True)):save.click()
            panel.close()
            other=GeometryPanel(library_settings=settings);applied=[]
            other.apply_requested.connect(applied.append)
            other.geometry_library_combo.setCurrentText('参数1')
            next(b for b in other.findChildren(QtWidgets.QPushButton) if b.text()=='套用').click()
            self.assertEqual(other.distance_spin.value(),400)
            self.assertAlmostEqual(other.wavelength_spin.value(),.10332)
            self.assertEqual(other.center_x_spin.value(),234)
            self.assertEqual(other.center_y_spin.value(),345)
            self.assertEqual(applied,[])
            rename=next(b for b in other.findChildren(QtWidgets.QPushButton) if b.text()=='重命名')
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('LFY几何',True)):rename.click()
            self.assertEqual(other.geometry_library_combo.currentText(),'LFY几何')
            self.assertEqual(other.geometry_library_combo.count(),2)
            other.close()

    def test_preview_updates_and_named_style_survives_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=QtCore.QSettings(str(Path(temp)/'styles.ini'),QtCore.QSettings.Format.IniFormat)
            data=np.arange(2000).reshape(40,50)
            provider=lambda raw:(data,None,DisplayPreset(),None)
            dialog=ImageExportDialog(batch_available=True)
            enable_export_preview(dialog,provider,settings=settings);dialog.show()
            def wait_image():
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    self.app.processEvents();time.sleep(.01)
                    if not dialog._export_preview_tasks.busy and not dialog._export_preview_timer.isActive():return
                self.fail('Preview timed out')
            wait_image()
            self.assertEqual(max(dialog.preview_canvas.item.pixmap().width(),dialog.preview_canvas.item.pixmap().height()),round(180/25.4*round(2800/(180/25.4))))
            old_size=dialog.size();old_view=dialog.preview_canvas.size()
            old_image=dialog.preview_canvas.item.pixmap().toImage()
            dialog.advanced_button.setChecked(True);self.app.processEvents()
            for scroll in dialog.findChildren(QtWidgets.QScrollArea):
                self.assertEqual(scroll.horizontalScrollBar().maximum(),0)
            self.assertEqual(dialog.size(),old_size)
            self.assertEqual(dialog.preview_canvas.size(),old_view)
            dialog.tick_font_spin.setValue(14);wait_image()
            self.assertEqual(dialog.preview_canvas.size(),old_view)
            self.assertNotEqual(dialog.preview_canvas.item.pixmap().toImage(),old_image)
            dialog.width_spin.setValue(150);dialog.axes_check.setChecked(False)
            wait_image()
            save=next(b for b in dialog.findChildren(QtWidgets.QPushButton) if b.text()=='另存样式')
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('样式1',True)):save.click()
            dialog.accept();self.app.processEvents()
            other=ImageExportDialog(batch_available=False)
            enable_export_preview(other,provider,settings=settings)
            self.assertEqual(other.width_spin.value(),150)
            self.assertFalse(other.axes_check.isChecked())
            other.reject();self.app.processEvents()


if __name__=='__main__':unittest.main()
