"""Candidate chain from image/background to multi-q Ruland analysis."""
from dataclasses import dataclass, replace
import numpy as np
from .background import apply_background
from .integration import RadialUnit, integrate_azimuthal_1d
from .geometry import geometry_to_document
from .masking import build_mask
from .ruland_integration import integrate_ruland_rings
from .ruland_series import fit_ruland_series, refit_ruland_series
from .peak_fitting import fit_peak
from .auto_azimuth import fit_automatic_azimuth, suggest_azimuth_peak_windows
from .readers.detector_io import frame_label


@dataclass(frozen=True)
class RulandWorkflowResult:
    rings: tuple
    series: object | None
    error: str | None
    background_audit: object
    mask_audit: object
    selected: tuple[bool, ...]
    geometry_audit: object
    sample_source: str
    peak_center_deg: float | None = None
    peak_center_source: str = "manual"
    peak_suggestion_q: float | None = None
    sample_label: str | None = None


@dataclass(frozen=True)
class RulandBatchItem:
    name: str
    result: RulandWorkflowResult | None
    error: str | None


def suggest_ruland_peak_centers(sample, air, geometry, background_recipe, mask_recipe,
                                integration_recipe, *, q_range, include_windows=False):
    """Rank visible azimuthal peak angles for one explicit q interval.

    Returned angles are interaction candidates only. No missing azimuths are
    filled, and this does not validate the Ruland model or selected q interval.
    """
    low, high = map(float, q_range)
    if not np.isfinite((low, high)).all() or low <= 0 or high <= low:
        raise ValueError("peak suggestion requires a positive increasing q interval")
    background = apply_background(sample, air, background_recipe)
    if background.audit.get("formula_version") == "air_background_equal_exposure_counts/1":
        integration_recipe = replace(integration_recipe, input_unit=background.output_unit)
    if background.output_unit != integration_recipe.input_unit:
        raise ValueError("integration input unit does not match background output unit")
    mask = build_mask(sample, mask_recipe)
    recipe = replace(integration_recipe, radial_range=(low, high))
    curve = integrate_azimuthal_1d(background.data, geometry, recipe, mask=mask.excluded)
    if include_windows:
        return suggest_azimuth_peak_windows(curve)
    return fit_automatic_azimuth(curve, count_only=True)


