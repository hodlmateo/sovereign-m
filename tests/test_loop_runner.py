import json

from sovereign_m.roundtable.model import StubModel
from sovereign_m.runner.cli import main
from sovereign_m.runner.loop import run_loop, stub_for

NOTE = {"id": "n-1", "title": "Draft the weekly newsletter summary", "status": "today", "proof": ""}
CODE = {"id": "m-fix", "title": "Fix the mail sort service", "status": "today", "proof": ""}
DONE = {"id": "d-1", "title": "Draft a note", "status": "done"}


def mk(tmp_path, cards):
    b = tmp_path / "board"
    b.mkdir()
    (b / "tasks.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cards))
    return b


def test_local_card_drafted_checked_and_proposed(tmp_path):
    b = mk(tmp_path, [NOTE, CODE, DONE])
    before = (b / "tasks.jsonl").read_bytes()
    rep = run_loop(b, b, stub_for, dry_run=True, today="2026-11-20", heat_flag=tmp_path / "nohot")
    assert rep["handoffs"]["needs_code"] == ["m-fix"] and rep["local_open"] == ["n-1"]
    (r,) = rep["local_results"]
    assert r["advisory"] == "PASS" and r["check"] == "PASS" and r["escalation"] is None
    assert (b / "roundtable" / "drafts" / "n-1-draft.md").exists()
    rows = [json.loads(x) for x in (b / "proposed" / "2026-11-20.jsonl").read_text().splitlines()]
    assert rows[0]["id"] == "n-1" and "status" not in rows[0]["fields"] and rows[0]["proposed"] is True
    assert "n-1" in (b / "proposed" / "2026-11-20.summary.md").read_text()
    assert (b / "tasks.jsonl").read_bytes() == before  # never edited


def test_second_run_does_not_redo_card(tmp_path):
    b = mk(tmp_path, [NOTE])
    run_loop(b, b, stub_for, dry_run=True, today="2026-11-20", heat_flag=tmp_path / "nohot")
    rep = run_loop(b, b, stub_for, dry_run=True, today="2026-11-20", heat_flag=tmp_path / "nohot")
    assert rep["local_results"] == [] and "already drafted" in rep["skipped"][0]
    assert len(list((b / "proposed").glob("*.jsonl"))) == 1


def test_proposed_file_never_overwritten(tmp_path):
    b = mk(tmp_path, [NOTE, dict(NOTE, id="n-2")])
    run_loop(b, b, stub_for, max_cards=1, dry_run=True, today="D", heat_flag=tmp_path / "nohot")
    run_loop(b, b, stub_for, max_cards=1, dry_run=True, today="D", heat_flag=tmp_path / "nohot")
    assert sorted(p.name for p in (b / "proposed").glob("*.jsonl")) == ["D-v2.jsonl", "D.jsonl"]


def test_draftless_run_fails_check_and_escalates(tmp_path):
    b = mk(tmp_path, [NOTE])
    def script(role, system, user, i):
        n = i // 3
        if role == "ops":
            return f"from: ops\nto: worker\nobjective: a{n}\nartifacts: none\ndecisions: d{n}\nopen questions: none"
        if role == "worker":
            return f"from: worker\nto: checker\nobjective: b{n}\nartifacts: none\ndecisions: w{n}\nopen questions: none"
        return f"from: checker\nto: ops\nobjective: c{n}\nartifacts: none\ndecisions: k{n}\nopen questions: none\nverdict: PASS"
    rep = run_loop(b, b, lambda cid: StubModel(script), today="D", heat_flag=tmp_path / "nohot")
    (r,) = rep["local_results"]
    assert r["check"] == "FAIL" and r["escalation"]
    assert "double-check" in open(r["escalation"]).read()


def test_model_error_is_fail_not_crash(tmp_path):
    from sovereign_m.roundtable.model import ModelError
    class Boom:
        backend_label = "boom"
        def chat(self, *a, **k):
            raise ModelError("down")
    b = mk(tmp_path, [NOTE, dict(NOTE, id="n-2")])
    rep = run_loop(b, b, lambda cid: Boom(), today="D", heat_flag=tmp_path / "nohot")
    assert len(rep["local_results"]) == 1 and rep["local_results"][0]["check"] == "FAIL"
    assert any("model error" in s for s in rep["skipped"])


def test_heat_flag_skips_model(tmp_path):
    b = mk(tmp_path, [NOTE])
    hot = tmp_path / "hot"
    hot.write_text("")
    rep = run_loop(b, b, stub_for, today="D", heat_flag=hot)
    assert rep["local_results"] == [] and "heat" in rep["skipped"][0]


def test_max_cards_cap(tmp_path):
    b = mk(tmp_path, [dict(NOTE, id=f"n-{i}") for i in range(4)])
    rep = run_loop(b, b, stub_for, max_cards=2, dry_run=True, today="D", heat_flag=tmp_path / "nohot")
    assert len(rep["local_results"]) == 2


def test_cli_dry_run_writes_only_to_temp(tmp_path, capsys):
    b = mk(tmp_path, [NOTE, CODE])
    snapshot = sorted(p.name for p in b.iterdir())
    assert main(["run", "--board", str(b), "--dry-run"]) == 0
    assert sorted(p.name for p in b.iterdir()) == snapshot
    assert "out board:" in capsys.readouterr().out


def test_systemd_files_present_and_not_installed():
    from pathlib import Path
    d = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
    t = (d / "m-sovereign-loop.timer").read_text()
    assert "07..21:00,30:00 America/New_York" in t and "22:00:00 America/New_York" in t
    assert "Type=oneshot" in (d / "m-sovereign-loop.service").read_text()
