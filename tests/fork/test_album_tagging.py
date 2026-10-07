"""Album pop-up backend: folder picker, file matching, tag proposal, apply."""

import os

import pytest
from mutagen.flac import FLAC

from core.fork import album_tagging, store

_MINIMAL_FLAC = (
    b"fLaC" + b"\x80\x00\x00\x22" + b"\x00\x10\x00\x10" + b"\x00\x00\x00\x00\x00\x00"
    + b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)

ALBUM = {"id": "al1", "name": "十一月的蕭邦", "release_date": "2005-11-01", "album_type": "album"}
ARTIST = {"id": "ar1", "name": "周杰倫"}
TRACKS = [
    {"id": "t1", "name": "夜曲", "artists": [{"name": "周杰倫"}], "track_number": 1, "disc_number": 1},
    {"id": "t2", "name": "髮如雪", "artists": [{"name": "周杰倫"}], "track_number": 2, "disc_number": 1},
    {"id": "t3", "name": "Duet", "artists": [{"name": "周杰倫"}, {"name": "Lara"}], "track_number": 3, "disc_number": 1},
]


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
def library(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setattr(album_tagging, "allowed_roots", lambda: [os.path.realpath(str(root))])
    monkeypatch.setattr(album_tagging.tags, "_save", lambda audio: audio.save())
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    store.save_translation("album", "十一月的蕭邦", "November's Chopin", model="m")
    store.save_translation("title", "夜曲", "Nocturne", model="m")
    store.save_translation("title", "髮如雪", "Hair Like Snow", model="m")
    return root


def test_browse_lists_folders_and_refuses_to_leave_the_library(library, tmp_path):
    _flac(str(library / "Jay Chou" / "Album" / "01.flac"))
    top = album_tagging.browse(None)
    assert [d["path"] for d in top["dirs"]] == [os.path.realpath(str(library))]
    inside = album_tagging.browse(str(library / "Jay Chou"))
    assert [(d["name"], d["audio"]) for d in inside["dirs"]] == [("Album", 1)]
    assert inside["parent"] == os.path.realpath(str(library))
    assert album_tagging.browse(str(library))["parent"] == ""  # cannot go above a root
    with pytest.raises(PermissionError):
        album_tagging.browse(str(tmp_path / "elsewhere"))
    with pytest.raises(PermissionError):
        album_tagging.browse(str(library / ".." / "elsewhere"))


def test_preview_matches_files_despite_drifted_tags_and_proposes_rule_applied_names(library):
    folder = library / "Jay Chou" / "Chopin"
    # translated earlier, artist flattened to the album artist, numbering intact
    _flac(str(folder / "01 - Night Song (夜曲).flac"), title="Night Song (夜曲)", artist="Jay Chou", tracknumber=1)
    # no usable tags at all: only the file name carries the original
    _flac(str(folder / "02. 髮如雪.flac"))
    # wrong track number in the tag, title is right
    _flac(str(folder / "x.flac"), title="Duet", artist="Jay Chou", tracknumber=9)
    _flac(str(folder / "bonus live jam.flac"), title="Unrelated Jam")
    data = album_tagging.preview(str(folder), ALBUM, ARTIST, TRACKS)
    by_file = {r["rel"]: r for r in data["rows"]}
    assert by_file["01 - Night Song (夜曲).flac"]["track"] == 0
    assert by_file["02. 髮如雪.flac"]["track"] == 1
    assert by_file["x.flac"]["track"] == 2
    assert by_file["bonus live jam.flac"]["track"] is None
    assert data["unmatched_tracks"] == []
    proposed = data["tracks"][0]["proposed"]
    assert proposed == {"title": "Nocturne (夜曲)", "artist": "Jay Chou", "albumartist": "Jay Chou",
                        "album": "November's Chopin (十一月的蕭邦)", "year": "2005",
                        "track_number": 1, "disc_number": 1}
    # the per-track credit is restored, with the rule applied to each name
    assert data["tracks"][2]["proposed"]["artist"] == "Jay Chou, Lara"
    assert data["tracks"][2]["proposed"]["albumartist"] == "Jay Chou"


def test_preview_without_rules_is_the_raw_source(library):
    folder = library / "A"
    _flac(str(folder / "01.flac"), title="夜曲", tracknumber=1)
    data = album_tagging.preview(str(folder), ALBUM, ARTIST, TRACKS, apply_rules=False)
    assert data["tracks"][0]["proposed"]["title"] == "夜曲"
    assert data["tracks"][0]["proposed"]["artist"] == "周杰倫"
    assert data["tracks"][0]["proposed"]["album"] == "十一月的蕭邦"


def test_each_track_is_used_once_and_position_alone_is_enough(library):
    folder = library / "B"
    _flac(str(folder / "a.flac"), title="Completely Different", tracknumber=2)
    _flac(str(folder / "b.flac"), title="髮如雪", tracknumber=2)
    data = album_tagging.preview(str(folder), ALBUM, ARTIST, TRACKS)
    by_file = {r["rel"]: r for r in data["rows"]}
    assert by_file["b.flac"]["track"] == 1          # the title match wins track 2
    assert by_file["a.flac"]["track"] is None       # and the other file is not forced onto another track
    assert set(data["unmatched_tracks"]) == {0, 2}


def test_apply_writes_only_the_edited_values_and_keeps_originals(library):
    folder = library / "C"
    path = _flac(str(folder / "01.flac"), title="old", artist="Jay Chou", genre="Mandopop", tracknumber=7)
    result = album_tagging.apply(str(folder), [{
        "rel": "01.flac", "track": 0,
        "tags": {"title": "Nocturne (夜曲)", "artist": "Jay Chou", "albumartist": "Jay Chou",
                 "album": "My Edited Album Name", "year": "2005", "track_number": "1", "disc_number": 1},
    }], ALBUM, ARTIST, TRACKS, source="spotify")
    assert result["written"] == 1 and result["failed"] == 0 and result["moved"] == 0
    audio = FLAC(path)
    assert audio["title"] == ["Nocturne (夜曲)"] and audio["album"] == ["My Edited Album Name"]
    assert audio["artist"] == ["Jay Chou"] and audio["albumartist"] == ["Jay Chou"]
    assert audio["tracknumber"][0].split("/")[0] == "1"
    assert audio["genre"] == ["Mandopop"]                       # untouched field survives
    assert audio["soulsync_original_title"] == ["夜曲"]          # for the ownership check
    assert audio["soulsync_original_artist"] == ["周杰倫"]
    assert os.path.exists(path)                                   # tags only: not moved


def test_apply_rejects_paths_outside_the_folder(library, tmp_path):
    folder = library / "D"
    _flac(str(folder / "01.flac"))
    outside = _flac(str(tmp_path / "elsewhere" / "z.flac"), title="keep")
    result = album_tagging.apply(str(folder), [
        {"rel": "../../elsewhere/z.flac", "track": 0, "tags": {"title": "hacked"}},
    ], ALBUM, ARTIST, TRACKS)
    assert result["written"] == 0 and result["results"][0]["ok"] is False
    assert FLAC(outside)["title"] == ["keep"]
    with pytest.raises(PermissionError):
        album_tagging.apply(str(tmp_path / "elsewhere"), [{"rel": "z.flac", "tags": {"title": "x"}}],
                            ALBUM, ARTIST, TRACKS)


def test_guess_folder_prefers_the_majority_and_the_parent_of_disc_folders():
    assert album_tagging.guess_folder(["/m/A/1.flac", "/m/A/2.flac", "/m/Other/3.flac"]) == "/m/A"
    assert album_tagging.guess_folder(["/m/A/Disc 1/1.flac", "/m/A/Disc 2/1.flac"]) == "/m/A"
    assert album_tagging.guess_folder([]) == ""


def test_apply_with_rename_moves_the_file_and_its_lyrics_to_the_template_path(library, monkeypatch):
    from core.imports import pipeline

    moved_in_db = []
    monkeypatch.setattr(pipeline, "_update_moved_track_file_path", lambda old, new: moved_in_db.append((old, new)))
    folder = library / "Messy Folder"
    path = _flac(str(folder / "track one.flac"), title="old")
    (folder / "track one.lrc").write_text("[00:01.00]translated\n", encoding="utf-8")
    (folder / "track one.original.lrc").write_text("[00:01.00]原文\n", encoding="utf-8")
    result = album_tagging.apply(str(folder), [{
        "rel": "track one.flac", "track": 0,
        "tags": {"title": "Nocturne (夜曲)", "artist": "Jay Chou", "albumartist": "Jay Chou",
                 "album": "November's Chopin (十一月的蕭邦)", "year": "2005", "track_number": 1, "disc_number": 1},
    }], ALBUM, ARTIST, TRACKS, rename=True)
    assert result["written"] == 1 and result["moved"] == 1
    target = result["results"][0]["moved_to"]
    assert target.startswith(os.path.realpath(str(library)) + os.sep) and target.endswith(".flac")
    assert "Jay Chou" in target and "Nocturne" in target
    assert os.path.isfile(target) and not os.path.exists(path)
    stem = os.path.splitext(target)[0]
    assert os.path.isfile(stem + ".lrc") and os.path.isfile(stem + ".original.lrc")
    assert moved_in_db == [(path, target)]
    assert not os.path.exists(str(folder))  # emptied folder is removed


def test_apply_with_rename_never_overwrites_an_existing_file(library, monkeypatch):
    from core.imports import pipeline

    monkeypatch.setattr(pipeline, "_update_moved_track_file_path", lambda old, new: None)
    folder = library / "Dupes"
    tags_ = {"title": "Same", "artist": "A", "albumartist": "A", "album": "B", "track_number": 1, "disc_number": 1}
    _flac(str(folder / "a.flac"))
    _flac(str(folder / "b.flac"))
    result = album_tagging.apply(str(folder), [
        {"rel": "a.flac", "track": 0, "tags": tags_}, {"rel": "b.flac", "track": 1, "tags": tags_},
    ], ALBUM, ARTIST, TRACKS, rename=True)
    assert result["written"] == 2 and result["moved"] == 1
    assert "already exists" in result["results"][1]["rename_error"]
    assert os.path.isfile(str(folder / "b.flac"))  # the second file stays put, tagged
