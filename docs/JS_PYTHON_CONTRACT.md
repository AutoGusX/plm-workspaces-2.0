# JS ↔ Python Action Contract

> Generated in Phase 0. This is the single source of truth for how the Fusion
> webview (vanilla JS) talks to the Python backend. Keep it current: any new action
> is added here **first**, then implemented.

## 1. The channel (the one hard constraint)

A Fusion palette is an embedded webview. The only channel between it and Python is:

- **JS → Python:** `adsk.fusionSendData(action, JSON.stringify(payload))` returns a
  `Promise<string>` resolving to whatever Python sets on `html_args.returnData`.
- **Python → JS (push):** `palette.sendInfoToHTML(action, jsonString)` invokes
  `window.fusionJavaScriptHandler.handle(action, data)` in the page.

`action` is a short string name; the payload and the response are JSON strings.

## 2. Uniform response shape

Every Python action handler returns a dict that is JSON-serialized onto
`html_args.returnData`. The shape is uniform:

```jsonc
// success
{ "success": true, /* ...action-specific fields... */ }

// failure
{ "success": false, "error": "human-readable message", "unauthorized": true }
```

- `success` is always present and boolean.
- On failure, `error` is always present (a user-presentable string).
- `unauthorized: true` is added when the failure is an auth problem (not signed in,
  or a 401 that survived the single token-refresh retry). The frontend should treat
  this as "show the login palette / prompt sign-in", **not** as a generic error.

The base class (`core/palette_base.PaletteCommand._on_incoming`) guarantees this
shape: it parses the incoming data, dispatches through the `@action` registry, and
JSON-encodes the handler's return value. An unknown action returns
`{"success": false, "error": "Unknown action: <name>"}`. A handler that raises is
caught, logged, and returned as `{"success": false, "error": "<message>"}`.

> Note: the Phase-0 login palette is a thin client and also reads a few non-`success`
> convenience fields (`started`, `message`, `opened`) alongside `success`. New
> capability frontends should rely on `success`/`error`/`unauthorized` only.

## 3. Dispatch mechanism (Python side)

Handlers are registered with the `@action('name')` decorator and dispatched per
class through a table built from the MRO, so subclasses inherit the shared actions:

```python
class MyWork(PaletteCommand):
    @action('getTasks')
    def get_tasks(self, ctx, data):
        return {'success': True, 'tasks': [...]}
```

Handler signature: `handler(self, ctx, data)`.
- `data` is the parsed JSON payload (always a dict; `{}` if none/invalid).
- `ctx` is reserved (currently passed as `None`); handlers that need identity call
  `self._resolve_ctx()` to get a `RequestContext` (or the uniform unauthorized dict).

## 4. Shared actions (implemented once on `PaletteCommand`)

These are available to **every** command without re-implementation:

| Action | Payload | Success response |
|---|---|---|
| `getAuthStatus` | `{}` | `{success, tenant, tenantSource, hasToken, userId, lastError}` |
| `getWorkspaceAccess` | `{systemName?}` | `{success, systemName, workspaceId?, hasAccess, failedOpen?}` |
| `getTheme` | `{}` | `{success, themeId, themeName}` (themeName ∈ classic/light-gray/dark-blue/dark-gray/device) |
| `getUiPrefs` | `{}` | `{success, prefs:{theme, textSize}}` |
| `setUiPrefs` | `{theme?, textSize?}` | `{success, prefs:{theme, textSize}}` (merged + persisted) |
| `openInBrowser` | `{url}` | `{success}` — opens an http(s) URL in the system browser |
| `openInFusion` | `{lineageUrn}` or `{workspaceId, itemId}` | `{success, message}` — opens/activates the Fusion document |
| `getItemDetail` | `{workspaceId, itemId}` | `{success, item, etag}` — `item.openInBrowserUrl` is added |

### Generic `getItemDetail` — no per-domain aliases

