"""Several artists on one track: separate tag values, or one joined value."""

import pytest
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TPE1, TPE2, TXXX

from core.fork import artist_format, artist_names, comma_split, store, tags

_MINIMAL_FLAC = (
    b"fLaC" + b"\x80\x00\x00\x22" + b"\x00\x10\x00\x10" + b"\x00\x00\x00\x00\x00\x00"
    + b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)
_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    class FakeMB:  # knows one separator-named act
        def search_artist(self, name, **kwargs):
            return [{"name": name, "score": 100, "aliases": []}] if name.casefold() == "simon & garfunkel" else []

    monkeypatch.setattr(artist_names, "_mb_client", lambda: FakeMB())
    artist_names._single_artist_cache.clear()
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())


def _flac(tmp_path, name="t.flac", **fields):
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_MINIMAL_FLAC)
    audio = FLAC(path)
    for key, value in fields.items():
        audio[key] = value if isinstance(value, list) else [value]
    audio.save()
    return path


def _mp3(tmp_path, artist, albumartist=None, artists=None):
    path = str(tmp_path / "t.mp3")
    with open(path, "wb") as fh:
        fh.write(_MP3_FRAME * 8)
    id3 = ID3()
    id3.add(TPE1(encoding=3, text=[artist]))
    if albumartist:
        id3.add(TPE2(encoding=3, text=[albumartist]))
    if artists:
        id3.add(TXXX(encoding=3, desc="Artists", text=artists))
    id3.save(path)
    return path


def test_defaults_are_separate_tags_with_semicolon_as_the_fallback_separator():
    assert artist_format.split_tags_enabled() is True
    assert artist_format.tag_values(["A", "B"]) == ["A", "B"]
    assert artist_format.display(["A", "B"]) == "A; B"
    assert artist_format.parse_display("A ;B;  a ; C") == ["A", "B", "C"]


def test_joined_strategy_uses_the_chosen_separator(fork_env):
    fork_env.set("fork.artists.split_tags", False)
    assert artist_format.tag_values(["A", "B"]) == ["A; B"]
    for name, text in (("comma", "A, B"), ("slash", "A / B"), ("ampersand", "A & B"), ("nonsense", "A; B")):
        fork_env.set("fork.artists.separator", name)
        assert artist_format.tag_values(["A", "B"]) == [text] and artist_format.display(["A", "B"]) == text
    # a joined tag is stored exactly as typed
    assert artist_format.parse_display("A, B") == ["A, B"]


def test_download_pass_splits_a_joined_artist_into_separate_values(tmp_path):
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    path = _flac(tmp_path, title="Duet", artist="周杰倫, Lara", albumartist="周杰倫 & Lara")
    changed = tags.apply_to_file(path)
    audio = FLAC(path)
    assert audio["artist"] == ["Jay Chou", "Lara"]
    assert audio["albumartist"] == ["Jay Chou", "Lara"]          # cascades to the album artist
    assert audio["artists"] == ["Jay Chou", "Lara"]              # companion list kept in step
    assert changed["artist"] == "Jay Chou; Lara"
    assert audio["soulsync_original_artist"] == ["周杰倫, Lara"]
    assert tags.apply_to_file(path) == {}                         # second pass: nothing to do


def test_download_pass_with_a_chosen_separator(tmp_path, fork_env):
    fork_env.set("fork.artists.split_tags", False)
    fork_env.set("fork.artists.separator", "slash")
    path = _flac(tmp_path, artist="A, B feat. C", albumartist="A")
    tags.apply_to_file(path)
    audio = FLAC(path)
    assert audio["artist"] == ["A / B / C"] and audio["albumartist"] == ["A"]
    assert audio["artists"] == ["A", "B", "C"]
    assert tags.apply_to_file(path) == {}


def test_switching_strategy_converts_already_tagged_files_both_ways(tmp_path, fork_env):
    path = _flac(tmp_path, artist=["A", "B"], artists=["A", "B"])
    assert tags.apply_to_file(path) == {}                         # already separate
    fork_env.set("fork.artists.split_tags", False)
    fork_env.set("fork.artists.separator", "comma")
    tags.apply_to_file(path)
    assert FLAC(path)["artist"] == ["A, B"]
    fork_env.set("fork.artists.split_tags", True)
    tags.apply_to_file(path)
    assert FLAC(path)["artist"] == ["A", "B"]


