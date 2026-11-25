"""Read-only, content-verified relocation of project file identities.

The resolver never accepts a same-name file as proof.  It first tries the
expected relative path and verifies size plus SHA-256; otherwise it searches
the selected root for the same content identity.  Multiple content matches are
reported as ambiguous instead of being chosen silently.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Mapping, Sequence

from .processing import (
    BatchProcessingPlan,
    BatchRecipeRevision,
    FileIdentity,
    ProcessingRecipe,
)


RELOCATION_REPORT_SCHEMA = "saxs_waxs_relocation_report/1"
RelocationStatus = Literal["resolved", "missing", "ambiguous"]


class RelocationError(RuntimeError):
    """Raised when relocation input or an attempted rebind is unsafe."""


@dataclass(frozen=True, slots=True)
class RelocationItem:
    source: FileIdentity
    status: RelocationStatus
    resolved_path: Path | None
    candidates: tuple[Path, ...]
    method: str | None


@dataclass(frozen=True, slots=True)
class RelocationReport:
    search_root: Path
    items: tuple[RelocationItem, ...]

    @property
    def complete(self) -> bool:
        return all(item.status == "resolved" for item in self.items)

    @property
    def mapping(self) -> dict[str, Path]:
        return {
            _path_key(item.source.path): item.resolved_path
            for item in self.items
            if item.status == "resolved" and item.resolved_path is not None
        }

    def to_document(self) -> dict[str, object]:
        return {
            "schema": RELOCATION_REPORT_SCHEMA,
            "search_root": str(self.search_root),
            "complete": self.complete,
            "items": [
                {
                    "source": {
                        "path": item.source.path,
                        "size_bytes": item.source.size_bytes,
                        "sha256": item.source.sha256,
                    },
                    "status": item.status,
                    "resolved_path": (
                        None if item.resolved_path is None else str(item.resolved_path)
                    ),
                    "candidates": [str(path) for path in item.candidates],
                    "method": item.method,
                }
                for item in self.items
            ],
        }


def _path_key(path: str | Path) -> str:
    return str(Path(path).expanduser().absolute().resolve(strict=False)).casefold()


def _verify_candidate(path: Path, identity: FileIdentity) -> bool:
    if not path.is_file() or path.stat().st_size != identity.size_bytes:
        return False
    return FileIdentity.from_path(path).sha256 == identity.sha256


def resolve_file_relocation(
    identities: Sequence[FileIdentity],
    search_root: str | Path,
    *,
    original_root: str | Path | None = None,
    recursive: bool = True,
) -> RelocationReport:
    """Locate identities under ``search_root`` without mutating any recipe.

    ``original_root`` is only a relative-path hint.  A hinted file is accepted
    only after the same size and SHA-256 checks used by the fallback search.
    """

    sources = tuple(identities)
    if not sources:
        raise RelocationError("relocation requires at least one file identity")
    if any(not isinstance(item, FileIdentity) for item in sources):
        raise RelocationError("relocation inputs must be FileIdentity values")
    if len({_path_key(item.path) for item in sources}) != len(sources):
        raise RelocationError("relocation input contains duplicate source paths")
    root = Path(search_root).expanduser().absolute()
    if not root.is_dir():
        raise RelocationError(f"relocation search root is not a directory: {root}")
    old_root = (
        None
        if original_root is None
        else Path(original_root).expanduser().absolute().resolve(strict=False)
    )

    candidates = tuple(
        path.absolute()
        for path in (root.rglob("*") if recursive else root.glob("*"))
        if path.is_file()
    )
    by_size: dict[int, list[Path]] = {}
    for path in candidates:
        try:
            by_size.setdefault(path.stat().st_size, []).append(path)
        except OSError:
            continue
    hash_cache: dict[Path, str | None] = {}
    items: list[RelocationItem] = []
    for identity in sources:
        hinted: Path | None = None
        if old_root is not None:
            try:
                relative = Path(identity.path).expanduser().absolute().resolve(
                    strict=False
                ).relative_to(old_root)
            except ValueError:
                relative = None
            if relative is not None:
                candidate = root / relative
                if _verify_candidate(candidate, identity):
                    hinted = candidate.absolute()
        if hinted is not None:
            items.append(
                RelocationItem(
                    identity,
                    "resolved",
                    hinted,
                    (hinted,),
                    "relative_path_and_sha256",
                )
            )
            continue

        matched: list[Path] = []
        for candidate in by_size.get(identity.size_bytes, ()):
            if candidate not in hash_cache:
                try:
                    hash_cache[candidate] = FileIdentity.from_path(candidate).sha256
                except OSError:
                    hash_cache[candidate] = None
            if hash_cache[candidate] == identity.sha256:
                matched.append(candidate)
        if len(matched) == 1:
            items.append(
                RelocationItem(
                    identity,
                    "resolved",
                    matched[0],
                    tuple(matched),
                    "size_and_sha256_search",
                )
            )
        elif matched:
            items.append(
                RelocationItem(identity, "ambiguous", None, tuple(matched), None)
            )
        else:
            items.append(RelocationItem(identity, "missing", None, (), None))
    return RelocationReport(root.resolve(strict=False), tuple(items))


def _relocated_identity(
    identity: FileIdentity, mapping: Mapping[str, str | Path]
) -> FileIdentity:
    normalized = {_path_key(key): Path(value) for key, value in mapping.items()}
    key = _path_key(identity.path)
    if key not in normalized:
        raise RelocationError(f"relocation mapping is missing: {identity.path}")
    path = normalized[key].expanduser().absolute()
    if not _verify_candidate(path, identity):
        raise RelocationError(f"relocation target identity does not match: {path}")
    return FileIdentity.from_path(path)


def relocate_file_identity(
    identity: FileIdentity, mapping: Mapping[str, str | Path]
) -> FileIdentity:
    """Return one file identity rebound to a content-verified target.

    This public wrapper keeps project relocation on the same strict
    size-and-SHA256 path used by processing recipes and batch plans.
    """

    return _relocated_identity(identity, mapping)


def relocate_processing_recipe(
    recipe: ProcessingRecipe, mapping: Mapping[str, str | Path]
) -> ProcessingRecipe:
    """Return a recipe rebound to verified background/MASK locations."""

    background = (
        None
        if recipe.background_source is None
        else _relocated_identity(recipe.background_source, mapping)
    )
    external_mask = (
        None
        if recipe.external_mask_source is None
        else _relocated_identity(recipe.external_mask_source, mapping)
    )
    return replace(
        recipe,
        background_source=background,
        external_mask_source=external_mask,
    )


def relocate_batch_processing_plan(
    plan: BatchProcessingPlan, mapping: Mapping[str, str | Path]
) -> BatchProcessingPlan:
    """Return a complete immutable batch plan rebound to verified files."""

    base_recipe = relocate_processing_recipe(plan.recipe, mapping)
    revisions = tuple(
        BatchRecipeRevision(
            revision.revision_id,
            relocate_processing_recipe(revision.recipe, mapping),
            revision.reason,
        )
        for revision in plan.recipe_revisions
    )
    samples = tuple(_relocated_identity(item, mapping) for item in plan.sample_sources)
    return BatchProcessingPlan(
        recipe=base_recipe,
        sample_sources=samples,
        metadata={**dict(plan.metadata), "relocated_from_plan": True},
        recipe_revisions=revisions,
        recipe_assignment=plan.recipe_assignment,
    )
