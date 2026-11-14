"""Weight digest checks, weight diffs, and trigger probes.

This is a defense layer you can copy and tweak. It does not prove a model
is safe. See docs/modelguard.md.
"""

from __future__ import annotations

from sovereign_m.modelguard.diff import diff_tensors
from sovereign_m.modelguard.lock import lock_paths
from sovereign_m.modelguard.verify import verify_lock

__all__ = ["diff_tensors", "lock_paths", "verify_lock"]
