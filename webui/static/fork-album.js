// fork (itsdmd/SoulSync): additions to the album pop-up (the dialog that opens
// when you pick an album from an artist's discography).
//
//  1. Opening an album only CHECKS the library: each track shows Found/Missing
//     straight away. Nothing is searched or downloaded until "Download missing"
//     (upstream's "Begin Analysis", renamed) is pressed.
//  2. A "Local files" bar shows the folder SoulSync thinks holds the album and
//     lets you pick another one.
//  3. "Auto-tag…" matches the files in that folder to the album's tracks and
//     proposes tags you can edit before anything is written.
//
// Self-contained: wraps window.openDownloadMissingModalForArtistAlbum and adds
// its own elements. Backed by api/fork.py (/api/fork/album/*). See FORK.md.
(function () {
    'use strict';

    const API = '/api/fork/album';
    const FIELDS = [
        ['title', 'Title', 3],
        ['artist', 'Artist', 2],
        ['albumartist', 'Album artist', 2],
        ['track_number', '#', 0],
        ['disc_number', 'Disc', 0],
    ];

    const toast = (msg, type) => (window.showToast ? window.showToast(msg, type || 'success') : console.log(msg));

    function el(tag, attrs, children) {
        const node = document.createElement(tag);
        for (const [k, v] of Object.entries(attrs || {})) {
            if (k === 'class') node.className = v;
            else if (k === 'text') node.textContent = v;
            else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
            else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
        }
        for (const child of [].concat(children || [])) {
            if (child != null) node.append(child);
        }
        return node;
    }


    // The server closes an idle keep-alive connection after a couple of seconds.
    // When the browser sends a request down a connection the server is closing
    // at that instant, the request is lost: fetch() rejects with a bare network
    // error ("NetworkError when attempting to fetch resource" in Firefox), and
    // a POST is never re-sent automatically. Such a failure is immediate and
    // means the server did not process anything, so it is safe to send again.
    async function fetchWithRetry(url, options, repeatable) {
        for (let attempt = 0; ; attempt++) {
            const started = Date.now();
            try {
                return await fetch(url, Object.assign({}, options, { signal: new AbortController().signal }));
            } catch (err) {
                // Only an INSTANT failure is the lost-connection case. A request
                // that ran for a while and then died is not re-sent on its own:
                // repeating slow work behind the user's back only piles it up.
                const instant = Date.now() - started < 1500;
                if (attempt >= 2 || !instant || (!repeatable && attempt >= 1)) throw err;
                await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
            }
        }
    }

    async function api(path, body) {
        // (the signal fetchWithRetry adds also opts the request out of the
        // app's 2.5s GET sharing, so a folder listing is never a stale one)
        // Everything here except applying tags only reads or is safe to repeat.
        const resp = await fetchWithRetry(API + path, body ? {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        } : {}, path !== '/tag-apply');
        let data = {};
        try { data = await resp.json(); } catch (_) { /* non-JSON error page */ }
        if (!resp.ok || data.success === false) throw new Error(data.error || `Request failed (${resp.status})`);
        return data;
    }

    function getProcess(id) {
        try {
            // eslint-disable-next-line no-undef
            return typeof activeDownloadProcesses !== 'undefined' ? activeDownloadProcesses[id] : null;
        } catch (_) { return null; }
    }

    function payload(process) {
        return {
            album: process.album || {},
            artist: process.artist || {},
            tracks: process.tracks || [],
            source: process.source || (process.artist && process.artist.source) || '',
        };
    }

    // ── 1. library check on open ─────────────────────────────────────────

    async function runCheck(id, state) {
        const process = getProcess(id);
        if (!process || process.status !== 'idle') return;
        const label = document.getElementById(`analysis-progress-text-${id}`);
        if (label) label.textContent = 'Checking library…';
        try {
            const data = await api('/check', payload(process));
            // a download run started meanwhile owns these cells now
            if ((getProcess(id) || {}).status !== 'idle') return;
            // Elements are looked up by id / walked, never by a CSS selector built
            // from the modal id: real ids contain "::" (702041953::soulsync), which
            // is not valid inside a selector.
            const tbody = document.getElementById(`download-tracks-tbody-${id}`);
            const boxes = new Map();
            if (tbody) {
                for (const box of tbody.getElementsByClassName('track-select-cb')) boxes.set(String(box.dataset.trackIndex), box);
            }
            for (const row of data.tracks) {
                const cell = document.getElementById(`match-${id}-${row.index}`);
                if (cell) {
                    cell.textContent = row.found ? '✅ Found' : '❌ Missing';
                    cell.className = `track-match-status ${row.found ? 'match-found' : 'match-missing'}`;
                    cell.title = row.found ? (row.file || row.library_title || '') : '';
                }
                // leave owned tracks unticked so "Download missing" means missing
                const box = boxes.get(String(row.index));
                if (box && row.found) box.checked = false;
            }
            try {
                if (typeof window.updateTrackSelectionCount === 'function') window.updateTrackSelectionCount(id);
            } catch (err) { console.warn('[fork] selection count not refreshed:', err); }
            if (label) label.textContent = `${data.found} of ${data.total} in library`;
            updateCard(process.album, data);
            const fill = document.getElementById(`analysis-progress-fill-${id}`);
            if (fill) fill.style.width = '100%';
            // a folder saved for the album always wins; otherwise keep one that
            // was picked but could not be saved over the library's guess
            if (data.folder_saved || !state.folderChosen) setFolder(state, data.folder || '', !!data.folder_saved);
            if (data.saved_missing && !state.folderChosen) {
                state.pathEl.title = `The folder saved for this album is gone: ${data.saved_missing}`;
            }
        } catch (err) {
            console.warn('[fork] album check failed:', err);
            if (!label) return;
            // say what went wrong and offer another go, instead of a dead end
            label.replaceChildren(
                `Library check failed: ${(err && err.message) || 'unknown error'} `,
                el('a', {
                    href: '#', class: 'fork-album-retry', text: 'Retry',
                    onclick: (e) => { e.preventDefault(); runCheck(id, state); },
                }),
            );
        }
    }

    // The album's card in the discography shows this same analysis; bring it
    // up to date now instead of on the next visit to the artist.
    function updateCard(album, data) {
        const albumId = album && album.id;
        if (!albumId || !data.total) return;
        const completion = {
            id: albumId,
            name: album.name || '',
            status: data.found >= data.total ? 'completed' : data.found ? 'partial' : 'missing',
            owned_tracks: data.found,
            expected_tracks: data.total,
            completion_percentage: Math.round((data.found / data.total) * 1000) / 10,
            confidence: data.found ? 1 : 0,
        };
        // the artist page (React) listens for this and repaints the card
        try { window.dispatchEvent(new CustomEvent('fork:album-checked', { detail: completion })); } catch (_) { /* old browser */ }
        try {
            for (const [containerId, type] of [['album-cards-container', 'albums'], ['singles-cards-container', 'singles']]) {
                const container = document.getElementById(containerId);
                if (!container) continue;
                const cards = container.querySelectorAll('[data-album-id]');
                if (![...cards].some((card) => card.dataset.albumId === String(albumId))) continue;
                if (typeof window.updateAlbumCompletionOverlay !== 'function') continue;
                window.updateAlbumCompletionOverlay(completion, type);
                // the page keeps what it was told per artist and paints from it again
                // eslint-disable-next-line no-undef
                const cache = typeof artistsPageState !== 'undefined' ? artistsPageState.cache.completionData : null;
                for (const entry of Object.values(cache || {})) {
                    for (const item of (entry && entry[type]) || []) {
                        if (String(item.id) === String(albumId)) Object.assign(item, completion);
                    }
                }
            }
        } catch (err) { console.warn('[fork] album card not refreshed:', err); }
    }

    // ── 2. local files bar ───────────────────────────────────────────────

    function setFolder(state, folder, saved) {
        state.folder = folder;
        state.saved = !!(saved && folder);
        state.pathEl.textContent = folder || 'No folder found for this album in your library';
        state.pathEl.title = folder;
        state.pathEl.classList.toggle('fork-album-none', !folder);
        state.tagBtn.disabled = !folder;
        state.savedEl.hidden = !state.saved;
        state.forgetBtn.hidden = !state.saved;
    }

    // Remember the picked folder for this album (an empty one forgets it), then
    // check again so Found/Missing reflects what that folder holds.
    async function saveFolder(state, folder) {
        const process = getProcess(state.id);
        if (!process) return;
        const body = payload(process);
        delete body.tracks;
        try {
            const data = await api('/folder', Object.assign(body, { folder }));
            state.folderChosen = false;
            setFolder(state, data.folder || '', true);
        } catch (err) {
            console.warn('[fork] album folder not saved:', err);
            // still usable for this visit, just not remembered
            state.folderChosen = !!folder;
            setFolder(state, folder, false);
            state.pathEl.title = `${folder}\nNot saved: ${(err && err.message) || 'unknown error'}`;
        }
        runCheck(state.id, state);
    }

    function addBar(id, modal) {
        const section = modal.querySelector('.download-tracks-section');
        if (!section || modal.querySelector('.fork-album-bar')) return null;
        const state = { id, folder: '', folderChosen: false, saved: false };
        state.savedEl = el('span', {
            class: 'fork-album-saved', text: 'Saved', hidden: true,
            title: 'You picked this folder for the album. It is used every time the album is opened.',
        });
        state.forgetBtn = el('button', {
            class: 'download-control-btn secondary', type: 'button', text: 'Forget', hidden: true,
            title: 'Stop using the saved folder and let SoulSync look for the album again',
            onclick: () => saveFolder(state, ''),
        });
        state.pathEl = el('span', { class: 'fork-album-path fork-album-none', text: 'Looking for this album in your library…' });
        state.tagBtn = el('button', {
            class: 'download-control-btn', type: 'button', text: 'Auto-tag…', disabled: true,
            title: 'Match the files in this folder to the album and review the tags before writing them',
            onclick: () => openTagger(state),
        });
        const bar = el('div', { class: 'fork-album-bar' }, [
            el('span', { class: 'fork-album-label', text: 'Local files' }),
            state.pathEl,
            state.savedEl,
            el('button', {
                class: 'download-control-btn secondary', type: 'button', text: 'Change…',
                title: 'Pick the folder that holds this album. It is remembered for this album.',
                onclick: () => openBrowser(state.folder, (picked) => saveFolder(state, picked)),
            }),
            state.forgetBtn,
            state.tagBtn,
        ]);
        section.parentNode.insertBefore(bar, section);
        return state;
    }

    function relabel(id) {
        const btn = document.getElementById(`begin-analysis-btn-${id}`);
        if (btn) {
            btn.textContent = 'Download missing';
            btn.title = 'Search for and download the ticked tracks that are not in your library';
        }
    }

    // ── folder browser ───────────────────────────────────────────────────

    function overlay(className, children) {
        const node = el('div', { class: 'fork-album-overlay' }, el('div', { class: `fork-album-dialog ${className}` }, children));
        node.addEventListener('mousedown', (e) => { if (e.target === node) node.remove(); });
        document.body.append(node);
        return node;
    }

    function openBrowser(start, onPick) {
        const list = el('div', { class: 'fork-album-dirlist' });
        const crumb = el('div', { class: 'fork-album-crumb' });
        let current = '';
        let searchTimer = null;
        let searchSeq = 0;
        const search = el('input', {
            class: 'fork-album-input fork-album-search', type: 'search',
            placeholder: 'Search your library for a folder (artist or album name)…',
            'aria-label': 'Search library folders',
            oninput: () => {
                clearTimeout(searchTimer);
                searchTimer = setTimeout(runSearch, 250);
            },
            onkeydown: (e) => {
                if (e.key === 'Escape' && search.value) { e.stopPropagation(); search.value = ''; runSearch(); }
            },
        });
        const pick = el('button', {
            class: 'download-control-btn primary', type: 'button', text: 'Use this folder', disabled: true,
            onclick: () => { onPick(current); root.remove(); },
        });
        const root = overlay('fork-album-browser', [
            el('h3', { text: 'Choose the album folder' }),
            search, crumb, list,
            el('div', { class: 'fork-album-actions' }, [
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Cancel', onclick: () => root.remove() }),
                pick,
            ]),
        ]);

        async function load(path) {
            list.replaceChildren(el('div', { class: 'fork-album-note', text: 'Loading…' }));
            let data;
            try {
                data = await api('/browse' + (path ? '?path=' + encodeURIComponent(path) : ''));
            } catch (err) {
                // a stale or foreign starting folder: fall back to the library roots
                if (path) return load('');
                list.replaceChildren(el('div', { class: 'fork-album-note', text: err.message }));
                return;
            }
            current = data.path || '';
            pick.disabled = !current;
            crumb.textContent = current ? `${current}${data.audio ? `  ·  ${data.audio} audio files` : ''}` : 'Library folders';
            const rows = [];
            if (current) {
                rows.push(el('button', {
                    class: 'fork-album-dir', type: 'button', text: '↑  Up one level',
                    onclick: () => load(data.parent || ''),
                }));
            }
            for (const dir of data.dirs) {
                rows.push(el('button', { class: 'fork-album-dir', type: 'button', onclick: () => load(dir.path) }, [
                    el('span', { text: dir.name }),
                    dir.audio ? el('small', { text: `${dir.audio} audio` }) : null,
                ]));
            }
            if (!data.dirs.length) rows.push(el('div', { class: 'fork-album-note', text: 'No sub-folders here.' }));
            list.replaceChildren(...rows);
        }
        // Search the whole library by folder name; picking a result opens that
        // folder in the browser so its contents can be checked before using it.
        async function runSearch() {
            const query = search.value.trim();
            const seq = ++searchSeq;
            if (!query) { load(current); return; }
            pick.disabled = true;
            crumb.textContent = `Searching for “${query}”…`;
            let data;
            try {
                data = await api('/search-folders?q=' + encodeURIComponent(query));
            } catch (err) {
                if (seq !== searchSeq) return;
                list.replaceChildren(el('div', { class: 'fork-album-note', text: err.message }));
                return;
            }
            if (seq !== searchSeq) return;   // a newer search (or a cleared box) superseded this one
            crumb.textContent = `${data.results.length}${data.truncated ? '+' : ''} folder${data.results.length === 1 ? '' : 's'} matching “${query}”`;
            if (!data.results.length) {
                list.replaceChildren(el('div', { class: 'fork-album-note', text: 'No folder with that name in your library.' }));
                return;
            }
            list.replaceChildren(...data.results.map((dir) => el('button', {
                class: 'fork-album-dir fork-album-result', type: 'button', title: dir.path,
                onclick: () => { search.value = ''; searchSeq++; load(dir.path); },
            }, [
                el('span', {}, [el('strong', { text: dir.name }), el('em', { text: dir.rel })]),
                dir.audio ? el('small', { text: `${dir.audio} audio` }) : null,
            ])));
        }

        load(start || '');
        setTimeout(() => search.focus(), 0);
    }

    // ── 3. auto-tag review ───────────────────────────────────────────────

    // The dialog's checkboxes are remembered per browser, so a choice such as
    // "don't translate with the model" holds for the next album too.
    const TAGGER_OPTIONS_KEY = 'soulsync-fork.auto-tag.options';
    const TAGGER_DEFAULTS = { applyRules: true, translate: true, semicolons: true, rename: false, cover: true };

    // every tag the dialog can write; each has a checkbox, all on by default
    const TAG_KEYS = ['album', 'year'].concat(FIELDS.map((f) => f[0]));

    function loadTaggerOptions() {
        const opts = Object.assign({}, TAGGER_DEFAULTS, { fields: {} });
        for (const key of TAG_KEYS) opts.fields[key] = true;
        try {
            const saved = JSON.parse(window.localStorage.getItem(TAGGER_OPTIONS_KEY) || '{}');
            for (const key of Object.keys(TAGGER_DEFAULTS)) {
                if (typeof saved[key] === 'boolean') opts[key] = saved[key];
            }
            for (const key of TAG_KEYS) {
                if (saved.fields && saved.fields[key] === false) opts.fields[key] = false;
            }
        } catch (_) { /* unreadable storage: defaults */ }
        return opts;
    }

    function saveTaggerOptions(opts) {
        try {
            const out = {};
            for (const key of Object.keys(TAGGER_DEFAULTS)) out[key] = !!opts[key];
            out.fields = Object.assign({}, opts.fields);
            window.localStorage.setItem(TAGGER_OPTIONS_KEY, JSON.stringify(out));
        } catch (_) { /* storage blocked: the choice still holds for this dialog */ }
    }

    function openTagger(state) {
        const process = getProcess(state.id);
        if (!process || !state.folder) return;
        const opts = loadTaggerOptions();
        let closed = false;
        let translateRun = 0;      // bumps whenever a newer preview/translation supersedes the running one
        let albumEdited = false;
        let data = null;
        let rows = [];   // {rel, current, track, include, tags:{}, reason}

        const body = el('div', { class: 'fork-album-tagbody' });
        const status = el('span', { class: 'fork-album-status' });
        const applyBtn = el('button', { class: 'download-control-btn primary', type: 'button', text: 'Apply tags', disabled: true });
        const albumInput = el('input', { class: 'fork-album-input', 'aria-label': 'Album', oninput: () => { albumEdited = true; } });
        const translateNote = el('div', { class: 'fork-album-translating' });
        const artistMode = el('span', { class: 'fork-album-mode' });
        const yearInput = el('input', { class: 'fork-album-input fork-album-year', 'aria-label': 'Year' });

        const check = (label, key, help, onChange) => el('label', { class: 'fork-album-check', title: help }, [
            el('input', {
                type: 'checkbox', checked: opts[key],
                onchange: (e) => { opts[key] = e.target.checked; saveTaggerOptions(opts); if (onChange) onChange(); },
            }),
            label,
        ]);

        // The cover to embed: the release's own unless an image is uploaded.
        // An upload belongs to this dialog only; the checkbox is remembered.
        const sourceCover = (process.album && (process.album.image_url
            || (process.album.images && process.album.images[0] && process.album.images[0].url))) || '';
        let upload = null;     // {data: base64, preview: data URL}
        const coverArt = el('div', { class: 'fork-album-coverart' });
        const coverNote = el('span', { class: 'fork-album-covernote' });
        const coverReset = el('button', {
            class: 'download-control-btn secondary', type: 'button', text: 'Use release cover',
            onclick: () => { upload = null; paintCover(); },
        });
        const coverPicker = el('input', {
            type: 'file', accept: '.jpg,.jpeg,.png,image/jpeg,image/png', hidden: true,
            onchange: () => {
                const file = coverPicker.files && coverPicker.files[0];
                coverPicker.value = '';
                if (!file) return;
                if (!/^image\/(jpeg|png)$/.test(file.type)) { toast('The cover must be a JPG or PNG image', 'error'); return; }
                if (file.size > 20 * 1024 * 1024) { toast('The cover is larger than 20 MB', 'error'); return; }
                const reader = new FileReader();
                reader.onload = () => {
                    const url = String(reader.result);
                    upload = { data: url.slice(url.indexOf(',') + 1), preview: url };
                    paintCover();
                };
                reader.readAsDataURL(file);
            },
        });
        const coverUpload = el('button', {
            class: 'download-control-btn secondary', type: 'button', text: 'Upload image…',
            title: 'Use a JPG or PNG of your own instead of the release cover', onclick: () => coverPicker.click(),
        });
        const coverChoice = () => (!opts.cover ? null : upload ? { action: 'set', data: upload.data }
            : sourceCover ? { action: 'source' } : null);
        function paintCover() {
            const shown = upload ? upload.preview : sourceCover;
            coverArt.replaceChildren(shown ? el('img', { src: shown, alt: 'New cover', loading: 'lazy' }) : el('span', { text: '♪' }));
            coverArt.classList.toggle('off', !opts.cover);
            coverNote.textContent = !opts.cover ? 'Covers stay as they are'
                : upload ? 'Your image' : sourceCover ? 'Release cover' : 'The release has no cover — upload an image';
            coverUpload.disabled = !opts.cover;
            coverReset.hidden = !upload || !sourceCover;
            coverReset.disabled = !opts.cover;
            refreshStatus();
        }

        // One checkbox per tag: an unticked tag is left as it is in every file.
        const fieldToggle = (key, label) => el('input', {
            type: 'checkbox', checked: opts.fields[key], class: 'fork-album-fieldcb',
            title: `Write ${label}. Untick to leave this tag as it is in the files.`,
            'aria-label': `Write ${label}`,
            onchange: (e) => { opts.fields[key] = e.target.checked; saveTaggerOptions(opts); applyFieldStates(); },
        });
        function applyFieldStates() {
            albumInput.disabled = !opts.fields.album;
            yearInput.disabled = !opts.fields.year;
            for (const input of body.querySelectorAll('input[data-field]')) {
                input.disabled = !opts.fields[input.dataset.field];
            }
            refreshStatus();
        }

        const root = overlay('fork-album-tagger', [
            el('h3', { text: `Auto-tag: ${(process.album && process.album.name) || 'album'}` }),
            el('div', { class: 'fork-album-crumb', text: state.folder }),
            el('div', { class: 'fork-album-albumrow' }, [
                el('label', {}, [fieldToggle('album', 'Album'), 'Album', albumInput]),
                el('label', {}, [fieldToggle('year', 'Year'), 'Year', yearInput]),
            ]),
            el('div', { class: 'fork-album-options' }, [
                check('Use my artist rules and saved translations', 'applyRules',
                    'Off: propose exactly what the metadata source reports.', () => load()),
                check('Translate new names with the model', 'translate',
                    'Names with no saved translation are translated in the background while you review. Off: skip that step — untranslated names stay as they are and nothing waits for the model.',
                    () => { if (opts.translate) load(); else { translateRun++; translateNote.textContent = ''; } }),
                check('Separate multiple artists', 'semicolons',
                    'Splits "A, B", "A & B" or "A feat. B" in Artist and Album artist into individual artists. A name MusicBrainz or your rules know as one artist is left whole. How they are stored is set in LLM & Tagging → Artists.', () => load()),
                artistMode,
                check('Also rename/move files to my path format', 'rename',
                    'Off (default): only tags change; files stay where they are.'),
            ]),
            el('div', { class: 'fork-album-coverrow' }, [
                coverArt,
                check('Update cover', 'cover',
                    'Replaces the cover embedded in every ticked file (and an existing cover.jpg / cover.png in the folder). Off: covers stay as they are.',
                    () => paintCover()),
                coverNote, coverUpload, coverReset, coverPicker,
            ]),
            translateNote,
            body,
            el('div', { class: 'fork-album-actions' }, [
                status,
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Cancel', onclick: () => { closed = true; root.remove(); } }),
                applyBtn,
            ]),
        ]);

        const proposedFor = (trackIndex) => (data.tracks[trackIndex] ? { ...data.tracks[trackIndex].proposed } : null);

        function refreshStatus() {
            if (!data) return;
            const chosen = rows.filter((r) => r.include).length;
            const writing = TAG_KEYS.filter((key) => opts.fields[key]).length;
            const cover = !!coverChoice();
            status.textContent = (writing ? `${chosen} of ${rows.length} files will be tagged`
                : cover ? `No tag is ticked: only the cover of ${chosen} of ${rows.length} files changes` : 'No tag is ticked: nothing to write')
                + (writing && writing < TAG_KEYS.length ? ` (${writing} of ${TAG_KEYS.length} tags)` : '')
                + (writing && cover ? ' · cover updated' : '')
                + (data.unmatched_tracks.length ? ` · ${data.unmatched_tracks.length} album tracks have no file` : '');
            applyBtn.disabled = chosen === 0 || (writing === 0 && !cover);
        }

        function renderRow(row) {
            const tr = el('tr', { class: row.include ? '' : 'fork-album-off' });
            const inputs = {};
            const fill = () => {
                for (const [key] of FIELDS) inputs[key].value = row.tags[key] != null ? row.tags[key] : '';
            };
            const select = el('select', {
                class: 'fork-album-input', 'aria-label': 'Album track',
                onchange: (e) => {
                    row.track = e.target.value === '' ? null : Number(e.target.value);
                    const proposed = row.track == null ? null : proposedFor(row.track);
                    row.tags = proposed || { ...row.current };
                    row.edited = {};
                    row.include = row.track != null;
                    include.checked = row.include;
                    tr.className = row.include ? '' : 'fork-album-off';
                    fill();
                    refreshStatus();
                },
            }, [el('option', { value: '', text: '— not on this album —' })].concat(data.tracks.map((t) => el('option', {
                value: String(t.index), selected: row.track === t.index,
                text: `${t.disc > 1 ? t.disc + '-' : ''}${String(t.number).padStart(2, '0')}  ${t.source.title}`,
            }))));
            const include = el('input', {
                type: 'checkbox', checked: row.include, 'aria-label': 'Tag this file',
                onchange: (e) => { row.include = e.target.checked; tr.className = row.include ? '' : 'fork-album-off'; refreshStatus(); },
            });
            tr.append(
                el('td', {}, include),
                el('td', { class: 'fork-album-file' }, [
                    el('div', { text: row.rel, title: row.rel }),
                    el('small', { text: row.reason ? `matched by ${row.reason}` : 'no match found' }),
                ]),
                el('td', {}, select),
            );
            for (const [key, label, _w] of FIELDS) {
                const numeric = key.endsWith('_number');
                inputs[key] = el('input', {
                    class: 'fork-album-input' + (numeric ? ' fork-album-num' : ''), 'aria-label': label,
                    'data-field': key, disabled: !opts.fields[key],
                    oninput: (e) => { row.tags[key] = e.target.value; row.edited[key] = true; },
                });
                const was = row.current[key];
                tr.append(el('td', {}, [
                    inputs[key],
                    el('small', { class: 'fork-album-was', text: was ? `was: ${was}` : 'was empty', title: String(was || '') }),
                ]));
            }
            fill();
            return tr;
        }

        function render() {
            if (!rows.length) {
                body.replaceChildren(el('div', { class: 'fork-album-note', text: 'No audio files in this folder.' }));
                refreshStatus();
                return;
            }
            body.replaceChildren(el('table', { class: 'fork-album-table' }, [
                el('thead', {}, el('tr', {}, ['', 'File', 'Album track'].map((t) => el('th', { text: t })).concat(
                    FIELDS.map(([key, label]) => el('th', {}, el('label', { class: 'fork-album-fieldhead' }, [fieldToggle(key, label), label])))))),
                el('tbody', {}, rows.map(renderRow)),
            ]));
            applyFieldStates();
        }

        async function load() {
            applyBtn.disabled = true;
            status.textContent = '';
            translateRun++;
            translateNote.textContent = '';
            body.replaceChildren(el('div', { class: 'fork-album-note', text: 'Reading files and preparing tags…' }));
            try {
                data = await api('/tag-preview', Object.assign(payload(process), { folder: state.folder, apply_rules: opts.applyRules, semicolons: opts.semicolons }));
            } catch (err) {
                // never a dead end: say what failed and offer another go
                body.replaceChildren(el('div', { class: 'fork-album-note' }, [
                    el('div', { text: `Could not prepare the tags: ${err.message}` }),
                    el('button', {
                        class: 'download-control-btn', type: 'button', text: 'Try again',
                        style: 'margin-top:12px', onclick: () => load(),
                    }),
                ]));
                return;
            }
            const mode = data.artist_mode || {};
            artistMode.textContent = !opts.semicolons ? ''
                : mode.split_tags ? 'Each artist is written as its own tag; type “;” between artists.'
                    : `Artists are written as one tag joined with “${mode.separator}”.`;
            const first = data.tracks[0] ? data.tracks[0].proposed : {};
            albumInput.value = first.album || '';
            yearInput.value = first.year || '';
            rows = data.rows.map((r) => ({
                rel: r.rel, current: r.current, track: r.track, reason: r.reason,
                include: r.track != null, edited: {},
                tags: r.track != null ? proposedFor(r.track) : { ...r.current },
            }));
            albumEdited = false;
            render();
            translateMissing();
        }

        // Names without a saved translation are translated in the background;
        // the table stays usable meanwhile and picks the results up when ready.
        async function translateMissing() {
            const run = ++translateRun;
            translateNote.textContent = '';
            const pending = (data && data.untranslated) || [];
            if (!opts.applyRules || !opts.translate || !pending.length) return;
            const live = () => !closed && run === translateRun;
            translateNote.textContent = `Translating ${pending.length} name${pending.length === 1 ? '' : 's'} with the model — you can keep reviewing…`;
            let job;
            try {
                job = (await api('/translate', { items: pending })).job;
                while (job.running) {
                    await new Promise((resolve) => setTimeout(resolve, 1000));
                    if (!live()) return;
                    job = (await api(`/translate?t=${Date.now()}`)).job;
                    translateNote.textContent = job.phase === 'loading'
                        ? 'Loading the model (the first time can take a couple of minutes) — you can keep reviewing…'
                        : `Translating names with the model: ${job.done} of ${job.total} — you can keep reviewing…`;
                }
            } catch (err) {
                if (live()) translateNote.textContent = `Translation did not finish: ${err.message}. The names below are untranslated; you can edit them or apply as they are.`;
                return;
            }
            if (!live()) return;
            if (job.error) {
                translateNote.textContent = `Translation did not finish: ${job.error} The names below are untranslated; you can edit them or apply as they are.`;
                return;
            }
            // fetch the proposals again (saved translations only, so this is
            // instant) and fill them in wherever nothing was typed by hand
            let fresh;
            try {
                fresh = await api('/tag-preview', Object.assign(payload(process), { folder: state.folder, apply_rules: opts.applyRules, semicolons: opts.semicolons }));
            } catch (err) { translateNote.textContent = `Translated, but the table could not be refreshed: ${err.message}`; return; }
            if (!live()) return;
            data.tracks = fresh.tracks;
            data.untranslated = fresh.untranslated;
            for (const row of rows) {
                if (row.track == null || !fresh.tracks[row.track]) continue;
                const proposed = fresh.tracks[row.track].proposed;
                for (const key of Object.keys(proposed)) {
                    if (!row.edited[key]) row.tags[key] = proposed[key];
                }
            }
            if (!albumEdited && fresh.tracks[0]) albumInput.value = fresh.tracks[0].proposed.album || albumInput.value;
            render();
            const left = (fresh.untranslated || []).length;
            translateNote.textContent = left
                ? `Translated ${job.translated} name${job.translated === 1 ? '' : 's'}; ${left} could not be translated and are left as they are.`
                : `Translated ${job.translated} name${job.translated === 1 ? '' : 's'}.`;
        }

        applyBtn.addEventListener('click', async () => {
            const chosen = rows.filter((r) => r.include);
            if (!chosen.length) return;
            applyBtn.disabled = true;
            status.textContent = 'Writing tags…';
            try {
                const result = await api('/tag-apply', Object.assign(payload(process), {
                    folder: state.folder,
                    rename: opts.rename,
                    apply_rules: opts.applyRules,
                    semicolons: opts.semicolons,
                    fields: TAG_KEYS.filter((key) => opts.fields[key]),
                    cover: coverChoice(),
                    // an unticked tag is sent as the file has it now, so a
                    // rename still builds the path from the real values
                    rows: chosen.map((r) => {
                        const tags = Object.assign({}, r.tags, { album: albumInput.value, year: yearInput.value });
                        for (const key of TAG_KEYS) {
                            if (!opts.fields[key]) tags[key] = r.current[key] != null ? r.current[key] : '';
                        }
                        return { rel: r.rel, track: r.track, tags };
                    }),
                }));
                const problems = result.results.filter((r) => !r.ok || r.rename_error || r.cover_error);
                toast(`Tagged ${result.written} file${result.written === 1 ? '' : 's'}`
                    + (result.moved ? `, moved ${result.moved}` : '')
                    + (problems.length ? ` — ${problems.length} with problems` : ''), problems.length ? 'error' : 'success');
                if (problems.length) {
                    status.textContent = problems.slice(0, 3).map((p) => `${p.rel}: ${p.error || p.rename_error || p.cover_error}`).join(' · ');
                    applyBtn.disabled = false;
                    return;
                }
                root.remove();
                runCheck(state.id, state);
            } catch (err) {
                status.textContent = err.message;
                applyBtn.disabled = false;
            }
        });

        paintCover();
        load();
    }

    // ── Album Volume Grouping: edit a set before it is grouped ───────────
    // Opened from the "Edit…" button of a finding on the Tools page.

    async function forkApi(path, body) {
        const resp = await fetchWithRetry('/api/fork' + path, body ? {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        } : {}, true);
        let data = {};
        try { data = await resp.json(); } catch (_) { /* non-JSON error page */ }
        if (!resp.ok || data.success === false) throw new Error(data.error || `Request failed (${resp.status})`);
        return data;
    }

    function openVolumeEditor(finding) {
        const details = (finding && finding.details) || {};
        let items = (details.volumes || []).map((v) => Object.assign({}, v));
        const keyOf = (item) => (item.album_id != null ? `a:${item.album_id}` : `f:${item.folder}`);
        const nextNumber = () => {
            const used = new Set(items.map((i) => Number(i.number)));
            let n = 1;
            while (used.has(n)) n++;
            return n;
        };

        const albumInput = el('input', { class: 'fork-album-input', 'aria-label': 'Album name', value: details.album || '' });
        const list = el('div', { class: 'fork-volume-list' });
        const status = el('span', { class: 'fork-album-status' });
        const results = el('div', { class: 'fork-album-dirlist fork-volume-results' });
        const saveBtn = el('button', { class: 'download-control-btn primary', type: 'button', text: 'Save' });

        function problem() {
            if (!albumInput.value.trim()) return 'The album needs a name';
            if (items.length < 2) return 'A set needs at least two items';
            const numbers = items.map((i) => Number(i.number));
            if (numbers.some((n) => !Number.isInteger(n) || n < 1 || n > 99)) return 'Disc numbers go from 1 to 99';
            if (new Set(numbers).size !== numbers.length) return 'Two items have the same disc number';
            return '';
        }

        function refresh() {
            const why = problem();
            const tracks = items.reduce((sum, i) => sum + (Number(i.tracks) || 0), 0);
            status.textContent = why || `${items.length} discs, ${tracks} tracks`;
            status.classList.toggle('fork-volume-problem', !!why);
            saveBtn.disabled = !!why;
        }

        function render() {
            list.replaceChildren(...(items.length ? items : [null]).map((item) => {
                if (!item) return el('div', { class: 'fork-album-note', text: 'Nothing in this set. Add albums or folders below.' });
                return el('div', { class: 'fork-volume-row' }, [
                    el('label', { class: 'fork-volume-disc' }, ['Disc', el('input', {
                        class: 'fork-album-input fork-album-num', type: 'number', min: '1', max: '99',
                        value: String(item.number), 'aria-label': `Disc number of ${item.title}`,
                        oninput: (e) => { item.number = Number(e.target.value); refresh(); },
                    })]),
                    el('div', { class: 'fork-volume-name' }, [
                        el('div', { text: item.title || item.folder || '', title: item.title || '' }),
                        el('small', {
                            text: [item.album_id != null ? 'Library album' : 'Folder',
                                item.tracks != null ? `${item.tracks} track${item.tracks === 1 ? '' : 's'}` : '',
                                item.assumed ? 'no volume marker: assumed to be the first' : '',
                                item.folder || ''].filter(Boolean).join('  ·  '),
                            title: item.folder || '',
                        }),
                    ]),
                    el('button', {
                        class: 'download-control-btn secondary', type: 'button', text: 'Remove',
                        title: 'Leave this one out of the set. Its files are not touched.',
                        onclick: () => { items = items.filter((i) => i !== item); render(); },
                    }),
                ]);
            }));
            refresh();
        }

        function add(item) {
            if (items.some((i) => keyOf(i) === keyOf(item))) { toast('Already in the set', 'error'); return; }
            items.push(Object.assign({ number: nextNumber() }, item));
            render();
        }

        let searchTimer = null;
        let searchSeq = 0;
        async function runSearch(query) {
            const seq = ++searchSeq;
            if (!query.trim()) { results.replaceChildren(); return; }
            let data;
            try { data = await forkApi('/volumes/search?q=' + encodeURIComponent(query)); } catch (err) {
                if (seq === searchSeq) results.replaceChildren(el('div', { class: 'fork-album-note', text: err.message }));
                return;
            }
            if (seq !== searchSeq) return;
            if (!data.albums.length) {
                results.replaceChildren(el('div', { class: 'fork-album-note', text: 'No album with that name in your library.' }));
                return;
            }
            results.replaceChildren(...data.albums.map((album) => el('div', {
                class: 'fork-album-dir', role: 'button', tabindex: '0', title: 'Add to the set',
                onclick: () => add({ album_id: album.album_id, title: album.title, tracks: album.tracks }),
            }, [
                el('span', { text: `${album.title}  —  ${album.artist}` }),
                el('small', { text: `${album.tracks} track${album.tracks === 1 ? '' : 's'}  ＋` }),
            ])));
        }

        const search = el('input', {
            class: 'fork-album-input fork-album-search', type: 'search',
            placeholder: 'Search library albums to add (album or artist name)…', 'aria-label': 'Search library albums',
            oninput: (e) => { clearTimeout(searchTimer); const q = e.target.value; searchTimer = setTimeout(() => runSearch(q), 250); },
        });

        saveBtn.addEventListener('click', async () => {
            saveBtn.disabled = true;
            status.textContent = 'Saving…';
            try {
                await forkApi(`/volumes/finding/${finding.id}`, {
                    album: albumInput.value,
                    items: items.map((i) => (i.album_id != null
                        ? { album_id: i.album_id, number: Number(i.number) }
                        : { folder: i.folder, number: Number(i.number) })),
                });
            } catch (err) {
                status.textContent = err.message;
                status.classList.add('fork-volume-problem');
                saveBtn.disabled = false;
                return;
            }
            root.remove();
            toast('Set saved. Press Group on the finding to apply it.');
            window.dispatchEvent(new Event('fork:findings-changed'));
        });
        albumInput.addEventListener('input', refresh);

        const root = overlay('fork-volume-editor', [
            el('h3', { text: 'Edit volume set' }),
            el('div', { class: 'fork-album-crumb', text: details.artist ? `Artist: ${details.artist}` : '' }),
            el('div', { class: 'fork-album-albumrow' }, [el('label', {}, ['Album name', albumInput])]),
            list,
            el('div', { class: 'fork-volume-add' }, [
                search,
                el('button', {
                    class: 'download-control-btn secondary', type: 'button', text: 'Add folder…',
                    title: 'Add a folder from your library as one disc (also for files SoulSync has not scanned)',
                    onclick: () => openBrowser('', (picked) => add({
                        folder: picked, title: picked.split('/').filter(Boolean).pop() || picked,
                    })),
                }),
            ]),
            results,
            el('div', { class: 'fork-album-actions' }, [
                status,
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Cancel', onclick: () => root.remove() }),
                saveBtn,
            ]),
        ]);
        render();
    }

    window.forkEditVolumeGroup = openVolumeEditor;

    // ── wiring ───────────────────────────────────────────────────────────

    function enhance(id) {
        const process = getProcess(id);
        const modal = process && process.modalElement;
        if (!modal || process.status !== 'idle') return;
        relabel(id);
        const state = addBar(id, modal);
        if (state) runCheck(id, state);
    }

    function install() {
        const original = window.openDownloadMissingModalForArtistAlbum;
        if (typeof original !== 'function' || original.__forkWrapped) return false;
        const wrapped = async function (virtualPlaylistId, ...rest) {
            const result = await original.call(this, virtualPlaylistId, ...rest);
            try {
                // playlist-style contexts (charts) are not one album in one folder
                if (rest[5] !== 'playlist') enhance(virtualPlaylistId);
            } catch (err) { console.warn('[fork] album pop-up additions failed:', err); }
            return result;
        };
        wrapped.__forkWrapped = true;
        window.openDownloadMissingModalForArtistAlbum = wrapped;
        return true;
    }

    // ── folders for a whole discography ──────────────────────────────────
    // Opened from the artist page ("Match folders…"). Looks up where each
    // release's files are, lets the user correct the list, then saves the
    // ticked folders so every later check reads them straight from there.

    const HOW = {
        saved: 'Saved',
        library: 'From your library',
        name: 'Folder with the album\'s name',
        picked: 'Picked by you',
    };

    function openFolderMatcher(options) {
        const artist = (options && options.artist) || {};
        const source = (options && options.source) || artist.source || '';
        const rows = ((options && options.releases) || []).filter((r) => r && r.id != null).map((r) => ({
            album: { id: String(r.id), name: r.name || '' }, type: r.type || '', state: 'waiting', folder: '', by: '',
            found: 0, total: 0, ticked: false,
        }));
        const list = el('div', { class: 'fork-volume-list fork-match-list' });
        const status = el('span', { class: 'fork-album-status' });
        const saveBtn = el('button', { class: 'download-control-btn primary', type: 'button', text: 'Save', disabled: true });
        let closed = false;
        let saving = false;

        const body = (row, extra) => Object.assign({ album: row.album, artist: { name: artist.name || '' }, source }, extra || {});
        const worth = (row) => row.by === 'library' || row.by === 'picked'
            || (row.by === 'name' && row.found >= Math.max(1, Math.ceil(row.total / 2)));

        function describe(row) {
            if (row.state === 'waiting') return 'Waiting…';
            if (row.state === 'looking') return 'Looking…';
            if (row.state === 'error') return `Could not check: ${row.error}`;
            if (!row.folder) return 'No folder found';
            const count = row.total ? `${row.found} of ${row.total} tracks there` : '';
            return [HOW[row.by] || '', count].filter(Boolean).join('  ·  ');
        }

        function refresh() {
            const done = rows.filter((r) => r.state === 'done' || r.state === 'error').length;
            const ticked = rows.filter((r) => r.ticked && r.folder && r.by !== 'saved').length;
            const saved = rows.filter((r) => r.by === 'saved').length;
            status.textContent = done < rows.length
                ? `Checking ${done} / ${rows.length}…`
                : `${rows.filter((r) => r.folder).length} of ${rows.length} have a folder (${saved} saved)`;
            saveBtn.textContent = ticked ? `Save ${ticked} folder${ticked === 1 ? '' : 's'}` : 'Save';
            saveBtn.disabled = saving || !ticked;
        }

        function paint(row) {
            const box = el('input', {
                type: 'checkbox', 'aria-label': `Save the folder for ${row.album.name}`,
                checked: row.ticked && !!row.folder && row.by !== 'saved',
                disabled: !row.folder || row.by === 'saved' || saving,
                onchange: (e) => { row.ticked = e.target.checked; refresh(); },
            });
            const node = el('div', { class: `fork-volume-row fork-match-row${row.by === 'saved' ? ' fork-match-saved' : ''}` }, [
                box,
                el('div', { class: 'fork-volume-name' }, [
                    el('div', { text: row.album.name + (row.type ? `  ·  ${row.type}` : ''), title: row.album.name }),
                    el('small', { text: row.folder || describe(row), title: row.folder }),
                    row.folder ? el('small', { text: describe(row) }) : null,
                ]),
                el('button', {
                    class: 'download-control-btn secondary', type: 'button', text: row.folder ? 'Change…' : 'Pick…',
                    title: 'Pick the folder that holds this album', disabled: saving,
                    onclick: () => openBrowser(row.folder, (picked) => {
                        Object.assign(row, { folder: picked, by: 'picked', ticked: true, state: 'done' });
                        paint(row);
                        refresh();
                    }),
                }),
            ]);
            if (row.node) row.node.replaceWith(node); else list.append(node);
            row.node = node;
        }

        async function look(row) {
            row.state = 'looking';
            paint(row);
            try {
                const data = await api('/suggest-folder', body(row));
                if (row.by === 'picked') return;   // the user was quicker
                Object.assign(row, { state: 'done', folder: data.folder || '', by: data.by || '', found: data.found || 0, total: data.total || 0 });
                row.ticked = worth(row);
            } catch (err) {
                Object.assign(row, { state: 'error', error: (err && err.message) || 'unknown error' });
            }
            paint(row);
            refresh();
        }

        async function lookAll() {
            let next = 0;
            const worker = async () => {
                while (!closed && next < rows.length) await look(rows[next++]);
            };
            await Promise.all([worker(), worker()]);
        }

        async function save() {
            saving = true;
            let saved = 0;
            const failed = [];
            for (const row of rows.filter((r) => r.ticked && r.folder && r.by !== 'saved')) {
                status.textContent = `Saving ${saved + 1}…`;
                try {
                    const data = await api('/folder', body(row, { folder: row.folder }));
                    const after = await api('/suggest-folder', body(row));
                    Object.assign(row, { folder: data.folder || row.folder, by: 'saved', ticked: false, found: after.found || 0, total: after.total || 0 });
                    updateCard(row.album, after);
                    saved++;
                } catch (err) {
                    failed.push(`${row.album.name}: ${(err && err.message) || 'unknown error'}`);
                }
                paint(row);
            }
            saving = false;
            rows.forEach(paint);
            refresh();
            if (failed.length) toast(`${failed.length} not saved — ${failed[0]}`, 'error');
            else toast(`Saved ${saved} folder${saved === 1 ? '' : 's'}`);
        }

        saveBtn.addEventListener('click', save);
        const root = overlay('fork-volume-editor fork-match-dialog', [
            el('h3', { text: `Match folders — ${artist.name || 'artist'}` }),
            el('div', {
                class: 'fork-album-note',
                text: 'Each release is looked up in your library. Ticked folders are saved for their album, so opening the '
                    + 'artist or the album later reads the files straight from there instead of searching for them again.',
            }),
            el('div', { class: 'fork-match-tools' }, [
                el('button', {
                    class: 'download-control-btn secondary', type: 'button', text: 'Tick all found',
                    onclick: () => { rows.forEach((r) => { if (r.folder && r.by !== 'saved') r.ticked = true; paint(r); }); refresh(); },
                }),
                el('button', {
                    class: 'download-control-btn secondary', type: 'button', text: 'Untick all',
                    onclick: () => { rows.forEach((r) => { r.ticked = false; paint(r); }); refresh(); },
                }),
            ]),
            list,
            el('div', { class: 'fork-album-actions' }, [
                status,
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Close', onclick: () => root.remove() }),
                saveBtn,
            ]),
        ]);
        new MutationObserver((_m, observer) => {
            if (!document.body.contains(root)) { closed = true; observer.disconnect(); }
        }).observe(document.body, { childList: true });
        rows.forEach(paint);
        refresh();
        if (!rows.length) status.textContent = 'This artist has no releases to match';
        lookAll();
    }

    window.forkMatchAlbumFolders = openFolderMatcher;

    // the folder picker is reused by the LLM & Tagging panel (fork-ui.js)
    window.forkOpenFolderBrowser = openBrowser;

    if (!install()) document.addEventListener('DOMContentLoaded', install);
})();
