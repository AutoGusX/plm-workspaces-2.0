# item_payload — build the Fusion Manage v3 create/update request body.
#
# This is the single source of truth for the SHAPE of a create/update body. It is a
# pure module: no HTTP, no adsk, no I/O — it takes the raw field values collected by the
# form (web/core/engine.js formValues) plus field metadata + section structure, and
# returns the `{sections: [...]}` payload that FmClient.create_item / update_item send.
#
# The rules mirror the Chrome extension's clone feature, which was live-validated to
# HTTP 201 (see the fork's better-bom-builder/CLONE_FIELD_RULES.md + plm.helper.js
# normalizeFieldValue). The three server errors those rules defend against:
#   error.editable                     -> sent a NEVER / formula / system field
#   error.derived.invalidDerivedFieldValue -> sent a derived field without its source
#   custom Item-Create script crash    -> derived field null; fixed by sending it with source
#
# Key shape facts:
#   - field object is MINIMAL {__self__, value} — the create endpoint rejects extra keys.
#   - links are WORKSPACE-scoped for BOTH create and update:
#       field:   /api/v3/workspaces/{ws}/views/{v}/fields/{id}
#       section: /api/v3/workspaces/{ws}/sections/{sid}
#     (the PATCH URL already carries the item id, so the body stays workspace-scoped —
#      this matches the extension's editItem path.)

import re

_FIELD_ID_RE = re.compile(r'fields/([^/?]+)')
_SECTION_ID_RE = re.compile(r'sections/(\d+)')

# Date output format. VERIFIED against FAA Sandbox (autodesk8937, ws 9 FOLLOWUP_DATE,
# 2026-07-08): a bare 'YYYY-MM-DD' write returns 2xx and reads back unchanged. The other
# modes remain here as a one-line switch if a future tenant/field needs ISO or epoch.
DATE_MODE = 'date'  # 'date' -> YYYY-MM-DD | 'iso' -> YYYY-MM-DDT00:00:00Z | 'epoch' -> ms


# ---------------------------------------------------------------------------
# Section-structure adapters (membership + classificationId), one per source.
# ---------------------------------------------------------------------------
def sections_from_ws(sections_result, workspace_id):
    """Normalize the client.sections(ws) result -> section_struct entries.

    The /workspaces/{ws}/sections endpoint returns only section metadata (link, title) —
    NO field membership. So these entries have empty field_ids; on CREATE, membership is
    supplied from a reference item (sections_from_item with for_create=True) when available."""
    out = []
    for sec in (sections_result or {}).get('sections') or []:
        sid = str(sec.get('id') or '').strip()
        if not sid:
            continue
        out.append({
            'id': sid,
            'classificationId': sec.get('classificationId'),
            # Workspace-scoped section link (FM accepts this form on create).
            'link': sec.get('link') or f'/api/v3/workspaces/{workspace_id}/sections/{sid}',
            'field_ids': [f.get('id') for f in (sec.get('fields') or []) if f.get('id')],
            'field_links': {},
        })
    return out


def sections_from_item(item, workspace_id=None, for_create=False):
    """Normalize a raw item detail dict -> section_struct entries with authoritative
    membership + links + classificationId.

    for_create=False (EDIT): keep the item's own ITEM-scoped links verbatim — FM's mutation
      endpoint validates against those exact links (a reconstructed /workspaces/{ws}/sections/{id}
      is rejected with "Could not find section N in workspace W").
    for_create=True: rewrite links to the workspace-/view-scoped form a create body expects,
      reusing a reference item purely for field->section membership + classificationId."""
    out = []
    for sec in (item or {}).get('sections') or []:
        if not isinstance(sec, dict):
            continue
        raw_link = sec.get('__self__') or sec.get('link') or ''
        m = _SECTION_ID_RE.search(raw_link)
        sid = m.group(1) if m else str(sec.get('id') or '').strip()
        if not sid:
            continue
        field_ids, field_links = [], {}
        for f in sec.get('fields') or []:
            if not isinstance(f, dict):
                continue
            raw_flink = f.get('__self__') or f.get('link') or ''
            fm = _FIELD_ID_RE.search(raw_flink)
            if not fm:
                continue
            fid = fm.group(1)
            field_ids.append(fid)
            field_links[fid] = raw_flink  # item-scoped, used verbatim on edit
        if for_create:
            link = f'/api/v3/workspaces/{workspace_id}/sections/{sid}'
            field_links = {}  # create builds view-scoped field selfs itself
        else:
            link = raw_link
        out.append({'id': sid, 'classificationId': sec.get('classificationId'),
                    'link': link, 'field_ids': field_ids, 'field_links': field_links})
    return out


