# PLM Workspaces 2.0 — Unified Architecture

> Status: **PROPOSED — awaiting approval.** This document is the source of truth
> for the 2.0 rewrite. The old add-in at `../PLM Workspaces` remains installed and
> working as the reference implementation throughout the migration.

## 1. Goals

1. **One way to do each thing.** Auth gating, API access, action dispatch, palette
   lifecycle, theming, and error handling are defined once and inherited — not
   re-implemented per capability.
2. **Adding a capability is small.** A new workspace command should be ~40 lines of
   Python (palette config + a handful of `@action` handlers) plus a config block on
   the shared frontend engine.
3. **Zero-build frontend.** Pure vanilla JS loaded directly by the Fusion webview
   (decision confirmed). Modernize *structure*, not the stack. No Node, no bundler.
4. **Cross-platform.** Windows + macOS (manifest targets both). No OS-specific paths
   outside `core/paths.py`.
5. **No regressions.** Feature parity with the old add-in, verified in Fusion per phase.

## 2. The one hard constraint

A Fusion palette is an embedded webview. The *only* channel between it and Python is:

- **JS → Python:** `adsk.fusionSendData(action, JSON.stringify(payload))` → returns a
  `Promise<string>` resolving to `html_args.returnData`.
- **Python → JS (push):** `palette.sendInfoToHTML(action, jsonString)` →
  `window.fusionJavaScriptHandler.handle(action, data)`.

Everything below is shaped to make that single contract uniform and typed-by-convention.

## 3. Directory layout

```
PLM Workspaces 2.0/
  PLM Workspaces.py              # add-in entry: run(context) / stop(context)
  PLM Workspaces.manifest        # copied from old; version reset
  AddInIcon.svg
  manifest/                      # (assets, icons per capability)
  core/
    __init__.py
    config.py                    # IDs, OAuth URLs, workspace systemNames — DATA ONLY
    paths.py                     # stable-path + addin-root resolution (cross-platform)
    log.py                       # futil-style logging + handle_error
    events.py                    # add_handler / clear_handlers (from fusionAddInUtils)
    http_client.py               # THE single request helper: build + send + 401 + error parse
    auth.py                      # PKCE flow, token cache, callback server (from oauth_pkce)
    entitlements.py              # systemName -> numeric id resolution, panel gating
    context.py                   # RequestContext: tenant + user_id + bearer, resolved once
    action_registry.py           # @action decorator + dispatch table
    palette_base.py              # PaletteCommand base class (lifecycle + dispatch + auth gate)
    panels.py                    # add/remove command buttons on PLM panel + CAM panel
  services/
    __init__.py
    fm_client.py                 # FM v3 client object (wraps http_client; cohesive methods)
    items.py                     # item detail / create / update / fields / sections / tableaus
    workflow.py                  # transitions: list + execute
    attachments.py               # list / request-upload / s3 put / check-in
    search.py                    # search-results, lineage lookup
    fusion_cad.py                # Fusion-side helpers: components, drawings, lineage URNs, screenshots
  commands/
    __init__.py                  # registers all PaletteCommand subclasses
    login/command.py             # auth palette (special: no panel button)
    my_work/command.py           # taskManagement
    change_management/command.py
    requirements/command.py
    engineering_projects/command.py
    supplier_packages/command.py
    design_review/command.py
    export_pdf/command.py
    export_gcode/command.py
    <each>/resources/            # 16x16/32x32/64x64 png + html/
  web/
    core/
      bridge.js                  # plmSend/plmParse + push handler + auth poller (ONE copy)
      engine.js                  # list/detail/form/picker engine (from plmWorkspaceCore.js)
                                 #   _pickerInlineHtml() is the ONE source of picker markup
      theme.js                   # theme + UI-prefs popover (styles moved to CSS)
      tokens.css                 # design tokens (colors, spacing, type, dark themes)
      components.css             # shared component styles (buttons, tables, picker, tabs)
    capabilities/
      my_work/                   # capability-specific config.js + hooks + view html
      design_review/             # screenshot/markup hooks live here
      ...
  docs/
    ARCHITECTURE.md              # this file
    IMPLEMENTATION_PLAN.md
    JS_PYTHON_CONTRACT.md        # generated in Phase 0
    VERIFICATION.md              # manual-in-Fusion checklist, grown per phase
```

