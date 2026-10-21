"""Search page videos are downloaded as audio, filed into the music library.

A click on a video in the Search page's music-video grid used to fetch the
whole video into the music videos folder. Now it fetches only the video's
audio stream and files it like any other downloaded track:

1. who and what it is: the strict metadata match the music-video path already
   uses (both artist and title must agree, a live/cover cut never borrows the
   studio name); without a confident match the name parsed off the video.
2. its subtitles, when the uploader published some (not YouTube's automatic
   captions, which are speech recognition and useless for lyrics), turned
   into LRC. They become the track's ``.lrc`` sidecar instead of LRClib's.
3. one pinned task in its own batch, ``youtube`` / ``<video id>||<title>``,
   the same handoff a basic-search YouTube pick makes. The music pipeline
   downloads it, tags it, files it with the library's path templates and
   shows it on the Downloads page.

The audio is extracted, never re-encoded: whatever Settings → YouTube says
about transcoding, these downloads take the best original stream (Opus, else
AAC) and remux it. See ``extract_only`` and the wrappers in
``core/youtube_client.py``.
"""

from __future__ import annotations

import html
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.logging_config import get_logger

logger = get_logger("fork.search_video_audio")

# what the search page's poller understands: completed / error, else running
_TERMINAL_OK = ("completed",)
_TERMINAL_BAD = ("failed", "not_found", "cancelled", "error")

state: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# video ids whose download must remux the original stream, never transcode
_extract_only: set = set()


# ── subtitles -> LRC ────────────────────────────────────────────────────

_CUE_TIME = re.compile(
    r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->")
_TAG = re.compile(r"<[^>]*>")
_NOTES = "♪♫♬🎵🎶"


def _lrc_stamp(seconds: float) -> str:
    centis = int(round(seconds * 100))
    minutes, centis = divmod(centis, 6000)
    return f"[{minutes:02d}:{centis // 100:02d}.{centis % 100:02d}]"


def vtt_to_lrc(vtt: str) -> str:
    """WebVTT (or SRT) cues as LRC lines: the cue's start time, its text on
    one line. Styling tags and music-note decoration are dropped, and a line
    repeated by the next cue (karaoke-style rolling captions) is kept once.
    '' when nothing with words is left."""
    lines: List[str] = []
    last = None
    blocks = re.split(r"\r?\n\s*\r?\n", (vtt or "").replace("﻿", ""))
    for block in blocks:
        rows = block.splitlines()
        for i, row in enumerate(rows):
            m = _CUE_TIME.match(row)
            if not m:
                continue
            hours, minutes, secs, frac = m.groups()
            start = (int(hours or 0) * 3600 + int(minutes) * 60 + int(secs)
                     + int(frac.ljust(3, "0")) / 1000)
            text = " ".join(html.unescape(_TAG.sub("", r)).strip() for r in rows[i + 1:])
            text = " ".join(text.strip(_NOTES + " ").split())
            if text and text != last:
                lines.append(_lrc_stamp(start) + text)
                last = text
            break
    return "\n".join(lines) + "\n" if lines else ""


def pick_subtitle_language(subtitles: Dict[str, Any], language: str = "") -> str:
    """The uploader's subtitle track to use: the video's own language when
    there is one in it, else the first one listed. '' when there is none."""
    langs = [k for k, v in (subtitles or {}).items() if v and k != "live_chat"]
    if not langs:
        return ""
    language = (language or "").lower()
    if language:
        for lang in langs:
            if lang.lower() == language:
                return lang
        for lang in langs:
            if lang.lower().split("-")[0] == language.split("-")[0]:
                return lang
    return langs[0]


# ── the job ─────────────────────────────────────────────────────────────

@dataclass
class Deps:
    # (query, limit) -> metadata Track objects (name, artists, album, ...)
    search_tracks: Callable[[str, int], List[Any]]
    # video url -> (subtitle text in WebVTT or '', duration in seconds or 0)
    fetch_subtitles: Callable[[str], Tuple[str, int]]
    # (task info, candidate, batch name, profile id) -> (batch_id, task_id)
    queue: Callable[[Dict[str, Any], Dict[str, Any], str, Any], Tuple[str, str]]
    # task id -> {'status', 'download_id', 'error_message'} or None
    task: Callable[[str], Optional[Dict[str, Any]]]
    # download id -> percent, or None
    progress: Callable[[str], Optional[float]]
    add_activity: Callable[[str, str, str], None] = lambda *_: None
    start_thread: Optional[Callable[[Callable[[], None], str], None]] = None


def extract_only(video_id: str) -> bool:
    with _lock:
        return str(video_id or "") in _extract_only


def _match(raw_title: str, raw_channel: str, deps: Deps) -> Tuple[str, str, Any]:
    """(artist, title, metadata track or None)."""
    from core.downloads import music_video

    artist, title = music_video.parse_artist_title(raw_title, raw_channel)
    try:
        results = deps.search_tracks(f"{artist} {title}".strip(), 8) or []
        match = music_video.pick_match(results, artist, title, raw_title)
        if match:
            for r in results:
                artists = [a for a in (getattr(r, "artists", None) or []) if a]
                if getattr(r, "name", "") == match.title and artists and artists[0] == match.artist:
                    logger.info("[Video Audio] Matched to: %s - %s (%.2f)", match.artist, match.title, match.score)
                    return match.artist, match.title, r
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Video Audio] Metadata lookup failed: %s", exc)
    logger.info("[Video Audio] No confident match, filing as parsed: %s - %s", artist, title)
    return artist, title, None


