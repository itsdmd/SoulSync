"""Applying a changed translation to files already in the library, and the
details pop-up data."""

import os
import sqlite3

import pytest
from mutagen.flac import FLAC

from core.fork import album_tagging, retro, store, tags

_MINIMAL_FLAC = (
    b"fLaC" + b"\x80\x00\x00\x22" + b"\x00\x10\x00\x10" + b"\x00\x00\x00\x00\x00\x00"
    + b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)


class FakeDB:
    def __init__(self, path):
        self.path = path
        conn = self._get_connection()
        conn.executescript(
            "CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, artist_id INTEGER, title TEXT, year INTEGER);"
            "CREATE TABLE tracks (id INTEGER PRIMARY KEY, album_id INTEGER, artist_id INTEGER, title TEXT,"
            " track_number INTEGER, duration INTEGER, file_path TEXT);")
        conn.commit()
        conn.close()
        self.ids = {}

    def _get_connection(self):
        return sqlite3.connect(self.path)

    def add(self, artist, album, title, number, file_path):
        conn = self._get_connection()
        aid = self.ids.get(("ar", artist)) or conn.execute("INSERT INTO artists (name) VALUES (?)", (artist,)).lastrowid
        self.ids[("ar", artist)] = aid
        alid = self.ids.get(("al", artist, album)) or conn.execute(
            "INSERT INTO albums (artist_id, title, year) VALUES (?, ?, 2005)", (aid, album)).lastrowid
        self.ids[("al", artist, album)] = alid
        conn.execute("INSERT INTO tracks (album_id, artist_id, title, track_number, duration, file_path)"
                     " VALUES (?, ?, ?, ?, 185000, ?)", (alid, aid, title, number, file_path))
        conn.commit()
        conn.close()


def _flac(path, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(_MINIMAL_FLAC)
    audio = FLAC(path)
    for key, value in fields.items():
        audio[key] = [str(value)]
    audio.save()
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(album_tagging, "allowed_roots", lambda: [os.path.realpath(str(root))])
    monkeypatch.setattr(album_tagging, "_resolve", lambda p: p if p and os.path.isfile(p) else None)
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())
    moved = []
    monkeypatch.setattr(retro, "_update_db_path", lambda old, new: moved.append((old, new)))
    db = FakeDB(str(tmp_path / "lib.db"))
    album_dir = root / "Jay Chou" / "Jay Chou - Chopin of November (十一月的蕭邦)"
    files = {}
    for number, (title, original) in enumerate([("Night Song (夜曲)", "夜曲"), ("Hair Like Snow (髮如雪)", "髮如雪")], 1):
        path = _flac(str(album_dir / f"{number:02d} - {title}.flac"), title=title, artist="Jay Chou",
                     album="Chopin of November (十一月的蕭邦)", soulsync_original_title=original,
                     soulsync_original_album="十一月的蕭邦")
        db.add("Jay Chou", "Chopin of November (十一月的蕭邦)", title, number, path)
        files[original] = path
    # a different title that merely contains the original text
    other = _flac(str(root / "X" / "Y" / "01 - Serenade (小夜曲).flac"), title="Serenade (小夜曲)", album="Y")
    db.add("X", "Y", "Serenade (小夜曲)", 1, other)
    return {"db": db, "root": root, "files": files, "album_dir": album_dir, "other": other, "moved": moved}


def test_dry_run_lists_changes_and_touches_nothing(lib):
    store.save_translation("title", "夜曲", "Nocturne", user_edited=True)
    data = retro.apply_translation(lib["db"], "title", "夜曲", dry_run=True)
    assert [(os.path.basename(f["path"]), f["old"], f["new"], f["rename_to"]) for f in data["files"]] == [
        ("01 - Night Song (夜曲).flac", "Night Song (夜曲)", "Nocturne (夜曲)", "01 - Nocturne (夜曲).flac")]
    assert data["written"] == 0 and data["dry_run"] is True
    assert FLAC(lib["files"]["夜曲"])["title"] == ["Night Song (夜曲)"]
    assert os.path.isfile(lib["files"]["夜曲"]) and lib["moved"] == []


