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
# owners who can receive the recon-spend report by text (shared 754 Twilio number)
RECON_OWNERS = {'me': '+14074309675', 'joe': '+13522099696',
                'todd': '+15613018622', 'gregg': '+15166803500'}
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
    'arrived_home': '#3b82f6', 'recon': '#f97316', 'ready': '#8b5cf6', 'picked_up': '#9ca3af',
}

# Board pipeline laid out as ordered STAGES (arrows render between stages on the
# sidebar). The 4 staging statuses are ONE stage (parallel options) so no arrow
# sits between them. "Arrived at Dealer" is deliberately LAST — a home-base car
# is picked up from Home Base and only then arrives at the buying dealer.
RECON_FLOW = [
    ['all'],
    ['dealer_to_dealer', 'dealer_to_home', 'indiv_to_dealer', 'indiv_to_home'],
    ['in_transport'],
    ['arrived_home'],
    ['recon'],  # RECON_LANE_2026_06_26 — in-recon at home base
    ['ready'],
    ['picked_up'],
    ['arrived_dealer'],
]


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


def _now_et():
    """Current wall-clock time in US Eastern (handles EST/EDT) for human-facing
    timestamps. The server runs on UTC/Europe — never label UTC as 'ET'."""
    try:
        from zoneinfo import ZoneInfo
        return _utcnow().astimezone(ZoneInfo('America/New_York'))
    except Exception:
        return _utcnow() - timedelta(hours=4)  # EDT fallback if tzdata missing


def _fmt_et(dt):
    """Short human ET stamp for a tz-aware datetime, e.g. 'Jun 26, 3:14 PM'."""
    if not dt:
        return ''
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo('America/New_York'))
    except Exception:
        try:
            dt = dt - timedelta(hours=4)
        except Exception:
            pass
    try:
        return dt.strftime('%b %-d, %-I:%M %p')
    except Exception:
        return dt.strftime('%b %d')


def _normalize_shipping(note):
    """Turn a free-text pickup note into a SHORT, consistent dashboard label
    (e.g. 'Beaver Mazda pickup', 'Enterprise delivery') via the local 9B, so the
    board stays aligned regardless of how each person phrases it. Returns '' on
    any failure — callers fall back to the raw note."""
    note = (note or '').strip()
    if not note:
        return ''
    try:
        from app import gemini_call
        prompt = (
            "You normalize a car-shipping note into a SHORT dashboard label.\n"
            "Identify the company/dealer MOVING the vehicle and whether they "
            "PICK UP or DELIVER it. Output ONLY '<Company> pickup' or "
            "'<Company> delivery'. Company = 1-3 words, Title Case. No quotes, "
            "no other words.\n\n"
            'Note: "beaver creek picking up"\nLabel: Beaver Creek pickup\n\n'
            'Note: "Enterprise delivering, Beaver Mazda picking up"\nLabel: Beaver Mazda pickup\n\n'
            'Note: "we are shipping to Buford Chevy"\nLabel: Buford Chevy pickup\n\n'
            'Note: "Enterprise delivering to us"\nLabel: Enterprise delivery\n\n'
            'Note: "%s"\nLabel:' % note[:200])
        out = gemini_call(prompt, max_tokens=16, temperature=0.0, disable_thinking=True)
        if out:
            lab = out.strip().splitlines()[0].strip().strip('"').strip()
            if 0 < len(lab) <= 40:
                return lab
    except Exception as e:
        print('[recon] shipping normalize failed: %s' % e, flush=True)
    return ''


_SHIP_HINTS = ('ship', 'pick', 'deliver', 'transport', 'haul', 'carrier', 'tow', 'driver')


def _shipping_from_note(note):
    """If a plain note describes WHO is shipping / picking up / delivering the
    car, return a normalized '<Company> pickup/delivery' label; else ''. A cheap
    keyword pre-filter avoids a 9B call on the vast majority of notes."""
    note = (note or '').strip()
    if len(note) < 4:
        return ''
    low = note.lower()
    if not any(h in low for h in _SHIP_HINTS):
        return ''   # not shipping-ish — skip the model entirely
    try:
        from app import gemini_call
        prompt = (
            "Read a note about a vehicle. If it says WHO is shipping, picking up, "
            "or delivering the car, output a SHORT label '<Company> pickup' or "
            "'<Company> delivery' (Company = 1-3 words, Title Case). If the note "
            "is NOT about who is moving the car, output exactly NONE.\n\n"
            'Note: "We are shipping to Buford Chevy"\nLabel: Buford Chevy pickup\n\n'
            'Note: "Enterprise delivering to us"\nLabel: Enterprise delivery\n\n'
            'Note: "Marshall Goldman picking up"\nLabel: Marshall Goldman pickup\n\n'
            'Note: "call seller about title"\nLabel: NONE\n\n'
            'Note: "needs front bumper repaint"\nLabel: NONE\n\n'
            'Note: "%s"\nLabel:' % note[:200])
        out = gemini_call(prompt, max_tokens=16, temperature=0.0, disable_thinking=True)
        if out:
            lab = out.strip().splitlines()[0].strip().strip('"').strip()
            if lab and 'NONE' not in lab.upper() and len(lab) <= 40:
                return lab
    except Exception as e:
        print('[recon] shipping note-detect failed: %s' % e, flush=True)
    return ''


def _shipping_disp(u):
    """Display dict for the 'Shipping Arranged' column / badges.
    Shipping can be arranged via Austin (email) or manually (a company picking
    it up). Returns arranged flag + a short 'who' label + a hover tip."""
    ts = u.get('shipping_arranged_at')
    if not ts:
        return {'arranged': False, 'who': '', 'when': '',
                'tip': 'Shipping not arranged yet'}
    via = (u.get('shipping_arranged_via') or '').lower()
    note = (u.get('shipping_arranged_note') or '').strip()
    who_norm = (u.get('shipping_arranged_who') or '').strip()
    when = _fmt_et(ts)
    if via == 'austin':
        who = 'Austin emailed'
        tip = 'Shipping arranged via Austin (%s)' % when
        if note:
            tip += ' — ' + note
    else:
        who = who_norm or note or 'arranged'
        tip = 'Shipping arranged (%s)' % when
        if note:
            tip = 'Shipping arranged: %s (%s)' % (note, when)
    return {'arranged': True, 'who': who, 'when': when, 'tip': tip}


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


# ── LSL recon + transport cost per car (deal field OR supp-cost line) ────────
import re as _re
_RECON_RX = _re.compile(r'Recon\s*-\s*\$?([\d,]+\.?\d*)', _re.I)
_TRANS_RX = _re.compile(r'Transport\s*-\s*\$?([\d,]+\.?\d*)', _re.I)


def _money(s):
    try:
        return float(str(s).replace(',', '').replace('$', '')) if s not in (None, '') else 0.0
    except Exception:
        return 0.0


