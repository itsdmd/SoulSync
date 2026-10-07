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
    # MusicBrainz stand-in: knows these exact names as single artists
    known = {"simon & garfunkel", "tyler, the creator", "chloe x halle"}

    class FakeMB:
        def search_artist(self, name, **kwargs):
            return [{"name": name, "score": 100, "aliases": []}] if name.casefold() in known else []

    from core.fork import artist_names
    monkeypatch.setattr(artist_names, "_mb_client", lambda: FakeMB())
    artist_names._single_artist_cache.clear()
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
    assert data["tracks"][2]["proposed"]["artist"] == "Jay Chou; Lara"
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


def test_search_folders_finds_albums_by_any_words_across_the_library(library):
    _flac(str(library / "Jay Chou" / "Jay Chou - November's Chopin (十一月的蕭邦)" / "01.flac"))
    _flac(str(library / "Jay Chou" / "Jay Chou - Fantasy" / "01.flac"))
    _flac(str(library / "Björk" / "Björk - Post" / "Disc 1" / "01.flac"))
    (library / ".hidden" / "chopin secret").mkdir(parents=True)

    def rels(query):
        return [r["rel"] for r in album_tagging.search_folders(query)["results"]]

    assert rels("chopin") == ["Jay Chou/Jay Chou - November's Chopin (十一月的蕭邦)"]
    assert rels("十一月") == ["Jay Chou/Jay Chou - November's Chopin (十一月的蕭邦)"]
    # every word must match, in any order, across the path; accents and case ignored
    assert rels("FANTASY chou") == ["Jay Chou/Jay Chou - Fantasy"]
    assert rels("bjork post") == ["Björk/Björk - Post"]
    assert rels("chopin bjork") == []
    # folders holding audio rank above their artist folder
    assert rels("jay chou") == ["Jay Chou/Jay Chou - Fantasy",
                                "Jay Chou/Jay Chou - November's Chopin (十一月的蕭邦)", "Jay Chou"]
    # a parent matching every word does not drag all of its sub-folders in
    # ("Disc 1" is not listed: its own name matches nothing)
    assert rels("bjork") == ["Björk", "Björk/Björk - Post"]
    assert rels("   ") == []
    assert all(r["path"].startswith(os.path.realpath(str(library))) for r in album_tagging.search_folders("o")["results"])


def test_search_folders_stops_at_the_limit(library):
    for i in range(6):
        (library / f"Artist {i}").mkdir()
    data = album_tagging.search_folders("artist", limit=4)
    assert len(data["results"]) == 4 and data["truncated"] is True


def test_split_credit_handles_the_common_separators(library):
    split = album_tagging.split_credit
    assert split("A, B & C") == ["A", "B", "C"]
    assert split("A feat. B") == ["A", "B"] and split("A FT. B") == ["A", "B"] and split("A ft B") == ["A", "B"]
    assert split("A (feat. B)") == ["A", "B"] and split("A featuring B") == ["A", "B"]
    assert split("A、B；C") == ["A", "B", "C"] and split("A / B") == ["A", "B"] and split("A x B") == ["A", "B"]
    assert split("A; B") == ["A", "B"]
    # not separators: no surrounding spaces, or part of a word
    assert split("AC/DC") == ["AC/DC"] and split("Daft Punk") == ["Daft Punk"] and split("Lil Nas X") == ["Lil Nas X"]
    assert split("Florence + the Machine") == ["Florence", "the Machine"]  # unknown to the stand-in: split


def test_a_known_single_artist_is_never_split(library):
    assert album_tagging.split_credit("Simon & Garfunkel") == ["Simon & Garfunkel"]
    assert album_tagging.split_credit("Tyler, The Creator") == ["Tyler, The Creator"]
    assert album_tagging.split_credit("Chloe x Halle") == ["Chloe x Halle"]
    store.save_artist_name("Florence + the Machine", "Florence + the Machine", "manual")
    assert album_tagging.split_credit("Florence + the Machine") == ["Florence + the Machine"]


