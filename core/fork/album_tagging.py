"""Tag the files of one album in place, from the release the user is looking at.

Backs the album pop-up's fork additions (``webui/static/fork-album.js``):

* :func:`check_album` — which of the release's tracks the library has, and
  which folder holds them. Read-only; never searches or downloads.
* :func:`browse` — folder picker, confined to the configured library roots.
* :func:`preview` — match the audio files in a folder to the release's tracks
  and propose tags for each. Nothing is written.
* :func:`apply` — write the (possibly hand-edited) tags; optionally rename the
  files to the path template.

Matching a file to a track is deliberately tolerant, because the files this is
for are the ones whose tags drifted: it weighs the title (exact
original/translation equivalence first, otherwise similarity of the tag or the
file name), the disc/track position and the duration, and reports how sure it
is so the user can correct it before anything is written.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from core.fork import artist_format, config, ownership, tags
from utils.logging_config import get_logger

logger = get_logger("fork.album_tagging")

_MAX_FILES = 400
_MAX_DEPTH = 2
_EDITABLE = ("title", "artist", "albumartist", "album", "year", "track_number", "disc_number")
_DISC_DIR_RE = re.compile(r"^(?:disc|disk|cd)\s*\d+", re.I)
_LEADING_NUMBER_RE = re.compile(r"^\s*(?:\d{1,2}[-. ]\d{1,3}|\d{1,3})\s*[-._)]*\s+")


# ── library roots / folder picker ───────────────────────────────────────

def _audio_exts() -> set:
    from core.tag_writer import SUPPORTED_EXTENSIONS

    return set(SUPPORTED_EXTENSIONS)


def allowed_roots() -> List[str]:
    """Folders the picker and the tagger may touch: the library output folder
    and any extra configured music paths. Never the downloads folder."""
    from core.imports.paths import config_root_path
    from core.settings import config_manager

    raw = [config_manager.get("soulseek.transfer_path", "./Transfer")]
    extra = config_manager.get("library.music_paths", []) or []
    raw.extend([extra] if isinstance(extra, str) else list(extra))
    roots: List[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            continue
        path = os.path.realpath(config_root_path(value))
        if os.path.isdir(path) and path not in roots:
            roots.append(path)
    return roots


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def root_of(path: str) -> Optional[str]:
    real = os.path.realpath(path)
    for root in allowed_roots():
        if _inside(real, root):
            return root
    return None


def safe_dir(path: Any) -> str:
    """``path`` as a real directory inside a library root, or raise."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("No folder given")
    real = os.path.realpath(path)
    if root_of(real) is None:
        raise PermissionError("Folder is outside the library folders")
    if not os.path.isdir(real):
        raise FileNotFoundError("Folder does not exist")
    return real


def _count_audio(folder: str) -> int:
    exts = _audio_exts()
    try:
        return sum(1 for name in os.listdir(folder) if os.path.splitext(name)[1].lower() in exts)
    except OSError:
        return 0


def browse(path: Optional[str] = None) -> Dict[str, Any]:
    roots = allowed_roots()
    if not path:
        return {"path": "", "parent": None, "roots": roots,
                "dirs": [{"name": r, "path": r, "audio": _count_audio(r)} for r in roots]}
    real = safe_dir(path)
    root = root_of(real)
    dirs = []
    try:
        for name in sorted(os.listdir(real), key=str.casefold):
            full = os.path.join(real, name)
            if not name.startswith(".") and os.path.isdir(full):
                dirs.append({"name": name, "path": full, "audio": _count_audio(full)})
    except OSError as exc:
        raise PermissionError(f"Cannot read folder: {exc}") from exc
    parent = os.path.dirname(real) if real != root else ""
    return {"path": real, "parent": parent, "roots": roots, "dirs": dirs, "audio": _count_audio(real)}


