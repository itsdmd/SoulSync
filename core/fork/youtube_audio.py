"""Turn a finished YouTube video download into an audio file in the import
folder, so it can be identified and filed like any other music.

Off by default. With it on, every YouTube video SoulSync downloads is also
written as audio (Opus 256 kbps unless changed) to the configured import
folder as ``<channel> - <title>.<ext>``, tagged with title, artist (the
channel) and date. When the video's audio is already in the chosen codec it is
copied, not re-encoded. The video itself stays where it was filed unless
"keep the video" is switched off.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from typing import Any, Dict, List, Optional

from core.fork import config
from utils.logging_config import get_logger

logger = get_logger("fork.youtube_audio")

# codec setting -> (file extension, ffmpeg encoder, codec name ffprobe reports, lossless)
CODECS: Dict[str, Dict[str, Any]] = {
    "opus": {"ext": ".opus", "encoder": "libopus", "probe": "opus", "lossless": False},
    "mp3": {"ext": ".mp3", "encoder": "libmp3lame", "probe": "mp3", "lossless": False},
    "aac": {"ext": ".m4a", "encoder": "aac", "probe": "aac", "lossless": False},
    "flac": {"ext": ".flac", "encoder": "flac", "probe": "flac", "lossless": True},
}
DEFAULT_CODEC = "opus"
DEFAULT_BITRATE = 256


def settings() -> Dict[str, Any]:
    codec = str(config.get("youtube_audio.codec") or DEFAULT_CODEC).strip().lower()
    try:
        bitrate = int(float(config.get("youtube_audio.bitrate") or DEFAULT_BITRATE))
    except (TypeError, ValueError):
        bitrate = DEFAULT_BITRATE
    return {
        "enabled": bool(config.get("youtube_audio.enabled")),
        "codec": codec if codec in CODECS else DEFAULT_CODEC,
        "bitrate": max(32, min(bitrate, 512)),
        "keep_video": bool(config.get("youtube_audio.keep_video")),
    }


def import_folder() -> str:
    from core.imports.paths import docker_resolve_path
    from core.settings import config_manager

    return docker_resolve_path(config_manager.get("import.staging_path", "./Staging"))


def _safe(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or ""))
    return " ".join(name.split()).strip(". ")[:150] or "untitled"


def output_path(folder: str, channel: str, title: str, ext: str) -> str:
    base = " - ".join(part for part in (_safe(channel) if channel else "", _safe(title)) if part)
    path = os.path.join(folder, base + ext)
    counter = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base} ({counter}){ext}")
        counter += 1
    return path


def source_codec(video_path: str) -> str:
    """Codec of the video's first audio stream, '' when it cannot be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
             "-of", "default=nw=1:nk=1", video_path],
            capture_output=True, text=True, timeout=60, check=False)
        return (out.stdout or "").strip().splitlines()[0].strip().lower() if out.stdout.strip() else ""
    except Exception as exc:
        logger.debug("ffprobe failed for %s: %s", video_path, exc)
        return ""


def ffmpeg_command(video_path: str, out_path: str, codec: str, bitrate: int, copy: bool,
                   meta: Dict[str, str]) -> List[str]:
    info = CODECS[codec]
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", video_path,
           "-vn", "-map", "0:a:0", "-map_metadata", "-1"]
    if copy:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", info["encoder"]]
        if not info["lossless"]:
            cmd += ["-b:a", f"{bitrate}k"]
    for key, value in meta.items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    return cmd + [out_path]


def convert(video_path: str, fields: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Write the audio of ``video_path`` to the import folder. Returns the new
    file's path, or None when it could not be made."""
    cfg = settings()
    if not video_path or not os.path.isfile(video_path):
        return None
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg is not available; cannot extract audio from %s", video_path)
        return None
    fields = fields or {}
    title = str(fields.get("title") or os.path.splitext(os.path.basename(video_path))[0])
    channel = str(fields.get("channel") or "")
    folder = import_folder()
    os.makedirs(folder, exist_ok=True)
    codec = cfg["codec"]
    out_path = output_path(folder, channel, title, CODECS[codec]["ext"])
    # written beside its final name and renamed when complete, so the import
    # watcher never picks up a half-written file
    tmp_path = os.path.join(folder, f".{os.path.basename(out_path)}.part{CODECS[codec]['ext']}")
    copy = source_codec(video_path) == CODECS[codec]["probe"] and not CODECS[codec]["lossless"]
    meta = {"title": title, "artist": channel, "album_artist": channel,
            "date": str(fields.get("published_at") or "")[:10]}
    try:
        done = subprocess.run(ffmpeg_command(video_path, tmp_path, codec, cfg["bitrate"], copy, meta),
                              capture_output=True, text=True, timeout=3600, check=False)
        if done.returncode != 0 or not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise RuntimeError((done.stderr or "ffmpeg failed").strip()[-300:])
        os.replace(tmp_path, out_path)
    except Exception as exc:
        logger.warning("Could not extract audio from %s: %s", os.path.basename(video_path), exc)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return None
    logger.info("YouTube audio (%s%s) -> %s", codec, " copy" if copy else f" {cfg['bitrate']}k",
                out_path)
    if not cfg["keep_video"]:
        try:
            os.remove(video_path)
            logger.info("Removed the video after extracting its audio: %s", video_path)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", video_path, exc)
    return out_path


def after_download(video_path: str, fields: Optional[Dict[str, Any]] = None, wait: bool = False) -> None:
    """Called when a YouTube video has landed. Converts in the background so
    the download worker moves straight on to the next video."""
    if not settings()["enabled"]:
        return
    if wait:
        convert(video_path, fields)
        return
    threading.Thread(target=convert, args=(video_path, fields), name="fork-youtube-audio", daemon=True).start()
