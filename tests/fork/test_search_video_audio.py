"""Search page videos are downloaded as audio and filed into the music library."""

from __future__ import annotations

import pytest

from core.fork import hooks, search_video_audio as sva
from core.itunes_client import Track

VTT = """WEBVTT
Kind: subtitles
Language: en

00:00:01.000 --> 00:00:03.500
♪ Is this the real life? ♪

00:00:03.500 --> 00:00:06.000 align:start position:0%
<c>Is this just</c> &amp; fantasy?

00:00:06.000 --> 00:00:07.000
Is this just &amp; fantasy?

00:00:07.000 --> 00:00:08.000
♪♪

01:02:03.456 --> 01:02:05.000
Open your eyes
"""


@pytest.fixture(autouse=True)
def clean():
    sva.state.clear()
    sva._extract_only.clear()
    yield
    sva.state.clear()
    sva._extract_only.clear()


def test_vtt_becomes_lrc():
    assert sva.vtt_to_lrc(VTT) == (
        "[00:01.00]Is this the real life?\n"
        "[00:03.50]Is this just & fantasy?\n"
        "[62:03.46]Open your eyes\n"
    )
    assert sva.vtt_to_lrc("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n♪\n") == ""
    # SRT cues read the same
    assert sva.vtt_to_lrc("1\n00:00:02,500 --> 00:00:04,000\nHello\n") == "[00:02.50]Hello\n"


def test_subtitle_language_prefers_the_videos_own():
    subs = {"live_chat": [{}], "en": [{}], "ja": [{}], "ko": []}
    assert sva.pick_subtitle_language(subs, "ja") == "ja"
    assert sva.pick_subtitle_language({"en-US": [{}], "fr": [{}]}, "en") == "en-US"
    assert sva.pick_subtitle_language(subs, "") == "en"
    assert sva.pick_subtitle_language({"live_chat": [{}]}, "en") == ""


def test_track_info_takes_the_match_but_the_videos_length():
    track = Track(id="123", name="Bohemian Rhapsody", artists=["Queen"], album="A Night at the Opera",
                  duration_ms=354000, popularity=0, release_date="1975-10-31", track_number=11)
    info = sva.track_info("Queen", "Bohemian Rhapsody", track, 367, "[00:01.00]x\n")
    assert info["id"] == "123"
    assert info["artists"] == [{"name": "Queen"}]
    assert info["album"]["name"] == "A Night at the Opera"
    assert info["album"]["release_date"] == "1975-10-31"
    assert info["track_number"] == 11
    assert info["duration_ms"] == 367000
    assert info["_youtube_lrc"] == "[00:01.00]x\n"

    bare = sva.track_info("Someone", "Song", None, 0, "")
    assert bare["album"] == {} and "_youtube_lrc" not in bare
    assert bare["artists"] == [{"name": "Someone"}]


class FakeApp:
    def __init__(self):
        self.queued = []
        self.tasks = {}
        self.percent = {}

    def deps(self, results=(), subs=("", 0)):
        def queue(info, candidate, name, profile_id):
            self.queued.append((info, candidate, name, profile_id))
            self.tasks["t1"] = {"status": "queued", "download_id": None}
            return "b1", "t1"

        return sva.Deps(
            search_tracks=lambda q, n: list(results),
            fetch_subtitles=lambda url: subs,
            queue=queue,
            task=lambda tid: self.tasks.get(tid),
            progress=lambda did: self.percent.get(did),
            start_thread=lambda fn, name: fn(),
        )


VIDEO = {"video_id": "fJ9rUzIMcZQ", "url": "https://www.youtube.com/watch?v=fJ9rUzIMcZQ",
         "title": "Queen – Bohemian Rhapsody (Official Video Remastered)", "channel": "Queen Official",
         "duration": 359}


