# WorkspaceCommand — base class for all "workspace" palette commands
# (Change Management, Engineering Projects, Supplier Packages, Requirements, …).
#
# Inherits PaletteCommand and adds:
#   - Workspace numeric ID resolution from self.workspace_system_name via entitlements
#     (with config.WORKSPACE_IDS fallback).
#   - LRU detail cache (per-instance, DETAIL_CACHE_MAX=50 entries) for getItemDetail.
#   - Parallel fetch pattern (ThreadPoolExecutor) for getItemDetail and getItemsTabCounts.
#   - Generic @action handlers for every workspace operation so thin subclasses
#     only declare their workspace_system_name, command/palette IDs, and any hooks.
#
# getItemDetail here returns {success, item, etag, transitions, currentStep} — it is
# a RICHER version than the base-class getItemDetail which returns {success, item, etag}.
# WorkspaceCommand shadows (overrides) the base handler so the JS always gets the
# enriched response needed to render detail + workflow actions in one call.

import copy
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import adsk.core

from . import config
from . import entitlements
from . import log
from .action_registry import action
from .palette_base import PaletteCommand
from ..services import fusion_cad as _cad
from ..services import item_payload as _payload


_DETAIL_CACHE_MAX = 50
_EXECUTOR_TIMEOUT = getattr(config, 'EXECUTOR_TIMEOUT_SECONDS', 60)


