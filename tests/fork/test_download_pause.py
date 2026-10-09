"""Pause All on the Downloads page: queued downloads are held, and started on
Resume All. The switch is not stored, so a restart begins unpaused."""

from __future__ import annotations

import pytest

from core.downloads import lifecycle
from core.fork import download_pause
from core.runtime_state import download_batches, download_tasks
from tests.downloads.test_downloads_lifecycle import _build_deps


@pytest.fixture(autouse=True)
def clean():
    download_pause.reset()
    download_tasks.clear()
    download_batches.clear()
    yield
    download_pause.reset()
    download_tasks.clear()
    download_batches.clear()


def _batch(batch_id, *tasks):
    for task in tasks:
        download_tasks[task] = {"status": "queued"}
    download_batches[batch_id] = {"queue": list(tasks), "queue_index": 0, "active_count": 0, "max_concurrent": 1}


def _started(rec):
    return [call[1] for call in rec.calls if call[0] == "submit_dl"]


def test_a_paused_queue_starts_nothing_until_resumed():
    _batch("b1", "t1", "t2")
    _batch("b2", "t3")
    deps, rec = _build_deps()

    assert download_pause.pause() == {"paused": True, "held_batches": 0}
    lifecycle.start_next_batch_of_downloads("b1", deps)
    lifecycle.start_next_batch_of_downloads("b2", deps)
    lifecycle.start_next_batch_of_downloads("b1", deps)
    assert _started(rec) == [] and download_tasks["t1"]["status"] == "queued"
    assert download_pause.state() == {"paused": True, "held_batches": 2}

    result = download_pause.resume()
    assert result == {"paused": False, "held_batches": 0, "resumed_batches": 2}
    assert _started(rec) == [("t1", "b1"), ("t3", "b2")]
    assert download_tasks["t1"]["status"] == "searching"

    download_batches["b1"]["active_count"] = 0                  # t1 finished: the queue moves on as usual
    lifecycle.start_next_batch_of_downloads("b1", deps)
    assert _started(rec)[-1] == ("t2", "b1")


def test_a_restart_begins_unpaused():
    import importlib

    download_pause.pause()
    importlib.reload(download_pause)                            # what a new process does
    assert download_pause.is_paused() is False


def test_the_page_reads_and_sets_the_switch(monkeypatch):
    import core.profile_context as profile_context
    from flask import Flask

    monkeypatch.setattr(profile_context, "admin_only", lambda fn: fn)
    import importlib

    import api.fork as fork_api

    fork_api = importlib.reload(fork_api)
    app = Flask(__name__)
    app.register_blueprint(fork_api.create_blueprint())
    client = app.test_client()
    assert client.get("/api/fork/downloads/pause").get_json()["paused"] is False
    assert client.post("/api/fork/downloads/pause", json={"paused": True}).get_json()["paused"] is True
    assert client.get("/api/fork/downloads/pause").get_json()["paused"] is True
    assert client.post("/api/fork/downloads/pause", json={"paused": False}).get_json()["paused"] is False
    monkeypatch.undo()
    importlib.reload(fork_api)
