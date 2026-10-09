# itsdmd/SoulSync — fork notes

A personal fork of [Nezreka/SoulSync](https://github.com/Nezreka/SoulSync). This
file is the map of what the fork adds, where it touches upstream code, and how
it stays in sync. Upstream's own docs (`README.md`, `docs/`) are unchanged.

## What the fork adds

All of it is configured from **LLM & Tagging** in the sidebar (under Settings).

| # | Feature | Summary |
|---|---------|---------|
| 1.1 | Smarter search terms | When upstream's fixed queries are about to run, extra `artist title` variants are appended after them: the artist's other names from MusicBrainz, and alternative title spellings (romanized, original script, official English) from the model. A file found by a variant may be matched against that variant. After those come **broader** queries, widest last, for networks and indexers that only return what contains every search word: artist + album (under each album spelling the model knows), album alone, part of the album name (the name without bracketed notes, each side of a colon/dash, shorter forms from the model), and an alternative title alone → for a CJK song only, the song's own name alone (always last, on top of the limit; needs at least two CJK characters). **A Vietnamese track** (a name with đ, ă, ơ, ư or a tone-marked vowel) runs the ladder twice: first every query as written, with its diacritics, then the same queries without them (`fork.search_terms.vietnamese_passes`, on). At most two per step and `Broader searches per track` (default 6, 0 = off) in total; generic pieces ("Original Soundtrack") and a single named after its track are skipped. Results are still judged against the track by upstream's matcher (or any variant), so broad queries find more without accepting more. |
| 1.2 | Name translation | CJK song titles and album names are translated and written to tags and paths using a template (default `{translated} ({original})`). Every translation is stored once per name, so an album is always named the same. Edit one in the GUI and the model never overwrites it. |
| 1.3 | Lyrics translation | CJK `.lrc` / `.txt` lyrics get a translated line under each original line. Alternatively `<name>.lrc` becomes the translation only (what the media server shows, and what is embedded) and the untranslated lyrics are kept as `<name>.original.lrc`. |
| 1.4 | Model per task | Separate model for search terms, names and lyrics. Default `qwen3.5:9b`. |
| 2 | Artist tagging rules | `original name → name to use`, applied to tags and folders. CJK artists are resolved automatically from MusicBrainz aliases (no model); manual rules always win and work for any name. |
| 3 | Discography: hide owned | "Hide owned" in the Download Discography dialog; owned releases are also shown as owned and left unchecked. |
| 4 | Import: rename only | The file is moved and renamed to the path format of the release it was matched to, and nothing inside the file changes: no tag rewrite, artwork, lyrics embed, ReplayGain or conversion. Manual imports: the switch in the Import page header. Automatic watcher: "Rename only" in the Import page's settings (gear). |
| 6 | Downloads page | Batches are full-width rows instead of a card grid, with a search box that filters by album/batch name, song title or artist (every word must match, any order; case and accents ignored). A batch found by one of its songs opens and shows just the matching songs. |
| 7 | Album pop-up | (Library → artist → album.) Opening an album only checks the library and shows Found/Missing per track; nothing is searched or downloaded until **Download missing** is pressed, and owned tracks start unticked. A **Local files** bar shows the folder holding the album and lets you pick another, by browsing or by searching the library for a folder name. A folder you pick is **saved for that album** (table `fork_album_folders`, keyed by source + album id, also found by artist + album name from another source) and used every time the album is opened: its files are matched to the tracks directly for Found/Missing, and the library database is only asked about tracks it does not hold. **Forget** removes it; a saved folder that no longer exists is ignored; renaming via Auto-tag moves the saved folder along. **The discography card's Complete/Partial/Missing badge is this same analysis**, not upstream's album-level guess: each card's track list is fetched (metadata cache) and every track looked up in the library and the saved folder, exactly as the pop-up does, so card and pop-up always agree. The result is remembered (table `fork_album_checks`) and reused until tracks are added to or removed from the library or the saved folder changes; opening the album always re-checks and updates its card at once. **Match folders…** (artist page, above the discography; admins) does this for the whole discography at once: each release is looked up — a folder already saved, else the folder the library's tracks of the album sit in, else a folder under the artist's own folder named after the album (its translation counts) — and listed with how many of its tracks are there; ticked rows (library matches, and name matches holding at least half the tracks, by default; any row can be changed with the folder picker) are saved, so later visits read those folders directly, and each saved card is repainted. Opening an album also repaints its card at once (event `fork:album-checked`). Only when a release's track list cannot be fetched does the card fall back to upstream's answer, with the audio files in the saved folder counted. Tools → **Album Completeness** does not flag an album whose saved folder holds at least the expected number of audio files. **Auto-tag…** matches that folder's files to the album's tracks (title incl. original/translation equivalence and file name, track position, duration), and shows an editable proposal before writing. The proposal appears at once using saved translations; names without one are translated in the background (model loaded first, several names per request) and filled into fields you have not edited. Options: use artist rules + saved translations (on); translate new names with the model (on — off skips that step so nothing waits for the model); separate multiple artists (on) — "A, B", "A & B", "A feat. B" are split into individual artists and stored per feature 8 (shown as "A; B" in the edit box when tags are split); rename/move to the path template (off). Every tag column (Album, Year, Title, Artist, Album artist, #, Disc) has its own checkbox, all ticked by default and remembered per browser: an unticked tag is left as it is in the files. Writes source ids and `SOULSYNC_ORIGINAL_*` too; other tags are left alone. |
| 8 | Multiple artists | How several artists on one track are stored, for Artist and Album artist alike. **Split into separate tags** (default): one tag value per artist (`ARTIST=A`, `ARTIST=B`). Or, with that off, one value joined with a chosen **separator** (semicolon, comma, slash, ampersand). The two are mutually exclusive: the separator is locked while tag splitting is on. Set in LLM & Tagging → Multiple artists; applied by the download/import tag pass and the album auto-tagger. The **Multiple Artist Formatter** tool (upstream's Comma Artist Splitter, renamed; its id is still `comma_artist_splitter`) has the same two settings of its own (Split Into Separate Tags, Separator) and also applies them to an album artist that was the combined string. A credit that is one act ("Simon & Garfunkel") is never split when a rule or MusicBrainz knows it. **Custom separators:** the separator can be a character of your own (Custom), and the characters *detected* as separators are an editable list on top of the built-in `, ; & / + feat. ft. featuring with vs. x`. It defaults to the ones CJK releases use: `、 ， ； ／ ＆ ＋ ｜ | • ・ × ✕ ✖ ｘ`. Added punctuation splits wherever it appears; a letter or plain ASCII symbol only between spaces. An all-katakana name with a middle dot (マイケル・ジャクソン) stays whole; the Chinese interpunct `·` is not a default because it mostly sits inside names. The splitter tool has matching Custom Separator and Extra Splitters settings. |
| 9 | Translation list tools | In LLM & Tagging → Translations: **Apply to library…** pushes the current translation onto files that already carry that album/title — a preview lists every file (old → new) first; it rewrites the tag and, optionally (on by default), renames the file or album folder where its name contains the old name. Files are found through the library database and confirmed from their own tags (`SOULSYNC_ORIGINAL_*`, or the original embedded in the name), so look-alikes are never touched. The dialog can be pointed at **one folder** instead of the whole library, which also reaches files SoulSync has not scanned. **Apply all to library…** does the same for every saved translation (optionally one type or one folder) as a background run with a preview and progress. **Artist rules have the same two buttons:** they rewrite ARTIST / ALBUMARTIST on files that still credit the artist under the old name (only that artist inside a shared credit) and, optionally, move albums into the artist's new folder — merging with it if it exists and bringing cover art along. A rule's previous replacement is remembered as another name of the artist, so files tagged under it are still found after the rule changes. Clicking an **original name** (also in Artist rules) opens a pop-up with what the library holds for it: the stored translation/rule, albums, folder and tracks. |
| 10 | Terms that are not translations | An editable list (LLM & Tagging → Translated names) of words that describe a release rather than name it: OST, Original Soundtrack, EP, Remastered, Deluxe Edition, Live, TV Size, feat., … In `危機合約滌墨作戰 (Original Soundtrack)` the bracket is kept as written and only the name is translated, instead of the bracket being taken for the translation. Whole words/phrases, any case; a four-digit year always counts. Records wrongly lifted from such a bracket are dropped automatically so the name gets translated properly. |
| 11 | Tool: Auto Translate | (Tools page.) Scans the library for album and song names still untranslated, gathers the distinct ones, and translates them several per model request (Batch Size, default 10) so the model loads once and stays warm. Names with a saved translation are not re-sent. Dry run (default) creates one finding per name with the proposal; approving it applies whatever is saved by then. Settings: Translate Albums / Titles, Batch Size, Rename Files, Dry Run. |
| 12 | Tool: Album Volume Grouping | (Tools page.) Finds albums by one artist that differ only by a volume marker (Vol./Volume, Pt./Part, Disc/CD + number or Roman numeral, 第N卷, 卷N) and makes each set one album: common album name, volume = disc number, files optionally moved to `<album>/Disc N/`. Markers are found after a comma, a dash, in brackets or bare (`X, Vol. 2`, `X Vol.3`, `X Vol, 4`, `X (Vol. 5)`, `X - Volume 6 (Cover)`); names are compared on letters and digits only, sets differing just by a bracketed note are joined when their numbers do not clash, and an unmarked album with the same name becomes volume 1 when no album claims that number. Needs at least two volumes with distinct numbers. A finding's **Edit…** button opens a dialog to rename the album, change each item's disc number, remove items, and add library albums (search) or library folders (picker); the edited set is stored in the finding (`edited: true`, left alone by later scans, while unedited findings are refreshed by a rescan) and grouped by the normal fix button. A source's "…, Vol. 2" album still counts as owned afterwards. Settings: Move Files, Dry Run. |
| 13 | Album ids stay consistent | After a file is tagged, it and the tracks already in its album folder (disc sub-folders included, same album name only) are made to agree on `MUSICBRAINZ_ALBUMID` / release-group id: whichever side lacks it gets it. Stops Navidrome showing one album as two when the MusicBrainz lookup worked for only some tracks. Two different ids in one folder are left for Tools → Album Tag Consistency. On by default (LLM & Tagging → Features). |
| 14 | YouTube videos to audio | Optional (off by default): every YouTube video SoulSync downloads is also written to the import folder as `Channel - Title.<ext>`, tagged with title, artist and date. Opus 256 kbps by default; MP3, AAC or FLAC selectable. Audio already in the chosen codec is copied rather than re-encoded. The video is kept unless switched off. |
| 5 | Ownership across renamed names | "Already in the library?" no longer depends on a source name fuzzy-matching a translated library name. See below. |
| 15 | Singles and their albums | (Tools → **Single/Album Dedup** and **Duplicate Detector**.) **Merge:** when an album in the library lists a song it does not hold and that song is in the library as a single, Single/Album Dedup reports it (finding *Singles to Merge*, button **Merge**): the file is retagged with the album's name, track/disc number, year and cover, renamed per the path template, moved into the album's folder and filed under the album in the database; the single's row and its emptied folder (cover image only) are removed. The album's track list comes from its metadata source (metadata cache; the first scan reads one list per incomplete album of an artist that has singles, so it is slow once). A single is merged only when *every* track it holds is a listed track the album lacks, in the same version and within `Merge Duration Tolerance` seconds (default 10); settings `Merge Into Albums` (on) and that tolerance are on the tool. **Kept versions:** a title's version note (instrumental, karaoke, off vocal, live, acoustic, remix, edit, demo, … and their CJK forms; not masterings like *remastered*, and not *Album Version* / *Original Mix*) makes it a different recording. Two versions of one song are never reported as duplicates, and a song held on an album *and* on a single that also carries a version the album lacks (the song + its instrumental) is kept in both places: not merged, not a *redundant single*, not a duplicate. Code: `core/fork/single_merge.py`. |
| 16 | Import: copy, verify, delete | A file leaving the import folder is copied next to its destination under a temporary name, read back and compared with the original (size + SHA-256), put in place, and only then deleted from the import folder; sub-folders of the import folder left empty are removed (never the import folder itself; system litter like `.DS_Store` does not keep a folder). A copy that does not match is discarded and the import fails with the original untouched. Applies to everything that leaves the import folder (audio, lyrics and other sidecars, quarantine); other moves are unchanged. Switch: LLM & Tagging → *Import folder: copy, verify, then delete* (`fork.import.copy_verify`, on). Code: `core/fork/import_move.py`. |
| 17 | Tag Editor page | Sidebar → **Tag Editor** (admins): a full-screen page for editing by hand. **Left:** the library's folder tree; it does not follow the list — the **Locate** button (map pin, beside the tree's filter box) opens the tree down to the folder the active list shows and scrolls to it. **Right, top:** search with two modes, *Entire library* and *This folder* (the open folder and the folders inside it). **Middle:** the open folder as a file browser (folders, audio with title/artist/album/#, lyrics, images; click, Ctrl+click, Shift+click for a range, Ctrl+A, double-click to enter, F2 to rename). **Bottom:** the tag editor in the manner of MusicBrainz Picard — *Tag / Original value / New value* for Title, Artist, Album artist, Album, Date, Track, Disc, Genre, Composer, Comment, and then, under "Other tags", **every other tag the selected files carry** (Vorbis comments by key, ID3 frames, MP4 atoms and free-form atoms; BPM, ISRC, LYRICS and the like share one name across formats, a custom tag goes by its own name; tags that are not text — binary frames, extra comments — are listed but locked; `core/fork/all_tags.py`). With several files selected a tag shows its common value or "(n different values)"; only tags you change are written, to every selected file; an emptied value removes the tag; several values are typed `A; B`; Track/Disc take `3` or `3/12`. The cover box shows the embedded cover and can replace it (JPEG/PNG, optionally also as `cover.jpg`/`cover.png` in the folder) or remove it. **Split view** (button beside the search modes) shows two folders side by side, each with its own breadcrumb, selection and search, so items can be dragged from one to the other without navigating; the search box, the buttons and the tag editor follow the pane last clicked. The list has a **tick-box column** (ticking adds to the selection; the header box selects all or none). Above the list, next to Rename, and on **right-click** (rows, the empty part of the list, tree folders): **Copy, Cut, Paste, Move to…, Delete** (Ctrl+C / Ctrl+X / Ctrl+V / Delete). Paste goes into the open folder or the folder right-clicked; a copy pasted next to its original is named "… (copy)"; cut items are dimmed until pasted; Delete counts what will go, asks first and deletes for good (a track's lyrics files with it; the library forgets the tracks). A **filter box above the tree** finds folders by name without touching the list. **Move** files and folders by dragging them onto a folder, within the list, within the tree, or between the two (dropping on the empty part of the list moves into the open folder); a confirmation shows what goes where, a track's lyrics files go with it, nothing is overwritten, and a library folder itself cannot be moved. **Rename** a file or folder in place: a track's lyrics files (`.lrc`, `.txt`, `.original.*`) are renamed with it, and library paths and saved album folders follow. The **Rename** dialog has two tabs: *New name* (one item) and *Find and replace*, which replaces text in the names of the whole selection, plain or regular expression (`$1` groups), with a live preview; extensions are never changed. Nothing is translated or normalised here. **Index:** the tree and the search read tables `fork_fs_dirs` / `fork_fs_files` — kept in their own file `fork_library_index.db` next to the music database, so filling them never makes the app wait — instead of the disk; only the open folder is listed from disk (one `scandir`), a tree node costs one `stat`, tags are re-read only for changed files, and the full walk runs once in the background (paced) and on *Rescan library*. Title/artist/album become searchable once a folder has been opened. The tree and the lists build only the rows in view, so a folder with thousands of entries stays responsive. Code: `core/fork/editor.py`, `core/fork/library_index.py`, `webui/static/fork-editor.js`, `/api/fork/editor/*`. |
| 18 | Import page: dismiss any selection | The selection bar's **Dismiss N** now covers every selected entry still in the import folder — also ones that are *Waiting* (no record yet) or *Failed*, which upstream cannot dismiss. Their record is marked rejected, or one is created; the files stay in the import folder and the importer leaves them alone. `POST /api/fork/import/dismiss`, `core/fork/import_inbox.py`. |
| 19 | Import page: manual import | **Manual** on an inbox row, or **Manual import N** for a selection, opens a form for that entry's files (several entries are taken one after another). It starts from what the files already say: Album, Album artist, Date, Genre and release type for the release, and #, Disc, Title, Artist per file, plus the cover (keep / replace for every file / remove). On **Import** the tags and cover are written into the files, still in the import folder, and the files then go through the normal import pipeline in rename-only mode with a release built from those tags instead of a matched one: named and filed by the path template (tagging rules for names in paths apply), registered in the library, nothing else in the file changed, nothing looked up online. An empty album on a single file makes it a single named after its title. A form with a missing title or artist is refused before anything is written. `core/fork/manual_import.py`, `webui/static/fork-import.js`, `/api/fork/import/manual*`. |
| 20 | Artist folder: first album artist only | When a file is organised, an album whose album artist is several artists (`A, B & C`) is filed in the folder of the first one — `A/A, B & C - Album/…` — instead of a folder named after all of them. Only the artist-level folder changes (the first folder of the path, when the template makes it the album artist and nothing else); the album folder and file names keep the full credit. Names that are one artist (`Simon & Garfunkel`) are not split. Applies to album and single paths, for every import and download; folders already in the library are not moved. Switch: Fork settings → *Artist folder: first album artist only* (`paths.first_album_artist_folder`, on by default). Code: `hooks.first_artist_folder`. |
| 21 | Wishlist: group by album | The Wishlist page (Music) has an **Artists / Albums** switch beside the Nebula / List one; the choice is remembered in the browser. *Albums* makes one group per release instead of one per artist, in both views: the group is named after the release, shows its cover and its artist, and its name opens the artist. Album tracks and singles of the same release by the same artist share a group; two artists' releases of the same name stay apart as "Name — Artist". The search box matches release or artist name. Grab all / Remove act on the release's tracks. Code: `webui/src/routes/wishlist/-wishlist.fork.ts`. |
| 22 | Filler tools: scan cache | (Tools → **ReplayGain Filler**, **Cover Art Filler**, **Lyrics Filler**.) A scan remembers what it checked, so the next one does not open every file or ask every service again: the ReplayGain tags read from a file and whether an album's file carries a cover (both read again when the file's size or mtime changed), and what LRClib and the artwork sources answered. "Nothing found" is only kept once a later question of the same scan to that service was answered, so an outage or a rate limit is not remembered. Each tool has a **cache_days** setting (default 7): the first scan after that many days forgets everything and checks the whole library again, so renamed or moved tracks leave no rows behind and services are asked again; `0` turns the cache off. Stored in `fork_filler_cache.db` next to the music database (`core/fork/filler_cache.py`). |
| 23 | Rating Tag Sync | Tools → **Rating Tag Sync** (weekly; off until enabled). Copies star ratings between Navidrome and the rating tag of the files, for library tracks that came from Navidrome (their id is Navidrome's song id). **Direction**: `navidrome_to_file` (default) or `file_to_navidrome`. Written as `POPM` in ID3 (1/64/128/196/255), `RATING` in Vorbis and as a free-form `RATING` atom in MP4 (20…100); other scales (1–5, 0–1 `FMPS_RATING`, Picard's `RATING:user`) are understood when reading and kept in step when writing. A track without a rating on the source side is left alone unless **Clear Unrated** is on. **Dry Run** lists the changes in the log only. Navidrome's ratings are read in one paged `search3` listing; if it cannot be read to the end nothing is changed. Ratings are those of the configured Navidrome account. Code: `core/fork/rating_sync.py`, job class in `core/repair_jobs/fork_tools.py`. |
| 24 | Wishlist: remove selected | The List view's selection bar has a **Remove** button beside Grab / Skip / Retry: it removes the ticked tracks from the wishlist after a confirmation, without putting them on the ignore list (which is what Skip does). Uses upstream's `/api/wishlist/remove-batch`. `forkRemoveWishlistTracks` in `-wishlist.fork.ts`; one prop and one button in `wishlist-list.tsx`, the mutation in `wishlist-page.tsx`. |

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
- **Splitting tags changes existing habits.** With tag splitting on, a track
  SoulSync downloads with two artists gets two ARTIST values where upstream
  would write one joined string; media servers that predate multi-value tags
  show only the first. Upstream's "featured artists in the title" layout (one
  display artist, longer ARTISTS list) is left as it is. Files the Comma Artist
  Splitter fixed before this setting existed are not revisited by it; the
  download/import pass and auto-tag convert a file when they next touch it.
