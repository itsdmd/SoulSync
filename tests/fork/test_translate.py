from core.fork import ollama, store, translate
from core.fork.cjk import contains_cjk, split_name


def _reply(text):
    return {"translations": [{"id": 1, "translation": text}]}


def test_contains_cjk_covers_chinese_japanese_korean_only():
    assert contains_cjk("夜曲") and contains_cjk("アイドル") and contains_cjk("사랑")
    assert not contains_cjk("Beyoncé") and not contains_cjk("Сплин") and not contains_cjk(None)


def test_split_name_separates_version_suffix_and_existing_translation():
    assert split_name("夜曲") == ("夜曲", "", "")
    assert split_name("夜曲 (Live)") == ("夜曲", "", "(Live)")
    assert split_name("夜曲 - 2019 Remaster") == ("夜曲", "", "- 2019 Remaster")
    assert split_name("夜曲 (Nocturne)") == ("夜曲", "Nocturne", "")
    assert split_name("Nocturne (夜曲) (Live)") == ("夜曲", "Nocturne", "(Live)")


def test_latin_names_are_never_sent_to_the_model(llm):
    assert translate.translate_name("album", "Abbey Road") == "Abbey Road"
    assert llm.calls == []


def test_translation_uses_template_and_is_cached(llm):
    llm.replies = [_reply("Nocturne")]
    assert translate.translate_name("title", "夜曲") == "Nocturne (夜曲)"
    # second call: served from the table, no model call
    assert translate.translate_name("title", "夜曲") == "Nocturne (夜曲)"
    assert len(llm.calls) == 1
    assert store.get_translation("title", "夜曲")["translated"] == "Nocturne"


def test_album_translation_is_consistent_across_decorated_variants(llm):
    llm.replies = [_reply("November's Chopin")]
    assert translate.translate_name("album", "十一月的蕭邦") == "November's Chopin (十一月的蕭邦)"
    assert translate.translate_name("album", "十一月的蕭邦 (Deluxe)") == "November's Chopin (十一月的蕭邦) (Deluxe)"
    assert len(llm.calls) == 1


def test_custom_template(llm, fork_env):
    fork_env.set("fork.translate.template", "{translated} [{original}]")
    llm.replies = [_reply("Idol")]
    assert translate.translate_name("title", "アイドル") == "Idol [アイドル]"


def test_already_bilingual_name_reuses_its_translation_without_the_model(llm):
    assert translate.translate_name("title", "夜曲 (Nocturne)") == "Nocturne (夜曲)"
    assert llm.calls == []
    # and the output is stable when fed back in
    assert translate.translate_name("title", "Nocturne (夜曲)") == "Nocturne (夜曲)"


def test_user_edit_wins_and_survives_a_later_model_result(llm):
    llm.replies = [_reply("Night Song")]
    translate.translate_name("album", "夜曲")
    store.save_translation("album", "夜曲", "Nocturne", user_edited=True)
    assert translate.translate_name("album", "夜曲") == "Nocturne (夜曲)"
    store.save_translation("album", "夜曲", "Something Else", model="m")  # model write
    row = store.get_translation("album", "夜曲")
    assert row["translated"] == "Nocturne" and row["user_edited"] == 1


def test_invalid_model_output_is_retried_then_given_up(llm):
    llm.replies = [_reply("夜曲"), _reply("")]
    assert translate.translate_name("title", "夜曲") == "夜曲"
    assert len(llm.calls) == 2
    assert "retry_feedback" in llm.calls[1][1]
    assert store.get_translation("title", "夜曲") is None


def test_model_outage_leaves_the_name_unchanged(llm):
    llm.replies = [ollama.OllamaError("down")]
    assert translate.translate_name("title", "夜曲") == "夜曲"


# ── terms that describe a release rather than name it ───────────────────

def test_original_soundtrack_is_decoration_not_the_translation(llm):
    assert split_name("危機合約滌墨作戰 (Original Soundtrack)") == ("危機合約滌墨作戰", "", "(Original Soundtrack)")
    llm.replies = [_reply("Contingency Contract: Ink Wash Operation")]
    assert translate.translate_name("album", "危機合約滌墨作戰 (Original Soundtrack)") == \
        "Contingency Contract: Ink Wash Operation (危機合約滌墨作戰) (Original Soundtrack)"
    assert len(llm.calls) == 1                       # the model was asked for a real translation
    assert llm.calls[0][1]["items"][0]["original"] == "危機合約滌墨作戰"


def test_default_terms_cover_common_release_decoration():
    for bracket in ("OST", "EP", "Remastered", "2019 Remaster", "Deluxe Edition", "Original Game Soundtrack",
                    "TV Size", "Instrumental", "feat. Lara", "Live at Budokan", "Vol. 2", "Bonus Track",
                    "Re-recorded", "rerecorded", "A Cappella", "ost", "Mini Album", "2005"):
        assert split_name(f"夜曲 ({bracket})") == ("夜曲", "", f"({bracket})"), bracket
    assert split_name("夜曲 - Original Soundtrack") == ("夜曲", "", "- Original Soundtrack")
    assert split_name("夜曲 [OST] (Deluxe)") == ("夜曲", "", "[OST] (Deluxe)")


