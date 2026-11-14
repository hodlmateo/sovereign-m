"""Command line for modelguard. ``python -m sovereign_m.modelguard``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sovereign_m.modelguard.config import load_config
from sovereign_m.modelguard.demo import format_demo, run as run_demo
from sovereign_m.modelguard.diff import diff_tensors, format_report as format_diff, load_weights
from sovereign_m.modelguard.lock import list_ollama_models, lock_paths, write_lock
from sovereign_m.modelguard.probe import format_report as format_probe, load_probes, run_probes
from sovereign_m.modelguard.verify import format_report as format_verify, verify_lock

USAGE = """
modelguard: record digests, verify they have not drifted, diff weights, and probe for hidden triggers.

  python -m sovereign_m.modelguard lock [PATH ...] [--out models.lock] [--ollama]
  python -m sovereign_m.modelguard verify [models.lock] [--ollama]
  python -m sovereign_m.modelguard diff BASE CANDIDATE [--json] [--config FILE]
  python -m sovereign_m.modelguard probe [--config FILE] [--probes FILE] [--endpoint URL] [--dry-run] [--json]
  python -m sovereign_m.modelguard demo

The same verbs work as: python -m sovereign_m modelguard ...
lock and verify are local file and listing checks. diff needs two weight files
(JSON, safetensors, or F32 GGUF). probe needs an endpoint, or --dry-run.
This is a heuristic layer, not a proof that a model is safe.
""".strip()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    command = args[0]
    rest = args[1:]
    if command == "lock":
        return _lock(rest)
    if command == "verify":
        return _verify(rest)
    if command == "diff":
        return _diff(rest)
    if command == "probe":
        return _probe(rest)
    if command == "demo":
        if rest:
            print(USAGE)
            return 2
        result = run_demo()
        print(format_demo(result))
        return 0 if result["ok"] else 1
    print(USAGE)
    return 2


def console_main() -> None:
    sys.exit(main())


def _lock(args: list[str]) -> int:
    out = Path("models.lock")
    use_ollama = False
    paths: list[Path] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"--out", "-o"}:
            if i + 1 >= len(args):
                print("usage: modelguard lock [PATH ...] [--out models.lock] [--ollama]")
                return 2
            out = Path(args[i + 1])
            i += 2
            continue
        if arg == "--ollama":
            use_ollama = True
            i += 1
            continue
        if arg.startswith("-"):
            print("usage: modelguard lock [PATH ...] [--out models.lock] [--ollama]")
            return 2
        paths.append(Path(arg))
        i += 1
    if not paths and not use_ollama:
        print("usage: modelguard lock [PATH ...] [--out models.lock] [--ollama]")
        return 2
    models = []
    if use_ollama:
        models = list_ollama_models(_client())
        if not models:
            print("could not list local models")
            return 1
    payload = lock_paths(paths, models=models or None)
    write_lock(payload, out)
    print(f"wrote {out}  files {len(payload.get('files', []))}  models {len(payload.get('models', []))}")
    return 0


def _verify(args: list[str]) -> int:
    use_ollama = False
    lock = Path("models.lock")
    for arg in args:
        if arg == "--ollama":
            use_ollama = True
        elif arg.startswith("-"):
            print("usage: modelguard verify [models.lock] [--ollama]")
            return 2
        else:
            lock = Path(arg)
    if not lock.is_file():
        print(f"missing lock file: {lock}")
        return 1
    client = _client() if use_ollama else None
    result = verify_lock(lock, client=client)
    print(format_verify(result))
    return 0 if result["ok"] else 1


def _diff(args: list[str]) -> int:
    as_json = False
    config_path = None
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--json":
            as_json = True
            i += 1
            continue
        if arg == "--config":
            if i + 1 >= len(args):
                print("usage: modelguard diff BASE CANDIDATE [--json] [--config FILE]")
                return 2
            config_path = Path(args[i + 1])
            i += 2
            continue
        if arg.startswith("-"):
            print("usage: modelguard diff BASE CANDIDATE [--json] [--config FILE]")
            return 2
        positional.append(arg)
        i += 1
    if len(positional) != 2:
        print("usage: modelguard diff BASE CANDIDATE [--json] [--config FILE]")
        return 2
    flags = load_config(config_path).get("diff")
    result = diff_tensors(load_weights(Path(positional[0])), load_weights(Path(positional[1])), flags=flags)
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(format_diff(result))
    return 0 if result["ok"] else 1


def _probe(args: list[str]) -> int:
    as_json = False
    dry_run = False
    config_path = None
    probes_path = None
    endpoint = ""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--json":
            as_json = True
            i += 1
            continue
        if arg == "--dry-run":
            dry_run = True
            i += 1
            continue
        if arg == "--config":
            if i + 1 >= len(args):
                print(USAGE)
                return 2
            config_path = Path(args[i + 1])
            i += 2
            continue
        if arg == "--probes":
            if i + 1 >= len(args):
                print(USAGE)
                return 2
            probes_path = Path(args[i + 1])
            i += 2
            continue
        if arg == "--endpoint":
            if i + 1 >= len(args):
                print(USAGE)
                return 2
            endpoint = args[i + 1]
            i += 2
            continue
        print("usage: modelguard probe [--config FILE] [--probes FILE] [--endpoint URL] [--dry-run] [--json]")
        return 2
    config = load_config(config_path)
    probes = load_probes(probes_path)
    try:
        result = run_probes(probes, config=config, endpoint=endpoint, dry_run=dry_run)
    except ValueError as exc:
        print(exc)
        return 2
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(format_probe(result))
    return 0 if result["ok"] else 1


def _client():
    from sovereign_m.memory.ollama import Ollama

    return Ollama.from_env()
