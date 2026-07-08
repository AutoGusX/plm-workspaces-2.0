# PLM Workspaces 2.0 — configuration DATA ONLY.
#
# IDs, OAuth URLs, workspace systemNames, admin URLs, executor timeout.
# No logic lives here. Path logic lives in core/paths.py.
#
# IMPORTANT — coexistence with the old "PLM Workspaces" add-in:
# Every command id, palette id, panel id, and the OAuth custom-event id carries a
# distinct "_V2" suffix so the two add-ins never collide on UI elements when both
# are installed side by side. (Fusion command/palette IDs are global per session.)

import os

# Debug flag — when True, all log messages go to the Text Command window.
DEBUG = True

# Distinct add-in identity. The old add-in used the folder name "PLM Workspaces";
# we hardcode a 2.0 company/name prefix so generated IDs never collide with it.
ADDIN_NAME = 'PLMWorkspaces2'
COMPANY_NAME = 'ACME'

# Stable add-in folder name — must match this add-in's folder so every Fusion
# process resolves the same tokens/prefs directory (see core/paths.py).
STABLE_ADDIN_FOLDER_NAME = 'PLM Workspaces 2.0'

# ---------------------------------------------------------------------------
# Fusion workspaces / tabs / panels that host PLM command buttons.
# ---------------------------------------------------------------------------
WORKSPACE_ID = 'FusionSolidEnvironment'             # Design workspace
DRAWING_WORKSPACE_ID = 'FusionDocumentationEnvironment'  # Drawing workspace
MANUFACTURE_WORKSPACE_ID = 'CAMEnvironment'         # Manufacture workspace
CAM_UTILITIES_TAB_ID = 'UtilitiesTab'
CAM_MANAGE_PANEL_ID = 'CAMManagePanel'              # Fusion-native; never created/deleted by us
MANAGE_TAB_ID = 'ManageTab'

# Electronics environment (PCB layout + schematic) — hosts the Export Electronics BOM
# command. These environment ids are candidates from the Fusion Electronics preview;
# confirm the exact ids in-Fusion (the panel helper tolerates whichever resolves).
ELECTRONICS_PCB_WORKSPACE_ID = 'FusionElectronicsPcbEnvironment'
ELECTRONICS_SCHEMATIC_WORKSPACE_ID = 'FusionElectronicsSchematicEnvironment'
# Extra candidate ids seen across Fusion Electronics builds (tried in order).
ELECTRONICS_WORKSPACE_ID_CANDIDATES = [
    'FusionElectronicsPcbEnvironment',
    'FusionElectronicsSchematicEnvironment',
    'ElectronicsPcbEnvironment',
    'ElectronicsSchematicEnvironment',
    'FusionElectronicsEnvironment',
]

# Our own panel on the Manage tab (created on start, removed on stop) — V2-suffixed id.
PLM_WORKSPACES_PANEL_ID = f'{COMPANY_NAME}_{ADDIN_NAME}_PLMWorkspacesPanel_V2'
# Our own panel in the Electronics environment(s).
ELECTRONICS_PANEL_ID = f'{COMPANY_NAME}_{ADDIN_NAME}_ElectronicsPanel_V2'

# Fallback when the Manage tab is not found (e.g. some locales).
FALLBACK_PANEL_ID = 'SolidScriptsAddinsPanel'
FALLBACK_COMMAND_BESIDE_ID = 'ScriptsManagerCommand'