def _lsl_costs(stock_no, vin):
    """Recon + transport cost + attachment count for a car from its LSL deal.
    recon = recon_cost field, else the 'Recon - $X' supp-cost line; transport =
    transport_fee field, else the 'Transport - $X' supp-cost line."""
    out = {'recon': 0.0, 'transport': 0.0, 'attachments': 0, 'recon_note': '', 'found': False}
    if not (stock_no or vin):
        return out
    try:
        c = _lsl_conn()
        try:
            r = None
            if stock_no:
                r = c.execute("SELECT recon_cost, transport_fee, supp_costs_desc, raw_json "
                              "FROM deals WHERE stock_no=? ORDER BY created_at DESC LIMIT 1",
                              (stock_no,)).fetchone()
            if not r and vin:
                r = c.execute("SELECT recon_cost, transport_fee, supp_costs_desc, raw_json "
                              "FROM deals WHERE vin_no=? ORDER BY created_at DESC LIMIT 1",
                              (vin,)).fetchone()
            if r:
                out['found'] = True
                desc = r['supp_costs_desc'] or ''
                rm = _RECON_RX.search(desc)
                tm = _TRANS_RX.search(desc)
                out['recon'] = max(_money(r['recon_cost']), _money(rm.group(1)) if rm else 0.0)
                out['transport'] = max(_money(r['transport_fee']), _money(tm.group(1)) if tm else 0.0)
                try:
                    rj = json.loads(r['raw_json']) if r['raw_json'] else {}
                except Exception:
                    rj = {}
                out['attachments'] = int(rj.get('totalAttachments') or 0)
                sn = rj.get('lastSaleNote') or ''
                if 'recon' in sn.lower():   # LSL writes "SAVED RECON ADJUSTMENTS: …" here
                    out['recon_note'] = _re.sub(r'<br\s*/?>', ' ', sn).strip()
        finally:
            c.close()
    except Exception as e:
        print('[recon-costs] %s' % e, flush=True)
    return out


# ── recon/transport SPEND report (aggregated from LSL deals) ────────────────
def _fmt_money(x):
    try:
        return '{:,.0f}'.format(float(x or 0))
    except Exception:
        return '0'


def _send_sms(nums, body):
    sid = os.environ.get('TWILIO_ACCOUNT_SID')
    tok = os.environ.get('TWILIO_AUTH_TOKEN')
    frm = os.environ.get('TWILIO_PHONE')
    if not (sid and tok and frm):
        print('[recon-sms] twilio creds missing', flush=True)
        return 0
    n = 0
    try:
        from twilio.rest import Client
        cl = Client(sid, tok)
        for to in nums:
            try:
                cl.messages.create(to=to, from_=frm, body=body)
                n += 1
            except Exception as e:
                print('[recon-sms] %s -> %s' % (to, e), flush=True)
    except Exception as e:
        print('[recon-sms] %s' % e, flush=True)
    return n


