// fork (itsdmd/SoulSync): the Tag Editor page — edit tags and cover art by
// hand, rename files and folders, rename in bulk.
//
// Layout: the library's folder tree on the left; on the right a search bar,
// the contents of the open folder (a file browser), and the tag editor
// (tag / original value / new value, as in MusicBrainz Picard).
//
// The tree and the search read a cached index of the library kept by the
// server (core/fork/library_index.py); only the folder that is open is read
// from disk. Backed by /api/fork/editor/* (api/fork.py). See FORK.md.
(function () {
    'use strict';

    const API = '/api/fork/editor';
    const MAX_EDIT = 500;

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

    async function api(path, body) {
        const resp = await fetch(API + path, body ? {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: new AbortController().signal,
        } : { signal: new AbortController().signal });
        let data = {};
        try { data = await resp.json(); } catch (_) { /* non-JSON error page */ }
        if (!resp.ok || data.success === false) throw new Error(data.error || `Request failed (${resp.status})`);
        return data;
    }

    const q = (params) => '?' + new URLSearchParams(params).toString();
    const sizeText = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);
    const dateText = (t) => (t ? new Date(t * 1000).toLocaleDateString() : '');
    const ICONS = { dir: '📁', audio: '🎵', lyrics: '📝', image: '🖼️', other: '📄' };

    let page = null;      // the open page's state, or null

    function open() {
        if (page) return;
        const s = page = {
            roots: [], fields: [], path: '', parent: null, items: [], searching: false,
            mode: 'global', selected: new Set(), anchor: null, tagData: null, edits: {}, cover: { action: 'keep' },
            loadSeq: 0, tagSeq: 0, searchTimer: null, pollTimer: null,
        };

        // ── skeleton ─────────────────────────────────────────────────────
        s.indexEl = el('span', { class: 'fork-editor-index' });
        s.treeEl = el('div', { class: 'fork-editor-tree', role: 'tree' });
        s.searchEl = el('input', {
            class: 'fork-album-input', type: 'search', placeholder: 'Search files and folders…',
            'aria-label': 'Search the library',
            oninput: () => { clearTimeout(s.searchTimer); s.searchTimer = setTimeout(runSearch, 250); },
            onkeydown: (e) => { if (e.key === 'Escape' && s.searchEl.value) { e.stopPropagation(); s.searchEl.value = ''; runSearch(); } },
        });
        const modeBtn = (mode, text, title) => el('button', {
            class: 'download-control-btn secondary fork-editor-mode', type: 'button', text, title, 'data-mode': mode,
            onclick: () => { s.mode = mode; paintModes(); if (s.searchEl.value.trim()) runSearch(); },
        });
        s.modeBtns = [
            modeBtn('global', 'Entire library', 'Search every folder of the library'),
            modeBtn('local', 'This folder', 'Search only the folder that is open, and the folders inside it'),
        ];
        s.crumbEl = el('div', { class: 'fork-editor-crumb' });
        s.countEl = el('span', { class: 'fork-album-status' });
        s.renameBtn = el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Rename…', disabled: true, title: 'Rename the selected file or folder (F2)', onclick: () => openRename() });
        s.bulkBtn = el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Bulk rename…', disabled: true, title: 'Find and replace in the names of the selected items', onclick: () => openBulkRename() });
        s.listEl = el('div', { class: 'fork-editor-list', tabindex: '0', onkeydown: onListKey });
        s.coverEl = el('div', { class: 'fork-editor-cover' });
        s.tagsEl = el('div', { class: 'fork-editor-tags' });
        s.statusEl = el('span', { class: 'fork-album-status' });
        s.saveBtn = el('button', { class: 'download-control-btn primary', type: 'button', text: 'Save', disabled: true, onclick: save });
        s.revertBtn = el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Revert', disabled: true, onclick: () => { resetEdits(); paintEditor(); } });

        s.root = el('div', { class: 'fork-editor', role: 'dialog', 'aria-label': 'Tag Editor' }, [
            el('div', { class: 'fork-editor-head' }, [
                el('h2', { text: 'Tag Editor' }),
                s.indexEl,
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Rescan library', title: 'Walk the whole library again to refresh the folder tree and the search', onclick: rescan }),
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Close', onclick: close }),
            ]),
            el('div', { class: 'fork-editor-body' }, [
                el('div', { class: 'fork-editor-side' }, [s.treeEl]),
                el('div', { class: 'fork-editor-main' }, [
                    el('div', { class: 'fork-editor-search' }, [s.searchEl, ...s.modeBtns]),
                    el('div', { class: 'fork-editor-bar' }, [s.crumbEl, s.countEl, s.renameBtn, s.bulkBtn]),
                    s.listEl,
                    el('div', { class: 'fork-editor-panel' }, [
                        s.coverEl,
                        el('div', { class: 'fork-editor-tagbox' }, [
                            s.tagsEl,
                            el('div', { class: 'fork-album-actions' }, [s.statusEl, s.revertBtn, s.saveBtn]),
                        ]),
                    ]),
                ]),
            ]),
        ]);
        document.body.append(s.root);
        document.addEventListener('keydown', onKey, true);
        paintModes();
        paintEditor();
        start();
    }

    function close() {
        if (!page) return;
        clearTimeout(page.searchTimer);
        clearTimeout(page.pollTimer);
        document.removeEventListener('keydown', onKey, true);
        page.root.remove();
        page = null;
    }

    function onKey(e) {
        if (!page || document.querySelector('.fork-album-overlay')) return;
        if (e.key === 'Escape' && !e.defaultPrevented && document.activeElement !== page.searchEl) close();
    }

    const dirty = () => !!page && (Object.keys(page.edits).length > 0 || page.cover.action !== 'keep');

    // ── index status ─────────────────────────────────────────────────────

    function paintIndex(index) {
        const s = page;
        if (!s || !index) return;
        clearTimeout(s.pollTimer);
        if (index.running) {
            s.indexEl.textContent = `Indexing the library… ${index.dirs.toLocaleString()} folders, ${index.files.toLocaleString()} files`;
            s.pollTimer = setTimeout(async () => {
                try { paintIndex((await api('/scan')).index); } catch (_) { /* try again on the next action */ }
            }, 1500);
        } else if (index.last_full_scan) {
            const mins = Math.max(0, Math.round((Date.now() / 1000 - index.last_full_scan) / 60));
            const ago = mins < 1 ? 'just now' : mins < 90 ? `${mins} min ago` : mins < 2880 ? `${Math.round(mins / 60)} h ago` : `${Math.round(mins / 1440)} days ago`;
            s.indexEl.textContent = `Library indexed ${ago}`;
        } else {
            s.indexEl.textContent = index.error ? `Indexing failed: ${index.error}` : 'Library not indexed yet';
        }
    }

    async function rescan() {
        try { paintIndex((await api('/scan', {})).index); } catch (err) { toast(err.message, 'error'); }
    }

    // ── tree ─────────────────────────────────────────────────────────────

    function treeNode(dir, depth) {
        const node = { path: dir.path, name: dir.name, open: false, loaded: false, kids: [] };
        node.caret = el('span', {
            class: 'fork-editor-caret', text: dir.has_children ? '▸' : '',
            onclick: (e) => { e.stopPropagation(); toggle(node); },
        });
        node.row = el('div', {
            class: 'fork-editor-node', role: 'treeitem', title: dir.path, style: `padding-left:${8 + depth * 14}px`,
            onclick: () => navigate(node.path),
            ondblclick: () => toggle(node),
        }, [node.caret, el('span', { class: 'fork-editor-nodename', text: dir.name })]);
        node.box = el('div', { class: 'fork-editor-kids', hidden: true });
        node.el = el('div', {}, [node.row, node.box]);
        node.depth = depth;
        return node;
    }

    async function toggle(node, forceOpen) {
        const s = page;
        if (!s || (forceOpen && node.open)) return;
        node.open = forceOpen || !node.open;
        if (node.open && !node.loaded) {
            try {
                const data = await api('/tree' + q({ path: node.path }));
                node.kids = data.dirs.map((d) => treeNode(d, node.depth + 1));
                node.box.replaceChildren(...node.kids.map((k) => k.el));
                node.loaded = true;
                if (!node.kids.length) node.caret.textContent = '';
            } catch (err) { toast(err.message, 'error'); node.open = false; }
        }
        node.box.hidden = !node.open;
        if (node.caret.textContent) node.caret.textContent = node.open ? '▾' : '▸';
    }

    // Open the tree down to `path` and mark it.
    async function reveal(path) {
        const s = page;
        if (!s) return;
        let level = s.tree;
        let found = null;
        for (;;) {
            const next = level.find((n) => path === n.path || path.startsWith(n.path.replace(/\/+$/, '') + '/'));
            if (!next) break;
            found = next;
            if (next.path === path) break;
            await toggle(next, true);
            if (page !== s) return;
            level = next.kids;
        }
        s.treeEl.querySelectorAll('.fork-editor-node.current').forEach((n) => n.classList.remove('current'));
        if (found && found.path === path) {
            found.row.classList.add('current');
            found.row.scrollIntoView({ block: 'nearest' });
        }
    }

    // After a rename: reload the children of a folder that is open in the tree.
    async function refreshTree(path) {
        const s = page;
        const find = (nodes) => {
            for (const n of nodes) {
                if (n.path === path) return n;
                const deeper = find(n.kids);
                if (deeper) return deeper;
            }
            return null;
        };
        const node = s && find(s.tree);
        if (!node || !node.loaded) return;
        node.loaded = false;
        node.open = false;
        await toggle(node, true);
    }

    // ── folder contents ──────────────────────────────────────────────────

    async function start() {
        const s = page;
        try {
            const data = await api('/roots');
            if (page !== s) return;
            s.roots = data.roots;
            s.fields = data.fields;
            s.tree = data.roots.map((r) => treeNode(r, 0));
            s.treeEl.replaceChildren(...s.tree.map((n) => n.el));
            paintIndex(data.index);
            paintEditor();
            if (s.roots.length) navigate(s.roots[0].path);
            else s.listEl.replaceChildren(el('div', { class: 'fork-album-note', text: 'No library folder is configured.' }));
        } catch (err) {
            s.listEl.replaceChildren(el('div', { class: 'fork-album-note', text: `Could not open the library: ${err.message}` }));
        }
    }

    function blocked() {
        if (!dirty()) return false;
        page.statusEl.textContent = 'Save or revert your changes first';
        page.statusEl.classList.add('fork-volume-problem');
        return true;
    }

    async function navigate(path, selectPath) {
        const s = page;
        if (!s || blocked()) return;
        const seq = ++s.loadSeq;
        s.searchEl.value = '';
        s.searching = false;
        try {
            const data = await api('/list' + q({ path }));
            if (page !== s || seq !== s.loadSeq) return;
            s.path = data.path;
            s.parent = data.parent;
            s.items = data.dirs.concat(data.files);
            s.selected = new Set(selectPath && s.items.some((i) => i.path === selectPath) ? [selectPath] : []);
            s.anchor = selectPath || null;
            paintCrumb();
            paintList();
            selectionChanged();
            reveal(s.path);
        } catch (err) { toast(err.message, 'error'); }
    }

    async function reload() {
        const s = page;
        if (!s) return;
        if (s.searching) return runSearch(true);
        const keep = new Set(s.selected);
        try {
            const data = await api('/list' + q({ path: s.path }));
            if (page !== s) return;
            s.items = data.dirs.concat(data.files);
            s.selected = new Set(s.items.filter((i) => keep.has(i.path)).map((i) => i.path));
            paintList();
            selectionChanged(true);
        } catch (err) { toast(err.message, 'error'); }
    }

    async function runSearch(keepSelection) {
        const s = page;
        if (!s) return;
        const text = s.searchEl.value.trim();
        if (!text) { if (s.searching) { s.searching = false; navigate(s.path); } return; }
        if (!keepSelection && blocked()) return;
        const seq = ++s.loadSeq;
        try {
            const params = { q: text };
            if (s.mode === 'local') params.path = s.path;
            const data = await api('/search' + q(params));
            if (page !== s || seq !== s.loadSeq) return;
            const keep = keepSelection ? new Set(s.selected) : new Set();
            s.searching = true;
            s.items = data.dirs.concat(data.files);
            s.truncated = !!data.truncated;
            s.selected = new Set(s.items.filter((i) => keep.has(i.path)).map((i) => i.path));
            s.anchor = null;
            paintIndex(data.index);
            paintCrumb();
            paintList();
            selectionChanged(keepSelection);
        } catch (err) { toast(err.message, 'error'); }
    }

    function paintModes() {
        for (const b of page.modeBtns) b.classList.toggle('active', b.dataset.mode === page.mode);
    }

    function paintCrumb() {
        const s = page;
        if (s.searching) {
            const where = s.mode === 'local' ? `in ${s.path}` : 'in the entire library';
            s.crumbEl.replaceChildren(el('span', { text: `Results ${where}${s.truncated ? ' (first matches only)' : ''}` }));
            return;
        }
        const root = s.roots.map((r) => r.path).filter((r) => s.path === r || s.path.startsWith(r.replace(/\/+$/, '') + '/'))
            .sort((a, b) => b.length - a.length)[0] || '';
        const parts = [el('a', { href: '#', text: root, onclick: (e) => { e.preventDefault(); navigate(root); } })];
        let acc = root.replace(/\/+$/, '');
        for (const name of s.path.slice(root.length).split('/').filter(Boolean)) {
            acc += '/' + name;
            const target = acc;
            parts.push(el('span', { text: ' / ' }), el('a', { href: '#', text: name, onclick: (e) => { e.preventDefault(); navigate(target); } }));
        }
        s.crumbEl.replaceChildren(...parts);
    }

    function paintList() {
        const s = page;
        const head = el('div', { class: 'fork-editor-row fork-editor-rowhead' }, ['Name', 'Title', 'Artist', 'Album', '#', 'Size', 'Modified'].map((t) => el('span', { text: t })));
        const rows = [];
        if (!s.searching && s.parent) {
            rows.push(el('div', { class: 'fork-editor-row', ondblclick: () => navigate(s.parent), title: 'Up one folder' },
                [el('span', { text: '📁 ..' }), ...Array.from({ length: 6 }, () => el('span'))]));
        }
        for (const item of s.items) {
            const where = s.searching ? item.path.slice(0, item.path.length - item.name.length - 1) : '';
            const row = el('div', {
                class: `fork-editor-row${s.selected.has(item.path) ? ' selected' : ''}`, title: item.path,
                onmousedown: (e) => { if (e.shiftKey) e.preventDefault(); },   // no text selection on shift+click
                onclick: (e) => clickRow(item, e),
                ondblclick: () => (item.kind === 'dir' ? navigate(item.path) : s.searching ? navigate(where, item.path) : null),
            }, [
                el('span', { class: 'fork-editor-name' }, [`${ICONS[item.kind] || ICONS.other} ${item.name}`, where ? el('small', { text: where }) : null]),
                el('span', { text: item.title || '' }), el('span', { text: item.artist || '' }), el('span', { text: item.album || '' }),
                el('span', { text: item.track || '' }),
                el('span', { text: item.kind === 'dir' ? '' : sizeText(item.size || 0) }),
                el('span', { text: item.kind === 'dir' ? '' : dateText(item.mtime) }),
            ]);
            item.row = row;
            rows.push(row);
        }
        if (!s.items.length) rows.push(el('div', { class: 'fork-album-note', text: s.searching ? 'Nothing found.' : 'This folder is empty.' }));
        s.listEl.replaceChildren(head, ...rows);
    }

    // Click selects one; Ctrl/⌘+click adds or removes one; Shift+click selects
    // everything from the last clicked row to this one.
    function clickRow(item, e) {
        const s = page;
        if (blocked()) return;
        const paths = s.items.map((i) => i.path);
        if (e.shiftKey && s.anchor && paths.includes(s.anchor)) {
            const a = paths.indexOf(s.anchor);
            const b = paths.indexOf(item.path);
            const range = paths.slice(Math.min(a, b), Math.max(a, b) + 1);
            s.selected = new Set(e.ctrlKey || e.metaKey ? [...s.selected, ...range] : range);
        } else if (e.ctrlKey || e.metaKey) {
            if (s.selected.has(item.path)) s.selected.delete(item.path); else s.selected.add(item.path);
            s.anchor = item.path;
        } else {
            s.selected = new Set([item.path]);
            s.anchor = item.path;
        }
        for (const i of s.items) if (i.row) i.row.classList.toggle('selected', s.selected.has(i.path));
        selectionChanged();
    }

    function onListKey(e) {
        const s = page;
        if (!s) return;
        if (e.key === 'F2') { e.preventDefault(); openRename(); }
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
            e.preventDefault();
            if (blocked()) return;
            s.selected = new Set(s.items.map((i) => i.path));
            for (const i of s.items) if (i.row) i.row.classList.add('selected');
            selectionChanged();
        }
        if (e.key === 'Enter' && s.selected.size === 1) {
            const item = s.items.find((i) => s.selected.has(i.path));
            if (item && item.kind === 'dir') navigate(item.path);
        }
    }

    const chosen = () => page.items.filter((i) => page.selected.has(i.path));

    // ── tag editor ───────────────────────────────────────────────────────

    function resetEdits() {
        page.edits = {};
        page.cover = { action: 'keep' };
    }

    async function selectionChanged(keepEdits) {
        const s = page;
        const picked = chosen();
        const audio = picked.filter((i) => i.kind === 'audio');
        s.countEl.textContent = picked.length ? `${picked.length} selected` : `${s.items.length} item${s.items.length === 1 ? '' : 's'}`;
        s.renameBtn.disabled = picked.length !== 1;
        s.bulkBtn.disabled = !picked.length;
        if (!keepEdits) resetEdits();
        const seq = ++s.tagSeq;
        if (!audio.length || audio.length > MAX_EDIT) {
            s.tagData = audio.length ? { tooMany: audio.length } : null;
            paintEditor();
            return;
        }
        try {
            const data = await api('/tags', { paths: audio.map((i) => i.path) });
            if (page !== s || seq !== s.tagSeq) return;
            s.tagData = { files: data.files, errors: data.errors };
        } catch (err) {
            if (page !== s || seq !== s.tagSeq) return;
            s.tagData = { files: [], errors: [err.message] };
        }
        paintEditor();
    }

    // What the selection has for a tag: {value} when every file agrees, else {mixed: n}.
    function original(field) {
        const values = new Set(page.tagData.files.map((f) => f.tags[field] || ''));
        return values.size <= 1 ? { value: [...values][0] || '' } : { mixed: values.size };
    }

    function paintEditor() {
        const s = page;
        const data = s.tagData;
        const files = (data && data.files) || [];
        s.statusEl.classList.remove('fork-volume-problem');
        if (!files.length) {
            const why = data && data.tooMany ? `${data.tooMany} files selected — the editor takes up to ${MAX_EDIT} at a time.`
                : data && data.errors && data.errors.length ? `Could not read the tags: ${data.errors[0]}`
                    : 'Select one or more audio files to edit their tags. Shift+click selects a range, Ctrl+click adds one.';
            s.tagsEl.replaceChildren(el('div', { class: 'fork-album-note', text: why }));
            s.coverEl.replaceChildren();
            s.statusEl.textContent = '';
            s.saveBtn.disabled = s.revertBtn.disabled = true;
            return;
        }
        const rows = [el('div', { class: 'fork-editor-tag fork-editor-taghead' }, [el('span', { text: 'Tag' }), el('span', { text: 'Original value' }), el('span', { text: 'New value' }), el('span')])];
        for (const field of s.fields) {
            const was = original(field.name);
            const edited = Object.prototype.hasOwnProperty.call(s.edits, field.name);
            const input = el('input', {
                class: 'fork-album-input', type: 'text', 'aria-label': `New ${field.label}`,
                value: edited ? s.edits[field.name] : (was.mixed ? '' : was.value),
                placeholder: was.mixed && !edited ? '(keep each file\'s own)' : '',
                oninput: () => {
                    const same = !was.mixed && input.value === was.value;
                    if (same) delete s.edits[field.name]; else s.edits[field.name] = input.value;
                    row.classList.toggle('changed', !same);
                    undo.hidden = same;
                    paintActions();
                },
            });
            const undo = el('button', {
                class: 'fork-editor-undo', type: 'button', text: '↺', title: 'Put the original value back', hidden: !edited,
                onclick: () => { delete s.edits[field.name]; paintEditor(); },
            });
            const row = el('div', { class: `fork-editor-tag${edited ? ' changed' : ''}` }, [
                el('span', { text: field.label }),
                el('span', { class: was.mixed ? 'fork-editor-mixed' : '', text: was.mixed ? `(${was.mixed} different values)` : was.value, title: was.mixed ? '' : was.value }),
                input, undo,
            ]);
            rows.push(row);
        }
        if (data.errors && data.errors.length) rows.push(el('div', { class: 'fork-album-note fork-volume-problem', text: `${data.errors.length} file(s) could not be read: ${data.errors[0]}` }));
        s.tagsEl.replaceChildren(...rows);
        paintCover();
        paintActions();
    }

    function paintCover() {
        const s = page;
        const files = s.tagData.files;
        const hashes = new Set(files.map((f) => (f.cover ? f.cover.hash : '')));
        let picture;
        let note;
        if (s.cover.action === 'set') {
            picture = el('img', { src: s.cover.preview, alt: 'New cover' });
            note = 'New cover (not saved yet)';
        } else if (s.cover.action === 'remove') {
            note = 'Cover will be removed';
        } else if (hashes.size > 1) {
            note = `${hashes.size} different covers`;
        } else if ([...hashes][0]) {
            const first = files.find((f) => f.cover);
            picture = el('img', { src: `${API}/cover${q({ path: first.path, v: first.cover.hash })}`, alt: 'Cover' });
            note = `${first.cover.mime.replace('image/', '').toUpperCase()}, ${sizeText(first.cover.size)}`;
        } else {
            note = 'No cover';
        }
        const picker = el('input', {
            type: 'file', accept: 'image/jpeg,image/png', hidden: true,
            onchange: () => {
                const file = picker.files && picker.files[0];
                if (!file) return;
                const reader = new FileReader();
                reader.onload = () => {
                    const url = String(reader.result);
                    s.cover = { action: 'set', data: url.slice(url.indexOf(',') + 1), preview: url, folder_file: folderBox.checked };
                    paintCover();
                    paintActions();
                };
                reader.readAsDataURL(file);
            },
        });
        const folderBox = el('input', { type: 'checkbox', checked: !!s.cover.folder_file, onchange: () => { s.cover.folder_file = folderBox.checked; } });
        s.coverEl.replaceChildren(
            el('div', { class: `fork-editor-art${s.cover.action !== 'keep' ? ' changed' : ''}` }, picture || el('span', { text: '♪' })),
            el('small', { text: note }),
            el('div', { class: 'fork-editor-coverbtns' }, [
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Replace…', onclick: () => picker.click() }),
                el('button', {
                    class: 'download-control-btn secondary', type: 'button', text: 'Remove', disabled: s.cover.action === 'remove',
                    onclick: () => { s.cover = { action: 'remove' }; paintCover(); paintActions(); },
                }),
                s.cover.action !== 'keep' ? el('button', { class: 'fork-editor-undo', type: 'button', text: '↺', title: 'Keep the cover as it is', onclick: () => { s.cover = { action: 'keep' }; paintCover(); paintActions(); } }) : null,
            ]),
            s.cover.action === 'set' ? el('label', { class: 'fork-editor-check' }, [folderBox, 'Also save as cover file in the folder']) : null,
            picker,
        );
    }

    function paintActions() {
        const s = page;
        const count = (s.tagData && s.tagData.files ? s.tagData.files.length : 0);
        const changes = Object.keys(s.edits).length + (s.cover.action !== 'keep' ? 1 : 0);
        s.statusEl.classList.remove('fork-volume-problem');
        s.statusEl.textContent = changes ? `${changes} change${changes === 1 ? '' : 's'} for ${count} file${count === 1 ? '' : 's'}` : `${count} file${count === 1 ? '' : 's'} selected`;
        s.saveBtn.disabled = !changes || s.saving;
        s.revertBtn.disabled = !changes || s.saving;
        s.saveBtn.textContent = changes && count > 1 ? `Save to ${count} files` : 'Save';
    }

    async function save() {
        const s = page;
        if (!s || !dirty() || s.saving) return;
        s.saving = true;
        paintActions();
        s.statusEl.textContent = 'Saving…';
        try {
            const cover = s.cover.action === 'keep' ? null : { action: s.cover.action, data: s.cover.data, folder_file: !!s.cover.folder_file };
            const data = await api('/save', { paths: s.tagData.files.map((f) => f.path), tags: s.edits, cover });
            if (data.errors.length) toast(`${data.saved} saved, ${data.errors.length} failed — ${data.errors[0]}`, 'error');
            else toast(`Saved ${data.saved} file${data.saved === 1 ? '' : 's'}`);
            resetEdits();
        } catch (err) {
            toast(err.message, 'error');
        }
        s.saving = false;
        if (page === s) { paintActions(); await reload(); }
    }

    // ── renaming ─────────────────────────────────────────────────────────

    function dialog(className, children) {
        const node = el('div', { class: 'fork-album-overlay fork-editor-dialog' }, el('div', { class: `fork-album-dialog ${className}` }, children));
        node.addEventListener('mousedown', (e) => { if (e.target === node) node.remove(); });
        node.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); node.remove(); } });
        document.body.append(node);
        return node;
    }

    async function afterRename(folders) {
        if (!page) return;
        await reload();
        for (const folder of folders) await refreshTree(folder);
    }

    function openRename() {
        const s = page;
        const picked = chosen();
        if (!s || picked.length !== 1 || blocked()) return;
        const item = picked[0];
        const input = el('input', { class: 'fork-album-input', type: 'text', value: item.name, 'aria-label': 'New name' });
        const status = el('span', { class: 'fork-album-status' });
        const go = async () => {
            if (!input.value.trim() || input.value === item.name) { root.remove(); return; }
            try {
                const done = await api('/rename', { path: item.path, name: input.value });
                root.remove();
                toast(done.sidecars.length ? `Renamed, with ${done.sidecars.length} lyrics file${done.sidecars.length === 1 ? '' : 's'}` : 'Renamed');
                s.selected = new Set([done.path]);
                await afterRename(item.kind === 'dir' ? [item.path.slice(0, item.path.length - item.name.length - 1)] : []);
            } catch (err) { status.textContent = err.message; status.classList.add('fork-volume-problem'); }
        };
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
        const root = dialog('fork-editor-rename', [
            el('h3', { text: `Rename ${item.kind === 'dir' ? 'folder' : 'file'}` }),
            item.kind === 'audio' ? el('div', { class: 'fork-album-note', text: 'Lyrics files with the same name (.lrc, .txt) are renamed with it.' }) : null,
            input,
            el('div', { class: 'fork-album-actions' }, [
                status,
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Cancel', onclick: () => root.remove() }),
                el('button', { class: 'download-control-btn primary', type: 'button', text: 'Rename', onclick: go }),
            ]),
        ]);
        input.focus();
        const dot = item.kind === 'dir' ? -1 : item.name.lastIndexOf('.');
        input.setSelectionRange(0, dot > 0 ? dot : item.name.length);
    }

    function openBulkRename() {
        const s = page;
        const picked = chosen();
        if (!s || !picked.length || blocked()) return;
        const paths = picked.map((i) => i.path);
        const find = el('input', { class: 'fork-album-input', type: 'text', placeholder: 'Find', 'aria-label': 'Find' });
        const replace = el('input', { class: 'fork-album-input', type: 'text', placeholder: 'Replace with', 'aria-label': 'Replace with' });
        const regex = el('input', { type: 'checkbox' });
        const matchCase = el('input', { type: 'checkbox' });
        const list = el('div', { class: 'fork-volume-list fork-editor-preview' });
        const status = el('span', { class: 'fork-album-status' });
        const applyBtn = el('button', { class: 'download-control-btn primary', type: 'button', text: 'Rename', disabled: true });
        let seq = 0;
        let timer = null;
        const body = (apply) => ({ paths, find: find.value, replace: replace.value, regex: regex.checked, case_sensitive: matchCase.checked, apply });

        async function preview() {
            const mine = ++seq;
            status.classList.remove('fork-volume-problem');
            if (!find.value) { list.replaceChildren(); status.textContent = `${paths.length} item${paths.length === 1 ? '' : 's'} selected`; applyBtn.disabled = true; return; }
            try {
                const data = await api('/bulk-rename', body(false));
                if (mine !== seq) return;
                const changed = data.items.filter((i) => i.new !== i.old || i.problem);
                list.replaceChildren(...(changed.length ? changed : [null]).map((i) => (i ? el('div', { class: `fork-volume-row${i.problem ? ' fork-editor-bad' : ''}` }, [
                    el('div', { class: 'fork-volume-name' }, [el('div', { text: i.old }), el('small', { text: i.problem ? `Skipped: ${i.problem}` : `→ ${i.new}`, title: i.new })]),
                ]) : el('div', { class: 'fork-album-note', text: 'No name contains that.' }))));
                status.textContent = `${data.changes} to rename${data.problems ? `, ${data.problems} skipped` : ''}`;
                applyBtn.disabled = !data.changes;
                applyBtn.textContent = data.changes ? `Rename ${data.changes}` : 'Rename';
            } catch (err) {
                if (mine !== seq) return;
                list.replaceChildren();
                status.textContent = err.message;
                status.classList.add('fork-volume-problem');
                applyBtn.disabled = true;
            }
        }
        const later = () => { clearTimeout(timer); timer = setTimeout(preview, 200); };
        for (const node of [find, replace]) node.addEventListener('input', later);
        for (const node of [regex, matchCase]) node.addEventListener('change', preview);
        applyBtn.addEventListener('click', async () => {
            applyBtn.disabled = true;
            try {
                const data = await api('/bulk-rename', body(true));
                root.remove();
                toast(data.problems ? `Renamed ${data.renamed}, ${data.problems} skipped` : `Renamed ${data.renamed}`, data.problems ? 'error' : 'success');
                s.selected = new Set(data.items.map((i) => i.path));
                const folders = new Set(picked.filter((i) => i.kind === 'dir').map((i) => i.path.slice(0, i.path.length - i.name.length - 1)));
                await afterRename([...folders]);
            } catch (err) { status.textContent = err.message; status.classList.add('fork-volume-problem'); applyBtn.disabled = false; }
        });
        const root = dialog('fork-volume-editor', [
            el('h3', { text: `Bulk rename — ${paths.length} item${paths.length === 1 ? '' : 's'}` }),
            el('div', { class: 'fork-album-note', text: 'Replaces text in the names of the selected files and folders. A file\'s extension is never changed, and lyrics files follow their track. With "Regular expression", groups are written $1, $2.' }),
            el('div', { class: 'fork-editor-find' }, [find, replace]),
            el('div', { class: 'fork-editor-find' }, [
                el('label', { class: 'fork-editor-check' }, [regex, 'Regular expression']),
                el('label', { class: 'fork-editor-check' }, [matchCase, 'Match case']),
            ]),
            list,
            el('div', { class: 'fork-album-actions' }, [
                status,
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Cancel', onclick: () => root.remove() }),
                applyBtn,
            ]),
        ]);
        preview();
        find.focus();
    }

    // ── sidebar entry ────────────────────────────────────────────────────

    function addNavEntry() {
        if (document.getElementById('fork-editor-nav-button')) return true;
        const anchor = document.querySelector('.nav-button[data-page="library"]') || document.getElementById('fork-nav-button');
        if (!anchor) return false;
        const button = el('a', { class: 'nav-button', id: 'fork-editor-nav-button', href: '#', title: 'Edit tags and cover art by hand, rename files and folders' });
        const icon = el('span', { class: 'nav-icon' });
        // static markup, no user data
        icon.innerHTML = '<svg class="nav-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.6 13.4l-7.2 7.2a2 2 0 0 1-2.800 0L3 13V3h10l7.600 7.600a2 2 0 0 1 0 2.800z"/><circle cx="7.500" cy="7.500" r="1.300"/></svg>';
        button.append(icon, el('span', { class: 'nav-text', text: 'Tag Editor' }));
        button.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); open(); });
        anchor.insertAdjacentElement('afterend', button);
        return true;
    }

    window.openForkTagEditor = open;

    function install() {
        if (addNavEntry()) return;
        // the sidebar is rendered after this script on some loads
        const observer = new MutationObserver(() => { if (addNavEntry()) observer.disconnect(); });
        observer.observe(document.body, { childList: true, subtree: true });
        setTimeout(() => observer.disconnect(), 30000);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
    else install();
})();
