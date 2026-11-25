"""Auditable guided calibrant refinement built on the official pyFAI workflow.

The workflow starts from a user-visible approximate geometry, extracts
sub-pixel control points near predicted calibrant rings, and refines only the
untilted distance and PONI coordinates.  It returns a *candidate* with complete
residual evidence; it never promotes the result to a validated calibration.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
from typing import Iterable, Sequence

import numpy as np

from .calibrants import Calibrant, ring_table
from .geometry import DetectorGeometry, GeometryError, GeometryProvenance
from .pyfai_adapter import PyFAIAdapterError, to_pyfai_geometry


class CalibrationError(GeometryError):
    """Guided calibration could not produce an auditable candidate."""


@dataclass(frozen=True, slots=True)
class ManualRingEstimate:
    """Zero-tilt centre/distance candidate from one assigned calibrant ring."""

    candidate_geometry: DetectorGeometry
    ring_ordinal: int | None
    d_angstrom: float | None
    point_count: int
    radius_m: float
    radial_rms_m: float


@dataclass(frozen=True, slots=True)
class CalibrationOptions:
    method: str = "massif"
    max_rings: int = 3
    points_per_degree: float = 0.2
    saturation_quantile: float = 0.9999
    mask_negative_for_peak_detection: bool = True
    max_center_shift_px: float = 25.0
    max_distance_fraction: float = 0.15
    max_iterations: int = 10_000
    minimum_ring_count: int = 2
    minimum_points_per_ring: int = 8
    random_seed: int = 1729
    coarse_center_enabled: bool = True

    def __post_init__(self) -> None:
        if self.method not in {"massif", "blob", "watershed"}:
            raise CalibrationError(f"unsupported pyFAI peak method: {self.method!r}")
        if self.max_rings < 2:
            raise CalibrationError("max_rings must be at least 2")
        if not math.isfinite(self.points_per_degree) or self.points_per_degree <= 0:
            raise CalibrationError("points_per_degree must be positive")
        if not 0.5 < self.saturation_quantile < 1.0:
            raise CalibrationError("saturation_quantile must be between 0.5 and 1")
        if self.max_center_shift_px <= 0 or not math.isfinite(self.max_center_shift_px):
            raise CalibrationError("max_center_shift_px must be positive")
        if not 0 < self.max_distance_fraction < 1:
            raise CalibrationError("max_distance_fraction must be between 0 and 1")
        if self.max_iterations <= 0:
            raise CalibrationError("max_iterations must be positive")
        if self.minimum_ring_count < 2 or self.minimum_ring_count > self.max_rings:
            raise CalibrationError("minimum_ring_count must be in [2, max_rings]")
        if self.minimum_points_per_ring < 3:
            raise CalibrationError("minimum_points_per_ring must be at least 3")
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool) or self.random_seed < 0:
            raise CalibrationError("random_seed must be a non-negative integer")
        if not isinstance(self.coarse_center_enabled, bool):
            raise CalibrationError("coarse_center_enabled must be boolean")


_PYFAI_RANDOM_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class RingResidual:
    ring_ordinal: int
    d_angstrom: float
    control_point_count: int
    mean_delta_two_theta_rad: float
    rms_delta_two_theta_rad: float
    max_abs_delta_two_theta_rad: float


@dataclass(frozen=True, slots=True)
class CalibrationRun:
    start_label: str
    start_geometry: DetectorGeometry
    candidate_geometry: DetectorGeometry
    control_points_y_x_ring: np.ndarray
    initial_rms_two_theta_rad: float
    final_rms_two_theta_rad: float
    ring_residuals: tuple[RingResidual, ...]
    warnings: tuple[str, ...]

    @property
    def ring_count(self) -> int:
        return len(self.ring_residuals)

    @property
    def control_point_count(self) -> int:
        return int(self.control_points_y_x_ring.shape[0])


@dataclass(frozen=True, slots=True)
class CoarseCenterEstimate:
    """Image-wide ring-correlation estimate used before local peak picking."""

    method: str
    center_x_px: float
    center_y_px: float
    input_shift_px: float
    score: float
    prominence_z: float
    ring_ordinals: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CalibrationSuggestion:
    calibrant_id: str
    selected_run_index: int
    runs: tuple[CalibrationRun, ...]
    saturation_threshold: float
    nonfinite_mask_count: int
    negative_mask_count: int
    saturated_mask_count: int
    warnings: tuple[str, ...]
    coarse_center: CoarseCenterEstimate | None = None

    @property
    def selected_run(self) -> CalibrationRun:
        return self.runs[self.selected_run_index]

    @property
    def candidate_geometry(self) -> DetectorGeometry:
        return self.selected_run.candidate_geometry

    @property
    def center_spread_px(self) -> float:
        centers = np.array(
            [
                [run.candidate_geometry.beam_center_x_px, run.candidate_geometry.beam_center_y_px]
                for run in self.runs
            ],
            dtype=np.float64,
        )
        return float(np.max(np.linalg.norm(centers - centers.mean(axis=0), axis=1)))

    @property
    def distance_spread_fraction(self) -> float:
        distances = np.array(
            [run.candidate_geometry.detector_distance_m for run in self.runs],
            dtype=np.float64,
        )
        return float(np.ptp(distances) / np.mean(distances))


def fit_manual_calibrant_ring(
    geometry: DetectorGeometry,
    calibrant: Calibrant | None,
    ring_ordinal: int,
    points_xy_px: Sequence[Sequence[float]],
) -> ManualRingEstimate:
    """Fit a ring; derive centre only when calibrant is None, otherwise also SDD.

    Points use the workbench canonical continuous detector coordinates.  The
    circle is fitted in detector-plane metres, so unequal X/Y pixel sizes do
    not silently turn a physical circle into a pixel-space ellipse.  This is a
    zero-tilt initializer, not a validated calibration; additional rings must
    be inspected or refined afterwards.
    """

    if not geometry.is_untilted:
        raise CalibrationError("manual ring fit currently requires zero detector tilt")
    points = np.asarray(points_xy_px, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or points.shape[0] < 3:
        raise CalibrationError("manual ring fit requires at least three (x, y) points")
    if not np.all(np.isfinite(points)):
        raise CalibrationError("manual ring points must be finite")
    if calibrant is not None and (ring_ordinal < 1 or ring_ordinal > len(calibrant.peaks)):
        raise CalibrationError("assigned calibrant ring is out of range")

    peak = None if calibrant is None else calibrant.peaks[ring_ordinal - 1]
    if peak is not None:
        bragg_argument = geometry.wavelength_m * 1e10 / (2.0 * peak.d_angstrom)
        if not 0.0 < bragg_argument <= 1.0:
            raise CalibrationError("assigned calibrant ring is not physical at this wavelength")

    x_m = points[:, 0] * geometry.pixel_size_x_m
    y_m = points[:, 1] * geometry.pixel_size_y_m
    design = np.column_stack((2.0 * x_m, 2.0 * y_m, np.ones(points.shape[0])))
    solution, _residuals, rank, singular_values = np.linalg.lstsq(
        design, x_m * x_m + y_m * y_m, rcond=None
    )
    if rank < 3 or singular_values[-1] <= np.finfo(np.float64).eps:
        raise CalibrationError("manual ring points are collinear or too close together")
    center_x_m, center_y_m, constant = (float(value) for value in solution)
    radius_squared = constant + center_x_m**2 + center_y_m**2
    if not math.isfinite(radius_squared) or radius_squared <= 0.0:
        raise CalibrationError("manual ring fit produced an invalid radius")
    radius_m = math.sqrt(radius_squared)
    fitted_radii = np.hypot(x_m - center_x_m, y_m - center_y_m)
    radial_rms_m = float(np.sqrt(np.mean((fitted_radii - radius_m) ** 2)))
    distance_m = geometry.detector_distance_m
    if peak is not None:
        two_theta = 2.0 * math.asin(bragg_argument)
        distance_m = radius_m / math.tan(two_theta)
    center_x_px = center_x_m / geometry.pixel_size_x_m
    center_y_px = center_y_m / geometry.pixel_size_y_m

    candidate = DetectorGeometry(
        wavelength_m=geometry.wavelength_m,
        detector_distance_m=distance_m,
        pixel_size_x_m=geometry.pixel_size_x_m,
        pixel_size_y_m=geometry.pixel_size_y_m,
        beam_center_x_px=center_x_px,
        beam_center_y_px=center_y_px,
        image_width_px=geometry.image_width_px,
        image_height_px=geometry.image_height_px,
        provenance=GeometryProvenance(
            method="manual_center_only_candidate" if calibrant is None else "manual_assigned_ring_candidate",
            source_application="XRelay",
            source_convention="top_left_edge_xy_right_down_pixel_center_plus_0p5",
            notes=("candidate_unvalidated; zero_tilt; supplied_distance_unchanged" if calibrant is None
                   else "candidate_unvalidated; zero_tilt; multi_ring_confirmation_required"),
        ),
        metadata={
            **dict(geometry.metadata),
            "status": "candidate_unvalidated",
            "calibrant_id": None if calibrant is None else calibrant.id,
            "preview_calibrant": "none" if calibrant is None else calibrant.id,
            "assigned_ring_ordinal": None if calibrant is None else ring_ordinal,
            "assigned_ring_d_angstrom": None if peak is None else peak.d_angstrom,
            "manual_point_count": int(points.shape[0]),
            "manual_ring_radial_rms_m": radial_rms_m,
        },
    )
    return ManualRingEstimate(
        candidate_geometry=candidate,
        ring_ordinal=None if calibrant is None else ring_ordinal,
        d_angstrom=None if peak is None else peak.d_angstrom,
        point_count=int(points.shape[0]),
        radius_m=radius_m,
        radial_rms_m=radial_rms_m,
    )
def _pyfai_calibrant(calibrant: Calibrant, wavelength_m: float):
    try:
        from pyFAI.calibrant import Calibrant as PyFAICalibrant
    except ImportError as exc:
        raise CalibrationError("pyFAI calibrant API is unavailable") from exc
    return PyFAICalibrant(
        dspacing=[peak.d_angstrom for peak in calibrant.peaks],
        wavelength=wavelength_m,
    )


def _validate_image(image: np.ndarray, geometry: DetectorGeometry) -> np.ndarray:
    source = np.asarray(image)
    if source.ndim != 2:
        raise CalibrationError("calibrant image must be two-dimensional")
    if geometry.image_height_px is not None and source.shape[0] != geometry.image_height_px:
        raise CalibrationError("image height differs from the candidate geometry")
    if geometry.image_width_px is not None and source.shape[1] != geometry.image_width_px:
        raise CalibrationError("image width differs from the candidate geometry")
    if not geometry.is_untilted:
        raise CalibrationError("guided Phase 2 refinement currently requires zero tilt")
    return source.astype(np.float64, copy=True)


def estimate_coarse_center(
    image: np.ndarray,
    initial: DetectorGeometry,
    calibrant: Calibrant,
    mask: np.ndarray,
    *,
    max_rings: int = 3,
) -> CoarseCenterEstimate:
    """Locate a coarse centre over the full image using known ring radii.

    pyFAI's automatic control-point extractor is deliberately local: it uses an
    already approximate geometry to seed each expected ring.  This deterministic
    correlation stage supplies that approximation without using the centre typed
    or dragged by the user.  The SDD, wavelength and pixel sizes still need to be
    plausible because they determine the ring radii used by the template.
    """

    data = _validate_image(image, initial)
    excluded = np.asarray(mask, dtype=bool)
    if excluded.shape != data.shape:
        raise CalibrationError("coarse-centre mask shape differs from image")
    valid = ~excluded
    if np.count_nonzero(valid) < 100:
        raise CalibrationError("too few valid pixels for full-image coarse centre")

    try:
        from scipy import ndimage, signal
    except ImportError as exc:
        raise CalibrationError("SciPy is unavailable for full-image coarse centre") from exc

    floor = float(np.quantile(data[valid], 0.10))
    transformed = np.log1p(np.maximum(data - floor, 0.0))
    transformed[excluded] = float(np.median(transformed[valid]))
    smooth = ndimage.gaussian_filter(transformed, 1.2, mode="nearest")
    highpass = np.maximum(smooth - ndimage.gaussian_filter(smooth, 15.0, mode="nearest"), 0.0)
    # Do not let edges of detector gaps, negative-value masks or saturated
    # regions impersonate a calibrant ring.
    exclusion_margin = ndimage.binary_dilation(excluded, iterations=8)
    highpass[exclusion_margin] = 0.0
    if not np.any(highpass > 0):
        raise CalibrationError("no positive ring features remain for coarse centre")

    height, width = data.shape
    maximum_radius = math.hypot(width, height)
    templates: list[tuple[int, float, float]] = []
    for ring in ring_table(calibrant, initial)[: max(0, int(max_rings))]:
        if min(ring.radius_x_px, ring.radius_y_px) < 5.0:
            continue
        if max(ring.radius_x_px, ring.radius_y_px) > maximum_radius:
            continue
        templates.append((ring.ordinal, ring.radius_x_px, ring.radius_y_px))
    if len(templates) < 2:
        raise CalibrationError("fewer than two usable ring templates for coarse centre")

    score_map = np.zeros_like(highpass, dtype=np.float64)
    used_ordinals: list[int] = []
    half_width_px = 3.0
    for ordinal, radius_x, radius_y in templates:
        half_x = int(math.ceil(radius_x + half_width_px + 1.0))
        half_y = int(math.ceil(radius_y + half_width_px + 1.0))
        yy, xx = np.mgrid[-half_y : half_y + 1, -half_x : half_x + 1]
        normalized_radius = np.hypot(xx / radius_x, yy / radius_y)
        radial_error_px = np.abs(normalized_radius - 1.0) * math.sqrt(radius_x * radius_y)
        kernel = (radial_error_px <= half_width_px).astype(np.float64)
        total = float(kernel.sum())
        if total <= 0:
            continue
        kernel /= total
        score_map += signal.fftconvolve(highpass, kernel, mode="same")
        used_ordinals.append(int(ordinal))
    if len(used_ordinals) < 2 or not np.all(np.isfinite(score_map)):
        raise CalibrationError("coarse-centre correlation did not produce a usable score map")

    peak_index = int(np.argmax(score_map))
    peak_y, peak_x = np.unravel_index(peak_index, score_map.shape)
    peak_score = float(score_map[peak_y, peak_x])
    median = float(np.median(score_map))
    mad = float(np.median(np.abs(score_map - median)))
    robust_scale = max(1e-15, 1.4826 * mad)
    center_x = float(peak_x) + 0.5
    center_y = float(peak_y) + 0.5
    return CoarseCenterEstimate(
        method="full_image_multiring_highpass_correlation_v1",
        center_x_px=center_x,
        center_y_px=center_y,
        input_shift_px=math.hypot(
            center_x - initial.beam_center_x_px,
            center_y - initial.beam_center_y_px,
        ),
        score=peak_score,
        prominence_z=(peak_score - median) / robust_scale,
        ring_ordinals=tuple(used_ordinals),
    )


def _residuals(
    backend,
    control_points: np.ndarray,
    calibrant,
    source_calibrant: Calibrant,
) -> tuple[float, tuple[RingResidual, ...]]:
    ring_index = control_points[:, 2].astype(np.int64)
    expected_table = np.asarray(calibrant.get_2th(), dtype=object)
    if ring_index.min() < 0 or ring_index.max() >= expected_table.size:
        raise CalibrationError("control point contains an invalid calibrant ring index")
    expected = np.array([float(expected_table[index]) for index in ring_index])
    calculated = np.asarray(
        backend.tth(control_points[:, 0], control_points[:, 1]), dtype=np.float64
    ).reshape(-1)
    delta = calculated - expected
    rows: list[RingResidual] = []
    for index in np.unique(ring_index):
        selected = delta[ring_index == index]
        rows.append(
            RingResidual(
                ring_ordinal=int(index) + 1,
                d_angstrom=source_calibrant.peaks[int(index)].d_angstrom,
                control_point_count=int(selected.size),
                mean_delta_two_theta_rad=float(np.mean(selected)),
                rms_delta_two_theta_rad=float(np.sqrt(np.mean(selected**2))),
                max_abs_delta_two_theta_rad=float(np.max(np.abs(selected))),
            )
        )
    return float(np.sqrt(np.mean(delta**2))), tuple(rows)


def refine_untilted_control_points(
    initial: DetectorGeometry,
    calibrant: Calibrant,
    control_points_y_x_ring: np.ndarray,
    *,
    options: CalibrationOptions = CalibrationOptions(),
    start_label: str = "initial",
) -> CalibrationRun:
    """Refine distance and centre from assigned pyFAI-format control points."""

    if not initial.is_untilted:
        raise CalibrationError("control-point refinement currently requires zero tilt")
    points = np.asarray(control_points_y_x_ring, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3 or points.shape[0] == 0:
        raise CalibrationError("control points must have shape (n, 3): y, x, ring_index")
    if np.any(~np.isfinite(points)):
        raise CalibrationError("control points must be finite")
    ring_indices, ring_counts = np.unique(points[:, 2].astype(np.int64), return_counts=True)
    if ring_indices.size < options.minimum_ring_count:
        raise CalibrationError(
            f"only {ring_indices.size} rings extracted; need {options.minimum_ring_count}"
        )
    sparse = [
        (int(index), int(count))
        for index, count in zip(ring_indices, ring_counts)
        if count < options.minimum_points_per_ring
    ]
    usable_ring_indices = ring_indices[
        ring_counts >= options.minimum_points_per_ring
    ]
    if usable_ring_indices.size < options.minimum_ring_count:
        sparse_text = ", ".join(
            f"{index + 1}:{count}" for index, count in sparse
        )
        raise CalibrationError(
            f"only {usable_ring_indices.size} rings have at least "
            f"{options.minimum_points_per_ring} control points; need "
            f"{options.minimum_ring_count}"
            + (f"; sparse ring(s) {sparse_text}" if sparse_text else "")
        )
    if sparse:
        points = points[
            np.isin(points[:, 2].astype(np.int64), usable_ring_indices)
        ]

    try:
        from pyFAI.geometryRefinement import GeometryRefinement
    except ImportError as exc:
        raise CalibrationError("pyFAI GeometryRefinement API is unavailable") from exc
    py_calibrant = _pyfai_calibrant(calibrant, initial.wavelength_m)
    initial_backend = to_pyfai_geometry(initial)
    refinement = GeometryRefinement(
        data=points,
        calibrant=py_calibrant,
        dist=initial_backend.dist,
        poni1=initial_backend.poni1,
        poni2=initial_backend.poni2,
        rot1=0.0,
        rot2=0.0,
        rot3=0.0,
        detector=initial_backend.detector,
        wavelength=initial.wavelength_m,
    )
    center_bound = options.max_center_shift_px
    refinement.dist_min = initial.detector_distance_m * (1.0 - options.max_distance_fraction)
    refinement.dist_max = initial.detector_distance_m * (1.0 + options.max_distance_fraction)
    refinement.poni1_min = max(
        0.0,
        (initial.beam_center_y_px - center_bound) * initial.pixel_size_y_m,
    )
    refinement.poni1_max = (
        initial.beam_center_y_px + center_bound
    ) * initial.pixel_size_y_m
    refinement.poni2_min = max(
        0.0,
        (initial.beam_center_x_px - center_bound) * initial.pixel_size_x_m,
    )
    refinement.poni2_max = (
        initial.beam_center_x_px + center_bound
    ) * initial.pixel_size_x_m

    initial_rms, _ = _residuals(
        initial_backend, points, py_calibrant, calibrant
    )
    refinement.refine2(
        options.max_iterations,
        fix=("wavelength", "rot1", "rot2", "rot3"),
    )
    candidate = DetectorGeometry(
        wavelength_m=initial.wavelength_m,
        detector_distance_m=float(refinement.dist),
        pixel_size_x_m=initial.pixel_size_x_m,
        pixel_size_y_m=initial.pixel_size_y_m,
        beam_center_x_px=float(refinement.poni2 / initial.pixel_size_x_m),
        beam_center_y_px=float(refinement.poni1 / initial.pixel_size_y_m),
        image_width_px=initial.image_width_px,
        image_height_px=initial.image_height_px,
        provenance=GeometryProvenance(
            method="pyfai_guided_untilted_candidate",
            source_application="XRelay + pyFAI",
            source_convention="top_left_edge_xy_right_down_pixel_center_plus_0p5",
            notes="candidate_unvalidated; user_overlay_confirmation_required",
        ),
        metadata={
            **dict(initial.metadata),
            "status": "candidate_unvalidated",
            "calibrant_id": calibrant.id,
            "pyfai_fixed_parameters": ["wavelength", "rot1", "rot2", "rot3"],
            "start_label": start_label,
            "peak_picker_random_seed": options.random_seed,
        },
    )
    final_rms, ring_residuals = _residuals(
        refinement, points, py_calibrant, calibrant
    )
    warnings: list[str] = ["candidate_requires_visual_ring_confirmation"]
    if sparse:
        warnings.append(
            "sparse_rings_excluded:"
            + ",".join(f"{index + 1}:{count}" for index, count in sparse)
        )
    if final_rms >= initial_rms:
        warnings.append("refinement_did_not_reduce_rms")
    if len(ring_residuals) < options.max_rings:
        warnings.append("fewer_rings_extracted_than_requested")
    return CalibrationRun(
        start_label=start_label,
        start_geometry=initial,
        candidate_geometry=candidate,
        control_points_y_x_ring=np.array(points, copy=True),
        initial_rms_two_theta_rad=initial_rms,
        final_rms_two_theta_rad=final_rms,
        ring_residuals=ring_residuals,
        warnings=tuple(warnings),
    )


def extract_control_points(
    image: np.ndarray,
    initial: DetectorGeometry,
    calibrant: Calibrant,
    mask: np.ndarray,
    *,
    options: CalibrationOptions,
) -> np.ndarray:
    """Use pyFAI's maintained ring-guided sub-pixel extraction workflow."""

    try:
        from pyFAI.gui.cli_calibration import AbstractCalibration
    except ImportError as exc:
        raise CalibrationError("pyFAI automatic control-point extraction is unavailable") from exc
    backend = to_pyfai_geometry(initial)
    # AbstractCalibration combines the caller mask with detector.mask; the
    # generic detector has no intrinsic mask, so make that explicit.
    backend.detector.mask = np.zeros(image.shape, dtype=np.int8)
    py_calibrant = _pyfai_calibrant(calibrant, initial.wavelength_m)
    calibration = AbstractCalibration(
        img=image,
        mask=np.asarray(mask, dtype=bool),
        detector=backend.detector,
        wavelength=initial.wavelength_m,
        calibrant=py_calibrant,
    )
    calibration.ai = backend
    calibration.gui = False
    calibration.interactive = False
    calibration.max_rings = options.max_rings
    calibration.preprocess()
    # pyFAI's massif picker shuffles seed pixels through NumPy's legacy global
    # RNG.  Preserve the caller's RNG state and serialize this narrow section
    # so a saved recipe reproduces the same control-point set.
    with _PYFAI_RANDOM_LOCK:
        random_state = np.random.get_state()
        np.random.seed(options.random_seed)
        try:
            calibration.extract_cpt(
                method=options.method,
                pts_per_deg=options.points_per_degree,
                max_rings=options.max_rings,
            )
        finally:
            np.random.set_state(random_state)
    points = np.asarray(calibration.data, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3 or points.shape[0] == 0:
        raise CalibrationError("pyFAI did not extract usable control points")
    return points


def _perturbed_geometry(
    initial: DetectorGeometry,
    *,
    dx_px: float,
    dy_px: float,
    distance_scale: float,
    label: str,
) -> DetectorGeometry:
    return DetectorGeometry(
        wavelength_m=initial.wavelength_m,
        detector_distance_m=initial.detector_distance_m * distance_scale,
        pixel_size_x_m=initial.pixel_size_x_m,
        pixel_size_y_m=initial.pixel_size_y_m,
        beam_center_x_px=initial.beam_center_x_px + dx_px,
        beam_center_y_px=initial.beam_center_y_px + dy_px,
        image_width_px=initial.image_width_px,
        image_height_px=initial.image_height_px,
        provenance=GeometryProvenance(
            method="automatic_multistart_probe",
            source_application="XRelay",
            notes=label,
        ),
        metadata=dict(initial.metadata),
    )


def suggest_untilted_geometry(
    image: np.ndarray,
    initial: DetectorGeometry,
    calibrant: Calibrant,
    *,
    options: CalibrationOptions = CalibrationOptions(),
    start_variations: Sequence[tuple[str, float, float, float]] = (
        ("baseline", 0.0, 0.0, 1.0),
        ("probe_plus", 1.0, -1.0, 0.995),
        ("probe_minus", -1.0, 1.0, 1.005),
    ),
) -> CalibrationSuggestion:
    """Return a multi-start candidate and residual evidence, never an approval."""

    data = _validate_image(image, initial)
    nonfinite = ~np.isfinite(data)
    negative = data < 0
    eligible = ~nonfinite
    if options.mask_negative_for_peak_detection:
        eligible &= ~negative
    if not np.any(eligible):
        raise CalibrationError("no finite pixels remain for calibrant peak detection")
    threshold = float(np.quantile(data[eligible], options.saturation_quantile))
    saturated = data > threshold
    mask = nonfinite | saturated
    if options.mask_negative_for_peak_detection:
        mask |= negative

    coarse_center: CoarseCenterEstimate | None = None
    coarse_warning: str | None = None
    refinement_anchor = initial
    if options.coarse_center_enabled:
        try:
            coarse_center = estimate_coarse_center(
                data,
                initial,
                calibrant,
                mask,
                max_rings=options.max_rings,
            )
            refinement_anchor = DetectorGeometry(
                wavelength_m=initial.wavelength_m,
                detector_distance_m=initial.detector_distance_m,
                pixel_size_x_m=initial.pixel_size_x_m,
                pixel_size_y_m=initial.pixel_size_y_m,
                beam_center_x_px=coarse_center.center_x_px,
                beam_center_y_px=coarse_center.center_y_px,
                image_width_px=initial.image_width_px,
                image_height_px=initial.image_height_px,
                provenance=GeometryProvenance(
                    method=coarse_center.method,
                    source_application="XRelay",
                    source_convention="top_left_edge_xy_right_down_pixel_center_plus_0p5",
                    notes="image_wide_coarse_candidate_before_pyfai_guided_refinement",
                ),
                metadata={
                    **dict(initial.metadata),
                    "coarse_center_method": coarse_center.method,
                    "coarse_center_input_x_px": initial.beam_center_x_px,
                    "coarse_center_input_y_px": initial.beam_center_y_px,
                    "coarse_center_shift_px": coarse_center.input_shift_px,
                },
            )
        except CalibrationError as exc:
            coarse_warning = f"coarse_center_failed_fell_back_to_visible_candidate: {exc}"

    runs: list[CalibrationRun] = []
    failures: list[str] = []
    for label, dx_px, dy_px, distance_scale in start_variations:
        try:
            start = _perturbed_geometry(
                refinement_anchor,
                dx_px=dx_px,
                dy_px=dy_px,
                distance_scale=distance_scale,
                label=label,
            )
            points = extract_control_points(
                data,
                start,
                calibrant,
                mask,
                options=options,
            )
            # All runs share bounds centred on the image-wide coarse candidate,
            # while retaining their own extraction start.
            run = refine_untilted_control_points(
                refinement_anchor,
                calibrant,
                points,
                options=options,
                start_label=label,
            )
            runs.append(run)
        except (CalibrationError, PyFAIAdapterError, ValueError, RuntimeError) as exc:
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
    if not runs:
        raise CalibrationError("all guided calibration starts failed: " + " | ".join(failures))

    # Prefer a stable multi-start medoid, not the numerically lowest residual
    # alone.  First retain the best ring coverage, then select the run closest
    # to the median centre/distance of that group.  Residual and point count are
    # only tie-breakers.  All runs remain available for inspection.
    maximum_ring_count = max(run.ring_count for run in runs)
    supported = [
        index for index, run in enumerate(runs) if run.ring_count == maximum_ring_count
    ]
    median_x = float(
        np.median([runs[index].candidate_geometry.beam_center_x_px for index in supported])
    )
    median_y = float(
        np.median([runs[index].candidate_geometry.beam_center_y_px for index in supported])
    )
    median_distance = float(
        np.median([runs[index].candidate_geometry.detector_distance_m for index in supported])
    )

    def stability_key(index: int) -> tuple[float, float, int]:
        candidate = runs[index].candidate_geometry
        center_term = math.hypot(
            candidate.beam_center_x_px - median_x,
            candidate.beam_center_y_px - median_y,
        ) / options.max_center_shift_px
        distance_term = abs(candidate.detector_distance_m - median_distance) / (
            refinement_anchor.detector_distance_m * options.max_distance_fraction
        )
        return (
            center_term**2 + distance_term**2,
            runs[index].final_rms_two_theta_rad,
            -runs[index].control_point_count,
        )

    selected_index = min(supported, key=stability_key)
    warnings = ["automatic_suggestion_is_not_a_validated_calibration"]
    if coarse_warning is not None:
        warnings.append(coarse_warning)
    warnings.extend(failures)
    if len(runs) < len(start_variations):
        warnings.append("one_or_more_multistart_probes_failed")
    return CalibrationSuggestion(
        calibrant_id=calibrant.id,
        selected_run_index=selected_index,
        runs=tuple(runs),
        saturation_threshold=threshold,
        nonfinite_mask_count=int(np.count_nonzero(nonfinite)),
        negative_mask_count=int(np.count_nonzero(negative))
        if options.mask_negative_for_peak_detection
        else 0,
        saturated_mask_count=int(np.count_nonzero(saturated)),
        warnings=tuple(warnings),
        coarse_center=coarse_center,
    )
