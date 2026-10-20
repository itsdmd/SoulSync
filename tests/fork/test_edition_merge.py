"""Album editions: sets are found by name and songs, and merged into the
version to keep — or left apart with a proposal to fill them up."""

import os

import pytest
from mutagen.flac import FLAC

from core.fork import album_tagging, edition_merge, retro, tags
from core.repair_jobs.fork_tools import EditionMergeJob
from tests.fork.test_single_merge import Ctx, Library


@pytest.fixture
def lib(tmp_path, monkeypatch):
    library = Library(tmp_path)
    monkeypatch.setattr(album_tagging, "allowed_roots", lambda: [os.path.realpath(str(library.root))])
    monkeypatch.setattr(album_tagging, "_resolve", lambda p: p if p and os.path.isfile(p) else None)
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())
    monkeypatch.setattr(retro, "_update_db_path", lambda old, new: None)
    return library


def _album(lib, title, songs, durations=None, artist="Adele"):
    return [lib.add(title, song, n, expected=len(songs), artist=artist,
                    duration=(durations or {}).get(song, 200 + 10 * n))
            for n, song in enumerate(songs, 1)]


class Worker:
    def __init__(self, lib):
        self.db, self.transfer_folder, self._config_manager = lib, str(lib.root), None
        self.filled = []

    def _fix_incomplete_album(self, entity_type, entity_id, file_path, details):
        self.filled.append(details["album_title"])
        return {"success": True, "message": "queued"}


def test_only_packaging_notes_are_taken_off_the_name():
    s = edition_merge.strip_editions
    assert s("25 (Deluxe Edition)") == ("25", True)
    assert s("25 [Remastered 2015] (Special Edition)") == ("25", True)
    assert s("25 - Limited Edition") == ("25", True)
    assert s("25 Deluxe Edition") == ("25", True)
    assert s("初恋 (初回限定盤)")[1] is True
    for kept in ("25", "Gold", "MTV Unplugged", "Red (Taylor's Version)", "25 (Live at Wembley)",
                 "25 (Acoustic)", "25 (Remixes)", "Special"):
        assert s(kept)[1] is False, kept


def test_same_songs_merge_into_the_plain_name(lib):
    _album(lib, "25 (Special Edition)", ["Hello", "Send My Love", "Water"])
    _album(lib, "25", ["Hello", "Send My Love", "Water"])
    sets = edition_merge.find_sets(lib)
    assert len(sets) == 1
    [(source, target)] = sets[0]["merges"]
    assert (source["title"], target["title"]) == ("25 (Special Edition)", "25")
    assert [k["title"] for k in sets[0]["keepers"]] == ["25"]


def test_the_edition_holding_every_song_is_kept(lib):
    _album(lib, "21", ["Rolling", "Rumour", "Turning"])
    _album(lib, "21 (Deluxe)", ["Rolling", "Rumour", "Turning", "If It Hadn't Been", "Need You Now"])
    [found] = edition_merge.find_sets(lib)
    assert [(s["title"], t["title"]) for s, t in found["merges"]] == [("21", "21 (Deluxe)")]


def test_editions_that_each_lack_songs_stay_apart(lib):
    _album(lib, "19", ["Daydreamer", "Best for Last", "Chasing Pavements"])
    _album(lib, "19 (Japan Edition)", ["Daydreamer", "Best for Last", "Bonus Song"],
           durations={"Bonus Song": 251})
    [found] = edition_merge.find_sets(lib)
    assert found["merges"] == [] and len(found["keepers"]) == 2
    details = edition_merge.gaps_details(found["keepers"], [])
    lacking = {a["title"]: a["lacking"] for a in details["albums"]}
    assert lacking == {"19": ["Bonus Song"], "19 (Japan Edition)": ["Chasing Pavements"]}

    worker = Worker(lib)
    assert edition_merge.fill_gaps(worker, details)["success"] is True
    assert sorted(worker.filled) == ["19", "19 (Japan Edition)"]


def test_three_editions(lib):
    _album(lib, "30", ["Strangers", "Easy", "Cry"])
    _album(lib, "30 (Deluxe)", ["Strangers", "Easy", "Cry", "Wild", "Can I"])
    _album(lib, "30 (Japan Edition)", ["Strangers", "Easy", "Cry", "Tokyo"], durations={"Tokyo": 260})
    [found] = edition_merge.find_sets(lib)
    assert [(s["title"], t["title"]) for s, t in found["merges"]] == [("30", "30 (Deluxe)")]
    assert sorted(k["title"] for k in found["keepers"]) == ["30 (Deluxe)", "30 (Japan Edition)"]


