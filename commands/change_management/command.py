# Change Management — unified multi-workspace WorkspaceCommand subclass.
#
# Default view: a single "All change records" table aggregating items from EVERY
# discovered change workspace (WS_CHANGE_ORDERS, WS_LEAN_CHANGE_ORDERS,
# WS_CHANGE_REQUESTS, WS_CHANGE_TASKS, WS_PROBLEM_REPORTS — plus tenant duplicates
# matching BASE_<digits>).
#
# Scope selector: switching to a specific workspace enables the single-workspace
# tableau/field/detail/edit flow, passing workspaceId in every payload so the
# inherited WorkspaceCommand @actions target the selected workspace.
#
# All standard workspace @actions (getTableaus, getTableauData, getViewFields,
# getWorkspaceSections, getItemDetail, createItem, updateItem, workflow, affected
# items, tabs, permissions) are inherited from WorkspaceCommand unchanged — they
# now accept an optional workspaceId in the payload (see _ws_from() in
# core/workspace_command.py).

import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import adsk.core

from ...core import config
from ...core import entitlements
from ...core import log
from ...core import paths
from ...core.action_registry import action
from ...core.workspace_command import WorkspaceCommand, _api_err

# ---------------------------------------------------------------------------
# Per-workspace cap for the unified fetch (bounds memory + API time).
# If any workspace returned fewer items than its total, truncated=True is set
# and a warning is logged (no silent truncation per repo principle).
# ---------------------------------------------------------------------------
_UNIFIED_PER_WS_CAP = 200

# Discovery bases — order determines sort order in the scope list.
_CHANGE_SYSTEM_NAME_BASES = [
    'WS_CHANGE_ORDERS',
    'WS_LEAN_CHANGE_ORDERS',
    'WS_CHANGE_REQUESTS',
    'WS_CHANGE_TASKS',
    'WS_PROBLEM_REPORTS',
]


