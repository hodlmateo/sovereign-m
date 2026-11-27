# sovereign-m

**Paper trading demo, not financial advice.**

sovereign-m (formerly m-lite) is a small public demo of a local-first personal agent stack. The real stack is private. This repo is invented sample data plus the patterns around it, so a developer or a coding agent can read the code and copy the shape.

The notes, the bots, and the prices are fiction. The program runs offline. Runtime dependencies are the Python standard library.

## Quickstart

```bash
python -m sovereign_m prove
python -m pytest
python -m sovereign_m "when do we water the fern"
python -m sovereign_m --who "draft and revise the markdown note"
python -m sovereign_m desk run
python -m sovereign_m.modelguard demo
```

`python -m pytest` needs pytest (`pip install -e ".[dev]"`). The demo commands do not.

Installing the package exposes the same program as `ask-m`:

```bash
pip install -e .
ask-m prove
```

## The ideas

### One job per bot

`data/registry.json` lists five fictional bots. Each `job` is a single line.

| Bot | Job |
| --- | --- |
| quill | Draft and revise markdown notes |
| archivist | Answer from local notes and cite the source |
| ledger | Run the paper desk on synthetic prices |
| porter | Stage imports until a human approves the merge |
| warden | Veto paper entries that break risk, cooldown, or size rules |

A task is scored against that one job, plus a few hints. The best score at or above the threshold (0.45) and above zero owns the task. Otherwise the owner is none.

### Anti-jobs

An anti-job is work the bot must refuse even when the task also sounds like its job. If the task shares two or more content words with any anti-job, that bot is out of the running: two points come off the score, which a normal match cannot earn back.

"Draft and revise the markdown note and place paper trades" does not go to quill. Placing paper trades is one of quill's anti-jobs, so ledger gets it. The arithmetic is in `sovereign_m/registry/router.py`.

### Local memory with citations

`ask` reads markdown under `data/notes/` and the SQLite seed in `data/sample_notes.sql` (loaded in memory at startup). Approved imports, if you have any, are read from the local workspace. The staging queue is not readable.

Retrieval is lexical. It does not need a model. A chunk is accepted when it shares two or more terms and those terms cover at least a third of the question, or when the question is a single term that hits. The reply is a sentence copied from the winning chunk. Every reply lists its top sources. A miss starts with `I don't know.` and still lists the closest notes, marked below the cutoff, so the refusal is visible.

Try the SQLite note with:

```bash
python -m sovereign_m "where does the fictional pantry keep flake salt"
```

That answer is cited as `sqlite:pantry-salt`.

Optional local model: if `OLLAMA_HOST` is set, or an `ollama` program is on PATH, embeddings may reorder the notes and a generate call may rephrase the sentence. A rephrase is kept only when every content word is already in the cited note. Embeddings cannot turn a miss into a hit, and a miss never asks the model to phrase an answer. `--no-ollama` skips the model. With the variable unset and no `ollama` program, nothing is contacted. `SOVEREIGN_M_EMBED_MODEL` and `SOVEREIGN_M_ANSWER_MODEL` override the model names.

### Approval gates

Imports land in a staging queue. `approve` is the only step that merges one into memory: a markdown file and a SQLite row. The staged text is bound as a SQL parameter. `reject` drops the item. Nothing in the gate approves itself.

```bash
python -m sovereign_m gate stage --title "Quartz sparrow" --body "The quartz sparrow sleeps in the sample drawer."
python -m sovereign_m gate list
python -m sovereign_m "where does the quartz sparrow sleep"
python -m sovereign_m gate approve ID
```

The question returns `I don't know.` until the approve step. The queue lives in `var/`, which is gitignored.

### Paper-only trading

**Paper trading demo, not financial advice.**

The desk reads `data/prices.csv`, a synthetic tape for the fictional symbol PIP. Four roles vote on each bar:

- **trend** proposes a long when the close is above a short average
- **skeptic** rejects a bar that chases
- **sizer** names a size from how many recent closes rose
- **risk** can veto

A veto stops the entry even when trend and skeptic both say yes. On this tape, risk vetoes a size of 3 (max is 2) and a bar whose high-low range is too wide.

