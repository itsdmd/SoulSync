"""Singles and the albums they belong to.

Two things the Single/Album Dedup and Duplicate Detector tools need to know:

* **which releases keep more than one version of a song.** A single that
  ships the song next to its instrumental is a release of its own: the album
  holds only the main version, so neither copy of the song is redundant and
  nothing is merged, removed or reported as a duplicate. The same holds for
  any two versions of one song (main / instrumental / live / remix …): they
  are different recordings, never duplicates of each other.

* **which singles are just a track an album is missing.** When an album in
  the library lists a song it does not hold, and that song is in the library
  as a single (every track of the single is on the album's track list), the
  single can be merged into the album: retagged with the album's name and
  its place on the track list, moved into the album's folder and filed under
  the album in the library.
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.logging_config import get_logger

logger = get_logger("fork.single_merge")

FINDING_TYPE = "fork_single_into_album"

# Words that make a title a different RECORDING of the song. Masterings and
# packaging ("remastered", "deluxe", "explicit", "mono") are left out on
# purpose: a remaster next to the original is a duplicate worth reporting.
_VERSION_WORDS = (
    r"instrumental|inst\.?|karaoke|off[ -]?vocal|backing track|a\s?cappella|acapella|"
    r"live|acoustic|unplugged|stripped|orchestral|piano|demo|"
    r"remix(?:ed)?|mix|edit|extended|sped[ -]?up|slowed|reverb|nightcore|"
    r"tv[ -]?size|short|long|full|cover|ver\.?|version"
)
_VERSION_CJK = r"伴奏|純音樂|纯音乐|演奏版|無人聲|无人声|カラオケ|インスト|オフボーカル|ライブ|現場|现场|翻唱"
_SEGMENT_RE = re.compile(r"[\(\[（【［]([^\)\]）】］]*)[\)\]）】］]|\s[-–—~]\s(.+)$")
_WORD_RE = re.compile(rf"(?<![a-z])(?:{_VERSION_WORDS})(?![a-z])|{_VERSION_CJK}", re.IGNORECASE)
_PLAIN_RE = re.compile(r"^(?:album|original|single|main|standard|studio|full)(?:\s+(?:version|ver\.?|mix|edit))?$",
                       re.IGNORECASE)
_COVER_NAMES = re.compile(r"^(?:cover|folder|front|album|albumart.*|thumb)\.(?:jpe?g|png|webp)$", re.IGNORECASE)


def _fold(text: Any) -> str:
    from core.text.fold import fold_title

    return fold_title(str(text or ""), drop_brackets=False)


def version_of(title: Any) -> Tuple[str, str]:
    """``(song, version)``: the title without its version note, and that note
    folded ("" for the plain song). "Song (Instrumental)" -> ("song",
    "instrumental"); "Song - Live at Wembley" -> ("song", "live at wembley")."""
    text = str(title or "")
    notes: List[str] = []

    def take(match: "re.Match[str]") -> str:
        note = match.group(1) if match.group(1) is not None else match.group(2)
        if note and _PLAIN_RE.match(note.strip()):
            return " "  # "Album Version", "Original Mix": the song itself
        if note and _WORD_RE.search(note):
            notes.append(note)
            return " "
        return match.group(0)

    base = _SEGMENT_RE.sub(take, text)
    return _fold(base), " ".join(sorted(_fold(n) for n in notes))


def _same_song(a: str, b: str, threshold: float) -> bool:
    """Two folded song names (version notes already removed)."""
    from core.text.fold import folded_similarity

    if not a or not b:
        return False
    return a == b or folded_similarity(a, b) >= threshold


def _same_title(a: Any, b: Any, threshold: float) -> bool:
    """The same song in the same version, across spellings and translations."""
    song_a, version_a = version_of(a)
    song_b, version_b = version_of(b)
    if version_a != version_b:
        return False
    if _same_song(song_a, song_b, threshold):
        return True
    try:
        from core.fork import album_tagging

        return album_tagging._keys_score(album_tagging.name_keys(str(a or "")),
                                         album_tagging.name_keys(str(b or ""))) >= max(threshold, 0.95)
    except Exception as exc:
        logger.debug("name keys failed for %r / %r: %s", a, b, exc)
        return False


# ── releases that keep several versions ─────────────────────────────────

class ReleaseIndex:
    """Which track sits on which release, read once per scan."""

    def __init__(self, db: Any):
        self.album_of: Dict[str, str] = {}
        self.titles: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        conn = db._get_connection()
        try:
            rows = conn.execute("SELECT id, album_id, title FROM tracks WHERE title IS NOT NULL").fetchall()
        finally:
            conn.close()
        for track_id, album_id, title in rows:
            if album_id is None:
                continue
            self.album_of[str(track_id)] = str(album_id)
            self.titles[str(album_id)].append(version_of(title))

    def _keeps(self, release: str, other: str, song: str, version: str, threshold: float) -> bool:
        """``release`` holds another version of ``song`` that ``other`` lacks."""
        theirs = self.titles.get(other, [])
        for sibling, sibling_version in self.titles.get(release, []):
            if sibling_version == version or not _same_song(sibling, song, threshold):
                continue
            if not any(v == sibling_version and _same_song(s, sibling, threshold) for s, v in theirs):
                return True
        return False

    def keep_both(self, track_a: Dict[str, Any], track_b: Dict[str, Any], threshold: float = 0.85) -> bool:
        """Whether two look-alike tracks are both wanted: they are different
        versions of the song, or one sits on a release that carries a version
        the other release does not (a single with its instrumental, next to
        the album that only has the song)."""
        song_a, version_a = version_of(track_a.get("title"))
        song_b, version_b = version_of(track_b.get("title"))
        if version_a != version_b:
            return _same_song(song_a, song_b, threshold)
        album_a = self.album_of.get(str(track_a.get("id")))
        album_b = self.album_of.get(str(track_b.get("id")))
        if not album_a or not album_b or album_a == album_b:
            return False
        return (self._keeps(album_a, album_b, song_a, version_a, threshold)
                or self._keeps(album_b, album_a, song_b, version_b, threshold))


# ── singles an album is missing ─────────────────────────────────────────

def _columns(conn: Any, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _col(columns: set, alias: str, name: str) -> str:
    return f"{alias}.{name}" if name in columns else "NULL"


_ALBUM_ID_COLUMNS = (("deezer", "deezer_id"), ("spotify", "spotify_album_id"), ("itunes", "itunes_album_id"),
                     ("discogs", "discogs_id"), ("musicbrainz", "musicbrainz_release_id"))


def _on_disk(album: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the tracks of ``album`` whose file is gone (rows left behind by
    files that have moved on) and give the rest their real path. Once."""
    from core.fork import album_tagging

    if not album.get("checked"):
        kept = []
        for track in album["tracks"]:
            path = album_tagging._resolve(track["path"])
            if path and os.path.isfile(path):
                kept.append({**track, "path": path})
        album["tracks"], album["checked"] = kept, True
    return album


