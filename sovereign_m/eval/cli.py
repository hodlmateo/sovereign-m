"""``python -m sovereign_m.eval run [--small] [--big] [--dry-run]``: write <board>/eval/m-ten-tasks.md (new file, -vN)."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from sovereign_m.boardio import default_board, write_new
from sovereign_m.eval.runner import render_report, run_model
from sovereign_m.roundtable.model import HttpModel
from sovereign_m.router.lanes import SMALL_MODEL, Small7B


class BigOnly(HttpModel):
    """llama-server only: the Ollama fallback is switched off so a silent fallback can't pass as the 35B."""

    def _fallback(self, messages, max_tokens):  # pragma: no cover - trivial
        from sovereign_m.roundtable.model import ModelError
        raise ModelError("llama-server unreachable (fallback disabled for eval)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="m-eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--small", action="store_true")
    r.add_argument("--big", action="store_true")
    r.add_argument("--board", type=Path)
    r.add_argument("--dry-run", action="store_true", help="write the report to a temp dir")
    a = p.parse_args(argv)
    board = (a.board or default_board()).expanduser()
    out = Path(tempfile.mkdtemp(prefix="m-eval-dry-")) if a.dry_run else board
    both = not (a.small or a.big)
    runs = []
    if a.small or both:
        runs.append(run_model(f"small: {SMALL_MODEL}", Small7B(timeout=120, json_mode=False), expect_backend="ollama"))
    if a.big or both:
        runs.append(run_model("big: Qwen3.6-35B-A3B (llama-server)", BigOnly(timeout=240), expect_backend="llama-server"))
    path = write_new(out / "eval", "m-ten-tasks.md", render_report(runs))
    print(path)
    for run in runs:
        print(f"{run.label}: {run.score}/10")
    return 0
