"""The only functions upstream code calls into the fork.

Every hook is fail-safe: any exception is logged and the upstream value is
returned unchanged, so a fork bug (or a stopped Ollama container) can never
break a download or an import.
"""

from __future__ import annotations

import os
import threading
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


def first_artist_folder(result: Any, context: Any, template_type: str = "album_path") -> Any:
    """An album by several album artists is filed under the first one: the
    artist-level folder (the first folder of the path, when it is the album
    artist and nothing else) is named after the first album artist only. The
    album folder and the file name keep the full credit."""
    if not _active():
        return result
    try:
        from core.fork import artist_format, config
        from core.imports.paths import sanitize_filename

        if template_type not in ("album_path", "single_path") or not isinstance(context, dict) \
                or not config.get("paths.first_album_artist_folder"):
            return result
        folder, name = result
        credit = context.get("albumartist") or context.get("artist")
        if not folder or not isinstance(credit, str):
            return result
        names = artist_format.split_credit(credit)
        head, sep, rest = folder.partition(os.sep)
        # values are cleaned once as template values and once as a folder name
        if len(names) < 2 or head != sanitize_filename(sanitize_filename(credit)):
            return result
        first = sanitize_filename(sanitize_filename(names[0]))
        return (first + sep + rest, name) if first else result
    except Exception as exc:
        logger.warning("first_artist_folder failed: %s", exc)
        return result


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


def completion_from_library_analysis(db: Any, result: Any, album: Any, artist_name: Any, source: Any = None) -> Any:
    """Discography card: the status the album pop-up's library analysis gives."""
    if not _active():
        return result
    try:
        from core.fork import album_tagging

        return album_tagging.completion_from_analysis(
            db, result, album if isinstance(album, dict) else {}, str(artist_name or ""), source or "")
    except Exception as exc:
        logger.debug("completion_from_library_analysis failed: %s", exc)
        return result


def keep_both_versions(job: Any, context: Any, track_a: Any, track_b: Any) -> bool:
    """Duplicate Detector / Single-Album Dedup: two look-alike tracks that are
    both wanted (different versions of a song, or one sits on a release that
    carries a version the other release lacks). The index is built once per
    scan and kept on the job."""
    if not _active():
        return False
    try:
        from core.fork import single_merge

        index = getattr(job, "_fork_release_index", None)
        if index is None:
            index = job._fork_release_index = single_merge.ReleaseIndex(context.db)
        return index.keep_both(track_a or {}, track_b or {})
    except Exception as exc:
        logger.debug("keep_both_versions failed: %s", exc)
        return False


def scan_single_merges(job: Any, context: Any, result: Any) -> None:
    """Single/Album Dedup: also report singles an album is missing."""
    if not _active():
        return
    try:
        from core.fork import jobs

        jobs.scan_single_merges(job, context, result)
    except Exception as exc:
        logger.warning("scan_single_merges failed: %s", exc)


def import_copy_verify(src: Any, dst: Any) -> bool:
    """A file leaving the import folder: copy, verify, delete the original
    and the folders left empty. True when handled here. A copy that fails or
    does not match RAISES (the original is intact): that must fail the import
    rather than fall back to a plain move."""
    if not _active():
        return False
    try:
        from core.fork import import_move

        root = import_move.applies(src)
    except Exception as exc:
        logger.debug("import_copy_verify not applied: %s", exc)
        return False
    if not root:
        return False
    import_move.copy_verify_delete(src, dst, root)
    return True


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
        # the backup may have been the last thing in an import sub-folder
        from core.fork import config, import_move

        root = import_move.staging_root() if config.get("import.copy_verify") else None
        folder = os.path.dirname(os.path.realpath(str(src_audio)))
        if root and folder.startswith(root.rstrip(os.sep) + os.sep):
            import_move.remove_empty_parents(folder, root)
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


# ── 22 filler tools: scan cache ─────────────────────────────────────────

def filler_cache(context: Any, job_id: str) -> Any:
    """A ``with`` block during which a filler's scan is answered from its cache."""
    import contextlib

    if not _active():
        return contextlib.nullcontext()
    try:
        from core.fork import filler_cache as cache

        manager = getattr(context, "config_manager", None)
        days = manager.get(f"repair.jobs.{job_id}.settings.cache_days", cache.DEFAULT_DAYS) if manager \
            else cache.DEFAULT_DAYS
        return cache.session(job_id, days)
    except Exception as exc:
        logger.warning("filler cache hook failed: %s", exc)
        return contextlib.nullcontext()


def filler_file_value(path: Any, read: Any) -> Any:
    if not _active():
        return read(path)
    from core.fork import filler_cache as cache

    return cache.file_value(path, read)


def filler_lookup(group: str, fetch: Any, *args: Any) -> Any:
    if not _active():
        return fetch(*args)
    from core.fork import filler_cache as cache

    return cache.lookup(group, fetch, *args)


# ── 29 import preview: one album-folder lookup per album ────────────────

_preview_memo = threading.local()


def album_preview_scope() -> Any:
    """A ``with`` block for one import preview. Inside it, the "does this album
    already have a folder" lookup is answered once per album instead of once
    per track: on an album the library does not have, every miss runs the
    whole fuzzy library search again, minutes of CPU for a long track list."""
    import contextlib

    @contextlib.contextmanager
    def _scope():
        previous = getattr(_preview_memo, "answers", None)
        _preview_memo.answers = {}
        try:
            yield
        finally:
            _preview_memo.answers = previous

    return _scope() if _active() else contextlib.nullcontext()


_ALBUM_FOLDER_KEY = ("transfer_dir", "album_name", "album_artist", "spotify_album_id", "active_server",
                     "expected_track_count", "musicbrainz_release_id", "disambiguation",
                     "incoming_album_type")


def album_folder_lookup(resolve: Callable[..., Any], kwargs: Dict[str, Any]) -> Any:
    """``resolve(**kwargs)``, remembered for the rest of the preview it runs in.
    Outside a preview (a real import) nothing is remembered."""
    answers = getattr(_preview_memo, "answers", None)
    if answers is None:
        return resolve(**kwargs)
    try:
        key = tuple(str(kwargs.get(name) or "") for name in _ALBUM_FOLDER_KEY)
    except Exception:  # noqa: BLE001 - an odd argument only costs the shortcut
        return resolve(**kwargs)
    if key not in answers:
        answers[key] = resolve(**kwargs)
    return answers[key]


# ── 25 Downloads: pause all ─────────────────────────────────────────────

def downloads_held(batch_id: str, start: Callable[[str], None]) -> bool:
    """Whether the batch must wait because downloads are paused."""
    if not _active():
        return False
    try:
        from core.fork import download_pause

        return download_pause.hold(batch_id, start)
    except Exception as exc:
        logger.warning("download pause hook failed: %s", exc)
        return False


# ── 26 YouTube: back off when refused ───────────────────────────────────

def youtube_watch(client_logger: Any) -> None:
    try:
        from core.fork import youtube_gate

        youtube_gate.watch(client_logger)
    except Exception as exc:
        logger.warning("YouTube gate not installed: %s", exc)


def youtube_blocked() -> bool:
    """Whether YouTube is refusing this address and must be left alone."""
    if not _active():
        return False
    try:
        from core.fork import youtube_gate

        return youtube_gate.blocked()
    except Exception as exc:
        logger.debug("YouTube gate check failed: %s", exc)
        return False