def _fold(text: str) -> str:
    """Case- and accent-insensitive form for folder-name search."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


def search_folders(query: str, limit: int = 100, max_depth: int = 4,
                   max_scanned: int = 80_000) -> Dict[str, Any]:
    """Folders anywhere in the library whose path matches every word of
    ``query`` (any order; case and accents ignored). A word may match the
    folder's own name or a parent's, so "chou november" finds
    ``Jay Chou/November's Chopin`` — but the folder's own name must match at
    least one word, otherwise every sub-folder of a matching artist would
    be listed."""
    terms = [t for t in _fold(query or "").split() if t]
    if not terms:
        return {"results": [], "truncated": False}
    results: List[Dict[str, Any]] = []
    scanned = 0
    truncated = False
    for root in allowed_roots():
        stack = [(root, "", 0)]
        while stack:
            current, rel, depth = stack.pop()
            try:
                entries = sorted((e for e in os.scandir(current)
                                  if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
                                 key=lambda e: e.name.casefold(), reverse=True)
            except OSError:
                continue
            for entry in entries:
                scanned += 1
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                own, whole = _fold(entry.name), _fold(child_rel)
                if all(t in whole for t in terms) and any(t in own for t in terms):
                    results.append({"name": entry.name, "path": entry.path, "rel": child_rel,
                                    "root": root, "audio": _count_audio(entry.path)})
                    if len(results) >= limit:
                        return {"results": _rank(results, terms), "truncated": True}
                if depth + 1 < max_depth:
                    stack.append((entry.path, child_rel, depth + 1))
            if scanned >= max_scanned:
                truncated = True
                break
    return {"results": _rank(results, terms), "truncated": truncated}


def _rank(results: List[Dict[str, Any]], terms: List[str]) -> List[Dict[str, Any]]:
    """Folders that hold audio first (an album, not an artist), then the ones
    whose own name carries more of the query, then by path."""
    def key(item: Dict[str, Any]):
        own = _fold(item["name"])
        return (0 if item["audio"] else 1, -sum(1 for t in terms if t in own), item["rel"].casefold())
    return sorted(results, key=key)


# ── release payload helpers ─────────────────────────────────────────────

def _artist_names(track: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for entry in track.get("artists") or []:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return names


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).split("/")[0])
    except (TypeError, ValueError):
        return default


def _year(album: Dict[str, Any]) -> str:
    m = re.search(r"\d{4}", str(album.get("release_date") or album.get("year") or ""))
    return m.group(0) if m else ""


def _album_artist(album: Dict[str, Any], artist: Dict[str, Any]) -> str:
    name = (artist or {}).get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    names = _artist_names(album or {})
    return names[0] if names else ""


def split_credit(text: str) -> List[str]:
    return artist_format.split_credit(text)


def source_tags(album: Dict[str, Any], artist: Dict[str, Any], track: Dict[str, Any],
                position: int) -> Dict[str, Any]:
    """Tag values exactly as the metadata source gives them."""
    from core.settings import config_manager

    separator = config_manager.get("metadata_enhancement.tags.artist_separator", ", ") or ", "
    album_artist = _album_artist(album, artist)
    names = _artist_names(track)
    return {
        "title": str(track.get("name") or track.get("title") or "").strip(),
        "artist": separator.join(names) if names else album_artist,
        "albumartist": album_artist,
        "album": str((album or {}).get("name") or (album or {}).get("title") or "").strip(),
        "year": _year(album or {}),
        "track_number": _int(track.get("track_number"), position + 1) or position + 1,
        "disc_number": _int(track.get("disc_number"), 1) or 1,
    }


def proposals(album: Dict[str, Any], artist: Dict[str, Any], tracks: List[Dict[str, Any]],
              apply_rules: bool, semicolons: bool = True) -> List[Dict[str, Any]]:
    """Per track: ``{"source": tags from the source, "proposed": tags to write}``.

    ``semicolons`` (kept name; "separate multiple artists") splits Artist and
    Album artist into individual artists whatever separator the source used
    ("A, B", "A & B", "A feat. B") and shows them the way the configured
    strategy stores them: "A; B" for separate tags, else the chosen separator.
    """
    out = []
    for position, track in enumerate(tracks):
        source = source_tags(album, artist, track, position)
        proposed = dict(source)
        if apply_rules:
            try:
                # saved translations only: the preview must never wait for the
                # model. Names without one are reported by untranslated() and
                # translated in the background.
                proposed.update({k: v for k, v in tags.transform_values(source, allow_llm=False).items()
                                 if isinstance(v, str)})
            except Exception as exc:
                logger.warning("Could not apply rules to %r: %s", source.get("title"), exc)
        if semicolons:
            try:
                # the source's own artist LIST is the best split there is; each
                # entry is still checked for a separator hiding inside it
                names = artist_format.artist_list(_artist_names(track) or [source["artist"]], apply_rules)
                album_names = artist_format.artist_list([source["albumartist"]], apply_rules)
                proposed["artist"] = artist_format.display(names) or proposed["artist"]
                proposed["albumartist"] = artist_format.display(album_names) or proposed["albumartist"]
            except Exception as exc:
                logger.warning("Could not normalise artist separators for %r: %s", source.get("title"), exc)
        out.append({"source": source, "proposed": proposed})
    return out


# ── library check ───────────────────────────────────────────────────────

def _resolve(file_path: Any) -> Optional[str]:
    if not file_path:
        return None
    try:
        from core.library.path_resolver import resolve_library_file_path
        from core.settings import config_manager

        return resolve_library_file_path(file_path, config_manager=config_manager)
    except Exception as exc:
        logger.debug("Could not resolve %r: %s", file_path, exc)
        return None


def guess_folder(paths: List[str]) -> str:
    """The folder most of ``paths`` live in; the shared parent when the album
    is split over disc subfolders."""
    dirs = [os.path.dirname(p) for p in paths if p]
    if not dirs:
        return ""
    counts = Counter(dirs)
    if len(counts) > 1 and all(_DISC_DIR_RE.match(os.path.basename(d)) for d in counts):
        parents = {os.path.dirname(d) for d in counts}
        if len(parents) == 1:
            return parents.pop()
    return counts.most_common(1)[0][0]


def check_album(db: Any, album: Dict[str, Any], artist: Dict[str, Any], tracks: List[Dict[str, Any]],
                server_source: Optional[str] = None) -> Dict[str, Any]:
    """Which tracks of the release the library already has. Read-only."""
    album_name = str((album or {}).get("name") or "")
    results, paths = [], []
    for index, track in enumerate(tracks):
        title = str(track.get("name") or "")
        match = None
        try:
            match = ownership.find_by_external_id(db, track, server_source)
        except Exception as exc:
            logger.debug("external id check failed: %s", exc)
        if match is None and title:
            for name in _artist_names(track) or [_album_artist(album, artist)]:
                try:
                    found, confidence = db.check_track_exists(
                        title, name, confidence_threshold=0.7, server_source=server_source, album=album_name)
                except Exception as exc:
                    logger.debug("check_track_exists failed for %r: %s", title, exc)
                    continue
                if found is not None and (confidence or 0) >= 0.7:
                    match = found
                    break
        path = _resolve(getattr(match, "file_path", None)) if match is not None else None
        if path:
            paths.append(path)
        results.append({
            "index": index,
            "found": match is not None,
            "library_title": getattr(match, "title", None) if match is not None else None,
            "file": path,
        })
    folder = guess_folder(paths)
    return {"tracks": results, "folder": folder if folder and root_of(folder) else "",
            "found": sum(1 for r in results if r["found"]), "total": len(results)}


# ── folder scan + matching ──────────────────────────────────────────────

def _read_tags(path: str) -> Dict[str, Any]:
    from core.soulsync_client import _read_tags as read

    raw = read(path) or {}
    return {
        "title": raw.get("title") or "",
        "artist": raw.get("artist") or "",
        "albumartist": raw.get("album_artist") or "",
        "album": raw.get("album") or "",
        "year": str(raw.get("year") or ""),
        "track_number": _int(raw.get("track_number")),
        "disc_number": _int(raw.get("disc_number"), 1) or 1,
        "duration_ms": _int(raw.get("duration_ms")),
    }


def scan_folder(folder: str) -> List[Dict[str, Any]]:
    """Audio files in ``folder`` (and disc subfolders) with their current tags."""
    exts = _audio_exts()
    files: List[Dict[str, Any]] = []
    base_depth = folder.rstrip(os.sep).count(os.sep)
    for current, dirs, names in os.walk(folder):
        if current.rstrip(os.sep).count(os.sep) - base_depth >= _MAX_DEPTH:
            dirs[:] = []
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names, key=str.casefold):
            if os.path.splitext(name)[1].lower() not in exts or name.startswith("."):
                continue
            path = os.path.join(current, name)
            files.append({"path": path, "rel": os.path.relpath(path, folder), "current": _read_tags(path)})
            if len(files) >= _MAX_FILES:
                return files
    return files


def _norm(text: Any) -> str:
    from core.fork.cjk import fold

    return "".join(ch for ch in fold(text) if ch.isalnum())


def _stem_title(rel: str) -> str:
    stem = os.path.splitext(os.path.basename(rel))[0]
    return _LEADING_NUMBER_RE.sub("", stem).strip() or stem


def _title_score(file_titles: List[str], track_titles: List[str]) -> float:
    best = 0.0
    for candidate in file_titles:
        if not candidate:
            continue
        for wanted in track_titles:
            if not wanted:
                continue
            if ownership.names_equivalent("title", wanted, candidate) or \
                    ownership.names_equivalent("title", candidate, wanted):
                return 1.0
            a, b = _norm(candidate), _norm(wanted)
            if a and b:
                best = max(best, SequenceMatcher(None, a, b).ratio())
    return best


def score(file: Dict[str, Any], track: Dict[str, Any], proposal: Dict[str, Any]) -> Tuple[float, str]:
    """``(score, reason)`` for pairing one file with one track."""
    current = file["current"]
    title = _title_score(
        [current.get("title", ""), _stem_title(file["rel"])],
        [proposal["source"]["title"], proposal["proposed"]["title"]],
    )
    position = bool(current.get("track_number")) \
        and current["track_number"] == proposal["source"]["track_number"] \
        and current.get("disc_number", 1) == proposal["source"]["disc_number"]
    duration = 0.0
    have, want = current.get("duration_ms") or 0, _int(track.get("duration_ms"))
    if have and want:
        diff = abs(have - want) / 1000.0
        duration = 0.15 if diff <= 3 else (0.0 if diff <= 10 else (-0.1 if diff <= 30 else -0.3))
    total = title * 0.6 + (0.25 if position else 0.0) + duration
    if title >= 0.999:
        reason = "title"
    elif title >= 0.75:
        reason = "similar title"
    elif position:
        reason = "track number"
    else:
        reason = "weak"
    return total, reason


def match_files(files: List[Dict[str, Any]], tracks: List[Dict[str, Any]],
                props: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Best one-to-one pairing: ``{file index: {"track", "score", "reason"}}``."""
    scored = []
    for f_idx, file in enumerate(files):
        for t_idx, track in enumerate(tracks):
            value, reason = score(file, track, props[t_idx])
            if value >= 0.25:
                scored.append((value, f_idx, t_idx, reason))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    assigned: Dict[int, Dict[str, Any]] = {}
    used_tracks: set = set()
    for value, f_idx, t_idx, reason in scored:
        if f_idx in assigned or t_idx in used_tracks:
            continue
        assigned[f_idx] = {"track": t_idx, "score": round(value, 2), "reason": reason}
        used_tracks.add(t_idx)
    return assigned


