/*
 * Export G-code to PLM — single-page logic
 *
 * One "Upload to PLM" button runs post-process then upload sequentially.
 * A 2-step progress bar (Post-Process → Upload) is shown in the footer during the operation.
 *
 * bridge.js must be loaded before this script.
 */
(function () {
    'use strict';

    // ── Helpers ──────────────────────────────────────────────────────────

    function send(action, data) { return plmSend(action, data || {}); }

    function parse(raw) {
        if (raw && typeof raw === 'object') return raw;
        try { return JSON.parse(raw); }
        catch (e) { return { success: false, error: 'Unexpected response format.' }; }
    }

    function $(id) { return document.getElementById(id); }

    function showBanner(el, msg, type) {
        if (!el) return;
        el.textContent = msg || '';
        el.style.display = msg ? '' : 'none';
        el.className = 'eg-banner' + (type ? ' ' + type : ' info');
    }

    // ── State ─────────────────────────────────────────────────────────────

    var lineageUrns     = [];   // lineage URNs from CAM setup component dataFiles
    var componentNames  = [];   // Fusion component names (fallback for internal components)
    var selectedTargets = {};   // keyed by itemId: {workspaceId, itemId, title, version}
    var revisionFilter  = 2;    // 1=latest released, 2=all, 3=working
    var mode            = 'auto';
    var busy            = false;

    // ── Upload-button gate ────────────────────────────────────────────────

    function updateUploadBtn() {
        var hasTargets  = Object.keys(selectedTargets).length > 0;
        var hasProg     = document.querySelectorAll('.nc-cb:checked').length > 0;
        $('btnUpload').disabled = busy || !(hasTargets && hasProg);
    }

    // ── Selection hint ────────────────────────────────────────────────────

    function updateSelectionHint() {
        var count = Object.keys(selectedTargets).length;
        if (count === 0) {
            $('selectionHint').style.display = 'none';
        } else {
            $('selectionCount').textContent = count;
            $('selectionPlural').textContent = count !== 1 ? 's' : '';
            $('selectionHint').style.display = '';
        }
    }

    // ── Step progress ─────────────────────────────────────────────────────
    // states: 'wait' | 'active' | 'done' | 'error'

    var STEP_SYMBOLS = { wait: '', active: '…', done: '✓', error: '✗' };

    function setStepState(step, state) {
        var dot = $(step === 'post' ? 'dotPost' : 'dotUpload');
        var lbl = $(step === 'post' ? 'lblPost' : 'lblUpload');
        var stepNum = step === 'post' ? '1' : '2';

        dot.className = 'step-dot' + (state !== 'wait' ? ' ' + state : '');
        dot.textContent = (state === 'done') ? '✓' : (state === 'error') ? '✗' : stepNum;
        lbl.className = 'step-label' + (state !== 'wait' ? ' ' + state : '');

        if (step === 'post' && state === 'done') {
            $('connPost').className = 'step-connector done';
        }
    }

    function showProgress(msg, type) {
        $('stepProgress').style.display = '';
        showBanner($('progressMsg'), msg, type || 'info');
    }

    function hideProgress() {
        $('stepProgress').style.display = 'none';
    }

    // ── Revision filter buttons ───────────────────────────────────────────

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

    // ── Mode toggle ───────────────────────────────────────────────────────

    function setMode(m) {
        mode = m;
        $('btnModeAuto').classList.toggle('active', m === 'auto');
        $('btnModeManual').classList.toggle('active', m === 'manual');
        $('autoSection').style.display   = (m === 'auto')   ? '' : 'none';
        $('manualSection').style.display = (m === 'manual') ? '' : 'none';
    }

    // ── Component row renderer ────────────────────────────────────────────

    var SUPERSEDED_STATES = ['superseded', 'obsolete', 'cancelled', 'rejected'];

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
            cb.type = 'checkbox';
            cb.checked = !!selectedTargets[c.itemId];
            (function (comp) {
                cb.onchange = function () {
                    if (cb.checked) { selectedTargets[comp.itemId] = comp; }
                    else { delete selectedTargets[comp.itemId]; }
                    updateSelectionHint();
                    updateUploadBtn();
                };
            })(c);
            row.appendChild(cb);

            // Thumbnail
            if (c.thumbnail) {
                var img = document.createElement('img');
                img.className = 'comp-thumb';
                img.src = c.thumbnail;
                img.alt = '';
                img.onerror = function () { this.style.display = 'none'; };
                row.appendChild(img);
            }

            var info = document.createElement('div');
            info.className = 'comp-info';

            var title = document.createElement('div');
            title.className   = 'comp-title';
            title.textContent = c.title || 'Unknown';

            var meta = document.createElement('div');
            meta.className = 'comp-meta';

            // Version + lock icon
            var ver = document.createElement('span');
            ver.className   = 'comp-version';
            ver.textContent = (c.version || '') + (c.locked ? ' 🔒' : '');
            meta.appendChild(ver);

            // Lifecycle — skip for superseded (shown as its own badge below)
            var lcLower = (c.lifecycle || '').toLowerCase();
            var isSuperseded = SUPERSEDED_STATES.indexOf(lcLower) !== -1;
            if (c.lifecycle && !isSuperseded) {
                var lc = document.createElement('span');
                lc.textContent = c.lifecycle;
                meta.appendChild(lc);
            }

            // Latest / Working badge
            if (c.latestRelease) {
                var b = document.createElement('span');
                b.className = 'badge-latest'; b.textContent = 'Latest';
                meta.appendChild(b);
            } else if (c.workingVersion) {
                var b = document.createElement('span');
                b.className = 'badge-working'; b.textContent = 'Working';
                meta.appendChild(b);
            }

            // Superseded / Obsolete badge
            if (isSuperseded) {
                var b = document.createElement('span');
                b.className = 'badge-superseded'; b.textContent = c.lifecycle;
                meta.appendChild(b);
            }

            // Modified indicator on working versions with unreleased changes
            if (c.changedSinceRelease && c.workingVersion) {
                var b = document.createElement('span');
                b.className = 'badge-changed'; b.textContent = 'Modified';
                meta.appendChild(b);
            }

            info.appendChild(title);
            info.appendChild(meta);
            row.appendChild(info);
            listEl.appendChild(row);
        });
    }

    // ── Auto-detect via lineage URN ───────────────────────────────────────

    function loadAutoRevisions() {
        var msgEl  = $('autoMsg');
        var listEl = $('autoResults');
        showBanner(msgEl, 'Searching PLM…', 'info');
        listEl.style.display = 'none';
        listEl.innerHTML = '';

        send('loadComponentRevisions', { lineageUrns: lineageUrns, componentNames: componentNames, revision: revisionFilter })
            .then(function (r) {
                var d = parse(r);
                if (!d.success) {
                    showBanner(msgEl, d.error || 'Auto-detect failed. Try Manual search.', 'error');
                    return;
                }
                renderComponents(listEl, msgEl, d.components, d.warning);
            })
            .catch(function (e) {
                showBanner(msgEl, 'Error: ' + String(e), 'error');
            });
    }

    // ── Manual text search ────────────────────────────────────────────────

    function runSearch(query) {
        if (!query) { showBanner($('manualMsg'), 'Enter a search term.', 'warn'); return; }
        var msgEl  = $('manualMsg');
        var listEl = $('manualResults');
        var btn    = $('btnSearch');

        btn.disabled = true;
        showBanner(msgEl, 'Searching…', 'info');
        listEl.style.display = 'none';
        listEl.innerHTML = '';

        send('searchFmComponents', { query: query, revision: revisionFilter })
            .then(function (r) {
                btn.disabled = false;
                var d = parse(r);
                if (!d.success) {
                    showBanner(msgEl, d.error || 'Search failed.', 'error');
                    return;
                }
                renderComponents(listEl, msgEl, d.components, d.warning);
            })
            .catch(function (e) {
                btn.disabled = false;
                showBanner(msgEl, 'Error: ' + String(e), 'error');
            });
    }

    // ── Context load ──────────────────────────────────────────────────────

    function loadContext() {
        showBanner($('initBanner'), 'Loading CAM context…', 'info');

        send('getGcodeContext', {}).then(function (r) {
            var d = parse(r);
            if (!d.success) {
                showBanner($('initBanner'), d.error || 'Failed to load CAM context.', 'error');
                return;
            }

            lineageUrns = d.lineageUrns || [];
            componentNames = d.componentNames || [];
            $('initBanner').style.display = 'none';

            var mc = $('mainContent');
            mc.style.display = 'flex';

            renderNcPrograms(d.ncPrograms || []);

            if (lineageUrns.length || componentNames.length) {
                loadAutoRevisions();
            } else {
                showBanner($('autoMsg'),
                    'No component identifiers found for this design. Use Manual search to find a PLM component.',
                    'warn');
            }

        }).catch(function (e) {
            showBanner($('initBanner'), 'Error: ' + String(e), 'error');
        });
    }

    // ── NC Programs ───────────────────────────────────────────────────────

    function renderNcPrograms(programs) {
        var list = $('ncProgramList');
        list.innerHTML = '';
        if (!programs.length) {
            list.innerHTML = '<div class="no-items">No NC Programs found.</div>';
            return;
        }
        programs.forEach(function (p) {
            var row = document.createElement('label');
            row.className = 'nc-row';

            var cb = document.createElement('input');
            cb.type = 'checkbox'; cb.value = p.id; cb.checked = true; cb.className = 'nc-cb';
            cb.onchange = updateUploadBtn;

            var name = document.createElement('span');
            name.className = 'nc-name'; name.textContent = p.name;

            row.appendChild(cb);
            row.appendChild(name);

            if (p.postName) {
                var b = document.createElement('span');
                b.className = 'nc-badge'; b.textContent = p.postName;
                row.appendChild(b);
            }
            if (p.operationCount) {
                var b2 = document.createElement('span');
                b2.className = 'nc-badge';
                b2.textContent = p.operationCount + ' op' + (p.operationCount !== 1 ? 's' : '');
                row.appendChild(b2);
            }
            list.appendChild(row);
        });
        updateUploadBtn();
    }

    // ── Upload (post-process + upload) ────────────────────────────────────

    function onUpload() {
        if (busy) return;

        var targets = Object.keys(selectedTargets).map(function (k) { return selectedTargets[k]; });
        var ids = [];
        document.querySelectorAll('.nc-cb:checked').forEach(function (cb) { ids.push(cb.value); });

        if (!ids.length) {
            showBanner($('ncMsg'), 'Select at least one NC Program.', 'warn');
            return;
        }
        if (!targets.length) {
            showBanner($('autoMsg').style.display !== 'none' ? $('autoMsg') : $('manualMsg'),
                'Select at least one component revision.', 'warn');
            return;
        }

        // ── Lock UI ──
        busy = true;
        $('uploadComment').disabled = true;
        updateUploadBtn();
        $('resultsCard').style.display = 'none';
        $('resultsBody').innerHTML = '';
        $('newRunRow').style.display = 'none';

        // ── Show progress ──
        setStepState('post', 'active');
        setStepState('upload', 'wait');
        $('connPost').className = 'step-connector';
        showProgress('Running post-process…', 'info');

        // ── Step 1: Post-process ──
        send('runPostProcess', { ncProgramIds: ids })
            .then(function (r) {
                var d = parse(r);
                if (!d.success) {
                    setStepState('post', 'error');
                    showProgress(d.error || 'Post-process failed.', 'error');
                    $('uploadComment').disabled = false;
                    busy = false; updateUploadBtn();
                    return;
                }

                var files = d.files || [];
                setStepState('post', 'done');
                setStepState('upload', 'active');
                showProgress('Uploading to PLM…', 'info');

                // ── Step 2: Upload ──
                var comment = ($('uploadComment').value || '').trim();
                send('uploadGcode', { files: files, targets: targets, comment: comment })
                    .then(function (r2) {
                        var d2 = parse(r2);
                        var ok    = d2.totalSuccessCount || 0;
                        var total = d2.totalCount || 0;

                        if (ok > 0) {
                            setStepState('upload', 'done');
                            var msg = ok + ' file' + (ok !== 1 ? 's' : '') + ' uploaded';
                            if (targets.length > 1) {
                                msg += ' to ' + targets.length + ' revisions';
                            }
                            showProgress(msg + '.', 'success');
                        } else {
                            setStepState('upload', 'error');
                            showProgress((d2.error || 'Upload failed.'), 'error');
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
            })
            .catch(function (e) {
                setStepState('post', 'error');
                showProgress('Post-process error: ' + String(e), 'error');
                busy = false; updateUploadBtn();
            });
    }

    function renderUploadResults(targetResults) {
        var body = $('resultsBody');
        body.innerHTML = '';
        targetResults.forEach(function (tr) {
            var group = document.createElement('div');
            group.className = 'upload-target-group';

            // Header row: label + per-target "Open in PLM" button
            var header = document.createElement('div');
            header.className = 'upload-target-header';

            var label = document.createElement('span');
            label.className = 'upload-target-label';
            label.textContent = (tr.targetVersion ? tr.targetVersion + '  ·  ' : '') + tr.targetTitle;
            header.appendChild(label);

            if (tr.itemUrl && tr.successCount > 0) {
                var openBtn = document.createElement('button');
                openBtn.type = 'button';
                openBtn.className = 'btn btn-sm upload-open-btn';
                openBtn.textContent = '🔗 Open in PLM';
                (function (url) {
                    openBtn.onclick = function () { send('openInBrowser', { url: url }); };
                })(tr.itemUrl);
                header.appendChild(openBtn);
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

    // ── Reset ─────────────────────────────────────────────────────────────

    function onNewRun() {
        selectedTargets = {};
        $('uploadComment').value = '';
        $('resultsCard').style.display = 'none';
        $('resultsBody').innerHTML = '';
        hideProgress();
        $('newRunRow').style.display = 'none';
        showBanner($('ncMsg'), '', '');
        updateSelectionHint();
        document.querySelectorAll('.comp-list input[type="checkbox"]').forEach(function (cb) {
            cb.checked = false;
        });
        updateUploadBtn();
    }

    // ── Event wiring ──────────────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', function () {

        // NC Programs
        $('checkAll').onclick = function (e) {
            document.querySelectorAll('.nc-cb').forEach(function (cb) {
                cb.checked = e.target.checked;
            });
            updateUploadBtn();
        };

        // Mode toggle
        $('btnModeAuto').onclick = function () {
            setMode('auto');
            if (lineageUrns.length || componentNames.length) loadAutoRevisions();
        };
        $('btnModeManual').onclick = function () { setMode('manual'); };

        // Revision filter
        $('revFilter').addEventListener('click', function (e) {
            var btn = e.target.closest && e.target.closest('.rev-btn');
            if (!btn) return;
            setRevision(parseInt(btn.getAttribute('data-rev')));
        });

        // Manual search
        $('btnSearch').onclick = function () {
            runSearch(($('searchInput').value || '').trim());
        };
        $('searchInput').onkeydown = function (e) {
            if (e.key === 'Enter') runSearch(($('searchInput').value || '').trim());
        };

        // Upload / reset
        $('btnUpload').onclick = onUpload;
        $('btnNewRun').onclick = onNewRun;

        loadContext();
    });

})();
