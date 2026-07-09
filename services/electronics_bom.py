# Electronics BOM extraction (Fusion Electronics preview API).
#
# Reads the active electronics design's schematic and builds a normalized, grouped
# bill of materials ready to hand to a Fusion Manage export. This module is called
# from the command's SYNC @action (main thread) because it touches the Fusion API
# (adsk.electron). adsk.electron is a May-2026 preview namespace, so it is imported
# LAZILY inside the functions — on older Fusion the import simply fails and we return
# a clean error instead of breaking add-in load.
#
# Algorithm (from the reference "BOM Export Wizard" recipe / Bill of Materials sample):
#   - Get the Schematic (Schematic.cast(activeProduct), or board.linkedSchematic if a
#     Board is active).
#   - For each Part: skip if no device.package, no package3d.name, or not populated for
#     the active variant.
#   - Read MPN / MF from Part.attributes (case-insensitive; free-form, no fixed schema).
#   - Group by (value, footprint, MPN, manufacturer); qty = group size; designators =
#     sorted Part.name list.
#   - Normalize each group to an FM-export-ready row.

import os
import re as _re


def _safe_name(s):
    return _re.sub(r'[^\w\-]', '_', str(s or 'design')).strip('_') or 'design'


def _or_none(s):
    if s is None:
        return None
    t = str(s).strip()
    return t or None


def _to_schematic(electron, obj):
    """Best-effort: turn an arbitrary product/design object into a Schematic.
    Tries a direct Schematic cast, then a Board cast -> linkedSchematic, then a couple
    of design accessors seen across Electronics builds. Returns a Schematic or None."""
    if obj is None:
        return None
    try:
        s = electron.Schematic.cast(obj)
        if s is not None:
            return s
    except Exception:
        pass
    try:
        b = electron.Board.cast(obj)
        if b is not None:
            ls = b.linkedSchematic
            if ls is not None:
                return ls
    except Exception:
        pass
    # Some builds expose an EcadDesign/Document that owns the schematic.
    for attr in ('schematic', 'activeSchematic', 'linkedSchematic'):
        try:
            sub = getattr(obj, attr, None)
            if sub is not None:
                s = electron.Schematic.cast(sub)
                if s is not None:
                    return s
        except Exception:
            pass
    return None


def _get_schematic(app):
    """Return (schematic, error). Robustly locates the active electronics schematic by
    checking the active product, every product in the active document, and the
    ElectronManager. On failure the error string carries a diagnostic of what WAS found
    so the exact object path can be pinned down."""
    try:
        import adsk.electron as electron
    except Exception:
        return None, ('The Fusion Electronics API is not available in this Fusion '
                      'version. Open an electronics (PCB/Schematic) design and update Fusion.')

    diag = {'activeProduct': None, 'products': [], 'mgr': None}

    # 1) Active product.
    try:
        ap = app.activeProduct
    except Exception:
        ap = None
    if ap is not None:
        diag['activeProduct'] = getattr(ap, 'objectType', None) or getattr(ap, 'productType', None)
        sch = _to_schematic(electron, ap)
        if sch is not None:
            return sch, None

    # 2) Every product in the active document.
    try:
        doc = app.activeDocument
        prods = doc.products if doc else None
        count = prods.count if prods else 0
    except Exception:
        prods, count = None, 0
    for i in range(count):
        try:
            p = prods.item(i)
        except Exception:
            p = None
        if p is None:
            continue
        diag['products'].append(getattr(p, 'objectType', None) or getattr(p, 'productType', None))
        sch = _to_schematic(electron, p)
        if sch is not None:
            return sch, None

    # 3) ElectronManager singleton — try its design/schematic accessors.
    try:
        mgr = electron.ElectronManager.get()
    except Exception:
        mgr = None
    if mgr is not None:
        diag['mgr'] = getattr(mgr, 'objectType', 'ElectronManager')
        for attr in ('activeSchematic', 'schematic', 'activeDesign', 'activeBoard',
                     'activeDocument', 'design', 'board'):
            try:
                obj = getattr(mgr, attr, None)
            except Exception:
                obj = None
            sch = _to_schematic(electron, obj)
            if sch is not None:
                return sch, None

    msg = ('Could not locate an electronics schematic. Open a schematic (or a PCB with a '
           'linked schematic) and try again. [diagnostic: activeProduct=%r; documentProducts=%r; '
           'electronManager=%r]' % (diag['activeProduct'], diag['products'], diag['mgr']))
    return None, msg


def _variant_populated(sch, part):
    """Mirror the reference sample: only enforce variant populate flags when the design
    actually defines variants; otherwise treat every part as populated."""
    try:
        vdefs = sch.variantDefs
        if vdefs is None or vdefs.count == 0:
            return True
        pv = part.variants
        if pv is None or pv.count == 0:
            return True
        # Use the first variant's populate flag as the default-variant decision.
        v0 = pv.item(0)
        if v0 is None:
            return True
        return bool(v0.populate)
    except Exception:
        return True


def _read_attrs(part):
    """Return (mpn, manufacturer, description) from a Part's free-form attributes,
    case-insensitively. Electronics parts have no fixed schema, so DESCRIPTION/DESC are
    read best-effort (a device-name fallback for description is applied by the caller)."""
    mpn = manufacturer = description = None
    try:
        attrs = part.attributes
        if attrs is not None:
            for j in range(attrs.count):
                a = attrs.item(j)
                if a is None or a.name is None:
                    continue
                up = str(a.name).upper()
                if up == 'MPN':
                    mpn = _or_none(a.value)
                elif up in ('MF', 'MANUFACTURER'):
                    manufacturer = _or_none(a.value)
                elif up in ('DESCRIPTION', 'DESC'):
                    description = _or_none(a.value)
    except Exception:
        pass
    return mpn, manufacturer, description


