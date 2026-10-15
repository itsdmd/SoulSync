"""The wishlist endpoints reuse their work while the table is unchanged."""

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core.fork import wishlist_cache as wc


@pytest.fixture(autouse=True)
def _active(monkeypatch):
    monkeypatch.setenv("SOULSYNC_FORK_TESTING", "1")
    wc.forget()
    yield
    wc.forget()


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE wishlist_tracks (id INTEGER PRIMARY KEY, profile_id INT, retry_count INT,"
            " last_attempted TEXT, date_added TEXT, quality_profile_id INT)")

    @contextmanager
    def _get_connection(self):
        yield self.conn

    def add(self, n=1):
        for _ in range(n):
            self.conn.execute("INSERT INTO wishlist_tracks (profile_id, retry_count, date_added)"
                              " VALUES (1, 0, '2026-01-01')")

    def runtime(self):
        return SimpleNamespace(get_music_database=lambda: self, profile_id=1)


def test_fingerprint_changes_with_the_table():
    db = _Db()
    db.add(2)
    first = wc.fingerprint(db, 1)
    db.add()
    assert wc.fingerprint(db, 1) != first
    db.conn.execute("UPDATE wishlist_tracks SET retry_count = 3 WHERE id = 1")
    assert wc.fingerprint(db, 1) not in (first,)


def test_slim_rows_keep_only_what_the_page_reads():
    full = {"id": "t1", "wishlist_id": 9, "spotify_track_id": "t1", "retry_count": 2, "failure_reason": "x",
            "last_attempted": "now", "source_info": {"big": "x" * 500}, "track_data": {"junk": 1},
            "spotify_data": {"name": "Song", "duration_ms": 5, "artists": [{"name": "A", "id": "1"}, {"name": "B"}],
                             "album": {"name": "Alb", "album_type": "album", "release_date": "2020",
                                       "images": [{"url": "u1", "w": 1}, {"url": "u2"}]}}}
    slim = wc.slim_track(full)
    assert slim["spotify_data"] == {"name": "Song", "artists": [{"name": "A"}],
                                    "album": {"name": "Alb", "album_type": "album", "images": [{"url": "u1"}]}}
    assert slim["spotify_track_id"] == "t1" and slim["retry_count"] == 2
    assert "source_info" not in slim and "track_data" not in slim


def test_a_string_album_or_artist_survives():
    slim = wc.slim_track({"id": 1, "spotify_data": {"name": "S", "album": "Plain", "artists": ["Solo"]}})
    assert slim["spotify_data"]["album"] == "Plain" and slim["spotify_data"]["artists"] == ["Solo"]


def test_counts_are_reused_and_the_load_skipped(monkeypatch):
    db = _Db()
    db.add(3)
    calls = []

    def upstream_counts(raw):
        calls.append(1)
        return {"singles": 1, "albums": 2, "total": 3}

    def upstream_stats(runtime):
        assert not wc.skip_load() or calls            # a warm run is told to skip the load
        return wc.counts(upstream_counts, ["x"]), 200

    first, _ = wc.stats(upstream_stats, db.runtime())
    second, _ = wc.stats(upstream_stats, db.runtime())
    assert first == second == {"singles": 1, "albums": 2, "total": 3}
    assert len(calls) == 1
    db.add()                                           # a change recomputes
    wc.stats(upstream_stats, db.runtime())
    assert len(calls) == 2


def test_slim_track_list_is_cached_only_on_request(monkeypatch):
    db = _Db()
    db.add()
    calls = []

    def upstream(runtime, category=None, limit=None):
        calls.append(category)
        return {"tracks": [{"id": "a", "spotify_data": {"name": "N"}}], "total": 1}, 200

    monkeypatch.setattr(wc, "_wants_slim", lambda: True)
    one, _ = wc.tracks(upstream, db.runtime(), category="albums")
    two, _ = wc.tracks(upstream, db.runtime(), category="albums")
    assert one is two and one["slim"] is True and len(calls) == 1
    wc.tracks(upstream, db.runtime(), category="singles")      # another category is its own entry
    assert len(calls) == 2
    db.add()
    wc.tracks(upstream, db.runtime(), category="albums")
    assert len(calls) == 3

    monkeypatch.setattr(wc, "_wants_slim", lambda: False)
    plain, _ = wc.tracks(upstream, db.runtime(), category="albums")
    assert "slim" not in plain and len(calls) == 4             # full rows are never held


def test_failed_loads_are_not_cached(monkeypatch):
    db = _Db()
    db.add()
    monkeypatch.setattr(wc, "_wants_slim", lambda: True)
    calls = []

    def upstream(runtime, category=None, limit=None):
        calls.append(1)
        return {"error": "boom"}, 500

    assert wc.tracks(upstream, db.runtime())[1] == 500
    wc.tracks(upstream, db.runtime())
    assert len(calls) == 2


def test_duplicate_scan_is_skipped_while_unchanged():
    db = _Db()
    db.add(2)
    runs = []

    def upstream(database, profile_id=1):
        runs.append(1)
        return 0

    wc.dedupe(upstream, db, 1)
    wc.dedupe(upstream, db, 1)
    assert len(runs) == 1
    db.add()
    wc.dedupe(upstream, db, 1)
    assert len(runs) == 2


