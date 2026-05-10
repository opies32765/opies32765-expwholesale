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


def search(row, limit=20, sort_pref=None):
    """
    Run inventory search for a sourcing_requests row. Returns list of
    dealer_inventory dicts (RealDictRow → plain dict).

    sort_pref overrides the default sort order:
      - 'miles_asc'  : lowest mileage first (default if no price_hint)
      - 'miles_desc' : highest mileage first
      - 'price_asc'  : lowest price first (cheapest)
      - 'price_desc' : highest price first
      - None         : price_hint proximity if set, else miles_asc

    Relaxations: row.get('relaxations') is a list of fields ('year',
    'ext_color', 'trim', 'miles_max') the user has explicitly opted out of.
    Search skips applying those filters even if the field is set on the row
    (which it shouldn't be after merge_spec, but we guard for safety).
    """
    # Deferred import: keep this module DB-agnostic.
    from app import get_db
    db = get_db()
    cur = db.cursor()

    relaxed = set(row.get('relaxations') or [])
    where = ["di.status = 'active'"]
    params = []

    # Hard: make + model required
    if not row.get('make') or not row.get('model'):
        return []
    where.append("lower(di.make) = %s")
    params.append(_norm(row['make']))
    where.append("lower(di.model) = %s")
    params.append(_norm(row['model']))

    # Year: skip if relaxed.
    if 'year' not in relaxed:
        ymin, ymax = row.get('year_min'), row.get('year_max')
        if ymin:
            where.append("di.year >= %s")
            params.append(int(ymin))
        if ymax:
            where.append("di.year <= %s")
            params.append(int(ymax))

    # Soft: trim (ILIKE substring — "carrera gts" matches "Carrera GTS Cab")
    if row.get('trim') and 'trim' not in relaxed:
        where.append("lower(coalesce(di.trim,'')) LIKE %s")
        params.append(f"%{_norm(row['trim'])}%")

    # Soft: ext color (any-of match)
    if row.get('ext_color') and 'ext_color' not in relaxed:
        colors = [_norm(c) for c in row['ext_color'] if c]
        if colors:
            # Simple substring OR — handles "python green" matching "green"
            ors = " OR ".join(["lower(coalesce(di.ext_color,'')) LIKE %s"] * len(colors))
            where.append(f"({ors})")
            params.extend([f"%{c}%" for c in colors])

    # Soft: int color
    if row.get('int_color') and 'int_color' not in relaxed:
        colors = [_norm(c) for c in row['int_color'] if c]
        if colors:
            ors = " OR ".join(["lower(coalesce(di.int_color,'')) LIKE %s"] * len(colors))
            where.append(f"({ors})")
            params.extend([f"%{c}%" for c in colors])

    # Soft: miles cap
    if row.get('miles_max') and 'miles_max' not in relaxed:
        where.append("(di.mileage IS NULL OR di.mileage <= %s)")
        params.append(int(row['miles_max']))

    # Sort. Explicit sort_pref wins; else price_hint proximity; else miles_asc.
    if sort_pref == 'miles_asc':
        order = "di.mileage ASC NULLS LAST, di.last_seen_at DESC NULLS LAST"
    elif sort_pref == 'miles_desc':
        order = "di.mileage DESC NULLS LAST, di.last_seen_at DESC NULLS LAST"
    elif sort_pref == 'price_asc':
        order = "di.price ASC NULLS LAST, di.mileage ASC NULLS LAST"
    elif sort_pref == 'price_desc':
        order = "di.price DESC NULLS LAST, di.mileage ASC NULLS LAST"
    elif row.get('price_hint'):
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


# ── Taxonomy helpers (backed by inventory_taxonomy materialized view) ─────
# Materialized view created 2026-05-10. Refreshed every 15 min by
# sourcing_cron. Keys: (make, model, trim) all lowercased; active_count =
# # of active dealer_inventory rows. Lets the bot answer "how many 911s do
# we have" / "what porsche models are in stock" without scanning the full
# dealer_inventory each turn.

def models_for_make(make, limit=8):
    """Top distinct models for a make, sorted by unit count. Returns
    [{model, units}, ...]."""
    if not make:
        return []
    from app import get_db
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT model, SUM(active_count)::int AS units
              FROM inventory_taxonomy
             WHERE make = %s
             GROUP BY model
             ORDER BY units DESC, model ASC
             LIMIT %s
        """, (make.strip().lower(), limit))
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f'[taxonomy] models_for_make error: {e}', flush=True)
        rows = []
    db.close()
    return rows


def trims_for_make_model(make, model, limit=8):
    """Trims available for a make+model. Returns [{trim, units}, ...]."""
    if not make or not model:
        return []
    from app import get_db
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT trim, active_count AS units
              FROM inventory_taxonomy
             WHERE make = %s AND model = %s AND trim <> ''
             ORDER BY units DESC, trim ASC
             LIMIT %s
        """, (make.strip().lower(), model.strip().lower(), limit))
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f'[taxonomy] trims_for_make_model error: {e}', flush=True)
        rows = []
    db.close()
    return rows


def count_for_make_model(make, model):
    """Total active_count for a make+model. Used to gate 'we have N' vs
    'we don't see any' offers."""
    if not make or not model:
        return 0
    from app import get_db
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT COALESCE(SUM(active_count),0)::int AS n
              FROM inventory_taxonomy
             WHERE make = %s AND model = %s
        """, (make.strip().lower(), model.strip().lower()))
        n = (cur.fetchone() or {}).get('n', 0) or 0
    except Exception as e:
        print(f'[taxonomy] count_for_make_model error: {e}', flush=True)
        n = 0
    db.close()
    return int(n)


def find_make_for_model(model):
    """When user gives a bare model with no make, look up which make(s) carry
    that model in our scans. Returns [{make, units}, ...]. If exactly one
    make has it, the bot can fill make automatically; if multiple, the bot
    asks 'ferrari 296 or maserati 296?'."""
    if not model:
        return []
    from app import get_db
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT make, SUM(active_count)::int AS units
              FROM inventory_taxonomy
             WHERE model = %s
             GROUP BY make
             ORDER BY units DESC, make ASC
        """, (model.strip().lower(),))
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f'[taxonomy] find_make_for_model error: {e}', flush=True)
        rows = []
    db.close()
    return rows


def suggest_models(make, model_hint=None, limit=20):
    """Legacy helper: distinct models that exist for that make in
    dealer_inventory directly (not via the materialized view). Kept for
    callers that prefer fresh data; the taxonomy helpers above are faster."""
    if not make:
        return []
    from app import get_db
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT model, COUNT(*) AS n "
        "FROM dealer_inventory "
        "WHERE status='active' AND lower(make)=lower(%s) AND model IS NOT NULL "
        "GROUP BY model ORDER BY n DESC LIMIT %s",
        (make, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    db.close()
    if model_hint:
        h = model_hint.strip().lower()
        rows.sort(key=lambda r: (h not in (r['model'] or '').lower(), -r['n']))
    return rows
