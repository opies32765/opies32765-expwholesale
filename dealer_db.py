"""
Dealer DB — Flask Blueprint.

Wires the scanner + UI into EW. Imported by app.py via:
    from dealer_db import bp as dealer_bp
    app.register_blueprint(dealer_bp)
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
from flask import Blueprint, Response, jsonify, render_template, request, redirect, url_for
from markupsafe import Markup


def _json_response(payload, status=200):
    """jsonify() can't handle datetimes or accept default=. Route through json.dumps."""
    return Response(json.dumps(payload, default=str),
                    status=status, mimetype='application/json')

import dealer_scanner

bp = Blueprint('dealer_db', __name__)

DB_URL = os.environ.get('DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost/expwholesale')


def _db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# ── Pages ────────────────────────────────────────────────────────────────
@bp.route('/dealers')
def dealers_list():
    with _db() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM dealer_stats ORDER BY last_scan_at DESC NULLS LAST, name')
        dealers = cur.fetchall()

        cur.execute('''SELECT
                         COUNT(*)                                        AS total,
                         COUNT(*) FILTER (WHERE last_scan_at > NOW() - INTERVAL '24 hours')
                                                                          AS scanned_24h,
                         COALESCE(SUM(in_stock),0)                        AS total_stock,
                         COALESCE(SUM(sold_last_7d),0)                    AS sold_7d
                       FROM dealer_stats''')
        totals = cur.fetchone() or {'total': 0, 'scanned_24h': 0,
                                     'total_stock': 0, 'sold_7d': 0}
        # Active cars whose price has dropped scan-over-scan. Sold/missing
        # cars are excluded — drops only matter for cars you can still buy.
        # Sticky for the row's active life: once detected, persists until
        # the car flips out of active (then it falls off the count).
        cur.execute('''SELECT COUNT(*) AS cnt
                       FROM dealer_inventory i
                       JOIN dealers d ON d.id = i.dealer_id
                       WHERE i.status = 'active' AND d.active
                         AND i.price_drop_amount IS NOT NULL''')
        totals['price_drops'] = (cur.fetchone() or {'cnt': 0})['cnt'] or 0
    return render_template('dealers_list.html', dealers=dealers, totals=totals)


@bp.route('/dealers/<int:dealer_id>')
def dealer_detail(dealer_id):
    with _db() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM dealer_stats WHERE dealer_id = %s', (dealer_id,))
        dealer = cur.fetchone()
        if not dealer:
            return 'Dealer not found', 404

        cur.execute('''SELECT * FROM dealer_inventory
                       WHERE dealer_id = %s AND status IN ('active','missing')
                       ORDER BY make NULLS LAST, model NULLS LAST, year DESC NULLS LAST,
                                first_seen_at''',
                    (dealer_id,))
        active = cur.fetchall()

        # Sold cars are lazy-loaded — see /api/dealer/<id>/sold below.
        # Skipping the inline fetch saves ~30-100ms on dealers with deep
        # sold history (Ferrari has 500+ sold rows). Just need the count
        # for the collapsed-section badge, which we already get from
        # _pipeline_stats(dealer_id) below.

        cur.execute('''SELECT * FROM dealer_scans
                       WHERE dealer_id = %s
                       ORDER BY started_at DESC LIMIT 20''',
                    (dealer_id,))
        scans = cur.fetchall()

    # Group by make only; rows sorted by price DESC within each make
    tree = _group_by_make(active)
    # Age buckets for the chart
    buckets = _age_buckets(active)
    # Pipeline coverage (for the visibility card)
    pipeline = _pipeline_stats(dealer_id)

    # Partner-account state — drives the "Set up Partner Account" button
    # visibility on the dealer detail page (only show if no account exists yet).
    with _db() as conn, conn.cursor() as cur:
        cur.execute('''SELECT id, email, full_name, phone,
                              sms_opt_in, email_bid_alerts, last_login_at
                       FROM partner_users
                       WHERE dealer_id = %s
                       ORDER BY id LIMIT 1''', (dealer_id,))
        partner_account = cur.fetchone()

    return render_template('dealers_detail.html',
                           dealer=dealer, tree=tree, buckets=buckets,
                           scans=scans, pipeline=pipeline,
                           partner_account=partner_account)


