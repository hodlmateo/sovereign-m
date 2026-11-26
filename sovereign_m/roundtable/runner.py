"""Turn loop, context trimming, and transcript output."""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sovereign_m.roundtable.model import ModelError
from sovereign_m.roundtable.safety import (
    DraftRejected,
    Parsed,
    fence_data,
    cited_paths,
    gather_evidence,
    parse_reply,
    paths_in_text,
    write_draft,
)

ROLES = ("ops", "worker", "checker")
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
REPO_ROOT = Path(__file__).resolve().parents[2]

CHARS_PER_TOKEN = 3.5
PROMPT_TOKEN_BUDGET = 12_000
CARD_CHARS = 1500
MAX_REPLY_TOKENS = 1200
SIMILARITY = 0.95
ADVISORY_LABEL = "local Checker (same model, not independent)"


def est_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def default_board() -> Path:
    override = os.environ.get("M_ROUNDTABLE_BOARD", "").strip()
    return Path(override).expanduser() if override else Path.home() / "m_hub" / "board"


def safe_name(text: str) -> str:
    cleaned = re.sub(r"\.{2,}", "_", re.sub(r"[^A-Za-z0-9._-]", "_", text)).lstrip(".")[:64]
    return cleaned or "card"


# ---------------------------------------------------------------- cards


class CardNotFound(LookupError):
    pass


def load_card(board: Path, card_id: str) -> dict:
    """Latest line in tasks.jsonl whose id matches. File is opened read-only."""
    path = board / "tasks.jsonl"
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError:
        raise CardNotFound(f"cannot read board file: {path}") from None
    found: dict | None = None
    with handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and card_id in (obj.get("id"), obj.get("card_id")):
                found = obj
    if found is None:
        raise CardNotFound(f"card not found: {card_id}")
    return found


