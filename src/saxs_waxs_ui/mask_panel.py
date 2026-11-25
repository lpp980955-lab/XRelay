"""Compact controls for auditable detector-mask preview."""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtWidgets

from saxs_waxs_core.masking import (
    EXTERNAL_MASK_FORMATS,
    MaskRecipe,
    UserMaskLayer,
    load_external_mask_layer,
)
from saxs_waxs_core.models import ImageFrame
from .widgets import CollapsibleSection, install_panel_scroll


class MaskPanel(QtWidgets.QWidget):
    preview_requested = QtCore.Signal(object)
    clear_requested = QtCore.Signal()
    add_region_requested = QtCore.Signal(str)
    undo_region_requested = QtCore.Signal()
    clear_regions_requested = QtCore.Signal()
    external_mask_changed = QtCore.Signal()
    auto_suggest_requested = QtCore.Signal()
    region_visibility_changed = QtCore.Signal(bool)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._external_mask_path: Path | None = None
        self.explanation = QtWidgets.QLabel(
            "MASK只标记积分时应排除的像素，不改强度，也不填补遮挡区域。"
        )
        self.explanation.setWordWrap(True)
        self.explanation.setStyleSheet(
            "QLabel { background:#FFF4E5; color:#5A3510; padding:6px; "
            "border:1px solid #E5B66F; }"
        )
        self.sentinel_check = QtWidgets.QCheckBox("原始TVX负哨兵像素")
        self.sentinel_check.setChecked(True)
        self.sentinel_check.setToolTip(
            "仅对原始有符号整数TVX帧启用；不会掩蔽扣背景后产生的负强度"
        )
        self.nonfinite_check = QtWidgets.QCheckBox("NaN / Inf像素")
        self.nonfinite_check.setChecked(True)
        self.cutoff_check = QtWidgets.QCheckBox("达到探测器计数上限的像素")
        self.cutoff_check.setChecked(True)
        self.sample_label = QtWidgets.QLabel("当前图像：未载入")
        self.sample_label.setWordWrap(True)
        self.metadata_label = QtWidgets.QLabel("探测器MASK元数据：未知")
        self.metadata_label.setWordWrap(True)
        self.preview_button = QtWidgets.QPushButton("预览MASK")
        self.clear_button = QtWidgets.QPushButton("隐藏MASK")
        self.circle_button = QtWidgets.QPushButton("＋圆形")
        self.circle_button.setToolTip("添加可拖动缩放的圆形beamstop/坏区")
        self.rectangle_button = QtWidgets.QPushButton("＋矩形")
        self.rectangle_button.setToolTip("添加可拖动缩放的矩形排除区")
        self.polygon_button = QtWidgets.QPushButton("＋逐点多边形")
        self.polygon_button.setToolTip(
            "进入逐点绘制：左键添加顶点，点击首点或右键完成，Esc取消"
        )
        self.auto_suggest_button = QtWidgets.QPushButton("自动建议beamstop/支撑杆")
        self.auto_suggest_button.setToolTip(
            "依据当前几何中心和同半径强度亏损生成可编辑候选；会自动预览，但仍需目视确认"
        )
        self.show_regions_check = QtWidgets.QCheckBox("显示MASK编辑轮廓")
        self.show_regions_check.setChecked(True)
        self.show_regions_check.setToolTip("只隐藏编辑框和手柄，不删除MASK区域或已计算结果")
        self.occlusion_side_combo = QtWidgets.QComboBox()
        for label, value in (
            ("左侧被挡", "occlusion_left"),
            ("右侧被挡", "occlusion_right"),
            ("上半区被挡", "occlusion_top"),
            ("下半区被挡", "occlusion_bottom"),
            ("自定义矩形", "occlusion_rectangle"),
            ("自定义多边形", "occlusion_polygon"),
        ):
            self.occlusion_side_combo.addItem(label, value)
        self.occlusion_button = QtWidgets.QPushButton("＋联用遮挡")
        self.occlusion_button.setToolTip(
            "添加红色虚线排除区；矩形可旋转，多边形逐点绘制；不会镜像或填补数据"
        )
        self.undo_button = QtWidgets.QPushButton("撤销区域")
        self.clear_regions_button = QtWidgets.QPushButton("清空区域")
        self.region_label = QtWidgets.QLabel("自定义区域：0")
        self.external_mask_label = QtWidgets.QLabel("外部MASK：未选择")
        self.external_mask_label.setWordWrap(True)
        self.external_mask_button = QtWidgets.QPushButton("导入外部MASK…")
        self.remove_external_mask_button = QtWidgets.QPushButton("移除外部MASK")
        self.remove_external_mask_button.setEnabled(False)
        self.status_label = QtWidgets.QLabel("未执行MASK预览")
        self.status_label.setWordWrap(True)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.preview_button)
        buttons.addWidget(self.clear_button)
        shape_buttons = QtWidgets.QHBoxLayout()
        shape_buttons.addWidget(self.circle_button)
        shape_buttons.addWidget(self.rectangle_button)
        shape_buttons.addWidget(self.polygon_button)
        occlusion_buttons = QtWidgets.QHBoxLayout()
        occlusion_buttons.addWidget(self.occlusion_side_combo, 1)
        occlusion_buttons.addWidget(self.occlusion_button)
        history_buttons = QtWidgets.QHBoxLayout()
        history_buttons.addWidget(self.undo_button)
        history_buttons.addWidget(self.clear_regions_button)

        advanced_widget = QtWidgets.QWidget()
        advanced_layout = QtWidgets.QVBoxLayout(advanced_widget)
        advanced_layout.setContentsMargins(4, 2, 4, 4)
        advanced_layout.addWidget(self.explanation)
        advanced_layout.addWidget(self.sample_label)
        advanced_layout.addWidget(self.metadata_label)
        advanced_layout.addWidget(self.sentinel_check)
        advanced_layout.addWidget(self.nonfinite_check)
        advanced_layout.addWidget(self.cutoff_check)
        advanced_layout.addWidget(QtWidgets.QLabel("联用遮挡（仅排除，不补全）"))
        advanced_layout.addLayout(occlusion_buttons)
        advanced_layout.addLayout(history_buttons)
        advanced_layout.addWidget(QtWidgets.QLabel("外部坏点MASK（非零=排除）"))
        external_buttons = QtWidgets.QHBoxLayout()
        external_buttons.addWidget(self.external_mask_button)
        external_buttons.addWidget(self.remove_external_mask_button)
        advanced_layout.addLayout(external_buttons)
        advanced_layout.addWidget(self.external_mask_label)
        self.advanced_section = CollapsibleSection(
            "更多MASK来源与操作", advanced_widget, expanded=False
        )

        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.addWidget(self.auto_suggest_button)
        layout.addWidget(self.show_regions_check)
        layout.addWidget(QtWidgets.QLabel("手动排除区域"))
        layout.addLayout(shape_buttons)
        layout.addWidget(self.region_label)
        layout.addLayout(buttons)
        layout.addWidget(self.status_label)
        layout.addWidget(self.advanced_section)
        layout.addStretch(1)

        install_panel_scroll(self, content)

        self.preview_button.clicked.connect(self._request_preview)
        self.clear_button.clicked.connect(self.clear_requested)
        self.circle_button.clicked.connect(lambda: self.add_region_requested.emit("circle"))
        self.rectangle_button.clicked.connect(
            lambda: self.add_region_requested.emit("rectangle")
        )
        self.polygon_button.clicked.connect(
            lambda: self.add_region_requested.emit("polygon_draw")
        )
        self.auto_suggest_button.clicked.connect(self.auto_suggest_requested)
        self.show_regions_check.toggled.connect(self.region_visibility_changed)
        self.occlusion_button.clicked.connect(
            lambda: self.add_region_requested.emit(
                str(self.occlusion_side_combo.currentData())
            )
        )
        self.undo_button.clicked.connect(self.undo_region_requested)
        self.clear_regions_button.clicked.connect(self.clear_regions_requested)
        self.external_mask_button.clicked.connect(self._choose_external_mask)
        self.remove_external_mask_button.clicked.connect(self._remove_external_mask)
        from .file_drop import install_file_drop,choose_input
        def dropped(paths):
            selected=choose_input(self,paths,lambda p:p.suffix.lower() in EXTERNAL_MASK_FORMATS,'选择外部MASK')
            if selected:self.set_external_mask_path(selected)
        install_file_drop(self,dropped,lambda message:self.set_status(message,error=True))

    @property
    def external_mask_path(self) -> Path | None:
        return self._external_mask_path

    def set_current_frame(self, path: Path | None, frame: ImageFrame | None) -> None:
        if path is None or frame is None:
            self.sample_label.setText("当前图像：未载入")
            self.metadata_label.setText("探测器MASK元数据：未知")
            return
        self.sample_label.setText(f"当前图像：{path.name}")
        metadata = frame.metadata
        referenced = metadata.excluded_pixels_file or "无文件名"
        count = (
            "未知"
            if metadata.excluded_pixel_count is None
            else str(metadata.excluded_pixel_count)
        )
        cutoff = "未知" if metadata.count_cutoff is None else str(metadata.count_cutoff)
        self.metadata_label.setText(
            f"头信息：坏点 {count}，引用 {referenced}；计数上限 {cutoff}。\n"
            "引用的外部坏点文件未找到时不会假装已经应用。"
        )

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color:#A4262C;" if error else "color:#126B34;")

    def set_region_count(self, count: int, *, synchronized: bool = False) -> None:
        suffix = "（已同步）" if synchronized else ""
        self.region_label.setText(f"自定义区域：{count}{suffix}")

    def recipe(self, user_layers: tuple[UserMaskLayer, ...] = ()) -> MaskRecipe:
        return MaskRecipe(
            mask_nonfinite=self.nonfinite_check.isChecked(),
            mask_tvx_negative_sentinels=self.sentinel_check.isChecked(),
            mask_count_cutoff=self.cutoff_check.isChecked(),
            user_layers=user_layers,
        )

    def set_recipe_options(
        self,
        recipe: MaskRecipe,
        external_mask_path: str | Path | None = None,
    ) -> None:
        """Load the non-raster mask policy stored in a processing recipe."""

        self.nonfinite_check.setChecked(recipe.mask_nonfinite)
        self.sentinel_check.setChecked(recipe.mask_tvx_negative_sentinels)
        self.cutoff_check.setChecked(recipe.mask_count_cutoff)
        self.set_external_mask_path(external_mask_path, emit=False)

    def set_external_mask_path(
        self,
        path: str | Path | None,
        *,
        emit: bool = True,
    ) -> None:
        self._external_mask_path = (
            None if path is None else Path(path).expanduser().absolute()
        )
        if self._external_mask_path is None:
            self.external_mask_label.setText("外部MASK：未选择")
            self.remove_external_mask_button.setEnabled(False)
        else:
            self.external_mask_label.setText(
                f"外部MASK：{self._external_mask_path.name}\n"
                "保持原方向和尺寸；不自动转置、缩放或反色。"
            )
            self.external_mask_label.setToolTip(str(self._external_mask_path))
            self.remove_external_mask_button.setEnabled(True)
        if emit:
            self.external_mask_changed.emit()

    def external_mask_layer(
        self, expected_shape: tuple[int, int]
    ) -> UserMaskLayer | None:
        if self._external_mask_path is None:
            return None
        return load_external_mask_layer(self._external_mask_path, expected_shape)

    @QtCore.Slot()
    def _choose_external_mask(self) -> None:
        suffix_text = " ".join(f"*{suffix}" for suffix in EXTERNAL_MASK_FORMATS)
        selected, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择外部坏点MASK（非零像素将被排除）",
            str(
                self._external_mask_path.parent
                if self._external_mask_path is not None
                else Path.cwd()
            ),
            f"二维MASK ({suffix_text});;所有文件 (*)",
        )
        if selected:
            self.set_external_mask_path(selected)

    @QtCore.Slot()
    def _remove_external_mask(self) -> None:
        self.set_external_mask_path(None)

    @QtCore.Slot()
    def _request_preview(self) -> None:
        self.preview_requested.emit(self.recipe())