# ---- paged view ------------------------------------------------------------------------

import json  # noqa: E402
import threading  # noqa: E402


class _FullDb(_Db):
    def __init__(self):
        super().__init__()
        self.conn.execute("DROP TABLE wishlist_tracks")
        self.conn.execute(
            "CREATE TABLE wishlist_tracks (id INTEGER PRIMARY KEY, profile_id INT, spotify_track_id TEXT,"
            " spotify_data TEXT, failure_reason TEXT, retry_count INT, last_attempted TEXT, date_added TEXT,"
            " quality_profile_id INT)")
        self.n = 0
        self.removed = 0

    def put(self, artist, album, title, *, album_type="album", total=12, retry=0, raw=None, sid=None):
        self.n += 1
        data = raw if raw is not None else json.dumps({
            "name": title, "artists": [{"name": artist}],
            "album": {"name": album, "album_type": album_type, "total_tracks": total,
                      "images": [{"url": f"{album}.jpg"}, {"url": "other.jpg"}]}})
        self.conn.execute(
            "INSERT INTO wishlist_tracks (profile_id, spotify_track_id, spotify_data, retry_count, date_added)"
            " VALUES (1, ?, ?, ?, ?)", (sid or f"id{self.n}", data, retry, f"2026-01-{self.n:02d}"))

    def remove_wishlist_duplicates(self, profile_id=1):
        return 0

    def _get_connection_unused(self):
        return self.conn

    def runtime(self):
        return SimpleNamespace(get_music_database=lambda: self, profile_id=1, tasks_lock=threading.Lock(),
                               download_batches={})


def _library():
    db = _FullDb()
    db.put("Busy", "Big", "t1")
    db.put("Busy", "Big", "t2", retry=4)
    db.put("Busy", "Big", "t3")
    db.put("Solo", "Tiny", "s1", album_type="single", total=1)
    db.put("Zed", "Mid", "z1", album_type=None, total=3)          # no usable album_type: 3 tracks -> single
    db.put("Zed", "Mid", "z2", album_type="", total=0)   # nothing to go on -> album
    db.put("Dup", "D", "d1", sid="same")
    db.put("Dup", "D", "d1 again", sid="same")   # same track id twice: counted once
    db.put("Broken", "B", "b", raw="{not json")
    return db


def test_index_counts_match_the_classifier_and_skip_duplicates():
    db = _library()
    counts = wc.index_for(db, 1).counts
    assert counts == {"singles": 2, "albums": 5, "total": 7}


def test_page_is_ordered_and_sliced_by_artist():
    db = _library()
    one = wc.page(db.runtime(), offset=0, limit=2)
    assert one["artists"] == ["Busy", "Dup"] or one["artists"][0] == "Busy"
    assert one["matched_artists"] == 4 and one["has_more"] is True
    rest = wc.page(db.runtime(), offset=2, limit=2)
    assert rest["has_more"] is False
    names = one["artists"] + rest["artists"]
    assert sorted(names) == ["Busy", "Dup", "Solo", "Zed"]
    busy = [r for r in one["albums"] if r["spotify_data"]["artists"][0]["name"] == "Busy"]
    assert len(busy) == 3
    row = busy[0]["spotify_data"]
    assert row["album"]["images"] == [{"url": "Big.jpg"}] and row["album"]["name"] == "Big"
    assert one["failing_total"] == 1


def test_page_filters_by_artist_or_album_and_failing():
    db = _library()
    by_album = wc.page(db.runtime(), q="tiny")
    assert by_album["artists"] == ["Solo"]
    by_artist = wc.page(db.runtime(), q="ZED")
    assert by_artist["artists"] == ["Zed"] and len(by_artist["singles"]) == 1 and len(by_artist["albums"]) == 1
    failing = wc.page(db.runtime(), failing=True)
    assert failing["artists"] == ["Busy"]


def test_page_sorts_by_name_and_failing():
    db = _library()
    assert wc.page(db.runtime(), sort="name")["artists"] == ["Busy", "Dup", "Solo", "Zed"]
    assert wc.page(db.runtime(), sort="failing")["artists"][0] == "Busy"


def test_failing_ids_cover_the_whole_wishlist():
    db = _library()
    db.put("Late", "L", "l1", retry=5, sid="late-1")
    assert wc.failing_track_ids(db.runtime()) == ["id2", "late-1"]


def test_index_is_rebuilt_when_the_wishlist_changes():
    db = _library()
    before = wc.page(db.runtime())["counts"]["total"]
    db.put("New", "N", "n1")
    assert wc.page(db.runtime())["counts"]["total"] == before + 1


def test_stats_come_from_the_index_without_loading_rows():
    db = _library()
    loads = []

    def upstream(runtime):
        loads.append(wc.skip_load())
        return wc.counts(lambda raw: {"singles": 99, "albums": 99, "total": 198}, []), 200

    payload, _ = wc.stats(upstream, db.runtime())
    assert payload == {"singles": 2, "albums": 5, "total": 7} and loads == [True]
