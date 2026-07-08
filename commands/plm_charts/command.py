# PLM Charts — a custom PaletteCommand (modeled on My Work) that renders the tenant's
# Fusion Manage report dashboard as charts inside Fusion.
#
# Backend is two async HTTP actions over the legacy REST v1 reporting API
# (services/reports.py via FmClient). All rendering happens client-side in
# resources/html/static/plm_charts.js from the normalized chart shape.

import os

from ...core import config
from ...core import paths
from ...core.action_registry import action
from ...core.palette_base import PaletteCommand


class PlmChartsCommand(PaletteCommand):
    command_id = config.COMMAND_IDS['plmCharts']
    command_name = 'PLM Charts'
    command_tooltip = 'View your Fusion Manage dashboard charts inside Fusion'
    palette_id = config.PALETTE_ID_PLM_CHARTS
    palette_title = 'PLM Charts'
    palette_size = (760, 760)
    docking = 'left'
    panel = 'plm'
    workspace_system_name = None   # dashboard is tenant-wide — always available
    requires_auth = True

    html_url = paths.to_file_url_path(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'resources', 'html', 'index.html'))

    @action('getDashboards', async_=True)
    def _act_get_dashboards(self, _ctx, data):
        """Return the dashboard report list [{id, position, link}] (ordered)."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        reports, fetch_err = self._client(ctx).report_dashboard()
        if fetch_err:
            unauth = fetch_err == 'unauthorized'
            return {'success': False,
                    'error': 'Not signed in.' if unauth else fetch_err,
                    'unauthorized': unauth}
        return {'success': True, 'reports': reports or []}

    @action('getChart', async_=True)
    def _act_get_chart(self, _ctx, data):
        """Return one normalized chart (type + series) for the given report id."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        report_id = (data or {}).get('id')
        if report_id in (None, ''):
            return {'success': False, 'error': 'Missing report id.'}
        chart, fetch_err = self._client(ctx).report_chart(report_id)
        if fetch_err:
            unauth = fetch_err == 'unauthorized'
            return {'success': False,
                    'error': 'Not signed in.' if unauth else fetch_err,
                    'unauthorized': unauth}
        return {'success': True, 'chart': chart}
