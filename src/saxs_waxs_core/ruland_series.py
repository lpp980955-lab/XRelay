"""Link explicit q slices to consistently fitted azimuthal peak widths."""
from dataclasses import dataclass, replace
import numpy as np
from .peak_fitting import PeakFitError, fit_peak
from .ruland import fit_ruland


@dataclass(frozen=True)
class RulandSlice:
    q: float
    name: str
    fit: object | None
    width_deg: float | None
    error: str | None


@dataclass(frozen=True)
class RulandSeries:
    slices: tuple[RulandSlice, ...]
    result: object | None
    error: str | None
    width_definition: str
    q_unit: str
    model: str


def measured_integral_breadth(curve, center_deg, baseline):
    """Pauw eq. 3 using complete 180° bins; no missing-data completion."""
    x = np.asarray(curve.x, dtype=float)
    y = np.asarray(curve.intensity, dtype=float)
    count = np.asarray(curve.count)
    order = np.argsort(x)
    x, y, count = x[order], y[order], count[order]
    if len(x) < 4 or not np.isfinite(x).all():
        raise ValueError("invalid azimuth coordinates")
    step = float(np.median(np.diff(x)))
    if step <= 0 or np.any(np.abs(np.diff(x) - step) > step * .05):
        raise ValueError("integral breadth requires a uniform azimuth grid")
    local = (x - center_deg + 180) % 360 - 180
    indices = np.flatnonzero(np.abs(local) < 90)
    # Bins cover ±90° when their total widths cover 180° within one bin.
    if not len(indices) or abs(len(indices) * step - 180) > step * 1.01:
        raise ValueError("integral breadth requires a complete 180° peak domain")
    values = y[indices] - baseline
    if np.any(count[indices] <= 0) or not np.isfinite(values).all():
        raise ValueError("integral breadth domain contains masked or missing data")
    if np.any(values < 0):
        raise ValueError("negative background-subtracted intensity in integral breadth domain")
    height = float(values.max())
    if height <= 0:
        raise ValueError("integral breadth peak intensity must be positive")
    return float(np.sum(values) * np.radians(step) / height)


def fit_ruland_series(slices, *, peak_center_deg, half_window_deg, model, width_definition,
                      q_unit="nm^-1", selected=None):
    """Fit each (name, q, azimuthal curve) with the same model and angle window.

    Explicitly selected failed slices prevent the final fit; no slice is removed
    automatically. The chosen width is passed as B, with its definition retained.
    """
    if model not in ("cauchy", "gaussian"):
        raise ValueError("model must be cauchy or gaussian")
    if width_definition not in ("FWHM", "HWHM", "integral_breadth"):
        raise ValueError("choose a width definition explicitly")
    if not np.isfinite(peak_center_deg) or not np.isfinite(half_window_deg) or not 0 < half_window_deg < 90:
        raise ValueError("invalid peak center or half-window")
    slices = tuple(slices)
    chosen = np.ones(len(slices), dtype=bool) if selected is None else np.asarray(selected)
    if chosen.shape != (len(slices),) or chosen.dtype.kind != "b":
        raise ValueError("selected must be a boolean per slice")
    peak_model = "lorentzian_area" if model == "cauchy" else "gaussian_height"
    output = []
    for name, q, curve in slices:
        try:
            if curve.kind != "azimuthal_1d" or curve.x_unit not in ("deg", "degrees", "degree", "°"):
                raise ValueError("azimuthal curve with degree unit required")
            x = np.asarray(curve.x, dtype=float)
            y = np.asarray(curve.intensity, dtype=float)
            count = np.asarray(curve.count)
            if x.shape != y.shape or x.shape != count.shape:
                raise ValueError("curve arrays have different shapes")
            local = (x - peak_center_deg + 180) % 360 - 180
            mask = np.abs(local) <= half_window_deg
            if np.any(mask & (~np.isfinite(y) | (count <= 0))):
                raise ValueError("peak window contains missing or masked bins")
            fit = fit_peak(local, y, model=peak_model, background="constant",
                           x_range=(-half_window_deg, half_window_deg),
                           x_unit="deg", y_unit=curve.intensity_unit)
            if not fit.metrics.get("success"):
                raise ValueError("peak fit did not converge")
            if width_definition == "integral_breadth":
                baseline = fit.parameters["background_intercept"].value
                width = np.degrees(measured_integral_breadth(curve,
                    peak_center_deg + fit.parameters["center"].value, baseline))
            else:
                width = fit.parameters["fwhm"].value / (2 if width_definition == "HWHM" else 1)
            output.append(RulandSlice(float(q), str(name), fit, float(width), None))
        except (ValueError, PeakFitError, KeyError) as exc:
            output.append(RulandSlice(float(q), str(name), None, None, str(exc)))
    series = RulandSeries(tuple(output), None, None, width_definition, q_unit, model)
    return refit_ruland_series(series, chosen)


def refit_ruland_series(series, selected):
    """Recompute the regression from cached peak widths."""
    if any(chosen and item.error for chosen, item in zip(selected, series.slices, strict=True)):
        return replace(series, result=None, error="selected slices include failed peak fits")
    valid = [(item, chosen) for item, chosen in zip(series.slices, selected, strict=True)
             if item.error is None]
    try:
        result = fit_ruland(
            [item.q for item, _ in valid],
            [item.width_deg for item, _ in valid],
            model=series.model, width_definition=series.width_definition,
            q_unit=series.q_unit, width_unit="deg",
            formula_version="pauw_2010" if series.width_definition == "integral_breadth" else "liu_2025",
            selected=np.array([chosen for _, chosen in valid], dtype=bool))
    except ValueError as exc:
        return replace(series, result=None, error=str(exc))
    return replace(series, result=result, error=None)
