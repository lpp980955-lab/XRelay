from __future__ import annotations

import math
from .branding import APP_NAME, VERSION, TITLE, resource_path, artifacts_directory
import json
import re
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import threading
from typing import Iterable

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from saxs_waxs_core.background import (
    BackgroundError,
    BackgroundMode,
    BackgroundRecipe,
    BackgroundResult,
    apply_background,
)
from saxs_waxs_core.processed_data import embedded_geometry
from saxs_waxs_core.readers.detector_io import (SUPPORTED_SUFFIXES,SourceSelectionRequired,
    source_catalog,source_selections,expand_identities,load_plan_sample,selection_suffix,selection_label,supported_detector_path)
from saxs_waxs_core.masking import (
    MaskError,
    MaskRecipe,
    MaskResult,
    UserMaskLayer,
    build_mask,
    load_external_mask_layer,
    suggest_beamstop_support_mask,
    user_mask_layers_from_descriptors,
)
from saxs_waxs_core.integration import (
    AzimuthalIntegrationRecipe,
    IntegrationCurve,
    IntegrationError,
    RadialUnit,
    detector_radial_array,
)
from saxs_waxs_core.ruland_integration import suggest_ruland_sampling
from saxs_waxs_core.processing import (
    BatchProcessingPlan,
    BATCH_PROCESSING_PLAN_SCHEMA,
    BATCH_PROCESSING_PLAN_SCHEMA_V1,
    FileIdentity,
    ProcessingPolicy,
    ProcessingError,
    ProcessingRecipe,
    add_batch_recipe_revision,
    execute_integration_stage,
    execute_processing_recipe,
    batch_processing_plan_from_document,
    load_batch_processing_plan,
    load_processing_recipe,
    save_batch_processing_plan,
    save_processing_recipe,
)
from saxs_waxs_core.batch import (
    BATCH_RUN_SCHEMA,
    LEGACY_BATCH_RUN_SCHEMA,
    BatchRunResult,
    run_batch_plan,
)
from saxs_waxs_core.display import DisplayPreset, normalize_for_display, resolve_display_limits
from saxs_waxs_core.image_export import (
    ImageExportError,
    ImageExportResult,
    export_2d_image,
)
from saxs_waxs_core.image_export_batch import (
    ImageExportBatchError,
    ImageExportBatchResult,
    RawImageExportPlan,
    load_image_export_resume_spec,
    run_image_export_batch,
)
from saxs_waxs_core.presets import ExportPreset
from saxs_waxs_core.project import (
    PROJECT_EXTENSION,
    ProjectError,
    WorkbenchProject,
    export_portable_project,
    load_project,
    project_file_identities,
    relocate_project,
    save_project,
    verify_project_references,
)
from saxs_waxs_core.relocation import resolve_file_relocation
from saxs_waxs_core.discovery import DiscoveredFile, discover_image_files
from saxs_waxs_core.calibrants import Calibrant, visible_rings
from saxs_waxs_core.calibration import (
    CalibrationError,
    CalibrationOptions,
    CalibrationSuggestion,
    fit_manual_calibrant_ring,
    suggest_untilted_geometry,
)
from saxs_waxs_core.geometry import (
    DetectorGeometry,
    GeometryError,
    geometry_to_document,
    scattering_coordinates,
)
from saxs_waxs_core.readers.tvx_tiff import TiffReaderError, read_tvx_tiff
from .colormaps import (
    COLORMAP_CATALOG,
    COLORMAP_KEYS,
    DEFAULT_CUSTOM_COLORS,
    CustomColormapDialog,
    gradient_pixmap,
    make_colormap,
)
from .geometry_panel import GeometryPanel
from .background_panel import BackgroundPanel
from .mask_panel import MaskPanel
from .integration_panel import IntegrationPanel
from .ruland_dialog import RulandDialog
from .saxs_analysis_dialog import SaxsAnalysisDialog
from .debye_bueche_dialog import DebyeBuecheDialog
from saxs_waxs_core.curve_import import import_curve, suggested_curve_name, read_curve_text, prepare_curve_text
from .curve_import_dialog import CurveImportDialog
from saxs_waxs_core.ruland_workflow import run_ruland_workflow, suggest_ruland_peak_centers
from .integration_overlay import IntegrationRegionOverlay
from .peak_fit_panel import PeakFitLauncherPanel, PeakFitWorkspaceWindow
from .recipe_panel import RecipePanel
from .image_export_dialog import ImageExportDialog


GUI_COLORMAPS = COLORMAP_KEYS


def batch_integration_directory_label(plan: BatchProcessingPlan) -> str:
    """Return a stable directory label for the integration kinds in a plan."""

    integration_kinds = {
        "azimuthal_integration"
        if isinstance(plan.recipe_for_index(index).integration, AzimuthalIntegrationRecipe)
        else "radial_integration"
        for index in range(len(plan.sample_sources))
    }
    if len(integration_kinds) == 1:
        return next(iter(integration_kinds))
    return "multi_integration"


class CalibrationWorker(QtCore.QObject):
    """Run pyFAI ring extraction without freezing the Qt interface."""

    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(
        self,
        image: np.ndarray,
        geometry: DetectorGeometry,
        calibrant: Calibrant,
        *,
        coarse_center_enabled: bool = True,
        mask=None,
    ) -> None:
        super().__init__()
        self.image = image
        self.geometry = geometry
        self.calibrant = calibrant
        self.mask = mask
        self.options = CalibrationOptions(
            coarse_center_enabled=coarse_center_enabled
        )

    @QtCore.Slot()
    def run(self) -> None:
        try:
            if self.calibrant is None:
                from saxs_waxs_core.center_finding import suggest_center_only
                suggestion=suggest_center_only(self.image,self.geometry,self.mask)
            else:
                suggestion = suggest_untilted_geometry(
                    self.image,
                    self.geometry,
                    self.calibrant,
                    options=self.options,
                )
        except Exception as exc:  # worker boundary: surface the exact failure in the UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(suggestion)


class BatchExecutionWorker(QtCore.QObject):
    """Execute a reviewed batch without blocking the Qt interface."""

    progress = QtCore.Signal(object)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(
        self,
        plan: BatchProcessingPlan,
        run_directory: Path,
        protected_roots: tuple[Path, ...],
        resume: bool = False,
    ) -> None:
        super().__init__()
        self.plan = plan
        self.run_directory = run_directory
        self.protected_roots = protected_roots
        self.resume = resume
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = run_batch_plan(
                self.plan,
                self.run_directory,
                protected_roots=self.protected_roots,
                resume=self.resume,
                stop_requested=self._stop.is_set,
                progress_callback=self.progress.emit,
            )
        except Exception as exc:  # worker boundary: exact error is surfaced in UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result)


class ImageExportBatchWorker(QtCore.QObject):
    """Export reviewed 2D frames without blocking the interface."""

    progress = QtCore.Signal(object)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(
        self,
        plan: BatchProcessingPlan | RawImageExportPlan,
        run_directory: Path,
        display: DisplayPreset,
        export: ExportPreset,
        protected_roots: tuple[Path, ...],
        resume: bool = False,
    ) -> None:
        super().__init__()
        self.plan = plan
        self.run_directory = run_directory
        self.display = display
        self.export = export
        self.protected_roots = protected_roots
        self.resume = resume
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = run_image_export_batch(
                self.plan,
                self.run_directory,
                self.display,
                self.export,
                protected_roots=self.protected_roots,
                resume=self.resume,
                stop_requested=self._stop.is_set,
                progress_callback=self.progress.emit,
            )
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result)


class FitCurvePreparationWorker(QtCore.QObject):
    """Prepare checked-sample integration curves without blocking the GUI."""

    progress = QtCore.Signal(object)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, plan: BatchProcessingPlan) -> None:
        super().__init__()
        self.plan = plan
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    @QtCore.Slot()
    def run(self) -> None:
        curves: list[tuple[str, IntegrationCurve]] = []
        contexts: list[dict[str, object]] = []
        failures: list[str] = []
        background_cache: dict[str, object] = {}
        total = len(self.plan.sample_sources)
        for index, identity in enumerate(self.plan.sample_sources):
            if self._stop.is_set():
                break
            source = Path(identity.path).expanduser().absolute()
            try:
                self.progress.emit(
                    {
                        "processed": index,
                        "total": total,
                        "passed": len(curves),
                        "failed": len(failures),
                        "current": source.name,
                        "phase": "read",
                    }
                )
                if FileIdentity.from_path(source) != identity:
                    raise ProcessingError("样品文件身份与勾选时不一致")
                sample = load_plan_sample(self.plan,index,read_tvx_tiff)
                recipe = self.plan.recipe_for_index(index)
                background_frame = None
                if recipe.background_source is not None:
                    background_path = str(
                        Path(recipe.background_source.path)
                        .expanduser()
                        .absolute()
                        .resolve(strict=False)
                    ).casefold()
                    background_path+=json.dumps(dict(recipe.background.metadata.get('source_selection',{})),sort_keys=True)
                    if background_path not in background_cache:
                        background_cache[background_path] = read_tvx_tiff(
                            recipe.background_source.path,**recipe.background.metadata.get('source_selection',{})
                        )
                    background_frame = background_cache[background_path]
                self.progress.emit(
                    {
                        "processed": index,
                        "total": total,
                        "passed": len(curves),
                        "failed": len(failures),
                        "current": source.name,
                        "phase": "integrate",
                    }
                )
                execution = execute_processing_recipe(
                    sample,
                    background_frame,
                    recipe,
                )
                curves.append((selection_label(self.plan,index), execution.integration))
                contexts.append(
                    {
                        "source": str(source),
                        "processing": dict(execution.audit),
                        "selection": "left_checked_sample",
                    }
                )
            except Exception as exc:  # isolate one bad input, report exact reason
                failures.append(f"{source.name}: {type(exc).__name__}: {exc}")
            self.progress.emit(
                {
                    "processed": index + 1,
                    "total": total,
                    "passed": len(curves),
                    "failed": len(failures),
                    "current": source.name,
                }
            )
        cancelled = self._stop.is_set()
        if not curves and not cancelled:
            self.failed.emit(
                "勾选样品均未能生成积分曲线：" + "；".join(failures[:3])
            )
            return
        self.finished.emit(
            {
                "curves": curves,
                "contexts": contexts,
                "failures": failures,
                "cancelled": cancelled,
                "requested": total,
            }
        )


def resolve_gui_colormap(
    name: str,
    invert: bool,
    custom_colors: tuple[str, ...] = DEFAULT_CUSTOM_COLORS,
) -> pg.ColorMap:
    return make_colormap(name, invert=invert, custom_colors=custom_colors)


class ToneRangeBar(QtWidgets.QWidget):
    """Compact horizontal color/level bar shared by the active image pane."""

    levels_changed = QtCore.Signal(float, float)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._low = 0.0
        self._high = 1.0
        self._dragging: str | None = None
        self._color_map = resolve_gui_colormap("viridis", False)
        self.setMinimumWidth(240)
        self.setFixedHeight(32)
        self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        self.setToolTip("拖动两端标记调节显示范围")

    @property
    def levels(self) -> tuple[float, float]:
        return self._low, self._high

    def set_levels(self, low: float, high: float, *, emit: bool = False) -> None:
        low = max(0.0, min(1.0, float(low)))
        high = max(0.0, min(1.0, float(high)))
        if low >= high:
            return
        changed = (low, high) != (self._low, self._high)
        self._low, self._high = low, high
        self.update()
        if emit and changed:
            self.levels_changed.emit(low, high)

    def set_colormap(self, color_map: pg.ColorMap) -> None:
        self._color_map = color_map
        self.update()

    def _bar_rect(self) -> QtCore.QRectF:
        return QtCore.QRectF(9.0, 7.0, max(1.0, self.width() - 18.0), 16.0)

    def _position_to_level(self, x: float) -> float:
        rect = self._bar_rect()
        return max(0.0, min(1.0, (x - rect.left()) / rect.width()))

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802 - Qt API
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = self._bar_rect()
        gradient = QtGui.QLinearGradient(rect.left(), 0.0, rect.right(), 0.0)
        table = self._color_map.getLookupTable(nPts=64, alpha=False)
        for index, rgb in enumerate(table):
            gradient.setColorAt(index / (len(table) - 1), QtGui.QColor(*map(int, rgb[:3])))
        painter.setPen(QtGui.QPen(QtGui.QColor("#666666"), 1.0))
        painter.setBrush(QtGui.QBrush(gradient))
        painter.drawRoundedRect(rect, 2.0, 2.0)
        low_x = rect.left() + self._low * rect.width()
        high_x = rect.left() + self._high * rect.width()
        shade = QtGui.QColor(0, 0, 0, 105)
        painter.fillRect(QtCore.QRectF(rect.left(), rect.top(), low_x - rect.left(), rect.height()), shade)
        painter.fillRect(QtCore.QRectF(high_x, rect.top(), rect.right() - high_x, rect.height()), shade)
        for x in (low_x, high_x):
            painter.setPen(QtGui.QPen(QtGui.QColor("white"), 2.0))
            painter.drawLine(QtCore.QPointF(x, 4.0), QtCore.QPointF(x, 27.0))
            painter.setPen(QtGui.QPen(QtGui.QColor("#202020"), 1.0))
            painter.drawLine(QtCore.QPointF(x + 2.0, 4.0), QtCore.QPointF(x + 2.0, 27.0))
        painter.end()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 - Qt API
        rect = self._bar_rect()
        low_x = rect.left() + self._low * rect.width()
        high_x = rect.left() + self._high * rect.width()
        self._dragging = "low" if abs(event.position().x() - low_x) <= abs(event.position().x() - high_x) else "high"
        self._move_handle(event.position().x())

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 - Qt API
        if self._dragging is not None:
            self._move_handle(event.position().x())

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 - Qt API
        del event
        self._dragging = None

    def _move_handle(self, x: float) -> None:
        value = self._position_to_level(x)
        gap = 0.001
        if self._dragging == "low":
            self.set_levels(min(value, self._high - gap), self._high, emit=True)
        elif self._dragging == "high":
            self.set_levels(self._low, max(value, self._low + gap), emit=True)


class FineTuneSplitterHandle(QtWidgets.QSplitterHandle):
    """Splitter handle with one-pixel keyboard adjustment."""

    def __init__(
        self,
        orientation: QtCore.Qt.Orientation,
        parent: QtWidgets.QSplitter,
    ) -> None:
        super().__init__(orientation, parent)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setToolTip("拖动调整；聚焦后方向键微调1 px，Shift+方向键调整10 px")

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802 - Qt API
        horizontal = self.orientation() == QtCore.Qt.Orientation.Horizontal
        negative_key = QtCore.Qt.Key.Key_Left if horizontal else QtCore.Qt.Key.Key_Up
        positive_key = QtCore.Qt.Key.Key_Right if horizontal else QtCore.Qt.Key.Key_Down
        if event.key() not in {negative_key, positive_key}:
            super().keyPressEvent(event)
            return
        splitter = self.splitter()
        handle_index = next(
            (
                index
                for index in range(1, splitter.count())
                if splitter.handle(index) is self
            ),
            None,
        )
        if handle_index is None:
            return
        step = 10 if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier else 1
        direction = -1 if event.key() == negative_key else 1
        position = self.pos().x() if horizontal else self.pos().y()
        splitter.moveSplitter(position + direction * step, handle_index)
        event.accept()


class FineTuneSplitter(QtWidgets.QSplitter):
    """Live splitter with a generous mouse target and precise key control."""

    def createHandle(self) -> QtWidgets.QSplitterHandle:  # noqa: N802 - Qt API
        return FineTuneSplitterHandle(self.orientation(), self)


