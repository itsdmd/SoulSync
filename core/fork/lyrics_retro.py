"""Lyrics Translator: translate the lyrics the library already has.

:mod:`core.fork.lyrics` translates a lyrics file at the moment it is fetched.
This goes over every track of the library afterwards and does the same for
what is already there: the ``.lrc`` / ``.txt`` file next to the track, and the
lyrics embedded in the track's tags.

What is already translated is recognised by the marker line the translation
itself carries (``[re:SoulSync LLM translation]``), in the lyrics file and in
the embedded lyrics alike. It is part of the content, so it is still there
after the track and its lyrics file were renamed or moved; nothing is tracked
by path. Two caches (:mod:`core.fork.filler_cache`, lifetime ``cache_days``)
only save work:

* per audio file (size + mtime): whether its embedded lyrics still need
  translating, so an unchanged file is not opened again;
* per lyrics *text* (a hash of its lines and the target language): the
  model's answer. The same song on a single and on its album, or in a lyrics
  file and in the tags, is one request, and a text the model could not
  translate is not sent again until the cache runs out.

Every lyrics text is its own model request; texts are never combined.

**Retranslate** (:func:`revert`) puts the untranslated lyrics back first, so
the same pass then translates them again: from the ``.original`` file where
separate mode kept one, otherwise by taking the added lines out of an inline
translation (:func:`core.fork.lyrics.strip_inline`). The caches are emptied
for such a run, so the model is really asked again.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional, Tuple

from core.fork import config, filler_cache, lyrics, ollama
from core.fork.cjk import contains_cjk
from utils.logging_config import get_logger

logger = get_logger("fork.lyrics_retro")

JOB_ID = "fork_lyrics_translate"
_VORBIS_KEYS = ("lyrics", "unsyncedlyrics")
_GIVE_UP_AFTER = 3          # model errors in a row


# ── embedded lyrics ─────────────────────────────────────────────────────

def _open(path: str) -> Any:
    from mutagen import File as MutagenFile

    audio = MutagenFile(path)
    return audio if audio is not None and audio.tags is not None else None


def _kind(audio: Any) -> str:
    from mutagen._vorbis import VCommentDict
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Tags

    if isinstance(audio.tags, ID3):
        return "id3"
    if isinstance(audio.tags, VCommentDict):
        return "vorbis"
    return "mp4" if isinstance(audio.tags, MP4Tags) else ""


def _embedded(audio: Any) -> str:
    kind = _kind(audio)
    if kind == "id3":
        frames = audio.tags.getall("USLT")
        return str(frames[0].text or "") if frames else ""
    if kind == "vorbis":
        for key in _VORBIS_KEYS:
            values = audio.tags.get(key)
            if values and str(values[0]).strip():
                return str(values[0])
    if kind == "mp4":
        values = audio.tags.get("\xa9lyr")
        return str(values[0]) if values else ""
    return ""


def read_embedded(path: str) -> str:
    """The lyrics in the tags of ``path``; ``""`` when it has none."""
    audio = _open(path)
    return _embedded(audio) if audio is not None else ""


def write_embedded(path: str, text: str) -> None:
    """Replace the lyrics in the tags of ``path``; nothing else changes."""
    audio = _open(path)
    kind = _kind(audio) if audio is not None else ""
    if kind == "id3":
        from mutagen.id3 import USLT

        old = audio.tags.getall("USLT")
        audio.tags.delall("USLT")
        audio.tags.add(USLT(encoding=3, lang=(old[0].lang if old else None) or "eng", desc="", text=text))
    elif kind == "vorbis":
        for key in [k for k in _VORBIS_KEYS if audio.tags.get(k)] or [_VORBIS_KEYS[0]]:
            audio.tags[key] = [text]
    elif kind == "mp4":
        audio.tags["\xa9lyr"] = [text]
    else:
        raise ValueError("lyrics cannot be embedded in this file format")
    audio.save()


def _embedded_needs(path: str) -> bool:
    try:
        return lyrics.needs_translation(read_embedded(path))
    except Exception as exc:
        logger.debug("embedded lyrics of %s not read: %s", path, exc)
        return False


# ── translating one text ────────────────────────────────────────────────

def _translate(text: str, is_lrc: bool, title: str, artist: str, inline: bool, album: str = "") -> Optional[str]:
    """``text`` translated and rendered, or None when the model's answer was
    not usable. Raises :class:`ollama.OllamaError` when the model failed."""
    lines = lyrics.bodies(text, is_lrc)
    language = str(config.get("translate.target_language") or "English")
    distinct = sorted({line.strip() for line in lines if contains_cjk(line)})
    digest = hashlib.sha1("\n".join([language, *distinct]).encode("utf-8")).hexdigest()  # noqa: S324
    translations = filler_cache.lookup(
        "model", lambda _digest: lyrics.translate_lines(lines, title, artist, strict=True, album=album), digest)
    return lyrics.render(text, is_lrc, translations, inline) if translations else None


def _keep_original(audio_path: str, text: str, is_lrc: bool) -> None:
    """Embedded lyrics have no file to keep the original in: write one,
    unless the track already has an ``.original`` file."""
    stem = os.path.splitext(audio_path)[0]
    if any(os.path.exists(f"{stem}.original{ext}") for ext in (".lrc", ".txt")):
        return
    with open(f"{stem}.original{'.lrc' if is_lrc else '.txt'}", "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


def _named(audio_path: str, title: str, artist: str, album: str) -> Tuple[str, str, str]:
    """What the library did not say about the song, from the file's tags."""
    if title and artist and album:
        return title, artist, album
    known = lyrics.metadata_of(audio_path)
    return title or known.get("title", ""), artist or known.get("artist", ""), album or known.get("album", "")


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Could not read lyrics %s: %s", path, exc)
        return None


