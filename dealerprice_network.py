"""
dealerprice_network.py — DealerPrice "Become part of the Dealer Network" gate.
DEALERPRICE_NETWORK_2026_06_30.

Adds the vetting/application funnel for dealerprice.net WITHOUT touching the
live bid/enrichment path. Two halves:

  • Public (X-Auth, login-exempt via the /api/dealerprice/ prefix):
      POST /api/dealerprice/apply           — a dealer applies to the network
      POST /api/dealerprice/check-existing   — Q0 "already an EW dealer?" lookup

  • Operator (behind the app-level require_login, NOT under /api/):
      GET  /network/applications                  — review queue
      GET  /network/application/<id>              — full vetting packet
      GET  /network/application/<id>/doc/<which>  — serve the PRIVATE license/Tax-ID image
      POST /network/application/<id>/approve      — mint member token + invite the dealer
      POST /network/application/<id>/reject
      POST /network/application/<id>/needs-info

HARD RULES honored: no FK to bids (HR1 — can never block enrichment); LSL is
read-only (HR6); no cloud LLM (HR4); C1-only (HR5); no import-time DDL — the
tables ship via ops/migrations/2026-06-30_dealer_network.sql (HR8). Uploaded
license/Tax-ID docs are stored OUTSIDE /static and served only through the
login-gated /network/.../doc route.

Registered by wsgi.py on every gunicorn worker boot (drift-resistant), the same
pattern as recon / wholesaler_review / network_push.
"""
from __future__ import annotations
import os
import re
import json
import time
import base64
import secrets
from datetime import datetime, timezone

from flask import (Blueprint, render_template, request, jsonify, abort,
                   session, redirect, url_for, send_file)

bp = Blueprint('dealerprice_network', __name__)

SECRET = (os.environ.get('EW_DEALERPRICE_SECRET') or '').strip()
LSL_DB = os.environ.get('LSL_DB_PATH', '/opt/livesaleslog/crm.db')
PRIV_DOC_ROOT = os.environ.get('DP_DOC_ROOT', '/opt/expwholesale/private/dealer_docs')
# public base for the magic link we text/email an approved dealer
DP_PUBLIC_BASE = os.environ.get('DP_PUBLIC_BASE', 'https://dealerprice.net')

DEALER_TYPES = ['Exotic', 'High-Volume Commodity', 'Niche / Specialty',
                'Wholesale', 'Large-Volume Mix', 'Subprime']


# ── small coercion helpers ──────────────────────────────────────────────────
def _s(v):
    return ('' if v is None else str(v)).strip()


def _digits(v):
    return re.sub(r'[^0-9]', '', _s(v))


def _int(v):
    d = _digits(v)
    try:
        return int(d) if d else None
    except ValueError:
        return None


