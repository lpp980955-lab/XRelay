"""Finite-range crystalline fraction from explicitly classified fit components."""
from dataclasses import dataclass
from typing import Mapping
import numpy as np
from scipy.integrate import IntegrationWarning, quad
import warnings
from .peak_fitting import PeakFitResult, _evaluate_peak, _peak_parameter_names

CRYSTALLINITY_METHOD_ID = "F-002:component_area_ratio"


@dataclass(frozen=True)
class CrystallinityResult:
    fraction: float
    crystalline_area: float
    amorphous_area: float
    component_areas: Mapping[str, float]
    classifications: Mapping[str, str]
    integration_range: tuple[float, float]
    x_unit: str
    area_unit: str
    fit_model: str
    background_model: str
    warnings: tuple[str, ...]
    area_method: str = "sampled_trapezoid"
    method_id: str = CRYSTALLINITY_METHOD_ID


def _component_models(fit: PeakFitResult) -> tuple[str, ...]:
    count = len(fit.components)
    if fit.model.startswith("mixed:"):
        models = tuple(fit.model.removeprefix("mixed:").split(","))
    else:
        base, separator, declared_count = fit.model.rpartition("_x")
        if not separator or not declared_count.isdecimal() or int(declared_count) != count:
            raise ValueError("fitted component models cannot be identified")
        models = (base,) * count
    if len(models) != count:
        raise ValueError("fitted component model count does not match components")
    return models


def _model_component_area(fit: PeakFitResult, name: str, model: str,
                          lower: float, upper: float, sampled: np.ndarray) -> float:
    prefix = name + "_"
    try:
        values = {key: float(fit.parameters[prefix + key].value)
                  for key in _peak_parameter_names(model)}
    except (KeyError, ValueError) as exc:
        raise ValueError(f"missing fitted parameters for {name}") from exc
    if not all(np.isfinite(value) for value in values.values()):
        raise ValueError(f"nonfinite fitted parameters for {name}")
    reconstructed = _evaluate_peak(model, np.asarray(fit.used_x, dtype=float), values)
    if not np.allclose(reconstructed, sampled, rtol=1e-7, atol=1e-9):
        raise ValueError(f"fitted parameters and sampled component disagree for {name}")
    center = values["center"]
    def value_at(position: float) -> float:
        return float(_evaluate_peak(model, np.asarray([position]), values)[0])
    with warnings.catch_warnings():
        warnings.simplefilter("error", IntegrationWarning)
        try:
            area, _error = quad(value_at, lower, upper,
                                points=[center] if lower < center < upper else None,
                                epsabs=1e-10, epsrel=1e-9, limit=200)
        except (IntegrationWarning, ValueError, OverflowError, ZeroDivisionError) as exc:
            raise ValueError(f"model area integration failed for {name}: {exc}") from exc
    if not np.isfinite(area) or area < 0:
        raise ValueError(f"nonphysical integrated component area for {name}")
    return float(area)


def crystallinity_from_fit(fit: PeakFitResult, classifications: Mapping[str, str]) -> CrystallinityResult:
    """Compute Ac/(Ac+Aa) over the actual fitted x range, excluding background.

    Every fitted peak must be explicitly classified as crystalline or
    amorphous. This is a fit-derived candidate, not a phase identification.
    """
    if not fit.components or set(classifications) != set(fit.components):
        raise ValueError("classify every fitted component exactly once")
    if set(classifications.values()) - {"crystalline", "amorphous"}:
        raise ValueError("classification must be crystalline or amorphous")
    if set(classifications.values()) != {"crystalline", "amorphous"}:
        raise ValueError("both crystalline and amorphous components are required")
    if fit.metrics.get("success") is False:
        raise ValueError("peak fit did not converge")
    centers = [fit.parameters.get(f"{name}_center") for name in fit.components]
    if all(value is not None for value in centers) and any(
            left.value >= right.value for left, right in zip(centers, centers[1:])):
        raise ValueError("fitted peak centers crossed; verify identity or constrain peak positions")
    if fit.input_counts.get("excluded_nonfinite_in_range", 0):
        raise ValueError("fitted range contains missing data; do not bridge gaps silently")
    x = np.asarray(fit.used_x, dtype=float)
    if x.ndim != 1 or x.size < 2 or not np.isfinite(x).all() or np.any(np.diff(x) <= 0):
        raise ValueError("fit x must be finite and strictly increasing")
    areas = {}
    model_based = bool(fit.parameters)
    models = _component_models(fit) if model_based else ()
    for index, (name, values) in enumerate(fit.components.items()):
        values = np.asarray(values, dtype=float)
        if values.shape != x.shape or not np.isfinite(values).all() or np.any(values < 0):
            raise ValueError("component intensities must be finite, nonnegative and match x")
        areas[name] = (_model_component_area(fit, name, models[index], float(x[0]), float(x[-1]), values)
                       if model_based else float(np.trapezoid(values, x)))
    crystalline = sum(areas[name] for name, kind in classifications.items() if kind == "crystalline")
    amorphous = sum(areas[name] for name, kind in classifications.items() if kind == "amorphous")
    if crystalline + amorphous <= 0:
        raise ValueError("component area sum must be positive")
    return CrystallinityResult(crystalline / (crystalline + amorphous),
        crystalline, amorphous, areas, dict(classifications), (float(x[0]), float(x[-1])),
        fit.x_unit, f"{fit.y_unit} * {fit.x_unit}", fit.model, fit.background,
        ("candidate depends on component assignments, background, fit model and finite integration range",
         "uncertainty and physical phase assignment have not been established") +
        (() if model_based else ("sampled trapezoid used: fitted parameters unavailable",)) +
        tuple(f"fit warning: {warning}" for warning in fit.warnings),
        "adaptive_model_quadrature" if model_based else "sampled_trapezoid")
