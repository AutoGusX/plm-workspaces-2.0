# Panel button placement. Ported from the old lib/ui_utils.py (+ the entitlement
# button-sync from lib/entitlements.sync_plm_panel_buttons).
#
# The "PLM Workspaces 2.0" panel is created on the Manage tab of both the Design
# and Drawing workspaces (with a fallback to the Add-Ins panel when the Manage tab
# is absent). The CAM Manage panel is Fusion-native and is never created/deleted.

import adsk.core

from . import config
from . import entitlements
from . import log

# Workspaces that host our PLM panel.
_PANEL_WORKSPACE_IDS = [config.WORKSPACE_ID, config.DRAWING_WORKSPACE_ID]


def get_manage_tab(workspace):
    """Return the Manage toolbar tab for a workspace, or None.

    Falls back to matching by name because the tab id can differ by version/locale.
    """
    try:
        tabs = workspace.toolbarTabs
        tab = tabs.itemById(config.MANAGE_TAB_ID)
        if tab:
            return tab
        for i in range(tabs.count):
            t = tabs.item(i)
            if t and (t.name or '').upper().find('MANAGE') >= 0:
                return t
    except Exception:
        pass
    return None


def _iter_panels(ui, create=True):
    """Yield (panel, used_fallback) for every configured workspace.

    create=True  — create the PLM panel if absent (start()).
    create=False — only yield existing panels (stop() / sync).

    The Add-Ins fallback is tried only for the primary Design workspace.
    """
    for ws_id in _PANEL_WORKSPACE_IDS:
        if not ws_id:
            continue
        workspace = ui.workspaces.itemById(ws_id)
        if not workspace:
            continue
        manage_tab = get_manage_tab(workspace)
        if manage_tab:
            panel = manage_tab.toolbarPanels.itemById(config.PLM_WORKSPACES_PANEL_ID)
            if not panel:
                if not create:
                    continue
                try:
                    panel = manage_tab.toolbarPanels.add(
                        config.PLM_WORKSPACES_PANEL_ID, 'PLM Workspaces 2.0')
                except Exception:
                    continue
            yield panel, False
        elif ws_id == config.WORKSPACE_ID:
            panel = workspace.toolbarPanels.itemById(config.FALLBACK_PANEL_ID)
            if panel:
                yield panel, True


def add_command_to_plm_panels(ui, cmd_def, is_promoted=False):
    """Add cmd_def to the PLM panel in every configured workspace."""
    for panel, used_fallback in _iter_panels(ui, create=True):
        beside_id = config.FALLBACK_COMMAND_BESIDE_ID if used_fallback else ''
        try:
            ctrl = panel.controls.addCommand(cmd_def, beside_id, False)
        except Exception:
            try:
                ctrl = panel.controls.addCommand(cmd_def, None, False)
            except Exception:
                continue
        try:
            ctrl.isPromoted = is_promoted
        except Exception:
            pass


def remove_command_from_plm_panels(ui, cmd_id):
    """Remove a button by cmd_id from PLM panels; delete our panels if they become
    empty (never the fallback Add-Ins panel)."""
    for ws_id in _PANEL_WORKSPACE_IDS:
        if not ws_id:
            continue
        workspace = ui.workspaces.itemById(ws_id)
        if not workspace:
            continue
        manage_tab = get_manage_tab(workspace)
        if manage_tab:
            panel = manage_tab.toolbarPanels.itemById(config.PLM_WORKSPACES_PANEL_ID)
            if panel:
                ctrl = panel.controls.itemById(cmd_id)
                if ctrl:
                    try:
                        ctrl.deleteMe()
                    except Exception:
                        pass
                if panel.controls.count == 0:
                    try:
                        panel.deleteMe()
                    except Exception:
                        pass
        elif ws_id == config.WORKSPACE_ID:
            panel = workspace.toolbarPanels.itemById(config.FALLBACK_PANEL_ID)
            if panel:
                ctrl = panel.controls.itemById(cmd_id)
                if ctrl:
                    try:
                        ctrl.deleteMe()
                    except Exception:
                        pass


