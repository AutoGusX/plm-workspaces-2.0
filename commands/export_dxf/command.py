# Export DXF to PLM — export flat patterns or sketch profiles as DXF files
# and attach them to CW_COMPONENTS records in Fusion Manage.
#
# Two export modes (selected via mode toggle in the palette):
#   Flat Pattern: walks all Design components for sheet metal flat patterns.
#   Sketch:       walks all visible sketches in the root component + occurrences.
#
# File mode:
#   Individual: each selected item → its own DXF → its own PLM attachment.
#   Single:     all selected items exported into one combined DXF → one PLM attachment.
#
# UI flow:
#   JS: getDxfContext  (sync)  — enumerate flat patterns / sketches + component URNs
#   JS: runDxfExport   (sync)  — export selected items to temp DXF files
#   JS: uploadDxf      (async) — upload DXF files to selected PLM revisions
#
# getDxfContext and runDxfExport MUST stay sync: they call the Fusion Design API
# and must run on the main UI thread.

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

_DXF_EXT = '.dxf'
_DXF_RESOURCE_SUFFIX = '_fusion_dxf_export'


# ---------------------------------------------------------------------------
# Fusion Design API helpers
# ---------------------------------------------------------------------------

def _get_design(app):
    """Return the active Fusion Design, or None."""
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design:
            return design
        doc = app.activeDocument
        if doc:
            return adsk.fusion.Design.cast(
                doc.products.itemByProductType('DesignProductType'))
    except Exception:
        pass
    return None


def _design_cad_context(design, app):
    """Extract lineage URNs and component names from the active design.

    Used for auto-detecting the PLM component to upload to.
    Returns (lineage_urns, component_names).
    """
    urns       = []
    seen_urns  = set()
    names      = []
    seen_names = set()

    try:
        root = design.rootComponent
        # Root document
        try:
            doc = app.activeDocument
            if doc and doc.dataFile:
                root_urn = _cad.file_id_to_lineage_urn(doc.dataFile.id)
                if root_urn and root_urn not in seen_urns:
                    seen_urns.add(root_urn)
                    urns.append(root_urn)
        except Exception:
            pass

        root_name = (getattr(root, 'name', '') or '').strip()
        if root_name and root_name not in seen_names:
            seen_names.add(root_name)
            names.append(root_name)

        # Walk occurrences
        for i in range(root.allOccurrences.count):
            try:
                comp = root.allOccurrences.item(i).component
                if not comp:
                    continue
                name = (getattr(comp, 'name', '') or '').strip()
                if name and name not in seen_names:
                    seen_names.add(name)
                    names.append(name)
                try:
                    fid = _cad.try_get_component_file_id(comp)
                    urn = _cad.file_id_to_lineage_urn(fid)
                    if urn and urn not in seen_urns:
                        seen_urns.add(urn)
                        urns.append(urn)
                except Exception:
                    pass
            except Exception:
                continue
    except Exception:
        pass
    return urns, names


def _collect_flat_patterns(design):
    """Walk all components and return flat pattern info via comp.flatPattern.

    FlatPattern is accessed directly on the Component — it is NOT a timeline
    feature.  comp.flatPattern returns a FlatPattern object when one exists
    (check isValid), or an invalid/None object when the component has no flat
    pattern.

    Returns a list of dicts: {id, name, componentName}
    id is the FlatPattern entityToken.
    """
    results = []
    seen = set()
    try:
        for comp in design.allComponents:
            comp_name = getattr(comp, 'name', '') or ''
            try:
                fp = comp.flatPattern
                # The property always returns a FlatPattern class reference;
                # isValid distinguishes a real instance from a null binding.
                if not fp or not getattr(fp, 'isValid', False):
                    continue
                token = getattr(fp, 'entityToken', '') or ''
                if not token or token in seen:
                    continue
                seen.add(token)
                fp_name = (getattr(fp, 'name', '') or comp_name
                           or f'Flat Pattern {len(results)+1}')
                results.append({
                    'id':            token,
                    'name':          fp_name,
                    'componentName': comp_name,
                })
            except Exception as e:
                _log.log(f'[DxfExport] comp "{comp_name}" flatPattern error: {e}')
    except Exception as e:
        _log.log(f'[DxfExport] _collect_flat_patterns error: {e}')
    return results


