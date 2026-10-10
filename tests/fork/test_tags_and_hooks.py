import os

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2

from core.fork import hooks, store, tags


def _reply(text):
    return {"translations": [{"id": 1, "translation": text}]}


@pytest.fixture
def no_lock(monkeypatch):
    """Use an in-place save so the test does not depend on upstream's atomic
    save verifying a synthetic file."""
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())


_MINIMAL_FLAC = (
    b"fLaC" + b"\x80\x00\x00\x22" + b"\x00\x10\x00\x10" + b"\x00\x00\x00\x00\x00\x00"
    + b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)
# One MPEG-1 Layer III frame header (128 kbps, 44.1 kHz) repeated: enough for
# mutagen to sync on.
_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def _flac(tmp_path, **fields):
    path = str(tmp_path / "t.flac")
    with open(path, "wb") as fh:
        fh.write(_MINIMAL_FLAC)
    audio = FLAC(path)
    for key, value in fields.items():
        audio[key] = value if isinstance(value, list) else [value]
    audio.save()
    return path


def test_flac_tags_get_rules_translations_and_original_backups(tmp_path, llm, no_lock):
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    path = _flac(tmp_path, title="夜曲", album="十一月的蕭邦", artist="周杰倫", albumartist="周杰倫",
                 artists=["周杰倫", "Someone"])
    llm.replies = [_reply("November's Chopin"), _reply("Nocturne")]
    changed = tags.apply_to_file(path)
    assert set(changed) == {"title", "album", "artist", "albumartist", "artists"}
    audio = FLAC(path)
    assert audio["title"] == ["Nocturne (夜曲)"]
    assert audio["album"] == ["November's Chopin (十一月的蕭邦)"]
    assert audio["artist"] == ["Jay Chou"] and audio["albumartist"] == ["Jay Chou"]
    assert audio["artists"] == ["Jay Chou", "Someone"]
    assert audio["soulsync_original_title"] == ["夜曲"]
    assert audio["soulsync_original_artist"] == ["周杰倫"]
    # idempotent: a second pass changes nothing and asks the model nothing
    assert tags.apply_to_file(path) == {}
    assert len(llm.calls) == 2


def test_id3_tags(tmp_path, llm, no_lock):
    path = str(tmp_path / "t.mp3")
    with open(path, "wb") as fh:
        fh.write(_MP3_FRAME * 8)
    id3 = ID3()
    id3.add(TIT2(encoding=3, text=["夜曲"]))
    id3.add(TALB(encoding=3, text=["Fantasy"]))
    id3.add(TPE1(encoding=3, text=["周杰倫"]))
    id3.add(TPE2(encoding=3, text=["周杰倫"]))
    id3.save(path)
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    llm.replies = [_reply("Nocturne")]
    tags.apply_to_file(path)
    out = ID3(path)
    assert out["TIT2"].text == ["Nocturne (夜曲)"]
    assert out["TALB"].text == ["Fantasy"]
    assert out["TPE1"].text == ["Jay Chou"] and out["TPE2"].text == ["Jay Chou"]
    assert out["TXXX:SOULSYNC_ORIGINAL_TITLE"].text == ["夜曲"]


def test_latin_file_is_not_rewritten(tmp_path, llm, no_lock):
    path = _flac(tmp_path, title="Yellow", album="Parachutes", artist="Coldplay")
    before = os.path.getmtime(path)
    assert tags.apply_to_file(path) == {}
    assert os.path.getmtime(path) == before and llm.calls == []


def test_path_context_matches_what_the_tags_get(llm):
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    llm.replies = [_reply("November's Chopin"), _reply("Nocturne")]
    ctx = {"artist": "周杰倫", "albumartist": "周杰倫", "album": "十一月的蕭邦", "title": "夜曲",
           "track_number": 3, "_artists_list": [{"name": "周杰倫", "id": "x"}]}
    out = hooks.transform_template_context(ctx, "album_path")
    assert out["albumartist"] == "Jay Chou" and out["artist"] == "Jay Chou"
    assert out["album"] == "November's Chopin (十一月的蕭邦)"
    assert out["title"] == "Nocturne (夜曲)"
    assert out["_artists_list"] == [{"name": "Jay Chou", "id": "x"}]
    assert out["track_number"] == 3
    assert ctx["album"] == "十一月的蕭邦"  # caller's dict untouched


