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


def defaults() -> Dict[str, Any]:
    return {
        "ollama": {
            "url": _default_ollama_url(),
            "timeout": 300,
            "keep_alive": "10m",
        },
        "models": {task: DEFAULT_MODEL for task in MODEL_TASKS},
        "search_terms": {
            "enabled": True,
            "max_variants": 4,
            # Also accept results that match the suggested artist/title rather
            # than only the original (e.g. a romanized filename for a CJK track).
            "match_variants": True,
        },
        "translate": {
            "titles": True,
            "albums": True,
            "target_language": "English",
            "template": "{translated} ({original})",
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
        "import": {
            # The automatic import watcher moves/renames only, leaving file
            # metadata untouched (manual imports have their own switch).
            "rename_only_auto": False,
        },
    }


def _config_manager():
    from core.settings import config_manager

    return config_manager


def get(key: str, default: Any = None) -> Any:
    """``get('translate.template')`` -> stored value, else the fork default."""
    node: Any = defaults()
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
