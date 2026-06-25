"""
recon_routes.py  —  EW Recon (Time-to-Line reconditioning tracker) Blueprint.

Phase 1 (RECON_PHASE1_2026_06_24): schema-backed manager board ("Recon" tab) +
per-unit detail page + manual unit entry + single-click step advance + audit.

Ships DARK behind the sentinel  /opt/expwholesale/RECON_ENABLED  — every route
404s and the dashboard nav link hides while the file is absent. Flip on with
`touch /opt/expwholesale/RECON_ENABLED`; flip off with `rm` (10s cache).

Drift-resistant: registered by wsgi.py on every gunicorn worker boot, the same
pattern as the wholesaler_review / network_push blueprints, so an app.py
overwrite by a parallel session can't lose these routes.

HARD RULES honored: recon_* have NO FK to bids -> recon can never block or be
blocked by the enrichment pipeline (HR1). All writes go to EW Postgres; LSL is
never touched (HR6). No external pulls / no retry loops here (HR3). No cloud LLM
(HR4). C1-only (HR5). Migration is a separate hand-run script, never import-time
DDL (HR8). Sentinel gate = operator rollout control (HR9).
"""
from __future__ import annotations
import os
import time
import json
import secrets
from datetime import datetime, timezone, timedelta

from flask import (Blueprint, render_template, request, jsonify, abort, session)

bp = Blueprint('recon', __name__)

SENTINEL = '/opt/expwholesale/RECON_ENABLED'
_RECON_CACHE = {'t': 0.0, 'v': False}
STORE_ID = 1

# ── owners' pipeline + transport/recon emails ───────────────────────────────
RECON_EMAIL_LIVE = '/opt/expwholesale/RECON_EMAILS_LIVE'   # absent = STAGED to the test inbox
RECON_EMAIL_TEST_TO = os.environ.get('RECON_EMAIL_TEST_TO', 'austin@experience-wholesale.com')
# send from the Resend-verified experience-wholesale.net domain; reply lands in M365
RECON_EMAIL_FROM = os.environ.get('RECON_EMAIL_FROM',
                                  'Experience Wholesale <recon@experience-wholesale.net>')
RECON_EMAIL_REPLY_TO = os.environ.get('RECON_EMAIL_REPLY_TO', 'oscar@experience-wholesale.com')
EMAIL_RECIP = {'austin': 'austin@experience-wholesale.com',
               'rose': 'Rose@experience-wholesale.com'}
HOME_BASE = 'Home Base (Pompano)'
# LSL web record deep-link. %s = inventory id (recon_units.lsl_inventory_ref).
# ⚠ best-guess path — confirm the exact app.livesaleslog.com route with the operator.
LSL_RECORD_URL = 'https://app.livesaleslog.com/inventory/%s'
# read-only LSL mirror — dealer/seller/buyer + carrier address book lives here (HR6).
LSL_DB = os.environ.get('LSL_DB_PATH', '/opt/livesaleslog/crm.db')

# status dot colors (board + sidebar + car page): red = new/staged,
# yellow = in transit, green = delivered to the BUYING dealer (not home base).
DOT_COLORS = {
    'all': '#e23b3b', 'dealer_to_dealer': '#e23b3b', 'dealer_to_home': '#e23b3b',
    'indiv_to_dealer': '#e23b3b', 'indiv_to_home': '#e23b3b',
    'in_transport': '#eab308', 'arrived_dealer': '#22c55e',
    'arrived_home': '#3b82f6', 'ready': '#8b5cf6', 'picked_up': '#9ca3af',
}


def _recon_enabled():
    """True only when the sentinel file exists. 10s cache; file-based, survives
    restarts — same pattern as _accutrade_decoupled / LOCAL_BRAIN_ON."""
    now = time.time()
    if now - _RECON_CACHE['t'] < 10:
        return _RECON_CACHE['v']
    try:
        v = os.path.exists(SENTINEL)
    except Exception:
        v = False
    _RECON_CACHE['t'] = now
    _RECON_CACHE['v'] = v
    return v


def _db():
    """Lazy import to avoid a circular dependency on app.py."""
    from app import get_db
    return get_db()


def _utcnow():
    return datetime.now(timezone.utc)


def _actor():
    try:
        return (session.get('username') or session.get('user')
                or session.get('owner') or 'operator')
    except Exception:
        return 'operator'


