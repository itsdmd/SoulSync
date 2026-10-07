"""Song/album name translation with a persistent, user-editable cache.

The prompt and the "English (原文)" convention come from
itsdmd/translate-music-library. Unlike that tool the model is only asked for
the translation itself; the final name is assembled here from the configured
template, so the output shape never depends on the model following
formatting instructions.
"""

from __future__ import annotations

import re
import threading
from typing import Callable, Dict, List, Optional

from core.fork import config, ollama, store
from core.fork.cjk import contains_cjk, is_only_decoration, split_name
from utils.logging_config import get_logger

logger = get_logger("fork.translate")

_KIND_LABEL = {"album": "album name", "title": "song title"}
_MAX_LEN = 200
_miss_lock = threading.Lock()

_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "translation": {"type": "string"}},
                "required": ["id", "translation"],
            },
        }
    },
    "required": ["translations"],
}


def _system_prompt(language: str) -> str:
    return (
        f"You translate Chinese, Japanese and Korean music-library values into {language}. "
        f"Translate the meaning into a natural {language} title. If the work has a well-known "
        f"official {language} title, use that. Do not romanize or transliterate unless the text "
        "is a proper name with no translation. Preserve numbers and any Latin text as written. "
        "Use the supplied artist and album only as context; never include them in the result. "
        "Return only one JSON object with a \"translations\" array holding exactly one entry per "
        "requested id: {\"id\", \"translation\"}. A translation must be a short title with no "
        "Chinese, Japanese or Korean characters, no surrounding quotes and no explanation."
    )


def _clean(text: object) -> str:
    if not isinstance(text, str):
        return ""
    out = " ".join(text.split()).strip()
    out = out.strip("\"'“”‘’「」『』")
    return out.strip()


def _valid(translated: str, original: str) -> bool:
    return bool(translated) and len(translated) <= _MAX_LEN and not contains_cjk(translated) \
        and translated.casefold() != original.casefold()


def _ask_model(kind: str, core: str, hint: Optional[Dict[str, str]]) -> str:
    language = str(config.get("translate.target_language") or "English")
    payload: Dict[str, object] = {
        "kind": _KIND_LABEL.get(kind, kind),
        "items": [{"id": 1, "original": core}],
    }
    for key in ("artist", "album"):
        value = (hint or {}).get(key)
        if value and value != core:
            payload[key] = value

    for attempt in range(2):
        try:
            data = ollama.chat_json("names", _system_prompt(language), payload, _SCHEMA, temperature=0.1)
        except ollama.OllamaError as exc:
            logger.warning("Name translation failed for %r: %s", core, exc)
            return ""
        items = data.get("translations")
        candidate = ""
        if isinstance(items, list) and items and isinstance(items[0], dict):
            candidate = _clean(items[0].get("translation"))
        if _valid(candidate, core):
            return candidate
        payload["retry_feedback"] = (
            f"The previous answer {candidate!r} was rejected: it must be a non-empty {language} "
            "title without Chinese, Japanese or Korean characters."
        )
        logger.debug("Rejected translation %r for %r (attempt %s)", candidate, core, attempt + 1)
    return ""


def format_name(translated: str, original: str, suffix: str = "") -> str:
    template = str(config.get("translate.template") or "{translated} ({original})")
    if "{translated}" not in template:
        template = "{translated} ({original})"
    name = template.replace("{translated}", translated).replace("{original}", original)
    name = re.sub(r"\s+", " ", name).strip()
    return f"{name} {suffix}".strip() if suffix else name


