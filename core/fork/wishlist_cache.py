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
        held = _get(key, fp)
        if held is None:
            built = index_for(db, profile)
            if built is not None:
                held = dict(built.counts)
                _put(key, fp, held)
        _local.counts = held
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


# ---- paged view: one request = a window of artists with their tracks ------------------
#
# The slim list above still ships every track. The wishlist page instead asks for
# artists a page at a time: this index is built from one SQL pass over the table
# (the JSON fields are read inside SQLite, no Python parsing or row formatting) and
# held per fingerprint; a page is a slice of it.

_FAILING_ATTEMPTS = 3          # webui WL_FAILING_ATTEMPTS: a track this deep in retries is "failing"

_INDEX_SQL = """
SELECT id, spotify_track_id, retry_count, last_attempted, failure_reason,
       json_extract(spotify_data, '$.name'),
       COALESCE(json_extract(spotify_data, '$.artists[0].name'),
                CASE WHEN json_type(spotify_data, '$.artists[0]') = 'text'
                     THEN json_extract(spotify_data, '$.artists[0]') END),
       CASE json_type(spotify_data, '$.album')
            WHEN 'object' THEN json_extract(spotify_data, '$.album.name')
            WHEN 'text' THEN json_extract(spotify_data, '$.album') END,
       CASE json_type(spotify_data, '$.album')
            WHEN 'object' THEN json_extract(spotify_data, '$.album.album_type') END,
       CASE json_type(spotify_data, '$.album')
            WHEN 'object' THEN json_extract(spotify_data, '$.album.total_tracks') END,
       CASE json_type(spotify_data, '$.album')
            WHEN 'object' THEN json_extract(spotify_data, '$.album.images[0].url') END
FROM wishlist_tracks
WHERE profile_id = ? AND json_valid(spotify_data)
ORDER BY date_added
"""


class _Artist:
    __slots__ = ("name", "albums", "singles", "failing", "album_names", "key")

    def __init__(self, name: str):
        self.name = name
        self.key = name.lower()
        self.albums: list = []        # track tuples of the "albums" category
        self.singles: list = []
        self.failing = 0
        self.album_names: set = set()

    @property
    def total(self) -> int:
        return len(self.albums) + len(self.singles)


class _Index:
    def __init__(self):
        self.artists: Dict[str, _Artist] = {}
        self.counts = {"singles": 0, "albums": 0, "total": 0}
        self.failing_ids: list = []
        self._orders: Dict[str, list] = {}

    def order(self, sort: str) -> list:
        cached = self._orders.get(sort)
        if cached is None:
            artists = list(self.artists.values())          # first appearance order
            if sort == "name":
                artists.sort(key=lambda a: a.key)
            elif sort == "failing":
                artists.sort(key=lambda a: (-a.failing, -a.total, a.key))
            else:
                artists.sort(key=lambda a: -a.total)
            cached = self._orders[sort] = artists
        return cached


def _category(album_type: Any, total_tracks: Any) -> str:
    from core.wishlist.classification import _coerce_positive_int

    kind = album_type.lower() if isinstance(album_type, str) else ""
    if kind in ("single", "ep"):
        return "singles"
    if kind in ("album", "compilation"):
        return "albums"
    count = _coerce_positive_int(total_tracks)
    if count is not None and count > 0:
        return "singles" if count < 6 else "albums"
    return "albums"


def _build_index(db: Any, profile_id: Any) -> _Index:
    index = _Index()
    seen: set = set()
    with db._get_connection() as conn:
        rows = conn.execute(_INDEX_SQL, (profile_id,)).fetchall()
    for (row_id, sid, retry, last, reason, title, artist, album, album_type, total, image) in rows:
        track_id = sid or str(row_id)
        if track_id in seen:
            continue
        seen.add(track_id)
        name = artist if isinstance(artist, str) and artist else "Unknown Artist"
        entry = index.artists.get(name)
        if entry is None:
            entry = index.artists[name] = _Artist(name)
        category = _category(album_type, total)
        try:
            retries = int(retry or 0)
        except (TypeError, ValueError):
            retries = 0
        item = (track_id, sid, retries, last, reason, title, album, album_type, image)
        # every release name is searchable here (a single's too: the page groups by
        # album as well); the page narrows the loaded groups again with its own rule
        entry.album_names.add((album if isinstance(album, str) and album else "Unknown").lower())
        if category == "singles":
            entry.singles.append(item)
        else:
            entry.albums.append(item)
        index.counts[category] += 1
        if retries >= _FAILING_ATTEMPTS:
            entry.failing += 1
            index.failing_ids.append(track_id)
    index.counts["total"] = index.counts["albums"] + index.counts["singles"]
    return index