def _audit(cur, unit_id, entity, entity_id, action, actor, detail=None):
    cur.execute(
        "INSERT INTO recon_audit (unit_id, entity, entity_id, action, actor, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (unit_id, entity, entity_id, action, actor, json.dumps(detail or {})))


def _steps(cur):
    cur.execute("SELECT * FROM recon_step_defs WHERE active AND store_id=%s "
                "ORDER BY sort_order", (STORE_ID,))
    return [dict(r) for r in cur.fetchall()]


# ── LSL address book (read-only crm.db) — dealers/sellers/buyers + carriers ──
def _companies(cur):
    """Saved carrier list for the dropdown (seed + anything Austin has added)."""
    cur.execute("SELECT name FROM recon_transport_companies WHERE active ORDER BY name")
    return [r['name'] for r in cur.fetchall()]


def _lsl_conn():
    import sqlite3
    c = sqlite3.connect('file:%s?mode=ro' % LSL_DB, uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _lsl_party(name):
    """Resolve a dealer/seller/buyer name -> {address, phone, contact} from the
    LSL mirror. Tries suppliers (structured address) first, then customers
    (individuals). Returns {} on no match. Pure read (HR6)."""
    name = (name or '').strip()
    if not name:
        return {}
    try:
        c = _lsl_conn()
        try:
            r = c.execute(
                "SELECT name, primary_contact, primary_contact_mobile, office, "
                "address1, address2, city, state, postal_code, full_address "
                "FROM suppliers WHERE name=? COLLATE NOCASE LIMIT 1", (name,)).fetchone()
            if not r:
                r = c.execute(
                    "SELECT name, primary_contact, primary_contact_mobile, office, "
                    "address1, address2, city, state, postal_code, full_address "
                    "FROM suppliers WHERE name LIKE ? ORDER BY length(name) LIMIT 1",
                    (name + '%',)).fetchone()
            if r:
                # build a tidy single line from structured parts; the LSL
                # full_address is often duplicated, so only use it as a fallback
                line2 = ' '.join(x for x in [r['city'], r['state'], r['postal_code']] if x and x.strip())
                # address2 in LSL is frequently a duplicate of the street/city, so skip it
                addr = ', '.join(x for x in [r['address1'], line2] if x and x.strip())
                if not addr:
                    addr = (r['full_address'] or '').strip()
                return {'address': addr.strip(' ,'),
                        'phone': (r['office'] or r['primary_contact_mobile'] or '').strip(),
                        'contact': (r['primary_contact'] or '').strip()}
            r = c.execute(
                "SELECT company_name, full_name, mobile, full_address FROM customers "
                "WHERE company_name=? COLLATE NOCASE OR full_name=? COLLATE NOCASE LIMIT 1",
                (name, name)).fetchone()
            if r:
                return {'address': (r['full_address'] or '').strip(),
                        'phone': (r['mobile'] or '').strip(),
                        'contact': (r['full_name'] or '').strip()}
        finally:
            c.close()
    except Exception as e:
        print('[recon-lsl-party] %s' % e, flush=True)
    return {}


def _resolve_party(u, side):
    """Stored override (recon_units.<side>_*) wins; otherwise auto-fill from LSL
    using bought_from (pickup) / sold_to (delivery). side in {'pickup','delivery'}."""
    nm = u.get('bought_from') if side == 'pickup' else u.get('sold_to')
    sug = _lsl_party(nm)
    return {
        'name': nm or '',
        'address': u.get(side + '_address') or sug.get('address', ''),
        'phone':   u.get(side + '_phone')   or sug.get('phone', ''),
        'contact': u.get(side + '_contact') or sug.get('contact', ''),
        'from_lsl': bool(sug) and not (u.get(side + '_address')),
    }


# ── owners' pipeline flow (path-aware next options) ─────────────────────────
def _next_options(code, path):
    flow = {
        'all': [('dealer_to_dealer', 'Dealer to Dealer'),
                ('dealer_to_home', 'Dealer to Home'),
                ('indiv_to_dealer', 'Individual to Dealer'),
                ('indiv_to_home', 'Individual to Home')],
        'dealer_to_dealer': [('in_transport', 'Move to In Transit')],
        'dealer_to_home': [('in_transport', 'Move to In Transit')],
        'indiv_to_dealer': [('in_transport', 'Move to In Transit')],
        'indiv_to_home': [('in_transport', 'Move to In Transit')],
        'arrived_home': [('ready', 'Mark Ready')],
        'ready': [('picked_up', 'Mark Picked Up from Home Base')],
    }
    if code == 'in_transport':
        if path == 'to_dealer':
            return [('arrived_dealer', 'Mark Arrived at Dealer')]
        if path == 'to_home':
            return [('arrived_home', 'Mark Arrived Home Base')]
        return [('arrived_dealer', 'Arrived at Dealer'),
                ('arrived_home', 'Arrived Home Base')]
    return flow.get(code, [])


# ── transport/recon emails (Resend via owner_portal._send_email) ────────────
# STAGED to RECON_EMAIL_TEST_TO unless the RECON_EMAILS_LIVE sentinel exists.
def _recon_send_raw(to_addr, subject, html):
    """Send a recon email via Resend DIRECTLY — bypassing owner_portal._send_email,
    whose global EMAIL_TEST_TO redirect would otherwise re-route EVERY recon email
    to the app-wide test inbox (opies32765). Recon does its own staging via
    RECON_EMAIL_TEST_TO, so it must not be double-redirected."""
    key = os.environ.get('RESEND_API_KEY', '')
    if not key:
        print('[recon-email:STUB] to=%s subj=%s' % (to_addr, subject), flush=True)
        return False
    try:
        import resend
        resend.api_key = key
        payload = {'from': RECON_EMAIL_FROM, 'to': to_addr, 'subject': subject, 'html': html}
        if RECON_EMAIL_REPLY_TO:
            payload['reply_to'] = RECON_EMAIL_REPLY_TO
        resend.Emails.send(payload)
        return True
    except Exception as e:
        print('[recon-email:FAIL] %s: %s' % (type(e).__name__, e), flush=True)
        return False


def _recon_email(cur, unit_id, kind, to_intended, subject, body, dedupe=True):
    if dedupe:
        cur.execute("SELECT 1 FROM recon_email_outbox WHERE unit_id=%s AND kind=%s LIMIT 1",
                    (unit_id, kind))
        if cur.fetchone():
            return  # idempotent: one auto-email per (unit, kind)
    live = os.path.exists(RECON_EMAIL_LIVE)
    to_actual = to_intended if live else RECON_EMAIL_TEST_TO
    subj = subject if live else ('[STAGED→%s] %s' % (to_intended, subject))
    html = '<div style="font:14px/1.5 -apple-system,Segoe UI,Arial,sans-serif">' \
           + (body or '').replace('\n', '<br>') + '</div>'
    ok = False
    try:
        ok = _recon_send_raw(to_actual, subj, html)
    except Exception as e:
        print('[recon-email] send error: %s' % e, flush=True)
    cur.execute("""INSERT INTO recon_email_outbox
                     (unit_id, kind, to_intended, to_actual, subject, body, staged, sent_ok)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (unit_id, kind, to_intended, to_actual, subj, body, (not live), ok))


def _maybe_emails_on_move(cur, u, to_code):
    # Austin's transport emails are MANUAL now (POST /api/recon/<id>/email-austin),
    # so moving a car never auto-spams him. Rose still gets the automatic event
    # notifications (real things that happened).
    ymm = ('%s %s %s' % (u.get('year') or '', u.get('make') or '',
                         u.get('model') or '')).strip()
    vin = u.get('vin')
    src = u.get('bought_from') or 'the seller'
    sold_to = u.get('sold_to')
    # individual purchase picked up (moved to transport) -> Rose
    if to_code == 'in_transport' and (u.get('buying_from_type') or '').lower() == 'individual':
        body = "%s (VIN %s) has been picked up from %s (individual sale)." % (ymm, vin, src)
        _recon_email(cur, u['id'], 'rose_pickup', EMAIL_RECIP['rose'],
                     'Picked up: %s' % ymm, body)
    # delivered to a dealer -> Rose
    if to_code == 'arrived_dealer':
        body = "%s (VIN %s) has been delivered to %s." % (ymm, vin, sold_to or 'the buying dealer')
        _recon_email(cur, u['id'], 'rose_delivered', EMAIL_RECIP['rose'],
                     'Delivered: %s' % ymm, body)


# ── photos: storage + AES-at-rest for pickup-proof + push ───────────────────
RECON_MEDIA_DIR = '/opt/expwholesale/recon_media'                 # encrypted store (outside static)
RECON_STATIC_DIR = '/opt/expwholesale/static/uploads/recon'       # plain car photos (served via /static/)
_FKEY = {'k': None}


def _fernet():
    """Lazy AES key (Fernet = AES-128-CBC + HMAC). Key in recon_media/.fernet.key (0600)."""
    from cryptography.fernet import Fernet
    if _FKEY['k'] is None:
        kp = os.path.join(RECON_MEDIA_DIR, '.fernet.key')
        try:
            os.makedirs(RECON_MEDIA_DIR, exist_ok=True)
            if os.path.exists(kp):
                key = open(kp, 'rb').read().strip()
            else:
                key = Fernet.generate_key()
                with open(kp, 'wb') as f:
                    f.write(key)
                os.chmod(kp, 0o600)
            _FKEY['k'] = Fernet(key)
        except Exception as e:
            print('[recon-photo] fernet key error: %s' % e, flush=True)
            return None
    return _FKEY['k']


def _recon_push(title, body, data=None):
    try:
        from ew_push import send_push
        send_push(title, body, data or {})
    except Exception as e:
        print('[recon-push] %s' % e, flush=True)


# ── gate: dark until the sentinel exists (require_login already ran app-level) ─
@bp.before_request
def _gate():
    if not _recon_enabled():
        abort(404)


# ── make recon_enabled() available to every Jinja render (drift-resistant nav) ─
@bp.app_context_processor
def _inject_recon():
    return {'recon_enabled': _recon_enabled}


# ============================================================================
# BOARD
# ============================================================================
def _hours(a, b):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600.0, 1)


def _days(a, b):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 86400.0, 1)


def _load_board(transit_all=False):
    """Returns (steps, units, kpis, transit_rows, tcounts). Live timers, pure read.
    The In-Transit lane is driven by recon_transport (the synced ops-sheet mirror);
    the recon step columns are driven by recon_units."""
    db = _db()
    cur = db.cursor()
    try:
        steps = _steps(cur)
        cur.execute("""
            SELECT u.*, sd.code AS step_code, sd.name AS step_name,
                   sd.sort_order AS step_sort, sd.sla_hours, sd.sla_hours_exotic,
                   hc.per_day_usd
              FROM recon_units u
              LEFT JOIN recon_step_defs sd ON sd.id = u.current_step_id
              LEFT JOIN recon_holding_cost_config hc
                     ON hc.store_id = u.store_id AND hc.vehicle_class = u.vehicle_class
                    AND hc.active
             WHERE u.store_id = %s
               AND u.status IN ('in_transit_stage0','in_recon','on_hold')
             ORDER BY u.current_step_entered_at NULLS FIRST
        """, (STORE_ID,))
        units = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT status, COUNT(*) c FROM recon_units WHERE store_id=%s "
                    "GROUP BY status", (STORE_ID,))
        status_counts = {r['status']: int(r['c']) for r in cur.fetchall()}
        # stage-0 transport lane (ops-sheet mirror); 'ours' by default, ?transit=all for everything
        where_our = '' if transit_all else 'AND is_our_unit'
        cur.execute("""SELECT vin, sub_status, ymm, company, pickup_loc, delivery_loc,
                              is_our_unit, est_delivery
                         FROM recon_transport
                        WHERE sub_status IN ('pending','in_transit') %s
                        ORDER BY is_our_unit DESC, sub_status, vin""" % where_our)
        transit_rows = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT sub_status, COUNT(*) all_c,
                              COUNT(*) FILTER (WHERE is_our_unit) our_c
                         FROM recon_transport GROUP BY sub_status""")
        traw = {r['sub_status']: dict(r) for r in cur.fetchall()}
    finally:
        db.close()

    def _tc(k, ours=True):
        r = traw.get(k) or {}
        return int(r.get('our_c' if ours else 'all_c') or 0)
    tcounts = {'pending': _tc('pending'), 'in_transit': _tc('in_transit'),
               'delivered': _tc('delivered'),
               'pending_all': _tc('pending', False),
               'in_transit_all': _tc('in_transit', False),
               'delivered_all': _tc('delivered', False)}

    now = _utcnow()
    burn = 0.0
    recon_days = []
    transit_days = []
    for u in units:
        u['days_in_recon'] = _days(u.get('entered_recon_at'), now)
        u['hours_in_step'] = _hours(u.get('current_step_entered_at'), now)
        sla = (u.get('sla_hours_exotic') if u.get('is_exotic') else u.get('sla_hours'))
        sla = float(sla) if sla is not None else None
        hrs = u['hours_in_step']
        if sla and hrs is not None:
            ratio = hrs / sla
            u['sla_state'] = 'breach' if ratio >= 1.0 else ('warn' if ratio >= 0.5 else 'ok')
        else:
            u['sla_state'] = 'ok'
        if u['status'] == 'in_recon':
            rate = float(u.get('per_day_usd') or 40)
            burn += rate
            if u.get('days_in_recon') is not None:
                recon_days.append(u['days_in_recon'])
        elif u['status'] == 'in_transit_stage0':
            d = _days(u.get('in_transit_at') or u.get('acquired_at'), now)
            if d is not None:
                transit_days.append(d)

    kpis = {
        'in_transit': tcounts['pending'] + tcounts['in_transit'],
        'in_recon': status_counts.get('in_recon', 0),
        'frontline_ready': status_counts.get('frontline_ready', 0),
        'holding_burn_day': round(burn, 2),
        'avg_days_in_recon': round(sum(recon_days) / len(recon_days), 1) if recon_days else None,
        'avg_transit': round(sum(transit_days) / len(transit_days), 1) if transit_days else None,
    }
    return steps, units, kpis, transit_rows, tcounts


@bp.route('/recon')
def board():
    """Rapid-Recon-style: a left step sidebar (with counts) + a main vehicle
    table (Step | Stock# | Vehicle | Days In Step | Days Since Recon Began |
    Days In DMS | Notes). ?step=<code> filters the table to one step."""
    sel = (request.args.get('step') or '').strip()
    db = _db()
    cur = db.cursor()
    try:
        steps = _steps(cur)
        cur.execute("""SELECT u.*, sd.code AS step_code, sd.name AS step_name,
                              sd.sort_order AS step_sort, sd.is_gate,
                              sd.sla_hours, sd.sla_hours_exotic
                         FROM recon_units u
                         LEFT JOIN recon_step_defs sd ON sd.id = u.current_step_id
                        WHERE u.store_id=%s
                          AND u.status IN ('in_transit_stage0','in_recon','on_hold')
                        ORDER BY sd.sort_order NULLS FIRST, u.current_step_entered_at""",
                    (STORE_ID,))
        units = [dict(r) for r in cur.fetchall()]
        ids = [u['id'] for u in units]
        notes_by = {}
        if ids:
            cur.execute("""SELECT unit_id,
                                  string_agg(body, '  •  ' ORDER BY created_at DESC) AS notes
                             FROM recon_notes WHERE unit_id = ANY(%s) GROUP BY unit_id""",
                        (ids,))
            notes_by = {r['unit_id']: r['notes'] for r in cur.fetchall()}
    finally:
        db.close()

    now = _utcnow()

    def _di(d):
        return int(d) if d is not None else None
    for u in units:
        hrs = _hours(u.get('current_step_entered_at'), now)
        u['d_step'] = _di(_days(u.get('current_step_entered_at'), now))
        u['d_recon'] = _di(_days(u.get('entered_recon_at'), now))
        u['d_dms'] = _di(_days(u.get('acquired_at') or u.get('entered_recon_at'), now))
        sla = (u.get('sla_hours_exotic') if u.get('is_exotic') else u.get('sla_hours'))
        sla = float(sla) if sla is not None else None
        u['sla_state'] = ('breach' if (sla and hrs is not None and hrs >= sla)
                          else ('warn' if (sla and hrs is not None and hrs >= sla * 0.5)
                                else 'ok'))
        u['notes'] = notes_by.get(u['id'], '')
        u['dot'] = DOT_COLORS.get(u.get('step_code'), '#cbd5e1')
        ref = u.get('lsl_inventory_ref')
        u['lsl_url'] = (LSL_RECORD_URL % ref) if ref else \
            ('https://app.livesaleslog.com/inventory?query=%s' % (u.get('stock_no') or ''))

    counts = {}
    for u in units:
        c = u.get('step_code') or '?'
        counts[c] = counts.get(c, 0) + 1
    stepnum = {s['code']: i + 1 for i, s in enumerate(steps)}
    rows = units if not sel else [u for u in units if u.get('step_code') == sel]
    return render_template('recon/dashboard.html', steps=steps, rows=rows,
                           counts=counts, stepnum=stepnum, total=len(units),
                           sel=sel, now=now, dot_colors=DOT_COLORS)


@bp.route('/api/recon/board')
def api_board():
    steps, units, kpis, transit_rows, tcounts = _load_board()
    out = []
    for u in units:
        out.append({
            'id': u['id'], 'vin': u['vin'], 'stock_no': u.get('stock_no'),
            'year': u.get('year'), 'make': u.get('make'), 'model': u.get('model'),
            'step_code': u.get('step_code'), 'step_name': u.get('step_name'),
            'status': u['status'], 'sub_status': u.get('sub_status'),
            'days_in_recon': u.get('days_in_recon'),
            'hours_in_step': u.get('hours_in_step'),
            'sla_state': u.get('sla_state'),
            'vehicle_class': u.get('vehicle_class'),
        })
    fp = '%d:%d,%d:%s' % (len(out), tcounts['pending'], tcounts['in_transit'],
                          ':'.join('%s,%s,%s' % (x['id'], x['step_code'], x['sla_state'])
                                   for x in out))
    return jsonify({'kpis': kpis, 'units': out, 'tcounts': tcounts, 'fp': fp})


# ============================================================================
# PER-UNIT DETAIL
# ============================================================================
@bp.route('/recon/u/<key>')
def unit_detail(key):
    key = (key or '').strip().upper()
    db = _db()
    cur = db.cursor()
    try:
        if len(key) == 17:
            cur.execute("SELECT * FROM recon_units WHERE vin=%s "
                        "ORDER BY created_at DESC LIMIT 1", (key,))
        else:
            cur.execute("SELECT * FROM recon_units WHERE UPPER(stock_no)=%s "
                        "ORDER BY created_at DESC LIMIT 1", (key,))
        row = cur.fetchone()
        if not row:
            abort(404)
        u = dict(row)
        cur.execute("""SELECT e.*, sd.code AS step_code, sd.name AS step_name,
                              sd.sort_order
                         FROM recon_step_events e
                         JOIN recon_step_defs sd ON sd.id = e.step_id
                        WHERE e.unit_id=%s ORDER BY e.entered_at""", (u['id'],))
        events = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM recon_workitems WHERE unit_id=%s "
                    "ORDER BY created_at", (u['id'],))
        workitems = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM recon_notes WHERE unit_id=%s "
                    "ORDER BY created_at DESC", (u['id'],))
        notes = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM recon_photos WHERE unit_id=%s ORDER BY created_at DESC", (u['id'],))
        photos = [dict(r) for r in cur.fetchall()]
        steps = _steps(cur)
        companies = _companies(cur)
    finally:
        db.close()

    now = _utcnow()
    for e in events:
        if e.get('exited_at'):
            e['hours'] = _hours(e['entered_at'], e['exited_at'])
        else:
            e['hours'] = _hours(e['entered_at'], now)
            e['open'] = True
    u['days_in_recon'] = _days(u.get('entered_recon_at'), now)
    u['time_in_transit'] = _days(u.get('acquired_at'), u.get('delivered_at') or now)
    if u.get('frontline_ready_at') and u.get('entered_recon_at'):
        u['t2l_recon'] = _days(u['entered_recon_at'], u['frontline_ready_at'])
    else:
        u['t2l_recon'] = u['days_in_recon']
    if u.get('frontline_ready_at') and u.get('acquired_at'):
        u['total_t2l'] = _days(u['acquired_at'], u['frontline_ready_at'])
    else:
        u['total_t2l'] = _days(u.get('acquired_at'), now)
    by_id = {s['id']: s for s in steps}
    cur_step = by_id.get(u.get('current_step_id'))
    cur_code = cur_step['code'] if cur_step else 'all'
    cur_name = cur_step['name'] if cur_step else 'ALL'
    next_opts = _next_options(cur_code, u.get('path'))
    pickup = _resolve_party(u, 'pickup')
    delivery = _resolve_party(u, 'delivery')
    return render_template('recon/unit.html', u=u, events=events,
                           workitems=workitems, notes=notes, photos=photos, steps=steps, now=now,
                           cur_code=cur_code, cur_name=cur_name, next_opts=next_opts,
                           companies=companies, pickup=pickup, delivery=delivery,
                           dot=DOT_COLORS.get(cur_code, '#cbd5e1'))