def test_proposals_use_semicolons_for_artist_and_album_artist(library):
    album = {"name": "Collab", "release_date": "2020"}
    tracks = [
        {"name": "One", "artists": [{"name": "周杰倫"}, {"name": "Lara & Friends"}], "track_number": 1},
        {"name": "Two", "artists": [{"name": "Simon & Garfunkel"}, {"name": "周杰倫 feat. Lara"}], "track_number": 2},
        {"name": "Three", "artists": [], "track_number": 3},
    ]
    props = album_tagging.proposals(album, {"name": "周杰倫, Lara"}, tracks, apply_rules=True)
    assert props[0]["proposed"]["artist"] == "Jay Chou; Lara; Friends"
    assert props[1]["proposed"]["artist"] == "Simon & Garfunkel; Jay Chou; Lara"   # deduplicated, rule applied
    assert props[2]["proposed"]["artist"] == "Jay Chou; Lara"                       # falls back to the album artist
    assert all(p["proposed"]["albumartist"] == "Jay Chou; Lara" for p in props)
    # without rules: same splitting, original names
    raw = album_tagging.proposals(album, {"name": "周杰倫, Lara"}, tracks, apply_rules=False)
    assert raw[0]["proposed"]["artist"] == "周杰倫; Lara; Friends"
    # option off: the source's own formatting is kept
    off = album_tagging.proposals(album, {"name": "周杰倫, Lara"}, tracks, apply_rules=False, semicolons=False)
    assert off[0]["proposed"]["artist"] == "周杰倫, Lara & Friends" and off[0]["proposed"]["albumartist"] == "周杰倫, Lara"


def test_musicbrainz_outage_leaves_an_ambiguous_credit_whole(library, monkeypatch):
    from core.fork import artist_names

    class Down:
        def search_artist(self, name, **kwargs):
            raise RuntimeError("timeout")

    monkeypatch.setattr(artist_names, "_mb_client", lambda: Down())
    artist_names._single_artist_cache.clear()
    assert album_tagging.split_credit("Earth, Wind & Fire") == ["Earth, Wind & Fire"]


def test_apply_stores_artists_per_the_strategy(library, fork_env):
    folder = library / "Multi"
    path = _flac(str(folder / "01.flac"), artist="old")
    row = {"rel": "01.flac", "track": 2,
           "tags": {"title": "Duet", "artist": "Jay Chou; Lara", "albumartist": "Jay Chou; Lara",
                    "album": "X", "track_number": 3, "disc_number": 1}}
    album_tagging.apply(str(folder), [row], ALBUM, ARTIST, TRACKS)
    audio = FLAC(path)
    assert audio["artist"] == ["Jay Chou", "Lara"] and audio["albumartist"] == ["Jay Chou", "Lara"]
    assert audio["artists"] == ["Jay Chou", "Lara"]
    # chosen separator: the proposal is shown joined, and written as typed
    fork_env.set("fork.artists.split_tags", False)
    fork_env.set("fork.artists.separator", "comma")
    props = album_tagging.proposals(ALBUM, ARTIST, TRACKS, apply_rules=True)
    assert props[2]["proposed"]["artist"] == "Jay Chou, Lara"
    row["tags"]["artist"] = row["tags"]["albumartist"] = "Jay Chou, Lara"
    album_tagging.apply(str(folder), [row], ALBUM, ARTIST, TRACKS)
    audio = FLAC(path)
    assert audio["artist"] == ["Jay Chou, Lara"] and audio["albumartist"] == ["Jay Chou, Lara"]
    # "separate multiple artists" unticked in the dialog: text is written verbatim
    fork_env.set("fork.artists.split_tags", True)
    row["tags"]["artist"] = "Jay Chou; Lara"
    album_tagging.apply(str(folder), [row], ALBUM, ARTIST, TRACKS, separate_artists=False)
    assert FLAC(path)["artist"] == ["Jay Chou; Lara"]


