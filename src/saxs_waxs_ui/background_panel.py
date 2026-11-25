"""Compact Phase 3 air-background role and preview controls."""

from __future__ import annotations
from saxs_waxs_core.readers.detector_io import SUPPORTED_SUFFIXES
from saxs_waxs_core.readers.detector_io import supported_detector_path,detector_base_suffix
from saxs_waxs_core.readers.detector_io import source_catalog,default_datasets,SourceSelectionRequired

from pathlib import Path
import re
from typing import Iterable

from PySide6 import QtCore, QtWidgets

from saxs_waxs_core.background import BackgroundMode, BackgroundRecipe
from .widgets import CollapsibleSection, install_panel_scroll


class BackgroundPanel(QtWidgets.QWidget):
    """Expose three business modes without leaking correction internals."""

    preview_requested = QtCore.Signal(object, object)
    raw_requested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._background_paths: list[Path] = []
        self._external_path: Path | None = None
        self._explicit_background = False

        self.explanation = QtWidgets.QLabel(
            "曝光时间未知时，默认按本批样品与背景曝光相同直接相减，输出计数；"
            "双方曝光时间已知时按时间归一化，输出计数率。"
            "预览不会修改原始文件。"
        )
        self.explanation.setWordWrap(True)
        self.explanation.setStyleSheet(
            "QLabel { background:#EAF3FF; color:#143A66; padding:6px; "
            "border:1px solid #9CC4F0; }"
        )

        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("自动扣背景", BackgroundMode.AUTO_AIR.value)
        self.mode_combo.addItem("不扣背景", BackgroundMode.NO_BACKGROUND.value)
        self.mode_combo.addItem("数据已扣背景", BackgroundMode.UPSTREAM_SUBTRACTED.value)
        self.equal_exposure_check = QtWidgets.QCheckBox("曝光未知时按同批等曝光处理")
        self.equal_exposure_check.setChecked(True)
        self.equal_exposure_check.setToolTip("无法读取曝光时间时直接计算：样品 − 背景；不假定具体秒数。已知曝光仍按各自时间归一化。")

        self.background_combo = QtWidgets.QComboBox()
        self.background_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.background_combo.setMinimumContentsLength(12)
        self.browse_button = QtWidgets.QPushButton("选择…")
        self.air_dataset_combo=QtWidgets.QComboBox()
        self.air_frame_spin=QtWidgets.QSpinBox();self.air_frame_spin.setMinimum(1)
        self._air_catalog=[]
        self.air_dataset_label=QtWidgets.QLabel('背景数据集')
        self.air_frame_label=QtWidgets.QLabel('背景帧')
        self.candidate_label = QtWidgets.QLabel("尚未扫描背景候选")
        self.candidate_label.setWordWrap(True)

        self.sample_label = QtWidgets.QLabel("当前样品：未载入")
        self.sample_label.setWordWrap(True)
        self.formula_label = QtWidgets.QLabel(
            "曝光已知：sample/t_sample − background/t_background（计数率）\n"
            "曝光未知且按等曝光处理：sample − background（计数）"
        )
        self.formula_label.setWordWrap(True)

        self.preview_button = QtWidgets.QPushButton("预览当前图")
        self.restore_button = QtWidgets.QPushButton("恢复原图")
        self.status_label = QtWidgets.QLabel("未执行背景预览")
        self.status_label.setWordWrap(True)

        form = QtWidgets.QFormLayout()
        form.addRow("处理方式", self.mode_combo)
        background_row = QtWidgets.QHBoxLayout()
        background_row.addWidget(self.background_combo, 1)
        background_row.addWidget(self.browse_button)
        form.addRow("背景文件", background_row)
        form.addRow(self.air_dataset_label,self.air_dataset_combo)
        form.addRow(self.air_frame_label,self.air_frame_spin)
        for widget in (self.air_dataset_label,self.air_dataset_combo,self.air_frame_label,self.air_frame_spin):widget.hide()
        form.addRow("", self.equal_exposure_check)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.preview_button)
        buttons.addWidget(self.restore_button)

        details_widget = QtWidgets.QWidget()
        details_layout = QtWidgets.QVBoxLayout(details_widget)
        details_layout.setContentsMargins(4, 2, 4, 4)
        details_layout.addWidget(self.explanation)
        details_layout.addWidget(self.candidate_label)
        details_layout.addWidget(self.sample_label)
        details_layout.addWidget(self.formula_label)
        self.details_section = CollapsibleSection(
            "当前样品与计算说明", details_widget, expanded=False
        )

        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.addLayout(form)
        layout.addLayout(buttons)
        layout.addWidget(self.status_label)
        layout.addWidget(self.details_section)
        layout.addStretch(1)

        install_panel_scroll(self, content)

        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.background_combo.currentIndexChanged.connect(self._update_air_source)
        self.background_combo.activated.connect(lambda *_: setattr(self, '_explicit_background', True))
        self.air_dataset_combo.currentIndexChanged.connect(self._air_dataset_changed)
        self.browse_button.clicked.connect(self._choose_background)
        # Keep the familiar picker and expose directory import beside it.
        menu=QtWidgets.QMenu(self.browse_button)
        menu.addAction('选择背景文件…',self._choose_background)
        menu.addAction('选择背景文件夹…',self._choose_background_folder)
        self.browse_button.clicked.disconnect(self._choose_background)
        self.browse_button.setMenu(menu)
        self.preview_button.clicked.connect(self._request_preview)
        self.restore_button.clicked.connect(self.raw_requested)
        self._mode_changed()
        from .file_drop import install_file_drop
        install_file_drop(self,self.import_paths,lambda error:self.set_status(error,error=True))

    @property
    def mode(self) -> BackgroundMode:
        return BackgroundMode(self.mode_combo.currentData())

    def background_recipe(self) -> BackgroundRecipe:
        metadata={
            "assume_equal_exposure_if_missing": self.equal_exposure_check.isChecked(),
        }
        if self.source_selection:metadata['source_selection']=self.source_selection
        return BackgroundRecipe(mode=self.mode, metadata=metadata)

    @property
    def source_ready(self):
        return not self._air_catalog or self.air_dataset_combo.currentIndex()>=0

    @property
    def source_selection(self):
        if not self._air_catalog or not self.source_ready:return {}
        entry=self._air_catalog[self.air_dataset_combo.currentIndex()]
        result={}
        if entry['dataset'] is not None:result['dataset']=entry['dataset']
        if entry['frames']>1:result['frame_index']=self.air_frame_spin.value()-1
        return result

    def _update_air_source(self):
        path=self.background_path
        self._air_catalog=[]
        try:
            if path is not None and path.is_file():self._air_catalog=source_catalog(path)
        except Exception as exc:
            self.status_label.setText(f'背景容器检查失败：{exc}')
        blocker=QtCore.QSignalBlocker(self.air_dataset_combo)
        self.air_dataset_combo.clear()
        for entry in self._air_catalog:
            self.air_dataset_combo.addItem(entry['dataset'] or '单一数据集',entry['dataset'])
        try:
            chosen=default_datasets(self._air_catalog) if self._air_catalog else []
            selected=self._air_catalog.index(chosen[0]) if len(chosen)==1 else -1
        except SourceSelectionRequired:selected=-1
        self.air_dataset_combo.setCurrentIndex(selected)
        del blocker
        for widget in (self.air_dataset_combo,self.air_dataset_label):widget.setVisible(len(self._air_catalog)>1)
        self._air_dataset_changed()

    def _air_dataset_changed(self):
        index=self.air_dataset_combo.currentIndex()
        count=self._air_catalog[index]['frames'] if 0<=index<len(self._air_catalog) else 1
        self.air_frame_spin.setMaximum(count)
        self.air_frame_spin.setValue(1)
        self.air_frame_spin.setSuffix(f' / {count}')
        self.air_frame_spin.setVisible(count>1);self.air_frame_label.setVisible(count>1)

    @property
    def background_path(self) -> Path | None:
        if self._external_path is not None and self.background_combo.currentData() == "external":
            return self._external_path
        index = self.background_combo.currentIndex()
        if 0 <= index < len(self._background_paths):
            return self._background_paths[index]
        return None

    def set_files(self, paths: Iterable[str | Path]) -> None:
        previous = self.background_path
        previous_selection=dict(self.source_selection)
        candidates = sorted(
            (
                Path(path)
                for path in paths
                if (re.search(r'(^|[^a-z0-9])(air|bg|bkg|background|blank)(?=$|[^a-z])',
                              Path(path).parent.name.casefold()+' '+Path(path).stem.casefold())
                    or '背景' in Path(path).parent.name+Path(path).stem)
                and supported_detector_path(path)
            ),
            key=lambda path: (path.name.casefold(), str(path).casefold()),
        )
        self._background_paths = candidates
        self._external_path = None
        self.background_combo.clear()
        for path in candidates:
            self.background_combo.addItem(path.name, str(path))
            self.background_combo.setItemData(
                self.background_combo.count() - 1,
                str(path),
                QtCore.Qt.ItemDataRole.ToolTipRole,
            )
        if previous is not None:
            previous_absolute = previous.expanduser().absolute()
            preserved_index = next(
                (
                    index
                    for index, candidate in enumerate(candidates)
                    if candidate.expanduser().absolute() == previous_absolute
                ),
                None,
            )
            if preserved_index is not None:
                self.background_combo.setCurrentIndex(preserved_index)
        if not candidates:
            self.background_combo.addItem("未发现候选", None)
            self.candidate_label.setText(
                "未发现文件名含 air 的图像；请选择背景文件。文件名只用于提出候选，不会默认锁定角色。"
            )
        elif len(candidates) == 1:
            self.candidate_label.setText(
                f"自动候选：{candidates[0].name}。请确认后再预览；角色不会仅凭文件名永久写死。"
            )
        else:
            self.candidate_label.setText(
                f"发现 {len(candidates)} 个背景候选；请选择本批次实际使用的文件。"
            )
        self._mode_changed()
        if self._explicit_background and previous is not None and previous.is_file():
            self.set_configuration(self.mode,previous,source_selection=previous_selection)

    def import_paths(self, paths):
        """Resolve a directory/master+data drop without treating linked chunks as samples."""
        from .file_drop import input_files
        from saxs_waxs_core.readers.detector_io import inspect_sources
        candidates=input_files(paths,supported_detector_path)
        candidates,_,errors,_=inspect_sources(candidates)
        if not candidates:
            raise ValueError('没有可读取的背景文件')
        if len(candidates)>1:
            names=[str(p) for p in candidates]
            chosen,accepted=QtWidgets.QInputDialog.getItem(self,'选择背景文件','背景文件',names,0,False)
            if not accepted:return
            selected=Path(chosen)
        else:selected=Path(candidates[0])
        if str(selected) in errors:
            raise ValueError(str(errors[str(selected)]))
        self.set_configuration(BackgroundMode.AUTO_AIR,selected)
        self.background_combo.setToolTip(str(selected))
        self.candidate_label.setText(f'已选择背景文件：{selected.name}。多帧背景请确认数据集和帧号。')

    def set_current_sample(self, path: Path | None, exposure_s: float | None) -> None:
        if path is None:
            self.sample_label.setText("当前样品：未载入")
            return
        exposure_text = "未知" if exposure_s is None else f"{exposure_s:g} s"
        self.sample_label.setText(f"当前样品：{path.name}（曝光 {exposure_text}）")
        # Never mix a native IMG raster with its potentially reoriented TIFF export.
        if self._external_path is None and self.background_path is not None:
            is_img = detector_base_suffix(path) == ".img"
            if (detector_base_suffix(self.background_path) == ".img") != is_img:
                for index, candidate in enumerate(self._background_paths):
                    if (detector_base_suffix(candidate) == ".img") == is_img:
                        self.background_combo.setCurrentIndex(index)
                        self.candidate_label.setText(f"同格式背景候选：{candidate.name}；请确认。")
                        break

    def set_configuration(
        self,
        mode: BackgroundMode,
        background_path: str | Path | None,
        *, assume_equal_exposure_if_missing: bool = True,source_selection=None,
    ) -> None:
        """Apply an explicit saved role mapping without filename inference."""
        self._explicit_background = background_path is not None
        self.equal_exposure_check.setChecked(assume_equal_exposure_if_missing)
        mode_index = self.mode_combo.findData(mode.value)
        if mode_index < 0:
            raise ValueError(f"unsupported background mode: {mode.value}")
        self.mode_combo.setCurrentIndex(mode_index)
        if mode is not BackgroundMode.AUTO_AIR:
            self._external_path = None
            self.candidate_label.setText("配方已载入：该处理方式不使用背景文件。")
            self._mode_changed()
            return
        if background_path is None:
            raise ValueError("auto-air configuration requires a background path")
        selected = Path(background_path).expanduser().absolute()
        matching_index = next(
            (
                index
                for index, candidate in enumerate(self._background_paths)
                if candidate.absolute() == selected
            ),
            None,
        )
        if matching_index is None:
            self._external_path = selected
            self.background_combo.addItem(selected.name, "external")
            matching_index = self.background_combo.count() - 1
            self.background_combo.setItemData(
                matching_index,
                str(selected),
                QtCore.Qt.ItemDataRole.ToolTipRole,
            )
        else:
            self._external_path = None
        self.background_combo.setCurrentIndex(matching_index)
        self.candidate_label.setText(
            f"配方明确映射：{selected.name}。执行时还会核验文件大小和SHA-256。"
        )
        self._mode_changed()
        if source_selection:
            self._update_air_source()
            dataset=source_selection.get('dataset')
            index=self.air_dataset_combo.findData(dataset)
            if index<0:raise ValueError('Saved air dataset is no longer available')
            self.air_dataset_combo.setCurrentIndex(index)
            frame=source_selection.get('frame_index',0)
            if not 0<=frame<self.air_frame_spin.maximum():raise ValueError('Saved air frame is no longer available')
            self.air_frame_spin.setValue(frame+1)

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        color = "#A4262C" if error else "#126B34"
        self.status_label.setStyleSheet(f"color:{color};")

    @QtCore.Slot()
    def _mode_changed(self) -> None:
        needs_air = self.mode is BackgroundMode.AUTO_AIR
        self.background_combo.setEnabled(needs_air)
        self.browse_button.setEnabled(needs_air)
        self.air_dataset_combo.setEnabled(needs_air)
        self.air_frame_spin.setEnabled(needs_air)
        self.equal_exposure_check.setEnabled(needs_air)
        self.formula_label.setVisible(needs_air)

    @QtCore.Slot()
    def _choose_background_folder(self):
        selected=QtWidgets.QFileDialog.getExistingDirectory(self,'选择背景文件夹',str(Path.cwd()))
        if selected:
            try:self.import_paths([selected])
            except (OSError,ValueError) as exc:self.set_status(str(exc),error=True)

    @QtCore.Slot()
    def _choose_background(self) -> None:
        selected, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择背景文件",
            str(self.background_path.parent if self.background_path else Path.cwd()),
            "二维探测器图像 (*.tif *.tiff *.img *.gfrm *.sfrm *.cbf *.edf *.h5 *.hdf5 *.nxs *.nx5 *.gz *.bz2);;所有文件 (*)",
        )
        if not selected:
            return
        try:self.import_paths([selected])
        except (OSError,ValueError) as exc:self.set_status(str(exc),error=True)

    @QtCore.Slot()
    def _request_preview(self) -> None:
        if self.mode is BackgroundMode.AUTO_AIR and not self.source_ready:
            self.set_status('请先明确选择背景数据集和帧。',error=True)
            return
        path = self.background_path if self.mode is BackgroundMode.AUTO_AIR else None
        if self.mode is BackgroundMode.AUTO_AIR and path is None:
            self.set_status("请先选择背景文件。", error=True)
            return
        self._explicit_background = path is not None
        self.preview_requested.emit(self.mode, path)
