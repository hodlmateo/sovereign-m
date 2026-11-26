from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from sovereign_m.boardio import open_cards, redact, safe_id, sha256_file, write_new
from sovereign_m.checker.cli import check_card
from sovereign_m.handoff.classify import LOCAL, classify_routed
from sovereign_m.handoff.writer import write_handoffs
from sovereign_m.roundtable.model import StubModel
from sovereign_m.roundtable.runner import run_roundtable

HEAT_FLAG = Path.home() / "m_hub" / "state" / "hot"


def stub_for(card_id: str) -> StubModel:
    """Scripted dry-run model that cites a real file and drafts one note. No model call."""
    name = f"{safe_id(card_id)}-draft.md"
    state = {"c": 0}
    words = ("alpha quartz meadow", "bravo ember harbor", "cedar lantern orbit", "delta willow summit")

    def script(role, system, user, index):
        n = index // 3 + 1
        if role == "ops":
            return (f"from: ops\nto: worker\nobjective: Round {n} ({words[n % 4]}): draft a short note for {card_id}.\n"
                    f"artifacts: tasks.jsonl\ndecisions: {words[n % 4]} One paragraph, facts from the card only.\nopen questions: none")
        if role == "worker":
            return (f"from: worker\nto: checker\nobjective: Draft note, round {n} {words[(n + 1) % 4]}.\nartifacts: tasks.jsonl\n"
                    f"decisions: Drafted the note {words[(n + 2) % 4]}.\n```draft name={name}\n# Draft (dry run, stub model)\n"
                    f"Stub note for card {card_id}. No model was called.\n```\nopen questions: none")
        state["c"] += 1
        verdict = "NONE" if state["c"] == 1 else "PASS"
        return (f"from: checker\nto: ops\nobjective: Check round {n}.\nartifacts: tasks.jsonl\n"
                f"decisions: Round {n} check {state['c']} {words[(n + 3) % 4]}.\nopen questions: none\nverdict: {verdict}")

    return StubModel(script)


def run_loop(board: Path, out_board: Path, model_for, max_cards: int = 2, max_turns: int = 6,
             dry_run: bool = False, today: str | None = None, heat_flag: Path = HEAT_FLAG, router=None) -> dict:
    today = today or datetime.now().strftime("%Y-%m-%d")
    report: dict = {"date": today, "dry_run": dry_run, "handoffs": write_handoffs(board, out_board, router),
                    "local_results": [], "skipped": []}
    if heat_flag.exists():
        report["skipped"].append("heat flag set: no model calls this run")
        return report
    local = [c for c in open_cards(board) if classify_routed(c, router)[0] == LOCAL]
    report["local_open"] = [c["id"] for c in local]
    done = 0
    for card in local:
        cid = safe_id(card["id"])
        if any((out_board / "proof").glob(f"{cid}.local-draft*.md")):
            report["skipped"].append(f"{card['id']}: already drafted (proof exists)")
            continue
        if done >= max_cards:
            report["skipped"].append(f"{card['id']}: over --max-cards {max_cards}")
            continue
        done += 1
        result = run_roundtable(card, card["id"], model_for(card["id"]), out_board,
                                max_turns=max_turns, dry_run=dry_run)
        lines = [f"# Local draft evidence: {card['id']}", "",
                 f"roundtable-advisory: {result.advisory_verdict}",
                 f"transcript: {result.md_path}", f"stop-reason: {result.stop_reason}"]
        if result.error:
            lines.append(f"error: {redact(result.error)[:200]}")
        if result.draft_path:
            lines += [f"file: {result.draft_path}", f"sha256: {sha256_file(result.draft_path)}  {result.draft_path}"]
        proof = write_new(out_board / "proof", f"{cid}.local-draft.md", "\n".join(lines) + "\n")
        verdict, check_path, esc = check_card(out_board, card["id"], proof, out_board)
        report["local_results"].append({
            "id": card["id"], "stop": result.stop_reason, "advisory": result.advisory_verdict,
            "check": verdict, "draft": str(result.draft_path) if result.draft_path else None,
            "proof": str(proof), "check_file": str(check_path), "escalation": str(esc) if esc else None})
        if result.stop_reason == "model_error":
            report["skipped"].append("model error: stopping after this card")
            break
    write_outputs(out_board, report)
    return report


def write_outputs(out_board: Path, report: dict) -> None:
    rows = report["local_results"]
    if not rows:
        return
    today = report["date"]
    jl = "".join(json.dumps({
        "id": r["id"], "source": "m-sovereign-loop", "proposed": True,
        "fields": {"proof": r["draft"] or r["proof"]},
        "local_check": r["check"], "check_file": r["check_file"], "escalation": r["escalation"],
        "note": "Proposed only. Local check is not a Checker PASS; Ops/the operator apply it.",
    }, ensure_ascii=False) + "\n" for r in rows)
    report["proposed_file"] = str(write_new(out_board / "proposed", f"{today}.jsonl", jl))
    s = [f"# Sovereign loop {today}{' (dry run)' if report['dry_run'] else ''}", ""]
    s += [f"- {r['id']}: local check {r['check']}, roundtable {r['advisory']}" +
          (f", escalation {r['escalation']}" if r["escalation"] else "") for r in rows]
    s.append(f"- handoffs written: {len(report['handoffs']['written'])}; unchanged: {len(report['handoffs']['unchanged'])}")
    report["summary_file"] = str(write_new(out_board / "proposed", f"{today}.summary.md", "\n".join(s) + "\n"))
