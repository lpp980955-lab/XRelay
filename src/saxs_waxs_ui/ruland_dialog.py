"""Candidate Ruland single-image workspace."""
from .export_naming import choose_export_folder
from dataclasses import replace
import json
from pathlib import Path
import threading
import re
from types import SimpleNamespace
import numpy as np
import pyqtgraph as pg
from openpyxl import Workbook
from PySide6 import QtCore, QtWidgets
from saxs_waxs_core.ruland import check_ruland_range_sensitivity
from saxs_waxs_core.ruland_integration import DEFAULT_RULAND_Q_POINTS, ruland_q_grid
from saxs_waxs_core.ruland_series import refit_ruland_series
from .widgets import scrollable_content


class _BatchSignals(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal(object)


class _SingleSignals(QtCore.QObject):
    finished = QtCore.Signal(object, object)


class _PeakSuggestionSignals(QtCore.QObject):
    finished = QtCore.Signal(object, object, object)


class RulandDialog(QtWidgets.QDialog):
    def __init__(self, run_analysis, parent=None, batch_sources=(), run_batch_item=None,
                 initial_sampling=None, suggest_peaks=None, auto_batch_angle=False):
        super().__init__(parent)
        self.setWindowTitle("Ruland 多 q 分析（候选）")
        self.resize(1120, 720)
        self.run_analysis = run_analysis
        self.batch_sources = tuple(batch_sources)
        self.run_batch_item = run_batch_item
        self.suggest_peaks = suggest_peaks
        self.batch_results = []
        self.batch_settings = None
        self.batch_sampling_source = None
        self.batch_sampling_details = None
        self.batch_angle_source = None
        self.batch_active = False
        self.image_export_active = False
        self.image_signals = _BatchSignals(self)
        self.image_signals.progress.connect(self._image_progress)
        self.image_signals.finished.connect(self._image_finished)
        self.batch_stop = threading.Event()
        self.batch_thread = None
        self.single_active = False
        self.single_thread = None
        self.peak_suggestion_active = False
        self.peak_suggestion_thread = None
        self._angle_manually_changed = False
        self._applying_suggestion = False
        self.angle_source = "default_candidate"
        self.peak_suggestion_signals = _PeakSuggestionSignals(self)
        self.peak_suggestion_signals.finished.connect(self._peak_suggestion_finished)
        self.single_signals = _SingleSignals(self)
        self.single_signals.finished.connect(self._single_finished)
        self.batch_signals = _BatchSignals(self)
        self.batch_signals.progress.connect(self._batch_progress)
        self.batch_signals.finished.connect(self._batch_finished)
        self.result = None
        self.range_checks = ()
        self.initial_sampling = initial_sampling
        self.q_min, self.q_max, self.q_width = (QtWidgets.QDoubleSpinBox() for _ in range(3))
        for spin in (self.q_min, self.q_max, self.q_width):
            spin.setRange(0, 1e6)
            spin.setDecimals(8)
        self.q_min.setValue(.1)
        self.q_max.setValue(.3)
        self._default_q_width = (.3 - .1) / (DEFAULT_RULAND_Q_POINTS - 1) * .8
        self.q_width.setValue(self._default_q_width)
        self.q_count = QtWidgets.QSpinBox()
        self.q_count.setRange(3, 1000)
        self.q_count.setValue(DEFAULT_RULAND_Q_POINTS)
        self.q_count.setToolTip("默认24点；可调整。环带宽度若大于相邻q点距，结果会共享部分像素。")
        if initial_sampling is not None:
            q_low, q_high, q_width, source = initial_sampling
            self.q_min.setValue(q_low)
            self.q_max.setValue(q_high)
            self.q_width.setValue(q_width)
            self.setToolTip(f"q 初值来源：{source}；只是可见像素范围候选，需检查信号、遮挡和拟合稳定性。")
        self.angle, self.half_window = QtWidgets.QDoubleSpinBox(), QtWidgets.QDoubleSpinBox()
        self.q_step = QtWidgets.QDoubleSpinBox()
        self.q_step.setDecimals(12)
        self.q_step.setRange(0, 1e6)
        self.q_step.setValue((self.q_max.value()-self.q_min.value())/(self.q_count.value()-1))
        self.q_sampling = QtWidgets.QComboBox()
        self.q_sampling.addItem("按步长", "step")
        self.q_sampling.addItem("按点数", "count")
        self.q_sampling.setCurrentIndex(1)
        self.q_step.setToolTip("按点数时，步长由 (q 终点 − q 起点) ÷ (点数 − 1) 自动计算。切换到按步长可直接输入。")
        self.angle.setRange(0, 360)
        self.half_window.setRange(.1, 89.9)
        self.angle.setValue(90)
        self.half_window.setValue(25)
        self.half_window.setToolTip("以峰中心为基准，左右各取此角度拟合。峰宽由拟合结果给出。")
        self.peak_candidates = QtWidgets.QComboBox()
        self.peak_candidates.setVisible(False)
        self.peak_candidates.currentIndexChanged.connect(self._choose_peak_candidate)
        self.suggest_peak_button = QtWidgets.QPushButton("更新峰中心建议")
        self.suggest_peak_button.setVisible(suggest_peaks is not None)
        self.suggest_peak_button.clicked.connect(self.start_peak_suggestion)
        self.model = QtWidgets.QComboBox()
        self.model.addItem("Gaussian–Gaussian", "gaussian")
        self.model.addItem("Lorentzian / Cauchy–Cauchy", "cauchy")
        self.width = QtWidgets.QComboBox()
        self.width.addItem("请选择峰宽定义", None)
        self.width.addItem("FWHM（峰高一半处的全宽，默认）", "FWHM")
        self.width.addItem("HWHM（半高半宽）", "HWHM")
        self.width.addItem("积分宽度（Pauw 2010）", "integral_breadth")
        self.width.setCurrentIndex(self.width.findData("FWHM"))
        self.width.setToolTip("FWHM/HWHM使用Liu候选公式；积分宽度使用Pauw公式与相应系数。"
                              "请核查扣底、覆盖范围和文献定义。")
        self.run_button = QtWidgets.QPushButton("生成环带并拟合当前样品")
        self.batch_button = QtWidgets.QPushButton(f"批量处理左侧勾选样品（{len(self.batch_sources)}）")
        self.batch_button.setEnabled(bool(self.batch_sources and self.run_batch_item))
        self.batch_auto_angle = QtWidgets.QCheckBox("批量逐样品拟合峰中心")
        self.batch_auto_angle.setChecked(bool(auto_batch_angle))
        self.batch_auto_angle.setVisible(bool(self.batch_sources and self.run_batch_item))
        self.batch_auto_angle.setToolTip("逐样品拟合峰中心。上方角度只用于匹配同侧候选。"
                                        "仅作候选，失败会保留失败状态。取消勾选则整批使用上方固定角度。")
        self.label = QtWidgets.QLabel("先确认 q 范围、峰中心、模型及 B 的定义；不同文献的峰宽与公式系数可能不同。")
        self.label.setWordWrap(True)
        self.sampling_note = QtWidgets.QLabel(
            "q 初值取自当前积分范围" if initial_sampling is not None and
            initial_sampling[3] == "integration_panel_with_detector_support" else
            "q 初值取自有效探测像素的 2%–90% 分位；当前积分范围像素不足，未沿用" if
            initial_sampling is not None and "preferred_unsupported" in initial_sampling[3] else
            "q 初值从径向主峰峰顶开始，延伸至强度趋稳的尾部" if initial_sampling is not None and
            initial_sampling[3] == "radial_profile_peak_to_tail" else
            "q 初值取自有效探测像素的 2%–90% 分位" if initial_sampling is not None else
            "q 为默认候选值")
        self.sampling_note.setWordWrap(True)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["参与", "q (nm⁻¹)", "范围 (nm⁻¹)", "峰宽 (°)", "状态", "峰拟合 R²"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.ruland_plot = pg.PlotWidget(background="w")
        self.ruland_plot.showGrid(x=False, y=False)
        self.ruland_plot.setMinimumHeight(180)
        self.azimuth_plot = pg.PlotWidget(background="w")

        self.azimuth_plot.setLabel("bottom", "方位角", units="°")
        self.plot_tabs = QtWidgets.QTabWidget()
        self.plot_tabs.addTab(self.azimuth_plot, "多 q 方位角与拟合")
        self.plot_tabs.addTab(self.ruland_plot, "Ruland 散点与线性拟合")
        self.export_button = QtWidgets.QPushButton("导出结果…")
        self.export_image_button = QtWidgets.QPushButton("导出 Ruland 图…")
        self.export_batch_images_button = QtWidgets.QPushButton("批量导出 Ruland 图…")
        self.range_check_button = QtWidgets.QPushButton("检查 q 范围敏感性…")
        layout = QtWidgets.QVBoxLayout(self)
        workspace = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        workspace.setChildrenCollapsible(False)
        controls = QtWidgets.QWidget()
        control_layout = QtWidgets.QVBoxLayout(controls)
        control_layout.setContentsMargins(6, 6, 6, 6)
        control_scroll = scrollable_content(controls)
        control_scroll.setMinimumWidth(300)
        workspace.addWidget(control_scroll)
        form = QtWidgets.QFormLayout()
        self._sampling_form = form
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        for label, widget in (("q 起点 (nm⁻¹)", self.q_min), ("q 终点 (nm⁻¹)", self.q_max),
                              ("q 采样方式", self.q_sampling), ("q 步长 (nm⁻¹)", self.q_step),
                              ("环带总宽 (nm⁻¹)", self.q_width), ("q 点数", self.q_count),
                              ("峰中心 (°)", self.angle), ("拟合窗口 ± (°)", self.half_window),
                              ("候选公式", self.model), ("B 的宽度定义", self.width)):
            form.addRow(label, widget)
        form.addRow("可见峰候选", self.peak_candidates)
        self.formula_label = QtWidgets.QLabel()
        self.formula_label.setWordWrap(True)
        control_layout.addWidget(self.formula_label)
        self.model.currentIndexChanged.connect(self._update_formula_label)
        self.width.currentIndexChanged.connect(self._update_formula_label)
        self._update_formula_label()
        control_layout.addLayout(form)
        control_layout.addWidget(self.sampling_note)
        self.overlap_note = QtWidgets.QLabel()
        self.overlap_note.setWordWrap(True)
        self.overlap_note.setStyleSheet("color: #9a6700;")
        control_layout.addWidget(self.overlap_note)
        control_layout.addWidget(self.suggest_peak_button)
        control_layout.addWidget(self.run_button)
        control_layout.addWidget(self.batch_auto_angle)
        control_layout.addWidget(self.batch_button)
        control_layout.addWidget(self.label)
        control_layout.addWidget(self.range_check_button)
        control_layout.addWidget(self.export_button)
        control_layout.addWidget(self.export_image_button)
        control_layout.addWidget(self.export_batch_images_button)
        self.export_image_button.hide()
        self.export_batch_images_button.hide()
        control_layout.addStretch(1)
        views = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.fit_q_range = None
        self.fit_region = None
        self.fit_range_button = QtWidgets.QPushButton("应用散点图选区并线性拟合")
        self.fit_range_button.clicked.connect(self._apply_fit_region)
        self.fit_range_button.setEnabled(False)
        views.addWidget(self.fit_range_button)
        self.preview_ranges = {}
        self.preview_format = {}
        self.preview_frame = QtWidgets.QCheckBox("四边框")
        self.preview_grid = QtWidgets.QCheckBox("网格")
        self.preview_legend = QtWidgets.QCheckBox("图例")
        self.preview_legend.setChecked(True)
        preview_auto = QtWidgets.QPushButton("自动范围")
        preview_manual = QtWidgets.QPushButton("设置范围…")
        preview_auto.clicked.connect(self._reset_preview_range)
        preview_manual.clicked.connect(self._edit_preview_range)
        format_row = QtWidgets.QHBoxLayout()
        for widget in (self.preview_frame, self.preview_grid, self.preview_legend,
                       preview_auto, preview_manual):
            format_row.addWidget(widget)
        plot_container = QtWidgets.QWidget()
        plot_layout = QtWidgets.QVBoxLayout(plot_container)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.addLayout(format_row)
        plot_row = QtWidgets.QHBoxLayout()
        plot_row.addWidget(self.plot_tabs, 1)
        self.q_legend = QtWidgets.QListWidget()
        self.q_legend.setMaximumWidth(155)
        self.q_legend.setToolTip("q 图例 (nm⁻¹) · 两张图使用相同颜色")
        plot_row.addWidget(self.q_legend)
        plot_layout.addLayout(plot_row, 1)
        views.addWidget(plot_container)
        for widget in (self.preview_frame, self.preview_grid, self.preview_legend):
            widget.toggled.connect(self._change_preview_format)
        self.plot_tabs.currentChanged.connect(self._restore_preview_format)
        views.addWidget(self.table)
        views.setSizes([450, 230])
        workspace.addWidget(views)
        workspace.setStretchFactor(0, 0)
        workspace.setStretchFactor(1, 1)
        workspace.setSizes([390, 710])
        layout.addWidget(workspace, 1)
        self.run_button.clicked.connect(self.start_single)
        self.batch_button.clicked.connect(self.start_batch)
        self.export_button.clicked.connect(self.export_dialog)
        self.export_image_button.clicked.connect(self.export_image_dialog)
        self.export_batch_images_button.clicked.connect(self.export_batch_images_dialog)
        self.range_check_button.clicked.connect(self.show_range_sensitivity)
        self.table.itemChanged.connect(self._point_selection_changed)
        self.q_step.valueChanged.connect(lambda: self.q_sampling.setCurrentIndex(0))
        self.q_count.valueChanged.connect(lambda: self.q_sampling.setCurrentIndex(1))
        for widget in (self.q_step, self.q_sampling):
            (widget.valueChanged if widget is self.q_step else widget.currentIndexChanged).connect(self.clear_result)
            (widget.valueChanged if widget is self.q_step else widget.currentIndexChanged).connect(self._update_overlap_note)
        self.q_sampling.currentIndexChanged.connect(self._sampling_visibility)
        self._sampling_visibility()
        for widget in (self.q_min, self.q_max, self.q_width, self.q_count, self.angle, self.half_window):
            widget.valueChanged.connect(self.clear_result)
        self.q_min.valueChanged.connect(self._peak_q_changed)
        self.q_max.valueChanged.connect(self._peak_q_changed)
        self.angle.valueChanged.connect(self._angle_changed)
        self.half_window.valueChanged.connect(self._angle_changed)
        self.model.currentIndexChanged.connect(lambda: self.preview_ranges.pop(self.ruland_plot, None))
        self.model.currentIndexChanged.connect(self.clear_result)
        self.width.currentIndexChanged.connect(self.clear_result)
        for widget in (self.q_min, self.q_max, self.q_width, self.q_count):
            widget.valueChanged.connect(self._update_overlap_note)
        self._update_overlap_note()
        self._previous_spacing = self.q_step.value()
        for widget in (self.q_min, self.q_max, self.q_count, self.q_step):
            widget.valueChanged.connect(self._scale_ring_width)
        self.q_sampling.currentIndexChanged.connect(self._scale_ring_width)
        if suggest_peaks is not None:
            QtCore.QTimer.singleShot(0, self.start_peak_suggestion)

    def project_defaults(self):
        return {
            "sampling_revision": 4,
            "q_min": self.q_min.value(), "q_max": self.q_max.value(),
            "q_step": self.q_step.value(), "q_width": self.q_width.value(),
            "q_count": self.q_count.value(), "angle": self.angle.value(),
            "half_window": self.half_window.value(), "sampling": self.q_sampling.currentData(),
            "model": self.model.currentData(), "width": self.width.currentData(),
            "fit_q_range": self.fit_q_range,
        }

    def restore_project_defaults(self, defaults):
        for name in ("q_min", "q_max", "q_step", "q_width", "q_count", "angle", "half_window"):
            if name in defaults:
                getattr(self, name).setValue(defaults[name])
        for name, widget in (("sampling", self.q_sampling), ("model", self.model), ("width", self.width)):
            index = widget.findData(defaults.get(name))
            if index >= 0:
                widget.setCurrentIndex(index)
        if "q_width" in defaults:
            self.q_width.setValue(defaults["q_width"])
        self._angle_manually_changed = True
        fit_range = defaults.get("fit_q_range")
        if fit_range is not None:
            self.fit_q_range = tuple(fit_range)
        self.angle_source = "project_defaults"
        self.sampling_note.setText("已沿用项目参数，可手动调整或重新选择峰候选。")

    def _scale_ring_width(self, *_):
        spacing = (self.q_max.value() - self.q_min.value()) / (self.q_count.value() - 1)
        if self.q_sampling.currentData() == "step":
            spacing = self.q_step.value()
        else:
            with QtCore.QSignalBlocker(self.q_step):
                self.q_step.setValue(max(0., spacing))
        if spacing > 0 and self._previous_spacing > 0:
            ratio = min(1.0, self.q_width.value() / self._previous_spacing)
            # Round down to the spin box resolution so rounding cannot overlap rings.
            self.q_width.setValue(np.floor(spacing * ratio * 1e8) / 1e8)
            self._previous_spacing = spacing

    def _update_overlap_note(self, *_):
        spacing = (self.q_max.value() - self.q_min.value()) / (self.q_count.value() - 1) if self.q_sampling.currentData() == "count" else self.q_step.value()
        overlap = spacing > 0 and self.q_width.value() > spacing
        self.overlap_note.setText(
            f"环带宽度 {self.q_width.value():.6g} 大于 q 点距 {spacing:.6g} nm⁻¹；"
            "相邻结果共享像素，增加点数不等于增加独立信息。" if overlap else "")
        self.overlap_note.setVisible(overlap)

    def _sampling_visibility(self, *_):
        self.q_step.setEnabled(True)
        self.q_step.setReadOnly(self.q_sampling.currentData() == "count")
        self.q_count.setEnabled(self.q_sampling.currentData() == "count")
        self._sampling_form.setRowVisible(self.q_step, True)
        self._sampling_form.setRowVisible(self.q_count, self.q_sampling.currentData() == "count")

    def _update_formula_label(self, *_):
        gaussian = self.model.currentData() == "gaussian"
        integral = self.width.currentData() == "integral_breadth"
        text = ("Gaussian：B² 对 1/q²；Bψ=√截距；Lf="
                + ("2π/√斜率" if integral else "1/√斜率")) if gaussian else (
                "Lorentzian：B 对 1/q；Bψ=截距；Lf=2π/斜率。")
        self.formula_label.setText(text + " B以弧度计算；同一q序列统一峰型和峰宽定义。")

    def _q_centers(self):
        if self.q_sampling.currentData() == "count":
            return np.linspace(self.q_min.value(), self.q_max.value(), self.q_count.value())
        return ruland_q_grid(self.q_min.value(), self.q_max.value(), self.q_step.value())

    def _sampling_details(self):
        return {"source": self._sampling_source(), "mode": self.q_sampling.currentData(),
            "linear_fit_q_range_nm_inv": self.fit_q_range,
            "requested_start_nm_inv": self.q_min.value(), "requested_stop_nm_inv": self.q_max.value(),
            "step_nm_inv": self.q_step.value()}

    def _angle_changed(self, *_):
        if not self._applying_suggestion:
            self._angle_manually_changed = True
            self.angle_source = "manual_override"

    def _peak_q_changed(self, *_):
        if self.peak_candidates.count():
            with QtCore.QSignalBlocker(self.peak_candidates):
                self.peak_candidates.clear()
            self.peak_candidates.setVisible(False)
            if self.angle_source.startswith("visible_peak_candidate"):
                self.angle_source = "candidate_from_prior_q_range"
            self.sampling_note.setText("q 范围已更改；可更新峰中心建议。")

    def _choose_peak_candidate(self, index):
        if index >= 0 and self.peak_candidates.itemData(index) is not None:
            self._apply_peak_candidate(self.peak_candidates.itemData(index))
            self.angle_source = "visible_peak_candidate_selected"

    def _apply_peak_candidate(self, candidate):
        self._applying_suggestion = True
        try:
            if isinstance(candidate, (tuple, list)):
                center, half_window = candidate
                self.half_window.setValue(half_window)
            else:
                center = candidate
            self.angle.setValue(center)
        finally:
            self._applying_suggestion = False

    def start_peak_suggestion(self):
        if self.suggest_peaks is None or self.peak_suggestion_active or self.single_active or self.batch_active:
            return
        q_range = (self.q_min.value(), self.q_max.value())
        if q_range[0] <= 0 or q_range[1] <= q_range[0]:
            self.sampling_note.setText("先设置有效的 q 范围，再建议峰中心。")
            return
        self.peak_suggestion_active = True
        self.suggest_peak_button.setEnabled(False)
        self.sampling_note.setText("正在后台寻找可见方位角峰候选…")
        self.peak_suggestion_thread = threading.Thread(target=self._run_peak_suggestion,
            args=(self.suggest_peaks, q_range, self.peak_suggestion_signals),
            name="ruland-peak-suggestion", daemon=True)
        self.peak_suggestion_thread.start()

    @staticmethod
    def _run_peak_suggestion(runner, q_range, signals):
        try:
            candidates, error = tuple(runner(q_range)), None
        except Exception as exc:
            candidates, error = (), f"{type(exc).__name__}: {exc}"
        signals.finished.emit(candidates, error, q_range)

    @QtCore.Slot(object, object, object)
    def _peak_suggestion_finished(self, candidates, error, q_range):
        self.peak_suggestion_active = False
        self.peak_suggestion_thread = None
        self.suggest_peak_button.setEnabled(True)
        if q_range != (self.q_min.value(), self.q_max.value()):
            self.sampling_note.setText("q 范围已更改；可更新峰中心建议。")
            return
        if error or not candidates:
            self.sampling_note.setText(f"自动建议未得到可用峰；请手动输入峰中心。{error or ''}")
            return
        with QtCore.QSignalBlocker(self.peak_candidates):
            self.peak_candidates.clear()
            for rank, candidate in enumerate(candidates, 1):
                description = (f"{candidate[0]:.2f}° ± {candidate[1]:.2f}°"
                               if isinstance(candidate, (tuple, list)) else f"{candidate:.2f}°")
                self.peak_candidates.addItem(f"候选 {rank} · {description}", candidate)
        self.peak_candidates.setVisible(True)
        if not self._angle_manually_changed:
            self._apply_peak_candidate(candidates[0])
            self.angle_source = "visible_peak_candidate_auto"
        else:
            with QtCore.QSignalBlocker(self.peak_candidates):
                self.peak_candidates.setCurrentIndex(-1)
        self.sampling_note.setText("请检查候选峰的中心、半范围和遮挡。两个值均可手动调整。")

    def clear_result(self, *_):
        self.fit_q_range = None
        self.fit_region = None
        self.fit_range_button.setEnabled(False)
        self.fit_range_button.setText("应用散点图选区并线性拟合")
        self.result = None
        self.q_legend.clear()
        self.range_checks = ()
        self.batch_results = []
        self.ruland_plot.clear()
        self.azimuth_plot.clear()
        self.table.setRowCount(0)
        self.label.setText("参数已更改，请重新计算。")

    def _point_selection_changed(self, item):
        if item.column() == 0 and self.result is not None:
            self.range_checks = ()
            selected = tuple(self.table.item(i, 0).checkState() == QtCore.Qt.CheckState.Checked
                             for i in range(self.table.rowCount()))
            self._plot_result(pending_selection=selected)
            self.label.setText("q 点选择已更改。可重新计算，或拖动图中选区后应用线性拟合。")

    def _sampling_error(self):
        try:
            self._q_centers()
        except ValueError as exc:
            return str(exc)
        if self.width.currentData() is None:
            return "请先选定 B 的峰宽定义。"
        q_min, q_max, q_width = self.q_min.value(), self.q_max.value(), self.q_width.value()
        if q_min >= q_max:
            return "q 起点必须小于终点。"
        if q_width <= 0:
            return "环带总宽必须大于零。"
        if q_min - q_width / 2 <= 0:
            return "最小 q 环带的下限必须大于零；请提高 q 起点或缩小环带宽度。"
        if q_width > self._previous_spacing + 1e-12:
            return "环带总宽不能超过 q 步长，请缩小环带宽度。"
        return None

    def calculate(self):
        """Synchronous calculation for explicit programmatic use and validation."""
        error = self._sampling_error()
        if error is not None:
            self.label.setText(error)
            return
        selected = None
        if self.table.rowCount() == len(self._q_centers()):
            selected = [self.table.item(i, 0).checkState() == QtCore.Qt.CheckState.Checked
                        for i in range(self.table.rowCount())]
        fit_range = self.fit_q_range
        self.clear_result()
        self.fit_q_range = fit_range
        error = self._sampling_error()
        if error is not None:
            self.label.setText(error)
            return
        try:
            self.result = self.run_analysis(
                q_centers=self._q_centers(),
                q_width=self.q_width.value(), peak_center_deg=self.angle.value(),
                half_window_deg=self.half_window.value(), model=self.model.currentData(),
                width_definition=self.width.currentData(), selected=selected,
                **({"fit_q_range": fit_range} if fit_range is not None else {}))
        except (ValueError, OSError) as exc:
            self.label.setText(f"分析失败：{exc}")
            return
        self._display_result()

    def start_single(self):
        """Run an interactive single-image analysis without blocking the GUI."""
        if self.single_active or self.batch_active:
            return
        if self.peak_suggestion_active:
            self.label.setText("峰中心建议仍在计算；完成后可运行分析。")
            return
        error = self._sampling_error()
        if error is not None:
            self.label.setText(error)
            return
        selected = None
        if self.table.rowCount() == len(self._q_centers()):
            selected = tuple(self.table.item(i, 0).checkState() == QtCore.Qt.CheckState.Checked
                             for i in range(self.table.rowCount()))
        fit_range = self.fit_q_range
        self.clear_result()
        self.fit_q_range = fit_range
        error = self._sampling_error()
        if error is not None:
            self.label.setText(error)
            return
        settings = dict(q_centers=self._q_centers(),
            q_width=self.q_width.value(), peak_center_deg=self.angle.value(),
            half_window_deg=self.half_window.value(), model=self.model.currentData(),
            width_definition=self.width.currentData(), selected=selected)
        if fit_range is not None:
            settings["fit_q_range"] = fit_range
        self.single_active = True
        self._single_controls = (self.q_min, self.q_max, self.q_width, self.q_count, self.q_step, self.q_sampling, self.angle,
            self.half_window, self.model, self.width, self.run_button, self.batch_button,
            self.batch_auto_angle,
            self.export_button, self.export_image_button, self.export_batch_images_button,
            self.range_check_button, self.table)
        for widget in self._single_controls:
            widget.setEnabled(False)
        self.label.setText("正在后台生成环带并拟合当前样品…")
        self.single_thread = threading.Thread(target=self._run_single_worker,
            args=(self.run_analysis, settings, self.single_signals), name="ruland-single", daemon=True)
        self.single_thread.start()

    @staticmethod
    def _run_single_worker(runner, settings, signals):
        try:
            result, error = runner(**settings), None
        except Exception as exc:
            result, error = None, f"{type(exc).__name__}: {exc}"
        signals.finished.emit(result, error)

    @QtCore.Slot(object, object)
    def _single_finished(self, result, error):
        self.single_active = False
        self.single_thread = None
        for widget in self._single_controls:
            widget.setEnabled(True)
        self._sampling_visibility()
        if error is not None:
            self.label.setText(f"分析失败：{error}")
            return
        self.result = result
        self._display_result()

    def _display_result(self):
        result = self.result
        with QtCore.QSignalBlocker(self.table):
            self.table.setRowCount(len(result.rings))
            by_q = {} if result.series is None else {item.q: item for item in result.series.slices}
            for i, ring in enumerate(result.rings):
                item = by_q.get(ring.center_q)
                checked = QtWidgets.QTableWidgetItem()
                checked.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                checked.setCheckState(QtCore.Qt.CheckState.Checked if result.selected[i] else QtCore.Qt.CheckState.Unchecked)
                self.table.setItem(i, 0, checked)
                values = (f"{ring.center_q:.6g}", f"{ring.radial_range[0]:.6g} – {ring.radial_range[1]:.6g}",
                          "—" if item is None or item.width_deg is None else f"{item.width_deg:.6g}",
                          ring.error or (item.error if item is not None else None) or "候选")
                for j, value in enumerate(values, 1):
                    cell = QtWidgets.QTableWidgetItem(value)
                    cell.setToolTip(value)
                    self.table.setItem(i, j, cell)
                peak_fit = getattr(item, "fit", None)
                quality = "—" if peak_fit is None else f"{getattr(peak_fit, 'metrics', {}).get('r_squared', float('nan')):.6g}"
                self.table.setItem(i, 5, QtWidgets.QTableWidgetItem(quality))
        fit = None if result.series is None else result.series.result
        if fit is None:
            self.label.setText(f"未得出结果：{result.error or '请检查各环带'}")
        else:
            length_error = getattr(fit, "length_stderr_nm", None)
            angle_error = getattr(fit, "misorientation_stderr_rad", None)
            length_text = f"{fit.length_nm:.5g}" + (f" ± {length_error:.3g}" if length_error is not None else "")
            angle_text = f"{np.degrees(fit.misorientation_rad):.5g}" + (
                f" ± {np.degrees(angle_error):.3g}" if angle_error is not None else "")
            self.label.setText(f"微纤长度 {length_text} nm；微纤位错角 {angle_text}°；"
                               f"斜率 {getattr(fit, 'slope', float('nan')):.6g}，截距 {getattr(fit, 'intercept', float('nan')):.6g}；"
                               f"R² {fit.r_squared:.5g}。±仅为回归标准误，不含峰宽和系统误差；"
                               "候选值须核对 B 定义与峰归属。")
        self._plot_result()
        self._plot_azimuth_curves()

    def _change_preview_format(self, *_):
        plot = self.plot_tabs.currentWidget()
        frame, grid, legend = (self.preview_frame.isChecked(), self.preview_grid.isChecked(),
                               self.preview_legend.isChecked())
        self.preview_format[plot] = (frame, grid, legend)
        for axis in ("top", "right"):
            plot.showAxis(axis, show=frame)
            plot.getAxis(axis).setStyle(showValues=False, tickLength=0)
        plot.getViewBox().setBorder(pg.mkPen("#262626", width=1.25) if frame else None)
        plot.showGrid(x=grid, y=grid, alpha=.12)
        self.q_legend.setVisible(legend)

    def _restore_preview_format(self, *_):
        settings = self.preview_format.get(self.plot_tabs.currentWidget(), (False, False, True))
        for widget, value in zip((self.preview_frame, self.preview_grid, self.preview_legend), settings):
            with QtCore.QSignalBlocker(widget):
                widget.setChecked(value)
        self._change_preview_format()

    def _reset_preview_range(self):
        plot = self.plot_tabs.currentWidget()
        self.preview_ranges.pop(plot, None)
        plot.getViewBox().enableAutoRange()
        plot.getViewBox().autoRange()

    def _edit_preview_range(self):
        plot = self.plot_tabs.currentWidget()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("设置预览坐标范围")
        form = QtWidgets.QFormLayout(dialog)
        fields = []
        for axis, limits in zip(("x", "y"), plot.viewRange()):
            for bound, value in zip(("下限", "上限"), limits):
                field = QtWidgets.QLineEdit(f"{value:.9g}")
                form.addRow(f"{axis} {bound}", field)
                fields.append(field)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            try:
                x0, x1, y0, y1 = (float(field.text()) for field in fields)
                if not np.isfinite((x0, x1, y0, y1)).all() or not (x0 < x1 and y0 < y1):
                    raise ValueError
            except ValueError:
                QtWidgets.QMessageBox.warning(self, "范围无效", "坐标上下限须为有限数字，且下限小于上限。")
                return
            self.preview_ranges[plot] = ((x0, x1), (y0, y1))
            plot.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)

    def _plot_azimuth_curves(self):
        self.azimuth_plot.clear()
        if self.result is None:
            return
        fits = {} if self.result.series is None else {item.q: getattr(item, "fit", None) for item in self.result.series.slices}
        count = len(self.result.rings)
        center = getattr(self.result, "peak_center_deg", None)
        center = self.angle.value() if center is None else center
        for number, ring in enumerate(self.result.rings):
            if ring.curve is None:
                continue
            color = pg.intColor(number, hues=max(1, count))
            curve = ring.curve
            x = np.asarray(curve.x) % 360
            y = np.asarray(curve.intensity, dtype=float).copy()
            y[np.asarray(curve.count) <= 0] = np.nan
            order = np.argsort(x)
            self.azimuth_plot.setLabel("left", "强度", units=getattr(curve, "intensity_unit", "count"))
            self.azimuth_plot.plot(x[order], y[order], pen=None, symbol="o", symbolSize=3,
                                   symbolBrush=color, symbolPen=None, name=f"q={ring.center_q:.5g}")
            fit = fits.get(ring.center_q)
            if fit is not None:
                angle = (np.asarray(fit.used_x)+center) % 360
                order = np.argsort(angle)
                angle, values = angle[order], np.array(np.asarray(fit.best_fit_y)[order], copy=True)
                values[np.r_[False, np.diff(angle) > 180]] = np.nan
                self.azimuth_plot.plot(angle, values, pen=pg.mkPen(color, width=2))
        if self.azimuth_plot in self.preview_ranges:
            x_range, y_range = self.preview_ranges[self.azimuth_plot]
            self.azimuth_plot.setRange(xRange=x_range, yRange=y_range, padding=0)
        else:
            self.azimuth_plot.setXRange(0, 360, padding=0)

    def _apply_fit_region(self):
        if self.result is None or self.fit_region is None or self.single_active or self.batch_active:
            return
        left, right = self.fit_region.getRegion()
        if left <= 0 or right <= left:
            return
        power = 2 if self.result.series.model == "gaussian" else 1
        lower = float(np.nextafter(right ** (-1 / power), -np.inf))
        upper = float(np.nextafter(left ** (-1 / power), np.inf))
        self.fit_q_range = (lower, upper)
        valid_q = {item.q for item in self.result.series.slices if item.error is None}
        selected = tuple(lower <= ring.center_q <= upper and ring.center_q in valid_q
                         for ring in self.result.rings)
        series = refit_ruland_series(self.result.series, [
            lower <= item.q <= upper and item.error is None for item in self.result.series.slices])
        self.result = replace(self.result, series=series, selected=selected, error=series.error)
        self.range_checks = ()
        self.batch_results = []
        self._display_result()

    def _plot_result(self, *, pending_selection=None):
        self.ruland_plot.clear()
        self.fit_region = None
        self.fit_range_button.setEnabled(False)
        if self.result is None or self.result.series is None:
            return
        gaussian = self.model.currentData() == "gaussian"
        self.ruland_plot.setLabel("bottom", "1/q² (nm²)" if gaussian else "1/q (nm)")
        self.ruland_plot.setLabel("left", "B² (rad²)" if gaussian else "B (rad)")
        by_q = {item.q: item for item in self.result.series.slices}
        groups = {True: ([], [], []), False: ([], [], [])}
        selected = self.result.selected if pending_selection is None else pending_selection
        for i, ring in enumerate(self.result.rings):
            item = by_q.get(ring.center_q)
            if item is None or item.width_deg is None or ring.center_q <= 0:
                continue
            x = 1 / ring.center_q**2 if gaussian else 1 / ring.center_q
            width = np.radians(item.width_deg)
            y = width**2 if gaussian else width
            groups[selected[i]][0].append(x)
            groups[selected[i]][1].append(y)
            groups[selected[i]][2].append((i + 1, ring.center_q))
        self.q_legend.clear()
        for number, ring in enumerate(self.result.rings):
            if ring.curve is not None:
                color = pg.intColor(number, hues=max(1, len(self.result.rings)))
                entry = QtWidgets.QListWidgetItem(f"●  {ring.center_q:.6g}")
                entry.setForeground(color)
                self.q_legend.addItem(entry)
        for chosen, (x, y, points) in groups.items():
            if x:
                brushes = [pg.mkBrush(pg.intColor(number - 1, hues=len(self.result.rings)))
                           for number, _ in points]
                pen = pg.mkPen("#222222") if chosen else pg.mkPen("#999999", width=2)
                plotted = self.ruland_plot.plot(x, y, data=points, pen=None,
                    symbol="o" if chosen else "t", symbolSize=8,
                    symbolBrush=brushes, symbolPen=pen)
                plotted.scatter.setData(
                    x=x, y=y, data=points, size=8, pen=pen, brush=brushes,
                    symbol="o" if chosen else "t", hoverable=True,
                    tip=lambda x, y, data: f"#{data[0]}  q = {data[1]:.6g} nm⁻¹",
                )
        coordinates = groups[True][0] + groups[False][0]
        self.fit_region = None
        if coordinates and min(coordinates) < max(coordinates):
            power = 2 if gaussian else 1
            limits = (min(coordinates), max(coordinates))
            region = limits if self.fit_q_range is None else tuple(
                q ** (-power) for q in reversed(self.fit_q_range))
            self.fit_region = pg.LinearRegionItem(region, bounds=limits, brush=(33, 118, 174, 35))
            self.ruland_plot.addItem(self.fit_region, ignoreBounds=True)
        self.fit_range_button.setEnabled(self.fit_region is not None)
        if self.fit_q_range is not None:
            lower, upper = self.fit_q_range
            self.fit_range_button.setText(f"应用选区并线性拟合 · 当前 q {lower:.6g}–{upper:.6g} nm⁻¹")
        else:
            self.fit_range_button.setText("拖动散点图选区边界后，点击线性拟合")
        fit = self.result.series.result
        if pending_selection is None and fit is not None and hasattr(fit, "slope") and groups[True][0]:
            x = np.linspace(min(groups[True][0]), max(groups[True][0]), 100)
            self.ruland_plot.plot(x, fit.slope * x + fit.intercept,
                                  pen=pg.mkPen("#d1495b", width=2))
        if self.ruland_plot in self.preview_ranges:
            x_range, y_range = self.preview_ranges[self.ruland_plot]
            self.ruland_plot.setRange(xRange=x_range, yRange=y_range, padding=0)
        elif coordinates:
            self.ruland_plot.getViewBox().enableAutoRange()
            self.ruland_plot.getViewBox().autoRange(padding=.09)

    def show_range_sensitivity(self):
        if self.result is None or self.result.series is None or self.result.series.result is None:
            self.label.setText("请先得到有效的当前样品 Ruland 候选。")
            return
        displayed = tuple(self.table.item(i, 0).checkState() == QtCore.Qt.CheckState.Checked
                          for i in range(self.table.rowCount()))
        if displayed != self.result.selected:
            self.label.setText("q 点选择已更改，请先重新计算。")
            return
        self.range_checks = check_ruland_range_sensitivity(self.result.series.result)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("q 范围敏感性（只供比较）")
        dialog.resize(760, 320)
        layout = QtWidgets.QVBoxLayout(dialog)
        note = QtWidgets.QLabel("仅比较移除边界点后的变化，不自动删除点或选择范围。")
        layout.addWidget(note)
        table = QtWidgets.QTableWidget(len(self.range_checks), 6)
        table.setHorizontalHeaderLabels(["范围", "q 下限", "q 上限", "点数", "长度 (nm)", "状态"])
        table.horizontalHeader().setStretchLastSection(True)
        labels = {"selected": "当前选择", "without_lowest_q": "去掉最低 q 点",
                  "without_highest_q": "去掉最高 q 点", "without_both_ends": "去掉两端 q 点"}
        for row, check in enumerate(self.range_checks):
            values = (labels.get(check.label, check.label), f"{check.q_min_nm_inv:.6g}", f"{check.q_max_nm_inv:.6g}",
                      str(check.point_count), "—" if check.result is None else f"{check.result.length_nm:.6g}",
                      check.error or "候选")
            for col, value in enumerate(values):
                table.setItem(row, col, QtWidgets.QTableWidgetItem(value))
        layout.addWidget(table)
        dialog.exec()

    def start_batch(self):
        if self.peak_suggestion_active:
            self.label.setText("峰中心建议仍在计算；完成后可运行批量分析。")
            return
        if self.single_active:
            self.label.setText("当前样品正在分析，请在计算结束后启动批量处理。")
            return
        if self.batch_active:
            self.batch_stop.set()
            self.label.setText("将在当前样品完成后停止。")
            return
        error = self._sampling_error()
        if error is not None:
            self.label.setText(error)
            return
        selected = (None if self.result is None else tuple(
            self.table.item(i, 0).checkState() == QtCore.Qt.CheckState.Checked
            for i in range(self.table.rowCount())))
        if self.result is not None and selected != self.result.selected:
            self.label.setText("q 点选择已更改，请先重新计算当前样品。")
            return
        if self.fit_q_range is not None:
            lower, upper = self.fit_q_range
            selected = tuple(lower <= q <= upper for q in self._q_centers())
        use_auto_angle = self.batch_auto_angle.isChecked()
        self.batch_settings = dict(q_centers=self._q_centers(),
            q_width=self.q_width.value(), peak_center_deg=None if use_auto_angle else self.angle.value(),
            half_window_deg=self.half_window.value(), model=self.model.currentData(),
            width_definition=self.width.currentData(), selected=selected)
        if self.fit_q_range is not None:
            self.batch_settings["fit_q_range"] = self.fit_q_range
        if use_auto_angle:
            self.batch_settings["reference_peak_center_deg"] = self.angle.value()
        self.batch_sampling_source = self._sampling_source()
        self.batch_sampling_details = self._sampling_details()
        self.batch_angle_source = "auto_per_sample" if use_auto_angle else self.angle_source
        self.batch_results = []
        self.batch_active = True
        self.batch_stop.clear()
        self._batch_controls = (self.q_min, self.q_max, self.q_width, self.q_count, self.q_step, self.q_sampling, self.angle,
                                self.half_window, self.model, self.width, self.run_button,
                                self.batch_auto_angle, self.export_button, self.export_image_button,
                                self.export_batch_images_button, self.range_check_button, self.table)
        for widget in self._batch_controls:
            widget.setEnabled(False)
        self.batch_button.setText("完成当前样品后停止")
        sources = self.batch_sources
        settings = dict(self.batch_settings)
        runner, stop, signals = self.run_batch_item, self.batch_stop, self.batch_signals
        self.batch_thread = threading.Thread(target=self._run_batch_worker,
            args=(sources, runner, settings, stop, signals), name="ruland-batch", daemon=True)
        self.batch_thread.start()

    @staticmethod
    def _run_batch_worker(sources, runner, settings, stop, signals):
        results = []
        for index, source in enumerate(sources):
            name = str(getattr(source,'display_name',getattr(source, "path", source)))
            if stop.is_set():
                results.append((name, None, "not_processed: stopped"))
                continue
            try:
                result = runner(source, **settings)
                results.append((name, result, result.error))
            except Exception as exc:
                results.append((name, None, f"{type(exc).__name__}: {exc}"))
            signals.progress.emit(index + 1, len(sources))
        signals.finished.emit(results)

    @QtCore.Slot(int, int)
    def _batch_progress(self, done, total):
        self.label.setText(f"后台批量处理 {done}/{total}")

    @QtCore.Slot(object)
    def _batch_finished(self, results):
        self.batch_results = list(results)
        self.batch_active = False
        self.batch_thread = None
        for widget in self._batch_controls:
            widget.setEnabled(True)
        self._sampling_visibility()
        self.batch_button.setText(f"批量处理左侧勾选样品（{len(self.batch_sources)}）")
        completed, failed, stopped = 0, [], []
        for name, entry, error in self.batch_results:
            if error and error.startswith("not_processed:"):
                stopped.append(name)
            elif entry is None or getattr(getattr(entry, "series", None), "result", None) is None:
                failed.append((name, error or "未得到 Ruland 候选结果"))
            else:
                completed += 1
        self.label.setToolTip("\n".join(f"{name}：{reason}" for name, reason in failed))
        self.label.setText(f"批量处理：候选 {completed}，失败 {len(failed)}，未处理 {len(stopped)}；"
                           "失败详情见提示，候选值请核查峰归属。")

    def closeEvent(self, event):
        if self.image_export_active:
            self.image_stop.set()
            self.label.setText("正在完成当前图片并清理本次导出，请稍后关闭。")
            event.ignore()
            return
        if self.peak_suggestion_active:
            self.label.setText("峰中心建议仍在后台运行，完成后可以关闭。")
            event.ignore()
            return
        if self.single_active:
            self.label.setText("当前样品仍在后台计算，完成后可以关闭。")
            event.ignore()
            return
        if self.batch_active:
            self.batch_stop.set()
            self.label.setText("正在完成当前样品，结束后可以关闭。")
            event.ignore()
            return
        event.accept()

    def export_dialog(self):
        if self.result is None and not self.batch_results:
            self.label.setText("请先计算，再导出。")
            return
        from .fit_export import FitExportDialog
        scopes = []
        if self.result is not None:
            scopes.append(("当前样品 · 全部q", "current"))
        if self.batch_results:
            scopes.append(("本批全部样品 · 全部q", "batch"))
        dialog = FitExportDialog(self, scope_items=scopes,
            note_text="工作簿只含曲线数据、参数汇总、处理说明。\n保留每个q的完整原始及拟合曲线、Ruland散点和回归结果。")
        dialog.setWindowTitle("导出 Ruland 结果")
        if self.batch_results:
            dialog.scope_combo.setCurrentIndex(dialog.scope_combo.findData("batch"))
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        scope = dialog.scope_combo.currentData()
        try:
            if dialog.content_combo.currentData() == "data":
                path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存 Ruland 数据", "ruland.xlsx", "Excel (*.xlsx)")
                if path:
                    self.export_workbook(Path(path), scope=scope)
                    self.label.setText("已导出：曲线数据、参数汇总、处理说明，共3张表。")
            elif scope == "batch":
                folder = choose_export_folder(self, "保存批量 Ruland 图片")
                if folder:
                    self.start_image_export(Path(folder), dialog.format_combo.currentText().lower())
            else:
                fmt = dialog.format_combo.currentText().lower()
                path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存 Ruland 图片", "ruland."+fmt, f"{fmt.upper()} (*.{fmt})")
                if path:
                    self.export_image(Path(path))
                    self.label.setText("已导出当前 Ruland 图片。")
        except (OSError, ValueError) as exc:
            self.label.setText(f"导出失败：{exc}")

    def export_image_dialog(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 Ruland 图", "",
            "PNG (*.png);;TIFF (*.tif);;PDF (*.pdf);;SVG (*.svg)")
        if path:
            try:
                self.export_image(Path(path))
            except (OSError, ValueError) as exc:
                self.label.setText(f"图片导出失败：{exc}")

    def export_batch_images_dialog(self):
        image_format, accepted = QtWidgets.QInputDialog.getItem(self, "图片格式", "格式", ("PNG", "TIFF", "PDF", "SVG"), 0, False)
        if not accepted:
            return
        folder = choose_export_folder(self, "选择批量导出文件夹")
        if not folder:
            return
        self.start_image_export(Path(folder), image_format.lower())

    def _image_snapshot(self):
        return (list(self.batch_results), self.batch_settings,
                self.batch_sampling_source, self.batch_angle_source)

    def start_image_export(self, directory, image_format="png", *, dpi=600):
        if self.image_export_active or self.single_active or self.batch_active or self.peak_suggestion_active:
            return
        snapshot = self._image_snapshot()
        self.image_export_active = True
        self.image_stop = threading.Event()
        self._image_controls = [(w, w.isEnabled()) for w in self.findChildren(QtWidgets.QWidget)
            if isinstance(w, (QtWidgets.QAbstractButton, QtWidgets.QAbstractSpinBox,
                              QtWidgets.QComboBox, QtWidgets.QTableWidget))
            and not isinstance(w.window(), QtWidgets.QProgressDialog)]
        for widget, _ in self._image_controls:
            widget.setEnabled(False)
        self.image_progress = QtWidgets.QProgressDialog("准备导出Ruland图片…", "停止导出", 0,
                                                        len(self.batch_results), self)
        self.image_progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        self.image_progress.canceled.connect(self.image_stop.set)
        self.image_progress.show()
        self.image_thread = threading.Thread(target=self._image_worker,
            args=(snapshot, directory, image_format, dpi, self.image_stop, self.image_signals), daemon=True)
        self.image_thread.start()

    @staticmethod
    def _image_worker(snapshot, directory, image_format, dpi, stop, signals):
        try:
            outcome = RulandDialog._write_batch_images(snapshot, directory, image_format,
                dpi=dpi, stop=stop, progress=signals.progress.emit)
            signals.finished.emit((outcome, None))
        except Exception as exc:
            signals.finished.emit((None, str(exc)))

    def _image_progress(self, done, total):
        self.image_progress.setMaximum(total)
        self.image_progress.setValue(done)

    def _image_finished(self, payload):
        self.image_export_active = False
        self.image_thread = None
        self.image_progress.close()
        self.image_progress.deleteLater()
        for widget, enabled in self._image_controls:
            widget.setEnabled(enabled)
        outcome, error = payload
        if error:
            self.label.setText(f"批量导图未完成：{error}")
        else:
            paths, self._last_image_export_failures = outcome
            self.label.setText(f"已导出 {len(paths)} 张图及一份参数说明；"
                               f"{self._last_image_export_failures} 个样品未导出。")

    def export_batch_images(self, directory, image_format="png", *, dpi=600):
        paths, failures = self._write_batch_images(self._image_snapshot(), directory, image_format, dpi=dpi)
        self._last_image_export_failures = failures
        return paths

    @staticmethod
    def _write_batch_images(snapshot, directory, image_format="png", *, dpi=600, stop=None, progress=None):
        batch_results, batch_settings, sampling_source, angle_source = snapshot
        formats = {"png": ".png", "tiff": ".tif", "pdf": ".pdf", "svg": ".svg"}
        if image_format not in formats or not batch_results:
            raise ValueError("请选择有效格式并先完成批处理")
        root = Path(directory).expanduser().resolve()
        protected = (Path(__file__).resolve().parents[2] / "data").resolve()
        if root == protected or protected in root.parents:
            raise ValueError("不能写入受保护的原始数据目录")
        candidates = [(name, entry) for name, entry, error in batch_results
                      if entry is not None and entry.series is not None and entry.series.result is not None]
        if not candidates:
            raise ValueError("批次没有可导出的拟合结果")
        paths, seen = [], set()
        for name, entry in candidates:
            stem = re.sub(r'[<>:"/\\|?*]', "_", str(name)).strip(" .") or "sample"
            base, counter = stem, 2
            while stem.casefold() in seen:
                stem, counter = f"{base}_{counter}", counter + 1
            seen.add(stem.casefold())
            paths.append(root / f"{stem}_ruland{formats[image_format]}")
        note = root / "ruland_image_parameters.txt"
        if any(path.exists() for path in [*paths, note]):
            raise FileExistsError("图片或参数说明已存在，请选择其他文件夹")
        root.mkdir(parents=True, exist_ok=True)
        created = []
        try:
            rendered, render_failures = [], []
            for number, ((name, entry), path) in enumerate(zip(candidates, paths), 1):
                if stop is not None and stop.is_set():
                    raise InterruptedError("已停止；本次导出文件已清理")
                try:
                    RulandDialog._render_image(path, entry, dpi=dpi)
                except ValueError as exc:
                    render_failures.append({"sample": name, "reason": str(exc)})
                    continue
                created.append(path)
                rendered.append((name, entry, path))
                if progress is not None:
                    progress(number, len(paths))
            if stop is not None and stop.is_set():
                raise InterruptedError("已停止；本次导出文件已清理")
            with note.open("x", encoding="utf-8") as stream:
                created.append(note)
                stream.write("Ruland 批量图片参数和处理记录\n")
                stream.write(json.dumps({"settings": batch_settings,
                    "q_sampling_source": sampling_source,
                    "peak_angle_source": angle_source, "format": image_format, "dpi": dpi,
                    "items": [{"sample": name, "image": path.name, "background": dict(entry.background_audit),
                               "mask": dict(entry.mask_audit), "selected": entry.selected,
                               "sample_source": getattr(entry, "sample_source", None),
                               "peak_center_deg": getattr(entry, "peak_center_deg", None),
                               "peak_center_source": (getattr(entry, "peak_center_source", None)
                                   if batch_settings is not None and batch_settings["peak_center_deg"] is None
                                   else angle_source),
                               "peak_suggestion_q": getattr(entry, "peak_suggestion_q", None),
                               "geometry": getattr(entry, "geometry_audit", {}),
                               "integration": dict(entry.rings[0].curve.audit)
                                   if entry.rings and entry.rings[0].curve is not None else None,
                               "length_nm": entry.series.result.length_nm,
                               "misorientation_deg": float(np.degrees(entry.series.result.misorientation_rad)),
                               "r_squared": entry.series.result.r_squared}
                              for name, entry, path in rendered],
                    "failures": [{"sample": name, "reason": error} for name, entry, error in batch_results
                                 if entry is None or entry.series is None or entry.series.result is None] + render_failures},
                    ensure_ascii=False, indent=2, default=lambda value: value.tolist() if hasattr(value, "tolist") else str(value)))
        except Exception:
            for created_path in reversed(created):
                created_path.unlink(missing_ok=True)
            raise
        return tuple(path for _, _, path in rendered), len(batch_results) - len(rendered)

    def export_image(self, path, *, dpi=600, result=None):
        """Validate current UI selection, then render an independent result."""
        if result is None:
            result = self.result
            displayed = tuple(self.table.item(i, 0).checkState() == QtCore.Qt.CheckState.Checked
                              for i in range(self.table.rowCount()))
            if result is not None and displayed != result.selected:
                raise ValueError("q 点选择已更改，请重新计算后导出")
        return self._render_image(path, result, dpi=dpi)

    @staticmethod
    def _render_image(path, result, *, dpi=600):
        from .rendering import publication_render_context
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from .integration_panel import IntegrationPanel
        if result is None or result.series is None or result.series.result is None:
            raise ValueError("请先完成 Ruland 拟合")
        if not isinstance(dpi, int) or dpi <= 0:
            raise ValueError("DPI必须为正整数")
        protected = (Path(__file__).resolve().parents[2] / "data").resolve()
        resolved = path.expanduser().resolve()
        if resolved == protected or protected in resolved.parents:
            raise ValueError("不能写入受保护的原始数据目录")
        suffix = path.suffix.lower()
        formats = {".png": "png", ".tif": "tiff", ".tiff": "tiff", ".pdf": "pdf", ".svg": "svg"}
        if suffix not in formats:
            raise ValueError("支持 PNG、TIFF、PDF 或 SVG")
        fit = result.series.result
        gaussian = fit.model == "gaussian"
        by_q = {item.q: item for item in result.series.slices}
        groups = {True: ([], []), False: ([], [])}
        for i, ring in enumerate(result.rings):
            item = by_q.get(ring.center_q)
            if item is None or item.width_deg is None or ring.center_q <= 0:
                continue
            x = 1 / ring.center_q**2 if gaussian else 1 / ring.center_q
            b = np.radians(item.width_deg)
            groups[result.selected[i]][0].append(x)
            groups[result.selected[i]][1].append(b**2 if gaussian else b)
        if not groups[True][0]:
            raise ValueError("没有可导出的有效选点")
        with publication_render_context(IntegrationPanel._publication_matplotlib_style()):
            figure = Figure(figsize=(180 / 25.4, 130 / 25.4), dpi=dpi)
            FigureCanvasAgg(figure)
            axis = figure.add_subplot(111)
            axis.scatter(*groups[True], s=26, color="#2176ae", label="Selected", zorder=3)
            if groups[False][0]:
                axis.scatter(*groups[False], s=26, facecolors="none", edgecolors="#888888",
                             label="Excluded", zorder=3)
            x = np.linspace(min(groups[True][0]), max(groups[True][0]), 200)
            axis.plot(x, fit.slope*x + fit.intercept, color="#d1495b", linewidth=1.8, label="Fit")
            axis.set_xlabel(r"$1/q^2$ (nm$^2$)" if gaussian else r"$1/q$ (nm)")
            axis.set_ylabel(r"$B^2$ (rad$^2$)" if gaussian else r"$B$ (rad)")
            axis.tick_params(direction="in", top=False, right=False)
            axis.legend(loc="best")
            figure.tight_layout()
            created = False
            try:
                with path.open("xb") as stream:
                    created = True
                    figure.savefig(stream, format=formats[suffix], dpi=dpi)
            except Exception:
                if created:
                    path.unlink(missing_ok=True)
                raise
            finally:
                figure.clear()
        return path

    @staticmethod
    def _q_point_status(ring, item, selected):
        error = ring.error or (item.error if item is not None else None)
        if error:
            return f"failed: {error}"
        if not selected:
            return "excluded"
        if item is None or item.width_deg is None:
            return "unavailable"
        return "candidate"

    def _sampling_source(self):
        current = (self.q_min.value(), self.q_max.value(), self.q_width.value())
        if self.initial_sampling is None:
            return ("default_candidate" if np.allclose(current, (.1, .3, self._default_q_width),
                    rtol=0, atol=5e-9) else "manual_override")
        return (self.initial_sampling[3] if np.allclose(current, self.initial_sampling[:3],
                rtol=0, atol=5e-9) else "manual_override")

    def export_workbook(self, path, *, scope="auto"):
        from .ruland_export import export_ruland_workbook
        if scope not in ("auto", "current", "batch"):
            raise ValueError("请选择当前样品或整批")
        batch = scope == "batch" or (scope == "auto" and bool(self.batch_results))
        if batch:
            if not self.batch_results:
                raise ValueError("尚无批处理结果")
            entries = tuple(self.batch_results)
            settings = self.batch_settings or {}
            sampling = {**(self.batch_sampling_details or {"source": self.batch_sampling_source}), "angle_source": self.batch_angle_source}
        else:
            if self.result is None:
                raise ValueError("尚无当前样品结果")
            displayed = tuple(self.table.item(i, 0).checkState() == QtCore.Qt.CheckState.Checked
                              for i in range(self.table.rowCount()))
            if displayed != self.result.selected:
                raise ValueError("点选择已更改，请重新计算后导出")
            source = getattr(self.result, "sample_source", None) or "current_sample"
            entries = ((getattr(self.result,'sample_label',None) or Path(source).name, self.result, self.result.error),)
            settings = {"model": self.model.currentData(), "width_definition": self.width.currentData(),
                "half_window_deg": self.half_window.value(), "peak_center_deg": self.angle.value(),
                "q_width": self.q_width.value(), "q_centers": [r.center_q for r in self.result.rings]}
            sampling = self._sampling_details()
        return export_ruland_workbook(path, entries, settings, sampling=sampling)

    def export_detailed_workbook(self, path):
        """Historical multi-sheet export retained for programmatic compatibility."""
        if self.result is None and not self.batch_results:
            raise ValueError("尚无计算结果")
        batch_only = self.result is None
        if not batch_only:
            displayed = tuple(self.table.item(i, 0).checkState() == QtCore.Qt.CheckState.Checked
                              for i in range(self.table.rowCount()))
            if displayed != self.result.selected:
                raise ValueError("点选择已更改，请重新计算后导出")
        protected = (Path(__file__).resolve().parents[2] / "data").resolve()
        resolved = path.expanduser().resolve()
        if resolved == protected or protected in resolved.parents:
            raise ValueError("不能写入受保护的原始数据目录")
        result = self.result if self.result is not None else SimpleNamespace(
            rings=(), series=None, error="batch_only", background_audit={}, mask_audit={},
            selected=(), sample_source=None, geometry_audit={})
        book = Workbook()
        summary = book.active
        summary.title = "Summary"
        summary.append(["Method", "Width definition", "q unit", "Peak center (deg)", "Half window (deg)",
                        "Length (nm)", "Misorientation (deg)", "R²", "Status", "Background audit", "Mask audit",
                        "Sample source", "Geometry audit", "Integration audit example", "Formula source",
                        "Width note", "Length StdErr (nm)", "Misorientation StdErr (deg)",
                        "Slope StdErr", "Intercept StdErr", "Uncertainty Assumption",
                        "q Sampling Source", "Peak Angle Source", "Slope", "Intercept", "q Sampling Mode", "Requested q Step (nm^-1)"])
        series = result.series
        fit = None if series is None else series.result
        summary.append([self.model.currentData(), self.width.currentData(), "nm^-1",
            self.angle.value(), self.half_window.value(), None if fit is None else fit.length_nm,
            None if fit is None else float(np.degrees(fit.misorientation_rad)),
            None if fit is None else fit.r_squared, result.error or "candidate",
            json.dumps(dict(result.background_audit), ensure_ascii=False, default=str),
            json.dumps(dict(result.mask_audit), ensure_ascii=False, default=str),
            getattr(result, "sample_source", None),
            json.dumps(getattr(result, "geometry_audit", {}), ensure_ascii=False, default=str),
            json.dumps(dict(result.rings[0].curve.audit), ensure_ascii=False, default=str)
                if result.rings and result.rings[0].curve is not None else None,
            "Pauw et al. 2010 p.3 eq.3,5,6" if self.width.currentData() == "integral_breadth"
                else "Liu et al. 2025 SI p.5 eq.5/6",
            "integral breadth from measured intensities; peak-fit constant baseline"
                if self.width.currentData() == "integral_breadth"
                else "B uses selected FWHM/HWHM directly; correspondence to source half-peak width unresolved",
            None if fit is None else getattr(fit, "length_stderr_nm", None),
            None if fit is None or getattr(fit, "misorientation_stderr_rad", None) is None else
                float(np.degrees(fit.misorientation_stderr_rad)),
            None if fit is None else getattr(fit, "slope_stderr", None),
            None if fit is None else getattr(fit, "intercept_stderr", None),
            "unweighted linear regression; fixed peak widths", self._sampling_source(),
            self.angle_source, None if fit is None else getattr(fit, "slope", None),
            None if fit is None else getattr(fit, "intercept", None), self.q_sampling.currentData(),
            self.q_step.value()])
        points = book.create_sheet("q Points")
        points.append(["q (nm^-1)", "Range Start", "Range End", "Selected", "Peak Width (deg)", "Status",
                       "Transformed X", "Transformed Y", "Fit Y", "Peak Fit R2", "FWHM (deg)"])
        curves = book.create_sheet("Azimuth Curves")
        by_q = {} if series is None else {item.q: item for item in series.slices}
        for i, ring in enumerate(result.rings):
            item = by_q.get(ring.center_q)
            tx = ty = predicted = None
            if item is not None and item.width_deg is not None and ring.center_q > 0:
                gaussian = self.model.currentData() == "gaussian"
                tx = 1 / ring.center_q**2 if gaussian else 1 / ring.center_q
                width = np.radians(item.width_deg)
                ty = float(width**2 if gaussian else width)
                if result.selected[i] and fit is not None and hasattr(fit, "slope"):
                    predicted = float(fit.slope * tx + fit.intercept)
            points.append([ring.center_q, *ring.radial_range, result.selected[i],
                None if item is None else item.width_deg,
                self._q_point_status(ring, item, result.selected[i]), tx, ty, predicted,
                getattr(getattr(item, "fit", None), "metrics", {}).get("r_squared"),
                getattr(getattr(item, "fit", None), "parameters", {}).get("fwhm").value
                if "fwhm" in getattr(getattr(item, "fit", None), "parameters", {}) else None])
            if ring.curve is None:
                continue
            col = i * 6 + 1
            for offset, title in enumerate((f"q={ring.center_q:g} angle (deg)", "Intensity", "Valid count",
                                            "Fit angle (deg)", "Fit intensity", "Residual")):
                curves.cell(1, col + offset, title)
            for row, values in enumerate(zip(ring.curve.x, ring.curve.intensity, ring.curve.count), 2):
                for offset, value in enumerate(values):
                    v = float(value)
                    curves.cell(row, col + offset, v if np.isfinite(v) else None)
            if item is not None and item.fit is not None:
                peak = item.fit
                for row, values in enumerate(zip((peak.used_x + self.angle.value()) % 360,
                                                  peak.best_fit_y, peak.residual_y), 2):
                    for offset, value in enumerate(values, 3):
                        v = float(value)
                        curves.cell(row, col + offset, v if np.isfinite(v) else None)
        if self.batch_results:
            batch = book.create_sheet("Batch Summary")
            batch.append(["Sample", "Status", "Length (nm)", "Misorientation (deg)", "R²",
                          "Method", "Width definition", "Failure", "Background audit", "Mask audit",
                          "Length StdErr (nm)", "Misorientation StdErr (deg)",
                          "q Sampling Source", "Peak Angle Source",
                          "Actual Peak Center (deg)", "Peak Suggestion q (nm^-1)",
                          "Formula version", "Formula source", "Width definition note",
                          "Sample source", "Geometry audit", "Integration audit example",
                          "Peak half window (deg)"])
            batch_points = book.create_sheet("Batch q Points")
            batch_points.append(["Sample", "q (nm^-1)", "Range Start", "Range End", "Selected", "Width (deg)", "Status"])
            batch_curves = book.create_sheet("Batch Curves")
            batch_curves.append(["Sample", "q (nm^-1)", "Angle (deg)", "Intensity", "Valid count"])
            batch_fits = book.create_sheet("Batch Peak Fits")
            batch_fits.append(["Sample", "q (nm^-1)", "Angle (deg)", "Observed", "Fit", "Residual"])
            batch_sensitivity = book.create_sheet("Batch Range Sensitivity")
            batch_sensitivity.append(["Sample", "Range", "q Min (nm^-1)", "q Max (nm^-1)",
                                      "Points", "Length (nm)", "Misorientation (deg)", "R²",
                                      "Length Change vs Full (%)", "Status"])
            batch_settings = self.batch_settings or {
                "width_definition": self.width.currentData(),
                "half_window_deg": self.half_window.value(),
            }
            for name, entry, error in self.batch_results:
                fit = None if entry is None or entry.series is None else entry.series.result
                status = "candidate" if fit is not None else ("not_processed" if error and error.startswith("not_processed:") else "failed")
                width_definition = batch_settings["width_definition"]
                formula_version = getattr(fit, "formula_version",
                    "pauw_2010" if width_definition == "integral_breadth" else "liu_2025")
                integration_audit = next((ring.curve.audit for ring in getattr(entry, "rings", ())
                    if ring.curve is not None), None) if entry is not None else None
                batch.append([name, status,
                              None if fit is None else fit.length_nm,
                              None if fit is None else float(np.degrees(fit.misorientation_rad)),
                              None if fit is None else fit.r_squared, self.model.currentData(),
                              width_definition, error,
                              None if entry is None else json.dumps(dict(entry.background_audit), ensure_ascii=False, default=str),
                              None if entry is None else json.dumps(dict(entry.mask_audit), ensure_ascii=False, default=str),
                              None if fit is None else getattr(fit, "length_stderr_nm", None),
                              None if fit is None or getattr(fit, "misorientation_stderr_rad", None) is None else
                                  float(np.degrees(fit.misorientation_stderr_rad)),
                              self.batch_sampling_source,
                              self.batch_angle_source if self.batch_settings is not None and
                                  self.batch_settings["peak_center_deg"] is not None else
                                  (None if entry is None else getattr(entry, "peak_center_source", self.batch_angle_source)),
                              None if entry is None else getattr(entry, "peak_center_deg", None),
                              None if entry is None else getattr(entry, "peak_suggestion_q", None),
                              formula_version,
                              "Pauw et al. 2010 p.3 eq.3,5,6" if formula_version == "pauw_2010"
                                  else "Liu et al. 2025 SI p.5 eq.5/6",
                              "integral breadth from measured intensities; peak-fit constant baseline"
                                  if formula_version == "pauw_2010" else
                                  "selected FWHM/HWHM used directly; source half-peak width correspondence unresolved",
                              name if entry is None else getattr(entry, "sample_source", name),
                              None if entry is None else json.dumps(getattr(entry, "geometry_audit", {}),
                                  ensure_ascii=False, default=str),
                              None if integration_audit is None else json.dumps(dict(integration_audit),
                                  ensure_ascii=False, default=str),
                              batch_settings["half_window_deg"]])
                if fit is not None and hasattr(fit, "q_nm_inv"):
                    for check in check_ruland_range_sensitivity(fit):
                        diagnostic = check.result
                        relative_change = (None if diagnostic is None or fit.length_nm == 0 else
                                           100.0 * (diagnostic.length_nm / fit.length_nm - 1.0))
                        batch_sensitivity.append([name, check.label, check.q_min_nm_inv,
                            check.q_max_nm_inv, check.point_count,
                            None if diagnostic is None else diagnostic.length_nm,
                            None if diagnostic is None else float(np.degrees(diagnostic.misorientation_rad)),
                            None if diagnostic is None else diagnostic.r_squared,
                            relative_change, check.error or "candidate"])
                if entry is None:
                    continue
                entry_angle = getattr(entry, "peak_center_deg", None)
                if entry_angle is None:
                    entry_angle = self.angle.value()
                by_q = {} if entry.series is None else {item.q: item for item in entry.series.slices}
                for i, ring in enumerate(entry.rings):
                    item = by_q.get(ring.center_q)
                    batch_points.append([name, ring.center_q, *ring.radial_range, entry.selected[i],
                        None if item is None else item.width_deg,
                        self._q_point_status(ring, item, entry.selected[i])])
                    if ring.curve is not None:
                        for angle, intensity, count in zip(ring.curve.x, ring.curve.intensity, ring.curve.count):
                            vals = [float(v) if np.isfinite(v) else None for v in (angle, intensity, count)]
                            batch_curves.append([name, ring.center_q, *vals])
                    if item is not None and item.fit is not None:
                        peak = item.fit
                        for local, observed, predicted, residual in zip(peak.used_x, peak.observed_y,
                                                                       peak.best_fit_y, peak.residual_y):
                            vals = [float(v) if np.isfinite(v) else None for v in
                                    ((local + entry_angle) % 360,
                                     observed, predicted, residual)]
                            batch_fits.append([name, ring.center_q, *vals])
        if self.range_checks:
            sensitivity = book.create_sheet("Range Sensitivity")
            sensitivity.append(["Range", "q Min (nm^-1)", "q Max (nm^-1)", "Points",
                                "Length (nm)", "Misorientation (deg)", "R²", "Status"])
            for check in self.range_checks:
                sensitivity.append([check.label, check.q_min_nm_inv, check.q_max_nm_inv, check.point_count,
                    None if check.result is None else check.result.length_nm,
                    None if check.result is None else float(np.degrees(check.result.misorientation_rad)),
                    None if check.result is None else check.result.r_squared, check.error or "candidate"])
        if batch_only:
            for empty_sheet in ("Summary", "q Points", "Azimuth Curves"):
                del book[empty_sheet]
        with path.open("xb") as stream:
            book.save(stream)
        return path
