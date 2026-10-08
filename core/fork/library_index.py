"""A cached index of the music library's folders and files.

The Tag Editor page shows the library as a tree and searches it by name.
Walking a large library for every click would hammer the disk, so the
structure lives in two tables of the music database:

* ``fork_fs_dirs``  one row per folder (its parent, name and mtime),
* ``fork_fs_files`` one row per file (size, mtime and, once read, its basic
  tags).

How it stays cheap **and** current:

* the tree and the search read the tables only;
* opening a folder lists that ONE folder from disk (a single ``scandir``) and
  brings its rows up to date, so what is on screen is always real;
* a folder's rows are also refreshed when its mtime no longer matches (one
  ``stat``), which is how a tree node notices new or removed sub-folders;
* tags are read only for the files of an opened folder, and only again when a
  file's size or mtime changed;
* a full walk runs in the background the first time and on request, paced so
  it never saturates the disk.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from core.fork import store
from utils.logging_config import get_logger

logger = get_logger("fork.library_index")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fork_fs_dirs (
    path TEXT PRIMARY KEY,
    parent TEXT NOT NULL,
    name TEXT NOT NULL,
    name_fold TEXT NOT NULL,
    mtime REAL,
    subdirs INTEGER,
    files INTEGER,
    scanned_at REAL
);
CREATE INDEX IF NOT EXISTS idx_fork_fs_dirs_parent ON fork_fs_dirs (parent);
CREATE TABLE IF NOT EXISTS fork_fs_files (
    path TEXT PRIMARY KEY,
    dir TEXT NOT NULL,
    name TEXT NOT NULL,
    name_fold TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    title TEXT, artist TEXT, album TEXT, track TEXT,
    tags_fold TEXT NOT NULL DEFAULT '',
    tags_mtime REAL
);
CREATE INDEX IF NOT EXISTS idx_fork_fs_files_dir ON fork_fs_files (dir);
CREATE TABLE IF NOT EXISTS fork_fs_meta (key TEXT PRIMARY KEY, value TEXT);
"""

_ready: set = set()
_ready_lock = threading.Lock()
_JUNK = {".ds_store", "thumbs.db", "desktop.ini"}


def _connect():
    """``store.connect`` with this module's tables in place."""
    ctx = store.connect()
    conn = ctx.__enter__()
    path = store.database_path()
    if path not in _ready:
        with _ready_lock:
            conn.executescript(_SCHEMA)
            _ready.add(path)
    return ctx, conn


class _Db:
    def __enter__(self):
        self.ctx, self.conn = _connect()
        return self.conn

    def __exit__(self, *exc):
        return self.ctx.__exit__(*exc)


def _fold(text: Any) -> str:
    from core.fork import album_tagging

    return album_tagging._fold(str(text or ""))


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _audio_exts() -> set:
    from core.fork import album_tagging

    return album_tagging._audio_exts()


def kind_of(ext: str) -> str:
    ext = ext.lower()
    if ext in _audio_exts():
        return "audio"
    if ext in (".lrc", ".txt"):
        return "lyrics"
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return "image"
    return "other"


# ── one folder ──────────────────────────────────────────────────────────

def _basic_tags(path: str) -> Dict[str, str]:
    from core.fork import editor

    try:
        tags = editor.read_tags(path)["tags"]
    except Exception as exc:
        logger.debug("tags not read for %s: %s", path, exc)
        return {"title": "", "artist": "", "album": "", "track": ""}
    return {"title": tags.get("title", ""), "artist": tags.get("artist", ""), "album": tags.get("album", ""),
            "track": tags.get("tracknumber", "")}


