from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MIN_STARS = 20
MAX_STARS = 2000          # "under ~2k stars"
ACTIVE_DAYS = 90          # real activity: pushed within this many days
MAX_REPO_KB = 150_000     # do not clone bigger than ~150 MB
TOPIC_PATTERN = r"[a-z0-9][a-z0-9-]{0,49}"
FIT_MODULES = ("router", "memory", "modelguard", "selftest", "gateway", "social loop")


@dataclass(frozen=True)
class Settings:
    min_stars: int = MIN_STARS
    max_stars: int = MAX_STARS
    active_days: int = ACTIVE_DAYS
    max_repo_kb: int = MAX_REPO_KB


def hub_root() -> Path:
    return Path(os.environ.get("M_HUB", "") or Path.home() / "m_hub")


def scout_dir(hub: Path | None = None) -> Path:
    return (hub or hub_root()) / "scout" / "github"


def default_topics_file() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "data" / "ghscout" / "topics.txt").is_file():
            return p / "data" / "ghscout" / "topics.txt"
    return here.parent / "topics.txt"


def load_topics(cli_topics: list[str] | None, hub: Path | None = None, file: Path | None = None) -> list[str]:
    """--topic wins, then a topics file given with --topics-file, then the hub scout folder, then the repo default."""
    import re
    if cli_topics:
        raw = cli_topics
    else:
        candidates = [file] if file else [scout_dir(hub) / "topics.txt", default_topics_file()]
        raw = []
        for c in candidates:
            if c and c.is_file():
                raw = [l.strip() for l in c.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#")]
                break
    return [t.lower() for t in raw if re.fullmatch(TOPIC_PATTERN, t.lower())]
