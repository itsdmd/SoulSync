"""Duplicate albums: one album the library (and the media server) lists twice.

Part of Album Grouping. Two library albums by one artist with the same name
(compared on letters and digits only) are the same album when nothing they
carry says otherwise (a different release group or year means two albums —
Weezer has several called "Weezer"). They come from files whose album tags
disagree — one file with another MusicBrainz release id is enough for
Navidrome to show a second album — from files spread over two folders, and
from fills that copied a song next to itself.

Merging them makes one album:

* each song is kept once. Copies of one song (same title and version, length
  within a few seconds) keep the better file (more data per second; among
  near-equal ones the original over a later fill copy, then the one already
  in the album folder); the others go to the deleted-files folder;
* a file holding another song's audio under this song's name (same format,
  size within 0.1 % and length within half a second of a different song,
  while this song has a copy of another length) is such a copy too;
* two files of one song with clearly different lengths are left alone and
  listed for a look by hand;
* the files move into one folder (the one holding most of them) and every
  file gets the same release tags (album, album artist, MusicBrainz release,
  release group and album artist ids, date), taken from the file whose
  release id most files carry;
* the library rows are joined into one album.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.fork import single_merge
from utils.logging_config import get_logger

logger = get_logger("fork.album_duplicates")

FINDING_TYPE = "fork_album_duplicates"
JOB_ID = "fork_volume_grouping"

# field -> (ID3 frame or TXXX description, Vorbis key, MP4 atom)
_RELEASE_FIELDS = {
    "album": ("TALB", "album", "\xa9alb"),
    "albumartist": ("TPE2", "albumartist", "aART"),
    "date": ("TDRC", "date", "\xa9day"),
    "musicbrainz_albumid": ("TXXX:MusicBrainz Album Id", "musicbrainz_albumid",
                            "----:com.apple.iTunes:MusicBrainz Album Id"),
    "musicbrainz_releasegroupid": ("TXXX:MusicBrainz Release Group Id", "musicbrainz_releasegroupid",
                                   "----:com.apple.iTunes:MusicBrainz Release Group Id"),
    "musicbrainz_albumartistid": ("TXXX:MusicBrainz Album Artist Id", "musicbrainz_albumartistid",
                                  "----:com.apple.iTunes:MusicBrainz Album Artist Id"),
}


# ── tags ────────────────────────────────────────────────────────────────

def read_release(path: str) -> Optional[Dict[str, Any]]:
    """The release tags of ``path`` plus its length and size, or None."""
    from mutagen import File as MutagenFile

    from core.fork import tags

    try:
        audio = MutagenFile(path)
    except Exception as exc:
        logger.debug("could not open %s: %s", path, exc)
        return None
    if audio is None:
        return None
    kind = tags._kind(audio) if audio.tags is not None else ""
    out: Dict[str, Any] = {"length": float(getattr(audio.info, "length", 0) or 0), "kind": kind}
    for field, (frame, vkey, atom) in _RELEASE_FIELDS.items():
        value: Any = None
        try:
            if kind == "id3":
                found = audio.tags.get(frame)
                value = found.text[0] if found is not None and found.text else None
            elif kind == "vorbis":
                value = (audio.get(vkey) or [None])[0]
            elif kind == "mp4":
                value = (audio.get(atom) or [None])[0]
        except Exception:
            value = None
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        out[field] = str(value).strip() if value is not None else ""
    return out


def write_release(path: str, values: Dict[str, str]) -> bool:
    """Write the non-empty ``values`` (release fields) into ``path``."""
    from mutagen import File as MutagenFile
    from mutagen import id3
    from mutagen.mp4 import MP4FreeForm

    from core.fork import tags

    audio = MutagenFile(path)
    if audio is None:
        return False
    if audio.tags is None:
        audio.add_tags()
    kind = tags._kind(audio)
    if not kind:
        return False
    for field, value in values.items():
        if not value or field not in _RELEASE_FIELDS:
            continue
        frame, vkey, atom = _RELEASE_FIELDS[field]
        if kind == "id3":
            if frame.startswith("TXXX:"):
                desc = frame[5:]
                audio.tags.setall(frame, [id3.TXXX(encoding=3, desc=desc, text=[value])])
            else:
                audio.tags.setall(frame, [getattr(id3, frame)(encoding=3, text=[value])])
        elif kind == "vorbis":
            audio[vkey] = [value]
        elif kind == "mp4":
            audio[atom] = [MP4FreeForm(value.encode("utf-8"))] if atom.startswith("----") else [value]
    tags._save(audio)
    return True


# ── finding the sets ────────────────────────────────────────────────────

def _key(title: Any) -> str:
    from core.fork.jobs import _strict_key

    return _strict_key(str(title or ""))


def _year(value: Any) -> str:
    text = str(value or "").strip()
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else ""


def _files(albums: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each file of ``albums`` once (two rows can point at one file), with
    its tags, length and the rows that list it."""
    by_path: Dict[str, Dict[str, Any]] = {}
    for album in albums:
        for track in album["tracks"]:
            real = os.path.realpath(track["path"])
            entry = by_path.get(real)
            if entry is None:
                info = read_release(real)
                if info is None:
                    continue
                try:
                    stat = os.stat(real)
                except OSError:
                    continue
                entry = by_path[real] = {
                    "path": real, "title": track["title"], "number": track.get("number"),
                    "disc": int(track.get("disc") or 1), "size": stat.st_size, "mtime": stat.st_mtime,
                    "length": info["length"] or _seconds(track.get("duration")),
                    "tags": info, "rows": []}
            entry["rows"].append({"id": track["id"], "album_id": album["id"]})
    names = {_key(a["title"]) for a in albums}
    # A folder named otherwise ("… (Dolby Atmos mix)") holds another release
    # that a row happens to list too: its files stay where they are. Unless
    # its files carry a release id of the named folders (a "… (Deluxe)" folder
    # the same release was downloaded into): they are this album.
    folders = {os.path.dirname(p) for p in by_path}
    named = {d for d in folders if _key(_album_dir_name(d)) in names}
    if named and named != folders:
        releases = {e["tags"]["musicbrainz_albumid"] for p, e in by_path.items()
                    if os.path.dirname(p) in named and e["tags"]["musicbrainz_albumid"]}
        named |= {d for d in folders - named
                  if any(os.path.dirname(p) == d and e["tags"]["musicbrainz_albumid"] in releases
                         for p, e in by_path.items())}
        by_path = {p: e for p, e in by_path.items() if os.path.dirname(p) in named}
    # files of these folders no row lists (a row whose path went stale)
    for folder in sorted({os.path.dirname(p) for p in by_path}):
        for path in _audio_files(folder):
            real = os.path.realpath(path)
            if real in by_path:
                continue
            info = read_release(real)
            if info is None or _key(info["album"]) not in names:
                continue
            stat = os.stat(real)
            title, number, disc = _track_tags(real)
            by_path[real] = {"path": real, "title": title, "number": number, "disc": disc,
                             "size": stat.st_size, "mtime": stat.st_mtime, "length": info["length"],
                             "tags": info, "rows": []}
    return sorted(by_path.values(), key=lambda f: (f["disc"], int(f["number"] or 0), f["path"]))


