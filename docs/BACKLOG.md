# PLM Workspaces 2.0 — Backlog

> Living document. Updated at the end of every work session.
> Last updated: 2026-07-08

---

## Shipped

### Infrastructure
- **Async dispatcher** (`core/async_dispatcher.py`) — all HTTP-bound `@action` handlers
  run on a shared 4-worker `ThreadPoolExecutor`; results delivered to the webview via a
  Fusion custom event on the main thread. 20 actions marked `async_=True`. Fusion main
  thread is never blocked by HTTP. `bridge.js` handles `{pending, requestId}` ack and
  routes `plmAsyncResult` push transparently — callers of `plmSend` see no change.
- **Unified `@action` registry** + `PaletteCommand` base — auth gating, dispatch, palette
  lifecycle, theme, prefs defined once and inherited.
- **`WorkspaceCommand` base** — list/detail/form/workflow shared by all workspace capabilities.
- **`FmClient`** with single 401-refresh-retry.
- **`bridge.js`** / `engine.js` / `theme.js` / `tokens.css` / `components.css` —
  one set of frontend files shared by all workspace palettes.
- **Persistent handler fix** — `commandCreated`, `incomingFromHTML`, and OAuth event
  handlers are on `_persistent_handlers` (survive button command destroy).
- **`_whenAdskReady()` bridge polling** — waits for `adsk.fusionSendData` injection before
  sending; prevents silent dropped calls on first open.
- **Rich HTML field rendering** — detects both raw tags and `&lt;`-escaped HTML.

### Capabilities shipped
| Capability | Type | Notes |
|---|---|---|
| **Login / Auth** | PaletteCommand (special) | PKCE sign-in, stay-signed-in, auto-open after sign-in, admin links |
| **Change Management** | WorkspaceCommand (multi-workspace) | Spans CO, LCO, CR, CT, PR + `_N` duplicates. Unified "All" table with lazy Status/Affected pills. Scope selector with drill-down. Dynamic New button label. |
| **Requirements** | WorkspaceCommand | WS_REQUIREMENTS (ws 245). Thin subclass; full list/detail/form/workflow via shared engine. |
| **Engineering Projects** | WorkspaceCommand | WS_ENGINEERING_PROJECTS (ws 322). Same. |
| **Supplier Packages** | WorkspaceCommand | WS_SUPPLIER_PACKAGES (ws 209). Same. |
| **My Work** | PaletteCommand (custom) | Outstanding work queue via `/api/v3/users/@me/outstanding-work`. Grouped by workspace, color-coded due dates (red/orange/yellow). Click → open in browser. |
| **Export G-code to PLM** | PaletteCommand (custom, panel='cam') | 3-step wizard in Manufacture workspace CAM Manage panel. Step 1: list CAM setups + scan `.cps` post processors, run `cam.postProcess()` (sync — main thread). Step 2: search CW_COMPONENTS in FM. Step 3: upload NC files via request_upload → S3 PUT → checkin. |

---

## Known Issues / Deferred Fixes

### High priority (deferred from active work)

**[FIXED — verified live] Universal create/edit record body shape**
Create/edit was failing (messy forms + rejected/incomplete API bodies). Reworked so Python
owns the FM v3 request-body shape, mirroring the Chrome extension's validated CLONE_FIELD_RULES.
New `services/item_payload.py` normalizer: excludes system/`NEVER`/formula fields, applies the
derived-field dependency rule, coerces values per type, and runs a required pre-flight
(`missingFields`). `createItem`/`updateItem` now take a flat `fieldValues` map (contract §7.3);
`workspace_fields` returns `derived`/`derivedFieldSource`/`formulaField`/`isSystemField`;
`sections` returns `classificationId`. Frontend `buildFormField` gained checkbox / float-money /
rich-text / single item-reference widgets and an edit-mode label fix.
Two bugs found only via live testing (FAA Sandbox autodesk8937): (1) edit needs the item's own
**item-scoped** section/field links verbatim — a reconstructed workspace-scoped section link is
rejected ("Could not find section N in workspace W"); create uses constructed workspace/view-scoped
links. (2) create's new itemId comes from the **`Location` response header** (201 empty body), now
captured via `Result.location`. Verified: create → item 17789, edit → 2xx (persisted), date write
`YYYY-MM-DD` accepted. Known limit: required *dropdown-selection* fields use a `dropDownSelection`
validator (not `required`) so client pre-flight won't pre-warn — server rejects with a clear message.
Verify probe: `docs/dev/api_probe_create.py`.

