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

    def deps(self, results=(), subs=("", 0, "")):
        def queue(info, candidate, name, profile_id):
            self.queued.append((info, candidate, name, profile_id))
            self.tasks["t1"] = {"status": "queued", "download_id": None}
            return "b1", "t1"

        return sva.Deps(
            search_tracks=lambda q, n: list(results),
            fetch_info=lambda url: subs,
            queue=queue,
            task=lambda tid: self.tasks.get(tid),
            progress=lambda did: self.percent.get(did),
            start_thread=lambda fn, name: fn(),
        )


VIDEO = {"video_id": "fJ9rUzIMcZQ", "url": "https://www.youtube.com/watch?v=fJ9rUzIMcZQ",
         "title": "Queen – Bohemian Rhapsody (Official Video Remastered)", "channel": "Queen Official",
         "duration": 359}


def test_a_click_queues_the_audio_as_a_pinned_youtube_task():
    thumb = "/tmp/x/fJ9rUzIMcZQ.jpg"
    fake = FakeApp()
    match = Track(id="9", name="Bohemian Rhapsody", artists=["Queen"], album="A Night at the Opera",
                  duration_ms=354000, popularity=0)
    deps = fake.deps(results=[match], subs=(VTT, 360, thumb))

    assert sva.start(VIDEO, 7, deps) == {"success": True, "video_id": "fJ9rUzIMcZQ"}
    info, candidate, name, profile_id = fake.queued[0]
    assert candidate["username"] == "youtube"
    assert candidate["filename"] == "fJ9rUzIMcZQ||Queen – Bohemian Rhapsody (Official Video Remastered)"
    assert name == "Queen - Bohemian Rhapsody" and profile_id == 7
    assert info["album"]["name"] == "A Night at the Opera"
    assert info["duration_ms"] == 360000
    assert info["_youtube_lrc"].startswith("[00:01.00]Is this the real life?")
    assert info["_youtube_thumbnail"] == thumb
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


# ── thumbnail and cover ─────────────────────────────────────────────────

