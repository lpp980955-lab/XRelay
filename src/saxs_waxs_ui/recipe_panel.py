"""Small UI surface for processing-recipe and explicit batch-plan I/O."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets
from .widgets import CollapsibleSection, install_panel_scroll


class RecipePanel(QtWidgets.QWidget):
    save_recipe_requested = QtCore.Signal()
    load_recipe_requested = QtCore.Signal()
    save_batch_requested = QtCore.Signal()
    run_batch_requested = QtCore.Signal()
    resume_batch_requested = QtCore.Signal()
    pause_batch_requested = QtCore.Signal()
    scope_apply_requested = QtCore.Signal(str)
    create_revision_requested = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        explanation = QtWidgets.QLabel(
            "处理配方保存几何、背景、MASK区域和积分参数。选择范围只会先生成左侧可检查的候选；"
            "文件夹、递归和序列范围会排除已配置输入及明显的背景/标样候选，执行前仍可人工增删。"
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet(
            "QLabel { background:#EAF3FF; color:#143A66; padding:6px; "
            "border:1px solid #9CC4F0; }"
        )
        self.selection_label = QtWidgets.QLabel("左侧已选择 0 个批次样品")
        self.selection_label.setWordWrap(True)
        self.scope_combo = QtWidgets.QComboBox()
        self.scope_combo.addItem("当前图像", "current")
        self.scope_combo.addItem("左侧选中项", "selected")
        self.scope_combo.addItem("当前文件夹", "folder")
        self.scope_combo.addItem("当前文件夹及子文件夹", "recursive")
        self.scope_combo.addItem("当前图所在序列", "sequence")
        self.scope_combo.setCurrentIndex(self.scope_combo.findData("selected"))
        self.apply_scope_button = QtWidgets.QPushButton("应用范围")
        self.apply_scope_button.setToolTip(
            "先把范围转换为左侧可见选择；检查无误后再执行，不会立即启动批处理"
        )
        self.save_recipe_button = QtWidgets.QPushButton("保存当前处理配方…")
        self.load_recipe_button = QtWidgets.QPushButton("载入配方/批次方案…")
        self.save_batch_button = QtWidgets.QPushButton("保存显式批次方案…")
        self.save_batch_button.setToolTip(
            "使用当前处理配方，并将左侧Ctrl/Shift多选的文件记录为样品范围"
        )
        self.run_batch_button = QtWidgets.QPushButton("执行所选样品批处理…")
        self.run_batch_button.setToolTip(
            "使用当前参数处理左侧明确选中的样品；结果写入新运行文件夹"
        )
        self.resume_batch_button = QtWidgets.QPushButton("恢复已有批处理…")
        self.resume_batch_button.setToolTip(
            "选择batch_manifest.json；跳过结果仍完整的已完成样品，并重试失败/未完成项"
        )
        self.pause_batch_button = QtWidgets.QPushButton("完成当前文件后暂停")
        self.pause_batch_button.setEnabled(False)
        self.batch_progress = QtWidgets.QProgressBar()
        self.batch_progress.setRange(0, 1)
        self.batch_progress.setValue(0)
        self.batch_progress.setTextVisible(True)
        self.batch_summary_label = QtWidgets.QLabel("批处理尚未启动。")
        self.batch_summary_label.setWordWrap(True)
        self.status_label = QtWidgets.QLabel("尚未保存或载入配方。")
        self.status_label.setWordWrap(True)

        revision_controls = QtWidgets.QWidget()
        revision_layout = QtWidgets.QFormLayout(revision_controls)
        revision_layout.setContentsMargins(0, 0, 0, 0)
        self.revision_scope_combo = QtWidgets.QComboBox()
        self.revision_scope_combo.addItem("左侧选中项", "selected")
        self.revision_scope_combo.addItem("从当前图起", "from_current")
        self.create_revision_button = QtWidgets.QPushButton("创建新版本…")
        self.create_revision_button.setToolTip(
            "基础版本来自已保存或载入的批次方案；新版本只追加，不覆盖旧版本"
        )
        self.revision_summary_label = QtWidgets.QLabel(
            "尚未建立基础批次方案；请先保存或载入批次方案。"
        )
        self.revision_summary_label.setWordWrap(True)
        revision_layout.addRow("应用范围", self.revision_scope_combo)
        revision_layout.addRow(self.create_revision_button)
        revision_layout.addRow(self.revision_summary_label)

        policy_controls = QtWidgets.QWidget()
        policy_layout = QtWidgets.QFormLayout(policy_controls)
        policy_layout.setContentsMargins(0, 0, 0, 0)
        self.parameter_mode_combo = QtWidgets.QComboBox()
        self.parameter_mode_combo.addItem("锁定（逐帧完全重放）", "locked")
        self.parameter_mode_combo.addItem("自适应建议（只记录，不改参数）", "adaptive_review")
        self.integration_space_label = QtWidgets.QLabel("物理绝对：q / s / 2θ / φ")
        self.mask_space_label = QtWidgets.QLabel("像素；相对区域仅用于预览")
        self.structural_qc_label = QtWidgets.QLabel("结构安全检查始终开启")
        self.scientific_qc_check = QtWidgets.QCheckBox("科学QC（尚未冻结规则）")
        self.scientific_qc_check.setChecked(False)
        self.scientific_qc_check.setEnabled(False)
        self.scientific_qc_check.setToolTip(
            "需先选择代表序列并共同冻结阈值；启用后也只能标记待复核"
        )
        policy_layout.addRow("参数", self.parameter_mode_combo)
        policy_layout.addRow("积分区", self.integration_space_label)
        policy_layout.addRow("MASK区", self.mask_space_label)
        policy_layout.addRow("结构QC", self.structural_qc_label)
        policy_layout.addRow("科学QC", self.scientific_qc_check)

        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.addWidget(self.selection_label)
        scope_row = QtWidgets.QHBoxLayout()
        scope_row.addWidget(QtWidgets.QLabel("范围"))
        scope_row.addWidget(self.scope_combo, 1)
        scope_row.addWidget(self.apply_scope_button)
        layout.addLayout(scope_row)
        layout.addWidget(self.run_batch_button)
        layout.addWidget(self.resume_batch_button)
        layout.addWidget(self.pause_batch_button)
        layout.addWidget(self.batch_progress)
        layout.addWidget(self.batch_summary_label)
        layout.addWidget(self.status_label)
        self.policy_section = CollapsibleSection(
            "批处理策略", policy_controls, expanded=False
        )
        layout.addWidget(self.policy_section)
        self.revision_section = CollapsibleSection(
            "配方版本", revision_controls, expanded=False
        )
        layout.addWidget(self.revision_section)
        file_controls = QtWidgets.QWidget()
        file_layout = QtWidgets.QVBoxLayout(file_controls)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.addWidget(self.save_recipe_button)
        file_layout.addWidget(self.load_recipe_button)
        file_layout.addWidget(self.save_batch_button)
        self.file_section = CollapsibleSection(
            "配方文件与批次方案", file_controls, expanded=False
        )
        layout.addWidget(self.file_section)
        self.explanation_section = CollapsibleSection(
            "配方与批次说明", explanation, expanded=False
        )
        layout.addWidget(self.explanation_section)
        layout.addStretch(1)

        install_panel_scroll(self, content)

        self.save_recipe_button.clicked.connect(self.save_recipe_requested)
        self.load_recipe_button.clicked.connect(self.load_recipe_requested)
        self.save_batch_button.clicked.connect(self.save_batch_requested)
        self.run_batch_button.clicked.connect(self.run_batch_requested)
        self.resume_batch_button.clicked.connect(self.resume_batch_requested)
        self.pause_batch_button.clicked.connect(self.pause_batch_requested)
        self.apply_scope_button.clicked.connect(
            lambda: self.scope_apply_requested.emit(str(self.scope_combo.currentData()))
        )
        self.create_revision_button.clicked.connect(
            lambda: self.create_revision_requested.emit(
                str(self.revision_scope_combo.currentData())
            )
        )

    def set_selection_count(self, count: int, *, reviewed: bool = False) -> None:
        del reviewed  # retained for source compatibility with earlier prototypes
        self.selection_label.setText(f"左侧已勾选 {count} 个批次样品")

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color:#A4262C;" if error else "color:#126B34;")

    def set_batch_running(self, running: bool) -> None:
        self.run_batch_button.setEnabled(not running)
        self.resume_batch_button.setEnabled(not running)
        self.apply_scope_button.setEnabled(not running)
        self.scope_combo.setEnabled(not running)
        self.parameter_mode_combo.setEnabled(not running)
        self.revision_scope_combo.setEnabled(not running)
        self.create_revision_button.setEnabled(not running)
        self.pause_batch_button.setEnabled(running)

    def set_revision_summary(self, text: str) -> None:
        self.revision_summary_label.setText(text)

    @property
    def parameter_mode(self) -> str:
        return str(self.parameter_mode_combo.currentData())

    def set_parameter_mode(self, mode: str) -> None:
        index = self.parameter_mode_combo.findData(mode)
        if index < 0:
            raise ValueError(f"unsupported parameter mode: {mode}")
        self.parameter_mode_combo.setCurrentIndex(index)

    def set_batch_progress(self, summary: dict[str, int]) -> None:
        total = max(1, int(summary.get("total", 0)))
        completed = (
            int(summary.get("passed", 0))
            + int(summary.get("needs_review", 0))
            + int(summary.get("failed", 0))
        )
        self.batch_progress.setRange(0, total)
        self.batch_progress.setValue(completed)
        self.batch_summary_label.setText(
            f"完成 {completed}/{summary.get('total', 0)}；"
            f"通过 {summary.get('passed', 0)}，"
            f"待复核 {summary.get('needs_review', 0)}，"
            f"失败 {summary.get('failed', 0)}"
        )