def test_single_artists_and_known_acts_are_left_alone(tmp_path):
    path = _flac(tmp_path, artist="Simon & Garfunkel", albumartist="AC/DC")
    assert tags.apply_to_file(path) == {}
    # "featured artists in the title" layout: one display artist, longer list
    path = _flac(tmp_path, "u.flac", artist="A", artists=["A", "B"])
    assert tags.apply_to_file(path) == {}
    assert FLAC(path)["artist"] == ["A"]


def test_the_list_tag_decides_how_a_joined_string_splits(tmp_path):
    # without the list, "Earth, Wind & Fire, B" could not be split correctly
    path = _flac(tmp_path, artist="Earth, Wind & Fire, B", artists=["Earth, Wind & Fire", "B"])
    # the stand-in MusicBrainz does not know the band, so only the list protects it
    tags.apply_to_file(path)
    assert FLAC(path)["artist"][-1] == "B" and len(FLAC(path)["artist"]) >= 2


def test_id3_gets_real_multi_value_frames(tmp_path):
    path = _mp3(tmp_path, "A, B", albumartist="A, B")
    tags.apply_to_file(path)
    id3 = ID3(path)
    assert id3["TPE1"].text == ["A", "B"] and id3["TPE2"].text == ["A", "B"]
    assert id3["TXXX:Artists"].text == ["A", "B"] and id3.version[:2] == (2, 4)


# ── Comma Artist Splitter ───────────────────────────────────────────────

class Worker:
    transfer_folder = None

    def __init__(self, settings):
        outer = settings

        class Cfg:
            def get(self, key, default=None):
                return outer if key == "repair.jobs.comma_artist_splitter.settings" else default

        self._config_manager = Cfg()


def _split(tmp_path, settings, monkeypatch, albumartist="A, B"):
    """Run the fork's part of the fix on a file in the state upstream leaves it."""
    import core.library.path_resolver as resolver

    monkeypatch.setattr(resolver, "resolve_library_file_path", lambda p, **kw: p)
    path = _flac(tmp_path, artist="A, B", albumartist=albumartist)
    details = {"combined_name": "A, B", "split_artists": ["A", "B"], "all_files": [{"file_path": path}]}
    worker = Worker(settings)
    before = comma_split.album_artist_files(worker, details)
    audio = FLAC(path)                       # what upstream's fix writes
    audio["artist"], audio["artists"] = ["A; B"], ["A", "B"]
    if albumartist == "A, B":
        audio["albumartist"] = ["A"]
    audio.save()
    result = comma_split.apply_strategy(worker, details, before, {"success": True, "action": "artists_split",
                                                                  "message": "Re-tagged 1 file(s)", "fixed": 1})
    return FLAC(path), result


def test_splitter_writes_separate_tags_by_default_and_cascades_to_album_artist(tmp_path, monkeypatch):
    audio, result = _split(tmp_path, {}, monkeypatch)
    assert audio["artist"] == ["A", "B"] and audio["albumartist"] == ["A", "B"]
    assert audio["artists"] == ["A", "B"]
    assert "separate tags" in result["message"] and result["fixed"] == 1


def test_splitter_with_tag_splitting_off_uses_its_separator(tmp_path, monkeypatch):
    audio, result = _split(tmp_path, {"split_into_separate_tags": False, "separator": "comma"}, monkeypatch)
    assert audio["artist"] == ["A, B"] and audio["albumartist"] == ["A, B"]
    assert audio["artists"] == ["A", "B"] and '"A, B"' in result["message"]


def test_splitter_ignores_the_separator_while_tag_splitting_is_on(tmp_path, monkeypatch):
    audio, _ = _split(tmp_path, {"split_into_separate_tags": True, "separator": "ampersand"}, monkeypatch)
    assert audio["artist"] == ["A", "B"]


