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
| 1.3 | Lyrics translation | CJK `.lrc` / `.txt` lyrics get a translated line under each original line (or a separate `<name>.en.lrc`). |
| 1.4 | Model per task | Separate model for search terms, names and lyrics. Default `qwen3.5:4b`. |
| 2 | Artist tagging rules | `original name → name to use`, applied to tags and folders. CJK artists are resolved automatically from MusicBrainz aliases (no model); manual rules always win and work for any name. |
| 3 | Discography: hide owned | "Hide owned" in the Download Discography dialog; owned releases are also shown as owned and left unchecked. |
| 4 | Import: rename only | A switch on the Import page. The file is moved and renamed to the path format of the release it was matched to, and nothing inside the file changes: no tag rewrite, artwork, lyrics embed, ReplayGain or conversion. Applies to manual imports. |

Ollama is reached at `OLLAMA_URL` (overridable in the panel). In the Portainer
stack that is `http://ollama:11434` over the external Docker network
`ollama-net`, which the Ollama container must be attached to — on this host
containers cannot reach host-published ports, so `host.docker.internal:11434`
(the fallback default) does not work. If Ollama is down, LLM features pause for
a minute at a time and everything else behaves like upstream.

### Things worth knowing

- **Model quality.** `qwen3.5:4b` is fast and fine for lyrics and plain titles,
  but it does invent romanizations and occasionally a wrong title. That is why
  the artist half of a search variant never comes from the model. For better
  names, set the "names" and "search terms" tasks to `qwen3.5:9b`.
- **Translated names vs. library matching.** Tags and folders carry the
  translated name, while metadata sources still report the original. The
  default template keeps the original inside the name, which upstream's fuzzy
  matching copes with; a template without `{original}` makes it much more
  likely that SoulSync fails to recognise a track it already has.
- **Originals are kept** in `SOULSYNC_ORIGINAL_TITLE / _ALBUM / _ARTIST /
  _ALBUMARTIST` tags when a value is rewritten.
- **Rename only** still names the path from the matched release (with artist
  rules and translations applied), so the file lands next to the rest of the
  album. Integrity checking still runs; quality, AcoustID and silence checks
  do not.

## Code layout

New code lives in files upstream does not have:

```
core/fork/            config, ollama client, sqlite store, translate, lyrics,
                      artist_names, search_terms, tags, hooks
api/fork.py           /api/fork/* endpoints
webui/static/fork-ui.js                         the LLM & Tagging panel
webui/src/routes/import/-import.fork.ts         rename-only preference
webui/src/routes/import/-ui/rename-only-toggle.tsx
tests/fork/           Python tests
webui/src/routes/artist-detail/-artist-detail.discography-modal.fork.test.ts
deploy/portainer-stack.yml
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
| `web_server.py` | registers the `api/fork.py` blueprint |
| `webui/index.html` | one `<script>` tag for `fork-ui.js` |
| `webui/src/routes/artist-detail/-artist-detail.discography-modal.ts` | ownership fallback, `hideOwned` filter |
| `webui/src/routes/artist-detail/-ui/discography-modal.tsx` | "Hide owned" button |
| `webui/src/routes/import/-import.api.ts` | sends `rename_only` |
| `webui/src/routes/import/-ui/import-page.tsx` | mounts the switch |

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
3. clean merge → pushes, which builds a new image;
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

`.github/workflows/fork-docker.yml` publishes
`ghcr.io/itsdmd/soulsync:latest` (and `:<commit sha>`) on every push to
`custom`. `deploy/portainer-stack.yml` is the stack definition: paste it into
the Portainer stack, keep the stack name, and redeploy with "re-pull image" to
update. The package is private until made public in GitHub → Packages (or add
ghcr.io as a registry in Portainer with a `read:packages` token).

To roll back, pin `image:` to a previous `:<commit sha>` tag, or to
`boulderbadgedad/soulsync:latest` for stock upstream — the fork's tables and
settings are simply ignored by upstream.
