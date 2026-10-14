"""Size and format on the duplicate-track finding card."""

import os

from core.fork import dup_info


class _Worker:
    transfer_folder = ""
    _config_manager = None


def _page(*paths):
    return {"items": [{"finding_type": "duplicate_tracks",
                       "details": {"tracks": [{"id": i, "file_path": p} for i, p in enumerate(paths)]}},
                      {"finding_type": "orphan_file", "details": {"tracks": [{"file_path": "x.mp3"}]}}]}


def test_each_copy_gets_its_size_and_format(tmp_path):
    big, small = tmp_path / "a.flac", tmp_path / "b.mp3"
    big.write_bytes(b"x" * 5000)
    small.write_bytes(b"x" * 100)
    page = _page(str(big), str(small), str(tmp_path / "gone.ogg"))
    dup_info.enrich(_Worker(), page)
    a, b, gone = page["items"][0]["details"]["tracks"]
    assert (a["file_size"], a["format"], a["file_found"]) == (5000, "FLAC", True)
    assert (b["file_size"], b["format"]) == (100, "MP3")
    assert gone["file_found"] is False and "file_size" not in gone and gone["format"] == "OGG"
    assert "file_size" not in page["items"][1]["details"]["tracks"][0]      # other types are left alone


def test_an_empty_page_is_fine():
    dup_info.enrich(_Worker(), {"items": []})
    dup_info.enrich(_Worker(), {})
