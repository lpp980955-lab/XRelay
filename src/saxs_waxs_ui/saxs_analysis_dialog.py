"""Small independent Guinier/Porod workspace for radial integration curves."""
from .export_naming import choose_export_folder
from pathlib import Path
import json
import re
import threading
import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets
from openpyxl import Workbook
from saxs_waxs_core.saxs_power_analysis import fit_saxs_power


class _AnalysisSignals(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    finished = QtCore.Signal(object)


from saxs_waxs_core.curve_coordinates import radial_q_intensity


class SaxsAnalysisDialog(QtWidgets.QDialog):
    def __init__(self, curves, parent=None, parameter_contexts=None):
        super().__init__(parent)
        self.setWindowTitle("Guinier / Porod 分析")
        self.resize(920, 650)
        self.curves = list(curves)
        self.parameter_contexts = ([dict(value) for value in parameter_contexts]
                                   if parameter_contexts is not None else [{} for _ in self.curves])
        if len(self.parameter_contexts) != len(self.curves):
            raise ValueError("curve parameter context count does not match curves")
        self.results = {}
        self.batch_active = False
        self.image_export_active = False
        self.batch_started = False
        self.batch_stop = threading.Event()
        self.batch_thread = None
        self.image_signals = _AnalysisSignals(self)
        self.image_signals.progress.connect(self._image_progress)
        self.image_signals.finished.connect(self._image_finished)
        self.batch_signals = _AnalysisSignals(self)
        self.batch_signals.progress.connect(self._batch_progress)
        self.batch_signals.finished.connect(self._batch_finished)
        self.selector = QtWidgets.QComboBox()
        for name, _ in self.curves:
            self.selector.addItem(name)
        self.method = QtWidgets.QComboBox()
        self.method.addItem("Guinier", "guinier")
        self.method.addItem("Porod 幂律", "porod")
        self.q_unit = QtWidgets.QComboBox()
        self.q_unit.addItem("nm⁻¹", "nm^-1")
        self.q_unit.addItem("Å⁻¹", "A^-1")
        self.plot_mode = QtWidgets.QComboBox()
        self.plot_mode.addItem("原始 I–q", "original")
        self.plot_mode.addItem("线性化", "linearized")
        self.lower, self.upper = QtWidgets.QDoubleSpinBox(), QtWidgets.QDoubleSpinBox()
        for spin in (self.lower, self.upper):
            spin.setRange(0, 1e9)
            spin.setDecimals(6)
        self.lower.setValue(.01)
        self.upper.setValue(.1)
        self.label = QtWidgets.QLabel("范围使用所选 q 单位；使用已处理的强度，不自动扣除背景。")
        self.label.setWordWrap(True)
        self.plot = pg.PlotWidget(background="w")
        self.plot.setLabel("bottom", "q")
        self.plot.setLabel("left", "Intensity")
        run = QtWidgets.QPushButton("计算当前")
        batch = self.batch_button = QtWidgets.QPushButton("计算全部")
        export = QtWidgets.QPushButton("导出结果…")
        export_images = QtWidgets.QPushButton("导出拟合图…")
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        form.addRow("曲线", self.selector)
        form.addRow("方法", self.method)
        form.addRow("q 单位", self.q_unit)
        form.addRow("显示", self.plot_mode)
        form.addRow("q 下限", self.lower)
        form.addRow("q 上限", self.upper)
        layout.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        for button in (run, batch, export, export_images):
            row.addWidget(button)
        layout.addLayout(row)
        layout.addWidget(self.label)
        layout.addWidget(self.plot, 1)
        run.clicked.connect(self.compute_current)
        batch.clicked.connect(self.start_batch)
        export.clicked.connect(self.export_dialog)
        export_images.clicked.connect(self.export_images_dialog)
        export_images.hide()
        self.selector.currentIndexChanged.connect(self.show_current)
        self.plot_mode.currentIndexChanged.connect(self.show_current)
        self.method.currentIndexChanged.connect(self.invalidate)
        self.q_unit.currentIndexChanged.connect(self.invalidate)
        self.lower.valueChanged.connect(self.invalidate)
        self.upper.valueChanged.connect(self.invalidate)
        self._batch_controls = (self.selector, self.method, self.q_unit, self.lower, self.upper,
                                run, export, export_images)

    def invalidate(self, *_):
        self.results.clear()
        self.batch_started = False
        self.show_current()

    def _calculate(self, index):
        return self._calculate_curve(self.curves[index][1], self.method.currentData(),
                                     self.q_unit.currentData(), (self.lower.value(), self.upper.value()))

    @staticmethod
    def _calculate_curve(curve, method, q_unit, q_range):
        q, intensity = radial_q_intensity(curve, q_unit)
        return fit_saxs_power(q, intensity, method=method, q_range=q_range, q_unit=q_unit)

    def compute_current(self):
        index = self.selector.currentIndex()
        if index < 0:
            return
        try:
            self.results[index] = self._calculate(index)
        except ValueError as exc:
            self.results[index] = str(exc)
        self.show_current()

    def compute_all(self):
        for index in range(len(self.curves)):
            try:
                self.results[index] = self._calculate(index)
            except ValueError as exc:
                self.results[index] = str(exc)
        self.show_current()

    def start_batch(self):
        if self.batch_active:
            self.batch_stop.set()
            self.batch_button.setEnabled(False)
            self.label.setText("将在当前曲线计算结束后停止。")
            return
        if not self.curves:
            self.label.setText("没有可计算的曲线。")
            return
        self.results.clear()
        self.batch_started = True
        self.batch_active = True
        self.batch_stop.clear()
        for widget in self._batch_controls:
            widget.setEnabled(False)
        self.batch_button.setText("完成当前曲线后停止")
        settings = (self.method.currentData(), self.q_unit.currentData(),
                    (self.lower.value(), self.upper.value()))
        self.batch_thread = threading.Thread(target=self._batch_worker,
            args=(tuple(curve for _, curve in self.curves), settings, self.batch_stop, self.batch_signals),
            name="saxs-analysis-batch", daemon=True)
        self.batch_thread.start()

    @staticmethod
    def _batch_worker(curves, settings, stop, signals):
        results = {}
        for index, curve in enumerate(curves):
            if stop.is_set():
                break
            try:
                results[index] = SaxsAnalysisDialog._calculate_curve(curve, *settings)
            except Exception as exc:
                results[index] = f"{type(exc).__name__}: {exc}"
            signals.progress.emit(index + 1, len(curves))
        signals.finished.emit(results)

    def _batch_progress(self, done, total):
        self.label.setText(f"正在计算 {done} / {total} 条曲线…")

    def _batch_finished(self, results):
        self.results = results
        self.batch_active = False
        self.batch_thread = None
        for widget in self._batch_controls:
            widget.setEnabled(True)
        self.batch_button.setEnabled(True)
        self.batch_button.setText("计算全部")
        self.show_current()
        failed = sum(isinstance(value, str) for value in results.values())
        self.label.setText(f"已计算 {len(results)} / {len(self.curves)} 条，失败 {failed}；"
                           "切换曲线查看结果。 " + self.label.text())

    def closeEvent(self, event):
        if self.image_export_active:
            self.image_stop.set()
            self.label.setText("正在完成当前图片并清理本次导出，请稍后关闭。")
            event.ignore()
            return
        if self.batch_active:
            self.batch_stop.set()
            self.label.setText("正在完成当前曲线，请稍后关闭。")
            event.ignore()
            return
        super().closeEvent(event)

    def show_current(self, *_):
        self.plot.clear()
        index = self.selector.currentIndex()
        if index < 0:
            return
        result = self.results.get(index)
        if result is None:
            self.label.setText("尚未计算；请确认 q 范围和背景处理。")
        elif isinstance(result, str):
            self.label.setText(f"未计算：{result}")
        else:
            if self.plot_mode.currentData() == "linearized":
                x = result.q ** 2 if result.method == "guinier" else np.log(result.q)
                y = np.log(result.intensity)
                fitted_y = result.slope * x + result.intercept
                self.plot.setLabel("bottom", f"q² ({result.q_unit})²" if result.method == "guinier"
                                   else f"ln[q / ({result.q_unit})]")
                self.plot.setLabel("left", "ln(I)")
            else:
                x, y, fitted_y = result.q, result.intensity, result.fitted_intensity
                self.plot.setLabel("bottom", f"q ({result.q_unit})")
                self.plot.setLabel("left", "Intensity")
            self.plot.plot(x, y, pen=None, symbol="o", symbolSize=4)
            self.plot.plot(x, fitted_y, pen=pg.mkPen("r", width=2))
            name = "Rg" if result.method == "guinier" else "Porod 指数"
            uncertainty = "" if result.derived_stderr is None else f" ± {result.derived_stderr:.3g}"
            i0 = "" if result.zero_intensity is None else f"；I(0) = {result.zero_intensity:.6g}"
            limit_warning = ("；q最大值 × Rg > 1.3，标准Guinier范围可能不适用"
                             if result.method == "guinier" and result.q.max() * result.derived_value > 1.3 else "")
            self.label.setText(f"{name} = {result.derived_value:.6g}{uncertainty} {result.derived_unit}{i0}；"
                               f"R² = {result.r_squared:.5g}；±为变换坐标回归标准误，"
                               f"候选结果请检查适用条件{limit_warning}。")

    def export_dialog(self):
        from .analysis_export import choose_analysis_export
        return choose_analysis_export(self)

    def export_legacy_dialog(self):
        if not self.results:
            self.label.setText("请先计算当前或全部曲线。")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出分析结果", "", "Excel (*.xlsx)")
        if path:
            try:
                self.export_workbook(Path(path))
            except (OSError, ValueError) as exc:
                self.label.setText(f"导出失败：{exc}")

    def export_images_dialog(self):
        scope, accepted = QtWidgets.QInputDialog.getItem(
            self, "导出范围", "曲线", ("当前曲线", "全部曲线"), 0, False)
        if not accepted:
            return
        image_format, accepted = QtWidgets.QInputDialog.getItem(
            self, "图片格式", "格式", ("PNG", "TIFF", "PDF", "SVG"), 0, False)
        if not accepted:
            return
        directory = choose_export_folder(self, "选择图片导出文件夹")
        if not directory:
            return
        indices = [self.selector.currentIndex()] if scope == "当前曲线" else range(len(self.curves))
        self.start_image_export(Path(directory), indices, image_format=image_format.lower())

    def _image_snapshot(self):
        return (list(self.curves), dict(self.results), list(self.parameter_contexts),
                self.batch_started, self.method.currentData(), self.plot_mode.currentData(),
                self.q_unit.currentData(), (self.lower.value(), self.upper.value()))

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
            paths = SaxsAnalysisDialog._write_images(snapshot, directory, indices,
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
        self.label.setText(f"图片导出未完成：{error}" if error else
                           f"已导出 {len(paths)} 张图和一份参数说明。")

    def export_images(self, directory, indices, *, image_format="png", dpi=600):
        return self._write_images(self._image_snapshot(), directory, indices,
                                  image_format=image_format, dpi=dpi)

    @staticmethod
    def _write_images(snapshot, directory, indices, *, image_format="png", dpi=600,
                      stop=None, progress=None):
        curves, results, contexts, batch_started, method, plot_mode, q_unit, q_range = snapshot
        from saxs_waxs_core.export_context import summarize_processing_contexts
        from .rendering import publication_render_context
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from .integration_panel import IntegrationPanel
        suffixes = {"png": ".png", "tiff": ".tif", "pdf": ".pdf", "svg": ".svg"}
        if image_format not in suffixes or not isinstance(dpi, int) or dpi <= 0:
            raise ValueError("不支持的格式或 DPI")
        root = Path(directory)
        protected = (Path(__file__).resolve().parents[2] / "data").resolve()
        resolved = root.resolve()
        if resolved == protected or protected in resolved.parents:
            raise ValueError("不能写入受保护的原始数据目录")
        selected = list(indices)
        if not selected or len(set(selected)) != len(selected) or any(i < 0 or i >= len(curves) for i in selected):
            raise ValueError("请选择有效曲线")
        for index in selected:
            if index not in results and not batch_started:
                try:
                    results[index] = SaxsAnalysisDialog._calculate_curve(curves[index][1], method, q_unit, q_range)
                except ValueError as exc:
                    results[index] = str(exc)
        successes = [i for i in selected if i in results and not isinstance(results[i], str)]
        if not successes:
            raise ValueError("没有可导出的有效拟合结果")
        root.mkdir(parents=True, exist_ok=True)
        paths, seen = [], set()
        for index in successes:
            stem = re.sub(r'[<>:"/\\|?*]', "_", Path(curves[index][0]).stem).strip(" .") or "sample"
            base, counter = stem, 2
            while stem.casefold() in seen:
                stem, counter = f"{base}_{counter}", counter + 1
            seen.add(stem.casefold())
            paths.append(root / f"{stem}_{method}{suffixes[image_format]}")
        note = root / f"{method}_image_parameters.txt"
        if any(path.exists() for path in [*paths, note]):
            raise FileExistsError("图片或参数说明已存在，请选择其他文件夹")
        created = []
        try:
            records = []
            with publication_render_context(IntegrationPanel._publication_matplotlib_style()):
                for number, (index, path) in enumerate(zip(successes, paths), 1):
                    if stop is not None and stop.is_set():
                        raise InterruptedError("已停止；本次导出文件已清理")
                    result = results[index]
                    if plot_mode == "linearized":
                        x = result.q**2 if result.method == "guinier" else np.log(result.q)
                        y = np.log(result.intensity)
                        fitted = result.slope*x + result.intercept
                        xlabel = f"q² ({result.q_unit})²" if result.method == "guinier" else f"ln[q / ({result.q_unit})]"
                        ylabel = "ln(I)"
                    else:
                        x, y, fitted = result.q, result.intensity, result.fitted_intensity
                        xlabel, ylabel = f"q ({result.q_unit})", "Intensity"
                    figure = Figure(figsize=(180/25.4, 130/25.4), dpi=dpi)
                    FigureCanvasAgg(figure)
                    ax = figure.add_subplot(111)
                    ax.plot(x, y, "o", markersize=3, color="#555555", label="Data")
                    ax.plot(x, fitted, color="#D1495B", linewidth=1.8, label="Fit")
                    ax.set_xlabel(xlabel)
                    ax.set_ylabel(ylabel)
                    ax.tick_params(direction="in", top=False, right=False)
                    ax.legend(loc="best")
                    figure.tight_layout()
                    with path.open("xb") as stream:
                        created.append(path)
                        figure.savefig(stream, format=image_format, dpi=dpi)
                    figure.clear()
                    records.append({"sample": curves[index][0], "image": path.name,
                        "q_range": result.q_range, "q_unit": result.q_unit, "value": result.derived_value,
                        "value_unit": result.derived_unit, "slope": result.slope, "intercept": result.intercept,
                        "r_squared": result.r_squared, "warnings": result.warnings})
                    if progress is not None:
                        progress(number, len(paths))
            if stop is not None and stop.is_set():
                raise InterruptedError("已停止；本次导出文件已清理")
            failures = [{"sample": curves[i][0], "reason": results[i]}
                        for i in selected if i in results and isinstance(results[i], str)]
            unprocessed = [curves[i][0] for i in selected if i not in results]
            with note.open("x", encoding="utf-8") as stream:
                created.append(note)
                stream.write("Guinier/Porod 拟合图参数与处理记录\n")
                stream.write(json.dumps({"method": method, "display": plot_mode,
                    "format": image_format, "dpi": dpi, "processing_context": summarize_processing_contexts(
                        (i, curves[i][0], contexts[i]) for i in selected),
                    "items": records, "failures": failures,
                    "unprocessed": unprocessed},
                    ensure_ascii=False, indent=2, default=str))
        except Exception:
            for created_path in reversed(created):
                created_path.unlink(missing_ok=True)
            raise
        return tuple(paths)

    def export_workbook(self, path):
        from .analysis_export import export_analysis_workbook
        return export_analysis_workbook(self,path)

    def export_detailed_workbook(self, path):
        protected = (Path(__file__).resolve().parents[2] / "data").resolve()
        resolved = path.expanduser().resolve()
        if resolved == protected or protected in resolved.parents:
            raise ValueError("不能写入受保护的原始数据目录")
        book = Workbook()
        summary = book.active
        summary.title = "Summary"
        summary.append(["Sample", "Status", "Method", "Range Start", "Range End", "q Unit",
                        "Value", "Value Unit", "Slope", "Intercept", "R²", "Warnings",
                        "Value StdErr", "Slope StdErr", "Intercept StdErr", "I(0)", "I(0) StdErr",
                        "Uncertainty Assumption"])
        curves_sheet = book.create_sheet("Curves")
        info_sheet = book.create_sheet("Processing Info")
        info_sheet.append(["Sample", "Source", "Center X (px)", "Center Y (px)", "SDD (mm)",
                           "Wavelength (nm)", "Pixel X (µm)", "Pixel Y (µm)", "Integration Range",
                           "Sectors", "Background Mode", "Background Source", "Sample Exposure (s)",
                           "Background Exposure (s)", "Excluded Pixels", "Processing Context"])
        indices = range(len(self.curves)) if self.batch_started else sorted(self.results)
        for index in indices:
            result = self.results.get(index)
            name = self.curves[index][0]
            context = self.parameter_contexts[index]
            processing = context.get("processing", {})
            processing = processing if isinstance(processing, dict) else {}
            audit = dict(self.curves[index][1].audit)
            geometry = audit.get("geometry_summary", {})
            center = geometry.get("beam_center_px", {})
            pixels = geometry.get("pixel_size_um", {})
            background = processing.get("background", {})
            info_sheet.append([name, context.get("source"), center.get("x"), center.get("y"),
                geometry.get("sample_detector_distance_mm"), geometry.get("wavelength_nm"),
                pixels.get("x"), pixels.get("y"), str(audit.get("radial_range")),
                json.dumps(audit.get("sectors"), ensure_ascii=False), background.get("mode"),
                background.get("background_source"), background.get("sample_exposure_s"),
                background.get("background_exposure_s"), audit.get("excluded_pixel_count"),
                json.dumps(context, ensure_ascii=False, default=str)])
            if result is None:
                summary.append([name, "not_processed", self.method.currentData(), self.lower.value(),
                                self.upper.value(), self.q_unit.currentData()])
                continue
            if isinstance(result, str):
                summary.append([name, "failed", self.method.currentData(), self.lower.value(),
                                self.upper.value(), self.q_unit.currentData(), None, None,
                                None, None, None, result])
                continue
            summary.append([name, "candidate", result.method, *result.q_range, result.q_unit,
                            result.derived_value, result.derived_unit, result.slope,
                            result.intercept, result.r_squared, " | ".join(result.warnings),
                            result.derived_stderr, result.slope_stderr, result.intercept_stderr,
                            result.zero_intensity, result.zero_intensity_stderr,
                            "unweighted linear regression in transformed coordinates"])
            col = index * 7 + 1
            x = result.q ** 2 if result.method == "guinier" else np.log(result.q)
            transformed = np.log(result.intensity)
            fit_transformed = result.slope * x + result.intercept
            transform_label = f"q² ({result.q_unit})²" if result.method == "guinier" else f"ln[q / ({result.q_unit})]"
            for offset, header in enumerate((f"{name} q ({result.q_unit})", "I observed", "I fit", "residual",
                                              transform_label, "ln(I)", "ln(I) fit")):
                curves_sheet.cell(1, col + offset, header)
            for row, values in enumerate(zip(result.q, result.intensity, result.fitted_intensity, result.residual,
                                               x, transformed, fit_transformed), 2):
                for offset, value in enumerate(values):
                    curves_sheet.cell(row, col + offset, float(value))
        with path.open("xb") as stream:
            book.save(stream)
        return path
