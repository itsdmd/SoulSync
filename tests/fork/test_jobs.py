"""The fork's Tools-page jobs: Auto Translate and Album Volume Grouping."""

import os
import sqlite3

import pytest
from mutagen.flac import FLAC

from core.fork import album_tagging, jobs, ollama, retro, store, tags, translate
from core.repair_jobs import fork_tools

_MINIMAL_FLAC = (
    b"fLaC" + b"\x80\x00\x00\x22" + b"\x00\x10\x00\x10" + b"\x00\x00\x00\x00\x00\x00"
    + b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16
)


class FakeDB:
    def __init__(self, path):
        self.path = path
        conn = self._get_connection()
        conn.executescript(
            "CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, artist_id INTEGER, title TEXT, year INTEGER);"
            "CREATE TABLE tracks (id INTEGER PRIMARY KEY, album_id INTEGER, artist_id INTEGER, title TEXT,"
            " track_number INTEGER, duration INTEGER, file_path TEXT);")
        conn.commit()
        conn.close()
        self.ids = {}

    def _get_connection(self):
        return sqlite3.connect(self.path)

    def add(self, artist, album, title, number=1, file_path=""):
        conn = self._get_connection()
        aid = self.ids.get(("ar", artist)) or conn.execute("INSERT INTO artists (name) VALUES (?)", (artist,)).lastrowid
        self.ids[("ar", artist)] = aid
        alid = self.ids.get(("al", artist, album)) or conn.execute(
            "INSERT INTO albums (artist_id, title, year) VALUES (?, ?, 2020)", (aid, album)).lastrowid
        self.ids[("al", artist, album)] = alid
        conn.execute("INSERT INTO tracks (album_id, artist_id, title, track_number, duration, file_path)"
                     " VALUES (?, ?, ?, ?, 1000, ?)", (alid, aid, title, number, file_path))
        conn.commit()
        conn.close()
        return alid


