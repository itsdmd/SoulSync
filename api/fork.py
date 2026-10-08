"""Fork endpoints (itsdmd/SoulSync): LLM settings, the translation table and
artist-name tagging rules. Self-contained; see FORK.md."""

from __future__ import annotations

import os

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
    return jsonify(success=True, **album_tagging.analyse_album(
        get_database(), album, artist, tracks, server, source=_body_.get("source")))


@bp.route("/api/fork/album/suggest-folder", methods=["POST"])
@admin_only
def album_suggest_folder():
    """Where an album's files are (saved, in the library, or by folder name),
    with how many of its tracks are there. For matching a discography at once."""
    from core.fork import album_tagging
    from database.music_database import get_database

    body, album, artist, _tracks = _album_payload()
    if not album.get("id") and not album.get("name"):
        return jsonify(success=False, error="No album given"), 400
    return jsonify(success=True, **album_tagging.suggest_folder(get_database(), album, artist, body.get("source")))


@bp.route("/api/fork/album/folder", methods=["POST"])
@admin_only
def album_save_folder():
    """Remember the folder picked for an album (an empty folder forgets it)."""
    from core.fork import album_tagging

    body, album, artist, _tracks = _album_payload()
    try:
        folder = album_tagging.save_folder(body.get("source"), album, artist, body.get("folder"))
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)
    return jsonify(success=True, folder=folder)


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
            separate_artists=body.get("semicolons", True) is not False,
            fields=[str(f) for f in body["fields"]] if isinstance(body.get("fields"), list) else None)
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)
    return jsonify(success=True, **data)


# ── Album Volume Grouping: editing a set by hand ────────────────────────

@bp.route("/api/fork/volumes/search", methods=["GET"])
@admin_only
def volumes_search_albums():
    from core.fork import jobs
    from database.music_database import get_database

    return jsonify(success=True, albums=jobs.search_albums(get_database(), request.args.get("q") or ""))


@bp.route("/api/fork/volumes/describe", methods=["POST"])
@admin_only
def volumes_describe():
    """Titles, track counts and folders for the items being edited."""
    from core.fork import jobs
    from database.music_database import get_database

    try:
        return jsonify(success=True, volumes=jobs.describe_volumes(get_database(), _body().get("items")))
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)


@bp.route("/api/fork/volumes/finding/<int:finding_id>", methods=["POST"])
@admin_only
def volumes_update_finding(finding_id: int):
    from core.fork import jobs
    from database.music_database import get_database

    body = _body()
    try:
        details = jobs.update_volume_finding(get_database(), finding_id, body.get("album"), body.get("items"))
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        return _folder_error(exc)
    return jsonify(success=True, details=details)


# ── Tag Editor page ─────────────────────────────────────────────────────

def _editor_error(exc: Exception):
    status = 404 if isinstance(exc, FileNotFoundError) else 403 if isinstance(exc, PermissionError) else 400
    return jsonify(success=False, error=str(exc)), status


_EDITOR_ERRORS = (ValueError, PermissionError, FileNotFoundError, FileExistsError, OSError)


@bp.route("/api/fork/editor/roots", methods=["GET"])
@admin_only
def editor_roots():
    """The library folders, the tag fields, and the state of the folder index
    (the first full walk is started here when the library was never indexed)."""
    from core.fork import editor, library_index

    roots = editor.roots()
    return jsonify(success=True, roots=[{"path": r, "name": r, "has_children": True} for r in roots],
                   fields=editor.field_list(), index=library_index.ensure_started(roots))


@bp.route("/api/fork/editor/scan", methods=["GET", "POST"])
@admin_only
def editor_scan():
    from core.fork import editor, library_index

    if request.method == "POST":
        return jsonify(success=True, index=library_index.start_scan(editor.roots()))
    return jsonify(success=True, index=library_index.status())


@bp.route("/api/fork/editor/tree", methods=["GET"])
@admin_only
def editor_tree():
    from core.fork import editor, library_index

    try:
        path = editor.safe_path(request.args.get("path"))
        return jsonify(success=True, path=path, dirs=library_index.children(path))
    except _EDITOR_ERRORS as exc:
        return _editor_error(exc)


@bp.route("/api/fork/editor/list", methods=["GET"])
@admin_only
def editor_list():
    from core.fork import album_tagging, editor, library_index

    try:
        path = editor.safe_path(request.args.get("path"))
        if not os.path.isdir(path):
            raise ValueError("Not a folder")
        root = album_tagging.root_of(path)
        return jsonify(success=True, path=path, root=root,
                       parent=None if os.path.normpath(path) == os.path.normpath(root or "") else os.path.dirname(path),
                       **library_index.listing(path))
    except _EDITOR_ERRORS as exc:
        return _editor_error(exc)


@bp.route("/api/fork/editor/search", methods=["GET"])
@admin_only
def editor_search():
    """``path`` given: only under that folder. Reads the index, never the disk."""
    from core.fork import editor, library_index

    try:
        base = editor.safe_path(request.args.get("path")) if request.args.get("path") else None
        return jsonify(success=True, **library_index.search(request.args.get("q") or "", base),
                       index=library_index.status())
    except _EDITOR_ERRORS as exc:
        return _editor_error(exc)


@bp.route("/api/fork/editor/tags", methods=["POST"])
@admin_only
def editor_tags():
    from core.fork import editor

    files, errors = [], []
    for raw in (_body().get("paths") or [])[:2000]:
        try:
            path = editor.safe_path(raw)
            files.append(dict(editor.read_tags(path), path=path))
        except Exception as exc:
            errors.append(f"{os.path.basename(str(raw))}: {exc}")
    return jsonify(success=True, files=files, errors=errors)


@bp.route("/api/fork/editor/cover", methods=["GET"])
@admin_only
def editor_cover():
    from flask import Response

    from core.fork import editor

    try:
        cover = editor.cover_of(editor.safe_path(request.args.get("path")))
    except Exception as exc:
        return _editor_error(exc)
    if not cover:
        return jsonify(success=False, error="No cover in this file"), 404
    return Response(cover[0], mimetype=cover[1], headers={"Cache-Control": "no-store"})


@bp.route("/api/fork/editor/save", methods=["POST"])
@admin_only
def editor_save():
    from core.fork import editor
    from database.music_database import get_database

    body = _body()
    try:
        result = editor.save_tags((body.get("paths") or [])[:2000], body.get("tags") or {},
                                  body.get("cover") if isinstance(body.get("cover"), dict) else None,
                                  db=get_database())
    except _EDITOR_ERRORS as exc:
        return _editor_error(exc)
    return jsonify(success=True, **result)


@bp.route("/api/fork/editor/rename", methods=["POST"])
@admin_only
def editor_rename():
    from core.fork import editor
    from database.music_database import get_database

    body = _body()
    try:
        return jsonify(success=True, **editor.rename(body.get("path"), body.get("name"), db=get_database()))
    except _EDITOR_ERRORS as exc:
        return _editor_error(exc)


@bp.route("/api/fork/editor/bulk-rename", methods=["POST"])
@admin_only
def editor_bulk_rename():
    """Preview (default) or apply a find/replace over the names of ``paths``."""
    from core.fork import editor
    from database.music_database import get_database

    body = _body()
    try:
        return jsonify(success=True, **editor.bulk_rename(
            (body.get("paths") or [])[:5000], body.get("find"), body.get("replace"),
            regex=bool(body.get("regex")), case_sensitive=bool(body.get("case_sensitive")),
            apply=bool(body.get("apply")), db=get_database()))
    except _EDITOR_ERRORS as exc:
        return _editor_error(exc)
