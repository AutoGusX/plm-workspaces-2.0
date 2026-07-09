"""Probe for the electronics-BOM export primitives (services/bom.py + match query) — NOT shipped.

Verifies the NEW REST layer against a live tenant before the resolve/push orchestration is
built on top. Runs the real services layer via the duck-typed Ctx (no Fusion needed).

Env:
    FM_TOKEN        — bearer token (needs data:write/create for the gated write)
    FM_TENANT       — tenant subdomain (e.g. autodesk8937)
    FM_PARENT_WS    — workspace id (numeric) of an item that HAS a BOM (any assembly)
    FM_PARENT_ITEM  — item id of that parent (read_bom target; also the scratch parent for writes)
    FM_CHILD_WS     — workspace id to search / create a child in (defaults to FM_PARENT_WS)
    FM_MATCH_FIELD  — field systemName to test the match query on (e.g. MPN or TITLE)
    FM_MATCH_VALUE  — value to search for
    FM_ALLOW_WRITE  — "1" to add → update → remove a scratch BOM row on FM_PARENT_ITEM

    python docs/dev/api_probe_bom.py
"""

import importlib
import json
import os
import sys
import types

ADDIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG = 'plmws'


def _install_adsk_stub():
    class _Any(types.ModuleType):
        def __getattr__(self, name):
            return _AnyObj()

    class _AnyObj:
        def __getattr__(self, name):
            return _AnyObj()
        def __call__(self, *a, **k):
            return _AnyObj()

    for name in ('adsk', 'adsk.core', 'adsk.fusion', 'adsk.electron'):
        if name not in sys.modules:
            sys.modules[name] = _Any(name)


def _bootstrap_package():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [ADDIN_ROOT]
    sys.modules[PKG] = pkg


class Ctx:
    def __init__(self, tenant, user_id, bearer):
        self.tenant = tenant
        self.user_id = user_id
        self.bearer = bearer

    def refresh_bearer(self):
        return None


def _show(label, value):
    print('\n' + '=' * 78 + '\n' + label + '\n' + '-' * 78)
    txt = json.dumps(value, indent=2, default=str) if isinstance(value, (dict, list)) else repr(value)
    enc = (getattr(sys.stdout, 'encoding', None) or 'utf-8')
    print(txt.encode(enc, 'replace').decode(enc, 'replace')[:3500])


def _search_field(client, ws, field, value):
    """Try the scoped field-equals query, then a full-text phrase, filtered to the ws."""
    import re
    forms = []
    if field:
        forms.append(f'ITEM_DETAILS:{field}="{value}" AND (workspaceId={ws})')
    forms.append(f'"{value}" AND (workspaceId={ws})')
    for q in forms:
        res, err = client.search_results([q], revision=1, limit=10, pre_formatted=True)
        items = (res or {}).get('items') or []
        hits = [it for it in items if not it.get('deleted')
                and re.search(rf'/workspaces/{ws}/items/(\d+)', it.get('__self__', ''))]
        print(f'   query={q!r} -> {len(items)} raw, {len(hits)} in-ws (err={err})')
        if hits:
            return hits[0].get('__self__'), q
    return None, None


def main():
    token = os.environ.get('FM_TOKEN', '').strip()
    tenant = os.environ.get('FM_TENANT', '').strip()
    parent_ws = os.environ.get('FM_PARENT_WS', '').strip()
    parent_item = os.environ.get('FM_PARENT_ITEM', '').strip()
    child_ws = os.environ.get('FM_CHILD_WS', '').strip() or parent_ws
    match_field = os.environ.get('FM_MATCH_FIELD', '').strip()
    match_value = os.environ.get('FM_MATCH_VALUE', '').strip()
    allow_write = os.environ.get('FM_ALLOW_WRITE', '').strip() == '1'
    if not token or not tenant:
        print('Set FM_TOKEN + FM_TENANT (and FM_PARENT_WS/FM_PARENT_ITEM for BOM tests).')
        return 2

    _install_adsk_stub()
    _bootstrap_package()
    fm = importlib.import_module(f'{PKG}.services.fm_client').FmClient(Ctx(tenant, '', token))
    bom = importlib.import_module(f'{PKG}.services.bom')

    print(f'Probing tenant={tenant!r} parentWs={parent_ws!r} parentItem={parent_item!r} write={allow_write}')

    # 1) match query
    if match_value:
        print('\n=== match query (', match_field or '(full-text)', '=', match_value, ') ===')
        link, form = _search_field(fm, child_ws, match_field, match_value)
        print('   -> matched link:', link, '| via:', form)

    # 2) read_bom
    if parent_ws and parent_item:
        result, err = fm.read_bom(parent_ws, parent_item)
        if err:
            print('\nread_bom error:', err)
        else:
            _show(f'read_bom({parent_ws},{parent_item}) — {result["totalCount"]} rows', result['edges'][:8])
        # working-version pre-flight
        wid = fm.working_item_id(parent_ws, parent_item)
        print('   working_item_id ->', wid, '(same as parent if already working / no versions)')

    # 3) gated write: add -> update -> remove a scratch row (child = the parent itself is invalid;
    #    require a real child link via FM_CHILD_ITEM)
    child_item = os.environ.get('FM_CHILD_ITEM', '').strip()
    if allow_write and parent_ws and parent_item and child_item:
        parent_link = bom.item_link(parent_ws, parent_item)
        child_link = bom.item_link(child_ws, child_item)
        print(f'\n>>> GATED WRITE add row child={child_link} -> parent={parent_link}')
        added, aerr = fm.add_bom_row(parent_link, child_link, quantity=1, item_number=None)
        print('   add ->', ('OK '+str(added)) if not aerr else ('ERR '+str(aerr)[:200]))
        edge_link = (added or {}).get('edgeLink') if added else None
        if edge_link:
            import re
            m = re.search(r'/bom-items/(\d+)', edge_link)
            edge_id = m.group(1) if m else None
            if edge_id:
                uerr = fm.update_bom_row(parent_link, edge_id, child_link, quantity=3)
                print('   update qty->3 ->', 'OK' if not uerr else ('ERR '+str(uerr)[:200]))
            rerr = fm.remove_bom_row(edge_link)
            print('   remove ->', 'OK' if not rerr else ('ERR '+str(rerr)[:200]))
        else:
            print('   (no edge Location returned — inspect add response)')
    elif allow_write:
        print('\nGated write skipped — set FM_PARENT_WS/FM_PARENT_ITEM/FM_CHILD_ITEM.')

    print('\nDone.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
