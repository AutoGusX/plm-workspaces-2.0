/**
 * PLM Workspaces 2.0 — My Work palette logic (full feature port)
 *
 * Self-contained: does NOT use engine.js or window._plmCfg.
 * Depends on: bridge.js (plmSend, plmCreateAuthPoller, plmHandleAsyncResult),
 *             theme.js (plmApplyTheme, plmUiPrefs).
 *
 * ES5-compatible (var, function declarations, no arrow functions) for the
 * Fusion embedded webview JavaScript engine.
 *
 * Features:
 *   - Task list: search, quick filters, workspace/date-preset filters,
 *     sort (dueDate/workspace/state/priority/title), group (workspace/
 *     dueDateBucket/priority/state), due-date color coding.
 *   - Summary strip: total/overdue/due-soon/high-priority counts.
 *   - Last-updated display with 1-minute ticker.
 *   - Auto-refresh (5/15/30 min), persisted to localStorage.
 *   - Multi-select (Ctrl+click, Shift+click), bulk workflow transitions.
 *   - Preview panel: parallel workspace-fields + item-detail fetch;
 *     visibleOnPreview field filtering; resizable; workflow actions.
 *   - Full detail view: breadcrumb navigation, collapsible sections,
 *     rich HTML field rendering, workflow transitions, refresh.
 *   - CAD-context matching: lineage URN → searchResultsForLineage →
 *     "Related to current model" badge on tasks/preview/detail.
 *   - Auth poller: retries when the user needs to sign in.
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
var DUE_SOON_DAYS = 7;
var DUE_BUCKET_ORDER = ['overdue', 'today', 'next3', 'later', 'none'];
var SEARCH_DEBOUNCE_MS = 250;
var PREVIEW_WIDTH_DEFAULT = 220;
var PREVIEW_WIDTH_MIN = 160;
var PREVIEW_WIDTH_STEP = 40;
var PREVIEW_WIDTH_MAX_PERCENT = 0.65;
var AUTO_REFRESH_STORAGE_KEY = 'plm_mw_auto_refresh_v2';
var PREVIEW_WIDTH_STORAGE_KEY = 'plm_mw_preview_width_v2';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
var allTasks = [];
var currentSearchQuery = '';
var currentQuickFilter = 'all';
var currentWorkspaceFilter = '';
var currentDatePreset = '';
var currentSortBy = 'dueDate';
var currentSortDir = 'asc';
var currentGroupBy = 'none';
var selectedTaskIndices = {};
var lastClickedIndex = null;
var previewEnabled = false;
var selectedTaskIndex = null;
var previewTask = null;
var currentTask = null;
var detailStack = [];
var lastUpdatedTime = null;
var lastUpdatedIntervalId = null;
var autoRefreshIntervalMinutes = 0;
var autoRefreshTimerId = null;
var searchDebounceTimer = null;
var tileClickTimeout = null;
var lineageContextCache = { urnsKey: null, itemIds: [] };
var detailDetailsClickBound = false;
var previewContentClickBound = false;
var _loadingTasks = false;

// ---------------------------------------------------------------------------
// Auth poller
// ---------------------------------------------------------------------------
var _authPoller = plmCreateAuthPoller(loadTasks);

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------
function _el(id) { return document.getElementById(id); }

function escapeHtml(s) {
    var div = document.createElement('div');
    div.textContent = s === null || s === undefined ? '' : String(s);
    return div.innerHTML;
}

function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function isCadWs(wsId) {
    var s = String(wsId || '');
    return s === '57' || s === '76';
}

// ---------------------------------------------------------------------------
// View management
// ---------------------------------------------------------------------------
function showView(name) {
    _el('viewList').style.display = (name === 'list') ? 'flex' : 'none';
    _el('viewDetail').style.display = (name === 'detail') ? 'flex' : 'none';
}

// ---------------------------------------------------------------------------
// loadTasks
// ---------------------------------------------------------------------------
function loadTasks() {
    if (_loadingTasks) { return; }
    _loadingTasks = true;

    var savedSelectedId = getSelectedTaskId();
    showView('list');
    _el('taskList').innerHTML = '';
    _el('taskList').style.display = 'none';
    _el('taskLoading').style.display = 'block';
    _el('taskError').style.display = 'none';
    setBtnRefreshLoading(true);

    plmSend('getTasks', {}).then(function (data) {
        _loadingTasks = false;
        _el('taskLoading').style.display = 'none';
        setBtnRefreshLoading(false);

        if (!data || !data.success) {
            if (data && data.unauthorized) {
                _el('taskList').innerHTML = '<div class="mw-empty">Sign in to continue.<br><small>Use the sign-in window that opened, or click Refresh after signing in.</small></div>';
                _el('taskList').style.display = 'block';
                _authPoller.start();
            } else {
                _el('taskError').style.display = 'block';
                _el('taskErrorMsg').textContent = (data && data.error) || 'Failed to load tasks.';
                _el('taskList').style.display = 'block';
            }
            return;
        }

        allTasks = data.tasks || [];
        selectedTaskIndices = {};
        lastUpdatedTime = Date.now();
        updateLastUpdatedDisplay();
        startLastUpdatedTicker();

        if (allTasks.length === 0) {
            updateSummaryStrip();
            selectedTaskIndex = null;
            _el('taskList').style.display = 'block';
            _el('taskList').innerHTML = '<div class="mw-empty">No outstanding work items.</div>';
            _el('taskCount').textContent = '0';
            _el('searchRow').style.display = 'none';
            _el('filterChips').style.display = 'none';
            _el('sortGroupRow').style.display = 'none';
            _el('summaryStripContainer').style.display = 'none';
            _el('filterSummary').style.display = 'none';
            return;
        }

        _el('taskCount').textContent = String(allTasks.length);
        _el('searchRow').style.display = 'block';
        _el('filterChips').style.display = 'flex';
        populateWorkspaceFilter();
        var newIndex = -1;
        if (savedSelectedId) {
            newIndex = findTaskIndexById(savedSelectedId.workspaceId, savedSelectedId.itemId);
        }
        if (newIndex >= 0) { selectedTaskIndex = newIndex; }
        else { selectedTaskIndex = null; }
        updateSummaryStrip();
        applyFiltersAndRender();
        applySummaryStripVisibility();
        updateBulkBar();
        if (newIndex >= 0) {
            scrollTileIntoView(newIndex);
            if (previewEnabled) { updatePreview(newIndex); }
        }
    }).catch(function () {
        _loadingTasks = false;
        _el('taskLoading').style.display = 'none';
        setBtnRefreshLoading(false);
        _el('taskError').style.display = 'block';
        _el('taskErrorMsg').textContent = 'Refresh failed. Please try again.';
        _el('taskList').style.display = 'block';
    });
}

function setBtnRefreshLoading(on) {
    var btn = _el('btnRefresh');
    if (!btn) { return; }
    btn.disabled = on;
    btn.textContent = on ? '…' : '↻ Refresh';
}

function getSelectedTaskId() {
    if (selectedTaskIndex !== null && allTasks[selectedTaskIndex]) {
        var t = allTasks[selectedTaskIndex];
        return { workspaceId: t.workspaceId, itemId: t.itemId };
    }
    return null;
}

function findTaskIndexById(workspaceId, itemId) {
    for (var i = 0; i < allTasks.length; i++) {
        if (allTasks[i].workspaceId === workspaceId && allTasks[i].itemId === itemId) { return i; }
    }
    return -1;
}

function scrollTileIntoView(index) {
    var tile = document.querySelector('.mw-tile[data-task-index="' + index + '"]');
    if (tile && tile.scrollIntoViewIfNeeded) { tile.scrollIntoViewIfNeeded(false); }
    else if (tile) { tile.scrollIntoView({ block: 'nearest' }); }
}

// ---------------------------------------------------------------------------
// Summary strip
// ---------------------------------------------------------------------------
function updateSummaryStrip() {
    var total = allTasks.length;
    var overdue = 0, dueSoon = 0, highPriority = 0;
    for (var i = 0; i < allTasks.length; i++) {
        var t = allTasks[i];
        var days = t.daysToDue;
        if (days !== undefined && days !== null) {
            if (days < 0) { overdue++; }
            else if (days <= DUE_SOON_DAYS) { dueSoon++; }
        }
        if (t.flagged) { highPriority++; }
    }
    _el('summaryTotal').textContent = String(total);
    _el('summaryOverdue').textContent = String(overdue);
    _el('summaryDueSoon').textContent = String(dueSoon);
    _el('summaryHighPriority').textContent = String(highPriority);
}

function applySummaryStripVisibility() {
    _el('summaryStripContainer').style.display = allTasks.length === 0 ? 'none' : 'flex';
}

// ---------------------------------------------------------------------------
// Last-updated display
// ---------------------------------------------------------------------------
function formatLastUpdated(ts) {
    if (!ts) { return '—'; }
    var diffMs = Date.now() - ts;
    var diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 1) { return 'just now'; }
    if (diffMins === 1) { return '1 min ago'; }
    if (diffMins < 60) { return diffMins + ' min ago'; }
    var diffHours = Math.floor(diffMins / 60);
    if (diffHours === 1) { return '1 hr ago'; }
    if (diffHours < 24) { return diffHours + ' hr ago'; }
    var d = new Date(ts);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function updateLastUpdatedDisplay() {
    var el = _el('lastUpdatedEl');
    if (!el) { return; }
    if (lastUpdatedTime === null) { el.style.display = 'none'; return; }
    el.style.display = 'inline';
    el.textContent = 'Updated ' + formatLastUpdated(lastUpdatedTime);
    el.title = new Date(lastUpdatedTime).toLocaleString();
}

function startLastUpdatedTicker() {
    if (lastUpdatedIntervalId) { clearInterval(lastUpdatedIntervalId); }
    lastUpdatedIntervalId = setInterval(function () {
        if (lastUpdatedTime) { updateLastUpdatedDisplay(); }
    }, 60000);
}

// ---------------------------------------------------------------------------
// Auto-refresh
// ---------------------------------------------------------------------------
function setAutoRefreshInterval() {
    var sel = _el('autoRefreshSelect');
    if (!sel) { return; }
    var mins = parseInt(sel.value, 10) || 0;
    autoRefreshIntervalMinutes = mins;
    try { localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, String(mins)); } catch (e) {}
    if (autoRefreshTimerId) { clearInterval(autoRefreshTimerId); autoRefreshTimerId = null; }
    if (mins > 0) {
        autoRefreshTimerId = setInterval(function () { loadTasks(); }, mins * 60 * 1000);
    }
}

// ---------------------------------------------------------------------------
// Due-date helpers
// ---------------------------------------------------------------------------
function getTileDueClass(task) {
    var days = task.daysToDue;
    if (days !== undefined && days !== null) {
        if (days < 0) { return 'tile-due-overdue'; }
        if (days === 0) { return 'tile-due-today'; }
        if (days <= 3) { return 'tile-due-soon'; }
        return 'tile-due-later';
    }
    if (task.dueDate != null) { return 'tile-due-later'; }
    return 'tile-due-none';
}

function getDueDateBucket(task) {
    var days = task.daysToDue;
    if (days == null) { return 'none'; }
    if (days < 0) { return 'overdue'; }
    if (days === 0) { return 'today'; }
    if (days <= 3) { return 'next3'; }
    return 'later';
}

function formatDueDateCell(task) {
    var days = task.daysToDue;
    var due = task.dueDate;
    if (due == null && (days === undefined || days === null)) {
        return { text: 'No due date', className: 'due-none' };
    }
    var d = due ? new Date(due) : null;
    var dateStr = (d && !isNaN(d.getTime()))
        ? d.toLocaleDateString(undefined, { month: '2-digit', day: '2-digit', year: 'numeric' })
        : String(due || '');
    var rel = '';
    if (days !== undefined && days !== null) {
        if (days < 0) { rel = 'Overdue by ' + Math.abs(days) + ' days'; }
        else if (days === 0) { rel = 'Due today'; }
        else if (days === 1) { rel = 'Due tomorrow'; }
        else { rel = 'Due in ' + days + ' days'; }
    }
    var text = dateStr + (rel ? ' — ' + rel : '');
    var className = 'due-later';
    if (days !== undefined && days !== null) {
        if (days < 0) { className = 'due-overdue'; }
        else if (days === 0) { className = 'due-today'; }
        else if (days <= 3) { className = 'due-soon'; }
    }
    return { text: text, className: className };
}

// ---------------------------------------------------------------------------
// Filtering
// ---------------------------------------------------------------------------
function getFilteredTasks() {
    var q = (currentSearchQuery || '').trim().toLowerCase();
    var tasks = [], indices = [];
    for (var i = 0; i < allTasks.length; i++) {
        var task = allTasks[i];
        if (currentQuickFilter === 'overdue') {
            if (task.daysToDue == null || task.daysToDue >= 0) { continue; }
        } else if (currentQuickFilter === 'dueSoon') {
            if (task.daysToDue == null || task.daysToDue < 0 || task.daysToDue > DUE_SOON_DAYS) { continue; }
        } else if (currentQuickFilter === 'highPriority') {
            if (!task.flagged) { continue; }
        }
        if (currentWorkspaceFilter && (task.workspaceTitle || '') !== currentWorkspaceFilter) { continue; }
        if (currentDatePreset && !taskMatchesDatePreset(task, currentDatePreset)) { continue; }
        if (q) {
            var descriptor = (task.itemDescriptor || task.itemTitle || '').toLowerCase();
            var workspace = (task.workspaceTitle || '').toLowerCase();
            var state = (task.workflowStateName || '').toLowerCase();
            var itemId = (task.itemId || '').toString();
            if (descriptor.indexOf(q) === -1 && workspace.indexOf(q) === -1 && state.indexOf(q) === -1 && itemId.indexOf(q) === -1) { continue; }
        }
        tasks.push(task);
        indices.push(i);
    }
    return { tasks: tasks, indices: indices };
}

function taskMatchesDatePreset(task, preset) {
    var dateStr = '';
    if ((preset === 'created7' || preset === 'created30') && (task.createdDate || task.created)) {
        dateStr = task.createdDate || task.created;
    } else if ((preset === 'modified7' || preset === 'modified30') && (task.modifiedDate || task.modified)) {
        dateStr = task.modifiedDate || task.modified;
    } else {
        dateStr = task.workflowStateSetDate || task.createdDate || task.created || task.modifiedDate || task.modified || '';
    }
    if (!dateStr) { return true; }
    var d = new Date(dateStr);
    if (isNaN(d.getTime())) { return true; }
    var daysAgo = (Date.now() - d.getTime()) / (24 * 60 * 60 * 1000);
    if (preset === 'created7' || preset === 'modified7') { return daysAgo <= 7; }
    if (preset === 'created30' || preset === 'modified30') { return daysAgo <= 30; }
    if (preset === 'older30') { return daysAgo > 30; }
    return true;
}

function setQuickFilter(filter) {
    currentQuickFilter = (filter === 'overdue' || filter === 'dueSoon' || filter === 'highPriority') ? filter : 'all';
    var chips = document.querySelectorAll('.filter-chip');
    for (var i = 0; i < chips.length; i++) {
        var active = chips[i].getAttribute('data-filter') === currentQuickFilter;
        chips[i].classList.toggle('active', active);
    }
    applyFiltersAndRender();
}

function setWorkspaceFilterAndRender() {
    var sel = _el('workspaceFilter');
    currentWorkspaceFilter = (sel && sel.value) ? sel.value : '';
    applyFiltersAndRender();
}

function setDatePresetAndRender() {
    var sel = _el('datePresetFilter');
    currentDatePreset = (sel && sel.value) ? sel.value : '';
    applyFiltersAndRender();
}

function populateWorkspaceFilter() {
    var sel = _el('workspaceFilter');
    if (!sel) { return; }
    var titles = [], seen = {};
    for (var i = 0; i < allTasks.length; i++) {
        var t = (allTasks[i].workspaceTitle || '').trim();
        if (t && !seen[t]) { seen[t] = true; titles.push(t); }
    }
    titles.sort(function (a, b) { return a.localeCompare(b); });
    var current = currentWorkspaceFilter;
    sel.innerHTML = '<option value="">All workspaces</option>' + titles.map(function (w) {
        return '<option value="' + escapeAttr(w) + '"' + (w === current ? ' selected' : '') + '>' + escapeHtml(w) + '</option>';
    }).join('');
    if (current && titles.indexOf(current) === -1) { currentWorkspaceFilter = ''; }
}

// ---------------------------------------------------------------------------
// Sorting
// ---------------------------------------------------------------------------
function compareTasks(a, b, sortBy, sortDir) {
    var va, vb;
    if (sortBy === 'dueDate') {
        va = (a.daysToDue != null) ? a.daysToDue : 999999;
        vb = (b.daysToDue != null) ? b.daysToDue : 999999;
        if (va !== vb) { return sortDir === 'asc' ? (va - vb) : (vb - va); }
        va = (a.dueDate || '').toString();
        vb = (b.dueDate || '').toString();
    } else if (sortBy === 'workspace') {
        va = (a.workspaceTitle || '').toLowerCase();
        vb = (b.workspaceTitle || '').toLowerCase();
    } else if (sortBy === 'state') {
        va = (a.workflowStateName || '').toLowerCase();
        vb = (b.workflowStateName || '').toLowerCase();
    } else if (sortBy === 'priority') {
        va = a.flagged ? 1 : 0; vb = b.flagged ? 1 : 0;
        if (va !== vb) { return sortDir === 'asc' ? (va - vb) : (vb - va); }
        va = (a.itemDescriptor || a.itemTitle || '').toLowerCase();
        vb = (b.itemDescriptor || b.itemTitle || '').toLowerCase();
    } else {
        va = (a.itemDescriptor || a.itemTitle || '').toLowerCase();
        vb = (b.itemDescriptor || b.itemTitle || '').toLowerCase();
    }
    if (va < vb) { return sortDir === 'asc' ? -1 : 1; }
    if (va > vb) { return sortDir === 'asc' ? 1 : -1; }
    return 0;
}

function applySort(tasks, indices) {
    if (!tasks.length) { return { tasks: tasks, indices: indices }; }
    var combined = tasks.map(function (t, i) { return { task: t, index: indices[i] }; });
    combined.sort(function (x, y) { return compareTasks(x.task, y.task, currentSortBy, currentSortDir); });
    return {
        tasks: combined.map(function (x) { return x.task; }),
        indices: combined.map(function (x) { return x.index; })
    };
}

function setSortAndRender() {
    var sb = _el('sortBy'), sd = _el('sortDir');
    if (sb) { currentSortBy = sb.value || 'dueDate'; }
    if (sd) { currentSortDir = sd.value || 'asc'; }
    applyFiltersAndRender();
}

// ---------------------------------------------------------------------------
// Grouping
// ---------------------------------------------------------------------------
function applyGroup(tasks, indices) {
    if (currentGroupBy === 'none' || !tasks.length) { return null; }
    var keyToGroup = {}, order = [];
    function getKey(task) {
        if (currentGroupBy === 'workspace') { return (task.workspaceTitle || '—').toString(); }
        if (currentGroupBy === 'dueDateBucket') { return getDueDateBucket(task); }
        if (currentGroupBy === 'priority') { return task.flagged ? 'Flagged' : 'Normal'; }
        if (currentGroupBy === 'state') { return (task.workflowStateName || '—').toString(); }
        return '—';
    }
    function getLabel(key) {
        if (currentGroupBy === 'dueDateBucket') {
            if (key === 'overdue') { return 'Overdue'; }
            if (key === 'today') { return 'Today'; }
            if (key === 'next3') { return 'Next 3 Days'; }
            if (key === 'later') { return 'Later'; }
            return 'No Due Date';
        }
        return key;
    }
    for (var i = 0; i < tasks.length; i++) {
        var k = getKey(tasks[i]);
        if (!keyToGroup[k]) {
            keyToGroup[k] = { label: getLabel(k), key: k, tasks: [], indices: [] };
            order.push(k);
        }
        keyToGroup[k].tasks.push(tasks[i]);
        keyToGroup[k].indices.push(indices[i]);
    }
    if (currentGroupBy === 'dueDateBucket') {
        order = DUE_BUCKET_ORDER.filter(function (k) { return keyToGroup[k]; });
    } else {
        order.sort(function (a, b) { return (a || '').localeCompare(b || ''); });
    }
    return order.map(function (k) { return keyToGroup[k]; });
}

function setGroupByAndRender() {
    var gb = _el('groupBy');
    if (gb) { currentGroupBy = gb.value || 'none'; }
    applyFiltersAndRender();
}

function syncSortGroupUI() {
    var sb = _el('sortBy'), sd = _el('sortDir'), gb = _el('groupBy');
    var wf = _el('workspaceFilter'), dp = _el('datePresetFilter');
    if (sb) { sb.value = currentSortBy; }
    if (sd) { sd.value = currentSortDir; }
    if (gb) { gb.value = currentGroupBy; }
    if (wf) { wf.value = currentWorkspaceFilter || ''; }
    if (dp) { dp.value = currentDatePreset || ''; }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
function applyFiltersAndRender() {
    var searchEl = _el('searchInput');
    if (searchEl) { currentSearchQuery = searchEl.value || ''; }
    var listEl = _el('taskList');
    var summaryEl = _el('filterSummary');
    var sortGroupRow = _el('sortGroupRow');
    if (!listEl || allTasks.length === 0) { return; }

    var result = getFilteredTasks();
    if (sortGroupRow) { sortGroupRow.style.display = 'flex'; }
    syncSortGroupUI();

    if (summaryEl) {
        summaryEl.style.display = 'inline';
        summaryEl.textContent = result.tasks.length === allTasks.length
            ? result.tasks.length + ' task(s)'
            : result.tasks.length + ' of ' + allTasks.length + ' task(s)';
    }

    if (result.tasks.length === 0) {
        listEl.style.display = 'block';
        listEl.innerHTML = '<div class="mw-empty">No tasks match your filters.</div>';
        updateBulkBar();
        return;
    }

    result = applySort(result.tasks, result.indices);
    var groups = applyGroup(result.tasks, result.indices);
    if (groups) { renderGroupedTiles(groups); }
    else { renderTaskTiles(result.tasks, result.indices); }
    updateBulkBar();
}

function onSearchInput() {
    if (searchDebounceTimer) { clearTimeout(searchDebounceTimer); }
    searchDebounceTimer = setTimeout(function () {
        searchDebounceTimer = null;
        var searchEl = _el('searchInput');
        if (searchEl) { currentSearchQuery = searchEl.value || ''; }
        applyFiltersAndRender();
    }, SEARCH_DEBOUNCE_MS);
}

function buildTileHtml(task, allTasksIndex) {
    var descriptor = task.itemDescriptor || task.itemTitle || 'Untitled';
    var descriptorContent = task.relatedToCurrentModel
        ? '<span class="tile-related-icon" title="Related to current model">&#128279;</span>' + escapeHtml(descriptor)
        : escapeHtml(descriptor);
    var meta = [task.workspaceTitle, task.workflowStateName].filter(Boolean).join(' · ') || '—';
    var dueInfo = formatDueDateCell(task);
    var dueHtml = '<div class="tile-due">' + escapeHtml(dueInfo.text) + '</div>';
    var relatedHtml = task.relatedToCurrentModel ? '<div class="tile-related">Related to current model</div>' : '';
    var tileClass = 'mw-tile ' + getTileDueClass(task);
    if (selectedTaskIndices[allTasksIndex]) { tileClass += ' mw-tile-selected'; }
    return '<div class="' + tileClass + '" data-task-index="' + allTasksIndex + '" onclick="handleTileClick(event,' + allTasksIndex + ')" ondblclick="handleTileDblClick(event,' + allTasksIndex + ')">' +
        '<div class="tile-descriptor">' + descriptorContent + '</div>' +
        '<div class="tile-meta">' + escapeHtml(meta) + '</div>' +
        dueHtml + relatedHtml + '</div>';
}

function renderTaskTiles(tasks, indices) {
    var listEl = _el('taskList');
    if (!listEl) { return; }
    if (!indices) { indices = tasks.map(function (_, i) { return i; }); }
    listEl.innerHTML = tasks.map(function (task, idx) { return buildTileHtml(task, indices[idx]); }).join('');
    listEl.style.display = 'block';
}

function renderGroupedTiles(groups) {
    var listEl = _el('taskList');
    if (!listEl) { return; }
    var html = [];
    for (var g = 0; g < groups.length; g++) {
        var grp = groups[g];
        html.push('<div class="mw-group-header">' + escapeHtml(grp.label) + '</div>');
        for (var i = 0; i < grp.tasks.length; i++) {
            html.push(buildTileHtml(grp.tasks[i], grp.indices[i]));
        }
    }
    listEl.innerHTML = html.join('');
    listEl.style.display = 'block';
}

// ---------------------------------------------------------------------------
// Tile click handling (single / double / ctrl / shift)
// ---------------------------------------------------------------------------
function handleTileClick(event, index) {
    if (event.ctrlKey || event.metaKey) {
        clearTimeout(tileClickTimeout);
        toggleTaskSelection(index);
        return;
    }
    if (event.shiftKey) {
        clearTimeout(tileClickTimeout);
        selectTaskRange(index);
        return;
    }
    if (previewEnabled) {
        clearTimeout(tileClickTimeout);
        tileClickTimeout = setTimeout(function () { updatePreview(index); }, 200);
    } else {
        openTaskDetailByIndex(index);
    }
    lastClickedIndex = index;
}

function handleTileDblClick(event, index) {
    clearTimeout(tileClickTimeout);
    openTaskDetailByIndex(index);
    lastClickedIndex = index;
}

// ---------------------------------------------------------------------------
// Multi-select
// ---------------------------------------------------------------------------
function toggleTaskSelection(index) {
    if (selectedTaskIndices[index]) { delete selectedTaskIndices[index]; }
    else { selectedTaskIndices[index] = true; }
    lastClickedIndex = index;
    updateBulkBar();
    applySelectionHighlight();
}

function selectTaskRange(index) {
    var start = (lastClickedIndex != null) ? lastClickedIndex : index;
    var lo = Math.min(start, index), hi = Math.max(start, index);
    for (var i = lo; i <= hi; i++) {
        if (allTasks[i]) { selectedTaskIndices[i] = true; }
    }
    lastClickedIndex = index;
    updateBulkBar();
    applySelectionHighlight();
}

function getSelectedIndicesList() {
    var list = [];
    for (var k in selectedTaskIndices) {
        if (selectedTaskIndices[k]) { list.push(parseInt(k, 10)); }
    }
    return list.sort(function (a, b) { return a - b; });
}

function clearTaskSelection() {
    selectedTaskIndices = {};
    updateBulkBar();
    applySelectionHighlight();
}

function applySelectionHighlight() {
    var listEl = _el('taskList');
    if (!listEl) { return; }
    var tiles = listEl.querySelectorAll('.mw-tile[data-task-index]');
    for (var i = 0; i < tiles.length; i++) {
        var idx = parseInt(tiles[i].getAttribute('data-task-index'), 10);
        tiles[i].classList.toggle('mw-tile-selected', !!selectedTaskIndices[idx]);
    }
}

// ---------------------------------------------------------------------------
// Bulk action bar
// ---------------------------------------------------------------------------
function updateBulkBar() {
    var bar = _el('bulkBar'), label = _el('bulkLabel');
    var list = getSelectedIndicesList();
    if (!bar || !label) { return; }
    if (list.length < 2) { bar.style.display = 'none'; return; }
    bar.style.display = 'flex';
    label.textContent = list.length + ' selected';
    _el('bulkMessage').style.display = 'none';
    var sel = _el('bulkTransition');
    if (sel) { sel.innerHTML = '<option value="">Loading…</option>'; }
    loadBulkTransitionsForFirstSelected(list[0]);
}

function loadBulkTransitionsForFirstSelected(firstIndex) {
    var task = allTasks[firstIndex];
    if (!task) { return; }
    var sel = _el('bulkTransition');
    if (!sel) { return; }
    plmSend('getTransitions', { workspaceId: task.workspaceId, itemId: task.itemId }).then(function (data) {
        var transitions = (data && data.transitions) || [];
        sel.innerHTML = '<option value="">— Select transition —</option>';
        for (var i = 0; i < transitions.length; i++) {
            var t = transitions[i];
            var lbl = t.shortName || t.name || t.transitionID || 'Action';
            var tid = t.transitionID || t.id || '';
            var opt = document.createElement('option');
            opt.value = tid; opt.textContent = lbl;
            sel.appendChild(opt);
        }
    }).catch(function () {
        if (sel) { sel.innerHTML = '<option value="">— Select —</option>'; }
    });
}

function runBulkWorkflowTransition() {
    var list = getSelectedIndicesList();
    if (list.length === 0) { return; }
    var sel = _el('bulkTransition');
    var transitionId = sel ? sel.value : '';
    if (!transitionId) { showBulkMessage('Select a transition first.', 'error'); return; }
    var commentEl = _el('bulkComment');
    var comments = commentEl ? commentEl.value.trim() : '';
    showBulkMessage('Applying…', '');
    var total = list.length, succeeded = 0, failed = 0, failedErrors = [];

    function runNext(idx) {
        if (idx >= list.length) {
            if (failed === 0) {
                showBulkMessage('Transition applied to ' + succeeded + ' item(s).', 'success');
                clearTaskSelection();
                loadTasks();
            } else {
                var msg = succeeded + ' succeeded, ' + failed + ' failed.';
                if (failedErrors.length) { msg += ' — ' + failedErrors[0]; }
                showBulkMessage(msg, 'error');
            }
            return;
        }
        var task = allTasks[list[idx]];
        if (!task) { failed++; runNext(idx + 1); return; }
        plmSend('getTransitions', { workspaceId: task.workspaceId, itemId: task.itemId }).then(function (trData) {
            var currentStep = (trData && trData.currentStep !== undefined) ? trData.currentStep : 0;
            return plmSend('runWorkflowTransition', {
                workspaceId: task.workspaceId, itemId: task.itemId,
                transitionId: transitionId, currentStep: currentStep,
                workflowComments: comments, comments: comments
            });
        }).then(function (runData) {
            if (runData && runData.success !== false && runData.ok !== false) { succeeded++; }
            else {
                failed++;
                var err = (runData && (runData.error || runData.message)) || '';
                if (err) { failedErrors.push(err); }
            }
            runNext(idx + 1);
        }).catch(function (err) {
            failed++;
            failedErrors.push((err && (err.message || err.error)) || 'Request failed');
            runNext(idx + 1);
        });
    }
    runNext(0);
}

function showBulkMessage(msg, type) {
    var el = _el('bulkMessage');
    if (!el) { return; }
    el.textContent = msg || '';
    el.className = 'bulk-message ' + (type || '');
    el.style.display = msg ? 'inline' : 'none';
}

// ---------------------------------------------------------------------------
// Preview panel
// ---------------------------------------------------------------------------
function togglePreview() {
    previewEnabled = !previewEnabled;
    var panel = _el('previewPanel');
    var btn = _el('btnPreview');
    if (panel) {
        if (previewEnabled) {
            panel.classList.add('preview-visible');
            applySavedPreviewWidth();
        } else {
            panel.classList.remove('preview-visible');
        }
    }
    if (btn) { btn.textContent = previewEnabled ? 'Hide preview' : 'Show preview'; }
    if (previewEnabled && selectedTaskIndex !== null) {
        updatePreview(selectedTaskIndex);
    } else if (!previewEnabled) {
        selectedTaskIndex = null;
        previewTask = null;
    }
}

function getPreviewWidth() {
    var panel = _el('previewPanel');
    if (!panel) { return PREVIEW_WIDTH_DEFAULT; }
    var w = panel.style.width;
    if (w && w.indexOf('px') !== -1) { return parseInt(w, 10) || PREVIEW_WIDTH_DEFAULT; }
    return PREVIEW_WIDTH_DEFAULT;
}

function setPreviewWidth(px) {
    var panel = _el('previewPanel');
    if (panel) { panel.style.width = Math.round(px) + 'px'; }
}

function getPreviewMaxWidth() {
    var content = _el('listViewContent');
    return content ? Math.floor(content.offsetWidth * PREVIEW_WIDTH_MAX_PERCENT) : 400;
}

function applySavedPreviewWidth() {
    var maxPx = getPreviewMaxWidth();
    try {
        var saved = localStorage.getItem(PREVIEW_WIDTH_STORAGE_KEY);
        if (saved !== null) {
            var num = parseInt(saved, 10);
            if (!isNaN(num) && num >= PREVIEW_WIDTH_MIN) {
                setPreviewWidth(Math.max(PREVIEW_WIDTH_MIN, Math.min(maxPx, num)));
                return;
            }
        }
    } catch (e) {}
    setPreviewWidth(PREVIEW_WIDTH_DEFAULT);
}

function previewNarrower() {
    var next = Math.max(PREVIEW_WIDTH_MIN, getPreviewWidth() - PREVIEW_WIDTH_STEP);
    setPreviewWidth(next);
    try { localStorage.setItem(PREVIEW_WIDTH_STORAGE_KEY, String(Math.round(next))); } catch (e) {}
}

function previewWider() {
    var next = Math.min(getPreviewMaxWidth(), getPreviewWidth() + PREVIEW_WIDTH_STEP);
    setPreviewWidth(next);
    try { localStorage.setItem(PREVIEW_WIDTH_STORAGE_KEY, String(Math.round(next))); } catch (e) {}
}

function showPreviewBanner(msg, type) {
    var el = _el('previewBanner');
    if (!el) { return; }
    el.textContent = msg || '';
    el.className = type || '';
    el.style.display = msg ? 'block' : 'none';
}

function clearPreviewBanner() { showPreviewBanner('', ''); }

function updatePreview(index) {
    var task = allTasks[index];
    if (!task) { return; }
    selectedTaskIndex = index;

    var emptyEl = _el('previewEmpty');
    var loadingEl = _el('previewLoading');
    var contentEl = _el('previewContent');
    if (emptyEl) { emptyEl.style.display = 'none'; }
    if (loadingEl) { loadingEl.style.display = 'block'; }
    if (contentEl) { contentEl.classList.remove('preview-visible-content'); }
    clearPreviewBanner();
    setPreviewRelatedIndicator(false);

    var workspaceId = task.workspaceId;
    var itemId = task.itemId;

    if (!workspaceId || !itemId) {
        if (loadingEl) { loadingEl.style.display = 'none'; }
        if (contentEl) {
            contentEl.classList.add('preview-visible-content');
            _el('previewTitle').textContent = task.itemDescriptor || task.itemTitle || 'Untitled';
            _el('previewMeta').textContent = [task.workspaceTitle, task.workflowStateName].filter(Boolean).join(' · ') || '—';
            _el('previewFields').innerHTML = '';
            setPreviewWorkflowActions([], 0);
        }
        return;
    }

    var promiseFields = plmSend('getWorkspaceFields', { workspaceId: workspaceId });
    var promiseDetail = plmSend('getItemDetail', { workspaceId: workspaceId, itemId: itemId });
    var promiseRelated = ensureLineageContext().then(function (ctx) {
        return (ctx.itemIds && ctx.itemIds.length)
            ? ctx.itemIds.indexOf(itemId) !== -1
            : false;
    }).catch(function () { return false; });

    Promise.all([promiseFields, promiseDetail, promiseRelated]).then(function (results) {
        if (loadingEl) { loadingEl.style.display = 'none'; }
        var fieldsResult = results[0];
        var detailResult = results[1];
        var related = results[2] === true;

        task.relatedToCurrentModel = related;
        setPreviewRelatedIndicator(related);

        if (!detailResult || !detailResult.success || !detailResult.item) {
            if (contentEl) {
                contentEl.classList.add('preview-visible-content');
                _el('previewTitle').textContent = task.itemDescriptor || task.itemTitle || 'Untitled';
                _el('previewMeta').textContent = [task.workspaceTitle, task.workflowStateName].filter(Boolean).join(' · ') || '—';
                _el('previewFields').innerHTML = '';
                setPreviewWorkflowActions([], 0);
            }
            showPreviewBanner((detailResult && detailResult.error) || 'Could not load details.', 'error');
            return;
        }

        var item = detailResult.item;
        var transitions = detailResult.transitions || [];
        var currentStep = (detailResult.currentStep !== undefined) ? detailResult.currentStep : 0;
        var title = item.title || task.itemDescriptor || task.itemTitle || 'Untitled';
        var meta = ((item.workspace && item.workspace.title) ? item.workspace.title : '') +
            ((item.currentState && item.currentState.title) ? ' · ' + item.currentState.title : '') || '—';

        previewTask = {
            id: itemId, workspaceId: workspaceId,
            title: title, meta: meta,
            openInBrowserUrl: item.openInBrowserUrl || task.fusionManageUrl || '',
            transitions: transitions, currentStep: currentStep
        };

        // Determine fields to show: prefer visibleOnPreview fields, else first 8 from sections
        var fieldsHtml = [];
        var visibleFields = [];
        if (fieldsResult && fieldsResult.success && Array.isArray(fieldsResult.fields)) {
            for (var i = 0; i < fieldsResult.fields.length; i++) {
                if (fieldsResult.fields[i].visibleOnPreview) { visibleFields.push(fieldsResult.fields[i]); }
            }
            visibleFields.sort(function (a, b) {
                return ((a.displayOrder != null ? Number(a.displayOrder) : 9999)) - ((b.displayOrder != null ? Number(b.displayOrder) : 9999));
            });
        }

        var fieldByKey = getItemFieldObjects(item);
        if (visibleFields.length > 0) {
            for (var j = 0; j < visibleFields.length; j++) {
                var fd = visibleFields[j];
                var fkey = getFieldKeyFromSelf(fd.__self__) || ((fd.name || fd.id || '').replace(/\s+/g, '_').toUpperCase());
                var label = fd.name || fd.title || fkey || '—';
                var field = fieldByKey[fkey];
                var valuePart = field ? renderFieldValuePart(getFieldDisplay(field)) : '—';
                fieldsHtml.push('<div class="preview-field-row"><div class="preview-field-label">' + escapeHtml(label) + '</div><div class="preview-field-value preview-field-value-rich">' + valuePart + '</div></div>');
            }
        } else {
            // Fall back: show up to 8 fields from sections
            var count = 0;
            if (item.sections) {
                for (var s = 0; s < item.sections.length && count < 8; s++) {
                    var sfields = item.sections[s].fields || [];
                    for (var fi = 0; fi < sfields.length && count < 8; fi++) {
                        var f = sfields[fi];
                        var fdisp = getFieldDisplay(f);
                        fieldsHtml.push('<div class="preview-field-row"><div class="preview-field-label">' + escapeHtml(f.title || '') + '</div><div class="preview-field-value preview-field-value-rich">' + renderFieldValuePart(fdisp) + '</div></div>');
                        count++;
                    }
                }
            }
            if (fieldsHtml.length === 0) {
                fieldsHtml.push('<div class="preview-field-row"><span class="preview-no-actions">No preview fields configured.</span></div>');
            }
        }

        if (contentEl) {
            contentEl.classList.add('preview-visible-content');
            _el('previewTitle').textContent = title;
            _el('previewMeta').textContent = meta;
            var previewFieldsEl = _el('previewFields');
            previewFieldsEl.innerHTML = fieldsHtml.join('');
            loadImageApiPlaceholders(previewFieldsEl);
            bindPreviewContentClicks();
            setPreviewWorkflowActions(transitions, currentStep);
        }
    }).catch(function () {
        if (loadingEl) { loadingEl.style.display = 'none'; }
        if (contentEl) {
            contentEl.classList.add('preview-visible-content');
            _el('previewTitle').textContent = task.itemDescriptor || task.itemTitle || 'Untitled';
            _el('previewMeta').textContent = [task.workspaceTitle, task.workflowStateName].filter(Boolean).join(' · ') || '—';
            _el('previewFields').innerHTML = '';
            setPreviewWorkflowActions([], 0);
        }
        showPreviewBanner('Could not load details.', 'error');
    });
}

function setPreviewRelatedIndicator(related) {
    var banner = _el('previewRelatedBanner');
    var badge = _el('previewRelatedBadge');
    if (banner) { banner.style.display = related ? 'block' : 'none'; }
    if (badge) { badge.style.display = related ? 'block' : 'none'; }
}

function setPreviewWorkflowActions(transitions, currentStep) {
    var container = _el('previewActions');
    if (!container) { return; }
    if (previewTask) { previewTask.currentStep = (currentStep !== undefined) ? currentStep : 0; }
    container.innerHTML = '';
    if (!transitions || !transitions.length) {
        container.innerHTML = '<span class="preview-no-actions">No actions</span>';
        return;
    }
    for (var i = 0; i < transitions.length; i++) {
        var t = transitions[i];
        var label = t.shortName || t.name || t.transitionID || 'Action';
        var tid = t.transitionID || t.id || '';
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'transition-btn';
        btn.textContent = label;
        (function (transId) {
            btn.onclick = function () { runPreviewWorkflowAction(transId); };
        }(tid));
        container.appendChild(btn);
    }
}

function runPreviewWorkflowAction(transitionId) {
    if (!previewTask || !transitionId) { return; }
    var commentEl = _el('previewComment');
    var comments = commentEl ? commentEl.value.trim() : '';
    clearPreviewBanner();
    plmSend('runWorkflowTransition', {
        itemId: previewTask.id, workspaceId: previewTask.workspaceId,
        transitionId: transitionId, currentStep: previewTask.currentStep || 0,
        workflowComments: comments, comments: comments
    }).then(function (data) {
        if (data && data.success !== false && data.ok !== false) {
            showPreviewBanner('Workflow action completed.', 'success');
            if (selectedTaskIndex !== null) { updatePreview(selectedTaskIndex); }
        } else {
            showPreviewBanner((data && (data.error || data.message)) || 'Action failed.', 'error');
        }
    }).catch(function () { showPreviewBanner('Action failed.', 'error'); });
}

function openDetailFromPreview() {
    if (selectedTaskIndex !== null) { openTaskDetailByIndex(selectedTaskIndex); }
}

function bindPreviewContentClicks() {
    if (previewContentClickBound) { return; }
    var container = _el('previewContent');
    if (!container) { return; }
    previewContentClickBound = true;
    container.addEventListener('click', function (e) {
        var fusionBtn = e.target.closest && e.target.closest('.open-in-fusion-icon');
        if (fusionBtn) {
            e.preventDefault(); e.stopPropagation();
            var fws = fusionBtn.getAttribute('data-workspace-id');
            var fid = fusionBtn.getAttribute('data-item-id');
            if (fws && fid) {
                fusionBtn.disabled = true;
                plmSend('openInFusion', { workspaceId: fws, itemId: fid })
                    .then(function () { fusionBtn.disabled = false; })
                    .catch(function () { fusionBtn.disabled = false; });
            }
            return;
        }
        var link = e.target.classList.contains('detail-item-link') ? e.target : (e.target.closest && e.target.closest('.detail-item-link'));
        if (link) {
            e.preventDefault();
            var ws = link.getAttribute('data-workspace-id');
            var iid = link.getAttribute('data-item-id');
            if (ws && iid) { drillIntoItem(ws, iid); }
            return;
        }
        var anchor = (e.target.tagName === 'A') ? e.target : (e.target.closest && e.target.closest('a'));
        if (anchor && anchor.href && /^https?:\/\//i.test(anchor.href)) {
            e.preventDefault();
            plmSend('openInBrowser', { url: anchor.href });
        }
    });
}

// ---------------------------------------------------------------------------
// Full detail view
// ---------------------------------------------------------------------------
function openTaskDetailByIndex(index) {
    var task = allTasks[index];
    if (!task) { return; }
    var titleText = task.itemDescriptor || task.itemTitle || 'Untitled';
    showView('detail');
    _el('detailTitle').textContent = titleText;
    _el('detailMetaText').textContent = '—';
    _el('detailRelatedBadge').style.display = 'none';
    _el('detailDetails').innerHTML = '<div class="detail-loading"><span>Loading details…</span></div>';
    var wfSec = _el('workflowSection'); if (wfSec) { wfSec.style.display = 'none'; }
    _el('detailBreadcrumb').innerHTML = '';
    clearDetailBanner();
    var commentEl = _el('detailComment');
    if (commentEl) { commentEl.value = ''; }
    setDetailRelatedIndicator(false);

    currentTask = {
        id: task.itemId, workspaceId: task.workspaceId,
        title: titleText,
        meta: [task.workspaceTitle, task.workflowStateName].filter(Boolean).join(' · ') || '—',
        openInBrowserUrl: task.fusionManageUrl || '',
        transitions: [], currentStep: 0
    };
    detailStack = [];
    _el('detailMetaText').textContent = currentTask.meta;
    setWorkflowActions([], 0);
    syncDetailOpenInFusionBtn(task.workspaceId, task.itemId);

    plmSend('getItemDetail', { workspaceId: currentTask.workspaceId, itemId: currentTask.id }).then(function (data) {
        if (!data || !data.success || !data.item) {
            _el('detailDetails').innerHTML = '<div style="padding:12px">' + escapeHtml((data && data.error) || 'Could not load details.') + '</div>';
            return;
        }
        var item = data.item;
        var transitions = data.transitions || [];
        var currentStep = (data.currentStep !== undefined) ? data.currentStep : 0;
        var title = item.title || currentTask.title;
        var meta = ((item.workspace && item.workspace.title) ? item.workspace.title : '') +
            ((item.currentState && item.currentState.title) ? ' · ' + item.currentState.title : '') || currentTask.meta;
        currentTask.transitions = transitions;
        currentTask.currentStep = currentStep;
        currentTask.openInBrowserUrl = item.openInBrowserUrl || currentTask.openInBrowserUrl || '';

        var entry = {
            workspaceId: currentTask.workspaceId, itemId: currentTask.id,
            title: title, meta: meta,
            openInBrowserUrl: currentTask.openInBrowserUrl,
            itemData: item, transitions: transitions, currentStep: currentStep
        };
        detailStack = [entry];
        _el('detailTitle').textContent = title;
        _el('detailMetaText').textContent = meta;
        renderBreadcrumb();
        renderItemDetails(item);
        setWorkflowActions(transitions, currentStep);

        ensureLineageContext().then(function (ctx) {
            return ctx.itemIds && ctx.itemIds.length ? ctx.itemIds.indexOf(currentTask.id) !== -1 : false;
        }).then(function (related) {
            if (task) { task.relatedToCurrentModel = related; }
            setDetailRelatedIndicator(related);
            if (related) {
                var mt = _el('detailMetaText');
                if (mt) { mt.textContent = meta + (meta ? ' · ' : '') + 'Related to current model'; }
            }
        }).catch(function () {});
    }).catch(function () {
        _el('detailDetails').innerHTML = '<div style="padding:12px">Could not load details.</div>';
    });
}

function drillIntoItem(workspaceId, itemId) {
    clearDetailBanner();
    syncDetailOpenInFusionBtn(workspaceId, itemId);
    _el('detailDetails').innerHTML = '<div class="detail-loading"><span>Loading…</span></div>';
    plmSend('getItemDetail', { workspaceId: workspaceId, itemId: itemId }).then(function (data) {
        if (!data || !data.success || !data.item) {
            _el('detailDetails').innerHTML = '<div style="padding:12px">' + escapeHtml((data && data.error) || 'Could not load details.') + '</div>';
            return;
        }
        var item = data.item;
        var title = item.title || 'Untitled';
        var meta = ((item.workspace && item.workspace.title) ? item.workspace.title : '') +
            ((item.currentState && item.currentState.title) ? ' · ' + item.currentState.title : '') || '—';
        var transitions = data.transitions || [];
        var currentStep = (data.currentStep !== undefined) ? data.currentStep : 0;
        var entry = {
            workspaceId: workspaceId, itemId: itemId,
            title: title, meta: meta, openInBrowserUrl: item.openInBrowserUrl || '',
            itemData: item, transitions: transitions, currentStep: currentStep
        };
        detailStack.push(entry);
        currentTask = { id: itemId, workspaceId: workspaceId, title: title, meta: meta,
            openInBrowserUrl: entry.openInBrowserUrl, transitions: transitions, currentStep: currentStep };
        _el('detailTitle').textContent = title;
        _el('detailMetaText').textContent = meta;
        renderBreadcrumb();
        renderItemDetails(item);
        setWorkflowActions(transitions, currentStep);
    }).catch(function () {
        _el('detailDetails').innerHTML = '<div style="padding:12px">Could not load details.</div>';
    });
}

function closeTaskDetail() {
    currentTask = null;
    detailStack = [];
    setDetailRelatedIndicator(false);
    syncDetailOpenInFusionBtn(null, null);
    _el('detailBreadcrumb').innerHTML = '';
    showView('list');
}

function renderBreadcrumb() {
    var el = _el('detailBreadcrumb');
    if (!el) { return; }
    if (!detailStack.length) { el.innerHTML = ''; return; }
    var parts = ['<a href="#" class="bread-link" data-back="list">Task List</a>'];
    for (var i = 0; i < detailStack.length; i++) {
        var entry = detailStack[i];
        var title = entry.title || ('Item ' + entry.itemId);
        parts.push('<span class="bread-sep"> &gt; </span>');
        if (i === detailStack.length - 1) {
            parts.push('<span class="bread-current">' + escapeHtml(title) + '</span>');
        } else {
            parts.push('<a href="#" class="bread-link" data-index="' + i + '">' + escapeHtml(title) + '</a>');
        }
    }
    el.innerHTML = parts.join('');
    var links = el.querySelectorAll('.bread-link');
    for (var j = 0; j < links.length; j++) {
        (function (a) {
            a.addEventListener('click', function (e) {
                e.preventDefault();
                if (a.getAttribute('data-back') === 'list') { closeTaskDetail(); return; }
                var idx = parseInt(a.getAttribute('data-index'), 10);
                if (!isNaN(idx) && idx >= 0 && idx < detailStack.length) { goToBreadcrumbIndex(idx); }
            });
        }(links[j]));
    }
}

function syncDetailOpenInFusionBtn(wsId, itemId) {
    var btn = _el('btnDetailOpenInFusion');
    if (!btn) { return; }
    if (isCadWs(wsId) && itemId) {
        btn.style.display = '';
        btn.onclick = function () {
            btn.disabled = true;
            plmSend('openInFusion', { workspaceId: String(wsId), itemId: String(itemId) })
                .then(function (r) {
                    btn.disabled = false;
                    var d = (r && typeof r === 'object') ? r : {};
                    try { if (typeof r === 'string') { d = JSON.parse(r); } } catch (e) {}
                    if (!d.success) { showDetailBanner(d.error || 'Could not open in Fusion.', 'error'); }
                })
                .catch(function () { btn.disabled = false; });
        };
    } else {
        btn.style.display = 'none';
        btn.onclick = null;
    }
}

function goToBreadcrumbIndex(index) {
    if (index < 0 || index >= detailStack.length) { return; }
    clearDetailBanner();
    detailStack = detailStack.slice(0, index + 1);
    var entry = detailStack[detailStack.length - 1];
    currentTask = {
        id: entry.itemId, workspaceId: entry.workspaceId,
        title: entry.title, meta: entry.meta, openInBrowserUrl: entry.openInBrowserUrl || '',
        transitions: entry.transitions || [], currentStep: entry.currentStep || 0
    };
    _el('detailTitle').textContent = currentTask.title;
    _el('detailMetaText').textContent = currentTask.meta;
    renderBreadcrumb();
    renderItemDetails(entry.itemData);
    setWorkflowActions(currentTask.transitions, currentTask.currentStep);
    syncDetailOpenInFusionBtn(entry.workspaceId, entry.itemId);
}

function setWorkflowActions(transitions, currentStep) {
    if (currentTask) { currentTask.currentStep = (currentStep !== undefined) ? currentStep : 0; }
    var section = _el('workflowSection');
    var btnsEl  = _el('transitionBtns');
    if (!section || !btnsEl) { return; }
    btnsEl.innerHTML = '';
    if (!transitions || !transitions.length) {
        section.style.display = 'none';
        return;
    }
    section.style.display = 'block';
    for (var i = 0; i < transitions.length; i++) {
        var t = transitions[i];
        var label = t.shortName || t.name || t.transitionID || 'Action';
        var tid = t.transitionID || t.id || '';
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'transition-btn';
        btn.textContent = label;
        (function (transId) {
            btn.onclick = function () { runWorkflowAction(transId); };
        }(tid));
        btnsEl.appendChild(btn);
    }
}

function runWorkflowAction(transitionId) {
    if (!currentTask || !transitionId) { return; }
    var commentEl = _el('detailComment');
    var comments = commentEl ? commentEl.value.trim() : '';
    clearDetailBanner();
    plmSend('runWorkflowTransition', {
        itemId: currentTask.id, workspaceId: currentTask.workspaceId,
        transitionId: transitionId, currentStep: currentTask.currentStep || 0,
        workflowComments: comments, comments: comments
    }).then(function (data) {
        if (data && data.success !== false && data.ok !== false) {
            showDetailBanner('Workflow action completed.', 'success');
            refreshTransitionsForCurrentTask();
        } else {
            showDetailBanner((data && (data.error || data.message)) || 'Action failed.', 'error');
        }
    }).catch(function () { showDetailBanner('Action failed.', 'error'); });
}

function refreshTransitionsForCurrentTask() {
    if (!currentTask || !currentTask.workspaceId || !currentTask.id) { return; }
    plmSend('getTransitions', { workspaceId: currentTask.workspaceId, itemId: currentTask.id }).then(function (data) {
        if (!data || data.error) { return; }
        currentTask.transitions = data.transitions || [];
        currentTask.currentStep = (data.currentStep !== undefined) ? data.currentStep : 0;
        setWorkflowActions(currentTask.transitions, currentTask.currentStep);
        setTimeout(function () { clearDetailBanner(); }, 2500);
    }).catch(function () {});
}

function refreshTaskDetail() {
    if (!currentTask || !currentTask.workspaceId || !currentTask.id) { return; }
    var container = _el('detailDetails');
    if (container) { container.innerHTML = '<div class="detail-loading"><span>Refreshing…</span></div>'; }
    plmSend('getItemDetail', { workspaceId: currentTask.workspaceId, itemId: currentTask.id, skipCache: true }).then(function (data) {
        if (!data || !data.success || !data.item) {
            if (container) { container.innerHTML = '<div style="padding:12px">' + escapeHtml((data && data.error) || 'Could not refresh.') + '</div>'; }
            return;
        }
        var item = data.item;
        var title = item.title || currentTask.title;
        var meta = ((item.workspace && item.workspace.title) ? item.workspace.title : '') +
            ((item.currentState && item.currentState.title) ? ' · ' + item.currentState.title : '') || currentTask.meta;
        var transitions = data.transitions || [];
        var currentStep = (data.currentStep !== undefined) ? data.currentStep : 0;
        currentTask.title = title; currentTask.meta = meta;
        currentTask.openInBrowserUrl = item.openInBrowserUrl || currentTask.openInBrowserUrl || '';
        currentTask.transitions = transitions; currentTask.currentStep = currentStep;
        if (detailStack.length) {
            var e = detailStack[detailStack.length - 1];
            e.title = title; e.meta = meta; e.openInBrowserUrl = currentTask.openInBrowserUrl;
            e.itemData = item; e.transitions = transitions; e.currentStep = currentStep;
        }
        _el('detailTitle').textContent = title;
        _el('detailMetaText').textContent = meta;
        renderItemDetails(item);
        setWorkflowActions(transitions, currentStep);
    }).catch(function () {
        if (container) { container.innerHTML = '<div style="padding:12px">Could not refresh.</div>'; }
    });
}

function openInBrowser() {
    var url = currentTask && currentTask.openInBrowserUrl ? currentTask.openInBrowserUrl : '';
    if (url) { plmSend('openInBrowser', { url: url }); }
}

function setDetailRelatedIndicator(related) {
    var banner = _el('detailRelatedBanner');
    var badge = _el('detailRelatedBadge');
    if (banner) { banner.style.display = related ? 'block' : 'none'; }
    if (badge) { badge.style.display = related ? 'inline-block' : 'none'; }
}

function showDetailBanner(msg, type) {
    var el = _el('detailBanner');
    if (!el) { return; }
    el.textContent = msg || '';
    el.className = type || '';
    el.style.display = msg ? 'block' : 'none';
}

function clearDetailBanner() { showDetailBanner('', ''); }

// ---------------------------------------------------------------------------
// Detail field rendering
// ---------------------------------------------------------------------------
function renderItemDetails(item) {
    var container = _el('detailDetails');
    if (!container) { return; }
    var sections = item && item.sections;
    if (!sections || !sections.length) {
        container.innerHTML = '<div style="padding:12px">No sections.</div>';
        return;
    }
    var bulkHtml = '<div class="section-bulk"><button type="button" class="btn btn-sm" onclick="expandAllSections()">Expand all</button> <button type="button" class="btn btn-sm" onclick="collapseAllSections()">Collapse all</button></div>';
    var sectionHtml = sections.map(function (sec, idx) {
        var sectionId = 'section-' + idx;
        var fields = sec.fields || [];
        var firstExpanded = (idx === 0);
        var fieldRows = fields.map(function (f) {
            var display = getFieldDisplay(f);
            return '<div class="field-row"><span class="field-label">' + escapeHtml(f.title || '') + '</span><span class="field-value">' + renderFieldValuePart(display) + '</span></div>';
        }).join('');
        var collapsedClass = firstExpanded ? '' : ' collapsed';
        return '<div class="detail-section">' +
            '<div class="section-header' + collapsedClass + '" data-section-index="' + idx + '" onclick="toggleSection(' + idx + ')"><span class="section-arrow">▼</span>' + escapeHtml(sec.title || 'Section') + '</div>' +
            '<div id="' + sectionId + '" class="section-content' + (firstExpanded ? '' : ' collapsed') + '">' + fieldRows + '</div></div>';
    }).join('');
    container.innerHTML = bulkHtml + sectionHtml;
    loadImageApiPlaceholders(container);
    if (!detailDetailsClickBound) {
        detailDetailsClickBound = true;
        container.addEventListener('click', function (e) {
            var fusionBtn = e.target.closest && e.target.closest('.open-in-fusion-icon');
            if (fusionBtn) {
                e.preventDefault(); e.stopPropagation();
                var fws = fusionBtn.getAttribute('data-workspace-id');
                var fid = fusionBtn.getAttribute('data-item-id');
                if (fws && fid) {
                    fusionBtn.disabled = true;
                    plmSend('openInFusion', { workspaceId: fws, itemId: fid })
                        .then(function () { fusionBtn.disabled = false; })
                        .catch(function () { fusionBtn.disabled = false; });
                }
                return;
            }
            var link = e.target.classList.contains('detail-item-link') ? e.target : (e.target.closest && e.target.closest('.detail-item-link'));
            if (link) {
                e.preventDefault();
                var ws = link.getAttribute('data-workspace-id');
                var iid = link.getAttribute('data-item-id');
                if (ws && iid) { drillIntoItem(ws, iid); }
                return;
            }
            var anchor = (e.target.tagName === 'A') ? e.target : (e.target.closest && e.target.closest('a'));
            if (anchor && anchor.href && /^https?:\/\//i.test(anchor.href)) {
                e.preventDefault();
                plmSend('openInBrowser', { url: anchor.href });
            }
        });
    }
}

function toggleSection(index) {
    var header = document.querySelector('.detail-section .section-header[data-section-index="' + index + '"]');
    var content = _el('section-' + index);
    if (!header || !content) { return; }
    header.classList.toggle('collapsed');
    content.classList.toggle('collapsed');
}

function expandAllSections() {
    var headers = document.querySelectorAll('#detailDetails .section-header');
    var contents = document.querySelectorAll('#detailDetails .section-content');
    for (var i = 0; i < headers.length; i++) { headers[i].classList.remove('collapsed'); }
    for (var j = 0; j < contents.length; j++) { contents[j].classList.remove('collapsed'); }
}

function collapseAllSections() {
    var headers = document.querySelectorAll('#detailDetails .section-header');
    var contents = document.querySelectorAll('#detailDetails .section-content');
    for (var i = 0; i < headers.length; i++) { headers[i].classList.add('collapsed'); }
    for (var j = 0; j < contents.length; j++) { contents[j].classList.add('collapsed'); }
}

// ---------------------------------------------------------------------------
// Field display helpers (shared: preview + detail)
// ---------------------------------------------------------------------------
var ALLOWED_TAGS = { div: 1, span: 1, p: 1, br: 1, img: 1, a: 1, b: 1, i: 1, strong: 1, em: 1 };
var ALLOWED_ATTRS = { style: 1, 'class': 1, src: 1, width: 1, height: 1, alt: 1, href: 1 };

function sanitizeHtml(html) {
    if (!html || typeof html !== 'string') { return ''; }
    var decoded = html.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&');
    var div = document.createElement('div');
    div.innerHTML = decoded;
    function sanitizeNode(node) {
        if (node.nodeType === 3) { return escapeHtml(node.textContent); }
        if (node.nodeType !== 1) { return ''; }
        var tag = node.tagName.toLowerCase();
        if (!ALLOWED_TAGS[tag]) { return ''; }
        var attrs = [];
        for (var i = 0; i < node.attributes.length; i++) {
            var a = node.attributes[i];
            var an = a.name.toLowerCase();
            if (ALLOWED_ATTRS[an]) { attrs.push(an + '="' + escapeAttr(a.value) + '"'); }
        }
        var open = '<' + tag + (attrs.length ? ' ' + attrs.join(' ') : '') + '>';
        if (tag === 'br' || tag === 'img') { return open; }
        var inner = '';
        for (var j = 0; j < node.childNodes.length; j++) { inner += sanitizeNode(node.childNodes[j]); }
        return open + inner + '</' + tag + '>';
    }
    var out = '';
    for (var k = 0; k < div.childNodes.length; k++) { out += sanitizeNode(div.childNodes[k]); }
    return out;
}

function parseItemLink(link) {
    if (!link || typeof link !== 'string') { return null; }
    var m = link.match(/\/api\/v3\/workspaces\/(\d+)\/items\/(\d+)/);
    return m ? { workspaceId: m[1], itemId: m[2] } : null;
}

function getFieldDisplay(field) {
    var value = field.value;
    if (value === null || value === undefined) { return { type: 'text', value: '—' }; }
    if (typeof value === 'number') {
        var uom = field.uom;
        return { type: 'text', value: uom ? value + ' ' + uom : String(value) };
    }
    if (Array.isArray(value)) {
        var items = [];
        for (var a = 0; a < value.length; a++) {
            var v = value[a];
            var parsed = (v && v.link) ? parseItemLink(v.link) : null;
            if (parsed && v.title !== undefined) { items.push({ workspaceId: parsed.workspaceId, itemId: parsed.itemId, title: v.title }); }
        }
        if (items.length) { return { type: 'links', items: items }; }
        var parts = value.map(function (v2) { return (v2 && v2.title !== undefined) ? v2.title : String(v2); });
        return parts.length ? { type: 'multiText', items: parts } : { type: 'text', value: '—' };
    }
    if (typeof value === 'object') {
        var linkInfo = value.link ? parseItemLink(value.link) : null;
        if (linkInfo && value.title !== undefined) { return { type: 'links', items: [{ workspaceId: linkInfo.workspaceId, itemId: linkInfo.itemId, title: value.title }] }; }
        if (value.link && typeof value.link === 'string' && /^https?:\/\//i.test(value.link)) { return { type: 'image', value: value.link }; }
        if (value.link && typeof value.link === 'string' && /\/image\/\d+/.test(value.link)) { return { type: 'imageApi', value: value.link }; }
        return { type: 'text', value: (value.title !== undefined) ? value.title : (JSON.stringify(value).substring(0, 80)) };
    }
    var s = String(value);
    if (/^\d{4}-\d{2}-\d{2}/.test(s)) {
        try {
            var d = new Date(s);
            if (!isNaN(d.getTime())) { return { type: 'text', value: d.toLocaleDateString(undefined, { dateStyle: 'medium' }) }; }
        } catch (e) {}
    }
    if (/&lt;|&gt;|&quot;/.test(s) || s.indexOf('<') !== -1) { return { type: 'html', value: s }; }
    return { type: 'text', value: s };
}

function renderFieldValuePart(display) {
    if (display.type === 'text') { return escapeHtml(display.value); }
    if (display.type === 'html') { return sanitizeHtml(display.value); }
    if (display.type === 'image') { return '<img class="detail-field-img" src="' + escapeAttr(display.value) + '" alt="">'; }
    if (display.type === 'imageApi') { return '<span class="detail-field-img-placeholder" data-link="' + escapeAttr(display.value) + '">Loading image…</span>'; }
    if (display.type === 'multiText' && display.items) {
        return display.items.map(function (t) { return '<div class="multi-value-entry">' + escapeHtml(t) + '</div>'; }).join('');
    }
    if (display.type === 'links' && display.items) {
        return display.items.map(function (it) {
            var icon = isCadWs(it.workspaceId) ? plmOpenInFusionIconHtml(it.workspaceId, it.itemId) : '';
            return '<div class="multi-value-entry">' + icon + '<a class="detail-item-link" href="#" data-workspace-id="' + escapeAttr(it.workspaceId) + '" data-item-id="' + escapeAttr(it.itemId) + '">' + escapeHtml(it.title) + '</a></div>';
        }).join('');
    }
    return '—';
}

function getFieldKeyFromSelf(selfRef) {
    if (!selfRef || typeof selfRef !== 'string') { return ''; }
    var parts = selfRef.split('/fields/');
    if (parts.length < 2) { return ''; }
    return parts[parts.length - 1].split('/')[0] || '';
}

function getItemFieldObjects(item) {
    var map = {};
    if (!item || !item.sections) { return map; }
    for (var s = 0; s < item.sections.length; s++) {
        var fields = item.sections[s].fields;
        if (!fields) { continue; }
        for (var i = 0; i < fields.length; i++) {
            var f = fields[i];
            var key = getFieldKeyFromSelf(f.__self__ || '');
            if (!key) { key = (f.title || '').toString().replace(/\s+/g, '_').toUpperCase(); }
            if (key) { map[key] = f; }
        }
    }
    return map;
}

function loadImageApiPlaceholders(container) {
    if (!container) { return; }
    var placeholders = container.querySelectorAll('.detail-field-img-placeholder');
    if (!placeholders.length) { return; }
    var links = [];
    for (var i = 0; i < placeholders.length; i++) {
        var link = placeholders[i].getAttribute('data-link');
        if (link) { links.push(link); }
    }
    if (!links.length) { return; }
    plmSend('getImageUrls', { links: links }).then(function (data) {
        var urlByLink = (data && data.urlByLink) || {};
        for (var j = 0; j < placeholders.length; j++) {
            var pl = placeholders[j];
            var lk = pl.getAttribute('data-link');
            var url = urlByLink[lk];
            if (url) {
                var img = document.createElement('img');
                img.className = 'detail-field-img'; img.alt = ''; img.src = url;
                pl.parentNode.replaceChild(img, pl);
            } else {
                pl.textContent = 'Image unavailable';
                pl.className = 'detail-field-img-failed';
            }
        }
    }).catch(function () {
        for (var k = 0; k < placeholders.length; k++) {
            placeholders[k].textContent = 'Image unavailable';
            placeholders[k].className = 'detail-field-img-failed';
        }
    });
}

// ---------------------------------------------------------------------------
// CAD-context matching
// ---------------------------------------------------------------------------
function ensureLineageContext() {
    return plmSend('getLineageUrns', {}).then(function (urnResult) {
        var urns = (urnResult && urnResult.success && urnResult.urns && urnResult.urns.length) ? urnResult.urns : [];
        if (urns.length === 0) { return { urns: [], itemIds: [] }; }
        var urnsKey = urns.join('|');
        if (lineageContextCache.urnsKey === urnsKey) {
            return { urns: urns, itemIds: lineageContextCache.itemIds };
        }
        return plmSend('searchResultsForLineage', { urns: urns }).then(function (searchResult) {
            var itemIds = (searchResult && searchResult.success && searchResult.itemIds) ? searchResult.itemIds : [];
            lineageContextCache = { urnsKey: urnsKey, itemIds: itemIds };
            return { urns: urns, itemIds: itemIds };
        }).catch(function () {
            lineageContextCache = { urnsKey: urnsKey, itemIds: [] };
            return { urns: urns, itemIds: [] };
        });
    }).catch(function () { return { urns: [], itemIds: [] }; });
}

// ---------------------------------------------------------------------------
// Theme + prefs
// ---------------------------------------------------------------------------
function applyThemeAndPrefs() {
    plmSend('getTheme', {}).then(function (result) {
        if (result && result.themeName && typeof plmApplyTheme === 'function') {
            plmApplyTheme(result.themeName);
        }
    });
    plmSend('getUiPrefs', {}).then(function (result) {
        if (result && result.prefs && result.prefs.textSize) {
            document.body.className = document.body.className.replace(/pref-text-\S+/g, '').trim();
            document.body.className += ' pref-text-' + result.prefs.textSize;
        }
        if (typeof plmUiPrefs !== 'undefined' && plmUiPrefs.mountAll) {
            plmUiPrefs.mountAll();
        }
    });
}

// ---------------------------------------------------------------------------
// Push handler (Python → JS)
// ---------------------------------------------------------------------------
window.fusionJavaScriptHandler = {
    handle: function (action, data) {
        try {
            if (typeof plmHandleAsyncResult === 'function' && plmHandleAsyncResult(action, data)) {
                return 'OK';
            }
            if (action === 'tokenResult') {
                _authPoller.stop();
                loadTasks();
            } else if (typeof plmUiPrefs !== 'undefined' && plmUiPrefs.handlePythonMessage) {
                plmUiPrefs.handlePythonMessage(action, data);
            }
        } catch (e) {}
        return 'OK';
    }
};

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
function init() {
    // Restore auto-refresh setting
    try {
        var a = localStorage.getItem(AUTO_REFRESH_STORAGE_KEY);
        var mins = parseInt(a, 10);
        if (!isNaN(mins) && mins >= 0) {
            var sel = _el('autoRefreshSelect');
            if (sel) { sel.value = String(mins); setAutoRefreshInterval(); }
        }
    } catch (e) {}

    // Wire buttons
    var btnRefresh = _el('btnRefresh');
    if (btnRefresh) { btnRefresh.addEventListener('click', loadTasks); }

    var searchInput = _el('searchInput');
    if (searchInput) { searchInput.addEventListener('input', onSearchInput); }

    // Apply theme + prefs, then load tasks
    applyThemeAndPrefs();
    loadTasks();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
