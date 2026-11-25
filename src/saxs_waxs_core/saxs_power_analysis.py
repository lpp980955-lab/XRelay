"""Explicit-range Guinier and Porod candidate analysis of 1D SAXS curves.

These transformations are mathematical fits, not proof of an applicable
microstructural model. No background, range or unit is silently inferred.
"""
from dataclasses import dataclass
import numpy as np
from scipy.stats import linregress


@dataclass(frozen=True)
class PowerAnalysis:
    method: str
    q_unit: str
    q_range: tuple[float, float]
    q: np.ndarray
    intensity: np.ndarray
    fitted_intensity: np.ndarray
    residual: np.ndarray
    slope: float
    intercept: float
    r_squared: float
    derived_value: float
    derived_unit: str
    slope_stderr: float | None
    intercept_stderr: float | None
    derived_stderr: float | None
    zero_intensity: float | None
    zero_intensity_stderr: float | None
    warnings: tuple[str, ...]


def fit_saxs_power(q, intensity, *, method: str, q_range, q_unit: str) -> PowerAnalysis:
    """Fit Guinier ln(I) vs q² or Porod ln(I) vs ln(q) in an explicit range.

    q_unit must identify q, not s or 2-theta. Intensity is assumed to have
    already had any scientifically chosen background subtracted.
    """
    if method not in ("guinier", "porod"):
        raise ValueError("method must be guinier or porod")
    if q_unit not in ("nm^-1", "A^-1"):
        raise ValueError("q unit must be nm^-1 or A^-1")
    q = np.asarray(q, dtype=float)
    intensity = np.asarray(intensity, dtype=float)
    if q.ndim != 1 or q.shape != intensity.shape:
        raise ValueError("q and intensity must be equal-length one-dimensional arrays")
    if not np.isfinite(q).all():
        raise ValueError("q contains nonfinite coordinates")
    lower, upper = map(float, q_range)
    if not np.isfinite([lower, upper]).all() or lower < 0 or lower >= upper:
        raise ValueError("q range must be finite, nonnegative and increasing")
    chosen = (q >= lower) & (q <= upper)
    if np.any(chosen & (~np.isfinite(q) | ~np.isfinite(intensity) | (intensity <= 0))):
        raise ValueError("selected range contains invalid or nonpositive intensity")
    selected_q, selected_i = q[chosen], intensity[chosen]
    if len(selected_q) < 4 or np.any(np.diff(selected_q) <= 0):
        raise ValueError("at least four strictly increasing q points are required")
    if method == "porod" and np.any(selected_q <= 0):
        raise ValueError("Porod fit requires positive q")
    x = selected_q ** 2 if method == "guinier" else np.log(selected_q)
    y = np.log(selected_i)
    regression = linregress(x, y)
    slope, intercept = float(regression.slope), float(regression.intercept)
    if method == "guinier" and slope >= 0:
        raise ValueError("Guinier slope must be negative to calculate Rg")
    prediction = slope * x + intercept
    error = y - prediction
    total = float(np.sum((y - y.mean()) ** 2))
    r_squared = float(1 - np.sum(error ** 2) / total) if total > 0 else float("nan")
    derived = float(np.sqrt(-3 * slope)) if method == "guinier" else float(-slope)
    slope_stderr = float(regression.stderr) if np.isfinite(regression.stderr) else None
    intercept_stderr = (float(regression.intercept_stderr)
                        if np.isfinite(regression.intercept_stderr) else None)
    derived_stderr = (None if slope_stderr is None else
                      float(3*slope_stderr/(2*derived) if method == "guinier" else slope_stderr))
    zero_intensity = float(np.exp(intercept)) if method == "guinier" else None
    if zero_intensity is not None and not np.isfinite(zero_intensity):
        raise ValueError("Guinier zero-angle intensity is not finite")
    zero_stderr = (None if zero_intensity is None or intercept_stderr is None else
                   float(zero_intensity * intercept_stderr))
    warnings = ["candidate fit; verify range, background and physical assumptions"]
    warnings.append("standard errors assume unweighted linear regression in transformed coordinates")
    if method == "guinier" and selected_q[-1] * derived > 1.3:
        warnings.append("q_max * Rg exceeds 1.3; standard compact-particle Guinier range may not apply")
    return PowerAnalysis(method, q_unit, (lower, upper), selected_q, selected_i,
                         np.exp(prediction), selected_i - np.exp(prediction),
                         float(slope), float(intercept), r_squared, derived,
                         ("nm" if q_unit == "nm^-1" else "A") if method == "guinier" else "1",
                         slope_stderr, intercept_stderr, derived_stderr, zero_intensity, zero_stderr,
                         tuple(warnings))
