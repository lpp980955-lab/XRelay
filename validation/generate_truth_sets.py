#!/usr/bin/env python3
"""Generate deterministic mathematical-truth datasets for validation.

The angular convention in this generator is deliberately local to the test
data (`generator_test_convention_v1`).  It does not freeze the application's
future user-facing azimuth convention.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np


SCHEMA = "saxs_waxs_mathematical_truth/1"
ANGLE_CONVENTION = {
    "id": "generator_test_convention_v1",
    "array": "image[row, column]",
    "origin": "specified subpixel center (column_x, row_y)",
    "zero_deg": "+column direction (right)",
    "positive": "counter-clockwise in a conventional Cartesian view; row decreases upward",
    "range": "[0, 360)",
    "application_ui_status": "not_frozen",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def geometry(shape: tuple[int, int], center_xy: tuple[float, float]):
    rows, columns = np.indices(shape, dtype=np.float64)
    cx, cy = center_xy
    dx = columns - cx
    dy = cy - rows
    radius = np.hypot(dx, dy)
    azimuth_deg = np.mod(np.degrees(np.arctan2(dy, dx)), 360.0)
    return radius, azimuth_deg


def wrapped_delta_deg(angle: np.ndarray, center: float) -> np.ndarray:
    return (angle - center + 180.0) % 360.0 - 180.0


def gaussian_area(x: np.ndarray, center: float, sigma: float, area: float) -> np.ndarray:
    amplitude = area / (sigma * np.sqrt(2.0 * np.pi))
    return amplitude * np.exp(-0.5 * ((x - center) / sigma) ** 2)


def build_truth_arrays() -> tuple[dict[str, np.ndarray], dict]:
    shape = (256, 272)
    center_xy = (131.25, 122.75)
    radius, azimuth = geometry(shape, center_xy)

    signed_int = np.zeros((16, 18), dtype=np.int32)
    signed_int[0, :5] = np.array([-2, -1, 0, 1, 2], dtype=np.int32)
    signed_int[5, 7] = np.iinfo(np.int32).max
    signed_int[9, 3] = np.iinfo(np.int32).min + 1

    signal = 8.0 + 0.02 * radius
    air_0p5s = 4.0 + 0.003 * radius
    sample_0p5s = signal + air_0p5s
    sample_1p0s = signal + 2.0 * air_0p5s

    # MT-013 is the exposure-linear physical truth introduced after D-104.
    # MT-002 is intentionally retained unchanged as a legacy algebraic truth.
    exposure_signal_rate = 12.0 + 0.025 * radius
    exposure_air_rate = 5.0 + 0.004 * radius
    exposure_air_counts_0p5s = exposure_air_rate * 0.5
    exposure_sample_counts_0p5s = (exposure_signal_rate + exposure_air_rate) * 0.5
    exposure_sample_counts_1p0s = exposure_signal_rate + exposure_air_rate

    ring_radius_px = 63.5
    ring_sigma_px = 2.25
    isotropic_ring = np.exp(-0.5 * ((radius - ring_radius_px) / ring_sigma_px) ** 2)

    arc_centers_deg = np.array([17.0, 90.0, 183.0, 350.0])
    arc_sigma_deg = 4.0
    radial_envelope = np.exp(-0.5 * ((radius - 78.0) / 3.0) ** 2)
    arbitrary_arcs = np.zeros(shape, dtype=np.float64)
    for index, center in enumerate(arc_centers_deg, start=1):
        angular = np.exp(-0.5 * (wrapped_delta_deg(azimuth, center) / arc_sigma_deg) ** 2)
        arbitrary_arcs += index * radial_envelope * angular

    beamstop_mask = radius <= 13.0
    module_gap_mask = (np.abs(np.indices(shape)[1] - 136) <= 2) | (
        np.abs(np.indices(shape)[0] - 128) <= 1
    )
    half_plane_mask = np.indices(shape)[1] < int(np.floor(center_xy[0]))
    combined_mask = beamstop_mask | module_gap_mask | half_plane_mask

    phi = np.linspace(0.0, np.pi / 2.0, 20001, dtype=np.float64)
    isotropic_orientation = np.ones_like(phi)
    aligned_orientation = np.exp(12.0 * np.cos(phi) ** 2)

    def herman(intensity: np.ndarray) -> tuple[float, float]:
        denominator = np.trapezoid(intensity * np.sin(phi), phi)
        moment = np.trapezoid(intensity * np.cos(phi) ** 2 * np.sin(phi), phi) / denominator
        return float(moment), float((3.0 * moment - 1.0) / 2.0)

    iso_moment, iso_herman = herman(isotropic_orientation)
    aligned_moment, aligned_herman = herman(aligned_orientation)

    q_nm_inv = np.linspace(0.08, 0.42, 18, dtype=np.float64)
    cauchy_lf_nm = 86.0
    cauchy_bpsi_rad = 0.041
    cauchy_bobs_rad = cauchy_bpsi_rad + 2.0 * np.pi / (cauchy_lf_nm * q_nm_inv)
    gaussian_lf_nm = 93.0
    gaussian_bpsi_rad = 0.036
    gaussian_bobs_rad = np.sqrt((1.0 / (gaussian_lf_nm * q_nm_inv)) ** 2 + gaussian_bpsi_rad**2)

    two_theta_deg = np.linspace(5.0, 40.0, 14001, dtype=np.float64)
    amorphous_area = 730.0
    crystalline_areas = np.array([280.0, 415.0, 205.0])
    waxs_amorphous = gaussian_area(two_theta_deg, 21.0, 4.3, amorphous_area)
    waxs_crystalline = (
        gaussian_area(two_theta_deg, 18.2, 0.42, crystalline_areas[0])
        + gaussian_area(two_theta_deg, 23.6, 0.55, crystalline_areas[1])
        + gaussian_area(two_theta_deg, 29.1, 0.68, crystalline_areas[2])
    )
    waxs_background = 2.5 + 0.025 * (two_theta_deg - 5.0)
    waxs_total = waxs_background + waxs_amorphous + waxs_crystalline
    crystallinity = float(crystalline_areas.sum() / (crystalline_areas.sum() + amorphous_area))

    arrays = {
        "mt001_signed_int32": signed_int,
        "mt002_signal": signal,
        "mt002_air_0p5s": air_0p5s,
        "mt002_sample_0p5s": sample_0p5s,
        "mt002_sample_1p0s": sample_1p0s,
        "mt013_signal_rate": exposure_signal_rate,
        "mt013_air_rate": exposure_air_rate,
        "mt013_air_counts_0p5s": exposure_air_counts_0p5s,
        "mt013_sample_counts_0p5s": exposure_sample_counts_0p5s,
        "mt013_sample_counts_1p0s": exposure_sample_counts_1p0s,
        "mt003_radius_px": radius,
        "mt003_azimuth_deg": azimuth,
        "mt003_isotropic_ring": isotropic_ring,
        "mt005_arbitrary_arcs": arbitrary_arcs,
        "mt006_beamstop_mask": beamstop_mask,
        "mt006_module_gap_mask": module_gap_mask,
        "mt006_half_plane_mask": half_plane_mask,
        "mt006_combined_mask": combined_mask,
        "mt009_phi_rad": phi,
        "mt009_isotropic_orientation": isotropic_orientation,
        "mt009_aligned_orientation": aligned_orientation,
        "mt010_q_nm_inv": q_nm_inv,
        "mt010_cauchy_bobs_rad": cauchy_bobs_rad,
        "mt011_gaussian_bobs_rad": gaussian_bobs_rad,
        "mt012_two_theta_deg": two_theta_deg,
        "mt012_waxs_background": waxs_background,
        "mt012_waxs_amorphous": waxs_amorphous,
        "mt012_waxs_crystalline": waxs_crystalline,
        "mt012_waxs_total": waxs_total,
    }
    expected = {
        "schema": SCHEMA,
        "generator": "validation/generate_truth_sets.py",
        "deterministic": True,
        "angle_convention": ANGLE_CONVENTION,
        "shape": list(shape),
        "center_xy_px": list(center_xy),
        "truth": {
            "mt001": {"dtype": "int32", "special_values": [-2, -1, 0, 1, 2]},
            "mt002": {
                "exposure_seconds": {"air": 0.5, "sample_equal": 0.5, "sample_double": 1.0},
                "equal_exposure_result": "sample_0p5s - air_0p5s == signal",
                "normalized_result": "sample_1p0s - 2*air_0p5s == signal",
            },
            "mt003": {"ring_radius_px": ring_radius_px, "ring_sigma_px": ring_sigma_px},
            "mt005": {
                "arc_centers_deg": arc_centers_deg.tolist(),
                "arc_sigma_deg": arc_sigma_deg,
                "radial_center_px": 78.0,
                "radial_sigma_px": 3.0,
            },
            "mt006": {
                "beamstop_radius_px": 13.0,
                "masked_pixel_count": int(combined_mask.sum()),
                "no_mirroring_allowed": True,
            },
            "mt009": {
                "isotropic_moment": iso_moment,
                "isotropic_herman": iso_herman,
                "aligned_moment": aligned_moment,
                "aligned_herman": aligned_herman,
                "integration": "numpy.trapezoid on 20001 points over [0, pi/2]",
            },
            "mt010": {"model": "Cauchy-Cauchy", "lf_nm": cauchy_lf_nm, "bpsi_rad": cauchy_bpsi_rad},
            "mt011": {"model": "Gaussian-Gaussian", "lf_nm": gaussian_lf_nm, "bpsi_rad": gaussian_bpsi_rad},
            "mt012": {
                "crystalline_areas": crystalline_areas.tolist(),
                "amorphous_area": amorphous_area,
                "crystallinity": crystallinity,
                "area_definition": "analytic Gaussian component areas; background excluded",
            },
            "mt013": {
                "formula_version": "air_background_rate/1",
                "exposure_seconds": {
                    "air": 0.5,
                    "sample_equal": 0.5,
                    "sample_double": 1.0,
                },
                "expected_rate_formula": "sample/t_sample - air/t_air == signal_rate",
                "legacy_mt002_preserved": True,
            },
        },
        "limitations": [
            "No detector noise, polarization, solid-angle or point-spread model in v1.",
            "Generator angle convention is a test fixture, not the frozen application UI convention.",
            "Analytic Gaussian areas differ slightly from finite sampled-range numerical integrals.",
        ],
    }
    return arrays, expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/validation/truth_v1"),
        help="Output directory (default: artifacts/validation/truth_v1)",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    arrays, expected = build_truth_arrays()
    npz_path = output / "truth_arrays.npz"
    json_path = output / "expected.json"
    np.savez_compressed(npz_path, **arrays)
    json_path.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema": "truth_artifact_manifest/1",
        "files": {
            npz_path.name: {"bytes": npz_path.stat().st_size, "sha256": sha256(npz_path)},
            json_path.name: {"bytes": json_path.stat().st_size, "sha256": sha256(json_path)},
        },
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), **manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

