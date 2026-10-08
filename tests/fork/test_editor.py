"""Tag Editor backend: folder index, tags and cover by hand, renaming; and
the verified move out of the import folder."""

import base64
import os
import sqlite3

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3

from core.fork import album_tagging, editor, hooks, import_move, library_index, store, tags

_MINIMAL_FLAC = (
    b"fLaC" + b"\x80\x00\x00\x22" + b"\x00\x10\x00\x10" + b"\x00\x00\x00\x00\x00\x00"
    + b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _flac(path, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(_MINIMAL_FLAC)
    audio = FLAC(path)
    for key, value in fields.items():
        audio[key] = value if isinstance(value, list) else [str(value)]
    audio.save()
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    real = os.path.realpath(str(root))
    monkeypatch.setattr(album_tagging, "allowed_roots", lambda: [real])
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())
    library_index._ready.clear()
    album = os.path.join(real, "Artist", "Album")
    _flac(os.path.join(album, "01 - One.flac"), title="One", artist=["A", "B"], album="Album", tracknumber=1,
          tracktotal=9)
    _flac(os.path.join(album, "02 - Two.flac"), title="Two", artist="A", album="Album", tracknumber=2)
    for name in ("01 - One.lrc", "01 - One.original.lrc", "cover.jpg", ".hidden"):
        with open(os.path.join(album, name), "wb") as fh:
            fh.write(b"x")
    os.makedirs(os.path.join(real, "Artist", "Empty"))
    return real


# ── the index ───────────────────────────────────────────────────────────

def test_tree_and_listing_come_from_the_index_and_follow_the_disk(lib):
    assert [d["name"] for d in library_index.children(lib)] == ["Artist"]
    kids = library_index.children(os.path.join(lib, "Artist"))
    assert [(d["name"], d["has_children"]) for d in kids] == [("Album", True), ("Empty", True)]  # not looked into yet
    album = os.path.join(lib, "Artist", "Album")
    shown = library_index.listing(album)
    assert [f["name"] for f in shown["files"]] == ["01 - One.flac", "01 - One.lrc", "01 - One.original.lrc",
                                                  "02 - Two.flac", "cover.jpg"]
    assert [(f["kind"], f["title"], f["artist"], f["track"]) for f in shown["files"]][0] == ("audio", "One", "A; B", "1/9")
    assert {f["kind"] for f in shown["files"]} == {"audio", "lyrics", "image"}
    library_index.listing(os.path.join(lib, "Artist", "Empty"))
    assert [(d["name"], d["has_children"]) for d in library_index.children(os.path.join(lib, "Artist"))] == [
        ("Album", False), ("Empty", False)]

    # a new folder is noticed through the parent's changed mtime, a removed one disappears
    os.makedirs(os.path.join(lib, "Artist", "New"))
    os.rmdir(os.path.join(lib, "Artist", "Empty"))
    os.utime(os.path.join(lib, "Artist"), (1, 1))
    assert [d["name"] for d in library_index.children(os.path.join(lib, "Artist"))] == ["Album", "New"]


def test_tags_are_read_once_until_the_file_changes(lib, monkeypatch):
    album = os.path.join(lib, "Artist", "Album")
    reads = []
    real = editor.read_basic
    monkeypatch.setattr(editor, "read_basic", lambda path: reads.append(path) or real(path))
    library_index.listing(album)
    library_index.listing(album)
    assert len(reads) == 2
    path = os.path.join(album, "02 - Two.flac")
    editor.save_tags([path], {"title": "Second"})
    assert [f["title"] for f in library_index.listing(album)["files"] if f["kind"] == "audio"] == ["One", "Second"]
    assert len(reads) == 3


def test_search_reads_the_index_everywhere_or_under_one_folder(lib):
    _flac(os.path.join(lib, "Other", "Live", "03 - One More.flac"), title="Encore")
    assert library_index.search("one")["files"] == []                 # nothing indexed yet
    library_index.start_scan([lib], background=False)
    state = library_index.status()
    assert not state["running"] and state["last_full_scan"] > 0 and state["dirs"] >= 5
    found = library_index.search("ONE")
    assert [f["name"] for f in found["files"]] == ["01 - One.flac", "01 - One.lrc", "01 - One.original.lrc",
                                                  "03 - One More.flac"]
    assert [f["name"] for f in library_index.search("one", os.path.join(lib, "Other"))["files"]] == ["03 - One More.flac"]
    assert [d["name"] for d in library_index.search("alb")["dirs"]] == ["Album"]
    # a title is found once its folder has been opened
    assert library_index.search("encore")["files"] == []
    library_index.listing(os.path.join(lib, "Other", "Live"))
    assert [f["name"] for f in library_index.search("encore")["files"]] == ["03 - One More.flac"]
    assert library_index.ensure_started([lib])["running"] is False       # already indexed: no second walk


# ── tags ────────────────────────────────────────────────────────────────

def test_only_changed_tags_are_written_and_values_keep_their_shape(lib):
    one, two = (os.path.join(lib, "Artist", "Album", n) for n in ("01 - One.flac", "02 - Two.flac"))
    assert editor.read_tags(one)["tags"]["artist"] == "A; B" and editor.read_tags(one)["tags"]["tracknumber"] == "1/9"
    out = editor.save_tags([one, two], {"album": "New Album", "genre": "Rock; Pop", "discnumber": "1/2",
                                        "date": ""})
    assert out == {"saved": 2, "errors": []}
    for path, title in ((one, "One"), (two, "Two")):
        audio = FLAC(path)
        assert audio["album"] == ["New Album"] and audio["genre"] == ["Rock", "Pop"]
        assert audio["discnumber"] == ["1"] and audio["disctotal"] == ["2"]
        assert audio["title"] == [title]                               # untouched
    assert FLAC(one)["artist"] == ["A", "B"] and FLAC(one)["tracktotal"] == ["9"]
    editor.save_tags([one], {"artist": "Solo", "tracknumber": "5"})
    assert FLAC(one)["artist"] == ["Solo"] and "tracktotal" not in FLAC(one)
    with pytest.raises(ValueError):
        editor.save_tags([one], {"tracknumber": "five"})
    assert editor.save_tags([one], {}) == {"saved": 0, "errors": []}
    outside = editor.save_tags([os.path.join(os.path.dirname(lib), "x.flac")], {"title": "x"})
    assert outside["saved"] == 0 and len(outside["errors"]) == 1


def test_mp3_tags_and_cover(lib):
    from mutagen.id3 import TIT2

    path = os.path.join(lib, "Artist", "Album", "03.mp3")
    with open(path, "wb") as fh:
        fh.write((b"\xff\xfb\x90\x00" + b"\x00" * 413) * 8)        # eight silent MPEG frames
    id3 = ID3()
    id3.add(TIT2(encoding=3, text=["Old"]))
    id3.save(path)
    out = editor.save_tags([path], {"title": "Three", "artist": "A; B", "tracknumber": "3/9", "comment": "hi"},
                           {"action": "set", "data": base64.b64encode(_JPEG).decode()})
    assert out["errors"] == [] and out["saved"] == 1
    read = editor.read_tags(path)
    assert read["tags"]["title"] == "Three" and read["tags"]["artist"] == "A; B"
    assert read["tags"]["tracknumber"] == "3/9" and read["tags"]["comment"] == "hi"
    assert read["cover"]["mime"] == "image/jpeg" and editor.cover_of(path)[0] == _JPEG


def test_every_tag_of_a_file_is_listed_and_the_text_ones_can_be_edited(lib):
    from mutagen.id3 import COMM, PRIV, TBPM, TIT2, TXXX, USLT

    one = os.path.join(lib, "Artist", "Album", "01 - One.flac")
    audio = FLAC(one)
    audio["mood"] = ["Calm"]
    audio["MusicBrainz_TrackId"] = ["abc"]
    audio["performer"] = ["X", "Y"]
    audio.save()
    read = editor.read_tags(one)
    assert read["tags"]["MOOD"] == "Calm" and read["tags"]["MUSICBRAINZ_TRACKID"] == "abc"
    assert read["tags"]["PERFORMER"] == "X; Y" and read["readonly"] == []
    assert "TRACKTOTAL" not in read["tags"] and read["tags"]["title"] == "One"     # shown by its own field
    out = editor.save_tags([one], {"MOOD": "Dark; Slow", "PERFORMER": "Z; W", "MUSICBRAINZ_TRACKID": "",
                                   "isrc": "QM1"})
    assert out == {"saved": 1, "errors": []}
    audio = FLAC(one)
    assert audio["mood"] == ["Dark; Slow"]                    # one value stays one value
    assert audio["performer"] == ["Z", "W"]                   # several stay several
    assert "musicbrainz_trackid" not in audio and audio["isrc"] == ["QM1"] and audio["title"] == ["One"]
    assert editor.save_tags([one], {"BAD=NAME": "x"})["saved"] == 0

    mp3 = os.path.join(lib, "Artist", "Album", "03.mp3")
    with open(mp3, "wb") as fh:
        fh.write((b"\xff\xfb\x90\x00" + b"\x00" * 413) * 8)
    id3 = ID3()
    for frame in (TIT2(encoding=3, text=["T"]), TBPM(encoding=3, text=["120"]),
                  TXXX(encoding=3, desc="Mood", text=["Calm"]), USLT(encoding=3, lang="eng", desc="", text="la\nla"),
                  COMM(encoding=3, lang="eng", desc="", text=["first"]),
                  COMM(encoding=3, lang="eng", desc="note", text=["second"]), PRIV(owner="x", data=b"12345")):
        id3.add(frame)
    id3.save(mp3)
    read = editor.read_tags(mp3)
    assert read["tags"]["BPM"] == "120" and read["tags"]["MOOD"] == "Calm" and read["tags"]["LYRICS"] == "la\nla"
    assert read["tags"]["comment"] == "first" and read["tags"]["COMM:note:eng"] == "second"
    assert sorted(read["readonly"]) == ["COMM:note:eng", "PRIV:x:12345"]
    out = editor.save_tags([mp3], {"BPM": "98", "MOOD": "Dark", "LYRICS": "", "ISRC": "QM2", "CUSTOM": "c",
                                   "comment": "changed"})
    assert out == {"saved": 1, "errors": []}
    id3 = ID3(mp3)
    assert id3["TBPM"].text == ["98"] and id3["TXXX:Mood"].text == ["Dark"] and not id3.getall("USLT")
    assert id3["TSRC"].text == ["QM2"] and id3["TXXX:CUSTOM"].text == ["c"]
    assert sorted(str(c.text[0]) for c in id3.getall("COMM")) == ["changed", "second"]    # the other comment stays
    assert id3.getall("PRIV")


def test_cover_is_set_replaced_and_removed(lib):
    album = os.path.join(lib, "Artist", "Album")
    one, two = os.path.join(album, "01 - One.flac"), os.path.join(album, "02 - Two.flac")
    assert editor.read_tags(one)["cover"] is None
    editor.save_tags([one, two], {}, {"action": "set", "data": base64.b64encode(_PNG).decode(), "folder_file": True})
    assert editor.cover_of(one) == (_PNG, "image/png") and len(FLAC(two).pictures) == 1
    assert open(os.path.join(album, "cover.png"), "rb").read() == _PNG and not os.path.exists(os.path.join(album, "cover.jpg"))
    editor.save_tags([one], {}, {"action": "set", "data": base64.b64encode(_JPEG).decode()})
    assert editor.cover_of(one) == (_JPEG, "image/jpeg") and len(FLAC(one).pictures) == 1
    editor.save_tags([one], {}, {"action": "remove"})
    assert editor.cover_of(one) is None and FLAC(one)["title"] == ["One"]
    with pytest.raises(ValueError):
        editor.save_tags([one], {}, {"action": "set", "data": base64.b64encode(b"GIF89a....").decode()})


# ── renaming ────────────────────────────────────────────────────────────

class _Db:
    def __init__(self, path, rows):
        self.path = path
        conn = self._get_connection()
        conn.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, track_number INTEGER, file_path TEXT)")
        conn.executemany("INSERT INTO tracks (title, file_path) VALUES (?, ?)", rows)
        conn.commit()
        conn.close()

    def _get_connection(self):
        return sqlite3.connect(self.path)

    def paths(self):
        conn = self._get_connection()
        out = [r[0] for r in conn.execute("SELECT file_path FROM tracks ORDER BY id")]
        conn.close()
        return out


