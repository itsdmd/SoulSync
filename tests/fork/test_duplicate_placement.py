"""Duplicate Tracks, Keep Best: the best copy takes the place of a copy that
sits in a fuller album, with that album's tags."""

import os

import pytest
from mutagen.flac import FLAC

from core import repair_worker as rw
from core.fork import album_tagging, duplicate_placement, jobs, retro, tags
from tests.fork.test_single_merge import Library


@pytest.fixture
def lib(tmp_path, monkeypatch):
    library = Library(tmp_path)
    monkeypatch.setattr(album_tagging, "allowed_roots", lambda: [os.path.realpath(str(library.root))])
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())
    monkeypatch.setattr(retro, "_update_db_path", lambda old, new: None)
    monkeypatch.setattr(rw, "_server_playlist_membership", lambda: {})
    monkeypatch.setattr(rw, "_resolve_file_path", lambda p, *a, **k: p if p and os.path.isfile(p) else None)
    return library


class Worker:
    def __init__(self, lib):
        self.db, self.transfer_folder, self._config_manager = lib, str(lib.root), None
        self.stock = []

    def _fix_duplicates(self, entity_type, entity_id, file_path, details):
        self.stock.append(details)
        return {"success": True, "message": "stock"}

    def _cleanup_empty_parents(self, path):
        pass


def _tag(path, **values):
    audio = FLAC(path)
    for key, value in values.items():
        audio[key] = [value]
    audio.save()


def _setup(lib):
    full = [lib.add("Full Album", song, n, artist="Band") for n, song in enumerate(["Intro", "Song", "Outro"], 1)]
    single = lib.add("Song", "Song", 1, kind="single", expected=1, artist="Band")
    home_path, best_path = lib.row(full[1])[2], lib.row(single)[2]
    _tag(home_path, musicbrainz_albumid="full-id", albumartist="Band", date="2020", comment="worse")
    _tag(best_path, musicbrainz_albumid="single-id", catalognumber="SINGLE-1", date="2019", comment="better")
    details = {"tracks": [
        {"id": full[1], "title": "Song", "album": "Full Album", "file_path": home_path, "bitrate": 320},
        {"id": single, "title": "Song", "album": "Song", "file_path": best_path, "bitrate": 1000},
    ]}
    return full, single, home_path, best_path, details


def test_the_best_copy_moves_into_the_fuller_album(lib):
    full, single, home_path, best_path, details = _setup(lib)
    worker = Worker(lib)
    result = jobs.fix_handlers(worker)["duplicate_tracks"]("track", str(single), best_path, details)
    assert result["success"] is True and "Full Album" in result["message"], result
    assert worker.stock == [] and not os.path.exists(best_path)
    audio = FLAC(home_path)
    assert audio["comment"] == ["better"]                       # the better file, in the album's place
    assert audio["album"] == ["Full Album"] and audio["tracknumber"][0].split("/")[0] == "2"
    assert audio["musicbrainz_albumid"] == ["full-id"] and audio["date"] == ["2020"]
    assert "catalognumber" not in audio                          # nothing of the single's release is left
    assert lib.row(full[1])[0] == "Full Album" and lib.row(full[1])[2] == home_path
    assert lib.row(single) is None
    deleted = [n for _r, _d, names in os.walk(lib.root / ".deleted") for n in names if n.endswith(".flac")]
    assert len(deleted) == 1


def test_stock_keep_best_when_the_best_copy_is_where_it_belongs_or_in_a_playlist(lib, monkeypatch):
    full, single, home_path, best_path, details = _setup(lib)
    # the better copy already sits in the fuller album
    details["tracks"][0]["bitrate"], details["tracks"][1]["bitrate"] = 1000, 320
    worker = Worker(lib)
    assert duplicate_placement.fix(worker, "track", "x", None, details)["message"] == "stock"
    # a copy picked by hand
    details["tracks"][0]["bitrate"], details["tracks"][1]["bitrate"] = 320, 1000
    assert duplicate_placement.fix(worker, "track", "x", None, {**details, "_fix_action": single})["message"] == "stock"
    # the best copy is in a playlist: moving it would drop it from there
    monkeypatch.setattr(rw, "_server_playlist_membership", lambda: {"x": 1})
    monkeypatch.setattr("core.library.playlist_membership.tag_tracks",
                        lambda tracks, membership: [t.update(playlists=["Mix"] if t["id"] == single else [])
                                                    for t in tracks])
    assert duplicate_placement.fix(worker, "track", "x", None, details)["message"] == "stock"
    assert len(worker.stock) == 3 and os.path.exists(best_path) and FLAC(home_path)["comment"] == ["worse"]