def _usable(kind: str, core: str, row: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    """Drop a record that was lifted from the name itself ("existing") but is
    only decoration — "Original Soundtrack" recorded as the translation of
    危機合約滌墨作戰 — so the name gets a real translation instead."""
    if row and row.get("model") == "existing" and not row.get("user_edited") \
            and is_only_decoration(str(row.get("translated") or "")):
        store.delete_translation(kind, str(row.get("original") or core))
        logger.info("Dropped decoration-only record for %s %r: %r", kind, core, row.get("translated"))
        return None
    return row


def purge_decoration_records() -> int:
    """Remove every such record; run when the term list changes and before the
    translation list is shown."""
    removed = 0
    for kind in store.KINDS:
        for item in store.list_translations(kind=kind, limit=100000)["items"]:
            if _usable(kind, item["original"], item) is None:
                removed += 1
    return removed


def lookup(kind: str, core: str, existing: str = "", hint: Optional[Dict[str, str]] = None,
           allow_llm: bool = True) -> str:
    """Bare translation of ``core`` from the cache, creating it if needed."""
    row = _usable(kind, core, store.find_translation(kind, core))
    if row and row.get("translated"):
        return str(row["translated"])
    with _miss_lock:
        row = _usable(kind, core, store.find_translation(kind, core))
        if row and row.get("translated"):
            return str(row["translated"])
        if existing:
            store.save_translation(kind, core, existing, model="existing")
            return existing
        if not allow_llm:
            return ""
        translated = _ask_model(kind, core, hint)
        if translated:
            store.save_translation(kind, core, translated, model=config.model_for("names"))
            logger.info("Translated %s %r -> %r", kind, core, translated)
        return translated


def translate_name(kind: str, value: str, hint: Optional[Dict[str, str]] = None,
                   allow_llm: bool = True) -> str:
    """Final display name for ``value``; returns it unchanged when there is
    nothing to translate or no translation could be produced."""
    if not isinstance(value, str) or not contains_cjk(value):
        return value
    core, existing, suffix = split_name(value)
    if not core or not contains_cjk(core):
        return value
    translated = lookup(kind, core, existing, hint, allow_llm)
    if not translated:
        return value
    return format_name(translated, core, suffix)


def enabled(kind: str) -> bool:
    return bool(config.get("translate.albums" if kind == "album" else "translate.titles"))


def translate_batch(kind: str, items: List[Dict[str, str]], batch_size: int = 10,
                    should_stop: Optional[Callable[[], bool]] = None,
                    on_progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, str]:
    """Translate many names with few model calls.

    ``items`` are ``{"original": core text, "artist": ..., "album": ...}``;
    names that already have a record are skipped. Each prompt carries
    ``batch_size`` names, so the model is loaded once and stays warm for the
    whole run instead of being called once per name. Returns
    ``{original: translation}`` for everything now on record.
    """
    language = str(config.get("translate.target_language") or "English")
    batch_size = max(1, min(int(batch_size or 10), 50))
    done: Dict[str, str] = {}
    pending: List[Dict[str, str]] = []
    seen = set()
    for item in items:
        core = str(item.get("original") or "").strip()
        if not core or core in seen:
            continue
        seen.add(core)
        row = _usable(kind, core, store.find_translation(kind, core))
        if row and row.get("translated"):
            done[core] = str(row["translated"])
        else:
            pending.append({**item, "original": core})

    total = len(pending)
    finished = 0
    for start in range(0, total, batch_size):
        if should_stop and should_stop():
            break
        chunk = pending[start:start + batch_size]
        payload: Dict[str, object] = {"kind": _KIND_LABEL.get(kind, kind), "items": []}
        for index, item in enumerate(chunk, 1):
            entry: Dict[str, object] = {"id": index, "original": item["original"]}
            for key in ("artist", "album"):
                value = item.get(key)
                if value and value != item["original"]:
                    entry[key] = value
            payload["items"].append(entry)  # type: ignore[union-attr]
        try:
            data = ollama.chat_json("names", _system_prompt(language), payload, _SCHEMA, temperature=0.1,
                                    max_tokens=200 + 120 * len(chunk))
        except ollama.OllamaError as exc:
            logger.warning("Batch translation failed (%s names): %s", len(chunk), exc)
            if not ollama.available():
                break   # the server is down: stop instead of failing every remaining batch
            data = {}
        answers: Dict[int, str] = {}
        for entry in data.get("translations") or []:
            if isinstance(entry, dict):
                try:
                    answers[int(entry.get("id"))] = _clean(entry.get("translation"))
                except (TypeError, ValueError):
                    continue
        for index, item in enumerate(chunk, 1):
            candidate = answers.get(index, "")
            if not _valid(candidate, item["original"]):
                # a batch answer can be missing or malformed for one name; ask for it alone
                candidate = _ask_model(kind, item["original"], item)
            if candidate:
                store.save_translation(kind, item["original"], candidate, model=config.model_for("names"))
                done[item["original"]] = candidate
        finished += len(chunk)
        if on_progress:
            on_progress(finished, total)
    return done
