from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .discovery import DiscoveredFile, discover_image_files
from .display import DisplayPreset
from .models import ImageFrame
from .readers.tvx_tiff import read_tvx_tiff


class ViewerError(RuntimeError):
    """Base error for the GUI-independent folder viewer model."""


class UnsupportedViewerFormat(ViewerError):
    pass


FrameLoader = Callable[[Path], ImageFrame]


def load_supported_frame(path: Path) -> ImageFrame:
    """Dispatch only formats that have a currently validated reader."""

    suffix = path.suffix.casefold()
    from .readers.detector_io import supported_detector_path
    if supported_detector_path(path):
        return read_tvx_tiff(path)
    raise UnsupportedViewerFormat(
        f"{suffix or '<no suffix>'} is discoverable but has no validated reader"
    )


@dataclass(frozen=True, slots=True)
class ViewerFailure:
    index: int
    path: Path
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class ViewerSelection:
    index: int | None
    frame: ImageFrame | None
    failure: ViewerFailure | None = None
    skipped: tuple[ViewerFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return self.frame is not None and self.failure is None


class FolderViewerSession:
    """Browse one discovered folder without relying on filename prefixes.

    The object keeps only the current full-resolution frame. Thumbnail caching is
    handled separately, so browsing a large folder does not retain every image in
    memory. Failures are recorded and surfaced rather than silently omitted.
    """

    def __init__(
        self,
        files: Iterable[DiscoveredFile],
        *,
        loader: FrameLoader = load_supported_frame,
    ) -> None:
        self.files = tuple(files)
        self._loader = loader
        self.current_index: int | None = None
        self.current_frame: ImageFrame | None = None
        self.failures: dict[int, ViewerFailure] = {}

    @classmethod
    def from_folder(
        cls,
        root: str | Path,
        *,
        recursive: bool = True,
        loader: FrameLoader = load_supported_frame,
    ) -> "FolderViewerSession":
        return cls(discover_image_files(root, recursive=recursive), loader=loader)

    def __len__(self) -> int:
        return len(self.files)

    def _failure(self, index: int, exc: Exception) -> ViewerFailure:
        failure = ViewerFailure(
            index=index,
            path=self.files[index].path,
            error_type=type(exc).__name__,
            message=str(exc),
        )
        self.failures[index] = failure
        return failure

    def select(self, index: int) -> ViewerSelection:
        if not 0 <= index < len(self.files):
            raise IndexError(f"Viewer index out of range: {index}")
        try:
            frame = self._loader(self.files[index].path)
        except Exception as exc:
            self.current_index = index
            self.current_frame = None
            return ViewerSelection(index=index, frame=None, failure=self._failure(index, exc))
        self.current_index = index
        self.current_frame = frame
        self.failures.pop(index, None)
        return ViewerSelection(index=index, frame=frame)

    def move(
        self,
        delta: int,
        *,
        skip_failures: bool = True,
        wrap: bool = False,
    ) -> ViewerSelection:
        if delta not in {-1, 1}:
            raise ValueError("Viewer move delta must be -1 or 1")
        if not self.files:
            return ViewerSelection(index=None, frame=None)

        step = 1 if delta > 0 else -1
        start = self.current_index if self.current_index is not None else (-1 if step > 0 else len(self.files))
        attempted: set[int] = set()
        skipped: list[ViewerFailure] = []
        candidate = start

        while len(attempted) < len(self.files):
            candidate += step
            if wrap:
                candidate %= len(self.files)
            elif candidate < 0 or candidate >= len(self.files):
                break
            if candidate in attempted:
                break
            attempted.add(candidate)
            result = self.select(candidate)
            if result.ok or not skip_failures:
                return ViewerSelection(
                    index=result.index,
                    frame=result.frame,
                    failure=result.failure,
                    skipped=tuple(skipped),
                )
            if result.failure is not None:
                skipped.append(result.failure)

        return ViewerSelection(index=None, frame=None, skipped=tuple(skipped))


class DisplaySyncCoordinator:
    """Coordinate optional shared display presets across viewer windows."""

    def __init__(self) -> None:
        self._presets: dict[str, DisplayPreset] = {}
        self._viewer_group: dict[str, str] = {}
        self._groups: dict[str, set[str]] = {}

    def register(self, viewer_id: str, preset: DisplayPreset) -> None:
        if not viewer_id:
            raise ValueError("viewer_id cannot be empty")
        if viewer_id in self._presets:
            raise ValueError(f"Viewer already registered: {viewer_id}")
        self._presets[viewer_id] = preset

    def unregister(self, viewer_id: str) -> None:
        self.unlink(viewer_id)
        self._presets.pop(viewer_id, None)

    def preset_for(self, viewer_id: str) -> DisplayPreset:
        try:
            return self._presets[viewer_id]
        except KeyError as exc:
            raise KeyError(f"Unknown viewer: {viewer_id}") from exc

    def link(self, group_id: str, viewer_ids: Iterable[str], *, leader_id: str) -> None:
        members = set(viewer_ids)
        if leader_id not in members:
            raise ValueError("leader_id must be included in viewer_ids")
        if not group_id:
            raise ValueError("group_id cannot be empty")
        unknown = members.difference(self._presets)
        if unknown:
            raise KeyError(f"Unknown viewers: {sorted(unknown)}")
        for viewer_id in members:
            self.unlink(viewer_id)
        leader_preset = self._presets[leader_id]
        self._groups[group_id] = members
        for viewer_id in members:
            self._viewer_group[viewer_id] = group_id
            self._presets[viewer_id] = leader_preset

    def unlink(self, viewer_id: str) -> None:
        group_id = self._viewer_group.pop(viewer_id, None)
        if group_id is None:
            return
        members = self._groups[group_id]
        members.discard(viewer_id)
        if len(members) < 2:
            for remaining in members:
                self._viewer_group.pop(remaining, None)
            self._groups.pop(group_id, None)

    def update(self, viewer_id: str, preset: DisplayPreset) -> tuple[str, ...]:
        if viewer_id not in self._presets:
            raise KeyError(f"Unknown viewer: {viewer_id}")
        group_id = self._viewer_group.get(viewer_id)
        affected = {viewer_id} if group_id is None else set(self._groups[group_id])
        for affected_id in affected:
            self._presets[affected_id] = preset
        return tuple(sorted(affected))

