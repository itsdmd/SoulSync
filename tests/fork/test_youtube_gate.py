"""YouTube's "not a bot" refusal pauses probes and downloads for a while."""

from __future__ import annotations

import logging

import pytest

from core.fork import youtube_gate

REFUSAL = ("ERROR: [youtube] l_QkKIxkaRI: Sign in to confirm you’re not a bot. "
           "Use --cookies-from-browser or --cookies for the authentication.")


@pytest.fixture(autouse=True)
def clean():
    youtube_gate.reset()
    yield
    youtube_gate.reset()


def test_only_the_refusal_starts_the_pause(monkeypatch):
    assert youtube_gate.note("ERROR: [youtube] abc: Video unavailable") is False
    assert youtube_gate.note("HTTP Error 403: Forbidden") is False
    assert youtube_gate.blocked() is False

    clock = [1000.0]
    monkeypatch.setattr(youtube_gate.time, "monotonic", lambda: clock[0])
    assert youtube_gate.note(REFUSAL) is True
    assert youtube_gate.blocked() and youtube_gate.blocked()
    assert youtube_gate.state() == {"blocked": True, "seconds_left": youtube_gate.PAUSE_SECONDS, "skipped": 2}

    clock[0] += youtube_gate.PAUSE_SECONDS + 1                 # the pause ran out: the next try goes through
    assert youtube_gate.blocked() is False
    youtube_gate.note(REFUSAL.replace("’", "'"))               # still refused: paused again
    assert youtube_gate.blocked() is True


def test_the_client_stops_probing_and_downloading_while_refused(monkeypatch):
    import core.youtube_client as yt

    calls = []
    monkeypatch.setattr(yt, "_upstream_refresh_claimed_quality", lambda self, c, *a, **k: calls.append("probe"))
    monkeypatch.setattr(yt, "_upstream_download_sync", lambda self, url, title: calls.append("download") or "/f")
    client = yt.YouTubeClient.__new__(yt.YouTubeClient)

    client.refresh_claimed_quality([])
    assert client._download_sync("https://www.youtube.com/watch?v=x", "Song") == "/f"
    assert calls == ["probe", "download"]

    yt.logger.error("Download attempt 1 failed: %s", REFUSAL)   # what the client logs when refused
    assert youtube_gate.state()["blocked"] is True
    client.refresh_claimed_quality([])
    assert client._download_sync("https://www.youtube.com/watch?v=y", "Other") is None
    assert calls == ["probe", "download"]
    assert sum(isinstance(h, youtube_gate._Watch) for h in logging.getLogger(yt.logger.name).handlers) == 1
