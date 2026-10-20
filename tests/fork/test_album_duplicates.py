"""Album Grouping, duplicates: one album listed twice becomes one album —
each song once, one folder, one set of release tags, one library row."""

import os
import shutil

import pytest
from mutagen.flac import FLAC

from core.fork import album_duplicates, album_tagging, edition_merge, retro, tags
from core.repair_jobs.fork_tools import VolumeGroupingJob
from tests.fork.test_edition_merge import Worker
from tests.fork.test_single_merge import Ctx, Library


@pytest.fixture
def lib(tmp_path, monkeypatch):
    library = Library(tmp_path)
    monkeypatch.setattr(album_tagging, "allowed_roots", lambda: [os.path.realpath(str(library.root))])
    monkeypatch.setattr(album_tagging, "_resolve", lambda p: p if p and os.path.isfile(p) else None)
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())
    monkeypatch.setattr(retro, "_update_db_path", lambda old, new: None)
    return library


def _tag(path, **values):
    audio = FLAC(path)
    for key, value in values.items():
        audio[key] = [value]
    audio.save()


def _row(lib, album_id, title, number, path, duration):
    conn = lib._get_connection()
    tid = conn.execute("INSERT INTO tracks (album_id, artist_id, title, track_number, disc_number, duration,"
                       " file_path) VALUES (?, 1, ?, ?, 1, ?, ?)", (album_id, title, number, duration, path)).lastrowid
    conn.commit()
    conn.close()
    return tid


def _second_listing(lib, title="Butterfly"):
    conn = lib._get_connection()
    alid = conn.execute("INSERT INTO albums (artist_id, title, year, record_type) VALUES (1, ?, 2015, 'album')",
                        (title,)).lastrowid
    conn.commit()
    conn.close()
    return alid


def _copy(src, dst, **values):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy(src, dst)
    if values:
        _tag(dst, **values)
    return dst


def _album(lib, songs, title="Butterfly", album_id="mb-1"):
    ids = [lib.add(title, song, n, expected=len(songs), artist="Kendrick", duration=100 + 50 * n)
           for n, song in enumerate(songs, 1)]
    for tid in ids:
        _tag(lib.row(tid)[2], musicbrainz_albumid=album_id, date="2015-03-15", albumartist="Kendrick")
    return ids


def _db_rows(lib):
    conn = lib._get_connection()
    rows = conn.execute("SELECT album_id, file_path FROM tracks ORDER BY file_path").fetchall()
    conn.close()
    return rows


def test_one_album_listed_twice_becomes_one(lib):
    ids = _album(lib, ["Wesley", "For Free", "King Kunta", "For Sale"])
    folder = os.path.dirname(lib.row(ids[0])[2])
    other = _second_listing(lib)
    # a fill copied a song next to itself; one file has another release id
    copy = _copy(lib.row(ids[2])[2], os.path.join(folder, "3. King Kunta.flac"))
    _row(lib, other, "King Kunta", 3, copy, 250)
    _tag(lib.row(ids[3])[2], musicbrainz_albumid="mb-2")
    os.utime(copy, (2_000_000_000, 2_000_000_000))             # the copy is the newer file
    # a copy no row lists, and a file holding "For Free" under "For Sale"'s name
    stray = _copy(lib.row(ids[0])[2], os.path.join(folder, "1.01. Wesley.flac"))
    os.utime(stray, (2_000_000_000, 2_000_000_000))
    fake = _copy(lib.row(ids[1])[2], os.path.join(folder, "8. For Sale.flac"), title="For Sale")
    _row(lib, other, "For Sale", 4, fake, 200)

    [found] = album_duplicates.find_sets(lib)
    gone = {os.path.basename(r["path"]): r["reason"] for r in found["removals"]}
    assert gone == {"3. King Kunta.flac": "a second copy", "1.01. Wesley.flac": "a second copy",
                    "8. For Sale.flac": 'holds the audio of "For Free"'}
    assert found["moves"] == [] and found["tags"]["musicbrainz_albumid"] == "mb-1"
    title, description = album_duplicates.finding_text(found)
    assert "Butterfly" in title and "3 duplicate copies" in description

    result = album_duplicates.merge(Worker(lib), found)
    assert result["success"] is True, result
    left = sorted(n for n in os.listdir(folder) if n.endswith(".flac"))
    assert left == ["01 - Wesley.flac", "02 - For Free.flac", "03 - King Kunta.flac", "04 - For Sale.flac"]
    assert {FLAC(os.path.join(folder, n))["musicbrainz_albumid"][0] for n in left} == {"mb-1"}
    assert lib.albums() == ["Butterfly"] and len(_db_rows(lib)) == 4
    assert any(n.endswith(".flac") for _r, _d, names in os.walk(lib.root / ".deleted") for n in names)
    # done: a new scan finds nothing
    assert album_duplicates.find_sets(lib) == []


