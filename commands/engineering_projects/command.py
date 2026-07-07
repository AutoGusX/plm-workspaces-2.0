# Engineering Projects — thin WorkspaceCommand subclass.
#
# All workspace @actions (getTableaus, getTableauData, getViewFields,
# getWorkspaceSections, getItemDetail, createItem, updateItem, workflow,
# affected items, tabs, permissions) are inherited from WorkspaceCommand.
# This file only declares identity + palette config.

import os

from ...core import config
from ...core import paths
from ...core.workspace_command import WorkspaceCommand


class EngineeringProjectsCommand(WorkspaceCommand):
    # --- Fusion command + palette identity ---
    command_id     = config.COMMAND_IDS['engineeringProjectManagement']
    command_name   = 'Engineering Projects'
    command_tooltip = 'View and manage engineering projects in Fusion Manage'
    palette_id     = config.PALETTE_ID_ENGINEERING_PROJECT_MANAGEMENT
    palette_title  = 'Engineering Projects'
    palette_size   = (700, 600)
    docking        = 'left'
    panel          = 'plm'

    # Workspace gating
    workspace_system_name = config.WORKSPACE_SYSTEM_NAMES['engineeringProjectManagement']  # 'WS_ENGINEERING_PROJECTS'
    workspace_id_fallback = config.WORKSPACE_IDS['engineeringProjectManagement']            # 322

    # Engineering Projects workspaces do not typically use Affected Items.
    showAffectedItems = False

    # HTML palette
    html_url = paths.to_file_url_path(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'resources', 'html', 'index.html',
        )
    )