class HorizontallyUnconstrainedWidget(QtWidgets.QWidget):
    """A splitter child that may be clipped down to zero horizontal pixels."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt API
        return QtCore.QSize(0, 0)


class ImageViewerWidget(QtWidgets.QWidget):
    """Small, reusable 2D viewer; scientific arrays remain read-only inputs."""

    preset_changed = QtCore.Signal(object)
    geometry_center_dragged = QtCore.Signal(float, float)
    geometry_center_drag_finished = QtCore.Signal(float, float)
    geometry_distance_dragged = QtCore.Signal(float)
    geometry_distance_drag_finished = QtCore.Signal(float)
    manual_ring_point_picked = QtCore.Signal(float, float)
    manual_mask_changed = QtCore.Signal(object)
    integration_region_changed = QtCore.Signal(object)
    activated = QtCore.Signal()

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        show_controls: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        pg.setConfigOption("imageAxisOrder", "row-major")
        self._data: np.ndarray | None = None
        self._normalized = None
        self._display_mask: np.ndarray | None = None
        self._mask: np.ndarray | None = None
        self._manual_mask_rois: list[tuple[str, pg.ROI]] = []
        self._selected_manual_mask_roi: pg.ROI | None = None
        self._manual_mask_regions_visible = True
        self._geometry_overlays_visible = True
        self._updating_manual_masks = False
        self._manual_polygon_kind: str | None = None
        self._manual_polygon_points: list[QtCore.QPointF] = []
        self._manual_polygon_preview: QtWidgets.QGraphicsPathItem | None = None
        self._preset = DisplayPreset()
        self._geometry_overlay_items: list[QtWidgets.QGraphicsItem] = []
        self._geometry_center_target: pg.TargetItem | None = None
        self._geometry_ring_items: list[tuple[QtWidgets.QGraphicsEllipseItem, float, float]] = []
        self._updating_geometry_target = False
        self._geometry_mouse_drag_active = False
        self._geometry_ring_drag_item: QtWidgets.QGraphicsEllipseItem | None = None
        self._geometry_ring_drag_start_distance_m = 0.0
        self._geometry_ring_drag_start_radius = (1.0, 1.0)
        self._geometry_mouse_drag_offset = QtCore.QPointF()
        self._geometry_candidate_distance_m = 0.0
        self._manual_ring_pick_active = False
        self._manual_ring_pick_items: list[QtWidgets.QGraphicsItem] = []
        self._inspection_geometry: DetectorGeometry | None = None
        self._inspection_map_cache: dict[str, np.ndarray] = {}
        self._base_status_text = "尚未载入图像"

        self.plot_item = pg.PlotItem()
        self.image_view = pg.ImageView(view=self.plot_item)
        self._mask_overlay_item = pg.ImageItem(axisOrder="row-major")
        self._mask_overlay_item.setZValue(20)
        self._mask_overlay_item.hide()
        self.plot_item.addItem(self._mask_overlay_item)
        self.integration_overlay = IntegrationRegionOverlay(self.plot_item, self)
        self.integration_overlay.region_changed.connect(
            self.integration_region_changed
        )
        self.plot_item.scene().sigMouseClicked.connect(self._geometry_scene_clicked)
        self.plot_item.scene().sigMouseClicked.connect(self._auxiliary_scene_clicked)
        self._inspection_mouse_proxy = pg.SignalProxy(
            self.plot_item.scene().sigMouseMoved,
            rateLimit=60,
            slot=self._image_mouse_moved,
        )
        self.image_view.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        # Every pane previously exposed its own vertical histogram/LUT and the
        # stock ROI/Menu buttons. The workbench owns shared controls, while its
        # scientific integration ROI will be implemented separately in Phase 3.
        # Hide all widgets in ImageView's auxiliary columns so those columns
        # collapse instead of leaving a large blank strip in narrow panes.
        self.image_view.ui.histogram.hide()
        self.image_view.ui.roiBtn.hide()
        self.image_view.ui.menuBtn.hide()
        self.image_view.ui.gridLayout.setColumnStretch(0, 1)
        self.image_view.ui.gridLayout.setColumnStretch(1, 0)
        self.image_view.ui.gridLayout.setColumnStretch(2, 0)
        self.image_view.ui.gridLayout.setColumnMinimumWidth(1, 0)
        self.image_view.ui.gridLayout.setColumnMinimumWidth(2, 0)
        # Detector inspection must not silently trade spatial detail for
        # rendering speed. Keep the full source raster and use pixel-sharp Qt
        # scaling; the user can switch to a true 1:1 view when fit-to-window
        # necessarily maps multiple detector pixels onto one screen pixel.
        self.image_view.imageItem.setAutoDownsample(False)
        self.image_view.ui.graphicsView.setRenderHint(
            QtGui.QPainter.RenderHint.SmoothPixmapTransform,
            False,
        )
        self.image_view.ui.graphicsView.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.image_view.ui.graphicsView.installEventFilter(self)
        self.image_view.ui.graphicsView.viewport().installEventFilter(self)
        self.plot_item.setLabel("bottom", "x (column / pixel)")
        self.plot_item.setLabel("left", "y (row / pixel)")

        self.colormap_combo = QtWidgets.QComboBox()
        self.colormap_combo.setIconSize(QtCore.QSize(72, 12))
        self.colormap_combo.setMinimumWidth(150)
        self.colormap_combo.setMaximumWidth(220)
        last_category: str | None = None
        for entry in COLORMAP_CATALOG:
            if last_category is not None and entry.category != last_category:
                self.colormap_combo.insertSeparator(self.colormap_combo.count())
            icon = QtGui.QIcon(gradient_pixmap(entry.key, width=72, height=12))
            self.colormap_combo.addItem(icon, entry.label, entry.key)
            index = self.colormap_combo.count() - 1
            self.colormap_combo.setItemData(index, entry.note, QtCore.Qt.ItemDataRole.ToolTipRole)
            last_category = entry.category
        self.custom_colormap_button = QtWidgets.QPushButton("自定义…")
        self.scale_combo = QtWidgets.QComboBox()
        self.scale_combo.addItems(("linear", "log"))
        self.invert_check = QtWidgets.QCheckBox("反转配色")
        self.range_mode_combo = QtWidgets.QComboBox()
        self.range_mode_combo.addItem("自动百分位", "percentile")
        self.range_mode_combo.addItem("手动强度", "manual")
        self.lower_spin = QtWidgets.QDoubleSpinBox()
        self.lower_spin.setRange(0.0, 99.98)
        self.lower_spin.setDecimals(2)
        self.lower_spin.setValue(1.0)
        self.lower_spin.setSuffix(" %")
        self.lower_spin.setFixedWidth(76)
        self.upper_spin = QtWidgets.QDoubleSpinBox()
        self.upper_spin.setRange(0.01, 100.0)
        self.upper_spin.setDecimals(2)
        self.upper_spin.setValue(99.5)
        self.upper_spin.setSuffix(" %")
        self.upper_spin.setFixedWidth(76)
        percentile_controls = QtWidgets.QWidget()
        percentile_layout = QtWidgets.QHBoxLayout(percentile_controls)
        percentile_layout.setContentsMargins(0, 0, 0, 0)
        percentile_layout.addWidget(QtWidgets.QLabel("下限"))
        percentile_layout.addWidget(self.lower_spin)
        percentile_layout.addWidget(QtWidgets.QLabel("上限"))
        percentile_layout.addWidget(self.upper_spin)

        self.manual_min_spin = QtWidgets.QDoubleSpinBox()
        self.manual_min_spin.setRange(-1e30, 1e30)
        self.manual_min_spin.setDecimals(6)
        self.manual_min_spin.setValue(0.0)
        self.manual_min_spin.setFixedWidth(92)
        self.manual_max_spin = QtWidgets.QDoubleSpinBox()
        self.manual_max_spin.setRange(-1e30, 1e30)
        self.manual_max_spin.setDecimals(6)
        self.manual_max_spin.setValue(1.0)
        self.manual_max_spin.setFixedWidth(92)
        manual_controls = QtWidgets.QWidget()
        manual_layout = QtWidgets.QHBoxLayout(manual_controls)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(QtWidgets.QLabel("最小"))
        manual_layout.addWidget(self.manual_min_spin)
        manual_layout.addWidget(QtWidgets.QLabel("最大"))
        manual_layout.addWidget(self.manual_max_spin)

        self.range_stack = QtWidgets.QStackedWidget()
        self.range_stack.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Maximum,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.range_stack.addWidget(percentile_controls)
        self.range_stack.addWidget(manual_controls)

        self.display_options_button = QtWidgets.QToolButton()
        self.display_options_button.setText("显示设置")
        self.display_options_button.setArrowType(QtCore.Qt.ArrowType.DownArrow)
        self.display_options_button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.display_options_button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.display_options_menu = QtWidgets.QMenu(self.display_options_button)
        display_options = QtWidgets.QWidget()
        display_options.setMinimumWidth(330)
        options_form = QtWidgets.QFormLayout(display_options)
        options_form.setContentsMargins(10, 8, 10, 8)
        color_row = QtWidgets.QWidget()
        color_layout = QtWidgets.QHBoxLayout(color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.addWidget(self.colormap_combo, 1)
        color_layout.addWidget(self.custom_colormap_button)
        scale_row = QtWidgets.QWidget()
        scale_layout = QtWidgets.QHBoxLayout(scale_row)
        scale_layout.setContentsMargins(0, 0, 0, 0)
        scale_layout.addWidget(self.scale_combo, 1)
        scale_layout.addWidget(self.invert_check)
        options_form.addRow("配色", color_row)
        options_form.addRow("色阶", scale_row)
        options_form.addRow("范围", self.range_mode_combo)
        options_form.addRow("上下限", self.range_stack)
        display_options_action = QtWidgets.QWidgetAction(self.display_options_menu)
        display_options_action.setDefaultWidget(display_options)
        self.display_options_menu.addAction(display_options_action)
        self.display_options_button.setMenu(self.display_options_menu)

        self.fit_view_button = QtWidgets.QToolButton()
        self.fit_view_button.setText("适应")
        self.fit_view_button.setToolTip("显示完整图像；屏幕小于原图时会缩小显示")
        self.actual_pixels_button = QtWidgets.QToolButton()
        self.actual_pixels_button.setText("1:1")
        self.actual_pixels_button.setToolTip("一个探测器像素对应一个屏幕逻辑像素")
        self.coordinate_combo = QtWidgets.QComboBox()
        self.coordinate_combo.addItem("自动坐标", "auto")
        self.coordinate_combo.addItem("像素 x/y", "pixel")
        self.coordinate_combo.addItem("q (nm⁻¹)", RadialUnit.Q_NM_INV.value)
        self.coordinate_combo.addItem("q (Å⁻¹)", RadialUnit.Q_ANGSTROM_INV.value)
        self.coordinate_combo.addItem("s (nm⁻¹)", RadialUnit.S_NM_INV.value)
        self.coordinate_combo.addItem("2θ (°)", RadialUnit.TWO_THETA_DEG.value)
        self.coordinate_combo.setMaximumWidth(118)
        self.coordinate_combo.setToolTip(
            "鼠标悬停读数；自动模式在已应用几何时显示q，否则显示像素坐标"
        )
        self.status_label = QtWidgets.QLabel("尚未载入图像")

        self.controls_widget = QtWidgets.QWidget()
        controls = QtWidgets.QHBoxLayout(self.controls_widget)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self.display_options_button)
        controls.addWidget(self.coordinate_combo)
        controls.addWidget(self.fit_view_button)
        controls.addWidget(self.actual_pixels_button)
        self.controls_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Maximum,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.controls_widget)
        layout.addWidget(self.image_view, 1)
        layout.addWidget(self.status_label)

        self.colormap_combo.currentTextChanged.connect(self._controls_changed)
        self.custom_colormap_button.clicked.connect(self._edit_custom_colormap)
        self.scale_combo.currentTextChanged.connect(self._controls_changed)
        self.invert_check.toggled.connect(self._controls_changed)
        self.range_mode_combo.currentIndexChanged.connect(self._range_mode_changed)
        self.lower_spin.valueChanged.connect(self._controls_changed)
        self.upper_spin.valueChanged.connect(self._controls_changed)
        self.manual_min_spin.valueChanged.connect(self._controls_changed)
        self.manual_max_spin.valueChanged.connect(self._controls_changed)
        self.fit_view_button.clicked.connect(self.fit_image_to_view_interactively)
        self.actual_pixels_button.clicked.connect(self.show_actual_pixels)
        self.coordinate_combo.currentIndexChanged.connect(self._inspection_mode_changed)
        if not show_controls:
            self.controls_widget.hide()
        self.custom_colormap_button.setEnabled(False)
        self._graphics_shutdown = False
        self._fit_timer = QtCore.QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(80)
        self._fit_timer.timeout.connect(self.fit_image_to_view)

    @property
    def preset(self) -> DisplayPreset:
        return self._preset

    @property
    def data(self) -> np.ndarray | None:
        return self._data

    @property
    def mask(self) -> np.ndarray | None:
        if self._mask is None:
            return self._display_mask
        if self._display_mask is not None:
            combined=self._mask | self._display_mask
            combined.setflags(write=False)
            return combined
        return self._mask

    def set_preset(self, preset: DisplayPreset) -> None:
        if preset.colormap not in GUI_COLORMAPS:
            raise ValueError(f"GUI colormap is not available: {preset.colormap}")
        widgets = (
            self.colormap_combo,
            self.scale_combo,
            self.invert_check,
            self.range_mode_combo,
            self.lower_spin,
            self.upper_spin,
            self.manual_min_spin,
            self.manual_max_spin,
        )
        blockers = [QtCore.QSignalBlocker(widget) for widget in widgets]
        colormap_index = self.colormap_combo.findData(preset.colormap)
        if colormap_index < 0:
            raise ValueError(f"GUI colormap is not available: {preset.colormap}")
        self.colormap_combo.setCurrentIndex(colormap_index)
        self.scale_combo.setCurrentText(preset.scale)
        self.invert_check.setChecked(preset.invert_colormap)
        mode_index = self.range_mode_combo.findData(preset.limit_mode)
        if mode_index < 0:
            raise ValueError(f"GUI limit mode is not available: {preset.limit_mode}")
        self.range_mode_combo.setCurrentIndex(mode_index)
        self.range_stack.setCurrentIndex(mode_index)
        self.lower_spin.setValue(preset.lower_percentile)
        self.upper_spin.setValue(preset.upper_percentile)
        if preset.manual_vmin is not None:
            self.manual_min_spin.setValue(preset.manual_vmin)
        if preset.manual_vmax is not None:
            self.manual_max_spin.setValue(preset.manual_vmax)
        self._preset = preset
        self.custom_colormap_button.setEnabled(preset.colormap == "custom")
        del blockers
        self._render()

    def set_data(self, data: np.ndarray, display_mask: np.ndarray | None = None,
                 *, normalized=None, fit=True) -> None:
        array = np.asarray(data)
        if array.ndim != 2:
            raise ValueError("Viewer data must be a 2D array")
        excluded=None if display_mask is None else np.array(display_mask,dtype=bool,copy=True)
        if excluded is not None:
            if excluded.shape!=array.shape:
                raise ValueError("Display mask must match viewer data")
            excluded.setflags(write=False)
        self.cancel_manual_polygon_drawing()
        self.clear_geometry_overlay()
        self.clear_mask_overlay()
        self.clear_integration_overlay()
        self._data = array
        self._normalized = None
        self._display_mask = excluded
        self._base_display_mask = excluded
        self._inspection_map_cache.clear()
        if (
            self._inspection_geometry is not None
            and (
                self._inspection_geometry.image_width_px != array.shape[1]
                or self._inspection_geometry.image_height_px != array.shape[0]
            )
        ):
            self._inspection_geometry = None
        valid=np.isfinite(array)
        if excluded is not None:
            valid &= ~excluded
        finite = array[valid]
        if finite.size:
            minimum = float(finite.min())
            maximum = float(finite.max())
            if self._preset.limit_mode=="manual":
                minimum=self._preset.manual_vmin
                maximum=self._preset.manual_vmax
            if minimum == maximum:
                maximum = minimum + max(abs(minimum) * 1e-6, 1.0)
            for widget, value in (
                (self.manual_min_spin, minimum),
                (self.manual_max_spin, maximum),
            ):
                widget.blockSignals(True)
                widget.setValue(value)
                widget.blockSignals(False)
        self._render(fit=fit,normalized=normalized)

    def set_display_mask(self, excluded: np.ndarray | None) -> None:
        """Refresh valid display pixels without resetting geometry or regions."""
        mask = None if excluded is None else np.array(excluded, dtype=bool, copy=True)
        if mask is not None:
            if self._data is None or mask.shape != self._data.shape:
                raise ValueError("Display mask must match current data")
            mask.setflags(write=False)
        self._display_mask = mask
        self._normalized = None
        self._render(fit=False)

    def set_inspection_geometry(self, geometry: DetectorGeometry | None) -> None:
        """Set the applied geometry used only for read-only pointer inspection."""

        if geometry is not None and self._data is not None:
            if (
                geometry.image_width_px != self._data.shape[1]
                or geometry.image_height_px != self._data.shape[0]
            ):
                geometry = None
        self._inspection_geometry = geometry
        self._inspection_map_cache.clear()

    @QtCore.Slot()
    def _inspection_mode_changed(self) -> None:
        if self._data is not None:
            self.status_label.setText(self._base_status_text)

    def _inspection_radial_value(self, row: int, column: int, mode: str) -> float:
        geometry = self._inspection_geometry
        if geometry is None:
            raise GeometryError("尚未应用几何参数")
        if geometry.is_untilted:
            coordinates = scattering_coordinates(
                geometry,
                float(column) + 0.5,
                float(row) + 0.5,
            )
            if mode == RadialUnit.Q_NM_INV.value:
                return float(coordinates.q_nm_inv)
            if mode == RadialUnit.Q_ANGSTROM_INV.value:
                return float(coordinates.q_nm_inv) / 10.0
            if mode == RadialUnit.S_NM_INV.value:
                return float(coordinates.s_nm_inv)
            if mode == RadialUnit.TWO_THETA_DEG.value:
                return math.degrees(float(coordinates.two_theta_rad))
        # Pointer readout needs one detector point, not an 18-million-pixel map.
        from saxs_waxs_core.pyfai_adapter import pyfai_scattering_coordinates
        coordinates=pyfai_scattering_coordinates(geometry,float(column)+.5,float(row)+.5)
        if mode == RadialUnit.TWO_THETA_DEG.value:
            return math.degrees(float(coordinates.two_theta_rad))
        value=float(coordinates.q_nm_inv)
        if mode == RadialUnit.Q_ANGSTROM_INV.value:return value/10.
        if mode == RadialUnit.S_NM_INV.value:return value/(2.*math.pi)
        return value

    def inspection_readout_at(
        self, x_view: float, y_view: float
    ) -> dict[str, object] | None:
        """Return the displayed pixel/intensity/scattering readout without mutation."""

        if self._data is None:
            return None
        column = int(math.floor(float(x_view)))
        row = int(math.floor(float(y_view)))
        if row < 0 or column < 0 or row >= self._data.shape[0] or column >= self._data.shape[1]:
            return None
        requested = str(self.coordinate_combo.currentData())
        mode = requested
        if requested == "auto":
            mode = RadialUnit.Q_NM_INV.value if self._inspection_geometry is not None else "pixel"
        if mode != "pixel" and self._inspection_geometry is None:
            mode = "pixel"
        result: dict[str, object] = {
            "row": row,
            "column": column,
            "intensity": float(self._data[row, column]),
            "masked": bool(self._mask is not None and self._mask[row, column] or
                           self._display_mask is not None and self._display_mask[row,column]),
            "mode": mode,
        }
        if mode != "pixel":
            result["value"] = self._inspection_radial_value(row, column, mode)
        return result

    @staticmethod
    def _format_inspection_number(value: float) -> str:
        return f"{float(value):.7g}"

    @QtCore.Slot(object)
    def _image_mouse_moved(self, event: object) -> None:
        scene_position = event[0] if isinstance(event, tuple) else event
        if not isinstance(scene_position, QtCore.QPointF):
            return
        if not self.image_view.imageItem.sceneBoundingRect().contains(scene_position):
            self.status_label.setText(self._base_status_text)
            return
        view_position = self.plot_item.getViewBox().mapSceneToView(scene_position)
        try:
            readout = self.inspection_readout_at(view_position.x(), view_position.y())
        except (GeometryError, IntegrationError) as exc:
            self.status_label.setText(f"坐标读取失败：{exc}")
            return
        if readout is None:
            self.status_label.setText(self._base_status_text)
            return
        intensity = self._format_inspection_number(float(readout["intensity"]))
        pixel_text = f"x={readout['column']} px, y={readout['row']} px"
        mode = str(readout["mode"])
        if mode == "pixel":
            text = f"{pixel_text} | I={intensity}"
        else:
            label, unit = {
                RadialUnit.Q_NM_INV.value: ("q", "nm⁻¹"),
                RadialUnit.Q_ANGSTROM_INV.value: ("q", "Å⁻¹"),
                RadialUnit.S_NM_INV.value: ("s", "nm⁻¹"),
                RadialUnit.TWO_THETA_DEG.value: ("2θ", "°"),
            }[mode]
            value = self._format_inspection_number(float(readout["value"]))
            text = f"{label}={value} {unit} | I={intensity} | {pixel_text}"
        if bool(readout["masked"]):
            text += " | MASK"
        self.status_label.setText(text)

    def set_mask_overlay(self, mask: np.ndarray) -> None:
        if self._data is None:
            raise ValueError("Cannot display a mask before image data is loaded")
        excluded = np.array(mask, dtype=bool, order="C", copy=True)
        if excluded.shape != self._data.shape:
            raise ValueError("Mask shape must match viewer data")
        excluded.setflags(write=False)
        self._mask = excluded
        stride=max(1,math.ceil(max(excluded.shape)/1024))
        display=excluded
        if stride>1:
            height=math.ceil(excluded.shape[0]/stride)
            width=math.ceil(excluded.shape[1]/stride)
            display=np.pad(excluded,((0,height*stride-excluded.shape[0]),(0,width*stride-excluded.shape[1])))
            display=display.reshape(height,stride,width,stride).any(axis=(1,3))
        rgba = np.zeros(display.shape + (4,), dtype=np.uint8)
        rgba[display] = (255, 48, 48, 112)
        self._mask_overlay_item.setImage(rgba, autoLevels=False)
        self._mask_overlay_item.setRect(QtCore.QRectF(0,0,display.shape[1]*stride,display.shape[0]*stride))
        self._mask_overlay_item.show()

    def clear_mask_overlay(self) -> None:
        self._mask = None
        self._mask_overlay_item.clear()
        self._mask_overlay_item.hide()
        if hasattr(self, "_base_display_mask") and self._display_mask is not self._base_display_mask:
            self._display_mask = self._base_display_mask
            self._normalized = None
            self._render(fit=False)

    def set_integration_overlay(
        self,
        descriptor: dict[str, object],
        geometry: DetectorGeometry,
        *,
        radial_map: np.ndarray | None = None,
    ) -> None:
        if self._data is None:
            raise ValueError("Cannot display an integration region before image data is loaded")
        if self._data.shape != (
            geometry.image_height_px,
            geometry.image_width_px,
        ):
            raise ValueError("Integration geometry dimensions must match viewer data")
        self.integration_overlay.set_region(
            descriptor,
            shape=self._data.shape,
            center_xy=(geometry.beam_center_x_px, geometry.beam_center_y_px),
            radial_map=radial_map,
            geometry=geometry,
        )

    def clear_integration_overlay(self) -> None:
        self.integration_overlay.clear()

    @property
    def manual_mask_region_count(self) -> int:
        return len(self._manual_mask_rois)

    def _default_manual_mask_descriptor(self, kind: str) -> dict[str, object]:
        if self._data is None:
            raise ValueError("Cannot add a mask region before image data is loaded")
        height, width = self._data.shape
        center_x, center_y = width / 2.0, height / 2.0
        scale = max(8.0, min(width, height) * 0.12)
        if kind == "circle":
            return {
                "kind": kind,
                "pos": (center_x - scale / 2.0, center_y - scale / 2.0),
                "size": (scale, scale),
            }
        if kind == "rectangle":
            return {
                "kind": kind,
                "pos": (center_x - scale, center_y - scale / 2.0),
                "size": (2.0 * scale, scale),
            }
        if kind in {
            "occlusion_left",
            "occlusion_right",
            "occlusion_top",
            "occlusion_bottom",
        }:
            right_start = int(math.ceil(width / 2.0))
            bottom_start = int(math.ceil(height / 2.0))
            last_x = float(width - 1)
            last_y = float(height - 1)
            rectangles = {
                "occlusion_left": (
                    (0.0, 0.0),
                    (float(right_start - 1), last_y),
                ),
                "occlusion_right": (
                    (float(right_start), 0.0),
                    (last_x - right_start, last_y),
                ),
                "occlusion_top": (
                    (0.0, 0.0),
                    (last_x, float(bottom_start - 1)),
                ),
                "occlusion_bottom": (
                    (0.0, float(bottom_start)),
                    (last_x, last_y - bottom_start),
                ),
            }
            pos, size = rectangles[kind]
            return {"kind": "occlusion_rectangle", "pos": pos, "size": size}
        if kind == "occlusion_rectangle":
            return {
                "kind": kind,
                "pos": (center_x - scale, center_y - scale / 2.0),
                "size": (2.0 * scale, scale),
            }
        if kind == "polygon":
            return {
                "kind": kind,
                "points": (
                    (center_x, center_y - scale),
                    (center_x + scale, center_y),
                    (center_x, center_y + scale),
                    (center_x - scale, center_y),
                ),
            }
        if kind == "occlusion_polygon":
            return {
                "kind": kind,
                "points": (
                    (center_x - scale, center_y - scale),
                    (center_x + scale, center_y - scale),
                    (center_x + scale, center_y + scale),
                    (center_x - scale, center_y + scale),
                ),
            }
        raise ValueError(f"Unsupported manual mask region: {kind}")

    def add_manual_mask_region(
        self,
        kind: str,
        descriptor: dict[str, object] | None = None,
        *,
        emit: bool = True,
    ) -> None:
        definition = descriptor or self._default_manual_mask_descriptor(kind)
        kind = str(definition.get("kind", kind))
        occlusion = kind.startswith("occlusion_")
        pen = pg.mkPen(
            "#D13438" if occlusion else "#FF7A00",
            width=2,
            style=QtCore.Qt.PenStyle.DashLine if occlusion else QtCore.Qt.PenStyle.SolidLine,
        )
        hover_pen = pg.mkPen("#FF99A0" if occlusion else "#FFD166", width=3)
        if kind == "circle":
            roi = pg.CircleROI(
                definition["pos"],
                definition["size"],
                pen=pen,
                hoverPen=hover_pen,
                removable=True,
            )
        elif kind in {"rectangle", "occlusion_rectangle"}:
            roi = pg.RectROI(
                definition["pos"],
                definition["size"],
                pen=pen,
                hoverPen=hover_pen,
                rotatable=True,
                removable=True,
            )
            roi.addRotateHandle((1.0, 0.0), (0.5, 0.5))
            roi.setToolTip(
                "拖动主体移动、角点缩放、菱形手柄旋转；右键可移除"
            )
        elif kind in {"polygon", "occlusion_polygon"}:
            roi = pg.PolyLineROI(
                definition["points"],
                closed=True,
                pen=pen,
                hoverPen=hover_pen,
                removable=True,
            )
            roi.addRotateHandle((1.0, 0.5), (0.5, 0.5))
            roi.setToolTip(
                "拖动节点修改轮廓、拖动菱形手柄整体旋转；右键可移除"
            )
        else:
            raise ValueError(f"Unsupported manual mask region: {kind}")
        roi.setZValue(30)
        roi._workbench_base_pen = pen  # type: ignore[attr-defined]
        roi.sigRegionChangeFinished.connect(self._manual_mask_roi_finished)
        roi.sigRemoveRequested.connect(lambda item=roi: self._remove_manual_mask_roi(item))
        roi.sigClicked.connect(
            lambda *_args, item=roi: self._select_manual_mask_roi(item)
        )
        self.plot_item.addItem(roi)
        roi.setVisible(self._manual_mask_regions_visible)
        self._manual_mask_rois.append((kind, roi))
        if emit and not self._updating_manual_masks:
            self.manual_mask_changed.emit(self.manual_mask_descriptors())

    def _remove_manual_mask_roi(self, target: pg.ROI) -> None:
        for index, (_kind, roi) in enumerate(self._manual_mask_rois):
            if roi is target:
                self.plot_item.removeItem(roi)
                del self._manual_mask_rois[index]
                if self._selected_manual_mask_roi is roi:
                    self._selected_manual_mask_roi = None
                self.clear_mask_overlay()
                self.manual_mask_changed.emit(self.manual_mask_descriptors())
                return

    def _select_manual_mask_roi(self, target: pg.ROI | None) -> None:
        self._selected_manual_mask_roi = (
            target
            if target is not None
            and any(roi is target for _kind, roi in self._manual_mask_rois)
            else None
        )
        for _kind, roi in self._manual_mask_rois:
            base_pen = getattr(roi, "_workbench_base_pen", roi.pen)
            roi.setPen(
                pg.mkPen("#FFD166", width=4)
                if roi is self._selected_manual_mask_roi
                else base_pen
            )
        if self._selected_manual_mask_roi is not None:
            self.integration_overlay.set_selected(False)

    @QtCore.Slot(object)
    def _auxiliary_scene_clicked(self, event: object) -> None:
        if not hasattr(event, "scenePos"):
            return
        try:
            button = event.button()
        except (AttributeError, RuntimeError):
            return
        if button != QtCore.Qt.MouseButton.LeftButton:
            return
        scene_position = event.scenePos()
        if self.integration_overlay.contains_scene_point(scene_position):
            self._select_manual_mask_roi(None)
            self.integration_overlay.set_selected(True)
        elif self._selected_manual_mask_roi is None:
            self.integration_overlay.set_selected(False)

    @QtCore.Slot()
    def _manual_mask_roi_finished(self) -> None:
        if self._updating_manual_masks:
            return
        self.clear_mask_overlay()
        self.manual_mask_changed.emit(self.manual_mask_descriptors())

    def manual_mask_descriptors(self) -> tuple[dict[str, object], ...]:
        descriptors: list[dict[str, object]] = []
        for kind, roi in self._manual_mask_rois:
            state = roi.getState()
            if kind == "circle":
                descriptors.append(
                    {
                        "kind": kind,
                        "pos": (float(state["pos"].x()), float(state["pos"].y())),
                        "size": (float(state["size"].x()), float(state["size"].y())),
                    }
                )
            elif kind in {"rectangle", "occlusion_rectangle"}:
                angle = float(state.get("angle", 0.0))
                if math.isclose(angle % 360.0, 0.0, abs_tol=1e-9):
                    descriptors.append(
                        {
                            "kind": kind,
                            "pos": (float(state["pos"].x()), float(state["pos"].y())),
                            "size": (
                                float(state["size"].x()),
                                float(state["size"].y()),
                            ),
                        }
                    )
                else:
                    size = state["size"]
                    points = tuple(
                        (
                            float(roi.mapToParent(point).x()),
                            float(roi.mapToParent(point).y()),
                        )
                        for point in (
                            QtCore.QPointF(0.0, 0.0),
                            QtCore.QPointF(float(size.x()), 0.0),
                            QtCore.QPointF(float(size.x()), float(size.y())),
                            QtCore.QPointF(0.0, float(size.y())),
                        )
                    )
                    descriptors.append(
                        {
                            "kind": "occlusion_polygon"
                            if kind == "occlusion_rectangle"
                            else "polygon",
                            "points": points,
                        }
                    )
            else:
                points = tuple(
                    (
                        float(roi.mapToParent(handle["pos"]).x()),
                        float(roi.mapToParent(handle["pos"]).y()),
                    )
                    for handle in roi.handles
                    if handle.get("type") == "f"
                )
                descriptors.append({"kind": kind, "points": points})
        return tuple(descriptors)

    @property
    def manual_polygon_drawing_active(self) -> bool:
        return self._manual_polygon_kind is not None

    def begin_manual_polygon_drawing(self, kind: str = "polygon") -> None:
        if kind not in {"polygon", "occlusion_polygon"}:
            raise ValueError(f"Unsupported polygon drawing kind: {kind}")
        self.cancel_manual_polygon_drawing()
        self._manual_polygon_kind = kind
        self._manual_polygon_points = []
        preview = QtWidgets.QGraphicsPathItem()
        preview.setPen(
            pg.mkPen(
                "#D13438" if kind.startswith("occlusion_") else "#FF7A00",
                width=2,
                style=QtCore.Qt.PenStyle.DashLine,
            )
        )
        preview.setZValue(31)
        self.plot_item.addItem(preview)
        self._manual_polygon_preview = preview
        self.image_view.ui.graphicsView.setCursor(QtCore.Qt.CursorShape.CrossCursor)

    def add_manual_polygon_point(self, x_px: float, y_px: float) -> None:
        if self._manual_polygon_kind is None:
            raise ValueError("Polygon drawing has not been started")
        point = QtCore.QPointF(float(x_px), float(y_px))
        if not np.isfinite([point.x(), point.y()]).all():
            raise ValueError("Polygon point must be finite")
        self._manual_polygon_points.append(point)
        self._update_manual_polygon_preview()

    def finish_manual_polygon_drawing(self) -> bool:
        kind = self._manual_polygon_kind
        points = tuple((point.x(), point.y()) for point in self._manual_polygon_points)
        if kind is None or len(points) < 3:
            return False
        self._clear_manual_polygon_preview()
        self.add_manual_mask_region(kind, {"kind": kind, "points": points})
        return True

    def cancel_manual_polygon_drawing(self) -> None:
        self._clear_manual_polygon_preview()

    def _clear_manual_polygon_preview(self) -> None:
        if self._manual_polygon_preview is not None:
            try:
                self.plot_item.removeItem(self._manual_polygon_preview)
            except (RuntimeError, ValueError):
                pass
        self._manual_polygon_preview = None
        self._manual_polygon_kind = None
        self._manual_polygon_points = []
        self.image_view.ui.graphicsView.unsetCursor()

    def _update_manual_polygon_preview(self) -> None:
        if self._manual_polygon_preview is None or not self._manual_polygon_points:
            return
        path = QtGui.QPainterPath(self._manual_polygon_points[0])
        for point in self._manual_polygon_points[1:]:
            path.lineTo(point)
        self._manual_polygon_preview.setPath(path)

    def set_manual_mask_descriptors(
        self, descriptors: tuple[dict[str, object], ...], *, emit: bool = False
    ) -> None:
        self._updating_manual_masks = True
        try:
            self.clear_manual_mask_regions(emit=False)
            for descriptor in descriptors:
                self.add_manual_mask_region(
                    str(descriptor["kind"]), descriptor, emit=False
                )
        finally:
            self._updating_manual_masks = False
        if emit:
            self.manual_mask_changed.emit(self.manual_mask_descriptors())

    def undo_manual_mask_region(self) -> None:
        if not self._manual_mask_rois:
            return
        _kind, roi = self._manual_mask_rois.pop()
        self.plot_item.removeItem(roi)
        self.clear_mask_overlay()
        self.manual_mask_changed.emit(self.manual_mask_descriptors())

    def clear_manual_mask_regions(self, *, emit: bool = True) -> None:
        self.cancel_manual_polygon_drawing()
        for _kind, roi in self._manual_mask_rois:
            try:
                self.plot_item.removeItem(roi)
            except (RuntimeError, ValueError):
                pass
        self._manual_mask_rois.clear()
        self._selected_manual_mask_roi = None
        self.clear_mask_overlay()
        if emit and not self._updating_manual_masks:
            self.manual_mask_changed.emit(())

    def set_manual_mask_regions_visible(self, visible: bool) -> None:
        self._manual_mask_regions_visible = bool(visible)
        for _kind, roi in self._manual_mask_rois:
            roi.setVisible(self._manual_mask_regions_visible)
        if not self._manual_mask_regions_visible:
            self._select_manual_mask_roi(None)

    def manual_mask_layers(self) -> tuple[UserMaskLayer, ...]:
        if self._data is None:
            return ()
        return user_mask_layers_from_descriptors(
            self._data.shape, self.manual_mask_descriptors()
        )

    @property
    def geometry_overlay_count(self) -> int:
        return len(self._geometry_overlay_items)

    @property
    def geometry_center_target(self) -> pg.TargetItem | None:
        return self._geometry_center_target

    def clear_geometry_overlay(self) -> None:
        self._geometry_mouse_drag_active = False
        self._geometry_ring_drag_item = None
        for item in self._geometry_overlay_items:
            try:
                self.plot_item.removeItem(item)
            except (RuntimeError, ValueError):
                pass
        self._geometry_overlay_items.clear()
        self._geometry_center_target = None
        self._geometry_ring_items.clear()

    def begin_manual_ring_picking(self) -> None:
        self.clear_manual_ring_pick_points()
        self._manual_ring_pick_active = True

    def end_manual_ring_picking(self) -> None:
        self._manual_ring_pick_active = False

    def clear_manual_ring_pick_points(self) -> None:
        for item in self._manual_ring_pick_items:
            try:
                self.plot_item.removeItem(item)
            except (RuntimeError, ValueError):
                pass
        self._manual_ring_pick_items.clear()

    def _add_manual_ring_pick_point(self, x_px: float, y_px: float) -> None:
        marker = pg.ScatterPlotItem(
            [x_px],
            [y_px],
            size=12,
            symbol="o",
            pen=pg.mkPen("#FFFFFF", width=2),
            brush=pg.mkBrush(255, 122, 0, 190),
        )
        marker.setZValue(60)
        self.plot_item.addItem(marker)
        self._manual_ring_pick_items.append(marker)

    def set_geometry_overlay_visible(self, visible: bool) -> None:
        self._geometry_overlays_visible = bool(visible)
        for item in self._geometry_overlay_items:
            item.setVisible(self._geometry_overlays_visible)

    def set_calibrant_overlay(
        self,
        geometry: DetectorGeometry,
        calibrant: Calibrant,
        max_rings: int,
    ) -> int:
        """Overlay source-pinned theoretical rings without changing the array."""

        if self._data is None:
            self.clear_geometry_overlay()
            return 0
        if geometry.image_width_px != self._data.shape[1] or geometry.image_height_px != self._data.shape[0]:
            raise GeometryError("geometry image dimensions do not match the active array")
        self.clear_geometry_overlay()
        pen = pg.mkPen("#00E5FF", width=1.5)
        cx = geometry.beam_center_x_px
        cy = geometry.beam_center_y_px
        target = pg.TargetItem(
            pos=(cx, cy),
            size=max(14.0, min(self._data.shape) * 0.018),
            symbol="crosshair",
            pen=pg.mkPen("#FF4D6D", width=2.0),
            hoverPen=pg.mkPen("#FFFFFF", width=2.0),
            brush=pg.mkBrush(255, 77, 109, 70),
            hoverBrush=pg.mkBrush(255, 255, 255, 90),
            movable=True,
        )
        target.setZValue(40)
        target.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable,
            True,
        )
        target.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsFocusable,
            True,
        )
        target.setToolTip(
            "拖动微调候选中心；或选中后按方向键移动1 px，Shift+方向键移动0.1 px"
        )
        target.sigPositionChanged.connect(
            lambda *_args, item=target: self._geometry_target_moved(item)
        )
        target.sigPositionChangeFinished.connect(
            lambda *_args, item=target: self._geometry_target_finished(item)
        )
        self.plot_item.addItem(target)
        self._geometry_overlay_items.append(target)
        self._geometry_center_target = target
        self._geometry_candidate_distance_m = geometry.detector_distance_m
        rings = visible_rings(calibrant, geometry)[: max(0, int(max_rings))]
        for ring in rings:
            ellipse = QtWidgets.QGraphicsEllipseItem(
                cx - ring.radius_x_px,
                cy - ring.radius_y_px,
                2.0 * ring.radius_x_px,
                2.0 * ring.radius_y_px,
            )
            ellipse.setPen(QtGui.QPen(QtGui.QColor("#00E5FF"), 1.5))
            ellipse.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            ellipse.setZValue(30)
            ellipse.setFlag(
                QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable,
                True,
            )
            ellipse.setFlag(
                QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsFocusable,
                True,
            )
            ellipse.setToolTip(
                "点击环后按方向键移动中心1 px；Shift+方向键移动0.1 px"
            )
            self.plot_item.addItem(ellipse)
            self._geometry_overlay_items.append(ellipse)
            self._geometry_ring_items.append((ellipse, ring.radius_x_px, ring.radius_y_px))
        self.set_geometry_overlay_visible(self._geometry_overlays_visible)
        return len(rings)

    def _bounded_geometry_target_position(self, target: pg.TargetItem) -> tuple[float, float]:
        if self._data is None:
            return float(target.pos().x()), float(target.pos().y())
        height, width = self._data.shape
        x = min(max(float(target.pos().x()), 0.0), float(width))
        y = min(max(float(target.pos().y()), 0.0), float(height))
        if (x, y) != (float(target.pos().x()), float(target.pos().y())):
            self._updating_geometry_target = True
            try:
                target.setPos(x, y)
            finally:
                self._updating_geometry_target = False
        return x, y

    def _geometry_target_moved(self, target: pg.TargetItem) -> None:
        if self._updating_geometry_target or target is not self._geometry_center_target:
            return
        x, y = self._bounded_geometry_target_position(target)
        for ellipse, radius_x, radius_y in self._geometry_ring_items:
            ellipse.setRect(
                x - radius_x,
                y - radius_y,
                2.0 * radius_x,
                2.0 * radius_y,
            )
        self.geometry_center_dragged.emit(x, y)

    def _geometry_target_finished(self, target: pg.TargetItem) -> None:
        if target is not self._geometry_center_target:
            return
        x, y = self._bounded_geometry_target_position(target)
        self.geometry_center_drag_finished.emit(x, y)

    def _geometry_adjustment_selected(self) -> bool:
        target = self._geometry_center_target
        if target is not None and (target.isSelected() or target.hasFocus()):
            return True
        return any(
            item.isSelected() or item.hasFocus()
            for item, _radius_x, _radius_y in self._geometry_ring_items
        )

    def _geometry_scene_clicked(self, event: object) -> None:
        """Select a thin calibration overlay by detector-space proximity."""

        try:
            self._select_geometry_at_scene(event.scenePos())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return

    def _select_geometry_at_scene(
        self,
        scene_position: QtCore.QPointF,
        *,
        select_target: bool = True,
    ) -> str | None:
        target = self._geometry_center_target
        if target is None:
            return None
        try:
            view_position = self.plot_item.vb.mapSceneToView(scene_position)
            x = float(view_position.x())
            y = float(view_position.y())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
        center_x = float(target.pos().x())
        center_y = float(target.pos().y())
        if math.hypot(x - center_x, y - center_y) <= 8.0:
            if select_target:
                for item in self._geometry_overlay_items:
                    item.setSelected(False)
                target.setSelected(True)
            return "target"
        else:
            for item in self._geometry_overlay_items:
                item.setSelected(False)
            selected_ring = None
            selected_error = math.inf
            for item, radius_x, radius_y in self._geometry_ring_items:
                normalized = math.hypot(
                    (x - center_x) / radius_x,
                    (y - center_y) / radius_y,
                )
                radial_error = abs(normalized - 1.0) * math.sqrt(radius_x * radius_y)
                if radial_error < selected_error:
                    selected_error = radial_error
                    selected_ring = item
            if selected_ring is not None and selected_error <= 6.0:
                selected_ring.setSelected(True)
                self.image_view.ui.graphicsView.setFocus(
                    QtCore.Qt.FocusReason.MouseFocusReason
                )
                return "ring"
        return None

    def nudge_geometry_center(self, dx_px: float, dy_px: float) -> bool:
        """Move the full ring overlay by a precise detector-pixel increment."""

        target = self._geometry_center_target
        if target is None:
            return False
        target.setPos(
            float(target.pos().x()) + float(dx_px),
            float(target.pos().y()) + float(dy_px),
        )
        return True

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:  # noqa: N802 - Qt API
        graphics_view = self.image_view.ui.graphicsView
        viewport = graphics_view.viewport()
        if (
            watched is viewport
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
            and event.button() == QtCore.Qt.MouseButton.LeftButton
        ):
            self.activated.emit()
            graphics_view.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
            self._select_manual_mask_roi(None)
        if watched is viewport and self._manual_polygon_kind is not None:
            if event.type() == QtCore.QEvent.Type.MouseButtonPress:
                mouse_event = event
                scene_position = graphics_view.mapToScene(
                    mouse_event.position().toPoint()
                )
                view_position = self.plot_item.vb.mapSceneToView(scene_position)
                if mouse_event.button() == QtCore.Qt.MouseButton.LeftButton:
                    if (
                        len(self._manual_polygon_points) >= 3
                        and QtCore.QLineF(
                            view_position, self._manual_polygon_points[0]
                        ).length()
                        <= 10.0
                    ):
                        self.finish_manual_polygon_drawing()
                    else:
                        self.add_manual_polygon_point(
                            float(view_position.x()), float(view_position.y())
                        )
                    mouse_event.accept()
                    return True
        if (
            watched is viewport
            and self._manual_ring_pick_active
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
            and event.button() == QtCore.Qt.MouseButton.LeftButton
        ):
            scene_position = graphics_view.mapToScene(event.position().toPoint())
            view_position = self.plot_item.vb.mapSceneToView(scene_position)
            x_px = float(view_position.x())
            y_px = float(view_position.y())
            if (
                self._data is not None
                and 0.0 <= x_px <= self._data.shape[1]
                and 0.0 <= y_px <= self._data.shape[0]
            ):
                self._add_manual_ring_pick_point(x_px, y_px)
                self.manual_ring_point_picked.emit(x_px, y_px)
                event.accept()
                return True
                if mouse_event.button() == QtCore.Qt.MouseButton.RightButton:
                    if not self.finish_manual_polygon_drawing():
                        self.cancel_manual_polygon_drawing()
                    mouse_event.accept()
                    return True
            if event.type() == QtCore.QEvent.Type.KeyPress:
                key_event = event
                if key_event.key() == QtCore.Qt.Key.Key_Escape:
                    self.cancel_manual_polygon_drawing()
                    key_event.accept()
                    return True
        if (
            watched is viewport
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
        ):
            mouse_event = event
            if mouse_event.button() == QtCore.Qt.MouseButton.LeftButton:
                scene_position = graphics_view.mapToScene(
                    mouse_event.position().toPoint()
                )
                selection = self._select_geometry_at_scene(
                    scene_position,
                    select_target=False,
                )
                if selection == "target" and self._geometry_center_target is not None:
                    view_position = self.plot_item.vb.mapSceneToView(scene_position)
                    self._geometry_mouse_drag_offset = (
                        self._geometry_center_target.pos() - view_position
                    )
                    for item in self._geometry_overlay_items:
                        item.setSelected(False)
                    self._geometry_center_target.setSelected(True)
                    self._geometry_mouse_drag_active = True
                    mouse_event.accept()
                    return True
                # A thin ring has no native drag behavior, so consume its click
                # after selecting it for keyboard nudging.
                if selection == "ring":
                    target = self._geometry_center_target
                    if target is not None:
                        view_position = self.plot_item.vb.mapSceneToView(scene_position)
                        for item, radius_x, radius_y in self._geometry_ring_items:
                            if item.isSelected():
                                self._geometry_ring_drag_item = item
                                self._geometry_ring_drag_start_radius = (
                                    radius_x,
                                    radius_y,
                                )
                                self._geometry_ring_drag_start_distance_m = (
                                    self._geometry_candidate_distance_m
                                )
                                self._geometry_mouse_drag_active = True
                                break
                    mouse_event.accept()
                    return True
        if (
            watched is viewport
            and event.type() == QtCore.QEvent.Type.MouseMove
            and self._geometry_mouse_drag_active
            and self._geometry_center_target is not None
        ):
            mouse_event = event
            if mouse_event.buttons() & QtCore.Qt.MouseButton.LeftButton:
                scene_position = graphics_view.mapToScene(
                    mouse_event.position().toPoint()
                )
                view_position = self.plot_item.vb.mapSceneToView(scene_position)
                if self._geometry_ring_drag_item is not None:
                    target = self._geometry_center_target
                    if target is None:
                        return True
                    radius_x, radius_y = self._geometry_ring_drag_start_radius
                    normalized_radius = math.hypot(
                        (float(view_position.x()) - float(target.pos().x())) / radius_x,
                        (float(view_position.y()) - float(target.pos().y())) / radius_y,
                    )
                    if normalized_radius > 0.02:
                        distance_m = (
                            self._geometry_ring_drag_start_distance_m
                            * normalized_radius
                        )
                        self._geometry_candidate_distance_m = distance_m
                        for item, base_x, base_y in self._geometry_ring_items:
                            scale = distance_m / self._geometry_ring_drag_start_distance_m
                            item.setRect(
                                float(target.pos().x()) - base_x * scale,
                                float(target.pos().y()) - base_y * scale,
                                2.0 * base_x * scale,
                                2.0 * base_y * scale,
                            )
                        self.geometry_distance_dragged.emit(distance_m)
                    mouse_event.accept()
                    return True
                self._geometry_center_target.setPos(
                    view_position + self._geometry_mouse_drag_offset
                )
                mouse_event.accept()
                return True
        if (
            watched is viewport
            and event.type() == QtCore.QEvent.Type.MouseButtonRelease
            and self._geometry_mouse_drag_active
        ):
            mouse_event = event
            if mouse_event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._geometry_mouse_drag_active = False
                if self._geometry_ring_drag_item is not None:
                    self._geometry_ring_drag_item = None
                    self.geometry_distance_drag_finished.emit(
                        self._geometry_candidate_distance_m
                    )
                elif self._geometry_center_target is not None:
                    self._geometry_target_finished(self._geometry_center_target)
                mouse_event.accept()
                return True
        if (
            watched in (graphics_view, viewport)
            and event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() in {QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace}
        ):
            if self._selected_manual_mask_roi is not None:
                self._remove_manual_mask_roi(self._selected_manual_mask_roi)
                event.accept()
                return True
            if self.integration_overlay.is_selected:
                self.integration_overlay.clear()
                event.accept()
                return True
        if (
            watched in (graphics_view, viewport)
            and event.type() == QtCore.QEvent.Type.KeyPress
            and self._geometry_adjustment_selected()
        ):
            key_event = event
            key = key_event.key()
            step = (
                0.1
                if key_event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier
                else 1.0
            )
            offsets = {
                QtCore.Qt.Key.Key_Left: (-step, 0.0),
                QtCore.Qt.Key.Key_Right: (step, 0.0),
                QtCore.Qt.Key.Key_Up: (0.0, -step),
                QtCore.Qt.Key.Key_Down: (0.0, step),
            }
            if key in offsets:
                self.nudge_geometry_center(*offsets[key])
                key_event.accept()
                return True
        return super().eventFilter(watched, event)

    @QtCore.Slot(int)
    def _range_mode_changed(self, index: int) -> None:
        self.range_stack.setCurrentIndex(index)
        self._controls_changed()

    @QtCore.Slot()
    def _edit_custom_colormap(self) -> None:
        colors = self._preset.custom_colors or DEFAULT_CUSTOM_COLORS
        dialog = CustomColormapDialog(colors, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.set_custom_colors(dialog.colors)

    def set_custom_colors(self, colors: tuple[str, ...]) -> None:
        self._preset = replace(
            self._preset,
            colormap="custom",
            custom_colors=tuple(colors),
        )
        index = self.colormap_combo.findData("custom")
        blocker = QtCore.QSignalBlocker(self.colormap_combo)
        self.colormap_combo.setCurrentIndex(index)
        del blocker
        self.custom_colormap_button.setEnabled(True)
        self._render()
        self.preset_changed.emit(self._preset)

    @QtCore.Slot()
    def _controls_changed(self, *_args: object) -> None:
        mode = self.range_mode_combo.currentData()
        colormap = self.colormap_combo.currentData()
        custom_colors = self._preset.custom_colors
        if colormap == "custom" and not custom_colors:
            custom_colors = DEFAULT_CUSTOM_COLORS
        changes: dict[str, object] = {
            "colormap": colormap,
            "custom_colors": tuple(custom_colors) if colormap == "custom" else (),
            "scale": self.scale_combo.currentText(),
            "limit_mode": mode,
            "invert_colormap": self.invert_check.isChecked(),
        }
        if mode == "percentile":
            lower = self.lower_spin.value()
            upper = self.upper_spin.value()
            if lower >= upper:
                self.status_label.setText("百分位下限必须小于上限")
                return
            changes.update(lower_percentile=lower, upper_percentile=upper)
        else:
            changes.update(
                manual_vmin=self.manual_min_spin.value(),
                manual_vmax=self.manual_max_spin.value(),
            )
        try:
            self._preset = replace(self._preset, **changes)
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        self.custom_colormap_button.setEnabled(self._preset.colormap == "custom")
        self._render()
        self.preset_changed.emit(self._preset)

    def _render(self, *, fit: bool = False, normalized=None) -> None:
        if self._data is None:
            return
        try:
            if normalized is None:
                normalized = normalize_for_display(self._data, self._preset,self._display_mask)
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        colormap = resolve_gui_colormap(
            self._preset.colormap,
            self._preset.invert_colormap,
            self._preset.custom_colors or DEFAULT_CUSTOM_COLORS,
        )
        self.image_view.setColorMap(colormap)
        self._normalized = normalized
        self.image_view.setImage(
            normalized.values,
            autoRange=False,
            autoLevels=False,
            levels=(0.0, 1.0),
        )
        if fit:
            self.fit_image_to_view()
        self._base_status_text = (
            f"显示范围 {normalized.vmin:.6g} – {normalized.vmax:.6g}；"
            f"原数组 {self._data.shape[1]}×{self._data.shape[0]} 未修改"
        )
        self.status_label.setText(self._base_status_text)

    def fit_image_to_view(self) -> None:
        """Fit the current detector frame to all space available in this pane."""
        if self._data is not None and not self._graphics_shutdown:
            self.image_view.autoRange()

    @QtCore.Slot()
    def fit_image_to_view_interactively(self) -> None:
        """Fit the image and propagate that view when synchronized."""
        self.fit_image_to_view()
        self.plot_item.getViewBox().sigRangeChangedManually.emit([True, True])

    @QtCore.Slot()
    def show_actual_pixels(self) -> None:
        """Show one detector pixel per logical screen pixel around image center."""
        if self._data is None or self._graphics_shutdown:
            return
        view_box = self.plot_item.getViewBox()
        viewport_rect = view_box.boundingRect()
        viewport_width = float(viewport_rect.width())
        viewport_height = float(viewport_rect.height())
        if viewport_width <= 0.0 or viewport_height <= 0.0:
            return
        height, width = self._data.shape
        view_box.setRange(
            QtCore.QRectF(
                width / 2.0 - viewport_width / 2.0,
                height / 2.0 - viewport_height / 2.0,
                viewport_width,
                viewport_height,
            ),
            padding=0.0,
            disableAutoRange=True,
        )
        view_box.sigRangeChangedManually.emit([True, True])

    def schedule_fit_image_to_view(self) -> None:
        if self._data is not None and not self._graphics_shutdown:
            self._fit_timer.start()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        # Debounce the relatively expensive PyQtGraph auto-range operation.
        # The pane itself still resizes continuously while dragging; re-fitting
        # once the pointer pauses keeps fine splitter adjustments responsive.
        self.schedule_fit_image_to_view()

    def shutdown_graphics(self) -> None:
        if self._graphics_shutdown:
            return
        self._graphics_shutdown = True
        self._fit_timer.stop()
        # PyQtGraph keeps every ViewBox in a global weak registry. Explicitly
        # close both the image and hidden per-pane histogram ViewBoxes before
        # Qt deletes their menus, otherwise PySide can report deleted comboboxes
        # while another ViewBox is being unregistered.
        for view_box in (
            self.plot_item.getViewBox(),
            self.image_view.ui.histogram.item.vb,
            self.image_view.ui.roiPlot.getViewBox(),
        ):
            try:
                view_box.destroyed.disconnect()
            except (RuntimeError, TypeError):
                pass
            try:
                view_box.close()
            except (KeyError, RuntimeError):
                pass

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 - Qt API
        self.shutdown_graphics()
        super().closeEvent(event)
        self.deleteLater()


class FolderBrowserWidget(QtWidgets.QWidget):
    """Folder navigation independent of detector decoding and filename prefixes."""

    file_selected = QtCore.Signal(object)
    folder_changed = QtCore.Signal(object)
    checked_selection_changed = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self._files: tuple[DiscoveredFile, ...] = ()
        self.source_masters: dict[str,Path] = {}
        self.path_edit = QtWidgets.QLineEdit()
        self.path_edit.setPlaceholderText("选择包含二维散射图的文件夹")
        self.browse_button = QtWidgets.QPushButton("浏览…")
        self.recursive_check = QtWidgets.QCheckBox("包含子文件夹")
        self.recursive_check.setChecked(True)
        from .frame_tree import FrameTree
        self.file_list = FrameTree()
        self.file_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.check_all_button = QtWidgets.QPushButton("全部勾选")
        self.check_highlighted_button = QtWidgets.QPushButton("勾选所选")
        self.clear_checks_button = QtWidgets.QPushButton("清空勾选")
        for button in (self.check_all_button,self.check_highlighted_button,self.clear_checks_button):
            button.setStyleSheet("padding: 4px;")
        self.previous_button = QtWidgets.QPushButton("上一张")
        self.next_button = QtWidgets.QPushButton("下一张")
        self.count_label = QtWidgets.QLabel("0 个候选文件")
        self.compare_frames_button=QtWidgets.QPushButton("对比预览所选帧")

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.path_edit, 1)
        top.addWidget(self.browse_button)
        navigation = QtWidgets.QHBoxLayout()
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.next_button)
        navigation.addStretch(1)
        check_actions = QtWidgets.QHBoxLayout()
        check_actions.setContentsMargins(0, 0, 0, 0)
        check_actions.addWidget(self.check_all_button)
        check_actions.addWidget(self.check_highlighted_button)
        check_actions.addWidget(self.clear_checks_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
        layout.addLayout(top)
        layout.addWidget(self.recursive_check)
        layout.addLayout(check_actions)
        layout.addWidget(self.file_list, 1)
        layout.addWidget(self.compare_frames_button)
        layout.addLayout(navigation)
        layout.addWidget(self.count_label)

        self.browse_button.clicked.connect(self._choose_folder)
        self.path_edit.returnPressed.connect(self._path_submitted)
        self.recursive_check.toggled.connect(self._reload_if_possible)
        self.file_list.currentRowChanged.connect(self._row_changed)
        self.check_all_button.clicked.connect(self.check_all)
        self.check_highlighted_button.clicked.connect(self.check_highlighted)
        self.clear_checks_button.clicked.connect(self.clear_checks)
        self.previous_button.clicked.connect(lambda: self.move_selection(-1))
        self.next_button.clicked.connect(lambda: self.move_selection(1))
        self._update_navigation()
        from .file_drop import install_file_drop
        install_file_drop(self,self.import_paths,self.count_label.setText)

    def import_paths(self,paths):
        if len(paths)==1 and Path(paths[0]).is_dir():
            self.set_folder(paths[0])
            return
        from .file_drop import input_files
        files=input_files(paths,supported_detector_path)
        if not files:raise ValueError('没有可读取的二维探测器文件')
        import os
        root=Path(os.path.commonpath([str(p.parent) for p in files]))
        self.set_folder(root)
        self.select_paths(tuple(files))
        first=self.source_masters.get(str(files[0].resolve()),files[0])
        self.set_current_path(first)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt API
        # File names and controls may be clipped while the user prioritizes the
        # image or preview area. Their size hints must not stop splitter motion.
        return QtCore.QSize(0, 0)

    @property
    def files(self) -> tuple[DiscoveredFile, ...]:
        return self._files

    @property
    def selected_path(self) -> Path | None:
        row = self.file_list.currentRow()
        return self._files[row].path if 0 <= row < len(self._files) else None

    @property
    def selected_paths(self) -> tuple[Path, ...]:
        checked_rows = [
            row
            for row in range(self.file_list.count())
            if self.file_list.item(row).checkState()
            != QtCore.Qt.CheckState.Unchecked
        ]
        return tuple(
            self._files[row].path
            for row in checked_rows
            if 0 <= row < len(self._files)
        )

    @property
    def highlighted_paths(self) -> tuple[Path, ...]:
        rows = self.file_list.selected_sample_rows()
        return tuple(self._files[row].path for row in rows if 0 <= row < len(self._files))

    def set_folder(self, root: str | Path) -> None:
        path = Path(root).expanduser().absolute()
        files = discover_image_files(path, recursive=self.recursive_check.isChecked())
        from saxs_waxs_core.readers.detector_io import inspect_sources,is_hdf5
        hdf_paths=[]
        for item in files:
            try:
                if is_hdf5(item.path):
                    hdf_paths.append(item.path)
            except OSError:
                continue  # Keep inaccessible entries visible for the normal read error.
        self.source_masters={}
        if hdf_paths:
            _,_,_,excluded=inspect_sources(hdf_paths)
            self.source_masters={
                str(Path(item["path"]).resolve()):Path(item["master"])
                for item in excluded
            }
            files=tuple(item for item in files
                if str(item.path.resolve()) not in self.source_masters)
        self.path_edit.setText(str(path))
        self._files = files
        self.file_list.blockSignals(True)
        self.file_list.clear()
        for item in files:
            from .frame_tree import SampleItem
            list_item = SampleItem(item.relative_path.as_posix())
            list_item.setToolTip(str(item.path))
            list_item.setFlags(
                list_item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable
            )
            list_item.setCheckState(QtCore.Qt.CheckState.Unchecked)
            self.file_list.addItem(list_item)
        self.file_list.blockSignals(False)
        self.count_label.setText(f"{len(files)} 个候选文件")
        self.folder_changed.emit(path)
        if files:
            self.file_list.setCurrentRow(0)
        self._update_navigation()

    def select_paths(self, paths: tuple[Path, ...]) -> tuple[Path, ...]:
        """Select visible files by absolute path without opening any frame."""
        wanted = {
            str(self.source_masters.get(str(path.expanduser().resolve()),path.expanduser().absolute()))
            for path in paths
        }
        blocker = QtCore.QSignalBlocker(self.file_list)
        self.file_list.clearSelection()
        selected: list[Path] = []
        first_row: int | None = None
        for row, discovered in enumerate(self._files):
            chosen = str(discovered.path.expanduser().absolute()) in wanted
            item = self.file_list.item(row)
            item.setCheckState(
                QtCore.Qt.CheckState.Checked
                if chosen
                else QtCore.Qt.CheckState.Unchecked
            )
            item.setSelected(chosen)
            if chosen:
                selected.append(discovered.path)
                if first_row is None:
                    first_row = row
        if first_row is not None:
            self.file_list.setCurrentRow(
                first_row,
                QtCore.QItemSelectionModel.SelectionFlag.NoUpdate,
            )
        del blocker
        self.checked_selection_changed.emit()
        return tuple(selected)

    def _set_check_state(self, rows: Iterable[int], checked: bool) -> None:
        state = (
            QtCore.Qt.CheckState.Checked
            if checked
            else QtCore.Qt.CheckState.Unchecked
        )
        blocker = QtCore.QSignalBlocker(self.file_list)
        for row in sorted({int(value) for value in rows}):
            if 0 <= row < self.file_list.count():
                self.file_list.item(row).setCheckState(state)
        del blocker
        self.checked_selection_changed.emit()

    @QtCore.Slot()
    def check_all(self) -> None:
        self._set_check_state(range(self.file_list.count()), True)

    @QtCore.Slot()
    def check_highlighted(self) -> None:
        rows = self.file_list.selected_sample_rows()
        self._set_check_state(rows, True)

    @QtCore.Slot()
    def clear_checks(self) -> None:
        self._set_check_state(range(self.file_list.count()), False)

    def set_current_path(self, path: str | Path) -> Path:
        """Open one visible file by exact absolute path."""

        source=Path(path).expanduser().resolve()
        wanted = str(self.source_masters.get(str(source),source)).casefold()
        for row, discovered in enumerate(self._files):
            if str(discovered.path.expanduser().absolute()).casefold() == wanted:
                if self.file_list.currentRow() == row:
                    self.file_selected.emit(discovered.path)
                else:
                    self.file_list.setCurrentRow(row)
                return discovered.path
        raise FileNotFoundError(f"project file is not visible in the browser: {path}")

    def move_selection(self, step: int) -> None:
        if step not in {-1, 1}:
            raise ValueError("Navigation step must be -1 or 1")
        if not self._files:
            return
        current = self.file_list.currentRow()
        target = min(max(current + step, 0), len(self._files) - 1)
        self.file_list.setCurrentRow(target)

    @QtCore.Slot()
    def _choose_folder(self) -> None:
        initial = self.path_edit.text() or str(Path.cwd())
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "选择数据文件夹", initial)
        if selected:
            self.set_folder(selected)

    @QtCore.Slot()
    def _path_submitted(self) -> None:
        if self.path_edit.text().strip():
            self.set_folder(self.path_edit.text().strip())

    @QtCore.Slot()
    def _reload_if_possible(self) -> None:
        text = self.path_edit.text().strip()
        if text and Path(text).is_dir():
            self.set_folder(text)

    @QtCore.Slot(int)
    def _row_changed(self, row: int) -> None:
        self._update_navigation()
        if 0 <= row < len(self._files):
            self.file_selected.emit(self._files[row].path)

    def _update_navigation(self) -> None:
        row = self.file_list.currentRow()
        self.previous_button.setEnabled(bool(self._files) and row > 0)
        self.next_button.setEnabled(bool(self._files) and row < len(self._files) - 1)


class LinkedViewerController(QtCore.QObject):
    """Optionally propagate display presets and manual view navigation."""

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._viewers: list[ImageViewerWidget] = []
        self._view_range_slots: dict[ImageViewerWidget, object] = {}
        self._linked = True

    @property
    def linked(self) -> bool:
        return self._linked

    def set_linked(
        self,
        linked: bool,
        *,
        source: ImageViewerWidget | None = None,
    ) -> None:
        self._linked = bool(linked)
        if self._linked and len(self._viewers) > 1:
            source_viewer = source if source in self._viewers else self._viewers[0]
            source_preset = source_viewer.preset
            for viewer in self._viewers[1:]:
                if viewer is not source_viewer:
                    viewer.set_preset(source_preset)
            if self._viewers[0] is not source_viewer:
                self._viewers[0].set_preset(source_preset)
            self.sync_view_from(source_viewer)

    def add_viewer(self, viewer: ImageViewerWidget) -> None:
        if viewer in self._viewers:
            return
        if self._linked and self._viewers:
            viewer.set_preset(self._viewers[0].preset)
        self._viewers.append(viewer)
        viewer.preset_changed.connect(
            lambda preset, source=viewer: self._preset_changed(source, preset)
        )
        view_range_slot = lambda _mask, source=viewer: self._manual_view_changed(source)
        self._view_range_slots[viewer] = view_range_slot
        viewer.plot_item.getViewBox().sigRangeChangedManually.connect(view_range_slot)

    def remove_viewer(self, viewer: ImageViewerWidget) -> None:
        if viewer in self._viewers:
            self._viewers.remove(viewer)
        slot = self._view_range_slots.pop(viewer, None)
        if slot is not None:
            try:
                viewer.plot_item.getViewBox().sigRangeChangedManually.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def sync_view_from(self, source: ImageViewerWidget) -> None:
        """Copy one pane's pixel-coordinate zoom and pan to all linked panes."""
        if not self._linked or source not in self._viewers:
            return
        source_range = source.plot_item.getViewBox().viewRange()
        x_range = tuple(float(value) for value in source_range[0])
        y_range = tuple(float(value) for value in source_range[1])
        for viewer in tuple(self._viewers):
            if viewer is source:
                continue
            viewer.plot_item.getViewBox().setRange(
                xRange=x_range,
                yRange=y_range,
                padding=0.0,
                disableAutoRange=True,
            )

    @QtCore.Slot(object)
    def _manual_view_changed(self, source: ImageViewerWidget) -> None:
        self.sync_view_from(source)

    @QtCore.Slot(object, object)
    def _preset_changed(self, source: ImageViewerWidget, preset: DisplayPreset) -> None:
        if not self._linked:
            return
        for viewer in tuple(self._viewers):
            if viewer is not source:
                viewer.set_preset(preset)


