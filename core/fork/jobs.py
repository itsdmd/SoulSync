"""The work behind the fork's Tools-page jobs (the job classes themselves are
in ``core/repair_jobs/fork_tools.py``, where the framework looks for them).

* **Auto Translate** — finds album and song names in the library that are
  still untranslated, translates them in batches (several names per model
  call), and applies the result to the files.
* **Album Volume Grouping** — finds albums released as "…, Vol. 1", "…, Vol. 2"
  and turns each set into ONE album whose volumes are its discs.

Both follow the framework's contract: the scan creates findings (dry run, the
default) or applies them straight away; a finding's fix does the same work
for one item.
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.fork import retro, translate
from core.fork.cjk import contains_cjk, fold, split_name
from core.repair_jobs.base import RepairJob
from utils.logging_config import get_logger

logger = get_logger("fork.jobs")


def job_settings(job: RepairJob, context: Any) -> Dict[str, Any]:
    merged = dict(job.default_settings)
    cfg = getattr(context, "config_manager", None)
    try:
        if cfg is not None:
            merged.update(cfg.get(f"repair.jobs.{job.job_id}.settings", {}) or {})
    except Exception as exc:
        logger.debug("could not read settings for %s: %s", job.job_id, exc)
    return merged


def is_on(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _rows(db: Any, sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    return retro._query(db, sql, params)


# ═══════════════════════════════════════════════════════════════════════
# Auto Translate
# ═══════════════════════════════════════════════════════════════════════

def untranslated_names(db: Any, albums: bool = True, titles: bool = True) -> Dict[str, List[Dict[str, Any]]]:
    """Library names that still read as an untranslated CJK name.

    Returns ``{"album": [...], "title": [...]}``; each entry is
    ``{"original", "artist", "album", "count", "example"}`` with one entry per
    distinct name (Traditional/Simplified spellings counted together).
    """
    out: Dict[str, List[Dict[str, Any]]] = {"album": [], "title": []}
    sources = []
    if albums:
        sources.append(("album", "SELECT al.title AS name, ar.name AS artist, al.title AS album, COUNT(t.album_id) AS n "
                                 "FROM albums al JOIN artists ar ON ar.id = al.artist_id "
                                 "LEFT JOIN tracks t ON t.album_id = al.id GROUP BY al.id"))
    if titles:
        sources.append(("title", "SELECT t.title AS name, ar.name AS artist, al.title AS album, 1 AS n "
                                 "FROM tracks t JOIN albums al ON al.id = t.album_id "
                                 "JOIN artists ar ON ar.id = t.artist_id"))
    for kind, sql in sources:
        seen: Dict[str, Dict[str, Any]] = {}
        for row in _rows(db, sql):
            name = str(row.get("name") or "")
            if not contains_cjk(name):
                continue
            core, existing, _suffix = split_name(name)
            # "<translation> (<original>)" is already translated
            if existing or not contains_cjk(core):
                continue
            key = fold(core)
            entry = seen.get(key)
            if entry is None:
                album_core = split_name(str(row.get("album") or ""))[0]
                entry = seen[key] = {"original": core, "artist": row.get("artist") or "",
                                     "album": "" if kind == "album" else album_core,
                                     "count": 0, "example": name}
            entry["count"] += int(row.get("n") or 1)
        out[kind] = list(seen.values())
    return out


def apply_translation_finding(db: Any, details: Dict[str, Any]) -> Dict[str, Any]:
    kind, original = str(details.get("kind") or ""), str(details.get("original") or "")
    try:
        data = retro.apply_translation(db, kind, original, rename=is_on(details.get("rename_files", True)))
    except LookupError:
        return {"success": False, "error": "The saved translation for this name was removed"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if data["errors"] and not data["written"]:
        return {"success": False, "error": "; ".join(data["errors"][:3])}
    message = f'Updated {data["written"]} file(s) to "{data["display"]}"'
    if data["renamed"]:
        message += f', renamed {data["renamed"]}'
    if data["errors"]:
        message += f' ({len(data["errors"])} problem(s))'
    if not data["written"]:
        message = "Nothing left to change (already applied, or the files are not in the library folders)"
    return {"success": True, "action": "translation_applied", "message": message, "fixed": data["written"]}


# ═══════════════════════════════════════════════════════════════════════
# Album Volume Grouping
# ═══════════════════════════════════════════════════════════════════════

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
          "xi": 11, "xii": 12}
_CJK_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
               "壹": 1, "貳": 2, "參": 3, "上": 1, "中": 2, "下": 3}
# "Vol. 2", "Volume II", "Pt. 3", "Part 2", "Disc 1", "CD2", "第二卷", "卷三".
# A closing bracket is part of the marker only when the marker opened one:
# "(Disc 3)" goes whole, but in "(足跡 Vol.2)" the ")" closes the name's bracket.
_VOLUME_RE = re.compile(
    r"[\s,，、:：\-–—]*(?P<open>[\(\[（【])?\s*(?:"
    r"(?<![a-z])(?:vol(?:ume)?|pt|part|disc|disk|cd)\s*[.,:#]?\s*(?P<latin>\d{1,3}|[ivx]{1,4})(?![a-z0-9])"
    r"|第\s*(?P<cjk1>\d{1,3}|[一二三四五六七八九十壹貳參]{1,3})\s*[卷巻集部章辑輯彈弹]"
    r"|[卷巻]\s*(?P<cjk2>\d{1,3}|[一二三四五六七八九十]{1,3})"
    r")(?(open)\s*[\)\]）】])",
    re.I,
)


def _number(text: str) -> Optional[int]:
    text = (text or "").strip().lower()
    if text.isdigit():
        return int(text)
    if text in _ROMAN:
        return _ROMAN[text]
    if text in _CJK_DIGITS:
        return _CJK_DIGITS[text]
    if len(text) == 2 and text[0] == "十" and text[1] in _CJK_DIGITS:
        return 10 + _CJK_DIGITS[text[1]]
    if len(text) == 2 and text[1] == "十" and text[0] in _CJK_DIGITS:
        return _CJK_DIGITS[text[0]] * 10
    return None


def parse_volume(title: str) -> Optional[Tuple[str, int]]:
    """``(album name without the volume marker, volume number)``, or None.

    A translated name carries the marker twice — "Footprints Vol. 2 (足跡
    Vol.2)" — so every marker is removed, and they must agree on the number.
    """
    title = " ".join(str(title or "").split())
    numbers = set()
    for match in _VOLUME_RE.finditer(title):
        number = _number(match.group("latin") or match.group("cjk1") or match.group("cjk2") or "")
        if number is None or number <= 0:
            return None
        numbers.add(number)
    if len(numbers) != 1:
        return None
    base = _VOLUME_RE.sub(" ", title)
    base = re.sub(r"[\(\[（【]\s*[\)\]）】]", " ", base)             # brackets the marker left empty
    base = re.sub(r"\s+([\)\]）】,，])", r"\1", " ".join(base.split()))
    base = base.strip(" ,，、:：-–—")
    if len(base) < 2:
        return None
    return base, numbers.pop()


_BRACKETED_RE = re.compile(r"[\(\[（【][^\)\]）】]*[\)\]）】]")


def _strict_key(name: str) -> str:
    """Letters and digits only: "Epic Collection, " and "Epic Collection:" agree."""
    return "".join(ch for ch in fold(name) if ch.isalnum())


def _loose_key(name: str) -> str:
    """Also without bracketed notes: "X (Cover)" and "X" agree."""
    return _strict_key(_BRACKETED_RE.sub(" ", name)) or _strict_key(name)


def _album_folder(db: Any, album_id: Any) -> str:
    from core.fork import album_tagging

    paths = [album_tagging._resolve(r.get("file_path")) for r in
             _rows(db, "SELECT file_path FROM tracks WHERE album_id = ? LIMIT 50", (album_id,))]
    return album_tagging.guess_folder([p for p in paths if p])


def volume_groups(db: Any) -> List[Dict[str, Any]]:
    """Sets of albums by one artist that are volumes of the same release.

    Volumes are named inconsistently — "X, Vol. 2", "X Vol.3", "X (Vol. 4)",
    "X - Volume 5 (Cover)" — and the first one often has no marker at all
    ("X"). So albums are grouped by the name left once the marker is gone,
    compared on letters and digits only; sets differing just by a bracketed
    note are joined when their numbers do not clash; and an unmarked album
    with that same name becomes volume 1 when no album claims that number.
    """
    albums = _rows(db, "SELECT al.id, al.title, ar.id AS artist_id, ar.name AS artist, COUNT(t.album_id) AS tracks "
                       "FROM albums al JOIN artists ar ON ar.id = al.artist_id "
                       "LEFT JOIN tracks t ON t.album_id = al.id GROUP BY al.id")
    groups: Dict[Tuple[Any, str], Dict[str, Any]] = {}
    unmarked: List[Dict[str, Any]] = []
    for album in albums:
        if not album.get("tracks"):
            continue
        parsed = parse_volume(album.get("title") or "")
        if not parsed:
            unmarked.append(album)
            continue
        base, number = parsed
        group = groups.setdefault((album["artist_id"], _strict_key(base)), {
            "artist": album["artist"], "artist_id": album["artist_id"], "album": base,
            "keys": {_strict_key(base)}, "loose": _loose_key(base), "volumes": []})
        group["volumes"].append({"album_id": album["id"], "title": album["title"], "number": number,
                                 "tracks": int(album["tracks"])})

    def numbers(group: Dict[str, Any]) -> List[int]:
        return [v["number"] for v in group["volumes"]]

    # sets whose names differ only by a bracketed note are one set, numbers permitting
    merged: Dict[Tuple[Any, str], Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for group in sorted(groups.values(), key=lambda g: (len(g["album"]), g["album"])):
        if len(set(numbers(group))) != len(numbers(group)):
            continue                       # two albums claim one number: left alone
        home = merged.get((group["artist_id"], group["loose"]))
        if home is not None and not set(numbers(home)) & set(numbers(group)):
            home["volumes"].extend(group["volumes"])
            home["keys"] |= group["keys"]
            continue
        if home is None:
            merged[(group["artist_id"], group["loose"])] = group
        out.append(group)

    # the first volume often carries no marker
    taken: set = set()
    for group in out:
        if 1 in numbers(group):
            continue
        exact = [a for a in unmarked if a["artist_id"] == group["artist_id"]
                 and _strict_key(a["title"]) in group["keys"] and a["id"] not in taken]
        close = exact or [a for a in unmarked if a["artist_id"] == group["artist_id"]
                          and _loose_key(a["title"]) == group["loose"] and a["id"] not in taken]
        if len(close) == 1:
            first = close[0]
            taken.add(first["id"])
            group["volumes"].append({"album_id": first["id"], "title": first["title"], "number": 1,
                                     "tracks": int(first["tracks"]), "assumed": True})

    # The library can hold one artist twice (once per media server). Both rows
    # describe the same files, so the same set is reported once: the fuller one.
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for group in out:
        if len(group["volumes"]) < 2:
            continue
        key = (str(group["artist"]).casefold(), _strict_key(group["album"]))
        size = (len(group["volumes"]), sum(v["tracks"] for v in group["volumes"]))
        if key not in best or size > best[key]["_size"]:
            group["_size"] = size
            best[key] = group
    result = []
    for group in best.values():
        group["volumes"].sort(key=lambda v: v["number"])
        for volume in group["volumes"]:
            volume["folder"] = _album_folder(db, volume["album_id"])
        result.append({"artist": group["artist"], "album": group["album"], "volumes": group["volumes"]})
    return sorted(result, key=lambda g: (str(g["artist"]).casefold(), str(g["album"]).casefold()))


# ── editing a set by hand ───────────────────────────────────────────────

def search_albums(db: Any, query: str, limit: int = 60) -> List[Dict[str, Any]]:
    """Library albums whose title or artist contains every word of ``query``."""
    words = [w for w in str(query or "").split() if w][:6]
    if not words:
        return []
    where = " AND ".join("(al.title LIKE ? OR ar.name LIKE ?)" for _ in words)
    params: List[Any] = []
    for word in words:
        params += [f"%{word}%", f"%{word}%"]
    rows = _rows(db, "SELECT al.id, al.title, ar.name AS artist, COUNT(t.album_id) AS tracks "
                     "FROM albums al JOIN artists ar ON ar.id = al.artist_id "
                     f"LEFT JOIN tracks t ON t.album_id = al.id WHERE {where} "
                     "GROUP BY al.id ORDER BY ar.name, al.title LIMIT ?", tuple(params) + (int(limit),))
    return [{"album_id": r["id"], "title": r["title"], "artist": r["artist"], "tracks": int(r["tracks"] or 0)}
            for r in rows]


def describe_volumes(db: Any, items: Any) -> List[Dict[str, Any]]:
    """Checked, complete volume entries for hand-picked ``items``: each a
    library album (``album_id``) or a folder inside the library (``folder``),
    with the disc ``number`` it should become."""
    from core.fork import album_tagging

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("number"))
        except (TypeError, ValueError):
            raise ValueError("Every item needs a disc number") from None
        if not 1 <= number <= 99:
            raise ValueError("Disc numbers go from 1 to 99")
        if str(item.get("folder") or "").strip() and not item.get("album_id"):
            folder = album_tagging.safe_dir(item["folder"])
            key, entry = ("folder", folder), {
                "folder": folder, "title": os.path.basename(folder), "number": number,
                "tracks": album_tagging.count_audio_deep(folder)}
        else:
            rows = _rows(db, "SELECT al.id, al.title, COUNT(t.album_id) AS tracks FROM albums al "
                             "LEFT JOIN tracks t ON t.album_id = al.id WHERE al.id = ? GROUP BY al.id",
                         (item.get("album_id"),))
            if not rows:
                raise ValueError(f"Album {item.get('album_id')!r} is not in the library anymore")
            key, entry = ("album", rows[0]["id"]), {
                "album_id": rows[0]["id"], "title": rows[0]["title"], "number": number,
                "tracks": int(rows[0]["tracks"] or 0), "folder": _album_folder(db, rows[0]["id"])}
        if key in seen:
            raise ValueError(f'"{entry["title"]}" is listed twice')
        seen.add(key)
        if not entry["tracks"]:
            raise ValueError(f'"{entry["title"]}" has no audio files')
        out.append(entry)
    if len(out) < 2:
        raise ValueError("A set needs at least two items")
    if len({v["number"] for v in out}) != len(out):
        raise ValueError("Two items have the same disc number")
    return sorted(out, key=lambda v: v["number"])


def finding_details(album: str, artist: str, volumes: List[Dict[str, Any]], move_files: bool) -> Dict[str, Any]:
    return {
        "artist": artist, "album": album, "volume_count": len(volumes),
        "volume_numbers": ", ".join(str(v["number"]) for v in volumes),
        "track_count": sum(int(v.get("tracks") or 0) for v in volumes),
        "volumes": volumes, "move_files": move_files,
    }


def finding_text(details: Dict[str, Any]) -> Tuple[str, str]:
    count = len(details["volumes"])
    return (f'Volumes of one album: {details["album"]}',
            f'{count} volumes by {details["artist"]} would become one album "{details["album"]}" '
            f'with {count} discs ({details["track_count"]} tracks)')


def update_volume_finding(db: Any, finding_id: Any, album: Any, items: Any) -> Dict[str, Any]:
    """Replace what a pending "volumes of one album" finding will group."""
    import json

    from core.fork import store

    rows = _rows(db, "SELECT id, status, finding_type, details_json FROM repair_findings WHERE id = ?", (finding_id,))
    if not rows or rows[0]["finding_type"] != "fork_album_volumes":
        raise FileNotFoundError("Finding not found")
    if rows[0]["status"] != "pending":
        raise ValueError("This finding is no longer open")
    try:
        old = json.loads(rows[0]["details_json"] or "{}")
    except ValueError:
        old = {}
    album = " ".join(str(album or "").split())
    if len(album) < 1:
        raise ValueError("The album needs a name")
    details = finding_details(album, str(old.get("artist") or ""), describe_volumes(db, items),
                              is_on(old.get("move_files", True)))
    details["edited"] = True          # a later scan leaves a hand-made set alone
    title, description = finding_text(details)
    with store.connect() as conn:
        conn.execute("UPDATE repair_findings SET details_json = ?, title = ?, description = ?, "
                     "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (json.dumps(details, ensure_ascii=False), title, description, finding_id))
    return details


def refresh_volume_finding(job_id: str, entity_id: str, details: Dict[str, Any]) -> bool:
    """A scan found this set again: bring its open finding up to date, unless
    the user has edited it."""
    import json

    from core.fork import store

    title, description = finding_text(details)
    with store.connect() as conn:
        rows = conn.execute("SELECT id, details_json FROM repair_findings WHERE job_id = ? AND entity_id = ? "
                            "AND status = 'pending'", (job_id, entity_id)).fetchall()
        changed = False
        for row in rows:
            try:
                old = json.loads(row["details_json"] or "{}")
            except ValueError:
                old = {}
            if old.get("edited") or old.get("volumes") == details["volumes"]:
                continue
            conn.execute("UPDATE repair_findings SET details_json = ?, title = ?, description = ?, "
                         "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (json.dumps(details, ensure_ascii=False), title, description, row["id"]))
            changed = True
    return changed


def _strip_marker_from_folder(name: str) -> str:
    parsed = parse_volume(name)
    return parsed[0] if parsed else name


def group_volumes(db: Any, details: Dict[str, Any], move_files: bool = True) -> Dict[str, Any]:
    """Make one album out of a set of volumes: every track gets the common
    album name and its volume as the disc number; with ``move_files`` the
    files go to ``<album folder>/Disc N/``."""
    from core.fork import album_tagging
    from core.tag_writer import write_tags_to_file

    album = str(details.get("album") or "").strip()
    volumes = [v for v in (details.get("volumes") or []) if isinstance(v, dict)]
    if not album or len(volumes) < 2:
        return {"success": False, "error": "Finding has no volumes to group"}
    total_discs = max(int(v.get("number") or 0) for v in volumes)

    files: List[Tuple[str, int]] = []
    for volume in sorted(volumes, key=lambda v: int(v.get("number") or 0)):
        if not volume.get("album_id") and volume.get("folder"):
            # a folder picked by hand: everything in it is that disc
            try:
                folder = album_tagging.safe_dir(volume["folder"])
            except (ValueError, PermissionError, FileNotFoundError) as exc:
                logger.warning("Volume folder skipped: %s", exc)
                continue
            files += [(f["path"], int(volume["number"])) for f in album_tagging.list_audio(folder)]
            continue
        for row in _rows(db, "SELECT file_path FROM tracks WHERE album_id = ?", (volume.get("album_id"),)):
            path = album_tagging._resolve(row.get("file_path"))
            if path and os.path.isfile(path) and album_tagging.root_of(path):
                files.append((path, int(volume["number"])))
    if not files:
        return {"success": True, "action": "already_gone", "message": "No files found for these volumes anymore"}

    # one album folder for the whole set: the first volume's folder, minus its marker
    first_dir = os.path.dirname(files[0][0])
    if re.match(r"^(?:disc|disk|cd)\s*\d+$", os.path.basename(first_dir), re.I):
        first_dir = os.path.dirname(first_dir)
    target_root = os.path.join(os.path.dirname(first_dir), _strip_marker_from_folder(os.path.basename(first_dir)))

    written = moved = 0
    errors: List[str] = []
    touched: List[Tuple[str, str, str]] = []
    for path, number in files:
        outcome = write_tags_to_file(path, {"album_title": album, "disc_number": number}, embed_cover=False)
        if not outcome.get("success"):
            errors.append(f"{os.path.basename(path)}: {outcome.get('error') or 'could not write tags'}")
            continue
        try:
            _write_disc_total(path, number, total_discs)
        except Exception as exc:
            logger.debug("disc total not written for %s: %s", path, exc)
        written += 1
        if not move_files:
            continue
        target = os.path.join(target_root, f"Disc {number}", os.path.basename(path))
        if os.path.normpath(target) == os.path.normpath(path):
            continue
        try:
            if os.path.exists(target):
                raise FileExistsError("a file already exists at the new location")
            retro._move_file(path, target)
            moved += 1
            entry = (os.path.dirname(path), os.path.dirname(target), album_tagging.root_of(target) or "")
            if entry not in touched:
                touched.append(entry)
        except Exception as exc:
            errors.append(f"{os.path.basename(path)}: move failed: {exc}")
    for old_dir, new_dir, root in sorted(touched, key=lambda item: -len(item[0])):
        try:
            retro._carry_leftovers(old_dir, new_dir, root)
        except Exception as exc:
            logger.debug("could not tidy %s: %s", old_dir, exc)
    if move_files and moved:
        _album_cover(target_root)

    if not written:
        return {"success": False, "error": "; ".join(errors[:3]) or "No file could be updated"}
    message = f'Grouped {len(volumes)} volumes into "{album}" ({written} track(s), {total_discs} discs)'
    if moved:
        message += f", moved {moved}"
    if errors:
        message += f" — {len(errors)} problem(s): {errors[0]}"
    logger.info("Volume grouping: %s", message)
    return {"success": True, "action": "volumes_grouped", "message": message, "fixed": written}


def _write_disc_total(path: str, number: int, total: int) -> None:
    """Disc ``number/total`` in the form each format expects."""
    from mutagen import File as MutagenFile
    from mutagen import id3

    from core.fork import tags

    audio = MutagenFile(path)
    if audio is None or audio.tags is None:
        return
    kind = tags._kind(audio)
    if kind == "id3":
        audio.tags.setall("TPOS", [id3.TPOS(encoding=3, text=[f"{number}/{total}"])])
    elif kind == "vorbis":
        audio["discnumber"] = [str(number)]
        audio["disctotal"] = [str(total)]
        audio["totaldiscs"] = [str(total)]
    elif kind == "mp4":
        audio["disk"] = [(number, total)]
    else:
        return
    tags._save(audio)


def _album_cover(target_root: str) -> None:
    """Give the merged album folder a cover of its own (disc 1's)."""
    for name in ("cover.jpg", "cover.png", "folder.jpg"):
        if os.path.exists(os.path.join(target_root, name)):
            return
    for disc in sorted(d for d in os.listdir(target_root) if os.path.isdir(os.path.join(target_root, d))):
        for name in ("cover.jpg", "cover.png", "folder.jpg"):
            src = os.path.join(target_root, disc, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(target_root, name))
                return


# ── fix handlers ────────────────────────────────────────────────────────

def fix_handlers(worker: Any) -> Dict[str, Callable[..., Dict[str, Any]]]:
    def translation(entity_type: Any, entity_id: Any, file_path: Any, details: Any) -> Dict[str, Any]:
        return apply_translation_finding(worker.db, details or {})

    def volumes(entity_type: Any, entity_id: Any, file_path: Any, details: Any) -> Dict[str, Any]:
        details = details or {}
        return group_volumes(worker.db, details, is_on(details.get("move_files", True)))

    return {"fork_untranslated": translation, "fork_album_volumes": volumes}


__all__ = ["apply_translation_finding", "describe_volumes", "fix_handlers", "group_volumes", "parse_volume",
           "refresh_volume_finding", "search_albums", "untranslated_names", "update_volume_finding",
           "volume_groups"]
