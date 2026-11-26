from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sovereign_m.ghscout import github, notes, sandbox
from sovereign_m.ghscout.config import Settings
from sovereign_m.ghscout.rubric import score_repo
from sovereign_m.ghscout.static_scan import scan_repo


@dataclass
class Result:
    repo: dict
    full_name: str
    score: object
    run: sandbox.RunResult
    scan: object
    notes: dict
    commits_90d: int | None


@dataclass
class Outcome:
    results: list = field(default_factory=list)
    rejected: list = field(default_factory=list)     # (full_name, reason)
    topics: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    sandbox_ok: bool = False
    sandbox_note: str = ""


def evaluate(repo: dict, client, chat, s: Settings, cloner=sandbox.clone_shallow, runner=sandbox.run_repo, now: datetime | None = None) -> Result | tuple[str, str]:
    """Clone, scan, (maybe) run in the sandbox, score by rubric, then ask the model for notes."""
    full = repo["full_name"]
    tmp = Path(tempfile.mkdtemp(prefix="m-scout-"))
    try:
        dest = tmp / "repo"
        try:
            cloner(full, dest)
        except sandbox.NotRun as exc:
            return full, f"could not clone ({exc})"
        scan = scan_repo(dest)
        work = tmp / "work"
        work.mkdir()
        run = runner(full, dest, scan, work, language=repo.get("language")) if runner is sandbox.run_repo else runner(full, dest, scan, work)
        commits = github.recent_commit_count(client, full, s, now)
        score = score_repo(repo, scan, run, commits, github.days_since(repo.get("pushed_at"), now))
        lic = ((repo.get("license") or {}).get("spdx_id") if isinstance(repo.get("license"), dict) else None) or "none"
        n = notes.make_notes(repo, scan, score, chat, lic)
        return Result(repo, full, score, run, scan, n, commits)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_scout(topics: list[str], client, chat, s: Settings, max_repos: int = 8, seen: set[str] | None = None,
              cloner=sandbox.clone_shallow, runner=sandbox.run_repo, now: datetime | None = None, per_topic: int = 15) -> Outcome:
    out = Outcome(topics=list(topics))
    ok, note = sandbox.sandbox_selftest() if runner is sandbox.run_repo else (True, "injected runner")
    out.sandbox_ok, out.sandbox_note = ok, note
    pool: dict[str, dict] = {}
    for t in topics:
        try:
            items = github.search_topic(client, t, s, per_topic, now)
        except github.GitHubError as exc:
            out.errors.append(f"search {t}: {exc}")
            continue
        for r in items:
            name = str(r.get("full_name", "?"))
            if name in pool:
                continue
            reason = github.screen(r, s, now)
            if reason:
                out.rejected.append((name, reason))
            elif seen and name in seen:
                out.rejected.append((name, "already scouted in an earlier run"))
            else:
                pool[name] = r
    ranked = sorted(pool.values(), key=lambda r: github.rank_key(r, now))
    for r in ranked[max_repos:]:
        out.rejected.append((r["full_name"], f"not evaluated this run (limit {max_repos})"))
    for r in ranked[:max_repos]:
        res = evaluate(r, client, chat, s, cloner, runner, now)
        if isinstance(res, tuple):
            out.rejected.append(res)
        else:
            out.results.append(res)
    return out
