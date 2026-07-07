# Export to PLM — unified STEP + Drawing PDF export.
#
# Appears in both the Design workspace PLM panel and the Drawing workspace PLM
# panel (panels.py already targets both via _PANEL_WORKSPACE_IDS).
#
# Two-phase flow:
#   Phase 1 (async): scan design components → batch PLM search → build match table
#                    Each matched item also fetches its last STEP attachment date.
#   Phase 2 (sync then async): user confirms → export STEP/PDF → upload files
#
# Component routing:
#   External (has own Fusion file): STEP → its own PLM item
#   Internal (same-file body):      STEP → nearest external ancestor's PLM item
#   Root assembly:                  Full-assembly STEP → root PLM item
#
# STEP export for individual components is probed at runtime (individual component
# STEP API varies by Fusion version). Assembly STEP is always supported.

import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import adsk.core
import adsk.fusion

from ...core import config
from ...core import entitlements
from ...core import log as _log
from ...core import paths
from ...core.action_registry import action
from ...core.palette_base import PaletteCommand
from ...services import fusion_cad as _cad

_STEP_SUFFIX = '_step_export'
_PDF_SUFFIX  = '_drawing_pdf_export'


# ---------------------------------------------------------------------------
# Resource naming
# ---------------------------------------------------------------------------

def _step_resource(name):
    return re.sub(r'[^\w\-]', '_', (name or 'component').strip()) + _STEP_SUFFIX

def _pdf_resource(name):
    return re.sub(r'[^\w\-]', '_', (name or 'drawing').strip()) + _PDF_SUFFIX


# ---------------------------------------------------------------------------
# Component collection
# ---------------------------------------------------------------------------

def _collect_components(design, app):
    """Walk all unique components in the design with hierarchy + PLM routing info.

    Returns (root_info, component_list) where root_info covers the full assembly
    and component_list covers each unique child component (deduplicated).

    Each entry:
      entityToken, name, parentPath, instanceCount, isAssembly,
      isExternal, lineageUrn, plmTargetUrn
    """
    root = design.rootComponent

    # Root assembly lineage URN
    root_urn = None
    try:
        doc = app.activeDocument
        if doc and doc.dataFile:
            root_urn = _cad.file_id_to_lineage_urn(doc.dataFile.id)
    except Exception:
        pass

    root_name = getattr(root, 'name', 'Assembly') or 'Assembly'
    root_info = {
        'entityToken':   getattr(root, 'entityToken', 'root') or 'root',
        'name':          root_name,
        'parentPath':    '',
        'instanceCount': 1,
        'isAssembly':    True,
        'isExternal':    bool(root_urn),
        'lineageUrn':    root_urn,
        'plmTargetUrn':  root_urn,
    }

    seen_tokens  = {root_info['entityToken']}
    comp_map     = {}   # token → data dict (for parent lookups)
    result_list  = []

    try:
        occs = root.allOccurrences
        for i in range(occs.count):
            try:
                occ  = occs.item(i)
                comp = occ.component
                if not comp:
                    continue

                token = getattr(comp, 'entityToken', '') or ''
                if not token:
                    continue

                if token in seen_tokens:
                    if token in comp_map:
                        comp_map[token]['instanceCount'] += 1
                    continue
                seen_tokens.add(token)

                comp_name  = (getattr(comp, 'name', '') or '').strip()
                full_path  = (getattr(occ, 'fullPathName', '') or '')

                # Build parent path from fullPathName ("Vise:1+Handle:1+Bolt:1")
                parts = full_path.split('+')
                if len(parts) > 1:
                    parent_labels = [re.sub(r':\d+$', '', p) for p in parts[:-1]]
                    parent_path = ' > '.join(parent_labels)
                else:
                    parent_path = root_name

                # File ID → lineage URN (external if available)
                fid = None
                urn = None
                try:
                    fid = _cad.try_get_component_file_id(comp)
                    urn = _cad.file_id_to_lineage_urn(fid) if fid else None
                except Exception:
                    pass
                is_external = bool(urn)

                # PLM target: own item if external, else nearest external ancestor
                if is_external:
                    plm_target_urn = urn
                else:
                    plm_target_urn = _find_external_ancestor_urn(
                        occ, comp_map, root_urn)

                try:
                    is_asm = occ.childOccurrences.count > 0
                except Exception:
                    is_asm = False

                entry = {
                    'entityToken':   token,
                    'name':          comp_name,
                    'parentPath':    parent_path,
                    'instanceCount': 1,
                    'isAssembly':    is_asm,
                    'isExternal':    is_external,
                    'lineageUrn':    urn,
                    'plmTargetUrn':  plm_target_urn,
                }
                comp_map[token]  = entry
                result_list.append(entry)

            except Exception:
                continue
    except Exception as e:
        _log.log(f'[ExportToPlm] component walk error: {e}')

    return root_info, result_list