def test_path_context_untouched_for_other_templates_or_when_disabled(llm, fork_env):
    ctx = {"artist": "周杰倫", "album": "十一月的蕭邦", "title": "夜曲"}
    assert hooks.transform_template_context(ctx, "podcast_path") is ctx
    fork_env.set("fork.translate.apply_to_paths", False)
    assert hooks.transform_template_context(ctx, "album_path") is ctx
    assert llm.calls == []


def test_hooks_are_inert_outside_fork_tests(monkeypatch, llm):
    monkeypatch.delenv("SOULSYNC_FORK_TESTING")
    ctx = {"artist": "周杰倫", "album": "十一月的蕭邦", "title": "夜曲"}
    assert hooks.transform_template_context(ctx, "album_path") is ctx
    assert hooks.augment_search_queries(object(), ["q"]) == ["q"]
    assert llm.calls == []


def test_rename_only_flag_and_quarantine_skips():
    context = {"_skip_quarantine_check": ["quality", "bit_depth"]}
    hooks.mark_rename_only(context, False)
    assert not hooks.is_rename_only(context)
    hooks.mark_rename_only(context, True)
    assert hooks.is_rename_only(context)
    assert context["_skip_quarantine_check"] == ["quality", "bit_depth", "acoustid", "silence"]


def test_rename_only_skips_enhancement_lyrics_and_transforms(tmp_path, monkeypatch):
    from core.imports import pipeline
    from core.metadata import enrichment, lyrics as metadata_lyrics

    def boom(*args, **kwargs):
        raise AssertionError("must not run for a rename-only import")

    monkeypatch.setattr(enrichment, "_upstream_enhance_file_metadata", boom)
    monkeypatch.setattr(metadata_lyrics, "_upstream_generate_lrc_file", boom)
    monkeypatch.setattr(pipeline, "_upstream_apply_profile_output_transforms", boom)
    context = {}
    hooks.mark_rename_only(context, True)
    path = str(tmp_path / "x.flac")
    assert enrichment.enhance_file_metadata(path, context, {}, {}) is True
    assert metadata_lyrics.generate_lrc_file(path, context, {}, {}) is False
    assert pipeline._apply_profile_output_transforms(path, context, {}) == path


def test_watcher_rename_only_follows_the_setting(fork_env):
    assert hooks.auto_import_rename_only() is False
    fork_env.set("fork.import.rename_only_auto", True)
    assert hooks.auto_import_rename_only() is True
    context = {}
    hooks.mark_rename_only(context, hooks.auto_import_rename_only())
    assert hooks.is_rename_only(context)


def test_a_track_the_pipeline_declined_to_file_is_reported_not_counted_as_imported():
    from core.imports.pipeline import import_rejection_reason

    assert import_rejection_reason({}) is None
    skipped = import_rejection_reason(
        {"_context_failure_msg": "Incoming file is not a verified improvement under the quality profile"})
    assert skipped.startswith("not imported: the library already has this track")
    assert import_rejection_reason({"_context_failure_msg": "Missing artist context"}) == \
        "not imported: Missing artist context"
    # upstream's own reasons still win
    assert import_rejection_reason({"_integrity_failure_msg": "truncated", "_context_failure_msg": "x"}) == \
        "integrity check failed: truncated"


def test_album_folder_lookup_runs_once_per_album_inside_a_preview(monkeypatch):
    from core.fork import hooks

    monkeypatch.setenv("SOULSYNC_FORK_TESTING", "1")
    calls = []

    def resolve(**kwargs):
        calls.append(kwargs["album_name"])
        return None

    with hooks.album_preview_scope():
        for _ in range(3):
            assert hooks.album_folder_lookup(resolve, {"album_name": "A", "album_artist": "X"}) is None
        hooks.album_folder_lookup(resolve, {"album_name": "B", "album_artist": "X"})
    assert calls == ["A", "B"]

    # a real import, outside a preview, is never answered from memory
    hooks.album_folder_lookup(resolve, {"album_name": "A", "album_artist": "X"})
    hooks.album_folder_lookup(resolve, {"album_name": "A", "album_artist": "X"})
    assert calls == ["A", "B", "A", "A"]
