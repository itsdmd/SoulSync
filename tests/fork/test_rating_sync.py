"""Rating Tag Sync: the rating tag of each format, and the two directions."""

from __future__ import annotations

import sqlite3

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, POPM, TIT2

from core.fork import rating_sync
from core.repair_jobs import fork_tools
from core.repair_jobs.base import JobContext

_MINIMAL_FLAC = (
    b"fLaC" + b"\x80\x00\x00\x22" + b"\x00\x10\x00\x10" + b"\x00\x00\x00\x00\x00\x00"
    + b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)


def _flac(path, **tags):
    path.write_bytes(_MINIMAL_FLAC)
    audio = FLAC(str(path))
    for key, value in tags.items():
        audio[key] = [value]
    audio.save()
    return str(path)


def _mp3(path, *frames):
    path.write_bytes((b"\xff\xfb\x90\x00" + b"\x00" * 413) * 8)
    id3 = ID3()
    for frame in (TIT2(encoding=3, text=["T"]), *frames):
        id3.add(frame)
    id3.save(str(path))
    return str(path)


def test_flac_rating_is_read_on_any_scale_and_written_as_0_to_100(tmp_path):
    for value, stars in (("80", 4), ("100", 5), ("3", 3), ("0.6", 3), ("1.0", 5), ("1", 1), ("0", 0), ("x", 0)):
        assert rating_sync.read_rating(_flac(tmp_path / "a.flac", rating=value)) == stars, value
    assert rating_sync.read_rating(_flac(tmp_path / "b.flac", fmps_rating="0.8")) == 4
    assert rating_sync.read_rating(_flac(tmp_path / "c.flac")) == 0

    path = _flac(tmp_path / "d.flac", title="Song", fmps_rating="0.2")
    rating_sync.write_rating(path, 4)
    audio = FLAC(path)
    assert (audio["rating"], audio["fmps_rating"], audio["title"]) == (["80"], ["0.8"], ["Song"])
    rating_sync.write_rating(path, 0)
    audio = FLAC(path)
    assert "rating" not in audio and "fmps_rating" not in audio and audio["title"] == ["Song"]


def test_mp3_rating_is_a_popm_frame(tmp_path):
    for byte, stars in ((0, 0), (1, 1), (64, 2), (128, 3), (186, 4), (196, 4), (255, 5)):
        path = _mp3(tmp_path / "a.mp3", POPM(email="MusicBee", rating=byte, count=7))
        assert rating_sync.read_rating(path) == stars, byte
    rating_sync.write_rating(path, 3)                        # an existing frame keeps its owner and play count
    frame = ID3(path).getall("POPM")[0]
    assert (frame.email, frame.rating, frame.count) == ("MusicBee", 128, 7)

    bare = _mp3(tmp_path / "b.mp3")
    assert rating_sync.read_rating(bare) == 0
    rating_sync.write_rating(bare, 5)
    assert rating_sync.read_rating(bare) == 5 and str(ID3(bare)["TIT2"]) == "T"
    rating_sync.write_rating(bare, 0)
    assert ID3(bare).getall("POPM") == []


class _DB:
    def __init__(self, path, tracks):
        self.path = str(path)
        conn = self._get_connection()
        conn.executescript(
            "CREATE TABLE artists (id TEXT PRIMARY KEY, name TEXT);"
            "CREATE TABLE tracks (id TEXT PRIMARY KEY, artist_id TEXT, title TEXT, file_path TEXT, server_source TEXT);"
            "INSERT INTO artists VALUES ('ar', 'Artist');")
        conn.executemany("INSERT INTO tracks VALUES (?, 'ar', ?, ?, ?)", tracks)
        conn.commit()
        conn.close()

    def _get_connection(self):
        return sqlite3.connect(self.path)


class _Navidrome:
    """Answers search3 in pages and records setRating."""

    def __init__(self, ratings, connected=True, broken=False):
        self.ratings, self.connected, self.broken = dict(ratings), connected, broken
        self.set = []
        self.songs = [{"id": f"unrated{n}"} for n in range(rating_sync._PAGE)] + [
            {"id": song, "userRating": stars} for song, stars in ratings.items()]

    def ensure_connection(self):
        return self.connected

    def _make_request(self, endpoint, params=None):
        if endpoint == "setRating":
            self.set.append((params["id"], params["rating"]))
            return {"status": "ok"}
        assert endpoint == "search3" and params["query"] == '""'
        if self.broken and params["songOffset"]:
            return None
        return {"searchResult3": {"song": self.songs[params["songOffset"]:params["songOffset"] + params["songCount"]]}}