def _album_dir_name(folder: str) -> str:
    """The album folder's name, above a "Disc N" sub-folder."""
    from core.fork import album_identity

    return os.path.basename(album_identity.album_folder(os.path.join(folder, "x")))


def _audio_files(folder: str) -> List[str]:
    from core.fork import album_identity

    return album_identity._audio_files(folder)


def _track_tags(path: str) -> Tuple[str, int, int]:
    """(title, track number, disc number) of a file no library row lists."""
    from core.fork import album_tagging

    try:
        tags = album_tagging._read_tags(path)
    except Exception:
        tags = {}
    return (str(tags.get("title") or os.path.splitext(os.path.basename(path))[0]),
            int(tags.get("track_number") or 0), int(tags.get("disc_number") or 1))


def _seconds(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number / 1000.0 if number > 20000 else number


def _rate(entry: Dict[str, Any]) -> float:
    return entry["size"] / entry["length"] if entry["length"] > 0 else float(entry["size"])


def _same(a: Dict[str, Any], b: Dict[str, Any], tolerance: float) -> bool:
    return single_merge._same_title(a["title"], b["title"], 0.85)


def _lengths_agree(a: Dict[str, Any], b: Dict[str, Any], tolerance: float) -> bool:
    return a["length"] <= 0 or b["length"] <= 0 or abs(a["length"] - b["length"]) <= tolerance


def _same_audio(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (os.path.splitext(a["path"])[1].lower() == os.path.splitext(b["path"])[1].lower()
            and a["size"] and b["size"] and abs(a["size"] - b["size"]) <= 0.001 * max(a["size"], b["size"])
            and a["length"] > 0 and abs(a["length"] - b["length"]) <= 0.5)


def _from_fill(entry: Dict[str, Any]) -> bool:
    return bool(entry["rows"]) and all(str(row["id"]).startswith("album_fill_") for row in entry["rows"])


def official_tracks(albums: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """The track lists the listings' metadata sources give (each distinct
    list once; a listing may point at another release)."""
    out: List[List[Dict[str, Any]]] = []
    for album in albums:
        songs = single_merge.track_list(album)
        if songs and songs not in out:
            out.append(songs)
    return out


def _strangers(files: List[Dict[str, Any]], official: List[List[Dict[str, Any]]],
               tolerance: float) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Files that are no song of the release: the title matches no song of
    any list, and every list that is this album's (most files match a song
    on it) has another song at the file's place, of a known, other length.
    (A bonus track sits past the end of a shorter list and stays.)"""
    def known(entry: Dict[str, Any], songs: List[Dict[str, Any]]) -> bool:
        song = single_merge.version_of(entry["title"])[0]
        return any(single_merge._same_title(entry["title"], o["title"], 0.85)
                   or single_merge._same_song(song, single_merge.version_of(o["title"])[0], 0.85)
                   for o in songs)

    lists = [songs for songs in official if 2 * sum(1 for f in files if not known(f, songs)) < len(files)]
    if not lists:
        return []
    out = []
    for entry in files:
        if any(known(entry, songs) for songs in official):
            continue
        places = [next((o for o in songs if (o["disc"], o["number"]) == (entry["disc"], int(entry["number"] or 0))),
                       None) for songs in lists]
        if all(song is not None and song["duration"] > 0 and entry["length"] > 0
               and abs(song["duration"] - entry["length"]) > tolerance for song in places):
            out.append((entry, places[0]))
    return out


def plan(albums: List[Dict[str, Any]], tolerance: float = 3.0,
         official: Optional[List[List[Dict[str, Any]]]] = None) -> Optional[Dict[str, Any]]:
    """What merging ``albums`` (one album listed more than once, files on
    disk) changes, or None when there is nothing to do. ``official`` is the
    release's track list (:func:`official_tracks`): a file of a song not on
    it, at the place of one that is, goes too."""
    files = _files(albums)
    if len(files) < 2:
        return None
    from core.fork import album_identity

    def album_dir(entry: Dict[str, Any]) -> str:
        return album_identity.album_folder(entry["path"])

    # one set of release tags, from the file whose release id most files carry
    ids = Counter(f["tags"]["musicbrainz_albumid"] for f in files if f["tags"]["musicbrainz_albumid"])
    pool = [f for f in files if not ids or f["tags"]["musicbrainz_albumid"] == ids.most_common(1)[0][0]]
    reference = sorted(pool, key=lambda f: (-sum(1 for k in _RELEASE_FIELDS if f["tags"][k]), f["path"]))[0]
    values = {k: reference["tags"][k] for k in _RELEASE_FIELDS if reference["tags"][k]}
    # the folder named after that album ("Manic", not "Manic (44.1kHz_24bit)"), then the one holding most
    folders = Counter(album_dir(f) for f in files)
    name = _key(values.get("album") or albums[0]["title"])
    target_folder = sorted(folders, key=lambda d: (_key(os.path.basename(d)) != name, -folders[d], len(d), d))[0]

    removals: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    gone: set = set()
    # another song's audio under this song's name
    for entry in files:
        twin = next((o for o in files if o is not entry and not _same(entry, o, tolerance)
                     and _same_audio(entry, o)), None)
        if twin is None:
            continue
        rival = next((o for o in files if o is not entry and o is not twin and _same(entry, o, tolerance)
                      and not _lengths_agree(entry, o, tolerance)), None)
        if rival is not None and twin["path"] not in gone:
            gone.add(entry["path"])
            removals.append({"path": entry["path"], "title": entry["title"], "kept": rival["path"],
                             "reason": f'holds the audio of "{twin["title"]}"'})
    # copies of one song
    left = [f for f in files if f["path"] not in gone]
    clusters: List[List[Dict[str, Any]]] = []
    for entry in left:
        home = next((c for c in clusters if _same(c[0], entry, tolerance)
                     and all(_lengths_agree(m, entry, tolerance) for m in c)), None)
        if home is None:
            clusters.append([entry])
        else:
            home.append(entry)
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        # bytes per second compare only when every length is known
        rated = all(m["length"] > 0 for m in cluster)
        best_rate = max(_rate(m) for m in cluster) if rated else 0.0
        keep = sorted(cluster, key=lambda m: (
            rated and _rate(m) < 0.95 * best_rate, _from_fill(m), album_dir(m) != target_folder,
            m["mtime"], len(os.path.basename(m["path"]))))[0]
        for member in cluster:
            if member is keep:
                continue
            gone.add(member["path"])
            removals.append({"path": member["path"], "title": member["title"], "kept": keep["path"],
                             "reason": "a second copy" if _rate(member) >= 0.95 * best_rate
                             else "a lower-quality copy"})
    # a song that is not on the release, in the place of one that is
    for entry, song in _strangers([f for f in files if f["path"] not in gone], official or [], tolerance):
        gone.add(entry["path"])
        removals.append({"path": entry["path"], "title": entry["title"], "kept": None,
                         "reason": f'not a song of this album (track {song["number"]} is "{song["title"]}")',
                         "wrong": True})
    kept = [f for f in files if f["path"] not in gone]
    by_song: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in kept:
        by_song[single_merge.version_of(entry["title"])[0] + "|" + single_merge.version_of(entry["title"])[1]
                ].append(entry)
    for same in by_song.values():
        if len(same) > 1:
            conflicts.append({"title": same[0]["title"],
                              "files": [{"path": m["path"], "seconds": round(m["length"])} for m in same]})

    retag = [f["path"] for f in kept if any(f["tags"][k] != v for k, v in values.items())]
    # a file in a "Disc N" sub-folder keeps it
    moves = [{"from": f["path"], "to": os.path.join(target_folder, os.path.relpath(f["path"], album_dir(f)))}
             for f in kept if album_dir(f) != target_folder]

    rows = Counter(r["album_id"] for f in kept for r in f["rows"])
    target_album = next(a for a in sorted(albums, key=lambda a: (-rows.get(a["id"], 0), str(a["id"]))))
    # rows alone (two media servers listing the same files) change nothing a
    # player shows: only sets with something to change on disk are reported
    if not (removals or retag or moves):
        return None
    return {"target": {"album_id": target_album["id"], "title": target_album["title"], "folder": target_folder},
            "albums": [{"album_id": a["id"], "title": a["title"], "tracks": len(a["tracks"]),
                        "folders": sorted({os.path.dirname(os.path.realpath(t["path"])) for t in a["tracks"]})}
                       for a in albums],
            "files": len(files), "kept": len(kept), "removals": removals, "conflicts": conflicts,
            "moves": moves, "retag": len(retag), "tags": values}


def _one_album(albums: List[Dict[str, Any]]) -> bool:
    """Nothing the rows carry says they are different albums: a release-group
    id on each side that differs, or a year on each side that differs."""
    groups, years = set(), set()
    for album in albums:
        rg = Counter(t["tags"]["musicbrainz_releasegroupid"] for t in album["_files"]
                     if t["tags"]["musicbrainz_releasegroupid"])
        yr = Counter(_year(t["tags"]["date"]) for t in album["_files"] if _year(t["tags"]["date"]))
        if rg:
            groups.add(rg.most_common(1)[0][0])
        if yr:
            years.add(yr.most_common(1)[0][0])
    return len(groups) <= 1 and len(years) <= 1


def _parts(group: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """The albums of ``group`` (one artist, one name once edition notes are
    dropped) that are one album: files in one folder make it one (an "X" and
    an "X (Deluxe)" there are one album split into rows, whatever years their
    tags give); one name makes it one when nothing they carry says otherwise
    (:func:`_one_album`). An "X (Deluxe)" in its own folder is another edition
    (Fill Editions)."""
    from core.fork import edition_merge

    parent = list(range(len(group)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    folders = [edition_merge._folders(a) for a in group]
    for i, a in enumerate(group):
        for j in range(i + 1, len(group)):
            b = group[j]
            if folders[i] & folders[j] or (_key(a["title"]) == _key(b["title"]) and _one_album([a, b])):
                parent[root(j)] = root(i)
    parts: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for i, album in enumerate(group):
        parts[root(i)].append(album)
    return [part for part in parts.values() if len(part) > 1]


def find_sets(db: Any, tolerance: float = 3.0, check_stop: Optional[Callable[[], bool]] = None,
              progress: Optional[Callable[[int, int, str], None]] = None) -> List[Dict[str, Any]]:
    """Every album the library lists more than once, with its merge plan."""
    from core.fork.edition_merge import strip_editions
    from core.fork.jobs import parse_volume

    buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for album in single_merge._load(db).values():
        artist = single_merge._fold(album["artist"])
        key = _key(album["title"])
        if not artist or not key or single_merge._is_single(album) or parse_volume(album["title"]):
            continue
        buckets[(artist, _key(strip_editions(album["title"])[0]) or key)].append(album)
    work = [group for group in buckets.values() if len(group) > 1]
    out: List[Dict[str, Any]] = []
    for done, group in enumerate(work, 1):
        if check_stop and check_stop():
            break
        if progress:
            progress(done, len(work), f'{group[0]["artist"]} — {group[0]["title"]}')
        group = [a for a in map(single_merge._on_disk, group) if a["tracks"]]
        if len(group) < 2:
            continue
        for album in group:
            album["_files"] = _files([album])
        for part in _parts(group):
            # the track list (a metadata lookup) only for sets with something to merge
            found = plan(part, tolerance) and plan(part, tolerance, official_tracks(part))
            if found:
                out.append({"artist": part[0]["artist"], "album": found["target"]["title"], **found})
    return out


def finding_text(details: Dict[str, Any]) -> Tuple[str, str]:
    count = len(details["albums"])
    parts = [f'{count} library albums → one ({details["kept"]} tracks)']
    copies = [r for r in details["removals"] if not r.get("wrong")]
    wrong = [r for r in details["removals"] if r.get("wrong")]
    if copies:
        parts.append(f'{len(copies)} duplicate cop{"y" if len(copies) == 1 else "ies"} to the deleted-files folder')
    if wrong:
        parts.append(f'{len(wrong)} song(s) not on this album ({", ".join(r["title"] for r in wrong[:2])}) to the '
                     "deleted-files folder, their places filled")
    if details["moves"]:
        parts.append(f'{len(details["moves"])} file(s) moved into "{os.path.basename(details["target"]["folder"])}"')
    if details["retag"]:
        parts.append(f'release tags aligned on {details["retag"]} file(s)')
    if details["conflicts"]:
        parts.append(f'{len(details["conflicts"])} song(s) with copies of different lengths left for you')
    return (f'Duplicate album: "{details["album"]}" by {details["artist"]}', "; ".join(parts))


# ── the merge ───────────────────────────────────────────────────────────

def merge(worker: Any, details: Dict[str, Any]) -> Dict[str, Any]:
    """Merge one finding's albums, planned again against the library as it is
    now. Only copies the finding listed are removed."""
    from core.fork import album_tagging, edition_merge, retro

    db = worker.db
    ids = [a.get("album_id") for a in details.get("albums") or [] if isinstance(a, dict)]
    loaded = list(single_merge._load(db, ids).values())
    listed = {a["id"]: [t["id"] for t in a["tracks"]] for a in loaded}
    albums = [a for a in map(single_merge._on_disk, loaded) if a["tracks"]]
    if not albums:
        return {"success": True, "action": "already_gone", "message": "These albums have no files anymore"}
    found = plan(albums, float(details.get("duration_tolerance") or 3.0), official_tracks(albums))
    if not found:
        return {"success": True, "action": "already_merged", "message": "Nothing left to merge"}
    approved = {r.get("path") for r in details.get("removals") or [] if isinstance(r, dict)}
    problems: List[str] = []
    removed = moved = retagged = 0
    wrong: List[str] = []
    files = {f["path"]: f for f in _files(albums)}
    gone_rows: List[Any] = []
    for removal in found["removals"]:
        path = removal["path"]
        if path not in approved:
            problems.append(f"{os.path.basename(path)}: not in the finding, kept (scan again)")
            continue
        if not album_tagging.root_of(path):
            problems.append(f"{path} is outside the library folders")
            continue
        ok, note = edition_merge._quarantine(worker, path, JOB_ID)
        if not ok and os.path.exists(path):
            problems.append(f"{os.path.basename(path)}: {note or 'could not move it away'}")
            continue
        removed += 1
        if removal.get("wrong"):
            wrong.append(removal["title"])
        rows = files[path]["rows"] if path in files else []
        kept = files.get(removal["kept"])
        if kept is not None and not kept["rows"] and rows:
            kept["rows"], rows = rows[:1], rows[1:]      # the kept copy had no row: it takes this one
        gone_rows += [r["id"] for r in rows]
    # rows of files that are gone (the media server not rescanned yet) would keep a listing alive
    for album in albums:
        present = {t["id"] for t in album["tracks"]}
        gone_rows += [tid for tid in listed.get(album["id"], []) if tid not in present]
    edition_merge._delete_rows(db, gone_rows)

    final: Dict[str, str] = {p: p for p in files if os.path.exists(p)}
    touched: set = set()
    for move in found["moves"]:
        src, dst = move["from"], move["to"]
        if not os.path.exists(src):
            continue
        if os.path.exists(dst):
            problems.append(f"{os.path.basename(src)}: a file of that name is already in the album folder")
            continue
        try:
            retro._move_file(src, dst)
            final[src] = dst
            moved += 1
            touched.add((os.path.dirname(src), os.path.dirname(dst), album_tagging.root_of(dst) or ""))
        except Exception as exc:
            problems.append(f"{os.path.basename(src)}: move failed: {exc}")
    for path in final.values():
        info = read_release(path)
        if info is None or all(info.get(k) == v for k, v in found["tags"].items()):
            continue
        try:
            if write_release(path, found["tags"]):
                retagged += 1
        except Exception as exc:
            problems.append(f"{os.path.basename(path)}: tags not written: {exc}")
    for old_dir, new_dir, root in sorted(touched, key=lambda item: -len(item[0])):
        try:
            retro._carry_leftovers(old_dir, new_dir, root)
        except Exception as exc:
            logger.debug("could not tidy %s: %s", old_dir, exc)

    # one library album
    target_id = found["target"]["album_id"]
    rows = [r["id"] for src, entry in files.items() if src in final for r in entry["rows"]]
    conn = db._get_connection()
    try:
        for src, dst in final.items():
            for row in files[src]["rows"]:
                conn.execute("UPDATE tracks SET album_id = ?, file_path = ? WHERE id = ?", (target_id, dst, row["id"]))
        for album_id in ids:
            if str(album_id) != str(target_id):
                conn.execute("DELETE FROM albums WHERE id = ? AND NOT EXISTS (SELECT 1 FROM tracks WHERE album_id = ?)",
                             (album_id, album_id))
        conn.commit()
    finally:
        conn.close()
    try:
        from core.fork import store

        store.clear_album_checks()
    except Exception as exc:
        logger.debug("album checks not cleared: %s", exc)
    filled = _refill(worker, found, albums) if wrong else ""
    message = (f'Merged {len(albums)} listings of "{found["target"]["title"]}" into one ({len(rows)} track rows): '
               f"{removed} duplicate copies moved to the deleted-files folder, {moved} file(s) moved, "
               f"{retagged} retagged")
    if wrong:
        message += f'; not on this album, removed: {", ".join(wrong[:3])} ({filled})'
    if found["conflicts"]:
        message += f'; check by hand: {", ".join(c["title"] for c in found["conflicts"][:3])}'
    if problems:
        message += f" — {len(problems)} problem(s): {problems[0]}"
    logger.info("Album grouping: %s", message)
    return {"success": True, "action": "duplicates_merged", "message": message, "fixed": removed + moved + retagged}


def _refill(worker: Any, found: Dict[str, Any], albums: List[Dict[str, Any]]) -> str:
    """Fill the places of the songs that were not this album's with the
    stock Fill Album fixer (a library copy first, the wishlist otherwise)."""
    target = next((a for a in albums if str(a["id"]) == str(found["target"]["album_id"])), albums[0])
    sources = {source: value for album in [target, *albums] for source, value in reversed(album["sources"])}
    info = {"album_id": target["id"], "album_title": found["target"]["title"], "artist": target["artist"],
            "missing_tracks": [], **{f"{source}_album_id": value for source, value in sources.items()
                                     if source in ("spotify", "itunes", "deezer", "discogs")}}
    try:
        result = worker._fix_incomplete_album("album", str(target["id"]), None, info)
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
    return str(result.get("message") or result.get("error") or "filled")


__all__ = ["FINDING_TYPE", "find_sets", "finding_text", "merge", "official_tracks", "plan", "read_release", "write_release"]
