"""Pause All / Resume All for the Downloads page.

Paused means no queued download is started: every batch holds where it is.
Downloads already searching or transferring run to their end — Soulseek has
no way to suspend a transfer, only to cancel it, and cancelling would throw
the progress away.

The switch lives in memory only, on purpose: a restart of SoulSync starts
unpaused, so downloads pick up again by themselves.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from utils.logging_config import get_logger

logger = get_logger("fork.download_pause")

_lock = threading.Lock()
_paused = False
_held: set = set()                      # batches that tried to start something while paused
_start: Optional[Callable[[str], None]] = None      # starts a batch's next downloads, unpaused


def is_paused() -> bool:
    return _paused


def state() -> Dict[str, Any]:
    with _lock:
        return {"paused": _paused, "held_batches": len(_held)}


def pause() -> Dict[str, Any]:
    global _paused
    with _lock:
        _paused = True
    logger.info("[Downloads] paused: queued downloads are held")
    return state()


def hold(batch_id: str, start: Callable[[str], None]) -> bool:
    """True when ``batch_id`` must not start anything now. ``start`` is kept
    so :func:`resume` can start what was held."""
    global _start
    with _lock:
        if not _paused:
            return False
        _held.add(batch_id)
        _start = start
    return True


def resume() -> Dict[str, Any]:
    """Unpause and start what was held, one batch after another."""
    global _paused, _start
    with _lock:
        _paused = False
        held, start = sorted(_held), _start
        _held.clear()
        _start = None
    started = 0
    for batch_id in held:
        try:
            if start is not None:
                start(batch_id)
                started += 1
        except Exception as exc:
            logger.warning("[Downloads] batch %s not restarted after the pause: %s", batch_id, exc)
    logger.info("[Downloads] resumed: %d held batch(es) started", started)
    return {**state(), "resumed_batches": started}


def reset() -> None:
    """For tests."""
    global _paused, _start
    with _lock:
        _paused = False
        _held.clear()
        _start = None


__all__ = ["hold", "is_paused", "pause", "reset", "resume", "state"]
