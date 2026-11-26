"""``python -m sovereign_m.social <command>``. Drafts only. Never posts, replies or schedules."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sovereign_m.social import analyst, ideas, llm, mentions, published, templates
from sovereign_m.social.brands import BRANDS, Paths


def _brand_args(p: argparse.ArgumentParser, allow_all: bool = False) -> None:
    p.add_argument("--brand", required=True, choices=sorted(BRANDS) + (["all"] if allow_all else []))
    p.add_argument("--hub", type=Path, help="hub root (default: M_HUB or the home hub); tests and dry runs use a copy")


def main(argv: list[str] | None = None, chat=None) -> int:
    chat = chat or llm.chat
    p = argparse.ArgumentParser(prog="m-social")
    sub = p.add_subparsers(dest="cmd", required=True)
    _brand_args(sub.add_parser("init-brand", help="write voice.md and visuals.md templates if missing"), allow_all=True)
    _brand_args(sub.add_parser("ideas", help="offer up to 5 numbered ideas, then stop"))
    pk = sub.add_parser("pick", help="write 3 hooks plus captions for idea N into a new dated folder")
    _brand_args(pk)
    pk.add_argument("number", type=int)
    _brand_args(sub.add_parser("analyst", help="weekly results note (new file; no_change if this week's exists)"))
    _brand_args(sub.add_parser("mentions", help="weekday digest of up to 10 mentions worth a reply (vanhorn-si only)"))
    pub = sub.add_parser("published", help="append-only published log")
    ps = pub.add_subparsers(dest="pcmd", required=True)
    add = ps.add_parser("add")
    _brand_args(add)
    add.add_argument("--channel", required=True)
    add.add_argument("--url", help="the real public link to the post; required except for WhatsApp")
    add.add_argument("--title", required=True)
    add.add_argument("--date")
    met = ps.add_parser("metrics")
    _brand_args(met)
    met.add_argument("--url", required=True)
    met.add_argument("--date")
    for k in ("views", "likes", "reposts", "replies", "comments", "shares", "bookmarks"):
        met.add_argument(f"--{k}", type=int)
    lst = ps.add_parser("list")
    _brand_args(lst)
    a = p.parse_args(argv)

    def paths_for(key: str) -> Paths:
        return Paths(BRANDS[key], a.hub)

    try:
        if a.cmd == "init-brand":
            for key in (sorted(BRANDS) if a.brand == "all" else [a.brand]):
                r = templates.init_brand(BRANDS[key], paths_for(key))
                print(key, "written:", r["written"] or "none", "skipped (already there):", r["skipped"] or "none")
        elif a.cmd == "ideas":
            rec = ideas.make_ideas(BRANDS[a.brand], chat, paths_for(a.brand))
            print(ideas.render_ideas(rec))
        elif a.cmd == "pick":
            r = ideas.pick(BRANDS[a.brand], a.number, chat, paths_for(a.brand))
            print(f"folder: {r['folder']}\nmodel: {r['model']}\nnever-use issues: {len(r['lint_issues'])} (see lint.md)")
        elif a.cmd == "analyst":
            print(analyst.analyse(BRANDS[a.brand], paths_for(a.brand), chat))
        elif a.cmd == "mentions":
            if a.brand != "vanhorn-si":
                print("error: the mentions digest is for vanhorn-si only", file=sys.stderr)
                return 2
            print(mentions.digest(BRANDS[a.brand], paths_for(a.brand), chat))
        elif a.cmd == "published":
            pa = paths_for(a.brand)
            if a.pcmd == "add":
                print(published.add_post(BRANDS[a.brand], pa.published, a.channel, a.url, a.title, a.date))
            elif a.pcmd == "metrics":
                vals = {k: getattr(a, k) for k in ("views", "likes", "reposts", "replies", "comments", "shares", "bookmarks")
                        if getattr(a, k) is not None}
                print(published.add_metrics(BRANDS[a.brand], pa.published, a.url, vals, a.date))
            else:
                log = published.read_log(pa.published)
                for post in log["posts"]:
                    print(post["date"], post["channel"], post["url"], post["title"], log["metrics"].get(post["url"], {}))
    except (published.PublishedError, ideas.Unavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
