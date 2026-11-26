"""Fixed /100 rubric. Every point comes from a check on the repo or the API; the model never touches the score."""

from __future__ import annotations

from dataclasses import dataclass, field

from sovereign_m.ghscout.sandbox import RunResult
from sovereign_m.ghscout.static_scan import Scan

PERMISSIVE = {"MIT", "APACHE-2.0", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "ISC", "UNLICENSE", "0BSD", "ZLIB", "CC0-1.0"}
WEAK_COPYLEFT = {"MPL-2.0", "LGPL-2.1", "LGPL-3.0", "LGPL-2.1-ONLY", "LGPL-3.0-ONLY", "EPL-2.0"}
COPYLEFT = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "GPL-2.0-ONLY", "GPL-3.0-ONLY", "AGPL-3.0-ONLY", "GPL-2.0-OR-LATER", "GPL-3.0-OR-LATER"}
MAX = {"runs": 20, "tests": 15, "docs": 15, "license": 10, "activity": 15, "quality": 10, "security": 15}
PENALTY = {"curl|bash pipe": 5, "obfuscated code": 6, "prompt-injection text": 3, "setup.py runs code at install": 4}
RED = ("secret:", "obfuscated code", "curl|bash pipe")


@dataclass
class Score:
    total: int
    cats: dict
    not_run: bool
    red_flags: list = field(default_factory=list)
    recommendation: str = "skip"
    hidden_gem: bool = False

    def as_dict(self) -> dict:
        return {"total": self.total, "cats": self.cats, "not_run": self.not_run, "red_flags": self.red_flags,
                "recommendation": self.recommendation, "hidden_gem": self.hidden_gem}


def _cat(points: int, key: str, why: list[str]) -> dict:
    return {"points": max(0, min(points, MAX[key])), "max": MAX[key], "why": why}


def license_points(spdx: str | None, has_file: bool) -> tuple[int, str]:
    key = (spdx or "").upper()
    if key in PERMISSIVE:
        return 10, f"{spdx}: permissive open source"
    if key in WEAK_COPYLEFT:
        return 8, f"{spdx}: weak copyleft, open source"
    if key in COPYLEFT:
        return 6, f"{spdx}: copyleft open source (check before any reuse)"
    if key in ("", "NOASSERTION", "OTHER") and has_file:
        return 3, "license file present but not a recognised SPDX id (UNVERIFIED terms)"
    return 0, "no license: all rights reserved by default"


def score_repo(repo: dict, scan: Scan, run: RunResult, commits_90d: int | None, days_since_push: float | None) -> Score:
    cats: dict = {}
    # runs (20)
    st = run.status
    if st == "passed":
        pts, why = 20, [run.detail]
    elif st == "failed":
        total = run.passed + run.failed
        pts, why = (12 if total and run.passed / total >= 0.5 else 6), [run.detail]
    elif st == "no_tests":
        pts, why = 8, ["installed but pytest collected no tests"]
    else:
        pts, why = 0, [f"NOT RUN: {run.detail}"]
    cats["runs"] = _cat(pts, "runs", why)
    # tests (15)
    pts, why = 0, []
    if scan.test_files:
        pts += 10 if scan.test_files >= 5 else 8
        why.append(f"{scan.test_files} test files")
    else:
        why.append("no test files found")
    if scan.ci:
        pts += 5
        why.append("CI config present")
    cats["tests"] = _cat(pts, "tests", why)
    # docs (15)
    pts, why = 0, []
    if scan.readme_chars >= 300:
        pts += 5
        why.append(f"README {scan.readme_chars} chars")
    else:
        why.append("README missing or very short")
    if scan.readme_chars >= 1500:
        pts += 3
    if scan.readme_has_usage:
        pts += 4
        why.append("install/usage section")
    if scan.doc_signals:
        pts += 3
        why.append("docs or examples present")
    cats["docs"] = _cat(pts, "docs", why)
    # license (10)
    spdx = ((repo.get("license") or {}).get("spdx_id")) if isinstance(repo.get("license"), dict) else None
    pts, why1 = license_points(spdx, scan.license_file)
    cats["license"] = _cat(pts, "license", [why1])
    # activity (15)
    pts, why = 0, []
    if days_since_push is None:
        why.append("last push UNVERIFIED")
    elif days_since_push <= 14:
        pts += 8
        why.append(f"pushed {int(days_since_push)} days ago")
    elif days_since_push <= 45:
        pts += 6
        why.append(f"pushed {int(days_since_push)} days ago")
    elif days_since_push <= 90:
        pts += 4
        why.append(f"pushed {int(days_since_push)} days ago")
    else:
        why.append(f"last push {int(days_since_push)} days ago")
    if commits_90d is None:
        why.append("commit count UNVERIFIED")
    else:
        pts += 7 if commits_90d >= 20 else 5 if commits_90d >= 8 else 3 if commits_90d >= 3 else 0
        why.append(f"{commits_90d}{'+' if commits_90d >= 30 else ''} commits in 90 days")
    cats["activity"] = _cat(pts, "activity", why)
    # quality (10)
    pts, why = 0, []
    if scan.lint_config:
        pts += 4
        why.append("lint/type config present")
    if scan.src_lines:
        if scan.biggest_src_lines <= 1500:
            pts += 3
            why.append("no giant source file")
        elif scan.biggest_src_lines <= 3000:
            pts += 1
            why.append(f"largest source file {scan.biggest_src_lines} lines")
        else:
            why.append(f"largest source file {scan.biggest_src_lines} lines")
        ratio = scan.test_lines / scan.src_lines
        pts += 3 if ratio >= 0.2 else 1 if scan.test_lines else 0
        why.append(f"test/source line ratio {ratio:.2f}")
    else:
        why.append("no source files recognised (UNVERIFIED)")
    cats["quality"] = _cat(pts, "quality", why)
    # security (15, starts full, penalties)
    pen, why, red, charged = 0, [], [], set()
    for f in scan.flags:
        kind = f["kind"]
        if kind not in charged:                      # each kind of problem costs points once, however many files show it
            charged.add(kind)
            if kind.startswith("secret:"):
                pen += 8
            elif kind.startswith("npm "):
                pen += 4
            else:
                pen += PENALTY.get(kind, 0)
        if kind.startswith(RED):
            red.append(f"{kind} ({f['where']})")
        why.append(f"{kind} ({f['where']})")
    cats["security"] = _cat(15 - pen, "security", why or ["no red flags found by the static checks (not a guarantee)"])
    total = sum(c["points"] for c in cats.values())
    stars = int(repo.get("stargazers_count") or 0)
    not_run = st == "not_run"
    rec = "recommend" if total >= 75 else "watch" if total >= 55 else "skip"
    if rec == "recommend" and (red or st != "passed"):
        rec = "watch"            # a recommendation needs tests that actually passed in the sandbox and no red flag
    gem = stars < 500 and total >= 70 and cats["activity"]["points"] >= 8 and not red and st == "passed"
    return Score(total, cats, not_run, red, rec, gem)
