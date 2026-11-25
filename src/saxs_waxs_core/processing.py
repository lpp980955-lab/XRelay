"""Versioned processing recipes and the shared preview/batch execution path.

This project wrapper is intentionally broader than pyFAI ``WorkerConfig``:
air-background semantics, canonical geometry, portable manual ROIs and stage
audits are project concerns.  Numerical integration still delegates to the
already-audited integration module.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .background import (
    BackgroundError,
    BackgroundMode,
    BackgroundRecipe,
    BackgroundResult,
    apply_background,
)
from .geometry import (
    DetectorGeometry,
    GeometryConfigError,
    geometry_from_document,
    geometry_to_document,
)
from .integration import (
    AzimuthSector,
    AzimuthalIntegrationRecipe,
    IntegrationCurve,
    IntegrationError,
    RadialIntegrationRecipe,
    integrate_azimuthal_1d,
    integrate_radial_1d,
)
from .masking import (
    MaskError,
    MaskRecipe,
    MaskResult,
    build_mask,
    load_external_mask_layer,
    user_mask_layers_from_descriptors,
)
from .models import ImageFrame


PROCESSING_RECIPE_SCHEMA_V1 = "saxs_waxs_processing_recipe/1"
PROCESSING_RECIPE_SCHEMA_V2 = "saxs_waxs_processing_recipe/2"
PROCESSING_RECIPE_SCHEMA_V3 = "saxs_waxs_processing_recipe/3"
PROCESSING_RECIPE_SCHEMA = "saxs_waxs_processing_recipe/4"
PROCESSING_AUDIT_SCHEMA = "saxs_waxs_processing_audit/1"
BATCH_PROCESSING_PLAN_SCHEMA_V1 = "saxs_waxs_batch_processing_plan/1"
BATCH_PROCESSING_PLAN_SCHEMA = "saxs_waxs_batch_processing_plan/2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProcessingError(ValueError):
    """Raised when a recipe cannot be represented or executed exactly."""


class ParameterMode(str, Enum):
    """Whether a batch replays values or only records adaptation suggestions."""

    LOCKED = "locked"
    ADAPTIVE_REVIEW = "adaptive_review"


class RegionCoordinateSpace(str, Enum):
    PHYSICAL_ABSOLUTE = "physical_absolute"
    PIXEL = "pixel"
    RELATIVE = "relative"


@dataclass(frozen=True, slots=True)
class ProcessingPolicy:
    """Non-numerical execution policy kept separate from integration settings."""

    parameter_mode: ParameterMode | str = ParameterMode.LOCKED
    adaptive_behavior: str = "suggest_only"
    integration_region_space: RegionCoordinateSpace | str = (
        RegionCoordinateSpace.PHYSICAL_ABSOLUTE
    )
    mask_region_space: RegionCoordinateSpace | str = RegionCoordinateSpace.PIXEL
    relative_region_use: str = "preview_only"
    structural_qc_enabled: bool = True
    scientific_qc_enabled: bool = False
    scientific_qc_action: str = "needs_review_only"

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "parameter_mode", ParameterMode(self.parameter_mode))
            object.__setattr__(
                self,
                "integration_region_space",
                RegionCoordinateSpace(self.integration_region_space),
            )
            object.__setattr__(
                self, "mask_region_space", RegionCoordinateSpace(self.mask_region_space)
            )
        except ValueError as exc:
            raise ProcessingError(f"invalid processing policy enum: {exc}") from exc
        if self.adaptive_behavior != "suggest_only":
            raise ProcessingError("adaptive behavior must remain suggest_only")
        if self.relative_region_use != "preview_only":
            raise ProcessingError("relative regions are currently preview_only")
        if self.scientific_qc_action != "needs_review_only":
            raise ProcessingError("scientific QC action must remain needs_review_only")
        if not self.structural_qc_enabled:
            raise ProcessingError("structural safety QC cannot be disabled")


@dataclass(frozen=True, slots=True)
class FileIdentity:
    path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ProcessingError("file identity path must be a non-empty string")
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool):
            raise ProcessingError("file identity size_bytes must be an integer")
        if self.size_bytes < 0:
            raise ProcessingError("file identity size_bytes must be non-negative")
        digest = str(self.sha256).lower()
        if _SHA256.fullmatch(digest) is None:
            raise ProcessingError("file identity sha256 must contain 64 hex digits")
        object.__setattr__(self, "sha256", digest)

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> "FileIdentity":
        source = Path(path).expanduser().absolute()
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
                digest.update(block)
        return cls(str(source), size, digest.hexdigest())


@dataclass(frozen=True, slots=True)
class ProcessingRecipe:
    geometry: DetectorGeometry
    background: BackgroundRecipe
    mask: MaskRecipe
    integration: RadialIntegrationRecipe | AzimuthalIntegrationRecipe
    background_source: FileIdentity | None = None
    external_mask_source: FileIdentity | None = None
    manual_mask_descriptors: Sequence[Mapping[str, Any]] = ()
    policy: ProcessingPolicy = field(default_factory=ProcessingPolicy)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mask.user_layers:
            raise ProcessingError(
                "processing recipes store portable manual_mask_descriptors, not raster arrays"
            )
        if self.background.mode is BackgroundMode.AUTO_AIR:
            if self.background_source is None:
                raise ProcessingError("auto_air processing recipe requires background_source")
        elif self.background_source is not None:
            raise ProcessingError(
                "background_source is only valid for the auto_air background mode"
            )
        if self.external_mask_source is not None and not isinstance(
            self.external_mask_source, FileIdentity
        ):
            raise ProcessingError("external_mask_source must be a FileIdentity")
        if not isinstance(self.policy, ProcessingPolicy):
            raise ProcessingError("policy must be a ProcessingPolicy")
        expected_unit = (
            f"{self.background.input_unit}/s"
            if self.background.mode is BackgroundMode.AUTO_AIR
            else self.background.input_unit
        )
        if self.integration.input_unit != expected_unit:
            raise ProcessingError(
                "integration input_unit does not match background output: "
                f"{self.integration.input_unit!r} != {expected_unit!r}"
            )
        descriptors = tuple(_normalize_mask_descriptor(item) for item in self.manual_mask_descriptors)
        # Validation uses the same rasterizer as execution and the interactive viewer.
        user_mask_layers_from_descriptors(
            (self.geometry.image_height_px, self.geometry.image_width_px), descriptors
        )
        object.__setattr__(self, "manual_mask_descriptors", descriptors)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    background: BackgroundResult
    mask: MaskResult
    integration: IntegrationCurve
    audit: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "audit", MappingProxyType(dict(self.audit)))


@dataclass(frozen=True, slots=True)
class BatchRecipeRevision:
    """An immutable recipe version that can be assigned to selected samples."""

    revision_id: str
    recipe: ProcessingRecipe
    reason: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"r[0-9]{3,}", self.revision_id) is None:
            raise ProcessingError("recipe revision id must use rNNN form")
        if not isinstance(self.recipe, ProcessingRecipe):
            raise ProcessingError("recipe revision requires a ProcessingRecipe")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ProcessingError("recipe revision reason must be non-empty")


@dataclass(frozen=True, slots=True)
class BatchProcessingPlan:
    """One reviewed processing recipe bound to an explicit sample set.

    The background identity remains inside ``recipe``.  Sample identities are
    deliberately explicit so a calibrant or unrelated frame in the same folder
    cannot enter a batch merely because of its filename.
    """

    recipe: ProcessingRecipe
    sample_sources: Sequence[FileIdentity]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    recipe_revisions: Sequence[BatchRecipeRevision] = ()
    recipe_assignment: Sequence[str] = ()

    def __post_init__(self) -> None:
        samples = tuple(self.sample_sources)
        if not samples:
            raise ProcessingError("batch processing plan requires at least one sample")
        selections=self.metadata.get('source_selections',[{} for item in samples])
        if len(selections)!=len(samples) or any(not isinstance(s,dict) or set(s)-{'dataset','frame_index'} for s in selections):
            raise ProcessingError('source_selections must contain one dataset/frame selector per sample')
        path_keys = {
            (str(Path(item.path).expanduser().absolute().resolve(strict=False)).casefold(),json.dumps(selections[i],sort_keys=True))
            for i,item in enumerate(samples)
        }
        if len(path_keys) != len(samples):
            raise ProcessingError("batch processing plan contains duplicate sample paths")
        background = self.recipe.background_source
        content_keys = {(item.size_bytes, item.sha256) for item in samples}
        if background is not None and (
            background.size_bytes,
            background.sha256,
        ) in content_keys:
            raise ProcessingError("air background cannot also be a batch sample")
        revisions = tuple(self.recipe_revisions)
        revision_ids = [item.revision_id for item in revisions]
        if len(set(revision_ids)) != len(revision_ids):
            raise ProcessingError("batch processing plan contains duplicate revision ids")
        for revision in revisions:
            if not isinstance(revision, BatchRecipeRevision):
                raise ProcessingError("recipe_revisions must contain BatchRecipeRevision values")
            if revision.recipe.background != self.recipe.background or (
                revision.recipe.background_source != self.recipe.background_source
            ):
                raise ProcessingError(
                    "recipe revisions must keep the reviewed background recipe and identity"
                )
            base_shape = (
                self.recipe.geometry.image_height_px,
                self.recipe.geometry.image_width_px,
            )
            revision_shape = (
                revision.recipe.geometry.image_height_px,
                revision.recipe.geometry.image_width_px,
            )
            if revision_shape != base_shape:
                raise ProcessingError("recipe revisions must keep the reviewed image shape")
        assignments = tuple(self.recipe_assignment) or tuple("base" for _ in samples)
        if len(assignments) != len(samples):
            raise ProcessingError("recipe assignment must contain one id per sample")
        unknown = sorted(set(assignments) - ({"base"} | set(revision_ids)))
        if unknown:
            raise ProcessingError(f"recipe assignment contains unknown ids: {unknown}")
        object.__setattr__(self, "sample_sources", samples)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "recipe_revisions", revisions)
        object.__setattr__(self, "recipe_assignment", assignments)

    def revision_id_for_index(self, index: int) -> str:
        return self.recipe_assignment[index]

    def recipe_for_index(self, index: int) -> ProcessingRecipe:
        revision_id = self.revision_id_for_index(index)
        if revision_id == "base":
            return self.recipe
        for revision in self.recipe_revisions:
            if revision.revision_id == revision_id:
                return revision.recipe
        raise ProcessingError(f"recipe revision is missing: {revision_id}")


def add_batch_recipe_revision(
    plan: BatchProcessingPlan,
    recipe: ProcessingRecipe,
    sample_indices: Sequence[int],
    *,
    reason: str,
) -> BatchProcessingPlan:
    """Return a new plan with a new immutable recipe assigned to explicit indices."""

    indices = tuple(dict.fromkeys(int(value) for value in sample_indices))
    if not indices:
        raise ProcessingError("recipe revision requires at least one sample index")
    if min(indices) < 0 or max(indices) >= len(plan.sample_sources):
        raise ProcessingError("recipe revision sample index is outside the plan")
    next_number = max(
        [int(item.revision_id[1:]) for item in plan.recipe_revisions] or [0]
    ) + 1
    revision = BatchRecipeRevision(f"r{next_number:03d}", recipe, reason)
    assignments = list(plan.recipe_assignment)
    for index in indices:
        assignments[index] = revision.revision_id
    return BatchProcessingPlan(
        recipe=plan.recipe,
        sample_sources=plan.sample_sources,
        metadata=plan.metadata,
        recipe_revisions=plan.recipe_revisions + (revision,),
        recipe_assignment=tuple(assignments),
    )


def _pair(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ProcessingError(f"{label} must contain exactly two values")
    result = (float(value[0]), float(value[1]))
    if not all(map(lambda item: item == item and abs(item) != float("inf"), result)):
        raise ProcessingError(f"{label} values must be finite")
    return result


def _normalize_mask_descriptor(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProcessingError("manual mask descriptor must be an object")
    kind = str(value.get("kind", ""))
    if kind in {"circle", "rectangle", "occlusion_rectangle"}:
        if set(value) != {"kind", "pos", "size"}:
            raise ProcessingError(f"{kind} descriptor fields must be kind, pos and size")
        return MappingProxyType(
            {"kind": kind, "pos": _pair(value["pos"], "pos"), "size": _pair(value["size"], "size")}
        )
    if kind in {"polygon", "occlusion_polygon"}:
        if set(value) != {"kind", "points"}:
            raise ProcessingError(f"{kind} descriptor fields must be kind and points")
        if not isinstance(value["points"], (list, tuple)):
            raise ProcessingError("polygon points must be a sequence")
        points = tuple(_pair(point, "polygon point") for point in value["points"])
        return MappingProxyType({"kind": kind, "points": points})
    raise ProcessingError(f"unsupported manual mask descriptor kind: {kind!r}")


def _background_document(recipe: BackgroundRecipe) -> dict[str, Any]:
    return {
        "mode": recipe.mode.value,
        "input_unit": recipe.input_unit,
        "sample_exposure_override_s": recipe.sample_exposure_override_s,
        "background_exposure_override_s": recipe.background_exposure_override_s,
        "notes": recipe.notes,
        "metadata": dict(recipe.metadata),
    }


def _mask_document(recipe: ProcessingRecipe) -> dict[str, Any]:
    source = recipe.external_mask_source
    return {
        "mask_nonfinite": recipe.mask.mask_nonfinite,
        "mask_tvx_negative_sentinels": recipe.mask.mask_tvx_negative_sentinels,
        "mask_count_cutoff": recipe.mask.mask_count_cutoff,
        "notes": recipe.mask.notes,
        "external_mask_source": None
        if source is None
        else {"path": source.path, "size_bytes": source.size_bytes, "sha256": source.sha256},
        "manual_regions": [dict(item) for item in recipe.manual_mask_descriptors],
    }


def _integration_document(
    recipe: RadialIntegrationRecipe | AzimuthalIntegrationRecipe,
) -> dict[str, Any]:
    common = {
        "pixel_splitting": recipe.pixel_splitting.value,
        "correct_solid_angle": recipe.correct_solid_angle,
        "polarization_factor": recipe.polarization_factor,
        "normalization_factor": recipe.normalization_factor,
        "input_unit": recipe.input_unit,
        "metadata": dict(recipe.metadata),
    }
    if isinstance(recipe, RadialIntegrationRecipe):
        return {
            "kind": "radial",
            "npt": recipe.npt,
            "unit": recipe.unit.value,
            "radial_range": None if recipe.radial_range is None else list(recipe.radial_range),
            "sectors": [
                {"center_deg": sector.center_deg, "width_deg": sector.width_deg}
                for sector in recipe.sectors
            ],
            **common,
        }
    return {
        "kind": "azimuthal",
        "radial_range": list(recipe.radial_range),
        "radial_unit": recipe.radial_unit.value,
        "npt_azimuth": recipe.npt_azimuth,
        "npt_radial": recipe.npt_radial,
        **common,
    }


def _policy_document(policy: ProcessingPolicy) -> dict[str, Any]:
    return {
        "parameter_mode": policy.parameter_mode.value,
        "adaptive_behavior": policy.adaptive_behavior,
        "integration_region_space": policy.integration_region_space.value,
        "mask_region_space": policy.mask_region_space.value,
        "relative_region_use": policy.relative_region_use,
        "structural_qc_enabled": policy.structural_qc_enabled,
        "scientific_qc_enabled": policy.scientific_qc_enabled,
        "scientific_qc_action": policy.scientific_qc_action,
    }


def processing_recipe_to_document(recipe: ProcessingRecipe) -> dict[str, Any]:
    source = recipe.background_source
    return {
        "schema": PROCESSING_RECIPE_SCHEMA,
        "geometry": geometry_to_document(recipe.geometry),
        "background": _background_document(recipe.background),
        "background_source": None
        if source is None
        else {"path": source.path, "size_bytes": source.size_bytes, "sha256": source.sha256},
        "mask": _mask_document(recipe),
        "integration": _integration_document(recipe.integration),
        "policy": _policy_document(recipe.policy),
        "metadata": dict(recipe.metadata),
    }


def processing_recipe_parameter_summary(recipe: ProcessingRecipe) -> dict[str, Any]:
    """Return one human-readable parameter set for an export explanation file.

    The immutable recipe document remains the machine-readable source of truth;
    this summary repeats the manuscript-facing quantities in convenient units so
    users do not have to convert SI values by hand.
    """

    geometry = recipe.geometry
    return {
        "geometry": {
            "beam_center_px": {
                "x": geometry.beam_center_x_px,
                "y": geometry.beam_center_y_px,
            },
            "sample_detector_distance_mm": geometry.detector_distance_m * 1e3,
            "wavelength_nm": geometry.wavelength_m * 1e9,
            "pixel_size_um": {
                "x": geometry.pixel_size_x_m * 1e6,
                "y": geometry.pixel_size_y_m * 1e6,
            },
            "image_size_px": {
                "width": geometry.image_width_px,
                "height": geometry.image_height_px,
            },
            "detector_tilt_rad": {
                "rot1": geometry.rot1_rad,
                "rot2": geometry.rot2_rad,
                "rot3": geometry.rot3_rad,
            },
        },
        "background": _background_document(recipe.background),
        "background_source": None
        if recipe.background_source is None
        else {
            "path": recipe.background_source.path,
            "size_bytes": recipe.background_source.size_bytes,
            "sha256": recipe.background_source.sha256,
        },
        "mask": _mask_document(recipe),
        "integration": _integration_document(recipe.integration),
        "policy": _policy_document(recipe.policy),
    }


def processing_recipe_to_json(recipe: ProcessingRecipe, *, indent: int = 2) -> str:
    return json.dumps(
        processing_recipe_to_document(recipe), ensure_ascii=False, indent=indent
    ) + "\n"


def batch_processing_plan_to_document(plan: BatchProcessingPlan) -> dict[str, Any]:
    return {
        "schema": BATCH_PROCESSING_PLAN_SCHEMA,
        "recipe": processing_recipe_to_document(plan.recipe),
        "sample_sources": [
            {"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256}
            for item in plan.sample_sources
        ],
        "metadata": dict(plan.metadata),
        "recipe_revisions": [
            {
                "revision_id": item.revision_id,
                "reason": item.reason,
                "recipe": processing_recipe_to_document(item.recipe),
            }
            for item in plan.recipe_revisions
        ],
        "recipe_assignment": list(plan.recipe_assignment),
    }


def batch_processing_plan_to_json(
    plan: BatchProcessingPlan, *, indent: int = 2
) -> str:
    return json.dumps(
        batch_processing_plan_to_document(plan), ensure_ascii=False, indent=indent
    ) + "\n"


def _require_fields(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        actual = sorted(value) if isinstance(value, Mapping) else type(value).__name__
        raise ProcessingError(
            f"{label} fields differ: expected {sorted(expected)}, got {actual}"
        )
    return value


def _integration_from_document(value: Any) -> RadialIntegrationRecipe | AzimuthalIntegrationRecipe:
    if not isinstance(value, Mapping):
        raise ProcessingError("integration must be an object")
    kind = value.get("kind")
    common_fields = {
        "pixel_splitting",
        "correct_solid_angle",
        "polarization_factor",
        "normalization_factor",
        "input_unit",
        "metadata",
    }
    try:
        if kind == "radial":
            document = _require_fields(
                value,
                {"kind", "npt", "unit", "radial_range", "sectors"} | common_fields,
                "radial integration",
            )
            sectors = tuple(
                AzimuthSector(**dict(_require_fields(item, {"center_deg", "width_deg"}, "sector")))
                for item in document["sectors"]
            )
            radial_range = document["radial_range"]
            return RadialIntegrationRecipe(
                npt=document["npt"],
                unit=document["unit"],
                radial_range=None if radial_range is None else tuple(radial_range),
                sectors=sectors,
                pixel_splitting=document["pixel_splitting"],
                correct_solid_angle=document["correct_solid_angle"],
                polarization_factor=document["polarization_factor"],
                normalization_factor=document["normalization_factor"],
                input_unit=document["input_unit"],
                metadata=document["metadata"],
            )
        if kind == "azimuthal":
            document = _require_fields(
                value,
                {"kind", "radial_range", "radial_unit", "npt_azimuth", "npt_radial"}
                | common_fields,
                "azimuthal integration",
            )
            return AzimuthalIntegrationRecipe(
                radial_range=tuple(document["radial_range"]),
                radial_unit=document["radial_unit"],
                npt_azimuth=document["npt_azimuth"],
                npt_radial=document["npt_radial"],
                pixel_splitting=document["pixel_splitting"],
                correct_solid_angle=document["correct_solid_angle"],
                polarization_factor=document["polarization_factor"],
                normalization_factor=document["normalization_factor"],
                input_unit=document["input_unit"],
                metadata=document["metadata"],
            )
    except (TypeError, ValueError, IntegrationError) as exc:
        raise ProcessingError(f"invalid integration recipe: {exc}") from exc
    raise ProcessingError(f"unsupported integration kind: {kind!r}")


def processing_recipe_from_document(document: Mapping[str, Any]) -> ProcessingRecipe:
    if not isinstance(document, Mapping):
        raise ProcessingError("processing recipe must be an object")
    schema = document.get("schema")
    if schema not in {
        PROCESSING_RECIPE_SCHEMA_V1,
        PROCESSING_RECIPE_SCHEMA_V2,
        PROCESSING_RECIPE_SCHEMA_V3,
        PROCESSING_RECIPE_SCHEMA,
    }:
        raise ProcessingError(f"unsupported processing recipe schema: {schema!r}")
    expected_root = {
        "schema",
        "geometry",
        "background",
        "background_source",
        "mask",
        "integration",
        "metadata",
    }
    if schema == PROCESSING_RECIPE_SCHEMA:
        expected_root.add("policy")
    root = _require_fields(document, expected_root, "processing recipe")
    background = _require_fields(
        root["background"],
        {
            "mode",
            "input_unit",
            "sample_exposure_override_s",
            "background_exposure_override_s",
            "notes",
            "metadata",
        },
        "background",
    )
    mask_fields = {
        "mask_nonfinite",
        "mask_tvx_negative_sentinels",
        "mask_count_cutoff",
        "notes",
        "manual_regions",
    }
    if root["schema"] in {
        PROCESSING_RECIPE_SCHEMA_V2,
        PROCESSING_RECIPE_SCHEMA_V3,
        PROCESSING_RECIPE_SCHEMA,
    }:
        mask_fields.add("external_mask_source")
    mask = _require_fields(root["mask"], mask_fields, "mask")
    if root["schema"] in {PROCESSING_RECIPE_SCHEMA_V1, PROCESSING_RECIPE_SCHEMA_V2}:
        legacy_regions = mask["manual_regions"]
        for descriptor in (
            legacy_regions if isinstance(legacy_regions, (list, tuple)) else ()
        ):
            if isinstance(descriptor, Mapping) and str(
                descriptor.get("kind", "")
            ).startswith("occlusion_"):
                raise ProcessingError(
                    "semantic linked-occlusion regions require processing recipe schema v3"
                )
    if root["schema"] == PROCESSING_RECIPE_SCHEMA:
        policy_document = _require_fields(
            root["policy"],
            {
                "parameter_mode",
                "adaptive_behavior",
                "integration_region_space",
                "mask_region_space",
                "relative_region_use",
                "structural_qc_enabled",
                "scientific_qc_enabled",
                "scientific_qc_action",
            },
            "processing policy",
        )
        policy = ProcessingPolicy(**dict(policy_document))
    else:
        policy = ProcessingPolicy()
    source_value = root["background_source"]
    source = None
    if source_value is not None:
        source = FileIdentity(
            **dict(
                _require_fields(
                    source_value, {"path", "size_bytes", "sha256"}, "background_source"
                )
            )
        )
    external_source = None
    external_source_value = mask.get("external_mask_source")
    if external_source_value is not None:
        external_source = FileIdentity(
            **dict(
                _require_fields(
                    external_source_value,
                    {"path", "size_bytes", "sha256"},
                    "external_mask_source",
                )
            )
        )
    try:
        return ProcessingRecipe(
            geometry=geometry_from_document(root["geometry"]),
            background=BackgroundRecipe(**dict(background)),
            background_source=source,
            external_mask_source=external_source,
            mask=MaskRecipe(
                mask_nonfinite=mask["mask_nonfinite"],
                mask_tvx_negative_sentinels=mask["mask_tvx_negative_sentinels"],
                mask_count_cutoff=mask["mask_count_cutoff"],
                notes=mask["notes"],
            ),
            manual_mask_descriptors=mask["manual_regions"],
            integration=_integration_from_document(root["integration"]),
            policy=policy,
            metadata=root["metadata"],
        )
    except (TypeError, ValueError, BackgroundError, MaskError, GeometryConfigError) as exc:
        raise ProcessingError(f"invalid processing recipe: {exc}") from exc


def processing_recipe_from_json(payload: str) -> ProcessingRecipe:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProcessingError(f"invalid processing recipe JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ProcessingError("processing recipe JSON root must be an object")
    return processing_recipe_from_document(document)


def batch_processing_plan_from_document(
    document: Mapping[str, Any],
) -> BatchProcessingPlan:
    if not isinstance(document, Mapping):
        raise ProcessingError("batch processing plan must be an object")
    schema = document.get("schema")
    if schema not in {BATCH_PROCESSING_PLAN_SCHEMA_V1, BATCH_PROCESSING_PLAN_SCHEMA}:
        raise ProcessingError(
            f"unsupported batch processing plan schema: {schema!r}"
        )
    expected = {"schema", "recipe", "sample_sources", "metadata"}
    if schema == BATCH_PROCESSING_PLAN_SCHEMA:
        expected |= {"recipe_revisions", "recipe_assignment"}
    root = _require_fields(document, expected, "batch processing plan")
    if not isinstance(root["sample_sources"], list):
        raise ProcessingError("sample_sources must be an array")
    samples = tuple(
        FileIdentity(
            **dict(_require_fields(item, {"path", "size_bytes", "sha256"}, "sample source"))
        )
        for item in root["sample_sources"]
    )
    revisions: tuple[BatchRecipeRevision, ...] = ()
    assignments: Sequence[str] = ()
    if schema == BATCH_PROCESSING_PLAN_SCHEMA:
        if not isinstance(root["recipe_revisions"], list):
            raise ProcessingError("recipe_revisions must be an array")
        revisions = tuple(
            BatchRecipeRevision(
                revision_id=revision["revision_id"],
                reason=revision["reason"],
                recipe=processing_recipe_from_document(revision["recipe"]),
            )
            for value in root["recipe_revisions"]
            for revision in (
                _require_fields(
                    value,
                    {"revision_id", "reason", "recipe"},
                    "recipe revision",
                ),
            )
        )
        if not isinstance(root["recipe_assignment"], list):
            raise ProcessingError("recipe_assignment must be an array")
        assignments = tuple(str(value) for value in root["recipe_assignment"])
    return BatchProcessingPlan(
        recipe=processing_recipe_from_document(root["recipe"]),
        sample_sources=samples,
        metadata=root["metadata"],
        recipe_revisions=revisions,
        recipe_assignment=assignments,
    )


def batch_processing_plan_from_json(payload: str) -> BatchProcessingPlan:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProcessingError(f"invalid batch processing plan JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ProcessingError("batch processing plan JSON root must be an object")
    return batch_processing_plan_from_document(document)


def load_processing_recipe(path: str | os.PathLike[str]) -> ProcessingRecipe:
    return processing_recipe_from_json(
        Path(path).expanduser().absolute().read_text(encoding="utf-8")
    )


def load_batch_processing_plan(path: str | os.PathLike[str]) -> BatchProcessingPlan:
    return batch_processing_plan_from_json(
        Path(path).expanduser().absolute().read_text(encoding="utf-8")
    )


def _atomic_save_json(
    payload: str,
    path: str | os.PathLike[str],
    *,
    protected_roots: tuple[str | os.PathLike[str], ...],
    overwrite: bool,
) -> Path:
    destination = Path(path).expanduser().absolute()
    resolved_destination = destination.resolve(strict=False)
    for protected in protected_roots:
        protected_path = Path(protected).expanduser().absolute().resolve(strict=False)
        if resolved_destination == protected_path or protected_path in resolved_destination.parents:
            raise ProcessingError(f"refusing to write inside protected root: {protected_path}")
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def save_processing_recipe(
    recipe: ProcessingRecipe,
    path: str | os.PathLike[str],
    *,
    protected_roots: tuple[str | os.PathLike[str], ...],
    overwrite: bool = False,
) -> Path:
    return _atomic_save_json(
        processing_recipe_to_json(recipe),
        path,
        protected_roots=protected_roots,
        overwrite=overwrite,
    )


def save_batch_processing_plan(
    plan: BatchProcessingPlan,
    path: str | os.PathLike[str],
    *,
    protected_roots: tuple[str | os.PathLike[str], ...],
    overwrite: bool = False,
) -> Path:
    return _atomic_save_json(
        batch_processing_plan_to_json(plan),
        path,
        protected_roots=protected_roots,
        overwrite=overwrite,
    )


def _verify_background_identity(frame: ImageFrame, identity: FileIdentity) -> None:
    path = frame.metadata.source_path.expanduser().absolute()
    if not path.is_file():
        raise ProcessingError(f"background source cannot be verified: {path}")
    actual = FileIdentity.from_path(path)
    if actual.size_bytes != identity.size_bytes or actual.sha256 != identity.sha256:
        raise ProcessingError("background source identity does not match the saved recipe")


def execute_integration_stage(
    sample: ImageFrame,
    geometry: DetectorGeometry,
    background: BackgroundResult,
    mask_recipe: MaskRecipe,
    integration_recipe: RadialIntegrationRecipe | AzimuthalIntegrationRecipe,
    *,
    workspace=None,
) -> ProcessingResult:
    """Shared numerical path used by GUI preview and future batch execution."""

    if background.output_unit != integration_recipe.input_unit:
        raise ProcessingError(
            "integration input_unit does not match the supplied background result"
        )
    try:
        mask = build_mask(sample, mask_recipe)
        if workspace is not None:
            curve=workspace.integrate(background.data,geometry,integration_recipe,mask.excluded)
        elif isinstance(integration_recipe, RadialIntegrationRecipe):
            curve = integrate_radial_1d(
                background.data, geometry, integration_recipe, mask=mask.excluded
            )
        else:
            curve = integrate_azimuthal_1d(
                background.data, geometry, integration_recipe, mask=mask.excluded
            )
    except (MaskError, IntegrationError) as exc:
        raise ProcessingError(f"integration stage failed: {exc}") from exc
    audit = {
        "schema": PROCESSING_AUDIT_SCHEMA,
        "stage_order": ["background", "mask_from_raw_frame", "integration"],
        "source_selection": {'dataset':sample.metadata.dataset_path,'frame_index':sample.metadata.frame_index,
                             'frame_count':sample.metadata.frame_count},
        "background": dict(background.audit),
        "mask": dict(mask.audit),
        "integration": dict(curve.audit),
    }
    return ProcessingResult(background, mask, curve, audit)


def execute_processing_recipe(
    sample: ImageFrame,
    background: ImageFrame | None,
    recipe: ProcessingRecipe,
) -> ProcessingResult:
    """Execute one immutable recipe without modifying either source frame."""

    if recipe.background.mode is BackgroundMode.AUTO_AIR:
        if background is None or recipe.background_source is None:
            raise ProcessingError("auto_air recipe requires the identified background frame")
        _verify_background_identity(background, recipe.background_source)
        selection=recipe.background.metadata.get('source_selection',{})
        if selection and (selection.get('dataset')!=background.metadata.dataset_path
                or selection.get('frame_index',0)!=background.metadata.frame_index):
            raise ProcessingError('Air dataset/frame differs from the saved background selection')
    try:
        background_result = apply_background(sample, background, recipe.background)
        user_layers = []
        if recipe.external_mask_source is not None:
            identity = recipe.external_mask_source
            user_layers.append(
                load_external_mask_layer(
                    identity.path,
                    sample.data.shape,
                    expected_size_bytes=identity.size_bytes,
                    expected_sha256=identity.sha256,
                )
            )
        user_layers.extend(
            user_mask_layers_from_descriptors(
                sample.data.shape, recipe.manual_mask_descriptors
            )
        )
        mask_recipe = MaskRecipe(
            mask_nonfinite=recipe.mask.mask_nonfinite,
            mask_tvx_negative_sentinels=recipe.mask.mask_tvx_negative_sentinels,
            mask_count_cutoff=recipe.mask.mask_count_cutoff,
            user_layers=tuple(user_layers),
            notes=recipe.mask.notes,
        )
        integration_recipe = recipe.integration
        if sample.metadata.embedded_processing or sample.metadata.intensity_unit is not None or background_result.audit.get("formula_version") == "air_background_equal_exposure_counts/1":
            integration_recipe = replace(integration_recipe, input_unit=background_result.output_unit)
        result = execute_integration_stage(
            sample,
            processing_geometry(recipe,sample),
            background_result,
            mask_recipe,
            integration_recipe,
        )
        audit = dict(result.audit)
        audit["policy"] = _policy_document(recipe.policy)
        return ProcessingResult(
            result.background,
            result.mask,
            result.integration,
            audit,
        )
    except (BackgroundError, MaskError) as exc:
        raise ProcessingError(f"processing recipe stage failed: {exc}") from exc


def processing_geometry(recipe,sample):
    if recipe.metadata.get('prefer_embedded_geometry'):
        from .processed_data import embedded_geometry
        saved=embedded_geometry(sample)
        if saved is not None:return saved
    return recipe.geometry
