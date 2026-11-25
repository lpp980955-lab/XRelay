"""Generate auditable multi-q azimuthal curves from an already prepared image."""
from dataclasses import dataclass, replace
import numpy as np
from .integration import AzimuthalIntegrationRecipe, RadialUnit, integrate_azimuthal_1d

DEFAULT_RULAND_Q_POINTS = 24


def ruland_q_grid(start, stop, step, *, max_points=1000):
    """Inclusive uniform q grid; never distort the requested step to hit stop."""
    start, stop, step = map(float, (start, stop, step))
    if not np.isfinite((start, stop, step)).all() or not 0 < start < stop or step <= 0:
        raise ValueError("q起点、终点和步长必须有效且为正数，终点大于起点")
    ratio = (stop-start)/step
    count = int(np.floor(ratio + 1e-8)) + 1
    if count < 3 or count > max_points:
        raise ValueError(f"q步长需生成3至{max_points}个点；当前为{count}个")
    return start + np.arange(count)*step


@dataclass(frozen=True)
class RulandRing:
    center_q: float
    radial_range: tuple[float, float]
    curve: object | None
    error: str | None


def suggest_ruland_sampling(q_map, excluded=None, *, preferred=None,
                            n_points=DEFAULT_RULAND_Q_POINTS, intensity=None):
    """Suggest a visible q window, without claiming a valid Ruland fit interval.

    The optional preferred interval comes from the current integration controls.
    An interval with too few valid detector pixels is not used as a starting
    range. This only initializes editable controls; scientific q selection
    remains explicit.
    """
    q = np.asarray(q_map, dtype=float)
    if q.ndim != 2 or q.size == 0 or n_points < 3:
        raise ValueError("a two-dimensional q map and at least three points are required")
    mask = np.zeros(q.shape, dtype=bool) if excluded is None else np.asarray(excluded, dtype=bool)
    if mask.shape != q.shape:
        raise ValueError("mask shape differs from q map")
    usable = q[np.isfinite(q) & (q > 0) & ~mask]
    if usable.size < max(100, n_points * 10):
        raise ValueError("too few valid detector pixels to suggest q sampling")
    low, high = map(float, np.quantile(usable, (.02, .90)))
    source = "detector_valid_pixels_2_to_90_percentile"
    if preferred is not None:
        try:
            p_low, p_high = map(float, preferred)
        except (TypeError, ValueError) as exc:
            raise ValueError("preferred q interval must contain two finite values") from exc
        if not np.isfinite((p_low, p_high)).all() or p_low <= 0 or p_low >= p_high:
            raise ValueError("preferred q interval must be positive and increasing")
        if np.count_nonzero((usable >= p_low) & (usable <= p_high)) >= max(240, n_points * 20):
            low, high = p_low, p_high
            source = "integration_panel_with_detector_support"
        else:
            source = "detector_valid_pixels_2_to_90_percentile_preferred_unsupported"
    if not np.isfinite((low, high)).all() or low <= 0 or low >= high:
        raise ValueError("detector does not provide a usable positive q window")
    if intensity is not None and preferred is None:
        from scipy.ndimage import gaussian_filter1d

        values = np.asarray(intensity, dtype=float)
        if values.shape != q.shape:
            raise ValueError("intensity shape differs from q map")
        # Start at the first populated bins rather than dropping the low-q signal
        # with a pixel quantile, which weights large-radius rings more heavily.
        start = float(np.min(usable))
        good = ~mask & np.isfinite(values) & (q >= start) & (q <= high)
        counts, edges = np.histogram(q[good], bins=256, range=(start, high))
        totals, _ = np.histogram(q[good], bins=edges, weights=values[good])
        supported = counts >= 5
        if np.count_nonzero(supported) >= 20:
            profile = np.divide(totals, counts, out=np.zeros_like(totals), where=supported)
            weight = gaussian_filter1d(supported.astype(float), 1)
            smooth = gaussian_filter1d(profile, 1) / np.maximum(weight, 1e-12)
            tail = profile[supported][-max(10, np.count_nonzero(supported) // 5):]
            baseline = np.median(tail)
            noise = 1.4826 * np.median(np.abs(tail - baseline))
            contrast = max(0., np.max(smooth[supported]) - baseline)
            peak = int(np.argmax(np.where(supported, smooth, -np.inf)))
            if contrast > max(4 * noise, 0.) and peak + 5 <= np.flatnonzero(supported)[-1]:
                centers = (edges[:-1] + edges[1:]) / 2
                low = float(centers[peak])
                last_supported = int(np.flatnonzero(supported)[-1])
                plateau = last_supported
                tolerance = max(3 * noise, .002 * contrast)
                # Require a clear drop before testing for a sustained flat tail.
                for index in range(peak + 1, last_supported - 4):
                    window = smooth[index:index + 6]
                    if (supported[index:index + 6].all()
                            and window.max() < baseline + .1 * contrast
                            and np.ptp(window) <= tolerance):
                        plateau = index
                        break
                tail_start = float(centers[plateau])
                spacing_to_tail = (tail_start - low) / (max(21, n_points) - 4)
                high = min(float(centers[last_supported]), tail_start + 3 * spacing_to_tail)
                source = "radial_profile_peak_to_tail"
    spacing = (high - low) / (max(21, n_points) - 1)
    width = min(spacing * .8, low)
    return low, high, width, source


def integrate_ruland_rings(data, geometry, recipe: AzimuthalIntegrationRecipe, *,
                           q_centers, q_width, mask=None):
    """Apply the same geometry, prepared intensities and mask to each q ring.

    q_width is the total integration width in the recipe's q unit. The
    caller must prepare the background-subtracted (or explicitly raw) data.
    """
    if recipe.radial_unit not in (RadialUnit.Q_NM_INV, RadialUnit.Q_ANGSTROM_INV):
        raise ValueError("Ruland rings require a q radial unit")
    q = np.asarray(q_centers, dtype=float)
    width = float(q_width)
    if q.ndim != 1 or not q.size or not np.isfinite(q).all() or not np.isfinite(width) or width <= 0:
        raise ValueError("q centers must be a nonempty finite sequence and width must be positive")
    if np.any(q - width / 2 <= 0) or len(np.unique(q)) != len(q):
        raise ValueError("rings must have positive limits and distinct centers")
    rings = []
    for center in q:
        limits = (float(center - width / 2), float(center + width / 2))
        ring_recipe = replace(recipe, radial_range=limits)
        try:
            curve = integrate_azimuthal_1d(data, geometry, ring_recipe, mask=mask)
            rings.append(RulandRing(float(center), limits, curve, None))
        except ValueError as exc:
            rings.append(RulandRing(float(center), limits, None, str(exc)))
    return tuple(rings)