class EmbeddedViewerPane(QtWidgets.QFrame):
    """An image pane embedded in the main workspace, never a system window."""

    activated = QtCore.Signal(object)
    navigate_requested = QtCore.Signal(object, int)
    frame_requested = QtCore.Signal(object, int)
    dataset_requested = QtCore.Signal(object, str)
    batch_frames_requested = QtCore.Signal(object)
    close_requested = QtCore.Signal(object)

    def __init__(
        self,
        pane_number: int,
        *,
        primary: bool = False,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.pane_number = pane_number
        self.primary = primary
        self.current_path: Path | None = None
        self.frame = None
        self.background_result: BackgroundResult | None = None
        self.mask_result: MaskResult | None = None
        self.integration_result: IntegrationCurve | None = None
        self.integration_region: dict[str, object] | None = None
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.title_label = QtWidgets.QLabel(f"图像 {pane_number} · 未载入")
        self.title_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.activate_button = QtWidgets.QToolButton()
        self.activate_button.setText("○")
        self.activate_button.setToolTip("点击任意图像区域即可设为当前；也可点击此处")
        self.previous_button = QtWidgets.QToolButton()
        self.previous_button.setArrowType(QtCore.Qt.ArrowType.LeftArrow)
        self.next_button = QtWidgets.QToolButton()
        self.next_button.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.close_button = QtWidgets.QToolButton()
        self.close_button.setText("×")
        self.close_button.setToolTip("关闭这个内嵌图像窗格")
        self.close_button.setEnabled(not primary)
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(4, 2, 4, 2)
        toolbar.addWidget(self.activate_button)
        toolbar.addWidget(self.previous_button)
        toolbar.addWidget(self.next_button)
        toolbar.addWidget(self.title_label, 1)
        self.dataset_combo=QtWidgets.QComboBox()
        self.dataset_combo.setMaximumWidth(210)
        self.dataset_combo.setToolTip('预览数据集；批量使用已确认的主信号范围')
        self.frame_spin=QtWidgets.QSpinBox()
        self.frame_spin.setMinimum(1)
        self.frame_spin.setToolTip('预览帧；批次帧可单独选择')
        self.frame_spin.setMaximumWidth(130)
        self.dataset_combo.hide();self.frame_spin.hide()
        self.frame_spin.setKeyboardTracking(False)
        from .frame_player import FramePlayer
        self.frame_player=FramePlayer(self)
        self.frame_controls=QtWidgets.QWidget()
        frame_toolbar=QtWidgets.QHBoxLayout(self.frame_controls)
        frame_toolbar.setContentsMargins(4,0,4,0)
        self.play_frames_button=QtWidgets.QToolButton()
        self.play_frames_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._playing_changed(False)
        self.frame_input_timer=QtCore.QTimer(self)
        self.frame_input_timer.setSingleShot(True)
        self.frame_input_timer.setInterval(300)
        self.frame_input_timer.timeout.connect(self._commit_frame_input)
        self.frame_controls.hide()
        toolbar.addWidget(self.dataset_combo)
        for control in (self.play_frames_button,self.frame_spin):
            frame_toolbar.addWidget(control)
        frame_toolbar.addStretch(1)
        self.batch_frames_button=QtWidgets.QToolButton()
        self.batch_frames_button.setText("批次帧…")
        self.batch_frames_button.setToolTip("选择当前数据集进入批处理的帧")
        self.batch_frames_button.hide()
        self.batch_frames_button.clicked.connect(lambda:self.batch_frames_requested.emit(self))
        frame_toolbar.addWidget(self.batch_frames_button)
        self.play_frames_button.clicked.connect(self._toggle_frame_playback)
        self.frame_spin.lineEdit().textEdited.connect(self._frame_input_edited)
        self.frame_spin.editingFinished.connect(self._commit_frame_input)
        self.frame_player.playing_changed.connect(self._playing_changed)
        self.frame_spin.valueChanged.connect(lambda n:self.frame_requested.emit(self,n-1))
        self.dataset_combo.currentIndexChanged.connect(lambda i:self.dataset_requested.emit(self,str(self.dataset_combo.itemData(i))))
        toolbar.addWidget(self.close_button)
        self.viewer = ImageViewerWidget(show_controls=primary)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        layout.addLayout(toolbar)
        layout.addWidget(self.frame_controls)
        layout.addWidget(self.viewer, 1)
        self.activate_button.clicked.connect(lambda: self.activated.emit(self))
        self.previous_button.clicked.connect(lambda: self.navigate_requested.emit(self, -1))
        self.next_button.clicked.connect(lambda: self.navigate_requested.emit(self, 1))
        self.close_button.clicked.connect(lambda: self.close_requested.emit(self))
        self.set_active(False)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt API
        return QtCore.QSize(0, 0)

    def set_active(self, active: bool) -> None:
        self.activate_button.setText("●" if active else "○")
        border = "#2F80ED" if active else "#777777"
        width = 3 if active else 1
        self.setStyleSheet(f"EmbeddedViewerPane {{ border: {width}px solid {border}; }}")

    def _playing_changed(self, playing):
        symbol=(QtWidgets.QStyle.StandardPixmap.SP_MediaPause if playing else
                QtWidgets.QStyle.StandardPixmap.SP_MediaPlay)
        self.play_frames_button.setIcon(self.style().standardIcon(symbol))
        self.play_frames_button.setToolTip("暂停" if playing else "播放")
        self.play_frames_button.setAccessibleName("暂停" if playing else "播放")

    def _toggle_frame_playback(self):
        self.frame_input_timer.stop()
        if self.frame_player.playing:
            self.frame_player.pause()
        else:
            self.activated.emit(self)
            self.frame_player.play()

    def _frame_input_edited(self):
        self.frame_player.pause()
        self.frame_input_timer.start()

    def _commit_frame_input(self):
        self.frame_input_timer.stop()
        self.frame_spin.interpretText()

    def set_frame(self, path: Path, frame: object, normalized=None) -> None:
        previous_shape = None if self.frame is None else self.frame.data.shape
        same_source=(self.current_path==path and self.frame is not None and
            self.frame.metadata.dataset_path==frame.metadata.dataset_path and
            previous_shape==frame.data.shape)
        self.current_path = path
        self.frame = frame
        self.background_result = None
        self.mask_result = None
        self.integration_result = None
        if not same_source:
            self.geometry_override=None
        if previous_shape is not None and previous_shape != frame.data.shape:
            self.viewer.clear_manual_mask_regions(emit=False)
        self.title_label.setText(f"图像 {self.pane_number} · {path.name}")
        if frame.metadata.frame_count>1:
            self.title_label.setText(f"图像 {self.pane_number} · {path.name} · 帧 {frame.metadata.frame_index+1}/{frame.metadata.frame_count}")
        self.title_label.setToolTip(str(path))
        self.viewer.set_data(frame.data,frame.metadata.detector_mask,
            normalized=normalized,fit=not same_source)
        self.frame_player.configure(frame,self.viewer.preset,self.viewer._normalized)
        blocker=QtCore.QSignalBlocker(self.frame_spin)
        self.frame_spin.setMaximum(frame.metadata.frame_count)
        self.frame_spin.setValue(frame.metadata.frame_index+1)
        self.frame_spin.setSuffix(f' / {frame.metadata.frame_count}')
        self.frame_spin.setVisible(frame.metadata.frame_count>1)
        self.batch_frames_button.setVisible(frame.metadata.frame_count>1)
        self.frame_controls.setVisible(frame.metadata.frame_count>1)
        del blocker


RECENT_DATA_FOLDER_KEY = "startup/recent_data_folder"


def resolve_startup_folder(
    arguments: list[str] | tuple[str, ...],
    settings: QtCore.QSettings | None,
) -> Path | None:
    """Resolve an explicit or per-computer recent folder without sample defaults."""

    if arguments:
        explicit = Path(arguments[0]).expanduser().absolute()
        return explicit if explicit.is_dir() else None
    if settings is None:
        return None
    recent_text = str(settings.value(RECENT_DATA_FOLDER_KEY, "") or "").strip()
    if not recent_text:
        return None
    recent = Path(recent_text).expanduser().absolute()
    if recent.is_dir():
        return recent
    # A path saved on another/older machine is not a valid startup choice.
    settings.remove(RECENT_DATA_FOLDER_KEY)
    settings.sync()
    return None


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, *, settings: QtCore.QSettings | None = None) -> None:
        super().__init__()
        self._settings = settings
        from .theme import apply_theme
        apply_theme(self)
        self.setWindowTitle(TITLE)
        self.setWindowIcon(QtGui.QIcon(str(resource_path("branding/xrelay.ico"))))
        self.resize(1200, 760)
        self.browser = FolderBrowserWidget()
        self._panes: list[EmbeddedViewerPane] = []
        self._active_pane: EmbeddedViewerPane | None = None
        self._next_pane_number = 1
        self.current_geometry: DetectorGeometry | None = None
        self._integration_radial_cache: dict[tuple[object, ...], np.ndarray] = {}
        from .latest_task import LatestTask
        self._integration_tasks=LatestTask(self)
        self._integration_tasks.setObjectName('integration')
        from saxs_waxs_core.integration import IntegrationWorkspace
        self._integration_workspace=IntegrationWorkspace()
        from .latest_task import PreviewBackgroundWorkspace, PreviewMaskWorkspace
        self._preview_background_workspace=PreviewBackgroundWorkspace()
        self._preview_mask_workspace=PreviewMaskWorkspace()
        self._mask_preview_workspace=PreviewMaskWorkspace()
        self._navigation_background_workspace=PreviewBackgroundWorkspace()
        self._follow_background=False
        self._follow_mask=False
        self._follow_integration=False
        self._integration_tasks.ready.connect(self._integration_preview_ready)
        self._integration_tasks.failed.connect(self._integration_preview_failed)
        self._overlay_tasks=LatestTask(self)
        self._overlay_tasks.setObjectName('integration_overlay')
        self._overlay_tasks.ready.connect(self._overlay_map_ready)
        self._overlay_tasks.failed.connect(lambda error:self.integration_panel.set_status(error,error=True))
        self._overlay_pending_key=None
        self._background_tasks=LatestTask(self)
        self._background_tasks.setObjectName('background_preview')
        self._background_tasks.ready.connect(self._background_preview_ready)
        self._background_tasks.failed.connect(lambda error:self.background_panel.set_status(error,error=True))
        self._mask_tasks=LatestTask(self)
        self._mask_tasks.setObjectName('mask_preview')
        self._mask_tasks.ready.connect(self._mask_preview_ready)
        self._mask_tasks.failed.connect(lambda error:self.mask_panel.set_status(error,error=True))
        self._updating_integration_regions = False
        self._integration_preview_timer = QtCore.QTimer(self)
        self._integration_preview_timer.setSingleShot(True)
        self._integration_preview_timer.setInterval(180)
        self._integration_preview_timer.timeout.connect(self._preview_integration)
        self.current_calibration_suggestion: CalibrationSuggestion | None = None
        self._manual_ring_pick_context: dict[str, object] | None = None
        self._calibration_thread: QtCore.QThread | None = None
        self._calibration_worker: CalibrationWorker | None = None
        self._batch_thread: QtCore.QThread | None = None
        self._batch_worker: BatchExecutionWorker | None = None
        self._image_export_thread: QtCore.QThread | None = None
        self._processed_data_thread = None
        self._processed_data_worker = None
        self._detector_dataset_choices={}
        self._detector_frame_choices={}
        self._image_export_worker: ImageExportBatchWorker | None = None
        self._fit_curve_thread: QtCore.QThread | None = None
        self._fit_curve_worker: FitCurvePreparationWorker | None = None
        self._peak_fit_windows: list[PeakFitWorkspaceWindow] = []
        self._next_peak_fit_session = 1
        self._batch_scope_rule = "explicit_left_panel_selection"
        self._batch_plan_draft: BatchProcessingPlan | None = None
        self._project_export_preset = ExportPreset()
        self._project_path: Path | None = None
        self._ruland_defaults = {}
        self.workspace_widget = HorizontallyUnconstrainedWidget()
        self.workspace_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.workspace_layout = QtWidgets.QVBoxLayout(self.workspace_widget)
        self.workspace_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_layout.setSpacing(0)
        self.workspace_splitter = FineTuneSplitter(QtCore.Qt.Orientation.Vertical)
        self._configure_splitter(self.workspace_splitter)
        self.workspace_layout.addWidget(self.workspace_splitter)
        self.workspace_row_splitters: list[FineTuneSplitter] = []
        self.workspace_scroll = QtWidgets.QScrollArea()
        self.workspace_scroll.setMinimumSize(0, 0)
        self.workspace_scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.workspace_scroll.setWidgetResizable(True)
        self.workspace_scroll.setWidget(self.workspace_widget)
        self.right_panel = HorizontallyUnconstrainedWidget()
        self.right_layout = QtWidgets.QVBoxLayout(self.right_panel)
        self.right_layout.setContentsMargins(4, 4, 4, 4)
        self.shared_panel_label = QtWidgets.QLabel("多图查看")
        self.shared_panel_label.setStyleSheet("font-weight: 600;")
        self.add_pane_button = QtWidgets.QPushButton("＋ 添加图像窗格")
        self.toggle_browser_button = QtWidgets.QToolButton()
        self.toggle_browser_button.setText("< 文件栏")
        self.toggle_browser_button.setToolTip("收起/显示左侧文件浏览栏")
        self.toggle_preview_button = QtWidgets.QToolButton()
        self.toggle_preview_button.setText("几何/预览 ▶")
        self.toggle_preview_button.setToolTip("显示/收起右侧几何与处理预览栏")
        header = QtWidgets.QHBoxLayout()
        header.addWidget(self.toggle_browser_button)
        header.addWidget(self.shared_panel_label, 1)
        header.addWidget(self.add_pane_button)
        header.addWidget(self.toggle_preview_button)
        self.right_layout.addLayout(header)
        self.preview_panel = HorizontallyUnconstrainedWidget()
        preview_layout = QtWidgets.QVBoxLayout(self.preview_panel)
        preview_header = QtWidgets.QHBoxLayout()
        preview_header.addWidget(QtWidgets.QLabel("几何与处理预览"), 1)
        self.hide_preview_button = QtWidgets.QToolButton()
        self.hide_preview_button.setText("×")
        self.hide_preview_button.setToolTip("收起右侧预览栏")
        preview_header.addWidget(self.hide_preview_button)
        preview_layout.addLayout(preview_header)
        self.geometry_panel = GeometryPanel()
        self.background_panel = BackgroundPanel()
        self.mask_panel = MaskPanel()
        self.integration_panel = IntegrationPanel(plot_settings=self._settings)
        self.peak_fit_panel = PeakFitLauncherPanel()
        self.recipe_panel = RecipePanel()
        self.processing_tabs = QtWidgets.QTabWidget()
        self.processing_tabs.addTab(self.geometry_panel, "几何/标定")
        self.processing_tabs.addTab(self.background_panel, "背景")
        self.processing_tabs.addTab(self.mask_panel, "MASK")
        self.processing_tabs.addTab(self.integration_panel, "积分")
        self.processing_tabs.addTab(self.peak_fit_panel, "拟合")
        self.processing_tabs.addTab(self.recipe_panel, "配方")
        preview_layout.addWidget(self.processing_tabs, 1)
        self.preview_panel.hide()
        self.main_splitter = FineTuneSplitter(QtCore.Qt.Orientation.Horizontal)
        self._configure_splitter(self.main_splitter)
        self.main_splitter.addWidget(self.browser)
        self.main_splitter.addWidget(self.right_panel)
        self.main_splitter.addWidget(self.preview_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        self.main_splitter.setCollapsible(0, True)
        self.main_splitter.setCollapsible(1, True)
        self.main_splitter.setCollapsible(2, True)
        self.main_splitter.setSizes([270, 930, 0])
        self.setCentralWidget(self.main_splitter)
        self.browser.file_selected.connect(self._file_selected)
        self.browser.file_list.frame_selected.connect(self._browser_frame_selected)
        self.browser.file_list.frames_checked.connect(self._browser_frames_checked)
        self.browser.compare_frames_button.clicked.connect(self._compare_browser_frames)
        self.browser.folder_changed.connect(self._folder_changed)
        self.peak_fit_panel.workspace_requested.connect(
            self._open_peak_fit_workspace
        )
        self.peak_fit_panel.load_checked_requested.connect(
            self._start_checked_fit_curve_preparation
        )
        self.peak_fit_panel.cancel_load_requested.connect(
            self._cancel_checked_fit_curve_preparation
        )
        self.display_linker = LinkedViewerController(self)
        primary = self._create_pane(primary=True)
        self.viewer = primary.viewer
        self.shared_controls: QtWidgets.QWidget | None = None
        self.display_toolbar = QtWidgets.QWidget()
        self.display_toolbar.setObjectName("sharedDisplayToolbar")
        self.display_toolbar_layout = QtWidgets.QHBoxLayout(self.display_toolbar)
        self.display_toolbar_layout.setContentsMargins(4, 0, 4, 0)
        self.display_toolbar_layout.setSpacing(6)
        self.sync_display_check = QtWidgets.QCheckBox("同步显示")
        self.sync_display_check.setChecked(True)
        self.sync_display_check.setToolTip(
            "开启：调色、缩放和平移同步到所有窗格；"
            "后续积分区域也遵循同一开关"
        )
        self.tone_bar = ToneRangeBar()
        self.tone_bar.setObjectName("sharedToneBar")
        self.tone_bar.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.display_toolbar_layout.addWidget(self.sync_display_check)
        self.display_toolbar_layout.addWidget(self.tone_bar, 1)
        self.right_layout.addWidget(self.display_toolbar)
        self.right_layout.addWidget(self.workspace_scroll, 1)
        self._linked_levels = (0.0, 1.0)
        self._propagating_levels = False
        self._activate_pane(primary)
        self.capability_label = QtWidgets.QLabel("读取：未选择 | 分析：未就绪")
        self.statusBar().addPermanentWidget(self.capability_label)

        file_menu = self.menuBar().addMenu("文件")
        self.import_curves_action = QtGui.QAction("导入一维曲线并拟合…", self)
        self.import_curves_action.triggered.connect(self._choose_import_curves)
        file_menu.addAction(self.import_curves_action)
        self.open_project_action = QtGui.QAction("打开项目…", self)
        self.open_project_action.setShortcut(QtGui.QKeySequence.StandardKey.Open)
        self.open_project_action.triggered.connect(self._choose_open_project)
        file_menu.addAction(self.open_project_action)
        self.save_project_action = QtGui.QAction("保存项目…", self)
        self.save_project_action.setShortcut(QtGui.QKeySequence.StandardKey.Save)
        self.save_project_action.triggered.connect(self._choose_save_project)
        file_menu.addAction(self.save_project_action)
        self.portable_project_action = QtGui.QAction("导出便携项目包…", self)
        self.portable_project_action.triggered.connect(
            self._choose_export_portable_project
        )
        file_menu.addAction(self.portable_project_action)
        file_menu.addSeparator()
        self.export_current_2d_action = QtGui.QAction("导出2D图…", self)
        self.export_current_2d_action.setToolTip(
            "从当前二维数组与显示预设直接渲染；不截取屏幕，也不受窗口缩放影响"
        )
        self.export_current_2d_action.triggered.connect(self._choose_export_2d)
        file_menu.addAction(self.export_current_2d_action)
        self.export_processed_data_action=QtGui.QAction('导出处理后二维数据…',self)
        self.export_processed_data_action.triggered.connect(self._choose_export_processed_data)
        file_menu.addAction(self.export_processed_data_action)
        self.resume_2d_export_action = QtGui.QAction("恢复2D批量导出…", self)
        self.resume_2d_export_action.triggered.connect(
            self._choose_resume_image_export
        )
        file_menu.addAction(self.resume_2d_export_action)
        self.pause_2d_export_action = QtGui.QAction(
            "当前文件后暂停2D批量导出", self
        )
        self.pause_2d_export_action.setEnabled(False)
        self.pause_2d_export_action.triggered.connect(
            self._request_image_export_pause
        )
        file_menu.addAction(self.pause_2d_export_action)

        window_menu = self.menuBar().addMenu("窗口")
        self.language_menu = self.menuBar().addMenu("Language / 语言")
        self.language_menu.addAction("简体中文", lambda: self.set_ui_language("zh"))
        self.language_menu.addAction("English", lambda: self.set_ui_language("en"))
        self.agent_action = QtGui.QAction("AI Agent 接口…", self)
        self.agent_action.triggered.connect(self._open_agent_setup)
        window_menu.addAction(self.agent_action)
        help_menu=self.menuBar().addMenu("帮助")
        help_menu.addAction("图文操作手册…", self._open_user_manual)
        help_menu.addAction("导出诊断报告…", self._export_diagnostics)
        help_menu.addAction("关于 XRelay…", lambda:QtWidgets.QMessageBox.about(self,"关于 XRelay",f"XRelay {VERSION}\nX射线散射数据处理与分析\nX-ray + Relay：可复用的连续处理流程\n开发者：张久天 / Jiutian Zhang\n原创软件：免费使用，改名包装销售须授权；见 LICENSE。\nQt/PySide6 与 FabIO 的相关组件按 LGPL 分发。\n第三方许可与库替换权利不受原创软件销售限制影响。\n许可/声明：licenses；匹配库源码：_sources。"))
        help_menu.addAction("操作指南…",self._show_workflow_guide)
        self.restart_action = help_menu.addAction("重启并加载更新", self._restart_workbench)
        self._restart_timer = QtCore.QTimer(self)
        self._restart_timer.setInterval(750)
        self._restart_timer.timeout.connect(self._finish_restart)
        self.ruland_action = QtGui.QAction("Ruland 多 q 分析…", self)
        self.ruland_action.triggered.connect(self._open_ruland_dialog)
        window_menu.addAction(self.ruland_action)
        self.new_comparison_action = QtGui.QAction("添加内嵌图像窗格", self)
        self.new_comparison_action.triggered.connect(self.open_comparison_window)
        self.add_pane_button.clicked.connect(self.open_comparison_window)
        self.toggle_browser_button.clicked.connect(self._toggle_browser_panel)
        self.toggle_preview_button.clicked.connect(self._toggle_preview_panel)
        self.hide_preview_button.clicked.connect(lambda: self._set_preview_visible(False))
        self.geometry_panel.preview_changed.connect(self._geometry_preview_changed)
        self.geometry_panel.apply_requested.connect(self._apply_candidate_geometry)
        self.geometry_panel.suggest_requested.connect(self._start_geometry_suggestion)
        self.geometry_panel.manual_ring_pick_requested.connect(
            self._start_manual_ring_pick
        )
        self.geometry_panel.manual_ring_pick_cancel_requested.connect(
            self._cancel_manual_ring_pick
        )
        self.background_panel.preview_requested.connect(self._preview_background)
        self.background_panel.raw_requested.connect(self._restore_raw_image)
        for signal in (self.background_panel.mode_combo.currentIndexChanged,
                       self.background_panel.background_combo.currentIndexChanged,
                       self.background_panel.air_dataset_combo.currentIndexChanged,
                       self.background_panel.air_frame_spin.valueChanged,
                       self.background_panel.equal_exposure_check.toggled):
            signal.connect(self._processing_settings_changed)
        self.mask_panel.preview_requested.connect(self._preview_mask)
        self.mask_panel.clear_requested.connect(self._clear_mask)
        self.mask_panel.add_region_requested.connect(self._add_manual_mask_region)
        self.mask_panel.undo_region_requested.connect(self._undo_manual_mask_region)
        self.mask_panel.clear_regions_requested.connect(self._clear_manual_mask_regions)
        self.mask_panel.external_mask_changed.connect(self._external_mask_changed)
        for checkbox in (self.mask_panel.nonfinite_check,self.mask_panel.sentinel_check,self.mask_panel.cutoff_check):
            checkbox.toggled.connect(lambda *_:self._processing_settings_changed() if self._follow_mask else None)
        self.mask_panel.auto_suggest_requested.connect(self._suggest_automatic_mask)
        self.mask_panel.region_visibility_changed.connect(
            self._update_auxiliary_overlay_visibility
        )
        from .integration_feedback import IntegrationFeedback
        self._integration_feedback=IntegrationFeedback(self._integration_tasks,self.integration_panel,self)
        self.integration_panel.preview_requested.connect(self._preview_integration)
        self.integration_panel.region_changed.connect(
            self._integration_panel_region_changed
        )
        self.integration_panel.curve_scope_combo.currentIndexChanged.connect(
            self._schedule_integration_preview
        )
        self.integration_panel.overlay_visibility_changed.connect(
            self._update_integration_overlay_visibility
        )
        self.processing_tabs.currentChanged.connect(
            self._processing_tab_changed
        )
        self.browser.file_list.itemSelectionChanged.connect(
            self._update_batch_selection_count
        )
        self.browser.file_list.itemChanged.connect(
            lambda _item: self._update_batch_selection_count()
        )
        self.browser.checked_selection_changed.connect(
            self._update_batch_selection_count
        )
        self.browser.checked_selection_changed.connect(self._checked_preview_selection_changed)
        self.recipe_panel.save_recipe_requested.connect(self._choose_save_recipe)
        self.recipe_panel.load_recipe_requested.connect(self._choose_load_recipe)
        self.recipe_panel.save_batch_requested.connect(self._choose_save_batch_plan)
        self.recipe_panel.run_batch_requested.connect(self._choose_run_batch)
        self.recipe_panel.resume_batch_requested.connect(self._choose_resume_batch)
        self.recipe_panel.pause_batch_requested.connect(self._request_batch_pause)
        self.recipe_panel.scope_apply_requested.connect(self._apply_batch_scope)
        self.recipe_panel.create_revision_requested.connect(
            self._choose_create_batch_recipe_revision
        )
        window_menu.addAction(self.new_comparison_action)
        self.sync_display_action = QtGui.QAction("同步显示", self)
        self.sync_display_action.setCheckable(True)
        self.sync_display_action.setChecked(True)
        self.sync_display_action.toggled.connect(self._sync_action_toggled)
        self.sync_display_check.toggled.connect(self._sync_check_toggled)
        self.tone_bar.levels_changed.connect(self._tone_levels_changed)
        window_menu.addAction(self.sync_display_action)
        self._update_auxiliary_overlay_visibility()
        self.statusBar().showMessage("请选择数据文件夹，或导入一维曲线开始处理。")
        from .file_drop import install_file_drop,choose_input
        install_file_drop(self,self._import_dropped_paths,self.statusBar().showMessage,descendants=False)
        for button,callback,title in (
            (self.recipe_panel.load_recipe_button,self._choose_load_recipe,'载入配方'),
            (self.recipe_panel.resume_batch_button,self._choose_resume_batch,'恢复批处理')):
            def dropped(paths,callback=callback,title=title):
                selected=choose_input(self,paths,lambda p:p.suffix.lower()=='.json',title)
                if selected:callback(str(selected))
            install_file_drop(button,dropped,self.statusBar().showMessage)
        if self._settings is not None:
            self.set_ui_language(str(self._settings.value("ui/language","zh")))

        from .adaptive_layout import AdaptiveLayout
        self.adaptive_layout=AdaptiveLayout(self)
        window_menu.addSeparator()
        window_menu.addAction(self.adaptive_layout.action)
        window_menu.addAction(self.adaptive_layout.reset_action)

    def set_ui_language(self, language):
        from .i18n import language_manager
        language_manager().set_language(language)
        if self._settings is not None:
            self._settings.setValue("ui/language",language)

    def _import_dropped_paths(self,paths):
        from .file_drop import input_files,choose_input
        if any(Path(p).is_dir() for p in paths):
            if input_files(paths,supported_detector_path):
                return self.browser.import_paths(paths)
            files=input_files(paths,lambda p:p.suffix.lower() in ('.csv','.tsv','.txt','.dat','.json',PROJECT_EXTENSION))
            if not files:raise ValueError('文件夹中没有此窗口支持导入的文件')
            return self._import_dropped_paths(files)
        if all(supported_detector_path(p) for p in paths):
            return self.browser.import_paths(paths)
        if all(Path(p).suffix.lower() in ('.csv','.tsv','.txt','.dat') for p in paths):
            return self._choose_import_curves([str(p) for p in paths])
        selected=choose_input(self,paths,lambda p:p.suffix.lower() in ('.json',PROJECT_EXTENSION),'选择导入文件')
        if selected is None:return
        if selected.suffix.lower()==PROJECT_EXTENSION:
            return self._choose_open_project(str(selected))
        schema=str(json.loads(selected.read_text(encoding='utf-8')).get('schema',''))
        if 'image_export' in schema:return self._choose_resume_image_export(str(selected))
        if 'batch_run' in schema:return self._choose_resume_batch(str(selected))
        if 'geometry' in schema:return self.geometry_panel._load_dialog(str(selected))
        return self._choose_load_recipe(str(selected))

    def _export_diagnostics(self):
        import time
        from .runtime_diagnostics import export_report
        selected,_=QtWidgets.QFileDialog.getSaveFileName(self,'导出诊断报告',
            f'XRelay-diagnostics-{time.strftime("%Y%m%d-%H%M%S")}.zip','ZIP (*.zip)')
        if not selected:return
        try:
            saved=export_report(selected)
            self.statusBar().showMessage(f'诊断报告已导出：{saved}')
        except OSError as exc:
            self.statusBar().showMessage(f'诊断报告导出失败：{exc}')

    def _open_user_manual(self):
        path=resource_path("manual/index.html")
        if path.is_file():QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
        else:self.statusBar().showMessage("图文手册未找到，请检查软件包是否完整。")

    def _open_agent_setup(self):
        from .agent_setup import AgentSetupDialog
        dialog=AgentSetupDialog(self)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _show_workflow_guide(self):
        from .i18n import language_manager
        english=language_manager().language == "en"
        text=("1. Choose a data folder or import 1D curves.\n"
              "2. Apply known geometry directly, or estimate it with a calibrant.\n"
              "3. Background and MASK are optional; raw export does not need calibration.\n"
              "4. Preview radial or azimuthal integration, then fit/analyze.\n"
              "5. Apply batch scope, review checked files, then process.\n"
              "6. Export: choose scope and data/images. Data uses one workbook with three sheets.\n"
              "7. Optional AI: Window > AI Agent interface. Generate a bounded MCP connection configuration."
              if english else
              "1. 选择数据文件夹，或导入一维曲线。\n"
              "2. 已知几何参数直接应用；未知时再用标样标定。\n"
              "3. 背景和MASK按需使用；原图导出不要求标定。\n"
              "4. 预览一维或方位角积分，再进入拟合/分析。\n"
              "5. 批量处理先应用范围、检查左侧勾选，再执行。\n"
              "6. 导出统一选择范围及数据/图片；数据默认一个工作簿、三张表。\n"
              "7. 需要AI时：窗口→AI Agent接口，选择输入/输出目录并生成MCP连接配置。")
        QtWidgets.QMessageBox.information(self,"Workflow guide" if english else "操作指南",text)

    def _project_storage_directory(self) -> Path:
        directory = artifacts_directory() / "projects"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _project_snapshot(self, *, name: str | None = None) -> WorkbenchProject:
        pane = self._active_pane
        current = (
            None
            if pane is None or pane.current_path is None
            else FileIdentity.from_path(pane.current_path)
        )
        selected = tuple(
            FileIdentity.from_path(path) for path in self.browser.selected_paths
        )
        processing: ProcessingRecipe | None = None
        if pane is not None and pane.frame is not None and self.current_geometry is not None:
            try:
                processing = self._processing_recipe_for_active_pane()
            except (OSError, ProcessingError, ValueError):
                # A viewer-only project is valid.  Incomplete scientific controls
                # remain visible in the UI but are not misrepresented as a recipe.
                processing = None
        root_text = self.browser.path_edit.text().strip()
        default_name = (
            Path(root_text).name
            if root_text
            else (pane.current_path.stem if pane and pane.current_path else "untitled")
        )
        return WorkbenchProject(
            name=(name or default_name),
            data_root_hint=root_text or None,
            recursive=self.browser.recursive_check.isChecked(),
            current_source=current,
            selected_sources=selected,
            processing=processing,
            batch_plan=self._batch_plan_draft,
            display=(pane.viewer.preset if pane is not None else DisplayPreset()),
            export=self._project_export_preset,
            metadata={
                "saved_by": "XRelay",
                "project_mode": "external_references",
                "detector_source_choices": {
                    item.path: {
                        "dataset": self._detector_dataset_choices.get(str(Path(item.path).resolve())),
                        "frames": self._detector_frame_choices.get(str(Path(item.path).resolve())),
                    }
                    for item in selected + ((current,) if current is not None else ())
                    if str(Path(item.path).resolve()) in self._detector_dataset_choices
                    or str(Path(item.path).resolve()) in self._detector_frame_choices
                },
                "ruland_defaults": self._ruland_defaults.get((str(self._project_path), root_text), {}),
            },
        )

    def save_project_to_path(
        self, path: str | Path, *, overwrite: bool = False
    ) -> Path:
        target = Path(path).expanduser().absolute()
        if target.suffix.casefold() != PROJECT_EXTENSION:
            target = target.with_suffix(PROJECT_EXTENSION)
        project = self._project_snapshot(name=target.stem)
        saved = save_project(
            project,
            target,
            protected_roots=self._protected_recipe_roots(),
            overwrite=overwrite,
        )
        defaults = project.metadata.get("ruland_defaults", {})
        if defaults:
            self._ruland_defaults[(str(saved), self.browser.path_edit.text().strip())] = defaults
        self._project_path = saved
        self.setWindowTitle(f"{TITLE} — {project.name}")
        return saved

    def load_project_into_ui(
        self,
        path: str | Path,
        *,
        relocation_root: str | Path | None = None,
    ) -> WorkbenchProject:
        source = Path(path).expanduser().absolute()
        project = load_project(source)
        invalid = verify_project_references(project)
        if invalid:
            if relocation_root is None:
                names = "、".join(Path(item.path).name for item in invalid[:3])
                raise ProjectError(f"项目引用需要重新定位：{names}")
            identities = project_file_identities(project)
            report = resolve_file_relocation(
                identities,
                relocation_root,
                original_root=project.data_root_hint,
                recursive=True,
            )
            if not report.complete:
                missing = sum(item.status == "missing" for item in report.items)
                ambiguous = sum(item.status == "ambiguous" for item in report.items)
                raise ProjectError(
                    f"重新定位未完成：缺失 {missing}，内容重复待选择 {ambiguous}；未应用项目"
                )
            project = relocate_project(
                project, report.mapping, data_root_hint=report.search_root
            )
        root = (
            None
            if project.data_root_hint is None
            else Path(project.data_root_hint).expanduser().absolute()
        )
        if root is None or not root.is_dir():
            if project.current_source is None:
                raise ProjectError("项目没有可用的数据根目录或当前图像")
            root = Path(project.current_source.path).parent
        choices=project.metadata.get("detector_source_choices",{})
        self._detector_dataset_choices={
            str(Path(path).resolve()): option["dataset"]
            for path,option in choices.items() if option.get("dataset") is not None
        }
        self._detector_frame_choices={
            str(Path(path).resolve()): list(option["frames"])
            for path,option in choices.items() if option.get("frames") is not None
        }
        self.browser.recursive_check.setChecked(project.recursive)
        self.set_folder(root)
        selection = project.selected_sources
        if not selection and project.batch_plan is not None:
            selection = project.batch_plan.sample_sources
        self.browser.select_paths(
            tuple(Path(item.path).expanduser().absolute() for item in selection)
        )
        if project.current_source is not None:
            self.browser.set_current_path(project.current_source.path)
        for pane in self._panes:
            pane.viewer.set_preset(project.display)
        recipe = project.processing
        if recipe is None and project.batch_plan is not None:
            recipe = project.batch_plan.recipe
        if recipe is not None:
            self._apply_processing_recipe_to_ui(recipe)
        self._batch_plan_draft = project.batch_plan
        self._update_recipe_revision_summary()
        self._update_batch_selection_count()
        self._project_export_preset = project.export
        self._project_path = source
        key = (str(source), self.browser.path_edit.text().strip())
        self._ruland_defaults[key] = project.metadata.get("ruland_defaults", {})
        self.setWindowTitle(f"{TITLE} — {project.name}")
        return project

    @QtCore.Slot()
    def _choose_save_project(self) -> None:
        current_name = (
            self._project_path.name
            if self._project_path is not None
            else f"workbench-project{PROJECT_EXTENSION}"
        )
        selected, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "保存 XRelay 项目",
            str(self._project_storage_directory() / current_name),
            f"XRelay 项目 (*{PROJECT_EXTENSION})",
        )
        if not selected:
            return
        try:
            saved = self.save_project_to_path(selected, overwrite=True)
        except (OSError, ProjectError, ProcessingError, ValueError) as exc:
            self.statusBar().showMessage(f"项目保存失败：{exc}")
            return
        self.statusBar().showMessage(f"项目已保存：{saved}")

    @QtCore.Slot()
    def _choose_open_project(self, selected=None) -> None:
        if not selected:
            selected, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "打开 XRelay 项目",
                str(self._project_storage_directory()),
                f"XRelay 项目 (*{PROJECT_EXTENSION})",
            )
        if not selected:
            return
        try:
            self.load_project_into_ui(selected)
        except ProjectError as first_error:
            if "需要重新定位" not in str(first_error):
                self.statusBar().showMessage(f"项目打开失败：{first_error}")
                return
            root = QtWidgets.QFileDialog.getExistingDirectory(
                self,
                "选择迁移后数据所在的根目录（将按大小和SHA-256核验）",
                str(Path(selected).parent),
            )
            if not root:
                self.statusBar().showMessage("项目未打开：尚未完成数据重新定位")
                return
            try:
                self.load_project_into_ui(selected, relocation_root=root)
            except (OSError, ProjectError, ProcessingError, ValueError) as exc:
                self.statusBar().showMessage(f"项目重新定位失败：{exc}")
                return
        except (OSError, ProcessingError, ValueError) as exc:
            self.statusBar().showMessage(f"项目打开失败：{exc}")
            return
        self.statusBar().showMessage(f"项目已打开：{selected}")

    @QtCore.Slot()
    def _choose_export_portable_project(self) -> None:
        parent = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "选择便携项目包的上级文件夹",
            str(self._project_storage_directory()),
        )
        if not parent:
            return
        project = self._project_snapshot()
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", project.name).strip(" .")
        from .export_naming import choose_run_directory
        destination=choose_run_directory(self,parent,f"{safe_name or 'workbench-project'}_portable")
        if destination is None:return
        try:
            exported = export_portable_project(
                project,
                destination,
                protected_roots=self._protected_recipe_roots(),
            )
        except (OSError, ProjectError, ProcessingError, ValueError) as exc:
            self.statusBar().showMessage(f"便携项目包导出失败：{exc}")
            return
        self.statusBar().showMessage(
            f"便携项目包已导出：{exported}；复制内容已逐文件核验SHA-256"
        )

    def _image_export_directory(self) -> Path:
        directory = artifacts_directory() / "exports"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def export_current_2d_to_path(
        self,
        path: str | Path,
        *,
        width_mm: float = 180.0,
        height_mm: float = 130.0,
        dpi: int = 600,
        include_axes: bool = True,
        include_colorbar: bool = True,
        overwrite: bool = False,
    ) -> ImageExportResult:
        pane = self._active_pane
        if pane is None or pane.current_path is None or pane.viewer.data is None:
            raise ImageExportError("请先在当前窗格载入二维图像")
        stage = "raw"
        audit: dict[str, object] = {"stage": stage}
        if pane.background_result is not None:
            stage = "background_corrected"
            audit = {
                "stage": stage,
                "background": pane.background_result.audit,
            }
        if pane.mask_result is not None:
            audit["mask"] = pane.mask_result.audit
        return export_2d_image(
            pane.viewer.data,
            path,
            pane.viewer.preset,
            mask=pane.viewer.mask,
            width_mm=width_mm,
            height_mm=height_mm,
            dpi=dpi,
            include_axes=include_axes,
            include_colorbar=include_colorbar,
            geometry=self.current_geometry,
            source_path=pane.current_path,
            processing_audit=audit,
            protected_roots=self._protected_recipe_roots(),
            overwrite=overwrite,
        )

    @QtCore.Slot()
    def _choose_export_current_2d(self) -> None:
        pane = self._active_pane
        if pane is None or pane.current_path is None or pane.viewer.data is None:
            self.statusBar().showMessage("导出失败：请先在当前窗格载入二维图像")
            return
        initial = self._image_export_directory() / f"{pane.current_path.stem}_2d.png"
        selected, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出当前二维图",
            str(initial),
            "PNG (*.png);;TIFF (*.tif *.tiff);;SVG (*.svg);;PDF (*.pdf)",
        )
        if not selected:
            return
        try:
            result = self.export_current_2d_to_path(selected)
        except (ImageExportError, OSError, ValueError) as exc:
            self.statusBar().showMessage(f"2D导出失败：{exc}")
            return
        self.statusBar().showMessage(
            f"已导出2D图：{result.image_path.name}；"
            f"{result.width_px}×{result.height_px} px，参数说明已同名保存"
        )

    @QtCore.Slot()
    def _choose_export_processed_data(self):
        from .processed_data_export import ProcessedDataExportDialog
        pane=self._active_pane
        if pane is None or pane.frame is None:
            self.statusBar().showMessage('请先载入二维数据。')
            return
        dialog=ProcessedDataExportDialog(self,has_geometry=self.current_geometry is not None,
            subtract_background=self.background_panel.mode is BackgroundMode.AUTO_AIR)
        if dialog.exec()!=QtWidgets.QDialog.DialogCode.Accepted:
            return
        paths=(pane.current_path,) if dialog.scope_combo.currentData()=='current' else self.browser.selected_paths
        if not paths:
            self.statusBar().showMessage('请在左侧勾选至少一个样品。')
            return
        background_path=self.background_panel.background_path if dialog.background_check.isChecked() else None
        if dialog.background_check.isChecked() and background_path is None:
            self.statusBar().showMessage('请在背景页选择背景文件，或取消扣背景。')
            return
        if dialog.background_check.isChecked() and not self.background_panel.source_ready:
            self.statusBar().showMessage('请先明确选择背景数据集和帧。')
            return
        folder=QtWidgets.QFileDialog.getExistingDirectory(self,'选择处理后数据的上级文件夹',str(self._image_export_directory()))
        if not folder:
            return
        from .export_naming import choose_run_directory
        directory=choose_run_directory(self,folder,'processed_2d_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
        if directory is None:return
        background=replace(self.background_panel.background_recipe(),mode=BackgroundMode.AUTO_AIR) if background_path else BackgroundRecipe(mode=BackgroundMode.NO_BACKGROUND)
        options=dict(background_path=background_path,background_recipe=background,
            output_format=dialog.format_combo.currentData(),dataset_selections=self._detector_dataset_choices.copy(),
            frame_choices=self._detector_frame_choices.copy(),
            background_selection=self.background_panel.source_selection,
            prefer_embedded_geometry=getattr(pane,'geometry_override',None) is None,
            frame_selections=({str(pane.current_path.resolve()):dict(
                **({'dataset':pane.frame.metadata.dataset_path} if pane.frame.metadata.dataset_path else {}),
                frame_index=pane.frame.metadata.frame_index)} if dialog.scope_combo.currentData()=='current' else {}),
            geometry=self.current_geometry if dialog.geometry_check.isChecked() else None,
            mask_recipe=self.mask_panel.recipe() if dialog.mask_check.isChecked() else MaskRecipe(),
            manual_masks=pane.viewer.manual_mask_descriptors() if dialog.mask_check.isChecked() else (),
            external_mask_path=self.mask_panel.external_mask_path if dialog.mask_check.isChecked() else None,
            protected_roots=(Path(__file__).resolve().parents[2]/'data',))
        self._start_processed_data_export(tuple(paths),directory,options)

    def _start_processed_data_export(self,paths,directory,options):
        from .processed_data_export import ProcessedDataWorker
        if self._processed_data_thread is not None:
            self.statusBar().showMessage('处理后数据导出正在运行。')
            return
        thread=QtCore.QThread(self)
        worker=ProcessedDataWorker(paths,directory,options)
        worker.moveToThread(thread)
        progress=QtWidgets.QProgressDialog('导出处理后二维数据','完成当前文件后停止',0,len(paths),self)
        progress.setWindowTitle('导出结果')
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.canceled.connect(worker.request_stop)
        self._processed_data_progress=progress
        thread.started.connect(worker.run)
        worker.progress.connect(self._processed_data_progress_changed)
        worker.finished.connect(self._processed_data_finished)
        worker.failed.connect(self._processed_data_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._processed_data_thread_finished)
        self._processed_data_thread=thread
        self._processed_data_worker=worker
        self.export_processed_data_action.setEnabled(False)
        progress.show()
        thread.start()

    def _processed_data_progress_changed(self,value):
        self._processed_data_progress.setMaximum(value['total'])
        self._processed_data_progress.setValue(value['completed'])
        self._processed_data_progress.setLabelText(f"{value['sample']} · {value['status']}")

    def _processed_data_finished(self,result):
        self._processed_data_progress.close()
        self.statusBar().showMessage(f"处理后数据导出：完成 {result['completed']}，失败 {result['failed']}；{result['directory']}")

    def _processed_data_failed(self,message):
        self._processed_data_progress.close()
        self.statusBar().showMessage(f'处理后数据导出失败：{message}')

    def _processed_data_thread_finished(self):
        self._processed_data_thread=None
        self._processed_data_worker=None
        self.export_processed_data_action.setEnabled(True)

    def _choose_export_2d(self) -> None:
        pane = self._active_pane
        if pane is None or pane.current_path is None or pane.viewer.data is None:
            self.statusBar().showMessage("导出失败：请先在当前窗格载入二维图像")
            return
        dialog = ImageExportDialog(
            batch_available=bool(self.browser.selected_paths),
            initial_preset=self._project_export_preset,
            parent=self,
        )
        from .export_preview import enable_export_preview
        def preview_source(raw):
            return (pane.frame.data,pane.frame.metadata.detector_mask,pane.viewer.preset,self.current_geometry) if raw else (
                pane.viewer.data,pane.viewer.mask,pane.viewer.preset,self.current_geometry)
        enable_export_preview(dialog,preview_source)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        export = dialog.export_preset()
        self._project_export_preset = export
        if export.detector_axis_mode == "detector_mm":
            if dialog.scope == "selected" and dialog.batch_source == "raw_array":
                self.statusBar().showMessage(
                    "探测器mm坐标需要已应用几何；原始数据批量请改用像素坐标"
                )
                return
            if dialog.scope == "current" and self.current_geometry is None:
                self.statusBar().showMessage(
                    "探测器mm坐标需要先应用标定几何；未标定图请使用像素坐标"
                )
                return
        if dialog.batch_source == "raw_array":
            display_data = pane.frame.data
            display_mask = pane.frame.metadata.detector_mask
        else:
            display_data = pane.viewer.data
            display_mask = pane.viewer.mask
        display = pane.viewer.preset
        if export.detector_range_mode == "shared_manual":
            try:
                low, high = resolve_display_limits(
                    display_data, display, display_mask
                )
                display = replace(
                    display,
                    limit_mode="manual",
                    manual_vmin=low,
                    manual_vmax=high,
                )
            except ValueError as exc:
                self.statusBar().showMessage(f"无法固定当前色阶：{exc}")
                return
        if dialog.scope == "current":
            if dialog.batch_source == "raw_array":
                image_data = pane.frame.data
                image_mask = pane.frame.metadata.detector_mask
                processing_audit = {
                    "stage": "raw_array",
                    "background": "not_applied",
                    "mask": "not_applied",
                    "geometry": "not_required_for_pixel_axis",
                    "integration": "not_applied",
                }
            else:
                image_data = pane.viewer.data
                image_mask = pane.viewer.mask
                processing_audit = {
                    "stage": "current_view_array",
                    "background": (
                        "not_applied"
                        if pane.background_result is None
                        else dict(pane.background_result.audit)
                    ),
                    "mask": (
                        "not_applied"
                        if pane.mask_result is None
                        else dict(pane.mask_result.audit)
                    ),
                    "integration": "not_applied_to_2d_image_export",
                }
            suffix = export.detector_image_format
            initial = self._image_export_directory() / f"{pane.current_path.stem}_2d.{suffix}"
            selected, _ = QtWidgets.QFileDialog.getSaveFileName(
                self,
                "导出当前二维图",
                str(initial),
                f"{suffix.upper()} (*.{suffix})",
            )
            if not selected:
                return
            try:
                result = export_2d_image(
                    image_data,
                    selected,
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
                    geometry=self.current_geometry,
                    source_path=pane.current_path,
                    processing_audit=processing_audit,
                    protected_roots=self._protected_recipe_roots(),
                )
            except (ImageExportError, OSError, ValueError) as exc:
                self.statusBar().showMessage(f"2D导出失败：{exc}")
                return
            self.statusBar().showMessage(f"已导出2D图：{result.image_path}")
            return

        if dialog.batch_source == "raw_array":
            try:
                plan: BatchProcessingPlan | RawImageExportPlan = (
                    self._raw_image_export_plan_for_current_selection()
                )
            except (ProcessingError, OSError, ValueError) as exc:
                self.statusBar().showMessage(f"2D原始图批量预检失败：{exc}")
                return
        else:
            try:
                plan = self._batch_plan_for_current_selection()
            except (ProcessingError, OSError) as exc:
                self.statusBar().showMessage(f"2D处理图批量预检失败：{exc}")
                return
        selected = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "选择2D批量导出的上级文件夹",
            str(self._image_export_directory()),
        )
        if not selected:
            return
        base = Path(selected).expanduser().absolute()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        from .export_naming import choose_run_directory
        run_directory=choose_run_directory(self,base,f"batch_2d_{timestamp}")
        if run_directory is None:return
        self._start_image_export_worker(plan, run_directory, display, export)

    @QtCore.Slot()
    def _choose_resume_image_export(self, selected=None) -> None:
        if not selected:
            selected, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "选择2D批量导出清单",
                str(self._image_export_directory()),
                "2D导出清单 (image_export_manifest.json);;JSON (*.json)",
            )
        if not selected:
            return
        try:
            spec = load_image_export_resume_spec(selected)
        except (ImageExportBatchError, ProcessingError, OSError, ValueError) as exc:
            self.statusBar().showMessage(f"无法恢复2D批量导出：{exc}")
            return
        self._start_image_export_worker(
            spec.plan,
            spec.manifest_path.parent,
            spec.display,
            spec.export,
            resume=True,
        )

    def _start_image_export_worker(
        self,
        plan: BatchProcessingPlan | RawImageExportPlan,
        run_directory: Path,
        display: DisplayPreset,
        export: ExportPreset,
        *,
        resume: bool = False,
    ) -> None:
        if self._image_export_thread is not None and self._image_export_thread.isRunning():
            self.statusBar().showMessage("已有2D批量导出正在运行")
            return
        thread = QtCore.QThread(self)
        worker = ImageExportBatchWorker(
            plan,
            run_directory,
            display,
            export,
            self._protected_recipe_roots(),
            resume=resume,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._image_export_progress)
        worker.finished.connect(self._image_export_finished)
        worker.failed.connect(self._image_export_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._image_export_thread_finished)
        self._image_export_thread = thread
        self._image_export_worker = worker
        self.pause_2d_export_action.setEnabled(True)
        self.resume_2d_export_action.setEnabled(False)
        action = "恢复" if resume else "启动"
        self.statusBar().showMessage(f"2D批量导出已{action}：{run_directory}")
        thread.start()

    @QtCore.Slot()
    def _request_image_export_pause(self) -> None:
        if self._image_export_worker is None:
            return
        self._image_export_worker.request_stop()
        self.pause_2d_export_action.setEnabled(False)
        self.statusBar().showMessage("将在当前2D图导出完成后暂停…")

    @QtCore.Slot(object)
    def _image_export_progress(self, summary: object) -> None:
        if isinstance(summary, dict):
            done = int(summary.get("passed", 0)) + int(summary.get("failed", 0))
            self.statusBar().showMessage(
                f"2D批量导出 {done}/{int(summary.get('total', 0))}；"
                f"失败 {int(summary.get('failed', 0))}"
            )

    @QtCore.Slot(object)
    def _image_export_finished(self, result: object) -> None:
        if not isinstance(result, ImageExportBatchResult):
            self.statusBar().showMessage("2D批量导出返回了未知结果")
            return
        if result.state == "paused":
            self.statusBar().showMessage(
                f"2D批量导出已暂停：{result.manifest_path}；"
                "可通过“文件 → 恢复2D批量导出…”继续"
            )
        else:
            failed = int(result.summary.get("failed", 0))
            self.statusBar().showMessage(
                f"2D批量导出完成：{result.manifest_path}"
                + (f"；{failed} 个文件失败" if failed else "")
            )

    @QtCore.Slot(str)
    def _image_export_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"2D批量导出失败：{message}")

    @QtCore.Slot()
    def _image_export_thread_finished(self) -> None:
        self._image_export_thread = None
        self._image_export_worker = None
        self.pause_2d_export_action.setEnabled(False)
        self.resume_2d_export_action.setEnabled(True)

    @staticmethod
    def _configure_splitter(splitter: QtWidgets.QSplitter) -> None:
        """Make panel and pane dividers visible and easy to drag."""
        splitter.setHandleWidth(8)
        splitter.setOpaqueResize(True)
        splitter.setStyleSheet(
            "QSplitter::handle { background: #D0D5DD; }"
            "QSplitter::handle:hover { background: #2F80ED; }"
        )

    def set_folder(self, root: str | Path) -> None:
        self.browser.set_folder(root)

    @QtCore.Slot()
    def choose_startup_folder(self) -> None:
        """Prompt on first use while allowing Cancel to leave a clean viewer."""

        initial = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.DocumentsLocation
        )
        selected = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "选择SAXS/WAXS数据文件夹",
            initial or str(Path.cwd()),
        )
        if selected:
            self.set_folder(selected)
        else:
            self.statusBar().showMessage(
                "尚未选择数据文件夹；可随时点击左侧‘浏览…’。"
            )

    @QtCore.Slot(object)
    def _folder_changed(self, root: Path) -> None:
        if self._settings is not None:
            self._settings.setValue(RECENT_DATA_FOLDER_KEY, str(root))
            self._settings.sync()
        self._batch_plan_draft = None
        self.recipe_panel.set_revision_summary(
            "尚未建立基础批次方案；请先保存或载入批次方案。"
        )
        self.background_panel.set_files(item.path for item in self.browser.files)
        self._update_batch_selection_count()

    @QtCore.Slot()
    def _update_batch_selection_count(self) -> None:
        count = len(self.browser.selected_paths)
        self.recipe_panel.set_selection_count(count)
        self.peak_fit_panel.set_checked_selection_count(count)

    @staticmethod
    def _sequence_key(path: Path) -> str | None:
        match = re.fullmatch(r"(.*?)(\d+)", path.stem)
        return match.group(1).casefold() if match and match.group(1) else None

    @staticmethod
    def _automatic_non_sample_role(path: Path) -> str | None:
        stem = path.stem.casefold()
        if re.search(r"(^|[_-])(air|background|dark)(?=[_-]|\d|$)", stem):
            return "背景候选"
        if re.search(
            r"(^|[_-])(agbh|agbeh|ceo2|silicon|si)(?=[_-]|\d|$)", stem
        ):
            return "标样候选"
        return None

    @QtCore.Slot(str)
    def _apply_batch_scope(self, scope: str) -> None:
        root_text = self.browser.path_edit.text().strip()
        root = Path(root_text).expanduser().absolute() if root_text else None
        active_path = (
            self._active_pane.current_path
            if self._active_pane is not None
            else None
        )
        if scope == "selected":
            candidates = list(
                self.browser.highlighted_paths or self.browser.selected_paths
            )
        elif scope == "current":
            candidates = [] if active_path is None else [active_path]
        elif root is None or not root.is_dir():
            self.recipe_panel.set_status("请先选择有效的数据文件夹。", error=True)
            return
        elif scope == "folder":
            candidates = [
                item.path for item in self.browser.files if item.path.parent == root
            ]
        elif scope == "recursive":
            if not self.browser.recursive_check.isChecked():
                self.browser.recursive_check.setChecked(True)
            candidates = [item.path for item in self.browser.files]
        elif scope == "sequence":
            if active_path is None:
                self.recipe_panel.set_status("当前没有可用于识别序列的图像。", error=True)
                return
            key = self._sequence_key(active_path)
            if key is None:
                self.recipe_panel.set_status(
                    "当前文件名末尾没有序号；无法自动识别原位序列。", error=True
                )
                return
            candidates = [
                item.path
                for item in self.browser.files
                if item.path.parent == active_path.parent
                and self._sequence_key(item.path) == key
            ]
        else:
            self.recipe_panel.set_status(f"未知批处理范围：{scope}", error=True)
            return

        configured_exclusions: dict[str, str] = {}
        background_path = self.background_panel.background_path
        if background_path is not None:
            configured_exclusions[str(background_path.absolute())] = "已确认背景"
        external_mask_path = self.mask_panel.external_mask_path
        if external_mask_path is not None:
            configured_exclusions[str(external_mask_path.absolute())] = "外部MASK"
        if self.current_geometry is not None:
            source_path = self.current_geometry.provenance.source_path
            if source_path and self.current_geometry.provenance.method!='instrument_header':
                configured_exclusions[str(Path(source_path).expanduser().absolute())] = (
                    "几何标定源"
                )

        accepted: list[Path] = []
        excluded: list[tuple[Path, str]] = []
        for path in candidates:
            absolute = path.expanduser().absolute()
            if not supported_detector_path(absolute):
                excluded.append((absolute, "格式尚未通过读取Gate"))
                continue
            reason = configured_exclusions.get(str(absolute))
            if reason is None and scope in {"folder", "recursive", "sequence"}:
                reason = self._automatic_non_sample_role(absolute)
            if reason is not None:
                excluded.append((absolute, reason))
                continue
            accepted.append(absolute)

        selected = self.browser.select_paths(tuple(accepted))
        self._update_batch_selection_count()
        self._batch_scope_rule = {
            "current": "reviewed_current_image",
            "selected": "explicit_left_panel_selection",
            "folder": "reviewed_current_folder",
            "recursive": "reviewed_recursive_folder",
            "sequence": "reviewed_filename_sequence",
        }[scope]
        if not selected:
            self.recipe_panel.set_status("该范围没有可执行样品。", error=True)
            return
        self._batch_plan_draft = None
        self.recipe_panel.set_revision_summary(
            "范围已改变；请先保存批次方案以建立base版本。"
        )
        exclusion_note = ""
        if excluded:
            shown = "、".join(f"{path.name}（{reason}）" for path, reason in excluded[:3])
            exclusion_note = f"；已排除 {len(excluded)} 个：{shown}"
        self.recipe_panel.set_status(
            f"范围已转换为左侧 {len(selected)} 个可见选择，请检查后再执行"
            f"{exclusion_note}。"
        )

    @QtCore.Slot(object)
    def _file_selected(self, path: Path, selection=None, preview=None) -> None:
        self._integration_preview_timer.stop()
        self._integration_tasks.invalidate()
        self._background_tasks.invalidate()
        self._mask_tasks.invalidate()
        pane = self._active_pane
        if pane is None:
            return
        master=getattr(self.browser,"source_masters",{}).get(str(path.resolve()))
        if master is not None:
            path=master
            selection=None
            blocker=QtCore.QSignalBlocker(self.browser)
            self.browser.set_current_path(path)
            del blocker
        if not supported_detector_path(path):
            self.statusBar().showMessage(f"未读取 {path.name}：该格式尚未通过读取Gate")
            self.capability_label.setText("读取：仅发现 | 分析：不可用")
            return
        frame = preview.frame if preview is not None else next(
            (
                candidate.frame
                for candidate in self._panes
                if selection is None and candidate is not pane and candidate.current_path == path and candidate.frame is not None
            ),
            None,
        )
        if frame is None:
            try:
                if selection is None:
                    try:
                        frame=read_tvx_tiff(path,dataset=self._detector_dataset_choices.get(str(path.resolve())))
                    except SourceSelectionRequired:
                        try:
                            options=source_selections(path,dataset=self._detector_dataset_choices.get(str(path.resolve())),all_frames=False)
                            selection=options[0]
                        except SourceSelectionRequired as exc:
                            names=[f"{item['dataset']} · {item['shape']}" for item in exc.choices]
                            name,accepted=QtWidgets.QInputDialog.getItem(self,'选择探测器数据集','数据集',names,0,False)
                            if not accepted:return
                            dataset=exc.choices[names.index(name)]['dataset']
                            self._detector_dataset_choices[str(path.resolve())]=dataset
                            selection={'dataset':dataset,'frame_index':0}
                        frame=read_tvx_tiff(path,**selection)
                else:
                    frame=read_tvx_tiff(path,**selection)
            except TiffReaderError as exc:
                self.statusBar().showMessage(f"读取失败 {path.name}：{exc}")
                pane.viewer.status_label.setText(f"读取失败：{path.name}\n{exc}")
                pane.viewer.status_label.setWordWrap(True)
                self.capability_label.setText("读取：失败/不支持 | 分析：不可用")
                return
        normalized=(preview.normalized if preview is not None and
            preview.preset==pane.viewer.preset else None)
        pane.set_frame(path, frame,normalized)
        self._register_browser_frames(path,frame)
        if frame.metadata.dataset_path:
            choices=source_catalog(path)
            blocker=QtCore.QSignalBlocker(pane.dataset_combo)
            pane.dataset_combo.clear()
            for item in choices:pane.dataset_combo.addItem(item['dataset'],item['dataset'])
            pane.dataset_combo.setCurrentIndex(pane.dataset_combo.findData(frame.metadata.dataset_path))
            pane.dataset_combo.setVisible(len(choices)>1)
            del blocker
        else:
            pane.dataset_combo.hide()
        saved_geometry=self._restore_processed_context(pane)
        pane.viewer.set_inspection_geometry(self.current_geometry)
        if pane.integration_region is None:
            pane.integration_region = self.integration_panel.region_descriptor()
        if self.current_geometry is not None:
            self._apply_integration_overlay_to_pane(pane)
        self.background_panel.set_current_sample(path, frame.metadata.exposure_time_s)
        self.mask_panel.set_current_frame(path, frame)
        self.mask_panel.set_region_count(
            pane.viewer.manual_mask_region_count,
            synchronized=self.display_linker.linked,
        )
        if pane is self._active_pane:
            self.geometry_panel.set_image_metadata(frame.metadata)
        if self.display_linker.linked:
            pane.viewer.image_view.imageItem.setLevels(self._linked_levels)
            self.display_linker.sync_view_from(pane.viewer)
        if pane is self._active_pane:
            self._refresh_tone_bar(pane)
        exposure = frame.metadata.exposure_time_s
        exposure_text = "未知" if exposure is None else f"{exposure:g} s"
        self.statusBar().showMessage(
            f"{path.name} | {frame.metadata.width_px}×{frame.metadata.height_px} | "
            f"{frame.metadata.dtype} | 曝光 {exposure_text} | 帧 {frame.metadata.frame_index+1}/{frame.metadata.frame_count} | "
            + ('只读处理数组' if frame.metadata.embedded_processing else '只读原始数组')
        )
        self._schedule_navigation_refresh()
        self.capability_label.setText("读取：定量候选 | 几何：文件内参数" if saved_geometry is not None else "读取：定量候选 | 分析：需波长、SDD和中心点")

    @QtCore.Slot()
    def open_comparison_window(self) -> EmbeddedViewerPane:
        if len(self._panes) >= 9:
            self.statusBar().showMessage("最多同时显示9个内嵌图像窗格")
            return self._panes[-1]
        pane = self._create_pane(primary=False)
        self._activate_pane(pane)
        selected = self.browser.selected_path
        if selected is not None:
            self._file_selected(selected)
        return pane

    @property
    def panes(self) -> tuple[EmbeddedViewerPane, ...]:
        return tuple(self._panes)

    @property
    def active_pane(self) -> EmbeddedViewerPane | None:
        return self._active_pane

    def _create_pane(self, *, primary: bool) -> EmbeddedViewerPane:
        pane = EmbeddedViewerPane(self._next_pane_number, primary=primary)
        if self._active_pane is not None and self._active_pane.integration_region is not None:
            pane.integration_region = dict(self._active_pane.integration_region)
        else:
            pane.integration_region = self.integration_panel.region_descriptor()
        self._next_pane_number += 1
        pane.activated.connect(self._activate_pane)
        pane.viewer.activated.connect(lambda selected=pane: self._activate_pane(selected))
        pane.navigate_requested.connect(self._navigate_pane)
        pane.frame_requested.connect(self._select_detector_frame)
        pane.frame_player.ready.connect(lambda preview,source=pane:self._display_detector_frame(source,preview))
        pane.frame_player.failed.connect(lambda error,source=pane:self._detector_frame_failed(source,error))
        pane.dataset_requested.connect(self._select_detector_dataset)
        pane.batch_frames_requested.connect(self._choose_batch_frames)
        pane.close_requested.connect(self._close_pane)
        self._panes.append(pane)
        self.display_linker.add_viewer(pane.viewer)
        pane.viewer.preset_changed.connect(
            lambda preset, source=pane.viewer: self._viewer_preset_changed(source, preset)
        )
        pane.viewer.geometry_center_dragged.connect(
            lambda x, y, source=pane.viewer: self._geometry_center_dragged(source, x, y)
        )
        pane.viewer.geometry_center_drag_finished.connect(
            lambda x, y, source=pane.viewer: self._geometry_center_drag_finished(source, x, y)
        )
        pane.viewer.geometry_distance_dragged.connect(
            lambda distance, source=pane.viewer: self._geometry_distance_dragged(
                source, distance
            )
        )
        pane.viewer.geometry_distance_drag_finished.connect(
            lambda distance, source=pane.viewer: self._geometry_distance_drag_finished(
                source, distance
            )
        )
        pane.viewer.manual_ring_point_picked.connect(
            lambda x, y, source=pane.viewer: self._manual_ring_point_picked(
                source, x, y
            )
        )
        pane.viewer.manual_mask_changed.connect(
            lambda descriptors, source=pane.viewer: self._manual_mask_regions_changed(
                source, descriptors
            )
        )
        pane.viewer.integration_region_changed.connect(
            lambda descriptor, source=pane.viewer: self._integration_region_changed(
                source, descriptor
            )
        )
        pane.viewer.integration_overlay.drag_state_changed.connect(self._integration_drag_state_changed)
        self._relayout_panes()
        return pane

    def _restore_processed_context(self,pane):
        frame=pane.frame
        saved_geometry=getattr(pane,'geometry_override',None) or embedded_geometry(frame)
        if saved_geometry is not None:
            self.geometry_panel.set_geometry(saved_geometry)
            self._restoring_file_geometry=True
            try:self._apply_candidate_geometry(saved_geometry)
            finally:self._restoring_file_geometry=False
        elif frame.metadata.embedded_processing:
            self.current_geometry=None
        if frame.metadata.embedded_processing:
            if not hasattr(self,'_mode_before_processed'):
                self._mode_before_processed=self.background_panel.mode
            mode=BackgroundMode.UPSTREAM_SUBTRACTED if frame.metadata.embedded_processing.get('background_applied') else BackgroundMode.NO_BACKGROUND
            self.background_panel.mode_combo.setCurrentIndex(self.background_panel.mode_combo.findData(mode.value))
            if pane.mask_result is None:
                pane.mask_result=build_mask(frame)
            pane.viewer.set_mask_overlay(pane.mask_result.excluded)
        elif hasattr(self,'_mode_before_processed'):
            self.background_panel.mode_combo.setCurrentIndex(self.background_panel.mode_combo.findData(self._mode_before_processed.value))
            del self._mode_before_processed
        pane.viewer.set_inspection_geometry(self.current_geometry)
        if frame.metadata.detector_mask is not None and pane.mask_result is None:
            pane.mask_result=build_mask(frame)
            pane.viewer.set_mask_overlay(pane.mask_result.excluded)
        return saved_geometry

    def _select_detector_frame(self,pane,index):
        if pane.frame is None or pane.current_path is None:return
        self._activate_pane(pane)
        pane.frame_player.configure(pane.frame,pane.viewer.preset)
        if pane.frame_player.pending_index==index:
            return
        if index==pane.frame.metadata.frame_index:
            pane.frame_player.pause()
            return
        self.statusBar().showMessage(f"正在读取第 {index+1} 帧…")
        pane.frame_player.jump(index)

    def _display_detector_frame(self,pane,preview):
        if pane not in self._panes:
            return
        if pane is not self._active_pane:
            pane.set_frame(pane.current_path,preview.frame,
                preview.normalized if preview.preset==pane.viewer.preset else None)
            self._apply_integration_overlay_to_pane(pane)
            self._schedule_navigation_refresh()
            return
        self._file_selected(pane.current_path,preview=preview)

    def _detector_frame_failed(self,pane,error):
        blocker=QtCore.QSignalBlocker(pane.frame_spin)
        pane.frame_spin.setValue(pane.frame.metadata.frame_index+1)
        del blocker
        pane.viewer.status_label.setText(f"读取失败：{error}")
        self.statusBar().showMessage(f"帧读取失败：{error}")

    def _select_detector_dataset(self,pane,dataset):
        if pane.frame is None or not dataset or dataset=='None':return
        self._activate_pane(pane)
        key=str(pane.current_path.resolve())
        self._detector_frame_choices.pop(key,None)
        if key in self._detector_dataset_choices:self._detector_dataset_choices[key]=dataset
        self._file_selected(pane.current_path,{'dataset':dataset,'frame_index':0})

    def _choose_batch_frames(self,pane):
        if pane.frame is None or pane.current_path is None:
            return
        from .frame_selection import FrameSelectionDialog
        key=str(pane.current_path.resolve())
        dialog=FrameSelectionDialog(pane.frame.metadata.frame_count,
            self._detector_frame_choices.get(key),self)
        if dialog.exec()!=QtWidgets.QDialog.DialogCode.Accepted:
            return
        selected=dialog.selected_frames()
        if pane.frame.metadata.dataset_path:
            self._detector_dataset_choices[key]=pane.frame.metadata.dataset_path
        if selected is None:
            self._detector_frame_choices.pop(key,None)
        else:
            self._detector_frame_choices[key]=selected
        total=pane.frame.metadata.frame_count if selected is None else len(selected)
        self._register_browser_frames(pane.current_path,pane.frame)
        for row,source in enumerate(self.browser.files):
            if source.path.resolve()==pane.current_path.resolve():
                blocker=QtCore.QSignalBlocker(self.browser.file_list)
                state=(QtCore.Qt.CheckState.Checked if total==pane.frame.metadata.frame_count else
                       QtCore.Qt.CheckState.PartiallyChecked if total else QtCore.Qt.CheckState.Unchecked)
                self.browser.file_list.item(row).setCheckState(state)
                del blocker
                break
        self.browser.checked_selection_changed.emit()
        self.statusBar().showMessage(f"{pane.current_path.name}：批次选择 {total} 帧")

    def _register_browser_frames(self,path,frame):
        for row,source in enumerate(self.browser.files):
            if source.path.resolve()==path.resolve():
                blocker=QtCore.QSignalBlocker(self.browser.file_list)
                self.browser.file_list.register(row,path,frame.metadata.frame_count,
                    self._detector_frame_choices.get(str(path.resolve())))
                del blocker
                break

    def _browser_frame_selected(self,path,index):
        pane=self._active_pane
        if pane is None:return
        if pane.current_path!=path:
            self._file_selected(path)
        self._select_detector_frame(pane,index)

    def _browser_frames_checked(self,path,selected):
        self._detector_frame_choices[str(path.resolve())]=selected
        pane=next((p for p in self._panes if p.current_path==path and p.frame is not None),None)
        if pane is not None and pane.frame.metadata.dataset_path:
            self._detector_dataset_choices[str(path.resolve())]=pane.frame.metadata.dataset_path
        self.browser.checked_selection_changed.emit()

    def _compare_browser_frames(self):
        selected=self.browser.file_list.selected_frame_items()
        if not selected:
            self.statusBar().showMessage("请展开样品，选择需要对比的帧。")
            return
        if len(selected)>9-len(self._panes):
            self.statusBar().showMessage(f"当前可再打开 {9-len(self._panes)} 个窗格，请减少选择。")
            return
        for path,index in selected:
            pane=self._create_pane(primary=False)
            self._activate_pane(pane)
            self._file_selected(path)
            self._select_detector_frame(pane,index)

    @QtCore.Slot(object)
    def _activate_pane(self, pane: EmbeddedViewerPane) -> None:
        if pane not in self._panes:
            return
        if self._active_pane is not pane:
            self._integration_tasks.invalidate()
        self._active_pane = pane
        for candidate in self._panes:
            if candidate is not pane and candidate.frame_player.playing:
                candidate.frame_player.pause()
            candidate.set_active(candidate is pane)
        self._install_active_controls(pane)
        self._refresh_tone_bar(pane)
        if pane.integration_region is None:
            pane.integration_region = self.integration_panel.region_descriptor()
        self.integration_panel.set_region_descriptor(pane.integration_region)
        if self.current_geometry is not None and pane.frame is not None:
            self._apply_integration_overlay_to_pane(pane)
        if pane.frame is not None:
            self._restore_processed_context(pane)
            self.geometry_panel.set_image_metadata(pane.frame.metadata)
            self.background_panel.set_current_sample(
                pane.current_path,
                pane.frame.metadata.exposure_time_s,
            )
            self.mask_panel.set_current_frame(pane.current_path, pane.frame)
            self.mask_panel.set_region_count(
                pane.viewer.manual_mask_region_count,
                synchronized=self.display_linker.linked,
            )
        if pane.current_path is not None:
            for index, discovered in enumerate(self.browser.files):
                if discovered.path == pane.current_path:
                    blocker = QtCore.QSignalBlocker(self.browser.file_list)
                    self.browser.file_list.setCurrentRow(index)
                    del blocker
                    break

    @QtCore.Slot(object, int)
    def _navigate_pane(self, pane: EmbeddedViewerPane, step: int) -> None:
        if not self.browser.files:
            return
        self._activate_pane(pane)
        current = 0
        if pane.current_path is not None:
            for index, discovered in enumerate(self.browser.files):
                if discovered.path == pane.current_path:
                    current = index
                    break
        target = min(max(current + step, 0), len(self.browser.files) - 1)
        self.browser.file_list.setCurrentRow(target)

    @QtCore.Slot(object)
    def _close_pane(self, pane: EmbeddedViewerPane) -> None:
        pane.frame_player.pause()
        if pane.frame_player.busy:
            self.statusBar().showMessage("正在完成帧读取，请稍后关闭窗格。")
            return
        if pane.primary or pane not in self._panes:
            return
        self.display_linker.remove_viewer(pane.viewer)
        self._panes.remove(pane)
        if self._active_pane is pane:
            self._activate_pane(self._panes[0])
        pane.deleteLater()
        self._relayout_panes()

    def _install_active_controls(self, pane: EmbeddedViewerPane) -> None:
        controls = pane.viewer.controls_widget
        if self.shared_controls is controls:
            return
        if self.shared_controls is not None:
            self.display_toolbar_layout.removeWidget(self.shared_controls)
            self.shared_controls.hide()
            previous_owner = next(
                (
                    candidate.viewer
                    for candidate in self._panes
                    if candidate.viewer.controls_widget is self.shared_controls
                ),
                None,
            )
            if previous_owner is not None:
                self.shared_controls.setParent(previous_owner)
                previous_owner.layout().insertWidget(0, self.shared_controls)
        pane.viewer.layout().removeWidget(controls)
        self.shared_controls = controls
        controls.setParent(self.display_toolbar)
        controls.show()
        self.display_toolbar_layout.insertWidget(0, controls)

    def _refresh_tone_bar(self, pane: EmbeddedViewerPane) -> None:
        levels = pane.viewer.image_view.imageItem.getLevels()
        if levels is None:
            levels = (0.0, 1.0)
        self.tone_bar.set_levels(float(levels[0]), float(levels[1]))
        preset = pane.viewer.preset
        self.tone_bar.set_colormap(
            resolve_gui_colormap(
                preset.colormap,
                preset.invert_colormap,
                preset.custom_colors or DEFAULT_CUSTOM_COLORS,
            )
        )

    @QtCore.Slot(bool)
    def _sync_check_toggled(self, checked: bool) -> None:
        blocker = QtCore.QSignalBlocker(self.sync_display_action)
        self.sync_display_action.setChecked(checked)
        del blocker
        self._set_display_sync(checked)

    @QtCore.Slot(bool)
    def _sync_action_toggled(self, checked: bool) -> None:
        blocker = QtCore.QSignalBlocker(self.sync_display_check)
        self.sync_display_check.setChecked(checked)
        del blocker
        self._set_display_sync(checked)

    def _set_display_sync(self, checked: bool) -> None:
        source = self._active_pane.viewer if self._active_pane is not None else None
        self.display_linker.set_linked(checked, source=source)
        if source is None:
            return
        levels = source.image_view.imageItem.getLevels()
        if levels is None:
            levels = (0.0, 1.0)
        self._linked_levels = (float(levels[0]), float(levels[1]))
        if checked:
            self._apply_levels_to_viewers(self._linked_levels)
            descriptors = source.manual_mask_descriptors()
            source_shape = None if source.data is None else source.data.shape
            for pane in self._panes:
                if pane.viewer is source or pane.frame is None:
                    continue
                if pane.frame.data.shape == source_shape:
                    pane.viewer.set_manual_mask_descriptors(descriptors, emit=False)
                    pane.mask_result = None
                    pane.viewer.clear_mask_overlay()
            if self._active_pane is not None:
                region = self._active_pane.integration_region
                if region is None:
                    region = self.integration_panel.region_descriptor()
                    self._active_pane.integration_region = dict(region)
                for pane in self._panes:
                    if pane is self._active_pane or pane.frame is None:
                        continue
                    if self._active_pane.frame is not None:
                        pane.integration_region = dict(region)
                        pane.integration_result = None
                        self._apply_integration_overlay_to_pane(pane)
                self._schedule_integration_preview()
        if self._active_pane is not None:
            self._refresh_tone_bar(self._active_pane)
            self.mask_panel.set_region_count(
                self._active_pane.viewer.manual_mask_region_count,
                synchronized=checked,
            )
        mode = (
            "调色/缩放/平移/MASK区域同步全部窗格"
            if checked
            else "各窗格独立显示"
        )
        self.statusBar().showMessage(f"显示同步：{mode}")

    @QtCore.Slot()
    def _tone_levels_changed(self, low: float, high: float) -> None:
        if self._propagating_levels:
            return
        if self._active_pane is None:
            return
        normalized = (float(low), float(high))
        if self.display_linker.linked:
            self._linked_levels = normalized
            self._apply_levels_to_viewers(normalized)
        else:
            self._active_pane.viewer.image_view.imageItem.setLevels(normalized)

    def _apply_levels_to_viewers(self, levels: tuple[float, float]) -> None:
        self._propagating_levels = True
        try:
            for pane in self._panes:
                pane.viewer.image_view.imageItem.setLevels(levels)
        finally:
            self._propagating_levels = False

    @QtCore.Slot(object, object)
    def _viewer_preset_changed(
        self,
        source: ImageViewerWidget,
        preset: DisplayPreset,
    ) -> None:
        if self._active_pane is None or source is not self._active_pane.viewer:
            return
        self.tone_bar.set_colormap(
            resolve_gui_colormap(
                preset.colormap,
                preset.invert_colormap,
                preset.custom_colors or DEFAULT_CUSTOM_COLORS,
            )
        )

    def _relayout_panes(self) -> None:
        count = len(self._panes)
        columns = 1 if count == 1 else 2 if count in {2, 4} else 3
        # Preserve the pane widgets while replacing only the splitter topology.
        # This gives every row and column a visible draggable handle without
        # recreating viewers or re-reading detector data.
        for pane in self._panes:
            pane.setParent(self.workspace_widget)
        for row_splitter in self.workspace_row_splitters:
            row_splitter.setParent(None)
            row_splitter.deleteLater()
        self.workspace_row_splitters = []

        rows = (count + columns - 1) // columns
        for row in range(rows):
            row_splitter = FineTuneSplitter(QtCore.Qt.Orientation.Horizontal)
            self._configure_splitter(row_splitter)
            self.workspace_splitter.addWidget(row_splitter)
            self.workspace_row_splitters.append(row_splitter)
            start = row * columns
            for pane in self._panes[start : start + columns]:
                row_splitter.addWidget(pane)
                row_splitter.setStretchFactor(row_splitter.count() - 1, 1)
                row_splitter.setCollapsible(row_splitter.count() - 1, True)
            # Large equal weights avoid Qt falling back to unequal size hints
            # when tiny values are below the panes' minimum-size requests.
            row_splitter.setSizes([1000] * row_splitter.count())
            self.workspace_splitter.setStretchFactor(row, 1)
            self.workspace_splitter.setCollapsible(row, True)
        self.workspace_splitter.setSizes([1000] * rows)
        self.workspace_layout.invalidate()
        self.workspace_widget.updateGeometry()
        # Geometry changes are queued by Qt. Fit each image after the new cell
        # sizes are known, including when panes are added or closed.
        for pane in self._panes:
            pane.viewer.schedule_fit_image_to_view()

    @QtCore.Slot(object)
    def _geometry_preview_changed(self, payload: object) -> None:
        pane = self._active_pane
        if pane is None:
            return
        pane.viewer.clear_geometry_overlay()
        if payload is None or pane.viewer.data is None:
            return
        geometry, calibrant, max_rings = payload
        try:
            count = pane.viewer.set_calibrant_overlay(geometry, calibrant, max_rings)
        except GeometryError as exc:
            self.geometry_panel.status_label.setText(f"无法预览：{exc}")
            return
        self.geometry_panel.status_label.setText(
            f"{calibrant.name}：显示 {count} 条可见理论环；候选参数未标定"
        )

    @QtCore.Slot(object)
    def _apply_candidate_geometry(self, geometry: DetectorGeometry) -> None:
        self._integration_tasks.invalidate()
        pane = self._active_pane
        if pane is None or pane.viewer.data is None:
            self.geometry_panel.status_label.setText("请先在当前窗格载入二维图像")
            return
        if geometry.image_width_px != pane.viewer.data.shape[1] or geometry.image_height_px != pane.viewer.data.shape[0]:
            self.geometry_panel.status_label.setText("无法应用：几何尺寸与当前图像不一致")
            return
        shape=pane.viewer.data.shape
        previous_key=None if self.current_geometry is None else self._integration_cache_key(shape,"q_nm^-1")
        self.current_geometry = geometry
        if not getattr(self,'_restoring_file_geometry',False):
            pane.geometry_override=geometry
        if previous_key!=self._integration_cache_key(shape,"q_nm^-1"):
            self._integration_radial_cache.clear()
            self._overlay_tasks.invalidate()
            self._overlay_pending_key=None
        for candidate in self._panes:
            if candidate.frame is not None:
                candidate.viewer.set_inspection_geometry(self._pane_geometry(candidate))
                self._apply_integration_overlay_to_pane(candidate)
        if geometry.provenance.method == "pyfai_guided_untilted_candidate":
            self.capability_label.setText(
                "读取：定量候选 | 几何：建议已应用（未标定）"
            )
            self.statusBar().showMessage(
                "已应用自动建议候选；仍需人工叠环和独立跨软件验证"
            )
        else:
            self.capability_label.setText("读取：定量候选 | 几何：输入参数")
            self.statusBar().showMessage(
                "已应用输入几何参数，可直接积分；请确认参数来源与适用性。"
            )

    @QtCore.Slot(object, object)
    def _start_geometry_suggestion(
        self,
        geometry: DetectorGeometry,
        calibrant: Calibrant,
    ) -> None:
        if self._calibration_thread is not None and self._calibration_thread.isRunning():
            self.geometry_panel.status_label.setText("标样建议正在计算，请等待完成")
            return
        pane = self._active_pane
        if pane is None or pane.viewer.data is None:
            self.geometry_panel.status_label.setText("请先在当前窗格载入标样二维图像")
            return
        if pane.viewer.data.shape != (
            geometry.image_height_px,
            geometry.image_width_px,
        ):
            self.geometry_panel.status_label.setText("无法建议：几何尺寸与当前图像不一致")
            return
        local_refinement = (
            geometry.provenance.method == "manual_assigned_ring_candidate"
            or geometry.metadata.get("calibration_refinement_mode") == "local"
        )
        self.geometry_panel.set_calibration_busy(
            True, local_refinement=local_refinement
        )
        self.capability_label.setText("读取：定量候选 | 几何：正在自动建议")
        self.statusBar().showMessage(
            (
                "正在识别样品对称中心，保持输入SDD；" if calibrant is None else
                f"正在从三点候选附近提取 {calibrant.name} 多环控制点并局部精修；"
                if local_refinement
                else f"正在用 {calibrant.name} 全图粗找中心并提取多环控制点；"
            )
            + "界面可继续查看图像"
        )
        thread = QtCore.QThread(self)
        worker = CalibrationWorker(
            pane.viewer.data,
            geometry,
            calibrant,
            coarse_center_enabled=not local_refinement,
            mask=pane.viewer.mask,
        )
        worker.moveToThread(thread)
        thread.started.connect(
            worker.run,
            QtCore.Qt.ConnectionType.DirectConnection,
        )
        worker.finished.connect(self._geometry_suggestion_finished)
        worker.failed.connect(self._geometry_suggestion_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._geometry_suggestion_thread_finished)
        self._calibration_thread = thread
        self._calibration_worker = worker
        thread.start()

    @QtCore.Slot(object, object, int)
    def _start_manual_ring_pick(
        self,
        geometry: DetectorGeometry,
        calibrant: Calibrant,
        ring_ordinal: int,
    ) -> None:
        pane = self._active_pane
        if pane is None or pane.viewer.data is None:
            self.geometry_panel.status_label.setText("请先在当前窗格载入标样二维图像")
            return
        self._cancel_manual_ring_pick(clear_status=False)
        self._manual_ring_pick_context = {
            "viewer": pane.viewer,
            "geometry": geometry,
            "calibrant": calibrant,
            "ring_ordinal": int(ring_ordinal),
            "points": [],
        }
        pane.viewer.begin_manual_ring_picking()
        self.geometry_panel.set_manual_ring_pick_active(True, 0)
        self.statusBar().showMessage(
            "三点定中心：请在同一条样品圆环上点击3个分散的点，保持输入SDD" if calibrant is None else
            f"三点定环：请在 {calibrant.name} 第 {ring_ordinal} 条真实环上点击3个相距较远的点"
        )

    @QtCore.Slot()
    def _cancel_manual_ring_pick(self, *, clear_status: bool = True) -> None:
        context = self._manual_ring_pick_context
        if context is not None:
            viewer = context.get("viewer")
            if isinstance(viewer, ImageViewerWidget):
                viewer.end_manual_ring_picking()
                viewer.clear_manual_ring_pick_points()
        self._manual_ring_pick_context = None
        self.geometry_panel.set_manual_ring_pick_active(False)
        if clear_status:
            self.geometry_panel.status_label.setText("已取消三点定环；候选参数未改变")

    @QtCore.Slot(object, float, float)
    def _manual_ring_point_picked(
        self,
        source: ImageViewerWidget,
        x_px: float,
        y_px: float,
    ) -> None:
        context = self._manual_ring_pick_context
        if context is None or context.get("viewer") is not source:
            return
        points = context["points"]
        if not isinstance(points, list):
            return
        points.append((float(x_px), float(y_px)))
        self.geometry_panel.set_manual_ring_pick_active(True, len(points))
        if len(points) < 3:
            return
        try:
            estimate = fit_manual_calibrant_ring(
                context["geometry"],
                context["calibrant"],
                int(context["ring_ordinal"]),
                points,
            )
        except (CalibrationError, GeometryError, ValueError) as exc:
            self._cancel_manual_ring_pick(clear_status=False)
            self.geometry_panel.status_label.setText(f"三点定环失败：{exc}")
            return
        source.end_manual_ring_picking()
        source.clear_manual_ring_pick_points()
        self._manual_ring_pick_context = None
        self.geometry_panel.show_manual_ring_estimate(estimate)
        candidate = estimate.candidate_geometry
        self.capability_label.setText("读取：定量候选 | 几何：三点候选待确认")
        if estimate.ring_ordinal is None:
            self.statusBar().showMessage(f"中心候选 ({candidate.beam_center_x_px:.2f}, {candidate.beam_center_y_px:.2f}) px；SDD保持 {candidate.detector_distance_m*1e3:.3f} mm")
            return
        self.statusBar().showMessage(
            f"三点候选：中心 ({candidate.beam_center_x_px:.2f}, "
            f"{candidate.beam_center_y_px:.2f}) px，SDD "
            f"{candidate.detector_distance_m * 1e3:.2f} mm；请用多环复核"
        )

    @QtCore.Slot(object)
    def _geometry_suggestion_finished(self, suggestion: CalibrationSuggestion) -> None:
        if isinstance(suggestion,DetectorGeometry):
            self.geometry_panel.set_calibration_busy(False)
            self.geometry_panel.set_geometry(suggestion)
            self.geometry_panel.residual_group.hide()
            message=f"中心候选 ({suggestion.beam_center_x_px:.3f}, {suggestion.beam_center_y_px:.3f}) px；SDD保持 {suggestion.detector_distance_m*1e3:.3f} mm，确认后应用。"
            self.geometry_panel.status_label.setText(message)
            self.statusBar().showMessage(message)
            self.capability_label.setText("读取：定量候选 | 几何：中心待确认")
            return
        self.current_calibration_suggestion = suggestion
        self.geometry_panel.set_calibration_busy(False)
        self.geometry_panel.show_calibration_suggestion(suggestion)
        run = suggestion.selected_run
        self.capability_label.setText("读取：定量候选 | 几何：自动建议待确认")
        self.statusBar().showMessage(
            f"自动建议完成：{run.ring_count} 环/{run.control_point_count} 控制点，"
            f"RMS {run.final_rms_two_theta_rad * 1e6:.2f} µrad；请人工叠环确认"
        )

    @QtCore.Slot(str)
    def _geometry_suggestion_failed(self, message: str) -> None:
        self.geometry_panel.set_calibration_busy(False)
        self.geometry_panel.status_label.setText(f"自动建议失败：{message}")
        self.capability_label.setText("读取：定量候选 | 几何：建议失败")
        self.statusBar().showMessage("标样建议失败；原始图像和手动参数未改变")

    @QtCore.Slot()
    def _geometry_suggestion_thread_finished(self) -> None:
        self._calibration_worker = None
        self._calibration_thread = None

    @QtCore.Slot(object, float, float)
    def _geometry_center_dragged(
        self,
        source: ImageViewerWidget,
        x_px: float,
        y_px: float,
    ) -> None:
        if self._active_pane is None or source is not self._active_pane.viewer:
            return
        self.geometry_panel.set_center_values(x_px, y_px, emit_preview=False)
        self.geometry_panel.status_label.setText(
            f"中心候选 x={x_px:.3f}, y={y_px:.3f} px；可继续拖动或用方向键微调"
        )
        if self.current_geometry is not None:
            self.capability_label.setText("读取：定量候选 | 几何：候选已修改（待应用）")

    @QtCore.Slot(object, float, float)
    def _geometry_center_drag_finished(
        self,
        source: ImageViewerWidget,
        x_px: float,
        y_px: float,
    ) -> None:
        if self._active_pane is None or source is not self._active_pane.viewer:
            return
        self.geometry_panel.set_center_values(x_px, y_px, emit_preview=True)

    @QtCore.Slot(object, float)
    def _geometry_distance_dragged(
        self,
        source: ImageViewerWidget,
        distance_m: float,
    ) -> None:
        if self._active_pane is None or source is not self._active_pane.viewer:
            return
        self.geometry_panel.set_distance_value(distance_m, emit_preview=False)
        self.geometry_panel.status_label.setText(
            f"SDD候选 {distance_m * 1e3:.3f} mm；松开后重绘全部理论环"
        )

    @QtCore.Slot(object, float)
    def _geometry_distance_drag_finished(
        self,
        source: ImageViewerWidget,
        distance_m: float,
    ) -> None:
        if self._active_pane is None or source is not self._active_pane.viewer:
            return
        self.geometry_panel.set_distance_value(distance_m, emit_preview=True)

    def _open_ruland_dialog(self) -> None:
        pane = self._active_pane
        if pane is None or pane.frame is None or pane.current_path is None or self.current_geometry is None:
            self.statusBar().showMessage("Ruland：请先打开样品并应用标定几何。")
            return
        try:
            recipe = self._processing_recipe_for_active_pane()
            air = None
            if recipe.background.mode is BackgroundMode.AUTO_AIR:
                path = self.background_panel.background_path
                if path is None:
                    raise ValueError("请先选择背景")
                air = read_tvx_tiff(path,**recipe.background.metadata.get('source_selection',{}))
            layers = list(pane.viewer.manual_mask_layers())
            external = self.mask_panel.external_mask_layer(pane.frame.data.shape)
            if external is not None:
                layers.insert(0, external)
            mask = self.mask_panel.recipe(tuple(layers))
            integration = AzimuthalIntegrationRecipe(radial_range=(.1, .2),
                radial_unit=RadialUnit.Q_NM_INV,
                input_unit="detector_count/s" if air is not None else "detector_count")
            q_map = detector_radial_array(self.current_geometry, pane.frame.data.shape,
                                          RadialUnit.Q_NM_INV)
            excluded = build_mask(pane.frame, mask).excluded
            descriptor = self.integration_panel.region_descriptor()
            preferred = None
            if descriptor["mode"] == "azimuthal":
                preferred = (float(descriptor["radial_lower"]), float(descriptor["radial_upper"]))
                unit = RadialUnit(descriptor["radial_unit"])
                if unit is RadialUnit.Q_ANGSTROM_INV:
                    preferred = tuple(value * 10 for value in preferred)
                elif unit is RadialUnit.S_NM_INV:
                    preferred = tuple(value * 2 * math.pi for value in preferred)
                elif unit is RadialUnit.TWO_THETA_DEG:
                    wavelength_nm = self.current_geometry.wavelength_m * 1e9
                    preferred = tuple(4 * math.pi * math.sin(math.radians(value) / 2) /
                                      wavelength_nm for value in preferred)
            project_key = (str(self._project_path), self.browser.path_edit.text().strip())
            defaults = self._ruland_defaults.get(project_key, {})
            initial_sampling = None
            if defaults.get("sampling_revision") != 4:
                prepared = apply_background(pane.frame, air, recipe.background)
                initial_sampling = suggest_ruland_sampling(
                    q_map, excluded, intensity=prepared.data)
                if preferred is not None and initial_sampling[3] != "radial_profile_peak_to_tail":
                    initial_sampling = suggest_ruland_sampling(q_map, excluded, preferred=preferred)
        except (ValueError, OSError, TiffReaderError) as exc:
            self.statusBar().showMessage(f"Ruland 准备失败：{exc}")
            return
        sample, geometry = pane.frame, self.current_geometry
        def run(**kwargs):
            return run_ruland_workflow(sample, air, geometry, recipe.background, mask, integration, **kwargs)
        def suggest_peaks(q_range):
            return suggest_ruland_peak_centers(sample, air, geometry, recipe.background,
                                               mask, integration, q_range=q_range, include_windows=True)
        try:
            from types import SimpleNamespace
            source_plan=self._batch_plan_for_current_selection()
            batch_sources=tuple(SimpleNamespace(path=item.path,identity=item,
                selection=source_plan.metadata.get('source_selections',[{}]*len(source_plan.sample_sources))[index],
                display_name=selection_label(source_plan,index))
                for index,item in enumerate(source_plan.sample_sources))
        except (ProcessingError, OSError, ValueError):
            batch_sources = ()
        def run_batch_item(identity, **kwargs):
            if FileIdentity.from_path(identity.path) != identity.identity:
                raise ProcessingError("样品文件在选择后发生变化")
            frame = read_tvx_tiff(identity.path,**identity.selection)
            return run_ruland_workflow(frame, air, embedded_geometry(frame) or geometry, recipe.background, mask, integration, **kwargs)
        dialog = RulandDialog(run, self, batch_sources=batch_sources,
                              run_batch_item=run_batch_item, initial_sampling=initial_sampling,
                              suggest_peaks=suggest_peaks, auto_batch_angle=True)
        if defaults.get("sampling_revision") == 4:
            dialog.restore_project_defaults(defaults)
        dialog.finished.connect(lambda _: self._ruland_defaults.__setitem__(
            project_key, dialog.project_defaults()))
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _processing_settings_changed(self,*args):
        for pane in self._panes:
            pane._follow_display_frame=None
        self._schedule_navigation_refresh()

    def _schedule_navigation_refresh(self):
        if not (self._follow_background or self._follow_mask or self._follow_integration):return
        active=self._active_pane
        if active is None or active.frame is None:return
        self._integration_preview_timer.stop()
        self._integration_tasks.invalidate()
        self.integration_panel.show_curves([],[])
        self.peak_fit_panel.set_curves([],[])
        for window in self._peak_fit_windows:
            if getattr(window,'_follow_live_preview',False):
                panel=window.fit_panel
                panel._interactive_tasks.invalidate()
                panel._results.clear()
                panel._result=None
                panel._clear_result_display()
                panel.status_label.setText('正在更新当前样品的处理曲线…')
        jobs=[]
        for pane in self._panes:
            if pane.frame is None or getattr(pane,'_follow_display_frame',None) is pane.frame:continue
            background_recipe=self.background_panel.background_recipe()
            if pane.frame.metadata.embedded_processing:
                background_recipe=replace(background_recipe,mode=(BackgroundMode.UPSTREAM_SUBTRACTED
                    if pane.frame.metadata.embedded_processing.get('background_applied') else BackgroundMode.NO_BACKGROUND))
            jobs.append(dict(pane=pane,frame=pane.frame,preset=pane.viewer.preset,
                background_recipe=background_recipe,background_path=self.background_panel.background_path,
                background_selection=dict(self.background_panel.source_selection),
                background_enabled=self._follow_background and background_recipe.mode is not BackgroundMode.NO_BACKGROUND,
                mask_enabled=self._follow_mask,mask_recipe=self.mask_panel.recipe(),
                manual_descriptors=pane.viewer.manual_mask_descriptors(),external_path=self.mask_panel.external_mask_path))
        if not jobs:
            if self._follow_integration:self._integration_preview_timer.start(0)
            return
        from .latest_task import prepare_navigation_previews
        self.background_panel.set_status('正在后台应用当前处理流程…')
        self._background_tasks.submit(lambda:prepare_navigation_previews(jobs,self._navigation_background_workspace))

    def _navigation_preview_ready(self,payload):
        from saxs_waxs_core.readers.detector_io import frame_label
        for job,background,mask,normalized in payload['results']:
            pane=job['pane']
            if pane not in self._panes or pane.frame is not job['frame']:continue
            if pane.viewer.preset!=job['preset']:
                pane._follow_display_frame=None
                self._schedule_navigation_refresh()
                return
            pane.background_result=background
            pane.mask_result=mask
            data=background.data if background is not None else pane.frame.data
            excluded=mask.excluded if mask is not None else pane.frame.metadata.detector_mask
            pane.viewer.set_data(data,excluded,normalized=normalized,fit=False)
            if mask is not None:pane.viewer.set_mask_overlay(mask.excluded)
            pane._follow_display_frame=pane.frame
            suffix=(' [背景已应用]' if background is not None else '')+(' [MASK已应用]' if mask is not None else '')
            pane.title_label.setText(f'图像 {pane.pane_number} · {frame_label(pane.frame)}{suffix}')
            pane.viewer.set_inspection_geometry(self.current_geometry)
            self._apply_integration_overlay_to_pane(pane)
            if pane is self._active_pane:self._refresh_tone_bar(pane)
        if payload['errors']:
            self.background_panel.set_status('；'.join(payload['errors'][:3]),error=True)
        else:
            self.background_panel.set_status('已按当前处理流程更新二维预览。')
        if self._follow_integration:self._integration_preview_timer.start(0)

    @QtCore.Slot(object, object)
    def _preview_background(
        self,
        mode: BackgroundMode,
        background_path: Path | None,
    ) -> None:
        pane = self._active_pane
        if pane is None or pane.frame is None or pane.current_path is None:
            self.background_panel.set_status("请先在当前窗格载入二维图像。", error=True)
            return
        if mode is BackgroundMode.AUTO_AIR and (background_path is None or background_path.absolute()==pane.current_path.absolute()):
            self.background_panel.set_status("请选择与样品不同的背景文件。",error=True)
            return
        frame=pane.frame
        selection=dict(self.background_panel.source_selection)
        recipe=self.background_panel.background_recipe()
        preset=pane.viewer.preset
        def calculate():
            background=None if mode is not BackgroundMode.AUTO_AIR else read_tvx_tiff(background_path,**selection)
            result=apply_background(frame,background,recipe)
            normalized=normalize_for_display(result.data,preset,frame.metadata.detector_mask)
            return pane,frame,mode,result,normalized,int(np.count_nonzero(result.data<0))
        self._integration_tasks.invalidate()
        self._background_tasks.submit(calculate)
        self.background_panel.set_status("正在后台预览背景…")

    def _background_preview_ready(self,payload):
        if isinstance(payload,dict) and payload.get('navigation'):
            self._navigation_preview_ready(payload)
            return
        pane,frame,mode,result,normalized,negative_count=payload
        if pane is not self._active_pane or pane.frame is not frame:return
        self._integration_tasks.invalidate()
        pane.background_result = result
        self._follow_background=mode is not BackgroundMode.NO_BACKGROUND
        pane._follow_display_frame=frame
        pane.mask_result = None
        pane.integration_result = None
        pane.viewer.set_data(result.data,frame.metadata.detector_mask,normalized=normalized,fit=False)
        pane.title_label.setText(
            f"图像 {pane.pane_number} · {pane.current_path.name} [背景预览]"
        )
        if mode is BackgroundMode.AUTO_AIR:
            assumption = (
                "曝光未知，按同批等曝光直接相减；"
                if result.audit.get("formula_version") == "air_background_equal_exposure_counts/1"
                else ""
            )
            status = (
                f"预览已应用；{assumption}输出 {result.output_unit}，"
                f"背景比例 {result.background_scale_to_sample:g}，"
                f"保留 {negative_count} 个负值像素。"
            )
        elif mode is BackgroundMode.NO_BACKGROUND:
            status = "已预览“不扣背景”；数值未改变，决定已记录。"
        else:
            status = "已预览“上游已扣背景”；数值未改变，来源语义已记录。"
        self.background_panel.set_status(status)
        self.capability_label.setText("读取：定量候选 | 背景：预览（未积分）")
        self.statusBar().showMessage(
            "背景预览仅改变当前显示缓冲；原始帧和源文件未修改"
        )
        if self._follow_mask or self._follow_integration:
            pane._follow_display_frame=None
            self._schedule_navigation_refresh()

    @QtCore.Slot()
    def _restore_raw_image(self) -> None:
        self._integration_preview_timer.stop()
        self._follow_background=False
        self._follow_mask=False
        self._follow_integration=False
        self._background_tasks.invalidate()
        self._mask_tasks.invalidate()
        self._integration_tasks.invalidate()
        self.integration_panel.show_curves([],[])
        self.peak_fit_panel.set_curves([],[])
        pane = self._active_pane
        if pane is None or pane.frame is None or pane.current_path is None:
            self.background_panel.set_status("当前窗格没有可恢复的原图。", error=True)
            return
        pane.background_result = None
        pane.mask_result = None
        pane.integration_result = None
        pane.viewer.set_data(pane.frame.data,pane.frame.metadata.detector_mask)
        pane.title_label.setText(f"图像 {pane.pane_number} · {pane.current_path.name}")
        self.background_panel.set_status("已恢复只读原图显示。")
        self.capability_label.setText("读取：定量候选 | 背景：未预览")
        self.statusBar().showMessage("已恢复原图；原始数组始终未修改")

    @QtCore.Slot(object)
    def _preview_mask(self, recipe: MaskRecipe) -> None:
        pane = self._active_pane
        if pane is None or pane.frame is None or pane.current_path is None:
            self.mask_panel.set_status("请先在当前窗格载入二维图像。", error=True)
            return
        try:
            frame=pane.frame
            job=dict(frame=frame,mask=self.mask_panel.recipe(()),
                manual_descriptors=pane.viewer.manual_mask_descriptors(),
                external_mask_path=self.mask_panel.external_mask_path)
            self._integration_tasks.invalidate()
            self._mask_tasks.submit(lambda:(pane,frame,build_mask(frame,self._mask_preview_workspace.recipe(job))))
        except MaskError as exc:
            self.mask_panel.set_status(f"MASK预览失败：{exc}", error=True)
            return
        self.mask_panel.set_status("正在后台生成MASK…")

    def _mask_preview_ready(self,payload):
        pane,frame,result=payload
        if pane is not self._active_pane or pane.frame is not frame:return
        self._integration_tasks.invalidate()
        pane.integration_result=None
        pane.mask_result = result
        self._follow_mask=True
        pane._follow_display_frame=frame
        pane.viewer.set_display_mask(result.excluded)
        pane.viewer.set_mask_overlay(result.excluded)
        suffix = " [背景预览]" if pane.background_result is not None else ""
        pane.title_label.setText(
            f"图像 {pane.pane_number} · {pane.current_path.name}{suffix} "
            f"[MASK {result.excluded_pixel_count}]"
        )
        sentinel_count = result.layer_counts.get("tvx_negative_sentinel", 0)
        cutoff_count = result.layer_counts.get("count_cutoff", 0)
        self.mask_panel.set_status(
            f"已排除 {result.excluded_pixel_count} / {result.excluded.size} 像素；"
            f"TVX负哨兵 {sentinel_count}，计数上限 {cutoff_count}。"
        )
        self.capability_label.setText("读取：定量候选 | MASK：预览（未积分）")
        self.statusBar().showMessage("红色半透明区域为排除像素；原始强度未修改")

    @QtCore.Slot(str)
    def _add_manual_mask_region(self, kind: str) -> None:
        pane = self._active_pane
        if pane is None or pane.frame is None:
            self.mask_panel.set_status("请先载入图像再添加区域。", error=True)
            return
        if kind in {"polygon_draw", "occlusion_polygon"}:
            draw_kind = "occlusion_polygon" if kind == "occlusion_polygon" else "polygon"
            pane.viewer.begin_manual_polygon_drawing(draw_kind)
            self.mask_panel.set_status(
                "逐点绘制已开始：左键添加顶点，点击首点或右键完成，Esc取消。"
            )
            return
        pane.viewer.add_manual_mask_region(kind)
        self.mask_panel.set_status(
            "区域已添加；矩形可用菱形手柄旋转，调整后点击“预览MASK”。"
        )

    @QtCore.Slot()
    def _suggest_automatic_mask(self) -> None:
        pane = self._active_pane
        geometry = self.current_geometry
        if pane is None or pane.frame is None:
            self.mask_panel.set_status("请先载入图像再自动建议MASK。", error=True)
            return
        if geometry is None:
            self.mask_panel.set_status(
                "自动识别beamstop/支撑杆需要先应用中心点候选；仍可使用手动MASK。",
                error=True,
            )
            return
        if pane.frame.data.shape != (
            geometry.image_height_px,
            geometry.image_width_px,
        ):
            self.mask_panel.set_status("当前几何尺寸与图像不一致。", error=True)
            return
        try:
            suggestion = suggest_beamstop_support_mask(
                pane.frame.data,
                (geometry.beam_center_x_px, geometry.beam_center_y_px),
            )
            for descriptor in suggestion.descriptors:
                pane.viewer.add_manual_mask_region(
                    str(descriptor["kind"]), dict(descriptor)
                )
            self._preview_mask(self.mask_panel.recipe())
        except (MaskError, ValueError) as exc:
            self.mask_panel.set_status(f"自动MASK建议失败：{exc}", error=True)
            return
        audit = suggestion.audit
        support = (
            f"；支撑杆角度 {audit['support_angle_image_deg']:.2f}°、"
            f"宽度 {audit['support_width_px']:.1f} px"
            if audit["support_detected"]
            else "；未可靠识别支撑杆"
        )
        self.mask_panel.set_status(
            f"已生成并预览可编辑候选（置信度 {suggestion.confidence}）："
            f"beamstop半径 {audit['beamstop_radius_px']:.1f} px{support}。"
            "请目视确认，必要时旋转矩形或逐点修正。"
        )

    @QtCore.Slot()
    def _undo_manual_mask_region(self) -> None:
        pane = self._active_pane
        if pane is None:
            return
        pane.viewer.undo_manual_mask_region()

    @QtCore.Slot()
    def _clear_manual_mask_regions(self) -> None:
        pane = self._active_pane
        if pane is None:
            return
        pane.viewer.clear_manual_mask_regions()

    @QtCore.Slot(object, object)
    def _manual_mask_regions_changed(
        self,
        source: ImageViewerWidget,
        descriptors: tuple[dict[str, object], ...],
    ) -> None:
        source_pane = next(
            (pane for pane in self._panes if pane.viewer is source),
            None,
        )
        if source_pane is None:
            return
        source_pane.mask_result = None
        source_pane.integration_result = None
        source.clear_mask_overlay()
        synchronized = self.display_linker.linked
        if synchronized and source_pane.frame is not None:
            source_shape = source_pane.frame.data.shape
            for pane in self._panes:
                if pane is source_pane or pane.frame is None:
                    continue
                if pane.frame.data.shape != source_shape:
                    continue
                pane.viewer.set_manual_mask_descriptors(descriptors, emit=False)
                pane.mask_result = None
                pane.integration_result = None
                pane.viewer.clear_mask_overlay()
        if source_pane is self._active_pane:
            self.mask_panel.set_region_count(
                len(descriptors), synchronized=synchronized
            )
            self.mask_panel.set_status(
                "自定义区域已更新；现有MASK预览已失效，请重新预览。"
            )

    @QtCore.Slot()
    def _external_mask_changed(self) -> None:
        for pane in self._panes:
            pane.mask_result = None
            pane.integration_result = None
            pane.viewer.clear_mask_overlay()
        path = self.mask_panel.external_mask_path
        if path is None:
            self.mask_panel.set_status("已移除外部MASK；现有预览已失效。")
        else:
            self.mask_panel.set_status(
                f"已选择 {path.name}；预览时将严格检查尺寸和非零排除约定。"
            )
        if self._follow_mask:self._processing_settings_changed()

    def _pane_geometry(self, pane: EmbeddedViewerPane) -> DetectorGeometry | None:
        """Keep calibrated panes independent while sharing physical ROI settings."""
        return (getattr(pane, 'geometry_override', None)
                or (embedded_geometry(pane.frame) if pane.frame is not None else None)
                or self.current_geometry)

    def _integration_cache_key(
        self,
        shape: tuple[int, int],
        unit: str,
        geometry: DetectorGeometry | None = None,
    ) -> tuple[object, ...]:
        geometry = geometry or self.current_geometry
        assert geometry is not None
        return (
            shape,
            unit,
            geometry.wavelength_m,
            geometry.detector_distance_m,
            geometry.pixel_size_x_m,
            geometry.pixel_size_y_m,
            geometry.beam_center_x_px,
            geometry.beam_center_y_px,
            geometry.rot1_rad,
            geometry.rot2_rad,
            geometry.rot3_rad,
        )

    def _radial_map_for_region(
        self,
        shape: tuple[int, int],
        descriptor: dict[str, object],
        geometry: DetectorGeometry | None = None,
    ) -> np.ndarray | None:
        if descriptor.get("mode") != "azimuthal":
            return None
        geometry = geometry or self.current_geometry
        if geometry is None:
            return None
        if geometry.is_untilted:
            return None
        unit = str(descriptor.get("radial_unit", "q_nm^-1"))
        key = self._integration_cache_key(shape, unit, geometry)
        cached = self._integration_radial_cache.get(key)
        if cached is None:
            if self._overlay_pending_key!=key:
                self._overlay_pending_key=key
                self._overlay_tasks.submit(lambda:(key,detector_radial_array(geometry,shape,unit)))
        return cached

    def _overlay_map_ready(self,payload):
        key,values=payload
        self._overlay_pending_key=None
        if not any(self._pane_geometry(pane) is not None
                   and pane.frame is not None and pane.frame.data.shape == key[0]
                   and key == self._integration_cache_key(key[0], key[1], self._pane_geometry(pane))
                   for pane in self._panes):
            return
        self._integration_radial_cache[key]=values
        while len(self._integration_radial_cache)>9:
            self._integration_radial_cache.pop(next(iter(self._integration_radial_cache)))
        for pane in self._panes:
            if pane.frame is not None:
                self._apply_integration_overlay_to_pane(pane)

    def _apply_integration_overlay_to_pane(self, pane: EmbeddedViewerPane) -> None:
        geometry = self._pane_geometry(pane)
        if geometry is None or pane.frame is None:
            pane.viewer.clear_integration_overlay()
            return
        descriptor = pane.integration_region
        if descriptor is None:
            descriptor = self.integration_panel.region_descriptor()
            pane.integration_region = dict(descriptor)
        if pane.frame.data.shape != (
            geometry.image_height_px,
            geometry.image_width_px,
        ):
            pane.viewer.clear_integration_overlay()
            return
        try:
            radial_map = self._radial_map_for_region(
                pane.frame.data.shape, descriptor, geometry
            )
            if descriptor.get("mode")=="azimuthal" and radial_map is None and not geometry.is_untilted:
                pane.viewer.clear_integration_overlay()
                return
            pane.viewer.set_integration_overlay(
                descriptor,
                geometry,
                radial_map=radial_map,
            )
            pane.viewer.integration_overlay.set_visible(
                self._integration_overlay_should_be_visible()
            )
        except (IntegrationError, ValueError) as exc:
            pane.viewer.clear_integration_overlay()
            if pane is self._active_pane:
                self.integration_panel.set_status(
                    f"积分区域无法显示：{exc}", error=True
                )

    def _integration_overlay_should_be_visible(self) -> bool:
        return (
            self.processing_tabs.currentWidget() is self.integration_panel
            and self.integration_panel.show_region_check.isChecked()
        )

    @QtCore.Slot(int)
    def _processing_tab_changed(self, _index: int) -> None:
        self._update_auxiliary_overlay_visibility()

    @QtCore.Slot()
    @QtCore.Slot(bool)
    def _update_auxiliary_overlay_visibility(self, _checked: bool = False) -> None:
        current = self.processing_tabs.currentWidget()
        show_geometry = current is self.geometry_panel
        show_masks = (
            current is self.mask_panel and self.mask_panel.show_regions_check.isChecked()
        )
        for pane in self._panes:
            pane.viewer.set_geometry_overlay_visible(show_geometry)
            pane.viewer.set_manual_mask_regions_visible(show_masks)
        self._update_integration_overlay_visibility()

    @QtCore.Slot()
    @QtCore.Slot(bool)
    def _update_integration_overlay_visibility(self, _checked: bool = False) -> None:
        visible = self._integration_overlay_should_be_visible()
        for pane in self._panes:
            if (
                visible
                and pane.viewer.integration_overlay.descriptor is None
                and self.current_geometry is not None
                and pane.frame is not None
            ):
                self._apply_integration_overlay_to_pane(pane)
            else:
                pane.viewer.integration_overlay.set_visible(visible)

    @QtCore.Slot(object)
    def _integration_panel_region_changed(
        self, descriptor: dict[str, object]
    ) -> None:
        if self._updating_integration_regions or self._active_pane is None:
            return
        self._updating_integration_regions = True
        try:
            active = self._active_pane
            previous=active.integration_region or {}
            if (previous.get("mode")==descriptor.get("mode")=="azimuthal" and
                previous.get("radial_unit")!=descriptor.get("radial_unit") and self.current_geometry is not None):
                from .radial_units import convert_radial_values
                bounds=convert_radial_values([previous["radial_lower"],previous["radial_upper"]],
                    previous["radial_unit"],descriptor["radial_unit"],self.current_geometry.wavelength_m)
                descriptor={**descriptor,"radial_lower":float(bounds[0]),"radial_upper":float(bounds[1])}
                self.integration_panel.set_region_descriptor(descriptor)
            active.integration_region = dict(descriptor)
            active.integration_result = None
            self._apply_integration_overlay_to_pane(active)
            if self.display_linker.linked and active.frame is not None:
                for pane in self._panes:
                    if pane is active or pane.frame is None:
                        continue
                    pane.integration_region = dict(descriptor)
                    pane.integration_result = None
                    self._apply_integration_overlay_to_pane(pane)
        finally:
            self._updating_integration_regions = False
        self._schedule_integration_preview()

    @QtCore.Slot(object, object)
    def _integration_region_changed(
        self,
        source: ImageViewerWidget,
        descriptor: dict[str, object],
    ) -> None:
        if self._updating_integration_regions:
            return
        source_pane = next(
            (pane for pane in self._panes if pane.viewer is source),
            None,
        )
        if source_pane is None:
            return
        self._updating_integration_regions = True
        try:
            source_pane.integration_region = dict(descriptor)
            source_pane.integration_result = None
            if source_pane is self._active_pane or self.display_linker.linked:
                self.integration_panel.set_region_descriptor(descriptor)
            if self.display_linker.linked and source_pane.frame is not None:
                for pane in self._panes:
                    if pane is source_pane or pane.frame is None:
                        continue
                    pane.integration_region = dict(descriptor)
                    pane.integration_result = None
                    self._apply_integration_overlay_to_pane(pane)
        finally:
            self._updating_integration_regions = False
        if source_pane is self._active_pane or self.display_linker.linked:
            self._schedule_integration_preview()

    def _schedule_integration_preview(self) -> None:
        self._integration_tasks.invalidate()
        if self.current_geometry is None or self._active_pane is None:
            return
        if self._active_pane.frame is None:
            return
        if any(pane.viewer.integration_overlay.dragging for pane in self._panes):
            self._integration_preview_timer.stop()
            self.integration_panel.set_status("正在调整积分区域，松开鼠标后更新曲线。")
            return
        self.integration_panel.set_status("积分区域已更新，正在刷新候选曲线…")
        self._integration_preview_timer.start(180)

    def _integration_drag_state_changed(self,active):
        self._integration_tasks.invalidate()
        self._integration_preview_timer.stop()
        if active:
            self.integration_panel.set_status("正在调整积分区域，松开鼠标后更新曲线。")
        elif self.current_geometry is not None and not any(pane.viewer.integration_overlay.dragging for pane in self._panes):
            self._integration_preview_timer.start(0)

    @QtCore.Slot()
    def _preview_integration(self) -> None:
        self._follow_integration=True
        self._follow_background=self.background_panel.mode is not BackgroundMode.NO_BACKGROUND
        active = self._active_pane
        if active is None or active.frame is None:
            self.integration_panel.set_status("请先载入二维图像。", error=True)
            return
        if self.current_geometry is None:
            self.integration_panel.set_status(
                "尚未应用几何候选；请先在“几何/标定”页确认中心、SDD和波长。",
                error=True,
            )
            return
        if self.integration_panel.curve_scope=='checked':
            self._preview_checked_integration()
            return
        # Curve comparison scope is deliberately independent from display/ROI
        # synchronization.  Users may keep four different per-pane regions and
        # still compare all four results in one plot.
        targets = (
            [pane for pane in self._panes if pane.frame is not None]
            if self.integration_panel.curve_scope == "all_open"
            else [active]
        )
        active_descriptor = active.integration_region or self.integration_panel.region_descriptor()
        active_mode = str(active_descriptor.get("mode", self.integration_panel.mode))
        jobs=[]
        failures: list[str] = []
        for pane in targets:
            assert pane.frame is not None
            geometry = self._pane_geometry(pane)
            descriptor = pane.integration_region or active_descriptor
            pane_mode = str(descriptor.get("mode", active_mode))
            if pane_mode != active_mode:
                name = pane.current_path.name if pane.current_path is not None else "未命名"
                failures.append(
                    f"{name}: 积分类型与当前窗格不同，未混合叠加"
                )
                continue
            if geometry is None or pane.frame.data.shape != (
                geometry.image_height_px,
                geometry.image_width_px,
            ):
                failures.append(f"{pane.current_path.name}: 图像尺寸与几何不一致")
                continue
            input_unit = (
                pane.background_result.output_unit
                if pane.background_result is not None
                else pane.frame.metadata.intensity_unit or pane.frame.metadata.embedded_processing.get('intensity_unit','detector_count')
            )
            try:
                background_recipe=self.background_panel.background_recipe()
                stored_unit=pane.frame.metadata.intensity_unit or pane.frame.metadata.embedded_processing.get('intensity_unit')
                if stored_unit is not None:
                    background_recipe=replace(background_recipe,input_unit=stored_unit)
                if pane.frame.metadata.embedded_processing:
                    mode=(BackgroundMode.UPSTREAM_SUBTRACTED if pane.frame.metadata.embedded_processing.get('background_applied')
                          else BackgroundMode.NO_BACKGROUND)
                    background_recipe=replace(background_recipe,mode=mode)
                if background_recipe.mode is BackgroundMode.AUTO_AIR and not self.background_panel.source_ready:
                    raise BackgroundError('请先明确选择背景数据集和帧。')
                # Snapshot vector descriptors only. Full-detector rasterization
                # and external MASK IO must not run in the GUI event loop.
                manual_descriptors = pane.viewer.manual_mask_descriptors()
                mask_recipe = self.mask_panel.recipe(())
                if pane_mode == "radial":
                    integration_recipe = self.integration_panel.radial_recipe(
                        input_unit, descriptor
                    )
                else:
                    integration_recipe = self.integration_panel.azimuthal_recipe(
                        input_unit, descriptor
                    )
                from saxs_waxs_core.readers.detector_io import frame_label
                name = frame_label(pane.frame)
                jobs.append(dict(pane=pane,frame=pane.frame,descriptor=dict(descriptor),
                    geometry=geometry,
                    mask=mask_recipe,recipe=integration_recipe,
                    manual_descriptors=manual_descriptors,
                    external_mask_path=self.mask_panel.external_mask_path,
                    background_recipe=background_recipe,
                    background_path=self.background_panel.background_path,
                    background_selection=dict(self.background_panel.source_selection),
                    name=name,source=None if pane.current_path is None else str(pane.current_path.absolute())))
            except (IntegrationError, MaskError, BackgroundError, ProcessingError) as exc:
                name = pane.current_path.name if pane.current_path is not None else "未命名"
                failures.append(f"{name}: {exc}")
                continue
        from .latest_task import integrate_previews
        self.integration_panel.set_status("正在后台积分…")
        self._integration_feedback.start(len(jobs))
        self._integration_tasks.submit(lambda progress:integrate_previews(jobs,failures,
            self._integration_workspace,self._preview_background_workspace,self._preview_mask_workspace,
            progress),with_progress=True)

    def _integration_preview_failed(self,error):
        self.integration_panel.set_status(error,error=True)
        if getattr(self,"_fit_preparation_after_preview",False):
            self._fit_preparation_after_preview=False
            self.peak_fit_panel.set_loading(False,error)

    def _checked_preview_selection_changed(self):
        if self.integration_panel.curve_scope=='checked':
            self._schedule_integration_preview()

    def _preview_checked_integration(self):
        from .checked_preview import integrate_checked
        pane=self._active_pane
        excluded=set()
        for path in (self.background_panel.background_path,self.mask_panel.external_mask_path):
            if path is not None:excluded.add(str(Path(path).resolve()).casefold())
        geometry=self.current_geometry
        if geometry.provenance.source_path and geometry.provenance.method!='instrument_header':
            excluded.add(str(Path(geometry.provenance.source_path).resolve()).casefold())
        paths=tuple(path for path in self.browser.selected_paths if str(path.resolve()).casefold() not in excluded)
        if not paths:
            self.integration_panel.show_curves([],[])
            self.peak_fit_panel.set_curves([],[])
            self.integration_panel.set_status('请在左侧勾选样品／帧；已确认背景和外部MASK不作为样品。',error=True)
            return
        try:
            background=self.background_panel.background_recipe()
            if background.mode is BackgroundMode.AUTO_AIR and not self.background_panel.source_ready:
                raise BackgroundError('请先明确选择背景数据集和帧。')
            descriptor=pane.integration_region or self.integration_panel.region_descriptor()
            recipe=(self.integration_panel.radial_recipe(background.input_unit,descriptor)
                if descriptor.get('mode','radial')=='radial' else self.integration_panel.azimuthal_recipe(background.input_unit,descriptor))
            snapshot=dict(geometry=geometry,recipe=recipe,mask=self.mask_panel.recipe(()),
                manual_descriptors=pane.viewer.manual_mask_descriptors(),external_mask_path=self.mask_panel.external_mask_path,
                background_recipe=background,background_path=self.background_panel.background_path,
                background_selection=dict(self.background_panel.source_selection))
        except (IntegrationError,MaskError,BackgroundError,ProcessingError) as exc:
            self._integration_preview_failed(str(exc));return
        datasets=dict(self._detector_dataset_choices)
        frames={key:tuple(value) if value is not None else None for key,value in self._detector_frame_choices.items()}
        version=self._integration_tasks.version+1
        self._integration_feedback.start(len(paths),long_running=True)
        self.integration_panel.set_status('正在后台准备左侧勾选样品／帧，不打开新的二维窗格…')
        self._integration_tasks.submit(lambda progress:integrate_checked(snapshot,paths,datasets,frames,
            self._integration_workspace,self._preview_background_workspace,self._preview_mask_workspace,
            progress,lambda:self._integration_tasks.version!=version),with_progress=True)

    def _integration_preview_ready(self, payload):
        if isinstance(payload,dict) and payload.get('checked_preview'):
            curves,contexts=payload['curves'],payload['contexts']
            self.integration_panel.show_curves(curves,contexts)
            self.peak_fit_panel.set_curves(curves,contexts)
            failures=payload['failures']
            detail=(f'已生成 {len(curves)} 条勾选样品／帧积分曲线，可在“显示曲线”中选择。'
                if curves else '未生成勾选样品／帧积分曲线。')
            if failures:detail+=f' {len(failures)} 个失败：'+'；'.join(failures[:2])
            self.integration_panel.set_status(detail,error=bool(failures) or not curves)
            if getattr(self,'_fit_preparation_after_preview',False):
                self._fit_preparation_after_preview=False
                self.peak_fit_panel.set_loading(False,detail)
                if curves:self._open_peak_fit_workspace(curves,contexts)
            return
        completed,failures=payload
        curves=[]
        curve_parameter_contexts=[]
        for job,execution in completed:
            pane=job["pane"]
            if pane not in self._panes or pane.frame is not job["frame"] or pane.integration_region!=job["descriptor"]:
                continue
            pane.mask_result=execution.mask
            pane.integration_result=execution.integration
            curves.append((job["name"],execution.integration))
            curve_parameter_contexts.append({"source":job["source"],"processing":dict(execution.audit)})
        if not curves:
            detail = "；".join(failures[:3])
            self._integration_preview_failed(f"积分预览失败：{detail}")
            return
        self.integration_panel.show_curves(curves, curve_parameter_contexts)
        self.peak_fit_panel.set_curves(curves, curve_parameter_contexts)
        for window in self._peak_fit_windows:
            if getattr(window,'_follow_live_preview',False):
                old=window.fit_panel._curves
                if old and len(curves)==1:
                    compatible=old[0][1].kind==curves[0][1].kind and old[0][1].x_unit==curves[0][1].x_unit
                    window.fit_panel.set_curves(curves,curve_parameter_contexts,preserve_settings=compatible)
        if self._follow_background and any(job['pane'].background_result is None for job,_ in completed):
            self._schedule_navigation_refresh()
        if failures:
            self.integration_panel.set_status(
                f"完成 {len(curves)} 条曲线，{len(failures)} 个窗格失败："
                + "；".join(failures[:2]),
                error=True,
            )
        self.capability_label.setText("读取：定量候选 | 积分：预览（候选几何）")
        self.statusBar().showMessage(
            "积分预览使用当前背景、MASK和几何候选；尚未完成跨软件科学验收"
        )

        if getattr(self,"_fit_preparation_after_preview",False):
            self._fit_preparation_after_preview=False
            self._start_checked_fit_curve_preparation()

    @QtCore.Slot()
    def _start_checked_fit_curve_preparation(self) -> None:
        if self._fit_curve_thread is not None and self._fit_curve_thread.isRunning():
            self.peak_fit_panel.set_loading(True, "勾选样品的积分曲线正在生成。")
            return
        # pyFAI's compiled histogram path is initialized by the ordinary current-
        # pane preview before it is reused from the worker thread.  This also
        # validates the exact current geometry/MASK/integration settings once,
        # instead of discovering a configuration error after a long selection.
        active = self._active_pane
        if active is None or active.frame is None:
            self.peak_fit_panel.set_loading(False, "请先载入一张样品图像。")
            return
        if active.integration_result is None:
            self._fit_preparation_after_preview=True
            self._preview_integration()
            self.peak_fit_panel.set_loading(True,"正在后台准备当前积分曲线…")
            return
        try:
            plan = self._batch_plan_for_current_selection()
        except (OSError, ProcessingError, ValueError) as exc:
            self.peak_fit_panel.set_loading(False, f"无法生成勾选样品曲线：{exc}")
            return
        thread = QtCore.QThread(self)
        worker = FitCurvePreparationWorker(plan)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._checked_fit_curve_progress)
        worker.finished.connect(self._checked_fit_curve_finished)
        worker.failed.connect(self._checked_fit_curve_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._checked_fit_curve_thread_finished)
        self._fit_curve_thread = thread
        self._fit_curve_worker = worker
        count = len(plan.sample_sources)
        self.peak_fit_panel.set_loading(
            True,
            f"正在按当前几何、背景、MASK和积分参数生成 0/{count} 条曲线…",
        )
        self.statusBar().showMessage(f"正在后台生成 {count} 个勾选样品的积分曲线…")
        thread.start()

    @QtCore.Slot()
    def _cancel_checked_fit_curve_preparation(self) -> None:
        if self._fit_curve_worker is None:
            return
        self._fit_curve_worker.request_stop()
        self.peak_fit_panel.set_loading(True, "将在当前样品处理完成后停止加载…")

    @QtCore.Slot(object)
    def _checked_fit_curve_progress(self, summary: object) -> None:
        if not isinstance(summary, dict):
            return
        processed = int(summary.get("processed", 0))
        total = int(summary.get("total", 0))
        failed = int(summary.get("failed", 0))
        phase = str(summary.get("phase", "done"))
        phase_text = {"read": "读取", "integrate": "积分", "done": "完成"}.get(
            phase, phase
        )
        current = str(summary.get("current", ""))
        self.peak_fit_panel.set_loading(
            True,
            f"正在生成勾选样品曲线：{processed}/{total}；"
            f"{phase_text} {current}；失败 {failed}。",
        )

    @QtCore.Slot(object)
    def _checked_fit_curve_finished(self, result: object) -> None:
        if not isinstance(result, dict):
            self._checked_fit_curve_failed("后台返回了未知结果")
            return
        curves = list(result.get("curves", []))
        contexts = list(result.get("contexts", []))
        failures = list(result.get("failures", []))
        cancelled = bool(result.get("cancelled", False))
        if curves:
            self.integration_panel.show_curves(curves, contexts)
            self.peak_fit_panel.set_curves(curves, contexts)
        detail = f"已生成 {len(curves)} 条勾选样品积分曲线"
        if failures:
            detail += f"；{len(failures)} 个失败"
        if cancelled:
            detail += "；已停止"
        self.peak_fit_panel.set_loading(False, detail + "。")
        self.statusBar().showMessage(detail)
        if curves and not cancelled:
            self._open_peak_fit_workspace(curves, contexts)

    @QtCore.Slot(str)
    def _checked_fit_curve_failed(self, message: str) -> None:
        self.peak_fit_panel.set_loading(False, f"勾选样品曲线生成失败：{message}")
        self.statusBar().showMessage(f"勾选样品曲线生成失败：{message}")

    @QtCore.Slot()
    def _checked_fit_curve_thread_finished(self) -> None:
        self._fit_curve_thread = None
        self._fit_curve_worker = None

    @QtCore.Slot(object, object)
    def _open_peak_fit_workspace(
        self,
        curves: object,
        parameter_contexts: object,
    ) -> None:
        curve_list = list(curves) if isinstance(curves, (list, tuple)) else []
        context_list = (
            list(parameter_contexts)
            if isinstance(parameter_contexts, (list, tuple))
            else []
        )
        if not curve_list or len(curve_list) != len(context_list):
            self.statusBar().showMessage("拟合窗口未打开：没有有效的积分曲线。")
            return
        session_number = self._next_peak_fit_session
        self._next_peak_fit_session += 1
        window = PeakFitWorkspaceWindow(
            curve_list,
            context_list,
            session_number=session_number,
            parent=self,
        )
        window._follow_live_preview=(len(curve_list)==1 and bool(context_list[0].get('processing'))
            and context_list[0].get('selection')!='left_checked_sample')
        if window._follow_live_preview:
            window.statusBar().showMessage('当前样品预览：随主界面切换更新曲线并沿用拟合设置。')
        self._peak_fit_windows.append(window)
        window.destroyed.connect(
            lambda _object=None, target=window: self._forget_peak_fit_window(target)
        )
        offset = 28 * ((session_number - 1) % 6)
        window.move(self.x() + 70 + offset, self.y() + 70 + offset)
        window.show()
        window.raise_()
        window.activateWindow()
        self.statusBar().showMessage(
            f"已打开拟合工作区 {session_number}（{len(curve_list)} 条曲线）；"
            "主窗口仍可继续操作。"
        )

    def _choose_import_curves(self, files=None) -> None:
        if not files:
            files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "选择一维曲线", "", "曲线文件 (*.csv *.tsv *.txt *.dat)")
        if not files:
            return
        dialog = CurveImportDialog(files, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            kind, unit, encoding, header_rows, pairs, intensity_unit, wavelength, sigma_column, zero_is_missing = dialog.settings()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "设置无效", str(exc))
            return
        curves, contexts, errors = [], [], []
        for file in files:
            try:
                source_text = read_curve_text(file, encoding)
                prepared = prepare_curve_text(source_text)
                del source_text
            except (ValueError, OSError) as exc:
                errors.append(f"{Path(file).name}: {exc}")
                continue
            for pair in pairs:
                x_col, y_col = pair[:2]
                current_sigma = pair[2] if len(pair) == 3 else sigma_column
                try:
                    curve = import_curve(file, x_column=x_col-1, y_column=y_col-1,
                                         kind=kind, x_unit=unit, intensity_unit=intensity_unit,
                                         encoding=encoding, header_rows=header_rows, wavelength_nm=wavelength,
                                         prepared=prepared, sigma_column=None if current_sigma is None else current_sigma-1,
                                         zero_is_missing=zero_is_missing)
                except (ValueError, OSError) as exc:
                    errors.append(f"{Path(file).name} [{x_col},{y_col}]: {exc}")
                    continue
                fallback = Path(file).name if len(pairs) == 1 else f"{Path(file).name} [列 {x_col},{y_col}]"
                name = suggested_curve_name(curve, fallback)
                if any(existing == name for existing, _ in curves):
                    name = f"{name} ({Path(file).name} [{x_col},{y_col}])"
                base, counter = name, 2
                while any(existing == name for existing, _ in curves):
                    name, counter = f"{base} #{counter}", counter + 1
                curves.append((name, curve))
                contexts.append({"source": str(Path(file).resolve()), "integration": dict(curve.audit)})
        if errors:
            QtWidgets.QMessageBox.warning(self, "部分文件未导入" if curves else "导入失败", "\n".join(errors))
        if curves:
            self._open_peak_fit_workspace(curves, contexts)

    def _forget_peak_fit_window(self, target: PeakFitWorkspaceWindow) -> None:
        self._peak_fit_windows = [
            window for window in self._peak_fit_windows if window is not target
        ]

    def _processing_recipe_for_active_pane(self) -> ProcessingRecipe:
        pane = self._active_pane
        if pane is None or pane.frame is None or pane.current_path is None:
            raise ProcessingError("请先在当前窗格载入样品图像")
        if self.current_geometry is None:
            raise ProcessingError("请先应用几何候选，再保存处理配方")
        if pane.frame.data.shape != (
            self.current_geometry.image_height_px,
            self.current_geometry.image_width_px,
        ):
            raise ProcessingError("当前图像尺寸与已应用几何不一致")
        mode = self.background_panel.mode
        background_path = self.background_panel.background_path
        source = None
        if mode is BackgroundMode.AUTO_AIR:
            if not self.background_panel.source_ready:
                raise ProcessingError('请先明确选择背景数据集和帧。')
            if background_path is None:
                raise ProcessingError("自动扣背景模式尚未明确映射背景文件")
            if background_path.absolute() == pane.current_path.absolute():
                raise ProcessingError("背景不能与当前样品是同一文件")
            source = FileIdentity.from_path(background_path)
        background = self.background_panel.background_recipe()
        stored_unit=pane.frame.metadata.intensity_unit or pane.frame.metadata.embedded_processing.get('intensity_unit')
        if stored_unit is not None:background=replace(background,input_unit=stored_unit)
        input_unit = background.input_unit+'/s' if mode is BackgroundMode.AUTO_AIR else background.input_unit
        descriptor = pane.integration_region or self.integration_panel.region_descriptor()
        if str(descriptor.get("mode", "radial")) == "radial":
            integration = self.integration_panel.radial_recipe(input_unit, descriptor)
        else:
            integration = self.integration_panel.azimuthal_recipe(input_unit, descriptor)
        mask = self.mask_panel.recipe()
        external_mask_source = None
        external_mask_path = self.mask_panel.external_mask_path
        if external_mask_path is not None:
            self.mask_panel.external_mask_layer(pane.frame.data.shape)
            external_mask_source = FileIdentity.from_path(external_mask_path)
        return ProcessingRecipe(
            geometry=self.current_geometry,
            background=background,
            background_source=source,
            external_mask_source=external_mask_source,
            mask=mask,
            manual_mask_descriptors=pane.viewer.manual_mask_descriptors(),
            integration=integration,
            policy=ProcessingPolicy(
                parameter_mode=self.recipe_panel.parameter_mode,
            ),
            metadata={
                "created_by": "XRelay Phase 3",
                "prefer_embedded_geometry": bool((pane.frame.metadata.embedded_processing or pane.frame.metadata.geometry_document)
                    and getattr(pane,'geometry_override',None) is None),
                "source_sample_hint": str(pane.current_path),
                "geometry_status": self.current_geometry.metadata.get(
                    "status", "not_recorded"
                ),
            },
        )

    def _protected_recipe_roots(self) -> tuple[Path, ...]:
        roots = [Path(__file__).resolve().parents[2] / "data"]
        browser_root = self.browser.path_edit.text().strip()
        if browser_root and Path(browser_root).is_dir():
            candidate = Path(browser_root).expanduser().absolute()
            if candidate not in roots:
                roots.append(candidate)
        return tuple(roots)

    def _recipe_storage_directory(self) -> Path:
        """Return the writable recipe folder, creating it before file dialogs open."""

        directory = artifacts_directory() / "recipes"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _recipe_save_error_text(prefix: str, error: Exception) -> str:
        detail = str(error)
        if "refusing to write inside protected root" in detail:
            return (
                f"{prefix}：为保护原始数据，不能保存到 data 或当前原始数据文件夹；"
                "请选择默认的 artifacts/recipes，或其他非数据目录。"
            )
        return f"{prefix}：{detail}"

    def save_current_processing_recipe_to_path(
        self,
        path: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        recipe = self._processing_recipe_for_active_pane()
        return save_processing_recipe(
            recipe,
            path,
            protected_roots=self._protected_recipe_roots(),
            overwrite=overwrite,
        )

    def save_current_batch_plan_to_path(
        self,
        path: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        plan = self._batch_plan_for_current_selection()
        saved = save_batch_processing_plan(
            plan,
            path,
            protected_roots=self._protected_recipe_roots(),
            overwrite=overwrite,
        )
        self._batch_plan_draft = plan
        self._update_recipe_revision_summary()
        return saved

    def _batch_plan_for_current_selection(self) -> BatchProcessingPlan:
        if self._batch_plan_draft is not None:
            draft_paths = {
                str(Path(item.path).expanduser().absolute().resolve(strict=False)).casefold()
                for item in self._batch_plan_draft.sample_sources
            }
            selected_paths = {
                str(item.expanduser().absolute().resolve(strict=False)).casefold()
                for item in self.browser.selected_paths
            }
            if selected_paths and selected_paths == draft_paths:
                for identity in self._batch_plan_draft.sample_sources:
                    actual = FileIdentity.from_path(identity.path)
                    if actual != identity:
                        raise ProcessingError(
                            f"批次方案中的文件身份已改变：{Path(identity.path).name}"
                        )
                return self._batch_plan_draft
        recipe = self._processing_recipe_for_active_pane()
        selected = self.browser.selected_paths
        if not selected:
            raise ProcessingError("请在左侧勾选至少一个批次样品")
        configured_exclusions: dict[str, str] = {}
        background_path = self.background_panel.background_path
        if background_path is not None:
            key = str(
                background_path.expanduser().absolute().resolve(strict=False)
            ).casefold()
            configured_exclusions[key] = "已确认背景"
        external_mask_path = self.mask_panel.external_mask_path
        if external_mask_path is not None:
            key = str(
                external_mask_path.expanduser().absolute().resolve(strict=False)
            ).casefold()
            configured_exclusions[key] = "外部MASK"
        if self.current_geometry is not None:
            source_path = self.current_geometry.provenance.source_path
            if source_path and self.current_geometry.provenance.method!='instrument_header':
                key = str(
                    Path(source_path).expanduser().absolute().resolve(strict=False)
                ).casefold()
                configured_exclusions[key] = "几何标定源"

        selected_keys = {
            str(item.expanduser().absolute().resolve(strict=False)).casefold()
            for item in selected
        }
        visible_tiff_keys = {
            str(item.path.expanduser().absolute().resolve(strict=False)).casefold()
            for item in self.browser.files
            if supported_detector_path(item.path)
        }
        whole_visible_folder_selected = bool(visible_tiff_keys) and (
            selected_keys == visible_tiff_keys
        )

        executable: list[Path] = []
        excluded_inputs: list[tuple[Path, str]] = []
        for item in selected:
            absolute = item.expanduser().absolute()
            key = str(absolute.resolve(strict=False)).casefold()
            reason = configured_exclusions.get(key)
            if reason is None and whole_visible_folder_selected:
                reason = self._automatic_non_sample_role(absolute)
            if reason is not None:
                excluded_inputs.append((absolute, reason))
            else:
                executable.append(absolute)
        if not executable:
            detail = "、".join(
                f"{path.name}（{reason}）" for path, reason in excluded_inputs[:3]
            )
            raise ProcessingError(f"所选文件均为已配置输入，不能作为样品：{detail}")
        unsupported = [
            item.name
            for item in executable
            if not supported_detector_path(item)
        ]
        if unsupported:
            raise ProcessingError(
                "当前批次尚无该格式适配器：" + ", ".join(unsupported[:3])
            )
        try:
            source_details={}
            samples,selections=expand_identities(executable,self._detector_dataset_choices,source_details,
                frame_choices=self._detector_frame_choices)
        except TiffReaderError as exc:
            raise ProcessingError(str(exc)) from exc
        return BatchProcessingPlan(
            recipe=recipe,
            sample_sources=samples,
            metadata={
                "selection_rule": self._batch_scope_rule,
                "source_selections":selections,
                "source_dependencies":source_details['dependencies'],
                "excluded_source_chunks":source_details['excluded'],
                "source_root": self.browser.path_edit.text().strip(),
                "excluded_configured_inputs": [
                    {"path": str(path), "reason": reason}
                    for path, reason in excluded_inputs
                ],
            },
        )

    def _raw_image_export_plan_for_current_selection(self) -> RawImageExportPlan:
        """Bind raw 2D export to the checkbox set without role inference."""

        selected = tuple(self.browser.selected_paths)
        if not selected:
            raise ProcessingError("请在左侧勾选至少一个文件")
        try:
            source_details={}
            identities,selections=expand_identities(selected,self._detector_dataset_choices,source_details,
                frame_choices=self._detector_frame_choices)
        except TiffReaderError as exc:
            raise ProcessingError(str(exc)) from exc
        return RawImageExportPlan(
            identities,
            metadata={
                "selection_rule": "exact_left_panel_checkboxes",
                "source_selections":selections,
                "source_dependencies":source_details['dependencies'],
                "excluded_source_chunks":source_details['excluded'],
                "source_root": self.browser.path_edit.text().strip(),
                "candidate_geometry_not_applied": (
                    None
                    if self.current_geometry is None
                    else geometry_to_document(self.current_geometry)
                ),
            },
        )

    def _update_recipe_revision_summary(self) -> None:
        plan = self._batch_plan_draft
        if plan is None:
            self.recipe_panel.set_revision_summary(
                "尚未建立基础批次方案；请先保存或载入批次方案。"
            )
            return
        counts = {
            revision_id: plan.recipe_assignment.count(revision_id)
            for revision_id in set(plan.recipe_assignment)
        }
        lines = [f"base：{counts.get('base', 0)} 帧"]
        for revision in plan.recipe_revisions:
            lines.append(
                f"{revision.revision_id}：{counts.get(revision.revision_id, 0)} 帧｜"
                f"{revision.reason}"
            )
        self.recipe_panel.set_revision_summary("\n".join(lines))

    def _recipe_revision_indices(self, scope: str) -> tuple[int, ...]:
        plan = self._batch_plan_draft
        if plan is None:
            raise ProcessingError("请先保存或载入批次方案，建立base版本")
        path_to_indices={}
        for index,item in enumerate(plan.sample_sources):
            key=str(Path(item.path).expanduser().absolute().resolve(strict=False)).casefold()
            path_to_indices.setdefault(key,[]).append(index)
        if scope == "selected":
            indices = []
            outside = []
            for path in self.browser.highlighted_paths or self.browser.selected_paths:
                key = str(path.expanduser().absolute().resolve(strict=False)).casefold()
                if key in path_to_indices:
                    indices.extend(path_to_indices[key])
                else:
                    outside.append(path.name)
            if outside:
                raise ProcessingError(
                    "左侧选择包含base方案之外的文件：" + "、".join(outside[:3])
                )
            if not indices:
                raise ProcessingError("左侧没有选中base方案内的帧")
            return tuple(sorted(set(indices)))
        if scope == "from_current":
            current_path = (
                self._active_pane.current_path
                if self._active_pane is not None
                else None
            )
            if current_path is None:
                raise ProcessingError("当前窗格没有图像")
            key = str(
                current_path.expanduser().absolute().resolve(strict=False)
            ).casefold()
            if key not in path_to_indices:
                raise ProcessingError("当前图不属于base批次方案")
            matches=path_to_indices[key]
            frame=self._active_pane.frame
            selectors=plan.metadata.get('source_selections',[{}]*len(plan.sample_sources))
            start=next((i for i in matches if frame is not None and
                selectors[i].get('frame_index',0)==frame.metadata.frame_index and
                selectors[i].get('dataset')==frame.metadata.dataset_path),matches[0])
            return tuple(range(start, len(plan.sample_sources)))
        raise ProcessingError(f"未知配方版本范围：{scope}")

    def create_batch_recipe_revision(
        self, scope: str, *, reason: str
    ) -> BatchProcessingPlan:
        if not reason.strip():
            raise ProcessingError("创建新配方版本必须填写修改原因")
        plan = self._batch_plan_draft
        if plan is None:
            raise ProcessingError("请先保存或载入批次方案，建立base版本")
        indices = self._recipe_revision_indices(scope)
        revised = add_batch_recipe_revision(
            plan,
            self._processing_recipe_for_active_pane(),
            indices,
            reason=reason.strip(),
        )
        self._batch_plan_draft = revised
        self._update_recipe_revision_summary()
        self.recipe_panel.set_status(
            f"已创建 {revised.recipe_revisions[-1].revision_id}，应用于 {len(indices)} 帧；"
            "旧版本未覆盖。"
        )
        return revised

    @QtCore.Slot(str)
    def _choose_create_batch_recipe_revision(self, scope: str) -> None:
        try:
            indices = self._recipe_revision_indices(scope)
            assert self._batch_plan_draft is not None
            names = [
                Path(self._batch_plan_draft.sample_sources[index].path).name
                for index in indices
            ]
        except (OSError, ProcessingError, ValueError) as exc:
            self.recipe_panel.set_status(f"不能创建配方版本：{exc}", error=True)
            return
        reason, accepted = QtWidgets.QInputDialog.getText(
            self,
            "创建新配方版本",
            "修改原因（将写入审计记录）：",
        )
        if not accepted:
            return
        shown = "、".join(names[:5])
        if len(names) > 5:
            shown += f" 等{len(names)}个文件"
        answer = QtWidgets.QMessageBox.question(
            self,
            "确认受影响范围",
            f"将当前处理参数创建为新版本并应用于 {len(names)} 帧：\n{shown}\n\n"
            "旧版本不会被覆盖。是否继续？",
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self.create_batch_recipe_revision(scope, reason=reason)
        except (OSError, ProcessingError, ValueError) as exc:
            self.recipe_panel.set_status(f"创建配方版本失败：{exc}", error=True)

    def load_processing_recipe_into_ui(self, path: str | Path) -> ProcessingRecipe:
        recipe = load_processing_recipe(path)
        self._apply_processing_recipe_to_ui(recipe)
        return recipe

    def load_batch_processing_plan_into_ui(
        self, path: str | Path
    ) -> BatchProcessingPlan:
        plan = load_batch_processing_plan(path)
        by_path = {
            str(item.path.expanduser().absolute()): (index, item.path)
            for index, item in enumerate(self.browser.files)
        }
        selected_rows: list[int] = []
        for identity in plan.sample_sources:
            match = by_path.get(str(Path(identity.path).expanduser().absolute()))
            if match is None:
                raise ProcessingError(
                    f"批次样品不在当前文件栏：{Path(identity.path).name}"
                )
            row, sample_path = match
            actual = FileIdentity.from_path(sample_path)
            if actual.size_bytes != identity.size_bytes or actual.sha256 != identity.sha256:
                raise ProcessingError(
                    f"批次样品身份校验失败：{sample_path.name}"
                )
            selected_rows.append(row)
        # Apply the nested recipe without rewriting it through a temporary file.
        self._apply_processing_recipe_to_ui(plan.recipe)
        self.browser.select_paths(
            tuple(
                Path(identity.path).expanduser().absolute()
                for identity in plan.sample_sources
            )
        )
        self._update_batch_selection_count()
        self._batch_plan_draft = plan
        self._update_recipe_revision_summary()
        return plan

    def _apply_processing_recipe_to_ui(self, recipe: ProcessingRecipe) -> None:
        """Apply a pre-parsed recipe after the same checks as file loading."""

        pane = self._active_pane
        if pane is None or pane.frame is None:
            raise ProcessingError("请先载入一张与配方几何尺寸一致的图像")
        expected_shape = (
            recipe.geometry.image_height_px,
            recipe.geometry.image_width_px,
        )
        if pane.frame.data.shape != expected_shape:
            raise ProcessingError(
                f"当前图像尺寸 {pane.frame.data.shape} 与配方尺寸 {expected_shape} 不一致"
            )
        background_path = None
        if recipe.background_source is not None:
            background_path = Path(recipe.background_source.path)
            if not background_path.is_file():
                raise ProcessingError(
                    "配方中的背景文件路径不存在；请先重新定位后再生成新配方"
                )
            actual = FileIdentity.from_path(background_path)
            if (
                actual.size_bytes != recipe.background_source.size_bytes
                or actual.sha256 != recipe.background_source.sha256
            ):
                raise ProcessingError("配方中的背景文件身份校验失败，未应用任何参数")
        external_mask_path = None
        if recipe.external_mask_source is not None:
            identity = recipe.external_mask_source
            external_mask_path = Path(identity.path)
            # The loader verifies existence, file identity, shape, finiteness and
            # non-zero-is-excluded semantics before any visible control changes.
            load_external_mask_layer(
                external_mask_path,
                pane.frame.data.shape,
                expected_size_bytes=identity.size_bytes,
                expected_sha256=identity.sha256,
            )
        descriptor = self.integration_panel.set_integration_recipe(recipe.integration)
        self.geometry_panel.set_geometry(recipe.geometry)
        self._apply_candidate_geometry(recipe.geometry)
        if recipe.metadata.get('prefer_embedded_geometry'):
            pane.geometry_override=None
        self.background_panel.set_configuration(
            recipe.background.mode, background_path,
            assume_equal_exposure_if_missing=recipe.background.metadata.get(
                "assume_equal_exposure_if_missing"
            ) is True,
            source_selection=recipe.background.metadata.get('source_selection'),
        )
        self.mask_panel.set_recipe_options(recipe.mask, external_mask_path)
        self.recipe_panel.set_parameter_mode(recipe.policy.parameter_mode.value)
        pane.viewer.set_manual_mask_descriptors(
            tuple(recipe.manual_mask_descriptors), emit=False
        )
        pane.integration_region = dict(descriptor)
        pane.background_result = None
        pane.mask_result = None
        pane.integration_result = None
        pane.viewer.clear_mask_overlay()
        self._apply_integration_overlay_to_pane(pane)
        self.mask_panel.set_region_count(
            pane.viewer.manual_mask_region_count,
            synchronized=self.display_linker.linked,
        )
        self.background_panel.set_status("处理配方已载入；尚未执行背景预览。")
        self.integration_panel.set_status("处理配方已载入；点击预览后才执行积分。")

    @QtCore.Slot()
    def _choose_save_recipe(self) -> None:
        initial = self._recipe_storage_directory() / "processing-recipe.json"
        selected, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "保存当前处理配方",
            str(initial),
            "处理配方 JSON (*.json)",
        )
        if not selected:
            return
        try:
            saved = self.save_current_processing_recipe_to_path(selected, overwrite=True)
        except (OSError, ProcessingError, ValueError) as exc:
            self.recipe_panel.set_status(
                self._recipe_save_error_text("保存失败", exc), error=True
            )
            return
        self.recipe_panel.set_status(f"已保存处理配方：{saved}")

    @QtCore.Slot()
    def _choose_load_recipe(self, selected=None) -> None:
        initial = self._recipe_storage_directory()
        if not selected:
            selected, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "载入处理配方或批次方案",
                str(initial),
                "处理配方 JSON (*.json);;所有文件 (*)",
            )
        if not selected:
            return
        try:
            document = json.loads(Path(selected).read_text(encoding="utf-8"))
            if isinstance(document, dict) and document.get("schema") in {
                BATCH_PROCESSING_PLAN_SCHEMA_V1,
                BATCH_PROCESSING_PLAN_SCHEMA,
            }:
                self.load_batch_processing_plan_into_ui(selected)
                kind = "批次方案"
            else:
                self.load_processing_recipe_into_ui(selected)
                kind = "处理配方"
        except (OSError, ProcessingError, ValueError) as exc:
            self.recipe_panel.set_status(f"载入失败：{exc}", error=True)
            return
        self.recipe_panel.set_status(f"已载入{kind}：{selected}")

    @QtCore.Slot()
    def _choose_save_batch_plan(self) -> None:
        initial = self._recipe_storage_directory() / "batch-plan.json"
        selected, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "保存显式批次方案",
            str(initial),
            "批次方案 JSON (*.json)",
        )
        if not selected:
            return
        try:
            saved = self.save_current_batch_plan_to_path(selected, overwrite=True)
        except (OSError, ProcessingError, ValueError) as exc:
            self.recipe_panel.set_status(
                self._recipe_save_error_text("批次方案保存失败", exc), error=True
            )
            return
        self.recipe_panel.set_status(
            f"已保存批次方案：{saved}；样品范围来自左侧明确选择。"
        )

    @QtCore.Slot()
    def _choose_run_batch(self) -> None:
        if self._batch_thread is not None and self._batch_thread.isRunning():
            self.recipe_panel.set_status("已有批处理正在运行。", error=True)
            return
        try:
            plan = self._batch_plan_for_current_selection()
        except (OSError, ProcessingError, ValueError) as exc:
            self.recipe_panel.set_status(f"批处理预检失败：{exc}", error=True)
            return
        excluded_inputs = plan.metadata.get("excluded_configured_inputs", [])
        if excluded_inputs:
            shown = "、".join(
                f"{Path(str(item['path'])).name}（{item['reason']}）"
                for item in excluded_inputs[:5]
            )
            answer = QtWidgets.QMessageBox.question(
                self,
                "确认批处理样品",
                f"将处理 {len(plan.sample_sources)} 个样品。\n\n"
                f"已自动排除 {len(excluded_inputs)} 个已配置输入：\n{shown}\n\n"
                "是否继续？",
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.Yes,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                self.recipe_panel.set_status("已取消批处理；左侧选择未改变。")
                return
        initial = artifacts_directory() / "batch_runs"
        initial.mkdir(parents=True, exist_ok=True)
        selected = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "选择批处理结果的上级文件夹",
            str(initial),
        )
        if not selected:
            return
        base = Path(selected).expanduser().absolute()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        integration_label = batch_integration_directory_label(plan)
        directory_stem = f"batch_{integration_label}_{timestamp}"
        from .export_naming import choose_run_directory
        run_directory=choose_run_directory(self,base,directory_stem)
        if run_directory is None:return
        self._start_batch_worker(plan, run_directory, resume=False)

    def _start_batch_worker(
        self,
        plan: BatchProcessingPlan,
        run_directory: Path,
        *,
        resume: bool,
    ) -> None:
        thread = QtCore.QThread(self)
        worker = BatchExecutionWorker(
            plan,
            run_directory,
            self._protected_recipe_roots(),
            resume=resume,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._batch_progress_updated)
        worker.finished.connect(self._batch_finished)
        worker.failed.connect(self._batch_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._batch_thread_finished)
        self._batch_thread = thread
        self._batch_worker = worker
        self.recipe_panel.set_batch_running(True)
        self.recipe_panel.set_batch_progress(
            {
                "total": len(plan.sample_sources),
                "pending": len(plan.sample_sources),
                "passed": 0,
                "needs_review": 0,
                "failed": 0,
            }
        )
        action = "恢复" if resume else "启动"
        self.recipe_panel.set_status(f"批处理已{action}：{run_directory}")
        thread.start()

    @QtCore.Slot()
    def _choose_resume_batch(self, selected=None) -> None:
        if self._batch_thread is not None and self._batch_thread.isRunning():
            self.recipe_panel.set_status("已有批处理正在运行。", error=True)
            return
        initial = artifacts_directory() / "batch_runs"
        if not selected:
            selected, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "选择需要恢复的批处理清单",
                str(initial),
                "批处理清单 (batch_manifest.json);;JSON (*.json)",
            )
        if not selected:
            return
        manifest_path = Path(selected).expanduser().absolute()
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("schema") not in {
                BATCH_RUN_SCHEMA,
                LEGACY_BATCH_RUN_SCHEMA,
            }:
                raise ProcessingError("所选文件不是受支持的批处理清单")
            if document.get("state") == "completed":
                raise ProcessingError("该批处理已经完整结束，无需恢复")
            plan_document = document.get("plan")
            if not isinstance(plan_document, dict):
                raise ProcessingError("批处理清单缺少可恢复的配方快照")
            plan = batch_processing_plan_from_document(plan_document)
        except (OSError, json.JSONDecodeError, ProcessingError, ValueError) as exc:
            self.recipe_panel.set_status(f"恢复预检失败：{exc}", error=True)
            return
        self._start_batch_worker(plan, manifest_path.parent, resume=True)

    @QtCore.Slot(object)
    def _batch_progress_updated(self, summary: object) -> None:
        if isinstance(summary, dict):
            self.recipe_panel.set_batch_progress(summary)

    @QtCore.Slot()
    def _request_batch_pause(self) -> None:
        if self._batch_worker is None:
            return
        self._batch_worker.request_stop()
        self.recipe_panel.pause_batch_button.setEnabled(False)
        self.recipe_panel.set_status("已请求暂停；当前文件完成后保存清单并暂停。")

    @QtCore.Slot(object)
    def _batch_finished(self, result: object) -> None:
        self.recipe_panel.set_batch_running(False)
        if not isinstance(result, BatchRunResult):
            self.recipe_panel.set_status("批处理返回了未知结果。", error=True)
            return
        self.recipe_panel.set_batch_progress(dict(result.summary))
        preflight_note = ""
        try:
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            preflight = manifest.get("preflight", {})
            issues = preflight.get("issues", []) if isinstance(preflight, dict) else []
            if issues:
                preflight_note = f"；结构预检有 {len(issues)} 项需复核（不含科学QC）"
            elif isinstance(preflight, dict) and preflight:
                preflight_note = "；代表帧结构预检通过（未做科学QC）"
        except (OSError, json.JSONDecodeError):
            preflight_note = "；结构预检摘要读取失败，请查看清单"
        if result.state == "paused":
            self.recipe_panel.set_status(
                f"批处理已暂停：{result.manifest_path}；可点击“恢复已有批处理”继续"
                f"{preflight_note}。"
            )
        else:
            failed = int(result.summary.get("failed", 0))
            self.recipe_panel.set_status(
                f"批处理完成：{result.manifest_path}"
                + (f"；{failed} 个文件失败，请查看清单。" if failed else "")
                + preflight_note
            )

    @QtCore.Slot(str)
    def _batch_failed(self, message: str) -> None:
        self.recipe_panel.set_batch_running(False)
        self.recipe_panel.set_status(f"批处理启动/执行失败：{message}", error=True)

    @QtCore.Slot()
    def _batch_thread_finished(self) -> None:
        self._batch_thread = None
        self._batch_worker = None

    @QtCore.Slot()
    def _clear_mask(self) -> None:
        self._follow_mask=False
        self._mask_tasks.invalidate()
        self._integration_tasks.invalidate()
        pane = self._active_pane
        if pane is None:
            self.mask_panel.set_status("当前没有图像窗格。", error=True)
            return
        pane.mask_result = None
        pane.viewer.clear_mask_overlay()
        if pane.current_path is not None:
            suffix = " [背景预览]" if pane.background_result is not None else ""
            pane.title_label.setText(
                f"图像 {pane.pane_number} · {pane.current_path.name}{suffix}"
            )
        self.mask_panel.set_status("已隐藏MASK；处理配方尚未保存。")
        self.statusBar().showMessage("已隐藏MASK；原始数组未修改")

    @QtCore.Slot()
    def _toggle_browser_panel(self) -> None:
        visible = not self.browser.isVisible()
        self.browser.setVisible(visible)
        self.toggle_browser_button.setText("< 文件栏" if visible else "文件栏 >")
        if visible:
            sizes = self.main_splitter.sizes()
            total = max(sum(sizes), self.width())
            self.main_splitter.setSizes([270, max(0, total - 270 - sizes[2]), sizes[2]])
        self.adaptive_layout.schedule()

    @QtCore.Slot()
    def _toggle_preview_panel(self) -> None:
        self._set_preview_visible(not self.preview_panel.isVisible())

    def _set_preview_visible(self, visible: bool) -> None:
        self.preview_panel.setVisible(visible)
        self.toggle_preview_button.setText("收起预览 ▶" if visible else "几何/预览 ▶")
        if visible:
            sizes = self.main_splitter.sizes()
            total = max(sum(sizes), self.width())
            left = sizes[0] if self.browser.isVisible() else 0
            from .adaptive_layout import recommended_sizes
            wanted=recommended_sizes(total,self.fontMetrics().horizontalAdvance('M'),browser=self.browser.isVisible())[2]
            self.main_splitter.setSizes([left, max(0, total - left - wanted), wanted])
        self.adaptive_layout.schedule()

    def _restart_workbench(self):
        self.restart_action.setEnabled(False)
        self._restart_timer.start()
        self._finish_restart()

    def _finish_restart(self):
        folder = self.browser.path_edit.text().strip()
        app = QtWidgets.QApplication.instance()
        if self.close():
            self._restart_timer.stop()
            app.setProperty("workbench_restart_folder", folder)
            app.quit()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 - Qt API
        self._integration_tasks.invalidate()
        self._overlay_tasks.invalidate()
        self._background_tasks.invalidate()
        self._mask_tasks.invalidate()
        from .curve_export_dialog import CurveExportDialog
        curve_exports=self.findChildren(CurveExportDialog)
        for dialog in curve_exports:dialog.request_stop()
        if any(dialog._export_tasks.busy or dialog._export_preview_tasks.busy for dialog in curve_exports):
            self.statusBar().showMessage('正在结束曲线导出，请稍后关闭。')
            event.ignore()
            return
        if any(queue.busy for queue in (self._integration_tasks,self._overlay_tasks,self._background_tasks,self._mask_tasks)):
            self.statusBar().showMessage("正在结束当前积分，请稍后关闭。")
            event.ignore()
            return
        if self._processed_data_thread is not None and self._processed_data_thread.isRunning():
            self._processed_data_worker.request_stop()
            self.statusBar().showMessage('正在完成当前数据文件的导出，请稍后关闭。')
            event.ignore()
            return
        for pane in self._panes:
            pane.frame_player.pause()
        if any(pane.frame_player.busy for pane in self._panes):
            self.statusBar().showMessage("正在完成帧读取，请稍后关闭程序。")
            event.ignore()
            return
        active_fits = [window for window in self._peak_fit_windows if window.fit_panel._auto_batch_active or window.fit_panel._interactive_tasks.busy or getattr(window.fit_panel, "image_export_active", False)]
        active_analyses = [dialog for kind in (RulandDialog, SaxsAnalysisDialog, DebyeBuecheDialog)
                           for dialog in self.findChildren(kind)
                           if dialog.batch_active or getattr(dialog, "single_active", False)
                           or getattr(dialog, "peak_suggestion_active", False)
                           or getattr(dialog, "image_export_active", False)]
        if active_fits or active_analyses:
            for window in active_fits:
                window.fit_panel._auto_batch_stop = True
                if getattr(window.fit_panel, "image_export_active", False):
                    window.fit_panel.image_stop.set()
                window.fit_panel.status_label.setText("正在完成当前样品，完成后可关闭程序。")
            for dialog in active_analyses:
                if dialog.batch_active:
                    dialog.batch_stop.set()
                if getattr(dialog, "image_export_active", False):
                    dialog.image_stop.set()
                dialog.label.setText("正在完成当前任务，请稍后关闭程序。")
            self.statusBar().showMessage("分析任务正在完成当前样品或曲线，请稍后再关闭程序。")
            event.ignore()
            return
        if self._image_export_thread is not None and self._image_export_thread.isRunning():
            if self._image_export_worker is not None:
                self._image_export_worker.request_stop()
            self._image_export_thread.quit()
            if not self._image_export_thread.wait(30_000):
                self.statusBar().showMessage("2D导出正在完成当前文件，请稍后再关闭。")
                event.ignore()
                return
        if self._batch_thread is not None and self._batch_thread.isRunning():
            if self._batch_worker is not None:
                self._batch_worker.request_stop()
            self._batch_thread.quit()
            if not self._batch_thread.wait(30_000):
                self.statusBar().showMessage("批处理正在完成当前文件，请稍后再关闭。")
                event.ignore()
                return
        if self._calibration_thread is not None and self._calibration_thread.isRunning():
            self._calibration_thread.quit()
            self._calibration_thread.wait(15_000)
        if self._fit_curve_thread is not None and self._fit_curve_thread.isRunning():
            if self._fit_curve_worker is not None:
                self._fit_curve_worker.request_stop()
            self._fit_curve_thread.quit()
            if not self._fit_curve_thread.wait(30_000):
                self.statusBar().showMessage(
                    "正在完成当前勾选样品的积分，请稍后再关闭。"
                )
                event.ignore()
                return
        for fit_window in tuple(self._peak_fit_windows):
            fit_window.close()
        self._peak_fit_windows.clear()
        if self.shared_controls is not None:
            active_controls = self.shared_controls
            self.display_toolbar_layout.removeWidget(active_controls)
            owner = next(
                (
                    pane.viewer
                    for pane in self._panes
                    if pane.viewer.controls_widget is active_controls
                ),
                None,
            )
            if owner is not None:
                active_controls.hide()
                active_controls.setParent(owner)
                owner.layout().insertWidget(0, active_controls)
            self.shared_controls = None
        for pane in self._panes:
            pane.viewer.shutdown_graphics()
        super().closeEvent(event)
        self.deleteLater()

