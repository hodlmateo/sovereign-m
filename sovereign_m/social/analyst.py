"""Local Analyst: weekly results note per brand. Public metrics only; 'missing' when unavailable.

Reads the brand's published.md (posts plus metrics rows added by CLI). Writes
<area>/results/YYYY-MM-DD.md as a NEW file. Suggests voice.md edits; never applies them.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sovereign_m.social.brands import Brand, Paths
from sovereign_m.social.common import DATA_RULE, fence, numbers_in, parse_json
from sovereign_m.social.lint import clean
from sovereign_m.social.published import read_log
from sovereign_m.social.safeio import write_new

ET = ZoneInfo("America/New_York")
ORDER = ("views", "likes", "reposts", "replies", "comments", "shares", "bookmarks")


def _sort_key(post: dict, metrics: dict):
    m = metrics.get(post["url"], {})
    return tuple(-(m.get(k, -1)) for k in ("views", "likes", "comments", "shares"))


def _fmt(brand: Brand, post: dict, metrics: dict) -> str:
    ch = brand.channel(post["channel"])
    have = metrics.get(post["url"], {})
    parts = [f"{k} {have[k]}" if k in have else f"{k} missing" for k in (ch.public_metrics if ch else ())]
    if ch and not ch.public_metrics:
        parts = ["no public metrics exist for this channel: missing"]
    return f"{post['title']} ({post['date']}, {ch.label if ch else post['channel']}): " + ", ".join(parts) + f"\n  link: {post['url']}"


def _rankable(brand: Brand, posts: list[dict], metrics: dict) -> list[dict]:
    return [p for p in posts if any(k in metrics.get(p["url"], {}) for k in ("views", "likes", "comments", "shares"))]


def analyse(brand: Brand, paths: Paths, chat, now: datetime | None = None) -> dict:
    now = (now or datetime.now(ET)).astimezone(ET)
    name = now.strftime("%Y-%m-%d")
    target = paths.results / f"{name}.md"
    if target.exists():
        return {"status": "no_change", "file": str(target)}   # check first: this week's note exists
    start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    log = read_log(paths.published)
    posts = [p for p in log["posts"] if start <= p["date"] <= name]
    metrics = log["metrics"]
    L = [f"# Weekly results note: {name}", "",
         f"- Brand: {brand.name}. Channels: " + ", ".join(f"{c.label} {c.handle}" for c in brand.channels),
         f"- Window: {start} through {name} America/New_York",
         "- Public metrics only, read from the published log. Nothing was posted. No invented numbers; gaps say `missing`.", ""]
    ranked = sorted(_rankable(brand, posts, metrics), key=lambda p: _sort_key(p, metrics))
    unranked = [p for p in posts if p not in ranked]
    L.append("## Top post")
    L.append(_fmt(brand, ranked[0], metrics) if ranked else "missing: no post in the window has a public metric logged.")
    L += ["", "## Bottom post"]
    L.append(_fmt(brand, ranked[-1], metrics) if len(ranked) > 1 else
             ("No separate bottom post (only one rankable post)." if ranked else "missing: nothing to rank."))
    L += ["", "## Posts in the window"]
    L += [f"- {_fmt(brand, p, metrics)}" for p in ranked + unranked] or ["- none logged in the window"]
    L += ["", "## Channels with no data"]
    for c in brand.channels:
        n = sum(1 for p in posts if p["channel"] == c.key)
        with_m = sum(1 for p in ranked if p["channel"] == c.key)
        L.append(f"- {c.label}: {n} posts logged, {with_m} with a public metric" + ("" if with_m else ": metrics missing"))
    voice = (paths.brand_dir / "voice.md")
    advice = _advice(brand, paths, chat, posts, ranked, metrics, voice)
    L += [""] + advice
    path = write_new(paths.results, f"{name}.md", "\n".join(L) + "\n")
    return {"status": "written", "file": str(path), "posts": len(posts), "ranked": len(ranked)}


def _advice(brand, paths, chat, posts, ranked, metrics, voice) -> list[str]:
    head_do, head_voice = "## Do more of (2-3) and stop (1)", "## Suggested voice.md edits (NOT applied; the operator decides)"
    if not ranked:
        return [head_do, "UNVERIFIED: no post with public metrics this week, so there is nothing to ground advice on.", "",
                head_voice, "UNVERIFIED: same reason. voice.md was not changed."]
    table = "\n".join(_fmt(brand, p, metrics) for p in ranked)
    voice_text = voice.read_text(encoding="utf-8")[:2500] if voice.is_file() else "voice.md missing"
    system = (f"You are the Analyst for {brand.name}. {DATA_RULE}\nUse ONLY the metrics table. Do not mention any number that is not in it.")
    user = (fence("metrics table", table) + "\n" + fence("current voice.md", voice_text) + "\n\n"
            'Reply ONLY with JSON: {"do_more": ["2 or 3 short notes"], "stop": "one thing to stop", '
            '"voice_edits": ["0 to 3 suggested edits to voice.md, each quoting what to change"]}')
    text, model = chat(system, user, 700)
    obj = parse_json(text) or {}
    ch = brand.channels[0]
    allowed = numbers_in(table) | {"1", "2", "3"}
    do = [clean(str(x), ch, True)[0] for x in (obj.get("do_more") or []) if str(x).strip()]
    stop = clean(str(obj.get("stop", "")), ch, True)[0]
    edits = [clean(str(x), ch, True)[0] for x in (obj.get("voice_edits") or []) if str(x).strip()][:3]
    bad = [t for t in do + [stop] + edits if numbers_in(t) - allowed]
    if not (2 <= len(do) <= 3) or not stop or bad:
        return [head_do, f"UNVERIFIED: the model output was missing parts or used numbers not in the table (model: {model}); nothing kept.",
                "", head_voice, "UNVERIFIED: not produced. voice.md was not changed."]
    return [head_do, *[f"- Do more: {d}" for d in do], f"- Stop: {stop}", "", head_voice,
            *([f"- {e}" for e in edits] or ["- no edits suggested"]), "", f"(Advice drafted by the local model: {model}.)"]
