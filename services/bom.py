# BOM (bill-of-materials) row endpoints — Fusion Manage v3.
#
# The electronics-BOM export writes child rows onto a parent item. This is a distinct
# REST surface from affected-items. Contract verified against the BOM Builder extension
# (all links are workspace-scoped relative paths: /api/v3/workspaces/{ws}/items/{id}):
#
#   READ   GET  {itemLink}/bom?depth&revisionBias=release&rootId&viewDefId
#          Accept: application/vnd.autodesk.plm.bom.bulk+json   -> {nodes, edges, totalCount}
#   ADD    POST {parentLink}/bom-items
#          {quantity, isPinned:false, item:{link:childLink}, itemNumber?, fields?:[{metaData:{link},value}]}
#          -> 201, new edge URL in Location
#   UPDATE PATCH {parentLink}/bom-items/{edgeId}   (same body)
#   REMOVE DELETE {edgeLink}
#   VERSIONS GET {itemLink}/versions -> entries with a 'status' (WORKING/RELEASED/...)
#
# Adds must be posted SERIALLY in ascending itemNumber (the server slots by hint).
# Each function returns (value, error) — error is 'unauthorized' or a message.

import re

_BOM_ACCEPT = 'application/vnd.autodesk.plm.bom.bulk+json'
_EDGE_ID_RE = re.compile(r'/bom-items/(\d+)\b')
_ITEM_ID_RE = re.compile(r'/items/(\d+)\b')
# Child refs come back as URNs: urn:adsk.plm:tenant.workspace.item:TENANT.<ws>.<item>
_URN_ITEM_RE = re.compile(r'item:[A-Za-z0-9]+\.(\d+)\.(\d+)$')


def _abs(client, link):
    """Absolute URL for a workspace-scoped relative link (leave full URLs untouched)."""
    link = str(link or '')
    if link.startswith('http'):
        return link
    return f'{client.base}{link}'


def item_link(workspace_id, item_id):
    """Canonical workspace-scoped item link."""
    return f'/api/v3/workspaces/{workspace_id}/items/{item_id}'


def _ref_link(ref):
    """Extract a workspace-scoped item link from an edge child/parent ref, which may be
    a dict ({link|__self__|urn}), a link string, a urn, or a bare id."""
    if isinstance(ref, dict):
        return ref.get('link') or ref.get('__self__') or ref.get('urn') or ''
    return str(ref or '')


def _node_index(nodes):
    """Map a node's urn AND item link -> the workspace-scoped item link, so edge refs
    (which may be urns) resolve to a usable link."""
    by_key = {}
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        item = n.get('item') if isinstance(n.get('item'), dict) else n
        link = item.get('link') or item.get('__self__') or ''
        urn = item.get('urn') or n.get('urn') or ''
        if link:
            if urn:
                by_key[urn] = link
            by_key[link] = link
    return by_key


def _edge_quantity(edge):
    """Quantity from edge.quantity, else the first numeric-valued row field.

    The BOM-row quantity field id is tenant/viewdef-specific (NOT the extension's 103),
    but among the row fields only quantity parses as a float — UoM is text ('EA') and
    isPinned is 'true'/'false'. So we pick the first float-parseable field value.
    """
    q = edge.get('quantity')
    if q not in (None, ''):
        try:
            return float(str(q).replace(',', '').strip())
        except (ValueError, TypeError):
            pass
    for f in edge.get('fields') or []:
        if not isinstance(f, dict):
            continue
        v = f.get('value')
        if v in (None, '') or str(v).strip().lower() in ('true', 'false'):
            continue
        try:
            return float(str(v).replace(',', '').strip())
        except (ValueError, TypeError):
            continue
    return 1.0


def _child_link(raw_child, node_idx):
    """Resolve an edge child ref (URN / link / bare) to a workspace-scoped item link."""
    if not raw_child:
        return ''
    if raw_child.startswith('/api'):
        return raw_child
    if raw_child in node_idx:
        return node_idx[raw_child]
    m = _URN_ITEM_RE.search(raw_child)
    if m:
        return item_link(m.group(1), m.group(2))
    return ''


