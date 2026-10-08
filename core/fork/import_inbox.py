"""Dismissing entries of the Import page in bulk.

Upstream can only dismiss an entry the importer has already written a record
for (needs review / needs identifying). A fresh drop that is still "waiting"
has no record, and a failed one offers no dismiss at all, so a selection of
those could not be cleared. Here any entry can be dismissed: its record is
marked rejected, or one is created for it. The files stay in the import
folder and the importer leaves a dismissed folder alone.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from utils.logging_config import get_logger

logger = get_logger("fork.import_inbox")


def dismiss(db: Any, items: List[Any]) -> Dict[str, Any]:
    from core.fork import import_move

    root = import_move.staging_root()
    done, errors = 0, []
    conn = db._get_connection()
    try:
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("folder_name") or item.get("name") or "")
            try:
                history_id = item.get("history_id")
                if history_id is not None:
                    changed = conn.execute(
                        "UPDATE auto_import_history SET status = 'rejected', updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?", (int(history_id),)).rowcount
                    if changed:
                        done += 1
                        continue
                path = os.path.realpath(str(item.get("folder_path") or ""))
                if not root or not (path == root or path.startswith(root.rstrip(os.sep) + os.sep)):
                    raise ValueError("not in the import folder")
                conn.execute(
                    "INSERT INTO auto_import_history (folder_name, folder_path, folder_hash, status, total_files,"
                    " processed_at) VALUES (?, ?, ?, 'rejected', ?, CURRENT_TIMESTAMP)",
                    (name or os.path.basename(path), str(item.get("folder_path")), str(item.get("key") or "") or None,
                     int(item.get("file_count") or 0)))
                done += 1
            except Exception as exc:
                errors.append(f"{name or 'item'}: {exc}")
        conn.commit()
    finally:
        conn.close()
    return {"dismissed": done, "errors": errors}


__all__ = ["dismiss"]
