"""cache: a local response cache for model calls.

Identical, low-temperature, non-personal requests are answered from a local
sqlite file instead of calling the model again. Standard library only.
"""

from sovereign_m.cache.store import ResponseCache, make_key, normalize_text

__all__ = ["ResponseCache", "make_key", "normalize_text", "policy", "middleware"]