## 4. Python backend design

### 4.1 `core/http_client.py` — the single request helper

Collapses the ~30 copies of "build request / urlopen / except HTTPError / 401 branch /
parse error body" in the old `fusion_manage_api.py` into one function.

```python
def request(method, url, *, bearer, user_id=None, tenant=None,
            accept='application/json', body=None, content_type=None,
            extra_headers=None, timeout=30, raw=False):
    """Returns Result(ok, status, data|text, etag, error, unauthorized)."""
```

- Centralizes header assembly (`Authorization`, `Accept`, `x-User-id`, `X-Tenant`).
- Centralizes JSON decode + the "empty body means []" quirks (search, affected-items).
- Centralizes error-body parsing (the old `_parse_workflow_error_response`).
- Returns a small `Result` dataclass-like dict so callers never write try/except again.

### 4.2 `core/context.py` — RequestContext

```python
class RequestContext:
    tenant: str        # auth_utils.get_tenant(app)
    user_id: str       # fusion user id / email
    bearer: str        # oauth.get_valid_access_token()
    @classmethod
    def resolve(cls, app) -> 'RequestContext | None'  # None if not signed in
```

Resolved once per palette action. Kills the duplicated `_get_fusion_user_id` /
`_get_bearer` helpers in all 8 modules.

### 4.3 `services/fm_client.py` — cohesive API

`FmClient(ctx)` wraps `http_client` + `RequestContext`. Methods replace the flat
functions and become 3–5 lines each. **One** place implements the 401-refresh-retry
(the old per-module `_api()` wrapper) — `FmClient` retries once on `unauthorized`,
refreshing the token via `core.auth`.

```python
class FmClient:
    def __init__(self, ctx): ...
    def outstanding_work(self): ...
    def item_detail(self, ws, item): ...           # returns (item, etag)
    def view_fields(self, ws, view=1): ...
    def sections(self, ws): ...
    def tableaus(self, ws); def tableau_data(self, ws, tid, page, size): ...
    def transitions(self, ws, item); def run_transition(...): ...
    def create_item(self, ws, sections); def update_item(self, ws, item, sections, etag): ...
    def affected_items(...); def add_affected(...); def remove_affected(...)
    def attachments(...); def request_upload(...); def checkin(...)   # + search.* helpers
```

### 4.4 `core/action_registry.py` + `core/palette_base.py`

**`@action`** registers a handler in a per-class dispatch table:

```python
class MyWork(PaletteCommand):
    @action('getTasks')
    def get_tasks(self, ctx, data): ...
```

**`PaletteCommand`** owns the entire lifecycle so subclasses declare config + actions only:

```python
class PaletteCommand:
    # --- subclass declares these ---
    command_id: str
    command_name: str
    palette_id: str
    palette_title: str
    html_url: str                       # resources/html/index.html
    palette_size = (700, 600)
    resizable = True
    docking = 'left'                    # 'left' | 'right'
    panel = 'plm'                       # 'plm' | 'cam'
    workspace_system_name: str | None   # for entitlement gating
    requires_auth = True                # base decides login-palette behavior — UNIFORM

    # --- base provides ---
    def start(self): ...                # cmd def, panel button, OAuth event, idempotent
    def stop(self): ...
    def _on_command_execute(self, args): ...    # create/show palette, auth gate
    def _on_incoming(self, html_args): ...      # parse data, dispatch via registry, return JSON
    # shared @actions: getAuthStatus, getTheme, getUiPrefs, setUiPrefs,
    #                  openInBrowser, openInFusion, getWorkspaceAccess, getItemDetail
```

Result: each `command.py` is small and **cannot** drift on auth/dispatch/lifecycle,
because it doesn't own them.

### 4.5 Auth & entitlements

`core/auth.py` is `oauth_pkce.py` cleaned up (callback server, token cache, file
persistence, stay-signed-in) — behavior preserved, no redesign of a working flow.
`core/entitlements.py` keeps the `systemName → numeric id` resolution. The login
palette becomes a `PaletteCommand` subclass with `requires_auth=False` and no panel button.

## 5. Frontend design (vanilla, no build)

### 5.1 Loading order (unchanged mechanism, deduplicated files)