def create_application(argv: list[str] | None = None) -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv or [])


def synthetic_detector_image(height: int = 600, width: int = 800) -> np.ndarray:
    y, x = np.mgrid[:height, :width]
    cx, cy = width * 0.47, height * 0.53
    radius = np.hypot(x - cx, y - cy)
    image = 10.0 + 1800.0 * np.exp(-0.5 * ((radius - 165.0) / 7.0) ** 2)
    image += 450.0 * np.exp(-0.5 * ((radius - 270.0) / 12.0) ** 2)
    image.setflags(write=False)
    return image


def main() -> int:
    app = create_application(sys.argv)
    from .runtime_diagnostics import install_ui_monitor
    install_ui_monitor(app)
    settings = QtCore.QSettings("SAXS-WAXS Workbench", "SAXS-WAXS Workbench")
    window = MainWindow(settings=settings)
    initial = resolve_startup_folder(sys.argv[1:], settings)
    if initial is not None:
        window.set_folder(initial)
    window.showMaximized()
    if initial is None:
        QtCore.QTimer.singleShot(0, window.choose_startup_folder)
    exit_code = app.exec()
    restart_folder = app.property("workbench_restart_folder")
    if restart_folder is not None:
        args = [] if getattr(sys, "frozen", False) else ["-m", "saxs_waxs_ui.app"]
        if restart_folder and Path(restart_folder).is_dir():
            args.append(restart_folder)
        started, _ = QtCore.QProcess.startDetached(sys.executable, args)
        if not started:
            QtWidgets.QMessageBox.critical(None, "重启失败", "请从原启动入口重新打开工作台。")
            return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

