from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from sovereign_m.boardio import default_board
from sovereign_m.roundtable.model import DEFAULT_TIMEOUT, HttpModel
from sovereign_m.runner.loop import run_loop, stub_for


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="m-runner", description="Run local board cards on the local model.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--board", type=Path)
    r.add_argument("--out-board", type=Path, help="write outputs here (dry run default: a temp dir)")
    r.add_argument("--dry-run", action="store_true", help="stub model; reads the real board, writes to a temp dir")
    r.add_argument("--max-cards", type=int, default=2)
    r.add_argument("--max-turns", type=int, default=6)
    r.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    a = p.parse_args(argv)
    board = (a.board or default_board()).expanduser()
    out = a.out_board.expanduser() if a.out_board else None
    if a.dry_run and out is None:
        out = Path(tempfile.mkdtemp(prefix="m-sovereign-dry-"))
    out = out or board
    if out != board and (board / "tasks.jsonl").exists():
        out.mkdir(parents=True, exist_ok=True)
        if not (out / "tasks.jsonl").exists():
            shutil.copy2(board / "tasks.jsonl", out / "tasks.jsonl")  # read copy for evidence
    model_for = stub_for if a.dry_run else (lambda _cid, m=HttpModel(timeout=a.timeout): m)
    report = run_loop(board, out, model_for, a.max_cards, a.max_turns, a.dry_run)
    print(f"out board: {out}")
    print(json.dumps(report, indent=1))
    return 0
