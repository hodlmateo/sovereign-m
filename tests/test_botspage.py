import json
import os
import threading
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from sovereign_m.botspage import actions, data, web
from sovereign_m.botspage.cli import make_handler
from sovereign_m.roundtable.model import ModelError, Reply, StubModel

H = {"X-M-Bots": "1", "Content-Type": "application/json", "Host": "rig"}


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "_sysctl", lambda *a, **k: "")
    monkeypatch.setattr(data, "_journal", lambda *a, **k: "")
    monkeypatch.setattr(actions, "HEAT_FLAG", tmp_path / "nohot")
    monkeypatch.setattr(actions, "ASK_M", tmp_path / "no-ask-m")
    b = tmp_path / "m_hub" / "board"
    for d in ("roundtable/drafts", "proof", "handoffs", "escalations"):
        (b / d).mkdir(parents=True)
    cards = [
        {"id": "ops-1", "title": "Write standup", "owner": "Ops", "status": "today", "opened": "2026-11-20", "gate_tier": 0},
        {"id": "m-fix", "title": "Fix service", "owner": "Email", "status": "waiting", "opened": "2026-11-19", "gate_tier": 1, "proof": "password: hunter22222 branch x"},
        {"id": "old", "title": "Ancient", "owner": "Ops", "status": "done", "opened": "2020-01-01"},
    ]
    (b / "tasks.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cards))
    (b / "roundtable/drafts/ops-1-draft.md").write_text("draft body")
    (b / "proof/ops-1.check.md").write_text("# check\n**PASS**\n- ok")
    (b / "proof/m-fix.check.md").write_text("**FAIL**")
    (b / "handoffs/m-fix.md").write_text("# Handoff: m-fix\nbody")
    (b / "escalations/m-fix.md").write_text("# Operator: send this to Grok or Claude to double-check\n")
    (b / "roundtable/drafts/m-fixed-draft.md").write_text("other card's file")
    (tmp_path / "m_hub" / "brief" / "daylog").mkdir(parents=True)
    (tmp_path / "m_hub" / "brief" / "daylog" / "2026-11-20.md").write_text("# Daylog\nhello <b>")
    return b


def call(board, method, path, body=None, headers=H, model=None, out=None):
    code, hdr, out_b = web.handle(method, path, headers, json.dumps(body).encode() if body is not None else b"",
                                  board, out or board, model)
    return code, hdr, out_b


def jcall(*a, **k):
    code, hdr, b = call(*a, **k)
    return code, json.loads(b)


def test_page_and_redirect(board):
    code, hdr, b = call(board, "GET", "/bots/")
    assert code == 200 and b"<title>M Bots</title>" in b and b"viewport-fit=cover" in b
    assert call(board, "GET", "/bots")[0] == 301
    assert web.is_bots_path("/bots") and web.is_bots_path("/bots/api/x") and not web.is_bots_path("/botsy")


def test_thread_has_that_bots_artifacts_only_oldest_first(board):
    code, t = jcall(board, "GET", "/bots/api/thread?bot=ops")
    kinds = [(i["kind"], i.get("card")) for i in t["items"]]
    assert ("draft", "ops-1") in kinds and ("check", "ops-1") in kinds
    assert not any(c == "m-fix" for _k, c in kinds)       # Email's card, files not in Ops thread
    assert not any(i.get("card") == "old" for i in t["items"])  # old done card dropped
    ts = [i["ts"] for i in t["items"]]
    assert ts == sorted(ts)
    chk = [i for i in t["items"] if i["kind"] == "check"][0]
    assert chk["verdict"] == "PASS" and chk["level"] == "ok"


def test_email_thread_has_fail_handoff_escalation_and_redaction(board):
    _c, t = jcall(board, "GET", "/bots/api/thread?bot=email")
    by = {i["kind"]: i for i in t["items"]}
    assert by["check"]["verdict"] == "FAIL" and by["check"]["level"] == "fail"
    assert "copy" in by["handoff"] and "escalation" in by
    assert "hunter22222" not in json.dumps(t)
    assert not any("m-fixed" in i["title"] for i in t["items"])  # prefix collision guarded


def test_unknown_bot_and_bad_paths(board):
    assert jcall(board, "GET", "/bots/api/thread?bot=nope")[0] == 404
    assert call(board, "GET", "/bots/daylog?date=../../etc/passwd")[0] == 404
    assert call(board, "GET", "/bots/api/zzz")[0] == 404
    assert call(board, "DELETE", "/bots/api/top")[0] == 405


def test_top_bar(board, monkeypatch):
    monkeypatch.setattr(data, "failed_units", lambda: [{"unit": "m-mail-nightly-sort.service"}])
    _c, top = jcall(board, "GET", "/bots/api/top")
    types = sorted(w["type"] for w in top["waiting"])
    assert types == ["escalation", "gate", "handoff"]
    assert top["failed"] == [{"unit": "m-mail-nightly-sort.service"}]
    assert top["daylog"]["exists"] is True


def test_daylog_is_escaped(board):
    code, hdr, b = call(board, "GET", "/bots/daylog?date=2026-11-20")
    assert code == 200 and b"&lt;b&gt;" in b and b"<b>" not in b


def test_post_requires_header_and_json_and_same_origin(board):
    body = {"bot": "ops", "text": "hi"}
    assert call(board, "POST", "/bots/api/message", body, headers={"Content-Type": "application/json"})[0] == 403
    assert call(board, "POST", "/bots/api/message", body, headers={"X-M-Bots": "1", "Content-Type": "text/plain"})[0] == 403
    assert call(board, "POST", "/bots/api/message", body, headers={**H, "Origin": "https://evil.example"})[0] == 403
    assert call(board, "POST", "/bots/api/message", body, headers={**H, "Origin": "http://rig"})[0] != 403
    big, _ = web.handle("POST", "/bots/api/message", H, b"x" * 9000, board, board, None)[0], 0
    assert big == 413


def test_message_to_specialist_becomes_proposed_card_only(board, tmp_path):
    out = tmp_path / "out"
    before = (board / "tasks.jsonl").read_bytes()
    code, r = jcall(board, "POST", "/bots/api/message", {"bot": "studio", "text": "make a newsletter draft"},
                    model=StubModel(["should not be called"]), out=out)
    assert code == 200 and "proposed card" in r["reply"]["body"]
    files = list((out / "proposed").glob("*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text())
    assert row["owner"] == "Studio" and row["status"] == "proposed" and row["route"] == "M to route"
    assert (board / "tasks.jsonl").read_bytes() == before


def test_message_to_m_uses_model_memory_and_never_sends(board, tmp_path):
    out = tmp_path / "out"
    stub = StubModel(["Short answer."])
    code, r = jcall(board, "POST", "/bots/api/message", {"bot": "m-systems", "text": "what is waiting?"},
                    model=stub, out=out)
    assert r["reply"]["body"] == "Short answer." and "copy" not in r["reply"]
    prompt = stub.calls[0]["user"]
    assert "UNTRUSTED_DATA" in prompt and "The operator says: what is waiting?" in prompt
    _c, t = jcall(board, "GET", "/bots/api/thread?bot=m-systems", out=out)
    assert [i["kind"] for i in t["items"] if i["kind"] in ("user", "m")] == ["user", "m"]


def test_m_needs_code_gives_handoff_text_and_file(board, tmp_path):
    out = tmp_path / "out"
    stub = StubModel(["That is a code change.\nNEEDS_CODE: add a retry to the mail sort"])
    _c, r = jcall(board, "POST", "/bots/api/message", {"bot": "m-systems", "text": "please fix the mail sort"},
                  model=stub, out=out)
    rep = r["reply"]
    assert "NEEDS_CODE" not in rep["body"] and "Do NOT push" in rep["copy"] and "add a retry" in rep["copy"]
    assert len(list((out / "handoffs").glob("chat-*.md"))) == 1


def test_model_down_is_reported_not_crashed(board, tmp_path):
    class Down:
        def chat(self, *a, **k):
            raise ModelError("llama down")
    _c, r = jcall(board, "POST", "/bots/api/message", {"bot": "m-systems", "text": "hello"}, model=Down(), out=tmp_path / "o")
    assert r["reply"]["level"] == "fail" and "llama down" in r["reply"]["body"]


def test_empty_and_unknown_bot_message(board):
    assert jcall(board, "POST", "/bots/api/message", {"bot": "ops", "text": "  "}, model=StubModel(["x"]))[0] == 400
    assert jcall(board, "POST", "/bots/api/message", {"bot": "zzz", "text": "hi"}, model=StubModel(["x"]))[0] == 400


def reply(items):
    return StubModel([json.dumps(items)])


def test_suggestions_cap_window_and_no_invented_cards(board, tmp_path):
    out = tmp_path / "out"
    now = datetime(2026, 11, 20, 10, 0)
    items = [{"card": "ops-1", "do": "run the standup", "because": "it is due"},
             {"card": "ghost", "do": "invent", "because": "x"},
             {"card": "m-fix", "do": "fix the mail timeout", "because": "it failed today"},
             {"card": "ops-1", "do": "third", "because": "y"},
             {"card": "ops-1", "do": "fourth", "because": "z"}]
    made = actions.make_suggestions(board, out, reply(items), now=now, heat_flag=tmp_path / "nohot")
    assert len(made) == 3
    assert "ghost" not in "".join(p.read_text() for p in made)
    assert actions.make_suggestions(board, out, reply(items), now=now, heat_flag=tmp_path / "nohot") == []  # cap 3 undecided
    assert actions.make_suggestions(board, out, reply(items), now=datetime(2026, 11, 20, 23, 0), heat_flag=tmp_path / "nohot") == []
    assert actions.make_suggestions(board, out, reply(items), now=datetime(2026, 11, 20, 6, 59), heat_flag=tmp_path / "nohot") == []
    hot = tmp_path / "hot"
    hot.write_text("")
    assert actions.make_suggestions(board, tmp_path / "o2", reply(items), now=now, heat_flag=hot) == []


def test_suggestion_garbage_reply_makes_nothing(board, tmp_path):
    assert actions.make_suggestions(board, tmp_path / "o", StubModel(["no json here"]), now=datetime(2026, 11, 20, 10, 0),
                                    heat_flag=tmp_path / "nohot") == []


def test_approve_writes_only_a_proposed_card_and_is_once(board, tmp_path):
    out = tmp_path / "out"
    (made,) = actions.make_suggestions(board, out, reply([{"card": "ops-1", "do": "run it", "because": "due"}]),
                                       now=datetime(2026, 11, 20, 10, 0), heat_flag=tmp_path / "nohot")
    sid = json.loads(made.read_text())["id"]
    before = (board / "tasks.jsonl").read_bytes()
    code, r = jcall(board, "POST", f"/bots/api/suggestion/{sid}/approve", {}, out=out)
    assert code == 200 and r["proposed_file"]
    row = json.loads(Path(r["proposed_file"]).read_text())
    assert row["status"] == "proposed" and row["owner"] == "M Systems" and row["related_card"] == "ops-1"
    assert jcall(board, "POST", f"/bots/api/suggestion/{sid}/not-now", {}, out=out)[0] == 409
    _c, t = jcall(board, "GET", "/bots/api/thread?bot=m-systems", out=out)
    assert [i["decision"] for i in t["items"] if i["kind"] == "suggestion"] == ["approve"]
    assert (board / "tasks.jsonl").read_bytes() == before
    assert len(actions.pending_suggestions(out)) == 0


def test_not_now_writes_no_card_and_bad_ids_rejected(board, tmp_path):
    out = tmp_path / "out"
    (made,) = actions.make_suggestions(board, out, reply([{"card": "ops-1", "do": "a", "because": "b"}]),
                                       now=datetime(2026, 11, 20, 10, 0), heat_flag=tmp_path / "nohot")
    sid = json.loads(made.read_text())["id"]
    code, r = jcall(board, "POST", f"/bots/api/suggestion/{sid}/not-now", {}, out=out)
    assert code == 200 and r["proposed_file"] is None and not (out / "proposed").exists()
    assert jcall(board, "POST", "/bots/api/suggestion/../../x/approve", {}, out=out)[0] == 404
    assert actions.decide(out, "20260101-000000-suggestion9", "approve")["ok"] is False


def test_real_http_server_roundtrip(board, tmp_path):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0" + ".1", 0), make_handler(board, tmp_path / "o", StubModel(["hi there"])))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://" + srv.server_address[0] + f":{srv.server_address[1]}"
    try:
        assert b"M Bots" in urllib.request.urlopen(base + "/bots/").read()
        req = urllib.request.Request(base + "/bots/api/message", data=json.dumps({"bot": "m-systems", "text": "yo"}).encode(),
                                     headers={"Content-Type": "application/json", "X-M-Bots": "1"})
        assert json.load(urllib.request.urlopen(req))["reply"]["body"] == "hi there"
    finally:
        srv.shutdown()


def test_jarvis_patch_is_additive_and_lazy():
    p = Path(__file__).resolve().parents[1] / "deploy" / "jarvis" / "server-bots-route.patch"
    text = p.read_text()
    removed = [l for l in text.splitlines() if l.startswith("-") and not l.startswith("---")]
    assert removed == []
    assert "from sovereign_m.botspage import jarvis" in text and "except Exception" in text