@bp.route('/api/dealer/<int:dealer_id>/sold')
def api_dealer_sold(dealer_id):
    """Return sold inventory rows for a dealer. Lazy-loaded by the dealer
    detail page when the user expands the Sold section — keeps the initial
    page render fast on dealers with deep sold history."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute('''SELECT id, year, make, model, trim, ext_color, vin,
                              price, sold_at, sold_confidence, sold_signals, url
                       FROM dealer_inventory
                       WHERE dealer_id = %s AND status = 'sold'
                       ORDER BY sold_at DESC NULLS LAST LIMIT 500''',
                    (dealer_id,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get('sold_at'):
            d['sold_at'] = d['sold_at'].isoformat()
        if d.get('price') is not None:
            d['price'] = int(d['price'])
        out.append(d)
    return jsonify({'sold': out})


@bp.route('/api/dealer/<int:dealer_id>/setup_partner', methods=['POST'])
def setup_partner_account(dealer_id):
    """Provision a partner-portal account for an EXISTING dealer.
    Same logic as the inline path in /api/dealer/create, but reachable
    independently from the dealer detail page so admins can onboard a
    dealer's contact later without re-adding the dealer."""
    data = request.get_json(silent=True) or request.form
    email = (data.get('contact_email') or '').strip()
    phone = (data.get('contact_phone') or '').strip()
    full_name = (data.get('contact_full_name') or '').strip() or None
    sms_opt_in = bool(data.get('sms_opt_in')) and bool(phone)
    email_bid_alerts = bool(data.get('email_bid_alerts', True))

    if not email or '@' not in email:
        return jsonify({'success': False, 'error': 'valid email required'}), 400

    # Update dealer's phone too if we got one and dealer doesn't have one
    if phone:
        with _db() as conn, conn.cursor() as cur:
            cur.execute('UPDATE dealers SET phone = COALESCE(phone, %s) WHERE id = %s',
                        (phone, dealer_id))
            conn.commit()

    from partner_portal import create_welcome_account
    result = create_welcome_account(
        dealer_id=dealer_id,
        email=email,
        phone=phone or None,
        sms_opt_in=sms_opt_in,
        email_bid_alerts=email_bid_alerts,
        full_name=full_name,
    )
    return jsonify(result)


# Effective first-seen: prefer vAuto-verified crawler data (most accurate -
# catches photo-reupload scams), fall back to dealer-declared source_added_at,
# then our scanner's first_seen_at. Used by bucket filters + ORDER BYs so
# aged-stock cards reflect reality as verification rolls in.
EFFECTIVE_FS = ("COALESCE("
                "i.verified_at - (i.verified_days_on_lot || ' days')::interval, "
                "i.source_added_at, i.first_seen_at)")

# ── Cross-dealer aggregated lists (dashboard stat-card drill-ins) ────────
_BUCKET_FILTERS = {
    '30_60': (
        f"{EFFECTIVE_FS} <= NOW() - INTERVAL '30 days' "
        f"AND {EFFECTIVE_FS} > NOW() - INTERVAL '60 days'",
        '30–60 days on lot',
    ),
    '60_90': (
        f"{EFFECTIVE_FS} <= NOW() - INTERVAL '60 days' "
        f"AND {EFFECTIVE_FS} > NOW() - INTERVAL '90 days'",
        '60–90 days on lot',
    ),
    '90_plus': (
        f"{EFFECTIVE_FS} <= NOW() - INTERVAL '90 days'",
        '90+ days on lot — wholesale candidates',
    ),
}


