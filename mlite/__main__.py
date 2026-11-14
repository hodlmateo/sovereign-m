"""python -m mlite (deprecated; use python -m sovereign_m)."""

import warnings

warnings.warn(
    "python -m mlite is deprecated; use python -m sovereign_m.",
    DeprecationWarning,
    stacklevel=1,
)

from sovereign_m.cli import main

raise SystemExit(main())
