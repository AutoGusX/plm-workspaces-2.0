# Export PDF to PLM — export Fusion 360 drawings as PDF and attach them to
# CW_COMPONENTS records in Fusion Manage.
#
# Behaves like G-code export:
#   - Input:  open Fusion drawing documents (listed like NC programs)
#   - Export: each drawing → temp PDF file (sync, main thread)
#   - Upload: auto version-bump — same resource name → new version if exists
#   - No mapping table; the resource name is fixed per drawing
#
# UI flow:
#   JS: getPdfContext   (sync)  — enumerate open drawings + component URNs
#   JS: runPdfExport    (sync)  — export selected drawings to temp PDF files
#   JS: uploadPdf       (async) — upload to selected PLM revisions with version-bump

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

_PDF_SUFFIX = '_drawing_pdf_export'
_PDF_SUFFIX_LOWER = _pdf_suffix = _PDF_SUFFIX.lower()


def _resource_name(drawing_name):
    safe = re.sub(r'[^\w\-]', '_', (drawing_name or 'drawing').strip())
    return f'{safe}{_PDF_SUFFIX}'


def _pdf_filename(drawing_name):
    return f'{_resource_name(drawing_name)}.pdf'


# ---------------------------------------------------------------------------
# Design / component context (same pattern as DXF / G-code)
# ---------------------------------------------------------------------------

def _design_cad_context(app):
    """Extract lineage URNs and component names from the active design.
    Returns (lineage_urns, component_names).
    """
    urns       = []
    seen_urns  = set()
    names      = []
    seen_names = set()
    try:
        doc = app.activeDocument
        if not doc:
            return urns, names
        design = None
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
        except Exception:
            pass
        if not design:
            try:
                design = adsk.fusion.Design.cast(
                    doc.products.itemByProductType('DesignProductType'))
            except Exception:
                pass
        if design:
            root = design.rootComponent
            try:
                root_name = (getattr(root, 'name', '') or '').strip()
                if root_name and root_name not in seen_names:
                    seen_names.add(root_name)
                    names.append(root_name)
            except Exception:
                pass
            try:
                root_df  = doc.dataFile
                root_urn = _cad.file_id_to_lineage_urn(getattr(root_df, 'id', None) if root_df else None)
                if root_urn and root_urn not in seen_urns:
                    seen_urns.add(root_urn)
                    urns.append(root_urn)
            except Exception:
                pass
            try:
                for i in range(root.allOccurrences.count):
                    try:
                        comp = root.allOccurrences.item(i).component
                        if not comp:
                            continue
                        name = (getattr(comp, 'name', '') or '').strip()
                        if name and name not in seen_names:
                            seen_names.add(name)
                            names.append(name)
                        fid = _cad.try_get_component_file_id(comp)
                        urn = _cad.file_id_to_lineage_urn(fid)
                        if urn and urn not in seen_urns:
                            seen_urns.add(urn)
                            urns.append(urn)
                    except Exception:
                        continue
            except Exception:
                pass
    except Exception:
        pass
    return urns, names