def _flac(path, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(_MINIMAL_FLAC)
    audio = FLAC(path)
    for key, value in fields.items():
        audio[key] = [str(value)]
    audio.save()
    return path


class Ctx:
    """The parts of JobContext the jobs use."""

    def __init__(self, db, settings=None, job_id=""):
        self.db = db
        self.findings = []
        self.progress = []
        stored = settings or {}

        class Cfg:
            def get(self, key, default=None):
                return stored if key == f"repair.jobs.{job_id}.settings" else default

        self.config_manager = Cfg()
        self.update_progress = lambda done, total: None

    def check_stop(self):
        return False

    def report_progress(self, **kwargs):
        self.progress.append(kwargs)

    def create_finding(self, **kwargs):
        if any(f["entity_id"] == kwargs["entity_id"] for f in self.findings):
            return False
        self.findings.append(kwargs)
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(album_tagging, "allowed_roots", lambda: [os.path.realpath(str(root))])
    monkeypatch.setattr(album_tagging, "_resolve", lambda p: p if p and os.path.isfile(p) else None)
    monkeypatch.setattr(tags, "_save", lambda audio: audio.save())
    db = FakeDB(str(tmp_path / "lib.db"))

    def moved(old, new):
        conn = db._get_connection()
        conn.execute("UPDATE tracks SET file_path = ? WHERE file_path = ?", (new, old))
        conn.commit()
        conn.close()
    monkeypatch.setattr(retro, "_update_db_path", moved)
    return {"db": db, "root": root}


# ── batch translation ───────────────────────────────────────────────────

def _batch(*texts):
    return {"translations": [{"id": i, "translation": t} for i, t in enumerate(texts, 1)]}


def test_batch_sends_many_names_per_request_and_skips_known_ones(llm):
    store.save_translation("title", "已知", "Known", user_edited=True)
    items = [{"original": name, "artist": "A"} for name in ["已知", "一", "二", "三", "四", "五", "一"]]
    llm.replies = [_batch("One", "Two"), _batch("Three", "Four"), _batch("Five")]
    done = translate.translate_batch("title", items, batch_size=2)
    assert done == {"已知": "Known", "一": "One", "二": "Two", "三": "Three", "四": "Four", "五": "Five"}
    assert [len(payload["items"]) for _task, payload in llm.calls] == [2, 2, 1]   # 5 new names, 3 requests
    assert llm.calls[0][1]["items"][0] == {"id": 1, "original": "一", "artist": "A"}
    assert store.get_translation("title", "三")["translated"] == "Three"


def test_a_bad_answer_in_a_batch_is_retried_alone_and_does_not_sink_the_rest(llm):
    items = [{"original": "一"}, {"original": "二"}, {"original": "三"}]
    llm.replies = [
        {"translations": [{"id": 1, "translation": "One"}, {"id": 2, "translation": "二"}]},  # 2 invalid, 3 missing
        {"translations": [{"id": 1, "translation": "Two"}]},     # retry of 二
        {"translations": [{"id": 1, "translation": ""}]},        # retry of 三: still nothing…
        {"translations": [{"id": 1, "translation": ""}]},        # …twice
    ]
    done = translate.translate_batch("title", items, batch_size=10)
    assert done == {"一": "One", "二": "Two"} and store.get_translation("title", "三") is None


def test_batch_stops_when_the_model_server_goes_down(llm, monkeypatch):
    def fail(*args, **kwargs):
        ollama._mark_down("refused")
        raise ollama.OllamaError("down")
    monkeypatch.setattr(ollama, "chat_json", fail)
    items = [{"original": name} for name in "一二三四五六"]
    assert translate.translate_batch("title", items, batch_size=2) == {}
    ollama.reset_cooldown()


# ── auto translate job ──────────────────────────────────────────────────

def _library(env):
    db, root = env["db"], env["root"]
    folder = root / "周杰倫" / "周杰倫 - 十一月的蕭邦"
    paths = {}
    for n, title in enumerate(["夜曲", "髮如雪"], 1):
        paths[title] = _flac(str(folder / f"0{n} - {title}.flac"), title=title, album="十一月的蕭邦")
        db.add("周杰倫", "十一月的蕭邦", title, n, paths[title])
    done = _flac(str(root / "X" / "Y" / "01 - Done (完了).flac"), title="Done (完了)", album="Y")
    db.add("X", "Y", "Done (完了)", 1, done)             # already translated
    db.add("Coldplay", "Parachutes", "Yellow", 1, "")     # nothing to translate
    db.add("X", "Y", "夜曲 (Live)", 2, "")                # same name, decorated: counted once
    return paths


def test_untranslated_names_are_consolidated(env):
    _library(env)
    names = jobs.untranslated_names(env["db"])
    assert [(n["original"], n["count"]) for n in names["album"]] == [("十一月的蕭邦", 2)]
    assert sorted((n["original"], n["count"]) for n in names["title"]) == [("夜曲", 2), ("髮如雪", 1)]
    assert jobs.untranslated_names(env["db"], albums=False)["album"] == []


def test_dry_run_translates_in_batches_and_creates_findings_only(env, llm):
    paths = _library(env)
    llm.replies = [_batch("November's Chopin"), _batch("Nocturne", "Hair Like Snow")]
    ctx = Ctx(env["db"])
    result = fork_tools.AutoTranslateJob().scan(ctx)
    assert len(llm.calls) == 2                                    # one request per kind, not per name
    assert result.findings_created == 3 and result.auto_fixed == 0
    finding = next(f for f in ctx.findings if f["entity_id"] == "title:夜曲")
    assert finding["finding_type"] == "fork_untranslated"
    assert finding["details"]["will_be_written_as"] == "Nocturne (夜曲)" and finding["details"]["library_entries"] == 2
    assert FLAC(paths["夜曲"])["title"] == ["夜曲"]               # nothing written yet
    # approving the finding applies whatever is saved at that moment
    store.save_translation("title", "夜曲", "Night Tune", user_edited=True)
    applied = jobs.apply_translation_finding(env["db"], finding["details"])
    assert applied["success"] and applied["fixed"] == 1
    new_path = str(env["root"] / "周杰倫" / "周杰倫 - 十一月的蕭邦" / "01 - Night Tune (夜曲).flac")
    assert FLAC(new_path)["title"] == ["Night Tune (夜曲)"]


def test_live_run_applies_and_honours_settings(env, llm):
    paths = _library(env)
    llm.replies = [_batch("Nocturne", "Hair Like Snow")]
    ctx = Ctx(env["db"], {"dry_run": False, "translate_albums": False, "rename_files": False, "batch_size": 10},
              "fork_auto_translate")
    result = fork_tools.AutoTranslateJob().scan(ctx)
    assert ctx.findings == [] and result.auto_fixed == 2
    assert FLAC(paths["夜曲"])["title"] == ["Nocturne (夜曲)"] and os.path.isfile(paths["夜曲"])   # not renamed
    assert FLAC(paths["夜曲"])["album"] == ["十一月的蕭邦"]                                        # albums were off
    # a second run finds nothing: the library rows still say 夜曲 only until a rescan, but the files are done
    assert jobs.apply_translation_finding(env["db"], {"kind": "title", "original": "夜曲"})["fixed"] == 0


def test_names_the_model_cannot_translate_are_skipped_not_failed(env, llm):
    _library(env)
    llm.replies = [_batch("November's Chopin")]        # titles get no usable answer at all
    ctx = Ctx(env["db"])
    result = fork_tools.AutoTranslateJob().scan(ctx)
    assert result.findings_created == 1 and result.skipped == 2 and result.errors == 0


# ── volume grouping ─────────────────────────────────────────────────────

def test_parse_volume_markers():
    parse = jobs.parse_volume
    assert parse("Genshin Impact - Footprints of the Traveler, Vol. 2") == ("Genshin Impact - Footprints of the Traveler", 2)
    assert parse("Best Hits Volume II") == ("Best Hits", 2)
    assert parse("Anthology (Disc 3)") == ("Anthology", 3) and parse("Anthology CD1") == ("Anthology", 1)
    assert parse("Symphony Pt. 4") == ("Symphony", 4) and parse("Symphony - Part 2") == ("Symphony", 2)
    assert parse("原神 第二卷") == ("原神", 2) and parse("原神 卷3") == ("原神", 3)
    # a translated name carries the marker twice; both go, and they must agree
    assert parse("Footprints of the Traveler Vol. 2 (旅行者的足跡 Vol.2)") == ("Footprints of the Traveler (旅行者的足跡)", 2)
    assert parse("Footprints Vol. 2 (足跡 Vol.3)") is None
    for title in ("Abbey Road", "Volume", "Revolver", "Part of Me", "ABCD 2", "Vol. 2"):
        assert parse(title) is None, title


def _volumes(env, count=3):
    db, root = env["db"], env["root"]
    paths = {}
    for vol in range(1, count + 1):
        album = f"Footprints of the Traveler, Vol. {vol}"
        folder = root / "HOYO-MiX" / f"HOYO-MiX - {album}"
        for n in (1, 2):
            path = _flac(str(folder / f"0{n} - Track {vol}{n}.flac"), title=f"Track {vol}{n}", album=album,
                         tracknumber=n, discnumber=1)
            db.add("HOYO-MiX", album, f"Track {vol}{n}", n, path)
            paths[(vol, n)] = path
        (folder / "cover.jpg").write_bytes(b"jpg%d" % vol)
    return paths


def test_volume_groups_need_two_volumes_one_artist_and_distinct_numbers(env):
    _volumes(env)
    env["db"].add("Someone Else", "Footprints of the Traveler, Vol. 9", "x", 1, "")      # other artist
    env["db"].add("Solo", "Lonely Album, Vol. 1", "x", 1, "")                              # a single volume
    env["db"].add("Dupe", "Twice, Vol. 1", "x", 1, "")
    env["db"].add("Dupe", "Twice Vol 1", "y", 1, "")                                       # same number twice
    groups = jobs.volume_groups(env["db"])
    assert [(g["artist"], g["album"], [v["number"] for v in g["volumes"]]) for g in groups] == [
        ("HOYO-MiX", "Footprints of the Traveler", [1, 2, 3])]


def test_grouping_retags_and_moves_into_disc_folders(env):
    paths = _volumes(env)
    ctx = Ctx(env["db"])
    result = fork_tools.VolumeGroupingJob().scan(ctx)
    assert result.findings_created == 1 and all(os.path.isfile(p) for p in paths.values())   # dry run
    details = ctx.findings[0]["details"]
    assert details["volume_numbers"] == "1, 2, 3" and details["track_count"] == 6
    applied = jobs.group_volumes(env["db"], details)
    assert applied["success"] and applied["fixed"] == 6
    album_dir = env["root"] / "HOYO-MiX" / "HOYO-MiX - Footprints of the Traveler"
    assert sorted(os.listdir(album_dir)) == ["Disc 1", "Disc 2", "Disc 3", "cover.jpg"]
    assert sorted(os.listdir(album_dir / "Disc 2")) == ["01 - Track 21.flac", "02 - Track 22.flac", "cover.jpg"]
    audio = FLAC(str(album_dir / "Disc 2" / "01 - Track 21.flac"))
    assert audio["album"] == ["Footprints of the Traveler"] and audio["discnumber"] == ["2"]
    assert audio["disctotal"] == ["3"] and audio["tracknumber"][0].split("/")[0] == "1"       # track numbers kept
    assert audio["title"] == ["Track 21"]
    assert sorted(os.listdir(env["root"] / "HOYO-MiX")) == ["HOYO-MiX - Footprints of the Traveler"]   # old folders gone


def test_grouping_tags_only(env):
    paths = _volumes(env, count=2)
    details = {"artist": "HOYO-MiX", "album": "Footprints of the Traveler",
               "volumes": jobs.volume_groups(env["db"])[0]["volumes"]}
    applied = jobs.group_volumes(env["db"], details, move_files=False)
    assert applied["fixed"] == 4 and all(os.path.isfile(p) for p in paths.values())
    assert FLAC(paths[(2, 1)])["discnumber"] == ["2"] and FLAC(paths[(2, 1)])["album"] == ["Footprints of the Traveler"]


def test_live_run_groups_without_findings(env):
    _volumes(env, count=2)
    ctx = Ctx(env["db"], {"dry_run": False, "move_files": True}, "fork_volume_grouping")
    result = fork_tools.VolumeGroupingJob().scan(ctx)
    assert ctx.findings == [] and result.auto_fixed == 4


def test_a_grouped_album_still_counts_as_owned_for_each_source_volume(env):
    """Sources keep listing "…, Vol. 2" as its own album."""
    from core.fork import hooks

    env["db"].add("HOYO-MiX", "Footprints of the Traveler", "Track 21", 1, "")
    calls = []

    def check(title, artist):
        calls.append(title)
        conn = env["db"]._get_connection()
        row = conn.execute("SELECT al.title FROM albums al JOIN artists ar ON ar.id = al.artist_id "
                           "WHERE al.title = ? AND ar.name = ?", (title, artist)).fetchone()
        conn.close()
        return (row[0], 1.0) if row else (None, 0.0)

    first = check("Footprints of the Traveler, Vol. 2", "HOYO-MiX")
    assert hooks.ownership_retry(env["db"], "album", "Footprints of the Traveler, Vol. 2", "HOYO-MiX",
                                 0.7, check, first) == ("Footprints of the Traveler", 1.0)
    # an ordinary missing album is not retried
    calls.clear()
    miss = check("Parachutes", "Coldplay")
    assert hooks.ownership_retry(env["db"], "album", "Parachutes", "Coldplay", 0.7, check, miss) == (None, 0.0)
    assert calls == ["Parachutes"]


def test_jobs_are_registered_with_handlers_labels_and_a_family():
    from core.repair_jobs import get_all_jobs
    from core.repair_worker import FINDING_TYPE_META, JOB_CATEGORIES, RepairWorker

    registry = get_all_jobs()
    assert registry["fork_auto_translate"].display_name == "Auto Translate"
    assert registry["fork_auto_translate"].default_settings["batch_size"] == 10
    assert registry["fork_volume_grouping"].display_name == "Album Volume Grouping"
    worker = RepairWorker.__new__(RepairWorker)
    handlers = worker._fix_handlers()
    assert {"fork_untranslated", "fork_album_volumes"} <= set(handlers) and "comma_artist_split" in handlers
    assert FINDING_TYPE_META["fork_album_volumes"]["verb"] == "Group Volumes"
    assert JOB_CATEGORIES["fork_auto_translate"] == "Tags & metadata"


def test_volume_markers_come_in_many_shapes():
    parse = jobs.parse_volume
    for title in ("Star Wars Epic Collection, Vol. 2", "Star Wars Epic Collection Vol. 4",
                  "Star Wars Epic Collection Vol, 4", "Star Wars Epic Collection (Vol. 3)",
                  "Star Wars Epic Collection - Volume 5", "Star Wars Epic Collection: Vol.6",
                  "Star Wars Epic Collection [Vol 7]"):
        assert parse(title)[0] == "Star Wars Epic Collection", title
    assert parse("Attack on Titan: Epic Collection, Vol. 3 (Cover)") == ("Attack on Titan: Epic Collection (Cover)", 3)


def test_first_volume_without_a_marker_and_uneven_names_are_grouped(env):
    db = env["db"]
    for title in ("Star Wars Epic Collection", "Star Wars Epic Collection, Vol. 2",
                  "Star Wars Epic Collection, Vol. 3", "Star Wars Epic Collection Vol, 4",
                  "Titan: Epic Collection", "Titan: Epic Collection, Vol. 2 (Cover)", "Titan Epic Collection (Vol. 3)",
                  "Jojo Epic Collection",                                    # nothing to join
                  "Hits", "Hits Vol. 1", "Hits Vol. 2"):                     # volume 1 is taken: "Hits" stays out
        db.add("Samuel Kim", title, "t", 1, "")
    db.add("Other", "Star Wars Epic Collection, Vol. 9", "t", 1, "")
    groups = {g["album"]: [(v["number"], v["title"], bool(v.get("assumed"))) for v in g["volumes"]]
              for g in jobs.volume_groups(db) if g["artist"] == "Samuel Kim"}
    assert groups["Star Wars Epic Collection"] == [
        (1, "Star Wars Epic Collection", True), (2, "Star Wars Epic Collection, Vol. 2", False),
        (3, "Star Wars Epic Collection, Vol. 3", False), (4, "Star Wars Epic Collection Vol, 4", False)]
    assert [v[:2] for v in groups["Titan Epic Collection"]] == [
        (1, "Titan: Epic Collection"), (2, "Titan: Epic Collection, Vol. 2 (Cover)"), (3, "Titan Epic Collection (Vol. 3)")]
    assert [v[0] for v in groups["Hits"]] == [1, 2] and len(groups) == 3


def _findings_table(env, monkeypatch, details):
    import json

    monkeypatch.setenv("DATABASE_PATH", env["db"].path)
    store._initialised.clear()
    conn = env["db"]._get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS repair_findings (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, "
                 "finding_type TEXT, status TEXT DEFAULT 'pending', entity_id TEXT, title TEXT, description TEXT, "
                 "details_json TEXT, updated_at TIMESTAMP)")
    conn.execute("INSERT INTO repair_findings (job_id, finding_type, entity_id, title, details_json) VALUES "
                 "('fork_volume_grouping', 'fork_album_volumes', 'HOYO-MiX:x', 'old', ?)", (json.dumps(details),))
    conn.commit()
    conn.close()

    def read():
        conn = env["db"]._get_connection()
        row = conn.execute("SELECT title, description, details_json FROM repair_findings WHERE id = 1").fetchone()
        conn.close()
        return row[0], row[1], json.loads(row[2])
    return read


def test_a_set_can_be_edited_by_hand_then_grouped(env, monkeypatch):
    paths = _volumes(env, count=3)
    extra = env["root"] / "HOYO-MiX" / "Loose Folder"
    loose = _flac(str(extra / "01 - Bonus.flac"), title="Bonus", album="Whatever", tracknumber=1)
    group = jobs.volume_groups(env["db"])[0]
    read = _findings_table(env, monkeypatch, jobs.finding_details(group["album"], "HOYO-MiX", group["volumes"], True))
    ids = {v["number"]: v["album_id"] for v in group["volumes"]}
    assert [a["title"] for a in jobs.search_albums(env["db"], "footprints vol. 2")] == ["Footprints of the Traveler, Vol. 2"]

    # drop volume 2, renumber volume 3 as disc 2, add a folder as disc 3, rename the album
    items = [{"album_id": ids[1], "number": 1}, {"album_id": ids[3], "number": 2}, {"folder": str(extra), "number": 3}]
    details = jobs.update_volume_finding(env["db"], 1, "Footprints (Complete)", items)
    title, description, stored = read()
    assert stored == details and details["edited"] is True and details["volume_numbers"] == "1, 2, 3"
    assert title == "Volumes of one album: Footprints (Complete)" and "5 tracks" in description
    assert details["volumes"][2] == {"folder": os.path.realpath(str(extra)), "title": "Loose Folder", "number": 3, "tracks": 1}

    for bad, why in (([items[0]], "at least two"), ([items[0], dict(items[1], number=1)], "same disc number"),
                     ([items[0], items[0]], "twice"), ([items[0], {"folder": "/etc", "number": 2}], "")):
        with pytest.raises((ValueError, PermissionError, FileNotFoundError), match=why or None):
            jobs.describe_volumes(env["db"], bad)

    # a later scan does not undo the edit
    assert jobs.refresh_volume_finding("fork_volume_grouping", "HOYO-MiX:x",
                                       jobs.finding_details(group["album"], "HOYO-MiX", group["volumes"], True)) is False
    applied = jobs.group_volumes(env["db"], details, move_files=False)
    assert applied["success"] and applied["fixed"] == 5
    assert FLAC(paths[(3, 1)])["discnumber"] == ["2"] and FLAC(paths[(3, 1)])["album"] == ["Footprints (Complete)"]
    assert FLAC(loose)["discnumber"] == ["3"] and FLAC(loose)["album"] == ["Footprints (Complete)"]
    assert FLAC(paths[(2, 1)])["album"] == ["Footprints of the Traveler, Vol. 2"]          # removed: untouched


def test_a_rescan_updates_an_unedited_finding(env, monkeypatch):
    _volumes(env, count=3)
    group = jobs.volume_groups(env["db"])[0]
    old = jobs.finding_details(group["album"], "HOYO-MiX", group["volumes"][:2], True)
    read = _findings_table(env, monkeypatch, old)
    new = jobs.finding_details(group["album"], "HOYO-MiX", group["volumes"], True)
    assert jobs.refresh_volume_finding("fork_volume_grouping", "HOYO-MiX:x", new) is True
    assert read()[2]["volume_numbers"] == "1, 2, 3" and "3 volumes" in read()[1]


# ── retranslate ─────────────────────────────────────────────────────────

class _Settings:
    """A config manager that can be written to, as the real one."""

    def __init__(self, job_id, **settings):
        self.key, self.saved = f"repair.jobs.{job_id}.settings", settings

    def get(self, key, default=None):
        return self.saved if key == self.key else default

    def set(self, key, value):
        assert key == self.key
        self.saved = value


def _translated_library(env):
    """An album the model translated before, with one title corrected by hand."""
    db, root = env["db"], env["root"]
    folder = root / "Jay Chou" / "Chopin of November (十一月的蕭邦)"
    album = "Chopin of November (十一月的蕭邦)"
    paths = {}
    for n, (title, original) in enumerate([("Night Song (夜曲)", "夜曲"), ("My Snow (髮如雪)", "髮如雪")], 1):
        paths[original] = _flac(str(folder / f"0{n} - {title}.flac"), title=title, album=album,
                                soulsync_original_title=original, soulsync_original_album="十一月的蕭邦")
        (folder / f"0{n} - {title}.lrc").write_text("x", encoding="utf-8")
        db.add("Jay Chou", album, title, n, paths[original])
    # in the same folder, not in the database: one carries only the old translation, one is another album
    paths["bare"] = _flac(str(folder / "03 - Bonus.flac"), title="Bonus", album="Chopin of November")
    paths["other"] = _flac(str(folder / "04 - Guest.flac"), title="Guest", album="Somebody Else's Album")
    store.save_translation("album", "十一月的蕭邦", "Chopin of November", model="old-model")
    store.save_translation("title", "夜曲", "Night Song", model="old-model")
    store.save_translation("title", "髮如雪", "My Snow", user_edited=True)
    store.save_translation("title", "完了", "Done", model="existing")
    return paths


def test_retranslate_renames_everything_that_carries_the_name_and_switches_itself_off(env, llm):
    _translated_library(env)
    llm.replies = [_batch("November's Chopin"), _batch("Nocturne")]
    ctx = Ctx(env["db"])
    ctx.config_manager = _Settings("fork_auto_translate", dry_run=False, retranslate=True)
    result = fork_tools.AutoTranslateJob().scan(ctx)
    # only what the model made is asked again, with the library's artist as context
    assert [(p["kind"], p["items"]) for _task, p in llm.calls] == [
        ("album name", [{"id": 1, "original": "十一月的蕭邦", "artist": "Jay Chou"}]),
        ("song title", [{"id": 1, "original": "夜曲", "artist": "Jay Chou", "album": "十一月的蕭邦"}])]
    assert store.get_translation("title", "髮如雪")["translated"] == "My Snow"      # hand-edited: kept
    assert store.get_translation("title", "完了")["translated"] == "Done"           # came with the name: kept
    folder = env["root"] / "Jay Chou" / "November's Chopin (十一月的蕭邦)"
    assert sorted(os.listdir(folder)) == [
        "01 - Nocturne (夜曲).flac", "01 - Nocturne (夜曲).lrc", "02 - My Snow (髮如雪).flac",
        "02 - My Snow (髮如雪).lrc", "03 - Bonus.flac", "04 - Guest.flac"]
    assert not (env["root"] / "Jay Chou" / "Chopin of November (十一月的蕭邦)").exists()
    song = FLAC(str(folder / "01 - Nocturne (夜曲).flac"))
    assert song["title"] == ["Nocturne (夜曲)"] and song["soulsync_original_title"] == ["夜曲"]
    # one album name for the folder and for every file of the album in it
    albums = {name: FLAC(str(folder / name))["album"][0] for name in os.listdir(folder) if name.endswith(".flac")}
    assert albums == {"01 - Nocturne (夜曲).flac": "November's Chopin (十一月的蕭邦)",
                      "02 - My Snow (髮如雪).flac": "November's Chopin (十一月的蕭邦)",
                      "03 - Bonus.flac": "November's Chopin (十一月的蕭邦)",
                      "04 - Guest.flac": "Somebody Else's Album"}
    assert result.auto_fixed == 4 and result.errors == 0
    assert ctx.config_manager.saved == {"dry_run": False, "retranslate": False}
    # the library database follows the files
    rows = retro._query(env["db"], "SELECT file_path FROM tracks ORDER BY track_number", ())
    assert all(os.path.isfile(row["file_path"]) for row in rows)


def test_retranslate_dry_run_proposes_only_what_changed(env, llm):
    paths = _translated_library(env)
    llm.replies = [_batch("Chopin of November"), _batch("Nocturne")]        # the album comes out the same
    ctx = Ctx(env["db"])
    ctx.config_manager = _Settings("fork_auto_translate", retranslate=True)
    result = fork_tools.AutoTranslateJob().scan(ctx)
    assert result.findings_created == 1 and result.auto_fixed == 0
    finding = ctx.findings[0]
    assert finding["title"] == "Retranslated title: 夜曲" and finding["entity_id"] == "title:夜曲:Nocturne"
    assert finding["description"] == '"Night Song (夜曲)" would become "Nocturne (夜曲)"'
    assert finding["details"]["previous"] == "Night Song"
    assert FLAC(paths["夜曲"])["title"] == ["Night Song (夜曲)"]                 # files wait for approval
    assert jobs.apply_translation_finding(env["db"], finding["details"])["fixed"] == 1
    assert ctx.config_manager.saved["retranslate"] is False


def test_without_retranslate_saved_translations_are_not_asked_again(env, llm):
    _translated_library(env)
    result = fork_tools.AutoTranslateJob().scan(Ctx(env["db"]))
    assert llm.calls == [] and result.scanned == 0


def test_a_folder_named_after_an_earlier_translation_is_renamed_with_it(env):
    folder = env["root"] / "Jay Chou" / "Chopin of November [2005]"
    path = _flac(str(folder / "01.flac"), title="x", album="十一月的蕭邦")
    env["db"].add("Jay Chou", "十一月的蕭邦", "x", 1, path)
    store.save_translation("album", "十一月的蕭邦", "November's Chopin", model="m")
    data = retro.apply_translation(env["db"], "album", "十一月的蕭邦", previous=["Chopin of November"])
    assert data["renamed"] == 1 and data["errors"] == []
    assert FLAC(str(env["root"] / "Jay Chou" / "November's Chopin [2005]" / "01.flac"))["album"] == [
        "November's Chopin (十一月的蕭邦)"]