def _orphan_original(audio_path: str) -> Optional[str]:
    """The ``.original`` file :func:`_keep_original` wrote for lyrics that
    only exist in the tags."""
    stem = os.path.splitext(audio_path)[0]
    return next((f"{stem}.original{ext}" for ext in (".lrc", ".txt") if os.path.isfile(f"{stem}.original{ext}")), None)


def is_translated(audio_path: str, embedded: bool = True) -> bool:
    """Whether the lyrics file or the embedded lyrics carry a translation."""
    sidecar = lyrics.find_sidecar(audio_path)
    if sidecar and lyrics.MARKER in (_read_text(sidecar) or ""):
        return True
    try:
        return embedded and lyrics.MARKER in read_embedded(audio_path)
    except Exception as exc:
        logger.debug("embedded lyrics of %s not read: %s", audio_path, exc)
        return False


def revert(audio_path: str, embedded: bool = True) -> bool:
    """Put the untranslated lyrics of one track back, in the lyrics file and
    (``embedded``) in the tags, so they can be translated again. Returns
    whether anything was put back. Lyrics whose original cannot be recovered
    — the translation replaced it and the ``.original`` file is gone — are
    left as they are."""
    changed = False
    sidecar = lyrics.find_sidecar(audio_path)
    if sidecar:
        text = _read_text(sidecar) or ""
        if lyrics.MARKER in text:
            backup = lyrics.backup_path(sidecar)
            if os.path.isfile(backup):
                os.replace(backup, sidecar)
                changed = True
            else:
                original = lyrics.strip_inline(text, sidecar.lower().endswith(".lrc"))
                if lyrics.needs_translation(original):
                    tmp = f"{sidecar}.fork-tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        fh.write(original)
                    os.replace(tmp, sidecar)
                    changed = True
    if not embedded:
        return changed
    text = read_embedded(audio_path)
    if lyrics.MARKER not in text:
        return changed
    if sidecar and lyrics.needs_translation(_read_text(sidecar)):
        return True         # the tags get the file's new translation
    orphan = None if sidecar else _orphan_original(audio_path)
    original = (_read_text(orphan) if orphan else None) or lyrics.strip_inline(text, lyrics.is_timed(text))
    if not lyrics.needs_translation(original):
        return changed
    write_embedded(audio_path, original.strip())
    if orphan:
        os.remove(orphan)   # written again, from this text, when it is translated
    return True


def translate_track(audio_path: str, title: str = "", artist: str = "", inline: bool = False,
                    embedded: bool = True, dry_run: bool = False, redo: bool = False,
                    album: str = "") -> Tuple[str, str]:
    """Translate the lyrics of one track. Returns ``(outcome, what)``:
    ``done`` / ``would`` (dry run) / ``unusable`` (the model's answer was not
    worth writing) / ``""`` (nothing to do); ``what`` names what was
    translated. Raises ``OllamaError`` when the model failed, ``OSError`` /
    mutagen errors when a file could not be written.

    ``redo`` translates lyrics that are translated already once more."""
    if redo and is_translated(audio_path, embedded):
        if dry_run:
            return "would", "lyrics again"
        revert(audio_path, embedded)
    sidecar = lyrics.find_sidecar(audio_path)
    side_text: Optional[str] = None
    if sidecar:
        try:
            with open(sidecar, "r", encoding="utf-8") as fh:
                side_text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Could not read lyrics %s: %s", sidecar, exc)

    if sidecar and lyrics.needs_translation(side_text):
        what = "lyrics file + embedded lyrics" if embedded else "lyrics file"
        if dry_run:
            return "would", what
        title, artist, album = _named(audio_path, title, artist, album)
        rendered = _translate(side_text, sidecar.lower().endswith(".lrc"), title, artist, inline, album)
        if rendered is None:
            return "unusable", what
        lyrics.write_sidecar(sidecar, rendered, inline)
        if embedded:
            try:
                write_embedded(audio_path, rendered.strip())
            except ValueError as exc:       # the file is done; a format without lyrics tags is no failure
                logger.debug("lyrics not embedded in %s: %s", audio_path, exc)
                what = "lyrics file"
        return "done", what

    if not embedded or not filler_cache.file_value(audio_path, _embedded_needs):
        return "", ""
    text = read_embedded(audio_path)
    if not lyrics.needs_translation(text):
        return "", ""
    if side_text and lyrics.MARKER in side_text:
        # the lyrics file is translated already: the tags simply follow it
        if not dry_run:
            write_embedded(audio_path, side_text.strip())
        return ("would" if dry_run else "done"), "embedded lyrics (from the lyrics file)"
    what = "embedded lyrics"
    if dry_run:
        return "would", what
    is_lrc = lyrics.is_timed(text)
    title, artist, album = _named(audio_path, title, artist, album)
    rendered = _translate(text, is_lrc, title, artist, inline, album)
    if rendered is None:
        return "unusable", what
    if not inline:
        _keep_original(audio_path, text, is_lrc)
    write_embedded(audio_path, rendered.strip())
    return "done", what


