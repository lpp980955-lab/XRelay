from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saxs_waxs_core.discovery import DiscoveredFile  # noqa: E402
from saxs_waxs_core.display import DisplayPreset  # noqa: E402
from saxs_waxs_core.models import ImageFrame, ImageMetadata  # noqa: E402
from saxs_waxs_core.viewer import (  # noqa: E402
    DisplaySyncCoordinator,
    FolderViewerSession,
    UnsupportedViewerFormat,
    load_supported_frame,
)


TEST_TMP_ROOT = ROOT / "artifacts" / "test_tmp"


def discovered(path: Path, root: Path) -> DiscoveredFile:
    return DiscoveredFile(
        path=path,
        relative_path=path.relative_to(root),
        suffix=path.suffix.casefold(),
        size_bytes=path.stat().st_size,
    )


def fake_frame(path: Path) -> ImageFrame:
    import numpy as np

    data = np.array([[1, 2], [3, 4]], dtype=np.int32)
    metadata = ImageMetadata(
        source_path=path,
        format_id="test",
        support_level="test_only",
        width_px=2,
        height_px=2,
        dtype="int32",
    )
    return ImageFrame(data=data, metadata=metadata)


class FolderViewerSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def test_folder_session_uses_all_names_and_recursive_files(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            root = Path(temporary) / "中文 数据"
            nested = root / "不同样品"
            nested.mkdir(parents=True)
            (root / "A-name.tif").write_bytes(b"x")
            (nested / "完全不同命名.tif").write_bytes(b"y")
            session = FolderViewerSession.from_folder(root, loader=fake_frame)
            self.assertEqual(len(session), 2)
            self.assertTrue(session.select(0).ok)
            self.assertTrue(session.move(1).ok)
            self.assertEqual(session.current_index, 1)

    def test_navigation_reports_and_skips_failure(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            root = Path(temporary)
            paths = [root / "good-1.tif", root / "broken.tif", root / "good-2.tif"]
            for path in paths:
                path.write_bytes(b"x")

            def loader(path: Path) -> ImageFrame:
                if path.name == "broken.tif":
                    raise ValueError("synthetic decode failure")
                return fake_frame(path)

            session = FolderViewerSession((discovered(path, root) for path in paths), loader=loader)
            self.assertTrue(session.select(0).ok)
            result = session.move(1, skip_failures=True)
            self.assertTrue(result.ok)
            self.assertEqual(result.index, 2)
            self.assertEqual(len(result.skipped), 1)
            self.assertEqual(result.skipped[0].path.name, "broken.tif")
            self.assertIn(1, session.failures)

    def test_direct_failure_is_visible(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            root = Path(temporary)
            path = root / "bad.tif"
            path.write_bytes(b"x")

            def failing_loader(_path: Path) -> ImageFrame:
                raise RuntimeError("bad frame")

            result = FolderViewerSession([discovered(path, root)], loader=failing_loader).select(0)
            self.assertFalse(result.ok)
            self.assertEqual(result.failure.error_type, "RuntimeError")
            self.assertEqual(result.failure.message, "bad frame")

    def test_default_dispatch_refuses_unvalidated_format(self):
        # GFRM now has a reviewed adapter. Unknown bytes must still fail the gate.
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            path=Path(temporary)/'example.vendor_unknown'
            path.write_bytes(b'not a reviewed detector encoding')
            with self.assertRaises(UnsupportedViewerFormat):
                load_supported_frame(path)

    def test_navigation_rejects_ambiguous_jump_delta(self):
        session = FolderViewerSession([])
        with self.assertRaisesRegex(ValueError, "-1 or 1"):
            session.move(2)


class DisplaySyncCoordinatorTests(unittest.TestCase):
    def test_link_uses_leader_then_propagates_updates(self):
        coordinator = DisplaySyncCoordinator()
        first = DisplayPreset(colormap="viridis")
        second = DisplayPreset(colormap="magma")
        shared = DisplayPreset(colormap="plasma", scale="log")
        coordinator.register("left", first)
        coordinator.register("right", second)
        coordinator.link("compare", ["left", "right"], leader_id="right")
        self.assertEqual(coordinator.preset_for("left"), second)
        self.assertEqual(coordinator.update("left", shared), ("left", "right"))
        self.assertEqual(coordinator.preset_for("right"), shared)

    def test_unlink_preserves_current_preset_and_future_independence(self):
        coordinator = DisplaySyncCoordinator()
        preset = DisplayPreset(colormap="magma")
        coordinator.register("a", DisplayPreset())
        coordinator.register("b", preset)
        coordinator.link("g", ["a", "b"], leader_id="b")
        coordinator.unlink("a")
        coordinator.update("b", DisplayPreset(colormap="plasma"))
        self.assertEqual(coordinator.preset_for("a"), preset)
        self.assertNotEqual(coordinator.preset_for("a"), coordinator.preset_for("b"))

    def test_unregistered_viewer_is_rejected(self):
        coordinator = DisplaySyncCoordinator()
        with self.assertRaises(KeyError):
            coordinator.update("missing", DisplayPreset())


if __name__ == "__main__":
    unittest.main()

