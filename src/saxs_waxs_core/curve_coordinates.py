"""Common radial coordinate conversion for the GUI and Agent."""
import numpy as np


def radial_q_intensity(curve,q_unit):
    if curve.kind != "radial_1d":
        raise ValueError("需要径向积分曲线")
    if q_unit not in ("nm^-1","A^-1"):
        raise ValueError("不支持的 q 单位")
    q=np.asarray(curve.x,dtype=float)
    if curve.x_unit == "q_nm^-1":
        q_nm=q
    elif curve.x_unit == "q_A^-1":
        q_nm=q*10
    elif curve.x_unit == "s_nm^-1":
        q_nm=q*(2*np.pi)
    elif curve.x_unit == "2th_deg":
        wavelength=curve.audit.get("geometry_summary",{}).get("wavelength_nm")
        if wavelength is None or not np.isfinite(wavelength) or wavelength<=0:
            raise ValueError("2θ 转换需要有效波长")
        if np.any(~np.isfinite(q)) or np.any(q<0) or np.any(q>=180):
            raise ValueError("2θ 需要在 0° 到 180° 之间")
        q_nm=4*np.pi*np.sin(np.radians(q/2))/wavelength
    else:
        raise ValueError("不支持的径向单位")
    intensity=np.asarray(curve.intensity,dtype=float).copy()
    intensity[np.asarray(curve.count)<=0]=np.nan
    return (q_nm if q_unit == "nm^-1" else q_nm/10),intensity
