"""The filler tools' scan cache: unchanged files are not read twice, a service
is not asked twice, and the whole cache is dropped when its lifetime ran out."""

from __future__ import annotations

import os
import sqlite3
import sys
import time
import types
from unittest.mock import MagicMock

from core.fork import filler_cache
from core.repair_jobs.base import JobContext


def _age(job: str, days: float) -> None:
    conn = sqlite3.connect(filler_cache.cache_path())
    conn.execute("UPDATE created SET at = ? WHERE job = ?", (time.time() - days * 86400, job))
    conn.commit()
    conn.close()


def test_a_file_is_read_again_only_when_it_changed(tmp_path):
    song = tmp_path / "song.flac"
    song.write_bytes(b"one")
    reads = []

    def read(path):
        reads.append(path)
        return {"track_gain": f"-{len(reads)}.00 dB"}

    with filler_cache.session("job"):
        assert filler_cache.file_value(str(song), read) == {"track_gain": "-1.00 dB"}
    with filler_cache.session("job"):
        assert filler_cache.file_value(str(song), read) == {"track_gain": "-1.00 dB"}
        assert len(reads) == 1
        song.write_bytes(b"changed")
        assert filler_cache.file_value(str(song), read) == {"track_gain": "-2.00 dB"}
    with filler_cache.session("other job"):
        filler_cache.file_value(str(song), read)
    assert len(reads) == 3
    assert filler_cache.file_value(str(song), read) and len(reads) == 4     # no scan running: no cache


def test_the_cache_is_dropped_after_its_lifetime_and_when_turned_off(tmp_path):
    song = tmp_path / "song.flac"
    song.write_bytes(b"one")
    reads = []

    def read(path):
        reads.append(path)
        return True

    for _scan in range(2):
        with filler_cache.session("job", 7):
            filler_cache.file_value(str(song), read)
            filler_cache.lookup("svc", read, "album")
    assert len(reads) == 2
    _age("job", 6)
    with filler_cache.session("job", 7):
        filler_cache.file_value(str(song), read)
    assert len(reads) == 2
    _age("job", 8)
    with filler_cache.session("job", 7):                    # expired: a full run that fills a new cache
        filler_cache.file_value(str(song), read)
        filler_cache.lookup("svc", read, "album")
    with filler_cache.session("job", 7):
        filler_cache.file_value(str(song), read)
    assert len(reads) == 4
    with filler_cache.session("job", 0):                    # off: nothing read from it, nothing kept
        filler_cache.file_value(str(song), read)
    with filler_cache.session("job", "not a number"):       # falls back to the default week
        filler_cache.file_value(str(song), read)
    assert len(reads) == 6


def test_nothing_found_is_kept_only_when_the_service_later_answered():
    asked = []
    answers = {"a": None, "b": "http://art/b", "c": None}

    def fetch(name):
        asked.append(name)
        return answers[name]

    with filler_cache.session("job"):
        for name in "abc":
            filler_cache.lookup("svc", fetch, name)
    with filler_cache.session("job"):
        assert [filler_cache.lookup("svc", fetch, name) for name in "abc"] == [None, "http://art/b", None]
    # a and b come from the cache; c was the last answer of its scan, and an
    # empty one, so the service may have been down: it is asked again
    assert asked == ["a", "b", "c", "c"]


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def _get_connection(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = self.rows
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn


def _context(fork_env, rows, tmp_path):
    return JobContext(db=_DB(rows), transfer_folder=str(tmp_path), config_manager=fork_env,
                      create_finding=MagicMock(return_value=True))


def test_replaygain_filler_reads_each_file_once(fork_env, tmp_path, monkeypatch):
    from core.repair_jobs.replaygain_filler import ReplayGainFillerJob

    tagged, bare = tmp_path / "tagged.flac", tmp_path / "bare.flac"
    tagged.write_bytes(b"x")
    bare.write_bytes(b"y")
    reads = []

    def read(path):
        reads.append(os.path.basename(path))
        return {"track_gain": "-3.10 dB" if "tagged" in path else None}

    monkeypatch.setitem(sys.modules, "core.replaygain",
                        types.SimpleNamespace(is_ffmpeg_available=lambda: True, read_replaygain_tags=read))
    rows = [(1, "Tagged", "A", str(tagged)), (2, "Bare", "A", str(bare))]
    assert ReplayGainFillerJob.default_settings["cache_days"] == 7

    first = ReplayGainFillerJob().scan(_context(fork_env, rows, tmp_path))
    second = ReplayGainFillerJob().scan(_context(fork_env, rows, tmp_path))
    assert sorted(reads) == ["bare.flac", "tagged.flac"]
    assert (first.findings_created, second.findings_created) == (1, 1)      # the same verdict from the cache

    fork_env.set("repair.jobs.replaygain_filler.settings.cache_days", 0)
    ReplayGainFillerJob().scan(_context(fork_env, rows, tmp_path))
    assert len(reads) == 4


def test_lyrics_filler_asks_lrclib_once_per_track(fork_env, tmp_path, monkeypatch):
    import core.lyrics_client as lyrics_module
    from core.repair_jobs.missing_lyrics import MissingLyricsJob

    asked = []

    def has_remote_lyrics(title, artist, album, duration):
        asked.append(title)
        return title == "Sung"

    monkeypatch.setattr(lyrics_module, "lyrics_client",
                        types.SimpleNamespace(api=object(), has_remote_lyrics=has_remote_lyrics))
    rows = []
    for number, title in enumerate(("Instrumental", "Sung"), 1):
        path = tmp_path / f"{title}.flac"
        path.write_bytes(b"x")
        rows.append((number, title, "A", "Album", str(path), 200000))

    for _scan in range(2):
        result = MissingLyricsJob().scan(_context(fork_env, rows, tmp_path))
        assert result.findings_created == 1
    assert asked == ["Instrumental", "Sung"]


def test_cover_art_filler_caches_the_picture_check_and_the_lookups(fork_env, tmp_path, monkeypatch):
    import core.repair_jobs.missing_cover_art as cover

    song = tmp_path / "song.flac"
    song.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(cover, "_upstream_file_has_embedded_art", lambda path: calls.append("picture") or False)
    monkeypatch.setattr(cover, "_upstream_try_source",
                        lambda self, source, *rest: calls.append(source) or "http://art/cover.jpg")
    monkeypatch.setattr(cover, "_upstream_find_artist_art", lambda self, *rest: calls.append("artist") or None)

    job = cover.MissingCoverArtJob()
    with filler_cache.session(job.job_id):
        for _scan in range(2):
            assert cover.file_has_embedded_art(str(song)) is False
            assert job._try_source("deezer", "1", "Album", "A") == "http://art/cover.jpg"
            assert job._find_artist_art("A", ["deezer"]) is None
    assert calls == ["picture", "deezer", "artist", "artist"]       # an empty answer alone is not trusted
    assert cover.MissingCoverArtJob.default_settings["cache_days"] == 7
