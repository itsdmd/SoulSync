import pytest

from core.fork import artist_names, store

SAWANO = {
    "id": "mbid-1", "name": "澤野弘之", "sort-name": "Sawano, Hiroyuki", "type": "Person", "score": 100,
    "aliases": [
        {"name": "泽野弘之", "locale": "zh_Hans", "primary": True, "type": "Artist name"},
        {"name": "Hiroyuki Sawano", "locale": "en", "primary": True, "type": "Artist name"},
    ],
}


class FakeMB:
    def __init__(self, results=None, error=None):
        self.results, self.error, self.calls = results or [], error, 0

    def search_artist(self, name, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.results


@pytest.fixture
def mb(monkeypatch):
    client = FakeMB([SAWANO])
    monkeypatch.setattr(artist_names, "_mb_client", lambda: client)
    return client


def test_pick_name_prefers_primary_locale_alias():
    assert artist_names.pick_name(SAWANO, "澤野弘之") == "Hiroyuki Sawano"


def test_pick_name_falls_back_to_flipped_sort_name_for_people():
    artist = {"name": "久石譲", "sort-name": "Hisaishi, Joe", "type": "Person", "aliases": []}
    assert artist_names.pick_name(artist, "久石譲") == "Joe Hisaishi"


def test_pick_name_keeps_group_sort_name_as_is():
    artist = {"name": "ヨルシカ", "sort-name": "Yorushika", "type": "Group", "aliases": []}
    assert artist_names.pick_name(artist, "ヨルシカ") == "Yorushika"


def test_lookup_is_cached(mb):
    assert artist_names.resolve("澤野弘之") == "Hiroyuki Sawano"
    assert artist_names.resolve("澤野弘之") == "Hiroyuki Sawano"
    assert mb.calls == 1
    assert store.get_artist_name("澤野弘之")["source"] == "musicbrainz"


def test_low_score_or_non_matching_result_is_not_used(mb):
    mb.results = [{**SAWANO, "score": 60}]
    assert artist_names.resolve("澤野弘之") == "澤野弘之"
    assert store.get_artist_name("澤野弘之")["source"] == "none"
    assert artist_names.resolve("澤野弘之") == "澤野弘之"
    assert mb.calls == 1  # the miss is cached too


def test_transport_error_is_not_cached_as_a_miss(mb):
    mb.error = RuntimeError("timeout")
    assert artist_names.resolve("澤野弘之") == "澤野弘之"
    assert store.get_artist_name("澤野弘之") is None


def test_manual_rule_wins_over_lookup_and_applies_to_latin_names(mb):
    store.save_artist_name("澤野弘之", "SawanoHiroyuki[nZk]", "manual")
    store.save_artist_name("Ye", "Kanye West", "manual")
    assert artist_names.resolve("澤野弘之") == "SawanoHiroyuki[nZk]"
    assert artist_names.resolve("ye") == "Kanye West"  # case-insensitive key
    assert mb.calls == 0
    store.save_artist_name("澤野弘之", "Hiroyuki Sawano", "musicbrainz")  # lookup write
    assert artist_names.resolve("澤野弘之") == "SawanoHiroyuki[nZk]"


def test_latin_names_are_never_looked_up(mb):
    assert artist_names.resolve("Radiohead") == "Radiohead"
    assert mb.calls == 0


def test_credit_with_several_artists_maps_each_and_keeps_separators(mb):
    store.save_artist_name("Aimer", "Aimer!", "manual")
    assert artist_names.resolve_credit("澤野弘之, Aimer & Someone") == "Hiroyuki Sawano, Aimer! & Someone"


def test_rule_for_whole_credit_beats_splitting(mb):
    store.save_artist_name("A & B", "The AB Band", "manual")
    assert artist_names.resolve_credit("A & B") == "The AB Band"


def test_disabled(mb, fork_env):
    fork_env.set("fork.artist_names.enabled", False)
    assert artist_names.resolve("澤野弘之") == "澤野弘之"


def test_known_names_lists_canonical_and_primary_aliases_once(mb):
    assert artist_names.known_names("Hiroyuki Sawano") == ["澤野弘之", "泽野弘之"]
    assert artist_names.known_names("hiroyuki sawano") == ["澤野弘之", "泽野弘之"]
    assert mb.calls == 1


def test_known_names_is_empty_and_uncached_when_musicbrainz_fails(mb):
    mb.error = RuntimeError("timeout")
    assert artist_names.known_names("Hiroyuki Sawano") == []
    assert store.get_artist_aliases("Hiroyuki Sawano") is None