def index_for(db: Any, profile_id: Any) -> Optional[_Index]:
    """The artist index for the table as it is now; ``None`` when it cannot be built."""
    key = ("index", profile_id)
    fp = fingerprint(db, profile_id)
    hit = _get(key, fp)
    if hit is not None:
        return hit
    with _flight(key):
        fp = fingerprint(db, profile_id)
        hit = _get(key, fp)
        if hit is not None:
            return hit
        try:
            built = _build_index(db, profile_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("wishlist index not built: %s", exc)
            return None
        _put(key, fp, built)
        return built


def _row_for_page(item: tuple, artist: str) -> Dict[str, Any]:
    track_id, sid, retries, last, reason, title, album, album_type, image = item
    return {
        "id": track_id,
        "spotify_track_id": sid,
        "retry_count": retries,
        "last_attempted": last,
        "failure_reason": reason,
        "spotify_data": {
            "name": title,
            "artists": [{"name": artist}],
            "album": {"name": album, "album_type": album_type,
                      "images": [{"url": image}] if image else []},
        },
    }


def page(runtime, *, offset: int = 0, limit: int = 48, q: str = "", failing: bool = False,
         sort: str = "busiest") -> Dict[str, Any]:
    """One window of artists (filtered and ordered like the page does) with their tracks."""
    db = runtime.get_music_database()
    profile = runtime.profile_id
    if index_for(db, profile) is None:
        return {"error": "wishlist index unavailable"}
    # as the full track list always did: clear duplicate rows, unless a batch is working the list
    try:
        with runtime.tasks_lock:
            busy = any(b.get("playlist_id") == "wishlist" and b.get("phase") in ("analysis", "downloading")
                       for b in runtime.download_batches.values())
        if not busy and db.remove_wishlist_duplicates(profile_id=profile):
            _entries.pop(("index", profile), None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("duplicate cleanup skipped: %s", exc)
    index = index_for(db, profile)
    if index is None:
        return {"error": "wishlist index unavailable"}

    needle = (q or "").lower().strip()
    matched = index.order(sort if sort in ("busiest", "failing", "name") else "busiest")
    if needle or failing:
        def keep(artist: _Artist) -> bool:
            if failing and artist.failing == 0:
                return False
            if not needle:
                return True
            return needle in artist.key or any(needle in name for name in artist.album_names)
        matched = [a for a in matched if keep(a)]

    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 200))
    window = matched[offset:offset + limit]
    albums: list = []
    singles: list = []
    for artist in window:
        albums.extend(_row_for_page(i, artist.name) for i in artist.albums)
        singles.extend(_row_for_page(i, artist.name) for i in artist.singles)

    images: Dict[str, str] = {}
    try:
        from core.wishlist.routes import _enrich_wishlist_images

        wrapped = [{"artist_name": row["spotify_data"]["artists"][0]["name"], "spotify_data": row["spotify_data"]}
                   for row in albums + singles]
        images = _enrich_wishlist_images(wrapped, db)
    except Exception as exc:  # noqa: BLE001 - art is cosmetic
        logger.debug("wishlist page art not resolved: %s", exc)

    return {
        "success": True,
        "offset": offset,
        "limit": limit,
        "artists": [a.name for a in window],
        "matched_artists": len(matched),
        "has_more": offset + limit < len(matched),
        "albums": albums,
        "singles": singles,
        "artist_images": images,
        "counts": dict(index.counts),
        "failing_total": len(index.failing_ids),
    }


def failing_track_ids(runtime) -> list:
    index = index_for(runtime.get_music_database(), runtime.profile_id)
    return list(index.failing_ids) if index else []
