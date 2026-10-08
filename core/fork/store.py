"""SQLite tables the fork adds to SoulSync's music database.

* ``fork_translations``   — one row per (kind, original) so an album or title
  always gets the same translation. ``user_edited`` rows are never
  overwritten by the model.
* ``fork_artist_names``   — tagging rules: original artist name -> the name
  to write instead. ``source`` is ``manual`` (set in the GUI), ``musicbrainz``
  (looked up) or ``none`` (looked up, nothing found).
* ``fork_artist_aliases`` — cached MusicBrainz names for an artist, used to
  anchor search suggestions to the right artist.
* ``fork_search_terms``   — cached query suggestions per track.

Tables are created on first use, so no upstream migration is touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

KINDS = ("album", "title")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fork_translations (
    kind TEXT NOT NULL,
    original TEXT NOT NULL,
    translated TEXT NOT NULL,
    model TEXT,
    user_edited INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (kind, original)
);
CREATE TABLE IF NOT EXISTS fork_artist_names (
    original TEXT NOT NULL PRIMARY KEY COLLATE NOCASE,
    replacement TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    mbid TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fork_artist_aliases (
    name TEXT NOT NULL PRIMARY KEY COLLATE NOCASE,
    aliases TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fork_search_terms (
    cache_key TEXT NOT NULL PRIMARY KEY,
    variants TEXT NOT NULL,
    model TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fork_album_folders (
    source TEXT NOT NULL,
    album_id TEXT NOT NULL,
    name_key TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL,
    album_name TEXT NOT NULL DEFAULT '',
    artist_name TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (source, album_id)
);
CREATE INDEX IF NOT EXISTS idx_fork_album_folders_name ON fork_album_folders (name_key);
CREATE TABLE IF NOT EXISTS fork_album_checks (
    source TEXT NOT NULL,
    album_id TEXT NOT NULL,
    found INTEGER NOT NULL,
    total INTEGER NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    checked_at REAL NOT NULL,
    PRIMARY KEY (source, album_id)
);
"""

_initialised: set = set()
_init_lock = threading.Lock()


def database_path() -> str:
    return os.environ.get("DATABASE_PATH", "database/music_library.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = database_path()
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        if path not in _initialised:
            with _init_lock:
                conn.executescript(_SCHEMA)
                _initialised.add(path)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── translations ────────────────────────────────────────────────────────

def get_translation(kind: str, original: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM fork_translations WHERE kind = ? AND original = ?", (kind, original)
        ).fetchone()
    return dict(row) if row else None


def find_translation(kind: str, original: str) -> Optional[Dict[str, Any]]:
    """The record for ``original``, or for the same name written in the other
    Chinese script (相變臨界 / 相变临界), so both always get one translation."""
    row = get_translation(kind, original)
    if row:
        return row
    from core.fork.cjk import contains_cjk, fold

    if not contains_cjk(original):
        return None
    key = fold(original)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM fork_translations WHERE kind = ? AND LENGTH(original) = ?", (kind, len(original))
        ).fetchall()
    for candidate in rows:
        if fold(candidate["original"]) == key:
            return dict(candidate)
    return None


def save_translation(kind: str, original: str, translated: str, *, model: str = "",
                     user_edited: bool = False) -> None:
    """Insert or update. A model result never replaces a user-edited row."""
    now = time.time()
    with connect() as conn:
        if user_edited:
            conn.execute(
                """INSERT INTO fork_translations (kind, original, translated, model, user_edited, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(kind, original) DO UPDATE SET
                       translated = excluded.translated, user_edited = 1, updated_at = excluded.updated_at""",
                (kind, original, translated, model, now, now),
            )
        else:
            conn.execute(
                """INSERT INTO fork_translations (kind, original, translated, model, user_edited, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?)
                   ON CONFLICT(kind, original) DO UPDATE SET
                       translated = excluded.translated, model = excluded.model, updated_at = excluded.updated_at
                   WHERE fork_translations.user_edited = 0""",
                (kind, original, translated, model, now, now),
            )


