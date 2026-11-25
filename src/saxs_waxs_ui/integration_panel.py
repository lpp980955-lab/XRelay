"""Compact single/multi-pane integration preview controls."""

from __future__ import annotations

import csv
import html
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from saxs_waxs_core.integration import (
    AzimuthSector,
    AzimuthalIntegrationRecipe,
    IntegrationCurve,
    IntegrationError,
    PixelSplitting,
    RadialIntegrationRecipe,
    RadialUnit,
)
from .widgets import CollapsibleSection, scrollable_content


class EditableAxisItem(pg.AxisItem):
    """Axis that opens the existing range editor on double-click."""

    range_edit_requested = QtCore.Signal()

    def tickSpacing(self,minVal,maxVal,size):
        levels=super().tickSpacing(minVal,maxVal,size)
        count=getattr(self,'_curve_minor_count',None)
        if count is None or not levels:return levels
        if not count:return levels[:1]
        major,offset=levels[0]
        return [(major,offset),(major/(int(count)+1),offset)]

    def generateDrawSpecs(self,painter):
        result=super().generateDrawSpecs(painter)
        if result is None or not hasattr(self,'_curve_minor_length'):return result
        axis,ticks,text=result;maximum=abs(self.style['tickLength']);changed=[]
        for pen,p1,p2 in ticks:
            delta=p2-p1;length=math.hypot(delta.x(),delta.y())
            if 0<length<maximum-.01:p2=p1+delta*(self._curve_minor_length/length)
            changed.append((pen,p1,p2))
        return axis,changed,text

    def mouseDoubleClickEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        self.range_edit_requested.emit()
        if hasattr(event, "accept"):
            event.accept()


