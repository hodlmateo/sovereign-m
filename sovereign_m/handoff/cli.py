from __future__ import annotations

import argparse
import json
from pathlib import Path

from sovereign_m.boardio import default_board
from sovereign_m.handoff.page import serve
from sovereign_m.handoff.writer import write_handoffs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="m-handoff")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="write handoffs/<id>.md for open needs-code cards")
    w.add_argument("--board", type=Path)
    w.add_argument("--out-board", type=Path, help="write under this board dir instead (dry runs)")
    s = sub.add_parser("serve", help="loopback read-only phone list (manual)")
    s.add_argument("--board", type=Path)
    s.add_argument("--port", type=int, default=8478)
    a = p.parse_args(argv)
    board = (a.board or default_board()).expanduser()
    if a.cmd == "serve":
        serve(board / "handoffs", a.port)
        return 0
    out = a.out_board.expanduser() if a.out_board else None
    print(json.dumps(write_handoffs(board, out), indent=1))
    return 0
