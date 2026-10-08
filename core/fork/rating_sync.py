"""Rating Tag Sync: Navidrome's star ratings and the rating tag in the files.

Navidrome keeps a rating (1–5 stars, per user) in its own database and never
writes it to the file, so it is lost with that database and no other player
sees it. This copies it one way or the other for every library track that
came from Navidrome (the track's id in the library is Navidrome's song id).

Where a rating lives in a file, and how it is written:

* ID3 (MP3…)            ``POPM`` frame, 0–255: 1, 64, 128, 196, 255 for 1–5 stars
* Vorbis (FLAC, Ogg…)   ``RATING``, 0–100: 20, 40, 60, 80, 100
* MP4 (M4A)             free-form ``RATING`` atom, 0–100

Reading is more forgiving, because players disagree: a Vorbis or MP4 value of
1–5 is taken as stars, a value with a decimal point up to 1 as a fraction
(``FMPS_RATING``, Picard's ``RATING:user``), anything larger as 0–100; a POPM
byte falls into the usual five bands. A file's existing rating tags of other
spellings are kept in step rather than left to disagree.

Ratings are those of the Navidrome account SoulSync is configured with.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from utils.logging_config import get_logger

logger = get_logger("fork.rating_sync")

NAVIDROME_TO_FILE = "navidrome_to_file"
FILE_TO_NAVIDROME = "file_to_navidrome"
DIRECTIONS = [NAVIDROME_TO_FILE, FILE_TO_NAVIDROME]

_POPM_BYTE = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
_POPM_EMAIL = "no@email"
_FREEFORM = "----:com.apple.iTunes:"
_PAGE = 500


# ── the tag ─────────────────────────────────────────────────────────────

def _stars_from_popm(value: Any) -> int:
    try:
        byte = int(value)
    except (TypeError, ValueError):
        return 0
    if byte <= 0:
        return 0
    return 1 if byte < 32 else 2 if byte < 96 else 3 if byte < 160 else 4 if byte < 224 else 5


def _stars_from_text(value: Any) -> int:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    text = str(value if value is not None else "").strip()
    try:
        number = float(text)
    except ValueError:
        return 0
    if number <= 0:
        return 0
    if number <= 1 and "." in text:
        stars = number * 5
    elif number <= 5:
        stars = number
    else:
        stars = number / 20
    return max(1, min(5, int(stars + 0.5)))


def _first(values: Any) -> Any:
    if isinstance(values, (list, tuple)):
        return values[0] if values else None
    return values


def _vorbis_keys(audio: Any) -> List[str]:
    keys = dict.fromkeys(str(key).lower() for key, _value in (audio.tags or []))
    return [k for k in keys if k in ("rating", "fmps_rating") or k.startswith("rating:")]


def _mp4_keys(audio: Any) -> List[str]:
    return [k for k in (audio.tags or {}) if k.startswith(_FREEFORM) and k[len(_FREEFORM):].upper() == "RATING"]


def _open(path: str) -> Any:
    from core.fork import editor

    return editor._open(path)


def read_rating(path: str) -> int:
    """The file's rating in stars, ``0`` when it has none."""
    audio, kind = _open(path)
    if audio.tags is None:
        return 0
    if kind == "id3":
        return next((s for s in (_stars_from_popm(f.rating) for f in audio.tags.getall("POPM")) if s), 0)
    if kind == "vorbis":
        keys = _vorbis_keys(audio)
        keys.sort(key=lambda k: k != "rating")
        return next((s for s in (_stars_from_text(_first(audio.tags.get(k))) for k in keys) if s), 0)
    if kind == "mp4":
        return next((s for s in (_stars_from_text(_first(audio.tags.get(k))) for k in _mp4_keys(audio)) if s), 0)
    raise ValueError("This file format cannot hold a rating")


def write_rating(path: str, stars: int) -> None:
    """Set the file's rating to ``stars`` (1–5); ``0`` removes it."""
    stars = max(0, min(5, int(stars)))
    audio, kind = _open(path)
    if kind not in ("id3", "vorbis", "mp4"):
        raise ValueError("This file format cannot hold a rating")
    if audio.tags is None:
        if not stars:
            return
        audio.add_tags()
    if kind == "id3":
        from mutagen import id3

        frames = audio.tags.getall("POPM")
        if not stars:
            audio.tags.delall("POPM")
        elif frames:
            for frame in frames:
                frame.rating = _POPM_BYTE[stars]
        else:
            audio.tags.add(id3.POPM(email=_POPM_EMAIL, rating=_POPM_BYTE[stars], count=0))
    elif kind == "vorbis":
        for key in _vorbis_keys(audio):
            if not stars:
                del audio[key]
            elif key != "rating":
                audio[key] = [str(stars / 5)]
        if stars:
            audio["rating"] = [str(stars * 20)]
    else:
        from mutagen.mp4 import MP4FreeForm

        keys = _mp4_keys(audio)
        for key in keys:
            del audio.tags[key]
        if stars:
            audio.tags[keys[0] if keys else _FREEFORM + "RATING"] = [MP4FreeForm(str(stars * 20).encode())]
    audio.save()


# ── Navidrome ───────────────────────────────────────────────────────────

def _client() -> Any:
    from core.navidrome_client import NavidromeClient

    return NavidromeClient()


