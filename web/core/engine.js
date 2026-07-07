/**
 * PLM Workspaces 2.0 — engine.js
 *
 * The shared list / detail / form / picker engine for every workspace capability.
 * Ported from lib/html/plmWorkspaceCore.js. Driven by window._plmCfg set by
 * each capability's config.js BEFORE DOMContentLoaded fires.
 *
 * Load order (per ARCHITECTURE §5.1):
 *   tokens.css → components.css → bridge.js → theme.js → config.js → engine.js
 *
 * _plmCfg contract (minimum):
 *   workspaceKey          string   — identifies the workspace on the Python side
 *   title                 string   — human name (shown in header h1)
 *   detailAction          string   — 'getItemDetail' (generic; no per-domain aliases)
 *   showAffectedItems     bool     — show Affected Items tab + list-view count column
 *   viewSelectKey         string?  — localStorage key for selected tableau (default: workspaceKey + '_tableau_id')
 *   newItemLabel          string?  — label for + New button (default: 'New Item')
 *   hooks:
 *     buildCustomFormField(fieldId, def, currentVal) → element|null
 *     renderCustomDetailField(el, val, def)          → bool (true = handled)
 *     onDetailToolbarExtra(toolbar, item, wsId)      → void
 *     afterSave(itemId, wsId)                        → void
 *
 * Action names used:
 *   getWorkspaceAccess, getAuthStatus, getTableaus, getTableauData,
 *   getViewFields, getWorkspaceSections, getLookupOptions, getItemDetail,
 *   createItem, updateItem, getAffectedItems, addAffectedItems,
 *   removeAffectedItem, getItemTabs, getItemPermissions,
 *   getItemsTabCounts, runWorkflowTransition,
 *   searchResultsForLineage, getSelectedComponents,
 *   getRootComponents, getOpenDrawings, openInBrowser, openInFusion
 *
 * Picker markup is an inline template string (_pickerInlineHtml) injected at startup.
 * There is no separate picker.html file; engine.js is the single source of picker markup.
 */
