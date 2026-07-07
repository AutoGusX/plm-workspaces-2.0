# Export G-code to PLM — post-process NC Programs and attach NC files to
# one or more CW_COMPONENTS records in Fusion Manage.
#
# UI flow (single page, one "Upload to PLM" button):
#   JS Step 1: runPostProcess (sync)  — post-process selected NC programs
#   JS Step 2: uploadGcode    (async) — upload NC files to each selected PLM revision
#
# Component auto-discovery: walks CAM setup.models → component dataFile →
# lineage URN → PLM search. Falls back to all referenced design components.
#
# runPostProcess MUST stay sync: nc.postProcess() calls the Fusion API and is
# only safe on the main UI thread.

import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import adsk.cam
import adsk.core

from ...core import config
from ...core import entitlements
from ...core import log as _log
from ...core import paths
from ...core.action_registry import action
from ...core.palette_base import PaletteCommand
from ...services import fusion_cad as _cad

_GCODE_SUFFIX = '_fusion_gcode_export.'
_GCODE_EXTS   = frozenset(['.nc', '.gcode', '.tap', '.cnc', '.eia', '.h', '.txt'])


def _make_resource_name(nc_name):
    safe = re.sub(r'[^\w\-]', '_', (nc_name or 'NCProgram').strip())
    return f'{safe}_fusion_gcode_export'


def _make_gcode_filename(nc_name, ext):
    return f'{_make_resource_name(nc_name)}.{ext}'


def _find_gcode_in(dir_path):
    """Return the first recognized G-code file in dir_path, or None."""
    try:
        for fname in sorted(os.listdir(dir_path)):
            fpath = os.path.join(dir_path, fname)
            if os.path.isfile(fpath):
                _, ext = os.path.splitext(fname)
                if ext.lower() in _GCODE_EXTS:
                    return fpath
    except Exception:
        pass
    return None


def _cam_setup_cad_context(cam, app):
    """Extract lineage URNs and component names from CAM setup models.

    Lineage URNs (primary): available for externally-saved (xref) components.
    Component names (fallback): always available; used for phrase-quoted PLM
    search when components are internal to the assembly document.

    Root-document fallback: when no component-level URN is found, the active
    document's lineage URN is used (single-body or fully-internal designs).

    Returns (lineage_urns, component_names) — both deduplicated lists.
    """
    urns       = []
    seen_urns  = set()
    names      = []
    seen_names = set()

    try:
        for si in range(cam.setups.count):
            setup = cam.setups.item(si)
            try:
                models = setup.models
            except Exception:
                continue
            for m in (models or []):
                comp = (getattr(m, 'parentComponent', None)
                        or getattr(m, 'component', None))
                if not comp:
                    continue

                # Component name — always available, used for fallback search.
                name = (getattr(comp, 'name', '') or '').strip()
                if name and name not in seen_names:
                    seen_names.add(name)
                    names.append(name)

                # Lineage URN — only resolvable for external (xref) components.
                try:
                    fid = _cad.try_get_component_file_id(comp)
                    urn = _cad.file_id_to_lineage_urn(fid)
                    if urn and urn not in seen_urns:
                        seen_urns.add(urn)
                        urns.append(urn)
                except Exception:
                    pass
    except Exception:
        pass

    # Root-document fallback for single-body or fully-internal designs.
    if not urns:
        try:
            doc = app.activeDocument
            if doc and doc.dataFile:
                root_urn = _cad.file_id_to_lineage_urn(doc.dataFile.id)
                if root_urn and root_urn not in seen_urns:
                    urns.append(root_urn)
        except Exception:
            pass

    return urns, names


def _section_field_value(item, field_id):
    """Extract a section field value by field ID suffix from item.sections."""
    for sec in (item.get('sections') or []):
        if not isinstance(sec, dict):
            continue
        for field in (sec.get('fields') or []):
            if not isinstance(field, dict):
                continue
            self_link = field.get('__self__', '')
            if self_link.endswith('/' + field_id):
                val = field.get('value')
                return val if isinstance(val, str) else ''
    return ''


