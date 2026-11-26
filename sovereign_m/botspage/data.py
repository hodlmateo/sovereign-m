"""Read-only views of the board for the page: per-bot threads and the top bar."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from sovereign_m.boardio import read_cards, redact, safe_id
from sovereign_m.botspage.config import BOTS, BY_ID, bot_for_owner

BODY_CHARS = 1500
COPY_CHARS = 40_000
THREAD_MAX = 80
DONE_DAYS = 14
_TS = "%Y-%m-%d %H:%M:%S"


def stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime(_TS)


def _read(path: Path, limit: int = BODY_CHARS) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        return redact(path.read_text(encoding="utf-8", errors="replace")[:limit])
    except OSError:
        return ""


def _files(directory: Path, card_id: str, suffix: str = ".md"):
    """Files named <card_id>.md, <card_id>-v2.md, <card_id>.check.md ... never other cards' files."""
    sid = safe_id(card_id)
    if not directory.is_dir():
        return []
    out = []
    for p in directory.iterdir():
        n = p.name
        if not n.endswith(suffix) or p.is_symlink():
            continue
        stem = n[: -len(suffix)]
        if stem == sid or re.fullmatch(re.escape(sid) + r"(-v\d+|-draft(-v\d+)?|\.check(-v\d+)?)", stem):
            out.append(p)
    return out


def verdict_of(text: str) -> str:
    m = re.search(r"\*\*(PASS|FAIL)\*\*", text)
    return m.group(1) if m else "UNKNOWN"


# ------------------------------------------------------------------ timers
_CACHE: dict = {}


def _sysctl(*args: str, timeout: float = 8.0) -> str:
    key = args
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 10:
        return hit[1]
    try:
        res = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)
        out = res.stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    _CACHE[key] = (time.monotonic(), out)
    return out


