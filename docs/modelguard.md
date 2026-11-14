# Model guard

A small, copyable defense layer for local model weights. The commands live
in `sovereign_m.modelguard` and run with the standard library. numpy is
optional. This does not prove a model is safe.

## What a weight-space backdoor is

A usual safety test asks the model ordinary questions. Some attacks try to
edit only a few dormant weights so those tests still pass, then fire later
when a hidden trigger appears (a date, a rare phrase, an error string).

A November 2026 write-up describes that pattern and a defense-in-depth
list: compare weights to a known-good base, keep provenance (hashes and a
private approved list), red-team for triggers, and contain what an agent
is allowed to do at runtime. Claims in that write-up about Microsoft
Research and a technique named Lazarus are UNVERIFIED.

## What this package does

1. `lock` / `verify` — record SHA-256 of files, and optional listing
   digests from a local model host, then alert if they drift.
2. `diff` — compare two weight maps (JSON, safetensors, or F32 GGUF).
   Report per-tensor relative change, a row-concentration score, and the
   share of change in the largest row. Concentrated row edits are flagged.
   A diffuse fine-tune should usually stay under the flags. See the
   synthetic demo.
3. `probe` — send a YAML suite of date, keyword, and error-string prompts
   to an OpenAI-compatible or Ollama-style endpoint, and compare each
   reply to a no-trigger baseline.

Config you can tweak: `data/modelguard/config.yaml` and
`data/modelguard/probes.yaml`.

```bash
python -m sovereign_m.modelguard demo
python -m sovereign_m.modelguard lock path/to/weights.safetensors --out models.lock
python -m sovereign_m.modelguard verify models.lock
python -m sovereign_m.modelguard diff base.safetensors candidate.safetensors
python -m sovereign_m.modelguard probe --dry-run
python -m sovereign_m.modelguard probe --endpoint http://localhost:11434
```

`--endpoint` is also accepted as `python -m sovereign_m modelguard ...`.
Leave the endpoint empty until you set one. `MODELGUARD_ENDPOINT` or
`OLLAMA_HOST` can supply it. No address literal is stored in the source.

## Honest limits

- A weight diff needs a trusted reference base. A file with no provenance
  is hard to clear.
- Behavioral probes miss rare, delayed, or unknown triggers.
- Safety fine-tuning has been reported, in related sleeper-agent work, to
  hide a backdoor rather than remove it. Treat that report as unverified
  here, and do not rely on a safety pass alone.
- Thresholds have false positives and false negatives. Tune them.
- Runtime containment (least privilege, sandboxed tool use, egress limits,
  a human yes for high-impact actions) is still required. This package
  does not enforce those controls.
- Model weights need the same scrutiny historically applied to executable
  code when they sit under agents with system access.

## Demo

`examples/modelguard/implanted_neuron_demo.py` builds two synthetic
matrices: one with small noise on every row, and one with the same noise
plus a large edit on two rows. `diff` should accept the first and flag
the second.
