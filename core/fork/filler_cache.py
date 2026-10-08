"""What the filler tools found the last time they looked.

The ReplayGain, Cover Art and Lyrics Fillers go over the whole library on
every scan: they open a file to read its tags or its picture, and ask a
service whether lyrics or artwork exist. This keeps those answers per tool in
a small SQLite file next to the music database (``fork_filler_cache.db``):

* ``files``   — what was read from a file, with the file's size and mtime. The
  next scan costs one ``stat``; a file that changed since (ReplayGain or a
  cover was written, it was re-tagged or replaced) is read again.
* ``lookups`` — what a service answered for a track or album. "Nothing found"
  is only kept once a later question of the same scan was answered with
  something: a service that is down or rate-limiting answers nothing to
  everything, and that must not be remembered.

Each tool's cache has a lifetime (its ``cache_days`` setting, a week by
default). The first scan after it ran out starts from an empty cache, so rows
of tracks that were renamed or moved since do not live on, and a service is
asked again about what it did not have. ``0`` turns the cache off.

A scan runs on one thread; its open cache is held per thread, so the job's
calls find it without passing anything along.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from core.fork import store
from utils.logging_config import get_logger

logger = get_logger("fork.filler_cache")

DEFAULT_DAYS = 7
_COMMIT_EVERY = 500
_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    job TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (job, path)
);
CREATE TABLE IF NOT EXISTS lookups (
    job TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (job, key)
);
CREATE TABLE IF NOT EXISTS created (job TEXT PRIMARY KEY, at REAL NOT NULL);
"""

_local = threading.local()


def cache_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(store.database_path())), "fork_filler_cache.db")


def lifetime_days(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(DEFAULT_DAYS)


class _Session:
    def __init__(self, conn: sqlite3.Connection, job: str):
        self.conn, self.job = conn, job
        self.hits = self.misses = self.unsaved = 0
        self.unanswered: Dict[str, List[Tuple[str, str]]] = {}     # group -> "nothing found" rows held back

    def wrote(self, rows: int = 1) -> None:
        self.unsaved += rows
        if self.unsaved >= _COMMIT_EVERY:
            self.conn.commit()
            self.unsaved = 0


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(cache_path(), timeout=30.0)
    conn.executescript(_SCHEMA)
    return conn


def _forget(conn: sqlite3.Connection, job: str) -> None:
    conn.execute("DELETE FROM files WHERE job = ?", (job,))
    conn.execute("DELETE FROM lookups WHERE job = ?", (job,))
    conn.execute("DELETE FROM created WHERE job = ?", (job,))


def clear(job: str) -> None:
    conn = _connect()
    try:
        _forget(conn, job)
        conn.commit()
    finally:
        conn.close()


def _open(job: str, days: float) -> sqlite3.Connection:
    """The cache file, with ``job``'s rows dropped when they are older than ``days``."""
    conn = _connect()
    now = time.time()
    row = conn.execute("SELECT at FROM created WHERE job = ?", (job,)).fetchone()
    if row is None or now - row[0] > days * 86400 or row[0] > now:
        if row is not None:
            logger.info("[filler cache] %s: older than %g days, this scan checks everything again", job, days)
        _forget(conn, job)
        conn.execute("INSERT INTO created VALUES (?, ?)", (job, now))
        conn.commit()
    return conn


@contextmanager
def session(job: str, days: Any = DEFAULT_DAYS) -> Iterator[None]:
    """Answer :func:`file_value` and :func:`lookup` from ``job``'s cache on
    this thread for the length of the ``with`` block. A cache that cannot be
    opened only means the scan checks everything, as it did without one."""
    days = lifetime_days(days)
    opened: Optional[_Session] = None
    try:
        if days <= 0:
            if os.path.exists(cache_path()):
                clear(job)
        else:
            opened = _Session(_open(job, days), job)
    except Exception as exc:
        logger.warning("[filler cache] %s: not used for this scan: %s", job, exc)
    previous = getattr(_local, "session", None)
    _local.session = opened
    try:
        yield
    finally:
        _local.session = previous
        if opened is not None:
            try:
                opened.conn.commit()
                logger.info("[filler cache] %s: %d answers from the cache, %d checked",
                            job, opened.hits, opened.misses)
            except Exception as exc:
                logger.debug("[filler cache] %s: last rows not saved: %s", job, exc)
            finally:
                opened.conn.close()


def file_value(path: Any, read: Callable[[Any], Any]) -> Any:
    """``read(path)``, or what it returned last time when the file has the
    same size and mtime. The value must survive JSON."""
    current: Optional[_Session] = getattr(_local, "session", None)
    if current is None or not path:
        return read(path)
    try:
        stat = os.stat(path)
        row = current.conn.execute(
            "SELECT size, mtime_ns, value FROM files WHERE job = ? AND path = ?", (current.job, str(path))).fetchone()
        if row and row[0] == stat.st_size and row[1] == stat.st_mtime_ns:
            current.hits += 1
            return json.loads(row[2])
    except Exception as exc:
        logger.debug("[filler cache] %s not looked up: %s", path, exc)
        return read(path)
    value = read(path)
    current.misses += 1
    try:
        current.conn.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?)",
                             (current.job, str(path), stat.st_size, stat.st_mtime_ns, json.dumps(value)))
        current.wrote()
    except Exception as exc:
        logger.debug("[filler cache] %s not stored: %s", path, exc)
    return value


def lookup(group: str, fetch: Callable[..., Any], *args: Any) -> Any:
    """``fetch(*args)``, or what it answered last time. ``group`` names the
    service asked; an empty answer is stored once a later question to the
    same group got a real one."""
    current: Optional[_Session] = getattr(_local, "session", None)
    if current is None:
        return fetch(*args)
    try:
        key = json.dumps([group, *args], sort_keys=True, default=str, ensure_ascii=False)
        row = current.conn.execute(
            "SELECT value FROM lookups WHERE job = ? AND key = ?", (current.job, key)).fetchone()
        if row:
            current.hits += 1
            return json.loads(row[0])
    except Exception as exc:
        logger.debug("[filler cache] lookup not read: %s", exc)
        return fetch(*args)
    value = fetch(*args)
    current.misses += 1
    try:
        held = current.unanswered.setdefault(group, [])
        held.append((key, json.dumps(value)))
        if value:
            current.conn.executemany("INSERT OR REPLACE INTO lookups VALUES (?, ?, ?)",
                                     [(current.job, k, v) for k, v in held])
            current.wrote(len(held))
            held.clear()
    except Exception as exc:
        logger.debug("[filler cache] lookup not stored: %s", exc)
    return value


__all__ = ["DEFAULT_DAYS", "cache_path", "clear", "file_value", "lifetime_days", "lookup", "session"]