There is exactly one detail action: `getItemDetail`. The old add-in had
`getItemDetails` / `getTaskDetailFull` / per-workspace variants; 2.0 collapses these.
The frontend passes `workspaceId` + `itemId` (resolved from a capability's
`workspaceKey` on the Python side via entitlements) and reads back the item plus its
`openInBrowserUrl` deep link and `etag` (used for optimistic-concurrency updates).

## 5. Push messages (Python → JS)

| Action | Sent when | JS handler should |
|---|---|---|
| `tokenResult` | OAuth sign-in completes (custom event → `_on_token`) | refresh auth UI / reload data |

The page registers a handler:

```js
window.fusionJavaScriptHandler = {
  handle: function (action, data) {
    if (action === 'tokenResult') { /* refresh */ }
    return 'OK';
  }
};
```

## 6. Login palette actions (login command only)

These live on `commands/login/command.py` (the only command with `requires_auth=False`
and no panel button). They are NOT part of the shared set:

| Action | Payload | Success response |
|---|---|---|
| `getLoginInfo` | `{}` | `{success, tenant, status, hasToken, clientId}` |
| `startLogin` | `{staySignedIn}` | `{success, started, message}` — opens the browser to the APS authorize URL |
| `cancel` | `{}` | `{success}` — hides the login palette |
| `openFusionManageAdmin` | `{tenant?}` | `{success, opened}` or `{success:false, error}` |
| `pageReady` | `{}` | `{success}` — lets Python re-apply the palette size after layout |

## 7. WorkspaceCommand actions (Phase 1 — available on every WorkspaceCommand subclass)

`WorkspaceCommand` extends `PaletteCommand` and adds a second dispatch layer for
workspace-specific operations. All actions listed here are inherited by every
capability (Change Management, etc.) with no per-subclass re-implementation.

The WorkspaceCommand also overrides `getWorkspaceAccess` to attach admin-URL hints
(`urlGroups`, `urlTemplateLibrary`) that the engine uses to build the no-access view,
and overrides `getItemDetail` to add workflow transitions and an LRU detail cache.

### 7.1 Workspace metadata

| Action | Payload | Success response |
|---|---|---|
| `getTableaus` | `{}` | `{success, tableaus:[{id, title, type}]}` |
| `getTableauData` | `{tableauId, page, size}` | `{success, columns:[{id,typeTitle}], rows:[{itemId,workspaceId,fields}], total}` |
| `getViewFields` | `{viewId?}` | `{success, fields:[{id,title,type,editability,visibility,picklist,fieldValidators,displayOrder,derived,derivedFieldSource,formulaField,isSystemField}]}` |
| `getWorkspaceSections` | `{}` | `{success, sections:[{name,link,fields:[{id,link}]}], viewId?}` |
| `getLookupOptions` | `{lookupPath, limit?, offset?}` | `{success, items:[{link,title,version,deleted}]}` |

### 7.2 Item detail (WorkspaceCommand override of the shared action)

| Action | Payload | Success response |
|---|---|---|
| `getItemDetail` | `{workspaceId, itemId, skipCache?}` | `{success, item, etag, transitions:[{transitionID,shortName,description}], currentStep}` |

- Result is cached in an LRU dict (max 20 entries, per-instance).
- Transitions and item fields are fetched in parallel via `ThreadPoolExecutor`.
- `skipCache: true` in the payload forces a fresh fetch and evicts the entry.

### 7.3 Item mutation

| Action | Payload | Success response |
|---|---|---|
| `createItem` | `{mode:'create', fieldValues:{fieldId:value}, workspaceId?}` | `{success, itemId}` **or** `{success:false, missingFields:[label,…], error}` |
| `updateItem` | `{mode:'edit', itemId, etag, fieldValues:{fieldId:value}, workspaceId?}` | `{success}` **or** `{success:false, missingFields:[label,…], error}` — also evicts the LRU cache entry |

