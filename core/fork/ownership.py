""""Do I already own this?" for a library whose names the fork rewrote.

Metadata sources report a track as 周杰倫 / 夜曲 while the library — tagged by
this fork, or earlier by translate-music-library — holds Jay Chou /
"Nocturne (夜曲)". Upstream decides ownership by fuzzy-comparing those
strings, which fails across scripts and re-downloads the track forever.

Three checks, strongest first, none of them fuzzy:

1. **External IDs.** Every SoulSync download embeds its Spotify / Deezer /
   iTunes / MusicBrainz / ISRC ids, and the library rows carry them. A source
   track whose id is on a library row is owned, whatever either side is
   called. (Upstream already has this lookup; it only used it in the
   watchlist scanner.)
2. **The fork's own records.** ``fork_translations`` and ``fork_artist_names``
   say exactly which name was written for which original, so the source names
   are mapped to the library's names and upstream's check is asked again.
3. **The original inside the library name.** A library title of the form
   "<anything> (夜曲)" names the source title 夜曲 exactly; that covers files
   translated before the fork existed, with any wording.

Layers 2 and 3 only produce *candidate names*; the final answer still comes
from upstream's own check run on those names, so its other rules (editions,
server scope, thresholds) keep applying.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.fork import artist_names, store, translate
from core.fork.cjk import contains_cjk, split_name
from utils.logging_config import get_logger

logger = get_logger("fork.ownership")

_MAX_ATTEMPTS = 6
_LIKE_LIMIT = 25


def _norm(text: Any) -> str:
    return "".join(str(text or "").casefold().split())


def artist_forms(artist: str) -> List[str]:
    """Every name the library may hold for ``artist``: itself, its tagging
    rule, and its MusicBrainz names (cached; one lookup per new CJK artist)."""
    forms: List[str] = []

    def add(name: Any) -> None:
        if isinstance(name, str) and name.strip() and _norm(name) not in {_norm(f) for f in forms}:
            forms.append(name.strip())

    add(artist)
    if not artist:
        return forms
    try:
        add(artist_names.resolve_credit(artist))
        cached = store.get_artist_aliases(artist.strip())
        for alias in (cached or {}).get("aliases") or []:
            add(alias)
            add(artist_names.resolve(alias, allow_lookup=False))
    except Exception as exc:
        logger.debug("artist_forms(%r) failed: %s", artist, exc)
    return forms


def _like_rows(db: Any, table: str, core: str) -> List[Tuple[str, str]]:
    """``(title, artist name)`` of library rows whose title contains ``core``."""
    if table not in ("tracks", "albums"):
        return []
    pattern = "%" + core.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    conn = None
    try:
        conn = db._get_connection()
        rows = conn.execute(
            f"SELECT DISTINCT t.title, a.name FROM {table} t JOIN artists a ON a.id = t.artist_id "  # noqa: S608 — table is whitelisted above
            "WHERE t.title LIKE ? ESCAPE '\\' LIMIT ?",
            (pattern, _LIKE_LIMIT),
        ).fetchall()
        return [(str(r[0] or ""), str(r[1] or "")) for r in rows]
    except Exception as exc:
        logger.debug("library LIKE lookup failed: %s", exc)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: S110
                pass


def library_names(db: Any, kind: str, name: str, artist: str) -> List[Tuple[str, str]]:
    """``(library title, library artist)`` pairs the source ``name`` by
    ``artist`` may be stored under, best first. Never contains the input
    pair itself. ``kind`` is ``title`` or ``album``."""
    forms = artist_forms(artist)
    pairs: List[Tuple[str, str]] = []
    seen = {(_norm(name), _norm(artist))}

    def add(title: str, by: str) -> None:
        key = (_norm(title), _norm(by))
        if title and key not in seen:
            seen.add(key)
            pairs.append((title, by))

    written = name
    if isinstance(name, str) and contains_cjk(name):
        try:
            written = translate.translate_name(kind, name, allow_llm=False)  # layer 2: recorded names only
        except Exception as exc:
            logger.debug("recorded name lookup failed for %r: %s", name, exc)
    for form in forms[1:]:
        add(written, form)
    if written != name:
        add(written, artist)
    for form in forms[1:]:
        add(name, form)

    if isinstance(name, str) and contains_cjk(name):  # layer 3: original embedded in the library name
        core = split_name(name)[0]
        if core and len(core) >= 2:
            wanted = {_norm(f) for f in forms}
            for lib_title, lib_artist in _like_rows(db, "tracks" if kind == "title" else "albums", core):
                lib_core, lib_existing, _suffix = split_name(lib_title)
                if _norm(lib_core) != _norm(core) or not lib_existing:
                    continue  # not "<translation> (<this original>)"
                if _norm(lib_artist) in wanted or any(_norm(p) in wanted for p in artist_names._CREDIT_SPLIT_RE.split(lib_artist)[::2]):
                    add(lib_title, lib_artist)
    return pairs[:_MAX_ATTEMPTS]


def _involves_fork_names(*values: Any) -> bool:
    return any(isinstance(v, str) and contains_cjk(v) for v in values)


def retry_check(db: Any, kind: str, name: str, artist: str, threshold: float,
                attempt: Callable[[str, str], Tuple[Any, float]],
                first: Tuple[Any, float]) -> Tuple[Any, float]:
    """Re-run an upstream ownership check under the library's names when the
    check with the source's names found nothing."""
    match, confidence = first[0], first[1]
    if match is not None and (confidence or 0) >= threshold:
        return first
    has_rule = False
    try:
        has_rule = bool(artist) and (
            artist_names.resolve_credit(artist, allow_lookup=False) != artist
            or bool((store.get_artist_aliases(artist.strip()) or {}).get("aliases")))
    except Exception:
        has_rule = False
    if not has_rule and not _involves_fork_names(name, artist):
        return first
    for lib_name, lib_artist in library_names(db, kind, name, artist):
        try:
            result = attempt(lib_name, lib_artist)
        except Exception as exc:
            logger.debug("ownership retry failed for %r / %r: %s", lib_name, lib_artist, exc)
            continue
        if result and result[0] is not None and (result[1] or 0) >= threshold:
            logger.info("Owned under library name: %r by %r -> %r by %r", name, artist, lib_name, lib_artist)
            return result
    return first


def find_by_external_id(db: Any, track: Any, server_source: Optional[str] = None) -> Optional[SimpleNamespace]:
    """Library row sharing an external id with the source ``track`` (layer 1)."""
    from core.library.track_identity import extract_external_ids, find_library_track_by_external_id

    hint = None
    try:
        from core.metadata.registry import get_primary_source

        hint = get_primary_source()
    except Exception:
        hint = None
    ids: Dict[str, str] = extract_external_ids(track, source_hint=hint)
    if not ids:
        return None
    row = find_library_track_by_external_id(db, external_ids=ids, server_source=server_source)
    if row is None:
        return None
    logger.info("Owned by external id (%s): %r", ", ".join(sorted(ids)), row.get("title"))
    return SimpleNamespace(**{k: row.get(k) for k in ("id", "title", "file_path", "album_id", "artist_id")})
