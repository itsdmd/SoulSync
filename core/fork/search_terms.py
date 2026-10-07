"""Extra search queries for tracks upstream's fixed query rules cannot find.

Upstream builds its query ladder from string rules over the one name the
metadata source gave. Files on sharing networks are often named differently:
romanized, in the original script, or under the artist's other name. Two
sources of variants are combined, appended AFTER upstream's own queries so
they only run once the standard ones found nothing:

* the ARTIST's other names come from MusicBrainz, never from the model — a
  small model will happily invent a different artist, and a variant with the
  wrong artist can match the wrong file;
* alternative TITLE spellings come from the model.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Optional

from core.fork import artist_names, config, ollama, store
from core.fork.cjk import contains_cjk
from utils.logging_config import get_logger

logger = get_logger("fork.search_terms")

_SCHEMA = {
    "type": "object",
    "properties": {"titles": {"type": "array", "items": {"type": "string"}}},
    "required": ["titles"],
}

_SYSTEM = (
    "You help find one specific music track on file-sharing networks, where files are named by "
    "many different people. Given the track's artist, title and album, list other ways the "
    "TITLE of this same recording is commonly written in file names. Useful forms: the "
    "original-script title (kanji/kana/hanzi/hangul) when the input is romanized or "
    "translated; the standard romanization (Hepburn romaji, pinyin, Revised Romanization) when "
    "the input is in another script; the official English title if one exists; the title "
    "without subtitle, punctuation or featuring credits. Only list titles you are confident "
    "name this exact track. Never give a different song, never invent a translation that is "
    "not in real use, and never include the artist or album name. Return only one JSON "
    "object: {\"titles\": [...]} with at most the requested number of titles, best first; "
    "return an empty list when there is no good alternative."
)

# query (lowercased) -> {"artist", "title"}; lets the candidate matcher score a
# result against the variant that found it.
_variant_by_query: Dict[str, Dict[str, str]] = {}
_lock = threading.Lock()
_MAX_REMEMBERED = 5000


def _first_artist(track: Any) -> str:
    artists = getattr(track, "artists", None) or []
    if not artists:
        return ""
    first = artists[0]
    if isinstance(first, dict):
        return str(first.get("name") or "").strip()
    return str(first or "").strip()


def _album(track: Any) -> str:
    for attr in ("album", "album_name", "album_title"):
        value = getattr(track, attr, None)
        if isinstance(value, dict):
            value = value.get("name")
        if value:
            return str(value).strip()
    return ""


def _remember(variants: List[Dict[str, str]]) -> None:
    with _lock:
        if len(_variant_by_query) > _MAX_REMEMBERED:
            _variant_by_query.clear()
        for v in variants:
            _variant_by_query[f"{v['artist']} {v['title']}".strip().lower()] = v


def _ascii(text: str) -> str:
    try:
        from unidecode import unidecode

        return " ".join(unidecode(text).split())
    except Exception:
        return text


def _pair_artist(title: str, artist: str, other_names: List[str]) -> str:
    """The artist form written in the same script as ``title``."""
    want_cjk = contains_cjk(title)
    for name in [artist] + other_names:
        if contains_cjk(name) == want_cjk:
            return name
    return artist


def _ask_titles(artist: str, other_names: List[str], title: str, album: str, limit: int) -> Optional[List[str]]:
    payload: Dict[str, object] = {"artist": artist, "title": title, "max_titles": limit}
    if other_names:
        payload["artist_also_known_as"] = other_names
    if album and album.casefold() != title.casefold():
        payload["album"] = album
    try:
        data = ollama.chat_json("search_terms", _SYSTEM, payload, _SCHEMA, temperature=0.1)
    except ollama.OllamaError as exc:
        logger.debug("Search-term suggestion failed for %r - %r: %s", artist, title, exc)
        return None
    banned = {n.casefold() for n in [artist, album] + other_names if n}
    titles: List[str] = []
    for item in data.get("titles") or []:
        text = " ".join(str(item or "").split())
        if 2 <= len(text) <= 150 and text.casefold() != title.casefold() \
                and text.casefold() not in banned and text not in titles:
            titles.append(text)
    return titles


def suggest(artist: str, title: str, album: str = "") -> List[Dict[str, str]]:
    """Cached list of ``{"artist", "title"}`` variants for one track."""
    limit = max(0, min(int(config.get("search_terms.max_variants") or 0), 8))
    if not title or not artist or limit == 0:
        return []
    cache_key = "\x1f".join((artist.casefold(), title.casefold(), album.casefold()))
    cached = store.get_search_terms(cache_key)
    if cached is not None:
        return cached[:limit]

    # Latin names first, folded to ASCII: file names rarely keep "ō" or "é".
    other_names = []
    for name in sorted(artist_names.known_names(artist), key=contains_cjk):
        name = name if contains_cjk(name) else _ascii(name)
        if name and name.casefold() != artist.casefold() and name not in other_names:
            other_names.append(name)
    titles = _ask_titles(artist, other_names, title, album, limit)

    variants: List[Dict[str, str]] = []
    seen = {f"{artist} {title}".casefold()}

    def add(v_artist: str, v_title: str) -> None:
        key = f"{v_artist} {v_title}".casefold()
        if key not in seen and len(variants) < limit:
            seen.add(key)
            variants.append({"artist": v_artist, "title": v_title})

    # Model titles first, each under the artist name in the matching script…
    for v_title in titles or []:
        add(_pair_artist(v_title, artist, other_names), v_title)
    # …then the original title under the artist's other names.
    # A CJK alias in front of a Latin title is not how anyone names a file.
    for name in other_names:
        if contains_cjk(title) or not contains_cjk(name):
            add(name, title)

    if titles is None and not variants:
        return []  # model unreachable and nothing verified: retry next time
    if titles is not None:
        store.save_search_terms(cache_key, variants, model=config.model_for("search_terms"))
    if variants:
        logger.info("Search variants for %r - %r: %s", artist, title, variants)
    return variants


def augment_queries(track: Any, queries: List[str]) -> List[str]:
    if not config.get("search_terms.enabled"):
        return queries
    title = str(getattr(track, "name", "") or "").strip()
    artist = _first_artist(track)
    if not title or not artist:
        # Upstream withholds unqualified queries for artist-less tracks; a
        # model guess without an artist to anchor it is no better.
        return queries
    variants = suggest(artist, title, _album(track))
    if not variants:
        return queries
    _remember(variants)
    out = list(queries)
    seen = {q.lower() for q in out if q}
    for v in variants:
        query = f"{v['artist']} {v['title']}".strip()
        if query.lower() not in seen:
            out.append(query)
            seen.add(query.lower())
    return out


def alternate_track(track: Any, query: Optional[str]) -> Optional[Any]:
    """A copy of ``track`` renamed to the variant behind ``query``, or None
    when the query is not an LLM suggestion."""
    if not query or not config.get("search_terms.match_variants"):
        return None
    with _lock:
        variant = _variant_by_query.get(str(query).strip().lower())
    if not variant:
        return None
    try:
        alt = copy.copy(track)
        alt.name = variant["title"]
        artists = list(getattr(track, "artists", None) or [])
        if artists and isinstance(artists[0], dict):
            artists[0] = {**artists[0], "name": variant["artist"]}
        elif artists:
            artists[0] = variant["artist"]
        else:
            artists = [variant["artist"]]
        alt.artists = artists
        return alt
    except Exception as exc:
        logger.debug("Could not build alternate track for %r: %s", query, exc)
        return None