def track_info(artist: str, title: str, track: Any, duration_s: int, lrc: str) -> Dict[str, Any]:
    """What the pinned task is tagged and filed by. The length is the video's,
    not the matched recording's: a video with an intro is longer, and the
    import's length check must not quarantine it for that."""
    info: Dict[str, Any] = {
        "id": "",
        "name": title,
        "artists": [{"name": artist}] if artist else [],
        "album": {},
        "duration_ms": int(duration_s or 0) * 1000,
        "track_number": 0,
    }
    if track is not None:
        info["id"] = str(getattr(track, "id", "") or "")
        info["artists"] = [{"name": a} for a in (getattr(track, "artists", None) or []) if a] or info["artists"]
        album = str(getattr(track, "album", "") or "")
        if album:
            info["album"] = {
                "name": album,
                "release_date": str(getattr(track, "release_date", "") or ""),
                "image_url": getattr(track, "image_url", None),
                "album_type": getattr(track, "album_type", None) or "album",
                "total_tracks": getattr(track, "total_tracks", None) or 0,
            }
        info["track_number"] = getattr(track, "track_number", None) or 0
        if getattr(track, "disc_number", None):
            info["disc_number"] = track.disc_number
        if not info["duration_ms"]:
            info["duration_ms"] = int(getattr(track, "duration_ms", 0) or 0)
    if lrc:
        info["_youtube_lrc"] = lrc
    return info


def start(video: Dict[str, Any], profile_id: Any, deps: Deps) -> Dict[str, Any]:
    """Match the video and queue its audio in the background.
    {'success', 'video_id'} or {'error', 'code'}."""
    video_id = str(video.get("video_id") or "").strip()
    if not video_id or not video.get("url"):
        return {"error": "Missing video_id or url", "code": 400}
    with _lock:
        current = state.get(video_id) or {}
        # a double click must not queue it twice; a finished or failed one may go again
        if current.get("status") == "matching" or (
                current.get("task_id") and _task_status(current, deps) == "running"):
            return {"error": "Already downloading", "code": 409}
        state[video_id] = {"status": "matching", "task_id": None, "error": None}
        _extract_only.add(video_id)
    run_thread = deps.start_thread or (
        lambda fn, name: threading.Thread(target=fn, daemon=True, name=name).start())
    run_thread(lambda: run(video_id, video, profile_id, deps), f"video-audio-{video_id}")
    return {"success": True, "video_id": video_id}


