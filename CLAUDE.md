# PLM Workspaces 2.0 — Autodesk Fusion 360 Add-in

Python Fusion 360 add-in (clean rewrite of the old "PLM Workspaces" add-in) that surfaces
Autodesk **Fusion Manage (PLM)** workspaces directly inside Fusion via embedded webview
palettes: Login/Auth, Change Management (unified multi-workspace), Requirements, Engineering
Projects, Supplier Packages, My Work, and Export-to-PLM commands (STEP/PDF/DXF/G-code). One
unified architecture — auth, HTTP, dispatch, palette lifecycle, theming, and error handling
are defined **once** in `core/` and inherited; adding a workspace capability is ~40 lines of
Python plus a frontend `config.js`. Zero-build vanilla-JS frontend. Independent internal tool,
not a shipping Autodesk product.

## Deployment

- **Runs from** the Fusion AddIns folder:
  `C:\Users\quadeg\AppData\Roaming\Autodesk\Autodesk Fusion 360\API\AddIns\PLM Workspaces 2.0`
- **Git source of truth** is this repo path (`…\1 - PLM add-ins\PLM Add-in for fusion\PLM Workspaces 2.0`),
  pushed to private repo `AutoGusX/plm-workspaces-2.0` (branch `main`).
- Ships as a **separate** add-in from the old "PLM Workspaces" (which stays installed as
  reference). Both coexist — all 2.0 UI/command/palette IDs carry a `_V2` suffix so panels and
  the OAuth custom event never collide.
- **Gitignored (never commit):** `tokens.json` (live access/refresh tokens), `prefs.json`,
  `ui_prefs.json`, `.venv/`, `__pycache__/`, `*.zip`. These are secrets/local runtime state.

## Architecture

See `docs/ARCHITECTURE.md` for the full picture. In brief:

- `core/` — `PaletteCommand`/`WorkspaceCommand` base classes, `@action` registry + dispatch,
  the single `http_client`, `auth` (PKCE), `entitlements`, `context` (RequestContext),
  `async_dispatcher`.
- `services/` — `FmClient` wraps all Fusion Manage v3 endpoints (single 401-refresh-retry lives
  here); plus `items`, `workflow`, `search`, `attachments`, `fusion_cad`.
- `web/` — ONE vanilla-JS frontend, no build step: `core/bridge.js` (messaging), `engine.js`
  (list/detail/form/picker), `theme.js`, `tokens.css`, `components.css`; per-capability
  `capabilities/<cap>/config.js` sets `window._plmCfg`.
- `commands/` — one thin subclass per capability (config + `@action` handlers only).
- Entry point `PLM Workspaces.py` → `commands.start()`/`stop()`; `stop()` also calls
  `events.clear_handlers()`.

**The one hard constraint:** a palette is a webview; the only channel is
`adsk.fusionSendData(action, payload)` (JS→Python) and `palette.sendInfoToHTML` (Python→JS push).
Full action catalog + response shapes are in `docs/JS_PYTHON_CONTRACT.md` — add a new action
there **first**, then implement it.

## Run / Verify

- **No headless test harness** — Fusion add-ins need the running Fusion GUI and `adsk.*`
  modules. `py_compile` passing means syntax is valid, NOT that the add-in runs.
- Static checks before loading: `py_compile` every changed `.py`; `node --check web/core/engine.js`
  (and any changed JS).
- Load/reload in Fusion: **Utilities → Add-Ins → Scripts and Add-Ins → "PLM Workspaces 2.0" → Run**.
  Test in the FAA Sandbox tenant.
- Follow `docs/VERIFICATION.md` — the manual per-phase checklist (load/unload, coexistence with the
  old add-in, cold sign-in, token refresh, each capability's flows).
- Read-only API probe (real services layer, no Fusion needed): `docs/dev/api_probe*.py` — uses a
  duck-typed RequestContext. Bearer token via `GET https://<tenant>.autodeskplm360.net/api/v3/token`
  with `credentials:'include'` (the HttpOnly `JSESSIONID` session cookie is auto-attached; the
  token is NOT in localStorage). Response `{accessToken, expiresIn, tokenType}` — this is the
  current-user token.

## Conventions & gotchas (do / don't — hard-won, do not regress)

- **Persistent handlers:** button commands are destroyed after each run — `commandCreated`,
  `incomingFromHTML`, and OAuth event handlers MUST go on `_persistent_handlers`, never
  `_local_handlers`, or they silently stop firing.
- **Async dispatcher:** HTTP-bound `@action`s are marked `async_=True` and run on the shared
  4-worker `ThreadPoolExecutor`; results come back on the main thread via the
  `ASYNC_RESULT_EVENT_ID` custom event, resolved transparently in `bridge.js`. **Async handlers
  must NOT call the Fusion API** — read identity from `self._resolve_ctx()` (RequestContext is
  pre-resolved on the main thread in `_on_incoming` and carried via
  `async_dispatcher._thread_ctx`).
- **These actions must stay SYNC** (they call `self._app.*`, main-thread-only):
  `openInFusion`, `getTheme`, `getSelectedComponents`, `getRootComponents`, `getOpenDrawings`,
  plus CAM/export steps like `cam.postProcess()`.
- **OAuth custom event is process-global, not ref-counted** — register it once in
  `commands/__init__.py`, never per-command. Keep the unique `_V2` event id so the old add-in
  never receives the 2.0 token (subscribe to the specific registered `CustomEvent`, not the
  global `customEventFired`).
- **`_whenAdskReady()`:** `adsk.fusionSendData` is injected asynchronously — `plmSend` polls for
  readiness before sending, else first-open calls silently drop.
- **`workspaceShortName` is junk** on many workspaces (it's an app-store blurb) — use
  `workspaceLongName` / the workspace title for display.
- **`_person_name()` helper** (`services/items.py`): the FM `/items` endpoint returns
  owner/creator as plain strings, not objects — the helper handles `str | dict | None`.
- **Rich HTML fields:** detect BOTH escaped (`&lt;div`) and raw (`<p>`) HTML in field values.
- **Uniform response shape:** every `@action` returns `{success: true, …}` or
  `{success: false, error: "…", unauthorized?: true}`. `unauthorized` means "show the login
  palette," not a generic error. Use generic action names (`getItemDetail` + `workspaceKey`),
  no per-domain aliases.
- Match surrounding style; don't reformat unrelated lines. Small, reviewable commits.

## Documentation map

- `docs/ARCHITECTURE.md` — unified architecture, directory layout, design rationale (source of truth).
- `docs/JS_PYTHON_CONTRACT.md` — every JS↔Python action, payload, and response shape.
- `docs/VERIFICATION.md` — manual in-Fusion verification checklist, grown per phase.
- `docs/IMPLEMENTATION_PLAN.md` — phased build plan.
- `docs/dev/api_probe*.py` — read-only API probes.

## Workflow

- **`docs/BACKLOG.md` is the current-work source of truth** — read it at the start of every
  session; update it as work completes. (Feature specs for new capabilities live under
  `docs/` and the old add-in's `specifications/` folder; note there is no top-level `specs/`.)
- New capability: read the spec → plan → implement (thin `WorkspaceCommand`/`PaletteCommand`
  subclass + `config.js`, add the action to `JS_PYTHON_CONTRACT.md` first) → `py_compile` +
  `node --check` → manual verify in Fusion per `VERIFICATION.md`.
- Bump `"version"` in `PLM Workspaces.manifest` on release. Confirm with Gus before marking a
  backlog item done.
