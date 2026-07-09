/* Export Electronics BOM to PLM — wizard: Extract -> Configure -> Preview -> Push.
 * Backend actions: getElectronicsBom (sync), getExportConfig/saveExportConfig (sync),
 * listWorkspaces/getMappingFields (async), resolveBomPlan/pushBom (async).
 */
(function () {
    'use strict';

    var STEPS = ['extract', 'configure', 'preview', 'results'];
    var ITEM_MAP_FIELDS = [   // electronics BOM field -> Items workspace field (mapped in UI)
        { key: 'value', label: 'Value' },
        { key: 'footprint', label: 'Footprint' },
        { key: 'description', label: 'Description' },
        { key: 'mpn', label: 'MPN (text copy)' },
        { key: 'manufacturer', label: 'Manufacturer (text copy)' }
    ];

    var _bom = null, _config = null, _plan = null, _wsList = [], _itemFields = [];
    var _step = 'extract', _authPoller = null, _busy = false;

    // ---- helpers ----------------------------------------------------------
    function el(tag, attrs, text) {
        var e = document.createElement(tag);
        if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
        if (text != null) e.textContent = text;
        return e;
    }
    function byId(id) { return document.getElementById(id); }
    function pill(status) { var s = el('span', { class: 'pill ' + status }, status); return s; }
    function stat(num, lbl) {
        var d = el('div', { class: 'stat' });
        d.appendChild(el('div', { class: 'num' }, String(num)));
        d.appendChild(el('div', { class: 'lbl' }, lbl));
        return d;
    }
    function cellText(v) {
        var td = el('td');
        if (v == null || v === '') { td.className = 'missing'; td.textContent = '(none)'; }
        else td.textContent = v;
        return td;
    }
    function status(msg, isError) {
        var s = byId('statusMsg');
        s.textContent = msg || '';
        s.className = 'state-msg' + (isError ? ' error' : '');
        s.style.display = msg ? 'block' : 'none';
    }
    function foot(msg) { byId('footMsg').textContent = msg || ''; }

    // ---- step navigation --------------------------------------------------
    function showPanel(step) {
        STEPS.forEach(function (s) {
            var p = byId('panel-' + s);
            if (p) p.classList.toggle('active', s === step);
        });
        var idx = STEPS.indexOf(step);
        Array.prototype.forEach.call(byId('steps').children, function (node) {
            var s = node.getAttribute('data-step'); var i = STEPS.indexOf(s);
            node.classList.toggle('active', s === step);
            node.classList.toggle('done', i < idx);
        });
    }

    var NEXT_LABEL = { extract: 'Next: Configure', configure: 'Save & Preview',
                       preview: 'Push to PLM ⟶', results: 'Re-extract' };

    function goStep(step) {
        _step = step;
        showPanel(step);
        byId('btnBack').style.display = (step === 'extract') ? 'none' : 'inline-flex';
        var next = byId('btnNext');
        next.textContent = NEXT_LABEL[step];
        next.className = 'btn ' + (step === 'preview' ? 'btn-primary' : 'btn-primary');
        next.disabled = false;
        foot('');
        if (step === 'configure') enterConfigure();
        if (step === 'preview') enterPreview();
    }

    function onNext() {
        if (_busy) return;
        if (_step === 'extract') { goStep('configure'); }
        else if (_step === 'configure') { saveConfigThenPreview(); }
        else if (_step === 'preview') { doPush(); }
        else if (_step === 'results') { location.reload(); }
    }
    function onBack() {
        if (_busy) return;
        var idx = STEPS.indexOf(_step);
        if (idx > 0) goStep(STEPS[idx - 1]);
    }

    // ---- step 1: Extract --------------------------------------------------
    function extract() {
        status('Reading the active electronics design…');
        plmSend('getElectronicsBom', {}).then(function (r) {
            var d = plmParse(r);
            if (!d.success) { status(d.error || 'Could not extract the BOM.', true); return; }
            _bom = d; status('');
            byId('designName').textContent = d.design ? ('Design: ' + d.design) : '';
            renderExtract(d);
            goStep('extract');
        }).catch(function (e) { status('Error: ' + e, true); });
    }

    function renderExtract(d) {
        var rows = d.rows || [];
        var sum = byId('extractSummary'); sum.innerHTML = '';
        sum.appendChild(stat(rows.length, 'BOM lines'));
        sum.appendChild(stat(rows.reduce(function (a, r) { return a + (r.quantity || 0); }, 0), 'Components'));
        sum.appendChild(stat(d.partCount || 0, 'Parts scanned'));
        sum.appendChild(stat(d.skipped || 0, 'Skipped'));
        var w = byId('extractWarnings'); w.innerHTML = '';
        (d.warnings || []).forEach(function (m) { w.appendChild(el('div', { class: 'warn' }, '⚠ ' + m)); });
        var wrap = byId('extractTable'); wrap.innerHTML = '';
        if (!rows.length) { wrap.appendChild(el('div', { class: 'state-msg' }, 'No BOM lines.')); return; }
        var t = el('table', { class: 'grid' }), thead = el('thead'), htr = el('tr');
        ['Ref Designators', 'Qty', 'Value', 'Footprint', 'MPN', 'Manufacturer']
            .forEach(function (h) { htr.appendChild(el('th', null, h)); });
        thead.appendChild(htr); t.appendChild(thead);
        var tb = el('tbody');
        rows.forEach(function (r) {
            var tr = el('tr');
            tr.appendChild(cellText((r.referenceDesignators || []).join(', ')));
            var q = el('td', { class: 'qty' }, String(r.quantity || 0)); tr.appendChild(q);
            tr.appendChild(cellText(r.value)); tr.appendChild(cellText(r.footprint));
            tr.appendChild(cellText(r.mpn)); tr.appendChild(cellText(r.manufacturer));
            tb.appendChild(tr);
        });
        t.appendChild(tb); wrap.appendChild(t);
    }

    // ---- step 2: Configure ------------------------------------------------
    function enterConfigure() {
        // workspace pickers
        plmSend('listWorkspaces', {}).then(function (r) {
            var d = plmParse(r);
            if (!d.success) { foot(d.error || 'Could not list workspaces.'); if (d.unauthorized && _authPoller) _authPoller.start(); return; }
            _wsList = d.workspaces || [];
            ['wsItems', 'wsMpn', 'wsSupplier'].forEach(function (id, i) {
                var key = ['itemsWs', 'mpnWs', 'supplierWs'][i];
                var sel = byId(id); sel.innerHTML = '';
                sel.appendChild(el('option', { value: '' }, '— none —'));
                _wsList.forEach(function (w) {
                    var o = el('option', { value: w.systemName }, w.title + '  (' + w.systemName + ')');
                    if (w.systemName === _config[key]) o.setAttribute('selected', 'selected');
                    sel.appendChild(o);
                });
                sel.value = _config[key] || '';
            });
            byId('wsItems').onchange = function () { _config.itemsWs = this.value; loadItemFields(); };
            byId('wsMpn').onchange = function () { _config.mpnWs = this.value; };
            byId('wsSupplier').onchange = function () { _config.supplierWs = this.value; };
            loadItemFields();
        }).catch(function (e) { foot('Error: ' + e); });
        byId('parentKeyField').value = _config.parentKeyField || '';
        byId('titleTemplate').value = _config.titleTemplate || '{mpn}';
    }

    function loadItemFields() {
        var wrap = byId('itemMapRows'); wrap.innerHTML = '';
        if (!_config.itemsWs) { wrap.appendChild(el('div', { class: 'warn' }, 'Pick the Items workspace first.')); return; }
        wrap.appendChild(el('div', { class: 'warn' }, 'Loading fields…'));
        plmSend('getMappingFields', { systemName: _config.itemsWs }).then(function (r) {
            var d = plmParse(r);
            wrap.innerHTML = '';
            if (!d.success) { wrap.appendChild(el('div', { class: 'warn' }, d.error || 'Could not load fields.')); return; }
            _itemFields = (d.fields || []).filter(function (f) { return f.editability !== 'NEVER'; });
            ITEM_MAP_FIELDS.forEach(function (mf) {
                var row = el('div', { class: 'cfg-row' });
                row.appendChild(el('label', null, mf.label));
                var sel = el('select');
                sel.appendChild(el('option', { value: '' }, '— skip —'));
                _itemFields.forEach(function (f) {
                    var o = el('option', { value: f.id }, (f.title || f.id) + '  (' + f.id + ')');
                    sel.appendChild(o);
                });
                sel.value = (_config.itemMapping || {})[mf.key] || '';
                sel.onchange = function () { _config.itemMapping = _config.itemMapping || {}; _config.itemMapping[mf.key] = this.value; };
                row.appendChild(sel); wrap.appendChild(row);
            });
        }).catch(function (e) { wrap.innerHTML = ''; wrap.appendChild(el('div', { class: 'warn' }, 'Error: ' + e)); });
    }

    function saveConfigThenPreview() {
        _config.parentKeyField = byId('parentKeyField').value.trim() || 'SOURCE_DESIGN_ID';
        _config.titleTemplate = byId('titleTemplate').value.trim() || '{mpn}';
        if (!_config.itemsWs) { byId('cfgMsg').textContent = 'Pick the Items workspace.'; return; }
        _busy = true; foot('Saving…');
        plmSend('saveExportConfig', { config: _config }).then(function (r) {
            var d = plmParse(r); _busy = false; foot('');
            if (d.success && d.config) _config = d.config;
            goStep('preview');
        }).catch(function (e) { _busy = false; byId('cfgMsg').textContent = 'Error: ' + e; });
    }

    // ---- step 3: Preview --------------------------------------------------
    function enterPreview() {
        var next = byId('btnNext'); next.disabled = true;
        byId('planCounts').innerHTML = '';
        byId('planTable').innerHTML = '<div class="state-msg">Resolving plan against Fusion Manage…</div>';
        byId('planRemovals').innerHTML = '';
        _busy = true;
        plmSend('resolveBomPlan', { bom: _bom, config: _config }).then(function (r) {
            var d = plmParse(r); _busy = false;
            if (!d.success) {
                byId('planTable').innerHTML = '';
                foot(d.error || 'Resolve failed.');
                if (d.unauthorized && _authPoller) _authPoller.start();
                byId('planTable').appendChild(el('div', { class: 'state-msg error' }, d.error || 'Resolve failed.'));
                return;
            }
            _plan = d; renderPlan(d); next.disabled = false;
        }).catch(function (e) { _busy = false; byId('planTable').innerHTML = ''; byId('planTable').appendChild(el('div', { class: 'state-msg error' }, 'Error: ' + e)); });
    }

    function renderPlan(d) {
        var c = d.counts || {};
        var cnt = byId('planCounts'); cnt.innerHTML = '';
        cnt.appendChild(stat(c.itemsCreate || 0, 'Items new'));
        cnt.appendChild(stat(c.itemsUpdate || 0, 'Items update'));
        cnt.appendChild(stat(c.mpnCreate || 0, 'MPNs new'));
        cnt.appendChild(stat(c.bomAdd || 0, 'BOM +add'));
        cnt.appendChild(stat(c.bomUpdate || 0, 'BOM ~qty'));
        cnt.appendChild(stat(c.bomRemove || 0, 'BOM −remove'));
        var p = d.parent || {};
        byId('planParent').textContent = 'Parent PCBA: ' + (p.status === 'existing'
            ? ('existing item ' + (p.itemId || '') + ' (' + (p.existingRows || 0) + ' current rows)')
            : 'will be created') + ' · key "' + (d.designId || '') + '"';

        var wrap = byId('planTable'); wrap.innerHTML = '';
        var t = el('table', { class: 'grid' }), thead = el('thead'), htr = el('tr');
        ['MPN', 'Value', 'Footprint', 'Qty', 'Item', 'MPN rec', 'BOM'].forEach(function (h) { htr.appendChild(el('th', null, h)); });
        thead.appendChild(htr); t.appendChild(thead);
        var tb = el('tbody');
        (d.lines || []).forEach(function (ln) {
            var tr = el('tr');
            tr.appendChild(cellText(ln.mpn)); tr.appendChild(cellText(ln.value));
            tr.appendChild(cellText(ln.footprint));
            tr.appendChild(el('td', { class: 'qty' }, String(ln.quantity || 0)));
            var tdI = el('td'); tdI.appendChild(pill(ln.itemStatus)); tr.appendChild(tdI);
            var tdM = el('td'); tdM.appendChild(pill(ln.mpnStatus)); tr.appendChild(tdM);
            var tdB = el('td'); tdB.appendChild(pill(ln.bomStatus || 'noop')); tr.appendChild(tdB);
            tb.appendChild(tr);
            if ((ln.warnings || []).length) {
                var wtr = el('tr'); var wtd = el('td', { colspan: '7', class: 'warn' }, '⚠ ' + ln.warnings.join('; '));
                wtr.appendChild(wtd); tb.appendChild(wtr);
            }
        });
        t.appendChild(tb); wrap.appendChild(t);

        var rem = byId('planRemovals'); rem.innerHTML = '';
        if ((d.removals || []).length) {
            rem.appendChild(el('div', { class: 'cfg-section-title' }, 'Rows to remove (full sync)'));
            d.removals.forEach(function (r) {
                rem.appendChild(el('div', { class: 'warn' }, '− item ' + r.childId + ' (qty ' + r.quantity + ')'));
            });
        }
        foot('Review, then push. Nothing is written until you click Push.');
    }

    // ---- step 4: Push -----------------------------------------------------
    function doPush() {
        _busy = true; var next = byId('btnNext'); next.disabled = true;
        goStep('results');
        byId('pushCounts').innerHTML = '';
        byId('pushWarnings').innerHTML = '';
        byId('pushTable').innerHTML = '<div class="state-msg">Writing to Fusion Manage…</div>';
        plmSend('pushBom', { bom: _bom, config: _config }).then(function (r) {
            var d = plmParse(r); _busy = false; next.disabled = false;
            if (!d.success && !d.results) {
                byId('pushTable').innerHTML = '';
                byId('pushTable').appendChild(el('div', { class: 'state-msg error' }, d.error || 'Push failed.'));
                return;
            }
            renderResults(d);
        }).catch(function (e) { _busy = false; next.disabled = false; byId('pushTable').innerHTML = ''; byId('pushTable').appendChild(el('div', { class: 'state-msg error' }, 'Error: ' + e)); });
    }

    function renderResults(d) {
        var c = d.counts || {};
        var cnt = byId('pushCounts'); cnt.innerHTML = '';
        cnt.appendChild(stat(c.itemsCreated || 0, 'Items new'));
        cnt.appendChild(stat(c.itemsUpdated || 0, 'Items upd'));
        cnt.appendChild(stat(c.mpnCreated || 0, 'MPNs new'));
        cnt.appendChild(stat(c.bomAdded || 0, 'BOM +'));
        cnt.appendChild(stat(c.bomUpdated || 0, 'BOM ~'));
        cnt.appendChild(stat(c.bomRemoved || 0, 'BOM −'));
        cnt.appendChild(stat(c.failed || 0, 'Failed'));
        var w = byId('pushWarnings'); w.innerHTML = '';
        if (d.parent && d.parent.itemId) w.appendChild(el('div', { class: 'warn' }, 'Parent item: ' + d.parent.itemId));
        (d.warnings || []).forEach(function (m) { w.appendChild(el('div', { class: 'warn' }, '⚠ ' + m)); });
        var wrap = byId('pushTable'); wrap.innerHTML = '';
        var t = el('table', { class: 'grid' }), thead = el('thead'), htr = el('tr');
        ['MPN', 'Title', 'Action', 'Result', 'Detail'].forEach(function (h) { htr.appendChild(el('th', null, h)); });
        thead.appendChild(htr); t.appendChild(thead);
        var tb = el('tbody');
        (d.results || []).forEach(function (res) {
            var tr = el('tr');
            tr.appendChild(cellText(res.mpn)); tr.appendChild(cellText(res.title));
            tr.appendChild(cellText(res.action));
            var tdR = el('td'); tdR.appendChild(pill(res.ok ? 'ok' : 'err')); tr.appendChild(tdR);
            tr.appendChild(cellText(res.error || (res.itemId ? ('item ' + res.itemId) : '')));
            tb.appendChild(tr);
        });
        t.appendChild(tb); wrap.appendChild(t);
        foot('Done. Re-run "Re-extract" to sync again after design changes.');
    }

    // ---- Python -> JS push ------------------------------------------------
    window.fusionJavaScriptHandler = {
        handle: function (action, data) {
            try {
                if (typeof plmHandleAsyncResult === 'function' && plmHandleAsyncResult(action, data)) return 'OK';
                if (action === 'tokenResult') { if (_authPoller) _authPoller.stop(); if (!_bom) extract(); }
            } catch (e) { /* ignore */ }
            return 'OK';
        }
    };

    function init() {
        if (typeof plmRequestTheme === 'function') plmRequestTheme();
        _authPoller = (typeof plmCreateAuthPoller === 'function') ? plmCreateAuthPoller(function () { if (!_bom) extract(); }) : null;
        byId('btnNext').onclick = onNext;
        byId('btnBack').onclick = onBack;
        // load config, then extract
        plmSend('getExportConfig', {}).then(function (r) {
            var d = plmParse(r);
            _config = (d && d.config) ? d.config : {};
            extract();
        }).catch(function () { _config = {}; extract(); });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
