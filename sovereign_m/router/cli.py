"""``python -m sovereign_m.router route (--all | <card_id> | --task "text")``.

Prints a table and writes <out-board>/proposed/router-<date>.jsonl (new file, -vN on collision).
tasks.jsonl is only read. No model server is started or stopped.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path

from sovereign_m.boardio import default_board, open_cards, read_cards, write_new
from sovereign_m.router.lanes import Route, Small7B, route_card


def route_many(cards: list[dict], model) -> list[Route]:
    return [route_card(c, model) for c in cards]


def render_table(routes: list[Route]) -> str:
    w = max([2] + [len(r.card_id) for r in routes])
    lines = [f"{'id'.ljust(w)}  floor       lane   bucket      source          why"]
    for r in routes:
        lines.append(f"{r.card_id.ljust(w)}  {r.floor.ljust(10)}  {r.lane.ljust(5)}  {r.bucket.ljust(10)}  "
                     f"{r.source.ljust(14)}  {r.why[:70]}")
    flagged = sorted({r.note for r in routes if r.note})
    lines += [f"NOTE: {n}" for n in flagged]
    return "\n".join(lines)


def write_proposed(out_board: Path, routes: list[Route], today: str | None = None) -> Path:
    today = today or datetime.now().strftime("%Y-%m-%d")
    body = "".join(json.dumps({**r.as_row(), "source_tool": "m-router", "proposed": True,
                               "note_to_ops": "Proposed lane only. tasks.jsonl is not edited."},
                              ensure_ascii=False) + "\n" for r in routes)
    return write_new(out_board / "proposed", f"router-{today}.jsonl", body)


def main(argv: list[str] | None = None, model=None) -> int:
    p = argparse.ArgumentParser(prog="m-router")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("route")
    r.add_argument("card_id", nargs="?")
    r.add_argument("--all", action="store_true", help="every open card")
    r.add_argument("--task", help="route a one-off task text instead of a card")
    r.add_argument("--board", type=Path)
    r.add_argument("--out-board", type=Path)
    r.add_argument("--dry-run", action="store_true", help="real routing, outputs to a temp dir")
    a = p.parse_args(argv)
    board = (a.board or default_board()).expanduser()
    out = a.out_board.expanduser() if a.out_board else (Path(tempfile.mkdtemp(prefix="m-router-dry-")) if a.dry_run else board)
    if a.task:
        cards = [{"id": "adhoc", "title": a.task, "status": "today"}]
    elif a.all:
        cards = open_cards(board)
    elif a.card_id:
        cards = [c for c in read_cards(board) if c["id"] == a.card_id]
        if not cards:
            print(f"error: card not found: {a.card_id}")
            return 2
    else:
        print("error: give a card id, --all, or --task")
        return 2
    routes = route_many(cards, model or Small7B())
    print(render_table(routes))
    print(f"\nproposed file: {write_proposed(out, routes)}")
    if any(r.source == "fail-closed" for r in routes):
        print("7B was unavailable or unusable for some cards: those failed closed to needs-code (lane big).")
    return 0