@bp.route('/dealers/aged/<bucket>')
def dealers_aged(bucket):
    """Cross-dealer list of aged vehicles in one bucket, oldest first.
    Aggregates every active partner dealer. Auto-includes future onboards."""
    if bucket not in _BUCKET_FILTERS:
        return 'Unknown bucket', 404
    where_age, title = _BUCKET_FILTERS[bucket]
    with _db() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT i.id, i.dealer_id, d.name AS dealer_name,
                   i.year, i.make, i.model, i.trim, i.vin, i.ext_color,
                   i.mileage, i.price, i.url, i.photo_url,
                   i.source_added_at, i.first_seen_at,
                   i.verified_at, i.verified_days_on_lot,
                   i.price_drop_amount, i.price_drop_at, i.last_price
            FROM dealer_inventory i
            JOIN dealers d ON d.id = i.dealer_id
            WHERE i.status = 'active' AND d.active AND ({where_age})
            ORDER BY {EFFECTIVE_FS} ASC
        """)
        rows = cur.fetchall()
    return render_template('dealers_cross_list.html',
                           rows=rows, title=title, bucket=bucket, mode='aged')


# ── vAuto rBook verification endpoints (Beelink worker calls these) ─────
# Worker polls /api/dealer/vauto_verify_queue → gets list of VINs needing
# verification → runs vAuto appraisal → extracts the (parens) days-on-market
# → POSTs {vin, days_on_market, market_price, market_odometer} back here.


@bp.route('/api/dealer/vauto_verify_queue', methods=['GET'])
def api_vauto_verify_queue():
    """Return active VINs that have NEVER been verified by vAuto.

    NEW-ONLY semantics (2026-04-27): once a vehicle is verified, its
    `verified_days_on_lot` is frozen as a snapshot and never re-checked.
    Reasoning: the dashboard's `EFFECTIVE_FS` formula computes a frozen
    anchor (`verified_at - verified_days_on_lot days`) that yields the
    correct current days-on-lot every page load via NOW() math, so the
    stored value never goes stale. This means the Beelink verifier runs
    in bursts after each scan brings new VINs in, then idles — instead of
    constantly cycling through stale re-verifies. Tradeoff: if a dealer
    pulls + re-lists a car (which would reset its vAuto days), we miss it
    until that vehicle is removed and re-added by the scanner.

    Query params: dealer_id (int, optional — defaults to all active dealers),
                  limit (int, default 5)."""
    try:
        dealer_id = request.args.get('dealer_id', type=int)
        limit = min(int(request.args.get('limit', 5)), 50)
    except (ValueError, TypeError):
        return jsonify({'error': 'bad params'}), 400

    where_dealer = 'AND i.dealer_id = %s' if dealer_id else ''
    params = []
    if dealer_id:
        params.append(dealer_id)
    params.append(limit)

    with _db() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT i.id, i.dealer_id, d.name AS dealer_name,
                   i.vin, i.year, i.make, i.model, i.trim, i.mileage,
                   i.source_added_at::date AS source_added_at,
                   i.verified_at, i.verified_days_on_lot,
                   COALESCE(d.vauto_seller_hints, ARRAY[]::text[]) AS vauto_seller_hints
            FROM dealer_inventory i
            JOIN dealers d ON d.id = i.dealer_id
            WHERE i.status = 'active' AND d.active
              AND i.vin IS NOT NULL AND i.vin <> '' AND LENGTH(i.vin) = 17
              -- Skip obvious fake/padded VINs (pre-1981 classics padded with
              -- leading zeros to hit the 17-char constraint). vAuto rBook
              -- has no data for these and the verifier hangs waiting.
              AND i.vin NOT LIKE '000%%'
              AND (i.year IS NULL OR i.year >= 1981)
              -- New-only: only ever pick up never-verified rows. Skip markers
              -- (no_rbook_data, appraisal_failed) already cleared via
              -- verified_at being set with a non-null verified_source —
              -- they don't re-queue.
              AND i.verified_at IS NULL
              {where_dealer}
            ORDER BY i.first_seen_at ASC
            LIMIT %s
        """, tuple(params))
        return _json_response({'queue': [dict(r) for r in cur.fetchall()]})