# ============================================================================
# MANUAL UNIT ENTRY (Phase 1 — operator seeds the board by hand)
# ============================================================================
@bp.route('/api/recon/add-unit', methods=['POST'])
def api_add_unit():
    data = request.get_json(silent=True) or request.form
    vin = (data.get('vin') or '').strip().upper()
    if len(vin) < 11:
        return jsonify({'error': 'a VIN (>= 11 chars) is required'}), 400

    def _i(v):
        try:
            return int(v) if str(v).strip() not in ('', 'None') else None
        except Exception:
            return None

    def _f(v):
        try:
            return float(v) if str(v).strip() not in ('', 'None') else None
        except Exception:
            return None

    year = _i(data.get('year'))
    make = (data.get('make') or '').strip() or None
    model = (data.get('model') or '').strip() or None
    trim = (data.get('trim') or '').strip() or None
    stock_no = (data.get('stock_no') or '').strip() or None
    miles = _i(data.get('miles'))
    purchase_cost = _f(data.get('purchase_cost'))
    vclass = (data.get('vehicle_class') or 'highline').strip()
    is_exotic = vclass == 'exotic'

    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT id, sla_hours, sla_hours_exotic FROM recon_step_defs "
                    "WHERE store_id=%s AND code='intake'", (STORE_ID,))
        st = cur.fetchone()
        if not st:
            return jsonify({'error': 'intake step missing — run the migration'}), 500
        intake_id = st['id']
        now = _utcnow()
        sla = st['sla_hours_exotic'] if is_exotic else st['sla_hours']
        sla_due = now + timedelta(hours=float(sla)) if sla is not None else None
        cur.execute("""
            INSERT INTO recon_units
                (vin, stock_no, year, make, model, trim, miles, purchase_cost,
                 vehicle_class, is_exotic, entered_recon_at, current_step_id,
                 current_step_entered_at, status, source, recon_token)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'in_recon','manual',%s)
            RETURNING id
        """, (vin, stock_no, year, make, model, trim, miles, purchase_cost,
              vclass, is_exotic, now, intake_id, now, secrets.token_urlsafe(16)))
        unit_id = cur.fetchone()['id']
        cur.execute("""INSERT INTO recon_step_events
                          (unit_id, step_id, entered_at, moved_by, move_reason)
                       VALUES (%s,%s,%s,%s,'manual_add') RETURNING id""",
                    (unit_id, intake_id, now, _actor()))
        ev_id = cur.fetchone()['id']
        cur.execute("""INSERT INTO recon_unit_active_steps
                          (unit_id, step_id, event_id, entered_at, sla_due_at)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (unit_id, step_id) DO NOTHING""",
                    (unit_id, intake_id, ev_id, now, sla_due))
        _audit(cur, unit_id, 'unit', unit_id, 'create', _actor(),
               {'vin': vin, 'class': vclass})
        db.commit()
        return jsonify({'ok': True, 'unit_id': unit_id, 'vin': vin})
    except Exception as e:
        db.rollback()
        msg = str(e)
        if 'ux_recon_units_vin_open' in msg:
            msg = 'that VIN already has an open recon record'
        return jsonify({'error': msg}), 400
    finally:
        db.close()


