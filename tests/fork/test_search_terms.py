from types import SimpleNamespace

import pytest

from core.fork import artist_names, hooks, ollama, search_terms


@pytest.fixture(autouse=True)
def aliases(monkeypatch):
    """MusicBrainz stand-in: artist -> its other names."""
    table = {"高橋洋子": ["Yōko Takahashi"], "Radiohead": ["レディオヘッド"]}
    monkeypatch.setattr(artist_names, "known_names", lambda name, limit=4: list(table.get(name, [])))
    return table


@pytest.fixture(autouse=True)
def variants_only(fork_env):
    """These first tests are about the title variants; the broader queries
    have their own tests below and switch themselves on."""
    fork_env.set("fork.search_terms.max_broad", 0)


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


# ── broader queries ─────────────────────────────────────────────────────

GHIBLI = "A Symphonic Celebration∶ Music from the Studio Ghibli Films of Hayao Miyazaki"


def test_album_parts_are_the_pieces_worth_searching_alone():
    assert search_terms.album_parts(GHIBLI) == [
        "A Symphonic Celebration", "Music from the Studio Ghibli Films of Hayao Miyazaki"]
    assert search_terms.album_parts("Frieren - Original Soundtrack (Deluxe)") == [
        "Frieren - Original Soundtrack", "Frieren"]          # the generic half is dropped
    assert search_terms.album_parts("OK Computer") == []
    assert search_terms.album_parts("明日方舟OST5") == []


def test_queries_broaden_step_by_step(llm, fork_env):
    fork_env.set("fork.search_terms.max_broad", 8)
    llm.replies = [{"titles": ["Merry-Go-Round of Life"],
                    "albums": ["Music from the Studio Ghibli Films", "Original Soundtrack", "久石譲"]}]
    track = SimpleNamespace(name="人生のメリーゴーランド", artists=["久石譲"], album=GHIBLI)
    out = hooks.augment_search_queries(track, ["久石譲 人生のメリーゴーランド"])
    assert out == [
        "久石譲 人生のメリーゴーランド",                      # 1. artist + title (upstream)
        "久石譲 Merry-Go-Round of Life",                      #    … and its variants
        f"久石譲 {GHIBLI}",                                   # 2. artist + album
        "久石譲 Music from the Studio Ghibli Films",
        GHIBLI,                                               # 3. album alone
        "Music from the Studio Ghibli Films",
        "A Symphonic Celebration",                            # 4. part of the album name
        "Music from the Studio Ghibli Films of Hayao Miyazaki",
        "Merry-Go-Round of Life",                             # 5. an alternative title alone
        "人生のメリーゴーランド",                             # 6. the song's name alone (CJK only)
    ]
    assert llm.calls[0][1]["max_albums"] == 3


def test_a_cjk_song_name_alone_is_the_last_resort_even_past_the_limit(llm, fork_env):
    fork_env.set("fork.search_terms.max_broad", 1)
    llm.replies = [{"titles": ["Ye Qu"], "albums": []}, {"titles": [], "albums": []}, {"titles": [], "albums": []}]
    track = SimpleNamespace(name="夜曲", artists=["周杰倫"], album="十一月的蕭邦")
    out = hooks.augment_search_queries(track, ["周杰倫 夜曲"])
    assert out[-2:] == ["周杰倫 十一月的蕭邦", "夜曲"]
    # one character says too little; and the whole ladder can be switched off
    short = SimpleNamespace(name="愛", artists=["周杰倫"], album="十一月的蕭邦")
    assert "愛" not in hooks.augment_search_queries(short, ["周杰倫 愛"])
    fork_env.set("fork.search_terms.max_broad", 0)
    other = SimpleNamespace(name="髮如雪", artists=["周杰倫"], album="十一月的蕭邦")
    assert "髮如雪" not in hooks.augment_search_queries(other, ["周杰倫 髮如雪"])


def test_broad_limit_and_a_single_named_after_its_track(llm, fork_env):
    fork_env.set("fork.search_terms.max_broad", 2)
    llm.replies = [{"titles": [], "albums": []}, {"titles": [], "albums": ["Anything Else"]}]
    track = SimpleNamespace(name="Creep", artists=["Radiohead"], album="Pablo Honey: Collectors Edition")
    assert hooks.augment_search_queries(track, ["radiohead creep"]) == [
        "radiohead creep", "Radiohead Pablo Honey: Collectors Edition", "Pablo Honey: Collectors Edition"]
    single = SimpleNamespace(name="Creep", artists=["Radiohead"], album="Creep")
    assert hooks.augment_search_queries(single, ["radiohead creep"]) == ["radiohead creep"]
    assert "album" not in llm.calls[1][1] and "max_albums" not in llm.calls[1][1]


