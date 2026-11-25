"""Compact fit exports: plotting columns, one statistical table, provenance."""
from pathlib import Path
import numpy as np
from PySide6 import QtWidgets

from saxs_waxs_core.herman import from_single_peak_fit


class FitExportDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, *, scope_items=None, note_text=None):
        super().__init__(parent)
        self.setWindowTitle("导出结果")
        self.scope_combo = QtWidgets.QComboBox()
        for label, value in scope_items or (("当前样品 · 勾选峰", "selected"),
                             ("当前样品 · 全部峰", "current"), ("本窗口全部样品 · 全部峰", "batch")):
            self.scope_combo.addItem(label, value)
        self.scope_combo.setCurrentIndex(1 if scope_items is None else 0)
        self.content_combo = QtWidgets.QComboBox()
        self.content_combo.addItem("数据工作簿（.xlsx）", "data")
        self.content_combo.addItem("图片", "images")
        self.format_combo = QtWidgets.QComboBox()
        self.format_combo.addItems(["PNG", "TIFF", "PDF", "SVG"])
        self.format_label = QtWidgets.QLabel("图片格式")
        form = QtWidgets.QFormLayout()
        form.addRow("范围", self.scope_combo)
        form.addRow("内容", self.content_combo)
        form.addRow(self.format_label, self.format_combo)
        note = QtWidgets.QLabel(note_text or "每个样品保留完整原始曲线，附上已拟合峰的曲线。\n工作簿只含曲线数据、参数汇总、处理说明。")
        note.setWordWrap(True)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText("选择保存位置…")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self.content_combo.currentIndexChanged.connect(self._content_changed)
        self._content_changed()
        self.resize(440, self.sizeHint().height())

    def _content_changed(self):
        images = self.content_combo.currentData() == "images"
        self.format_label.setVisible(images)
        self.format_combo.setVisible(images)