def run(video_id: str, video: Dict[str, Any], profile_id: Any, deps: Deps) -> None:
    raw_title = str(video.get("title") or "")
    raw_channel = str(video.get("channel") or "")
    url = str(video.get("url") or "")
    try:
        artist, title, track = _match(raw_title, raw_channel, deps)
        lrc, duration = "", 0
        try:
            vtt, duration = deps.fetch_subtitles(url)
            lrc = vtt_to_lrc(vtt) if vtt else ""
            if lrc:
                logger.info("[Video Audio] Subtitles found for %s, kept as lyrics", video_id)
        except Exception as exc:  # noqa: BLE001 - lyrics are a bonus, never a reason to fail
            logger.debug("[Video Audio] subtitle fetch failed for %s: %s", video_id, exc)
        if not duration:
            try:
                duration = int(float(video.get("duration") or 0))
            except (TypeError, ValueError):
                duration = 0
        info = track_info(artist, title, track, duration, lrc)
        candidate = {
            "username": "youtube",
            "filename": f"{video_id}||{raw_title or title}",
            "size": 0,
            "duration": duration,
            "quality": "",
            "artist": artist,
            "title": title,
            "album": (info.get("album") or {}).get("name", ""),
        }
        name = f"{artist} - {title}" if artist and title else (raw_title or "YouTube video")
        _batch_id, task_id = deps.queue(info, candidate, name, profile_id)
        with _lock:
            state[video_id] = {"status": "queued", "task_id": task_id, "error": None}
        deps.add_activity("", "Video Audio Download Started", name)
    except Exception as exc:  # noqa: BLE001 - a card left 'matching' would spin forever
        logger.error("[Video Audio] %s: %s", video_id, exc)
        with _lock:
            state[video_id] = {"status": "error", "task_id": None, "error": str(exc)}
            _extract_only.discard(video_id)


def _task_status(entry: Dict[str, Any], deps: Deps) -> str:
    task = deps.task(entry["task_id"]) or {}
    status = str(task.get("status") or "")
    if status in _TERMINAL_OK:
        return "completed"
    if status in _TERMINAL_BAD or not task:
        return "error"
    return "running"


def status(video_id: str, deps: Deps) -> Optional[Dict[str, Any]]:
    """The search page's poll answer, or None when this module never saw the
    video (the music-video path may have)."""
    with _lock:
        entry = dict(state.get(video_id) or {})
    if not entry:
        return None
    if entry.get("status") == "error":
        return {"status": "error", "progress": 0, "error": entry.get("error")}
    if not entry.get("task_id"):
        return {"status": "downloading", "progress": 0}
    task = deps.task(entry["task_id"]) or {}
    outcome = _task_status(entry, deps)
    if outcome != "running":
        with _lock:
            _extract_only.discard(video_id)
        if outcome == "completed":
            return {"status": "completed", "progress": 100}
        return {"status": "error", "progress": 0,
                "error": task.get("error_message") or task.get("status") or "gone"}
    percent = None
    if task.get("download_id"):
        try:
            percent = deps.progress(task["download_id"])
        except Exception:  # noqa: BLE001
            percent = None
    if task.get("status") == "post_processing":
        percent = 99
    return {"status": "downloading", "progress": round(float(percent or 0), 1)}


# ── the .lrc, when the import files the track ───────────────────────────

def write_lyrics(file_path: str, context: Any) -> bool:
    """Write the subtitles carried on the task as ``<track>.lrc`` and embed
    them. False when the track came from somewhere else or had none."""
    import os

    from core.imports.context import get_import_track_info, normalize_import_context

    info = get_import_track_info(normalize_import_context(context))
    lrc = info.get("_youtube_lrc")
    if not lrc or not file_path:
        return False
    lrc_path = os.path.splitext(file_path)[0] + ".lrc"
    with open(lrc_path, "w", encoding="utf-8") as fh:
        fh.write(lrc)
    # the sidecar exists now, so this only embeds it (and translates it,
    # FORK.md 1.3) without asking LRClib
    from core.lyrics_client import lyrics_client

    artists = info.get("artists") or []
    first = artists[0] if artists else ""
    artist = first.get("name", "") if isinstance(first, dict) else str(first or "")
    lyrics_client.create_lrc_file(file_path, info.get("name") or "", artist)
    logger.info("[Video Audio] Subtitles written as %s", os.path.basename(lrc_path))
    return True


