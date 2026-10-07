"""Rewrite name tags on a finished file: artist rules + name translation.

Runs AFTER upstream's metadata enhancement, on the tags it just wrote, so
upstream's source lookups (MusicBrainz, Deezer, …) still see the original
names and nothing in that pipeline has to know about the fork.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from core.fork import artist_names, config, translate
from utils.logging_config import get_logger

logger = get_logger("fork.tags")

# field -> (ID3 frame, Vorbis key, MP4 atom)
_FIELDS = {
    "title": ("TIT2", "title", "\xa9nam"),
    "album": ("TALB", "album", "\xa9alb"),
    "artist": ("TPE1", "artist", "\xa9ART"),
    "albumartist": ("TPE2", "albumartist", "aART"),
}
# multi-value lists: field -> (ID3 TXXX desc, Vorbis key)
_LIST_FIELDS = {"artists": ("Artists", "artists"), "albumartists": ("Album Artists", "albumartists")}
_ORIGINAL_PREFIX = "SOULSYNC_ORIGINAL_"


def _kind(audio: Any) -> str:
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4

    if isinstance(audio, MP4):
        return "mp4"
    if isinstance(getattr(audio, "tags", None), ID3):
        return "id3"
    name = type(audio).__name__
    if name in ("FLAC", "OggVorbis", "OggOpus", "OggFLAC", "OggSpeex"):
        return "vorbis"
    return ""


def _read(audio: Any, kind: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for field, (frame, vkey, atom) in _FIELDS.items():
        value: Optional[str] = None
        if kind == "id3":
            f = audio.tags.get(frame)
            if f is not None and f.text:
                value = str(f.text[0])
        elif kind == "vorbis":
            v = audio.get(vkey)
            if v:
                value = str(v[0])
        elif kind == "mp4":
            v = audio.get(atom)
            if v:
                value = str(v[0])
        if value:
            out[field] = value
    for field, (desc, vkey) in _LIST_FIELDS.items():
        values: Optional[List[str]] = None
        if kind == "id3":
            f = audio.tags.get(f"TXXX:{desc}")
            if f is not None and f.text:
                values = [str(t) for t in f.text]
        elif kind == "vorbis":
            v = audio.get(vkey)
            if v:
                values = [str(t) for t in v]
        if values:
            out[field] = values
    return out


def _write(audio: Any, kind: str, field: str, value: Any) -> None:
    from mutagen import id3

    if field in _FIELDS:
        frame, vkey, atom = _FIELDS[field]
        if kind == "id3":
            audio.tags.setall(frame, [getattr(id3, frame)(encoding=3, text=[value])])
        elif kind == "vorbis":
            audio[vkey] = [value]
        elif kind == "mp4":
            audio[atom] = [value]
    elif field in _LIST_FIELDS:
        desc, vkey = _LIST_FIELDS[field]
        if kind == "id3":
            audio.tags.setall(f"TXXX:{desc}", [id3.TXXX(encoding=3, desc=desc, text=list(value))])
        elif kind == "vorbis":
            audio[vkey] = list(value)


def _write_original(audio: Any, kind: str, field: str, value: str) -> None:
    from mutagen import id3

    name = f"{_ORIGINAL_PREFIX}{field.upper()}"
    if kind == "id3":
        audio.tags.setall(f"TXXX:{name}", [id3.TXXX(encoding=3, desc=name, text=[value])])
    elif kind == "vorbis":
        audio[name.lower()] = [value]
    elif kind == "mp4":
        audio[f"----:com.apple.iTunes:{name}"] = [value.encode("utf-8")]


def transform_values(current: Dict[str, Any], allow_network: bool = True) -> Dict[str, Any]:
    """New values for the name fields in ``current`` (only the changed ones)."""
    changed: Dict[str, Any] = {}
    for field in ("artist", "albumartist"):
        if current.get(field):
            new = artist_names.resolve_credit(current[field], allow_network)
            if new and new != current[field]:
                changed[field] = new
    for field in _LIST_FIELDS:
        if current.get(field):
            new_list = artist_names.resolve_list(current[field], allow_network)
            if new_list and new_list != current[field]:
                changed[field] = new_list
    hint = {"artist": changed.get("artist") or current.get("artist") or "",
            "album": current.get("album") or ""}
    if current.get("album") and translate.enabled("album"):
        new = translate.translate_name("album", current["album"], {"artist": hint["artist"]}, allow_network)
        if new and new != current["album"]:
            changed["album"] = new
    if current.get("title") and translate.enabled("title"):
        new = translate.translate_name("title", current["title"], hint, allow_network)
        if new and new != current["title"]:
            changed["title"] = new
    return changed


def _save(audio: Any) -> None:
    try:
        from core.metadata.common import get_mutagen_symbols, save_audio_file

        symbols = get_mutagen_symbols()
        if symbols and save_audio_file(audio, symbols):
            return
    except Exception as exc:
        logger.debug("Atomic save unavailable, saving in place: %s", exc)
    audio.save()


def apply_to_file(file_path: str, lock_factory: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Apply artist rules and name translation to ``file_path``'s tags.
    Returns ``{field: new_value}`` for what changed."""
    if not config.get("translate.apply_to_tags") or not file_path or not os.path.isfile(file_path):
        return {}
    from mutagen import File as MutagenFile

    if lock_factory is None:
        try:
            from core.metadata.common import get_file_lock as lock_factory  # type: ignore[no-redef]
        except Exception:
            lock_factory = None

    def run() -> Dict[str, Any]:
        audio = MutagenFile(file_path)
        if audio is None or audio.tags is None:
            return {}
        kind = _kind(audio)
        if not kind:
            return {}
        current = _read(audio, kind)
        changed = transform_values(current)
        if not changed:
            return {}
        keep_original = bool(config.get("translate.write_original_tags"))
        for field, value in changed.items():
            _write(audio, kind, field, value)
            if keep_original and field in _FIELDS:
                _write_original(audio, kind, field, current[field])
        _save(audio)
        logger.info("Fork tags for %s: %s", os.path.basename(file_path),
                    {k: v for k, v in changed.items() if k in _FIELDS})
        return changed

    if lock_factory is not None:
        with lock_factory(file_path):
            return run()
    return run()