def test_renaming_a_track_takes_its_lyrics_along_and_updates_the_library(lib, tmp_path):
    album = os.path.join(lib, "Artist", "Album")
    db = _Db(str(tmp_path / "lib.db"), [("One", os.path.join(album, "01 - One.flac")),
                                        ("Two", os.path.join(album, "02 - Two.flac"))])
    library_index.listing(album)
    out = editor.rename(os.path.join(album, "01 - One.flac"), "01 - Uno.flac", db=db)
    assert sorted(out["sidecars"]) == ["01 - Uno.lrc", "01 - Uno.original.lrc"]
    assert sorted(os.listdir(album)) == [".hidden", "01 - Uno.flac", "01 - Uno.lrc", "01 - Uno.original.lrc",
                                         "02 - Two.flac", "cover.jpg"]
    assert db.paths()[0] == os.path.join(album, "01 - Uno.flac")
    assert "01 - Uno.flac" in [f["name"] for f in library_index.listing(album)["files"]]
    with pytest.raises(FileExistsError):
        editor.rename(os.path.join(album, "01 - Uno.flac"), "02 - Two.flac")
    for bad in ("", "a/b", ".."):
        with pytest.raises(ValueError):
            editor.rename(os.path.join(album, "02 - Two.flac"), bad)
    with pytest.raises(PermissionError):
        editor.rename(lib, "Elsewhere")


