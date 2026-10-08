"""Backend of the Tag Editor page: read and write tags and cover art by hand,
rename files and folders (lyrics follow their track), rename in bulk.

Everything works on paths inside the library folders only
(:func:`album_tagging.allowed_roots`). Writes touch exactly the tags the user
changed; nothing is translated, normalised or looked up.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from utils.logging_config import get_logger

logger = get_logger("fork.editor")

# field -> (label, ID3 frame, Vorbis key, MP4 atom)
FIELDS: List[Tuple[str, str, str, str, str]] = [
    ("title", "Title", "TIT2", "title", "\xa9nam"),
    ("artist", "Artist", "TPE1", "artist", "\xa9ART"),
    ("albumartist", "Album artist", "TPE2", "albumartist", "aART"),
    ("album", "Album", "TALB", "album", "\xa9alb"),
    ("date", "Date", "TDRC", "date", "\xa9day"),
    ("tracknumber", "Track", "TRCK", "tracknumber", "trkn"),
    ("discnumber", "Disc", "TPOS", "discnumber", "disk"),
    ("genre", "Genre", "TCON", "genre", "\xa9gen"),
    ("composer", "Composer", "TCOM", "composer", "\xa9wrt"),
    ("comment", "Comment", "COMM", "comment", "\xa9cmt"),
]
_BY_NAME = {f[0]: f for f in FIELDS}
# several values in one tag are shown and typed as "A; B"
_MULTI = {"artist", "albumartist", "genre", "composer"}
_TOTALS = {"tracknumber": "tracktotal", "discnumber": "disctotal"}
_TOTAL_ALIASES = {"tracktotal": "totaltracks", "disctotal": "totaldiscs"}
SIDECARS = (".lrc", ".txt", ".original.lrc", ".original.txt")
_BAD_NAME = re.compile(r'[/\\\x00]')
MAX_COVER_BYTES = 20 * 1024 * 1024


# ── paths ───────────────────────────────────────────────────────────────

def safe_path(path: Any, roots: Optional[List[str]] = None) -> str:
    """A real path inside the library folders (or inside ``roots`` when
    given, for callers that work somewhere else), or an error."""
    from core.fork import album_tagging

    text = str(path or "").strip()
    if not text:
        raise ValueError("No path given")
    real = os.path.realpath(text)
    if roots is not None:
        if not any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots if r):
            raise PermissionError("That path is outside the allowed folders")
    elif not album_tagging.root_of(real):
        raise PermissionError("That path is outside the library folders")
    if not os.path.exists(real):
        raise FileNotFoundError(f"Not found: {text}")
    return real


def roots() -> List[str]:
    from core.fork import album_tagging

    return album_tagging.allowed_roots()


def _is_root(path: str) -> bool:
    return os.path.normpath(path) in {os.path.normpath(r) for r in roots()}


# ── tags ────────────────────────────────────────────────────────────────

def _open(path: str) -> Tuple[Any, str]:
    from mutagen import File as MutagenFile

    from core.fork import tags

    audio = MutagenFile(path)
    if audio is None:
        raise ValueError("Not an audio file the editor can read")
    return audio, tags._kind(audio)


def _joined(values: Any) -> str:
    if values is None:
        return ""
    if not isinstance(values, (list, tuple)):
        values = [values]
    return "; ".join(str(v).strip() for v in values if str(v).strip())


def _read_field(audio: Any, kind: str, field: str) -> str:
    _name, _label, frame, key, atom = _BY_NAME[field]
    if audio.tags is None:
        return ""
    if kind == "id3":
        if frame == "COMM":
            frames = audio.tags.getall("COMM")
            return _joined(frames[0].text) if frames else ""
        found = audio.tags.get(frame)
        return _joined([str(t) for t in found.text]) if found is not None else ""
    if kind == "vorbis":
        value = _joined(audio.get(key))
        total = _joined(audio.get(_TOTALS[field]) or audio.get(_TOTAL_ALIASES[_TOTALS[field]])) \
            if field in _TOTALS else ""
        return f"{value}/{total}" if value and total and "/" not in value else value
    if kind == "mp4":
        value = audio.tags.get(atom)
        if not value:
            return ""
        if atom in ("trkn", "disk"):
            number, total = value[0]
            return f"{number}/{total}" if total else (str(number) if number else "")
        return _joined(value)
    return ""


def _split(field: str, value: str) -> List[str]:
    if field in _MULTI:
        return [part.strip() for part in value.split(";") if part.strip()]
    return [value.strip()] if value.strip() else []


def _pair(value: str) -> Tuple[int, int]:
    number, _sep, total = value.partition("/")
    try:
        return int(number.strip() or 0), int(total.strip() or 0)
    except ValueError:
        raise ValueError(f'"{value}" is not a number (use 3 or 3/12)') from None


def _write_field(audio: Any, kind: str, field: str, value: str) -> None:
    from mutagen import id3

    _name, _label, frame, key, atom = _BY_NAME[field]
    values = _split(field, value)
    if field in _TOTALS and values:
        _pair(values[0])  # validates
    if kind == "id3":
        if frame == "COMM":
            shown = audio.tags.getall("COMM")[:1]
            for comment in shown:                    # other comments are tags of their own
                audio.tags.delall(comment.HashKey)
            if values:
                audio.tags.add(id3.COMM(encoding=3, lang="eng", desc="", text=values))
            return
        audio.tags.delall(frame)
        if values:
            audio.tags.add(getattr(id3, frame)(encoding=3, text=values))
    elif kind == "vorbis":
        totals = (_TOTALS[field], _TOTAL_ALIASES[_TOTALS[field]]) if field in _TOTALS else ()
        for stale in (key, *totals):
            if stale in audio:
                del audio[stale]
        if not values:
            return
        if field in _TOTALS:
            number, total = _pair(values[0])
            audio[key] = [str(number)]
            if total:
                audio[_TOTALS[field]] = [str(total)]
        else:
            audio[key] = values
    elif kind == "mp4":
        if not values:
            audio.tags.pop(atom, None)
        elif atom in ("trkn", "disk"):
            audio.tags[atom] = [_pair(values[0])]
        else:
            audio.tags[atom] = values if field in _MULTI else [values[0]]
    else:
        raise ValueError("This file format cannot be tagged here")


def _pictures(audio: Any, kind: str) -> List[Tuple[bytes, str]]:
    out: List[Tuple[bytes, str]] = []
    try:
        if kind == "id3" and audio.tags is not None:
            out = [(f.data, f.mime or "image/jpeg") for f in audio.tags.getall("APIC")]
        elif kind == "mp4" and audio.tags is not None:
            for cover in audio.tags.get("covr") or []:
                out.append((bytes(cover), "image/png" if getattr(cover, "imageformat", 13) == 14 else "image/jpeg"))
        elif kind == "vorbis":
            if hasattr(audio, "pictures"):
                out = [(p.data, p.mime or "image/jpeg") for p in audio.pictures]
            else:
                from mutagen.flac import Picture

                for raw in audio.get("metadata_block_picture") or []:
                    picture = Picture(base64.b64decode(raw))
                    out.append((picture.data, picture.mime or "image/jpeg"))
    except Exception as exc:
        logger.debug("cover not read: %s", exc)
    return out


def read_tags(path: str) -> Dict[str, Any]:
    """``{"tags": {name: value}, "readonly": [...], "cover": {...} | None,
    "format": ...}``. ``tags`` holds the fixed fields (lower-case names) and
    then every other tag of the file (upper-case names, see
    :mod:`core.fork.all_tags`); ``readonly`` names the ones that are not text."""
    from core.fork import all_tags

    audio, kind = _open(path)
    pictures = _pictures(audio, kind)
    cover = None
    if pictures:
        data, mime = pictures[0]
        cover = {"hash": hashlib.sha1(data).hexdigest()[:16], "size": len(data), "mime": mime}
    info = getattr(audio, "info", None)
    found = {name: _read_field(audio, kind, name) for name, *_rest in FIELDS}
    extra, readonly = all_tags.read(audio, kind)     # everything else the file carries
    found.update(extra)
    return {"tags": found, "readonly": readonly, "cover": cover,
            "format": kind, "length": round(float(getattr(info, "length", 0) or 0), 1),
            "bitrate": int(getattr(info, "bitrate", 0) or 0)}


def read_basic(path: str) -> Dict[str, str]:
    """Just what the file list shows; no cover is touched."""
    audio, kind = _open(path)
    return {"title": _read_field(audio, kind, "title"), "artist": _read_field(audio, kind, "artist"),
            "album": _read_field(audio, kind, "album"), "track": _read_field(audio, kind, "tracknumber")}


def cover_of(path: str) -> Optional[Tuple[bytes, str]]:
    audio, kind = _open(path)
    pictures = _pictures(audio, kind)
    return pictures[0] if pictures else None


def _remove_cover(audio: Any, kind: str) -> None:
    if kind == "id3" and audio.tags is not None:
        audio.tags.delall("APIC")
    elif kind == "mp4" and audio.tags is not None:
        audio.tags.pop("covr", None)
    elif kind == "vorbis":
        if hasattr(audio, "clear_pictures"):
            audio.clear_pictures()
        elif "metadata_block_picture" in audio:
            del audio["metadata_block_picture"]


def _set_cover(audio: Any, kind: str, data: bytes, mime: str) -> None:
    from mutagen import id3
    from mutagen.flac import Picture

    _remove_cover(audio, kind)
    if kind == "id3":
        audio.tags.add(id3.APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    elif kind == "mp4":
        from mutagen.mp4 import MP4Cover

        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
    elif kind == "vorbis":
        picture = Picture()
        picture.type, picture.mime, picture.desc, picture.data = 3, mime, "Cover", data
        if hasattr(audio, "add_picture"):
            audio.add_picture(picture)
        else:
            audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    else:
        raise ValueError("This file format cannot hold a cover")


def _image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    raise ValueError("The cover must be a JPEG or PNG image")


def save_tags(paths: List[Any], changes: Dict[str, Any], cover: Optional[Dict[str, Any]] = None,
              db: Any = None, roots: Optional[List[str]] = None) -> Dict[str, Any]:
    """Write ``changes`` (name -> new value; "" removes the tag) to every
    file of ``paths``, and set or remove the cover when asked. Tags not in
    ``changes`` are not touched. A name that is not a fixed field is any
    other tag of the file, by its upper-case name."""
    from core.fork import all_tags, library_index, tags

    changes = {(k if k in _BY_NAME else str(k).strip().upper()): str(v if v is not None else "")
               for k, v in (changes or {}).items() if str(k).strip()}
    action = str((cover or {}).get("action") or "keep")
    image: Optional[Tuple[bytes, str]] = None
    if action == "set":
        try:
            data = base64.b64decode(str((cover or {}).get("data") or ""), validate=True)
        except Exception:
            raise ValueError("The cover could not be read") from None
        if not data or len(data) > MAX_COVER_BYTES:
            raise ValueError("The cover is empty or larger than 20 MB")
        image = (data, _image_mime(data))
    if not changes and action == "keep":
        return {"saved": 0, "errors": []}
    for field in _TOTALS:
        if changes.get(field):
            _pair(changes[field])
    saved, errors, done = 0, [], []
    for raw in paths:
        try:
            path = safe_path(raw, roots)
            audio, kind = _open(path)
            if audio.tags is None:
                audio.add_tags()
            for field, value in changes.items():
                if field in _BY_NAME:
                    _write_field(audio, kind, field, value)
                else:
                    all_tags.write(audio, kind, field, value)
            if image:
                _set_cover(audio, kind, *image)
            elif action == "remove":
                _remove_cover(audio, kind)
            tags._save(audio)
            saved += 1
            done.append(path)
        except Exception as exc:
            errors.append(f"{os.path.basename(str(raw))}: {exc}")
    if image and (cover or {}).get("folder_file"):
        for folder in {os.path.dirname(p) for p in done}:
            try:
                for stale in ("cover.jpg", "cover.png"):
                    if os.path.exists(os.path.join(folder, stale)):
                        os.remove(os.path.join(folder, stale))
                name = "cover.png" if image[1] == "image/png" else "cover.jpg"
                with open(os.path.join(folder, name), "wb") as fh:
                    fh.write(image[0])
            except OSError as exc:
                errors.append(f"cover file in {os.path.basename(folder)}: {exc}")
    if roots is None:
        library_index.touched(done)
    if db is not None and done:
        _sync_library(db, done, changes)
    return {"saved": saved, "errors": errors}


def _sync_library(db: Any, paths: List[str], changes: Dict[str, str]) -> None:
    """Carry the plain per-track values into the library database. Album and
    artist changes move a track between records; that is left to a library
    scan."""
    sets: Dict[str, Any] = {}
    if "title" in changes and changes["title"].strip():
        sets["title"] = changes["title"].strip()
    for field, column in (("tracknumber", "track_number"), ("discnumber", "disc_number")):
        if changes.get(field):
            sets[column] = _pair(changes[field])[0]
    if not sets:
        return
    try:
        conn = db._get_connection()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
            sets = {k: v for k, v in sets.items() if k in columns}
            if not sets:
                return
            clause = ", ".join(f"{k} = ?" for k in sets)
            for path in paths:
                conn.execute(f"UPDATE tracks SET {clause} WHERE file_path = ?", (*sets.values(), path))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("library rows not updated: %s", exc)


# ── renaming ────────────────────────────────────────────────────────────

def _check_name(name: Any) -> str:
    text = str(name or "").strip()
    if not text or text in (".", "..") or _BAD_NAME.search(text):
        raise ValueError("That is not a usable name")
    if len(text.encode("utf-8")) > 250:
        raise ValueError("That name is too long")
    return text


def _stem(name: str) -> str:
    """A track's name without its extension; a sidecar's without its
    (possibly double) extension."""
    lowered = name.lower()
    for suffix in sorted(SIDECARS, key=len, reverse=True):
        if lowered.endswith(suffix):
            return name[:-len(suffix)]
    return os.path.splitext(name)[0]


def _repoint_library(db: Any, old: str, new: str, is_dir: bool) -> None:
    from core.fork import store

    if db is not None:
        try:
            conn = db._get_connection()
            try:
                if is_dir:
                    prefix = old.rstrip(os.sep) + os.sep
                    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                    conn.execute(
                        "UPDATE tracks SET file_path = ? || substr(file_path, ?) WHERE file_path LIKE ? ESCAPE '\\'",
                        (new.rstrip(os.sep) + os.sep, len(prefix) + 1, like))
                else:
                    conn.execute("UPDATE tracks SET file_path = ? WHERE file_path = ?", (new, old))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("library paths not updated for %s: %s", new, exc)
    if is_dir:
        try:
            store.move_album_folders_under(old, new)
        except Exception as exc:
            logger.debug("saved album folders not moved: %s", exc)


def rename(path: Any, name: Any, db: Any = None) -> Dict[str, Any]:
    """Rename one file or folder in place. A track's lyric files (same name,
    ``.lrc`` / ``.txt`` / ``.original.*``) are renamed with it."""
    from core.fork import library_index

    old = safe_path(path)
    if _is_root(old):
        raise PermissionError("A library folder itself cannot be renamed here")
    new_name = _check_name(name)
    new = os.path.join(os.path.dirname(old), new_name)
    if os.path.normpath(new) == os.path.normpath(old):
        return {"path": old, "name": new_name, "sidecars": []}
    is_dir = os.path.isdir(old)
    # a rename that only changes letter case is fine on a case-insensitive disk
    if os.path.exists(new) and not (new.lower() == old.lower() and os.path.samefile(old, new)):
        raise FileExistsError(f'"{new_name}" already exists here')
    sidecars: List[Tuple[str, str]] = []
    if not is_dir and library_index.kind_of(os.path.splitext(old)[1]) == "audio":
        old_stem, new_stem = os.path.splitext(old)[0], os.path.splitext(new)[0]
        for suffix in SIDECARS:
            if os.path.isfile(old_stem + suffix):
                if os.path.exists(new_stem + suffix) and old_stem.lower() != new_stem.lower():
                    raise FileExistsError(f'"{os.path.basename(new_stem + suffix)}" already exists here')
                sidecars.append((old_stem + suffix, new_stem + suffix))
    os.rename(old, new)
    moved = []
    for src, dst in sidecars:
        try:
            os.rename(src, dst)
            moved.append(os.path.basename(dst))
            library_index.moved(src, dst, False)
        except OSError as exc:
            logger.warning("lyrics file %s not renamed: %s", src, exc)
    _repoint_library(db, old, new, is_dir)
    library_index.moved(old, new, is_dir)
    return {"path": new, "name": new_name, "sidecars": moved}


def move(paths: List[Any], destination: Any, db: Any = None) -> Dict[str, Any]:
    """Move files and folders into the folder ``destination``. A track's
    lyrics files go with it; library paths and saved album folders follow.
    Nothing is ever overwritten. ``{"moved": [new paths], "errors": [...]}``."""
    import shutil

    from core.fork import library_index

    target_dir = safe_path(destination)
    if not os.path.isdir(target_dir):
        raise ValueError("The destination is not a folder")
    moved: List[str] = []
    errors: List[str] = []
    for raw in paths:
        name = os.path.basename(str(raw).rstrip(os.sep))
        try:
            old = safe_path(raw)
            if _is_root(old):
                raise PermissionError("a library folder itself cannot be moved")
            if os.path.normpath(os.path.dirname(old)) == os.path.normpath(target_dir):
                continue                                    # already there
            is_dir = os.path.isdir(old)
            if is_dir and (target_dir + os.sep).startswith(old.rstrip(os.sep) + os.sep):
                raise ValueError("a folder cannot be moved into itself")
            new = os.path.join(target_dir, os.path.basename(old))
            if os.path.exists(new):
                raise FileExistsError("something with that name is already there")
            sidecars: List[Tuple[str, str]] = []
            if not is_dir and library_index.kind_of(os.path.splitext(old)[1]) == "audio":
                old_stem, new_stem = os.path.splitext(old)[0], os.path.splitext(new)[0]
                for suffix in SIDECARS:
                    if os.path.isfile(old_stem + suffix) and not os.path.exists(new_stem + suffix):
                        sidecars.append((old_stem + suffix, new_stem + suffix))
            shutil.move(old, new)
            for src, dst in sidecars:
                try:
                    shutil.move(src, dst)
                    library_index.moved(src, dst, False)
                except OSError as exc:
                    logger.warning("lyrics file %s not moved: %s", src, exc)
            _repoint_library(db, old, new, is_dir)
            library_index.moved(old, new, is_dir)
            moved.append(new)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return {"moved": moved, "errors": errors, "destination": target_dir}


def _free_name(folder: str, name: str, is_dir: bool) -> str:
    """``name``, or "name (copy)", "name (copy 2)"… when it is taken in ``folder``."""
    if not os.path.exists(os.path.join(folder, name)):
        return name
    stem, ext = (name, "") if is_dir else (_stem(name), name[len(_stem(name)):])
    for n in range(1, 1000):
        candidate = f"{stem} (copy{'' if n == 1 else f' {n}'}){ext}"
        if not os.path.exists(os.path.join(folder, candidate)):
            return candidate
    raise FileExistsError("too many copies with that name")


def copy(paths: List[Any], destination: Any) -> Dict[str, Any]:
    """Copy files and folders into the folder ``destination``. A name that is
    taken there gets "(copy)" added, so pasting next to the original works. A
    track's lyrics files are copied with it. The copies are plain files: the
    library learns about them on its next scan."""
    import shutil

    from core.fork import library_index

    target_dir = safe_path(destination)
    if not os.path.isdir(target_dir):
        raise ValueError("The destination is not a folder")
    copied: List[str] = []
    errors: List[str] = []
    for raw in paths:
        name = os.path.basename(str(raw).rstrip(os.sep))
        try:
            old = safe_path(raw)
            is_dir = os.path.isdir(old)
            if is_dir and (target_dir + os.sep).startswith(old.rstrip(os.sep) + os.sep):
                raise ValueError("a folder cannot be copied into itself")
            new = os.path.join(target_dir, _free_name(target_dir, os.path.basename(old), is_dir))
            if is_dir:
                shutil.copytree(old, new)
            else:
                shutil.copy2(old, new)
                if library_index.kind_of(os.path.splitext(old)[1]) == "audio":
                    old_stem, new_stem = os.path.splitext(old)[0], os.path.splitext(new)[0]
                    for suffix in SIDECARS:
                        if os.path.isfile(old_stem + suffix) and not os.path.exists(new_stem + suffix):
                            shutil.copy2(old_stem + suffix, new_stem + suffix)
            copied.append(new)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return {"copied": copied, "errors": errors, "destination": target_dir}


def describe(paths: List[Any]) -> Dict[str, Any]:
    """What deleting ``paths`` would remove: ``{"files", "folders", "bytes"}``."""
    files = folders = size = 0
    for raw in paths:
        try:
            path = safe_path(raw)
        except (ValueError, PermissionError, FileNotFoundError):
            continue
        if os.path.isdir(path):
            folders += 1
            for current, dirs, names in os.walk(path):
                folders += len(dirs)
                for name in names:
                    files += 1
                    try:
                        size += os.path.getsize(os.path.join(current, name))
                    except OSError:
                        pass
        else:
            files += 1
            try:
                size += os.path.getsize(path)
            except OSError:
                pass
    return {"files": files, "folders": folders, "bytes": size}


def _forget_library(db: Any, path: str, is_dir: bool) -> None:
    if db is None:
        return
    try:
        conn = db._get_connection()
        try:
            if is_dir:
                prefix = path.rstrip(os.sep) + os.sep
                like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                conn.execute("DELETE FROM tracks WHERE file_path LIKE ? ESCAPE '\\'", (like,))
            else:
                conn.execute("DELETE FROM tracks WHERE file_path = ?", (path,))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("library rows not removed for %s: %s", path, exc)


def delete(paths: List[Any], db: Any = None) -> Dict[str, Any]:
    """Delete files and folders for good. A track's lyrics files go with it,
    and the library forgets the tracks. A library folder itself is refused."""
    import shutil

    from core.fork import library_index

    deleted: List[str] = []
    errors: List[str] = []
    parents: set = set()
    # a folder first: its contents need no deleting of their own afterwards
    for raw in sorted({str(p) for p in paths}, key=len):
        name = os.path.basename(raw.rstrip(os.sep))
        try:
            if not os.path.lexists(raw) and any(raw.startswith(d.rstrip(os.sep) + os.sep) for d in deleted):
                continue
            path = safe_path(raw)
            if _is_root(path):
                raise PermissionError("a library folder itself cannot be deleted")
            is_dir = os.path.isdir(path)
            if is_dir:
                shutil.rmtree(path)
            else:
                os.remove(path)
                if library_index.kind_of(os.path.splitext(path)[1]) == "audio":
                    stem = os.path.splitext(path)[0]
                    for suffix in SIDECARS:
                        if os.path.isfile(stem + suffix):
                            os.remove(stem + suffix)
                            library_index.moved(stem + suffix, "", False)
            _forget_library(db, path, is_dir)
            if is_dir:
                library_index.forget(path)
                parents.add(os.path.dirname(path))
            else:
                library_index.moved(path, "", False)
            deleted.append(path)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    for parent in parents:                           # once each, however many folders went
        try:
            library_index.scan_dir(parent)
        except FileNotFoundError:
            pass
    return {"deleted": deleted, "errors": errors}


def _replacer(find: str, replace: str, regex: bool, case_sensitive: bool):
    if not find:
        raise ValueError("Nothing to look for")
    flags = 0 if case_sensitive else re.IGNORECASE
    if regex:
        try:
            pattern = re.compile(find, flags)
        except re.error as exc:
            raise ValueError(f"The pattern is not valid: {exc}") from None
        # $1 as most tools write a group; Python wants \g<1>
        template = re.sub(r"\$(\d+)", r"\\g<\1>", replace)
        try:
            pattern.sub(template, "")
        except (re.error, IndexError) as exc:
            raise ValueError(f"The replacement is not valid: {exc}") from None
        return lambda text: pattern.sub(template, text)
    pattern = re.compile(re.escape(find), flags)
    return lambda text: pattern.sub(lambda _m: replace, text)


def bulk_rename(paths: List[Any], find: Any, replace: Any, regex: bool = False, case_sensitive: bool = False,
                apply: bool = False, db: Any = None) -> Dict[str, Any]:
    """Replace ``find`` with ``replace`` in the names of ``paths`` (a file's
    extension is never part of the match). Without ``apply`` this only
    previews: ``{"items": [{"path", "old", "new", "problem"}]}``."""
    swap = _replacer(str(find or ""), str(replace or ""), bool(regex), bool(case_sensitive))
    items: List[Dict[str, Any]] = []
    targets: Dict[str, str] = {}
    for raw in paths:
        try:
            path = safe_path(raw)
        except (ValueError, PermissionError, FileNotFoundError) as exc:
            items.append({"path": str(raw), "old": os.path.basename(str(raw)), "new": "", "problem": str(exc)})
            continue
        old = os.path.basename(path)
        if os.path.isdir(path):
            new = swap(old)
        else:
            stem = _stem(old)
            new = swap(stem) + old[len(stem):]
        item = {"path": path, "old": old, "new": new, "problem": ""}
        if new != old:
            target = os.path.join(os.path.dirname(path), new)
            try:
                _check_name(new)
                if _is_root(path):
                    raise PermissionError("A library folder itself cannot be renamed here")
                if target.lower() in targets or (os.path.exists(target) and target.lower() != path.lower()):
                    raise FileExistsError("Another item would get the same name")
            except (ValueError, PermissionError, FileExistsError) as exc:
                item["problem"] = str(exc)
            targets[target.lower()] = path
        items.append(item)
    changing = [i for i in items if i["new"] != i["old"] and not i["problem"]]
    out: Dict[str, Any] = {"items": items, "changes": len(changing),
                           "problems": sum(1 for i in items if i["problem"]), "renamed": 0}
    if not apply:
        return out
    # deepest first, so renaming a folder never invalidates a path still to do
    for item in sorted(changing, key=lambda i: -i["path"].count(os.sep)):
        target = os.path.join(os.path.dirname(item["path"]), item["new"])
        if not os.path.exists(item["path"]) and os.path.exists(target):
            item["path"] = target       # a lyrics file that already followed its track
            out["renamed"] += 1
            continue
        try:
            done = rename(item["path"], item["new"], db=db)
            item["path"] = done["path"]
            out["renamed"] += 1
        except Exception as exc:
            item["problem"] = str(exc)
            out["problems"] += 1
    return out


def field_list() -> List[Dict[str, str]]:
    return [{"name": name, "label": label} for name, label, *_rest in FIELDS]


__all__ = ["FIELDS", "bulk_rename", "copy", "cover_of", "delete", "describe", "field_list", "move", "read_basic", "read_tags", "rename",
           "roots", "safe_path", "save_tags"]
