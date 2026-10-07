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

After those come progressively BROADER queries, for networks and indexers
that only return what literally contains every search word: the more words a
query has, the more ways a real file name can fail it. Each rung drops
something, so a later rung can only be reached by fewer constraints:

1. artist + title            (upstream, then the variants above)
2. artist + album            the whole release, under each album spelling
3. album alone               for uploads filed without, or under another, artist
4. part of the album name    "A Symphonic Celebration: Music from the Studio
                             Ghibli Films…" -> "A Symphonic Celebration"
5. title alone               an alternative title with no artist

Whatever a broad query returns is still judged against the track by
upstream's matcher, so a broad query can find more but cannot accept more.
"""

from __future__ import annotations

import copy
import re
import threading
from typing import Any, Dict, List, Optional

from core.fork import artist_names, config, ollama, store
from core.fork.cjk import contains_cjk
from utils.logging_config import get_logger

logger = get_logger("fork.search_terms")

_SCHEMA = {
    "type": "object",
    "properties": {"titles": {"type": "array", "items": {"type": "string"}},
                   "albums": {"type": "array", "items": {"type": "string"}}},
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
    "not in real use, and never include the artist or album name in a title. When an album is "
    "given, also list in \"albums\" other ways that ALBUM's name is written in folder names: "
    "the same kinds of forms as for titles, and shorter forms people actually use for it, such "
    "as the main title without its subtitle, or a distinctive subtitle on its own. An album "
    "form must still identify this album: never a generic phrase like \"Original Soundtrack\" "
    "or \"Greatest Hits\", and never the artist's name alone. Return only one JSON object: "
    "{\"titles\": [...], \"albums\": [...]} with at most the requested number of each, best "
    "first; use an empty list when there is no good alternative."
)

_CACHE_VERSION = "v2"
# rungs 2-5, at most this many queries each
_PER_RUNG = 2
_ALBUM_SPLIT_RE = re.compile(r"\s*[:∶：|｜/／]\s*|\s+[-–—~〜]\s+|\s*[–—]\s*")
_BRACKETS_RE = re.compile(r"\s*[\(\[（【〔「『][^\)\]）】〕」』]*[\)\]）】〕」』]\s*")
_GENERIC_RE = re.compile(
    r"^(?:the\s+)?(?:greatest hits|best of|the best|complete|collection|anthology|essentials?|"
    r"original (?:motion picture |game |video game |television |tv |anime )?(?:soundtrack|score)|"
    r"soundtrack|ost|live|unplugged|remix(?:es)?|singles?|ep|album|deluxe(?: edition)?|"
    r"bonus tracks?|vol(?:ume)?\.?\s*\d*|part\s*\d*|disc\s*\d*|unknown album)$", re.I)

# query (lowercased) -> {"artist", "title"}; lets the candidate matcher score a
# result against the variant that found it.
_variant_by_query: Dict[str, Dict[str, str]] = {}
_variants_by_broad_query: Dict[str, List[Dict[str, str]]] = {}
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


def _remember(variants: List[Dict[str, str]], broad: Optional[List[str]] = None) -> None:
    with _lock:
        if len(_variant_by_query) > _MAX_REMEMBERED:
            _variant_by_query.clear()
            _variants_by_broad_query.clear()
        for v in variants:
            _variant_by_query[f"{v['artist']} {v['title']}".strip().lower()] = v
        # a broad query was not made from one variant: what it finds may be
        # named after any of them
        for query in broad or []:
            if variants:
                _variants_by_broad_query[query.strip().lower()] = list(variants)


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


def _ask(artist: str, other_names: List[str], title: str, album: str, limit: int,
         album_limit: int = 0) -> Optional[Dict[str, List[str]]]:
    payload: Dict[str, object] = {"artist": artist, "title": title, "max_titles": limit}
    if other_names:
        payload["artist_also_known_as"] = other_names
    if album and album.casefold() != title.casefold():
        payload["album"] = album
        if album_limit:
            payload["max_albums"] = album_limit
    try:
        data = ollama.chat_json("search_terms", _SYSTEM, payload, _SCHEMA, temperature=0.1)
    except ollama.OllamaError as exc:
        logger.debug("Search-term suggestion failed for %r - %r: %s", artist, title, exc)
        return None
    banned = {n.casefold() for n in [artist, album] + other_names if n}
    titles: List[str] = []
    for item in data.get("titles") or []:
        text = " ".join(str(item or "").split())
        # "Creep (Radiohead)" is the same title with the artist glued on
        names_artist = any(n.casefold() in text.casefold() for n in [artist] + other_names
                           if n and n.casefold() not in title.casefold())
        if 2 <= len(text) <= 150 and text.casefold() != title.casefold() and not names_artist \
                and text.casefold() not in banned and text not in titles:
            titles.append(text)
    albums: List[str] = []
    not_an_album = {n.casefold() for n in [artist, album, title] + other_names if n}
    for item in (data.get("albums") or []) if album and album_limit else []:
        text = " ".join(str(item or "").split())
        if 2 <= len(text) <= 150 and text.casefold() not in not_an_album and text not in albums \
                and _distinctive(text):
            albums.append(text)
    return {"titles": titles[:limit], "albums": albums[:album_limit]}


def _distinctive(text: str) -> bool:
    """Whether ``text`` says enough to be searched without an artist next to it."""
    text = " ".join(str(text or "").split())
    if not text or _GENERIC_RE.match(text):
        return False
    try:
        from core.fork.cjk import is_only_decoration

        if is_only_decoration(text):
            return False
    except Exception as exc:
        logger.debug("decoration check failed: %s", exc)
    if contains_cjk(text):
        return sum(1 for ch in text if contains_cjk(ch)) >= 2
    return sum(1 for ch in text if ch.isalnum()) >= 6


def album_parts(album: str) -> List[str]:
    """The pieces an album name is made of, most telling first: the name without
    its bracketed notes, then each side of a colon/dash. Only pieces that could
    be searched on their own."""
    album = " ".join(str(album or "").split())
    out: List[str] = []

    def add(text: str) -> None:
        text = text.strip(" -–—:∶：|/·.,")
        if text and text.casefold() != album.casefold() and text not in out and _distinctive(text):
            out.append(text)

    bare = " ".join(_BRACKETS_RE.sub(" ", album).split())
    add(bare)
    for piece in _ALBUM_SPLIT_RE.split(bare):
        add(piece)
    return out


def _real_album(album: str, title: str) -> str:
    album = " ".join(str(album or "").split())
    if not album or album.casefold() in ("unknown album", title.casefold()):
        return ""  # a single named after its track adds nothing a title query lacks
    return album


def broaden(artist: str, title: str, album: str, variants: List[Dict[str, str]],
            albums: List[str], other_names: List[str], limit: int) -> List[str]:
    """Rungs 2-5 of the ladder (see the module docstring), broadest last."""
    if limit <= 0:
        return []
    album = _real_album(album, title)
    names = [album] + [a for a in albums if a.casefold() != album.casefold()] if album else []
    parts = [p for name in names for p in album_parts(name)]
    rungs: List[List[str]] = [[], [], [], []]
    for name in names:                                  # 2. artist + album
        rungs[0].append(f"{_pair_artist(name, artist, other_names)} {name}")
    for name in names:                                  # 3. album alone
        if _distinctive(name):
            rungs[1].append(name)
    # 4. part of the album name — and the spellings rung 3 had no room for
    rungs[2] = [p for p in parts if p not in names] + [n for n in rungs[1][_PER_RUNG:]]
    for v in variants:                                   # 5. an alternative title alone
        if v["title"].casefold() != title.casefold() and _distinctive(v["title"]):
            rungs[3].append(v["title"])
    out: List[str] = []
    seen = {f"{artist} {title}".casefold(), title.casefold()}
    for rung in rungs:
        taken = 0
        for query in rung:
            if taken >= _PER_RUNG or len(out) >= limit:
                break
            if query.casefold() not in seen:
                seen.add(query.casefold())
                out.append(query)
                taken += 1
    return out


def _plan(artist: str, title: str, album: str) -> Dict[str, Any]:
    """Cached ``{"variants", "albums", "artists"}`` for one track: what the
    model and MusicBrainz know. The queries are built from it on every call,
    so a changed limit applies without asking again."""
    limit = max(0, min(int(config.get("search_terms.max_variants") or 0), 8))
    broad = max(0, min(int(config.get("search_terms.max_broad") or 0), 12))
    empty: Dict[str, Any] = {"variants": [], "albums": [], "artists": []}
    if not title or not artist or (limit == 0 and broad == 0):
        return empty
    cache_key = "\x1f".join((_CACHE_VERSION, artist.casefold(), title.casefold(), album.casefold()))
    cached = store.get_search_terms(cache_key)
    if cached is not None:
        meta = next((c["meta"] for c in cached if isinstance(c, dict) and isinstance(c.get("meta"), dict)), {})
        return {"variants": [c for c in cached if isinstance(c, dict) and "title" in c][:limit],
                "albums": list(meta.get("albums") or []), "artists": list(meta.get("artists") or [])}

    # Latin names first, folded to ASCII: file names rarely keep "ō" or "é".
    other_names = []
    for name in sorted(artist_names.known_names(artist), key=contains_cjk):
        name = name if contains_cjk(name) else _ascii(name)
        if name and name.casefold() != artist.casefold() and name not in other_names:
            other_names.append(name)
    # always ask for the full number: the cache outlives the current limits
    answer = _ask(artist, other_names, title, album, 8, 3 if _real_album(album, title) else 0)
    titles = answer["titles"] if answer is not None else None

    variants: List[Dict[str, str]] = []
    seen = {f"{artist} {title}".casefold()}

    def add(v_artist: str, v_title: str) -> None:
        key = f"{v_artist} {v_title}".casefold()
        if key not in seen:
            seen.add(key)
            variants.append({"artist": v_artist, "title": v_title})

    # Model titles first, each under the artist name in the matching script…
    for v_title in (titles or [])[:max(limit, 1)]:
        add(_pair_artist(v_title, artist, other_names), v_title)
    # …then the original title under the artist's other names.
    # A CJK alias in front of a Latin title is not how anyone names a file.
    for name in other_names:
        if contains_cjk(title) or not contains_cjk(name):
            add(name, title)
    for v_title in (titles or [])[max(limit, 1):]:
        add(_pair_artist(v_title, artist, other_names), v_title)

    albums = answer["albums"] if answer is not None else []
    if answer is not None:
        # a model outage is not cached: the next search asks again
        store.save_search_terms(cache_key, variants + [{"meta": {"albums": albums, "artists": other_names}}],
                                model=config.model_for("search_terms"))
    if variants[:limit]:
        logger.info("Search variants for %r - %r: %s", artist, title, variants[:limit])
    return {"variants": variants[:limit], "albums": albums, "artists": other_names}


def suggest(artist: str, title: str, album: str = "") -> List[Dict[str, str]]:
    """List of ``{"artist", "title"}`` variants for one track."""
    return _plan(artist, title, album)["variants"]


def augment_queries(track: Any, queries: List[str]) -> List[str]:
    if not config.get("search_terms.enabled"):
        return queries
    title = str(getattr(track, "name", "") or "").strip()
    artist = _first_artist(track)
    if not title or not artist:
        # Upstream withholds unqualified queries for artist-less tracks; a
        # model guess without an artist to anchor it is no better.
        return queries
    album = _album(track)
    plan = _plan(artist, title, album)
    variants = plan["variants"]
    broad = broaden(artist, title, album, variants, plan["albums"], plan["artists"],
                    max(0, min(int(config.get("search_terms.max_broad") or 0), 12)))
    if not variants and not broad:
        return queries
    _remember(variants, broad)
    out = list(queries)
    seen = {q.lower() for q in out if q}
    for query in [f"{v['artist']} {v['title']}".strip() for v in variants] + broad:
        if query.lower() not in seen:
            out.append(query)
            seen.add(query.lower())
    if broad:
        logger.info("Broader searches for %r - %r: %s", artist, title, broad)
    return out


def alternate_tracks(track: Any, query: Optional[str]) -> List[Any]:
    """Copies of ``track`` under each name a result of ``query`` may carry:
    the one variant behind a variant query, every variant for a broad one."""
    if not query or not config.get("search_terms.match_variants"):
        return []
    key = str(query).strip().lower()
    with _lock:
        variants = [_variant_by_query[key]] if key in _variant_by_query else list(
            _variants_by_broad_query.get(key) or [])
    return [alt for alt in (_renamed(track, v) for v in variants) if alt is not None]


def alternate_track(track: Any, query: Optional[str]) -> Optional[Any]:
    """A copy of ``track`` renamed to the variant behind ``query``, or None
    when the query is not an LLM suggestion."""
    if not query or not config.get("search_terms.match_variants"):
        return None
    with _lock:
        variant = _variant_by_query.get(str(query).strip().lower())
    if not variant:
        return None
    return _renamed(track, variant)


def _renamed(track: Any, variant: Dict[str, str]) -> Optional[Any]:
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
        logger.debug("Could not build alternate track for %r: %s", variant, exc)
        return None
