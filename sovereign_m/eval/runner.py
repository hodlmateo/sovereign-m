from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from sovereign_m.boardio import redact
from sovereign_m.eval.tasks import SYSTEM, TASKS
from sovereign_m.roundtable.model import ModelError


@dataclass
class TaskResult:
    id: str
    name: str
    passed: bool
    detail: str
    answer: str
    seconds: float
    backend: str


@dataclass
class ModelRun:
    label: str
    results: list[TaskResult] = field(default_factory=list)
    error: str = ""

    @property
    def score(self) -> int:
        return sum(r.passed for r in self.results)


def run_model(label: str, model, expect_backend: str | None = None, max_tokens: int = 300) -> ModelRun:
    """Run all ten tasks. A model error fails that task ("can't verify" = fail). The answering
    backend is recorded; if it is not the one we meant (silent fallback) the task fails."""
    run = ModelRun(label)
    for t in TASKS:
        t0 = time.monotonic()
        try:
            reply = model.chat("eval", SYSTEM, t.prompt, max_tokens)
            ok, detail = t.check(reply.text)
            backend, answer = reply.backend, reply.text
            if expect_backend and not backend.startswith(expect_backend):
                ok, detail = False, f"answered by {backend}, not {expect_backend} (fallback)"
        except ModelError as exc:
            ok, detail, backend, answer = False, f"model error: {exc}", "none", ""
            run.error = str(exc)
        run.results.append(TaskResult(t.id, t.name, ok, detail, redact(answer.strip())[:240],
                                      round(time.monotonic() - t0, 1), backend))
    return run


def render_report(runs: list[ModelRun], when: datetime | None = None, notes: str = "") -> str:
    when = when or datetime.now()
    out = [f"# Ten-task self-eval", "", f"Run {when.strftime('%Y-%m-%d %H:%M')} on the rig. Deterministic checks, no model as judge.",
           "Task 10 is an abstention test: the only correct answer is \"I cannot tell\". A model error or a fallback counts as FAIL.", ""]
    out.append("| task | " + " | ".join(r.label for r in runs) + " |")
    out.append("|---|" + "---|" * len(runs))
    for i, t in enumerate(TASKS):
        out.append(f"| {t.id} {t.name} | " + " | ".join(("PASS" if r.results[i].passed else "FAIL") for r in runs) + " |")
    out.append("| **score** | " + " | ".join(f"**{r.score}/10**" for r in runs) + " |")
    for r in runs:
        out += ["", f"## {r.label}", ""]
        if r.error:
            out.append(f"- model error seen: {r.error}")
        for x in r.results:
            out.append(f"- {x.id} {x.name}: {'PASS' if x.passed else 'FAIL'} ({x.detail}; {x.seconds}s; {x.backend})")
            out.append(f"  - answer: `{x.answer.replace(chr(96), chr(39)).replace(chr(10), ' / ')[:200]}`")
    if notes:
        out += ["", "## Notes", "", notes]
    return "\n".join(out) + "\n"