def test_renaming_a_folder_moves_everything_that_points_into_it(lib, tmp_path):
    album = os.path.join(lib, "Artist", "Album")
    db = _Db(str(tmp_path / "lib.db"), [("One", os.path.join(album, "01 - One.flac")),
                                        ("Other", os.path.join(lib, "Artist", "Albumen", "x.flac"))])
    store.save_album_folder("spotify", "al1", album)
    library_index.start_scan([lib], background=False)
    new = editor.rename(os.path.join(lib, "Artist"), "Artiste", db=db)["path"]
    assert db.paths() == [os.path.join(new, "Album", "01 - One.flac"), os.path.join(new, "Albumen", "x.flac")]
    assert store.get_album_folder("spotify", "al1")["folder"] == os.path.join(new, "Album")
    assert [d["name"] for d in library_index.children(lib)] == ["Artiste"]
    assert library_index.search("one")["files"] == []                  # the old paths are gone from the index
    library_index.listing(os.path.join(new, "Album"))
    assert library_index.search("one")["files"][0]["path"].startswith(new)


def test_bulk_rename_previews_then_applies_plain_and_regex(lib):
    album = os.path.join(lib, "Artist", "Album")
    names = ["01 - One.flac", "01 - One.lrc", "02 - Two.flac", "cover.jpg"]
    paths = [os.path.join(album, n) for n in names]
    preview = editor.bulk_rename(paths, " - ", ". ")
    assert [(i["old"], i["new"]) for i in preview["items"]] == [
        ("01 - One.flac", "01. One.flac"), ("01 - One.lrc", "01. One.lrc"), ("02 - Two.flac", "02. Two.flac"),
        ("cover.jpg", "cover.jpg")]
    assert preview["changes"] == 3 and preview["renamed"] == 0 and os.path.exists(paths[0])
    # the extension is never matched: "flac" is not found in any name
    assert editor.bulk_rename(paths, "flac", "x")["changes"] == 0

    done = editor.bulk_rename(paths, r"^(\d+) - (.+)$", "$2 ($1)", regex=True, apply=True)
    assert done["renamed"] == 3 and done["problems"] == 0
    assert sorted(n for n in os.listdir(album) if not n.startswith(".")) == [
        "One (01).flac", "One (01).lrc", "One (01).original.lrc", "Two (02).flac", "cover.jpg"]

    clash = editor.bulk_rename([os.path.join(album, "One (01).flac"), os.path.join(album, "Two (02).flac")],
                               r"^.*$", "Same", regex=True)
    assert clash["problems"] == 1 and clash["changes"] == 1
    assert editor.bulk_rename([os.path.join(album, "Two (02).flac")], "two", "TWO", case_sensitive=True)["changes"] == 0
    with pytest.raises(ValueError):
        editor.bulk_rename(paths, "(", "x", regex=True)
    with pytest.raises(ValueError):
        editor.bulk_rename(paths, "", "x")