# ============================================================================
# SINGLE-CLICK ADVANCE  (the core verb)
# ============================================================================
@bp.route('/api/recon/<int:unit_id>/advance', methods=['POST'])
def api_advance(unit_id):
    data = request.get_json(silent=True) or request.form
    to_code = (data.get('to_step') or '').strip()  # optional explicit target

    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT * FROM recon_units WHERE id=%s FOR UPDATE", (unit_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'unit not found'}), 404
        u = dict(row)
        steps = _steps(cur)
        by_code = {s['code']: s for s in steps}
        by_id = {s['id']: s for s in steps}
        cur_step = by_id.get(u['current_step_id'])
        cur_sort = cur_step['sort_order'] if cur_step else -1

        if to_code:
            target = by_code.get(to_code)
            if not target:
                return jsonify({'error': 'unknown step %r' % to_code}), 400
        else:
            forward = [s for s in steps
                       if s['sort_order'] > cur_sort and s['code'] != 'transport']
            if not forward:
                return jsonify({'error': 'already at the final step'}), 400
            nonterm = [s for s in forward if not s['is_terminal']]
            target = nonterm[0] if nonterm else forward[0]

        # idempotent: advancing into the step it already occupies is a no-op
        if cur_step and target['id'] == cur_step['id']:
            db.rollback()
            return jsonify({'ok': True, 'to': target['code'], 'noop': True})

        now = _utcnow()
        is_rework = bool(cur_step and target['sort_order'] < cur_sort)
        actor = _actor()

        # close all open events + active lanes for this unit (Phase 1: single lane)
        cur.execute("""UPDATE recon_step_events
                          SET exited_at=%s,
                              duration_sec=EXTRACT(EPOCH FROM (%s - entered_at))::bigint
                        WHERE unit_id=%s AND exited_at IS NULL""", (now, now, unit_id))
        cur.execute("DELETE FROM recon_unit_active_steps WHERE unit_id=%s", (unit_id,))

        if target['is_terminal']:
            new_status = 'frontline_ready' if target['code'] == 'frontline' else 'wholesale'
            cur.execute("""
                UPDATE recon_units
                   SET current_step_id=%s, current_step_entered_at=%s, status=%s,
                       frontline_ready_at = CASE WHEN %s THEN %s ELSE frontline_ready_at END,
                       exited_at = CASE WHEN %s THEN %s ELSE exited_at END,
                       updated_at=now()
                 WHERE id=%s
            """, (target['id'], now, new_status,
                  target['code'] == 'frontline', now,
                  target['code'] == 'wholesale', now, unit_id))
            cur.execute("""INSERT INTO recon_step_events
                              (unit_id, step_id, entered_at, exited_at, duration_sec,
                               moved_by, move_reason, from_step_id, is_rework)
                           VALUES (%s,%s,%s,%s,0,%s,%s,%s,%s)""",
                        (unit_id, target['id'], now, now, actor,
                         'advance_terminal', u['current_step_id'], is_rework))
        else:
            # advancing out of a reopened frontline clears the T2L stop
            cur.execute("""
                UPDATE recon_units
                   SET current_step_id=%s, current_step_entered_at=%s, status='in_recon',
                       frontline_ready_at = CASE WHEN status='frontline_ready'
                                                 THEN NULL ELSE frontline_ready_at END,
                       updated_at=now()
                 WHERE id=%s
            """, (target['id'], now, unit_id))
            cur.execute("""INSERT INTO recon_step_events
                              (unit_id, step_id, entered_at, moved_by, move_reason,
                               from_step_id, is_rework)
                           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (unit_id, target['id'], now, actor,
                         'rework' if is_rework else 'advance',
                         u['current_step_id'], is_rework))
            ev_id = cur.fetchone()['id']
            sla = target['sla_hours_exotic'] if u.get('is_exotic') else target['sla_hours']
            sla_due = now + timedelta(hours=float(sla)) if sla is not None else None
            cur.execute("""INSERT INTO recon_unit_active_steps
                              (unit_id, step_id, event_id, entered_at, sla_due_at)
                           VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (unit_id, step_id) DO NOTHING""",
                        (unit_id, target['id'], ev_id, now, sla_due))

        _audit(cur, unit_id, 'step', target['id'], 'advance', actor,
               {'from': cur_step['code'] if cur_step else None,
                'to': target['code'], 'rework': is_rework})
        db.commit()
        return jsonify({'ok': True, 'to': target['code'],
                        'terminal': bool(target['is_terminal']), 'rework': is_rework})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ============================================================================
# NOTES  (add a note to a unit)
# ============================================================================
@bp.route('/api/recon/<int:unit_id>/note', methods=['POST'])
def api_add_note(unit_id):
    data = request.get_json(silent=True) or request.form
    body = (data.get('body') or '').strip()
    if not body:
        return jsonify({'error': 'note is empty'}), 400
    if len(body) > 4000:
        body = body[:4000]
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT current_step_id FROM recon_units WHERE id=%s", (unit_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'unit not found'}), 404
        cur.execute("""INSERT INTO recon_notes (unit_id, step_id, author, body)
                       VALUES (%s,%s,%s,%s) RETURNING id""",
                    (unit_id, row['current_step_id'], _actor(), body))
        nid = cur.fetchone()['id']
        _audit(cur, unit_id, 'note', nid, 'add', _actor(), {'len': len(body)})
        db.commit()
        return jsonify({'ok': True, 'note_id': nid})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@bp.route('/api/recon/<int:unit_id>/note/<int:note_id>/edit', methods=['POST'])
def api_edit_note(unit_id, note_id):
    data = request.get_json(silent=True) or request.form
    body = (data.get('body') or '').strip()
    if not body:
        return jsonify({'error': 'note is empty'}), 400
    body = body[:4000]
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE recon_notes SET body=%s WHERE id=%s AND unit_id=%s RETURNING id",
                    (body, note_id, unit_id))
        if not cur.fetchone():
            return jsonify({'error': 'note not found'}), 404
        _audit(cur, unit_id, 'note', note_id, 'edit', _actor(), {'len': len(body)})
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@bp.route('/api/recon/<int:unit_id>/note/<int:note_id>/delete', methods=['POST'])
def api_delete_note(unit_id, note_id):
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM recon_notes WHERE id=%s AND unit_id=%s RETURNING id",
                    (note_id, unit_id))
        if not cur.fetchone():
            return jsonify({'error': 'note not found'}), 404
        _audit(cur, unit_id, 'note', note_id, 'delete', _actor(), {})
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ============================================================================
# MOVE  (owners' pipeline: ALL -> d2d/d2h -> in_transport -> arrived -> recon -> ready -> picked_up)
# ============================================================================
@bp.route('/api/recon/<int:unit_id>/move', methods=['POST'])
def api_move(unit_id):
    data = request.get_json(silent=True) or request.form
    to_code = (data.get('to_step') or '').strip()
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT * FROM recon_units WHERE id=%s FOR UPDATE", (unit_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'unit not found'}), 404
        u = dict(row)
        steps = _steps(cur)
        by_code = {s['code']: s for s in steps}
        by_id = {s['id']: s for s in steps}
        cur_step = by_id.get(u['current_step_id'])
        cur_code = cur_step['code'] if cur_step else 'all'
        target = by_code.get(to_code)
        if not target:
            return jsonify({'error': 'unknown step %r' % to_code}), 400
        # free movement: any active status is allowed (no forced linear flow)

        now = _utcnow()
        actor = _actor()
        # the 4 staging steps set destination path (to_dealer/to_home) + source type
        new_path = u.get('path')
        new_bft = u.get('buying_from_type')
        if to_code in ('dealer_to_dealer', 'indiv_to_dealer'):
            new_path = 'to_dealer'
        elif to_code in ('dealer_to_home', 'indiv_to_home'):
            new_path = 'to_home'
        if to_code in ('indiv_to_dealer', 'indiv_to_home'):
            new_bft = 'Individual'
        elif to_code in ('dealer_to_dealer', 'dealer_to_home'):
            new_bft = 'Dealer'

        cur.execute("""UPDATE recon_step_events SET exited_at=%s,
              duration_sec=EXTRACT(EPOCH FROM (%s - entered_at))::bigint
            WHERE unit_id=%s AND exited_at IS NULL""", (now, now, unit_id))
        cur.execute("DELETE FROM recon_unit_active_steps WHERE unit_id=%s", (unit_id,))
        cur.execute("""UPDATE recon_units SET current_step_id=%s, current_step_entered_at=%s,
              path=%s, buying_from_type=%s, updated_at=now(),
              in_transit_at=CASE WHEN %s='in_transport' AND in_transit_at IS NULL THEN %s ELSE in_transit_at END,
              delivered_at=CASE WHEN %s IN ('arrived_home','arrived_dealer') AND delivered_at IS NULL THEN %s ELSE delivered_at END,
              entered_recon_at=CASE WHEN %s='arrived_home' AND entered_recon_at IS NULL THEN %s ELSE entered_recon_at END,
              frontline_ready_at=CASE WHEN %s='ready' AND frontline_ready_at IS NULL THEN %s ELSE frontline_ready_at END
            WHERE id=%s""",
            (target['id'], now, new_path, new_bft, to_code, now, to_code, now, to_code, now, to_code, now, unit_id))
        cur.execute("""INSERT INTO recon_step_events
                          (unit_id, step_id, entered_at, moved_by, move_reason, from_step_id)
                       VALUES (%s,%s,%s,%s,'move',%s) RETURNING id""",
                    (unit_id, target['id'], now, actor, u['current_step_id']))
        ev = cur.fetchone()['id']
        cur.execute("""INSERT INTO recon_unit_active_steps (unit_id, step_id, event_id, entered_at)
                       VALUES (%s,%s,%s,%s) ON CONFLICT (unit_id, step_id) DO NOTHING""",
                    (unit_id, target['id'], ev, now))
        u['path'] = new_path
        u['buying_from_type'] = new_bft
        _maybe_emails_on_move(cur, u, to_code)
        _audit(cur, unit_id, 'step', target['id'], 'move', actor, {'from': cur_code, 'to': to_code})
        db.commit()
        # push every device on a status change (deep-links to the car in the app)
        try:
            ymm = ('%s %s %s' % (u.get('year') or '', u.get('make') or '',
                                 u.get('model') or '')).strip()
            _recon_push('Recon update', '%s → %s' % (ymm or u.get('vin'), target['name']),
                        {'reconKey': u.get('recon_token') or u.get('vin')})
        except Exception:
            pass
        return jsonify({'ok': True, 'to': to_code})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ── per-car field setters: needs-photos checkbox + transport company ────────
@bp.route('/api/recon/<int:unit_id>/photos-flag', methods=['POST'])
def api_photos_flag(unit_id):
    data = request.get_json(silent=True) or request.form
    val = bool(data.get('needs_photos'))
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE recon_units SET needs_photos=%s, updated_at=now() "
                    "WHERE id=%s RETURNING id", (val, unit_id))
        if not cur.fetchone():
            return jsonify({'error': 'unit not found'}), 404
        _audit(cur, unit_id, 'unit', unit_id, 'photos_flag', _actor(), {'needs_photos': val})
        db.commit()
        return jsonify({'ok': True, 'needs_photos': val})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@bp.route('/api/recon/<int:unit_id>/transport-company', methods=['POST'])
def api_transport_company(unit_id):
    data = request.get_json(silent=True) or request.form
    company = (data.get('transport_company') or '').strip() or None
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE recon_units SET transport_company=%s, updated_at=now() "
                    "WHERE id=%s RETURNING id", (company, unit_id))
        if not cur.fetchone():
            return jsonify({'error': 'unit not found'}), 404
        _audit(cur, unit_id, 'unit', unit_id, 'transport_company', _actor(), {'company': company})
        db.commit()
        return jsonify({'ok': True, 'transport_company': company})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@bp.route('/api/recon/<int:unit_id>/transport-details', methods=['POST'])
def api_transport_details(unit_id):
    """Carrier + editable pickup (seller) and delivery (buyer) address/phone/
    contact. A newly-typed carrier is remembered for the dropdown."""
    data = request.get_json(silent=True) or request.form
    def g(k):
        return (data.get(k) or '').strip() or None
    company = g('transport_company')
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("""UPDATE recon_units SET transport_company=%s,
              pickup_address=%s, pickup_phone=%s, pickup_contact=%s,
              delivery_address=%s, delivery_phone=%s, delivery_contact=%s,
              est_pickup_date=%s, est_delivery_date=%s,
              updated_at=now() WHERE id=%s RETURNING id""",
            (company, g('pickup_address'), g('pickup_phone'), g('pickup_contact'),
             g('delivery_address'), g('delivery_phone'), g('delivery_contact'),
             g('est_pickup_date'), g('est_delivery_date'), unit_id))
        if not cur.fetchone():
            return jsonify({'error': 'unit not found'}), 404
        if company:
            cur.execute("INSERT INTO recon_transport_companies (name) VALUES (%s) "
                        "ON CONFLICT (name) DO NOTHING", (company,))
        _audit(cur, unit_id, 'unit', unit_id, 'transport_details', _actor(),
               {'company': company})
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@bp.route('/api/recon/lsl-company-search')
def api_lsl_company_search():
    """Carrier/dealer autocomplete over the LSL address book (read-only)."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'results': []})
    out = []
    try:
        c = _lsl_conn()
        try:
            rows = c.execute(
                "SELECT DISTINCT name FROM suppliers WHERE name LIKE ? AND name<>'' "
                "ORDER BY (name LIKE ?) DESC, length(name) LIMIT 12",
                ('%' + q + '%', q + '%')).fetchall()
            out = [r['name'] for r in rows]
        finally:
            c.close()
    except Exception as e:
        print('[recon-company-search] %s' % e, flush=True)
    return jsonify({'results': out})


# ============================================================================
# PHOTOS  (camera/library upload; pickup-proof photos AES-encrypted at rest)
# ============================================================================
@bp.route('/api/recon/<int:unit_id>/photo', methods=['POST'])
def api_photo_upload(unit_id):
    import uuid as _uuid
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'no photo'}), 400
    raw = f.read()
    if not raw:
        return jsonify({'error': 'empty file'}), 400
    is_pickup = (request.form.get('is_pickup') in ('1', 'true', 'True', 'on'))
    caption = (request.form.get('caption') or '').strip() or None
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT id FROM recon_units WHERE id=%s", (unit_id,))
        if not cur.fetchone():
            return jsonify({'error': 'unit not found'}), 404
        name = _uuid.uuid4().hex
        if is_pickup:
            fern = _fernet()
            if not fern:
                return jsonify({'error': 'encryption unavailable'}), 500
            d = os.path.join(RECON_MEDIA_DIR, str(unit_id))
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, name + '.enc')
            with open(path, 'wb') as out:
                out.write(fern.encrypt(raw))
            cur.execute("""INSERT INTO recon_photos
                  (unit_id, local_path, caption, uploaded_by, is_approval_media)
                  VALUES (%s,%s,%s,%s,TRUE) RETURNING id""",
                        (unit_id, path, caption or 'Pickup proof', _actor()))
            pid = cur.fetchone()['id']
            url = '/api/recon/photo/%d' % pid
        else:
            d = os.path.join(RECON_STATIC_DIR, str(unit_id))
            os.makedirs(d, exist_ok=True)
            ext = '.jpg'
            fn = (f.filename or '')
            if '.' in fn:
                e = fn.rsplit('.', 1)[1].lower()
                if e in ('jpg', 'jpeg', 'png', 'heic', 'webp'):
                    ext = '.' + e
            path = os.path.join(d, name + ext)
            with open(path, 'wb') as out:
                out.write(raw)
            url = '/static/uploads/recon/%d/%s%s' % (unit_id, name, ext)
            cur.execute("""INSERT INTO recon_photos
                  (unit_id, local_path, url, caption, uploaded_by, is_walkaround)
                  VALUES (%s,%s,%s,%s,%s,TRUE) RETURNING id""",
                        (unit_id, path, url, caption, _actor()))
            pid = cur.fetchone()['id']
        _audit(cur, unit_id, 'photo', pid, 'upload', _actor(),
               {'pickup': is_pickup, 'bytes': len(raw)})
        db.commit()
        return jsonify({'ok': True, 'photo_id': pid, 'url': url, 'pickup': is_pickup})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@bp.route('/api/recon/<int:unit_id>/photo/<int:photo_id>/delete', methods=['POST'])
