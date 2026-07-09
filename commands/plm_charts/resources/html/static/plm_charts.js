/* PLM Charts — dashboard palette logic + inline-SVG chart renderer.
 * Consumes the normalized chart shape from services/reports.py:
 *   { id, name, type, title, xLabel, yLabel, seriesLabel, categories:[..],
 *     series:[ {name, points:[ {x, y} ]} ], hasData }
 * No external chart lib — everything is hand-rolled SVG so the palette has zero deps.
 */
(function () {
    'use strict';

    var SVG_NS = 'http://www.w3.org/2000/svg';
    // Categorical palette (brand-neutral, readable in light + dark), by series index.
    var PALETTE = ['#0696d7', '#f2a900', '#5cb85c', '#e8613c', '#8a5cf6',
                   '#12a594', '#d6409f', '#eab308', '#64748b', '#2563eb'];

    var W = 420, H = 250;
    var M = { l: 46, r: 14, t: 14, b: 52 };
    var _authPoller = null;

    // ---- small DOM helpers ------------------------------------------------
    function el(tag, attrs, text) {
        var e = document.createElement(tag);
        if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
        if (text != null) e.textContent = text;
        return e;
    }
    function svgEl(tag, attrs) {
        var e = document.createElementNS(SVG_NS, tag);
        if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
        return e;
    }
    function esc(s) {
        return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }
    function color(i) { return PALETTE[i % PALETTE.length]; }
    function fmtNum(n) {
        if (n == null) return '';
        if (Math.abs(n) >= 1000000) return (n / 1000000).toFixed(1) + 'M';
        if (Math.abs(n) >= 1000) return (n / 1000).toFixed(1) + 'k';
        return (Math.round(n * 100) / 100).toString();
    }
    function niceMax(v) {
        if (v <= 0) return 1;
        var pow = Math.pow(10, Math.floor(Math.log10(v)));
        var n = v / pow;
        var step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
        return step * pow;
    }
    function truncate(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

    // ---- chart type routing ----------------------------------------------
    function normType(t) { return String(t || 'COLUMN').toUpperCase(); }
    function isBar(t) { return t.indexOf('BAR') !== -1; }
    function isLineOrArea(t) { return t.indexOf('LINE') !== -1 || t.indexOf('SPLINE') !== -1 || t.indexOf('AREA') !== -1; }
    function isArea(t) { return t.indexOf('AREA') !== -1; }
    function isPie(t) { return t.indexOf('PIE') !== -1 || t.indexOf('DOUGHNUT') !== -1 || t.indexOf('DONUT') !== -1; }
    function isStacked(t) { return t.indexOf('STACK') !== -1; }

    // ---- axis frame -------------------------------------------------------
    function drawFrame(svg, yMax, plotW, plotH, horizontal) {
        // gridlines + y ticks (value axis)
        var ticks = 4;
        for (var i = 0; i <= ticks; i++) {
            var val = yMax * i / ticks;
            if (!horizontal) {
                var y = M.t + plotH - (plotH * i / ticks);
                svg.appendChild(svgEl('line', { x1: M.l, y1: y, x2: M.l + plotW, y2: y,
                    stroke: 'var(--divider)', 'stroke-width': i === 0 ? 1 : 0.5 }));
                var lbl = svgEl('text', { x: M.l - 6, y: y + 3, 'text-anchor': 'end',
                    'font-size': 9, fill: 'var(--ink-muted)' });
                lbl.textContent = fmtNum(val); svg.appendChild(lbl);
            } else {
                var x = M.l + (plotW * i / ticks);
                svg.appendChild(svgEl('line', { x1: x, y1: M.t, x2: x, y2: M.t + plotH,
                    stroke: 'var(--divider)', 'stroke-width': i === 0 ? 1 : 0.5 }));
                var lbx = svgEl('text', { x: x, y: M.t + plotH + 12, 'text-anchor': 'middle',
                    'font-size': 9, fill: 'var(--ink-muted)' });
                lbx.textContent = fmtNum(val); svg.appendChild(lbx);
            }
        }
    }

    function catLabel(svg, x, y, text, rotate) {
        var t = svgEl('text', { x: x, y: y, 'text-anchor': rotate ? 'end' : 'middle',
            'font-size': 9, fill: 'var(--ink-label)' });
        if (rotate) t.setAttribute('transform', 'rotate(-35 ' + x + ' ' + y + ')');
        t.textContent = truncate(text, 16);
        svg.appendChild(t);
    }

    // ---- renderers --------------------------------------------------------
    function renderColumns(chart, type) {
        var cats = chart.categories, series = chart.series;
        var stacked = isStacked(type) && series.length > 1;
        var plotW = W - M.l - M.r, plotH = H - M.t - M.b;
        var svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img' });
        // y max
        var yMax;
        if (stacked) {
            yMax = 0;
            cats.forEach(function (c, ci) {
                var sum = 0; series.forEach(function (s) { sum += (s.points[ci] || {}).y || 0; });
                yMax = Math.max(yMax, sum);
            });
        } else {
            yMax = 0; series.forEach(function (s) { s.points.forEach(function (p) { yMax = Math.max(yMax, p.y || 0); }); });
        }
        yMax = niceMax(yMax || 1);
        drawFrame(svg, yMax, plotW, plotH, false);
        var band = plotW / Math.max(cats.length, 1);
        var rotate = cats.length > 5;
        cats.forEach(function (c, ci) {
            var cx = M.l + band * ci;
            if (stacked) {
                var acc = 0;
                series.forEach(function (s, si) {
                    var v = (s.points[ci] || {}).y || 0;
                    var h = plotH * v / yMax;
                    var y = M.t + plotH - (plotH * (acc + v) / yMax);
                    acc += v;
                    if (v > 0) svg.appendChild(svgEl('rect', { x: cx + band * 0.15, y: y,
                        width: band * 0.7, height: h, fill: color(si), rx: 1 }));
                });
            } else {
                var groupW = band * 0.7, bw = groupW / series.length;
                series.forEach(function (s, si) {
                    var v = (s.points[ci] || {}).y || 0;
                    var h = plotH * v / yMax;
                    var y = M.t + plotH - h;
                    svg.appendChild(svgEl('rect', { x: cx + band * 0.15 + bw * si, y: y,
                        width: Math.max(bw - 1, 1), height: h, fill: color(si), rx: 1 }));
                    if (cats.length <= 6 && series.length === 1 && v > 0) {
                        var vl = svgEl('text', { x: cx + band / 2, y: y - 3, 'text-anchor': 'middle',
                            'font-size': 9, fill: 'var(--ink-label)' });
                        vl.textContent = fmtNum(v); svg.appendChild(vl);
                    }
                });
            }
            catLabel(svg, cx + band / 2, M.t + plotH + (rotate ? 12 : 14), c, rotate);
        });
        return svg;
    }

    function renderBars(chart) {
        var cats = chart.categories, series = chart.series;
        var s0 = series[0] || { points: [] };
        var plotW = W - M.l - M.r, plotH = H - M.t - M.b;
        var svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img' });
        var yMax = 0; s0.points.forEach(function (p) { yMax = Math.max(yMax, p.y || 0); });
        yMax = niceMax(yMax || 1);
        drawFrame(svg, yMax, plotW, plotH, true);
        var band = plotH / Math.max(cats.length, 1);
        cats.forEach(function (c, ci) {
            var v = (s0.points[ci] || {}).y || 0;
            var w = plotW * v / yMax;
            var y = M.t + band * ci + band * 0.15;
            svg.appendChild(svgEl('rect', { x: M.l, y: y, width: w, height: band * 0.7,
                fill: color(0), rx: 1 }));
            var lbl = svgEl('text', { x: M.l - 4, y: y + band * 0.35 + 3, 'text-anchor': 'end',
                'font-size': 9, fill: 'var(--ink-label)' });
            lbl.textContent = truncate(c, 14); svg.appendChild(lbl);
        });
        return svg;
    }

    function renderLineArea(chart, type) {
        var cats = chart.categories, series = chart.series;
        var area = isArea(type);
        var plotW = W - M.l - M.r, plotH = H - M.t - M.b;
        var svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img' });
        var yMax = 0; series.forEach(function (s) { s.points.forEach(function (p) { yMax = Math.max(yMax, p.y || 0); }); });
        yMax = niceMax(yMax || 1);
        drawFrame(svg, yMax, plotW, plotH, false);
        var n = Math.max(cats.length - 1, 1);
        var xAt = function (i) { return M.l + (cats.length === 1 ? plotW / 2 : plotW * i / n); };
        var yAt = function (v) { return M.t + plotH - (plotH * (v || 0) / yMax); };
        series.forEach(function (s, si) {
            var pts = s.points.map(function (p, i) { return xAt(i) + ',' + yAt(p.y); });
            if (area) {
                var poly = pts.join(' ') + ' ' + xAt(cats.length - 1) + ',' + (M.t + plotH) +
                    ' ' + xAt(0) + ',' + (M.t + plotH);
                svg.appendChild(svgEl('polygon', { points: poly, fill: color(si),
                    'fill-opacity': 0.18, stroke: 'none' }));
            }
            svg.appendChild(svgEl('polyline', { points: pts.join(' '), fill: 'none',
                stroke: color(si), 'stroke-width': 1.8, 'stroke-linejoin': 'round' }));
            s.points.forEach(function (p, i) {
                svg.appendChild(svgEl('circle', { cx: xAt(i), cy: yAt(p.y), r: 2.2, fill: color(si) }));
            });
        });
        var rotate = cats.length > 5;
        var stepLbl = Math.ceil(cats.length / 8);
        cats.forEach(function (c, i) {
            if (i % stepLbl !== 0 && i !== cats.length - 1) return;
            catLabel(svg, xAt(i), M.t + plotH + (rotate ? 12 : 14), c, rotate);
        });
        return svg;
    }

    function renderPie(chart, type) {
        var pts = (chart.series[0] || { points: [] }).points.filter(function (p) { return (p.y || 0) > 0; });
        var total = pts.reduce(function (a, p) { return a + (p.y || 0); }, 0);
        var svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img' });
        var cx = W * 0.36, cy = H / 2, r = Math.min(H, W * 0.6) / 2 - 12;
        var inner = type.indexOf('PIE') !== -1 ? 0 : r * 0.55;
        var ang = -Math.PI / 2;
        pts.forEach(function (p, i) {
            var frac = p.y / total, a2 = ang + frac * Math.PI * 2;
            var large = frac > 0.5 ? 1 : 0;
            var x1 = cx + r * Math.cos(ang), y1 = cy + r * Math.sin(ang);
            var x2 = cx + r * Math.cos(a2), y2 = cy + r * Math.sin(a2);
            var d;
            if (pts.length === 1) {
                d = 'M ' + (cx - r) + ' ' + cy + ' A ' + r + ' ' + r + ' 0 1 1 ' + (cx + r) + ' ' + cy +
                    ' A ' + r + ' ' + r + ' 0 1 1 ' + (cx - r) + ' ' + cy + ' Z';
            } else {
                d = 'M ' + cx + ' ' + cy + ' L ' + x1 + ' ' + y1 +
                    ' A ' + r + ' ' + r + ' 0 ' + large + ' 1 ' + x2 + ' ' + y2 + ' Z';
            }
            svg.appendChild(svgEl('path', { d: d, fill: color(i), stroke: 'var(--surface)', 'stroke-width': 1 }));
            ang = a2;
        });
        if (inner > 0) svg.appendChild(svgEl('circle', { cx: cx, cy: cy, r: inner, fill: 'var(--surface)' }));
        return svg;
    }

    function legendFor(chart, type) {
        var items;
        if (isPie(type)) {
            items = (chart.series[0] || { points: [] }).points
                .filter(function (p) { return (p.y || 0) > 0; })
                .map(function (p, i) { return { name: p.x, i: i }; });
        } else if (chart.series.length > 1) {
            items = chart.series.map(function (s, i) { return { name: s.name || ('Series ' + (i + 1)), i: i }; });
        } else {
            return null;
        }
        if (!items.length) return null;
        var wrap = el('div', { class: 'chart-legend' });
        items.slice(0, 12).forEach(function (it) {
            var span = el('span');
            span.appendChild(el('i', { style: 'background:' + color(it.i) }));
            span.appendChild(document.createTextNode(truncate(it.name || '(blank)', 22)));
            wrap.appendChild(span);
        });
        return wrap;
    }

    function renderChart(container, chart) {
        container.innerHTML = '';
        var type = normType(chart.type);
        if (!chart.hasData || !chart.series.length || !chart.categories.length) {
            container.appendChild(el('div', { class: 'chart-msg' }, 'No data for this report.'));
            return;
        }
        var svg;
        try {
            if (isPie(type)) svg = renderPie(chart, type);
            else if (isBar(type)) svg = renderBars(chart);
            else if (isLineOrArea(type)) svg = renderLineArea(chart, type);
            else svg = renderColumns(chart, type);
        } catch (e) {
            container.appendChild(el('div', { class: 'chart-msg' }, 'Could not render chart.'));
            return;
        }
        container.appendChild(svg);
        var lg = legendFor(chart, type);
        if (lg) container.appendChild(lg);
    }

    // ---- data loading -----------------------------------------------------
    function setStatus(msg, isError) {
        var s = document.getElementById('statusMsg');
        if (!s) return;
        s.textContent = msg || '';
        s.className = 'state-msg' + (isError ? ' error' : '');
        s.style.display = msg ? 'block' : 'none';
    }

    function buildCard(report) {
        var card = el('div', { class: 'chart-card', 'data-report-id': report.id });
        var head = el('div', { class: 'chart-card-head' });
        var title = el('div', { class: 'chart-card-title' }, 'Report ' + report.id);
        var pill = el('span', { class: 'chart-type-pill' }, '…');
        var openBtn = el('button', { class: 'chart-open-btn', title: 'Open in browser',
            'aria-label': 'Open in browser' });
        openBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" ' +
            'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
            '<path d="M10 3h4v4"/><path d="M14 3l-7 7"/>' +
            '<path d="M12 10v3a1.2 1.2 0 0 1-1.2 1.2H3.2A1.2 1.2 0 0 1 2 13V5.2A1.2 1.2 0 0 1 3.2 4H6"/></svg>';
        head.appendChild(title); head.appendChild(pill); head.appendChild(openBtn);
        var body = el('div', { class: 'chart-body' });
        body.appendChild(el('div', { class: 'chart-msg' }, 'Loading…'));
        card.appendChild(head); card.appendChild(body);
        card._title = title; card._pill = pill; card._body = body; card._openBtn = openBtn;
        return card;
    }

    function loadChart(card, report) {
        plmSend('getChart', { id: report.id }).then(function (r) {
            var d = plmParse(r);
            if (!d.success || !d.chart) {
                card._body.innerHTML = '';
                card._body.appendChild(el('div', { class: 'chart-msg' }, d.error || 'Failed to load chart.'));
                return;
            }
            var c = d.chart;
            card._title.textContent = c.title || c.name || ('Report ' + report.id);
            card._title.title = card._title.textContent;
            card._pill.textContent = (normType(c.type) || '').toLowerCase();
            var chartUrl = report.chartUrl || c.chartUrl || report.link || '';
            card._openBtn.title = 'Open chart (' + chartUrl + ')';
            card._openBtn.onclick = function () {
                if (!chartUrl) return;
                // Prefer a small popup window; fall back to the system browser if the
                // Fusion webview blocks window.open.
                var popup = null;
                try {
                    popup = window.open(chartUrl, 'plmChart_' + report.id,
                        'popup=yes,width=1000,height=780,scrollbars=yes,resizable=yes');
                } catch (e) { popup = null; }
                if (!popup) { plmOpenUrl(chartUrl); }
            };
            renderChart(card._body, c);
        }).catch(function (e) {
            card._body.innerHTML = '';
            card._body.appendChild(el('div', { class: 'chart-msg' }, 'Error: ' + e));
        });
    }

    function loadDashboard() {
        setStatus('Loading dashboard…', false);
        var grid = document.getElementById('chartGrid');
        grid.innerHTML = '';
        plmSend('getDashboards', {}).then(function (r) {
            var d = plmParse(r);
            if (!d.success) {
                setStatus(d.error || 'Could not load the dashboard.', true);
                if (d.unauthorized && _authPoller) _authPoller.start();
                return;
            }
            var reports = d.reports || [];
            if (!reports.length) {
                setStatus('No dashboard reports are configured for this tenant.', false);
                return;
            }
            setStatus('', false);
            reports.forEach(function (rep) {
                var card = buildCard(rep);
                grid.appendChild(card);
                loadChart(card, rep);
            });
        }).catch(function (e) {
            setStatus('Error: ' + e, true);
        });
    }

    // ---- Python → JS push receiver ---------------------------------------
    window.fusionJavaScriptHandler = {
        handle: function (action, data) {
            try {
                if (typeof plmHandleAsyncResult === 'function' && plmHandleAsyncResult(action, data)) {
                    return 'OK';
                }
                if (action === 'tokenResult') {
                    if (_authPoller) _authPoller.stop();
                    loadDashboard();
                }
            } catch (e) { /* ignore */ }
            return 'OK';
        }
    };

    function init() {
        if (typeof plmRequestTheme === 'function') plmRequestTheme();
        _authPoller = (typeof plmCreateAuthPoller === 'function')
            ? plmCreateAuthPoller(loadDashboard) : null;
        var btn = document.getElementById('btnRefresh');
        if (btn) btn.onclick = loadDashboard;
        loadDashboard();
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
