"""Deterministic 2D scientific-image export from arrays, never screenshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .colormap_data import DEFAULT_CUSTOM_COLORS, STATIC_PALETTES, load_colorcet_rgb
from .display import DisplayPreset, resolve_display_limits
from .geometry import DetectorGeometry


IMAGE_EXPORT_PARAMETERS_SCHEMA = "saxs_waxs_2d_image_export_parameters/2"
# Backward import compatibility for callers that used the old constant name.
IMAGE_EXPORT_AUDIT_SCHEMA = IMAGE_EXPORT_PARAMETERS_SCHEMA
SUPPORTED_IMAGE_EXPORT_FORMATS = frozenset({"png", "tiff", "svg", "pdf"})


class ImageExportError(RuntimeError):
    """Raised when an image export would be ambiguous or destructive."""


@dataclass(frozen=True, slots=True)
class ImageExportResult:
    image_path: Path
    parameters_path: Path | None
    parameter_document: Mapping[str, Any]
    sha256: str
    width_px: int
    height_px: int
    vmin: float
    vmax: float

    @property
    def audit_path(self) -> Path | None:
        """Deprecated alias retained for older API callers."""

        return self.parameters_path


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _hex_rgb(value: str) -> tuple[float, float, float]:
    text = value.lstrip("#")
    if len(text) != 6:
        raise ImageExportError(f"invalid color node: {value!r}")
    return tuple(int(text[index : index + 2], 16) / 255.0 for index in (0, 2, 4))


def _color_nodes(preset: DisplayPreset) -> Sequence[tuple[float, float, float]] | None:
    if preset.colormap == "custom":
        colors = preset.custom_colors or DEFAULT_CUSTOM_COLORS
        return tuple(_hex_rgb(color) for color in colors)
    if preset.colormap in STATIC_PALETTES:
        return tuple(_hex_rgb(color) for color in STATIC_PALETTES[preset.colormap])
    if preset.colormap.startswith("CET-"):
        return load_colorcet_rgb(preset.colormap)
    return None


def _matplotlib_colormap(preset: DisplayPreset):
    try:
        from matplotlib import colormaps
        from matplotlib.colors import LinearSegmentedColormap
    except ImportError as exc:  # pragma: no cover - dependency gate
        raise ImageExportError("2D image export requires Matplotlib") from exc

    nodes = _color_nodes(preset)
    if nodes is None:
        try:
            color_map = colormaps[preset.colormap]
        except KeyError as exc:
            raise ImageExportError(f"unsupported export colormap: {preset.colormap}") from exc
    else:
        color_map = LinearSegmentedColormap.from_list(
            f"saxs_waxs_{preset.colormap}", nodes, N=256
        )
    if preset.invert_colormap:
        color_map = color_map.reversed()
    return color_map.with_extremes(bad="#000000")


def _display_document(preset: DisplayPreset) -> dict[str, Any]:
    return {
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


def _geometry_summary(geometry: DetectorGeometry | None) -> dict[str, Any] | None:
    """Return manuscript-facing geometry fields in convenient units."""

    if geometry is None:
        return None
    return {
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
    }


def export_2d_image(
    data: np.ndarray,
    destination: str | Path,
    display: DisplayPreset,
    *,
    mask: np.ndarray | None = None,
    width_mm: float = 180.0,
    height_mm: float = 130.0,
    dpi: int = 600,
    include_axes: bool = True,
    include_colorbar: bool = True,
    x_label: str = "x (column / pixel)",
    y_label: str = "y (row / pixel)",
    intensity_label: str = "Intensity",
    font_family: str = "DejaVu Sans",
    label_font_size_pt: float = 10.0,
    tick_font_size_pt: float = 8.0,
    line_width_pt: float = 0.8,
    axis_mode: str = "pixel",
    geometry: DetectorGeometry | None = None,
    source_path: str | Path | None = None,
    processing_audit: Mapping[str, Any] | None = None,
    write_parameter_file: bool = True,
    protected_roots: Sequence[str | Path] = (),
    overwrite: bool = False,
) -> ImageExportResult:
    """Render a fixed-size image directly from a 2D array and display preset.

    The source array is never resampled before Matplotlib renders the requested
    output canvas.  Screen zoom, window size and UI overlays are not inputs.
    """

    array = np.asarray(data)
    if array.ndim != 2:
        raise ImageExportError(f"2D export requires a 2D array, got {array.ndim}D")
    mask_array = None if mask is None else np.asarray(mask, dtype=bool)
    if mask_array is not None and mask_array.shape != array.shape:
        raise ImageExportError("mask shape must match the exported array")
    if not np.isfinite(width_mm) or not np.isfinite(height_mm) or width_mm <= 0 or height_mm <= 0:
        raise ImageExportError("export dimensions must be finite and positive")
    if int(dpi) != dpi or dpi <= 0:
        raise ImageExportError("export DPI must be a positive integer")
    if not str(font_family).strip():
        raise ImageExportError("export font family must not be empty")
    if not np.isfinite(label_font_size_pt) or label_font_size_pt <= 0:
        raise ImageExportError("label font size must be finite and positive")
    if not np.isfinite(tick_font_size_pt) or tick_font_size_pt <= 0:
        raise ImageExportError("tick font size must be finite and positive")
    if not np.isfinite(line_width_pt) or line_width_pt <= 0:
        raise ImageExportError("line width must be finite and positive")
    if axis_mode not in {"pixel", "detector_mm"}:
        raise ImageExportError(f"unsupported detector axis mode: {axis_mode}")
    image_extent: tuple[float, float, float, float] | None = None
    resolved_x_label = x_label
    resolved_y_label = y_label
    if axis_mode == "detector_mm":
        if geometry is None:
            raise ImageExportError("detector-mm axes require applied geometry")
        if geometry.image_width_px is not None and geometry.image_width_px != array.shape[1]:
            raise ImageExportError("geometry width does not match exported array")
        if geometry.image_height_px is not None and geometry.image_height_px != array.shape[0]:
            raise ImageExportError("geometry height does not match exported array")
        x_left = -geometry.beam_center_x_px * geometry.pixel_size_x_m * 1e3
        x_right = (
            array.shape[1] - geometry.beam_center_x_px
        ) * geometry.pixel_size_x_m * 1e3
        y_top = -geometry.beam_center_y_px * geometry.pixel_size_y_m * 1e3
        y_bottom = (
            array.shape[0] - geometry.beam_center_y_px
        ) * geometry.pixel_size_y_m * 1e3
        image_extent = (x_left, x_right, y_bottom, y_top)
        resolved_x_label = "x from beam center (mm)"
        resolved_y_label = "y from beam center (mm)"

    output = Path(destination).expanduser().resolve()
    suffix = output.suffix.lower().lstrip(".")
    suffix = "tiff" if suffix in {"tif", "tiff"} else suffix
    if suffix not in SUPPORTED_IMAGE_EXPORT_FORMATS:
        raise ImageExportError(f"unsupported 2D image format: {output.suffix or '<none>'}")
    for root in protected_roots:
        if _is_within(output, Path(root).expanduser().resolve()):
            raise ImageExportError(f"refusing to write inside protected root: {root}")
    format_tag = output.suffix.lower().lstrip(".")
    parameters_path = output.with_name(
        f"{output.stem}_{format_tag or 'file'}_parameters.json"
    )
    if not overwrite and (
        output.exists() or (write_parameter_file and parameters_path.exists())
    ):
        raise ImageExportError(f"refusing to overwrite existing export: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        from matplotlib import rc_context
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.colors import LogNorm, Normalize
        from matplotlib.figure import Figure
        from matplotlib.font_manager import FontProperties, findfont
    except ImportError as exc:  # pragma: no cover - dependency gate
        raise ImageExportError("2D image export requires Matplotlib") from exc

    requested_font = str(font_family).strip()
    try:
        resolved_font = findfont(
            FontProperties(family=[requested_font]), fallback_to_default=False
        )
    except ValueError as exc:
        raise ImageExportError(
            f"requested export font is not installed: {requested_font}"
        ) from exc
    resolved_font_path = Path(resolved_font).resolve()

    try:
        vmin, vmax = resolve_display_limits(array, display, mask_array)
    except ValueError as exc:
        raise ImageExportError(str(exc)) from exc
    valid = np.isfinite(array)
    if mask_array is not None:
        valid &= ~mask_array
    if display.scale == "log":
        valid &= array > 0
        norm = LogNorm(vmin=vmin, vmax=vmax, clip=True)
    else:
        norm = Normalize(vmin=vmin, vmax=vmax, clip=True)
    plotted = np.ma.array(array.astype(np.float64, copy=False), mask=~valid)

    width_px = int(round(width_mm / 25.4 * dpi))
    height_px = int(round(height_mm / 25.4 * dpi))
    figure_width_in = width_px / dpi
    figure_height_in = height_px / dpi
    figure = Figure(
        figsize=(figure_width_in, figure_height_in), dpi=dpi, facecolor="white"
    )
    FigureCanvasAgg(figure)
    if include_axes:
        # Reserve margins in physical units, not fixed fractions.  This keeps
        # axis/colorbar labels inside both compact previews and publication-size
        # exports without wasting a quarter of a wide figure on empty space.
        left = min(0.32, max(0.07, 0.45 / figure_width_in))
        right = min(
            0.32,
            max(0.07, (0.48 if include_colorbar else 0.16) / figure_width_in),
        )
        bottom = min(0.35, max(0.09, 0.35 / figure_height_in))
        top = min(0.18, max(0.04, 0.12 / figure_height_in))
        axes = figure.add_axes(
            [left, bottom, 1.0 - left - right, 1.0 - bottom - top]
        )
    else:
        axes = figure.add_axes([0.0, 0.0, 1.0, 1.0])
    image = axes.imshow(
        plotted,
        cmap=_matplotlib_colormap(display),
        norm=norm,
        origin="upper",
        interpolation="nearest",
        aspect="equal",
        extent=image_extent,
    )
    if include_axes:
        axes.set_xlabel(
            resolved_x_label,
            fontsize=label_font_size_pt,
            fontfamily=requested_font,
        )
        axes.set_ylabel(
            resolved_y_label,
            fontsize=label_font_size_pt,
            fontfamily=requested_font,
        )
        axes.tick_params(
            direction="in",
            top=True,
            right=True,
            labelsize=tick_font_size_pt,
            width=line_width_pt,
        )
        for tick_label in (*axes.get_xticklabels(), *axes.get_yticklabels()):
            tick_label.set_fontfamily(requested_font)
        for spine in axes.spines.values():
            spine.set_linewidth(line_width_pt)
        if include_colorbar:
            colorbar = figure.colorbar(image, ax=axes, fraction=0.046, pad=0.04)
            colorbar.set_label(
                intensity_label,
                fontsize=label_font_size_pt,
                fontfamily=requested_font,
            )
            colorbar.ax.tick_params(
                labelsize=tick_font_size_pt, width=line_width_pt
            )
            colorbar.outline.set_linewidth(line_width_pt)
            for tick_label in colorbar.ax.get_yticklabels():
                tick_label.set_fontfamily(requested_font)
    else:
        axes.set_axis_off()

    temporary_image = output.with_name(f".{output.name}.tmp")
    temporary_parameters = parameters_path.with_name(f".{parameters_path.name}.tmp")
    try:
        save_options: dict[str, Any] = {
            "format": suffix,
            "dpi": dpi,
            "facecolor": "white",
        }
        if suffix != "tiff":
            save_options["metadata"] = {"Creator": "XRelay"}
        # Keep labels as editable SVG text and embedded TrueType text in PDF.
        # Raster data remain rasterized by imshow; axes, ticks and text stay
        # vector without changing any scientific array values.
        with rc_context({"svg.fonttype": "none", "pdf.fonttype": 42}):
            figure.savefig(temporary_image, **save_options)
        sha256 = hashlib.sha256(temporary_image.read_bytes()).hexdigest()
        parameter_document = {
            "schema": IMAGE_EXPORT_PARAMETERS_SCHEMA,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_path": None if source_path is None else str(Path(source_path).resolve()),
            "output_file": output.name,
            "output_sha256": sha256,
            "array_shape_row_column": list(array.shape),
            "array_dtype": str(array.dtype),
            "masked_pixel_count": int(np.count_nonzero(~valid)),
            "display": _display_document(display),
            "resolved_limits": {"vmin": vmin, "vmax": vmax},
            "geometry": _geometry_summary(geometry),
            "render": {
                "format": suffix,
                "width_mm": float(width_mm),
                "height_mm": float(height_mm),
                "dpi": int(dpi),
                "width_px": width_px,
                "height_px": height_px,
                "include_axes": bool(include_axes),
                "include_colorbar": bool(include_colorbar),
                "font_family_requested": requested_font,
                "font_file_name": resolved_font_path.name,
                "font_file_sha256": hashlib.sha256(
                    resolved_font_path.read_bytes()
                ).hexdigest(),
                "label_font_size_pt": float(label_font_size_pt),
                "tick_font_size_pt": float(tick_font_size_pt),
                "line_width_pt": float(line_width_pt),
                "axis_mode": axis_mode,
                "axis_extent": None
                if image_extent is None
                else [float(value) for value in image_extent],
                "x_label": resolved_x_label,
                "y_label": resolved_y_label,
                "interpolation": "nearest",
                "source": "array_not_screenshot",
            },
            "processing_audit": dict(processing_audit or {}),
        }
        if write_parameter_file:
            temporary_parameters.write_text(
                json.dumps(parameter_document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        os.replace(temporary_image, output)
        if write_parameter_file:
            os.replace(temporary_parameters, parameters_path)
    except Exception:
        temporary_image.unlink(missing_ok=True)
        temporary_parameters.unlink(missing_ok=True)
        raise
    finally:
        figure.clear()

    return ImageExportResult(
        image_path=output,
        parameters_path=parameters_path if write_parameter_file else None,
        parameter_document=parameter_document,
        sha256=sha256,
        width_px=width_px,
        height_px=height_px,
        vmin=vmin,
        vmax=vmax,
    )