def test_files_in_two_folders_move_into_one(lib):
    ids = _album(lib, ["One", "Two", "Three"])
    folder = os.path.dirname(lib.row(ids[0])[2])
    other = _second_listing(lib)
    elsewhere = os.path.join(os.path.dirname(folder), "Butterfly_", "Disc 2", "01 - Four.flac")
    _copy(lib.row(ids[0])[2], elsewhere, title="Four")
    _row(lib, other, "Four", 1, elsewhere, 400)

    [found] = album_duplicates.find_sets(lib)
    assert found["removals"] == []
    assert [m["to"] for m in found["moves"]] == [os.path.join(folder, "Disc 2", "01 - Four.flac")]
    assert album_duplicates.merge(Worker(lib), found)["success"] is True
    assert os.path.isfile(os.path.join(folder, "Disc 2", "01 - Four.flac"))
    assert not os.path.exists(os.path.join(os.path.dirname(folder), "Butterfly_"))
    assert {r[0] for r in _db_rows(lib)} == {lib.ids[("Kendrick", "Butterfly")]} and lib.albums() == ["Butterfly"]


def test_different_years_or_folders_named_otherwise_stay_apart(lib):
    ids = _album(lib, ["One", "Two"])
    folder = os.path.dirname(lib.row(ids[0])[2])
    other = _second_listing(lib)
    reissue = os.path.join(os.path.dirname(folder), "Butterfly (2025)", "01 - One.flac")
    _copy(lib.row(ids[0])[2], reissue, date="2025-01-01")
    _row(lib, other, "One", 1, reissue, 150)
    assert album_duplicates.find_sets(lib) == []

    # the same year, but the folder names another release (a Dolby Atmos mix): its files stay put
    _tag(reissue, date="2015-03-15")
    os.rename(os.path.dirname(reissue), os.path.join(os.path.dirname(folder), "Butterfly (Dolby Atmos)"))
    conn = lib._get_connection()
    conn.execute("UPDATE tracks SET file_path = ? WHERE album_id = ?",
                 (os.path.join(os.path.dirname(folder), "Butterfly (Dolby Atmos)", "01 - One.flac"), other))
    conn.commit()
    conn.close()
    _tag(lib.row(ids[1])[2], musicbrainz_albumid="mb-2")
    [found] = album_duplicates.find_sets(lib)
    assert found["removals"] == [] and found["moves"] == []


def test_copies_of_different_lengths_are_left_for_a_look(lib):
    ids = _album(lib, ["One", "Two"])
    folder = os.path.dirname(lib.row(ids[0])[2])
    other = _second_listing(lib)
    longer = _copy(lib.row(ids[1])[2], os.path.join(folder, "2. Two.flac"), musicbrainz_albumid="mb-2")
    _row(lib, other, "Two", 2, longer, 600)
    [found] = album_duplicates.find_sets(lib)
    assert found["removals"] == [] and found["retag"] == 1
    assert [c["title"] for c in found["conflicts"]] == ["Two"]


def test_the_job_reports_duplicates_and_fill_editions_refuses_split_rows(lib):
    ids = _album(lib, ["One", "Two"])
    other = _second_listing(lib)
    folder = os.path.dirname(lib.row(ids[0])[2])
    copy = _copy(lib.row(ids[0])[2], os.path.join(folder, "1. One.flac"))
    _row(lib, other, "One", 1, copy, 150)
    ctx = Ctx(lib, "fork_volume_grouping")
    result = VolumeGroupingJob().scan(ctx)
    assert [f["finding_type"] for f in ctx.findings] == ["fork_album_duplicates"] and result.findings_created == 1
    assert VolumeGroupingJob.display_name == "Album Grouping"

    details = {"artist": "Kendrick", "albums": [{"album_id": lib.ids[("Kendrick", "Butterfly")], "title": "Butterfly"},
                                                {"album_id": other, "title": "Butterfly"}]}
    worker = Worker(lib)
    outcome = edition_merge.fill_gaps(worker, details)
    assert outcome["success"] is False and "Album Grouping" in outcome["error"] and worker.filled == []
