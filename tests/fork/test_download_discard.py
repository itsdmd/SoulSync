"""Discard All: unfinished batches vanish and nothing reaches the wishlist."""

from __future__ import annotations

import pytest

from core import runtime_state
from core.fork import download_discard, download_pause
from core.runtime_state import download_batches, download_tasks


@pytest.fixture(autouse=True)
def clean():
    download_pause.reset()
    download_tasks.clear()
    download_batches.clear()
    yield
    download_pause.reset()
    download_tasks.clear()
    download_batches.clear()


def test_unfinished_batches_are_dropped_without_wishlisting():
    download_tasks.update({"a": {"status": "queued"}, "b": {"status": "downloading"}, "c": {"status": "completed"}})
    download_batches["live"] = {"queue": ["a", "b"], "phase": "downloading",
                                "cancelled_tracks": {1, 2}, "playlist_id": "wishlist"}
    download_batches["done"] = {"queue": ["c"], "phase": "complete"}
    runtime_state.wishlist_auto_processing = True

    result = download_discard.discard_all()

    assert result == {"discarded_batches": 1, "discarded_tasks": 2}
    assert list(download_batches) == ["done"]
    assert list(download_tasks) == ["c"]
    assert runtime_state.wishlist_auto_processing is False


def test_a_batch_busy_writing_the_wishlist_is_left_alone():
    download_batches["w"] = {"queue": [], "phase": "downloading",
                             "wishlist_processing_started": True}
    assert download_discard.discard_all()["discarded_batches"] == 0
    assert "w" in download_batches


def test_resume_does_not_restart_a_discarded_batch():
    download_batches["x"] = {"queue": [], "phase": "downloading"}
    download_pause.hold("x", lambda b: None)
    download_discard.discard_all()
    assert download_pause.state()["held_batches"] == 0
