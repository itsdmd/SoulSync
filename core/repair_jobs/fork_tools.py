"""fork (itsdmd/SoulSync): the fork's own Tools-page jobs.

* **Auto Translate** — finds album and song names in the library that are
  still untranslated, translates them in batches (several names per model
  call), and applies the result to the files.
* **Album Volume Grouping** — finds albums released as "…, Vol. 1", "…, Vol. 2"
  and turns each set into ONE album whose volumes are its discs.
* **Rating Tag Sync** — copies star ratings between Navidrome and the rating
  tag in the files (``core/fork/rating_sync.py``).

Only the job classes live here, because this folder is where the framework
(and its consistency tests) look for jobs; the work is in ``core/fork/jobs.py``.
Both follow the framework's contract: the scan creates findings (dry run, the
default) or applies them straight away; a finding's fix does the same work for
one item. See FORK.md.
"""

from core.fork import jobs, rating_sync, translate
from core.fork.cjk import fold
from core.fork.jobs import is_on, job_settings
from core.repair_jobs import register_job
from core.repair_jobs.base import JobContext, JobResult, RepairJob
from utils.logging_config import get_logger

logger = get_logger("repair_job.fork_tools")


@register_job
class AutoTranslateJob(RepairJob):
    job_id = 'fork_auto_translate'
    display_name = "Auto Translate"
    description = "Finds untranslated album and song names in your library and translates them in batches"
    help_text = (
        "Scans your library for album and song names that are still in Chinese, Japanese or Korean "
        "with no translation, gathers them into one list, and translates them with your local model "
        "several names per request — so the model is loaded once and stays warm instead of being "
        "called for every single name.\n\n"
        "Names that already have a saved translation (LLM & Tagging → Translations) are not sent to "
        "the model again; they are only applied.\n\n"
        "In dry run mode (default) each name becomes a finding showing the proposed translation. You "
        "can correct it in LLM & Tagging → Translations first; approving the finding writes whatever "
        "is saved at that moment. Disable dry run to apply everything straight away.\n\n"
        "Settings:\n"
        "- Translate Albums / Translate Titles: which names to look at\n"
        "- Batch Size: names per model request (default 10)\n"
        "- Rename Files: also rename the file or album folder when its name contains the old name\n"
        "- Dry Run: only report, do not change files"
    )
    icon = "repair-icon-tag"
    default_enabled = False
    default_interval_hours = 168
    default_settings = {
        "translate_albums": True,
        "translate_titles": True,
        "batch_size": 10,
        "rename_files": True,
        "dry_run": True,
    }
    auto_fix = True
    writes_library_files = True

    def estimate_scope(self, context: JobContext) -> int:
        try:
            names = jobs.untranslated_names(context.db)
            return len(names["album"]) + len(names["title"])
        except Exception:
            return 0

    def scan(self, context: JobContext) -> JobResult:
        result = JobResult()
        settings = job_settings(self, context)
        dry_run = is_on(settings.get("dry_run", True))
        rename = is_on(settings.get("rename_files", True))
        try:
            batch_size = int(float(settings.get("batch_size") or 10))
        except (TypeError, ValueError):
            batch_size = 10

        names = jobs.untranslated_names(context.db, is_on(settings.get("translate_albums", True)),
                                   is_on(settings.get("translate_titles", True)))
        total = len(names["album"]) + len(names["title"])
        result.scanned = total
        if context.report_progress:
            context.report_progress(phase=f"{total} untranslated name(s) found",
                                    log_line=f'{len(names["album"])} album(s), {len(names["title"])} title(s)',
                                    log_type="info", scanned=0, total=total)
        if not total:
            return result

        done = 0
        for kind in ("album", "title"):
            items = names[kind]
            if not items:
                continue

            def progress(finished: int, pending: int, _kind: str = kind, _base: int = done) -> None:
                if context.report_progress:
                    context.report_progress(phase=f"Translating {_kind}s: {finished}/{pending}",
                                            scanned=_base + finished, total=total)

            translated = translate.translate_batch(kind, items, batch_size, context.check_stop, progress)
            if context.check_stop():
                return result
            for item in items:
                done += 1
                value = translated.get(item["original"])
                if not value:
                    result.skipped += 1
                    continue
                display = translate.format_name(value, item["original"])
                details = {
                    "kind": kind, "original": item["original"], "translation": value,
                    "will_be_written_as": display, "artist": item["artist"],
                    "library_entries": item["count"], "currently": item["example"],
                    "rename_files": rename,
                }
                if dry_run:
                    if not context.create_finding:
                        continue
                    try:
                        inserted = context.create_finding(
                            job_id=self.job_id, finding_type="fork_untranslated", severity="info",
                            entity_type="album" if kind == "album" else "track",
                            entity_id=f"{kind}:{item['original']}", file_path=None,
                            title=f"Untranslated {kind}: {item['original']}",
                            description=f'"{item["original"]}" would become "{display}"',
                            details=details)
                        if inserted:
                            result.findings_created += 1
                        else:
                            result.findings_skipped_dedup += 1
                    except Exception as exc:
                        logger.debug("could not create finding for %r: %s", item["original"], exc)
                        result.errors += 1
                else:
                    applied = jobs.apply_translation_finding(context.db, details)
                    if applied.get("success"):
                        result.auto_fixed += int(applied.get("fixed") or 0)
                    else:
                        result.errors += 1
                if context.update_progress:
                    context.update_progress(done, total)
        if context.report_progress:
            context.report_progress(
                phase=(f"Done — {result.findings_created} name(s) ready to apply" if dry_run
                       else f"Done — {result.auto_fixed} file(s) updated"),
                log_line=f"{result.skipped} could not be translated", log_type="success",
                scanned=total, total=total)
        return result


