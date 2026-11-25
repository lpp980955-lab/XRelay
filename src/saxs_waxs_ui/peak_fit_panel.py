"""Compact Phase 6 single-peak analysis card."""

from __future__ import annotations
from .export_naming import choose_export_folder

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from typing import Any, Iterable, Mapping
from uuid import uuid4

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from saxs_waxs_core.integration import IntegrationCurve
from saxs_waxs_core.herman import from_single_peak_fit, compare_peaks
from saxs_waxs_core.crystallinity import CRYSTALLINITY_METHOD_ID, crystallinity_from_fit
from saxs_waxs_core.auto_azimuth import fit_all_automatic_azimuth
from saxs_waxs_core.peak_fitting import (
    PeakFitError,
    PeakFitResult,
    compare_peak_models,
    fit_multi_peak,
    fit_peak,
)
from .widgets import CollapsibleSection, scrollable_content
from .saxs_analysis_dialog import SaxsAnalysisDialog
from .debye_bueche_dialog import DebyeBuecheDialog


MODEL_ITEMS = (
    ("GaussianAmp（峰高参数）", "gaussian_height"),
    ("Gaussian（面积参数）", "gaussian_area"),
    ("Lorentzian", "lorentzian_area"),
    ("Voigt", "voigt_area"),
    ("Pseudo-Voigt", "pseudo_voigt_area"),
    ("Pearson VII", "pearson7_area"),
    ("Split Gaussian", "split_gaussian_area"),
    ("Split Lorentzian", "split_lorentzian_area"),
)
BACKGROUND_ITEMS = (
    ("无", "none"),
    ("常数", "constant"),
    ("线性", "linear"),
)

HERMAN_STATUS_LABELS = {
    "outside_domain": "选定范围超出峰中心两侧90°，请缩小拟合范围",
    "insufficient_domain": "峰区有遮挡、缺失或只覆盖峰的一侧；请检查积分区域和MASK",
    "negative_intensity": "扣除拟合背景后存在负强度；请检查峰范围和背景模型",
    "invalid_points_integral": "散点加权积分无效；请检查背景和积分范围",
    "negative_fit": "拟合峰包含负强度；请检查峰模型",
    "invalid_integral": "有效强度不足，无法计算取向因子",
    "duplicate_angles": "角度数据存在重复；请检查积分数据",
}


