"""Versioned, explicit-width Ruland candidates; no automatic point rejection."""
from dataclasses import dataclass
import numpy as np
from scipy.stats import linregress


@dataclass(frozen=True)
class RulandResult:
    model: str
    formula_version: str
    width_definition: str
    q_nm_inv: np.ndarray
    width_rad: np.ndarray
    transformed_x: np.ndarray
    transformed_y: np.ndarray
    predicted_width_rad: np.ndarray
    residual_rad: np.ndarray
    length_nm: float
    misorientation_rad: float
    slope: float
    intercept: float
    r_squared: float
    slope_stderr: float | None
    intercept_stderr: float | None
    length_stderr_nm: float | None
    misorientation_stderr_rad: float | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RulandRangeCheck:
    label: str
    q_min_nm_inv: float
    q_max_nm_inv: float
    point_count: int
    result: RulandResult | None
    error: str | None


def fit_ruland(q, width, *, model: str, width_definition: str,
               q_unit: str, width_unit: str, selected=None, formula_version="liu_2025") -> RulandResult:
    """Fit F-006 or F-007; width is the quantity B used in the selected formula.

    FWHM and HWHM are labels, not converted automatically: their relationship
    to B must be established for the chosen source and peak shape.
    """
    if model not in ("cauchy", "gaussian"):
        raise ValueError("model must be cauchy or gaussian")
    if formula_version not in ("liu_2025", "pauw_2010"):
        raise ValueError("unknown Ruland formula version")
    if width_definition not in ("FWHM", "HWHM", "source_B", "integral_breadth"):
        raise ValueError("declare width definition")
    if (formula_version == "pauw_2010") != (width_definition == "integral_breadth"):
        raise ValueError("Pauw 2010 requires integral breadth; Liu candidate requires declared peak width")
    if q_unit not in ("nm^-1", "A^-1") or width_unit not in ("deg", "rad"):
        raise ValueError("unsupported q or angular width unit")
    q = np.asarray(q, dtype=float)
    width = np.asarray(width, dtype=float)
    if q.ndim != 1 or q.shape != width.shape:
        raise ValueError("q and width must be equal-length one-dimensional arrays")
    selection = np.ones(q.shape, dtype=bool) if selected is None else np.asarray(selected)
    if selection.shape != q.shape or selection.dtype.kind != "b":
        raise ValueError("selected must be a boolean array matching q")
    q = q[selection] * (10.0 if q_unit == "A^-1" else 1.0)
    width = width[selection] * (np.pi / 180 if width_unit == "deg" else 1.0)
    if len(q) < 3 or not np.isfinite(q).all() or not np.isfinite(width).all():
        raise ValueError("at least three finite selected points are required")
    if np.any(q <= 0) or np.any(width <= 0) or len(np.unique(q)) < 3:
        raise ValueError("q and width must be positive with at least three distinct q values")
    x = 1 / q if model == "cauchy" else 1 / q**2
    y = width if model == "cauchy" else width**2
    regression = linregress(x, y)
    slope, intercept = float(regression.slope), float(regression.intercept)
    if slope <= 0 or intercept < 0:
        raise ValueError("nonphysical slope or intercept: inspect model and selected q range")
    predicted_y = slope * x + intercept
    predicted_width = predicted_y if model == "cauchy" else np.sqrt(predicted_y)
    total = float(np.sum((y - y.mean())**2))
    r2 = float(1 - np.sum((y - predicted_y)**2) / total) if total > 0 else float("nan")
    length = (2 * np.pi / slope if model == "cauchy" else
              (2 * np.pi if formula_version == "pauw_2010" else 1) / np.sqrt(slope))
    misorientation = intercept if model == "cauchy" else np.sqrt(intercept)
    slope_stderr = float(regression.stderr) if np.isfinite(regression.stderr) else None
    intercept_stderr = (float(regression.intercept_stderr)
                        if np.isfinite(regression.intercept_stderr) else None)
    length_stderr = (None if slope_stderr is None else float(
        abs(length / slope) * slope_stderr * (1 if model == "cauchy" else .5)))
    angle_stderr = (None if intercept_stderr is None or (model == "gaussian" and intercept <= 0)
                    else float(intercept_stderr if model == "cauchy" else
                               intercept_stderr / (2 * np.sqrt(intercept))))
    return RulandResult(model, formula_version, width_definition, q, width, x, y, predicted_width,
        width - predicted_width, float(length), float(misorientation), slope,
        intercept, r2, slope_stderr, intercept_stderr, length_stderr, angle_stderr,
        ("candidate result; verify B width convention and q selection",
         "standard errors assume unweighted linear regression and fixed peak widths"))


def check_ruland_range_sensitivity(result: RulandResult):
    """Compare the selected q points with boundary points removed one at a time.

    This is a diagnostic only: no range is selected automatically.
    """
    order = np.argsort(result.q_nm_inv)
    q, widths = result.q_nm_inv[order], result.width_rad[order]
    checks = []
    for label, index in (("selected", slice(None)), ("without_lowest_q", slice(1, None)),
                         ("without_highest_q", slice(None, -1)),
                         ("without_both_ends", slice(1, -1))):
        qq, ww = q[index], widths[index]
        if not len(qq):
            continue
        try:
            fit = fit_ruland(qq, ww, model=result.model, width_definition=result.width_definition,
                             q_unit="nm^-1", width_unit="rad", formula_version=result.formula_version)
            error = None
        except ValueError as exc:
            fit, error = None, str(exc)
        checks.append(RulandRangeCheck(label, float(qq.min()), float(qq.max()), len(qq), fit, error))
    return tuple(checks)