class WorkspaceCommand(PaletteCommand):
    """Base class for every workspace-backed capability.

    Subclass requirements:
        command_id           str  — unique command definition id (V2-suffixed via config)
        command_name         str  — visible name
        palette_id           str  — unique palette id (V2-suffixed via config)
        palette_title        str  — palette window title
        html_url             str  — absolute path to resources/html/index.html
        workspace_system_name str — FM systemName (e.g. 'WS_CHANGE_ORDERS')

    Optional:
        showAffectedItems    bool — tells the frontend to show the Affected Items tab
                                   (inspected by _plmCfg, not Python logic)
    """

    # Subclasses may set this to True to signal the frontend to show the affected-items tab.
    showAffectedItems = False

    def __init__(self):
        super().__init__()
        # LRU detail cache keyed by (workspace_id_str, item_id_str)
        self._detail_cache = OrderedDict()

    # ------------------------------------------------------------------
    # Workspace ID resolution (instance method for testability / override)
    # ------------------------------------------------------------------
    def _workspace_id(self):
        """Resolve this command's FM workspace numeric ID from entitlements, with
        a fallback to config.WORKSPACE_IDS if entitlements haven't been populated yet."""
        sys_name = self.workspace_system_name
        fallback_key = _find_config_key_for_system_name(sys_name)
        fallback_id = str(config.WORKSPACE_IDS.get(fallback_key, '')) if fallback_key else None
        return entitlements.get_workspace_id(sys_name, self._app, fallback_id)

    def _ws_from(self, data):
        """Return the workspace ID to use for an action: the payload's workspaceId when
        present and non-empty, otherwise fall back to self._workspace_id().

        This makes every workspace action capable of targeting an arbitrary workspace
        (needed for the unified Change Management drill-down) while preserving identical
        behaviour for single-workspace callers that never pass workspaceId.
        """
        ws_from_payload = str(data.get('workspaceId') or '').strip()
        return ws_from_payload if ws_from_payload else (self._workspace_id() or '')

    # ------------------------------------------------------------------
    # Shared @actions — all workspace capabilities get these for free.
    # ------------------------------------------------------------------

    @action('getWorkspaceAccess')
    def _act_ws_get_workspace_access(self, _ctx, data):
        """Override base getWorkspaceAccess to also return admin URLs.

        The frontend uses urlGroups / urlTemplateLibrary to wire the no-access
        admin buttons (same fields the old changeManagement did).
        """
        sys_name = data.get('systemName') or self.workspace_system_name
        tenant = (data.get('tenant') or '') + ''  # keep for URL build even if empty
        try:
            from . import auth
            tenant = auth.get_tenant(self._app) or ''
        except Exception:
            pass
        url_groups = getattr(config, 'ADMIN_URL_GROUPS', '').format(tenant=tenant)
        url_tpl = getattr(config, 'ADMIN_URL_TEMPLATE_LIBRARY', '').format(tenant=tenant)

        if not sys_name:
            return {
                'success': True, 'hasAccess': True, 'systemName': None,
                'urlGroups': url_groups, 'urlTemplateLibrary': url_tpl,
            }
        entitled = entitlements.get_entitled_workspace_ids(self._app)
        if entitled is None:
            return {
                'success': True, 'hasAccess': True, 'systemName': sys_name,
                'failedOpen': True, 'urlGroups': url_groups, 'urlTemplateLibrary': url_tpl,
            }
        wid = entitlements.get_workspace_id(sys_name, self._app)
        return {
            'success': True,
            'systemName': sys_name,
            'workspaceId': wid,
            'hasAccess': bool(wid and wid in entitled),
            'urlGroups': url_groups,
            'urlTemplateLibrary': url_tpl,
        }

    @action('getTableaus', async_=True)
    def _act_get_tableaus(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = self._ws_from(data)
        if not ws:
            return {'success': False, 'error': 'Workspace not resolved. Check entitlements.'}
        tableaus, fetch_err = self._client(ctx).tableaus(ws)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True, 'tableaus': tableaus or []}

    @action('getTableauData', async_=True)
    def _act_get_tableau_data(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = self._ws_from(data)
        if not ws:
            return {'success': False, 'error': 'Workspace not resolved.'}
        tid = data.get('tableauId')
        if not tid:
            return {'success': False, 'error': 'Missing tableauId.'}
        page = int(data.get('page') or 1)
        size = int(data.get('size') or 50)
        result, fetch_err = self._client(ctx).tableau_data(ws, tid, page, size)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True, **result}

    @action('getViewFields', async_=True)
    def _act_get_view_fields(self, _ctx, data):
        """Return authoritative field definitions for the workspace (title, type, editability, picklist).

        Uses /workspaces/{id}/fields (workspace-scoped, not view-scoped) which is the
        most reliable source for field definitions and doesn't require knowing a view id.
        Accepts an optional workspaceId in the payload to target a specific workspace
        (used by drill-down from the unified change records view).
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = self._ws_from(data)
        if not ws:
            return {'success': False, 'error': 'Workspace not resolved.'}
        fields, fetch_err = self._client(ctx).workspace_fields(ws)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True, 'fields': fields or []}

    @action('getWorkspaceSections', async_=True)
    def _act_get_workspace_sections(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = self._ws_from(data)
        if not ws:
            return {'success': False, 'error': 'Workspace not resolved.'}
        result, fetch_err = self._client(ctx).sections(ws)
        if fetch_err:
            return _api_err(fetch_err)
        return {
            'success': True,
            'sections': result.get('sections', []),
            'viewId': result.get('viewId', 1),
        }

    @action('getLookupOptions', async_=True)
    def _act_get_lookup_options(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        lookup_path = data.get('lookupPath')
        if not lookup_path:
            return {'success': False, 'error': 'Missing lookupPath.'}
        filter_text = data.get('filter', '')
        limit = int(data.get('limit') or 100)
        offset = int(data.get('offset') or 0)
        result, fetch_err = self._client(ctx).lookup_options(lookup_path, filter_text, limit, offset)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True, **result}

    @action('getItemDetail', async_=True)
    def _act_ws_get_item_detail(self, _ctx, data):
        """Workspace getItemDetail — fetches item + workflow transitions in parallel and
        caches by (workspaceId, itemId) with an LRU eviction policy.

        Shadows (overrides) the base PaletteCommand.getItemDetail so the JS always gets
        the enriched {success, item, etag, transitions, currentStep} response needed to
        render detail view + workflow actions in one round-trip.

        Payload: {itemId, workspaceId?, skipCache?}
        Returns: {success, item, etag, transitions, currentStep}
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err
        item_id = str(data.get('itemId') or '')
        ws_id = str(data.get('workspaceId') or self._workspace_id() or '')
        skip_cache = bool(data.get('skipCache'))
        if not item_id:
            return {'success': False, 'error': 'Missing itemId.'}
        if not ws_id:
            return {'success': False, 'error': 'Workspace not resolved.'}

        cache_key = (ws_id, item_id)
        if not skip_cache and cache_key in self._detail_cache:
            cached = self._detail_cache[cache_key]
            self._detail_cache.move_to_end(cache_key)
            return copy.deepcopy(cached)

        client = self._client(ctx)

        def _fetch_item():
            return client.item_detail(ws_id, item_id)

        def _fetch_transitions():
            return client.transitions(ws_id, item_id)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                future_item = executor.submit(_fetch_item)
                future_trans = executor.submit(_fetch_transitions)
                try:
                    item_tuple = future_item.result(timeout=_EXECUTOR_TIMEOUT)
                    trans_result = future_trans.result(timeout=_EXECUTOR_TIMEOUT)
                except FuturesTimeoutError:
                    return {'success': False, 'error': 'Request timed out. Check your connection and try again.'}
        except Exception as e:
            log.handle_error(f'{self.command_name}.getItemDetail')
            return {'success': False, 'error': str(e) or 'Fetch error.'}

        item, etag, item_err = item_tuple
        if item_err:
            unauth = item_err == 'unauthorized'
            return {
                'success': False,
                'error': 'Not signed in.' if unauth else item_err,
                'unauthorized': unauth,
            }

        item = dict(item) if item else {}
        item['openInBrowserUrl'] = client.build_item_details_url(ws_id, item_id)

        # transitions() always returns a 2-tuple (dict, error)
        transitions_data, _trans_err = trans_result

        transitions = (transitions_data or {}).get('transitions', []) if transitions_data else []
        current_step = (transitions_data or {}).get('currentStep', 0) if transitions_data else 0

        result = {
            'success': True,
            'item': item,
            'etag': etag or '',
            'transitions': transitions,
            'currentStep': current_step,
        }
        # LRU cache write
        while len(self._detail_cache) >= _DETAIL_CACHE_MAX:
            self._detail_cache.popitem(last=False)
        self._detail_cache[cache_key] = copy.deepcopy(result)
        self._detail_cache.move_to_end(cache_key)
        return result

    @action('createItem', async_=True)
    def _act_create_item(self, _ctx, data):
        """Build a correctly-shaped FM v3 create body from raw field values and POST it.

        Payload: {mode:'create', fieldValues:{fieldId:value}, workspaceId?}
        The request-body SHAPE is owned by services.item_payload (exclusions, coercion,
        derived-dependency, workspace-scoped links, required pre-flight) — see the
        JS_PYTHON_CONTRACT §7.3. Returns {success, itemId} or {success:false, missingFields}.
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err
        field_values = data.get('fieldValues')
        if not isinstance(field_values, dict):
            return {'success': False, 'error': 'Missing fieldValues payload.'}
        ws = self._ws_from(data)
        if not ws:
            return {'success': False, 'error': 'Workspace not resolved.'}
        client = self._client(ctx)
        fields, ferr = client.workspace_fields(ws)
        if ferr:
            return _api_err(ferr)
        sec_result, serr = client.sections(ws)
        if serr:
            return _api_err(serr)
        fields_meta = {f['id']: f for f in (fields or []) if f.get('id')}
        view_id = (sec_result or {}).get('viewId', 1)
        # The /sections endpoint carries no field->section membership, so borrow it from a
        # reference item in this workspace (also our reliable isSystemField source on create).
        # Fall back to the bare section list (all fields in the first section) if empty.
        section_struct = _payload.sections_from_ws(sec_result or {}, ws)
        ref_list, _rerr = client.items_list(ws, 0, 1)
        ref_items = (ref_list or {}).get('items') if isinstance(ref_list, dict) else None
        if ref_items:
            ref_item, _e_tag, _rerr2 = client.item_detail(ws, ref_items[0].get('itemId'))
            if ref_item:
                section_struct = _payload.sections_from_item(ref_item, ws, for_create=True)
                for fid, is_sys in _payload.system_fields_from_item(ref_item).items():
                    if fid in fields_meta:
                        fields_meta[fid]['isSystemField'] = is_sys
        sections, missing = _payload.build_item_body(
            'create', field_values, fields_meta, section_struct, ws, view_id)
        if missing:
            return {'success': False, 'missingFields': missing,
                    'error': 'Required: ' + ', '.join(missing)}
        if not sections:
            return {'success': False, 'error': 'Nothing to create — no writable fields provided.'}
        result, fetch_err = client.create_item(ws, sections)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True, 'itemId': (result or {}).get('itemId')}

    @action('updateItem', async_=True)
    def _act_update_item(self, _ctx, data):
        """Build a correctly-shaped FM v3 update body and PATCH it.

        Payload: {mode:'edit', itemId, etag, fieldValues:{fieldId:value}, workspaceId?}
        Section membership, isSystemField, and classificationId come from the item detail;
        editability/derived/formula/validators from workspace_fields. The JS-supplied etag
        drives If-Match (optimistic concurrency). Returns {success} or {success:false,
        missingFields}.
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err
        item_id = str(data.get('itemId') or '')
        field_values = data.get('fieldValues')
        etag = data.get('etag', '')
        if not item_id or not isinstance(field_values, dict):
            return {'success': False, 'error': 'Missing itemId or fieldValues payload.'}
        ws = self._ws_from(data)
        if not ws:
            return {'success': False, 'error': 'Workspace not resolved.'}
        client = self._client(ctx)
        fields, ferr = client.workspace_fields(ws)
        if ferr:
            return _api_err(ferr)
        item, _item_etag, ierr = client.item_detail(ws, item_id)
        if ierr:
            return _api_err(ierr)
        fields_meta = {f['id']: f for f in (fields or []) if f.get('id')}
        # isSystemField is not reliable on /fields — merge it from the item detail.
        for fid, is_sys in _payload.system_fields_from_item(item or {}).items():
            if fid in fields_meta:
                fields_meta[fid]['isSystemField'] = is_sys
        section_struct = _payload.sections_from_item(item or {})
        view_id = data.get('viewId') or 1
        sections, missing = _payload.build_item_body(
            'edit', field_values, fields_meta, section_struct, ws, view_id)
        if missing:
            return {'success': False, 'missingFields': missing,
                    'error': 'Required: ' + ', '.join(missing)}
        if not sections:
            return {'success': False, 'error': 'Nothing to save — no editable changes.'}
        fetch_err = client.update_item(ws, item_id, sections, etag)
        # Evict stale cache entry so next getItemDetail re-fetches (on success and failure).
        self._detail_cache.pop((ws, item_id), None)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True}

    @action('getAffectedItems', async_=True)
    def _act_get_affected_items(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = str(data.get('workspaceId') or self._workspace_id() or '')
        item_id = str(data.get('itemId') or '')
        if not item_id:
            return {'success': False, 'error': 'Missing itemId.'}
        view_id = int(data.get('viewId') or getattr(config, 'AFFECTED_ITEMS_VIEW_ID', 11))
        offset = int(data.get('offset') or 0)
        limit = int(data.get('limit') or 100)
        sort = str(data.get('sort') or 'item.title')
        result, fetch_err = self._client(ctx).affected_items(ws, item_id, view_id, offset, limit, sort)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True, **result}

    @action('addAffectedItems', async_=True)
    def _act_add_affected_items(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = str(data.get('workspaceId') or self._workspace_id() or '')
        item_id = str(data.get('itemId') or '')
        paths = data.get('paths') or []
        if not item_id or not isinstance(paths, list) or not paths:
            return {'success': False, 'error': 'Missing itemId or paths.'}
        results, fetch_err = self._client(ctx).add_affected(ws, item_id, paths)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True, 'results': results or []}

    @action('removeAffectedItem', async_=True)
    def _act_remove_affected_item(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = str(data.get('workspaceId') or self._workspace_id() or '')
        item_id = str(data.get('itemId') or '')
        aid = str(data.get('affectedItemId') or '')
        if not item_id or not aid:
            return {'success': False, 'error': 'Missing itemId or affectedItemId.'}
        view_id = int(data.get('viewId') or getattr(config, 'AFFECTED_ITEMS_VIEW_ID', 11))
        fetch_err = self._client(ctx).remove_affected(ws, item_id, aid, view_id)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True}

    @action('getItemTabs', async_=True)
    def _act_get_item_tabs(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = str(data.get('workspaceId') or self._workspace_id() or '')
        item_id = str(data.get('itemId') or '')
        if not item_id:
            return {'success': False, 'error': 'Missing itemId.'}
        tabs, fetch_err = self._client(ctx).item_tabs(ws, item_id)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True, 'tabs': tabs or []}

    @action('getItemPermissions', async_=True)
    def _act_get_item_permissions(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = str(data.get('workspaceId') or self._workspace_id() or '')
        item_id = str(data.get('itemId') or '')
        if not item_id:
            return {'success': False, 'error': 'Missing itemId.'}
        permissions, fetch_err = self._client(ctx).item_permissions(ws, item_id)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True, 'permissions': permissions or []}

    @action('getItemsTabCounts', async_=True)
    def _act_get_items_tab_counts(self, _ctx, data):
        """Batch-fetch tab counts for a list of items using parallel requests.

        Used by the list view to show an Affected Items count per row without
        forcing the user to open each record.
        Returns {success, counts: {itemId: {TAB_NAME: count}}}.
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = str(data.get('workspaceId') or self._workspace_id() or '')
        raw_ids = data.get('itemIds') or []
        if not isinstance(raw_ids, list):
            return {'success': True, 'counts': {}}
        item_ids = [str(i) for i in raw_ids if i is not None and str(i).strip()]
        if not item_ids:
            return {'success': True, 'counts': {}}

        client = self._client(ctx)
        counts = {}
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {
                    executor.submit(client.item_tabs, ws, iid): iid
                    for iid in item_ids
                }
                for fut, iid in list(futures.items()):
                    try:
                        tabs, _err = fut.result(timeout=_EXECUTOR_TIMEOUT)
                        if tabs:
                            per_tab = {}
                            for t in tabs:
                                if isinstance(t, dict) and t.get('name'):
                                    per_tab[t['name']] = t.get('totalCount', 0)
                            counts[iid] = per_tab
                    except FuturesTimeoutError:
                        pass
                    except Exception:
                        pass
        except Exception as e:
            log.handle_error(f'{self.command_name}.getItemsTabCounts')
            return {'success': False, 'error': str(e) or 'Tab-counts fetch error.'}

        return {'success': True, 'counts': counts}

    @action('getTransitions', async_=True)
    def _act_get_transitions(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = str(data.get('workspaceId') or self._workspace_id() or '')
        item_id = str(data.get('itemId') or '')
        if not item_id:
            return {'success': False, 'error': 'Missing itemId.'}
        result, fetch_err = self._client(ctx).transitions(ws, item_id)
        if fetch_err:
            return _api_err(fetch_err)
        return {'success': True, **result}

    @action('runWorkflowTransition', async_=True)
    def _act_run_workflow_transition(self, _ctx, data):
        ctx, err = self._resolve_ctx()
        if err:
            return err
        ws = str(data.get('workspaceId') or self._workspace_id() or '')
        item_id = str(data.get('itemId') or '')
        transition_id = str(data.get('transitionId') or '')
        current_step = int(data.get('currentStep') or 0)
        comments = (data.get('workflowComments') or data.get('comments') or '').strip()
        if not item_id or not transition_id:
            return {'success': False, 'error': 'Missing itemId or transitionId.'}
        fetch_err = self._client(ctx).run_transition(ws, item_id, transition_id, current_step, comments)
        if fetch_err:
            # Evict cache so next detail fetch is fresh.
            self._detail_cache.pop((ws, item_id), None)
            return _api_err(fetch_err)
        self._detail_cache.pop((ws, item_id), None)
        return {'success': True}

    @action('getRecordsEnrichment', async_=True)
    def _act_get_records_enrichment(self, _ctx, data):
        """Lazy enrichment for the unified change records table: state pill + affected count.

        Payload: {items: [{workspaceId, itemId}, ...]}
        Server-side cap: 80 items per call (excess are logged and ignored).

        For each item, fetches IN PARALLEL (ThreadPoolExecutor, max_workers=8):
          - state       : item_detail → currentState.title (falls back to str or '')
          - affectedCount: item_tabs  → LINKEDITEMS tab totalCount (0 if unavailable)

        Per-item errors are silently omitted so one slow/failing item cannot fail
        the whole call.  Unauthorized propagates normally (the _resolve_ctx guard).

        Response:
          {success: True, enrichment: {"<wsId>:<itemId>": {state, affectedCount}, ...}}
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err

        raw_items = data.get('items') or []
        if not isinstance(raw_items, list):
            return {'success': True, 'enrichment': {}}

        _ENRICHMENT_CAP = 80
        if len(raw_items) > _ENRICHMENT_CAP:
            log.log(
                f'{self.command_name}.getRecordsEnrichment: received {len(raw_items)} '
                f'items, capping at {_ENRICHMENT_CAP}.',
                adsk.core.LogLevels.WarningLogLevel,
            )
            raw_items = raw_items[:_ENRICHMENT_CAP]

        # Deduplicate and validate items
        seen = set()
        items_to_fetch = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            ws_id = str(entry.get('workspaceId') or '').strip()
            item_id = str(entry.get('itemId') or '').strip()
            if not ws_id or not item_id:
                continue
            key = f'{ws_id}:{item_id}'
            if key in seen:
                continue
            seen.add(key)
            items_to_fetch.append((ws_id, item_id))

        if not items_to_fetch:
            return {'success': True, 'enrichment': {}}

        client = self._client(ctx)
        enrichment = {}

        def _fetch_one(ws_id, item_id):
            """Fetch state + affectedCount for a single item. Returns (key, result_dict)."""
            key = f'{ws_id}:{item_id}'
            state = ''
            affected_count = 0
            try:
                item, _etag, item_err = client.item_detail(ws_id, item_id)
                if not item_err and item:
                    cs = item.get('currentState')
                    if isinstance(cs, dict):
                        state = cs.get('title') or ''
                    elif isinstance(cs, str):
                        state = cs
            except Exception:
                pass
            try:
                tabs, tabs_err = client.item_tabs(ws_id, item_id)
                if not tabs_err and tabs:
                    for t in tabs:
                        if isinstance(t, dict) and t.get('name') == 'LINKEDITEMS':
                            affected_count = int(t.get('totalCount') or 0)
                            break
            except Exception:
                pass
            return key, {'state': state, 'affectedCount': affected_count}

        try:
            executor = ThreadPoolExecutor(max_workers=8)
            try:
                futures = {
                    executor.submit(_fetch_one, ws_id, item_id): (ws_id, item_id)
                    for ws_id, item_id in items_to_fetch
                }
                for fut in list(futures.keys()):
                    try:
                        key, result = fut.result(timeout=_EXECUTOR_TIMEOUT)
                        enrichment[key] = result
                    except FuturesTimeoutError:
                        pass
                    except Exception:
                        pass
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            log.handle_error(f'{self.command_name}.getRecordsEnrichment')
            return {'success': False, 'error': str(e) or 'Enrichment fetch error.'}

        return {'success': True, 'enrichment': enrichment}

    @action('searchResultsForLineage', async_=True)
    def _act_search_results_for_lineage(self, _ctx, data):
        """Search FM for items matching lineage URN tails.

        Used by the component picker to resolve Fusion dataFile IDs to FM item links.
        Queries with the bare URN tail (unique base64url segment) which FM full-text
        indexes without colons — the same strategy as the old changeManagement code.
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err
        urns = data.get('urns')
        if not urns or not isinstance(urns, list):
            return {'success': False, 'error': 'Missing urns.', 'items': []}
        # Extract the unique tail of each lineage URN for FM search.
        search_terms = []
        for u in urns:
            u = str(u).strip()
            tail = u.rsplit(':', 1)[-1] if ':' in u else u
            if tail:
                search_terms.append(tail)
        if not search_terms:
            return {'success': True, 'items': []}

        revision = int(data.get('revision') or 1)
        if revision not in (1, 2, 3):
            revision = 1

        import re
        _WS_ITEM_RE = re.compile(r'/workspaces/(\d+)/items/(\d+)')
        result, fetch_err = self._client(ctx).search_results(
            search_terms, revision=revision, limit=20, pre_formatted=True)
        if fetch_err:
            return {'success': False, 'error': fetch_err, 'items': []}

        items_out = []
        for it in (result or {}).get('items', []):
            self_ref = (it.get('__self__') or it.get('link') or '').strip()
            m = _WS_ITEM_RE.search(self_ref)
            if not m:
                continue
            ws_id_str = m.group(1)
            item_id_str = m.group(2)
            title = (it.get('title') or it.get('name') or it.get('descriptor') or '').strip()
            version = (it.get('version') or it.get('revisionLabel') or '').strip()
            label = title or f'Item #{item_id_str}'
            if version:
                label = f'{label}  (v{version})'
            items_out.append({
                'link': self_ref,
                'workspaceId': ws_id_str,
                'itemId': item_id_str,
                'title': label,
                'version': version,
                'urn': it.get('urn') or '',
            })
        return {'success': True, 'items': items_out}

    # ------------------------------------------------------------------
    # Fusion CAD pickers — delegate to services/fusion_cad.py
    # ------------------------------------------------------------------

    @action('getSelectedComponents')
    def _act_get_selected_components(self, _ctx, data):
        try:
            return _cad.get_selected_components()
        except Exception as e:
            return {'success': False, 'error': str(e), 'components': []}

    @action('getRootComponents')
    def _act_get_root_components(self, _ctx, data):
        try:
            return _cad.get_root_components()
        except Exception as e:
            return {'success': False, 'error': str(e), 'components': []}

    @action('getOpenDrawings')
    def _act_get_open_drawings(self, _ctx, data):
        try:
            return _cad.get_open_drawings()
        except Exception as e:
            return {'success': False, 'error': str(e), 'drawings': []}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_err(fetch_err):
    """Convert a service-layer error string to the uniform response shape."""
    unauth = fetch_err == 'unauthorized'
    return {
        'success': False,
        'error': 'Not signed in.' if unauth else (fetch_err or 'API error.'),
        'unauthorized': unauth,
    }


def _find_config_key_for_system_name(system_name):
    """Return the config.WORKSPACE_IDS key for the given systemName, or None."""
    if not system_name:
        return None
    ws_names = getattr(config, 'WORKSPACE_SYSTEM_NAMES', {})
    for key, sn in ws_names.items():
        if sn == system_name:
            return key
    return None
