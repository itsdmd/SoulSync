"""Lyrics Translator: lyrics files and embedded lyrics the library already has."""

from __future__ import annotations

import os
import sqlite3

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, USLT

from core.fork import filler_cache, lyrics, lyrics_retro, ollama
from core.repair_jobs import fork_tools
from core.repair_jobs.base import JobContext

_MINIMAL_FLAC = (
    b"fLaC" + b"\x80\x00\x00\x22" + b"\x00\x10\x00\x10" + b"\x00\x00\x00\x00\x00\x00"
    + b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)
LRC = "[00:01.00]一群嗜血的螞蟻\n[00:05.50]Hello\n"
TRANSLATED = "[re:SoulSync LLM translation]\n[00:01.00]A swarm of ants\n[00:05.50]Hello"


def _reply(*texts):
    return {"lines": [{"id": i, "translation": t} for i, t in enumerate(texts)]}


def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_MINIMAL_FLAC)
    audio = FLAC(str(path))
    audio["title"] = ["T"]
    for key, value in tags.items():
        audio[key] = [value]
    audio.save()
    return str(path)


class _DB:
    def __init__(self, path, files):
        self.path = str(path)
        conn = self._get_connection()
        conn.executescript(
            "CREATE TABLE artists (id TEXT PRIMARY KEY, name TEXT);"
            "CREATE TABLE tracks (id TEXT PRIMARY KEY, artist_id TEXT, title TEXT, file_path TEXT);"
            "INSERT INTO artists VALUES ('ar', 'Artist');")
        conn.executemany("INSERT INTO tracks VALUES (?, 'ar', ?, ?)",
                         [(str(n), os.path.basename(f), f) for n, f in enumerate(files)])
        conn.commit()
        conn.close()

    def _get_connection(self):
        return sqlite3.connect(self.path)


@pytest.fixture
def scan(tmp_path, fork_env, monkeypatch):
    monkeypatch.setattr(filler_cache, "cache_path", lambda: str(tmp_path / "cache.db"))

    def run(files, **settings):
        fork_env.set(f"repair.jobs.{lyrics_retro.JOB_ID}.settings", settings)
        log = []
        context = JobContext(db=_DB(tmp_path / f"lib{len(os.listdir(tmp_path))}.db", files),
                             transfer_folder=str(tmp_path), config_manager=fork_env,
                             report_progress=lambda **kw: log.append(kw.get("log_line")))
        result = fork_tools.LyricsTranslateJob().scan(context)
        result.log = [line for line in log if line]
        return result

    return run


def test_lyrics_file_is_translated_separately_and_embedded(tmp_path, llm, scan):
    audio = _flac(tmp_path / "a" / "song.flac", lyrics=LRC)
    (tmp_path / "a" / "song.lrc").write_text(LRC, encoding="utf-8")
    llm.replies = [_reply("A swarm of ants")]
    result = scan([audio])
    assert (result.auto_fixed, result.errors) == (1, 0)
    assert (tmp_path / "a" / "song.lrc").read_text(encoding="utf-8").strip() == TRANSLATED
    assert (tmp_path / "a" / "song.original.lrc").read_text(encoding="utf-8") == LRC
    assert FLAC(audio)["lyrics"] == [TRANSLATED] and FLAC(audio)["title"] == ["T"]

    # translated lyrics are recognised by their content, also under another name
    os.rename(tmp_path / "a", tmp_path / "b")
    again = scan([str(tmp_path / "b" / "song.flac")])
    assert (again.auto_fixed, len(llm.calls)) == (0, 1)


