"""Fork settings: defaults + accessors over the upstream config manager.

Values live under the ``fork`` key of the normal SoulSync config, so they are
persisted, exported and migrated with everything else. Defaults are applied in
code rather than merged into upstream's default config.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict

DEFAULT_MODEL = "qwen3.5:9b"

# Task name -> human label. One model setting per task.
MODEL_TASKS = {
    "search_terms": "Search term suggestions",
    "names": "Song & album name translation",
    "lyrics": "Lyrics translation",
}


def _default_ollama_url() -> str:
    return os.environ.get("OLLAMA_URL") or "http://host.docker.internal:11434"


def _default_keep_terms() -> str:
    from core.fork.cjk import DEFAULT_KEEP_TERMS

    return DEFAULT_KEEP_TERMS


def defaults() -> Dict[str, Any]:
    return {
        "ollama": {
            "url": _default_ollama_url(),
            "timeout": 300,
            "keep_alive": "10m",
            # one context size for every request (a change makes Ollama reload the model)
            "num_ctx": 8192,
        },
        "models": {task: DEFAULT_MODEL for task in MODEL_TASKS},
        "search_terms": {
            "enabled": True,
            "max_variants": 4,
            # Broader queries after those (artist + album, album, part of the
            # album name, title alone); 0 turns them off.
            "max_broad": 6,
            # A Vietnamese track is searched with its diacritics first, then
            # again without them.
            "vietnamese_passes": True,
            # Also accept results that match the suggested artist/title rather
            # than only the original (e.g. a romanized filename for a CJK track).
            "match_variants": True,
        },
        "translate": {
            "titles": True,
            "albums": True,
            "target_language": "English",
            "template": "{translated} ({original})",
            # Terms that describe a release rather than name it (OST, EP,
            # Remastered…): never taken for a translation, kept after the name.
            "keep_terms": _default_keep_terms(),
            "apply_to_paths": True,
            "apply_to_tags": True,
            "write_original_tags": True,
        },
        "lyrics": {
            "enabled": True,
            # inline: translated line follows each original line in the same
            # file. separate: <name>.lrc holds the translation only and the
            # untranslated lyrics are kept as <name>.original.lrc
            "mode": "inline",
        },
        "artist_names": {
            "enabled": True,
            "auto_lookup": True,
        },
        "artists": {
            # Several artists on one track: write one tag value per artist
            # (ARTIST=A, ARTIST=B). Applies to ALBUMARTIST too.
            "split_tags": True,
            # Only used when split_tags is off: semicolon | comma | slash |
            # ampersand | custom (-> custom_separator)
            "separator": "semicolon",
            "custom_separator": "",
            # Extra separators to DETECT when splitting a credit, space-separated,
            # on top of the built-in , ; & / + feat. ft. featuring with vs. x
            "detect": "、 ， ； ／ ＆ ＋ ｜ | • ・ × ✕ ✖ ｘ",
        },
        "albums": {
            # After tagging a file, make it and the tracks already in its album
            # folder agree on the MusicBrainz release id, so the media server
            # does not show one album as two.
            "keep_ids_consistent": True,
        },
        "youtube_audio": {
            # Also write every downloaded YouTube video as audio to the import folder.
            "enabled": False,
            "codec": "opus",      # opus | mp3 | aac | flac
            "bitrate": 256,       # kbps; ignored for flac
            "keep_video": True,
        },
        "import": {
            # The automatic import watcher moves/renames only, leaving file
            # metadata untouched (manual imports have their own switch).
            "rename_only_auto": False,
            # Files leaving the import folder: copy, compare the copy with the
            # original, then delete the original and the folders left empty.
            "copy_verify": True,
        },
        "paths": {
            # An album by several album artists goes into the folder of the
            # first one ("A/A, B - Album"), not a folder of its own ("A, B/…").
            "first_album_artist_folder": True,
        },
    }


def _config_manager():
    from core.settings import config_manager

    return config_manager


_defaults_cache: Dict[str, Any] = {}


def _cached_defaults() -> Dict[str, Any]:
    """The defaults tree, built once per Ollama URL (its only varying part).
    ``get`` is called in tight loops; callers must not mutate the result."""
    url = _default_ollama_url()
    cached = _defaults_cache.get(url)
    if cached is None:
        _defaults_cache.clear()
        cached = _defaults_cache[url] = defaults()
    return cached


def get(key: str, default: Any = None) -> Any:
    """``get('translate.template')`` -> stored value, else the fork default."""
    node: Any = _cached_defaults()
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            node = default
            break
    try:
        return _config_manager().get(f"fork.{key}", node)
    except Exception:
        return node


def model_for(task: str) -> str:
    return str(get(f"models.{task}") or DEFAULT_MODEL)


def _merge(base: Dict[str, Any], override: Any) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        return out
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def all_settings() -> Dict[str, Any]:
    try:
        stored = _config_manager().get("fork", {})
    except Exception:
        stored = {}
    return _merge(defaults(), stored)


def update(incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Persist only keys the fork knows about; returns the effective settings."""
    known = defaults()
    cm = _config_manager()

    def walk(node: Dict[str, Any], template: Dict[str, Any], prefix: str) -> None:
        for k, v in node.items():
            if k not in template:
                continue
            path = f"{prefix}.{k}"
            if isinstance(template[k], dict):
                if isinstance(v, dict):
                    walk(v, template[k], path)
                continue
            want = type(template[k])
            try:
                if want is bool:
                    v = bool(v)
                elif want is int:
                    v = int(v)
                else:
                    v = str(v).strip()
            except (TypeError, ValueError):
                continue
            cm.set(path, v)

    if isinstance(incoming, dict):
        with cm.batch():  # one DB write for the whole save
            walk(incoming, known, "fork")
    return all_settings()
