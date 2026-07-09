# Electronics-BOM export orchestration — resolve a plan (no writes) and push it.
#
# Pure module: takes an FmClient (all HTTP), no adsk. Safe on async threads. Verified
# model (FAA Sandbox, ws 506/505/212, 2026-07-09):
#   Component Item (WS_ITEMS): VALUE/FOOTPRINT/DESCRIPTION + MANUFACTURER_PN/MANUFACTURER
#     text; TITLE required; REFERENCE_MPN is a NEVER-editable sync reverse.
#   MPN record (WS_MANUFACTURER_PN): MANUFACTURER_PN + REFERENCE_ITEM(->Item) +
#     MANUFACTURER(->WS_SUPPLIERS). The Item<->MPN link is DRIVEN FROM THE MPN SIDE.
#   Parent PCBA Item (WS_ITEMS): found/created by SOURCE_DESIGN_ID; children are BOM rows.
#
# Push order (safe): suppliers -> component items -> MPN records (link) -> parent ->
# BOM reconcile (adds -> removes -> updates). Per-line failures are isolated.

import re

from ...services import bom as _bom
from ...services import item_write as _iw


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _field_value(item, field_id):
    """Read a field value from an item-detail dict by systemName."""
    for sec in (item or {}).get('sections') or []:
        for f in sec.get('fields') or []:
            if not isinstance(f, dict):
                continue
            ref = f.get('__self__') or f.get('link') or ''
            m = re.search(r'/fields/([^/?]+)', ref)
            if (m and m.group(1) == field_id) or f.get('id') == field_id:
                return f.get('value')
    return None


def _find_by_field(client, ws, field, value):
    """Find the first in-workspace, non-deleted item matching a field value.
    Tries the scoped field-equals query, then a full-text phrase (both filtered to ws)."""
    value = str(value or '').strip()
    if not value:
        return None
    forms = []
    if field:
        forms.append(f'ITEM_DETAILS:{field}="{value}" AND (workspaceId={ws})')
    forms.append(f'"{value}" AND (workspaceId={ws})')
    seen_q = set()
    for q in forms:
        if q in seen_q:
            continue
        seen_q.add(q)
        res, err = client.search_results([q], revision=1, limit=20, pre_formatted=True)
        if err:
            continue
        for it in (res or {}).get('items') or []:
            if it.get('deleted'):
                continue
            m = re.search(rf'/workspaces/{ws}/items/(\d+)', it.get('__self__', '') or '')
            if m:
                return {'id': m.group(1),
                        'link': _bom.item_link(ws, m.group(1)),
                        'descriptor': it.get('descriptor', '')}
    return None


def _resolve_ws(client, config):
    """Resolve the items/mpn/supplier workspace systemNames -> numeric ids."""
    res = client.workspaces()
    if not res.get('success'):
        return None, (res.get('error') or 'Could not list workspaces.')
    sn = res.get('system_name_to_id', {})
    items_ws = sn.get(config.get('itemsWs'))
    if not items_ws:
        return None, ('Items workspace "%s" not found — set it in Configure.'
                      % config.get('itemsWs'))
    return {'items': items_ws,
            'mpn': sn.get(config.get('mpnWs')),
            'supplier': sn.get(config.get('supplierWs'))}, None


def _compose_title(config, row):
    """TITLE (required on the Item) from a single BOM property, or a composed template."""
    source = (config.get('titleSource') or 'mpn')
    if source == 'composed':
        try:
            title = (config.get('titleTemplate') or '{mpn}').format(
                mpn=row.get('mpn') or '', value=row.get('value') or '',
                footprint=row.get('footprint') or '', manufacturer=row.get('manufacturer') or '',
                description=row.get('description') or '')
        except (KeyError, IndexError, ValueError):
            title = row.get('mpn') or ''
    else:
        title = row.get(source) or ''
    title = str(title).strip(' -')
    if not title:   # fallbacks so the required field is never blank
        title = (row.get('mpn') or ((row.get('value') or '') + ' ' + (row.get('footprint') or ''))).strip()
        title = title or 'Electronics component'
    return title


def _item_field_values(config, row, title):
    """Build the WS_ITEMS field_values for a component from the mapping."""
    m = config.get('itemMapping', {})
    fv = {'TITLE': title}
    for bom_key, sys_name in m.items():
        if not sys_name:
            continue
        val = row.get(bom_key)
        if val not in (None, ''):
            fv[sys_name] = val
    return fv


