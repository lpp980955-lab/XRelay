"""Calibrant-free radial-edge centre suggestion at fixed geometry."""
from dataclasses import replace
import numpy as np
from scipy import ndimage
from .geometry import GeometryProvenance
from .calibration import CalibrationError


def suggest_center_only(image, geometry, mask=None):
    if not geometry.is_untilted:
        raise CalibrationError("只定中心目前要求零倾角；请使用适合倾斜几何的标定。")
    data=np.asarray(image)
    if data.ndim!=2 or data.shape!=(geometry.image_height_px,geometry.image_width_px):
        raise CalibrationError("图像尺寸与几何不一致")
    valid=np.isfinite(data)
    if mask is not None:
        valid &= ~np.asarray(mask,dtype=bool)
    if np.count_nonzero(valid)<100:
        raise CalibrationError("有效信号不足，请使用三点定环")
    # Weighted block means avoid sampling only detector gaps or a sparse lattice.
    step=max(1,int(np.ceil(max(data.shape)/512)))
    height=(data.shape[0]+step-1)//step
    width=(data.shape[1]+step-1)//step
    pad=((0,height*step-data.shape[0]),(0,width*step-data.shape[1]))
    weights=np.pad(valid.astype(float),pad).reshape(height,step,width,step).sum(axis=(1,3))
    values=np.pad(np.where(valid,data,0).astype(float),pad).reshape(height,step,width,step).sum(axis=(1,3))
    usable=weights>step*step*.5
    if np.count_nonzero(usable)<100:
        raise CalibrationError("有效探测器覆盖不足，请使用三点定环")
    values=np.divide(values,weights,out=np.zeros_like(values),where=weights>0)
    floor=np.percentile(values[usable],10)
    features=np.log1p(np.maximum(values-floor,0))
    features[~usable]=np.median(features[usable])
    features=ndimage.gaussian_filter(features,1)
    features=np.maximum(features-ndimage.gaussian_filter(features,6),0)
    usable &= ~ndimage.binary_dilation(~usable,iterations=2)
    features[~usable]=0
    if features.max()<=1e-10:
        raise CalibrationError("没有可识别的对称环信号，请使用三点定环")
    # Ring-edge normals meet at the centre without knowing the ring's d-spacing.
    gy,gx=np.gradient(ndimage.gaussian_filter(features,1))
    ratio=geometry.pixel_size_y_m/geometry.pixel_size_x_m
    magnitude=np.hypot(gx,gy/ratio)
    magnitude[usable==0]=0
    selected=np.flatnonzero(magnitude.ravel()>np.percentile(magnitude[usable>0],95))
    if selected.size<30:
        raise CalibrationError("可用圆环边缘不足，请使用三点定环")
    if selected.size>7500:
        selected=selected[np.argsort(magnitude.ravel()[selected])[-7500:]]
    rows,cols=np.unravel_index(selected,features.shape)
    strength=magnitude[rows,cols]
    dx=gx[rows,cols]/strength
    dy=(gy[rows,cols]/ratio)/strength/ratio
    votes=np.zeros(features.shape)
    for radius in np.arange(6,max(features.shape)*.7,2):
        for sign in (-1,1):
            vx=np.rint(cols+sign*radius*dx).astype(int)
            vy=np.rint(rows+sign*radius*dy).astype(int)
            inside=(vx>=0)&(vx<width)&(vy>=0)&(vy<height)
            np.add.at(votes,(vy[inside],vx[inside]),strength[inside])
    votes=ndimage.gaussian_filter(votes,1.5)
    ry,rx=np.unravel_index(np.argmax(votes),votes.shape)
    prominence=float(votes[ry,rx]/max(float(np.percentile(votes,95)),1e-15))
    if prominence<2.5:
        raise CalibrationError("圆环中心不明确，未修改参数；请使用三点定环")
    radial_x=(rx+.5)*step
    radial_y=(ry+.5)*step
    # Radial votes are the reported coarse suggestion. No distance or wavelength
    # is inferred from the unknown ring radius.
    center_x,center_y=radial_x,radial_y
    return replace(geometry,beam_center_x_px=center_x,beam_center_y_px=center_y,
        provenance=GeometryProvenance(method="radial_edge_center_only_candidate",
            source_application="XRelay",
            source_convention="top_left_edge_xy_right_down_pixel_center_plus_0p5",
            notes="candidate_unvalidated; zero_tilt; supplied_distance_unchanged"),
        metadata={**dict(geometry.metadata),"preview_calibrant":"none","calibrant_id":None,
            "status":"candidate_unvalidated","center_vote_prominence":prominence,
            "center_search_block_size_px":step})
