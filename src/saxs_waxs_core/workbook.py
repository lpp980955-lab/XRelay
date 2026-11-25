"""Shared three-sheet data contract for desktop and Agent workflows."""
from pathlib import Path
from collections.abc import Mapping
import json
import numpy as np

SHEETS = ("曲线数据", "参数汇总", "处理说明")


def flatten_context(value, prefix=""):
    if isinstance(value, Mapping):
        return {k:v for key, child in value.items() for k,v in flatten_context(child,
                f"{prefix}.{key}" if prefix else str(key)).items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        value = json.dumps(value.tolist() if isinstance(value,np.ndarray) else value,ensure_ascii=False,default=str)
    if isinstance(value,np.generic):
        value = value.item()
    if value is not None and not isinstance(value,(str,int,float,bool)):
        value = str(value)
    return {prefix:value}


def write_workbook(path, groups, records, contexts=(), *, notes=()):
    """groups: (label,x,y,x_name,y_name,x_unit,y_unit); contexts: (sample,dict)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    path=Path(path).expanduser().resolve()
    protected=(Path(__file__).resolve().parents[2]/"data").resolve()
    if path == protected or protected in path.parents:
        raise ValueError("不能写入原始数据目录")
    if path.suffix.lower() != ".xlsx":
        raise ValueError("请使用.xlsx工作簿")
    groups,records=list(groups),list(records)
    if len(groups)*2 > 16384:
        raise ValueError("曲线超过Excel列数限制，请分批导出")
    book=Workbook()
    curves=book.active
    curves.title=SHEETS[0]
    summary=book.create_sheet(SHEETS[1])
    info=book.create_sheet(SHEETS[2])
    for n,(label,x,y,xname,yname,xunit,yunit) in enumerate(groups):
        x,y=np.asarray(x,dtype=float),np.asarray(y,dtype=float)
        if x.shape != y.shape or x.ndim != 1 or len(x)+4 > 1048576:
            raise ValueError("曲线坐标不匹配或超过工作表容量")
        for offset,(name,unit,designation,array) in enumerate(((xname,xunit,"X",x),(yname,yunit,"Y",y))):
            for row,value in enumerate((label,name,unit,designation),1):
                curves.cell(row,n*2+offset+1,value).data_type="s"
            for row,value in enumerate(array,5):
                if np.isfinite(value):
                    curves.cell(row,n*2+offset+1,float(value))
    keys=list(dict.fromkeys(key for row in records for key in row)) or ["状态"]
    summary.append(keys)
    for row in records:
        summary.append([row.get(key) for key in keys])
    info.append(["适用样品","参数","值"])
    info.append(["全部","表格说明","每组曲线为独立XY对，前4行是名称、物理量、单位、Origin列类型。原始曲线与拟合曲线分开保存，不补缺失点。"])
    info.append(["全部","空值与失败","空值表示未知、不适用或未估计，不代表零。失败/停止未处理样品保留状态。"])
    for note in notes:
        info.append(["全部","说明",note])
    contexts=[(str(name),flatten_context(context)) for name,context in contexts]
    shared={}
    for key in dict.fromkeys(k for _,context in contexts for k in context):
        values=[context[key] for _,context in contexts if key in context]
        if all(value == values[0] for value in values):
            shared[key]=values[0]
    for key,value in shared.items():
        info.append(["公共（有记录样品）",key,value])
    for name,context in contexts:
        for key,value in context.items():
            if key not in shared:
                info.append([name,key,value])
    for sheet in book:
        for cell in sheet[1]:
            cell.fill=PatternFill("solid",fgColor="1F6D7A")
            cell.font=Font(color="FFFFFF",bold=True)
        sheet.freeze_panes="A5" if sheet is curves else "C2"
        if sheet is not curves:
            sheet.auto_filter.ref=sheet.dimensions
        for col in range(1,sheet.max_column+1):
            sheet.column_dimensions[get_column_letter(col)].width=24
    path.parent.mkdir(parents=True,exist_ok=True)
    created=False
    try:
        with path.open("xb") as stream:
            created=True
            book.save(stream)
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    finally:
        book.close()
    return path