(function () {
    'use strict';

    // -------------------------------------------------------------------------
    // Require bridge.js (plmSend, plmParse, plmCreateAuthPoller)
    // -------------------------------------------------------------------------
    var send  = window.plmSend;
    var parse = window.plmParse;

    // -------------------------------------------------------------------------
    // _plmCfg defaults
    // -------------------------------------------------------------------------
    var cfg = window._plmCfg = window._plmCfg || {};
    cfg.title              = cfg.title              || 'Workspace';
    cfg.detailAction       = cfg.detailAction       || 'getItemDetail';
    cfg.viewSelectKey      = cfg.viewSelectKey      || (cfg.workspaceKey || 'ws') + '_tableau_id';
    cfg.newItemLabel       = cfg.newItemLabel       || 'New Item';
    cfg.showAffectedItems  = !!cfg.showAffectedItems;
    // Legacy alias: old palette.js used showAffectedItemsColumn
    if (cfg.showAffectedItemsColumn) cfg.showAffectedItems = true;
    cfg.hooks = cfg.hooks || {};
    // Scopes (unified multi-workspace): optional; when absent behaviour is unchanged.
    // cfg.scopes            bool   — enable scope selector
    // cfg.scopesAction      string — Python action returning {scopes:[{key,label,...}]}
    // cfg.unifiedAction     string — Python action returning {rows,columns,totalCount,truncated}
    // cfg.unifiedDefaultKey string — key value for "All" scope (default: 'all')
    cfg.scopes            = !!cfg.scopes;
    cfg.scopesAction      = cfg.scopesAction      || '';
    cfg.unifiedAction     = cfg.unifiedAction     || '';
    cfg.unifiedDefaultKey = cfg.unifiedDefaultKey || 'all';

    // -------------------------------------------------------------------------
    // State
    // -------------------------------------------------------------------------
    var tableaus        = [];
    var currentTableauId = null;
    var currentPage     = 1;
    var pageSize        = 50;
    var totalRecords    = 0;
    var tableColumns    = [];   // [{id, typeTitle}]
    var fieldTitleMap   = {};   // {fieldId: title}
    var fieldDefMap     = {};   // {fieldId: full def}
    var tableRows       = [];   // [{itemId, workspaceId, fields}]

    // Scope selector state (only active when cfg.scopes is true)
    var currentScope      = cfg.unifiedDefaultKey;  // 'all' or a workspaceId string
    var scopeList         = [];                     // [{key,label,workspaceId?,systemName?}]
    var unifiedRows       = [];                     // merged rows from getUnifiedChangeRecords
    var unifiedColumns    = [];                     // [{id,label}] from getUnifiedChangeRecords
    var unifiedTruncated  = false;

    // Lazy enrichment cache for the unified table.
    // Keyed "<workspaceId>:<itemId>" → {state, affectedCount}.
    // Persists for the session; cleared on scope change or full unified refresh.
    var _enrichCache      = {};

    // Map of scope key → scope label (populated from getChangeScopes response).
    // Keyed by s.key (= option value / currentScope), used by _applyScopeUI
    // to build "New <SingularLabel>" button text.
    var _scopeLabels      = {};  // {key: label}

    // Stale-render guard for list loads: a monotonic sequence incremented on each
    // loadUnifiedView / loadListView call. The .then() handler checks that the
    // token still matches before rendering, so a slow in-flight response from a
    // previous scope never overwrites the table for the current scope.
    var _listLoadSeq    = 0;

    var detailStack     = [];   // breadcrumb stack
    var currentItemId   = null;
    var currentWorkspaceId = null;
    var currentItem     = null;
    var currentItemETag = '';
    var currentTransitions = [];
    var currentStep     = 0;

    var formMode        = 'create';   // 'create' | 'edit'
    var formSections    = [];
    var formFieldDefs   = {};
    var formValues      = {};
    var _originalFormValues = {};
    var lookupCache     = {};
    var savedFieldElems = {};

    var pickerTargetFieldId   = null;
    var pickerActiveTab       = 'selection';
    var pickerSelectedComponents = [];
    var pickerSelectedFmItems = [];
    var pickerMode            = 'components';  // 'components' | 'drawings'
    var pickerTarget          = 'field';       // 'field' | 'affectedItems'
    var pickerAffectedItemsCtx = null;
    var _pickerOnAdded        = null;

    var _detectedViewId = 1;

    // Detail tab state
    var currentItemTabs   = [];
    var currentPermissions = null;
    var activeDetailTab   = 'details';
    var affectedItemsCache = {};
    var lastTabsDebug     = '';
    var _pendingInitialTab = null;

    var TAB_LABELS = {
        'LINKEDITEMS':         'Affected Items',
        'PART_ATTACHMENTS':    'Attachments',
        'PART_HISTORY':        'History',
        'RELATIONSHIPS':       'Relationships',
        'PART_GRID':           'Managed Items',
        'ACTIONS_NOTIFICATIONS': 'Actions',
        'PROJECT_MANAGEMENT':  'Project'
    };
    var SUPPORTED_TABS = { 'LINKEDITEMS': true };

    // CAD workspace IDs for "Open in Fusion" icon
    var COMPONENTS_WS_ID = '57';
    var DRAWINGS_WS_ID   = '76';

    // -------------------------------------------------------------------------
    // Auth poller (exposed globally so index.html can stop it on tokenResult)
    // -------------------------------------------------------------------------
    var _authPoller = plmCreateAuthPoller(checkAccessAndLoad);
    window._authPoller = _authPoller;

    // -------------------------------------------------------------------------
    // Utility
    // -------------------------------------------------------------------------
    function showView(v) {
        document.body.className = document.body.className.replace(/\bview-\S+/g, '').trim();
        document.body.classList.add('view-' + v);
    }

    function setMsg(elId, text, type) {
        var el = document.getElementById(elId);
        if (!el) return;
        el.textContent = text || '';
        el.className = 'msg-bar' + (text ? ' visible ' + (type || 'info') : '');
    }

    function setLoading(elId, on) {
        var el = document.getElementById(elId);
        if (el) el.style.display = on ? 'block' : 'none';
    }

    function escHtml(s) {
        if (s === null || s === undefined) return '';
        return String(s)
            .replace(/&/g,'&amp;').replace(/</g,'&lt;')
            .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    function decodeHtmlEntities(s) {
        if (!s || typeof s !== 'string') return s;
        var ta = document.createElement('textarea');
        ta.innerHTML = s;
        return ta.value;
    }

    function hasHtmlContent(s) {
        if (!s || typeof s !== 'string') return false;
        // FM returns rich-text/HTML fields in two forms:
        //   - escaped (state pills, flags):  &lt;div ...&gt;  → detect &lt;
        //   - raw (rich-text descriptions):  <p>Added hole</p>, <br>, <b>, <a ...>
        //     → detect any real opening/closing HTML tag (a '<' immediately
        //       followed by a letter or '/'), which plain text like "a < b" won't match.
        if (s.indexOf('&lt;') !== -1) return true;
        return /<\/?[a-z][^>]*>/i.test(s);
    }

    function _fieldIsHidden(def) {
        var v = def && def.visibility;
        return v === false || v === 'NEVER';
    }

    function isCadWorkspace(wsId) {
        return wsId === COMPONENTS_WS_ID || wsId === DRAWINGS_WS_ID;
    }

    function extractWorkspaceIdFromLink(link) {
        var m = (link || '').match(/workspaces\/(\d+)/);
        return m ? m[1] : null;
    }

    function extractItemIdFromLink(link) {
        var m = (link || '').match(/items\/(\d+)/);
        return m ? m[1] : null;
    }

    function hasPerm(name) {
        if (!currentPermissions) return true;
        return !!currentPermissions.has(name);
    }

    // -------------------------------------------------------------------------
    // Picker HTML injection
    // The picker markup lives entirely in _pickerInlineHtml() — the single source.
    // There is no picker.html file and no XHR load; the markup is injected directly
    // into the document body at startup, eliminating all file:// CORS risk.
    // -------------------------------------------------------------------------
    function _injectPickerMarkup(callback) {
        // If already injected, skip.
        if (document.getElementById('pickerOverlay')) {
            if (callback) callback();
            return;
        }
        var container = document.createElement('div');
        container.innerHTML = _pickerInlineHtml();
        while (container.firstChild) {
            document.body.appendChild(container.firstChild);
        }
        _bindPickerEvents();
        if (callback) callback();
    }

    function _pickerInlineHtml() {
        // Inline picker markup — the ONE authoritative source.
        return '<div class="picker-overlay" id="pickerOverlay">'
            + '<div class="picker-panel">'
            + '<div class="picker-header"><h3 id="pickerHeaderTitle">Add Fusion Component</h3>'
            + '<button type="button" class="btn btn-sm" id="btnClosePicker">&times; Close</button></div>'
            + '<div class="picker-tabs">'
            + '<button type="button" class="picker-tab active" data-tab="selection" id="tabSelection">Active Selection</button>'
            + '<button type="button" class="picker-tab" data-tab="browse" id="tabBrowse">Browse Components</button>'
            + '</div>'
            + '<div class="picker-body">'
            + '<div class="picker-step">'
            + '<div class="picker-step-label" id="pickerStep1Label">Step 1: Load components</div>'
            + '<div style="margin-bottom:6px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;">'
            + '<button type="button" class="btn btn-sm" id="btnLoadComponents" style="display:none;">Load</button>'
            + '<select id="revisionFilter" class="view-select" style="width:auto;min-width:0;flex:none;">'
            + '<option value="1">Latest released</option><option value="3">Working</option><option value="2">All revisions</option>'
            + '</select>'
            + '<button type="button" class="btn btn-primary btn-sm" id="btnSearchFM" disabled>Search Fusion Manage</button>'
            + '<span class="picker-msg" id="pickerLoadMsg"></span>'
            + '</div><ul class="picker-list" id="componentList"></ul></div>'
            + '<div class="picker-step" id="pickerStep2" style="display:none;">'
            + '<div class="picker-step-label">Fusion Manage results</div>'
            + '<span class="picker-msg" id="pickerSearchMsg"></span>'
            + '<ul class="picker-list" id="fmResultList"></ul></div>'
            + '</div>'
            + '<div class="picker-footer">'
            + '<button type="button" class="btn btn-primary btn-sm" id="btnAddToShared" disabled>Add Selected</button>'
            + '<button type="button" class="btn btn-sm" id="btnAddAllToShared" disabled>Add All</button>'
            + '<button type="button" class="btn btn-sm" id="btnCancelPicker">Cancel</button>'
            + '</div></div></div>';
    }

    function _bindPickerEvents() {
        var ids = {
            tabSelection:    function () { setPickerTab('selection'); },
            tabBrowse:       function () { setPickerTab('browse'); },
            btnLoadComponents: function () { loadPickerComponents(); },
            btnSearchFM:     function () { searchFusionManageForComponents(pickerSelectedComponents); },
            btnAddToShared:  function () { addSelectedToSharedItems(); },
            btnAddAllToShared: function () { addAllToSharedItems(); },
            btnClosePicker:  function () { closeComponentPicker(); },
            btnCancelPicker: function () { closeComponentPicker(); }
        };
        Object.keys(ids).forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.onclick = ids[id];
        });
    }

    // -------------------------------------------------------------------------
    // Init
    // -------------------------------------------------------------------------
    function plmCoreInit() {
        currentWorkspaceId = cfg.workspaceIdDefault || '';
        // Show the list shell immediately so the palette is never blank while the
        // auth/access round-trips are in flight.
        showView('list');
        setLoading('listLoading', true);
        _authPoller.stop();
        plmRequestTheme();
        // Mount the settings gear now that the headers are in the DOM.
        if (window.plmUiPrefs && typeof window.plmUiPrefs.mountAll === 'function') {
            window.plmUiPrefs.mountAll();
        }
        send('getAuthStatus', {}).then(function (r) {
            var d = parse(r);
            if (!d.hasToken) {
                setLoading('listLoading', false);
                setMsg('listMsg', 'Not signed in. Sign in via the login window — this palette will update automatically.', 'info');
                _authPoller.start();
                return;
            }
            checkAccessAndLoad();
        }).catch(function (e) {
            setLoading('listLoading', false);
            setMsg('listMsg', 'Error checking auth: ' + e, 'error');
        });
    }
    window.plmCoreInit = plmCoreInit;

    function checkAccessAndLoad() {
        send('getWorkspaceAccess', {}).then(function (r) {
            var d = parse(r);
            if (d.workspaceId) {
                cfg.workspaceIdDefault = String(d.workspaceId);
                currentWorkspaceId = cfg.workspaceIdDefault;
            }
            if (!d.hasAccess) {
                showNoAccessView(d.urlGroups || '', d.urlTemplateLibrary || '');
                return;
            }
            if (cfg.scopes && cfg.scopesAction) {
                initScopes();
            } else {
                loadListView();
            }
        }).catch(function (e) {
            setLoading('listLoading', false);
            setMsg('listMsg', 'Error checking workspace access: ' + e, 'error');
        });
    }

    // -------------------------------------------------------------------------
    // SCOPE SELECTOR — unified multi-workspace support
    //
    // When cfg.scopes is true the list toolbar shows a <select id="scopeSelector">
    // BEFORE the existing #viewSelector.  Selecting:
    //   'all'        → unified view (hides #viewSelector + New btn, calls unifiedAction)
    //   <workspaceId> → drilled-down single-workspace view (shows #viewSelector + New)
    //
    // When cfg.scopes is false/absent these functions are never called and behaviour
    // is exactly as before.
    // -------------------------------------------------------------------------

    function initScopes() {
        // Render the scope <select> immediately (with just a placeholder) so the
        // toolbar is stable while the API call is in flight.
        _ensureScopeSelector();
        setLoading('listLoading', true);
        send(cfg.scopesAction, {}).then(function (r) {
            setLoading('listLoading', false);
            var d = parse(r);
            if (!d.success) {
                setMsg('listMsg', d.error || 'Failed to load scopes.', 'error');
                return;
            }
            scopeList = d.scopes || [];
            // Build the scope-key → label map for dynamic New-button labels.
            // Keyed by s.key (= the value stored in currentScope / selector),
            // not s.workspaceId, so _applyScopeUI's lookup always matches.
            _scopeLabels = {};
            scopeList.forEach(function (s) {
                if (s.key) _scopeLabels[s.key] = s.label;
            });
            _populateScopeSelector();
            // Apply the initial / persisted scope.
            var savedScope = localStorage.getItem(_scopeStorageKey());
            if (savedScope && scopeList.some(function (s) { return s.key === savedScope; })) {
                currentScope = savedScope;
            } else {
                currentScope = cfg.unifiedDefaultKey;
            }
            var sel = document.getElementById('scopeSelector');
            if (sel) sel.value = currentScope;
            _applyScopeUI(currentScope);
        }).catch(function (e) {
            setLoading('listLoading', false);
            setMsg('listMsg', 'Error loading scopes: ' + e, 'error');
        });
    }

    function _scopeStorageKey() {
        return (cfg.workspaceKey || 'ws') + '_scope';
    }

    function _ensureScopeSelector() {
        if (document.getElementById('scopeSelector')) return;
        var toolbar = document.querySelector('.list-toolbar');
        if (!toolbar) return;
        var sel = document.createElement('select');
        sel.id = 'scopeSelector';
        sel.className = 'view-select';
        sel.title = 'Select workspace scope';
        var ph = document.createElement('option');
        ph.value = cfg.unifiedDefaultKey;
        ph.textContent = 'All change records';
        sel.appendChild(ph);
        // Insert BEFORE the first existing child (view selector goes after).
        toolbar.insertBefore(sel, toolbar.firstChild);
        sel.onchange = function () {
            currentScope = sel.value;
            localStorage.setItem(_scopeStorageKey(), currentScope);
            // Clear enrichment cache on every scope change so stale pills
            // from a previous scope are never shown in a new context.
            _enrichCache = {};
            _applyScopeUI(currentScope);
        };
    }

    function _populateScopeSelector() {
        var sel = document.getElementById('scopeSelector');
        if (!sel) return;
        sel.innerHTML = '';
        scopeList.forEach(function (scope) {
            var opt = document.createElement('option');
            opt.value = scope.key;
            opt.textContent = scope.label;
            sel.appendChild(opt);
        });
    }

    /**
     * Singularize a scope label for the "+ New" button.
     * Rule: if the label ends in 's' but NOT 'ss', drop the trailing 's'.
     * All known change workspace labels follow this pattern:
     *   "Change Orders" → "Change Order"
     *   "Problem Reports" → "Problem Report"
     *   "Lean Change Orders" → "Lean Change Order"
     *   "Change Requests" → "Change Request"
     *   "Change Tasks" → "Change Task"
     */
    function _singularize(label) {
        if (!label) return label;
        if (label.length >= 2 && label.charAt(label.length - 1) === 's' && label.charAt(label.length - 2) !== 's') {
            return label.slice(0, -1);
        }
        return label;
    }

    function _applyScopeUI(scope) {
        currentPage = 1;
        var viewSel = document.getElementById('viewSelector');
        var btnNew  = document.getElementById('btnNew');
        if (scope === cfg.unifiedDefaultKey) {
            // Unified "All" view: hide per-workspace controls, load merged table.
            if (viewSel) viewSel.style.display = 'none';
            if (btnNew)  btnNew.style.display  = 'none';
            loadUnifiedView();
        } else {
            // Drilled-down single workspace.
            if (viewSel) viewSel.style.display = '';
            if (btnNew)  btnNew.style.display  = '';
            // Set New button label: "New <SingularScopeLabel>"
            if (btnNew) {
                var scopeLabel = _scopeLabels[scope] || cfg.newItemLabel || 'New Item';
                btnNew.textContent = 'New ' + _singularize(scopeLabel);
            }
            // Update currentWorkspaceId so all existing single-ws flows target it.
            currentWorkspaceId = scope;
            loadListView();
        }
    }

    // -------------------------------------------------------------------------
    // UNIFIED VIEW — load and render all change records in one table
    // -------------------------------------------------------------------------

    function loadUnifiedView() {
        var myToken = ++_listLoadSeq;
        // Clear enrichment cache on a full unified refresh so pills are re-fetched.
        _enrichCache = {};
        showView('list');
        setMsg('listMsg', '', '');
        setLoading('listLoading', true);
        var tbody = document.getElementById('tableBody');
        if (tbody) tbody.innerHTML = '';
        send(cfg.unifiedAction, {}).then(function (r) {
            if (myToken !== _listLoadSeq) return;  // superseded by a later load
            setLoading('listLoading', false);
            var d = parse(r);
            if (!d.success) {
                setMsg('listMsg', d.error || 'Failed to load change records.', 'error');
                return;
            }
            unifiedRows     = d.rows    || [];
            unifiedColumns  = d.columns || [];
            unifiedTruncated = !!d.truncated;
            totalRecords = unifiedRows.length;
            if (unifiedTruncated) {
                setMsg('listMsg',
                    'Some workspaces returned partial results (record cap reached). ' +
                    'Switch to a specific workspace for the full list.',
                    'info');
            }
            renderUnifiedTable(myToken);
            renderPagination();
        }).catch(function (e) {
            if (myToken !== _listLoadSeq) return;
            setLoading('listLoading', false);
            setMsg('listMsg', 'Load error: ' + e, 'error');
        });
    }

    function renderUnifiedTable(renderToken) {
        // renderToken: optional stale-guard — if _listLoadSeq changed since we were
        // called, abort silently (a new scope/refresh is in flight).
        if (renderToken !== undefined && renderToken !== _listLoadSeq) return;

        var thead = document.getElementById('tableHead');
        var tbody = document.getElementById('tableBody');
        if (!thead || !tbody) return;
        thead.innerHTML = '';
        tbody.innerHTML = '';

        var colCount = unifiedColumns.length + 2; // +2 for Status + Affected
        // Guard on rows-only: unifiedColumns is always non-empty (type/descriptor/owner),
        // so the old "both empty" check never fired when rows:[] was returned.
        if (!unifiedRows.length) {
            tbody.innerHTML = '<tr><td colspan="' + colCount + '" style="padding:12px;color:#888;text-align:center;">No change records found.</td></tr>';
            return;
        }

        // Header — base columns first, then Status + Affected
        var tr = document.createElement('tr');
        unifiedColumns.forEach(function (col) {
            var th = document.createElement('th');
            th.textContent = col.label || col.id;
            tr.appendChild(th);
        });
        var thStatus = document.createElement('th');
        thStatus.textContent = 'Status';
        thStatus.className = 'unified-status-th';
        tr.appendChild(thStatus);
        var thAff = document.createElement('th');
        thAff.textContent = 'Affected';
        thAff.className = 'unified-affected-th';
        tr.appendChild(thAff);
        thead.appendChild(tr);

        // Body — plain-text cells (descriptor/owner/type), click → detail.
        // Status + Affected cells are seeded with a placeholder and filled by
        // _enrichUnifiedRows() after render.
        // Apply client-side pagination (unifiedRows is already fully loaded).
        var start    = (currentPage - 1) * pageSize;
        var end      = Math.min(start + pageSize, unifiedRows.length);
        var pageRows = unifiedRows.slice(start, end);

        pageRows.forEach(function (row) {
            var tr2 = document.createElement('tr');
            tr2.style.cursor = 'pointer';
            tr2.setAttribute('data-item-id',  row.itemId);
            tr2.setAttribute('data-ws-id',    row.workspaceId);
            var enrichKey = row.workspaceId + ':' + row.itemId;
            tr2.setAttribute('data-enrich-key', enrichKey);
            (function (r) {
                tr2.onclick = function () {
                    openDetail(r.itemId, r.workspaceId, r.descriptor || r.title || null);
                };
            })(row);

            unifiedColumns.forEach(function (col) {
                var td = document.createElement('td');
                td.textContent = row[col.id] || '';
                tr2.appendChild(td);
            });

            // Status cell — filled lazily by _enrichUnifiedRows
            var tdStatus = document.createElement('td');
            tdStatus.className = 'unified-status-cell';
            tdStatus.setAttribute('data-enrich-status', enrichKey);
            var statusPh = document.createElement('span');
            statusPh.className = 'state-pill state-pill--loading';
            statusPh.textContent = '…';
            statusPh.setAttribute('aria-label', 'Loading status');
            tdStatus.appendChild(statusPh);
            tr2.appendChild(tdStatus);

            // Affected cell — filled lazily by _enrichUnifiedRows
            var tdAff = document.createElement('td');
            tdAff.className = 'unified-affected-cell';
            tdAff.setAttribute('data-enrich-affected', enrichKey);
            var affPh = document.createElement('span');
            affPh.className = 'affected-pill affected-pill--loading';
            affPh.textContent = '…';
            affPh.setAttribute('aria-label', 'Loading affected count');
            tdAff.appendChild(affPh);
            tr2.appendChild(tdAff);

            tbody.appendChild(tr2);
        });

        // Kick off lazy enrichment after the page is rendered.
        _enrichUnifiedRows(renderToken);
    }

    /**
     * Fetch enrichment (state + affectedCount) for the CURRENT page's rows in the
     * unified table, skip already-cached items, merge results into _enrichCache, and
     * update the visible Status/Affected cells.
     *
     * Guarded by renderToken (== _listLoadSeq at render time) so a scope switch or
     * refresh that fires mid-enrichment doesn't write pills into the wrong table.
     */
    function _enrichUnifiedRows(renderToken) {
        var myToken = renderToken !== undefined ? renderToken : _listLoadSeq;

        // Collect items on the current visible page that aren't in the cache yet.
        var start    = (currentPage - 1) * pageSize;
        var end      = Math.min(start + pageSize, unifiedRows.length);
        var pageRows = unifiedRows.slice(start, end);
        var needed   = [];
        pageRows.forEach(function (row) {
            var key = row.workspaceId + ':' + row.itemId;
            if (!_enrichCache[key]) {
                needed.push({workspaceId: row.workspaceId, itemId: row.itemId});
            }
        });

        // Apply any already-cached items immediately (handles back-paging).
        _applyEnrichmentToPage(myToken);

        if (!needed.length) return;

        send('getRecordsEnrichment', {items: needed}).then(function (r) {
            if (myToken !== _listLoadSeq) return;  // scope changed while in flight
            var d = parse(r);
            if (!d || !d.success) return;
            var incoming = d.enrichment || {};
            Object.keys(incoming).forEach(function (k) {
                _enrichCache[k] = incoming[k];
            });
            _applyEnrichmentToPage(myToken);
        }).catch(function () { /* enrichment is best-effort */ });
    }

    /**
     * Walk the visible unified table rows and fill Status + Affected cells from
     * _enrichCache.  Only runs if the current token still matches _listLoadSeq.
     */
    function _applyEnrichmentToPage(myToken) {
        if (myToken !== _listLoadSeq) return;
        document.querySelectorAll('tr[data-enrich-key]').forEach(function (tr) {
            var key = tr.getAttribute('data-enrich-key');
            var data = _enrichCache[key];
            if (!data) return;

            // Status cell
            var statusCell = tr.querySelector('[data-enrich-status]');
            if (statusCell) {
                statusCell.innerHTML = '';
                var pill = document.createElement('span');
                pill.className = 'state-pill';
                pill.textContent = data.state || '—';
                statusCell.appendChild(pill);
            }

            // Affected cell
            var affCell = tr.querySelector('[data-enrich-affected]');
            if (affCell) {
                affCell.innerHTML = '';
                var count = typeof data.affectedCount === 'number' ? data.affectedCount : 0;
                if (count > 0) {
                    // Clickable pill: opens the record's detail on the LINKEDITEMS tab.
                    var affPill = document.createElement('span');
                    affPill.className = 'affected-pill';
                    affPill.textContent = String(count);
                    affPill.setAttribute('role', 'button');
                    affPill.setAttribute('tabindex', '0');
                    affPill.title = 'Open Affected Items (' + count + ')';
                    affPill.setAttribute('aria-label', 'Open Affected Items (' + count + ')');
                    // Resolve the row's itemId + workspaceId from the data attribute.
                    var parts  = key.split(':');
                    var wsId   = parts[0];
                    var itemId = parts[1];
                    (function (wid, iid) {
                        function _openAffected(e) {
                            if (e) e.stopPropagation();
                            _pendingInitialTab = 'LINKEDITEMS';
                            openDetail(iid, wid, null);
                        }
                        affPill.onclick = _openAffected;
                        affPill.onkeydown = function (e) {
                            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _openAffected(e); }
                        };
                    })(wsId, itemId);
                    affCell.appendChild(affPill);
                } else {
                    var zeroSpan = document.createElement('span');
                    zeroSpan.className = 'affected-pill affected-pill--zero';
                    zeroSpan.textContent = '—';
                    affCell.appendChild(zeroSpan);
                }
            }
        });
    }

    function showNoAccessView(urlGroups, urlTpl) {
        if (typeof plmShowNoAccess === 'function') {
            plmShowNoAccess(urlGroups, urlTpl);
        } else {
            // bridge.js not loaded? best-effort inline
            var nav = document.getElementById('noAccessView');
            if (nav) nav.classList.add('visible');
        }
    }

    // -------------------------------------------------------------------------
    // LIST VIEW
    // -------------------------------------------------------------------------
    function loadListView() {
        var myToken = ++_listLoadSeq;
        showView('list');
        setMsg('listMsg', '', '');
        setLoading('listLoading', true);
        // When a scope is active, pass workspaceId so the workspace actions target it.
        var wsPayload = _scopedWsPayload();
        Promise.all([
            send('getTableaus', wsPayload).then(parse),
            send('getViewFields', Object.assign({viewId: _detectedViewId}, wsPayload)).then(parse)
        ]).then(function (results) {
            if (myToken !== _listLoadSeq) return;  // superseded by a later scope switch
            setLoading('listLoading', false);
            var tRes = results[0], fRes = results[1];
            if (!tRes.success) {
                setMsg('listMsg', tRes.error || 'Failed to load views.', 'error');
                return;
            }
            tableaus = tRes.tableaus || [];
            buildFieldMaps(fRes.fields || []);
            populateViewSelector();
            if (!tableaus.length) {
                var vsEl = document.getElementById('viewSelector');
                var bnEl = document.getElementById('btnNew');
                if (vsEl) vsEl.style.display = 'none';
                if (bnEl) bnEl.style.display  = 'none';
                setMsg('listMsg',
                    'No list views are configured for this workspace. ' +
                    'Records can be viewed and managed in Fusion Manage.',
                    'info');
                return;
            }
            loadTableauData();
        }).catch(function (e) {
            if (myToken !== _listLoadSeq) return;
            setLoading('listLoading', false);
            setMsg('listMsg', 'Load error: ' + e, 'error');
        });
    }

    /**
     * Return a payload fragment {workspaceId: ...} when operating in a drilled-down
     * scope (currentScope is a workspaceId, not the unifiedDefaultKey).
     * Returns {} when scopes are not enabled or the current scope is 'all'.
     * This is appended to every single-workspace action payload so the Python
     * _ws_from(data) helper can target the correct workspace.
     */
    function _scopedWsPayload() {
        if (!cfg.scopes) return {};
        if (!currentScope || currentScope === cfg.unifiedDefaultKey) return {};
        return {workspaceId: currentScope};
    }

    function buildFieldMaps(fields) {
        fieldTitleMap = {};
        fieldDefMap   = {};
        fields.forEach(function (f) {
            if (f.id) {
                fieldTitleMap[f.id] = f.title || f.id;
                fieldDefMap[f.id]   = f;
            }
        });
    }

    function populateViewSelector() {
        var sel = document.getElementById('viewSelector');
        if (!sel) return;
        sel.innerHTML = '';
        tableaus.forEach(function (t) {
            var opt = document.createElement('option');
            opt.value       = t.id;
            opt.textContent = t.title + (t.type === 'DEFAULT' ? ' (Default)' : '');
            sel.appendChild(opt);
        });
        var saved = localStorage.getItem(cfg.viewSelectKey);
        if (saved && tableaus.some(function (t) { return t.id === saved; })) {
            sel.value        = saved;
            currentTableauId = saved;
        } else if (tableaus.length > 0) {
            var def = tableaus.filter(function (t) { return t.type === 'DEFAULT'; })[0] || tableaus[0];
            sel.value        = def.id;
            currentTableauId = def.id;
        }
        sel.onchange = function () {
            currentTableauId = sel.value;
            localStorage.setItem(cfg.viewSelectKey, currentTableauId);
            currentPage = 1;
            loadTableauData();
        };
    }

    function loadTableauData() {
        if (!currentTableauId) return;
        setLoading('listLoading', true);
        setMsg('listMsg', '', '');
        var tbody = document.getElementById('tableBody');
        if (tbody) tbody.innerHTML = '';
        var payload = Object.assign({tableauId: currentTableauId, page: currentPage, size: pageSize}, _scopedWsPayload());
        send('getTableauData', payload).then(function (r) {
            setLoading('listLoading', false);
            var d = parse(r);
            if (!d.success) { setMsg('listMsg', d.error || 'Failed to load data.', 'error'); return; }
            tableColumns  = d.columns || [];
            tableRows     = d.rows    || [];
            totalRecords  = d.total   || 0;
            renderTable();
            renderPagination();
        }).catch(function (e) {
            setLoading('listLoading', false);
            setMsg('listMsg', 'Load error: ' + e, 'error');
        });
    }

    function renderTable() {
        var thead = document.getElementById('tableHead');
        var tbody = document.getElementById('tableBody');
        if (!thead || !tbody) return;
        thead.innerHTML = '';
        tbody.innerHTML = '';
        if (!tableColumns.length && !tableRows.length) {
            tbody.innerHTML = '<tr><td colspan="1" style="padding:12px;color:#888;text-align:center;">No records found.</td></tr>';
            return;
        }
        var hasAi = cfg.showAffectedItems;

        // Header
        var tr = document.createElement('tr');
        if (hasAi) {
            var thAi = document.createElement('th');
            thAi.className = 'ai-count-th';
            thAi.textContent = 'Affected';
            thAi.title = 'Affected items on each record';
            tr.appendChild(thAi);
        }
        tableColumns.forEach(function (col) {
            var th = document.createElement('th');
            th.textContent = fieldTitleMap[col.id] || col.id;
            tr.appendChild(th);
        });
        thead.appendChild(tr);

        // Body
        var rowsForAiCounts = [];
        tableRows.forEach(function (row) {
            var tr2 = document.createElement('tr');
            tr2.setAttribute('data-item-id', row.itemId);
            tr2.setAttribute('data-ws-id',   row.workspaceId);
            tr2.onclick = function () { openDetail(row.itemId, row.workspaceId, null); };

            if (hasAi) {
                var tdAi = document.createElement('td');
                tdAi.className = 'ai-count-cell';
                tdAi.setAttribute('data-ai-item-id', String(row.itemId));
                tdAi.title = 'Open Affected Items';
                var pill = document.createElement('span');
                pill.className = 'ai-count-pill';
                pill.textContent = '…';
                tdAi.appendChild(pill);
                (function (r) {
                    tdAi.onclick = function (e) {
                        e.stopPropagation();
                        _pendingInitialTab = 'LINKEDITEMS';
                        openDetail(r.itemId, r.workspaceId, null);
                    };
                })(row);
                tr2.appendChild(tdAi);
                rowsForAiCounts.push(String(row.itemId));
            }

            tableColumns.forEach(function (col) {
                var td = document.createElement('td');
                var cell = row.fields[col.id];
                renderTableCell(td, cell ? cell.value : undefined, col.typeTitle);
                tr2.appendChild(td);
            });
            tbody.appendChild(tr2);
        });
        if (hasAi && rowsForAiCounts.length) loadAffectedItemsCounts(rowsForAiCounts);
    }

    function loadAffectedItemsCounts(itemIds) {
        send('getItemsTabCounts', Object.assign({itemIds: itemIds}, _scopedWsPayload())).then(function (r) {
            var d = parse(r);
            if (!d || !d.success) return;
            var counts = d.counts || {};
            Object.keys(counts).forEach(function (iid) {
                var td = document.querySelector('td.ai-count-cell[data-ai-item-id="' + iid + '"]');
                if (!td) return;
                var n = (counts[iid] || {}).LINKEDITEMS || 0;
                var p = td.querySelector('.ai-count-pill');
                if (p) p.textContent = n;
                td.classList.add(n > 0 ? 'ai-count-has' : 'ai-count-zero');
            });
            document.querySelectorAll('td.ai-count-cell').forEach(function (td) {
                if (td.classList.contains('ai-count-has') || td.classList.contains('ai-count-zero')) return;
                var p = td.querySelector('.ai-count-pill');
                if (p) { p.textContent = '—'; td.classList.add('ai-count-zero'); }
            });
        }).catch(function () { /* ignore */ });
    }

    function renderTableCell(td, val, typeTitle) {
        if (val === null || val === undefined) { td.textContent = '—'; td.style.color = '#aaa'; return; }
        if (typeof val === 'string' && hasHtmlContent(val)) {
            td.className = 'td-html'; td.innerHTML = decodeHtmlEntities(val); return;
        }
        if (typeof val === 'object' && !Array.isArray(val)) {
            td.textContent = val.title || val.label || val.value || JSON.stringify(val); return;
        }
        if (Array.isArray(val)) {
            td.textContent = val.map(function (v) { return typeof v === 'object' ? (v.title || v.label || '') : v; }).join(', ');
            return;
        }
        td.textContent = String(val);
    }

    function renderPagination() {
        var count = _isUnifiedScope() ? unifiedRows.length : totalRecords;
        var totalPages = Math.max(1, Math.ceil(count / pageSize));
        var pi = document.getElementById('pageInfo');
        if (pi) pi.textContent = 'Page ' + currentPage + ' of ' + totalPages + '  (' + count + ' records)';
        var bPrev = document.getElementById('btnPrev');
        var bNext = document.getElementById('btnNext');
        if (bPrev) bPrev.disabled = currentPage <= 1;
        if (bNext) bNext.disabled = currentPage >= totalPages;
    }

    function _isUnifiedScope() {
        return cfg.scopes && currentScope === cfg.unifiedDefaultKey;
    }

    // -------------------------------------------------------------------------
    // DETAIL VIEW
    // -------------------------------------------------------------------------
    function openDetail(itemId, wsId, descriptor) {
        // Capture and clear _pendingInitialTab immediately so a tab-load failure
        // or item-identity mismatch can never leave a stale value that leaks into
        // the next openDetail call.
        var initialTab = _pendingInitialTab;
        _pendingInitialTab = null;

        if (!detailStack.length) {
            detailStack = [{
                label: cfg.title, view: 'list',
                tableauId: currentTableauId, page: currentPage
            }];
        }
        detailStack.push({label: descriptor || 'Loading…', view: 'detail',
                          itemId: itemId, wsId: wsId || cfg.workspaceIdDefault});
        currentItemId      = itemId;
        currentWorkspaceId = wsId || cfg.workspaceIdDefault;
        showView('detail');
        renderDetailBreadcrumb();
        setLoading('detailLoading', true);
        setMsg('detailMsg', '', '');
        var dtEl = document.getElementById('detailTitle');
        if (dtEl) dtEl.textContent = '';
        var dsSec = document.getElementById('detailSections');
        if (dsSec) dsSec.innerHTML = '';
        var wfSec = document.getElementById('workflowSection');
        if (wfSec) wfSec.style.display = 'none';
        hideBanner();

        send(cfg.detailAction, {itemId: itemId, workspaceId: wsId || cfg.workspaceIdDefault}).then(function (r) {
            setLoading('detailLoading', false);
            var d = parse(r);
            if (!d.success) { setMsg('detailMsg', d.error || 'Failed to load item.', 'error'); return; }
            currentItem        = d.item;
            currentItemETag    = d.etag || '';
            currentTransitions = d.transitions || [];
            currentStep        = d.currentStep || 0;
            var desc = getItemDescriptor(d.item);
            if (desc && detailStack.length) {
                detailStack[detailStack.length - 1].label = desc;
                renderDetailBreadcrumb();
            }
            renderDetailView(d.item);
            renderWorkflowActions();
            initDetailTabsForItem(currentWorkspaceId, itemId, initialTab);
        }).catch(function (e) {
            setLoading('detailLoading', false);
            setMsg('detailMsg', 'Load error: ' + e, 'error');
        });
    }

    function openDetailForWs(itemId, wsId, label) {
        if (!detailStack.some(function (e) { return e.view === 'list'; })) {
            detailStack.unshift({label: cfg.title, view: 'list', tableauId: currentTableauId, page: currentPage});
        }
        detailStack.push({label: label || 'Item', view: 'detail', itemId: itemId, wsId: wsId});
        currentItemId      = itemId;
        currentWorkspaceId = wsId;
        showView('detail');
        renderDetailBreadcrumb();
        setLoading('detailLoading', true);
        setMsg('detailMsg', '', '');
        var dtEl = document.getElementById('detailTitle');
        if (dtEl) dtEl.textContent = '';
        var dsSec = document.getElementById('detailSections');
        if (dsSec) dsSec.innerHTML = '';
        var wfSec = document.getElementById('workflowSection');
        if (wfSec) wfSec.style.display = 'none';
        hideBanner();

        send(cfg.detailAction, {itemId: itemId, workspaceId: wsId}).then(function (r) {
            setLoading('detailLoading', false);
            var d = parse(r);
            if (!d.success) { setMsg('detailMsg', d.error || 'Failed to load item.', 'error'); return; }
            currentItem        = d.item;
            currentItemETag    = d.etag || '';
            currentTransitions = d.transitions || [];
            currentStep        = d.currentStep || 0;
            var desc = getItemDescriptor(d.item);
            if (desc && detailStack.length) {
                detailStack[detailStack.length - 1].label = desc;
                renderDetailBreadcrumb();
            }
            renderDetailView(d.item);
            // Add Open-in-Fusion button for CAD workspaces
            if (isCadWorkspace(wsId)) {
                var toolbar = document.getElementById('detailToolbar');
                if (toolbar) {
                    var btnFusion = document.createElement('button');
                    btnFusion.className = 'btn btn-sm';
                    btnFusion.textContent = 'Open in Fusion';
                    (function (wid, iid) {
                        btnFusion.onclick = function () {
                            btnFusion.disabled = true;
                            send('openInFusion', {workspaceId: wid, itemId: iid}).then(function (r2) {
                                btnFusion.disabled = false;
                                var dr = parse(r2);
                                if (!dr.success) showBanner(dr.error || 'Could not open in Fusion.', 'error');
                            }).catch(function () { btnFusion.disabled = false; });
                        };
                    })(wsId, itemId);
                    toolbar.appendChild(btnFusion);
                }
            }
            renderWorkflowActions();
            initDetailTabsForItem(wsId, itemId);
        }).catch(function (e) {
            setLoading('detailLoading', false);
            setMsg('detailMsg', 'Load error: ' + e, 'error');
        });
    }

    function getItemDescriptor(item) {
        if (!item) return '';
        var sections = item.sections || [];
        for (var i = 0; i < sections.length; i++) {
            var fields = sections[i].fields || [];
            for (var j = 0; j < fields.length; j++) {
                var f = fields[j];
                var selfRef = f.__self__ || '';
                if (selfRef.indexOf('DESCRIPTOR') !== -1 ||
                    (f.title || '').toUpperCase() === 'DESCRIPTOR') {
                    var v = f.value;
                    if (typeof v === 'string' && v.trim()) return v.trim();
                }
            }
        }
        return 'Item ' + (item.id || '');
    }

    function renderDetailBreadcrumb() {
        var el = document.getElementById('detailBreadcrumb');
        if (!el) return;
        el.innerHTML = '';
        detailStack.forEach(function (entry, idx) {
            if (idx > 0) {
                var sep = document.createElement('span');
                sep.className = 'breadcrumb-sep';
                sep.textContent = '›';
                el.appendChild(sep);
            }
            var span = document.createElement('span');
            span.className = 'breadcrumb-item' + (idx === detailStack.length - 1 ? ' current' : '');
            span.textContent = entry.label;
            if (idx < detailStack.length - 1) {
                (function (e) { span.onclick = function () { navigateBreadcrumb(e); }; })(entry);
            }
            el.appendChild(span);
        });
    }

    function navigateBreadcrumb(entry) {
        var idx = detailStack.indexOf(entry);
        if (idx < 0) idx = 0;
        detailStack = detailStack.slice(0, idx + 1);
        if (entry.view === 'list') {
            currentTableauId = entry.tableauId || currentTableauId;
            currentPage      = entry.page || 1;
            detailStack = [];
            if (_isUnifiedScope()) {
                // Return to unified table — rows are already loaded, just re-render.
                showView('list');
                renderUnifiedTable(_listLoadSeq);
                renderPagination();
                if (unifiedTruncated) {
                    setMsg('listMsg',
                        'Some workspaces returned partial results (record cap reached). ' +
                        'Switch to a specific workspace for the full list.',
                        'info');
                }
            } else {
                loadListView();
            }
        } else if (entry.view === 'detail') {
            detailStack.pop();
            openDetail(entry.itemId, entry.wsId, entry.label);
        }
    }

    function renderDetailView(item) {
        var dtEl = document.getElementById('detailTitle');
        if (dtEl) dtEl.textContent = getItemDescriptor(item);

        var toolbar = document.getElementById('detailToolbar');
        if (toolbar) {
            toolbar.innerHTML = '';
            var btnEdit = document.createElement('button');
            btnEdit.className = 'btn btn-sm';
            btnEdit.textContent = 'Edit';
            btnEdit.onclick = function () { openEditForm(currentItemId, currentWorkspaceId); };
            toolbar.appendChild(btnEdit);

            var btnBrowser = document.createElement('button');
            btnBrowser.className = 'btn btn-sm';
            btnBrowser.textContent = 'Open in Browser';
            btnBrowser.onclick = function () {
                if (item.openInBrowserUrl) send('openInBrowser', {url: item.openInBrowserUrl});
            };
            toolbar.appendChild(btnBrowser);

            if (typeof cfg.hooks.onDetailToolbarExtra === 'function') {
                cfg.hooks.onDetailToolbarExtra(toolbar, item, currentWorkspaceId);
            }
        }

        var container = document.getElementById('detailSections');
        if (!container) return;
        container.innerHTML = '';
        (item.sections || []).forEach(function (sec) {
            var fields = sec.fields || [];
            if (!fields.length) return;
            var block = document.createElement('div');
            block.className = 'section-block';
            var titleEl = document.createElement('div');
            titleEl.className = 'section-title';
            titleEl.textContent = sec.title || sec.name || 'Section';
            var fieldsEl = document.createElement('div');
            fieldsEl.className = 'section-fields';
            titleEl.onclick = function () {
                titleEl.classList.toggle('collapsed');
                fieldsEl.style.display = titleEl.classList.contains('collapsed') ? 'none' : '';
            };
            fields.forEach(function (f) {
                var row = document.createElement('div');
                row.className = 'field-row';
                var lbl = document.createElement('div');
                lbl.className = 'field-label';
                lbl.textContent = f.title || f.id || '';
                var val = document.createElement('div');
                val.className = 'field-value';
                renderDetailFieldValue(val, f.value, f);
                row.appendChild(lbl);
                row.appendChild(val);
                fieldsEl.appendChild(row);
            });
            block.appendChild(titleEl);
            block.appendChild(fieldsEl);
            container.appendChild(block);
        });
    }

    function buildOpenInFusionIcon(wsId, itemId) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'open-in-fusion-icon';
        btn.title = 'Open in Fusion';
        btn.setAttribute('aria-label', 'Open in Fusion');
        btn.setAttribute('data-workspace-id', wsId);
        btn.setAttribute('data-item-id', itemId);
        btn.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            + '<path d="M10 3h4v4"/><path d="M14 3l-7 7"/>'
            + '<path d="M12 10v3a1.2 1.2 0 0 1-1.2 1.2H3.2A1.2 1.2 0 0 1 2 13V5.2A1.2 1.2 0 0 1 3.2 4H6"/>'
            + '</svg>';
        btn.onclick = function (e) {
            e.preventDefault(); e.stopPropagation();
            btn.disabled = true;
            send('openInFusion', {workspaceId: wsId, itemId: itemId}).then(function (r) {
                btn.disabled = false;
                var dr = parse(r);
                if (!dr.success) showBanner(dr.error || 'Could not open in Fusion.', 'error');
            }).catch(function () { btn.disabled = false; });
        };
        return btn;
    }

    function renderDetailFieldValue(el, val, fieldDef) {
        if (typeof cfg.hooks.renderCustomDetailField === 'function') {
            if (cfg.hooks.renderCustomDetailField(el, val, fieldDef)) return;
        }
        if (val === null || val === undefined) {
            el.classList.add('null-val'); el.textContent = '—'; return;
        }
        if (Array.isArray(val)) {
            if (!val.length) { el.classList.add('null-val'); el.textContent = '—'; return; }
            el.classList.add('multi-value');
            val.forEach(function (v) {
                var row = document.createElement('div');
                row.className = 'multi-value-entry';
                if (typeof v === 'object' && v && v.link) {
                    var wsId  = extractWorkspaceIdFromLink(v.link);
                    var iid   = extractItemIdFromLink(v.link);
                    if (isCadWorkspace(wsId) && iid) row.appendChild(buildOpenInFusionIcon(wsId, iid));
                    var a = document.createElement('a');
                    a.textContent = v.title || v.label || v.value || 'Item';
                    a.title = v.link;
                    if (wsId && iid) {
                        (function (wid, id, lbl) {
                            a.onclick = function () { openDetailForWs(id, wid, lbl); };
                        })(wsId, iid, a.textContent);
                    }
                    row.appendChild(a);
                } else {
                    row.textContent = (typeof v === 'object' && v) ? (v.title || v.label || '') : String(v);
                }
                el.appendChild(row);
            });
            return;
        }
        if (typeof val === 'object' && val.link) {
            var wsId2  = extractWorkspaceIdFromLink(val.link);
            var iid2   = extractItemIdFromLink(val.link);
            if (isCadWorkspace(wsId2) && iid2) el.appendChild(buildOpenInFusionIcon(wsId2, iid2));
            var a2 = document.createElement('a');
            a2.textContent = val.title || val.label || val.value || 'Item';
            if (wsId2 && iid2) {
                (function (wid, id, lbl) {
                    a2.onclick = function () { openDetailForWs(id, wid, lbl); };
                })(wsId2, iid2, a2.textContent);
            }
            el.appendChild(a2);
            return;
        }
        if (typeof val === 'string' && hasHtmlContent(val)) {
            el.innerHTML = decodeHtmlEntities(val); return;
        }
        if (typeof val === 'object') {
            el.textContent = val.title || val.label || val.value || JSON.stringify(val); return;
        }
        el.textContent = String(val);
    }

    // -------------------------------------------------------------------------
    // DETAIL TABS
    // -------------------------------------------------------------------------
    function ensureDetailTabsDom() {
        if (document.getElementById('detailTabs')) return;
        var scroll = document.getElementById('detailScroll');
        if (!scroll || !scroll.parentNode) return;
        var tabs = document.createElement('div');
        tabs.className = 'detail-tabs';
        tabs.id = 'detailTabs';
        scroll.parentNode.insertBefore(tabs, scroll);
        scroll.classList.add('tab-pane', 'tab-details', 'active');
        var linkedPane = document.createElement('div');
        linkedPane.className = 'tab-pane tab-linked-items';
        linkedPane.id = 'tabLinkedItemsPane';
        scroll.parentNode.insertBefore(linkedPane, scroll.nextSibling);
    }

    function renderDetailTabs() {
        var host = document.getElementById('detailTabs');
        if (!host) return;
        host.innerHTML = '';
        var visible = [{id: 'details', label: 'Details', count: null}];
        (currentItemTabs || []).forEach(function (t) {
            var name = t && t.name;
            if (!name || !SUPPORTED_TABS[name]) return;
            if (name === 'LINKEDITEMS' && !hasPerm('view_workflow_items')) return;
            visible.push({
                id: name,
                label: TAB_LABELS[name] || name,
                count: typeof t.totalCount === 'number' ? t.totalCount : null
            });
        });
        visible.forEach(function (v) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'detail-tab-btn' + (v.id === activeDetailTab ? ' active' : '');
            btn.setAttribute('data-tab-id', v.id);
            btn.textContent = v.label;
            if (v.count !== null) {
                var cnt = document.createElement('span');
                cnt.className = 'tab-count';
                cnt.textContent = v.count;
                btn.appendChild(cnt);
            }
            (function (id) { btn.onclick = function () { switchDetailTab(id); }; })(v.id);
            host.appendChild(btn);
        });
        if (visible.length <= 1 && lastTabsDebug) {
            var dbg = document.createElement('span');
            dbg.className = 'detail-tab-debug';
            dbg.textContent = lastTabsDebug;
            dbg.title = lastTabsDebug;
            host.appendChild(dbg);
        }
        if (!visible.some(function (v) { return v.id === activeDetailTab; })) {
            switchDetailTab('details');
        }
    }

    function switchDetailTab(tabId) {
        activeDetailTab = tabId;
        document.querySelectorAll('#detailTabs .detail-tab-btn').forEach(function (b) {
            b.classList.toggle('active', b.getAttribute('data-tab-id') === tabId);
        });
        var scroll = document.getElementById('detailScroll');
        var linked = document.getElementById('tabLinkedItemsPane');
        if (scroll) scroll.classList.toggle('active', tabId === 'details');
        if (linked) linked.classList.toggle('active', tabId === 'LINKEDITEMS');
        if (tabId === 'LINKEDITEMS') renderAffectedItemsTab();
    }

    function loadItemTabs(wsId, itemId) {
        return send('getItemTabs', {workspaceId: wsId, itemId: itemId}).then(function (r) {
            var d = parse(r);
            if (!d || !d.success) { lastTabsDebug = 'Tabs API: ' + String((d && d.error) || 'unknown').slice(0, 80); return []; }
            if (!Array.isArray(d.tabs) || !d.tabs.length) { lastTabsDebug = 'Tabs: none returned'; return []; }
            lastTabsDebug = 'Tabs: ' + d.tabs.map(function (t) { return t && t.name; }).filter(Boolean).join(', ');
            return d.tabs;
        }).catch(function (e) { lastTabsDebug = 'Tabs fetch error: ' + (e && e.message || 'unknown'); return []; });
    }

    function loadItemPermissions(wsId, itemId) {
        return send('getItemPermissions', {workspaceId: wsId, itemId: itemId}).then(function (r) {
            var d = parse(r);
            if (!d || !d.success || !Array.isArray(d.permissions)) return null;
            var s = {};
            d.permissions.forEach(function (p) { s[p] = true; });
            return { has: function (name) { return !!s[name]; } };
        }).catch(function () { return null; });
    }

    // initialTab: optional tab name to auto-switch to once tabs are loaded
    // (captured from _pendingInitialTab by openDetail before the async fetch,
    //  so failures cannot leave a stale global that bleeds into the next open).
    function initDetailTabsForItem(wsId, itemId, initialTab) {
        ensureDetailTabsDom();
        currentItemTabs    = [];
        currentPermissions = null;
        activeDetailTab    = 'details';
        var scroll = document.getElementById('detailScroll');
        var linked = document.getElementById('tabLinkedItemsPane');
        if (scroll) scroll.classList.add('active');
        if (linked) linked.classList.remove('active');
        renderDetailTabs();
        Promise.all([loadItemTabs(wsId, itemId), loadItemPermissions(wsId, itemId)]).then(function (results) {
            if (String(currentWorkspaceId) !== String(wsId) || String(currentItemId) !== String(itemId)) return;
            currentItemTabs    = results[0] || [];
            currentPermissions = results[1];
            renderDetailTabs();
            if (initialTab && SUPPORTED_TABS[initialTab]) {
                var avail = (currentItemTabs || []).some(function (t) { return t && t.name === initialTab; });
                if (avail) switchDetailTab(initialTab);
            }
        });
    }

    function renderAffectedItemsTab() {
        var pane = document.getElementById('tabLinkedItemsPane');
        if (!pane) return;
        pane.innerHTML = '';
        pane.classList.add('affected-items-pane');
        var wsId   = currentWorkspaceId;
        var itemId = currentItemId;
        // Extract the real view ID from the LINKEDITEMS tab's __self__ link rather than
        // hardcoding 11 — each workspace may configure a different system view ID.
        var aiViewId = 11;
        (currentItemTabs || []).forEach(function (t) {
            if (t && t.name === 'LINKEDITEMS') {
                var m = ((t.__self__ || t.link || '')).match(/views\/(\d+)/);
                if (m) { aiViewId = parseInt(m[1], 10); }
            }
        });
        var toolbar = document.createElement('div');
        toolbar.className = 'affected-items-toolbar';
        if (hasPerm('add_workflow_items')) {
            var btnAdd = document.createElement('button');
            btnAdd.type = 'button';
            btnAdd.className = 'btn btn-primary btn-sm';
            btnAdd.textContent = '+ Add Affected Items';
            (function (wid, iid) {
                btnAdd.onclick = function () { openAffectedItemsPicker(wid, iid); };
            })(wsId, itemId);
            toolbar.appendChild(btnAdd);
        }
        var countEl = document.createElement('span');
        countEl.className = 'ai-count';
        toolbar.appendChild(countEl);
        pane.appendChild(toolbar);
        var listEl = document.createElement('div');
        listEl.className = 'affected-items-list';
        pane.appendChild(listEl);
        var loading = document.createElement('div');
        loading.className = 'loading-bar';
        loading.textContent = 'Loading affected items…';
        pane.appendChild(loading);

        send('getAffectedItems', {workspaceId: wsId, itemId: itemId, viewId: aiViewId}).then(function (r) {
            if (loading.parentNode) loading.parentNode.removeChild(loading);
            var d = parse(r);
            if (!d || !d.success) {
                var err = document.createElement('div');
                err.className = 'affected-items-empty';
                err.textContent = (d && d.error) || 'Could not load affected items.';
                listEl.appendChild(err); return;
            }
            var items = Array.isArray(d.items) ? d.items : [];
            affectedItemsCache[String(wsId) + ':' + String(itemId)] = items;
            countEl.textContent = items.length + ' ' + (items.length === 1 ? 'item' : 'items');
            if (!items.length) {
                var expected = 0;
                (currentItemTabs || []).forEach(function (t) { if (t && t.name === 'LINKEDITEMS') expected = t.totalCount || 0; });
                var empty = document.createElement('div');
                empty.className = 'affected-items-empty';
                if (expected > 0) {
                    empty.textContent = 'Tabs API reported ' + expected + ' affected item(s) but the list endpoint returned none. totalCount=' + d.totalCount;
                } else {
                    empty.textContent = 'No affected items on this record yet.';
                }
                listEl.appendChild(empty); return;
            }
            items.forEach(function (ai) {
                try { listEl.appendChild(buildAffectedItemCard(ai, wsId, itemId, aiViewId)); }
                catch (cardErr) {
                    var fb = document.createElement('div');
                    fb.className = 'affected-items-empty';
                    fb.textContent = 'Render error: ' + (cardErr && cardErr.message ? cardErr.message : cardErr);
                    listEl.appendChild(fb);
                }
            });
        }).catch(function (e) {
            if (loading.parentNode) loading.parentNode.removeChild(loading);
            var err2 = document.createElement('div');
            err2.className = 'affected-items-empty';
            err2.textContent = 'Load error: ' + e;
            listEl.appendChild(err2);
        });
    }

    function buildAffectedItemCard(ai, parentWsId, parentItemId, parentViewId) {
        var card = document.createElement('div');
        card.className = 'affected-item';
        var header = document.createElement('div');
        header.className = 'affected-item-header';
        var itemLink  = (ai && ai.item && ai.item.link) || '';
        var linkedWsId  = extractWorkspaceIdFromLink(itemLink);
        var linkedItemId = extractItemIdFromLink(itemLink);
        if (isCadWorkspace(linkedWsId) && linkedItemId) {
            header.appendChild(buildOpenInFusionIcon(linkedWsId, linkedItemId));
        }
        var title = document.createElement('a');
        title.className = 'affected-item-title';
        title.textContent = (ai.item && ai.item.title) || 'Item';
        title.title = (ai.item && ai.item.title) || '';
        if (linkedWsId && linkedItemId) {
            (function (wid, id, lbl) {
                title.onclick = function () { openDetailForWs(id, wid, lbl); };
            })(linkedWsId, linkedItemId, title.textContent);
        }
        header.appendChild(title);
        var pill = document.createElement('span');
        pill.className = 'revision-pill';
        var fr = ai.fromRelease || '', to = ai.toRelease || '';
        if (fr && to && fr !== to) {
            pill.innerHTML = escHtml(fr) + '<span class="rev-arrow">→</span>' + escHtml(to);
        } else if (to) {
            pill.textContent = 'REV ' + to;
        } else if (ai.item && ai.item.version) {
            pill.textContent = String(ai.item.version).replace(/[\[\]]/g, '');
        } else {
            pill.style.display = 'none';
        }
        header.appendChild(pill);
        if (hasPerm('delete_workflow_items')) {
            var btnRemove = document.createElement('button');
            btnRemove.type = 'button';
            btnRemove.className = 'affected-item-remove';
            btnRemove.textContent = 'Remove';
            (function (btn, pWsId, pItemId, pViewId) {
                btn.onclick = function () {
                    var aiId = extractAffectedItemId(ai);
                    if (!aiId) return;
                    btn.disabled = true;
                    send('removeAffectedItem', {workspaceId: pWsId, itemId: pItemId, affectedItemId: aiId, viewId: pViewId || 11}).then(function (r) {
                        var d = parse(r);
                        btn.disabled = false;
                        if (!d || !d.success) { showBanner((d && d.error) || 'Could not remove.', 'error'); return; }
                        delete affectedItemsCache[String(pWsId) + ':' + String(pItemId)];
                        renderAffectedItemsTab();
                    }).catch(function () { btn.disabled = false; });
                };
            })(btnRemove, parentWsId, parentItemId, parentViewId);
            header.appendChild(btnRemove);
        }
        card.appendChild(header);
        var fields = Array.isArray(ai.linkedFields) ? ai.linkedFields : [];
        if (fields.length) {
            var fieldsEl = document.createElement('div');
            fieldsEl.className = 'affected-item-fields';
            fields.forEach(function (f) {
                var row = document.createElement('div');
                row.className = 'affected-item-field';
                var lbl2 = document.createElement('div');
                lbl2.className = 'field-label';
                lbl2.textContent = f.title || '';
                var val2 = document.createElement('div');
                val2.className = 'field-value';
                renderDetailFieldValue(val2, f.value, f);
                row.appendChild(lbl2);
                row.appendChild(val2);
                fieldsEl.appendChild(row);
            });
            card.appendChild(fieldsEl);
        }
        return card;
    }

    function extractAffectedItemId(ai) {
        var self = (ai && ai.__self__) || '';
        var m = self.match(/affected-items\/(\d+)/);
        return m ? m[1] : null;
    }

    function openAffectedItemsPicker(wsId, itemId) {
        pickerTarget = 'affectedItems';
        pickerAffectedItemsCtx = {wsId: String(wsId), itemId: String(itemId)};
        pickerMode = 'components';
        pickerTargetFieldId = null;
        _pickerOnAdded = null;
        pickerActiveTab = 'browse';
        pickerSelectedComponents = [];
        pickerSelectedFmItems = [];
        _resetPickerUI();
        ensurePickerModeSwitch();
        updatePickerModeSwitchUI();
        updatePickerHeader();
        setPickerTab('browse');
        var ov = document.getElementById('pickerOverlay');
        if (ov) ov.classList.add('visible');
    }

    // -------------------------------------------------------------------------
    // WORKFLOW ACTIONS
    // -------------------------------------------------------------------------
    function renderWorkflowActions() {
        var section = document.getElementById('workflowSection');
        var btnsEl  = document.getElementById('transitionBtns');
        if (!section || !btnsEl) return;
        btnsEl.innerHTML = '';
        if (!currentTransitions || !currentTransitions.length) { section.style.display = 'none'; return; }
        section.style.display = 'block';
        currentTransitions.forEach(function (t) {
            var btn = document.createElement('button');
            btn.className = 'transition-btn';
            btn.textContent = t.shortName || t.description || 'Transition';
            (function (tid) { btn.onclick = function () { executeTransition(tid); }; })(t.transitionID);
            btnsEl.appendChild(btn);
        });
    }

    function executeTransition(transitionId) {
        var commentEl = document.getElementById('workflowComment');
        var comment = (commentEl && commentEl.value || '').trim();
        hideBanner();
        send('runWorkflowTransition', {
            workspaceId: currentWorkspaceId, itemId: currentItemId,
            transitionId: transitionId, currentStep: currentStep, workflowComments: comment
        }).then(function (r) {
            var d = parse(r);
            if (!d.success) { showBanner(d.error || 'Transition failed.', 'error'); return; }
            if (commentEl) commentEl.value = '';
            showBanner('Workflow updated successfully.', 'success');
            send(cfg.detailAction, {itemId: currentItemId, workspaceId: currentWorkspaceId, skipCache: true}).then(function (r2) {
                var d2 = parse(r2);
                if (d2.success) {
                    currentItem        = d2.item;
                    currentTransitions = d2.transitions || [];
                    currentStep        = d2.currentStep || 0;
                    renderDetailView(d2.item);
                    renderWorkflowActions();
                }
            }).catch(function () {});
        }).catch(function (e) { showBanner('Error: ' + e, 'error'); });
    }

    function showBanner(msg, type) {
        var el = document.getElementById('detailBanner');
        if (!el) return;
        el.textContent = msg;
        el.className = 'detail-banner ' + (type || 'info');
        el.style.display = 'block';
    }

    function hideBanner() {
        var el = document.getElementById('detailBanner');
        if (el) { el.style.display = 'none'; el.textContent = ''; }
    }

    // -------------------------------------------------------------------------
    // CREATE / EDIT FORM
    // -------------------------------------------------------------------------
    function openCreateForm() {
        formMode = 'create';
        detailStack = [
            {label: cfg.title, view: 'list', tableauId: currentTableauId, page: currentPage},
            {label: cfg.newItemLabel, view: 'form'}
        ];
        showView('form');
        renderFormBreadcrumb();
        var ftEl = document.getElementById('formTitle');
        if (ftEl) ftEl.textContent = cfg.newItemLabel;
        loadAndRenderForm(null);
    }

    function openEditForm(itemId, wsId) {
        formMode = 'edit';
        if (!detailStack.some(function (e) { return e.view === 'list'; })) {
            detailStack.unshift({label: cfg.title, view: 'list', tableauId: currentTableauId, page: currentPage});
        }
        var desc = currentItem ? getItemDescriptor(currentItem) : ('Item ' + itemId);
        if (!detailStack.some(function (e) { return e.view === 'detail' && e.itemId === itemId; })) {
            detailStack.push({label: desc, view: 'detail', itemId: itemId, wsId: wsId || cfg.workspaceIdDefault});
        }
        detailStack.push({label: 'Edit', view: 'form'});
        showView('form');
        renderFormBreadcrumb();
        var ftEl = document.getElementById('formTitle');
        if (ftEl) ftEl.textContent = 'Edit: ' + desc;
        loadAndRenderForm(itemId);
    }

    function renderFormBreadcrumb() {
        var el = document.getElementById('formBreadcrumb');
        if (!el) return;
        el.innerHTML = '';
        detailStack.forEach(function (entry, idx) {
            if (idx > 0) {
                var sep = document.createElement('span');
                sep.className = 'breadcrumb-sep'; sep.textContent = '›'; el.appendChild(sep);
            }
            var span = document.createElement('span');
            span.className = 'breadcrumb-item' + (idx === detailStack.length - 1 ? ' current' : '');
            span.textContent = entry.label;
            if (idx < detailStack.length - 1) {
                (function (e) { span.onclick = function () { cancelForm(e); }; })(entry);
            }
            el.appendChild(span);
        });
    }

    function cancelForm(backEntry) {
        detailStack.pop();
        if (backEntry && backEntry.view === 'list') {
            detailStack = [];
            if (_isUnifiedScope()) {
                showView('list');
                renderUnifiedTable(_listLoadSeq);
                renderPagination();
            } else {
                loadListView();
            }
        } else if (backEntry && backEntry.view === 'detail') {
            detailStack.pop();
            openDetail(backEntry.itemId, backEntry.wsId, backEntry.label);
        } else {
            detailStack = [];
            if (_isUnifiedScope()) {
                showView('list');
                renderUnifiedTable(_listLoadSeq);
                renderPagination();
            } else {
                loadListView();
            }
        }
    }

    function loadAndRenderForm(editItemId) {
        setLoading('formLoading', true);
        setMsg('formMsg', '', '');
        var fsEl = document.getElementById('formSections');
        if (fsEl) fsEl.innerHTML = '';
        var ffEl = document.getElementById('formFooter');
        if (ffEl) ffEl.style.display = 'none';
        savedFieldElems = {};
        formValues = {};
        _originalFormValues = {};

        // When in a drilled-down scope, pass workspaceId so the Python actions
        // target the correct workspace (not the default WS_CHANGE_ORDERS).
        var wsPayload = _scopedWsPayload();
        var fieldsPromise = send('getViewFields', Object.assign({viewId: _detectedViewId || 1}, wsPayload)).then(parse);
        var contextPromise;
        if (editItemId && currentItem) {
            contextPromise = Promise.resolve({sections: _sectionsFromItem(currentItem), item: currentItem});
        } else if (editItemId) {
            contextPromise = Promise.all([
                send('getWorkspaceSections', wsPayload).then(parse),
                send(cfg.detailAction, Object.assign({itemId: editItemId, workspaceId: currentWorkspaceId || cfg.workspaceIdDefault}, wsPayload)).then(parse)
            ]).then(function (results) {
                var secRes = results[0], detRes = results[1];
                var item = (detRes && detRes.success) ? detRes.item : null;
                var sections = item ? _sectionsFromItem(item)
                    : ((secRes && secRes.success) ? (secRes.sections || []) : []);
                return {sections: sections, item: item, error: (!sections.length && secRes && !secRes.success) ? secRes.error : null};
            });
        } else {
            contextPromise = send('getWorkspaceSections', wsPayload).then(parse).then(function (secRes) {
                if (!secRes.success) return {wsSections: [], item: null, error: secRes.error};
                if (secRes.viewId) _detectedViewId = secRes.viewId;
                return {wsSections: secRes.sections || [], item: null, error: null};
            });
        }

        Promise.all([contextPromise, fieldsPromise]).then(function (results) {
            setLoading('formLoading', false);
            var ctx = results[0], fieldRes = results[1];
            if (!fieldRes.success) {
                setMsg('formMsg', 'Warning: could not load field definitions (' + (fieldRes.error || 'unknown') + '). Fields may render with limited metadata.', 'warn');
            }
            buildFieldMaps(fieldRes.fields || []);
            formFieldDefs = fieldDefMap;
            if (formMode === 'create') {
                if (ctx.error && !(ctx.wsSections && ctx.wsSections.length)) {
                    setMsg('formMsg', ctx.error || 'Failed to load form structure.', 'error'); return;
                }
                formSections = _buildCreateFormSections(ctx.wsSections || [], formFieldDefs);
            } else {
                formSections = ctx.sections || [];
            }
            if (ctx.item) { currentItem = ctx.item; extractFormValuesFromItem(ctx.item); }
            else if (editItemId && currentItem) { extractFormValuesFromItem(currentItem); }
            renderForm();
            if (ffEl) ffEl.style.display = 'flex';
        }).catch(function (e) {
            setLoading('formLoading', false);
            setMsg('formMsg', 'Load error: ' + e, 'error');
        });
    }

    function extractFormValuesFromItem(item) {
        formValues = {};
        (item.sections || []).forEach(function (sec) {
            (sec.fields || []).forEach(function (f) {
                var selfRef = f.__self__ || f.link || '';
                var m = selfRef.match(/fields\/([^/?]+)/);
                var fid = m ? m[1] : '';
                if (fid) formValues[fid] = f.value;
            });
        });
        try { _originalFormValues = JSON.parse(JSON.stringify(formValues)); } catch (e) { _originalFormValues = {}; }
    }

    function _sectionsFromItem(item) {
        return (item.sections || []).map(function (sec) {
            var secSelf = sec.__self__ || sec.link || '';
            var fields = (sec.fields || []).map(function (f) {
                var selfRef = f.__self__ || f.link || '';
                var m = selfRef.match(/fields\/([^/?]+)/);
                var fid = m ? m[1] : '';
                return {id: fid, title: fid, __self__: selfRef};
            }).filter(function (f) { return !!f.id; });
            return {name: sec.name || '', link: secSelf, fields: fields};
        });
    }

    function _buildCreateFormSections(wsSections, fieldDefs) {
        var allFields = Object.keys(fieldDefs).map(function (id) { return fieldDefs[id]; })
            .filter(function (d) { return d.id && !_fieldIsHidden(d); })
            .sort(function (a, b) { return (a.displayOrder || 0) - (b.displayOrder || 0); });
        if (!wsSections.length) {
            return [{name: 'Details', link: '', fields: allFields.map(function (d) { return {id: d.id, link: ''}; })}];
        }
        var fieldToSecIdx = {};
        wsSections.forEach(function (sec, idx) {
            (sec.fields || []).forEach(function (sf) { if (sf.id) fieldToSecIdx[sf.id] = idx; });
        });
        var buckets = wsSections.map(function (sec) { return {name: sec.name || 'Section', link: sec.link || '', fields: []}; });
        var unassigned = [];
        allFields.forEach(function (d) {
            var idx = fieldToSecIdx[d.id];
            if (idx !== undefined && buckets[idx]) {
                var wsField = (wsSections[idx].fields || []).filter(function (sf) { return sf.id === d.id; })[0];
                buckets[idx].fields.push({id: d.id, link: wsField ? (wsField.link || '') : ''});
            } else {
                unassigned.push({id: d.id, link: ''});
            }
        });
        if (unassigned.length) buckets[0].fields = unassigned.concat(buckets[0].fields);
        return buckets.filter(function (b) { return b.fields.length > 0; });
    }

    function renderForm() {
        var container = document.getElementById('formSections');
        if (!container) return;
        container.innerHTML = '';
        if (!formSections.length) {
            container.innerHTML = '<p style="color:var(--ink-muted);padding:8px 0;font-size:var(--fs-sm);">No form sections returned. Check workspace configuration in Fusion Manage.</p>';
            return;
        }
        formSections.forEach(function (sec) {
            if (!sec.fields || !sec.fields.length) return;
            var hasVisible = sec.fields.some(function (sf) {
                if (!sf.id) return false;
                var def = formFieldDefs[sf.id];
                return !def || !_fieldIsHidden(def);
            });
            if (!hasVisible) return;
            var block = document.createElement('div');
            block.className = 'form-section-block';
            var titleEl = document.createElement('div');
            titleEl.className = 'form-section-title';
            titleEl.textContent = sec.name || 'Section';
            block.appendChild(titleEl);
            sec.fields.forEach(function (sf) {
                if (!sf.id) return;
                var def = formFieldDefs[sf.id];
                if (def && _fieldIsHidden(def)) return;
                if (!def) def = {id: sf.id, title: sf.title || sf.id, type: {title: 'Single Line Text'}, editability: 'ALWAYS', visibility: true, picklist: null, fieldValidators: []};
                var fieldEl = buildFormField(sf.id, def);
                if (fieldEl) block.appendChild(fieldEl);
            });
            container.appendChild(block);
        });
    }

    function buildFormField(fieldId, def) {
        var typeTitle   = (def && def.type && def.type.title) || 'Single Line Text';
        var editability = def.editability || 'ALWAYS';
        var isReadOnly  = editability === 'NEVER' || ((editability === 'ON_CREATION' || editability === 'CREATE_ONLY') && formMode === 'edit');
        var wrap = document.createElement('div');
        wrap.className = 'form-field';
        var isReq = (def.fieldValidators || []).some(function (v) { return v.validatorName === 'required'; });
        var lbl = document.createElement('label');
        lbl.className = 'form-label';
        lbl.textContent = def.title || fieldId;
        if (isReq) { var req = document.createElement('span'); req.className = 'field-required'; req.textContent = ' *'; lbl.appendChild(req); }
        wrap.appendChild(lbl);
        var currentVal = formValues[fieldId] !== undefined ? formValues[fieldId] : null;
        if (isReadOnly) {
            var ro = document.createElement('div');
            ro.className = 'form-readonly';
            renderDetailFieldValue(ro, currentVal, def);
            wrap.appendChild(ro);
            return wrap;
        }
        if (typeof cfg.hooks.buildCustomFormField === 'function') {
            var customEl = cfg.hooks.buildCustomFormField(fieldId, def, currentVal);
            if (customEl) { wrap.appendChild(customEl); return wrap; }
        }
        var input = null;
        if (fieldId === 'SHARED_ITEMS' || typeTitle === 'Multiple Selection') {
            input = buildMultiSelectField(fieldId, def, currentVal);
        } else if (typeTitle === 'Single Selection' || typeTitle === 'Radio Button') {
            input = buildSingleSelectField(fieldId, def, currentVal);
        } else if (typeTitle === 'Paragraph') {
            input = document.createElement('textarea');
            input.className = 'form-textarea form-input';
            input.value = typeof currentVal === 'string' ? currentVal : '';
            (function (fid, el) { el.oninput = function () { formValues[fid] = el.value || null; }; })(fieldId, input);
        } else if (typeTitle === 'Date') {
            input = document.createElement('input');
            input.type = 'date'; input.className = 'form-input';
            if (currentVal) input.value = currentVal.substring(0, 10);
            (function (fid, el) { el.onchange = function () { formValues[fid] = el.value || null; }; })(fieldId, input);
        } else if (typeTitle === 'Integer') {
            input = document.createElement('input');
            input.type = 'number'; input.step = '1'; input.className = 'form-input';
            input.value = currentVal !== null && currentVal !== undefined ? currentVal : '';
            (function (fid, el) { el.oninput = function () { formValues[fid] = el.value !== '' ? parseInt(el.value, 10) : null; }; })(fieldId, input);
        } else {
            input = document.createElement('input');
            input.type = 'text'; input.className = 'form-input';
            input.value = typeof currentVal === 'string' ? currentVal : (currentVal !== null && currentVal !== undefined ? String(currentVal) : '');
            (function (fid, el) { el.oninput = function () { formValues[fid] = el.value || null; }; })(fieldId, input);
        }
        if (input) { wrap.appendChild(input); savedFieldElems[fieldId] = input; }
        return wrap;
    }

    function buildSingleSelectField(fieldId, def, currentVal) {
        var wrap = document.createElement('div');
        var sel = document.createElement('select');
        sel.className = 'form-select';
        var ph = document.createElement('option'); ph.value = ''; ph.textContent = 'Loading options…'; ph.disabled = true;
        sel.appendChild(ph); sel.value = '';
        wrap.appendChild(sel);
        if (currentVal && typeof currentVal === 'object') {
            var cur = document.createElement('option');
            cur.value = currentVal.value || currentVal.link || '';
            cur.textContent = currentVal.title || currentVal.label || '';
            cur.selected = true; sel.insertBefore(cur, sel.firstChild);
            formValues[fieldId] = currentVal;
        }
        sel.onchange = function () {
            var opt = sel.options[sel.selectedIndex];
            formValues[fieldId] = sel.value ? {value: sel.value, link: sel.value, label: opt.textContent, title: opt.textContent} : null;
        };
        var loaded = false;
        sel.onfocus = function () {
            if (loaded || !def.picklist) return; loaded = true;
            loadPicklist(def.picklist, function (items) {
                sel.innerHTML = '';
                var empty = document.createElement('option'); empty.value = ''; empty.textContent = '— Select —'; sel.appendChild(empty);
                items.forEach(function (it) {
                    if (it.deleted) return;
                    var opt2 = document.createElement('option');
                    opt2.value = it.link || it.urn || '';
                    opt2.textContent = it.title + (it.version ? ' ' + it.version : '');
                    sel.appendChild(opt2);
                });
                if (currentVal && (currentVal.value || currentVal.link)) sel.value = currentVal.value || currentVal.link;
            });
        };
        return wrap;
    }

    function buildMultiSelectField(fieldId, def, currentVal) {
        var isSharedItems = fieldId === 'SHARED_ITEMS';
        var wrap = document.createElement('div');
        var msWrap = document.createElement('div');
        msWrap.className = 'multiselect-wrap';
        var tagsEl = document.createElement('div');
        tagsEl.className = 'multiselect-tags';
        msWrap.appendChild(tagsEl);
        var currentItems = Array.isArray(currentVal) ? currentVal.filter(function (v) { return v && typeof v === 'object'; }) : [];
        formValues[fieldId] = currentItems.slice();
        function renderTags() {
            tagsEl.innerHTML = '';
            (formValues[fieldId] || []).forEach(function (item, idx) {
                var tag = document.createElement('span');
                tag.className = 'ms-tag';
                tag.textContent = item.title || item.label || item.value || 'Item';
                var rm = document.createElement('span');
                rm.className = 'ms-tag-remove'; rm.textContent = '×';
                (function (i) {
                    rm.onclick = function () { formValues[fieldId].splice(i, 1); renderTags(); };
                })(idx);
                tag.appendChild(rm); tagsEl.appendChild(tag);
            });
        }
        renderTags();
        if (def.picklist) {
            var addRow = document.createElement('div');
            addRow.className = 'multiselect-add';
            var addSel = document.createElement('select');
            addSel.className = 'form-select'; addSel.style.fontSize = '11px';
            var addOpt = document.createElement('option'); addOpt.value = ''; addOpt.textContent = 'Add from list…'; addSel.appendChild(addOpt);
            addRow.appendChild(addSel);
            var addBtn = document.createElement('button');
            addBtn.type = 'button'; addBtn.className = 'btn btn-sm'; addBtn.textContent = 'Add';
            addBtn.onclick = function () {
                if (!addSel.value) return;
                var opt = addSel.options[addSel.selectedIndex];
                var newItem = {value: addSel.value, link: addSel.value, label: opt.textContent, title: opt.textContent};
                var already = (formValues[fieldId] || []).some(function (x) { return x.link === newItem.link || x.value === newItem.value; });
                if (!already) { formValues[fieldId] = (formValues[fieldId] || []).concat([newItem]); renderTags(); }
                addSel.value = '';
            };
            addRow.appendChild(addBtn);
            msWrap.appendChild(addRow);
            var loaded = false;
            addSel.onfocus = function () {
                if (loaded) return; loaded = true;
                loadPicklist(def.picklist, function (items) {
                    addSel.innerHTML = '';
                    var p = document.createElement('option'); p.value=''; p.textContent='Add from list…'; addSel.appendChild(p);
                    items.forEach(function (it) {
                        if (it.deleted) return;
                        var opt2 = document.createElement('option');
                        opt2.value = it.link || it.urn || '';
                        opt2.textContent = it.title + (it.version ? ' ' + it.version : '');
                        addSel.appendChild(opt2);
                    });
                });
            };
        }
        if (isSharedItems) {
            var fusionRow = document.createElement('div');
            fusionRow.style.cssText = 'display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;';
            var btnSel = document.createElement('button');
            btnSel.type = 'button'; btnSel.className = 'btn btn-sm'; btnSel.textContent = 'Add from Active Selection';
            (function (fid, rt) { btnSel.onclick = function () { openComponentPicker(fid, 'selection', rt); }; })(fieldId, renderTags);
            fusionRow.appendChild(btnSel);
            var btnBrowse = document.createElement('button');
            btnBrowse.type = 'button'; btnBrowse.className = 'btn btn-sm'; btnBrowse.textContent = 'Browse Fusion Components';
            (function (fid, rt) { btnBrowse.onclick = function () { openComponentPicker(fid, 'browse', rt); }; })(fieldId, renderTags);
            fusionRow.appendChild(btnBrowse);
            msWrap.appendChild(fusionRow);
        }
        wrap.appendChild(msWrap);
        return wrap;
    }

    function loadPicklist(path, callback) {
        if (lookupCache[path]) { callback(lookupCache[path]); return; }
        send('getLookupOptions', {lookupPath: path, limit: 200, offset: 0}).then(function (r) {
            var d = parse(r);
            if (d.success) { lookupCache[path] = d.items || []; callback(lookupCache[path]); }
        }).catch(function () {});
    }

    // -------------------------------------------------------------------------
    // FORM SAVE
    // -------------------------------------------------------------------------
    function saveForm() {
        var btnSave = document.getElementById('btnSave');
        var msgEl   = document.getElementById('formSaveMsg');
        // Required-field validation
        var missingLabels = [];
        formSections.forEach(function (sec) {
            (sec.fields || []).forEach(function (sf) {
                if (!sf.id) return;
                var def = formFieldDefs[sf.id];
                if (!def) return;
                var isReq = (def.fieldValidators || []).some(function (v) { return v.validatorName === 'required'; });
                if (!isReq) return;
                var val = formValues[sf.id];
                if (val === null || val === undefined || val === '' || (Array.isArray(val) && !val.length)) {
                    missingLabels.push(def.title || sf.id);
                }
            });
        });
        if (missingLabels.length) {
            if (msgEl) { msgEl.textContent = 'Required: ' + missingLabels.join(', '); msgEl.style.color = '#c62828'; }
            return;
        }
        if (btnSave) btnSave.disabled = true;
        if (msgEl) { msgEl.textContent = 'Saving…'; msgEl.style.color = '#666'; }
        var viewId = _detectedViewId || 1;
        var wsBase      = '/api/v3/workspaces/' + currentWorkspaceId;
        var itemViewBase = wsBase + '/items/' + currentItemId + '/views/' + viewId;
        var sections = formSections.map(function (sec) {
            var secLink = sec.link || '';
            if (formMode === 'edit' && secLink && secLink.indexOf('/items/') === -1) {
                var secIdM = secLink.match(/sections\/(\d+)/);
                secLink = secIdM ? (itemViewBase + '/sections/' + secIdM[1]) : '';
            }
            var fields = (sec.fields || []).map(function (sf) {
                var def = formFieldDefs[sf.id];
                if (!def) return null;
                if (_fieldIsHidden(def)) return null;
                var ed = def.editability || 'ALWAYS';
                if (ed === 'NEVER') return null;
                if ((ed === 'ON_CREATION' || ed === 'CREATE_ONLY') && formMode === 'edit') return null;
                var fieldSelf;
                if (formMode === 'create') {
                    fieldSelf = sf.link || def.__self__ || '';
                } else {
                    fieldSelf = (sf.link && sf.link.indexOf('/items/') !== -1) ? sf.link : (itemViewBase + '/fields/' + sf.id);
                }
                var val = formValues[sf.id] !== undefined ? formValues[sf.id] : null;
                if (formMode === 'edit') {
                    var origVal = _originalFormValues[sf.id] !== undefined ? _originalFormValues[sf.id] : null;
                    var isNull  = val === null || val === undefined || (Array.isArray(val) && !val.length);
                    var wasNull = origVal === null || origVal === undefined || (Array.isArray(origVal) && !origVal.length);
                    if (isNull && wasNull) return null;
                }
                if (val && typeof val === 'object' && val.isDraft && val.dataUrl) val = val.dataUrl;
                return {'__self__': fieldSelf, 'title': def.title || sf.id, 'value': val};
            }).filter(Boolean);
            if (!fields.length) return null;
            var sec_out = {fields: fields};
            if (secLink) sec_out.link = secLink;
            return sec_out;
        }).filter(Boolean).filter(function (s) { return s.fields.length > 0; });

        if (!sections.length) {
            if (btnSave) btnSave.disabled = false;
            if (msgEl) { msgEl.textContent = 'Nothing to save (all fields are read-only or unchanged).'; msgEl.style.color = '#c62828'; }
            return;
        }
        var action, payload;
        var wsPayload = _scopedWsPayload();
        if (formMode === 'create') {
            action = 'createItem';
            payload = Object.assign({sections: sections}, wsPayload);
        } else {
            action = 'updateItem';
            payload = Object.assign({itemId: currentItemId, sections: sections, etag: currentItemETag}, wsPayload);
        }
        send(action, payload).then(function (r) {
            if (btnSave) btnSave.disabled = false;
            var d = parse(r);
            if (!d.success) {
                if (msgEl) { msgEl.textContent = d.error || 'Save failed.'; msgEl.style.color = '#c62828'; }
                return;
            }
            if (msgEl) msgEl.textContent = '';
            // For create: use the new item's ID returned by the server.
            // For edit: server returns no itemId, fall back to currentItemId.
            var newItemId = formMode === 'create' ? d.itemId : (d.itemId || currentItemId);
            if (formMode === 'create' && !newItemId) {
                if (msgEl) { msgEl.textContent = 'Item created but the server did not return an ID. Refresh the list to see it.'; msgEl.style.color = '#c62828'; }
                if (btnSave) btnSave.disabled = false;
                return;
            }
            if (typeof cfg.hooks.afterSave === 'function') {
                cfg.hooks.afterSave(newItemId, currentWorkspaceId || cfg.workspaceIdDefault);
            } else {
                detailStack = [{label: cfg.title, view: 'list', tableauId: currentTableauId, page: currentPage}];
                openDetail(newItemId, currentWorkspaceId || cfg.workspaceIdDefault, null);
            }
        }).catch(function (e) {
            if (btnSave) btnSave.disabled = false;
            if (msgEl) { msgEl.textContent = 'Error: ' + e; msgEl.style.color = '#c62828'; }
        });
    }

    // -------------------------------------------------------------------------
    // COMPONENT PICKER
    // -------------------------------------------------------------------------
    function openComponentPicker(fieldId, tab, onAdded) {
        pickerTargetFieldId = fieldId;
        pickerTarget = 'field';
        pickerAffectedItemsCtx = null;
        pickerMode = 'components';
        pickerActiveTab = tab;
        pickerSelectedComponents = [];
        pickerSelectedFmItems = [];
        _pickerOnAdded = onAdded;
        _resetPickerUI();
        ensurePickerModeSwitch();
        updatePickerModeSwitchUI();
        updatePickerHeader();
        setPickerTab(tab);
        var ov = document.getElementById('pickerOverlay');
        if (ov) ov.classList.add('visible');
    }

    function closeComponentPicker() {
        var ov = document.getElementById('pickerOverlay');
        if (ov) ov.classList.remove('visible');
        pickerSelectedComponents = [];
        pickerSelectedFmItems = [];
        _pickerOnAdded = null;
    }

    function _resetPickerUI() {
        var cl = document.getElementById('componentList');
        var fl = document.getElementById('fmResultList');
        var ps2 = document.getElementById('pickerStep2');
        var plm = document.getElementById('pickerLoadMsg');
        var psm = document.getElementById('pickerSearchMsg');
        var bsfm = document.getElementById('btnSearchFM');
        var bats = document.getElementById('btnAddToShared');
        var baal = document.getElementById('btnAddAllToShared');
        if (cl) cl.innerHTML = '';
        if (fl) fl.innerHTML = '';
        if (ps2) ps2.style.display = 'none';
        if (plm) plm.textContent = '';
        if (psm) psm.textContent = '';
        if (bsfm) bsfm.disabled = true;
        if (bats) bats.disabled = true;
        if (baal) baal.disabled = true;
    }

    function ensurePickerModeSwitch() {
        var btnLoad = document.getElementById('btnLoadComponents');
        if (btnLoad) btnLoad.style.display = 'none';
        if (document.querySelector('.picker-mode-switch')) return;
        var tabs = document.querySelector('.picker-tabs');
        if (!tabs || !tabs.parentNode) return;
        var sw = document.createElement('div');
        sw.className = 'picker-mode-switch';
        sw.innerHTML = '<span class="picker-mode-label">Type</span>'
            + '<div class="plm-segmented" data-picker-mode>'
            + '<button type="button" data-mode="components" class="active">Components</button>'
            + '<button type="button" data-mode="drawings">Drawings</button>'
            + '</div>';
        tabs.parentNode.insertBefore(sw, tabs);
        sw.querySelector('[data-picker-mode]').addEventListener('click', function (e) {
            var btn = e.target && e.target.closest ? e.target.closest('button[data-mode]') : null;
            if (!btn) {
                // closest not available in older webviews — walk up manually
                var t = e.target;
                while (t && t !== sw && !t.getAttribute('data-mode')) { t = t.parentNode; }
                btn = t && t.getAttribute('data-mode') ? t : null;
            }
            if (!btn) return;
            setPickerMode(btn.getAttribute('data-mode'));
        });
    }

    function updatePickerModeSwitchUI() {
        var sw = document.querySelector('.picker-mode-switch');
        if (!sw) return;
        sw.querySelectorAll('button[data-mode]').forEach(function (b) {
            b.classList.toggle('active', b.getAttribute('data-mode') === pickerMode);
        });
        var tabSel   = document.getElementById('tabSelection');
        var tabBrowse = document.getElementById('tabBrowse');
        if (tabSel)    tabSel.style.display = pickerMode === 'drawings' ? 'none' : '';
        if (tabBrowse) tabBrowse.textContent = pickerMode === 'drawings' ? 'Open Drawings' : 'Browse Components';
    }

    function setPickerMode(mode) {
        if (pickerMode === mode) return;
        pickerMode = mode;
        updatePickerModeSwitchUI();
        updatePickerHeader();
        var psm = document.getElementById('pickerSearchMsg');
        if (psm) psm.textContent = '';
        var tab = (mode === 'drawings') ? 'browse' : (pickerActiveTab || 'browse');
        setPickerTab(tab);
    }

    function updatePickerHeader() {
        var h = document.getElementById('pickerHeaderTitle');
        if (!h) h = document.querySelector('.picker-header h3');
        if (!h) return;
        if (pickerTarget === 'affectedItems') {
            h.textContent = pickerMode === 'drawings' ? 'Add Drawings as Affected Items' : 'Add Components as Affected Items';
        } else {
            h.textContent = pickerMode === 'drawings' ? 'Add Fusion Drawing' : 'Add Fusion Component to Shared Items';
        }
    }

    function setPickerTab(tab) {
        pickerActiveTab = tab;
        var tabSel   = document.getElementById('tabSelection');
        var tabBrowse = document.getElementById('tabBrowse');
        if (tabSel)    tabSel.classList.toggle('active', tab === 'selection');
        if (tabBrowse) tabBrowse.classList.toggle('active', tab === 'browse');
        var lbl = document.getElementById('pickerStep1Label');
        if (lbl) {
            if (pickerMode === 'drawings') lbl.textContent = 'Open drawings in this Fusion session:';
            else if (tab === 'selection')  lbl.textContent = 'Components currently selected in Fusion:';
            else                           lbl.textContent = 'All components in the active design:';
        }
        _resetPickerUI();
        pickerSelectedComponents = [];
        pickerSelectedFmItems = [];
        loadPickerComponents();
    }

    function loadPickerComponents() {
        var action, emptyMsg;
        if (pickerMode === 'drawings') {
            action = 'getOpenDrawings';
            emptyMsg = 'No drawings are currently open in this Fusion session.';
        } else if (pickerActiveTab === 'selection') {
            action = 'getSelectedComponents';
            emptyMsg = 'No components selected. Select items in the Fusion model tree or canvas first.';
        } else {
            action = 'getRootComponents';
            emptyMsg = 'No components found in the active document.';
        }
        var plm = document.getElementById('pickerLoadMsg');
        if (plm) plm.textContent = 'Loading…';
        pickerSelectedComponents = [];
        pickerSelectedFmItems = [];
        send(action, {}).then(function (r) {
            var d = parse(r);
            if (plm) plm.textContent = '';
            if (!d.success) { if (plm) plm.textContent = d.error || 'Failed to load.'; return; }
            var items = d.components || d.drawings || [];
            if (!items.length) { if (plm) plm.textContent = emptyMsg; return; }
            renderPickerComponentList(items);
        }).catch(function (e) { if (plm) plm.textContent = 'Error: ' + e; });
    }

    function renderPickerComponentList(components) {
        var list = document.getElementById('componentList');
        if (!list) return;
        list.innerHTML = '';
        pickerSelectedComponents = [];
        components.forEach(function (comp) {
            var li = document.createElement('li');
            var hasId = !!comp.fileId;
            li.className = hasId ? 'picker-info-row' : 'picker-info-row disabled';
            var name = document.createElement('span');
            name.textContent = comp.name || 'Unnamed';
            li.appendChild(name);
            if (!hasId) {
                var hint = document.createElement('span');
                hint.className = 'picker-no-fileid';
                hint.textContent = '(no Fusion Manage link)';
                li.appendChild(hint);
            } else {
                pickerSelectedComponents.push(comp);
            }
            list.appendChild(li);
        });
        var bsfm = document.getElementById('btnSearchFM');
        if (bsfm) bsfm.disabled = pickerSelectedComponents.length === 0;
    }

    function searchFusionManageForComponents(comps) {
        if (!comps || !comps.length) return;
        var step2  = document.getElementById('pickerStep2');
        var fmList = document.getElementById('fmResultList');
        var searchMsg = document.getElementById('pickerSearchMsg');
        var bats = document.getElementById('btnAddToShared');
        var baal = document.getElementById('btnAddAllToShared');
        if (step2)  step2.style.display = 'block';
        if (fmList) fmList.innerHTML = '';
        if (searchMsg) searchMsg.textContent = 'Searching Fusion Manage…';
        if (bats) bats.disabled = true;
        if (baal) baal.disabled = true;
        pickerSelectedFmItems = [];
        var urns = comps.map(function (c) { return c.fileId; }).filter(Boolean);
        var revSel   = document.getElementById('revisionFilter');
        var revision = revSel ? parseInt(revSel.value, 10) : 1;
        send('searchResultsForLineage', {urns: urns, revision: revision}).then(function (r) {
            var d = parse(r);
            if (searchMsg) searchMsg.textContent = '';
            if (!d.success) { if (searchMsg) searchMsg.textContent = d.error || 'Search failed.'; return; }
            var items = d.items || [];
            if (!items.length) { if (searchMsg) searchMsg.textContent = 'No matching items found in Fusion Manage.'; return; }
            renderFmResultList(items);
        }).catch(function (e) { if (searchMsg) searchMsg.textContent = 'Error: ' + e; });
    }

    function renderFmResultList(items) {
        var fmList = document.getElementById('fmResultList');
        if (!fmList) return;
        fmList.innerHTML = '';
        var baal = document.getElementById('btnAddAllToShared');
        if (baal) baal.disabled = !items.length;
        pickerSelectedFmItems = [];
        var _fmLastAnchor = -1;
        function syncAdd() { var b = document.getElementById('btnAddToShared'); if (b) b.disabled = !pickerSelectedFmItems.length; }
        function setSelected(el, item, selected) {
            if (selected) { if (pickerSelectedFmItems.indexOf(item) === -1) pickerSelectedFmItems.push(item); el.classList.add('selected'); }
            else { var idx = pickerSelectedFmItems.indexOf(item); if (idx >= 0) pickerSelectedFmItems.splice(idx, 1); el.classList.remove('selected'); }
        }
        function clearAll() {
            pickerSelectedFmItems = [];
            fmList.querySelectorAll('li.selected').forEach(function (el) { el.classList.remove('selected'); });
        }
        items.forEach(function (item, index) {
            var li = document.createElement('li');
            li.title = item.link || ''; li.textContent = item.title || ('Item #' + (item.itemId || '?'));
            li._fmItem = item; li._fmIndex = index;
            li.onclick = function (e) {
                var shift = e && e.shiftKey, meta = e && (e.ctrlKey || e.metaKey);
                if (shift && _fmLastAnchor >= 0) {
                    if (!meta) clearAll();
                    var lis = fmList.querySelectorAll('li');
                    var a = Math.min(_fmLastAnchor, index), b = Math.max(_fmLastAnchor, index);
                    for (var i = a; i <= b; i++) { var el = lis[i]; if (el && el._fmItem) setSelected(el, el._fmItem, true); }
                } else if (meta) {
                    setSelected(li, item, pickerSelectedFmItems.indexOf(item) === -1);
                    _fmLastAnchor = index;
                } else {
                    clearAll(); setSelected(li, item, true); _fmLastAnchor = index;
                }
                syncAdd();
            };
            fmList.appendChild(li);
        });
    }

    function _addFmItemsToShared(fmItems) {
        if (!fmItems || !fmItems.length || !pickerTargetFieldId) return;
        var arr = formValues[pickerTargetFieldId];
        if (!Array.isArray(arr)) arr = [];
        var added = false;
        fmItems.forEach(function (item) {
            var ne = {value: item.link, link: item.link, label: item.title, title: item.title, deleted: false};
            if (!arr.some(function (x) { return x.link === ne.link || x.value === ne.value; })) { arr.push(ne); added = true; }
        });
        if (added) { formValues[pickerTargetFieldId] = arr; if (typeof _pickerOnAdded === 'function') _pickerOnAdded(); }
    }

    function addSelectedToSharedItems() {
        if (!pickerSelectedFmItems.length) return;
        if (pickerTarget === 'affectedItems') _submitAffectedItems(pickerSelectedFmItems);
        else if (pickerTargetFieldId) { _addFmItemsToShared(pickerSelectedFmItems); closeComponentPicker(); }
    }

    function addAllToSharedItems() {
        var allItems = [];
        var fmList = document.getElementById('fmResultList');
        if (fmList) fmList.querySelectorAll('li').forEach(function (li) { if (li._fmItem) allItems.push(li._fmItem); });
        if (!allItems.length) return;
        if (pickerTarget === 'affectedItems') _submitAffectedItems(allItems);
        else if (pickerTargetFieldId) { _addFmItemsToShared(allItems); closeComponentPicker(); }
    }

    function _submitAffectedItems(fmItems) {
        var ctx = pickerAffectedItemsCtx;
        if (!ctx || !ctx.itemId) { closeComponentPicker(); return; }
        var paths = (fmItems || []).map(function (i) { return i && i.link; }).filter(Boolean);
        if (!paths.length) { closeComponentPicker(); return; }
        var bats = document.getElementById('btnAddToShared');
        var baal = document.getElementById('btnAddAllToShared');
        if (bats) bats.disabled = true;
        if (baal) baal.disabled = true;
        send('addAffectedItems', {workspaceId: ctx.wsId, itemId: ctx.itemId, paths: paths}).then(function (r) {
            var d = parse(r);
            closeComponentPicker();
            if (!d || !d.success) { showBanner((d && d.error) || 'Could not add affected items.', 'error'); return; }
            var failed = (d.results || []).filter(function (x) { return x && x.result !== 'SUCCESS'; });
            if (failed.length) showBanner(failed.length + ' item(s) failed to add.', 'warn');
            delete affectedItemsCache[String(ctx.wsId) + ':' + String(ctx.itemId)];
            renderAffectedItemsTab();
        }).catch(function (e) { closeComponentPicker(); showBanner('Add failed: ' + e, 'error'); });
    }

    // -------------------------------------------------------------------------
    // Bootstrap on DOMContentLoaded
    // -------------------------------------------------------------------------
    document.addEventListener('DOMContentLoaded', function () {
        // Inject the picker HTML partial before wiring any events.
        _injectPickerMarkup(function () {
            // List view
            var btnNew     = document.getElementById('btnNew');
            var btnRefresh = document.getElementById('btnRefresh');
            var btnPrev    = document.getElementById('btnPrev');
            var btnNext    = document.getElementById('btnNext');
            if (btnNew)     btnNew.onclick     = function () { openCreateForm(); };
            if (btnRefresh) btnRefresh.onclick  = function () {
                if (_isUnifiedScope()) { currentPage = 1; loadUnifiedView(); }
                else { loadTableauData(); }
            };
            if (btnPrev)    btnPrev.onclick     = function () {
                if (currentPage > 1) {
                    currentPage--;
                    if (_isUnifiedScope()) { renderUnifiedTable(_listLoadSeq); renderPagination(); }
                    else { loadTableauData(); }
                }
            };
            if (btnNext)    btnNext.onclick     = function () {
                var count = _isUnifiedScope() ? unifiedRows.length : totalRecords;
                var total = Math.ceil(count / pageSize);
                if (currentPage < total) {
                    currentPage++;
                    if (_isUnifiedScope()) { renderUnifiedTable(_listLoadSeq); renderPagination(); }
                    else { loadTableauData(); }
                }
            };
            // Form view
            var btnSave   = document.getElementById('btnSave');
            var btnCancel = document.getElementById('btnCancelForm');
            if (btnSave)   btnSave.onclick   = function () { saveForm(); };
            if (btnCancel) btnCancel.onclick = function () {
                var backEntry = detailStack.length >= 2 ? detailStack[detailStack.length - 2] : null;
                cancelForm(backEntry);
            };
            // Start
            plmCoreInit();
        });
    });

})();
