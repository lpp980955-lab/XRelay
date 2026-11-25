"""Versioned XRelay projects and portable directory packages.

Normal ``.swwproj`` files are small JSON documents that externally reference
source files by path, byte size and SHA-256.  Portable packages copy only the
project's referenced inputs into a BagIt-compatible directory and rewrite the
saved project to package-relative paths.  Opening never accepts a same-name
file as proof of identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .display import DisplayPreset
from .presets import (
    ExportPreset,
    display_preset_from_document,
    display_preset_to_document,
    export_preset_from_document,
    export_preset_to_document,
)
from .processing import (
    BatchProcessingPlan,
    FileIdentity,
    ProcessingError,
    ProcessingRecipe,
    batch_processing_plan_from_document,
    batch_processing_plan_to_document,
    processing_recipe_from_document,
    processing_recipe_to_document,
)
from .relocation import (
    RelocationError,
    relocate_batch_processing_plan,
    relocate_file_identity,
    relocate_processing_recipe,
)


PRODUCT_DISPLAY_NAME = "XRelay"
PROJECT_EXTENSION = ".swwproj"
PROJECT_SCHEMA = "saxs_waxs_project/1"
PORTABLE_MANIFEST_SCHEMA = "saxs_waxs_portable_manifest/1"


class ProjectError(ProcessingError):
    """Raised when a project or portable package is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class WorkbenchProject:
    name: str
    data_root_hint: str | None = None
    recursive: bool = True
    current_source: FileIdentity | None = None
    selected_sources: Sequence[FileIdentity] = ()
    processing: ProcessingRecipe | None = None
    batch_plan: BatchProcessingPlan | None = None
    display: DisplayPreset = DisplayPreset()
    export: ExportPreset = ExportPreset()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ProjectError("project name must be non-empty")
        if self.data_root_hint is not None and (
            not isinstance(self.data_root_hint, str) or not self.data_root_hint.strip()
        ):
            raise ProjectError("data_root_hint must be null or a non-empty string")
        if not isinstance(self.recursive, bool):
            raise ProjectError("recursive must be a boolean")
        if self.current_source is not None and not isinstance(
            self.current_source, FileIdentity
        ):
            raise ProjectError("current_source must be a FileIdentity")
        selected = tuple(self.selected_sources)
        if any(not isinstance(item, FileIdentity) for item in selected):
            raise ProjectError("selected_sources must contain FileIdentity values")
        keys = {_path_key(item.path) for item in selected}
        if len(keys) != len(selected):
            raise ProjectError("selected_sources contains duplicate paths")
        if self.processing is not None and not isinstance(
            self.processing, ProcessingRecipe
        ):
            raise ProjectError("processing must be a ProcessingRecipe")
        if self.batch_plan is not None and not isinstance(
            self.batch_plan, BatchProcessingPlan
        ):
            raise ProjectError("batch_plan must be a BatchProcessingPlan")
        if not isinstance(self.display, DisplayPreset):
            raise ProjectError("display must be a DisplayPreset")
        if not isinstance(self.export, ExportPreset):
            raise ProjectError("export must be an ExportPreset")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "selected_sources", selected)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def _path_key(path: str | Path) -> str:
    return str(Path(path).expanduser().absolute().resolve(strict=False)).casefold()


def _identity_document(identity: FileIdentity) -> dict[str, object]:
    return {
        "path": identity.path,
        "size_bytes": identity.size_bytes,
        "sha256": identity.sha256,
    }


def _identity_from_document(value: Any, label: str) -> FileIdentity:
    if not isinstance(value, Mapping) or set(value) != {
        "path",
        "size_bytes",
        "sha256",
    }:
        raise ProjectError(f"{label} fields differ from schema")
    try:
        return FileIdentity(
            path=value["path"],
            size_bytes=value["size_bytes"],
            sha256=value["sha256"],
        )
    except (TypeError, ValueError) as exc:
        raise ProjectError(f"invalid {label}: {exc}") from exc