def api_delete_photo(unit_id, photo_id):
    """Remove a photo row + its file on disk (plain or AES-encrypted)."""
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT local_path FROM recon_photos WHERE id=%s AND unit_id=%s",
                    (photo_id, unit_id))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'photo not found'}), 404
        cur.execute("DELETE FROM recon_photos WHERE id=%s AND unit_id=%s", (photo_id, unit_id))
        _audit(cur, unit_id, 'photo', photo_id, 'delete', _actor(), {})
        db.commit()
        try:
            lp = r['local_path']
            if lp and os.path.exists(lp):
                os.remove(lp)
        except Exception as e:
            print('[recon-photo] unlink failed: %s' % e, flush=True)
        return jsonify({'ok': True})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@bp.route('/api/recon/photo/<int:photo_id>')
def api_photo_serve(photo_id):
    """Login-gated serve (decrypts AES pickup-proof photos)."""
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT local_path, url, is_approval_media FROM recon_photos WHERE id=%s",
                    (photo_id,))
        r = cur.fetchone()
    finally:
        db.close()
    if not r:
        abort(404)
    r = dict(r)
    if r.get('is_approval_media') and (r.get('local_path') or '').endswith('.enc'):
        fern = _fernet()
        try:
            pt = fern.decrypt(open(r['local_path'], 'rb').read())
        except Exception:
            abort(404)
        from flask import Response
        return Response(pt, mimetype='image/jpeg',
                        headers={'Cache-Control': 'private, no-store'})
    if r.get('url'):
        from flask import redirect as _rd
        return _rd(r['url'])
    abort(404)