def test_title_is_retagged_and_the_file_and_its_lyrics_renamed(lib):
    old = lib["files"]["夜曲"]
    stem = os.path.splitext(old)[0]
    for suffix in (".lrc", ".original.lrc"):
        with open(stem + suffix, "w", encoding="utf-8") as fh:
            fh.write("x")
    store.save_translation("title", "夜曲", "Nocturne", user_edited=True)
    data = retro.apply_translation(lib["db"], "title", "夜曲")
    new = str(lib["album_dir"] / "01 - Nocturne (夜曲).flac")
    assert data["written"] == 1 and data["renamed"] == 1 and data["errors"] == []
    assert FLAC(new)["title"] == ["Nocturne (夜曲)"] and FLAC(new)["soulsync_original_title"] == ["夜曲"]
    assert not os.path.exists(old)
    assert os.path.isfile(os.path.splitext(new)[0] + ".lrc") and os.path.isfile(os.path.splitext(new)[0] + ".original.lrc")
    assert lib["moved"] == [(old, new)]
    # the look-alike title was not touched
    assert FLAC(lib["other"])["title"] == ["Serenade (小夜曲)"]
    # running it again finds nothing left to do
    lib["db"].add("Jay Chou", "Chopin of November (十一月的蕭邦)", "Nocturne (夜曲)", 1, new)
    assert retro.apply_translation(lib["db"], "title", "夜曲")["files"] == []


def test_tags_only_when_rename_is_off(lib):
    store.save_translation("title", "夜曲", "Nocturne", user_edited=True)
    data = retro.apply_translation(lib["db"], "title", "夜曲", rename=False)
    assert data["written"] == 1 and data["renamed"] == 0
    assert FLAC(lib["files"]["夜曲"])["title"] == ["Nocturne (夜曲)"]


def test_album_is_retagged_on_every_track_and_the_folder_renamed_once(lib):
    store.save_translation("album", "十一月的蕭邦", "November's Chopin", user_edited=True)
    data = retro.apply_translation(lib["db"], "album", "十一月的蕭邦")
    new_dir = lib["root"] / "Jay Chou" / "Jay Chou - November's Chopin (十一月的蕭邦)"
    assert data["written"] == 2 and data["renamed"] == 1 and data["errors"] == []
    assert not lib["album_dir"].exists() and new_dir.is_dir()
    for name in sorted(os.listdir(new_dir)):
        assert FLAC(str(new_dir / name))["album"] == ["November's Chopin (十一月的蕭邦)"]
    assert sorted(new for _old, new in lib["moved"]) == sorted(str(new_dir / n) for n in os.listdir(new_dir))


def test_a_file_without_the_original_tag_is_matched_by_the_embedded_original(lib):
    path = lib["files"]["髮如雪"]
    audio = FLAC(path)
    del audio["soulsync_original_title"]
    audio.save()
    store.save_translation("title", "髮如雪", "Snowy Hair", user_edited=True)
    data = retro.apply_translation(lib["db"], "title", "髮如雪", rename=False)
    assert data["written"] == 1 and FLAC(path)["title"] == ["Snowy Hair (髮如雪)"]
    assert FLAC(path)["soulsync_original_title"] == ["髮如雪"]


def test_version_suffix_from_the_original_is_kept(lib):
    path = _flac(str(lib["root"] / "Jay Chou" / "Live" / "01 - Night Song (夜曲) (Live).flac"),
                 title="Night Song (夜曲) (Live)", soulsync_original_title="夜曲 (Live)")
    lib["db"].add("Jay Chou", "Live", "Night Song (夜曲) (Live)", 1, path)
    store.save_translation("title", "夜曲", "Nocturne", user_edited=True)
    data = retro.apply_translation(lib["db"], "title", "夜曲", rename=False)
    assert {f["new"] for f in data["files"]} == {"Nocturne (夜曲)", "Nocturne (夜曲) (Live)"}