def system_fields_from_item(item):
    """Extract {fieldId: isSystemField} from a raw item detail — isSystemField is not
    reliably on the /fields endpoint, so on edit we merge it from the item."""
    flags = {}
    for sec in (item or {}).get('sections') or []:
        if not isinstance(sec, dict):
            continue
        for f in sec.get('fields') or []:
            if not isinstance(f, dict):
                continue
            fm = _FIELD_ID_RE.search(f.get('__self__') or f.get('link') or '')
            if fm and f.get('isSystemField'):
                flags[fm.group(1)] = True
    return flags


# ---------------------------------------------------------------------------
# Value coercion (port of plm.helper.js normalizeFieldValue).
# ---------------------------------------------------------------------------
def _type_title(meta):
    t = meta.get('type')
    if isinstance(t, dict):
        return (t.get('title') or '').strip().lower()
    return str(t or '').strip().lower()


def _link_obj(link, title=None):
    out = {'link': link}
    t = (title or '').strip() if isinstance(title, str) else ''
    if t:
        out['title'] = t
    return out


def _coerce_date(raw):
    if raw in (None, ''):
        return None
    s = str(raw).strip()
    if not s:
        return None
    if DATE_MODE == 'date':
        return s[:10]
    if DATE_MODE == 'iso':
        return s[:10] + 'T00:00:00Z'
    if DATE_MODE == 'epoch':
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return s[:10]
    return s[:10]


def coerce_value(raw, meta):
    """Turn a raw form value into the FM v3 wire value for its field type. Empty -> None."""
    # Item-reference / select values arrive from JS as {link,title,value} objects or arrays.
    if isinstance(raw, dict):
        link = raw.get('link') or raw.get('value')
        return _link_obj(link, raw.get('title') or raw.get('label')) if link else None
    if isinstance(raw, list):
        items = []
        for e in raw:
            if isinstance(e, dict):
                link = e.get('link') or e.get('value')
                if link:
                    items.append(_link_obj(link, e.get('title') or e.get('label')))
            elif isinstance(e, str) and e.strip():
                items.append(_link_obj(e.strip()))
        return items or None
    if isinstance(raw, bool):
        return raw
    # A bare link string (e.g. from a picklist) — treat as a single reference.
    if isinstance(raw, str) and raw.startswith('/api/v3/') and ',' not in raw:
        return _link_obj(raw)

    tt = _type_title(meta)
    if 'integer' in tt:
        if raw in (None, ''):
            return None
        try:
            return int(str(raw).strip())
        except (ValueError, TypeError):
            return None
    if any(k in tt for k in ('number', 'decimal', 'money', 'float', 'currency', 'numeric')):
        if raw in (None, ''):
            return None
        norm = re.sub(r'[^\d.\-]', '', str(raw).strip())
        if norm in ('', '-', '.', '-.'):
            return None
        try:
            return float(norm)
        except (ValueError, TypeError):
            return None
    if 'boolean' in tt or 'check' in tt:
        if raw in (None, ''):
            return None
        return str(raw).strip().lower() in ('true', '1', 'yes', 'checked', 'on')
    if 'date' in tt:
        return _coerce_date(raw)
    # Default: text / paragraph / rich text.
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw if raw != '' else None
    return raw


def _is_empty(v):
    return v is None or v == '' or v == [] or v == {}


# ---------------------------------------------------------------------------
# Inclusion rules.
# ---------------------------------------------------------------------------
def _is_required(meta):
    return any(v.get('validatorName') == 'required'
               for v in (meta.get('fieldValidators') or []))


