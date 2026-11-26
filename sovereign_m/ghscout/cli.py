"""``python -m sovereign_m.ghscout run [--topic X] [--dry-run]``. Read-only on GitHub. Never prints a token or the llama key."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from sovereign_m.ghscout import fixtures, notes, pipeline, report, sandbox
from sovereign_m.ghscout.config import Settings, load_topics, scout_dir
from sovereign_m.ghscout.github import GitHubClient


def seen_repos(directory: Path) -> set[str]:
    seen = set()
    for f in directory.glob("*.jsonl") if directory.is_dir() else []:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("repo") and "score" in row:
                seen.add(row["repo"])
    return seen


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="m-gh-scout")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--topic", action="append", help="GitHub topic (repeatable); overrides the topics file")
    r.add_argument("--topics-file", type=Path)
    r.add_argument("--max-repos", type=int, default=8, help="repos to evaluate this run")
    r.add_argument("--dry-run", action="store_true", help="fixtures only: no GitHub, canned repos, stub model; output to a temp dir")
    r.add_argument("--hub", type=Path)
    sub.add_parser("sandbox-check", help="report whether the sandbox is usable; runs nothing from any repo")
    a = p.parse_args(argv)
    if a.cmd == "sandbox-check":
        ok, why = sandbox.sandbox_selftest()
        print(("READY: " if ok else "NOT AVAILABLE: ") + why)
        return 0 if ok else 1
    s = Settings()
    when = datetime.now()
    if a.dry_run:
        base = Path(tempfile.mkdtemp(prefix="m-gh-scout-dry-"))
        repos = fixtures.build_repos(base / "repos")
        out = pipeline.run_scout(a.topic or ["fixture-topic"], fixtures.FixtureClient(), fixtures.stub_chat, s, a.max_repos,
                                 cloner=fixtures.fixture_cloner(repos), now=fixtures.NOW)
        md, log = report.write_report(out, base / "out", when, dry_run=True)
    else:
        topics = load_topics(a.topic, a.hub, a.topics_file)
        if not topics:
            print("error: no valid topics (use --topic or a topics file)", file=sys.stderr)
            return 2
        directory = scout_dir(a.hub)
        client = GitHubClient()
        print(f"scouting {len(topics)} topic(s), {'authenticated' if client.authenticated else 'unauthenticated'} read-only API", file=sys.stderr)
        out = pipeline.run_scout(topics, client, notes.llm_chat, s, a.max_repos, seen=seen_repos(directory))
        md, log = report.write_report(out, directory, when)
    print(f"report: {md}\nlog: {log}")
    print(md.read_text(encoding="utf-8"))
    return 0
