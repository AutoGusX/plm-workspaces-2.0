# PaletteCommand — the base class that owns the entire palette lifecycle so each
# capability command declares config + @action handlers only and CANNOT drift on
# auth gating, dispatch, or lifecycle.
#
# A subclass declares class attributes (command_id, palette_id, html_url, ...) and
# a handful of @action methods. The base provides:
#   - start()/stop(): command definition, panel button, OAuth custom-event hook (idempotent)
#   - command-created/execute/destroy handlers
#   - palette creation + UNIFORM auth gating (requires_auth + not signed in -> login)
#   - the incomingFromHTML handler that parses data, dispatches via the action
#     registry, and returns JSON
#   - shared @actions implemented once: getAuthStatus, getWorkspaceAccess, getTheme,
#     getUiPrefs, setUiPrefs, openInBrowser, openInFusion, getItemDetail
#
# Response contract (see docs/JS_PYTHON_CONTRACT.md):
#   success: {'success': True, ...}
#   failure: {'success': False, 'error': '...', 'unauthorized': True?}

import json
import re
import webbrowser

import adsk.core

from . import async_dispatcher
from . import entitlements
from . import events
from . import log
from . import auth
from .action_registry import action, build_action_map, _ASYNC_ATTR
from .context import RequestContext

# Fusion color-theme enum -> CSS theme name used by the frontend.
_THEME_NAMES = {0: 'classic', 1: 'light-gray', 2: 'dark-blue', 3: 'dark-gray', 4: 'device'}

# Internal auth-error classification codes -> short human-readable messages. The raw
# code is still returned under 'lastErrorCode' for diagnostics.
_AUTH_ERROR_MESSAGES = {
    'refresh_failed': 'Your session expired. Please sign in again.',
    'refresh_transient': 'Could not reach the sign-in service. Check your connection and try again.',
    'not_signed_in': 'Not signed in.',
}


def _humanize_auth_error(code):
    """Map an internal auth-error code to a user-facing message (don't leak raw codes)."""
    if not code:
        return ''
    return _AUTH_ERROR_MESSAGES.get(code, 'Sign-in error. Please sign in again.')


# The command whose auth gate most recently redirected the user to the login palette.
# When the OAuth token arrives, that command opens its own palette automatically so the
# user lands on the feature they originally clicked (instead of having to click again).
_pending_command = None


