"""Sort open board cards into "local" or "needs-code". Pure heuristics, no model.

needs-code: the work is code, a service, or a config change. Anything the rule
cannot place confidently goes to needs-code, because a wrong "local" would let a
small model attempt real code; a wrong "needs-code" only costs the operator a paste.
"""

from __future__ import annotations

import re

LOCAL = "local"
NEEDS_CODE = "needs-code"

_CODE = re.compile(
    r"\b(code|script|patch(?:es)?|branch|merge|commit|refactor|implement\w*|bug|fix(?:es)?|"
    r"endpoint|api|firmware|service|systemd|timer|unit|restart|deploy\w*|install\w*|"
    r"config\w*|schema|database|sqlite|migration|pytest|tests?|build|lora|gguf|train\w*|"
    r"apply|apply\w*|wire[sd]?|hook|cron|\.py|\.service|\.timer|\.yaml|\.json)\b",
    re.IGNORECASE,
)
_LOCAL = re.compile(
    r"\b(note|notes|draft|drafts|summar\w+|digest|rollup|recap|outline|brief|list|review|"
    r"checklist|write-?up|plan|research|report|newsletter|post|caption|email)\b",
    re.IGNORECASE,
)


def classify(card: dict) -> tuple[str, str]:
    """Return (bucket, reason)."""
    text = " ".join(str(card.get(k, "")) for k in ("title", "proof"))
    code_hits = sorted({m.group(0).lower() for m in _CODE.finditer(text)})
    local_hits = sorted({m.group(0).lower() for m in _LOCAL.finditer(str(card.get("title", "")))})
    if code_hits:
        return NEEDS_CODE, "code/service/config words: " + ", ".join(code_hits[:6])
    if local_hits:
        return LOCAL, "text-only words: " + ", ".join(local_hits[:6])
    return NEEDS_CODE, "unclear, defaulting to needs-code"


def classify_routed(card: dict, router=None) -> tuple[str, str, object | None]:
    """Like classify(), but a router (callable card -> Route) may pick the lane.

    The regex stays a hard floor: a needs-code card can never come back local. With no router
    this is exactly classify() plus None. Returns (bucket, reason, route).
    """
    bucket, reason = classify(card)
    if router is None:
        return bucket, reason, None
    route = router(card)
    if route.bucket == LOCAL and bucket == NEEDS_CODE:  # belt and braces; route_card already enforces it
        return NEEDS_CODE, reason, route
    return route.bucket, f"{route.lane} lane: {route.why}", route
