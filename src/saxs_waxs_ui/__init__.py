"""Desktop user-interface prototype for the SAXS/WAXS workbench.

The public names are loaded lazily so ``python -m saxs_waxs_ui.app`` can start
without importing the application module twice.
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "EmbeddedViewerPane",
    "FolderBrowserWidget",
    "ImageViewerWidget",
    "LinkedViewerController",
    "MainWindow",
    "create_application",
]


def __getattr__(name: str):
    """Load public UI objects only when callers actually request them."""

    if name not in __all__:
        raise AttributeError(name)
    return getattr(import_module(".app", __name__), name)