# ── the preview never waits for the model ───────────────────────────────

def test_preview_uses_saved_translations_only_and_reports_the_rest(library, llm):
    folder = library / "E"
    _flac(str(folder / "01.flac"), title="夜曲", tracknumber=1)
    tracks = TRACKS + [{"id": "t4", "name": "不存在的樂園", "artists": [{"name": "周杰倫"}], "track_number": 4},
                       {"id": "t5", "name": "不存在的樂園 (Live)", "artists": [{"name": "周杰倫"}], "track_number": 5}]
    data = album_tagging.preview(str(folder), ALBUM, ARTIST, tracks)
    assert llm.calls == []                                             # no model call at all
    assert data["tracks"][0]["proposed"]["title"] == "Nocturne (夜曲)"   # saved translation applied
    assert data["tracks"][3]["proposed"]["title"] == "不存在的樂園"        # unknown: left as is…
    assert data["untranslated"] == [{"kind": "title", "original": "不存在的樂園", "artist": "周杰倫",
                                     "album": "十一月的蕭邦"}]            # …and reported once
    # rules off: nothing to translate
    assert album_tagging.preview(str(folder), ALBUM, ARTIST, tracks, apply_rules=False)["untranslated"] == []


def test_background_translation_warms_the_model_then_batches(library, llm, monkeypatch):
    import time

    from core.fork import ollama

    warmed = []
    monkeypatch.setattr(ollama, "warm", lambda task, model=None: warmed.append(task) or True)
    llm.replies = [{"translations": [{"id": 1, "translation": "The Nonexistent Paradise"},
                                     {"id": 2, "translation": "Judgement Day"}]}]
    job = album_tagging.start_translate([
        {"kind": "title", "original": "不存在的樂園", "artist": "A"},
        {"kind": "title", "original": "裁決日", "artist": "A"},
        {"kind": "bogus", "original": "x"}, {"kind": "title", "original": " "}, "junk",
    ])
    assert job["running"] is True and job["total"] == 2
    for _ in range(200):
        status = album_tagging.translate_status()
        if not status["running"]:
            break
        time.sleep(0.02)
    assert status["error"] is None and status["translated"] == 2 and status["done"] == 2
    assert warmed == ["names"] and len(llm.calls) == 1                 # one request for both names
    assert store.get_translation("title", "裁決日")["translated"] == "Judgement Day"


def test_background_translation_reports_a_model_that_will_not_load(library, llm, monkeypatch):
    import time

    from core.fork import ollama

    monkeypatch.setattr(ollama, "warm", lambda task, model=None: False)
    album_tagging.start_translate([{"kind": "title", "original": "裁決日"}])
    for _ in range(200):
        status = album_tagging.translate_status()
        if not status["running"]:
            break
        time.sleep(0.02)
    assert "could not be loaded" in status["error"] and llm.calls == []


# ── matching must stay fast on a big album ──────────────────────────────

def test_preview_of_a_hundred_track_album_is_quick_and_touches_the_database_little(library, monkeypatch):
    import time

    folder = library / "Big"
    count = 100
    tracks = []
    for n in range(1, count + 1):
        original = f"歌曲第{n}號"
        # half the files carry an old translation, half only the original; numbering is off by one
        title = f"Old Song {n} ({original})" if n % 2 else original
        _flac(str(folder / f"{n:03d} - {title}.flac"), title=title, tracknumber=n + 1)
        tracks.append({"id": f"t{n}", "name": original, "artists": [{"name": "周杰倫"}],
                       "track_number": n, "disc_number": 1})
    lookups = []
    real = store.get_translation
    monkeypatch.setattr(store, "get_translation", lambda kind, original: lookups.append(original) or real(kind, original))
    started = time.time()
    data = album_tagging.preview(str(folder), ALBUM, ARTIST, tracks)
    elapsed = time.time() - started
    assert elapsed < 5, f"preview took {elapsed:.1f}s"
    # every file found its own track by name, despite the wrong numbers
    wrong = [r["rel"] for r in data["rows"] if r["track"] is None or tracks[r["track"]]["name"] not in r["rel"]]
    assert wrong == []
    # a few lookups per track, not one per file-track pair (that was 10,000+)
    assert len(lookups) < count * 8


