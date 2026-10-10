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
from core.fork.cjk import fold, script_variants, split_name
from utils.logging_config import get_logger

logger = get_logger("fork.retro")

_FIELD = {"title": "title", "album": "album"}
_MAX_ROWS = 2000
_SIDECARS = (".lrc", ".txt", ".original.lrc", ".original.txt")


def _norm(text: Any) -> str:
    # script-insensitive: 相變臨界 and 相变临界 are the same name
    return fold(text)


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
    # the library may hold the name in the other Chinese script
    variants = script_variants(original)
    where = " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for _ in variants)
    return _query(db, _TRACK_SQL.format(where=f"({where})"), (*[_like(v) for v in variants], _MAX_ROWS))


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
    # What to record as the original when the file has none: the name AND its
    # decoration ("相变临界 OST"), so the next pass rebuilds the same result.
    core, _existing, suffix = split_name(source)
    return {"path": path, "field": field, "old": current, "new": new,
            "original": recorded or f"{core} {suffix}".strip()}


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


def _album_home(path: str) -> str:
    """The folder that holds the album of ``path``: the file's own, or the
    one above a disc sub-folder."""
    from core.fork import album_tagging

    folder = os.path.dirname(path)
    return os.path.dirname(folder) if album_tagging._DISC_DIR_RE.match(os.path.basename(folder)) else folder


def _home_files(home: str, limit: int = 2000) -> List[str]:
    """Audio files of the album folder ``home``: its own and its disc folders'."""
    from core.fork import album_tagging

    exts = album_tagging._audio_exts()
    out: List[str] = []
    try:
        folders = [home] + sorted(os.path.join(home, d) for d in os.listdir(home)
                                  if album_tagging._DISC_DIR_RE.match(d) and os.path.isdir(os.path.join(home, d)))
        for folder in folders:
            for name in sorted(os.listdir(folder), key=str.casefold):
                if os.path.splitext(name)[1].lower() in exts and not name.startswith("."):
                    out.append(os.path.join(folder, name))
    except OSError as exc:
        logger.debug("Could not list %s: %s", home, exc)
    return out[:limit]


def _album_tag(path: str) -> Optional[str]:
    from mutagen import File as MutagenFile

    audio = MutagenFile(path)
    kind_tag = tags._kind(audio) if audio is not None and audio.tags is not None else ""
    return str(tags._read(audio, kind_tag).get("album") or "") if kind_tag else None


