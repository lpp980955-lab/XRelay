"""Compact manual geometry and theoretical calibrant-ring preview panel."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from saxs_waxs_core.calibrants import (
    CalibrantLibrary,
    CalibrantLibraryError,
    load_calibrant_library,
    load_custom_calibrant,
)
from saxs_waxs_core.calibration import CalibrationSuggestion, ManualRingEstimate
from saxs_waxs_core.geometry import (
    DetectorGeometry,
    GeometryError,
    GeometryProvenance,
    load_geometry,
    save_geometry,
)
from .widgets import CollapsibleSection


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROTECTED_DATA_ROOT = PROJECT_ROOT / "data"


class GeometryPanel(QtWidgets.QWidget):
    """Edit a zero-tilt candidate without claiming it is calibrated."""

    preview_changed = QtCore.Signal(object)
    apply_requested = QtCore.Signal(object)
    suggest_requested = QtCore.Signal(object, object)
    manual_ring_pick_requested = QtCore.Signal(object, object, int)
    manual_ring_pick_cancel_requested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None, *, library_settings=None) -> None:
        super().__init__(parent)
        self._library_settings=library_settings
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.library: CalibrantLibrary = load_calibrant_library()
        self._backing_geometry: DetectorGeometry | None = None
        self._prefer_local_refinement = False
        self._parameters_are_placeholders = True
        self._build_ui()
        self._connect_live_preview()
        self._update_preview()

    @staticmethod
    def _double_spin(
        minimum: float,
        maximum: float,
        value: float,
        *,
        decimals: int = 4,
        suffix: str = "",
    ) -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setKeyboardTracking(False)
        return spin

    def _build_ui(self) -> None:
        self.warning_label = QtWidgets.QLabel(
            "候选几何（未标定）\n当前仅预览零倾角理论环；不会修改原始图像。"
        )
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(
            "QLabel { background:#FFF4CE; color:#5F4500; padding:6px; border:1px solid #E6C95C; }"
        )

        self.wavelength_spin = self._double_spin(
            0.000001, 1000.0, 0.1, decimals=6, suffix=" nm"
        )
        self.distance_spin = self._double_spin(
            0.001, 10_000_000.0, 1000.0, decimals=3, suffix=" mm"
        )
        self.pixel_x_spin = self._double_spin(
            0.001, 1_000_000.0, 100.0, decimals=3, suffix=" µm"
        )
        self.pixel_y_spin = self._double_spin(
            0.001, 1_000_000.0, 100.0, decimals=3, suffix=" µm"
        )
        self.center_x_spin = self._double_spin(
            0.0, 10_000_000.0, 500.0, decimals=4, suffix=" px"
        )
        self.center_y_spin = self._double_spin(
            0.0, 10_000_000.0, 500.0, decimals=4, suffix=" px"
        )
        self.width_spin = QtWidgets.QSpinBox()
        self.width_spin.setRange(1, 10_000_000)
        self.width_spin.setValue(1000)
        self.height_spin = QtWidgets.QSpinBox()
        self.height_spin.setRange(1, 10_000_000)
        self.height_spin.setValue(1000)

        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("波长", self.wavelength_spin)
        form.addRow("SDD", self.distance_spin)
        form.addRow("中心 X", self.center_x_spin)
        form.addRow("中心 Y", self.center_y_spin)

        self.calibrant_combo = QtWidgets.QComboBox()
        self.calibrant_combo.setEditable(True)
        self.calibrant_combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.calibrant_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.calibrant_combo.setMinimumContentsLength(10)
        self.calibrant_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.calibrant_combo.completer().setCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)
        self.calibrant_combo.completer().setFilterMode(QtCore.Qt.MatchFlag.MatchContains)
        self.calibrant_combo.addItem("无标样", "none")
        for calibrant in self.library.calibrants:
            self._add_calibrant_item(calibrant)
        agbh_index = self.calibrant_combo.findData("AgBh")
        if agbh_index >= 0:
            self.calibrant_combo.setCurrentIndex(agbh_index)
        self.max_rings_spin = QtWidgets.QSpinBox()
        self.max_rings_spin.setRange(1, 50)
        self.max_rings_spin.setValue(8)
        self.preview_check = QtWidgets.QCheckBox("显示理论环")
        # Calibration overlays are an opt-in tool, not a normal image-viewer
        # default.  Starting unchecked also prevents stale candidate geometry
        # from obscuring a newly opened sample image.
        self.preview_check.setChecked(False)
        form.addRow("标样", self.calibrant_combo)

        self.import_calibrant_button = QtWidgets.QPushButton("自定义标样…")
        self.import_calibrant_button.setToolTip(
            "导入pyFAI/FIT2D风格.D/TXT文件：每行一个从大到小排列的d-spacing（Å）"
        )
        form.addRow("", self.import_calibrant_button)

        self.manual_ring_combo = QtWidgets.QComboBox()
        self.manual_ring_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.manual_ring_combo.setMinimumContentsLength(10)
        self.manual_ring_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.manual_ring_combo.setToolTip(
            "选择你准备点击的那一条真实标样环；环序号必须与理论标样一致"
        )
        self.manual_ring_button = QtWidgets.QPushButton("三点定环")
        self.manual_ring_button.setToolTip(
            "在同一条可见标样环上依次点击3个相距较远的点，计算零倾角中心和SDD候选"
        )
        self.manual_ring_cancel_button = QtWidgets.QPushButton("取消取点")
        self.manual_ring_cancel_button.hide()
        manual_ring_actions = QtWidgets.QHBoxLayout()
        manual_ring_actions.addWidget(self.manual_ring_button, 1)
        manual_ring_actions.addWidget(self.manual_ring_cancel_button)
        self.manual_ring_label=QtWidgets.QLabel("理论环")
        form.addRow(self.manual_ring_label, self.manual_ring_combo)
        form.addRow("", manual_ring_actions)

        self.apply_button = QtWidgets.QPushButton("应用几何参数")
        self.apply_button.setToolTip("已知中心、SDD、波长和像素尺寸时可直接应用，无需提供标样。")
        self.suggest_button = QtWidgets.QPushButton("全图自动建议中心 / SDD…")
        self.suggest_button.setToolTip(
            "先在全图用多环相关粗找中心，再用pyFAI提取控制点精修；SDD仍需合理初值，结果仍需人工确认"
        )
        self.clear_button = QtWidgets.QPushButton("清除预览")
        self.load_button = QtWidgets.QPushButton("导入配置…")
        self.save_button = QtWidgets.QPushButton("导出配置…")
        buttons = QtWidgets.QVBoxLayout()
        buttons.addWidget(self.suggest_button)
        buttons.addWidget(self.apply_button)

        advanced_widget = QtWidgets.QWidget()
        advanced_layout = QtWidgets.QVBoxLayout(advanced_widget)
        advanced_layout.setContentsMargins(4, 2, 4, 4)
        detector_form = QtWidgets.QFormLayout()
        detector_form.addRow("像素 X", self.pixel_x_spin)
        detector_form.addRow("像素 Y", self.pixel_y_spin)
        detector_form.addRow("图像宽度", self.width_spin)
        detector_form.addRow("图像高度", self.height_spin)
        detector_form.addRow("最多显示环", self.max_rings_spin)
        detector_form.addRow("", self.preview_check)
        advanced_layout.addLayout(detector_form)
        advanced_actions = QtWidgets.QGridLayout()
        advanced_actions.addWidget(self.clear_button, 0, 0, 1, 2)
        advanced_actions.addWidget(self.load_button, 1, 0)
        advanced_actions.addWidget(self.save_button, 1, 1)
        advanced_layout.addLayout(advanced_actions)
        self.advanced_section = CollapsibleSection(
            "探测器尺寸与配置", advanced_widget, expanded=False
        )
        self.status_label = QtWidgets.QLabel("等待图像与参数")
        self.status_label.setWordWrap(True)

        self.residual_group = QtWidgets.QGroupBox("多环残差（建议值）")
        residual_layout = QtWidgets.QVBoxLayout(self.residual_group)
        self.residual_summary_label = QtWidgets.QLabel()
        self.residual_summary_label.setWordWrap(True)
        self.residual_table = QtWidgets.QTableWidget(0, 5)
        self.residual_table.setHorizontalHeaderLabels(
            ["环", "N", "均值 /µrad", "RMS /µrad", "最大 /µrad"]
        )
        self.residual_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.residual_table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        self.residual_table.verticalHeader().setVisible(False)
        self.residual_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.residual_table.horizontalHeader().setStretchLastSection(True)
        self.residual_table.setMaximumHeight(150)
        residual_layout.addWidget(self.residual_summary_label)
        residual_layout.addWidget(self.residual_table)
        self.residual_group.hide()

        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.addWidget(self.warning_label)
        layout.addLayout(form)
        layout.addLayout(buttons)
        layout.addWidget(self.advanced_section)
        layout.addWidget(self.status_label)
        layout.addWidget(self.residual_group)
        layout.addStretch(1)
        from .geometry_library import install_geometry_library
        install_geometry_library(self,layout,settings=self._library_settings)

        from .widgets import scrollable_content
        scroll = scrollable_content(content)
        self.content_scroll=scroll
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.apply_button.clicked.connect(self._apply)
        self.suggest_button.clicked.connect(self._request_suggestion)
        self.manual_ring_button.clicked.connect(self._request_manual_ring_pick)
        self.manual_ring_cancel_button.clicked.connect(
            self.manual_ring_pick_cancel_requested.emit
        )
        self.clear_button.clicked.connect(self._clear)
        self.load_button.clicked.connect(self._load_dialog)
        self.save_button.clicked.connect(self._save_dialog)
        self.import_calibrant_button.clicked.connect(self._import_calibrant_dialog)
        from .file_drop import install_file_drop,choose_input
        for button,suffixes,importer,title in (
            (self.load_button,('.json',),self._load_dialog,'导入几何配置'),
            (self.import_calibrant_button,('.d','.txt','.dat'),self._import_calibrant_dialog,'导入标样')):
            def dropped(paths,suffixes=suffixes,importer=importer,title=title):
                selected=choose_input(self,paths,lambda p:p.suffix.lower() in suffixes,title)
                if selected:importer(str(selected))
            install_file_drop(button,dropped,self.status_label.setText)

    def _connect_live_preview(self) -> None:
        for widget in (
            self.wavelength_spin,
            self.distance_spin,
            self.pixel_x_spin,
            self.pixel_y_spin,
            self.center_x_spin,
            self.center_y_spin,
            self.width_spin,
            self.height_spin,
        ):
            widget.valueChanged.connect(self._candidate_edited)
        self.max_rings_spin.valueChanged.connect(self._update_preview)
        self.calibrant_combo.currentIndexChanged.connect(self._update_preview)
        self.calibrant_combo.currentIndexChanged.connect(self._populate_manual_rings)
        self.calibrant_combo.currentIndexChanged.connect(self._calibrant_mode_changed)
        self.wavelength_spin.valueChanged.connect(self._populate_manual_rings)
        self.preview_check.toggled.connect(self._update_preview)
        self._populate_manual_rings()

    @property
    def center_only(self) -> bool:
        return self.calibrant_combo.currentData()=="none"

    def _calibrant_mode_changed(self):
        if self._backing_geometry is not None:
            self._backing_geometry=replace(self._backing_geometry,metadata={
                **dict(self._backing_geometry.metadata),"preview_calibrant":self.selected_calibrant_id})
        self.manual_ring_label.setVisible(not self.center_only)
        self.manual_ring_combo.setVisible(not self.center_only)
        self.preview_check.setEnabled(not self.center_only)
        self.max_rings_spin.setEnabled(not self.center_only)
        self.residual_group.hide()
        self._refresh_suggest_button_label()
        self.manual_ring_button.setToolTip("在同一条样品圆环上点击3个分散的点，只确定中心，保留输入的SDD。" if self.center_only else
            "在同一条可见标样环上点击3个分散的点，计算中心和SDD候选。")
        self.warning_label.setText("无标样：只确定中心。SDD、波长及像素尺寸使用输入值。" if self.center_only else
            "候选几何（未标定）\n当前仅预览零倾角理论环；不会修改原始图像。")
        self._update_preview()

    @staticmethod
    def _calibrant_label(calibrant: object) -> str:
        calibrant_id = str(getattr(calibrant, "id"))
        if calibrant_id.startswith("custom:"):
            return str(getattr(calibrant, "name"))
        return (
            calibrant_id.replace("alpha_Al2O3", "Al2O3")
            .replace("_SRM", " SRM")
            .replace("_", "-")
        )

    def _add_calibrant_item(self, calibrant: object) -> None:
        label = self._calibrant_label(calibrant)
        self.calibrant_combo.addItem(label, getattr(calibrant, "id"))
        index = self.calibrant_combo.count() - 1
        source = str(getattr(calibrant, "source_path", ""))
        description = str(getattr(calibrant, "name", label))
        tooltip = f"{description}\n{source}" if source else description
        self.calibrant_combo.setItemData(
            index, tooltip, QtCore.Qt.ItemDataRole.ToolTipRole
        )

    @property
    def selected_calibrant_id(self) -> str:
        value = self.calibrant_combo.currentData()
        if value is None:
            raise CalibrantLibraryError("请从标样候选列表中选择，或导入d-spacing文件")
        return str(value)

    @property
    def selected_manual_ring_ordinal(self) -> int:
        return int(self.manual_ring_combo.currentData() or 1)

    @QtCore.Slot()
    def _populate_manual_rings(self, *_args: object) -> None:
        current = self.manual_ring_combo.currentData()
        blocker = QtCore.QSignalBlocker(self.manual_ring_combo)
        self.manual_ring_combo.clear()
        try:
            calibrant = None if self.center_only else self.library.resolve(self.selected_calibrant_id)
        except CalibrantLibraryError:
            del blocker
            return
        if calibrant is None:
            del blocker
            return
        wavelength_a = self.wavelength_spin.value() * 10.0
        for ordinal, peak in enumerate(calibrant.peaks, start=1):
            if wavelength_a / (2.0 * peak.d_angstrom) > 1.0:
                continue
            hkl = (
                "(" + " ".join(str(value) for value in peak.hkl) + ")"
                if peak.hkl is not None
                else "hkl未提供"
            )
            self.manual_ring_combo.addItem(
                f"{ordinal}: d={peak.d_angstrom:.5g} Å  {hkl}",
                ordinal,
            )
        if current is not None:
            index = self.manual_ring_combo.findData(current)
            if index >= 0:
                self.manual_ring_combo.setCurrentIndex(index)
        del blocker

    @QtCore.Slot()
    def _request_manual_ring_pick(self) -> None:
        try:
            geometry = self.geometry()
            calibrant = None if self.center_only else self.library.resolve(self.selected_calibrant_id)
        except (GeometryError, CalibrantLibraryError) as exc:
            self.status_label.setText(f"无法开始三点定环：{exc}")
            return
        self.manual_ring_pick_requested.emit(
            geometry, calibrant, self.selected_manual_ring_ordinal
        )

    def set_manual_ring_pick_active(self, active: bool, point_count: int = 0) -> None:
        self.manual_ring_button.setEnabled(not active)
        self.manual_ring_combo.setEnabled(not active)
        self.calibrant_combo.setEnabled(not active)
        self.wavelength_spin.setEnabled(not active)
        self.manual_ring_cancel_button.setVisible(active)
        if active:
            self.status_label.setText(
                f"三点定环：已取 {point_count}/3 点；请在所选同一条真实环上点击，点尽量分散。"
            )

    def show_manual_ring_estimate(self, estimate: ManualRingEstimate) -> None:
        self.set_manual_ring_pick_active(False)
        self.set_geometry(estimate.candidate_geometry)
        candidate = estimate.candidate_geometry
        self.residual_group.hide()
        if estimate.ring_ordinal is None:
            self.status_label.setText(f"三点定中心：({candidate.beam_center_x_px:.3f}, {candidate.beam_center_y_px:.3f}) px；SDD保持 {candidate.detector_distance_m*1e3:.3f} mm。确认中心后点击应用几何参数。")
            return
        self.status_label.setText(
            f"三点候选：中心 ({candidate.beam_center_x_px:.3f}, "
            f"{candidate.beam_center_y_px:.3f}) px；SDD "
            f"{candidate.detector_distance_m * 1e3:.3f} mm。"
            "请检查其余理论环是否同时吻合，再应用或继续自动精修。"
        )

    def geometry(self) -> DetectorGeometry:
        if self._backing_geometry is not None:
            return self._backing_geometry
        return DetectorGeometry(
            wavelength_m=self.wavelength_spin.value() * 1e-9,
            detector_distance_m=self.distance_spin.value() * 1e-3,
            pixel_size_x_m=self.pixel_x_spin.value() * 1e-6,
            pixel_size_y_m=self.pixel_y_spin.value() * 1e-6,
            beam_center_x_px=self.center_x_spin.value(),
            beam_center_y_px=self.center_y_spin.value(),
            image_width_px=self.width_spin.value(),
            image_height_px=self.height_spin.value(),
            provenance=GeometryProvenance(
                method="manual_candidate",
                source_application="XRelay",
                source_convention="top_left_edge_xy_right_down_pixel_center_plus_0p5",
                notes="candidate_unvalidated; zero_tilt_preview",
            ),
            metadata={
                "status": "candidate_unvalidated",
                "preview_calibrant": self.selected_calibrant_id,
                "calibration_refinement_mode": (
                    "local" if self._prefer_local_refinement else "full_image"
                ),
            },
        )

    def set_geometry(self, geometry: DetectorGeometry) -> None:
        mode=geometry.metadata.get("preview_calibrant")
        if mode is not None:
            index=self.calibrant_combo.findData(mode)
            if index>=0:
                self.calibrant_combo.setCurrentIndex(index)
        widgets = (
            self.wavelength_spin,
            self.distance_spin,
            self.pixel_x_spin,
            self.pixel_y_spin,
            self.center_x_spin,
            self.center_y_spin,
            self.width_spin,
            self.height_spin,
        )
        blockers = [QtCore.QSignalBlocker(widget) for widget in widgets]
        self.wavelength_spin.setValue(geometry.wavelength_m * 1e9)
        self.distance_spin.setValue(geometry.detector_distance_m * 1e3)
        self.pixel_x_spin.setValue(geometry.pixel_size_x_m * 1e6)
        self.pixel_y_spin.setValue(geometry.pixel_size_y_m * 1e6)
        self.center_x_spin.setValue(geometry.beam_center_x_px)
        self.center_y_spin.setValue(geometry.beam_center_y_px)
        if geometry.image_width_px is not None:
            self.width_spin.setValue(geometry.image_width_px)
        if geometry.image_height_px is not None:
            self.height_spin.setValue(geometry.image_height_px)
        del blockers
        self._backing_geometry = geometry
        self._parameters_are_placeholders = False
        self._prefer_local_refinement = (
            geometry.provenance.method == "manual_assigned_ring_candidate"
            or geometry.metadata.get("calibration_refinement_mode") == "local"
        )
        self._refresh_suggest_button_label()
        self._update_preview()

    def _refresh_suggest_button_label(self) -> None:
        self.suggest_button.setText(
            "自动识别中心…" if self.center_only else
            "精修当前候选中心 / SDD…"
            if self._prefer_local_refinement
            else "全图自动建议中心 / SDD…"
        )

    def set_image_metadata(self, metadata: object) -> None:
        width = int(getattr(metadata, "width_px"))
        height = int(getattr(metadata, "height_px"))
        pixel_x = getattr(metadata, "pixel_size_x_m", None)
        pixel_y = getattr(metadata, "pixel_size_y_m", None)
        if self._backing_geometry is not None:
            differs = (
                self._backing_geometry.image_width_px != width
                or self._backing_geometry.image_height_px != height
                or (
                    pixel_x is not None
                    and self._backing_geometry.pixel_size_x_m != float(pixel_x)
                )
                or (
                    pixel_y is not None
                    and self._backing_geometry.pixel_size_y_m != float(pixel_y)
                )
            )
            if differs:
                self._backing_geometry = None
        blockers = [QtCore.QSignalBlocker(self.width_spin), QtCore.QSignalBlocker(self.height_spin)]
        self.width_spin.setValue(width)
        self.height_spin.setValue(height)
        del blockers
        if pixel_x is not None:
            blocker = QtCore.QSignalBlocker(self.pixel_x_spin)
            self.pixel_x_spin.setValue(float(pixel_x) * 1e6)
            del blocker
        if pixel_y is not None:
            blocker = QtCore.QSignalBlocker(self.pixel_y_spin)
            self.pixel_y_spin.setValue(float(pixel_y) * 1e6)
            del blocker
        if self._parameters_are_placeholders:
            blockers = [
                QtCore.QSignalBlocker(self.center_x_spin),
                QtCore.QSignalBlocker(self.center_y_spin),
            ]
            self.center_x_spin.setValue(width / 2.0)
            self.center_y_spin.setValue(height / 2.0)
            del blockers
        else:
            if self.center_x_spin.value() > width:
                self.center_x_spin.setValue(width / 2.0)
            if self.center_y_spin.value() > height:
                self.center_y_spin.setValue(height / 2.0)
        self._update_preview()

    def set_center_values(self, x_px: float, y_px: float, *, emit_preview: bool) -> None:
        self._parameters_are_placeholders = False
        if (
            self.center_x_spin.value() != float(x_px)
            or self.center_y_spin.value() != float(y_px)
        ):
            if (
                self._backing_geometry is not None
                and self._backing_geometry.provenance.method
                == "manual_assigned_ring_candidate"
            ):
                self._backing_geometry = replace(
                    self._backing_geometry,
                    beam_center_x_px=float(x_px),
                    beam_center_y_px=float(y_px),
                )
            else:
                self._backing_geometry = None
        blockers = [
            QtCore.QSignalBlocker(self.center_x_spin),
            QtCore.QSignalBlocker(self.center_y_spin),
        ]
        self.center_x_spin.setValue(float(x_px))
        self.center_y_spin.setValue(float(y_px))
        del blockers
        if emit_preview:
            self._update_preview()

    def set_distance_value(self, distance_m: float, *, emit_preview: bool) -> None:
        self._parameters_are_placeholders = False
        distance_mm = float(distance_m) * 1e3
        if self.distance_spin.value() != distance_mm:
            if (
                self._backing_geometry is not None
                and self._backing_geometry.provenance.method
                == "manual_assigned_ring_candidate"
            ):
                self._backing_geometry = replace(
                    self._backing_geometry,
                    detector_distance_m=float(distance_m),
                )
            else:
                self._backing_geometry = None
        blocker = QtCore.QSignalBlocker(self.distance_spin)
        self.distance_spin.setValue(distance_mm)
        del blocker
        if emit_preview:
            self._update_preview()

    @QtCore.Slot()
    def _candidate_edited(self, *_args: object) -> None:
        self._backing_geometry = None
        self._parameters_are_placeholders = False
        self._update_preview()

    def set_calibration_busy(self, busy: bool, *, local_refinement: bool = False) -> None:
        self.calibrant_combo.setEnabled(not busy)
        self.manual_ring_button.setEnabled(not busy)
        if self.center_only:
            self.suggest_button.setEnabled(not busy)
            self.suggest_button.setText("正在识别中心…" if busy else "自动识别中心…")
            if busy:
                self.status_label.setText("正在后台识别中心，保持输入的SDD、波长和像素尺寸。")
            return
        self.suggest_button.setEnabled(not busy)
        self.suggest_button.setText(
            (
                "正在从当前候选局部精修…"
                if local_refinement
                else "正在全图粗找并精修…"
            )
            if busy
            else self.suggest_button.text()
        )
        if not busy:
            self._refresh_suggest_button_label()
        if busy:
            self.status_label.setText(
                (
                    "正在从当前三点候选附近提取多环控制点；"
                    if local_refinement
                    else "正在后台全图搜索标样候选；"
                )
                + "不修改原始图像，完成后需人工确认。"
            )

    def show_calibration_suggestion(self, suggestion: CalibrationSuggestion) -> None:
        run = suggestion.selected_run
        self.set_geometry(run.candidate_geometry)
        self.residual_table.setRowCount(len(run.ring_residuals))
        for row, residual in enumerate(run.ring_residuals):
            values = (
                str(residual.ring_ordinal),
                str(residual.control_point_count),
                f"{residual.mean_delta_two_theta_rad * 1e6:.2f}",
                f"{residual.rms_delta_two_theta_rad * 1e6:.2f}",
                f"{residual.max_abs_delta_two_theta_rad * 1e6:.2f}",
            )
            for column, value in enumerate(values):
                self.residual_table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        candidate = run.candidate_geometry
        coarse_summary = (
            f"全图粗中心 ({suggestion.coarse_center.center_x_px:.3f}, "
            f"{suggestion.coarse_center.center_y_px:.3f}) px；"
            f"相对输入移动 {suggestion.coarse_center.input_shift_px:.2f} px\n"
            if suggestion.coarse_center is not None
            else "全图粗定位失败，已退回当前参数附近精修\n"
        )
        self.residual_summary_label.setText(
            coarse_summary
            + f"选中 {run.start_label}；{run.control_point_count} 点 / {run.ring_count} 环\n"
            f"中心 ({candidate.beam_center_x_px:.3f}, {candidate.beam_center_y_px:.3f}) px；"
            f"SDD {candidate.detector_distance_m * 1e3:.3f} mm\n"
            f"{len(suggestion.runs)} 个初值的中心展宽 {suggestion.center_spread_px:.3f} px；"
            f"SDD 相对展宽 {suggestion.distance_spread_fraction * 100:.3f}%\n"
            f"峰检测掩膜：负值 {suggestion.negative_mask_count}，"
            f"饱和候选 {suggestion.saturated_mask_count} 像素"
        )
        sparse_warning = next(
            (
                warning.partition(":")[2]
                for warning in run.warnings
                if warning.startswith("sparse_rings_excluded:")
            ),
            "",
        )
        self.residual_group.show()
        self.status_label.setText(
            "已生成自动建议（未标定）；"
            + (
                f"已排除控制点不足的环（环号:点数 {sparse_warning}）；"
                if sparse_warning
                else ""
            )
            + "请观察其余理论环和逐环残差，确认后再应用。"
        )

    def load_from_path(self, path: str | Path) -> DetectorGeometry:
        geometry = load_geometry(path)
        self.set_geometry(geometry)
        self.status_label.setText(f"已导入候选配置：{Path(path).name}（未标定）")
        return geometry

    def save_to_path(self, path: str | Path, *, overwrite: bool = False) -> Path:
        destination = save_geometry(
            self.geometry(),
            path,
            protected_roots=(PROTECTED_DATA_ROOT,),
            overwrite=overwrite,
        )
        self.status_label.setText(f"已导出候选配置：{destination.name}")
        return destination

    @QtCore.Slot()
    def _update_preview(self, *_args: object) -> None:
        try:
            geometry = self.geometry()
        except GeometryError as exc:
            self.preview_changed.emit(None)
            self.status_label.setText(f"参数无效：{exc}")
            return
        if self.center_only:
            self.preview_changed.emit(None)
            self.status_label.setText("无标样：输入已知参数，只确定中心后应用即可积分。")
            return
        if not self.preview_check.isChecked():
            self.preview_changed.emit(None)
            self.status_label.setText("理论环预览已关闭")
            return
        try:
            calibrant = self.library.resolve(self.selected_calibrant_id)
        except (GeometryError, CalibrantLibraryError) as exc:
            self.preview_changed.emit(None)
            self.status_label.setText(f"参数无效：{exc}")
            return
        self.preview_changed.emit((geometry, calibrant, self.max_rings_spin.value()))
        self.status_label.setText(
            "理论环实时预览；拖动红十字调整中心，径向拖动理论环调整SDD；"
            "选中中心后方向键移动1 px，Shift+方向键移动0.1 px；当前未完成标定。"
        )

    @QtCore.Slot()
    def _apply(self) -> None:
        try:
            geometry = self.geometry()
        except (GeometryError, CalibrantLibraryError) as exc:
            self.status_label.setText(f"无法应用：{exc}")
            return
        self.apply_requested.emit(geometry)
        if geometry.provenance.method == "pyfai_guided_untilted_candidate":
            self.status_label.setText(
                "已应用自动建议的候选几何；仍需人工叠环和独立验证"
            )
        else:
            self.status_label.setText("已应用输入的几何参数，可直接积分；已知参数无需再做标样标定。")

    @QtCore.Slot()
    def _request_suggestion(self) -> None:
        try:
            geometry = self.geometry()
            calibrant = None if self.center_only else self.library.resolve(self.selected_calibrant_id)
        except (GeometryError, CalibrantLibraryError) as exc:
            self.status_label.setText(f"无法建议：{exc}")
            return
        self.suggest_requested.emit(geometry, calibrant)

    @QtCore.Slot()
    def _clear(self) -> None:
        self.preview_check.setChecked(False)
        self.preview_changed.emit(None)

    @QtCore.Slot()
    def _load_dialog(self, path=None) -> None:
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "导入几何候选", str(PROJECT_ROOT), "Geometry JSON (*.json)"
            )
        if not path:
            return
        try:
            self.load_from_path(path)
        except (OSError, GeometryError) as exc:
            self.status_label.setText(f"导入失败：{exc}")

    @QtCore.Slot()
    def _save_dialog(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出几何候选", str(PROJECT_ROOT / "artifacts"), "Geometry JSON (*.json)"
        )
        if not path:
            return
        try:
            self.save_to_path(path)
        except (OSError, GeometryError) as exc:
            self.status_label.setText(f"导出失败：{exc}")

    @QtCore.Slot()
    def _import_calibrant_dialog(self, path=None) -> None:
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "导入自定义标样 d-spacing",
                str(PROJECT_ROOT),
                "d-spacing files (*.D *.d *.txt *.dat);;All files (*)",
            )
        if not path:
            return
        try:
            calibrant = load_custom_calibrant(path)
            self.library = self.library.with_calibrant(calibrant)
        except CalibrantLibraryError as exc:
            self.status_label.setText(f"自定义标样导入失败：{exc}")
            return
        self._add_calibrant_item(calibrant)
        self.calibrant_combo.setCurrentIndex(self.calibrant_combo.count() - 1)
        self.status_label.setText(
            f"已读取自定义标样 {calibrant.name}：{len(calibrant.peaks)} 条 d-spacing；"
            "仅加入当前会话，源文件未修改。"
        )
