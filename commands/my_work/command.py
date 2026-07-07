# My Work — personal outstanding-work queue spanning all entitled workspaces.
#
# Extends WorkspaceCommand (not PaletteCommand directly) so it inherits all
# workspace @actions needed for the full feature set: getItemDetail (with
# transitions), getTransitions, runWorkflowTransition, searchResultsForLineage,
# getRecordsEnrichment, etc.
#
# My Work-specific @actions beyond the inherited set:
#   getTasks / refreshTasks    — fetch /api/v3/users/@me/outstanding-work
#   openTaskInBrowser          — open fusionManageUrl in system browser
#   getLineageUrns             — read current Fusion file's lineage URNs (sync)
#   getWorkspaceFields         — workspace field defs with visibleOnPreview flag

import os
import webbrowser

import adsk.core

from ...core import config
from ...core import log
from ...core import paths
from ...core.action_registry import action
from ...core.workspace_command import WorkspaceCommand
from ...services import fusion_cad as _cad

# Fields retained from each _process_task result; heavy raw API fields are stripped.
_TASK_KEEP_FIELDS = frozenset([
    'itemId', 'workspaceId', 'itemDescriptor', 'itemTitle', 'workspaceTitle',
    'workflowStateName', 'workflowStateSetDate',
    'dueDate', 'daysToDue', 'fusionManageUrl',
    'flagged', 'createdDate', 'created', 'modifiedDate', 'modified',
])


def _slim_task(task):
    """Return only the fields the My Work frontend needs from a processed task."""
    slim = {}
    for k in _TASK_KEEP_FIELDS:
        if k in task:
            slim[k] = task[k]
    return slim


class MyWorkCommand(WorkspaceCommand):
    # --- Fusion command + palette identity ---
    command_id      = config.COMMAND_IDS['taskManagement']
    command_name    = 'My Work'
    command_tooltip = 'View your outstanding work items across all Fusion Manage workspaces'
    palette_id      = config.PALETTE_ID_MY_WORK
    palette_title   = 'My Work'
    palette_size    = (560, 700)
    docking         = 'left'
    panel           = 'plm'

    # My Work has no workspace-level entitlement gate — outstanding work is
    # user-centric and spans ALL entitled workspaces.
    workspace_system_name = None
    workspace_id_fallback = None
    requires_auth = True

    # HTML palette
    html_url = paths.to_file_url_path(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'resources', 'html', 'index.html',
        )
    )

    # ------------------------------------------------------------------
    # Task list actions
    # ------------------------------------------------------------------

    @action('getTasks', async_=True)
    def _act_get_tasks(self, _ctx, data):
        """Fetch the user's outstanding work items."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        result = self._client(ctx).outstanding_work()
        if not result.get('success', False):
            return {
                'success': False,
                'error': result.get('error') or 'Failed to fetch tasks.',
                'unauthorized': result.get('unauthorized', False),
            }
        raw_tasks = result.get('tasks') or []
        tasks = [_slim_task(t) for t in raw_tasks]
        return {'success': True, 'tasks': tasks, 'count': len(tasks)}

    @action('refreshTasks', async_=True)
    def _act_refresh_tasks(self, _ctx, data):
        """Semantic alias for getTasks — triggered by the Refresh button."""
        return self._act_get_tasks(_ctx, data)

    @action('openTaskInBrowser')
    def _act_open_task_in_browser(self, _ctx, data):
        """Open a task's Fusion Manage detail page in the system browser."""
        url = (data or {}).get('url', '')
        if url and isinstance(url, str) and url.startswith('http'):
            try:
                webbrowser.open(url)
                return {'success': True}
            except Exception as e:
                log.log(f'MyWork: openTaskInBrowser failed: {e}',
                        adsk.core.LogLevels.WarningLogLevel)
                return {'success': False, 'error': str(e) or 'Could not open browser.'}
        return {'success': False, 'error': 'Invalid or missing URL.'}

    # ------------------------------------------------------------------
    # CAD-context matching
    # ------------------------------------------------------------------

    @action('getLineageUrns')
    def _act_get_lineage_urns(self, _ctx, data):
        """Return APS lineage URNs for components in the currently active Fusion document.

        Sync (not async_=True) because it reads adsk.* Fusion API — must run on the
        main thread. Returns {success, urns: [...]} — empty list when no document is open.
        """
        try:
            result = _cad.get_root_components()
        except Exception:
            return {'success': True, 'urns': []}
        urns = []
        for comp in (result.get('components') or []):
            urn = _cad.file_id_to_lineage_urn(comp.get('fileId') or '')
            if urn:
                urns.append(urn)
        return {'success': True, 'urns': list(dict.fromkeys(urns))}  # de-dup, preserve order

    # ------------------------------------------------------------------
    # Workspace field definitions (for preview panel)
    # ------------------------------------------------------------------

    @action('getWorkspaceFields', async_=True)
    def _act_get_workspace_fields(self, _ctx, data):
        """Return workspace field definitions including the visibleOnPreview flag.

        Payload: {workspaceId}
        Response: {success, fields: [{id, name, title, displayOrder, visibleOnPreview, ...}]}
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = str((data or {}).get('workspaceId') or '').strip()
        if not ws:
            return {'success': False, 'error': 'Missing workspaceId.', 'fields': []}
        fields, fetch_err = self._client(ctx).workspace_fields(ws)
        if fetch_err:
            return {'success': False, 'error': fetch_err, 'fields': []}
        return {'success': True, 'fields': fields or []}