def _num(v):
    s = re.sub(r'[^0-9.]', '', _s(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _b(v):
    return v in (True, 'true', 'on', '1', 1, 'yes', 'YES')


def _date(v):
    s = _s(v)
    if not s:
        return None
    if re.fullmatch(r'\d{4}-\d{2}', s):
        s += '-01'
    try:
        datetime.strptime(s, '%Y-%m-%d')
        return s
    except ValueError:
        return None


# ── DB / LSL ────────────────────────────────────────────────────────────────
def _db():
    from app import get_db
    return get_db()


def _lsl_conn():
    import sqlite3
    c = sqlite3.connect('file:%s?mode=ro' % LSL_DB, uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _roster_match(name):
    """Is this dealership/referrer already an EW counterparty? Read-only LSL
    lookup against suppliers (sellers/wholesalers) then customers (buyers).
    Returns {} when unknown, else a tidy match dict for the review packet."""
    name = _s(name)
    if len(name) < 3:
        return {}
    try:
        c = _lsl_conn()
        try:
            r = c.execute(
                "SELECT name, primary_contact, office, primary_contact_mobile, "
                "city, state FROM suppliers WHERE name=? COLLATE NOCASE LIMIT 1",
                (name,)).fetchone()
            if not r:
                r = c.execute(
                    "SELECT name, primary_contact, office, primary_contact_mobile, "
                    "city, state FROM suppliers WHERE name LIKE ? "
                    "ORDER BY length(name) LIMIT 1", (name + '%',)).fetchone()
            if not r and len(name) >= 5:                 # contains fallback (partial dealership / referrer)
                r = c.execute(
                    "SELECT name, primary_contact, office, primary_contact_mobile, "
                    "city, state FROM suppliers WHERE name LIKE ? "
                    "ORDER BY length(name) LIMIT 1", ('%' + name + '%',)).fetchone()
            if r:
                return {'matched': True, 'source': 'suppliers', 'name': r['name'],
                        'contact': _s(r['primary_contact']),
                        'phone': _s(r['office'] or r['primary_contact_mobile']),
                        'city': _s(r['city']), 'state': _s(r['state'])}
            r = c.execute(
                "SELECT company_name, full_name, mobile FROM customers "
                "WHERE company_name=? COLLATE NOCASE OR full_name=? COLLATE NOCASE "
                "LIMIT 1", (name, name)).fetchone()
            if r:
                return {'matched': True, 'source': 'customers',
                        'name': _s(r['company_name'] or r['full_name']),
                        'contact': _s(r['full_name']), 'phone': _s(r['mobile'])}
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] roster_match: %s' % e, flush=True)
    return {}


def _roster_search(q, limit=8):
    """Typeahead for the Q0 existing-dealer path — distinct supplier names."""
    q = _s(q)
    if len(q) < 2:
        return []
    out = []
    try:
        c = _lsl_conn()
        try:
            rows = c.execute(
                "SELECT DISTINCT name, city, state FROM suppliers "
                "WHERE name LIKE ? AND name<>'' "
                "ORDER BY (name LIKE ?) DESC, length(name) LIMIT ?",
                ('%' + q + '%', q + '%', limit)).fetchall()
            out = [{'name': r['name'], 'city': _s(r['city']), 'state': _s(r['state'])}
                   for r in rows]
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] roster_search: %s' % e, flush=True)
    return out


# ── private document storage (NOT under /static) ────────────────────────────
def _save_doc(app_id, which, data_url):
    """Persist a base64 data-url (license / tax-id image or PDF) to a private,
    0600 file under PRIV_DOC_ROOT/<app_id>/. Returns the absolute path or None."""
    if not data_url:
        return None
    media = 'image/jpeg'
    s = data_url
    if isinstance(s, str) and s.startswith('data:'):
        try:
            head, s = s.split(',', 1)
            media = head.split(';')[0].split(':', 1)[1] or media
        except Exception:
            return None
    try:
        raw = base64.b64decode(s)
    except Exception:
        return None
    if not raw or len(raw) > 18_000_000:           # guard: empty / >18MB
        return None
    ext = {'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png',
           'image/webp': 'webp', 'application/pdf': 'pdf'}.get(media.lower(), 'bin')
    d = os.path.join(PRIV_DOC_ROOT, str(app_id))
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(PRIV_DOC_ROOT, 0o700)
        os.chmod(d, 0o700)
    except Exception:
        pass
    path = os.path.join(d, '%s.%s' % (which, ext))
    with open(path, 'wb') as f:
        f.write(raw)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def _doc_mime(path):
    ext = (path or '').rsplit('.', 1)[-1].lower()
    return {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp',
            'pdf': 'application/pdf'}.get(ext, 'application/octet-stream')


# ── notifications ───────────────────────────────────────────────────────────
def _tg(msg):
    try:
        from app import _tg_worker_alert
        _tg_worker_alert(msg)
    except Exception as e:
        print('[dp-network] tg: %s' % e, flush=True)


def _email(to_addr, subject, html):
    """Best-effort email via the Resend path recon already uses."""
    if not to_addr:
        return
    try:
        from recon_routes import _recon_send_raw
        _recon_send_raw(to_addr, subject, html)
    except Exception as e:
        print('[dp-network] email: %s' % e, flush=True)


def _invite_member(m):
    """Text + email an approved dealer their access magic link."""
    link = '%s/access/%s' % (DP_PUBLIC_BASE.rstrip('/'), m['token'])
    name = _s(m.get('dealership_name')) or 'there'
    phone = _digits(m.get('contact_phone'))
    if len(phone) == 10:
        try:
            from app import send_sms
            send_sms('+1' + phone,
                     "You're approved for the Experience Wholesale dealer "
                     "network. Submit vehicles for a bid here: %s" % link)
        except Exception as e:
            print('[dp-network] invite sms: %s' % e, flush=True)
    _email(_s(m.get('contact_email')),
           'Approved — Experience Wholesale Dealer Network',
           "<p>Welcome to the network, %s.</p>"
           "<p>You're approved to submit vehicles for a wholesale bid. "
           "Use your private link any time:</p>"
           "<p><a href='%s'>%s</a></p>"
           "<p>— Experience Wholesale</p>" % (name, link, link))


# ── auth ────────────────────────────────────────────────────────────────────
def _bad_secret():
    """Return a JSON 401 if X-Auth is wrong, else None. Returning a response
    (not abort) keeps us off the app's HTML error-handler path, which 500s on
    /api/ routes — matches the existing /api/dealerprice/bid pattern."""
    if not SECRET or (request.headers.get('X-Auth') or '').strip() != SECRET:
        return jsonify({'error': 'bad auth'}), 401
    return None


def _reviewer():
    return (session.get('user') or session.get('username')
            or session.get('reviewer') or 'operator')


# ── dashboard nav badge: pending-application count (cached 15s, drift-resistant
#    via @bp.app_context_processor like recon_enabled) ────────────────────────
_PENDING_CACHE = {'t': 0.0, 'n': 0}


@bp.app_context_processor
def _inject_dp_network():
    def dealer_apps_pending():
        now = time.time()
        if now - _PENDING_CACHE['t'] < 15:
            return _PENDING_CACHE['n']
        try:
            db = _db(); cur = db.cursor()
            cur.execute("SELECT count(*) AS n FROM dealer_applications WHERE status='pending'")
            _PENDING_CACHE['n'] = cur.fetchone()['n']
            _PENDING_CACHE['t'] = now
            db.close()
        except Exception:
            pass
        return _PENDING_CACHE['n']
    return {'dealer_apps_pending': dealer_apps_pending}


CLASS_LABELS = {'current_partner': 'Current Partner',
                'previous_partner': 'Previous Partner',
                'new_applicant': 'New Applicant'}


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC API  (dealerprice.net -> EW, shared-secret)
# ════════════════════════════════════════════════════════════════════════════
@bp.route('/api/dealerprice/check-existing', methods=['POST'])
def api_dp_check_existing():
    """Q0 existing-dealer typeahead -> candidate roster names."""
    r = _bad_secret()
    if r:
        return r
    data = request.get_json(silent=True) or {}
    return jsonify({'ok': True, 'matches': _roster_search(data.get('name') or data.get('q'))})


@bp.route('/api/dealerprice/apply', methods=['POST'])
def api_dp_apply():
    """A dealer applies to the network. Lands as a dealer_applications row for
    operator review. New dealers MUST include license + tax-id (number + image)
    up front. NEVER auto-provisions (impersonation guard) — approval is an
    operator action in /network/applications."""
    r = _bad_secret()
    if r:
        return r
    d = request.get_json(silent=True) or {}

    is_existing = _b(d.get('is_existing'))
    dealership = _s(d.get('dealership_name'))
    cname = _s(d.get('contact_name'))
    cemail = _s(d.get('contact_email')).lower()
    cphone = _digits(d.get('contact_phone'))

    # base requirements for everyone
    miss = [lbl for k, lbl in (('dealership_name', 'Dealership name'),
                               ('contact_name', 'Your name'),
                               ('contact_email', 'Email'),
                               ('contact_phone', 'Mobile')) if not _s(d.get(k))]
    if miss:
        return jsonify({'ok': False, 'error': '%s required.' % ', '.join(miss)}), 400

    # NEW dealers: license + tax-id (number + image) + attestation up front
    if not is_existing:
        if not _s(d.get('license_number')) or not _s(d.get('tax_id')):
            return jsonify({'ok': False, 'error': 'Dealer license number and Tax ID are required.'}), 400
        if not d.get('license_image') or not d.get('taxid_image'):
            return jsonify({'ok': False, 'error': 'A photo of your dealer license and Tax ID / resale certificate is required.'}), 400
        if not _b(d.get('attestation')):
            return jsonify({'ok': False, 'error': 'Please confirm the information is accurate.'}), 400

    types = d.get('dealer_types')
    if isinstance(types, list):
        types = ', '.join(_s(x) for x in types if _s(x))
    else:
        types = _s(types)

    name_match = _roster_match(dealership)
    referrer = _s(d.get('referrer_name'))
    referrer_match = _roster_match(referrer) if referrer and referrer.lower() not in ('none', 'n/a') else {}

    # keep an audit copy of the submission WITHOUT the big base64 blobs
    audit = {k: v for k, v in d.items() if k not in ('license_image', 'taxid_image', 'photos')}

    from psycopg2.extras import Json
    db = _db(); cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO dealer_applications (
                status, is_existing, dealership_name, dba, dealer_group, franchises,
                entity_type, entity_state, years_in_business, years_at_location,
                units_per_month, units_annual, avg_investment_band, avg_investment_num,
                credit_line, floorplan_provider, floorplan_line, dealer_types,
                primary_makes, price_tier, license_number, license_state, license_exp,
                tax_id, bond_provider, bond_amount, physical_lot, lot_address, website,
                reputation_url, auction_access, payment_ready, bank_reference,
                trade_reference, referrer_name, contact_name, contact_email,
                contact_phone, attestation, tcpa_consent, notes, name_match,
                referrer_match, raw_payload)
            VALUES ('pending',%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,
                    %s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,
                    %s,%s,%s,%s, %s,%s,%s)
            RETURNING id
        """, (
            is_existing, dealership, _s(d.get('dba')), _s(d.get('dealer_group')),
            _s(d.get('franchises')), _s(d.get('entity_type')), _s(d.get('entity_state')),
            _int(d.get('years_in_business')), _int(d.get('years_at_location')),
            _int(d.get('units_per_month')), _int(d.get('units_annual')),
            _s(d.get('avg_investment_band')), _num(d.get('avg_investment_num') or d.get('avg_investment')),
            _num(d.get('credit_line')), _s(d.get('floorplan_provider')), _num(d.get('floorplan_line')),
            types, _s(d.get('primary_makes')), _s(d.get('price_tier')),
            _s(d.get('license_number')), _s(d.get('license_state')), _date(d.get('license_exp')),
            _s(d.get('tax_id')), _s(d.get('bond_provider')), _num(d.get('bond_amount')),
            (_b(d.get('physical_lot')) if d.get('physical_lot') is not None else None),
            _s(d.get('lot_address')), _s(d.get('website')), _s(d.get('reputation_url')),
            _s(d.get('auction_access')), _s(d.get('payment_ready')), _s(d.get('bank_reference')),
            _s(d.get('trade_reference')), referrer, cname, cemail, cphone,
            _b(d.get('attestation')), _b(d.get('tcpa_consent')), _s(d.get('notes')),
            Json(name_match or None), Json(referrer_match or None), Json(audit),
        ))
        app_id = cur.fetchone()['id']

        lic = _save_doc(app_id, 'license', d.get('license_image'))
        tax = _save_doc(app_id, 'taxid', d.get('taxid_image'))
        # auto-classify: existing-dealer self-declaration OR an LSL roster match
        # => current partner; otherwise a brand-new applicant (operator can override)
        classification = 'current_partner' if (is_existing or (name_match or {}).get('matched')) else 'new_applicant'
        cur.execute("UPDATE dealer_applications SET license_doc_path=%s, taxid_doc_path=%s, classification=%s WHERE id=%s",
                    (lic, tax, classification, app_id))
        db.commit()
    except Exception as e:
        db.rollback(); db.close()
        print('[dp-network] apply insert: %s' % e, flush=True)
        return jsonify({'ok': False, 'error': 'Could not submit your application — please try again.'}), 500
    db.close()

    tag = 'EXISTING ✓' if is_existing else 'NEW'
    mtag = (' · roster:%s' % name_match['name']) if name_match.get('matched') else ''
    _tg('🪪 <b>New Dealer-Network application</b> #%d (%s)\n%s%s\n%s · %s\nReview: /network/applications'
        % (app_id, tag, dealership or '?', mtag, cname, cemail or cphone))
    return jsonify({'ok': True, 'application_id': app_id, 'status': 'pending', 'existing': is_existing})


# ════════════════════════════════════════════════════════════════════════════
# OPERATOR REVIEW  (behind app-level require_login; NOT under /api/)
# ════════════════════════════════════════════════════════════════════════════
@bp.route('/network/applications')
def network_applications():
    db = _db(); cur = db.cursor()
    cur.execute("""SELECT id, created_at, status, is_existing, dealership_name,
                          dealer_types, units_per_month, avg_investment_band,
                          credit_line, license_number, contact_name, contact_email,
                          contact_phone, name_match, member_id, classification
                     FROM dealer_applications
                    ORDER BY (status='pending') DESC, created_at DESC LIMIT 300""")
    rows = cur.fetchall()
    cur.execute("SELECT status, count(*) AS n FROM dealer_applications GROUP BY status")
    counts = {r['status']: r['n'] for r in cur.fetchall()}
    db.close()
    return render_template('network/applications.html', rows=rows, counts=counts,
                           types=DEALER_TYPES, class_labels=CLASS_LABELS)


@bp.route('/network/members')
def network_members():
    """Onboarded-dealer roster + their bid activity (the per-dealer tracking)."""
    db = _db(); cur = db.cursor()
    try:
        cur.execute("""SELECT m.*,
                          (SELECT count(*) FROM bids b WHERE b.dp_member_id=m.id) AS bid_count,
                          (SELECT max(b.created_at) FROM bids b WHERE b.dp_member_id=m.id) AS last_bid
                         FROM dealerprice_members m
                        ORDER BY m.approved_at DESC LIMIT 500""")
        rows = cur.fetchall()
    except Exception as e:
        print('[dp-network] members list: %s' % e, flush=True)
        cur.execute("SELECT m.*, 0 AS bid_count, NULL AS last_bid FROM dealerprice_members m ORDER BY approved_at DESC LIMIT 500")
        rows = cur.fetchall()
    db.close()
    return render_template('network/members.html', rows=rows)


@bp.route('/network/application/<int:app_id>')
def network_application(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("SELECT * FROM dealer_applications WHERE id=%s", (app_id,))
    a = cur.fetchone()
    if not a:
        db.close(); abort(404)
    member = None
    if a.get('member_id'):
        cur.execute("SELECT * FROM dealerprice_members WHERE id=%s", (a['member_id'],))
        member = cur.fetchone()
    db.close()
    member_bids = _member_bids(member['id']) if member else []
    return render_template('network/application.html', a=a, member=member, member_bids=member_bids, class_labels=CLASS_LABELS)


@bp.route('/network/application/<int:app_id>/doc/<which>')
def network_application_doc(app_id, which):
    """Serve the PRIVATE license / tax-id file. Operator-only (require_login)."""
    if which not in ('license', 'taxid'):
        abort(404)
    db = _db(); cur = db.cursor()
    cur.execute("SELECT license_doc_path, taxid_doc_path FROM dealer_applications WHERE id=%s", (app_id,))
    r = cur.fetchone(); db.close()
    if not r:
        abort(404)
    path = r['license_doc_path'] if which == 'license' else r['taxid_doc_path']
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype=_doc_mime(path),
                     download_name='%s-%s.%s' % (which, app_id, path.rsplit('.', 1)[-1]))


@bp.route('/network/application/<int:app_id>/approve', methods=['POST'])
def network_application_approve(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("SELECT * FROM dealer_applications WHERE id=%s", (app_id,))
    a = cur.fetchone()
    if not a:
        db.close(); abort(404)
    if a.get('member_id'):
        db.close()
        return redirect(url_for('dealerprice_network.network_application', app_id=app_id))
    from psycopg2.extras import Json
    token = secrets.token_urlsafe(24)
    try:
        cur.execute("""INSERT INTO dealerprice_members
                         (application_id, dealership_name, contact_name, contact_email,
                          contact_phone, token, is_existing, lsl_match, approved_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (app_id, a['dealership_name'], a['contact_name'], a['contact_email'],
                     a['contact_phone'], token, a['is_existing'],
                     Json(a.get('name_match') or None), _reviewer()))
        member_id = cur.fetchone()['id']
        cur.execute("""UPDATE dealer_applications SET status='approved', member_id=%s,
                          reviewer=%s, reviewed_at=now(),
                          review_notes=COALESCE(%s, review_notes) WHERE id=%s""",
                    (member_id, _reviewer(), _s(request.form.get('review_notes')) or None, app_id))
        db.commit()
        cur.execute("SELECT * FROM dealerprice_members WHERE id=%s", (member_id,))
        m = cur.fetchone()
    except Exception as e:
        db.rollback(); db.close()
        print('[dp-network] approve: %s' % e, flush=True)
        abort(500)
    db.close()
    try:
        _invite_member(m)
    except Exception as e:
        print('[dp-network] approve invite: %s' % e, flush=True)
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


@bp.route('/network/application/<int:app_id>/reject', methods=['POST'])
def network_application_reject(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("""UPDATE dealer_applications SET status='rejected', reviewer=%s,
                      reviewed_at=now(), review_notes=COALESCE(%s, review_notes)
                    WHERE id=%s""",
                (_reviewer(), _s(request.form.get('review_notes')) or None, app_id))
    db.commit(); db.close()
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


@bp.route('/network/application/<int:app_id>/needs-info', methods=['POST'])
def network_application_needs_info(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("""UPDATE dealer_applications SET status='needs_info', reviewer=%s,
                      reviewed_at=now(), review_notes=COALESCE(%s, review_notes)
                    WHERE id=%s""",
                (_reviewer(), _s(request.form.get('review_notes')) or None, app_id))
    db.commit(); db.close()
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


@bp.route('/network/application/<int:app_id>/classify', methods=['POST'])
def network_application_classify(app_id):
    """Operator override of the auto-classification."""
    c = _s(request.form.get('classification'))
    if c not in CLASS_LABELS:
        c = None
    db = _db(); cur = db.cursor()
    cur.execute("UPDATE dealer_applications SET classification=%s WHERE id=%s", (c, app_id))
    db.commit(); db.close()
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


# ── member token lookup / per-dealer bids ───────────────────────────────────
def _member_by_token(token, touch=False, count_submit=False):
    """Active member row for a token, or None. touch=update last_used_at;
    count_submit=also bump submit_count (used on the /bid path)."""
    token = _s(token)
    if not token:
        return None
    db = _db(); cur = db.cursor()
    try:
        cur.execute("SELECT * FROM dealerprice_members WHERE token=%s AND status='active' LIMIT 1", (token,))
        m = cur.fetchone()
        if m and (touch or count_submit):
            if count_submit:
                cur.execute("UPDATE dealerprice_members SET last_used_at=now(), submit_count=submit_count+1 WHERE id=%s", (m['id'],))
            else:
                cur.execute("UPDATE dealerprice_members SET last_used_at=now() WHERE id=%s", (m['id'],))
            db.commit()
        return m
    except Exception as e:
        print('[dp-network] member lookup: %s' % e, flush=True)
        return None
    finally:
        db.close()


def validate_member_token(token):
    """For the /bid path: validate the token + count a submit. Returns row|None."""
    return _member_by_token(token, count_submit=True)


def _member_bids(member_id, limit=200):
    """All EW bids tagged to this network member (newest first)."""
    db = _db(); cur = db.cursor()
    try:
        cur.execute("""SELECT id, year, make, model, trim, mileage, status, ai_price, created_at
                         FROM bids WHERE dp_member_id=%s ORDER BY id DESC LIMIT %s""", (member_id, limit))
        return cur.fetchall()
    except Exception as e:
        print('[dp-network] member_bids: %s' % e, flush=True)
        return []
    finally:
        db.close()


@bp.route('/api/dealerprice/member', methods=['GET', 'POST'])
def api_dp_member():
    """Validate a member token -> member info, for the /access magic link and
    the pre-filled Get-a-Bid form. Shared-secret; never exposes the token."""
    r = _bad_secret()
    if r:
        return r
    token = request.args.get('token') or (request.get_json(silent=True) or {}).get('token')
    m = _member_by_token(token, touch=True)
    if not m:
        return jsonify({'ok': False})
    return jsonify({'ok': True, 'member': {
        'member_id': m['id'],
        'dealership_name': m['dealership_name'],
        'contact_name': m['contact_name'],
        'contact_email': m['contact_email'],
        'contact_phone': m['contact_phone'],
    }})
