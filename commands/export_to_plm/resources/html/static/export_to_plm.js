/*
 * Export to PLM — unified STEP + PDF match table logic.
 *
 * Phase 1: getExportContext (sync) → searchPlmMatches (async) → populate table
 * Phase 2: user confirms → runStepExports (sync) → runPdfExports (sync) → uploadFiles (async)
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

    var rootInfo     = null;    // root assembly info
    var components   = [];      // [{entityToken, name, parentPath, ...}]
    var drawings     = [];      // [{fileId, name, linkedUrn}]
    var matchResults  = {};     // entityToken → PLM match (or null)
    var drawingResults = {};    // fileId → PLM match (or null)
    var rowState     = {};      // key → {includeStep, includePdf, status, error}
    var revisionFilter = 1;     // 1=latest released, 2=all, 3=working
    var busy         = false;
    var searchDone   = false;

    // ── Helpers ────────────────────────────────────────────────────────────

    function rowKey(item) {
        return item.entityToken || item.fileId || '';
    }

    function hasAnySelected() {
        // Assembly card: root STEP or any drawing PDF
        if (rootInfo) {
            var rrs = rowState[rootInfo.entityToken];
            if (rrs && rrs.includeStep && matchResults[rootInfo.entityToken]) return true;
        }
        var drawingOk = drawings.some(function (drw) {
            var rs = rowState[drw.fileId];
            return rs && rs.includePdf && drawingResults[drw.fileId];
        });
        if (drawingOk) return true;
        // Component card: any component STEP
        return components.some(function (c) {
            var rs = rowState[c.entityToken];
            return rs && rs.includeStep && matchResults[c.entityToken];
        });
    }

    function updateExportBtn() {
        $('btnExport').disabled = busy || !searchDone || !hasAnySelected();
    }

    // ── Step progress ──────────────────────────────────────────────────────

    function setStep(step, state) {
        var ids = { search: 'dotSearch', export: 'dotExport', upload: 'dotUpload' };
        var lbls = { search: 'lblSearch', export: 'lblExport', upload: 'lblUpload' };
        var dot = $(ids[step]); var lbl = $(lbls[step]);
        var nums = { search: '1', export: '2', upload: '3' };
        if (!dot) return;
        dot.className   = 'step-dot' + (state !== 'wait' ? ' ' + state : '');
        dot.textContent = state === 'done' ? '✓' : state === 'error' ? '✗' : nums[step];
        lbl.className   = 'step-label' + (state !== 'wait' ? ' ' + state : '');
        if (step === 'search' && state === 'done') $('conn1').className = 'step-connector done';
        if (step === 'export' && state === 'done') $('conn2').className = 'step-connector done';
    }
    function showProgress(msg, type) {
        $('stepProgress').style.display = '';
        showBanner($('progressMsg'), msg, type || 'info');
    }

    // ── Inline match override ──────────────────────────────────────────

    var openSearchKey    = null;   // which row's panel is currently open
    var searchDebounce   = null;

    function openSearchPanel(key) {
        // close any existing panel first
        closeSearchPanel();
        openSearchKey = key;

        var mainRow = $('row_' + key);
        if (!mainRow) return;

        var panelRow = document.createElement('tr');
        panelRow.id        = 'searchPanel_' + key;
        panelRow.className = 'search-panel-row';

        var td = document.createElement('td');
        td.setAttribute('colspan', '6');

        var panel = document.createElement('div');
        panel.className = 'match-search-panel';

        var input = document.createElement('input');
        input.type        = 'search';
        input.className   = 'match-search-input';
        input.id          = 'matchSearchInput_' + key;
        input.placeholder = 'Search PLM by name or item number…';
        input.autocomplete = 'off';

        var candidates = document.createElement('div');
        candidates.className = 'match-candidates';
        candidates.id        = 'matchCandidates_' + key;

        // Debounced search on input
        input.oninput = function () {
            clearTimeout(searchDebounce);
            var q = input.value.trim();
            if (!q) { candidates.innerHTML = ''; return; }
            candidates.innerHTML = '<div class="match-searching">Searching…</div>';
            searchDebounce = setTimeout(function () { doManualSearch(key, q); }, 350);
        };
        input.onkeydown = function (e) {
            if (e.key === 'Escape') closeSearchPanel();
        };

        // Clear/no-match option
        var clearOpt = document.createElement('div');
        clearOpt.className   = 'match-clear';
        clearOpt.textContent = '✕  No match — skip this component';
        clearOpt.onclick     = function () { clearMatch(key); };

        panel.appendChild(input);
        panel.appendChild(candidates);
        panel.appendChild(clearOpt);
        td.appendChild(panel);
        panelRow.appendChild(td);

        // Insert after the main row
        mainRow.parentNode.insertBefore(panelRow, mainRow.nextSibling);
        setTimeout(function () { input.focus(); }, 50);
    }

    function closeSearchPanel() {
        if (!openSearchKey) return;
        var row = $('searchPanel_' + openSearchKey);
        if (row) row.parentNode.removeChild(row);
        openSearchKey = null;
        clearTimeout(searchDebounce);
    }

    function doManualSearch(key, query) {
        send('searchManualMatch', { query: query, revision: revisionFilter === 1 ? 2 : revisionFilter })
            .then(function (r) {
                if (openSearchKey !== key) return;   // panel was closed
                var d = parse(r);
                var cDiv = $('matchCandidates_' + key);
                if (!cDiv) return;
                cDiv.innerHTML = '';

                if (!d.success) {
                    cDiv.innerHTML = '<div class="match-no-results">Search failed.</div>';
                    return;
                }
                var items = d.candidates || [];
                if (!items.length) {
                    cDiv.innerHTML = '<div class="match-no-results">No results found.</div>';
                    return;
                }
                items.forEach(function (item) {
                    var row = document.createElement('div');
                    row.className = 'match-candidate';

                    var title = document.createElement('div');
                    title.className   = 'match-candidate-title';
                    title.textContent = item.title || 'Unknown';

                    var meta = document.createElement('div');
                    meta.className = 'match-candidate-meta';
                    var parts = [item.version];
                    if (item.lifecycle) parts.push(item.lifecycle);
                    if (item.latestRelease) parts.push('Latest');
                    if (item.workingVersion) parts.push('Working');
                    meta.textContent = parts.filter(Boolean).join('  ·  ');

                    row.appendChild(title);
                    row.appendChild(meta);
                    (function (candidate) {
                        row.onclick = function () { selectCandidate(key, candidate); };
                    })(item);
                    cDiv.appendChild(row);
                });
            })
            .catch(function () {
                var cDiv = $('matchCandidates_' + key);
                if (cDiv) cDiv.innerHTML = '<div class="match-no-results">Search error.</div>';
            });
    }

    function selectCandidate(key, candidate) {
        // Update the match for this row
        var isDrawing = drawingResults.hasOwnProperty(key);
        if (isDrawing) {
            drawingResults[key] = candidate;
        } else {
            matchResults[key] = candidate;
        }
        // Re-enable the checkbox (previously cleared if no match)
        var stepCb = $('stepCb_' + key);
        var pdfCb  = $('pdfCb_'  + key);
        if (stepCb && !isDrawing) { stepCb.checked = true; if (rowState[key]) rowState[key].includeStep = true; }
        if (pdfCb  &&  isDrawing) { pdfCb.checked  = true; if (rowState[key]) rowState[key].includePdf  = true; }
        // Refresh the PLM cell and last-export cell
        updateRowAfterSearch(key);
        closeSearchPanel();
        updateExportBtn();
    }

    function clearMatch(key) {
        var isDrawing = drawingResults.hasOwnProperty(key);
        if (isDrawing) drawingResults[key] = null;
        else           matchResults[key]   = null;
        updateRowAfterSearch(key);
        closeSearchPanel();
        updateExportBtn();
    }

    // ── Row status update ──────────────────────────────────────────────────

    function setRowStatus(key, status, label) {
        if (rowState[key]) rowState[key].status = status;
        var cell = $('status_' + key);
        if (!cell) return;
        cell.className   = 'status-cell ' + status;
        cell.textContent = label || status;
    }

    // ── Revision filter ────────────────────────────────────────────────────

    function setRevision(rev) {
        revisionFilter = rev;
        document.querySelectorAll('.rev-btn').forEach(function (btn) {
            btn.classList.toggle('active', parseInt(btn.getAttribute('data-rev')) === rev);
        });
        if (searchDone) rerunSearch();
    }

    function rerunSearch() {
        searchDone = false;
        $('matchStatus').textContent = 'Re-searching…';
        updateExportBtn();
        runSearch();
    }

    // ── Match table rendering — two separate cards ────────────────────────

    function renderTable() {
        renderAssemblyCard();
        renderComponentCard();
    }

    function renderAssemblyCard() {
        var tbody = $('asmBody');
        tbody.innerHTML = '';
        var hasRows = false;

        if (rootInfo) {
            appendRow(tbody, rootInfo.entityToken, {
                isRoot: true, name: rootInfo.name, parentPath: '',
                instanceCount: 1, isAssembly: true, isExternal: rootInfo.isExternal,
                hasPdf: false, defaultStep: true,
            });
            hasRows = true;
        }
        drawings.forEach(function (drw) {
            appendRow(tbody, drw.fileId, {
                isRoot: false, name: drw.name, parentPath: '',
                instanceCount: 1, isAssembly: false, isExternal: false,
                hasPdf: true, isDrawing: true, defaultStep: false,
            });
            hasRows = true;
        });

        $('asmTable').style.display = hasRows ? '' : 'none';
        $('asmMsg').style.display   = hasRows ? 'none' : '';
        if (!hasRows) showBanner($('asmMsg'), 'No design or drawings open.', 'info');
    }

    function renderComponentCard() {
        var tbody = $('compBody');
        tbody.innerHTML = '';

        if (!components.length) { $('compCard').style.display = 'none'; return; }
        $('compCard').style.display = '';

        components.forEach(function (comp) {
            appendRow(tbody, comp.entityToken, {
                isRoot: false, name: comp.name, parentPath: comp.parentPath,
                instanceCount: comp.instanceCount, isAssembly: comp.isAssembly,
                isExternal: comp.isExternal, hasPdf: false,
                defaultStep: false,   // opt-in — unchecked by default
            });
        });

        $('compTable').style.display = '';
        $('compMsg').style.display   = 'none';
        updateCompUI();
    }

    // ── Component card live helpers ────────────────────────────────────────

    function updateCompUI() {
        var checked = 0;
        var hasAnyExported = false;
        components.forEach(function (c) {
            var rs = rowState[c.entityToken];
            if (rs && rs.includeStep) checked++;
            var m = matchResults[c.entityToken];
            if (m && m.lastStep) hasAnyExported = true;
        });

        // Live time estimate badge (assumes ~5 sec per component export)
        var badge = $('compTimeBadge');
        if (checked > 0) {
            var secs  = checked * 5;
            var label = secs < 60 ? secs + ' sec' : '~' + Math.ceil(secs / 60) + ' min';
            badge.textContent = '⏱ ' + label;
            badge.style.display = '';
        } else {
            badge.style.display = 'none';
        }

        // Warning strip visible only when something is checked
        $('compWarnStrip').style.display = checked > 0 ? '' : 'none';

        // "Skip Exported" button only useful when some rows have lastStep data
        $('btnSkipExported').style.display = hasAnyExported ? '' : 'none';
    }

    function skipExported() {
        components.forEach(function (c) {
            var m  = matchResults[c.entityToken];
            if (m && m.lastStep && rowState[c.entityToken]) {
                rowState[c.entityToken].includeStep = false;
                var cb = $('stepCb_' + c.entityToken);
                if (cb) cb.checked = false;
            }
        });
        updateCompUI();
        updateExportBtn();
    }

    function appendRow(tbody, key, info) {
        // Init row state using info.defaultStep rather than a blanket !isDrawing
        if (!rowState[key]) {
            rowState[key] = {
                includeStep: !!info.defaultStep,
                includePdf:  !!info.isDrawing,
                status: 'searching',
            };
        }
        var rs = rowState[key];
        var match = matchResults[key] || drawingResults[key] || null;

        var tr = document.createElement('tr');
        tr.id = 'row_' + key;
        if (info.isRoot)    tr.className = 'row-root';
        if (info.isDrawing) tr.className = 'row-drawing';

        // Col 1: STEP checkbox
        var td1 = document.createElement('td');
        td1.style.textAlign = 'center';
        if (!info.isDrawing) {
            var cbStep = document.createElement('input');
            cbStep.type = 'checkbox';
            cbStep.className = 'include-step-cb';
            cbStep.id        = 'stepCb_' + key;
            cbStep.checked   = rs.includeStep;
            (function (k, isComp) {
                cbStep.onchange = function () {
                    rowState[k].includeStep = cbStep.checked;
                    if (isComp) updateCompUI();
                    updateExportBtn();
                };
            })(key, !info.isRoot && !info.isDrawing);
            td1.appendChild(cbStep);
        }
        tr.appendChild(td1);

        // Col 2: PDF checkbox
        var td2 = document.createElement('td');
        td2.style.textAlign  = 'center';
        td2.style.borderLeft = '1px solid var(--color-border,#e0e0e0)';
        if (info.isDrawing) {
            var cbPdf = document.createElement('input');
            cbPdf.type = 'checkbox';
            cbPdf.className = 'include-pdf-cb';
            cbPdf.id        = 'pdfCb_' + key;
            cbPdf.checked   = rs.includePdf;
            (function (k) {
                cbPdf.onchange = function () {
                    rowState[k].includePdf = cbPdf.checked;
                    updateExportBtn();
                };
            })(key);
            td2.appendChild(cbPdf);
        }
        tr.appendChild(td2);

        // Col 3: Component / Drawing name
        var td3 = document.createElement('td');
        var nameCell = document.createElement('div'); nameCell.className = 'comp-name-cell';
        var nameLine = document.createElement('span'); nameLine.className = 'comp-name-main';
        nameLine.textContent = info.name + (info.instanceCount > 1 ? '' : '');
        nameCell.appendChild(nameLine);
        if (info.parentPath) {
            var pathLine = document.createElement('span'); pathLine.className = 'comp-name-path';
            pathLine.textContent = info.parentPath;
            nameCell.appendChild(pathLine);
        }
        var metaLine = document.createElement('div'); metaLine.className = 'comp-name-meta';
        if (info.isRoot) {
            var b = document.createElement('span'); b.className = 'badge-asm'; b.textContent = 'Assembly';
            metaLine.appendChild(b);
        } else if (info.isAssembly) {
            var b = document.createElement('span'); b.className = 'badge-asm'; b.textContent = 'Sub-Asm';
            metaLine.appendChild(b);
        }
        if (info.isDrawing) {
            var b = document.createElement('span'); b.className = 'badge-drw'; b.textContent = 'Drawing';
            metaLine.appendChild(b);
        } else if (info.isExternal) {
            var b = document.createElement('span'); b.className = 'badge-ext'; b.textContent = 'External';
            metaLine.appendChild(b);
        } else if (!info.isRoot) {
            var b = document.createElement('span'); b.className = 'badge-int'; b.textContent = 'Internal';
            metaLine.appendChild(b);
        }
        if (info.instanceCount > 1) {
            var ic = document.createElement('span'); ic.className = 'inst-count';
            ic.textContent = '×' + info.instanceCount;
            metaLine.appendChild(ic);
        }
        nameCell.appendChild(metaLine);
        td3.appendChild(nameCell);
        tr.appendChild(td3);

        // Col 4: PLM Record  +  Change button
        var td4 = document.createElement('td');
        td4.id = 'plm_' + key;

        var plmWrap = document.createElement('div');
        plmWrap.className = 'plm-cell-wrap';
        plmWrap.id        = 'plmWrap_' + key;
        plmWrap.appendChild(makePlmCell(match));

        var changeBtn = document.createElement('button');
        changeBtn.type      = 'button';
        changeBtn.className = 'btn-change-match';
        changeBtn.textContent = '⟳';
        changeBtn.title       = 'Change PLM match';
        (function (k) {
            changeBtn.onclick = function (e) {
                e.stopPropagation();
                if (openSearchKey === k) { closeSearchPanel(); }
                else                     { openSearchPanel(k); }
            };
        })(key);
        plmWrap.appendChild(changeBtn);

        td4.appendChild(plmWrap);
        tr.appendChild(td4);

        // Col 5: Last Export
        var td5 = document.createElement('td');
        td5.id = 'last_' + key;
        td5.appendChild(makeLastExportCell(match));
        tr.appendChild(td5);

        // Col 6: Status
        var td6 = document.createElement('td');
        var statusSpan = document.createElement('span');
        statusSpan.id        = 'status_' + key;
        statusSpan.className = 'status-cell searching';
        statusSpan.textContent = 'Searching…';
        td6.appendChild(statusSpan);
        tr.appendChild(td6);

        tbody.appendChild(tr);
    }

    function makePlmCell(match) {
        var div = document.createElement('div');
        if (!match) {
            div.className   = 'no-match';
            div.textContent = 'No match';
            return div;
        }
        div.className = 'plm-match-cell';
        var title = document.createElement('div'); title.className = 'plm-match-title';
        title.textContent = match.title || 'Unknown';
        div.appendChild(title);
        var meta = document.createElement('div'); meta.className = 'plm-match-meta';
        meta.textContent = (match.version || '') + (match.lifecycle ? '  ·  ' + match.lifecycle : '');
        div.appendChild(meta);
        return div;
    }

    function makeLastExportCell(match) {
        var span = document.createElement('span');
        var lastStep = match && match.lastStep;
        if (!lastStep) {
            span.className   = 'last-exp never';
            span.textContent = '—';
        } else {
            span.className = 'last-exp';
            var d = lastStep.date ? new Date(lastStep.date) : null;
            span.textContent = d && !isNaN(d) ? d.toLocaleDateString() : '?';
            span.title = lastStep.name || '';
        }
        return span;
    }

    function updateRowAfterSearch(key) {
        var match      = matchResults[key] || drawingResults[key] || null;
        var plmWrap    = $('plmWrap_'  + key);
        var lastCell   = $('last_'     + key);
        var statusCell = $('status_'   + key);

        // Refresh the match content inside the wrap (preserves the Change button)
        if (plmWrap) {
            // Replace everything except the last child (the Change button)
            var changeBtn = plmWrap.lastChild;
            plmWrap.innerHTML = '';
            plmWrap.appendChild(makePlmCell(match));
            if (changeBtn) plmWrap.appendChild(changeBtn);
        }

        if (lastCell) {
            lastCell.innerHTML = '';
            lastCell.appendChild(makeLastExportCell(match));
        }

        if (statusCell) {
            if (match) {
                statusCell.className   = 'status-cell pending';
                statusCell.textContent = 'Ready';
            } else {
                statusCell.className   = 'status-cell skipped';
                statusCell.textContent = 'No match';
                var stepCb = $('stepCb_' + key);
                var pdfCb  = $('pdfCb_'  + key);
                if (stepCb) { stepCb.checked = false; if (rowState[key]) rowState[key].includeStep = false; }
                if (pdfCb)  { pdfCb.checked  = false; if (rowState[key]) rowState[key].includePdf  = false; }
            }
        }
    }

    // ── Phase 1: search ────────────────────────────────────────────────────

    function runSearch() {
        var compList = (rootInfo ? [rootInfo] : []).concat(components);
        send('searchPlmMatches', {
            components: compList,
            drawings:   drawings,
            revision:   revisionFilter,
        }).then(function (r) {
            var d = parse(r);
            if (!d.success) {
                showBanner($('tableMsg'), d.error || 'PLM search failed.', 'error');
                return;
            }
            matchResults  = d.matchResults  || {};
            drawingResults = d.drawingResults || {};

            // Update every row
            if (rootInfo) updateRowAfterSearch(rootInfo.entityToken);
            components.forEach(function (c) { updateRowAfterSearch(c.entityToken); });
            drawings.forEach(function (drw)  { updateRowAfterSearch(drw.fileId);   });

            var matched = Object.values(matchResults).filter(Boolean).length
                        + Object.values(drawingResults).filter(Boolean).length;
            var total   = (rootInfo ? 1 : 0) + components.length + drawings.length;
            $('matchStatus').textContent = matched + ' of ' + total + ' matched';

            searchDone = true;
            setStep('search', 'done');
            updateCompUI();   // refresh time badge + Skip Exported button now that lastStep data is available
            updateExportBtn();
        }).catch(function (e) {
            $('matchStatus').textContent = 'Search failed';
            showBanner($('tableMsg'), 'PLM search error: ' + String(e), 'error');
        });
    }

    // ── Phase 2: export + upload ────────────────────────────────────────────

    function onExport() {
        if (busy) return;

        // Collect selected STEP items
        var stepItems = [];
        if (rootInfo && rowState[rootInfo.entityToken] && rowState[rootInfo.entityToken].includeStep
                && matchResults[rootInfo.entityToken]) {
            stepItems.push({ entityToken: rootInfo.entityToken, name: rootInfo.name, isRoot: true });
        }
        components.forEach(function (c) {
            var rs = rowState[c.entityToken];
            if (rs && rs.includeStep && matchResults[c.entityToken]) {
                stepItems.push({ entityToken: c.entityToken, name: c.name, isRoot: false });
            }
        });

        // Collect selected PDF drawings
        var pdfFileIds = [];
        drawings.forEach(function (drw) {
            var rs = rowState[drw.fileId];
            if (rs && rs.includePdf && drawingResults[drw.fileId]) {
                pdfFileIds.push(drw.fileId);
            }
        });

        if (!stepItems.length && !pdfFileIds.length) {
            showBanner($('progressMsg'), 'No matched items selected.', 'warn');
            $('stepProgress').style.display = '';
            return;
        }

        var comment = ($('uploadComment').value || '').trim();
        busy = true;
        $('uploadComment').disabled = true;
        updateExportBtn();
        $('resultsCard').style.display = 'none';
        $('resultsBody').innerHTML = '';
        $('newRunRow').style.display = 'none';
        $('conn1').className = 'step-connector';
        $('conn2').className = 'step-connector';
        setStep('search', 'done');
        setStep('export', 'active');
        setStep('upload', 'wait');
        showProgress('Exporting files…', 'info');

        // Update row statuses
        stepItems.forEach(function (it) { setRowStatus(it.entityToken, 'exporting', 'Exporting…'); });
        pdfFileIds.forEach(function (fid) { setRowStatus(fid, 'exporting', 'Exporting…'); });

        var exportedStepFiles = [];
        var exportedPdfFiles  = [];

        // ── Step exports ──
        var stepPromise = stepItems.length
            ? send('runStepExports', { exportItems: stepItems })
                .then(function (r) {
                    var d = parse(r);
                    if (!d.success) throw new Error(d.error || 'STEP export failed.');
                    exportedStepFiles = d.files || [];
                    stepItems.forEach(function (it) { setRowStatus(it.entityToken, 'uploading', 'Queued…'); });
                    if (d.probeLog && d.probeLog.length) {
                        console.log('[ExportToPlm] STEP probe log:', d.probeLog.join('\n'));
                    }
                })
            : Promise.resolve();

        // ── PDF exports (after STEP, still in sync window) ──
        var allExportPromise = stepPromise.then(function () {
            return pdfFileIds.length
                ? send('runPdfExports', { drawingFileIds: pdfFileIds })
                    .then(function (r2) {
                        var d2 = parse(r2);
                        if (!d2.success) throw new Error(d2.error || 'PDF export failed.');
                        exportedPdfFiles = d2.files || [];
                        pdfFileIds.forEach(function (fid) { setRowStatus(fid, 'uploading', 'Queued…'); });
                    })
                : Promise.resolve();
        });

        allExportPromise.then(function () {
            setStep('export', 'done');
            setStep('upload', 'active');
            showProgress('Uploading to PLM…', 'info');

            // Build upload batches: one per unique PLM item
            var uploadsMap = {};   // workspaceId:itemId → {targetTitle, targetVersion, ws, item, files:[]}

            function addFile(entityToken, fileObj, fileType) {
                var match = matchResults[entityToken];
                if (!match) return;
                var mapKey = match.workspaceId + ':' + match.itemId;
                if (!uploadsMap[mapKey]) {
                    uploadsMap[mapKey] = {
                        workspaceId:   match.workspaceId,
                        itemId:        match.itemId,
                        targetTitle:   match.title,
                        targetVersion: match.version,
                        files:         [],
                    };
                }
                uploadsMap[mapKey].files.push(Object.assign({}, fileObj, { fileType: fileType }));
            }

            // Map STEP files to PLM items
            exportedStepFiles.forEach(function (f) {
                var token = f.entityToken;
                addFile(token, f, 'step');
            });

            // Map PDF files to PLM items (via drawing match)
            exportedPdfFiles.forEach(function (f) {
                var fid   = f.fileId;
                var match = drawingResults[fid];
                if (!match) return;
                var mapKey = match.workspaceId + ':' + match.itemId;
                if (!uploadsMap[mapKey]) {
                    uploadsMap[mapKey] = {
                        workspaceId:   match.workspaceId,
                        itemId:        match.itemId,
                        targetTitle:   match.title,
                        targetVersion: match.version,
                        files:         [],
                    };
                }
                uploadsMap[mapKey].files.push(Object.assign({}, f, { fileType: 'pdf' }));
                setRowStatus(fid, 'uploading', 'Uploading…');
            });

            var uploads = Object.values(uploadsMap);
            if (!uploads.length) {
                setStep('upload', 'error');
                showProgress('Nothing to upload — no matched files.', 'warn');
                busy = false; $('uploadComment').disabled = false; updateExportBtn();
                return;
            }

            send('uploadFiles', { uploads: uploads, comment: comment })
                .then(function (r3) {
                    var d3 = parse(r3);
                    var ok = d3.totalSuccessCount || 0;
                    if (ok > 0) {
                        setStep('upload', 'done');
                        showProgress(ok + ' file' + (ok !== 1 ? 's' : '') + ' uploaded.', 'success');
                    } else {
                        setStep('upload', 'error');
                        showProgress(d3.error || 'Upload failed.', 'error');
                    }

                    // Update row statuses from results
                    (d3.targetResults || []).forEach(function (tr) {
                        // Find which tokens map to this PLM item
                        var mapKey = tr.workspaceId + ':' + tr.itemId;
                        stepItems.forEach(function (it) {
                            var m = matchResults[it.entityToken];
                            if (m && m.workspaceId + ':' + m.itemId === mapKey) {
                                var ok2 = (tr.fileResults || []).some(function (fr) { return fr.success; });
                                setRowStatus(it.entityToken, ok2 ? 'done' : 'error',
                                    ok2 ? 'Uploaded ✓' : 'Error');
                            }
                        });
                        drawings.forEach(function (drw) {
                            var m = drawingResults[drw.fileId];
                            if (m && m.workspaceId + ':' + m.itemId === mapKey) {
                                var ok2 = (tr.fileResults || []).some(function (fr) { return fr.success; });
                                setRowStatus(drw.fileId, ok2 ? 'done' : 'error',
                                    ok2 ? 'Uploaded ✓' : 'Error');
                            }
                        });
                    });

                    renderUploadResults(d3.targetResults || []);
                    $('resultsCard').style.display = '';
                    $('newRunRow').style.display   = '';
                    busy = false; $('uploadComment').disabled = false; updateExportBtn();
                })
                .catch(function (e) {
                    setStep('upload', 'error');
                    showProgress('Upload error: ' + String(e), 'error');
                    busy = false; $('uploadComment').disabled = false; updateExportBtn();
                });

        }).catch(function (e) {
            setStep('export', 'error');
            showProgress('Export error: ' + String(e), 'error');
            busy = false; $('uploadComment').disabled = false; updateExportBtn();
        });
    }

    // ── Upload results renderer ────────────────────────────────────────────

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
        rowState = {};
        matchResults = {}; drawingResults = {};
        searchDone = false;
        $('uploadComment').value = '';
        $('resultsCard').style.display = 'none';
        $('resultsBody').innerHTML = '';
        $('newRunRow').style.display = 'none';
        $('stepProgress').style.display = 'none';
        $('matchStatus').textContent = 'Searching PLM…';
        renderTable();
        setStep('search', 'active');
        setStep('export', 'wait');
        setStep('upload', 'wait');
        $('stepProgress').style.display = '';
        runSearch();
    }

    // ── Context load ───────────────────────────────────────────────────────

    function loadContext() {
        showBanner($('initBanner'), 'Scanning design…', 'info');
        send('getExportContext', {}).then(function (r) {
            var d = parse(r);
            if (!d.success) {
                showBanner($('initBanner'), d.error || 'Failed to load design context.', 'error');
                return;
            }
            rootInfo   = d.rootInfo   || null;
            components = d.components || [];
            drawings   = d.drawings   || [];

            $('initBanner').style.display  = 'none';
            $('mainContent').style.display = 'flex';

            renderTable();

            // Start progress
            setStep('search', 'active');
            setStep('export', 'wait');
            setStep('upload', 'wait');
            $('stepProgress').style.display = '';
            showProgress('Searching PLM for ' + ((rootInfo ? 1 : 0) + components.length + drawings.length) + ' items…', 'info');

            runSearch();

        }).catch(function (e) {
            showBanner($('initBanner'), 'Error: ' + String(e), 'error');
        });
    }

    // ── Event wiring ───────────────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', function () {

        // Close search panel on outside click
        document.addEventListener('click', function (e) {
            if (!openSearchKey) return;
            var panel = $('searchPanel_' + openSearchKey);
            var changeBtn = document.querySelector('#row_' + openSearchKey + ' .btn-change-match');
            if (panel && !panel.contains(e.target) && e.target !== changeBtn) {
                closeSearchPanel();
            }
        });

        // Assembly card has no select-all (only 1-2 rows, always meaningful)

        // Component card: select/deselect all component STEPs
        $('checkAllComp').onclick = function (e) {
            document.querySelectorAll('#compBody .include-step-cb').forEach(function (cb) {
                cb.checked = e.target.checked;
                var key = cb.id.replace('stepCb_', '');
                if (rowState[key]) rowState[key].includeStep = e.target.checked;
            });
            updateCompUI();
            updateExportBtn();
        };

        // Skip previously exported component rows
        $('btnSkipExported').onclick = skipExported;

        $('revFilter').addEventListener('click', function (e) {
            var btn = e.target.closest && e.target.closest('.rev-btn');
            if (!btn) return;
            setRevision(parseInt(btn.getAttribute('data-rev')));
        });

        $('btnExport').onclick = onExport;
        $('btnNewRun').onclick = onNewRun;

        loadContext();
    });

})();