def test_broad_queries_work_from_the_cache_and_follow_the_setting(llm, fork_env):
    fork_env.set("fork.search_terms.max_broad", 1)
    llm.replies = [{"titles": ["Thesis of a Cruel Angel"], "albums": ["Shin Seiki Evangelion"]}]
    track = SimpleNamespace(name="残酷な天使のテーゼ", artists=["高橋洋子"], album="Neon Genesis Evangelion")
    first = hooks.augment_search_queries(track, [])
    assert first[-2] in ("高橋洋子 Neon Genesis Evangelion", "Yoko Takahashi Neon Genesis Evangelion")
    assert first[-1] == "残酷な天使のテーゼ"
    fork_env.set("fork.search_terms.max_broad", 12)
    again = hooks.augment_search_queries(track, [])
    assert len(llm.calls) == 1
    assert "Yoko Takahashi Shin Seiki Evangelion" in again and "Thesis of a Cruel Angel" in again


def test_a_result_of_a_broad_query_may_match_any_variant(llm, fork_env):
    fork_env.set("fork.search_terms.max_broad", 4)
    llm.replies = [{"titles": ["Zankoku na Tenshi no Thesis", "A Cruel Angel's Thesis"], "albums": []}]
    track = SimpleNamespace(name="残酷な天使のテーゼ", artists=["高橋洋子"], album="Neon Genesis Evangelion")
    hooks.augment_search_queries(track, [])
    tried = []

    def select(results, candidate_track, q, *rest):
        tried.append(candidate_track.name)
        return ["hit"] if candidate_track.name == "A Cruel Angel's Thesis" else []

    assert hooks.rescue_candidates([], ["row"], track, "Neon Genesis Evangelion", select, None, None) == ["hit"]
    assert tried == ["Zankoku na Tenshi no Thesis", "A Cruel Angel's Thesis"]


def test_spare_album_spellings_are_searched_alone_and_artist_in_a_title_is_dropped(llm, fork_env):
    fork_env.set("fork.search_terms.max_broad", 12)
    llm.replies = [{"titles": ["Creep (Radiohead)", "Creep - Radiohead"],
                    "albums": ["Pablo Honey Deluxe", "Pablo Honey Collectors", "Pablo Honey Japan"]}]
    track = SimpleNamespace(name="Creep", artists=["Radiohead"], album="Pablo Honey")
    out = hooks.augment_search_queries(track, ["radiohead creep"])
    assert not any("(Radiohead)" in q or "- Radiohead" in q for q in out)
    assert out[1:] == ["Radiohead Pablo Honey", "Radiohead Pablo Honey Deluxe",          # 2 (two per step)
                       "Pablo Honey", "Pablo Honey Deluxe",                              # 3
                       "Pablo Honey Collectors", "Pablo Honey Japan"]                    # 4: the rest


def test_a_vietnamese_track_is_searched_with_diacritics_then_without(llm, fork_env):
    from core.fork import search_terms

    assert search_terms.is_vietnamese("Em của ngày hôm qua") and search_terms.is_vietnamese("Đen")
    assert not search_terms.is_vietnamese("Café del Mar", "Björk", "Beyoncé", "夜曲", "")
    assert search_terms.strip_diacritics("Sơn Tùng M-TP Đừng Làm Trái Tim Anh Đau") == "Son Tung M-TP Dung Lam Trai Tim Anh Dau"

    fork_env.set("fork.search_terms.max_broad", 2)
    llm.replies = [{"titles": ["Yesterday's Me"], "albums": []}]
    track = SimpleNamespace(name="Em Của Ngày Hôm Qua", artists=["Sơn Tùng M-TP"], album="m-tp M-TP")
    # upstream had already folded its own query
    out = hooks.augment_search_queries(track, ["Son Tung M-TP Em Cua Ngay Hom Qua"])
    assert out == [
        "Sơn Tùng M-TP Em Của Ngày Hôm Qua",        # pass 1: as written
        "Sơn Tùng M-TP Yesterday's Me",
        "Sơn Tùng M-TP m-tp M-TP",
        "Son Tung M-TP Em Cua Ngay Hom Qua",        # pass 2: the same ladder without diacritics
        "Son Tung M-TP Yesterday's Me",
        "Son Tung M-TP m-tp M-TP",
        "m-tp M-TP",
    ]
    # a result of a folded query is still matched against the variant behind it
    assert [t.name for t in search_terms.alternate_tracks(track, "Son Tung M-TP Yesterday's Me")] == ["Yesterday's Me"]

    fork_env.set("fork.search_terms.vietnamese_passes", False)
    assert hooks.augment_search_queries(track, ["x"])[0] == "x"
    # other languages are left exactly as they were
    fork_env.set("fork.search_terms.vietnamese_passes", True)
    llm.replies = [{"titles": [], "albums": []}]
    french = SimpleNamespace(name="Déjà Vu", artists=["Beyoncé"], album="B'Day")
    assert hooks.augment_search_queries(french, ["beyonce deja vu"])[0] == "beyonce deja vu"
