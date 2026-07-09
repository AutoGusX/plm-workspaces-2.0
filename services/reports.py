# Reporting / dashboard endpoints (Fusion Manage legacy REST v1).
#
# The charts feature reads the tenant's report dashboard and each chart report.
# These live on the /api/rest/v1 API (same family as workflow transitions), so every
# call goes through FmClient._request(..., send_tenant=True) to attach the X-Tenant
# header. Each function returns (value, error) — error is 'unauthorized' or a message.
#
# Verified live (autodesk8937 / adskgusquade2025, 2026-07-08):
#   GET /api/rest/v1/reports/dashboard
#     -> {"dashboardReportList": {"list": [{"id", "position", "link"}, ...]}}
#   GET /api/rest/v1/reports/{id}/chart.json
#     -> {"reportDefinition": {"name", "description", "reportChart": {...}},
#         "reportResult": {"columnKey": [{"value","label"}],
#                          "row": [{"rowId","fields":{"entry":[{"key","fieldData":{...}}]}}]},
#         "xAxisColumn": "<colId>", "yAxisColumn": "<colId>"}


def dashboard(client):
    """GET /api/rest/v1/reports/dashboard — the tenant's dashboard report list.

    Returns (list_of_reports, error) where each report is {'id', 'position', 'link'},
    sorted by 'position'. The raw envelope is dashboardReportList.list.
    """
    url = f'{client.base}/api/rest/v1/reports/dashboard'
    r = client._request('GET', url, content_type='application/json', send_tenant=True)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    data = r.data or {}
    raw = ((data.get('dashboardReportList') or {}).get('list')) or []
    reports = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rid = entry.get('id')
        if rid is None:
            continue
        reports.append({
            'id': rid,
            'position': entry.get('position', 0),
            'link': entry.get('link') or '',
            # User-facing chart deep link (e.g. https://<tenant>.autodeskplm360.net/plm/chart/312),
            # not the raw /api/rest/v1 link.
            'chartUrl': f'{client.base}/plm/chart/{rid}',
        })
    reports.sort(key=lambda x: x.get('position') or 0)
    return reports, None


def _field_map(row):
    """Flatten a report row's fields.entry list into {columnKey: fieldData}."""
    out = {}
    for e in ((row.get('fields') or {}).get('entry') or []):
        if isinstance(e, dict) and e.get('key') is not None:
            out[e['key']] = e.get('fieldData') or {}
    return out


def _num(field_data):
    """Best-effort numeric from a fieldData object ({value|formattedValue})."""
    if not isinstance(field_data, dict):
        return None
    for k in ('value', 'formattedValue'):
        v = field_data.get(k)
        if v in (None, ''):
            continue
        try:
            return float(str(v).replace(',', '').strip())
        except (ValueError, TypeError):
            continue
    return None


def _label(field_data):
    """Best-effort display label from a fieldData object."""
    if not isinstance(field_data, dict):
        return ''
    return str(field_data.get('label') or field_data.get('formattedValue')
               or field_data.get('value') or '').strip()


def chart(client, report_id):
    """GET /api/rest/v1/reports/{id}/chart.json — one chart report's definition + data.

    Returns (normalized_chart, error). The normalized chart is a small, render-ready
    shape the frontend can draw without re-deriving anything:
        {
          'id', 'name', 'description',
          'type',                # COLUMN | BAR | STACKEDCOLUMN | LINE | AREA | MSAREA | PIE | DOUGHNUT | ...
          'title', 'xLabel', 'yLabel',
          'series': [ {'name': <seriesLabel>, 'points': [ {'x': <label>, 'y': <number>}, ... ] }, ... ],
          'categories': [ <x label>, ... ]   # union of x labels in row order
        }
    Single-series charts return one series; stacked/multi-series charts split on the
    third (series) column when present.
    """
    url = f'{client.base}/api/rest/v1/reports/{report_id}/chart.json'
    r = client._request('GET', url, content_type='application/json', send_tenant=True)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    data = r.data or {}
    rdef = data.get('reportDefinition') or {}
    rchart = rdef.get('reportChart') or {}
    result = data.get('reportResult') or {}
    x_col = data.get('xAxisColumn') or ''
    y_col = data.get('yAxisColumn') or ''

    # Identify the series column: the column that is neither x nor y (stacked/multi charts).
    col_labels = {}
    series_col = ''
    for c in (result.get('columnKey') or []):
        if isinstance(c, dict) and c.get('value') is not None:
            col_labels[c['value']] = c.get('label') or c['value']
            if c['value'] not in (x_col, y_col) and not series_col:
                series_col = c['value']

    categories = []
    series_map = {}   # series name -> {x label -> y}
    series_order = []
    for row in (result.get('row') or []):
        if not isinstance(row, dict):
            continue
        fmap = _field_map(row)
        x_label = _label(fmap.get(x_col)) if x_col else ''
        y_val = _num(fmap.get(y_col)) if y_col else None
        if y_val is None:
            y_val = 0.0
        if x_label and x_label not in categories:
            categories.append(x_label)
        s_name = _label(fmap.get(series_col)) if series_col else ''
        if s_name not in series_map:
            series_map[s_name] = {}
            series_order.append(s_name)
        # Sum duplicates (same x within a series).
        series_map[s_name][x_label] = series_map[s_name].get(x_label, 0.0) + y_val

    series = []
    for name in series_order:
        pts = [{'x': x, 'y': series_map[name].get(x, 0.0)} for x in categories]
        series.append({'name': name, 'points': pts})

    chart_type = (rchart.get('type') or 'COLUMN').upper()
    return {
        'id': rdef.get('id', report_id),
        'name': rdef.get('name') or '',
        'description': rdef.get('description') or '',
        'type': chart_type,
        'title': rchart.get('title') or rdef.get('name') or '',
        'xLabel': rchart.get('chartXaxisLabel') or col_labels.get(x_col, ''),
        'yLabel': rchart.get('chartYaxisLabel') or col_labels.get(y_col, ''),
        'seriesLabel': rchart.get('chartSeriesLabel') or col_labels.get(series_col, ''),
        'categories': categories,
        'series': series,
        'hasData': any(p['y'] for s in series for p in s['points']),
    }, None
