"""Pre-route oversized prompts away from a short-context llama-server.

The estimator is character-length / 4 plus a small per-message overhead.
It is not a real tokenizer. A prompt dense with code can still be
underestimated; treat a context-overflow error from the primary server
as a second chance to use the fallback.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

DEFAULT_MAX_PROMPT_TOKENS = 15_000
PRIMARY = "primary"
FALLBACK = "fallback"


def prompt_token_estimate(
    messages: Sequence[Mapping[str, Any]] | None = None,
    prompt: str = "",
    system: str = "",
) -> int:
    """Return a conservative token estimate for a chat or a raw prompt."""
    if messages:
        text = "".join(str(item.get("content") or "") for item in messages)
        extra = 8 * max(len(messages), 1)
    else:
        text = f"{system}\n{prompt}"
        extra = 16
    return extra + (len(text) + 3) // 4


def should_use_fallback(
    messages: Sequence[Mapping[str, Any]] | None = None,
    prompt: str = "",
    system: str = "",
    max_prompt_tokens: int = DEFAULT_MAX_PROMPT_TOKENS,
) -> bool:
    """True when the estimate is at or above the pre-route threshold."""
    return prompt_token_estimate(messages, prompt, system) >= max_prompt_tokens


def choose_route(
    messages: Sequence[Mapping[str, Any]] | None = None,
    prompt: str = "",
    system: str = "",
    max_prompt_tokens: int = DEFAULT_MAX_PROMPT_TOKENS,
) -> str:
    """Return "primary" or "fallback" for a 16k-context llama-server lane.

    Call this before contacting the primary server. Prompts at or over
    ``max_prompt_tokens`` (15,000 by default) go straight to the
    fallback so they never overflow the 16k context window first.
    """
    if should_use_fallback(messages, prompt, system, max_prompt_tokens):
        return FALLBACK
    return PRIMARY