def _replicate_album(plans: List[Dict[str, Any]], original: str,
                     aliases: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """One album folder, one album name. The files confirmed to be this album
    decide the name of their folder; every other audio file in it whose album
    tag is just another spelling of the same album (``aliases``: the bare or
    an earlier translation, with no original recorded) or is empty gets that
    name too. Returns ``(plans for those files, files of the folder that
    carry some other album and were left alone)``."""
    from collections import Counter

    homes: Dict[str, List[Dict[str, Any]]] = {}
    for plan in plans:
        homes.setdefault(_album_home(plan["path"]), []).append(plan)
    known = {_norm(a) for a in [*aliases, *(p["old"] for p in plans)] if a}
    wanted = _norm(original)
    planned = {p["path"] for p in plans}
    extra: List[Dict[str, Any]] = []
    other: List[Dict[str, str]] = []
    for home, group in homes.items():
        lead = Counter(p["new"] for p in group).most_common(1)[0][0]
        recorded = next(p["original"] for p in group if p["new"] == lead)
        files = _home_files(home)
        mostly_this_album = len(group) * 2 >= len(files)
        for path in files:
            if path in planned:
                continue
            try:
                current = _album_tag(path)
            except Exception as exc:
                logger.debug("Could not read %s: %s", path, exc)
                continue
            if current is None or current == lead:
                continue
            if _norm(current) in known or (not current and mostly_this_album):
                extra.append({"path": path, "field": "album", "old": current, "new": lead,
                              "original": recorded, "replicated": True})
            elif _norm(split_name(current)[0]) != wanted:       # same album, other edition: its own name stands
                other.append({"path": path, "album": current})
    return extra, other


def _is_root(folder: str) -> bool:
    from core.fork import album_tagging

    return os.path.realpath(folder) in {os.path.realpath(r) for r in album_tagging.allowed_roots()}


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
                      paths: Optional[List[str]] = None,
                      previous: Optional[List[str]] = None) -> Dict[str, Any]:
    """Rewrite the title/album tag of every file that is ``original`` to the
    currently stored translation. ``folder`` limits the search to one folder
    (and reaches files the library database does not list); ``paths`` is a
    pre-scanned file list for callers applying many names to one folder.

    An album is named once and that name goes everywhere: the stored
    translation makes the name, the album's folder is renamed to it, and it
    is written to every file of that folder that is this album
    (:func:`_replicate_album`). ``previous`` are earlier translations of the
    name (a retranslation knows them): files and folders that carry one of
    those without the original beside it are recognised by it."""
    from mutagen import File as MutagenFile

    if kind not in _FIELD or not original:
        raise ValueError("kind and original are required")
    row = store.find_translation(kind, original)
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

    earlier = [p for p in (previous or []) if p and p != row["translated"]]
    other_albums: List[Dict[str, str]] = []
    if kind == "album" and plans:
        aliases = [original, row["translated"], translate.format_name(row["translated"], original)]
        for old in earlier:
            aliases += [old, translate.format_name(old, original)]
        replicated, other_albums = _replicate_album(plans, original, aliases)
        plans += replicated

    folders: Dict[str, str] = {}
    if rename and kind == "album":
        for plan in plans:
            album_dir = _album_dir(plan["path"], plan["old"])
            if album_dir and album_dir not in folders:
                new_name = _renamed(os.path.basename(album_dir), plan["old"], plan["new"])
                if new_name:
                    folders[album_dir] = os.path.join(os.path.dirname(album_dir), new_name)
        # a folder still named after an earlier translation the tags no longer carry
        swaps = [(translate.format_name(old, original), translate.format_name(row["translated"], original))
                 for old in earlier] + [(old, row["translated"]) for old in earlier]
        for home in {_album_home(p["path"]) for p in plans} - set(folders):
            if not _is_root(home):
                new_name = next(filter(None, (_renamed(os.path.basename(home), a, b) for a, b in swaps)), None)
                if new_name:
                    folders[home] = os.path.join(os.path.dirname(home), new_name)

    result: Dict[str, Any] = {
        "kind": kind, "original": original, "display": translate.format_name(row["translated"], original),
        "files": [{"path": p["path"], "old": p["old"], "new": p["new"], "rename_to": p.get("rename_to")} for p in plans],
        "folders": [{"from": a, "to": b} for a, b in folders.items()],
        "other_albums": other_albums,
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
            continue
        try:
            store.move_album_folders_under(src, dst)     # a folder saved for the album follows
        except Exception as exc:
            logger.debug("Saved album folder not updated: %s", exc)
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


def _start_job(label: str, folder: Optional[str], dry_run: bool, runner: Any) -> Dict[str, Any]:
    """Run ``runner(progress)`` in the background (a large library takes a
    while); poll :func:`job_status`. One run at a time, of either kind."""
    with _job_lock:
        if _job.get("running"):
            raise RuntimeError("An apply-all run is already in progress")
        job_id = int(_job.get("id") or 0) + 1
        _job.clear()
        _job.update({"id": job_id, "running": True, "done": 0, "total": 0, "dry_run": dry_run,
                     "what": label, "folder": folder or "", "result": None, "error": None})

    def progress(done: int, total: int) -> None:
        with _job_lock:
            if _job.get("id") == job_id:
                _job.update({"done": done, "total": total})

    def work() -> None:
        result, error = None, None
        try:
            result = runner(progress)
        except Exception as exc:  # noqa: BLE001 - reported to the caller through the status
            logger.exception("apply-all failed")
            error = str(exc)
        with _job_lock:
            if _job.get("id") == job_id:
                _job.update({"running": False, "result": result, "error": error})

    threading.Thread(target=work, name="fork-apply-all", daemon=True).start()
    return job_status()


def start_apply_all(db_factory: Any, kind: Optional[str] = None, rename: bool = True,
                    dry_run: bool = False, folder: Optional[str] = None) -> Dict[str, Any]:
    from core.fork import album_tagging

    if folder:
        folder = album_tagging.safe_dir(folder)   # fail now, not in the thread
    return _start_job("translations", folder, dry_run,
                      lambda progress: run_apply_all(db_factory(), kind, rename, dry_run, folder, progress))


# ── artist rules ────────────────────────────────────────────────────────

def _artist_names_for(original: str) -> List[str]:
    """Every name the library may still hold for the artist a rule is about:
    the original itself, and its other known names — MusicBrainz aliases and
    what the rule USED to say before it was changed."""
    names = [original]
    for alias in (store.get_artist_aliases(original) or {}).get("aliases") or []:
        if isinstance(alias, str) and alias.strip() and alias not in names:
            names.append(alias.strip())
    return names


def remember_previous_name(original: str, previous: str) -> None:
    """Keep a rule's old replacement as a known name of the artist, so files
    tagged with it are still found (and still count as owned) after the rule
    changes."""
    previous = (previous or "").strip()
    if not previous or _norm(previous) == _norm(original):
        return
    known = list((store.get_artist_aliases(original) or {}).get("aliases") or [])
    if _norm(previous) not in {_norm(k) for k in known}:
        store.save_artist_aliases(original, known + [previous])


def library_artist_tracks(db: Any, names: List[str]) -> List[Dict[str, Any]]:
    patterns: List[str] = []
    for name in names:
        for variant in script_variants(name):
            if variant not in patterns:
                patterns.append(variant)
    where = " OR ".join("ar.name LIKE ? ESCAPE '\\'" for _ in patterns)
    return _query(db, _TRACK_SQL.format(where=f"({where})"), (*[_like(p) for p in patterns], _MAX_ROWS))


def plan_artist_file(path: str, original: str, replacement: str, known: List[str]) -> Optional[Dict[str, Any]]:
    """What applying the rule would change in one file's ARTIST / ALBUMARTIST."""
    from mutagen import File as MutagenFile

    from core.fork import artist_format, artist_names

    audio = MutagenFile(path)
    if audio is None or audio.tags is None:
        return None
    kind_tag = tags._kind(audio)
    if not kind_tag:
        return None
    wanted = {_norm(n) for n in known}
    fields: Dict[str, Dict[str, Any]] = {}
    for field in ("artist", "albumartist"):
        current = artist_format.current_names(audio, field)
        if not current:
            continue
        recorded = _original_of(audio, kind_tag, field)
        recorded_names = artist_format.artist_list(recorded) if recorded else []
        if recorded_names and any(_norm(n) in wanted for n in recorded_names):
            # rebuild from the true original credit, with every rule applied
            target = [artist_names.resolve(n, allow_lookup=False) for n in recorded_names]
        elif any(_norm(n) in wanted for n in current):
            target = [replacement if _norm(n) in wanted else n for n in current]
        else:
            continue
        deduped: List[str] = []
        for name in target:
            if name and _norm(name) not in {_norm(d) for d in deduped}:
                deduped.append(name)
        stored = artist_format.read_values(audio, field)
        if deduped and artist_format.tag_values(deduped) != stored:
            fields[field] = {"names": deduped, "old_names": current,
                             "old": artist_format.display(current), "new": artist_format.display(deduped),
                             "original": recorded or artist_format.display(current)}
    if not fields:
        return None
    first = fields.get("artist") or fields["albumartist"]
    return {"path": path, "fields": fields, "old": first["old"], "new": first["new"]}


def _renamed_path(path: str, root: str, swaps: List[Tuple[str, str]]) -> str:
    """``path`` with the old artist name replaced in every folder and file
    name below ``root`` that carries it."""
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    out = []
    for part in parts:
        for old, new in swaps:
            changed = _renamed(part, old, new)
            if changed:
                part = changed
                break
        out.append(part)
    return os.path.join(root, *out)


def _move_file(src: str, dst: str) -> None:
    import shutil

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    src_stem, dst_stem = os.path.splitext(src)[0], os.path.splitext(dst)[0]
    for suffix in _SIDECARS:
        if os.path.isfile(src_stem + suffix) and not os.path.exists(dst_stem + suffix):
            shutil.move(src_stem + suffix, dst_stem + suffix)
    _update_db_path(src, dst)


def _carry_leftovers(old_dir: str, new_dir: str, stop: str) -> None:
    """After the audio left ``old_dir``: bring cover art and the like along,
    then remove the folders that are now empty, up to (not including) ``stop``."""
    import shutil

    if os.path.isdir(old_dir) and os.path.normpath(old_dir) != os.path.normpath(new_dir):
        exts = None
        try:
            from core.fork import album_tagging

            exts = album_tagging._audio_exts()
        except Exception:
            exts = set()
        names = os.listdir(old_dir)
        if not any(os.path.splitext(n)[1].lower() in exts for n in names):
            for name in names:
                src, dst = os.path.join(old_dir, name), os.path.join(new_dir, name)
                if os.path.isfile(src) and not os.path.exists(dst):
                    os.makedirs(new_dir, exist_ok=True)
                    shutil.move(src, dst)
    current = old_dir
    while os.path.isdir(current) and os.path.normpath(current) != os.path.normpath(stop):
        try:
            os.rmdir(current)
        except OSError:
            break
        current = os.path.dirname(current)


def apply_artist_rule(db: Any, original: str, rename: bool = True, dry_run: bool = False,
                      folder: Optional[str] = None, paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """Rewrite ARTIST / ALBUMARTIST on files that still carry the artist's old
    name to what the rule says now; optionally rename folders and files whose
    names contain the old name (merging into an existing artist folder)."""
    from mutagen import File as MutagenFile

    from core.fork import album_tagging, artist_format

    original = (original or "").strip()
    rule = store.get_artist_name(original) if original else None
    if not rule or rule.get("source") == "none" or not rule.get("replacement"):
        raise LookupError("No rule for this artist")
    replacement = rule["replacement"]
    known = _artist_names_for(original)

    unreachable = 0
    if paths is None:
        if folder:
            paths = _folder_files(folder)
        else:
            paths = []
            for track in library_artist_tracks(db, known):
                path = _resolve(track.get("file_path"))
                if not path or not os.path.isfile(path):
                    unreachable += 1
                elif path not in paths and album_tagging.root_of(path) is not None:
                    paths.append(path)

    plans: List[Dict[str, Any]] = []
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            plan = plan_artist_file(path, original, replacement, known)
        except Exception as exc:
            logger.debug("Could not read %s: %s", path, exc)
            continue
        if not plan:
            continue
        if rename:
            root = album_tagging.root_of(path)
            swaps = []
            for info in plan["fields"].values():
                for name in info["old_names"]:
                    if _norm(name) in {_norm(k) for k in known} and (name, replacement) not in swaps:
                        swaps.append((name, replacement))
            target = _renamed_path(path, root, swaps) if root and swaps else path
            plan["move_to"] = target if os.path.normpath(target) != os.path.normpath(path) else None
            plan["rename_to"] = os.path.relpath(target, root) if plan["move_to"] else None
        plans.append(plan)

    result: Dict[str, Any] = {
        "kind": "artist", "original": original, "display": replacement,
        "files": [{"path": p["path"], "old": p["old"], "new": p["new"], "rename_to": p.get("rename_to")} for p in plans],
        "folders": [], "checked": len(paths), "unreachable": unreachable, "dry_run": dry_run,
        "folder": folder or "", "written": 0, "renamed": 0, "errors": [],
    }
    if dry_run:
        return result

    touched_dirs: List[Tuple[str, str, str]] = []
    for plan in plans:
        path = plan["path"]
        try:
            audio = MutagenFile(path)
            kind_tag = tags._kind(audio)
            for field, info in plan["fields"].items():
                artist_format.write_values(audio, field, info["names"])
                if info["original"]:
                    tags._write_original(audio, kind_tag, field, info["original"])
            tags._save(audio)
            result["written"] += 1
        except Exception as exc:
            result["errors"].append(f"{os.path.basename(path)}: {exc}")
            continue
        target = plan.get("move_to")
        if rename and target:
            try:
                if os.path.exists(target):
                    raise FileExistsError("A file already exists at the new location")
                _move_file(path, target)
                result["renamed"] += 1
                entry = (os.path.dirname(path), os.path.dirname(target), album_tagging.root_of(target) or "")
                if entry not in touched_dirs:
                    touched_dirs.append(entry)
            except Exception as exc:
                result["errors"].append(f"{os.path.basename(path)}: move failed: {exc}")
    # deepest first, so an album folder is emptied before its artist folder
    for old_dir, new_dir, root in sorted(touched_dirs, key=lambda item: -len(item[0])):
        try:
            _carry_leftovers(old_dir, new_dir, root)
        except Exception as exc:
            logger.debug("Could not tidy %s: %s", old_dir, exc)
    logger.info("Applied artist rule %r -> %r: %s tag(s), %s move(s), %s error(s)",
                original, replacement, result["written"], result["renamed"], len(result["errors"]))
    return result


def run_apply_all_rules(db: Any, rename: bool = True, dry_run: bool = False,
                        folder: Optional[str] = None, progress: Optional[Any] = None) -> Dict[str, Any]:
    rules = store.list_artist_names(limit=1_000_000)["items"]
    totals: Dict[str, Any] = {"total": len(rules), "done": 0, "names_changed": 0, "files": 0, "folders": 0,
                              "written": 0, "renamed": 0, "errors": [], "samples": [],
                              "dry_run": dry_run, "folder": folder or ""}
    paths = _folder_files(folder) if folder else None
    for rule in rules:
        try:
            if paths is not None and totals["renamed"] and not dry_run:
                paths = _folder_files(folder) if folder and os.path.isdir(folder) else []
            data = apply_artist_rule(db, rule["original"], rename=rename, dry_run=dry_run,
                                     folder=folder, paths=paths)
        except Exception as exc:
            totals["errors"].append(f"{rule['original']}: {exc}")
            data = None
        if data and data["files"]:
            totals["names_changed"] += 1
            totals["files"] += len(data["files"])
            totals["folders"] += sum(1 for f in data["files"] if f.get("rename_to"))
            totals["written"] += data["written"]
            totals["renamed"] += data["renamed"]
            totals["errors"] += data["errors"]
            for item in data["files"]:
                if len(totals["samples"]) < _SAMPLE_LIMIT:
                    totals["samples"].append({"kind": "artist", "file": os.path.basename(item["path"]),
                                              "old": item["old"], "new": item["new"]})
        totals["done"] += 1
        if progress:
            progress(totals["done"], totals["total"])
    return totals


def start_apply_all_rules(db_factory: Any, rename: bool = True, dry_run: bool = False,
                          folder: Optional[str] = None) -> Dict[str, Any]:
    from core.fork import album_tagging

    if folder:
        folder = album_tagging.safe_dir(folder)
    return _start_job("artist rules", folder, dry_run,
                      lambda progress: run_apply_all_rules(db_factory(), rename, dry_run, folder, progress))


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
        record = store.find_translation(kind, core)
        if record:
            record = {**record, "display": translate.format_name(record["translated"], core)}
        out["record"] = record
        rows = library_tracks(db, kind, core)
    out["albums"] = _group_albums(rows)
    out["track_count"] = len(rows)
    out["truncated"] = len(rows) >= _MAX_ROWS
    return out