# ---------------------------------------------------------------------------
# resolve (no writes)
# ---------------------------------------------------------------------------
def resolve_plan(client, bom, config):
    """Build the export plan without writing. Returns {success, ...plan} or {success:false}."""
    ws, err = _resolve_ws(client, config)
    if err:
        return {'success': False, 'error': err}
    items_ws, mpn_ws = ws['items'], ws['mpn']
    mpn_map = config.get('mpnMapping', {})
    ref_item_field = mpn_map.get('referenceItemField', 'REFERENCE_ITEM')
    mpn_num_field = mpn_map.get('mpn', 'MANUFACTURER_PN')
    item_mpn_field = config.get('itemMapping', {}).get('mpn', 'MANUFACTURER_PN')
    parent_key = config.get('parentKeyField', 'SOURCE_DESIGN_ID')

    rows = bom.get('rows') or []
    design_id = str(bom.get('designId') or bom.get('design') or '').strip()

    # --- MPN + component resolution (cache per unique MPN) ---
    mpn_cache = {}   # mpn string -> {mpnId, mpnLink, itemId, itemLink}
    lines = []
    counts = {'itemsCreate': 0, 'itemsUpdate': 0, 'mpnCreate': 0,
              'bomAdd': 0, 'bomUpdate': 0, 'bomRemove': 0, 'warnings': 0}

    for row in rows:
        mpn = (row.get('mpn') or '').strip()
        warnings = []
        rec = mpn_cache.get(mpn) if mpn else None
        if mpn and rec is None:
            rec = {'mpnId': None, 'mpnLink': None, 'itemId': None, 'itemLink': None}
            if mpn_ws:
                hit = _find_by_field(client, mpn_ws, mpn_num_field, mpn)
                if hit:
                    rec['mpnId'] = hit['id']
                    rec['mpnLink'] = hit['link']
                    detail, _e, _err = client.item_detail(mpn_ws, hit['id'])
                    ref = _field_value(detail, ref_item_field)
                    if isinstance(ref, dict) and ref.get('link'):
                        im = re.search(r'/items/(\d+)', ref['link'])
                        rec['itemLink'] = ref['link']
                        rec['itemId'] = im.group(1) if im else None
            mpn_cache[mpn] = rec
        rec = rec or {'mpnId': None, 'mpnLink': None, 'itemId': None, 'itemLink': None}

        # If no MPN reference match, try matching the component item directly by its
        # MANUFACTURER_PN text field (handles items created before an MPN record existed).
        if not rec.get('itemId') and mpn:
            direct = _find_by_field(client, items_ws, item_mpn_field, mpn)
            if direct:
                rec['itemId'] = direct['id']
                rec['itemLink'] = direct['link']

        if not mpn:
            warnings.append('No MPN on this line — will create an unlinked component.')

        item_status = 'update' if rec.get('itemId') else 'create'
        mpn_status = 'match' if rec.get('mpnId') else ('create' if mpn else 'skip')
        if item_status == 'create':
            counts['itemsCreate'] += 1
        else:
            counts['itemsUpdate'] += 1
        if mpn_status == 'create':
            counts['mpnCreate'] += 1
        counts['warnings'] += len(warnings)

        lines.append({
            'mpn': mpn, 'manufacturer': row.get('manufacturer'),
            'value': row.get('value'), 'footprint': row.get('footprint'),
            'quantity': row.get('quantity'),
            'referenceDesignators': row.get('referenceDesignators') or [],
            'itemStatus': item_status, 'itemId': rec.get('itemId'), 'itemLink': rec.get('itemLink'),
            'mpnStatus': mpn_status, 'mpnId': rec.get('mpnId'), 'mpnLink': rec.get('mpnLink'),
            'warnings': warnings,
        })

    # --- parent + BOM diff ---
    parent = None
    parent_bom = []
    if design_id and parent_key:
        parent = _find_by_field(client, items_ws, parent_key, design_id)
    if parent:
        working = client.working_item_id(items_ws, parent['id'])
        parent['workingId'] = working
        bres, berr = client.read_bom(items_ws, working)
        if not berr and bres:
            parent_bom = bres.get('edges') or []

    existing_by_child = {e['childId']: e for e in parent_bom if e.get('childId')}
    desired_child_ids = set()
    for ln in lines:
        cid = ln.get('itemId')
        if ln['itemStatus'] == 'create' or not cid:
            ln['bomStatus'] = 'add'
            counts['bomAdd'] += 1
        else:
            desired_child_ids.add(cid)
            existing = existing_by_child.get(cid)
            if not existing:
                ln['bomStatus'] = 'add'
                counts['bomAdd'] += 1
            elif float(existing.get('quantity') or 0) != float(ln.get('quantity') or 0):
                ln['bomStatus'] = 'update'
                ln['edgeId'] = existing.get('edgeId')
                ln['edgeLink'] = existing.get('edgeLink')
                counts['bomUpdate'] += 1
            else:
                ln['bomStatus'] = 'noop'

    removals = []
    for e in parent_bom:
        if e.get('childId') and e['childId'] not in desired_child_ids:
            removals.append({'childId': e['childId'], 'edgeId': e.get('edgeId'),
                             'edgeLink': e.get('edgeLink'), 'quantity': e.get('quantity')})
    counts['bomRemove'] = len(removals)

    return {
        'success': True,
        'design': bom.get('design'), 'designId': design_id,
        'wsIds': ws,
        'parent': {'status': 'existing' if parent else 'create',
                   'itemId': parent['id'] if parent else None,
                   'workingId': parent.get('workingId') if parent else None,
                   'existingRows': len(parent_bom)},
        'lines': lines,
        'removals': removals,
        'counts': counts,
    }


