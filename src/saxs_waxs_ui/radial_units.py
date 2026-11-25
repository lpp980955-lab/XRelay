"""Scalar boundary conversions; keep detector ROI fixed when units change."""
import numpy as np


def convert_radial_values(values,source,target,wavelength_m):
    values=np.asarray(values,dtype=float)
    wavelength_nm=wavelength_m*1e9
    if source=="2th_deg":
        q=4*np.pi/wavelength_nm*np.sin(np.deg2rad(values)/2)
    elif source=="q_A^-1":
        q=values*10
    elif source=="s_nm^-1":
        q=values*2*np.pi
    elif source=="q_nm^-1":
        q=values
    else:
        raise ValueError(f"Unsupported radial unit: {source}")
    if target=="2th_deg":
        ratio=q*wavelength_nm/(4*np.pi)
        if np.any(np.abs(ratio)>1):
            raise ValueError("径向范围超出当前波长的物理范围")
        return np.rad2deg(2*np.arcsin(ratio))
    if target=="q_A^-1":
        return q/10
    if target=="s_nm^-1":
        return q/(2*np.pi)
    if target=="q_nm^-1":
        return q
    raise ValueError(f"Unsupported radial unit: {target}")
