"""Singles and their albums: merging a single an album is missing, and
keeping releases that carry another version of a song."""

import os
import sqlite3

import pytest
from mutagen.flac import FLAC

from core.fork import album_tagging, retro, single_merge, tags
from core.repair_jobs.duplicate_detector import DuplicateDetectorJob
from core.repair_jobs.single_album_dedup import SingleAlbumDedupJob

_MINIMAL_FLAC = (
    b"fLaC" + b"\x80\x00\x00\x22" + b"\x00\x10\x00\x10" + b"\x00\x00\x00\x00\x00\x00"
    + b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)
LISTING = {"Be Not Afraid": ["Intro", "With Eyes to See", "Outro"]}


class Library:
    def __init__(self, tmp_path):
        self.root = tmp_path / "library"
        self.root.mkdir(parents=True)
        self.path = str(tmp_path / "lib.db")
        conn = self._get_connection()
        conn.executescript(
            "CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT, thumb_url TEXT);"
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, artist_id INTEGER, title TEXT, year INTEGER,"
            " record_type TEXT, track_count INTEGER, api_track_count INTEGER, deezer_id TEXT, thumb_url TEXT);"
            "CREATE TABLE tracks (id INTEGER PRIMARY KEY, album_id INTEGER, artist_id INTEGER, title TEXT,"
            " track_number INTEGER, disc_number INTEGER, duration INTEGER, file_path TEXT, bitrate INTEGER,"
            " track_artist TEXT);")
        conn.commit()
        conn.close()
        self.ids = {}

    def _get_connection(self):
        return sqlite3.connect(self.path)

    def add(self, album, title, number=1, kind="album", expected=3, duration=200, artist="Polyphia", files=True):
        conn = self._get_connection()
        aid = self.ids.get(artist) or conn.execute("INSERT INTO artists (name) VALUES (?)", (artist,)).lastrowid
        self.ids[artist] = aid
        alid = self.ids.get((artist, album)) or conn.execute(
            "INSERT INTO albums (artist_id, title, year, record_type, track_count, api_track_count, deezer_id)"
            " VALUES (?, ?, 2025, ?, ?, ?, ?)", (aid, album, kind, expected, expected, album)).lastrowid
        self.ids[(artist, album)] = alid
        path = str(self.root / artist / album / f"{number:02d} - {title}.flac")
        if files:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(_MINIMAL_FLAC)
            audio = FLAC(path)
            audio["title"], audio["album"], audio["artist"] = [title], [album], [artist]
            audio["tracknumber"] = [str(number)]
            audio.save()
        tid = conn.execute(
            "INSERT INTO tracks (album_id, artist_id, title, track_number, disc_number, duration, file_path, bitrate)"
            " VALUES (?, ?, ?, ?, 1, ?, ?, 900)", (alid, aid, title, number, duration, path)).lastrowid
        conn.commit()
        conn.close()
        return tid

    def row(self, track_id):
        conn = self._get_connection()
        row = conn.execute("SELECT al.title, t.track_number, t.file_path FROM tracks t JOIN albums al"
                           " ON al.id = t.album_id WHERE t.id = ?", (track_id,)).fetchone()
        conn.close()
        return row

    def albums(self):
        conn = self._get_connection()
        names = [r[0] for r in conn.execute("SELECT title FROM albums ORDER BY title")]
        conn.close()
        return names


class Ctx:
    def __init__(self, db, job_id, settings=None):
        self.db, self.findings = db, []
        stored = settings or {}

        class Cfg:
            def get(self, key, default=None):
                return stored if key == f"repair.jobs.{job_id}.settings" else default

        self.config_manager = Cfg()
        self.update_progress = lambda done, total: None
        self.report_progress = lambda **kwargs: None
        self.playlist_membership = None

    def check_stop(self):
        return False

    def create_finding(self, **kwargs):
        self.findings.append(kwargs)
        return True