def _transform_identity_paths(value: Any, transform) -> Any:
    """Copy a project document while transforming only identity paths."""

    if isinstance(value, Mapping):
        if set(value) == {"path", "size_bytes", "sha256"}:
            return {**value, "path": transform(str(value["path"]))}
        return {key: _transform_identity_paths(item, transform) for key, item in value.items()}
    if isinstance(value, list):
        return [_transform_identity_paths(item, transform) for item in value]
    return value


def project_to_document(
    project: WorkbenchProject, *, relative_to: str | Path | None = None
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": PROJECT_SCHEMA,
        "product": PRODUCT_DISPLAY_NAME,
        "name": project.name,
        "data_root_hint": project.data_root_hint,
        "recursive": project.recursive,
        "current_source": (
            None
            if project.current_source is None
            else _identity_document(project.current_source)
        ),
        "selected_sources": [
            _identity_document(item) for item in project.selected_sources
        ],
        "processing": (
            None
            if project.processing is None
            else processing_recipe_to_document(project.processing)
        ),
        "batch_plan": (
            None
            if project.batch_plan is None
            else batch_processing_plan_to_document(project.batch_plan)
        ),
        "display": display_preset_to_document(project.display),
        "export": export_preset_to_document(project.export),
        "metadata": dict(project.metadata),
    }
    if relative_to is None:
        return document
    base = Path(relative_to).expanduser().absolute().resolve(strict=False)

    def relative_path(value: str) -> str:
        path = Path(value).expanduser().absolute().resolve(strict=False)
        try:
            return path.relative_to(base).as_posix()
        except ValueError as exc:
            raise ProjectError(f"project reference is outside portable root: {path}") from exc

    document = _transform_identity_paths(document, relative_path)
    hint = document["data_root_hint"]
    if hint is not None:
        hint_path = Path(str(hint)).expanduser().absolute().resolve(strict=False)
        try:
            document["data_root_hint"] = hint_path.relative_to(base).as_posix()
        except ValueError as exc:
            raise ProjectError("portable data_root_hint is outside package") from exc
    return document


def project_from_document(
    document: Mapping[str, Any], *, base_directory: str | Path | None = None
) -> WorkbenchProject:
    expected = {
        "schema",
        "product",
        "name",
        "data_root_hint",
        "recursive",
        "current_source",
        "selected_sources",
        "processing",
        "batch_plan",
        "display",
        "export",
        "metadata",
    }
    if not isinstance(document, Mapping) or set(document) != expected:
        raise ProjectError("project fields differ from schema")
    if document["schema"] != PROJECT_SCHEMA:
        raise ProjectError(f"unsupported project schema: {document['schema']!r}")
    if document["product"] != PRODUCT_DISPLAY_NAME:
        raise ProjectError(f"unsupported project product: {document['product']!r}")
    value: Mapping[str, Any] = document
    base: Path | None = None
    if base_directory is not None:
        base = Path(base_directory).expanduser().absolute().resolve(strict=False)

        def absolute_path(raw: str) -> str:
            path = Path(raw).expanduser()
            return str(path.absolute() if path.is_absolute() else (base / path).absolute())

        value = _transform_identity_paths(document, absolute_path)
    selected_value = value["selected_sources"]
    if not isinstance(selected_value, list):
        raise ProjectError("selected_sources must be an array")
    hint = value["data_root_hint"]
    if hint is not None and base is not None and not Path(str(hint)).is_absolute():
        hint = str((base / str(hint)).absolute())
    try:
        return WorkbenchProject(
            name=value["name"],
            data_root_hint=hint,
            recursive=value["recursive"],
            current_source=(
                None
                if value["current_source"] is None
                else _identity_from_document(value["current_source"], "current_source")
            ),
            selected_sources=tuple(
                _identity_from_document(item, "selected_source")
                for item in selected_value
            ),
            processing=(
                None
                if value["processing"] is None
                else processing_recipe_from_document(value["processing"])
            ),
            batch_plan=(
                None
                if value["batch_plan"] is None
                else batch_processing_plan_from_document(value["batch_plan"])
            ),
            display=display_preset_from_document(value["display"]),
            export=export_preset_from_document(value["export"]),
            metadata=value["metadata"],
        )
    except (TypeError, ValueError, ProcessingError) as exc:
        if isinstance(exc, ProjectError):
            raise
        raise ProjectError(f"invalid project: {exc}") from exc


