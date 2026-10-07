"""Things done from the translation / artist-rule lists, against the library:

* :func:`apply_translation` — push a (changed) translation onto the files that
  already carry the old one: rewrite the title or album tag and, optionally,
  rename the file or album folder to match. Has a dry run for the preview.
* :func:`details` — what the library holds for an original album, title or
  artist name, for the pop-up shown when the name is clicked.

Files are found through SoulSync's library database (rows whose name contains
the original text) and then confirmed from their own tags, so only files that
really are that album/title are touched: either the ``SOULSYNC_ORIGINAL_*``
tag equals the original, or the tag embeds it ("Night Song (夜曲)").
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from core.fork import store, tags, translate
from core.fork.cjk import split_name
from utils.logging_config import get_logger

logger = get_logger("fork.retro")

_FIELD = {"title": "title", "album": "album"}
_MAX_ROWS = 2000
_SIDECARS = (".lrc", ".txt", ".original.lrc", ".original.txt")


def _norm(text: Any) -> str:
    return "".join(str(text or "").casefold().split())


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _query(db: Any, sql: str, params: Tuple[Any, ...]) -> List[Dict[str, Any]]:
    conn = None
    try:
        conn = db._get_connection()
        cursor = conn.execute(sql, params)
        cols = [c[0] for c in cursor.description]
        return [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]
    except Exception as exc:
        logger.debug("library query failed: %s", exc)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: S110
                pass


_TRACK_SQL = (
    "SELECT t.id, t.title, t.track_number, t.duration, t.file_path, al.id AS album_id, al.title AS album, "
    "al.year, ar.name AS artist FROM tracks t JOIN albums al ON al.id = t.album_id "
    "JOIN artists ar ON ar.id = t.artist_id WHERE {where} ORDER BY ar.name, al.title, t.track_number LIMIT ?"
)


def library_tracks(db: Any, kind: str, original: str) -> List[Dict[str, Any]]:
    column = "t.title" if kind == "title" else "al.title"
    return _query(db, _TRACK_SQL.format(where=f"{column} LIKE ? ESCAPE '\\'"), (_like(original), _MAX_ROWS))


def _resolve(file_path: Any) -> Optional[str]:
    from core.fork import album_tagging

    return album_tagging._resolve(file_path)


def _original_of(audio: Any, kind_tag: str, field: str) -> str:
    """The value of SOULSYNC_ORIGINAL_<FIELD>, or ''."""
    name = f"{tags._ORIGINAL_PREFIX}{field.upper()}"
    try:
        if kind_tag == "id3":
            frame = audio.tags.get(f"TXXX:{name}")
            return str(frame.text[0]) if frame is not None and frame.text else ""
        if kind_tag == "vorbis":
            value = audio.get(name.lower())
            return str(value[0]) if value else ""
        if kind_tag == "mp4":
            value = audio.get(f"----:com.apple.iTunes:{name}")
            return bytes(value[0]).decode("utf-8", "replace") if value else ""
    except Exception:
        return ""
    return ""


def plan_file(path: str, kind: str, original: str) -> Optional[Dict[str, Any]]:
    """What applying the stored translation would change in one file, or None
    when the file is not this album/title or is already up to date."""
    from mutagen import File as MutagenFile

    field = _FIELD[kind]
    audio = MutagenFile(path)
    if audio is None or audio.tags is None:
        return None
    kind_tag = tags._kind(audio)
    if not kind_tag:
        return None
    current = tags._read(audio, kind_tag).get(field) or ""
    recorded = _original_of(audio, kind_tag, field)
    wanted = _norm(original)
    source = ""
    if recorded and _norm(split_name(recorded)[0]) == wanted:
        source = recorded                       # translate from the true original, suffix and all
    elif current and _norm(split_name(current)[0]) == wanted:
        source = current                        # "<old translation> (<original>)"
    if not source:
        return None
    new = translate.translate_name(kind, source, allow_llm=False)
    if not new or new == current:
        return None
    return {"path": path, "field": field, "old": current, "new": new, "original": recorded or split_name(source)[0]}


def _sanitize(name: str) -> str:
    from core.imports.paths import sanitize_filename

    return sanitize_filename(name)


def _renamed(component: str, old: str, new: str) -> Optional[str]:
    """``component`` with the old name swapped for the new one, when it
    contains the old name as SoulSync would have written it to disk."""
    for a, b in ((_sanitize(old), _sanitize(new)), (old, new)):
        if a and a in component and a != b:
            return component.replace(a, b)
    return None


def _album_dir(path: str, old: str) -> Optional[str]:
    """The folder named after the album: the file's folder, or the one above
    a disc sub-folder."""
    folder = os.path.dirname(path)
    for candidate in (folder, os.path.dirname(folder)):
        if _renamed(os.path.basename(candidate), old, "\0") is not None:
            return candidate
    return None


def _update_db_path(old: str, new: str) -> None:
    try:
        from core.imports.pipeline import _update_moved_track_file_path

        _update_moved_track_file_path(old, new)
    except Exception as exc:
        logger.debug("Could not update the library path for %s: %s", new, exc)


def _rename_title_file(plan: Dict[str, Any]) -> Optional[str]:
    path = plan["path"]
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    new_stem = _renamed(stem, plan["old"], plan["new"])
    if not new_stem:
        return None
    target = os.path.join(folder, new_stem + ext)
    if os.path.exists(target):
        raise FileExistsError("A file with the new name already exists")
    os.rename(path, target)
    for suffix in _SIDECARS:
        src = os.path.join(folder, stem + suffix)
        if os.path.isfile(src) and not os.path.exists(os.path.join(folder, new_stem + suffix)):
            os.rename(src, os.path.join(folder, new_stem + suffix))
    _update_db_path(path, target)
    return target


def _candidate_paths(db: Any, kind: str, original: str, folder: Optional[str]) -> Tuple[List[str], int]:
    """Files worth checking, and how many library rows had no file on disk.

    With ``folder`` the audio files under that folder are checked directly —
    for files SoulSync's database does not know, or to limit the change to one
    place. Otherwise the library database supplies the candidates.
    """
    from core.fork import album_tagging

    if folder:
        return _folder_files(folder), 0
    paths: List[str] = []
    unreachable = 0
    for track in library_tracks(db, kind, original):
        path = _resolve(track.get("file_path"))
        if not path or not os.path.isfile(path):
            unreachable += 1
        elif path not in paths and album_tagging.root_of(path) is not None:
            paths.append(path)
    return paths, unreachable


def _folder_files(folder: str, limit: int = 20000) -> List[str]:
    """Audio files under ``folder`` (any depth), which must be inside a
    library folder."""
    from core.fork import album_tagging

    real = album_tagging.safe_dir(folder)
    exts = album_tagging._audio_exts()
    out: List[str] = []
    for current, dirs, names in os.walk(real):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names, key=str.casefold):
            if os.path.splitext(name)[1].lower() in exts and not name.startswith("."):
                out.append(os.path.join(current, name))
                if len(out) >= limit:
                    return out
    return out


def apply_translation(db: Any, kind: str, original: str, rename: bool = True,
                      dry_run: bool = False, folder: Optional[str] = None,
                      paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """Rewrite the title/album tag of every file that is ``original`` to the
    currently stored translation. ``folder`` limits the search to one folder
    (and reaches files the library database does not list); ``paths`` is a
    pre-scanned file list for callers applying many names to one folder."""
    from mutagen import File as MutagenFile

    if kind not in _FIELD or not original:
        raise ValueError("kind and original are required")
    row = store.get_translation(kind, original)
    if not row:
        raise LookupError("No stored translation for this name")

    plans: List[Dict[str, Any]] = []
    if paths is None:
        paths, unreachable = _candidate_paths(db, kind, original, folder)
    else:
        unreachable = 0
    seen = set(paths)
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            plan = plan_file(path, kind, original)
        except Exception as exc:
            logger.debug("Could not read %s: %s", path, exc)
            continue
        if plan:
            if rename and kind == "title":
                stem = os.path.splitext(os.path.basename(path))[0]
                new_stem = _renamed(stem, plan["old"], plan["new"])
                plan["rename_to"] = (new_stem + os.path.splitext(path)[1]) if new_stem else None
            plans.append(plan)

    folders: Dict[str, str] = {}
    if rename and kind == "album":
        for plan in plans:
            folder = _album_dir(plan["path"], plan["old"])
            if folder and folder not in folders:
                new_name = _renamed(os.path.basename(folder), plan["old"], plan["new"])
                if new_name:
                    folders[folder] = os.path.join(os.path.dirname(folder), new_name)

    result: Dict[str, Any] = {
        "kind": kind, "original": original, "display": translate.format_name(row["translated"], original),
        "files": [{"path": p["path"], "old": p["old"], "new": p["new"], "rename_to": p.get("rename_to")} for p in plans],
        "folders": [{"from": a, "to": b} for a, b in folders.items()],
        "checked": len(seen), "unreachable": unreachable, "dry_run": dry_run, "folder": folder or "",
        "written": 0, "renamed": 0, "errors": [],
    }
    if dry_run:
        return result

    for plan in plans:
        try:
            audio = MutagenFile(plan["path"])
            kind_tag = tags._kind(audio)
            tags._write(audio, kind_tag, plan["field"], plan["new"])
            if plan["original"]:
                tags._write_original(audio, kind_tag, plan["field"], plan["original"])
            tags._save(audio)
            result["written"] += 1
        except Exception as exc:
            result["errors"].append(f"{os.path.basename(plan['path'])}: {exc}")
            continue
        if rename and kind == "title" and plan.get("rename_to"):
            try:
                if _rename_title_file(plan):
                    result["renamed"] += 1
            except Exception as exc:
                result["errors"].append(f"{os.path.basename(plan['path'])}: rename failed: {exc}")

    for src, dst in folders.items():
        try:
            if os.path.exists(dst):
                raise FileExistsError("A folder with the new name already exists")
            contents = []
            for current, _dirs, names in os.walk(src):
                contents += [os.path.join(current, n) for n in names]
            os.rename(src, dst)
            for old_path in contents:
                _update_db_path(old_path, dst + old_path[len(src):])
            result["renamed"] += 1
        except Exception as exc:
            result["errors"].append(f"{os.path.basename(src)}: rename failed: {exc}")
    logger.info("Applied %s translation for %r: %s tag(s), %s rename(s), %s error(s)",
                kind, original, result["written"], result["renamed"], len(result["errors"]))
    return result


# ── apply every stored translation ──────────────────────────────────────

_job_lock = threading.Lock()
_job: Dict[str, Any] = {"id": 0, "running": False}
_SAMPLE_LIMIT = 200


def job_status() -> Dict[str, Any]:
    with _job_lock:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in _job.items()}


def _records(kind: Optional[str]) -> List[Dict[str, Any]]:
    """Titles before albums: an album folder rename moves every file in it,
    so the per-file title work is done while paths are still as recorded."""
    out: List[Dict[str, Any]] = []
    for k in ("title", "album"):
        if kind in (None, "", k):
            out += store.list_translations(kind=k, limit=1_000_000)["items"]
    return out


def run_apply_all(db: Any, kind: Optional[str] = None, rename: bool = True, dry_run: bool = False,
                  folder: Optional[str] = None, progress: Optional[Any] = None) -> Dict[str, Any]:
    """Apply every stored translation. Returns totals and a sample of the
    changes; ``progress(done, total)`` is called after each name."""
    records = _records(kind)
    totals: Dict[str, Any] = {"total": len(records), "done": 0, "names_changed": 0, "files": 0,
                              "folders": 0, "written": 0, "renamed": 0, "errors": [], "samples": [],
                              "dry_run": dry_run, "folder": folder or ""}
    paths = _folder_files(folder) if folder else None
    for record in records:
        try:
            # a folder rename earlier in this run invalidates the pre-scanned list
            if paths is not None and totals["renamed"] and not dry_run:
                paths = _folder_files(folder) if folder and os.path.isdir(folder) else []
            data = apply_translation(db, record["kind"], record["original"], rename=rename,
                                     dry_run=dry_run, folder=folder, paths=paths)
        except Exception as exc:
            totals["errors"].append(f"{record['original']}: {exc}")
            data = None
        if data and (data["files"] or data["folders"]):
            totals["names_changed"] += 1
            totals["files"] += len(data["files"])
            totals["folders"] += len(data["folders"])
            totals["written"] += data["written"]
            totals["renamed"] += data["renamed"]
            totals["errors"] += data["errors"]
            for item in data["files"]:
                if len(totals["samples"]) < _SAMPLE_LIMIT:
                    totals["samples"].append({"kind": record["kind"], "file": os.path.basename(item["path"]),
                                              "old": item["old"], "new": item["new"]})
        totals["done"] += 1
        if progress:
            progress(totals["done"], totals["total"])
    return totals


def start_apply_all(db_factory: Any, kind: Optional[str] = None, rename: bool = True,
                    dry_run: bool = False, folder: Optional[str] = None) -> Dict[str, Any]:
    """Run :func:`run_apply_all` in the background (a large library takes a
    while); poll :func:`job_status`. One run at a time."""
    from core.fork import album_tagging

    if folder:
        folder = album_tagging.safe_dir(folder)   # fail now, not in the thread
    with _job_lock:
        if _job.get("running"):
            raise RuntimeError("An apply-all run is already in progress")
        job_id = int(_job.get("id") or 0) + 1
        _job.clear()
        _job.update({"id": job_id, "running": True, "done": 0, "total": 0, "dry_run": dry_run,
                     "folder": folder or "", "result": None, "error": None})

    def progress(done: int, total: int) -> None:
        with _job_lock:
            if _job.get("id") == job_id:
                _job.update({"done": done, "total": total})

    def work() -> None:
        result, error = None, None
        try:
            result = run_apply_all(db_factory(), kind, rename, dry_run, folder, progress)
        except Exception as exc:  # noqa: BLE001 - reported to the caller through the status
            logger.exception("apply-all failed")
            error = str(exc)
        with _job_lock:
            if _job.get("id") == job_id:
                _job.update({"running": False, "result": result, "error": error})

    threading.Thread(target=work, name="fork-apply-all", daemon=True).start()
    return job_status()


# ── details pop-up ──────────────────────────────────────────────────────

def _group_albums(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    albums: Dict[Any, Dict[str, Any]] = {}
    for row in rows:
        album = albums.setdefault(row["album_id"], {
            "album": row["album"], "artist": row["artist"], "year": row.get("year"), "tracks": [], "folder": ""})
        path = _resolve(row.get("file_path")) or ""
        if path and not album["folder"]:
            album["folder"] = os.path.dirname(path)
        album["tracks"].append({"title": row["title"], "number": row.get("track_number"),
                                "duration": row.get("duration"), "file": os.path.basename(path) if path else ""})
    return list(albums.values())


def details(db: Any, kind: str, name: str) -> Dict[str, Any]:
    """What the library holds for an original album / title / artist name."""
    name = (name or "").strip()
    if kind not in ("album", "title", "artist") or not name:
        raise ValueError("kind and name are required")
    out: Dict[str, Any] = {"kind": kind, "name": name, "albums": [], "record": None}
    if kind == "artist":
        rule = store.get_artist_name(name)
        out["record"] = rule
        aliases = (store.get_artist_aliases(name) or {}).get("aliases") or []
        out["also_known_as"] = aliases
        names = [name] + ([rule["replacement"]] if rule and rule.get("replacement") else []) + list(aliases)
        placeholders = ",".join("?" * len(names))
        rows = _query(db, _TRACK_SQL.format(where=f"ar.name COLLATE NOCASE IN ({placeholders})"),
                      (*names, _MAX_ROWS))
        out["library_names"] = sorted({r["artist"] for r in rows})
    else:
        core = split_name(name)[0] or name
        record = store.get_translation(kind, core)
        if record:
            record = {**record, "display": translate.format_name(record["translated"], core)}
        out["record"] = record
        rows = library_tracks(db, kind, core)
    out["albums"] = _group_albums(rows)
    out["track_count"] = len(rows)
    out["truncated"] = len(rows) >= _MAX_ROWS
    return out