def _device_name(part):
    """Best-effort component type name (device / deviceset) for a description fallback."""
    for path in (('device', 'name'), ('device', 'deviceSet', 'name'), ('deviceSet', 'name')):
        try:
            obj = part
            for attr in path:
                obj = getattr(obj, attr, None)
                if obj is None:
                    break
            if isinstance(obj, str) and obj.strip():
                return obj.strip()
        except Exception:
            continue
    return None


def extract_bom(app):
    """Build the normalized, grouped BOM for the active electronics design.

    Returns (result, error). result:
      {
        'design': <schematic name>,
        'rows': [ {mpn, manufacturer, value, footprint, quantity,
                   referenceDesignators: [..], raw: {...}}, ... ],
        'skipped': <int count of parts filtered out>,
        'partCount': <total parts scanned>,
        'warnings': [str, ...]
      }
    Rows are 'export-ready': one row per unique (value, footprint, MPN, manufacturer).
    """
    sch, err = _get_schematic(app)
    if err:
        return None, err

    warnings = []
    try:
        design_name = sch.name or ''
    except Exception:
        design_name = ''
    # Stable-ish key used to find-or-create the parent PCBA item across re-exports.
    # Prefer an id/headline if the preview API exposes one; fall back to the name.
    design_id = ''
    for attr in ('id', 'headline'):
        try:
            v = getattr(sch, attr, None)
            if v:
                design_id = str(v)
                break
        except Exception:
            pass
    design_id = design_id or design_name

    try:
        parts = sch.parts
    except Exception:
        parts = None
    if parts is None:
        return {'design': design_name, 'rows': [], 'skipped': 0, 'partCount': 0,
                'warnings': ['The schematic exposed no parts collection.']}, None

    groups = {}   # key tuple -> row dict (accumulating designators)
    order = []
    part_count = 0
    skipped = 0

    try:
        total = parts.count
    except Exception:
        total = 0

    for i in range(total):
        try:
            p = parts.item(i)
        except Exception:
            continue
        if p is None:
            continue
        part_count += 1

        # Filter: needs a device+package and a linked 3D footprint name.
        try:
            if p.device is None or p.device.package is None:
                skipped += 1
                continue
        except Exception:
            skipped += 1
            continue
        footprint = None
        try:
            p3 = p.package3d
            footprint = _or_none(p3.name) if p3 is not None else None
        except Exception:
            footprint = None
        if not footprint:
            skipped += 1
            continue

        if not _variant_populated(sch, p):
            skipped += 1
            continue

        mpn, manufacturer, description = _read_attrs(p)
        if not description:
            description = _device_name(p)   # fallback: component/device type name
        try:
            designator = _or_none(p.name) or '(unnamed)'
        except Exception:
            designator = '(unnamed)'
        try:
            value = _or_none(p.value)
        except Exception:
            value = None

        key = (value or '', footprint, mpn or '', manufacturer or '')
        row = groups.get(key)
        if row is None:
            row = {
                'mpn': mpn,
                'manufacturer': manufacturer,
                'value': value,
                'footprint': footprint,
                'description': description,
                'quantity': 0,
                'referenceDesignators': [],
                'raw': {'value': value, 'footprint': footprint, 'mpn': mpn,
                        'manufacturer': manufacturer, 'description': description},
            }
            groups[key] = row
            order.append(key)
        row['referenceDesignators'].append(designator)
        row['quantity'] += 1

    rows = []
    for key in order:
        row = groups[key]
        row['referenceDesignators'] = sorted(row['referenceDesignators'])
        rows.append(row)

    if not rows and part_count > 0:
        warnings.append('No parts qualified for the BOM (all were missing a package, a '
                        'linked 3D footprint, or were not populated for the active variant).')

    return {
        'design': design_name,
        'designId': design_id,
        'rows': rows,
        'skipped': skipped,
        'partCount': part_count,
        'warnings': warnings,
    }, None


def export_design_files(app, out_dir):
    """Export the active design's EAGLE files into out_dir via the electronics export
    manager. Writes the schematic (.sch) and, when a linked board exists, the board (.brd).
    Returns ({'designName', 'files':[{name, path, size}], 'warnings':[...]}, error).
    SYNC / main-thread only (Fusion API). Preview API — guarded lazily like extract_bom."""
    sch, err = _get_schematic(app)
    if err:
        return None, err
    try:
        design_name = sch.name or 'design'
    except Exception:
        design_name = 'design'
    base = _safe_name(design_name)
    files, warnings = [], []

    def _export(mgr_owner, factory, ext):
        try:
            mgr = mgr_owner.exportManager
            opts = getattr(mgr, factory)()
            path = os.path.join(out_dir, base + ext)
            opts.outputPath = path
            if mgr.execute(opts) and os.path.isfile(path):
                files.append({'name': os.path.basename(path), 'path': path,
                              'size': os.path.getsize(path)})
            else:
                warnings.append('%s export did not produce a file.' % ext)
        except Exception as e:
            warnings.append('%s export failed: %s' % (ext, e))

    _export(sch, 'createEagleSchExportOptions', '.sch')
    try:
        board = sch.linkedBoard
    except Exception:
        board = None
    if board is not None:
        _export(board, 'createEagleBrdExportOptions', '.brd')
    else:
        warnings.append('No linked board — exported the schematic only.')

    if not files:
        return None, ('Could not export any design files. ' + ' '.join(warnings)).strip()
    return {'designName': design_name, 'files': files, 'warnings': warnings}, None
