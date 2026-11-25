"""Dependency-neutral color stops shared by GUI, thumbnails and later export."""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
import sys


STATIC_PALETTES: dict[str, tuple[str, ...]] = {
    "gray": ("#000000", "#FFFFFF"),
    "hot": ("#000000", "#B40000", "#FFB000", "#FFFF66", "#FFFFFF"),
    "jet": ("#000080", "#0000FF", "#00FFFF", "#FFFF00", "#FF0000", "#800000"),
    "blue_yellow_red": ("#071A52", "#0066CC", "#00C8FF", "#FFF200", "#FF5A00", "#7A0000"),
    "coolwarm": ("#3B4CC0", "#8DB0FE", "#F7F7F7", "#F49A7B", "#B40426"),
    "RdBu": ("#67001F", "#D6604D", "#F7F7F7", "#4393C3", "#053061"),
}

DEFAULT_CUSTOM_COLORS = ("#000000", "#2447A8", "#00B8D9", "#FFE600", "#D7191C")

COLORCET_RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "resources" / "colormaps" / "colorcet"


@lru_cache(maxsize=32)
def load_colorcet_rgb(name: str) -> tuple[tuple[float, float, float], ...]:
    """Load an attributed, hash-audited selected ColorCET table."""

    if not name.startswith("CET-") or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for character in name
    ):
        raise ValueError(f"Invalid ColorCET resource name: {name}")
    path = COLORCET_RESOURCE_DIR / f"{name}.csv"
    if not path.is_file():
        raise ValueError(f"ColorCET resource is not bundled: {name}")
    rows: list[tuple[float, float, float]] = []
    with path.open("r", encoding="ascii", newline="") as stream:
        for row in csv.reader(stream):
            if len(row) != 3:
                raise ValueError(f"Malformed ColorCET row in {path.name}: {row!r}")
            rgb = tuple(float(value) for value in row)
            if any(value < 0.0 or value > 1.0 for value in rgb):
                raise ValueError(f"Out-of-range ColorCET value in {path.name}: {rgb!r}")
            rows.append(rgb)
    if len(rows) < 2:
        raise ValueError(f"ColorCET table has too few rows: {path.name}")
    return tuple(rows)