- **The app shares identical GET requests for 2.5 seconds** (`fetch-dedupe.js`).
  The fork's panels re-read a list right after changing it, so their requests
  carry an abort signal, which opts them out. New fork GETs should do the same.
- **Chinese script variants are the same name.** Traditional and Simplified
  spellings (相變臨界 / 相变临界) share one translation record and match each
  other in ownership checks, auto-tag matching and Apply to library. This uses
  the optional `zhconv` package (GPLv2+, soft-imported, listed at the end of
  `requirements.txt`); a file keeps its own script in the written name.
- **Decoration glued to a name** (`相变临界OST`, no bracket or space) is split
  off like a bracketed one when the Latin tail is nothing but listed terms.
- **Navidrome virtual paths.** Unless "Report Real Path" is enabled for the
  SoulSync player in Navidrome, it reports made-up file names
  (`Artist/Album/01-01 - Title.flac`). The shared path resolver copes by
  probing; upstream's Album Tag Consistency job did its own lookup, skipped
  those tracks, and so both missed split albums and fixed only part of an
  album. The fork makes it fall back to the shared resolver. Enabling Report
  Real Path and refreshing the library is still the proper fix.
- **Cold model.** Loading a model and its first answer take minutes; warm it
  answers in under a second. Requests made while the model is not loaded get
  at least 8 minutes whatever the configured timeout, every request uses the
  same context size (a different size makes Ollama reload the model), and
  output length is capped so a runaway answer ends quickly.
