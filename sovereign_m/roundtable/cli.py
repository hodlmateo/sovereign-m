"""m-roundtable command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sovereign_m.roundtable.model import DEFAULT_TIMEOUT, HttpModel, dry_run_model
from sovereign_m.roundtable.runner import (
    CardNotFound,
    default_board,
    load_card,
    load_card_file,
    run_roundtable,
    safe_name,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m-roundtable", description="Run a local bot-to-bot roundtable for a board card."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run Ops -> Worker -> Checker on one card")
    run.add_argument("card_id", nargs="?", help="card id in the board tasks.jsonl")
    run.add_argument("--card-file", type=Path, help="JSON file holding one synthetic card")
    run.add_argument("--max-turns", type=int, default=6, help="max total model calls (default 6)")
    run.add_argument("--dry-run", action="store_true", help="use the scripted stub model")
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-request seconds")
    run.add_argument("--board", type=Path, help="board dir (default M_ROUNDTABLE_BOARD or the home board)")
    run.add_argument("--out-dir", type=Path, help="transcript dir (default <board>/roundtable)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    board = (args.board or default_board()).expanduser()
    try:
        if args.card_file:
            card = load_card_file(args.card_file)
            card_id = args.card_id or str(card.get("id") or args.card_file.stem)
        elif args.card_id:
            card_id = args.card_id
            card = load_card(board, card_id)
        else:
            print("error: give a card id or --card-file", file=sys.stderr)
            return 2
    except CardNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.max_turns < 1:
        print("error: --max-turns must be at least 1", file=sys.stderr)
        return 2

    model = dry_run_model() if args.dry_run else HttpModel(timeout=args.timeout)
    result = run_roundtable(
        card, card_id, model, board, out_dir=args.out_dir, max_turns=args.max_turns,
        dry_run=args.dry_run,
    )
    print(f"card: {safe_name(card_id)}")
    print(f"stop: {result.stop_reason}")
    print(f"advisory verdict (local Checker, same model, not independent): {result.advisory_verdict}")
    print(f"transcript: {result.md_path}")
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