@pytest.fixture
def library(tmp_path):
    files = {
        "n1": _flac(tmp_path / "one.flac"),                          # rated 4 in Navidrome only
        "n2": _flac(tmp_path / "two.flac", rating="40"),              # 5 in Navidrome, 2 in the file
        "n3": _flac(tmp_path / "three.flac", rating="60"),            # rated in the file only
        "n4": _flac(tmp_path / "four.flac", rating="100"),            # 5 on both sides
        "p1": _flac(tmp_path / "plex.flac", rating="20"),             # not a Navidrome track
    }
    rows = [(song, song, path, "plex" if song == "p1" else "navidrome") for song, path in files.items()]
    rows.append(("n5", "gone", str(tmp_path / "missing.flac"), "navidrome"))
    db = _DB(tmp_path / "library.db", rows)
    log = []
    context = JobContext(db=db, transfer_folder=str(tmp_path), config_manager=None,
                         report_progress=lambda **kw: log.append(kw.get("log_line")))
    return {"files": files, "context": context, "log": log, "ratings": {"n1": 4, "n2": 5, "n4": 5, "n5": 3}}


def _file_ratings(library):
    return {song: rating_sync.read_rating(path) for song, path in library["files"].items()}


def _run(library, client, **settings):
    from core.repair_jobs.base import JobResult

    return rating_sync.sync(library["context"], JobResult(), client=client, **settings)


def test_navidrome_ratings_are_written_to_the_files(library):
    client = _Navidrome(library["ratings"])
    dry = _run(library, client, dry_run=True)
    assert dry.auto_fixed == 0 and _file_ratings(library)["n1"] == 0
    assert sum("Would set" in (line or "") for line in library["log"]) == 2

    result = _run(library, client)
    assert _file_ratings(library) == {"n1": 4, "n2": 5, "n3": 3, "n4": 5, "p1": 1}
    assert (result.scanned, result.auto_fixed, result.errors) == (5, 2, 0) and client.set == []
    assert _run(library, client).auto_fixed == 0                       # nothing left to do

    _run(library, client, clear_unrated=True)                          # unrated in Navidrome: tag removed
    assert _file_ratings(library)["n3"] == 0


def test_file_ratings_are_sent_to_navidrome(library):
    client = _Navidrome(library["ratings"])
    result = _run(library, client, direction=rating_sync.FILE_TO_NAVIDROME)
    assert sorted(client.set) == [("n2", 2), ("n3", 3)] and result.auto_fixed == 2
    assert _file_ratings(library)["n1"] == 0                           # files are not touched

    client.set.clear()
    _run(library, client, direction=rating_sync.FILE_TO_NAVIDROME, clear_unrated=True)
    assert ("n1", 0) in client.set


def test_nothing_changes_when_navidrome_cannot_be_read(library):
    for client in (_Navidrome(library["ratings"], connected=False), _Navidrome(library["ratings"], broken=True)):
        result = _run(library, client, clear_unrated=True)
        assert result.stopped_early and result.auto_fixed == 0
    assert _file_ratings(library) == {"n1": 0, "n2": 2, "n3": 3, "n4": 5, "p1": 1}


def test_the_job_is_registered_weekly_with_navidrome_to_file_as_default(library, monkeypatch):
    from core.repair_jobs import get_all_jobs
    from core.repair_worker import JOB_CATEGORIES

    job = get_all_jobs()["fork_rating_sync"]
    assert job.display_name == "Rating Tag Sync" and job.default_interval_hours == 168
    assert job.default_settings["direction"] == "navidrome_to_file"
    assert job.setting_options["direction"] == ["navidrome_to_file", "file_to_navidrome"]
    assert JOB_CATEGORIES["fork_rating_sync"] == "Tags & metadata"

    client = _Navidrome(library["ratings"])
    monkeypatch.setattr(rating_sync, "_client", lambda: client)
    assert fork_tools.RatingTagSyncJob().scan(library["context"]).auto_fixed == 2
    assert fork_tools.RatingTagSyncJob().estimate_scope(library["context"]) == 5