def _find_external_ancestor_urn(occ, comp_map, root_urn):
    """Walk assemblyContext upward to find the nearest external ancestor's URN."""
    try:
        parent = occ.assemblyContext
        while parent:
            p_comp = parent.component
            if p_comp:
                p_token = getattr(p_comp, 'entityToken', '') or ''
                if p_token in comp_map and comp_map[p_token]['isExternal']:
                    return comp_map[p_token]['lineageUrn']
                # Not in map yet — check directly
                try:
                    fid = _cad.try_get_component_file_id(p_comp)
                    urn = _cad.file_id_to_lineage_urn(fid) if fid else None
                    if urn:
                        return urn
                except Exception:
                    pass
            try:
                parent = parent.assemblyContext
            except Exception:
                break
    except Exception:
        pass
    return root_urn   # fallback: root assembly record


# ---------------------------------------------------------------------------
# Drawing linked-design discovery
# ---------------------------------------------------------------------------

def _linked_design_urn(doc):
    """Try to extract the lineage URN of the design linked to a drawing document.

    Walks drawing product → sheets → views → referencedDocument to get the
    design file's lineage URN for PLM component auto-detection.
    Returns a lineage URN string or None.
    """
    try:
        for j in range(doc.products.count):
            product = doc.products.item(j)
            obj_type = getattr(product, 'objectType', '') or ''
            if 'Drawing' not in obj_type:
                continue
            for sheets_attr in ('sheets', 'activeSheet'):
                try:
                    sheets_obj = getattr(product, sheets_attr, None)
                    if sheets_obj is None:
                        continue
                    sheet_list = ([sheets_obj]
                                  if not hasattr(sheets_obj, 'count')
                                  else [sheets_obj.item(k)
                                        for k in range(min(sheets_obj.count, 5))])
                    for sheet in sheet_list:
                        for vattr in ('drawingViews', 'views'):
                            try:
                                views = getattr(sheet, vattr, None)
                                if views is None:
                                    continue
                                for vi in range(min(views.count, 10)):
                                    view = views.item(vi)
                                    for vref in ('referencedDocument', 'modelDocument'):
                                        ref_doc = getattr(view, vref, None)
                                        if ref_doc and hasattr(ref_doc, 'dataFile'):
                                            df = ref_doc.dataFile
                                            fid = getattr(df, 'id', None) if df else None
                                            urn = _cad.file_id_to_lineage_urn(fid)
                                            if urn:
                                                return urn
                            except Exception:
                                continue
                except Exception:
                    continue
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Attachment helpers
# ---------------------------------------------------------------------------

def _extract_thumbnail(item):
    for sec in (item.get('sections') or []):
        if not isinstance(sec, dict):
            continue
        for field in (sec.get('fields') or []):
            if not isinstance(field, dict):
                continue
            if field.get('__self__', '').endswith('/DESIGN_THUMBNAIL'):
                val = field.get('value')
                if isinstance(val, dict) and not val.get('deleted'):
                    return val.get('link', '')
                return ''
    return ''


def _fetch_item_full(client, self_link):
    """GET full item — returns (item_dict_or_None, thumbnail, locked, lifecycle, last_step_att)."""
    if not self_link:
        return None, '', False, '', None
    try:
        r = client._request('GET', f'{client.base}{self_link}', empty_body_as={})
        if not r.ok or not isinstance(r.data, dict):
            return None, '', False, '', None
        data      = r.data
        lifecycle = data.get('lifecycle') or {}
        lc_title  = lifecycle.get('title', '') if isinstance(lifecycle, dict) else ''
        thumb     = _extract_thumbnail(data)
        locked    = bool(data.get('itemLocked'))
        return data, thumb, locked, lc_title, None
    except Exception:
        return None, '', False, '', None