class PaletteCommand:
    # --- subclass declares these ---
    command_id = None
    command_name = None
    command_tooltip = ''
    palette_id = None
    palette_title = None
    html_url = None                  # absolute file path to the palette's index.html
    palette_size = (700, 600)        # (width, height)
    resizable = True
    docking = 'left'                 # 'left' | 'right'
    panel = 'plm'                    # 'plm' | 'cam' | None (no panel button)
    workspace_system_name = None     # systemName for entitlement gating (None = always)
    requires_auth = True             # base enforces login-palette behavior uniformly
    show_panel_button = True         # set False for the login command

    # --- base-owned state (per instance) ---
    def __init__(self):
        self._app = adsk.core.Application.get()
        self._ui = self._app.userInterface
        # Per-invocation handlers (execute/destroy) — cleared every time the transient
        # button command is destroyed after it runs.
        self._local_handlers = []
        # Add-in-lifetime handlers (commandCreated + the OAuth token event). These MUST
        # NOT live on _local_handlers: a Fusion button command is destroyed right after
        # it executes, and _on_command_destroy clears _local_handlers — which would
        # release the commandCreated handler and make the button dead after one click.
        self._persistent_handlers = []
        self._action_map = build_action_map(type(self))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, oauth_event=None):
        """Create the command definition + panel button, subscribe to the OAuth event.
        Idempotent: safe to call repeatedly (re-uses existing definitions).

        oauth_event -- the shared OAuth-token CustomEvent registered ONCE at add-in
        scope (commands/__init__.py). Fusion custom events are process-global per id
        and not ref-counted, so each command must subscribe its handler to this same
        object rather than re-registering the id itself.
        """
        # Token persistence target: the live add-in root.
        from . import paths
        auth.set_addin_root(paths.get_addin_root())

        cmd_def = self._ui.commandDefinitions.itemById(self.command_id)
        if cmd_def is None:
            icon_folder = self._icon_folder()
            cmd_def = self._ui.commandDefinitions.addButtonDefinition(
                self.command_id, self.command_name, self.command_tooltip or '', icon_folder)
        # commandCreated must persist for the add-in's lifetime (see __init__).
        events.add_handler(cmd_def.commandCreated, self._on_command_created,
                           local_handlers=self._persistent_handlers)

        if self.show_panel_button and self.panel:
            self._add_panel_button(cmd_def)

        # Subscribe to the shared OAuth token custom event so the palette can react to
        # sign-in. The callback server fires it via app.fireCustomEvent from its
        # background thread, marshalling the token JSON back onto the Fusion UI thread.
        # Persistent: must survive command destroy (otherwise sign-in notifications die
        # after the command's first run).
        if oauth_event is not None:
            try:
                events.add_handler(oauth_event, self._on_oauth_token_received,
                                   local_handlers=self._persistent_handlers)
            except Exception as e:
                log.log(f'{self.command_name}: could not subscribe to OAuth custom event: {e}',
                        adsk.core.LogLevels.WarningLogLevel)
        log.log(f'{self.command_name}: start() complete '
                f'(panel_button={self.show_panel_button and bool(self.panel)})')

    def stop(self):
        """Remove the palette, panel button, and command definition."""
        # Palette
        palette = self._ui.palettes.itemById(self.palette_id)
        if palette:
            try:
                palette.deleteMe()
            except Exception:
                pass
        # Panel button
        if self.show_panel_button and self.panel:
            self._remove_panel_button()
        # Command definition
        cmd_def = self._ui.commandDefinitions.itemById(self.command_id)
        if cmd_def:
            try:
                cmd_def.deleteMe()
            except Exception:
                pass
        # NOTE: the shared OAuth custom event is registered/unregistered exactly once
        # at add-in scope (commands/__init__.py); a command must NOT unregister it here
        # or it would tear the event down for every other command (Fusion custom events
        # are process-global per id and not ref-counted).
        self._local_handlers = []
        self._persistent_handlers = []

    # ------------------------------------------------------------------
    # Panel wiring (delegates to core.panels)
    # ------------------------------------------------------------------
    def _icon_folder(self):
        """Return the resources folder for this command's button icons, or '' if it
        contains no icon PNGs (e.g. the login command's html-only resources)."""
        import os
        module = type(self).__module__
        try:
            mod = __import__(module, fromlist=['__file__'])
            base = os.path.dirname(os.path.abspath(mod.__file__))
            res = os.path.join(base, 'resources')
            if not os.path.isdir(res):
                return ''
            for name in os.listdir(res):
                if name.lower().endswith('.png'):
                    return res
            return ''
        except Exception:
            return ''

    def _add_panel_button(self, cmd_def):
        from . import panels
        if self.panel == 'cam':
            panels.add_command_to_cam_manage_panel(self._ui, cmd_def)
        elif self.panel == 'electronics':
            panels.add_command_to_electronics_panel(self._ui, cmd_def)
        else:
            panels.add_command_to_plm_panels(self._ui, cmd_def)

    def _remove_panel_button(self):
        from . import panels
        if self.panel == 'cam':
            panels.remove_command_from_cam_manage_panel(self._ui, self.command_id)
        elif self.panel == 'electronics':
            panels.remove_command_from_electronics_panel(self._ui, self.command_id)
        else:
            panels.remove_command_from_plm_panels(self._ui, self.command_id)

    # ------------------------------------------------------------------
    # Command event handlers
    # ------------------------------------------------------------------
    def _on_command_created(self, args: adsk.core.CommandCreatedEventArgs):
        cmd = args.command
        events.add_handler(cmd.execute, self._on_command_execute,
                           local_handlers=self._local_handlers)
        events.add_handler(cmd.destroy, self._on_command_destroy,
                           local_handlers=self._local_handlers)

    def _on_command_execute(self, args: adsk.core.CommandEventArgs):
        # UNIFORM auth gate: if this command needs auth and the user isn't signed in,
        # show the login palette instead of this command's palette.
        global _pending_command
        has_token = auth.has_valid_token()
        if self.requires_auth and not has_token:
            # Remember which command sent the user to login so we can re-open it
            # automatically once the token arrives.
            _pending_command = self
            self._show_login_palette()
            return
        self._show_palette()

    def _on_command_destroy(self, args: adsk.core.CommandEventArgs):
        self._local_handlers = []

    # ------------------------------------------------------------------
    # Palette creation + show
    # ------------------------------------------------------------------
    def _create_palette(self):
        palettes = self._ui.palettes
        palette = palettes.itemById(self.palette_id)
        if palette is None:
            width, height = self.palette_size
            palette = palettes.add(
                id=self.palette_id,
                name=self.palette_title,
                htmlFileURL=self.html_url,
                isVisible=False,
                showCloseButton=True,
                isResizable=self.resizable,
                width=width,
                height=height,
                useNewWebBrowser=True,
            )
            # PERSISTENT: the palette outlives the transient button command. If this
            # handler were on _local_handlers it would be released by
            # _on_command_destroy right after the command runs, leaving the palette
            # visible but deaf — every plmSend would reach Python with no handler and
            # return empty (observed as hasToken=undefined / blank palette).
            events.add_handler(palette.incomingFromHTML, self._on_incoming,
                               local_handlers=self._persistent_handlers)
            log.log(f'{self.command_name}: created palette {self.palette_id}')
        return palette

    def _show_palette(self):
        palette = self._create_palette()
        palette.isVisible = True
        try:
            dock = (adsk.core.PaletteDockingStates.PaletteDockStateRight
                    if self.docking == 'right'
                    else adsk.core.PaletteDockingStates.PaletteDockStateLeft)
            if palette.dockingState == adsk.core.PaletteDockingStates.PaletteDockStateFloating:
                palette.dockingState = dock
        except Exception:
            pass
        return palette

    def _show_login_palette(self):
        """Show the shared login palette (imported lazily to avoid an import cycle)."""
        try:
            from ..commands.login.command import LoginCommand
            LoginCommand.show()
        except Exception as e:
            log.log(f'{self.command_name}: could not show login palette: {e}',
                    adsk.core.LogLevels.ErrorLogLevel)

    # ------------------------------------------------------------------
    # OAuth custom-event hook (subclasses may override _on_token to refresh UI)
    # ------------------------------------------------------------------
    def _on_oauth_token_received(self, args: adsk.core.CustomEventArgs):
        global _pending_command
        try:
            data = getattr(args, 'additionalInfo', None)
            if not data:
                return
            # Entitlements may have changed at sign-in; drop the cache.
            entitlements.clear_entitlement_cache()
            # If this command sent the user to login, open its palette now so they land
            # on the feature they originally clicked.
            if _pending_command is self:
                _pending_command = None
                log.log(f'{self.command_name}: token received -> auto-opening palette')
                try:
                    self._show_palette()
                except Exception as e:
                    log.log(f'{self.command_name}: auto-open failed: {e}',
                            adsk.core.LogLevels.ErrorLogLevel)
            self._on_token(data)
        except Exception as e:
            log.log(f'{self.command_name}: oauth_token_received error: {e}',
                    adsk.core.LogLevels.ErrorLogLevel)

    def _on_token(self, token_json):
        """Hook: notify an open palette of a fresh token. Default pushes 'tokenResult'."""
        palette = self._ui.palettes.itemById(self.palette_id)
        if palette and palette.isVisible:
            try:
                palette.sendInfoToHTML('tokenResult', token_json)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Incoming-from-HTML dispatch
    # ------------------------------------------------------------------
    def _on_incoming(self, html_args: adsk.core.HTMLEventArgs):
        action_name = html_args.action
        try:
            data = json.loads(html_args.data) if html_args.data else {}
        except (TypeError, ValueError, AttributeError):
            data = {}
        if not isinstance(data, dict):
            data = {}

        handler = self._action_map.get(action_name)
        if handler is None:
            html_args.returnData = json.dumps(
                {'success': False, 'error': f'Unknown action: {action_name}'})
            return

        # --- Async path: handler marked async_=True ---
        # Pre-resolve ctx here (main thread — safe to call Fusion API).
        # Return a {pending, requestId} ack immediately so the main thread is
        # never blocked by HTTP.  The result is pushed via plmAsyncResult push.
        if getattr(handler, _ASYNC_ATTR, False):
            pre_ctx = RequestContext.resolve(self._app)
            if pre_ctx is None:
                # Not signed in — return error synchronously; no point queuing.
                html_args.returnData = json.dumps(
                    {'success': False, 'error': 'Not signed in.', 'unauthorized': True})
                return
            request_id = async_dispatcher.new_request_id()
            html_args.returnData = json.dumps({'pending': True, 'requestId': request_id})
            async_dispatcher.submit(
                request_id, self.palette_id, action_name,
                handler, self, pre_ctx, data,
            )
            return

        # --- Sync path: handler runs inline on the main thread ---
        try:
            result = handler(self, None, data)
        except Exception as e:
            log.handle_error(f'{self.command_name}.{action_name}')
            result = {'success': False, 'error': str(e) or 'Handler error.'}
        html_args.returnData = json.dumps(result if result is not None else {'success': True})

    # ------------------------------------------------------------------
    # Helpers for action handlers
    # ------------------------------------------------------------------
    def _resolve_ctx(self):
        """Resolve a RequestContext or return (None, error_dict) for unauthorized.

        When called from a background async-dispatcher thread, returns the
        RequestContext that was pre-resolved on the main thread (stored in
        async_dispatcher._thread_ctx).  This avoids calling Fusion API
        (app.user, app.activeHub, …) from a non-UI thread.
        """
        pre = getattr(async_dispatcher._thread_ctx, 'ctx', async_dispatcher._UNSET)
        if pre is not async_dispatcher._UNSET:
            if pre is None:
                return None, {'success': False, 'error': 'Not signed in.', 'unauthorized': True}
            return pre, None
        ctx = RequestContext.resolve(self._app)
        if ctx is None:
            return None, {'success': False, 'error': 'Not signed in.', 'unauthorized': True}
        return ctx, None

    def _client(self, ctx):
        """Build an FmClient for the given context (lazy import to avoid a cycle)."""
        from ..services.fm_client import FmClient
        return FmClient(ctx)

    # ------------------------------------------------------------------
    # Shared @actions — implemented ONCE here for every PaletteCommand.
    # ------------------------------------------------------------------
    @action('getAuthStatus')
    def _act_get_auth_status(self, _ctx, data):
        tenant = auth.get_tenant(self._app)
        has_token = auth.has_valid_token()
        from .context import get_fusion_user_id
        _, last_error = auth.get_last_token_result()
        return {
            'success': True,
            'tenant': tenant or '',
            'tenantSource': 'active_hub' if tenant else 'none',
            'hasToken': has_token,
            'userId': get_fusion_user_id(self._app) or '',
            'lastError': _humanize_auth_error(last_error),
            'lastErrorCode': last_error or '',
        }

    @action('getWorkspaceAccess')
    def _act_get_workspace_access(self, _ctx, data):
        """Report whether the user is entitled to this command's workspace.
        Optionally accepts a 'systemName' override in the payload."""
        sys_name = data.get('systemName') or self.workspace_system_name
        if not sys_name:
            return {'success': True, 'hasAccess': True, 'systemName': None}
        entitled = entitlements.get_entitled_workspace_ids(self._app)
        if entitled is None:
            # Not signed in / API unavailable — fail open.
            return {'success': True, 'hasAccess': True, 'systemName': sys_name, 'failedOpen': True}
        wid = entitlements.get_workspace_id(sys_name, self._app)
        return {
            'success': True,
            'systemName': sys_name,
            'workspaceId': wid,
            'hasAccess': bool(wid and wid in entitled),
        }

    @action('getTheme')
    def _act_get_theme(self, _ctx, data):
        try:
            theme_val = getattr(self._ui, 'theme', None)
            theme_id = int(theme_val.value) if theme_val is not None and hasattr(theme_val, 'value') else 1
        except Exception:
            theme_id = 1
        return {'success': True, 'themeId': theme_id,
                'themeName': _THEME_NAMES.get(theme_id, 'light-gray')}

    @action('getUiPrefs')
    def _act_get_ui_prefs(self, _ctx, data):
        return {'success': True, 'prefs': auth.load_ui_prefs()}

    @action('setUiPrefs')
    def _act_set_ui_prefs(self, _ctx, data):
        return {'success': True, 'prefs': auth.save_ui_prefs(data or {})}

    @action('openInBrowser')
    def _act_open_in_browser(self, _ctx, data):
        url = (data or {}).get('url', '')
        if url and isinstance(url, str) and url.startswith('http'):
            webbrowser.open(url)
            return {'success': True}
        return {'success': False, 'error': 'Invalid URL.'}

    @action('openInFusion')
    def _act_open_in_fusion(self, _ctx, data):
        """Open/activate a Fusion document by its lineage URN.

        The caller passes either a 'lineageUrn' directly, or a (workspaceId, itemId)
        pair whose item's LINEAGE_URN field is resolved via FM first.
        """
        lineage_urn = (data or {}).get('lineageUrn') or ''
        if not lineage_urn:
            ctx, err = self._resolve_ctx()
            if err:
                return err
            ws = data.get('workspaceId')
            iid = data.get('itemId')
            if not ws or not iid:
                return {'success': False, 'error': 'Missing lineageUrn or workspaceId/itemId.'}
            item, _etag, fetch_err = self._client(ctx).item_detail(ws, iid)
            if fetch_err:
                return {'success': False, 'error': fetch_err}
            lineage_urn = _field_value_from_item(item, 'LINEAGE_URN') or ''
        if not lineage_urn or not lineage_urn.startswith('urn:'):
            return {'success': False, 'error': 'Item has no Lineage URN (not a Fusion design?).'}
        try:
            data_obj = self._app.data
            if not data_obj:
                return {'success': False, 'error': 'Fusion Data API not available.'}
            data_file = data_obj.findFileById(lineage_urn)
            if not data_file:
                return {'success': False, 'error': 'File not found in Fusion data. Check project access.'}
            docs = self._app.documents
            for i in range(docs.count):
                doc = docs.item(i)
                if doc.dataFile and getattr(doc.dataFile, 'id', None) == lineage_urn:
                    doc.activate()
                    return {'success': True, 'message': 'Document activated.'}
            opened = docs.open(data_file, True)
            if opened:
                return {'success': True, 'message': 'Opened in Fusion.'}
            return {'success': False, 'error': 'Could not open document.'}
        except Exception as e:
            log.log(f'{self.command_name}: openInFusion failed: {e}',
                    adsk.core.LogLevels.WarningLogLevel)
            return {'success': False, 'error': str(e) or 'Open in Fusion failed.'}

    @action('getItemDetail')
    def _act_get_item_detail(self, _ctx, data):
        """Generic item-detail action used by every capability (no per-domain aliases).

        Payload: { workspaceId, itemId }. Returns the item plus an
        'openInBrowserUrl' deep link.
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = data.get('workspaceId')
        iid = data.get('itemId')
        if not ws or not iid:
            return {'success': False, 'error': 'Missing workspaceId or itemId.'}
        client = self._client(ctx)
        item, etag, fetch_err = client.item_detail(ws, iid)
        if fetch_err:
            unauthorized = fetch_err == 'unauthorized'
            return {'success': False,
                    'error': 'Not signed in.' if unauthorized else fetch_err,
                    'unauthorized': unauthorized}
        if item is not None:
            item = dict(item)
            item['openInBrowserUrl'] = client.build_item_details_url(str(ws), str(iid))
        return {'success': True, 'item': item, 'etag': etag}


# Re-export so subclasses can `from ..core.palette_base import PaletteCommand, action`.
__all__ = ['PaletteCommand', 'action']


def _field_value_from_item(item, field_id):
    """Return the value of a field by id from an FM item's sections, or None.

    The v3 item payload may expose the field id as a '__self__'/'link' path
    (.../fields/LINEAGE_URN) rather than a bare short name, so match on either form
    (same robustness as services/items.py)."""
    if not item:
        return None
    for sec in item.get('sections') or []:
        for f in sec.get('fields') or []:
            if not isinstance(f, dict):
                continue
            if f.get('id') == field_id:
                return f.get('value')
            self_ref = f.get('__self__') or f.get('link') or ''
            m = re.search(r'fields/([^/?]+)', self_ref)
            if m and m.group(1) == field_id:
                return f.get('value')
    return None