def read_bom(client, workspace_id, item_id, view_id=1, depth=100, revision_bias='release',
             page_limit=500, max_pages=50):
    """Read a parent item's BOM edges. Returns ({'edges':[...], 'totalCount':N}, error).

    Each edge: {edgeId, edgeLink, childLink, childId, quantity, itemNumber}.
    Paginates offset+=page_limit until a short page / totalCount reached / max_pages.
    """
    base_item = item_link(workspace_id, item_id)
    offset, all_edges, all_nodes, total = 0, [], [], None
    for _ in range(max_pages):
        import urllib.parse
        params = urllib.parse.urlencode({
            'depth': depth, 'revisionBias': revision_bias, 'rootId': item_id,
            'limit': page_limit, 'offset': offset, 'viewDefId': view_id})
        url = f'{client.base}{base_item}/bom?{params}'
        r = client._request('GET', url, accept=_BOM_ACCEPT, empty_body_as={})
        if not r.ok:
            return None, ('unauthorized' if r.unauthorized else r.error)
        data = r.data if isinstance(r.data, dict) else {}
        edges = data.get('edges') or []
        nodes = data.get('nodes') or []
        all_edges.extend(edges)
        all_nodes.extend(nodes)
        total = data.get('totalCount', total)
        if len(edges) < page_limit:
            break
        if total is not None and len(all_edges) >= total:
            break
        offset += page_limit

    node_idx = _node_index(all_nodes)
    out = []
    seen = set()
    for e in all_edges:
        if not isinstance(e, dict):
            continue
        edge_link = e.get('edgeLink') or e.get('__self__') or e.get('link') or ''
        edge_id = str(e.get('edgeId') or e.get('id') or e.get('bomItemId') or '')
        if not edge_id:
            m = _EDGE_ID_RE.search(edge_link)
            edge_id = m.group(1) if m else ''
        child_link = _child_link(_ref_link(e.get('child')), node_idx)
        cm = _ITEM_ID_RE.search(child_link)
        child_id = cm.group(1) if cm else ''
        key = edge_id or child_link
        if key in seen:
            continue
        seen.add(key)
        out.append({
            'edgeId': edge_id,
            'edgeLink': edge_link,
            'childLink': child_link,
            'childId': child_id,
            'quantity': _edge_quantity(e),
            'itemNumber': e.get('itemNumber'),
        })
    return {'edges': out, 'totalCount': total if total is not None else len(out)}, None


def _row_body(child_link, quantity, item_number=None, fields=None, is_pinned=False):
    body = {
        'quantity': float(quantity) if quantity not in (None, '') else 1.0,
        'isPinned': bool(is_pinned),
        'item': {'link': child_link},
    }
    if item_number is not None:
        try:
            body['itemNumber'] = int(item_number)
        except (ValueError, TypeError):
            pass
    if fields:
        body['fields'] = [{'metaData': {'link': f['link']}, 'value': f.get('value')}
                          for f in fields if f.get('link')]
    return body


def add_bom_row(client, parent_link, child_link, quantity, item_number=None, fields=None):
    """POST {parentLink}/bom-items. Returns ({'edgeLink': <Location>}, error)."""
    url = f'{_abs(client, parent_link)}/bom-items'
    body = _row_body(child_link, quantity, item_number, fields)
    r = client._request('POST', url, body=body, content_type='application/json',
                        send_tenant=True)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    return {'edgeLink': getattr(r, 'location', '') or ''}, None


def update_bom_row(client, parent_link, edge_id, child_link, quantity, fields=None,
                   item_number=None):
    """PATCH {parentLink}/bom-items/{edgeId} (same body as add). Returns error_or_None."""
    url = f'{_abs(client, parent_link)}/bom-items/{edge_id}'
    body = _row_body(child_link, quantity, item_number, fields)
    r = client._request('PATCH', url, body=body, content_type='application/json',
                        send_tenant=True)
    if not r.ok:
        return ('unauthorized' if r.unauthorized else r.error)
    return None


def remove_bom_row(client, edge_link):
    """DELETE {edgeLink}. Returns error_or_None."""
    r = client._request('DELETE', _abs(client, edge_link), send_tenant=True)
    if not r.ok:
        return ('unauthorized' if r.unauthorized else r.error)
    return None


def item_versions(client, workspace_id, item_id):
    """GET {itemLink}/versions. Returns (list, error) of {status, link, version} entries."""
    url = f'{client.base}{item_link(workspace_id, item_id)}/versions'
    r = client._request('GET', url, empty_body_as={})
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    data = r.data
    raw = data if isinstance(data, list) else ((data or {}).get('items')
          or (data or {}).get('versions') or [])
    out = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        out.append({
            'status': (v.get('status') or v.get('lifecycleStatus') or '').upper(),
            'link': v.get('__self__') or v.get('link') or '',
            'version': v.get('version') or v.get('number'),
        })
    return out, None


def working_item_id(client, workspace_id, item_id):
    """Return the item id of the WORKING version (editable), or the given id if none is
    marked or the list is unavailable. Used as a pre-flight before BOM writes."""
    versions, err = item_versions(client, workspace_id, item_id)
    if err or not versions:
        return item_id
    for v in versions:
        if v['status'] == 'WORKING':
            m = _ITEM_ID_RE.search(v['link'])
            if m:
                return m.group(1)
    return item_id
