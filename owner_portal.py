"""EW Owner Portal — mobile-first read-mostly view for the 3 EW owners.

Mirrors the Partner Portal pattern (separate session key, bcrypt auth, invite
+ reset tokens, no-cache headers) but is a single shared portal at /owner
rather than per-dealer. Owners see all bids in a stripped-down phone-friendly
list and a collapsed bid detail page; they observe activity, they don't
operate (no reply input, no status buttons).

Push notifications fire on new bid creation via the shared push_subscriptions
table — owner subs are stored with `rep_phone = 'owner:<id>'` so the existing
`send_push_to_rep` helper in app.py works unchanged.

Routes:
  /owner                    — dashboard (login required)
  /owner/login              — login form
  /owner/logout
  /owner/forgot             — request reset email
  /owner/reset/<token>      — reset password landing
  /owner/invite/<token>     — first-time set-password landing
  /owner/bid/<id>           — collapsed read-only bid detail
  /owner/poll               — JSON: new-bid count + has_new flag for chime
  /owner/settings           — notification toggles + push subscribe button
  /owner/push/subscribe     — POST: save VAPID subscription
  /owner/push/unsubscribe   — POST: remove VAPID subscription
  /admin/owners             — admin: list / invite / disable owners
  /admin/owners/invite      — admin: POST invite
  /admin/owners/<id>/toggle — admin: POST toggle active
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2
import psycopg2.extras
from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for, flash)

bp = Blueprint('owner', __name__)


@bp.after_request
def _no_cache(resp):
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# ── Config ────────────────────────────────────────────────────────────────
PORTAL_BASE = os.environ.get('PORTAL_BASE', 'https://experience-wholesale.net')
EMAIL_FROM = os.environ.get('EMAIL_FROM', 'opies32765@gmail.com')
EMAIL_REPLY_TO = os.environ.get('EMAIL_REPLY_TO', 'oscar@experience-wholesale.com')
EMAIL_TEST_TO = os.environ.get('EMAIL_TEST_TO', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
INVITE_TTL_DAYS = 7
RESET_TTL_HOURS = 2
TG_BOT = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '')


def _db():
    return psycopg2.connect(os.environ['DATABASE_URL'],
                            cursor_factory=psycopg2.extras.RealDictCursor)


# ── Email + Telegram helpers (mirror partner_portal patterns) ─────────────
def _send_email(to_addr: str, subject: str, html: str) -> bool:
    if EMAIL_TEST_TO:
        subject = f'[TEST→{to_addr}] {subject}'
        to_addr = EMAIL_TEST_TO
    if not RESEND_API_KEY:
        print(f'[owner-email:STUB] to={to_addr}  subj={subject}', flush=True)
        return False
    try:
        import resend
        resend.api_key = RESEND_API_KEY
        payload = {'from': EMAIL_FROM, 'to': to_addr,
                   'subject': subject, 'html': html}
        if EMAIL_REPLY_TO:
            payload['reply_to'] = EMAIL_REPLY_TO
        resend.Emails.send(payload)
        return True
    except Exception as e:
        print(f'[owner-email:FAIL] {type(e).__name__}: {e}', flush=True)
        return False


def _tg_alert(text: str):
    if not TG_BOT or not TG_CHAT:
        return
    try:
        import requests
        requests.post(
            f'https://api.telegram.org/bot{TG_BOT}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': text, 'parse_mode': 'HTML'},
            timeout=4)
    except Exception:
        pass


# ── Auth helpers ──────────────────────────────────────────────────────────
def _hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _check_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def _current_owner():
    """Return the logged-in owner_users row, or None. Uses session key
    `owner_user_id` so it doesn't collide with EW-admin or partner sessions."""
    uid = session.get('owner_user_id')
    if not uid:
        return None
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM owner_users WHERE id = %s AND active = TRUE",
                    (uid,))
        return cur.fetchone()


