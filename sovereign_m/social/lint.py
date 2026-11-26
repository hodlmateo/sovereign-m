"""The never-use list, as code. Sanitize what can be fixed mechanically, flag the rest."""

from __future__ import annotations

import re

from sovereign_m.social.brands import Channel

HASHTAG = re.compile(r"(?<![\w&])#[A-Za-z0-9_]+")
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
ARROW = re.compile("[←-⇿⟰-⟿⤀-⥿]|->|=>|<-")
LONG_DASH = re.compile("[–—―]|(?<!-)--(?!-)")
HYPE = (
    "game-changer", "game changer", "game-changing", "game changing", "revolutionary", "revolutionize",
    "groundbreaking", "cutting-edge", "cutting edge", "unleash", "supercharge", "skyrocket", "mind-blowing",
    "insane", "you won't believe", "must-see", "must see", "next level", "next-level", "10x", "crushing it",
    "unlock the power", "secret sauce", "blazing fast", "effortless", "seamless", "ultimate", "life-changing",
    "disrupt", "guaranteed", "best ever", "the future of", "like never before", "changes everything",
)
CLAIM = re.compile(
    r"\d+(?:\.\d+)?\s?%|\b\d{2,}x\b|\b(?:studies|research|experts|data)\s+(?:show|shows|say|says|prove|proves)\b"
    r"|\b(?:proven|fastest|cheapest|#1|number one|first ever|world's first)\b", re.IGNORECASE)

NEVER_USE = (
    "No hashtags on X. (Institute Facebook, Instagram and WhatsApp may keep 2-3; TikTok and YouTube Shorts at most 2.)",
    "No hype phrases (game-changer, revolutionary, supercharge, unleash, next level, and the like).",
    "No emoji, no arrows, no long dashes. Use a plain hyphen, comma or full stop.",
    "No unsourced claims: every number, ranking or 'studies show' needs a source listed in sources, or it is cut.",
    "Nothing private: no passwords, codes, addresses, phone numbers, private emails, IPs, hostnames or file paths.",
)


def sanitize(text: str, ch: Channel) -> str:
    """Mechanical fixes only: hashtags beyond the channel limit, emoji, arrows, long dashes."""
    tags = HASHTAG.findall(text)
    if tags:
        keep = set(tags[: ch.hashtag_max])
        seen: list[str] = []

        def drop(m: re.Match) -> str:
            if m.group(0) in keep and m.group(0) not in seen:
                seen.append(m.group(0))
                return m.group(0)
            return ""
        text = HASHTAG.sub(drop, text)
    text = EMOJI.sub("", text)
    text = ARROW.sub(" to ", text)
    text = LONG_DASH.sub(", ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r",\s*,", ",", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def lint(text: str, ch: Channel, has_sources: bool = False) -> list[str]:
    """Violations left after (or without) sanitizing. Empty list means clean."""
    out: list[str] = []
    n = len(HASHTAG.findall(text))
    if n > ch.hashtag_max:
        out.append(f"{n} hashtags on {ch.label} (max {ch.hashtag_max})")
    if EMOJI.search(text):
        out.append("emoji")
    if ARROW.search(text):
        out.append("arrow")
    if LONG_DASH.search(text):
        out.append("long dash")
    low = text.lower()
    for phrase in HYPE:
        if phrase in low:
            out.append(f"hype phrase: {phrase}")
    if not has_sources:
        m = CLAIM.search(text)
        if m:
            out.append(f"unsourced claim: {m.group(0)!r}")
    if ch.max_chars and len(re.sub(r"https?://\S+", "x" * 23, text)) > ch.max_chars:
        out.append(f"over {ch.max_chars} characters on {ch.label}")
    return out


def clean(text: str, ch: Channel, has_sources: bool = False) -> tuple[str, list[str]]:
    fixed = sanitize(text, ch)
    return fixed, lint(fixed, ch, has_sources)
