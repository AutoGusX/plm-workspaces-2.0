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