@bp.route('/api/dealer/vauto_verify', methods=['POST'])
def api_vauto_verify():
    """Worker POSTs verified days-on-market data here.
    Body: {vin, days_on_market, market_price?, market_odometer?, notes?}"""
    data = request.get_json(silent=True) or {}
    vin = (data.get('vin') or '').strip().upper()
    days = data.get('days_on_market')
    if not vin or len(vin) != 17:
        return jsonify({'error': 'invalid_vin'}), 400
    if days is None:
        # Allow negative-result submissions — records that we tried but vAuto had no data
        days = None
    else:
        try:
            days = int(days)
            if days < 0 or days > 3000:
                return jsonify({'error': 'days_out_of_range'}), 400
        except (ValueError, TypeError):
            return jsonify({'error': 'days_not_int'}), 400

    # Distinguish real verified days vs skip markers coming in with days=0
    # and a note like 'no_rbook_data' or 'appraisal_failed:XYZ' from the
    # worker. The source column is what UI/queries check to ignore skips.
    notes = (data.get('notes') or '').strip()
    if days == 0 and notes:
        source = notes[:64]  # e.g. 'no_rbook_data', 'appraisal_failed:...'
        days_val = None      # don't let a 0 look like "just listed"
    else:
        source = 'vauto_rbook'
        days_val = days

    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE dealer_inventory
               SET verified_days_on_lot = %s,
                   verified_at = NOW(),
                   verified_source = %s,
                   updated_at = NOW()
             WHERE vin = %s AND status = 'active'
             RETURNING id, dealer_id
        """, (days_val, source, vin))
        rows = cur.fetchall()
        conn.commit()

    if not rows:
        return jsonify({'ok': False, 'reason': 'vin_not_active'}), 200
    return jsonify({'ok': True, 'rows_updated': len(rows),
                    'vin': vin, 'days_on_market': days})


@bp.route('/dealers/search')
def dealers_search():
    """Cross-dealer vehicle search. GET params (all optional):
       year_from, year_to, make, model, trim, color, vin,
       miles_max, price_min, price_max,
       dealer_id (repeat for multiple), status (default 'active'),
       min_age, max_age, has_drop (0/1), has_verified_age (0/1)."""
    q = request.args
    parts = []
    params = []

    status = q.get('status') or 'active'
    if status != 'any':
        parts.append("i.status = %s"); params.append(status)

    if q.get('year_from'):
        try:
            parts.append("i.year >= %s"); params.append(int(q['year_from']))
        except ValueError:
            pass
    if q.get('year_to'):
        try:
            parts.append("i.year <= %s"); params.append(int(q['year_to']))
        except ValueError:
            pass

    if q.get('make'):
        parts.append("LOWER(i.make) = LOWER(%s)"); params.append(q['make'].strip())
    if q.get('model'):
        parts.append("LOWER(i.model) LIKE LOWER(%s)"); params.append(f"%{q['model'].strip()}%")
    if q.get('trim'):
        parts.append("LOWER(COALESCE(i.trim,'')) LIKE LOWER(%s)")
        params.append(f"%{q['trim'].strip()}%")
    if q.get('color'):
        parts.append("LOWER(COALESCE(i.ext_color,'')) LIKE LOWER(%s)")
        params.append(f"%{q['color'].strip()}%")
    if q.get('vin'):
        parts.append("UPPER(COALESCE(i.vin,'')) LIKE UPPER(%s)")
        params.append(f"%{q['vin'].strip()}%")

    if q.get('miles_max'):
        try:
            parts.append("i.mileage <= %s"); params.append(int(q['miles_max']))
        except ValueError:
            pass
    if q.get('price_min'):
        try:
            parts.append("i.price >= %s"); params.append(int(q['price_min']))
        except ValueError:
            pass
    if q.get('price_max'):
        try:
            parts.append("i.price <= %s"); params.append(int(q['price_max']))
        except ValueError:
            pass

    # Dealer multi-select (repeat param or comma-separated)
    dealer_ids = q.getlist('dealer_id') or []
    if dealer_ids:
        try:
            ids = [int(d) for d in dealer_ids if d]
            if ids:
                parts.append(f"i.dealer_id IN ({','.join(['%s']*len(ids))})")
                params.extend(ids)
        except ValueError:
            pass

    # Age filters use EFFECTIVE_FS (vAuto-verified > dealer > scanner)
    if q.get('min_age'):
        try:
            parts.append(f"{EFFECTIVE_FS} <= NOW() - (%s || ' days')::interval")
            params.append(str(int(q['min_age'])))
        except ValueError:
            pass
    if q.get('max_age'):
        try:
            parts.append(f"{EFFECTIVE_FS} > NOW() - (%s || ' days')::interval")
            params.append(str(int(q['max_age'])))
        except ValueError:
            pass

    if q.get('has_drop') == '1':
        parts.append("i.price_drop_amount IS NOT NULL")
    if q.get('has_verified_age') == '1':
        parts.append("i.verified_source = 'vauto_rbook'")

    where = "d.active = TRUE AND " + " AND ".join(parts) if parts else "d.active = TRUE"

    with _db() as conn, conn.cursor() as cur:
        # Pull dealers + makes once for the form dropdowns
        cur.execute("SELECT id, name FROM dealers WHERE active = TRUE ORDER BY name")
        dealers_all = cur.fetchall()
        cur.execute("SELECT DISTINCT make FROM dealer_inventory "
                    "WHERE make IS NOT NULL AND make <> '' ORDER BY make")
        makes_all = [r['make'] for r in cur.fetchall()]

        # Only run the results query if at least one filter beyond active is set
        rows = []
        if parts:
            cur.execute(f"""
                SELECT i.id, i.dealer_id, d.name AS dealer_name,
                       i.year, i.make, i.model, i.trim, i.vin, i.ext_color,
                       i.mileage, i.price, i.url, i.photo_url, i.status,
                       i.source_added_at, i.first_seen_at,
                       i.verified_at, i.verified_days_on_lot,
                       i.price_drop_amount, i.price_drop_at, i.last_price,
                       (SELECT b.id FROM bids b
                          WHERE b.vin = i.vin
                            AND b.status IN ('new','reviewing','bid_sent')
                          ORDER BY b.created_at DESC LIMIT 1) AS active_bid_id
                FROM dealer_inventory i
                JOIN dealers d ON d.id = i.dealer_id
                WHERE {where}
                ORDER BY {EFFECTIVE_FS} ASC
                LIMIT 500
            """, tuple(params))
            rows = cur.fetchall()

    return render_template('dealers_search.html',
                           rows=rows, q=q,
                           dealers_all=dealers_all, makes_all=makes_all,
                           selected_dealer_ids=set(int(d) for d in dealer_ids if d.isdigit()))


@bp.route('/dealers/price-drops')
def dealers_price_drops():
    """Active cars where the price has dropped scan-over-scan. Sold/missing
    excluded — drops are only actionable on cars still buyable.
    Accepts ?dealer_id=N to scope to one partner."""
    dealer_id = request.args.get('dealer_id', type=int)
    params = []
    where = "i.status = 'active' AND d.active AND i.price_drop_amount IS NOT NULL"
    if dealer_id:
        where += " AND i.dealer_id = %s"
        params.append(dealer_id)
    with _db() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT i.id, i.dealer_id, d.name AS dealer_name,
                   i.year, i.make, i.model, i.trim, i.vin, i.ext_color,
                   i.mileage, i.price, i.url, i.photo_url,
                   i.source_added_at, i.first_seen_at,
                   i.verified_at, i.verified_days_on_lot,
                   i.price_drop_amount, i.price_drop_at, i.last_price
            FROM dealer_inventory i
            JOIN dealers d ON d.id = i.dealer_id
            WHERE {where}
            ORDER BY i.price_drop_at DESC NULLS LAST, i.price_drop_amount DESC
        """, tuple(params))
        rows = cur.fetchall()
        title = 'Active cars with price drops'
        if dealer_id and rows:
            title = f'{rows[0]["dealer_name"]} — cars with price drops'
    return render_template('dealers_cross_list.html',
                           rows=rows, title=title,
                           bucket='price_drops', mode='drops')


