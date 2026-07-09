# write_item — gather field metadata, build the FM v3 body, and create/update an item.
#
# Factored out of core.workspace_command._act_create_item/_act_update_item so it can be
# reused wherever an item must be written (the createItem/updateItem actions AND the
# electronics-BOM push, which creates component/MPN/parent items outside a WorkspaceCommand).
#
# Pure services layer: no adsk, no Fusion API — safe to call from async handler threads.
# Returns a uniform dict:
#   create success -> {'success': True, 'itemId': <id>, 'data': <raw>}
#   edit   success -> {'success': True, 'itemId': <id>}
#   failure        -> {'success': False, 'error': <msg>, 'unauthorized'?: bool, 'missingFields'?: [..]}

from . import item_payload as _payload


def _err(e):
    unauth = e == 'unauthorized'
    return {'success': False, 'error': 'Not signed in.' if unauth else e, 'unauthorized': unauth}


def write_item(client, workspace_id, mode, field_values, item_id=None, etag=''):
    """mode = 'create' | 'edit'. field_values = {fieldId: rawValue}."""
    if not isinstance(field_values, dict):
        return {'success': False, 'error': 'Missing fieldValues.'}
    fields, ferr = client.workspace_fields(workspace_id)
    if ferr:
        return _err(ferr)
    fields_meta = {f['id']: f for f in (fields or []) if f.get('id')}

    if mode == 'create':
        sec_result, serr = client.sections(workspace_id)
        if serr:
            return _err(serr)
        view_id = (sec_result or {}).get('viewId', 1)
        # /sections has no field->section membership; borrow it (and isSystemField) from a
        # reference item, falling back to the bare section list.
        section_struct = _payload.sections_from_ws(sec_result or {}, workspace_id)
        ref_list, _rerr = client.items_list(workspace_id, 0, 1)
        ref_items = (ref_list or {}).get('items') if isinstance(ref_list, dict) else None
        if ref_items:
            ref_item, _tag, _rerr2 = client.item_detail(workspace_id, ref_items[0].get('itemId'))
            if ref_item:
                section_struct = _payload.sections_from_item(ref_item, workspace_id, for_create=True)
                for fid, is_sys in _payload.system_fields_from_item(ref_item).items():
                    if fid in fields_meta:
                        fields_meta[fid]['isSystemField'] = is_sys
        sections, missing = _payload.build_item_body('create', field_values, fields_meta,
                                                     section_struct, workspace_id, view_id)
        if missing:
            return {'success': False, 'missingFields': missing,
                    'error': 'Required: ' + ', '.join(missing)}
        if not sections:
            return {'success': False, 'error': 'Nothing to create — no writable fields provided.'}
        result, fetch_err = client.create_item(workspace_id, sections)
        if fetch_err:
            return _err(fetch_err)
        return {'success': True, 'itemId': (result or {}).get('itemId'),
                'data': (result or {}).get('data')}

    # edit
    if not item_id:
        return {'success': False, 'error': 'Missing itemId.'}
    item, _item_etag, ierr = client.item_detail(workspace_id, item_id)
    if ierr:
        return _err(ierr)
    for fid, is_sys in _payload.system_fields_from_item(item or {}).items():
        if fid in fields_meta:
            fields_meta[fid]['isSystemField'] = is_sys
    section_struct = _payload.sections_from_item(item or {})
    sections, missing = _payload.build_item_body('edit', field_values, fields_meta,
                                                 section_struct, workspace_id, 1)
    if missing:
        return {'success': False, 'missingFields': missing,
                'error': 'Required: ' + ', '.join(missing)}
    if not sections:
        return {'success': False, 'error': 'Nothing to save — no editable changes.'}
    fetch_err = client.update_item(workspace_id, item_id, sections, etag)
    if fetch_err:
        return _err(fetch_err)
    return {'success': True, 'itemId': item_id}