# ── manual "Send Austin an email" (transport request, user-triggered) ───────
@bp.route('/api/recon/<int:unit_id>/email-austin', methods=['POST'])
def api_email_austin(unit_id):
    data = request.get_json(silent=True) or request.form
    note = (data.get('note') or '').strip()
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT * FROM recon_units WHERE id=%s", (unit_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'unit not found'}), 404
        u = dict(row)
        ymm = ('%s %s %s' % (u.get('year') or '', u.get('make') or '',
                             u.get('model') or '')).strip()
        vin = u.get('vin')
        src = u.get('bought_from') or 'the seller'
        sold_to = u.get('sold_to')
        company = u.get('transport_company')
        pickup = _resolve_party(u, 'pickup')
        delivery = _resolve_party(u, 'delivery')
        if u.get('path') == 'to_home':
            deliv_name = HOME_BASE
        else:
            deliv_name = delivery.get('name') or sold_to or 'the buying dealer'

        def _blk(label, name, addr, phone, contact):
            s = '%s: %s' % (label, name or '—')
            if addr:
                s += '\n  ' + addr
            if phone:
                s += '\n  Phone: ' + phone
            if contact:
                s += '\n  Contact: ' + contact
            return s
        body = "Transport needed.\n\nVehicle: %s\nVIN: %s\n\n%s\n\n%s" % (
            ymm, vin,
            _blk('Pick up from', pickup.get('name') or src, pickup.get('address'),
                 pickup.get('phone'), pickup.get('contact')),
            _blk('Deliver to', deliv_name, delivery.get('address'),
                 delivery.get('phone'), delivery.get('contact')))
        if company:
            body += '\n\nTransport company: %s' % company
        if note:
            body += '\n\nNote: %s' % note
        _recon_email(cur, unit_id, 'austin_manual', EMAIL_RECIP['austin'],
                     'Transport needed: %s' % ymm, body, dedupe=False)
        _audit(cur, unit_id, 'email', None, 'austin_manual', _actor(), {'note': note[:200]})
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ── 9B photo scan: VIN + odometer (camera/library -> local 9B vision) ───────
def _scan_image(raw, prompt, mime):
    from app import gemini_call   # routes to the local 9B vision via the shim
    return gemini_call(prompt, image_bytes=raw, mime=(mime or 'image/jpeg'),
                       max_tokens=80, temperature=0.0, disable_thinking=True)


