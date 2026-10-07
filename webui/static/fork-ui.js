// fork (itsdmd/SoulSync): settings panel for the fork's features — local LLM
// (Ollama) options, the name-translation table and artist tagging rules.
// Self-contained on purpose: it adds one sidebar entry and one modal, and
// touches nothing upstream owns. Backed by api/fork.py. See FORK.md.
(function () {
    'use strict';

    const API = '/api/fork';
    const TOGGLES = [
        ['search_terms.enabled', 'Suggest extra search terms',
            'Ask the model for alternative artist/title spellings (romanized, original script, official English) and search with them after the standard queries.'],
        ['search_terms.match_variants', 'Accept matches on a suggested name',
            'Lets a file named with the suggested spelling pass the match check when nothing matched the original name.'],
        ['translate.titles', 'Translate CJK song titles', ''],
        ['translate.albums', 'Translate CJK album names',
            'Each album name is translated once and reused, so every track of an album gets the same name.'],
        ['translate.apply_to_tags', 'Apply to tags', 'Write artist rules and translated names into the file tags.'],
        ['translate.apply_to_paths', 'Apply to folders and file names', ''],
        ['translate.write_original_tags', 'Keep originals in SOULSYNC_ORIGINAL_* tags', ''],
        ['lyrics.enabled', 'Translate CJK lyrics', ''],
        ['import.rename_only_auto', 'Automatic import: rename only',
            'The import watcher moves and renames files without changing their tags, artwork or audio. Also on the Import page settings.'],
        ['artist_names.enabled', 'Apply artist name rules', ''],
        ['artist_names.auto_lookup', 'Look up CJK artist names on MusicBrainz',
            'Uses the artist\'s official alias. No model involved; rules you add by hand always win.'],
    ];

    let settings = null;
    let tasks = {};
    let overlay = null;

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

    async function api(path, method, body) {
        const resp = await fetch(API + path, {
            method: method || 'GET',
            headers: body ? { 'Content-Type': 'application/json' } : undefined,
            body: body ? JSON.stringify(body) : undefined,
        });
        let data = {};
        try { data = await resp.json(); } catch (_) { /* non-JSON error page */ }
        if (!resp.ok || data.success === false) throw new Error(data.error || `Request failed (${resp.status})`);
        return data;
    }

    const getPath = (obj, path) => path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
    function setPath(obj, path, value) {
        const keys = path.split('.');
        let node = obj;
        for (const k of keys.slice(0, -1)) node = node[k] = node[k] || {};
        node[keys[keys.length - 1]] = value;
    }

    function injectStyle() {
        if (document.getElementById('fork-ui-style')) return;
        document.head.append(el('style', { id: 'fork-ui-style', text: `
.fork-overlay{position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.7);backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center}
.fork-modal{width:860px;max-width:96vw;height:86vh;background:rgba(14,14,14,.98);border:1px solid rgba(255,255,255,.08);border-radius:18px;box-shadow:0 24px 80px rgba(0,0,0,.7);display:flex;flex-direction:column;overflow:hidden;color:#e8e8e8;font-size:14px}
.fork-head{display:flex;align-items:center;gap:16px;padding:18px 22px 0}
.fork-head h2{margin:0;font-size:18px;font-weight:600;flex:1}
.fork-x{background:none;border:0;color:#aaa;font-size:24px;cursor:pointer;line-height:1}
.fork-tabs{display:flex;gap:6px;padding:14px 22px 0;border-bottom:1px solid rgba(255,255,255,.07)}
.fork-tab{background:none;border:0;border-bottom:2px solid transparent;color:#999;padding:8px 12px;cursor:pointer;font-size:14px}
.fork-tab.active{color:#fff;border-bottom-color:rgb(var(--accent-rgb,29,185,84))}
.fork-body{flex:1;overflow-y:auto;padding:18px 22px}
.fork-foot{display:flex;justify-content:flex-end;gap:10px;padding:14px 22px;border-top:1px solid rgba(255,255,255,.07)}
.fork-section{margin:0 0 22px}
.fork-section h3{margin:0 0 10px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#8a8a8a;font-weight:600}
.fork-row{display:flex;align-items:center;gap:12px;padding:7px 0}
.fork-row label{flex:0 0 230px;color:#ccc}
.fork-row .fork-help{color:#777;font-size:12px}
.fork-input,.fork-select{flex:1;min-width:0;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:8px;color:#eee;padding:7px 10px;font-size:14px}
.fork-input:focus,.fork-select:focus{outline:none;border-color:rgba(var(--accent-rgb,29,185,84),.6)}
.fork-select option{background:#181818}
.fork-check{display:flex;gap:10px;align-items:flex-start;padding:6px 0;cursor:pointer}
.fork-check input{margin-top:3px;accent-color:rgb(var(--accent-rgb,29,185,84))}
.fork-check small{display:block;color:#777;font-size:12px;margin-top:2px}
.fork-btn{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);color:#e8e8e8;border-radius:8px;padding:7px 14px;cursor:pointer;font-size:13px;white-space:nowrap}
.fork-btn:hover{background:rgba(255,255,255,.12)}
.fork-btn.primary{background:rgb(var(--accent-rgb,29,185,84));border-color:transparent;color:#000;font-weight:600}
.fork-btn.danger{color:#ff7b7b}
.fork-btn:disabled{opacity:.5;cursor:default}
.fork-bar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.fork-table{width:100%;border-collapse:collapse}
.fork-table th{text-align:left;font-size:12px;color:#8a8a8a;font-weight:600;padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.08)}
.fork-table td{padding:5px 8px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle}
.fork-table td.fork-orig{max-width:240px;word-break:break-word}
.fork-table td.fork-actions{white-space:nowrap;text-align:right}
.fork-tag{font-size:11px;padding:2px 7px;border-radius:999px;background:rgba(255,255,255,.07);color:#aaa}
.fork-tag.manual{background:rgba(var(--accent-rgb,29,185,84),.18);color:rgb(var(--accent-rgb,29,185,84))}
.fork-empty{color:#777;padding:24px 0;text-align:center}
.fork-note{color:#888;font-size:12px;margin:0 0 12px}
` }));
    }

    // ── LLM tab ───────────────────────────────────────────────────────────

    function renderLlm(body) {
        const modelList = el('datalist', { id: 'fork-models' });
        const loadModels = async () => {
            try {
                const data = await api('/models?url=' + encodeURIComponent(settings.ollama.url));
                modelList.replaceChildren(...data.models.map((m) => el('option', { value: m })));
                toast(`Ollama reachable — ${data.models.length} models`);
            } catch (err) { toast(err.message, 'error'); }
        };

        const textRow = (label, path, help, attrs) => el('div', { class: 'fork-row' }, [
            el('label', { text: label }),
            el('input', Object.assign({
                class: 'fork-input', value: getPath(settings, path) ?? '',
                oninput: (e) => setPath(settings, path, e.target.value),
            }, attrs || {})),
            help ? el('span', { class: 'fork-help', text: help }) : null,
        ]);

        const connection = el('div', { class: 'fork-section' }, [
            el('h3', { text: 'Ollama' }),
            el('div', { class: 'fork-row' }, [
                el('label', { text: 'Server URL' }),
                el('input', {
                    class: 'fork-input', value: settings.ollama.url,
                    oninput: (e) => { settings.ollama.url = e.target.value; },
                }),
                el('button', { class: 'fork-btn', text: 'Check', onclick: loadModels }),
            ]),
            textRow('Request timeout (seconds)', 'ollama.timeout', 'Loading a model cold can take over a minute.', { type: 'number', min: '10' }),
        ]);

        const models = el('div', { class: 'fork-section' }, [el('h3', { text: 'Model per task' }), modelList]);
        for (const [task, label] of Object.entries(tasks)) {
            const input = el('input', {
                class: 'fork-input', list: 'fork-models', value: settings.models[task] || '',
                oninput: (e) => { settings.models[task] = e.target.value; },
            });
            models.append(el('div', { class: 'fork-row' }, [
                el('label', { text: label }),
                input,
                el('button', {
                    class: 'fork-btn', text: 'Test',
                    onclick: async (e) => {
                        e.target.disabled = true;
                        try {
                            await save(true);
                            const data = await api('/test', 'POST', { task });
                            toast(`${data.model} answered`);
                        } catch (err) { toast(err.message, 'error'); }
                        e.target.disabled = false;
                    },
                }),
            ]));
        }

        const naming = el('div', { class: 'fork-section' }, [
            el('h3', { text: 'Translated names' }),
            textRow('Name format', 'translate.template', '{translated} and {original}'),
            textRow('Target language', 'translate.target_language'),
            el('div', { class: 'fork-row' }, [
                el('label', { text: 'Translated lyrics' }),
                el('select', {
                    class: 'fork-select',
                    onchange: (e) => { settings.lyrics.mode = e.target.value; },
                }, [
                    el('option', { value: 'inline', text: 'Under each original line, same file', selected: settings.lyrics.mode !== 'separate' }),
                    el('option', { value: 'separate', text: 'Translation only; original kept as .original.lrc', selected: settings.lyrics.mode === 'separate' }),
                ]),
            ]),
            textRow('Search suggestions per track', 'search_terms.max_variants', '', { type: 'number', min: '0', max: '8' }),
        ]);

        const features = el('div', { class: 'fork-section' }, [el('h3', { text: 'Features' })]);
        for (const [path, label, help] of TOGGLES) {
            features.append(el('label', { class: 'fork-check' }, [
                el('input', {
                    type: 'checkbox', checked: !!getPath(settings, path),
                    onchange: (e) => setPath(settings, path, e.target.checked),
                }),
                el('span', {}, [label, help ? el('small', { text: help }) : null]),
            ]));
        }

        body.replaceChildren(connection, models, naming, features);
        api('/models?url=' + encodeURIComponent(settings.ollama.url))
            .then((data) => modelList.replaceChildren(...data.models.map((m) => el('option', { value: m }))))
            .catch(() => { /* shown when the user presses Check */ });
    }

    async function save(quiet) {
        const data = await api('/settings', 'POST', settings);
        settings = data.settings;
        if (!quiet) toast('Saved');
    }

    // ── Translations tab ──────────────────────────────────────────────────

    function renderTranslations(body) {
        const state = { kind: '', search: '' };
        const tbody = el('tbody');
        const count = el('span', { class: 'fork-help' });

        const load = async () => {
            const q = new URLSearchParams({ kind: state.kind, search: state.search, limit: '300' });
            try {
                const data = await api('/translations?' + q);
                count.textContent = `${data.total} saved`;
                tbody.replaceChildren(...data.items.map(row));
                if (!data.items.length) {
                    tbody.append(el('tr', {}, el('td', { colspan: '4', class: 'fork-empty', text: 'Nothing translated yet.' })));
                }
            } catch (err) { toast(err.message, 'error'); }
        };

        const row = (item) => {
            const input = el('input', { class: 'fork-input', value: item.translated });
            const commit = async () => {
                const value = input.value.trim();
                if (!value || value === item.translated) return;
                try {
                    await api('/translations', 'PUT', { kind: item.kind, original: item.original, translated: value });
                    toast('Translation updated');
                    load();
                } catch (err) { toast(err.message, 'error'); }
            };
            input.addEventListener('keydown', (e) => { if (e.key === 'Enter') commit(); });
            return el('tr', {}, [
                el('td', {}, el('span', { class: 'fork-tag' + (item.user_edited ? ' manual' : ''), text: item.kind + (item.user_edited ? ' · edited' : '') })),
                el('td', { class: 'fork-orig', text: item.original }),
                el('td', {}, input),
                el('td', { class: 'fork-actions' }, [
                    el('button', { class: 'fork-btn', text: 'Save', onclick: commit }),
                    ' ',
                    el('button', {
                        class: 'fork-btn danger', text: 'Forget', title: 'Remove so it is translated again next time',
                        onclick: async () => {
                            try {
                                await api('/translations', 'DELETE', { kind: item.kind, original: item.original });
                                load();
                            } catch (err) { toast(err.message, 'error'); }
                        },
                    }),
                ]),
            ]);
        };

        const addKind = el('select', { class: 'fork-select', style: 'flex:0 0 100px' }, [
            el('option', { value: 'album', text: 'album' }), el('option', { value: 'title', text: 'title' }),
        ]);
        const addOriginal = el('input', { class: 'fork-input', placeholder: 'Original name' });
        const addTranslated = el('input', { class: 'fork-input', placeholder: 'Translation' });

        body.replaceChildren(
            el('p', { class: 'fork-note', text: 'Every translated album and title is remembered here and reused. Edit one and the model will never overwrite it; new downloads and imports use your version.' }),
            el('div', { class: 'fork-bar' }, [
                el('select', { class: 'fork-select', style: 'flex:0 0 130px', onchange: (e) => { state.kind = e.target.value; load(); } }, [
                    el('option', { value: '', text: 'All' }), el('option', { value: 'album', text: 'Albums' }), el('option', { value: 'title', text: 'Titles' }),
                ]),
                el('input', { class: 'fork-input', placeholder: 'Search…', oninput: (e) => { state.search = e.target.value; load(); } }),
                count,
            ]),
            el('div', { class: 'fork-bar' }, [
                addKind, addOriginal, addTranslated,
                el('button', {
                    class: 'fork-btn', text: 'Suggest', title: 'Ask the model',
                    onclick: async (e) => {
                        if (!addOriginal.value.trim()) return;
                        e.target.disabled = true;
                        try {
                            const data = await api('/translations/preview', 'POST', { kind: addKind.value, value: addOriginal.value });
                            toast(data.changed ? data.result : 'No translation produced', data.changed ? 'success' : 'error');
                            load();
                        } catch (err) { toast(err.message, 'error'); }
                        e.target.disabled = false;
                    },
                }),
                el('button', {
                    class: 'fork-btn primary', text: 'Add',
                    onclick: async () => {
                        try {
                            await api('/translations', 'PUT', { kind: addKind.value, original: addOriginal.value, translated: addTranslated.value });
                            addOriginal.value = ''; addTranslated.value = '';
                            load();
                        } catch (err) { toast(err.message, 'error'); }
                    },
                }),
            ]),
            el('table', { class: 'fork-table' }, [
                el('thead', {}, el('tr', {}, ['Type', 'Original', 'Translation', ''].map((t) => el('th', { text: t })))),
                tbody,
            ]),
        );
        load();
    }

    // ── Artist rules tab ──────────────────────────────────────────────────

    function renderArtists(body) {
        let search = '';
        const tbody = el('tbody');

        const load = async () => {
            try {
                const data = await api('/artist-names?' + new URLSearchParams({ search }));
                tbody.replaceChildren(...data.items.map(row));
                if (!data.items.length) {
                    tbody.append(el('tr', {}, el('td', { colspan: '4', class: 'fork-empty', text: 'No rules yet.' })));
                }
            } catch (err) { toast(err.message, 'error'); }
        };

        const put = async (original, replacement) => {
            await api('/artist-names', 'PUT', { original, replacement });
            toast('Rule saved');
            load();
        };

        const row = (item) => {
            const input = el('input', { class: 'fork-input', value: item.replacement });
            const commit = () => {
                const value = input.value.trim();
                if (value && value !== item.replacement) put(item.original, value).catch((err) => toast(err.message, 'error'));
            };
            input.addEventListener('keydown', (e) => { if (e.key === 'Enter') commit(); });
            return el('tr', {}, [
                el('td', {}, el('span', { class: 'fork-tag' + (item.source === 'manual' ? ' manual' : ''), text: item.source })),
                el('td', { class: 'fork-orig', text: item.original }),
                el('td', {}, input),
                el('td', { class: 'fork-actions' }, [
                    el('button', { class: 'fork-btn', text: 'Save', onclick: commit }),
                    ' ',
                    el('button', {
                        class: 'fork-btn danger', text: 'Delete',
                        onclick: async () => {
                            try { await api('/artist-names', 'DELETE', { original: item.original }); load(); }
                            catch (err) { toast(err.message, 'error'); }
                        },
                    }),
                ]),
            ]);
        };

        const addOriginal = el('input', { class: 'fork-input', placeholder: 'Name as the source reports it (e.g. 澤野弘之)' });
        const addReplacement = el('input', { class: 'fork-input', placeholder: 'Name to use instead' });

        body.replaceChildren(
            el('p', { class: 'fork-note', text: 'When a track\'s artist matches the left column, the right column is written to its tags and used for its folder. Rules found on MusicBrainz are added automatically; rules you save here always win.' }),
            el('div', { class: 'fork-bar' }, [
                addOriginal, addReplacement,
                el('button', {
                    class: 'fork-btn', text: 'Look up', title: 'Fetch the official name from MusicBrainz',
                    onclick: async (e) => {
                        if (!addOriginal.value.trim()) return;
                        e.target.disabled = true;
                        try {
                            const data = await api('/artist-names/lookup', 'POST', { original: addOriginal.value });
                            if (data.replacement) addReplacement.value = data.replacement;
                            else toast('MusicBrainz has no alternative name for this artist', 'error');
                        } catch (err) { toast(err.message, 'error'); }
                        e.target.disabled = false;
                    },
                }),
                el('button', {
                    class: 'fork-btn primary', text: 'Add rule',
                    onclick: () => put(addOriginal.value.trim(), addReplacement.value.trim())
                        .then(() => { addOriginal.value = ''; addReplacement.value = ''; })
                        .catch((err) => toast(err.message, 'error')),
                }),
            ]),
            el('div', { class: 'fork-bar' }, [
                el('input', { class: 'fork-input', placeholder: 'Search rules…', oninput: (e) => { search = e.target.value; load(); } }),
            ]),
            el('table', { class: 'fork-table' }, [
                el('thead', {}, el('tr', {}, ['Source', 'Original', 'Use instead', ''].map((t) => el('th', { text: t })))),
                tbody,
            ]),
        );
        load();
    }

    // ── modal shell ───────────────────────────────────────────────────────

    const TABS = [['LLM', renderLlm, true], ['Translations', renderTranslations, false], ['Artist rules', renderArtists, false]];

    function close() {
        if (overlay) overlay.remove();
        overlay = null;
        document.removeEventListener('keydown', onKey);
    }
    function onKey(e) { if (e.key === 'Escape') close(); }

    async function open() {
        if (overlay) return;
        injectStyle();
        try {
            const data = await api('/settings');
            settings = data.settings;
            tasks = data.tasks;
        } catch (err) { toast(err.message, 'error'); return; }

        const body = el('div', { class: 'fork-body' });
        const saveBtn = el('button', {
            class: 'fork-btn primary', text: 'Save',
            onclick: () => save().catch((err) => toast(err.message, 'error')),
        });
        const tabBar = el('div', { class: 'fork-tabs' });
        TABS.forEach(([label, render, hasSave], index) => {
            const tab = el('button', {
                class: 'fork-tab' + (index === 0 ? ' active' : ''), text: label,
                onclick: () => {
                    tabBar.querySelectorAll('.fork-tab').forEach((t) => t.classList.remove('active'));
                    tab.classList.add('active');
                    saveBtn.style.display = hasSave ? '' : 'none';
                    render(body);
                },
            });
            tabBar.append(tab);
        });

        overlay = el('div', { class: 'fork-overlay', onclick: (e) => { if (e.target === overlay) close(); } },
            el('div', { class: 'fork-modal' }, [
                el('div', { class: 'fork-head' }, [
                    el('h2', { text: 'LLM & Tagging' }),
                    el('button', { class: 'fork-x', text: '×', 'aria-label': 'Close', onclick: close }),
                ]),
                tabBar,
                body,
                el('div', { class: 'fork-foot' }, [el('button', { class: 'fork-btn', text: 'Close', onclick: close }), saveBtn]),
            ]));
        document.body.append(overlay);
        document.addEventListener('keydown', onKey);
        renderLlm(body);
    }

    function addNavEntry() {
        if (document.getElementById('fork-nav-button')) return;
        const anchor = document.querySelector('.nav-button[data-page="settings"]');
        if (!anchor) return;
        const button = el('a', { class: 'nav-button', id: 'fork-nav-button', href: '#', title: 'Local LLM, translations and artist tagging rules' });
        const icon = el('span', { class: 'nav-icon' });
        // Static markup only — no user data is interpolated here.
        icon.innerHTML = '<svg class="nav-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 8h6M8 5v3M6 14c2-1 4-3.500 5-6M7.500 11c1 1.500 2.500 2.500 4 3"/><path d="M13 20l4-9 4 9M14.500 17h5"/></svg>';
        button.append(icon, el('span', { class: 'nav-text', text: 'LLM & Tagging' }));
        button.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); open(); });
        anchor.insertAdjacentElement('afterend', button);
    }

    window.openForkSettings = open;
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addNavEntry);
    else addNavEntry();
})();