def _extract_thumbnail(item):
    """Pull the DESIGN_THUMBNAIL S3 link from an item's sections (rich format)."""
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
    """Fetch thumbnail, lock, lifecycle for a compact-format item (lineage URN search).

    Makes one GET to the item endpoint to retrieve the full item dict that includes
    itemLocked, lifecycle, workingHasChanged, and sections (with DESIGN_THUMBNAIL).
    Returns a dict with 'thumbnail', 'locked', 'lifecycle', 'changedSinceRelease'.
    """
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
        'thumbnail':          _extract_thumbnail(data),
        'locked':             bool(data.get('itemLocked')),
        'lifecycle':          lifecycle_title,
        'changedSinceRelease': bool(data.get('workingHasChanged')),
    }


def _parse_component_item(item):
    """Extract display-ready fields from a PLM search-result item."""
    if item.get('deleted'):
        return None
    self_link = item.get('__self__', '')
    ws_m   = re.search(r'workspaces/(\d+)', self_link)
    item_m = re.search(r'items/(\d+)', self_link)
    if not ws_m or not item_m:
        return None

    version_raw = item.get('version', '')
    m = re.match(r'\[REV:(.+)\]', version_raw)
    version_label = f'Rev {m.group(1)}' if m else (version_raw or 'Unknown')
    # Compact format (lineage search) omits versionId; parse it from version string.
    version_id = item.get('versionId') or (m.group(1) if m else '')

    lifecycle = item.get('lifecycle') or {}
    lifecycle_title = lifecycle.get('title', '') if isinstance(lifecycle, dict) else ''

    # Compact format omits latestRelease/workingVersion; infer working from versionId.
    is_working  = bool(item.get('workingVersion')) or version_id == 'w'
    is_released = bool(item.get('latestRelease'))

    # The API returns two formats depending on query type:
    #   Rich (name search):        'title' = "EX-000009- - Brick", full sections/lifecycle
    #   Compact (lineage search):  'descriptor' = "EX-000009- - Brick", no title/sections
    # Try title first, descriptor second, then section fields as last resort.
    title = item.get('title') or item.get('descriptor') or ''
    if not title:
        part_desc = _section_field_value(item, 'PART_DESCRIPTOR')
        item_num  = _section_field_value(item, 'ITEM_NUMBER')
        if item_num and part_desc:
            title = f'{item_num} - {part_desc}'
        elif part_desc:
            title = part_desc
        elif item_num:
            title = item_num

    return {
        'itemId':        item_m.group(1),
        'workspaceId':   ws_m.group(1),
        'title':         title or 'Unknown',
        'version':       version_label,
        'versionId':     version_id,
        'latestRelease': is_released,
        'workingVersion': is_working,
        'lifecycle':     lifecycle_title,
    }


def _cleanup_dir(path):
    try:
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


