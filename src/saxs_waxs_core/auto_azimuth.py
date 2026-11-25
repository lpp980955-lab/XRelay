"""Automatic, auditable candidate selection for azimuthal single-peak fits."""
from dataclasses import dataclass, replace
import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks, peak_prominences, peak_widths
from .peak_fitting import PeakFitError, PeakFitResult, compare_peak_models


@dataclass(frozen=True)
class AutomaticAzimuthFit:
    result: PeakFitResult
    peak_angle: float
    selection_range: tuple[float, float]
    candidate_aic: tuple[tuple[str, float], ...]
    warnings: tuple[str, ...]


def _prepare_azimuth(curve, prominence_fraction):
    """Detect candidates once; the returned arrays retain the original intensities."""
    if curve.kind != "azimuthal_1d" or curve.x_unit not in ("deg", "degrees", "degree", "°"):
        raise PeakFitError("自动识峰需要以度为单位的方位角曲线")
    if not np.isfinite(prominence_fraction) or not 0 < prominence_fraction <= 1:
        raise PeakFitError("峰识别阈值必须在0和100%之间")
    x = np.asarray(curve.x, dtype=float)
    y = np.asarray(curve.intensity, dtype=float)
    count = np.asarray(curve.count)
    if x.ndim != 1 or x.shape != y.shape or x.shape != count.shape or x.size < 20:
        raise PeakFitError("方位角曲线数据不足")
    order = np.argsort(x)
    x, y, count = x[order], y[order], count[order]
    dx = np.diff(x)
    step = float(np.median(dx))
    if not np.isfinite(step) or step <= 0 or np.any(np.abs(dx - step) > step * 0.05):
        raise PeakFitError("自动识峰需要均匀方位角网格")
    valid = np.isfinite(y) & (count > 0)
    if np.count_nonzero(valid) < 20:
        raise PeakFitError("有效方位角点不足")
    periodic = abs((x[-1] - x[0] + step) - 360.0) <= step * 1.1
    mode = "wrap" if periodic else "nearest"
    weighted = gaussian_filter1d(np.where(valid, y, 0.0), 1.0, mode=mode)
    weights = gaussian_filter1d(valid.astype(float), 1.0, mode=mode)
    smooth = np.divide(weighted, weights, out=np.zeros_like(weighted), where=weights > 0)
    # Break detection at missing bins, not across an unobserved wedge.
    floor = float(np.min(smooth[valid]) - max(np.ptp(smooth[valid]), 1.0) * 2)
    smooth[~valid] = floor
    work = np.tile(smooth, 3) if periodic else smooth
    peaks, props = find_peaks(work, prominence=max(float(np.ptp(smooth[valid])) * prominence_fraction, np.finfo(float).eps))
    if periodic:
        keep = (peaks >= x.size) & (peaks < 2 * x.size)
        peaks, prominence = peaks[keep], props["prominences"][keep]
    else:
        prominence = props["prominences"]
    if not peaks.size:
        raise PeakFitError("未找到清晰主峰，请手动选择峰范围")
    ranking = np.argsort(prominence)[::-1]
    return x, y, valid, periodic, step, work, peaks, ranking


def _fit_prepared(curve, prepared, peak_rank, models=None, allow_masked=False):
    x, y, valid, periodic, step, work, peaks, ranking = prepared
    if peak_rank < 0 or peak_rank >= len(ranking):
        raise PeakFitError("候选峰序号超出范围")
    chosen = int(peaks[ranking[peak_rank]])
    widths = peak_widths(work, [chosen], rel_height=0.95)
    left, right = float(widths[2][0]), float(widths[3][0])
    peak_idx = chosen % x.size
    center = float(x[peak_idx])
    left_width = max((chosen - left) * step, 4 * step)
    right_width = max((right - chosen) * step, 4 * step)
    left_width, right_width = min(left_width, 90.0), min(right_width, 90.0)
    local = (x - center + 180.0) % 360.0 - 180.0 if periodic else x - center
    selected = (local >= -left_width) & (local <= right_width)
    nearby = (local >= -left_width - 2 * step) & (local <= right_width + 2 * step)
    if not allow_masked and np.any(nearby & ~valid):
        raise PeakFitError("建议峰范围包含缺失或遮挡点，请手动复核")
    if np.count_nonzero(selected & valid) < 9:
        raise PeakFitError("建议峰范围有效点不足9个，请手动复核")
    if np.min(local[selected]) >= -step or np.max(local[selected]) <= step:
        raise PeakFitError("主峰缺少一侧，无法自动分析")
    unwrapped = center + local
    lower, upper = center - left_width, center + right_width
    if lower < 0:
        unwrapped += 360.0
        lower += 360.0
        upper += 360.0
    intensity = np.where(valid, y, np.nan)
    comparison = compare_peak_models(unwrapped, intensity, **({"models": models} if models else {}), background="constant",
        x_range=(lower, upper), x_unit=str(curve.x_unit), y_unit=str(curve.intensity_unit))
    usable = [r for r in comparison.ranked_by_aic if r.metrics.get("success") and np.isfinite(r.metrics.get("aic", np.nan))]
    if not usable:
        raise PeakFitError("候选模型均未收敛，请手动拟合")
    best = usable[0]
    warnings = ["自动候选按峰突出度与AIC选择；需检查峰归属、范围和残差"]
    if np.any(nearby & ~valid):
        warnings.append("峰区含遮挡；仅拟合有效实测点，Herman完整域使用显式模型外推")
    warnings.extend(f"拟合警告：{warning}" for warning in best.warnings)
    if len(peaks) > 2:
        warnings.append("存在多个候选峰，当前选择突出度最大的峰")
    return AutomaticAzimuthFit(best, center, (lower % 360.0, upper % 360.0),
        tuple((r.model, float(r.metrics["aic"])) for r in usable), tuple(warnings))


