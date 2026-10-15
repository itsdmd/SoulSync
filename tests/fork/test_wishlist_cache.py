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