def test_a_click_queues_the_audio_as_a_pinned_youtube_task():
    fake = FakeApp()
    match = Track(id="9", name="Bohemian Rhapsody", artists=["Queen"], album="A Night at the Opera",
                  duration_ms=354000, popularity=0)
    deps = fake.deps(results=[match], subs=(VTT, 360))

    assert sva.start(VIDEO, 7, deps) == {"success": True, "video_id": "fJ9rUzIMcZQ"}
    info, candidate, name, profile_id = fake.queued[0]
    assert candidate["username"] == "youtube"
    assert candidate["filename"] == "fJ9rUzIMcZQ||Queen – Bohemian Rhapsody (Official Video Remastered)"
    assert name == "Queen - Bohemian Rhapsody" and profile_id == 7
    assert info["album"]["name"] == "A Night at the Opera"
    assert info["duration_ms"] == 360000
    assert info["_youtube_lrc"].startswith("[00:01.00]Is this the real life?")
    assert sva.extract_only("fJ9rUzIMcZQ")

    # running: progress from the download; a second click is refused
    fake.tasks["t1"] = {"status": "downloading", "download_id": "d1"}
    fake.percent["d1"] = 42.0
    assert sva.status("fJ9rUzIMcZQ", deps) == {"status": "downloading", "progress": 42.0}
    assert sva.start(VIDEO, 7, deps)["code"] == 409

    fake.tasks["t1"]["status"] = "completed"
    assert sva.status("fJ9rUzIMcZQ", deps) == {"status": "completed", "progress": 100}
    assert not sva.extract_only("fJ9rUzIMcZQ")
    assert sva.status("other", deps) is None


def test_no_match_files_the_parsed_name_and_a_failure_reports_error():
    fake = FakeApp()
    deps = fake.deps()
    sva.start(VIDEO, None, deps)
    info = fake.queued[0][0]
    assert info["name"] == "Bohemian Rhapsody" and info["artists"] == [{"name": "Queen"}]
    assert info["album"] == {} and info["duration_ms"] == 359000
    assert "_youtube_lrc" not in info

    fake.tasks["t1"] = {"status": "failed", "error_message": "nope"}
    assert sva.status(VIDEO["video_id"], deps) == {"status": "error", "progress": 0, "error": "nope"}
    # a failed one may be clicked again
    assert sva.start(VIDEO, None, deps)["success"]


def test_subtitles_become_the_tracks_lrc(tmp_path, monkeypatch):
    from core.lyrics_client import lyrics_client

    calls = []
    monkeypatch.setattr(type(lyrics_client), "create_lrc_file",
                        lambda self, path, title, artist, *a, **k: calls.append((path, title, artist)) or True)
    audio = tmp_path / "Queen - Bohemian Rhapsody.opus"
    audio.write_bytes(b"x")
    context = {"track_info": {"name": "Bohemian Rhapsody", "artists": [{"name": "Queen"}],
                              "_youtube_lrc": "[00:01.00]Is this the real life?\n"}}

    assert hooks.write_video_lyrics(str(audio), context) is True
    assert (tmp_path / "Queen - Bohemian Rhapsody.lrc").read_text() == "[00:01.00]Is this the real life?\n"
    assert calls == [(str(audio), "Bohemian Rhapsody", "Queen")]
    # any other track goes to LRClib as before
    assert hooks.write_video_lyrics(str(audio), {"track_info": {"name": "x"}}) is False


def test_an_extract_only_download_remuxes_the_best_original_stream(monkeypatch):
    import core.youtube_client as yt

    monkeypatch.setattr(yt, "_upstream_postprocessor_from_config",
                        lambda: {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"})
    monkeypatch.setattr(yt, "_upstream_requested_tier", lambda: "mp3_320")
    seen = []

    def fake_download(self, url, title):
        seen.append((yt.youtube_audio_postprocessor_from_config(), yt.youtube_requested_tier()))
        return "/tmp/x.opus"

    monkeypatch.setattr(yt, "_upstream_download_sync", fake_download)
    client = object.__new__(yt.YouTubeClient)

    sva._extract_only.add("fJ9rUzIMcZQ")
    yt.YouTubeClient._download_sync(client, VIDEO["url"], "t")
    yt.YouTubeClient._download_sync(client, "https://www.youtube.com/watch?v=aaaaaaaaaaa", "t")
    assert seen[0] == ({"key": "FFmpegExtractAudio", "preferredcodec": "best"}, "opus_256")
    assert seen[1] == ({"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}, "mp3_320")
