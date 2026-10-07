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
    try:
        translate.purge_decoration_records()   # the term list may have changed
    except Exception as exc:
        logger.debug("purge after settings save failed: %s", exc)
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
    try:
        translate.purge_decoration_records()
    except Exception as exc:
        logger.debug("purge before listing failed: %s", exc)
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


@bp.route("/api/fork/translations/apply", methods=["POST"])
@admin_only
def apply_translation_to_library():
    """Rewrite library files that carry this album/title to the stored
    translation. ``dry_run`` returns what would change without writing."""
    from core.fork import retro
    from database.music_database import get_database

    body = _body()
    try:
        data = retro.apply_translation(
            get_database(), str(body.get("kind") or ""), str(body.get("original") or "").strip(),
            rename=body.get("rename", True) is not False, dry_run=body.get("dry_run") is True,
            folder=str(body.get("folder") or "").strip() or None)
    except (PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except LookupError as exc:
        return jsonify(success=False, error=str(exc)), 404
    return jsonify(success=True, **data)


@bp.route("/api/fork/translations/apply-all", methods=["POST"])
@admin_only
def apply_all_translations():
    """Start applying every stored translation in the background."""
    from core.fork import retro
    from database.music_database import get_database

    body = _body()
    try:
        job = retro.start_apply_all(
            get_database, kind=str(body.get("kind") or "") or None,
            rename=body.get("rename", True) is not False, dry_run=body.get("dry_run") is True,
            folder=str(body.get("folder") or "").strip() or None)
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)
    except RuntimeError as exc:
        return jsonify(success=False, error=str(exc)), 409
    return jsonify(success=True, job=job)


@bp.route("/api/fork/translations/apply-all", methods=["GET"])
@admin_only
def apply_all_status():
    from core.fork import retro

    return jsonify(success=True, job=retro.job_status())


@bp.route("/api/fork/details", methods=["GET"])
@admin_only
def library_details():
    """What the library holds for an original album / title / artist name."""
    from core.fork import retro
    from database.music_database import get_database

    try:
        data = retro.details(get_database(), request.args.get("kind") or "", request.args.get("name") or "")
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    return jsonify(success=True, **data)


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
    previous = store.get_artist_name(original)
    store.save_artist_name(original, replacement, "manual")
    if previous and previous.get("replacement") and previous["replacement"] != replacement:
        from core.fork import retro

        # files tagged under the old rule keep being recognised as this artist
        retro.remember_previous_name(original, previous["replacement"])
    return jsonify(success=True)


@bp.route("/api/fork/artist-names", methods=["DELETE"])
@admin_only
def delete_artist_name():
    removed = store.delete_artist_name(str(_body().get("original") or "").strip())
    return jsonify(success=removed), (200 if removed else 404)


@bp.route("/api/fork/artist-names/apply", methods=["POST"])
@admin_only
def apply_artist_rule_to_library():
    """Rewrite files that still carry the artist's old name to the rule's
    current one. ``dry_run`` returns what would change without writing."""
    from core.fork import retro
    from database.music_database import get_database

    body = _body()
    try:
        data = retro.apply_artist_rule(
            get_database(), str(body.get("original") or "").strip(),
            rename=body.get("rename", True) is not False, dry_run=body.get("dry_run") is True,
            folder=str(body.get("folder") or "").strip() or None)
    except (PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except LookupError as exc:
        return jsonify(success=False, error=str(exc)), 404
    return jsonify(success=True, **data)


@bp.route("/api/fork/artist-names/apply-all", methods=["POST"])
@admin_only
def apply_all_artist_rules():
    from core.fork import retro
    from database.music_database import get_database

    body = _body()
    try:
        job = retro.start_apply_all_rules(
            get_database, rename=body.get("rename", True) is not False,
            dry_run=body.get("dry_run") is True, folder=str(body.get("folder") or "").strip() or None)
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)
    except RuntimeError as exc:
        return jsonify(success=False, error=str(exc)), 409
    return jsonify(success=True, job=job)


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


# ── album pop-up: library check, folder picker, in-place tagging ────────

def _album_payload():
    body = _body()
    album = body.get("album") if isinstance(body.get("album"), dict) else {}
    artist = body.get("artist") if isinstance(body.get("artist"), dict) else {}
    tracks = [t for t in (body.get("tracks") or []) if isinstance(t, dict)][:500]
    return body, album, artist, tracks


def _folder_error(exc: Exception):
    status = 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, FileNotFoundError) else 400
    return jsonify(success=False, error=str(exc)), status


@bp.route("/api/fork/album/check", methods=["POST"])
@admin_only
def album_check():
    """Which tracks of this release are in the library. Never downloads."""
    from core.fork import album_tagging
    from core.settings import config_manager
    from database.music_database import get_database

    _body_, album, artist, tracks = _album_payload()
    if not tracks:
        return jsonify(success=False, error="No tracks given"), 400
    try:
        server = config_manager.get_active_media_server()
    except Exception:
        server = None
    return jsonify(success=True, **album_tagging.check_album(get_database(), album, artist, tracks, server))


@bp.route("/api/fork/album/browse", methods=["GET"])
@admin_only
def album_browse():
    from core.fork import album_tagging

    try:
        return jsonify(success=True, **album_tagging.browse(request.args.get("path") or None))
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)


@bp.route("/api/fork/album/search-folders", methods=["GET"])
@admin_only
def album_search_folders():
    from core.fork import album_tagging

    return jsonify(success=True, **album_tagging.search_folders(request.args.get("q") or ""))


@bp.route("/api/fork/album/tag-preview", methods=["POST"])
@admin_only
def album_tag_preview():
    from core.fork import album_tagging

    body, album, artist, tracks = _album_payload()
    if not tracks:
        return jsonify(success=False, error="No tracks given"), 400
    try:
        data = album_tagging.preview(str(body.get("folder") or ""), album, artist, tracks,
                                     apply_rules=body.get("apply_rules", True) is not False,
                                     semicolons=body.get("semicolons", True) is not False)
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)
    return jsonify(success=True, **data)


@bp.route("/api/fork/album/translate", methods=["POST"])
@admin_only
def album_translate_start():
    """Translate names in the background; the review dialog polls the status."""
    from core.fork import album_tagging

    items = _body().get("items")
    return jsonify(success=True, job=album_tagging.start_translate(items if isinstance(items, list) else []))


@bp.route("/api/fork/album/translate", methods=["GET"])
@admin_only
def album_translate_status():
    from core.fork import album_tagging

    return jsonify(success=True, job=album_tagging.translate_status())


@bp.route("/api/fork/album/tag-apply", methods=["POST"])
@admin_only
def album_tag_apply():
    from core.fork import album_tagging

    body, album, artist, tracks = _album_payload()
    rows = body.get("rows") if isinstance(body.get("rows"), list) else []
    if not rows:
        return jsonify(success=False, error="Nothing selected to tag"), 400
    try:
        data = album_tagging.apply(
            str(body.get("folder") or ""), rows, album, artist, tracks,
            source=str(body.get("source") or ""), rename=body.get("rename") is True,
            apply_rules=body.get("apply_rules", True) is not False,
            separate_artists=body.get("semicolons", True) is not False)
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)
    return jsonify(success=True, **data)
