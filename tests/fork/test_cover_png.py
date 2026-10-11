"""cover.png before cover.jpg for the album folder's cover file."""

import os

import pytest

from core.fork import config, cover_png
from core.metadata import art_apply, artwork

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2000


@pytest.fixture
def switch(monkeypatch):
    state = {"on": True}
    real = config.get
    monkeypatch.setattr(config, "get", lambda key, default=None: state["on"] if key == "cover.prefer_png" else real(key, default))
    return state


def _art(monkeypatch, data, seen=None):
    """The upstream fetcher, answering ``data`` for any address."""
    def fetch(url):
        if seen is not None:
            seen.append(url)
        return data, "image/png" if data == _PNG else "image/jpeg"
    monkeypatch.setattr(artwork, "_upstream_fetch_art_bytes", fetch)
    return fetch


def _album(url="https://example.test/cover"):
    return {"album_name": "A", "album_image_url": url}


def test_png_variants_only_for_sources_that_can_serve_one():
    assert cover_png.png_variants("https://is1-ssl.mzstatic.com/image/x/3000x3000bb.jpg") == \
        ["https://is1-ssl.mzstatic.com/image/x/3000x3000bb.png"]
    assert cover_png.png_variants("https://coverartarchive.org/release/abc/front-1200") == \
        ["https://coverartarchive.org/release/abc/front"]
    assert cover_png.png_variants("https://i.scdn.co/image/abc") == []
    assert cover_png.png_variants("") == []


def test_a_png_cover_is_saved_as_cover_png(tmp_path, monkeypatch, switch):
    _art(monkeypatch, _PNG)
    artwork.download_cover_art(_album(), str(tmp_path), {}, force=True)
    assert sorted(os.listdir(tmp_path)) == ["cover.png"]
    assert (tmp_path / "cover.png").read_bytes() == _PNG


def test_a_jpeg_cover_stays_cover_jpg(tmp_path, monkeypatch, switch):
    _art(monkeypatch, _JPEG)
    artwork.download_cover_art(_album(), str(tmp_path), {}, force=True)
    assert sorted(os.listdir(tmp_path)) == ["cover.jpg"]


def test_the_png_of_the_source_is_tried_first(tmp_path, monkeypatch, switch):
    _art(monkeypatch, _JPEG)
    asked = []
    monkeypatch.setattr(cover_png, "fetch_png", lambda url: asked.append(url) or (_PNG, "image/png"))
    artwork.download_cover_art(_album("https://is1-ssl.mzstatic.com/image/x/600x600bb.jpg"), str(tmp_path), {}, force=True)
    assert asked and "mzstatic.com" in asked[0]
    assert sorted(os.listdir(tmp_path)) == ["cover.png"]


def test_a_folder_that_has_a_cover_png_gets_no_cover_jpg(tmp_path, monkeypatch, switch):
    (tmp_path / "cover.png").write_bytes(_PNG)
    seen = []
    _art(monkeypatch, _JPEG, seen)
    artwork.download_cover_art(_album(), str(tmp_path), {}, force=True)
    assert sorted(os.listdir(tmp_path)) == ["cover.png"]
    assert not seen                                     # and nothing was fetched


def test_switched_off_everything_is_as_before(tmp_path, monkeypatch, switch):
    switch["on"] = False
    _art(monkeypatch, _PNG)
    artwork.download_cover_art(_album(), str(tmp_path), {}, force=True)
    assert sorted(os.listdir(tmp_path)) == ["cover.jpg"]


def test_a_png_extracted_by_the_filler_becomes_cover_png(tmp_path, monkeypatch, switch):
    song = tmp_path / "a.flac"
    song.write_bytes(b"x")
    (tmp_path / "cover.jpg").write_bytes(_PNG)           # what the filler wrote from embedded PNG art
    monkeypatch.setattr(art_apply, "_upstream_apply_art_to_album_files", lambda *a, **k: {"cover_written": True})
    art_apply.apply_art_to_album_files([str(song)], {}, {}, folder=str(tmp_path))
    assert sorted(p for p in os.listdir(tmp_path) if p.startswith("cover")) == ["cover.png"]
