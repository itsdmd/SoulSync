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
    real = editor.read_tags
    monkeypatch.setattr(editor, "read_tags", lambda path: reads.append(path) or real(path))
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
                                        "date": "", "nonsense": "x"})
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
