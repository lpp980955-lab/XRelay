"""Ruland workbook export, independent of live Qt controls and recalculation."""
from pathlib import Path
from collections.abc import Mapping
import json
import numpy as np


def _flatten(value, prefix=""):
    if isinstance(value, Mapping):
        return {k: v for key, child in value.items()
                for k, v in _flatten(child, f"{prefix}.{key}" if prefix else str(key)).items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        value = json.dumps(value.tolist() if isinstance(value, np.ndarray) else value, ensure_ascii=False, default=str)
    if isinstance(value, np.generic):
        value = value.item()
    if value is not None and not isinstance(value, (str, int, float, bool)):
        value = str(value)
    return {prefix: value}


def export_ruland_workbook(path, entries, settings, *, sampling=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    path = Path(path).expanduser().resolve()
    protected = (Path(__file__).resolve().parents[2]/"data").resolve()
    if path == protected or protected in path.parents:
        raise ValueError("不能写入原始数据目录")
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Ruland结果使用.xlsx工作簿")
    entries = tuple(entries)
    if not entries:
        raise ValueError("没有可导出的结果")
    book = Workbook()
    curves = book.active
    curves.title = "曲线数据"
    linear = book.create_sheet("Ruland线性拟合")
    linear.append(["样品", "q (nm⁻¹)", "FWHM (°)", "B (°)", "峰宽定义", "参与回归",
                   "X 物理量", "X 单位", "散点 X", "Y 物理量", "Y 单位", "散点 Y", "拟合 Y"])
    parameters = book.create_sheet("参数汇总")
    info = book.create_sheet("处理说明")
    rows, contexts = [], []
    column = 1

    def xy(label, x, y, x_name, y_name, x_unit, y_unit):
        nonlocal column
        x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
        if column+1 > 16384 or len(x)+4 > 1048576:
            raise ValueError("本次曲线数据超出Excel单表容量，请分批导出或缩小q点数；尚未写入文件")
        order = np.argsort(x)
        for offset, (name, unit, designation, array) in enumerate((
            (x_name, x_unit, "X", x[order]), (y_name, y_unit, "Y", y[order]))):
            for row, value in enumerate((label, name, unit, designation), 1):
                curves.cell(row, column+offset, value)
            for row, value in enumerate(array, 5):
                if np.isfinite(value):
                    curves.cell(row, column+offset, float(value))
        column += 2

    for ordinal, (name, entry, error) in enumerate(entries, 1):
        label = name if sum(other[0] == name for other in entries) == 1 else f"{name} [{ordinal}]"
        series = getattr(entry, "series", None)
        if getattr(series, "q_unit", "nm^-1") != "nm^-1":
            raise ValueError("Ruland导出需要nm^-1的q坐标；请先明确换算单位")
        regression = getattr(series, "result", None)
        model = getattr(series, "model", settings.get("model", "gaussian"))
        definition = getattr(series, "width_definition", settings.get("width_definition", "FWHM"))
        center = getattr(entry, "peak_center_deg", None)
        center = settings.get("peak_center_deg", 90) if center is None else center
        center = 90 if center is None else center
        failed = error or getattr(entry, "error", None)
        status = "候选结果" if regression is not None else ("未处理" if str(failed).startswith("not_processed:") else "失败")
        source = getattr(entry, "sample_source", name)
        row = {"样品": label, "记录类型": "样品结果", "状态": status, "说明": failed,
               "来源": source, "峰型": model, "峰宽定义": definition, "方位峰参考中心 (°)": center,
               "方位拟合半范围 (°)": settings.get("half_window_deg")}
        if regression is not None:
            row.update({"微纤长度 (nm)": regression.length_nm,
                "微纤长度 标准误 (nm)": getattr(regression,"length_stderr_nm",None),
                "位错角 (°)": np.degrees(regression.misorientation_rad),
                "位错角 标准误 (°)": None if getattr(regression,"misorientation_stderr_rad",None) is None else np.degrees(regression.misorientation_stderr_rad),
                "斜率": regression.slope, "截距": regression.intercept,
                "斜率 标准误": getattr(regression,"slope_stderr",None),
                "截距 标准误": getattr(regression,"intercept_stderr",None), "线性拟合 R²": regression.r_squared})
        rows.append(row)
        by_q = {s.q: s for s in getattr(series, "slices", ())}
        x_name, y_name, x_unit, y_unit = (
            ("1/q²", "B²", "nm²", "rad²") if model == "gaussian"
            else ("1/q", "B", "nm", "rad"))
        rings = getattr(entry, "rings", ())
        chosen = getattr(entry, "selected", (True,)*len(rings))
        for i, ring in enumerate(rings):
            item = by_q.get(ring.center_q)
            peak = getattr(item, "fit", None)
            width = getattr(item, "width_deg", None)
            peak_error = ring.error or getattr(item, "error", None)
            qrow = {"样品": label, "记录类型": "q环带", "q (nm⁻¹)": ring.center_q,
                "参与回归": bool(chosen[i]), "q下限 (nm⁻¹)": ring.radial_range[0], "q上限 (nm⁻¹)": ring.radial_range[1],
                "峰型": model, "峰宽定义": definition, "B (°)": width,
                "状态": "失败" if peak_error else "候选结果" if chosen[i] else "未参与回归",
                "说明": peak_error}
            if peak is not None:
                qrow["峰拟合 R²"] = getattr(peak, "metrics", {}).get("r_squared")
                qrow["峰拟合 RMSE"] = getattr(peak, "metrics", {}).get("rmse")
                for key, p in getattr(peak, "parameters", {}).items():
                    value = (p.value+center)%360 if key == "center" else p.value
                    title = {"center":"峰位", "fwhm":"FWHM"}.get(key,key)
                    qrow[f"{title} ({p.unit})"] = value
                    qrow[f"{title} 标准误 ({p.unit})"] = p.stderr
            rows.append(qrow)
            label_q = f"{label} · q={ring.center_q:.7g} nm⁻¹"
            if ring.curve is not None:
                xy(label_q, np.asarray(ring.curve.x)%360, ring.curve.intensity,
                   "原始方位角", "原始强度", "deg", getattr(ring.curve,"intensity_unit","count"))
            if peak is not None:
                xy(label_q, (np.asarray(peak.used_x)+center)%360, peak.best_fit_y,
                   "拟合方位角", "拟合强度", "deg", getattr(peak,"y_unit",getattr(ring.curve,"intensity_unit","count")))
            if width is not None and ring.center_q > 0:
                tx = 1/ring.center_q**2 if model == "gaussian" else 1/ring.center_q
                ty = np.radians(width)**2 if model == "gaussian" else np.radians(width)
                qrow.update({"Ruland X": tx, "Ruland Y": ty})
                predicted = None
                if regression is not None and chosen[i]:
                    predicted = regression.slope * tx + regression.intercept
                    qrow["Ruland 拟合Y"] = predicted
                fwhm = getattr(peak, "parameters", {}).get("fwhm")
                linear.append([label, ring.center_q, None if fwhm is None else fwhm.value,
                               width, definition, bool(chosen[i]), x_name, x_unit, tx,
                               y_name, y_unit, ty, predicted])
        integration_audit = next((r.curve.audit for r in rings if r.curve is not None), {})
        contexts.append((label, _flatten({"几何":getattr(entry,"geometry_audit",{}),
            "背景":getattr(entry,"background_audit",{}), "MASK":getattr(entry,"mask_audit",{}),
            "积分":integration_audit, "源文件":source,
            "公式版本":getattr(regression,"formula_version","pauw_2010" if definition == "integral_breadth" else "liu_2025"),
            "方位峰中心来源":getattr(entry,"peak_center_source",None)})))
    first = ["样品","记录类型","q (nm⁻¹)","参与回归","峰型","峰宽定义","FWHM (deg)","峰拟合 R²",
             "微纤长度 (nm)","位错角 (°)","斜率","截距","线性拟合 R²","状态","说明"]
    headers = first+list(dict.fromkeys(key for row in rows for key in row if key not in first))
    parameters.append(headers)
    for row in rows:
        parameters.append([row.get(key) for key in headers])
    info.append(["适用样品","参数","值"])
    for key, value in {
        "表格说明":"曲线数据：每个q的完整原始方位XY及拟合方位XY。Ruland线性拟合：每行对应一个有效环带，含q、FWHM、散点和拟合值。参数汇总：筛选记录类型=样品结果比较最终参数，=q环带比较各q峰宽。",
        "曲线格式":"前4行为名称、物理量、单位、Origin列类型；各XY对独立，不按行号对齐，不补遮挡点。拟合强度含拟合背景。",
        "回归点选择":"原始Ruland散点保留有效未参与点；绘图前可按参数汇总的参与回归列筛选；线性拟合只用参与点。",
        "误差与失败":"空白表示不适用或未估计，不能当作0；失败和停止未处理样品保留。回归标准误不含峰宽及系统误差。",
        "来源与状态":"Liu 2025 SI第5页式5/6；积分宽度另用Pauw 2010。FWHM为用户确认的默认约定，结果尚待真实数据与独立软件验收。",
        **_flatten({"处理设置":settings,"采样说明":sampling or {}})}.items():
        info.append(["全部",key,value])
    shared = {}
    for key in dict.fromkeys(k for _,context in contexts for k in context):
        values = [context[key] for _,context in contexts if key in context]
        if all(value == values[0] for value in values):
            shared[key] = values[0]
    info.append(["全部","公共参数范围","公共参数合并有记录且数值相同的项；失败或未处理样品缺失的元数据仍是未知，不据此补写。"])
    for key,value in shared.items():
        info.append(["公共（有记录样品）",key,value])
    for label,context in contexts:
        for key,value in context.items():
            if key not in shared:
                info.append([label,key,value])
    for sheet in book:
        for cell in sheet[1]:
            cell.fill = PatternFill("solid",fgColor="1F6D7A")
            cell.font = Font(color="FFFFFF",bold=True)
        sheet.freeze_panes = "A5" if sheet is curves else "C2"
        if sheet is not curves:
            sheet.auto_filter.ref = sheet.dimensions
        for col in range(1,sheet.max_column+1):
            sheet.column_dimensions[get_column_letter(col)].width = 24
    path.parent.mkdir(parents=True,exist_ok=True)
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
