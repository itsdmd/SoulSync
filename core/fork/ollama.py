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


# A cold model has to be loaded before it answers, and the first answer after
# a load is slow too: together well over two minutes for a 9B model here,
# against under a second once warm. Calls made while the model is not loaded
# get at least this long, whatever the configured timeout says.
COLD_TIMEOUT_SECONDS = 480
DEFAULT_NUM_CTX = 8192


def context_size() -> int:
    """One context size for every call. Ollama reloads the model whenever a
    request asks for a different one, so mixing sizes costs a reload each time."""
    try:
        return max(2048, int(float(config.get("ollama.num_ctx") or DEFAULT_NUM_CTX)))
    except (TypeError, ValueError):
        return DEFAULT_NUM_CTX


def is_loaded(model: str, url: Optional[str] = None) -> Optional[bool]:
    """Whether ``model`` is in memory right now; None when Ollama did not say."""
    try:
        resp = requests.get(f"{(url or base_url()).rstrip('/')}/api/ps", timeout=4)
        resp.raise_for_status()
        loaded = [str(m.get("name") or m.get("model") or "") for m in resp.json().get("models") or []]
    except (requests.RequestException, ValueError):
        return None
    return any(name == model or name.split(":")[0] == model for name in loaded)


def request_timeout(model: str) -> float:
    configured = float(config.get("ollama.timeout") or 300)
    return configured if is_loaded(model) else max(configured, COLD_TIMEOUT_SECONDS)


def warm(task: str, model: Optional[str] = None) -> bool:
    """Load the task's model into memory without generating anything, and
    wait for it. Cheap when it is already loaded. Returns whether it is ready."""
    model = model or config.model_for(task)
    if is_loaded(model):
        return True
    body = {"model": model, "keep_alive": config.get("ollama.keep_alive") or "10m",
            "options": {"num_ctx": context_size()}}
    try:
        with _call_lock:
            resp = requests.post(f"{base_url()}/api/generate", json=body, timeout=COLD_TIMEOUT_SECONDS)
            resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Could not load %s: %s", model, exc)
        return False
    reset_cooldown()
    return True


def chat_json(
    task: str,
    system: str,
    payload: Dict[str, Any],
    schema: Dict[str, Any],
    *,
    model: Optional[str] = None,
    temperature: float = 0.2,
    num_ctx: Optional[int] = None,
    max_tokens: int = 1024,
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
        # num_predict: constrained JSON output can run away into endless
        # whitespace; a cap ends that in seconds instead of at the timeout.
        "options": {"temperature": temperature, "num_ctx": context_size(), "num_predict": int(max_tokens)},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
        ],
    }
    # ``num_ctx`` from callers is ignored on purpose: see context_size().
    url = f"{base_url()}/api/chat"
    timeout = request_timeout(body["model"])

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
