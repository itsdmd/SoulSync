"""How several artists on one track are written to tags.

Two mutually exclusive strategies (LLM & Tagging → Artists):

* **separate tags** (default) — one tag value per artist::

      ARTIST=Artist A
      ARTIST=Artist B

  Media servers credit each artist directly, with no separator to guess at.
* **one tag, chosen separator** — ``ARTIST=Artist A; Artist B`` with the
  separator the user picked.

The same strategy is applied to ALBUMARTIST. Used by the album auto-tagger,
the tag pass after downloads/imports, and the Comma Artist Splitter job.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.fork import config
from utils.logging_config import get_logger

logger = get_logger("fork.artist_format")

# setting value -> the text written between artists
SEPARATORS: Dict[str, str] = {
    "semicolon": "; ",
    "comma": ", ",
    "slash": " / ",
    "ampersand": " & ",
}
DEFAULT_SEPARATOR = "semicolon"
# How a list of artists is shown in ONE editable text box while tags are split.
DISPLAY_SEPARATOR = "; "

# "A, B" / "A & B" / "A、B" / "A / B" / "A feat. B" / "A FT B" / "A featuring B" / "A x B" …
_CREDIT_SEPARATORS_RE = re.compile(
    r"\s*[,;、，；]\s*|\s+[&/×+]\s+|\s*\(?\b(?:feat\.?|ft\.?|featuring)\s+|\s+(?:with|vs\.?|x)\s+", re.I)

# multi-value companions upstream writes next to the display string (Picard convention)
_LIST_TAGS = {"artist": ("Artists", "artists"), "albumartist": ("Album Artists", "albumartists")}
_ID3_FRAMES = {"artist": "TPE1", "albumartist": "TPE2"}
_MP4_ATOMS = {"artist": "\xa9ART", "albumartist": "aART"}


def split_tags_enabled(override: Optional[bool] = None) -> bool:
    return bool(config.get("artists.split_tags")) if override is None else bool(override)


def separator(name: Optional[str] = None) -> str:
    key = str(name or config.get("artists.separator") or DEFAULT_SEPARATOR).strip().lower()
    return SEPARATORS.get(key, SEPARATORS[DEFAULT_SEPARATOR])


def split_credit(text: Any) -> List[str]:
    """The individual artists in a credit string. A string that is itself one
    known artist ("Simon & Garfunkel") is returned whole."""
    from core.fork import artist_names

    text = " ".join(str(text or "").split())
    if not text:
        return []
    parts = [p.strip(" )") for p in _CREDIT_SEPARATORS_RE.split(text)]
    parts = [p for p in parts if p]
    if len(parts) <= 1 or artist_names.is_single_artist(text):
        return [text]
    return parts


def artist_list(credits: Any, apply_rules: bool = False) -> List[str]:
    """Flat, de-duplicated artist names from one credit string or a list of
    them (each of which may still hide several artists)."""
    from core.fork import artist_names

    if isinstance(credits, str):
        credits = [credits]
    out: List[str] = []
    for credit in credits or []:
        for name in split_credit(credit):
            if apply_rules:
                name = artist_names.resolve(name)
            if name and name.casefold() not in {n.casefold() for n in out}:
                out.append(name)
    return out


def tag_values(names: List[str], split: Optional[bool] = None, sep: Optional[str] = None) -> List[str]:
    """What to store in the tag: one value per artist, or one joined value."""
    names = [n for n in names if n]
    if not names:
        return []
    if split_tags_enabled(split):
        return list(names)
    return [separator(sep).join(names)]


def display(names: List[str], split: Optional[bool] = None, sep: Optional[str] = None) -> str:
    """The same artists as one line of text for an editable field."""
    if split_tags_enabled(split):
        return DISPLAY_SEPARATOR.join(n for n in names if n)
    return separator(sep).join(n for n in names if n)


def parse_display(text: Any, split: Optional[bool] = None) -> List[str]:
    """Inverse of :func:`display` for hand-edited text. With separate tags the
    box is split on ``;``; with a joined tag the text is taken as written."""
    text = " ".join(str(text or "").split())
    if not text:
        return []
    if not split_tags_enabled(split):
        return [text]
    seen: List[str] = []
    for part in text.split(";"):
        part = part.strip()
        if part and part.casefold() not in {s.casefold() for s in seen}:
            seen.append(part)
    return seen


def _kind(audio: Any) -> str:
    from core.fork import tags

    return tags._kind(audio)


def read_values(audio: Any, field: str) -> List[str]:
    """Every value currently stored for ``artist`` / ``albumartist``."""
    kind = _kind(audio)
    raw: Any = None
    if kind == "id3":
        frame = audio.tags.get(_ID3_FRAMES[field])
        raw = getattr(frame, "text", None) if frame is not None else None
    elif kind == "vorbis":
        raw = audio.get(field)
    elif kind == "mp4":
        raw = audio.get(_MP4_ATOMS[field])
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    # a zero-padded ID3v2.4 frame reads back with a trailing empty value
    return [str(v).strip() for v in raw if str(v).strip()]


def read_list_tag(audio: Any, field: str) -> List[str]:
    """The companion multi-value tag (ARTISTS / ALBUMARTISTS), when present."""
    kind = _kind(audio)
    desc, vorbis_key = _LIST_TAGS[field]
    raw: Any = None
    if kind == "id3":
        frame = audio.tags.get(f"TXXX:{desc}")
        raw = getattr(frame, "text", None) if frame is not None else None
    elif kind == "vorbis":
        raw = audio.get(vorbis_key)
    if not raw:
        return []
    return [str(v).strip() for v in raw if str(v).strip()]


def write_values(audio: Any, field: str, names: List[str], split: Optional[bool] = None,
                 sep: Optional[str] = None) -> bool:
    """Write ``names`` to ``artist`` / ``albumartist`` using the strategy, and
    keep the companion list tag in step. Returns whether anything changed.
    The caller saves the file."""
    from mutagen import id3

    kind = _kind(audio)
    values = tag_values(names, split, sep)
    if not kind or not values:
        return False
    desc, vorbis_key = _LIST_TAGS[field]
    before = (read_values(audio, field), read_list_tag(audio, field))
    if kind == "id3":
        frame = _ID3_FRAMES[field]
        audio.tags.setall(frame, [getattr(id3, frame)(encoding=3, text=list(values))])
        if len(names) > 1:
            audio.tags.setall(f"TXXX:{desc}", [id3.TXXX(encoding=3, desc=desc, text=list(names))])
        else:
            audio.tags.delall(f"TXXX:{desc}")
    elif kind == "vorbis":
        audio[field] = list(values)
        if len(names) > 1:
            audio[vorbis_key] = list(names)
        elif vorbis_key in audio:
            del audio[vorbis_key]
    elif kind == "mp4":
        audio[_MP4_ATOMS[field]] = list(values)
    return before != (read_values(audio, field), read_list_tag(audio, field))


def current_names(audio: Any, field: str) -> List[str]:
    """The artists a file credits in ``field``, however it stores them: several
    tag values, or one separator-joined string.

    The companion list tag (ARTISTS) is only used to split a joined string
    correctly. A display value naming ONE artist is left as one artist even if
    the list holds more — that is upstream's "featured artists go in the
    title" layout, and the user chose it.
    """
    values = read_values(audio, field)
    if len(values) > 1:
        return artist_list(values)
    if not values:
        return []
    if len([p for p in _CREDIT_SEPARATORS_RE.split(values[0]) if p.strip(" )")]) <= 1:
        return [values[0]]
    listed = read_list_tag(audio, field)
    if len(listed) > 1:
        return artist_list(listed)
    return artist_list(values)