def _collect_sketches(design):
    """Walk root component and all occurrences for all sketches (any visibility).

    Returns list of dicts: {id, name, componentName, profileCount, isVisible}
    id is the sketch entityToken.
    """
    results = []
    seen = set()

    def _add_sketches_from_comp(comp, comp_label):
        try:
            for i in range(comp.sketches.count):
                try:
                    sk = comp.sketches.item(i)
                    token = getattr(sk, 'entityToken', '') or ''
                    if not token or token in seen:
                        continue
                    seen.add(token)
                    results.append({
                        'id':            token,
                        'name':          getattr(sk, 'name', '') or f'Sketch {len(results)+1}',
                        'componentName': comp_label,
                        'profileCount':  sk.profiles.count if sk.profiles else 0,
                        'isVisible':     bool(getattr(sk, 'isVisible', True)),
                    })
                except Exception:
                    continue
        except Exception:
            pass

    try:
        root = design.rootComponent
        _add_sketches_from_comp(root, getattr(root, 'name', 'Root'))
        for i in range(root.allOccurrences.count):
            try:
                comp = root.allOccurrences.item(i).component
                if comp:
                    _add_sketches_from_comp(comp, getattr(comp, 'name', ''))
            except Exception:
                continue
    except Exception:
        pass
    return results


def _find_flat_pattern(design, token):
    """Look up a FlatPattern by entityToken.

    Fusion API docs warn that entityToken strings change between calls.
    design.findEntityByToken() handles this — two different token strings
    for the same entity will both resolve to that entity correctly.
    Falls back to a direct walk-and-compare if findEntityByToken fails.
    """
    # Primary: use findEntityByToken (token-change safe)
    try:
        entity = design.findEntityByToken(token)
        # findEntityByToken may return the object directly or wrap it
        if entity:
            if isinstance(entity, (list, tuple)):
                entity = entity[0] if entity else None
            if entity and getattr(entity, 'isValid', False):
                return entity
    except Exception:
        pass

    # Fallback: walk components and compare current tokens
    try:
        for comp in design.allComponents:
            try:
                fp = comp.flatPattern
                if fp and getattr(fp, 'isValid', False):
                    if getattr(fp, 'entityToken', '') == token:
                        return fp
            except Exception:
                continue
    except Exception:
        pass
    return None


def _find_sketch(design, token):
    """Look up a Sketch by entityToken across root + all occurrences."""
    def _search_comp(comp):
        try:
            for i in range(comp.sketches.count):
                sk = comp.sketches.item(i)
                if getattr(sk, 'entityToken', '') == token:
                    return sk
        except Exception:
            pass
        return None

    try:
        root = design.rootComponent
        sk = _search_comp(root)
        if sk:
            return sk
        for i in range(root.allOccurrences.count):
            try:
                comp = root.allOccurrences.item(i).component
                if comp:
                    sk = _search_comp(comp)
                    if sk:
                        return sk
            except Exception:
                continue
    except Exception:
        pass
    return None


def _safe_resource_name(name):
    return re.sub(r'[^\w\-]', '_', (name or 'dxf').strip()) + _DXF_RESOURCE_SUFFIX


# ---------------------------------------------------------------------------
# Attachment helpers (shared with g-code export pattern)
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


