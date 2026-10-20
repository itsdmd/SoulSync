"""Duplicate Tracks, Keep Best: keep the best copy *where it belongs*.

The stock fix keeps the best copy (playlist first, then format, bitrate…) and
moves the other copies to the deleted-files folder, wherever each one sat. When
the best copy is a single or a stray in a thin album while a worse copy sits
in a fuller album, that empties the fuller album of the song.

So before the stock fix runs: when another copy's album holds more tracks
than the best copy's album, the best file takes that copy's place — the worse
file goes to the deleted-files folder, the better one moves to its path (with
its own extension) and gets that album's tags: title, track and disc number,
album, album artist, date and the release-level MusicBrainz ids and release
details (fields that album's file lacks are removed, so no id of the old
release is left for Navidrome to split the track out by). The library row of
that album keeps the song. Any further copies then go the stock way.

Left to the stock fix: a copy picked by hand (Keep this version), a best copy
in a playlist (moving it would drop it from the playlist), copies whose
lengths differ (another recording), and files outside the library folders.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from utils.logging_config import get_logger

logger = get_logger("fork.duplicate_placement")

# release-level fields beyond album_duplicates' set: field -> (ID3, Vorbis, MP4)
_MORE_FIELDS = {
    "originaldate": ("TDOR", "originaldate", "----:com.apple.iTunes:ORIGINALDATE"),
    "releasetype": ("TXXX:MusicBrainz Album Type", "releasetype", "----:com.apple.iTunes:MusicBrainz Album Type"),
    "releasestatus": ("TXXX:MusicBrainz Album Status", "releasestatus",
                      "----:com.apple.iTunes:MusicBrainz Album Status"),
    "barcode": ("TXXX:BARCODE", "barcode", "----:com.apple.iTunes:BARCODE"),
    "catalognumber": ("TXXX:CATALOGNUMBER", "catalognumber", "----:com.apple.iTunes:CATALOGNUMBER"),
    "musicbrainz_releasetrackid": ("TXXX:MusicBrainz Release Track Id", "musicbrainz_releasetrackid",
                                   "----:com.apple.iTunes:MusicBrainz Release Track Id"),
}
_LENGTH_TOLERANCE = 3.0


def _fields() -> Dict[str, Any]:
    from core.fork import album_duplicates

    return {**{k: v for k, v in album_duplicates._RELEASE_FIELDS.items() if k != "album"}, **_MORE_FIELDS}


def _read_frames(path: str) -> Dict[str, Any]:
    """The raw release-level tag values of ``path``, per field (None = absent)."""
    from mutagen import File as MutagenFile

    from core.fork import tags

    audio = MutagenFile(path)
    kind = tags._kind(audio) if audio is not None and audio.tags is not None else ""
    out: Dict[str, Any] = {"_kind": kind}
    for field, (frame, vkey, atom) in _fields().items():
        value = None
        if kind == "id3":
            found = audio.tags.get(frame)
            value = [str(t) for t in found.text] if found is not None and getattr(found, "text", None) else None
        elif kind == "vorbis":
            value = list(audio.get(vkey) or []) or None
        elif kind == "mp4":
            raw = audio.get(atom)
            value = [bytes(v).decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)
                     for v in raw] if raw else None
        out[field] = value
    return out


def _copy_release(path: str, values: Dict[str, Any]) -> None:
    """Make the release-level tags of ``path`` exactly ``values``: present
    fields written, absent ones removed."""
    from mutagen import File as MutagenFile
    from mutagen import id3
    from mutagen.mp4 import MP4FreeForm

    from core.fork import tags

    audio = MutagenFile(path)
    if audio is None:
        return
    if audio.tags is None:
        audio.add_tags()
    kind = tags._kind(audio)
    for field, (frame, vkey, atom) in _fields().items():
        value = values.get(field)
        if kind == "id3":
            audio.tags.delall(frame)
            if value:
                if frame.startswith("TXXX:"):
                    audio.tags.add(id3.TXXX(encoding=3, desc=frame[5:], text=list(value)))
                else:
                    audio.tags.add(getattr(id3, frame)(encoding=3, text=list(value)))
        elif kind == "vorbis":
            if value:
                audio[vkey] = list(value)
            elif vkey in audio:
                del audio[vkey]
        elif kind == "mp4":
            if value:
                audio[atom] = ([MP4FreeForm(str(v).encode("utf-8")) for v in value] if atom.startswith("----")
                               else [str(v) for v in value])
            else:
                audio.pop(atom, None)
    tags._save(audio)


def _album_sizes(db: Any, track_ids: List[Any]) -> Dict[str, Dict[str, Any]]:
    """``{track id: {"album_id", "album", "size"}}`` — size = the album's track rows."""
    conn = db._get_connection()
    try:
        marks = ", ".join("?" for _ in track_ids)
        rows = conn.execute(
            f"SELECT t.id, t.album_id, al.title, (SELECT COUNT(*) FROM tracks x WHERE x.album_id = t.album_id) "
            f"FROM tracks t LEFT JOIN albums al ON al.id = t.album_id WHERE t.id IN ({marks})",
            tuple(track_ids)).fetchall()
    finally:
        conn.close()
    return {str(r[0]): {"album_id": r[1], "album": r[2] or "", "size": int(r[3] or 0)} for r in rows}


