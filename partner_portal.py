"""Partner Dealer Portal.

Self-service dashboard for partner dealers (TXT Charlie, Marino, …) to see
their own cars that crossed an age bucket and request us to bid on them.

Flow:
  1. Admin invites dealer → we email invite link with one-time token
  2. Dealer clicks → sets password → lands on their dashboard
  3. Dashboard lists cars in enabled buckets (30-60, 60-90, 90+, price drops)
  4. Dealer checks cars + sets target price + message + submits
  5. Each submission creates an EW bid with source=partner_request
  6. EW counteroffer streams back to their dashboard + emails them
  7. Daily 5pm ET digest emails new cars crossing thresholds

Routes:
  /partner/<slug>                    — dashboard (login required)
  /partner/<slug>/login              — login form
  /partner/<slug>/logout
  /partner/<slug>/forgot             — forgot-password
  /partner/<slug>/reset/<token>      — reset-password landing
  /partner/invite/<token>            — first-time set-password landing
  /partner/<slug>/submit             — POST: create bid requests
  /admin/partner/<dealer_id>/invite  — admin: send invite
  /admin/partner/<dealer_id>/config  — admin: toggle alerts
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2
import psycopg2.extras
from flask import (Blueprint, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)

bp = Blueprint('partner', __name__)


@bp.after_request
def _no_cache(resp):
    """Partner portal pages must never be cached by the browser. Dashboard
    state changes every time a car is verified, a bucket shifts, or an
    EW response lands — a stale dashboard is worse than a slow one."""
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# ── Config ────────────────────────────────────────────────────────────────
PORTAL_BASE = os.environ.get('PORTAL_BASE', 'https://experience-wholesale.net')
EMAIL_FROM = os.environ.get('EMAIL_FROM', 'opies32765@gmail.com')
# Reply-To routes dealer replies to Oscar's real Outlook mailbox at
# experience-wholesale.com rather than back to the Resend-owned
# orlandoaisolutions.net address. Works even though we can't send FROM that
# domain — Reply-To doesn't require domain verification.
EMAIL_REPLY_TO = os.environ.get('EMAIL_REPLY_TO', 'oscar@experience-wholesale.com')
EMAIL_TEST_TO = os.environ.get('EMAIL_TEST_TO', 'opies32765@gmail.com')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
INVITE_TTL_DAYS = 7
RESET_TTL_HOURS = 2

BUCKETS = [
    ('30_60', '30–60 days on lot'),
    ('60_90', '60–90 days on lot'),
    ('90_plus', '90+ days on lot'),
    ('price_drop', 'Price dropped'),
]


# ── DB helper ─────────────────────────────────────────────────────────────
def _db():
    return psycopg2.connect(os.environ['DATABASE_URL'],
                            cursor_factory=psycopg2.extras.RealDictCursor)


# ── Email helper ──────────────────────────────────────────────────────────
def _send_email(to_addr: str, subject: str, html: str) -> bool:
    """Send via Resend. Falls back to logging if no API key.
    In testing mode (EMAIL_TEST_TO set), redirects every recipient to that
    address so we don't accidentally email a real dealer during dev."""
    # Testing override — route everything to EMAIL_TEST_TO
    if EMAIL_TEST_TO:
        subject = f'[TEST→{to_addr}] {subject}'
        to_addr = EMAIL_TEST_TO

    if not RESEND_API_KEY:
        print(f'[email:STUB] to={to_addr}  subj={subject}\n{html[:400]}')
        return False
    try:
        import resend
        resend.api_key = RESEND_API_KEY
        payload = {
            'from': EMAIL_FROM,
            'to': to_addr,
            'subject': subject,
            'html': html,
        }
        if EMAIL_REPLY_TO:
            payload['reply_to'] = EMAIL_REPLY_TO
        resend.Emails.send(payload)
        return True
    except Exception as e:
        print(f'[email:FAIL] {type(e).__name__}: {e}')
        return False


# ── Welcome onboarding (no activation code) ─────────────────────────────
def create_welcome_account(dealer_id: int, email: str, phone: str = None,
                           sms_opt_in: bool = False, email_bid_alerts: bool = True,
                           full_name: str = None) -> dict:
    """Pre-provision a partner_user account when EW adds a dealer to the DB.
    Skips the magic-link / activation-code dance — directly creates an
    activated account with a random password and emails it. Dealer logs in
    with email + that password and changes it whenever they want.

    Returns {'success': bool, 'password': str|None, 'error': str|None}.
    Per task #33 — onboarding shouldn't require dealer-side action before
    they can receive their first bid.
    """
    if not email or '@' not in email:
        return {'success': False, 'error': 'valid email required', 'password': None}

    raw_pw = secrets.token_urlsafe(9)  # ~12 chars, URL-safe, no ambiguous chars
    pw_hash = _hash_password(raw_pw)

    try:
        with _db() as conn, conn.cursor() as cur:
            # Skip if a partner_user already exists for this email
            cur.execute("SELECT id, dealer_id FROM partner_users WHERE LOWER(email) = LOWER(%s)",
                        (email,))
            existing = cur.fetchone()
            if existing:
                return {'success': False,
                        'error': f'partner account already exists for {email} (dealer #{existing["dealer_id"]})',
                        'password': None}

            cur.execute('''INSERT INTO partner_users
                             (dealer_id, email, full_name, phone,
                              password_hash, sms_opt_in, email_bid_alerts,
                              invite_token, invite_used_at, created_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, NOW(), NOW())
                           RETURNING id''',
                        (dealer_id, email.strip().lower(), full_name,
                         (phone or '').strip() or None,
                         pw_hash, bool(sms_opt_in), bool(email_bid_alerts)))
            user_id = cur.fetchone()['id']
            conn.commit()

            # Build dashboard URL
            cur.execute("SELECT name FROM dealers WHERE id = %s", (dealer_id,))
            d = cur.fetchone()
            slug = _slug_for_dealer(dealer_id, cur)
            portal_url = f"{PORTAL_BASE}/partner/{slug}"
            dealer_name = d['name'] if d else 'your dashboard'

            # Send welcome email — single message, has everything
            html = (
                f'<p>Hi{(" " + full_name) if full_name else ""},</p>'
                f'<p>Experience Wholesale has set up a partner dashboard for '
                f'<strong>{dealer_name}</strong>. You can log in any time to see '
                f'bid offers on your inventory and respond directly.</p>'
                f'<p style="margin:18px 0;padding:14px 16px;background:#f1f5f9;border-radius:8px">'
                f'<strong>Dashboard:</strong> '
                f'<a href="{portal_url}" style="color:#2563eb">{portal_url}</a><br>'
                f'<strong>Login:</strong> {email}<br>'
                f'<strong>Password:</strong> <code style="background:#fff;padding:2px 6px;'
                f'border-radius:3px;font-family:monospace">{raw_pw}</code>'
                f'</p>'
                f'<p>Change this password from your dashboard once you log in.</p>'
                f'<p>You\'ll start receiving bid notifications here when EW makes offers '
                f'on cars in your inventory.</p>'
                f'<p style="font-size:12px;color:#64748b">No further action needed on your '
                f'end — this account is already active.</p>'
            )
            sent = _send_email(email, f'Your Experience Wholesale partner dashboard', html)

        return {'success': True, 'password': raw_pw, 'user_id': user_id, 'email_sent': sent}
    except Exception as e:
        print(f'[create_welcome_account] failed for dealer {dealer_id}: {e}', flush=True)
        return {'success': False, 'error': str(e), 'password': None}


