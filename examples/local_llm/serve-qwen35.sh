#!/usr/bin/env bash
# llama-server for Qwen3.6-35B-A3B UD-Q4_K_M.
# Binds the loopback address only. Never prints the API key.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${QWEN35_ROOT:-$HERE}"
CONF="${QWEN35_CONF:-$ROOT/serve-qwen35.conf}"
if [[ -f "$CONF" ]]; then
  # shellcheck disable=SC1090
  source "$CONF"
fi

loopback_addr() {
  printf '%d.%d.%d.%d\n' 127 0 0 1
}

LOOPBACK="$(loopback_addr)"
HOST_RAW="${HOST:-loopback}"
case "$HOST_RAW" in
  loopback|"$LOOPBACK") HOST="$LOOPBACK" ;;
  *)
    echo "refusing to bind $HOST_RAW — this lane must stay on loopback" >&2
    exit 2
    ;;
esac

PORT="${PORT:-18080}"
MODEL="${MODEL:-}"
KEYFILE="${KEYFILE:-}"
LLAMA_SERVER="${LLAMA_SERVER:-llama-server}"
PIDFILE="${PIDFILE:-$ROOT/llama-server-35b.pid}"
LOG="${LOG:-$ROOT/llama-server-35b.log}"
NGL="${NGL:-99}"
THREADS="${THREADS:-8}"
CTX="${CTX:-16384}"
N_CPU_MOE="${N_CPU_MOE:-34}"
CACHE_TYPE_K="${CACHE_TYPE_K:-q8_0}"
CACHE_TYPE_V="${CACHE_TYPE_V:-q8_0}"
FLASH_ATTN="${FLASH_ATTN:-on}"
PARALLEL="${PARALLEL:-1}"
REASONING="${REASONING:-off}"

FOREGROUND=0
CHECK=0
PRINT_CMD=0
for arg in "$@"; do
  case "$arg" in
    --foreground|-f) FOREGROUND=1 ;;
    --check) CHECK=1 ;;
    --print-cmd) PRINT_CMD=1 ;;
  esac
done
if [[ -n "${SYSTEMD_EXEC_PID:-}" || -n "${INVOCATION_ID:-}" ]]; then
  FOREGROUND=1
fi

require_paths() {
  if [[ -z "$MODEL" ]]; then
    echo "MODEL is not set (put a path in serve-qwen35.conf or the environment)" >&2
    exit 1
  fi
  if [[ -z "$KEYFILE" ]]; then
    echo "KEYFILE is not set (put a path in serve-qwen35.conf or the environment)" >&2
    exit 1
  fi
  if [[ ! -f "$MODEL" ]]; then
    echo "missing model: $MODEL" >&2
    exit 1
  fi
  if [[ ! -f "$KEYFILE" ]]; then
    echo "missing api key file: $KEYFILE" >&2
    exit 1
  fi
  if [[ "$(stat -c '%a' "$KEYFILE")" != "600" ]]; then
    echo "api key file must be chmod 600" >&2
    exit 1
  fi
  if [[ -x "$LLAMA_SERVER" ]] || command -v "$LLAMA_SERVER" >/dev/null 2>&1; then
    return 0
  fi
  if [[ "$CHECK" -eq 1 || "$PRINT_CMD" -eq 1 ]]; then
    return 0
  fi
  echo "llama-server not found: $LLAMA_SERVER" >&2
  exit 1
}

build_cmd() {
  CMD=(
    "$LLAMA_SERVER"
    --model "$MODEL"
    --host "$HOST"
    --port "$PORT"
    --api-key-file "$KEYFILE"
    --n-gpu-layers "$NGL"
    --n-cpu-moe "$N_CPU_MOE"
    --threads "$THREADS"
    --ctx-size "$CTX"
    --cache-type-k "$CACHE_TYPE_K"
    --cache-type-v "$CACHE_TYPE_V"
    --flash-attn "$FLASH_ATTN"
    --parallel "$PARALLEL"
    --reasoning "$REASONING"
    --no-webui
  )
}

if [[ "$CHECK" -eq 1 ]]; then
  require_paths
  echo "ok bind=loopback port=$PORT ngl=$NGL n-cpu-moe=$N_CPU_MOE ctx=$CTX kv=$CACHE_TYPE_K/$CACHE_TYPE_V flash-attn=$FLASH_ATTN"
  echo "model=$MODEL"
  echo "keyfile=$KEYFILE mode=$(stat -c '%a' "$KEYFILE")"
  echo "llama-server=$LLAMA_SERVER"
  exit 0
fi

require_paths
build_cmd

if [[ "$PRINT_CMD" -eq 1 ]]; then
  printf '%q ' "${CMD[@]}"
  printf '\n'
  exit 0
fi

mkdir -p "$(dirname "$PIDFILE")" "$(dirname "$LOG")"

if [[ "$FOREGROUND" -eq 1 ]]; then
  exec "${CMD[@]}"
fi

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already running pid $(cat "$PIDFILE") on loopback:$PORT" >&2
  exit 0
fi

nohup "${CMD[@]}" >>"$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "started llama-server pid $(cat "$PIDFILE") on loopback:$PORT (key file $KEYFILE)"
