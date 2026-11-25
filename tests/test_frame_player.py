import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import threading
import time
import unittest

import h5py
import numpy as np
from PySide6 import QtCore, QtWidgets

from saxs_waxs_core.display import DisplayPreset
from saxs_waxs_core.readers.tvx_tiff import read_tvx_tiff
from saxs_waxs_ui.frame_player import FramePlayer

ROOT = Path(__file__).resolve().parents[1]


class FramePlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "artifacts" / "test_tmp")
        self.path = Path(self.temporary.name) / "stack.h5"
        with h5py.File(self.path, "w") as file:
            group = file.create_group("entry/data")
            group.attrs.update(NX_class="NXdata", signal="data")
            group.create_dataset("data", data=np.arange(60, dtype="uint32").reshape(5, 3, 4))
        self.players = []

    def tearDown(self):
        for player in self.players:
            player.pause()
            self.assertTrue(player.pool.waitForDone(5000))
        self.app.processEvents()
        for player in self.players:
            player.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def wait(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(condition())

    def player(self, loader=read_tvx_tiff):
        player = FramePlayer(loader=loader)
        self.players.append(player)
        player.configure(read_tvx_tiff(self.path, frame_index=0), DisplayPreset())
        return player

    def test_latest_jump_wins_and_event_loop_remains_responsive(self):
        entered, release = threading.Event(), threading.Event()
        def loader(path, **selection):
            if selection["frame_index"] == 1:
                entered.set()
                release.wait(2)
            return read_tvx_tiff(path, **selection)
        player = self.player(loader)
        shown = []
        player.ready.connect(lambda preview: shown.append(preview.frame.metadata.frame_index))
        player.jump(1)
        self.wait(entered.is_set)
        alive = []
        QtCore.QTimer.singleShot(0, lambda: alive.append(True))
        self.wait(lambda: bool(alive))
        player.jump(3)
        player.jump(2)
        release.set()
        self.wait(lambda: shown == [2])
        self.assertFalse(player.playing)
        self.assertLessEqual(len(player.cache), 2)

    def test_pause_discards_pending_display_and_play_stops_at_last_frame(self):
        entered, release = threading.Event(), threading.Event()
        def loader(path, **selection):
            if selection["frame_index"] == 1:
                entered.set()
                release.wait(2)
            return read_tvx_tiff(path, **selection)
        player = self.player(loader)
        shown = []
        player.ready.connect(lambda preview: shown.append(preview.frame.metadata.frame_index))
        self.assertEqual(player.interval_ms, 1)
        player.play()
        self.wait(entered.is_set)
        player.pause()
        release.set()
        self.wait(lambda: not player.busy)
        self.assertEqual(shown, [])
        player.play()
        self.wait(lambda: not player.playing and player.current_index == 4)
        self.assertEqual(shown, [1, 2, 3, 4])
        self.assertLessEqual(len(player.cache), 2)

    def test_frame_error_keeps_current_frame_and_later_jump_can_recover(self):
        def loader(path, **selection):
            if selection["frame_index"] == 1:
                raise ValueError("invalid test frame")
            return read_tvx_tiff(path, **selection)
        player = self.player(loader)
        errors, shown = [], []
        player.failed.connect(errors.append)
        player.ready.connect(lambda preview: shown.append(preview.frame.metadata.frame_index))
        player.play()
        self.wait(lambda: bool(errors))
        self.assertFalse(player.playing)
        self.assertEqual(player.current_index, 0)
        player.jump(3)
        self.wait(lambda: shown == [3])

    def test_switching_source_discards_old_pending_frame(self):
        entered, release = threading.Event(), threading.Event()
        def loader(path, **selection):
            if Path(path)==self.path and selection["frame_index"]==1:
                entered.set()
                release.wait(2)
            return read_tvx_tiff(path, **selection)
        player = self.player(loader)
        shown = []
        player.ready.connect(lambda preview: shown.append(preview.frame))
        player.jump(1)
        self.wait(entered.is_set)
        other=Path(self.temporary.name)/"other.h5"
        with h5py.File(other,"w") as file:
            group=file.create_group("entry/data")
            group.attrs.update(NX_class="NXdata",signal="data")
            group["data"]=np.full((5,3,4),200,dtype="uint32")
        player.configure(read_tvx_tiff(other,frame_index=0),DisplayPreset())
        player.jump(3)
        release.set()
        self.wait(lambda: len(shown)==1)
        self.assertEqual(shown[0].metadata.source_path,other)
        self.assertEqual(shown[0].metadata.frame_index,3)
        np.testing.assert_array_equal(shown[0].data,np.full((3,4),200))


if __name__ == "__main__":
    unittest.main()