@pytest.fixture
def lib(tmp_path, monkeypatch):
    library = Library(tmp_path)
    monkeypatch.setattr(album_tagging, "allowed_roots", lambda: [os.path.realpath(str(library.root))])
    monkeypatch.setattr(album_tagging, "_resolve", lambda p: p if p and os.path.isfile(p) else None)
    monkeypatch.setattr(album_tagging, "_template_path", lambda root, values, album, total, discs, ext: os.path.join(
        root, f"{int(values['track_number']):02d} - {values['title']}{ext}"))
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())
    monkeypatch.setattr(retro, "_update_db_path", lambda old, new: None)
    monkeypatch.setattr(single_merge, "track_list", lambda album: [
        {"title": title, "number": n, "disc": 1, "duration": 200.0}
        for n, title in enumerate(LISTING.get(album["title"], []), 1)])
    return library


def test_version_notes_are_told_from_the_song():
    v = single_merge.version_of
    assert v("With Eyes to See") == ("with eyes to see", "")
    assert v("With Eyes to See (Instrumental)") == ("with eyes to see", "instrumental")
    assert v("Song - Live at Wembley")[1] == "live at wembley" and v("Song [Acoustic]")[1] == "acoustic"
    assert v("夜曲（伴奏）")[1] and v("夜曲")[1] == ""
    # not versions: a subtitle, a feature credit, the plain take, a mastering
    for plain in ("Song (Part 2)", "Song (feat. Someone)", "Song (Album Version)", "Song (Original Mix)",
                  "Song (Remastered 2011)", "Alive", "Olive Tree"):
        assert v(plain)[1] == "", plain


def test_a_single_an_album_is_missing_is_found_and_merged(lib):
    lib.add("Be Not Afraid", "Intro", 1)
    single = lib.add("With Eyes to See", "WITH EYES TO SEE", 1, kind="single", expected=1)
    cover = lib.root / "Polyphia" / "With Eyes to See" / "cover.jpg"
    cover.write_bytes(b"x")
    merges = single_merge.find_merges(lib)
    assert len(merges) == 1
    details = merges[0]
    assert details["target"]["title"] == "Be Not Afraid" and details["album"] == "Be Not Afraid" and details["single"]["title"] == "With Eyes to See"
    assert [(t["track_id"], t["track_number"], t["listed_as"]) for t in details["tracks"]] == [
        (single, 2, "With Eyes to See")]
    title, description = single_merge.finding_text(details)
    assert "Be Not Afraid" in title and "1 of 3" in description

    done = single_merge.merge(lib, details)
    assert done["success"] and done["fixed"] == 1
    target = str(lib.root / "Polyphia" / "Be Not Afraid" / "02 - WITH EYES TO SEE.flac")
    assert lib.row(single) == ("Be Not Afraid", 2, target)
    audio = FLAC(target)
    assert audio["album"] == ["Be Not Afraid"] and audio["tracknumber"][0].split("/")[0] in ("2", "02")
    assert lib.albums() == ["Be Not Afraid"]                       # the single is gone as a release
    assert not cover.parent.exists()                               # and so is its folder
    assert single_merge.find_merges(lib) == []
    again = single_merge.merge(lib, details)
    assert again["success"] and again["action"] == "already_gone"


def test_a_single_with_its_instrumental_is_kept_whole(lib):
    lib.add("Be Not Afraid", "Intro", 1)
    lib.add("With Eyes to See", "With Eyes to See", 1, kind="single", expected=2)
    lib.add("With Eyes to See", "With Eyes to See (Instrumental)", 2, kind="single", expected=2)
    assert single_merge.find_merges(lib) == []
    # ... unless the album lists the instrumental too
    LISTING["Be Not Afraid"].append("With Eyes to See (Instrumental)")
    try:
        merges = single_merge.find_merges(lib)
        assert [t["track_number"] for t in merges[0]["tracks"]] == [2, 4]
    finally:
        LISTING["Be Not Afraid"].pop()


