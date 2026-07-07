# Requirements Management — thin WorkspaceCommand subclass.
#
# All workspace @actions (getTableaus, getTableauData, getViewFields,
# getWorkspaceSections, getItemDetail, createItem, updateItem, workflow,
# affected items, tabs, permissions) are inherited from WorkspaceCommand.
# This file only declares identity + palette config.

import os

from ...core import config
from ...core import paths
from ...core.workspace_command import WorkspaceCommand


class RequirementsCommand(WorkspaceCommand):
    # --- Fusion command + palette identity ---
    command_id     = config.COMMAND_IDS['requirementsManagement']
    command_name   = 'Requirements Management'
    command_tooltip = 'View and manage requirements in Fusion Manage'
    palette_id     = config.PALETTE_ID_REQUIREMENTS_MANAGEMENT
    palette_title  = 'Requirements Management'
    palette_size   = (700, 600)
    docking        = 'left'
    panel          = 'plm'

    # Workspace gating
    workspace_system_name = config.WORKSPACE_SYSTEM_NAMES['requirementsManagement']  # 'WS_REQUIREMENTS'
    workspace_id_fallback = config.WORKSPACE_IDS['requirementsManagement']            # 245

    # Requirements workspaces do not have Affected Items in the standard template.
    showAffectedItems = False

    # HTML palette
    html_url = paths.to_file_url_path(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'resources', 'html', 'index.html',
        )
    )
