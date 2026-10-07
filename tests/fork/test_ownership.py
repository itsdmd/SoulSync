"""Ownership checks against a library the fork has renamed."""

import sqlite3

import pytest

from core.fork import hooks, ownership, store


class FakeDB:
    """The two tables the ownership layer reads, plus a stand-in for
    upstream's name check: exact (title, artist) lookup."""

    def __init__(self, path):
        self.path = path
        conn = self._get_connection()
        conn.executescript(
            "CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, artist_id INTEGER, title TEXT);"
            "CREATE TABLE tracks (id INTEGER PRIMARY KEY, album_id INTEGER, artist_id INTEGER, title TEXT,"
            " file_path TEXT, server_source TEXT, spotify_track_id TEXT, itunes_track_id TEXT, deezer_id TEXT,"
            " tidal_id TEXT, qobuz_id TEXT, musicbrainz_recording_id TEXT, audiodb_id TEXT, soul_id TEXT, isrc TEXT);"
        )
        conn.commit()
        conn.close()
        self.calls = []

    def _get_connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, artist, album, title, **ids):
        conn = self._get_connection()
        aid = conn.execute("INSERT INTO artists (name) VALUES (?)", (artist,)).lastrowid
        alid = conn.execute("INSERT INTO albums (artist_id, title) VALUES (?, ?)", (aid, album)).lastrowid
        cols = ", ".join(["album_id", "artist_id", "title", "file_path"] + list(ids))
        conn.execute(f"INSERT INTO tracks ({cols}) VALUES ({', '.join('?' * (4 + len(ids)))})",
                     [alid, aid, title, f"/music/{title}.flac"] + list(ids.values()))
        conn.commit()
        conn.close()

    def exact(self, table):
        def check(title, artist):
            self.calls.append((title, artist))
            conn = self._get_connection()
            row = conn.execute(
                f"SELECT t.title FROM {table} t JOIN artists a ON a.id = t.artist_id WHERE t.title = ? AND a.name = ?",
                (title, artist)).fetchone()
            conn.close()
            return (row["title"], 1.0) if row else (None, 0.0)
        return check


@pytest.fixture
def db(tmp_path, monkeypatch):
    # no MusicBrainz in these tests: only stored rules/aliases are used
    monkeypatch.setattr(ownership.artist_names, "lookup_musicbrainz",
                        lambda name: {"replacement": "", "mbid": ""})
    return FakeDB(str(tmp_path / "library.db"))


def _retry(db, kind, name, artist):
    check = db.exact("tracks" if kind == "title" else "albums")
    return hooks.ownership_retry(db, kind, name, artist, 0.7, check, check(name, artist))


def test_recorded_translation_and_artist_rule_find_the_library_row(db):
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    store.save_translation("title", "夜曲", "Nocturne", model="m")
    db.add("Jay Chou", "November's Chopin (十一月的蕭邦)", "Nocturne (夜曲)")
    assert _retry(db, "title", "夜曲", "周杰倫") == ("Nocturne (夜曲)", 1.0)


def test_original_inside_the_library_name_matches_any_wording(db):
    """A file translated before the fork existed: nothing recorded, different
    wording, but the library title carries the original."""
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    db.add("Jay Chou", "Chopin of November (十一月的蕭邦)", "Night Song (夜曲)")
    assert _retry(db, "title", "夜曲", "周杰倫") == ("Night Song (夜曲)", 1.0)
    assert _retry(db, "album", "十一月的蕭邦", "周杰倫") == ("Chopin of November (十一月的蕭邦)", 1.0)
    # version decoration on the source side is ignored for the lookup
    assert _retry(db, "title", "夜曲 (Live)", "周杰倫")[0] == "Night Song (夜曲)"


def test_embedded_original_must_be_the_whole_original_and_the_same_artist(db):
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    db.add("Jay Chou", "A", "Half Nocturne (小夜曲)")      # contains 夜曲 but is another title
    db.add("Someone Else", "B", "Nocturne (夜曲)")           # right title, wrong artist
    db.add("Jay Chou", "C", "夜曲集")                        # no translation part at all
    assert _retry(db, "title", "夜曲", "周杰倫") == (None, 0.0)


def test_artist_rule_alone_rescues_a_latin_title(db):
    store.save_artist_name("澤野弘之", "Hiroyuki Sawano", "manual")
    db.add("Hiroyuki Sawano", "Album", "aLIEz")
    assert _retry(db, "title", "aLIEz", "澤野弘之") == ("aLIEz", 1.0)


def test_cached_aliases_cover_a_source_that_reports_the_other_script(db):
    """Source says "Jay Chou", library folder was tagged from 周杰倫 -> rule."""
    store.save_artist_aliases("Jay Chou", ["周杰倫"])
    store.save_artist_name("周杰倫", "Jay Chou (周杰倫)", "manual")
    db.add("Jay Chou (周杰倫)", "Album", "Mojito")
    assert _retry(db, "title", "Mojito", "Jay Chou") == ("Mojito", 1.0)


