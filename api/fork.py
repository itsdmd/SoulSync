"""Fork endpoints (itsdmd/SoulSync): LLM settings, the translation table and
artist-name tagging rules. Self-contained; see FORK.md."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from core.fork import artist_names, config, ollama, store, translate
from core.fork.cjk import contains_cjk, split_name
from core.profile_context import admin_only
from utils.logging_config import get_logger

logger = get_logger("api.fork")

bp = Blueprint("fork", __name__)


def create_blueprint():
    return bp


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _int_arg(name: str, default: int, maximum: int) -> int:
    try:
        return max(0, min(int(request.args.get(name, default)), maximum))
    except (TypeError, ValueError):
        return default


# ── settings ────────────────────────────────────────────────────────────

@bp.route("/api/fork/settings", methods=["GET"])
@admin_only
def get_settings():
    return jsonify(success=True, settings=config.all_settings(), defaults=config.defaults(),
                   tasks=config.MODEL_TASKS)


@bp.route("/api/fork/settings", methods=["POST"])
@admin_only
def save_settings():
    settings = config.update(_body())
    ollama.reset_cooldown()
    return jsonify(success=True, settings=settings)


@bp.route("/api/fork/models", methods=["GET"])
@admin_only
def list_models():
    try:
        return jsonify(success=True, models=ollama.list_models(request.args.get("url") or None))
    except ollama.OllamaError as exc:
        return jsonify(success=False, error=str(exc), models=[]), 502


@bp.route("/api/fork/test", methods=["POST"])
@admin_only
def test_model():
    """One round trip through the model configured for ``task``."""
    task = str(_body().get("task") or "names")
    if task not in config.MODEL_TASKS:
        return jsonify(success=False, error="Unknown task"), 400
    ollama.reset_cooldown()
    schema = {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]}
    try:
        data = ollama.chat_json(task, "Reply with the JSON object {\"reply\": \"ok\"}.", {"ping": True}, schema)
    except ollama.OllamaError as exc:
        return jsonify(success=False, error=str(exc), model=config.model_for(task)), 502
    return jsonify(success=True, model=config.model_for(task), reply=data.get("reply"))


# ── translations ────────────────────────────────────────────────────────

@bp.route("/api/fork/translations", methods=["GET"])
@admin_only
def list_translations():
    data = store.list_translations(
        kind=request.args.get("kind") or None,
        search=(request.args.get("search") or "").strip(),
        limit=_int_arg("limit", 200, 1000),
        offset=_int_arg("offset", 0, 10_000_000),
    )
    for item in data["items"]:
        item["display"] = translate.format_name(item["translated"], item["original"])
    return jsonify(success=True, **data)


@bp.route("/api/fork/translations", methods=["PUT"])
@admin_only
def save_translation():
    """Create or correct a translation. Saved rows are marked user-edited and
    are never overwritten by the model."""
    body = _body()
    kind = str(body.get("kind") or "")
    original = str(body.get("original") or "").strip()
    translated = " ".join(str(body.get("translated") or "").split())
    if kind not in store.KINDS or not original or not translated:
        return jsonify(success=False, error="kind, original and translated are required"), 400
    # Key by the same core text the pipeline looks up ("夜曲 (Live)" -> "夜曲").
    core = split_name(original)[0] if contains_cjk(original) else original
    store.save_translation(kind, core or original, translated, user_edited=True)
    return jsonify(success=True, original=core or original,
                   display=translate.format_name(translated, core or original))


@bp.route("/api/fork/translations", methods=["DELETE"])
@admin_only
def delete_translation():
    body = _body()
    removed = store.delete_translation(str(body.get("kind") or ""), str(body.get("original") or ""))
    return jsonify(success=removed), (200 if removed else 404)


@bp.route("/api/fork/translations/preview", methods=["POST"])
@admin_only
def preview_translation():
    body = _body()
    kind = str(body.get("kind") or "title")
    value = str(body.get("value") or "").strip()
    if kind not in store.KINDS or not value:
        return jsonify(success=False, error="kind and value are required"), 400
    result = translate.translate_name(kind, value, {"artist": str(body.get("artist") or "")})
    return jsonify(success=True, original=value, result=result, changed=result != value)


# ── artist name rules ───────────────────────────────────────────────────

@bp.route("/api/fork/artist-names", methods=["GET"])
@admin_only
def list_artist_names():
    data = store.list_artist_names(
        search=(request.args.get("search") or "").strip(),
        include_misses=request.args.get("include_misses") == "1",
        limit=_int_arg("limit", 500, 2000),
        offset=_int_arg("offset", 0, 10_000_000),
    )
    return jsonify(success=True, **data)


@bp.route("/api/fork/artist-names", methods=["PUT"])
@admin_only
def save_artist_name():
    body = _body()
    original = str(body.get("original") or "").strip()
    replacement = " ".join(str(body.get("replacement") or "").split())
    if not original or not replacement:
        return jsonify(success=False, error="original and replacement are required"), 400
    store.save_artist_name(original, replacement, "manual")
    return jsonify(success=True)


@bp.route("/api/fork/artist-names", methods=["DELETE"])
@admin_only
def delete_artist_name():
    removed = store.delete_artist_name(str(_body().get("original") or "").strip())
    return jsonify(success=removed), (200 if removed else 404)


@bp.route("/api/fork/artist-names/lookup", methods=["POST"])
@admin_only
def lookup_artist_name():
    """Ask MusicBrainz now, without saving (the GUI offers the result)."""
    original = str(_body().get("original") or "").strip()
    if not original:
        return jsonify(success=False, error="original is required"), 400
    try:
        found = artist_names.lookup_musicbrainz(original)
    except Exception as exc:
        return jsonify(success=False, error=f"MusicBrainz lookup failed: {exc}"), 502
    return jsonify(success=True, original=original, **found)


@bp.route("/api/fork/search-terms/cache", methods=["DELETE"])
@admin_only
def clear_search_terms():
    return jsonify(success=True, removed=store.clear_search_terms())
