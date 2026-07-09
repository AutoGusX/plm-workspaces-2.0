# Export Electronics BOM — a PaletteCommand on the Electronics environment panel that
# extracts the active schematic's bill of materials and prepares it for a Fusion Manage
# export.
#
# v1 scope: READ & DISPLAY only — prove we can extract the BOM off the Fusion Electronics
# API and normalize it into FM-export-ready rows. No FM writes yet (that's phase 2).
#
# The extraction action is SYNC because it calls the Fusion API (adsk.electron), which is
# main-thread only — never mark it async_=True.

import copy
import os

from ...core import auth
from ...core import config
from ...core import paths
from ...core.action_registry import action
from ...core.palette_base import PaletteCommand
from ...services import electronics_bom as _ebom
from . import export as _export

_CONFIG_SECTION = 'electronicsBom'


class ExportElectronicsBomCommand(PaletteCommand):
    command_id = config.COMMAND_IDS['exportElectronicsBom']
    command_name = 'Export Electronics BOM'
    command_tooltip = 'Extract the electronics BOM and prepare it for Fusion Manage'
    palette_id = config.PALETTE_ID_EXPORT_ELECTRONICS_BOM
    palette_title = 'Export Electronics BOM to PLM'
    palette_size = (720, 720)
    docking = 'right'
    panel = 'electronics'
    workspace_system_name = None
    # The wizard writes into Fusion Manage, so require sign-in (extraction step still works
    # once signed in). The login palette shows first if there's no valid token.
    requires_auth = True

    html_url = paths.to_file_url_path(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'resources', 'html', 'index.html'))

    @action('getElectronicsBom')   # SYNC — reads adsk.electron on the main thread
    def _act_get_bom(self, _ctx, data):
        """Extract + normalize the active electronics design's BOM. Returns
        {success, design, rows, partCount, skipped, warnings} or {success:false,error}."""
        try:
            result, err = _ebom.extract_bom(self._app)
        except Exception as e:
            return {'success': False, 'error': str(e) or 'BOM extraction failed.'}
        if err:
            return {'success': False, 'error': err}
        return {'success': True, **result}

    # ------------------------------------------------------------------
    # Configure step — workspace pickers + field mapping (persisted to app_prefs)
    # ------------------------------------------------------------------
    def _merged_config(self, override=None):
        """Saved prefs merged over the default preset, then any live override."""
        merged = copy.deepcopy(config.ELECTRONICS_BOM_DEFAULT_CONFIG)
        for src in (auth.load_app_prefs(_CONFIG_SECTION), override):
            for key, val in (src or {}).items():
                if isinstance(val, dict) and isinstance(merged.get(key), dict):
                    merged[key].update(val)
                else:
                    merged[key] = val
        return merged

    @action('getExportConfig')   # SYNC — reads local prefs only
    def _act_get_config(self, _ctx, data):
        """Return the saved export config merged over the default preset."""
        return {'success': True, 'config': self._merged_config()}

    @action('saveExportConfig')   # SYNC — writes local prefs only
    def _act_save_config(self, _ctx, data):
        """Persist {itemsWs, mpnWs, itemMapping, mpnMapping, parentKeyField, refDesField}."""
        cfg = (data or {}).get('config')
        if not isinstance(cfg, dict):
            return {'success': False, 'error': 'Missing config.'}
        allowed = {'itemsWs', 'mpnWs', 'itemMapping', 'mpnMapping',
                   'parentKeyField', 'refDesField'}
        clean = {k: v for k, v in cfg.items() if k in allowed}
        saved = auth.save_app_prefs(_CONFIG_SECTION, clean)
        return {'success': True, 'config': saved}

    @action('listWorkspaces', async_=True)
    def _act_list_workspaces(self, _ctx, data):
        """Return the tenant's workspaces [{systemName, id, title}] for the WS pickers."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        result = self._client(ctx).workspaces()
        if not result.get('success'):
            unauth = bool(result.get('unauthorized'))
            return {'success': False, 'error': result.get('error') or 'Could not list workspaces.',
                    'unauthorized': unauth}
        sn_to_id = result.get('system_name_to_id', {})
        sn_to_title = result.get('system_name_to_title', {})
        workspaces = [{'systemName': sn, 'id': wid, 'title': sn_to_title.get(sn, sn)}
                      for sn, wid in sn_to_id.items()]
        workspaces.sort(key=lambda w: (w['title'] or w['systemName']).lower())
        return {'success': True, 'workspaces': workspaces}

    @action('getMappingFields', async_=True)
    def _act_get_mapping_fields(self, _ctx, data):
        """Return a workspace's fields [{id,title,type,editability,picklist}] for the
        mapping UI. Payload: {systemName} or {workspaceId}."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        client = self._client(ctx)
        ws = data.get('workspaceId')
        sys_name = data.get('systemName')
        if not ws and sys_name:
            # Resolve via the HTTP workspaces map (async thread must not touch the Fusion API).
            wres = client.workspaces()
            if wres.get('success'):
                ws = wres.get('system_name_to_id', {}).get(sys_name)
        if not ws:
            return {'success': False, 'error': 'Workspace not resolved.'}
        fields, ferr = client.workspace_fields(ws)
        if ferr:
            unauth = ferr == 'unauthorized'
            return {'success': False, 'error': 'Not signed in.' if unauth else ferr,
                    'unauthorized': unauth}
        slim = [{'id': f.get('id'), 'title': f.get('title'),
                 'type': (f.get('type') or {}).get('title') if isinstance(f.get('type'), dict) else f.get('type'),
                 'editability': f.get('editability'),
                 'isReference': bool(f.get('picklist'))}
                for f in (fields or []) if f.get('id')]
        return {'success': True, 'workspaceId': ws, 'fields': slim}

    # ------------------------------------------------------------------
    # Preview + Push (the BOM never leaves the CAD without a preview first)
    # ------------------------------------------------------------------
    @action('resolveBomPlan', async_=True)
    def _act_resolve_plan(self, _ctx, data):
        """Resolve the export plan (match/diff, no writes). Payload: {bom, config?}."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        bom = (data or {}).get('bom') or {}
        if not bom.get('rows'):
            return {'success': False, 'error': 'No BOM rows to resolve — extract the BOM first.'}
        cfg = self._merged_config((data or {}).get('config'))
        return _export.resolve_plan(self._client(ctx), bom, cfg)

    @action('pushBom', async_=True)
    def _act_push_bom(self, _ctx, data):
        """Execute the export (writes). Payload: {bom, config?}."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        bom = (data or {}).get('bom') or {}
        if not bom.get('rows'):
            return {'success': False, 'error': 'No BOM rows to push — extract the BOM first.'}
        cfg = self._merged_config((data or {}).get('config'))
        return _export.push_plan(self._client(ctx), bom, cfg)

    @action('getBomRowFields', async_=True)
    def _act_get_bom_row_fields(self, _ctx, data):
        """Return the Items workspace's BOM-row (viewdef) fields for mapping (e.g. the
        Reference Designators field). Payload: {systemName?|workspaceId?}."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        client = self._client(ctx)
        ws = (data or {}).get('workspaceId')
        if not ws:
            sys_name = (data or {}).get('systemName') or self._merged_config().get('itemsWs')
            wres = client.workspaces()
            if wres.get('success'):
                ws = wres.get('system_name_to_id', {}).get(sys_name)
        if not ws:
            return {'success': False, 'error': 'Items workspace not resolved.'}
        result, ferr = client.bom_row_fields(ws)
        if ferr:
            unauth = ferr == 'unauthorized'
            return {'success': False, 'error': 'Not signed in.' if unauth else ferr,
                    'unauthorized': unauth}
        return {'success': True, 'fields': (result or {}).get('fields', []),
                'viewId': (result or {}).get('viewId')}

    # ------------------------------------------------------------------
    # Optional: sync EAGLE design files (.sch/.brd) to the parent PCBA item.
    # Export is SYNC (adsk.electron, main thread); upload is ASYNC (S3).
    # ------------------------------------------------------------------
    @action('exportDesignFiles')   # SYNC — adsk.electron export on the main thread
    def _act_export_files(self, _ctx, data):
        """Export the active design's .sch/.brd, zip them, return a one-file manifest."""
        import os
        import tempfile
        import zipfile
        out_dir = tempfile.mkdtemp(prefix='plm_ebom_')
        result, err = _ebom.export_design_files(self._app, out_dir)
        if err:
            return {'success': False, 'error': err}
        files = result.get('files') or []
        base = _ebom._safe_name(result.get('designName') or 'design')
        zip_path = os.path.join(out_dir, base + '_eagle.zip')
        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    z.write(f['path'], arcname=f['name'])
        except Exception as e:
            return {'success': False, 'error': 'Could not zip design files: %s' % e}
        return {'success': True,
                'files': [{'name': os.path.basename(zip_path), 'path': zip_path,
                           'size': os.path.getsize(zip_path),
                           'resourceName': base + '_eagle'}],
                'warnings': result.get('warnings', [])}

    @action('uploadDesignFiles', async_=True)
    def _act_upload_files(self, _ctx, data):
        """Attach exported design files to the parent PCBA item (version-bump on re-sync).
        Payload: {parentItemId, files:[{name,path,size,resourceName}], config?}."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        import os
        from ...services import attachments as _att
        files = (data or {}).get('files') or []
        parent_item = str((data or {}).get('parentItemId') or '')
        if not parent_item or not files:
            return {'success': False, 'error': 'Missing parentItemId or files.'}
        client = self._client(ctx)
        cfg = self._merged_config((data or {}).get('config'))
        wres = client.workspaces()
        ws = wres.get('system_name_to_id', {}).get(cfg.get('itemsWs')) if wres.get('success') else None
        if not ws:
            return {'success': False, 'error': 'Items workspace not resolved.'}
        existing, _e = _att.list_attachments(client, ws, parent_item)
        results = []
        for f in files:
            path = f.get('path'); name = f.get('name'); resource = f.get('resourceName') or name
            if not path or not os.path.isfile(path):
                results.append({'name': name, 'success': False, 'error': 'File missing.'})
                continue
            try:
                size = os.path.getsize(path)
                with open(path, 'rb') as fh:
                    file_bytes = fh.read()
            except OSError as e:
                results.append({'name': name, 'success': False, 'error': str(e)})
                continue
            existing_id = None
            for att in (existing or []):
                an = att.get('name', '')
                if isinstance(an, str) and resource and an.lower().startswith(resource.lower()):
                    existing_id = att.get('id')
                    break
            up, uerr = _att.request_upload(client, ws, parent_item, name, resource, size,
                                           existing_id, comment='Electronics design files')
            if uerr:
                results.append({'name': name, 'success': False, 'error': uerr})
                continue
            s3err = _att.upload_to_s3(client, up['s3_url'], up['extra_headers'], file_bytes)
            if s3err:
                results.append({'name': name, 'success': False, 'error': s3err})
                continue
            ver, cierr = _att.checkin(client, ws, parent_item, up['attachment_id'])
            results.append({'name': name, 'success': not cierr, 'version': ver,
                            'isNewVersion': existing_id is not None, 'error': cierr})
        return {'success': all(r['success'] for r in results), 'results': results}