class ChangeManagementCommand(WorkspaceCommand):
    # --- Fusion command + palette identity ---
    command_id    = config.COMMAND_IDS['changeManagement']
    command_name  = 'Change Management'
    command_tooltip = 'View and manage change records in Fusion Manage'
    palette_id    = config.PALETTE_ID_CHANGE_MANAGEMENT
    palette_title = 'Change Management'
    palette_size  = (700, 600)
    docking       = 'left'
    panel         = 'plm'

    # Primary workspace for entitlement gating (the classic Change Orders WS).
    # Drill-down into other change workspaces uses the workspaceId in each payload.
    workspace_system_name = config.WORKSPACE_SYSTEM_NAMES['changeManagement']  # 'WS_CHANGE_ORDERS'
    workspace_id_fallback = config.WORKSPACE_IDS['changeManagement']            # 9

    # --- Feature flags ---
    showAffectedItems = True

    # --- HTML palette ---
    html_url = paths.to_file_url_path(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'resources', 'html', 'index.html',
        )
    )

    # ------------------------------------------------------------------
    # Change workspace discovery
    # ------------------------------------------------------------------
    CHANGE_SYSTEM_NAME_BASES = _CHANGE_SYSTEM_NAME_BASES

    def _discover_change_workspaces(self):
        """Return an ordered list of change workspace dicts accessible to the user.

        Each entry: {systemName: str, workspaceId: str, title: str}

        Discovery rule: a workspace qualifies if its systemName equals one of the
        CHANGE_SYSTEM_NAME_BASES or matches BASE_<digits> (tenant duplicates).
        Sorted by base-order (BASES list above), then by systemName within a base.
        """
        sn_to_id = entitlements.get_system_name_to_id(self._app) or {}
        sn_to_title = entitlements.get_system_name_to_title(self._app) or {}
        entitled_ids = entitlements.get_entitled_workspace_ids(self._app) or set()

        found = []
        for system_name, ws_id in sn_to_id.items():
            if ws_id not in entitled_ids:
                continue
            base = _match_change_base(system_name)
            if base is None:
                continue
            base_order = _CHANGE_SYSTEM_NAME_BASES.index(base)
            title = sn_to_title.get(system_name) or _humanize_system_name(system_name)
            found.append({
                'systemName': system_name,
                'workspaceId': str(ws_id),
                'title': title,
                '_sort_key': (base_order, system_name),
            })

        found.sort(key=lambda x: x['_sort_key'])
        # Remove the internal sort key before returning.
        return [{'systemName': e['systemName'], 'workspaceId': e['workspaceId'],
                 'title': e['title']} for e in found]

    # ------------------------------------------------------------------
    # New @actions for unified / scoped Change Management
    # ------------------------------------------------------------------

    @action('getChangeScopes', async_=True)
    def _act_get_change_scopes(self, _ctx, data):
        """Return the scope selector options: 'all' plus each discovered change workspace.

        Response:
          {success, scopes: [{key, label, workspaceId?, systemName?}]}
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err
        workspaces = self._discover_change_workspaces()
        scopes = [{'key': 'all', 'label': 'All change records'}]
        for ws in workspaces:
            scopes.append({
                'key': ws['workspaceId'],
                'label': ws['title'],
                'workspaceId': ws['workspaceId'],
                'systemName': ws['systemName'],
            })
        return {'success': True, 'scopes': scopes}

    @action('getUnifiedChangeRecords', async_=True)
    def _act_get_unified_change_records(self, _ctx, data):
        """Fetch items from every discovered change workspace in parallel and merge.

        Payload (all optional):
          {offset?: int, limit?: int, sort?: 'descriptor'|...}

        Response:
          {success, rows: [...], totalCount: int,
           columns: [{id, label}], truncated: bool}

        Each row: {type, recordNumber, title, descriptor, owner, itemId, workspaceId}

        Per-workspace cap: _UNIFIED_PER_WS_CAP.  If any workspace had more records
        than the cap, truncated=True is returned and a warning is logged.
        Workflow state is NOT included (not available in the bulk /items endpoint —
        it lives in the per-item detail / drill-down view, as expected).
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err

        workspaces = self._discover_change_workspaces()
        if not workspaces:
            # No change workspaces entitled — return empty result, not an error.
            return {
                'success': True,
                'rows': [],
                'totalCount': 0,
                'columns': _unified_columns(),
                'truncated': False,
            }

        client = self._client(ctx)
        truncated = False

        def _fetch_ws(ws_entry):
            ws_id = ws_entry['workspaceId']
            result, fetch_err = client.items_list(ws_id, offset=0, limit=_UNIFIED_PER_WS_CAP)
            if fetch_err:
                log.log(
                    f'ChangeManagement.getUnifiedChangeRecords: workspace {ws_id} '
                    f'({ws_entry["systemName"]}) fetch error: {fetch_err}',
                    adsk.core.LogLevels.WarningLogLevel,
                )
                return [], False
            items = result.get('items') or []
            total = result.get('totalCount', len(items))
            was_capped = total > len(items)
            if was_capped:
                log.log(
                    f'ChangeManagement.getUnifiedChangeRecords: workspace {ws_id} '
                    f'({ws_entry["systemName"]}) has {total} records but only '
                    f'{len(items)} were fetched (cap={_UNIFIED_PER_WS_CAP}). '
                    f'Set truncated=True.',
                    adsk.core.LogLevels.WarningLogLevel,
                )
            return items, was_capped

        all_rows = []
        executor = ThreadPoolExecutor(max_workers=min(len(workspaces), 8))
        try:
            futures = {executor.submit(_fetch_ws, ws): ws for ws in workspaces}
            for fut in list(futures.keys()):
                try:
                    items, was_capped = fut.result(timeout=config.EXECUTOR_TIMEOUT_SECONDS)
                    if was_capped:
                        truncated = True
                    ws_entry = futures[fut]
                    for item in items:
                        # Type column = the workspace's real title. NOTE: the /items
                        # endpoint's `workspaceShortName` is a junk app-store blurb on
                        # some workspaces ("This workspace is part of the Change
                        # Management app…"), so use the discovered title, then
                        # workspaceLongName, never workspaceShortName.
                        ws_type = (ws_entry.get('title') or '').strip() \
                            or (item.get('workspaceLongName') or '').strip() \
                            or ws_entry['workspaceId']
                        all_rows.append({
                            'type': ws_type,
                            'recordNumber': item.get('number') or '',
                            'title': item.get('title') or '',
                            'descriptor': item.get('descriptor') or '',
                            'owner': item.get('owner') or '',
                            'itemId': item.get('itemId') or '',
                            'workspaceId': item.get('workspaceId') or ws_entry['workspaceId'],
                        })
                except FuturesTimeoutError:
                    ws_entry = futures[fut]
                    log.log(
                        f'ChangeManagement.getUnifiedChangeRecords: workspace '
                        f'{ws_entry["workspaceId"]} ({ws_entry["systemName"]}) '
                        f'timed out after {config.EXECUTOR_TIMEOUT_SECONDS}s.',
                        adsk.core.LogLevels.WarningLogLevel,
                    )
                    truncated = True
                except Exception as e:
                    ws_entry = futures[fut]
                    log.log(
                        f'ChangeManagement.getUnifiedChangeRecords: workspace '
                        f'{ws_entry["workspaceId"]} error: {e}',
                        adsk.core.LogLevels.WarningLogLevel,
                    )
        except Exception as e:
            log.handle_error('ChangeManagement.getUnifiedChangeRecords')
            return {'success': False, 'error': str(e) or 'Unified fetch failed.'}
        finally:
            # Shut down without waiting — any hung futures are abandoned so a
            # slow/stuck workspace cannot stall the entire call beyond the
            # per-future timeout already applied above.
            executor.shutdown(wait=False, cancel_futures=True)

        # Sort merged rows by descriptor ascending (default).
        sort_key = str(data.get('sort') or 'descriptor')
        if sort_key in ('descriptor', 'recordNumber', 'type', 'owner'):
            all_rows.sort(key=lambda r: (r.get(sort_key) or '').lower())
        else:
            all_rows.sort(key=lambda r: (r.get('descriptor') or '').lower())

        return {
            'success': True,
            'rows': all_rows,
            'totalCount': len(all_rows),
            'columns': _unified_columns(),
            'truncated': truncated,
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _match_change_base(system_name):
    """Return the matching CHANGE_SYSTEM_NAME_BASE for a systemName, or None.

    Matches exact base names and BASE_<digits> variants (e.g. WS_LEAN_CHANGE_ORDERS_1).
    """
    for base in _CHANGE_SYSTEM_NAME_BASES:
        if system_name == base:
            return base
        if re.match(r'^' + re.escape(base) + r'_\d+$', system_name):
            return base
    return None


def _humanize_system_name(system_name):
    """Convert a systemName like WS_LEAN_CHANGE_ORDERS to 'Lean Change Orders'."""
    s = system_name
    # Strip common prefixes
    for prefix in ('WS_', 'CW_'):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # Strip trailing _<digits> variant suffix
    s = re.sub(r'_\d+$', '', s)
    # Convert underscores to spaces, title-case
    return s.replace('_', ' ').title()


def _unified_columns():
    """Column definitions for the unified change records table."""
    return [
        {'id': 'type',       'label': 'Type'},
        {'id': 'descriptor', 'label': 'Record'},
        {'id': 'owner',      'label': 'Owner'},
    ]
