"""Auditable one-dimensional single-peak fitting for Phase 6 analysis cards.

The module deliberately keeps model choice explicit.  Fit statistics may be used
to compare candidates, but they never change the selected scientific model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import least_squares
from scipy.signal import find_peaks
from scipy.special import gammaln, voigt_profile


PEAK_FIT_SCHEMA = "saxs_waxs_peak_fit/1"
SUPPORTED_PEAK_MODELS = (
    "gaussian_area",
    "gaussian_height",
    "lorentzian_area",
    "voigt_area",
    "pseudo_voigt_area",
    "pearson7_area",
    "split_gaussian_area",
    "split_lorentzian_area",
)
SUPPORTED_BACKGROUNDS = ("none", "constant", "linear")


class PeakFitError(ValueError):
    """Raised when a fit request is incomplete or numerically unusable."""


@dataclass(frozen=True)
class FitParameter:
    value: float
    stderr: float | None
    lower_bound: float | None = None
    upper_bound: float | None = None
    unit: str = ""
    derived: bool = False

    def to_document(self) -> dict[str, Any]:
        return {
            "value": float(self.value),
            "stderr": None if self.stderr is None else float(self.stderr),
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "unit": self.unit,
            "derived": bool(self.derived),
        }


@dataclass(frozen=True)
class PeakFitResult:
    model: str
    background: str
    x_unit: str
    y_unit: str
    requested_range: tuple[float, float]
    used_x: np.ndarray
    observed_y: np.ndarray
    best_fit_y: np.ndarray
    residual_y: np.ndarray
    parameters: Mapping[str, FitParameter]
    metrics: Mapping[str, float | int | bool | str | None]
    input_counts: Mapping[str, int]
    warnings: tuple[str, ...]
    components: Mapping[str, np.ndarray] = field(default_factory=dict)

    def to_document(self, *, include_arrays: bool = True) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema": PEAK_FIT_SCHEMA,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "model": self.model,
            "background": self.background,
            "x_unit": self.x_unit,
            "y_unit": self.y_unit,
            "requested_range": [float(v) for v in self.requested_range],
            "parameters": {
                name: value.to_document() for name, value in self.parameters.items()
            },
            "metrics": dict(self.metrics),
            "input_counts": dict(self.input_counts),
            "warnings": list(self.warnings),
            "scientific_status": "technical_fit_only",
            "scientific_note": (
                "Convergence and fit statistics do not prove that the chosen "
                "line shape or range is physically valid."
            ),
        }
        if include_arrays:
            document["arrays"] = {
                "x": self.used_x.tolist(),
                "observed": self.observed_y.tolist(),
                "best_fit": self.best_fit_y.tolist(),
                "residual": self.residual_y.tolist(),
                "components": {
                    name: np.asarray(values, dtype=float).tolist()
                    for name, values in self.components.items()
                },
            }
        return document


@dataclass(frozen=True)
class PeakModelComparison:
    results: tuple[PeakFitResult, ...]
    failures: Mapping[str, str]

    @property
    def ranked_by_aic(self) -> tuple[PeakFitResult, ...]:
        def rank(item: PeakFitResult) -> tuple[bool, float]:
            aic = float(item.metrics.get("aic", math.inf))
            return (not bool(item.metrics.get("success", False)), aic if math.isfinite(aic) else math.inf)
        return tuple(
            sorted(
                self.results,
                key=rank,
            )
        )


def _validate_choice(value: str, allowed: Sequence[str], label: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in allowed:
        raise PeakFitError(f"unsupported {label}: {value!r}")
    return normalized


def _background_parameter_names(background: str) -> tuple[str, ...]:
    if background == "none":
        return ()
    if background == "constant":
        return ("background_intercept",)
    return ("background_intercept", "background_slope")


def _peak_parameter_names(model: str) -> tuple[str, ...]:
    if model == "gaussian_height":
        return ("height", "center", "sigma")
    if model in ("gaussian_area", "lorentzian_area"):
        return ("area", "center", "sigma")
    if model == "voigt_area":
        return ("area", "center", "sigma", "gamma")
    if model == "pearson7_area":
        return ("area", "center", "fwhm", "exponent")
    if model in ("split_gaussian_area", "split_lorentzian_area"):
        return ("area", "center", "width_left", "width_right")
    return ("area", "center", "fwhm", "fraction")


def _evaluate_peak(model: str, x: np.ndarray, values: Mapping[str, float]) -> np.ndarray:
    delta = x - values["center"]
    if model == "gaussian_height":
        sigma = values["sigma"]
        return values["height"] * np.exp(-0.5 * (delta / sigma) ** 2)
    if model == "gaussian_area":
        sigma = values["sigma"]
        return (
            values["area"]
            / (sigma * math.sqrt(2.0 * math.pi))
            * np.exp(-0.5 * (delta / sigma) ** 2)
        )
    if model == "lorentzian_area":
        sigma = values["sigma"]
        return values["area"] * sigma / (math.pi * (delta**2 + sigma**2))
    if model == "voigt_area":
        return values["area"] * voigt_profile(
            delta, values["sigma"], values["gamma"]
        )
    if model == "pearson7_area":
        fwhm = values["fwhm"]
        exponent = values["exponent"]
        shape = 4.0 * np.expm1(math.log(2.0) / exponent) / (fwhm**2)
        log_norm = (
            0.5 * math.log(shape)
            + float(gammaln(exponent))
            - 0.5 * math.log(math.pi)
            - float(gammaln(exponent - 0.5))
        )
        return values["area"] * math.exp(log_norm) * (1.0 + shape * delta**2) ** (-exponent)
    if model == "split_gaussian_area":
        width = np.where(delta < 0.0, values["width_left"], values["width_right"])
        height = values["area"] * math.sqrt(2.0 / math.pi) / (
            values["width_left"] + values["width_right"]
        )
        return height * np.exp(-0.5 * (delta / width) ** 2)
    if model == "split_lorentzian_area":
        width = np.where(delta < 0.0, values["width_left"], values["width_right"])
        height = 2.0 * values["area"] / (
            math.pi * (values["width_left"] + values["width_right"])
        )
        return height / (1.0 + (delta / width) ** 2)
    fwhm = values["fwhm"]
    fraction = values["fraction"]
    gaussian = (
        2.0
        * math.sqrt(math.log(2.0))
        / (fwhm * math.sqrt(math.pi))
        * np.exp(-4.0 * math.log(2.0) * (delta / fwhm) ** 2)
    )
    half_width = 0.5 * fwhm
    lorentzian = half_width / (math.pi * (delta**2 + half_width**2))
    return values["area"] * ((1.0 - fraction) * gaussian + fraction * lorentzian)


def _evaluate_model(
    model: str,
    background: str,
    x: np.ndarray,
    values: Mapping[str, float],
    x_reference: float,
) -> np.ndarray:
    result = _evaluate_peak(model, x, values)
    if background != "none":
        result = result + values["background_intercept"]
    if background == "linear":
        result = result + values["background_slope"] * (x - x_reference)
    return np.asarray(result, dtype=float)


def _edge_background_guess(
    x: np.ndarray, y: np.ndarray, background: str, x_reference: float
) -> tuple[float, float]:
    if background == "none":
        return 0.0, 0.0
    edge_count = max(2, min(x.size // 4, max(3, int(math.ceil(x.size * 0.1)))))
    indices = np.r_[0:edge_count, x.size - edge_count : x.size]
    edge_x = x[indices]
    edge_y = y[indices]
    if background == "linear" and np.unique(edge_x).size >= 2:
        slope, intercept_at_zero = np.polyfit(edge_x, edge_y, 1)
        return float(intercept_at_zero + slope * x_reference), float(slope)
    return float(np.median(edge_y)), 0.0


def _initial_peak_width(
    x: np.ndarray, signal: np.ndarray, center_index: int, span: float
) -> float:
    height = float(signal[center_index])
    if height <= 0.0:
        return span / 10.0
    above = np.flatnonzero(signal >= 0.5 * height)
    if above.size >= 2:
        width = abs(float(x[above[-1]] - x[above[0]]))
        if width > 0.0:
            return min(max(width, span / (x.size * 4.0)), span)
    return span / 10.0


def _build_initial_values(
    model: str,
    background: str,
    x: np.ndarray,
    y: np.ndarray,
) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray, float]:
    x_reference = float(np.mean(x))
    intercept, slope = _edge_background_guess(x, y, background, x_reference)
    baseline = np.zeros_like(y)
    if background != "none":
        baseline += intercept
    if background == "linear":
        baseline += slope * (x - x_reference)
    signal = y - baseline
    center_index = int(np.argmax(signal))
    peak_height = float(signal[center_index])
    if not np.isfinite(peak_height) or peak_height <= 0.0:
        raise PeakFitError("selected range does not contain a positive peak above background")
    x_min = float(np.min(x))
    x_max = float(np.max(x))
    span = x_max - x_min
    if span <= 0.0:
        raise PeakFitError("fit x range has zero span")
    fwhm = _initial_peak_width(x, signal, center_index, span)
    sigma = max(fwhm / (2.0 * math.sqrt(2.0 * math.log(2.0))), span * 1e-6)
    area = max(float(np.trapezoid(np.maximum(signal, 0.0), x)), peak_height * span * 1e-6)

    names = list(_peak_parameter_names(model)) + list(
        _background_parameter_names(background)
    )
    initial_map: dict[str, float] = {
        "area": area,
        "height": peak_height,
        "center": float(x[center_index]),
        "sigma": sigma,
        "gamma": sigma,
        "fwhm": fwhm,
        "fraction": 0.5,
        "exponent": 2.0,
        "width_left": sigma,
        "width_right": sigma,
        "background_intercept": intercept,
        "background_slope": slope,
    }
    tiny_width = max(np.finfo(float).eps * max(abs(x_min), abs(x_max), 1.0), span * 1e-9)
    lower_map = {
        "area": 0.0,
        "height": 0.0,
        "center": x_min,
        "sigma": tiny_width,
        "gamma": tiny_width,
        "fwhm": tiny_width,
        "fraction": 0.0,
        "exponent": 0.5001,
        "width_left": tiny_width,
        "width_right": tiny_width,
        "background_intercept": -np.inf,
        "background_slope": -np.inf,
    }
    upper_map = {
        "area": np.inf,
        "height": np.inf,
        "center": x_max,
        "sigma": span,
        "gamma": span,
        "fwhm": 2.0 * span,
        "fraction": 1.0,
        "exponent": 100.0,
        "width_left": span,
        "width_right": span,
        "background_intercept": np.inf,
        "background_slope": np.inf,
    }
    return (
        names,
        np.asarray([initial_map[name] for name in names], dtype=float),
        np.asarray([lower_map[name] for name in names], dtype=float),
        np.asarray([upper_map[name] for name in names], dtype=float),
        x_reference,
    )


def _parameter_stderr(jacobian: np.ndarray, rss: float, dof: int) -> np.ndarray | None:
    if dof <= 0 or jacobian.ndim != 2 or jacobian.shape[0] <= jacobian.shape[1]:
        return None
    try:
        information = jacobian.T @ jacobian
        if np.linalg.matrix_rank(information) != information.shape[0]:
            return None
        covariance = np.linalg.inv(information) * (rss / dof)
    except np.linalg.LinAlgError:
        return None
    diagonal = np.diag(covariance)
    if np.any(diagonal < 0.0) or not np.all(np.isfinite(diagonal)):
        return None
    return np.sqrt(diagonal)


def _derived_parameters(
    model: str,
    values: Mapping[str, float],
    stderr: Mapping[str, float | None],
    x_unit: str,
    y_unit: str,
) -> dict[str, FitParameter]:
    derived: dict[str, FitParameter] = {}
    if model in ("gaussian_area", "gaussian_height"):
        sigma = values["sigma"]
        fwhm_factor = 2.0 * math.sqrt(2.0 * math.log(2.0))
        fwhm_stderr = stderr.get("sigma")
        if fwhm_stderr is not None:
            fwhm_stderr *= fwhm_factor
        derived["fwhm"] = FitParameter(
            sigma * fwhm_factor, fwhm_stderr, unit=x_unit, derived=True
        )
        if model == "gaussian_area":
            height = values["area"] / (sigma * math.sqrt(2.0 * math.pi))
            derived["height"] = FitParameter(
                height, None, unit=y_unit, derived=True
            )
        else:
            area = values["height"] * sigma * math.sqrt(2.0 * math.pi)
            derived["area"] = FitParameter(
                area, None, unit=f"{y_unit}·{x_unit}", derived=True
            )
    elif model == "lorentzian_area":
        sigma_stderr = stderr.get("sigma")
        derived["fwhm"] = FitParameter(
            2.0 * values["sigma"],
            None if sigma_stderr is None else 2.0 * sigma_stderr,
            unit=x_unit,
            derived=True,
        )
        derived["height"] = FitParameter(
            values["area"] / (math.pi * values["sigma"]),
            None,
            unit=y_unit,
            derived=True,
        )
    elif model == "voigt_area":
        sigma = values["sigma"]
        gamma = values["gamma"]
        gaussian_fwhm = 2.0 * math.sqrt(2.0 * math.log(2.0)) * sigma
        lorentz_fwhm = 2.0 * gamma
        fwhm = 0.5346 * lorentz_fwhm + math.sqrt(
            0.2166 * lorentz_fwhm**2 + gaussian_fwhm**2
        )
        derived["fwhm"] = FitParameter(fwhm, None, unit=x_unit, derived=True)
        derived["height"] = FitParameter(
            float(_evaluate_peak(model, np.asarray([values["center"]]), values)[0]),
            None,
            unit=y_unit,
            derived=True,
        )
    elif model == "pearson7_area":
        derived["height"] = FitParameter(
            float(_evaluate_peak(model, np.asarray([values["center"]]), values)[0]),
            None,
            unit=y_unit,
            derived=True,
        )
    elif model in ("split_gaussian_area", "split_lorentzian_area"):
        if model == "split_gaussian_area":
            factor = math.sqrt(2.0 * math.log(2.0))
            fwhm = factor * (values["width_left"] + values["width_right"])
        else:
            fwhm = values["width_left"] + values["width_right"]
        derived["fwhm"] = FitParameter(fwhm, None, unit=x_unit, derived=True)
        derived["height"] = FitParameter(
            float(_evaluate_peak(model, np.asarray([values["center"]]), values)[0]),
            None,
            unit=y_unit,
            derived=True,
        )
    else:
        derived["height"] = FitParameter(
            float(_evaluate_peak(model, np.asarray([values["center"]]), values)[0]),
            None,
            unit=y_unit,
            derived=True,
        )
    return derived


def fit_peak(
    x: Sequence[float] | np.ndarray,
    y: Sequence[float] | np.ndarray,
    *,
    model: str = "gaussian_height",
    background: str = "constant",
    x_range: tuple[float, float] | None = None,
    x_unit: str = "",
    y_unit: str = "",
) -> PeakFitResult:
    """Fit one positive peak without smoothing or silent outlier removal."""

    model = _validate_choice(model, SUPPORTED_PEAK_MODELS, "peak model")
    background = _validate_choice(background, SUPPORTED_BACKGROUNDS, "background")
    x_array = np.asarray(x, dtype=float).reshape(-1)
    y_array = np.asarray(y, dtype=float).reshape(-1)
    if x_array.shape != y_array.shape or x_array.size == 0:
        raise PeakFitError("x and y must be non-empty one-dimensional arrays of equal length")
    total_count = int(x_array.size)
    finite = np.isfinite(x_array) & np.isfinite(y_array)
    if x_range is None:
        if not np.any(finite):
            raise PeakFitError("fit input contains no finite x/y pairs")
        requested_range = (
            float(np.min(x_array[finite])),
            float(np.max(x_array[finite])),
        )
    else:
        requested_range = (float(x_range[0]), float(x_range[1]))
        if not all(np.isfinite(requested_range)) or requested_range[0] >= requested_range[1]:
            raise PeakFitError("fit range must contain two finite increasing values")
    in_range = (x_array >= requested_range[0]) & (x_array <= requested_range[1])
    used = finite & in_range
    used_x = x_array[used]
    observed = y_array[used]
    if used_x.size < 7:
        raise PeakFitError("at least seven finite points are required in the fit range")
    order = np.argsort(used_x, kind="stable")
    used_x = used_x[order]
    observed = observed[order]
    if np.unique(used_x).size < 4:
        raise PeakFitError("fit range must contain at least four distinct x values")

    names, initial, lower, upper, x_reference = _build_initial_values(
        model, background, used_x, observed
    )

    def unpack(vector: np.ndarray) -> dict[str, float]:
        return {name: float(value) for name, value in zip(names, vector, strict=True)}

    def residual(vector: np.ndarray) -> np.ndarray:
        return _evaluate_model(
            model, background, used_x, unpack(vector), x_reference
        ) - observed

    fit = least_squares(
        residual,
        initial,
        bounds=(lower, upper),
        method="trf",
        loss="linear",
        x_scale="jac",
        max_nfev=10000,
    )
    values = unpack(fit.x)
    best_fit = _evaluate_model(model, background, used_x, values, x_reference)
    residual_values = observed - best_fit
    rss = float(np.sum(residual_values**2))
    parameter_count = len(names)
    dof = int(used_x.size - parameter_count)
    stderr_array = _parameter_stderr(np.asarray(fit.jac), rss, dof)
    stderr: dict[str, float | None] = {
        name: None if stderr_array is None else float(stderr_array[index])
        for index, name in enumerate(names)
    }
    parameter_units = {
        "area": f"{y_unit}·{x_unit}" if x_unit or y_unit else "",
        "height": y_unit,
        "center": x_unit,
        "sigma": x_unit,
        "gamma": x_unit,
        "fwhm": x_unit,
        "fraction": "1",
        "exponent": "1",
        "width_left": x_unit,
        "width_right": x_unit,
        "background_intercept": y_unit,
        "background_slope": f"{y_unit}/{x_unit}" if x_unit else y_unit,
    }
    parameters = {
        name: FitParameter(
            values[name],
            stderr[name],
            None if not np.isfinite(lower[index]) else float(lower[index]),
            None if not np.isfinite(upper[index]) else float(upper[index]),
            parameter_units.get(name, ""),
        )
        for index, name in enumerate(names)
    }
    parameters.update(_derived_parameters(model, values, stderr, x_unit, y_unit))

    mean_y = float(np.mean(observed))
    total_sum_squares = float(np.sum((observed - mean_y) ** 2))
    r_squared = (
        float(1.0 - rss / total_sum_squares)
        if total_sum_squares > 0.0
        else math.nan
    )
    safe_rss = max(rss, np.finfo(float).tiny)
    n_used = int(used_x.size)
    aic = float(n_used * math.log(safe_rss / n_used) + 2 * parameter_count)
    bic = float(
        n_used * math.log(safe_rss / n_used) + parameter_count * math.log(n_used)
    )
    warnings: list[str] = []
    if not fit.success:
        warnings.append("optimizer did not report success")
    if stderr_array is None:
        warnings.append("parameter covariance could not be estimated")
    tolerance = 1e-6
    for index, name in enumerate(names):
        scale = max(abs(float(fit.x[index])), 1.0)
        if np.isfinite(lower[index]) and abs(float(fit.x[index] - lower[index])) <= tolerance * scale:
            warnings.append(f"parameter {name} is at its lower bound")
        if np.isfinite(upper[index]) and abs(float(upper[index] - fit.x[index])) <= tolerance * scale:
            warnings.append(f"parameter {name} is at its upper bound")
    excluded_nonfinite = int(np.count_nonzero(in_range & ~finite))
    excluded_outside = int(total_count - np.count_nonzero(in_range))
    if excluded_nonfinite:
        warnings.append(
            f"{excluded_nonfinite} non-finite point(s) were explicitly excluded"
        )

    return PeakFitResult(
        model=model,
        background=background,
        x_unit=str(x_unit),
        y_unit=str(y_unit),
        requested_range=requested_range,
        used_x=used_x,
        observed_y=observed,
        best_fit_y=best_fit,
        residual_y=residual_values,
        parameters=parameters,
        metrics={
            "success": bool(fit.success),
            "optimizer_status": int(fit.status),
            "message": str(fit.message),
            "function_evaluations": int(fit.nfev),
            "data_points": n_used,
            "variable_parameters": parameter_count,
            "degrees_of_freedom": dof,
            "rss": rss,
            "reduced_chi_square": None if dof <= 0 else float(rss / dof),
            "rmse": float(math.sqrt(rss / n_used)),
            "r_squared": r_squared,
            "aic": aic,
            "bic": bic,
        },
        input_counts={
            "total": total_count,
            "used": n_used,
            "excluded_outside_range": excluded_outside,
            "excluded_nonfinite_in_range": excluded_nonfinite,
            "excluded_as_outlier": 0,
        },
        warnings=tuple(warnings),
    )


def fit_multi_peak(
    x: Sequence[float] | np.ndarray,
    y: Sequence[float] | np.ndarray,
    *,
    peak_count: int,
    model: str = "pseudo_voigt_area",
    models: Sequence[str] | None = None,
    center_bounds: Sequence[tuple[float, float] | None] | None = None,
    fwhm_bounds: Sequence[tuple[float, float] | None] | None = None,
    background: str = "constant",
    x_range: tuple[float, float] | None = None,
    x_unit: str = "",
    y_unit: str = "",
) -> PeakFitResult:
    """Fit 2-8 positive peaks with one shared or explicitly assigned line shapes.

    This is a technical decomposition with deterministic automatic seeds.  Peak
    identity and crystalline/amorphous assignment remain user decisions.
    """

    peak_count = int(peak_count)
    component_models = tuple(models) if models is not None else (model,) * peak_count
    if len(component_models) != peak_count:
        raise PeakFitError("one model is required for each peak")
    component_models = tuple(_validate_choice(value, SUPPORTED_PEAK_MODELS, "peak model")
                             for value in component_models)
    bounds = tuple(center_bounds) if center_bounds is not None else (None,) * peak_count
    if len(bounds) != peak_count:
        raise PeakFitError("one center constraint is required for each peak")
    widths = tuple(fwhm_bounds) if fwhm_bounds is not None else (None,) * peak_count
    if len(widths) != peak_count:
        raise PeakFitError("one FWHM constraint is required for each peak")
    if peak_count == 1 and widths[0] is not None:
        raise PeakFitError("FWHM constraints require multiple peaks")
    if peak_count == 1 and bounds[0] is not None:
        raise PeakFitError("center constraints require multiple peaks")
    if peak_count == 1:
        return fit_peak(
            x,
            y,
            model=component_models[0],
            background=background,
            x_range=x_range,
            x_unit=x_unit,
            y_unit=y_unit,
        )
    if peak_count < 2 or peak_count > 8:
        raise PeakFitError("multi-peak fitting supports 2 to 8 peaks")
    background = _validate_choice(background, SUPPORTED_BACKGROUNDS, "background")
    x_array = np.asarray(x, dtype=float).reshape(-1)
    y_array = np.asarray(y, dtype=float).reshape(-1)
    if x_array.shape != y_array.shape or x_array.size == 0:
        raise PeakFitError("x and y must be non-empty one-dimensional arrays of equal length")
    total_count = int(x_array.size)
    finite = np.isfinite(x_array) & np.isfinite(y_array)
    if x_range is None:
        if not np.any(finite):
            raise PeakFitError("fit input contains no finite x/y pairs")
        requested_range = (
            float(np.min(x_array[finite])),
            float(np.max(x_array[finite])),
        )
    else:
        requested_range = (float(x_range[0]), float(x_range[1]))
        if not all(np.isfinite(requested_range)) or requested_range[0] >= requested_range[1]:
            raise PeakFitError("fit range must contain two finite increasing values")
    in_range = (x_array >= requested_range[0]) & (x_array <= requested_range[1])
    used = finite & in_range
    used_x = x_array[used]
    observed = y_array[used]
    if used_x.size < max(12, peak_count * 5):
        raise PeakFitError("fit range contains too few finite points for the requested peak count")
    order = np.argsort(used_x, kind="stable")
    used_x = used_x[order]
    observed = observed[order]
    x_min = float(used_x[0])
    x_max = float(used_x[-1])
    span = x_max - x_min
    if span <= 0.0:
        raise PeakFitError("fit x range has zero span")
    x_reference = float(np.mean(used_x))
    intercept, slope = _edge_background_guess(used_x, observed, background, x_reference)
    baseline = np.zeros_like(observed)
    if background != "none":
        baseline += intercept
    if background == "linear":
        baseline += slope * (used_x - x_reference)
    signal = observed - baseline
    prominence = max(float(np.ptp(signal)) * 0.015, np.finfo(float).eps)
    minimum_distance = max(1, int(used_x.size / max(peak_count * 3, 1)))
    detected, properties = find_peaks(
        signal,
        distance=minimum_distance,
        prominence=prominence,
    )
    ranked = list(
        detected[np.argsort(properties.get("prominences", signal[detected]))[::-1]]
    )
    for candidate in np.argsort(signal)[::-1]:
        candidate = int(candidate)
        if signal[candidate] <= 0.0:
            break
        if all(abs(candidate - existing) >= minimum_distance for existing in ranked):
            ranked.append(candidate)
        if len(ranked) >= peak_count:
            break
    if len(ranked) < peak_count:
        raise PeakFitError("could not seed the requested number of positive peaks")
    center_indices = sorted(ranked[:peak_count], key=lambda index: used_x[index])
    tiny_width = max(
        np.finfo(float).eps * max(abs(x_min), abs(x_max), 1.0),
        span * 1e-9,
    )
    default_fwhm = max(span / (peak_count * 5.0), tiny_width * 10.0)
    names: list[str] = []
    initial_values: list[float] = []
    lower_values: list[float] = []
    upper_values: list[float] = []
    for component_index, center_index in enumerate(center_indices, start=1):
        base_names = _peak_parameter_names(component_models[component_index - 1])
        center_bound = bounds[component_index - 1]
        if center_bound is not None:
            lower_center, upper_center = map(float, center_bound)
            if not np.isfinite([lower_center, upper_center]).all() or not x_min <= lower_center < upper_center <= x_max:
                raise PeakFitError("peak center bounds must lie within the fitted data range")
            indices = np.flatnonzero((used_x >= lower_center) & (used_x <= upper_center))
            if not indices.size:
                raise PeakFitError("no data points within peak center bounds")
            center_index = int(indices[np.argmax(signal[indices])])
        else:
            lower_center, upper_center = x_min, x_max
        prefix = f"peak_{component_index}_"
        height = max(float(signal[center_index]), np.finfo(float).eps)
        sigma = default_fwhm / (2.0 * math.sqrt(2.0 * math.log(2.0)))
        area = max(height * default_fwhm, np.finfo(float).eps)
        initial_map = {
            "area": area,
            "height": height,
            "center": float(used_x[center_index]),
            "sigma": sigma,
            "gamma": sigma,
            "fwhm": default_fwhm,
            "fraction": 0.5,
            "exponent": 2.0,
            "width_left": sigma,
            "width_right": sigma,
        }
        lower_map = {
            "area": 0.0,
            "height": 0.0,
            "center": lower_center,
            "sigma": tiny_width,
            "gamma": tiny_width,
            "fwhm": tiny_width,
            "fraction": 0.0,
            "exponent": 0.5001,
            "width_left": tiny_width,
            "width_right": tiny_width,
        }
        upper_map = {
            "area": np.inf,
            "height": np.inf,
            "center": upper_center,
            "sigma": span,
            "gamma": span,
            "fwhm": 2.0 * span,
            "fraction": 1.0,
            "exponent": 100.0,
            "width_left": span,
            "width_right": span,
        }
        width_bound = widths[component_index - 1]
        if width_bound is not None:
            low_width, high_width = map(float, width_bound)
            if not np.isfinite([low_width, high_width]).all() or not 0 < low_width < high_width:
                raise PeakFitError("FWHM bounds must be positive and increasing")
            component_model = component_models[component_index - 1]
            if component_model in ("gaussian_area", "gaussian_height"):
                parameter, factor = "sigma", 2 * math.sqrt(2 * math.log(2))
            elif component_model == "lorentzian_area":
                parameter, factor = "sigma", 2.0
            elif component_model in ("pseudo_voigt_area", "pearson7_area"):
                parameter, factor = "fwhm", 1.0
            else:
                raise PeakFitError("FWHM bounds are not supported for this peak model")
            lower_map[parameter], upper_map[parameter] = low_width / factor, high_width / factor
            initial_map[parameter] = (lower_map[parameter] + upper_map[parameter]) / 2
        for base_name in base_names:
            names.append(prefix + base_name)
            initial_values.append(initial_map[base_name])
            lower_values.append(lower_map[base_name])
            upper_values.append(upper_map[base_name])
    for name in _background_parameter_names(background):
        names.append(name)
        initial_values.append(intercept if name == "background_intercept" else slope)
        lower_values.append(-np.inf)
        upper_values.append(np.inf)
    initial = np.asarray(initial_values, dtype=float)
    lower = np.asarray(lower_values, dtype=float)
    upper = np.asarray(upper_values, dtype=float)

    def unpack(vector: np.ndarray) -> dict[str, float]:
        return {name: float(value) for name, value in zip(names, vector, strict=True)}

    def evaluate(vector: np.ndarray) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        values = unpack(vector)
        components: dict[str, np.ndarray] = {}
        total = np.zeros_like(used_x)
        for component_index in range(1, peak_count + 1):
            prefix = f"peak_{component_index}_"
            component_model = component_models[component_index - 1]
            base_names = _peak_parameter_names(component_model)
            component_values = {
                base_name: values[prefix + base_name] for base_name in base_names
            }
            component = _evaluate_peak(component_model, used_x, component_values)
            components[f"peak_{component_index}"] = component
            total += component
        if background != "none":
            total += values["background_intercept"]
        if background == "linear":
            total += values["background_slope"] * (used_x - x_reference)
        return total, components

    fit = least_squares(
        lambda vector: evaluate(vector)[0] - observed,
        initial,
        bounds=(lower, upper),
        method="trf",
        loss="linear",
        x_scale="jac",
        max_nfev=20000,
    )
    values = unpack(fit.x)
    best_fit, components = evaluate(fit.x)
    residual_values = observed - best_fit
    rss = float(np.sum(residual_values**2))
    parameter_count = len(names)
    dof = int(used_x.size - parameter_count)
    stderr_array = _parameter_stderr(np.asarray(fit.jac), rss, dof)
    stderr = {
        name: None if stderr_array is None else float(stderr_array[index])
        for index, name in enumerate(names)
    }
    parameter_units = {
        "area": f"{y_unit}·{x_unit}" if x_unit or y_unit else "",
        "height": y_unit,
        "center": x_unit,
        "sigma": x_unit,
        "gamma": x_unit,
        "fwhm": x_unit,
        "fraction": "1",
        "exponent": "1",
        "width_left": x_unit,
        "width_right": x_unit,
        "background_intercept": y_unit,
        "background_slope": f"{y_unit}/{x_unit}" if x_unit else y_unit,
    }
    parameters: dict[str, FitParameter] = {}
    for index, name in enumerate(names):
        base_name = name.split("_", 2)[2] if name.startswith("peak_") else name
        parameters[name] = FitParameter(
            values[name],
            stderr[name],
            None if not np.isfinite(lower[index]) else float(lower[index]),
            None if not np.isfinite(upper[index]) else float(upper[index]),
            parameter_units.get(base_name, ""),
        )
    for component_index in range(1, peak_count + 1):
        prefix = f"peak_{component_index}_"
        component_model = component_models[component_index - 1]
        base_names = _peak_parameter_names(component_model)
        component_values = {
            base_name: values[prefix + base_name] for base_name in base_names
        }
        component_stderr = {
            base_name: stderr[prefix + base_name] for base_name in base_names
        }
        for derived_name, parameter in _derived_parameters(
            component_model, component_values, component_stderr, x_unit, y_unit
        ).items():
            parameters[prefix + derived_name] = parameter
    mean_y = float(np.mean(observed))
    tss = float(np.sum((observed - mean_y) ** 2))
    safe_rss = max(rss, np.finfo(float).tiny)
    n_used = int(used_x.size)
    warnings: list[str] = []
    if not fit.success:
        warnings.append("optimizer did not report success")
    if stderr_array is None:
        warnings.append("parameter covariance could not be estimated")
    active_bounds = [name for name, active in zip(names, fit.active_mask, strict=True) if active]
    if active_bounds:
        warnings.append("parameters at optimization bounds; review constraints: " + ", ".join(active_bounds))
    warnings.append("automatic peak seeds require visual review")
    centers = [values[f"peak_{index}_center"] for index in range(1, peak_count + 1)]
    if any(left >= right for left, right in zip(centers, centers[1:])):
        warnings.append("peak centers crossed; verify peak identity")
    for index in range(1, peak_count):
        left_width = parameters[f"peak_{index}_fwhm"].value
        right_width = parameters[f"peak_{index + 1}_fwhm"].value
        if (np.isfinite(left_width) and np.isfinite(right_width)
                and centers[index] - centers[index - 1] < (left_width + right_width) / 2.0):
            warnings.append(
                f"peak {index} and peak {index + 1} fitted FWHM intervals overlap; "
                "review component identity and area stability"
            )
    return PeakFitResult(
        model=(f"{component_models[0]}_x{peak_count}" if len(set(component_models)) == 1
               else "mixed:" + ",".join(component_models)),
        background=background,
        x_unit=str(x_unit),
        y_unit=str(y_unit),
        requested_range=requested_range,
        used_x=used_x,
        observed_y=observed,
        best_fit_y=best_fit,
        residual_y=residual_values,
        parameters=parameters,
        metrics={
            "success": bool(fit.success),
            "optimizer_status": int(fit.status),
            "message": str(fit.message),
            "function_evaluations": int(fit.nfev),
            "data_points": n_used,
            "variable_parameters": parameter_count,
            "degrees_of_freedom": dof,
            "rss": rss,
            "reduced_chi_square": None if dof <= 0 else float(rss / dof),
            "rmse": float(math.sqrt(rss / n_used)),
            "r_squared": float(1.0 - rss / tss) if tss > 0.0 else math.nan,
            "aic": float(n_used * math.log(safe_rss / n_used) + 2 * parameter_count),
            "bic": float(
                n_used * math.log(safe_rss / n_used)
                + parameter_count * math.log(n_used)
            ),
            "peak_count": peak_count,
        },
        input_counts={
            "total": total_count,
            "used": n_used,
            "excluded_outside_range": int(total_count - np.count_nonzero(in_range)),
            "excluded_nonfinite_in_range": int(np.count_nonzero(in_range & ~finite)),
            "excluded_as_outlier": 0,
        },
        warnings=tuple(warnings),
        components=components,
    )


def compare_peak_models(
    x: Sequence[float] | np.ndarray,
    y: Sequence[float] | np.ndarray,
    *,
    models: Iterable[str] = (
        "gaussian_height",
        "lorentzian_area",
        "voigt_area",
        "pseudo_voigt_area",
    ),
    background: str = "constant",
    x_range: tuple[float, float] | None = None,
    x_unit: str = "",
    y_unit: str = "",
    peak_count: int = 1,
    center_bounds: Sequence[tuple[float, float] | None] | None = None,
    fwhm_bounds: Sequence[tuple[float, float] | None] | None = None,
) -> PeakModelComparison:
    """Compare shared line shapes at a fixed peak count without selecting a model."""

    if isinstance(peak_count, bool) or not isinstance(peak_count, int) or not 1 <= peak_count <= 8:
        raise PeakFitError("peak count must be an integer from 1 to 8")

    results: list[PeakFitResult] = []
    failures: dict[str, str] = {}
    seen: set[str] = set()
    for candidate in models:
        normalized = _validate_choice(candidate, SUPPORTED_PEAK_MODELS, "peak model")
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            if peak_count == 1:
                result = fit_peak(x, y, model=normalized, background=background,
                    x_range=x_range, x_unit=x_unit, y_unit=y_unit)
            else:
                result = fit_multi_peak(x, y, peak_count=peak_count, model=normalized,
                    center_bounds=center_bounds, fwhm_bounds=fwhm_bounds,
                    background=background, x_range=x_range, x_unit=x_unit, y_unit=y_unit)
            results.append(result)
        except PeakFitError as exc:
            failures[normalized] = str(exc)
    if not results:
        raise PeakFitError("all candidate peak models failed")
    return PeakModelComparison(tuple(results), failures)
