"""Tracks of one album must agree on the id a media server groups them by."""

import os

import pytest
from mutagen.flac import FLAC

from core.fork import album_identity, hooks, tags

_MINIMAL_FLAC = (
    b"fLaC" + b"\x80\x00\x00\x22" + b"\x00\x10\x00\x10" + b"\x00\x00\x00\x00\x00\x00"
    + b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)
MBID, RGID = "4b2c4b2b-4994-4b9f-8960-a553700d7ac4", "91f803c3-d487-4391-89af-2ce972f5a09e"


@pytest.fixture(autouse=True)
def plain_save(monkeypatch):
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())


def _flac(path, album="Outside It Is Growing Dark", mbid="", rgid=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(_MINIMAL_FLAC)
    audio = FLAC(path)
    audio["album"] = [album]
    if mbid:
        audio["musicbrainz_albumid"] = [mbid]
    if rgid:
        audio["musicbrainz_releasegroupid"] = [rgid]
    audio.save()
    return str(path)


def _ids(path):
    audio = FLAC(path)
    return (audio.get("musicbrainz_albumid") or [""])[0], (audio.get("musicbrainz_releasegroupid") or [""])[0]


def test_new_track_without_an_id_adopts_the_albums(tmp_path):
    album = tmp_path / "lib" / "Album"
    _flac(str(album / "01.flac"), mbid=MBID, rgid=RGID)
    _flac(str(album / "02.flac"), mbid=MBID, rgid=RGID)
    incoming = _flac(str(tmp_path / "staging" / "03.flac"))
    result = album_identity.harmonize(incoming, str(album / "03.flac"))
    assert result == {"adopted": True, "propagated": 0, "conflict": False}
    assert _ids(incoming) == (MBID, RGID)


def test_new_track_with_an_id_gives_it_to_earlier_tracks_that_lack_one(tmp_path):
    """The reported split: 53 tracks without the id, then tracks that have it."""
    album = tmp_path / "lib" / "Album"
    earlier = [_flac(str(album / f"0{n}.flac")) for n in (1, 2, 3)]
    incoming = _flac(str(tmp_path / "staging" / "04.flac"), mbid=MBID, rgid=RGID)
    result = album_identity.harmonize(incoming, str(album / "04.flac"))
    assert result["propagated"] == 3 and not result["adopted"]
    assert all(_ids(path) == (MBID, RGID) for path in earlier)


def test_disc_subfolders_belong_to_the_same_album(tmp_path):
    album = tmp_path / "lib" / "Album"
    _flac(str(album / "Disc 1" / "01.flac"), mbid=MBID)
    incoming = _flac(str(tmp_path / "staging" / "x.flac"))
    assert album_identity.harmonize(incoming, str(album / "Disc 2" / "01.flac"))["adopted"] is True
    assert _ids(incoming)[0] == MBID


def test_other_albums_in_the_folder_and_nested_folders_are_ignored(tmp_path):
    album = tmp_path / "lib" / "Artist"
    other = _flac(str(album / "01 - other album.flac"), album="A Different Album")
    deep = _flac(str(album / "Sub Album" / "01.flac"))
    incoming = _flac(str(tmp_path / "staging" / "x.flac"), mbid=MBID)
    result = album_identity.harmonize(incoming, str(album / "02.flac"))
    assert result == {"adopted": False, "propagated": 0, "conflict": False}
    assert _ids(other) == ("", "") and _ids(deep) == ("", "")


def test_conflicting_ids_are_not_spread(tmp_path):
    album = tmp_path / "lib" / "Album"
    a = _flac(str(album / "01.flac"), mbid=MBID)
    b = _flac(str(album / "02.flac"), mbid=MBID)
    c = _flac(str(album / "03.flac"), mbid="other-release")
    blank = _flac(str(album / "04.flac"))
    incoming = _flac(str(tmp_path / "staging" / "05.flac"))
    result = album_identity.harmonize(incoming, str(album / "05.flac"))
    assert result["conflict"] is True and result["adopted"] is True and result["propagated"] == 0
    assert _ids(incoming)[0] == MBID                        # the new file joins the majority…
    assert _ids(c)[0] == "other-release" and _ids(blank)[0] == ""   # …and nothing else is rewritten
    assert _ids(a)[0] == MBID and _ids(b)[0] == MBID


def test_nothing_happens_without_ids_or_when_switched_off(tmp_path, fork_env):
    album = tmp_path / "lib" / "Album"
    sibling = _flac(str(album / "01.flac"))
    incoming = _flac(str(tmp_path / "staging" / "02.flac"))
    assert album_identity.harmonize(incoming, str(album / "02.flac"))["adopted"] is False
    fork_env.set("fork.albums.keep_ids_consistent", False)
    with_id = _flac(str(tmp_path / "staging" / "03.flac"), mbid=MBID)
    assert album_identity.harmonize(with_id, str(album / "03.flac"))["propagated"] == 0
    assert _ids(sibling) == ("", "")


def test_the_import_hook_harmonizes_against_the_destination_folder(tmp_path):
    album = tmp_path / "lib" / "Album"
    _flac(str(album / "01.flac"), mbid=MBID)
    incoming = _flac(str(tmp_path / "staging" / "02.flac"))
    hooks.after_metadata_enhanced(incoming, {"_final_processed_path": str(album / "02.flac")})
    assert _ids(incoming)[0] == MBID
    # a rename-only import must not have its tags touched
    untouched = _flac(str(tmp_path / "staging" / "03.flac"))
    context = {"_final_processed_path": str(album / "03.flac")}
    hooks.mark_rename_only(context, True)
    hooks.after_metadata_enhanced(untouched, context)
    assert _ids(untouched)[0] == ""
