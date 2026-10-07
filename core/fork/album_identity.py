"""Keep the tags a media server groups an album by identical across its tracks.

Navidrome identifies an album by its MusicBrainz release id when a file has
one, and by album artist + album name + release date otherwise. SoulSync looks
that id up per track, and the lookup does not succeed for every track of a
release (a title that differs from MusicBrainz's, a timeout). The result is
one album folder where some files carry ``MUSICBRAINZ_ALBUMID`` and others do
not — which the server shows as two albums with the same name.

After each file is tagged, the files already in its album folder are compared:
the id (with its release-group id) is copied to whichever side lacks it. With
two different ids present the majority wins for the new file and nothing else
is touched — that needs a human (Tools → Album Tag Consistency).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from core.fork import config
from utils.logging_config import get_logger

logger = get_logger("fork.album_identity")

_DISC_DIR_RE = re.compile(r"^(?:disc|disk|cd)\s*\d+$", re.I)
_MAX_SIBLINGS = 400
_FIELDS = ("musicbrainz_albumid", "musicbrainz_releasegroupid")


def _helpers():
    from core.repair_jobs.album_tag_consistency import _read_tag, _write_tag

    return _read_tag, _write_tag


def _fold(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def album_folder(final_path: str) -> str:
    folder = os.path.dirname(final_path)
    return os.path.dirname(folder) if _DISC_DIR_RE.match(os.path.basename(folder)) else folder


def _audio_files(folder: str) -> List[str]:
    from core.tag_writer import SUPPORTED_EXTENSIONS

    out: List[str] = []
    for current, dirs, names in os.walk(folder):
        # the album folder itself and its disc sub-folders, nothing deeper
        if current != folder:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if _DISC_DIR_RE.match(d)]
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() in SUPPORTED_EXTENSIONS and not name.startswith("."):
                out.append(os.path.join(current, name))
                if len(out) >= _MAX_SIBLINGS:
                    return out
    return out


def _read(path: str) -> Optional[Dict[str, Any]]:
    from mutagen import File as MutagenFile

    read_tag, _write = _helpers()
    try:
        audio = MutagenFile(path)
    except Exception as exc:
        logger.debug("could not open %s: %s", path, exc)
        return None
    if audio is None or audio.tags is None:
        return None
    return {"audio": audio, "album": read_tag(audio, "album") or "",
            **{field: (read_tag(audio, field) or "").strip() for field in _FIELDS}}


def _write(path: str, values: Dict[str, str]) -> bool:
    from mutagen import File as MutagenFile

    from core.fork import tags

    _read_tag, write_tag = _helpers()
    audio = MutagenFile(path)
    if audio is None or audio.tags is None:
        return False
    changed = False
    for field, value in values.items():
        if value and write_tag(audio, field, value):
            changed = True
    if changed:
        tags._save(audio)
    return changed


def harmonize(file_path: str, final_path: Optional[str] = None) -> Dict[str, Any]:
    """Make ``file_path`` and the files of the album folder it is headed for
    (``final_path``; the file's own folder when omitted) agree on the album's
    MusicBrainz ids. Returns what was done, for the log and the tests."""
    result: Dict[str, Any] = {"adopted": False, "propagated": 0, "conflict": False}
    if not config.get("albums.keep_ids_consistent"):
        return result
    mine = _read(file_path)
    if mine is None:
        return result
    folder = album_folder(final_path or file_path)
    if not os.path.isdir(folder):
        return result
    here = os.path.normpath(file_path)
    there = os.path.normpath(final_path) if final_path else here
    siblings: List[Tuple[str, Dict[str, Any]]] = []
    for path in _audio_files(folder):
        if os.path.normpath(path) in (here, there):
            continue
        info = _read(path)
        # same folder, same album name: a folder can hold more than one album
        if info is not None and _fold(info["album"]) == _fold(mine["album"]):
            siblings.append((path, info))
    if not siblings:
        return result

    ids = Counter(info["musicbrainz_albumid"] for _p, info in siblings if info["musicbrainz_albumid"])
    my_id = mine["musicbrainz_albumid"]
    if ids:
        album_id, _count = ids.most_common(1)[0]
        result["conflict"] = len(ids) > 1 or (bool(my_id) and my_id != album_id)
        if my_id != album_id:
            donor = next(info for _p, info in siblings if info["musicbrainz_albumid"] == album_id)
            _write(file_path, {f: donor[f] for f in _FIELDS})
            result["adopted"] = True
            logger.info("Album id for %s taken from its album folder (%s)", os.path.basename(file_path), album_id)
        my_id = album_id
        mine = {**mine, **{f: next(info for _p, info in siblings
                                    if info["musicbrainz_albumid"] == album_id)[f] for f in _FIELDS}}
        if len(ids) > 1:
            logger.warning("Album folder %s holds %s different MusicBrainz release ids; run Tools → "
                           "Album Tag Consistency", folder, len(ids))
            return result
    if my_id:
        for path, info in siblings:
            if not info["musicbrainz_albumid"]:
                try:
                    if _write(path, {f: mine[f] for f in _FIELDS}):
                        result["propagated"] += 1
                except Exception as exc:
                    logger.debug("could not write album id to %s: %s", path, exc)
        if result["propagated"]:
            logger.info("Album id %s written to %s earlier track(s) in %s", my_id, result["propagated"],
                        os.path.basename(folder))
    return result