# ── leaving the import folder ───────────────────────────────────────────

@pytest.fixture
def staging(tmp_path, monkeypatch):
    root = tmp_path / "import"
    (root / "Artist" / "Album").mkdir(parents=True)
    monkeypatch.setattr(import_move, "staging_root", lambda: os.path.realpath(str(root)))
    return root


def test_import_copies_verifies_then_deletes_and_clears_empty_folders(staging, tmp_path):
    from core.imports import file_ops

    src = staging / "Artist" / "Album" / "song.flac"
    src.write_bytes(b"music" * 5000)
    (staging / "Artist" / "Album" / ".DS_Store").write_bytes(b"x")
    keep = staging / "Other"
    keep.mkdir()
    (keep / "notes.txt").write_text("stay")
    dst = tmp_path / "library" / "A" / "B" / "01 - song.flac"
    file_ops.safe_move_file(str(src), str(dst))
    assert dst.read_bytes() == b"music" * 5000 and not src.exists()
    assert sorted(p.name for p in staging.iterdir()) == ["Other"]        # emptied folders gone, the root stays
    assert [p.name for p in dst.parent.iterdir()] == ["01 - song.flac"]  # no temporary file left


def test_a_copy_that_does_not_match_fails_the_import_and_keeps_the_original(staging, tmp_path, monkeypatch):
    from core.imports import file_ops

    src = staging / "Artist" / "Album" / "song.flac"
    src.write_bytes(b"music" * 5000)
    dst = tmp_path / "library" / "song.flac"
    monkeypatch.setattr(import_move, "_digest", lambda path: "not the same")
    with pytest.raises(OSError, match="does not match"):
        file_ops.safe_move_file(str(src), str(dst))
    assert src.read_bytes() == b"music" * 5000 and not dst.exists()
    assert list(dst.parent.iterdir()) == []


