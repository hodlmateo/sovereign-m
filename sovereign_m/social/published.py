"""Append-only published.md per brand. Rows are added by CLI; a URL is never invented."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from sovereign_m.social.brands import Brand
from sovereign_m.social.safeio import append_line

HEADER = ("# Published log (append-only)\n\n"
          "One row per post or metrics reading. Rows are only ever added. Public metrics only.\n\n"
          "| date | kind | channel | url | detail |\n|---|---|---|---|---|\n")
_BAD_URL = re.compile(r"example\.(com|org)|todo|unverified|\.\.\.|<|>|\s", re.IGNORECASE)
NO_URL = "no public url"


class PublishedError(ValueError):
    pass


def _cell(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("|", "\\|")).strip()


def check_url(brand: Brand, channel_key: str, url: str | None) -> str:
    ch = brand.channel(channel_key)
    if ch is None:
        raise PublishedError(f"{channel_key} is not a channel for {brand.name} (allowed: {', '.join(brand.keys)})")
    if not url:
        if channel_key == "whatsapp":
            return NO_URL
        raise PublishedError("a real public URL is required; this tool never invents one")
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or _BAD_URL.search(url) or not any(host == d or host.endswith("." + d) for d in ch.domains):
        raise PublishedError(f"URL does not look like a real {ch.label} link ({'/'.join(ch.domains)}): {url}")
    if len(u.path.strip("/")) < 3 and not u.query:
        raise PublishedError("URL has no post path; paste the link to the post itself")
    return url


def read_log(path: Path) -> dict:
    """{'posts': [{date, channel, url, title}], 'metrics': {url: {metric: int}}} from the log."""
    posts, metrics = [], {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"posts": posts, "metrics": metrics}
    for line in text.splitlines():
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) != 5 or cells[1] not in ("post", "metrics"):
            continue
        date, kind, channel, url, detail = cells
        if kind == "post":
            posts.append({"date": date, "channel": channel, "url": url, "title": detail.replace("\\|", "|")})
        else:
            metrics.setdefault(url, {}).update({k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", detail)})
    return {"posts": posts, "metrics": metrics}


def add_post(brand: Brand, path: Path, channel: str, url: str | None, title: str, date: str | None = None) -> str:
    good = check_url(brand, channel, url)
    if not title.strip():
        raise PublishedError("a title (what the post says, in a few words) is required")
    date = date or datetime.now().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise PublishedError("date must be YYYY-MM-DD")
    if good != NO_URL and any(p["url"] == good for p in read_log(path)["posts"]):
        raise PublishedError("that URL is already in the log")
    row = f"| {date} | post | {channel} | {good} | {_cell(title)} |"
    append_line(path, row, HEADER)
    return row


def add_metrics(brand: Brand, path: Path, url: str, values: dict[str, int], date: str | None = None) -> str:
    post = next((p for p in read_log(path)["posts"] if p["url"] == url), None)
    if post is None:
        raise PublishedError("that URL is not in the log; add the post first")
    ch = brand.channel(post["channel"])
    bad = sorted(set(values) - set(ch.public_metrics))
    if bad:
        raise PublishedError(f"not a public metric on {ch.label}: {', '.join(bad)} (public: {', '.join(ch.public_metrics) or 'none'})")
    if not values or any(v < 0 for v in values.values()):
        raise PublishedError("give at least one non-negative public metric")
    date = date or datetime.now().strftime("%Y-%m-%d")
    row = f"| {date} | metrics | {post['channel']} | {url} | " + " ".join(f"{k}={v}" for k, v in sorted(values.items())) + " |"
    append_line(path, row, HEADER)
    return row