**`fieldValues`** is a flat map `{fieldId: rawValue}` of the fields JS wants to write. On **create** JS
sends every field the user entered; on **edit** JS sends only the fields it detected as *changed* (the
diff against the item's original values lives in JS). Raw value shapes as produced by the form widgets:
text/paragraph/rich-text → string; numeric → string (Python coerces); date → `"YYYY-MM-DD"`; checkbox →
bool; single-select / item-reference → `{link,title?,value?}`; multi-select → array of those. Draft-image
fields are resolved to their `dataUrl` string by JS before sending.

**Python is authoritative for the request-body shape.** The `createItem`/`updateItem` handlers gather field
metadata server-side (`/workspaces/{ws}/fields` for editability/derived/derivedFieldSource/formulaField/
validators; `/workspaces/{ws}/sections` for section membership + `classificationId` on create; the item
detail for section membership + `isSystemField` + `classificationId` on edit) and call
`services.item_payload.build_item_body` to produce the FM v3 `{sections:[{link, classificationId?,
fields:[{__self__, value}]}]}` body. That normalizer: excludes `isSystemField` / `editability==NEVER` /
`formulaField` (and `ON_CREATION`/`CREATE_ONLY` on edit); includes a derived field only when its
`derivedFieldSource` is also being written; coerces each value by field type; emits **minimal**
`{__self__, value}` per field (never `title`) with **workspace-scoped** links
(`/api/v3/workspaces/{ws}/views/{v}/fields/{id}` and `/api/v3/workspaces/{ws}/sections/{sid}`) for both
modes; and runs a **required pre-flight** — if a writable required field is blank it returns
`{success:false, missingFields:[…]}` (friendly labels) **before** any POST/PATCH. `updateItem` uses the
JS-supplied `etag` for the `If-Match` header (optimistic concurrency) and evicts the LRU detail cache.

### 7.4 Affected Items (Linked Items tab)

| Action | Payload | Success response |
|---|---|---|
| `getAffectedItems` | `{workspaceId, itemId}` | `{success, items:[…], totalCount}` |
| `addAffectedItems` | `{workspaceId, itemId, paths:[link,…]}` | `{success, results:[{result}]}` |
| `removeAffectedItem` | `{workspaceId, itemId, affectedItemId}` | `{success}` |

### 7.1a Optional `workspaceId` in workspace-action payloads

Every workspace action listed in §7.1–7.6 now accepts an **optional** `workspaceId`
field in its payload. When present and non-empty, it overrides the command's
default workspace (resolved via `workspace_system_name`). When absent the behaviour
is identical to before. This is implemented by the `_ws_from(data)` helper in
`WorkspaceCommand` and powers the Change Management drill-down from the unified view.

Affected actions: `getTableaus`, `getTableauData`, `getViewFields`,
`getWorkspaceSections`, `createItem`, `updateItem`, plus all actions that already
accepted an optional `workspaceId` (`getItemDetail`, `getAffectedItems`,
`addAffectedItems`, `removeAffectedItem`, `getItemTabs`, `getItemPermissions`,
`getItemsTabCounts`, `getTransitions`, `runWorkflowTransition`).

### 7.5 Detail tabs + permissions

| Action | Payload | Success response |
|---|---|---|
| `getItemTabs` | `{workspaceId, itemId}` | `{success, tabs:[{name,totalCount}]}` |
| `getItemPermissions` | `{workspaceId, itemId}` | `{success, permissions:[string]}` |
| `getItemsTabCounts` | `{itemIds:[…]}` | `{success, counts:{itemId:{tabName:count}}}` |

### 7.6 Workflow transitions

| Action | Payload | Success response |
|---|---|---|
| `getTransitions` | `{workspaceId, itemId}` | `{success, transitions:[…], currentStep}` |
| `runWorkflowTransition` | `{workspaceId, itemId, transitionId, currentStep, workflowComments?}` | `{success}` — also evicts the LRU cache |

### 7.7 Lineage search (component picker — Step 2)

| Action | Payload | Success response |
|---|---|---|
| `searchResultsForLineage` | `{urns:[urnTail,…], revision}` | `{success, items:[{title,link,itemId}]}` |

### 7.8 Fusion CAD context (component picker — Step 1)

| Action | Payload | Success response |
|---|---|---|
| `getSelectedComponents` | `{}` | `{success, components:[{name,fileId,…}]}` |
| `getRootComponents` | `{}` | `{success, components:[{name,fileId,…}]}` |
| `getOpenDrawings` | `{}` | `{success, drawings:[{name,fileId,…}]}` |

## 8. Change Management — unified multi-workspace actions

These actions are specific to `ChangeManagementCommand` (not inherited by other
workspace capabilities). They power the scope-selector and unified table in the
Change Management palette.

### 8.1 Scope discovery

| Action | Payload | Success response |
|---|---|---|
| `getChangeScopes` | `{}` | `{success, scopes: [{key, label, workspaceId?, systemName?}]}` |

- First scope always has `key:'all'`, `label:'All change records'` — the unified default.
- Subsequent scopes represent each entitled change workspace discovered by the
  `CHANGE_SYSTEM_NAME_BASES` discovery rule (WS_CHANGE_ORDERS, WS_LEAN_CHANGE_ORDERS,
  WS_CHANGE_REQUESTS, WS_CHANGE_TASKS, WS_PROBLEM_REPORTS, plus `BASE_<digits>` variants).
- `workspaceId` and `systemName` are present on all non-`all` scopes.

### 8.2 Unified record list

| Action | Payload | Success response |
|---|---|---|
| `getUnifiedChangeRecords` | `{sort?}` | `{success, rows:[...], totalCount, columns:[{id,label}], truncated}` |

- Payload `sort`: optional field id to sort by (`'descriptor'` default, or `'type'`,
  `'recordNumber'`, `'owner'`).
- Each row: `{type, recordNumber, title, descriptor, owner, itemId, workspaceId}`.
  - `type` = the workspace's **title** string (from `_discover_change_workspaces`,
    i.e. `sn_to_title[systemName]` or humanized systemName). Never `workspaceShortName`
    (that field is an app-store blurb on some tenants, not a human workspace name).
  - `descriptor` = raw FM descriptor string (e.g. `"PR-000035 - Danger hazard…"`).
  - `number` / `title` = descriptor split on first `' - '`.
  - `owner` = plain display name string (resolved from owner str-or-dict by
    `_person_name()` helper in `services/items.py`).
  - No workflow state: that field is not available on the bulk `/items` endpoint —
    it is shown in drill-down detail view, which is the correct UX.
