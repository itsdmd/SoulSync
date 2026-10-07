from types import SimpleNamespace

import pytest

from core.fork import artist_names, hooks, ollama, search_terms


@pytest.fixture(autouse=True)
def aliases(monkeypatch):
    """MusicBrainz stand-in: artist -> its other names."""
    table = {"高橋洋子": ["Yōko Takahashi"], "Radiohead": ["レディオヘッド"]}
    monkeypatch.setattr(artist_names, "known_names", lambda name, limit=4: list(table.get(name, [])))
    return table


def _track():
    return SimpleNamespace(name="残酷な天使のテーゼ", artists=["高橋洋子"], album="Neon Genesis")


def _titles(*titles):
    return {"titles": list(titles)}


def test_variants_are_appended_after_upstream_queries_and_deduped(llm):
    llm.replies = [_titles("Zankoku na Tenshi no Thesis",
                           "残酷な天使のテーゼ",  # the input title: dropped
                           "A Cruel Angel's Thesis")]
    out = hooks.augment_search_queries(_track(), ["高橋洋子 残酷な天使のテーゼ"])
    assert out == [
        "高橋洋子 残酷な天使のテーゼ",
        # model titles, under the artist name in the same script (ASCII-folded)
        "Yoko Takahashi Zankoku na Tenshi no Thesis",
        "Yoko Takahashi A Cruel Angel's Thesis",
        # the original title under the artist's other name
        "Yoko Takahashi 残酷な天使のテーゼ",
    ]
    assert llm.calls[0][1]["artist_also_known_as"] == ["Yoko Takahashi"]


def test_the_model_cannot_introduce_an_artist(llm):
    """Only MusicBrainz names are ever used for the artist part; a title that
    just repeats an artist or the album is dropped."""
    llm.replies = [_titles("Yoko Takahashi", "Neon Genesis", "Tears for Sale")]
    out = hooks.augment_search_queries(_track(), [])
    assert out == ["Yoko Takahashi Tears for Sale", "Yoko Takahashi 残酷な天使のテーゼ"]
    assert all(q.startswith(("Yoko Takahashi ", "高橋洋子 ")) for q in out)


def test_cjk_alias_is_not_paired_with_a_latin_title(llm):
    llm.replies = [_titles()]
    track = SimpleNamespace(name="Creep", artists=["Radiohead"], album="Pablo Honey")
    assert hooks.augment_search_queries(track, ["radiohead creep"]) == ["radiohead creep"]


def test_alias_variant_survives_a_model_outage_but_is_not_cached(llm):
    llm.replies = [ollama.OllamaError("down"), _titles("A Cruel Angel's Thesis")]
    assert hooks.augment_search_queries(_track(), []) == ["Yoko Takahashi 残酷な天使のテーゼ"]
    ollama.reset_cooldown()
    assert hooks.augment_search_queries(_track(), [])[0] == "Yoko Takahashi A Cruel Angel's Thesis"


def test_suggestions_are_cached_per_track(llm):
    llm.replies = [_titles("Zankoku na Tenshi no Thesis")]
    hooks.augment_search_queries(_track(), [])
    hooks.augment_search_queries(_track(), [])
    assert len(llm.calls) == 1


def test_max_variants_is_respected(llm, fork_env):
    fork_env.set("fork.search_terms.max_variants", 1)
    llm.replies = [_titles("One", "Two")]
    assert hooks.augment_search_queries(_track(), ["q"]) == ["q", "Yoko Takahashi One"]


def test_disabled_returns_upstream_queries_untouched(llm, fork_env):
    fork_env.set("fork.search_terms.enabled", False)
    assert hooks.augment_search_queries(_track(), ["q"]) == ["q"]
    assert llm.calls == []


def test_track_without_artist_gets_no_suggestions(llm):
    track = SimpleNamespace(name="Vortex", artists=[], album="")
    assert hooks.augment_search_queries(track, ["vortex"]) == ["vortex"]
    assert llm.calls == []


def test_rescue_scores_results_against_the_variant_that_found_them(llm):
    llm.replies = [_titles("Zankoku na Tenshi no Thesis")]
    track = _track()
    query = hooks.augment_search_queries(track, [])[0]
    seen = []

    def select(results, candidate_track, q, profile_id, why):
        seen.append((candidate_track.name, candidate_track.artists, q))
        return ["hit"]

    assert hooks.rescue_candidates([], ["row"], track, query, select, None, None) == ["hit"]
    assert seen == [("Zankoku na Tenshi no Thesis", ["Yoko Takahashi"], None)]
    assert track.name == "残酷な天使のテーゼ"  # original untouched


def test_rescue_does_nothing_for_upstream_queries_or_when_already_matched(llm):
    def select(*args):
        raise AssertionError("must not re-score")

    assert hooks.rescue_candidates([], ["row"], _track(), "some upstream query", select, None, None) == []
    assert hooks.rescue_candidates(["ok"], ["row"], _track(), "anything", select, None, None) == ["ok"]


def test_alternate_track_handles_dict_artists(llm):
    llm.replies = [_titles("Thesis")]
    track = SimpleNamespace(name="残酷な天使のテーゼ", artists=[{"name": "高橋洋子", "id": "1"}], album="")
    query = hooks.augment_search_queries(track, [])[0]
    alt = search_terms.alternate_track(track, query)
    assert alt.artists == [{"name": "Yoko Takahashi", "id": "1"}]
