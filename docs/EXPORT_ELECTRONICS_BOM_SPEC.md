# Export Electronics BOM to Fusion Manage — Feature Spec

Status: **shipped, verified live** (FAA Sandbox `autodesk8937`, 2026-07). Internal/sales-demo
tool — the `adsk.electron` API is preview, so do not ship publicly until it's released.

## Purpose
One click in the Fusion **Schematic/PCB editor** turns the CAD bill of materials into a
rev-controlled PLM BOM: component Items linked to MPN sourcing records, hung off a parent PCBA
Item, kept in sync on re-export. Optionally attaches the design files.

## Where it lives
`commands/export_electronics_bom/` — a `PaletteCommand` (`panel='electronics'`) that creates its
own **"Manage" toolbar tab → "PLM" panel** in the Schematic Editor (`SchEditorEnvironement`) and
PCB Editor (`BoardLayoutEnvironement`). Requires sign-in.

## Fusion Manage model (verified)
Three workspaces, resolved by **systemName** (numeric ids vary per tenant), picked once in the
wizard and saved to `app_prefs.json`:

- **WS_ITEMS** — component Items + the parent PCBA Item. Fields used: `TITLE` (required),
  `VALUE`, `FOOTPRINT`, `DESCRIPTION`, `MANUFACTURER_PN`/`MANUFACTURER` (text copies),
  `SOURCE_DESIGN_ID` (parent key), `REFERENCE_MPN` (NEVER-editable — a sync reverse).
- **WS_MANUFACTURER_PN** — MPN sourcing records. Fields: `MANUFACTURER_PN`, `REFERENCE_ITEM`
  (reference → the Item), `MANUFACTURER` (reference → a Supplier).
- **WS_SUPPLIERS** — manufacturer/supplier records, matched/created by `NAME`.

**Link direction (important):** the Item↔MPN link is driven from the **MPN side** — set
`REFERENCE_ITEM` on the MPN record. The Item's `REFERENCE_MPN` populates via the tenant's sync
job (not immediately).

**BOM rows** live on the WS_ITEMS **Bill-of-Materials view** (found by title). Quantity is native;
**Reference Designators** is a BOM-row (viewdef) field written via `add_bom_row(fields=[…])`.

## Wizard flow
1. **Extract** (SYNC, `adsk.electron`): read `Schematic.parts` (or `board.linkedSchematic`),
   filter (no package / no 3D footprint / not-populated-for-variant), group by
   `(value, footprint, MPN, manufacturer)`. Row: `{mpn, manufacturer, value, footprint,
   description, quantity, referenceDesignators[], raw}`. `description` = a `DESCRIPTION`/`DESC`
   attribute else the device/deviceset name. `designId` = schematic name/id (parent key).
2. **Configure**: pick the three workspaces; map component fields (dropdowns from the Items
   workspace's fields); pick the **Title source** (a property or a composed template); map
   **Reference Designators** to a BOM-row field (dropdown from the BOM view's viewdefs); toggle
   **Sync design files**. Saved to prefs.
3. **Preview** (`resolveBomPlan`, no writes): per-line pills — Item **create/update**, MPN
   **create/match**, BOM **add/update/remove** — with counts, the parent's status, and rows to
   remove (full sync). Match is by MPN (via the MPN record's `REFERENCE_ITEM`, or the Item's
   `MANUFACTURER_PN` text as a fallback).
4. **Push** (`pushBom`, ordered writes): suppliers (find/create) → component Items
   (create/update) → MPN records (create + `REFERENCE_ITEM` link + `MANUFACTURER` supplier ref)
   → parent (find-or-create by `SOURCE_DESIGN_ID`, working-version guard) → **BOM reconcile**
   (adds → removes → updates; reference designators written on the row). Per-line failures are
   isolated. If "Sync design files" is on: `exportDesignFiles` (SYNC — EAGLE `.sch`/`.brd` via
   `exportManager`, zipped) → `uploadDesignFiles` (attach to the parent, version-bump).

## Reconcile semantics
Full sync: new rows added, changed quantities updated, and rows in PLM no longer in the CAD
design removed (shown in Preview, applied on Push). Idempotent — re-export doesn't duplicate.

## Actions (see JS_PYTHON_CONTRACT §7.x)
`getElectronicsBom` (sync), `getExportConfig`/`saveExportConfig` (sync),
`listWorkspaces`/`getMappingFields`/`getBomRowFields` (async), `resolveBomPlan`/`pushBom` (async),
`exportDesignFiles` (sync), `uploadDesignFiles` (async).

## Key code
- `services/electronics_bom.py` — extract + `export_design_files`.
- `services/bom.py` — BOM rows (read/add/update/remove), `item_versions`/`working_item_id`,
  `viewdef_fields`/`bom_row_fields`.
- `services/item_write.py` — shared item create/update (reused from create/edit).
- `services/attachments.py` — S3 upload (reused).
- `commands/export_electronics_bom/export.py` — `resolve_plan` / `push_plan`.
- `core/config.py` `ELECTRONICS_BOM_DEFAULT_CONFIG` — the mapping preset.

## Known limitations / open items
See `docs/BACKLOG.md` → "Electronics BOM export". Notably: `REFERENCE_MPN` is a sync-job reverse
(not instant); parent identity is keyed on the design name/id until a stable design lineage URN
is available; the MPN match query (scoped field-equals with a full-text fallback) should be
watched on large workspaces; refdes field link is stored per-config (re-pick if the tenant changes).
