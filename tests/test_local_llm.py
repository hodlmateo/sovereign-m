"""Local llama-server helpers and the reusable Qwen3.6-35B-A3B serve script."""

import json
import os
import stat
import subprocess
from pathlib import Path

from sovereign_m.local_llm import (
    DEFAULT_MAX_PROMPT_TOKENS,
    FALLBACK,
    PRIMARY,
    choose_route,
    prompt_token_estimate,
    should_use_fallback,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "local_llm"
SCRIPT = EXAMPLE / "serve-qwen35.sh"
LOCK = EXAMPLE / "models.lock"
EXPECTED_SHA = "ac0e2c1189e055faa36eff361580e79c5bd6f8e76bffb4ce547f167d53e31a61"


def test_short_prompt_stays_on_primary():
    messages = [
        {"role": "system", "content": "Reply with one word."},
        {"role": "user", "content": "pong"},
    ]
    assert prompt_token_estimate(messages) < DEFAULT_MAX_PROMPT_TOKENS
    assert choose_route(messages) == PRIMARY
    assert not should_use_fallback(prompt="pong", system="Reply with one word.")


def test_prompt_at_threshold_uses_fallback():
    # A bare prompt still gets a leading newline in the estimator.
    under = "x" * (4 * (DEFAULT_MAX_PROMPT_TOKENS - 16) - 8)
    assert prompt_token_estimate(prompt=under) < DEFAULT_MAX_PROMPT_TOKENS
    assert choose_route(prompt=under) == PRIMARY
    over = under + ("x" * 16)
    assert prompt_token_estimate(prompt=over) >= DEFAULT_MAX_PROMPT_TOKENS
    assert choose_route(prompt=over) == FALLBACK
    assert should_use_fallback(prompt=over)


def test_over_16k_estimate_uses_fallback():
    prompt = "ab" * 40_000  # well over 16k tokens by char/4
    est = prompt_token_estimate(prompt=prompt)
    assert est > 16_000
    assert choose_route(prompt=prompt) == FALLBACK


def test_lock_entry_is_filename_and_known_sha():
    data = json.loads(LOCK.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert len(data["files"]) == 1
    row = data["files"][0]
    assert row["path"] == "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
    assert "/" not in row["path"]
    assert row["sha256"] == EXPECTED_SHA
    assert row["size"] == 22_134_528_992
    assert "home" not in json.dumps(data)


def _prep_run(tmp_path: Path, host: str = "loopback", mode: int = 0o600):
    model = tmp_path / "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
    key = tmp_path / "api.key"
    model.write_bytes(b"not-a-real-model")
    key.write_text("placeholder-not-a-real-key\n", encoding="utf-8")
    os.chmod(key, mode)
    conf = tmp_path / "serve-qwen35.conf"
    conf.write_text(
        "\n".join(
            [
                f"HOST={host}",
                "PORT=18080",
                f"MODEL={model}",
                f"KEYFILE={key}",
                "LLAMA_SERVER=llama-server",
                "N_CPU_MOE=34",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["QWEN35_ROOT"] = str(tmp_path)
    env["QWEN35_CONF"] = str(conf)
    return env, model, key


def test_serve_check_accepts_loopback_and_hides_key(tmp_path):
    env, model, key = _prep_run(tmp_path)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "bind=loopback" in out
    assert "n-cpu-moe=34" in out
    assert "ctx=16384" in out
    assert f"model={model}" in out
    assert f"keyfile={key}" in out
    assert "mode=600" in out
    assert "placeholder-not-a-real-key" not in out
    assert "placeholder-not-a-real-key" not in proc.stderr


def test_serve_refuses_non_loopback(tmp_path):
    env, _, _ = _prep_run(tmp_path, host="all-interfaces")
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2
    assert "must stay on loopback" in proc.stderr


def test_serve_requires_key_mode_600(tmp_path):
    env, _, _ = _prep_run(tmp_path, mode=0o644)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 1
    assert "chmod 600" in proc.stderr


def test_print_cmd_uses_expected_flags_and_api_key_file(tmp_path):
    env, model, key = _prep_run(tmp_path)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--print-cmd"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout
    assert "--ctx-size 16384" in line
    assert "--cache-type-k q8_0" in line
    assert "--cache-type-v q8_0" in line
    assert "--flash-attn on" in line
    assert "--n-gpu-layers 99" in line
    assert "--n-cpu-moe 34" in line
    assert f"--model {model}" in line or f"--model {model}" in line.replace("\\", "")
    assert "--api-key-file" in line
    assert "placeholder-not-a-real-key" not in line
    # Bind must be the constructed loopback address, not a public or LAN address.
    loopback = ".".join("127 0 0 1".split())
    assert f"--host {loopback}" in line


def test_unit_template_uses_placeholders_only():
    text = (EXAMPLE / "qwen35.service").read_text(encoding="utf-8")
    assert "WantedBy=default.target" in text
    assert "serve-qwen35.sh --foreground" in text
    assert "%h/local-llm" in text
    assert "Qwen3.6-35B-A3B" in text