def project_file_identities(project: WorkbenchProject) -> tuple[FileIdentity, ...]:
    identities: list[FileIdentity] = []

    def append(identity: FileIdentity | None) -> None:
        if identity is not None:
            identities.append(identity)

    def append_recipe(recipe: ProcessingRecipe | None) -> None:
        if recipe is not None:
            append(recipe.background_source)
            append(recipe.external_mask_source)

    append(project.current_source)
    identities.extend(project.selected_sources)
    append_recipe(project.processing)
    if project.batch_plan is not None:
        identities.extend(project.batch_plan.sample_sources)
        append_recipe(project.batch_plan.recipe)
        for revision in project.batch_plan.recipe_revisions:
            append_recipe(revision.recipe)
    unique: dict[str, FileIdentity] = {}
    for identity in identities:
        key = _path_key(identity.path)
        existing = unique.get(key)
        if existing is not None and existing != identity:
            raise ProjectError(f"conflicting identities for path: {identity.path}")
        unique[key] = identity
    return tuple(unique.values())


def verify_project_references(project: WorkbenchProject) -> tuple[FileIdentity, ...]:
    invalid: list[FileIdentity] = []
    for identity in project_file_identities(project):
        path = Path(identity.path)
        try:
            actual = FileIdentity.from_path(path)
        except OSError:
            invalid.append(identity)
            continue
        if actual.size_bytes != identity.size_bytes or actual.sha256 != identity.sha256:
            invalid.append(identity)
    return tuple(invalid)


def relocate_project(
    project: WorkbenchProject,
    mapping: Mapping[str, str | Path],
    *,
    data_root_hint: str | Path | None = None,
) -> WorkbenchProject:
    current = (
        None
        if project.current_source is None
        else relocate_file_identity(project.current_source, mapping)
    )
    selected = tuple(
        relocate_file_identity(item, mapping) for item in project.selected_sources
    )
    processing = (
        None
        if project.processing is None
        else relocate_processing_recipe(project.processing, mapping)
    )
    batch = (
        None
        if project.batch_plan is None
        else relocate_batch_processing_plan(project.batch_plan, mapping)
    )
    source_paths={old.path:new.path for old,new in zip(project.selected_sources,selected)}
    if current is not None:
        source_paths[project.current_source.path]=current.path
    return replace(
        project,
        current_source=current,
        selected_sources=selected,
        processing=processing,
        batch_plan=batch,
        data_root_hint=(
            project.data_root_hint
            if data_root_hint is None
            else str(Path(data_root_hint).expanduser().absolute())
        ),
        metadata={
            **dict(project.metadata),
            "relocated": True,
            "detector_source_choices": {
                str(Path(source_paths.get(path,path)).resolve()): option
                for path,option in project.metadata.get("detector_source_choices",{}).items()
            },
        },
    )


def _validate_destination(
    path: str | Path, protected_roots: Sequence[str | Path]
) -> Path:
    destination = Path(path).expanduser().absolute()
    resolved = destination.resolve(strict=False)
    for protected in protected_roots:
        root = Path(protected).expanduser().absolute().resolve(strict=False)
        if resolved == root or root in resolved.parents:
            raise ProjectError(f"refusing to write inside protected root: {root}")
    return destination


def save_project(
    project: WorkbenchProject,
    path: str | Path,
    *,
    protected_roots: Sequence[str | Path] = (),
    overwrite: bool = False,
) -> Path:
    destination = _validate_destination(path, protected_roots)
    if destination.suffix.casefold() != PROJECT_EXTENSION:
        raise ProjectError(f"project path must end with {PROJECT_EXTENSION}")
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        project_to_document(project), ensure_ascii=False, indent=2
    ) + "\n"
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


