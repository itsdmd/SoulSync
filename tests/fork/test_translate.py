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
