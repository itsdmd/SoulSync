"""Custom tagging rules for artist names (no LLM involved).

A rule maps the name a metadata source reports ("澤野弘之") to the name that
should be written to tags and folders ("Hiroyuki Sawano"). Rules come from:

1. the GUI (``source = manual``) — always win, and work for any name;
2. MusicBrainz aliases (``source = musicbrainz``) — looked up automatically
   for CJK names, using the artist's primary alias in the target locale.

Misses are cached too (``source = none``) and retried after a month.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional

from core.fork import config, store
from core.fork.cjk import contains_cjk
from utils.logging_config import get_logger

logger = get_logger("fork.artist_names")

_MISS_RETRY_SECONDS = 30 * 24 * 3600
_MIN_SCORE = 90
_lookup_lock = threading.Lock()
_client = None

# Separators between artists in a display credit. Whitespace-bounded where the
# bare character also occurs inside names.
_CREDIT_SPLIT_RE = re.compile(r"(\s*[,;、]\s*|\s+[&/×]\s+|\s+(?:feat\.?|ft\.?|featuring|with|vs\.?|x)\s+)", re.I)


def _mb_client():
    global _client
    if _client is None:
        from core.musicbrainz_client import MusicBrainzClient

        _client = MusicBrainzClient()
    return _client


def _locale() -> str:
    language = str(config.get("translate.target_language") or "English").strip().lower()
    return {"english": "en", "vietnamese": "vi", "french": "fr", "german": "de", "spanish": "es"}.get(language, "en")


def pick_name(artist: Dict[str, Any], original: str, locale: str = "en") -> str:
    """Best non-CJK name for a MusicBrainz artist record, or ''."""
    aliases = [a for a in (artist.get("aliases") or []) if isinstance(a, dict) and a.get("name")]

    def usable(name: Any) -> bool:
        return isinstance(name, str) and bool(name.strip()) and not contains_cjk(name) \
            and name.strip().casefold() != original.strip().casefold()

    for want_primary in (True, False):
        for alias in aliases:
            if alias.get("locale") == locale and usable(alias["name"]) \
                    and (bool(alias.get("primary")) or not want_primary) \
                    and (alias.get("type") in (None, "Artist name")):
                return alias["name"].strip()
    if usable(artist.get("name")):
        return artist["name"].strip()
    sort_name = artist.get("sort-name")
    if usable(sort_name):
        parts = [p.strip() for p in sort_name.split(",")]
        if artist.get("type") == "Person" and len(parts) == 2 and all(parts):
            return f"{parts[1]} {parts[0]}"
        return sort_name.strip()
    return ""


def _matches(artist: Dict[str, Any], original: str) -> bool:
    target = original.strip().casefold()
    names = [artist.get("name")] + [a.get("name") for a in (artist.get("aliases") or []) if isinstance(a, dict)]
    return any(isinstance(n, str) and n.strip().casefold() == target for n in names)


def lookup_musicbrainz(original: str) -> Dict[str, str]:
    """``{"replacement", "mbid"}`` — empty replacement when nothing fits.
    Raises on transport errors so a timeout is never cached as a miss."""
    results: List[Dict[str, Any]] = _mb_client().search_artist(original, limit=5, strict=False, raise_on_error=True)
    for artist in results or []:
        try:
            score = int(artist.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score < _MIN_SCORE or not _matches(artist, original):
            continue
        name = pick_name(artist, original, _locale())
        if name:
            return {"replacement": name, "mbid": str(artist.get("id") or "")}
    return {"replacement": "", "mbid": ""}


_ALIAS_LOCALES = ("en", "ja", "zh", "ko")
_ALIAS_TTL_SECONDS = 90 * 24 * 3600


def known_names(name: str, limit: int = 4) -> List[str]:
    """Other names MusicBrainz records for the artist called ``name`` (its
    canonical name and primary aliases in Latin/CJK locales). Used to anchor
    search suggestions: a variant may only use an artist name from this list.
    Empty when MusicBrainz does not know the artist or cannot be reached."""
    key = (name or "").strip()
    if not key:
        return []
    cached = store.get_artist_aliases(key)
    if cached and time.time() - float(cached["updated_at"] or 0) < _ALIAS_TTL_SECONDS:
        return [a for a in cached["aliases"] if isinstance(a, str)][:limit]
    with _lookup_lock:
        try:
            results = _mb_client().search_artist(key, limit=5, strict=False, raise_on_error=True)
        except Exception as exc:
            logger.debug("Alias lookup failed for %r: %s", key, exc)
            return []
        names: List[str] = []
        for artist in results or []:
            try:
                score = int(artist.get("score") or 0)
            except (TypeError, ValueError):
                score = 0
            if score < _MIN_SCORE or not _matches(artist, key):
                continue
            candidates = [artist.get("name")]
            for alias in artist.get("aliases") or []:
                if not isinstance(alias, dict) or alias.get("type") not in (None, "Artist name"):
                    continue
                locale = str(alias.get("locale") or "")[:2]
                if alias.get("primary") and locale in _ALIAS_LOCALES:
                    candidates.append(alias.get("name"))
            for candidate in candidates:
                if isinstance(candidate, str) and candidate.strip() \
                        and candidate.strip().casefold() != key.casefold() \
                        and candidate.strip().casefold() not in {n.casefold() for n in names}:
                    names.append(candidate.strip())
            break  # only the best matching artist
        store.save_artist_aliases(key, names)
        return names[:limit]


def resolve(name: str, allow_lookup: bool = True) -> str:
    """The name to write for ``name`` (itself when no rule applies)."""
    if not isinstance(name, str) or not name.strip() or not config.get("artist_names.enabled"):
        return name
    key = name.strip()
    row = store.get_artist_name(key)
    if row:
        if row["source"] != "none":
            return row["replacement"] or name
        if time.time() - float(row.get("updated_at") or 0) < _MISS_RETRY_SECONDS:
            return name
    if not allow_lookup or not config.get("artist_names.auto_lookup") or not contains_cjk(key):
        return name
    with _lookup_lock:
        row = store.get_artist_name(key)
        if row and (row["source"] != "none" or time.time() - float(row.get("updated_at") or 0) < _MISS_RETRY_SECONDS):
            return (row["replacement"] or name) if row["source"] != "none" else name
        try:
            found = lookup_musicbrainz(key)
        except Exception as exc:
            logger.warning("Artist name lookup failed for %r: %s", key, exc)
            return name
        if found["replacement"]:
            store.save_artist_name(key, found["replacement"], "musicbrainz", found["mbid"])
            logger.info("Artist rule from MusicBrainz: %r -> %r", key, found["replacement"])
            return found["replacement"]
        store.save_artist_name(key, "", "none")
        return name


def resolve_credit(credit: str, allow_lookup: bool = True) -> str:
    """Apply rules to a display credit, which may list several artists."""
    if not isinstance(credit, str) or not credit.strip() or not config.get("artist_names.enabled"):
        return credit
    pieces = _CREDIT_SPLIT_RE.split(credit)
    if len(pieces) == 1:
        return resolve(credit, allow_lookup)
    # A rule for the exact full string wins ("Simon & Garfunkel" is one artist).
    whole = resolve(credit, allow_lookup=False)
    if whole != credit:
        return whole
    # Even indexes are names, odd indexes the separators (kept verbatim).
    return "".join(resolve(p, allow_lookup) if i % 2 == 0 else p for i, p in enumerate(pieces))


def resolve_list(names: Any, allow_lookup: bool = True) -> Optional[List[str]]:
    if not isinstance(names, (list, tuple)):
        return None
    return [resolve(n, allow_lookup) if isinstance(n, str) else n for n in names]


_single_artist_cache: Dict[str, bool] = {}


def is_single_artist(name: str) -> bool:
    """Whether ``name`` is ONE artist even though it contains something that
    looks like a separator ("Simon & Garfunkel", "Tyler, The Creator").

    True when a tagging rule exists for the exact name, or MusicBrainz lists
    an artist under exactly this name. When MusicBrainz cannot be asked the
    answer is True: leaving a credit whole is the safe mistake.
    """
    key = (name or "").strip()
    if not key:
        return True
    folded = key.casefold()
    if folded in _single_artist_cache:
        return _single_artist_cache[folded]
    row = store.get_artist_name(key)
    if row and row["source"] != "none":
        return True
    try:
        results = _mb_client().search_artist(key, limit=5, strict=False, raise_on_error=True)
    except Exception as exc:
        logger.debug("Could not check %r on MusicBrainz: %s", key, exc)
        return True  # not cached: ask again next time
    known = False
    for artist in results or []:
        try:
            score = int(artist.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score >= _MIN_SCORE and _matches(artist, key):
            known = True
            break
    if len(_single_artist_cache) > 5000:
        _single_artist_cache.clear()
    _single_artist_cache[folded] = known
    return known