@register_job
class VolumeGroupingJob(RepairJob):
    job_id = 'fork_volume_grouping'
    display_name = "Album Volume Grouping"
    description = "Finds albums split into volumes and groups them into one album, with each volume as a disc"
    help_text = (
        'Some releases come as several albums: "Footprints of the Traveler, Vol. 1", "…, Vol. 2", '
        "and so on. This finds those sets — same artist, same name apart from the volume marker — "
        "and turns each into a single album: every track gets the common album name, and its volume "
        "number becomes its disc number.\n\n"
        "Recognised markers: Vol. / Volume, Pt. / Part, Disc / CD with a number or Roman numeral, "
        "and 第N卷 / 第N集 / 卷N — after a comma, a dash, in brackets or with nothing in front. An album "
        "with the same name and no marker counts as volume 1 when no other album claims that number.\n\n"
        "Two volumes are the minimum, and a set where two albums claim the same number is left alone. "
        "In dry run mode (default) each set is a finding you approve; nothing changes until then. "
        "Edit… on a finding lets you add albums or folders to the set, remove some, rename the album "
        "and change each item's disc number first.\n\n"
        "Settings:\n"
        "- Move Files: also move the files into one album folder, in Disc 1, Disc 2… sub-folders "
        "(off: only the tags change)\n"
        "- Dry Run: only report, do not change files"
    )
    icon = "repair-icon-album"
    default_enabled = False
    default_interval_hours = 168
    default_settings = {"move_files": True, "dry_run": True}
    auto_fix = True
    writes_library_files = True

    def scan(self, context: JobContext) -> JobResult:
        result = JobResult()
        settings = job_settings(self, context)
        dry_run = is_on(settings.get("dry_run", True))
        move_files = is_on(settings.get("move_files", True))
        groups = jobs.volume_groups(context.db)
        result.scanned = len(groups)
        for index, group in enumerate(groups, 1):
            if context.check_stop():
                return result
            details = jobs.finding_details(group["album"], group["artist"], group["volumes"], move_files)
            numbers = details["volume_numbers"]
            title, description = jobs.finding_text(details)
            entity_id = f'{group["artist"]}:{fold(group["album"])}'
            if context.report_progress:
                context.report_progress(scanned=index, total=len(groups), log_type="warning",
                                        log_line=f'{group["artist"]} — {group["album"]}: volumes {numbers}')
            if dry_run:
                if not context.create_finding:
                    continue
                try:
                    inserted = context.create_finding(
                        job_id=self.job_id, finding_type="fork_album_volumes", severity="info",
                        entity_type="album", entity_id=entity_id,
                        file_path=None, title=title, description=description, details=details)
                    if inserted:
                        result.findings_created += 1
                    else:
                        result.findings_skipped_dedup += 1
                        # already reported: what the set contains may have
                        # changed since (a volume was added, or is now detected)
                        try:
                            jobs.refresh_volume_finding(self.job_id, entity_id, details)
                        except Exception as exc:
                            logger.debug("finding for %r not refreshed: %s", group["album"], exc)
                except Exception as exc:
                    logger.debug("could not create finding for %r: %s", group["album"], exc)
                    result.errors += 1
            else:
                applied = jobs.group_volumes(context.db, details, move_files)
                if applied.get("success"):
                    result.auto_fixed += int(applied.get("fixed") or 0)
                else:
                    result.errors += 1
            if context.update_progress:
                context.update_progress(index, len(groups))
        return result