def test_real_translations_are_still_recognised():
    # whole words only: "Deep" contains EP, "Mixtures" contains Mix, "Olive" contains Live
    for name in ("Nocturne", "Deep Sleep", "Mixtures of Night", "Olive Garden", "Cruel Angel's Thesis"):
        assert split_name(f"夜曲 ({name})") == ("夜曲", name, ""), name


def test_user_can_edit_the_term_list(fork_env):
    fork_env.set("fork.translate.keep_terms", "Drama CD,  Character Song\nOST")
    assert split_name("夜曲 (Drama CD)") == ("夜曲", "", "(Drama CD)")
    assert split_name("夜曲 (Character   Song)") == ("夜曲", "", "(Character   Song)")
    assert split_name("夜曲 (OST)") == ("夜曲", "", "(OST)")
    # removed from the list: now read as a translation again
    assert split_name("夜曲 (Remastered)") == ("夜曲", "Remastered", "")
    assert split_name("夜曲 (2019)") == ("夜曲", "", "(2019)")      # a year always counts
    fork_env.set("fork.translate.keep_terms", "")
    assert split_name("夜曲 (OST)") == ("夜曲", "OST", "")


def test_a_wrongly_recorded_decoration_is_dropped_and_retranslated(llm):
    # what the old behaviour stored
    store.save_translation("album", "危機合約滌墨作戰", "Original Soundtrack", model="existing")
    llm.replies = [_reply("Contingency Contract")]
    assert translate.translate_name("album", "危機合約滌墨作戰 (Original Soundtrack)") == \
        "Contingency Contract (危機合約滌墨作戰) (Original Soundtrack)"
    assert store.get_translation("album", "危機合約滌墨作戰")["translated"] == "Contingency Contract"


def test_purge_removes_only_auto_lifted_decoration_records():
    store.save_translation("album", "甲", "Original Soundtrack", model="existing")
    store.save_translation("album", "乙", "Nocturne", model="existing")          # a real lifted translation
    store.save_translation("album", "丙", "Live", user_edited=True)              # the user chose it
    store.save_translation("title", "丁", "Remix", model="qwen3.5:9b")           # the model said so
    assert translate.purge_decoration_records() == 1
    assert store.get_translation("album", "甲") is None
    assert all(store.get_translation(k, o) for k, o in (("album", "乙"), ("album", "丙"), ("title", "丁")))


def test_a_name_the_old_behaviour_wrote_is_read_back_correctly(llm):
    """"Original Soundtrack (危機合約滌墨作戰)" on an already-tagged file."""
    assert split_name("Original Soundtrack (危機合約滌墨作戰)") == ("危機合約滌墨作戰", "", "(Original Soundtrack)")
    store.save_translation("album", "危機合約滌墨作戰", "Contingency Contract", user_edited=True)
    assert translate.translate_name("album", "Original Soundtrack (危機合約滌墨作戰)", allow_llm=False) == \
        "Contingency Contract (危機合約滌墨作戰) (Original Soundtrack)"


def test_a_title_that_merely_contains_a_term_is_still_a_translation():
    # our own "<translation> (<original>)" form: the front is a translation
    assert split_name("Arknights OST (明日方舟)") == ("明日方舟", "Arknights OST", "")
    assert split_name("Live Forever (永生)") == ("永生", "Live Forever", "")
    # and such a record is not purged
    store.save_translation("album", "明日方舟", "Arknights OST", model="existing")
    assert translate.purge_decoration_records() == 0


def test_decoration_glued_to_the_name_is_split_off():
    assert split_name("相变临界OST") == ("相变临界", "", "OST")
    assert split_name("相变临界 OST") == ("相变临界", "", "OST")
    assert split_name("夜曲 Remix") == ("夜曲", "", "Remix")
    assert split_name("Critical Transition Point OST (相变临界OST)") == ("相变临界", "Critical Transition Point OST", "OST")
    # a Latin tail that is a real word stays part of the name
    assert split_name("東京Tower") == ("東京Tower", "", "")
    assert split_name("夜曲A") == ("夜曲A", "", "")


def test_traditional_and_simplified_spellings_share_one_translation(llm):
    from core.fork.cjk import fold

    assert fold("相變臨界") == fold("相变临界") and fold("夜曲") != fold("夜")
    llm.replies = [_reply("Critical Phase Transition")]
    assert translate.translate_name("album", "相變臨界") == "Critical Phase Transition (相變臨界)"
    # the other script reuses the record: no second model call, its own script kept in the name
    assert translate.translate_name("album", "相变临界OST") == "Critical Phase Transition (相变临界) OST"
    assert len(llm.calls) == 1
    assert store.list_translations(kind="album")["total"] == 1
