"""Files in the .deleted quarantine are out of the library for every tool.

The duplicate detector offered a copy for deletion that was already sitting in
<transfer>/.deleted: the standalone Full Refresh / Deep Scan walked the whole
transfer folder, quarantine included, and indexed it. The walkers now skip it
(like the regular scan always did) and tools reading library rows drop any row
that points there.
"""

from __future__ import annotations

import os

from core.repair_jobs.base import drop_quarantined, is_internal_transfer_dir


def test_rows_in_the_quarantine_are_dropped(tmp_path):
    transfer = str(tmp_path / "Transfer")
    rows = [
        ("1", os.path.join(transfer, "Artist", "Album", "01 Song.flac")),
        ("2", os.path.join(transfer, ".deleted", "Artist", "Album", "01 Song.flac")),
        ("3", os.path.join(transfer, "deleted", "Artist", "Album", "01 Song.flac")),     # legacy spelling
        ("4", os.path.join(transfer, ".soulsync_atomic_staging", "x", "01 Song.flac")),
        ("5", os.path.join(transfer, "Artist", "deleted", "01 Song.flac")),               # an artist's own folder
        ("6", ""),
    ]
    kept = drop_quarantined(rows, lambda r: r[1], transfer)
    assert [r[0] for r in kept] == ["1", "5", "6"]


def test_walkers_prune_the_quarantine(tmp_path):
    from core.repair_jobs.base import skip_deleted_quarantine, walk_library

    transfer = tmp_path / "Transfer"
    for rel in ("Artist/Album/a.flac", ".deleted/Artist/Album/a.flac", "deleted/b.flac"):
        path = transfer / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    seen = []
    for root, dirs, files in os.walk(transfer):
        skip_deleted_quarantine(root, dirs, str(transfer))
        seen += [os.path.join(root, f) for f in files]
    assert seen == [str(transfer / "Artist/Album/a.flac")]
    assert [f for _r, _d, fs in walk_library(str(transfer)) for f in fs] == ["a.flac"]


def test_the_standalone_scans_leave_the_quarantine_alone(tmp_path, monkeypatch):
    """Full Refresh indexes only the library; Deep Scan neither moves the
    quarantine's files to Staging nor keeps rows that point into it."""
    import api.database_admin as admin

    transfer = tmp_path / "Transfer"
    keep = transfer / "Artist" / "Album" / "01 Song.flac"
    gone = transfer / ".deleted" / "Artist" / "Album" / "01 Song.flac"
    for path in (keep, gone):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    assert is_internal_transfer_dir(str(gone), str(transfer))
    src = open(admin.__file__, encoding="utf-8").read()
    # the three walkers over the transfer folder all prune it
    assert src.count("skip_deleted_quarantine(root, _dirs, transfer_path)") == 2
    assert "is_internal_transfer_dir(dir_path, transfer_path)" in src
    assert "is_internal_transfer_dir(db_path, transfer_path)" in src