def test_other_recordings_are_not_editions(lib):
    _album(lib, "Live", ["One", "Two", "Three"], artist="Band")
    _album(lib, "Live (Acoustic)", ["One", "Two", "Three"], artist="Band")
    # same name, same titles, but every song another length: another recording
    _album(lib, "Echo", ["One", "Two", "Three"], artist="Band")
    _album(lib, "Echo (Deluxe)", ["One", "Two", "Three"], artist="Band",
           durations={"One": 300, "Two": 320, "Three": 340})
    assert edition_merge.find_sets(lib) == []


def test_the_merge_keeps_one_copy_and_the_better_one(lib):
    kept = _album(lib, "25", ["Hello", "Water"])
    gone = _album(lib, "25 (Special Edition)", ["Hello", "Water"])
    better = lib.row(gone[0])[2]
    audio = FLAC(better)
    audio["comment"] = ["the better copy"]
    audio.save(padding=lambda info: 400_000)          # many more bytes per second than the kept copy
    [found] = edition_merge.find_sets(lib)
    [(source, target)] = found["merges"]
    details = edition_merge.merge_details(target, [source], 3.0, 0.85)
    title, description = edition_merge.finding_text(edition_merge.MERGE_TYPE, details)
    assert "25" in title and "same songs" in description

    result = edition_merge.merge(Worker(lib), details)
    assert result["success"] is True, result
    assert lib.albums() == ["25"]
    hello_path = lib.row(kept[0])[2]
    assert FLAC(hello_path)["comment"] == ["the better copy"] and FLAC(hello_path)["album"] == ["25"]
    assert lib.row(gone[0]) is None and lib.row(gone[1]) is None
    deleted = lib.root / ".deleted"
    assert any(name.endswith(".flac") for _r, _d, files in os.walk(deleted) for name in files)
    assert not (lib.root / "Adele" / "25 (Special Edition)").exists()


def test_the_job_reports_merges_and_gaps(lib):
    _album(lib, "21", ["Rolling", "Rumour"])
    _album(lib, "21 (Deluxe)", ["Rolling", "Rumour", "Lovesong"])
    _album(lib, "19", ["Daydreamer", "Best for Last", "Chasing Pavements"])
    _album(lib, "19 (Japan Edition)", ["Daydreamer", "Best for Last", "Bonus Song"],
           durations={"Bonus Song": 251})
    ctx = Ctx(lib, "fork_edition_merge")
    result = EditionMergeJob().scan(ctx)
    kinds = sorted(f["finding_type"] for f in ctx.findings)
    assert kinds == ["fork_album_editions", "fork_edition_gaps"] and result.findings_created == 2


def test_different_titles_match_only_in_the_same_place_with_the_same_length(lib):
    _album(lib, "Album", ["One", "Two", "Three"], artist="X")
    _album(lib, "Album (Deluxe)", ["One", "Two", "Drei"], artist="X")     # renamed, same place and length
    [found] = edition_merge.find_sets(lib)
    # the same songs, so the plain name is kept
    assert [(s["title"], t["title"]) for s, t in found["merges"]] == [("Album (Deluxe)", "Album")]


def test_two_rows_for_one_file_lose_only_the_row(lib):
    kept = _album(lib, "25", ["Hello"])
    path = lib.row(kept[0])[2]
    conn = lib._get_connection()
    alid = conn.execute("INSERT INTO albums (artist_id, title, record_type) VALUES (1, '25 (Deluxe)', 'album')").lastrowid
    extra = conn.execute("INSERT INTO tracks (album_id, artist_id, title, track_number, disc_number, duration,"
                         " file_path) VALUES (?, 1, 'Hello', 1, 1, 210, ?)", (alid, path)).lastrowid
    conn.commit()
    conn.close()
    [found] = edition_merge.find_sets(lib)
    [(source, target)] = found["merges"]
    result = edition_merge.merge(Worker(lib), edition_merge.merge_details(target, [source], 3.0, 0.85))
    assert result["success"] is True and os.path.isfile(path)
    assert lib.row(extra) is None and lib.albums() == ["25"]
