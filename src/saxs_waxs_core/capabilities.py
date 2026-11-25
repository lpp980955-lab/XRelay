from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path

from .readers.tvx_tiff import probe_tvx_tiff


class ReadLevel(str, Enum):
    DISCOVERED_ONLY = "discovered_only"
    PREVIEW_ONLY = "preview_only"
    QUANTITATIVE_CANDIDATE = "quantitative_candidate"
    STABLE = "stable"


class WorkflowStatus(str, Enum):
    UNAVAILABLE = "unavailable"
    PREVIEW_ONLY = "preview_only"
    NEEDS_PARAMETERS = "needs_parameters"
    CANDIDATE_READY = "candidate_ready"
    STABLE_READY = "stable_ready"


@dataclass(frozen=True, slots=True)
class GeometryInputs:
    """Only parameter presence and basic numeric validity; no angle convention."""

    wavelength_m: float | None = None
    detector_distance_m: float | None = None
    beam_center_x_px: float | None = None
    beam_center_y_px: float | None = None

    def __post_init__(self) -> None:
        positive = {
            "wavelength_m": self.wavelength_m,
            "detector_distance_m": self.detector_distance_m,
        }
        for name, value in positive.items():
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and positive")
        for name, value in {
            "beam_center_x_px": self.beam_center_x_px,
            "beam_center_y_px": self.beam_center_y_px,
        }.items():
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and cannot be negative")

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "wavelength_m",
                "detector_distance_m",
                "beam_center_x_px",
                "beam_center_y_px",
            )
            if getattr(self, name) is None
        )

    @property
    def complete(self) -> bool:
        return not self.missing


@dataclass(frozen=True, slots=True)
class FileCapability:
    path: Path
    format_id: str | None
    read_level: ReadLevel
    workflow_status: WorkflowStatus
    missing_parameters: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def can_preview(self) -> bool:
        return self.read_level in {
            ReadLevel.PREVIEW_ONLY,
            ReadLevel.QUANTITATIVE_CANDIDATE,
            ReadLevel.STABLE,
        }

    @property
    def can_enter_quantitative_workflow(self) -> bool:
        return self.workflow_status in {
            WorkflowStatus.CANDIDATE_READY,
            WorkflowStatus.STABLE_READY,
        }


def classify_file_capability(
    path: str | Path,
    *,
    geometry: GeometryInputs | None = None,
) -> FileCapability:
    """Classify evidence without promoting display success to stable support."""

    source = Path(path).expanduser().absolute()
    suffix = source.suffix.casefold()
    supplied=geometry is not None
    geometry = geometry or GeometryInputs()

    from .readers.detector_io import supported_detector_path
    if supported_detector_path(source):
        probe = probe_tvx_tiff(source)
        if probe.support_level=='selection_required':
            return FileCapability(source,probe.format_id,ReadLevel.PREVIEW_ONLY,
                WorkflowStatus.NEEDS_PARAMETERS,('dataset_selection',),(probe.reason,'stable_gate_not_yet_passed'))
        if probe.support_level == "quantitative_candidate":
            if not supplied:
                from .readers.tvx_tiff import read_tvx_tiff
                from .processed_data import embedded_geometry
                saved=embedded_geometry(read_tvx_tiff(source,frame_index=0))
                if saved is not None:
                    geometry=GeometryInputs(saved.wavelength_m,saved.detector_distance_m,saved.beam_center_x_px,saved.beam_center_y_px)
            status = (
                WorkflowStatus.CANDIDATE_READY
                if geometry.complete
                else WorkflowStatus.NEEDS_PARAMETERS
            )
            return FileCapability(
                path=source,
                format_id=probe.format_id,
                read_level=ReadLevel.QUANTITATIVE_CANDIDATE,
                workflow_status=status,
                missing_parameters=geometry.missing,
                evidence=("content_decoded_detector_profile", "stable_gate_not_yet_passed"),
            )
        return FileCapability(
            path=source,
            format_id=probe.format_id,
            read_level=ReadLevel.DISCOVERED_ONLY,
            workflow_status=WorkflowStatus.UNAVAILABLE,
            missing_parameters=(),
            evidence=(probe.support_level, probe.reason),
        )

    if suffix in {".gfrm", ".cbf", ".edf", ".h5", ".hdf5", ".nxs"}:
        return FileCapability(
            path=source,
            format_id=suffix.removeprefix(".") or None,
            read_level=ReadLevel.DISCOVERED_ONLY,
            workflow_status=WorkflowStatus.UNAVAILABLE,
            missing_parameters=(),
            evidence=("extension_discovered", "no_validated_reader_for_current_project"),
        )

    return FileCapability(
        path=source,
        format_id=None,
        read_level=ReadLevel.DISCOVERED_ONLY,
        workflow_status=WorkflowStatus.UNAVAILABLE,
        missing_parameters=(),
        evidence=("extension_not_registered",),
    )