- **Dropped requests.** gunicorn's default closes an idle connection after 2
  seconds; a request sent at that instant is lost and Firefox reports
  "NetworkError when attempting to fetch resource" (a POST is never re-sent by
  the browser). `keepalive` is raised to 120s so the browser closes first, and
  the fork's own requests are re-sent after such a failure (writes only when it
  failed instantly, i.e. before the server could have started on it).
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
                      artist_names, artist_format, comma_split, retro, search_terms, tags,
                      ownership, album_tagging, album_identity, youtube_audio,
                      jobs, hooks
core/repair_jobs/fork_tools.py   the fork job classes (the framework looks for jobs here)
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
`fork_search_terms`, `fork_album_folders`, `fork_album_checks` in the music database (created on first use), and settings
under the `fork` key of the normal config.

### Touch points in upstream files

Kept deliberately small. Each is marked with a `fork` comment. Where possible
the fork **wraps** a function at the bottom of the module instead of editing
its body, so upstream can rewrite the function freely without a conflict.

| File | Change |
|------|--------|
| `core/downloads/task_worker.py` | 3 lines after the query list is built: `augment_search_queries` |
| `core/downloads/validation.py` | EOF wrapper around `_select` (variant rescue) |
| `core/imports/paths.py` | EOF wrapper around `get_file_path_from_template` (names in paths; artist folder named after the first album artist) |
| `core/metadata/enrichment.py` | EOF wrapper around `enhance_file_metadata` (tag pass; rename-only skip) |
| `core/metadata/completion.py` | EOF wrappers: `check_album_completion`, `check_single_completion` (the discography card shows the album pop-up's track-by-track library analysis) |
| `core/repair_jobs/single_album_dedup.py` | EOF wrapper around `SingleAlbumDedupJob.scan` (keeps singles that carry another version; reports singles an album is missing), two settings, help text |
| `core/repair_jobs/duplicate_detector.py` | 1 line in `_scan_bucket` + EOF wrapper around `scan` (versions and releases that carry them are not duplicates), help text |
| `core/repair_jobs/album_completeness.py` | EOF wrapper around `AlbumCompletenessJob.scan` (no finding for an album whose saved folder holds every track) |
| `core/metadata/lyrics.py` | EOF wrapper around `generate_lrc_file` (rename-only skip) |
| `core/lyrics_client.py` | EOF wrapper around `LyricsClient.create_lrc_file` (lyrics translation) |
| `core/imports/pipeline.py` | ReplayGain condition; EOF wrappers around `_apply_profile_output_transforms` and `import_rejection_reason` (a skipped track is reported, not counted as imported) |
| `core/imports/routes.py` | 2 × `mark_rename_only(...)` |
| `core/auto_import_worker.py` | 2 lines: rename-only for the watcher |
| `core/imports/file_ops.py` | EOF wrappers: `move_companion_sidecars`, `downsample_hires_flac` (lyrics backup follows the track), `safe_move_file` (verified copy out of the import folder) |
| `core/library_reorganize.py` | EOF wrappers: `_finalize_track`, `_rename_track_in_place` (same) |
| `core/repair_jobs/track_number_repair.py` | EOF wrapper: `_rename_to_basename` (same) |
| `core/repair_worker.py`, `core/repair_jobs/unknown_artist_fixer.py` | 2 lines at each sidecar move (same) |
| `core/repair_worker.py` | EOF wrapper around `_fix_comma_artist_split` (store the split per the job's strategy) |
| `core/repair_jobs/comma_artist_splitter.py` | EOF block: two extra settings, their options and help text |
| `core/repair_jobs/__init__.py` | one line: registers `core.repair_jobs.fork_tools` |
| `core/repair_jobs/album_tag_consistency.py` | EOF wrapper: `_resolve_path` falls back to the shared path resolver |
| `core/repair_jobs/replaygain_filler.py` | 1 line at the tag read + EOF wrapper around `scan` (scan cache), `cache_days` setting, help text |
| `core/repair_jobs/missing_lyrics.py` | 1 call at the LRClib check + EOF wrapper around `scan` (same) |
| `core/repair_jobs/missing_cover_art.py` | 1 call at the preferred-art lookup + EOF wrappers around `scan`, `file_has_embedded_art`, `_try_source`, `_find_artist_art` (same) |
| `core/video/youtube_download.py` | EOF wrapper around `process_youtube_download` (audio copy to the import folder) |
| `core/repair_worker.py` | EOF block: finding labels, job family, fix handlers for the fork's tools |
| `webui/src/routes/tools/-tools.groups.ts` | two finding blurbs |
| `webui/src/routes/tools/-ui/operations.tsx` | select `disabled`/`title` from `-tools.fork.ts` (separator lock) |
| `core/downloads/master.py` | 7 lines: external-id ownership before a track is queued |
| `core/wishlist/library_match.py` | EOF wrapper around `_strict_identity_matches` |
| `database/music_database.py` | EOF wrappers around the three `check_*_exists` methods |
| `requirements.txt` | one appended dependency: `zhconv` |
| `gunicorn.conf.py` | one appended setting: `keepalive = 120` |
| `web_server.py` | registers the `api/fork.py` blueprint; 2 lines at each of the two track-delete paths (remove the lyrics backup) |
| `webui/src/routes/tools/-ui/findings-surface.tsx` | import + `Edit…` button on a finding card (`forkFindingEditor`) and a reload on `fork:findings-changed` |
| `webui/src/routes/tools/-ui/album-inspection-tray.tsx` | same `Edit…` button and reload in the album action center; both components fall back to `forkFixLabel` so the fork's finding types get a fix button (`Apply`, `Group`) |
| `webui/index.html` | `<script>` tags for `fork-ui.js`, `fork-album.js`, `fork-editor.js` and `fork-import.js`, one `<link>` for `fork.css` |
| `webui/src/routes/active-downloads/-ui/active-downloads-page.tsx` | search state, filtering, mounts the search box |
| `webui/src/routes/active-downloads/-ui/adl-groups.tsx` | optional `searching` prop (unfold matches) |
| `webui/src/routes/artist-detail/-artist-detail.discography-modal.ts` | ownership fallback, `hideOwned` filter |
| `webui/src/routes/artist-detail/-ui/discography-modal.tsx` | "Hide owned" button |
| `webui/src/routes/artist-detail/-ui/artist-detail-page.tsx` | "Match folders…" button above the discography (glue in `-artist-detail.fork.ts`) |
| `webui/src/routes/artist-detail/-artist-detail.use-completion.ts` | one effect: repaint a card when `fork:album-checked` arrives |
| `webui/src/routes/import/-import.api.ts` | sends `rename_only` |
| `webui/src/routes/import/-ui/import-page.tsx` | mounts the switch |
| `webui/src/routes/import/-ui/inbox.tsx` | the selection's Dismiss uses `forkCanDismiss` / `forkDismissItems` (one mutation added); `Manual` button per row and `Manual import N` for a selection; one effect reloading on `fork:import-changed` |
| `webui/src/routes/wishlist/-ui/wishlist-page.tsx`, `wishlist-orb.tsx`, `wishlist-list.tsx` | Group-by switch and regrouping in the page; the two views read the group's artist through `forkArtistOf` (wishlist grouped by album) |
| `webui/src/routes/import/-ui/matcher.tsx` | item lookup goes through `-import.fork-matcher.ts` (survives a folder re-read and a changed key) |
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
