"""Discard All for the Downloads page.

Cancel All stops the work but hands every unfinished track to the wishlist
(that is what a cancelled batch does when it completes). Discard All throws
the work away: each unfinished batch and its tasks are stopped and removed
from the server's state, and nothing is added to the wishlist.

Finished batches are left alone — "Clear Completed" is for those.
"""

from __future__ import annotations

from typing import Any, Dict

from utils.logging_config import get_logger

logger = get_logger("fork.download_discard")

_TERMINAL_TASK = ("completed", "failed", "not_found", "cancelled")
_TERMINAL_PHASE = ("complete", "error", "cancelled")


def discard_all() -> Dict[str, Any]:
    from core import runtime_state
    from core.fork import download_pause
    from core.runtime_state import download_batches, download_tasks, tasks_lock

    discarded_batches = 0
    discarded_tasks = 0
    wishlist_batch = False
    with tasks_lock:
        for batch_id in list(download_batches):
            batch = download_batches[batch_id]
            if batch.get("phase") in _TERMINAL_PHASE:
                continue
            if batch.get("wishlist_processing_started") and not batch.get("wishlist_processing_complete"):
                continue                      # its own thread is writing to the wishlist right now
            queue = list(batch.get("queue", []))
            for task_id in queue:
                task = download_tasks.get(task_id)
                if task and task.get("status") not in _TERMINAL_TASK:
                    task["status"] = "cancelled"
                    discarded_tasks += 1
            batch["phase"] = "cancelled"
            batch["cancelled_tracks"] = set()           # nothing goes to the wishlist
            if batch.get("playlist_id") == "wishlist":
                wishlist_batch = True
            try:
                from core.downloads.atomic_album_publish import discard_staging_root, should_discard_staging

                if should_discard_staging(batch)[0]:
                    discard_staging_root(batch.get("_atomic_staging_root"))
            except Exception as exc:
                logger.debug("staging not discarded for %s: %s", batch_id, exc)
            for task_id in queue:
                download_tasks.pop(task_id, None)
            del download_batches[batch_id]
            download_pause.forget(batch_id)
            discarded_batches += 1
        if wishlist_batch:
            runtime_state.wishlist_auto_processing = False
            runtime_state.wishlist_auto_processing_timestamp = 0.0
    logger.warning("[Downloads] discarded %d batch(es), %d task(s); nothing added to the wishlist",
                   discarded_batches, discarded_tasks)
    return {"discarded_batches": discarded_batches, "discarded_tasks": discarded_tasks}