def test_files_outside_the_import_folder_and_the_switch_off_use_the_plain_move(staging, tmp_path, fork_env):
    elsewhere = tmp_path / "downloads" / "song.flac"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"x")
    assert hooks.import_copy_verify(str(elsewhere), str(tmp_path / "out.flac")) is False and elsewhere.exists()
    inside = staging / "song.flac"
    inside.write_bytes(b"x")
    fork_env.set("fork.import.copy_verify", False)
    assert hooks.import_copy_verify(str(inside), str(tmp_path / "out.flac")) is False and inside.exists()


def test_moving_takes_lyrics_along_updates_paths_and_overwrites_nothing(lib, tmp_path):
    album = os.path.join(lib, "Artist", "Album")
    db = _Db(str(tmp_path / "lib.db"), [("One", os.path.join(album, "01 - One.flac")),
                                        ("Two", os.path.join(album, "02 - Two.flac"))])
    store.save_album_folder("spotify", "al1", album)
    library_index.start_scan([lib], background=False)
    other = os.path.join(lib, "Artist", "Empty")
    out = editor.move([os.path.join(album, "01 - One.flac")], other, db=db)
    assert out["errors"] == [] and out["moved"] == [os.path.join(other, "01 - One.flac")]
    assert sorted(os.listdir(other)) == ["01 - One.flac", "01 - One.lrc", "01 - One.original.lrc"]
    assert db.paths()[0] == os.path.join(other, "01 - One.flac")

    # a folder: everything pointing into it follows, the tree shows it in its new place
    os.makedirs(os.path.join(lib, "Elsewhere"))
    out = editor.move([album], os.path.join(lib, "Elsewhere"), db=db)
    new_album = os.path.join(lib, "Elsewhere", "Album")
    assert out["moved"] == [new_album] and db.paths()[1] == os.path.join(new_album, "02 - Two.flac")
    assert store.get_album_folder("spotify", "al1")["folder"] == new_album
    assert [d["name"] for d in library_index.children(os.path.join(lib, "Elsewhere"))] == ["Album"]
    assert [d["name"] for d in library_index.children(os.path.join(lib, "Artist"))] == ["Empty"]

    # refused: onto an existing name, a library folder; the same place is a no-op
    os.makedirs(os.path.join(lib, "Artist", "Album"))
    bad = editor.move([new_album, lib], os.path.join(lib, "Artist"), db=db)
    assert bad["moved"] == [] and len(bad["errors"]) == 2 and os.path.isdir(new_album)
    assert editor.move([os.path.join(other, "01 - One.flac")], other)["moved"] == []
    with pytest.raises((PermissionError, FileNotFoundError)):
        editor.move([os.path.join(other, "01 - One.flac")], str(tmp_path))
    inside = editor.move([os.path.join(lib, "Artist")], os.path.join(lib, "Artist", "Empty"))
    assert inside["moved"] == [] and "into itself" in inside["errors"][0]


