"""Comma Artist Splitter: store a verified split the way the job is set to.

Upstream's fix writes the display artist as "A; B" plus the Artists list, and
turns an album artist equal to the combined string into the first artist only.
This step runs right after it, on the same files:

* ``split_into_separate_tags`` (default on): ARTIST becomes one value per
  artist; an album artist that was the combined string gets the same.
* otherwise: one value joined with the job's ``separator``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Set

from core.fork import artist_format, tags
from utils.logging_config import get_logger

logger = get_logger("fork.comma_split")

JOB_ID = "comma_artist_splitter"


def job_settings(worker: Any) -> Dict[str, Any]:
    cfg = getattr(worker, "_config_manager", None)
    stored: Dict[str, Any] = {}
    try:
        if cfg is not None:
            stored = cfg.get(f"repair.jobs.{JOB_ID}.settings", {}) or {}
    except Exception:
        stored = {}
    split = stored.get("split_into_separate_tags", True)
    if isinstance(split, str):
        split = split.strip().lower() != "false"
    name = str(stored.get("separator") or "semicolon").strip().lower()
    if name == artist_format.CUSTOM:
        # the job has its own custom separator, independent of the global one
        name = "literal:" + str(stored.get("custom_separator") or "").strip()
    return {"split": bool(split), "separator": name}


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _parts(details: Any) -> List[str]:
    parts = (details or {}).get("split_artists") if isinstance(details, dict) else None
    return [str(p).strip() for p in parts or [] if str(p).strip()]


def _files(worker: Any, details: Any) -> List[str]:
    from core.library.path_resolver import resolve_library_file_path

    infos = (details or {}).get("all_files") or (details or {}).get("files") or []
    out: List[str] = []
    for info in infos:
        raw = info.get("file_path") if isinstance(info, dict) else info
        try:
            path = resolve_library_file_path(
                raw, transfer_folder=getattr(worker, "transfer_folder", None),
                config_manager=getattr(worker, "_config_manager", None))
        except Exception:
            path = None
        if path and os.path.isfile(path) and path not in out:
            out.append(path)
    return out


def album_artist_files(worker: Any, details: Any) -> Set[str]:
    """Resolved paths whose album artist currently IS the combined string."""
    from mutagen import File as MutagenFile

    combined = _norm((details or {}).get("combined_name") or (details or {}).get("artist_name"))
    found: Set[str] = set()
    if not combined:
        return found
    for path in _files(worker, details):
        try:
            audio = MutagenFile(path)
            if audio is None or audio.tags is None:
                continue
            if any(_norm(v) == combined for v in artist_format.read_values(audio, "albumartist")):
                found.add(path)
        except Exception as exc:
            logger.debug("Could not read %s: %s", path, exc)
    return found


def apply_strategy(worker: Any, details: Any, album_artist_paths: Set[str],
                   result: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from mutagen import File as MutagenFile

    parts = _parts(details)
    if len(parts) < 2:
        return result
    settings = settings or job_settings(worker)
    split, sep = settings["split"], settings["separator"]
    wanted = {_norm(p) for p in parts}
    rewritten = 0
    for path in _files(worker, details):
        try:
            audio = MutagenFile(path)
            if audio is None or audio.tags is None:
                continue
            # only files that now carry exactly this split (upstream's fix, or
            # an earlier run): a file the user edited since is left alone
            names = artist_format.current_names(audio, "artist")
            listed = artist_format.read_list_tag(audio, "artist")
            if {_norm(n) for n in names} != wanted and {_norm(n) for n in listed} != wanted:
                continue
            changed = artist_format.write_values(audio, "artist", parts, split, sep)
            if path in album_artist_paths:
                changed = artist_format.write_values(audio, "albumartist", parts, split, sep) or changed
            if changed:
                tags._save(audio)
                rewritten += 1
        except Exception as exc:
            logger.warning("Could not rewrite artists for %s: %s", path, exc)
    shown = "separate tags" if split else f'"{artist_format.display(parts, False, sep)}"'
    out = dict(result)
    if result.get("message"):
        out["message"] = f'{result["message"]} — artists written as {shown}'
    logger.info("Comma-artist split stored as %s for %s file(s)", shown, rewritten)
    return out