# ── Auth helpers ──────────────────────────────────────────────────────────
def _slug_for_dealer(dealer_id: int, cur) -> str:
    """Return the portal slug for a dealer. Prefers the dedicated
    dealers.portal_slug column (admin-chosen, short URLs like 'marino')
    and falls back to a name-derived slug if not set."""
    cur.execute("SELECT name, portal_slug FROM dealers WHERE id=%s", (dealer_id,))
    row = cur.fetchone()
    if not row:
        return ''
    if row.get('portal_slug'):
        return row['portal_slug']
    import re
    s = re.sub(r'[^a-z0-9]+', '', row['name'].lower())
    return s[:32] or f'dealer{dealer_id}'


def _dealer_by_slug(cur, slug: str):
    """Resolve slug → dealer row. Checks the explicit portal_slug column
    first, then falls back to name-derived for dealers without one set."""
    slug_lower = (slug or '').lower()
    cur.execute("SELECT id, name, portal_slug FROM dealers "
                "WHERE active = TRUE AND LOWER(portal_slug) = %s", (slug_lower,))
    d = cur.fetchone()
    if d:
        return d
    # Fallback: name-derived slug for dealers without an explicit one
    cur.execute("SELECT id, name FROM dealers WHERE active = TRUE")
    for d in cur.fetchall():
        import re
        s = re.sub(r'[^a-z0-9]+', '', d['name'].lower())[:32]
        if s == slug_lower:
            return d
    return None


