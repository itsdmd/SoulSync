"""What the duplicate-track finding card shows for each copy that the scan did
not store: the size of the file on disk and its format, read when the card is
fetched (a stat per copy, so it is also right for findings raised long ago).
"""

from __future__ import annotations

import os
from typing import Any, Dict

from utils.logging_config import get_logger

logger = get_logger("fork.dup_info")

_MAX_FILES = 400          # one page of findings: never a long walk over a network share


def enrich(worker: Any, page: Dict[str, Any]) -> None:
    """Add ``file_size`` (bytes), ``format`` and ``file_found`` to every copy of
    every ``duplicate_tracks`` finding of a page, in place."""
    from core.fork import hooks

    if not hooks._active():
        return
    items = [i for i in (page.get("items") or []) if i.get("finding_type") == "duplicate_tracks"]
    if not items:
        return
    from core.repair_worker import _resolve_file_path

    config = getattr(worker, "_config_manager", None)
    download = ""
    try:
        download = config.get("soulseek.download_path", "") if config else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("download path not read: %s", exc)
    budget = _MAX_FILES
    for item in items:
        for track in (item.get("details") or {}).get("tracks") or []:
            if not isinstance(track, dict) or "file_size" in track:
                continue
            stored = str(track.get("file_path") or "")
            if not stored:
                continue
            track["format"] = os.path.splitext(stored)[1].lstrip(".").upper()
            if budget <= 0:
                continue
            budget -= 1
            try:
                found = _resolve_file_path(stored, worker.transfer_folder, download, config_manager=config)
                if found and os.path.isfile(found):
                    track["file_size"] = os.path.getsize(found)
                    track["file_found"] = True
                else:
                    track["file_found"] = False
            except Exception as exc:  # noqa: BLE001 - a missing size only costs the figure
                logger.debug("No size for %s: %s", stored, exc)