class ExportGcodeCommand(PaletteCommand):
    command_id      = config.COMMAND_IDS['exportGcodeToPlm']
    command_name    = 'Export G-code to PLM'
    command_tooltip = 'Post-process NC Programs and upload NC files to Fusion Manage'
    palette_id      = config.PALETTE_ID_EXPORT_GCODE
    palette_title   = 'Export G-code to PLM'
    palette_size    = (500, 740)
    resizable       = True
    docking         = 'right'
    panel           = 'cam'

    workspace_system_name = None
    workspace_id_fallback = None

    html_url = paths.to_file_url_path(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'resources', 'html', 'index.html',
        )
    )

    # ------------------------------------------------------------------
    # Context load (sync — Fusion CAM API, main thread)
    # ------------------------------------------------------------------

    @action('getGcodeContext')
    def _act_get_context(self, _ctx, data):
        """List NC Programs and extract lineage URNs from CAM setup models."""
        try:
            cam = adsk.cam.CAM.cast(self._app.activeProduct)
        except Exception as e:
            return {'success': False, 'error': f'Could not access CAM: {e}'}

        if cam is None:
            # Try alternate product access path.
            try:
                cam = adsk.cam.CAM.cast(
                    self._app.activeDocument.products.itemByProductType('CAMProductType'))
            except Exception:
                pass
        if cam is None:
            return {
                'success': False,
                'error': 'No CAM data in the active document. Switch to the Manufacture workspace.',
            }

        nc_programs = []
        try:
            for i in range(cam.ncPrograms.count):
                nc = cam.ncPrograms.item(i)
                post_name = ''
                try:
                    pc = nc.postConfiguration
                    if pc:
                        post_name = (getattr(pc, 'description', '')
                                     or getattr(pc, 'name', '') or '')
                except Exception:
                    pass
                op_count = 0
                try:
                    op_count = len(nc.operations) if nc.operations else 0
                except Exception:
                    pass
                nc_programs.append({
                    'id':             nc.operationId,
                    'name':           nc.name or f'NC Program {i + 1}',
                    'postName':       post_name,
                    'operationCount': op_count,
                })
        except Exception as e:
            return {'success': False, 'error': f'Could not read NC programs: {e}'}

        if not nc_programs:
            return {
                'success': False,
                'error': 'No NC Programs found. Create an NC Program in the Manufacture workspace first.',
            }

        lineage_urns, component_names = _cam_setup_cad_context(cam, self._app)

        return {
            'success':         True,
            'ncPrograms':      nc_programs,
            'lineageUrns':     lineage_urns,
            'componentNames':  component_names,
        }

    # ------------------------------------------------------------------
    # Component discovery — lineage URN (async: network)
    # ------------------------------------------------------------------

    @action('loadComponentRevisions', async_=True)
    def _act_load_component_revisions(self, _ctx, data):
        """Search PLM for components matching the design's CAM setup models.

        Strategy 1 — Lineage URN (external xref components):
          Searches each lineage URN tail; compact results enriched concurrently
          with thumbnail / lock / lifecycle via a follow-up GET per item.

        Strategy 2 — Component name (internal/same-file components):
          Phrase-quoted name + workspace constraint:
            '"<name>" AND (workspaceId=<id>)'
          Matches PLM PART_DESCRIPTOR; avoids token-explosion on hyphens/spaces.

        revision: 1 = latest released, 2 = all, 3 = working
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
                'warning':    ('No linked components found for this design. '
                               'Use Manual search to find a PLM component.'),
            }

        from ...services import search as _search
        client = self._client(ctx)

        ws_id = entitlements.get_workspace_id(
            'CW_COMPONENTS', self._app,
            str(config.WORKSPACE_IDS.get('components', '57')))
        ws_id_str = str(ws_id)

        components  = []
        seen        = set()
        meta_fetches = []   # [(self_link, parsed_dict)]

        for lineage_urn in lineage_urns:
            tail = lineage_urn.rsplit(':', 1)[-1] if ':' in lineage_urn else lineage_urn
            if not tail:
                continue
            result, fetch_err = _search.search_results(
                client, [tail], revision=revision, limit=50, pre_formatted=True)
            if fetch_err:
                _log.log(f'[GcodeExport] URN search error for {tail!r}: {fetch_err}')
                continue
            for item in (result.get('items') or []):
                parsed = _parse_component_item(item)
                if not parsed or parsed['workspaceId'] != ws_id_str:
                    continue
                key = (parsed['workspaceId'], parsed['itemId'])
                if key in seen:
                    continue
                seen.add(key)
                parsed.update({'thumbnail': '', 'locked': False,
                               'changedSinceRelease': False})
                components.append(parsed)
                meta_fetches.append((item.get('__self__', ''), parsed))

        # Concurrently fetch thumbnail / lock / lifecycle for each result item.
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

        # ── Strategy 2: component name → phrase-quoted PLM search ────────────
        # Used when no lineage URNs were resolvable (internal/same-file components).
        # The Fusion component name matches the PLM PART_DESCRIPTOR field.
        #
        # Query: '"<name>" AND (workspaceId=<id>)'
        # Phrase quoting prevents token-explosion on hyphenated/spaced names.
        # The workspace constraint eliminates cross-workspace noise.
        if not components and component_names:
            for name in component_names:
                query = f'"{name}" AND (workspaceId={ws_id_str})'
                result, fetch_err = _search.search_results(
                    client, [query], revision=revision, limit=50, pre_formatted=True)
                if fetch_err:
                    continue
                raw_items = result.get('items') or []
                for item in raw_items:
                    parsed = _parse_component_item(item)
                    if not parsed or parsed['workspaceId'] != ws_id_str:
                        continue
                    key = (parsed['workspaceId'], parsed['itemId'])
                    if key in seen:
                        continue
                    seen.add(key)
                    parsed.update({'thumbnail': '', 'locked': False,
                                   'changedSinceRelease': False})
                    components.append(parsed)
                    meta_fetches.append((item.get('__self__', ''), parsed))

            # Enrich part-number results with thumbnail/lock just like URN results.
            if meta_fetches:
                with ThreadPoolExecutor(max_workers=6) as ex:
                    future_map = {
                        ex.submit(_fetch_item_meta, client, sl): p
                        for sl, p in meta_fetches
                        if not p.get('thumbnail')  # skip already-enriched items
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
                'warning':    ('No PLM components found for this design\'s linked '
                               'components. Try Manual search.'),
            }

        return {'success': True, 'components': components}

    # ------------------------------------------------------------------
    # Component discovery — manual text search (async: network)
    # ------------------------------------------------------------------

    @action('searchFmComponents', async_=True)
    def _act_search_components(self, _ctx, data):
        """Text-based component search within CW_COMPONENTS.

        revision: 1 = latest released, 2 = all, 3 = working
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
    # Post-process (sync — Fusion CAM API, main thread only)
    # ------------------------------------------------------------------

    @action('runPostProcess')
    def _act_run_post_process(self, _ctx, data):
        """Post-process selected NC Programs using their configured settings."""
        nc_program_ids = data.get('ncProgramIds') or []
        if not nc_program_ids:
            return {'success': False, 'error': 'No NC Programs selected.'}

        try:
            cam = adsk.cam.CAM.cast(self._app.activeProduct)
        except Exception as e:
            return {'success': False, 'error': f'Cannot access CAM: {e}'}
        if cam is None:
            try:
                cam = adsk.cam.CAM.cast(
                    self._app.activeDocument.products.itemByProductType('CAMProductType'))
            except Exception:
                pass
        if cam is None:
            return {'success': False, 'error': 'No CAM product in active document.'}

        all_files = []
        tmp_dirs  = []

        for raw_id in nc_program_ids:
            nc = None
            candidates = ([int(raw_id), raw_id]
                          if str(raw_id).lstrip('-').isdigit() else [raw_id])
            for id_val in candidates:
                try:
                    nc = cam.ncPrograms.itemByOperationId(id_val)
                    if nc is not None:
                        break
                except Exception:
                    pass

            if nc is None:
                for d in tmp_dirs:
                    _cleanup_dir(d)
                return {'success': False, 'error': 'NC Program not found. Refresh and try again.'}

            nc_name       = nc.name or f'nc_{raw_id}'
            resource_name = _make_resource_name(nc_name)
            out_dir       = tempfile.mkdtemp(prefix='plm_gcode_')
            out_dir_fwd   = out_dir.replace('\\', '/')
            tmp_dirs.append(out_dir)

            try:
                params = nc.parameters
                params.itemByName('nc_program_filename').value.value      = resource_name
                params.itemByName('nc_program_output_folder').value.value = out_dir_fwd
                try:
                    params.itemByName('nc_program_openInEditor').value.value = False
                except Exception:
                    pass
                options = adsk.cam.NCProgramPostProcessOptions.create()
                nc.postProcess(options)
                adsk.doEvents()   # yield after each post-process so WebView can update
            except Exception as e:
                for d in tmp_dirs:
                    _cleanup_dir(d)
                return {'success': False,
                        'error': f'Post-process failed for {nc_name!r}: {e}\nMake sure toolpaths are computed.'}

            gcode_path = _find_gcode_in(out_dir)
            if not gcode_path:
                for d in tmp_dirs:
                    _cleanup_dir(d)
                return {
                    'success': False,
                    'error': f'Post-process completed for {nc_name!r} but no output file found.',
                }

            ext = os.path.splitext(gcode_path)[1].lstrip('.').lower() or 'nc'
            all_files.append({
                'name':         _make_gcode_filename(nc_name, ext),
                'path':         gcode_path,
                'size':         os.path.getsize(gcode_path),
                'resourceName': resource_name,
            })

        if not all_files:
            return {'success': False, 'error': 'Post-process produced no output files.'}

        return {'success': True, 'files': all_files}

    # ------------------------------------------------------------------
    # Upload (async: network I/O)
    # ------------------------------------------------------------------

    @action('uploadGcode', async_=True)
    def _act_upload_gcode(self, _ctx, data):
        """Upload NC files to one or more PLM component revisions.

        targets: list of {workspaceId, itemId, title, version}
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err

        files   = data.get('files') or []
        targets = data.get('targets') or []
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
        last_att_id    = ''
        last_ws        = ''
        last_item      = ''

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

            existing_atts, _ = _att.list_attachments(client, workspace_id, item_id)
            existing_atts = existing_atts or []

            file_results = []
            t_last_att   = None

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

                    # Version bump: find existing gcode attachment by resource name prefix.
                    existing_att_id = None
                    for att in existing_atts:
                        aname = att.get('name', '')
                        if (isinstance(aname, str)
                                and aname.lower().startswith(resource_name.lower())
                                and _GCODE_SUFFIX in aname.lower()):
                            existing_att_id = att.get('id')
                            break

                    upload_info, upload_err = _att.request_upload(
                        client, workspace_id, item_id,
                        file_name, resource_name, file_size, existing_att_id,
                        comment=comment)
                    if upload_err:
                        file_results.append({'name': file_name, 'success': False, 'error': upload_err})
                        continue

                    s3_err = _att.upload_to_s3(
                        client, upload_info['s3_url'], upload_info['extra_headers'], file_bytes)
                    if s3_err:
                        file_results.append({'name': file_name, 'success': False, 'error': s3_err})
                        continue

                    version, ci_err = _att.checkin(
                        client, workspace_id, item_id, upload_info['attachment_id'])
                    if ci_err:
                        file_results.append({'name': file_name, 'success': False, 'error': ci_err})
                        continue

                    t_last_att = upload_info['attachment_id']
                    file_results.append({
                        'name': file_name, 'success': True,
                        'version': version, 'isNewVersion': existing_att_id is not None,
                    })

                except Exception as exc:
                    file_results.append({'name': file_name, 'success': False, 'error': str(exc)})

            success_count = sum(1 for r in file_results if r.get('success'))

            t_item_url = ''
            if t_last_att and tenant:
                i_urn = f'urn:adsk.plm:tenant.workspace.item:{tenant_upper}.{workspace_id}.{item_id}'
                f_urn = f'urn:adsk.plm:tenant.workspace.item.attachment:{tenant_upper}.{workspace_id}.{item_id}.{t_last_att}'
                t_item_url = (f'https://{tenant}.autodeskplm360.net/plm/fileViewer'
                              f'?itemUrn={i_urn}&fileUrn={f_urn}&vectorPdf=false')
                last_att_id = t_last_att
                last_ws = workspace_id
                last_item = item_id
            elif tenant:
                t_item_url = f'https://{tenant}.autodeskplm360.net/plm/workspaces/{workspace_id}/items/{item_id}'

            target_results.append({
                'targetTitle':   target_title,
                'targetVersion': target_version,
                'workspaceId':   workspace_id,
                'itemId':        item_id,
                'fileResults':   file_results,
                'successCount':  success_count,
                'totalCount':    len(file_results),
                'itemUrl':       t_item_url,
            })

        total_ok    = sum(r.get('successCount', 0) for r in target_results)
        total_files = sum(r.get('totalCount',   0) for r in target_results)

        item_url = ''
        if last_att_id and tenant:
            i_urn = f'urn:adsk.plm:tenant.workspace.item:{tenant_upper}.{last_ws}.{last_item}'
            f_urn = f'urn:adsk.plm:tenant.workspace.item.attachment:{tenant_upper}.{last_ws}.{last_item}.{last_att_id}'
            item_url = (f'https://{tenant}.autodeskplm360.net/plm/fileViewer'
                        f'?itemUrn={i_urn}&fileUrn={f_urn}&vectorPdf=false')
        elif tenant and target_results:
            tr = target_results[-1]
            item_url = (f'https://{tenant}.autodeskplm360.net'
                        f'/plm/workspaces/{tr["workspaceId"]}/items/{tr["itemId"]}')

        return {
            'success':           total_ok > 0,
            'targetResults':     target_results,
            'totalSuccessCount': total_ok,
            'totalCount':        total_files,
            'itemUrl':           item_url,
        }