def test_name_keys_recognise_the_same_name_across_forms():
    keys, score_ = album_tagging.name_keys, album_tagging._keys_score
    assert score_(keys("夜曲"), keys("Nocturne (夜曲)")) == 1.0          # original inside a translated name
    assert score_(keys("Nocturne"), keys("Nocturne (夜曲)")) == 1.0      # the translation it carries
    assert score_(keys("相變臨界"), keys("Critical (相变临界)")) == 1.0          # other Chinese script
    assert score_(keys("夜曲 (Live)"), keys("Nocturne (夜曲)")) == 0.9   # same song, different recording
    assert score_(keys("夜曲 (Live)"), keys("Nocturne (夜曲) (Live)")) == 1.0
    assert score_(keys("夜曲"), keys("髮如雪")) < 0.5
    assert score_(keys(""), keys("夜曲")) == 0.0


class _EmptyLibrary:
    """A library database that knows no track."""

    def __init__(self):
        self.asked = []

    def check_track_exists(self, title, artist, **kwargs):
        self.asked.append(title)
        return None, 0.0


def _album_on_disk(library, name="Chosen"):
    folder = library / name
    _flac(str(folder / "01 - Nocturne (夜曲).flac"), title="Nocturne (夜曲)", tracknumber=1)
    _flac(str(folder / "02 - whatever.flac"), title="Hair Like Snow (髮如雪)", tracknumber=2)
    return str(folder)


def test_a_saved_folder_is_used_for_the_album_from_then_on(library, monkeypatch):
    monkeypatch.setattr(album_tagging.ownership, "find_by_external_id", lambda *a, **k: None)
    folder = _album_on_disk(library)
    db = _EmptyLibrary()
    before = album_tagging.check_album(db, ALBUM, ARTIST, TRACKS, source="spotify")
    assert before["folder"] == "" and before["folder_saved"] is False and before["found"] == 0

    assert album_tagging.save_folder("spotify", ALBUM, ARTIST, folder) == os.path.realpath(folder)
    db.asked.clear()
    after = album_tagging.check_album(db, ALBUM, ARTIST, TRACKS, source="spotify")
    assert after["folder"] == os.path.realpath(folder) and after["folder_saved"] is True
    assert [t["found"] for t in after["tracks"]] == [True, True, False]
    assert after["tracks"][0]["file"].endswith("01 - Nocturne (夜曲).flac")
    # the library is only asked about the track the folder does not hold
    assert set(db.asked) == {"Duet"}


def test_a_saved_folder_is_found_from_another_source_by_name_and_can_be_forgotten(library, monkeypatch):
    monkeypatch.setattr(album_tagging.ownership, "find_by_external_id", lambda *a, **k: None)
    folder = _album_on_disk(library)
    album_tagging.save_folder("spotify", ALBUM, ARTIST, folder)
    same_album_elsewhere = dict(ALBUM, id="deezer-77")
    assert album_tagging.saved_folder("deezer", same_album_elsewhere, ARTIST)["folder"] == os.path.realpath(folder)
    assert album_tagging.saved_folder("spotify", dict(ALBUM, id="x", name="Other"), ARTIST)["folder"] == ""

    assert album_tagging.save_folder("deezer", same_album_elsewhere, ARTIST, "") == ""
    assert album_tagging.saved_folder("spotify", ALBUM, ARTIST) == {"folder": "", "missing": ""}