def test_splitter_leaves_a_different_album_artist_alone(tmp_path, monkeypatch):
    audio, _ = _split(tmp_path, {}, monkeypatch, albumartist="Various Artists")
    assert audio["artist"] == ["A", "B"] and audio["albumartist"] == ["Various Artists"]


def test_splitter_job_exposes_the_settings_with_tag_splitting_on_by_default():
    from core.repair_jobs.comma_artist_splitter import CommaArtistSplitterJob
    from core.repair_worker import RepairWorker

    assert CommaArtistSplitterJob.default_settings["split_into_separate_tags"] is True
    assert CommaArtistSplitterJob.default_settings["separator"] == "semicolon"
    assert CommaArtistSplitterJob.setting_options["separator"] == ["semicolon", "comma", "slash", "ampersand", "custom"]
    assert CommaArtistSplitterJob.default_settings["custom_separator"] == ""
    assert "、" in CommaArtistSplitterJob.default_settings["extra_splitters"].split()
    assert CommaArtistSplitterJob.default_settings["dry_run"] is True      # upstream settings intact
    assert RepairWorker._fix_comma_artist_split.__name__ == "_fork_fix_comma_artist_split"


def test_splitter_scan_treats_separate_values_as_already_split(tmp_path):
    from core.repair_jobs.comma_artist_splitter import file_already_split

    path = _flac(tmp_path, artist=["A", "B"], artists=["A", "B"])
    assert file_already_split(MutagenFile(path), ["A", "B"]) is True


def test_upstream_fix_then_fork_strategy_end_to_end(tmp_path, monkeypatch):
    """The real RepairWorker fix, wrapped: upstream verifies and re-tags, the
    fork then stores the split as separate values."""
    import core.library.path_resolver as resolver
    from core.repair_worker import RepairWorker

    monkeypatch.setattr(resolver, "resolve_library_file_path", lambda p, **kw: p)
    path = _flac(tmp_path, artist="A, B", albumartist="A, B")
    worker = RepairWorker.__new__(RepairWorker)
    worker.transfer_folder = None
    worker._config_manager = Worker({})._config_manager
    details = {"combined_name": "A, B", "split_artists": ["A", "B"], "primary_artist": "A",
               "new_display_artist": "A; B", "all_files": [{"file_path": path}]}
    result = worker._fix_comma_artist_split("track", "A, B", None, details)
    assert result["success"] and result["action"] == "artists_split"
    audio = FLAC(path)
    assert audio["artist"] == ["A", "B"] and audio["albumartist"] == ["A", "B"] and audio["artists"] == ["A", "B"]
    # running the fix again finds nothing left to do and changes nothing
    again = worker._fix_comma_artist_split("track", "A, B", None, details)
    assert again["success"] and FLAC(path)["artist"] == ["A", "B"]


# ── configurable separators ─────────────────────────────────────────────

def test_cjk_separators_are_detected_by_default():
    parts = artist_format.raw_parts
    assert parts("周杰倫、方文山") == ["周杰倫", "方文山"]
    assert parts("周杰倫／方文山") == ["周杰倫", "方文山"]
    assert parts("A，B；C") == ["A", "B", "C"]
    assert parts("ヨルシカ×米津玄師") == ["ヨルシカ", "米津玄師"]
    assert parts("A • B") == ["A", "B"] and parts("A・B") == ["A", "B"] and parts("A＆B") == ["A", "B"]
    assert parts("A ｘ B") == ["A", "B"] and parts("A | B") == ["A", "B"]
    # still whole: no spaces around a letter/ASCII symbol, or not a separator at all
    assert parts("AxB") == ["AxB"] and parts("A|B") == ["A|B"] and parts("AC/DC") == ["AC/DC"]
    assert parts("乔治·马丁") == ["乔治·马丁"]            # the Chinese interpunct is not a default


def test_a_katakana_name_with_a_middle_dot_is_one_artist():
    assert artist_format.raw_parts("マイケル・ジャクソン") == ["マイケル・ジャクソン"]
    assert artist_format.raw_parts("周杰倫、マイケル・ジャクソン") == ["周杰倫", "マイケル・ジャクソン"]
    assert artist_format.raw_parts("米津玄師・ヨルシカ") == ["米津玄師", "ヨルシカ"]   # not all-katakana: a real separator


