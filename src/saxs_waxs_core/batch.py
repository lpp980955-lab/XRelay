"""Recoverable sequential batch execution for reviewed processing plans.

The numerical work stays in :mod:`saxs_waxs_core.processing`.  This module adds
identity preflight, per-file failure isolation, atomic outputs and a resumable
manifest without introducing a second integration implementation.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Mapping

import numpy as np

from .background import BackgroundMode
from .integration import AzimuthalIntegrationRecipe, IntegrationCurve
from .processing import (
    BatchProcessingPlan,
    FileIdentity,
    ProcessingError,
    batch_processing_plan_from_document,
    batch_processing_plan_to_json,
    execute_processing_recipe,
    processing_recipe_parameter_summary,
)
from .viewer import FrameLoader, load_supported_frame
from .readers.detector_io import load_plan_sample,selection_suffix,selection_label,load_background_source
from .processing import processing_geometry


BATCH_RUN_SCHEMA = "saxs_waxs_batch_run/2"
LEGACY_BATCH_RUN_SCHEMA = "saxs_waxs_batch_run/1"
BATCH_PREFLIGHT_SCHEMA = "saxs_waxs_batch_preflight/1"
_TRAILING_SEQUENCE = re.compile(r"^(.*?)(\d+)$")


class BatchRunError(RuntimeError):
    """Raised when a batch cannot be started or safely resumed."""


@dataclass(frozen=True, slots=True)
class BatchRunResult:
    manifest_path: Path
    state: str
    summary: Mapping[str, int]


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


def _refuse_protected_output(
    run_directory: Path, protected_roots: tuple[str | os.PathLike[str], ...]
) -> None:
    resolved = run_directory.resolve(strict=False)
    for root in protected_roots:
        protected = Path(root).expanduser().absolute().resolve(strict=False)
        if resolved == protected or protected in resolved.parents:
            raise BatchRunError(f"refusing to write batch outputs inside protected root: {protected}")


def _verify_identity(identity: FileIdentity) -> Path:
    path = Path(identity.path).expanduser().absolute()
    if not path.is_file():
        raise BatchRunError(f"source file is missing: {path}")
    actual = FileIdentity.from_path(path)
    if actual.size_bytes != identity.size_bytes or actual.sha256 != identity.sha256:
        raise BatchRunError(f"source identity changed since plan review: {path}")
    return path


def _curve_headers(curve: IntegrationCurve) -> tuple[str, str, str, str]:
    if curve.kind == "azimuthal_1d":
        x_name, x_unit = "φ", "deg"
    else:
        x_name, x_unit = {
            "q_nm^-1": ("q", "nm⁻¹"),
            "q_A^-1": ("q", "Å⁻¹"),
            "s_nm^-1": ("s", "nm⁻¹"),
            "2th_deg": ("2θ", "deg"),
        }.get(curve.x_unit, ("x", curve.x_unit))
    y_unit = {
        "detector_count": "counts",
        "detector_count/s": "counts s⁻¹",
    }.get(curve.intensity_unit, curve.intensity_unit)
    return x_name, x_unit, "Intensity", y_unit


def _write_curve_csv(path: Path, source_name: str, curve: IntegrationCurve) -> None:
    x_name, x_unit, y_name, y_unit = _curve_headers(curve)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [f"{source_name} | {x_name} (X)", f"{source_name} | {y_name} (Y)"]
            )
            writer.writerow([x_unit, y_unit])
            for x_value, y_value in zip(curve.x, curve.intensity, strict=True):
                writer.writerow(
                    [
                        float(x_value) if np.isfinite(x_value) else None,
                        float(y_value) if np.isfinite(y_value) else None,
                    ]
                )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_combined_curve_csv(path: Path, member_paths: tuple[Path, ...]) -> None:
    """Write existing two-column curve files as adjacent Origin-ready XY pairs."""

    columns: list[tuple[list[str], list[str], list[list[str]]]] = []
    for member_path in member_paths:
        with member_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        if len(rows) < 2 or len(rows[0]) != 2 or len(rows[1]) != 2:
            raise BatchRunError(
                f"cannot combine malformed curve CSV: {member_path.name}"
            )
        data_rows = []
        for row in rows[2:]:
            if len(row) < 2:
                row = [*row, *("" for _ in range(2 - len(row)))]
            data_rows.append(row[:2])
        columns.append((rows[0], rows[1], data_rows))

    if not columns:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.writer(stream)
            writer.writerow([cell for header, _, _ in columns for cell in header])
            writer.writerow([cell for _, units, _ in columns for cell in units])
            row_count = max(len(rows) for _, _, rows in columns)
            for row_index in range(row_count):
                output_row: list[str] = []
                for _, _, rows in columns:
                    output_row.extend(
                        rows[row_index] if row_index < len(rows) else ["", ""]
                    )
                writer.writerow(output_row)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_combined_outputs(
    run_root: Path, items: list[dict[str, Any]]
) -> dict[str, str]:
    groups: dict[str, list[Path]] = {
        "radial_integration": [],
        "azimuthal_integration": [],
    }
    for item in items:
        if item.get("status") not in {"passed", "needs_review"}:
            continue
        relative = item.get("data_file")
        if not isinstance(relative, str):
            continue
        for integration_name in groups:
            if relative.endswith(f"_{integration_name}.csv"):
                groups[integration_name].append(run_root / relative)
                break

    outputs: dict[str, str] = {}
    for integration_name, members in groups.items():
        if not members:
            continue
        destination = run_root / f"all_samples_{integration_name}.csv"
        _write_combined_curve_csv(destination, tuple(members))
        outputs[integration_name] = destination.relative_to(run_root).as_posix()
    return outputs


def _result_stem(identity: FileIdentity, duplicate_stems: set[str]) -> str:
    stem = Path(identity.path).stem
    if stem.casefold() in duplicate_stems:
        return f"{stem}__{identity.sha256[:8]}"
    return stem


def _item_template(identity: FileIdentity, revision_id: str) -> dict[str, Any]:
    return {
        "source": {
            "path": identity.path,
            "size_bytes": identity.size_bytes,
            "sha256": identity.sha256,
        },
        "status": "pending",
        "message": "",
        "review_reasons": [],
        "data_file": None,
        "data_sha256": None,
        "result_summary": None,
        "started_at": None,
        "finished_at": None,
        "recipe_revision": revision_id,
    }


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(items),
        "pending": sum(item["status"] == "pending" for item in items),
        "passed": sum(item["status"] == "passed" for item in items),
        "needs_review": sum(item["status"] == "needs_review" for item in items),
        "failed": sum(item["status"] == "failed" for item in items),
    }


def _save_manifest(path: Path, document: dict[str, Any]) -> None:
    document["summary"] = _summary(document["items"])
    _atomic_write_text(
        path, json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    )


def _representative_indices(count: int) -> tuple[int, ...]:
    """Return deterministic first/middle/last positions without duplication."""

    if count <= 0:
        return ()
    return tuple(sorted({0, (count - 1) // 2, count - 1}))


def _sequence_continuity(identities: tuple[FileIdentity, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[tuple[int, str]]] = {}
    for identity in identities:
        path = Path(identity.path)
        matched = _TRAILING_SEQUENCE.fullmatch(path.stem)
        if matched is None:
            continue
        prefix, index_text = matched.groups()
        key = (str(path.parent.absolute()), prefix.casefold(), path.suffix.casefold())
        groups.setdefault(key, []).append((int(index_text), path.name))
    report: list[dict[str, Any]] = []
    for (parent, prefix, suffix), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        values = [value for value, _ in members]
        unique_values = set(values)
        missing = sorted(set(range(min(values), max(values) + 1)) - unique_values)
        duplicates = sorted(value for value in unique_values if values.count(value) > 1)
        report.append(
            {
                "directory": parent,
                "prefix": prefix,
                "suffix": suffix,
                "count": len(members),
                "first_index": min(values),
                "last_index": max(values),
                "missing_indices": missing,
                "duplicate_indices": duplicates,
                "files": [name for _, name in sorted(members)],
            }
        )
    return report


def preflight_batch_plan(
    plan: BatchProcessingPlan,
    *,
    loader: FrameLoader = load_supported_frame,
    background_frame: Any | None = None,
) -> dict[str, Any]:
    """Inspect deterministic representatives and filename continuity.

    This is structural prevalidation only. It deliberately does not infer peak
    quality, signal-to-noise thresholds, drift limits or scientific anomalies.
    """

    identities = tuple(plan.sample_sources)
    representative_indices = _representative_indices(len(identities))
    issues: list[dict[str, Any]] = []
    representatives: list[dict[str, Any]] = []
    exposures: dict[str, list[str]] = {}

    for index in representative_indices:
        identity = identities[index]
        item_recipe = plan.recipe_for_index(index)
        expected_shape = (
            item_recipe.geometry.image_height_px,
            item_recipe.geometry.image_width_px,
        )
        item: dict[str, Any] = {
            "index": index,
            "source": identity.path,
            "sha256": identity.sha256,
            "recipe_revision": plan.revision_id_for_index(index),
            "readable": False,
        }
        try:
            path = _verify_identity(identity)
            frame = load_plan_sample(plan,index,loader)
            geometry=processing_geometry(item_recipe,frame)
            expected_shape=(geometry.image_height_px,geometry.image_width_px)
            metadata = frame.metadata
            item.update(
                {
                    "readable": True,
                    "shape": [int(frame.data.shape[0]), int(frame.data.shape[1])],
                    "dtype": str(frame.data.dtype),
                    "detector_model": metadata.detector_model,
                    "pixel_size_m": [metadata.pixel_size_x_m, metadata.pixel_size_y_m],
                    "exposure_time_s": metadata.exposure_time_s,
                }
            )
            if frame.data.shape != expected_shape:
                issues.append(
                    {
                        "severity": "error",
                        "code": "representative_shape_mismatch",
                        "source": str(path),
                        "message": f"shape {frame.data.shape} != recipe {expected_shape}",
                    }
                )
            if (
                plan.recipe.background.mode is BackgroundMode.AUTO_AIR
                and plan.recipe.background.sample_exposure_override_s is None
                and metadata.exposure_time_s is None
                and plan.recipe.background.metadata.get("assume_equal_exposure_if_missing") is not True
            ):
                issues.append(
                    {
                        "severity": "error",
                        "code": "representative_exposure_missing",
                        "source": str(path),
                        "message": "auto-air processing requires sample exposure metadata or an override",
                    }
                )
            exposure_key = (
                "missing"
                if metadata.exposure_time_s is None
                else format(float(metadata.exposure_time_s), ".12g")
            )
            exposures.setdefault(exposure_key, []).append(path.name)
            if background_frame is not None:
                background_metadata = background_frame.metadata
                if (
                    metadata.detector_model is not None
                    and background_metadata.detector_model is not None
                    and metadata.detector_model != background_metadata.detector_model
                ):
                    issues.append(
                        {
                            "severity": "error",
                            "code": "representative_detector_mismatch",
                            "source": str(path),
                            "message": "sample/background detector models differ",
                        }
                    )
                for axis, sample_value, background_value in (
                    ("x", metadata.pixel_size_x_m, background_metadata.pixel_size_x_m),
                    ("y", metadata.pixel_size_y_m, background_metadata.pixel_size_y_m),
                ):
                    if (
                        sample_value is not None
                        and background_value is not None
                        and not math.isclose(
                            float(sample_value),
                            float(background_value),
                            rel_tol=1e-12,
                            abs_tol=0.0,
                        )
                    ):
                        issues.append(
                            {
                                "severity": "error",
                                "code": f"representative_pixel_size_{axis}_mismatch",
                                "source": str(path),
                                "message": f"sample/background {axis} pixel sizes differ",
                            }
                        )
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            issues.append(
                {
                    "severity": "error",
                    "code": "representative_read_failed",
                    "source": identity.path,
                    "message": item["error"],
                }
            )
        representatives.append(item)

    sequences = _sequence_continuity(identities)
    for sequence in sequences:
        if sequence["missing_indices"]:
            issues.append(
                {
                    "severity": "review",
                    "code": "filename_sequence_gap",
                    "source": sequence["directory"],
                    "message": (
                        f"{sequence['prefix']} has missing indices "
                        f"{sequence['missing_indices']}"
                    ),
                }
            )
        if sequence["duplicate_indices"]:
            issues.append(
                {
                    "severity": "review",
                    "code": "filename_sequence_duplicate_index",
                    "source": sequence["directory"],
                    "message": (
                        f"{sequence['prefix']} has duplicate indices "
                        f"{sequence['duplicate_indices']}"
                    ),
                }
            )

    version_recipes = [("base", plan.recipe)] + [
        (revision.revision_id, revision.recipe)
        for revision in plan.recipe_revisions
    ]
    version_policies: list[dict[str, Any]] = []
    for revision_id, version_recipe in version_recipes:
        version_policy = version_recipe.policy
        version_policies.append(
            {
                "revision_id": revision_id,
                "assigned_count": plan.recipe_assignment.count(revision_id),
                "parameter_mode": version_policy.parameter_mode.value,
                "adaptive_behavior": version_policy.adaptive_behavior,
                "scientific_qc_enabled": version_policy.scientific_qc_enabled,
                "scientific_qc_action": version_policy.scientific_qc_action,
            }
        )
        if version_policy.parameter_mode.value == "adaptive_review":
            issues.append(
                {
                    "severity": "review",
                    "code": "adaptive_review_is_suggest_only",
                    "source": f"processing_policy:{revision_id}",
                    "message": "adaptive mode records suggestions only and does not mutate parameters",
                }
            )
        if version_policy.scientific_qc_enabled:
            issues.append(
                {
                    "severity": "review",
                    "code": "scientific_qc_rules_not_frozen",
                    "source": f"processing_policy:{revision_id}",
                    "message": "scientific QC was requested but no representative-data rules are frozen",
                }
            )

    policy = plan.recipe.policy

    status = "ready"
    if any(item["severity"] == "error" for item in issues):
        status = "review_required"
    elif issues:
        status = "review_recommended"
    return {
        "schema": BATCH_PREFLIGHT_SCHEMA,
        "scope": "structural_representatives_and_filename_continuity_only",
        "status": status,
        "sample_count": len(identities),
        "representative_indices": list(representative_indices),
        "representatives": representatives,
        "exposure_groups": exposures,
        "sequence_groups": sequences,
        "issues": issues,
        "policy": {
            "parameter_mode": policy.parameter_mode.value,
            "adaptive_behavior": policy.adaptive_behavior,
            "integration_region_space": policy.integration_region_space.value,
            "mask_region_space": policy.mask_region_space.value,
            "relative_region_use": policy.relative_region_use,
            "structural_qc_enabled": policy.structural_qc_enabled,
            "scientific_qc_enabled": policy.scientific_qc_enabled,
            "scientific_qc_action": policy.scientific_qc_action,
        },
        "recipe_version_policies": version_policies,
        "scientific_qc_applied": False,
    }


def run_batch_plan(
    plan: BatchProcessingPlan,
    run_directory: str | os.PathLike[str],
    *,
    protected_roots: tuple[str | os.PathLike[str], ...] = (),
    resume: bool = False,
    loader: FrameLoader = load_supported_frame,
    stop_requested: Callable[[], bool] | None = None,
    progress_callback: Callable[[Mapping[str, int]], None] | None = None,
) -> BatchRunResult:
    """Execute a reviewed plan sequentially and persist progress after every file.

    On resume, reuse completed items whose CSV still matches its recorded hash.
    Retry failed items and rebuild missing or damaged outputs.
    """

    run_root = Path(run_directory).expanduser().absolute()
    _refuse_protected_output(run_root, protected_roots)
    manifest_path = run_root / "batch_manifest.json"
    plan_payload = batch_processing_plan_to_json(plan)
    plan_sha256 = hashlib.sha256(plan_payload.encode("utf-8")).hexdigest()

    background_frame = None
    if plan.recipe.background.mode is BackgroundMode.AUTO_AIR:
        if plan.recipe.background_source is None:
            raise BatchRunError("auto-air batch has no background identity")
        background_path = _verify_identity(plan.recipe.background_source)
        background_frame = load_background_source(plan.recipe,loader)

    if manifest_path.exists():
        if not resume:
            raise BatchRunError("batch manifest already exists; use explicit resume")
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BatchRunError(f"cannot read existing batch manifest: {exc}") from exc
        if document.get("schema") not in {BATCH_RUN_SCHEMA, LEGACY_BATCH_RUN_SCHEMA}:
            raise BatchRunError("existing batch manifest schema is unsupported")
        if document.get("plan_sha256") != plan_sha256:
            try:
                existing_plan = batch_processing_plan_from_document(document.get("plan"))
            except (ProcessingError, TypeError, ValueError) as exc:
                raise BatchRunError(
                    "existing batch manifest contains an unreadable plan"
                ) from exc
            if existing_plan != plan:
                raise BatchRunError("existing batch manifest belongs to a different plan")
        items = document.get("items")
        if not isinstance(items, list) or len(items) != len(plan.sample_sources):
            raise BatchRunError("existing batch manifest item list does not match the plan")
        document["state"] = "running"
        document["resumed_at"] = _utc_now()
    else:
        if run_root.exists():
            if not run_root.is_dir():
                raise BatchRunError("run directory path exists but is not a directory")
            if any(run_root.iterdir()):
                raise BatchRunError("run directory is not empty and has no resumable manifest")
        run_root.mkdir(parents=True, exist_ok=True)
        items = [
            {**_item_template(identity, plan.revision_id_for_index(index)),
             "display_name": selection_label(plan,index),
             "source_selection": dict(plan.metadata.get('source_selections',[{}]*len(plan.sample_sources))[index])}
            for index, identity in enumerate(plan.sample_sources)
        ]
        preflight = preflight_batch_plan(
            plan, loader=loader, background_frame=background_frame
        )
        document = {
            "schema": BATCH_RUN_SCHEMA,
            "state": "running",
            "created_at": _utc_now(),
            "finished_at": None,
            "plan_sha256": plan_sha256,
            "plan": json.loads(plan_payload),
            "parameter_organization": {
                "description": (
                    "This batch_manifest.json is the only parameter JSON for the "
                    "operation. Shared recipe parameters are stored once; each item "
                    "contains only source identity, output identity and result-specific QC."
                ),
                "per_curve_json_written": False,
            },
            "manuscript_parameters": {
                "base": processing_recipe_parameter_summary(plan.recipe),
                "revisions": {
                    revision.revision_id: processing_recipe_parameter_summary(
                        revision.recipe
                    )
                    for revision in plan.recipe_revisions
                },
            },
            "preflight": preflight,
            "items": items,
            "summary": {},
        }
        _save_manifest(manifest_path, document)
    if progress_callback is not None:
        progress_callback(dict(document["summary"]))

    stem_counts: dict[str, int] = {}
    for identity in plan.sample_sources:
        key = Path(identity.path).stem.casefold()
        stem_counts[key] = stem_counts.get(key, 0) + 1
    duplicate_stems = {key for key, count in stem_counts.items() if count > 1}

    for index, (identity, item) in enumerate(
        zip(plan.sample_sources, items, strict=True)
    ):
        if stop_requested is not None and stop_requested():
            document["state"] = "paused"
            _save_manifest(manifest_path, document)
            if progress_callback is not None:
                progress_callback(dict(document["summary"]))
            return BatchRunResult(manifest_path, "paused", document["summary"])
        if item.get("status") in {"passed", "needs_review"}:
            data_file = item.get("data_file")
            expected_hash = item.get("data_sha256")
            if data_file and expected_hash:
                output = run_root / data_file
                try:
                    actual_hash = hashlib.sha256(output.read_bytes()).hexdigest()
                except OSError:
                    actual_hash = None
                if actual_hash == expected_hash:
                    continue
        item.update(
            {
                "status": "pending",
                "message": "",
                "review_reasons": [],
                "data_file": None,
                "data_sha256": None,
                "result_summary": None,
                "started_at": _utc_now(),
                "finished_at": None,
            }
        )
        _save_manifest(manifest_path, document)
        try:
            source_path = _verify_identity(identity)
            sample = load_plan_sample(plan,index,loader)
            item_recipe = plan.recipe_for_index(index)
            geometry=processing_geometry(item_recipe,sample)
            expected_shape = (
                geometry.image_height_px,
                geometry.image_width_px,
            )
            if sample.data.shape != expected_shape:
                raise BatchRunError(
                    f"sample shape {sample.data.shape} does not match recipe {expected_shape}"
                )
            result = execute_processing_recipe(sample, background_frame, item_recipe)
            integration_name = (
                "azimuthal_integration"
                if isinstance(item_recipe.integration, AzimuthalIntegrationRecipe)
                else "radial_integration"
            )
            stem = _result_stem(identity, duplicate_stems)+selection_suffix(plan,index)
            data_path = run_root / f"{stem}_{integration_name}.csv"
            if not resume and data_path.exists():
                raise BatchRunError(f"refusing to overwrite existing output for {source_path.name}")
            _write_curve_csv(data_path, selection_label(plan,index), result.integration)
            finite = np.isfinite(result.integration.x) & np.isfinite(
                result.integration.intensity
            )
            review_reasons = [] if np.any(finite) else ["no_finite_integration_points"]
            item_status = "needs_review" if review_reasons else "passed"
            result_summary = {
                "qc": {
                    "structural": "passed" if not review_reasons else "needs_review",
                    "scientific": "disabled"
                    if not item_recipe.policy.scientific_qc_enabled
                    else "not_evaluated_rules_not_frozen",
                    "scientific_action": item_recipe.policy.scientific_qc_action,
                },
                "curve": {
                    "kind": result.integration.kind,
                    "x_unit": result.integration.x_unit,
                    "intensity_unit": result.integration.intensity_unit,
                    "point_count": int(len(result.integration.x)),
                    "finite_point_count": int(np.count_nonzero(finite)),
                    "excluded_pixel_count": int(
                        result.integration.audit.get("excluded_pixel_count", 0)
                    ),
                },
                "runtime": {
                    "background": dict(result.background.audit),
                    "mask": dict(result.mask.audit),
                },
            }
            item.update(
                {
                    "status": item_status,
                    "display_name": selection_label(plan,index),
                    "source_selection": dict(result.audit["source_selection"]),
                    "message": "",
                    "review_reasons": review_reasons,
                    "data_file": data_path.relative_to(run_root).as_posix(),
                    "data_sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
                    "result_summary": result_summary,
                }
            )
        except Exception as exc:  # per-file isolation is the batch contract
            item.update(
                {
                    "status": "failed",
                    "message": f"{type(exc).__name__}: {exc}",
                    "review_reasons": [],
                }
            )
        finally:
            item["finished_at"] = _utc_now()
            _save_manifest(manifest_path, document)
            if progress_callback is not None:
                progress_callback(dict(document["summary"]))

    summary = _summary(items)
    document["combined_data_files"] = _write_combined_outputs(run_root, items)
    document["state"] = "completed_with_failures" if summary["failed"] else "completed"
    document["finished_at"] = _utc_now()
    _save_manifest(manifest_path, document)
    if progress_callback is not None:
        progress_callback(dict(document["summary"]))
    return BatchRunResult(manifest_path, document["state"], document["summary"])
