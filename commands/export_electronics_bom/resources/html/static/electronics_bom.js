/* Export Electronics BOM — v1 palette logic (read & display + export-prep preview).
 * Calls the SYNC action getElectronicsBom (Python reads adsk.electron on the main thread)
 * and renders the grouped BOM plus the normalized rows destined for Fusion Manage.
 */
(function () {
    'use strict';

    var _lastRows = null;

    function el(tag, attrs, text) {
        var e = document.createElement(tag);
        if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
        if (text != null) e.textContent = text;
        return e;
    }
    function esc(s) {
        return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    function cell(val) {
        var td = el('td');
        if (val == null || val === '') { td.className = 'missing'; td.textContent = '(missing)'; }
        else td.textContent = val;
        return td;
    }

    function setStatus(msg, isError) {
        var s = document.getElementById('statusMsg');
        var c = document.getElementById('content');
        if (msg) {
            s.textContent = msg;
            s.className = 'state-msg' + (isError ? ' error' : '');
            s.style.display = 'block';
            if (c) c.style.display = 'none';
        } else {
            s.style.display = 'none';
            if (c) c.style.display = 'block';
        }
    }

    function stat(num, lbl) {
        var d = el('div', { class: 'stat' });
        d.appendChild(el('div', { class: 'num' }, String(num)));
        d.appendChild(el('div', { class: 'lbl' }, lbl));
        return d;
    }

    function render(data) {
        setStatus('', false);
        var rows = data.rows || [];
        _lastRows = rows;

        var summary = document.getElementById('summary');
        summary.innerHTML = '';
        summary.appendChild(stat(rows.length, 'BOM lines'));
        var totalQty = rows.reduce(function (a, r) { return a + (r.quantity || 0); }, 0);
        summary.appendChild(stat(totalQty, 'Components'));
        summary.appendChild(stat(data.partCount || 0, 'Parts scanned'));
        summary.appendChild(stat(data.skipped || 0, 'Skipped'));

        var note = document.getElementById('exportNote');
        note.textContent = 'Design: ' + (data.design || '(untitled)') +
            ' — these ' + rows.length + ' normalized rows are ready to push to Fusion Manage ' +
            '(v1 preview: extraction only, no write yet).';

        var warnWrap = document.getElementById('warnings');
        warnWrap.innerHTML = '';
        (data.warnings || []).forEach(function (w) {
            warnWrap.appendChild(el('div', { class: 'warn' }, '⚠ ' + w));
        });

        var wrap = document.getElementById('tableWrap');
        wrap.innerHTML = '';
        if (!rows.length) {
            wrap.appendChild(el('div', { class: 'state-msg' }, 'No BOM lines extracted.'));
        } else {
            var table = el('table', { class: 'bom' });
            var thead = el('thead');
            var htr = el('tr');
            ['Reference Designators', 'Qty', 'Value', 'Footprint', 'MPN', 'Manufacturer']
                .forEach(function (h) { htr.appendChild(el('th', null, h)); });
            thead.appendChild(htr); table.appendChild(thead);
            var tbody = el('tbody');
            rows.forEach(function (r) {
                var tr = el('tr');
                tr.appendChild(cell((r.referenceDesignators || []).join(', ')));
                var q = el('td', { class: 'qty' }, String(r.quantity || 0));
                tr.appendChild(q);
                tr.appendChild(cell(r.value));
                tr.appendChild(cell(r.footprint));
                tr.appendChild(cell(r.mpn));
                tr.appendChild(cell(r.manufacturer));
                tbody.appendChild(tr);
            });
            table.appendChild(tbody); wrap.appendChild(table);
        }

        document.getElementById('diagJson').textContent = JSON.stringify(rows, null, 2);
        document.getElementById('btnCopy').style.display = rows.length ? 'inline-flex' : 'none';
    }

    function extract() {
        setStatus('Reading the active electronics design…', false);
        plmSend('getElectronicsBom', {}).then(function (r) {
            var d = plmParse(r);
            if (!d.success) { setStatus(d.error || 'Could not extract the BOM.', true); return; }
            render(d);
        }).catch(function (e) { setStatus('Error: ' + e, true); });
    }

    window.fusionJavaScriptHandler = {
        handle: function (action, data) {
            try {
                if (typeof plmHandleAsyncResult === 'function' && plmHandleAsyncResult(action, data)) {
                    return 'OK';
                }
            } catch (e) { /* ignore */ }
            return 'OK';
        }
    };

    function init() {
        if (typeof plmRequestTheme === 'function') plmRequestTheme();
        var b = document.getElementById('btnExtract');
        if (b) b.onclick = extract;
        var copy = document.getElementById('btnCopy');
        if (copy) copy.onclick = function () {
            if (!_lastRows) return;
            var txt = JSON.stringify(_lastRows, null, 2);
            try {
                if (navigator.clipboard) { navigator.clipboard.writeText(txt); }
                else {
                    var ta = document.createElement('textarea');
                    ta.value = txt; document.body.appendChild(ta); ta.select();
                    document.execCommand('copy'); document.body.removeChild(ta);
                }
                copy.textContent = 'Copied!';
                setTimeout(function () { copy.textContent = 'Copy JSON'; }, 1500);
            } catch (e) { /* ignore */ }
        };
        extract();
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
