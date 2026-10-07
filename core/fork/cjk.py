"""CJK detection and name splitting.

Ranges follow itsdmd/translate-music-library (``music_translator.cjk``), with
Hangul added so Korean titles are covered too.
"""

from __future__ import annotations

import re
from typing import Tuple

CJK_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x2A6DF),  # Extension B
    (0x2A700, 0x2EBEF),  # Extensions C-F
    (0x2EBF0, 0x2EE5F),  # Extension I
    (0x2F800, 0x2FA1F),  # Compatibility Ideographs Supplement
    (0x30000, 0x323AF),  # Extensions G-H
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x31F0, 0x31FF),  # Katakana Phonetic Extensions
    (0xFF66, 0xFF9F),  # Half-width Katakana
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x3130, 0x318F),  # Hangul Compatibility Jamo
    (0xAC00, 0xD7AF),  # Hangul Syllables
)


def contains_cjk(value: object) -> bool:
    if not isinstance(value, str):
        return False
    for ch in value:
        cp = ord(ch)
        if cp < 0x1100:
            continue
        for start, end in CJK_RANGES:
            if start <= cp <= end:
                return True
    return False


# Bracketed text that describes the recording rather than naming it. Kept
# outside the "(original)" part of a translated name.
_VERSION_WORDS = (
    r"live|remaster(?:ed)?|remix|mix|ver\.?|version|edit|instrumental|inst\.?|"
    r"acoustic|demo|deluxe|bonus|karaoke|off\s*vocal|tv\s*size|short|full|"
    r"feat\.?|ft\.?|featuring|with|ost|cover|single|album|ep|mono|stereo|"
    r"explicit|clean|reprise|interlude|intro|outro|skit|piano|orchestra|"
    r"\d{4}"
)
_VERSION_RE = re.compile(rf"(?i)(?<![a-z])(?:{_VERSION_WORDS})(?![a-z])")
_TRAILING_GROUP_RE = re.compile(r"^(?P<head>.*\S)\s*(?P<group>[(\[（【](?P<inner>[^()\[\]（）【】]+)[)\]）】])\s*$")
_TRAILING_DASH_RE = re.compile(r"^(?P<head>.*\S)\s+(?P<group>[-–—]\s+(?P<inner>[^-–—]+))$")


def _is_version_text(inner: str) -> bool:
    return not contains_cjk(inner) and bool(_VERSION_RE.search(inner))


def split_name(value: str) -> Tuple[str, str, str]:
    """Split ``value`` into ``(core, existing_translation, suffix)``.

    * ``suffix``: trailing Latin-only version decoration ("(Live)",
      "- 2019 Remaster"), re-attached verbatim after the translated name.
    * ``existing_translation``: when the name is already bilingual
      ("夜曲 (Nocturne)" or "Nocturne (夜曲)"), the Latin half — reused
      instead of asking the model.
    * ``core``: the CJK text that actually needs translating.
    """
    core = (value or "").strip()
    suffixes = []
    while True:
        m = _TRAILING_GROUP_RE.match(core) or _TRAILING_DASH_RE.match(core)
        if not m or not _is_version_text(m.group("inner")) or not contains_cjk(m.group("head")):
            break
        suffixes.insert(0, m.group("group").strip())
        core = m.group("head").strip()

    existing = ""
    m = _TRAILING_GROUP_RE.match(core)
    if m:
        head, inner = m.group("head").strip(), m.group("inner").strip()
        head_cjk, inner_cjk = contains_cjk(head), contains_cjk(inner)
        if head_cjk and not inner_cjk and re.search(r"[A-Za-z]{2}", inner):
            core, existing = head, inner
        elif inner_cjk and not head_cjk and re.search(r"[A-Za-z]{2}", head):
            core, existing = inner, head

    return core, existing, " ".join(suffixes)