def _fetch_last_step_attachment(client, workspace_id, item_id):
    """Return the most recent STEP attachment info, or None."""
    try:
        from ...services import attachments as _att
        atts, _ = _att.list_attachments(client, workspace_id, item_id)
        if not atts:
            return None
        step_atts = [a for a in atts
                     if isinstance(a.get('name', ''), str)
                     and a['name'].lower().endswith('.step')]
        if not step_atts:
            return None
        # Pick most recent (list_attachments returns them sorted by name, not date)
        # Use the one whose resourceName contains our suffix if possible
        preferred = [a for a in step_atts
                     if _STEP_SUFFIX in (a.get('name', '') or '').lower()]
        best = preferred[0] if preferred else step_atts[0]
        return {
            'id':   best.get('id', ''),
            'name': best.get('name', ''),
            'date': best.get('lastModifiedOn', '') or best.get('createdOn', ''),
        }
    except Exception:
        return None


def _parse_search_item(item, ws_id_str):
    """Parse a PLM search result item into a component dict. Returns None if invalid."""
    if item.get('deleted'):
        return None
    self_link = item.get('__self__', '')
    ws_m  = re.search(r'workspaces/(\d+)', self_link)
    itm_m = re.search(r'items/(\d+)', self_link)
    if not ws_m or not itm_m or ws_m.group(1) != ws_id_str:
        return None
    version_raw = item.get('version', '')
    vm = re.match(r'\[REV:(.+)\]', version_raw)
    ver_label  = f'Rev {vm.group(1)}' if vm else (version_raw or 'Unknown')
    ver_id     = item.get('versionId') or (vm.group(1) if vm else '')
    lifecycle  = item.get('lifecycle') or {}
    lc_title   = lifecycle.get('title', '') if isinstance(lifecycle, dict) else ''
    title = item.get('title') or item.get('descriptor') or ''
    return {
        'itemId':      itm_m.group(1),
        'workspaceId': ws_m.group(1),
        'title':       title or 'Unknown',
        'version':     ver_label,
        'versionId':   ver_id,
        'latestRelease':     bool(item.get('latestRelease')),
        'workingVersion':    bool(item.get('workingVersion')) or ver_id == 'w',
        'lifecycle':         lc_title,
        'thumbnail':         '',
        'locked':            False,
    }


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class ExportToPlmCommand(PaletteCommand):
    command_id      = config.COMMAND_IDS['exportToPlm']
    command_name    = 'Export to PLM'
    command_tooltip = 'Export STEP and Drawing PDF files and attach to Fusion Manage component records'
    palette_id      = config.PALETTE_ID_EXPORT_TO_PLM
    palette_title   = 'Export to PLM'
    palette_size    = (620, 820)
    resizable       = True
    docking         = 'right'
    panel           = 'plm'   # appears in Design AND Drawing workspaces via _PANEL_WORKSPACE_IDS

    workspace_system_name = None
    workspace_id_fallback = None

    html_url = paths.to_file_url_path(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'resources', 'html', 'index.html',
        )
    )

    # ------------------------------------------------------------------
    # Phase 0: context load (sync — Fusion API, main thread)
    # ------------------------------------------------------------------

    @action('getExportContext')
    def _act_get_context(self, _ctx, data):
        """Scan the active design for components and open drawings.

        Returns the component list and drawing list needed to build the
        match table in Phase 1. No PLM network calls here.
        """
        # Detect active product
        design = None
        try:
            design = adsk.fusion.Design.cast(self._app.activeProduct)
        except Exception:
            pass
        if not design:
            try:
                doc = self._app.activeDocument
                if doc:
                    design = adsk.fusion.Design.cast(
                        doc.products.itemByProductType('DesignProductType'))
            except Exception:
                pass

        components  = []
        root_info   = None
        has_design  = False

        if design:
            has_design = True
            root_info, components = _collect_components(design, self._app)

        # Scan open drawing documents
        drawings = []
        try:
            for i in range(self._app.documents.count):
                try:
                    doc      = self._app.documents.item(i)
                    is_drw   = False
                    doc_name = getattr(doc, 'name', '') or ''
                    try:
                        for j in range(doc.products.count):
                            if 'Drawing' in (getattr(doc.products.item(j), 'objectType', '') or ''):
                                is_drw = True
                                break
                    except Exception:
                        pass
                    if not is_drw:
                        continue
                    df  = getattr(doc, 'dataFile', None)
                    fid = getattr(df, 'id', '') if df else ''
                    linked_urn = _linked_design_urn(doc)
                    clean_name = re.sub(r'\.[^.]+$', '', doc_name).strip() or doc_name
                    drawings.append({
                        'name':       clean_name,
                        'fileId':     str(fid) if fid else '',
                        'linkedUrn':  linked_urn or '',
                    })
                except Exception:
                    continue
        except Exception:
            pass

        return {
            'success':    True,
            'hasDesign':  has_design,
            'rootInfo':   root_info,
            'components': components,
            'drawings':   drawings,
        }

    # ------------------------------------------------------------------
    # Phase 1: PLM batch search (async — network)
    # ------------------------------------------------------------------

    @action('searchPlmMatches', async_=True)
    def _act_search_plm_matches(self, _ctx, data):
        """Search PLM for each component and drawing, fetch last STEP date.

        components: [{entityToken, name, lineageUrn, plmTargetUrn}]
        drawings:   [{fileId, name, linkedUrn}]
        revision:   1|2|3

        Returns matchResults keyed by entityToken and drawingResults keyed by fileId.
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err

        components = data.get('components') or []
        drawings   = data.get('drawings')   or []
        revision   = int(data.get('revision') or 1)

        from ...services import search as _search
        from ...services import attachments as _att
        client = self._client(ctx)

        ws_id = entitlements.get_workspace_id(
            'CW_COMPONENTS', self._app,
            str(config.WORKSPACE_IDS.get('components', '57')))
        ws_id_str = str(ws_id)

        def _search_one(urn_or_name, is_urn):
            """Search PLM and return list of parsed item dicts."""
            try:
                if is_urn:
                    tail = urn_or_name.rsplit(':', 1)[-1] if ':' in urn_or_name else urn_or_name
                    query = tail
                else:
                    query = f'"{urn_or_name}" AND (workspaceId={ws_id_str})'
                result, err = _search.search_results(
                    client, [query], revision=revision, limit=10, pre_formatted=True)
                if err or not result:
                    return []
                return [_parse_search_item(it, ws_id_str)
                        for it in (result.get('items') or [])
                        if _parse_search_item(it, ws_id_str)]
            except Exception:
                return []

        def _enrich_match(match):
            """Fetch thumbnail, lock, lifecycle, last STEP attachment for one matched item."""
            if not match:
                return match
            ws  = match['workspaceId']
            iid = match['itemId']
            try:
                self_link = f'/api/v3/workspaces/{ws}/items/{iid}'
                _, thumb, locked, lc, _ = _fetch_item_full(client, self_link)
                match['thumbnail'] = thumb
                match['locked']    = locked
                if lc and not match.get('lifecycle'):
                    match['lifecycle'] = lc
            except Exception:
                pass
            try:
                last = _fetch_last_step_attachment(client, ws, iid)
                match['lastStep'] = last
            except Exception:
                match['lastStep'] = None
            return match

        # ── Build search tasks ─────────────────────────────────────────
        # Each task: (key, search_term, is_urn)
        # Keys: entityToken for components, fileId for drawings
        tasks = []

        # Components: prefer lineageUrn, fall back to name search on plmTargetUrn
        # Group by plmTargetUrn so each unique PLM target is only searched once
        urn_to_tokens = {}   # plmTargetUrn → [entityToken, ...]
        name_tasks    = []   # (entityToken, name) for components without URN
        for comp in components:
            token      = comp.get('entityToken', '')
            target_urn = comp.get('plmTargetUrn')
            name       = comp.get('name', '')
            if target_urn:
                urn_to_tokens.setdefault(target_urn, []).append(token)
            else:
                name_tasks.append((token, name))

        for urn, tokens in urn_to_tokens.items():
            tasks.append(('urn', urn, tokens, True))
        for token, name in name_tasks:
            tasks.append(('name', name, [token], False))

        # Drawings
        for drw in drawings:
            fid        = drw.get('fileId', '')
            linked_urn = drw.get('linkedUrn', '')
            drw_name   = drw.get('name', '')
            if linked_urn:
                tasks.append(('drawing_urn', linked_urn, [fid], True))
            elif drw_name:
                tasks.append(('drawing_name', drw_name, [fid], False))

        # ── Execute searches concurrently ──────────────────────────────
        match_results  = {}   # entityToken → best matching PLM item (or None)
        drawing_results = {}  # fileId → best matching PLM item (or None)
        enrichment_queue = [] # items that need thumbnail/lastStep fetch

        def _run_task(task):
            kind, term, keys, is_urn = task
            items = _search_one(term, is_urn)
            # Pick best: prefer latestRelease, then workingVersion, then first
            best = None
            for it in items:
                if it:
                    if not best or (it.get('latestRelease') and not best.get('latestRelease')):
                        best = it
            return kind, keys, best

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_run_task, t): t for t in tasks}
            for fut in as_completed(futures, timeout=60):
                try:
                    kind, keys, best = fut.result()
                    for key in keys:
                        if kind.startswith('drawing'):
                            drawing_results[key] = best
                        else:
                            match_results[key] = best
                except Exception:
                    pass

        # ── Enrich unique matched items (thumbnail + last STEP) ────────
        unique_items = {}   # (ws, itemId) → match dict
        for v in list(match_results.values()) + list(drawing_results.values()):
            if v:
                k = (v['workspaceId'], v['itemId'])
                unique_items[k] = v

        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(_enrich_match, item): item
                       for item in unique_items.values()}
            for fut in as_completed(futures, timeout=30):
                try:
                    fut.result()
                except Exception:
                    pass

        return {
            'success':        True,
            'matchResults':   match_results,
            'drawingResults': drawing_results,
        }

    # ------------------------------------------------------------------
    # Manual match override search (async: network)
    # ------------------------------------------------------------------

    @action('searchManualMatch', async_=True)
    def _act_search_manual_match(self, _ctx, data):
        """Search PLM for a user-typed query to override a row's auto-match.

        Returns up to 15 candidate items from the Components workspace,
        defaulting to revision=2 (all) so users can find any version.
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err

        query    = str(data.get('query') or '').strip()
        revision = int(data.get('revision') or 2)

        if not query:
            return {'success': False, 'error': 'Enter a search term.'}

        from ...services import search as _search
        client = self._client(ctx)

        ws_id = entitlements.get_workspace_id(
            'CW_COMPONENTS', self._app,
            str(config.WORKSPACE_IDS.get('components', '57')))
        ws_id_str = str(ws_id)

        result, fetch_err = _search.search_results(
            client, [query], revision=revision, limit=15, pre_formatted=True)
        if fetch_err:
            return {'success': True, 'candidates': [], 'warning': fetch_err}

        candidates = []
        for it in (result.get('items') or []):
            parsed = _parse_search_item(it, ws_id_str)
            if parsed:
                candidates.append(parsed)

        return {'success': True, 'candidates': candidates}

    # ------------------------------------------------------------------
    # Phase 2a: STEP export (sync — Fusion API, main thread)
    # ------------------------------------------------------------------

    @action('runStepExports')
    def _act_run_step_exports(self, _ctx, data):
        """Export STEP files for selected components.

        exportItems: [{entityToken, name, isRoot}]
          isRoot=True  → full assembly STEP
          isRoot=False → individual component STEP (probes Fusion API)

        Returns files list [{name, path, size, resourceName, entityToken}]
        plus a probeLog of what API paths were tried for individual components.
        """
        export_items = data.get('exportItems') or []
        if not export_items:
            return {'success': False, 'error': 'No components selected for STEP export.'}

        design = None
        try:
            design = adsk.fusion.Design.cast(self._app.activeProduct)
        except Exception:
            pass
        if not design:
            try:
                design = adsk.fusion.Design.cast(
                    self._app.activeDocument.products.itemByProductType('DesignProductType'))
            except Exception:
                pass
        if not design:
            return {'success': False, 'error': 'No active Fusion design.'}

        export_mgr = design.exportManager
        out_dir    = tempfile.mkdtemp(prefix='plm_step_')
        files      = []
        probe_log  = []
        total      = len(export_items)

        # Native progress dialog — works even when the WebView is unresponsive
        # because it runs at the OS/Fusion level, not inside the palette.
        progress = None
        try:
            progress = self._app.userInterface.createProgressDialog()
            progress.isCancelButtonShown = True
            progress.show('Export to PLM — STEP',
                          'Preparing export…', 0, total, 1)
            adsk.doEvents()
        except Exception:
            progress = None

        for idx, item in enumerate(export_items):
            token   = item.get('entityToken', '')
            name    = item.get('name', '') or 'component'
            is_root = bool(item.get('isRoot'))

            # Update native progress dialog and check for cancellation
            try:
                if progress:
                    if progress.wasCancelled:
                        progress.hide()
                        return {'success': False,
                                'error': 'Export cancelled by user.',
                                'files': files, 'probeLog': probe_log}
                    label = 'Assembly' if is_root else f'{idx + 1} of {total}: {name}'
                    progress.message      = f'Exporting STEP — {label}'
                    progress.progressValue = idx
                adsk.doEvents()   # yield → lets WebView re-render between exports
            except Exception:
                pass

            res_name = _step_resource(name)
            fname    = f'{res_name}.step'
            out_path = os.path.join(out_dir, fname)
            out_fwd  = out_path.replace('\\', '/')

            if is_root:
                # Full assembly STEP — straightforward
                try:
                    options = export_mgr.createSTEPExportOptions(out_fwd)
                    export_mgr.execute(options)
                    if os.path.isfile(out_path):
                        files.append({
                            'name':         fname,
                            'path':         out_path,
                            'size':         os.path.getsize(out_path),
                            'resourceName': res_name,
                            'entityToken':  token,
                        })
                    else:
                        return {'success': False,
                                'error': f'Assembly STEP export produced no file.'}
                except Exception as e:
                    return {'success': False, 'error': f'Assembly STEP export failed: {e}'}

            else:
                # Individual component STEP — probe API options
                exported = False

                # Probe STEPExportOptions properties (runs once per export session)
                try:
                    probe_opts = export_mgr.createSTEPExportOptions(
                        os.path.join(out_dir, '_probe.step').replace('\\', '/'))
                    for attr in ('isOneFilePerComponent', 'component', 'occurrence',
                                 'occurrences', 'exportBody'):
                        val = getattr(probe_opts, attr, 'ATTR_MISSING')
                        entry = f'STEPExportOptions.{attr}={val!r}'
                        probe_log.append(entry)
                        _log.log(f'[ExportToPlm] {entry}')   # visible in Text Commands
                except Exception as e:
                    probe_log.append(f'createSTEPExportOptions probe error: {e}')
                    _log.log(f'[ExportToPlm] probe error: {e}')

                # Attempt 1: set component on options if property exists
                comp_obj = None
                for comp in design.allComponents:
                    if getattr(comp, 'entityToken', '') == token:
                        comp_obj = comp
                        break

                if comp_obj:
                    try:
                        options = export_mgr.createSTEPExportOptions(out_fwd)
                        if hasattr(options, 'component'):
                            options.component = comp_obj
                            export_mgr.execute(options)
                            if os.path.isfile(out_path):
                                exported = True
                                probe_log.append(f'Attempt1 (options.component) SUCCESS for {name}')
                    except Exception as e:
                        probe_log.append(f'Attempt1 failed: {e}')

                    # Attempt 2: activate an occurrence of the component, export, restore
                    if not exported:
                        occ_activated = None
                        try:
                            for i in range(design.rootComponent.allOccurrences.count):
                                occ = design.rootComponent.allOccurrences.item(i)
                                if (occ.component
                                        and getattr(occ.component, 'entityToken', '') == token):
                                    occ_activated = occ
                                    break
                            if occ_activated:
                                # Try activating the occurrence context
                                try:
                                    occ_activated.activate()
                                except Exception as ae:
                                    probe_log.append(f'occ.activate() error: {ae}')

                                try:
                                    options = export_mgr.createSTEPExportOptions(out_fwd)
                                    export_mgr.execute(options)
                                    if os.path.isfile(out_path):
                                        exported = True
                                        probe_log.append(f'Attempt2 (activate) SUCCESS for {name}')
                                except Exception as e:
                                    probe_log.append(f'Attempt2 export failed: {e}')

                                # Restore to root context
                                try:
                                    design.rootComponent.activate()
                                except Exception:
                                    pass
                        except Exception as e:
                            probe_log.append(f'Attempt2 setup failed: {e}')

                    # Attempt 3: open the component's own Fusion file if external
                    if not exported and comp_obj:
                        try:
                            fid = _cad.try_get_component_file_id(comp_obj)
                            if fid:
                                df = self._app.data.findFileById(fid)
                                if df:
                                    opened_doc = self._app.documents.open(df, False)
                                    if opened_doc:
                                        try:
                                            d2 = adsk.fusion.Design.cast(
                                                opened_doc.products.itemByProductType(
                                                    'DesignProductType'))
                                            if d2:
                                                opts2 = d2.exportManager.createSTEPExportOptions(out_fwd)
                                                d2.exportManager.execute(opts2)
                                                if os.path.isfile(out_path):
                                                    exported = True
                                                    probe_log.append(
                                                        f'Attempt3 (open external file) SUCCESS for {name}')
                                        finally:
                                            try:
                                                opened_doc.close(False)
                                            except Exception:
                                                pass
                        except Exception as e:
                            probe_log.append(f'Attempt3 failed: {e}')

                if exported and os.path.isfile(out_path):
                    files.append({
                        'name':         fname,
                        'path':         out_path,
                        'size':         os.path.getsize(out_path),
                        'resourceName': res_name,
                        'entityToken':  token,
                    })
                else:
                    probe_log.append(f'All attempts failed for "{name}" — skipping')
                    _log.log(f'[ExportToPlm] Could not export individual STEP for "{name}". '
                             f'Probe log: {probe_log}')

        try:
            if progress:
                progress.hide()
        except Exception:
            pass

        if not files:
            return {'success': False,
                    'error': ('STEP export produced no files. '
                              'Assembly export may have failed, or all individual '
                              'component exports were unsuccessful.'),
                    'probeLog': probe_log}

        return {'success': True, 'files': files, 'probeLog': probe_log}

    # ------------------------------------------------------------------
    # Phase 2b: PDF export (sync — Fusion API, main thread)
    # ------------------------------------------------------------------

    @action('runPdfExports')
    def _act_run_pdf_exports(self, _ctx, data):
        """Export selected drawing documents as PDF files."""
        drawing_file_ids = data.get('drawingFileIds') or []
        if not drawing_file_ids:
            return {'success': False, 'error': 'No drawings selected.'}

        out_dir = tempfile.mkdtemp(prefix='plm_pdf_')
        files   = []

        for fid in drawing_file_ids:
            doc = None
            try:
                for i in range(self._app.documents.count):
                    d  = self._app.documents.item(i)
                    df = getattr(d, 'dataFile', None)
                    if df and getattr(df, 'id', '') == fid:
                        doc = d
                        break
            except Exception:
                pass
            if not doc:
                return {'success': False,
                        'error': f'Drawing document not found (id={fid}).'}

            raw_name   = re.sub(r'\.[^.]+$', '', getattr(doc, 'name', '') or '').strip() or 'drawing'
            res_name   = _pdf_resource(raw_name)
            fname      = f'{res_name}.pdf'
            out_path   = os.path.join(out_dir, fname)
            exported   = False

            adsk.doEvents()   # yield between drawings so WebView can breathe

            for attempt, mgr_source in enumerate([
                lambda: doc.exportManager,
                lambda: next((doc.products.item(j).exportManager
                              for j in range(doc.products.count)
                              if 'Drawing' in (getattr(doc.products.item(j), 'objectType', '') or '')
                              and hasattr(doc.products.item(j), 'exportManager')),
                             None),
            ]):
                try:
                    mgr = mgr_source()
                    if not mgr:
                        continue
                    opts = mgr.createPDFExportOptions(out_path.replace('\\', '/'))
                    mgr.execute(opts)
                    if os.path.isfile(out_path):
                        exported = True
                        break
                except Exception as e:
                    _log.log(f'[ExportToPlm] PDF attempt {attempt+1} failed: {e}')

            if not exported:
                return {'success': False,
                        'error': (f'PDF export failed for "{raw_name}". '
                                  'Ensure the drawing is saved.')}
            files.append({
                'name':         fname,
                'path':         out_path,
                'size':         os.path.getsize(out_path),
                'resourceName': res_name,
                'fileId':       fid,
            })

        return {'success': True, 'files': files}

    # ------------------------------------------------------------------
    # Phase 2c: Upload (async — network)
    # ------------------------------------------------------------------

    @action('uploadFiles', async_=True)
    def _act_upload_files(self, _ctx, data):
        """Upload STEP and PDF files to their matched PLM component items.

        uploads: [{
            workspaceId, itemId, targetTitle, targetVersion,
            files: [{name, path, size, resourceName, fileType}]
        }]
        comment: str
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err

        uploads = data.get('uploads') or []
        comment = str(data.get('comment') or '').strip()

        if not uploads:
            return {'success': False, 'error': 'Nothing to upload.'}

        from ...services import attachments as _att
        client = self._client(ctx)

        tenant       = ctx.tenant or ''
        tenant_upper = tenant.upper()
        target_results = []

        for upload in uploads:
            workspace_id   = str(upload.get('workspaceId') or '')
            item_id        = str(upload.get('itemId')      or '')
            target_title   = str(upload.get('targetTitle') or item_id)
            target_version = str(upload.get('targetVersion') or '')
            files_to_up    = upload.get('files') or []

            if not workspace_id or not item_id:
                continue

            # Fetch existing attachments once per target for version-bump
            existing_atts, _ = _att.list_attachments(client, workspace_id, item_id)
            existing_atts    = existing_atts or []

            file_results = []
            last_att_id  = None

            for f in files_to_up:
                file_path     = str(f.get('path') or '')
                file_name     = str(f.get('name') or os.path.basename(file_path))
                resource_name = str(f.get('resourceName') or file_name)
                file_type     = str(f.get('fileType') or 'step')  # 'step' or 'pdf'

                if not file_path or not os.path.isfile(file_path):
                    file_results.append({'name': file_name, 'success': False,
                                          'error': 'File not found on disk.'})
                    continue

                try:
                    file_size  = os.path.getsize(file_path)
                    file_bytes = open(file_path, 'rb').read()

                    # Version-bump detection
                    suffix_check = _STEP_SUFFIX if file_type == 'step' else _PDF_SUFFIX
                    existing_id  = None
                    for att in existing_atts:
                        aname = att.get('name', '')
                        if (isinstance(aname, str)
                                and suffix_check in aname.lower()
                                and aname.lower().endswith(
                                    '.step' if file_type == 'step' else '.pdf')):
                            existing_id = att.get('id')
                            break

                    upload_info, up_err = _att.request_upload(
                        client, workspace_id, item_id,
                        file_name, resource_name, file_size, existing_id,
                        comment=comment)
                    if up_err:
                        file_results.append({'name': file_name, 'success': False,
                                              'error': up_err})
                        continue

                    s3_err = _att.upload_to_s3(
                        client, upload_info['s3_url'],
                        upload_info['extra_headers'], file_bytes)
                    if s3_err:
                        file_results.append({'name': file_name, 'success': False,
                                              'error': s3_err})
                        continue

                    version, ci_err = _att.checkin(
                        client, workspace_id, item_id, upload_info['attachment_id'])
                    if ci_err:
                        file_results.append({'name': file_name, 'success': False,
                                              'error': ci_err})
                        continue

                    last_att_id = upload_info['attachment_id']
                    file_results.append({
                        'name':         file_name,
                        'success':      True,
                        'version':      version,
                        'isNewVersion': existing_id is not None,
                    })

                except Exception as exc:
                    file_results.append({'name': file_name, 'success': False,
                                          'error': str(exc)})

            success_count = sum(1 for r in file_results if r.get('success'))
            item_url = ''
            if last_att_id and tenant:
                i_urn = (f'urn:adsk.plm:tenant.workspace.item:'
                         f'{tenant_upper}.{workspace_id}.{item_id}')
                f_urn = (f'urn:adsk.plm:tenant.workspace.item.attachment:'
                         f'{tenant_upper}.{workspace_id}.{item_id}.{last_att_id}')
                item_url = (f'https://{tenant}.autodeskplm360.net/plm/fileViewer'
                            f'?itemUrn={i_urn}&fileUrn={f_urn}&vectorPdf=false')
            elif tenant:
                item_url = (f'https://{tenant}.autodeskplm360.net'
                            f'/plm/workspaces/{workspace_id}/items/{item_id}')

            target_results.append({
                'targetTitle':   target_title,
                'targetVersion': target_version,
                'workspaceId':   workspace_id,
                'itemId':        item_id,
                'fileResults':   file_results,
                'successCount':  success_count,
                'totalCount':    len(file_results),
                'itemUrl':       item_url,
            })

        total_ok    = sum(r.get('successCount', 0) for r in target_results)
        total_files = sum(r.get('totalCount',   0) for r in target_results)

        return {
            'success':           total_ok > 0,
            'targetResults':     target_results,
            'totalSuccessCount': total_ok,
            'totalCount':        total_files,
        }
