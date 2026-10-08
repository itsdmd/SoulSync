"""Manual import: the user types the tags, SoulSync files the tracks.

For files in the import folder that no metadata source knows (or that the
user simply wants filed their own way). The page loads each file's current
tags as defaults; the user edits them and optionally sets a cover; then

1. the edited tags and the cover are written into the files, still in the
   import folder, and
2. the files go through SoulSync's normal import pipeline in the fork's
   "rename only" mode, with a release built from those tags instead of a
   matched one. The pipeline names and files them by the configured path
   template and registers them in the library, and changes nothing else in
   the file.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.logging_config import get_logger

logger = get_logger("fork.manual_import")

MAX_FILES = 500


def _roots() -> List[str]:
    from core.fork import import_move

    root = import_move.staging_root()
    return [root] if root else []


def load(paths: List[Any]) -> Dict[str, Any]:
    """Each file's current tags, the defaults of the form."""
    from core.fork import editor

    roots = _roots()
    files, errors = [], []
    for raw in paths[:MAX_FILES]:
        try:
            path = editor.safe_path(raw, roots)
            files.append(dict(editor.read_tags(path), path=path, name=os.path.basename(path)))
        except Exception as exc:
            errors.append(f"{os.path.basename(str(raw))}: {exc}")
    files.sort(key=lambda f: (_number(f["tags"].get("discnumber")) or 1, _number(f["tags"].get("tracknumber")) or 9999,
                              f["name"].casefold()))
    return {"files": files, "errors": errors, "fields": editor.field_list()}


def cover_of(path: Any) -> Optional[Tuple[bytes, str]]:
    from core.fork import editor

    return editor.cover_of(editor.safe_path(path, _roots()))


def _number(value: Any) -> int:
    try:
        return int(str(value or "").split("/")[0].strip() or 0)
    except ValueError:
        return 0


def _names(value: Any) -> List[Dict[str, str]]:
    return [{"name": part.strip()} for part in str(value or "").split(";") if part.strip()]


def release_type(requested: Any, count: int) -> str:
    kind = str(requested or "").strip().lower()
    if kind in ("album", "ep", "single", "compilation"):
        return kind
    return "single" if count <= 1 else "album"


def build_release(files: List[Dict[str, Any]], requested_type: Any = "") -> Dict[str, Any]:
    """The ``album`` + ``matches`` payload upstream's album import takes, from
    the tags the user confirmed. One call is one release."""
    if not files:
        raise ValueError("No files to import")
    first = files[0]["tags"]
    album_name = str(first.get("album") or "").strip() or str(first.get("title") or "").strip()
    album_artist = str(first.get("albumartist") or "").strip() or str(first.get("artist") or "").strip()
    if not album_name:
        raise ValueError("The album (or, for a single, the title) is missing")
    if not album_artist:
        raise ValueError("The artist is missing")
    totals = [int(str(f["tags"].get("tracknumber") or "").split("/")[1]) for f in files
              if "/" in str(f["tags"].get("tracknumber") or "") and str(f["tags"]["tracknumber"]).split("/")[1].strip().isdigit()]
    total = max([len(files)] + totals)
    album = {
        "id": "", "source": "", "name": album_name, "album_name": album_name,
        "artist": _names(album_artist)[0]["name"], "artist_name": _names(album_artist)[0]["name"],
        "artists": _names(album_artist), "release_date": str(first.get("date") or "").strip(),
        "total_tracks": total, "album_type": release_type(requested_type, len(files)), "image_url": "", "images": [],
    }
    matches = []
    for position, file in enumerate(files, 1):
        tags = file["tags"]
        title = str(tags.get("title") or "").strip()
        if not title:
            raise ValueError(f'{os.path.basename(file["path"])} has no title')
        matches.append({
            "staging_file": {"full_path": file["path"], "filename": os.path.basename(file["path"])},
            "track": {"id": "", "name": title, "track_number": _number(tags.get("tracknumber")) or position,
                      "disc_number": _number(tags.get("discnumber")) or 1,
                      "duration_ms": int(float(file.get("length") or 0) * 1000),
                      "artists": _names(tags.get("artist")) or _names(album_artist)},
        })
    return {"album": album, "matches": matches, "rename_only": True, "source": ""}


def run(files: List[Any], cover: Optional[Dict[str, Any]], requested_type: Any,
        process: Callable[[Dict[str, Any]], Tuple[Dict[str, Any], int]]) -> Dict[str, Any]:
    """Write the confirmed tags (and cover) into ``files``, then import them
    as one release. ``process`` is upstream's album import. Nothing is moved
    unless every file took its tags."""
    from core.fork import editor

    roots = _roots()
    if not roots:
        raise ValueError("No import folder is configured")
    clean: List[Dict[str, Any]] = []
    for item in files[:MAX_FILES]:
        if not isinstance(item, dict):
            continue
        path = editor.safe_path(item.get("path"), roots)
        tags = {k: str(v if v is not None else "").strip() for k, v in (item.get("tags") or {}).items()}
        clean.append({"path": path, "tags": tags, "length": item.get("length") or 0})
    payload = build_release(clean, requested_type)          # every problem with the form surfaces here, before any write
    for file in clean:
        result = editor.save_tags([file["path"]], file["tags"], cover, roots=roots)
        if result["errors"]:
            raise ValueError(f"Tags not written, nothing was imported — {result['errors'][0]}")
    body, status = process(payload)
    if status >= 400 or not body.get("success"):
        raise ValueError(str(body.get("error") or "The import did not run"))
    logger.info("Manual import: %s — %s/%s tracks", payload["album"]["name"], body.get("processed"), body.get("total"))
    return {"processed": int(body.get("processed") or 0), "total": int(body.get("total") or len(clean)),
            "errors": list(body.get("errors") or []), "album": payload["album"]["name"],
            "artist": payload["album"]["artist"]}


__all__ = ["build_release", "cover_of", "load", "release_type", "run"]