def load_card_file(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CardNotFound(f"cannot load card file: {exc}") from None
    if not isinstance(obj, dict):
        raise CardNotFound("card file must hold a single JSON object")
    return obj


def card_text(card: dict) -> str:
    text = json.dumps(card, indent=1, ensure_ascii=False)
    if len(text) > CARD_CHARS:
        text = text[:CARD_CHARS] + "\n[card truncated]"
    return text


# ---------------------------------------------------------------- prompts


def system_prompt(role: str) -> str:
    common = (PROMPT_DIR / "_common.md").read_text(encoding="utf-8")
    body = (PROMPT_DIR / f"{role}.md").read_text(encoding="utf-8")
    return body.strip() + "\n\n" + common.strip() + "\n"


def build_user(
    role: str,
    card: str,
    history: list[tuple[int, str, str]],
    evidence: str,
    system: str,
    note: str = "",
    budget_chars: int = int(PROMPT_TOKEN_BUDGET * CHARS_PER_TOKEN),
) -> tuple[str, int]:
    """Assemble the user message within budget. Returns (text, turns_trimmed).

    System prompt, card, evidence and the closing instruction are always kept;
    older history entries are dropped first (newest kept), then the newest is
    truncated if it alone still does not fit.
    """
    head = "Card (data):\n" + fence_data("card", card) + "\n"
    closing = ""
    if evidence:
        closing += "\nEvidence gathered by the runner (data):\n" + evidence + "\n"
    if note:
        closing += "\n" + note + "\n"
    closing += f"\nNow reply as {role.upper()} in the required format."
    trimmed_note_room = 60
    room = budget_chars - len(system) - len(head) - len(closing) - trimmed_note_room - 40
    entries: list[str] = []
    for turn, who, text in reversed(history):
        block = f"Message {turn} from {who} (data):\n" + fence_data(f"message-{turn}", text)
        cost = len(block) + 1
        if cost <= room:
            entries.append(block)
            room -= cost
        elif not entries and room > 300:
            keep = room - 200
            block = f"Message {turn} from {who} (data):\n" + fence_data(
                f"message-{turn}", text[:keep] + "\n[truncated]"
            )
            entries.append(block)
            room = 0
            break
        else:
            break
    dropped = len(history) - len(entries)
    entries.reverse()
    middle = ""
    if dropped:
        middle += f"[{dropped} earlier turns trimmed]\n"
    middle += "\n".join(entries)
    return head + middle + closing, dropped


def _worker_evidence(card: dict, history: list[tuple[int, str, str]], roots: list[Path]):
    """Evidence for a Worker turn: files named in the card and in the latest Ops reply."""
    cited = []
    for key in ("title", "proof"):
        val = card.get(key)
        if val:
            cited += paths_in_text(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
    ops_text = next((text for _t, who, text in reversed(history) if who == "ops"), "")
    if ops_text:
        fields = parse_reply("ops", ops_text).fields
        cited += cited_from_ops(fields)
    return gather_evidence("\n".join(cited), roots, none_note="")


def cited_from_ops(fields: dict[str, str]) -> list[str]:
    out = cited_paths(fields.get("artifacts", ""))
    out += paths_in_text(fields.get("objective", ""))
    return out


# ---------------------------------------------------------------- loop


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def _similar(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= SIMILARITY


@dataclass
class Result:
    stop_reason: str
    advisory_verdict: str
    turns: list[dict]
    md_path: Path
    jsonl_path: Path
    draft_path: Path | None = None
    backends: list[str] = field(default_factory=list)
    error: str = ""


def run_roundtable(
    card: dict,
    card_id: str,
    model,
    board: Path,
    out_dir: Path | None = None,
    max_turns: int = 6,
    dry_run: bool = False,
    now: datetime | None = None,
) -> Result:
    out_dir = out_dir if out_dir is not None else board / "roundtable"
    ctext = card_text(card)
    roots = [board, board / "roundtable" / "drafts", REPO_ROOT]
    systems = {role: system_prompt(role) for role in ROLES}

    history: list[tuple[int, str, str]] = []  # (turn, role, accepted reply)
    turns: list[dict] = []
    last_text: dict[str, str] = {}
    fail_streak: dict[str, int] = {r: 0 for r in ROLES}
    evidence_for_checker = ""
    evidence_for_worker = ""
    evidence_read = 0
    draft_path: Path | None = None
    draft_name: str | None = None
    advisory = "none"
    stop = ""
    error = ""
    calls = 0
    step = 0
    reask_note = ""

    while True:
        if calls >= max_turns:
            stop = "max_turns"
            break
        role = ROLES[step % 3]
        system = systems[role]
        if role == "worker":
            wev = _worker_evidence(card, history, roots)
            evidence_for_worker = wev.text
        ev = evidence_for_checker if role == "checker" else evidence_for_worker if role == "worker" else ""
        user, _dropped = build_user(role, ctext, history, ev, system, reask_note)
        prompt_est = est_tokens(system + user)
        calls += 1
        started = time.monotonic()
        try:
            reply = model.chat(role, system, user, MAX_REPLY_TOKENS)
        except ModelError as exc:
            error = str(exc)
            stop = "model_error"
            turns.append(
                {"turn": calls, "role": role, "backend": "none", "wall_s": round(time.monotonic() - started, 2),
                 "prompt_tokens_est": prompt_est, "raw": "", "parsed": {}, "parse_ok": False,
                 "parse_errors": [error]}
            )
            break
        wall = round(time.monotonic() - started, 2)
        parsed: Parsed = parse_reply(role, reply.text)
        record = {
            "turn": calls, "role": role, "backend": reply.backend, "wall_s": wall,
            "prompt_tokens_est": prompt_est, "raw": reply.text, "parsed": parsed.fields,
            "parse_ok": parsed.ok, "parse_errors": parsed.errors,
        }
        turns.append(record)

        repeated = role in last_text and _similar(last_text[role], reply.text)
        last_text[role] = reply.text

        if not parsed.ok:
            fail_streak[role] += 1
            if fail_streak[role] >= 2:
                if role == "checker":
                    advisory, stop = "FAIL", "checker_parse_fail"
                else:
                    stop = "deadlock"
                break
            reask_note = (
                "Your previous reply did not follow the format ("
                + "; ".join(parsed.errors)
                + "). Reply again, ONLY in the fixed format."
            )
            continue
        fail_streak[role] = 0
        reask_note = ""

        if repeated:
            stop = "deadlock"
            break

        history.append((calls, role, reply.text))

        if role == "worker":
            for name, body in parsed.drafts[:1]:
                if draft_name is not None and name != draft_name:
                    record["draft"] = "rejected: one draft per run"
                    break
                try:
                    draft_path = write_draft(board, name, body)
                    draft_name = name
                    record["draft"] = f"written: {draft_path.name}"
                except DraftRejected as exc:
                    record["draft"] = f"rejected: {exc}"
            if len(parsed.drafts) > 1:
                record["draft_extra"] = "only the first draft block is considered"
            extra = [str(draft_path)] if draft_path is not None else []
            ev_obj = gather_evidence(parsed.fields.get("artifacts", ""), roots, extra=extra)
            evidence_for_checker = ev_obj.text
            # The draft alone never counts: Worker must cite a real file.
            draft_real = str(draft_path.resolve()) if draft_path is not None else None
            evidence_read = sum(
                1 for r in ev_obj.read
                if draft_real is None or str(Path(os.path.expanduser(str(r))).resolve()) != draft_real
            )
            record["evidence_read"] = ev_obj.read
            record["evidence_skipped"] = ev_obj.skipped

        if role == "checker":
            verdict = parsed.verdict or "NONE"
            record["verdict_raw"] = verdict
            if verdict == "PASS" and evidence_read == 0:
                verdict = "FAIL"
                record["verdict_note"] = "PASS downgraded: no verifiable file evidence besides the draft"
            record["verdict"] = verdict
            advisory = verdict
            if verdict == "PASS":
                stop = "checker_pass"
                break
            if verdict == "FAIL":
                stop = "checker_fail"
                break
        step += 1

    return _write_outputs(
        card_id, model, board, out_dir, max_turns, dry_run, now, stop, advisory,
        turns, draft_path, error,
    )


# ---------------------------------------------------------------- output


def _fence_for(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _write_outputs(
    card_id, model, board, out_dir, max_turns, dry_run, now, stop, advisory, turns, draft_path, error
) -> Result:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = f"{safe_name(card_id)}-{stamp}" + ("-dryrun" if dry_run else "")
    md_path, jsonl_path = out_dir / f"{base}.md", out_dir / f"{base}.jsonl"
    backends = list(dict.fromkeys(t["backend"] for t in turns if t["backend"] != "none"))

    lines = [
        f"# Roundtable: {card_id}",
        "",
        f"- card id: {card_id}",
        f"- backend: {', '.join(backends) or 'none'}",
        f"- max turns: {max_turns}",
        f"- stop reason: {stop}",
        f"- advisory verdict ({ADVISORY_LABEL}): {advisory}",
        "- note: this is advisory only. The real Checker bot still decides done.",
    ]
    if dry_run:
        lines.append("- mode: dry run (stub model, no model was called)")
    if draft_path is not None:
        lines.append(f"- draft written: {draft_path.name}")
    if error:
        lines.append(f"- error: {error}")
    for t in turns:
        text = t["raw"]
        fence = _fence_for(text)
        lines += [
            "",
            f"## Turn {t['turn']}: {t['role']} ({t['wall_s']} s, {t['backend']})",
            "",
        ]
        if not t["parse_ok"]:
            lines += [f"parse errors: {'; '.join(t['parse_errors'])}", ""]
        for key in ("draft", "verdict_note"):
            if key in t:
                lines += [f"{key}: {t[key]}", ""]
        lines += [fence, text, fence]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "summary": True, "card": card_id, "stop_reason": stop, "advisory_verdict": advisory,
        "advisory_label": ADVISORY_LABEL, "turns": len(turns), "backends": backends,
        "dry_run": dry_run, "draft": draft_path.name if draft_path else None, "error": error,
    }
    with open(jsonl_path, "w", encoding="utf-8") as handle:
        for t in turns:
            handle.write(json.dumps(t, ensure_ascii=False) + "\n")
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")

    return Result(stop, advisory, turns, md_path, jsonl_path, draft_path, backends, error)
