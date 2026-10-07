"""Fixtures for the fork's tests: an isolated DB, an in-memory config and a
scripted Ollama, so nothing here touches the network or the real config."""

from __future__ import annotations

import pytest

from core.fork import config, ollama, store


class FakeConfigManager:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        node = self.data
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, key, value):
        node = self.data
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def batch(self):
        class _Batch:
            def __enter__(self_inner):
                return self

            def __exit__(self_inner, *exc):
                return False

        return _Batch()


@pytest.fixture(autouse=True)
def fork_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "fork-test.db"))
    monkeypatch.setenv("SOULSYNC_FORK_TESTING", "1")
    store._initialised.clear()
    cm = FakeConfigManager()
    monkeypatch.setattr(config, "_config_manager", lambda: cm)
    ollama.reset_cooldown()
    yield cm
    store._initialised.clear()


@pytest.fixture
def llm(monkeypatch):
    """Scripted model: ``llm.replies`` is a list of dicts (or exceptions)
    returned in order; ``llm.calls`` records (task, payload)."""

    class Script:
        def __init__(self):
            self.replies = []
            self.calls = []

        def __call__(self, task, system, payload, schema, **kwargs):
            self.calls.append((task, payload))
            if not self.replies:
                raise ollama.OllamaError("no scripted reply")
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    script = Script()
    monkeypatch.setattr(ollama, "chat_json", script)
    return script
