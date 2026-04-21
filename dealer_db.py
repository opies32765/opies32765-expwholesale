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

        cur.execute('''SELECT * FROM dealer_inventory
                       WHERE dealer_id = %s AND status = 'sold'
                       ORDER BY sold_at DESC NULLS LAST LIMIT 500''',
                    (dealer_id,))
        sold = cur.fetchall()

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

    return render_template('dealers_detail.html',
                           dealer=dealer, tree=tree, buckets=buckets,
                           sold=sold, scans=scans, pipeline=pipeline)


# ── APIs ─────────────────────────────────────────────────────────────────
_scan_threads = {}           # dealer_id -> Thread
_scan_lock = threading.Lock()


@bp.route('/api/dealer/create', methods=['POST'])
def create_dealer():
    data = request.get_json(silent=True) or request.form
    url = (data.get('url') or '').strip().rstrip('/')
    name = (data.get('name') or '').strip()
    notes = (data.get('notes') or '').strip() or None
    if not url or not name:
        return jsonify({'error': 'url and name required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url

    with _db() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO dealers (name, url, notes)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (url) DO UPDATE SET name = EXCLUDED.name
                       RETURNING id''',
                    (name, url, notes))
        dealer_id = cur.fetchone()['id']
        conn.commit()

    _trigger_scan(dealer_id)
    return jsonify({'dealer_id': dealer_id, 'scan_started': True})


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
    with _scan_lock:
        t = _scan_threads.get(dealer_id)
        if t and t.is_alive():
            return False

        def _run():
            try:
                scanner = dealer_scanner.DealerScanner.from_dealer_id(dealer_id)
                scanner.run()
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
    now = datetime.now(timezone.utc)
    bucket = {'under_30': 0, 'd30_60': 0, 'd60_90': 0, 'over_90': 0}
    for r in rows:
        fs = r.get('first_seen_at')
        if not fs:
            continue
        if fs.tzinfo is None:
            fs = fs.replace(tzinfo=timezone.utc)
        days = (now - fs).days
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
    now = datetime.now(timezone.utc)
    return (now - fs).days


@bp.app_template_filter('money')
def money_filter(v):
    if v is None:
        return '—'
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