def _image(path, size=(160, 90), color=(200, 30, 30)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    # a marker in the centre so the crop can be told from the whole picture
    for x in range(size[0] // 2 - 5, size[0] // 2 + 5):
        for y in range(size[1] // 2 - 5, size[1] // 2 + 5):
            img.putpixel((x, y), (0, 255, 0))
    img.save(path)
    return str(path)


def _ctx(thumb):
    return {"track_info": {"name": "x", "_youtube_thumbnail": thumb}}


def test_square_crop_takes_the_centre(tmp_path):
    from PIL import Image

    src = _image(tmp_path / "wide.jpg", (160, 90))
    sva.crop_square_png(src, str(tmp_path / "cover.png"))
    with Image.open(tmp_path / "cover.png") as cover:
        assert cover.format == "PNG" and cover.size == (90, 90)
        centre, corner = cover.getpixel((45, 45)), cover.getpixel((2, 2))
        assert centre[1] > 200 and centre[0] < 60               # the centre marker survived (jpeg is lossy)
        assert corner[0] > 180 and corner[1] < 60


def test_thumbnail_and_cover_are_saved_beside_the_audio(tmp_path):
    thumb = _image(tmp_path / "tmp" / "vid.webp")
    lib = tmp_path / "lib" / "Queen" / "Bohemian Rhapsody"
    lib.mkdir(parents=True)
    audio = lib / "Queen - Bohemian Rhapsody.opus"
    audio.write_bytes(b"x")
    (lib / "cover.png").write_bytes(b"album art from the pipeline")

    assert hooks.write_video_art(str(audio), _ctx(thumb)) is True
    assert (lib / "original-thumbnail.webp").is_file()
    assert (lib / "cover.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"     # the crop replaced the lone track's cover
    assert not (tmp_path / "tmp" / "vid.webp").exists()
    assert hooks.write_video_art(str(audio), _ctx(thumb)) is False      # nothing left to place
    assert hooks.write_video_art(str(audio), {"track_info": {"name": "x"}}) is False


def test_a_video_in_an_album_folder_keeps_the_albums_cover(tmp_path):
    thumb = _image(tmp_path / "tmp" / "vid.jpg")
    album = tmp_path / "lib" / "Queen" / "A Night at the Opera"
    album.mkdir(parents=True)
    (album / "01 Death on Two Legs.flac").write_bytes(b"x")
    audio = album / "11 Bohemian Rhapsody.opus"
    audio.write_bytes(b"x")
    (album / "cover.png").write_bytes(b"album cover")

    hooks.write_video_art(str(audio), _ctx(thumb))
    assert (album / "cover.png").read_bytes() == b"album cover"
    assert (album / "original-thumbnail.jpg").is_file()


def _filed(tmp_path, name="a"):
    folder = tmp_path / name
    folder.mkdir()
    audio = folder / "Song.opus"
    audio.write_bytes(b"x")
    (folder / "original-thumbnail.jpg").write_bytes(b"orig")
    (folder / "cover.png").write_bytes(b"crop")
    return folder, audio


def test_thumbnail_and_cover_move_with_the_last_track(tmp_path):
    src, audio = _filed(tmp_path)
    dst = tmp_path / "b"
    dst.mkdir()
    new = dst / "Song.opus"
    audio.rename(new)

    hooks.move_lyrics_backup(audio, new)      # the call every audio-moving site already makes
    assert (dst / "original-thumbnail.jpg").read_bytes() == b"orig"
    assert (dst / "cover.png").read_bytes() == b"crop"
    assert not (src / "original-thumbnail.jpg").exists() and not (src / "cover.png").exists()


def test_thumbnail_is_copied_while_other_tracks_stay_behind(tmp_path):
    src, audio = _filed(tmp_path)
    (src / "Other.opus").write_bytes(b"x")
    dst = tmp_path / "b"
    dst.mkdir()
    new = dst / "Song.opus"
    audio.rename(new)

    hooks.move_lyrics_backup(audio, new)
    assert (dst / "original-thumbnail.jpg").is_file() and (dst / "cover.png").is_file()
    assert (src / "original-thumbnail.jpg").is_file() and (src / "cover.png").is_file()


def test_travel_never_replaces_and_leaves_nothing_stranded(tmp_path):
    src, audio = _filed(tmp_path)
    dst = tmp_path / "b"
    dst.mkdir()
    (dst / "original-thumbnail.jpg").write_bytes(b"orig")      # same picture already there
    (dst / "cover.png").write_bytes(b"theirs")
    new = dst / "Song.opus"
    audio.rename(new)

    sva.move_art(audio, new)
    assert (dst / "cover.png").read_bytes() == b"theirs"
    assert not (src / "original-thumbnail.jpg").exists()       # identical copy removed from the old folder
    assert (src / "cover.png").exists()                        # a different one is not ours to delete


def test_an_album_cover_alone_is_not_taken_along(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    audio = src / "Song.flac"
    audio.write_bytes(b"x")
    (src / "cover.png").write_bytes(b"album")
    dst = tmp_path / "b"
    dst.mkdir()
    new = dst / "Song.flac"
    audio.rename(new)

    assert sva.move_art(audio, new) == []
    assert not (dst / "cover.png").exists()


def test_tag_editor_move_and_delete_handle_the_thumbnail(tmp_path):
    src, audio = _filed(tmp_path)
    audio.unlink()
    hooks.remove_lyrics_backup(str(audio))     # the delete paths call this after removing the file
    assert not (src / "original-thumbnail.jpg").exists() and not (src / "cover.png").exists()


def test_thumbnails_are_tried_biggest_first_jpg_before_webp():
    info = {"thumbnail": "https://i.ytimg.com/vi/ID/hqdefault.jpg", "thumbnails": [
        {"url": "https://i.ytimg.com/vi/ID/hqdefault.jpg", "preference": -7},
        {"url": "https://i.ytimg.com/vi_webp/ID/maxresdefault.webp", "preference": 0},
        {"url": "https://i.ytimg.com/vi/ID/maxresdefault.jpg", "preference": -1},
        {"url": "https://i.ytimg.com/vi/ID/hq720.jpg", "preference": -3},
    ]}
    assert sva._thumbnail_urls(info) == [
        "https://i.ytimg.com/vi/ID/maxresdefault.jpg",
        "https://i.ytimg.com/vi/ID/hq720.jpg",
        "https://i.ytimg.com/vi/ID/hqdefault.jpg",
    ]