def _is_writable(meta, mode):
    ed = (meta.get('editability') or 'ALWAYS').upper()
    if ed == 'NEVER':
        return False
    if mode == 'edit' and ed in ('ON_CREATION', 'CREATE_ONLY'):
        return False
    if meta.get('formulaField'):
        return False
    return True


def build_item_body(mode, field_values, fields_meta, section_struct,
                    workspace_id, view_id=1):
    """Build (sections_payload, missing_required).

    mode           -- 'create' | 'edit'
    field_values   -- {fieldId: rawValue}: the fields the form wants to write (on edit,
                      JS sends only the changed ones — diffing lives in the frontend).
    fields_meta    -- {fieldId: def} from workspace_fields (+ merged isSystemField on edit).
    section_struct -- [{id, classificationId, field_ids:[...]}] (sections_from_ws / _from_item).
    Returns (list, []) on success or (None, [labels]) if a writable required field is blank.
    """
    field_values = field_values or {}
    fields_meta = fields_meta or {}

    fid_to_section = {}
    for sec in section_struct or []:
        for fid in sec.get('field_ids') or []:
            fid_to_section.setdefault(fid, sec)

    def _label(fid):
        return (fields_meta.get(fid) or {}).get('title') or fid

    included = {}          # fid -> coerced value (non-derived, pass 1)
    derived_pending = {}   # fid -> meta (evaluated in pass 2)
    missing = []

    # Pass 1 — the fields the form is submitting.
    for fid, raw in field_values.items():
        meta = fields_meta.get(fid)
        if not meta or meta.get('isSystemField'):
            continue
        if not _is_writable(meta, mode):
            continue  # NEVER / formula / create-only-on-edit -> server owns it
        if meta.get('derived'):
            derived_pending[fid] = meta
            continue
        coerced = coerce_value(raw, meta)
        if _is_empty(coerced):
            if _is_required(meta):
                missing.append(_label(fid))
            elif mode == 'edit':
                included[fid] = None  # explicit clear of an editable field
            # create: silently omit an empty optional field
            continue
        included[fid] = coerced

    # Required pre-flight for CREATE — a required writable field the user never touched
    # (absent from field_values) is still a miss. (On edit an untouched required field is
    # assumed already populated on the item, so it is not flagged.)
    if mode == 'create':
        seen = set(field_values.keys())
        for fid, sec in fid_to_section.items():
            meta = fields_meta.get(fid)
            if not meta or meta.get('isSystemField') or meta.get('derived'):
                continue
            if _is_writable(meta, mode) and _is_required(meta) and fid not in included and fid not in seen:
                missing.append(_label(fid))

    if missing:
        # De-dupe while preserving order.
        seen_lbl, ordered = set(), []
        for lbl in missing:
            if lbl not in seen_lbl:
                seen_lbl.add(lbl)
                ordered.append(lbl)
        return None, ordered

    # Pass 2 — derived fields, included only when their source is also being written.
    for fid, meta in derived_pending.items():
        src = meta.get('derivedFieldSource')
        if src and src in included:
            coerced = coerce_value(field_values.get(fid), meta)
            if not _is_empty(coerced):
                included[fid] = coerced

    # Build the sections payload with minimal {__self__, value} fields. Section + field
    # links come VERBATIM from the source on edit (item-scoped — FM validates against them)
    # and are constructed workspace-/view-scoped on create.
    sections_out, by_id = [], {}
    fallback_sec = section_struct[0] if section_struct else None
    for fid, val in included.items():
        sec = fid_to_section.get(fid) or fallback_sec
        sid = sec['id'] if sec else '1'
        entry = by_id.get(sid)
        if not entry:
            sec_link = (sec or {}).get('link') or f'/api/v3/workspaces/{workspace_id}/sections/{sid}'
            entry = {'link': sec_link, 'fields': []}
            cid = (sec or {}).get('classificationId')
            if cid is not None:
                entry['classificationId'] = cid
            by_id[sid] = entry
            sections_out.append(entry)
        field_self = ((sec or {}).get('field_links') or {}).get(fid) \
            or f'/api/v3/workspaces/{workspace_id}/views/{view_id}/fields/{fid}'
        entry['fields'].append({'__self__': field_self, 'value': val})

    return sections_out, []