def _hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _check_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def _current_partner_user():
    """Return the logged-in partner_user row, or None.
    Uses a separate session key (`partner_user_id`) so it doesn't collide
    with EW admin auth."""
    uid = session.get('partner_user_id')
    if not uid:
        return None
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT pu.*, d.name AS dealer_name
            FROM partner_users pu
            JOIN dealers d ON d.id = pu.dealer_id
            WHERE pu.id = %s
        """, (uid,))
        return cur.fetchone()


# ── Dashboard ─────────────────────────────────────────────────────────────
@bp.route('/partner/<slug>')
def dashboard(slug):
    user = _current_partner_user()
    if not user:
        return redirect(url_for('partner.login', slug=slug))

    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            session.pop('partner_user_id', None)
            return redirect(url_for('partner.login', slug=slug))

        # Enabled buckets for this dealer
        cur.execute("""
            SELECT bucket, enabled FROM partner_alert_config
            WHERE dealer_id = %s
        """, (dealer['id'],))
        enabled_buckets = {r['bucket']: r['enabled'] for r in cur.fetchall()}

        # Cars currently in any enabled age bucket. Age precedence: vAuto >
        # dealer source_added_at > scanner first_seen_at.
        effective_fs = ("COALESCE("
                        "i.verified_at - (i.verified_days_on_lot || ' days')::interval, "
                        "i.source_added_at, i.first_seen_at)")
        where_parts = []
        if enabled_buckets.get('30_60'):
            where_parts.append(f"({effective_fs} <= NOW() - INTERVAL '30 days' "
                               f"AND {effective_fs} > NOW() - INTERVAL '60 days')")
        if enabled_buckets.get('60_90'):
            where_parts.append(f"({effective_fs} <= NOW() - INTERVAL '60 days' "
                               f"AND {effective_fs} > NOW() - INTERVAL '90 days')")
        if enabled_buckets.get('90_plus'):
            where_parts.append(f"{effective_fs} <= NOW() - INTERVAL '90 days'")
        if enabled_buckets.get('price_drop'):
            where_parts.append("i.price_drop_amount IS NOT NULL")

        rows = []
        already_requested_vins = set()
        recent_responses = []
        if where_parts:
            cur.execute(f"""
                SELECT i.id, i.vin, i.year, i.make, i.model, i.trim,
                       i.mileage, i.price, i.url, i.photo_url, i.ext_color,
                       i.source_added_at, i.first_seen_at,
                       i.verified_at, i.verified_days_on_lot,
                       i.price_drop_amount, i.price_drop_at, i.last_price
                FROM dealer_inventory i
                WHERE i.dealer_id = %s
                  AND i.status = 'active'
                  AND ({' OR '.join(where_parts)})
                ORDER BY {effective_fs} ASC
            """, (dealer['id'],))
            rows = cur.fetchall()

            # VINs already in a submitted bid request so we can show status.
            # `ew_counter` is EW's offer back to the partner (bids.bid_amount),
            # NOT what the partner asked for (bids.asking_price, which is just
            # our storage of the target_price they typed in).
            cur.execute("""
                SELECT pbr.id AS pbr_id, pbr.vin, pbr.target_price,
                       pbr.partner_message, pbr.submitted_at, pbr.bid_id,
                       b.status AS bid_status,
                       b.bid_amount AS ew_counter,
                       b.bid_response AS ew_message
                FROM partner_bid_requests pbr
                LEFT JOIN bids b ON b.id = pbr.bid_id
                WHERE pbr.dealer_id = %s
                ORDER BY pbr.submitted_at DESC
            """, (dealer['id'],))
            already_requested_vins = {r['vin']: r for r in cur.fetchall()}

    return render_template('partner_dashboard.html',
                           user=user, dealer=dealer, slug=slug,
                           rows=rows, enabled_buckets=enabled_buckets,
                           already_requested_vins=already_requested_vins,
                           viewing_as_admin=session.get('partner_viewing_as_admin', False),
                           BUCKETS=BUCKETS)


# ── Auth routes ───────────────────────────────────────────────────────────
@bp.route('/partner/<slug>/login', methods=['GET', 'POST'])
def login(slug):
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        pw = request.form.get('password') or ''
        with _db() as conn, conn.cursor() as cur:
            dealer = _dealer_by_slug(cur, slug)
            if not dealer:
                flash('Unknown portal.', 'error')
                return render_template('partner_login.html', slug=slug)
            cur.execute("""
                SELECT * FROM partner_users
                WHERE dealer_id = %s AND LOWER(email) = %s
            """, (dealer['id'], email))
            u = cur.fetchone()
            if not u or not u['password_hash'] or not _check_password(pw, u['password_hash']):
                flash('Email or password is incorrect.', 'error')
                return render_template('partner_login.html', slug=slug)
            cur.execute("UPDATE partner_users SET last_login_at = NOW() WHERE id = %s",
                        (u['id'],))
            conn.commit()
            session['partner_user_id'] = u['id']
            _tg_alert(f'🔓 <b>{dealer["name"]}</b> partner logged in\n'
                      f'{u["full_name"] or "(no name)"} &lt;{u["email"]}&gt;')
            return redirect(url_for('partner.dashboard', slug=slug))
    return render_template('partner_login.html', slug=slug)


@bp.route('/partner/<slug>/logout')
def logout(slug):
    session.pop('partner_user_id', None)
    return redirect(url_for('partner.login', slug=slug))


@bp.route('/partner/invite/<token>', methods=['GET', 'POST'])
def accept_invite(token):
    """Land here from an invite email. GET shows set-password form.
    POST saves the hash + marks the invite used."""
    import re
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT pu.*, d.name AS dealer_name
            FROM partner_users pu
            JOIN dealers d ON d.id = pu.dealer_id
            WHERE pu.invite_token = %s AND pu.invite_used_at IS NULL
        """, (token,))
        u = cur.fetchone()
        if not u:
            # Look up the dealer even for expired/used tokens so we can
            # redirect to their login page instead of a dead-end error.
            cur.execute("""
                SELECT d.name FROM partner_users pu
                JOIN dealers d ON d.id = pu.dealer_id
                WHERE pu.invite_token = %s
                   OR pu.id IN (SELECT id FROM partner_users
                                WHERE invite_token IS NULL LIMIT 0)
                LIMIT 1
            """, (token,))
            row = cur.fetchone()
            slug = re.sub(r'[^a-z0-9]+', '', row['name'].lower())[:32] if row else 'txtcharlie'
            return render_template('partner_invite_invalid.html', slug=slug), 400
        if u['invite_sent_at'] and \
           u['invite_sent_at'] < datetime.now(timezone.utc) - timedelta(days=INVITE_TTL_DAYS):
            return render_template('partner_invite_invalid.html', expired=True), 400

        if request.method == 'POST':
            pw = request.form.get('password') or ''
            pw2 = request.form.get('password2') or ''
            if len(pw) < 8:
                flash('Password must be at least 8 characters.', 'error')
            elif pw != pw2:
                flash('Passwords do not match.', 'error')
            else:
                cur.execute("""
                    UPDATE partner_users
                    SET password_hash = %s, invite_used_at = NOW(),
                        invite_token = NULL
                    WHERE id = %s
                """, (_hash_password(pw), u['id']))
                conn.commit()
                session['partner_user_id'] = u['id']
                import re
                slug = re.sub(r'[^a-z0-9]+', '', u['dealer_name'].lower())[:32]
                _tg_alert(f'✅ <b>{u["dealer_name"]}</b> partner set their password\n'
                          f'{u["full_name"] or "(no name)"} &lt;{u["email"]}&gt;')
                # Welcome email with the portal URL — user's first-reference
                # bookmark. Skip if this is a password reset (they've been
                # here before and already have the link).
                portal_url = f"{PORTAL_BASE}/partner/{slug}"
                _send_email(u['email'],
                    f'Welcome to the {u["dealer_name"]} partner portal',
                    f'<p>Hi{(" " + u["full_name"]) if u["full_name"] else ""},</p>'
                    f'<p>Your password is set. Here is your portal link — '
                    f'<strong>bookmark it</strong> for easy access:</p>'
                    f'<p><a href="{portal_url}" '
                    f'style="display:inline-block;padding:12px 22px;background:#2563eb;'
                    f'color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">'
                    f'Open your dashboard</a></p>'
                    f'<p style="font-size:13px;color:#475569">Or paste: {portal_url}</p>'
                    f'<p style="font-size:13px;color:#475569">We\'ll also email you a daily digest '
                    f'at 5pm ET whenever a car in your inventory crosses an age threshold '
                    f'you\'ve asked us to watch.</p>'
                    f'<hr style="border:0;border-top:1px solid #cbd5e1;margin:24px 0">'
                    f'<p style="font-size:12px;color:#64748b">Sign in with your email + '
                    f'the password you just set. Forgot it? Use the '
                    f'<a href="{PORTAL_BASE}/partner/{slug}/forgot">reset link</a>.</p>')
                return redirect(url_for('partner.dashboard', slug=slug))

    return render_template('partner_set_password.html', user=u, token=token)


@bp.route('/partner/<slug>/forgot', methods=['GET', 'POST'])
def forgot_password(slug):
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        with _db() as conn, conn.cursor() as cur:
            dealer = _dealer_by_slug(cur, slug)
            if dealer:
                cur.execute("""
                    SELECT id FROM partner_users
                    WHERE dealer_id = %s AND LOWER(email) = %s
                """, (dealer['id'], email))
                u = cur.fetchone()
                if u:
                    tok = secrets.token_urlsafe(32)
                    cur.execute("""
                        UPDATE partner_users
                        SET reset_token = %s,
                            reset_expires = NOW() + INTERVAL '%s hours'
                        WHERE id = %s
                    """, (tok, RESET_TTL_HOURS, u['id']))
                    conn.commit()
                    reset_url = f"{PORTAL_BASE}/partner/reset/{tok}"
                    _send_email(email, 'Reset your partner-portal password',
                                f'<p>Click to reset: <a href="{reset_url}">{reset_url}</a></p>'
                                f'<p>Link expires in {RESET_TTL_HOURS} hours.</p>')
        # Always show success (avoid email enumeration)
        flash('If that email is registered, a reset link is on the way.', 'info')
        return redirect(url_for('partner.login', slug=slug))
    return render_template('partner_forgot.html', slug=slug)


