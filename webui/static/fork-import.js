// fork (itsdmd/SoulSync): manual import, opened from the Import page.
//
// For entries of the import folder you want to tag yourself: the form loads
// each file's current tags, you edit them (and the cover), and on Import the
// tags are written into the files and SoulSync names, files and registers
// them by those tags and the configured path template. Nothing is looked up.
//
// Backed by /api/fork/import/manual* (api/fork.py, core/fork/manual_import.py).
// See FORK.md.
(function () {
    'use strict';

    const API = '/api/fork/import/manual';
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

    async function post(path, body) {
        const resp = await fetch(API + path, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        let data = {};
        try { data = await resp.json(); } catch (_) { /* non-JSON error page */ }
        if (!resp.ok || data.success === false) throw new Error(data.error || `Request failed (${resp.status})`);
        return data;
    }

    // the value most files agree on ('' when none has one)
    function common(files, field) {
        const counts = new Map();
        for (const f of files) {
            const value = (f.tags[field] || '').trim();
            if (value) counts.set(value, (counts.get(value) || 0) + 1);
        }
        return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || '';
    }

    // One entry of the import folder = one release. `next` opens the one after it.
    async function openEntry(entries, index, changed) {
        const entry = entries[index];
        if (!entry) { if (changed.count) window.dispatchEvent(new CustomEvent('fork:import-changed')); return; }
        const step = () => { root.remove(); openEntry(entries, index + 1, changed); };
        const status = el('span', { class: 'fork-album-status', text: 'Reading the files…' });
        const body = el('div', { class: 'fork-manual-body' });
        const importBtn = el('button', { class: 'download-control-btn primary', type: 'button', text: 'Import', disabled: true });
        const more = entries.length > 1 ? `  (${index + 1} of ${entries.length})` : '';
        const root = el('div', { class: 'fork-album-overlay' }, el('div', { class: 'fork-album-dialog fork-manual-dialog' }, [
            el('h3', { text: `Manual import — ${entry.name}${more}` }),
            el('div', {
                class: 'fork-album-note',
                text: 'The fields start from what the files already say. On Import these tags (and the cover) are written into the '
                    + 'files, which are then named and filed by them using your path template, and added to the library. Nothing is looked up online.',
            }),
            body,
            el('div', { class: 'fork-album-actions' }, [
                status,
                el('button', { class: 'download-control-btn secondary', type: 'button', text: index + 1 < entries.length ? 'Skip' : 'Cancel', onclick: step }),
                importBtn,
            ]),
        ]));
        root.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); step(); } });
        document.body.append(root);

        let data;
        try {
            data = await post('/load', { paths: entry.paths });
        } catch (err) { status.textContent = err.message; status.classList.add('fork-volume-problem'); return; }
        const files = data.files;
        if (!files.length) {
            status.textContent = data.errors[0] || 'No audio file could be read.';
            status.classList.add('fork-volume-problem');
            return;
        }

        const input = (value, label, extra) => el('input', Object.assign({ class: 'fork-album-input', type: 'text', value: value || '', 'aria-label': label }, extra || {}));
        const albumArtist = common(files, 'albumartist') || common(files, 'artist');
        const release = {
            album: input(common(files, 'album'), 'Album', { placeholder: files.length === 1 ? 'Empty: a single named after its title' : '' }),
            albumartist: input(albumArtist, 'Album artist'),
            date: input(common(files, 'date'), 'Date', { placeholder: '2024 or 2024-05-17' }),
            genre: input(common(files, 'genre'), 'Genre', { placeholder: 'Several: Rock; Pop' }),
        };
        const type = el('select', { class: 'fork-album-input', 'aria-label': 'Release type' }, [
            ['', `Automatic (${files.length === 1 ? 'single' : 'album'})`], ['album', 'Album'], ['ep', 'EP'], ['single', 'Single'], ['compilation', 'Compilation'],
        ].map(([value, text]) => el('option', { value, text })));

        // cover: keep what the files have, replace it for all of them, or remove it
        let cover = { action: 'keep' };
        const coverBox = el('div', { class: 'fork-editor-cover' });
        function paintCover() {
            const withCover = files.find((f) => f.cover);
            const distinct = new Set(files.map((f) => (f.cover ? f.cover.hash : ''))).size;
            let picture = null;
            let note = 'No cover';
            if (cover.action === 'set') { picture = el('img', { src: cover.preview, alt: 'New cover' }); note = 'New cover for every file'; }
            else if (cover.action === 'remove') note = 'Cover will be removed';
            else if (withCover) {
                picture = el('img', { src: `${API}/cover?${new URLSearchParams({ path: withCover.path, v: withCover.cover.hash })}`, alt: 'Cover' });
                note = distinct > 1 ? 'Files have different covers (each keeps its own)' : 'Cover in the files';
            }
            const picker = el('input', {
                type: 'file', accept: 'image/jpeg,image/png', hidden: true,
                onchange: () => {
                    const file = picker.files && picker.files[0];
                    if (!file) return;
                    const reader = new FileReader();
                    reader.onload = () => {
                        const url = String(reader.result);
                        cover = { action: 'set', data: url.slice(url.indexOf(',') + 1), preview: url };
                        paintCover();
                    };
                    reader.readAsDataURL(file);
                },
            });
            coverBox.replaceChildren(
                el('div', { class: `fork-editor-art${cover.action !== 'keep' ? ' changed' : ''}` }, picture || el('span', { text: '♪' })),
                el('small', { text: note }),
                el('div', { class: 'fork-editor-coverbtns' }, [
                    el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Replace…', onclick: () => picker.click() }),
                    el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Remove', disabled: cover.action === 'remove', onclick: () => { cover = { action: 'remove' }; paintCover(); } }),
                    cover.action !== 'keep' ? el('button', { class: 'fork-editor-undo', type: 'button', text: '↺', title: 'Keep the cover as it is', onclick: () => { cover = { action: 'keep' }; paintCover(); } }) : null,
                ]),
                picker,
            );
        }
        paintCover();

        const rows = files.map((f, i) => ({
            file: f,
            track: input(f.tags.tracknumber || String(i + 1), `Track number of ${f.name}`),
            disc: input(f.tags.discnumber || '', `Disc number of ${f.name}`, { placeholder: '1' }),
            title: input(f.tags.title || f.name.replace(/\.[^.]+$/, ''), `Title of ${f.name}`),
            artist: input(f.tags.artist || albumArtist, `Artist of ${f.name}`),
        }));
        const field = (label, node) => el('label', { class: 'fork-manual-field' }, [el('span', { text: label }), node]);
        body.replaceChildren(
            el('div', { class: 'fork-manual-top' }, [
                coverBox,
                el('div', { class: 'fork-manual-release' }, [
                    field('Album', release.album), field('Album artist', release.albumartist),
                    field('Date', release.date), field('Genre', release.genre), field('Type', type),
                ]),
            ]),
            el('div', { class: 'fork-manual-files' }, [
                el('div', { class: 'fork-manual-row fork-editor-taghead' }, ['#', 'Disc', 'Title', 'Artist', 'File'].map((t) => el('span', { text: t }))),
                ...rows.map((r) => el('div', { class: 'fork-manual-row' }, [r.track, r.disc, r.title, r.artist, el('span', { class: 'fork-manual-file', text: r.file.name, title: r.file.path })])),
            ]),
        );
        const count = `${files.length} file${files.length === 1 ? '' : 's'}`;
        status.textContent = data.errors.length ? `${count}; ${data.errors.length} could not be read` : count;
        importBtn.disabled = false;
        importBtn.textContent = `Import ${count}`;

        importBtn.addEventListener('click', async () => {
            status.classList.remove('fork-volume-problem');
            const payload = rows.map((r) => ({
                path: r.file.path, length: r.file.length,
                tags: {
                    title: r.title.value, artist: r.artist.value, tracknumber: r.track.value, discnumber: r.disc.value,
                    album: release.album.value, albumartist: release.albumartist.value, date: release.date.value, genre: release.genre.value,
                },
            }));
            importBtn.disabled = true;
            status.textContent = 'Writing tags and importing…';
            try {
                const done = await post('', { files: payload, album_type: type.value, cover: cover.action === 'keep' ? null : { action: cover.action, data: cover.data } });
                changed.count += done.processed;
                if (done.errors.length) toast(`${done.processed} of ${done.total} imported — ${done.errors[0]}`, 'error');
                else toast(`Imported ${done.processed} track${done.processed === 1 ? '' : 's'}: ${done.artist} — ${done.album}`);
                step();
            } catch (err) {
                status.textContent = err.message;
                status.classList.add('fork-volume-problem');
                importBtn.disabled = false;
                // the tags may have been written even though the import did not run
                window.dispatchEvent(new CustomEvent('fork:import-changed'));
            }
        });
        release.album.focus();
    }

    // entries: [{ name, paths: [audio files in the import folder] }]
    window.forkManualImport = function (options) {
        const entries = ((options && options.entries) || []).filter((e) => e && e.paths && e.paths.length);
        if (!entries.length) { toast('Nothing to import', 'error'); return; }
        openEntry(entries, 0, { count: 0 });
    };
})();
