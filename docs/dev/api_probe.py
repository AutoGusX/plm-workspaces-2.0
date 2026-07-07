"""Read-only CLI probe for the real services/fm_client layer (NOT shipped).

Runs the actual http_client + FmClient code against a live Fusion Manage tenant so we
can verify request/response shapes outside Fusion. Credentials come from the env:

    FM_TOKEN   — a 3-legged OAuth bearer access token
    FM_TENANT  — tenant subdomain (e.g. autodesk8937)
    FM_USER_ID — optional x-User-id (email / fusion user id); blank is fine for most reads

Read-only: it never calls create/update/transition. Usage:
    python docs/dev/api_probe.py
"""

import importlib
import json
import os
import sys
import types

ADDIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG = 'plmws'


def _install_adsk_stub():
    """Make `import adsk[.core/.fusion]` succeed outside Fusion (we never call into it
    for read-only probes; the service modules only import it at module top)."""
    class _Any(types.ModuleType):
        def __getattr__(self, name):
            return _AnyObj()

    class _AnyObj:
        def __getattr__(self, name):
            return _AnyObj()
        def __call__(self, *a, **k):
            return _AnyObj()

    for name in ('adsk', 'adsk.core', 'adsk.fusion'):
        if name not in sys.modules:
            sys.modules[name] = _Any(name)


def _bootstrap_package():
    """Register the add-in root (folder name has spaces) as importable package `plmws`
    so the relative imports inside core/ and services/ resolve."""
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [ADDIN_ROOT]
    sys.modules[PKG] = pkg


class Ctx:
    """Duck-typed RequestContext: FmClient only reads these three + refresh_bearer()."""
    def __init__(self, tenant, user_id, bearer):
        self.tenant = tenant
        self.user_id = user_id
        self.bearer = bearer

    def refresh_bearer(self):
        # No refresh in the probe — a 401 just surfaces as unauthorized.
        return None


def _safe(s):
    """Avoid Windows cp1252 console UnicodeEncodeError on FM data."""
    enc = (getattr(sys.stdout, 'encoding', None) or 'utf-8')
    return s.encode(enc, errors='replace').decode(enc, errors='replace')


def _show(label, value):
    print('\n' + '=' * 78)
    print(label)
    print('-' * 78)
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, indent=2, default=str)
        print(_safe(text[:4000] + ('\n... [truncated]' if len(text) > 4000 else '')))
    else:
        print(_safe(repr(value)))


def main():
    token = os.environ.get('FM_TOKEN', '').strip()
    tenant = os.environ.get('FM_TENANT', '').strip()
    user_id = os.environ.get('FM_USER_ID', '').strip()
    if not token or not tenant:
        print('Set FM_TOKEN and FM_TENANT in the environment first.')
        return 2

    _install_adsk_stub()
    _bootstrap_package()
    fm_client_mod = importlib.import_module(f'{PKG}.services.fm_client')
    client = fm_client_mod.FmClient(Ctx(tenant, user_id, token))

    print(f'Probing tenant={tenant!r} user_id={user_id!r} token=...{token[-8:]}')

    # 1) Workspaces / entitlements ------------------------------------------------
    ws = client.workspaces()
    _show('client.workspaces()', ws)
    sn_map = (ws or {}).get('system_name_to_id') if isinstance(ws, dict) else None

    # Resolve Change Orders workspace id (fallback 9).
    change_ws = (sn_map or {}).get('WS_CHANGE_ORDERS', '9')
    print(f'\n>>> WS_CHANGE_ORDERS resolved to workspace id: {change_ws}')

    # 2) Outstanding work ---------------------------------------------------------
    ow = client.outstanding_work()
    _show('client.outstanding_work()', ow)

    # 3) Tableaus (saved views) for Change Orders ---------------------------------
    tabs = client.tableaus(change_ws)
    _show(f'client.tableaus({change_ws})', tabs)

    # Pick the first tableau id from whatever shape came back.
    first_tableau = None
    cand = tabs[0] if isinstance(tabs, tuple) else tabs
    rows = cand.get('tableaus') if isinstance(cand, dict) else cand
    if isinstance(rows, list) and rows:
        first_tableau = (rows[0] or {}).get('id') if isinstance(rows[0], dict) else None

    # 4) Tableau data (first page) ------------------------------------------------
    if first_tableau:
        td = client.tableau_data(change_ws, first_tableau, 1, 5)
        _show(f'client.tableau_data({change_ws}, {first_tableau}, page=1, size=5)', td)
        # 5) Item detail for the first row, if present.
        data = td[0] if isinstance(td, tuple) else td
        rrows = data.get('rows') if isinstance(data, dict) else None
        if isinstance(rrows, list) and rrows:
            first_item = rrows[0].get('itemId')
            if first_item:
                detail = client.item_detail(change_ws, first_item)
                _show(f'client.item_detail({change_ws}, {first_item})', detail)

    # 6) View fields for Change Orders -------------------------------------------
    vf = client.view_fields(change_ws)
    _show(f'client.view_fields({change_ws})', vf)

    print('\nDone.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
