# Export Electronics BOM — a PaletteCommand on the Electronics environment panel that
# extracts the active schematic's bill of materials and prepares it for a Fusion Manage
# export.
#
# v1 scope: READ & DISPLAY only — prove we can extract the BOM off the Fusion Electronics
# API and normalize it into FM-export-ready rows. No FM writes yet (that's phase 2).
#
# The extraction action is SYNC because it calls the Fusion API (adsk.electron), which is
# main-thread only — never mark it async_=True.

import os

from ...core import config
from ...core import paths
from ...core.action_registry import action
from ...core.palette_base import PaletteCommand
from ...services import electronics_bom as _ebom


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
    # v1 is local extraction/prep only, so don't force sign-in to view the BOM.
    requires_auth = False

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