def _recon_report(period, frm=None, to=None):
    """Aggregate recon + transport spend from LSL deals for a named period OR a
    custom from/to date range (EDT). recon = recon_cost field else 'Recon - $X'
    line; transport = transport_fee field else 'Transport - $X' line."""
    def _sd(s):
        s = (s or '').strip()
        return s if _re.match(r'^\d{4}-\d{2}-\d{2}$', s) else None
    lo0, hi0 = _sd(frm), _sd(to)
    if lo0 or hi0:
        lo, hi = (lo0 or '2000-01-01'), (hi0 or '2999-12-31')
        flt = "date(d.created_at,'-4 hours') BETWEEN '" + lo + "' AND '" + hi + "'"
        period = 'custom'
    else:
        period = period if period in ('week', 'month', 'year', 'all') else 'month'
        flt = {
            'week':  "datetime(d.created_at) >= datetime('now','-7 days')",
            'month': "strftime('%Y-%m', d.created_at, '-4 hours') = strftime('%Y-%m','now','-4 hours')",
            'year':  "strftime('%Y', d.created_at, '-4 hours') = strftime('%Y','now','-4 hours')",
            'all':   "1=1",
        }[period]
    rows = []
    try:
        c = _lsl_conn()
        try:
            rows = list(c.execute(
                "SELECT d.stock_no, d.make_name make, i.group_model_name model, "
                "d.recon_cost, d.transport_fee, d.supp_costs_desc, datetime(d.created_at,'-4 hours') edt "
                "FROM deals d LEFT JOIN inventory i ON i.stock_no=d.stock_no "
                "WHERE " + flt + " ORDER BY d.created_at DESC"))
        finally:
            c.close()
    except Exception as e:
        print('[recon-report] %s' % e, flush=True)
    tot_r = tot_t = 0.0
    by_make, by_model, cars = {}, {}, []
    for r in rows:
        desc = r['supp_costs_desc'] or ''
        rm = _RECON_RX.search(desc)
        tm = _TRANS_RX.search(desc)
        rc = max(_money(r['recon_cost']), _money(rm.group(1)) if rm else 0.0)
        tc = max(_money(r['transport_fee']), _money(tm.group(1)) if tm else 0.0)
        tot_r += rc
        tot_t += tc
        mk = (r['make'] or 'Unknown').strip() or 'Unknown'
        md = (r['model'] or mk).strip() or mk
        a = by_make.setdefault(mk, {'recon': 0.0, 'transport': 0.0, 'n': 0})
        a['recon'] += rc; a['transport'] += tc; a['n'] += 1
        b = by_model.setdefault(md, {'recon': 0.0, 'transport': 0.0, 'n': 0})
        b['recon'] += rc; b['transport'] += tc; b['n'] += 1
        if rc > 0 or tc > 0:
            cars.append({'stock_no': r['stock_no'], 'make': mk, 'model': md,
                         'recon': rc, 'transport': tc, 'edt': r['edt']})
    mk_list = sorted([dict(name=k, **v) for k, v in by_make.items()],
                     key=lambda x: -(x['recon'] + x['transport']))
    md_list = sorted([dict(name=k, **v) for k, v in by_model.items()],
                     key=lambda x: -(x['recon'] + x['transport']))
    n = len(rows)
    return {'period': period, 'from': lo0 or '', 'to': hi0 or '',
            'deals': n, 'total_recon': tot_r, 'total_transport': tot_t,
            'avg_recon': (tot_r / n if n else 0.0), 'avg_transport': (tot_t / n if n else 0.0),
            'by_make': mk_list[:30], 'by_model': md_list[:30], 'cars': cars[:300]}


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
        'arrived_home': [('ready', 'Mark Ready for Pickup')],
        'ready': [('picked_up', 'Mark Picked Up from Home Base')],
        'picked_up': [('arrived_dealer', 'Mark Arrived at Dealer')],
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
                             FROM recon_notes
                            WHERE unit_id = ANY(%s) AND COALESCE(category,'general')='general'
                            GROUP BY unit_id""",
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
        u['ship'] = _shipping_disp(u)
        ref = u.get('lsl_inventory_ref')
        u['lsl_url'] = (LSL_RECORD_URL % ref) if ref else \
            ('https://app.livesaleslog.com/inventory?query=%s' % (u.get('stock_no') or ''))

    counts = {}
    for u in units:
        c = u.get('step_code') or '?'
        counts[c] = counts.get(c, 0) + 1
    stepnum = {s['code']: i + 1 for i, s in enumerate(steps)}
    # Pipeline laid out as ordered STAGES with arrow connectors between them.
    # The 4 staging statuses are ONE stage (parallel options) — no arrow between them.
    by_code = {s['code']: s for s in steps}
    flow_groups = []
    for grp in RECON_FLOW:
        g = [by_code[c] for c in grp if c in by_code]
        if g:
            flow_groups.append(g)
    known = {c for grp in RECON_FLOW for c in grp}
    extras = [s for s in steps if s['code'] not in known]
    if extras:
        flow_groups.append(extras)
    rows = units if not sel else [u for u in units if u.get('step_code') == sel]
    # The "Austin Emailed" column shows on any specific step EXCEPT the default
    # "all vehicles" board and the very first "New" bucket (code 'all') — so the
    # 4 staging lanes + In Transit + Home Base + Ready + Picked Up + At Dealer
    # all surface it (those are where transport / Austin is actually in play).
    show_austin_col = bool(sel and sel != 'all')
    return render_template('recon/dashboard.html', steps=steps, rows=rows,
                           counts=counts, stepnum=stepnum, total=len(units),
                           sel=sel, show_austin_col=show_austin_col, now=now,
                           dot_colors=DOT_COLORS, flow_groups=flow_groups)


@bp.route('/recon/reports')
def reports():
    rep = _recon_report(request.args.get('period') or 'month',
                        request.args.get('from'), request.args.get('to'))
    return render_template('recon/reports.html', rep=rep, owners=list(RECON_OWNERS.keys()))


@bp.route('/api/recon/reports/text', methods=['POST'])
def api_report_text():
    data = request.get_json(silent=True) or request.form
    period = data.get('period') or 'month'
    who = (data.get('owner') or 'me').lower()
    rep = _recon_report(period, data.get('from'), data.get('to'))
    if rep['period'] == 'custom':
        plabel = '%s to %s' % (rep['from'] or 'start', rep['to'] or 'today')
    else:
        plabel = {'week': 'Last 7 Days', 'month': 'This Month', 'year': 'This Year',
                  'all': 'All Time'}.get(rep['period'], rep['period'])
    lines = ['EW Recon + Transport Spend — %s' % plabel, '',
             'Recon: $%s (avg $%s/veh)' % (_fmt_money(rep['total_recon']), _fmt_money(rep['avg_recon'])),
             'Transport: $%s (avg $%s/veh)' % (_fmt_money(rep['total_transport']), _fmt_money(rep['avg_transport'])),
             'Deals: %d' % rep['deals'], '', 'Top makes:']
    for m in rep['by_make'][:6]:
        lines.append('- %s: recon $%s / transport $%s (%d)' % (
            m['name'], _fmt_money(m['recon']), _fmt_money(m['transport']), m['n']))
    body = '\n'.join(lines)[:1500]
    nums = list(RECON_OWNERS.values()) if who == 'all' else [RECON_OWNERS.get(who, RECON_OWNERS['me'])]
    sent = _send_sms(nums, body)
    return jsonify({'ok': sent > 0, 'sent': sent, 'to': who})


# ── Austin's transport tab: pending pickup + in transit, with ETAs ──────────
TRANSPORT_PENDING = ('dealer_to_dealer', 'dealer_to_home', 'indiv_to_dealer', 'indiv_to_home')


def _transport_data():
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("""SELECT u.*, sd.code AS step_code, sd.name AS step_name
                         FROM recon_units u LEFT JOIN recon_step_defs sd ON sd.id=u.current_step_id
                        WHERE u.store_id=%s AND u.status IN ('in_transit_stage0','in_recon','on_hold')
                          AND sd.code IN %s
                        ORDER BY u.current_step_entered_at NULLS FIRST""",
                    (STORE_ID, TRANSPORT_PENDING + ('in_transport', 'arrived_home')))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        db.close()
    now = _utcnow()
    pending, transit = [], []
    for u in rows:
        d = _days(u.get('current_step_entered_at'), now)
        u['days'] = int(d) if d is not None else 0
        u['ship'] = _shipping_disp(u)
        code = u.get('step_code')
        has_dealer = bool((u.get('sold_to') or '').strip())
        been_home = bool(u.get('entered_recon_at'))   # has arrived Home Base at least once
        pre = 'indiv' if (u.get('buying_from_type') or '').lower() == 'individual' else 'dealer'
        u['leg2'] = False
        u['via_home'] = False                         # going to Home Base first, then a dealer
        u['final_dest'] = u.get('sold_to') or ''
        if code == 'arrived_home':
            # at Home Base — only a transport job if it's onward-bound to a dealer
            if not has_dealer:
                continue
            u['pickup_from'] = HOME_BASE
            u['dest'] = u.get('sold_to')
            u['arrive_to'] = 'arrived_dealer'
            u['leg2'] = True
            pending.append(u)
        elif code == 'in_transport':
            if been_home:                              # second leg: Home Base -> dealer
                u['pickup_from'] = HOME_BASE
                u['dest'] = u.get('sold_to') or 'dealer'
                u['arrive_to'] = 'arrived_dealer'
                u['leg2'] = True
                u['back_to'] = 'arrived_home'
            else:                                      # first leg
                to_home = (u.get('path') == 'to_home')
                u['pickup_from'] = u.get('bought_from') or 'seller'
                u['dest'] = HOME_BASE if to_home else (u.get('sold_to') or 'buyer')
                u['arrive_to'] = 'arrived_home' if to_home else 'arrived_dealer'
                u['via_home'] = to_home and has_dealer
                u['back_to'] = '%s_to_%s' % (pre, 'home' if to_home else 'dealer')
            transit.append(u)
        else:                                          # staging steps — first leg, pending pickup
            to_home = code in ('dealer_to_home', 'indiv_to_home')
            u['pickup_from'] = u.get('bought_from') or 'seller'
            u['dest'] = HOME_BASE if to_home else (u.get('sold_to') or 'buyer')
            u['arrive_to'] = 'arrived_home' if to_home else 'arrived_dealer'
            u['via_home'] = to_home and has_dealer
            pending.append(u)
    return pending, transit, now


def transport_sms_text(ident):
    """SMS reply for 'ship/transport/track <stock# or VIN>': a car's transport
    status + ETAs + from/to. Read-only; safe to call from the SMS webhook."""
    ident = (ident or '').strip()
    if not ident:
        return 'Text a stock # — e.g. "ship LL37042".'
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("""SELECT u.*, sd.code AS step_code, sd.name AS step_name
                         FROM recon_units u LEFT JOIN recon_step_defs sd ON sd.id=u.current_step_id
                        WHERE UPPER(u.stock_no)=UPPER(%s) OR UPPER(u.vin)=UPPER(%s)
                        ORDER BY u.id DESC LIMIT 1""", (ident, ident))
        r = cur.fetchone()
    finally:
        db.close()
    if not r:
        return 'No recon car found for "%s". Text the stock # like: ship LL37042' % ident
    u = dict(r)
    code = u.get('step_code')
    sold = (u.get('sold_to') or '').strip()
    been_home = bool(u.get('entered_recon_at'))
    if code in ('dealer_to_dealer', 'dealer_to_home', 'indiv_to_dealer', 'indiv_to_home'):
        state = 'Pending pickup'
    elif code == 'arrived_home':
        state = 'Pending pickup (Home Base → dealer)' if sold else 'At Home Base'
    else:
        state = {'in_transport': 'In transit', 'arrived_dealer': 'Delivered to dealer',
                 'ready': 'Ready', 'picked_up': 'Picked up from Home Base'}.get(
                     code, u.get('step_name') or 'New')
    if code in ('in_transport', 'arrived_home') and been_home and sold:
        frm, to = HOME_BASE, sold
    elif code in ('dealer_to_home', 'indiv_to_home') or \
            (code == 'in_transport' and u.get('path') == 'to_home' and not been_home):
        frm = u.get('bought_from') or 'seller'
        to = HOME_BASE + (' → ' + sold if sold else '')
    else:
        frm = u.get('bought_from') or 'seller'
        to = sold or 'buyer'
    ymm = ('%s %s %s' % (u.get('year') or '', u.get('make') or '', u.get('model') or '')).strip()
    out = ['%s  %s' % (u.get('stock_no') or '', ymm), 'Status: ' + state]
    if u.get('transport_company'):
        out.append('Carrier: ' + u['transport_company'])
    out.append('From: ' + frm)
    out.append('To: ' + to)
    out.append('Est pickup: %s' % (u.get('est_pickup_date') or 'TBD'))
    out.append('Est delivery: %s' % (u.get('est_delivery_date') or 'TBD'))
    return '\n'.join(out)


@bp.route('/recon/transport')
def transport_tab():
    pending, transit, now = _transport_data()
    return render_template('recon/transport.html', pending=pending, transit=transit,
                           today=now.strftime('%Y-%m-%d'),
                           recipients=_report_recipients())


def _transport_pdf():
    pending, transit, now = _transport_data()
    import io
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), topMargin=34, bottomMargin=34,
                            leftMargin=28, rightMargin=28)
    st = getSampleStyleSheet()
    el = [Paragraph('Experience Wholesale — Transport Status', st['Title']),
          Paragraph(_now_et().strftime('%b %d, %Y  %I:%M %p') + ' ET', st['Normal']), Spacer(1, 12)]

    def _d(x):
        return str(x) if x else '—'

    def section(title, items):
        el.append(Paragraph('%s (%d)' % (title, len(items)), st['Heading2']))
        data = [['Stock #', 'Vehicle', 'Carrier', 'Pick up → Deliver', 'Est. pickup', 'Est. delivery', 'Days']]
        for u in items:
            veh = ('%s %s %s' % (u.get('year') or '', u.get('make') or '', u.get('model') or '')).strip()
            route = '%s → %s' % (u.get('pickup_from') or '', u.get('dest') or '')
            data.append([u.get('stock_no') or '—', veh[:34], (u.get('transport_company') or '—')[:18],
                         route[:48], _d(u.get('est_pickup_date')), _d(u.get('est_delivery_date')), str(u.get('days'))])
        t = Table(data, repeatRows=1,
                  colWidths=[0.7*inch, 2.3*inch, 1.3*inch, 2.9*inch, 0.9*inch, 0.9*inch, 0.5*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#c0392b')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d8dde2')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f8fa')]),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ]))
        el.append(t)
        el.append(Spacer(1, 16))
    section('Pending Pickup', pending)
    section('In Transit', transit)
    doc.build(el)
    return buf.getvalue()


@bp.route('/recon/transport/report.pdf')
def transport_pdf():
    from flask import Response
    return Response(_transport_pdf(), mimetype='application/pdf',
                    headers={'Content-Disposition': 'inline; filename="ew-transport-status.pdf"'})


@bp.route('/api/recon/transport/email', methods=['POST'])
def api_transport_email():
    data = request.get_json(silent=True) or request.form
    raw = (data.get('to') or '').replace(';', ',')
    tos = [x.strip() for x in raw.split(',') if x.strip() and '@' in x]
    if not tos:
        return jsonify({'error': 'enter at least one email address'}), 400
    key = os.environ.get('RESEND_API_KEY', '')
    if not key:
        return jsonify({'error': 'email not configured'}), 500
    try:
        import base64
        pdf = _transport_pdf()
        import resend
        resend.api_key = key
        resend.Emails.send({
            'from': RECON_EMAIL_FROM, 'to': tos, 'reply_to': RECON_EMAIL_REPLY_TO,
            'subject': 'EW Transport Status — %s' % _now_et().strftime('%b %d, %Y'),
            'html': '<p>Attached: the current Experience Wholesale transport status '
                    '(cars pending pickup and in transit, with ETAs).</p>',
            'attachments': [{'filename': 'ew-transport-status.pdf',
                             'content': base64.b64encode(pdf).decode()}],
        })
        return jsonify({'ok': True, 'sent': len(tos)})
    except Exception as e:
        print('[transport-email] %s' % e, flush=True)
        return jsonify({'error': str(e)}), 500


def _report_recipients():
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT id, email FROM recon_report_recipients WHERE active ORDER BY lower(email)")
        return [dict(r) for r in cur.fetchall()]
    finally:
        db.close()


@bp.route('/api/recon/transport/recipients/add', methods=['POST'])
def api_recipient_add():
    data = request.get_json(silent=True) or request.form
    email = (data.get('email') or '').strip()
    if '@' not in email or len(email) < 5:
        return jsonify({'error': 'enter a valid email'}), 400
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("INSERT INTO recon_report_recipients (email) VALUES (%s) "
                    "ON CONFLICT (email) DO UPDATE SET active=TRUE RETURNING id, email", (email,))
        r = cur.fetchone()
        db.commit()
        return jsonify({'ok': True, 'id': r['id'], 'email': r['email']})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@bp.route('/api/recon/transport/recipients/delete', methods=['POST'])
def api_recipient_delete():
    data = request.get_json(silent=True) or request.form
    rid = data.get('id')
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM recon_report_recipients WHERE id=%s", (rid,))
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@bp.route('/recon/out-for-recon')
def out_for_recon_tab():
    # RECON_GARAGE_FILTER_2026_06_26: filter the out-for-recon list by shop/garage
    garage = (request.args.get('garage') or '').strip()
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("""SELECT out_for_recon_to AS g, count(*) AS c FROM recon_units
                        WHERE store_id=%s AND out_for_recon_at IS NOT NULL
                          AND COALESCE(out_for_recon_to,'') <> ''
                        GROUP BY out_for_recon_to ORDER BY lower(out_for_recon_to)""", (STORE_ID,))
        garages = [dict(r) for r in cur.fetchall()]
        if garage:
            cur.execute("""SELECT u.*, sd.name AS step_name FROM recon_units u
                             LEFT JOIN recon_step_defs sd ON sd.id=u.current_step_id
                            WHERE u.store_id=%s AND u.out_for_recon_at IS NOT NULL
                              AND lower(COALESCE(u.out_for_recon_to,''))=lower(%s)
                            ORDER BY u.out_for_recon_at""", (STORE_ID, garage))
        else:
            cur.execute("""SELECT u.*, sd.name AS step_name FROM recon_units u
                             LEFT JOIN recon_step_defs sd ON sd.id=u.current_step_id
                            WHERE u.store_id=%s AND u.out_for_recon_at IS NOT NULL
                            ORDER BY u.out_for_recon_at""", (STORE_ID,))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        db.close()
    now = _utcnow()
    for u in rows:
        u['out_days'] = round(_days(u.get('out_for_recon_at'), now) or 0, 1)
    return render_template('recon/out_for_recon.html', rows=rows, now=now,
                           garages=garages, sel_garage=garage)


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
        cur.execute("SELECT * FROM recon_notes WHERE unit_id=%s AND COALESCE(category,'general')='general' "
                    "ORDER BY created_at DESC", (u['id'],))
        notes = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM recon_notes WHERE unit_id=%s AND category='recon' "
                    "ORDER BY created_at DESC", (u['id'],))
        rnotes = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM recon_photos WHERE unit_id=%s ORDER BY created_at DESC", (u['id'],))
        photos = [dict(r) for r in cur.fetchall()]
        # history of "Email Austin" sends (timestamp + the note that went with it)
        cur.execute("""SELECT created_at, detail FROM recon_audit
                        WHERE unit_id=%s AND action='austin_manual'
                        ORDER BY created_at DESC""", (u['id'],))
        austin_emails = []
        for r in cur.fetchall():
            d = r.get('detail') or {}
            if isinstance(d, str):
                try:
                    d = json.loads(d)
                except Exception:
                    d = {}
            austin_emails.append({'when': _fmt_et(r.get('created_at')),
                                  'note': (d.get('note') or '').strip()})
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
    u['ship'] = _shipping_disp(u)
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
    costs = _lsl_costs(u.get('stock_no'), u.get('vin'))
    out_days = round(_days(u.get('out_for_recon_at'), now) or 0, 1) if u.get('out_for_recon_at') else None
    return render_template('recon/unit.html', u=u, events=events,
                           workitems=workitems, notes=notes, rnotes=rnotes, photos=photos,
                           steps=steps, now=now, cur_code=cur_code, cur_name=cur_name,
                           next_opts=next_opts, companies=companies, pickup=pickup, delivery=delivery,
                           costs=costs, out_days=out_days, austin_emails=austin_emails,
                           dot=DOT_COLORS.get(cur_code, '#cbd5e1'))


# ============================================================================
# MANUAL UNIT ENTRY (Phase 1 — operator seeds the board by hand)
# ============================================================================
@bp.route('/api/recon/search')
def api_recon_search():
    """RECON_SEARCH_ALL_2026_06_26: find a unit by VIN/stock/make/model/year across
    ALL statuses (incl. sold/removed/off-board), not just the open board rows."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'ok': True, 'rows': []})
    like = '%' + q.lower() + '%'
    db = _db(); cur = db.cursor()
    try:
        cur.execute("SELECT u.id, u.vin, u.stock_no, u.year, u.make, u.model, u.exterior_color, "
                    "u.status, sd.code AS step_code, COALESCE(sd.name,'') AS step_name "
                    "FROM recon_units u LEFT JOIN recon_step_defs sd ON sd.id=u.current_step_id "
                    "WHERE u.store_id=%s AND ("
                    "lower(u.vin) LIKE %s OR lower(COALESCE(u.stock_no,'')) LIKE %s "
                    "OR lower(COALESCE(u.make,'')) LIKE %s OR lower(COALESCE(u.model,'')) LIKE %s "
                    "OR CAST(u.year AS text) LIKE %s) "
                    "ORDER BY u.updated_at DESC NULLS LAST LIMIT 80",
                    (STORE_ID, like, like, like, like, like))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        db.close()
    for r in rows:
        r['dot'] = DOT_COLORS.get(r.get('step_code'), '#cbd5e1')
    return jsonify({'ok': True, 'rows': rows})