def test_user_can_add_and_remove_detected_separators(fork_env):
    fork_env.set("fork.artists.detect", "· と ~~")
    parts = artist_format.raw_parts
    assert parts("乔治·马丁") == ["乔治", "马丁"]         # added
    assert parts("A と B") == ["A", "B"] and parts("AとB") == ["AとB"]   # a word needs spaces
    assert parts("A~~B") == ["A~~B"] and parts("A ~~ B") == ["A", "B"]  # ASCII symbol needs spaces
    assert parts("周杰倫、方文山") == ["周杰倫、方文山"]     # removed from the list
    assert parts("A, B & C") == ["A", "B", "C"]            # built-ins always apply
    fork_env.set("fork.artists.detect", "")
    assert parts("A×B") == ["A×B"] and parts("A; B") == ["A", "B"]


def test_regex_special_characters_are_safe_to_add(fork_env):
    fork_env.set("fork.artists.detect", "] [ \\ ^ - . * ( )")
    assert artist_format.raw_parts("A ] B") == ["A", "B"]
    assert artist_format.raw_parts("A.B") == ["A.B"] and artist_format.raw_parts("A . B") == ["A", "B"]


def test_custom_output_separator(fork_env):
    fork_env.set("fork.artists.split_tags", False)
    fork_env.set("fork.artists.separator", "custom")
    for chars, text in (("、", "A、B"), ("／", "A／B"), ("|", "A | B"), ("•", "A • B"), (",", "A, B"), ("x", "A x B")):
        fork_env.set("fork.artists.custom_separator", chars)
        assert artist_format.tag_values(["A", "B"]) == [text]
    fork_env.set("fork.artists.custom_separator", "")
    assert artist_format.tag_values(["A", "B"]) == ["A; B"]          # empty custom falls back
    # the custom separator is itself detected again when it is a known one, so a
    # second pass over the file is a no-op
    fork_env.set("fork.artists.custom_separator", "、")
    assert artist_format.raw_parts("A、B") == ["A", "B"]


def test_download_pass_splits_cjk_credits_and_writes_a_custom_separator(tmp_path, fork_env):
    path = _flac(tmp_path, artist="周杰倫、方文山／Lara")
    tags.apply_to_file(path)
    assert FLAC(path)["artist"] == ["周杰倫", "方文山", "Lara"]
    fork_env.set("fork.artists.split_tags", False)
    fork_env.set("fork.artists.separator", "custom")
    fork_env.set("fork.artists.custom_separator", "、")
    tags.apply_to_file(path)
    assert FLAC(path)["artist"] == ["周杰倫、方文山、Lara"]
    assert tags.apply_to_file(path) == {}


def test_splitter_job_custom_separator_and_extra_scan_characters(tmp_path, monkeypatch):
    from core.repair_jobs.comma_artist_splitter import CommaArtistSplitterJob, split_artist_parts

    audio, result = _split(tmp_path, {"split_into_separate_tags": False, "separator": "custom",
                                      "custom_separator": "、"}, monkeypatch)
    assert audio["artist"] == ["A、B"] and audio["albumartist"] == ["A、B"] and "A、B" in result["message"]
    job = CommaArtistSplitterJob()
    symbols = job._get_symbols({**job.default_settings})
    assert symbols[:4] == [",", ";", "/", "&"] and "、" in symbols and "／" in symbols and "×" in symbols
    assert split_artist_parts("周杰倫、方文山／Lara×A", symbols) == ["周杰倫", "方文山", "Lara", "A"]
    # unsafe-in-a-character-class and multi-character entries are dropped, not passed to upstream's regex
    risky = job._get_symbols({**job.default_settings, "extra_splitters": "] ^ - \\\\ ab x ｜"})
    assert risky == [",", ";", "/", "&", "｜"]
    assert split_artist_parts("A｜B", risky) == ["A", "B"]
    # switching an upstream toggle off still works alongside the extras
    assert "," not in job._get_symbols({**job.default_settings, "comma_splitter": False})