# ── the app's side, wired by web_server ─────────────────────────────────

def fetch_subtitles(url: str) -> Tuple[str, int]:
    """(the uploader's subtitles as WebVTT or '', the video's length in s)."""
    import yt_dlp

    from core.youtube_client import _resolve_cookie_opts, youtube_authenticated_extractor_args

    opts: Dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    opts.update(_resolve_cookie_opts())
    extra = youtube_authenticated_extractor_args(opts)
    if extra:
        opts["extractor_args"] = extra
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
        duration = int(info.get("duration") or 0)
        subtitles = info.get("subtitles") or {}
        lang = pick_subtitle_language(subtitles, str(info.get("language") or ""))
        if not lang:
            return "", duration
        tracks = [t for t in subtitles[lang] if isinstance(t, dict) and t.get("url")]
        vtt = next((t for t in tracks if t.get("ext") == "vtt"), None)
        if not vtt:
            return "", duration
        with ydl.urlopen(vtt["url"]) as resp:
            return resp.read().decode("utf-8", "replace"), duration


def queue_pinned(info: Dict[str, Any], candidate: Dict[str, Any], name: str, profile_id: Any,
                 deps: Any) -> Tuple[str, str]:
    """One pinned task in its own batch, dispatched. ``deps`` is the
    PinnedBatchDeps the basic search uses."""
    from core.downloads import pinned_batch
    from core.runtime_state import download_tasks, tasks_lock

    batch_id, task_ids = pinned_batch.create_pinned_batch(
        [pinned_batch.PinnedFile(candidate=candidate, track_info=info)],
        name=name, profile_id=profile_id, playlist_prefix="video_audio")
    with tasks_lock:
        task = download_tasks.get(task_ids[0])
        if task is not None:
            # the user picked this video; a quality profile asking for more
            # than YouTube has must not quarantine it
            task["_override_quality"] = True
    pinned_batch.dispatch_pinned_batch(batch_id, task_ids, deps)
    return batch_id, task_ids[0]


def task_snapshot(task_id: str) -> Optional[Dict[str, Any]]:
    from core.runtime_state import download_tasks, tasks_lock

    with tasks_lock:
        task = download_tasks.get(task_id)
        if not task:
            return None
        return {k: task.get(k) for k in ("status", "download_id", "error_message")}


def install(app: Any, *, run_async: Callable, orchestrator: Callable, metadata_client: Callable,
            pinned_deps: Callable, check_permission: Callable, profile_id: Callable,
            add_activity: Callable) -> None:
    """Take over the Search page's two music-video endpoints. The upstream
    views stay as they are and are simply no longer called."""
    from flask import jsonify, request

    def _progress(download_id: str) -> Optional[float]:
        st = run_async(orchestrator().get_download_status(download_id), timeout=10)
        if st is None:
            return None
        raw = float(getattr(st, "progress", 0) or 0)
        return raw * 100.0 if raw <= 1.0 else raw

    def _deps() -> Deps:
        return Deps(
            search_tracks=lambda query, limit: metadata_client().search_tracks(query, limit=limit),
            fetch_subtitles=fetch_subtitles,
            queue=lambda info, candidate, name, pid: queue_pinned(info, candidate, name, pid, pinned_deps()),
            task=task_snapshot,
            progress=_progress,
            add_activity=lambda icon, title, subtitle: add_activity(icon, title, subtitle, "Now"),
        )

    upstream_status = app.view_functions["get_music_video_status"]

    def download_view():
        denied = check_permission()
        if denied:
            return denied
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "No data"}), 400
        result = start(data, profile_id(), _deps())
        if result.get("error"):
            return jsonify({"error": result["error"]}), result.get("code", 400)
        return jsonify(result)

    def status_view(video_id):
        answer = status(video_id, _deps())
        return jsonify(answer) if answer else upstream_status(video_id)

    app.view_functions["download_music_video"] = download_view
    app.view_functions["get_music_video_status"] = status_view