- `columns`: `[{id:'type',label:'Type'},{id:'descriptor',label:'Record'},{id:'owner',label:'Owner'}]`.
- `truncated: true` when any workspace hit the per-workspace cap (200 records).
  A warning is logged for each capped workspace (no silent truncation).
- Fetches every discovered change workspace in parallel
  (`ThreadPoolExecutor`, `max_workers=min(N,8)`, `EXECUTOR_TIMEOUT_SECONDS` timeout).
- Workspace fetch errors are logged and that workspace is skipped (partial results
  rather than a hard failure); `truncated` is set.

## 9. Change Management — lazy enrichment (Status + Affected)

### 9.1 getRecordsEnrichment

| Action | Payload | Success response |
|---|---|---|
| `getRecordsEnrichment` | `{items: [{workspaceId, itemId}, ...]}` | `{success, enrichment: {"<wsId>:<itemId>": {state, affectedCount}, ...}}` |

- Defined as a generic `@action` on `WorkspaceCommand` (available to any capability,
  not just Change Management).
- **Payload `items`**: array of `{workspaceId, itemId}` pairs for the CURRENT visible
  page.  Server-side cap of 80 items; excess are logged and truncated.
- **Parallel fetch**: `ThreadPoolExecutor(max_workers=8)`, per-future
  `EXECUTOR_TIMEOUT_SECONDS` timeout, `shutdown(wait=False, cancel_futures=True)` on
  completion — mirrors the executor discipline in `getUnifiedChangeRecords`.