def _journal(unit: str, n: int = 4) -> str:
    try:
        res = subprocess.run(["journalctl", "--user", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso"],
                             capture_output=True, text=True, timeout=8)
        return redact(res.stdout.strip()[-BODY_CHARS:])
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _props(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _parse_systemd_ts(value: str) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", value or "")
    return f"{m.group(1)} {m.group(2)}" if m else ""


def timer_items(names) -> list[dict]:
    items = []
    for name in names:
        svc = _props(_sysctl("show", f"{name}.service", "-p", "ActiveState,Result,ExecMainExitTimestamp,ExecMainStartTimestamp,LoadState"))
        tim = _props(_sysctl("show", f"{name}.timer", "-p", "LoadState,ActiveState,NextElapseUSecRealtime"))
        if svc.get("LoadState") != "loaded" and tim.get("LoadState") != "loaded":
            continue  # not installed (for example this branch's own unit): nothing to report
        failed = svc.get("ActiveState") == "failed" or svc.get("Result") not in (None, "", "success")
        last = _parse_systemd_ts(svc.get("ExecMainExitTimestamp") or svc.get("ExecMainStartTimestamp", ""))
        nxt = _parse_systemd_ts(tim.get("NextElapseUSecRealtime", ""))
        body = f"last run {last or 'never'}; next {nxt or 'none scheduled'}; result {svc.get('Result') or 'n/a'}"
        if failed:
            body += "\n" + _journal(f"{name}.service")
        items.append({"ts": last or "1970-01-01 00:00:00", "kind": "timer", "title": f"{name}.timer",
                      "body": body, "level": "fail" if failed else "ok"})
    return items


def failed_units() -> list[dict]:
    out = []
    for line in _sysctl("list-units", "--failed", "--no-legend", "--plain").splitlines():
        parts = line.split()
        if parts and parts[0].endswith((".service", ".timer")):
            out.append({"unit": parts[0]})
    return out


# ------------------------------------------------------------------ threads
def bot_list(board: Path) -> list[dict]:
    cards = read_cards(board)
    rows = []
    for bid, name, owners, _t in BOTS:
        mine = [c for c in cards if c.get("owner") in owners]
        open_n = sum(1 for c in mine if c.get("status") != "done")
        waiting = sum(1 for c in mine if c.get("status") == "waiting")
        rows.append({"id": bid, "name": name, "open": open_n, "waiting": waiting})
    return rows


def thread(board: Path, bot_id: str, out_board: Path | None = None) -> dict | None:
    if bot_id not in BY_ID:
        return None
    out_board = out_board or board
    _bid, name, owners, timers = BY_ID[bot_id]
    cutoff = time.time() - DONE_DAYS * 86400
    items: list[dict] = []
    for card in read_cards(board):
        if card.get("owner") not in owners:
            continue
        cid = str(card["id"])
        opened = str(card.get("opened") or "1970-01-01")
        try:
            opened_epoch = datetime.strptime(opened, "%Y-%m-%d").timestamp()
        except ValueError:
            opened_epoch = 0
        done = card.get("status") == "done"
        files_epoch = 0.0
        sub = []
        for sub_dir, kind in (("roundtable/drafts", "draft"), ("proof", "check"),
                              ("handoffs", "handoff"), ("escalations", "escalation")):
            for p in _files(board / sub_dir, cid):
                if kind == "check" and ".check" not in p.name:
                    continue
                if kind == "draft" and ".local-draft" in p.name:
                    continue
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                files_epoch = max(files_epoch, mt)
                text = _read(p)
                it = {"ts": stamp(mt), "kind": kind, "title": f"{kind}: {p.name}", "body": text, "card": cid}
                if kind == "check":
                    v = verdict_of(text)
                    it["verdict"], it["level"] = v, ("ok" if v == "PASS" else "fail")
                elif kind == "escalation":
                    it["level"] = "warn"
                elif kind == "handoff":
                    it["copy"] = _read(p, COPY_CHARS)
                    it["level"] = "warn"
                sub.append(it)
        if done and max(opened_epoch, files_epoch) < cutoff:
            continue
        items.append({"ts": stamp(max(opened_epoch, 0)), "kind": "card", "title": f"{cid}: {card.get('title', '')}",
                      "body": f"status {card.get('status')} · gate tier {card.get('gate_tier')} · due {card.get('due') or 'none'}\n"
                              + redact(str(card.get("proof", ""))[:600]),
                      "status": card.get("status"), "card": cid,
                      "level": "warn" if card.get("status") == "waiting" else "ok"})
        items += sub
    items += timer_items(timers)
    for sub_dir, kinds in (("botchat", None), ("suggestions", None)):
        d = out_board / sub_dir / bot_id
        if d.is_dir():
            for p in d.glob("*.json"):
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(obj, dict) and "ts" in obj and "kind" in obj:
                    items.append(obj)
    # attach decisions to suggestions
    decisions = {}
    ddir = out_board / "suggestions" / bot_id
    if ddir.is_dir():
        for p in ddir.glob("*.decision.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                decisions[d.get("suggestion")] = d
            except (OSError, ValueError):
                pass
    items = [i for i in items if not str(i.get("title", "")).endswith(".decision.json")]
    for it in items:
        if it.get("kind") == "suggestion":
            it["decision"] = (decisions.get(it.get("id")) or {}).get("decision")
    rank = {"user": 0, "m": 2}
    items.sort(key=lambda i: (str(i["ts"]), rank.get(i.get("kind"), 1)))
    return {"bot": bot_id, "name": name, "items": items[-THREAD_MAX:]}


# ------------------------------------------------------------------ top bar
def top(board: Path, out_board: Path | None = None, today: str | None = None) -> dict:
    out_board = out_board or board
    today = today or datetime.now().strftime("%Y-%m-%d")
    cards = {str(c["id"]): c for c in read_cards(board)}
    waiting = []
    for c in cards.values():
        if c.get("status") == "waiting":
            waiting.append({"type": "gate", "id": c["id"], "title": c.get("title", ""),
                            "bot": bot_for_owner(c.get("owner", "")), "tier": c.get("gate_tier")})
    for kind, sub in (("escalation", "escalations"), ("handoff", "handoffs")):
        for d in {board / sub, out_board / sub}:
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.md")):
                if p.is_symlink():
                    continue
                stem = re.sub(r"(-v\d+)?$", "", p.stem)
                card = cards.get(stem)
                if card and card.get("status") == "done":
                    continue
                waiting.append({"type": kind, "id": stem, "title": _title(p),
                                "bot": bot_for_owner((card or {}).get("owner", "")), "file": p.name})
    daylog = board.parent / "brief" / "daylog"
    path = daylog / f"{today}.md"
    link = {"date": today, "exists": path.is_file()}
    if not link["exists"] and daylog.is_dir():
        older = sorted(p for p in daylog.glob("????-??-??.md"))
        if older:
            link = {"date": older[-1].stem, "exists": True, "stale": True}
    return {"waiting": waiting, "failed": failed_units(), "daylog": link}


def _title(p: Path) -> str:
    first = _read(p, 200).splitlines()
    return first[0].lstrip("# ").strip() if first else p.stem


def daylog_text(board: Path, date: str) -> str | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return None
    return _read(board.parent / "brief" / "daylog" / f"{date}.md", 60_000) or None
