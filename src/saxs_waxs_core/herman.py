"""Numerical Herman orientation moment on an explicitly observed single peak.

Angles are local offsets in degrees from the selected peak centre.  This module
does not infer a fibre axis or replace missing observations with fitted values.
"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class HermanResult:
    status: str
    f_fit: float | None
    f_points: float | None
    coverage_deg: tuple[float, float]
    negative_points: int
    message: str
    domain: str = "full_90"


@dataclass(frozen=True)
class HermanPeakComparison:
    first: HermanResult
    second: HermanResult
    fit_difference: float | None
    points_difference: float | None
    mean_fit: float | None
    mean_points: float | None


def compare_peaks(first: HermanResult, second: HermanResult, *, average: bool = False) -> HermanPeakComparison:
    """Compare separately calculated peaks; average only on explicit request."""
    if first.domain != second.domain:
        raise ValueError("peak domains must use the same calculation mode")
    fit_difference = (first.f_fit - second.f_fit
                      if first.f_fit is not None and second.f_fit is not None else None)
    points_difference = (first.f_points - second.f_points
                         if first.f_points is not None and second.f_points is not None else None)
    return HermanPeakComparison(first, second, fit_difference, points_difference,
        (first.f_fit + second.f_fit) / 2 if average and fit_difference is not None else None,
        (first.f_points + second.f_points) / 2 if average and points_difference is not None else None)


def calculate_herman(
    angle_deg: np.ndarray,
    observed: np.ndarray,
    fitted_peak: np.ndarray,
    background: np.ndarray,
    *,
    center_deg: float,
    max_gap_deg: float,
    domain: str = "full_90",
) -> HermanResult:
    """Calculate both moments from observed support without filling masked gaps.

    ``full_90`` requires both sides to 90 degrees within one bin width.
    ``selected`` explicitly computes the observed, truncated peak domain and
    labels it as such; the caller must review the peak boundaries. The declared
    bin width defines the maximum gap, including missing or masked bins.
    """
    x = np.asarray(angle_deg, dtype=float).reshape(-1)
    raw = np.asarray(observed, dtype=float).reshape(-1)
    fit = np.asarray(fitted_peak, dtype=float).reshape(-1)
    bg = np.asarray(background, dtype=float).reshape(-1)
    if not (x.shape == raw.shape == fit.shape == bg.shape) or x.size < 3:
        raise ValueError("angle, intensity, peak and background must have equal lengths")
    if not np.isfinite(center_deg) or not np.isfinite(max_gap_deg) or max_gap_deg <= 0:
        raise ValueError("finite centre and positive bin gap are required")
    if domain not in ("full_90", "selected"):
        raise ValueError("domain must be full_90 or selected")
    # Periodic difference permits a peak crossing 0 degrees.
    local = (x - center_deg + 180.0) % 360.0 - 180.0
    order = np.argsort(local)
    local, raw, fit, bg = (a[order] for a in (local, raw, fit, bg))
    if domain == "selected" and np.any(np.isfinite(local) & (np.abs(local) > 90.0 + 1e-9)):
        finite_local = local[np.isfinite(local)]
        coverage = (float(finite_local.min()), float(finite_local.max()))
        negative = int(np.count_nonzero(np.isfinite(raw) & np.isfinite(bg) & ((raw - bg) < 0)))
        return HermanResult("outside_domain", None, None, coverage, negative,
                            "Selected peak domain exceeds 90 degrees from the peak center; narrow the range explicitly.", domain)
    selected = np.abs(local) <= 90.0 + 1e-9
    local, raw, fit, bg = (a[selected] for a in (local, raw, fit, bg))
    finite = np.isfinite(local) & np.isfinite(raw) & np.isfinite(fit) & np.isfinite(bg)
    finite_angles = local[finite]
    coverage = (float(finite_angles.min()), float(finite_angles.max())) if finite_angles.size else (0.0, 0.0)
    points = raw - bg
    negative = int(np.count_nonzero(points[finite] < 0))
    if finite_angles.size > 1 and np.any(np.diff(finite_angles) <= 1e-10):
        return HermanResult("duplicate_angles", None, None, coverage, negative,
                            "Duplicate local angles cannot be integrated without an explicit merge rule.", domain)
    if (finite_angles.size < 3 or not finite.all() or
        finite_angles[0] >= 0 or finite_angles[-1] <= 0 or
        (domain == "full_90" and (finite_angles[0] > -90.0 + max_gap_deg or
                                  finite_angles[-1] < 90.0 - max_gap_deg)) or
        np.any(np.diff(finite_angles) > max_gap_deg * (1.0 + 1e-8))):
        return HermanResult("insufficient_domain", None, None, coverage, negative,
                            "Measured peak domain is incomplete or contains a gap.", domain)
    if np.any(fit < 0):
        return HermanResult("negative_fit", None, None, coverage, negative,
                            "Fitted peak contains negative intensity.", domain)

    theta = np.deg2rad(np.abs(local))
    weight = np.sin(theta)
    # The angular weight is exactly zero at the peak centre. If it falls
    # between measured bins, add the known zero of the weighted integrand;
    # this does not estimate or fill a missing intensity sample.
    crosses_center = np.any(local < 0) and np.any(local > 0) and not np.any(local == 0)
    center_index = int(np.searchsorted(local, 0.0))
    def integrate_weighted(values: np.ndarray) -> float:
        if crosses_center:
            return float(np.trapezoid(np.insert(values, center_index, 0.0),
                                      np.insert(local, center_index, 0.0)))
        return float(np.trapezoid(values, local))
    def moment(intensity: np.ndarray) -> float | None:
        denominator = integrate_weighted(intensity * weight)
        if not np.isfinite(denominator) or denominator <= 0:
            return None
        numerator = integrate_weighted(intensity * weight * np.cos(theta) ** 2)
        return (3.0 * numerator / denominator - 1.0) / 2.0

    f_fit = moment(fit)
    if f_fit is None:
        return HermanResult("invalid_integral", None, None, coverage, negative,
                            "The fitted peak weighted intensity integral must be positive.", domain)
    if negative:
        return HermanResult("negative_intensity", f_fit, None, coverage, negative,
                            "Fitted result available; background-subtracted points contain negative intensity and require review.", domain)
    f_points = moment(points)
    if f_points is None:
        return HermanResult("invalid_points_integral", f_fit, None, coverage, negative,
                            "Fitted result available; observed weighted intensity integral is invalid.", domain)
    message = ("Selected-domain moment, not the full 0–90° moment; review both peak boundaries."
               if domain == "selected" else
               "Single-peak moment; reference and physical interpretation require review.")
    return HermanResult("ok", f_fit, f_points, coverage, negative, message, domain)


def from_single_peak_fit(curve, result, *, domain: str = "selected") -> HermanResult:
    """Use a single-peak fit while preserving unavailable source bins.

    The fit range is the explicitly selected observed domain.  Missing points
    are never reconstructed from the fit or joined across by interpolation.
    """
    if getattr(curve, "kind", None) != "azimuthal_1d" or result.x_unit not in ("deg", "degree", "degrees", "°"):
        raise ValueError("Herman calculation requires an azimuthal curve in degrees")
    if domain == "model_full_90":
        from .peak_fitting import _evaluate_peak
        if not result.metrics.get("success", False) or "center" not in result.parameters or result.components:
            raise ValueError("model-domain Herman requires a converged single-peak fit")
        center = result.parameters["center"].value
        local = np.linspace(-90., 90., 18001)
        values = {key: parameter.value for key, parameter in result.parameters.items()}
        peak = _evaluate_peak(result.model, center + local, values)
        moment = calculate_herman(center + local, peak, peak, np.zeros_like(peak),
                                  center_deg=center, max_gap_deg=.011, domain="full_90")
        if moment.f_fit is None:
            return HermanResult(moment.status, None, None, (-90., 90.), 0, moment.message, domain)
        used = np.asarray(result.used_x)
        offsets = np.sort(used - center)
        spacing = np.median(np.diff(np.sort(used)))
        # Count model support outside fitted observations without inventing measurements.
        positions = np.searchsorted(offsets, local)
        distance = np.minimum(np.abs(local - offsets[np.clip(positions, 0, len(offsets)-1)]),
                              np.abs(local - offsets[np.clip(positions-1, 0, len(offsets)-1)]))
        outside = distance > spacing * .51
        weighted = peak * np.sin(np.radians(np.abs(local)))
        fraction = np.trapezoid(np.where(outside, weighted, 0), local) / np.trapezoid(weighted, local)
        observed = from_single_peak_fit(curve, result, domain="full_90")
        message = (f"模型完整域 Herman；扣除拟合背景，按峰中心±90°积分；"
                   f"拟合实测域 {offsets.min():.2f}° 至 {offsets.max():.2f}°；"
                   f"拟合实测支撑外的加权面积占比 {fraction:.2%}。"
                   "缺失角度使用模型外推，原始数据未补齐；结果依赖峰形和尾部假设。")
        return HermanResult("model_based", moment.f_fit, observed.f_points,
                            (-90., 90.), observed.negative_points, message, domain)
    if not result.metrics.get("success", False):
        raise ValueError("peak fit did not converge")
    if "center" not in result.parameters or result.components:
        raise ValueError("select a single-peak fit for Herman calculation")
    x = np.asarray(curve.x, dtype=float)
    raw = np.asarray(curve.intensity, dtype=float).copy()
    counts = np.asarray(curve.count)
    if x.ndim != 1 or x.shape != raw.shape or x.shape != counts.shape:
        raise ValueError("invalid integration curve")
    finite_x = np.sort(np.unique(x[np.isfinite(x)]))
    spacing = np.diff(finite_x)
    spacing = spacing[spacing > 0]
    if not spacing.size:
        raise ValueError("azimuthal bin spacing is unavailable")
    # A missing bin must remain a gap; use nominal spacing, never the largest gap.
    bin_width = float(np.median(spacing))
    lower, upper = result.requested_range
    if upper > 360.0:
        x = np.where(x < lower, x + 360.0, x)
    chosen = np.isfinite(x) & (x >= lower) & (x <= upper)
    x, raw, counts = x[chosen], raw[chosen], counts[chosen]
    raw[counts <= 0] = np.nan
    background = np.zeros_like(x)
    if result.background != "none":
        background += result.parameters["background_intercept"].value
    if result.background == "linear":
        background += result.parameters["background_slope"].value * (x - np.mean(result.used_x))
    fitted = np.full_like(x, np.nan)
    positions = np.searchsorted(result.used_x, x)
    valid = positions < result.used_x.size
    indices = np.flatnonzero(valid)
    exact = np.isclose(result.used_x[positions[indices]], x[indices], rtol=0, atol=bin_width * 1e-7)
    indices = indices[exact]
    fitted[indices] = result.best_fit_y[positions[indices]] - background[indices]
    return calculate_herman(x, raw, fitted, background,
                            center_deg=result.parameters["center"].value,
                            max_gap_deg=bin_width * 1.01, domain=domain)