def add_command_to_cam_manage_panel(ui, cmd_def, is_promoted=False):
    """Add cmd_def to the Fusion-native CAM Manage panel (Manufacture workspace).

    Targets CAMEnvironment -> UtilitiesTab -> CAMManagePanel. Does NOT create the
    panel. Returns True on success.
    """
    ws = ui.workspaces.itemById(config.MANUFACTURE_WORKSPACE_ID)
    if not ws:
        return False
    tab = ws.toolbarTabs.itemById(config.CAM_UTILITIES_TAB_ID)
    if not tab:
        return False
    panel = tab.toolbarPanels.itemById(config.CAM_MANAGE_PANEL_ID)
    if not panel:
        return False
    try:
        ctrl = panel.controls.addCommand(cmd_def, '', False)
    except Exception:
        try:
            ctrl = panel.controls.addCommand(cmd_def, None, False)
        except Exception:
            return False
    try:
        ctrl.isPromoted = is_promoted
    except Exception:
        pass
    return True


def remove_command_from_cam_manage_panel(ui, cmd_id):
    """Remove a button by cmd_id from the CAM Manage panel. Never deletes the panel."""
    ws = ui.workspaces.itemById(config.MANUFACTURE_WORKSPACE_ID)
    if not ws:
        return
    tab = ws.toolbarTabs.itemById(config.CAM_UTILITIES_TAB_ID)
    if not tab:
        return
    panel = tab.toolbarPanels.itemById(config.CAM_MANAGE_PANEL_ID)
    if not panel:
        return
    ctrl = panel.controls.itemById(cmd_id)
    if ctrl:
        try:
            ctrl.deleteMe()
        except Exception:
            pass


def sync_plm_panel_buttons(app):
    """Add/remove PLM panel buttons by entitlement across all configured workspaces.

    My Work is always shown first; workspace-gated commands follow in COMMAND_IDS
    insertion order (the canonical intended order), skipping any the user lacks
    entitlement for. Idempotent — call after sign-in and at the end of each command's start().
    """
    try:
        ui = app.userInterface

        our_cmd_ids = set(config.COMMAND_IDS.values())

        # Drive button order from COMMAND_IDS insertion order, which is the intended
        # canonical order: My Work → Change Management → Requirements →
        # Engineering Projects → Supplier Packages → (Phase 4 commands).
        # 'login' has no panel button; skip it. Entitlement checks happen per-command
        # in the loop below so non-entitled workspace buttons are simply not added.
        order_to_add = [k for k in config.COMMAND_IDS if k != 'login']

        for panel, used_fallback in _iter_panels(ui, create=False):
            # Remove all existing PLM controls so we can re-add in the correct order.
            for cmd_id in our_cmd_ids:
                ctrl = panel.controls.itemById(cmd_id)
                if ctrl:
                    try:
                        ctrl.deleteMe()
                    except Exception:
                        pass

            beside_id = config.FALLBACK_COMMAND_BESIDE_ID if used_fallback else ''
            for command_key in order_to_add:
                cmd_id = config.COMMAND_IDS.get(command_key)
                if not cmd_id:
                    continue
                cmd_def = ui.commandDefinitions.itemById(cmd_id)
                if not cmd_def:
                    continue
                if command_key != 'taskManagement' and not entitlements.is_command_entitled(command_key, app):
                    continue
                try:
                    ctrl = panel.controls.addCommand(cmd_def, beside_id, False)
                except Exception:
                    try:
                        ctrl = panel.controls.addCommand(cmd_def, None, False)
                    except Exception:
                        continue
                if ctrl:
                    beside_id = cmd_id
    except Exception as e:
        try:
            log.log(f'panels.sync_plm_panel_buttons error: {e}',
                    adsk.core.LogLevels.WarningLogLevel)
        except Exception:
            pass
