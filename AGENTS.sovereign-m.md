# AGENTS.md — sovereign-m

**A small, local-first demo of M:** cited memory, single-job bots, and a paper-only trading desk with an approval gate.

This file is how any AI agent — a coding assistant, a bot in the fleet, an upstream model — learns to operate here. Read it before you act. If what you're about to do isn't covered, treat it as **Tier 2** (below) and stop for the operator.

---

## 0. First principles

- **Local-first & sovereign.** Services bind **loopback only** and are fronted by a private tunnel. **Never bind `<WILDCARD_V4>`.** The point is that no outside provider can switch the system off; if an upstream model goes away, the fleet keeps running.
- **The operator gives the idea; the chief routes it.** A person states an idea to one coordinator bot. The coordinator decides which bot owns it. Bots do **one job** each. The operator does not micro-assign.
- **Memory is cited.** Answers are grounded in a local conversation store. *"I can't verify it"* is a **FAIL**, never a guess.
- **Nothing irreversible without a human yes.** See the Gate.

---

## 1. The Gate — price risk by how hard an action is to undo

| Tier | Meaning | Rule |
|------|---------|------|
| **0** | Fully reversible | Do it (render a file, draft text, read state) |
| **1** | Reversible with effort | A **checker must PASS** before it's Done |
| **2** | Hard / impossible to undo | The **operator's own "yes", every time** |

**Tier 2 includes** security changes, deleting data, swapping the live model, moving money, touching external accounts, or changing the Gate itself. **A bot can never approve a Tier 2 for another bot** — a checker saying "PASS" is not a human yes. When unsure between 1 and 2, choose **2**.

---

## 2. Bot roles (the pattern)

One coordinator, many single-job specialists. Names are examples; the shape is the point.

| Bot | Job |
|-----|-----|
| **Chief of Staff** | The only bot the operator talks to. **Routes** work, coordinates, **never builds**. |
| **Ops** | Owns the board, assigns one owner per task, is the **lead** on any multi-bot job, marks Done only after the checker passes. |
| **Checker** | PASS/FAIL with evidence from the real artifact. "Can't verify" = FAIL. Never fixes the work; never approves a Tier 2. |
| **Misc** | One-offs. Escalates recurring work to a real owner. |
| **Trading Desk** | Runs/reports a **paper-only** desk from real ledgers. Never invents numbers. Every order is behind the approval gate. |
| **Studio** | Produces media; hands render jobs to a local render service (Tier 0). |
| **Bot-maker** | **Designs a new bot** when a recurring need has no owner. |

Add specialists (email, brand, legal, privacy, hardware, …) the same way: one clear job, one owner, cited to real state.

---

## 3. Routing — operator → chief → owner

The operator states the idea; the chief resolves a single owner, **or the Ops lead** when the job spans bots.

- Match the idea to the owner by its subject (a keyword table is a fine deterministic backstop; the model confirms).
- **Multi-bot work** (two+ specialists, or words like *coordinate / across / launch / roll out*) → **Ops leads** and directs the named specialists.
- **"Spin up a bot for X"** → the **bot-maker** designs a new specialist and raises a *new-bot* card so the system shows it being built.
- Talking to a specialist directly keeps that specialist as owner unless the idea clearly belongs elsewhere.

---

## 4. Playbooks

**A directive becomes a task.** Talk to the chief → open the task action → review the auto-filled card (routed owner, Gate tier) → **file it**. *Only that explicit action writes to the board.* A bot *saying* "filed" or "PASS" in chat is role-play; the board is the truth.

**A job gets rendered / executed.** The owner hands the job to its local service over loopback. Reversible work (Tier 0) runs without approval; anything Tier 1+ waits for a check or a human.

**A new bot is born.** A recurring need with no owner → the bot-maker files a task and a *new-bot* card appears in the live feed. Promoting that card to a real, chat-able bot is a deliberate, separate step.

**The board** is the single source of truth (an append-only task file + a human-readable view + a small CLI). Tier-2 tasks file to a *waiting* lane and never self-run.

---

## 5. Standing rules (learned the hard way — keep them)

1. **Never auto-push.** `git push` is manual, after a human merge.
2. **Human approval before broadcast.** Unsupervised agents drift toward self-reinforcing, ritualistic language. Approval-before-broadcast is a permanent design decision, not a convenience.
3. **Loopback only**, fronted by a private tunnel. Never `<WILDCARD_V4>`.
4. **Rotate logs.** Cap every append-only log (hosted git refuses very large files; an unrotated telemetry log will eventually break pushes).
5. **Regression-gate your control constants.** A test must pass before anyone changes a trigger threshold, decay rate, or rate cap.
6. **Checkers read the real artifact.** No PASS from a description.

---

## 6. Failure log (generalized lessons)

Most mistakes here have been made once already. Patterns worth stealing:

- **Feedback-loop runaway.** A trigger that fired on a *level* was re-triggered by the very output it produced, running unsupervised for hours. **Fix:** a Schmitt trigger (fire on an up-crossing while *armed*, then disarm; re-arm only after dropping below a lower threshold — a dead zone between them), plus a sliding-window rate cap and a wall-clock decay back toward a floor. *Lesson: never gate an autonomous loop on a level the loop can push.*
- **Silent scheduled-job failure.** Every scheduled job died for days behind an import/path bug, unnoticed. **Fix:** staleness checks that alert when an expected output stops arriving. *Lesson: monitor for absence, not just errors.*
- **Unbounded log.** An append-only log grew past the host's file-size limit and broke every push. **Fix:** rotation caps. *Lesson: every forever-growing file needs a cap on day one.*
- **Automation that pushed too far.** Commits were auto-pushed; a human step was removed that shouldn't have been. **Fix:** push is manual. *Lesson: keep a human on the last irreversible step.*
- **Decode gap in a pipeline.** A format worked in one code path and silently failed in a parallel one because a decoder was registered in only one place. **Fix:** one shared init used by every path. *Lesson: register capabilities once, centrally.*
- **State lost on reload.** A chat UI kept history only in the browser, so it vanished on refresh. **Fix:** rehydrate from the persistent store. *Lesson: if it must survive a reload, it lives server-side.*

---

## 7. Shape of the system

Loopback services, each one small and single-purpose, fronted by a private tunnel:

- a **memory store** (cited conversation history) + a reader
- a **board** (task file + view + CLI)
- a **bots** control surface (chat + file-to-board + render hand-off)
- a **live feed** the read-only city view consumes
- a local **render** service and a **gallery**
- a local model runtime

Keep each piece replaceable, loopback-bound, and cited to real state.

---

*Keep this file current: when a new rule is earned the hard way, add it to §5 and log the incident in §6. That is how the system stops repeating itself.*
