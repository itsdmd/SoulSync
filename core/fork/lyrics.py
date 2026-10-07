"""Translate CJK lyrics sidecars (.lrc / .txt) with the local model."""

from __future__ import annotations

import os
import re
import shutil
from typing import Dict, List, Optional, Tuple

from core.fork import config, ollama
from core.fork.cjk import contains_cjk
from utils.logging_config import get_logger

logger = get_logger("fork.lyrics")

MARKER = "SoulSync LLM translation"
_LRC_MARKER_LINE = f"[re:{MARKER}]"
_TXT_MARKER_LINE = f"[{MARKER}]"
_TIMED_RE = re.compile(r"^((?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+)(.*)$")
# LRC metadata ("[ar:Artist]", "[length:03:20]") — not lyric text.
_ID_TAG_RE = re.compile(r"^\[[A-Za-z#][A-Za-z_]*:.*\]\s*$")
_CHUNK = 40

_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "translation": {"type": "string"}},
                "required": ["id", "translation"],
            },
        }
    },
    "required": ["lines"],
}


def _system_prompt(language: str) -> str:
    return (
        f"You translate song lyrics from Chinese, Japanese or Korean into {language}. You receive "
        "the lyric lines of one song in order, each with an id. Translate every line into natural, "
        f"singable-sounding {language} that keeps the meaning and tone; use the surrounding lines "
        "for context. Translate line by line: never merge, split, reorder or skip lines. Keep "
        "Latin text and names as written. Return only one JSON object: {\"lines\": [{\"id\", "
        "\"translation\"}]} with exactly one entry per input id and no commentary."
    )


