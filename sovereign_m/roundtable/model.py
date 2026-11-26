"""Model clients: local llama-server (OpenAI-compatible), Ollama fallback, and a stub.

One request at a time. The API key is read from a key file at call time and is
never logged, returned, or included in error text.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

_LOOPBACK = "127.0.0" + ".1"
DEFAULT_URL = f"http://{_LOOPBACK}:18080/v1/chat/completions"
DEFAULT_OLLAMA_URL = f"http://{_LOOPBACK}:11434/api/chat"
DEFAULT_OLLAMA_MODEL = "qwen2.5:14b"
DEFAULT_TIMEOUT = 300.0

_LOCK = threading.Lock()  # strictly one request at a time


class ModelError(RuntimeError):
    """Raised for any model failure. The message is already redacted."""


@dataclass
class Reply:
    text: str
    backend: str


def default_key_file() -> Path:
    override = os.environ.get("M_ROUNDTABLE_KEY_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "sidetests" / "keys" / "llama-server.key"


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    text = _THINK_BLOCK.sub("", text)
    low = text.lower()
    start = low.find("<think>")
    if start != -1:  # unclosed block: drop to the end
        text = text[:start]
        low = text.lower()
    end = low.rfind("</think>")
    if end != -1:  # closing tag with no opener: keep what follows
        text = text[end + len("</think>"):]
    return text.strip()


class HttpModel:
    def __init__(
        self,
        url: str | None = None,
        ollama_url: str | None = None,
        ollama_model: str | None = None,
        key_file: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        model_name: str | None = None,
    ) -> None:
        self.url = url or os.environ.get("M_ROUNDTABLE_URL") or DEFAULT_URL
        self.ollama_url = (
            ollama_url or os.environ.get("M_ROUNDTABLE_OLLAMA_URL") or DEFAULT_OLLAMA_URL
        )
        self.ollama_model = (
            ollama_model or os.environ.get("M_ROUNDTABLE_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL
        )
        self.model_name = model_name or os.environ.get("M_ROUNDTABLE_MODEL") or "local"
        self.key_file = key_file if key_file is not None else default_key_file()
        self.timeout = timeout
        self.backend_label = "llama-server"

    # -- key handling -------------------------------------------------------
    def _key(self) -> str:
        try:
            return self.key_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _redact(self, text: str) -> str:
        key = self._key()
        if key:
            text = text.replace(key, "[REDACTED]")
        return text

    # -- transport ----------------------------------------------------------
    def _post(self, url: str, payload: dict, auth: str | None) -> dict:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + auth
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    @staticmethod
    def _is_unreachable(exc: BaseException) -> bool:
        """Connection-level failure (refused, no route), not a slow or bad reply."""
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return False
        return isinstance(reason, (ConnectionError, OSError)) and not isinstance(
            exc, urllib.error.HTTPError
        )

    def chat(self, role: str, system: str, user: str, max_tokens: int = 1200) -> Reply:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        with _LOCK:
            try:
                return self._primary(messages, max_tokens)
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                if isinstance(exc, urllib.error.HTTPError) or not self._is_unreachable(exc):
                    raise ModelError(self._describe("llama-server", exc)) from None
            try:
                return self._fallback(messages, max_tokens)
            except Exception as exc:  # noqa: BLE001 - redact everything
                raise ModelError(self._describe("ollama", exc)) from None

    def _primary(self, messages: list[dict], max_tokens: int) -> Reply:
        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            data = self._post(self.url, payload, self._key() or None)
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, ValueError, AttributeError) as exc:
            raise ModelError(self._describe("llama-server", exc)) from None
        return Reply(strip_think(str(content)), self.backend_label)

    def _fallback(self, messages: list[dict], max_tokens: int) -> Reply:
        payload = {
            "model": self.ollama_model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": max_tokens, "num_ctx": 16384, "temperature": 0.3},
        }
        data = self._post(self.ollama_url, payload, None)
        content = data["message"]["content"]
        return Reply(strip_think(str(content)), f"ollama:{self.ollama_model}")

    def _describe(self, backend: str, exc: BaseException) -> str:
        if isinstance(exc, ModelError):
            return self._redact(str(exc))
        kind = type(exc).__name__
        detail = ""
        if isinstance(exc, urllib.error.HTTPError):
            detail = f" HTTP {exc.code}"
        elif isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(
            getattr(exc, "reason", None), (TimeoutError, socket.timeout)
        ):
            detail = " timeout"
        else:
            detail = " " + str(getattr(exc, "reason", exc))[:200]
        return self._redact(f"{backend} request failed: {kind}{detail}")


class StubModel:
    """Deterministic scripted model for --dry-run and tests.

    ``script`` is a list of reply strings (consumed in order; the last one repeats)
    or a callable ``(role, system, user, call_index) -> str``. Every prompt it
    receives is kept in ``calls`` so tests can inspect what the model would see.
    """

    backend_label = "stub"

    def __init__(self, script: Sequence[str] | Callable[..., str]) -> None:
        self.script = script
        self.calls: list[dict] = []

    def chat(self, role: str, system: str, user: str, max_tokens: int = 1200) -> Reply:
        index = len(self.calls)
        self.calls.append({"role": role, "system": system, "user": user, "max_tokens": max_tokens})
        if callable(self.script):
            text = self.script(role, system, user, index)
        else:
            text = self.script[min(index, len(self.script) - 1)]
        return Reply(text, self.backend_label)


def dry_run_model() -> StubModel:
    """Canned, well-formed exchange: Checker asks for another round, then passes."""
    checks = {"n": 0}

    def script(role: str, system: str, user: str, index: int) -> str:
        n = index // 3 + 1
        topic = ("scope the summary", "tighten the wording", "add acceptance criteria")[(n - 1) % 3]
        if role == "ops":
            return (
                f"from: ops\nto: worker\nobjective: Round {n}: {topic} of the card for the worker.\n"
                "artifacts: sovereign_m/roundtable/prompts/worker.md\n"
                "decisions: Keep it to one paragraph.\nopen questions: none"
            )
        if role == "worker":
            return (
                "from: worker\nto: checker\nobjective: One-paragraph summary (dry run).\n"
                "artifacts: sovereign_m/roundtable/prompts/worker.md\n"
                f"decisions: Round {n}: {topic}; this stub made no model call.\nopen questions: none"
            )
        checks["n"] += 1
        verdict = "NONE" if checks["n"] == 1 else "PASS"
        return (
            "from: checker\nto: ops\nobjective: Check the stub summary.\n"
            "artifacts: sovereign_m/roundtable/prompts/worker.md\n"
            f"decisions: Round {n}: canned check that tried to {topic}.\nopen questions: none\n"
            f"verdict: {verdict}"
        )

    return StubModel(script)
