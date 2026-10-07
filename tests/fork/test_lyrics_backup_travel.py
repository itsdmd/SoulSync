"""<name>.original.lrc must follow its track through every move SoulSync does."""

import os

from core.fork import hooks, lyrics


def _touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return str(path)


def _names(directory):
    return sorted(os.listdir(directory)) if os.path.isdir(directory) else []


def test_backup_moves_and_takes_the_new_stem(tmp_path):
    src = _touch(tmp_path / "old" / "01 - 夜曲.flac")
    _touch(tmp_path / "old" / "01 - 夜曲.original.lrc", "ORIGINAL")
    _touch(tmp_path / "old" / "01 - 夜曲.lrc", "TRANSLATED")
    dst = str(tmp_path / "new" / "sub" / "02 - Nocturne (夜曲).flac")
    lyrics.move_backups(src, dst)
    assert _names(tmp_path / "new" / "sub") == ["02 - Nocturne (夜曲).original.lrc"]
    assert open(tmp_path / "new" / "sub" / "02 - Nocturne (夜曲).original.lrc", encoding="utf-8").read() == "ORIGINAL"
    # the translated sidecar is upstream's to move unless asked
    assert _names(tmp_path / "old") == ["01 - 夜曲.flac", "01 - 夜曲.lrc"]


def test_with_partner_carries_the_translated_file_too(tmp_path):
    src = _touch(tmp_path / "a" / "t.flac")
    _touch(tmp_path / "a" / "t.original.txt", "ORIGINAL")
    _touch(tmp_path / "a" / "t.txt", "TRANSLATED")
    lyrics.move_backups(src, str(tmp_path / "b" / "u.flac"), with_partner=True)
    assert _names(tmp_path / "b") == ["u.original.txt", "u.txt"]
    assert _names(tmp_path / "a") == ["t.flac"]


def test_partner_is_left_alone_when_there_is_no_backup(tmp_path):
    src = _touch(tmp_path / "a" / "t.flac")
    _touch(tmp_path / "a" / "t.lrc", "plain upstream lyrics")
    lyrics.move_backups(src, str(tmp_path / "b" / "u.flac"), with_partner=True)
    assert _names(tmp_path / "a") == ["t.flac", "t.lrc"] and _names(tmp_path / "b") == []


def test_existing_destination_backup_wins_and_the_source_copy_is_not_left_behind(tmp_path):
    src = _touch(tmp_path / "a" / "t.flac")
    _touch(tmp_path / "a" / "t.original.lrc", "STALE")
    _touch(tmp_path / "b" / "t.original.lrc", "FRESH")
    lyrics.move_backups(src, str(tmp_path / "b" / "t.flac"))
    assert open(tmp_path / "b" / "t.original.lrc", encoding="utf-8").read() == "FRESH"
    assert _names(tmp_path / "a") == ["t.flac"]


def test_same_path_is_a_no_op_and_remove_deletes_both_kinds(tmp_path):
    src = _touch(tmp_path / "a" / "t.flac")
    _touch(tmp_path / "a" / "t.original.lrc")
    _touch(tmp_path / "a" / "t.original.txt")
    lyrics.move_backups(src, str(tmp_path / "a" / "t.mp3"))  # same stem, other format
    assert _names(tmp_path / "a") == ["t.flac", "t.original.lrc", "t.original.txt"]
    hooks.remove_lyrics_backup(src)
    assert _names(tmp_path / "a") == ["t.flac"]


# ── through upstream's own movers ───────────────────────────────────────

def test_import_move_carries_the_backup(tmp_path):
    from core.imports.file_ops import move_companion_sidecars

    src = _touch(tmp_path / "dl" / "song.flac")
    _touch(tmp_path / "dl" / "song.lrc", "TRANSLATED")
    _touch(tmp_path / "dl" / "song.original.lrc", "ORIGINAL")
    dst = _touch(tmp_path / "lib" / "01 - Song.flac")
    move_companion_sidecars(src, dst)
    assert _names(tmp_path / "lib") == ["01 - Song.flac", "01 - Song.lrc", "01 - Song.original.lrc"]
    assert _names(tmp_path / "dl") == ["song.flac"]


def test_reorganize_rename_in_place_carries_backup_and_translation_and_undoes(tmp_path):
    from core.library_reorganize import _rename_track_in_place

    src = _touch(tmp_path / "Old Album" / "01 - a.flac")
    _touch(tmp_path / "Old Album" / "01 - a.lrc", "TRANSLATED")
    _touch(tmp_path / "Old Album" / "01 - a.original.lrc", "ORIGINAL")
    dst = str(tmp_path / "New Album" / "01 - b.flac")
    assert _rename_track_in_place(src, dst) == (True, None)
    assert _names(tmp_path / "New Album") == ["01 - b.flac", "01 - b.lrc", "01 - b.original.lrc"]
    assert _names(tmp_path / "Old Album") == []
    # reorganize rolls a track back with the same function when the DB update fails
    assert _rename_track_in_place(dst, src) == (True, None)
    assert _names(tmp_path / "Old Album") == ["01 - a.flac", "01 - a.lrc", "01 - a.original.lrc"]


def test_failed_rename_moves_nothing(tmp_path):
    from core.library_reorganize import _rename_track_in_place

    src = _touch(tmp_path / "a" / "t.flac")
    _touch(tmp_path / "a" / "t.original.lrc")
    dst = _touch(tmp_path / "b" / "t.flac", "someone else's file")
    ok, _err = _rename_track_in_place(src, dst)
    assert ok is False
    assert _names(tmp_path / "a") == ["t.flac", "t.original.lrc"]


def test_track_number_repair_rename_carries_the_backup(tmp_path):
    from core.repair_jobs.track_number_repair import _rename_to_basename

    src = _touch(tmp_path / "al" / "1 - a.flac")
    _touch(tmp_path / "al" / "1 - a.lrc", "TRANSLATED")
    _touch(tmp_path / "al" / "1 - a.original.lrc", "ORIGINAL")
    new_path = _rename_to_basename(src, "1 - a.flac", "01 - a")
    assert new_path and os.path.basename(new_path) == "01 - a.flac"
    assert _names(tmp_path / "al") == ["01 - a.flac", "01 - a.lrc", "01 - a.original.lrc"]