def test_a_first_hit_is_returned_untouched_and_latin_misses_are_not_retried(db):
    db.add("Coldplay", "Parachutes", "Yellow")
    assert _retry(db, "title", "Yellow", "Coldplay") == ("Yellow", 1.0)
    db.calls.clear()
    assert _retry(db, "title", "Trouble", "Coldplay") == (None, 0.0)
    assert db.calls == [("Trouble", "Coldplay")]  # no extra queries for ordinary misses


def test_external_id_identifies_the_track_regardless_of_names(db):
    db.add("Jay Chou", "November's Chopin (十一月的蕭邦)", "Nocturne (夜曲)", spotify_track_id="sp-1", isrc="TW-1")
    by_spotify = hooks.owned_by_external_id(db, {"name": "夜曲", "id": "sp-1", "source": "spotify"})
    assert by_spotify.title == "Nocturne (夜曲)" and by_spotify.file_path == "/music/Nocturne (夜曲).flac"
    assert hooks.owned_by_external_id(db, {"name": "夜曲", "isrc": "TW-1"}).id == by_spotify.id
    assert hooks.owned_by_external_id(db, {"name": "夜曲", "id": "sp-2", "source": "spotify"}) is None
    assert hooks.owned_by_external_id(db, {"name": "夜曲"}) is None


def test_real_database_methods_are_wrapped():
    from database.music_database import MusicDatabase

    assert MusicDatabase.check_track_exists.__name__ == "_fork_check_track_exists"
    assert MusicDatabase.check_album_exists_with_editions.__name__ == "_fork_check_album_exists_with_editions"


# ── the wishlist's strict identity re-check ─────────────────────────────

def _row(title, artist, album):
    from types import SimpleNamespace

    return SimpleNamespace(title=title, artist_name=artist, track_artist=None, album_title=album,
                           file_path="/music/x.flac")


def _strict(row, title, artist, album, require_album=True):
    from core.wishlist import library_match

    return library_match._strict_identity_matches(row, title, artist, album, require_album)


def test_strict_identity_accepts_a_row_the_fork_renamed(db):
    store.save_artist_name("塞壬唱片-MSR", "Monster Siren Records", "manual")
    store.save_translation("title", "生命流", "Lifestream", model="m")
    row = _row("Lifestream (生命流)", "Monster Siren Records", "Arknights OST (明日方舟)")
    assert _strict(row, "生命流", "塞壬唱片-MSR", "明日方舟") is True


def test_strict_identity_accepts_embedded_original_without_any_record(db):
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    row = _row("Night Song (夜曲)", "Jay Chou", "Chopin of November (十一月的蕭邦)")
    assert _strict(row, "夜曲", "周杰倫", "十一月的蕭邦") is True


def test_strict_identity_artist_rule_with_untranslated_title(db):
    store.save_artist_name("塞壬唱片-MSR", "Monster Siren Records", "manual")
    row = _row("Renegade", "Monster Siren Records", "Renegade")
    assert _strict(row, "Renegade", "塞壬唱片-MSR", "Renegade") is True


def test_strict_identity_stays_strict(db):
    store.save_artist_name("周杰倫", "Jay Chou", "manual")
    # another artist's song with the same original title
    assert _strict(_row("Nocturne (夜曲)", "Someone Else", "X (十一月的蕭邦)"), "夜曲", "周杰倫", "十一月的蕭邦") is False
    # a different title that merely contains the original
    assert _strict(_row("Serenade (小夜曲)", "Jay Chou", "X (十一月的蕭邦)"), "夜曲", "周杰倫", "十一月的蕭邦") is False
    # a live recording is not the studio one, in either direction
    assert _strict(_row("Nocturne (夜曲) (Live)", "Jay Chou", "X (十一月的蕭邦)"), "夜曲", "周杰倫", "十一月的蕭邦") is False
    assert _strict(_row("Nocturne (夜曲)", "Jay Chou", "X (十一月的蕭邦)"), "夜曲 (Live)", "周杰倫", "十一月的蕭邦") is False
    # right track, wrong album, when the wish names an album
    assert _strict(_row("Nocturne (夜曲)", "Jay Chou", "Greatest Hits"), "夜曲", "周杰倫", "十一月的蕭邦") is False
    assert _strict(_row("Nocturne (夜曲)", "Jay Chou", "Greatest Hits"), "夜曲", "周杰倫", "十一月的蕭邦", require_album=False) is True


def test_strict_identity_leaves_ordinary_latin_mismatches_rejected(db):
    assert _strict(_row("The Sun Maid", "Soul Asylum", "Grave Dancers Union"),
                   "Runaway Train", "Soul Asylum", "Grave Dancers Union") is False
