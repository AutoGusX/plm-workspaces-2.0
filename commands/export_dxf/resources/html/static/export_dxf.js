/*
 * Export DXF to PLM — palette logic
 * bridge.js must be loaded before this script.
 */
(function () {
    'use strict';

    // ── Helpers ──────────────────────────────────────────────────────────

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

    var exportMode          = 'flatPattern';  // 'flatPattern' | 'sketch'
    var fileMode            = 'individual';   // 'individual' | 'single'
    var compMode            = 'auto';         // 'auto' | 'manual'
    var revisionFilter      = 2;
    var lineageUrns         = [];
    var componentNames      = [];
    var allFlatPatterns     = [];
    var allSketches         = [];
    // Mode-isolated selection — flat patterns and sketches never share state
    var selectedFlatPatterns = {};  // entityToken → flat pattern info
    var selectedSketches     = {};  // entityToken → sketch info
    var selectedTargets      = {};  // itemId → component info
    var existingAtts         = [];  // DXF attachments from PLM (for mapping)
    var exportedFiles        = [];  // files returned from runDxfExport
    var busy                 = false;
    var SUPERSEDED           = ['superseded', 'obsolete', 'cancelled', 'rejected'];

    // Returns the active selection dict for the current export mode
    function activeSelection() {
        return exportMode === 'flatPattern' ? selectedFlatPatterns : selectedSketches;
    }

    // ── Upload button gate ────────────────────────────────────────────────

    function updateUploadBtn() {
        var hasItems   = Object.keys(activeSelection()).length > 0;
        var hasTargets = Object.keys(selectedTargets).length > 0;
        $('btnUpload').disabled = busy || !(hasItems && hasTargets);
    }

    // ── Step progress ─────────────────────────────────────────────────────

    function setStepState(step, state) {
        var dot = $(step === 'export' ? 'dotExport' : 'dotUpload');
        var lbl = $(step === 'export' ? 'lblExport' : 'lblUpload');
        var num = step === 'export' ? '1' : '2';
        dot.className  = 'step-dot' + (state !== 'wait' ? ' ' + state : '');
        dot.textContent = state === 'done' ? '✓' : state === 'error' ? '✗' : num;
        lbl.className  = 'step-label' + (state !== 'wait' ? ' ' + state : '');
        if (step === 'export' && state === 'done')
            $('connExport').className = 'step-connector done';
    }
    function showProgress(msg, type) {
        $('stepProgress').style.display = '';
        showBanner($('progressMsg'), msg, type || 'info');
    }
    function hideProgress() { $('stepProgress').style.display = 'none'; }

    // ── Export mode toggle ────────────────────────────────────────────────

    function setExportMode(m) {
        exportMode = m;
        $('btnModeFlatPattern').classList.toggle('active', m === 'flatPattern');
        $('btnModeSketch').classList.toggle('active', m === 'sketch');
        $('flatPatternSection').style.display = m === 'flatPattern' ? '' : 'none';
        $('sketchSection').style.display      = m === 'sketch'      ? '' : 'none';
        // Each mode has its own selection — no cross-contamination
        refreshMappingCard();
        updateUploadBtn();
    }

    // ── File mode ─────────────────────────────────────────────────────────

    function setFileMode(m) {
        fileMode = m;
        $('singleNameRow').style.display = m === 'single' ? '' : 'none';
        refreshMappingCard();
    }

    // ── Revision filter ───────────────────────────────────────────────────

    function setRevision(rev) {
        revisionFilter = rev;
        document.querySelectorAll('.rev-btn').forEach(function (btn) {
            btn.classList.toggle('active', parseInt(btn.getAttribute('data-rev')) === rev);
        });
        if (compMode === 'auto' && (lineageUrns.length || componentNames.length)) {
            loadAutoComponents();
        } else if (compMode === 'manual') {
            var q = ($('compSearchInput').value || '').trim();
            if (q) runCompSearch(q);
        }
    }

    // ── Component mode toggle ─────────────────────────────────────────────

    function setCompMode(m) {
        compMode = m;
        $('btnCompAuto').classList.toggle('active', m === 'auto');
        $('btnCompManual').classList.toggle('active', m === 'manual');
        $('compAutoSection').style.display   = m === 'auto'   ? '' : 'none';
        $('compManualSection').style.display = m === 'manual' ? '' : 'none';
    }

    // ── Render item list (flat patterns / sketches) ───────────────────────

    function renderItemList(listEl, msgEl, items, checkAllId, allArray) {
        listEl.innerHTML = '';
        if (!items || !items.length) {
            showBanner(msgEl,
                exportMode === 'flatPattern'
                    ? 'No flat patterns found in this design.'
                    : 'No visible sketches found in this design.',
                'info');
            return;
        }
        msgEl.style.display = 'none';
        items.forEach(function (item) {
            var row = document.createElement('label');
            row.className = 'item-row';

            var cb = document.createElement('input');
            cb.type = 'checkbox';
            var sel = activeSelection();
            cb.checked = !!sel[item.id];
            (function (it) {
                cb.onchange = function () {
                    var s = activeSelection();
                    if (cb.checked) s[it.id] = it;
                    else delete s[it.id];
                    refreshMappingCard();
                    updateUploadBtn();
                };
            })(item);

            var name = document.createElement('span');
            name.className = 'item-name';
            name.textContent = item.name;

            var meta = document.createElement('span');
            meta.className = 'item-meta';
            if (item.componentName) meta.textContent = item.componentName;
            if (item.profileCount !== undefined)
                meta.textContent += (meta.textContent ? ' · ' : '') + item.profileCount + ' profiles';

            row.appendChild(cb);
            row.appendChild(name);
            row.appendChild(meta);
            listEl.appendChild(row);
        });
    }

    // ── Render component tiles (same pattern as g-code export) ───────────

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
                    if (cb.checked) selectedTargets[comp.itemId] = comp;
                    else delete selectedTargets[comp.itemId];
                    updateCompHint();
                    updateUploadBtn();
                    refreshMappingCard();
                };
            })(c);
            row.appendChild(cb);
            if (c.thumbnail) {
                var img = document.createElement('img');
                img.className = 'comp-thumb'; img.src = c.thumbnail; img.alt = '';
                img.onerror = function () { this.style.display = 'none'; };
                row.appendChild(img);
            }
            var info = document.createElement('div'); info.className = 'comp-info';
            var title = document.createElement('div'); title.className = 'comp-title';
            title.textContent = c.title || 'Unknown';
            var meta = document.createElement('div'); meta.className = 'comp-meta';
            var ver = document.createElement('span'); ver.className = 'comp-version';
            ver.textContent = (c.version || '') + (c.locked ? ' 🔒' : '');
            meta.appendChild(ver);
            var lcLower = (c.lifecycle || '').toLowerCase();
            var isSup = SUPERSEDED.indexOf(lcLower) !== -1;
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

    function updateCompHint() {
        var count = Object.keys(selectedTargets).length;
        if (count === 0) {
            $('compSelectionHint').style.display = 'none';
        } else {
            $('compSelCount').textContent  = count;
            $('compSelPlural').textContent = count !== 1 ? 's' : '';
            $('compSelectionHint').style.display = '';
        }
    }

    // ── Attachment mapping card ───────────────────────────────────────────

    function refreshMappingCard() {
        var hasItems   = Object.keys(activeSelection()).length > 0;
        var hasTargets = Object.keys(selectedTargets).length > 0;
        if (!hasItems || !hasTargets) {
            $('mappingCard').style.display = 'none';
            return;
        }
        $('mappingCard').style.display = '';
        buildMappingTable();
    }

    function buildMappingTable() {
        var items = Object.keys(activeSelection()).map(function (k) { return activeSelection()[k]; });
        var tbody = $('mappingBody');
        tbody.innerHTML = '';
        $('mappingMsg').style.display = 'none';
        $('mappingTable').style.display = '';

        // Determine file names that will be produced
        var fileNames = getExpectedFileNames(items);

        fileNames.forEach(function (fname, idx) {
            var tr = document.createElement('tr');

            // Col 1: DXF file name
            var td1 = document.createElement('td');
            td1.textContent = fname;
            td1.style.fontFamily = 'monospace';
            td1.style.fontSize   = '10px';
            tr.appendChild(td1);

            // Col 2: Action dropdown (new | overwrite existing)
            var td2 = document.createElement('td');
            var sel = document.createElement('select');
            sel.className = 'mapping-select';
            sel.id        = 'mapAction_' + idx;
            var optNew = document.createElement('option');
            optNew.value = 'new'; optNew.textContent = '+ Create new';
            sel.appendChild(optNew);
            existingAtts.forEach(function (att) {
                var opt = document.createElement('option');
                opt.value = 'overwrite:' + att.id;
                opt.textContent = 'Overwrite: ' + att.name;
                sel.appendChild(opt);
            });
            sel.onchange = function () { updateNameField(idx); };
            td2.appendChild(sel);
            tr.appendChild(td2);

            // Col 3: Name input (for new file) or existing name (for overwrite)
            var td3 = document.createElement('td');
            var inp = document.createElement('input');
            inp.type = 'text';
            inp.className = 'mapping-name-input';
            inp.id        = 'mapName_' + idx;
            inp.value     = fname;
            inp.placeholder = 'Filename (with .dxf)';
            td3.appendChild(inp);
            tr.appendChild(td3);

            tbody.appendChild(tr);
        });
    }

    function getActiveItems() {
        var sel = activeSelection();
        return Object.keys(sel).map(function (k) { return sel[k]; });
    }

    function getExpectedFileNames(items) {
        if (fileMode === 'single') {
            var sn = ($('singleFileName').value || '').trim() || 'export';
            return [sn + '.dxf'];
        }
        return items.map(function (it) {
            return it.name.replace(/[^\w\-]/g, '_') + '_fusion_dxf_export.dxf';
        });
    }

    function updateNameField(idx) {
        var sel = $('mapAction_' + idx);
        var inp = $('mapName_' + idx);
        if (!sel || !inp) return;
        if (sel.value === 'new') {
            inp.disabled = false;
        } else {
            // Show the existing attachment name
            var attId   = sel.value.replace('overwrite:', '');
            var att     = existingAtts.filter(function (a) { return a.id === attId; })[0];
            inp.value   = att ? att.name : '';
            inp.disabled = true;
        }
    }

    function collectMapping(fileNames) {
        return fileNames.map(function (fname, idx) {
            var sel    = $('mapAction_' + idx);
            var inp    = $('mapName_'   + idx);
            var action = sel ? sel.value : 'new';
            var existingId = '';
            var newName    = fname;
            if (action.indexOf('overwrite:') === 0) {
                existingId = action.replace('overwrite:', '');
                action     = 'overwrite';
                newName    = fname;
            } else {
                newName = (inp && inp.value.trim()) || fname;
                if (!newName.toLowerCase().endsWith('.dxf')) newName += '.dxf';
            }
            return { localName: fname, action: action, existingId: existingId, newName: newName };
        });
    }

    // ── Fetch PLM attachments for mapping ─────────────────────────────────

    function fetchAttachmentsForMapping() {
        var targetIds = Object.keys(selectedTargets);
        if (!targetIds.length) return;
        // Use the first selected target to get existing DXF attachments
        var comp = selectedTargets[targetIds[0]];
        send('listItemAttachments', { workspaceId: comp.workspaceId, itemId: comp.itemId })
            .then(function (r) {
                var d = parse(r);
                if (d.success) {
                    existingAtts = d.attachments || [];
                    buildMappingTable();
                }
            })
            .catch(function () {});
    }

    // ── Auto component detection ──────────────────────────────────────────

    function loadAutoComponents() {
        var msgEl  = $('compAutoMsg');
        var listEl = $('compAutoResults');
        showBanner(msgEl, 'Searching PLM…', 'info');
        listEl.style.display = 'none';
        listEl.innerHTML = '';
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

    // ── Manual component search ───────────────────────────────────────────

    function runCompSearch(query) {
        if (!query) { showBanner($('compManualMsg'), 'Enter a search term.', 'warn'); return; }
        var msgEl  = $('compManualMsg');
        var listEl = $('compManualResults');
        var btn    = $('btnCompSearch');
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
            .catch(function (e) {
                btn.disabled = false;
                showBanner(msgEl, 'Error: ' + String(e), 'error');
            });
    }

    // ── Sketch search filter ──────────────────────────────────────────────

    function filterSketches(query) {
        var q = (query || '').toLowerCase().trim();
        var filtered = q
            ? allSketches.filter(function (s) { return s.name.toLowerCase().indexOf(q) !== -1; })
            : allSketches;
        renderItemList($('sketchList'), $('sketchMsg'), filtered, 'checkAllSk', allSketches);
    }

    // ── Context load ──────────────────────────────────────────────────────

    function loadContext() {
        showBanner($('initBanner'), 'Loading design context…', 'info');
        send('getDxfContext', {}).then(function (r) {
            var d = parse(r);
            if (!d.success) {
                showBanner($('initBanner'), d.error || 'Failed to load design context.', 'error');
                return;
            }

            allFlatPatterns = d.flatPatterns || [];
            allSketches     = d.sketches     || [];
            lineageUrns     = d.lineageUrns  || [];
            componentNames  = d.componentNames || [];

            $('initBanner').style.display = 'none';
            $('mainContent').style.display = 'flex';

            // Pre-select all flat patterns (into their own dict)
            allFlatPatterns.forEach(function (fp) { selectedFlatPatterns[fp.id] = fp; });
            renderItemList($('fpList'), $('fpMsg'), allFlatPatterns, 'checkAllFp', allFlatPatterns);

            // Pre-select all sketches (into their own dict)
            allSketches.forEach(function (sk) { selectedSketches[sk.id] = sk; });
            renderItemList($('sketchList'), $('sketchMsg'), allSketches, 'checkAllSk', allSketches);

            updateUploadBtn();

            // Auto-detect component
            if (lineageUrns.length || componentNames.length) {
                loadAutoComponents();
            } else {
                showBanner($('compAutoMsg'),
                    'No component identifiers found. Use Manual search.', 'warn');
            }

        }).catch(function (e) {
            showBanner($('initBanner'), 'Error: ' + String(e), 'error');
        });
    }

    // ── Upload (export + upload) ──────────────────────────────────────────

    function onUpload() {
        if (busy) return;

        var items   = getActiveItems();
        var targets = Object.keys(selectedTargets).map(function (k) { return selectedTargets[k]; });

        if (!items.length) {
            showBanner(
                exportMode === 'flatPattern' ? $('fpMsg') : $('sketchMsg'),
                'Select at least one item to export.', 'warn');
            return;
        }
        if (!targets.length) {
            showBanner($('compAutoMsg').style.display !== 'none' ? $('compAutoMsg') : $('compManualMsg'),
                'Select at least one component revision.', 'warn');
            return;
        }

        var scale      = parseFloat(document.getElementById('scaleSelect' + (exportMode === 'sketch' ? 'Sk' : '')).value) || 1.0;
        var grainDir   = (document.querySelector('input[name="grain"]:checked') || {}).value || 'none';
        var singleName = ($('singleFileName').value || '').trim() || 'export';
        var fileNames  = getExpectedFileNames(items);
        var mapping    = collectMapping(fileNames);
        var comment    = ($('uploadComment').value || '').trim();

        busy = true;
        $('uploadComment').disabled = true;
        updateUploadBtn();
        $('resultsCard').style.display = 'none';
        $('resultsBody').innerHTML = '';
        $('newRunRow').style.display = 'none';

        setStepState('export', 'active');
        setStepState('upload', 'wait');
        $('connExport').className = 'step-connector';
        showProgress('Exporting DXF…', 'info');

        // ── Step 1: Export DXF ──
        send('runDxfExport', {
            mode:           exportMode,
            itemIds:        items.map(function (it) { return it.id; }),
            scale:          scale,
            grainDirection: grainDir,
            fileMode:       fileMode,
            singleName:     singleName,
        }).then(function (r) {
            var d = parse(r);
            if (!d.success) {
                setStepState('export', 'error');
                showProgress(d.error || 'DXF export failed.', 'error');
                $('uploadComment').disabled = false;
                busy = false; updateUploadBtn();
                return;
            }

            exportedFiles = d.files || [];
            setStepState('export', 'done');
            setStepState('upload', 'active');
            showProgress('Uploading to PLM…', 'info');

            // ── Step 2: Upload ──
            send('uploadDxf', {
                files:   exportedFiles,
                targets: targets,
                mapping: mapping,
                comment: comment,
            }).then(function (r2) {
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

            }).catch(function (e) {
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

    // ── Render upload results ─────────────────────────────────────────────

    function renderUploadResults(targetResults) {
        var body = $('resultsBody');
        body.innerHTML = '';
        targetResults.forEach(function (tr) {
            var group  = document.createElement('div');
            group.className = 'upload-target-group';

            var header = document.createElement('div');
            header.className = 'upload-target-header';

            var label = document.createElement('span');
            label.className = 'upload-target-label';
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

    // ── Reset ─────────────────────────────────────────────────────────────

    function onNewRun() {
        selectedFlatPatterns = {};
        selectedSketches     = {};
        selectedTargets      = {};
        exportedFiles        = [];
        existingAtts         = [];
        $('uploadComment').value = '';
        $('resultsCard').style.display = 'none';
        $('resultsBody').innerHTML = '';
        hideProgress();
        $('newRunRow').style.display = 'none';
        $('mappingCard').style.display = 'none';
        updateCompHint();
        // Re-select all items for each mode
        allFlatPatterns.forEach(function (fp) { selectedFlatPatterns[fp.id] = fp; });
        allSketches.forEach(function (sk) { selectedSketches[sk.id] = sk; });
        document.querySelectorAll('.comp-list input[type="checkbox"]').forEach(function (cb) {
            cb.checked = false;
        });
        // Sync checkboxes in both item lists
        document.querySelectorAll('#fpList input, #sketchList input').forEach(function (cb) {
            cb.checked = true;
        });
        updateUploadBtn();
    }

    // ── Event wiring ──────────────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', function () {

        // Mode toggles
        $('btnModeFlatPattern').onclick = function () { setExportMode('flatPattern'); };
        $('btnModeSketch').onclick      = function () { setExportMode('sketch'); };

        // File mode radios
        document.querySelectorAll('input[name="fileMode"]').forEach(function (r) {
            r.onchange = function () { if (r.checked) setFileMode(r.value); };
        });
        $('singleFileName').oninput = function () { buildMappingTable(); };

        // Select all flat patterns
        $('checkAllFp').onclick = function (e) {
            if (e.target.checked) {
                allFlatPatterns.forEach(function (fp) { selectedFlatPatterns[fp.id] = fp; });
            } else {
                selectedFlatPatterns = {};
            }
            document.querySelectorAll('#fpList input[type="checkbox"]').forEach(function (cb) {
                cb.checked = e.target.checked;
            });
            refreshMappingCard(); updateUploadBtn();
        };

        // Select all sketches
        $('checkAllSk').onclick = function (e) {
            if (e.target.checked) {
                allSketches.forEach(function (sk) { selectedSketches[sk.id] = sk; });
            } else {
                selectedSketches = {};
            }
            document.querySelectorAll('#sketchList input[type="checkbox"]').forEach(function (cb) {
                cb.checked = e.target.checked;
            });
            refreshMappingCard(); updateUploadBtn();
        };

        // Sketch search filter
        $('sketchSearch').oninput = function () {
            filterSketches($('sketchSearch').value);
        };

        // Revision filter
        $('revFilter').addEventListener('click', function (e) {
            var btn = e.target.closest && e.target.closest('.rev-btn');
            if (!btn) return;
            setRevision(parseInt(btn.getAttribute('data-rev')));
        });

        // Component mode
        $('btnCompAuto').onclick   = function () {
            setCompMode('auto');
            if (lineageUrns.length || componentNames.length) loadAutoComponents();
        };
        $('btnCompManual').onclick = function () { setCompMode('manual'); };

        // Manual component search
        $('btnCompSearch').onclick = function () {
            runCompSearch(($('compSearchInput').value || '').trim());
        };
        $('compSearchInput').onkeydown = function (e) {
            if (e.key === 'Enter') runCompSearch(($('compSearchInput').value || '').trim());
        };

        // Mapping refresh
        $('btnRefreshMapping').onclick = function () { fetchAttachmentsForMapping(); };

        // Upload / reset
        $('btnUpload').onclick = onUpload;
        $('btnNewRun').onclick = onNewRun;

        loadContext();
    });

})();