def _track_presence(owner_id: int):
    try:
        ip = (request.headers.get('X-Forwarded-For') or
              request.remote_addr or '').split(',')[0].strip()
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE owner_users
                           SET last_seen_at = NOW(), last_ip = %s
                           WHERE id = %s""", (ip, owner_id))
            conn.commit()
    except Exception:
        pass


# ── Dashboard ─────────────────────────────────────────────────────────────
@bp.route('/owner')
def dashboard():
    user = _current_owner()
    if not user:
        return redirect(url_for('owner.login'))
    _track_presence(user['id'])

    status_filter = (request.args.get('status') or 'open').lower()
    with _db() as conn, conn.cursor() as cur:
        # Stat strip — counts by status across all bids, last 7 days
        cur.execute("""
            SELECT status, COUNT(*) AS n
            FROM bids
            WHERE created_at >= NOW() - INTERVAL '30 days'
            GROUP BY status
        """)
        stats_raw = {r['status']: r['n'] for r in cur.fetchall()}
        stats = {
            'new': stats_raw.get('new', 0),
            'reviewing': stats_raw.get('reviewing', 0),
            'bid_sent': stats_raw.get('bid_sent', 0),
            'bought': stats_raw.get('bought', 0),
            'passed': stats_raw.get('passed', 0),
        }

        # List query. Default 'open' = new + reviewing + bid_sent (active work).
        if status_filter == 'all':
            where = "TRUE"
        elif status_filter == 'open':
            where = "b.status IN ('new','reviewing','bid_sent')"
        elif status_filter in ('new', 'reviewing', 'bid_sent', 'bought', 'passed'):
            where = f"b.status = '{status_filter}'"
        else:
            where = "TRUE"

        cur.execute(f"""
            SELECT b.id, b.vin, b.year, b.make, b.model, b.trim, b.mileage,
                   b.color, b.status, b.asking_price, b.ai_price, b.bid_amount,
                   b.created_at, b.updated_at, b.has_unread, b.salesperson,
                   c.name AS contact_name, b.phone,
                   (SELECT COALESCE(local_path, url) FROM bid_photos
                    WHERE bid_id = b.id
                    ORDER BY id ASC LIMIT 1) AS first_photo
            FROM bids b
            LEFT JOIN contacts c ON c.id = b.contact_id
            WHERE {where}
            ORDER BY b.updated_at DESC
            LIMIT 100
        """)
        bids = cur.fetchall()

    return render_template('owner_dashboard.html',
                           user=user, bids=bids, stats=stats,
                           status_filter=status_filter)


# ── Bid detail (collapsed read-only) ─────────────────────────────────────
@bp.route('/owner/bid/<int:bid_id>')
def bid_detail(bid_id):
    user = _current_owner()
    if not user:
        return redirect(url_for('owner.login'))
    _track_presence(user['id'])

    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT b.*, c.name AS contact_name, c.company AS contact_company
            FROM bids b
            LEFT JOIN contacts c ON c.id = b.contact_id
            WHERE b.id = %s
        """, (bid_id,))
        bid = cur.fetchone()
        if not bid:
            return ('Not found', 404)

        cur.execute("""
            SELECT id, COALESCE(local_path, url) AS photo_url
            FROM bid_photos
            WHERE bid_id = %s
            ORDER BY id ASC
        """, (bid_id,))
        photos = cur.fetchall()

        cur.execute("""
            SELECT id, direction, message, from_phone, created_at
            FROM bid_messages
            WHERE bid_id = %s
            ORDER BY created_at ASC
        """, (bid_id,))
        messages = cur.fetchall()

        # vAuto books, Carfax/AutoCheck, iPacket, AccuTrade — best-effort,
        # tables may not exist on dev installs.
        vauto = None
        try:
            cur.execute("SELECT * FROM vauto_lookups WHERE bid_id = %s", (bid_id,))
            vauto = cur.fetchone()
        except Exception:
            pass
        ipacket = None
        try:
            cur.execute("SELECT * FROM ipacket_lookups WHERE bid_id = %s", (bid_id,))
            ipacket = cur.fetchone()
        except Exception:
            pass
        accutrade = None
        try:
            cur.execute("SELECT * FROM accutrade_lookups WHERE bid_id = %s", (bid_id,))
            accutrade = cur.fetchone()
        except Exception:
            pass

    return render_template('owner_bid.html',
                           user=user, bid=bid, photos=photos,
                           messages=messages, vauto=vauto,
                           ipacket=ipacket, accutrade=accutrade)


