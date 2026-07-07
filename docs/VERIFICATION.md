# Manual Verification Checklist — run inside Fusion

> Fusion add-ins require the running Fusion GUI process and the `adsk.*` modules, so
> there is **no headless test harness**. Static `py_compile` passing only means the
> syntax is valid — it does **not** mean the add-in runs. Everything below must be
> executed manually in Fusion. This file grows one section per phase.

## Phase 0 — Backbone + login smoke command

Prerequisite: the old **"PLM Workspaces"** add-in remains installed (we test
coexistence). Both folders live under `…/API/AddIns/`.

### A. Load / unload
- [ ] In Fusion: **Utilities → Add-Ins → Scripts and Add-Ins**, select
      **"PLM Workspaces 2.0"**, click **Run**. It loads with **no error dialog** and
      nothing is printed to the Text Command window at Error level.
- [ ] **Stop** the add-in, then **Run** it again — it re-loads cleanly (no "custom
      event already registered" or "command id in use" errors). Repeat once more.

### B. Coexistence with the old add-in (no UI collision)
- [ ] With BOTH add-ins running, open the **Design** workspace → **Manage** tab.
      The old "PLM Workspaces" panel/buttons and the new ones do **not** collide or
      throw (distinct `_V2` ids). (Phase 0 ships no capability buttons, so expect to
      see only the old add-in's buttons plus, after sign-in, none-yet from 2.0 —
      this is fine; the check is "no errors / no overwritten controls".)
- [ ] Confirm the new add-in created **no** duplicate panel with the old add-in's id.

### C. Login palette opens
- [ ] Because Phase 0 has no capability buttons, trigger the login palette directly:
      run the **"Sign in to PLM"** command from **Scripts and Add-Ins → Add-Ins**
      list is not applicable; instead temporarily invoke it via the Text Command, or
      verify via Phase-1+ once a gated command exists. (Phase 0 acceptance: the
      command definition exists and `LoginCommand.show()` displays the palette —
      confirm by calling it from a later gated command, or trust the next checks
      which exercise the same `_show_palette` path.)
- [ ] The login palette renders: Autodesk logo, **Account** card (status dot +
      tenant), **Before signing in** card (Client ID + Copy + Open Admin), **Sign in**
      card (Stay signed in checkbox + buttons). No broken image, no unstyled flash.
- [ ] The **Client ID** shown matches the value in `core/config.py` (`APS_CLIENT_ID`).
- [ ] **Tenant** shows the active hub's tenant when a cloud document/hub is open,
      else the "(open a cloud document…)" hint.

### D. Cold sign-in (the end-to-end smoke test)
- [ ] Sign OUT / clear tokens first (delete `tokens.json` from the stable add-in root
      if present — see §G for the path).
- [ ] Click **Sign in with Autodesk** with **Stay signed in** checked. The system
      browser opens the APS authorize page.
- [ ] Complete sign-in. The browser shows the branded **"Signed in successfully"**
      page (Autodesk logo + green check).
- [ ] Back in Fusion, the login palette updates to **Signed in** (status dot green)
      without a manual reload (driven by the `tokenResult` push).

### E. Token refresh
- [ ] After a successful sign-in, leave Fusion idle long enough for the access token
      to approach expiry (or shorten by editing `tokens.json`'s `expires_at` to a
      near-now value). Trigger any FM call (see §F). It succeeds — confirming
      `core/auth.get_valid_access_token()` silently refreshed via the refresh token.
- [ ] Verify the **single 401-refresh-retry** path: with a deliberately stale
      `access_token` (but valid `refresh_token`) in `tokens.json`, an `FmClient` call
      returns data (one transparent retry), not an `unauthorized` error.

### F. `FmClient.outstanding_work()` returns data
- [ ] With a signed-in session and an active hub/tenant, call
      `FmClient.outstanding_work()` (e.g. from the Text Command or a temporary probe)
      and confirm it returns `{'success': True, 'tasks': [...], 'count': N}` matching
      the user's "My Outstanding Work" in the Fusion Manage web UI.
- [ ] Confirm `FmClient.workspaces()` returns a non-empty `workspace_ids` set and a
      `system_name_to_id` map (this also powers entitlement gating in later phases).

### G. Stay-signed-in persists across restart
- [ ] With **Stay signed in** checked and signed in, fully **quit and relaunch
      Fusion**. Run the 2.0 add-in. Without signing in again, an FM call (§F)
      succeeds — the token was loaded from disk and refreshed as needed.
- [ ] Uncheck **Stay signed in** (or sign out): `tokens.json` is removed from both
      the stable path and the add-in path, and after restart an FM call reports
      `unauthorized` / shows the login palette.
- [ ] Token/prefs file locations (Windows):
      `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\PLM Workspaces 2.0\`
      → `tokens.json`, `prefs.json`, `ui_prefs.json` (mirrored in the live add-in
      root). Confirm `tokens.json` is `0600`-ish (user-only) and contains
      `access_token`, `expires_at`, `refresh_token`.

### H. UI prefs round-trip (shared actions)
- [ ] `setUiPrefs {theme:'dark'}` then `getUiPrefs` returns `theme:'dark'`; the value
      survives a palette close/reopen (persisted to `ui_prefs.json`).
- [ ] `getTheme` returns the current Fusion color theme id/name.

### H2. No cross-add-in custom-event leak (old add-in must NOT receive the 2.0 token)
> Both add-ins listen on a process-global custom event for their OAuth token. The IDs
> differ (`…_oauth_token_V2` for 2.0 vs the old add-in's id), but the old add-in
> attaches a handler to `app.customEventFired` and filters only on payload presence,
> so a misconfigured/shared id could let it receive the 2.0 token. Verify there is no
> cross-talk.
- [ ] With BOTH the old "PLM Workspaces" and "PLM Workspaces 2.0" add-ins running,
      perform a fresh sign-in **in the 2.0 login palette** (§D).
- [ ] **Expected result:** ONLY the 2.0 add-in reacts — its login palette flips to
      **Signed in** and 2.0 panel buttons sync. The OLD add-in must show **no**
      reaction: it does not flip to signed-in, does not open/refresh its palettes, and
      logs nothing indicating it received a token. (The 2.0 event id is
      `…_oauth_token_V2`, registered once at add-in scope; the old add-in uses a
      different id, so its `app.customEventFired` handler must not fire for the 2.0
      token broadcast.)
- [ ] Repeat the reverse: sign in via the OLD add-in and confirm the 2.0 add-in does
      **not** pick up the old add-in's token.
- [ ] **If it leaks** (the old add-in reacts to the 2.0 token, or vice-versa): the two
      add-ins are sharing a custom-event id or one listens on the global
      `customEventFired` without filtering by id. Fix by ensuring 2.0 keeps its unique
      `OAUTH_CUSTOM_EVENT_ID` (the `_V2` suffix in `core/config.py`) and subscribes its
      handler to that specific registered `CustomEvent` object (done in
      `commands/__init__.py` + `core/palette_base.py`), never to the global
      `customEventFired`. Do not change the old add-in; the 2.0 add-in owns the
      isolation.

### I. Reviewer sign-off (static)
- [ ] No duplicated request/dispatch/auth-gate logic remains in any command module —
      all of it lives in `core/http_client.py`, `services/fm_client.py`, and
      `core/palette_base.py`.
- [ ] `py_compile` passes for every `.py` file (run by the implementer; see report).

### Known Phase-0 limitations (expected, not failures)
- No capability buttons appear on panels yet (login has no panel button by design;
  capability commands arrive in Phase 2+). The login palette is reached via the
  shared auth gate once a gated command exists.
- The login frontend is a self-contained Phase-0 page; the shared `web/core/*`
  frontend (bridge/engine/theme/tokens) lands in Phase 1.

---

## Phase 1 — Shared frontend engine + Change Management

Prerequisite: Phase-0 passes. Both "PLM Workspaces" (old) and "PLM Workspaces 2.0"
(new) are installed and the old add-in continues to run without interference.

### A. Static checks (before loading in Fusion)
- [ ] `py_compile` passes on every new/modified `.py`:
      `core/workspace_command.py`, `commands/change_management/command.py`,
      `commands/change_management/__init__.py`, `commands/__init__.py`.
- [ ] `web/core/tokens.css`, `components.css`, `bridge.js`, `theme.js`, `engine.js`
      all exist and are non-empty. (`picker.html` was removed — picker markup is now an
      inline template inside `engine.js _pickerInlineHtml()`; no separate file.)
- [ ] `web/capabilities/change_management/config.js` sets `window._plmCfg` with
      `workspaceKey:'changeManagement'`, `showAffectedItems:true`,
      `detailAction:'getItemDetail'`.
- [ ] `commands/change_management/resources/` contains `16x16.png`, `32x32.png`,
      `64x64.png` (copied from old add-in), and `html/index.html`.
- [ ] `index.html` loads assets in the exact order: `tokens.css`, `components.css`,
      `bridge.js`, `theme.js`, `config.js`, `engine.js`.

### B. Add-in loads with Change Management command
- [ ] **Run** PLM Workspaces 2.0. No error dialog. No Error-level Text Command output.
- [ ] The **Manage** tab in Design workspace shows a **"Change Management"** button.
      (Button icon is the 64×64 PNG from resources/.)
- [ ] Without signing in, click the Change Management button. The **login palette**
      appears (auth gate from `PaletteCommand._on_command_execute`). Sign in.
- [ ] After sign-in, click Change Management again. The Change Management palette opens
      (700 × 600, docked left).

### C. No-access path
- [ ] Sign in as a user who does NOT have the `WS_CHANGE_ORDERS` entitlement. Click
      Change Management. The palette shows the **"Access Required"** no-access card
      (lock icon, explanation text, two admin-link buttons). The buttons (`#btnManageAccess`,
      `#btnTemplateLibrary`, class `admin-btn`) are declared in `index.html` and wired
      by `plmShowNoAccess()` in `bridge.js`.
- [ ] The two admin-link buttons open the Fusion Manage Groups admin page and the
      Template Library page for the correct tenant in the system browser.

### D. List view — views, data, pagination
- [ ] After sign-in (entitled user): the list view loads. The **view selector** is
      populated with the workspace's tableaus; the DEFAULT tableau is pre-selected.
- [ ] Rows appear. Each row is clickable (opens detail).
- [ ] Changing the view selector reloads the table to the selected tableau. The choice
      persists across palette close/reopen (localStorage key `cm_tableau_id`).
- [ ] With ≥ 50 records: **Prev / Next** buttons paginate correctly; page info updates.

### E. Affected Items count column
- [ ] The first column in the list view is the **"Affected"** pill column.
- [ ] After the table renders, `getItemsTabCounts` is called in the background and the
      pills update from `…` to a number (or `—` if the count is 0).
- [ ] Clicking a pill (not the row) opens the detail view directly on the **Affected
      Items tab** (`_pendingInitialTab = 'LINKEDITEMS'`).

### F. Detail view
- [ ] Clicking a row opens the detail view. The breadcrumb shows
      `Change Management › <descriptor>`.
- [ ] Field sections render (section titles collapse/expand on click).
- [ ] **Edit** button is present; **Open in Browser** opens the FM web UI in the
      system browser.
- [ ] Workflow **transition buttons** are shown when the item has available transitions.
      Clicking one calls `runWorkflowTransition`; on success a green banner appears
      and the detail reloads.
- [ ] The **Affected Items** tab appears (count from `getItemTabs`). Switching to it
      calls `getAffectedItems` and renders the affected item cards.
- [ ] **Add Affected Items** button opens the component picker.

### G. Create / Edit form
- [ ] **+ New Change Order** button opens the form view with empty fields and the
      correct breadcrumb.
- [ ] **Edit** from the detail view pre-populates fields with the current item values.
- [ ] Required-field validation fires before save (missing required fields show a
      red message; no API call is made).
- [ ] A successful **Save** navigates to the detail view of the created/updated item.
- [ ] **Cancel** returns to the correct previous view (list or detail).

### H. Component picker (Shared Items field + Affected Items)
- [ ] From the Edit form on a field of type `SHARED_ITEMS`: clicking **"Add from
      Active Selection"** opens the picker with the **Active Selection tab** active.
- [ ] **"Browse Fusion Components"** opens the picker on the **Browse Components tab**.
- [ ] The picker fetches `getSelectedComponents` / `getRootComponents` and renders the
      component list.
- [ ] **Search Fusion Manage** calls `searchResultsForLineage` and shows FM results.
      Click-to-select, Ctrl-click, and Shift-click multi-select all work.
- [ ] **Add Selected** / **Add All** add items to the field's multi-select chip list
      and close the picker.
- [ ] **Components / Drawings** mode-switch: switching to Drawings hides the
      Active Selection tab and calls `getOpenDrawings`.
- [ ] **Close** / **Cancel** dismisses the picker without side-effects.

### I. Theme + text-size prefs
- [ ] The settings gear (⚙) is visible in every palette header (list, detail, form).
- [ ] Switching **Dark** applies `body.theme-applied-dark`; the palette re-themes
      without a reload. **Auto** follows the Fusion theme.
- [ ] Text-size buttons (S / M / L / XL) change the body class immediately. The choice
      persists across palette close/reopen (localStorage key `plm_ui_prefs_v1`).

### J. Coexistence with old add-in
- [ ] Both add-ins running simultaneously: the old "Change Management" palette (if open)
      and the new one can coexist without palette-id collisions or JS errors.
      (V2-suffixed palette id: `ACME_PLMWorkspaces2_ChangeManagement_V2` vs
      old `PLMChangeManagement`.)
- [ ] `py_compile` still passes on all files after both add-ins are stopped/started.

### K. py_compile (implementer sign-off)
- [ ] `core/workspace_command.py` — OK
- [ ] `commands/change_management/command.py` — OK
- [ ] `commands/change_management/__init__.py` — OK
- [ ] `commands/__init__.py` — OK

---

## Phase 2 — Unified Change Management (multi-workspace scope selector)

Prerequisite: Phase 1 passes. Change Management palette opens and displays records.

### A. Static checks (before loading in Fusion)
- [ ] `py_compile` passes on all modified files:
      `services/items.py`, `services/fm_client.py`, `core/entitlements.py`,
      `core/workspace_command.py`, `commands/change_management/command.py`.
- [ ] `node --check web/core/engine.js` passes.
- [ ] `web/capabilities/change_management/config.js` sets `cfg.scopes=true`,
      `cfg.scopesAction='getChangeScopes'`, `cfg.unifiedAction='getUnifiedChangeRecords'`,
      `cfg.unifiedDefaultKey='all'`.

### B. Scope selector appears
- [ ] Open the Change Management palette (entitled user, signed in).
- [ ] A **scope `<select>` dropdown** (`#scopeSelector`) appears in the list toolbar
      BEFORE the view selector (`#viewSelector`).
- [ ] The scope dropdown contains at least **"All change records"** plus one entry per
      discovered change workspace the user is entitled to (e.g. Change Orders, Lean
      Change Orders, Change Requests, Change Tasks, Problem Reports).
- [ ] **Problem Reports** appears in the list even though it has no tableaus — the
      unified fetch uses `/items` not `/tableaus`.
- [ ] If the tenant has duplicate workspaces (`WS_LEAN_CHANGE_ORDERS_1`, etc.), they
      appear as SEPARATE entries with their workspace title as the label.

### C. "All change records" unified view (default)
- [ ] On first open, scope defaults to **"All change records"**.
- [ ] `#viewSelector` (view selector) and `#btnNew` (New Change Order) are **hidden**.
- [ ] The table has three columns: **Type**, **Record**, **Owner**.
- [ ] Rows appear from MULTIPLE change workspaces mixed together (Type column differs
      between rows). Descriptor format is e.g. `"PR-000035 - Danger hazard in the
      factory"` for Problem Reports, `"CO-000012 - Reduce material cost"` for a CO.
- [ ] The table is sorted by **Record (descriptor)** ascending.
- [ ] If any workspace hit the 200-record cap, a yellow/info message bar appears:
      *"Some workspaces returned partial results (record cap reached)…"*
- [ ] Pagination (Prev/Next) works client-side over the merged rows.
- [ ] **Refresh** reloads the unified fetch.

### D. Row click → detail (unified scope)
- [ ] Clicking any row in the unified table opens the detail view for that item.
- [ ] The detail view renders the item's fields from its own workspace — not
      from the default WS_CHANGE_ORDERS.
- [ ] The **Open in Browser** button links to the correct workspace/item URL.
- [ ] Workflow transition buttons appear (if any available for that item).
- [ ] The **Affected Items tab** appears if the item has linked items.
- [ ] Breadcrumb shows `Change Management › <descriptor>`.
- [ ] Clicking the breadcrumb `Change Management` link returns to the unified table
      without refetching (rows are already in memory).

### E. Drill-down to a specific workspace
- [ ] Switching the scope selector to a specific workspace (e.g. "Change Orders")
      shows `#viewSelector` and `#btnNew` again.
- [ ] The view selector populates with that workspace's tableaus.
- [ ] Records shown match that workspace only (correct subset).
- [ ] Changing the tableau works; pagination works.
- [ ] **Edit** opens the edit form targeting the selected workspace.
- [ ] **+ New Change Order** creates an item in the selected workspace.
- [ ] Workflow transitions work for items in the selected workspace.
- [ ] **Affected Items** tab shows and the Remove/Add picker works.

### F. Detail/edit/workflow carry workspaceId correctly
- [ ] In drilled-down scope, `getTableaus`, `getTableauData`, `getViewFields`,
      `getWorkspaceSections` all carry the scoped `workspaceId` in the payload
      (visible in Python logs / Text Command window with `DEBUG=True`).
- [ ] `createItem` and `updateItem` target the scoped workspace.
- [ ] `runWorkflowTransition` targets the item's workspace.
- [ ] Switching scope to 'all' and clicking a row opens detail from the row's
      own `workspaceId` — NOT from the default WS_CHANGE_ORDERS.

### G. Scope selector persistence
- [ ] The selected scope is saved to localStorage and restored on palette re-open.
- [ ] If the saved scope is no longer in the list (workspace removed), it falls
      back to 'all' gracefully.

### H. Problem Reports specific
- [ ] Problem Reports workspace appears in the unified "All" table — records have
      the `workspaceShortName` (e.g. "PR") as the Type value, or the workspace title.
- [ ] Clicking a Problem Reports row opens its detail view correctly.
- [ ] Switching scope to "Problem Reports" shows its records (even though it has
      no tableaus — if the workspace has no tableaus, `getTableaus` returns an empty
      list and an appropriate message is shown, or the list stays empty with a
      "No views" message; this is expected and not an error).

### I. No regression on single-workspace capabilities
- [ ] Open a different workspace palette (e.g. Requirements Management, if enabled).
      It has NO scope selector and behaves identically to before.
- [ ] The Change Management palette with scope='all' does NOT break after switching
      to another palette and back.

### J. py_compile + node --check (implementer sign-off)
- [ ] `services/items.py` — OK
- [ ] `services/fm_client.py` — OK
- [ ] `core/entitlements.py` — OK
- [ ] `core/workspace_command.py` — OK
- [ ] `commands/change_management/command.py` — OK
- [ ] `web/core/engine.js` — OK (node --check)

---

## Phase 3 — Lazy enrichment pills + dynamic New button label

Prerequisite: Phase 2 passes. Unified "All change records" table is functional.

### A. Static checks (before loading in Fusion)
- [ ] `py_compile` passes on `core/workspace_command.py`.
- [ ] `node --check web/core/engine.js` passes.
- [ ] `web/core/components.css` contains `.state-pill` and `.affected-pill` rules.

### B. Unified Status + Affected pills appear after list load
- [ ] Open the Change Management palette. Switch to (or start in) **"All change records"** scope.
- [ ] The table renders immediately with **Status** and **Affected** columns to the right
      of the base columns (Type, Record, Owner). Both columns show `…` placeholders on initial render.
- [ ] Within a few seconds (after `getRecordsEnrichment` returns), the `…` placeholders
      are replaced:
      - **Status** column: each row shows a `.state-pill` badge with the record's current
        workflow state text (e.g. "Open", "In Review", "Released") or `—` for items with no state.
      - **Affected** column: items with `affectedCount > 0` show a clickable blue count badge;
        items with count 0 show `—`.
- [ ] Paginating (Next / Prev) re-renders the table; items already enriched show their pills
      immediately (from the in-memory cache, no extra network call). New items on the
      new page trigger a `getRecordsEnrichment` call for the uncached subset only.
- [ ] Clicking **Refresh** clears the enrichment cache and re-fetches everything.
- [ ] Switching scope to a specific workspace and back to "All" clears the cache and
      re-fetches (pills briefly show `…` then resolve).
- [ ] **No enrichment calls fire for the single-workspace (drilled-down) view** — the
      Status/Affected columns do not appear there.

### C. Affected pill click opens the record's Affected Items tab
- [ ] In the unified table, click an **Affected** count badge (must have count > 0) on any row.
- [ ] The detail view opens for that record — and the **Affected Items** tab is automatically
      activated (not the Details tab). This is achieved via `_pendingInitialTab = 'LINKEDITEMS'`
      set before `openDetail()`, which `initDetailTabsForItem` honors after tabs load.
- [ ] The row's normal click handler (opening Details) is NOT triggered; `stopPropagation`
      prevents it.
- [ ] Keyboard navigation: focus the pill with Tab and press Enter or Space — same
      Affected Items tab activation occurs.

### D. Dynamic "+ New" button label per scope
- [ ] With scope set to **"All change records"**, the `#btnNew` button is **hidden**.
- [ ] Switch to **"Change Orders"** scope → the button becomes visible and reads
      **"New Change Order"** (trailing 's' dropped).
- [ ] Switch to **"Problem Reports"** → button reads **"New Problem Report"**.
- [ ] Switch to **"Lean Change Orders"** → button reads **"New Lean Change Order"**.
- [ ] Switch to **"Change Requests"** → button reads **"New Change Request"**.
- [ ] Switch to **"Change Tasks"** → button reads **"New Change Task"**.
- [ ] Switch back to **"All change records"** → button is hidden again.
- [ ] Clicking the New button still creates an item in the currently scoped workspace
      (no change to creation behavior).

### E. py_compile + node --check (implementer sign-off)
- [ ] `core/workspace_command.py` — OK
- [ ] `web/core/engine.js` — OK (node --check)