def test_rename_never_overwrites_and_the_tag_still_changes(lib):
    _flac(str(lib["album_dir"] / "01 - Nocturne (夜曲).flac"))
    store.save_translation("title", "夜曲", "Nocturne", user_edited=True)
    data = retro.apply_translation(lib["db"], "title", "夜曲")
    assert data["written"] == 1 and data["renamed"] == 0 and "already exists" in data["errors"][0]
    assert FLAC(lib["files"]["夜曲"])["title"] == ["Nocturne (夜曲)"]


def test_unknown_translation_and_bad_input_are_rejected(lib):
    with pytest.raises(LookupError):
        retro.apply_translation(lib["db"], "title", "沒有這首")
    with pytest.raises(ValueError):
        retro.apply_translation(lib["db"], "artist", "x")


def test_details_for_album_title_and_artist(lib):
    store.save_translation("album", "十一月的蕭邦", "November's Chopin", user_edited=True)
    album = retro.details(lib["db"], "album", "十一月的蕭邦")
    assert album["record"]["display"] == "November's Chopin (十一月的蕭邦)" and album["track_count"] == 2
    assert [a["album"] for a in album["albums"]] == ["Chopin of November (十一月的蕭邦)"]
    assert album["albums"][0]["folder"] == str(lib["album_dir"])
    assert [t["title"] for t in album["albums"][0]["tracks"]] == ["Night Song (夜曲)", "Hair Like Snow (髮如雪)"]
    title = retro.details(lib["db"], "title", "夜曲")
    assert title["record"] is None and title["track_count"] == 2   # 夜曲 and 小夜曲 both contain it: shown, never rewritten
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    artist = retro.details(lib["db"], "artist", "周杰倫")
    assert artist["record"]["replacement"] == "Jay Chou" and artist["library_names"] == ["Jay Chou"]
    assert artist["track_count"] == 2
    assert retro.details(lib["db"], "artist", "Nobody")["albums"] == []


# ── folder target ───────────────────────────────────────────────────────

def test_a_folder_target_reaches_files_the_database_does_not_know(lib):
    # same album sitting in another folder, never scanned into the library
    spare = lib["root"] / "Unscanned" / "Chopin copy"
    path = _flac(str(spare / "01 - Night Song (夜曲).flac"), title="Night Song (夜曲)", soulsync_original_title="夜曲")
    store.save_translation("title", "夜曲", "Nocturne", user_edited=True)
    data = retro.apply_translation(lib["db"], "title", "夜曲", folder=str(spare))
    assert data["written"] == 1 and data["renamed"] == 1 and data["folder"] == str(spare)
    assert FLAC(str(spare / "01 - Nocturne (夜曲).flac"))["title"] == ["Nocturne (夜曲)"]
    assert not os.path.exists(path)
    # the library copy outside the chosen folder was left alone
    assert FLAC(lib["files"]["夜曲"])["title"] == ["Night Song (夜曲)"]


def test_a_folder_target_limits_the_change_to_that_folder(lib):
    store.save_translation("title", "夜曲", "Nocturne", user_edited=True)
    elsewhere = lib["root"] / "X"
    assert retro.apply_translation(lib["db"], "title", "夜曲", folder=str(elsewhere))["files"] == []
    assert FLAC(lib["files"]["夜曲"])["title"] == ["Night Song (夜曲)"]


def test_a_folder_outside_the_library_is_refused(lib, tmp_path):
    store.save_translation("title", "夜曲", "Nocturne", user_edited=True)
    (tmp_path / "elsewhere").mkdir()
    with pytest.raises(PermissionError):
        retro.apply_translation(lib["db"], "title", "夜曲", folder=str(tmp_path / "elsewhere"))


