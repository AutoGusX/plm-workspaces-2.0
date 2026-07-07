# Entitlement filtering: only show command buttons for workspaces the user can access.
#
# Backed by GET /api/v3/workspaces?unlimited=true (via services.FmClient.workspaces()).
# Numeric workspace IDs vary by environment; systemNames (config.WORKSPACE_SYSTEM_NAMES)
# are consistent across environments and are resolved to the numeric ID at runtime.
#
# Ported from the old lib/entitlements.py. The workspace HTTP call now lives in the
# service layer (FmClient.workspaces); this module only caches + gates.

from . import auth
from . import config
from . import log

_entitled_workspace_ids = None          # set of str numeric IDs the user can access
_entitled_workspace_ids_ordered = None  # same IDs in API response order
_system_name_to_id = None               # systemName -> str numericId
_system_name_to_title = None            # systemName -> workspace title str


def clear_entitlement_cache():
    """Clear the cached workspace list so the next check refetches (e.g. after sign-in)."""
    global _entitled_workspace_ids, _entitled_workspace_ids_ordered, _system_name_to_id, _system_name_to_title
    _entitled_workspace_ids = None
    _entitled_workspace_ids_ordered = None
    _system_name_to_id = None
    _system_name_to_title = None


def _fetch_workspaces(app):
    """Call the workspaces endpoint via the service layer. Returns the FmClient
    workspaces() dict, or None if we can't build a RequestContext (not signed in)."""
    # Imported lazily to avoid a core <-> services import cycle.
    from .context import RequestContext
    from ..services.fm_client import FmClient
    ctx = RequestContext.resolve(app)
    if ctx is None:
        return None
    return FmClient(ctx).workspaces()


def get_entitled_workspace_ids(app):
    """Return the set of workspace IDs (str) the user can access, or None if not
    signed in / the API failed. Cached for the session. Also populates the
    systemName->id map as a side effect."""
    global _entitled_workspace_ids, _entitled_workspace_ids_ordered, _system_name_to_id, _system_name_to_title
    if _entitled_workspace_ids is not None:
        return _entitled_workspace_ids

    result = _fetch_workspaces(app)
    if result is None or not result.get('success'):
        if getattr(config, 'DEBUG', False):
            detail = (result or {}).get('error') if result else 'no context (not signed in)'
            _log_debug(f'workspaces fetch failed: {detail}')
        return None

    _entitled_workspace_ids = result.get('workspace_ids') or set()
    _entitled_workspace_ids_ordered = result.get('workspace_ids_ordered') or []
    _system_name_to_id = result.get('system_name_to_id') or {}
    _system_name_to_title = result.get('system_name_to_title') or {}
    if getattr(config, 'DEBUG', False):
        _log_debug(f'entitled count={len(_entitled_workspace_ids)} '
                   f'systemNames={sorted(_system_name_to_id.keys())}')
    return _entitled_workspace_ids


def get_system_name_to_id(app):
    """Return {systemName -> str numericId} for accessible workspaces, or None."""
    get_entitled_workspace_ids(app)  # ensure cache populated
    return _system_name_to_id


def get_system_name_to_title(app):
    """Return {systemName -> workspace title str} for accessible workspaces, or {}.

    Populated as a side effect of get_entitled_workspace_ids(); returns {} (not None)
    when the cache hasn't been filled yet (not signed in / API failure)."""
    get_entitled_workspace_ids(app)  # ensure cache populated
    return _system_name_to_title or {}


def get_workspace_id(system_name, app, fallback_id=None):
    """Resolve a workspace systemName (e.g. 'WS_CHANGE_ORDERS') to its numeric ID string.
    Falls back to fallback_id when the systemName is empty or not found."""
    if system_name:
        sn_map = get_system_name_to_id(app)
        if sn_map and system_name in sn_map:
            return sn_map[system_name]
    return fallback_id or None


def get_entitled_workspace_ids_ordered(app):
    """Return (ordered_list, set). ordered_list is in API response order.
    ([], None) if not signed in / API failed."""
    get_entitled_workspace_ids(app)
    ordered = _entitled_workspace_ids_ordered if _entitled_workspace_ids_ordered is not None else []
    return ordered, _entitled_workspace_ids


def is_command_entitled(command_key, app):
    """Return True if the panel button for command_key should be shown.

    - taskManagement (and any capability with no workspace gate): always shown.
    - No entitlement list (not signed in / API failed): fail OPEN (show all).
    - Otherwise: resolve the workspace via systemName (falling back to the legacy
      numeric ID) and show only if that ID is in the entitled set. Unresolvable
      IDs fail open rather than hiding the command.
    """
    ws_system_names = getattr(config, 'WORKSPACE_SYSTEM_NAMES', {})
    sys_name = ws_system_names.get(command_key)
    fallback_id = str(getattr(config, 'WORKSPACE_IDS', {}).get(command_key, '')) or None

    if not sys_name and not fallback_id:
        return True  # always-on (e.g. My Work)

    entitled = get_entitled_workspace_ids(app)
    if entitled is None:
        return True  # fail open when the API isn't available

    resolved_id = get_workspace_id(sys_name, app, fallback_id)
    if not resolved_id:
        return True  # can't resolve — fail open rather than hide
    return resolved_id in entitled


def _log_debug(detail):
    try:
        import adsk.core
        log.log(f'Entitlements: {detail}', adsk.core.LogLevels.InfoLogLevel)
    except Exception:
        pass