**[FIXED] New record creation flow — itemId null fallback**
After `createItem` succeeds, `d.itemId || currentItemId` was falling back to the last
opened item's ID if the server didn't return an `itemId`. Now for `formMode === 'create'`
a null `itemId` shows a clear error ("Item created but server did not return an ID") rather
than silently navigating to the wrong record. Edit mode still uses the fallback correctly.
workspaceId routing for scoped/unscoped create was verified correct; Cancel and
required-field validation (`validatorName === 'required'`) both confirmed correct.

**[FIXED] Affected Items tab view ID**
`getAffectedItems` and `removeAffectedItem` were hardcoding view_id=11 for the FM API
URLs. The actual view ID lives in the LINKEDITEMS tab's `__self__` link returned by
`getItemTabs`. Engine.js now extracts it from `currentItemTabs` and passes it to both
`getAffectedItems` and `removeAffectedItem`. `buildAffectedItemCard` takes `parentViewId`
and includes it in the remove payload. Fallback is still 11 if no tab link is found.

**[FIXED] Affected Items returned empty — wrong response key**
`affected_items()` in `services/items.py` was reading `data.get('items')` but the FM v3
API (with `Accept: application/json`) returns the array under the key `"affectedItems"`.
The paginated format with `"items"` only appears when the browser sends a vendor Accept
header. Fix: read `affectedItems` first, fall back to `items`. `totalCount` is now
derived from `len(items)` when the non-paginated format is returned.

### Medium priority

**[FIXED] Panel button order instability after sign-in**
`sync_plm_panel_buttons` now builds `order_to_add` by iterating `config.COMMAND_IDS`
insertion order (canonical order: My Work → Change Management → Requirements →
Engineering Projects → Supplier Packages) rather than the FM API entitlement sequence.

**[FIXED] Problem Reports drill-down shows no views**
`loadListView` now hides `#viewSelector` and `#btnNew` and shows a clear "No list views
configured" message when `getTableaus` returns an empty list for the drilled-down scope.

**[REFINEMENT] Requirements / Eng Projects / Supplier Packages — needs Fusion testing**
All three capabilities were built as thin `WorkspaceCommand` subclasses but have not yet
been tested in Fusion. Expected to work out of the box; validate list loads, detail opens,
create/edit form, workflow transitions. Test in FAA Sandbox after reloading the add-in.

**[VERIFY] My Work — full feature port (needs Fusion testing)**
My Work was fully ported (2026-06-24). Now has: search, quick-filter chips
(All/Overdue/Due Soon/High Priority), workspace + date-preset filters, sort/group
(dueDate/workspace/dueDateBucket/priority/state), summary strip, last-updated ticker,
auto-refresh, multi-select + bulk transitions, preview panel (with visibleOnPreview
workspace fields + resize), full detail view (collapsible sections, breadcrumb drill-down,
workflow transitions), and CAD-context matching (lineage URN → relatedToCurrentModel badge).
New Python actions: getLineageUrns (sync), getWorkspaceFields (async). MyWorkCommand now
extends WorkspaceCommand. Needs manual verification in Fusion FAA Sandbox.

### Low priority

**[REFINEMENT] Detail view — parallel sub-fetches not yet done**
`getItemDetail` runs item_detail + transitions in parallel (already done). But `getViewFields`
and `getWorkspaceSections` are fetched separately by the JS in sequential `plmSend` calls.
Could batch them: one Python action that returns item + fields + sections + transitions together.

**[REFINEMENT] Enrichment batch size cap (80 items)**
`getRecordsEnrichment` is capped at 80 items. For large unified tables (200+ rows from 5
workspaces) the remaining rows never get enriched. Options: (a) raise cap, (b) enrich in
multiple batches client-side, (c) enrich only the visible page.

**[REFINEMENT] `getWorkspaceAccess` still sync**
`getWorkspaceAccess` is the first call on palette open. It calls `entitlements.get_entitled_workspace_ids`
which may trigger an FM API call on the first open. Mark it `async_=True` once the entitlements
module is confirmed safe to call from a background thread (check `get_fusion_user_id` usage).

