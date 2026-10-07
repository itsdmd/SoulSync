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
    let defaults = { artists: { detect: '' }, translate: { keep_terms: '' } };
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
        // The app shares identical GETs made within 2.5s (fetch-dedupe.js). These
        // lists are re-read right after an edit, and a shared response would be
        // the list from BEFORE the edit — a request carrying a signal opts out.
        const resp = await fetch(API + path, {
            method: method || 'GET',
            headers: body ? { 'Content-Type': 'application/json' } : undefined,
            body: body ? JSON.stringify(body) : undefined,
            signal: new AbortController().signal,
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
.fork-orig-link{color:inherit;text-decoration:underline dotted rgba(255,255,255,.35);text-underline-offset:3px;cursor:pointer}
.fork-orig-link:hover{color:rgb(var(--accent-light-rgb,var(--accent-rgb,29,185,84)));text-decoration-style:solid}
.fork-pop-layer{position:fixed;inset:0;z-index:100003;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center}
.fork-pop{width:560px;max-width:94vw;max-height:84vh;display:flex;flex-direction:column;gap:12px;padding:18px 20px;background:#141414;border:1px solid rgba(255,255,255,.1);border-radius:14px;box-shadow:0 24px 80px rgba(0,0,0,.7);color:#e8e8e8;font-size:14px}
.fork-pop.wide{width:820px}
.fork-pop-head{display:flex;align-items:center;gap:12px}
.fork-pop-head h3{margin:0;flex:1;font-size:16px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fork-pop-body{flex:1;min-height:80px;overflow-y:auto;display:flex;flex-direction:column;gap:12px}
.fork-facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px 18px}
.fork-fact span{display:block;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:#8a8a8a}
.fork-fact strong{font-weight:500;word-break:break-word}
.fork-pop-album{border:1px solid rgba(255,255,255,.07);border-radius:8px;padding:10px 12px}
.fork-pop-album-head{display:flex;justify-content:space-between;gap:12px;margin-bottom:4px}
.fork-pop-album-head span{color:#999;font-size:12px;white-space:nowrap}
.fork-pop-path{color:#8a8a8a;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:6px}
.fork-pop-num{width:44px;color:#8a8a8a;font-variant-numeric:tabular-nums;white-space:nowrap}
.fork-pop-file{max-width:260px;color:#9a9a9a;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fork-old{color:#8a8a8a;text-decoration:line-through;font-size:12px}
.fork-where{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#999}
.fork-where.fork-where-set{color:#e8e8e8;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;direction:rtl;text-align:left}
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

        const keepTerms = el('textarea', {
            class: 'fork-input', rows: '4', 'aria-label': 'Terms that are not translations',
            style: 'resize:vertical;font-family:inherit;line-height:1.45',
            oninput: (e) => { settings.translate.keep_terms = e.target.value; },
        });
        keepTerms.value = settings.translate.keep_terms != null ? settings.translate.keep_terms : '';
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
            el('div', { class: 'fork-row', style: 'align-items:flex-start' }, [
                el('label', { text: 'Terms that are not translations', style: 'padding-top:7px' }),
                keepTerms,
                el('button', {
                    class: 'fork-btn', type: 'button', text: 'Defaults', title: 'Restore the default list',
                    onclick: () => { settings.translate.keep_terms = defaults.translate.keep_terms; keepTerms.value = defaults.translate.keep_terms; },
                }),
            ]),
            el('p', { class: 'fork-note', style: 'margin:4px 0 0', text:
                'Words that describe a release rather than name it, separated by commas. In “危機合約滌墨作戰 (Original Soundtrack)” the bracket is kept as it is and only the name is translated, '
                + 'instead of “Original Soundtrack” being taken for the translation. Whole words only, any letter case; a four-digit year always counts.' }),
        ]);

        // Artists: separate tags and a chosen separator are mutually exclusive —
        // the separator only exists when everything goes into one tag.
        settings.artists = settings.artists || {};
        const PRESETS = { semicolon: '; ', comma: ', ', slash: ' / ', ampersand: ' & ' };
        // mirrors pad_separator() in core/fork/artist_format.py
        const padded = (chars) => {
            const c = (chars || '').trim();
            if (!c) return '; ';
            if (/^[\u3000-\u303F\uFF00-\uFFEF\u30FB]+$/.test(c)) return c;
            if (c === ',' || c === ';') return c + ' ';
            return ` ${c} `;
        };
        const separatorSelect = el('select', {
            class: 'fork-select',
            onchange: (e) => { settings.artists.separator = e.target.value; syncSeparator(); },
        }, [['semicolon', 'Semicolon —  A; B'], ['comma', 'Comma —  A, B'], ['slash', 'Slash —  A / B'],
            ['ampersand', 'Ampersand —  A & B'], ['custom', 'Custom…']]
            .map(([value, text]) => el('option', { value, text, selected: (settings.artists.separator || 'semicolon') === value })));
        const customInput = el('input', {
            class: 'fork-input', style: 'flex:0 0 90px', maxlength: '6', placeholder: 'e.g. 、',
            'aria-label': 'Custom separator', value: settings.artists.custom_separator || '',
            oninput: (e) => { settings.artists.custom_separator = e.target.value; syncSeparator(); },
        });
        const separatorHelp = el('span', { class: 'fork-help' });
        const syncSeparator = () => {
            const split = !!settings.artists.split_tags;
            const custom = (settings.artists.separator || 'semicolon') === 'custom';
            separatorSelect.disabled = split;
            customInput.disabled = split;
            customInput.style.display = custom ? '' : 'none';
            const sep = custom ? padded(settings.artists.custom_separator) : (PRESETS[settings.artists.separator] || '; ');
            separatorHelp.textContent = split
                ? 'Not used while artists are split into separate tags.'
                : `Written as:  Artist A${sep}Artist B`;
        };
        const detectInput = el('input', {
            class: 'fork-input', 'aria-label': 'Extra separators to detect',
            value: settings.artists.detect != null ? settings.artists.detect : '',
            oninput: (e) => { settings.artists.detect = e.target.value; },
        });
        syncSeparator();
        const artists = el('div', { class: 'fork-section' }, [
            el('h3', { text: 'Multiple artists' }),
            el('label', { class: 'fork-check' }, [
                el('input', {
                    type: 'checkbox', checked: !!settings.artists.split_tags,
                    onchange: (e) => { settings.artists.split_tags = e.target.checked; syncSeparator(); },
                }),
                el('span', {}, ['Split artists into separate tags', el('small', {
                    text: 'Writes one tag per artist (ARTIST=Artist A, ARTIST=Artist B) instead of one combined value. Applies to Artist and Album artist, on downloads, imports and auto-tagging.',
                })]),
            ]),
            el('div', { class: 'fork-row' }, [el('label', { text: 'Separator in a combined tag' }), separatorSelect, customInput, separatorHelp]),
            el('div', { class: 'fork-row' }, [
                el('label', { text: 'Also detect as separators' }),
                detectInput,
                el('button', {
                    class: 'fork-btn', type: 'button', text: 'Defaults', title: 'Restore the default list',
                    onclick: () => { settings.artists.detect = defaults.artists.detect; detectInput.value = defaults.artists.detect; },
                }),
            ]),
            el('p', { class: 'fork-note', style: 'margin:4px 0 0', text:
                'Separate each with a space. Always detected: , ; and, between spaces, & / + feat. ft. featuring with vs. x. '
                + 'Punctuation you add here (、 ／ × •) splits wherever it appears; a letter or plain symbol (x | +) only between spaces. '
                + 'A name your rules or MusicBrainz know as one artist is never split.' }),
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

        body.replaceChildren(connection, models, naming, artists, features);
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
                    toast('Translation updated — “Apply to library…” updates files you already have');
                    load();
                } catch (err) { toast(err.message, 'error'); }
            };
            input.addEventListener('keydown', (e) => { if (e.key === 'Enter') commit(); });
            return el('tr', {}, [
                el('td', {}, el('span', { class: 'fork-tag' + (item.user_edited ? ' manual' : ''), text: item.kind + (item.user_edited ? ' · edited' : '') })),
                el('td', { class: 'fork-orig' }, originalLink(item.kind, item.original)),
                el('td', {}, input),
                el('td', { class: 'fork-actions' }, [
                    el('button', { class: 'fork-btn', text: 'Save', onclick: commit }),
                    ' ',
                    el('button', {
                        class: 'fork-btn', text: 'Apply to library…',
                        title: 'Update files already in your library that carry this name to the current translation',
                        onclick: async () => {
                            // an unsaved edit in the box is saved first, so what is applied is what is shown
                            const value = input.value.trim();
                            if (value && value !== item.translated) {
                                try {
                                    await api('/translations', 'PUT', { kind: item.kind, original: item.original, translated: value });
                                    item.translated = value;
                                } catch (err) { toast(err.message, 'error'); return; }
                            }
                            openApply(item.kind, item.original, load);
                        },
                    }),
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
                el('button', {
                    class: 'fork-btn', type: 'button', text: 'Apply all to library…',
                    title: 'Update every file that still carries an older translation of a saved album or title',
                    onclick: () => openApplyAll(state.kind, load),
                }),
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
                el('td', { class: 'fork-orig' }, originalLink('artist', item.original)),
                el('td', {}, input),
                el('td', { class: 'fork-actions' }, [
                    el('button', { class: 'fork-btn', text: 'Save', onclick: commit }),
                    ' ',
                    el('button', {
                        class: 'fork-btn', text: 'Apply to library…',
                        title: 'Update files already in your library that still credit this artist under the old name',
                        onclick: async () => {
                            // an unsaved edit in the box is saved first, so what is applied is what is shown
                            const value = input.value.trim();
                            if (value && value !== item.replacement) {
                                try {
                                    await api('/artist-names', 'PUT', { original: item.original, replacement: value });
                                    item.replacement = value;
                                } catch (err) { toast(err.message, 'error'); return; }
                            }
                            openApply('artist', item.original, load);
                        },
                    }),
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
                el('button', {
                    class: 'fork-btn', type: 'button', text: 'Apply all to library…',
                    title: 'Update every file that still credits an artist under a name one of your rules replaces',
                    onclick: () => openApplyAll('artist', load),
                }),
            ]),
            el('table', { class: 'fork-table' }, [
                el('thead', {}, el('tr', {}, ['Source', 'Original', 'Use instead', ''].map((t) => el('th', { text: t })))),
                tbody,
            ]),
        );
        load();
    }

    // ── pop-ups over the panel: library details, apply-to-library ─────────

    function popup(title, children, wide) {
        const box = el('div', { class: 'fork-pop' + (wide ? ' wide' : '') }, [
            el('div', { class: 'fork-pop-head' }, [
                el('h3', { text: title }),
                el('button', { class: 'fork-x', text: '×', 'aria-label': 'Close', onclick: () => layer.remove() }),
            ]),
        ].concat(children));
        const layer = el('div', { class: 'fork-pop-layer', onmousedown: (e) => { if (e.target === layer) layer.remove(); } }, box);
        layer.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); layer.remove(); } });
        document.body.append(layer);
        return layer;
    }

    /** The original name as a link that shows what the library holds for it. */
    function originalLink(kind, name) {
        return el('a', {
            href: '#', class: 'fork-orig-link', text: name, title: 'Show this in your library',
            onclick: (e) => { e.preventDefault(); openDetails(kind, name); },
        });
    }

    const mmss = (ms) => {
        const s = Math.round((ms || 0) / 1000);
        return s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : '';
    };

    async function openDetails(kind, name) {
        const body = el('div', { class: 'fork-pop-body' }, el('div', { class: 'fork-empty', text: 'Loading…' }));
        popup(name, [body], true);
        let data;
        try {
            data = await api('/details?' + new URLSearchParams({ kind, name }));
        } catch (err) { body.replaceChildren(el('div', { class: 'fork-empty', text: err.message })); return; }

        const facts = [];
        const fact = (label, value) => { if (value) facts.push(el('div', { class: 'fork-fact' }, [el('span', { text: label }), el('strong', { text: String(value) })])); };
        const rec = data.record;
        if (kind === 'artist') {
            fact('Written as', rec && rec.replacement);
            fact('Rule from', rec && rec.source);
            fact('Also known as', (data.also_known_as || []).join(' · '));
            fact('In library as', (data.library_names || []).join(' · '));
        } else {
            fact('Type', kind);
            fact('Translation', rec && rec.translated);
            fact('Written as', rec && rec.display);
            fact('Source', rec && (rec.user_edited ? 'edited by you' : rec.model));
        }
        fact('In library', `${data.albums.length} album${data.albums.length === 1 ? '' : 's'} · ${data.track_count} track${data.track_count === 1 ? '' : 's'}${data.truncated ? '+' : ''}`);

        const albums = data.albums.map((album) => el('div', { class: 'fork-pop-album' }, [
            el('div', { class: 'fork-pop-album-head' }, [
                el('strong', { text: album.album }),
                el('span', { text: [album.artist, album.year].filter(Boolean).join(' · ') }),
            ]),
            album.folder ? el('div', { class: 'fork-pop-path', text: album.folder, title: album.folder }) : null,
            el('table', { class: 'fork-table' }, el('tbody', {}, album.tracks.map((t) => el('tr', {}, [
                el('td', { class: 'fork-pop-num', text: t.number != null ? String(t.number) : '' }),
                el('td', { text: t.title }),
                el('td', { class: 'fork-pop-file', text: t.file, title: t.file }),
                el('td', { class: 'fork-pop-num', text: mmss(t.duration) }),
            ])))),
        ]));
        body.replaceChildren(
            el('div', { class: 'fork-facts' }, facts),
            ...(albums.length ? albums : [el('div', { class: 'fork-empty', text: 'Nothing in your library carries this name yet.' })]),
        );
    }

    const base = (p) => String(p || '').split('/').pop();

    /** "Where" row shared by the two apply dialogs: whole library, or one folder. */
    function folderRow(opts, onChange) {
        const label = el('span', { class: 'fork-where' });
        const reset = el('button', {
            class: 'fork-btn', type: 'button', text: 'Whole library',
            onclick: () => { opts.folder = ''; sync(); onChange(); },
        });
        const sync = () => {
            label.textContent = opts.folder || 'Whole library (everything SoulSync has scanned)';
            label.title = opts.folder || '';
            label.classList.toggle('fork-where-set', !!opts.folder);
            reset.style.display = opts.folder ? '' : 'none';
        };
        sync();
        return el('div', { class: 'fork-row' }, [
            el('label', { text: 'Apply to', style: 'flex:0 0 70px' }),
            label,
            el('button', {
                class: 'fork-btn', type: 'button', text: 'Choose folder…',
                title: 'Only change files inside one folder. Also reaches files SoulSync has not scanned.',
                onclick: () => {
                    if (typeof window.forkOpenFolderBrowser !== 'function') { toast('Folder picker is not available', 'error'); return; }
                    window.forkOpenFolderBrowser(opts.folder, (picked) => { opts.folder = picked; sync(); onChange(); });
                },
            }),
            reset,
        ]);
    }

    const renameCheck = (opts, onChange, artist) => el('label', { class: 'fork-check' }, [
        el('input', { type: 'checkbox', checked: true, onchange: (e) => { opts.rename = e.target.checked; onChange(); } }),
        el('span', {}, [artist ? 'Also rename folders and files that carry the old name' : 'Also rename the files / album folder', el('small', {
            text: artist
                ? 'Moves albums into the artist’s new folder, merging with it if it already exists. Off: only the tags change.'
                : 'Off: only the tag changes and files keep their current names.',
        })]),
    ]);

    // translations and artist rules share the two apply dialogs
    const applyUrl = (kind) => (kind === 'artist' ? '/artist-names/apply' : '/translations/apply');

    function openApply(kind, original, onDone) {
        const opts = { rename: true, folder: '' };
        const body = el('div', { class: 'fork-pop-body' });
        const status = el('span', { class: 'fork-help', style: 'flex:1' });
        const go = el('button', { class: 'fork-btn primary', text: 'Apply', disabled: true });
        const isArtist = kind === 'artist';
        const layer = popup(`${isArtist ? 'Apply artist rule' : 'Apply translation'}: ${original}`, [
            folderRow(opts, () => preview()),
            renameCheck(opts, () => preview(), isArtist),
            body,
            el('div', { class: 'fork-foot', style: 'padding:12px 0 0;border:0' }, [
                status,
                el('button', { class: 'fork-btn', text: 'Cancel', onclick: () => layer.remove() }),
                go,
            ]),
        ], true);

        async function preview() {
            go.disabled = true;
            status.textContent = '';
            body.replaceChildren(el('div', { class: 'fork-empty', text: opts.folder ? 'Checking the folder…' : 'Checking your library…' }));
            let data;
            try {
                data = await api(applyUrl(kind), 'POST', { kind, original, rename: opts.rename, folder: opts.folder, dry_run: true });
            } catch (err) { body.replaceChildren(el('div', { class: 'fork-empty', text: err.message })); return; }
            if (!data.files.length) {
                body.replaceChildren(el('div', { class: 'fork-empty', text:
                    `No files need changing: ${data.checked} checked, all already read “${data.display}” or are not this ${kind}.`
                    + (isArtist && !opts.folder ? ' Tracks where this artist is only a featured artist are found by choosing their folder.' : '')
                    + (data.unreachable ? ` ${data.unreachable} could not be found on disk.` : '')
                    + (opts.folder ? '' : ' If the files are not in SoulSync’s library yet, choose their folder above.') }));
                return;
            }
            const rows = data.files.map((f) => el('tr', {}, [
                el('td', { class: 'fork-pop-file', text: base(f.path), title: f.path }),
                el('td', {}, [el('div', { class: 'fork-old', text: f.old }), el('div', { text: f.new })]),
                el('td', { class: 'fork-pop-file', text: f.rename_to ? `→ ${f.rename_to}` : '' }),
            ]));
            const folders = data.folders.map((f) => el('div', { class: 'fork-pop-path', title: `${f.from} → ${f.to}`, text: `Folder: ${base(f.from)}  →  ${base(f.to)}` }));
            body.replaceChildren(
                ...folders,
                el('table', { class: 'fork-table' }, [
                    el('thead', {}, el('tr', {}, ['File', isArtist ? 'Artist tag' : kind === 'album' ? 'Album tag' : 'Title tag',
                        opts.rename && isArtist ? 'New location' : opts.rename && kind === 'title' ? 'New file name' : ''].map((t) => el('th', { text: t })))),
                    el('tbody', {}, rows),
                ]),
            );
            status.textContent = `${data.files.length} file${data.files.length === 1 ? '' : 's'} will be updated`
                + (data.folders.length ? `, ${data.folders.length} folder${data.folders.length === 1 ? '' : 's'} renamed` : '');
            go.disabled = false;
        }

        go.addEventListener('click', async () => {
            go.disabled = true;
            status.textContent = 'Applying…';
            try {
                const done = await api(applyUrl(kind), 'POST', { kind, original, rename: opts.rename, folder: opts.folder });
                if (done.errors.length) {
                    toast(`Updated ${done.written} file(s); ${done.errors.length} problem(s)`, 'error');
                    status.textContent = done.errors.slice(0, 3).join(' · ');
                    return;
                }
                toast(`Updated ${done.written} file${done.written === 1 ? '' : 's'}${done.renamed ? `, ${done.renamed} renamed` : ''}. Rescan your media server to see it.`);
                layer.remove();
                if (onDone) onDone();
            } catch (err) { status.textContent = err.message; go.disabled = false; }
        });
        preview();
    }

    /** Apply every stored translation. Runs in the background on the server;
     *  this dialog previews first, then applies, polling for progress. */
    function openApplyAll(kind, onDone) {
        const opts = { rename: true, folder: '' };
        const isArtist = kind === 'artist';
        const scope = kind === 'album' ? 'album' : kind === 'title' ? 'title' : '';
        const body = el('div', { class: 'fork-pop-body' });
        const status = el('span', { class: 'fork-help', style: 'flex:1' });
        const previewBtn = el('button', { class: 'fork-btn', text: 'Preview' });
        const go = el('button', { class: 'fork-btn primary', text: 'Apply all', disabled: true });
        let closed = false;
        let previewed = false;
        const layer = popup(isArtist ? 'Apply all artist rules to the library' : `Apply all ${scope ? scope + ' ' : ''}translations to the library`, [
            folderRow(opts, () => invalidate()),
            renameCheck(opts, () => invalidate(), isArtist),
            body,
            el('div', { class: 'fork-foot', style: 'padding:12px 0 0;border:0' }, [
                status,
                el('button', { class: 'fork-btn', text: 'Close', onclick: () => { closed = true; layer.remove(); } }),
                previewBtn, go,
            ]),
        ], true);
        const intro = () => body.replaceChildren(el('div', { class: 'fork-empty', text:
            (isArtist
                ? 'Every rule is compared with the files that credit that artist, and files that still show the old name are updated. '
                : 'Every saved translation is compared with the files that carry that album or title, and files that still show an older translation are updated. ')
            + 'Run Preview to see what would change. On a large library this can take a few minutes.' }));
        function invalidate() { previewed = false; go.disabled = true; status.textContent = ''; intro(); }
        intro();

        async function run(dryRun) {
            previewBtn.disabled = true; go.disabled = true;
            status.textContent = dryRun ? 'Checking…' : 'Applying…';
            let job;
            try {
                const started = (await api(isArtist ? '/artist-names/apply-all' : '/translations/apply-all', 'POST', { kind: scope, rename: opts.rename, folder: opts.folder, dry_run: dryRun })).job;
                // Poll until THIS run finishes. The URL is unique per poll and the
                // job id is checked: the app de-duplicates identical GETs, which
                // otherwise hands back the previous run's (preview) result.
                for (;;) {
                    await new Promise((resolve) => setTimeout(resolve, 700));
                    if (closed) return null;
                    job = (await api(`/translations/apply-all?run=${started.id}&t=${Date.now()}`)).job;
                    if (job.id !== started.id) continue;
                    if (!job.running) break;
                    status.textContent = `${dryRun ? 'Checking' : 'Applying'}… ${job.done} / ${job.total || '?'} names`;
                }
            } catch (err) { status.textContent = err.message; previewBtn.disabled = false; return null; }
            previewBtn.disabled = false;
            if (job.error) { status.textContent = job.error; return null; }
            return job.result;
        }

        function show(result, dryRun) {
            const verb = dryRun ? 'would be updated' : 'updated';
            const rows = result.samples.map((s) => el('tr', {}, [
                el('td', {}, el('span', { class: 'fork-tag', text: s.kind })),
                el('td', { class: 'fork-pop-file', text: s.file, title: s.file }),
                el('td', {}, [el('div', { class: 'fork-old', text: s.old }), el('div', { text: s.new })]),
            ]));
            const more = result.files - result.samples.length;
            body.replaceChildren(
                el('div', { class: 'fork-facts' }, [
                    ['Names checked', result.total], ['Names with changes', result.names_changed],
                    [`Files ${verb}`, dryRun ? result.files : result.written],
                    [dryRun ? (isArtist ? 'Files to move' : 'Folders to rename') : (isArtist ? 'Moved' : 'Renamed'), dryRun ? result.folders : result.renamed],
                ].map(([label, value]) => el('div', { class: 'fork-fact' }, [el('span', { text: label }), el('strong', { text: String(value) })]))),
                rows.length ? el('table', { class: 'fork-table' }, [
                    el('thead', {}, el('tr', {}, ['', 'File', 'Tag'].map((t) => el('th', { text: t })))),
                    el('tbody', {}, rows),
                ]) : el('div', { class: 'fork-empty', text: isArtist ? 'Everything already matches your artist rules.' : 'Everything already matches the saved translations.' }),
                more > 0 ? el('div', { class: 'fork-help', text: `…and ${more} more file${more === 1 ? '' : 's'}.` }) : null,
                result.errors.length ? el('div', { class: 'fork-help', style: 'color:#ff8f8f', text: result.errors.slice(0, 5).join(' · ') }) : null,
            );
        }

        previewBtn.addEventListener('click', async () => {
            const result = await run(true);
            if (!result) return;
            show(result, true);
            previewed = true;
            status.textContent = result.files ? `${result.files} file${result.files === 1 ? '' : 's'} would be updated` : 'Nothing to change';
            go.disabled = !result.files;
        });
        go.addEventListener('click', async () => {
            if (!previewed) return;
            const result = await run(false);
            if (!result) return;
            show(result, false);
            previewed = false;
            status.textContent = result.errors.length
                ? `Done with ${result.errors.length} problem${result.errors.length === 1 ? '' : 's'}`
                : 'Done. Rescan your media server to see the changes.';
            toast(`Updated ${result.written} file${result.written === 1 ? '' : 's'}${result.renamed ? `, ${result.renamed} renamed` : ''}`, result.errors.length ? 'error' : 'success');
            if (onDone) onDone();
        });
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
            if (data.defaults) defaults = data.defaults;
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