@bp.route('/api/recon/lsl-stock-lookup')
def api_lsl_stock_lookup():
    """RECON_STOCK_LOOKUP_2026_06_26: type a stock # -> pull that vehicle from LSL
    (read-only inventory) so the Add modal can auto-fill. Mirrors the field
    mapping the LSL sync uses (HR6-safe: LSL opened read-only)."""
    sn = (request.args.get('stock') or '').strip()
    if not sn:
        return jsonify({'ok': False, 'error': 'stock number required'}), 400
    _EXOTIC = {'maserati', 'bentley', 'ferrari', 'lamborghini', 'rolls-royce',
               'aston martin', 'mclaren', 'bugatti'}
    try:
        conn = _lsl_conn()
        row = conn.execute(
            "SELECT id, stock_no, vin_no, group_model_trim_year, vehicle_make_name, "
            "group_model_name, exterior_color, usage, purchase_cost, source, "
            "purchased_from_type, datetime(created_at) AS cdt "
            "FROM inventory WHERE stock_no = ? COLLATE NOCASE "
            "ORDER BY created_at DESC LIMIT 1", (sn,)).fetchone()
        conn.close()
    except Exception as e:
        return jsonify({'ok': False, 'error': 'LSL lookup failed: %s' % e}), 200
    if not row:
        return jsonify({'ok': False, 'error': 'No vehicle in LSL for stock #%s' % sn}), 200
    mk = row['vehicle_make_name']
    md = (row['group_model_name'] or '').replace(mk or '', '').strip() or row['group_model_name']
    ym = _re.search(r'(19|20)\d\d', row['group_model_trim_year'] or '')
    year = int(ym.group(0)) if ym else None
    bft = 'Individual' if (row['purchased_from_type'] or '').lower() == 'individual' else 'Dealer'
    cls = 'exotic' if (mk or '').lower() in _EXOTIC else 'highline'
    vin = (row['vin_no'] or '').strip().upper()
    dup = None
    try:
        rdb = _db(); rc = rdb.cursor()
        rc.execute("SELECT u.id, u.status, COALESCE(s.name,'') AS step_name "
                   "FROM recon_units u LEFT JOIN recon_step_defs s ON s.id=u.current_step_id "
                   "WHERE u.vin=%s AND u.status IN "
                   "('in_transit_stage0','in_recon','frontline_ready','on_hold') "
                   "ORDER BY u.id DESC LIMIT 1", (vin,))
        rr2 = rc.fetchone()
        rdb.close()
        if rr2:
            dup = {'unit_id': rr2['id'], 'status': rr2['status'], 'step_name': rr2['step_name']}
    except Exception:
        dup = None
    return jsonify({
        'ok': True, 'vin': vin, 'stock_no': row['stock_no'], 'year': year, 'already_in_recon': dup,
        'make': mk, 'model': md, 'trim': None,
        'exterior_color': row['exterior_color'], 'miles': row['usage'],
        'purchase_cost': row['purchase_cost'], 'vehicle_class': cls,
        'bought_from': row['source'], 'buying_from_type': bft,
        'lsl_inventory_ref': row['id'], 'acquired_at': row['cdt'],
        'label': ('%s %s %s' % (year or '', mk or '', md or '')).strip(),
    })


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
    # Carried over when the row was pulled from LSL by stock # so stock-added
    # cars get the same LSL deep-link + transport auto-fill as synced ones.
    exterior_color = (data.get('exterior_color') or '').strip() or None
    lsl_inventory_ref = _i(data.get('lsl_inventory_ref'))
    bought_from = (data.get('bought_from') or '').strip() or None
    buying_from_type = (data.get('buying_from_type') or '').strip() or None
    acquired_at = (data.get('acquired_at') or '').strip() or None

    db = _db()
    cur = db.cursor()
    try:
        # ADD_UNIT_INTO_ALL_2026_06_26: manual/stock adds land in the live "New"
        # lane (code='all'), NOT the inactive off-board 'intake' step.
        cur.execute("SELECT id, sla_hours, sla_hours_exotic FROM recon_step_defs "
                    "WHERE store_id=%s AND code='all'", (STORE_ID,))
        st = cur.fetchone()
        if not st:
            return jsonify({'error': 'New step missing — run the migration'}), 500
        intake_id = st['id']
        now = _utcnow()
        sla = st['sla_hours_exotic'] if is_exotic else st['sla_hours']
        sla_due = now + timedelta(hours=float(sla)) if sla is not None else None
        cur.execute("""
            INSERT INTO recon_units
                (vin, stock_no, year, make, model, trim, miles, purchase_cost,
                 vehicle_class, is_exotic, exterior_color, lsl_inventory_ref,
                 bought_from, buying_from_type, acquired_at,
                 entered_recon_at, current_step_id, current_step_entered_at,
                 status, source, recon_token)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,COALESCE(%s::timestamptz, %s),
                    %s,%s,%s,
                    'in_recon','manual',%s)
            RETURNING id
        """, (vin, stock_no, year, make, model, trim, miles, purchase_cost,
              vclass, is_exotic, exterior_color, lsl_inventory_ref,
              bought_from, buying_from_type, acquired_at, now,
              now, intake_id, now,
              secrets.token_urlsafe(16)))
        unit_id = cur.fetchone()['id']
        cur.execute("INSERT INTO recon_seen (vin) VALUES (%s) ON CONFLICT (vin) DO NOTHING", (vin,))
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
    category = (data.get('category') or 'general').strip().lower()
    if category not in ('general', 'recon'):
        category = 'general'
    if not body:
        return jsonify({'error': 'note is empty'}), 400
    if len(body) > 4000:
        body = body[:4000]
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT current_step_id, shipping_arranged_at "
                    "FROM recon_units WHERE id=%s", (unit_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'unit not found'}), 404
        cur.execute("""INSERT INTO recon_notes (unit_id, step_id, author, body, category)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (unit_id, row['current_step_id'], _actor(), body, category))
        nid = cur.fetchone()['id']
        _audit(cur, unit_id, 'note', nid, 'add', _actor(), {'len': len(body)})
        # auto-detect a shipping arrangement from a plain note (operator opt-in).
        # Only fills in cars not already arranged — never overrides Austin/manual.
        auto_ship = None
        if category == 'general' and not row.get('shipping_arranged_at'):
            label = _shipping_from_note(body)
            if label:
                cur.execute("""UPDATE recon_units
                                  SET shipping_arranged_at=now(), shipping_arranged_via='note',
                                      shipping_arranged_note=%s, shipping_arranged_who=%s,
                                      updated_at=now()
                                WHERE id=%s""", (body[:500], label, unit_id))
                _audit(cur, unit_id, 'shipping', None, 'auto_from_note', _actor(), {'who': label})
                auto_ship = label
        db.commit()
        return jsonify({'ok': True, 'note_id': nid, 'shipping': auto_ship})
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


@bp.route('/api/recon/<int:unit_id>/set-eta', methods=['POST'])
def api_set_eta(unit_id):
    """Update just one ETA date (from the transport tab) without touching the
    rest of the transport details."""
    data = request.get_json(silent=True) or request.form
    col = {'pickup': 'est_pickup_date', 'delivery': 'est_delivery_date'}.get(data.get('field'))
    if not col:
        return jsonify({'error': 'bad field'}), 400
    val = (data.get('date') or '').strip() or None
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE recon_units SET " + col + "=%s, updated_at=now() "
                    "WHERE id=%s RETURNING id", (val, unit_id))
        if not cur.fetchone():
            return jsonify({'error': 'unit not found'}), 404
        _audit(cur, unit_id, 'unit', unit_id, 'set_eta', _actor(), {'field': data.get('field'), 'date': val})
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@bp.route('/api/recon/<int:unit_id>/recon-out', methods=['POST'])
def api_recon_out(unit_id):
    """Ship a car OUT for recon work (starts the out-for-recon timer) or mark it
    returned. out_for_recon_at is the timer start; COALESCE keeps it if already set."""
    data = request.get_json(silent=True) or request.form
    out = bool(data.get('out'))
    vendor = (data.get('vendor') or '').strip() or None
    db = _db()
    cur = db.cursor()
    try:
        if out:
            cur.execute("UPDATE recon_units SET out_for_recon_at=COALESCE(out_for_recon_at, now()), "
                        "out_for_recon_to=%s, updated_at=now() WHERE id=%s RETURNING id", (vendor, unit_id))
        else:
            cur.execute("UPDATE recon_units SET out_for_recon_at=NULL, out_for_recon_to=NULL, "
                        "updated_at=now() WHERE id=%s RETURNING id", (unit_id,))
        if not cur.fetchone():
            return jsonify({'error': 'unit not found'}), 404
        _audit(cur, unit_id, 'unit', unit_id, 'recon_out' if out else 'recon_back', _actor(), {'vendor': vendor})
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
        # stamp the unit: Austin emailed -> shipping is arranged (via Austin)
        cur.execute("""UPDATE recon_units
                          SET austin_emailed_at=now(), updated_at=now(),
                              shipping_arranged_at=now(), shipping_arranged_via='austin',
                              shipping_arranged_note=%s
                        WHERE id=%s RETURNING austin_emailed_at""",
                    (note[:500] or None, unit_id))
        stamped = cur.fetchone()
        ts = stamped['austin_emailed_at'] if stamped else None
        # leave a visible note in the transport log so there's a record on the car
        nbody = '📧 Emailed Austin to arrange transport.'
        if note:
            nbody += ' Note: ' + note
        cur.execute("INSERT INTO recon_notes (unit_id, step_id, author, body, category) "
                    "VALUES (%s,%s,%s,%s,'general')",
                    (unit_id, u.get('current_step_id'), _actor(), nbody))
        db.commit()
        return jsonify({'ok': True, 'emailed_at': _fmt_et(ts)})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ── manual "someone else is picking it up" — arrange shipping without Austin ──
@bp.route('/api/recon/<int:unit_id>/arrange-pickup', methods=['POST'])
def api_arrange_pickup(unit_id):
    data = request.get_json(silent=True) or request.form
    note = (data.get('note') or '').strip()
    if not note:
        return jsonify({'error': 'add a note — who is picking it up?'}), 400
    note = note[:500]
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT current_step_id FROM recon_units WHERE id=%s", (unit_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'unit not found'}), 404
        who = _normalize_shipping(note)   # 9B -> consistent "<Company> pickup" label
        cur.execute("""UPDATE recon_units
                          SET shipping_arranged_at=now(), shipping_arranged_via='manual',
                              shipping_arranged_note=%s, shipping_arranged_who=%s, updated_at=now()
                        WHERE id=%s""", (note, who or None, unit_id))
        cur.execute("INSERT INTO recon_notes (unit_id, step_id, author, body, category) "
                    "VALUES (%s,%s,%s,%s,'general')",
                    (unit_id, row['current_step_id'], _actor(),
                     '🚚 Shipping arranged: ' + note))
        _audit(cur, unit_id, 'shipping', None, 'arrange_manual', _actor(), {'note': note[:200]})
        db.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ── clear the shipping-arranged status (e.g. plans changed) ─────────────────
@bp.route('/api/recon/<int:unit_id>/arrange-pickup/clear', methods=['POST'])
def api_arrange_pickup_clear(unit_id):
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("""UPDATE recon_units
                          SET shipping_arranged_at=NULL, shipping_arranged_via=NULL,
                              shipping_arranged_note=NULL, shipping_arranged_who=NULL,
                              austin_emailed_at=NULL, updated_at=now()
                        WHERE id=%s""", (unit_id,))
        _audit(cur, unit_id, 'shipping', None, 'clear', _actor(), {})
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


# ============================================================================
# RECON_BOL_INBOX_2026_06_26 — inbound Bill-of-Lading email pipeline.
# Trucking co emails recon@experience-wholesale.net -> Cloudflare Email Routing
# -> tiny Worker POSTs the raw email here -> the 9B reads each doc -> VIN/stock#
# matched to a recon car -> doc attached + note, else queued as 'needs_match'.
# All LSL/9B usage is read-only/vision; nothing blocks enrichment.
# ============================================================================
from flask import send_file
import re as _bre, json as _bjson, uuid as _buuid, os as _bos, subprocess as _bsub, tempfile as _btemp, glob as _bglob, email as _bemail
from email import policy as _bpolicy

_BOL_DIR = _bos.path.join(RECON_MEDIA_DIR, 'bol')
_VINRE = _bre.compile(r'^[A-HJ-NPR-Z0-9]{17}$')
_BOL_OPEN_ORDER = "(status NOT IN ('removed','sold')) DESC, id DESC"


def _bol_extract(file_bytes, content_type):
    """9B reads a BOL doc (image or PDF) -> {vins, stocks, label}."""
    from app import gemini_call
    prompt = (
        "This image is a vehicle transport document (Bill of Lading, dispatch / load "
        "sheet, gate pass, or shipping confirmation). For EVERY vehicle listed, extract "
        "the 17-character VIN (only A-Z and 0-9; the letters I, O, Q never appear in a "
        "VIN), any stock number (often like LL##### or a short code), and the "
        "year/make/model. Reply with ONLY compact JSON and nothing else: "
        '{"vins":["..."],"stocks":["..."],"label":"<first vehicle year make model>"}. '
        'If there are no vehicles, reply {"vins":[],"stocks":[],"label":""}.'
    )
    imgs = []
    ct = (content_type or '').lower()
    if 'pdf' in ct or file_bytes[:5] == b'%PDF-':
        with _btemp.TemporaryDirectory() as td:
            pin = _bos.path.join(td, 'in.pdf')
            with open(pin, 'wb') as f:
                f.write(file_bytes)
            try:
                _bsub.run(['pdftoppm', '-png', '-r', '150', '-l', '5', pin, _bos.path.join(td, 'pg')],
                          check=True, timeout=90)
                for p in sorted(_bglob.glob(_bos.path.join(td, 'pg*.png')))[:5]:
                    imgs.append((open(p, 'rb').read(), 'image/png'))
            except Exception as e:
                print('[bol] pdftoppm failed: %s' % e, flush=True)
    elif 'image' in ct:
        imgs.append((file_bytes, ct if '/' in ct else 'image/jpeg'))
    vins, stocks, label = set(), set(), ''
    for (data, mime) in imgs:
        try:
            out = gemini_call(prompt, image_bytes=data, mime=mime, model='gemini-2.5-flash',
                              max_tokens=900, temperature=0, img_max_dim=2400, img_quality=90)
        except Exception as e:
            print('[bol] 9B error: %s' % e, flush=True)
            continue
        if not out:
            continue
        m = _bre.search(r'\{.*\}', out, _bre.S)
        if not m:
            continue
        try:
            d = _bjson.loads(m.group(0))
        except Exception:
            continue
        for v in (d.get('vins') or []):
            v = str(v).strip().upper().replace(' ', '')
            if _VINRE.match(v):
                vins.add(v)
        for s in (d.get('stocks') or []):
            s = str(s).strip().upper()
            if s and len(s) <= 16:
                stocks.add(s)
        if not label and d.get('label'):
            label = str(d['label']).strip()
    return {'vins': sorted(vins), 'stocks': sorted(stocks), 'label': label}


def _bol_match_unit(cur, vins, stocks):
    for v in vins:
        cur.execute("SELECT id, stock_no, year, make, model, current_step_id FROM recon_units "
                    "WHERE vin=%s ORDER BY " + _BOL_OPEN_ORDER + " LIMIT 1", (v,))
        u = cur.fetchone()
        if u:
            return u
    for s in stocks:
        cur.execute("SELECT id, stock_no, year, make, model, current_step_id FROM recon_units "
                    "WHERE UPPER(stock_no)=%s ORDER BY " + _BOL_OPEN_ORDER + " LIMIT 1", (s,))
        u = cur.fetchone()
        if u:
            return u
    return None


def _bol_attach(cur, unit, bol_id, subject, saved):
    for i, s in enumerate(saved):
        url = '/api/recon/bol/%d/doc/%d' % (bol_id, i)
        cap = ('BOL: ' + (subject[:70] or 'document')) if 'pdf' not in (s.get('content_type') or '') \
              else ('BOL (PDF): ' + (subject[:60] or 'document'))
        cur.execute("INSERT INTO recon_photos (unit_id, local_path, url, caption, uploaded_by) "
                    "VALUES (%s,%s,%s,%s,'email_bol')", (unit['id'], s.get('path'), url, cap))


@bp.route('/api/recon/inbound-email', methods=['POST'])
def api_inbound_email():
    """Inbound BOL email from the Cloudflare Email Worker. Auth: X-Recon-Inbound."""
    secret = (_bos.environ.get('EW_RECON_INBOUND_SECRET') or '').strip()
    if not secret or (request.headers.get('X-Recon-Inbound') or '').strip() != secret:
        return jsonify({'error': 'bad auth'}), 401
    raw = request.get_data() or b''
    try:
        msg = _bemail.message_from_bytes(raw, policy=_bpolicy.default)
    except Exception:
        msg = None
    hfrom = (request.headers.get('X-Mail-From') or (msg.get('From') if msg else '') or '').strip()
    hto = (request.headers.get('X-Mail-To') or (msg.get('To') if msg else '') or '').strip()
    subject = ((msg.get('Subject') if msg else '') or request.headers.get('X-Mail-Subject') or '').strip()
    body_text, atts = '', []
    if msg:
        for part in msg.walk():
            ctype = (part.get_content_type() or '').lower()
            disp = part.get_content_disposition()
            if ctype == 'text/plain' and disp != 'attachment':
                try:
                    body_text += (part.get_content() or '')
                except Exception:
                    pass
            elif disp == 'attachment' or ctype in ('application/pdf', 'image/jpeg', 'image/jpg', 'image/png'):
                try:
                    payload = part.get_payload(decode=True)
                    if payload and len(payload) < 25 * 1024 * 1024:
                        atts.append((part.get_filename() or ('doc.' + ctype.split('/')[-1]), ctype, payload))
                except Exception:
                    pass
    # persist raw + attachments
    bdir = _bos.path.join(_BOL_DIR, _buuid.uuid4().hex)
    _bos.makedirs(bdir, exist_ok=True)
    try:
        with open(_bos.path.join(bdir, 'raw.eml'), 'wb') as f:
            f.write(raw)
    except Exception:
        pass
    saved = []
    for i, (fn, ct, data) in enumerate(atts):
        ext = '.pdf' if 'pdf' in ct else ('.' + (ct.split('/')[-1] if '/' in ct else 'bin'))
        sp = _bos.path.join(bdir, 'doc%d%s' % (i, ext))
        try:
            with open(sp, 'wb') as f:
                f.write(data)
            saved.append({'filename': fn, 'path': sp, 'content_type': ct})
        except Exception:
            pass
    # VIN/stock from body (regex) + each attachment (9B)
    vins, stocks, label = set(), set(), ''
    for m in _bre.findall(r'[A-HJ-NPR-Z0-9]{17}', (body_text or '').upper()):
        if _VINRE.match(m):
            vins.add(m)
    for m in _bre.findall(r'\bLL\d{3,6}\b', (body_text or '').upper()):
        stocks.add(m)
    for (fn, ct, data) in atts:
        ex = _bol_extract(data, ct)
        vins.update(ex['vins'])
        stocks.update(ex['stocks'])
        if not label and ex['label']:
            label = ex['label']
    db = _db(); cur = db.cursor()
    try:
        unit = _bol_match_unit(cur, sorted(vins), sorted(stocks))
        status = 'matched' if unit else ('needs_match' if (vins or stocks) else 'no_vin')
        cur.execute("""INSERT INTO recon_inbound_bol
              (from_addr, to_addr, subject, status, matched_unit_id, extracted_vin,
               extracted_stock, extracted_label, body_text, attach_count, raw_path, attachments)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (hfrom[:300], hto[:300], subject[:500], status, (unit['id'] if unit else None),
                     (sorted(vins)[0] if vins else None), (sorted(stocks)[0] if stocks else None),
                     label[:200], (body_text or '')[:4000], len(saved),
                     _bos.path.join(bdir, 'raw.eml'), _bjson.dumps(saved)))
        bol_id = cur.fetchone()['id']
        if unit:
            _bol_attach(cur, unit, bol_id, subject, saved)
            note = '\U0001f4c4 BOL received from %s%s — auto-matched & attached' % (
                hfrom or 'carrier', (' (%s)' % subject[:60]) if subject else '')
            cur.execute("INSERT INTO recon_notes (unit_id, step_id, author, body, category) "
                        "VALUES (%s,%s,'email_bol',%s,'general')", (unit['id'], unit['current_step_id'], note))
            try:
                cur.execute("UPDATE recon_units SET has_bol=TRUE WHERE id=%s", (unit['id'],))
            except Exception:
                pass
        db.commit()
    finally:
        db.close()
    return jsonify({'ok': True, 'bol_id': bol_id, 'matched': bool(unit),
                    'unit_id': (unit['id'] if unit else None), 'status': status,
                    'vins': sorted(vins), 'stocks': sorted(stocks)})