def translate_lines(lines: List[str], title: str = "", artist: str = "") -> Dict[str, str]:
    """``{original line: translation}`` for the distinct CJK lines given."""
    unique: List[str] = []
    for line in lines:
        text = line.strip()
        if text and contains_cjk(text) and text not in unique:
            unique.append(text)
    result: Dict[str, str] = {}
    if not unique:
        return result
    language = str(config.get("translate.target_language") or "English")
    for start in range(0, len(unique), _CHUNK):
        chunk = unique[start:start + _CHUNK]
        payload: Dict[str, object] = {
            "lines": [{"id": i, "text": text} for i, text in enumerate(chunk)],
        }
        if title:
            payload["song_title"] = title
        if artist:
            payload["artist"] = artist
        try:
            data = ollama.chat_json("lyrics", _system_prompt(language), payload, _SCHEMA,
                                    temperature=0.3, max_tokens=6000)
        except ollama.OllamaError as exc:
            logger.warning("Lyrics translation failed: %s", exc)
            return {}  # all or nothing: never write a half-translated file
        for item in data.get("lines") or []:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            text = " ".join(str(item.get("translation") or "").split())
            if 0 <= idx < len(chunk) and text and not contains_cjk(text) and text != chunk[idx]:
                result[chunk[idx]] = text
    # A model that answered for only a sliver of the song is not worth writing.
    if len(result) < max(1, len(unique) // 2):
        logger.warning("Lyrics translation covered %s/%s lines; discarding", len(result), len(unique))
        return {}
    return result


def _split_timed(line: str) -> Tuple[str, str]:
    """``(timestamps, lyric text)`` for one LRC line; metadata lines have no
    lyric text."""
    m = _TIMED_RE.match(line)
    if m:
        return m.group(1), m.group(2)
    if _ID_TAG_RE.match(line):
        return "", ""
    return "", line


def render(text: str, is_lrc: bool, translations: Dict[str, str], inline: bool) -> str:
    """Build the output file body. ``inline`` interleaves each translation
    after its original line (same timestamp); otherwise the translation
    replaces the line and untranslated lines are kept as they are."""
    out: List[str] = [_LRC_MARKER_LINE] if is_lrc else []
    for raw in text.splitlines():
        stamp, body = _split_timed(raw) if is_lrc else ("", raw)
        translated = translations.get(body.strip())
        if inline:
            out.append(raw)
            if translated:
                out.append(f"{stamp}{translated}")
        else:
            out.append(f"{stamp}{translated}" if translated else raw)
    if not is_lrc:
        out.append(_TXT_MARKER_LINE)
    return "\n".join(out) + "\n"


def find_sidecar(audio_path: str) -> Optional[str]:
    base = os.path.splitext(audio_path)[0]
    for ext in (".lrc", ".txt"):
        if os.path.isfile(base + ext):
            return base + ext
    return None


def backup_path(sidecar: str) -> str:
    """Where the untranslated lyrics are kept in ``separate`` mode:
    ``song.lrc`` -> ``song.original.lrc``."""
    base, ext = os.path.splitext(sidecar)
    return f"{base}.original{ext}"


def translate_sidecar(audio_path: str, title: str = "", artist: str = "") -> Optional[str]:
    """Translate the lyrics next to ``audio_path``. Returns the text to embed
    in the audio file, or None when nothing was written.

    * ``inline``: each translated line is added under its original line.
    * ``separate``: the sidecar becomes the translation only (so the media
      server shows it) and the untranslated file is kept beside it as
      ``<name>.original.lrc``.
    """
    if not config.get("lyrics.enabled"):
        return None
    sidecar = find_sidecar(audio_path)
    if not sidecar:
        return None
    is_lrc = sidecar.lower().endswith(".lrc")
    inline = str(config.get("lyrics.mode") or "inline") != "separate"
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Could not read lyrics %s: %s", sidecar, exc)
        return None
    if MARKER in text or not contains_cjk(text):
        return None

    bodies = [(_split_timed(line)[1] if is_lrc else line) for line in text.splitlines()]
    translations = translate_lines(bodies, title, artist)
    if not translations:
        return None
    rendered = render(text, is_lrc, translations, inline)
    tmp = f"{sidecar}.fork-tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        if not inline:
            backup = backup_path(sidecar)
            # An existing backup is the true original; never replace it with
            # whatever is in the sidecar now.
            if not os.path.exists(backup):
                os.replace(sidecar, backup)
        os.replace(tmp, sidecar)
    except OSError as exc:
        logger.warning("Could not write translated lyrics %s: %s", sidecar, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    logger.info("Translated lyrics (%s lines, %s) -> %s", len(translations),
                "inline" if inline else "original kept as .original", os.path.basename(sidecar))
    return rendered


_BACKUP_EXTS = (".lrc", ".txt")


def _stem(audio_path: str) -> str:
    return os.path.splitext(str(audio_path))[0]


def move_backups(src_audio: str, dst_audio: str, with_partner: bool = False) -> List[str]:
    """Carry ``<stem>.original.lrc`` / ``.original.txt`` from next to
    ``src_audio`` to next to ``dst_audio``, renamed to the new stem.

    ``with_partner`` also moves the translated ``<stem>.lrc`` that the backup
    belongs to, for callers that move the audio but no sidecars at all. If the
    destination already has a backup (lyrics were regenerated there) the stale
    source copy is removed instead, so nothing is left behind. Best-effort:
    returns the destination paths that now exist, never raises.
    """
    moved: List[str] = []
    src_stem, dst_stem = _stem(src_audio), _stem(dst_audio)
    if not src_stem or not dst_stem or os.path.normpath(src_stem) == os.path.normpath(dst_stem):
        return moved
    for ext in _BACKUP_EXTS:
        pairs = [(f"{src_stem}.original{ext}", f"{dst_stem}.original{ext}")]
        if with_partner and os.path.isfile(pairs[0][0]):
            pairs.append((src_stem + ext, dst_stem + ext))
        for src, dst in pairs:
            if not os.path.isfile(src):
                continue
            try:
                if os.path.exists(dst):
                    os.remove(src)
                else:
                    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                    shutil.move(src, dst)
                    logger.info("Moved lyrics file with its track: %s", os.path.basename(dst))
                moved.append(dst)
            except OSError as exc:
                logger.warning("Could not move lyrics file %s: %s", src, exc)
    return moved


def remove_backups(audio_path: str) -> None:
    """Delete the lyrics backups of a track that is being deleted."""
    stem = _stem(audio_path)
    for ext in _BACKUP_EXTS:
        path = f"{stem}.original{ext}"
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError as exc:
                logger.debug("Could not remove lyrics backup %s: %s", path, exc)
