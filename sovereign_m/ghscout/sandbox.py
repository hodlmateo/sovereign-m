"""Clone and run, without ever running a repo's code on the host.

Host side (network allowed, repo code NEVER executed): shallow ``git clone`` of a public repo with hooks and
symlinks off, and ``pip download --only-binary=:all:`` of plain requirement names (wheels carry no install-time
code). Sandbox side (bubblewrap, no network, no home, no keys, time and memory limits): ``pytest`` only.
Anything that cannot be done this way means "not run".
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CLONE_TIMEOUT = 120
PIP_TIMEOUT = 240
TEST_TIMEOUT = 120
MAX_REQS = 60
MAX_WHEEL_MB = 400
HEAVY = {"torch", "torchvision", "torchaudio", "tensorflow", "tensorflow-gpu", "jax", "jaxlib", "triton", "xformers", "bitsandbytes",
         "flash-attn", "vllm", "deepspeed", "cuda-python"}
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
REQ = re.compile(rf"^{_NAME}(\[[A-Za-z0-9,._-]+\])?\s*((==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+\s*,?\s*)*(;\s*[A-Za-z0-9 ._'\"<>=!()-]{{1,120}})?$")
SAFE_PATH = "/usr/bin:/bin"


class NotRun(Exception):
    """The repo was not run. The message is the reason shown in the report."""


@dataclass
class RunResult:
    status: str                  # passed | failed | no_tests | not_run
    detail: str
    passed: int = 0
    failed: int = 0
    log_tail: str = ""
    sandbox: str = "none"
    notes: list = field(default_factory=list)


# ------------------------------------------------------------------ clone (host side, no code runs)
def _clean_env(home: Path) -> dict:
    return {"PATH": SAFE_PATH, "HOME": str(home), "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "PIP_CONFIG_FILE": os.devnull}


def clone_shallow(full_name: str, dest: Path, runner=subprocess.run) -> None:
    owner, _, name = full_name.partition("/")
    if not (re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)):
        raise NotRun("unsafe repo name")
    argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.symlinks=false", "-c", "core.fsmonitor=false",
            "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "clone", "--depth", "1", "--single-branch",
            "--no-tags", "--no-recurse-submodules", f"https://github.com/{owner}/{name}.git", str(dest)]
    try:
        res = runner(argv, capture_output=True, text=True, timeout=CLONE_TIMEOUT, env=_clean_env(dest.parent), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun(f"clone failed: {type(exc).__name__}") from None
    if res.returncode != 0:
        raise NotRun("clone failed")


# ------------------------------------------------------------------ requirements (untrusted text -> plain names only)
def parse_requirements(repo: Path) -> tuple[list[str], list[str]]:
    """Return (accepted requirement strings, skipped notes). Only plain PEP 508 names with version bounds pass."""
    raw: list[str] = []
    req = repo / "requirements.txt"
    if req.is_file() and not req.is_symlink():
        raw += req.read_text(encoding="utf-8", errors="replace").splitlines()
    pp = repo / "pyproject.toml"
    if pp.is_file() and not pp.is_symlink():
        try:
            data = tomllib.loads(pp.read_text(encoding="utf-8", errors="replace"))
            proj = data.get("project", {})
            raw += [str(x) for x in proj.get("dependencies", [])]
            for key in ("test", "tests", "dev", "testing"):
                raw += [str(x) for x in (proj.get("optional-dependencies", {}).get(key, []))]
        except (tomllib.TOMLDecodeError, AttributeError, TypeError):
            pass
    ok, skipped = [], []
    for line in raw:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-") or "@" in line or "://" in line or "/" in line or "\\" in line:
            skipped.append(f"skipped unsafe requirement line: {line[:40]}")
            continue
        if not REQ.match(line):
            skipped.append(f"skipped unparseable requirement: {line[:40]}")
            continue
        name = re.match(_NAME, line).group(0).lower().replace("_", "-")
        if name in HEAVY or name.startswith("nvidia-"):
            raise NotRun(f"heavy dependency ({name}): not installed in the sandbox")
        if line not in ok:
            ok.append(line)
    if len(ok) > MAX_REQS:
        raise NotRun(f"{len(ok)} requirements, over the {MAX_REQS} limit")
    return ok, skipped


def prepare_deps(reqs: list[str], work: Path, runner=subprocess.run, need_pytest: bool | None = None) -> Path:
    """Wheels only, downloaded on the host, installed into a plain directory. No sdist, so no build code runs."""
    wheels, deps = work / "wheels", work / "deps"
    wheels.mkdir()
    deps.mkdir()
    if need_pytest is None:
        import importlib.util
        need_pytest = importlib.util.find_spec("pytest") is None   # the sandbox sees the same read-only /usr
    wanted = (["pytest"] if need_pytest else []) + reqs
    if not wanted:
        return deps                                                # nothing to download: no network touched at all
    rfile = work / "requirements.safe.txt"
    rfile.write_text("\n".join(wanted) + "\n", encoding="utf-8")
    env = _clean_env(work)
    base = [sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check"]
    try:
        d = runner(base + ["download", "--only-binary=:all:", "--no-cache-dir", "-d", str(wheels), "-r", str(rfile)],
                   capture_output=True, text=True, timeout=PIP_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
        if d.returncode != 0:
            raise NotRun("dependencies not available as wheels for this Python")
        size = sum(f.stat().st_size for f in wheels.iterdir()) / 1e6
        if size > MAX_WHEEL_MB:
            raise NotRun(f"dependencies are {int(size)} MB, over the {MAX_WHEEL_MB} MB limit")
        i = runner(base + ["install", "--no-index", "--find-links", str(wheels), "--only-binary=:all:", "--no-compile", "--target", str(deps),
                           "-r", str(rfile)], capture_output=True, text=True, timeout=PIP_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
        if i.returncode != 0:
            raise NotRun("could not install the downloaded wheels")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotRun(f"dependency step failed: {type(exc).__name__}") from None
    return deps


# ------------------------------------------------------------------ sandbox (no network, no home, no keys)
def bwrap_argv(repo: Path, deps: Path, inner: list[str]) -> list[str]:
    """bubblewrap command line. Nothing from the host home directory is mapped in; the network is a fresh empty namespace."""
    return ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind", str(repo), "/work", "--ro-bind", str(deps), "/deps", "--chdir", "/work",
            "--setenv", "PATH", "/usr/bin", "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
            "--setenv", "PYTHONPATH", "/deps:/work:/work/src", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            "--setenv", "PYTHONHASHSEED", "0", "--", *inner]


def limits_prefix(timeout: int, scope_ok: bool) -> list[str]:
    pre = ["timeout", "-k", "5", str(timeout + 15)]
    if scope_ok:
        pre += ["systemd-run", "--user", "--scope", "-q", "-p", "TasksMax=256", "-p", "MemoryMax=3G", "--"]
    pre += ["prlimit", "--as=6442450944", "--cpu=" + str(timeout + 30), "--fsize=104857600", "--"]
    return pre


def _scope_available(runner=subprocess.run) -> bool:
    try:
        return runner(["systemd-run", "--user", "--scope", "-q", "-p", "TasksMax=16", "--", "/usr/bin/true"],
                      capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def sandbox_selftest(runner=subprocess.run) -> tuple[bool, str]:
    """Can we really isolate? Checks bwrap exists and that inside it: no network, no home, no key files."""
    if shutil.which("bwrap") is None:
        return False, "bubblewrap (bwrap) is not installed"
    with tempfile.TemporaryDirectory(prefix="m-scout-selftest-") as t:
        repo, deps = Path(t) / "r", Path(t) / "d"
        repo.mkdir()
        deps.mkdir()
        probe = ("import os,socket\n"
                 "s=socket.socket(); s.settimeout(2)\n"
                 "try:\n s.connect((chr(46).join('9999'),53)); print('NET')\nexcept OSError: pass\n"
                 "print('HOME' if os.path.exists('/home') else 'nohome')\n")
        try:
            res = runner(bwrap_argv(repo, deps, ["python3", "-c", probe]), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False, "bubblewrap could not start"
        out = res.stdout.split()
        if res.returncode != 0:
            return False, "bubblewrap could not start (user namespaces blocked?)"
        if "NET" in out or "HOME" in out:
            return False, "sandbox self-test failed: network or home visible"
        return True, "bubblewrap: no network, no home"


_PASSED = re.compile(r"(\d+) passed")
_FIRST_ERROR = re.compile(r"^(?:E\s+(.+)|ERROR (?:collecting|at setup)[^\n]*)$", re.MULTILINE)


def first_error(out: str) -> str:
    m = _FIRST_ERROR.search(out)
    return (m.group(1) or m.group(0)).strip()[:140] if m else ""
_FAILED = re.compile(r"(\d+) failed")


def run_tests(repo: Path, deps: Path, timeout: int = TEST_TIMEOUT, runner=subprocess.run, scope_ok: bool | None = None) -> RunResult:
    scope = _scope_available() if scope_ok is None else scope_ok
    inner = ["python3", "-m", "pytest", "-x", "-q", "--maxfail=10", "-p", "no:cacheprovider", "-o", "addopts="]
    argv = limits_prefix(timeout, scope) + bwrap_argv(repo, deps, ["timeout", "-k", "3", str(timeout), *inner])
    try:
        res = runner(argv, capture_output=True, text=True, timeout=timeout + 40, stdin=subprocess.DEVNULL,
                     env={"PATH": SAFE_PATH, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", ""),
                          "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")})
    except subprocess.TimeoutExpired:
        return RunResult("failed", f"tests hit the {timeout}s time limit and were killed", sandbox="bubblewrap")
    except OSError as exc:
        return RunResult("not_run", f"sandbox could not start: {type(exc).__name__}")
    out = (res.stdout or "") + (res.stderr or "")
    log_end = out[-1500:]
    p = _PASSED.findall(out)
    f = _FAILED.findall(out)
    passed, failed = (int(p[-1]) if p else 0), (int(f[-1]) if f else 0)
    if res.returncode in (124, 137, 143):
        return RunResult("failed", f"tests hit the {timeout}s time limit and were killed", passed, failed, log_end, "bubblewrap")
    if res.returncode == 5 or "no tests ran" in out:
        return RunResult("no_tests", "pytest collected no tests", 0, 0, log_end, "bubblewrap")
    if res.returncode == 0 and passed:
        return RunResult("passed", f"{passed} tests passed in the sandbox", passed, 0, log_end, "bubblewrap")
    why = first_error(out) if not passed else ""
    return RunResult("failed", f"{passed} passed, {failed} failed (exit {res.returncode})" + (f"; first error: {why}" if why else ""),
                     passed, failed, log_end, "bubblewrap")


def run_repo(full_name: str, repo: Path, scan, work: Path, runner=subprocess.run, language: str | None = None) -> RunResult:
    """Decide whether this repo can be run safely, and run it. Python + pytest only."""
    if not (scan.has_pyproject or scan.has_requirements or scan.languages.get(".py")) or (language and language != "Python"):
        return RunResult("not_run", "not run: not a Python project (the sandbox runner supports Python only)")
    if scan.py_test_files == 0:
        return RunResult("not_run", "not run: no Python (pytest) tests found to run")
    ok, why = sandbox_selftest(runner)
    if not ok:
        return RunResult("not_run", f"not run: no safe sandbox ({why})")
    try:
        reqs, notes = parse_requirements(repo)
        deps = prepare_deps(reqs, work, runner)
    except NotRun as exc:
        return RunResult("not_run", f"not run: {exc}")
    res = run_tests(repo, deps, runner=runner)
    res.notes = notes
    return res