# ── Poll for new-bid alerts ───────────────────────────────────────────────
@bp.route('/owner/poll')
def poll():
    user = _current_owner()
    if not user:
        return jsonify({'logged_in': False}), 401
    _track_presence(user['id'])
    since = request.args.get('since', type=int) or 0
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS n,
                   COALESCE(MAX(id), 0) AS max_id
            FROM bids
            WHERE id > %s AND status IN ('new', 'reviewing')
        """, (since,))
        row = cur.fetchone()
    return jsonify({
        'logged_in': True,
        'new_count': row['n'] or 0,
        'max_id': row['max_id'] or since,
    })


# ── Settings: notification toggles + push subscribe ──────────────────────
@bp.route('/owner/settings', methods=['GET', 'POST'])
def settings():
    user = _current_owner()
    if not user:
        return redirect(url_for('owner.login'))
    if request.method == 'POST':
        notify_new = bool(request.form.get('notify_new_bid'))
        notify_reply = bool(request.form.get('notify_bid_reply'))
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE owner_users
                           SET notify_new_bid = %s, notify_bid_reply = %s
                           WHERE id = %s""",
                        (notify_new, notify_reply, user['id']))
            conn.commit()
        flash('Saved.', 'success')
        return redirect(url_for('owner.settings'))
    return render_template('owner_settings.html', user=user)


@bp.route('/owner/push/subscribe', methods=['POST'])
def push_subscribe():
    user = _current_owner()
    if not user:
        return jsonify({'error': 'not logged in'}), 401
    data = request.json or {}
    sub = data.get('subscription') or {}
    endpoint = sub.get('endpoint', '')
    keys = sub.get('keys') or {}
    p256dh = keys.get('p256dh', '')
    auth = keys.get('auth', '')
    if not endpoint or not p256dh or not auth:
        return jsonify({'error': 'missing fields'}), 400
    rep_phone = f'owner:{user["id"]}'
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO push_subscriptions (rep_phone, endpoint, p256dh, auth)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (endpoint) DO UPDATE
            SET rep_phone = EXCLUDED.rep_phone,
                p256dh = EXCLUDED.p256dh,
                auth = EXCLUDED.auth
        """, (rep_phone, endpoint, p256dh, auth))
        conn.commit()
    return jsonify({'success': True})


@bp.route('/owner/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    user = _current_owner()
    if not user:
        return jsonify({'error': 'not logged in'}), 401
    endpoint = (request.json or {}).get('endpoint', '')
    if not endpoint:
        return jsonify({'error': 'missing endpoint'}), 400
    with _db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM push_subscriptions WHERE endpoint = %s",
                    (endpoint,))
        conn.commit()
    return jsonify({'success': True})


# ── Auth routes ───────────────────────────────────────────────────────────
@bp.route('/owner/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        pw = request.form.get('password') or ''
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM owner_users
                           WHERE LOWER(email) = %s AND active = TRUE""",
                        (email,))
            u = cur.fetchone()
            if not u or not u['password_hash'] or not _check_password(pw, u['password_hash']):
                flash('Email or password is incorrect.', 'error')
                return render_template('owner_login.html')
            cur.execute("UPDATE owner_users SET last_login_at = NOW() WHERE id = %s",
                        (u['id'],))
            conn.commit()
            session.permanent = True
            session['owner_user_id'] = u['id']
            _tg_alert(f'🔓 EW Partner login: <b>{u["name"] or u["email"]}</b>')
            return redirect(url_for('owner.dashboard'))
    return render_template('owner_login.html')


@bp.route('/owner/logout')
def logout():
    session.pop('owner_user_id', None)
    return redirect(url_for('owner.login'))


