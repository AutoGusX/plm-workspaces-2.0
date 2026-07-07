# PLM Workspaces 2.0 — Implementation Plan

> Status: **ACTIVE — Phases 0–3 complete (shipped 2026-06-15 → 2026-06-24). My Work full feature port complete 2026-06-24. Phase 4 not started.**
> See `docs/BACKLOG.md` for current bugs, deferred fixes, and upcoming features.
> Companion to `ARCHITECTURE.md`.

## How sub-agents and models are used

Every phase runs as: **implement → mechanical scan → code review → manual-in-Fusion verify**.

| Role | Model | Why |
|---|---|---|
| **Architect / scaffolder** | Opus 4.8 | Phase 0 backbone defines every later contract; highest leverage, must be right. |
| **Implementer** (per module / frontend port) | Sonnet 4.6 | High-volume, well-specified work against fixed contracts. |
| **Mechanical QA** | Haiku 4.5 | Cheap, fast: import/syntax load, action-name parity, dead-code/fork scans, header audits. |
| **Code reviewer** | Opus 4.8 | Adversarial correctness review against `ARCHITECTURE.md` before each phase is accepted. |
| **Verification author** | Opus 4.8 | Writes/maintains the manual Fusion checklist; you run it (Fusion can't be driven headlessly here). |

Sub-agents are dispatched with explicit, self-contained specs and the relevant old-code
paths to port from. Reviewer agents are prompted to *refute* correctness, not confirm it.
Each phase ends with a written summary + an updated `VERIFICATION.md` for you to run in Fusion.

> Note on "testing": Fusion add-ins require the running Fusion process and the `adsk.*`
> modules, so there is **no headless unit-test harness**. QA = static parity/lint scans
> (automated) + a manual checklist executed inside Fusion (you). This is called out so
> "tests pass" is never claimed when it means "static scan passed."

---

## Phase 0 — Backbone  *(COMPLETE)*  *(model: Opus build, Opus review)*

**Build:** `core/` (config, paths, log, events, **http_client**, **auth**, entitlements,
context, **action_registry**, **palette_base**, panels), `services/fm_client.py` +
service modules, `PLM Workspaces.py`, `commands/__init__.py`, manifest. Plus a single
trivial smoke command (login palette) to prove the base end-to-end. Generate
`docs/JS_PYTHON_CONTRACT.md`.

**Accept when:** add-in loads in Fusion with no errors; sign-in / token-refresh /
stay-signed-in all work through `core/auth`; `FmClient.outstanding_work()` returns data;
reviewer confirms no duplicated request/dispatch logic remains in the base.

**Deliverables:** working backbone + login; contract doc; `VERIFICATION.md` v1.

---

## Phase 1 — Frontend engine  *(COMPLETE)*  *(model: Sonnet port, Opus review)*

**Build:** `web/core/` — `bridge.js` (one messaging layer + auth poller), `engine.js`
(ported `plmWorkspaceCore.js`), `theme.js`, `tokens.css`, `components.css`, `picker.html`.
Wire one read-only capability (Change Management) as the first consumer to exercise
list/detail.

**Accept when:** Change Management list + detail render and theme correctly off the shared
engine; only **one** `bridge.js` exists (designReview fork deleted, not ported); picker
markup exists once; no inline-JS style strings remain.

---

## Phase 2 — Read-only / form workspaces  *(COMPLETE)*  *(model: Sonnet impl, Haiku scan, Opus review)*

**Build:** `change_management`, `engineering_projects`, `supplier_packages`, `requirements`
as `PaletteCommand` subclasses + engine configs. Generic `getItemDetail` everywhere
(no aliases). Affected-items, create/edit forms, workflow transitions via shared engine.

**Accept when:** each lists, opens detail, creates/edits, runs a transition in Fusion;
Haiku parity scan confirms no per-module `_api()`/dispatch boilerplate reintroduced;
action names match the contract doc.

---

## Phase 3 — My Work + enrichment + async  *(COMPLETE)*  *(model: Sonnet impl, Opus review)*

**Build:** `my_work` capability — re-express the 1,916-line standalone palette.js as engine
config + hooks, retaining tile coloring, summary strip, quick/structured filters, sort/group,
preview pane, bulk transitions, auto-refresh, and CAD-context (lineage-URN) matching.

**Accept when:** feature-by-feature diff vs. the old My Work passes (checklist enumerates
every Phase 1–8 feature from the old spec); shares `bridge.js`/tokens/components.

---

## Phase 4 — Stateful flows  *(model: Sonnet impl, Opus review)*

**Build:** `design_review` (screenshot capture + canvas markup as capability hooks),
`export_pdf` (drawing → PDF → CW_DRAWINGS attach + version), `export_gcode` (NC post →
CW_COMPONENTS attach + version, multi-file). Reuse `services/attachments.py` + `fusion_cad.py`.

**Accept when:** screenshot/markup save round-trips to a Design Review record; PDF and
G-code each export + attach + version correctly against a real record in Fusion.

---

## Phase 5 — Cross-cutting QA & cutover  *(model: Haiku scan, Opus review)*

**Build/verify:** full auth matrix (cold sign-in, expired-token refresh, refresh-failure
re-login, sign-out, stay-signed-in across restart); entitlement gating (button show/hide
per workspace access); parity audit old↔new across all capabilities; finalize
`VERIFICATION.md`; version bump + manifest finalize.

**Accept when:** every checklist item passes in Fusion; reviewer signs off parity; you
decide cutover (rename/replace old add-in or keep side-by-side).

---

## Coordination & guardrails

- **IDs:** all 2.0 command/palette IDs carry a `_v2`/`2.0` suffix so old and new add-ins
  coexist without panel/palette collisions during migration.
- **Source of truth:** sub-agents implement strictly against `ARCHITECTURE.md` +
  `JS_PYTHON_CONTRACT.md`; deviations require updating the doc first.
- **Porting, not reinventing:** auth, FM endpoints, S3 flow, lineage matching, and
  screenshot UX are *ported with behavior preserved*; only structure changes.
- **Between phases:** I report a summary + updated checklist and **pause for your go**
  before starting the next phase (per the confirmed execution model).
- **Parallelism:** within a phase, independent modules (e.g. the four Phase-2 workspaces)
  are implemented by concurrent Sonnet sub-agents, then reviewed together by one Opus pass.

## Decisions (confirmed by user 2026-06-15)

1. **APS client id**: **Reuse** the existing `APS_CLIENT_ID` from old `config.py` verbatim.
2. **Cutover**: Ship as a **separate installed add-in** ("PLM Workspaces 2.0"); the old
   add-in is never replaced/removed by this work. All 2.0 command/palette IDs carry a
   distinct suffix so both coexist.
3. **Verification**: **Windows-tested**; code written cross-platform (win+mac), with all
   OS-specific logic isolated in `core/paths.py`. No Mac verification required for now.
