"""Explicit Debye–Bueche candidates for already processed 1D SAXS data.

The fitted L values are correlation lengths under the selected model, not
automatically pore radii or independently validated morphology.
"""
from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares


@dataclass(frozen=True)
class DebyeBuecheResult:
    model: str
    q_unit: str
    length_unit: str
    q_range: tuple[float, float]
    q: np.ndarray
    intensity: np.ndarray
    fitted_intensity: np.ndarray
    residual: np.ndarray
    correlation_lengths: tuple[float, ...]
    amplitudes: tuple[float, ...]
    constant_background: float
    background_fitted: bool
    rss: float
    rmse: float
    r_squared: float
    aic: float
    bic: float
    diagnostic_flags: tuple[str, ...]
    warnings: tuple[str, ...]


def _curve(q, params, model):
    if model == "single":
        amplitude, length, background = params
        return amplitude / (1 + (q * length) ** 2) ** 2 + background
    long_amplitude, long_length, short_amplitude, short_length, background = params
    return (long_amplitude / (1 + (q * long_length) ** 2) ** 2
            + short_amplitude / (1 + (q * short_length) ** 2) ** 2
            + background)


def fit_debye_bueche(q, intensity, *, model: str, q_range, q_unit: str,
                     constant_background: bool = True) -> DebyeBuecheResult:
    """Fit the single or double Debye–Bueche candidate on an explicit q range.

    The supplied intensity is used as-is. Neither air subtraction nor a q
    interval is inferred. The two-component lengths are sorted long to short.
    """
    if model not in ("single", "double"):
        raise ValueError("model must be single or double")
    if q_unit not in ("nm^-1", "A^-1"):
        raise ValueError("q unit must be nm^-1 or A^-1")
    q = np.asarray(q, dtype=float)
    intensity = np.asarray(intensity, dtype=float)
    if q.ndim != 1 or q.shape != intensity.shape:
        raise ValueError("q and intensity must be equal-length one-dimensional arrays")
    if not np.isfinite(q).all():
        raise ValueError("q contains nonfinite coordinates")
    try:
        lower, upper = map(float, q_range)
    except (TypeError, ValueError) as exc:
        raise ValueError("q range must contain two finite numbers") from exc
    if not np.isfinite([lower, upper]).all() or lower < 0 or lower >= upper:
        raise ValueError("q range must be finite, nonnegative and increasing")
    chosen = (q >= lower) & (q <= upper)
    selected_q, selected_i = q[chosen], intensity[chosen]
    minimum = 12 if model == "double" else 8
    if (len(selected_q) < minimum or np.any(np.diff(selected_q) <= 0)
            or not np.isfinite(selected_i).all()):
        raise ValueError(f"at least {minimum} finite points with increasing q are required")
    if np.any(selected_i < 0):
        raise ValueError("selected intensity contains negative values; choose a valid model/range")
    scale = float(max(np.ptp(selected_i), np.max(np.abs(selected_i)), 1e-12))
    y = selected_i / scale
    q_mid = max(float(np.median(selected_q)), 1e-12)
    q_low = max(float(selected_q[max(0, len(selected_q)//8)]), 1e-12)
    q_high = max(float(selected_q[-max(2, len(selected_q)//8)]), 1e-12)
    bg0 = float(max(0, np.percentile(y, 5))) if constant_background else 0.0
    amp0 = max(float(y.max() - bg0), 1e-8)
    if model == "single":
        starts = [[amp0, 1/q0, bg0] for q0 in (q_low, q_mid, q_high)]
        bounds = ([0, 1e-12, 0],
                  [np.inf, np.inf, np.inf if constant_background else 0])
    else:
        starts = [[amp0*f, 1/q_low, amp0*(1-f), 1/q_high, bg0]
                  for f in (.25, .5, .75)]
        bounds = ([0, 1e-12, 0, 1e-12, 0],
                  [np.inf, np.inf, np.inf, np.inf, np.inf if constant_background else 0])
    if not constant_background:
        # Least-squares requires strict bounds; remove the fixed background.
        starts = [start[:-1] for start in starts]
        bounds = (bounds[0][:-1], bounds[1][:-1])

    def predict(parameters):
        full = parameters if constant_background else [*parameters, 0.0]
        return _curve(selected_q, full, model)

    fits = [least_squares(lambda p: predict(p) - y, start, bounds=bounds,
                          max_nfev=8000) for start in starts]
    valid = [fit for fit in fits if fit.success and np.isfinite(fit.x).all()]
    if not valid:
        raise ValueError("Debye–Bueche fit did not converge")
    best = min(valid, key=lambda fit: float(np.dot(fit.fun, fit.fun)))
    params = np.asarray(best.x if constant_background else [*best.x, 0.0], dtype=float)
    if model == "double" and params[1] < params[3]:
        params[:4] = params[[2, 3, 0, 1]]
    fitted = _curve(selected_q, params, model) * scale
    residual = selected_i - fitted
    rss = float(np.dot(residual, residual))
    total = float(np.sum((selected_i - selected_i.mean())**2))
    n, k = len(selected_q), len(best.x)
    floor = np.finfo(float).tiny
    aic = float(n * np.log(max(rss/n, floor)) + 2*k)
    bic = float(n * np.log(max(rss/n, floor)) + k*np.log(n))
    if model == "single":
        lengths, amplitudes = (float(params[1]),), (float(params[0]*scale),)
    else:
        lengths = float(params[1]), float(params[3])
        amplitudes = float(params[0]*scale), float(params[2]*scale)
    warnings = ["candidate model; correlation length is not automatically a pore radius",
                "inspect range, background, residual and directional applicability",
                "AIC/BIC comparisons require identical q points and intensity scale"]
    flags = []
    if model == "double" and lengths[0] / lengths[1] < 1.5:
        flags.append("components_unresolved")
        warnings.append("two fitted length scales are poorly separated")
    if any(selected_q[0]*length > 1 or selected_q[-1]*length < 1 for length in lengths):
        flags.append("range_does_not_span_knee")
        warnings.append("the chosen q range does not span the knee of every component")
    if any(amplitude < scale*1e-4 for amplitude in amplitudes):
        flags.append("weak_component")
        warnings.append("one component amplitude is near zero; model may be overparameterized")
    return DebyeBuecheResult(model, q_unit, "nm" if q_unit == "nm^-1" else "A",
                             (lower, upper), selected_q.copy(), selected_i.copy(),
                             fitted, residual, lengths, amplitudes, float(params[-1]*scale),
                             constant_background,
                             rss, float(np.sqrt(rss/n)), float(1-rss/total) if total > 0 else float("nan"),
                             aic, bic, tuple(flags), tuple(warnings))
