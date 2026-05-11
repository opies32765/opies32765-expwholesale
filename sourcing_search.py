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


# ── Wishlist <-> bid cross-reference ──────────────────────────────────────
# When a bid (sell-side) lands for a vehicle that matches an active
# sourcing_request (buy-side wishlist), we want EW staff alerted: yellow
# banner on every dashboard page, click-through to the sourcing thread.
# The matching is permissive on optional fields and strict on make+model.

def _bid_matches_request(bid, req):
    """Pure function. Given a bid dict and a sourcing_request dict, return
    (matches: bool, strength: 'exact'|'partial', reasons: list[str]).

    Match rules (2026-05-10):
      - REQUIRED: lowercased make AND model match
      - year:  bid.year in [req.year_min, req.year_max] OR year null/relaxed
      - color: if req.ext_color set + not relaxed -> bid color must intersect
      - miles: if req.miles_max set + not relaxed -> bid.mileage <= cap
      - trim/transmission: informational only, add to reasons if they line up

    'exact' = every set filter matches. 'partial' = only make+model match
    (some filter missing from the bid or unenforced)."""
    if not bid or not req:
        return False, None, []
    b_make = (bid.get('canon_make') or bid.get('make') or '').strip().lower()
    b_model = (bid.get('canon_model') or bid.get('model') or '').strip().lower()
    r_make = (req.get('make') or '').strip().lower()
    r_model = (req.get('model') or '').strip().lower()
    if not (b_make and b_model and r_make and r_model):
        return False, None, []
    if b_make != r_make or b_model != r_model:
        return False, None, []

    reasons = [f'make={r_make}', f'model={r_model}']
    relaxed = set(req.get('relaxations') or [])
    strength = 'exact'

    # Year gate
    b_year = bid.get('year')
    y_min, y_max = req.get('year_min'), req.get('year_max')
    if (y_min or y_max) and 'year' not in relaxed:
        if b_year is None:
            strength = 'partial'
            reasons.append('year=unknown')
        else:
            if y_min and b_year < int(y_min):
                return False, None, []
            if y_max and b_year > int(y_max):
                return False, None, []
            reasons.append(f'year={b_year}')
    elif b_year:
        reasons.append(f'year={b_year}(any)')

    # Color gate
    r_ec = [str(c).strip().lower() for c in (req.get('ext_color') or [])]
    if r_ec and 'ext_color' not in relaxed:
        b_color = (bid.get('color') or '').strip().lower()
        if not b_color:
            strength = 'partial'
            reasons.append('color=unknown')
        elif not any(c in b_color or b_color in c for c in r_ec):
            return False, None, []
        else:
            reasons.append(f'color={b_color}')

    # Mileage gate
    if req.get('miles_max') and 'miles_max' not in relaxed:
        cap = int(req['miles_max'])
        b_miles = bid.get('mileage')
        if b_miles is None:
            strength = 'partial'
            reasons.append('miles=unknown')
        else:
            if int(b_miles) > cap:
                return False, None, []
            reasons.append(f'miles={b_miles}<=cap{cap}')

    # Trim gate — when wholesaler specified a trim ("turbo") AND we haven't
    # relaxed it, the bid's trim must contain the requested trim as a
    # substring (case-insensitive). "Turbo" matches "Turbo S", "Turbo
    # Cabriolet", "Turbo Coupe" but NOT plain "Cabriolet" or "Carrera S".
    # If bid trim is unknown, accept with partial strength so we don't miss
    # under-decoded bids — staff can adjudicate.
    r_trim = (req.get('trim') or '').strip().lower()
    b_trim = (bid.get('canon_trim') or bid.get('trim') or '').strip().lower()
    if r_trim and 'trim' not in relaxed:
        if not b_trim:
            strength = 'partial'
            reasons.append('trim=unknown')
        elif r_trim in b_trim or b_trim in r_trim:
            reasons.append(f'trim={r_trim}')
        else:
            return False, None, []
    elif r_trim and b_trim and (r_trim in b_trim or b_trim in r_trim):
        reasons.append(f'trim={r_trim}')

    return True, strength, reasons


def find_wishlist_matches_for_bid(bid):
    """For a single bid row, return list of (sourcing_request_id, strength,
    reasons) for every active wishlist that matches. Used at bid-detail
    render time to show the yellow banner with prior wholesaler interest."""
    from app import get_db
    if not bid or not bid.get('id'):
        return []
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT * FROM sourcing_requests
             WHERE status NOT IN ('archived')
               AND make IS NOT NULL AND model IS NOT NULL
        """)
        reqs = cur.fetchall()
    except Exception as e:
        print(f'[wishlist-match] err: {e}', flush=True)
        reqs = []
    db.close()
    out = []
    for r in reqs:
        matches, strength, reasons = _bid_matches_request(bid, dict(r))
        if matches:
            out.append({
                'sourcing_request_id': r['id'],
                'customer_name': r.get('customer_name'),
                'phone': r.get('phone'),
                'narrative_brief': r.get('narrative_brief'),
                'strength': strength,
                'reasons': reasons,
            })
    return out


def find_bid_matches_for_request(req):
    """Inverse: for one sourcing_request, return matching bids from the
    last 30 days. Used by the cron scan to populate sourcing_bid_matches."""
    from app import get_db
    if not req:
        return []
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT id, vin, year, make, model, trim, color, mileage,
                   canon_make, canon_model, canon_trim, status, created_at
              FROM bids
             WHERE created_at > NOW() - INTERVAL '30 days'
               AND COALESCE(canon_make, make) IS NOT NULL
               AND COALESCE(canon_model, model) IS NOT NULL
        """)
        bids_rows = cur.fetchall()
    except Exception as e:
        print(f'[bid-match] err: {e}', flush=True)
        bids_rows = []
    db.close()
    out = []
    for b in bids_rows:
        matches, strength, reasons = _bid_matches_request(dict(b), req)
        if matches:
            out.append({
                'bid_id': b['id'],
                'strength': strength,
                'reasons': reasons,
            })
    return out


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