def test_no_merge_when_the_album_has_the_song_or_the_cut_differs_or_nothing_is_owned(lib):
    lib.add("Be Not Afraid", "With Eyes to See", 2)
    lib.add("With Eyes to See", "With Eyes to See", 1, kind="single", expected=1)
    assert single_merge.find_merges(lib) == []                     # redundant, not missing
    other = Library(lib.root.parent / "other")
    other.add("Be Not Afraid", "Intro", 1)
    other.add("With Eyes to See", "With Eyes to See", 1, kind="single", expected=1, duration=140)
    assert single_merge.find_merges(other) == []                   # a radio edit, a minute shorter
    assert len(single_merge.find_merges(other, tolerance=90)) == 1
    loose = Library(lib.root.parent / "loose")
    loose.add("Be Not Afraid", "Intro", 1)
    loose.add("Greatest Hits", "With Eyes to See", 1, kind=None, expected=0)   # one track of who knows what
    assert single_merge.find_merges(loose) == []
    empty = Library(lib.root.parent / "empty")
    empty.add("Be Not Afraid", "Intro", 1, files=False)            # the album is only a database row
    empty.add("With Eyes to See", "With Eyes to See", 1, kind="single", expected=1)
    assert single_merge.find_merges(empty) == []


def _two_releases(lib, with_instrumental):
    album = lib.add("Be Not Afraid", "With Eyes to See", 2)
    lib.add("Be Not Afraid", "Intro", 1)
    single = lib.add("With Eyes to See", "With Eyes to See", 1, kind="single", expected=2)
    inst = lib.add("With Eyes to See", "With Eyes to See (Instrumental)", 2, kind="single", expected=2) \
        if with_instrumental else None
    return album, single, inst


def test_versions_and_releases_that_carry_them_are_kept(lib):
    album, single, inst = _two_releases(lib, True)
    index = single_merge.ReleaseIndex(lib)
    t = lambda track_id, title: {"id": track_id, "title": title}  # noqa: E731
    assert index.keep_both(t(single, "With Eyes to See"), t(inst, "With Eyes to See (Instrumental)"))
    assert index.keep_both(t(album, "With Eyes to See"), t(single, "With Eyes to See"))
    assert index.keep_both(t(single, "With Eyes to See"), t(album, "With Eyes to See"))


def test_a_plain_single_copy_is_still_a_duplicate(lib):
    album, single, _ = _two_releases(lib, False)
    index = single_merge.ReleaseIndex(lib)
    assert not index.keep_both({"id": album, "title": "With Eyes to See"}, {"id": single, "title": "With Eyes to See"})


@pytest.mark.parametrize("with_instrumental", [True, False])
def test_the_tools_report_or_keep_accordingly(lib, with_instrumental):
    _two_releases(lib, with_instrumental)
    lib.add("Lonely", "Lonely Song", 1, kind="single", expected=1)
    lib.add("Longplay", "Opener", 1)
    LISTING["Longplay"] = ["Opener", "Lonely Song", "Closer"]
    try:
        dedup = Ctx(lib, "single_album_dedup")
        SingleAlbumDedupJob().scan(dedup)
        dupes = Ctx(lib, "duplicate_detector")
        DuplicateDetectorJob().scan(dupes)
    finally:
        del LISTING["Longplay"]
    kinds = sorted(f["finding_type"] for f in dedup.findings)
    merge = next(f for f in dedup.findings if f["finding_type"] == "fork_single_into_album")
    assert merge["details"]["target"]["title"] == "Longplay" and merge["entity_type"] == "album"
    if with_instrumental:
        assert kinds == ["fork_single_into_album"] and dupes.findings == []
    else:
        assert kinds == ["fork_single_into_album", "single_album_redundant"]
        assert [f["details"]["count"] for f in dupes.findings] == [2]


def test_merging_can_be_switched_off(lib):
    lib.add("Be Not Afraid", "Intro", 1)
    lib.add("With Eyes to See", "With Eyes to See", 1, kind="single", expected=1)
    ctx = Ctx(lib, "single_album_dedup", {"merge_into_albums": False})
    SingleAlbumDedupJob().scan(ctx)
    assert ctx.findings == []
