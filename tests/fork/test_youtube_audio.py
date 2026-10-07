"""YouTube video -> audio file in the import folder."""

import os
import shutil
import subprocess

import pytest

from core.fork import hooks, youtube_audio

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")


@pytest.fixture
def folders(tmp_path, monkeypatch):
    staging = tmp_path / "import"
    monkeypatch.setattr(youtube_audio, "import_folder", lambda: str(staging))
    return {"staging": staging, "videos": tmp_path / "videos"}


def _video(path, audio_codec="aac"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
                    "-c:v", "libx264", "-c:a", audio_codec, path],
                   check=True)
    return str(path)


def _probe(path, entry):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", entry, "-of", "default=nw=1:nk=1", path],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()


def test_defaults_are_off_opus_256_keep_video():
    assert youtube_audio.settings() == {"enabled": False, "codec": "opus", "bitrate": 256, "keep_video": True}


def test_settings_are_clamped_and_unknown_codecs_fall_back(fork_env):
    fork_env.set("fork.youtube_audio.codec", "wma")
    fork_env.set("fork.youtube_audio.bitrate", 9999)
    assert youtube_audio.settings()["codec"] == "opus" and youtube_audio.settings()["bitrate"] == 512


def test_output_names_are_safe_and_never_overwrite(tmp_path):
    first = youtube_audio.output_path(str(tmp_path), "Chan/nel", 'A "Title": Part 1?', ".opus")
    assert os.path.basename(first) == "Chan_nel - A _Title__ Part 1_.opus"
    open(first, "w").close()
    assert os.path.basename(youtube_audio.output_path(str(tmp_path), "Chan/nel", 'A "Title": Part 1?', ".opus")) \
        == "Chan_nel - A _Title__ Part 1_ (2).opus"
    assert os.path.basename(youtube_audio.output_path(str(tmp_path), "", "Solo", ".mp3")) == "Solo.mp3"


def test_ffmpeg_command_encodes_or_copies():
    encode = youtube_audio.ffmpeg_command("in.mp4", "out.opus", "opus", 256, False, {"title": "T", "artist": ""})
    assert encode[encode.index("-c:a") + 1] == "libopus" and encode[encode.index("-b:a") + 1] == "256k"
    assert "title=T" in encode and "-vn" in encode and not any(a.startswith("artist=") for a in encode)
    copy = youtube_audio.ffmpeg_command("in.webm", "out.opus", "opus", 256, True, {})
    assert copy[copy.index("-c:a") + 1] == "copy" and "-b:a" not in copy
    flac = youtube_audio.ffmpeg_command("in.mp4", "out.flac", "flac", 256, False, {})
    assert "-b:a" not in flac


def test_nothing_happens_while_the_feature_is_off(folders, monkeypatch):
    called = []
    monkeypatch.setattr(youtube_audio, "convert", lambda *a, **k: called.append(a))
    youtube_audio.after_download("/videos/x.mp4", {"title": "x"}, wait=True)
    assert called == [] and not folders["staging"].exists()


@needs_ffmpeg
def test_video_becomes_tagged_opus_in_the_import_folder(folders, fork_env):
    fork_env.set("fork.youtube_audio.enabled", True)
    video = _video(str(folders["videos"] / "Channel" / "clip.mp4"))
    hooks.after_youtube_download({"status": "completed", "dest_path": video},
                                 {"search_ctx": {}, "title": "My Song"})
    # the hook converts in the background; do it inline for the test as well
    out = youtube_audio.convert(video, {"title": "My Song", "channel": "Some Channel", "published_at": "2025-10-16T00:00:00"})
    assert out == str(folders["staging"] / "Some Channel - My Song.opus")
    assert _probe(out, "stream=codec_name") == ["opus"]
    from mutagen import File as MutagenFile
    audio = MutagenFile(out)
    assert audio["title"] == ["My Song"] and audio["artist"] == ["Some Channel"] and audio["date"] == ["2025-10-16"]
    assert os.path.isfile(video)                                        # the video is kept
    assert not [n for n in os.listdir(folders["staging"]) if ".part" in n]   # no half-written leftovers


@needs_ffmpeg
def test_matching_audio_is_copied_and_the_video_can_be_removed(folders, fork_env):
    fork_env.set("fork.youtube_audio.enabled", True)
    fork_env.set("fork.youtube_audio.keep_video", False)
    video = _video(str(folders["videos"] / "clip.mkv"), audio_codec="libopus")   # opus audio, as YouTube serves it
    assert youtube_audio.source_codec(video) == "opus"
    out = youtube_audio.convert(video, {"title": "Copied", "channel": "C"})
    assert out and _probe(out, "stream=codec_name") == ["opus"]
    assert not os.path.exists(video)


@needs_ffmpeg
def test_other_formats_and_a_broken_video(folders, fork_env):
    fork_env.set("fork.youtube_audio.enabled", True)
    fork_env.set("fork.youtube_audio.codec", "mp3")
    fork_env.set("fork.youtube_audio.bitrate", 192)
    video = _video(str(folders["videos"] / "clip.mp4"))
    out = youtube_audio.convert(video, {"title": "As MP3", "channel": "C"})
    assert out.endswith("C - As MP3.mp3") and _probe(out, "stream=codec_name") == ["mp3"]
    broken = folders["videos"] / "broken.mp4"
    broken.write_bytes(b"not a video")
    assert youtube_audio.convert(str(broken), {"title": "Broken"}) is None
    assert sorted(os.listdir(folders["staging"])) == ["C - As MP3.mp3"]      # nothing left behind
    assert youtube_audio.convert(str(folders["videos"] / "missing.mp4")) is None


def test_failed_or_incomplete_downloads_are_ignored(folders, fork_env, monkeypatch):
    fork_env.set("fork.youtube_audio.enabled", True)
    called = []
    monkeypatch.setattr(youtube_audio, "after_download", lambda *a, **k: called.append(a))
    hooks.after_youtube_download({"status": "failed", "error": "x"}, {})
    hooks.after_youtube_download({"status": "completed"}, {})
    hooks.after_youtube_download(None, {})
    assert called == []
    hooks.after_youtube_download({"status": "completed", "dest_path": "/v/a.mp4"}, {"title": "T"})
    assert called and called[0][0] == "/v/a.mp4"
