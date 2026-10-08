"""Every tag of a file beyond the Tag Editor's fixed fields.

The editor (:mod:`core.fork.editor`) knows ten fields by name. This module
reads and writes everything else a file carries, so the editor can show a
file's tags exhaustively: Vorbis comments by their key, ID3 frames, MP4 atoms
and free-form (``----``) atoms.

A tag is addressed by an upper-case NAME. Where the formats have a standard
home for the same thing (BPM, ISRC, LYRICS…) they share the name; a Vorbis
key, an ID3 ``TXXX`` description and an iTunes free-form name are their own
name; anything else goes by its frame or atom id. Tags that are not text
(binary frames, URLs, extra comments) are listed read-only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from utils.logging_config import get_logger

logger = get_logger("fork.all_tags")

# ID3 frame -> name, for frames the fixed fields do not use
_ID3_NAMES = {
    "TBPM": "BPM", "TSRC": "ISRC", "TCOP": "COPYRIGHT", "TPUB": "LABEL", "TENC": "ENCODEDBY",
    "TSSE": "ENCODERSETTINGS", "TKEY": "KEY", "TLAN": "LANGUAGE", "TMED": "MEDIA", "TMOO": "MOOD",
    "TIT1": "GROUPING", "TIT3": "SUBTITLE", "TEXT": "LYRICIST", "TPE3": "CONDUCTOR", "TPE4": "REMIXER",
    "TSOP": "ARTISTSORT", "TSO2": "ALBUMARTISTSORT", "TSOA": "ALBUMSORT", "TSOT": "TITLESORT",
    "TSOC": "COMPOSERSORT", "TCMP": "COMPILATION", "TDOR": "ORIGINALDATE", "TOPE": "ORIGINALARTIST",
    "TOAL": "ORIGINALALBUM", "TSST": "DISCSUBTITLE",
}
_MP4_NAMES = {
    "tmpo": "BPM", "\xa9lyr": "LYRICS", "cprt": "COPYRIGHT", "\xa9grp": "GROUPING", "cpil": "COMPILATION",
    "soar": "ARTISTSORT", "soaa": "ALBUMARTISTSORT", "soal": "ALBUMSORT", "sonm": "TITLESORT",
    "soco": "COMPOSERSORT", "\xa9too": "ENCODEDBY", "pgap": "GAPLESS", "desc": "DESCRIPTION",
}
_ID3_BY_NAME = {name: frame for frame, name in _ID3_NAMES.items()}
_MP4_BY_NAME = {name: atom for atom, name in _MP4_NAMES.items()}

# what the editor's fixed fields (and the cover) already show
_ID3_KNOWN = {"TIT2", "TPE1", "TPE2", "TALB", "TDRC", "TRCK", "TPOS", "TCON", "TCOM", "APIC"}
_VORBIS_KNOWN = {"title", "artist", "albumartist", "album", "date", "tracknumber", "discnumber", "genre",
                 "composer", "comment", "tracktotal", "totaltracks", "disctotal", "totaldiscs",
                 "metadata_block_picture", "coverart", "coverartmime"}
_MP4_KNOWN = {"\xa9nam", "\xa9ART", "aART", "\xa9alb", "\xa9day", "trkn", "disk", "\xa9gen", "\xa9wrt",
              "\xa9cmt", "covr"}
_FREEFORM = "----:com.apple.iTunes:"
_MP4_INT = {"tmpo"}
_MP4_BOOL = {"cpil", "pgap", "pcst"}
_TEXT_FRAME = re.compile(r"^T[A-Z0-9]{3}$")
_NEW_NAME = re.compile(r"^[\x20-\x3c\x3e-\x7d]{1,120}$")       # a Vorbis key: printable ASCII, no "="


def _join(values: Any) -> str:
    if values is None:
        return ""
    if not isinstance(values, (list, tuple)):
        values = [values]
    texts = [str(v) for v in values if str(v).strip()]
    return texts[0] if len(texts) == 1 else "; ".join(t.strip() for t in texts)


def _values(value: str, several: bool) -> List[str]:
    """Typed text as tag values: one value, unless the tag already held several."""
    if several:
        return [part.strip() for part in value.split(";") if part.strip()]
    return [value] if value.strip() else []


def _blob(value: Any) -> str:
    for attr in ("url", "text", "owner", "email"):
        found = getattr(value, attr, None)
        if isinstance(found, (list, tuple)):
            found = _join(found)
        if isinstance(found, str) and found:
            return found
    data = getattr(value, "data", value)
    return f"({len(data):,} bytes of data)" if isinstance(data, (bytes, bytearray)) else str(value)


# ── reading ─────────────────────────────────────────────────────────────

def _first_comment(audio: Any) -> Any:
    frames = audio.tags.getall("COMM")
    return frames[0] if frames else None


def _first_lyrics(audio: Any) -> Any:
    frames = audio.tags.getall("USLT")
    return frames[0] if frames else None


def _read_id3(audio: Any, out: Dict[str, str], readonly: List[str]) -> None:
    comment, lyrics = _first_comment(audio), _first_lyrics(audio)
    for key, frame in audio.tags.items():
        fid = frame.FrameID
        if fid in _ID3_KNOWN or frame is comment:
            continue
        name: Optional[str] = None
        if frame is lyrics:
            name, value = "LYRICS", str(frame.text or "")
        elif fid == "TXXX":
            name, value = (frame.desc or "").upper() or "TXXX", _join([str(t) for t in frame.text])
        elif _TEXT_FRAME.match(fid) and hasattr(frame, "text"):
            name, value = _ID3_NAMES.get(fid, fid), _join([str(t) for t in frame.text])
        if name is None or name in out:
            name, value = key, _blob(frame)         # not text, or a second tag of the same name
            readonly.append(name)
        out[name] = value


def _read_vorbis(audio: Any, out: Dict[str, str]) -> None:
    if audio.tags is None:
        return
    for key in dict.fromkeys(k.lower() for k, _v in audio.tags):
        if key not in _VORBIS_KNOWN:
            out[key.upper()] = _join(audio.tags.get(key))


def _read_mp4(audio: Any, out: Dict[str, str], readonly: List[str]) -> None:
    for atom, value in audio.tags.items():
        if atom in _MP4_KNOWN:
            continue
        items = value if isinstance(value, list) else [value]
        if atom.startswith(_FREEFORM):
            name = atom[len(_FREEFORM):].upper()
            text: Optional[str] = _join([bytes(v).decode("utf-8", "replace") for v in items])
        elif all(isinstance(v, str) for v in items):
            name, text = _MP4_NAMES.get(atom, atom), _join(items)
        elif atom in _MP4_INT or atom in _MP4_BOOL:
            name, text = _MP4_NAMES.get(atom, atom), (str(int(items[0])) if items else "")
        else:
            name, text = atom, None
        if text is None or name in out:
            name, text = atom, "; ".join(_blob(v) for v in items)
            readonly.append(name)
        out[name] = text


def read(audio: Any, kind: str) -> Tuple[Dict[str, str], List[str]]:
    """``({NAME: value}, [names that cannot be edited])`` for every tag the
    fixed fields do not cover."""
    out: Dict[str, str] = {}
    readonly: List[str] = []
    if audio.tags is None:
        return out, readonly
    if kind == "id3":
        _read_id3(audio, out, readonly)
    elif kind == "vorbis":
        _read_vorbis(audio, out)
    elif kind == "mp4":
        _read_mp4(audio, out, readonly)
    else:
        try:
            for key, value in audio.tags.items():
                out[str(key)] = _blob(value) if not isinstance(value, (list, tuple, str)) else _join(value)
                readonly.append(str(key))
        except Exception as exc:
            logger.debug("tags of an unknown format not listed: %s", exc)
    return out, readonly


# ── writing ─────────────────────────────────────────────────────────────

def _new_name(name: str) -> str:
    if not _NEW_NAME.match(name) or not name.strip():
        raise ValueError(f'"{name}" cannot be used as a tag name')
    return name


def _write_id3(audio: Any, name: str, value: str) -> None:
    from mutagen import id3

    if name == "LYRICS":
        old = _first_lyrics(audio)
        if old is not None:
            audio.tags.delall(old.HashKey)
        if value.strip():
            audio.tags.add(id3.USLT(encoding=3, lang=getattr(old, "lang", None) or "eng",
                                    desc=getattr(old, "desc", "") or "", text=value))
        return
    custom = next((f for f in audio.tags.getall("TXXX") if (f.desc or "").upper() == name), None)
    frame_id = _ID3_BY_NAME.get(name) or (name if _TEXT_FRAME.match(name) and name != "TXXX" else None)
    if frame_id and not hasattr(id3, frame_id):
        frame_id = None
    if frame_id and (custom is None or audio.tags.getall(frame_id)):
        old = audio.tags.getall(frame_id)
        several = bool(old) and len(old[0].text) > 1
        audio.tags.delall(frame_id)
        values = _values(value, several)
        if values:
            audio.tags.add(getattr(id3, frame_id)(encoding=3, text=values))
        return
    if custom is None and name in audio.tags:
        raise ValueError(f"{name} cannot be edited here")
    values = _values(value, custom is not None and len(custom.text) > 1)
    if custom is not None:
        audio.tags.delall(custom.HashKey)
    if values:
        audio.tags.add(id3.TXXX(encoding=3, desc=custom.desc if custom is not None else _new_name(name),
                                text=values))


def _write_vorbis(audio: Any, name: str, value: str) -> None:
    key = name.lower()
    if key in _VORBIS_KNOWN:
        raise ValueError(f"{name} is edited through its own field")
    old = audio.tags.get(key) if audio.tags is not None else None
    values = _values(value, bool(old) and len(old) > 1)
    if old:
        del audio[key]
    elif values:
        _new_name(name)
    if values:
        audio[key] = values


def _write_mp4(audio: Any, name: str, value: str) -> None:
    from mutagen.mp4 import MP4FreeForm

    freeform = next((k for k in audio.tags if k.startswith(_FREEFORM) and k[len(_FREEFORM):].upper() == name), None)
    atom = _MP4_BY_NAME.get(name) or (name if name in audio.tags and not name.startswith("----") else None)
    if atom and (freeform is None or atom in audio.tags):
        old = audio.tags.get(atom) or []
        if not value.strip():
            audio.tags.pop(atom, None)
        elif atom in _MP4_INT:
            try:
                audio.tags[atom] = [int(float(value.strip()))]
            except ValueError:
                raise ValueError(f'{name}: "{value}" is not a number') from None
        elif atom in _MP4_BOOL:
            audio.tags[atom] = value.strip().lower() in ("1", "true", "yes", "y")
        elif all(isinstance(v, str) for v in old):
            audio.tags[atom] = _values(value, len(old) > 1)
        else:
            raise ValueError(f"{name} cannot be edited here")
        return
    old = audio.tags.get(freeform) or [] if freeform else []
    values = _values(value, len(old) > 1)
    if freeform:
        del audio.tags[freeform]
    if values:
        audio.tags[freeform or _FREEFORM + _new_name(name)] = [MP4FreeForm(v.encode("utf-8")) for v in values]


def write(audio: Any, kind: str, name: str, value: str) -> None:
    """Set the tag ``name`` to ``value``; ``""`` removes it."""
    if kind == "id3":
        _write_id3(audio, name, value)
    elif kind == "vorbis":
        _write_vorbis(audio, name, value)
    elif kind == "mp4":
        _write_mp4(audio, name, value)
    else:
        raise ValueError("This file format cannot be tagged here")


__all__ = ["read", "write"]
