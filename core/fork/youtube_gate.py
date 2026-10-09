"""Back off when YouTube refuses this machine.

When YouTube decides an address looks automated it answers every video with
"Sign in to confirm you're not a bot". Nothing can be downloaded then until
the address is forgiven or a signed-in session is used (Settings → YouTube →
cookies.txt). SoulSync, not knowing, went on asking: a format probe and three
download tries for every candidate of every query of every queued track —
hundreds of refused requests an hour, each one a reason to keep the block.

This watches the YouTube client's log for that answer and, once seen, stops
the probes and the downloads for a while. After the pause the next attempt
is let through; if YouTube still refuses, the pause starts again.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict

from utils.logging_config import get_logger

logger = get_logger("fork.youtube_gate")

PAUSE_SECONDS = 30 * 60
_SIGNS = ("sign in to confirm you", "confirm you’re not a bot", "confirm you're not a bot")

_lock = threading.Lock()
_blocked_until = 0.0
_skipped = 0


def is_refusal(text: Any) -> bool:
    low = str(text or "").lower()
    return any(sign in low for sign in _SIGNS)


def note(text: Any) -> bool:
    """Start (or extend) the pause when ``text`` is YouTube's refusal."""
    global _blocked_until, _skipped
    if not is_refusal(text):
        return False
    with _lock:
        fresh = time.monotonic() >= _blocked_until
        _blocked_until = time.monotonic() + PAUSE_SECONDS
        if fresh:
            _skipped = 0
    if fresh:
        logger.warning(
            "YouTube is refusing this address (\"Sign in to confirm you're not a bot\"). "
            "YouTube downloads are paused for %d minutes so the block is not prolonged. "
            "To download from YouTube again now: Settings → YouTube → paste a cookies.txt "
            "of a signed-in browser session.", PAUSE_SECONDS // 60)
    return True


def blocked() -> bool:
    """True while YouTube must be left alone; counts what was held back."""
    global _skipped
    with _lock:
        if time.monotonic() >= _blocked_until:
            return False
        _skipped += 1
        return True


def state() -> Dict[str, Any]:
    with _lock:
        left = max(0.0, _blocked_until - time.monotonic())
        return {"blocked": left > 0, "seconds_left": int(left), "skipped": _skipped}


def reset() -> None:
    global _blocked_until, _skipped
    with _lock:
        _blocked_until = 0.0
        _skipped = 0


class _Watch(logging.Handler):
    """Sees the refusal wherever the YouTube client logs it."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            note(record.getMessage())
        except Exception:  # noqa: S110 - a log line must never break the client
            pass


def watch(client_logger: logging.Logger) -> None:
    if not any(isinstance(handler, _Watch) for handler in client_logger.handlers):
        client_logger.addHandler(_Watch(level=logging.INFO))


__all__ = ["PAUSE_SECONDS", "blocked", "is_refusal", "note", "reset", "state", "watch"]
