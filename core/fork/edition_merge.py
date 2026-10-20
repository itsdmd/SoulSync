"""Album editions: one album where the library holds several versions of it.

"25", "25 (Deluxe Edition)" and "25 (Japan Edition)" by the same artist are
one album packaged differently. This finds such sets and decides, per set:

* every version holds the same songs (only the name differs) → keep the
  "normal" one (the name without an edition note) and merge the rest into it;
* one version holds every song of another → merge the smaller into it (the
  deluxe edition keeps its bonus tracks, the standard one goes);
* versions that each hold songs the other lacks stay separate; the finding
  proposes filling each of them up (library copies first, else the wishlist).

A set can have any number of versions; each smaller one is merged into a
version that holds all of its songs.

Only *editions* are grouped. A name note made only of packaging words
("Deluxe", "Limited Edition", "2011 Remaster", "初回限定盤"…) is ignored when
comparing names; any other word ("Live at Wembley", "Acoustic", "Remixes",
"Taylor's Version") keeps the album apart. The tracks must agree too: a song
counts as the same in two versions only when its title and its length match
(within a few seconds), or, when the titles differ, its length and file size
both match closely. Two versions are a set only when at least half of the
smaller one's songs match and hardly any same-titled song differs in length
(a different length means a different recording).

Merging keeps one copy of each song: the better one of the two (more data
per second of audio), filed under the kept album with its name and track
number. The copy that goes is moved to the deleted-files folder, where it can
be restored.
"""

from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.fork import single_merge
from utils.logging_config import get_logger

logger = get_logger("fork.edition_merge")

MERGE_TYPE = "fork_album_editions"
GAPS_TYPE = "fork_edition_gaps"

# Words that only describe a package. A note made of nothing else is an edition.
_EDITION_WORDS = frozenset("""
deluxe super special limited collector collectors expanded anniversary remaster remastered remasters
edition editions ed version ver vers bonus track tracks standard international japan japanese uk us usa eu
european explicit clean digital reissue reissued platinum gold tour complete definitive ultimate legacy
hd hires hi res 24bit 24 bit 96khz 192khz mqa original album lp the and with plus a an of
extended exclusive edit edited new release re issue issued mastered master
初回限定盤 初回盤 限定盤 通常盤 初回生産限定盤 完全生産限定盤 通常版 限定版 豪华版 豪華版 特別版 特别版 完全版
纪念版 紀念版 珍藏版 典藏版 デラックス デラックス盤 デラックスエディション 盤 版
""".split())
_ORDINAL_RE = re.compile(r"^\d+(?:st|nd|rd|th)?$")
_SEGMENT_RE = re.compile(r"[\(\[（【［]([^\)\]）】］]*)[\)\]）】］]|\s[-–—~:]\s(.+)$")

_LOSSLESS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv", ".dsf", ".dff", ".alac"}


def _is_edition_note(note: str) -> bool:
    tokens = single_merge._fold(note).split()
    return bool(tokens) and all(t in _EDITION_WORDS or _ORDINAL_RE.match(t) for t in tokens)


def strip_editions(title: Any) -> Tuple[str, bool]:
    """``(name without edition notes, folded; whether there was one)``."""
    text = str(title or "")
    found = False

    def take(match: "re.Match[str]") -> str:
        nonlocal found
        note = match.group(1) if match.group(1) is not None else match.group(2)
        if note is not None and _is_edition_note(note):
            found = True
            return " "
        return match.group(0)

    previous = None
    while previous != text:              # "Album (Deluxe) [Remastered]" has two
        previous, text = text, _SEGMENT_RE.sub(take, text).strip()
    words = single_merge._fold(text).split()
    bracketed = bool(re.search(r"[\)\]）】］]\s*$", text))     # "Red (Taylor's Version)" is a name
    if not bracketed and len(words) > 1 and words[-1] in ("edition", "version"):
        # an unbracketed "… Deluxe Edition": the packaging words before it go too
        while len(words) > 1 and (words[-1] in _EDITION_WORDS or _ORDINAL_RE.match(words[-1])):
            words.pop()
            found = True
    return " ".join(words), found


