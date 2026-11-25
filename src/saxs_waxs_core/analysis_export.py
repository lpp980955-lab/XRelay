"""Use the common data workbook for existing scientific analysis windows."""
from saxs_waxs_core.workbook import write_workbook
import numpy as np


def export_analysis_workbook(panel,path,indices=None):
    groups,rows,contexts=[],[],[]
    if indices is None:
        indices=range(len(panel.curves)) if panel.batch_started else sorted(panel.results)
    for index in indices:
        name,curve=panel.curves[index]
        result=panel.results.get(index)
        groups.append((name,curve.x,curve.intensity,"原始横坐标","原始强度",curve.x_unit,curve.intensity_unit))
        row={"样品":name,"状态":"未处理" if result is None else "失败" if isinstance(result,str) else "候选结果"}
        contexts.append((name,{"integration":dict(curve.audit),"processing":
            panel.parameter_contexts[index] if hasattr(panel,"parameter_contexts") else {}}))
        if isinstance(result,str):
            row["说明"]=result
        elif result is not None:
            for key,value in vars(result).items():
                if value is None or isinstance(value,(str,int,float,bool,np.number)):
                    row[key]=value.item() if isinstance(value,np.generic) else value
                elif isinstance(value,tuple) and all(isinstance(v,(int,float)) for v in value):
                    for number,scalar in enumerate(value,1):
                        row[f"{key}_{number}"]=scalar
                elif isinstance(value,tuple) and all(isinstance(v,str) for v in value):
                    row[key]="; ".join(value)
            groups.append((name+" · 拟合",result.q,result.fitted_intensity,"拟合q","拟合强度",result.q_unit,curve.intensity_unit))
            if getattr(result,"method",None) in ("guinier","porod"):
                x=result.q**2 if result.method == "guinier" else np.log(result.q)
                xname="q²" if result.method == "guinier" else "ln(q)"
                xunit=f"({result.q_unit})²" if result.method == "guinier" else "1"
                groups.append((name+" · 变换散点",x,np.log(result.intensity),xname,"ln(I)",xunit,"1"))
                groups.append((name+" · 变换拟合",x,result.slope*x+result.intercept,xname,"ln(I)",xunit,"1"))
        rows.append(row)
    if not rows:
        raise ValueError("请先计算，再导出结果")
    return write_workbook(path,groups,rows,contexts,
        notes=("仅导出当前结果快照，不重新拟合；收敛与R²不构成科学有效性的证明。",))
