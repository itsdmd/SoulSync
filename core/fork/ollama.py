"""Minimal Ollama chat client with structured (JSON-schema) output."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from core.fork import config
from utils.logging_config import get_logger

logger = get_logger("fork.ollama")


class OllamaError(RuntimeError):
    pass


# After a transport failure, skip calls for a while so a stopped Ollama
# container costs one timeout rather than one per track.
_COOLDOWN_SECONDS = 60
_down_until = 0.0
_state_lock = threading.Lock()
# One request at a time: a single local GPU gains nothing from parallel calls
# and the import pipeline runs several workers.
_call_lock = threading.Lock()


def base_url() -> str:
    return str(config.get("ollama.url") or "").rstrip("/")


def available() -> bool:
    return time.time() >= _down_until


def _mark_down(reason: str) -> None:
    global _down_until
    with _state_lock:
        _down_until = time.time() + _COOLDOWN_SECONDS
    logger.warning("Ollama unavailable (%s); pausing LLM features for %ss", reason, _COOLDOWN_SECONDS)


def reset_cooldown() -> None:
    global _down_until
    with _state_lock:
        _down_until = 0.0


def list_models(url: Optional[str] = None, timeout: float = 10) -> List[str]:
    target = (url or base_url()).rstrip("/")
    try:
        resp = requests.get(f"{target}/api/tags", timeout=timeout)
        resp.raise_for_status()
        models = resp.json().get("models") or []
    except (requests.RequestException, ValueError) as exc:
        raise OllamaError(f"Could not reach Ollama at {target}: {exc}") from exc
    return sorted(str(m.get("name")) for m in models if m.get("name"))


def chat_json(
    task: str,
    system: str,
    payload: Dict[str, Any],
    schema: Dict[str, Any],
    *,
    model: Optional[str] = None,
    temperature: float = 0.2,
    num_ctx: Optional[int] = None,
) -> Dict[str, Any]:
    """Send one stateless request and return the parsed JSON object.

    Raises :class:`OllamaError` on any transport or format failure; callers
    treat that as "no LLM answer" and fall back to upstream behaviour.
    """
    if not available():
        raise OllamaError("Ollama is in cooldown after a recent failure")

    body: Dict[str, Any] = {
        "model": model or config.model_for(task),
        "stream": False,
        "think": False,
        "format": schema,
        "keep_alive": config.get("ollama.keep_alive") or "10m",
        "options": {"temperature": temperature},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
        ],
    }
    if num_ctx:
        body["options"]["num_ctx"] = int(num_ctx)
    url = f"{base_url()}/api/chat"
    timeout = float(config.get("ollama.timeout") or 180)

    with _call_lock:
        try:
            resp = requests.post(url, json=body, timeout=timeout)
            if resp.status_code == 400 and "think" in resp.text.lower():
                # Model without a thinking mode rejects the flag outright.
                body.pop("think", None)
                resp = requests.post(url, json=body, timeout=timeout)
            resp.raise_for_status()
            document = resp.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # A 4xx is a bad request/model name, not an outage.
            if status is None or status >= 500:
                _mark_down(str(exc))
            raise OllamaError(f"Ollama request failed: {exc}") from exc
        except ValueError as exc:
            raise OllamaError("Ollama response was not valid JSON") from exc

    content = (document.get("message") or {}).get("content") if isinstance(document, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise OllamaError("Ollama response contained no content")
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise OllamaError("Ollama content was not a JSON object") from exc
    if not isinstance(parsed, dict):
        raise OllamaError("Ollama content was not a JSON object")
    return parsed
