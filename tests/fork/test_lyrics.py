from core.fork import lyrics, ollama

LRC = "[ar:周杰倫]\n[00:01.00]一群嗜血的螞蟻\n[00:05.50]Hello\n[00:09.00]\n[00:12.00]一群嗜血的螞蟻\n"


def _lines(*texts):
    return {"lines": [{"id": i, "translation": t} for i, t in enumerate(texts)]}


def _write(tmp_path, name, body):
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"")
    (tmp_path / name).write_text(body, encoding="utf-8")
    return str(audio)


def test_inline_lrc_repeats_the_timestamp_and_dedupes_model_input(tmp_path, llm, fork_env):
    fork_env.set("fork.lyrics.mode", "inline")
    audio = _write(tmp_path, "song.lrc", LRC)
    llm.replies = [_lines("A swarm of bloodthirsty ants")]
    embedded = lyrics.translate_sidecar(audio, "夜曲", "周杰倫")
    out = (tmp_path / "song.lrc").read_text(encoding="utf-8")
    assert out == embedded
    assert out.splitlines() == [
        "[re:SoulSync LLM translation]",
        "[ar:周杰倫]",
        "[00:01.00]一群嗜血的螞蟻",
        "[00:01.00]A swarm of bloodthirsty ants",
        "[00:05.50]Hello",
        "[00:09.00]",
        "[00:12.00]一群嗜血的螞蟻",
        "[00:12.00]A swarm of bloodthirsty ants",
    ]
    # the repeated line was sent once
    assert [l["text"] for l in llm.calls[0][1]["lines"]] == ["一群嗜血的螞蟻"]


def test_already_translated_file_is_left_alone(tmp_path, llm):
    audio = _write(tmp_path, "song.lrc", LRC)
    llm.replies = [_lines("A swarm of bloodthirsty ants")]
    lyrics.translate_sidecar(audio)
    assert lyrics.translate_sidecar(audio) is None
    assert len(llm.calls) == 1


def test_separate_mode_swaps_in_the_translation_and_backs_up_the_original(tmp_path, llm, fork_env):
    fork_env.set("fork.lyrics.mode", "separate")
    audio = _write(tmp_path, "song.lrc", LRC)
    llm.replies = [_lines("A swarm of bloodthirsty ants")]
    embedded = lyrics.translate_sidecar(audio)
    # the untranslated file is preserved byte for byte under .original.lrc
    assert (tmp_path / "song.original.lrc").read_text(encoding="utf-8") == LRC
    # the sidecar the server reads is now the translation only
    translated = (tmp_path / "song.lrc").read_text(encoding="utf-8")
    assert translated == embedded
    assert translated.splitlines() == [
        "[re:SoulSync LLM translation]",
        "[ar:周杰倫]",
        "[00:01.00]A swarm of bloodthirsty ants",
        "[00:05.50]Hello",
        "[00:09.00]",
        "[00:12.00]A swarm of bloodthirsty ants",
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["song.flac", "song.lrc", "song.original.lrc"]
    # a second pass does nothing: no model call, backup untouched
    assert lyrics.translate_sidecar(audio) is None
    assert len(llm.calls) == 1
    assert (tmp_path / "song.original.lrc").read_text(encoding="utf-8") == LRC


def test_separate_mode_never_overwrites_an_existing_backup(tmp_path, llm, fork_env):
    fork_env.set("fork.lyrics.mode", "separate")
    audio = _write(tmp_path, "song.lrc", LRC)
    (tmp_path / "song.original.lrc").write_text("the true original\n", encoding="utf-8")
    llm.replies = [_lines("A swarm of bloodthirsty ants")]
    assert lyrics.translate_sidecar(audio) is not None
    assert (tmp_path / "song.original.lrc").read_text(encoding="utf-8") == "the true original\n"
    assert "A swarm of bloodthirsty ants" in (tmp_path / "song.lrc").read_text(encoding="utf-8")


def test_separate_mode_plain_text(tmp_path, llm, fork_env):
    fork_env.set("fork.lyrics.mode", "separate")
    audio = _write(tmp_path, "song.txt", "夜曲\nla la\n")
    llm.replies = [_lines("Nocturne")]
    lyrics.translate_sidecar(audio)
    assert (tmp_path / "song.original.txt").read_text(encoding="utf-8") == "夜曲\nla la\n"
    assert (tmp_path / "song.txt").read_text(encoding="utf-8").splitlines() == [
        "Nocturne", "la la", "[SoulSync LLM translation]"]


def test_plain_text_lyrics(tmp_path, llm, fork_env):
    fork_env.set("fork.lyrics.mode", "inline")
    audio = _write(tmp_path, "song.txt", "夜曲\nla la\n")
    llm.replies = [_lines("Nocturne")]
    lyrics.translate_sidecar(audio)
    assert (tmp_path / "song.txt").read_text(encoding="utf-8").splitlines() == [
        "夜曲", "Nocturne", "la la", "[SoulSync LLM translation]"]


def test_latin_lyrics_and_model_failures_change_nothing(tmp_path, llm):
    audio = _write(tmp_path, "song.lrc", "[00:01.00]Hello\n")
    assert lyrics.translate_sidecar(audio) is None and llm.calls == []
    (tmp_path / "song.lrc").write_text(LRC, encoding="utf-8")
    llm.replies = [ollama.OllamaError("down")]
    assert lyrics.translate_sidecar(audio) is None
    assert (tmp_path / "song.lrc").read_text(encoding="utf-8") == LRC


def test_mostly_missing_translation_is_discarded(tmp_path, llm):
    body = "".join(f"[00:0{i}.00]歌詞{i}\n" for i in range(6))
    audio = _write(tmp_path, "song.lrc", body)
    llm.replies = [_lines("only one line")]
    assert lyrics.translate_sidecar(audio) is None
    assert (tmp_path / "song.lrc").read_text(encoding="utf-8") == body