# ---------------------------------------------------------------------------
# Palette IDs — one per capability, all V2-suffixed.
# Phase 0 only ships the login palette; the rest are reserved so later phases
# register them without renumbering.
# ---------------------------------------------------------------------------
PALETTE_ID_LOGIN = f'{COMPANY_NAME}_{ADDIN_NAME}_Login_V2'
PALETTE_ID_MY_WORK = f'{COMPANY_NAME}_{ADDIN_NAME}_MyWork_V2'
PALETTE_ID_CHANGE_MANAGEMENT = f'{COMPANY_NAME}_{ADDIN_NAME}_ChangeManagement_V2'
PALETTE_ID_REQUIREMENTS_MANAGEMENT = f'{COMPANY_NAME}_{ADDIN_NAME}_RequirementsManagement_V2'
PALETTE_ID_ENGINEERING_PROJECT_MANAGEMENT = f'{COMPANY_NAME}_{ADDIN_NAME}_EngineeringProjectManagement_V2'
PALETTE_ID_SUPPLIER_PACKAGES = f'{COMPANY_NAME}_{ADDIN_NAME}_SupplierPackages_V2'
PALETTE_ID_DESIGN_REVIEW = f'{COMPANY_NAME}_{ADDIN_NAME}_DesignReview_V2'
PALETTE_ID_EXPORT_PDF = f'{COMPANY_NAME}_{ADDIN_NAME}_ExportPdfToPlm_V2'
PALETTE_ID_EXPORT_GCODE = f'{COMPANY_NAME}_{ADDIN_NAME}_ExportGcodeToPlm_V2'
PALETTE_ID_EXPORT_DXF    = f'{COMPANY_NAME}_{ADDIN_NAME}_ExportDxfToPlm_V2'
PALETTE_ID_EXPORT_TO_PLM = f'{COMPANY_NAME}_{ADDIN_NAME}_ExportToPlm_V2'
PALETTE_ID_PLM_CHARTS = f'{COMPANY_NAME}_{ADDIN_NAME}_PlmCharts_V2'
PALETTE_ID_EXPORT_ELECTRONICS_BOM = f'{COMPANY_NAME}_{ADDIN_NAME}_ExportElectronicsBom_V2'

# ---------------------------------------------------------------------------
# Command definition IDs (panel buttons) — all V2-suffixed. Keyed by capability.
# ---------------------------------------------------------------------------
COMMAND_IDS = {
    'login': f'{COMPANY_NAME}_{ADDIN_NAME}_Login_V2',  # no panel button, but a cmd def is still defined
    'taskManagement': f'{COMPANY_NAME}_{ADDIN_NAME}_MyWork_V2',
    'changeManagement': f'{COMPANY_NAME}_{ADDIN_NAME}_ChangeManagement_V2',
    'requirementsManagement': f'{COMPANY_NAME}_{ADDIN_NAME}_RequirementsManagement_V2',
    'engineeringProjectManagement': f'{COMPANY_NAME}_{ADDIN_NAME}_EngineeringProjectManagement_V2',
    'supplierPackages': f'{COMPANY_NAME}_{ADDIN_NAME}_SupplierPackages_V2',
    'exportPdfToPlm': f'{COMPANY_NAME}_{ADDIN_NAME}_ExportPdfToPlm_V2',
    'exportGcodeToPlm': f'{COMPANY_NAME}_{ADDIN_NAME}_ExportGcodeToPlm_V2',
    'exportDxfToPlm':  f'{COMPANY_NAME}_{ADDIN_NAME}_ExportDxfToPlm_V2',
    'exportToPlm':     f'{COMPANY_NAME}_{ADDIN_NAME}_ExportToPlm_V2',
    'plmCharts':       f'{COMPANY_NAME}_{ADDIN_NAME}_PlmCharts_V2',
    'exportElectronicsBom': f'{COMPANY_NAME}_{ADDIN_NAME}_ExportElectronicsBom_V2',
}

# Commands that live on a NON-PLM panel (CAM Manage / Electronics) — excluded from the
# PLM panel entitlement sync so their button never leaks onto the Design/Drawing panel.
NON_PLM_PANEL_COMMANDS = {'exportGcodeToPlm', 'exportElectronicsBom'}

# ---------------------------------------------------------------------------
# OAuth / PKCE (APS). The client id is REUSED verbatim from the old config so
# the same APS app registration / tenant whitelist works for both add-ins.
# Override via the APS_CLIENT_ID environment variable in production.
# ---------------------------------------------------------------------------
APS_CLIENT_ID = os.environ.get('APS_CLIENT_ID', 'iI3dUG9G7akGtYgQlluEukAXnLyKoib68GGAAdtA3lOqC6Yk')
APS_REDIRECT_URI = 'http://localhost:8080/'
APS_CALLBACK_PORT = 8080
APS_AUTHORIZE_URL = 'https://developer.api.autodesk.com/authentication/v2/authorize'
APS_TOKEN_URL = 'https://developer.api.autodesk.com/authentication/v2/token'
APS_SCOPE = 'data:read data:write data:create'