@bp.route('/api/recon/scan-vin', methods=['POST'])
def api_scan_vin():
    import re
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'no photo'}), 400
    raw = f.read()
    if len(raw) < 200:
        return jsonify({'error': 'image too small'}), 400
    try:
        res = _scan_image(raw,
            "Read the 17-character VIN from this photo of a car, its door-jamb "
            "sticker, or the dash. A VIN is exactly 17 characters of uppercase "
            "letters and digits and never uses the letters I, O or Q. Reply with "
            "ONLY the VIN, nothing else. If you cannot clearly read a 17-character "
            "VIN, reply NONE.", f.mimetype)
        txt = (res or '').strip().upper().replace(' ', '').replace('-', '')
        m = re.search(r'[A-HJ-NPR-Z0-9]{17}', txt)
        vin = m.group(0) if m else None
        if not vin:
            return jsonify({'ok': False, 'error': 'could not read a VIN — try a clearer photo'})
        return jsonify({'ok': True, 'vin': vin})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/recon/scan-miles', methods=['POST'])
def api_scan_miles():
    import re
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'no photo'}), 400
    raw = f.read()
    if len(raw) < 200:
        return jsonify({'error': 'image too small'}), 400
    try:
        res = _scan_image(raw,
            "Read the odometer mileage from this photo of a vehicle dashboard or "
            "odometer. Reply with ONLY the number of miles (digits only, no commas, "
            "no words, no units). If you cannot read it, reply NONE.", f.mimetype)
        txt = (res or '').strip().upper().replace(',', '').replace(' ', '')
        m = re.search(r'\d{1,7}', txt)
        if not m:
            return jsonify({'ok': False, 'error': 'could not read the odometer'})
        return jsonify({'ok': True, 'miles': int(m.group(0))})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
