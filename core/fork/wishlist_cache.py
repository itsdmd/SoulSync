"""Keeps the wishlist endpoints fast when the list holds tens of thousands of tracks.

Upstream rebuilt everything on every request: a duplicate scan that parses every
row, a load that formats every row, then the category filter and art lookup, and
the page asks for the stats and both categories at once. With 60k+ rows each of
those took tens of seconds and they ran on top of each other.

What is kept here, all keyed by a cheap fingerprint of the table (one aggregate
query, no JSON parsing) so any change to the wishlist invalidates it:

* the three counts of the stats endpoint,
* the slim track list the web UI asks for (``?slim=1``): just the fields the page
  reads (about a fifth of the bytes of the full rows),
* the duplicate scan: skipped while the table is unchanged.

Concurrent identical requests share one computation instead of each doing it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from utils.logging_config import get_logger

logger = get_logger("fork.wishlist_cache")

_TTL = 600.0        # a safety net: edits that leave the fingerprint alone show up this late

_local = threading.local()
_mutex = threading.Lock()
_entries: Dict[Any, tuple] = {}     # key -> (fingerprint, stored_at, value)
_flights: Dict[Any, threading.Lock] = {}


def _enabled() -> bool:
    from core.fork import hooks

    return hooks._active()


def fingerprint(db: Any, profile_id: Any) -> Optional[tuple]:
    """One cheap aggregate over the profile's rows; ``None`` when it cannot be read."""
    try:
        with db._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(id), 0), COALESCE(MAX(id), 0),"
                " COALESCE(SUM(retry_count), 0), COALESCE(MAX(last_attempted), ''),"
                " COALESCE(MAX(date_added), ''), COALESCE(SUM(quality_profile_id), 0)"
                " FROM wishlist_tracks WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        return tuple(row)
    except Exception as exc:  # noqa: BLE001 - no fingerprint just means no caching
        logger.debug("wishlist fingerprint not read: %s", exc)
        return None


def _get(key: Any, fp: Optional[tuple]) -> Any:
    if fp is None:
        return None
    with _mutex:
        entry = _entries.get(key)
    if entry and entry[0] == fp and time.monotonic() - entry[1] < _TTL:
        return entry[2]
    return None


def _put(key: Any, fp: Optional[tuple], value: Any) -> None:
    if fp is None:
        return
    with _mutex:
        _entries[key] = (fp, time.monotonic(), value)


def _flight(key: Any) -> threading.Lock:
    with _mutex:
        return _flights.setdefault(key, threading.Lock())


def forget() -> None:
    """Drop everything (tests, and a hook for code that rewrites rows in bulk)."""
    with _mutex:
        _entries.clear()


# ---- slim rows ------------------------------------------------------------------------

def slim_track(track: Dict[str, Any]) -> Dict[str, Any]:
    """The fields the wishlist page reads, nothing else."""
    sd = track.get("spotify_data")
    sd = sd if isinstance(sd, dict) else {}
    album = sd.get("album")
    if isinstance(album, dict):
        images = album.get("images")
        first = images[0] if isinstance(images, list) and images else None
        url = first.get("url") if isinstance(first, dict) else None
        album = {
            "name": album.get("name"),
            "album_type": album.get("album_type"),
            "images": [{"url": url}] if url else [],
        }
    artists = sd.get("artists")
    if isinstance(artists, list) and artists:
        first_artist = artists[0]
        artists = [{"name": first_artist.get("name")}] if isinstance(first_artist, dict) else [first_artist]
    else:
        artists = []
    return {
        "id": track.get("id"),
        "wishlist_id": track.get("wishlist_id"),
        "spotify_track_id": track.get("spotify_track_id"),
        "retry_count": track.get("retry_count"),
        "last_attempted": track.get("last_attempted"),
        "failure_reason": track.get("failure_reason"),
        "spotify_data": {"name": sd.get("name"), "album": album, "artists": artists},
    }


def _slim_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    out["tracks"] = [slim_track(t) for t in payload.get("tracks") or []]
    out["slim"] = True
    return out


def _wants_slim() -> bool:
    try:
        from flask import has_request_context, request

        return bool(has_request_context() and request.args.get("slim") in ("1", "true"))
    except Exception:  # noqa: BLE001
        return False


# ---- /api/wishlist/tracks -------------------------------------------------------------

def tracks(upstream, runtime, *, category=None, limit=None):
    if not _enabled() or not _wants_slim():
        return upstream(runtime, category=category, limit=limit)
    try:
        db = runtime.get_music_database()
        profile = runtime.profile_id
    except Exception:  # noqa: BLE001
        return upstream(runtime, category=category, limit=limit)
    key = ("tracks", profile, category, limit)
    fp = fingerprint(db, profile)
    hit = _get(key, fp)
    if hit is not None:
        return hit, 200
    with _flight(key):
        fp = fingerprint(db, profile)
        hit = _get(key, fp)
        if hit is not None:
            return hit, 200
        payload, status = upstream(runtime, category=category, limit=limit)
        if status != 200 or not isinstance(payload, dict):
            return payload, status
        slim = _slim_payload(payload)
        # the upstream call may itself have removed duplicates: file the result
        # under the fingerprint of what is in the table now
        _put(key, fingerprint(db, profile), slim)
        return slim, status


# ---- /api/wishlist/stats --------------------------------------------------------------

def stats(upstream, runtime):
    if not _enabled():
        return upstream(runtime)
    try:
        db = runtime.get_music_database()
        profile = runtime.profile_id
    except Exception:  # noqa: BLE001
        return upstream(runtime)
    key = ("counts", profile)
    with _flight(key):
        fp = fingerprint(db, profile)
        _local.counts = _get(key, fp)
        _local.store = (key, fp)
        try:
            return upstream(runtime)
        finally:
            _local.counts = None
            _local.store = None


def skip_load() -> bool:
    """True while a stats request already holds its counts: the load can be skipped."""
    return getattr(_local, "counts", None) is not None


def counts(upstream, raw_tracks):
    """Wrap ``count_wishlist_tracks_by_category`` for the stats request on this thread."""
    held = getattr(_local, "counts", None)
    if held is not None:
        return dict(held)
    result = upstream(raw_tracks)
    store = getattr(_local, "store", None)
    if store:
        _put(store[0], store[1], dict(result))
    return result


# ---- duplicate scan -------------------------------------------------------------------

def dedupe(upstream, db, profile_id=1):
    """Skip the duplicate scan while the table is as it was after the last one."""
    if not _enabled():
        return upstream(db, profile_id=profile_id)
    try:
        from core.settings import config_manager

        allow = bool(config_manager.get("wishlist.allow_duplicate_tracks", True))
    except Exception:  # noqa: BLE001
        allow = True
    key = ("dedupe", profile_id, allow)
    fp = fingerprint(db, profile_id)
    if _get(key, fp) is not None:
        return 0
    removed = upstream(db, profile_id=profile_id)
    _put(key, fingerprint(db, profile_id), True)
    return removed