@bp.route('/recon/bol-inbox')
def bol_inbox():
    db = _db(); cur = db.cursor()
    try:
        cur.execute("""SELECT b.*, u.stock_no AS u_stock, u.vin AS u_vin, u.make AS u_make,
                              u.model AS u_model, u.year AS u_year
                         FROM recon_inbound_bol b LEFT JOIN recon_units u ON u.id=b.matched_unit_id
                        ORDER BY b.created_at DESC LIMIT 300""")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        db.close()
    for r in rows:
        a = r.get('attachments')
        if isinstance(a, str):
            try:
                a = _bjson.loads(a or '[]')
            except Exception:
                a = []
        r['attachments'] = a or []
    nmatch = sum(1 for r in rows if r['status'] == 'needs_match')
    return render_template('recon/bol_inbox.html', rows=rows, nmatch=nmatch)


@bp.route('/api/recon/bol/<int:bol_id>/assign', methods=['POST'])
def api_bol_assign(bol_id):
    data = request.get_json(silent=True) or request.form
    key = (data.get('stock_or_vin') or '').strip().upper()
    if not key:
        return jsonify({'error': 'enter a stock # or VIN'}), 400
    db = _db(); cur = db.cursor()
    try:
        cur.execute("SELECT id, subject, from_addr, attachments FROM recon_inbound_bol WHERE id=%s", (bol_id,))
        bol = cur.fetchone()
        if not bol:
            return jsonify({'error': 'BOL not found'}), 404
        cur.execute("SELECT id, current_step_id FROM recon_units WHERE vin=%s OR UPPER(stock_no)=%s "
                    "ORDER BY " + _BOL_OPEN_ORDER + " LIMIT 1", (key, key))
        unit = cur.fetchone()
        if not unit:
            return jsonify({'ok': False, 'error': 'no recon car matches %s' % key}), 200
        atts = bol['attachments'] if isinstance(bol['attachments'], list) else _bjson.loads(bol['attachments'] or '[]')
        _bol_attach(cur, unit, bol_id, bol['subject'] or '', atts)
        cur.execute("INSERT INTO recon_notes (unit_id, step_id, author, body, category) "
                    "VALUES (%s,%s,'email_bol',%s,'general')",
                    (unit['id'], unit['current_step_id'], '\U0001f4c4 BOL from %s manually assigned' % (bol['from_addr'] or 'carrier')))
        cur.execute("UPDATE recon_inbound_bol SET status='matched', matched_unit_id=%s WHERE id=%s", (unit['id'], bol_id))
        try:
            cur.execute("UPDATE recon_units SET has_bol=TRUE WHERE id=%s", (unit['id'],))
        except Exception:
            pass
        db.commit()
    finally:
        db.close()
    return jsonify({'ok': True, 'unit_id': unit['id']})