def delete_translation(kind: str, original: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM fork_translations WHERE kind = ? AND original = ?", (kind, original))
        return cur.rowcount > 0


def list_translations(kind: Optional[str] = None, search: str = "", limit: int = 200,
                      offset: int = 0) -> Dict[str, Any]:
    where, params = [], []
    if kind in KINDS:
        where.append("kind = ?")
        params.append(kind)
    if search:
        where.append("(original LIKE ? OR translated LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM fork_translations {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM fork_translations {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


# ── artist names ────────────────────────────────────────────────────────

def get_artist_name(original: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM fork_artist_names WHERE original = ?", (original,)).fetchone()
    return dict(row) if row else None


def save_artist_name(original: str, replacement: str, source: str, mbid: str = "") -> None:
    """Insert or update. A lookup result never replaces a manual rule."""
    now = time.time()
    with connect() as conn:
        if source == "manual":
            conn.execute(
                """INSERT INTO fork_artist_names (original, replacement, source, mbid, updated_at)
                   VALUES (?, ?, 'manual', ?, ?)
                   ON CONFLICT(original) DO UPDATE SET
                       replacement = excluded.replacement, source = 'manual', updated_at = excluded.updated_at""",
                (original, replacement, mbid, now),
            )
        else:
            conn.execute(
                """INSERT INTO fork_artist_names (original, replacement, source, mbid, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(original) DO UPDATE SET
                       replacement = excluded.replacement, source = excluded.source,
                       mbid = excluded.mbid, updated_at = excluded.updated_at
                   WHERE fork_artist_names.source != 'manual'""",
                (original, replacement, source, mbid, now),
            )


def delete_artist_name(original: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM fork_artist_names WHERE original = ?", (original,))
        return cur.rowcount > 0


def list_artist_names(search: str = "", include_misses: bool = False, limit: int = 500,
                      offset: int = 0) -> Dict[str, Any]:
    where, params = [], []
    if not include_misses:
        where.append("source != 'none'")
    if search:
        where.append("(original LIKE ? OR replacement LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM fork_artist_names {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM fork_artist_names {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


# ── artist aliases ──────────────────────────────────────────────────────

def get_artist_aliases(name: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT aliases, updated_at FROM fork_artist_aliases WHERE name = ?", (name,)).fetchone()
    if not row:
        return None
    try:
        aliases = json.loads(row["aliases"])
    except ValueError:
        return None
    return {"aliases": aliases if isinstance(aliases, list) else [], "updated_at": row["updated_at"]}


def save_artist_aliases(name: str, aliases: List[str]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fork_artist_aliases (name, aliases, updated_at) VALUES (?, ?, ?)",
            (name, json.dumps(aliases, ensure_ascii=False), time.time()),
        )


# ── search terms ────────────────────────────────────────────────────────

def get_search_terms(cache_key: str) -> Optional[List[Dict[str, str]]]:
    with connect() as conn:
        row = conn.execute("SELECT variants FROM fork_search_terms WHERE cache_key = ?", (cache_key,)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["variants"])
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def save_search_terms(cache_key: str, variants: List[Dict[str, str]], model: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fork_search_terms (cache_key, variants, model, created_at) VALUES (?, ?, ?, ?)",
            (cache_key, json.dumps(variants, ensure_ascii=False), model, time.time()),
        )


def clear_search_terms() -> int:
    with connect() as conn:
        conn.execute("DELETE FROM fork_artist_aliases")
        return conn.execute("DELETE FROM fork_search_terms").rowcount


# ── album folders ───────────────────────────────────────────────────────
# The folder the user picked for an album in the album pop-up. Keyed by the
# metadata source's album id; ``name_key`` (artist + album name) lets the same
# album opened from another source find it too.

def get_album_folder(source: str, album_id: str, name_key: str = "") -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = None
        if album_id:
            row = conn.execute("SELECT * FROM fork_album_folders WHERE source = ? AND album_id = ?",
                               (source, album_id)).fetchone()
        if row is None and album_id and not album_id.startswith("name:"):
            # the same id asked about without (or with another spelling of) its source
            row = conn.execute(
                "SELECT * FROM fork_album_folders WHERE album_id = ? ORDER BY updated_at DESC LIMIT 1",
                (album_id,)).fetchone()
        if row is None and name_key:
            row = conn.execute(
                "SELECT * FROM fork_album_folders WHERE name_key = ? ORDER BY updated_at DESC LIMIT 1",
                (name_key,)).fetchone()
    return dict(row) if row else None


def save_album_folder(source: str, album_id: str, folder: str, *, name_key: str = "",
                      album_name: str = "", artist_name: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fork_album_folders "
            "(source, album_id, name_key, folder, album_name, artist_name, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, album_id, name_key, folder, album_name, artist_name, time.time()),
        )


def delete_album_folder(source: str, album_id: str, name_key: str = "") -> int:
    """Forget the folder for this album: its own row and any row found by name."""
    with connect() as conn:
        removed = conn.execute("DELETE FROM fork_album_folders WHERE source = ? AND album_id = ?",
                               (source, album_id)).rowcount
        if name_key:
            removed += conn.execute("DELETE FROM fork_album_folders WHERE name_key = ?", (name_key,)).rowcount
    return removed


def move_album_folders(old: str, new: str) -> int:
    with connect() as conn:
        return conn.execute("UPDATE fork_album_folders SET folder = ?, updated_at = ? WHERE folder = ?",
                            (new, time.time(), old)).rowcount


def move_album_folders_under(old: str, new: str) -> int:
    """A folder was renamed: saved folders at or below it follow."""
    prefix = old.rstrip(os.sep) + os.sep
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with connect() as conn:
        moved = conn.execute("UPDATE fork_album_folders SET folder = ?, updated_at = ? WHERE folder = ?",
                             (new, time.time(), old)).rowcount
        moved += conn.execute(
            "UPDATE fork_album_folders SET folder = ? || substr(folder, ?), updated_at = ? "
            "WHERE folder LIKE ? ESCAPE '\\'",
            (new.rstrip(os.sep) + os.sep, len(prefix) + 1, time.time(), like)).rowcount
    return moved


# ── album checks ────────────────────────────────────────────────────────
# The last library analysis of an album (how many of its tracks are owned),
# with a fingerprint of what it was computed from so it is only reused while
# that is unchanged.

def get_album_check(source: str, album_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM fork_album_checks WHERE source = ? AND album_id = ?",
                           (source, album_id)).fetchone()
    return dict(row) if row else None


def save_album_check(source: str, album_id: str, found: int, total: int, fingerprint: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fork_album_checks (source, album_id, found, total, fingerprint, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, album_id, int(found), int(total), fingerprint, time.time()),
        )


def clear_album_checks() -> int:
    with connect() as conn:
        return conn.execute("DELETE FROM fork_album_checks").rowcount


def library_fingerprint() -> str:
    """Changes whenever tracks are added to or removed from the library."""
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM tracks").fetchone()
    return f"{row[0]}:{row[1]}"
