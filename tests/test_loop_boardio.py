import json

import pytest

from sovereign_m import boardio


def test_write_new_never_overwrites(tmp_path):
    a = boardio.write_new(tmp_path, "x.md", "one")
    b = boardio.write_new(tmp_path, "x.md", "two")
    c = boardio.write_new(tmp_path, "x.md", "three")
    assert [p.name for p in (a, b, c)] == ["x.md", "x-v2.md", "x-v3.md"]
    assert a.read_text() == "one" and b.read_text() == "two"


def test_write_new_unless_same_is_idempotent(tmp_path):
    p1, c1 = boardio.write_new_unless_same(tmp_path, "x.md", "same")
    p2, c2 = boardio.write_new_unless_same(tmp_path, "x.md", "same")
    p3, c3 = boardio.write_new_unless_same(tmp_path, "x.md", "different")
    assert (c1, c2, c3) == (True, False, True)
    assert p1 == p2 and p3.name == "x-v2.md"


def test_write_new_refuses_symlink_and_bad_names(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (tmp_path / "x.md").symlink_to(victim)
    p = boardio.write_new(tmp_path, "x.md", "new")
    assert victim.read_text() == "keep" and p.name == "x-v2.md"
    for bad in ("../x", "a/b", ".hidden"):
        with pytest.raises(ValueError):
            boardio.write_new(tmp_path, bad, "t")


def test_read_cards_latest_wins_and_skips_bad_lines(tmp_path):
    (tmp_path / "tasks.jsonl").write_text(
        '{"id":"a","status":"today"}\nnot json\n{"id":"a","status":"done"}\n{"id":"b","status":"waiting"}\n')
    assert {c["id"]: c["status"] for c in boardio.read_cards(tmp_path)} == {"a": "done", "b": "waiting"}
    assert [c["id"] for c in boardio.open_cards(tmp_path)] == ["b"]
    assert boardio.read_cards(tmp_path / "nope") == []


def test_redact_secrets():
    assert "sk-abcdefghijklmnopqrstuvwx" not in boardio.redact("key sk-abcdefghijklmnopqrstuvwx ok")
    assert "hunter22222" not in boardio.redact("password: hunter22222")
