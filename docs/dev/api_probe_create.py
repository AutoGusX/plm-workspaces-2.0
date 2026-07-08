"""Probe for the create/update payload builder (services/item_payload) — NOT shipped.

Runs the REAL services layer against a live Fusion Manage tenant so we can verify the
create/update request-body SHAPE (and settle the date write-format) outside Fusion.

Env:
    FM_TOKEN        — 3-legged OAuth bearer access token (needs data:write/create to write)
    FM_TENANT       — tenant subdomain (e.g. autodesk8937)
    FM_USER_ID      — optional x-User-id
    FM_WS           — workspace id to probe (e.g. 9)
    FM_ITEM         — an existing item id in FM_WS (used for the edit sample + gated write)
    FM_ALLOW_WRITE  — set to "1" to perform a REAL, revertible single-field UPDATE
    FM_WRITE_FIELD  — field systemName to poke on the gated update (default: first editable text field)
    FM_DATE_FIELD   — optional date field systemName; when set with FM_ALLOW_WRITE, tries all
                      three DATE_MODE candidates and reports which one the tenant accepts

Read-only unless FM_ALLOW_WRITE=1, and even then the single field is written then reverted.

    python docs/dev/api_probe_create.py
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

    for name in ('adsk', 'adsk.core', 'adsk.fusion'):
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


def _safe(s):
    enc = (getattr(sys.stdout, 'encoding', None) or 'utf-8')
    return s.encode(enc, errors='replace').decode(enc, errors='replace')


def _show(label, value):
    print('\n' + '=' * 78)
    print(label)
    print('-' * 78)
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, indent=2, default=str)
        print(_safe(text[:6000] + ('\n... [truncated]' if len(text) > 6000 else '')))
    else:
        print(_safe(repr(value)))


def _extract_values_from_item(item):
    """Mirror engine.js extractFormValuesFromItem: {fieldId: value} from item sections."""
    import re
    out = {}
    for sec in (item or {}).get('sections') or []:
        for f in sec.get('fields') or []:
            m = re.search(r'fields/([^/?]+)', f.get('__self__') or f.get('link') or '')
            if m:
                out[m.group(1)] = f.get('value')
    return out


def _assert_body_shape(sections, label):
    """Structural assertions that mirror CLONE_FIELD_RULES."""
    problems = []
    for sec in sections or []:
        if 'link' not in sec:
            problems.append('section missing link')
        for fld in sec.get('fields') or []:
            if set(fld.keys()) - {'__self__', 'value'}:
                problems.append(f"field has extra keys: {sorted(fld.keys())}")
            self_ref = fld.get('__self__') or ''
            if '/items/' in self_ref:
                problems.append(f'field self is item-scoped (should be workspace): {self_ref}')
            if '/views/' not in self_ref or '/fields/' not in self_ref:
                problems.append(f'field self not view-scoped: {self_ref}')
    print('\n' + ('OK  ' if not problems else 'FAIL') + f' shape assertions — {label}')
    for p in problems:
        print('   - ' + p)
    return not problems


def main():
    token = os.environ.get('FM_TOKEN', '').strip()
    tenant = os.environ.get('FM_TENANT', '').strip()
    user_id = os.environ.get('FM_USER_ID', '').strip()
    ws = os.environ.get('FM_WS', '').strip()
    item_id = os.environ.get('FM_ITEM', '').strip()
    allow_write = os.environ.get('FM_ALLOW_WRITE', '').strip() == '1'
    write_field = os.environ.get('FM_WRITE_FIELD', '').strip()
    date_field = os.environ.get('FM_DATE_FIELD', '').strip()
    if not token or not tenant or not ws:
        print('Set FM_TOKEN, FM_TENANT and FM_WS first (FM_ITEM for edit/write tests).')
        return 2

    _install_adsk_stub()
    _bootstrap_package()
    fm = importlib.import_module(f'{PKG}.services.fm_client').FmClient(Ctx(tenant, user_id, token))
    payload = importlib.import_module(f'{PKG}.services.item_payload')

    print(f'Probing tenant={tenant!r} ws={ws!r} item={item_id!r} token=...{token[-8:]} write={allow_write}')

    # 1) Extended field metadata ------------------------------------------------
    fields, ferr = fm.workspace_fields(ws)
    if ferr:
        print(f'workspace_fields error: {ferr}')
        return 1
    fields_meta = {f['id']: f for f in fields if f.get('id')}
    sample = [{k: f.get(k) for k in ('id', 'title', 'editability', 'derived',
              'derivedFieldSource', 'formulaField', 'isSystemField')}
              for f in fields[:12]]
    _show(f'workspace_fields({ws}) — first 12 (write-path flags)', sample)

    # 2) Sections + classificationId -------------------------------------------
    sec_result, serr = fm.sections(ws)
    if serr:
        print(f'sections error: {serr}')
        return 1
    _show(f'sections({ws}) — classificationId per section',
          [{'id': s['id'], 'name': s['name'], 'classificationId': s.get('classificationId'),
            'nFields': len(s.get('fields') or [])} for s in sec_result.get('sections', [])])
    view_id = sec_result.get('viewId', 1)

    if not item_id:
        print('\nNo FM_ITEM set — skipping edit/create body build. Done.')
        return 0

    # 3) Edit-body build from the item's current values -------------------------
    item, _etag, ierr = fm.item_detail(ws, item_id)
    if ierr:
        print(f'item_detail error: {ierr}')
        return 1
    item_values = _extract_values_from_item(item)
    edit_meta = {k: dict(v) for k, v in fields_meta.items()}
    for fid, is_sys in payload.system_fields_from_item(item).items():
        if fid in edit_meta:
            edit_meta[fid]['isSystemField'] = is_sys
    edit_sections, edit_missing = payload.build_item_body(
        'edit', item_values, edit_meta, payload.sections_from_item(item), ws, view_id)
    _show('build_item_body(EDIT) from item values', edit_sections)
    print(f'edit missing_required: {edit_missing}')
    _assert_body_shape(edit_sections, 'edit')

    # 4) Create-body build (clone of the item, sans system/derived) -------------
    # Use the item itself as the membership reference (mirrors the create action).
    create_struct = payload.sections_from_item(item, ws, for_create=True)
    create_sections, create_missing = payload.build_item_body(
        'create', item_values, fields_meta, create_struct, ws, view_id)
    _show('build_item_body(CREATE) from item values (clone-style)', create_sections)
    print(f'create missing_required: {create_missing}')
    _assert_body_shape(create_sections, 'create')

    # 5) Date coercion preview across the three candidate write formats ---------
    if date_field:
        raw = item_values.get(date_field)
        print(f'\nDate coercion for {date_field!r} raw={raw!r}:')
        for mode in ('date', 'iso', 'epoch'):
            payload.DATE_MODE = mode
            print(f'   DATE_MODE={mode:5s} -> {payload._coerce_date(raw)!r}')
        payload.DATE_MODE = 'date'

    # 6) Gated, revertible single-field write ----------------------------------
    if allow_write and item_id:
        # Pick a simple editable single-line-text field to poke (or FM_WRITE_FIELD).
        target = write_field
        if not target:
            for f in fields:
                tt = (f.get('type') or {}).get('title', '').lower() if isinstance(f.get('type'), dict) else ''
                ed = (f.get('editability') or 'ALWAYS').upper()
                if 'single line' in tt and ed == 'ALWAYS' and not f.get('formulaField') \
                        and not f.get('derived') and not f.get('isSystemField'):
                    target = f['id']
                    break
        if not target:
            print('\nNo editable text field found for gated write; set FM_WRITE_FIELD.')
            return 0
        original = item_values.get(target)
        test_val = ((original or '') + ' [probe]').strip() if isinstance(original, (str, type(None))) else original
        print(f'\n>>> GATED WRITE: field {target!r}  {original!r} -> {test_val!r}')
        secs, miss = payload.build_item_body('edit', {target: test_val}, edit_meta,
                                             payload.sections_from_item(item), ws, view_id)
        _show('write body', secs)
        err = fm.update_item(ws, item_id, secs, _etag)
        print(f'update_item result: {"OK 2xx" if not err else err}')
        # Revert (fetch fresh etag first).
        item2, etag2, _ = fm.item_detail(ws, item_id)
        secs_rev, _ = payload.build_item_body('edit', {target: original}, edit_meta,
                                              payload.sections_from_item(item2 or item), ws, view_id)
        err2 = fm.update_item(ws, item_id, secs_rev, etag2)
        print(f'revert result: {"OK 2xx" if not err2 else err2}')

    print('\nDone.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