@bp.route('/api/recon/bol/<int:bol_id>/doc/<int:n>')
def api_bol_doc(bol_id, n):
    db = _db(); cur = db.cursor()
    try:
        cur.execute("SELECT attachments FROM recon_inbound_bol WHERE id=%s", (bol_id,))
        r = cur.fetchone()
    finally:
        db.close()
    if not r:
        abort(404)
    atts = r['attachments'] if isinstance(r['attachments'], list) else _bjson.loads(r['attachments'] or '[]')
    if n < 0 or n >= len(atts):
        abort(404)
    s = atts[n]
    p, ct = s.get('path'), (s.get('content_type') or 'application/octet-stream')
    if not p or not _bos.path.exists(p):
        abort(404)
    return send_file(p, mimetype=ct, download_name=(s.get('filename') or ('doc%d' % n)))


# ============================================================================
# RECON_CHECKIN_2026_06_26 — mobile scan-to-check-in. Pick a status, scan the
# VIN (9B vision, from anywhere on the car), and the car auto-moves to that
# status. The page then calls the canonical /api/recon/<id>/move so all the
# move side-effects (timers, emails, push) fire exactly as a manual move.
# ============================================================================
@bp.route('/recon/checkin')
def recon_checkin_page():
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT code, name FROM recon_step_defs WHERE store_id=%s AND active=true "
                    "ORDER BY sort_order", (STORE_ID,))
        steps = [dict(r) for r in cur.fetchall()]
    finally:
        db.close()
    return render_template('recon/checkin.html', steps=steps, dot_colors=DOT_COLORS)