def test_copy_keeps_both_and_delete_takes_lyrics_and_library_rows(lib, tmp_path):
    album = os.path.join(lib, "Artist", "Album")
    one = os.path.join(album, "01 - One.flac")
    out = editor.copy([one], album)                               # pasted next to the original
    assert out["errors"] == [] and [os.path.basename(p) for p in out["copied"]] == ["01 - One (copy).flac"]
    assert os.path.exists(os.path.join(album, "01 - One (copy).lrc")) and os.path.exists(one)
    assert os.path.basename(editor.copy([one], album)["copied"][0]) == "01 - One (copy 2).flac"
    empty = os.path.join(lib, "Artist", "Empty")
    out = editor.copy([album], empty)
    assert out["copied"] == [os.path.join(empty, "Album")] and len(os.listdir(os.path.join(empty, "Album"))) >= 6
    assert "into itself" in editor.copy([os.path.join(lib, "Artist")], empty)["errors"][0]

    db = _Db(str(tmp_path / "lib.db"), [("One", one), ("Two", os.path.join(album, "02 - Two.flac")),
                                        ("Far", os.path.join(lib, "Other", "x.flac"))])
    assert editor.describe([one, empty]) == {"files": 1 + len(os.listdir(os.path.join(empty, "Album"))),
                                             "folders": 2, "bytes": editor.describe([one, empty])["bytes"]}
    library_index.listing(album)
    out = editor.delete([one], db=db)
    assert out["errors"] == [] and not os.path.exists(one)
    assert not os.path.exists(os.path.join(album, "01 - One.lrc")) and not os.path.exists(
        os.path.join(album, "01 - One.original.lrc"))
    assert os.path.exists(os.path.join(album, "01 - One (copy).lrc"))      # another track's lyrics stay
    assert len(db.paths()) == 2
    out = editor.delete([album, os.path.join(album, "02 - Two.flac"), lib], db=db)
    assert out["deleted"] == [album] and len(out["errors"]) == 1 and os.path.isdir(lib)
    assert db.paths() == [os.path.join(lib, "Other", "x.flac")]
    assert [d["name"] for d in library_index.children(os.path.join(lib, "Artist"))] == ["Empty"]


def test_any_import_entry_can_be_dismissed(tmp_path, monkeypatch):
    from core.fork import import_inbox

    root = tmp_path / "import"
    (root / "Drop").mkdir(parents=True)
    monkeypatch.setattr(import_move, "staging_root", lambda: os.path.realpath(str(root)))

    class Db:
        def _get_connection(self):
            return sqlite3.connect(str(tmp_path / "h.db"))

    conn = Db()._get_connection()
    conn.execute("CREATE TABLE auto_import_history (id INTEGER PRIMARY KEY AUTOINCREMENT, folder_name TEXT NOT NULL,"
                 " folder_path TEXT NOT NULL, folder_hash TEXT, status TEXT NOT NULL DEFAULT 'scanning',"
                 " total_files INTEGER DEFAULT 0, updated_at TIMESTAMP, processed_at TIMESTAMP)")
    conn.execute("INSERT INTO auto_import_history (folder_name, folder_path, status) VALUES ('Old', '/x', 'failed')")
    conn.commit()
    conn.close()
    out = import_inbox.dismiss(Db(), [
        {"history_id": 1, "folder_name": "Old"},                                             # a failed one
        {"key": "h1", "folder_name": "Drop", "folder_path": str(root / "Drop"), "file_count": 3},   # still waiting
        {"key": "h2", "folder_name": "Else", "folder_path": str(tmp_path / "elsewhere")},    # not ours to record
    ])
    assert out["dismissed"] == 2 and len(out["errors"]) == 1
    conn = Db()._get_connection()
    rows = conn.execute("SELECT folder_name, folder_hash, status, total_files FROM auto_import_history ORDER BY id").fetchall()
    conn.close()
    assert rows == [("Old", None, "rejected", 0), ("Drop", "h1", "rejected", 3)]