@bp.route('/partner/reset/<token>', methods=['GET', 'POST'])
def reset_password(token):
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT pu.*, d.name AS dealer_name
            FROM partner_users pu
            JOIN dealers d ON d.id = pu.dealer_id
            WHERE pu.reset_token = %s AND pu.reset_expires > NOW()
        """, (token,))
        u = cur.fetchone()
        if not u:
            return render_template('partner_invite_invalid.html', expired=True), 400
        if request.method == 'POST':
            pw = request.form.get('password') or ''
            pw2 = request.form.get('password2') or ''
            if len(pw) < 8 or pw != pw2:
                flash('Passwords must match and be at least 8 characters.', 'error')
            else:
                cur.execute("""
                    UPDATE partner_users
                    SET password_hash = %s, reset_token = NULL, reset_expires = NULL
                    WHERE id = %s
                """, (_hash_password(pw), u['id']))
                conn.commit()
                import re
                slug = re.sub(r'[^a-z0-9]+', '', u['dealer_name'].lower())[:32]
                flash('Password updated. Please log in.', 'info')
                return redirect(url_for('partner.login', slug=slug))
    return render_template('partner_set_password.html', user=u, token=token, is_reset=True)


# ── Admin: "view as dealer" impersonation ─────────────────────────────────
@bp.route('/admin/partner/<int:dealer_id>/view-as')
def admin_view_as(dealer_id):
    """EW admin clicks a link → gets logged into that dealer's portal as the
    first registered user on that dealer. Requires EW admin auth (the
    before_request hook in app.py gates /admin/* on session['logged_in']).
    Sets a flag so the dashboard banner shows 'Viewing as admin'."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT pu.id, d.name AS dealer_name
            FROM partner_users pu
            JOIN dealers d ON d.id = pu.dealer_id
            WHERE pu.dealer_id = %s AND pu.password_hash IS NOT NULL
            ORDER BY pu.created_at ASC LIMIT 1
        """, (dealer_id,))
        row = cur.fetchone()
        if not row:
            return f'No partner users have set a password for dealer {dealer_id}.', 404
        import re
        slug = re.sub(r'[^a-z0-9]+', '', row['dealer_name'].lower())[:32]

    session['partner_user_id'] = row['id']
    session['partner_viewing_as_admin'] = True
    return redirect(url_for('partner.dashboard', slug=slug))


@bp.route('/partner/<slug>/exit-view-as')
def exit_view_as(slug):
    """Clear the impersonation — used by the banner 'exit' link. Returns
    the admin to the EW admin config page for that dealer."""
    session.pop('partner_user_id', None)
    session.pop('partner_viewing_as_admin', None)
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if dealer:
            return redirect(f'/admin/partner/{dealer["id"]}/config')
    return redirect('/')


# ── Admin: invite a partner + toggle alerts ───────────────────────────────
@bp.route('/admin/partner/<int:dealer_id>/invite', methods=['POST'])
def admin_invite(dealer_id):
    """Admin creates or re-sends a partner user invite. Form fields:
       email, full_name. If a partner_user already exists for that email,
       we re-issue the invite token."""
    email = (request.form.get('email') or '').strip().lower()
    full_name = (request.form.get('full_name') or '').strip()
    if not email:
        return 'email required', 400

    tok = secrets.token_urlsafe(32)
    with _db() as conn, conn.cursor() as cur:
        # Upsert partner user
        cur.execute("""
            INSERT INTO partner_users
                (dealer_id, email, full_name, invite_token, invite_sent_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (email) DO UPDATE
              SET invite_token = EXCLUDED.invite_token,
                  invite_sent_at = NOW(),
                  full_name = COALESCE(EXCLUDED.full_name, partner_users.full_name),
                  dealer_id = EXCLUDED.dealer_id,
                  invite_used_at = NULL,
                  password_hash = NULL
            RETURNING id
        """, (dealer_id, email, full_name or None, tok))

        # Pre-seed default alert config (all enabled) if missing
        for bucket_key, _ in BUCKETS:
            cur.execute("""
                INSERT INTO partner_alert_config (dealer_id, bucket, enabled)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (dealer_id, bucket) DO NOTHING
            """, (dealer_id, bucket_key))
        conn.commit()

        cur.execute("SELECT name FROM dealers WHERE id = %s", (dealer_id,))
        dealer_name = cur.fetchone()['name']

    invite_url = f"{PORTAL_BASE}/partner/invite/{tok}"
    email_sent = _send_email(email,
                f'Invitation to the {dealer_name} aged-inventory portal',
                f'<p>Hello{(" " + full_name) if full_name else ""},</p>'
                f'<p>You have been invited to the <strong>{dealer_name}</strong> partner portal '
                f'on Experience Wholesale. This is where you will see your aged '
                f'inventory and request wholesale bids from our team.</p>'
                f'<p><a href="{invite_url}" '
                f'style="display:inline-block;padding:10px 18px;background:#2563eb;'
                f'color:#fff;text-decoration:none;border-radius:6px;">Set your password</a></p>'
                f'<p style="color:#64748b;font-size:12px">Link expires in {INVITE_TTL_DAYS} days.</p>'
                f'<p style="color:#64748b;font-size:12px">If the button does not work, paste this URL: {invite_url}</p>')
    return jsonify({'ok': True, 'email': email, 'invite_url': invite_url,
                    'email_sent': bool(email_sent)})


@bp.route('/admin/partner/user/<int:user_id>/update', methods=['POST'])
def admin_update_partner_user(user_id):
    """Admin-only: change a partner user's email + full name, OR delete them.
    Used when an admin needs to swap Oscar's test email for a real dealer
    address, fix a typo, or remove a stale test account."""
    action = request.form.get('action') or 'update'
    with _db() as conn, conn.cursor() as cur:
        if action == 'delete':
            cur.execute("DELETE FROM partner_users WHERE id = %s RETURNING dealer_id", (user_id,))
            row = cur.fetchone()
            conn.commit()
            if not row:
                return jsonify({'error': 'not_found'}), 404
            return jsonify({'ok': True, 'deleted': True, 'dealer_id': row['dealer_id']})
        email = (request.form.get('email') or '').strip().lower()
        full_name = (request.form.get('full_name') or '').strip() or None
        if not email or '@' not in email:
            return jsonify({'error': 'bad_email'}), 400
        try:
            cur.execute("""
                UPDATE partner_users
                SET email = %s, full_name = %s
                WHERE id = %s
                RETURNING dealer_id, email
            """, (email, full_name, user_id))
            row = cur.fetchone()
            conn.commit()
        except Exception as e:
            # Usually a unique-constraint violation (email already in use)
            conn.rollback()
            return jsonify({'error': 'email_in_use_or_invalid', 'detail': str(e)[:200]}), 409
        if not row:
            return jsonify({'error': 'not_found'}), 404
        return jsonify({'ok': True, 'email': row['email'], 'dealer_id': row['dealer_id']})


@bp.route('/admin/partner/<int:dealer_id>/config', methods=['GET', 'POST'])
def admin_config(dealer_id):
    """Toggle which alert buckets are enabled for this dealer."""
    if request.method == 'POST':
        with _db() as conn, conn.cursor() as cur:
            for bucket_key, _ in BUCKETS:
                enabled = request.form.get(f'enabled_{bucket_key}') == 'on'
                cur.execute("""
                    INSERT INTO partner_alert_config (dealer_id, bucket, enabled)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (dealer_id, bucket) DO UPDATE
                      SET enabled = EXCLUDED.enabled, updated_at = NOW()
                """, (dealer_id, bucket_key, enabled))
            conn.commit()
        flash('Alert preferences saved.', 'info')

    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM dealers WHERE id = %s", (dealer_id,))
        dealer = cur.fetchone()
        cur.execute("""
            SELECT bucket, enabled FROM partner_alert_config
            WHERE dealer_id = %s
        """, (dealer_id,))
        cfg = {r['bucket']: r['enabled'] for r in cur.fetchall()}
        cur.execute("""
            SELECT id, email, full_name, invite_sent_at, invite_used_at, last_login_at
            FROM partner_users WHERE dealer_id = %s ORDER BY created_at
        """, (dealer_id,))
        users = cur.fetchall()

    return render_template('partner_admin_config.html',
                           dealer=dealer, cfg=cfg, users=users, BUCKETS=BUCKETS)


# ── SMS helper + phone normalizer ─────────────────────────────────────────
def _normalize_phone(raw: str) -> str | None:
    """Strip formatting, add +1 for 10-digit US numbers, bail on anything else."""
    if not raw:
        return None
    digits = ''.join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f'+1{digits}'
    if len(digits) == 11 and digits.startswith('1'):
        return f'+{digits}'
    if raw.startswith('+') and 10 <= len(digits) <= 15:
        return f'+{digits}'
    return None


def _send_sms(to_phone: str, body: str) -> bool:
    """Route through app.send_sms helper (Twilio). Returns True on success."""
    try:
        from app import send_sms
        return send_sms(to_phone, body)
    except Exception as e:
        print(f'[partner sms] failed: {e}')
        return False


# ── Thumb pre-warmer — avoid cold-fetch latency on first share-page view ─
def warm_bid_photo_thumbs(photo_ids: list[int]) -> None:
    """Background thread: hits /p/<id>/<size> for strip/mobile/full on each
    photo so the thumb cache is pre-populated. Called right after partner
    submits a bid — by the time anyone opens the bid detail or share link,
    thumbs are already on disk (63ms load vs 836ms cold-gen)."""
    if not photo_ids:
        return
    import threading
    def _worker(ids):
        import requests as _r
        base = 'http://127.0.0.1:9000'
        for pid in ids:
            for size in ('strip', 'mobile', 'full'):
                try:
                    _r.get(f'{base}/p/{pid}/{size}', timeout=30)
                except Exception:
                    pass
    threading.Thread(target=_worker, args=(list(photo_ids),), daemon=True).start()


# ── Telegram alert (Oscar's bot) ──────────────────────────────────────────
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '7985611488')


def _tg_alert(text: str) -> None:
    """Fire-and-forget Telegram message to Oscar. HTML formatting allowed.
    Never raises — partner-portal flow shouldn't break if Telegram is down."""
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        import requests as _r
        _r.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': text, 'parse_mode': 'HTML'},
                timeout=5)
    except Exception as e:
        print(f'[tg alert] failed: {e}')


# ── Notify partner on EW response (called from app.py bid-response route) ─
def notify_partner_of_ew_response(bid_id: int, send_email: bool = False, send_text: bool = False):
    """Called from the EW bid-response endpoint after bid_amount / bid_response
    is saved. Notifies partner user(s) via the channels the EW client picked
    on the Send Bid UI.

    Channel resolution:
      - Dashboard: always implicit (the partner's portal page is the canonical
        record; if a partner_bid_requests row exists it'll show there
        automatically, and EW back-bids appear in the same view since they
        share contact_id via _ensure_partner_contact).
      - send_email: client-checkbox + dealer's email_bid_alerts opt-in
        (BOTH must be true to send).
      - send_text:  client-checkbox + dealer's sms_opt_in + sms_verified_at
        (legal requirement — verified opt-in before any SMS).

    Works for both partner-portal bids (via partner_request_id) AND EW-pushed
    back-bids from Dealer DB Search (via partner_dealer_id directly on bids).
    Safe no-op for non-partner bids (no dealer found, returns silently).
    """
    try:
        with _db() as conn, conn.cursor() as cur:
            # Resolve dealer_id from either partner_request_id (legacy partner-
            # portal bids) or partner_dealer_id (new dealer-DB-push bids).
            cur.execute("""
                SELECT b.id, b.bid_amount, b.bid_response, b.vin, b.year, b.make,
                       b.model, b.trim, b.partner_request_id, b.partner_dealer_id,
                       COALESCE(b.partner_dealer_id,
                                (SELECT pbr.dealer_id FROM partner_bid_requests pbr
                                  WHERE pbr.id = b.partner_request_id)) AS dealer_id
                FROM bids b
                WHERE b.id = %s
            """, (bid_id,))
            b = cur.fetchone()
            if not b or not b.get('dealer_id'):
                return
            cur.execute("SELECT name FROM dealers WHERE id = %s", (b['dealer_id'],))
            d = cur.fetchone()
            if not d:
                return
            dealer_name = d['name']
            import re
            slug = re.sub(r'[^a-z0-9]+', '', dealer_name.lower())[:32]
            portal_url = f"{PORTAL_BASE}/partner/{slug}"
            car = f"{b['year'] or ''} {b['make'] or ''} {b['model'] or ''}".strip()

            cur.execute("""
                SELECT email, full_name, phone, sms_opt_in, sms_verified_at, email_bid_alerts
                FROM partner_users
                WHERE dealer_id = %s AND password_hash IS NOT NULL
            """, (b['dealer_id'],))
            for u in cur.fetchall():
                amount_line = (f'<p><strong>Our offer: '
                               f'${int(float(b["bid_amount"])):,}</strong></p>') \
                              if b['bid_amount'] else ''
                msg_line = (f'<p style="background:#f1f5f9;padding:12px 14px;'
                            f'border-radius:6px;margin:12px 0">{b["bid_response"]}</p>') \
                           if b['bid_response'] else ''
                # Email — only if client requested AND user opted in
                if send_email and u.get('email_bid_alerts'):
                    _send_email(u['email'],
                        f'Bid update: {car} (VIN {b["vin"][-6:] if b["vin"] else "—"})',
                        f'<p>Hi{(" " + u["full_name"]) if u["full_name"] else ""},</p>'
                        f'<p>Experience Wholesale has responded to your bid request '
                        f'on the <strong>{car}</strong>.</p>'
                        f'{amount_line}{msg_line}'
                        f'<p><a href="{portal_url}" '
                        f'style="display:inline-block;padding:11px 22px;background:#2563eb;'
                        f'color:#fff;text-decoration:none;border-radius:6px;font-weight:600">'
                        f'Open your dashboard</a></p>'
                        f'<p style="font-size:12px;color:#64748b">You can counter, accept, '
                        f'or decline from the portal.</p>')
                # SMS — only if client requested AND user opted in AND verified
                if send_text and u.get('sms_opt_in') and u.get('sms_verified_at') and u.get('phone'):
                    sms_body_parts = [f'EW responded on your {car}']
                    if b['bid_amount']:
                        sms_body_parts.append(f'Offer: ${int(float(b["bid_amount"])):,}')
                    sms_body_parts.append(f'Open: {portal_url}')
                    sms_body_parts.append('Reply STOP to unsubscribe.')
                    _send_sms(u['phone'], ' — '.join(sms_body_parts))
    except Exception as e:
        print(f'[partner notify] failed for bid {bid_id}: {e}')


# ── Submit bid requests (partner side) ────────────────────────────────────
def _ensure_partner_contact(cur, dealer_id: int, dealer_name: str) -> int:
    """One synthetic contact per partner dealer. All bids from a partner use
    this contact_id so they group together in the EW bid list. Phone uses
    a non-numeric sentinel (`PARTNER{id}`) so it can't collide with a real
    field-rep number but still satisfies NOT NULL."""
    phone = f'PARTNER{dealer_id}'
    cur.execute("""
        INSERT INTO contacts (phone, name, company)
        VALUES (%s, %s, %s)
        ON CONFLICT (phone) DO UPDATE
          SET name = EXCLUDED.name, company = EXCLUDED.company
        RETURNING id
    """, (phone, f'{dealer_name} (Partner Portal)', dealer_name))
    return cur.fetchone()['id']


@bp.route('/partner/<slug>/submit', methods=['POST'])
def submit_requests(slug):
    """Partner checked boxes + entered prices + hit submit.
    For each checked row we create:
      1. partner_bid_requests row (the audit trail of what they asked for)
      2. bids row (appears in EW bid list just like any other bid)
      3. bid_photos from dealer_inventory.photo_url
      4. bid_messages opening line: 'Bid request from {dealer}: need $X — {msg}'
    The bids row is linked back via bids.partner_request_id so the EW side
    knows it came from the portal and can route responses back."""
    user = _current_partner_user()
    if not user:
        return redirect(url_for('partner.login', slug=slug))

    count = 0
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            return 'Forbidden', 403

        contact_id = _ensure_partner_contact(cur, dealer['id'], dealer['name'])
        partner_phone = f'PARTNER{dealer["id"]}'

        # Pre-pass: collect all selected inv_ids + their prices so we can
        # reject the whole submission if any checked row is missing a price
        # (enforces the "price required" rule even if someone bypasses the
        # client-side JS).
        selections = []
        for key in request.form:
            if not key.startswith('select_'):
                continue
            inv_id = key.replace('select_', '')
            try:
                inv_id = int(inv_id)
            except ValueError:
                continue
            price = request.form.get(f'price_{inv_id}') or ''
            try:
                price_val = float(price.replace(',', '').replace('$', '')) if price else None
            except ValueError:
                price_val = None
            message = (request.form.get(f'message_{inv_id}') or '').strip()
            selections.append((inv_id, price_val, message))

        # Reject if any checked row has no valid positive price
        missing = [s for s in selections if not s[1] or s[1] <= 0]
        if missing:
            flash(f'Please enter a price for every car you selected '
                  f'({len(missing)} missing). Your submission was not sent.',
                  'error')
            return redirect(url_for('partner.dashboard', slug=slug))

        for inv_id, price_val, message in selections:

            # Pull vehicle details from dealer_inventory
            cur.execute("""
                SELECT id, vin, year, make, model, trim, mileage, ext_color,
                       price, photo_url, photos, url
                FROM dealer_inventory
                WHERE id = %s AND dealer_id = %s
            """, (inv_id, dealer['id']))
            inv = cur.fetchone()
            if not inv:
                continue

            # 1. Insert the partner_bid_requests audit row
            cur.execute("""
                INSERT INTO partner_bid_requests
                    (dealer_id, partner_user_id, inventory_id, vin,
                     target_price, partner_message)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (dealer['id'], user['id'], inv_id, inv['vin'],
                  price_val, message or None))
            pbr_id = cur.fetchone()['id']

            # 2. Create the EW bid
            raw_message = f'Bid request from {dealer["name"]}'
            if price_val:
                raw_message += f': need ${int(price_val):,}'
            if message:
                raw_message += f' — {message}'
            notes = f'[Partner Portal · {dealer["name"]} · requested by {user["email"]}]'
            cur.execute("""
                INSERT INTO bids (contact_id, phone, vin,
                                  year, make, model, trim, mileage, color,
                                  raw_message, asking_price, notes,
                                  status, has_unread, partner_request_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'new', TRUE, %s)
                RETURNING id
            """, (contact_id, partner_phone, inv['vin'],
                  inv['year'], inv['make'], inv['model'], inv['trim'],
                  inv['mileage'], inv['ext_color'],
                  raw_message, price_val, notes, pbr_id))
            bid_id = cur.fetchone()['id']

            # 3. Link bid back to partner_bid_requests
            cur.execute("UPDATE partner_bid_requests SET bid_id = %s WHERE id = %s",
                        (bid_id, pbr_id))

            # 4. Copy every scraped photo into bid_photos. dealer_scanner
            # stores the full gallery in dealer_inventory.photos (JSONB array).
            # Fall back to the single photo_url if the gallery is empty.
            all_photos = []
            if inv.get('photos'):
                raw = inv['photos']
                if isinstance(raw, list):
                    all_photos = [p for p in raw if isinstance(p, str) and p.startswith('http')]
            if not all_photos and inv.get('photo_url'):
                all_photos = [inv['photo_url']]
            # Filter out dealer logos / badges / icons — those routinely
            # slip into scraped galleries and end up as photo #1.
            _JUNK = ('logo', 'badge', 'icon', 'placeholder', '/cms/', '/ui/',
                     'facebook', 'instagram', 'twitter', 'youtube')
            all_photos = [p for p in all_photos
                          if not any(j in p.lower() for j in _JUNK)]
            # Dedupe while preserving order
            seen = set()
            all_photos = [p for p in all_photos if not (p in seen or seen.add(p))]
            # Cap at 10 — TXT Charlie posts 25+ per car but EW bid UI doesn't
            # need a full gallery. First 10 cover exterior + key interior.
            new_photo_ids = []
            for url in all_photos[:10]:
                cur.execute("""
                    INSERT INTO bid_photos (bid_id, url) VALUES (%s, %s)
                    RETURNING id
                """, (bid_id, url))
                new_photo_ids.append(cur.fetchone()['id'])

            # 5. Seed the message thread with the partner's opening line
            cur.execute("""
                INSERT INTO bid_messages (bid_id, direction, message,
                                          from_phone, to_phone)
                VALUES (%s, 'inbound', %s, %s, %s)
            """, (bid_id, raw_message, partner_phone, 'EW'))

            # 6. Pre-warm thumbnail cache for the new photos in the background.
            # By the time anyone opens this bid in EW or sends the share
            # link, every size (strip/mobile/full) is already on disk.
            warm_bid_photo_thumbs(new_photo_ids)

            count += 1
        conn.commit()

    flash(f'{count} car{"s" if count != 1 else ""} submitted — our team will respond shortly.',
          'info')
    if count:
        _tg_alert(f'💰 <b>{dealer["name"]}</b> submitted {count} bid request'
                  f'{"s" if count != 1 else ""}\n'
                  f'{user["full_name"] or user["email"]}')
    return redirect(url_for('partner.dashboard', slug=slug))


# ── Partner counters EW's offer ───────────────────────────────────────────
@bp.route('/partner/<slug>/counter', methods=['POST'])
def counter_offer(slug):
    """Partner hit 'Counter' after EW responded. Append inbound message +
    flag the bid as unread on EW side so the yellow star lights up."""
    user = _current_partner_user()
    if not user:
        return jsonify({'error': 'not_authenticated'}), 401
    try:
        pbr_id = int(request.form.get('pbr_id') or 0)
    except ValueError:
        return jsonify({'error': 'bad_pbr_id'}), 400
    counter_raw = (request.form.get('counter_price') or '').strip()
    counter_msg = (request.form.get('counter_message') or '').strip()
    try:
        counter_price = float(counter_raw.replace(',', '').replace('$', '')) if counter_raw else None
    except ValueError:
        counter_price = None

    # Reject empty submissions outright — the old buggy flow produced
    # "Counter from TXT Charlie" ghost rows when a double-click fired with
    # empty form. A counter must have at least a price or a message.
    if counter_price is None and not counter_msg:
        return jsonify({'error': 'empty'}), 400

    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT pbr.id, pbr.bid_id, pbr.dealer_id, d.name AS dealer_name,
                   b.phone AS partner_phone
            FROM partner_bid_requests pbr
            JOIN dealers d ON d.id = pbr.dealer_id
            LEFT JOIN bids b ON b.id = pbr.bid_id
            WHERE pbr.id = %s
        """, (pbr_id,))
        row = cur.fetchone()
        if not row or row['dealer_id'] != user['dealer_id'] or not row['bid_id']:
            return jsonify({'error': 'forbidden'}), 403

        # Build the message body
        body_parts = []
        if counter_price:
            body_parts.append(f'Counter from {row["dealer_name"]}: ${int(counter_price):,}')
        if counter_msg:
            body_parts.append(counter_msg)
        body = ' — '.join(body_parts) or f'Counter from {row["dealer_name"]}'

        # Append to thread + flag bid as unread + bump asking_price so the
        # listing reflects the new ask
        cur.execute("""
            INSERT INTO bid_messages (bid_id, direction, message, from_phone, to_phone)
            VALUES (%s, 'inbound', %s, %s, 'EW')
        """, (row['bid_id'], body, row['partner_phone']))
        cur.execute("""
            UPDATE bids SET has_unread = TRUE, status = 'new',
                            asking_price = COALESCE(%s, asking_price),
                            updated_at = NOW()
            WHERE id = %s
        """, (counter_price, row['bid_id']))
        # Partner is re-engaging with this row — clear their seen-response marker
        cur.execute("""
            UPDATE partner_bid_requests
            SET partner_seen_response_at = NOW()
            WHERE id = %s
        """, (pbr_id,))
        conn.commit()

    _tg_alert(f'↩️ <b>{row["dealer_name"]}</b> countered on Bid #{row["bid_id"]}\n'
              f'{body}')
    return jsonify({'ok': True})


# ── Dashboard poll endpoint for chime / gold star ─────────────────────────
@bp.route('/partner/<slug>/poll')
def poll_unread(slug):
    """Lightweight JSON the dashboard polls every 15s. Returns:
       - responses: [{pbr_id, vin, amount, msg}] — EW replies newer than seen
       - total_displayable: count of cars that should appear on the dashboard
         right now (uses same bucket filter as the main view). JS compares
         against its initial count to show a 'new inventory' toast."""
    user = _current_partner_user()
    if not user:
        return jsonify({'error': 'not_authenticated'}), 401
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            return jsonify({'error': 'forbidden'}), 403

        # Unread EW responses
        cur.execute("""
            SELECT pbr.id AS pbr_id, pbr.vin, b.updated_at,
                   b.bid_amount, b.bid_response
            FROM partner_bid_requests pbr
            JOIN bids b ON b.id = pbr.bid_id
            WHERE pbr.dealer_id = %s
              AND (b.bid_amount IS NOT NULL OR b.bid_response IS NOT NULL)
              AND (pbr.partner_seen_response_at IS NULL
                   OR b.updated_at > pbr.partner_seen_response_at)
            ORDER BY b.updated_at DESC
        """, (dealer['id'],))
        rows = cur.fetchall()

        # Displayable car count (same logic as dashboard render)
        cur.execute("""
            SELECT bucket, enabled FROM partner_alert_config
            WHERE dealer_id = %s
        """, (dealer['id'],))
        enabled = {r['bucket']: r['enabled'] for r in cur.fetchall()}
        effective_fs = ("COALESCE("
                        "verified_at - (verified_days_on_lot || ' days')::interval, "
                        "source_added_at, first_seen_at)")
        where_parts = []
        if enabled.get('30_60'):
            where_parts.append(f"({effective_fs} <= NOW() - INTERVAL '30 days' "
                               f"AND {effective_fs} > NOW() - INTERVAL '60 days')")
        if enabled.get('60_90'):
            where_parts.append(f"({effective_fs} <= NOW() - INTERVAL '60 days' "
                               f"AND {effective_fs} > NOW() - INTERVAL '90 days')")
        if enabled.get('90_plus'):
            where_parts.append(f"{effective_fs} <= NOW() - INTERVAL '90 days'")
        if enabled.get('price_drop'):
            where_parts.append("price_drop_amount IS NOT NULL")
        total = 0
        if where_parts:
            cur.execute(f"""
                SELECT COUNT(*) AS c
                FROM dealer_inventory
                WHERE dealer_id = %s AND status = 'active'
                  AND ({' OR '.join(where_parts)})
            """, (dealer['id'],))
            total = cur.fetchone()['c']

    return jsonify({
        'unread': len(rows),
        'total_displayable': total,
        'responses': [{
            'pbr_id': r['pbr_id'], 'vin': r['vin'],
            'amount': float(r['bid_amount']) if r['bid_amount'] else None,
            'msg': r['bid_response'],
        } for r in rows],
    })


# ── SMS settings: enroll + verify + opt-out ───────────────────────────────
@bp.route('/partner/<slug>/settings', methods=['GET', 'POST'])
def settings_page(slug):
    """Partner self-serve: phone + SMS opt-in. Two-step verify via 6-digit
    code sent over SMS. Opt-in only flips TRUE after successful code entry —
    that's the Twilio-documented consent trail (user entered the phone,
    received code, typed code back into OUR form = meaningful consent)."""
    user = _current_partner_user()
    if not user:
        return redirect(url_for('partner.login', slug=slug))
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            return redirect(url_for('partner.login', slug=slug))

        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'send_code':
                raw_phone = request.form.get('phone') or ''
                phone = _normalize_phone(raw_phone)
                if not phone:
                    flash('Please enter a valid US phone number (10 digits).', 'error')
                else:
                    code = f'{secrets.randbelow(1_000_000):06d}'
                    cur.execute("""
                        UPDATE partner_users
                        SET phone = %s, sms_verify_code = %s,
                            sms_verify_expires = NOW() + INTERVAL '10 minutes',
                            sms_opt_in = FALSE, sms_verified_at = NULL
                        WHERE id = %s
                    """, (phone, code, user['id']))
                    conn.commit()
                    ok = _send_sms(phone,
                        f'Experience Wholesale verification code: {code}. '
                        f'Reply with this to enable SMS bid alerts. '
                        f'Reply STOP to stop.')
                    if ok:
                        flash(f'Code sent to {phone}. Enter it below.', 'info')
                    else:
                        flash('Could not send SMS. Double-check the number.', 'error')
            elif action == 'verify_code':
                typed = (request.form.get('code') or '').strip()
                cur.execute("""
                    SELECT sms_verify_code, sms_verify_expires, phone
                    FROM partner_users WHERE id = %s
                """, (user['id'],))
                row = cur.fetchone()
                if not row or not row['sms_verify_code']:
                    flash('No code on file. Send yourself a new one.', 'error')
                elif row['sms_verify_expires'] and row['sms_verify_expires'] < datetime.now(timezone.utc):
                    flash('That code expired. Send a new one.', 'error')
                elif typed != row['sms_verify_code']:
                    flash('Wrong code. Check the SMS and try again.', 'error')
                else:
                    cur.execute("""
                        UPDATE partner_users
                        SET sms_opt_in = TRUE, sms_verified_at = NOW(),
                            sms_verify_code = NULL, sms_verify_expires = NULL
                        WHERE id = %s
                    """, (user['id'],))
                    conn.commit()
                    # Confirmation SMS per Twilio best practice
                    _send_sms(row['phone'],
                        'You are subscribed to Experience Wholesale bid alerts. '
                        'Reply STOP to unsubscribe.')
                    flash('SMS alerts enabled — you are all set.', 'info')
            elif action == 'opt_out':
                cur.execute("""
                    UPDATE partner_users
                    SET sms_opt_in = FALSE, sms_verified_at = NULL
                    WHERE id = %s
                """, (user['id'],))
                conn.commit()
                flash('SMS alerts disabled.', 'info')
            elif action == 'toggle_email':
                enabled = request.form.get('email_bid_alerts') == 'on'
                cur.execute("""
                    UPDATE partner_users SET email_bid_alerts = %s WHERE id = %s
                """, (enabled, user['id']))
                conn.commit()
                flash('Email preferences saved.', 'info')

        # Refetch latest state
        cur.execute("SELECT * FROM partner_users WHERE id = %s", (user['id'],))
        user = cur.fetchone()

    return render_template('partner_settings.html',
                           user=user, dealer=dealer, slug=slug)


@bp.route('/partner/<slug>/mark-seen', methods=['POST'])
def mark_seen(slug):
    """Mark all EW responses for this dealer as seen by the partner — stops
    the chime from firing again on the same responses."""
    user = _current_partner_user()
    if not user:
        return jsonify({'error': 'not_authenticated'}), 401
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            return jsonify({'error': 'forbidden'}), 403
        cur.execute("""
            UPDATE partner_bid_requests
            SET partner_seen_response_at = NOW()
            WHERE dealer_id = %s
        """, (dealer['id'],))
        conn.commit()
    return jsonify({'ok': True})
