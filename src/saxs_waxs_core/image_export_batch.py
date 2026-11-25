"""Recoverable batch export of processed 2D detector images.

Every frame is processed by the reviewed :class:`BatchProcessingPlan` before
the image is rendered.  This keeps background subtraction and MASK semantics
identical to integration while keeping 2D representation choices separate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .background import BackgroundMode
from .display import DisplayPreset
from .image_export import export_2d_image
from .presets import ExportPreset, export_preset_from_document, export_preset_to_document
from .processing import (
    BatchProcessingPlan,
    FileIdentity,
    batch_processing_plan_from_document,
    batch_processing_plan_to_json,
    execute_processing_recipe,
    processing_recipe_parameter_summary,
)
from .viewer import FrameLoader, load_supported_frame
from .readers.detector_io import load_plan_sample,selection_suffix,load_background_source
from .processing import processing_geometry


IMAGE_EXPORT_BATCH_SCHEMA = "saxs_waxs_2d_image_batch/2"
LEGACY_IMAGE_EXPORT_BATCH_SCHEMA = "saxs_waxs_2d_image_batch/1"
RAW_IMAGE_EXPORT_PLAN_SCHEMA = "saxs_waxs_raw_2d_export_plan/1"


class ImageExportBatchError(RuntimeError):
    """Raised when a 2D image batch cannot be started or safely resumed."""


@dataclass(frozen=True, slots=True)
class ImageExportBatchResult:
    manifest_path: Path
    state: str
    summary: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ImageExportResumeSpec:
    """Typed, validated inputs recovered from one existing batch manifest."""

    manifest_path: Path
    plan: BatchProcessingPlan | RawImageExportPlan
    display: DisplayPreset
    export: ExportPreset
    state: str


@dataclass(frozen=True, slots=True)
class RawImageExportPlan:
    """Explicit file identities for batch rendering without scientific processing."""

    sample_sources: Sequence[FileIdentity]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        sources = tuple(self.sample_sources)
        if not sources:
            raise ImageExportBatchError("raw image-export plan requires at least one source")
        if any(not isinstance(item, FileIdentity) for item in sources):
            raise ImageExportBatchError("raw image-export sources must be FileIdentity values")
        selections=self.metadata.get('source_selections',[{} for item in sources])
        if len(selections)!=len(sources):
            raise ImageExportBatchError('source selections do not align with frames')
        path_keys = {
            (str(Path(item.path).expanduser().absolute().resolve(strict=False)).casefold(),json.dumps(selections[i],sort_keys=True))
            for i,item in enumerate(sources)
        }
        if len(path_keys) != len(sources):
            raise ImageExportBatchError("raw image-export plan contains duplicate source paths")
        object.__setattr__(self, "sample_sources", sources)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def _raw_plan_document(plan: RawImageExportPlan) -> dict[str, Any]:
    return {
        "schema": RAW_IMAGE_EXPORT_PLAN_SCHEMA,
        "sample_sources": [
            {
                "path": item.path,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in plan.sample_sources
        ],
        "metadata": dict(plan.metadata),
    }


def raw_image_export_plan_from_document(document: Mapping[str, Any]) -> RawImageExportPlan:
    expected = {"schema", "sample_sources", "metadata"}
    if not isinstance(document, Mapping) or set(document) != expected:
        raise ImageExportBatchError("raw image-export plan fields differ from schema")
    if document["schema"] != RAW_IMAGE_EXPORT_PLAN_SCHEMA:
        raise ImageExportBatchError("raw image-export plan schema is unsupported")
    values = document["sample_sources"]
    if not isinstance(values, list) or not values:
        raise ImageExportBatchError("raw image-export plan requires source identities")
    identities: list[FileIdentity] = []
    for value in values:
        if not isinstance(value, Mapping) or set(value) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise ImageExportBatchError("raw image-export source fields are invalid")
        try:
            identities.append(
                FileIdentity(
                    path=str(value["path"]),
                    size_bytes=int(value["size_bytes"]),
                    sha256=str(value["sha256"]),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ImageExportBatchError(
                f"raw image-export source identity is invalid: {exc}"
            ) from exc
    metadata = document["metadata"]
    if not isinstance(metadata, Mapping):
        raise ImageExportBatchError("raw image-export metadata must be a mapping")
    return RawImageExportPlan(tuple(identities), metadata=dict(metadata))


def _display_preset_from_manifest(document: Any) -> DisplayPreset:
    expected = {
        "colormap",
        "scale",
        "limit_mode",
        "lower_percentile",
        "upper_percentile",
        "manual_vmin",
        "manual_vmax",
        "invert_colormap",
        "custom_colors",
    }
    if not isinstance(document, Mapping) or set(document) != expected:
        raise ImageExportBatchError("manifest display fields are invalid")
    try:
        return DisplayPreset(
            colormap=str(document["colormap"]),
            scale=document["scale"],
            limit_mode=document["limit_mode"],
            lower_percentile=float(document["lower_percentile"]),
            upper_percentile=float(document["upper_percentile"]),
            manual_vmin=(
                None
                if document["manual_vmin"] is None
                else float(document["manual_vmin"])
            ),
            manual_vmax=(
                None
                if document["manual_vmax"] is None
                else float(document["manual_vmax"])
            ),
            invert_colormap=bool(document["invert_colormap"]),
            custom_colors=tuple(document["custom_colors"]),
        )
    except (TypeError, ValueError) as exc:
        raise ImageExportBatchError(f"manifest display preset is invalid: {exc}") from exc


def load_image_export_resume_spec(
    manifest_path: str | os.PathLike[str],
) -> ImageExportResumeSpec:
    """Load the exact saved inputs required to resume one 2D export batch."""

    path = Path(manifest_path).expanduser().absolute()
    if not path.is_file():
        raise ImageExportBatchError(f"image-export manifest is missing: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImageExportBatchError(f"cannot read image-export manifest: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ImageExportBatchError("image-export manifest must be a JSON object")
    if document.get("schema") not in {
        IMAGE_EXPORT_BATCH_SCHEMA,
        LEGACY_IMAGE_EXPORT_BATCH_SCHEMA,
    }:
        raise ImageExportBatchError("image-export manifest schema is unsupported")
    source_mode = document.get("source_mode")
    plan_document = document.get("plan")
    try:
        if source_mode == "processed_recipe":
            plan: BatchProcessingPlan | RawImageExportPlan = (
                batch_processing_plan_from_document(plan_document)
            )
        elif source_mode == "raw_array":
            plan = raw_image_export_plan_from_document(plan_document)
        else:
            raise ImageExportBatchError("image-export source mode is unsupported")
        display = _display_preset_from_manifest(document.get("display"))
        export = export_preset_from_document(document.get("export"))
    except ImageExportBatchError:
        raise
    except Exception as exc:
        raise ImageExportBatchError(f"image-export manifest inputs are invalid: {exc}") from exc
    state = document.get("state")
    if not isinstance(state, str):
        raise ImageExportBatchError("image-export manifest state is invalid")
    return ImageExportResumeSpec(
        manifest_path=path,
        plan=plan,
        display=display,
        export=export,
        state=state,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _refuse_protected_output(
    run_root: Path, protected_roots: Sequence[str | os.PathLike[str]]
) -> None:
    resolved = run_root.resolve(strict=False)
    for root in protected_roots:
        protected = Path(root).expanduser().absolute().resolve(strict=False)
        if _is_within(resolved, protected):
            raise ImageExportBatchError(
                f"refusing to write batch outputs inside protected root: {protected}"
            )


def _verify_identity(identity: FileIdentity) -> Path:
    path = Path(identity.path).expanduser().absolute()
    if not path.is_file():
        raise ImageExportBatchError(f"source file is missing: {path}")
    actual = FileIdentity.from_path(path)
    if actual.size_bytes != identity.size_bytes or actual.sha256 != identity.sha256:
        raise ImageExportBatchError(f"source identity changed since plan review: {path}")
    return path


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(items),
        "pending": sum(item["status"] == "pending" for item in items),
        "passed": sum(item["status"] == "passed" for item in items),
        "failed": sum(item["status"] == "failed" for item in items),
    }


def _save_manifest(path: Path, document: dict[str, Any]) -> None:
    document["summary"] = _summary(document["items"])
    _atomic_write_text(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def _result_stems(sources: Sequence[FileIdentity]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for identity in sources:
        key = Path(identity.path).stem.casefold()
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        f"{Path(identity.path).stem}__{identity.sha256[:8]}"
        if counts[Path(identity.path).stem.casefold()] > 1
        else Path(identity.path).stem
        for identity in sources
    )


def _item_template(identity: FileIdentity, revision_id: str) -> dict[str, Any]:
    return {
        "source": {
            "path": identity.path,
            "size_bytes": identity.size_bytes,
            "sha256": identity.sha256,
        },
        "recipe_revision": revision_id,
        "status": "pending",
        "message": "",
        "image_file": None,
        "output_sha256": None,
        "resolved_limits": None,
        "array": None,
        "render": None,
        "started_at": None,
        "finished_at": None,
    }


def run_image_export_batch(
    plan: BatchProcessingPlan | RawImageExportPlan,
    run_directory: str | os.PathLike[str],
    display: DisplayPreset,
    export: ExportPreset,
    *,
    protected_roots: Sequence[str | os.PathLike[str]] = (),
    resume: bool = False,
    loader: FrameLoader = load_supported_frame,
    stop_requested: Callable[[], bool] | None = None,
    progress_callback: Callable[[Mapping[str, int]], None] | None = None,
) -> ImageExportBatchResult:
    """Process and export every explicitly reviewed sample as a 2D image.

    ``shared_manual`` deliberately means one reviewed manual range, not a hidden
    estimate from batch pixels.  It therefore requires manual limits in the
    display preset; ``independent`` resolves each frame using its own display
    preset policy.
    """

    if export.detector_range_mode == "shared_manual" and display.limit_mode != "manual":
        raise ImageExportBatchError(
            "shared_manual range requires a display preset with manual limits"
        )
    if export.overwrite:
        raise ImageExportBatchError(
            "batch overwrite is not supported; choose a new run directory or resume"
        )

    run_root = Path(run_directory).expanduser().absolute()
    _refuse_protected_output(run_root, protected_roots)
    manifest_path = run_root / "image_export_manifest.json"
    export_document = export_preset_to_document(export)
    processed_mode = isinstance(plan, BatchProcessingPlan)
    if export.detector_axis_mode != "pixel" and not processed_mode:
        raise ImageExportBatchError(
            "detector-mm axes require a processed batch plan with applied geometry"
        )
    if processed_mode:
        plan_document = json.loads(batch_processing_plan_to_json(plan))
        source_mode = "processed_recipe"
    elif isinstance(plan, RawImageExportPlan):
        plan_document = _raw_plan_document(plan)
        source_mode = "raw_array"
    else:  # defensive API boundary
        raise ImageExportBatchError("unsupported image-export plan type")
    sources = tuple(plan.sample_sources)
    batch_signature = json.dumps(
        {
            "source_mode": source_mode,
            "plan": plan_document,
            "display": {
                "colormap": display.colormap,
                "scale": display.scale,
                "limit_mode": display.limit_mode,
                "lower_percentile": display.lower_percentile,
                "upper_percentile": display.upper_percentile,
                "manual_vmin": display.manual_vmin,
                "manual_vmax": display.manual_vmax,
                "invert_colormap": display.invert_colormap,
                "custom_colors": list(display.custom_colors),
            },
            "export": export_document,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    signature_sha256 = hashlib.sha256(batch_signature.encode("utf-8")).hexdigest()

    background_frame = None
    if processed_mode and plan.recipe.background.mode is BackgroundMode.AUTO_AIR:
        if plan.recipe.background_source is None:
            raise ImageExportBatchError("auto-air batch has no background identity")
        _verify_identity(plan.recipe.background_source)
        background_frame = load_background_source(plan.recipe,loader)

    if manifest_path.exists():
        if not resume:
            raise ImageExportBatchError("image-export manifest already exists; use resume")
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ImageExportBatchError(f"cannot read existing manifest: {exc}") from exc
        if document.get("schema") not in {
            IMAGE_EXPORT_BATCH_SCHEMA,
            LEGACY_IMAGE_EXPORT_BATCH_SCHEMA,
        }:
            raise ImageExportBatchError("existing image-export manifest schema is unsupported")
        if document.get("signature_sha256") != signature_sha256:
            raise ImageExportBatchError("existing manifest belongs to different settings")
        items = document.get("items")
        if not isinstance(items, list) or len(items) != len(sources):
            raise ImageExportBatchError("existing manifest item list does not match plan")
        document["state"] = "running"
        document["resumed_at"] = _utc_now()
    else:
        if run_root.exists():
            if not run_root.is_dir():
                raise ImageExportBatchError("run directory exists but is not a directory")
            if any(run_root.iterdir()):
                raise ImageExportBatchError("run directory is not empty")
        run_root.mkdir(parents=True, exist_ok=True)
        items = [
            _item_template(
                identity,
                plan.revision_id_for_index(index) if processed_mode else "raw",
            )
            for index, identity in enumerate(sources)
        ]
        document = {
            "schema": IMAGE_EXPORT_BATCH_SCHEMA,
            "state": "running",
            "created_at": _utc_now(),
            "finished_at": None,
            "signature_sha256": signature_sha256,
            "source_mode": source_mode,
            "plan": plan_document,
            "display": json.loads(batch_signature)["display"],
            "export": export_document,
            "parameter_organization": {
                "description": (
                    "This is the only parameter JSON for the export operation. "
                    "Shared settings are stored once; items contain only source, "
                    "output, resolved display limits and other per-file results."
                ),
                "per_image_json_written": False,
            },
            "manuscript_parameters": (
                {
                    "base": processing_recipe_parameter_summary(plan.recipe),
                    "revisions": {
                        revision.revision_id: processing_recipe_parameter_summary(
                            revision.recipe
                        )
                        for revision in plan.recipe_revisions
                    },
                }
                if processed_mode
                else {
                    "processing_stage": "raw_array",
                    "geometry": plan.metadata.get(
                        "candidate_geometry_not_applied"
                    ),
                    "geometry_status": "not_applied_to_raw_pixels",
                    "background": "not_applied",
                    "mask": "not_applied",
                    "integration": "not_applied",
                }
            ),
            "items": items,
            "summary": {},
        }
        _save_manifest(manifest_path, document)
    if progress_callback is not None:
        progress_callback(dict(document["summary"]))

    stems = tuple(stem+selection_suffix(plan,index) for index,stem in enumerate(_result_stems(sources)))
    suffix = "." + export.detector_image_format
    for index, (identity, item, stem) in enumerate(
        zip(sources, items, stems, strict=True)
    ):
        if stop_requested is not None and stop_requested():
            document["state"] = "paused"
            _save_manifest(manifest_path, document)
            return ImageExportBatchResult(manifest_path, "paused", document["summary"])
        if item.get("status") == "passed":
            image_name = item.get("image_file")
            expected_hash = item.get("output_sha256")
            if image_name and expected_hash:
                try:
                    with (run_root / image_name).open("rb") as image_file:
                        actual_hash = hashlib.file_digest(image_file, "sha256").hexdigest()
                except OSError:
                    actual_hash = None
                if actual_hash == expected_hash:
                    continue
        item.update(
            status="pending",
            message="",
            image_file=None,
            output_sha256=None,
            resolved_limits=None,
            array=None,
            render=None,
            started_at=_utc_now(),
            finished_at=None,
        )
        _save_manifest(manifest_path, document)
        try:
            source_path = _verify_identity(identity)
            sample = load_plan_sample(plan,index,loader)
            if processed_mode:
                recipe = plan.recipe_for_index(index)
                geometry=processing_geometry(recipe,sample)
                expected_shape = (
                    geometry.image_height_px,
                    geometry.image_width_px,
                )
                if sample.data.shape != expected_shape:
                    raise ImageExportBatchError(
                        f"sample shape {sample.data.shape} does not match recipe {expected_shape}"
                    )
                result = execute_processing_recipe(sample, background_frame, recipe)
                image_data = result.background.data
                image_mask = result.mask.excluded
                processing_audit = (
                    result.audit if export.include_processing_audit else None
                )
            else:
                image_data = sample.data
                # Detector gap/sentinel flags are intrinsic invalid pixels,
                # independent of optional scientific/user MASK processing.
                image_mask = sample.metadata.detector_mask
                processing_audit = (
                    {
                        "stage": "raw_array",
                        "source_identity": item["source"],
                    }
                    if export.include_processing_audit
                    else None
                )
            image_path = run_root / f"{stem}_2d{suffix}"
            exported = export_2d_image(
                image_data,
                image_path,
                display,
                mask=image_mask,
                width_mm=export.image_width_mm,
                height_mm=export.image_height_mm,
                dpi=export.image_dpi,
                include_axes=export.detector_include_axes,
                include_colorbar=export.detector_include_colorbar,
                font_family=export.detector_font_family,
                label_font_size_pt=export.detector_label_font_size_pt,
                tick_font_size_pt=export.detector_tick_font_size_pt,
                line_width_pt=export.detector_line_width_pt,
                axis_mode=export.detector_axis_mode,
                geometry=geometry if processed_mode else None,
                source_path=source_path,
                processing_audit=processing_audit,
                write_parameter_file=False,
                protected_roots=protected_roots,
                # Resume may revisit a failed/incomplete item whose deterministic
                # output file exists but whose audit pair is absent.  Intact
                # passed pairs were skipped above, so replacement is confined to
                # items that actually need recovery inside this run directory.
                overwrite=resume,
            )
            item.update(
                status="passed",
                image_file=exported.image_path.relative_to(run_root).as_posix(),
                output_sha256=exported.sha256,
                resolved_limits=exported.parameter_document["resolved_limits"],
                array={
                    "shape_row_column": exported.parameter_document[
                        "array_shape_row_column"
                    ],
                    "dtype": exported.parameter_document["array_dtype"],
                    "masked_pixel_count": exported.parameter_document[
                        "masked_pixel_count"
                    ],
                },
                render=exported.parameter_document["render"],
            )
        except Exception as exc:  # per-file isolation is part of the contract
            item.update(status="failed", message=f"{type(exc).__name__}: {exc}")
        finally:
            item["finished_at"] = _utc_now()
            _save_manifest(manifest_path, document)
            if progress_callback is not None:
                progress_callback(dict(document["summary"]))

    summary = _summary(items)
    document["state"] = "completed_with_failures" if summary["failed"] else "completed"
    document["finished_at"] = _utc_now()
    _save_manifest(manifest_path, document)
    if progress_callback is not None:
        progress_callback(dict(document["summary"]))
    return ImageExportBatchResult(manifest_path, document["state"], document["summary"])
