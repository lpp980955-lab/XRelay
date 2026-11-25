"""Versioned separation of processing, display and export preset domains.

The three domains deliberately have different effects:

* processing changes scientific arrays and therefore embeds a ProcessingRecipe;
* display changes only on-screen rendering;
* export controls file representation and never changes processing or display.

Phase 4 records the separation.  Phase 5 may extend the export fields with new
formats, but must migrate this schema instead of silently reinterpreting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .display import DisplayPreset
from .processing import (
    ProcessingError,
    ProcessingRecipe,
    processing_recipe_from_document,
    processing_recipe_to_document,
)


DISPLAY_PRESET_SCHEMA = "saxs_waxs_display_preset/1"
EXPORT_PRESET_SCHEMA_V1 = "saxs_waxs_export_preset/1"
EXPORT_PRESET_SCHEMA_V2 = "saxs_waxs_export_preset/2"
EXPORT_PRESET_SCHEMA = "saxs_waxs_export_preset/3"
PRESET_DOMAINS_SCHEMA = "saxs_waxs_preset_domains/1"


@dataclass(frozen=True, slots=True)
class ExportPreset:
    """Representation choices only; no field may alter scientific values."""

    curve_data_format: str = "csv"
    curve_image_format: str | None = None
    detector_image_format: str = "png"
    detector_include_axes: bool = True
    detector_include_colorbar: bool = True
    detector_range_mode: str = "independent"
    detector_axis_mode: str = "pixel"
    image_width_mm: float = 180.0
    image_height_mm: float = 130.0
    image_dpi: int = 600
    detector_font_family: str = "DejaVu Sans"
    detector_label_font_size_pt: float = 10.0
    detector_tick_font_size_pt: float = 8.0
    detector_line_width_pt: float = 0.8
    include_processing_audit: bool = True
    overwrite: bool = False

    def __post_init__(self) -> None:
        if self.curve_data_format not in {"csv", "tsv", "json", "npz"}:
            raise ValueError("unsupported curve data export format")
        if self.curve_image_format not in {None, "png", "tiff", "svg", "pdf"}:
            raise ValueError("unsupported curve image export format")
        if self.detector_image_format not in {"png", "tiff", "svg", "pdf"}:
            raise ValueError("unsupported detector image export format")
        if self.detector_range_mode not in {"independent", "shared_manual"}:
            raise ValueError("unsupported detector range mode")
        if self.detector_axis_mode not in {"pixel", "detector_mm"}:
            raise ValueError("unsupported detector axis mode")
        if self.image_width_mm <= 0 or self.image_height_mm <= 0:
            raise ValueError("export image dimensions must be positive")
        if self.image_dpi <= 0:
            raise ValueError("export image DPI must be positive")
        if not self.detector_font_family.strip():
            raise ValueError("detector font family must not be empty")
        if self.detector_label_font_size_pt <= 0:
            raise ValueError("detector label font size must be positive")
        if self.detector_tick_font_size_pt <= 0:
            raise ValueError("detector tick font size must be positive")
        if self.detector_line_width_pt <= 0:
            raise ValueError("detector line width must be positive")


@dataclass(frozen=True, slots=True)
class PresetDomains:
    """A typed snapshot; domains stay separate rather than becoming one recipe."""

    processing: ProcessingRecipe
    display: DisplayPreset = DisplayPreset()
    export: ExportPreset = ExportPreset()


def display_preset_to_document(preset: DisplayPreset) -> dict[str, Any]:
    return {
        "schema": DISPLAY_PRESET_SCHEMA,
        "colormap": preset.colormap,
        "scale": preset.scale,
        "limit_mode": preset.limit_mode,
        "lower_percentile": preset.lower_percentile,
        "upper_percentile": preset.upper_percentile,
        "manual_vmin": preset.manual_vmin,
        "manual_vmax": preset.manual_vmax,
        "invert_colormap": preset.invert_colormap,
        "custom_colors": list(preset.custom_colors),
    }


def display_preset_from_document(document: Mapping[str, Any]) -> DisplayPreset:
    expected = {
        "schema",
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
        raise ProcessingError("display preset fields differ from schema")
    if document["schema"] != DISPLAY_PRESET_SCHEMA:
        raise ProcessingError(f"unsupported display preset schema: {document['schema']!r}")
    try:
        return DisplayPreset(
            colormap=document["colormap"],
            scale=document["scale"],
            limit_mode=document["limit_mode"],
            lower_percentile=document["lower_percentile"],
            upper_percentile=document["upper_percentile"],
            manual_vmin=document["manual_vmin"],
            manual_vmax=document["manual_vmax"],
            invert_colormap=document["invert_colormap"],
            custom_colors=tuple(document["custom_colors"]),
        )
    except (TypeError, ValueError) as exc:
        raise ProcessingError(f"invalid display preset: {exc}") from exc


def export_preset_to_document(preset: ExportPreset) -> dict[str, Any]:
    return {
        "schema": EXPORT_PRESET_SCHEMA,
        "curve_data_format": preset.curve_data_format,
        "curve_image_format": preset.curve_image_format,
        "detector_image_format": preset.detector_image_format,
        "detector_include_axes": preset.detector_include_axes,
        "detector_include_colorbar": preset.detector_include_colorbar,
        "detector_range_mode": preset.detector_range_mode,
        "detector_axis_mode": preset.detector_axis_mode,
        "image_width_mm": preset.image_width_mm,
        "image_height_mm": preset.image_height_mm,
        "image_dpi": preset.image_dpi,
        "detector_font_family": preset.detector_font_family,
        "detector_label_font_size_pt": preset.detector_label_font_size_pt,
        "detector_tick_font_size_pt": preset.detector_tick_font_size_pt,
        "detector_line_width_pt": preset.detector_line_width_pt,
        "include_processing_audit": preset.include_processing_audit,
        "overwrite": preset.overwrite,
    }


def export_preset_from_document(document: Mapping[str, Any]) -> ExportPreset:
    expected_v1 = {
        "schema",
        "curve_data_format",
        "curve_image_format",
        "image_width_mm",
        "image_height_mm",
        "image_dpi",
        "include_processing_audit",
        "overwrite",
    }
    expected_v2 = expected_v1 | {
        "detector_image_format",
        "detector_include_axes",
        "detector_include_colorbar",
        "detector_range_mode",
    }
    style_fields = {
        "detector_axis_mode",
        "detector_font_family",
        "detector_label_font_size_pt",
        "detector_tick_font_size_pt",
        "detector_line_width_pt",
    }
    expected = expected_v2 | style_fields
    if not isinstance(document, Mapping):
        raise ProcessingError("export preset fields differ from schema")
    schema = document.get("schema")
    if schema == EXPORT_PRESET_SCHEMA_V1:
        if set(document) != expected_v1:
            raise ProcessingError("export preset fields differ from schema")
        values = {key: document[key] for key in expected_v1 - {"schema"}}
        # v1 had no detector-image fields.  Migration uses explicit v2 defaults
        # rather than interpreting any curve field as a detector-image choice.
        try:
            return ExportPreset(**values)
        except (TypeError, ValueError) as exc:
            raise ProcessingError(f"invalid export preset: {exc}") from exc
    if schema == EXPORT_PRESET_SCHEMA_V2:
        if set(document) != expected_v2:
            raise ProcessingError("export preset fields differ from schema")
        values = {key: document[key] for key in expected_v2 - {"schema"}}
        # v2 predates explicit detector typography.  Preserve its rendered
        # defaults instead of deriving styles from the current host.
        try:
            return ExportPreset(**values)
        except (TypeError, ValueError) as exc:
            raise ProcessingError(f"invalid export preset: {exc}") from exc
    if set(document) != expected:
        raise ProcessingError("export preset fields differ from schema")
    if schema != EXPORT_PRESET_SCHEMA:
        raise ProcessingError(f"unsupported export preset schema: {document['schema']!r}")
    try:
        return ExportPreset(**{key: document[key] for key in expected - {"schema"}})
    except (TypeError, ValueError) as exc:
        raise ProcessingError(f"invalid export preset: {exc}") from exc


def preset_domains_to_document(presets: PresetDomains) -> dict[str, Any]:
    return {
        "schema": PRESET_DOMAINS_SCHEMA,
        "processing": processing_recipe_to_document(presets.processing),
        "display": display_preset_to_document(presets.display),
        "export": export_preset_to_document(presets.export),
    }


def preset_domains_from_document(document: Mapping[str, Any]) -> PresetDomains:
    expected = {"schema", "processing", "display", "export"}
    if not isinstance(document, Mapping) or set(document) != expected:
        raise ProcessingError("preset-domain fields differ from schema")
    if document["schema"] != PRESET_DOMAINS_SCHEMA:
        raise ProcessingError(f"unsupported preset-domain schema: {document['schema']!r}")
    return PresetDomains(
        processing=processing_recipe_from_document(document["processing"]),
        display=display_preset_from_document(document["display"]),
        export=export_preset_from_document(document["export"]),
    )