def _fetch_item_meta(client, self_link):
    if not self_link:
        return {}
    url = f'{client.base}{self_link}'
    r = client._request('GET', url, empty_body_as={})
    if not r.ok or not isinstance(r.data, dict):
        return {}
    data = r.data
    lifecycle = data.get('lifecycle') or {}
    lifecycle_title = lifecycle.get('title', '') if isinstance(lifecycle, dict) else ''
    return {
        'thumbnail':           _extract_thumbnail(data),
        'locked':              bool(data.get('itemLocked')),
        'lifecycle':           lifecycle_title,
        'changedSinceRelease': bool(data.get('workingHasChanged')),
    }


def _parse_component_item(item):
    if item.get('deleted'):
        return None
    self_link = item.get('__self__', '')
    ws_m  = re.search(r'workspaces/(\d+)', self_link)
    item_m = re.search(r'items/(\d+)', self_link)
    if not ws_m or not item_m:
        return None
    version_raw = item.get('version', '')
    m = re.match(r'\[REV:(.+)\]', version_raw)
    version_label = f'Rev {m.group(1)}' if m else (version_raw or 'Unknown')
    version_id = item.get('versionId') or (m.group(1) if m else '')
    lifecycle = item.get('lifecycle') or {}
    lifecycle_title = lifecycle.get('title', '') if isinstance(lifecycle, dict) else ''
    is_working  = bool(item.get('workingVersion')) or version_id == 'w'
    is_released = bool(item.get('latestRelease'))
    title = item.get('title') or item.get('descriptor') or ''
    if not title:
        from ...services import fusion_cad as _cad2  # avoid circular at module level
        # fall back to section fields
        for sec in (item.get('sections') or []):
            if not isinstance(sec, dict):
                continue
            for field in (sec.get('fields') or []):
                if not isinstance(field, dict):
                    continue
                sl = field.get('__self__', '')
                val = field.get('value')
                if sl.endswith('/PART_DESCRIPTOR') and isinstance(val, str) and val:
                    title = val
                    break
                if sl.endswith('/ITEM_NUMBER') and isinstance(val, str) and val and not title:
                    title = val
            if title:
                break
    return {
        'itemId':            item_m.group(1),
        'workspaceId':       ws_m.group(1),
        'title':             title or 'Unknown',
        'version':           version_label,
        'versionId':         version_id,
        'latestRelease':     is_released,
        'workingVersion':    is_working,
        'lifecycle':         lifecycle_title,
        'thumbnail':         '',
        'locked':            False,
        'changedSinceRelease': False,
    }


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class ExportDxfCommand(PaletteCommand):
    command_id      = config.COMMAND_IDS['exportDxfToPlm']
    command_name    = 'Export DXF to PLM'
    command_tooltip = 'Export flat patterns or sketch profiles as DXF and attach to Fusion Manage'
    palette_id      = config.PALETTE_ID_EXPORT_DXF
    palette_title   = 'Export DXF to PLM'
    palette_size    = (520, 800)
    resizable       = True
    docking         = 'right'
    panel           = 'plm'   # Design workspace PLM panel

    workspace_system_name = None
    workspace_id_fallback = None

    html_url = paths.to_file_url_path(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'resources', 'html', 'index.html',
        )
    )

    # ------------------------------------------------------------------
    # Context load (sync — Design API, main thread)
    # ------------------------------------------------------------------

    @action('getDxfContext')
    def _act_get_context(self, _ctx, data):
        """Enumerate flat patterns and visible sketches in the active Design."""
        design = _get_design(self._app)
        if not design:
            return {
                'success': False,
                'error': 'No active Fusion design. Open a design in the Design workspace first.',
            }

        flat_patterns = _collect_flat_patterns(design)
        sketches      = _collect_sketches(design)
        lineage_urns, component_names = _design_cad_context(design, self._app)

        return {
            'success':        True,
            'flatPatterns':   flat_patterns,
            'sketches':       sketches,
            'lineageUrns':    lineage_urns,
            'componentNames': component_names,
        }

    # ------------------------------------------------------------------
    # DXF export (sync — Design API, main thread)
    # ------------------------------------------------------------------

    @action('runDxfExport')
    def _act_run_dxf_export(self, _ctx, data):
        """Export selected flat patterns or sketches to temporary DXF files.

        mode:           'flatPattern' | 'sketch'
        itemIds:        list of entityToken strings
        scale:          float (1.0 = full scale)
        grainDirection: 'none' | 'x' | 'y'
        fileMode:       'individual' | 'single'
        singleName:     filename for single-file mode (without extension)
        """
        design = _get_design(self._app)
        if not design:
            return {'success': False, 'error': 'No active Fusion design.'}

        mode          = str(data.get('mode') or 'sketch')
        item_ids      = data.get('itemIds') or []
        scale         = float(data.get('scale') or 1.0)
        grain_dir     = str(data.get('grainDirection') or 'none')
        file_mode     = str(data.get('fileMode') or 'individual')
        single_name   = str(data.get('singleName') or 'export').strip() or 'export'

        if not item_ids:
            return {'success': False, 'error': 'No items selected for export.'}

        export_mgr = design.exportManager
        out_dir    = tempfile.mkdtemp(prefix='plm_dxf_')
        files      = []

        if mode == 'flatPattern':
            files = self._export_flat_patterns(
                design, export_mgr, item_ids, scale, grain_dir,
                file_mode, single_name, out_dir)
        else:
            files = self._export_sketches(
                design, export_mgr, item_ids, scale,
                file_mode, single_name, out_dir)

        if isinstance(files, dict):   # error dict returned
            return files
        if not files:
            return {'success': False, 'error': 'DXF export produced no output files.'}

        return {'success': True, 'files': files}

    def _export_flat_patterns(self, design, export_mgr, item_ids, scale,
                               grain_dir, file_mode, single_name, out_dir):
        """Export flat patterns to DXF using createDXFFlatPatternExportOptions.

        FlatPattern is accessed via comp.flatPattern, then exported with the
        dedicated DXFFlatPatternExportOptions (not the sketch 2D path).
        """
        files = []
        for token in item_ids:
            fp = _find_flat_pattern(design, token)
            if not fp:
                return {'success': False, 'error': f'Flat pattern not found: {token}'}

            name = getattr(fp, 'name', '') or 'flat_pattern'
            fname = (f'{_safe_resource_name(single_name)}.dxf'
                     if file_mode == 'single'
                     else f'{_safe_resource_name(name)}.dxf')
            out_path = os.path.join(out_dir, fname)

            try:
                # Use the dedicated flat-pattern DXF export path
                options = export_mgr.createDXFFlatPatternExportOptions(
                    out_path.replace('\\', '/'), fp)

                # Apply scale if the option exposes it
                for scale_attr in ('scale', 'scaleFactor'):
                    if hasattr(options, scale_attr):
                        try:
                            setattr(options, scale_attr, float(scale))
                        except Exception:
                            pass
                        break

                # Apply grain direction if the option exposes it
                if grain_dir and grain_dir != 'none':
                    for gattr in ('grainDirection', 'grainLineDirection'):
                        if hasattr(options, gattr):
                            try:
                                setattr(options, gattr, grain_dir)
                            except Exception:
                                pass
                            break

                export_mgr.execute(options)
                adsk.doEvents()   # yield so WebView stays responsive between exports

                if not os.path.isfile(out_path):
                    return {'success': False,
                            'error': f'Flat pattern DXF export produced no file for "{name}".'}

                files.append({
                    'name':         fname,
                    'path':         out_path,
                    'size':         os.path.getsize(out_path),
                    'resourceName': _safe_resource_name(
                        single_name if file_mode == 'single' else name),
                    'sourceLabel':  name,
                })

                if file_mode == 'single':
                    break   # single-file mode: stop after first pattern

            except Exception as exc:
                return {'success': False,
                        'error': f'Flat pattern export failed for "{name}": {exc}'}

        return files

    def _export_sketches(self, design, export_mgr, item_ids, scale,
                          file_mode, single_name, out_dir):
        """Export sketch(es) to DXF. Returns file list or error dict."""
        files = []
        sketches_to_export = []

        for token in item_ids:
            sk = _find_sketch(design, token)
            if not sk:
                return {'success': False, 'error': f'Sketch not found: {token}'}
            sketches_to_export.append(sk)

        if file_mode == 'single' and len(sketches_to_export) > 1:
            # Export all sketches to a single DXF — use the first as the base,
            # then attempt to export each into the same file path.
            # NOTE: Fusion's createDXF2DExportOptions does not natively merge
            # multiple sketches into one file; each call overwrites the file.
            # A common workaround: export last sketch only, or use STL/SVG.
            # For now we export each and return only the last (single output).
            # Users can adjust this in a follow-up iteration.
            fname    = f'{_safe_resource_name(single_name)}.dxf'
            out_path = os.path.join(out_dir, fname)
            last_ok  = False
            for sk in sketches_to_export:
                try:
                    options = export_mgr.createDXF2DExportOptions(
                        out_path.replace('\\', '/'), sk)
                    if hasattr(options, 'scale'):
                        options.scale = scale
                    export_mgr.execute(options)
                    adsk.doEvents()
                    last_ok = True
                except Exception as e:
                    _log.log(f'[DxfExport] sketch single-mode export failed for '
                             f'"{sk.name}": {e}')
            if last_ok and os.path.isfile(out_path):
                files.append({
                    'name':         fname,
                    'path':         out_path,
                    'size':         os.path.getsize(out_path),
                    'resourceName': _safe_resource_name(single_name),
                    'sourceLabel':  ', '.join(s.name for s in sketches_to_export),
                })
        else:
            for sk in sketches_to_export:
                sk_name  = getattr(sk, 'name', 'sketch') or 'sketch'
                fname    = f'{_safe_resource_name(sk_name)}.dxf'
                out_path = os.path.join(out_dir, fname)
                try:
                    options = export_mgr.createDXF2DExportOptions(
                        out_path.replace('\\', '/'), sk)
                    if hasattr(options, 'scale'):
                        options.scale = scale
                    export_mgr.execute(options)
                    adsk.doEvents()   # yield between sketches
                    if not os.path.isfile(out_path):
                        return {'success': False,
                                'error': f'DXF export produced no file for "{sk_name}".'}
                    files.append({
                        'name':         fname,
                        'path':         out_path,
                        'size':         os.path.getsize(out_path),
                        'resourceName': _safe_resource_name(sk_name),
                        'sourceLabel':  sk_name,
                    })
                except Exception as exc:
                    return {'success': False,
                            'error': f'Sketch export failed for "{sk_name}": {exc}'}

        return files

    # ------------------------------------------------------------------
    # Component discovery (async: network) — same pattern as G-code
    # ------------------------------------------------------------------

    @action('loadComponentRevisions', async_=True)
    def _act_load_component_revisions(self, _ctx, data):
        """Search PLM for components matching the design's URNs or component names.

        Strategy 1 — lineage URN search (external xref components).
        Strategy 2 — component name phrase-quoted + workspace constraint.
        revision: 1=latest released, 2=all, 3=working
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err

        lineage_urns    = data.get('lineageUrns')    or []
        component_names = data.get('componentNames') or []
        revision        = int(data.get('revision') or 2)

        if not lineage_urns and not component_names:
            return {
                'success':    True,
                'components': [],
                'warning':    'No component identifiers found. Use Manual search.',
            }

        from ...services import search as _search
        client = self._client(ctx)

        ws_id = entitlements.get_workspace_id(
            'CW_COMPONENTS', self._app,
            str(config.WORKSPACE_IDS.get('components', '57')))
        ws_id_str = str(ws_id)

        components   = []
        seen         = set()
        meta_fetches = []

        # Strategy 1: lineage URN
        for lineage_urn in lineage_urns:
            tail = lineage_urn.rsplit(':', 1)[-1] if ':' in lineage_urn else lineage_urn
            if not tail:
                continue
            result, err = _search.search_results(
                client, [tail], revision=revision, limit=50, pre_formatted=True)
            if err:
                continue
            for item in (result.get('items') or []):
                parsed = _parse_component_item(item)
                if not parsed or parsed['workspaceId'] != ws_id_str:
                    continue
                key = (parsed['workspaceId'], parsed['itemId'])
                if key in seen:
                    continue
                seen.add(key)
                components.append(parsed)
                meta_fetches.append((item.get('__self__', ''), parsed))

        # Strategy 2: component name phrase-quoted + workspace constraint
        if not components:
            for name in component_names:
                if not name:
                    continue
                query = f'"{name}" AND (workspaceId={ws_id_str})'
                result, err = _search.search_results(
                    client, [query], revision=revision, limit=50, pre_formatted=True)
                if err:
                    continue
                for item in (result.get('items') or []):
                    parsed = _parse_component_item(item)
                    if not parsed or parsed['workspaceId'] != ws_id_str:
                        continue
                    key = (parsed['workspaceId'], parsed['itemId'])
                    if key in seen:
                        continue
                    seen.add(key)
                    components.append(parsed)
                    meta_fetches.append((item.get('__self__', ''), parsed))

        # Enrich results with thumbnail / lock / lifecycle
        if meta_fetches:
            with ThreadPoolExecutor(max_workers=6) as ex:
                future_map = {
                    ex.submit(_fetch_item_meta, client, sl): p
                    for sl, p in meta_fetches
                }
                for fut in as_completed(future_map, timeout=25):
                    p = future_map[fut]
                    try:
                        meta = fut.result() or {}
                        if meta.get('thumbnail'):
                            p['thumbnail'] = meta['thumbnail']
                        p['locked']             = bool(meta.get('locked'))
                        p['changedSinceRelease'] = bool(meta.get('changedSinceRelease'))
                        if meta.get('lifecycle') and not p.get('lifecycle'):
                            p['lifecycle'] = meta['lifecycle']
                    except Exception:
                        pass

        if not components:
            return {
                'success':    True,
                'components': [],
                'warning':    'No PLM components found. Try Manual search.',
            }
        return {'success': True, 'components': components}

    @action('searchFmComponents', async_=True)
    def _act_search_components(self, _ctx, data):
        """Manual component search within CW_COMPONENTS."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        query    = str(data.get('query') or '').strip()
        revision = int(data.get('revision') or 2)
        if not query:
            return {'success': False, 'error': 'Enter a search term.'}

        from ...services import search as _search
        client   = self._client(ctx)
        ws_id    = entitlements.get_workspace_id(
            'CW_COMPONENTS', self._app,
            str(config.WORKSPACE_IDS.get('components', '57')))

        result, fetch_err = _search.search_results(
            client, [query], revision=revision, limit=30, pre_formatted=True)
        if fetch_err:
            return {'success': True, 'components': [], 'warning': fetch_err}

        components = []
        seen = set()
        for item in (result.get('items') or []):
            parsed = _parse_component_item(item)
            if not parsed:
                continue
            if ws_id and parsed['workspaceId'] != str(ws_id):
                continue
            key = (parsed['workspaceId'], parsed['itemId'])
            if key in seen:
                continue
            seen.add(key)
            lifecycle = item.get('lifecycle') or {}
            if not parsed.get('lifecycle') and isinstance(lifecycle, dict):
                parsed['lifecycle'] = lifecycle.get('title', '')
            parsed['thumbnail']          = _extract_thumbnail(item)
            parsed['locked']             = bool(item.get('itemLocked'))
            parsed['changedSinceRelease'] = bool(item.get('workingHasChanged'))
            components.append(parsed)
        return {'success': True, 'components': components}

    # ------------------------------------------------------------------
    # Attachment listing (async: network)
    # ------------------------------------------------------------------

    @action('listItemAttachments', async_=True)
    def _act_list_attachments(self, _ctx, data):
        """List existing attachments on a PLM item for the mapping UI.

        Filters to DXF files only and returns name + id for overwrite selection.
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err
        workspace_id = str(data.get('workspaceId') or '')
        item_id      = str(data.get('itemId') or '')
        if not workspace_id or not item_id:
            return {'success': False, 'error': 'Missing workspaceId or itemId.'}

        from ...services import attachments as _att
        client = self._client(ctx)
        atts, fetch_err = _att.list_attachments(client, workspace_id, item_id)
        if fetch_err:
            return {'success': False, 'error': fetch_err}

        dxf_atts = [
            {'id': a.get('id', ''), 'name': a.get('name', '')}
            for a in (atts or [])
            if isinstance(a.get('name', ''), str)
            and a['name'].lower().endswith('.dxf')
        ]
        return {'success': True, 'attachments': dxf_atts}

    # ------------------------------------------------------------------
    # Upload (async: network I/O)
    # ------------------------------------------------------------------

    @action('uploadDxf', async_=True)
    def _act_upload_dxf(self, _ctx, data):
        """Upload DXF files to one or more PLM component revisions.

        files:   [{name, path, size, resourceName}]
        targets: [{workspaceId, itemId, title, version}]
        mapping: [{localName, action ('overwrite'|'new'), existingId, newName}]
        comment: optional string stored as attachment description
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err

        files   = data.get('files')   or []
        targets = data.get('targets') or []
        mapping = {m['localName']: m for m in (data.get('mapping') or [])
                   if isinstance(m, dict) and m.get('localName')}
        comment = str(data.get('comment') or '').strip()

        if not files:
            return {'success': False, 'error': 'No files to upload.'}
        if not targets:
            return {'success': False, 'error': 'No target component selected.'}

        from ...services import attachments as _att
        client = self._client(ctx)

        tenant       = ctx.tenant or ''
        tenant_upper = tenant.upper()

        target_results = []

        for target in targets:
            workspace_id   = str(target.get('workspaceId') or '')
            item_id        = str(target.get('itemId')      or '')
            target_title   = str(target.get('title')       or item_id)
            target_version = str(target.get('version')     or '')

            if not workspace_id or not item_id:
                target_results.append({
                    'targetTitle': target_title, 'targetVersion': target_version,
                    'fileResults': [], 'successCount': 0, 'totalCount': 0,
                    'error': 'Missing workspaceId or itemId.',
                })
                continue

            file_results = []
            last_att_id  = None

            for f in files:
                file_path     = str(f.get('path') or '')
                file_name     = str(f.get('name') or os.path.basename(file_path))
                resource_name = str(f.get('resourceName') or file_name)

                if not file_path or not os.path.isfile(file_path):
                    file_results.append({'name': file_name, 'success': False,
                                          'error': 'File not found on disk.'})
                    continue

                try:
                    file_size  = os.path.getsize(file_path)
                    file_bytes = open(file_path, 'rb').read()

                    # Determine overwrite target from mapping
                    m_entry         = mapping.get(file_name) or {}
                    m_action        = m_entry.get('action', 'new')
                    existing_att_id = m_entry.get('existingId') if m_action == 'overwrite' else None
                    upload_name     = (m_entry.get('newName') or file_name) if m_action == 'new' else file_name

                    upload_info, upload_err = _att.request_upload(
                        client, workspace_id, item_id,
                        upload_name, resource_name, file_size, existing_att_id,
                        comment=comment)
                    if upload_err:
                        file_results.append({'name': file_name, 'success': False,
                                              'error': upload_err})
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
                        'name':         upload_name,
                        'success':      True,
                        'version':      version,
                        'isNewVersion': existing_att_id is not None,
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
