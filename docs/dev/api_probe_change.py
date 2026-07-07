"""Probe the change-type workspaces to find the columns they share (for the unified
Change Management table design). Read-only. Reuses api_probe's bootstrap.

Env: FM_TOKEN, FM_TENANT (+ optional FM_USER_ID).
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_probe as base  # noqa: E402

# Base systemNames we treat as "change management" (＋ optional _N variants in real code).
CHANGE_BASES = [
    'WS_CHANGE_ORDERS', 'WS_LEAN_CHANGE_ORDERS', 'WS_CHANGE_REQUESTS',
    'WS_CHANGE_TASKS', 'WS_PROBLEM_REPORTS',
]


def _rows_cols(td):
    """tableau_data returns (dict, error) or dict; normalize to (columns, rows)."""
    d = td[0] if isinstance(td, tuple) else td
    if not isinstance(d, dict):
        return [], []
    return d.get('columns') or [], d.get('rows') or []


def main():
    token = os.environ.get('FM_TOKEN', '').strip()
    tenant = os.environ.get('FM_TENANT', '').strip()
    if not token or not tenant:
        print('Set FM_TOKEN and FM_TENANT.')
        return 2
    base._install_adsk_stub()
    base._bootstrap_package()
    fm = importlib.import_module(f'{base.PKG}.services.fm_client')
    client = fm.FmClient(base.Ctx(tenant, os.environ.get('FM_USER_ID', '').strip(), token))

    ws = client.workspaces()
    sn_map = ws.get('system_name_to_id', {}) if isinstance(ws, dict) else {}

    # Resolve every change workspace incl. _N variants (e.g. WS_LEAN_CHANGE_ORDERS_1).
    targets = []  # (systemName, title-ish, wsId)
    for sn, wid in sn_map.items():
        for b in CHANGE_BASES:
            if sn == b or sn.startswith(b + '_'):
                targets.append((sn, wid))
                break
    print('Change workspaces discovered:', targets)

    col_sets = {}
    for sn, wid in targets:
        tabs = client.tableaus(wid)
        rows_t = tabs[0] if isinstance(tabs, tuple) else tabs
        tlist = rows_t if isinstance(rows_t, list) else (rows_t.get('tableaus') if isinstance(rows_t, dict) else [])
        # default tableau = type DEFAULT, else first
        default = None
        for t in (tlist or []):
            if (t or {}).get('type') == 'DEFAULT':
                default = t.get('id'); break
        if default is None and tlist:
            default = tlist[0].get('id')
        if default is None:
            print(f'\n{sn} (ws {wid}): NO tableaus')
            continue
        td = client.tableau_data(wid, default, 1, 3)
        cols, rows = _rows_cols(td)
        col_ids = [c.get('id') for c in cols]
        col_sets[sn] = set(col_ids)
        print(f'\n{sn} (ws {wid}) default tableau {default}: columns={col_ids}')
        if rows:
            sample = rows[0].get('fields', {})
            print('  sample row field ids:', list(sample.keys()))

    if col_sets:
        common = set.intersection(*col_sets.values()) if len(col_sets) > 1 else next(iter(col_sets.values()))
        print('\n=== COLUMNS COMMON TO ALL CHANGE WORKSPACES ===')
        print(sorted(common))
    return 0


if __name__ == '__main__':
    sys.exit(main())