def navidrome_ratings(client: Any, should_stop: Optional[Callable[[], bool]] = None) -> Optional[Dict[str, int]]:
    """``{song id: stars}`` for every rated song, or ``None`` when the listing
    could not be read to the end (an incomplete one would read as "unrated")."""
    ratings: Dict[str, int] = {}
    offset = 0
    while True:
        if should_stop and should_stop():
            return None
        reply = client._make_request("search3", {
            "query": '""', "artistCount": 0, "albumCount": 0, "songCount": _PAGE, "songOffset": offset})
        if reply is None:
            return None
        songs = (reply.get("searchResult3") or {}).get("song") or []
        if isinstance(songs, dict):
            songs = [songs]
        for song in songs:
            try:
                stars = int(song.get("userRating") or 0)
            except (TypeError, ValueError):
                stars = 0
            if stars > 0 and song.get("id"):
                ratings[str(song["id"])] = min(5, stars)
        if len(songs) < _PAGE:
            return ratings
        offset += len(songs)


def set_navidrome_rating(client: Any, song_id: str, stars: int) -> bool:
    return client._make_request("setRating", {"id": song_id, "rating": max(0, min(5, int(stars)))}) is not None


# ── the sync ────────────────────────────────────────────────────────────

def library_tracks(db: Any) -> List[Dict[str, Any]]:
    from core.fork import retro

    return retro._query(db, """
        SELECT t.id, t.title, ar.name AS artist, t.file_path
        FROM tracks t LEFT JOIN artists ar ON ar.id = t.artist_id
        WHERE t.server_source = 'navidrome' AND t.file_path IS NOT NULL AND t.file_path != ''
    """, ())


def _on_disk(file_path: str, context: Any) -> Optional[str]:
    from core.library.path_resolver import resolve_library_file_path

    resolved = resolve_library_file_path(file_path, transfer_folder=getattr(context, "transfer_folder", None),
                                         config_manager=getattr(context, "config_manager", None))
    return resolved or (file_path if os.path.isfile(file_path) else None)


def _stars(count: int) -> str:
    return "★" * count + "☆" * (5 - count) if count else "no rating"


def sync(context: Any, result: Any, direction: str = NAVIDROME_TO_FILE, clear_unrated: bool = False,
         dry_run: bool = False, client: Any = None) -> Any:
    """Copy ratings one way for every Navidrome track of the library, counting
    into ``result`` (a ``JobResult``). A track with no rating on the source
    side is left alone unless ``clear_unrated``."""
    to_file = direction != FILE_TO_NAVIDROME

    def say(line: str, kind: str = "info", **more: Any) -> None:
        if context.report_progress:
            context.report_progress(log_line=line, log_type=kind, **more)

    def stop(reason: str) -> Any:
        result.stopped_early = reason
        say(reason, "warning")
        return result

    client = client or _client()
    if not client.ensure_connection():
        return stop("Navidrome is not connected (Settings → media server)")
    tracks = library_tracks(context.db)
    total = len(tracks)
    if context.update_progress:
        context.update_progress(0, total)
    if context.report_progress:
        context.report_progress(phase="Reading ratings from Navidrome...", total=total)
    ratings = navidrome_ratings(client, context.check_stop)
    if context.check_stop():
        return result
    if ratings is None:
        return stop("Navidrome's ratings could not be read; nothing was changed")
    say(f"{len(ratings)} rated song(s) in Navidrome, {total} Navidrome track(s) in the library")
    if context.report_progress:
        context.report_progress(phase="Navidrome → files" if to_file else "Files → Navidrome", total=total)

    would = 0
    for index, track in enumerate(tracks, 1):
        if context.check_stop():
            return result
        if index % 10 == 0 and context.wait_if_paused():
            return result
        if context.update_progress and index % 25 == 0:
            context.update_progress(index, total)
        result.scanned += 1
        song_id = str(track["id"])
        in_navidrome = ratings.get(song_id, 0)
        if to_file and not in_navidrome and not clear_unrated:
            result.skipped += 1                 # nothing to copy: the file is not even opened
            continue
        path = _on_disk(track["file_path"], context)
        if not path:
            result.skipped += 1
            continue
        try:
            in_file = read_rating(path)
        except Exception as exc:
            logger.debug("rating of %s not read: %s", path, exc)
            result.skipped += 1
            continue
        source, target = (in_navidrome, in_file) if to_file else (in_file, in_navidrome)
        if source == target or (not source and not clear_unrated):
            result.skipped += 1
            continue
        name = f'{track.get("title") or os.path.basename(path)} — {track.get("artist") or "Unknown"}'
        change = f"{name}: {_stars(target)} → {_stars(source)} ({'file' if to_file else 'Navidrome'})"
        if dry_run:
            say(f"Would set {change}", scanned=index, total=total)
            would += 1
            continue
        try:
            if to_file:
                write_rating(path, source)
            elif not set_navidrome_rating(client, song_id, source):
                raise RuntimeError(getattr(client, "last_api_error", None) or "Navidrome refused the rating")
            result.auto_fixed += 1
            say(change, "success", scanned=index, total=total)
        except Exception as exc:
            result.errors += 1
            say(f"Failed — {name}: {exc}", "error", scanned=index, total=total)

    if context.update_progress:
        context.update_progress(total, total)
    say(f"Done — {would} rating(s) would change (dry run)" if dry_run
        else f"Done — {result.auto_fixed} rating(s) copied, {result.errors} failed", "success")
    return result


__all__ = ["DIRECTIONS", "FILE_TO_NAVIDROME", "NAVIDROME_TO_FILE", "library_tracks", "navidrome_ratings",
           "read_rating", "set_navidrome_rating", "sync", "write_rating"]