# ---------------------------------------------------------------------------
# Attachment helpers (same pattern as G-code / DXF)
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
    r   = client._request('GET', url, empty_body_as={})
    if not r.ok or not isinstance(r.data, dict):
        return {}
    data     = r.data
    lifecycle = data.get('lifecycle') or {}
    lc_title  = lifecycle.get('title', '') if isinstance(lifecycle, dict) else ''
    return {
        'thumbnail':           _extract_thumbnail(data),
        'locked':              bool(data.get('itemLocked')),
        'lifecycle':           lc_title,
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
    version_id    = item.get('versionId') or (m.group(1) if m else '')
    lifecycle     = item.get('lifecycle') or {}
    lc_title      = lifecycle.get('title', '') if isinstance(lifecycle, dict) else ''
    title = item.get('title') or item.get('descriptor') or ''
    return {
        'itemId':            item_m.group(1),
        'workspaceId':       ws_m.group(1),
        'title':             title or 'Unknown',
        'version':           version_label,
        'versionId':         version_id,
        'latestRelease':     bool(item.get('latestRelease')),
        'workingVersion':    bool(item.get('workingVersion')) or version_id == 'w',
        'lifecycle':         lc_title,
        'thumbnail':         '',
        'locked':            False,
        'changedSinceRelease': False,
    }


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class ExportPdfCommand(PaletteCommand):
    command_id      = config.COMMAND_IDS['exportPdfToPlm']
    command_name    = 'Export Drawing PDF to PLM'
    command_tooltip = 'Export Fusion 360 drawings as PDF and attach to Fusion Manage component records'
    palette_id      = config.PALETTE_ID_EXPORT_PDF
    palette_title   = 'Export Drawing PDF to PLM'
    palette_size    = (500, 720)
    resizable       = True
    docking         = 'right'
    panel           = 'plm'

    workspace_system_name = None
    workspace_id_fallback = None

    html_url = paths.to_file_url_path(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'resources', 'html', 'index.html',
        )
    )

    # ------------------------------------------------------------------
    # Context (sync — Fusion API, main thread)
    # ------------------------------------------------------------------

    @action('getPdfContext')
    def _act_get_context(self, _ctx, data):
        """List open Fusion drawing documents and extract component identifiers.

        For each open drawing, we probe the Drawing product hierarchy to find
        the linked design document — that design's lineage URN is what maps to
        the PLM component record.  Trace output goes to the Text Commands window.
        """
        drawings_result = _cad.get_open_drawings()
        drawings = drawings_result.get('drawings') or []

        lineage_urns   = []
        component_names = []
        seen_urns  = set()
        seen_names = set()

        _log.log('[PdfExport] ── Drawing→Design Link Trace ─────────────────')

        try:
            for i in range(self._app.documents.count):
                try:
                    doc = self._app.documents.item(i)
                    doc_name = getattr(doc, 'name', '') or ''

                    # Determine if this is a drawing document
                    is_drawing = False
                    try:
                        for j in range(doc.products.count):
                            p = doc.products.item(j)
                            ot = getattr(p, 'objectType', '') or ''
                            if 'Drawing' in ot:
                                is_drawing = True
                                break
                    except Exception:
                        pass
                    if not is_drawing:
                        continue

                    _log.log(f'[PdfExport] Drawing doc: "{doc_name}"')

                    # ── Probe document-level reference properties ─────────
                    for attr in ('documentReferences', 'referencedDocuments',
                                 'parentDocument', 'modelDocument'):
                        try:
                            val = getattr(doc, attr, 'ATTR_MISSING')
                            _log.log(f'  doc.{attr} = {val!r}')
                        except Exception as e:
                            _log.log(f'  doc.{attr} → error: {e}')

                    # ── Probe each product on the drawing document ────────
                    for j in range(doc.products.count):
                        try:
                            product  = doc.products.item(j)
                            obj_type = getattr(product, 'objectType', '') or ''
                            _log.log(f'  product[{j}] objectType={obj_type!r}')

                            for attr in ('documentReferences', 'modelDocument',
                                         'modelReferences', 'sheets', 'activeSheet',
                                         'referencedDocuments', 'parentDocument'):
                                try:
                                    val = getattr(product, attr, 'ATTR_MISSING')
                                    _log.log(f'    .{attr} = {val!r}')
                                except Exception as e:
                                    _log.log(f'    .{attr} → error: {e}')

                            # ── Walk sheets → views → referencedDocument ──
                            for sheets_attr in ('sheets', 'activeSheet'):
                                try:
                                    sheets_obj = getattr(product, sheets_attr, None)
                                    if sheets_obj is None:
                                        continue
                                    sheet_list = ([sheets_obj]
                                                  if not hasattr(sheets_obj, 'count')
                                                  else [sheets_obj.item(k)
                                                        for k in range(min(sheets_obj.count, 3))])
                                    for sheet in sheet_list:
                                        sheet_name = getattr(sheet, 'name', '') or ''
                                        _log.log(f'    sheet "{sheet_name}":')
                                        for vattr in ('drawingViews', 'views'):
                                            try:
                                                views = getattr(sheet, vattr, None)
                                                if views is None:
                                                    continue
                                                _log.log(f'      .{vattr} count={views.count}')
                                                for vi in range(min(views.count, 5)):
                                                    try:
                                                        view = views.item(vi)
                                                        v_name = getattr(view, 'name', '') or ''
                                                        _log.log(f'      view[{vi}] "{v_name}":')
                                                        for va in ('referencedDocument',
                                                                   'modelDocument',
                                                                   'referencedComponent',
                                                                   'parentDocument',
                                                                   'documentReference'):
                                                            try:
                                                                vv = getattr(view, va, 'ATTR_MISSING')
                                                                _log.log(f'        .{va} = {vv!r}')
                                                                # If we got a document, try to get its fileId
                                                                if (vv and vv != 'ATTR_MISSING'
                                                                        and hasattr(vv, 'dataFile')):
                                                                    df = vv.dataFile
                                                                    fid = getattr(df, 'id', None) if df else None
                                                                    urn = _cad.file_id_to_lineage_urn(fid)
                                                                    _log.log(f'        .{va}.dataFile.id = {fid!r}')
                                                                    _log.log(f'        .{va} → lineage_urn = {urn!r}')
                                                                    if urn and urn not in seen_urns:
                                                                        seen_urns.add(urn)
                                                                        lineage_urns.append(urn)
                                                                        _log.log(f'        >>> LINKED DESIGN URN: {urn!r}')
                                                            except Exception as e:
                                                                _log.log(f'        .{va} → error: {e}')
                                                    except Exception as e:
                                                        _log.log(f'      view[{vi}] error: {e}')
                                            except Exception as e:
                                                _log.log(f'      .{vattr} error: {e}')
                                except Exception as e:
                                    _log.log(f'    {sheets_attr} error: {e}')

                        except Exception as e:
                            _log.log(f'  product[{j}] error: {e}')

                    # ── Also extract component names as fallback ──────────
                    name_clean = re.sub(r'\.[^.]+$', '', doc_name).strip()
                    if name_clean and name_clean not in seen_names:
                        seen_names.add(name_clean)
                        component_names.append(name_clean)

                except Exception as e:
                    _log.log(f'[PdfExport] doc[{i}] error: {e}')

        except Exception as e:
            _log.log(f'[PdfExport] document scan error: {e}')

        _log.log(f'[PdfExport] Discovered lineage URNs: {lineage_urns}')
        _log.log(f'[PdfExport] Fallback component names: {component_names}')
        _log.log('[PdfExport] ──────────────────────────────────────────────')

        return {
            'success':        True,
            'drawings':       drawings,
            'lineageUrns':    lineage_urns,
            'componentNames': component_names,
        }

    # ------------------------------------------------------------------
    # PDF export (sync — Fusion API, main thread)
    # ------------------------------------------------------------------

    @action('runPdfExport')
    def _act_run_pdf_export(self, _ctx, data):
        """Export selected open Fusion drawings to temporary PDF files.

        drawingFileIds: list of dataFile IDs identifying the drawing documents.
        """
        drawing_file_ids = data.get('drawingFileIds') or []
        if not drawing_file_ids:
            return {'success': False, 'error': 'No drawings selected for export.'}

        out_dir = tempfile.mkdtemp(prefix='plm_pdf_')
        files   = []

        for fid in drawing_file_ids:
            # Find the open document with this fileId
            doc = None
            try:
                for i in range(self._app.documents.count):
                    d = self._app.documents.item(i)
                    df = getattr(d, 'dataFile', None)
                    if df and getattr(df, 'id', '') == fid:
                        doc = d
                        break
            except Exception:
                pass

            if not doc:
                return {'success': False,
                        'error': f'Drawing document not found (id={fid}). '
                                 'Make sure it is still open.'}

            draw_name = getattr(doc, 'name', '') or 'drawing'
            # Strip file extension from display name if present
            draw_name = re.sub(r'\.[^.]+$', '', draw_name).strip() or 'drawing'

            res_name = _resource_name(draw_name)
            fname    = f'{res_name}.pdf'
            out_path = os.path.join(out_dir, fname)

            exported = False

            # Attempt 1: document-level exportManager
            try:
                export_mgr = doc.exportManager
                options    = export_mgr.createPDFExportOptions(
                    out_path.replace('\\', '/'))
                export_mgr.execute(options)
                adsk.doEvents()   # yield after export so WebView can update
                if os.path.isfile(out_path):
                    exported = True
                    _log.log(f'[PdfExport] Exported "{draw_name}" via doc.exportManager')
            except Exception as e:
                _log.log(f'[PdfExport] doc.exportManager attempt failed: {e}')

            # Attempt 2: look for a drawing product and use its export manager
            if not exported:
                try:
                    for i in range(doc.products.count):
                        product    = doc.products.item(i)
                        prod_type  = getattr(product, 'objectType', '') or ''
                        if 'Drawing' not in prod_type:
                            continue
                        export_mgr = getattr(product, 'exportManager', None)
                        if not export_mgr:
                            continue
                        options = export_mgr.createPDFExportOptions(
                            out_path.replace('\\', '/'))
                        export_mgr.execute(options)
                        adsk.doEvents()
                        if os.path.isfile(out_path):
                            exported = True
                            _log.log(f'[PdfExport] Exported "{draw_name}" via drawing product')
                            break
                except Exception as e:
                    _log.log(f'[PdfExport] drawing product attempt failed: {e}')

            if not exported:
                return {
                    'success': False,
                    'error': (f'PDF export failed for "{draw_name}". '
                              'Make sure the drawing is saved and the PDF format '
                              'is available in your Fusion 360 version.'),
                }

            files.append({
                'name':         fname,
                'path':         out_path,
                'size':         os.path.getsize(out_path),
                'resourceName': res_name,
                'drawingName':  draw_name,
            })

        if not files:
            return {'success': False, 'error': 'PDF export produced no output files.'}

        return {'success': True, 'files': files}

    # ------------------------------------------------------------------
    # Component discovery (async: network) — same as G-code
    # ------------------------------------------------------------------

    @action('loadComponentRevisions', async_=True)
    def _act_load_component_revisions(self, _ctx, data):
        """Search PLM for components matching the design URNs or component names."""
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

        for lineage_urn in lineage_urns:
            tail = lineage_urn.rsplit(':', 1)[-1] if ':' in lineage_urn else lineage_urn
            if not tail:
                continue
            result, _ = _search.search_results(
                client, [tail], revision=revision, limit=50, pre_formatted=True)
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

        if not components:
            for name in component_names:
                if not name:
                    continue
                query = f'"{name}" AND (workspaceId={ws_id_str})'
                result, _ = _search.search_results(
                    client, [query], revision=revision, limit=50, pre_formatted=True)
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
        """Manual component text search."""
        ctx, err = self._resolve_ctx()
        if err:
            return err
        query    = str(data.get('query') or '').strip()
        revision = int(data.get('revision') or 2)
        if not query:
            return {'success': False, 'error': 'Enter a search term.'}

        from ...services import search as _search
        client = self._client(ctx)
        ws_id  = entitlements.get_workspace_id(
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
    # Upload (async: network) — same auto version-bump as G-code
    # ------------------------------------------------------------------

    @action('uploadPdf', async_=True)
    def _act_upload_pdf(self, _ctx, data):
        """Upload PDF files to one or more PLM component revisions.

        Auto version-bump: if an attachment with the same resource name already
        exists, a new version is created.  The user never chooses overwrite vs
        create — the resource name is fixed per drawing.
        """
        ctx, err = self._resolve_ctx()
        if err:
            return err

        files   = data.get('files')   or []
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

            # Fetch existing attachments once per target for version-bump detection
            existing_atts, _ = _att.list_attachments(client, workspace_id, item_id)
            existing_atts    = existing_atts or []

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

                    # Version-bump: find existing PDF with matching resource name prefix
                    existing_att_id = None
                    for att in existing_atts:
                        aname = att.get('name', '')
                        if (isinstance(aname, str)
                                and aname.lower().startswith(resource_name.lower())
                                and aname.lower().endswith('.pdf')):
                            existing_att_id = att.get('id')
                            break

                    upload_info, upload_err = _att.request_upload(
                        client, workspace_id, item_id,
                        file_name, resource_name, file_size, existing_att_id,
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
                        'name':         file_name,
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