@bp.route('/owner/invite/<token>', methods=['GET', 'POST'])
def accept_invite(token):
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT * FROM owner_users
                       WHERE invite_token = %s AND invite_used_at IS NULL""",
                    (token,))
        u = cur.fetchone()
        if not u:
            return render_template('owner_invite_invalid.html'), 400
        if u['invite_sent_at'] and u['invite_sent_at'] < \
                datetime.now(timezone.utc) - timedelta(days=INVITE_TTL_DAYS):
            return render_template('owner_invite_invalid.html', expired=True), 400
        if request.method == 'POST':
            pw = request.form.get('password') or ''
            pw2 = request.form.get('password2') or ''
            if len(pw) < 8:
                flash('Password must be at least 8 characters.', 'error')
            elif pw != pw2:
                flash('Passwords do not match.', 'error')
            else:
                cur.execute("""UPDATE owner_users
                               SET password_hash = %s,
                                   invite_used_at = NOW(),
                                   invite_token = NULL
                               WHERE id = %s""",
                            (_hash_password(pw), u['id']))
                conn.commit()
                session.permanent = True
                session['owner_user_id'] = u['id']
                _tg_alert(f'✅ EW Partner activated: <b>{u["name"] or u["email"]}</b>')
                return redirect(url_for('owner.dashboard'))
    return render_template('owner_set_password.html', email=u['email'])


@bp.route('/owner/forgot', methods=['GET', 'POST'])
def forgot():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""SELECT id, name, email FROM owner_users
                           WHERE LOWER(email) = %s AND active = TRUE""",
                        (email,))
            u = cur.fetchone()
            if u:
                tok = secrets.token_urlsafe(32)
                cur.execute("""UPDATE owner_users
                               SET reset_token = %s, reset_sent_at = NOW()
                               WHERE id = %s""", (tok, u['id']))
                conn.commit()
                link = f'{PORTAL_BASE}/owner/reset/{tok}'
                _send_email(u['email'], 'Reset your EW Partner Portal password',
                    f'<p>Hi {u["name"] or ""},</p>'
                    f'<p>Click to reset your password (link valid {RESET_TTL_HOURS}h):</p>'
                    f'<p><a href="{link}">{link}</a></p>')
        # Always show generic success message — don't leak which emails exist
        flash('If that email is registered, a reset link is on the way.', 'success')
        return redirect(url_for('owner.login'))
    return render_template('owner_forgot.html')


@bp.route('/owner/reset/<token>', methods=['GET', 'POST'])
def reset(token):
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM owner_users WHERE reset_token = %s", (token,))
        u = cur.fetchone()
        if not u or not u['reset_sent_at'] or u['reset_sent_at'] < \
                datetime.now(timezone.utc) - timedelta(hours=RESET_TTL_HOURS):
            return render_template('owner_invite_invalid.html', expired=True), 400
        if request.method == 'POST':
            pw = request.form.get('password') or ''
            pw2 = request.form.get('password2') or ''
            if len(pw) < 8:
                flash('Password must be at least 8 characters.', 'error')
            elif pw != pw2:
                flash('Passwords do not match.', 'error')
            else:
                cur.execute("""UPDATE owner_users
                               SET password_hash = %s, reset_token = NULL,
                                   reset_sent_at = NULL
                               WHERE id = %s""", (_hash_password(pw), u['id']))
                conn.commit()
                session.permanent = True
                session['owner_user_id'] = u['id']
                return redirect(url_for('owner.dashboard'))
    return render_template('owner_set_password.html', email=u['email'])


# ── Admin: list / invite / disable owners ────────────────────────────────
def _admin_required():
    return session.get('logged_in')