@bp.route('/api/recon/checkin-scan', methods=['POST'])
def api_checkin_scan():
    """Scan a VIN photo (9B) -> find the recon car. No move here; the page calls
    /api/recon/<id>/move next so the full move logic runs."""
    import re as _cre
    f = request.files.get('photo')
    vin = (request.form.get('vin') or '').strip().upper()
    if f is not None and not vin:
        raw = f.read()
        if len(raw) < 200:
            return jsonify({'ok': False, 'error': 'image too small — retake the photo'})
        try:
            res = _scan_image(raw,
                "Read the 17-character VIN from this photo of a car, its door-jamb "
                "sticker, windshield plate, or the dash. A VIN is exactly 17 characters "
                "of uppercase letters and digits and never uses the letters I, O or Q. "
                "Reply with ONLY the VIN, nothing else. If you cannot clearly read a "
                "17-character VIN, reply NONE.", f.mimetype)
        except Exception as e:
            return jsonify({'ok': False, 'error': 'scan failed: %s' % e})
        txt = (res or '').strip().upper().replace(' ', '').replace('-', '')
        m = _cre.search(r'[A-HJ-NPR-Z0-9]{17}', txt)
        vin = m.group(0) if m else ''
    if len(vin) != 17:
        return jsonify({'ok': False, 'error': 'could not read a VIN — get a closer, clearer shot of the VIN'})
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT id, year, make, model, stock_no, current_step_id FROM recon_units "
                    "WHERE vin=%s ORDER BY (status NOT IN ('removed','sold')) DESC, id DESC LIMIT 1", (vin,))
        u = cur.fetchone()
        cur_name = None
        if u and u.get('current_step_id'):
            cur.execute("SELECT name FROM recon_step_defs WHERE id=%s", (u['current_step_id'],))
            sr = cur.fetchone()
            cur_name = sr['name'] if sr else None
    finally:
        db.close()
    if not u:
        return jsonify({'ok': False, 'error': 'VIN %s is not on the recon board' % vin, 'vin': vin})
    label = ('%s %s %s' % (u.get('year') or '', u.get('make') or '', u.get('model') or '')).strip()
    return jsonify({'ok': True, 'vin': vin, 'unit_id': u['id'], 'stock_no': u.get('stock_no'),
                    'label': label, 'current_step': cur_name})
