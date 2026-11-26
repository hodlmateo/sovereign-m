import json
import os
import subprocess
import time

import pytest

from sovereign_m.boardio import sha256_file
from sovereign_m.checker.cli import check_card, main
from sovereign_m.checker.verify import allowed_test_command, verify_proof


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    board = tmp_path / "board"
    (board / "proof").mkdir(parents=True)
    (board / "tasks.jsonl").write_text(json.dumps({"id": "c1", "title": "T", "status": "checking"}) + "\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True,
                                  env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    g("init", "-q")
    (repo / "a.py").write_text("x = 1\n")
    (repo / "test_a.py").write_text("def test_a():\n    assert True\n\ndef test_b():\n    assert True\n")
    g("add", "a.py", "test_a.py")
    g("commit", "-qm", "init")
    sha = g("rev-parse", "HEAD").stdout.strip()
    old = time.time() - 100
    for f in (repo / "a.py", repo / "test_a.py"):
        os.utime(f, (old, old))
    return board, repo, sha


def good_proof(repo, sha, extra=""):
    return (f"repo: {repo}\ncommit: {sha}\nfile: {repo}/a.py\nsha256: {sha256_file(repo / 'a.py')}  {repo}/a.py\n"
            f"tests-cmd: python3 -m pytest -q\ntests-passed: 2\n{extra}")


def test_pass_with_everything_verified(rig):
    board, repo, sha = rig
    (board / "proof" / "c1.md").write_text(good_proof(repo, sha))
    verdict, check, esc = check_card(board, "c1")
    assert verdict == "PASS" and esc is None
    assert "**PASS**" in check.read_text() and "2" in check.read_text()
    assert not (board / "escalations").exists()


def test_wrong_test_count_fails_and_escalates(rig):
    board, repo, sha = rig
    (board / "proof" / "c1.md").write_text(good_proof(repo, sha).replace("tests-passed: 2", "tests-passed: 9"))
    verdict, check, esc = check_card(board, "c1")
    assert verdict == "FAIL"
    text = esc.read_text()
    assert "Operator: send this to Grok or Claude to double-check" in text and "9" in text


def test_sha256_mismatch_and_missing_file(rig):
    board, repo, sha = rig
    p = board / "proof" / "c1.md"
    p.write_text(f"repo: {repo}\nsha256: {'0'*64}  {repo}/a.py\nfile: {repo}/nope.py\n")
    out = verify_proof(p)
    assert out.verdict == "FAIL"
    assert sum(not c.ok for c in out.checks) >= 2


def test_unknown_commit_fails(rig):
    board, repo, _ = rig
    p = board / "proof" / "c1.md"
    p.write_text(f"repo: {repo}\ncommit: {'a'*40}\n")
    assert verify_proof(p).verdict == "FAIL"


def test_commit_without_repo_cannot_verify(rig):
    board, repo, sha = rig
    p = board / "proof" / "c1.md"
    p.write_text(f"commit: {sha}\n")
    assert verify_proof(p).verdict == "FAIL"


def test_edit_after_proof_fails(rig):
    board, repo, sha = rig
    p = board / "proof" / "c1.md"
    p.write_text(good_proof(repo, sha))
    os.utime(p, (time.time() - 50, time.time() - 50))
    (repo / "a.py").write_text("x = 2\n")  # edited later (newer mtime) -> also sha mismatch
    out = verify_proof(p)
    assert out.verdict == "FAIL"
    assert any("after the proof" in c.detail for c in out.checks)


def test_dirty_tracked_files_fail(rig):
    board, repo, sha = rig
    p = board / "proof" / "c1.md"
    p.write_text(f"repo: {repo}\ncommit: {sha}\n")
    (repo / "a.py").write_text("changed\n")
    out = verify_proof(p)
    assert any("clean" in c.name and not c.ok for c in out.checks)


def test_missing_proof_and_empty_proof_and_unknown_card(rig):
    board, repo, sha = rig
    v, check, esc = check_card(board, "c1")  # no proof file
    assert v == "FAIL" and esc is not None
    with open(board / "tasks.jsonl", "a") as f:
        f.write(json.dumps({"id": "c2", "title": "T2"}) + "\n")
        f.write(json.dumps({"id": "c3", "title": "T3"}) + "\n")
    assert check_card(board, "ghost")[0] == "FAIL"  # unknown card
    (board / "proof" / "c2.md").write_text("just some prose, nothing verifiable\n")
    v2, check2, _ = check_card(board, "c2")
    assert v2 == "FAIL" and "can't verify" in check2.read_text()


def test_never_overwrites_and_never_touches_tasks(rig):
    board, repo, sha = rig
    before = (board / "tasks.jsonl").read_bytes()
    (board / "proof" / "c1.md").write_text("nothing\n")
    _, c1, e1 = check_card(board, "c1")
    _, c2, e2 = check_card(board, "c1")
    assert c1.name == "c1.check.md" and c2.name == "c1.check-v2.md"
    assert e1.name == "c1.md" and e2.name == "c1-v2.md"
    assert (board / "tasks.jsonl").read_bytes() == before


def test_test_command_allowlist():
    assert allowed_test_command("python3 -m pytest -q tests/test_x.py") == ["python3", "-m", "pytest", "-q", "tests/test_x.py"]
    for bad in ("rm -rf ~", "python3 -m pytest; rm x", "bash -c pytest", "pytest $(id)", "pytest --rootdir=/ -p evil"):
        assert allowed_test_command(bad) is None


def test_hostile_proof_text_is_not_executed(rig):
    board, repo, sha = rig
    marker = board / "pwned"
    p = board / "proof" / "c1.md"
    p.write_text(f"repo: {repo}\ntests-cmd: touch {marker}\ntests-passed: 1\n")
    assert verify_proof(p).verdict == "FAIL" and not marker.exists()


def test_service_state_checked(rig, monkeypatch):
    board, repo, sha = rig
    p = board / "proof" / "c1.md"
    p.write_text("service: definitely-not-a-real-unit.service active\n")
    assert verify_proof(p).verdict == "FAIL"


def test_cli_exit_codes(rig, capsys):
    board, repo, sha = rig
    (board / "proof" / "c1.md").write_text(good_proof(repo, sha))
    assert main(["c1", "--board", str(board)]) == 0
    (board / "proof" / "c3.md").write_text("x\n")
    assert main(["c3", "--board", str(board)]) == 1