def _seconds(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number / 1000.0 if number > 20000 else number


def _song(title: Any) -> Tuple[str, str]:
    """A track's (song, version) with edition notes ("Remastered 2011",
    "Bonus Track") taken off first."""
    text = str(title or "")
    stripped = _SEGMENT_RE.sub(
        lambda m: " " if _is_edition_note(m.group(1) if m.group(1) is not None else m.group(2) or "")
        else m.group(0), text).strip()
    return single_merge.version_of(stripped or text)


def _size(track: Dict[str, Any]) -> int:
    if "size" not in track:
        try:
            track["size"] = os.path.getsize(track["path"])
        except OSError:
            track["size"] = 0
    return track["size"]


def _ext(track: Dict[str, Any]) -> str:
    return os.path.splitext(str(track.get("path") or ""))[1].lower()


def _rate(track: Dict[str, Any]) -> float:
    """Bytes per second of audio: a format-blind measure of how much is kept."""
    length = _seconds(track.get("duration"))
    size = _size(track)
    if length <= 0:
        return float(size)
    return size / length


class Pairing:
    """How the songs of two albums line up."""

    def __init__(self, a: Dict[str, Any], b: Dict[str, Any], tolerance: float, threshold: float):
        self.pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        self.conflicts = 0
        free = list(b["tracks"])
        rest = []
        for track in a["tracks"]:
            song, version = _song(track["title"])
            hit = None
            for other in free:
                other_song, other_version = _song(other["title"])
                if version != other_version or not single_merge._same_song(song, other_song, threshold):
                    continue
                if single_merge._durations_agree(track["duration"], other["duration"], tolerance):
                    hit = other
                    break
                hit = hit or False      # same name, other length: another recording
            if hit:
                free.remove(hit)
                self.pairs.append((track, hit))
            else:
                if hit is False:
                    self.conflicts += 1
                rest.append(track)
        # titles that differ (a translated or reworded name): the place on the
        # album, the length and the size must all agree
        for track in rest:
            for other in free:
                if (track.get("number") and track.get("number") == other.get("number")
                        and int(track.get("disc") or 1) == int(other.get("disc") or 1)
                        and _ext(track) == _ext(other) and _size(track) and _size(other)
                        and abs(_size(track) - _size(other)) <= 0.01 * max(_size(track), _size(other))
                        and abs(_seconds(track["duration"]) - _seconds(other["duration"])) <= 1.0
                        and _seconds(track["duration"]) > 0):
                    free.remove(other)
                    self.pairs.append((track, other))
                    break
        self.a_count, self.b_count = len(a["tracks"]), len(b["tracks"])

    def similar(self, min_overlap: float) -> bool:
        smaller = min(self.a_count, self.b_count)
        if not smaller or not self.pairs:
            return False
        return (len(self.pairs) >= math.ceil(min_overlap * smaller)
                and self.conflicts <= smaller // 10)

    def a_holds_b(self) -> bool:
        return len(self.pairs) == self.b_count

    def b_holds_a(self) -> bool:
        return len(self.pairs) == self.a_count


def _preference(album: Dict[str, Any]) -> Tuple[int, int, int]:
    """Lower is the better album to keep among versions holding the same songs:
    the plain name first, then the shorter name."""
    return (1 if album["edition_note"] else 0, len(album["title"]), -len(album["tracks"]))


def _components(albums: List[Dict[str, Any]], similar: Dict[Tuple[str, str], Pairing]) -> List[List[Dict[str, Any]]]:
    by_id = {str(a["id"]): a for a in albums}
    links: Dict[str, set] = defaultdict(set)
    for a_id, b_id in similar:
        links[a_id].add(b_id)
        links[b_id].add(a_id)
    seen, out = set(), []
    for start in links:
        if start in seen:
            continue
        stack, part = [start], []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            part.append(by_id[node])
            stack.extend(links[node] - seen)
        if len(part) > 1:
            out.append(part)
    return out


def plan_set(albums: List[Dict[str, Any]], pairing: Callable[[Dict[str, Any], Dict[str, Any]], Pairing]
             ) -> Dict[str, Any]:
    """``{"merges": [(source, target), …], "keepers": [album, …]}`` for one set.

    A version that another version holds entirely is merged into it; among
    versions holding exactly the same songs the plain-named one is kept. What
    is left (the keepers) holds songs none of the others hold all of."""
    def holds(big: Dict[str, Any], small: Dict[str, Any]) -> bool:
        return pairing(big, small).a_holds_b()

    ordered = sorted(albums, key=lambda a: (-len(a["tracks"]), _preference(a)))
    keepers: List[Dict[str, Any]] = []
    merges: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for album in ordered:
        # a keeper that holds it; when they hold the same songs, the better name wins
        home = next((k for k in keepers if holds(k, album)), None)
        if home is None:
            keepers.append(album)
            continue
        if len(home["tracks"]) == len(album["tracks"]) and _preference(album) < _preference(home):
            # same songs, nicer name: this one is kept and the earlier keeper merges into it
            keepers[keepers.index(home)] = album
            merges = [(s, album if t is home else t) for s, t in merges]
            merges.append((home, album))
        else:
            merges.append((album, home))
    return {"merges": merges, "keepers": keepers}


def find_sets(db: Any, tolerance: float = 3.0, min_overlap: float = 0.5, threshold: float = 0.85,
              check_stop: Optional[Callable[[], bool]] = None,
              progress: Optional[Callable[[int, int, str], None]] = None) -> List[Dict[str, Any]]:
    """Every set of editions in the library, planned (see :func:`plan_set`)."""
    albums = single_merge._load(db)
    buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for album in albums.values():
        artist = single_merge._fold(album["artist"])
        if not artist or single_merge._is_single(album):
            continue
        base, note = strip_editions(album["title"])
        if not base:
            continue
        album["edition_note"] = note
        buckets[(artist, base)].append(album)
    work = [group for group in buckets.values() if len(group) > 1]
    out: List[Dict[str, Any]] = []
    for done, group in enumerate(work, 1):
        if check_stop and check_stop():
            break
        if progress:
            progress(done, len(work), f'{group[0]["artist"]} — {group[0]["title"]}')
        group = [a for a in map(single_merge._on_disk, group) if a["tracks"]]
        cache: Dict[Tuple[str, str], Pairing] = {}

        def pairing(a: Dict[str, Any], b: Dict[str, Any], cache: Dict[Tuple[str, str], Pairing] = cache) -> Pairing:
            key = (str(a["id"]), str(b["id"]))
            if key not in cache:
                cache[key] = Pairing(a, b, tolerance, threshold)
            return cache[key]

        similar = {}
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if pairing(a, b).similar(min_overlap):
                    similar[(str(a["id"]), str(b["id"]))] = True
        for part in _components(group, similar):
            plan = plan_set(part, pairing)
            out.append({"albums": part, **plan})
    return out


def _album_ref(album: Dict[str, Any]) -> Dict[str, Any]:
    return {"album_id": album["id"], "title": album["title"], "tracks": len(album["tracks"])}


def merge_details(target: Dict[str, Any], sources: List[Dict[str, Any]], tolerance: float,
                  threshold: float) -> Dict[str, Any]:
    return {
        "album": target["title"], "artist": target["artist"], "artist_id": target["artist_id"],
        "album_thumb_url": target["thumb_url"],
        "target": _album_ref(target), "sources": [_album_ref(s) for s in sources],
        "duration_tolerance": tolerance, "title_similarity": threshold,
    }


def gaps_details(keepers: List[Dict[str, Any]], sets_with: List[Dict[str, Any]]) -> Dict[str, Any]:
    first = keepers[0]
    albums = []
    for album in keepers:
        others = [o for o in keepers if o is not album]
        lacking = []
        for other in others:
            for track in other["tracks"]:
                if not any(single_merge._same_title(track["title"], t["title"], 0.85) for t in album["tracks"]):
                    if track["title"] not in lacking:
                        lacking.append(track["title"])
        albums.append({**_album_ref(album), "lacking": lacking[:50],
                       **{f"{source}_album_id": value for source, value in reversed(album["sources"])}})
    return {"album": first["title"], "artist": first["artist"], "artist_id": first["artist_id"],
            "album_thumb_url": first["thumb_url"], "albums": albums,
            "merged_in": [_album_ref(a) for a in sets_with]}


def finding_text(kind: str, details: Dict[str, Any]) -> Tuple[str, str]:
    artist = details.get("artist") or ""
    if kind == MERGE_TYPE:
        target, sources = details["target"], details["sources"]
        names = ", ".join(f'"{s["title"]}" ({s["tracks"]})' for s in sources)
        title = f'Merge editions into "{target["title"]}" by {artist}'
        same = all(s["tracks"] == target["tracks"] for s in sources)
        why = ("they hold the same songs, and this is the plain name" if same
               else f'it holds every song of {"it" if len(sources) == 1 else "them"}')
        return title, (f'{names} → "{target["title"]}" ({target["tracks"]} tracks): {why}. One copy of each song '
                       "is kept (the better one) and filed under the kept album; the other copies go to the "
                       "deleted-files folder.")
    albums = details["albums"]
    title = f'Editions that each lack songs: "{details["album"]}" by {artist}'
    parts = "; ".join(f'"{a["title"]}" ({a["tracks"]}) lacks {len(a["lacking"])}' for a in albums)
    return title, (f"{parts}. Neither holds all of the other, so they stay separate. Filling them copies the "
                   "missing songs from the library where it can and puts the rest on the wishlist; the next "
                   "scan can then merge them.")


# ── the merge ───────────────────────────────────────────────────────────

def _quarantine(worker: Any, path: str, job: str = "fork_edition_merge") -> Tuple[bool, Optional[str]]:
    from core.repair_worker import _quarantine_file_if_present

    config = getattr(worker, "_config_manager", None)
    download = config.get("soulseek.download_path", "") if config else None
    moved, note, _dest = _quarantine_file_if_present(path, worker.transfer_folder, job,
                                                     config_manager=config, download_folder=download)
    if moved:
        stem = os.path.splitext(path)[0]
        for suffix in (".lrc", ".txt", ".original.lrc", ".original.txt"):
            if os.path.isfile(stem + suffix):
                _quarantine_file_if_present(stem + suffix, worker.transfer_folder, job,
                                            config_manager=config, download_folder=download)
    return moved, note


def _delete_rows(db: Any, track_ids: List[Any]) -> None:
    if not track_ids:
        return
    conn = db._get_connection()
    try:
        conn.execute(f"DELETE FROM tracks WHERE id IN ({', '.join('?' for _ in track_ids)})", tuple(track_ids))
        conn.commit()
    finally:
        conn.close()


def _replace(worker: Any, target_album: Dict[str, Any], keep: Dict[str, Any], old: Dict[str, Any]) -> str:
    """Put the better copy ``keep`` (from another edition) where ``old`` is."""
    from core.fork import album_identity, retro
    from core.tag_writer import write_tags_to_file

    new_path = os.path.splitext(old["path"])[0] + _ext(keep)
    outcome = write_tags_to_file(keep["path"], {
        "title": old["title"], "album_title": target_album["title"], "track_number": old.get("number"),
        "disc_number": int(old.get("disc") or 1), "year": target_album.get("year"),
        "release_date": target_album.get("release_date"),
    }, embed_cover=False)
    if not outcome.get("success"):
        raise RuntimeError(outcome.get("error") or "could not write tags")
    moved, note = _quarantine(worker, old["path"])
    if not moved and os.path.exists(old["path"]):
        raise RuntimeError(note or "the old copy could not be moved away")
    retro._move_file(keep["path"], new_path)
    try:
        album_identity.harmonize(new_path)
    except Exception as exc:
        logger.debug("album ids not aligned for %s: %s", new_path, exc)
    conn = worker.db._get_connection()
    try:
        conn.execute("UPDATE tracks SET file_path = ? WHERE id = ?", (new_path, old["id"]))
        conn.commit()
    finally:
        conn.close()
    return new_path


def merge(worker: Any, details: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the source editions into the target. Checked again against the
    library as it is now: a source the target no longer holds entirely is
    left alone."""
    from core.fork import album_tagging

    db = worker.db
    tolerance = float(details.get("duration_tolerance") or 3.0)
    threshold = float(details.get("title_similarity") or 0.85)
    target_id = (details.get("target") or {}).get("album_id")
    source_ids = [s.get("album_id") for s in details.get("sources") or [] if isinstance(s, dict)]
    loaded = {k: single_merge._on_disk(v) for k, v in single_merge._load(db, [target_id, *source_ids]).items()}
    target = loaded.get(str(target_id))
    if target is None or not target["tracks"]:
        return {"success": False, "error": "The album to keep has no files in the library anymore"}
    merged, upgraded, removed, problems, old_dirs = [], 0, 0, [], set()
    for source_id in source_ids:
        source = loaded.get(str(source_id))
        if source is None or not source["tracks"]:
            continue                                  # already gone
        pairing = Pairing(target, source, tolerance, threshold)
        if not pairing.a_holds_b():
            problems.append(f'"{target["title"]}" no longer holds every song of "{source["title"]}" — scan again')
            continue
        gone = []
        for kept, other in pairing.pairs:
            if not album_tagging.root_of(other["path"]):
                problems.append(f'{other["path"]} is outside the library folders')
                break
            try:
                if os.path.realpath(other["path"]) == os.path.realpath(kept["path"]):
                    # two library rows for one file: only the extra row goes
                    gone.append(other["id"])
                    continue
                if _rate(other) > 1.1 * _rate(kept) and album_tagging.root_of(kept["path"]):
                    kept["path"] = _replace(worker, target, other, kept)
                    upgraded += 1
                else:
                    moved, note = _quarantine(worker, other["path"])
                    if not moved and os.path.exists(other["path"]):
                        raise RuntimeError(note or "could not move it away")
                    removed += 1
                old_dirs.add(os.path.dirname(other["path"]))
                gone.append(other["id"])
            except Exception as exc:
                problems.append(f'{os.path.basename(other["path"])}: {exc}')
        _delete_rows(db, gone)
        if len(gone) == len(source["tracks"]):
            conn = db._get_connection()
            try:
                conn.execute("DELETE FROM albums WHERE id = ? AND NOT EXISTS "
                             "(SELECT 1 FROM tracks WHERE album_id = ?)", (source["id"], source["id"]))
                conn.commit()
            finally:
                conn.close()
            merged.append(source["title"])
    for folder in old_dirs:
        try:
            single_merge._tidy(folder, album_tagging.root_of(folder) or folder)
        except Exception as exc:
            logger.debug("could not tidy %s: %s", folder, exc)
    if not merged and problems:
        return {"success": False, "error": "; ".join(problems[:3])}
    try:
        from core.fork import store

        store.clear_album_checks()
    except Exception as exc:
        logger.debug("album checks not cleared: %s", exc)
    message = (f'Merged {", ".join(repr(m) for m in merged) or "nothing"} into "{target["title"]}": '
               f"{removed} duplicate copies moved to the deleted-files folder, {upgraded} better copies kept")
    if problems:
        message += f" — {len(problems)} problem(s): {problems[0]}"
    logger.info("Edition merge: %s", message)
    return {"success": True, "action": "editions_merged", "message": message, "fixed": removed + upgraded}


def _folders(album: Dict[str, Any]) -> set:
    return {os.path.dirname(os.path.realpath(t["path"])) for t in album["tracks"] if t.get("path")}


def share_folder(albums: List[Dict[str, Any]]) -> bool:
    """True when two of ``albums`` have files in one folder: that is one album
    split into library rows, not two editions. Filling them from each other
    would only copy files next to themselves (Album Grouping merges them)."""
    seen: set = set()
    for album in albums:
        folders = _folders(album)
        if folders & seen:
            return True
        seen |= folders
    return False


def fill_gaps(worker: Any, details: Dict[str, Any]) -> Dict[str, Any]:
    """Fill each edition up with the stock Fill Album fixer (library copies
    first, the wishlist for the rest)."""
    ids = [a.get("album_id") for a in details.get("albums") or [] if isinstance(a, dict)]
    loaded = [single_merge._on_disk(a) for a in single_merge._load(worker.db, ids).values()] if ids else []
    if share_folder(loaded):
        return {"success": False, "error": "These are one album split into library rows (their files share a "
                                           "folder), not editions: use Album Grouping to merge them"}
    messages, failed = [], []
    for album in details.get("albums") or []:
        if not isinstance(album, dict) or album.get("album_id") is None:
            continue
        info = {**album, "album_title": album.get("title"), "artist": details.get("artist"),
                "album_thumb_url": details.get("album_thumb_url"), "missing_tracks": []}
        try:
            result = worker._fix_incomplete_album("album", str(album["album_id"]), None, info)
        except Exception as exc:
            result = {"success": False, "error": str(exc)}
        (messages if result.get("success") else failed).append(
            f'"{album.get("title")}": {result.get("message") or result.get("error") or "done"}')
    if not messages:
        return {"success": False, "error": "; ".join(failed[:3]) or "Nothing to fill"}
    return {"success": True, "action": "editions_filled", "message": " | ".join(messages + failed)}


__all__ = ["GAPS_TYPE", "MERGE_TYPE", "Pairing", "fill_gaps", "find_sets", "finding_text", "gaps_details",
           "merge", "merge_details", "plan_set", "share_folder", "strip_editions"]