def untranslated(props: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Album and title names in these proposals that have no saved
    translation yet: ``{"kind", "original", "artist", "album"}``."""
    from core.fork import store, translate
    from core.fork.cjk import contains_cjk, split_name

    out: List[Dict[str, str]] = []
    seen = set()
    for prop in props:
        source = prop["source"]
        album_core = split_name(source.get("album") or "")[0]
        for kind, value in (("album", source.get("album")), ("title", source.get("title"))):
            if not value or not contains_cjk(value) or not translate.enabled(kind):
                continue
            core, existing, _suffix = split_name(value)
            if existing or not contains_cjk(core) or (kind, core) in seen:
                continue
            seen.add((kind, core))
            if store.find_translation(kind, core):
                continue
            out.append({"kind": kind, "original": core, "artist": source.get("artist") or "",
                        "album": "" if kind == "album" else album_core})
    return out


# ── background translation for the review dialog ────────────────────────

_translate_lock = threading.Lock()
_translate_job: Dict[str, Any] = {"id": 0, "running": False}


def translate_status() -> Dict[str, Any]:
    with _translate_lock:
        return dict(_translate_job)


def start_translate(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Translate ``items`` (as returned by :func:`untranslated`) in the
    background: load the model first, then several names per request."""
    from core.fork import ollama, translate

    clean = [{"kind": str(i.get("kind") or ""), "original": str(i.get("original") or "").strip(),
              "artist": str(i.get("artist") or ""), "album": str(i.get("album") or "")}
             for i in items if isinstance(i, dict)]
    clean = [i for i in clean if i["kind"] in ("album", "title") and i["original"]][:500]
    with _translate_lock:
        if _translate_job.get("running"):
            return dict(_translate_job)        # one at a time; the caller polls the running one
        job_id = int(_translate_job.get("id") or 0) + 1
        _translate_job.clear()
        _translate_job.update({"id": job_id, "running": True, "phase": "loading", "done": 0,
                               "total": len(clean), "translated": 0, "error": None})

    def update(**values: Any) -> None:
        with _translate_lock:
            if _translate_job.get("id") == job_id:
                _translate_job.update(values)

    def work() -> None:
        error = None
        translated = 0
        try:
            ollama.reset_cooldown()
            if not ollama.warm("names"):
                raise RuntimeError("The model could not be loaded. Is Ollama running?")
            update(phase="translating")
            done = 0
            for kind in ("album", "title"):
                batch = [i for i in clean if i["kind"] == kind]
                if not batch:
                    continue
                base = done

                def progress(finished: int, _pending: int, _base: int = base) -> None:
                    update(done=_base + finished)

                result = translate.translate_batch(kind, batch, 10, None, progress)
                translated += sum(1 for i in batch if i["original"] in result)
                done += len(batch)
                update(done=done)
        except Exception as exc:  # noqa: BLE001 - shown to the user through the status
            logger.warning("background translation failed: %s", exc)
            error = str(exc)
        update(running=False, phase="done", translated=translated, error=error)

    threading.Thread(target=work, name="fork-album-translate", daemon=True).start()
    return translate_status()


def preview(folder: str, album: Dict[str, Any], artist: Dict[str, Any], tracks: List[Dict[str, Any]],
            apply_rules: bool = True, semicolons: bool = True) -> Dict[str, Any]:
    real = safe_dir(folder)
    files = scan_folder(real)
    props = proposals(album, artist, tracks, apply_rules, semicolons)
    assigned = match_files(files, tracks, props)
    rows = []
    for f_idx, file in enumerate(files):
        match = assigned.get(f_idx)
        rows.append({
            "rel": file["rel"],
            "current": file["current"],
            "track": match["track"] if match else None,
            "score": match["score"] if match else 0,
            "reason": match["reason"] if match else "",
        })
    matched = {m["track"] for m in assigned.values()}
    return {
        "folder": real,
        "rows": rows,
        "tracks": [{"index": i, "number": p["source"]["track_number"], "disc": p["source"]["disc_number"],
                    "source": p["source"], "proposed": p["proposed"]} for i, p in enumerate(props)],
        "unmatched_tracks": [i for i in range(len(tracks)) if i not in matched],
        "total_tracks": len(tracks),
        "truncated": len(files) >= _MAX_FILES,
        "untranslated": untranslated(props) if apply_rules else [],
        "artist_mode": {"split_tags": artist_format.split_tags_enabled(),
                        "separator": artist_format.separator().strip() or artist_format.separator()},
    }


# ── apply ───────────────────────────────────────────────────────────────

def _clean_tags(raw: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for key in _EDITABLE:
        value = raw.get(key)
        if key in ("track_number", "disc_number"):
            number = _int(value)
            if number > 0:
                out[key] = number
        elif isinstance(value, (str, int)) and str(value).strip():
            out[key] = " ".join(str(value).split())
    return out


def _write_extras(path: str, originals: Dict[str, str], ids: Dict[str, str],
                  artists: Optional[Dict[str, List[str]]] = None) -> None:
    """Originals (for the fork's ownership check), source ids, and the artist
    fields in their final multi-artist form — additively."""
    if not originals and not ids and not artists:
        return
    from mutagen import File as MutagenFile

    audio = MutagenFile(path)
    if audio is None or audio.tags is None:
        return
    kind = tags._kind(audio)
    if not kind:
        return
    for field, names in (artists or {}).items():
        artist_format.write_values(audio, field, names)
    for field, value in originals.items():
        tags._write_original(audio, kind, field, value)
    if ids:
        try:
            from core.metadata.common import get_mutagen_symbols
            from core.metadata.musicbrainz_tags import write_tag

            symbols = get_mutagen_symbols()
            for name, value in ids.items():
                write_tag(audio, name, value, symbols)
        except Exception as exc:
            logger.debug("Could not write source ids to %s: %s", path, exc)
    tags._save(audio)


def _source_ids(source: str, album: Dict[str, Any], artist: Dict[str, Any], track: Dict[str, Any]) -> Dict[str, str]:
    from core.imports.context import get_source_tag_names

    names = get_source_tag_names(source or "")
    out: Dict[str, str] = {}
    for key, payload in (("track", track), ("album", album), ("artist", artist)):
        tag_name, value = names.get(key), (payload or {}).get("id")
        if tag_name and value not in (None, ""):
            out[tag_name] = str(value)
    isrc = track.get("isrc") or (track.get("external_ids") or {}).get("isrc")
    if isinstance(isrc, str) and isrc.strip():
        out["ISRC"] = isrc.strip()
    return out


def _template_path(root: str, values: Dict[str, Any], album: Dict[str, Any], total_tracks: int,
                   total_discs: int, ext: str) -> Optional[str]:
    """Destination for a file under the path template, from the exact tag
    values just written (no second round of rules or translation)."""
    from core.imports import paths

    album_type = "Album"
    try:
        album_type = paths.get_album_type_display((album or {}).get("album_type"), total_tracks, locked=True)
    except Exception:
        album_type = "Album"
    context = {
        "artist": values.get("artist") or values.get("albumartist") or "Unknown Artist",
        # a multi-artist album files under its first artist, as upstream does
        "albumartist": (artist_format.parse_display(values.get("albumartist") or values.get("artist") or "")
                        or ["Unknown Artist"])[0],
        "album": values.get("album") or "Unknown Album",
        "title": values.get("title") or "Unknown Track",
        "track_number": values.get("track_number") or 1,
        "disc_number": values.get("disc_number") or 1,
        "total_discs": total_discs,
        "year": values.get("year") or "",
        "albumtype": album_type,
    }
    folder, name = paths._upstream_get_file_path_from_template(context, "album_path")
    if not name:
        return None
    return os.path.join(root, folder or "", name + ext)


def _move(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    # lyrics follow the track; the fork's untranslated backup too
    from core.fork import lyrics

    src_stem, dst_stem = os.path.splitext(src)[0], os.path.splitext(dst)[0]
    lyrics.move_backups(src, dst)
    for ext in (".lrc", ".txt"):
        if os.path.isfile(src_stem + ext) and not os.path.exists(dst_stem + ext):
            shutil.move(src_stem + ext, dst_stem + ext)
    try:
        from core.imports.pipeline import _update_moved_track_file_path

        _update_moved_track_file_path(src, dst)
    except Exception as exc:
        logger.debug("Could not update the library path for %s: %s", dst, exc)
    try:
        os.rmdir(os.path.dirname(src))  # only succeeds when now empty
    except OSError:
        pass


def apply(folder: str, rows: List[Dict[str, Any]], album: Dict[str, Any], artist: Dict[str, Any],
          tracks: List[Dict[str, Any]], source: str = "", rename: bool = False,
          apply_rules: bool = True, separate_artists: bool = True) -> Dict[str, Any]:
    """Write tags for each row ``{"rel", "track", "tags"}``; rename when asked."""
    from core.tag_writer import write_tags_to_file

    real = safe_dir(folder)
    root = root_of(real) or real
    keep_originals = apply_rules and bool(config.get("translate.write_original_tags"))
    total_discs = max([_int((r.get("tags") or {}).get("disc_number"), 1) for r in rows] or [1])
    results, written, moved = [], 0, 0
    for row in rows if isinstance(rows, list) else []:
        rel = str(row.get("rel") or "")
        path = os.path.realpath(os.path.join(real, rel))
        entry: Dict[str, Any] = {"rel": rel, "ok": False}
        results.append(entry)
        if not rel or not _inside(path, real) or not os.path.isfile(path):
            entry["error"] = "File not found in the folder"
            continue
        values = _clean_tags(row.get("tags"))
        if not values:
            entry["error"] = "No tag values"
            continue
        db_data = {
            "title": values.get("title"),
            "track_artist": values.get("artist"),
            "artist_name": values.get("albumartist") or values.get("artist"),
            "album_title": values.get("album"),
            "year": values.get("year"),
            "track_number": values.get("track_number"),
            "disc_number": values.get("disc_number"),
            "track_count": len(tracks) or None,
        }
        outcome = write_tags_to_file(path, {k: v for k, v in db_data.items() if v not in (None, "")},
                                     embed_cover=False)
        if not outcome.get("success"):
            entry["error"] = outcome.get("error") or "Could not write tags"
            continue
        t_idx = row.get("track")
        track = tracks[t_idx] if isinstance(t_idx, int) and 0 <= t_idx < len(tracks) else None
        try:
            originals: Dict[str, str] = {}
            if track is not None and keep_originals:
                src = source_tags(album, artist, track, t_idx)
                originals = {f: src[f] for f in ("title", "album", "artist", "albumartist")
                             if src.get(f) and values.get(f) and src[f] != values[f]}
            # the text boxes hold one line per field; store it per the strategy
            # (separate tag values, or the single joined value as typed)
            artists = {}
            if separate_artists:
                artists = {f: artist_format.parse_display(values[f]) for f in ("artist", "albumartist") if values.get(f)}
            _write_extras(path, originals, _source_ids(source, album, artist, track) if track is not None else {},
                          artists)
        except Exception as exc:
            logger.debug("Extras not written for %s: %s", path, exc)
        entry["ok"] = True
        written += 1
        if rename:
            try:
                target = _template_path(root, values, album, len(tracks), total_discs,
                                        os.path.splitext(path)[1])
                if target and os.path.normpath(target) != os.path.normpath(path):
                    if os.path.exists(target):
                        entry["rename_error"] = "A file already exists at the new location"
                    else:
                        _move(path, target)
                        entry["moved_to"] = target
                        moved += 1
            except Exception as exc:
                entry["rename_error"] = str(exc)
    return {"results": results, "written": written, "moved": moved,
            "failed": sum(1 for r in results if not r["ok"])}