After any losing close, new entries stay blocked until `fresh_read`. Walking more bars does not clear the cooldown. The bundled script records that read before bar 9, which is why a second entry happens. `python -m sovereign_m desk run --no-fresh-read` leaves the later signal blocked.

A position still open after 4 bars is closed by the time exit. A losing time exit starts the cooldown too. The book is an in-memory chip ledger. The only market input is the CSV. No order leaves the process. There is no exchange client and no keys.

### Prove it works

`python -m sovereign_m prove` asks a question the notes can answer, asks one they cannot, routes five tasks, checks that a staged note is invisible until approve, and checks that the tape shows a veto, a cooldown, and a time exit. It prints `PASS` or `FAIL`.

`tests/` covers the same pieces one at a time: retrieval and citations, the registry and the threshold, each desk rule, the gate, the command line, and a scan that the checkout stays free of contact addresses and similar markers.

## Sample output

A question the notes answer:

```text
$ python -m sovereign_m "when do we water the fern"
Water the fern on even days, half a cup, and skip if the soil is still dark.

From the local sample notes. Nothing was added.

Sources:
1. notes/garden.md#fern — Fern — markdown — score 7.18 — used
   Water the fern on even days, half a cup, and skip if the soil is still dark. The pot is green.
2. notes/desk-rules.md#cooldown — Cooldown — markdown — score 0.00 — below cutoff
   After any losing close, the desk blocks new entries until a fresh read is recorded.
3. notes/desk-rules.md#paper-desk-rules — Paper desk rules — markdown — score 0.00 — below cutoff
   Rules for the fictional PIP book. PIP prices are synthetic. The desk never sends an order.
```

A question they do not answer:

```text
$ python -m sovereign_m "what is the cargo code for station northwind"
I don't know.

Closest notes are under the cutoff. They are cited so the miss is visible.

Sources:
1. notes/desk-rules.md#cooldown — Cooldown — markdown — score 0.00 — below cutoff
   After any losing close, the desk blocks new entries until a fresh read is recorded.
2. notes/desk-rules.md#paper-desk-rules — Paper desk rules — markdown — score 0.00 — below cutoff
   Rules for the fictional PIP book. PIP prices are synthetic. The desk never sends an order.
3. notes/desk-rules.md#time-exit — Time exit — markdown — score 0.00 — below cutoff
   A position held longer than the max bar count is closed, even when the other roles still like it.
```

Routing:

```text
$ python -m sovereign_m --who "draft and revise the markdown note"
owner: quill
score: 1.10
threshold: 0.45
job: draft and revise markdown notes in the local sample memory
anti-jobs:
- place paper trades
- merge staged imports
- cite memory sources
ranked:
- quill 1.10
- archivist 0.12
- porter 0.12
- ledger 0.00
- warden 0.00

$ python -m sovereign_m --who "compose a string quartet"
owner: none
score: 0.00
threshold: 0.45
No bot cleared the threshold.
ranked:
- archivist 0.00
- ledger 0.00
- porter 0.00
- quill 0.00
- warden 0.00
```

The paper tape, with the fresh read before bar 9:

```text
$ python -m sovereign_m desk run
Paper trading demo, not financial advice.
Synthetic prices for the fictional symbol PIP. No order leaves this process.

bar  0  flat   need 3 closes  [trend=no skeptic=yes sizer=1 risk=yes]
bar  1  flat   need 3 closes  [trend=no skeptic=yes sizer=1 risk=yes]
bar  2  flat   chase 0.041 is over the limit  [trend=yes skeptic=no sizer=2 risk=yes]
bar  3  block  veto: size 3 is over max 2  [trend=yes skeptic=yes sizer=3 risk=no]
bar  4  block  veto: bar range 0.20 is over 0.08  [trend=yes skeptic=yes sizer=2 risk=no]
bar  5  flat   close 101.00 is not a long  [trend=no skeptic=yes sizer=1 risk=yes]
bar  6  enter  long 1 @ 101.80  [trend=yes skeptic=yes sizer=1 risk=yes]
bar  7  exit   stop below entry  pnl -1.30  cooldown on  [trend=no skeptic=yes sizer=1 risk=yes]
bar  8  block  veto: cooldown until a fresh read  [trend=yes skeptic=yes sizer=2 risk=no]
bar  9  read   fresh read clears cooldown before this bar
bar  9  enter  long 2 @ 101.90  [trend=yes skeptic=yes sizer=2 risk=yes]
bar 10  hold   held 1  [trend=yes skeptic=yes sizer=3 risk=no]
bar 11  hold   held 2  [trend=yes skeptic=yes sizer=3 risk=no]
bar 12  hold   held 3  [trend=yes skeptic=yes sizer=3 risk=no]
bar 13  exit   time exit  pnl 1.40  cooldown off  [trend=yes skeptic=yes sizer=3 risk=no]

paper chips 10000.00 -> 10000.10
```

