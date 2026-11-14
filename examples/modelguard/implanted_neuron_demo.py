#!/usr/bin/env python3
"""Run the synthetic implanted-row demo.

This file is a thin wrapper so you can tweak the demo without opening the
package. It uses only the standard library plus this checkout.

    python examples/modelguard/implanted_neuron_demo.py
    python -m sovereign_m.modelguard demo
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sovereign_m.modelguard.demo import format_demo, run


def main() -> int:
    result = run()
    print(format_demo(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