class IntegrationPanel(QtWidgets.QWidget):
    plot_changed=QtCore.Signal()
    preview_requested = QtCore.Signal()
    region_changed = QtCore.Signal(object)
    overlay_visibility_changed = QtCore.Signal(bool)

    def __init__(self, parent: QtWidgets.QWidget | None = None,*,plot_settings=None) -> None:
        super().__init__(parent)
        from .curve_style import default_style
        self._curve_style_settings=plot_settings if plot_settings is not None else QtCore.QSettings('XRelay','CurvePlotStyle')
        self._curve_style=default_style(self._curve_style_settings)
        self._curve_overrides={};self._visible_curve_keys=[];self._curve_style_dialog=None
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("一维径向 I–q / I–2θ", "radial")
        self.mode_combo.addItem("完整360°方位角 I–φ", "azimuthal")

        self.unit_combo = QtWidgets.QComboBox()
        for label, value in (
            ("q (nm⁻¹)", RadialUnit.Q_NM_INV.value),
            ("q (Å⁻¹)", RadialUnit.Q_ANGSTROM_INV.value),
            ("s (nm⁻¹)", RadialUnit.S_NM_INV.value),
            ("2θ (deg)", RadialUnit.TWO_THETA_DEG.value),
        ):
            self.unit_combo.addItem(label, value)

        self.splitting_combo = QtWidgets.QComboBox()
        self.splitting_combo.addItem("bbox（候选默认）", PixelSplitting.BOUNDING_BOX.value)
        self.splitting_combo.addItem("no（像素中心）", PixelSplitting.NONE.value)
        self.splitting_combo.addItem("full（完整分割）", PixelSplitting.FULL.value)
        self.solid_angle_check = QtWidgets.QCheckBox("立体角校正")
        self.solid_angle_check.setChecked(False)
        self.solid_angle_check.setToolTip("首版默认关闭；选择会进入审计记录")

        self.show_region_check = QtWidgets.QCheckBox("显示积分区域")
        self.show_region_check.setChecked(True)
        self.show_region_check.setToolTip(
            "只控制二维图上的扇区/环带是否可见，不删除区域参数，也不改变积分结果"
        )

        self.curve_scope_combo = QtWidgets.QComboBox()
        self.curve_scope_combo.addItem("当前窗格", "current")
        self.curve_scope_combo.addItem("全部已打开窗格（各自区域）", "all_open")
        self.curve_scope_combo.addItem("左侧勾选样品／帧", "checked")
        self.curve_scope_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.curve_scope_combo.setMinimumContentsLength(8)
        self.curve_scope_combo.setCurrentIndex(1)
        self.curve_scope_combo.setToolTip(
            "左侧勾选范围使用当前代表图的几何、背景、MASK与积分区域；已打开窗格范围使用各自区域。"
        )

        self.x_display_combo = QtWidgets.QComboBox()
        self.y_display_combo = QtWidgets.QComboBox()
        self._populate_axis_options("radial_1d")
        self.x_display_combo.setToolTip("仅改变预览坐标，不改变积分结果或配方")
        self.y_display_combo.setToolTip(
            "非正强度在对数显示中保留为断点，不删除也不替换原始点"
        )
        self.auto_range_button = QtWidgets.QPushButton("自动范围")
        self.manual_range_button = QtWidgets.QPushButton("设置范围…")
        range_buttons = QtWidgets.QWidget()
        range_layout = QtWidgets.QHBoxLayout(range_buttons)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.addWidget(self.auto_range_button)
        range_layout.addWidget(self.manual_range_button)
        self._curve_items: list[tuple[str, IntegrationCurve]] = []
        self._available_curves=[]
        self._available_contexts=[]
        self._available_keys=[]
        self._curve_visibility={}
        self._curve_selection_dialog=None
        self._curve_parameter_contexts: list[Mapping[str, Any]] = []
        self._plot_items: list[pg.PlotDataItem] = []
        self._focused_curve_index: int | None = None
        self._curve_colors = (
            "#0F4D92",
            "#B64342",
            "#2E7D5B",
            "#9A4D8E",
            "#42949E",
            "#767676",
        )
        self._preview_line_width = 2.4
        self._preview_focus_line_width = 3.6
        self._manual_x_range: tuple[float, float] | None = None
        self._manual_y_range: tuple[float, float] | None = None
        self._last_hover_point: tuple[float, float] | None = None
        self._measurement_guides: list[dict[str, object]] = []
        self._selected_measurement_guide: dict[str, object] | None = None

        self.radial_npt = QtWidgets.QSpinBox()
        self.radial_npt.setRange(16, 20000)
        self.radial_npt.setValue(1000)
        self.sector_combo = QtWidgets.QComboBox()
        self.sector_combo.addItem("全环", "full")
        self.sector_combo.addItem("单侧扇区", "single")
        self.sector_combo.addItem("两侧对称扇区", "opposite")
        self.sector_center = QtWidgets.QDoubleSpinBox()
        self.sector_center.setRange(0.0, 359.99)
        self.sector_center.setDecimals(2)
        self.sector_center.setValue(90.0)
        self.sector_center.setSuffix("°")
        self.sector_width = QtWidgets.QDoubleSpinBox()
        self.sector_width.setRange(0.1, 360.0)
        self.sector_width.setDecimals(2)
        self.sector_width.setValue(35.0)
        self.sector_width.setSuffix("°")

        self.azimuth_lower = QtWidgets.QDoubleSpinBox()
        self.azimuth_upper = QtWidgets.QDoubleSpinBox()
        for widget in (self.azimuth_lower, self.azimuth_upper):
            widget.setRange(0.0, 1e6)
            widget.setDecimals(6)
            widget.setSingleStep(0.001)
            widget.setAccelerated(True)
            widget.setToolTip(
                "箭头每次微调 0.001；也可直接键入数值。拖动圆环时仍按探测器像素定位。"
            )
        self.azimuth_lower.setValue(0.1)
        self.azimuth_upper.setValue(0.3)
        self.azimuth_npt = QtWidgets.QSpinBox()
        self.azimuth_npt.setRange(36, 7200)
        self.azimuth_npt.setValue(360)
        self.azimuth_radial_bins = QtWidgets.QSpinBox()
        self.azimuth_radial_bins.setRange(2, 5000)
        self.azimuth_radial_bins.setValue(100)
        for widget in (self.radial_npt,self.sector_center,self.sector_width,
                       self.azimuth_lower,self.azimuth_upper,self.azimuth_npt,self.azimuth_radial_bins):
            widget.setKeyboardTracking(False)
            widget.setToolTip(widget.toolTip()+'\n键入后按 Enter 或点击其他位置应用；箭头立即微调。')

        self.radial_widget = QtWidgets.QWidget()
        radial_form = QtWidgets.QFormLayout(self.radial_widget)
        radial_form.setContentsMargins(0, 0, 0, 0)
        radial_form.addRow("积分区域", self.sector_combo)
        radial_form.addRow("扇区中心", self.sector_center)
        radial_form.addRow("扇区总开角", self.sector_width)

        self.azimuth_widget = QtWidgets.QWidget()
        azimuth_form = QtWidgets.QFormLayout(self.azimuth_widget)
        azimuth_form.setContentsMargins(0, 0, 0, 0)
        azimuth_form.addRow("径向下限", self.azimuth_lower)
        azimuth_form.addRow("径向上限", self.azimuth_upper)

        self.mode_stack = QtWidgets.QStackedWidget()
        self.mode_stack.addWidget(self.radial_widget)
        self.mode_stack.addWidget(self.azimuth_widget)
        self.preview_button = QtWidgets.QPushButton("预览积分曲线")
        self.select_curves_button=QtWidgets.QPushButton('显示曲线（0/0）…')
        self.select_curves_button.setToolTip('选择已计算曲线的显示和当前导出范围，不重新积分。')
        self.select_curves_button.setEnabled(False)
        self.select_curves_button.clicked.connect(self._choose_visible_curves)
        self.curve_style_button=QtWidgets.QPushButton('绘图选项…')
        self.curve_style_button.clicked.connect(self._choose_curve_style)
        self.status_label = QtWidgets.QLabel("未执行积分；预览与后续批处理共用同一核心。")
        self.status_label.setWordWrap(True)
        self._plot_axes = {
            side: EditableAxisItem(orientation=side)
            for side in ("bottom", "left", "top", "right")
        }
        self.plot = pg.PlotWidget(axisItems=self._plot_axes)
        self.plot.setBackground("w")
        self.plot.showGrid(x=False, y=False)
        from .curve_style import EditableLegend
        self.legend = EditableLegend(self,offset=(8,8))
        self.legend.setParentItem(self.plot.getViewBox())
        self.plot.getPlotItem().legend=self.legend
        self.plot.getPlotItem().setMenuEnabled(False)
        self.plot.getViewBox().setMenuEnabled(False)
        self.plot.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.plot.customContextMenuRequested.connect(self._show_curve_menu)
        self.plot.setMinimumHeight(240)
        self.plot.showAxis("top", show=False)
        self.plot.showAxis("right", show=False)
        self._plot_axes["top"].setStyle(showValues=False)
        self._plot_axes["right"].setStyle(showValues=False)
        for axis in self._plot_axes.values():
            axis.range_edit_requested.connect(self._request_manual_ranges)
        self._apply_preview_plot_style()
        self._hover_marker = pg.TargetItem(
            pos=(0.0, 0.0),
            size=8,
            pen=pg.mkPen("#111111", width=1.2),
            brush=pg.mkBrush(255, 255, 255, 180),
            movable=False,
        )
        self._hover_marker.setZValue(90)
        self._hover_marker.hide()
        self._hover_text = pg.TextItem(
            "",
            color="#111111",
            fill=pg.mkBrush(255, 255, 255, 225),
            border=pg.mkPen("#777777", width=0.8),
            anchor=(0.0, 1.0),
        )
        self._hover_text.setZValue(91)
        self._hover_text.hide()
        self._install_measurement_items()

        self.four_frame_check = QtWidgets.QCheckBox("四边框")
        self.grid_check = QtWidgets.QCheckBox("网格")
        self.four_frame_check.setChecked(self._curve_style['frame'])
        self.grid_check.setChecked(self._curve_style['grid'])
        self.data_layout_combo = QtWidgets.QComboBox()
        self.data_layout_combo.addItem("宽表 XY（Origin）", "wide")
        self.data_layout_combo.addItem("长表", "long")
        self.data_layout_combo.setToolTip(
            "CSV/TSV：宽表按相邻 XY 列导入 Origin；长表按曲线逐点排列。"
        )
        self.export_data_button = QtWidgets.QPushButton("导出结果…")
        self.export_image_button = QtWidgets.QPushButton("导出图片…")
        self.add_guide_button = QtWidgets.QPushButton("＋辅助线")
        self.delete_guide_button = QtWidgets.QPushButton("删除辅助线")
        self.clear_guides_button = QtWidgets.QPushButton("清空辅助线")
        self.delete_guide_button.setEnabled(False)
        self.clear_guides_button.setEnabled(False)
        self.focus_hint = QtWidgets.QLabel("点击图例可突出曲线；再次点击可恢复。")
        self.focus_hint.setWordWrap(True)
        plot_options = QtWidgets.QWidget()
        plot_options_layout = QtWidgets.QGridLayout(plot_options)
        plot_options_layout.setContentsMargins(4, 2, 4, 4)
        plot_options_layout.addWidget(self.four_frame_check, 0, 0)
        plot_options_layout.addWidget(self.grid_check, 0, 1)
        plot_options_layout.addWidget(self.auto_range_button, 0, 2)
        plot_options_layout.addWidget(self.data_layout_combo, 1, 0, 1, 2)
        plot_options_layout.addWidget(self.export_data_button, 1, 2)
        plot_options_layout.addWidget(self.export_image_button, 2, 0, 1, 3)
        self.export_image_button.hide()
        self.data_layout_combo.hide()
        plot_options_layout.addWidget(self.add_guide_button, 3, 0)
        plot_options_layout.addWidget(self.delete_guide_button, 3, 1)
        plot_options_layout.addWidget(self.clear_guides_button, 3, 2)
        plot_options_layout.addWidget(self.focus_hint, 4, 0, 1, 3)
        self.plot_options_section = CollapsibleSection(
            "曲线显示与导出", plot_options, expanded=False
        )

        form = QtWidgets.QFormLayout()
        form.addRow("预览类型", self.mode_combo)
        form.addRow("曲线范围", self.curve_scope_combo)
        form.addRow("图上区域", self.show_region_check)
        advanced_widget = QtWidgets.QWidget()
        advanced_form = QtWidgets.QFormLayout(advanced_widget)
        advanced_form.setContentsMargins(4, 2, 4, 4)
        advanced_form.addRow("径向单位", self.unit_combo)
        advanced_form.addRow("像素分割", self.splitting_combo)
        advanced_form.addRow("修正", self.solid_angle_check)
        advanced_form.addRow("径向点数", self.radial_npt)
        advanced_form.addRow("方位点数", self.azimuth_npt)
        advanced_form.addRow("方位内部bin", self.azimuth_radial_bins)
        advanced_form.addRow("横坐标", self.x_display_combo)
        advanced_form.addRow("纵坐标", self.y_display_combo)
        advanced_form.addRow("坐标范围", range_buttons)
        self.advanced_section = CollapsibleSection(
            "更多积分与坐标设置", advanced_widget, expanded=False
        )
        controls = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(controls)
        layout.addLayout(form)
        layout.addWidget(self.mode_stack)
        layout.addWidget(self.advanced_section)
        layout.addWidget(self.status_label)
        layout.addWidget(self.plot_options_section)
        self.plot_options_section.hide()
        layout.addStretch(1)
        self.controls_scroll = scrollable_content(controls)
        self.integration_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.integration_splitter.addWidget(self.controls_scroll)
        plot_host=QtWidgets.QWidget();plot_layout=QtWidgets.QVBoxLayout(plot_host);plot_layout.setContentsMargins(0,0,0,0)
        plot_layout.addWidget(self.plot,1)
        self._outside_legend=QtWidgets.QScrollArea();self._outside_legend.setWidgetResizable(True)
        legend_content=QtWidgets.QWidget();self._outside_legend_layout=QtWidgets.QGridLayout(legend_content)
        self._outside_legend_layout.setContentsMargins(4,2,4,2)
        self._outside_legend.setWidget(legend_content);self._outside_legend.hide();plot_layout.addWidget(self._outside_legend)
        self.integration_splitter.addWidget(plot_host)
        self.integration_splitter.setStretchFactor(0, 0)
        self.integration_splitter.setStretchFactor(1, 1)
        self.integration_splitter.setSizes([270, 400])
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        shortcuts=QtWidgets.QHBoxLayout();shortcuts.addWidget(self.preview_button,1)
        self._curve_menu_button=QtWidgets.QToolButton();self._curve_menu_button.setText('…')
        self._curve_menu_button.setToolTip('曲线菜单（也可在曲线区右键打开）')
        self._curve_menu_button.clicked.connect(lambda:self._show_curve_menu_global(self._curve_menu_button.mapToGlobal(QtCore.QPoint(0,self._curve_menu_button.height()))))
        shortcuts.addWidget(self._curve_menu_button);outer.addLayout(shortcuts)
        self.select_curves_button.hide();self.curve_style_button.hide()
        self.plot.setToolTip('右键打开曲线菜单：选择曲线、绘图选项、坐标范围、导出与辅助线。')
        outer.addWidget(self.integration_splitter)

        self.show_region_check.toggled.connect(self.overlay_visibility_changed)

        self.mode_combo.currentIndexChanged.connect(self.mode_stack.setCurrentIndex)
        self.mode_combo.currentIndexChanged.connect(self._update_controls)
        self.sector_combo.currentIndexChanged.connect(self._update_controls)
        for signal in (
            self.mode_combo.currentIndexChanged,
            self.unit_combo.currentIndexChanged,
            self.sector_combo.currentIndexChanged,
            self.sector_center.valueChanged,
            self.sector_width.valueChanged,
            self.azimuth_lower.valueChanged,
            self.azimuth_upper.valueChanged,
        ):
            signal.connect(self._emit_region_changed)
        self.preview_button.clicked.connect(self.preview_requested)
        self.x_display_combo.currentIndexChanged.connect(self._refresh_curve_display)
        self.y_display_combo.currentIndexChanged.connect(self._refresh_curve_display)
        self.auto_range_button.clicked.connect(self.reset_auto_ranges)
        self.manual_range_button.clicked.connect(self._request_manual_ranges)
        self.four_frame_check.toggled.connect(self._set_four_frame)
        self.grid_check.toggled.connect(self._set_grid_visible)
        self.export_data_button.clicked.connect(self._choose_unified_export)
        self.export_image_button.clicked.connect(self._choose_export_image)
        self.add_guide_button.clicked.connect(self.add_measurement_guide)
        self.delete_guide_button.clicked.connect(self.delete_selected_measurement_guide)
        self.clear_guides_button.clicked.connect(self.clear_measurement_guides)
        self.plot.scene().sigMouseClicked.connect(self._plot_scene_clicked)
        self._mouse_move_proxy = pg.SignalProxy(
            self.plot.scene().sigMouseMoved,
            rateLimit=60,
            slot=self._plot_mouse_moved,
        )
        self.plot.installEventFilter(self)
        self.plot.viewport().installEventFilter(self)
        self._update_controls()
        self._set_four_frame(self.four_frame_check.isChecked())
        self._set_grid_visible(self.grid_check.isChecked())

    def _install_measurement_items(self) -> None:
        self.plot.addItem(self._hover_marker, ignoreBounds=True)
        self.plot.addItem(self._hover_text, ignoreBounds=True)
        for guide in self._measurement_guides:
            self.plot.addItem(guide["vertical"], ignoreBounds=True)
            self.plot.addItem(guide["horizontal"], ignoreBounds=True)
            self.plot.addItem(guide["label"], ignoreBounds=True)

    @staticmethod
    def _format_coordinate(value: float) -> str:
        return f"{float(value):.7g}"

    def _nearest_curve_point(
        self, scene_position: QtCore.QPointF
    ) -> tuple[int, int, float, float, float] | None:
        if not self.plot.sceneBoundingRect().contains(scene_position):
            return None
        view_position = self.plot.getViewBox().mapSceneToView(scene_position)
        nearest: tuple[int, int, float, float, float] | None = None
        try:
            transformed = getattr(self,'_display_curve_cache',None)
            if transformed is None:transformed, _axis_labels = self._transformed_curves()
        except IntegrationError:
            return None
        for curve_index, (_name, _curve, x_values, y_values) in enumerate(transformed):
            finite = np.isfinite(x_values) & np.isfinite(y_values)
            finite_indices = np.flatnonzero(finite)
            if finite_indices.size == 0:
                continue
            finite_x = x_values[finite_indices]
            if finite_x.size > 1 and np.all(np.diff(finite_x) >= 0.0):
                insertion = int(np.searchsorted(finite_x, float(view_position.x())))
                candidate_offsets = {max(0, insertion - 1), min(finite_x.size - 1, insertion)}
                candidate_indices = [int(finite_indices[offset]) for offset in candidate_offsets]
            else:
                candidate_indices = [
                    int(finite_indices[np.argmin(np.abs(finite_x - float(view_position.x())))])
                ]
            for point_index in candidate_indices:
                x_value = float(x_values[point_index])
                y_value = float(y_values[point_index])
                point_scene = self.plot.getViewBox().mapViewToScene(
                    QtCore.QPointF(x_value, y_value)
                )
                distance = math.hypot(
                    float(point_scene.x() - scene_position.x()),
                    float(point_scene.y() - scene_position.y()),
                )
                if nearest is None or distance < nearest[4]:
                    nearest = (curve_index, point_index, x_value, y_value, distance)
        return nearest

    @QtCore.Slot(object)
    def _plot_mouse_moved(self, event: object) -> None:
        scene_position = event[0] if isinstance(event, tuple) else event
        if not isinstance(scene_position, QtCore.QPointF):
            return
        nearest = self._nearest_curve_point(scene_position)
        if nearest is None or nearest[4] > 18.0:
            self._hover_marker.hide()
            self._hover_text.hide()
            return
        curve_index, _point_index, x_value, y_value, _distance = nearest
        self._last_hover_point = (x_value, y_value)
        self._hover_marker.setPos(x_value, y_value)
        self._hover_marker.show()
        self._hover_text.setText(
            f"{self._curve_items[curve_index][0]}\n"
            f"x={self._format_coordinate(x_value)}, "
            f"y={self._format_coordinate(y_value)}"
        )
        self._hover_text.setPos(x_value, y_value)
        self._hover_text.show()

    def _guide_moved(self, guide: dict[str, object]) -> None:
        vertical = guide["vertical"]
        horizontal = guide["horizontal"]
        x_value = float(vertical.value())
        y_value = float(horizontal.value())
        guide["position"] = (x_value, y_value)
        label = guide["label"]
        label.setText(
            f"x={self._format_coordinate(x_value)}\n"
            f"y={self._format_coordinate(y_value)}"
        )
        label.setPos(x_value, y_value)

    def _guide_clicked(self, guide: dict[str, object], event: object) -> None:
        if hasattr(event, "accept"):
            event.accept()
        self._select_measurement_guide(guide)

    def _select_measurement_guide(self, guide: dict[str, object] | None) -> None:
        if guide is not None and guide not in self._measurement_guides:
            return
        self._selected_measurement_guide = guide
        for candidate in self._measurement_guides:
            selected = candidate is guide
            pen = pg.mkPen(
                "#D32F2F" if selected else candidate["color"],
                width=2.2 if selected else 1.4,
                style=QtCore.Qt.PenStyle.DashLine,
            )
            candidate["vertical"].setPen(pen)
            candidate["horizontal"].setPen(pen)
        self.delete_guide_button.setEnabled(guide is not None)

    @QtCore.Slot()
    def add_measurement_guide(self) -> None:
        if self._last_hover_point is not None:
            x_value, y_value = self._last_hover_point
        else:
            x_range, y_range = self.plot.viewRange()
            x_value = (float(x_range[0]) + float(x_range[1])) / 2.0
            y_value = (float(y_range[0]) + float(y_range[1])) / 2.0
        color = self._curve_colors[len(self._measurement_guides) % len(self._curve_colors)]
        pen = pg.mkPen(color, width=1.4, style=QtCore.Qt.PenStyle.DashLine)
        hover_pen = pg.mkPen("#D32F2F", width=2.2)
        vertical = pg.InfiniteLine(
            pos=x_value, angle=90, movable=True, pen=pen, hoverPen=hover_pen
        )
        horizontal = pg.InfiniteLine(
            pos=y_value, angle=0, movable=True, pen=pen, hoverPen=hover_pen
        )
        label = pg.TextItem(
            "",
            color="#111111",
            fill=pg.mkBrush(255, 255, 255, 225),
            border=pg.mkPen(color, width=0.8),
            anchor=(0.0, 1.0),
        )
        for item in (vertical, horizontal, label):
            item.setZValue(80)
        guide: dict[str, object] = {
            "vertical": vertical,
            "horizontal": horizontal,
            "label": label,
            "color": color,
            "position": (x_value, y_value),
        }
        vertical.sigPositionChanged.connect(lambda _line, current=guide: self._guide_moved(current))
        horizontal.sigPositionChanged.connect(lambda _line, current=guide: self._guide_moved(current))
        vertical.sigClicked.connect(lambda _line, event, current=guide: self._guide_clicked(current, event))
        horizontal.sigClicked.connect(lambda _line, event, current=guide: self._guide_clicked(current, event))
        self._measurement_guides.append(guide)
        self.plot.addItem(vertical, ignoreBounds=True)
        self.plot.addItem(horizontal, ignoreBounds=True)
        self.plot.addItem(label, ignoreBounds=True)
        self._guide_moved(guide)
        self._select_measurement_guide(guide)
        self.clear_guides_button.setEnabled(True)

    @QtCore.Slot()
    def delete_selected_measurement_guide(self) -> None:
        guide = self._selected_measurement_guide
        if guide is None:
            return
        for key in ("vertical", "horizontal", "label"):
            self.plot.removeItem(guide[key])
        self._measurement_guides.remove(guide)
        self._select_measurement_guide(None)
        self.clear_guides_button.setEnabled(bool(self._measurement_guides))

    @QtCore.Slot()
    def clear_measurement_guides(self) -> None:
        for guide in tuple(self._measurement_guides):
            for key in ("vertical", "horizontal", "label"):
                self.plot.removeItem(guide[key])
        self._measurement_guides.clear()
        self._select_measurement_guide(None)
        self.clear_guides_button.setEnabled(False)

    @property
    def measurement_guide_positions(self) -> tuple[tuple[float, float], ...]:
        return tuple(guide["position"] for guide in self._measurement_guides)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:  # noqa: N802
        if (
            (watched is self.plot or watched is self.plot.viewport())
            and event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() in {QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace}
            and self._selected_measurement_guide is not None
        ):
            self.delete_selected_measurement_guide()
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _apply_preview_plot_style(self) -> None:
        """Apply a readable paper-like style without changing curve data."""

        axis_pen = pg.mkPen("#262626", width=1.25)
        tick_pen = pg.mkPen("#262626", width=1.0)
        tick_font = QtGui.QFont("Arial")
        tick_font.setStyleHint(QtGui.QFont.StyleHint.SansSerif)
        tick_font.setPointSizeF(9.5)
        for axis in self._plot_axes.values():
            axis.setPen(axis_pen)
            axis.setTickPen(tick_pen)
            axis.setTextPen(pg.mkPen("#262626"))
            axis.setTickFont(tick_font)
            axis.setStyle(tickLength=-6, tickTextOffset=4)
        self.plot.getViewBox().setBorder(None)

    @staticmethod
    def _pretty_x_label(curve: IntegrationCurve) -> str:
        if curve.kind == "azimuthal_1d":
            return "φ (°)"
        return {
            RadialUnit.Q_NM_INV.value: "q (nm⁻¹)",
            RadialUnit.Q_ANGSTROM_INV.value: "q (Å⁻¹)",
            RadialUnit.S_NM_INV.value: "s (nm⁻¹)",
            RadialUnit.TWO_THETA_DEG.value: "2θ (°)",
        }.get(curve.x_unit, curve.x_unit)

    @staticmethod
    def _pretty_y_label(curve: IntegrationCurve) -> str:
        unit = {
            "detector_count": "counts",
            "detector_count/s": "counts s⁻¹",
        }.get(curve.intensity_unit, curve.intensity_unit)
        return f"Intensity ({unit})"

    def _populate_axis_options(self, kind: str) -> None:
        previous_x = self.x_display_combo.currentData() if self.x_display_combo.count() else None
        previous_y = self.y_display_combo.currentData() if self.y_display_combo.count() else None
        x_blocker = QtCore.QSignalBlocker(self.x_display_combo)
        y_blocker = QtCore.QSignalBlocker(self.y_display_combo)
        self.x_display_combo.clear()
        self.y_display_combo.clear()
        if kind == "azimuthal_1d":
            self.x_display_combo.addItem("φ (deg)", "native")
            y_options = (
                ("I", "intensity"),
                ("log₁₀(I)", "log10_intensity"),
                ("ln(I)", "ln_intensity"),
            )
        else:
            for label, value in (
                ("原始单位", "native"),
                ("q (nm⁻¹)", "q_nm^-1"),
                ("q (Å⁻¹)", "q_A^-1"),
                ("s (nm⁻¹)", "s_nm^-1"),
                ("2θ (deg)", "2th_deg"),
                ("q² (nm⁻²)", "q2_nm^-2"),
                ("log₁₀(q / nm⁻¹)", "log10_q_nm_inv"),
            ):
                self.x_display_combo.addItem(label, value)
            y_options = (
                ("I", "intensity"),
                ("log₁₀(I)", "log10_intensity"),
                ("ln(I)", "ln_intensity"),
                ("qI", "qI"),
                ("q²I", "q2I"),
                ("q⁴I", "q4I"),
            )
        for label, value in y_options:
            self.y_display_combo.addItem(label, value)
        x_index = self.x_display_combo.findData(previous_x)
        y_index = self.y_display_combo.findData(previous_y)
        self.x_display_combo.setCurrentIndex(max(0, x_index))
        self.y_display_combo.setCurrentIndex(max(0, y_index))
        del x_blocker, y_blocker

    @property
    def mode(self) -> str:
        return str(self.mode_combo.currentData())

    @property
    def curve_scope(self) -> str:
        return str(self.curve_scope_combo.currentData())

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color:#A4262C;" if error else "color:#126B34;")

    def region_descriptor(self) -> dict[str, object]:
        if self.mode == "radial":
            return {
                "mode": "radial",
                "sector_mode": str(self.sector_combo.currentData()),
                "center_deg": float(self.sector_center.value()),
                "width_deg": float(self.sector_width.value()),
            }
        return {
            "mode": "azimuthal",
            "radial_unit": str(self.unit_combo.currentData()),
            "radial_lower": float(self.azimuth_lower.value()),
            "radial_upper": float(self.azimuth_upper.value()),
        }

    def set_region_descriptor(
        self, descriptor: dict[str, object], *, emit: bool = False
    ) -> None:
        widgets = (
            self.mode_combo,
            self.unit_combo,
            self.sector_combo,
            self.sector_center,
            self.sector_width,
            self.azimuth_lower,
            self.azimuth_upper,
        )
        blockers = [QtCore.QSignalBlocker(widget) for widget in widgets]
        mode = str(descriptor.get("mode", "radial"))
        self.mode_combo.setCurrentIndex(0 if mode == "radial" else 1)
        self.mode_stack.setCurrentIndex(self.mode_combo.currentIndex())
        if mode == "radial":
            sector_index = self.sector_combo.findData(
                str(descriptor.get("sector_mode", "full"))
            )
            self.sector_combo.setCurrentIndex(max(0, sector_index))
            self.sector_center.setValue(float(descriptor.get("center_deg", 90.0)))
            self.sector_width.setValue(float(descriptor.get("width_deg", 35.0)))
        else:
            unit_index = self.unit_combo.findData(
                str(descriptor.get("radial_unit", RadialUnit.Q_NM_INV.value))
            )
            self.unit_combo.setCurrentIndex(max(0, unit_index))
            self.azimuth_lower.setValue(float(descriptor.get("radial_lower", 0.1)))
            self.azimuth_upper.setValue(float(descriptor.get("radial_upper", 0.3)))
        del blockers
        self._update_controls()
        if emit:
            self._emit_region_changed()

    @QtCore.Slot()
    def _emit_region_changed(self) -> None:
        descriptor = self.region_descriptor()
        if descriptor["mode"] == "azimuthal" and not (
            float(descriptor["radial_lower"]) < float(descriptor["radial_upper"])
        ):
            self.set_status("方位角积分要求径向下限小于上限。", error=True)
            return
        self.region_changed.emit(descriptor)

    @QtCore.Slot()
    def _update_controls(self) -> None:
        sector_enabled = self.mode == "radial" and self.sector_combo.currentData() != "full"
        self.sector_center.setEnabled(sector_enabled)
        self.sector_width.setEnabled(sector_enabled)

    def radial_recipe(
        self,
        input_unit: str,
        descriptor: dict[str, object] | None = None,
    ) -> RadialIntegrationRecipe:
        descriptor = descriptor or self.region_descriptor()
        sector_mode = str(descriptor.get("sector_mode", "full"))
        sectors: tuple[AzimuthSector, ...] = ()
        if sector_mode in {"single", "opposite"}:
            center = float(descriptor.get("center_deg", self.sector_center.value()))
            width = float(descriptor.get("width_deg", self.sector_width.value()))
            sectors = (AzimuthSector(center, width),)
            if sector_mode == "opposite":
                sectors += (AzimuthSector(center + 180.0, width),)
        return RadialIntegrationRecipe(
            npt=self.radial_npt.value(),
            unit=self.unit_combo.currentData(),
            sectors=sectors,
            pixel_splitting=self.splitting_combo.currentData(),
            correct_solid_angle=self.solid_angle_check.isChecked(),
            input_unit=input_unit,
            metadata={"ui_sector_mode": sector_mode},
        )

    def azimuthal_recipe(
        self,
        input_unit: str,
        descriptor: dict[str, object] | None = None,
    ) -> AzimuthalIntegrationRecipe:
        descriptor = descriptor or self.region_descriptor()
        return AzimuthalIntegrationRecipe(
            radial_range=(
                float(descriptor.get("radial_lower", self.azimuth_lower.value())),
                float(descriptor.get("radial_upper", self.azimuth_upper.value())),
            ),
            radial_unit=str(
                descriptor.get("radial_unit", self.unit_combo.currentData())
            ),
            npt_azimuth=self.azimuth_npt.value(),
            npt_radial=self.azimuth_radial_bins.value(),
            pixel_splitting=self.splitting_combo.currentData(),
            correct_solid_angle=self.solid_angle_check.isChecked(),
            input_unit=input_unit,
            metadata={"ui_mode": "full_360"},
        )

    def set_integration_recipe(
        self,
        recipe: RadialIntegrationRecipe | AzimuthalIntegrationRecipe,
    ) -> dict[str, object]:
        """Load every numerical control the current UI can represent exactly."""

        if recipe.polarization_factor is not None or recipe.normalization_factor != 1.0:
            raise ValueError(
                "当前界面尚不能无损表示偏振或非1归一化因子，已拒绝载入"
            )
        widgets = (
            self.mode_combo,
            self.unit_combo,
            self.splitting_combo,
            self.solid_angle_check,
            self.radial_npt,
            self.sector_combo,
            self.sector_center,
            self.sector_width,
            self.azimuth_lower,
            self.azimuth_upper,
            self.azimuth_npt,
            self.azimuth_radial_bins,
        )
        blockers = [QtCore.QSignalBlocker(widget) for widget in widgets]
        unit_value: str
        if isinstance(recipe, RadialIntegrationRecipe):
            if recipe.radial_range is not None:
                raise ValueError("当前界面尚不能无损表示径向积分裁剪范围，已拒绝载入")
            self.mode_combo.setCurrentIndex(0)
            self.radial_npt.setValue(recipe.npt)
            unit_value = recipe.unit.value
            sector_mode = str(recipe.metadata.get("ui_sector_mode", ""))
            if sector_mode not in {"full", "single", "opposite"}:
                sector_mode = {0: "full", 1: "single", 2: "opposite"}.get(
                    len(recipe.sectors), ""
                )
            if not sector_mode:
                raise ValueError("当前界面不能无损表示该径向扇区组合，已拒绝载入")
            sector_index = self.sector_combo.findData(sector_mode)
            self.sector_combo.setCurrentIndex(sector_index)
            if recipe.sectors:
                first = recipe.sectors[0]
                self.sector_center.setValue(first.center_deg % 360.0)
                self.sector_width.setValue(first.width_deg)
            descriptor = {
                "mode": "radial",
                "sector_mode": sector_mode,
                "center_deg": float(self.sector_center.value()),
                "width_deg": float(self.sector_width.value()),
            }
        else:
            self.mode_combo.setCurrentIndex(1)
            self.azimuth_lower.setValue(recipe.radial_range[0])
            self.azimuth_upper.setValue(recipe.radial_range[1])
            self.azimuth_npt.setValue(recipe.npt_azimuth)
            self.azimuth_radial_bins.setValue(recipe.npt_radial)
            unit_value = recipe.radial_unit.value
            descriptor = {
                "mode": "azimuthal",
                "radial_unit": unit_value,
                "radial_lower": float(recipe.radial_range[0]),
                "radial_upper": float(recipe.radial_range[1]),
            }
        unit_index = self.unit_combo.findData(unit_value)
        splitting_index = self.splitting_combo.findData(recipe.pixel_splitting.value)
        if unit_index < 0 or splitting_index < 0:
            raise ValueError("配方包含当前界面不支持的单位或像素分割方式")
        self.unit_combo.setCurrentIndex(unit_index)
        self.splitting_combo.setCurrentIndex(splitting_index)
        self.solid_angle_check.setChecked(recipe.correct_solid_angle)
        self.mode_stack.setCurrentIndex(self.mode_combo.currentIndex())
        del blockers
        self._update_controls()
        return descriptor

    @staticmethod
    def _q_nm_inverse(curve: IntegrationCurve) -> np.ndarray:
        values = np.asarray(curve.x, dtype=float)
        if curve.x_unit == RadialUnit.Q_NM_INV.value:
            return values.copy()
        if curve.x_unit == RadialUnit.Q_ANGSTROM_INV.value:
            return values * 10.0
        if curve.x_unit == RadialUnit.S_NM_INV.value:
            return values * (2.0 * math.pi)
        if curve.x_unit == RadialUnit.TWO_THETA_DEG.value:
            try:
                wavelength_m = float(curve.audit["geometry_poni"]["wavelength_m"])
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrationError(
                    "2θ转换为q需要曲线审计记录中的波长"
                ) from exc
            wavelength_nm = wavelength_m * 1e9
            return 4.0 * math.pi * np.sin(np.deg2rad(values / 2.0)) / wavelength_nm
        raise IntegrationError(f"不支持从 {curve.x_unit!r} 转换为q")

    def _display_x(self, curve: IntegrationCurve) -> tuple[np.ndarray, str]:
        mode = str(self.x_display_combo.currentData() or "native")
        values = np.asarray(curve.x, dtype=float).copy()
        if curve.kind == "azimuthal_1d" or mode == "native":
            return values, self._pretty_x_label(curve)
        q_nm = self._q_nm_inverse(curve)
        if mode == RadialUnit.Q_NM_INV.value:
            return q_nm, "q (nm⁻¹)"
        if mode == RadialUnit.Q_ANGSTROM_INV.value:
            return q_nm / 10.0, "q (Å⁻¹)"
        if mode == RadialUnit.S_NM_INV.value:
            return q_nm / (2.0 * math.pi), "s (nm⁻¹)"
        if mode == RadialUnit.TWO_THETA_DEG.value:
            try:
                wavelength_nm = float(curve.audit["geometry_poni"]["wavelength_m"]) * 1e9
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrationError("q转换为2θ需要曲线审计记录中的波长") from exc
            argument = q_nm * wavelength_nm / (4.0 * math.pi)
            converted = np.full(q_nm.shape, np.nan, dtype=float)
            valid = np.isfinite(argument) & (np.abs(argument) <= 1.0)
            converted[valid] = np.rad2deg(2.0 * np.arcsin(argument[valid]))
            return converted, "2θ (deg)"
        if mode == "q2_nm^-2":
            return np.square(q_nm), "q² (nm⁻²)"
        if mode == "log10_q_nm_inv":
            converted = np.full(q_nm.shape, np.nan, dtype=float)
            valid = np.isfinite(q_nm) & (q_nm > 0.0)
            converted[valid] = np.log10(q_nm[valid])
            return converted, "log₁₀(q / nm⁻¹)"
        raise IntegrationError(f"未知横坐标显示模式: {mode}")

    def _display_y(self, curve: IntegrationCurve) -> tuple[np.ndarray, str]:
        mode = str(self.y_display_combo.currentData() or "intensity")
        values = np.asarray(curve.intensity, dtype=float).copy()
        label = self._pretty_y_label(curve)
        if mode == "intensity":
            return values, label
        if mode in {"log10_intensity", "ln_intensity"}:
            converted = np.full(values.shape, np.nan, dtype=float)
            valid = np.isfinite(values) & (values > 0.0)
            function = np.log10 if mode == "log10_intensity" else np.log
            converted[valid] = function(values[valid])
            prefix = "log₁₀" if mode == "log10_intensity" else "ln"
            return converted, f"{prefix}(I / {curve.intensity_unit})"
        if curve.kind != "radial_1d":
            raise IntegrationError("方位角曲线不支持q加权纵坐标")
        power = {"qI": 1, "q2I": 2, "q4I": 4}.get(mode)
        if power is None:
            raise IntegrationError(f"未知纵坐标显示模式: {mode}")
        q_nm = self._q_nm_inverse(curve)
        return values * np.power(q_nm, power), {1: "qI", 2: "q²I", 4: "q⁴I"}[power]

    def apply_manual_ranges(
        self,
        x_range: tuple[float, float] | None,
        y_range: tuple[float, float] | None,
    ) -> None:
        for label, values in (("x", x_range), ("y", y_range)):
            if values is None:
                continue
            lower, upper = (float(value) for value in values)
            if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
                raise ValueError(f"{label}坐标范围必须为有限数且下限小于上限")
        self._manual_x_range = x_range
        self._manual_y_range = y_range
        self._apply_view_ranges()

    def _apply_view_ranges(self) -> None:
        if self._manual_x_range is None and self._manual_y_range is None:
            self.plot.enableAutoRange()
            self.plot.autoRange()
            return
        self.plot.enableAutoRange(x=self._manual_x_range is None, y=self._manual_y_range is None)
        if self._manual_x_range is not None:
            self.plot.setXRange(*self._manual_x_range, padding=0.0)
        if self._manual_y_range is not None:
            self.plot.setYRange(*self._manual_y_range, padding=0.0)

    @QtCore.Slot()
    def reset_auto_ranges(self) -> None:
        self._manual_x_range = None
        self._manual_y_range = None
        self._apply_view_ranges()

    @QtCore.Slot()
    def _request_manual_ranges(self) -> None:
        current_x, current_y = self.plot.viewRange()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("设置积分预览范围")
        form = QtWidgets.QFormLayout(dialog)
        fields: list[QtWidgets.QLineEdit] = []
        for label, value in (
            ("x 下限", current_x[0]),
            ("x 上限", current_x[1]),
            ("y 下限", current_y[0]),
            ("y 上限", current_y[1]),
        ):
            field = QtWidgets.QLineEdit(f"{float(value):.9g}")
            form.addRow(label, field)
            fields.append(field)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            values = [float(field.text()) for field in fields]
            self.apply_manual_ranges((values[0], values[1]), (values[2], values[3]))
        except ValueError as exc:
            self.set_status(str(exc), error=True)

    @QtCore.Slot()
    def _refresh_curve_display(self) -> None:
        if self._curve_items:
            self._render_curves()

    def show_curves(
        self,
        curves: Iterable[tuple[str, IntegrationCurve]],
        parameter_contexts: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self._available_curves = list(curves)
        if parameter_contexts is None:
            self._available_contexts = [
                {"integration": dict(curve.audit)} for _name, curve in self._available_curves
            ]
        else:
            contexts = list(parameter_contexts)
            if len(contexts) != len(self._available_curves):
                raise ValueError("曲线参数上下文数量必须与曲线数量一致")
            self._available_contexts = contexts
        occurrences={};keys=[]
        for (name,_curve),context in zip(self._available_curves,self._available_contexts):
            base=(str(context.get('source','')),name)
            occurrence=occurrences.get(base,0);occurrences[base]=occurrence+1
            keys.append((*base,occurrence))
        self._available_keys=keys
        if keys:self._curve_visibility={key:self._curve_visibility.get(key,True) for key in keys}
        self._apply_curve_selection()
        if self._curve_selection_dialog is not None:self._curve_selection_dialog.refresh()

    def _choose_visible_curves(self):
        from .curve_selection import CurveSelectionDialog
        if self._curve_selection_dialog is None:
            self._curve_selection_dialog=CurveSelectionDialog(self)
        modal=QtWidgets.QApplication.activeModalWidget()
        parent=modal if modal is not None and modal is not self._curve_selection_dialog else self
        if self._curve_selection_dialog.parentWidget() is not parent:self._curve_selection_dialog.setParent(parent,QtCore.Qt.WindowType.Dialog)
        self._curve_selection_dialog.refresh()
        self._curve_selection_dialog.show();self._curve_selection_dialog.raise_()

    def _choose_curve_style(self):
        from .curve_style import CurveStyleDialog
        if self._curve_style_dialog is None:self._curve_style_dialog=CurveStyleDialog(self)
        modal=QtWidgets.QApplication.activeModalWidget()
        parent=modal if modal is not None and modal is not self._curve_style_dialog else self
        if self._curve_style_dialog.parentWidget() is not parent:self._curve_style_dialog.setParent(parent,QtCore.Qt.WindowType.Dialog)
        self._curve_style_dialog.refresh_curves();self._curve_style_dialog.show();self._curve_style_dialog.raise_()

    def _build_curve_menu(self):
        menu=QtWidgets.QMenu(self)
        menu.addAction('恢复完整视图',self.reset_auto_ranges)
        menu.addAction('设置坐标范围…',self._request_manual_ranges)
        menu.addSeparator()
        menu.addAction('绘图选项…',self._choose_curve_style)
        choose=menu.addAction(self.select_curves_button.text(),self._choose_visible_curves)
        choose.setEnabled(bool(self._available_curves))
        for widget in (self.four_frame_check,self.grid_check):
            action=menu.addAction(widget.text());action.setCheckable(True);action.setChecked(widget.isChecked())
            action.toggled.connect(widget.setChecked);widget.toggled.connect(action.setChecked)
        menu.addSeparator()
        export=menu.addAction('导出结果…',self._choose_unified_export);export.setEnabled(bool(self._curve_items))
        guides=menu.addMenu('辅助线')
        guides.addAction('＋辅助线',lambda:self.add_measurement_guide())
        remove=guides.addAction('删除辅助线',self.delete_selected_measurement_guide);remove.setEnabled(self._selected_measurement_guide is not None)
        clear=guides.addAction('清空辅助线',self.clear_measurement_guides);clear.setEnabled(bool(self._measurement_guides))
        from .i18n import language_manager
        language_manager().apply_all()
        return menu

    def _show_curve_menu(self,position):
        self._show_curve_menu_global(self.plot.viewport().mapToGlobal(position))

    def _show_curve_menu_global(self,position):
        menu=self._build_curve_menu();menu.exec(position);menu.deleteLater()

    def _apply_curve_selection(self):
        selected=[index for index,key in enumerate(self._available_keys) if self._curve_visibility.get(key,True)]
        self._curve_items=[self._available_curves[index] for index in selected]
        self._visible_curve_keys=[self._available_keys[index] for index in selected]
        self._curve_parameter_contexts=[self._available_contexts[index] for index in selected]
        self.select_curves_button.setText(f'显示曲线（{len(selected)}/{len(self._available_curves)}）…')
        self.select_curves_button.setEnabled(bool(self._available_curves))
        if self._curve_items:
            self._populate_axis_options(self._curve_items[0][1].kind)
        self._render_curves()
        if self._curve_style_dialog is not None:self._curve_style_dialog.refresh_curves()

    def _transformed_curves(
        self,
    ) -> tuple[list[tuple[str, IntegrationCurve, np.ndarray, np.ndarray]], tuple[str, str] | None]:
        transformed: list[tuple[str, IntegrationCurve, np.ndarray, np.ndarray]] = []
        axis_labels: tuple[str, str] | None = None
        for name, curve in self._curve_items:
            x_values, x_label = self._display_x(curve)
            y_values, y_label = self._display_y(curve)
            y_values = np.asarray(y_values, dtype=float).copy()
            y_values[np.asarray(curve.count) <= 0] = np.nan
            transformed.append(
                (name, curve, np.asarray(x_values, dtype=float).copy(), y_values)
            )
            if axis_labels is None:
                axis_labels = (x_label, y_label)
        return transformed, axis_labels

    def _render_curves(self) -> None:
        from .curve_style import apply_axes,curve_pen,curve_label,legend_visible,render_legend
        items = self._curve_items
        self.plot.clear()
        self.legend.clear()
        self._install_measurement_items()
        self._hover_marker.hide()
        self._hover_text.hide()
        self._plot_items.clear()
        self._focused_curve_index = None
        self._display_curve_cache=[]
        try:
            transformed, axis_labels = self._transformed_curves()
        except IntegrationError as exc:
            self.set_status(str(exc), error=True)
            return
        self._display_curve_cache=transformed
        show_legend=legend_visible(self)
        apply_axes(self)
        for index, (name, curve, x_values, values) in enumerate(transformed):
            item = self.plot.plot(
                x_values,
                values,
                pen=curve_pen(self,index),
                name=html.escape(curve_label(self,index)) if show_legend else None,
                connect="finite",
            )
            self._plot_items.append(item)
        self.focus_hint.setText('点击图例可突出曲线；再次点击可恢复。' if show_legend else
            f'共 {len(transformed)} 条曲线；请通过“显示曲线”列表筛选，避免图例遮挡。')
        render_legend(self)
        s=self._curve_style
        self.plot.setTitle(html.escape(s['title']),color=s['axis_color'],size=f'{s["label_size"]}pt',bold=s['label_bold'])
        if items and axis_labels is not None:
            curve = items[0][1]
            label_style = {
                "color": s['axis_color'],
                "font-size": f'{s["label_size"]}pt',
                "font-family": s['font'],
                "font-weight": 'bold' if s['label_bold'] else 'normal',
            }
            self._plot_axes["bottom"].setLabel(html.escape(s['x_title'] or axis_labels[0]), **label_style)
            self._plot_axes["left"].setLabel(html.escape(s['y_title'] or axis_labels[1]), **label_style)
            self._apply_view_ranges()
            self.set_status(f"已叠加 {len(items)} 条{curve.kind}预览曲线。")
        self.plot_changed.emit()

    @QtCore.Slot(bool)
    def _set_four_frame(self, enabled: bool) -> None:
        self._curve_style['frame']=bool(enabled)
        self.plot.showAxis("top", show=enabled)
        self.plot.showAxis("right", show=enabled)
        self._plot_axes["top"].setStyle(showValues=False, tickLength=0)
        self._plot_axes["right"].setStyle(showValues=False, tickLength=0)
        from .curve_style import apply_axes
        apply_axes(self)
        self.plot_changed.emit()

    @QtCore.Slot(bool)
    def _set_grid_visible(self, enabled: bool) -> None:
        self._curve_style['grid']=bool(enabled)
        self.plot.showGrid(x=enabled, y=enabled, alpha=0.12)
        from .curve_style import apply_axes
        apply_axes(self)
        self.plot_changed.emit()

    @QtCore.Slot(object)
    def _plot_scene_clicked(self, event: object) -> None:
        if not hasattr(event, "scenePos"):
            return
        scene_position = event.scenePos()
        selected: int | None = None
        for index, (sample, label) in enumerate(self.legend.items):
            if sample.sceneBoundingRect().contains(scene_position) or label.sceneBoundingRect().contains(scene_position):
                selected = index
                break
        if selected is None:
            if self._focused_curve_index is not None and not self.legend.sceneBoundingRect().contains(scene_position):
                self._set_curve_focus(None)
            return
        self._set_curve_focus(
            None if self._focused_curve_index == selected else selected
        )

    def _set_curve_focus(self, index: int | None) -> None:
        from .curve_style import curve_pen
        if index is not None and not (0 <= index < len(self._plot_items)):
            raise IndexError("curve focus index is outside the plotted curves")
        self._focused_curve_index = index
        for item_index, item in enumerate(self._plot_items):
            focused = index is None or item_index == index
            item.setPen(curve_pen(self,item_index,index is not None and focused))
            item.setOpacity(1.0 if focused else 0.18)
        for item_index, (sample, label) in enumerate(self.legend.items):
            opacity = 1.0 if index is None or item_index == index else 0.3
            sample.setOpacity(opacity)
            label.setOpacity(opacity)
        if index is None:
            self.focus_hint.setText("点击图例可突出曲线；再次点击可恢复。")
        else:
            self.focus_hint.setText(
                f"已突出：{self._curve_items[index][0]}（再次点击图例恢复）"
            )

    @staticmethod
    def _json_number(value: float) -> float | None:
        return float(value) if math.isfinite(float(value)) else None

    @staticmethod
    def _split_axis_label(label: str) -> tuple[str, str]:
        """Split a human axis label into Origin Long Name and Units fields."""
        marker = label.rfind(" (")
        if marker > 0 and label.endswith(")"):
            return label[:marker], label[marker + 2 : -1]
        return label, ""

    def _export_axis_metadata(
        self,
        curve: IntegrationCurve,
        axis_labels: tuple[str, str],
    ) -> tuple[str, str, str, str]:
        x_name, x_unit = self._split_axis_label(axis_labels[0])
        y_name, y_unit = self._split_axis_label(axis_labels[1])
        x_mode = str(self.x_display_combo.currentData() or "native")
        y_mode = str(self.y_display_combo.currentData() or "intensity")
        if x_mode == "log10_q_nm_inv":
            x_unit = "1"
        if y_mode in {"log10_intensity", "ln_intensity"}:
            y_unit = "1"
        elif y_mode in {"qI", "q2I", "q4I"}:
            power = {"qI": 1, "q2I": 2, "q4I": 4}[y_mode]
            intensity_unit = {
                "detector_count": "counts",
                "detector_count/s": "counts s⁻¹",
            }.get(curve.intensity_unit, curve.intensity_unit)
            q_unit = "nm⁻¹" if power == 1 else f"nm⁻{power}"
            y_unit = f"{intensity_unit} {q_unit}"
        return x_name, x_unit, y_name, y_unit

    @staticmethod
    def _parameter_path(target: Path) -> Path:
        format_tag = target.suffix.casefold().lstrip(".")
        return target.with_name(
            f"{target.stem}_{format_tag or 'file'}_parameters.json"
        )

    def _export_parameter_document(
        self,
        target: Path,
        *,
        export_type: str,
        axis_labels: tuple[str, str],
    ) -> dict[str, Any]:
        """Build one deduplicated explanation document for one export action."""

        parameter_sets: dict[str, Mapping[str, Any]] = {}
        curve_entries: list[dict[str, Any]] = []
        for index, ((name, curve), context) in enumerate(
            zip(self._curve_items, self._curve_parameter_contexts, strict=True)
        ):
            canonical = json.dumps(
                context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            parameter_id = f"set_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"
            parameter_sets.setdefault(parameter_id, dict(context))
            curve_entries.append(
                {
                    "index": index + 1,
                    "name": name,
                    "kind": curve.kind,
                    "source_x_unit": curve.x_unit,
                    "source_intensity_unit": curve.intensity_unit,
                    "parameter_set": parameter_id,
                }
            )
        return {
            "schema": "saxs_waxs_export_parameters/1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "export_type": export_type,
            "output_file": target.name,
            "output_sha256": (
                hashlib.sha256(target.read_bytes()).hexdigest()
                if target.is_file()
                else None
            ),
            "parameter_organization": (
                "One explanation JSON per export action. Identical parameter sets "
                "are stored once and referenced by all matching curves."
            ),
            "displayed_axes": {
                "x_label": axis_labels[0],
                "y_label": axis_labels[1],
                "x_transform": str(self.x_display_combo.currentData() or "native"),
                "y_transform": str(self.y_display_combo.currentData() or "intensity"),
                "x_range": self._manual_x_range,
                "y_range": self._manual_y_range,
            },
            "curves": curve_entries,
            "parameter_sets": parameter_sets,
        }

    def _write_export_parameters(
        self,
        target: Path,
        *,
        export_type: str,
        axis_labels: tuple[str, str],
    ) -> Path:
        path = self._parameter_path(target)
        document = self._export_parameter_document(
            target, export_type=export_type, axis_labels=axis_labels
        )
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def export_displayed_data(
        self,
        path: str | Path,
        *,
        layout: str = "wide",
    ) -> Path:
        """Export only the currently displayed coordinate transforms."""

        if layout not in {"wide", "long"}:
            raise ValueError("数据表布局必须是 wide 或 long")
        target = Path(path)
        transformed, axis_labels = self._transformed_curves()
        if not transformed or axis_labels is None:
            raise ValueError("当前没有可导出的预览曲线")
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = target.suffix.casefold()
        if suffix == ".xlsx":
            from saxs_waxs_core.workbook import write_workbook
            groups,records,contexts=[],[],[]
            for name,curve,x_values,y_values in transformed:
                groups.append((name,curve.x,curve.intensity,"原始横坐标","原始强度",curve.x_unit,curve.intensity_unit))
                xname,xunit,yname,yunit=self._export_axis_metadata(curve,axis_labels)
                if not (np.array_equal(curve.x,x_values,equal_nan=True) and np.array_equal(curve.intensity,y_values,equal_nan=True)):
                    groups.append((name+" · 显示坐标",x_values,y_values,xname,yname,xunit,yunit))
                records.append({"样品":name,"积分类型":curve.kind,"点数":len(curve.x),
                    "横坐标单位":curve.x_unit,"强度单位":curve.intensity_unit})
                contexts.append((name,dict(curve.audit)))
            return write_workbook(target,groups,records,contexts)
        if suffix in {".csv", ".tsv"}:
            delimiter = "," if suffix == ".csv" else "\t"
            with target.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle, delimiter=delimiter)
                if layout == "wide":
                    long_names: list[str] = []
                    units: list[str] = []
                    for name, curve, _x_values, _y_values in transformed:
                        x_name, x_unit, y_name, y_unit = self._export_axis_metadata(
                            curve, axis_labels
                        )
                        long_names.extend(
                            [f"{name} | {x_name} (X)", f"{name} | {y_name} (Y)"]
                        )
                        units.extend([x_unit, y_unit])
                    writer.writerow(long_names)
                    writer.writerow(units)
                    max_points = max(
                        max(len(x_values), len(y_values))
                        for _name, _curve, x_values, y_values in transformed
                    )
                    for point in range(max_points):
                        row: list[float | None] = []
                        for _name, _curve, x_values, y_values in transformed:
                            row.extend(
                                [
                                    self._json_number(float(x_values[point]))
                                    if point < len(x_values)
                                    else None,
                                    self._json_number(float(y_values[point]))
                                    if point < len(y_values)
                                    else None,
                                ]
                            )
                        writer.writerow(row)
                else:
                    writer.writerow(
                        [
                            "Curve",
                            "Point",
                            "X",
                            "Y",
                            "X Long Name",
                            "X Unit",
                            "Y Long Name",
                            "Y Unit",
                        ]
                    )
                    for name, curve, x_values, y_values in transformed:
                        x_name, x_unit, y_name, y_unit = self._export_axis_metadata(
                            curve, axis_labels
                        )
                        point_count = max(len(x_values), len(y_values))
                        for point in range(point_count):
                            writer.writerow(
                                [
                                    name,
                                    point + 1,
                                    self._json_number(float(x_values[point]))
                                    if point < len(x_values)
                                    else None,
                                    self._json_number(float(y_values[point]))
                                    if point < len(y_values)
                                    else None,
                                    x_name,
                                    x_unit,
                                    y_name,
                                    y_unit,
                                ]
                            )
        elif suffix == ".json":
            metadata = [
                self._export_axis_metadata(curve, axis_labels)
                for _name, curve, _x_values, _y_values in transformed
            ]
            payload = {
                "schema": "saxs_waxs_displayed_curve_export/2",
                "note": "display coordinates; raw integration arrays remain in the processing result",
                "x_label": axis_labels[0],
                "y_label": axis_labels[1],
                "curves": [
                    {
                        "name": name,
                        "x_long_name": axis_meta[0],
                        "x_unit": axis_meta[1],
                        "x_designation": "X",
                        "y_long_name": axis_meta[2],
                        "y_unit": axis_meta[3],
                        "y_designation": "Y",
                        "x": [self._json_number(value) for value in x_values],
                        "y": [self._json_number(value) for value in y_values],
                        "count": [int(value) for value in np.asarray(curve.count)],
                        "source_x_unit": curve.x_unit,
                        "source_intensity_unit": curve.intensity_unit,
                    }
                    for (name, curve, x_values, y_values), axis_meta in zip(
                        transformed, metadata, strict=True
                    )
                ],
            }
            payload["export_parameters"] = self._export_parameter_document(
                target, export_type="curve_data", axis_labels=axis_labels
            )
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        elif suffix == ".npz":
            arrays: dict[str, np.ndarray] = {}
            meta_curves = []
            for index, (name, curve, x_values, y_values) in enumerate(transformed):
                key = f"curve_{index:03d}"
                arrays[f"{key}_x"] = x_values
                arrays[f"{key}_y"] = y_values
                arrays[f"{key}_count"] = np.asarray(curve.count)
                x_name, x_unit, y_name, y_unit = self._export_axis_metadata(
                    curve, axis_labels
                )
                meta_curves.append(
                    {
                        "key": key,
                        "name": name,
                        "x_long_name": x_name,
                        "x_unit": x_unit,
                        "x_designation": "X",
                        "y_long_name": y_name,
                        "y_unit": y_unit,
                        "y_designation": "Y",
                    }
                )
            arrays["metadata_json"] = np.asarray(
                json.dumps(
                    {
                        "schema": "saxs_waxs_displayed_curve_export/2",
                        "x_label": axis_labels[0],
                        "y_label": axis_labels[1],
                        "curves": meta_curves,
                    },
                    ensure_ascii=False,
                )
            )
            np.savez_compressed(target, **arrays)
        else:
            raise ValueError("数据导出支持 .csv、.tsv、.json 或 .npz")
        if suffix != ".json":
            self._write_export_parameters(
                target, export_type="curve_data", axis_labels=axis_labels
            )
        return target

    def _publication_matplotlib_style(self=None) -> dict[str, object]:
        # Analysis dialogs also call this shared helper without a panel instance.
        from .curve_style import DEFAULTS
        s=self._curve_style if self is not None else DEFAULTS
        return {
            "font.family": [s['font'], "Microsoft YaHei", "DejaVu Sans"],
            "font.sans-serif": [
                s['font'],
                "Microsoft YaHei",
                "DejaVu Sans",
            ],
            "font.size": s['tick_size'],
            "font.weight": 'bold' if s['tick_bold'] else 'normal',
            "axes.labelsize": s['label_size'],
            "axes.labelweight": 'bold' if s['label_bold'] else 'normal',
            "axes.linewidth": s['axis_width'],
            "axes.edgecolor": s['axis_color'],
            "axes.labelcolor": s['axis_color'],
            "xtick.color": s['axis_color'],
            "ytick.color": s['axis_color'],
            "xtick.labelsize": s['tick_size'],
            "ytick.labelsize": s['tick_size'],
            "legend.fontsize": s['legend_size'],
            "legend.frameon": s['legend_frame'],
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "savefig.facecolor": "white",
            "figure.facecolor": "white",
        }

    def _create_publication_figure(self, *, width_px: int):
        from .curve_render import snapshot_plot,render_plot
        dpi=max(150,round(int(width_px)/(180./25.4)))
        return render_plot(snapshot_plot(self),180.,130.,dpi)

    def export_plot_image(self, path: str | Path, *, width_px: int = 4252) -> Path:
        target = Path(path)
        if not self._plot_items:
            raise ValueError("当前没有可导出的预览图")
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = target.suffix.casefold()
        if suffix not in {".png", ".tif", ".tiff", ".svg", ".pdf"}:
            raise ValueError("图片导出支持 .png、.tif、.tiff、.svg 或 .pdf")
        figure, _axis, dpi = self._create_publication_figure(width_px=width_px)
        try:
            save_kwargs: dict[str, object] = {"dpi": dpi}
            if suffix in {".tif", ".tiff"}:
                save_kwargs["pil_kwargs"] = {"compression": "tiff_lzw"}
            from .rendering import publication_render_context

            with publication_render_context(self._publication_matplotlib_style()):
                figure.savefig(target, **save_kwargs)
        finally:
            figure.clear()
        _transformed, axis_labels = self._transformed_curves()
        if axis_labels is None:
            raise ValueError("当前没有可记录的预览曲线参数")
        self._write_export_parameters(
            target, export_type="curve_plot_image", axis_labels=axis_labels
        )
        return target

    def _suggested_export_stem(self) -> str:
        """Build a short, traceable export name from the displayed source curves."""
        if not self._curve_items:
            return "integration"
        source_stem = Path(str(self._curve_items[0][0])).stem or "integration"
        curve_kinds = {curve.kind for _name, curve in self._curve_items}
        if curve_kinds == {"azimuthal_1d"}:
            integration_suffix = "azimuthal_integration"
        elif curve_kinds == {"radial_1d"}:
            integration_suffix = "radial_integration"
        else:
            integration_suffix = "mixed_integration"
        if len(self._curve_items) == 1:
            return f"{source_stem}_{integration_suffix}"
        return (
            f"{source_stem}_plus_{len(self._curve_items) - 1}_"
            f"{integration_suffix}"
        )

    @QtCore.Slot()
    def _choose_unified_export(self):
        from .curve_export_dialog import CurveExportDialog
        dialog=CurveExportDialog(self)
        dialog.exec()

    def _choose_export_data(self) -> None:
        selected, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出当前显示曲线数据",
            f"{self._suggested_export_stem()}.xlsx",
            "Excel 工作簿 (*.xlsx);;CSV (*.csv);;TSV (*.tsv)",
        )
        if not selected:
            return
        try:
            saved = self.export_displayed_data(
                selected,
                layout=str(self.data_layout_combo.currentData() or "wide"),
            )
            self.set_status(f"已导出当前显示曲线数据：{saved.name}")
        except (OSError, ValueError, IntegrationError) as exc:
            self.set_status(f"导出数据失败：{exc}", error=True)

    @QtCore.Slot()
    def _choose_export_image(self) -> None:
        selected, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出当前预览图",
            f"{self._suggested_export_stem()}.png",
            "PNG (*.png);;TIFF (*.tif *.tiff);;SVG (*.svg);;PDF (*.pdf)",
        )
        if not selected:
            return
        try:
            saved = self.export_plot_image(selected)
            self.set_status(f"已导出当前预览图：{saved.name}")
        except (OSError, ValueError) as exc:
            self.set_status(f"导出图片失败：{exc}", error=True)
