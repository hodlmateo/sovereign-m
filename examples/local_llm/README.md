# Local Qwen3.6-35B-A3B (llama-server)

A reusable, loopback-only setup for serving [Qwen3.6-35B-A3B](https://huggingface.co) as a GGUF quant (`UD-Q4_K_M`) with llama-server. Copy the script and config, point them at your binary, weights, and key file, then run or install the user unit.

This is the chat-model lane. It does not start a trading desk, does not open a public port, and does not print the API key.

## Defaults

| Setting | Default |
|---|---|
| Bind | loopback only (any other host is refused) |
| Port | 18080 |
| Context | 16384 tokens |
| KV cache | `q8_0` / `q8_0` |
| Flash attention | on |
| GPU layers | `-ngl 99` |
| MoE on CPU | `--n-cpu-moe 34` (override in the config) |
| API key | path in `KEYFILE`, file mode must be 600 |

`--n-cpu-moe` is a setting. 34 is the default because that is the value that fit a 12 GB card next to a 7B desk model in the measurements below. 99 is slower. 30 ran out of memory when the desk model was already resident.

## Measure (RTX 3060 12 GB, 7B desk model resident)

Qwen3.6-35B-A3B UD-Q4_K_M, llama.cpp CUDA build, generation is about 30 to 35 tokens a second:

- `--n-cpu-moe 34`, `-ngl 99`: prefill (pp512) about 356 tokens/s, generate (tg64) 35.4 tokens/s
- `--n-cpu-moe 99`, `-ngl 99`: prefill about 324 tokens/s, generate 32.2 tokens/s

A dense 27B on the same card, with the desk model resident, was about 1.7 tokens/s, which is why this MoE 35B (about 3B active parameters per token) is the chat-model pick.

## Hash lock

`models.lock` is a modelguard lock entry for the UD-Q4_K_M weights (sha256 only; path is the filename, not a machine path):

`ac0e2c1189e055faa36eff361580e79c5bd6f8e76bffb4ce547f167d53e31a61` (22,134,528,992 bytes)

After you download the file, verify it with modelguard (or `sha256sum`) before serving it.

## 15k pre-route

The server context is 16,384 tokens. Before calling llama-server, estimate the prompt with character-length / 4 (plus a little per-message overhead). If that estimate is at or over 15,000 tokens, skip llama-server and send the request to a longer-context fallback (on the machine that measured this, `qwen2.5:14b`). Keep a second fallback for a context-overflow error, because the estimator is not a real tokenizer and a dense-code prompt can still sneak over 16k.

The helper is `preroute.py` (`choose_route`, `prompt_token_estimate`, `should_use_fallback`).

## Setup

1. Copy `serve-qwen35.conf.example` to `serve-qwen35.conf` in the same directory (or set `QWEN35_CONF`). Fill in `LLAMA_SERVER`, `MODEL`, and `KEYFILE`. Do not commit the filled-in file or the key.
2. Write one secret into the key file and `chmod 600` it.
3. Check the resolved settings (this prints paths, never the key):

       ./serve-qwen35.sh --check

4. Run in the foreground (the systemd unit uses this), or without `--foreground` to daemonize to a pid file and log under `QWEN35_ROOT`.

To start at login, copy `qwen35.service` into the user systemd directory, edit `QWEN35_ROOT` / `WorkingDirectory` / `ExecStart` to this directory, then:

    systemctl --user daemon-reload
    systemctl --user enable --now qwen35.service

The unit template uses `%h` placeholders. It does not ship a machine path or a username.
