"""voice.md and visuals.md templates. Placeholders say UNVERIFIED until the operator fills them in."""

from __future__ import annotations

from pathlib import Path

from sovereign_m.social.brands import Brand, Paths
from sovereign_m.social.lint import NEVER_USE


def _never() -> str:
    return "\n".join(f"- {line}" for line in NEVER_USE)


def _channels(brand: Brand) -> str:
    rows = [f"- {c.label} {c.handle}: hashtags max {c.hashtag_max}" + (f", {c.max_chars} characters" if c.max_chars else "")
            for c in brand.channels]
    return "\n".join(rows)


def voice_md(brand: Brand) -> str:
    kid = ("\n- Audience includes children and families: kid-safe, no student names or data, no private details.\n"
           if brand.kid_safe else "\n")
    return f"""# {brand.name} voice (TEMPLATE: the operator fills the UNVERIFIED parts)

Status: template. Nothing below is confirmed brand voice until the UNVERIFIED markers are gone.
The Analyst may suggest edits to this file in its weekly note. It never changes this file.

## Who is speaking
- Speaker: UNVERIFIED (for example, first person, the operator, concrete)
- Reader we write for: UNVERIFIED
- One sentence on what this brand is for: UNVERIFIED

## Tone
- Three words that describe the tone: UNVERIFIED, UNVERIFIED, UNVERIFIED
- Reading level and sentence length: UNVERIFIED
- Humor allowed: UNVERIFIED

## Words and phrases we use
- UNVERIFIED (list 5 to 10 real phrases from posts the operator liked)

## Words and phrases we do not use
- UNVERIFIED (add brand-specific words here; the never-use list below always applies)

## Never-use list (always on)
{_never()}

## Channels (hard rule)
{_channels(brand)}{kid}
## Examples
- A post the operator thinks sounds right: UNVERIFIED (paste the public link, not the text from memory)
- A post that sounds wrong: UNVERIFIED
"""


def visuals_md(brand: Brand) -> str:
    return f"""# {brand.name} visuals (TEMPLATE: the operator fills the UNVERIFIED parts)

Status: template. Nothing below is confirmed until the UNVERIFIED markers are gone.

## Look
- Main colors (hex): UNVERIFIED
- Accent color (hex): UNVERIFIED
- Fonts: UNVERIFIED
- Logo file and where it goes: UNVERIFIED
- Photo or illustration style: UNVERIFIED

## Formats
- Vertical video and cards: 1080 x 1920 (from the existing Studio spec)
- Other sizes per channel: UNVERIFIED

## Rules for every image or frame
- Blur or crop anything private: passwords, codes, addresses, phone numbers, private emails, IPs, hostnames, file paths.
- Mail screens: blur senders and subjects.
- No emoji, no arrows drawn as text, no long dashes in on-screen text.
- Every on-screen number or claim needs a source, or it is cut.

## Never-use list (always on)
{_never()}
"""


def init_brand(brand: Brand, paths: Paths | None = None) -> dict:
    """Write voice.md and visuals.md if they do not exist. Existing files are never touched."""
    paths = paths or Paths(brand)
    paths.brand_dir.mkdir(parents=True, exist_ok=True)
    out = {"written": [], "skipped": []}
    for name, body in (("voice.md", voice_md(brand)), ("visuals.md", visuals_md(brand))):
        target = paths.brand_dir / name
        if target.exists() or target.is_symlink():
            out["skipped"].append(str(target))
            continue
        import os
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        out["written"].append(str(target))
    return out