---

## Upcoming Features (Phase 4)

**Export Electronics BOM to PLM** (`commands/export_electronics_bom/`)

Full spec at: `C:\Users\quadeg\AppData\Roaming\Autodesk\Autodesk Fusion 360\API\AddIns\PLM Workspaces\specifications\capabilities\EXPORT_ELECTRONICS_BOM_TO_PLM_SPECIFICATION.md`

Two-phase delivery (same pattern as Export to PLM):

*Phase 1 — Read & display (no writes):*
- Reads the active Fusion Electronics design BOM (PCB or Schematic workspace)
- Normalises rows to `{componentUrn, mpn, manufacturer, partNumber, value, footprint, description, quantity, referenceDesignators[], raw}`
- One BOM row per unique MPN; quantity rolled up; designators concatenated
- Renders a structured table in the palette plus a raw diagnostic section
- No FM writes in Phase 1 — validates real API schema before committing Phase 2

*Phase 2 — Resolve, preview, push (writes):*
- Resolves each MPN line against the electronics FM workspace: **Component URN first → MPN fallback → create new**
- Lines with neither URN nor MPN: warn + let user supply MPN inline or skip
- Preview step shows plan (N to update, M to create, P BOM rows, W warnings) before any write
- Push order: update existing items → create new items → create/update parent PCBA item → build BOM rows on parent
- One parent PCBA item per design variant; shared child component items across variants where MPN matches
- Per-item result reporting; one failure does not abort the batch

*V2 architecture notes (V1 spec uses old patterns — translate as follows):*
- `lib/electronics_bom.py` → `services/electronics_bom.py`
- `lib/fusion_manage_api.py` BOM helpers → new functions in `services/items.py` or a new `services/bom.py`
- `lib/ui_utils.py` electronics panel helpers → new entry in `core/panels.py` (add Electronics environments to `_PANEL_WORKSPACE_IDS` or a separate helper, same discipline as `add_command_to_cam_manage_panel`)
- Command follows `PaletteCommand` base; actions use `@action` + `async_=True` for all FM network calls
- Config: add `PALETTE_ID_EXPORT_ELECTRONICS_BOM`, `COMMAND_IDS['exportElectronicsBomToPlm']`, `WORKSPACE_SYSTEM_NAMES['electronicsBom']`, `WORKSPACE_IDS['electronicsBom']`
- Button placement: PCB editor + Schematic editor — exact environment IDs to be confirmed via diagnostic in Phase 1 (`FusionElectronicsPcbEnvironment`, `FusionElectronicsSchematicEnvironment` are candidates)

*Open items — product owner inputs needed before Phase 2 can be spec'd:*
1. **Electronics BOM export API** — Fusion Python API endpoint, auth model, request/response schema
2. **Electronics FM workspace(s)** — systemName + numeric fallback ID; one workspace or two (parent PCBA vs component items)
3. **FM field systemNames** — fields holding component URN, MPN, manufacturer, internal part number, reference designators, quantity
4. **FM BOM REST API** — read/add/update/remove row endpoints and payload shape; check-out/revision requirements; whether children are referenced by URN or item id
5. **Variant model** — does the export expose variants; how are they keyed
6. **Parent item identity** — how a design maps to its parent PCBA item across re-exports (stable design URN?)

Status: **Phase 2 shipped + verified live (2026-07). Full spec: `docs/EXPORT_ELECTRONICS_BOM_SPEC.md`.**
Wizard (Extract → Configure → Preview → Push) pushes the BOM into WS_ITEMS: creates/updates
component Items, creates MPN records (WS_MANUFACTURER_PN) linked from the MPN side via
`REFERENCE_ITEM`, resolves suppliers (WS_SUPPLIERS by NAME), finds-or-creates the parent PCBA
Item by `SOURCE_DESIGN_ID`, and full-syncs the BOM. Optional "sync design files" attaches the
zipped EAGLE `.sch`/`.brd`. Verified live: item/MPN/supplier create+link, BOM add/qty,
reference-designator row write, description, file upload.

**Known limitations / bugs (Electronics BOM export):**
- `Item.REFERENCE_MPN` populates via the tenant sync job, not immediately after push (canonical
  link is `MPN.REFERENCE_ITEM`, which we set). Cosmetic only.