def _load(db: Any, only: Optional[List[Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Albums with their tracks as the database lists them: ``{album id:
    album}``. Whether the files exist is checked later, by :func:`_on_disk`,
    for the few albums that matter."""
    where, params = "", ()
    if only is not None:
        where = f" AND al.id IN ({', '.join('?' for _ in only)})"
        params = tuple(only)
    conn = db._get_connection()
    try:
        tc, ac = _columns(conn, "tracks"), _columns(conn, "albums")
        ids = ", ".join(f"{_col(ac, 'al', column)}" for _source, column in _ALBUM_ID_COLUMNS)
        rows = conn.execute(f"""
            SELECT t.id, t.title, t.file_path, t.track_number, {_col(tc, 't', 'disc_number')},
                   t.duration, al.id, al.title, {_col(ac, 'al', 'record_type')},
                   {_col(ac, 'al', 'api_track_count')}, {_col(ac, 'al', 'track_count')},
                   {_col(ac, 'al', 'year')}, {_col(ac, 'al', 'release_date')}, {_col(ac, 'al', 'thumb_url')},
                   {_col(ac, 'al', 'canonical_source')}, {_col(ac, 'al', 'canonical_album_id')},
                   ar.id, ar.name, {ids}
            FROM tracks t
            JOIN albums al ON al.id = t.album_id
            LEFT JOIN artists ar ON ar.id = al.artist_id
            WHERE t.title IS NOT NULL AND t.title != ''
              AND t.file_path IS NOT NULL AND t.file_path != ''
        """ + where, params).fetchall()
    finally:
        conn.close()
    albums: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        (track_id, title, file_path, number, disc, duration, album_id, album_title, record_type, api_count,
         track_count, year, release_date, thumb, canonical_source, canonical_id, artist_id, artist_name) = row[:18]
        album = albums.setdefault(str(album_id), {
            "id": album_id, "title": album_title or "", "record_type": str(record_type or "").lower(),
            "expected": int(api_count or track_count or 0), "year": year, "release_date": release_date,
            "thumb_url": thumb, "artist_id": artist_id, "artist": artist_name or "", "tracks": [],
            "sources": ([(str(canonical_source).lower(), str(canonical_id))]
                        if canonical_source and canonical_id else [])
            + [(source, str(value)) for (source, _c), value in zip(_ALBUM_ID_COLUMNS, row[18:], strict=True) if value],
        })
        album["tracks"].append({"id": track_id, "title": title, "path": file_path, "number": number,
                                "disc": disc or 1, "duration": duration})
    return albums


def _is_single(album: Dict[str, Any]) -> bool:
    if album["record_type"] == "single":
        return True
    if album["record_type"] in ("album", "ep", "compile", "compilation"):
        return False
    # a release of unknown type is a single only when its size is known: one
    # owned track of a compilation is not a single
    return 0 < album["expected"] <= 2 and len(album["tracks"]) <= 2


def track_list(album: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The release's official track list from its metadata source (cached by
    the metadata cache), or [] when no source can give it."""
    from core.metadata.album_tracks import get_album_tracks_for_source

    try:
        from core.metadata.registry import get_primary_source

        primary = str(get_primary_source() or "").lower()
    except Exception:
        primary = ""
    # the pinned release first (it leads the list), then the primary source's id
    pinned, rest = album["sources"][:1], album["sources"][1:]
    sources = pinned + sorted(rest, key=lambda s: s[0] != primary)
    seen = set()
    for source, album_id in sources:
        if (source, album_id) in seen:
            continue
        seen.add((source, album_id))
        try:
            data = get_album_tracks_for_source(source, album_id)
        except Exception as exc:
            logger.debug("track list of %s:%s not fetched: %s", source, album_id, exc)
            continue
        items = data.get("items") if isinstance(data, dict) else data
        out = []
        for position, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("title") or "").strip()
            if not name:
                continue
            duration = item.get("duration_ms") or 0
            out.append({"title": name, "number": int(item.get("track_number") or position + 1),
                        "disc": int(item.get("disc_number") or 1),
                        "duration": float(duration) / 1000.0 if duration else 0.0})
        if out:
            return out
    return []


def _durations_agree(a: Any, b: Any, tolerance: float) -> bool:
    """False only when both lengths are known and clearly differ (a radio
    edit against the album cut). Library durations may be in ms or seconds."""
    try:
        first, second = float(a or 0), float(b or 0)
    except (TypeError, ValueError):
        return True
    if first > 20000:
        first /= 1000.0
    if second > 20000:
        second /= 1000.0
    if first <= 0 or second <= 0:
        return True
    return abs(first - second) <= tolerance


def _placement(single: Dict[str, Any], album: Dict[str, Any], listing: List[Dict[str, Any]],
               threshold: float, tolerance: float) -> Optional[List[Dict[str, Any]]]:
    """Where each track of ``single`` goes on ``album``, or None unless EVERY
    one of them is a listed track the album does not hold yet."""
    missing = [entry for entry in listing
               if not any(_same_title(t["title"], entry["title"], threshold) for t in album["tracks"])]
    placed, used = [], set()
    for track in single["tracks"]:
        slot = next((i for i, entry in enumerate(missing)
                     if i not in used and _same_title(track["title"], entry["title"], threshold)
                     and _durations_agree(track["duration"], entry["duration"], tolerance)), None)
        if slot is None:
            return None
        used.add(slot)
        placed.append({"track_id": track["id"], "title": track["title"], "file_path": track["path"],
                       "listed_as": missing[slot]["title"], "track_number": missing[slot]["number"],
                       "disc_number": missing[slot]["disc"]})
    return placed or None


def find_merges(db: Any, threshold: float = 0.85, tolerance: float = 10.0,
                check_stop: Optional[Callable[[], bool]] = None,
                progress: Optional[Callable[[int, int, str], None]] = None) -> List[Dict[str, Any]]:
    """One entry per single that an incomplete album of the same artist is
    missing: finding details ready for :func:`merge`."""
    albums = _load(db)
    by_artist: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: {"singles": [], "albums": []})
    for album in albums.values():
        key = _fold(album["artist"])
        if not key:
            continue
        if _is_single(album):
            by_artist[key]["singles"].append(album)
        elif album["sources"] and (not album["expected"] or len(album["tracks"]) < album["expected"]):
            by_artist[key]["albums"].append(album)
    work = []
    for group in by_artist.values():
        if not group["singles"] or not group["albums"]:
            continue
        # only now look at the disk, and only for these artists
        singles = [a for a in map(_on_disk, group["singles"]) if a["tracks"]]
        targets = [a for a in map(_on_disk, group["albums"])
                   if a["tracks"] and (not a["expected"] or len(a["tracks"]) < a["expected"])]
        if singles and targets:
            work.append({"singles": singles, "albums": targets})
    total = sum(len(group["albums"]) for group in work)
    out: List[Dict[str, Any]] = []
    done = 0
    for group in work:
        # the album the user holds most of comes first when two list the song
        targets = sorted(group["albums"], key=lambda a: (a["record_type"] != "album", -len(a["tracks"])))
        listings: Dict[str, List[Dict[str, Any]]] = {}
        for album in targets:
            if check_stop and check_stop():
                return out
            done += 1
            if progress:
                progress(done, total, f'{album["artist"]} — {album["title"]}')
            listings[str(album["id"])] = track_list(album)
        for single in group["singles"]:
            for album in targets:
                placed = _placement(single, album, listings.get(str(album["id"])) or [], threshold, tolerance)
                if not placed:
                    continue
                out.append({
                    "single": {"album_id": single["id"], "title": single["title"]},
                    "album": {"album_id": album["id"], "title": album["title"], "artist": album["artist"],
                              "owned_tracks": len(album["tracks"]),
                              "expected_tracks": len(listings[str(album["id"])])},
                    "tracks": placed,
                    "artist": album["artist"],
                    "artist_id": album["artist_id"],
                    "album_thumb_url": album["thumb_url"] or single["thumb_url"],
                    "title_similarity": threshold,
                })
                # those slots are spoken for: a second copy must not land on them
                for item in placed:
                    album["tracks"].append({"id": item["track_id"], "title": item["listed_as"],
                                            "path": item["file_path"], "number": item["track_number"],
                                            "disc": item["disc_number"], "duration": 0})
                break
    return out


def finding_text(details: Dict[str, Any]) -> Tuple[str, str]:
    single, album, tracks = details["single"], details["album"], details["tracks"]
    title = f'Merge single into album: "{single["title"]}" → "{album["title"]}" by {details.get("artist") or ""}'
    places = ", ".join(f'"{t["title"]}" as track {t["track_number"]}' for t in tracks[:4])
    return title, (f'"{album["title"]}" lists {"this song" if len(tracks) == 1 else "these songs"} but holds '
                   f'{album["owned_tracks"]} of {album["expected_tracks"]} tracks, and '
                   f'{"it is" if len(tracks) == 1 else "they are"} in the library as the single '
                   f'"{single["title"]}". Merging retags and moves: {places}.')


# ── the merge ───────────────────────────────────────────────────────────

def _target_dir(album: Dict[str, Any], disc: int) -> str:
    same_disc = [os.path.dirname(t["path"]) for t in album["tracks"] if int(t["disc"] or 1) == disc]
    folders = same_disc or [os.path.dirname(t["path"]) for t in album["tracks"]]
    folder = Counter(folders).most_common(1)[0][0]
    if not same_disc and re.match(r"^(?:disc|disk|cd)\s*\d+$", os.path.basename(folder), re.I):
        # the album is split into disc folders and this disc has none yet
        folder = os.path.join(os.path.dirname(folder), re.sub(r"\d+", str(disc), os.path.basename(folder)))
    return folder


def _file_name(path: str, album: Dict[str, Any], item: Dict[str, Any], total_discs: int) -> str:
    """The file's name under the path template; its own name when that fails."""
    from core.fork import album_tagging

    ext = os.path.splitext(path)[1]
    try:
        current = album_tagging._read_tags(path)
        values = {"title": current.get("title") or item["title"], "artist": current.get("artist") or album["artist"],
                  "albumartist": album["artist"], "album": album["title"], "year": str(album.get("year") or ""),
                  "track_number": item["track_number"], "disc_number": item["disc_number"]}
        templated = album_tagging._template_path("", values, {"album_type": album["record_type"] or "album"},
                                                 max(album["expected"], len(album["tracks"]) + 1), total_discs, ext)
        if templated:
            return os.path.basename(templated)
    except Exception as exc:
        logger.debug("template name not built for %s: %s", path, exc)
    return os.path.basename(path)


def _tidy(folder: str, root: str) -> None:
    """Remove a single's folder once its audio is gone: only cover images go
    with it; anything else keeps the folder."""
    from core.fork import album_tagging

    if not os.path.isdir(folder) or os.path.normpath(folder) == os.path.normpath(root):
        return
    names = os.listdir(folder)
    exts = album_tagging._audio_exts()
    if any(os.path.splitext(n)[1].lower() in exts or not _COVER_NAMES.match(n) for n in names):
        return
    for name in names:
        os.remove(os.path.join(folder, name))
    current = folder
    while os.path.isdir(current) and os.path.normpath(current) != os.path.normpath(root):
        try:
            os.rmdir(current)
        except OSError:
            break
        current = os.path.dirname(current)


def merge(db: Any, details: Dict[str, Any]) -> Dict[str, Any]:
    """Move a single's tracks into the album that lists them. Everything is
    checked again against the library as it is now."""
    from core.fork import album_identity, album_tagging, retro
    from core.tag_writer import write_tags_to_file

    items = [t for t in (details.get("tracks") or []) if isinstance(t, dict)]
    wanted = [(details.get(key) or {}).get("album_id") for key in ("album", "single")]
    albums = {key: _on_disk(value) for key, value in _load(db, [w for w in wanted if w is not None]).items()}
    album, single = albums.get(str(wanted[0])), albums.get(str(wanted[1]))
    if not items or album is None or not album["tracks"]:
        return {"success": False, "error": "The album has no files in the library anymore"}
    if single is None or not single["tracks"]:
        return {"success": True, "action": "already_gone", "message": "The single is no longer in the library"}
    threshold = float(details.get("title_similarity") or 0.85)
    by_id = {str(t["id"]): t for t in single["tracks"]}
    todo = []
    for item in items:
        track = by_id.get(str(item.get("track_id")))
        if track is None:
            continue
        if any(_same_title(t["title"], item.get("listed_as") or track["title"], threshold) for t in album["tracks"]):
            return {"success": False, "error": f'"{album["title"]}" already holds "{track["title"]}" — '
                                               "run Single/Album Dedup again"}
        if not album_tagging.root_of(track["path"]):
            return {"success": False, "error": f'{track["path"]} is outside the library folders'}
        todo.append((track, item))
    if len(todo) != len(single["tracks"]):
        return {"success": False, "error": "The single has changed since the scan — run Single/Album Dedup again"}

    total_discs = max([int(t["disc"] or 1) for t in album["tracks"]] + [int(i["disc_number"] or 1) for _t, i in todo])
    cover = album["thumb_url"] if str(album.get("thumb_url") or "").startswith(("http://", "https://")) else None
    moved, errors, old_dirs = 0, [], set()
    for track, item in todo:
        path, disc = track["path"], int(item.get("disc_number") or 1)
        target = os.path.join(_target_dir(album, disc), _file_name(path, album, item, total_discs))
        if os.path.exists(target) and os.path.normpath(target) != os.path.normpath(path):
            errors.append(f"{os.path.basename(target)} already exists in the album folder")
            continue
        outcome = write_tags_to_file(path, {
            "album_title": album["title"], "track_number": int(item["track_number"]), "disc_number": disc,
            "track_count": album["expected"] or None, "release_date": album.get("release_date"),
            "year": album.get("year"),
        }, embed_cover=bool(cover), cover_url=cover)
        if not outcome.get("success"):
            errors.append(f"{os.path.basename(path)}: {outcome.get('error') or 'could not write tags'}")
            continue
        try:
            if os.path.normpath(target) != os.path.normpath(path):
                retro._move_file(path, target)
                old_dirs.add(os.path.dirname(path))
        except Exception as exc:
            errors.append(f"{os.path.basename(path)}: move failed: {exc}")
            continue
        try:
            album_identity.harmonize(target)
        except Exception as exc:
            logger.debug("album ids not aligned for %s: %s", target, exc)
        conn = db._get_connection()
        try:
            columns = _columns(conn, "tracks")
            sets, values = ["album_id = ?", "track_number = ?", "file_path = ?"], [album["id"], int(item["track_number"]), target]
            if "disc_number" in columns:
                sets.append("disc_number = ?")
                values.append(disc)
            if album.get("artist_id") is not None:
                sets.append("artist_id = ?")
                values.append(album["artist_id"])
            conn.execute(f"UPDATE tracks SET {', '.join(sets)} WHERE id = ?", (*values, track["id"]))
            conn.commit()
        finally:
            conn.close()
        album["tracks"].append({**track, "path": target, "disc": disc, "number": item["track_number"]})
        moved += 1
    if not moved:
        return {"success": False, "error": "; ".join(errors[:3]) or "Nothing could be merged"}

    conn = db._get_connection()
    try:
        # the single is gone as a release once none of its tracks are left
        conn.execute("DELETE FROM albums WHERE id = ? AND NOT EXISTS (SELECT 1 FROM tracks WHERE album_id = ?)",
                     (single["id"], single["id"]))
        conn.commit()
    finally:
        conn.close()
    for folder in old_dirs:
        try:
            _tidy(folder, album_tagging.root_of(folder) or folder)
        except Exception as exc:
            logger.debug("could not tidy %s: %s", folder, exc)
    try:
        # discography badges were worked out with the single as its own release
        from core.fork import store

        store.clear_album_checks()
    except Exception as exc:
        logger.debug("album checks not cleared: %s", exc)
    message = f'Merged "{single["title"]}" into "{album["title"]}" ({moved} track(s))'
    if errors:
        message += f" — {len(errors)} problem(s): {errors[0]}"
    logger.info("Single merge: %s", message)
    return {"success": True, "action": "single_merged", "message": message, "fixed": moved}


__all__ = ["FINDING_TYPE", "ReleaseIndex", "find_merges", "finding_text", "merge", "track_list", "version_of"]
