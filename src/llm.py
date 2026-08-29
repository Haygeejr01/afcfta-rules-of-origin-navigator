"""Minimal Ollama client.

Deliberately plain: one POST, no retry, no backoff, no circuit breaker. The build
brief calls for running calls plainly first and only adding resilience if the
evaluation actually produces failures worth handling. If retry logic ever appears
in this file it should arrive with a CHANGELOG entry citing the observed failure.

Both the local model and the cloud model are reached through the same Ollama
endpoint, so escalation is a change of model name and nothing else.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import requests
from pydantic import BaseModel, ConfigDict

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# The local workhorse. Note this is the *coder* variant of Qwen 2.5 7B -- see
# CHANGELOG for why the instruct variant was not used.
LOCAL_MODEL = os.environ.get("NAVIGATOR_LOCAL_MODEL", "qwen2.5-coder:7b")

# Escalation target, used only where the local model is shown to fall short.
CLOUD_MODEL = os.environ.get("NAVIGATOR_CLOUD_MODEL", "qwen3.5:cloud")

# Generous but finite: a hung request should fail the run, not stall the CLI.
REQUEST_TIMEOUT_S = float(os.environ.get("NAVIGATOR_TIMEOUT_S", "300"))


class LlmResult(BaseModel):
    """One model call and what it cost in wall-clock terms."""

    model_config = ConfigDict(extra="forbid")

    model: str
    text: str
    duration_ms: float
    ok: bool = True
    error: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None


def call_model(
    prompt: str,
    *,
    model: str = LOCAL_MODEL,
    system: Optional[str] = None,
    json_mode: bool = False,
    temperature: float = 0.0,
) -> LlmResult:
    """Send one chat completion to Ollama and return the raw text.

    ``json_mode`` sets Ollama's structured-output flag, which constrains decoding
    to valid JSON. That is grammar-level enforcement rather than a prompt request,
    which is why the extraction node does not need a parse-retry loop.

    Temperature defaults to 0 so that repeated evaluation runs are comparable.
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if json_mode:
        payload["format"] = "json"

    started = time.perf_counter()
    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/chat", json=payload, timeout=REQUEST_TIMEOUT_S
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        response.raise_for_status()
        body = response.json()
        return LlmResult(
            model=model,
            text=body.get("message", {}).get("content", ""),
            duration_ms=elapsed_ms,
            prompt_tokens=body.get("prompt_eval_count"),
            completion_tokens=body.get("eval_count"),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a failed result
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return LlmResult(
            model=model,
            text="",
            duration_ms=elapsed_ms,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def parse_json_text(text: str) -> Optional[dict[str, Any]]:
    """Best-effort JSON parse of a model response.

    Tries the whole string first, then the outermost brace-delimited span, which
    covers a model that wraps JSON in prose or a fenced block. Returns None rather
    than raising so callers can treat unparseable output as a routing decision.
    """
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def health_check() -> tuple[bool, str]:
    """Confirm Ollama is reachable and report which models are installed."""
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        response.raise_for_status()
        names = [m["name"] for m in response.json().get("models", [])]
        return True, ", ".join(names) if names else "no models installed"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