def suggest_azimuth_peak_windows(curve):
    """Return each detected peak and its symmetric fitting half-window."""
    x, y, valid, periodic, step, work, peaks, ranking = _prepare_azimuth(curve, 0.03)
    prominence, left_base, right_base = peak_prominences(work, peaks)
    widths = peak_widths(work, peaks, prominence_data=(prominence, left_base, right_base))[0]
    adjacent = np.diff(y)[valid[:-1] & valid[1:]]
    noise = (1.4826 * np.median(np.abs(adjacent - np.median(adjacent))) / np.sqrt(2)
             if adjacent.size else 0.)
    min_half = min(90., max(8 * step, 8.))
    angles = x[peaks % x.size]
    accepted = []
    for index in ranking:
        if widths[index] < 2 or prominence[index] < 4 * noise:
            continue
        distances = np.abs(angles[accepted] - angles[index])
        if periodic:
            distances = np.minimum(distances, 360 - distances)
        if not np.any(distances < 2 * min_half):
            accepted.append(index)
    candidates = []
    for index in accepted:
        distances = np.abs(angles[[j for j in accepted if j != index]] - angles[index])
        if periodic:
            distances = np.minimum(distances, 360 - distances)
        limit = min(90., float(distances.min()) / 2) if distances.size else 90.
        half = min(max(min_half, 2 * step * widths[index]), limit)
        candidates.append((float(angles[index]), half))
    return tuple(candidates)


def fit_automatic_azimuth(curve, *, peak_rank: int = 0, count_only: bool = False, prominence_fraction: float = 0.03):
    """Suggest a peak using smoothed detection and unsmoothed fitting data."""
    prepared = _prepare_azimuth(curve, prominence_fraction)
    x, _y, _valid, _periodic, _step, _work, peaks, ranking = prepared
    if count_only:
        return tuple(float(x[int(peaks[index]) % x.size]) for index in ranking)
    return _fit_prepared(curve, prepared, peak_rank)


def fit_all_automatic_azimuth(curve, *, prominence_fraction: float = 0.03, shared_model: bool = False, allow_masked: bool = False):
    """Return all detected fits and per-candidate failures without dropping peaks."""
    prepared = _prepare_azimuth(curve, prominence_fraction)
    x, _y, _valid, _periodic, _step, _work, peaks, ranking = prepared
    angles = tuple(float(x[int(peaks[index]) % x.size]) for index in ranking)
    fits, failures = [], []
    for rank, angle in enumerate(angles):
        try:
            fits.append(_fit_prepared(curve, prepared, rank, allow_masked=allow_masked))
        except PeakFitError as exc:
            failures.append((rank + 1, f"{angle:.2f}°: {exc}"))
    if shared_model and fits:
        common = set(model for model, _ in fits[0].candidate_aic)
        for item in fits[1:]:
            common.intersection_update(model for model, _ in item.candidate_aic)
        if not common:
            raise PeakFitError("各峰没有共同收敛模型，不能进行同模型比较")
        chosen = min(sorted(common), key=lambda model: sum(dict(item.candidate_aic)[model] for item in fits))
        comparable = []
        for item in fits:
            rank = next(i for i, angle in enumerate(angles) if angle == item.peak_angle)
            common_fit = _fit_prepared(curve, prepared, rank, models=(chosen,), allow_masked=allow_masked)
            comparable.append(replace(common_fit, candidate_aic=item.candidate_aic,
                warnings=common_fit.warnings + (f"各峰共用模型：{chosen}；按各峰AIC之和选择，物理适用性仍需核查",)))
        fits = sorted(comparable, key=lambda item: item.peak_angle)
    return fits, failures