def load_project(path: str | Path) -> WorkbenchProject:
    source = Path(path).expanduser().absolute()
    if source.suffix.casefold() != PROJECT_EXTENSION:
        raise ProjectError(f"project path must end with {PROJECT_EXTENSION}")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProjectError(f"invalid project JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ProjectError("project JSON root must be an object")
    return project_from_document(document, base_directory=source.parent)


def _portable_relative_path(identity: FileIdentity, root: Path | None) -> Path:
    source = Path(identity.path).expanduser().absolute().resolve(strict=False)
    if root is not None:
        try:
            relative = source.relative_to(root)
            if relative.parts and all(part not in {".", ".."} for part in relative.parts):
                return Path("data") / relative
        except ValueError:
            pass
    safe_name = source.name or "payload.bin"
    return Path("data") / "external" / f"{identity.sha256[:12]}_{safe_name}"


def export_portable_project(
    project: WorkbenchProject,
    destination: str | Path,
    *,
    protected_roots: Sequence[str | Path] = (),
) -> Path:
    """Create a new, non-overwriting BagIt-compatible portable directory."""

    target = _validate_destination(destination, protected_roots)
    if target.exists():
        raise FileExistsError(target)
    identities = project_file_identities(project)
    if not identities:
        raise ProjectError("portable project has no referenced files")
    invalid = verify_project_references(project)
    if invalid:
        raise ProjectError(
            "cannot package missing or changed references: "
            + ", ".join(Path(item.path).name for item in invalid[:3])
        )
    original_root = (
        None
        if project.data_root_hint is None
        else Path(project.data_root_hint).expanduser().absolute().resolve(strict=False)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        mapping: dict[str, Path] = {}
        occupied: dict[str, FileIdentity] = {}
        entries: list[dict[str, object]] = []
        for identity in identities:
            relative = _portable_relative_path(identity, original_root)
            collision_key = relative.as_posix().casefold()
            previous = occupied.get(collision_key)
            if previous is not None and previous != identity:
                relative = Path("data") / "external" / (
                    f"{identity.sha256}_{Path(identity.path).name}"
                )
                collision_key = relative.as_posix().casefold()
                if collision_key in occupied and occupied[collision_key] != identity:
                    raise ProjectError("portable payload path collision")
            occupied[collision_key] = identity
            copied = staging / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(identity.path, copied)
            actual = FileIdentity.from_path(copied)
            if actual.size_bytes != identity.size_bytes or actual.sha256 != identity.sha256:
                raise ProjectError(f"portable copy verification failed: {identity.path}")
            mapping[identity.path] = copied
            entries.append(
                {
                    "path": relative.as_posix(),
                    "size_bytes": identity.size_bytes,
                    "sha256": identity.sha256,
                    "original_path": identity.path,
                }
            )
        packaged = relocate_project(project, mapping, data_root_hint=staging / "data")
        project_path = staging / f"project{PROJECT_EXTENSION}"
        project_path.write_text(
            json.dumps(
                project_to_document(packaged, relative_to=staging),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        manifest_path = staging / "portable-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": PORTABLE_MANIFEST_SCHEMA,
                    "product": PRODUCT_DISPLAY_NAME,
                    "project": project_path.name,
                    "entries": entries,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        (staging / "bagit.txt").write_text(
            "BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n",
            encoding="utf-8",
            newline="\n",
        )
        payload_lines = [
            f"{entry['sha256']}  {entry['path']}" for entry in entries
        ]
        (staging / "manifest-sha256.txt").write_text(
            "\n".join(payload_lines) + "\n", encoding="utf-8", newline="\n"
        )
        tag_names = (
            "bagit.txt",
            "manifest-sha256.txt",
            "portable-manifest.json",
            project_path.name,
        )
        tag_lines = []
        for name in tag_names:
            digest = hashlib.sha256((staging / name).read_bytes()).hexdigest()
            tag_lines.append(f"{digest}  {name}")
        (staging / "tagmanifest-sha256.txt").write_text(
            "\n".join(tag_lines) + "\n", encoding="utf-8", newline="\n"
        )
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target
