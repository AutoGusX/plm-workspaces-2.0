# Fusion-side CAD helpers: enumerate components/drawings, resolve dataFile IDs, and
# convert them to APS lineage URNs for matching against FM CAD workspaces.
#
# Ported from the old lib/components_utils.py. These touch only the adsk.* API
# (no network); FM lineage matching is done by services.search.find_item_by_lineage_urn.

import re

import adsk.core
import adsk.fusion

_app = adsk.core.Application.get()


def try_get_component_file_id(comp):
    """Best-effort resolve a component's dataFile id across Fusion API variations."""
    # Direct dataFile on the component (some versions / external refs).
    try:
        df = getattr(comp, 'dataFile', None)
        if df:
            fid = getattr(df, 'id', None)
            if fid:
                return str(fid)
    except Exception:
        pass
    # parentDesign -> matching open document -> its dataFile id.
    try:
        parent_design = getattr(comp, 'parentDesign', None)
        if parent_design:
            for i in range(_app.documents.count):
                try:
                    doc = _app.documents.item(i)
                    doc_design = adsk.fusion.Design.cast(
                        getattr(doc, 'products', None) and doc.products.item(0)
                        if doc.products.count > 0 else None)
                    if doc_design and doc_design == parent_design:
                        df = getattr(doc, 'dataFile', None)
                        if df:
                            fid = getattr(df, 'id', None)
                            if fid:
                                return str(fid)
                except Exception:
                    continue
    except Exception:
        pass
    return None


def get_selected_components():
    """Read ui.activeSelections; return {success, components:[{name, fileId}]}."""
    components = []
    try:
        sels = _app.userInterface.activeSelections
        seen = set()
        for i in range(sels.count):
            try:
                entity = sels.item(i).entity
                comp = None
                if hasattr(entity, 'component'):
                    comp = entity.component
                elif isinstance(entity, adsk.fusion.Component):
                    comp = entity
                if comp is None:
                    continue
                name = getattr(comp, 'name', '') or ''
                file_id = try_get_component_file_id(comp)
                key = name + '|' + (file_id or '')
                if key in seen:
                    continue
                seen.add(key)
                components.append({'name': name, 'fileId': file_id or ''})
            except Exception:
                continue
    except Exception as e:
        return {'success': False, 'error': str(e), 'components': []}
    return {'success': True, 'components': components}


def get_open_drawings():
    """Scan open documents for Drawings; return {success, drawings:[{name, fileId}]}.

    Fusion does not expose drawings as occurrences under a design — they are separate
    documents — so we enumerate the session's open documents whose first product is a
    Drawing. Callers then match against the CW_DRAWINGS workspace by lineage URN.
    """
    drawings = []
    try:
        for i in range(_app.documents.count):
            try:
                doc = _app.documents.item(i)
                is_drawing = False
                try:
                    if doc.products and doc.products.count > 0:
                        product = doc.products.item(0)
                        if 'Drawing' in type(product).__name__:
                            is_drawing = True
                        elif 'Drawing' in (getattr(product, 'objectType', '') or ''):
                            is_drawing = True
                except Exception:
                    pass
                if not is_drawing:
                    try:
                        cls_type = getattr(doc, 'classType', None)
                        if callable(cls_type):
                            ct = cls_type()
                            if isinstance(ct, str) and 'Drawing' in ct:
                                is_drawing = True
                    except Exception:
                        pass
                if not is_drawing:
                    continue
                name = getattr(doc, 'name', '') or 'Drawing'
                df = getattr(doc, 'dataFile', None)
                fid = getattr(df, 'id', '') if df else ''
                drawings.append({'name': name, 'fileId': str(fid) if fid else ''})
            except Exception:
                continue
    except Exception as e:
        return {'success': False, 'error': str(e), 'drawings': []}
    return {'success': True, 'drawings': drawings}


def get_root_components():
    """Traverse design.rootComponent.allOccurrences; up to 200 entries, root first."""
    components = []
    try:
        product = _app.activeProduct
        if not product:
            return {'success': False, 'error': 'No active product.', 'components': []}
        design = adsk.fusion.Design.cast(product)
        if not design:
            return {'success': False, 'error': 'Active product is not a Fusion design.', 'components': []}
        root = design.rootComponent
        seen = set()
        root_file_id = ''
        try:
            doc = _app.activeDocument
            if doc and getattr(doc, 'dataFile', None):
                root_file_id = getattr(doc.dataFile, 'id', '') or ''
        except Exception:
            pass
        root_name = getattr(root, 'name', '') or 'Root'
        seen.add(root_name + '|' + root_file_id)
        components.append({'name': root_name, 'fileId': root_file_id, 'isRoot': True})
        occs = root.allOccurrences
        for i in range(min(occs.count, 199)):
            try:
                comp = occs.item(i).component
                if comp is None:
                    continue
                name = getattr(comp, 'name', '') or ''
                file_id = try_get_component_file_id(comp)
                key = name + '|' + (file_id or '')
                if key in seen:
                    continue
                seen.add(key)
                components.append({'name': name, 'fileId': file_id or ''})
            except Exception:
                continue
    except Exception as e:
        return {'success': False, 'error': str(e), 'components': []}
    return {'success': True, 'components': components}


def file_id_to_lineage_urn(file_id):
    """Convert a Fusion dataFile.id to an APS lineage URN.

    Handles version-file URNs (urn:adsk.wipprod:fs.file:vf.{id}) and already-converted
    lineage URNs (urn:adsk.wipprod:dm.lineage:{id}).
    """
    if not file_id:
        return None
    m = re.search(r'vf\.([A-Za-z0-9_\-]+)', file_id)
    if m:
        return f'urn:adsk.wipprod:dm.lineage:{m.group(1)}'
    if 'dm.lineage:' in file_id:
        return file_id
    tail = file_id.rsplit(':', 1)[-1]
    return f'urn:adsk.wipprod:dm.lineage:{tail}' if tail else None


def get_all_referenced_components(app):
    """Return [{name, lineageUrn}] for every unique Component in the active document.

    Walks rootComponent.allOccurrences (+ the root itself), de-dupes by lineage URN,
    and skips components without a resolvable cloud lineage URN. Suitable for the
    G-code export Component picker.
    """
    results = []
    seen_urns = set()
    try:
        doc = app.activeDocument
        if doc is None:
            return results
        design = None
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
        except Exception:
            pass
        if design is None:
            try:
                design = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
            except Exception:
                pass
        if design is None:
            return results
        root = design.rootComponent
        try:
            df = getattr(doc, 'dataFile', None)
            root_fid = str(getattr(df, 'id', '') or '') if df else ''
            root_urn = file_id_to_lineage_urn(root_fid)
            if root_urn and root_urn not in seen_urns:
                seen_urns.add(root_urn)
                results.append({'name': getattr(root, 'name', '') or 'Root Assembly',
                                'lineageUrn': root_urn})
        except Exception:
            pass
        occs = root.allOccurrences
        for i in range(min(occs.count, 200)):
            try:
                comp = occs.item(i).component
                if comp is None:
                    continue
                name = getattr(comp, 'name', '') or 'Component'
                urn = file_id_to_lineage_urn(try_get_component_file_id(comp))
                if not urn or urn in seen_urns:
                    continue
                seen_urns.add(urn)
                results.append({'name': name, 'lineageUrn': urn})
            except Exception:
                continue
    except Exception:
        pass
    return results