### Model guard (weights and triggers)

`sovereign_m.modelguard` is a small toolkit you can copy: record file and
listing digests; diff two weight maps for concentrated row edits and for
a handful of MLP neurons that hold most of a layer's change; probe an
endpoint with date, keyword, and error-string triggers (and flag a
shell-like payload that the baseline did not have); and review a
proposed tool command with `cmdguard` (allow, ask, or block). numpy is
optional. The synthetic demo uses only the standard library.

A November 2026 write-up on weight-space backdoors is the starting point.
Claims in that write-up about Microsoft Research, a technique named
Lazarus, and specific hit rates are UNVERIFIED. A digest match or a
clean diff is not a proof of safety. See `docs/modelguard.md` for the
threat model, the dormancy hook (intentionally unimplemented), and a
five-line `cmdguard` integration. This repo does not wire `cmdguard`
into a live agent.

```bash
python -m sovereign_m.modelguard demo
python -m sovereign_m.modelguard probe --dry-run
python -m sovereign_m.modelguard cmdguard -- ls
python -m sovereign_m modelguard lock path/to/weights.safetensors --out models.lock
```


### Self check (ports, web, privesc)

`sovereign_m.selftest` is a read-only self check for machines you own. It
scans an allowlist of your own addresses for services that listen too widely,
probes local web services with a few GET requests, and runs a LinPEAS-style
look at local privilege paths. It never exploits anything, never changes a
setting, and writes a redacted report with a plain-English fix for each
finding. A target outside the allowlist in the config is refused. See
`docs/selftest.md`.

```bash
python examples/selftest/scan_local_demo.py
python -m sovereign_m.selftest scan --check privesc
```


### Security self-check (host audit)

`security-selfcheck/` is a free, zero-dependency, read-only audit for a Linux machine you own. It checks auth logs, user persistence, exposed listeners, firewall/updates, ClamAV/YARA readiness, a honeyfile canary, and backup freshness, then prints a plain-English fix card for every warning. No SaaS, no telemetry, no root required.

```bash
python3 security-selfcheck/security_selfcheck.py
python3 security-selfcheck/security_selfcheck.py --purple-test
python3 security-selfcheck/security_selfcheck.py --json
```

See `security-selfcheck/README.md` for rules, a daily timer, and how to set a canary.

## Where the patterns live

- One job and anti-jobs: `data/registry.json`, `sovereign_m/registry/router.py`
- Memory, citations, and the refusal: `sovereign_m/memory/corpus.py`, `sovereign_m/memory/retrieve.py`
- Optional local model: `sovereign_m/memory/ollama.py`
- Paper desk: `sovereign_m/desk/roles.py`, `sovereign_m/desk/engine.py`, `data/prices.csv`, `data/desk_script.json`
- Approval gate: `sovereign_m/gate/queue.py`
- The tour: `sovereign_m/prove.py`
- Commands: `sovereign_m/cli.py`
- Model guard: `sovereign_m/modelguard/`, `data/modelguard/`, `docs/modelguard.md`
- Host audit: `security-selfcheck/`

## Left out on purpose

Sample files are invented. They do not contain real names, real places, email addresses, network addresses, or keys. The desk has no exchange client and no network order path. A model is optional and cannot override `I don't know.`

## License

MIT. See `LICENSE`.