# ── apply all ───────────────────────────────────────────────────────────

def _seed_all():
    store.save_translation("title", "夜曲", "Nocturne", user_edited=True)
    store.save_translation("title", "髮如雪", "Hair Like Snow", model="m")      # already what the file says
    store.save_translation("album", "十一月的蕭邦", "November's Chopin", user_edited=True)
    store.save_translation("title", "不在庫", "Not In Library", model="m")


def test_apply_all_dry_run_reports_and_changes_nothing(lib):
    _seed_all()
    progress = []
    result = retro.run_apply_all(lib["db"], dry_run=True, progress=lambda done, total: progress.append((done, total)))
    assert result["total"] == 4 and result["done"] == 4 and progress[-1] == (4, 4)
    assert result["names_changed"] == 2 and result["files"] == 3 and result["folders"] == 1
    assert result["written"] == 0 and result["renamed"] == 0
    assert {(s["kind"], s["new"]) for s in result["samples"]} == {
        ("title", "Nocturne (夜曲)"), ("album", "November's Chopin (十一月的蕭邦)")}
    assert FLAC(lib["files"]["夜曲"])["title"] == ["Night Song (夜曲)"] and lib["album_dir"].is_dir()


def test_apply_all_updates_titles_then_albums(lib, monkeypatch):
    _seed_all()
    # keep the fake database's paths in step, as the real one is
    def moved(old, new):
        conn = lib["db"]._get_connection()
        conn.execute("UPDATE tracks SET file_path = ? WHERE file_path = ?", (new, old))
        conn.commit()
        conn.close()
    monkeypatch.setattr(retro, "_update_db_path", moved)
    result = retro.run_apply_all(lib["db"])
    assert result["errors"] == [] and result["written"] == 3 and result["renamed"] == 2
    new_dir = lib["root"] / "Jay Chou" / "Jay Chou - November's Chopin (十一月的蕭邦)"
    assert sorted(os.listdir(new_dir)) == ["01 - Nocturne (夜曲).flac", "02 - Hair Like Snow (髮如雪).flac"]
    track = FLAC(str(new_dir / "01 - Nocturne (夜曲).flac"))
    assert track["title"] == ["Nocturne (夜曲)"] and track["album"] == ["November's Chopin (十一月的蕭邦)"]
    # a second run has nothing left to do
    again = retro.run_apply_all(lib["db"])
    assert again["files"] == 0 and again["written"] == 0


def test_apply_all_can_be_limited_by_kind_and_folder(lib):
    _seed_all()
    assert retro.run_apply_all(lib["db"], kind="album", dry_run=True)["total"] == 1
    only = retro.run_apply_all(lib["db"], dry_run=True, folder=str(lib["root"] / "X"))
    assert only["files"] == 0 and only["folder"]
    inside = retro.run_apply_all(lib["db"], dry_run=True, folder=str(lib["album_dir"]))
    assert inside["files"] == 3


def test_background_job_runs_to_completion_and_refuses_a_second_run(lib):
    import time

    _seed_all()
    job = retro.start_apply_all(lambda: lib["db"], dry_run=True)
    assert job["running"] is True
    for _ in range(200):
        status = retro.job_status()
        if not status["running"]:
            break
        time.sleep(0.02)
    assert status["running"] is False and status["error"] is None
    assert status["result"]["files"] == 3 and status["done"] == 4
    with pytest.raises(PermissionError):
        retro.start_apply_all(lambda: lib["db"], folder="/")


def test_tool_is_named_for_what_it_now_does():
    from core.repair_jobs.comma_artist_splitter import CommaArtistSplitterJob

    assert CommaArtistSplitterJob.display_name == "Multiple Artist Formatter"
    assert CommaArtistSplitterJob.job_id == "comma_artist_splitter"   # saved settings and findings carry over
