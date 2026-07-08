# Item-related FM v3 endpoints: outstanding work, item detail, fields, sections,
# tableaus, lookups, images, create/update, tabs, affected-items, permissions,
# workspaces (entitlements), and the item-details deep-link builder.
#
# Ported from the old lib/fusion_manage_api.py. Each function takes the FmClient so
# all HTTP goes through its single 401-refresh-retry. Functions return plain values
# + an error string (None on success) — the old success/error dicts are unwrapped
# at this boundary so callers stay terse.

import re
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# URL + parsing helpers (no network)
# ---------------------------------------------------------------------------
def _extract_workspace_id(task):
    link = (task.get('workspace') or {}).get('link') or ''
    m = re.search(r'workspaces/(\d+)', link)
    return m.group(1) if m else None


def _extract_item_id(task):
    link = (task.get('item') or {}).get('link') or ''
    m = re.search(r'items/(\d+)', link)
    return m.group(1) if m else None


def _person_name(v):
    """Owner/creator may be a plain string (the /items endpoint) or an object
    ({title|name}) on other endpoints. Return a display name either way."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return (v.get('title') or v.get('name') or '').strip()
    return ''


def build_item_details_url(tenant_name, workspace_id, item_id):
    """Build the PLM web deep link for an item's detail page."""
    if not workspace_id or not item_id:
        return None
    tenant_upper = (tenant_name or '').upper()
    item_urn = f'urn%60adsk,plm%60tenant,workspace,item%60{tenant_upper},{workspace_id},{item_id}'
    return (f'https://{tenant_name}.autodeskplm360.net/plm/workspaces/{workspace_id}'
            f'/items/itemDetails?view=full&tab=details&mode=view&itemId={item_urn}')


def build_fusion_manage_url(tenant_name, task):
    workspace_id = _extract_workspace_id(task)
    item_id = _extract_item_id(task)
    if not workspace_id or not item_id:
        return None
    return build_item_details_url(tenant_name, workspace_id, item_id)


def _parse_date(s):
    if s is None:
        return None, None
    try:
        if isinstance(s, (int, float)):
            dt = datetime.fromtimestamp(s / 1000.0, tz=timezone.utc)
            return dt, dt.isoformat()
        if not isinstance(s, str) or not s.strip():
            return None, None
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt, s
    except Exception:
        return None, s if isinstance(s, str) else None


