/*
 * Export Drawing PDF to PLM — palette logic
 * Behaves like G-code export: fixed resource name → auto version-bump.
 */
(function () {
    'use strict';

    function send(action, data) { return plmSend(action, data || {}); }
    function parse(raw) {
        if (raw && typeof raw === 'object') return raw;
        try { return JSON.parse(raw); }
        catch (e) { return { success: false, error: 'Unexpected response.' }; }
    }
    function $(id) { return document.getElementById(id); }
    function showBanner(el, msg, type) {
        if (!el) return;
        el.textContent = msg || '';
        el.style.display = msg ? '' : 'none';
        el.className = 'eg-banner' + (type ? ' ' + type : ' info');
    }

    // ── State ─────────────────────────────────────────────────────────────

    var allDrawings     = [];          // [{name, fileId}]
    var selectedDrawing = {};          // fileId → drawing info
    var selectedTargets = {};          // itemId → component info
    var revisionFilter  = 2;
    var mode            = 'auto';
    var lineageUrns     = [];
    var componentNames  = [];
    var busy            = false;
    var SUPERSEDED      = ['superseded', 'obsolete', 'cancelled', 'rejected'];

    // ── Upload button gate ────────────────────────────────────────────────

    function updateUploadBtn() {
        var hasDrawings = Object.keys(selectedDrawing).length > 0;
        var hasTargets  = Object.keys(selectedTargets).length > 0;
        $('btnUpload').disabled = busy || !(hasDrawings && hasTargets);
    }

    // ── Step progress ─────────────────────────────────────────────────────

    function setStepState(step, state) {
        var dot = $(step === 'export' ? 'dotExport' : 'dotUpload');
        var lbl = $(step === 'export' ? 'lblExport' : 'lblUpload');
        var num = step === 'export' ? '1' : '2';
        dot.className   = 'step-dot' + (state !== 'wait' ? ' ' + state : '');
        dot.textContent = state === 'done' ? '✓' : state === 'error' ? '✗' : num;
        lbl.className   = 'step-label' + (state !== 'wait' ? ' ' + state : '');
        if (step === 'export' && state === 'done')
            $('connExport').className = 'step-connector done';
    }
    function showProgress(msg, type) {
        $('stepProgress').style.display = '';
        showBanner($('progressMsg'), msg, type || 'info');
    }
    function hideProgress() { $('stepProgress').style.display = 'none'; }

    // ── Selection hint ─────────────────────────────────────────────────────

    function updateSelectionHint() {
        var count = Object.keys(selectedTargets).length;
        if (count === 0) {
            $('selectionHint').style.display = 'none';
        } else {
            $('selectionCount').textContent  = count;
            $('selectionPlural').textContent = count !== 1 ? 's' : '';
            $('selectionHint').style.display = '';
        }
    }

    // ── Mode toggle ────────────────────────────────────────────────────────

    function setMode(m) {
        mode = m;
        $('btnModeAuto').classList.toggle('active',   m === 'auto');
        $('btnModeManual').classList.toggle('active', m === 'manual');
        $('autoSection').style.display   = m === 'auto'   ? '' : 'none';
        $('manualSection').style.display = m === 'manual' ? '' : 'none';
    }

    // ── Revision filter ────────────────────────────────────────────────────

    function setRevision(rev) {
        revisionFilter = rev;
        document.querySelectorAll('.rev-btn').forEach(function (btn) {
            btn.classList.toggle('active', parseInt(btn.getAttribute('data-rev')) === rev);
        });
        if (mode === 'auto' && (lineageUrns.length || componentNames.length)) {
            loadAutoRevisions();
        } else if (mode === 'manual') {
            var q = ($('searchInput').value || '').trim();
            if (q) runSearch(q);
        }
    }

    // ── Drawing list renderer ──────────────────────────────────────────────

    function renderDrawings(drawings) {
        var list = $('drawingList');
        list.innerHTML = '';
        if (!drawings.length) {
            showBanner($('drawingMsg'),
                'No drawing documents are open. Open a Fusion 360 drawing first.', 'warn');
            return;
        }
        $('drawingMsg').style.display = 'none';
        drawings.forEach(function (d) {
            var row = document.createElement('label');
            row.className = 'drawing-row';
            var cb = document.createElement('input');
            cb.type    = 'checkbox';
            cb.checked = !!selectedDrawing[d.fileId];
            (function (drawing) {
                cb.onchange = function () {
                    if (cb.checked) selectedDrawing[drawing.fileId] = drawing;
                    else delete selectedDrawing[drawing.fileId];
                    updateUploadBtn();
                };
            })(d);
            var name = document.createElement('span');
            name.className   = 'drawing-name';
            name.textContent = d.name;
            var badge = document.createElement('span');
            badge.className   = 'drawing-badge';
            badge.textContent = 'Drawing';
            row.appendChild(cb);
            row.appendChild(name);
            row.appendChild(badge);
            list.appendChild(row);
        });
    }

    // ── Component tile renderer ────────────────────────────────────────────

    function renderComponents(listEl, msgEl, components, warning) {
        if (warning) showBanner(msgEl, warning, 'warn');
        else msgEl.style.display = 'none';
        listEl.innerHTML = '';
        if (!components || !components.length) {
            if (!warning) showBanner(msgEl, 'No components found.', 'info');
            listEl.style.display = 'none';
            return;
        }
        listEl.style.display = '';
        components.forEach(function (c) {
            var row = document.createElement('label');
            row.className = 'comp-row';
            var cb = document.createElement('input');
            cb.type    = 'checkbox';
            cb.checked = !!selectedTargets[c.itemId];
            (function (comp) {
                cb.onchange = function () {
                    if (cb.checked) selectedTargets[comp.itemId] = comp;
                    else delete selectedTargets[comp.itemId];
                    updateSelectionHint();
                    updateUploadBtn();
                };
            })(c);
            row.appendChild(cb);
            if (c.thumbnail) {
                var img = document.createElement('img');
                img.className = 'comp-thumb'; img.src = c.thumbnail; img.alt = '';
                img.onerror = function () { this.style.display = 'none'; };
                row.appendChild(img);
            }
            var info  = document.createElement('div'); info.className = 'comp-info';
            var title = document.createElement('div'); title.className = 'comp-title';
            title.textContent = c.title || 'Unknown';
            var meta = document.createElement('div'); meta.className = 'comp-meta';
            var ver  = document.createElement('span'); ver.className = 'comp-version';
            ver.textContent = (c.version || '') + (c.locked ? ' 🔒' : '');
            meta.appendChild(ver);
            var lcLower = (c.lifecycle || '').toLowerCase();
            var isSup   = SUPERSEDED.indexOf(lcLower) !== -1;
            if (c.lifecycle && !isSup) {
                var lc = document.createElement('span'); lc.textContent = c.lifecycle;
                meta.appendChild(lc);
            }
            if (c.latestRelease) {
                var b = document.createElement('span'); b.className = 'badge-latest'; b.textContent = 'Latest';
                meta.appendChild(b);
            } else if (c.workingVersion) {
                var b = document.createElement('span'); b.className = 'badge-working'; b.textContent = 'Working';
                meta.appendChild(b);
            }
            if (isSup) {
                var b = document.createElement('span'); b.className = 'badge-superseded'; b.textContent = c.lifecycle;
                meta.appendChild(b);
            }
            info.appendChild(title); info.appendChild(meta);
            row.appendChild(info);
            listEl.appendChild(row);
        });
    }

    // ── Auto-detect component ──────────────────────────────────────────────

    function loadAutoRevisions() {
        var msgEl  = $('autoMsg');
        var listEl = $('autoResults');
        showBanner(msgEl, 'Searching PLM…', 'info');
        listEl.style.display = 'none'; listEl.innerHTML = '';
        send('loadComponentRevisions', {
            lineageUrns: lineageUrns, componentNames: componentNames,
            revision: revisionFilter,
        }).then(function (r) {
            var d = parse(r);
            if (!d.success) {
                showBanner(msgEl, d.error || 'Auto-detect failed.', 'error');
                return;
            }
            renderComponents(listEl, msgEl, d.components, d.warning);
        }).catch(function (e) {
            showBanner(msgEl, 'Error: ' + String(e), 'error');
        });
    }

    function runSearch(query) {
        if (!query) { showBanner($('manualMsg'), 'Enter a search term.', 'warn'); return; }
        var msgEl = $('manualMsg'); var listEl = $('manualResults'); var btn = $('btnSearch');
        btn.disabled = true;
        showBanner(msgEl, 'Searching…', 'info');
        listEl.style.display = 'none'; listEl.innerHTML = '';
        send('searchFmComponents', { query: query, revision: revisionFilter })
            .then(function (r) {
                btn.disabled = false;
                var d = parse(r);
                if (!d.success) { showBanner(msgEl, d.error || 'Search failed.', 'error'); return; }
                renderComponents(listEl, msgEl, d.components, d.warning);
            })
            .catch(function (e) { btn.disabled = false; showBanner(msgEl, 'Error: ' + String(e), 'error'); });
    }

    // ── Context load ───────────────────────────────────────────────────────

    function loadContext() {
        showBanner($('initBanner'), 'Loading context…', 'info');
        send('getPdfContext', {}).then(function (r) {
            var d = parse(r);
            if (!d.success) {
                showBanner($('initBanner'), d.error || 'Failed to load context.', 'error');
                return;
            }
            allDrawings    = d.drawings       || [];
            lineageUrns    = d.lineageUrns    || [];
            componentNames = d.componentNames || [];

            $('initBanner').style.display = 'none';
            $('mainContent').style.display = 'flex';

            // Pre-select all drawings
            allDrawings.forEach(function (drw) { selectedDrawing[drw.fileId] = drw; });
            renderDrawings(allDrawings);
            updateUploadBtn();

            if (lineageUrns.length || componentNames.length) {
                loadAutoRevisions();
            } else {
                showBanner($('autoMsg'),
                    'No component identifiers found. Use Manual search.', 'warn');
            }
        }).catch(function (e) {
            showBanner($('initBanner'), 'Error: ' + String(e), 'error');
        });
    }

    // ── Upload ─────────────────────────────────────────────────────────────

    function onUpload() {
        if (busy) return;

        var drawings = Object.keys(selectedDrawing).map(function (k) { return selectedDrawing[k]; });
        var targets  = Object.keys(selectedTargets).map(function (k) { return selectedTargets[k]; });

        if (!drawings.length) {
            showBanner($('drawingMsg'), 'Select at least one drawing.', 'warn');
            return;
        }
        if (!targets.length) {
            showBanner(mode === 'auto' ? $('autoMsg') : $('manualMsg'),
                'Select at least one component revision.', 'warn');
            return;
        }

        var comment = ($('uploadComment').value || '').trim();

        busy = true;
        $('uploadComment').disabled = true;
        updateUploadBtn();
        $('resultsCard').style.display = 'none';
        $('resultsBody').innerHTML = '';
        $('newRunRow').style.display = 'none';

        setStepState('export', 'active');
        setStepState('upload', 'wait');
        $('connExport').className = 'step-connector';
        showProgress('Exporting drawing PDF…', 'info');

        // ── Step 1: Export PDF ──
        send('runPdfExport', {
            drawingFileIds: drawings.map(function (d) { return d.fileId; }),
        }).then(function (r) {
            var d = parse(r);
            if (!d.success) {
                setStepState('export', 'error');
                showProgress(d.error || 'PDF export failed.', 'error');
                $('uploadComment').disabled = false;
                busy = false; updateUploadBtn();
                return;
            }

            var files = d.files || [];
            setStepState('export', 'done');
            setStepState('upload', 'active');
            showProgress('Uploading to PLM…', 'info');

            // ── Step 2: Upload ──
            send('uploadPdf', { files: files, targets: targets, comment: comment })
                .then(function (r2) {
                    var d2  = parse(r2);
                    var ok  = d2.totalSuccessCount || 0;
                    var tot = d2.totalCount || 0;
                    if (ok > 0) {
                        setStepState('upload', 'done');
                        var msg = ok + ' file' + (ok !== 1 ? 's' : '') + ' uploaded';
                        if (targets.length > 1) msg += ' to ' + targets.length + ' revisions';
                        showProgress(msg + '.', 'success');
                    } else {
                        setStepState('upload', 'error');
                        showProgress(d2.error || 'Upload failed.', 'error');
                    }
                    renderUploadResults(d2.targetResults || []);
                    $('resultsCard').style.display = '';
                    $('newRunRow').style.display = '';
                    $('uploadComment').disabled = false;
                    busy = false; updateUploadBtn();
                })
                .catch(function (e) {
                    setStepState('upload', 'error');
                    showProgress('Upload error: ' + String(e), 'error');
                    $('uploadComment').disabled = false;
                    busy = false; updateUploadBtn();
                });
        }).catch(function (e) {
            setStepState('export', 'error');
            showProgress('Export error: ' + String(e), 'error');
            $('uploadComment').disabled = false;
            busy = false; updateUploadBtn();
        });
    }

    // ── Results renderer ───────────────────────────────────────────────────

    function renderUploadResults(targetResults) {
        var body = $('resultsBody');
        body.innerHTML = '';
        targetResults.forEach(function (tr) {
            var group  = document.createElement('div'); group.className = 'upload-target-group';
            var header = document.createElement('div'); header.className = 'upload-target-header';
            var label  = document.createElement('span'); label.className = 'upload-target-label';
            label.textContent = (tr.targetVersion ? tr.targetVersion + '  ·  ' : '') + tr.targetTitle;
            header.appendChild(label);
            if (tr.itemUrl && tr.successCount > 0) {
                var btn = document.createElement('button');
                btn.type = 'button'; btn.className = 'btn btn-sm upload-open-btn';
                btn.textContent = '🔗 Open in PLM';
                (function (url) { btn.onclick = function () { send('openInBrowser', { url: url }); }; })(tr.itemUrl);
                header.appendChild(btn);
            }
            group.appendChild(header);
            (tr.fileResults || []).forEach(function (fr) {
                var row = document.createElement('div');
                row.className = 'upload-result-row ' + (fr.success ? 'result-ok' : 'result-fail');
                var detail = fr.success
                    ? 'v' + (fr.version || 1) + (fr.isNewVersion ? ' (updated)' : ' (new)')
                    : (fr.error || 'Failed');
                row.textContent = (fr.success ? '✓ ' : '✗ ') + fr.name + '  —  ' + detail;
                group.appendChild(row);
            });
            body.appendChild(group);
        });
    }

    // ── Reset ──────────────────────────────────────────────────────────────

    function onNewRun() {
        selectedDrawing = {};
        selectedTargets = {};
        $('uploadComment').value = '';
        $('resultsCard').style.display = 'none';
        $('resultsBody').innerHTML = '';
        hideProgress();
        $('newRunRow').style.display = 'none';
        updateSelectionHint();
        // Re-select all drawings
        allDrawings.forEach(function (drw) { selectedDrawing[drw.fileId] = drw; });
        document.querySelectorAll('#drawingList input[type="checkbox"]').forEach(function (cb) {
            cb.checked = true;
        });
        document.querySelectorAll('.comp-list input[type="checkbox"]').forEach(function (cb) {
            cb.checked = false;
        });
        updateUploadBtn();
    }

    // ── Event wiring ───────────────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', function () {

        $('checkAll').onclick = function (e) {
            document.querySelectorAll('#drawingList input[type="checkbox"]').forEach(function (cb) {
                cb.checked = e.target.checked;
                cb.onchange && cb.onchange();
            });
        };

        $('btnModeAuto').onclick   = function () {
            setMode('auto');
            if (lineageUrns.length || componentNames.length) loadAutoRevisions();
        };
        $('btnModeManual').onclick = function () { setMode('manual'); };

        $('revFilter').addEventListener('click', function (e) {
            var btn = e.target.closest && e.target.closest('.rev-btn');
            if (!btn) return;
            setRevision(parseInt(btn.getAttribute('data-rev')));
        });

        $('btnSearch').onclick = function () {
            runSearch(($('searchInput').value || '').trim());
        };
        $('searchInput').onkeydown = function (e) {
            if (e.key === 'Enter') runSearch(($('searchInput').value || '').trim());
        };

        $('btnUpload').onclick = onUpload;
        $('btnNewRun').onclick = onNewRun;

        loadContext();
    });

})();