class _ImageExportSignals(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal(object)


class _AutomaticBatchSignals(QtCore.QObject):
    finished = QtCore.Signal(int, object, object, object, object)


class _ModelComparisonSignals(QtCore.QObject):
    finished = QtCore.Signal(int, object, object)


class PeakFitPanel(QtWidgets.QWidget):
    """Fit one explicitly selected integration curve and range."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        from .latest_task import LatestTask
        self._interactive_tasks=LatestTask(self)
        self._interactive_tasks.ready.connect(self._interactive_fit_ready)
        self._interactive_tasks.failed.connect(lambda error:self.status_label.setText(error))
        self._background_fitting=False
        self._curves: list[tuple[str, IntegrationCurve]] = []
        self._parameter_contexts: list[Mapping[str, Any]] = []
        self._result: PeakFitResult | None = None
        self._result_curve_index: int | None = None
        self._results: dict[int, PeakFitResult] = {}
        self._fit_failures: dict[int, str] = {}
        self._herman_pairs: dict[int, tuple[Any, PeakFitResult]] = {}
        self._herman_pair_failures: dict[int, str] = {}
        self._herman_comparison_dialog: QtWidgets.QDialog | None = None
        self._auto_fits: dict[int, list] = {}
        self._component_models: tuple[str, ...] | None = None
        self._component_center_bounds: tuple | None = None
        self._component_fwhm_bounds: tuple | None = None
        self._auto_herman: dict[int, list] = {}
        self._selected_peaks: dict[int, set[int]] = {}
        self._auto_failures: dict[int, list] = {}
        self._auto_batch_pending: set[int] = set()
        self._auto_batch_active = False
        self._auto_batch_stop = False
        self.image_export_active = False
        self.image_signals = _ImageExportSignals(self)
        self.image_signals.progress.connect(self._image_progress)
        self.image_signals.finished.connect(self._image_finished)
        self._auto_batch_thread = None
        self._auto_batch_signals = _AutomaticBatchSignals(self)
        self._auto_batch_signals.finished.connect(self._auto_batch_finished_item)
        self._auto_batch_timer = QtCore.QTimer(self)
        self._auto_batch_timer.setSingleShot(True)
        self._auto_batch_timer.timeout.connect(self._auto_batch_step)
        self._comparison_signals = _ModelComparisonSignals(self)
        self._comparison_signals.finished.connect(self._comparison_finished)
        self._comparison_thread = None
        self._comparison_request_id = 0
        self._comparison_started_with = None

        self.curve_combo = QtWidgets.QComboBox()
        self.auto_check = QtWidgets.QCheckBox("自动识峰、选模型并计算 Herman")
        self.auto_prominence = QtWidgets.QDoubleSpinBox()
        self.auto_prominence.setRange(0.1, 30.0)
        self.auto_prominence.setValue(3.0)
        self.auto_prominence.setDecimals(1)
        self.auto_prominence.setSingleStep(0.5)
        self.auto_prominence.setSuffix(" %")
        self.auto_prominence.setToolTip("调小可识别较弱峰，但也可能识别噪声；请检查候选峰。")
        self.azimuth_view_combo = QtWidgets.QComboBox()
        self.azimuth_view_combo.addItem("完整方位角（全部峰）", "full")
        self.azimuth_view_combo.addItem("当前峰局部", "peak")
        self.azimuth_view_combo.currentIndexChanged.connect(self._azimuth_view_changed)
        self.auto_peak_combo = QtWidgets.QComboBox()
        self.auto_peak_combo.setVisible(False)
        self.model_combo = QtWidgets.QComboBox()
        for label, key in MODEL_ITEMS:
            self.model_combo.addItem(label, key)
        self.peak_strategy_widget = QtWidgets.QWidget()
        self._peak_checkboxes = []
        self._peak_checkbox_curve = None
        self._peak_plot_lines = []
        peak_tools = QtWidgets.QHBoxLayout(self.peak_strategy_widget)
        peak_tools.setContentsMargins(0, 0, 0, 0)
        self.select_all_peaks_button = QtWidgets.QPushButton("全选峰")
        self.peak_checks_widget = QtWidgets.QWidget()
        self.peak_checks_layout = QtWidgets.QHBoxLayout(self.peak_checks_widget)
        self.peak_checks_layout.setContentsMargins(0, 0, 0, 0)
        self.peak_checks_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        self.peak_checks_scroll = scrollable_content(self.peak_checks_widget)
        self.peak_checks_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.peak_checks_scroll.setMinimumHeight(38)
        self.peak_model_combo = QtWidgets.QComboBox()
        for label, key in MODEL_ITEMS:
            self.peak_model_combo.addItem(label, key)
        self.apply_peak_model_button = QtWidgets.QPushButton("拟合选中峰")
        self.peak_selection_label = QtWidgets.QLabel()
        self.peak_selection_label.setToolTip("勾选要拟合或导出的峰；勾选不会重新计算或改变坐标范围。")
        for widget in (self.select_all_peaks_button, self.peak_model_combo, self.apply_peak_model_button, self.peak_selection_label):
            peak_tools.addWidget(widget)
        self.select_all_peaks_button.clicked.connect(self.select_all_peaks)
        self.apply_peak_model_button.clicked.connect(self._request_selected_peak_fit)
        self.peak_strategy_widget.hide()
        self.background_combo = QtWidgets.QComboBox()
        for label, key in BACKGROUND_ITEMS:
            self.background_combo.addItem(label, key)
        self.background_combo.setCurrentIndex(1)
        self.peak_count_spin = QtWidgets.QSpinBox()
        self.peak_count_spin.setRange(1, 8)
        self.peak_count_spin.setValue(1)
        self.peak_count_spin.setToolTip(
            "2–8峰使用同一线型自动给出初值；每个峰的物理归属仍需人工确认"
        )
        self.component_models_button = QtWidgets.QPushButton("逐峰线型…")
        self.component_models_button.setToolTip("为各峰分别选择线型；按峰中心从小到大编号。")
        self.component_models_button.clicked.connect(self._choose_component_models)

        self.range_lower = QtWidgets.QDoubleSpinBox()
        self.range_upper = QtWidgets.QDoubleSpinBox()
        self.range_lower.setToolTip("方位角跨0°时可输入下限大于上限，例如350到10。")
        self.range_upper.setToolTip("方位角跨0°时可输入下限大于上限，例如350到10。")
        for widget in (self.range_lower, self.range_upper):
            widget.setRange(-1e15, 1e15)
            widget.setDecimals(8)
            widget.setKeyboardTracking(False)
        self.use_full_range_button = QtWidgets.QPushButton("使用全部有限点")
        self.select_range_button = QtWidgets.QPushButton("在图中选择拟合范围")
        self._fit_range_region = None
        range_row = QtWidgets.QWidget()
        range_layout = QtWidgets.QHBoxLayout(range_row)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.addWidget(self.range_lower)
        range_layout.addWidget(QtWidgets.QLabel("—"))
        range_layout.addWidget(self.range_upper)

        self.fit_button = QtWidgets.QPushButton("拟合当前模型")
        self.saxs_analysis_button = QtWidgets.QPushButton("Guinier / Porod…")
        self.saxs_analysis_button.clicked.connect(self._open_saxs_analysis)
        self.debye_analysis_button = QtWidgets.QPushButton("Debye–Bueche…")
        self.debye_analysis_button.clicked.connect(self._open_debye_analysis)
        self.fit_batch_button = QtWidgets.QPushButton("按共享设置拟合整批")
        self.herman_button = QtWidgets.QPushButton("计算当前峰 Herman")
        self.herman_domain_combo = QtWidgets.QComboBox()
        self.herman_domain_combo.addItem("选定峰域", "selected")
        self.herman_domain_combo.addItem("完整0–90°域", "full_90")
        self.herman_domain_combo.addItem("自动：拟合模型完整域", "model_full_90")
        self.herman_domain_combo.setCurrentIndex(2)
        self.herman_domain_combo.setToolTip("默认从单峰模型取峰中心±90°计算，与预览范围独立；未测角度标为模型外推。其他选项保留实测域计算。")
        self.herman_preview_button = QtWidgets.QPushButton("查看折叠峰")
        self.herman_compare_button = QtWidgets.QPushButton("比较第二个峰…")
        self.herman_export_check = QtWidgets.QCheckBox("导出时汇总 Herman")
        self.herman_export_check.setToolTip("为导出的每条单峰方位角曲线计算；缺口和失败会保留状态。")
        self.herman_label = QtWidgets.QLabel("选择方位角单峰并拟合后计算。")
        self.herman_label.setWordWrap(True)
        self.crystal_peaks = QtWidgets.QLineEdit()
        self.crystal_peaks.setPlaceholderText("晶峰编号，如 1,2")
        self.amorphous_peaks = QtWidgets.QLineEdit()
        self.amorphous_peaks.setPlaceholderText("非晶峰编号，如 3")
        self.crystal_button = QtWidgets.QPushButton("计算结晶度候选")
        self.crystal_scope = QtWidgets.QComboBox()
        self.crystal_scope.addItem("请确认曲线适用性", "unconfirmed")
        self.crystal_scope.addItem("WAXS径向曲线 · 有结晶特征峰", "waxs_crystalline")
        self.crystal_scope.addItem("只有非晶包峰 · 不做结晶度拟合", "amorphous_only")
        self.crystal_setup_button = QtWidgets.QPushButton("定义晶峰 / 非晶峰与约束…")
        self.crystal_setup_button.clicked.connect(self._choose_crystallinity_template)
        self.crystal_label = QtWidgets.QLabel("仅用于有结晶特征峰的WAXS径向曲线；先人工定义峰位与归属，再批量沿用约束。")
        self.crystal_label.setWordWrap(True)
        self.crystal_peaks.setToolTip("峰编号与图例对应；峰曲线显示该峰与背景之和。")
        self.amorphous_peaks.setToolTip("每个峰都需要指定归属；背景曲线不作为非晶峰。")
        self.crystal_export_check = QtWidgets.QCheckBox("导出时汇总结晶度")
        self.crystal_export_check.setToolTip("整批使用相同峰编号分类；需逐样品核查峰身份。")
        self.crystal_button.clicked.connect(self._calculate_crystallinity)
        self.crystal_scope.currentIndexChanged.connect(self._crystal_scope_changed)
        self._crystal_scope_changed()
        for widget in (self.crystal_peaks, self.amorphous_peaks):
            widget.textChanged.connect(lambda: self.crystal_label.setText("分类已更改，请重新计算。"))
        self.compare_button = QtWidgets.QPushButton("比较常用模型")
        self.compare_button.setToolTip("固定峰数和范围，比较每个峰共用同一线形的候选；不替换逐峰自定义线形。")
        self.export_button = QtWidgets.QToolButton()
        self.export_button.setText("导出拟合结果")
        self.export_button.setToolTip("选择样品/峰范围，导出精简工作簿或拟合图片。")
        self.export_button.setEnabled(False)

        form = QtWidgets.QFormLayout()
        form.addRow("识峰阈值", self.auto_prominence)
        self.fit_settings_form = form
        form.addRow("峰模型", self.model_combo)
        form.addRow("峰数", self.peak_count_spin)
        form.addRow("", self.component_models_button)
        form.addRow("背景", self.background_combo)
        form.addRow("拟合范围", range_row)
        form.addRow("", self.use_full_range_button)
        form.addRow("", self.select_range_button)

        action_row = QtWidgets.QWidget()
        action_layout = QtWidgets.QGridLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.addWidget(self.fit_button, 0, 0)
        action_layout.addWidget(self.compare_button, 0, 1)
        action_layout.addWidget(self.fit_batch_button, 1, 0)
        action_layout.addWidget(self.export_button, 1, 1)

        self.status_label = QtWidgets.QLabel(
            "请先在“积分”页生成曲线；本卡片不平滑、不删异常点。"
        )
        self.status_label.setWordWrap(True)

        self.plot_layout = pg.GraphicsLayoutWidget()
        self.plot_layout.setBackground("w")
        self.fit_plot = self.plot_layout.addPlot(row=0, col=0)
        self.fit_plot.addLegend()
        self.residual_plot = self.plot_layout.addPlot(row=1, col=0)
        self.residual_plot.setXLink(self.fit_plot)
        self.fit_plot.showGrid(x=False, y=False)
        self.residual_plot.showGrid(x=False, y=False)
        self.fit_plot.setMinimumHeight(180)
        self.residual_plot.setMaximumHeight(120)
        self.residual_plot.setLabel("left", "Residual")
        for plot in (self.fit_plot, self.residual_plot):
            for side in ("left", "bottom"):
                plot.getAxis(side).setPen(pg.mkPen("#222222", width=1.0))
                plot.getAxis(side).setTextPen(pg.mkPen("#222222"))

        self.parameter_table = QtWidgets.QTableWidget(0, 4)
        self.parameter_table.setHorizontalHeaderLabels(
            ["参数", "值", "标准误", "单位"]
        )
        self.parameter_table.horizontalHeader().setStretchLastSection(True)
        self.parameter_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.metrics_label = QtWidgets.QLabel("")
        self.metrics_label.setWordWrap(True)
        result_widget = QtWidgets.QWidget()
        result_layout = QtWidgets.QVBoxLayout(result_widget)
        result_layout.setContentsMargins(4, 2, 4, 4)
        self.peak_overview = QtWidgets.QTableWidget(0, 6)
        self.peak_overview.setHorizontalHeaderLabels(["峰", "峰位 (°)", "模型", "FWHM (°)", "R²", "Herman"])
        self.peak_overview.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.peak_overview.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.peak_overview.horizontalHeader().setStretchLastSection(True)
        self.peak_overview.setMinimumHeight(100)
        self.peak_overview.setMaximumHeight(180)
        self.peak_results_tabs = QtWidgets.QTabWidget()
        self.peak_results_tabs.setMinimumHeight(300)
        self._peak_details_key = None
        self.peak_results_tabs.currentChanged.connect(self._peak_detail_changed)
        self.peak_overview.cellClicked.connect(lambda row, _column: self.peak_results_tabs.setCurrentIndex(row))
        result_layout.addWidget(self.peak_overview)
        result_layout.addWidget(self.peak_results_tabs)
        self.peak_overview.hide()
        self.peak_results_tabs.hide()
        result_layout.addWidget(self.metrics_label)
        self.herman_controls = QtWidgets.QWidget()
        herman_layout = QtWidgets.QVBoxLayout(self.herman_controls)
        herman_layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.herman_button, self.herman_domain_combo,
                       self.herman_preview_button, self.herman_compare_button,
                       self.herman_label, self.herman_export_check):
            herman_layout.addWidget(widget)
        result_layout.addWidget(self.herman_controls)
        crystal_widget = QtWidgets.QWidget()
        crystal_layout = QtWidgets.QVBoxLayout(crystal_widget)
        crystal_layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.crystal_scope, self.crystal_setup_button, self.crystal_peaks, self.amorphous_peaks, self.crystal_button,
                       self.crystal_label, self.crystal_export_check):
            crystal_layout.addWidget(widget)
        self.crystal_section = CollapsibleSection("结晶度（候选）", crystal_widget, expanded=False)
        result_layout.addWidget(self.crystal_section)
        result_layout.addWidget(self.parameter_table)
        self.result_section = CollapsibleSection(
            "参数、误差与技术统计", result_widget, expanded=False
        )

        self.comparison_table = QtWidgets.QTableWidget(0, 5)
        self.comparison_table.setHorizontalHeaderLabels(
            ["模型", "AIC", "BIC", "R²", "状态"]
        )
        self.comparison_table.horizontalHeader().setStretchLastSection(True)
        self.comparison_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        comparison_widget = QtWidgets.QWidget()
        comparison_layout = QtWidgets.QVBoxLayout(comparison_widget)
        comparison_layout.setContentsMargins(4, 2, 4, 4)
        comparison_note = QtWidgets.QLabel(
            "排序只提供统计证据，不会自动替换您选择的模型，也不证明物理模型正确。"
        )
        comparison_note.setWordWrap(True)
        comparison_layout.addWidget(comparison_note)
        comparison_layout.addWidget(self.comparison_table)
        self.comparison_section = CollapsibleSection(
            "模型比较（可选）", comparison_widget, expanded=False
        )

        warning = QtWidgets.QLabel(
            "技术拟合 ≠ 科学有效。模型、背景和范围必须随结果保存；Herman、Ruland与结晶度在后续独立卡片中处理。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "QLabel { background: #fff4cc; border: 1px solid #e0bd55; padding: 5px; }"
        )

        controls = QtWidgets.QWidget()
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        header_form = QtWidgets.QFormLayout()
        header_form.addRow("曲线", self.curve_combo)
        header_form.addRow("", self.auto_check)
        header_form.addRow("曲线视图", self.azimuth_view_combo)
        header_form.addRow("查看峰参数", self.auto_peak_combo)
        header_form.addRow("选择峰", self.peak_checks_scroll)
        self.curve_header_form = header_form
        header_form.addRow(self.peak_strategy_widget)
        controls_layout.addLayout(header_form)
        settings = QtWidgets.QWidget()
        settings_layout = QtWidgets.QVBoxLayout(settings)
        settings_layout.addLayout(form)
        settings_layout.addWidget(self.saxs_analysis_button)
        settings_layout.addWidget(self.debye_analysis_button)
        self.fit_settings_section = CollapsibleSection("拟合设置与其他分析", settings, expanded=False)
        controls_layout.addWidget(self.fit_settings_section)
        controls_layout.addWidget(action_row)
        controls_layout.addWidget(self.status_label)

        details = QtWidgets.QWidget()
        details_layout = QtWidgets.QVBoxLayout(details)
        details_layout.setContentsMargins(4, 0, 4, 0)
        details_layout.addWidget(self.result_section)
        details_layout.addWidget(self.comparison_section)
        details_layout.addWidget(warning)
        details_layout.addStretch(1)
        details_scroll = QtWidgets.QScrollArea()
        details_scroll.setWidgetResizable(True)
        details_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        details_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        details_scroll.setWidget(details)

        self.workspace_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(6)
        self.workspace_splitter.addWidget(self.plot_layout)
        self.workspace_splitter.addWidget(details_scroll)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 2)
        self.workspace_splitter.setSizes([780, 420])

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.fit_controls_scroll = scrollable_content(controls)
        self.fit_vertical_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.fit_vertical_splitter.addWidget(self.fit_controls_scroll)
        self.fit_vertical_splitter.addWidget(self.workspace_splitter)
        self.fit_vertical_splitter.setStretchFactor(0, 0)
        self.fit_vertical_splitter.setStretchFactor(1, 1)
        self.fit_vertical_splitter.setSizes([220, 500])
        layout.addWidget(self.fit_vertical_splitter, 1)

        self.curve_combo.currentIndexChanged.connect(self._curve_changed)
        self.auto_check.toggled.connect(self._auto_mode_changed)
        self.auto_prominence.editingFinished.connect(self._auto_threshold_changed)
        self.auto_peak_combo.currentIndexChanged.connect(self._auto_peak_changed)
        self.model_combo.currentIndexChanged.connect(self._reset_component_models)
        self.model_combo.currentIndexChanged.connect(self._shared_setting_changed)
        self.background_combo.currentIndexChanged.connect(self._shared_setting_changed)
        self.peak_count_spin.valueChanged.connect(self._reset_component_models)
        self.peak_count_spin.valueChanged.connect(self._shared_setting_changed)
        self.range_lower.editingFinished.connect(self._shared_setting_changed)
        self.range_upper.editingFinished.connect(self._shared_setting_changed)
        self.use_full_range_button.clicked.connect(self._use_full_range_and_fit)
        self.select_range_button.clicked.connect(self._select_fit_range_on_plot)
        self.fit_button.clicked.connect(self._request_fit_selected)
        self.herman_button.clicked.connect(self._calculate_herman)
        self.herman_domain_combo.currentIndexChanged.connect(self._herman_domain_changed)
        self.herman_preview_button.clicked.connect(self._preview_herman_fold)
        self.herman_compare_button.clicked.connect(self._open_herman_comparison)
        self.fit_batch_button.clicked.connect(self._fit_batch_from_button)
        self.compare_button.clicked.connect(self.compare_models)
        self.export_button.clicked.connect(self._choose_simple_export)
        self._update_enabled_state()

    @property
    def current_result(self) -> PeakFitResult | None:
        return self._result

    def set_curves(
        self,
        curves: Iterable[tuple[str, IntegrationCurve]],
        parameter_contexts: Iterable[Mapping[str, Any]] | None = None,
        *, preserve_settings: bool = False,
    ) -> None:
        self._interactive_tasks.invalidate()
        previous_range=(self.range_lower.value(),self.range_upper.value())
        previous_auto=self.auto_check.isChecked()
        previous_name = self.curve_combo.currentText()
        self._curves = list(curves)
        if parameter_contexts is None:
            self._parameter_contexts = [
                {"integration": dict(curve.audit)} for _name, curve in self._curves
            ]
        else:
            self._parameter_contexts = [dict(value) for value in parameter_contexts]
            if len(self._parameter_contexts) != len(self._curves):
                raise ValueError("curve parameter context count does not match curves")
        blocker = QtCore.QSignalBlocker(self.curve_combo)
        self.curve_combo.clear()
        for name, _curve in self._curves:
            self.curve_combo.addItem(str(name))
        del blocker
        if previous_name:
            index = self.curve_combo.findText(previous_name)
            if index >= 0:
                self.curve_combo.setCurrentIndex(index)
        self._result = None
        self._result_curve_index = None
        self._results.clear()
        self._fit_failures.clear()
        self._herman_pairs.clear()
        self._herman_pair_failures.clear()
        self._auto_fits.clear()
        self._selected_peaks.clear()
        self._auto_herman.clear()
        self._auto_failures.clear()
        self._auto_batch_pending.clear()
        auto = bool(self._curves and all(c.kind == "azimuthal_1d" for _, c in self._curves))
        with QtCore.QSignalBlocker(self.auto_check):
            self.auto_check.setChecked(previous_auto if preserve_settings else auto)
        self.herman_export_check.setChecked(auto)
        self.export_button.setEnabled(False)
        self._set_full_range()
        if preserve_settings:
            with QtCore.QSignalBlocker(self.range_lower),QtCore.QSignalBlocker(self.range_upper):
                self.range_lower.setValue(previous_range[0])
                self.range_upper.setValue(previous_range[1])
        self._clear_result_display()
        self._update_enabled_state()
        if self._curves:
            self.status_label.setText(
                f"已接收 {len(self._curves)} 条积分曲线；当前共享设置会用于整批。"
            )
            self.fit_selected()

    def _update_enabled_state(self) -> None:
        enabled = bool(self._curves)
        self._update_azimuth_controls()
        for widget in (
            self.curve_combo,
            self.model_combo,
            self.component_models_button,
            self.peak_count_spin,
            self.background_combo,
            self.range_lower,
            self.range_upper,
            self.use_full_range_button,
            self.fit_button,
            self.fit_batch_button,
            self.compare_button,
        ):
            widget.setEnabled(enabled)

    def _update_azimuth_controls(self) -> None:
        index = self.curve_combo.currentIndex()
        is_azimuthal = 0 <= index < len(self._curves) and (
            self._curves[index][1].kind == "azimuthal_1d"
            and self._curves[index][1].x_unit == "deg"
        )
        self.herman_controls.setVisible(is_azimuthal)
        self.curve_header_form.setRowVisible(self.auto_check, is_azimuthal)
        self.curve_header_form.setRowVisible(self.azimuth_view_combo, is_azimuthal)
        self.fit_settings_form.setRowVisible(self.auto_prominence, is_azimuthal)
        for widget in (self.auto_peak_combo, self.peak_checks_scroll):
            label = self.curve_header_form.labelForField(widget)
            label.setVisible(is_azimuthal)

    def _curve_changed(self, _index: int) -> None:
        self._interactive_tasks.invalidate()
        self._update_azimuth_controls()
        if not self._curves:
            return
        index = self.curve_combo.currentIndex()
        cached = self._results.get(index)
        if cached is not None:
            self._show_result(index, cached)
            return
        self.fit_selected()

    def _auto_mode_changed(self, enabled: bool) -> None:
        self._auto_fits.clear()
        self._selected_peaks.clear()
        self._auto_herman.clear()
        self._auto_failures.clear()
        self._auto_batch_pending.clear()
        self._results.clear()
        self._fit_failures.clear()
        self._herman_pairs.clear()
        self._herman_pair_failures.clear()
        if not enabled:
            self.auto_peak_combo.setVisible(False)
        if self._curves:
            self.fit_selected()

    def _auto_threshold_changed(self) -> None:
        if self.auto_check.isChecked() and self._curves:
            self._auto_mode_changed(True)

    def _auto_peak_changed(self, index: int) -> None:
        curve_index = self.curve_combo.currentIndex()
        fits = self._auto_fits.get(curve_index, [])
        if 0 <= index < len(fits):
            self._herman_pairs.pop(curve_index, None)
            self._herman_pair_failures.pop(curve_index, None)
            self._results[curve_index] = fits[index].result
            self._show_result(curve_index, fits[index].result, render=self.azimuth_view_combo.currentData() != "full")

    def _shared_setting_changed(self, *_args: object) -> None:
        """Invalidate old fits and immediately apply the shared recipe to current data."""
        self._interactive_tasks.invalidate()
        if not self._curves:
            return
        if self.auto_check.isChecked():
            with QtCore.QSignalBlocker(self.auto_check):
                self.auto_check.setChecked(False)
            self.auto_peak_combo.setVisible(False)
            self._auto_fits.clear()
            self._auto_herman.clear()
            self._auto_failures.clear()
            self._auto_batch_pending.clear()
        self._results.clear()
        self._fit_failures.clear()
        self._herman_pairs.clear()
        self._herman_pair_failures.clear()
        self._result = None
        self._result_curve_index = None
        self.export_button.setEnabled(False)
        self.fit_selected()

    def _reset_component_models(self, *_args: object) -> None:
        self._component_models = None
        self._component_center_bounds = None
        self._component_fwhm_bounds = None
        self.component_models_button.setText("逐峰线型…")

    def _set_component_models(self, models, center_bounds=None, fwhm_bounds=None) -> None:
        models = tuple(models)
        valid = {value for _label, value in MODEL_ITEMS}
        if len(models) != self.peak_count_spin.value() or any(model not in valid for model in models):
            raise ValueError("每个峰需要一个有效线型")
        if center_bounds is not None and len(center_bounds) != len(models):
            raise ValueError("每个峰需要一个位置约束")
        if fwhm_bounds is not None and len(fwhm_bounds) != len(models):
            raise ValueError("每个峰需要一个峰宽约束")
        if fwhm_bounds is not None:
            supported = {"gaussian_area", "gaussian_height", "lorentzian_area",
                         "pseudo_voigt_area", "pearson7_area"}
            for model, bound in zip(models, fwhm_bounds, strict=True):
                if bound is not None and model not in supported:
                    raise ValueError(f"线型 {model} 不支持 FWHM 约束，请留空或更换线型")
        self._component_models = models
        self._component_center_bounds = None if center_bounds is None else tuple(center_bounds)
        self._component_fwhm_bounds = None if fwhm_bounds is None else tuple(fwhm_bounds)
        self.component_models_button.setText("逐峰线型（已设置）…")
        self._shared_setting_changed()

    def _choose_component_models(self) -> None:
        count = self.peak_count_spin.value()
        if count < 2:
            self.status_label.setText("请先将峰数设置为至少 2。")
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("逐峰选择线型")
        layout = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()
        choices = []
        bounds_edits = []
        width_edits = []
        defaults = self._component_models or (str(self.model_combo.currentData()),) * count
        default_bounds = self._component_center_bounds or (None,) * count
        default_widths = self._component_fwhm_bounds or (None,) * count
        for index in range(count):
            combo = QtWidgets.QComboBox()
            for label, value in MODEL_ITEMS:
                combo.addItem(label, value)
            combo.setCurrentIndex(combo.findData(defaults[index]))
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(combo)
            bounds_edit = QtWidgets.QLineEdit()
            bounds_edit.setPlaceholderText("峰位下限,上限（可留空）")
            if default_bounds[index] is not None:
                bounds_edit.setText(",".join(str(value) for value in default_bounds[index]))
            row_layout.addWidget(bounds_edit)
            bounds_edits.append(bounds_edit)
            width_edit = QtWidgets.QLineEdit()
            width_edit.setPlaceholderText("FWHM 下限,上限（可留空）")
            if default_widths[index] is not None:
                width_edit.setText(",".join(str(value) for value in default_widths[index]))
            row_layout.addWidget(width_edit)
            width_edits.append(width_edit)
            form.addRow(f"峰 {index + 1}", row)
            choices.append(combo)
        layout.addLayout(form)
        note = QtWidgets.QLabel("峰按初始中心位置编号；FWHM 约束支持 Gaussian、Lorentzian、Pseudo-Voigt、Pearson VII，其他峰型请留空。")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                             QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            try:
                bounds = []
                for edit in bounds_edits:
                    value = edit.text().strip().replace("，", ",")
                    if not value:
                        bounds.append(None)
                        continue
                    parts = value.split(",")
                    if len(parts) != 2:
                        raise ValueError("每个峰位范围需输入下限,上限")
                    lower, upper = map(float, parts)
                    if not np.isfinite([lower, upper]).all() or lower >= upper:
                        raise ValueError("峰位范围须为有限数，且下限小于上限")
                    bounds.append((lower, upper))
                widths = []
                for edit in width_edits:
                    value = edit.text().strip().replace("，", ",")
                    if not value:
                        widths.append(None)
                        continue
                    parts = value.split(",")
                    if len(parts) != 2:
                        raise ValueError("峰宽范围需输入下限,上限")
                    lower, upper = map(float, parts)
                    if not np.isfinite([lower, upper]).all() or not 0 < lower < upper:
                        raise ValueError("峰宽范围须为正数且下限小于上限")
                    widths.append((lower, upper))
                self._set_component_models((combo.currentData() for combo in choices), bounds, widths)
            except ValueError as exc:
                self.status_label.setText(f"逐峰设置无效：{exc}")

    def _selected_curve(self) -> tuple[int, str, IntegrationCurve]:
        index = self.curve_combo.currentIndex()
        if index < 0 or index >= len(self._curves):
            raise PeakFitError("no integration curve is selected")
        name, curve = self._curves[index]
        return index, name, curve

    def _set_full_range(self) -> None:
        if not self._curves:
            return
        _index, _name, curve = self._selected_curve()
        x = np.asarray(curve.x, dtype=float)
        y = np.asarray(curve.intensity, dtype=float)
        finite = np.isfinite(x) & np.isfinite(y) & (np.asarray(curve.count) > 0)
        if not np.any(finite):
            self.status_label.setText("当前曲线没有可拟合的有限点。")
            return
        lower = float(np.min(x[finite]))
        upper = float(np.max(x[finite]))
        self.range_lower.setValue(lower)
        self.range_upper.setValue(upper)
        step = max((upper - lower) / 1000.0, 1e-6)
        self.range_lower.setSingleStep(step)
        self.range_upper.setSingleStep(step)

    def _use_full_range_and_fit(self) -> None:
        self._set_full_range()
        self._shared_setting_changed()

    def _select_fit_range_on_plot(self) -> None:
        if self._result is None:
            self.status_label.setText("请先拟合一次，再在图中拖动范围。")
            return
        if self._fit_range_region is not None:
            self.fit_plot.removeItem(self._fit_range_region)
        lower, upper = self._result.requested_range
        curve = self._curves[self._result_curve_index][1]
        all_x = np.asarray(curve.x, dtype=float)
        if upper > 360 and curve.kind == "azimuthal_1d":
            all_x = np.where(all_x < lower, all_x + 360.0, all_x)
        order = np.argsort(all_x)
        self.fit_plot.plot(all_x[order], self._fit_intensity(curve)[order],
                           pen=None, symbol="o", symbolSize=3,
                           symbolBrush=pg.mkBrush("#aaaaaa"))
        self._fit_range_region = pg.LinearRegionItem(values=(lower, upper), movable=True)
        self._fit_range_region.setZValue(20)
        self.fit_plot.addItem(self._fit_range_region)
        self._fit_range_region.sigRegionChangeFinished.connect(self._apply_plot_fit_range)
        self.status_label.setText("拖动两条边界；松开后拟合当前曲线，并将范围用于共享设置。")

    def _apply_plot_fit_range(self) -> None:
        if self._fit_range_region is None:
            return
        lower, upper = self._fit_range_region.getRegion()
        if self._result is not None and self._result.requested_range[1] > 360:
            lower %= 360.0
            upper %= 360.0
        self.range_lower.setValue(lower)
        self.range_upper.setValue(upper)
        self._shared_setting_changed()

    def _fit_arguments_for(
        self, index: int
    ) -> tuple[int, str, IntegrationCurve, dict[str, Any]]:
        if index < 0 or index >= len(self._curves):
            raise PeakFitError("no integration curve is selected")
        name, curve = self._curves[index]
        return (
            index,
            name,
            curve,
            {
                "model": str(self.model_combo.currentData()),
                "background": str(self.background_combo.currentData()),
                "x_range": (self.range_lower.value(), self.range_upper.value()),
                "x_unit": str(curve.x_unit),
                "y_unit": str(curve.intensity_unit),
                "peak_count": int(self.peak_count_spin.value()),
            },
        )

    def _fit_arguments(self) -> tuple[int, str, IntegrationCurve, dict[str, Any]]:
        return self._fit_arguments_for(self.curve_combo.currentIndex())

    @staticmethod
    def _fit_intensity(curve: IntegrationCurve) -> np.ndarray:
        values = np.asarray(curve.intensity, dtype=float).copy()
        values[np.asarray(curve.count) <= 0] = np.nan
        return values

    def fit_selected(self) -> PeakFitResult | None:
        if self._background_fitting or self.isVisible():
            self._request_fit_selected()
            return None
        if self.auto_check.isChecked() and self._auto_fits.get(self.curve_combo.currentIndex()):
            return self.fit_selected_peaks()
        return self._fit_index(self.curve_combo.currentIndex(), display=True)

    def _request_fit_selected(self):
        if self._auto_batch_active or not self._curves:return
        index=self.curve_combo.currentIndex()
        if self.auto_check.isChecked() and self._auto_fits.get(index):
            self._request_selected_peak_fit()
            return
        try:
            index,name,curve,kwargs=self._fit_arguments_for(index)
        except ValueError as exc:
            self.status_label.setText(f"无法拟合：{exc}")
            return
        automatic=self.auto_check.isChecked() and curve.kind=="azimuthal_1d" and curve.x_unit=="deg"
        prominence=self.auto_prominence.value()/100
        domain=str(self.herman_domain_combo.currentData())
        models,bounds,widths=self._component_models,self._component_center_bounds,self._component_fwhm_bounds
        def calculate():
            from types import SimpleNamespace
            captured=[]
            signals=SimpleNamespace(finished=SimpleNamespace(emit=lambda *args:captured.append(args)))
            if automatic:self._auto_batch_worker(index,curve,prominence,domain,signals)
            else:self._manual_batch_worker(index,curve,kwargs,models,bounds,widths,signals)
            return curve,automatic,captured[0]
        self.status_label.setText("正在后台拟合…")
        self._interactive_tasks.submit(calculate)

    def _interactive_fit_ready(self,payload):
        curve,automatic,data=payload[:3]
        preferred=payload[3] if len(payload)>3 else 0
        index,fits,failures,herman,error=data
        if not 0<=index<len(self._curves) or self._curves[index][1] is not curve:return
        if error:
            self.status_label.setText(error)
            self._fit_failures[index]=error
            return
        if automatic:
            self._auto_fits[index],self._auto_failures[index],self._auto_herman[index]=fits,failures,herman
        self._herman_pairs.pop(index,None)
        self._herman_pair_failures.pop(index,None)
        self._results[index]=fits[preferred].result if automatic else fits[preferred]
        self._fit_failures.pop(index,None)
        self._show_result(index,self._results[index])

    def _request_selected_peak_fit(self):
        index=self.curve_combo.currentIndex()
        fits=list(self._auto_fits.get(index,[]))
        selected=sorted(self._selected_peaks.get(index,set(range(len(fits)))))
        if not fits or not selected:
            self.status_label.setText("请先识峰并选择要拟合的峰。")
            return
        curve=self._curves[index][1]
        model=self.peak_model_combo.currentData()
        domain=str(self.herman_domain_combo.currentData())
        def calculate():
            pending=list(fits)
            for number in selected:
                old=fits[number].result
                result=fit_peak(old.used_x,old.observed_y,model=model,background=old.background,
                    x_range=old.requested_range,x_unit=old.x_unit,y_unit=old.y_unit)
                pending[number]=replace(fits[number],result=result,warnings=fits[number].warnings+(f"人工峰策略：{model}",))
            herman=[]
            for item in pending:
                try:herman.append(from_single_peak_fit(curve,item.result,domain=domain))
                except ValueError as exc:herman.append(str(exc))
            return curve,True,(index,pending,[],herman,None),selected[0]
        self.status_label.setText("正在后台拟合选中峰…")
        self._interactive_tasks.submit(calculate)

    def _open_saxs_analysis(self) -> None:
        dialog = SaxsAnalysisDialog(self._curves, self, parameter_contexts=self._parameter_contexts)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _open_debye_analysis(self) -> None:
        dialog = DebyeBuecheDialog(self._curves, self, parameter_contexts=self._parameter_contexts)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _fit_index(self, index: int, *, display: bool) -> PeakFitResult | None:
        self._herman_pairs.pop(index, None)
        self._herman_pair_failures.pop(index, None)
        self._results.pop(index, None)
        if self.auto_check.isChecked():
            self._auto_fits.pop(index, None)
            self._selected_peaks.pop(index, None)
            self._auto_herman.pop(index, None)
            self._auto_failures.pop(index, None)
        try:
            index, name, curve, kwargs = self._fit_arguments_for(index)
            if self.auto_check.isChecked() and curve.kind == "azimuthal_1d" and curve.x_unit == "deg":
                fits, failures = fit_all_automatic_azimuth(curve, shared_model=True, allow_masked=self.herman_domain_combo.currentData() == "model_full_90", prominence_fraction=self.auto_prominence.value() / 100.0)
                self._auto_fits[index], self._auto_failures[index] = fits, failures
                self._calculate_all_auto_herman(index)
                if not fits:
                    raise PeakFitError("自动识峰未得到可用拟合；请查看原始曲线并手动设置")
                result = fits[0].result
                self._results[index] = result
                self._fit_failures.pop(index, None)
                if display:
                    self._show_result(index, result)
                return result
            peak_count = int(kwargs.pop("peak_count"))
            fit_x = np.asarray(curve.x, dtype=float)
            lower, upper = kwargs["x_range"]
            if lower > upper:
                if curve.kind != "azimuthal_1d" or not (0 <= upper < lower < 360):
                    raise PeakFitError("递减范围仅适用于跨0°的方位角，例如350到10")
                fit_x = np.where(fit_x < lower, fit_x + 360.0, fit_x)
                kwargs["x_range"] = (lower, upper + 360.0)
            if peak_count == 1:
                result = fit_peak(fit_x, self._fit_intensity(curve), **kwargs)
            else:
                result = fit_multi_peak(
                    fit_x,
                    self._fit_intensity(curve),
                    peak_count=peak_count,
                    models=self._component_models,
                    center_bounds=self._component_center_bounds,
                    fwhm_bounds=self._component_fwhm_bounds,
                    **kwargs,
                )
        except PeakFitError as exc:
            self._fit_failures[index] = str(exc)
            if display:
                self.status_label.setText(f"拟合失败：{exc}")
                self.status_label.setStyleSheet("color: #a11;")
                self._result = None
                self._result_curve_index = None
                self._clear_result_display()
                self.export_button.setEnabled(bool(self._results or self._fit_failures))
            return None
        self._results[index] = result
        self._fit_failures.pop(index, None)
        if display:
            self._show_result(index, result)
        return result

    def _calculate_all_auto_herman(self, index: int) -> None:
        curve = self._curves[index][1]
        results = []
        for item in self._auto_fits.get(index, []):
            try:
                results.append(from_single_peak_fit(curve, item.result, domain=str(self.herman_domain_combo.currentData())))
            except ValueError as exc:
                results.append(str(exc))
        self._auto_herman[index] = results

    def _show_result(self, index: int, result: PeakFitResult, *, render=True) -> None:
        name, _curve = self._curves[index]
        self.crystal_label.setText("曲线或拟合已更新，请重新计算结晶度。")
        self._result = result
        self._result_curve_index = index
        self.export_button.setEnabled(True)
        self.status_label.setStyleSheet("")
        warning_text = "；".join(result.warnings) if result.warnings else "无数值警告"
        self.status_label.setText(
            f"{name}：技术拟合完成（{warning_text}）；尚未判定物理模型有效。"
        )
        self.peak_strategy_widget.setVisible(bool(self.auto_check.isChecked() and self._auto_fits.get(index)))
        self.peak_checks_scroll.setVisible(bool(self.auto_check.isChecked() and self._auto_fits.get(index)))
        self.fit_button.setText("拟合选中峰" if self.auto_check.isChecked() and self._auto_fits.get(index) else "拟合当前模型")
        with QtCore.QSignalBlocker(self.peak_model_combo):
            self.peak_model_combo.setCurrentIndex(self.peak_model_combo.findData(result.model))
        if render:
            self._render_result(name, result)
        self._populate_parameter_table(result)
        if self.auto_check.isChecked() and index in self._auto_fits:
            fits = self._auto_fits[index]
            with QtCore.QSignalBlocker(self.auto_peak_combo):
                self.auto_peak_combo.clear()
                selected = 0
                for i, fit in enumerate(fits):
                    r_squared = fit.result.metrics.get("r_squared", float("nan"))
                    score = f"{r_squared:.3f}" if np.isfinite(r_squared) else "无"
                    self.auto_peak_combo.addItem(f"峰 {i+1} · {fit.peak_angle:.2f}° · {fit.result.model} · R² {score}")
                    if fit.result is result:
                        selected = i
                self.auto_peak_combo.setCurrentIndex(selected)
            self.auto_peak_combo.setVisible(True)
            self.peak_strategy_widget.show()
            chosen = self._selected_peaks.setdefault(index, set(range(len(fits))))
            self._sync_peak_checkboxes(index, len(fits))
            self._refresh_peak_selection()
            current = fits[selected]
            with QtCore.QSignalBlocker(self.model_combo), QtCore.QSignalBlocker(self.background_combo), QtCore.QSignalBlocker(self.peak_count_spin), QtCore.QSignalBlocker(self.range_lower), QtCore.QSignalBlocker(self.range_upper):
                self.model_combo.setCurrentIndex(self.model_combo.findData(current.result.model))
                self.background_combo.setCurrentIndex(self.background_combo.findData(current.result.background))
                self.peak_count_spin.setValue(1)
                self.range_lower.setValue(current.selection_range[0])
                self.range_upper.setValue(current.selection_range[1])
            self.status_label.setText(f"{name}：自动识别 {len(fits)} 个峰，失败 {len(self._auto_failures.get(index, []))}；按AIC选择模型，请检查峰归属、范围和残差。"
                                      + (" 拟合警告：" + "；".join(current.result.warnings) if current.result.warnings else ""))
        else:
            self.auto_peak_combo.setVisible(False)
        self._calculate_herman()

    def _calculate_herman(self) -> None:
        if self._result is None or self._result_curve_index is None:
            self.herman_label.setText("请先拟合方位角单峰。")
            return
        self._refresh_peak_results()
        curve = self._curves[self._result_curve_index][1]
        if curve.kind != "azimuthal_1d" or curve.x_unit != "deg":
            return
        try:
            result = from_single_peak_fit(curve, self._result, domain=str(self.herman_domain_combo.currentData()))
        except ValueError as exc:
            self.herman_label.setText(f"无法计算：{exc}")
            return
        for row in range(self.parameter_table.rowCount() - 1, -1, -1):
            item = self.parameter_table.item(row, 0)
            if item is not None and item.text().startswith("Herman "):
                self.parameter_table.removeRow(row)
        for label, value in (("Herman 拟合", result.f_fit), ("Herman 散点复核", result.f_points)):
            row = self.parameter_table.rowCount()
            self.parameter_table.insertRow(row)
            for column, text_value in enumerate((label, "—" if value is None else f"{value:.8g}", "—", "1")):
                self.parameter_table.setItem(row, column, QtWidgets.QTableWidgetItem(text_value))
        if result.status == "model_based":
            peak_index = next((i for i, item in enumerate(self._auto_fits.get(self._result_curve_index, [])) if item.result is self._result), 0)
            self.herman_label.setText(f"峰 {peak_index+1} · Herman = {result.f_fit:.6f}（模型计算）\n{result.message}\nHerman误差尚未计算。")
            return
        if result.status != "ok":
            description = HERMAN_STATUS_LABELS.get(result.status, result.message)
            fitted_text = (f"拟合曲线 Herman：{result.f_fit:.6f}；"
                           if result.f_fit is not None else "拟合曲线未计算；")
            self.herman_label.setText(
                f"{fitted_text}散点复核未计算：{description}；"
                f"局部范围 {result.coverage_deg[0]:.2f}° 至 "
                f"{result.coverage_deg[1]:.2f}°；负点 {result.negative_points}。"
                "\n拟合值待科学复核；Herman误差尚未计算。"
            )
            return
        domain_label = "有限峰域" if result.domain == "selected" else "完整0–90°域"
        domain_note = "不是完整0–90°域结果，请检查峰边界。" if result.domain == "selected" else "请检查峰边界及角度覆盖。"
        self.herman_label.setText(
            f"{domain_label}：拟合 {result.f_fit:.6f}；散点 {result.f_points:.6f}；"
            f"差值 {result.f_fit - result.f_points:.6f}。\n"
            f"局部范围 {result.coverage_deg[0]:.2f}° 至 {result.coverage_deg[1]:.2f}°；"
            f"{domain_note}\nHerman误差尚未计算。"
        )

    def _crystal_scope_changed(self, *_):
        confirmed = self.crystal_scope.currentData() == "waxs_crystalline"
        for widget in (self.crystal_setup_button, self.crystal_button, self.crystal_export_check):
            widget.setEnabled(confirmed)
        if not confirmed:
            self.crystal_export_check.setChecked(False)
        self.crystal_label.setText(
            "只有非晶包峰：不做结晶度拟合，不输出假定的0%。" if self.crystal_scope.currentData() == "amorphous_only"
            else "先定义晶峰/非晶峰的范围与归属，再试拟合并计算。" if confirmed
            else "请先确认是有结晶特征峰的WAXS径向曲线。")

    def apply_crystallinity_template(self, definitions):
        from .crystallinity_setup import validate_peak_definitions
        if self._auto_batch_active or self._comparison_thread is not None or self.image_export_active:
            raise ValueError("请等待当前拟合或导出任务结束，再修改峰定义")
        index = self.curve_combo.currentIndex()
        if index < 0 or self._curves[index][1].kind != "radial_1d":
            raise ValueError("请选择WAXS径向曲线")
        if self.crystal_scope.currentData() != "waxs_crystalline":
            raise ValueError("先确认WAXS数据中存在结晶特征峰；纯非晶包峰不执行此拟合")
        definitions = validate_peak_definitions(definitions)
        lower, upper = self.range_lower.value(), self.range_upper.value()
        if any(not lower <= d["center_bounds"][0] < d["center_bounds"][1] <= upper for d in definitions):
            raise ValueError("峰位约束应位于当前拟合范围内")
        with QtCore.QSignalBlocker(self.peak_count_spin):
            self.peak_count_spin.setValue(len(definitions))
        self.crystal_peaks.setText(",".join(str(i+1) for i, d in enumerate(definitions) if d["role"] == "crystalline"))
        self.amorphous_peaks.setText(",".join(str(i+1) for i, d in enumerate(definitions) if d["role"] == "amorphous"))
        self._crystal_template = definitions
        self._set_component_models([d["model"] for d in definitions],
            [d["center_bounds"] for d in definitions], [d.get("fwhm_bounds") for d in definitions])
        self.crystal_export_check.setChecked(True)

    def _choose_crystallinity_template(self):
        from .crystallinity_setup import CrystallinitySetupDialog, suggest_peak_definitions
        if self.crystal_scope.currentData() != "waxs_crystalline":
            self.crystal_label.setText("先确认这是有结晶特征峰的WAXS径向曲线；纯非晶包峰无需结晶度拟合。")
            return
        definitions = getattr(self, "_crystal_template", ())
        index = self.curve_combo.currentIndex()
        if not definitions and index >= 0:
            curve = self._curves[index][1]
            if curve.kind == "radial_1d":
                definitions = suggest_peak_definitions(
                    curve, self.range_lower.value(), self.range_upper.value())
        dialog = CrystallinitySetupDialog(definitions, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            try:
                self.apply_crystallinity_template(dialog.definitions)
            except ValueError as exc:
                self.crystal_label.setText(f"未应用：{exc}")

    def _crystallinity_for_index(self, index: int):
        if self._curves[index][1].kind != "radial_1d":
            raise ValueError("结晶度需要径向积分曲线")
        if self.crystal_scope.currentData() == "amorphous_only":
            raise ValueError("只有非晶包峰：不进行结晶峰分解，也不把未计算结果记为0%")
        if self.crystal_scope.currentData() != "waxs_crystalline":
            raise ValueError("请先确认这是有结晶特征峰的WAXS径向曲线；不根据文件夹名自动判断")
        classifications = {}
        for widget, category in ((self.crystal_peaks, "crystalline"), (self.amorphous_peaks, "amorphous")):
            entries = widget.text().replace("，", ",").split(",")
            for entry in entries:
                entry = entry.strip()
                if not entry:
                    continue
                if not entry.isdigit() or int(entry) < 1:
                    raise ValueError("请输入正整数峰编号，以逗号分隔")
                name = f"peak_{int(entry)}"
                if name in classifications:
                    raise ValueError("峰编号重复")
                classifications[name] = category
        return crystallinity_from_fit(self._results[index], classifications)

    def _calculate_crystallinity(self) -> None:
        index = self._result_curve_index
        if index is None:
            self.crystal_label.setText("请先拟合径向多峰。")
            return
        try:
            result = self._crystallinity_for_index(index)
        except ValueError as exc:
            self.crystal_label.setText(f"未计算：{exc}")
            return
        self.crystal_label.setText(
            f"结晶度候选 {result.fraction * 100:.2f}%；晶峰面积 {result.crystalline_area:.6g}，"
            f"非晶面积 {result.amorphous_area:.6g}。请核查峰归属、背景和范围。"
            + (" 拟合警告：" + "；".join(self._results[index].warnings) if self._results[index].warnings else ""))

    def _herman_domain_changed(self) -> None:
        self._herman_pairs.clear()
        self._herman_pair_failures.clear()
        for index in self._auto_fits:
            self._calculate_all_auto_herman(index)
        self.herman_label.setText("计算域已更改，请重新计算 Herman。")
        if self.auto_check.isChecked() and self._result is not None:
            self._calculate_herman()

    def _peak_detail_changed(self, number):
        if number >= 0 and number != self.auto_peak_combo.currentIndex():
            self.auto_peak_combo.setCurrentIndex(number)

    def _refresh_peak_results(self):
        index = self._result_curve_index
        fits = self._auto_fits.get(index, []) if self.auto_check.isChecked() else []
        automatic = bool(fits)
        self.peak_overview.setVisible(automatic)
        self.peak_results_tabs.setVisible(automatic)
        for widget in (self.metrics_label, self.parameter_table, self.herman_label, self.herman_button,
                       self.herman_preview_button, self.herman_compare_button):
            widget.setVisible(not automatic)
        if not automatic:
            return
        key = (index, tuple(id(item.result) for item in fits), self.herman_domain_combo.currentData())
        current = next((i for i, item in enumerate(fits) if item.result is self._result), 0)
        if key == self._peak_details_key:
            with QtCore.QSignalBlocker(self.peak_results_tabs):
                self.peak_results_tabs.setCurrentIndex(current)
            return
        self._peak_details_key = key
        with QtCore.QSignalBlocker(self.peak_results_tabs):
            while self.peak_results_tabs.count():
                old = self.peak_results_tabs.widget(0)
                self.peak_results_tabs.removeTab(0)
                old.deleteLater()
            self.peak_overview.setRowCount(len(fits))
            for number, item in enumerate(fits):
                fit = item.result
                center = fit.parameters["center"].value % 360
                herman = self._auto_herman.get(index, [])
                h = herman[number] if number < len(herman) else "尚未计算"
                value = None if isinstance(h, str) else h.f_fit
                note = h if isinstance(h, str) else h.message
                overview = (str(number+1), f"{center:.3f}", fit.model,
                            f"{fit.parameters['fwhm'].value:.6g}", f"{fit.metrics['r_squared']:.6g}",
                            "未计算" if value is None else f"{value:.8g}")
                for column, text in enumerate(overview):
                    cell = QtWidgets.QTableWidgetItem(text)
                    cell.setToolTip(note)
                    self.peak_overview.setItem(number, column, cell)
                page = QtWidgets.QWidget()
                layout = QtWidgets.QVBoxLayout(page)
                title = QtWidgets.QLabel(f"峰 {number+1} · {center:.3f}° · {fit.model}")
                title.setWordWrap(True)
                layout.addWidget(title)
                table = QtWidgets.QTableWidget(0, 4)
                table.setHorizontalHeaderLabels(["参数/结果", "值", "标准误", "单位"])
                table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
                table.horizontalHeader().setStretchLastSection(True)
                rows = [(name, parameter.value % 360 if name == "center" else parameter.value,
                         parameter.stderr, parameter.unit) for name, parameter in fit.parameters.items()]
                rows.extend((name, fit.metrics.get(name), None, "") for name in ("r_squared", "rmse", "aic", "bic"))
                rows.extend([("Herman 拟合", value, None, "1"),
                             ("Herman 散点复核", None if isinstance(h, str) else h.f_points, None, "1")])
                table.setRowCount(len(rows))
                for row, values in enumerate(rows):
                    for column, value_ in enumerate(values):
                        text = "—" if value_ is None else f"{value_:.9g}" if isinstance(value_, (int, float)) else str(value_)
                        table.setItem(row, column, QtWidgets.QTableWidgetItem(text))
                table.resizeColumnsToContents()
                layout.addWidget(table, 1)
                explanation = QtWidgets.QLabel(note)
                explanation.setWordWrap(True)
                layout.addWidget(explanation)
                self.peak_results_tabs.addTab(page, f"峰 {number+1}")
            self.peak_overview.resizeColumnsToContents()
            self.peak_overview.setFixedHeight(min(180, max(90,
                self.peak_overview.horizontalHeader().height()
                + len(fits) * self.peak_overview.verticalHeader().defaultSectionSize() + 22)))
            self.peak_results_tabs.setCurrentIndex(current)

    def _preview_herman_fold(self) -> None:
        result = self._result
        if result is None or self._result_curve_index is None:
            self.herman_label.setText("请先拟合方位角单峰。")
            return
        curve = self._curves[self._result_curve_index][1]
        if curve.kind != "azimuthal_1d" or "center" not in result.parameters or result.components:
            self.herman_label.setText("折叠预览需要方位角单峰拟合。")
            return
        x = result.used_x
        local = (x - result.parameters["center"].value + 180.0) % 360.0 - 180.0
        background = np.zeros_like(x)
        if result.background != "none":
            background += result.parameters["background_intercept"].value
        if result.background == "linear":
            background += result.parameters["background_slope"].value * (x - np.mean(x))
        signal = result.observed_y - background
        fitted = result.best_fit_y - background
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("方位峰折叠预览")
        dialog.resize(800, 520)
        layout = QtWidgets.QVBoxLayout(dialog)
        note = QtWidgets.QLabel("左右两侧分别显示；横坐标是距拟合峰中心的角度。数据未镜像、补点或平均。")
        note.setWordWrap(True)
        layout.addWidget(note)
        plot = pg.PlotWidget(background="w")
        plot.addLegend()
        plot.setLabel("bottom", "局部角度绝对值", units="°")
        plot.setLabel("left", "扣除拟合背景后的强度", units=result.y_unit)
        for mask, color, name in ((local < 0, "#3577b0", "左侧"),
                                  (local >= 0, "#d45b44", "右侧")):
            if np.any(mask):
                angle = np.abs(local[mask])
                order = np.argsort(angle)
                plot.plot(angle[order], signal[mask][order], pen=None, symbol="o",
                          symbolSize=4, symbolBrush=pg.mkBrush(color), name=name + "散点")
                plot.plot(angle[order], fitted[mask][order], pen=None, symbol="t",
                          symbolSize=4, symbolBrush=pg.mkBrush(color), name=name + "拟合")
        layout.addWidget(plot)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _compare_second_peak(self, lower: float, upper: float, *, average: bool = False):
        if self._result is None or self._result_curve_index is None:
            raise ValueError("请先拟合第一个方位角单峰")
        self._herman_pairs.pop(self._result_curve_index, None)
        curve = self._curves[self._result_curve_index][1]
        domain = str(self.herman_domain_combo.currentData())
        first = from_single_peak_fit(curve, self._result, domain=domain)
        x = np.asarray(curve.x, dtype=float)
        if lower > upper:
            if not (0 <= upper < lower < 360):
                raise ValueError("跨0°范围必须位于0至360度")
            x = np.where(x < lower, x + 360.0, x)
            upper += 360.0
        second_fit = fit_peak(x, self._fit_intensity(curve), model=self._result.model,
                              background=self._result.background, x_range=(lower, upper),
                              x_unit=str(curve.x_unit), y_unit=str(curve.intensity_unit))
        second = from_single_peak_fit(curve, second_fit, domain=domain)
        comparison = compare_peaks(first, second, average=average)
        self._herman_pair_failures.pop(self._result_curve_index, None)
        self._herman_pairs[self._result_curve_index] = (comparison, second_fit)
        return comparison, second_fit

    def _open_herman_comparison(self) -> None:
        if self._herman_comparison_dialog is not None:
            self._herman_comparison_dialog.raise_()
            self._herman_comparison_dialog.activateWindow()
            return
        if self._result is None or self._result_curve_index is None:
            self.herman_label.setText("请先拟合第一个方位角单峰。")
            return
        dialog = QtWidgets.QDialog(self)
        self._herman_comparison_dialog = dialog
        def forget_dialog(*_args: object) -> None:
            if self._herman_comparison_dialog is dialog:
                self._herman_comparison_dialog = None
        dialog.finished.connect(forget_dialog)
        dialog.destroyed.connect(forget_dialog)
        dialog.setWindowTitle("比较第二个方位峰")
        layout = QtWidgets.QFormLayout(dialog)
        lower = QtWidgets.QDoubleSpinBox()
        upper = QtWidgets.QDoubleSpinBox()
        for spin in (lower, upper):
            spin.setRange(0, 360)
            spin.setDecimals(3)
            spin.setSuffix("°")
        center = self._result.parameters.get("center")
        if center is None:
            self.herman_label.setText("需要单峰拟合。")
            self._herman_comparison_dialog = None
            dialog.deleteLater()
            return
        opposite = (center.value + 180.0) % 360.0
        lower.setValue((opposite - 30.0) % 360.0)
        upper.setValue((opposite + 30.0) % 360.0)
        average = QtWidgets.QCheckBox("同时计算两个峰的平均值")
        output = QtWidgets.QLabel("第二峰范围仅为初始建议，请根据曲线调整。")
        output.setWordWrap(True)
        run = QtWidgets.QPushButton("分别计算并比较")
        run_batch = QtWidgets.QPushButton("整批比较第二峰")
        stop_batch = QtWidgets.QPushButton("完成当前样品后停止")
        stop_batch.setEnabled(False)
        layout.addRow("第二峰下限", lower)
        layout.addRow("第二峰上限", upper)
        layout.addRow(average)
        layout.addRow(run)
        layout.addRow(run_batch)
        layout.addRow(stop_batch)
        layout.addRow(output)
        compared_index = self._result_curve_index
        compared_fit = self._result
        compared_domain = self.herman_domain_combo.currentData()
        def invalidate_comparison(*_args: object) -> None:
            self._herman_pairs.clear()
            self._herman_pair_failures.clear()
            output.setText("设置已变化，请重新计算比较结果。")
        lower.valueChanged.connect(invalidate_comparison)
        upper.valueChanged.connect(invalidate_comparison)
        average.toggled.connect(invalidate_comparison)
        def calculate() -> None:
            if (self._result_curve_index != compared_index or self._result is not compared_fit or
                self.herman_domain_combo.currentData() != compared_domain):
                output.setText("当前曲线或拟合设置已变化，请关闭窗口并重新打开比较。")
                return
            try:
                comparison, fit = self._compare_second_peak(lower.value(), upper.value(), average=average.isChecked())
            except (ValueError, PeakFitError) as exc:
                output.setText(f"无法比较：{exc}")
                return
            a, b = comparison.first, comparison.second
            if a.status != "ok" or b.status != "ok":
                first_status = "正常" if a.status == "ok" else HERMAN_STATUS_LABELS.get(a.status, a.message)
                second_status = "正常" if b.status == "ok" else HERMAN_STATUS_LABELS.get(b.status, b.message)
                fit_values = (f"拟合值：第一峰 {a.f_fit:.6f}；第二峰 {b.f_fit:.6f}；差值 {comparison.fit_difference:.6f}。\n"
                              if comparison.fit_difference is not None else "拟合值不足，无法比较。\n")
                if comparison.mean_fit is not None:
                    fit_values += f"拟合值平均：{comparison.mean_fit:.6f}。\n"
                output.setText(f"{fit_values}散点复核：第一峰 {first_status}；第二峰 {second_status}。")
                return
            message = (f"第一峰：{a.f_fit:.6f}；第二峰：{b.f_fit:.6f}；差值：{comparison.fit_difference:.6f}。\n"
                       f"散点复核：第一峰 {a.f_points:.6f}；第二峰 {b.f_points:.6f}；差值 {comparison.points_difference:.6f}。\n"
                       f"第二峰中心：{fit.parameters['center'].value % 360:.3f}°。局部覆盖范围分别为 "
                       f"{a.coverage_deg[0]:.2f}–{a.coverage_deg[1]:.2f}° 和 "
                       f"{b.coverage_deg[0]:.2f}–{b.coverage_deg[1]:.2f}°，请检查范围可比性。")
            if comparison.mean_fit is not None:
                message += f"\n拟合值平均：{comparison.mean_fit:.6f}；散点值平均：{comparison.mean_points:.6f}。"
            output.setText(message)
        timer = QtCore.QTimer(dialog)
        timer.setSingleShot(True)
        batch_state: dict[str, Any] = {"running": False, "stop": False, "index": 0}
        controlled = (self.curve_combo, self.model_combo, self.peak_count_spin,
                      self.background_combo, self.range_lower, self.range_upper,
                      self.use_full_range_button, self.select_range_button, self.fit_button,
                      self.fit_batch_button, self.compare_button, self.export_button,
                      self.herman_domain_combo, self.herman_button, self.herman_compare_button,
                      lower, upper, average, run, run_batch)
        def finish_batch(*_args: object) -> None:
            if not batch_state["running"]:
                return
            timer.stop()
            batch_state["running"] = False
            if batch_state["index"] < len(self._curves):
                batch_state["stop"] = True
            if batch_state["stop"]:
                for index in range(batch_state["index"], len(self._curves)):
                    self._herman_pair_failures[index] = "not_processed: batch stopped"
            for widget, was_enabled in batch_state["enabled"]:
                widget.setEnabled(was_enabled)
            stop_batch.setEnabled(False)
            if compared_index in self._results:
                self._show_result(compared_index, self._results[compared_index])
            failures = sum(not message.startswith("not_processed:") for message in self._herman_pair_failures.values())
            pending = len(self._herman_pair_failures) - failures
            successes = len(self._herman_pairs) - sum(i in self._herman_pairs for i in self._herman_pair_failures)
            label = "已停止" if batch_state["stop"] else "已完成"
            output.setText(f"整批比较{label}：处理 {batch_state['index']}/{len(self._curves)}，成功 {successes}，失败 {failures}，未处理 {pending}。")
        def step_batch() -> None:
            if not batch_state["running"]:
                return
            index = batch_state["index"]
            if batch_state["stop"] or index >= len(self._curves):
                finish_batch()
                return
            try:
                result = self._results.get(index) or self._fit_index(index, display=False)
                if result is None:
                    self._herman_pair_failures[index] = self._fit_failures.get(index, "first fit failed")
                else:
                    self._result, self._result_curve_index = result, index
                    try:
                        comparison, _ = self._compare_second_peak(batch_state["lower"], batch_state["upper"], average=batch_state["average"])
                        if comparison.first.f_fit is None or comparison.second.f_fit is None:
                            self._herman_pair_failures[index] = f"first={comparison.first.status}; second={comparison.second.status}"
                    except (ValueError, PeakFitError) as exc:
                        self._herman_pair_failures[index] = str(exc)
            except Exception as exc:
                self._herman_pair_failures[index] = f"unexpected error: {type(exc).__name__}: {exc}"
                batch_state["stop"] = True
            finally:
                self._result, self._result_curve_index = compared_fit, compared_index
            batch_state["index"] += 1
            output.setText(f"正在比较 {batch_state['index']}/{len(self._curves)}；可在当前样品完成后停止。")
            timer.start(0)
        timer.timeout.connect(step_batch)
        stop_batch.clicked.connect(lambda: batch_state.update(stop=True))
        dialog.finished.connect(finish_batch)
        def calculate_batch() -> None:
            if (self._result_curve_index != compared_index or self._result is not compared_fit or
                self.herman_domain_combo.currentData() != compared_domain):
                output.setText("当前曲线或拟合设置已变化，请关闭窗口并重新打开比较。")
                return
            self._herman_pairs.clear()
            self._herman_pair_failures.clear()
            batch_state.update(running=True, stop=False, index=0, lower=lower.value(),
                               upper=upper.value(), average=average.isChecked(),
                               enabled=[(widget, widget.isEnabled()) for widget in controlled])
            for widget in controlled:
                widget.setEnabled(False)
            stop_batch.setEnabled(True)
            timer.start(0)
        run.clicked.connect(calculate)
        run_batch.clicked.connect(calculate_batch)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _compare_second_peak_batch(self, lower: float, upper: float, *, average: bool = False) -> tuple[int, int]:
        original_index = self.curve_combo.currentIndex()
        self._herman_pairs.clear()
        self._herman_pair_failures.clear()
        for index in range(len(self._curves)):
            result = self._results.get(index)
            if result is None:
                result = self._fit_index(index, display=False)
            if result is None:
                self._herman_pair_failures[index] = self._fit_failures.get(index, "first fit failed")
                continue
            self._result = result
            self._result_curve_index = index
            try:
                comparison, _ = self._compare_second_peak(lower, upper, average=average)
                if comparison.first.f_fit is None or comparison.second.f_fit is None:
                    self._herman_pair_failures[index] = f"first={comparison.first.status}; second={comparison.second.status}"
            except (ValueError, PeakFitError) as exc:
                self._herman_pair_failures[index] = str(exc)
        if original_index in self._results:
            self._show_result(original_index, self._results[original_index])
        return len(self._herman_pairs) - sum(index in self._herman_pairs for index in self._herman_pair_failures), len(self._herman_pair_failures)

    def fit_batch(self) -> tuple[dict[int, PeakFitResult], dict[int, str]]:
        """Apply the current shared fit recipe to every curve in this workspace."""
        self._results.clear()
        self._fit_failures.clear()
        self._herman_pairs.clear()
        self._herman_pair_failures.clear()
        for index in range(len(self._curves)):
            self._fit_index(index, display=False)
        current = self.curve_combo.currentIndex()
        if current in self._results:
            self._show_result(current, self._results[current])
        self.export_button.setEnabled(bool(self._results or self._fit_failures))
        return dict(self._results), dict(self._fit_failures)

    def _fit_batch_from_button(self) -> None:
        if self._auto_batch_active:
            self._auto_batch_stop = True
            self.status_label.setText("将在当前样品完成后停止。")
            return
        if self._curves:
            self._batch_automatic = self.auto_check.isChecked()
            self._results.clear()
            self._fit_failures.clear()
            self._auto_fits.clear()
            self._auto_herman.clear()
            self._auto_failures.clear()
            self._herman_pairs.clear()
            self._herman_pair_failures.clear()
            self._auto_batch_pending = set(range(len(self._curves)))
            self._auto_batch_active = True
            self._auto_batch_stop = False
            self._auto_batch_widgets = (self.curve_combo, self.model_combo, self.peak_count_spin,
                self.component_models_button, self.background_combo, self.range_lower, self.range_upper, self.use_full_range_button,
                self.select_range_button, self.auto_check, self.auto_prominence, self.auto_peak_combo, self.fit_button,
                self.compare_button, self.export_button, self.herman_domain_combo,
                self.crystal_setup_button, self.crystal_scope, self.crystal_peaks, self.amorphous_peaks,
                self.crystal_button, self.crystal_export_check)
            self._auto_batch_enabled = [(widget, widget.isEnabled()) for widget in self._auto_batch_widgets]
            for widget in self._auto_batch_widgets:
                widget.setEnabled(False)
            self.fit_batch_button.setText("完成当前样品后停止")
            self.status_label.setToolTip("")
            self.status_label.setText(f"后台拟合 0/{len(self._curves)}")
            self._auto_batch_timer.start(0)
            return
        results, failures = self.fit_batch()
        self.status_label.setStyleSheet("" if not failures else "color: #8a5a00;")
        self.status_label.setText(
            f"整批技术拟合完成：成功 {len(results)}，失败 {len(failures)}；"
            "共享模型、背景、峰数和范围已应用到全部曲线。"
        )

    def _auto_batch_step(self) -> None:
        if not self._auto_batch_active:
            return
        if self._auto_batch_stop or not self._auto_batch_pending:
            self._auto_batch_active = False
            for widget, enabled in self._auto_batch_enabled:
                widget.setEnabled(enabled)
            self.fit_batch_button.setText("按共享设置拟合整批")
            current = self.curve_combo.currentIndex()
            if current in self._results:
                self._show_result(current, self._results[current])
            self.export_button.setEnabled(bool(self._results or self._fit_failures))
            partial = [(index, failures) for index, failures in self._auto_failures.items() if failures]
            failure_details = [f"{self._curves[index][0]}：{reason}" for index, reason in self._fit_failures.items()]
            for index, failures in partial:
                failure_details.extend(f"{self._curves[index][0]}，候选峰 {rank}：{reason}" for rank, reason in failures)
            self.status_label.setToolTip("\n".join(failure_details))
            self.status_label.setText(f"批处理：完成 {len(self._results)}，样品失败 {len(self._fit_failures)}，"
                                      f"候选峰失败 {sum(len(failures) for _, failures in partial)}，"
                                      f"未处理 {len(self._auto_batch_pending)}；失败详情见提示。")
            return
        index = min(self._auto_batch_pending)
        curve = self._curves[index][1]
        prominence = self.auto_prominence.value() / 100.0
        domain = str(self.herman_domain_combo.currentData())
        if self._batch_automatic:
            target = self._auto_batch_worker
            args = (index, curve, prominence, domain, self._auto_batch_signals)
        else:
            _, _, _, kwargs = self._fit_arguments_for(index)
            target = self._manual_batch_worker
            args = (index, curve, kwargs, self._component_models, self._component_center_bounds,
                    self._component_fwhm_bounds, self._auto_batch_signals)
        self._auto_batch_thread = threading.Thread(target=target, args=args,
            name="azimuth-auto-fit", daemon=True)
        self._auto_batch_thread.start()

    @staticmethod
    def _manual_batch_worker(index, curve, kwargs, models, center_bounds, fwhm_bounds, signals):
        fits, error = [], None
        try:
            kwargs = dict(kwargs)
            peak_count = int(kwargs.pop("peak_count"))
            fit_x = np.asarray(curve.x, dtype=float)
            lower, upper = kwargs["x_range"]
            if lower > upper:
                if curve.kind != "azimuthal_1d" or not (0 <= upper < lower < 360):
                    raise PeakFitError("递减范围仅适用于跨0°的方位角")
                fit_x = np.where(fit_x < lower, fit_x + 360., fit_x)
                kwargs["x_range"] = (lower, upper + 360.)
            y = PeakFitPanel._fit_intensity(curve)
            if peak_count == 1:
                fits = [fit_peak(fit_x, y, **kwargs)]
            else:
                fits = [fit_multi_peak(fit_x, y, peak_count=peak_count, models=models,
                                       center_bounds=center_bounds, fwhm_bounds=fwhm_bounds, **kwargs)]
        except Exception as exc:
            error = f"拟合失败：{type(exc).__name__}: {exc}"
        try:
            signals.finished.emit(index, fits, [], [], error)
        except RuntimeError:
            pass

    @staticmethod
    def _auto_batch_worker(index, curve, prominence, domain, signals):
        fits, failures, herman, error = [], [], [], None
        try:
            fits, failures = fit_all_automatic_azimuth(curve, shared_model=True, allow_masked=domain == "model_full_90", prominence_fraction=prominence)
            for item in fits:
                try:
                    herman.append(from_single_peak_fit(curve, item.result, domain=domain))
                except ValueError as exc:
                    herman.append(str(exc))
            if not fits:
                error = "自动识峰未得到可用拟合；请查看原始曲线并手动设置"
        except Exception as exc:
            error = f"自动处理异常：{type(exc).__name__}: {exc}"
        try:
            signals.finished.emit(index, fits, failures, herman, error)
        except RuntimeError:
            pass  # A parent application shutdown may already have destroyed the receiver.

    def _auto_batch_finished_item(self, index, fits, failures, herman, error):
        if not self._auto_batch_active:
            return
        self._auto_batch_thread = None
        if self._batch_automatic:
            self._auto_fits[index], self._auto_failures[index], self._auto_herman[index] = fits, failures, herman
        if error is not None:
            self._fit_failures[index] = error
        else:
            self._results[index] = fits[0].result if self._batch_automatic else fits[0]
            self._fit_failures.pop(index, None)
        self._auto_batch_pending.remove(index)
        self.status_label.setText(f"后台拟合 {len(self._curves) - len(self._auto_batch_pending)}/{len(self._curves)}")
        self._auto_batch_timer.start(0)

    def compare_models(self) -> None:
        if self._comparison_thread is not None:
            return
        try:
            _index, _name, curve, kwargs = self._fit_arguments()
            fit_x = np.asarray(curve.x, dtype=float)
            lower, upper = kwargs["x_range"]
            if lower > upper:
                if curve.kind != "azimuthal_1d" or not (0 <= upper < lower < 360):
                    raise PeakFitError("递减范围仅适用于跨0°的方位角，例如350到10")
                fit_x = np.where(fit_x < lower, fit_x + 360.0, fit_x)
                kwargs["x_range"] = (lower, upper + 360.0)
        except PeakFitError as exc:
            self.status_label.setText(f"模型比较失败：{exc}")
            self.status_label.setStyleSheet("color: #a11;")
            return
        if kwargs["peak_count"] == 1:
            try:
                comparison = compare_peak_models(fit_x, self._fit_intensity(curve),
                    background=kwargs["background"], x_range=kwargs["x_range"],
                    x_unit=kwargs["x_unit"], y_unit=kwargs["y_unit"])
            except PeakFitError as exc:
                self.status_label.setStyleSheet("color: #a11;")
                self.status_label.setText(f"模型比较失败：{exc}")
                return
            self._show_model_comparison(comparison)
            return
        self._comparison_request_id += 1
        request_id = self._comparison_request_id
        self._comparison_started_with = self._comparison_signature()
        self.comparison_table.setRowCount(0)
        self.compare_button.setEnabled(False)
        self.status_label.setStyleSheet("")
        self.status_label.setText("后台比较共用线形；固定当前峰数、约束、背景和拟合范围。")
        self._comparison_thread = threading.Thread(
            target=self._comparison_worker,
            args=(request_id, fit_x.copy(), self._fit_intensity(curve),
                  {"background": kwargs["background"], "x_range": kwargs["x_range"],
                   "x_unit": kwargs["x_unit"], "y_unit": kwargs["y_unit"],
                   "peak_count": kwargs["peak_count"],
                   "center_bounds": self._component_center_bounds,
                   "fwhm_bounds": self._component_fwhm_bounds}, self._comparison_signals),
            name="peak-model-comparison", daemon=True)
        self._comparison_thread.start()

    def _comparison_signature(self):
        index = self.curve_combo.currentIndex()
        return (index, id(self._curves[index][1]) if 0 <= index < len(self._curves) else None,
                self.peak_count_spin.value(), self.background_combo.currentData(),
                self.range_lower.value(), self.range_upper.value(),
                self._component_center_bounds, self._component_fwhm_bounds)

    @staticmethod
    def _comparison_worker(request_id, x, y, kwargs, signals):
        comparison, error = None, None
        try:
            comparison = compare_peak_models(x, y, **kwargs)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        try:
            signals.finished.emit(request_id, comparison, error)
        except RuntimeError:
            pass

    def _comparison_finished(self, request_id, comparison, error) -> None:
        if request_id != self._comparison_request_id:
            return
        self._comparison_thread = None
        self.compare_button.setEnabled(bool(self._curves))
        if self._comparison_started_with != self._comparison_signature():
            self.status_label.setText("比较期间样品或设置已更改；旧结果已丢弃，请重新比较。")
            return
        if error is not None:
            self.status_label.setStyleSheet("color: #a11;")
            self.status_label.setText(f"模型比较失败：{error}")
            return
        self._show_model_comparison(comparison)

    def _show_model_comparison(self, comparison) -> None:
        ranked = comparison.ranked_by_aic
        self.comparison_table.setRowCount(len(ranked) + len(comparison.failures))
        for row, result in enumerate(ranked):
            missing = int(result.input_counts.get("excluded_nonfinite_in_range", 0))
            review_count = len(result.warnings) + int(missing > 0)
            status = "技术收敛" if result.metrics["success"] else "未收敛"
            if review_count:
                status += f"；待复核{review_count}项"
            values = (
                result.model,
                f"{float(result.metrics['aic']):.7g}",
                f"{float(result.metrics['bic']):.7g}",
                f"{float(result.metrics['r_squared']):.7g}",
                status,
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if column == 4:
                    details = list(result.warnings)
                    if missing:
                        details.append(f"拟合范围内有 {missing} 个缺失强度点")
                    item.setToolTip("\n".join(details) if details else status)
                self.comparison_table.setItem(row, column, item)
        start = len(ranked)
        for offset, (model, reason) in enumerate(comparison.failures.items()):
            row = start + offset
            for column, value in enumerate((model, "", "", "", f"失败：{reason}")):
                self.comparison_table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        self.comparison_table.resizeColumnsToContents()
        self.comparison_section.set_expanded(True)
        self.status_label.setStyleSheet("")
        converged = sum(bool(result.metrics.get("success", False)) for result in ranked)
        self.status_label.setText(
            ("模型比较完成；先列收敛项，再按AIC排序。" if converged else "模型比较完成；没有收敛候选。")
            + "峰归属和当前模型均未自动更改。"
        )

    def _select_plot_peak(self, number, *, additive=None):
        index = self.curve_combo.currentIndex()
        fits = self._auto_fits.get(index, [])
        if not 0 <= number < len(fits):
            return
        if additive is None:
            additive = bool(QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.KeyboardModifier.ControlModifier)
        chosen = set(self._selected_peaks.get(index, set())) if additive else set()
        if additive and number in chosen and len(chosen) > 1:
            chosen.remove(number)
        else:
            chosen.add(number)
        self._selected_peaks[index] = chosen
        self._refresh_peak_selection()

    def _sync_peak_checkboxes(self, index, count):
        if self._peak_checkbox_curve == index and len(self._peak_checkboxes) == count:
            return
        for checkbox in self._peak_checkboxes:
            self.peak_checks_layout.removeWidget(checkbox)
            checkbox.hide()
            checkbox.deleteLater()
        self._peak_checkboxes = []
        self._peak_checkbox_curve = index
        for number in range(count):
            checkbox = QtWidgets.QCheckBox(f"峰 {number+1}")
            checkbox.setLayoutDirection(QtCore.Qt.LayoutDirection.RightToLeft)
            checkbox.toggled.connect(lambda checked, n=number: self._peak_checkbox_toggled(n, checked))
            self.peak_checks_layout.addWidget(checkbox)
            self._peak_checkboxes.append(checkbox)

    def _peak_checkbox_toggled(self, number, checked):
        index = self.curve_combo.currentIndex()
        chosen = self._selected_peaks.setdefault(index, set())
        if checked:
            chosen.add(number)
        else:
            chosen.discard(number)
        self._refresh_peak_selection()

    def _refresh_peak_selection(self):
        index = self.curve_combo.currentIndex()
        selected = self._selected_peaks.get(index, set())
        for number, checkbox in enumerate(self._peak_checkboxes):
            with QtCore.QSignalBlocker(checkbox):
                checkbox.setChecked(number in selected)
        for number, line in enumerate(self._peak_plot_lines):
            pen = pg.mkPen(line.opts["pen"])
            pen.setWidthF(3 if number in selected else 1)
            line.setPen(pen)
        self.peak_selection_label.setText("已选：" + (", ".join(str(i+1) for i in sorted(selected)) or "无"))
        self.apply_peak_model_button.setEnabled(bool(selected))

    def select_all_peaks(self):
        index = self.curve_combo.currentIndex()
        fits = self._auto_fits.get(index, [])
        if fits:
            self._selected_peaks[index] = set(range(len(fits)))
            self._sync_peak_checkboxes(index, len(fits))
            self._refresh_peak_selection()

    def fit_selected_peaks(self):
        index = self.curve_combo.currentIndex()
        fits = self._auto_fits.get(index, [])
        selected = sorted(self._selected_peaks.get(index, set(range(len(fits)))))
        if not fits or not selected:
            self.status_label.setText("请先自动识峰，再选择要拟合的峰。")
            return
        curve = self._curves[index][1]
        pending = list(fits)
        model = self.peak_model_combo.currentData()
        try:
            for number in selected:
                old = fits[number].result
                updated = fit_peak(old.used_x, old.observed_y, model=model, background=old.background,
                                   x_range=old.requested_range, x_unit=old.x_unit, y_unit=old.y_unit)
                pending[number] = replace(fits[number], result=updated,
                    warnings=fits[number].warnings + (f"人工峰策略：{model}",))
        except ValueError as exc:
            self.status_label.setText(f"选中峰拟合失败，保留上次结果：{exc}")
            return
        self._auto_fits[index] = pending
        self._calculate_all_auto_herman(index)
        self._results[index] = pending[selected[0]].result
        self._show_result(index, pending[selected[0]].result)
        return pending[selected[0]].result

    def export_peak_selection(self, path, *, all_peaks=False):
        from openpyxl import Workbook
        path = Path(path).expanduser().resolve()
        protected = (Path(__file__).resolve().parents[2] / "data").resolve()
        if path == protected or protected in path.parents:
            raise ValueError("不能写入原始数据目录")
        if path.suffix.lower() != ".xlsx":
            raise ValueError("峰数据工作簿使用.xlsx格式")
        index = self.curve_combo.currentIndex()
        fits = self._auto_fits.get(index, [])
        selected = list(range(len(fits))) if all_peaks else sorted(self._selected_peaks.get(index, set()))
        if not selected:
            raise ValueError("请选择至少一个已拟合的峰")
        book = Workbook()
        summary = book.active
        summary.title = "Peak Summary"
        summary.append(["Sample", "Peak", "Model", "Center (deg)", "FWHM (deg)", "R2", "Herman", "Domain", "Note"])
        params = book.create_sheet("Parameters")
        params.append(["Sample", "Peak", "Parameter", "Value", "StdErr", "Unit"])
        curves = book.create_sheet("Curves")
        columns = []
        for number in selected:
            result = fits[number].result
            herman = from_single_peak_fit(self._curves[index][1], result, domain=self.herman_domain_combo.currentData())
            name = self._curves[index][0]
            summary.append([name, number+1, result.model, result.parameters["center"].value % 360,
                result.parameters["fwhm"].value, result.metrics["r_squared"], herman.f_fit, herman.domain, herman.message])
            for key, parameter in result.parameters.items():
                params.append([name, number+1, key, parameter.value, parameter.stderr, parameter.unit])
            order = np.argsort(result.used_x % 360)
            columns.append((result.used_x[order] % 360, result.observed_y[order], result.best_fit_y[order], result.residual_y[order]))
        curves.append([f"Peak {n+1} {label}" for n in selected for label in ("Angle (deg)", "Observed", "Fit", "Residual")])
        for row in range(max(len(col[0]) for col in columns)):
            curves.append([float(array[row]) if row < len(array) else None for col in columns for array in col])
        info = book.create_sheet("Processing Info")
        info.append(["Sample", self._curves[index][0]])
        info.append(["Context", json.dumps(self._parameter_contexts[index], ensure_ascii=False, default=str)])
        path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with path.open("xb") as stream:
                created = True
                book.save(stream)
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise
        finally:
            book.close()
        return path

    def _choose_peak_export(self, all_peaks):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出当前样品的峰", "", "Excel (*.xlsx)")
        if path:
            try:
                self.export_peak_selection(path, all_peaks=all_peaks)
                self.status_label.setText("已导出最终峰策略、参数、Herman和曲线数据。")
            except (ValueError, OSError) as exc:
                self.status_label.setText(f"峰导出失败：{exc}")

    def _peak_image_snapshot(self, all_peaks=False):
        index = self.curve_combo.currentIndex()
        fits = self._auto_fits.get(index, [])
        numbers = list(range(len(fits))) if all_peaks else sorted(self._selected_peaks.get(index, set()))
        if not numbers:
            raise ValueError("请选择至少一个已拟合的峰")
        curves = [(f"{Path(self._curves[index][0]).stem}_peak_{number+1}", self._curves[index][1]) for number in numbers]
        results = {position: fits[number].result for position, number in enumerate(numbers)}
        context = {"shared": self._parameter_contexts[index], "peak_selection": [n+1 for n in numbers],
                   "sample": self._curves[index][0]}
        return curves, results, {}, context

    def _choose_peak_images(self, all_peaks):
        try:
            snapshot = self._peak_image_snapshot(all_peaks)
            fmt, accepted = QtWidgets.QInputDialog.getItem(self, "图片格式", "格式", ("PNG", "TIFF", "PDF", "SVG"), 0, False)
            if not accepted:
                return
            directory = choose_export_folder(self, "导出选定峰拟合图")
            if directory:
                self.start_image_export(directory, list(range(len(snapshot[0]))), fmt.lower(), snapshot=snapshot)
        except ValueError as exc:
            self.status_label.setText(str(exc))

    def _azimuth_view_changed(self, *_):
        if self._result is not None and self._result_curve_index is not None:
            self._render_result(self._curves[self._result_curve_index][0], self._result)

    def _render_full_azimuth(self, name, curve, result):
        self._fit_range_region = None
        self.fit_plot.clear()
        self.residual_plot.clear()
        x = np.asarray(curve.x) % 360.
        y = np.asarray(curve.intensity, dtype=float).copy()
        y[np.asarray(curve.count) <= 0] = np.nan
        order = np.argsort(x)
        self.fit_plot.plot(x[order], y[order], pen=None, symbol="o", symbolSize=3,
                           symbolBrush="#555555", symbolPen=None, name=name)
        fits = self._auto_fits.get(self._result_curve_index, []) if self.auto_check.isChecked() else []
        results = [item.result for item in fits] or [result]
        colors = ("#D1495B", "#277DA1", "#59A14F", "#B07AA1")
        for index, fitted in enumerate(results):
            angles = fitted.used_x % 360.
            idx = np.argsort(angles)
            xx, yy = angles[idx], fitted.best_fit_y[idx]
            rr = fitted.residual_y[idx]
            breaks = np.where(np.diff(xx) > 180)[0]+1
            xx, yy, rr = (np.insert(array, breaks, np.nan) for array in (xx, yy, rr))
            selected = index in self._selected_peaks.get(self._result_curve_index, set(range(len(results))))
            line = self.fit_plot.plot(xx, yy, pen=pg.mkPen(colors[index % len(colors)], width=3 if selected else 1), name=f"峰 {index+1} Fit")
            self._peak_plot_lines.append(line)
            self.residual_plot.plot(xx, rr, pen=pg.mkPen(colors[index % len(colors)]))
            center = fitted.parameters["center"].value % 360.
            label = pg.TextItem(f"峰 {index+1}", color=colors[index % len(colors)], anchor=(.5, 1))
            label.setPos(center, float(np.max(fitted.best_fit_y)))
            self.fit_plot.addItem(label, ignoreBounds=True)
        self.fit_plot.setLabel("left", f"I ({result.y_unit})")
        self.residual_plot.setLabel("bottom", "方位角 φ (°)")
        ticks = [[(angle, str(angle)) for angle in range(0, 361, 60)]]
        self.fit_plot.getAxis("bottom").setTicks(ticks)
        self.residual_plot.getAxis("bottom").setTicks(ticks)
        self.fit_plot.enableAutoRange()
        self.fit_plot.setXRange(0, 360, padding=0)
        self.residual_plot.setXRange(0, 360, padding=0)

    def _render_result(self, name: str, result: PeakFitResult) -> None:
        self._peak_plot_lines = []
        self.fit_plot.getAxis("bottom").setTicks(None)
        self.residual_plot.getAxis("bottom").setTicks(None)
        if self._result_curve_index is not None:
            curve = self._curves[self._result_curve_index][1]
            if curve.kind == "azimuthal_1d" and self.azimuth_view_combo.currentData() == "full":
                self._render_full_azimuth(name, curve, result)
                return
        self._fit_range_region = None
        self.fit_plot.clear()
        self.residual_plot.clear()
        self.fit_plot.plot(
            result.used_x,
            result.observed_y,
            pen=None,
            symbol="o",
            symbolSize=4,
            symbolBrush=pg.mkBrush("#555555"),
            symbolPen=pg.mkPen(None),
            name=name,
        )
        self.fit_plot.plot(
            result.used_x,
            result.best_fit_y,
            pen=pg.mkPen("#D1495B", width=2.4),
            name="Fit",
        )
        component_colors = (
            "#4C78A8",
            "#59A14F",
            "#F28E2B",
            "#B07AA1",
            "#76B7B2",
            "#E15759",
        )
        component_background = (
            result.best_fit_y - np.sum(np.stack(list(result.components.values())), axis=0)
            if result.components else None
        )
        for component_index, (component_name, component_y) in enumerate(
            result.components.items()
        ):
            self.fit_plot.plot(
                result.used_x,
                component_y + component_background,
                pen=pg.mkPen(
                    component_colors[component_index % len(component_colors)],
                    width=1.2,
                    style=QtCore.Qt.PenStyle.DashLine,
                ),
                name=f"Peak {component_index + 1} + background",
            )
        if component_background is not None:
            self.fit_plot.plot(result.used_x, component_background,
                               pen=pg.mkPen("#777777", width=1, style=QtCore.Qt.PenStyle.DotLine),
                               name="Background")
        self.residual_plot.plot(
            result.used_x,
            result.residual_y,
            pen=pg.mkPen("#35618D", width=1.5),
        )
        self.residual_plot.addLine(y=0.0, pen=pg.mkPen("#777777", width=0.8))
        self.fit_plot.setLabel("left", f"I ({result.y_unit})")
        axis_label = f"x ({result.x_unit})"
        if result.requested_range[1] > 360 and result.x_unit == "deg":
            axis_label += " — 0°以上展开为360°以上"
        self.residual_plot.setLabel("bottom", axis_label)
        self.fit_plot.enableAutoRange()
        self.residual_plot.enableAutoRange()

    def _populate_parameter_table(self, result: PeakFitResult) -> None:
        self.parameter_table.setRowCount(len(result.parameters))
        for row, (name, parameter) in enumerate(result.parameters.items()):
            stderr = "—" if parameter.stderr is None else f"{parameter.stderr:.7g}"
            values = (name, f"{parameter.value:.9g}", stderr, parameter.unit)
            for column, value in enumerate(values):
                self.parameter_table.setItem(
                    row, column, QtWidgets.QTableWidgetItem(str(value))
                )
        self.parameter_table.resizeColumnsToContents()
        metrics = result.metrics
        self.metrics_label.setText(
            " | ".join(
                (
                    f"n={metrics['data_points']}",
                    f"RMSE={float(metrics['rmse']):.6g}",
                    f"R²={float(metrics['r_squared']):.6g}",
                    f"AIC={float(metrics['aic']):.6g}",
                    f"BIC={float(metrics['bic']):.6g}",
                )
            )
        )
        self.result_section.set_expanded(True)

    def _clear_result_display(self) -> None:
        self._peak_details_key = None
        self.peak_overview.setRowCount(0)
        self.peak_overview.hide()
        self.peak_results_tabs.hide()
        self.herman_label.setText("当前无有效拟合结果。")
        self.fit_plot.clear()
        self.residual_plot.clear()
        self.parameter_table.setRowCount(0)
        self.metrics_label.clear()
        self.comparison_table.setRowCount(0)

    @staticmethod
    def _parameter_parts(name: str) -> tuple[str, str, str]:
        """Return a stable category, peak label and readable parameter name."""
        match = re.fullmatch(r"peak_(\d+)_(.+)", name)
        peak = f"Peak {match.group(1)}" if match else "Peak 1"
        base = match.group(2) if match else name
        labels = {
            "center": "Center",
            "fwhm": "FWHM",
            "sigma": "Sigma",
            "gamma": "Gamma",
            "area": "Area",
            "height": "Height",
            "fraction": "Fraction",
            "exponent": "Exponent",
            "width_left": "Left Width",
            "width_right": "Right Width",
            "background_intercept": "Background Intercept",
            "background_slope": "Background Slope",
        }
        if base.startswith("background_"):
            return "Background", "", labels.get(base, base.replace("_", " ").title())
        if base == "center":
            category = "Peak Position"
        elif base in {"fwhm", "sigma", "gamma", "width_left", "width_right"}:
            category = "Peak Width"
        elif base == "area":
            category = "Peak Area"
        elif base == "height":
            category = "Peak Height"
        elif base in {"fraction", "exponent"}:
            category = "Peak Shape"
        else:
            category = "Other"
        return category, peak, labels.get(base, base.replace("_", " ").title())

    @classmethod
    def _summary_parameter_label(cls, name: str) -> str:
        _category, peak, label = cls._parameter_parts(name)
        return f"{peak} {label}".strip()

    @staticmethod
    def _flatten_context(
        value: Any, *, prefix: str = ""
    ) -> list[tuple[str, Any]]:
        rows: list[tuple[str, Any]] = []
        if isinstance(value, Mapping):
            for key in sorted(value, key=str):
                child = f"{prefix}.{key}" if prefix else str(key)
                rows.extend(PeakFitPanel._flatten_context(value[key], prefix=child))
        elif isinstance(value, (list, tuple)):
            rows.append((prefix, json.dumps(value, ensure_ascii=False)))
        else:
            rows.append((prefix, value))
        return rows

    def export_current_fit(self, path: str | Path) -> Path:
        if self._result is None or self._result_curve_index is None:
            raise PeakFitError("run a fit before exporting")
        target = Path(path)
        if target.suffix.lower() != ".xlsx":
            raise PeakFitError("peak-fit workbook export requires .xlsx")
        return self._export_fit_workbook(target, [self._result_curve_index])

    def _source_context_summary(self, indices: list[int]) -> dict[str, Any]:
        contexts = [dict(self._parameter_contexts[index]) for index in indices]
        if not contexts:
            return {"shared": {}, "per_curve_differences": {}}
        shared: dict[str, Any] = {}
        for key in contexts[0]:
            value = contexts[0][key]
            if all(key in context and context[key] == value for context in contexts[1:]):
                shared[key] = value
        differences: dict[str, Any] = {}
        names = [self._curves[index][0] for index in indices]
        reserved = set(names)
        for index, context in zip(indices, contexts, strict=True):
            difference = {key: value for key, value in context.items() if key not in shared}
            if difference:
                name = self._curves[index][0]
                label = name
                if names.count(name) > 1:
                    base = f"{name} [curve {index + 1}]"
                    label, suffix = base, 2
                    while label in reserved:
                        label, suffix = f"{base} #{suffix}", suffix + 1
                    reserved.add(label)
                differences[label] = difference
        return {"shared": shared, "per_curve_differences": differences}

    def export_fit_bundle(
        self, directory: str | Path, indices: Iterable[int]
    ) -> Path:
        """Export one Origin-friendly XLSX workbook for the requested curves."""
        ordered = sorted({int(index) for index in indices})
        if not ordered:
            raise PeakFitError("no curves were selected for export")
        for index in ordered:
            if index < 0 or index >= len(self._curves):
                raise PeakFitError("fit export selection is out of range")
            if index in self._auto_batch_pending:
                continue
            if index not in self._results and index not in self._fit_failures:
                pair_failure = self._herman_pair_failures.get(index)
                self._fit_index(index, display=False)
                if pair_failure is not None:
                    self._herman_pair_failures[index] = pair_failure
        successful = [index for index in ordered if index in self._results]
        if not successful and not any(index in self._fit_failures for index in ordered):
            raise PeakFitError("all selected curves failed fitting")
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        prefix = (
            f"{Path(self._curves[ordered[0]][0]).stem}_peak_fit"
            if len(ordered) == 1
            else "batch_peak_fit"
        )
        return self._export_fit_workbook(root / f"{prefix}.xlsx", ordered)

    def _export_fit_workbook(self, target: Path, ordered: Iterable[int]) -> Path:
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise PeakFitError(
                "XLSX export requires openpyxl; install the locked UI dependencies"
            ) from exc

        selected = [int(index) for index in ordered]
        if not selected:
            raise PeakFitError("no curves were selected for export")
        for index in selected:
            if index < 0 or index >= len(self._curves):
                raise PeakFitError("fit export selection is out of range")
            if index in self._auto_batch_pending:
                continue
            if index not in self._results and index not in self._fit_failures:
                pair_failure = self._herman_pair_failures.get(index)
                self._fit_index(index, display=False)
                if pair_failure is not None:
                    self._herman_pair_failures[index] = pair_failure
        successful = [index for index in selected if index in self._results]
        if not successful and not any(index in self._fit_failures for index in selected):
            raise PeakFitError("all selected curves failed fitting")
        if target.exists():
            raise FileExistsError("fit export would overwrite an existing file")
        target.parent.mkdir(parents=True, exist_ok=True)

        workbook = Workbook()
        summary_sheet = workbook.active
        summary_sheet.title = "Fit Summary"
        curves_sheet = workbook.create_sheet("Curves")
        parameters_sheet = workbook.create_sheet("Parameters")
        info_sheet = workbook.create_sheet("Processing Info")
        imported = [(index, self._curves[index][1]) for index in selected
                    if self._curves[index][1].audit.get("source_type") == "external_1d"]
        if imported:
            source_sheet = workbook.create_sheet("Imported Curves")
            for sample_index, (index, curve) in enumerate(imported):
                column = sample_index * 4 + 1
                name = self._curves[index][0]
                for offset, (label, unit) in enumerate((("x", curve.x_unit), ("intensity", curve.intensity_unit),
                                                       ("sigma", curve.intensity_unit), ("valid", "1"))):
                    source_sheet.cell(1, column + offset, name)
                    source_sheet.cell(2, column + offset, label)
                    source_sheet.cell(3, column + offset, unit)
                sigma = curve.sigma if curve.sigma is not None else np.full(len(curve.x), np.nan)
                for row, values in enumerate(zip(curve.x, curve.intensity, sigma, curve.count), 4):
                    for offset, value in enumerate(values):
                        if np.isfinite(value):
                            source_sheet.cell(row, column + offset, float(value))
        herman_sheet = workbook.create_sheet("Herman") if self.herman_export_check.isChecked() else None
        herman_input_sheet = workbook.create_sheet("Herman Input") if herman_sheet is not None else None
        auto_sheet = workbook.create_sheet("Automatic Peaks") if self.auto_check.isChecked() else None
        crystal_sheet = workbook.create_sheet("Crystallinity") if self.crystal_export_check.isChecked() else None
        crystal_peaks_sheet = workbook.create_sheet("Crystallinity Peaks") if crystal_sheet is not None else None
        if crystal_sheet is not None:
            crystal_sheet.append(["Sample", "Status", "Fraction", "Crystalline Area", "Amorphous Area",
                                  "Area Unit", "Range Start", "Range End", "X Unit", "Model",
                                  "Background", "Assignments", "Component Areas", "Warnings", "Area Calculation",
                                  "Method ID"])
            crystal_peaks_sheet.append(["Sample", "Peak", "Classification", "Center", "Center Unit",
                                        "Finite Range Area", "Area Unit", "Range Start", "Range End",
                                        "Peak Model", "Background", "Status"])
            for index in selected:
                name = self._curves[index][0]
                if index not in self._results:
                    status = "not_processed" if index in self._auto_batch_pending else "fit_failed"
                    reason = "not processed" if status == "not_processed" else self._fit_failures.get(index, "unknown")
                    crystal_sheet.append([name, status, None, None, None, None, None, None,
                                          None, None, None, None, None,
                                          reason, None, CRYSTALLINITY_METHOD_ID])
                    continue
                try:
                    crystal = self._crystallinity_for_index(index)
                    crystal_sheet.append([name, "candidate", crystal.fraction, crystal.crystalline_area,
                        crystal.amorphous_area, crystal.area_unit, *crystal.integration_range, crystal.x_unit,
                        crystal.fit_model, crystal.background_model, json.dumps(crystal.classifications),
                        json.dumps(crystal.component_areas), " | ".join(crystal.warnings), crystal.area_method,
                        crystal.method_id])
                    peak_fit = self._results[index]
                    if peak_fit.model.startswith("mixed:"):
                        models = peak_fit.model.removeprefix("mixed:").split(",")
                    else:
                        models = [peak_fit.model.rsplit("_x", 1)[0]] * len(crystal.component_areas)
                    for number, (peak_name, area) in enumerate(crystal.component_areas.items()):
                        center = peak_fit.parameters.get(f"{peak_name}_center")
                        crystal_peaks_sheet.append([name, peak_name, crystal.classifications[peak_name],
                            None if center is None else center.value, crystal.x_unit, area, crystal.area_unit,
                            *crystal.integration_range, models[number], crystal.background_model, "candidate"])
                except ValueError as exc:
                    crystal_sheet.append([name, "not_calculated", None, None, None, None, None,
                                          None, None, None, None, None, None, str(exc), None,
                                          CRYSTALLINITY_METHOD_ID])
        pair_indices = [index for index in selected if index in self._herman_pairs]
        pair_failure_indices = [index for index in selected if index in self._herman_pair_failures and index not in self._herman_pairs]
        pair_sheet = workbook.create_sheet("Herman Pairs") if herman_sheet is not None and (pair_indices or pair_failure_indices) else None
        extra_results = [(index, self._herman_pairs[index][1],
                          f"{self._curves[index][0]} — second peak") for index in pair_indices]
        if auto_sheet is not None:
            extra_results.extend((index, item.result, f"{self._curves[index][0]} — automatic peak {number}")
                for index in successful for number, item in enumerate(self._auto_fits.get(index, []), start=1)
                if item.result is not self._results[index])

        header_fill = PatternFill("solid", fgColor="1F6D7A")
        header_font = Font(color="FFFFFF", bold=True)
        subheader_fill = PatternFill("solid", fgColor="DDECEF")

        parameter_names = sorted(
            {
                name
                for index in successful
                for name in self._results[index].parameters
            },
            key=lambda name: self._summary_parameter_label(name),
        )
        fixed_headers = [
            "Sample",
            "Status",
            "Model",
            "Peak Count",
            "Background",
            "X Min",
            "X Max",
            "Data Points",
            "Degrees of Freedom",
            "RMSE",
            "Reduced Chi-square",
            "R²",
            "AIC",
            "BIC",
            "Warnings",
        ]
        parameter_headers = [
            value
            for name in parameter_names
            for value in (
                self._summary_parameter_label(name),
                f"{self._summary_parameter_label(name)} StdErr",
                f"{self._summary_parameter_label(name)} Unit",
            )
        ]
        summary_sheet.append(fixed_headers + parameter_headers)
        if auto_sheet is not None:
            auto_sheet.append(["Sample", "Peak", "Status", "Detected Angle (deg)", "Fit Center (deg)",
                               "Range Start (deg)", "Range End (deg)", "Model", "AIC", "R²",
                               "FWHM (deg)", "Herman Fit", "Herman Points", "Negative Points",
                               "Calculation Domain", "Candidate AIC", "Warnings", "FWHM StdErr (deg)",
                               "Center StdErr (deg)", "RMSE"])
            for index in selected:
                curve_name, curve = self._curves[index]
                for number, item in enumerate(self._auto_fits.get(index, []), start=1):
                    h = self._auto_herman[index][number - 1]
                    if not isinstance(h, str):
                        status, fit_value, point_value, negative = h.status, h.f_fit, h.f_points, h.negative_points
                    else:
                        status, fit_value, point_value, negative = f"error: {h}", None, None, None
                    fwhm = item.result.parameters.get("fwhm")
                    auto_sheet.append([curve_name, number, status, item.peak_angle,
                        item.result.parameters["center"].value % 360.0, *item.selection_range,
                        item.result.model, item.result.metrics.get("aic"), item.result.metrics.get("r_squared"),
                        None if fwhm is None else fwhm.value, fit_value, point_value, negative,
                        self.herman_domain_combo.currentData(),
                        " | ".join(f"{name}: {aic:.8g}" for name, aic in item.candidate_aic),
                        " | ".join(item.warnings), None if fwhm is None else fwhm.stderr,
                        item.result.parameters["center"].stderr, item.result.metrics.get("rmse")])
                for rank, reason in self._auto_failures.get(index, []):
                    auto_sheet.append([curve_name, f"candidate {rank}", "fit_failed", None, None, None,
                                       None, None, None, None, None, None, None, None, None, None, reason])
                if not self._auto_fits.get(index) and not self._auto_failures.get(index):
                    status = "not_processed" if index in self._auto_batch_pending else "fit_failed"
                    reason = "batch stopped" if status == "not_processed" else self._fit_failures.get(index, "automatic fit unavailable")
                    auto_sheet.append([curve_name, None, status, None, None, None, None, None,
                                       None, None, None, None, None, None, None, None,
                                       reason])
        if herman_sheet is not None:
            herman_sheet.append(["Sample", "Status", "Domain", "Local Min (deg)",
                                 "Local Max (deg)", "Herman Fit", "Herman Points",
                                 "Fit - Points", "Negative Points", "Message",
                                 "Peak Center Original (deg)", "Fit Start Original (deg)",
                                 "Fit End Original (deg)", "Fit Range Crosses 0",
                                 "Peak Model", "FWHM (deg)", "Herman StdErr", "Uncertainty Status"])
            for index in selected:
                name, curve = self._curves[index]
                if index not in self._results:
                    status = "not_processed" if index in self._auto_batch_pending else "fit_failed"
                    herman_sheet.append([name, status, self.herman_domain_combo.currentData(), None, None, None, None,
                                         None, None, self._fit_failures.get(index, "unknown")])
                    continue
                try:
                    herman = from_single_peak_fit(curve, self._results[index], domain=str(self.herman_domain_combo.currentData()))
                except ValueError as exc:
                    herman_sheet.append([name, "unsupported", self.herman_domain_combo.currentData(), None, None, None, None,
                                         None, None, str(exc)])
                    continue
                difference = (herman.f_fit - herman.f_points
                              if herman.f_fit is not None and herman.f_points is not None else None)
                fit = self._results[index]
                center = fit.parameters.get("center")
                fwhm = fit.parameters.get("fwhm")
                fit_start, fit_end = fit.requested_range
                herman_sheet.append([name, herman.status, herman.domain, *herman.coverage_deg,
                                     herman.f_fit, herman.f_points, difference,
                                     herman.negative_points, herman.message,
                                     None if center is None else center.value % 360.0,
                                     fit_start % 360.0, fit_end - 360.0 if fit_end > 360.0 else fit_end,
                                     fit_end > 360.0, fit.model,
                                     None if fwhm is None else fwhm.value, None, "not estimated"])
        if pair_sheet is not None:
            pair_sheet.append(["Sample", "First Status", "Second Status", "Domain",
                               "First Fit", "Second Fit", "Fit Difference", "Mean Fit",
                               "First Points", "Second Points", "Points Difference", "Mean Points",
                               "First Local Min", "First Local Max", "Second Local Min", "Second Local Max",
                               "Second Center Original (deg)", "Second Fit Start (deg)",
                               "Second Fit End (deg)", "Second FWHM (deg)", "Second Model", "Failure Reason"])
            for index in pair_indices:
                comparison, second_fit = self._herman_pairs[index]
                a, b = comparison.first, comparison.second
                lo, hi = second_fit.requested_range
                fwhm = second_fit.parameters.get("fwhm")
                pair_sheet.append([self._curves[index][0], a.status, b.status, a.domain,
                    a.f_fit, b.f_fit, comparison.fit_difference, comparison.mean_fit,
                    a.f_points, b.f_points, comparison.points_difference, comparison.mean_points,
                    *a.coverage_deg, *b.coverage_deg,
                    second_fit.parameters["center"].value % 360, lo % 360,
                    hi - 360 if hi > 360 else hi, None if fwhm is None else fwhm.value,
                    second_fit.model, self._herman_pair_failures.get(index, "")])
            for index in pair_failure_indices:
                reason = self._herman_pair_failures[index]
                status = "not_processed" if reason.startswith("not_processed:") else "failed"
                row = [self._curves[index][0], status] + [None] * 19
                pair_sheet.append(row + [self._herman_pair_failures[index]])

        if herman_input_sheet is not None:
            column = 1
            inputs = [(index, self._results[index], self._curves[index][0]) for index in successful]
            inputs.extend(extra_results)
            for index, result, input_name in inputs:
                curve = self._curves[index][1]
                if curve.kind != "azimuthal_1d" or "center" not in result.parameters or result.components:
                    continue
                source_x = np.asarray(curve.x, dtype=float)
                fit_x = source_x.copy()
                lo, hi = result.requested_range
                if hi > 360.0:
                    fit_x = np.where(fit_x < lo, fit_x + 360.0, fit_x)
                chosen = np.isfinite(fit_x) & (fit_x >= lo) & (fit_x <= hi)
                columns = [source_x[chosen], np.asarray(curve.intensity)[chosen],
                           np.asarray(curve.count)[chosen]]
                for offset, (label, unit, designation) in enumerate((
                    ("source_angle", "deg", "X"),
                    ("source_intensity", result.y_unit, "Y"),
                    ("integration_count", "weighted contributions", "Y"),
                )):
                    herman_input_sheet.cell(1, column + offset, input_name)
                    herman_input_sheet.cell(2, column + offset, label)
                    herman_input_sheet.cell(3, column + offset, unit)
                    herman_input_sheet.cell(4, column + offset, designation)
                    for row_index, value in enumerate(columns[offset], start=5):
                        value = float(value)
                        if np.isfinite(value):
                            herman_input_sheet.cell(row_index, column + offset, value)
                column += 3
        for index in selected:
            curve_name = self._curves[index][0]
            if index not in self._results:
                status = "not_processed" if index in self._auto_batch_pending else f"failed: {self._fit_failures.get(index, 'unknown')}"
                summary_sheet.append(
                    [curve_name, status]
                )
                continue
            result = self._results[index]
            row: list[Any] = [
                curve_name,
                "technical_fit",
                result.model,
                1 if auto_sheet is not None else int(self.peak_count_spin.value()),
                result.background,
                result.requested_range[0],
                result.requested_range[1],
                result.metrics.get("data_points"),
                result.metrics.get("degrees_of_freedom"),
                result.metrics.get("rmse"),
                result.metrics.get("reduced_chi_square"),
                result.metrics.get("r_squared"),
                result.metrics.get("aic"),
                result.metrics.get("bic"),
                " | ".join(result.warnings),
            ]
            for name in parameter_names:
                parameter = result.parameters.get(name)
                row.extend(
                    ("", "", "")
                    if parameter is None
                    else (parameter.value, parameter.stderr, parameter.unit)
                )
            summary_sheet.append(row)

        column = 1
        for index in successful:
            result = self._results[index]
            curve_name = self._curves[index][0]
            series = [
                ("x", result.used_x, result.x_unit, "X"),
                ("observed", result.observed_y, result.y_unit, "Y"),
                ("best_fit", result.best_fit_y, result.y_unit, "Y"),
                ("residual", result.residual_y, result.y_unit, "Y"),
            ]
            series.extend(
                (name, values, result.y_unit, "Y")
                for name, values in result.components.items()
            )
            if (herman_sheet is not None and self._curves[index][1].kind == "azimuthal_1d"
                and "center" in result.parameters and not result.components):
                local = ((result.used_x - result.parameters["center"].value + 180.0) % 360.0) - 180.0
                background = np.zeros_like(result.used_x)
                if result.background != "none":
                    background += result.parameters["background_intercept"].value
                if result.background == "linear":
                    background += result.parameters["background_slope"].value * (result.used_x - np.mean(result.used_x))
                series.extend([
                    ("local_angle", local, "deg", "X"),
                    ("fit_background", background, result.y_unit, "Y"),
                    ("observed_minus_background", result.observed_y - background, result.y_unit, "Y"),
                    ("fitted_peak", result.best_fit_y - background, result.y_unit, "Y"),
                ])
            for offset, (label, _values, unit, designation) in enumerate(series):
                curves_sheet.cell(1, column + offset, curve_name)
                curves_sheet.cell(2, column + offset, label)
                curves_sheet.cell(3, column + offset, unit)
                curves_sheet.cell(4, column + offset, designation)
            for point_index in range(result.used_x.size):
                for offset, (_label, values, _unit, _designation) in enumerate(series):
                    curves_sheet.cell(5 + point_index, column + offset, float(values[point_index]))
            column += len(series)

        if extra_results:
            for index, result, name in extra_results:
                local = ((result.used_x - result.parameters["center"].value + 180.0) % 360.0) - 180.0
                background = np.zeros_like(result.used_x)
                if result.background != "none":
                    background += result.parameters["background_intercept"].value
                if result.background == "linear":
                    background += result.parameters["background_slope"].value * (result.used_x - np.mean(result.used_x))
                series = [
                    ("x", result.used_x, result.x_unit, "X"),
                    ("observed", result.observed_y, result.y_unit, "Y"),
                    ("best_fit", result.best_fit_y, result.y_unit, "Y"),
                    ("residual", result.residual_y, result.y_unit, "Y"),
                    ("local_angle", local, "deg", "X"),
                    ("fit_background", background, result.y_unit, "Y"),
                    ("observed_minus_background", result.observed_y - background, result.y_unit, "Y"),
                    ("fitted_peak", result.best_fit_y - background, result.y_unit, "Y"),
                ]
                for offset, (label, values, unit, designation) in enumerate(series):
                    curves_sheet.cell(1, column + offset, name)
                    curves_sheet.cell(2, column + offset, label)
                    curves_sheet.cell(3, column + offset, unit)
                    curves_sheet.cell(4, column + offset, designation)
                    for point_index, value in enumerate(values):
                        curves_sheet.cell(5 + point_index, column + offset, float(value))
                column += len(series)

        parameters_sheet.append(
            ["Sample", "Category", "Peak", "Parameter", "Value", "StdErr", "Unit", "Derived"]
        )
        for index in successful:
            result = self._results[index]
            curve_name = self._curves[index][0]
            for name, parameter in result.parameters.items():
                category, peak, label = self._parameter_parts(name)
                parameters_sheet.append(
                    [
                        curve_name,
                        category,
                        peak,
                        label,
                        parameter.value,
                        parameter.stderr,
                        parameter.unit,
                        bool(parameter.derived),
                    ]
                )
            metric_labels = {
                "rmse": "RMSE",
                "reduced_chi_square": "Reduced Chi-square",
                "r_squared": "R²",
                "aic": "AIC",
                "bic": "BIC",
                "data_points": "Data Points",
                "degrees_of_freedom": "Degrees of Freedom",
            }
            for key, label in metric_labels.items():
                parameters_sheet.append(
                    [curve_name, "Fit Quality", "", label, result.metrics.get(key), "", "", True]
                )

        created_utc = datetime.now(timezone.utc).isoformat()
        info_sheet.append(["Section", "Sample", "Parameter", "Value"])
        common_rows = [
            ("Export", "", "Schema", "saxs_waxs_peak_fit_workbook/3" if auto_sheet is not None else ("saxs_waxs_peak_fit_workbook/2" if herman_sheet is not None else "saxs_waxs_peak_fit_workbook/1")),
            ("Export", "", "Created UTC", created_utc),
            ("Export", "", "Scientific Status", "technical_fit_only"),
            ("Shared Fit Settings", "", "Weighting", "unweighted; imported sigma is not used in fitting"),
            ("Shared Fit Settings", "", "Model", "automatic_per_peak" if self.auto_check.isChecked() else
             ("per_peak:" + ",".join(self._component_models) if self._component_models else str(self.model_combo.currentData()))),
            ("Shared Fit Settings", "", "Peak Count", "one fit per detected peak" if auto_sheet is not None else int(self.peak_count_spin.value())),
            ("Shared Fit Settings", "", "Peak Center Bounds", "not applied in automatic mode" if self.auto_check.isChecked() else (json.dumps(self._component_center_bounds) if self._component_center_bounds is not None else "unconstrained")),
            ("Shared Fit Settings", "", "Peak FWHM Bounds", "not applied in automatic mode" if self.auto_check.isChecked() else (json.dumps(self._component_fwhm_bounds) if self._component_fwhm_bounds is not None else "unconstrained")),
            ("Shared Fit Settings", "", "Background", "constant" if auto_sheet is not None else str(self.background_combo.currentData())),
            ("Shared Fit Settings", "", "X Min", "per peak; see Automatic Peaks" if auto_sheet is not None else self.range_lower.value()),
            ("Shared Fit Settings", "", "X Max", "per peak; see Automatic Peaks" if auto_sheet is not None else self.range_upper.value()),
        ]
        for row in common_rows:
            info_sheet.append(row)
        if herman_sheet is not None:
            info_sheet.append(["Herman", "", "Calculation Schema", "herman_single_peak_selected_domain/3" if self.herman_domain_combo.currentData() == "selected" else "herman_single_peak_full_90/3"])
            info_sheet.append(["Herman", "", "Reference", "Fitted peak center; periodic local angle"])
            info_sheet.append(["Herman", "", "Domain", "Observed selected peak domain; no missing-bin fill" if self.herman_domain_combo.currentData() == "selected" else "Observed full 0–90 degrees; no missing-bin fill"])
            info_sheet.append(["Herman", "", "Weight", "sin(abs(local angle)); intensity-weighted trapezoidal moment"])
            info_sheet.append(["Herman", "", "Center Node", "Weighted integrand is exactly zero at the peak center"])
            info_sheet.append(["Herman", "", "Negative Points", "Fit moment retained; point moment withheld, no clipping"])
            info_sheet.append(["Herman", "", "Uncertainty", "not estimated; fit parameter errors are not Herman uncertainty"])
        if auto_sheet is not None:
            info_sheet.append(["Automatic", "", "Peak detection", f"Gaussian-smoothed candidate detection only; raw points fitted; prominence >= {self.auto_prominence.value():g}% of detected range"])
            info_sheet.append(["Automatic", "", "Fit range", "95% prominence width, at least four bins per side, at most 90 degrees per side; candidate only"])
            info_sheet.append(["Automatic", "", "Model selection", "minimum AIC among converged Gaussian, Lorentzian, Voigt and pseudo-Voigt on identical points"])
            info_sheet.append(["Automatic", "", "Scientific status", "candidate; inspect peak assignment, domain, background and residuals"])
        context_summary = self._source_context_summary(selected)
        for key, value in self._flatten_context(context_summary["shared"]):
            info_sheet.append(["Shared Processing Context", "", key, value])
        for curve_name, context in context_summary["per_curve_differences"].items():
            for key, value in self._flatten_context(context):
                info_sheet.append(["Per-sample Difference", curve_name, key, value])
        for index in selected:
            if index not in self._results:
                info_sheet.append(
                    [
                        "Fit Failure",
                        self._curves[index][0],
                        "Reason",
                        self._fit_failures.get(index, "unknown"),
                    ]
                )

        formatted_sheets = [summary_sheet, parameters_sheet, info_sheet]
        if herman_sheet is not None:
            formatted_sheets.append(herman_sheet)
        if auto_sheet is not None:
            formatted_sheets.append(auto_sheet)
        if pair_sheet is not None:
            formatted_sheets.append(pair_sheet)
        for sheet in formatted_sheets:
            for cell in sheet[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
        for cell in curves_sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
        for row_number in (2, 3, 4):
            for cell in curves_sheet[row_number]:
                cell.fill = subheader_fill
        curves_sheet.freeze_panes = "A5"
        if herman_input_sheet is not None:
            for cell in herman_input_sheet[1]:
                cell.fill = header_fill
                cell.font = header_font
            for row_number in (2, 3, 4):
                for cell in herman_input_sheet[row_number]:
                    cell.fill = subheader_fill
            herman_input_sheet.freeze_panes = "A5"

        for sheet in workbook.worksheets:
            for cells in sheet.columns:
                letter = get_column_letter(cells[0].column)
                width = min(42, max(10, max(len(str(cell.value or "")) for cell in cells) + 2))
                sheet.column_dimensions[letter].width = width

        temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp.xlsx")
        try:
            workbook.save(temporary)
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target

    def _fit_image_snapshot(self, selected):
        return (list(self._curves), dict(self._results), dict(self._fit_failures),
                self._source_context_summary(selected))

    def export_fit_images(self, directory, indices, *, image_format="png", dpi=600):
        selected = [int(index) for index in indices]
        if not selected or any(i < 0 or i >= len(self._curves) for i in selected) or len(set(selected)) != len(selected):
            raise ValueError("请选择不重复的有效曲线")
        for index in selected:
            if index in self._auto_batch_pending or index in self._fit_failures:
                continue
            if index not in self._results:
                self._fit_index(index, display=False)
        return self._write_fit_images(self._fit_image_snapshot(selected), directory, selected,
                                      image_format=image_format, dpi=dpi)

    @staticmethod
    def _write_fit_images(snapshot, directory, indices, *, image_format="png", dpi=600,
                          stop=None, progress=None):
        curves, results, fit_failures, context_summary = snapshot
        from .rendering import publication_render_context
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from .integration_panel import IntegrationPanel

        suffixes = {"png": ".png", "tiff": ".tif", "pdf": ".pdf", "svg": ".svg"}
        if image_format not in suffixes or not isinstance(dpi, int) or dpi <= 0:
            raise ValueError("unsupported image format or DPI")
        root = Path(directory)
        protected = (Path(__file__).resolve().parents[2] / "data").resolve()
        resolved = root.resolve()
        if resolved == protected or protected in resolved.parents:
            raise ValueError("不能写入受保护的原始数据目录")
        selected = [int(index) for index in indices]
        if not selected or any(index < 0 or index >= len(curves) for index in selected):
            raise ValueError("请选择有效曲线")
        if len(set(selected)) != len(selected):
            raise ValueError("曲线选择重复")
        successful = [index for index in selected if index in results]
        if not successful:
            raise ValueError("没有可导出的拟合结果")
        root.mkdir(parents=True, exist_ok=True)
        paths = []
        stems = set()
        for index in successful:
            stem = re.sub(r'[<>:"/\\|?*]', "_", Path(curves[index][0]).stem).strip(" .") or "sample"
            base, count = stem, 2
            while stem.casefold() in stems:
                stem = f"{base}_{count}"
                count += 1
            stems.add(stem.casefold())
            paths.append(root / f"{stem}_peak_fit{suffixes[image_format]}")
        note_path = root / "peak_fit_image_parameters.txt"
        if any(path.exists() for path in [*paths, note_path]):
            raise FileExistsError("拟合图或参数说明已存在，请选择其他文件夹")
        descriptions = []
        colors = ("#4C78A8", "#59A14F", "#F28E2B", "#B07AA1", "#76B7B2", "#E15759")
        created_paths: list[Path] = []
        try:
            with publication_render_context(IntegrationPanel._publication_matplotlib_style()):
                for ordinal, (index, path) in enumerate(zip(successful, paths), 1):
                    if stop is not None and stop.is_set():
                        raise InterruptedError("已停止；本次导出文件已清理")
                    result = results[index]
                    figure = Figure(figsize=(180/25.4, 130/25.4), dpi=dpi)
                    FigureCanvasAgg(figure)
                    axes = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [3, 1]})
                    axis, residual = axes
                    axis.plot(result.used_x, result.observed_y, "o", markersize=2.8, color="#555555", label="Data")
                    axis.plot(result.used_x, result.best_fit_y, color="#D1495B", linewidth=1.8, label="Fit")
                    if result.components:
                        baseline = result.best_fit_y - np.sum(np.stack(list(result.components.values())), axis=0)
                        for number, component in enumerate(result.components.values(), 1):
                            axis.plot(result.used_x, component + baseline, linestyle="--", linewidth=1.1,
                                      color=colors[(number-1) % len(colors)], label=f"Peak {number} + background")
                        axis.plot(result.used_x, baseline, linestyle=":", linewidth=1, color="#777777", label="Background")
                    residual.plot(result.used_x, result.residual_y, color="#35618D", linewidth=1.2)
                    residual.axhline(0, color="#777777", linewidth=.8)
                    axis.set_ylabel(f"I ({result.y_unit})")
                    residual.set_ylabel("Residual")
                    residual.set_xlabel(f"x ({result.x_unit})")
                    for ax in axes:
                        ax.tick_params(direction="in", top=False, right=False)
                    axis.legend(loc="best", ncol=2 if len(result.components) > 3 else 1)
                    figure.tight_layout()
                    with path.open("xb") as stream:
                        created_paths.append(path)
                        figure.savefig(stream, format=image_format, dpi=dpi)
                    figure.clear()
                    descriptions.append({"sample": curves[index][0], "image": path.name,
                        "fit": result.to_document(include_arrays=False)})
                    if progress is not None:
                        progress(ordinal, len(paths))
            if stop is not None and stop.is_set():
                raise InterruptedError("已停止；本次导出文件已清理")
            failures = [{"sample": curves[index][0], "reason": fit_failures.get(index, "not processed")}
                        for index in selected if index not in results]
            with note_path.open("x", encoding="utf-8") as stream:
                created_paths.append(note_path)
                stream.write("拟合图参数和处理记录\n")
                stream.write(json.dumps({"format": image_format, "dpi": dpi,
                                         "processing_context": context_summary,
                                         "items": descriptions,
                                         "failures": failures}, ensure_ascii=False, indent=2, default=str))
        except Exception:
            for created in created_paths:
                created.unlink(missing_ok=True)
            raise
        return tuple(paths)

    def _choose_export_figures(self, batch: bool) -> None:
        indices = range(len(self._curves)) if batch else [self.curve_combo.currentIndex()]
        image_format, accepted = QtWidgets.QInputDialog.getItem(
            self, "选择图片格式", "格式", ("PNG", "TIFF", "PDF", "SVG"), 0, False)
        if not accepted:
            return
        directory = choose_export_folder(self, "选择拟合图导出文件夹", str(Path.cwd()))
        if not directory:
            return
        self.start_image_export(directory, indices, image_format.lower())

    def start_image_export(self, directory, indices, image_format="png", *, dpi=600, snapshot=None):
        if self.image_export_active or self._auto_batch_active or self._comparison_thread is not None:
            return
        selected = list(indices)
        snapshot = self._fit_image_snapshot(selected) if snapshot is None else snapshot
        self.image_export_active = True
        self.image_stop = threading.Event()
        self._image_controls = [(w, w.isEnabled()) for w in self.findChildren(QtWidgets.QWidget)
            if isinstance(w, (QtWidgets.QAbstractButton, QtWidgets.QAbstractSpinBox,
                              QtWidgets.QComboBox, QtWidgets.QTableWidget))
            and not isinstance(w.window(), QtWidgets.QProgressDialog)]
        for widget, _ in self._image_controls:
            widget.setEnabled(False)
        self.image_progress = QtWidgets.QProgressDialog("准备导出拟合图片…", "停止导出", 0,
                                                        len(selected), self)
        self.image_progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        self.image_progress.canceled.connect(self.image_stop.set)
        self.image_progress.show()
        self.image_thread = threading.Thread(target=self._image_worker,
            args=(snapshot, directory, selected, image_format, dpi, self.image_stop, self.image_signals), daemon=True)
        self.image_thread.start()

    @staticmethod
    def _image_worker(snapshot, directory, selected, image_format, dpi, stop, signals):
        try:
            outcome = PeakFitPanel._write_fit_images(snapshot, directory, selected, image_format=image_format,
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
            self.status_label.setText(f"批量导图未完成：{error}")
        else:
            paths = outcome
            self.status_label.setText(f"已导出 {len(paths)} 张图及一份参数说明；失败或未拟合项见说明。")

    def export_simple_workbook(self, path, indices, *, selected_peaks=False):
        from .fit_export import export_compact_fit_workbook
        return export_compact_fit_workbook(self, path, indices, selected_peaks=selected_peaks)

    def _simple_image_snapshot(self, indices, *, selected_peaks=False):
        curves, results, failures = [], {}, {}
        for index in indices:
            fits = self._auto_fits.get(index, []) if self.auto_check.isChecked() else []
            if fits:
                numbers = sorted(self._selected_peaks.get(index, set())) if selected_peaks else range(len(fits))
                for number in numbers:
                    position = len(curves)
                    curves.append((f"{Path(self._curves[index][0]).stem}_peak_{number+1}", self._curves[index][1]))
                    results[position] = fits[number].result
            else:
                position = len(curves)
                curves.append(self._curves[index])
                if index in self._results:
                    results[position] = self._results[index]
                else:
                    failures[position] = self._fit_failures.get(index, "尚未拟合")
        if not results:
            raise ValueError("所选范围没有已完成的拟合")
        return curves, results, failures, self._source_context_summary(list(indices))

    def _choose_simple_export(self):
        from .fit_export import FitExportDialog
        dialog = FitExportDialog(self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        scope = dialog.scope_combo.currentData()
        indices = list(range(len(self._curves))) if scope == "batch" else [self.curve_combo.currentIndex()]
        selected_peaks = scope == "selected"
        try:
            if dialog.content_combo.currentData() == "data":
                stem = "batch_peak_fit" if scope == "batch" else f"{Path(self._curves[indices[0]][0]).stem}_peak_fit"
                path, _ = QtWidgets.QFileDialog.getSaveFileName(
                    self, "保存拟合结果", stem + ".xlsx", "Excel 工作簿 (*.xlsx)")
                if path:
                    result = self.export_simple_workbook(path, indices, selected_peaks=selected_peaks)
                    self.status_label.setText(f"已导出 {result.name}：曲线数据、参数汇总、处理说明。")
            else:
                snapshot = self._simple_image_snapshot(indices, selected_peaks=selected_peaks)
                directory = choose_export_folder(self, "保存拟合图片")
                if directory:
                    self.start_image_export(directory, list(range(len(snapshot[0]))),
                        dialog.format_combo.currentText().lower(), snapshot=snapshot)
        except (ValueError, OSError) as exc:
            self.status_label.setText(f"导出失败：{exc}")

    def _choose_export_current(self) -> None:
        index = self.curve_combo.currentIndex()
        if index < 0:
            return
        self._choose_export_scope([index], "选择当前曲线导出文件夹")

    def _choose_export_batch(self) -> None:
        if not self._curves:
            return
        self._choose_export_scope(range(len(self._curves)), "选择整批拟合导出文件夹")

    def _choose_export_scope(self, indices: Iterable[int], title: str) -> None:
        selected = choose_export_folder(self, title, str(Path.cwd()))
        if not selected:
            return
        try:
            workbook = self.export_fit_bundle(selected, indices)
        except (PeakFitError, OSError) as exc:
            self.status_label.setStyleSheet("color: #a11;")
            self.status_label.setText(f"导出失败：{exc}")
            return
        self.status_label.setStyleSheet("")
        self.status_label.setText(f"已导出工作簿 {workbook.name}（曲线、汇总、参数和处理信息）。")


class PeakFitLauncherPanel(QtWidgets.QWidget):
    """Compact sidebar entry that opens one or more modeless fit workspaces."""

    workspace_requested = QtCore.Signal(object, object)
    load_checked_requested = QtCore.Signal()
    cancel_load_requested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._curves: list[tuple[str, IntegrationCurve]] = []
        self._parameter_contexts: list[Mapping[str, Any]] = []
        self._checked_selection_count = 0
        self._loading = False

        self.summary_label = QtWidgets.QLabel("")
        self.summary_label.setWordWrap(True)
        self.load_checked_button = QtWidgets.QPushButton(
            "加载左侧勾选样品的积分曲线"
        )
        self.cancel_load_button = QtWidgets.QPushButton("停止加载")
        self.cancel_load_button.hide()
        self.curve_combo = QtWidgets.QComboBox()
        self.open_current_button = QtWidgets.QPushButton("打开当前已生成曲线")
        self.open_selected_button = QtWidgets.QPushButton("选择已生成曲线到同一窗口…")
        self.open_all_button = QtWidgets.QPushButton("全部已生成曲线到同一窗口")
        note = QtWidgets.QLabel(
            "拟合窗口不会占用二维查看区域；可以同时打开多个窗口。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.load_checked_button)
        layout.addWidget(self.cancel_load_button)
        layout.addWidget(self.curve_combo)
        layout.addWidget(self.open_current_button)
        layout.addWidget(self.open_selected_button)
        layout.addWidget(self.open_all_button)
        layout.addWidget(note)
        layout.addStretch(1)

        self.load_checked_button.clicked.connect(self.load_checked_requested.emit)
        self.cancel_load_button.clicked.connect(self.cancel_load_requested.emit)
        self.open_current_button.clicked.connect(self._request_current)
        self.open_selected_button.clicked.connect(self._request_selected)
        self.open_all_button.clicked.connect(self._request_all)
        self._refresh_summary()
        self._update_enabled_state()

    @property
    def curves(self) -> tuple[tuple[str, IntegrationCurve], ...]:
        return tuple(self._curves)

    def set_curves(
        self,
        curves: Iterable[tuple[str, IntegrationCurve]],
        parameter_contexts: Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        previous_name = self.curve_combo.currentText()
        self._curves = list(curves)
        if parameter_contexts is None:
            self._parameter_contexts = [
                {"integration": dict(curve.audit)} for _name, curve in self._curves
            ]
        else:
            self._parameter_contexts = [dict(value) for value in parameter_contexts]
            if len(self._parameter_contexts) != len(self._curves):
                raise ValueError("curve parameter context count does not match curves")
        blocker = QtCore.QSignalBlocker(self.curve_combo)
        self.curve_combo.clear()
        for name, _curve in self._curves:
            self.curve_combo.addItem(str(name))
        del blocker
        if previous_name:
            previous_index = self.curve_combo.findText(previous_name)
            if previous_index >= 0:
                self.curve_combo.setCurrentIndex(previous_index)
        self._refresh_summary()
        self._update_enabled_state()

    def set_checked_selection_count(self, count: int) -> None:
        self._checked_selection_count = max(0, int(count))
        self._refresh_summary()
        self._update_enabled_state()

    def set_loading(self, loading: bool, detail: str | None = None) -> None:
        self._loading = bool(loading)
        self.cancel_load_button.setVisible(self._loading)
        if detail:
            self.summary_label.setText(detail)
        else:
            self._refresh_summary()
        self._update_enabled_state()

    def _refresh_summary(self) -> None:
        checked = self._checked_selection_count
        generated = len(self._curves)
        if checked or generated:
            self.summary_label.setText(
                f"左侧已勾选 {checked} 个样品；当前已生成 {generated} 条积分曲线。"
            )
        else:
            self.summary_label.setText(
                "请先在左侧勾选样品，或在“积分”页生成当前窗格曲线。"
            )

    def _update_enabled_state(self) -> None:
        enabled = bool(self._curves)
        self.load_checked_button.setEnabled(
            self._checked_selection_count > 0 and not self._loading
        )
        self.load_checked_button.setText(
            f"加载左侧勾选的 {self._checked_selection_count} 个样品"
            if self._checked_selection_count
            else "加载左侧勾选样品的积分曲线"
        )
        self.curve_combo.setEnabled(enabled)
        self.open_current_button.setEnabled(enabled and not self._loading)
        self.open_selected_button.setEnabled(enabled and not self._loading)
        self.open_all_button.setEnabled(
            enabled and len(self._curves) > 1 and not self._loading
        )

    def _emit_indices(self, indices: Iterable[int]) -> None:
        ordered = sorted(set(int(index) for index in indices))
        curves = [self._curves[index] for index in ordered]
        contexts = [self._parameter_contexts[index] for index in ordered]
        if curves:
            self.workspace_requested.emit(curves, contexts)

    def _request_current(self) -> None:
        index = self.curve_combo.currentIndex()
        if index >= 0:
            self._emit_indices([index])

    def _request_all(self) -> None:
        self._emit_indices(range(len(self._curves)))

    def _request_selected(self) -> None:
        if not self._curves:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("选择进入同一拟合窗口的曲线")
        dialog.resize(460, 420)
        list_widget = QtWidgets.QListWidget()
        list_widget.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        )
        for index, (name, _curve) in enumerate(self._curves):
            item = QtWidgets.QListWidgetItem(str(name))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, index)
            list_widget.addItem(item)
            item.setSelected(True)
        hint = QtWidgets.QLabel("可用 Ctrl/Shift 选择多条；每条曲线仍保留独立拟合结果。")
        hint.setWordWrap(True)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(hint)
        layout.addWidget(list_widget, 1)
        layout.addWidget(buttons)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        self._emit_indices(
            item.data(QtCore.Qt.ItemDataRole.UserRole)
            for item in list_widget.selectedItems()
        )


class PeakFitWorkspaceWindow(QtWidgets.QMainWindow):
    """Large modeless workspace; several instances may coexist."""

    def __init__(
        self,
        curves: Iterable[tuple[str, IntegrationCurve]],
        parameter_contexts: Iterable[Mapping[str, Any]],
        *,
        session_number: int,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent, QtCore.Qt.WindowType.Window)
        curve_list = list(curves)
        context_list = [dict(value) for value in parameter_contexts]
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setWindowTitle(
            f"峰拟合工作区 {session_number} — {len(curve_list)} 条曲线"
        )
        self.resize(1240, 820)
        self.setMinimumSize(760, 520)
        self.fit_panel = PeakFitPanel()
        self.fit_panel._background_fitting=True
        self.fit_panel.set_curves(curve_list, context_list)
        self.setCentralWidget(self.fit_panel)
        self.statusBar().showMessage(
            "本窗口与主界面独立；曲线下拉框可在同一窗口内切换样品。"
        )

    def closeEvent(self, event):
        self.fit_panel._interactive_tasks.invalidate()
        # Stop all independently active operations before any earlier guard returns.
        if self.fit_panel.image_export_active:
            self.fit_panel.image_stop.set()
        if self.fit_panel._auto_batch_active:
            self.fit_panel._auto_batch_stop = True
        for kind in (SaxsAnalysisDialog, DebyeBuecheDialog):
            for dialog in self.findChildren(kind):
                if dialog.batch_active:
                    dialog.batch_stop.set()
                if getattr(dialog, 'image_export_active', False):
                    dialog.image_stop.set()
        if self.fit_panel._interactive_tasks.busy:
            self.fit_panel.status_label.setText("正在结束当前拟合，请稍后关闭。")
            event.ignore()
            return
        if self.fit_panel.image_export_active:
            self.fit_panel.image_stop.set()
            self.fit_panel.status_label.setText("正在完成当前图片及清理，请稍后关闭。")
            event.ignore()
            return
        active_saxs = [dialog for kind in (SaxsAnalysisDialog, DebyeBuecheDialog)
                       for dialog in self.findChildren(kind)
                       if dialog.batch_active or getattr(dialog, "image_export_active", False)]
        if active_saxs:
            for dialog in active_saxs:
                if getattr(dialog, "image_export_active", False):
                    dialog.image_stop.set()
                if dialog.batch_active:
                    dialog.batch_stop.set()
                dialog.label.setText("正在完成当前任务及清理，请稍后关闭。")
            self.fit_panel.status_label.setText("SAXS分析正在完成当前曲线，请稍后关闭。")
            event.ignore()
            return
        if self.fit_panel._auto_batch_active:
            self.fit_panel._auto_batch_stop = True
            self.fit_panel.status_label.setText("正在完成当前样品，请稍后关闭。")
            event.ignore()
            return
        super().closeEvent(event)