# ── the scan ────────────────────────────────────────────────────────────

def library_tracks(db: Any) -> List[Dict[str, Any]]:
    from core.fork import retro

    return retro._query(db, """
        SELECT t.id, t.title, ar.name AS artist, al.title AS album, t.file_path
        FROM tracks t LEFT JOIN artists ar ON ar.id = t.artist_id LEFT JOIN albums al ON al.id = t.album_id
        WHERE t.file_path IS NOT NULL AND t.file_path != ''
    """, ())


def run(context: Any, result: Any, mode: str = lyrics.SEPARATE, embedded: bool = True,
        dry_run: bool = False, cache_days: Any = filler_cache.DEFAULT_DAYS, redo: bool = False) -> Any:
    """Translate the untranslated CJK lyrics of every library track, counting
    into ``result`` (a ``JobResult``). ``redo`` (Retranslate) also does the
    translated ones again. ``result.completed`` says the whole library was
    gone through."""
    from core.fork.rating_sync import _on_disk

    def say(line: str, kind: str = "info", **more: Any) -> None:
        if context.report_progress:
            context.report_progress(log_line=line, log_type=kind, **more)

    inline = lyrics.inline_mode(mode)
    tracks = library_tracks(context.db)
    total = len(tracks)
    if context.update_progress:
        context.update_progress(0, total)
    if context.report_progress:
        context.report_progress(phase="Translating lyrics...", total=total)

    would = failures = 0
    seen = set()
    result.completed = False
    if redo and not dry_run:
        try:        # what the model answered before must not be answered from the cache
            filler_cache.clear(JOB_ID)
        except Exception as exc:
            logger.warning("lyrics cache not emptied: %s", exc)
    with filler_cache.session(JOB_ID, cache_days):
        for index, track in enumerate(tracks, 1):
            if context.check_stop():
                return result
            if index % 10 == 0 and context.wait_if_paused():
                return result
            if context.update_progress and index % 25 == 0:
                context.update_progress(index, total)
            result.scanned += 1
            path = _on_disk(track["file_path"], context)
            if not path or path in seen:
                result.skipped += 1
                continue
            seen.add(path)
            name = f'{track.get("title") or os.path.basename(path)} — {track.get("artist") or "Unknown"}'
            try:
                outcome, what = translate_track(path, str(track.get("title") or ""), str(track.get("artist") or ""),
                                                inline=inline, embedded=embedded, dry_run=dry_run, redo=redo,
                                                album=str(track.get("album") or ""))
            except ollama.OllamaError as exc:
                result.errors += 1
                failures += 1
                say(f"Model failed — {name}: {exc}", "error", scanned=index, total=total)
                if failures >= _GIVE_UP_AFTER:
                    result.stopped_early = "The model failed several times in a row; stopped (LLM & Tagging → Ollama)"
                    say(result.stopped_early, "warning")
                    return result
                continue
            except Exception as exc:
                result.errors += 1
                say(f"Failed — {name}: {exc}", "error", scanned=index, total=total)
                continue
            failures = 0
            if outcome == "done":
                result.auto_fixed += 1
                say(f"Translated {what} — {name}", "success", scanned=index, total=total)
            elif outcome == "would":
                would += 1
                say(f"Would translate {what} — {name}", scanned=index, total=total)
            else:
                result.skipped += 1
                if outcome == "unusable":
                    say(f"The model's answer was not usable, left as it is — {name}", "warning",
                        scanned=index, total=total)

    result.completed = True
    if context.update_progress:
        context.update_progress(total, total)
    say(f"Done — {would} track(s) would be translated (dry run)" if dry_run
        else f"Done — lyrics of {result.auto_fixed} track(s) translated, {result.errors} failed", "success")
    return result


__all__ = ["JOB_ID", "is_translated", "library_tracks", "read_embedded", "revert", "run", "translate_track",
           "write_embedded"]
