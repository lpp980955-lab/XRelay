"""Read explicitly mapped external one-dimensional curves without inventing metadata."""
from __future__ import annotations

import csv
from pathlib import Path
import numpy as np

from .integration import IntegrationCurve


def suggested_curve_name(curve, fallback):
    """Use matching explicit XY header names; otherwise retain the caller's label."""
    audit = curve.audit
    headers = audit.get("header_lines", [])
    if not headers:
        return fallback
    delimiter = audit.get("delimiter")
    fields = headers[0].split() if delimiter == "whitespace" else next(csv.reader([headers[0]], delimiter=delimiter))
    x, y = audit.get("x_column_1based", 0) - 1, audit.get("y_column_1based", 0) - 1
    if min(x, y) < 0 or max(x, y) >= len(fields):
        return fallback
    left, right = fields[x].strip(), fields[y].strip()
    if " | " not in left or " | " not in right:
        return fallback
    sample_x, axis_x = left.rsplit(" | ", 1)
    sample_y, axis_y = right.rsplit(" | ", 1)
    if sample_x and sample_x == sample_y and axis_x.endswith(" (X)") and axis_y.endswith(" (Y)"):
        return sample_x
    return fallback


def read_curve_text(path, encoding="utf-8-sig"):
    if encoding not in ("utf-8-sig", "gb18030"):
        raise ValueError("不支持所选文件编码")
    try:
        return Path(path).read_text(encoding=encoding)
    except UnicodeDecodeError as exc:
        raise ValueError(f"无法按 {encoding} 解码文件") from exc


def prepare_curve_text(text):
    comments = [line for line in text.splitlines() if line.lstrip().startswith("#")]
    lines = [(i, line) for i, line in enumerate(text.splitlines(), 1)
             if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise ValueError("文件没有数据")
    try:
        delimiter = csv.Sniffer().sniff("\n".join(line for _, line in lines[:10]), delimiters=",\t;").delimiter
    except csv.Error:
        delimiter = None
    return delimiter, [(number, line, next(csv.reader([line], delimiter=delimiter)) if delimiter else line.split())
                       for number, line in lines], comments


def import_curve(path, *, x_column, y_column, kind, x_unit, intensity_unit, encoding="utf-8-sig", header_rows=0, wavelength_nm=None, source_text=None, prepared=None, sigma_column=None, zero_is_missing=False):
    path = Path(path)
    allowed = {"radial_1d": {"q_nm^-1", "q_A^-1", "s_nm^-1", "2th_deg"},
               "azimuthal_1d": {"deg"}}
    if kind not in allowed or x_unit not in allowed[kind]:
        raise ValueError("请选择与曲线类型匹配的坐标单位")
    if not isinstance(x_column, int) or not isinstance(y_column, int) or min(x_column, y_column) < 0 or x_column == y_column:
        raise ValueError("横纵坐标列必须是不同的非负列号")
    if sigma_column is not None and (not isinstance(sigma_column, int) or sigma_column < 0 or sigma_column in (x_column, y_column)):
        raise ValueError("误差列必须与横纵坐标列不同")
    if not intensity_unit or not intensity_unit.strip():
        raise ValueError("请明确强度单位；未知时可填写 a.u.")
    if wavelength_nm is not None:
        if x_unit != "2th_deg" or not np.isfinite(wavelength_nm) or wavelength_nm <= 0:
            raise ValueError("波长必须为正数，且仅供2θ转换使用")
    if path.suffix.lower() not in (".txt", ".csv", ".dat", ".tsv"):
        raise ValueError("支持 TXT、CSV、TSV 或 DAT")
    if encoding not in ("utf-8-sig", "gb18030"):
        raise ValueError("不支持所选文件编码")
    if not isinstance(header_rows, int) or isinstance(header_rows, bool) or header_rows < 0:
        raise ValueError("表头行数须为非负整数")
    if prepared is None:
        text = read_curve_text(path, encoding) if source_text is None else source_text
        prepared = prepare_curve_text(text)
    delimiter, lines, comments = prepared
    if len(lines) <= header_rows:
        raise ValueError("表头行数超出文件内容")
    rows, header = [], [line for _, line, _ in lines[:header_rows]]
    for number, line, fields in lines[header_rows:]:
        if len(fields) <= max(x_column, y_column, sigma_column if sigma_column is not None else 0):
            raise ValueError(f"第 {number} 行缺少所选列")
        if not fields[x_column].strip() and not fields[y_column].strip():
            continue
        try:
            x = float(fields[x_column].strip())
            y_text = fields[y_column].strip()
            y = float(y_text) if y_text else float("nan")
        except ValueError as exc:
            if not rows:
                try:
                    float(fields[x_column].strip())
                except ValueError:
                    try:
                        float(fields[y_column].strip())
                    except ValueError:
                        header.append(line)
                        continue
            raise ValueError(f"第 {number} 行所选列不是数字") from exc
        if not np.isfinite(x):
            raise ValueError(f"第 {number} 行横坐标不是有限数")
        sigma = float("nan")
        if sigma_column is not None and fields[sigma_column].strip():
            try:
                sigma = float(fields[sigma_column].strip())
            except ValueError as exc:
                raise ValueError(f"第 {number} 行误差列不是数字") from exc
            if np.isfinite(sigma) and sigma < 0:
                raise ValueError(f"第 {number} 行误差不能为负")
            if not np.isfinite(sigma):
                sigma = float("nan")
        rows.append((x, y, sigma))
    if len(rows) < 2:
        raise ValueError("曲线至少需要两个数据点")
    data = np.asarray(rows, dtype=float)
    valid = np.isfinite(data[:, 1])
    if zero_is_missing:
        valid &= data[:, 1] != 0
    if np.count_nonzero(valid) < 2:
        raise ValueError("曲线至少需要两个有限强度点")
    audit = {"source_type": "external_1d",
        "source_path": str(path.resolve()), "x_column_1based": x_column + 1,
        "y_column_1based": y_column + 1, "delimiter": delimiter or "whitespace",
        "header_lines": header, "source_comments": comments, "encoding": encoding,
        "sigma_column_1based": None if sigma_column is None else sigma_column + 1,
        "explicit_header_rows": header_rows, "zero_intensity_excluded": bool(zero_is_missing)}
    if wavelength_nm is not None:
        audit["geometry_summary"] = {"wavelength_nm": float(wavelength_nm)}
    return IntegrationCurve(kind=kind, x=data[:, 0], intensity=data[:, 1],
        sigma=data[:, 2], count=valid.astype(float), x_unit=x_unit,
        intensity_unit=intensity_unit.strip(), audit=audit)
