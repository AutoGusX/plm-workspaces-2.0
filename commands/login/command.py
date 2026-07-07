# Login palette — the Phase-0 smoke command that proves the PaletteCommand base
# end-to-end (lifecycle + dispatch + auth + token persistence + custom-event hook).
#
# Special among commands: requires_auth=False (it IS the auth surface) and no panel
# button (shown programmatically by the base's auth gate). It reuses the shared
# getUiPrefs/setUiPrefs/getTheme actions from PaletteCommand and adds the login-only
# actions (getLoginInfo, startLogin, cancel, openFusionManageAdmin, pageReady).

import os
import webbrowser

import adsk.core

from ...core import auth
from ...core import config
from ...core import entitlements
from ...core import log
from ...core import paths
from ...core.palette_base import PaletteCommand, action

# Module-level singleton so the base's auth gate can call LoginCommand.show()
# without re-instantiating (the instance is created in commands/__init__.py).
_instance = None


class LoginCommand(PaletteCommand):
    command_id = config.COMMAND_IDS['login']
    command_name = 'Sign in to PLM'
    command_tooltip = 'Sign in to Fusion Manage PLM'
    palette_id = config.PALETTE_ID_LOGIN
    palette_title = 'Sign in to PLM'
    palette_size = (420, 520)
    resizable = True
    docking = 'left'
    panel = None
    show_panel_button = False     # shown programmatically, never on a toolbar
    requires_auth = False         # this is the auth surface itself
    workspace_system_name = None

    html_url = paths.to_file_url_path(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     'resources', 'html', 'login.html'))

    def __init__(self):
        super().__init__()
        global _instance
        _instance = self

    # ------------------------------------------------------------------
    # Programmatic show (called by the base auth gate on other commands).
    # ------------------------------------------------------------------
    @classmethod
    def show(cls):
        if _instance is not None:
            _instance._show_palette()

    def _show_palette(self):
        # Re-apply the configured size so the palette doesn't reopen at a previously
        # shrunk size (preserved from the old login behavior).
        palette = self._create_palette()
        try:
            palette.width, palette.height = self.palette_size
        except Exception:
            pass
        palette.isVisible = True
        try:
            if palette.dockingState == adsk.core.PaletteDockingStates.PaletteDockStateFloating:
                palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateLeft
        except Exception:
            pass
        return palette

    def _close(self):
        try:
            palette = self._ui.palettes.itemById(self.palette_id)
            if palette:
                palette.isVisible = False
        except Exception:
            pass

    # ------------------------------------------------------------------
    # OAuth token hook: on sign-in, close the login palette and notify open palettes.
    # ------------------------------------------------------------------
    def _on_token(self, token_json):
        self._close()
        entitlements.clear_entitlement_cache()
        # Sync entitled panel buttons now that we know the user's workspace access.
        try:
            from ...core import panels
            panels.sync_plm_panel_buttons(self._app)
        except Exception:
            pass
        # Notify any open PLM palettes so they can refresh without a manual reload.
        for pid in (config.PALETTE_ID_MY_WORK, config.PALETTE_ID_CHANGE_MANAGEMENT,
                    config.PALETTE_ID_REQUIREMENTS_MANAGEMENT,
                    config.PALETTE_ID_ENGINEERING_PROJECT_MANAGEMENT,
                    config.PALETTE_ID_SUPPLIER_PACKAGES):
            try:
                p = self._ui.palettes.itemById(pid)
                if p and p.isVisible:
                    p.sendInfoToHTML('tokenResult', token_json)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Login-only actions.
    # ------------------------------------------------------------------
    @action('getLoginInfo')
    def _act_get_login_info(self, _ctx, data):
        tenant = auth.get_tenant(self._app)
        has_token = auth.has_valid_token()
        return {
            'success': True,
            'tenant': tenant or '(open a cloud document for tenant)',
            'status': 'Signed in' if has_token else 'Not signed in',
            'hasToken': has_token,
            'clientId': getattr(config, 'APS_CLIENT_ID', '') or '',
        }

    @action('startLogin')
    def _act_start_login(self, _ctx, data):
        stay_signed_in = data.get('staySignedIn', True)
        try:
            auth.set_pending_stay_signed_in(stay_signed_in)
            auth.set_stay_signed_in(stay_signed_in)
            auth.start_callback_server(self._app, config.OAUTH_CUSTOM_EVENT_ID)
            url = auth.prepare_login()
            webbrowser.open(url)
            return {'success': True, 'started': True,
                    'message': 'Complete sign-in in your browser, then return to Fusion.'}
        except Exception as e:
            log.log(f'Login: startLogin error: {e}', adsk.core.LogLevels.ErrorLogLevel)
            return {'success': False, 'started': False, 'error': str(e)}

    @action('cancel')
    def _act_cancel(self, _ctx, data):
        self._close()
        return {'success': True}

    @action('openFusionManageAdmin')
    def _act_open_admin(self, _ctx, data):
        tenant = (data.get('tenant', '') or auth.get_tenant(self._app) or '').strip()
        if tenant:
            webbrowser.open(config.ADMIN_URL_GENERAL_SETTINGS.format(tenant=tenant))
            return {'success': True, 'opened': True}
        return {'success': False,
                'error': 'No tenant set. Open a cloud document to resolve the tenant.'}

    @action('pageReady')
    def _act_page_ready(self, _ctx, data):
        # Re-apply palette size after the HTML lays out so content fits correctly.
        try:
            palette = self._ui.palettes.itemById(self.palette_id)
            if palette and palette.isVisible:
                palette.width, palette.height = self.palette_size
        except Exception:
            pass
        return {'success': True}