def test_embedded_only_lyrics_are_translated_and_the_original_kept(tmp_path, llm, scan):
    audio = _flac(tmp_path / "song.flac", unsyncedlyrics="一群嗜血的螞蟻\nHello")
    path = tmp_path / "song.mp3"
    path.write_bytes((b"\xff\xfb\x90\x00" + b"\x00" * 413) * 8)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["T"]))
    tags.add(USLT(encoding=3, lang="zho", desc="", text="夜的第七章"))
    tags.save(str(path))
    llm.replies = [_reply("A swarm of ants"), _reply("Chapter seven of the night")]
    assert scan([audio, str(path)]).auto_fixed == 2
    assert FLAC(audio)["unsyncedlyrics"] == ["A swarm of ants\nHello\n[SoulSync LLM translation]"]
    assert "lyrics" not in FLAC(audio)
    assert (tmp_path / "song.original.txt").read_text(encoding="utf-8") == "一群嗜血的螞蟻\nHello\n"
    frame = ID3(str(path)).getall("USLT")[0]
    assert (frame.lang, frame.text) == ("zho", "Chapter seven of the night\n[SoulSync LLM translation]")
    assert len(llm.calls) == 2                               # one request per lyrics text


def test_inline_mode_and_embedded_lyrics_switched_off(tmp_path, llm, scan):
    audio = _flac(tmp_path / "song.flac", lyrics="夜的第七章")
    other = _flac(tmp_path / "other.flac", lyrics="夜的第七章")
    (tmp_path / "song.lrc").write_text(LRC, encoding="utf-8")
    llm.replies = [_reply("A swarm of ants")]
    assert scan([audio, other], mode="inline", embedded_lyrics=False).auto_fixed == 1
    assert (tmp_path / "song.lrc").read_text(encoding="utf-8").splitlines()[1:3] == [
        "[00:01.00]一群嗜血的螞蟻", "[00:01.00]A swarm of ants"]
    assert not (tmp_path / "song.original.lrc").exists()
    assert FLAC(audio)["lyrics"] == ["夜的第七章"] and FLAC(other)["lyrics"] == ["夜的第七章"]


def test_same_lyrics_are_one_request_and_tags_follow_a_translated_file(tmp_path, llm, scan):
    single = _flac(tmp_path / "single" / "song.flac", lyrics=LRC)
    album = _flac(tmp_path / "album" / "01 song.flac", lyrics=LRC)
    done = _flac(tmp_path / "done" / "song.flac", lyrics=LRC)
    (tmp_path / "done" / "song.lrc").write_text(TRANSLATED + "\n", encoding="utf-8")
    llm.replies = [_reply("A swarm of ants")]
    assert scan([single, album, done]).auto_fixed == 3
    assert len(llm.calls) == 1
    assert FLAC(single)["lyrics"] == FLAC(album)["lyrics"] == FLAC(done)["lyrics"] == [TRANSLATED]


def test_dry_run_calls_no_model_and_latin_lyrics_are_skipped(tmp_path, llm, scan):
    audio = _flac(tmp_path / "song.flac", lyrics=LRC)
    latin = _flac(tmp_path / "latin.flac", lyrics="Hello there")
    result = scan([audio, latin], dry_run=True)
    assert (result.auto_fixed, llm.calls) == (0, [])
    assert "Would translate embedded lyrics — song.flac — Artist" in result.log
    assert FLAC(audio)["lyrics"] == [LRC]


def test_unusable_answer_is_not_asked_again_and_a_dead_model_stops_the_run(tmp_path, llm, scan):
    bad = _flac(tmp_path / "bad.flac", lyrics="夜的第七章")
    good = _flac(tmp_path / "good.flac", lyrics=LRC)
    llm.replies = [_reply("夜的第七章"), _reply("A swarm of ants")]       # the first answer is no translation
    assert scan([bad, good]).auto_fixed == 1
    assert scan([bad, good]).auto_fixed == 0 and len(llm.calls) == 2
    assert FLAC(bad)["lyrics"] == ["夜的第七章"]

    files = [_flac(tmp_path / f"n{n}.flac", lyrics=f"夜的第{n}章 一") for n in range(5)]
    llm.replies = [ollama.OllamaError("down")] * 5
    result = scan(files)
    assert result.errors == 3 and "model failed" in result.stopped_early
    assert lyrics.needs_translation(lyrics_retro.read_embedded(files[0]))