def _length(path: str) -> float:
    from mutagen import File as MutagenFile

    try:
        audio = MutagenFile(path)
        return float(audio.info.length) if audio is not None else 0.0
    except Exception:
        return 0.0


def plan(worker: Any, details: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``{"best", "home", "best_path", "home_path", "home_album"}`` when the
    best copy should take the place of a copy in a fuller album, else None."""
    from core import repair_worker as rw
    from core.fork import album_tagging
    from core.library.duplicate_keep import pick_duplicate_to_keep

    tracks = [t for t in details.get("tracks") or [] if isinstance(t, dict)]
    if details.get("_fix_action") or len(tracks) < 2:
        return None
    membership = rw._server_playlist_membership()
    if membership:
        from core.library.playlist_membership import tag_tracks

        tag_tracks(tracks, membership)
    best = pick_duplicate_to_keep(tracks)
    if best is None or best.get("playlists"):
        return None
    ids = [t.get("track_id") or t.get("id") for t in tracks]
    sizes = _album_sizes(worker.db, [i for i in ids if i is not None])
    best_id = str(best.get("track_id") or best.get("id"))
    mine = sizes.get(best_id)
    if mine is None:
        return None
    others = [t for t in tracks if str(t.get("track_id") or t.get("id")) != best_id
              and str(t.get("track_id") or t.get("id")) in sizes]
    fuller = [t for t in others if sizes[str(t.get("track_id") or t.get("id"))]["size"] > mine["size"]
              and sizes[str(t.get("track_id") or t.get("id"))]["album_id"] != mine["album_id"]]
    if not fuller:
        return None
    home = max(fuller, key=lambda t: sizes[str(t.get("track_id") or t.get("id"))]["size"])
    download = worker._config_manager.get("soulseek.download_path", "") if worker._config_manager else None

    def resolve(track: Dict[str, Any]) -> str:
        return rw._resolve_file_path(track.get("file_path"), worker.transfer_folder, download,
                                     config_manager=worker._config_manager) or ""

    best_path, home_path = resolve(best), resolve(home)
    if not (best_path and home_path and os.path.isfile(best_path) and os.path.isfile(home_path)):
        return None
    if os.path.realpath(best_path) == os.path.realpath(home_path):
        return None
    if not (album_tagging.root_of(best_path) and album_tagging.root_of(home_path)):
        return None
    a, b = _length(best_path), _length(home_path)
    if a > 0 and b > 0 and abs(a - b) > _LENGTH_TOLERANCE:
        return None                                   # another recording
    return {"best": best, "home": home, "best_path": best_path, "home_path": home_path,
            "home_album": sizes[str(home.get("track_id") or home.get("id"))], "best_album": mine}


def place(worker: Any, found: Dict[str, Any]) -> str:
    """Put the best file where the fuller album's copy is. Returns its new path."""
    from core.fork import album_duplicates, album_identity, album_tagging, edition_merge, retro
    from core.tag_writer import write_tags_to_file

    best_path, home_path = found["best_path"], found["home_path"]
    current = album_tagging._read_tags(home_path)
    release = _read_frames(home_path)
    album_line = album_duplicates.read_release(home_path) or {}
    new_path = os.path.splitext(home_path)[0] + os.path.splitext(best_path)[1].lower()
    if os.path.exists(new_path) and os.path.realpath(new_path) != os.path.realpath(home_path):
        raise FileExistsError(f"{os.path.basename(new_path)} already exists in the album folder")

    moved, note = edition_merge._quarantine(worker, home_path, "duplicate_tracks")
    if not moved and os.path.exists(home_path):
        raise RuntimeError(note or "the copy in the album could not be moved away")
    retro._move_file(best_path, new_path)
    outcome = write_tags_to_file(new_path, {
        "title": current.get("title") or found["home"].get("title"),
        "album_title": album_line.get("album") or current.get("album") or found["home_album"]["album"],
        "track_number": current.get("track_number") or None,
        "disc_number": current.get("disc_number") or 1,
    }, embed_cover=False)
    if not outcome.get("success"):
        logger.warning("tags not written to %s: %s", new_path, outcome.get("error"))
    _copy_release(new_path, release)
    try:
        album_identity.harmonize(new_path)
    except Exception as exc:
        logger.debug("album ids not aligned for %s: %s", new_path, exc)
    try:
        worker._cleanup_empty_parents(best_path)
    except Exception as exc:
        logger.debug("could not tidy %s: %s", os.path.dirname(best_path), exc)

    home_id = found["home"].get("track_id") or found["home"].get("id")
    best_id = found["best"].get("track_id") or found["best"].get("id")
    conn = worker.db._get_connection()
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        carried = [c for c in ("bitrate", "duration") if c in columns]
        if carried:
            sets = ", ".join(f"{c} = (SELECT {c} FROM tracks WHERE id = ?)" for c in carried)
            conn.execute(f"UPDATE tracks SET {sets} WHERE id = ?", (*[best_id] * len(carried), home_id))
        conn.execute("UPDATE tracks SET file_path = ? WHERE id = ?", (new_path, home_id))
        conn.execute("DELETE FROM tracks WHERE id = ?", (best_id,))
        conn.commit()
    finally:
        conn.close()
    return new_path


def fix(worker: Any, entity_type: Any, entity_id: Any, file_path: Any, details: Any) -> Dict[str, Any]:
    details = details or {}
    try:
        found = plan(worker, details)
    except Exception as exc:
        logger.debug("duplicate placement not planned: %s", exc)
        found = None
    if found is None:
        return worker._fix_duplicates(entity_type, entity_id, file_path, details)
    try:
        new_path = place(worker, found)
    except Exception as exc:
        logger.warning("Duplicate placement failed for %s: %s", found["best_path"], exc)
        return {"success": False, "error": f"Could not put the better copy into the fuller album: {exc}"}
    message = (f'Kept the better copy ({os.path.splitext(new_path)[1].lstrip(".").upper()}) in '
               f'"{found["home_album"]["album"]}" ({found["home_album"]["size"]} tracks) instead of '
               f'"{found["best_album"]["album"]}" ({found["best_album"]["size"]})')
    best_id = str(found["best"].get("track_id") or found["best"].get("id"))
    home = {**found["home"], "file_path": new_path}
    home_id = str(home.get("track_id") or home.get("id"))
    rest = [t for t in details.get("tracks") or []
            if str(t.get("track_id") or t.get("id")) not in (best_id, home_id)]
    if rest:
        stock = worker._fix_duplicates(entity_type, entity_id, file_path,
                                       {**details, "tracks": [home, *rest], "_fix_action": home_id})
        message += f'; {stock.get("message") or stock.get("error") or "the other copies were handled"}'
    logger.info("Duplicate placement: %s", message)
    return {"success": True, "action": "duplicate_placed", "message": message, "fixed": 1 + len(rest)}


__all__ = ["fix", "place", "plan"]
