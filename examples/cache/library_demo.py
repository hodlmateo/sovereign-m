"""Use the cache as a library, with no server and no files left behind.

    python examples/cache/library_demo.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sovereign_m.cache import ResponseCache, make_key
from sovereign_m.cache.policy import should_cache

REQUEST = {
    "model": "demo",
    "temperature": 0,
    "messages": [{"role": "user", "content": "Explain what a hash map is."}],
}


def fake_model(_request: dict) -> bytes:
    print("  (model called)")
    return b'{"reply": "a hash map stores values by key"}'


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        with ResponseCache(Path(tmp) / "demo.sqlite", ttl_seconds=60) as cache:
            ok, reason = should_cache(REQUEST, headers={})
            print("cacheable:", ok, reason)
            key = make_key(REQUEST, route="demo")
            for attempt in (1, 2):
                print(f"call {attempt}")
                hit = cache.get(key)
                body = hit[0] if hit else fake_model(REQUEST)
                if hit is None:
                    cache.put(key, body)
            print(cache.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
