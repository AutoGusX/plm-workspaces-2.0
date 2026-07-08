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


def _or_none(s):
    if s is None:
        return None
    t = str(s).strip()
    return t or None


def _get_schematic(app):
    """Return (schematic, error). Accepts either an active Schematic or an active Board
    (via board.linkedSchematic). error is a user-facing string or None."""
    try:
        import adsk.electron as electron
    except Exception:
        return None, ('The Fusion Electronics API is not available in this Fusion '
                      'version. Open an electronics (PCB/Schematic) design and update Fusion.')
    try:
        product = app.activeProduct
    except Exception:
        product = None
    if product is None:
        return None, 'No active design. Open an electronics schematic or PCB, then try again.'

    sch = None
    try:
        sch = electron.Schematic.cast(product)
    except Exception:
        sch = None
    if sch is None:
        # Maybe the PCB layout is active — hop to its linked schematic.
        try:
            board = electron.Board.cast(product)
            if board is not None:
                sch = board.linkedSchematic
        except Exception:
            sch = None
    if sch is None:
        return None, ('Active product is not an electronics schematic or PCB. Switch to '
                      'the Electronics workspace with a schematic (or a PCB that has a '
                      'linked schematic) open.')
    return sch, None


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
    """Return (mpn, manufacturer) from a Part's free-form attributes, case-insensitively."""
    mpn = manufacturer = None
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
    except Exception:
        pass
    return mpn, manufacturer


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

        mpn, manufacturer = _read_attrs(p)
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
                'quantity': 0,
                'referenceDesignators': [],
                'raw': {'value': value, 'footprint': footprint, 'mpn': mpn,
                        'manufacturer': manufacturer},
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
        'rows': rows,
        'skipped': skipped,
        'partCount': part_count,
        'warnings': warnings,
    }, None
