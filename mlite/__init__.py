"""Compatibility shim. The package was renamed to ``sovereign_m``.

``python -m mlite`` still works. Prefer ``python -m sovereign_m``.
"""

from __future__ import annotations

import warnings

warnings.warn(
    "The 'mlite' package was renamed to 'sovereign_m'. Use 'python -m sovereign_m'.",
    DeprecationWarning,
    stacklevel=2,
)

from sovereign_m.cli import console_main, main

__all__ = ["console_main", "main"]
