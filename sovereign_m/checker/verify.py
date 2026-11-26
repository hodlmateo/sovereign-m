"""Evidence checks. Proof text is DATA: it is parsed, never executed.

The only command ever run is a test command that matches a strict allowlist
(pytest only, no shell, safe characters, cwd = a git repo under $HOME).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sovereign_m.boardio import sha256_file

TEST_TIMEOUT = 900
_SHA = re.compile(r"^(?:commit|sha)\s*:\s*([0-9a-f]{7,40})\b", re.IGNORECASE | re.MULTILINE)
_SHA256 = re.compile(r"^sha256\s*:\s*([0-9a-f]{64})\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_FILE = re.compile(r"^file\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_REPO = re.compile(r"^repo\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_TCMD = re.compile(r"^tests-cmd\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_TCOUNT = re.compile(r"^tests-passed\s*:\s*(\d+)", re.IGNORECASE | re.MULTILINE)
_SERVICE = re.compile(r"^service\s*:\s*([A-Za-z0-9@._:-]+)\s+(active|inactive|failed)\b", re.IGNORECASE | re.MULTILINE)
_ADVISORY = re.compile(r"^roundtable-advisory\s*:\s*(PASS|FAIL|NONE)\b", re.IGNORECASE | re.MULTILINE)
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./=:,@-]+$")
_PASSED = re.compile(r"(\d+) passed")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Outcome:
    checks: list[Check] = field(default_factory=list)
    claims: int = 0

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))

    @property
    def verdict(self) -> str:
        if self.claims == 0:
            return "FAIL"
        return "PASS" if all(c.ok for c in self.checks) else "FAIL"


def _path(text: str) -> Path:
    return Path(os.path.expanduser(text))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )


def _repo_ok(repo: Path) -> bool:
    home = Path.home().resolve()
    try:
        real = repo.resolve()
    except OSError:
        return False
    return real.is_dir() and real.is_relative_to(home) and _git(real, "rev-parse", "--git-dir").returncode == 0


def allowed_test_command(text: str) -> list[str] | None:
    """Return argv for an allowlisted pytest command, else None."""
    try:
        argv = shlex.split(text)
    except ValueError:
        return None
    if argv[:3] == ["python3", "-m", "pytest"] or argv[:3] == ["python", "-m", "pytest"]:
        rest = argv[3:]
    elif argv[:1] == ["pytest"]:
        rest = argv[1:]
    else:
        return None
    if not all(x in ("-q", "-x") or (_SAFE_ARG.match(x) and not x.startswith("-")) for x in rest):
        return None
    return ["python3", "-m", "pytest", *rest]


def verify_proof(proof_path: Path, runner=subprocess.run, now_skew: float = 1.0) -> Outcome:
    out = Outcome()
    try:
        text = proof_path.read_text(encoding="utf-8")
        proof_mtime = proof_path.stat().st_mtime
    except OSError as exc:
        out.add("proof file readable", False, f"{proof_path}: {exc.strerror}")
        return out
    if not text.strip():
        out.add("proof file non-empty", False, "empty proof")
        return out

    repos = []
    for raw in _REPO.findall(text):
        r = _path(raw)
        ok = _repo_ok(r)
        out.claims += 1
        out.add(f"repo {raw}", ok, "git repo under $HOME" if ok else "not a git repo under $HOME")
        if ok:
            repos.append(r.resolve())

    watched: list[Path] = []
    for raw in _FILE.findall(text):
        out.claims += 1
        p = _path(raw)
        ok = p.is_file()
        out.add(f"file exists {raw}", ok, "exists" if ok else "missing")
        if ok:
            watched.append(p)
    for digest, raw in _SHA256.findall(text):
        out.claims += 1
        p = _path(raw)
        if not p.is_file():
            out.add(f"sha256 {raw}", False, "file missing")
            continue
        actual = sha256_file(p)
        out.add(f"sha256 {raw}", actual == digest.lower(), "match" if actual == digest.lower() else f"MISMATCH actual {actual}")
        watched.append(p)

    for sha in _SHA.findall(text):
        out.claims += 1
        if not repos:
            out.add(f"commit {sha}", False, "no repo: line to resolve it in (can't verify)")
            continue
        found = [r for r in repos if _git(r, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0]
        out.add(f"commit {sha}", bool(found), f"in {found[0]}" if found else "not found in any listed repo")
        for r in found[:1]:
            ct = _git(r, "show", "-s", "--format=%ct", sha).stdout.strip()
            if ct.isdigit() and float(ct) > proof_mtime + now_skew:
                out.add(f"commit {sha} not after proof", False, "commit is newer than the proof file")

    for r in repos:
        st = _git(r, "status", "--porcelain", "--untracked-files=no")
        dirty = st.stdout.strip()
        out.add(f"{r} tracked files clean", not dirty, "clean" if not dirty else "uncommitted edits: " + dirty.replace("\n", "; ")[:200])

    for p in watched:
        newer = p.stat().st_mtime > proof_mtime + now_skew
        out.add(f"not edited after proof {p}", not newer, "ok" if not newer else "file modified after the proof was written")

    cmds = _TCMD.findall(text)
    counts = _TCOUNT.findall(text)
    if cmds or counts:
        out.claims += 1
        if not (cmds and counts):
            out.add("tests", False, "need both tests-cmd and tests-passed (can't verify)")
        elif not repos:
            out.add("tests", False, "no repo: line to run the tests in (can't verify)")
        else:
            argv = allowed_test_command(cmds[0].strip())
            if argv is None:
                out.add("tests", False, f"command not on the allowlist (pytest only): {cmds[0].strip()[:80]}")
            else:
                try:
                    res = runner(argv, cwd=str(repos[0]), capture_output=True, text=True, timeout=TEST_TIMEOUT)
                    m = _PASSED.findall(res.stdout)
                    got = int(m[-1]) if m else None
                    ok = res.returncode == 0 and got == int(counts[0])
                    out.add("tests re-run", ok, f"exit {res.returncode}, passed {got}, claimed {counts[0]}")
                except (subprocess.TimeoutExpired, OSError) as exc:
                    out.add("tests re-run", False, f"could not run: {type(exc).__name__}")

    for unit, state in _SERVICE.findall(text):
        out.claims += 1
        seen = []
        for scope in (["--user"], []):
            try:
                res = subprocess.run(["systemctl", *scope, "is-active", unit], capture_output=True, text=True, timeout=20)
                seen.append(res.stdout.strip() or "unknown")
            except (OSError, subprocess.TimeoutExpired):
                seen.append("unknown")
        actual = state.lower() if state.lower() in seen else seen[0]
        out.add(f"service {unit}", actual == state.lower(), f"claimed {state}, actual {actual}")

    adv = _ADVISORY.findall(text)
    if adv:
        out.add("roundtable advisory", adv[-1].upper() == "PASS", f"roundtable said {adv[-1].upper()}")
    return out
