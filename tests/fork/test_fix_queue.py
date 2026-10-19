"""The findings fix queue starts the next entry once the running job is done."""

from __future__ import annotations

import pytest

from core.fork import fix_queue


class FakeWorker:
    def __init__(self):
        self.running = False
        self.started = []
        self.refuse = {}

    def get_bulk_fix_status(self):
        return {"running": self.running}

    def start_bulk_fix(self, finding_type=None, fix_action=None, **_):
        if finding_type in self.refuse:
            return {"started": False, "error": self.refuse[finding_type]}
        self.started.append((finding_type, fix_action))
        self.running = True
        return {"started": True, "total": 3}


@pytest.fixture
def worker(monkeypatch):
    fix_queue.reset()
    fake = FakeWorker()
    monkeypatch.setattr(fix_queue, "_worker", lambda: fake)
    monkeypatch.setattr(fix_queue, "_ensure_thread", lambda: None)     # drive step() by hand
    yield fake
    fix_queue.reset()


def test_entries_start_one_after_another(worker):
    fix_queue.add("cover_art", None, "Cover art", 5)
    fix_queue.add("dead_file", "remove", "Dead files", 2)

    assert fix_queue.step() is True
    assert worker.started == [("cover_art", None)]
    assert fix_queue.state()["current"]["finding_type"] == "cover_art"

    fix_queue.step()                       # still running: nothing new starts
    assert len(worker.started) == 1

    worker.running = False
    assert fix_queue.step() is False       # last entry started, queue now empty
    assert worker.started[-1] == ("dead_file", "remove")


def test_the_same_type_and_action_is_queued_once(worker):
    assert fix_queue.add("cover_art", None, "Cover art", 5)["added"] is True
    assert fix_queue.add("cover_art", None, "Cover art", 5)["duplicate"] is True
    assert fix_queue.add("cover_art", "x", "Cover art", 5)["added"] is True


def test_an_entry_that_cannot_start_is_skipped_and_reported(worker):
    worker.refuse["cover_art"] = "No pending fixable findings match"
    fix_queue.add("cover_art", None, "Cover art", 5)
    fix_queue.add("lyrics", None, "Lyrics", 1)

    fix_queue.step()
    assert fix_queue.state()["problems"][0]["error"] == "No pending fixable findings match"
    fix_queue.step()
    assert worker.started == [("lyrics", None)]


def test_remove_one_or_all(worker):
    first = fix_queue.add("a", None, "A", 1)
    fix_queue.add("b", None, "B", 1)
    item_id = fix_queue.state()["items"][0]["id"]
    assert fix_queue.remove(item_id) == 1
    assert [i["finding_type"] for i in fix_queue.state()["items"]] == ["b"]
    assert fix_queue.remove() == 1
    assert first["added"] is True
