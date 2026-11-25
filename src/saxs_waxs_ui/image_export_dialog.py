"""Compact 2D export settings used for one image or a reviewed selection."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from saxs_waxs_core.presets import ExportPreset


class ImageExportDialog(QtWidgets.QDialog):
    def __init__(
        self,
        *,
        batch_available: bool,
        initial_preset: ExportPreset | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出2D图")
        self.setModal(True)
        self.setMinimumWidth(370)

        form = QtWidgets.QFormLayout()
        self.scope_combo = QtWidgets.QComboBox()
        self.scope_combo.addItem("当前图", "current")
        self.scope_combo.addItem("左侧勾选项", "selected")
        if not batch_available:
            model_item = self.scope_combo.model().item(1)
            if model_item is not None:
                model_item.setEnabled(False)
        self.format_combo = QtWidgets.QComboBox()
        for label, value in (("PNG", "png"), ("TIFF", "tiff"), ("SVG", "svg"), ("PDF", "pdf")):
            self.format_combo.addItem(label, value)
        self.batch_source_combo = QtWidgets.QComboBox()
        self.batch_source_combo.setToolTip(
            "原始二维数据直接读取探测器数组，不执行背景、MASK、标定或积分；"
            "处理后二维图才使用当前已预览或已设置的处理步骤。"
        )

        self.width_spin = QtWidgets.QDoubleSpinBox()
        self.width_spin.setRange(10.0, 1000.0)
        self.width_spin.setValue(180.0)
        self.width_spin.setSuffix(" mm")
        self.height_spin = QtWidgets.QDoubleSpinBox()
        self.height_spin.setRange(10.0, 1000.0)
        self.height_spin.setValue(130.0)
        self.height_spin.setSuffix(" mm")
        self.dpi_spin = QtWidgets.QSpinBox()
        self.dpi_spin.setRange(72, 2400)
        self.dpi_spin.setValue(600)

        size_row = QtWidgets.QWidget()
        size_layout = QtWidgets.QHBoxLayout(size_row)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.addWidget(self.width_spin)
        size_layout.addWidget(QtWidgets.QLabel("×"))
        size_layout.addWidget(self.height_spin)

        self.axes_check = QtWidgets.QCheckBox("坐标轴")
        self.axes_check.setChecked(True)
        self.colorbar_check = QtWidgets.QCheckBox("色标")
        self.colorbar_check.setChecked(True)
        decoration_row = QtWidgets.QWidget()
        decoration_layout = QtWidgets.QHBoxLayout(decoration_row)
        decoration_layout.setContentsMargins(0, 0, 0, 0)
        decoration_layout.addWidget(self.axes_check)
        decoration_layout.addWidget(self.colorbar_check)
        decoration_layout.addStretch(1)

        self.range_combo = QtWidgets.QComboBox()
        self.range_combo.addItem("按每张图当前预览规则（推荐）", "independent")
        self.range_combo.addItem("固定当前图数值上下限", "shared_manual")
        self.range_combo.setToolTip(
            "按每张图当前预览规则：复用当前配色、色阶类型和百分位，"
            "并对每张图独立求可见范围，效果与多图预览一致。\n"
            "固定当前图数值上下限：所有图共用当前图的绝对强度范围；"
            "适合严格同尺度比较，但较弱图可能变暗。"
        )
        self.axis_mode_combo = QtWidgets.QComboBox()
        self.axis_mode_combo.addItem("像素 x/y", "pixel")
        self.axis_mode_combo.addItem("探测器 mm（束心为0）", "detector_mm")
        self.axis_mode_combo.setToolTip(
            "q、s和2θ是二维径向场，不是线性横纵轴；定量q/s/2θ请使用积分曲线或二维光标读数"
        )

        form.addRow("范围", self.scope_combo)
        form.addRow("导出内容", self.batch_source_combo)
        form.addRow("格式", self.format_combo)
        form.addRow("版面", size_row)
        form.addRow("DPI", self.dpi_spin)
        form.addRow("标注", decoration_row)
        form.addRow("二维轴", self.axis_mode_combo)
        form.addRow("色阶", self.range_combo)

        self.advanced_button = QtWidgets.QToolButton()
        self.advanced_button.setText("高级样式")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.advanced_button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )

        self.advanced_widget = QtWidgets.QWidget()
        advanced_form = QtWidgets.QFormLayout(self.advanced_widget)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        self.font_combo = QtWidgets.QComboBox()
        self.font_combo.setEditable(True)
        self.font_combo.addItems(
            ("DejaVu Sans", "Arial", "Times New Roman", "Noto Sans CJK SC")
        )
        self.font_combo.setToolTip(
            "只使用本机实际安装且Matplotlib可解析的字体；缺失字体会明确报错，不静默替换"
        )
        self.label_font_spin = QtWidgets.QDoubleSpinBox()
        self.label_font_spin.setRange(5.0, 48.0)
        self.label_font_spin.setSingleStep(0.5)
        self.label_font_spin.setValue(10.0)
        self.label_font_spin.setSuffix(" pt")
        self.tick_font_spin = QtWidgets.QDoubleSpinBox()
        self.tick_font_spin.setRange(5.0, 36.0)
        self.tick_font_spin.setSingleStep(0.5)
        self.tick_font_spin.setValue(8.0)
        self.tick_font_spin.setSuffix(" pt")
        font_size_row = QtWidgets.QWidget()
        font_size_layout = QtWidgets.QHBoxLayout(font_size_row)
        font_size_layout.setContentsMargins(0, 0, 0, 0)
        font_size_layout.addWidget(QtWidgets.QLabel("轴标题"))
        font_size_layout.addWidget(self.label_font_spin)
        font_size_layout.addWidget(QtWidgets.QLabel("刻度"))
        font_size_layout.addWidget(self.tick_font_spin)
        self.line_width_spin = QtWidgets.QDoubleSpinBox()
        self.line_width_spin.setRange(0.2, 5.0)
        self.line_width_spin.setSingleStep(0.1)
        self.line_width_spin.setValue(0.8)
        self.line_width_spin.setSuffix(" pt")
        advanced_form.addRow("字体", self.font_combo)
        advanced_form.addRow("字号", font_size_row)
        advanced_form.addRow("框线/刻度线", self.line_width_spin)
        self.advanced_widget.setVisible(False)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
            | QtWidgets.QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("继续…")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_widget)
        layout.addWidget(buttons)
        self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        self.advanced_button.toggled.connect(self._advanced_toggled)
        self._scope_changed()
        if initial_preset is not None:
            self.set_export_preset(initial_preset)

    def set_export_preset(self, preset: ExportPreset) -> None:
        """Populate controls from a project export-domain snapshot."""

        for combo, value in (
            (self.format_combo, preset.detector_image_format),
            (self.axis_mode_combo, preset.detector_axis_mode),
        ):
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)
        # A new export should reproduce the visible preview by default.  Do not
        # silently carry a previous run's fixed absolute range into unrelated
        # images; users can still opt into that comparison mode explicitly.
        self.range_combo.setCurrentIndex(
            self.range_combo.findData("independent")
        )
        self.width_spin.setValue(preset.image_width_mm)
        self.height_spin.setValue(preset.image_height_mm)
        self.dpi_spin.setValue(preset.image_dpi)
        self.axes_check.setChecked(preset.detector_include_axes)
        self.colorbar_check.setChecked(preset.detector_include_colorbar)
        self.font_combo.setCurrentText(preset.detector_font_family)
        self.label_font_spin.setValue(preset.detector_label_font_size_pt)
        self.tick_font_spin.setValue(preset.detector_tick_font_size_pt)
        self.line_width_spin.setValue(preset.detector_line_width_pt)

    @property
    def scope(self) -> str:
        return str(self.scope_combo.currentData())

    @property
    def batch_source(self) -> str:
        return str(self.batch_source_combo.currentData())

    def _scope_changed(self) -> None:
        is_batch = self.scope == "selected"
        previous = self.batch_source
        blocker = QtCore.QSignalBlocker(self.batch_source_combo)
        self.batch_source_combo.clear()
        self.batch_source_combo.addItem(
            "原始二维数据（跳过全部处理）", "raw_array"
        )
        if is_batch:
            self.batch_source_combo.addItem(
                "处理后二维图（按当前处理设置）", "processed_recipe"
            )
        else:
            self.batch_source_combo.addItem(
                "当前显示的处理结果", "current_view"
            )
        index = self.batch_source_combo.findData(previous)
        # Raw is deliberately the safe default: exporting pictures must not
        # silently require or execute scientific processing.
        self.batch_source_combo.setCurrentIndex(index if index >= 0 else 0)
        del blocker

    def _advanced_toggled(self, checked: bool) -> None:
        self.advanced_button.setArrowType(
            QtCore.Qt.ArrowType.DownArrow
            if checked
            else QtCore.Qt.ArrowType.RightArrow
        )
        self.advanced_widget.setVisible(checked)
        if not hasattr(self, '_export_preview_tasks'):
            self.adjustSize()

    def export_preset(self) -> ExportPreset:
        return ExportPreset(
            detector_image_format=str(self.format_combo.currentData()),
            detector_include_axes=self.axes_check.isChecked(),
            detector_include_colorbar=self.colorbar_check.isChecked(),
            detector_range_mode=str(self.range_combo.currentData()),
            detector_axis_mode=str(self.axis_mode_combo.currentData()),
            image_width_mm=self.width_spin.value(),
            image_height_mm=self.height_spin.value(),
            image_dpi=self.dpi_spin.value(),
            detector_font_family=self.font_combo.currentText().strip(),
            detector_label_font_size_pt=self.label_font_spin.value(),
            detector_tick_font_size_pt=self.tick_font_spin.value(),
            detector_line_width_pt=self.line_width_spin.value(),
        )