def export_compact_fit_workbook(panel, path, indices, *, selected_peaks=False):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    target = Path(path).expanduser().resolve()
    protected = (Path(__file__).resolve().parents[2] / "data").resolve()
    if target == protected or protected in target.parents:
        raise ValueError("不能写入原始数据目录")
    if target.suffix.lower() != ".xlsx":
        raise ValueError("拟合数据使用 .xlsx 工作簿")
    indices = list(dict.fromkeys(int(i) for i in indices))
    if not indices or any(i < 0 or i >= len(panel._curves) for i in indices):
        raise ValueError("请选择有效的样品")
    rows, groups = [], []
    for index in indices:
        sample, curve = panel._curves[index]
        automatic = panel._auto_fits.get(index, []) if panel.auto_check.isChecked() else []
        if automatic:
            numbers = sorted(panel._selected_peaks.get(index, set())) if selected_peaks else range(len(automatic))
            fits = [(n+1, automatic[n].result) for n in numbers]
            if not fits:
                raise ValueError(f"{sample} 尚未勾选峰")
        else:
            result = panel._results.get(index)
            fits = [(1, result)] if result is not None else []
        if not fits:
            rows.append({"样品": sample, "峰号": None, "状态": "未拟合或失败",
                         "说明": panel._fit_failures.get(index, "尚未拟合；导出不会重新选择拟合策略")})
        for number, fit in fits:
            x = np.asarray(fit.used_x)
            if curve.kind == "azimuthal_1d":
                x = x % 360
            order = np.argsort(x)
            groups.append((f"{sample} · 峰{number}", fit, x[order], order))
            components = list(fit.components) or [None]
            for component_index, component in enumerate(components, 1):
                row = {"样品": sample, "峰号": number if component is None else component_index,
                       "模型": fit.model, "背景模型": fit.background,
                       "R²": fit.metrics.get("r_squared"), "RMSE": fit.metrics.get("rmse"),
                       "AIC": fit.metrics.get("aic"), "BIC": fit.metrics.get("bic"),
                       "范围起点": fit.requested_range[0], "范围终点": fit.requested_range[1],
                       "横坐标单位": fit.x_unit, "强度单位": fit.y_unit,
                       "状态": "拟合完成", "说明": " | ".join(fit.warnings)}
                for key, param in fit.parameters.items():
                    if component is not None and key.startswith("peak_"):
                        if not key.startswith(component + "_"):
                            continue
                        key = key[len(component)+1:]
                    label = {"center": "峰位", "fwhm": "FWHM", "area": "峰面积",
                             "height": "峰高"}.get(key, key)
                    value = param.value
                    if key == "center" and curve.kind == "azimuthal_1d":
                        value %= 360
                    row[label] = value
                    row[label + " 标准误"] = param.stderr
                    row[label + " 单位"] = param.unit
                if curve.kind == "azimuthal_1d" and component is None:
                    try:
                        h = from_single_peak_fit(curve, fit, domain=panel.herman_domain_combo.currentData())
                        row.update({"Herman": h.f_fit, "Herman 状态": h.status,
                                    "Herman 范围": h.domain, "Herman 说明": h.message})
                    except ValueError as exc:
                        row.update({"Herman 状态": "未计算", "Herman 说明": str(exc)})
                if panel.crystal_export_check.isChecked():
                    try:
                        crystal = panel._crystallinity_for_index(index)
                        row.update({"结晶度（样品）": crystal.fraction,
                                    "结晶度方法": crystal.method_id,
                                    "晶峰面积（样品）": crystal.crystalline_area,
                                    "非晶峰面积（样品）": crystal.amorphous_area,
                                    "面积单位": crystal.area_unit,
                                    "峰归属": crystal.classifications.get(component),
                                    "分峰面积": crystal.component_areas.get(component)})
                    except ValueError as exc:
                        row["结晶度说明"] = str(exc)
                rows.append(row)
        for rank, reason in panel._auto_failures.get(index, []):
            rows.append({"样品": sample, "峰号": f"候选{rank}", "状态": "拟合失败", "说明": reason})

    book = Workbook()
    curves = book.active
    curves.title = "曲线数据"
    summary = book.create_sheet("参数汇总")
    info = book.create_sheet("处理说明")
    # One full source XY per sample, then independent fitted XY pairs. Different
    # peak grids and ranges stay independent; missing angles are never filled.
    column = 1
    group_cursor = 0
    for index in indices:
        sample, curve = panel._curves[index]
        source_x = np.asarray(curve.x)
        if curve.kind == "azimuthal_1d":
            source_x = source_x % 360
        source_order = np.argsort(source_x)
        values = [(sample, "原始横坐标", curve.x_unit, "X", source_x[source_order]),
                  (sample, "原始强度", curve.intensity_unit, "Y", curve.intensity[source_order])]
        automatic = panel._auto_fits.get(index, []) if panel.auto_check.isChecked() else []
        numbers = sorted(panel._selected_peaks.get(index, set())) if selected_peaks else range(len(automatic))
        group_count = len(list(numbers)) if automatic else int(index in panel._results)
        for label, fit, x, order in groups[group_cursor:group_cursor+group_count]:
            values.extend([(label, "拟合横坐标", fit.x_unit, "X", x),
                           (label, "拟合强度", fit.y_unit, "Y", fit.best_fit_y[order]),
                           (label, "残差（原始−拟合）", fit.y_unit, "Y", fit.residual_y[order])])
            # A joint multipeak result retains its sum and individual components.
            for name, array in fit.components.items():
                values.extend([(label+" · "+name, "分峰横坐标", fit.x_unit, "X", x),
                               (label+" · "+name, "分峰强度（不含背景）", fit.y_unit, "Y", array[order])])
        group_cursor += group_count
        for offset, (label, name, unit, designation, array) in enumerate(values):
            for row_number, value in enumerate((label, name, unit, designation), 1):
                curves.cell(row_number, column+offset, value)
            for row_number, value in enumerate(array, 5):
                if np.isfinite(value):
                    curves.cell(row_number, column+offset, float(value))
        column += len(values)
    first = ["样品", "峰号", "模型", "峰位", "峰位 标准误", "FWHM", "FWHM 标准误", "Herman", "R²", "RMSE", "状态"]
    headers = first + list(dict.fromkeys(key for row in rows for key in row if key not in first))
    summary.append(headers)
    for row in rows:
        summary.append([row.get(key) for key in headers])
    info.append(["适用样品", "参数", "值"])
    info.append(["全部", "工作表用途", "曲线数据用于画图；参数汇总每个样品每个峰一行，可直接统计；处理说明记录共享条件和个别差异。"])
    info.append(["全部", "曲线列说明", "每个样品：完整原始X/Y，然后各已拟合峰的X、拟合Y、残差Y（原始减拟合）；残差与前面的拟合Y共用拟合X。联合多峰另列各峰分量。前4行依次为名称、物理量、单位、Origin列类型。"])
    info.append(["全部", "数据范围", "原始积分曲线完整保留，拟合曲线只保留其实际拟合范围；各XY对独立，不按行号对齐，不补缺失角度。拟合强度含拟合背景，分峰分量不含背景。"])
    info.append(["全部", "标准误", "空白表示未估计，不表示零；峰面积单位含横坐标单位。"])
    info.append(["全部", "Herman", "逐峰扣除拟合背景后计算；模型完整域含外推。具体方法及失败原因见参数汇总；Herman误差尚未估计。"])
    info.append(["全部", "有效性", "导出保存最终拟合策略，不重新拟合；拟合收敛不代表模型已通过科学验证。"])
    contexts = panel._source_context_summary(indices)
    if panel.crystal_export_check.isChecked():
        info.append(["全部", "WAXS适用性确认", panel.crystal_scope.currentText()])
        for key, value in panel._flatten_context({"peak_models": panel._component_models,
                "center_bounds": panel._component_center_bounds, "fwhm_bounds": panel._component_fwhm_bounds,
                "crystalline_peak_ids": panel.crystal_peaks.text(), "amorphous_peak_ids": panel.amorphous_peaks.text()}):
            info.append(["全部", key, value])
    for key, value in panel._flatten_context(contexts["shared"]):
        info.append(["全部", key, value])
    for sample, context in contexts["per_curve_differences"].items():
        for key, value in panel._flatten_context(context):
            info.append([sample, key, value])
    for sheet in book:
        for cell in sheet[1]:
            cell.fill = PatternFill("solid", fgColor="1F6D7A")
            cell.font = Font(color="FFFFFF", bold=True)
        sheet.freeze_panes = "C2" if sheet is summary else ("A5" if sheet is curves else "A2")
        if sheet is not curves:
            sheet.auto_filter.ref = sheet.dimensions
        for column in range(1, sheet.max_column+1):
            sheet.column_dimensions[get_column_letter(column)].width = 22 if sheet is curves else 24
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with target.open("xb") as stream:
            created = True
            book.save(stream)
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    finally:
        book.close()
    return target
