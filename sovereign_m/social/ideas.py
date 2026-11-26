"""Gap 1 flow: offer up to 5 numbered ideas and STOP. On a pick, write 3 hook versions plus captions
into a new dated folder. Two separate commands, so nothing runs past the question."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sovereign_m.social.brands import Brand, Paths
from sovereign_m.social.common import DATA_RULE, fence, newest, parse_json, read_text
from sovereign_m.social.lint import NEVER_USE, clean
from sovereign_m.social.safeio import mkdir_new, slug, write_new

MAX_IDEAS = 5


class Unavailable(RuntimeError):
    pass


def _system(brand: Brand, paths: Paths) -> str:
    voice = read_text(paths.brand_dir / "voice.md", 3000) or "voice.md missing: UNVERIFIED"
    visuals = read_text(paths.brand_dir / "visuals.md", 2000) or "visuals.md missing: UNVERIFIED"
    chans = "; ".join(f"{c.label} {c.handle}" for c in brand.channels)
    return (f"You draft social posts for {brand.name}. Channels (and only these): {chans}.\n"
            + ("Kid-safe. No student names or data.\n" if brand.kid_safe else "")
            + "Never-use list:\n" + "\n".join(f"- {n}" for n in NEVER_USE) + "\n"
            + fence("voice.md", voice, 3000) + "\n" + fence("visuals.md", visuals, 2000) + "\n" + DATA_RULE)


def _inputs(brand: Brand, paths: Paths) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if brand.key == "vanhorn-si":
        n = newest(paths.area / "trends")
        if n:
            items.append((f"trends/{n.name}", read_text(n)))
    else:
        d = paths.area / "daily-post"
        days = sorted(p for p in d.iterdir() if p.is_dir()) if d.is_dir() else []
        for day in days[-2:]:
            c = day / "curriculum.md"
            if c.is_file():
                items.append((f"daily-post/{day.name}/curriculum.md", read_text(c)))
    r = newest(paths.results)
    if r:
        items.append((f"results/{r.name}", read_text(r)))
    return items


def make_ideas(brand: Brand, chat, paths: Paths | None = None, now: datetime | None = None) -> dict:
    """Ask the model for up to 5 ideas, save them, return them. The caller prints and stops."""
    paths = paths or Paths(brand)
    now = now or datetime.now()
    inputs = _inputs(brand, paths)
    if not inputs:
        raise Unavailable("no inputs found (no trends/curriculum/results file): nothing to base ideas on. UNVERIFIED")
    user = ("\n".join(fence(label, body) for label, body in inputs)
            + f"\n\nOffer up to {MAX_IDEAS} post ideas grounded ONLY in the data above. Reply ONLY with JSON: "
            '{"ideas": [{"title": "<short>", "why": "<one sentence>", "from": "<one of the data labels above>"}]}')
    text, model = chat(_system(brand, paths), user, 900)
    obj = parse_json(text) or {}
    labels = {label for label, _ in inputs}
    ideas = []
    for raw in (obj.get("ideas") or [])[:MAX_IDEAS]:
        if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
            continue
        ch = brand.channels[0]
        title, _v = clean(str(raw["title"]), ch, has_sources=True)
        why, _v2 = clean(str(raw.get("why", "")), ch, has_sources=True)
        src = str(raw.get("from", ""))
        ideas.append({"n": len(ideas) + 1, "title": title[:120], "why": why[:240],
                      "from": next((l for l in labels if src and (src == l or l.endswith(src) or src.endswith(l))), "UNVERIFIED")})
    if not ideas:
        raise Unavailable(f"model gave no usable ideas (model: {model})")
    record = {"brand": brand.key, "created": now.strftime("%Y-%m-%d %H:%M:%S"), "model": model, "ideas": ideas}
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    path = write_new(paths.ideas, f"{stamp}.json", json.dumps(record, ensure_ascii=False, indent=1))
    record["file"] = str(path)
    return record


def render_ideas(record: dict) -> str:
    lines = [f"Ideas for {record['brand']} (pick one by number, then run pick):", ""]
    for i in record["ideas"]:
        lines += [f"{i['n']}. {i['title']}", f"   why: {i['why']}", f"   from: {i['from']}"]
    lines += ["", "Stopped. Nothing was drafted yet. Reply with a number."]
    return "\n".join(lines)


def latest_ideas(paths: Paths) -> dict | None:
    files = sorted(paths.ideas.glob("*.json")) if paths.ideas.is_dir() else []
    return json.loads(files[-1].read_text(encoding="utf-8")) | {"file": str(files[-1])} if files else None


def pick(brand: Brand, n: int, chat, paths: Paths | None = None, now: datetime | None = None) -> dict:
    paths = paths or Paths(brand)
    now = now or datetime.now()
    record = latest_ideas(paths)
    if record is None:
        raise Unavailable("no ideas on file: run ideas first")
    idea = next((i for i in record["ideas"] if i["n"] == n), None)
    if idea is None:
        raise Unavailable(f"pick a number from 1 to {len(record['ideas'])}")
    keys = ", ".join(f'"{c.key}"' for c in brand.channels)
    user = (fence("chosen idea", json.dumps(idea, ensure_ascii=False)) + "\n\n"
            "Write exactly 3 hook versions (first line of the post, different angles) and one caption per channel. "
            f"Channel keys allowed: {keys}. Reply ONLY with JSON: "
            '{"hooks": ["h1","h2","h3"], "captions": {"<channel key>": "<caption>"}, "sources": ["<source or UNVERIFIED>"]}')
    text, model = chat(_system(brand, paths), user, 1200)
    obj = parse_json(text) or {}
    hooks = [str(h) for h in (obj.get("hooks") or []) if str(h).strip()]
    if len(hooks) != 3:
        raise Unavailable(f"model did not give exactly 3 hooks (model: {model}); nothing written")
    sources = [str(s) for s in (obj.get("sources") or []) if str(s).strip()]
    has_sources = bool(sources) and all("UNVERIFIED" not in s for s in sources)
    first = brand.channels[0]
    report, hook_rows = [], []
    for i, h in enumerate(hooks, 1):
        fixed, bad = clean(h, first, has_sources)
        hook_rows.append((i, fixed))
        report += [f"hook {i}: {b}" for b in bad]
    caps, dropped = [], []
    for key, body in (obj.get("captions") or {}).items():
        ch = brand.channel(str(key))
        if ch is None:
            dropped.append(str(key))  # channel rule: not one of this brand's channels
            continue
        fixed, bad = clean(str(body), ch, has_sources)
        caps.append((ch, fixed))
        report += [f"{ch.label} caption: {b}" for b in bad]
    missing = [c.label for c in brand.channels if c.key not in {x.key for x, _ in caps}]
    folder = mkdir_new(paths.drafts, f"{now.strftime('%Y-%m-%d')}-{slug(idea['title'])}")
    write_new(folder, "idea.json", json.dumps({"idea": idea, "model": model, "from_ideas": record["file"]}, ensure_ascii=False, indent=1))
    write_new(folder, "hooks.md", "# Hook versions (DRAFT)\n\n" + "\n\n".join(f"## Hook {i}\n\n{t}" for i, t in hook_rows) + "\n")
    write_new(folder, "captions.md", "# Captions (DRAFT)\n\n" + "\n\n".join(
        f"## {ch.label} {ch.handle}\n\n{body}\n" for ch, body in caps) + ("\nMissing: " + ", ".join(missing) + "\n" if missing else ""))
    write_new(folder, "sources.md", "# Sources\n\n" + ("\n".join(f"- {s}" for s in sources) if sources else "- UNVERIFIED (none given)") + "\n")
    write_new(folder, "lint.md", "# Never-use check\n\n" + ("\n".join(f"- {r}" for r in report) if report else "Clean after mechanical fixes.")
              + ("\n\nDropped captions for channels this brand does not use: " + ", ".join(dropped) if dropped else "") + "\n")
    write_new(folder, "README.md", f"# {idea['title']}\n\nDRAFT for {brand.name}. Made on the local model ({model}). "
              "A bot never posts, schedules or replies. The operator posts. Check lint.md before using anything.\n")
    return {"folder": str(folder), "lint_issues": report, "model": model}