def run_ruland_workflow(sample, air, geometry, background_recipe, mask_recipe, integration_recipe,
                        *, q_centers, q_width, peak_center_deg, half_window_deg,
                        model, width_definition, selected=None, fit_q_range=None, reference_peak_center_deg=None):
    """Apply explicit shared recipes and retain all ring failures."""
    centers = tuple(q_centers)
    selected_array = np.ones(len(centers), dtype=bool) if selected is None else np.asarray(selected)
    if selected_array.shape != (len(centers),) or selected_array.dtype.kind != "b":
        raise ValueError("selected must be a boolean per q ring")
    background = apply_background(sample, air, background_recipe)
    if background.audit.get("formula_version") == "air_background_equal_exposure_counts/1":
        integration_recipe = replace(integration_recipe, input_unit=background.output_unit)
    if background.output_unit != integration_recipe.input_unit:
        raise ValueError("integration input unit does not match background output unit")
    mask = build_mask(sample, mask_recipe)
    rings = integrate_ruland_rings(background.data, geometry, integration_recipe,
                                   q_centers=centers, q_width=q_width, mask=mask.excluded)
    if fit_q_range is not None:
        low, high = fit_q_range
        if not np.isfinite((low, high)).all() or not 0 < low < high:
            raise ValueError("linear-fit q range must be positive and increasing")
        selected_array = np.array([low <= q <= high for q in centers], dtype=bool)
    selected_ring_failed = any(selected_array[i] and ring.error for i, ring in enumerate(rings))
    valid = [(i, ring) for i, ring in enumerate(rings) if ring.curve is not None]
    angle_source, suggestion_q = "manual", None
    if peak_center_deg is None:
        ranked = []
        for index, ring in valid:
            if not selected_array[index]:
                continue
            curve = ring.curve
            values = np.asarray(curve.intensity, dtype=float)
            counts = np.asarray(curve.count)
            finite = values[np.isfinite(values) & (counts > 0)]
            if finite.size >= 20:
                contrast = float(np.percentile(finite, 99) - np.median(finite))
                ranked.append((contrast, index, ring))
        for _contrast, _index, ring in sorted(ranked, key=lambda item: (-item[0], item[1])):
            try:
                curve = ring.curve
                candidates = fit_automatic_azimuth(curve, count_only=True)
                seed = candidates[0]
                if reference_peak_center_deg is not None:
                    def distance(angle):
                        return abs((angle - reference_peak_center_deg + 180) % 360 - 180)
                    seed = min(candidates, key=distance)
                    if distance(seed) >= 90:
                        continue
                local = (np.asarray(curve.x) - seed + 180) % 360 - 180
                values = np.where(np.asarray(curve.count) > 0, curve.intensity, np.nan)
                in_window = np.abs(local) <= half_window_deg
                if np.any(in_window & ~np.isfinite(values)):
                    continue
                fit = fit_peak(
                    local, values,
                    model="lorentzian_area" if model == "cauchy" else "gaussian_height",
                    background="constant", x_range=(-half_window_deg, half_window_deg),
                    x_unit="deg", y_unit=curve.intensity_unit)
                if not fit.metrics.get("success"):
                    continue
                peak_center_deg = float((seed + fit.parameters["center"].value) % 360)
                suggestion_q = ring.center_q
                angle_source = "auto_per_sample_visible_peak_candidate"
                break
            except (ValueError, IndexError):
                continue
        if peak_center_deg is None:
            return RulandWorkflowResult(rings, None, "no usable single-peak fit for automatic center; inspect the sample and fitting window",
                background.audit, mask.audit, tuple(map(bool, selected_array)),
                geometry_to_document(geometry), str(sample.metadata.source_path), None,
                "auto_per_sample_failed", sample_label=frame_label(sample))
    q_unit = {RadialUnit.Q_NM_INV: "nm^-1", RadialUnit.Q_ANGSTROM_INV: "A^-1"}[integration_recipe.radial_unit]
    series = fit_ruland_series(
        [(f"q={ring.center_q:g}", ring.center_q, ring.curve) for _, ring in valid],
        peak_center_deg=peak_center_deg, half_window_deg=half_window_deg,
        model=model, width_definition=width_definition, q_unit=q_unit,
        selected=np.asarray([selected_array[i] for i, _ in valid], dtype=bool))
    if fit_q_range is not None:
        usable = {item.q for item in series.slices if item.error is None}
        selected_array &= np.array([q in usable for q in centers], dtype=bool)
        series = refit_ruland_series(series, [
            item.q in usable and low <= item.q <= high for item in series.slices])
    elif selected_ring_failed:
        series = replace(series, result=None, error="selected ring integration failed")
    return RulandWorkflowResult(rings, series, series.error, background.audit,
                                mask.audit, tuple(map(bool, selected_array)),
                                geometry_to_document(geometry), str(sample.metadata.source_path),
                                peak_center_deg, angle_source, suggestion_q, sample_label=frame_label(sample))


def run_ruland_batch(samples, air, geometry, background_recipe, mask_recipe, integration_recipe,
                     *, progress=None, should_stop=None, **analysis_settings):
    """Process explicitly provided (name, frame) samples in order.

    No sample is silently dropped. A stop request takes effect between samples;
    remaining entries retain a not-processed status.
    """
    samples = tuple(samples)
    items = []
    stopped = False
    for index, (name, sample) in enumerate(samples):
        if stopped or (should_stop is not None and should_stop()):
            stopped = True
            items.append(RulandBatchItem(str(name), None, "not_processed: stopped"))
            continue
        try:
            result = run_ruland_workflow(sample, air, geometry, background_recipe, mask_recipe,
                                        integration_recipe, **analysis_settings)
            item = RulandBatchItem(str(name), result, result.error)
        except Exception as exc:
            item = RulandBatchItem(str(name), None, f"{type(exc).__name__}: {exc}")
        items.append(item)
        if progress is not None:
            progress(index + 1, len(samples), item)
    return tuple(items)