@register_job
class RatingTagSyncJob(RepairJob):
    job_id = 'fork_rating_sync'
    display_name = "Rating Tag Sync"
    description = "Copies star ratings between Navidrome and the rating tag in your files"
    help_text = (
        "Navidrome keeps your star ratings in its own database and never writes them to the files, "
        "so they are lost with that database and no other player sees them. This tool copies them "
        "one way, for every track of the library that came from Navidrome.\n\n"
        "The rating is written where other players look for it: a POPM frame in MP3 (1, 64, 128, "
        "196, 255 for 1–5 stars), a RATING tag in FLAC / Ogg / Opus and a RATING atom in M4A "
        "(20, 40, 60, 80, 100). Ratings written by other players on other scales are understood "
        "when reading. Only a rating that differs is written; nothing else in the file changes.\n\n"
        "Ratings are those of the Navidrome account SoulSync is connected with. Files are found "
        "through the library's paths, so Navidrome must report real paths. The tool changes things "
        "itself on every run; there is nothing to approve.\n\n"
        "Settings:\n"
        "- Direction: navidrome_to_file (default) writes Navidrome's rating into the file; "
        "file_to_navidrome sets Navidrome's rating from the file's tag\n"
        "- Clear Unrated: off (default), a track with no rating on the source side is left alone. "
        "On, its rating is removed on the other side too, so both always agree\n"
        "- Dry Run: only list in the log what would change"
    )
    icon = "repair-icon-tag"
    default_enabled = False
    default_interval_hours = 168
    default_settings = {
        "direction": rating_sync.NAVIDROME_TO_FILE,
        "clear_unrated": False,
        "dry_run": False,
    }
    setting_options = {"direction": list(rating_sync.DIRECTIONS)}
    auto_fix = True
    writes_library_files = True

    def estimate_scope(self, context: JobContext) -> int:
        try:
            return len(rating_sync.library_tracks(context.db))
        except Exception:
            return 0

    def scan(self, context: JobContext) -> JobResult:
        settings = job_settings(self, context)
        direction = str(settings.get("direction") or "").strip().lower()
        if direction not in rating_sync.DIRECTIONS:
            direction = rating_sync.NAVIDROME_TO_FILE
        return rating_sync.sync(context, JobResult(), direction,
                                clear_unrated=is_on(settings.get("clear_unrated", False)),
                                dry_run=is_on(settings.get("dry_run", False)))
