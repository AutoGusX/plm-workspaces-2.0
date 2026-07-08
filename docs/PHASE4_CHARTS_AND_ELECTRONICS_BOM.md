# Phase 4 prototypes — PLM Charts + Export Electronics BOM (v1)

Status: **built, static-checked, charts logic verified offline against live API shapes.**
Needs an **in-Fusion smoke test** (reload the add-in). Built overnight 2026-07-08.

---

## 1. PLM Charts (dashboard palette)

**What it does:** renders the tenant's Fusion Manage report dashboard as charts inside a
Fusion palette (PLM panel button "PLM Charts").

**API (verified live on adskgusquade2025, 2026-07-08):**
- `GET /api/rest/v1/reports/dashboard` → `{dashboardReportList:{list:[{id,position,link}]}}`.
- `GET /api/rest/v1/reports/{id}/chart.json` → top-level `reportDefinition` (+ `reportChart`
  = type/title/axis labels), `reportResult` (`columnKey[{value,label}]` + `row[{fields.entry
  [{key,fieldData{value,formattedValue,label}}]}]`), and `xAxisColumn`/`yAxisColumn`.
  The **data is in `reportResult`**, not `reportChart`. The series column (stacked/multi) is the
  `columnKey` that is neither x nor y. Uses `/api/rest/v1` → needs `X-Tenant` (`send_tenant=True`).
- Chart types seen: COLUMN, STACKEDCOLUMN, LINE, MSAREA, DOUGHNUT (renderer also handles
  BAR/PIE/AREA variants).

**Files:**
- `services/reports.py` — `dashboard()` + `chart()` (normalizes to a render-ready shape).
- `services/fm_client.py` — `report_dashboard()` / `report_chart()` delegators.
- `commands/plm_charts/command.py` — `PlmChartsCommand(PaletteCommand)`, async `getDashboards`/`getChart`.
- `commands/plm_charts/resources/html/{index.html, static/plm_charts.js}` — self-contained palette,
  inline-SVG renderer (no chart lib), light/dark theme, refresh, open-in-browser per chart.
- `core/config.py` — `PALETTE_ID_PLM_CHARTS`, `COMMAND_IDS['plmCharts']`.
- Registered in `commands/__init__.py`.

**Verified:** `services/reports.py` normalization run offline against the captured live JSON —
single-series COLUMN, series-split STACKEDCOLUMN, DOUGHNUT, and dashboard position-ordering all
correct (see the offline test in the build session). SVG rendering itself needs eyes-on in Fusion.

---

## 2. Export Electronics BOM (v1: read & display + export-prep)

**What it does:** extracts the active electronics design's BOM and shows it in a palette
(Electronics environment panel button "Export Electronics BOM"), plus a normalized
"export-prep" payload destined for Fusion Manage. **v1 makes no FM writes.**

**API (from `../fusion-desktop-api-docs.md`, Electronics preview `adsk.electron`):**
- Cast `adsk.electron.Schematic.cast(app.activeProduct)`; if a Board is active use
  `board.linkedSchematic`. BOM lives on `Schematic.parts`, not the board.
- Per `Part`: skip if `device.package is None`, if `package3d.name` is empty, or if not
  populated for the active variant. Read `MPN`/`MF` from `Part.attributes` (case-insensitive,
  free-form). Group by `(value, footprint, MPN, manufacturer)`; qty = group size; designators
  = sorted `Part.name`.
- No built-in BOM export on `ElectronicsExportManager` (EAGLE files only) — we build it ourselves.
- Entire `adsk.electron` namespace is 🧪 preview — internal/sales-demo only, don't ship publicly.

**Files:**
- `services/electronics_bom.py` — `extract_bom(app)` (lazy `import adsk.electron` so older Fusion
  degrades to a clean error). Returns `{design, rows, partCount, skipped, warnings}`.
- `commands/export_electronics_bom/command.py` — `ExportElectronicsBomCommand(PaletteCommand)`,
  `panel='electronics'`, SYNC `getElectronicsBom` action (main-thread; calls the Fusion API).
- `commands/export_electronics_bom/resources/html/{index.html, static/electronics_bom.js}` —
  summary tiles, BOM table, warnings, and a collapsible export-prep JSON payload (+ Copy).
- `core/config.py` — `PALETTE_ID_EXPORT_ELECTRONICS_BOM`, `COMMAND_IDS['exportElectronicsBom']`,
  `ELECTRONICS_*` environment/panel ids.
- `core/panels.py` — `add/remove_command_from_electronics_panel` (creates our own panel in the
  electronics env), plus a guard so non-PLM commands don't leak onto the Design/Drawing panel.
- `core/palette_base.py` — `_add/_remove_panel_button` handle `panel=='electronics'`.
- Registered in `commands/__init__.py`.

---

## 3. Open items / next steps

1. **In-Fusion smoke test (both).** Reload the add-in; sign in; open PLM Charts (confirm the
   dashboard grid + each chart type renders) and, in an Electronics design, open Export
   Electronics BOM (confirm the table + export-prep payload).
2. **Confirm the Electronics environment/panel ids.** `core/config.py`
   `ELECTRONICS_WORKSPACE_ID_CANDIDATES` lists guesses (`FusionElectronicsPcbEnvironment`, …).
   Use "Write user interface to a file" in Fusion to capture the real PCB/Schematic environment +
   tab ids and pin them down; the panel helper already tries all candidates.
3. **Charts: live render check.** The data path is verified; only the SVG visuals need eyes-on.
   Two-column responsive grid, ~10-color categorical palette, legend for pie/multi-series.
4. **Electronics BOM phase 2 (future).** Resolve each row against the electronics FM workspace
   (component URN → MPN → create), preview, then push (update → create → parent PCBA → BOM rows).
   Needs the FM workspace systemName + field systemNames + BOM REST shape (open items from the
   original spec). v1 deliberately stops at extraction + prep.
