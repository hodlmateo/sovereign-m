# cmdguard (Rust)

Port of `sovereign_m.modelguard.cmdguard` (policy-review path only). Python
remains the live implementation and the fallback. See `docs/modelguard.md`.

- `src/lib.rs` review, default policy, payload patterns, JSONL log
- `src/yaml_lite.rs` port of `modelguard/yaml_lite.py` (same policy files)
- `src/pytext.rs`, `src/pytables.rs` Python `str`/`re` semantics (generated)
- `src/main.rs` `cmdguard-rs [--context F] [--log F] [--policy F] -- COMMAND`
  (same output and exit codes as the Python CLI) plus machine modes
  `--rs-stdin-json`, `--rs-batch`, `--rs-dump-word` used by the shim and tests

Anything the port cannot reproduce exactly is refused with an
`unsupported:` error so the Python shim falls back; it is never guessed.
