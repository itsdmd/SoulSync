# itsdmd/SoulSync — fork notes

A personal fork of [Nezreka/SoulSync](https://github.com/Nezreka/SoulSync). This
file is the map of what the fork adds, where it touches upstream code, and how
it stays in sync. Upstream's own docs (`README.md`, `docs/`) are unchanged.

## What the fork adds

All of it is configured from **LLM & Tagging** in the sidebar (under Settings).

| # | Feature | Summary |
|---|---------|---------|
| 1.1 | Smarter search terms | When upstream's fixed queries are about to run, extra `artist title` variants are appended after them: the artist's other names from MusicBrainz, and alternative title spellings (romanized, original script, official English) from the model. A file found by a variant may be matched against that variant. |
| 1.2 | Name translation | CJK song titles and album names are translated and written to tags and paths using a template (default `{translated} ({original})`). Every translation is stored once per name, so an album is always named the same. Edit one in the GUI and the model never overwrites it. |
| 1.3 | Lyrics translation | CJK `.lrc` / `.txt` lyrics get a translated line under each original line. Alternatively `<name>.lrc` becomes the translation only (what the media server shows, and what is embedded) and the untranslated lyrics are kept as `<name>.original.lrc`. |
| 1.4 | Model per task | Separate model for search terms, names and lyrics. Default `qwen3.5:9b`. |
| 2 | Artist tagging rules | `original name → name to use`, applied to tags and folders. CJK artists are resolved automatically from MusicBrainz aliases (no model); manual rules always win and work for any name. |
| 3 | Discography: hide owned | "Hide owned" in the Download Discography dialog; owned releases are also shown as owned and left unchecked. |
| 4 | Import: rename only | The file is moved and renamed to the path format of the release it was matched to, and nothing inside the file changes: no tag rewrite, artwork, lyrics embed, ReplayGain or conversion. Manual imports: the switch in the Import page header. Automatic watcher: "Rename only" in the Import page's settings (gear). |
| 6 | Downloads page | Batches are full-width rows instead of a card grid, with a search box that filters by album/batch name, song title or artist (every word must match, any order; case and accents ignored). A batch found by one of its songs opens and shows just the matching songs. |
| 7 | Album pop-up | (Library → artist → album.) Opening an album only checks the library and shows Found/Missing per track; nothing is searched or downloaded until **Download missing** is pressed, and owned tracks start unticked. A **Local files** bar shows the folder holding the album and lets you pick another. **Auto-tag…** matches that folder's files to the album's tracks (title incl. original/translation equivalence and file name, track position, duration), and shows an editable proposal before writing. Options: use artist rules + translations (on), rename/move to the path template (off). Writes source ids and `SOULSYNC_ORIGINAL_*` too; other tags are left alone. |
| 5 | Ownership across renamed names | "Already in the library?" no longer depends on a source name fuzzy-matching a translated library name. See below. |

Ollama is reached at `OLLAMA_URL` (overridable in the panel). In the Portainer
stack that is `http://ollama:11434` over the external Docker network
`ollama-net`, which the Ollama container must be attached to — on this host
containers cannot reach host-published ports, so `host.docker.internal:11434`
(the fallback default) does not work. If Ollama is down, LLM features pause for
a minute at a time and everything else behaves like upstream.

### Things worth knowing

- **Model quality.** The default is `qwen3.5:9b`. Smaller models invent
  romanizations and titles, which is why the artist half of a search variant
  never comes from the model.
- **Originals are kept** in `SOULSYNC_ORIGINAL_TITLE / _ALBUM / _ARTIST /
  _ALBUMARTIST` tags when a value is rewritten.
- **Lyrics backups travel with the track.** `<name>.original.lrc` is moved and
  renamed wherever SoulSync moves a track's lyrics (import, both reorganize
  modes, the repair jobs that rename files, the downsample rename) and deleted
  when the track is deleted. If upstream adds a new place that moves `.lrc`
  files, it needs a `move_lyrics_backup(src, dst)` call too; a backup that does
  get stranded is removed by the Empty Folder Cleaner once its folder has no
  audio left.
- **After auto-tagging**, the media server has to rescan and SoulSync has to
  refresh its library before discography checks reflect the new tags; the
  tagger changes files, not SoulSync's database (except the path, when it
  moves a file).
