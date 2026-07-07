# Search-results + lineage lookup. Ported from lib/fusion_manage_api.py.

import re


def search_results(client, query_terms, revision=2, limit=50, offset=0, pre_formatted=False):
    """GET /api/v3/search-results?query=...&revision=2. Returns (dict, error).

    query_terms: list of terms.
      pre_formatted=True  -> terms are already valid Lucene expressions.
      pre_formatted=False -> raw URNs; each is wrapped in double quotes so Lucene
                             treats it as a literal phrase (URNs contain ':' which
                             Lucene would otherwise read as field:value operators,
                             yielding a 400).

    Quirk: FM returns an EMPTY body (not {"items":[]}) for zero results;
    empty_body_as={} maps that to a normal empty result.
    """
    import urllib.parse
    if not query_terms or not isinstance(query_terms, list):
        return {'items': [], 'totalCount': 0}, 'Missing or invalid query terms'
    if pre_formatted:
        query_string = ' '.join(str(u).strip() for u in query_terms if u)
    else:
        query_string = ' '.join(f'"{str(u).strip()}"' for u in query_terms if u)
    if not query_string:
        return {'items': [], 'totalCount': 0}, None
    params = urllib.parse.urlencode({'query': query_string, 'revision': revision,
                                     'limit': limit, 'offset': offset, 'page': 1})
    url = f'{client.base}/api/v3/search-results?{params}'
    r = client._request('GET', url, empty_body_as={})
    if not r.ok:
        return {'items': [], 'totalCount': 0}, ('unauthorized' if r.unauthorized else r.error)
    if r.data is None and r.text and r.text.strip():
        # 2xx body that wasn't JSON — surface a clear error like the old code.
        snippet = r.text[:300].replace('\n', ' ')
        return {'items': []}, f'Search response not JSON: {snippet!r}'
    data = r.data if isinstance(r.data, dict) else {}
    items = data.get('items') if isinstance(data.get('items'), list) else []
    return {'items': items, 'totalCount': data.get('totalCount', len(items))}, None


def find_item_by_lineage_urn(client, drawings_ws_id, lineage_urn):
    """Search a CAD workspace for an item whose lineage URN tail matches.

    Strategy (preserved from the old code): query the BARE tail (base64url, no
    colons), pre_formatted=True so no quoting is added. The tail is globally unique
    and FM indexes it in the full-text index. Quoted ("tail") and field-prefix
    (LINEAGE_URN:tail) forms both returned empty bodies in testing; bare tail works.

    Search-result items expose the canonical path in '__self__'
    (e.g. '/api/v3/workspaces/76/items/17317') — IDs come from there, not from a
    'workspace.link' sub-object.

    Returns (dict, error). On a clean miss: dict {'not_found': True} with error None.
    """
    lineage_urn = str(lineage_urn or '').strip()
    tail = lineage_urn.rsplit(':', 1)[-1] if ':' in lineage_urn else lineage_urn
    if not tail:
        return None, 'Invalid lineage URN.'

    result, err = search_results(client, [tail], revision=2, limit=50, pre_formatted=True)
    if err:
        return None, err

    ws_id_str = str(drawings_ws_id)
    for item in result.get('items', []):
        if item.get('deleted'):
            continue
        self_link = item.get('__self__', '')
        ws_m = re.search(r'workspaces/(\d+)', self_link)
        item_m = re.search(r'items/(\d+)', self_link)
        if not ws_m or not item_m or ws_m.group(1) != ws_id_str:
            continue
        return {'item_id': item_m.group(1), 'workspace_id': ws_m.group(1),
                'descriptor': item.get('descriptor', '')}, None

    return {'not_found': True}, None