def scan_dir(path: str, read_tags: bool = False) -> Dict[str, Any]:
    """Bring the rows of ``path`` (not its sub-folders' contents) in line with
    the disk. ``read_tags`` also reads the basic tags of audio files that are
    new or changed. Returns ``{"dirs": [...], "files": [...]}`` as stored."""
    dirs: List[Dict[str, Any]] = []
    files: List[Dict[str, Any]] = []
    try:
        mtime = os.stat(path).st_mtime
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith(".") or entry.name.lower() in _JUNK:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append({"path": entry.path, "name": entry.name})
                    elif entry.is_file(follow_symlinks=False):
                        info = entry.stat()
                        files.append({"path": entry.path, "name": entry.name, "size": info.st_size,
                                      "mtime": info.st_mtime,
                                      "ext": os.path.splitext(entry.name)[1].lower()})
                except OSError:
                    continue
    except OSError as exc:
        forget(path)
        raise FileNotFoundError(str(exc)) from exc

    audio = _audio_exts()
    with _Db() as conn:
        known = {row["path"]: dict(row) for row in conn.execute("SELECT * FROM fork_fs_files WHERE dir = ?", (path,))}
        for file in files:
            old = known.pop(file["path"], None)
            fresh = old is not None and old["size"] == file["size"] and old["mtime"] == file["mtime"]
            tags = {k: (old or {}).get(k) or "" for k in ("title", "artist", "album", "track")} if fresh else \
                {"title": "", "artist": "", "album": "", "track": ""}
            tags_mtime = old["tags_mtime"] if fresh else None
            if read_tags and file["ext"] in audio and tags_mtime != file["mtime"]:
                tags, tags_mtime = _basic_tags(file["path"]), file["mtime"]
            file.update(tags, tags_mtime=tags_mtime)
            conn.execute(
                "INSERT OR REPLACE INTO fork_fs_files (path, dir, name, name_fold, ext, size, mtime, title, artist,"
                " album, track, tags_fold, tags_mtime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file["path"], path, file["name"], _fold(file["name"]), file["ext"], file["size"], file["mtime"],
                 tags["title"], tags["artist"], tags["album"], tags["track"],
                 _fold(" ".join((tags["title"], tags["artist"], tags["album"]))), tags_mtime))
        for gone in known:
            conn.execute("DELETE FROM fork_fs_files WHERE path = ?", (gone,))

        known_dirs = {row["path"]: dict(row) for row in
                      conn.execute("SELECT * FROM fork_fs_dirs WHERE parent = ?", (path,))}
        for child in dirs:
            old = known_dirs.pop(child["path"], None)
            child["subdirs"] = old["subdirs"] if old else None
            child["files"] = old["files"] if old else None
            if old is None:
                conn.execute(
                    "INSERT OR REPLACE INTO fork_fs_dirs (path, parent, name, name_fold) VALUES (?, ?, ?, ?)",
                    (child["path"], path, child["name"], _fold(child["name"])))
        for gone in known_dirs:
            _forget(conn, gone)
        row = conn.execute("SELECT parent, name FROM fork_fs_dirs WHERE path = ?", (path,)).fetchone()
        parent = row["parent"] if row else ""
        name = row["name"] if row else os.path.basename(path.rstrip(os.sep)) or path
        conn.execute(
            "INSERT OR REPLACE INTO fork_fs_dirs (path, parent, name, name_fold, mtime, subdirs, files, scanned_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (path, parent, name, _fold(name), mtime, len(dirs), len(files), time.time()))
    dirs.sort(key=lambda d: d["name"].casefold())
    files.sort(key=lambda f: f["name"].casefold())
    return {"dirs": dirs, "files": files}


def _forget(conn: Any, path: str) -> None:
    prefix = _like(path.rstrip(os.sep) + os.sep) + "%"
    conn.execute("DELETE FROM fork_fs_dirs WHERE path = ? OR path LIKE ? ESCAPE '\\'", (path, prefix))
    conn.execute("DELETE FROM fork_fs_files WHERE dir = ? OR dir LIKE ? ESCAPE '\\'", (path, prefix))


def forget(path: str) -> None:
    """Drop a folder and everything under it from the index."""
    with _Db() as conn:
        _forget(conn, path)


def children(path: str) -> List[Dict[str, Any]]:
    """Sub-folders of ``path`` for the tree, from the index. The disk is only
    touched (one ``stat``, then one listing) when the folder changed."""
    with _Db() as conn:
        row = conn.execute("SELECT mtime, scanned_at FROM fork_fs_dirs WHERE path = ?", (path,)).fetchone()
    try:
        stale = row is None or row["scanned_at"] is None or row["mtime"] != os.stat(path).st_mtime
    except OSError:
        forget(path)
        raise FileNotFoundError(path) from None
    if stale:
        scan_dir(path)
    with _Db() as conn:
        rows = conn.execute("SELECT path, name, subdirs FROM fork_fs_dirs WHERE parent = ?", (path,)).fetchall()
    out = [{"path": r["path"], "name": r["name"], "has_children": r["subdirs"] is None or r["subdirs"] > 0}
           for r in rows]
    out.sort(key=lambda d: d["name"].casefold())
    return out


def listing(path: str) -> Dict[str, Any]:
    """What a file browser shows for ``path``: listed from disk (one
    ``scandir``), with tags from the index where the file has not changed."""
    result = scan_dir(path, read_tags=True)
    return {"dirs": [{"path": d["path"], "name": d["name"], "kind": "dir"} for d in result["dirs"]],
            "files": [_file_item(f) for f in result["files"]]}


def _file_item(row: Any) -> Dict[str, Any]:
    row = dict(row)
    return {"path": row["path"], "name": row["name"], "kind": kind_of(row["ext"]), "size": row["size"],
            "mtime": row["mtime"], "title": row.get("title") or "", "artist": row.get("artist") or "",
            "album": row.get("album") or "", "track": row.get("track") or ""}


# ── search ──────────────────────────────────────────────────────────────

def search(query: str, base: Optional[str] = None, limit: int = 400) -> Dict[str, Any]:
    """Folders and files whose name (or cached title / artist / album)
    contains every word of ``query``; under ``base`` when given. Index only."""
    terms = [t for t in _fold(query).split() if t]
    if not terms:
        return {"dirs": [], "files": [], "truncated": False}
    params: List[Any] = []
    under_dir = under_file = ""
    if base:
        prefix = _like(base.rstrip(os.sep) + os.sep) + "%"
        under_dir, under_file = " AND path LIKE ? ESCAPE '\\'", " AND (dir = ? OR dir LIKE ? ESCAPE '\\')"
    dir_sql = " AND ".join("name_fold LIKE ? ESCAPE '\\'" for _ in terms)
    file_sql = " AND ".join("(name_fold LIKE ? ESCAPE '\\' OR tags_fold LIKE ? ESCAPE '\\')" for _ in terms)
    likes = [f"%{_like(t)}%" for t in terms]
    with _Db() as conn:
        params = list(likes) + ([prefix] if base else []) + [limit + 1]
        dirs = conn.execute(f"SELECT path, name FROM fork_fs_dirs WHERE {dir_sql}{under_dir} AND parent != ''"
                            " ORDER BY name_fold LIMIT ?", params).fetchall()
        params = [v for like in likes for v in (like, like)] + ([base, prefix] if base else []) + [limit + 1]
        files = conn.execute(f"SELECT * FROM fork_fs_files WHERE {file_sql}{under_file} ORDER BY name_fold LIMIT ?",
                             params).fetchall()
    truncated = len(dirs) > limit or len(files) > limit
    return {"dirs": [{"path": d["path"], "name": d["name"], "kind": "dir"} for d in dirs[:limit]],
            "files": [_file_item(f) for f in files[:limit]], "truncated": truncated}


# ── keeping the index right after the editor changes things ─────────────

def moved(old: str, new: str, is_dir: bool) -> None:
    """A file or folder was renamed in place by the editor."""
    with _Db() as conn:
        if not is_dir:
            conn.execute("DELETE FROM fork_fs_files WHERE path = ?", (old,))
            return
        _forget(conn, old)
    for folder in {os.path.dirname(old), os.path.dirname(new)}:
        try:
            scan_dir(folder)
        except FileNotFoundError:
            pass


def touched(paths: Iterable[str]) -> None:
    """Files whose tags the editor just wrote: their cached tags are stale."""
    with _Db() as conn:
        for path in paths:
            conn.execute("UPDATE fork_fs_files SET tags_mtime = NULL WHERE path = ?", (path,))


# ── the full walk ───────────────────────────────────────────────────────

_scan: Dict[str, Any] = {"running": False, "dirs": 0, "files": 0, "started": 0.0, "finished": 0.0, "error": ""}
_scan_lock = threading.Lock()


def _meta(key: str, value: Optional[str] = None) -> Optional[str]:
    with _Db() as conn:
        if value is not None:
            conn.execute("INSERT OR REPLACE INTO fork_fs_meta (key, value) VALUES (?, ?)", (key, value))
            return value
        row = conn.execute("SELECT value FROM fork_fs_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def status() -> Dict[str, Any]:
    with _scan_lock:
        out = dict(_scan)
    try:
        out["last_full_scan"] = float(_meta("last_full_scan") or 0)
    except (TypeError, ValueError):
        out["last_full_scan"] = 0.0
    return out


def _walk(roots: List[str], pause: float) -> None:
    try:
        stack = list(roots)
        for root in roots:
            with _Db() as conn:
                conn.execute("INSERT OR IGNORE INTO fork_fs_dirs (path, parent, name, name_fold) VALUES (?, '', ?, ?)",
                             (root, root, _fold(root)))
        while stack:
            current = stack.pop()
            try:
                result = scan_dir(current)
            except FileNotFoundError:
                continue
            with _scan_lock:
                _scan["dirs"] += 1
                _scan["files"] += len(result["files"])
            stack.extend(d["path"] for d in result["dirs"])
            if pause:
                time.sleep(pause)   # leave the disk room for everything else
        _meta("last_full_scan", str(time.time()))
    except Exception as exc:
        logger.warning("library index walk failed: %s", exc)
        with _scan_lock:
            _scan["error"] = str(exc)
    finally:
        with _scan_lock:
            _scan.update(running=False, finished=time.time())


def start_scan(roots: List[str], pause: float = 0.002, background: bool = True) -> Dict[str, Any]:
    """Walk every folder of ``roots`` into the index. One walk at a time."""
    with _scan_lock:
        if _scan["running"]:
            return dict(_scan)
        _scan.update(running=True, dirs=0, files=0, started=time.time(), finished=0.0, error="")
    if background:
        threading.Thread(target=_walk, args=(list(roots), pause), name="fork-library-index", daemon=True).start()
    else:
        _walk(list(roots), 0)
    return status()


def ensure_started(roots: List[str]) -> Dict[str, Any]:
    """Start the first full walk if the library was never indexed."""
    current = status()
    if not current["running"] and not current["last_full_scan"]:
        return start_scan(roots)
    return current


__all__ = ["children", "ensure_started", "forget", "kind_of", "listing", "moved", "scan_dir", "search",
           "start_scan", "status", "touched"]