def test_a_saved_folder_that_is_gone_or_outside_the_library_is_not_used(library, tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(album_tagging.ownership, "find_by_external_id", lambda *a, **k: None)
    with pytest.raises((ValueError, PermissionError, FileNotFoundError)):
        album_tagging.save_folder("spotify", ALBUM, ARTIST, str(tmp_path / "elsewhere"))
    folder = _album_on_disk(library)
    album_tagging.save_folder("spotify", ALBUM, ARTIST, folder)
    shutil.rmtree(folder)
    result = album_tagging.check_album(_EmptyLibrary(), ALBUM, ARTIST, TRACKS, source="spotify")
    assert result["folder"] == "" and result["folder_saved"] is False
    assert result["saved_missing"] == os.path.realpath(folder)


def test_a_saved_folder_follows_the_files_when_tagging_moves_them(library, monkeypatch):
    folder = _album_on_disk(library)
    album_tagging.save_folder("spotify", ALBUM, ARTIST, folder)
    new_home = os.path.join(os.path.realpath(str(library)), "Jay Chou", "Album")
    monkeypatch.setattr(album_tagging, "_template_path",
                        lambda root, values, album, total, discs, ext: os.path.join(
                            new_home, f"{values['track_number']}{ext}"))
    rows = [{"rel": "01 - Nocturne (夜曲).flac", "track": 0, "tags": {"title": "Nocturne", "track_number": 1}},
            {"rel": "02 - whatever.flac", "track": 1, "tags": {"title": "Hair Like Snow", "track_number": 2}}]
    out = album_tagging.apply(folder, rows, ALBUM, ARTIST, TRACKS, source="spotify", rename=True)
    assert out["moved"] == 2 and out["folder"] == new_home
    assert album_tagging.saved_folder("spotify", ALBUM, ARTIST)["folder"] == new_home


def test_discography_count_uses_the_saved_folder_and_never_lowers(library):
    folder = _album_on_disk(library)
    card = {"id": "al1", "name": ALBUM["name"], "total_tracks": 3}
    missing = {"id": "al1", "status": "missing", "owned_tracks": 0, "expected_tracks": 3, "completion_percentage": 0}
    # nothing saved: untouched
    assert album_tagging.completion_from_saved_folder(missing, card, "周杰倫", "spotify") is missing

    album_tagging.save_folder("spotify", ALBUM, ARTIST, folder)
    # asked without a source and under the library's name for the artist: found by id
    out = album_tagging.completion_from_saved_folder(missing, card, "Jay Chou", None)
    assert (out["owned_tracks"], out["status"], out["completion_percentage"]) == (2, "partial", 66.7)
    _flac(os.path.join(folder, "Disc 2", "03.flac"), title="Duet")
    _flac(os.path.join(folder, "Disc 2", "04 extra.flac"), title="Extra")
    out = album_tagging.completion_from_saved_folder(missing, card, "周杰倫", "spotify")
    assert (out["owned_tracks"], out["status"]) == (3, "completed")   # capped at the album's size
    done = dict(missing, status="completed", owned_tracks=3)
    assert album_tagging.completion_from_saved_folder(done, card, "周杰倫", "spotify") is done


def test_completion_check_is_wrapped_and_counts_the_saved_folder(library, monkeypatch):
    from core.metadata import completion

    folder = _album_on_disk(library)
    album_tagging.save_folder("spotify", ALBUM, ARTIST, folder)
    monkeypatch.setattr(completion, "_upstream_check_album_completion", lambda db, album, artist, source=None, *a, **k: {
        "id": album["id"], "status": "missing", "owned_tracks": 0, "expected_tracks": 3, "completion_percentage": 0})
    out = completion.check_album_completion(None, {"id": "al1", "name": ALBUM["name"], "total_tracks": 3}, "周杰倫",
                                            source_override="spotify", candidate_albums=[])
    assert out["owned_tracks"] == 2 and out["status"] == "partial"