# ── APIs ─────────────────────────────────────────────────────────────────
_scan_threads = {}           # dealer_id -> Thread
_scan_lock = threading.Lock()


@bp.route('/api/bids/from_dealer_inventory', methods=['POST'])
def api_bid_from_dealer_inventory():
    """Create a bids row from a dealer_inventory row. Powers the green
    'SUBMIT BID' button on the Dealer DB search page — pushes a car the EW
    client wants to bid on into the main bid pipeline so vAuto/AccuTrade/iPacket
    lookups run automatically. Client then decides bid amount from the
    dashboard with full enrichment data on hand.

    Idempotency: if there's already a bid for this VIN in status new/
    reviewing/bid_sent, return that existing bid_id with `duplicate=True`
    so the client lands on the existing entry instead of creating a fork.
    """
    data = request.get_json(silent=True) or {}
    inv_id = data.get('inventory_id')
    if not inv_id:
        return jsonify({'error': 'inventory_id required'}), 400

    with _db() as conn, conn.cursor() as cur:
        cur.execute('''SELECT id, dealer_id, vin, year, make, model, trim,
                              ext_color, mileage, photo_url
                       FROM dealer_inventory WHERE id = %s''', (inv_id,))
        inv = cur.fetchone()
        if not inv:
            return jsonify({'error': 'inventory row not found'}), 404
        if not inv.get('vin') or len(inv['vin']) != 17:
            return jsonify({'error': 'inventory row has no valid VIN'}), 400

        # Idempotency check
        cur.execute('''SELECT id FROM bids
                       WHERE vin = %s
                         AND status IN ('new', 'reviewing', 'bid_sent')
                       ORDER BY created_at DESC LIMIT 1''',
                    (inv['vin'],))
        existing = cur.fetchone()
        if existing:
            return jsonify({'bid_id': existing['id'], 'duplicate': True})

        # Create new bid. `phone` is NOT NULL on bids, so use a synthetic
        # marker that distinguishes dealer-DB pushes from real customer
        # contact. The dashboard renders this as a system-source bid.
        cur.execute('''INSERT INTO bids (
                         vin, year, make, model, trim, color, mileage,
                         phone, raw_message, status,
                         partner_dealer_id, vauto_priority, created_at
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s,
                                 %s, %s, 'new',
                                 %s, TRUE, NOW())
                       RETURNING id''',
                    (inv['vin'], inv['year'], inv['make'], inv['model'],
                     inv.get('trim'), inv.get('ext_color'), inv.get('mileage'),
                     "sys:db_push",  # bids.phone is varchar(20) — keep short
                     f"Pushed from Dealer DB search (dealer_inventory id={inv_id})",
                     inv['dealer_id']))
        new_bid_id = cur.fetchone()['id']
        conn.commit()

    return jsonify({'bid_id': new_bid_id, 'duplicate': False})