- **Rename only** still names the path from the matched release (with artist
  rules and translations applied), so the file lands next to the rest of the
  album. Integrity checking still runs; quality, AcoustID and silence checks
  do not.

### How "already owned" works

Sources report 周杰倫 / 夜曲; the library holds Jay Chou / "Nocturne (夜曲)".
Upstream compares those strings fuzzily, finds nothing, and would download the
track again on every scan. The fork adds three exact checks
(`core/fork/ownership.py`):

1. **External IDs** — in the pre-download analysis of every batch, a library
   row carrying the source track's Spotify / Deezer / iTunes / MusicBrainz /
   ISRC id means owned, whatever the names are. SoulSync embeds these ids in
   every file it tags and reads them back into the library.
2. **Recorded names** — `fork_translations` and `fork_artist_names` state which
   name was written for which original; the source names are mapped through
   them and upstream's check is repeated.
3. **Original inside the library name** — a library title shaped
   "<anything> (夜曲)" by one of the artist's known names is the source title
   夜曲. This covers files translated before the fork existed (e.g. by
   translate-music-library), whatever the English wording.

2 and 3 wrap `check_track_exists`, `check_album_exists` and
`check_album_exists_with_editions`, so wishlist, watchlist, discography
completion and sync all benefit. The wishlist then re-checks a hit with a
strict title/artist/album comparison; that check (`_strict_identity_matches`)
is wrapped too and accepts the same exact equivalences, so a fulfilled wish is
recognised and removed. Version decoration still has to agree: "夜曲 (Live)"
is not "Nocturne (夜曲)". They only run after upstream's own check
missed and a CJK name or an artist rule is involved.

Limits: 3 needs the original in the name, so it does not help with a template
that drops `{original}` (1 and 2 still do). Rename-only imports write no ids,
so those files rely on 2 and 3. The search page's "in library" badges use a
separate key lookup and are not covered.

## Code layout

New code lives in files upstream does not have:

```
core/fork/            config, ollama client, sqlite store, translate, lyrics,
                      artist_names, search_terms, tags, ownership, album_tagging, hooks
api/fork.py           /api/fork/* endpoints
webui/static/fork-ui.js                         the LLM & Tagging panel
webui/static/fork.css                           style overrides (loaded last): Downloads rows, search box, album pop-up
webui/static/fork-album.js                      album pop-up: library check, folder picker, auto-tag (wraps the vanilla opener; no upstream edit)
webui/src/routes/active-downloads/-adl.fork-search.ts, -ui/adl-search.tsx
webui/src/routes/import/-import.fork.ts         rename-only preference
webui/src/routes/import/-ui/rename-only-toggle.tsx
webui/src/routes/import/-ui/auto-rename-only-row.tsx
tests/fork/           Python tests
webui/src/routes/artist-detail/-artist-detail.discography-modal.fork.test.ts
deploy/                stacks (SoulSync published + local test, Ollama), build-local.sh
.github/workflows/fork-*.yml
```

Data: tables `fork_translations`, `fork_artist_names`, `fork_artist_aliases`,
`fork_search_terms` in the music database (created on first use), and settings
under the `fork` key of the normal config.

### Touch points in upstream files

Kept deliberately small. Each is marked with a `fork` comment. Where possible
the fork **wraps** a function at the bottom of the module instead of editing
its body, so upstream can rewrite the function freely without a conflict.

