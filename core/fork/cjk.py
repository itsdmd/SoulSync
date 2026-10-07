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


# Terms that DESCRIBE a release or recording rather than name it. A bracket
# holding one of these next to a CJK name — "危機合約滌墨作戰 (Original
# Soundtrack)", "夜曲 (Live)", "夜曲 - 2019 Remaster" — is decoration: it is
# never mistaken for the name's translation, and is kept as written after the
# translated name. Editable in LLM & Tagging → Translated names; this is the
# default list. A four-digit year always counts.
DEFAULT_KEEP_TERMS = (
    "OST, Original Soundtrack, Original Sound Track, Soundtrack, Sound Track, Original Score, "
    "Original Game Soundtrack, Original Motion Picture Soundtrack, Music From, BGM, "
    "EP, Single, Album, LP, Mini Album, Maxi Single, Compilation, Best Of, Greatest Hits, Collection, "
    "Remaster, Remastered, Remix, Remixed, Mix, Mixed, Edit, Radio Edit, Version, Ver, Ver., "
    "Deluxe, Deluxe Edition, Edition, Expanded, Extended, Anniversary, Limited, Collector's, Bonus, Bonus Track, "
    "Live, Unplugged, Acoustic, Demo, Instrumental, Inst, Inst., Karaoke, Off Vocal, A Cappella, Acapella, "
    "TV Size, Game Size, Movie Size, Short, Short Ver, Full, Full Ver, Full Size, "
    "Feat, Feat., Ft, Ft., Featuring, With, Cover, Self Cover, Re-recorded, Rerecorded, "
    "Mono, Stereo, Explicit, Clean, Reprise, Interlude, Intro, Outro, Skit, Piano, Orchestra, Orchestral, "
    "CD, Disc, Disk, Vol, Vol., Volume, Digital"
)

_TRAILING_GROUP_RE = re.compile(r"^(?P<head>.*\S)\s*(?P<group>[(\[（【](?P<inner>[^()\[\]（）【】]+)[)\]）】])\s*$")
_TRAILING_DASH_RE = re.compile(r"^(?P<head>.*\S)\s+(?P<group>[-–—]\s+(?P<inner>[^-–—]+))$")

_terms_cache: dict = {}


def keep_terms(raw: object = None) -> list:
    """The configured terms (comma- or line-separated), or the defaults."""
    if raw is None:
        try:
            from core.fork import config

            raw = config.get("translate.keep_terms")
        except Exception:
            raw = None
    if raw is None:
        raw = DEFAULT_KEEP_TERMS
    out = []
    for term in re.split(r"[,\n]+", str(raw)):
        term = " ".join(term.split())
        if term and term.casefold() not in {t.casefold() for t in out}:
            out.append(term)
    return out


def _terms_regex(raw: object = None) -> "re.Pattern[str]":
    terms = keep_terms(raw)
    key = "\x1f".join(terms)
    cached = _terms_cache.get(key)
    if cached is None:
        # whole words/phrases only ("EP" must not match inside "Deep"); spaces
        # inside a phrase may be any whitespace or a hyphen ("Re-recorded")
        parts = [r"[\s\-]*".join(re.escape(word) for word in re.split(r"[\s\-]+", term) if word)
                 for term in sorted(terms, key=len, reverse=True)]
        cached = re.compile(r"(?i)(?<![a-z0-9])(?:" + "|".join(parts + [r"\d{4}"]) + r")(?![a-z0-9])")
        if len(_terms_cache) > 20:
            _terms_cache.clear()
        _terms_cache[key] = cached
    return cached


def is_decoration(text: str) -> bool:
    """Whether ``text`` is release/recording decoration rather than a name."""
    return bool(text) and not contains_cjk(text) and bool(_terms_regex().search(text))


def is_only_decoration(text: str) -> bool:
    """Stricter: ``text`` consists of nothing BUT such terms ("Original
    Soundtrack", "Deluxe Edition 2019"), with no other word in it. "Arknights
    OST" is not — that is a title that happens to contain a term."""
    if not text or contains_cjk(text):
        return False
    rest = _terms_regex().sub(" ", text)
    return rest != text and not any(ch.isalnum() for ch in rest)


def _is_version_text(inner: str) -> bool:
    return is_decoration(inner)


# "相变临界OST", "夜曲 Remix": a Latin tail attached straight to the CJK name
# with no bracket. Only split off when the tail is nothing but decoration.
_GLUED_TAIL_RE = re.compile(r"^(?P<name>.*[^\x00-\x7F])\s*(?P<tail>[A-Za-z][A-Za-z0-9 .'\-]*)$")


def _split_glued_tail(core: str) -> Tuple[str, str]:
    m = _GLUED_TAIL_RE.match(core)
    if m and contains_cjk(m.group("name")) and is_only_decoration(m.group("tail")):
        return m.group("name").strip(), m.group("tail").strip()
    return core, ""


_convert = None


def to_simplified(text: str) -> str:
    """Traditional Chinese -> Simplified, for COMPARING names only. Sources
    disagree on the script (相變臨界 vs 相变临界) and they are the same name.
    Uses the optional ``zhconv`` package; without it the text is unchanged."""
    global _convert
    if _convert is None:
        try:
            import zhconv

            _convert = lambda value: zhconv.convert(value, "zh-hans")  # noqa: E731
        except Exception:
            _convert = lambda value: value  # noqa: E731
    try:
        return _convert(text)
    except Exception:
        return text


def fold(text: object) -> str:
    """Comparison key for a name: Unicode-normalised, case-folded, script
    variants unified, whitespace removed."""
    import unicodedata

    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    if contains_cjk(value):
        value = to_simplified(value)
    return "".join(value.split())


def script_variants(text: str) -> list:
    """``text`` plus its Simplified / Traditional spellings, for searching a
    database that may hold either."""
    out = [text]
    try:
        import zhconv

        for locale in ("zh-hans", "zh-hant"):
            variant = zhconv.convert(text, locale)
            if variant and variant not in out:
                out.append(variant)
    except Exception:  # noqa: S110 - the package is optional
        pass
    return out


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
            if is_only_decoration(head):
                # "Original Soundtrack (危機合約滌墨作戰)" — a name an earlier
                # mis-split produced: the bracket is the real name, the front
                # is decoration, never its translation. The front of this form
                # is normally a real translation, so it has to be NOTHING but
                # terms: "Arknights OST (明日方舟)" stays a translation.
                core = inner
                suffixes.insert(0, f"({head})")
            else:
                core, existing = inner, head

    core, tail = _split_glued_tail(core)
    if tail:
        suffixes.append(tail)
    return core, existing, " ".join(suffixes)