@bp.route('/api/dealer/create', methods=['POST'])
def create_dealer():
    data = request.get_json(silent=True) or request.form
    url = (data.get('url') or '').strip().rstrip('/')
    name = (data.get('name') or '').strip()
    notes = (data.get('notes') or '').strip() or None
    contact_email = (data.get('contact_email') or '').strip()
    contact_phone = (data.get('contact_phone') or '').strip()
    contact_full_name = (data.get('contact_full_name') or '').strip() or None
    sms_opt_in = bool(data.get('sms_opt_in')) and bool(contact_phone)
    email_bid_alerts = bool(data.get('email_bid_alerts', True))
    if not url or not name:
        return jsonify({'error': 'url and name required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url

    with _db() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO dealers (name, url, notes, phone)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (url) DO UPDATE SET
                         name = EXCLUDED.name,
                         phone = COALESCE(EXCLUDED.phone, dealers.phone)
                       RETURNING id''',
                    (name, url, notes, contact_phone or None))
        dealer_id = cur.fetchone()['id']
        conn.commit()

    # Pre-provision the partner-portal account when contact email is provided.
    # No activation code, no magic link — single welcome email with credentials.
    # Dealer can log in immediately and start receiving bid notifications.
    welcome_result = None
    if contact_email:
        try:
            from partner_portal import create_welcome_account
            welcome_result = create_welcome_account(
                dealer_id=dealer_id,
                email=contact_email,
                phone=contact_phone or None,
                sms_opt_in=sms_opt_in,
                email_bid_alerts=email_bid_alerts,
                full_name=contact_full_name,
            )
        except Exception as e:
            print(f'[create_dealer] welcome-account failed for dealer {dealer_id}: {e}',
                  flush=True)
            welcome_result = {'success': False, 'error': str(e)}

    _trigger_scan(dealer_id)
    return jsonify({
        'dealer_id': dealer_id,
        'scan_started': True,
        'partner_account': welcome_result,
    })


@bp.route('/api/dealer/<int:dealer_id>/scan', methods=['POST'])
def scan_now(dealer_id):
    started = _trigger_scan(dealer_id)
    return jsonify({'dealer_id': dealer_id, 'scan_started': started})


@bp.route('/api/dealer/<int:dealer_id>/scan-status')
def scan_status(dealer_id):
    with _db() as conn, conn.cursor() as cur:
        cur.execute('''SELECT * FROM dealer_scans WHERE dealer_id = %s
                       ORDER BY started_at DESC LIMIT 1''', (dealer_id,))
        latest = cur.fetchone()
        cur.execute('SELECT last_scan_status FROM dealers WHERE id = %s', (dealer_id,))
        d = cur.fetchone()
    with _scan_lock:
        running = dealer_id in _scan_threads and _scan_threads[dealer_id].is_alive()
    return _json_response({
        'running': running,
        'last_status': (d or {}).get('last_scan_status'),
        'scan': dict(latest) if latest else None,
        'pipeline': _pipeline_stats(dealer_id),
    })


@bp.route('/api/dealer/<int:dealer_id>/delete', methods=['POST'])
def delete_dealer(dealer_id):
    """Soft-delete: mark inactive. Inventory stays (feedback_dia_rules: never delete data)."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute('UPDATE dealers SET active=FALSE WHERE id=%s', (dealer_id,))
        conn.commit()
    return jsonify({'ok': True})


@bp.route('/api/dealer/<int:dealer_id>')
def dealer_json(dealer_id):
    with _db() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM dealer_stats WHERE dealer_id = %s', (dealer_id,))
        d = cur.fetchone()
    return _json_response(dict(d) if d else None)


# ── Helpers ──────────────────────────────────────────────────────────────
def _trigger_scan(dealer_id):
    """Spawn a scan thread for dealer_id. Cross-process-safe via a Postgres
    advisory lock — gunicorn runs N workers each with its own thread dict,
    so the in-process `_scan_lock` alone could let two workers spawn parallel
    scanners on a double-click. The advisory lock is held by whichever
    backend connection acquires it; if another worker tries while the scan
    is in flight, `pg_try_advisory_lock` returns false and we bail out.
    Lock key uses 0x5CA0_0000 + dealer_id for namespace separation."""
    LOCK_NS = 0x5CA00000

    with _scan_lock:
        t = _scan_threads.get(dealer_id)
        if t and t.is_alive():
            return False

        # Pre-flight check: don't start if another worker is already running
        # this dealer's scan. Atomic via Postgres.
        try:
            with _db() as conn, conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_NS + dealer_id,))
                got = cur.fetchone()
                got = got[0] if not isinstance(got, dict) else got.get('pg_try_advisory_lock')
                if not got:
                    print(f'[dealer_db] scan {dealer_id} skipped — '
                          f'advisory lock held by another worker', flush=True)
                    return False
                # Release immediately; the worker thread will re-acquire
                # for the duration of its scan.
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_NS + dealer_id,))
                conn.commit()
        except Exception as e:
            print(f'[dealer_db] advisory-lock probe failed: {e}', flush=True)
            # Fall through — better to allow a double-scan than to never scan

        def _run():
            try:
                # Hold the advisory lock for the lifetime of this scan thread.
                # Connection close releases it automatically (no pg_advisory_unlock
                # needed — session-level locks die with the session).
                with _db() as conn, conn.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s)",
                                (LOCK_NS + dealer_id,))
                    got = cur.fetchone()
                    got = got[0] if not isinstance(got, dict) else got.get('pg_try_advisory_lock')
                    if not got:
                        print(f'[dealer_db] scan {dealer_id} aborted — '
                              f'lock raced with another worker', flush=True)
                        return
                    conn.commit()
                    try:
                        scanner = dealer_scanner.DealerScanner.from_dealer_id(dealer_id)
                        scanner.run()
                    finally:
                        try:
                            cur.execute("SELECT pg_advisory_unlock(%s)",
                                        (LOCK_NS + dealer_id,))
                            conn.commit()
                        except Exception:
                            pass
            except Exception as e:
                print(f'[dealer_db] scan {dealer_id} failed: {e}', flush=True)

        th = threading.Thread(target=_run, daemon=True, name=f'dealer-scan-{dealer_id}')
        _scan_threads[dealer_id] = th
        th.start()
        return True


def _group_by_make(rows):
    """Build { make: { 'count': N, 'rows': [rows sorted by price desc] } }."""
    out = {}
    for r in rows:
        make = r.get('make') or 'Unknown'
        m = out.setdefault(make, {'count': 0, 'rows': []})
        m['count'] += 1
        m['rows'].append(dict(r))
    # Sort rows within each make by price DESC (NULLs last)
    for m in out.values():
        m['rows'].sort(key=lambda v: (v.get('price') is None, -(v.get('price') or 0)))
    # Sort makes by count DESC
    return dict(sorted(out.items(), key=lambda kv: -kv[1]['count']))


def _pipeline_stats(dealer_id):
    """Coverage snapshot for the Pipeline Status card on the dealer detail page."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute('''SELECT
            COUNT(*) FILTER (WHERE status='active')                        AS total_active,
            COUNT(*) FILTER (WHERE status='active' AND ext_color IS NOT NULL
                                                 AND ext_color <> '')      AS with_color,
            COUNT(*) FILTER (WHERE status='active' AND photos IS NOT NULL) AS with_photos,
            COUNT(*) FILTER (WHERE status='active' AND price_drop_amount IS NOT NULL) AS with_drops,
            COUNT(*) FILTER (WHERE status='sold')                          AS sold_total
          FROM dealer_inventory WHERE dealer_id = %s''',
                    (dealer_id,))
        r = cur.fetchone() or {}
        total = r.get('total_active') or 0
        with_color = r.get('with_color') or 0
        with_photos = r.get('with_photos') or 0
        color_pct = round(100 * with_color / total) if total else 0
        photo_pct = round(100 * with_photos / total) if total else 0

        cur.execute('''SELECT COALESCE(SUM(colors_detected),0) AS colors_last_24h,
                              COUNT(*)                         AS scans_last_24h
                       FROM dealer_scans
                       WHERE dealer_id = %s AND started_at > NOW() - INTERVAL '24 hours' ''',
                    (dealer_id,))
        last_24h = cur.fetchone() or {'colors_last_24h': 0, 'scans_last_24h': 0}
    return {
        'total_active': total,
        'with_color': with_color,
        'with_photos': with_photos,
        'with_drops': r.get('with_drops') or 0,
        'sold_total': r.get('sold_total') or 0,
        'color_pct': color_pct,
        'photo_pct': photo_pct,
        'color_remaining': max(0, total - with_color),
        'photo_remaining': max(0, total - with_photos),
        'colors_last_24h': last_24h.get('colors_last_24h') or 0,
        'scans_last_24h': last_24h.get('scans_last_24h') or 0,
    }


def _age_buckets(rows):
    """Bucket cars by age. Precedence: vAuto-verified > dealer source_added_at
    > scanner first_seen_at. Calendar-day diff in ET (dealer local)."""
    et = timezone(timedelta(hours=-4))
    today_et = datetime.now(et).date()
    bucket = {'under_30': 0, 'd30_60': 0, 'd60_90': 0, 'over_90': 0}
    for r in rows:
        vd = r.get('verified_days_on_lot')
        va = r.get('verified_at')
        if vd is not None and va is not None:
            if va.tzinfo is None:
                va = va.replace(tzinfo=timezone.utc)
            elapsed = max(0, (today_et - va.astimezone(et).date()).days)
            days = max(0, int(vd) + elapsed)
        else:
            fs = r.get('source_added_at') or r.get('first_seen_at')
            if not fs:
                continue
            if fs.tzinfo is None:
                fs = fs.replace(tzinfo=timezone.utc)
            days = max(0, (today_et - fs.astimezone(et).date()).days)
        if days < 30:
            bucket['under_30'] += 1
        elif days < 60:
            bucket['d30_60'] += 1
        elif days < 90:
            bucket['d60_90'] += 1
        else:
            bucket['over_90'] += 1
    return bucket


@bp.app_template_filter('age_days')
def age_days_filter(fs):
    if not fs:
        return '—'
    if hasattr(fs, 'tzinfo') and fs.tzinfo is None:
        fs = fs.replace(tzinfo=timezone.utc)
    # Calendar-day diff in ET (dealer local). Seen today = 0, yesterday = 1.
    # Using (now - fs).days truncates to full 24h windows, so a car first seen
    # yesterday afternoon would show "0 d" until 24h elapsed — wrong mental model.
    et = timezone(timedelta(hours=-4))  # EDT; swap to -5 in winter if it matters
    return max(0, (datetime.now(et).date() - fs.astimezone(et).date()).days)


@bp.app_template_filter('best_age_days')
def best_age_days_filter(row):
    """Age source precedence (most-trusted first):
    1. vAuto-verified: verified_days_on_lot + days_since_verification
       (catches photo-reupload scams — Cox crawler first-seen-anywhere)
    2. Dealer-declared source_added_at (JSON-LD datePosted / sitemap <lastmod>)
    3. Our scanner's first_seen_at"""
    if not row:
        return '—'
    # Accept dict-like rows OR plain datetimes (back-compat when callers pass fs directly)
    if hasattr(row, 'get'):
        vd = row.get('verified_days_on_lot')
        va = row.get('verified_at')
        if vd is not None and va is not None:
            if hasattr(va, 'tzinfo') and va.tzinfo is None:
                va = va.replace(tzinfo=timezone.utc)
            et = timezone(timedelta(hours=-4))
            elapsed = (datetime.now(et).date() - va.astimezone(et).date()).days
            return max(0, int(vd) + max(0, elapsed))
        fs = row.get('source_added_at') or row.get('first_seen_at')
    else:
        fs = row
    return age_days_filter(fs)


@bp.app_template_filter('age_source')
def age_source_filter(row):
    """Label hint so the UI can show where the age came from.
    Returns 'vauto' | 'dealer' | 'scan' | ''."""
    if not row or not hasattr(row, 'get'):
        return ''
    if row.get('verified_at') and row.get('verified_days_on_lot') is not None:
        return 'vauto'
    if row.get('source_added_at'):
        return 'dealer'
    if row.get('first_seen_at'):
        return 'scan'
    return ''


@bp.app_template_filter('money')
def money_filter(v):
    """Price cell renderer. NULL / 0 / <$1000 → bold red 'NO PRICE' (dealer
    opted for 'Call for Price' or showed a KBB reference only). We don't want
    KBB values in our DB — those are the dealer's reference, not their ask."""
    if v is None or (isinstance(v, (int, float)) and v < 1000):
        return Markup('<span class="no-price">NO PRICE</span>')
    try:
        return f'${int(v):,}'
    except (ValueError, TypeError):
        return str(v)


@bp.app_template_filter('miles')
def miles_filter(v):
    if v is None:
        return '—'
    try:
        return f'{int(v):,} mi'
    except (ValueError, TypeError):
        return str(v)


@bp.app_template_filter('time_ago')
def time_ago_filter(ts):
    if not ts:
        return 'never'
    if hasattr(ts, 'tzinfo') and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    diff = now - ts
    sec = int(diff.total_seconds())
    if sec < 60:
        return f'{sec}s ago'
    if sec < 3600:
        return f'{sec // 60}m ago'
    if sec < 86400:
        return f'{sec // 3600}h ago'
    return f'{sec // 86400}d ago'
