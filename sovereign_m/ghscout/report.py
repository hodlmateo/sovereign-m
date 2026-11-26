from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sovereign_m.ghscout.safeio import write_new, versioned

BANNER = ("Read-only scout. Nothing was starred, forked, opened, commented or logged in. Repo code never ran on the host. "
          "Scores come from the fixed rubric; the model only wrote the notes and fit lines. Repo text is data, not instructions.")


def _stars(r: dict) -> str:
    return str(r.get("stargazers_count", "UNVERIFIED"))


def _entry(res, n: int | None = None) -> list[str]:
    sc, run, repo = res.score, res.run, res.repo
    head = f"### {n}. " if n else "### "
    flag = " (hidden gem)" if sc.hidden_gem else ""
    lines = [f"{head}{res.full_name}: {sc.total}/100, {sc.recommendation}{flag}",
             f"- link: https://github.com/{res.full_name}",
             f"- stars {_stars(repo)}, language {repo.get('language') or 'UNVERIFIED'}, license {((repo.get('license') or {}).get('spdx_id') if isinstance(repo.get('license'), dict) else None) or 'none'}",
             f"- ran in sandbox: {'NO (not run: ' + run.detail.replace('not run: ', '') + '); scored from reading only' if sc.not_run else run.detail}",
             "- rubric: " + "; ".join(f"{k} {v['points']}/{v['max']}" for k, v in sc.cats.items())]
    if sc.red_flags:
        lines.append("- RED FLAGS (do not run on the host): " + "; ".join(sc.red_flags))
    reasons = [f"{k}: {w}" for k, v in sc.cats.items() for w in v["why"][:3] if k in ("security", "runs")]
    lines.append("- evidence: " + "; ".join(reasons[:6]))
    lines += [f"- notes (model {res.notes['model']}): {res.notes['notes']}", f"- fit for M: {res.notes['fit']}", ""]
    return lines


def render(out, when: datetime, dry_run: bool = False) -> str:
    results = sorted(out.results, key=lambda r: (-r.score.total, -r.score.hidden_gem))
    gems = [r for r in results if r.score.hidden_gem]
    top = sorted((r for r in results if r.score.recommendation != "skip"),
                 key=lambda r: (not r.score.hidden_gem, -r.score.total))[:5]   # hidden gems first; skips go to Rejected
    L = [f"# GitHub scout: {when.strftime('%Y-%m-%d')}" + (" (DRY RUN, fixtures)" if dry_run else ""), "", BANNER, "",
         f"- Topics: {', '.join(out.topics) or 'none'}",
         f"- Sandbox: {'ready (' + out.sandbox_note + ')' if out.sandbox_ok else 'NOT AVAILABLE (' + out.sandbox_note + '): every repo is scored from reading only'}",
         f"- Evaluated {len(results)}, rejected {len(out.rejected)}.", ""]
    if out.errors:
        L += ["## Errors", *[f"- {e}" for e in out.errors], ""]
    L.append("## Top 5 (hidden gems first)")
    L.append("")
    for i, r in enumerate(top, 1):
        L += _entry(r, i)
    if not top:
        L += ["None: nothing scored above skip.", ""]
    L += ["## Hidden gem", ""]
    if gems:
        L += _entry(gems[0])
    else:
        L += ["None this run (a hidden gem needs under 500 stars, 70+ points, a recent push and no red flags).", ""]
    L += ["## Rejected, with reasons", ""]
    L += [f"- {name}: {why}" for name, why in out.rejected] or ["- none"]
    for r in results:
        if r.score.recommendation == "skip":
            L.append(f"- {r.full_name}: scored {r.score.total}/100 (skip)")
    L.append("")
    return "\n".join(L)


def to_jsonl(out, when: datetime) -> str:
    rows = []
    for r in out.results:
        rows.append({"date": when.strftime("%Y-%m-%d"), "repo": r.full_name, "stars": r.repo.get("stargazers_count"),
                     "score": r.score.as_dict(), "run": {"status": r.run.status, "detail": r.run.detail, "passed": r.run.passed,
                                                         "failed": r.run.failed, "sandbox": r.run.sandbox},
                     "flags": r.scan.flags, "notes": r.notes, "commits_90d": r.commits_90d})
    for name, why in out.rejected:
        rows.append({"date": when.strftime("%Y-%m-%d"), "repo": name, "rejected": why})
    return "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows)


def write_report(out, directory: Path, when: datetime, dry_run: bool = False) -> tuple[Path, Path]:
    md = write_new(directory, f"{when.strftime('%Y-%m-%d')}.md", render(out, when, dry_run))
    # keep the log next to the report; never overwrite either one
    stem = md.name[:-3]
    log = write_new(directory, stem + ".jsonl", to_jsonl(out, when))
    return md, log