| File | Change |
|------|--------|
| `core/downloads/task_worker.py` | 3 lines after the query list is built: `augment_search_queries` |
| `core/downloads/validation.py` | EOF wrapper around `_select` (variant rescue) |
| `core/imports/paths.py` | EOF wrapper around `get_file_path_from_template` (names in paths) |
| `core/metadata/enrichment.py` | EOF wrapper around `enhance_file_metadata` (tag pass; rename-only skip) |
| `core/metadata/lyrics.py` | EOF wrapper around `generate_lrc_file` (rename-only skip) |
| `core/lyrics_client.py` | EOF wrapper around `LyricsClient.create_lrc_file` (lyrics translation) |
| `core/imports/pipeline.py` | ReplayGain condition; EOF wrapper around `_apply_profile_output_transforms` |
| `core/imports/routes.py` | 2 × `mark_rename_only(...)` |
| `core/auto_import_worker.py` | 2 lines: rename-only for the watcher |
| `core/imports/file_ops.py` | EOF wrappers: `move_companion_sidecars`, `downsample_hires_flac` (lyrics backup follows the track) |
| `core/library_reorganize.py` | EOF wrappers: `_finalize_track`, `_rename_track_in_place` (same) |
| `core/repair_jobs/track_number_repair.py` | EOF wrapper: `_rename_to_basename` (same) |
| `core/repair_worker.py`, `core/repair_jobs/unknown_artist_fixer.py` | 2 lines at each sidecar move (same) |
| `core/downloads/master.py` | 7 lines: external-id ownership before a track is queued |
| `core/wishlist/library_match.py` | EOF wrapper around `_strict_identity_matches` |
| `database/music_database.py` | EOF wrappers around the three `check_*_exists` methods |
| `web_server.py` | registers the `api/fork.py` blueprint; 2 lines at each of the two track-delete paths (remove the lyrics backup) |
| `webui/index.html` | `<script>` tags for `fork-ui.js` and `fork-album.js`, one `<link>` for `fork.css` |
| `webui/src/routes/active-downloads/-ui/active-downloads-page.tsx` | search state, filtering, mounts the search box |
| `webui/src/routes/active-downloads/-ui/adl-groups.tsx` | optional `searching` prop (unfold matches) |
| `webui/src/routes/artist-detail/-artist-detail.discography-modal.ts` | ownership fallback, `hideOwned` filter |
| `webui/src/routes/artist-detail/-ui/discography-modal.tsx` | "Hide owned" button |
| `webui/src/routes/import/-import.api.ts` | sends `rename_only` |
| `webui/src/routes/import/-ui/import-page.tsx` | mounts the switch |
| `webui/src/routes/import/-ui/settings-drawer.tsx` | mounts the watcher's rename-only row |

Hooks are fail-safe (an exception returns upstream's value) and inert under
pytest unless `SOULSYNC_FORK_TESTING=1`, so upstream's test suite never reaches
Ollama or MusicBrainz through them. `SOULSYNC_FORK_DISABLE=1` turns every hook
off at runtime.

## Staying in sync with upstream

Branches:

- `main` mirrors `Nezreka/SoulSync@main` exactly. Never commit to it.
- `custom` (default branch) = `main` + the fork. All work happens here.

`.github/workflows/fork-sync-upstream.yml` runs every 6 hours (and on demand):

1. fast-forwards `main` to upstream;
2. merges it into `custom`;
3. clean merge → pushes (publishing an image stays a manual step);
4. conflict → leaves `custom` alone and opens an **"Upstream sync conflict"**
   issue listing the files (closed automatically once it merges cleanly again).

One-time setup: add a repository secret **`SYNC_TOKEN`** — a fine-grained PAT
for this repo with *Contents*, *Workflows* and *Issues* read/write. Without it
the job uses the default token, which GitHub does not allow to push merges
that change files under `.github/workflows/`, so the sync will fail whenever
upstream edits its workflows.

Resolving a conflict by hand:

```bash
git fetch upstream
git checkout custom
git merge upstream/main
# fix the listed files (the table above says what each fork change is for)
git commit && git push
```

`build-and-test.yml` (upstream's CI) runs on every push to `custom`, including
merge commits, and covers `tests/fork/` too.

## Deploying

**Test locally first.**

```bash
./deploy/build-local.sh          # builds soulsync-fork:local on this host
```

Deploy `deploy/portainer-stack.local.yml` in Portainer with "Re-pull image"
off (the image only exists locally). It uses the same ports and volumes as the
normal stack, so stop that one first.

**Publish when it is good.** Run the "Fork: build image" workflow (Actions tab,
or `gh workflow run fork-docker.yml --ref custom`). It pushes
`ghcr.io/itsdmd/soulsync:latest` and `:<commit sha>`; then use
`deploy/portainer-stack.yml`. The package is private until made public in
GitHub → Packages (or add ghcr.io as a registry in Portainer with a
`read:packages` token).

**Ollama.** `deploy/ollama-stack.yml` reproduces the current Ollama container
as a stack and joins it to `ollama-net`, which both SoulSync stacks use.

To roll back, pin `image:` to a previous `:<commit sha>` tag, or to
`boulderbadgedad/soulsync:latest` for stock upstream — the fork's tables and
settings are simply ignored by upstream.
