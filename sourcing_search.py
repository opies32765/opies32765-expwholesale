"""
EW sourcing-bot — inventory search.

Search runs against dealer_inventory only — current and historical EW
dealer scans. No external sources, no auctions.

Hard filters: year range, make, model.
Soft filters (applied only when set): trim, ext_color, miles_max,
must_clean_title.
Sort: by abs(price - price_hint) when hint given, else by lowest mileage.
"""


def _norm(s):
    return s.strip().lower() if isinstance(s, str) else s


def search(row, limit=20):
    """
    Run inventory search for a sourcing_requests row. Returns list of
    dealer_inventory dicts (RealDictRow → plain dict).
    """
    # Deferred import: keep this module DB-agnostic.
    from app import get_db
    db = get_db()
    cur = db.cursor()

    where = ["di.status = 'active'"]
    params = []

    # Hard: make + model required
    if not row.get('make') or not row.get('model'):
        return []
    where.append("lower(di.make) = %s")
    params.append(_norm(row['make']))
    where.append("lower(di.model) = %s")
    params.append(_norm(row['model']))

    # Hard: year range (at least one bound required to fire search)
    ymin, ymax = row.get('year_min'), row.get('year_max')
    if ymin:
        where.append("di.year >= %s")
        params.append(int(ymin))
    if ymax:
        where.append("di.year <= %s")
        params.append(int(ymax))

    # Soft: trim (ILIKE substring — "carrera gts" matches "Carrera GTS Cab")
    if row.get('trim'):
        where.append("lower(coalesce(di.trim,'')) LIKE %s")
        params.append(f"%{_norm(row['trim'])}%")

    # Soft: ext color (any-of match)
    if row.get('ext_color'):
        colors = [_norm(c) for c in row['ext_color'] if c]
        if colors:
            # Simple substring OR — handles "python green" matching "green"
            ors = " OR ".join(["lower(coalesce(di.ext_color,'')) LIKE %s"] * len(colors))
            where.append(f"({ors})")
            params.extend([f"%{c}%" for c in colors])

    # Soft: int color
    if row.get('int_color'):
        colors = [_norm(c) for c in row['int_color'] if c]
        if colors:
            ors = " OR ".join(["lower(coalesce(di.int_color,'')) LIKE %s"] * len(colors))
            where.append(f"({ors})")
            params.extend([f"%{c}%" for c in colors])

    # Soft: miles cap
    if row.get('miles_max'):
        where.append("(di.mileage IS NULL OR di.mileage <= %s)")
        params.append(int(row['miles_max']))

    # Sort: price_hint proximity if given, else lowest mileage
    if row.get('price_hint'):
        order = ("CASE WHEN di.price IS NULL THEN 999999999 "
                 f"ELSE abs(di.price - %s) END ASC, di.mileage ASC NULLS LAST")
        params.append(int(row['price_hint']))
    else:
        order = "di.mileage ASC NULLS LAST, di.last_seen_at DESC NULLS LAST"

    sql = f"""
        SELECT di.*, d.name AS dealer_name
          FROM dealer_inventory di
          JOIN dealers d ON d.id = di.dealer_id
         WHERE {' AND '.join(where)}
         ORDER BY {order}
         LIMIT %s
    """
    params.append(limit)
    cur.execute(sql, params)
    rows = cur.fetchall()
    db.close()
    return [dict(r) for r in rows]


def search_with_fallback(row, limit=20):
    """Strict-first search; on empty, progressively broaden by dropping
    soft filters. Returns (rows, fallback_level) where fallback_level is:
       'exact'    — original spec
       'no_trim'  — trim filter dropped
       'no_color' — color filter dropped
       'broad'    — both trim and color dropped
       'none'     — still nothing
    """
    res = search(row, limit)
    if res:
        return res, 'exact'

    has_trim = bool(row.get('trim'))
    has_ext  = bool(row.get('ext_color'))

    if has_trim:
        r2 = dict(row); r2['trim'] = None
        res = search(r2, limit)
        if res:
            return res, 'no_trim'

    if has_ext:
        r3 = dict(row); r3['ext_color'] = None
        res = search(r3, limit)
        if res:
            return res, 'no_color'

    if has_trim or has_ext:
        r4 = dict(row); r4['trim'] = None; r4['ext_color'] = None
        res = search(r4, limit)
        if res:
            return res, 'broad'

    return [], 'none'


def to_match_descs(rows):
    """
    Strip dealer_inventory rows down to the spec-only fields we ever expose
    to the user. Removes price, dealer name, URL, VIN, stock#, location.
    """
    out = []
    for r in rows:
        out.append({
            'inventory_id': r.get('id'),
            'year': r.get('year'),
            'make': r.get('make'),
            'model': r.get('model'),
            'trim': r.get('trim'),
            'ext_color': r.get('ext_color'),
            'int_color': r.get('int_color'),
            'mileage': r.get('mileage'),
        })
    return out