```html
<link rel="stylesheet" href="/web/core/tokens.css">
<link rel="stylesheet" href="/web/core/components.css">
<script src="/web/core/bridge.js"></script>
<script src="/web/core/theme.js"></script>
<script src="/web/capabilities/<cap>/config.js"></script>   <!-- sets window._plmCfg + hooks -->
<script src="/web/core/engine.js"></script>                 <!-- reads _plmCfg on DOMContentLoaded -->
```

### 5.2 `web/core/bridge.js` — ONE messaging layer

`plmSend(action, payload) -> Promise<obj>`, `plmParse`, the push-message handler
(`window.fusionJavaScriptHandler`), and the auth poller — single definition.
**Eliminates** the `designReview` fork of `plmPaletteUtils.js` and the three
independent auth pollers.

### 5.3 `web/core/engine.js` — ONE list/detail/form/picker engine

Ported from `plmWorkspaceCore.js`, driven by `window._plmCfg`:

```js
window._plmCfg = {
  workspaceKey: 'changeManagement',     // resolves to systemName/id on Python side
  title: 'Change Management',
  detailAction: 'getItemDetail',        // GENERIC name for all — no per-domain aliases
  showAffectedItems: true,
  hooks: { buildCustomFormField, renderCustomDetailField, onDetailToolbarExtra, afterSave }
};
```

- Component-picker markup lives as an inline template string (`_pickerInlineHtml()`)
  inside `engine.js` — the single source, injected directly at startup with no XHR
  and no separate file, eliminating all file:// CORS risk.
- Settings/theme popover styles move from inline-JS strings into `components.css`.

### 5.4 `taskManagement` and `designReview`

- **My Work**: the old 1,916-line standalone palette.js is re-expressed as a *capability
  config + hooks* on the engine where it shares list/detail/picker, keeping only its
  genuinely unique pieces (tile coloring, summary strip, CAD-context matching) as hooks.
  This is the largest single port (Phase 3) and is allowed its own view module under
  `web/capabilities/my_work/` if a piece doesn't fit the generic engine — but it consumes
  the shared `bridge.js`, `tokens.css`, and `components.css`.
- **Design Review**: screenshot capture + canvas markup stay as capability hooks
  (`buildCustomFormField`/`renderCustomDetailField`) under `web/capabilities/design_review/`.

## 6. The JS↔Python action contract (uniform)

- Every response is `{ "success": true, ... }` or `{ "success": false, "error": "...", "unauthorized": true? }`.
- Generic action names; **no** `getChangeRecordDetail`/`getProjectDetail` aliases — all use
  `getItemDetail` with `workspaceKey` in the payload.
- Shared actions implemented once on `PaletteCommand`:
  `getAuthStatus, getWorkspaceAccess, getTheme, getUiPrefs, setUiPrefs, openInBrowser, openInFusion`.
- Full action catalog is generated into `docs/JS_PYTHON_CONTRACT.md` in Phase 0 and kept current.

## 7. What is preserved vs. changed

**Preserved (working — don't redesign):** OAuth PKCE flow + callback page, token
persistence/stay-signed-in, tenant resolution from active hub, entitlement
systemName resolution, FM v3 endpoints + headers, S3 attachment upload flow, the
screenshot/markup UX, CAD lineage-URN matching logic.

**Changed (the rewrite):** flat API module → layered `http_client` + `fm_client`;
per-module boilerplate → `PaletteCommand` base; if/elif dispatch → `@action` registry;
inconsistent auth gating → uniform base behavior; per-domain action names → generic;
duplicated/forked frontend → one `bridge.js` + `engine.js` + tokens; inline-JS styles → CSS.

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Can't unit-test Fusion code headlessly | Per-phase **manual verification checklist** run in Fusion (`VERIFICATION.md`); static import/parity scans by a QA sub-agent. |
| My Work port loses behavior | Port behind the engine incrementally; diff against old palette feature list before sign-off. |
| OAuth/token regressions | Lift `auth.py` with minimal changes; verify sign-in / refresh / stay-signed-in explicitly in Phase 0. |
| macOS path/webview differences | All path logic in `core/paths.py`; verify on both OSes if available, else note Windows-verified. |
| Old + new add-ins both installed | Distinct command/palette IDs (2.0 suffix) so panels don't collide during migration. |
