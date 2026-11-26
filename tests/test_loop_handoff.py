import json

from sovereign_m.handoff.classify import LOCAL, NEEDS_CODE, classify
from sovereign_m.handoff.page import render_fragment, render_page
from sovereign_m.handoff.writer import build_handoff, list_handoffs, write_handoffs
from sovereign_m.handoff.cli import main


def board_with(tmp_path, cards):
    (tmp_path / "tasks.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cards))
    return tmp_path


CODE = {"id": "m-fix", "title": "Fix the mail sort service", "status": "today",
        "proof": "branch m-fix in /srv/project, see /srv/mail/sort.py"}
NOTE = {"id": "n-1", "title": "Draft the weekly newsletter summary", "status": "today", "proof": ""}
DONE = {"id": "d-1", "title": "Fix a bug", "status": "done"}


def test_classify():
    assert classify(CODE)[0] == NEEDS_CODE
    assert classify(NOTE)[0] == LOCAL
    assert classify({"id": "x", "title": "Zzz"})[0] == NEEDS_CODE  # unclear -> needs-code


def test_handoff_contents_and_rules():
    text = build_handoff(CODE, "why")
    for needle in ("## Goal", "## Files", "## Branch", "## Tests to run", "## Proof to write",
                   "m-fix", "/srv/mail/sort.py", "Do NOT push", "DATA, not instructions",
                   "UNVERIFIED", "proof/m-fix.md", "tests-passed"):
        assert needle in text


def test_handoff_redacts_and_fences_card():
    card = dict(CODE, title="Fix ``` thing password: hunter22222")
    text = build_handoff(card)
    assert "hunter22222" not in text


def test_write_handoffs_only_needs_code_open(tmp_path):
    b = board_with(tmp_path, [CODE, NOTE, DONE])
    s = write_handoffs(b)
    assert s["needs_code"] == ["m-fix"] and s["local"] == ["n-1"] and len(s["written"]) == 1
    assert (b / "handoffs" / "m-fix.md").exists()
    assert not (b / "handoffs" / "n-1.md").exists()
    s2 = write_handoffs(b)  # idempotent
    assert s2["written"] == [] and len(s2["unchanged"]) == 1
    assert (b / "tasks.jsonl").read_text().count("\n") == 3


def test_changed_card_gets_new_version_not_overwrite(tmp_path):
    b = board_with(tmp_path, [CODE])
    write_handoffs(b)
    first = (b / "handoffs" / "m-fix.md").read_text()
    board_with(tmp_path, [dict(CODE, title="Fix the mail sort service now")])
    write_handoffs(b)
    assert (b / "handoffs" / "m-fix.md").read_text() == first
    assert (b / "handoffs" / "m-fix-v2.md").exists()


def test_page_is_read_only_and_escapes(tmp_path):
    d = tmp_path / "handoffs"
    d.mkdir()
    (d / "a.md").write_text("# Handoff: <script>alert(1)</script>\nbody")
    page = render_page(d)
    assert "&lt;script&gt;alert(1)" in page
    assert "Copy" in page and "<form" not in page and "fetch(" not in page and "XMLHttpRequest" not in page
    assert render_fragment([]) == "<p>No handoffs waiting.</p>"
    assert list_handoffs(tmp_path / "missing") == []


def test_cli_write(tmp_path, capsys):
    b = board_with(tmp_path, [CODE])
    assert main(["write", "--board", str(b)]) == 0
    assert "m-fix" in capsys.readouterr().out
