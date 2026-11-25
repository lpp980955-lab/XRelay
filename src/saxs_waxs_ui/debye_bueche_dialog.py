"""Independent candidate Debye–Bueche workspace for processed radial curves."""
from .export_naming import choose_export_folder
from pathlib import Path
import json
import re
import threading

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets
from openpyxl import Workbook

from saxs_waxs_core.debye_bueche import fit_debye_bueche
from .saxs_analysis_dialog import radial_q_intensity


class _Signals(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal(object)


class DebyeBuecheDialog(QtWidgets.QDialog):
    def __init__(self, curves, parent=None, parameter_contexts=None):
        super().__init__(parent)
        self.setWindowTitle("Debye–Bueche 候选分析")
        self.resize(1040, 700)
        self.curves = list(curves)
        self.parameter_contexts = ([dict(item) for item in parameter_contexts]
                                   if parameter_contexts is not None else [{} for _ in self.curves])
        if len(self.curves) != len(self.parameter_contexts):
            raise ValueError("curve parameter context count does not match curves")
        self.results = {}
        self.batch_active = False
        self.image_export_active = False
        self.batch_started = False
        self.batch_stop = threading.Event()
        self.batch_thread = None
        self.image_signals = _Signals(self)
        self.image_signals.progress.connect(self._image_progress)
        self.image_signals.finished.connect(self._image_finished)
        self.signals = _Signals(self)
        self.signals.progress.connect(self._batch_progress)
        self.signals.finished.connect(self._batch_finished)

        self.selector = QtWidgets.QComboBox()
        for name, _ in self.curves:
            self.selector.addItem(name)
        self.model = QtWidgets.QComboBox()
        self.model.addItem("单组分", "single")
        self.model.addItem("双组分", "double")
        self.q_unit = QtWidgets.QComboBox()
        self.q_unit.addItem("nm⁻¹", "nm^-1")
        self.q_unit.addItem("Å⁻¹", "A^-1")
        self.background = QtWidgets.QCheckBox("拟合常数背景")
        self.background.setChecked(True)
        self.lower = QtWidgets.QDoubleSpinBox()
        self.upper = QtWidgets.QDoubleSpinBox()
        for spin in (self.lower, self.upper):
            spin.setRange(0, 1e9)
            spin.setDecimals(6)
            spin.setKeyboardTracking(False)
        initial_lower, initial_upper = self._initial_q_range()
        self.lower.setValue(initial_lower)
        self.upper.setValue(initial_upper)
        self._last_q_unit = "nm^-1"
        self.plot = pg.PlotWidget(background="w")
        self.plot.setLabel("bottom", "q (nm⁻¹)")
        self.plot.setLabel("left", "Intensity")
        self.residual_plot = pg.PlotWidget(background="w")
        self.residual_plot.setLabel("left", "Residual")
        self.residual_plot.setLabel("bottom", "q (nm⁻¹)")
        self.residual_plot.setXLink(self.plot)
        self.summary = QtWidgets.QLabel("明确选择模型和 q 范围后计算；相关长度不直接等于孔径。")
        self.summary.setWordWrap(True)
        self.label = self.summary  # Shared parent-window close protection uses this status label.
        run = QtWidgets.QPushButton("计算当前")
        self.batch_button = QtWidgets.QPushButton("计算整批")
        self.export_button = QtWidgets.QPushButton("导出工作簿…")
        self.export_images_button = QtWidgets.QPushButton("导出拟合图…")
        form = QtWidgets.QGridLayout()
        form.addWidget(QtWidgets.QLabel("曲线"), 0, 0)
        form.addWidget(self.selector, 0, 1, 1, 5)
        form.addWidget(QtWidgets.QLabel("模型"), 1, 0)
        form.addWidget(self.model, 1, 1)
        form.addWidget(QtWidgets.QLabel("q 单位"), 1, 2)
        form.addWidget(self.q_unit, 1, 3)
        form.addWidget(self.background, 1, 4, 1, 2)
        form.addWidget(QtWidgets.QLabel("q 范围"), 2, 0)
        form.addWidget(self.lower, 2, 1)
        form.addWidget(QtWidgets.QLabel("—"), 2, 2)
        form.addWidget(self.upper, 2, 3)
        form.addWidget(run, 2, 4)
        form.addWidget(self.batch_button, 2, 5)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.summary)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        splitter.addWidget(self.plot)
        splitter.addWidget(self.residual_plot)
        splitter.setSizes([480, 140])
        layout.addWidget(splitter, 1)
        export_row = QtWidgets.QHBoxLayout()
        export_row.addWidget(self.export_button)
        export_row.addWidget(self.export_images_button)
        self.export_button.setText("导出结果…")
        self.export_images_button.hide()
        layout.addLayout(export_row)
        self.selector.currentIndexChanged.connect(self.show_current)
        self.model.currentIndexChanged.connect(self.invalidate)
        self.q_unit.currentIndexChanged.connect(self._unit_changed)
        for spin in (self.lower, self.upper):
            spin.valueChanged.connect(self.invalidate)
        self.background.toggled.connect(self.invalidate)
        run.clicked.connect(self.compute_current)
        self.batch_button.clicked.connect(self.start_batch)
        self.export_button.clicked.connect(self.export_dialog)
        self.export_images_button.clicked.connect(self.export_images_dialog)
        self._batch_controls = (self.selector, self.model, self.q_unit, self.background,
                                self.lower, self.upper, run, self.export_button,
                                self.export_images_button)

    def _initial_q_range(self):
        limits = []
        for _, curve in self.curves:
            try:
                q, intensity = radial_q_intensity(curve, "nm^-1")
            except ValueError:
                continue
            finite = q[np.isfinite(q) & (q >= 0) & np.isfinite(intensity)]
            if len(finite) >= 8:
                limits.append((float(finite.min()), float(finite.max())))
        if not limits:
            return 0.0, 1.0
        shared = (max(a for a, _ in limits), min(b for _, b in limits))
        return shared if shared[0] < shared[1] else limits[0]

    def _unit_changed(self, *_):
        new_unit = self.q_unit.currentData()
        if new_unit != self._last_q_unit:
            factor = .1 if new_unit == "A^-1" else 10.0
            with QtCore.QSignalBlocker(self.lower), QtCore.QSignalBlocker(self.upper):
                self.lower.setValue(self.lower.value() * factor)
                self.upper.setValue(self.upper.value() * factor)
            self._last_q_unit = new_unit
        self.invalidate()

    def _settings(self):
        return (self.model.currentData(), self.q_unit.currentData(),
                (self.lower.value(), self.upper.value()), self.background.isChecked())

    @staticmethod
    def _calculate_curve(curve, model, q_unit, q_range, fit_background):
        q, intensity = radial_q_intensity(curve, q_unit)
        return fit_debye_bueche(q, intensity, model=model, q_range=q_range,
                                q_unit=q_unit, constant_background=fit_background)

    def invalidate(self, *_):
        self.results.clear()
        self.batch_started = False
        self.show_current()

    def compute_current(self):
        index = self.selector.currentIndex()
        if index < 0:
            return
        try:
            self.results[index] = self._calculate_curve(self.curves[index][1], *self._settings())
        except ValueError as exc:
            self.results[index] = str(exc)
        self.show_current()

    def start_batch(self):
        if self.batch_active:
            self.batch_stop.set()
            self.batch_button.setEnabled(False)
            self.summary.setText("将在当前曲线完成后停止。")
            return
        if not self.curves:
            self.summary.setText("没有可计算的曲线。")
            return
        self.results.clear()
        self.batch_active = self.batch_started = True
        self.batch_stop.clear()
        for widget in self._batch_controls:
            widget.setEnabled(False)
        self.batch_button.setText("完成当前曲线后停止")
        self.batch_thread = threading.Thread(target=self._batch_worker,
            args=(tuple(curve for _, curve in self.curves), self._settings(),
                  self.batch_stop, self.signals), name="debye-bueche-batch", daemon=True)
        self.batch_thread.start()

    @staticmethod
    def _batch_worker(curves, settings, stop, signals):
        results = {}
        for index, curve in enumerate(curves):
            if stop.is_set():
                break
            try:
                results[index] = DebyeBuecheDialog._calculate_curve(curve, *settings)
            except Exception as exc:
                results[index] = f"{type(exc).__name__}: {exc}"
            signals.progress.emit(index+1, len(curves))
        signals.finished.emit(results)

    def _batch_progress(self, done, total):
        self.summary.setText(f"正在计算 {done}/{total} 条曲线…")

    def _batch_finished(self, results):
        self.results = results
        self.batch_active = False
        self.batch_thread = None
        for widget in self._batch_controls:
            widget.setEnabled(True)
        self.batch_button.setEnabled(True)
        self.batch_button.setText("计算整批")
        self.show_current()
        failed = sum(isinstance(value, str) for value in results.values())
        self.summary.setText(f"完成 {len(results)}/{len(self.curves)} 条，失败 {failed}。 "
                             + self.summary.text())

    def closeEvent(self, event):
        if self.image_export_active:
            self.image_stop.set()
            self.summary.setText("正在完成当前图片并清理本次导出，请稍后关闭。")
            event.ignore()
            return
        if self.batch_active:
            self.batch_stop.set()
            self.summary.setText("正在完成当前曲线，请稍后关闭。")
            event.ignore()
            return
        super().closeEvent(event)

    def show_current(self, *_):
        self.plot.clear()
        self.residual_plot.clear()
        index = self.selector.currentIndex()
        if index < 0:
            return
        result = self.results.get(index)
        if result is None:
            self.summary.setText("尚未计算；先确认 q 范围、背景和单/双组分模型。")
            return
        if isinstance(result, str):
            self.summary.setText(f"失败：{result}")
            return
        unit = "nm⁻¹" if result.q_unit == "nm^-1" else "Å⁻¹"
        self.plot.setLabel("bottom", f"q ({unit})")
        self.residual_plot.setLabel("bottom", f"q ({unit})")
        self.plot.plot(result.q, result.intensity, pen=None, symbol="o", symbolSize=4)
        self.plot.plot(result.q, result.fitted_intensity, pen=pg.mkPen("#C43D4D", width=2))
        self.residual_plot.plot(result.q, result.residual, pen=pg.mkPen("#277DA1", width=1.4))
        lengths = ", ".join(f"{value:.5g}" for value in result.correlation_lengths)
        status = "需人工复核" if result.diagnostic_flags else "数值候选"
        self.summary.setText(f"{status}｜{result.model}：Lc = {lengths} {result.length_unit}；"
                             f"R² = {result.r_squared:.5g}；RMSE = {result.rmse:.5g}；"
                             f"AIC = {result.aic:.5g}；请检查曲线残差与模型适用性。 "
                             + "；".join(result.warnings))

    def export_dialog(self):
        from .analysis_export import choose_analysis_export
        return choose_analysis_export(self)

    def export_legacy_dialog(self):
        if not self.results:
            self.summary.setText("请先计算当前或整批曲线。")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 Debye–Bueche 结果", "", "Excel (*.xlsx)")
        if path:
            try:
                self.export_workbook(Path(path))
            except (OSError, ValueError) as exc:
                self.summary.setText(f"导出失败：{exc}")

    def export_images_dialog(self):
        scope, accepted = QtWidgets.QInputDialog.getItem(
            self, "导出范围", "曲线", ("当前曲线", "全部曲线"), 0, False)
        if not accepted:
            return
        fmt, accepted = QtWidgets.QInputDialog.getItem(
            self, "图片格式", "格式", ("PNG", "TIFF", "PDF", "SVG"), 0, False)
        if not accepted:
            return
        directory = choose_export_folder(self, "选择图片导出文件夹")
        if not directory:
            return
        indices = ([self.selector.currentIndex()] if scope == "当前曲线"
                   else range(len(self.curves)))
        self.start_image_export(Path(directory), indices, image_format=fmt.lower())

    def _image_snapshot(self):
        return list(self.curves), dict(self.results), list(self.parameter_contexts)

    def start_image_export(self, directory, indices, *, image_format="png", dpi=600):
        if self.image_export_active or self.batch_active:
            return
        snapshot = self._image_snapshot()
        self.image_export_active = True
        self.image_stop = threading.Event()
        self.image_progress = QtWidgets.QProgressDialog("准备导出图片…", "停止导出", 0, len(indices), self)
        self.image_progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        self.image_progress.canceled.connect(self.image_stop.set)
        self.image_progress.show()
        for widget in self._batch_controls:
            widget.setEnabled(False)
        self.image_thread = threading.Thread(target=self._image_worker,
            args=(snapshot, directory, list(indices), image_format, dpi, self.image_stop, self.image_signals),
            daemon=True)
        self.image_thread.start()

    @staticmethod
    def _image_worker(snapshot, directory, indices, image_format, dpi, stop, signals):
        try:
            paths = DebyeBuecheDialog._write_images(snapshot, directory, indices,
                image_format=image_format, dpi=dpi, stop=stop, progress=signals.progress.emit)
            signals.finished.emit((paths, None))
        except Exception as exc:
            signals.finished.emit(((), str(exc)))

    def _image_progress(self, done, total):
        self.image_progress.setMaximum(total)
        self.image_progress.setValue(done)

    def _image_finished(self, outcome):
        self.image_export_active = False
        self.image_thread = None
        self.image_progress.close()
        self.image_progress.deleteLater()
        for widget in self._batch_controls:
            widget.setEnabled(True)
        paths, error = outcome
        self.summary.setText(f"图片导出未完成：{error}" if error else
                           f"已导出 {len(paths)} 张图和一份参数说明。")

    def export_images(self, directory, indices, *, image_format="png", dpi=600):
        return self._write_images(self._image_snapshot(), directory, indices,
                                  image_format=image_format, dpi=dpi)

    @staticmethod
    def _write_images(snapshot, directory, indices, *, image_format="png", dpi=600,
                      stop=None, progress=None):
        curves, results, contexts = snapshot
        from saxs_waxs_core.export_context import summarize_processing_contexts
        from .rendering import publication_render_context
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from .integration_panel import IntegrationPanel

        suffixes = {"png": ".png", "tiff": ".tif", "pdf": ".pdf", "svg": ".svg"}
        if image_format not in suffixes or not isinstance(dpi, int) or dpi <= 0:
            raise ValueError("不支持的格式或 DPI")
        root = Path(directory).expanduser().resolve()
        protected = (Path(__file__).resolve().parents[2] / "data").resolve()
        if root == protected or protected in root.parents:
            raise ValueError("不能写入受保护的原始数据目录")
        selected = list(indices)
        if (not selected or len(selected) != len(set(selected))
                or any(i < 0 or i >= len(curves) for i in selected)):
            raise ValueError("请选择有效曲线")
        successes = [i for i in selected if i in results and not isinstance(results[i], str)]
        if not successes:
            raise ValueError("所选曲线尚无可导出的拟合结果")
        seen, planned = set(), []
        for index in successes:
            stem = re.sub(r'[<>:"/\\|?*]', "_", Path(curves[index][0]).stem).strip(" .") or "sample"
            base, counter = stem, 2
            while stem.casefold() in seen:
                stem, counter = f"{base}_{counter}", counter+1
            seen.add(stem.casefold())
            planned.append(root / f"{stem}_debye_{results[index].model}{suffixes[image_format]}")
        note = root / "debye_bueche_image_parameters.txt"
        if any(path.exists() for path in (*planned, note)):
            raise FileExistsError("图片或参数说明已存在，请选择其他文件夹")
        root.mkdir(parents=True, exist_ok=True)
        created = []
        try:
            records = []
            with publication_render_context(IntegrationPanel._publication_matplotlib_style()):
                for number, (index, path) in enumerate(zip(successes, planned), 1):
                    if stop is not None and stop.is_set():
                        raise InterruptedError("已停止；本次导出文件已清理")
                    result = results[index]
                    fig = Figure(figsize=(180/25.4, 140/25.4), dpi=dpi)
                    FigureCanvasAgg(fig)
                    top, bottom = fig.subplots(2, 1, sharex=True, height_ratios=(3, 1))
                    top.plot(result.q, result.intensity, "o", color="#555555", markersize=2.8, label="Data")
                    top.plot(result.q, result.fitted_intensity, color="#C43D4D", linewidth=1.8, label="Fit")
                    bottom.plot(result.q, result.residual, color="#277DA1", linewidth=1.2)
                    bottom.axhline(0, color="#777777", linewidth=.7)
                    top.set_ylabel("Intensity")
                    bottom.set_ylabel("Residual")
                    bottom.set_xlabel(f"q ({result.q_unit})")
                    for ax in (top, bottom):
                        ax.tick_params(direction="in", top=False, right=False)
                    top.legend(loc="best")
                    fig.tight_layout()
                    with path.open("xb") as stream:
                        created.append(path)
                        fig.savefig(stream, format=image_format, dpi=dpi)
                    fig.clear()
                    records.append({"sample": curves[index][0], "image": path.name,
                                    "model": result.model, "q_range": result.q_range,
                                    "q_unit": result.q_unit, "correlation_lengths": result.correlation_lengths,
                                    "length_unit": result.length_unit, "amplitudes": result.amplitudes,
                                    "constant_background": result.constant_background,
                                    "background_fitted": result.background_fitted,
                                    "r_squared": result.r_squared, "rmse": result.rmse,
                                    "diagnostic_flags": result.diagnostic_flags,
                                    "warnings": result.warnings})
                    if progress is not None:
                        progress(number, len(planned))
            if stop is not None and stop.is_set():
                raise InterruptedError("已停止；本次导出文件已清理")
            failures = [{"sample": curves[i][0], "reason": results[i]}
                        for i in selected if i in results and isinstance(results[i], str)]
            unprocessed = [curves[i][0] for i in selected if i not in results]
            with note.open("x", encoding="utf-8") as stream:
                created.append(note)
                stream.write("Debye–Bueche 候选拟合图参数和处理记录\n")
                stream.write(json.dumps({"format": image_format, "dpi": dpi,
                                         "processing_context": summarize_processing_contexts(
                        (i, curves[i][0], contexts[i]) for i in selected),
                    "items": records, "failures": failures,
                                         "unprocessed": unprocessed},
                                        ensure_ascii=False, indent=2, default=str))
        except Exception:
            for created_path in reversed(created):
                created_path.unlink(missing_ok=True)
            raise
        return tuple(planned)

    def export_workbook(self, path):
        from .analysis_export import export_analysis_workbook
        return export_analysis_workbook(self,path)

    def export_detailed_workbook(self, path):
        path = Path(path).expanduser().resolve()
        protected = (Path(__file__).resolve().parents[2] / "data").resolve()
        if path == protected or protected in path.parents:
            raise ValueError("不能写入受保护的原始数据目录")
        book = Workbook()
        summary = book.active
        summary.title = "Summary"
        summary.append(["Sample", "Status", "Model", "q Min", "q Max", "q Unit",
                        "Lc1", "Lc2", "Lc Unit", "A1", "A2", "Ifl", "Background Fitted",
                        "R²", "RMSE", "RSS", "AIC", "BIC", "Warnings", "Diagnostic Flags"])
        curves = book.create_sheet("Curves")
        info = book.create_sheet("Processing Info")
        info.append(["Sample", "Source", "Center X (px)", "Center Y (px)",
                     "SDD (mm)", "Wavelength (nm)", "Pixel X (µm)", "Pixel Y (µm)",
                     "Integration Range", "Sectors", "Background Mode", "Background Source",
                     "Excluded Pixels", "Processing Context"])
        for index in (range(len(self.curves)) if self.batch_started else sorted(self.results)):
            name, curve = self.curves[index]
            result = self.results.get(index)
            context = self.parameter_contexts[index]
            geometry = curve.audit.get("geometry_summary", {})
            center = geometry.get("beam_center_px", {})
            pixels = geometry.get("pixel_size_um", {})
            processing = context.get("processing", {})
            background = processing.get("background", {}) if isinstance(processing, dict) else {}
            info.append([name, context.get("source"), center.get("x"), center.get("y"),
                         geometry.get("sample_detector_distance_mm"), geometry.get("wavelength_nm"),
                         pixels.get("x"), pixels.get("y"), str(curve.audit.get("radial_range")),
                         json.dumps(curve.audit.get("sectors"), ensure_ascii=False),
                         background.get("mode"), background.get("background_source"),
                         curve.audit.get("excluded_pixel_count"),
                         json.dumps(context, ensure_ascii=False, default=str)])
            if result is None:
                summary.append([name, "not_processed", self.model.currentData(),
                                self.lower.value(), self.upper.value(), self.q_unit.currentData()])
                continue
            if isinstance(result, str):
                summary.append([name, "failed", self.model.currentData(),
                                self.lower.value(), self.upper.value(), self.q_unit.currentData(),
                                None, None, None, None, None, None, None, None, None, None,
                                None, None, result, None])
                continue
            lengths, amplitudes = result.correlation_lengths, result.amplitudes
            summary.append([name, "review_required" if result.diagnostic_flags else "candidate",
                            result.model, *result.q_range, result.q_unit,
                            lengths[0], lengths[1] if len(lengths) > 1 else None, result.length_unit,
                            amplitudes[0], amplitudes[1] if len(amplitudes) > 1 else None,
                            result.constant_background, result.background_fitted,
                            result.r_squared, result.rmse, result.rss, result.aic, result.bic,
                            " | ".join(result.warnings), " | ".join(result.diagnostic_flags)])
            col = index*4 + 1
            for offset, title in enumerate((f"{name} q ({result.q_unit})", "I observed", "I fit", "Residual")):
                curves.cell(1, col+offset, title)
            for row, values in enumerate(zip(result.q, result.intensity,
                                             result.fitted_intensity, result.residual), 2):
                for offset, value in enumerate(values):
                    curves.cell(row, col+offset, float(value))
        with path.open("xb") as stream:
            book.save(stream)
        return path