def test_manual_import_writes_the_confirmed_tags_then_files_the_release(tmp_path, monkeypatch):
    from core.fork import manual_import

    root = tmp_path / "import"
    monkeypatch.setattr(import_move, "staging_root", lambda: os.path.realpath(str(root)))
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())
    drop = os.path.join(os.path.realpath(str(root)), "Drop")
    b = _flac(os.path.join(drop, "b.flac"), title="Bee", tracknumber=2)
    a = _flac(os.path.join(drop, "a.flac"), title="Ay", artist="Someone", album="Rough", tracknumber=1)

    loaded = manual_import.load([b, a, str(tmp_path / "outside.flac")])
    assert [f["name"] for f in loaded["files"]] == ["a.flac", "b.flac"] and len(loaded["errors"]) == 1
    assert loaded["files"][0]["tags"]["album"] == "Rough"

    seen = {}

    def process(data):
        seen.update(data)
        return {"success": True, "processed": len(data["matches"]), "total": len(data["matches"]), "errors": []}, 200

    files = [{"path": a, "tags": {"title": "Ay!", "artist": "Me; You", "albumartist": "Me", "album": "Finished",
                                 "date": "2024", "tracknumber": "1/2", "genre": "Folk"}, "length": 12.5},
             {"path": b, "tags": {"title": "Bee", "artist": "Me", "albumartist": "Me", "album": "Finished",
                                 "date": "2024", "tracknumber": "2/2"}}]
    cover = {"action": "set", "data": base64.b64encode(_PNG).decode()}
    out = manual_import.run(files, cover, "", process)
    assert out == {"processed": 2, "total": 2, "errors": [], "album": "Finished", "artist": "Me"}
    # the files carry what the user typed, cover included, before they are filed
    audio = FLAC(a)
    assert audio["title"] == ["Ay!"] and audio["artist"] == ["Me", "You"] and audio["album"] == ["Finished"]
    assert audio["tracktotal"] == ["2"] and len(audio.pictures) == 1 and len(FLAC(b).pictures) == 1
    # and the pipeline is handed a release made of those tags, to rename only
    assert seen["rename_only"] is True and seen["album"]["name"] == "Finished" and seen["album"]["album_type"] == "album"
    assert seen["album"]["artists"] == [{"name": "Me"}] and seen["album"]["total_tracks"] == 2
    assert [(m["track"]["name"], m["track"]["track_number"], [x["name"] for x in m["track"]["artists"]])
            for m in seen["matches"]] == [("Ay!", 1, ["Me", "You"]), ("Bee", 2, ["Me"])]
    assert seen["matches"][0]["track"]["duration_ms"] == 12500

    # one loose file without an album is a single named after its title
    single = manual_import.build_release([{"path": a, "tags": {"title": "Solo", "artist": "Me"}}])
    assert (single["album"]["name"], single["album"]["album_type"], single["album"]["artist"]) == ("Solo", "single", "Me")
    assert manual_import.build_release([{"path": a, "tags": {"title": "S", "artist": "M"}}], "EP")["album"]["album_type"] == "ep"
    # a form with a hole in it stops before anything is written or moved
    for bad in ({"title": "", "artist": "Me", "album": "X"}, {"title": "T", "album": "X"}):
        seen.clear()
        with pytest.raises(ValueError):
            manual_import.run([{"path": b, "tags": bad}], None, "", process)
        assert seen == {} and FLAC(b)["title"] == ["Bee"]
    with pytest.raises(PermissionError):
        manual_import.run([{"path": str(tmp_path / "outside.flac"), "tags": {"title": "T", "artist": "A"}}], None, "", process)
    with pytest.raises(ValueError, match="did not run"):
        manual_import.run([{"path": b, "tags": {"title": "T", "artist": "A"}}], None, "",
                          lambda data: ({"success": False}, 503))