- **`state`**: fetched via `FmClient.item_detail(ws, item)` → `currentState.title`
  (falls back to the raw `currentState` string if it's not a dict, else `''`).
- **`affectedCount`**: fetched via `FmClient.item_tabs(ws, item)` → `LINKEDITEMS`
  tab `totalCount` (same data path as `getItemsTabCounts`). Returns `0` if the tab
  is absent or the fetch fails.
- **Per-item errors are silently omitted**: one slow/failing item never fails the call.
  The item's key is simply absent from `enrichment` (the JS treats absent keys as
  not-yet-loaded and leaves the placeholder visible — harmless).
- **Response key**: `"<workspaceId>:<itemId>"` (colon-separated string).
- Unauthorized errors propagate normally (the `_resolve_ctx()` guard).

### 9.2 Enrichment caching and stale-render guard (frontend)

- `_enrichCache` is a module-level JS object keyed `"<wsId>:<itemId>"` that persists
  for the session.  Paging back/forward within the unified table re-applies cached
  values immediately (no re-fetch).
- The cache is **cleared** on two events:
  1. `_applyScopeUI`: the user switches to a different scope (or back to 'all').
  2. `loadUnifiedView`: a full unified refresh (Refresh button or first load).
- **Stale-render guard**: `loadUnifiedView` captures `_listLoadSeq` as `myToken` and
  passes it to `renderUnifiedTable(myToken)` → `_enrichUnifiedRows(myToken)` →
  `_applyEnrichmentToPage(myToken)`.  Each function checks `myToken !== _listLoadSeq`
  before writing to the DOM, so a `getRecordsEnrichment` response that arrives after
  a scope switch is dropped silently.

## 9.3 PLM Charts (dashboard) — `commands/plm_charts/`

Custom `PaletteCommand` on the PLM panel (modeled on My Work; own frontend, not engine.js).
Both actions are `async_=True` (HTTP over the legacy REST v1 reporting API, `send_tenant=True`).

| Action | Payload | Success response |
|---|---|---|
| `getDashboards` | `{}` | `{success, reports:[{id, position, link}]}` (ordered by position) |
| `getChart` | `{id}` | `{success, chart}` |

`chart` is normalized server-side (`services/reports.py`) into a render-ready shape:
```
{ id, name, description, type,           // COLUMN|BAR|STACKEDCOLUMN|LINE|AREA|MSAREA|PIE|DOUGHNUT|…
  title, xLabel, yLabel, seriesLabel,
  categories:[<x label>,…],
  series:[ {name:<series label>, points:[ {x:<label>, y:<number>} ]} ],
  hasData:<bool> }
```
Raw source: `GET /api/rest/v1/reports/dashboard` (`dashboardReportList.list`) and
`GET /api/rest/v1/reports/{id}/chart.json` (`reportDefinition.reportChart` + `reportResult`
+ `xAxisColumn`/`yAxisColumn`). The series column is the `columnKey` that is neither x nor y
(stacked/multi-series charts). Rendering is inline SVG in `resources/html/static/plm_charts.js`.

## 9.4 Export Electronics BOM (v1) — `commands/export_electronics_bom/`

Custom `PaletteCommand` on the Electronics environment panel (`panel='electronics'`,
`requires_auth=False` for v1). One **SYNC** action (reads `adsk.electron` on the main thread —
must NOT be async).

| Action | Payload | Success response |
|---|---|---|
| `getElectronicsBom` | `{}` | `{success, design, rows, partCount, skipped, warnings}` |

`rows` are grouped + FM-export-ready, one per unique `(value, footprint, MPN, manufacturer)`:
```
{ mpn, manufacturer, value, footprint, quantity, referenceDesignators:[…], raw:{…} }
```
Extraction (`services/electronics_bom.py`) reads `Schematic.parts` (or `board.linkedSchematic`),
skips parts with no device package / no linked 3D-footprint name / not-populated-for-variant,
reads MPN + MF from `Part.attributes` (case-insensitive). v1 = read & display + export-prep
preview only; no FM writes.

## 10. Auth gating (uniform, base-owned)

When a command with `requires_auth = True` is invoked and there is no valid token,
`PaletteCommand._on_command_execute` shows the **login palette** instead of the
command's own palette. Commands never re-implement this. The login command itself
sets `requires_auth = False`.
