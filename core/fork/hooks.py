"""The only functions upstream code calls into the fork.

Every hook is fail-safe: any exception is logged and the upstream value is
returned unchanged, so a fork bug (or a stopped Ollama container) can never
break a download or an import.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from utils.logging_config import get_logger

logger = get_logger("fork.hooks")

RENAME_ONLY_KEY = "_fork_rename_only"


def _active() -> bool:
    """False under pytest unless a fork test opts in, so upstream's own test
    suite never reaches Ollama or MusicBrainz through a hook."""
    if os.environ.get("SOULSYNC_FORK_DISABLE") == "1":
        return False
    return "PYTEST_CURRENT_TEST" not in os.environ or os.environ.get("SOULSYNC_FORK_TESTING") == "1"
_PATH_TEMPLATES = ("album_path", "single_path", "compilation_path", "playlist_path")


# ── 1.1 search terms ────────────────────────────────────────────────────

def augment_search_queries(track: Any, queries: List[str]) -> List[str]:
    if not _active():
        return queries
    try:
        from core.fork import search_terms

        return search_terms.augment_queries(track, queries)
    except Exception as exc:
        logger.warning("augment_search_queries failed: %s", exc)
        return queries


def rescue_candidates(accepted: list, results: list, track: Any, query: Optional[str],
                      select: Callable[..., list], *args: Any) -> list:
    """When nothing matched the original names and ``query`` was an LLM
    variant, score the same results against that variant instead."""
    if accepted or not results or not _active():
        return accepted
    try:
        from core.fork import search_terms

        for alt in search_terms.alternate_tracks(track, query):
            rescued = select(results, alt, None, *args)
            if rescued:
                logger.info("Accepted %s candidate(s) via search variant %r", len(rescued), query)
                return rescued
        return accepted
    except Exception as exc:
        logger.warning("rescue_candidates failed: %s", exc)
        return accepted


# ── 1.2 / 2 names in paths and tags ─────────────────────────────────────

def transform_template_context(context: Dict[str, Any], template_type: str = "album_path") -> Dict[str, Any]:
    """Artist rules + translated names for a path-template context."""
    if not _active():
        return context
    try:
        from core.fork import artist_names, config, translate

        if template_type not in _PATH_TEMPLATES or not isinstance(context, dict) \
                or not config.get("translate.apply_to_paths"):
            return context
        out = dict(context)
        for field in ("artist", "albumartist"):
            if isinstance(out.get(field), str):
                out[field] = artist_names.resolve_credit(out[field])
        artists_list = out.get("_artists_list")
        if isinstance(artists_list, (list, tuple)):
            mapped = []
            for entry in artists_list:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    mapped.append({**entry, "name": artist_names.resolve(entry["name"])})
                elif isinstance(entry, str):
                    mapped.append(artist_names.resolve(entry))
                else:
                    mapped.append(entry)
            out["_artists_list"] = mapped
        hint = {"artist": str(context.get("artist") or ""), "album": str(context.get("album") or "")}
        if isinstance(out.get("album"), str) and translate.enabled("album"):
            out["album"] = translate.translate_name("album", out["album"], {"artist": hint["artist"]})
        if isinstance(out.get("title"), str) and translate.enabled("title"):
            out["title"] = translate.translate_name("title", out["title"], hint)
        return out
    except Exception as exc:
        logger.warning("transform_template_context failed: %s", exc)
        return context


def after_metadata_enhanced(file_path: str, context: Optional[Dict[str, Any]] = None) -> None:
    try:
        if is_rename_only(context) or not _active():
            return
        from core.fork import tags

        tags.apply_to_file(file_path)
        try:
            from core.fork import album_identity

            final = context.get("_final_processed_path") if isinstance(context, dict) else None
            album_identity.harmonize(file_path, final)
        except Exception as exc:
            logger.warning("album id harmonize failed for %s: %s", file_path, exc)
    except Exception as exc:
        logger.warning("after_metadata_enhanced failed for %s: %s", file_path, exc)


# ── 1.3 lyrics ──────────────────────────────────────────────────────────

def after_lyrics(file_path: str, track_name: str = "", artist_name: str = "") -> None:
    if not _active():
        return
    try:
        from core.fork import lyrics

        embedded = lyrics.translate_sidecar(file_path, track_name, artist_name)
        if embedded:
            from core.lyrics_client import lyrics_client

            lyrics_client._embed_lyrics(file_path, embedded.strip())
    except Exception as exc:
        logger.warning("after_lyrics failed for %s: %s", file_path, exc)


# ── 4 rename-only import ────────────────────────────────────────────────

def is_rename_only(context: Any) -> bool:
    return isinstance(context, dict) and bool(context.get(RENAME_ONLY_KEY))


def auto_import_rename_only() -> bool:
    """Whether the automatic import watcher files tracks rename-only."""
    if not _active():
        return False
    try:
        from core.fork import config

        return bool(config.get("import.rename_only_auto"))
    except Exception:
        return False


def mark_rename_only(context: Any, requested: Any) -> None:
    """Flag an import context so the pipeline files the track without
    rewriting any of its metadata."""
    if isinstance(context, dict) and requested:
        context[RENAME_ONLY_KEY] = True
        # The user asked for the file to be filed as-is: checks that exist to
        # judge a download must not quarantine it. Integrity still runs.
        skip = list(context.get("_skip_quarantine_check") or [])
        for check in ("quality", "bit_depth", "acoustid", "silence"):
            if check not in skip:
                skip.append(check)
        context["_skip_quarantine_check"] = skip


# ── ownership: library names differ from source names ───────────────────

def ownership_retry(db: Any, kind: str, name: str, artist: str, threshold: float,
                    attempt: Callable[[str, str], Any], first: Any) -> Any:
    if not _active():
        return first
    try:
        from core.fork import ownership

        return ownership.retry_check(db, kind, name, artist, threshold, attempt, first)
    except Exception as exc:
        logger.warning("ownership_retry failed: %s", exc)
        return first


def owned_by_external_id(db: Any, track: Any, server_source: Optional[str] = None) -> Any:
    """Library track sharing an external id with ``track``, or None."""
    if not _active():
        return None
    try:
        from core.fork import ownership

        return ownership.find_by_external_id(db, track, server_source)
    except Exception as exc:
        logger.debug("owned_by_external_id failed: %s", exc)
        return None


def completion_with_saved_folder(result: Any, album: Any, artist_name: Any, source: Any = None) -> Any:
    """Discography card: count the folder the user picked for the album."""
    if not _active():
        return result
    try:
        from core.fork import album_tagging

        return album_tagging.completion_from_saved_folder(
            result, album if isinstance(album, dict) else {}, str(artist_name or ""), source or "")
    except Exception as exc:
        logger.debug("completion_with_saved_folder failed: %s", exc)
        return result


def incomplete_album_covered(details: Any) -> str:
    """Album Completeness: the saved folder that holds the whole album, or ""."""
    if not _active() or not isinstance(details, dict):
        return ""
    try:
        from core.fork import album_tagging

        return album_tagging.finding_covered_by_saved_folder(details)
    except Exception as exc:
        logger.debug("incomplete_album_covered failed: %s", exc)
        return ""


# ── lyrics backups (<name>.original.lrc) follow their track ─────────────
# Deliberately NOT gated on _active(): these only ever act on files the fork
# itself created, and must keep working if the feature is later switched off.

def move_lyrics_backup(src_audio: Any, dst_audio: Any, with_partner: bool = False) -> None:
    try:
        if not src_audio or not dst_audio:
            return
        from core.fork import lyrics

        lyrics.move_backups(str(src_audio), str(dst_audio), with_partner)
    except Exception as exc:
        logger.warning("move_lyrics_backup failed for %s: %s", src_audio, exc)


def remove_lyrics_backup(audio_path: Any) -> None:
    try:
        if audio_path:
            from core.fork import lyrics

            lyrics.remove_backups(str(audio_path))
    except Exception as exc:
        logger.debug("remove_lyrics_backup failed for %s: %s", audio_path, exc)


def same_identity(db_track: Any, track_name: str, artist_name: str, album: Any, require_album: bool,
                  same_title: Callable[..., bool], same_artist: Callable[[str, Any], bool]) -> bool:
    """Fallback for upstream's strict identity check when the library row
    carries fork-written names."""
    if not _active():
        return False
    try:
        from core.fork import ownership

        return ownership.same_identity(db_track, track_name, artist_name, album, require_album,
                                       same_title, same_artist)
    except Exception as exc:
        logger.warning("same_identity failed: %s", exc)
        return False


# ── Comma Artist Splitter: write the split the configured way ───────────

def comma_split_before(worker: Any, details: Any) -> Any:
    """Files whose ALBUM artist is the combined string, noted before
    upstream's fix replaces it with the first artist only."""
    if not _active():
        return set()
    try:
        from core.fork import comma_split

        return comma_split.album_artist_files(worker, details)
    except Exception as exc:
        logger.debug("comma_split_before failed: %s", exc)
        return set()


def comma_split_after(worker: Any, details: Any, before: Any, result: Any) -> Any:
    if not _active():
        return result
    try:
        if not isinstance(result, dict) or not result.get("success") \
                or result.get("action") != "artists_split":
            return result
        from core.fork import comma_split

        return comma_split.apply_strategy(worker, details, before or set(), result)
    except Exception as exc:
        logger.warning("comma_split_after failed: %s", exc)
        return result


# ── YouTube video -> audio in the import folder ─────────────────────────

def after_youtube_download(result: Any, dl: Any) -> None:
    if not _active():
        return
    try:
        if not isinstance(result, dict) or result.get("status") != "completed" or not result.get("dest_path"):
            return
        from core.fork import youtube_audio
        from core.video.youtube_download import youtube_fields_from_download

        youtube_audio.after_download(result["dest_path"], youtube_fields_from_download(dl or {}))
    except Exception as exc:
        logger.warning("after_youtube_download failed: %s", exc)
