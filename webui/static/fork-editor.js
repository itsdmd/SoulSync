// fork (itsdmd/SoulSync): the Tag Editor page — edit tags and cover art by
// hand, rename files and folders, rename in bulk.
//
// Layout: the library's folder tree on the left; on the right a search bar,
// the contents of the open folder (a file browser), and the tag editor
// (tag / original value / new value, as in MusicBrainz Picard).
//
// Files and folders are moved by dragging them onto a folder, in the list or
// the tree; a box above the tree filters folders by name. Copy, cut, paste,
// move to, delete and rename are buttons above the list and a right-click menu.
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
    const PANE_KEYS = ['path', 'parent', 'items', 'selected', 'anchor', 'searching', 'truncated', 'query', 'loadSeq', 'listEl', 'crumbEl', 'allBox'];

    function open() {
        if (page) return;
        const s = page = {
            roots: [], fields: [], panes: [], pane: null,
            mode: 'global', tagData: null, edits: {}, cover: { action: 'keep' },
            tagSeq: 0, searchTimer: null, pollTimer: null,
        };
        // The list area is one pane, or two side by side (split view). Each
        // pane has its own folder, selection and search; `s.path`, `s.items`,
        // `s.selected`… always mean those of the ACTIVE pane.
        for (const key of PANE_KEYS) {
            Object.defineProperty(s, key, { get: () => s.pane[key], set: (value) => { s.pane[key] = value; } });
        }
        s.pane = makePane();
        s.panes = [s.pane];
        s.pane.el.classList.add('active');

        // ── skeleton ─────────────────────────────────────────────────────
        s.indexEl = el('span', { class: 'fork-editor-index' });
        s.treeEl = el('div', { class: 'fork-editor-tree', role: 'tree' });
        s.treeHitsEl = el('div', { class: 'fork-editor-tree', hidden: true });
        s.treeFilterEl = el('input', {
            class: 'fork-album-input', type: 'search', placeholder: 'Filter folders…', 'aria-label': 'Filter the folder tree',
            oninput: () => { clearTimeout(s.treeTimer); s.treeTimer = setTimeout(filterTree, 250); },
            onkeydown: (e) => { if (e.key === 'Escape' && s.treeFilterEl.value) { e.stopPropagation(); s.treeFilterEl.value = ''; filterTree(); } },
        });
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
        s.countEl = el('span', { class: 'fork-album-status' });
        const act = (text, title, fn) => el('button', { class: 'download-control-btn secondary', type: 'button', text, title, disabled: true, onclick: fn });
        s.btns = {
            copy: act('Copy', 'Copy the selected items (Ctrl+C)', () => setClip('copy', picks())),
            cut: act('Cut', 'Cut the selected items, to paste them elsewhere (Ctrl+X)', () => setClip('cut', picks())),
            paste: act('Paste', 'Paste into the open folder (Ctrl+V)', () => paste(s.path)),
            moveTo: act('Move to…', 'Pick a folder to move the selected items into', () => moveTo(picks())),
            del: act('Delete', 'Delete the selected items for good (Delete)', () => confirmDelete(picks())),
            rename: act('Rename…', 'Rename the selection: a new name, or find and replace (F2)', () => openRename()),
        };
        s.panesEl = el('div', { class: 'fork-editor-panes' }, [s.pane.el]);
        s.splitBtn = el('button', {
            class: 'download-control-btn secondary fork-editor-mode', type: 'button', text: 'Split view',
            title: 'Show two folders side by side, to drag files and folders between them', onclick: toggleSplit,
        });
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
                el('div', { class: 'fork-editor-side' }, [
                    el('div', { class: 'fork-editor-treefilter' }, [s.treeFilterEl]),
                    s.treeEl, s.treeHitsEl,
                ]),
                el('div', { class: 'fork-editor-main' }, [
                    el('div', { class: 'fork-editor-search' }, [s.searchEl, ...s.modeBtns, s.splitBtn]),
                    el('div', { class: 'fork-editor-bar' }, [s.countEl, ...Object.values(s.btns)]),
                    s.panesEl,
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
        clearTimeout(page.treeTimer);
        closeMenu();
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
        node.row.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            const self = [{ path: node.path, name: node.name, kind: 'dir' }];
            const fixed = depth === 0;                                 // a library folder itself stays put
            showMenu(e, [
                { label: 'Open', fn: () => navigate(node.path) },
                { label: 'Rename…', disabled: fixed, fn: () => openRename(self) },
                { label: 'Copy', disabled: fixed, fn: () => setClip('copy', self) },
                { label: 'Cut', disabled: fixed, fn: () => setClip('cut', self) },
                { label: 'Paste into this folder', disabled: !page.clip, fn: () => paste(node.path) },
                { label: 'Move to…', disabled: fixed, fn: () => moveTo(self) },
                { label: 'Delete', disabled: fixed, danger: true, fn: () => confirmDelete(self) },
            ]);
        });
        if (depth > 0) dragSource(node.row, () => [node.path]);       // a library folder itself stays put
        dropTarget(node.row, () => node.path);
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
        if (!s || !s.tree) return;
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
        if (node && !node.loaded && !node.caret.textContent) node.caret.textContent = '▸';
        if (!node || !node.loaded) return;
        node.loaded = false;
        node.open = false;
        await toggle(node, true);
    }

    // The filter box above the tree: folders by name, from the index. It does
    // not touch the list on the right.
    async function filterTree() {
        const s = page;
        if (!s) return;
        const text = s.treeFilterEl.value.trim();
        s.treeEl.hidden = !!text;
        s.treeHitsEl.hidden = !text;
        if (!text) { reveal(s.path); return; }
        const seq = s.treeSeq = (s.treeSeq || 0) + 1;
        try {
            const data = await api('/search' + q({ q: text, dirs: '1' }));
            if (page !== s || seq !== s.treeSeq) return;
            const rows = data.dirs.map((dir) => {
                const where = dir.path.slice(0, dir.path.length - dir.name.length - 1);
                const row = el('div', { class: 'fork-editor-node fork-editor-hit', title: dir.path, onclick: () => navigate(dir.path) },
                    [el('span', { class: 'fork-editor-nodename' }, [dir.name, el('small', { text: where })])]);
                dragSource(row, () => [dir.path]);
                dropTarget(row, () => dir.path);
                return row;
            });
            if (!rows.length) rows.push(el('div', { class: 'fork-album-note', text: data.index && data.index.running ? 'Nothing yet — the library is still being indexed.' : 'No folder with that name.' }));
            if (data.truncated) rows.push(el('div', { class: 'fork-album-note', text: 'Showing the first matches only.' }));
            s.treeHitsEl.replaceChildren(...rows);
        } catch (err) { toast(err.message, 'error'); }
    }

    // ── moving by drag and drop ──────────────────────────────────────────
    // Anything in the list or the tree can be dragged onto a folder in either;
    // dropping on the empty part of the list moves into the open folder.

    const inside = (path, folder) => path === folder || path.startsWith(folder.replace(/\/+$/, '') + '/');
    const parentOf = (path) => path.slice(0, path.lastIndexOf('/')) || '/';

    function dragSource(node, getPaths) {
        node.draggable = true;
        node.addEventListener('dragstart', (e) => {
            const s = page;
            if (!s || dirty()) { e.preventDefault(); blocked(); return; }
            s.drag = getPaths();
            e.dataTransfer.effectAllowed = 'move';
            try { e.dataTransfer.setData('text/plain', s.drag.join('\n')); } catch (_) { /* some browsers refuse */ }
            e.stopPropagation();
        });
        node.addEventListener('dragend', () => {
            if (page) page.drag = null;
            document.querySelectorAll('.fork-editor-drop').forEach((n) => n.classList.remove('fork-editor-drop'));
        });
    }

    // A drop is allowed onto a folder that is not one of the dragged items,
    // not inside one of them, and not where they already are.
    function canDrop(dest) {
        const paths = page && page.drag;
        return !!dest && !!paths && paths.length > 0
            && !paths.some((p) => inside(dest, p)) && paths.some((p) => parentOf(p) !== dest);
    }

    function dropTarget(node, getDest) {
        node.addEventListener('dragover', (e) => {
            if (!canDrop(getDest())) return;
            e.preventDefault();
            e.stopPropagation();
            e.dataTransfer.dropEffect = 'move';
            node.classList.add('fork-editor-drop');
        });
        node.addEventListener('dragleave', () => node.classList.remove('fork-editor-drop'));
        node.addEventListener('drop', (e) => {
            node.classList.remove('fork-editor-drop');
            const dest = getDest();
            if (!canDrop(dest)) return;
            e.preventDefault();
            e.stopPropagation();
            confirmMove(page.drag.slice(), dest);
        });
    }

    function confirmMove(paths, dest) {
        const s = page;
        const names = paths.map((p) => p.slice(p.lastIndexOf('/') + 1));
        const status = el('span', { class: 'fork-album-status' });
        const go = el('button', { class: 'download-control-btn primary', type: 'button', text: 'Move' });
        go.addEventListener('click', async () => {
            go.disabled = true;
            try {
                const data = await api('/move', { paths, destination: dest });
                root.remove();
                if (data.errors.length) toast(`${data.moved.length} moved, ${data.errors.length} not — ${data.errors[0]}`, 'error');
                else toast(`Moved ${data.moved.length} item${data.moved.length === 1 ? '' : 's'}`);
                if (page !== s) return;
                await refreshAfter(paths.map(parentOf).concat([dest]), paths, dest);
            } catch (err) { status.textContent = err.message; status.classList.add('fork-volume-problem'); go.disabled = false; }
        });
        const root = dialog('fork-editor-rename', [
            el('h3', { text: `Move ${paths.length} item${paths.length === 1 ? '' : 's'}?` }),
            el('div', { class: 'fork-album-note', text: `${names.slice(0, 4).join(', ')}${names.length > 4 ? ` and ${names.length - 4} more` : ''}` }),
            el('div', { class: 'fork-editor-moveto' }, ['into ', el('strong', { text: dest })]),
            el('div', { class: 'fork-album-note', text: 'Lyrics files go with their track. Nothing is overwritten: an item whose name is already there is left where it is.' }),
            el('div', { class: 'fork-album-actions' }, [
                status,
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Cancel', onclick: () => root.remove() }),
                go,
            ]),
        ]);
        go.focus();
    }

    // ── panes (split view) ───────────────────────────────────────────────

    function makePane() {
        const pane = { path: '', parent: null, items: [], selected: new Set(), anchor: null, searching: false, truncated: false, query: '', loadSeq: 0, allBox: null };
        pane.crumbEl = el('div', { class: 'fork-editor-crumb' });
        pane.listEl = el('div', { class: 'fork-editor-list', tabindex: '0', onkeydown: onListKey });
        // dropping on the empty part of a list moves into the folder it shows
        dropTarget(pane.listEl, () => (pane.searching ? '' : pane.path));
        pane.listEl.addEventListener('contextmenu', (e) => {
            if (e.target.closest('.fork-editor-row:not(.fork-editor-rowhead)')) return;   // rows have their own
            e.preventDefault();
            if (!pane.searching) showMenu(e, [{ label: 'Paste', disabled: !page.clip, fn: () => paste(pane.path) }]);
        });
        pane.el = el('div', { class: 'fork-editor-pane' }, [pane.crumbEl, pane.listEl]);
        // whatever is done in a pane is done to that pane: make it the active one first
        for (const type of ['mousedown', 'contextmenu', 'focusin']) {
            pane.el.addEventListener(type, (e) => {
                if (!activate(pane) && type !== 'focusin') { e.preventDefault(); e.stopPropagation(); }
            }, true);
        }
        return pane;
    }

    // The search box, the buttons and the tag editor follow the active pane.
    // Refused while there are unsaved tag edits (they belong to the other one).
    function activate(pane) {
        const s = page;
        if (!s || pane === s.pane) return true;
        if (blocked()) return false;
        s.pane = pane;
        for (const other of s.panes) other.el.classList.toggle('active', other === pane);
        s.searchEl.value = pane.query || '';
        selectionChanged();
        if (!pane.searching && pane.path) reveal(pane.path);
        return true;
    }

    function toggleSplit() {
        const s = page;
        if (!s) return;
        if (s.panes.length === 1) {
            const second = makePane();
            s.panes.push(second);
            s.panesEl.append(second.el);
            navigate(s.pane.path, null, second);
        } else {
            if (s.pane !== s.panes[0] && !activate(s.panes[0])) return;
            s.panes.pop().el.remove();
        }
        const split = s.panes.length > 1;
        s.panesEl.classList.toggle('split', split);
        s.splitBtn.classList.toggle('active', split);
        s.splitBtn.textContent = split ? 'Single view' : 'Split view';
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

    async function navigate(path, selectPath, pane) {
        const s = page;
        if (!s || blocked()) return;
        pane = pane || s.pane;
        const seq = ++pane.loadSeq;
        pane.query = '';
        if (pane === s.pane) s.searchEl.value = '';
        pane.searching = false;
        try {
            const data = await api('/list' + q({ path }));
            if (page !== s || seq !== pane.loadSeq) return;
            pane.path = data.path;
            pane.parent = data.parent;
            pane.items = data.dirs.concat(data.files);
            pane.selected = new Set(selectPath && pane.items.some((i) => i.path === selectPath) ? [selectPath] : []);
            pane.anchor = selectPath || null;
            paintCrumb(pane);
            paintList(pane);
            if (pane === s.pane) { selectionChanged(); reveal(pane.path); }
        } catch (err) { toast(err.message, 'error'); }
    }

    async function reload(pane) {
        const s = page;
        if (!s) return;
        pane = pane || s.pane;
        if (pane.searching) return runSearch(true, pane);
        const keep = new Set(pane.selected);
        try {
            const data = await api('/list' + q({ path: pane.path }));
            if (page !== s) return;
            pane.items = data.dirs.concat(data.files);
            pane.selected = new Set(pane.items.filter((i) => keep.has(i.path)).map((i) => i.path));
            paintList(pane);
            if (pane === s.pane) selectionChanged(true);
        } catch (err) { toast(err.message, 'error'); }
    }

    async function runSearch(keepSelection, pane) {
        const s = page;
        if (!s) return;
        pane = pane || s.pane;
        const text = (pane === s.pane ? s.searchEl.value : pane.query).trim();
        pane.query = text;
        if (!text) { if (pane.searching) { pane.searching = false; navigate(pane.path, null, pane); } return; }
        if (!keepSelection && blocked()) return;
        const seq = ++pane.loadSeq;
        try {
            const params = { q: text };
            if (s.mode === 'local') params.path = pane.path;
            const data = await api('/search' + q(params));
            if (page !== s || seq !== pane.loadSeq) return;
            const keep = keepSelection ? new Set(pane.selected) : new Set();
            pane.searching = true;
            pane.items = data.dirs.concat(data.files);
            pane.truncated = !!data.truncated;
            pane.selected = new Set(pane.items.filter((i) => keep.has(i.path)).map((i) => i.path));
            pane.anchor = null;
            paintIndex(data.index);
            paintCrumb(pane);
            paintList(pane);
            if (pane === s.pane) selectionChanged(keepSelection);
        } catch (err) { toast(err.message, 'error'); }
    }

    function paintModes() {
        for (const b of page.modeBtns) b.classList.toggle('active', b.dataset.mode === page.mode);
    }

    function paintCrumb(pane) {
        const s = page;
        pane = pane || s.pane;
        if (pane.searching) {
            const where = s.mode === 'local' ? `in ${pane.path}` : 'in the entire library';
            pane.crumbEl.replaceChildren(el('span', { text: `Results ${where}${pane.truncated ? ' (first matches only)' : ''}` }));
            return;
        }
        const root = s.roots.map((r) => r.path).filter((r) => pane.path === r || pane.path.startsWith(r.replace(/\/+$/, '') + '/'))
            .sort((a, b) => b.length - a.length)[0] || '';
        const parts = [el('a', { href: '#', text: root, onclick: (e) => { e.preventDefault(); navigate(root, null, pane); } })];
        let acc = root.replace(/\/+$/, '');
        for (const name of pane.path.slice(root.length).split('/').filter(Boolean)) {
            acc += '/' + name;
            const target = acc;
            parts.push(el('span', { text: ' / ' }), el('a', { href: '#', text: name, onclick: (e) => { e.preventDefault(); navigate(target, null, pane); } }));
        }
        pane.crumbEl.replaceChildren(...parts);
    }

    function paintList(pane) {
        const s = page;
        pane = pane || s.pane;
        pane.allBox = el('input', {
            type: 'checkbox', 'aria-label': 'Select everything', title: 'Select all / none',
            onchange: () => { if (blocked()) { paintChecks(pane); return; } pane.selected = new Set(pane.allBox.checked ? pane.items.map((i) => i.path) : []); paintChecks(pane); selectionChanged(); },
        });
        const head = el('div', { class: 'fork-editor-row fork-editor-rowhead' }, [el('span', {}, pane.allBox), ...['Name', 'Title', 'Artist', 'Album', '#', 'Size', 'Modified'].map((t) => el('span', { text: t }))]);
        const rows = [];
        if (!pane.searching && pane.parent) {
            const up = el('div', { class: 'fork-editor-row', ondblclick: () => navigate(pane.parent, null, pane), title: 'Up one folder' },
                [el('span'), el('span', { text: '📁 ..' }), ...Array.from({ length: 6 }, () => el('span'))]);
            dropTarget(up, () => pane.parent);
            rows.push(up);
        }
        for (const item of pane.items) {
            const where = pane.searching ? item.path.slice(0, item.path.length - item.name.length - 1) : '';
            item.box = el('input', {
                type: 'checkbox', 'aria-label': `Select ${item.name}`,
                onclick: (e) => e.stopPropagation(),            // ticking never replaces the selection
                ondblclick: (e) => e.stopPropagation(),
                onchange: () => {
                    if (blocked()) { paintChecks(); return; }
                    if (item.box.checked) pane.selected.add(item.path); else pane.selected.delete(item.path);
                    pane.anchor = item.path;
                    paintChecks();
                    selectionChanged();
                },
            });
            const row = el('div', {
                class: 'fork-editor-row', title: item.path,
                onmousedown: (e) => { if (e.shiftKey) e.preventDefault(); },   // no text selection on shift+click
                onclick: (e) => clickRow(item, e),
                ondblclick: () => (item.kind === 'dir' ? navigate(item.path) : pane.searching ? navigate(where, item.path) : null),
                oncontextmenu: (e) => rowMenu(item, where, e),
            }, [
                el('span', { class: 'fork-editor-tick' }, item.box),
                el('span', { class: 'fork-editor-name' }, [`${ICONS[item.kind] || ICONS.other} ${item.name}`, where ? el('small', { text: where }) : null]),
                el('span', { text: item.title || '' }), el('span', { text: item.artist || '' }), el('span', { text: item.album || '' }),
                el('span', { text: item.track || '' }),
                el('span', { text: item.kind === 'dir' ? '' : sizeText(item.size || 0) }),
                el('span', { text: item.kind === 'dir' ? '' : dateText(item.mtime) }),
            ]);
            dragSource(row, () => (pane.selected.has(item.path) ? chosen().map((i) => i.path) : [item.path]));
            if (item.kind === 'dir') dropTarget(row, () => item.path);
            item.row = row;
            rows.push(row);
        }
        if (!pane.items.length) rows.push(el('div', { class: 'fork-album-note', text: pane.searching ? 'Nothing found.' : 'This folder is empty.' }));
        pane.listEl.replaceChildren(head, ...rows);
        paintChecks(pane);
    }

    // Rows, their tick boxes and the header box follow the selection (and
    // cut items are dimmed until they are pasted).
    function paintChecks(pane) {
        const s = page;
        pane = pane || s.pane;
        const cut = s.clip && s.clip.mode === 'cut' ? new Set(s.clip.items.map((i) => i.path)) : null;
        for (const i of pane.items) {
            const on = pane.selected.has(i.path);
            if (i.row) { i.row.classList.toggle('selected', on); i.row.classList.toggle('cut', !!cut && cut.has(i.path)); }
            if (i.box) i.box.checked = on;
        }
        if (pane.allBox) {
            pane.allBox.checked = pane.items.length > 0 && pane.selected.size >= pane.items.length;
            pane.allBox.indeterminate = pane.selected.size > 0 && pane.selected.size < pane.items.length;
        }
    }

    function rowMenu(item, where, e) {
        const s = page;
        e.preventDefault();
        if (!s.selected.has(item.path)) {
            if (blocked()) return;
            s.selected = new Set([item.path]);
            s.anchor = item.path;
            paintChecks();
            selectionChanged();
        }
        const many = s.selected.size > 1;
        showMenu(e, [
            item.kind === 'dir' ? { label: 'Open', disabled: many, fn: () => navigate(item.path) }
                : s.searching ? { label: 'Show in its folder', disabled: many, fn: () => navigate(where, item.path) } : null,
            { label: many ? `Rename ${s.selected.size} items…` : 'Rename…', fn: () => openRename() },
            { label: 'Copy', fn: () => setClip('copy', picks()) },
            { label: 'Cut', fn: () => setClip('cut', picks()) },
            item.kind === 'dir' && !many ? { label: 'Paste into this folder', disabled: !s.clip, fn: () => paste(item.path) }
                : { label: 'Paste', disabled: !s.clip || s.searching, fn: () => paste(s.path) },
            { label: 'Move to…', fn: () => moveTo(picks()) },
            { label: many ? `Delete ${s.selected.size} items` : 'Delete', danger: true, fn: () => confirmDelete(picks()) },
        ]);
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
        paintChecks();
        selectionChanged();
    }

    function onListKey(e) {
        const s = page;
        if (!s || e.target.tagName === 'INPUT' && e.target.type !== 'checkbox') return;
        const mod = e.ctrlKey || e.metaKey;
        const key = e.key.toLowerCase();
        if (e.key === 'F2') { e.preventDefault(); openRename(); }
        if (e.key === 'Delete' && s.selected.size) { e.preventDefault(); confirmDelete(picks()); }
        if (mod && key === 'c' && s.selected.size) { e.preventDefault(); setClip('copy', picks()); }
        if (mod && key === 'x' && s.selected.size) { e.preventDefault(); setClip('cut', picks()); }
        if (mod && key === 'v' && s.clip && !s.searching) { e.preventDefault(); paste(s.path); }
        if (mod && key === 'a') {
            e.preventDefault();
            if (blocked()) return;
            s.selected = new Set(s.items.map((i) => i.path));
            paintChecks();
            selectionChanged();
        }
        if (e.key === 'Enter' && s.selected.size === 1) {
            const item = s.items.find((i) => s.selected.has(i.path));
            if (item && item.kind === 'dir') navigate(item.path);
        }
    }

    const chosen = () => page.items.filter((i) => page.selected.has(i.path));
    // the selection as plain {path, name, kind}, safe to keep after the list changes
    const picks = () => chosen().map((i) => ({ path: i.path, name: i.name, kind: i.kind }));

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
        paintButtons();
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
        if (page === s) { paintActions(); for (const pane of s.panes) await reload(pane); }
    }

    // ── renaming ─────────────────────────────────────────────────────────

    function dialog(className, children) {
        const node = el('div', { class: 'fork-album-overlay fork-editor-dialog' }, el('div', { class: `fork-album-dialog ${className}` }, children));
        node.addEventListener('mousedown', (e) => { if (e.target === node) node.remove(); });
        node.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); node.remove(); } });
        document.body.append(node);
        return node;
    }

    function paintButtons() {
        const s = page;
        const any = s.selected.size > 0;
        for (const name of ['copy', 'cut', 'moveTo', 'del', 'rename']) s.btns[name].disabled = !any;
        s.btns.paste.disabled = !s.clip || s.searching;
        s.btns.paste.textContent = s.clip ? `Paste ${s.clip.items.length}` : 'Paste';
    }

    // The list, the tree and the tree filter after files or folders changed.
    // `gone` are paths that no longer exist where they were.
    async function refreshAfter(folders, gone, fallback) {
        const s = page;
        if (!s) return;
        for (const folder of new Set(folders)) await refreshTree(folder);
        if (page !== s) return;
        if (s.treeFilterEl.value.trim()) filterTree();
        for (const pane of s.panes) {
            if ((gone || []).some((p) => inside(pane.path, p))) await navigate(fallback || parentOf(gone[0]), null, pane);
            else await reload(pane);
        }
    }

    // ── right-click menu ─────────────────────────────────────────────────

    function closeMenu() {
        if (page && page.menu) { page.menu.remove(); page.menu = null; }
    }

    function showMenu(e, entries) {
        const s = page;
        closeMenu();
        const menu = s.menu = el('div', { class: 'fork-editor-menu', role: 'menu' }, entries.filter(Boolean).map((entry) => el('button', {
            type: 'button', role: 'menuitem', class: entry.danger ? 'danger' : '', text: entry.label, disabled: !!entry.disabled,
            onclick: () => { closeMenu(); entry.fn(); },
        })));
        s.root.append(menu);
        const box = menu.getBoundingClientRect();
        menu.style.left = `${Math.max(4, Math.min(e.clientX, window.innerWidth - box.width - 4))}px`;
        menu.style.top = `${Math.max(4, Math.min(e.clientY, window.innerHeight - box.height - 4))}px`;
        setTimeout(() => {
            const away = (ev) => {
                if (ev.type === 'keydown' && ev.key !== 'Escape') return;
                if (ev.type === 'mousedown' && menu.contains(ev.target)) return;
                closeMenu();
                for (const type of ['mousedown', 'keydown', 'wheel']) document.removeEventListener(type, away, true);
            };
            for (const type of ['mousedown', 'keydown', 'wheel']) document.addEventListener(type, away, true);
        }, 0);
    }

    // ── copy, cut, paste, move to, delete ────────────────────────────────

    function setClip(mode, items) {
        const s = page;
        if (!s || !items.length) return;
        s.clip = { mode, items };
        paintChecks();
        paintButtons();
        toast(`${items.length} item${items.length === 1 ? '' : 's'} ${mode === 'cut' ? 'cut' : 'copied'} — paste into a folder`);
    }

    async function paste(dest) {
        const s = page;
        if (!s || !s.clip || !dest || blocked()) return;
        const clip = s.clip;
        const paths = clip.items.map((i) => i.path);
        try {
            if (clip.mode === 'cut') {
                if (paths.some((p) => inside(dest, p))) { toast('A folder cannot be moved into itself', 'error'); return; }
                const data = await api('/move', { paths, destination: dest });
                s.clip = null;
                if (data.errors.length) toast(`${data.moved.length} moved, ${data.errors.length} not — ${data.errors[0]}`, 'error');
                else toast(`Moved ${data.moved.length} item${data.moved.length === 1 ? '' : 's'}`);
                await refreshAfter(paths.map(parentOf).concat([dest]), paths, dest);
            } else {
                const data = await api('/copy', { paths, destination: dest });
                if (data.errors.length) toast(`${data.copied.length} copied, ${data.errors.length} not — ${data.errors[0]}`, 'error');
                else toast(`Copied ${data.copied.length} item${data.copied.length === 1 ? '' : 's'}`);
                await refreshAfter([dest], [], dest);
            }
        } catch (err) { toast(err.message, 'error'); }
        if (page === s) { paintChecks(); paintButtons(); }
    }

    function moveTo(items) {
        const s = page;
        if (!s || !items.length || blocked()) return;
        if (typeof window.forkOpenFolderBrowser !== 'function') { toast('The folder picker is not available', 'error'); return; }
        const paths = items.map((i) => i.path);
        window.forkOpenFolderBrowser(s.path, (picked) => {
            if (!picked) return;
            if (paths.some((p) => inside(picked, p))) { toast('A folder cannot be moved into itself', 'error'); return; }
            if (!paths.some((p) => parentOf(p) !== picked)) { toast('They are already in that folder'); return; }
            confirmMove(paths, picked);
        });
    }

    async function confirmDelete(items) {
        const s = page;
        if (!s || !items.length || blocked()) return;
        const paths = items.map((i) => i.path);
        let what = '';
        try {
            const d = await api('/delete', { paths, preview: true });
            what = `${d.files.toLocaleString()} file${d.files === 1 ? '' : 's'}${d.folders ? ` in ${d.folders.toLocaleString()} folder${d.folders === 1 ? '' : 's'}` : ''}, ${sizeText(d.bytes)}`;
        } catch (err) { toast(err.message, 'error'); return; }
        if (page !== s) return;
        const status = el('span', { class: 'fork-album-status' });
        const go = el('button', { class: 'download-control-btn primary fork-editor-danger', type: 'button', text: 'Delete' });
        go.addEventListener('click', async () => {
            go.disabled = true;
            try {
                const data = await api('/delete', { paths });
                root.remove();
                if (data.errors.length) toast(`${data.deleted.length} deleted, ${data.errors.length} not — ${data.errors[0]}`, 'error');
                else toast(`Deleted ${data.deleted.length} item${data.deleted.length === 1 ? '' : 's'}`);
                if (page !== s) return;
                if (s.clip) { s.clip.items = s.clip.items.filter((i) => !paths.some((p) => inside(i.path, p))); if (!s.clip.items.length) s.clip = null; }
                await refreshAfter(paths.map(parentOf), paths);
            } catch (err) { status.textContent = err.message; status.classList.add('fork-volume-problem'); go.disabled = false; }
        });
        const root = dialog('fork-editor-rename', [
            el('h3', { text: `Delete ${items.length} item${items.length === 1 ? '' : 's'}?` }),
            el('div', { class: 'fork-album-note', text: `${items.slice(0, 4).map((i) => i.name).join(', ')}${items.length > 4 ? ` and ${items.length - 4} more` : ''}` }),
            el('div', { class: 'fork-editor-moveto' }, [el('strong', { text: what }), ' will be deleted for good. This cannot be undone.']),
            el('div', { class: 'fork-album-note', text: 'A track\'s lyrics files are deleted with it, and the library forgets the deleted tracks.' }),
            el('div', { class: 'fork-album-actions' }, [
                status,
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Cancel', onclick: () => root.remove() }),
                go,
            ]),
        ]);
    }

    // ── renaming: one name, or find and replace ──────────────────────────

    function openRename(given) {
        const s = page;
        const items = given || picks();
        if (!s || !items.length || blocked()) return;
        const paths = items.map((i) => i.path);
        const dirParents = [...new Set(items.filter((i) => i.kind === 'dir').map((i) => parentOf(i.path)))];
        const one = items.length === 1 ? items[0] : null;
        let root = null;

        // tab 1: a new name for one item
        const nameInput = el('input', { class: 'fork-album-input', type: 'text', value: one ? one.name : '', 'aria-label': 'New name' });
        const nameStatus = el('span', { class: 'fork-album-status' });
        const renameOne = async () => {
            if (!nameInput.value.trim() || nameInput.value === one.name) { root.remove(); return; }
            try {
                const done = await api('/rename', { path: one.path, name: nameInput.value });
                root.remove();
                toast(done.sidecars.length ? `Renamed, with ${done.sidecars.length} lyrics file${done.sidecars.length === 1 ? '' : 's'}` : 'Renamed');
                if (s.selected.has(one.path)) s.selected = new Set([done.path]);
                await refreshAfter(dirParents, one.kind === 'dir' ? [one.path] : [], done.path);
            } catch (err) { nameStatus.textContent = err.message; nameStatus.classList.add('fork-volume-problem'); }
        };
        nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') renameOne(); });
        const single = el('div', { class: 'fork-editor-tabpane' }, one ? [
            one.kind === 'audio' ? el('div', { class: 'fork-album-note', text: 'Lyrics files with the same name (.lrc, .txt) are renamed with it.' }) : null,
            nameInput,
            el('div', { class: 'fork-album-actions' }, [
                nameStatus,
                el('button', { class: 'download-control-btn secondary', type: 'button', text: 'Cancel', onclick: () => root.remove() }),
                el('button', { class: 'download-control-btn primary', type: 'button', text: 'Rename', onclick: renameOne }),
            ]),
        ] : [el('div', { class: 'fork-album-note', text: 'Select a single item to give it a new name.' })]);

        // tab 2: find and replace over every selected name
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
                if (!given) s.selected = new Set(data.items.map((i) => i.path));
                await refreshAfter(dirParents, items.filter((i) => i.kind === 'dir').map((i) => i.path), data.items[0] && data.items[0].path);
            } catch (err) { status.textContent = err.message; status.classList.add('fork-volume-problem'); applyBtn.disabled = false; }
        });
        const bulk = el('div', { class: 'fork-editor-tabpane' }, [
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

        const tabs = [
            { button: el('button', { type: 'button', class: 'fork-editor-tab', role: 'tab', text: 'New name', disabled: !one, title: one ? '' : 'For a single item' }), pane: single, focus: nameInput },
            { button: el('button', { type: 'button', class: 'fork-editor-tab', role: 'tab', text: 'Find and replace' }), pane: bulk, focus: find },
        ];
        const show = (tab) => {
            for (const t of tabs) { t.button.classList.toggle('active', t === tab); t.pane.hidden = t !== tab; }
            tab.focus.focus();
        };
        for (const t of tabs) t.button.addEventListener('click', () => show(t));
        root = dialog('fork-volume-editor fork-editor-renamer', [
            el('h3', { text: one ? `Rename ${one.kind === 'dir' ? 'folder' : 'file'}` : `Rename ${items.length} items` }),
            el('div', { class: 'fork-editor-tabs', role: 'tablist' }, tabs.map((t) => t.button)),
            single, bulk,
        ]);
        preview();
        show(one ? tabs[0] : tabs[1]);
        if (one) {
            const dot = one.kind === 'dir' ? -1 : one.name.lastIndexOf('.');
            nameInput.setSelectionRange(0, dot > 0 ? dot : one.name.length);
        }
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