def _process_task(tenant_name, raw_task):
    """Enrich a raw outstanding-work task with derived display fields (ported)."""
    workspace = raw_task.get('workspace') or {}
    item = raw_task.get('item') or {}
    workspace_id = _extract_workspace_id(raw_task)
    item_id = _extract_item_id(raw_task)
    workflow_state = raw_task.get('workflowState') or {}
    workflow_state_name = raw_task.get('workflowStateName') or workflow_state.get('name') or ''
    milestone_dt, milestone_iso = _parse_date(raw_task.get('milestoneDate'))
    days_to_due = None
    if milestone_dt:
        days_to_due = int(((milestone_dt - datetime.now(timezone.utc)).total_seconds() / 86400) + 0.5)
    state_set_dt, state_set_iso = _parse_date(raw_task.get('workflowStateSetDate'))
    days_from_state_change = None
    if state_set_dt:
        days_from_state_change = int((datetime.now(timezone.utc) - state_set_dt).total_seconds() / 86400)
    item_title = item.get('title') or 'Untitled Task'
    item_number = item.get('number') or item.get('identifier')
    if item_number and item_title and str(item_number).strip() not in item_title:
        item_descriptor = f'{item_number} - {item_title}'
    else:
        item_descriptor = item_title
    state_set_by = (raw_task.get('workflowStateSetBy') or raw_task.get('stateSetBy')
                    or (workflow_state.get('setBy') if isinstance(workflow_state.get('setBy'), str) else None))
    flagged = bool(raw_task.get('flagged') or raw_task.get('flag'))
    return {
        **raw_task,
        'workspaceTitle': workspace.get('title') or 'Unknown Workspace',
        'itemTitle': item_title,
        'itemDescriptor': item_descriptor,
        'workspaceId': workspace_id,
        'itemId': item_id,
        'tenantName': tenant_name,
        'fusionManageUrl': build_fusion_manage_url(tenant_name, raw_task),
        'workflowStateName': workflow_state_name,
        'dueDate': milestone_iso or raw_task.get('milestoneDate'),
        'daysToDue': days_to_due,
        'workflowStateSetDate': state_set_iso or raw_task.get('workflowStateSetDate'),
        'workflowStateSetBy': state_set_by,
        'stateSetBy': state_set_by,
        'daysFromStateChange': days_from_state_change,
        'flagged': flagged,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def workspaces(client):
    """GET /api/v3/workspaces?unlimited=true. User identity is on the 3-legged token,
    so x-User-id is not required here. Returns the entitlement dict shape, including a
    new system_name_to_title map (systemName -> workspace title string)."""
    url = f'{client.base}/api/v3/workspaces?unlimited=true'
    r = client._request('GET', url)
    if not r.ok:
        out = {'success': False, 'error': r.error}
        if r.unauthorized:
            out['unauthorized'] = True
        return out
    data = r.data or {}
    workspace_ids, ordered, sn_map, sn_title_map = set(), [], {}, {}
    for item in (data.get('items') or []):
        if not isinstance(item, dict):
            continue
        link = item.get('link') or ''
        system_name = item.get('systemName') or ''
        title = item.get('title') or item.get('name') or ''
        m = re.search(r'workspaces/(\d+)', link)
        if m:
            wid = m.group(1)
            workspace_ids.add(wid)
            ordered.append(wid)
            if system_name:
                sn_map[system_name] = wid
                if title:
                    sn_title_map[system_name] = title
    return {'success': True, 'workspace_ids': workspace_ids,
            'workspace_ids_ordered': ordered, 'system_name_to_id': sn_map,
            'system_name_to_title': sn_title_map}


def items_list(client, workspace_id, offset=0, limit=50):
    """GET /api/v3/workspaces/{id}/items?offset&limit — paginated item list.

    Works for every change workspace including those with no tableaus (e.g. Problem Reports).
    Each item in the response has __self__, descriptor, owner, creator, workspaceShortName,
    workspaceLongName, category, and urn.

    Returns {'items': [...parsed...], 'totalCount': N, 'offset': N, 'limit': N}
    or (None, error_string) on failure.
    """
    import urllib.parse
    params = urllib.parse.urlencode({'offset': int(offset), 'limit': int(limit)})
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/items?{params}'
    r = client._request('GET', url)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    data = r.data or {}
    raw_items = data.get('items') or []
    parsed = []
    _WS_ITEM_RE = re.compile(r'/workspaces/(\d+)/items/(\d+)')
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        self_ref = entry.get('__self__') or ''
        m = _WS_ITEM_RE.search(self_ref)
        ws_id_str = m.group(1) if m else str(workspace_id)
        item_id_str = m.group(2) if m else ''
        if not item_id_str:
            continue
        descriptor = entry.get('descriptor') or ''
        # Split descriptor on first ' - ' into number + title
        if ' - ' in descriptor:
            sep_idx = descriptor.index(' - ')
            number = descriptor[:sep_idx].strip()
            title = descriptor[sep_idx + 3:].strip()
        else:
            number = ''
            title = descriptor
        # owner/creator come back as a plain string ("Orrin Bourne") on this endpoint,
        # but tolerate the object form ({title|name}) other endpoints use.
        owner = _person_name(entry.get('owner'))
        creator = _person_name(entry.get('creator'))
        parsed.append({
            'itemId': item_id_str,
            'workspaceId': ws_id_str,
            'descriptor': descriptor,
            'number': number,
            'title': title,
            'owner': owner,
            'creator': creator,
            'workspaceShortName': entry.get('workspaceShortName') or '',
            'workspaceLongName': entry.get('workspaceLongName') or '',
            'category': entry.get('category') or '',
            'urn': entry.get('urn') or '',
        })
    return {
        'items': parsed,
        'totalCount': data.get('totalCount', len(parsed)),
        'offset': data.get('offset', offset),
        'limit': data.get('limit', limit),
    }, None


def outstanding_work(client):
    """GET /api/v3/users/@me/outstanding-work. Returns {'success', 'tasks', 'count'}."""
    url = f'{client.base}/api/v3/users/@me/outstanding-work'
    r = client._request('GET', url)
    if not r.ok:
        out = {'success': False, 'error': r.error}
        if r.unauthorized:
            out['unauthorized'] = True
        return out
    data = r.data or {}
    raw_list = data.get('outstandingWork') or []
    tasks = [_process_task(client.ctx.tenant, t) for t in raw_list]
    return {'success': True, 'tasks': tasks, 'count': data.get('count', len(tasks))}


def item_detail(client, workspace_id, item_id):
    """GET item. Returns (item_dict, etag, error). error == 'unauthorized' on 401."""
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/items/{item_id}'
    r = client._request('GET', url)
    if not r.ok:
        return None, '', ('unauthorized' if r.unauthorized else r.error)
    return r.data, r.etag, None


def view_fields(client, workspace_id, view_id=1):
    """GET /views/{view_id}/fields — full field definitions with editability/picklist."""
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/views/{view_id}/fields'
    r = client._request('GET', url)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    data = r.data
    raw = data if isinstance(data, list) else ((data or {}).get('fields') or (data or {}).get('items') or [])
    fields = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        self_ref = f.get('__self__') or ''
        m = re.search(r'fields/([^/?]+)', self_ref)
        fields.append({
            'id': m.group(1) if m else '',
            '__self__': self_ref,
            'title': f.get('title') or (m.group(1) if m else ''),
            'type': f.get('type') or {},
            'value': f.get('value'),
            'isSystemField': f.get('isSystemField', False),
            'editability': f.get('editability') or 'ALWAYS',
            'visibility': f.get('visibility', True),
            'picklist': f.get('picklist') or None,
            'fieldValidators': f.get('fieldValidators') or [],
        })
    return fields, None


def workspace_fields(client, workspace_id):
    """GET /workspaces/{id}/fields — authoritative field definitions (uses 'name' for title)."""
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/fields'
    r = client._request('GET', url)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    raw = (r.data or {}).get('fields') or []
    if not isinstance(raw, list):
        raw = []
    fields = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        self_ref = f.get('__self__') or ''
        m = re.search(r'fields/([^/?]+)', self_ref)
        fid = m.group(1) if m else ''
        if not fid:
            continue
        # derivedFieldSource is an object {__self__: '.../fields/{srcId}'}; extract the id.
        derived_src = ''
        dfs = f.get('derivedFieldSource')
        if isinstance(dfs, dict):
            dm = re.search(r'fields/([^/?]+)', dfs.get('__self__') or '')
            derived_src = dm.group(1) if dm else ''
        fields.append({
            'id': fid,
            'name': f.get('name') or fid,
            '__self__': self_ref,
            'title': f.get('name') or f.get('title') or fid,
            'type': f.get('type') or {},
            'editability': f.get('editability') or 'ALWAYS',
            'visibility': f.get('visibility') or 'ALWAYS',
            'visibleOnPreview': bool(f.get('visibleOnPreview', False)),
            'picklist': f.get('picklist') or None,
            'fieldValidators': f.get('fieldValidators') or [],
            'displayOrder': f.get('displayOrder') or 0,
            # Write-path metadata (mirrors the extension's CLONE_FIELD_RULES): these
            # decide which fields are safe to send in a create/update body. isSystemField
            # is NOT reliably on this endpoint — it comes from the item detail on edit.
            'derived': bool(f.get('derived')),
            'derivedFieldSource': derived_src,
            'formulaField': bool(f.get('formulaField')),
            'isSystemField': bool(f.get('isSystemField', False)),
        })
    return fields, None


def sections(client, workspace_id):
    """GET /sections — section structure with field links. Returns (dict, error)
    where dict has 'sections' and the detected 'viewId'."""
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/sections'
    r = client._request('GET', url)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    data = r.data
    raw = data if isinstance(data, list) else ((data or {}).get('sections') or (data or {}).get('items') or [])
    out_sections = []
    detected_view_id = None

    def _extract_field_ref(f_dict):
        flink = f_dict.get('link') or f_dict.get('__self__') or ''
        fm2 = re.search(r'fields/([^/?]+)', flink)
        fid = fm2.group(1) if fm2 else str(f_dict.get('id') or '').strip()
        return fid, flink

    for sec in raw:
        if not isinstance(sec, dict):
            continue
        self_ref = sec.get('__self__') or sec.get('link') or ''
        m = re.search(r'sections/(\d+)', self_ref)
        sec_id = m.group(1) if m else ''
        if not sec_id and sec.get('id') is not None:
            sec_id = str(sec.get('id')).strip()
        if not self_ref and sec_id:
            self_ref = f'/api/v3/workspaces/{workspace_id}/sections/{sec_id}'
        fields = []
        for f in sec.get('fields') or []:
            if not isinstance(f, dict) or f.get('type') == 'MATRIX':
                continue
            fid, flink = _extract_field_ref(f)
            if detected_view_id is None and flink:
                vm = re.search(r'views/(\d+)/fields', flink)
                if vm:
                    detected_view_id = int(vm.group(1))
            fields.append({'id': fid, 'link': flink, 'title': f.get('title') or fid,
                           'type': f.get('type') or ''})
        for mx in sec.get('matrices') or []:
            if not isinstance(mx, dict):
                continue
            for row in mx.get('fields') or []:
                if not isinstance(row, list):
                    continue
                for mf in row:
                    if not isinstance(mf, dict):
                        continue
                    fid, flink = _extract_field_ref(mf)
                    if detected_view_id is None and flink:
                        vm = re.search(r'views/(\d+)/fields', flink)
                        if vm:
                            detected_view_id = int(vm.group(1))
                    if fid and not any(x['id'] == fid for x in fields):
                        fields.append({'id': fid, 'link': flink,
                                       'title': mf.get('title') or fid, 'type': mf.get('type') or ''})
        out_sections.append({
            # The /sections endpoint names the section in 'title' (not 'name').
            'id': sec_id, 'link': self_ref,
            'name': sec.get('title') or sec.get('name') or '',
            'displayOrder': sec.get('displayOrder') or 0, 'fields': fields,
            'matrices': sec.get('matrices') or [],
            # Carried into the create body for classified workspaces (FM v3 needs the
            # section's classificationId or the create is rejected / mis-classified).
            'classificationId': sec.get('classificationId'),
        })
    return {'sections': out_sections, 'viewId': detected_view_id or 1}, None


def tableaus(client, workspace_id):
    """GET /tableaus — list available views. Returns (list, error)."""
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/tableaus'
    r = client._request('GET', url)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    out = []
    for t in ((r.data or {}).get('tableaus') or []):
        if not isinstance(t, dict) or t.get('deleted'):
            continue
        m = re.search(r'tableaus/(\d+)', t.get('link') or '')
        tid = m.group(1) if m else None
        if tid:
            out.append({'id': tid, 'title': t.get('title') or t.get('name') or f'View {tid}',
                        'type': t.get('type') or '', 'urn': t.get('urn') or ''})
    return out, None


def tableau_data(client, workspace_id, tableau_id, page=1, size=50):
    """GET /tableaus/{id}?page&size — paginated rows. Returns (dict, error)."""
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/tableaus/{tableau_id}?page={page}&size={size}'
    r = client._request('GET', url)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    data = r.data or {}
    raw_items = data.get('items') or []
    rows = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        item_link = (entry.get('item') or {}).get('link') or ''
        m = re.search(r'workspaces/(\d+)/items/(\d+)', item_link)
        if not m:
            continue
        fields = {}
        for f in entry.get('fields') or []:
            if isinstance(f, dict) and f.get('id'):
                fields[f['id']] = {'value': f.get('value'),
                                   'typeTitle': (f.get('type') or {}).get('title') or ''}
        rows.append({'itemId': m.group(2), 'workspaceId': m.group(1), 'fields': fields})
    columns = []
    if raw_items:
        for f in (raw_items[0].get('fields') or []):
            if isinstance(f, dict) and f.get('id'):
                columns.append({'id': f['id'], 'typeTitle': (f.get('type') or {}).get('title') or ''})
    return {'columns': columns, 'rows': rows, 'pageNumber': data.get('pageNumber', page),
            'pageSize': data.get('pageSize', size), 'total': data.get('total', len(rows))}, None


def lookup_options(client, lookup_path, filter_text='', limit=100, offset=0):
    """GET {lookup_path}?asc=title&filter=... — picklist options. Returns (dict, error)."""
    import urllib.parse
    path = lookup_path if lookup_path.startswith('/') else '/' + lookup_path
    params = urllib.parse.urlencode({'asc': 'title', 'filter': filter_text or '',
                                     'limit': limit, 'offset': offset})
    url = f'{client.base}{path}?{params}'
    r = client._request('GET', url)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    items = []
    for it in ((r.data or {}).get('items') or []):
        if not isinstance(it, dict):
            continue
        items.append({'link': it.get('link') or '', 'title': it.get('title') or '',
                      'version': it.get('version') or None, 'deleted': bool(it.get('deleted', False)),
                      'urn': it.get('urn') or ''})
    total = (r.data or {}).get('totalCount', len(items))
    return {'items': items, 'totalCount': total, 'offset': offset, 'limit': limit}, None


def image_url(client, relative_link):
    """GET an image by relative link; returns (data_url_or_url, error).

    Images come back as binary; we base64 them into a data: URL so the webview can
    render without a second authenticated request.
    """
    if not relative_link or not isinstance(relative_link, str):
        return None, 'Missing or invalid link'
    import base64
    path = relative_link if relative_link.startswith('/') else '/' + relative_link
    url = f'{client.base}{path}'
    r = client._request('GET', url, accept='image/*,*/*', raw=True)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    if r.content_type.startswith('image/') and r.raw_bytes is not None:
        b64 = base64.b64encode(r.raw_bytes).decode('ascii')
        return f'data:{r.content_type};base64,{b64}', None
    return url, None


def create_item(client, workspace_id, sections_payload):
    """POST /items — create a new item. Returns (dict, error) with 'itemId' + 'data'.

    FM v3 returns 201 with an (often empty) body and the new item URL in the Location
    header, so the id is read from Location first, then the body __self__ as a fallback.
    """
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/items'
    r = client._request('POST', url, body={'sections': sections_payload})
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    data = r.data if isinstance(r.data, dict) else {}
    m = (re.search(r'items/(\d+)', getattr(r, 'location', '') or '')
         or re.search(r'items/(\d+)', data.get('__self__') or ''))
    return {'itemId': m.group(1) if m else None, 'data': data}, None


def update_item(client, workspace_id, item_id, sections_payload, if_match=''):
    """PATCH /items/{id} — update with optimistic concurrency.

    if_match is the ETag from the GET; FM rejects the PATCH if the item changed
    since (412 Precondition Failed), preventing lost-update overwrites.
    """
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/items/{item_id}'
    extra = {'if-match': if_match} if if_match else None
    r = client._request('PATCH', url, body={'sections': sections_payload}, extra_headers=extra)
    if not r.ok:
        return ('unauthorized' if r.unauthorized else r.error)
    return None


def item_tabs(client, workspace_id, item_id):
    """GET /items/{id}/tabs — tab names with counts. Returns (list, error)."""
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/items/{item_id}/tabs'
    r = client._request('GET', url)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    tabs = (r.data or {}).get('tabs') if isinstance(r.data, dict) else None
    return (tabs if isinstance(tabs, list) else []), None


def affected_items(client, workspace_id, item_id, view_id=11, offset=0, limit=100, sort='item.title'):
    """GET /items/{id}/views/{view_id} — affected-items (LINKEDITEMS) list.

    Quirk: some tenants return an EMPTY body (not {items: []}) when a record has
    zero linked items. empty_body_as={} maps that to a normal empty result rather
    than a parse failure.
    """
    import urllib.parse
    params = urllib.parse.urlencode({'offset': int(offset), 'limit': int(limit),
                                     'sort': sort or 'item.title'})
    url = (f'{client.base}/api/v3/workspaces/{workspace_id}'
           f'/items/{item_id}/views/{view_id}?{params}')
    r = client._request('GET', url, empty_body_as={})
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    data = r.data if isinstance(r.data, dict) else {}
    # FM returns "affectedItems" with Accept: application/json; some clients/versions
    # use "items" in a paginated envelope — handle both.
    raw = data.get('affectedItems') or data.get('items')
    items = raw if isinstance(raw, list) else []
    total = data.get('totalCount') if data.get('totalCount') is not None else len(items)
    return {
        'items': items,
        'totalCount': total,
        'offset': data.get('offset', offset),
        'limit': data.get('limit', limit),
    }, None


def add_affected(client, workspace_id, item_id, link_paths):
    """POST /items/{id}/affected-items — body is a JSON array of item link paths.
    Returns (results_list, error); each result carries the new affected-item location."""
    url = f'{client.base}/api/v3/workspaces/{workspace_id}/items/{item_id}/affected-items'
    if not isinstance(link_paths, list):
        link_paths = []
    body = [str(p) for p in link_paths if p]
    r = client._request('POST', url, body=body, empty_body_as=[])
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    results = r.data if isinstance(r.data, list) else []
    return results, None


def remove_affected(client, workspace_id, item_id, affected_item_id, view_id=11):
    """DELETE /items/{id}/views/{view_id}/affected-items/{aid}. 204 on success."""
    aid = str(affected_item_id).lstrip('/')
    url = (f'{client.base}/api/v3/workspaces/{workspace_id}'
           f'/items/{item_id}/views/{view_id}/affected-items/{aid}')
    r = client._request('DELETE', url)
    if not r.ok:
        return ('unauthorized' if r.unauthorized else r.error)
    return None


def item_permissions(client, workspace_id, item_id):
    """GET /items/{id}/users/@me/permissions — granted permission short names.
    Returns (list, error); empty/non-JSON bodies are treated as no-perms."""
    url = (f'{client.base}/api/v3/workspaces/{workspace_id}'
           f'/items/{item_id}/users/@me/permissions')
    r = client._request('GET', url, empty_body_as={})
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    raw = (r.data or {}).get('permissions') if isinstance(r.data, dict) else None
    names = []
    if isinstance(raw, list):
        for p in raw:
            if not isinstance(p, dict):
                continue
            name = p.get('name') or ''
            if not isinstance(name, str):
                continue
            short = name.split('.')[-1] if '.' in name else name
            if short:
                names.append(short)
    return names, None