- MPN match uses a scoped field-equals query with a full-text fallback; watch precision on large
  MPN workspaces (could false-match). Verify against real data.
- Parent identity is keyed on the design name/id (`SOURCE_DESIGN_ID`) until a stable design
  lineage URN is available from the electronics API — renaming a design creates a new parent.
- `refDesField` is stored as a workspace-specific viewdef field link; re-pick in Configure if the
  tenant changes.
- Reference designators are written to a single BOM-row field (comma-joined); no per-designator rows.

**Enhancements (Electronics BOM export):**
- Variant awareness (one parent per variant; shared children across variants).
- Per-line match override in Preview (reuse export_to_plm inline search).
- CSV fallback export of the mapped BOM.
- Write richer sourcing fields onto the MPN record (datasheet/URL/cost) from part attributes.
- Stable parent identity via a design lineage URN when the preview API exposes one.

---

**[BUILT 2026-07-08] PLM Charts dashboard** (`commands/plm_charts/`)
Renders the tenant's Fusion Manage report dashboard as inline-SVG charts inside a PLM-panel palette.
`services/reports.py` calls `GET /api/rest/v1/reports/dashboard` + `/reports/{id}/chart.json`
(legacy REST v1, `X-Tenant`) and normalizes each report to a render-ready {type, categories, series}
shape; `plm_charts.js` draws COLUMN/BAR/STACKEDCOLUMN/LINE/AREA/PIE/DOUGHNUT with no chart lib.
Normalization verified offline against live API captures; SVG visuals need an in-Fusion check.
Status: **built — needs in-Fusion smoke test.**

---

**Design Review** (`commands/design_review/`)
- Screenshot current Fusion viewport
- Canvas markup (annotations)
- Save markup to a Design Review workspace record in FM
- Command in the CAM/Manufacture panel (or PLM panel)
- Re-use `services/attachments.py` + `services/fusion_cad.py`
- Status: not started

**[SHIPPED] Export PDF to PLM** (`commands/export_pdf/`)
- Export active Fusion 360 drawing as PDF via `doc.exportManager`
- Auto-detect linked design's lineage URN from drawing views → PLM component auto-match
- Fixed resource name → auto version-bump on re-export
- Upload comment support; per-revision "Open in PLM" button
- Status: shipped

**[SHIPPED] Export DXF to PLM** (`commands/export_dxf/`)
- Flat pattern export (sheet metal) via `comp.flatPattern` + `createDXFFlatPatternExportOptions`
- Sketch export via `createDXF2DExportOptions`; all sketches regardless of visibility
- Scale + grain direction options; Individual or Single file mode
- Attachment mapping table (overwrite existing or create new per file)
- Status: shipped

**[SHIPPED] Export to PLM** (`commands/export_to_plm/`)
- Unified STEP + Drawing PDF export; appears in Design and Drawing workspace PLM panels
- Two-phase: Phase 1 async PLM batch search with match table (component + last-export date);
  Phase 2 sync STEP/PDF export + async upload
- All unique components deduplicated; external → own PLM item; internal → nearest external ancestor
- Full-assembly STEP + per-component STEP (three API approaches probed at runtime)
- Inline match override: "⟳ Change" button per row opens inline PLM search panel
- Native Fusion `ProgressDialog` + `adsk.doEvents()` between exports for UI responsiveness
- Status: shipped — individual component STEP export API being validated in testing

**[SHIPPED] Export G-code to PLM** (`commands/export_gcode/`)
- NC post-process from CAM environment; auto-detects component via lineage URN + MFGDM part descriptor fallback
- Attach multi-file output to a `CW_COMPONENTS` record in FM; auto version-bump
- Upload comment; per-revision "Open in PLM" button; 2-step progress bar
- Status: shipped

---

## Architecture / Tech Debt

**`_person_name()` helper scope**
Currently in `services/items.py`. If other service modules need it, promote to `services/fm_client.py`
or a shared `services/_utils.py`.

**`VERIFICATION.md` needs Phase 4+ sections**
Add manual test sections for async behavior, the new capabilities (Requirements, Eng Projects,
Supplier Packages, My Work), and the async dispatcher once Fusion testing is done.

**`docs/ARCHITECTURE.md` and `docs/IMPLEMENTATION_PLAN.md` status banners**
Still say "PROPOSED — awaiting approval." Update to reflect shipped state.