@bp.route('/admin/owners')
def admin_list():
    if not _admin_required():
        return redirect('/login')
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT id, email, name, phone, active, last_login_at,
                              last_seen_at, last_ip, notify_new_bid,
                              notify_bid_reply, invite_used_at, invite_sent_at,
                              created_at
                       FROM owner_users
                       ORDER BY created_at DESC""")
        owners = cur.fetchall()
    return render_template('admin_owners.html', owners=owners)


@bp.route('/admin/owners/invite', methods=['POST'])
def admin_invite():
    if not _admin_required():
        return redirect('/login')
    email = (request.form.get('email') or '').strip().lower()
    name = (request.form.get('name') or '').strip() or None
    phone = (request.form.get('phone') or '').strip() or None
    if not email or '@' not in email:
        flash('Valid email required.', 'error')
        return redirect(url_for('owner.admin_list'))
    tok = secrets.token_urlsafe(32)
    with _db() as conn, conn.cursor() as cur:
        try:
            cur.execute("""
                INSERT INTO owner_users (email, name, phone, invite_token,
                                         invite_sent_at)
                VALUES (%s, %s, %s, %s, NOW())
                RETURNING id
            """, (email, name, phone, tok))
            conn.commit()
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            # Re-arm invite on existing row
            cur.execute("""
                UPDATE owner_users
                SET invite_token = %s, invite_sent_at = NOW(),
                    invite_used_at = NULL,
                    name = COALESCE(%s, name), phone = COALESCE(%s, phone),
                    active = TRUE
                WHERE LOWER(email) = %s
                RETURNING id
            """, (tok, name, phone, email))
            conn.commit()
    link = f'{PORTAL_BASE}/owner/invite/{tok}'
    _send_email(email, 'You\'re invited to the EW Partner Portal',
        f'<p>Hi {name or ""},</p>'
        f'<p>You have been invited to the Experience Wholesale Partner Portal — '
        f'a mobile dashboard that pings you when new bids arrive.</p>'
        f'<p>Click here to set your password (valid {INVITE_TTL_DAYS} days):</p>'
        f'<p><a href="{link}">{link}</a></p>'
        f'<p>Once your password is set, bookmark <a href="{PORTAL_BASE}/owner">'
        f'{PORTAL_BASE}/owner</a> on your phone — install it as an app from '
        f'the browser menu (Add to Home Screen) for push notifications.</p>')
    _tg_alert(f'📧 EW Partner invite sent to <b>{email}</b>')
    flash(f'Invite sent to {email}.', 'success')
    return redirect(url_for('owner.admin_list'))


@bp.route('/admin/owners/<int:owner_id>/toggle', methods=['POST'])
def admin_toggle(owner_id):
    if not _admin_required():
        return redirect('/login')
    with _db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE owner_users SET active = NOT active WHERE id = %s",
                    (owner_id,))
        conn.commit()
    return redirect(url_for('owner.admin_list'))


@bp.route('/admin/owners/<int:owner_id>/delete', methods=['POST'])
def admin_delete(owner_id):
    if not _admin_required():
        return redirect('/login')
    with _db() as conn, conn.cursor() as cur:
        # Clean push subs too
        cur.execute("DELETE FROM push_subscriptions WHERE rep_phone = %s",
                    (f'owner:{owner_id}',))
        cur.execute("DELETE FROM owner_users WHERE id = %s", (owner_id,))
        conn.commit()
    return redirect(url_for('owner.admin_list'))


# ── Notification fan-out (called from app.py bid-creation paths) ─────────
def notify_owners_new_bid(bid_id: int, send_push_fn):
    """Fire push to all opted-in owners when a new bid lands.

    `send_push_fn` is app.py's `send_push_to_rep(rep_phone, title, body, url)`
    — passed in to avoid a circular import. Best-effort: any errors are
    swallowed (owner UX should never block bid creation).
    """
    try:
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT b.id, b.year, b.make, b.model, b.vin, b.asking_price,
                       c.name AS rep_name
                FROM bids b
                LEFT JOIN contacts c ON c.id = b.contact_id
                WHERE b.id = %s
            """, (bid_id,))
            bid = cur.fetchone()
            if not bid:
                return
            cur.execute("""SELECT id FROM owner_users
                           WHERE active = TRUE AND notify_new_bid = TRUE""")
            owner_ids = [r['id'] for r in cur.fetchall()]
        if not owner_ids:
            return
        ymm_parts = [str(bid.get(k) or '').strip() for k in ('year', 'make', 'model')]
        ymm = ' '.join(p for p in ymm_parts if p) or 'Vehicle'
        ask = bid.get('asking_price')
        ask_str = f'  •  Ask ${ask:,}' if ask else ''
        rep = (bid.get('rep_name') or '').strip()
        rep_str = f'  •  {rep}' if rep else ''
        title = f'New bid #{bid["id"]}'
        body = f'{ymm}{ask_str}{rep_str}'
        url = f'/owner/bid/{bid["id"]}'
        for oid in owner_ids:
            try:
                send_push_fn(f'owner:{oid}', title, body, url)
            except Exception:
                pass
    except Exception as e:
        print(f'[owner-notify] {type(e).__name__}: {e}', flush=True)