# ---------------------------------------------------------------------------
# push (writes)
# ---------------------------------------------------------------------------
def _resolve_supplier(client, supplier_ws, name, name_field, results):
    """Find (or create) a supplier by NAME; return its link or None (best-effort)."""
    if not supplier_ws or not name:
        return None
    hit = _find_by_field(client, supplier_ws, name_field, name)
    if hit:
        return hit['link']
    # Try to create a minimal supplier; tolerate required-field failures.
    r = _iw.write_item(client, supplier_ws, 'create', {name_field: name, 'TITLE': name})
    if r.get('success') and r.get('itemId'):
        return _bom.item_link(supplier_ws, r['itemId'])
    results.setdefault('warnings', []).append(
        f'Could not create supplier "{name}" ({r.get("error")}) — manufacturer left blank.')
    return None


def push_plan(client, bom, config):
    """Execute the export. Re-resolves the plan, then writes in the safe order.
    Returns {success, results:[per-line], parent, counts, warnings}."""
    plan = resolve_plan(client, bom, config)
    if not plan.get('success'):
        return plan
    ws = plan['wsIds']
    items_ws, mpn_ws, supplier_ws = ws['items'], ws['mpn'], ws['supplier']
    item_map = config.get('itemMapping', {})
    mpn_map = config.get('mpnMapping', {})
    supplier_map = config.get('supplierMapping', {})
    parent_key = config.get('parentKeyField', 'SOURCE_DESIGN_ID')
    ref_item_field = mpn_map.get('referenceItemField', 'REFERENCE_ITEM')
    mpn_num_field = mpn_map.get('mpn', 'MANUFACTURER_PN')
    mfr_ref_field = mpn_map.get('manufacturerField', 'MANUFACTURER')
    name_field = supplier_map.get('nameField', 'NAME')
    refdes_field = config.get('refDesField') or ''   # BOM-row (viewdef) field link

    out = {'success': True, 'results': [], 'warnings': [],
           'counts': {'itemsCreated': 0, 'itemsUpdated': 0, 'mpnCreated': 0,
                      'bomAdded': 0, 'bomUpdated': 0, 'bomRemoved': 0, 'failed': 0}}

    child_links = []   # (itemLink, quantity, refdes) for BOM reconcile

    for ln in plan['lines']:
        row = ln
        res = {'mpn': ln.get('mpn'), 'title': None, 'itemId': ln.get('itemId'),
               'action': ln['itemStatus'], 'ok': False, 'error': None}
        try:
            title = _compose_title(config, row)
            res['title'] = title
            # 1) component item (create or update)
            fv = _item_field_values(config, row, title)
            if ln['itemStatus'] == 'update' and ln.get('itemId'):
                r = _iw.write_item(client, items_ws, 'edit', fv, item_id=ln['itemId'])
                if r.get('success'):
                    out['counts']['itemsUpdated'] += 1
            else:
                r = _iw.write_item(client, items_ws, 'create', fv)
                if r.get('success'):
                    out['counts']['itemsCreated'] += 1
                    ln['itemId'] = r.get('itemId')
            if not r.get('success'):
                res['error'] = r.get('error') or 'Item write failed.'
                out['counts']['failed'] += 1
                out['results'].append(res)
                continue
            item_id = ln['itemId']
            item_link = _bom.item_link(items_ws, item_id)
            res['itemId'] = item_id

            # 2) MPN record (create + link back to the item) when there's an MPN
            if ln.get('mpn'):
                supplier_link = _resolve_supplier(
                    client, supplier_ws, row.get('manufacturer'), name_field, out)
                if ln['mpnStatus'] == 'create' and mpn_ws:
                    mfv = {mpn_num_field: ln['mpn'],
                           ref_item_field: {'link': item_link}}
                    if supplier_link:
                        mfv[mfr_ref_field] = {'link': supplier_link}
                    mr = _iw.write_item(client, mpn_ws, 'create', mfv)
                    if mr.get('success'):
                        out['counts']['mpnCreated'] += 1
                    else:
                        out.setdefault('warnings', []).append(
                            f'MPN record for {ln["mpn"]} not created: {mr.get("error")}')
                elif ln.get('mpnId') and mpn_ws and not ln.get('itemId') is None:
                    # MPN exists but wasn't linked to this item — set REFERENCE_ITEM.
                    _iw.write_item(client, mpn_ws, 'edit', {ref_item_field: {'link': item_link}},
                                   item_id=ln['mpnId'])

            child_links.append((item_link, row.get('quantity'), row.get('referenceDesignators')))
            res['ok'] = True
        except Exception as e:  # noqa: BLE001 — isolate per-line failures
            res['error'] = str(e) or 'Unexpected error.'
            out['counts']['failed'] += 1
        out['results'].append(res)

    # 3) parent (create or find) + working version
    parent = plan['parent']
    parent_id = parent.get('workingId') or parent.get('itemId')
    if parent['status'] == 'create':
        design = plan.get('design') or plan.get('designId') or 'PCBA'
        pr = _iw.write_item(client, items_ws, 'create',
                            {'TITLE': design, parent_key: plan.get('designId') or design})
        if pr.get('success'):
            parent_id = pr.get('itemId')
        else:
            out['success'] = False
            out['error'] = f'Parent item not created: {pr.get("error")}'
            return out
    else:
        parent_id = client.working_item_id(items_ws, parent_id)
    out['parent'] = {'itemId': parent_id}
    parent_link = _bom.item_link(items_ws, parent_id)

    # 4) BOM reconcile — adds, then removes, then updates (per the FM contract)
    existing, berr = client.read_bom(items_ws, parent_id)
    existing_edges = (existing or {}).get('edges', []) if not berr else []
    existing_children = {e['childId']: e for e in existing_edges if e.get('childId')}
    desired_ids = set()
    next_num = (max([int(e['itemNumber'] or 0) for e in existing_edges], default=0)) + 1

    for (item_link, qty, refdes) in child_links:
        cid = re.search(r'/items/(\d+)', item_link)
        cid = cid.group(1) if cid else None
        if cid:
            desired_ids.add(cid)
        # Reference designators are a BOM-row (viewdef) field, written via fields=[...].
        row_fields = None
        if refdes_field and refdes:
            row_fields = [{'link': refdes_field, 'value': ', '.join(refdes)}]
        edge = existing_children.get(cid)
        if edge:
            qty_changed = float(edge.get('quantity') or 0) != float(qty or 1)
            if qty_changed or row_fields:
                err = client.update_bom_row(parent_link, edge['edgeId'], item_link, qty or 1,
                                            fields=row_fields)
                if not err and qty_changed:
                    out['counts']['bomUpdated'] += 1
        else:
            added, err = client.add_bom_row(parent_link, item_link, qty or 1,
                                            item_number=next_num, fields=row_fields)
            if not err:
                out['counts']['bomAdded'] += 1
                next_num += 1
            else:
                out.setdefault('warnings', []).append(f'BOM row add failed for {item_link}: {err}')

    # removals — full sync: drop rows no longer in the design
    for e in existing_edges:
        if e.get('childId') and e['childId'] not in desired_ids:
            err = client.remove_bom_row(_bom._abs(client, e['edgeLink']))
            if not err:
                out['counts']['bomRemoved'] += 1

    return out
