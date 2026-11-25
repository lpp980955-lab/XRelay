import os
import unittest
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6 import QtCore, QtWidgets
from saxs_waxs_core.display import DisplayPreset
from saxs_waxs_ui.app import ImageViewerWidget, FolderBrowserWidget, EmbeddedViewerPane
from saxs_waxs_ui.frame_selection import FrameSelectionDialog


class FrameSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_range_and_explicit_selection_preserve_zero_based_frame_order(self):
        dialog = FrameSelectionDialog(40)
        try:
            self.assertIsNone(dialog.selected_frames())
            dialog.mode.setCurrentIndex(1)
            dialog.first.setValue(2)
            dialog.last.setValue(40)
            dialog.step.setValue(3)
            self.assertEqual(dialog.selected_frames(), list(range(1, 40, 3)))
            dialog.mode.setCurrentIndex(2)
            dialog.model.checked = {0, 19, 39}
            dialog.model.setData(dialog.model.index(19, 0),
                QtCore.Qt.CheckState.Unchecked.value, QtCore.Qt.ItemDataRole.CheckStateRole)
            self.assertEqual(dialog.selected_frames(), [0, 39])
            dialog.model.checked.clear()
            dialog._accept()
            self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Rejected)
            self.assertTrue(dialog.status.text())
        finally:
            dialog.close()

    def test_typed_frame_commits_automatically_and_play_pause_share_one_icon(self):
        pane=EmbeddedViewerPane(1,primary=True)
        requested=[]
        pane.frame_requested.connect(lambda source,index:requested.append(index))
        try:
            pane.frame_spin.setMaximum(40)
            self.assertEqual(pane.play_frames_button.text(), "")
            self.assertFalse(pane.play_frames_button.icon().isNull())
            self.assertFalse(hasattr(pane,"jump_frame_button"))
            self.assertFalse(hasattr(pane,"playback_rate"))
            pane.frame_spin.lineEdit().setText("20")
            pane.frame_player.playing=True
            pane.frame_player.playing_changed.emit(True)
            self.assertEqual(pane.play_frames_button.toolTip(),"暂停")
            pane.play_frames_button.click()
            self.assertFalse(pane.frame_player.playing)
            self.assertEqual(pane.play_frames_button.toolTip(),"播放")
            pane.frame_player.playing=True
            pane.frame_spin.lineEdit().textEdited.emit("20")
            self.assertFalse(pane.frame_player.playing)
            self.assertEqual(requested,[])
            deadline=time.monotonic()+2
            while not requested and time.monotonic()<deadline:
                self.app.processEvents()
                time.sleep(.005)
            self.assertEqual(requested,[19])
            pane.frame_spin.lineEdit().setText("32")
            pane.frame_spin.editingFinished.emit()
            self.assertEqual(requested,[19,31])
        finally:
            pane.close()

    def test_detector_mask_excludes_sentinel_from_display_without_changing_counts(self):
        data = np.array([[1, 2, 3], [4, 5, 4294967295]], dtype="uint32")
        excluded = np.array([[0, 0, 0], [0, 0, 1]], dtype=bool)
        viewer = ImageViewerWidget()
        try:
            viewer.set_data(data, excluded)
            viewer.set_preset(DisplayPreset(lower_percentile=0, upper_percentile=100))
            self.assertIn("1 – 5", viewer.status_label.text())
            np.testing.assert_array_equal(viewer.data, data)
            user = np.zeros(data.shape, dtype=bool)
            user[0, 0] = True
            viewer.set_mask_overlay(user)
            self.assertTrue(viewer.mask[0, 0])
            self.assertTrue(viewer.mask[1, 2])
            viewer.clear_mask_overlay()
            np.testing.assert_array_equal(viewer.mask, excluded)
        finally:
            viewer.close()

    def test_folder_uses_external_links_to_collapse_chunks_and_restore_old_selections(self):
        import h5py
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/"artifacts"/"test_tmp") as temporary:
            folder=Path(temporary)
            chunk=folder/"a_renamed_payload.h5"
            master=folder/"z_entry.h5"
            with h5py.File(chunk,"w") as file:
                file["data"]=np.arange(24,dtype="uint32").reshape(2,3,4)
            with h5py.File(master,"w") as file:
                data=file.create_group("entry/data")
                data.attrs["NX_class"]="NXdata"
                data["data_000001"]=h5py.ExternalLink(chunk.name,"/data")
            browser=FolderBrowserWidget()
            try:
                browser.set_folder(folder)
                self.assertEqual([item.path for item in browser.files],[master])
                self.assertEqual(browser.select_paths((chunk,)),(master,))
                self.assertEqual(browser.set_current_path(chunk),master)
                master.unlink()  # Synthetic fixture only; standalone chunks remain visible.
                browser.set_folder(folder)
                self.assertEqual([item.path for item in browser.files],[chunk])
                self.assertEqual(browser.source_masters,{})
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
