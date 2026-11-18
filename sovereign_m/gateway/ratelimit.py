"""Token buckets: one per client, and optionally one per client and route."""

from __future__ import annotations

import math
import threading
import time
from typing import Callable


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], list[float]] = {}

    def check(self, client: str, scope: str, per_min: int | None) -> int:
        """Take a token. Returns 0 when allowed, else the seconds to wait.

        ``per_min`` of None or 0 means no limit. The burst equals ``per_min``.
        """
        if not per_min:
            return 0
        now = self._clock()
        rate = per_min / 60.0
        with self._lock:
            bucket = self._buckets.get((client, scope))
            if bucket is None or bucket[2] != per_min:
                bucket = [float(per_min), now, float(per_min)]
                self._buckets[(client, scope)] = bucket
            tokens = min(float(per_min), bucket[0] + (now - bucket[1]) * rate)
            bucket[1] = now
            if tokens >= 1.0:
                bucket[0] = tokens - 1.0
                return 0
            bucket[0] = tokens
            return max(1, math.ceil((1.0 - tokens) / rate))