# Custom event fired from the OAuth callback thread back onto the Fusion UI thread.
# V2-suffixed so the old add-in's listener never receives our token (and vice versa).
OAUTH_CUSTOM_EVENT_ID = f'{COMPANY_NAME}_{ADDIN_NAME}_oauth_token_V2'

# Custom event fired from background action-handler threads when their HTTP work
# completes. The main-thread handler receives it and calls palette.sendInfoToHTML.
ASYNC_RESULT_EVENT_ID = f'{COMPANY_NAME}_{ADDIN_NAME}_async_result_V2'

# ---------------------------------------------------------------------------
# Entitlements: FM workspace systemNames per capability.
# systemNames are consistent across all FM environments (unlike numeric IDs which
# vary per tenant). Resolved to the environment-specific numeric ID at runtime via
# GET /api/v3/workspaces?unlimited=true. My Work (taskManagement) has no workspace
# gate: outstanding work is user-centric and always enabled.
# ---------------------------------------------------------------------------
WORKSPACE_SYSTEM_NAMES = {
    'changeManagement': 'WS_CHANGE_ORDERS',
    # The unified Change Management list spans three workspaces; CR/CT resolve at runtime.
    'changeRequests': 'WS_CHANGE_REQUESTS',
    'changeTasks': 'WS_CHANGE_TASKS',
    'requirementsManagement': 'WS_REQUIREMENTS',
    'engineeringProjectManagement': 'WS_ENGINEERING_PROJECTS',
    'supplierPackages': 'WS_SUPPLIER_PACKAGES',
    'designReview': 'WS_DESIGN_REVIEWS',
    # Drawings mirrors the Components pattern (open-in-Fusion icon + picker mode).
    'drawings': 'CW_DRAWINGS',
    'components': 'CW_COMPONENTS',
}

# Numeric ID fallbacks for the development environment. Used when a systemName has
# no entry or is not found in the current environment's workspace list.
WORKSPACE_IDS = {
    'changeManagement': 9,
    'requirementsManagement': 245,
    'engineeringProjectManagement': 322,
    'supplierPackages': 209,
    'designReview': 241,
    'drawings': 76,
    'components': 57,
}

# Reverse map: static fallback for environments where systemName resolution hasn't run.
WORKSPACE_ID_TO_COMMAND = {str(wid): key for key, wid in WORKSPACE_IDS.items()}

# Affected Items (LINKEDITEMS tab) is served off view id 11 for all change workspaces.
AFFECTED_ITEMS_VIEW_ID = 11

# Components workspace — used by taskManagement for CAD context (open-in-Fusion, lineage search).
COMPONENTS_WORKSPACE_SYSTEM_NAME = 'CW_COMPONENTS'
COMPONENTS_WORKSPACE_ID_FALLBACK = '57'   # dev environment fallback

# Upper bound for concurrent.futures result() calls in parallel API fetches.
# Individual urllib calls time out at 30s; this wraps multi-request handlers so a
# stuck worker cannot block the Fusion UI thread indefinitely.
EXECUTOR_TIMEOUT_SECONDS = 60

# Default per-request HTTP timeout (seconds) for http_client.request.
HTTP_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------------
# Admin URLs shown when the user has no access to a workspace ({tenant} substituted).
# ---------------------------------------------------------------------------
ADMIN_URL_GROUPS = 'https://{tenant}.autodesk360.com/g/admin/manage/groups'
ADMIN_URL_TEMPLATE_LIBRARY = 'https://{tenant}.autodeskplm360.net/plm/admin/template-library'
ADMIN_URL_GENERAL_SETTINGS = 'https://{tenant}.autodeskplm360.net/plm/admin/system-configuration/general-settings'
