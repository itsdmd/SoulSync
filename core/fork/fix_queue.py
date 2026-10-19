"""Fix queue for the Tools page findings.

"Fix all" runs as one background job and only one runs at a time, so lining
up several groups meant coming back after each to press the next. The queue
holds ``(finding type, action)`` entries — the answers to the confirmation
prompts are given when the entry is added — and a small thread starts the next
one as soon as the running job has finished.

Kept in memory: a restart of SoulSync empties it, and nothing is lost but the
list (the findings themselves stay pending).
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from utils.logging_config import get_logger

logger = get_logger("fork.fix_queue")

POLL_SECONDS = 2.0

_lock = threading.Lock()
_items: List[Dict[str, Any]] = []
_problems: List[Dict[str, Any]] = []        # entries that could not start, newest last
_current: Optional[Dict[str, Any]] = None   # the entry whose job is running
_thread: Optional[threading.Thread] = None
_ids = itertools.count(1)
_worker_getter: Optional[Callable[[], Any]] = None


def _worker():
    if _worker_getter is not None:
        return _worker_getter()
    from api.repair import _repair_worker      # the same worker the Fix All button uses
    return _repair_worker()


def state() -> Dict[str, Any]:
    with _lock:
        return {"items": [dict(i) for i in _items], "current": dict(_current) if _current else None,
                "problems": [dict(p) for p in _problems]}


def add(finding_type: str, fix_action: Optional[str], label: str, count: int) -> Dict[str, Any]:
    """Queue a type. The same type with the same action is queued once."""
    finding_type = (finding_type or "").strip()
    if not finding_type:
        return {"added": False, "error": "finding_type is required"}
    fix_action = fix_action or None
    with _lock:
        for item in _items:
            if item["finding_type"] == finding_type and item["fix_action"] == fix_action:
                return {"added": False, "duplicate": True, "position": _items.index(item) + 1}
        _items.append({"id": next(_ids), "finding_type": finding_type, "fix_action": fix_action,
                       "label": label or finding_type, "count": int(count or 0)})
        position = len(_items)
    _ensure_thread()
    return {"added": True, "position": position}


def remove(item_id: Optional[int] = None) -> int:
    """Drop one entry, or every entry when ``item_id`` is None. Returns how many went."""
    with _lock:
        before = len(_items)
        _items[:] = [] if item_id is None else [i for i in _items if i["id"] != item_id]
        if item_id is None:
            _problems.clear()
        return before - len(_items)


def clear_problems() -> None:
    with _lock:
        _problems.clear()


def _running(worker) -> bool:
    try:
        return bool(worker.get_bulk_fix_status().get("running"))
    except Exception:
        return True                     # cannot tell: do not start a second job on top


def step() -> bool:
    """Start the next entry when nothing is running. False once the queue is empty."""
    global _current
    with _lock:
        if not _items:
            _current = None
            return False
        head = dict(_items[0])
    worker = _worker()
    if worker is None or _running(worker):
        return True
    try:
        result = worker.start_bulk_fix(finding_type=head["finding_type"], fix_action=head["fix_action"])
    except Exception as exc:
        result = {"started": False, "error": str(exc)}
    with _lock:
        if result.get("already_running"):
            return True                 # someone else won the race; try again next round
        if _items and _items[0]["id"] == head["id"]:
            _items.pop(0)
        if result.get("started"):
            _current = {**head, "total": result.get("total", 0)}
            logger.info("[Fix queue] started %s (%s findings)", head["label"], result.get("total"))
        else:
            _current = None
            _problems.append({**head, "error": result.get("error") or "could not start"})
            del _problems[:-10]
            logger.warning("[Fix queue] skipped %s: %s", head["label"], result.get("error"))
        return bool(_items)


def _loop() -> None:
    global _thread
    try:
        while step():
            time.sleep(POLL_SECONDS)
    except Exception as exc:
        logger.error("[Fix queue] stopped on an error: %s", exc, exc_info=True)
    finally:
        with _lock:
            _thread = None


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, daemon=True, name="fork-fix-queue")
        _thread.start()


def reset() -> None:
    """For tests."""
    global _current, _worker_getter
    with _lock:
        _items.clear()
        _problems.clear()
        _current = None
        _worker_getter = None
