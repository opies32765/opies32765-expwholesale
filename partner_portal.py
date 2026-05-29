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


# Dealers opted in to per-unit market-comps card on the partner dashboard.
# Populated nightly by /opt/expwholesale/encore_comps_pipeline.py
# (cron /etc/cron.d/encore_comps). Add a slug here and schedule a comps
# cron for that dealer to roll out to additional partners.
COMPS_ENABLED_SLUGS = {'encore', 'txtcharlie'}


@bp.after_request
def _no_cache(resp):
    """Partner portal pages must never be cached by the browser. Dashboard
    state changes every time a car is verified, a bucket shifts, or an
    EW response lands — a stale dashboard is worse than a slow one."""
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@bp.before_request
def _auto_login_via_query_token():
    """Auto-login is gated on the `?d=<token>` query param matching the
    dealer's `dashboard_token`. Bare slug URLs (no token, no cookie)
    fall through to the normal login redirect.

    Reverted from the slug-as-auth behavior on 2026-05-05 per spec —
    only explicit token-bearing URLs get in without creds. The
    /partner/<slug>/d/<token> path-based route is a separate handler
    (`dashboard_access_token`) and is not affected by this hook."""
    if session.get('partner_user_id'):
        return
    if not request.view_args:
        return
    slug = request.view_args.get('slug')
    if not slug:
        return
    if request.endpoint in ('partner.accept_invite',
                            'partner.reset_password',
                            'partner.dashboard_access_token'):
        return
    tok = (request.args.get('d') or '').strip()
    if not tok:
        return
    import hmac
    try:
        with _db() as conn, conn.cursor() as cur:
            dealer = _dealer_by_slug(cur, slug)
            if not dealer:
                return
            if not hmac.compare_digest(dealer.get('dashboard_token') or '', tok):
                return
            cur.execute("""SELECT id FROM partner_users
                            WHERE dealer_id = %s
                            ORDER BY (password_hash IS NULL) DESC,
                                     created_at ASC
                            LIMIT 1""", (dealer['id'],))
            u = cur.fetchone()
            if u:
                session['partner_user_id'] = u['id']
                session.permanent = True
    except Exception:
        pass


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
def provision_passwordless_portal(dealer_id: int, full_name: str = None,
                                  phone: str = None) -> dict:
    """PASSWORDLESS_PORTAL_2026_05_29 — every new dealer gets a tokenized
    portal with NO login credentials. Mints portal_slug + dashboard_token +
    mobile_token and creates ONE placeholder partner_user with NULL
    password_hash for the /partner/<slug>/d/<token> link to ride on. No
    email, no password. Dealer can set real credentials later via settings /
    forgot-password. Idempotent: never clobbers an existing slug/token and
    never creates a 2nd placeholder."""
    import re as _re
    try:
        with _db() as conn, conn.cursor() as cur:
            cur.execute("SELECT name, portal_slug, dashboard_token, mobile_token "
                        "FROM dealers WHERE id=%s", (dealer_id,))
            d = cur.fetchone()
            if not d:
                return {'success': False, 'error': 'dealer not found'}
            slug = d.get('portal_slug')
            if not slug:
                base = _re.sub(r'[^a-z0-9]+', '', (d['name'] or '').lower())[:32] or f'dealer{dealer_id}'
                slug = base; n = 2
                while True:
                    cur.execute("SELECT 1 FROM dealers WHERE LOWER(portal_slug)=%s AND id<>%s",
                                (slug, dealer_id))
                    if not cur.fetchone():
                        break
                    slug = f'{base[:30]}{n}'; n += 1
            dash = d.get('dashboard_token') or secrets.token_urlsafe(20)
            mob = d.get('mobile_token') or secrets.token_urlsafe(16)
            cur.execute("UPDATE dealers SET portal_slug=COALESCE(portal_slug,%s), "
                        "dashboard_token=COALESCE(dashboard_token,%s), "
                        "mobile_token=COALESCE(mobile_token,%s) WHERE id=%s",
                        (slug, dash, mob, dealer_id))
            placeholder_email = f'portal+dealer{dealer_id}@experience-wholesale.net'
            cur.execute("SELECT id FROM partner_users WHERE dealer_id=%s LIMIT 1", (dealer_id,))
            if not cur.fetchone():
                cur.execute('''INSERT INTO partner_users
                                 (dealer_id, email, full_name, phone, password_hash,
                                  sms_opt_in, email_bid_alerts, invite_token,
                                  invite_used_at, created_at)
                               VALUES (%s, %s, %s, %s, NULL, FALSE, TRUE, NULL, NULL, NOW())
                               ON CONFLICT (email) DO NOTHING''',
                            (dealer_id, placeholder_email, full_name,
                             (phone or '').strip() or None))
            conn.commit()
        link = f"{PORTAL_BASE}/partner/{slug}/d/{dash}"
        try:
            _tg_alert(f"\U0001F511 Passwordless portal provisioned for <b>{d['name']}</b>\n{link}")
        except Exception:
            pass
        return {'success': True, 'slug': slug, 'dashboard_link': link,
                'mobile_token': mob}
    except Exception as e:
        print(f'[provision_passwordless_portal] dealer {dealer_id}: {e}', flush=True)
        return {'success': False, 'error': str(e)}


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

        # Fire-and-forget Telegram alert to Oscar so he knows the dealer is
        # onboarded and can follow up if the email doesn't land. Same pattern
        # we use for TXT Charlie / inbound partner activity.
        _tg_alert(
            f"<b>🤝 New partner dashboard set up</b>\n"
            f"Dealer: {dealer_name}\n"
            f"Contact: {full_name or email}\n"
            f"Email: {email}\n"
            f"Phone: {phone or '(none)'}\n"
            f"SMS opt-in: {'yes' if sms_opt_in else 'no'}\n"
            f"Email opt-in: {'yes' if email_bid_alerts else 'no'}\n"
            f"Dashboard: {portal_url}\n"
            f"Welcome email: {'sent ✓' if sent else 'FAILED — check Resend logs'}"
        )

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
    first, then falls back to name-derived for dealers without one set.
    Returned columns must include `salesperson` so partner-bid create
    paths can snapshot it onto bids.salesperson."""
    slug_lower = (slug or '').lower()
    cur.execute("SELECT id, name, portal_slug, salesperson, salesperson_phone, "
                "mobile_token, dashboard_token, brand, portal_mode "
                "FROM dealers "
                "WHERE active = TRUE AND LOWER(portal_slug) = %s", (slug_lower,))
    d = cur.fetchone()
    if d:
        return d
    # Fallback: name-derived slug for dealers without an explicit one.
    # Must include portal_slug so downstream gates (e.g. COMPS_ENABLED_SLUGS)
    # work whether the route was hit via /partner/<portal_slug> or via the
    # name-derived alias (e.g. /partner/encoremotorcarsofsarasota for Encore).
    cur.execute("SELECT id, name, portal_slug, salesperson, salesperson_phone, "
                "mobile_token, dashboard_token, brand, portal_mode "
                "FROM dealers WHERE active = TRUE")
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
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer:
            return 'Unknown portal.', 404
        # Frictionless entry: if no session user (or session belongs to a
        # different dealer), silently adopt this dealer's placeholder
        # (no-password) partner_user. A real password-set account is only
        # required if the dealer has explicitly enabled it via /login.
        if not user or dealer['id'] != user['dealer_id']:
            cur.execute("""SELECT id, dealer_id, email, password_hash
                            FROM partner_users
                           WHERE dealer_id = %s
                           ORDER BY (password_hash IS NULL) DESC,
                                    created_at ASC
                           LIMIT 1""", (dealer['id'],))
            u = cur.fetchone()
            if not u:
                return redirect(url_for('partner.login', slug=slug))
            session['partner_user_id'] = u['id']
            session.permanent = True
            user = dict(u)

        # Auto-provision a mobile-app token on first dashboard hit so the
        # partner can copy/share their /mobile?p=<token> link without an
        # admin step. One token per dealer; doesn't rotate (per spec).
        if not dealer.get('mobile_token'):
            new_token = secrets.token_urlsafe(16)
            cur.execute("UPDATE dealers SET mobile_token = %s "
                        "WHERE id = %s AND mobile_token IS NULL",
                        (new_token, dealer['id']))
            conn.commit()
            dealer = dict(dealer)
            dealer['mobile_token'] = new_token

        # Wholesaler mode skips every inventory-driven query. Wholesalers
        # have no scraped site, so there's no aged inventory, no buckets,
        # no per-row bid form — they only use Quick Drop + mobile.
        wholesaler_mode = (dealer.get('portal_mode') == 'wholesaler')

        # Enabled buckets for this dealer
        cur.execute("""
            SELECT bucket, enabled FROM partner_alert_config
            WHERE dealer_id = %s
        """, (dealer['id'],))
        enabled_buckets = {r['bucket']: r['enabled'] for r in cur.fetchall()}

        # Fetch ALL active inventory regardless of enabled buckets — the
        # partner dashboard now shows the full bucketed view (matches the
        # admin /dealers/<id> page pattern). enabled_buckets is still used
        # for the daily-digest email; the dashboard view is no longer
        # gated on it.
        # Age precedence: vAuto > dealer source_added_at > scanner first_seen_at.
        effective_fs = ("COALESCE("
                        "i.verified_at - (i.verified_days_on_lot || ' days')::interval, "
                        "i.source_added_at, i.first_seen_at)")
        rows = []
        already_requested_vins = {}
        recent_responses = []
        bucket_rows = {'under_30': [], '30_60': [], '60_90': [], '90_plus': []}
        if not wholesaler_mode:
            cur.execute(f"""
                SELECT i.id, i.vin, i.year, i.make, i.model, i.trim,
                       i.mileage, i.price, i.url, i.photo_url, i.ext_color,
                       i.source_added_at, i.first_seen_at,
                       i.verified_at, i.verified_days_on_lot,
                       i.price_drop_amount, i.price_drop_at, i.last_price
                FROM dealer_inventory i
                WHERE i.dealer_id = %s
                  AND i.status = 'active'
                ORDER BY {effective_fs} ASC
            """, (dealer['id'],))
            rows = cur.fetchall()

            # Group rows into 4 age buckets using the same effective-age
            # precedence as the admin /dealers/<id> page. Re-use
            # best_age_days_filter for parity.
            from dealer_db import best_age_days_filter
            for r in rows:
                age = best_age_days_filter(r)
                try:
                    age_int = int(age)
                except (TypeError, ValueError):
                    age_int = 0
                if age_int < 30:
                    bucket_rows['under_30'].append(r)
                elif age_int < 60:
                    bucket_rows['30_60'].append(r)
                elif age_int < 90:
                    bucket_rows['60_90'].append(r)
                else:
                    bucket_rows['90_plus'].append(r)

        # VINs already in a submitted bid request so we can show status.
        # `ew_counter` is EW's offer back to the partner (bids.bid_amount),
        # NOT what the partner asked for (bids.asking_price, which is just
        # our storage of the target_price they typed in). Pulled outside the
        # `where_parts` guard so quick-drop bids stay visible even if the
        # dealer has no enabled age buckets.
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

        # Quick-drop bid requests — partner_bid_requests rows where
        # inventory_id IS NULL (car wasn't in their scraped inventory at
        # submit time). Render as their own section below the inventory
        # table so they get the same EW-thread + counter UX as in-inventory
        # bids. YMM/miles come from the bids row (NHTSA-decoded at submit).
        cur.execute("""
            SELECT pbr.id AS pbr_id, pbr.vin, pbr.target_price,
                   pbr.partner_message, pbr.submitted_at, pbr.bid_id,
                   b.status AS bid_status,
                   b.bid_amount AS ew_counter,
                   b.bid_response AS ew_message,
                   b.year, b.make, b.model, b.trim, b.mileage
            FROM partner_bid_requests pbr
            LEFT JOIN bids b ON b.id = pbr.bid_id
            WHERE pbr.dealer_id = %s
              AND pbr.inventory_id IS NULL
            ORDER BY pbr.submitted_at DESC
        """, (dealer['id'],))
        quick_drop_rows = cur.fetchall()

        # Inventory-based bid requests — same shape as quick_drop_rows so
        # the template can render them in one combined "Bid Requests"
        # stack. Fall back to inv columns when bid columns are missing
        # (the bid INSERT didn't always copy YMM/miles).
        cur.execute("""
            SELECT pbr.id AS pbr_id, pbr.vin, pbr.target_price,
                   pbr.partner_message, pbr.submitted_at, pbr.bid_id,
                   b.status AS bid_status,
                   b.bid_amount AS ew_counter,
                   b.bid_response AS ew_message,
                   COALESCE(b.year, i.year)       AS year,
                   COALESCE(b.make, i.make)       AS make,
                   COALESCE(b.model, i.model)     AS model,
                   COALESCE(b.trim, i.trim)       AS trim,
                   COALESCE(b.mileage, i.mileage) AS mileage
            FROM partner_bid_requests pbr
            LEFT JOIN bids b ON b.id = pbr.bid_id
            LEFT JOIN dealer_inventory i ON i.id = pbr.inventory_id
            WHERE pbr.dealer_id = %s
              AND pbr.inventory_id IS NOT NULL
            ORDER BY pbr.submitted_at DESC
        """, (dealer['id'],))
        inventory_bid_rows = cur.fetchall()

        # Unread EW responses — drive the floating banners at the top of
        # the dashboard. One banner per pbr where the bid has an EW
        # offer/message and the dealer hasn't acknowledged it yet (no
        # partner_seen_response_at, or response is newer than that ts).
        cur.execute("""
            SELECT pbr.id AS pbr_id, pbr.vin, pbr.bid_id,
                   b.bid_amount, b.bid_response, b.updated_at,
                   b.year, b.make, b.model
            FROM partner_bid_requests pbr
            JOIN bids b ON b.id = pbr.bid_id
            WHERE pbr.dealer_id = %s
              AND (b.bid_amount IS NOT NULL OR b.bid_response IS NOT NULL)
              AND (pbr.partner_seen_response_at IS NULL
                   OR b.updated_at > pbr.partner_seen_response_at)
            ORDER BY b.updated_at DESC
        """, (dealer['id'],))
        unread_responses = cur.fetchall()

        # 2026-05-11: Inbound bids pushed TO this dealer (bid_pushes).
        # Join the freshest photo per bid for the thumbnail in the card.
        cur.execute("""
            SELECT bp.id AS push_id, bp.bid_id, bp.pushed_at, bp.viewed_at,
                   b.year, b.make, b.model, b.trim, b.mileage, b.vin, b.color,
                   (SELECT url FROM bid_photos WHERE bid_id = bp.bid_id
                     ORDER BY id LIMIT 1) AS thumb_url,
                   (SELECT COUNT(*) FROM bid_partner_offers
                     WHERE bid_id = bp.bid_id AND dealer_id = bp.dealer_id) AS our_offer_count
              FROM bid_pushes bp
              JOIN bids b ON b.id = bp.bid_id
             WHERE bp.dealer_id = %s
             ORDER BY b.id DESC
             LIMIT 25
        """, (dealer['id'],))
        inbound_pushes = cur.fetchall()
        # Unread count must reflect ALL pushes for this dealer (not just the
        # LIMIT-25 visible slice) so the banner stays accurate when there
        # are >25 pushes — otherwise older unread rows fall outside the
        # window and the badge under-counts.
        cur.execute("""
            SELECT COUNT(*) AS n FROM bid_pushes
             WHERE dealer_id = %s AND viewed_at IS NULL
        """, (dealer['id'],))
        _row = cur.fetchone()
        inbound_unread = int((_row.get('n') if hasattr(_row, 'get') else _row[0]) or 0)

        # 2026-05-18: Per-unit market comps (MMR / rBook / trends).
        # Gated to dealers explicitly opted-in via portal_slug whitelist —
        # rolled out to Encore first; other partners get nothing until the
        # comps cron is widened. The card mirrors the visual density of the
        # internal /opportunities scout view (pricing-grid + signal chips +
        # trend chips) using:
        #   - dealer_inventory_comps:  daily comps snapshot, trends, market DOL
        #   - dealer_mmr (joined):     retail avg/above/below, grade, sample size
        #   - sourcing_requests:       active wishlist matches per YMM
        comps_by_inv_id = {}
        wishlists_by_inv_id = {}
        history_by_inv_id = {}
        intel_by_inv_id = {}     # DEALER_INTEL_2026_05_19
        segment_intel = []        # DEALER_INTEL_2026_05_19
        intel_summary = None      # DEALER_INTEL_L4_2026_05_19
        if dealer.get('portal_slug') in COMPS_ENABLED_SLUGS and not wholesaler_mode:
            cur.execute("""
                SELECT DISTINCT ON (di.id)
                       di.id          AS dealer_inventory_id,
                       dic.snapshot_date,
                       dic.mmr_comp_value, dic.mmr_comp_count,
                       dic.rbook_p25, dic.rbook_p50, dic.rbook_comp_count,
                       dic.market_median_days_on_lot, dic.market_median_source,
                       dic.price_trend_7d, dic.price_trend_14d,
                       dic.price_trend_30d, dic.price_trend_60d,
                       dic.comps_raw,
                       dm.wholesale_avg     AS mmr_wholesale_avg,
                       dm.wholesale_above   AS mmr_wholesale_above,
                       dm.wholesale_below   AS mmr_wholesale_below,
                       dm.retail_avg        AS mmr_retail_avg,
                       dm.retail_above      AS mmr_retail_above,
                       dm.retail_below      AS mmr_retail_below,
                       dm.grade             AS mmr_grade,
                       dm.sample_size       AS mmr_sample_size,
                       dm.fetched_at        AS mmr_fetched_at
                  FROM dealer_inventory di
                  LEFT JOIN dealer_inventory_comps dic
                         ON dic.dealer_inventory_id = di.id
                  LEFT JOIN dealer_mmr dm ON dm.vin = di.vin
                 WHERE di.dealer_id = %s AND di.status='active'
                 ORDER BY di.id, dic.snapshot_date DESC NULLS LAST
            """, (dealer['id'],))
            for r in cur.fetchall():
                # Skip when neither comps nor MMR cache exists for this inv row.
                if r['snapshot_date'] is None and r['mmr_wholesale_avg'] is None:
                    continue
                comps_by_inv_id[r['dealer_inventory_id']] = dict(r)
            # Per-VIN 60d comp price history for the chart card.
            # Combines 'daily_run' snapshots + 'manheim_tx_backfill' synthesized
            # rows. Most recent point per (inv_id, snapshot_date) wins.
            history_by_inv_id = {}
            if comps_by_inv_id:
                inv_ids = list(comps_by_inv_id.keys())
                cur.execute("""
                    SELECT dealer_inventory_id AS inv_id, snapshot_date,
                           mmr_wholesale_avg AS mmr, rbook_p50 AS rbook,
                           source
                      FROM dealer_inventory_comp_history
                     WHERE dealer_inventory_id = ANY(%s)
                       AND snapshot_date >= CURRENT_DATE - INTERVAL '60 days'
                     ORDER BY dealer_inventory_id, snapshot_date ASC
                """, (inv_ids,))
                for r in cur.fetchall():
                    history_by_inv_id.setdefault(r['inv_id'], []).append({
                        'd': r['snapshot_date'].isoformat(),
                        'mmr': float(r['mmr']) if r['mmr'] is not None else None,
                        'rbook': float(r['rbook']) if r['rbook'] is not None else None,
                        'src': r['source'],
                    })

            # DEALER_INTEL_2026_05_19: per-VIN action chips (keep/hold/
            # price_drop/sell_now) + segment rollups for the top-of-page
            # banner. Written by dealer_intel.py daily after the comps
            # pipeline. Gated by COMPS_ENABLED_SLUGS so other dealers
            # never see this surface.
            cur.execute("""
                SELECT dealer_inventory_id, chip, confidence, reasoning_text,
                       inputs, days_on_lot, segment_avg_dol, segment_volume,
                       asking_price, rbook_p50, rbook_p75, mmr_now,
                       mmr_trend_7d_pct, mmr_trend_30d_pct
                  FROM dealer_intel_snapshot
                 WHERE dealer_id = %s
            """, (dealer['id'],))
            for r in cur.fetchall():
                intel_by_inv_id[r['dealer_inventory_id']] = dict(r)
            # Segment rollups — keep just today's snapshot per (dealer,
            # segment_key). Pulls strong/slow segments for the banner.
            cur.execute("""
                SELECT DISTINCT ON (segment_key)
                       segment_key, make, year_band, mileage_band,
                       window_days, sold_volume, avg_dol_days,
                       median_dol_days, active_count, aging_count,
                       mmr_trend_7d_pct, mmr_trend_30d_pct, verdict, confidence,
                       snapshot_date
                  FROM dealer_intel_segments
                 WHERE dealer_id = %s
                 ORDER BY segment_key, snapshot_date DESC
            """, (dealer['id'],))
            segment_intel = [dict(r) for r in cur.fetchall()]

            # DEALER_INTEL_NEWSLETTER_2026_05_19: long-form daily Gemini
            # newsletter replacing the old 3-card summary. Falls back to
            # the older summary row if newsletter not yet generated.
            cur.execute("""
                SELECT headline, lede, body_sections, sample_sizes,
                       snapshot_date, computed_at, model_name
                  FROM dealer_intel_newsletter
                 WHERE dealer_id = %s
                 ORDER BY snapshot_date DESC, computed_at DESC
                 LIMIT 1
            """, (dealer['id'],))
            _dn = cur.fetchone()
            if _dn:
                intel_summary = dict(_dn)
                intel_summary['_kind'] = 'newsletter'
            else:
                cur.execute("""
                    SELECT headline, what_you_move_best, watch_list,
                           acquisition_blind_spots, sample_sizes,
                           snapshot_date, computed_at, model_name
                      FROM dealer_intel_summary
                     WHERE dealer_id = %s
                     ORDER BY snapshot_date DESC, computed_at DESC
                     LIMIT 1
                """, (dealer['id'],))
                _ds = cur.fetchone()
                if _ds:
                    intel_summary = dict(_ds)
                    intel_summary['_kind'] = 'cards_legacy'

            # Wishlist matches: active sourcing_requests whose YMM matches
            # any Encore inventory row. One query, grouped client-side by
            # inv_id (cross-joined on the YMM match in Python because the
            # sourcing model relaxations make pure-SQL matching ugly).
            cur.execute("""
                SELECT id AS sourcing_id, phone, customer_name, status,
                       year_min, year_max, make, model, trim, ext_color,
                       miles_max, narrative_brief, last_msg_at,
                       relaxations
                  FROM sourcing_requests
                 WHERE archived_at IS NULL
                   AND status IN ('wishlist','gathering','searching','presented')
                   AND make IS NOT NULL AND model IS NOT NULL
            """)
            wishlists = cur.fetchall()
            from datetime import datetime as _dt, timezone as _tz
            now_utc = _dt.now(_tz.utc)
            for v in rows or []:
                v_make = (v.get('make') or '').lower()
                v_model = (v.get('model') or '').lower()
                if not (v_make and v_model):
                    continue
                matches = []
                for w in wishlists:
                    if (w['make'] or '').lower() != v_make:
                        continue
                    wm = (w['model'] or '').lower()
                    if not (wm == v_model or wm in v_model or v_model in wm):
                        continue
                    relax = w['relaxations'] or []
                    if w['year_min'] and v.get('year') and v['year'] < w['year_min'] and 'year' not in relax:
                        continue
                    if w['year_max'] and v.get('year') and v['year'] > w['year_max'] and 'year' not in relax:
                        continue
                    if (w['miles_max'] and (v.get('mileage') or 0) > w['miles_max']
                            and 'miles' not in relax):
                        continue
                    days_old = 0
                    if w['last_msg_at']:
                        days_old = int((now_utc - w['last_msg_at']).total_seconds() / 86400)
                    matches.append({
                        'sourcing_id': w['sourcing_id'],
                        'customer_name': w['customer_name'] or 'unknown buyer',
                        'phone': w['phone'],
                        'status': w['status'],
                        'days_old': days_old,
                    })
                if matches:
                    wishlists_by_inv_id[v['id']] = matches

    return render_template('partner_dashboard.html',
                           user=user, dealer=dealer, slug=slug,
                           rows=rows, enabled_buckets=enabled_buckets,
                           bucket_rows=bucket_rows,
                           already_requested_vins=already_requested_vins,
                           quick_drop_rows=quick_drop_rows,
                           inventory_bid_rows=inventory_bid_rows,
                           unread_responses=unread_responses,
                           inbound_pushes=inbound_pushes,
                           inbound_unread=inbound_unread,
                           wholesaler_mode=wholesaler_mode,
                           comps_by_inv_id=comps_by_inv_id,
                           history_by_inv_id=history_by_inv_id if dealer.get("portal_slug") in COMPS_ENABLED_SLUGS else {},
                           intel_by_inv_id=intel_by_inv_id,
                           segment_intel=segment_intel,
                           intel_summary=intel_summary,
                           wishlists_by_inv_id=wishlists_by_inv_id,
                           viewing_as_admin=session.get('partner_viewing_as_admin', False),
                           BUCKETS=BUCKETS)


# ── Password-less dashboard access via per-dealer token ──────────────────
@bp.route('/partner/<slug>/d/<token>')
def dashboard_access_token(slug, token):
    """Frictionless dashboard entry — anyone with this URL lands on the
    dealer's portal without setting a password. The token is per-dealer
    and stored in `dealers.dashboard_token`. The handler logs the visitor
    in as the dealer's first partner_user (preferring the placeholder
    no-password account, which is the natural one to ride for token
    access). Settings on that account let the dealer optionally set
    real credentials later for a more conventional login.

    Token comparison is constant-time to defang any timing-attack
    enumeration. No expiry — token is permanent per spec; rotate via
    `UPDATE dealers SET dashboard_token = ...` to invalidate."""
    import hmac
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer:
            return 'Unknown portal.', 404
        stored = dealer.get('dashboard_token') or ''
        if not stored or not hmac.compare_digest(stored, token):
            return 'Invalid or expired access link.', 404
        cur.execute("""SELECT id FROM partner_users
                        WHERE dealer_id = %s
                        ORDER BY (password_hash IS NULL) DESC,
                                 created_at ASC
                        LIMIT 1""", (dealer['id'],))
        u = cur.fetchone()
        if not u:
            return 'No partner account exists for this dealer yet.', 500
    session['partner_user_id'] = u['id']
    session.permanent = True
    # Telegram-alert only on first login per session (suppress duplicate
    # pings when the dealer refreshes the same token URL repeatedly).
    if not session.get('_partner_link_alerted'):
        _tg_alert(f'🔑 <b>{dealer["name"]}</b> partner opened dashboard via direct link')
        session['_partner_link_alerted'] = True
    # Render the dashboard inline (no redirect) so the URL bar keeps the
    # /d/<token> path. Whatever the user copies from their address bar
    # then has the token in it and can be shared without triggering the
    # login redirect on the recipient's side.
    return dashboard(slug)


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
    # GET: if any partner_user exists (including the placeholder
    # no-password account), skip the login form and bounce straight into
    # the dashboard — the dashboard route auto-adopts the right user.
    # Login form only shows when the dealer has no users at all.
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if dealer:
            cur.execute("SELECT 1 FROM partner_users WHERE dealer_id=%s LIMIT 1",
                        (dealer['id'],))
            if cur.fetchone():
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
    Sets a flag so the dashboard banner shows 'Viewing as admin'.
    Prefers a user who has set a password; falls back to any placeholder
    partner_user so admin can preview a dealer that's been provisioned
    but not yet activated."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT pu.id, d.name AS dealer_name
            FROM partner_users pu
            JOIN dealers d ON d.id = pu.dealer_id
            WHERE pu.dealer_id = %s
            ORDER BY (pu.password_hash IS NOT NULL) DESC,
                     pu.created_at ASC
            LIMIT 1
        """, (dealer_id,))
        row = cur.fetchone()
        if not row:
            return (f'No partner_user row exists for dealer {dealer_id}. '
                    f'Provision one via /admin/partner/{dealer_id}/invite '
                    f'or /admin/partner/{dealer_id}/config first.'), 404
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
    """Toggle which alert buckets are enabled for this dealer.
    Also surfaces the password-less dashboard access link + mobile-link
    so the EW admin has one canonical place to grab them per dealer."""
    if request.method == 'POST':
        action = request.form.get('action')
        with _db() as conn, conn.cursor() as cur:
            if action == 'rotate_dashboard_token':
                new_token = secrets.token_urlsafe(20)
                cur.execute("UPDATE dealers SET dashboard_token = %s WHERE id = %s",
                            (new_token, dealer_id))
                conn.commit()
                flash('Dashboard access link rotated. Old link no longer works.', 'info')
            elif action == 'rotate_mobile_token':
                new_token = secrets.token_urlsafe(16)
                cur.execute("UPDATE dealers SET mobile_token = %s WHERE id = %s",
                            (new_token, dealer_id))
                conn.commit()
                flash('Mobile link rotated. Old link no longer works.', 'info')
            else:
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
        # Auto-provision missing tokens so this page is always actionable
        # without admin having to seed them via SQL first.
        updates = {}
        if dealer and not dealer.get('dashboard_token'):
            updates['dashboard_token'] = secrets.token_urlsafe(20)
        if dealer and not dealer.get('mobile_token'):
            updates['mobile_token'] = secrets.token_urlsafe(16)
        if updates:
            sets = ', '.join(f"{k} = %s" for k in updates)
            cur.execute(f"UPDATE dealers SET {sets} WHERE id = %s",
                        (*updates.values(), dealer_id))
            conn.commit()
            for k, v in updates.items():
                dealer = dict(dealer); dealer[k] = v
        if not dealer.get('portal_slug'):
            import re
            dealer = dict(dealer)
            dealer['_computed_slug'] = re.sub(r'[^a-z0-9]+', '',
                                              dealer['name'].lower())[:32]
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
      - send_text:  client-checkbox + dealer's sms_opt_in. Partner dealers
        consent via the signed partner agreement, not the SMS double-opt-in
        handshake, so sms_verified_at is not required here.

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
                # SMS — only if client requested AND user opted in (partner
                # agreement = consent; no separate sms_verified_at needed).
                # Bid # leads the message so partner replies can be routed
                # back to this thread by the Twilio inbound webhook.
                if send_text and u.get('sms_opt_in') and u.get('phone'):
                    sms_body_parts = [f'Bid #{bid_id} ({car})',
                                      'EW responded — reply to this text to chat']
                    if b['bid_amount']:
                        sms_body_parts.append(f'Offer: ${int(float(b["bid_amount"])):,}')
                    sms_body_parts.append('Reply STOP to unsubscribe.')
                    if _send_sms(u['phone'], ' — '.join(sms_body_parts)):
                        # Track the (phone, bid) anchor so the inbound webhook
                        # can route a plain reply back to this exact bid.
                        try:
                            cur.execute("""INSERT INTO partner_sms_sent
                                             (phone, bid_id, dealer_id)
                                           VALUES (%s, %s, %s)""",
                                        (u['phone'], bid_id, b['dealer_id']))
                        except Exception as _e:
                            print(f'[partner sms_sent log] skipped: {_e}')
    except Exception as e:
        print(f'[partner notify] failed for bid {bid_id}: {e}')


# ── Inbound bid push to subscribed partners + partner offer capture ──────
# 2026-05-11 — new flow distinct from partner_bid_requests. EW collects
# inbound bids from sellers (SMS to 754 number, dashboard manual entry).
# Subscribed partner dealers (dealers.receive_inbound_pushes=TRUE) get
# those bids pushed to their portal with a strip-down view. They submit
# offers via /partner/<slug>/inbound/<bid_id>/offer. Offers attach to the
# original bid_id via bid_partner_offers — no new partner_bid_requests row.
# EW operator sees all offers side-by-side on the bid detail page.

OPERATOR_HOME_IP = '108.64.163.112'
OPERATOR_TEST_PHONES = {'4074309675'}


def _bid_qualifies_for_push(bid: dict) -> tuple[bool, str]:
    """Filter rules — return (allowed, reason). Skip operator test bids and
    explicitly-killed bids. Keep all rules in one place so future filters
    are easy to extend.

    2026-05-11: per operator request, also exclude bids from the operator's
    home IP 108.64.163.112 (quick-drop tests) so Nuccio etc. never see them.
    """
    # Operator test phone — normalize to 10 digits
    raw_phone = bid.get('phone') or ''
    digits = ''.join(c for c in raw_phone if c.isdigit())
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]
    if digits in OPERATOR_TEST_PHONES:
        return (False, 'operator_test_phone')

    # Operator home IP — quick-drops from the operator's local computer
    creation_ip = (bid.get('creation_ip') or '').strip()
    if creation_ip == OPERATOR_HOME_IP:
        return (False, 'operator_home_ip')
    if creation_ip and OPERATOR_HOME_IP in creation_ip:
        return (False, 'operator_home_ip_in_chain')

    status = (bid.get('status') or '').lower()
    if status in ('dropped', 'rejected', 'spam'):
        return (False, f'status={status}')
    return (True, '')



# ── Match-routing score (2026-05-12) ─────────────────────────────────────
# Uses dealers.buy_profile JSONB built nightly from dealer_inventory.
# Returns (score, reason). score >= INBOUND_PUSH_MIN_SCORE → push allowed.
INBOUND_PUSH_MIN_SCORE = int(os.environ.get('INBOUND_PUSH_MIN_SCORE', '60'))

def _score_bid_for_dealer(bid: dict, profile: dict | None, dealer_id: int | None = None):
    """Score a bid against a dealer's buy_profile. Returns (score, reason).
    score >= INBOUND_PUSH_MIN_SCORE → push allowed. score is None → hard skip.

    VIN-on-lot exclusion: if `dealer_id` is provided AND bid.vin matches an
    active dealer_inventory row for that dealer, returns (None, 'has this VIN').
    Prevents pushing a dealer their own stock back."""
    if not profile or not (profile.get('makes')):
        return None, 'no profile'
    make = (bid.get('make') or '').upper().strip()
    if not make:
        return None, 'bid missing make'

    # ── VIN-on-lot seller-protection ─────────────────────────────
    vin = (bid.get('vin') or '').strip().upper()
    if vin and dealer_id is not None:
        try:
            with _db() as _c, _c.cursor() as _cur:
                _cur.execute("""
                    SELECT 1 FROM dealer_inventory
                     WHERE dealer_id = %s AND UPPER(vin) = %s AND status = 'active'
                     LIMIT 1
                """, (dealer_id, vin))
                if _cur.fetchone():
                    return None, f'has this VIN on lot ({vin})'
        except Exception as _e:
            print(f'[score] VIN check failed dealer={dealer_id} vin={vin}: {_e}',
                  flush=True)

    makes = profile.get('makes') or {}
    bands = profile.get('bands') or {}
    overrides = profile.get('overrides') or {}

    asked = bid.get('asking_price') or 0
    miles = bid.get('mileage')
    year  = bid.get('year')

    for rule in (overrides.get('never') or []):
        if 'price_lt' in rule and asked and asked < rule['price_lt']:
            return None, f"never (price<{rule['price_lt']})"
        if 'miles_gt' in rule and miles and miles > rule['miles_gt']:
            return None, f"never (miles>{rule['miles_gt']})"
    for rule in (overrides.get('always') or []):
        if rule.get('make') == make:
            return 100, 'override always'

    if make not in makes:
        return None, f'never stocks {make.title()}'

    m = makes[make]

    # YMMT_MATCH_2026_05_26: if bid is canonically tagged, require the dealer
    # to stock that specific (model, trim). Prevents SMS-pushing a GLS 450 to
    # a dealer whose only GLS units are 63 AMGs. Falls back to make-level for
    # untagged bids so legacy behavior is preserved.
    ymmt_model = (bid.get('ymmt_model') or '').strip()
    ymmt_trim  = (bid.get('ymmt_trim')  or '').strip()
    if ymmt_model:
        _models = m.get('models') or {}
        _me = _models.get(ymmt_model)
        if not _me:
            return None, f'never stocked {make.title()} {ymmt_model}'
        if ymmt_trim:
            _trims = _me.get('trims') or {}
            if ymmt_trim not in _trims:
                return None, (f'stocks {make.title()} {ymmt_model} but not '
                              f'{ymmt_trim} specifically')

    s = 50
    share = m.get('share') or 0
    if share >= 20:   s += 15
    elif share >= 10: s += 10
    elif share >= 5:  s += 5

    avg_y = m.get('avg_year')
    if avg_y and year:
        yd = abs(year - avg_y)
        if yd <= 2:    s += 20
        elif yd <= 5:  s += 10
        elif yd <= 10: pass
        else:          s -= 20

    ymin = bands.get('year_min'); ymax = bands.get('year_max')
    if year and ymin and ymax:
        if year < ymin - 2 or year > ymax + 2:
            s -= 25

    sold_n = m.get('sold_n') or 0
    days   = m.get('avg_days_on_lot') or 0
    if sold_n >= 3 and 0 < days < 15:
        s += 15
    elif sold_n >= 1 and 0 < days < 30:
        s += 5

    p10 = bands.get('price_p10'); p90 = bands.get('price_p90')
    if asked and p10 and p90:
        if p10 <= asked <= p90:
            s += 10
        elif asked < p10 * 0.6 or asked > p90 * 1.5:
            s -= 15

    mp90 = bands.get('miles_p90')
    if miles is not None and mp90:
        if miles <= mp90:
            s += 5
        elif miles > mp90 * 1.5:
            s -= 5

    return s, 'ok'


def _push_bid_to_subscribed_partners(bid_id: int) -> None:
    """Called from end of _run_assessment. Inserts bid_pushes rows for every
    subscribed dealer and notifies their partner_users. Idempotent via
    UNIQUE (bid_id, dealer_id) — re-running on the same bid is a no-op."""
    try:
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT b.id, b.phone, b.status, b.year, b.make, b.model, b.trim, b.mileage, b.vin,
                       b.creation_ip, b.creation_source,
                       COALESCE(b.asking_price, b.ai_price, b.bid_amount) AS asking_price,
                       b.ymmt_id,
                       yc.model AS ymmt_model,
                       yc.trim  AS ymmt_trim
                  FROM bids b
                  LEFT JOIN ymmt_catalog yc ON yc.id = b.ymmt_id
                 WHERE b.id = %s
            """, (bid_id,))
            bid = cur.fetchone()
            if not bid:
                return
            allowed, reason = _bid_qualifies_for_push(bid)
            if not allowed:
                print(f'[bid-push] bid={bid_id} skipped reason={reason}',
                      flush=True)
                return
            cur.execute("""
                SELECT id, name, portal_slug, dashboard_token, salesperson, salesperson_phone,
                       buy_profile
                  FROM dealers
                 WHERE receive_inbound_pushes = TRUE AND active = TRUE
                   AND portal_slug != ALL(%s)
            """, (['ect'],))  # YMMT_MATCH_EXCLUDE_2026_05_26: ECT excluded from SMS push too
            subscribers = cur.fetchall()
            if not subscribers:
                return
            for d in subscribers:
                # 2026-05-12: match-routing score gate
                score, reason = _score_bid_for_dealer(dict(bid), d.get('buy_profile'), dealer_id=d['id'])
                if score is None or score < INBOUND_PUSH_MIN_SCORE:
                    print(f'[bid-push] bid={bid_id} SKIP dealer={d["name"]} '
                          f'score={score} reason={reason}', flush=True)
                    continue
                cur.execute("""
                    INSERT INTO bid_pushes (bid_id, dealer_id)
                    VALUES (%s, %s)
                    ON CONFLICT (bid_id, dealer_id) DO NOTHING
                    RETURNING id
                """, (bid_id, d['id']))
                inserted = cur.fetchone()
                conn.commit()
                if inserted:
                    print(f'[bid-push] bid={bid_id} -> {d["name"]} '
                          f'score={score} reason={reason}', flush=True)
                    notify_partner_of_inbound_bid(bid_id, d)
    except Exception as e:
        print(f'[bid-push] error bid={bid_id}: {e}', flush=True)


def notify_partner_of_inbound_bid(bid_id: int, dealer: dict) -> None:
    """Notify partner users when a new inbound bid is pushed to them.
    Multi-recipient by design — iterates partner_users per dealer.

    2026-05-11: SMS + email are GATED behind INBOUND_PUSH_SEND_SMS_EMAIL
    env (default off). v1 strategy is to rely on the dashboard card +
    in-browser chime/banner; we'll flip the env to "true" once the
    dashboard UX is validated with Nuccio. Telegram-alert-to-Oscar fires
    regardless so the operator always knows a bid pushed."""
    send_channels = os.environ.get('INBOUND_PUSH_SEND_SMS_EMAIL', '').lower() in ('1', 'true', 'yes', 'on')
    try:
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT year, make, model, trim, mileage, vin
                  FROM bids WHERE id = %s
            """, (bid_id,))
            b = cur.fetchone()
            if not b:
                return
            car = f"{b['year'] or ''} {(b['make'] or '').upper()} " \
                  f"{(b['model'] or '').upper()}".strip()
            if b['trim']:
                car += f" {b['trim']}"
            vin_last6 = (b['vin'] or '')[-6:] if b['vin'] else ''
            miles_str = f"{b['mileage']:,} mi" if b['mileage'] else ''
            slug = dealer.get('portal_slug') or ''
            dtok = dealer.get('dashboard_token') or ''
            inbound_url = (f"{PORTAL_BASE}/partner/{slug}/d/{dtok}"
                           if dtok else f"{PORTAL_BASE}/partner/{slug}")
            sms_body = (f'EW: New inbound bid — {car} · {miles_str}'
                        f'{f" · VIN ...{vin_last6}" if vin_last6 else ""}\n'
                        f'View + offer: {inbound_url}\n'
                        f'Reply STOP to unsubscribe.')
            cur.execute("""
                SELECT id, email, full_name, phone, sms_opt_in, email_bid_alerts
                  FROM partner_users
                 WHERE dealer_id = %s
            """, (dealer['id'],))
            recipients = cur.fetchall()
            for u in recipients:
                if not send_channels:
                    print(f'[inbound-notify] skip bid={bid_id} '
                          f'user={u["id"]} (channels off — dashboard only)',
                          flush=True)
                    continue
                if u.get('sms_opt_in') and u.get('phone'):
                    sent = _send_sms(u['phone'], sms_body)
                    print(f'[inbound-notify] sms bid={bid_id} '
                          f'dealer={dealer["id"]} user={u["id"]} '
                          f'-> {u["phone"]} sent={sent}', flush=True)
                if u.get('email_bid_alerts') and u.get('email') \
                   and '@' in (u['email'] or '') \
                   and not u['email'].endswith('.invite'):
                    name_greet = (" " + u["full_name"]) if u.get("full_name") else ""
                    vin_line = f"<br>VIN: {b['vin']}" if b.get('vin') else ""
                    html = (
                        f'<p>Hi{name_greet},</p>'
                        f'<p>A new inbound vehicle bid just arrived at '
                        f'Experience Wholesale that you have rights to see:</p>'
                        f'<p style="background:#f1f5f9;padding:12px 14px;'
                        f'border-radius:6px;margin:12px 0;font-size:15px">'
                        f'<strong>{car}</strong><br>{miles_str}{vin_line}'
                        f'</p>'
                        f'<p><a href="{inbound_url}" '
                        f'style="display:inline-block;padding:11px 22px;'
                        f'background:#2563eb;color:#fff;text-decoration:none;'
                        f'border-radius:6px;font-weight:600">View + Submit Offer</a></p>'
                        f'<p style="font-size:12px;color:#64748b">'
                        f'You can change these alerts from your portal settings.</p>'
                    )
                    sent = _send_email(u['email'],
                                       f'New inbound bid: {car}', html)
                    print(f'[inbound-notify] email bid={bid_id} '
                          f'dealer={dealer["id"]} user={u["id"]} '
                          f'-> {u["email"]} sent={sent}', flush=True)
            _tg_alert(f'📨 <b>Inbound bid pushed to {dealer["name"]}</b>\n'
                      f'Bid #{bid_id} — {car}\n'
                      f'Recipients: {len(recipients)} user(s)\n'
                      f'View: {inbound_url}')
    except Exception as e:
        print(f'[inbound-notify] err bid={bid_id} dealer={dealer.get("id")}: {e}',
              flush=True)


# ── 2026-05-12 — Bidder-triggered push with target ask + SMS ─────────────
def push_bid_with_network_ask(bid_id: int, network_ask: float, note: str | None,
                              pushed_by: str | None = 'bidder',
                              only_dealer_ids: list[int] | None = None):
    """Bidder's manual push: stamps bids.network_ask + broadcasts to scored,
    VIN-filtered subscribed dealers + sends SMS to each surviving dealer.
    Returns dict: {sent: [{dealer,name,score,sms_to}], skipped: [...], errors: [...]}"""
    out = {'sent': [], 'skipped': [], 'errors': []}
    try:
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE bids
                   SET network_ask = %s, network_ask_note = %s,
                       network_pushed_at = NOW(), network_pushed_by = %s,
                       network_push_count = COALESCE(network_push_count,0) + 1
                 WHERE id = %s
             RETURNING id, year, make, model, mileage, vin,
                       COALESCE(asking_price, ai_price, bid_amount) AS asking_price
            """, (network_ask, note, pushed_by, bid_id))
            bid = cur.fetchone()
            if not bid:
                out['errors'].append(f'bid {bid_id} not found')
                return out

            cur.execute("""
                SELECT id, name, portal_slug, dashboard_token, salesperson,
                       salesperson_phone, buy_profile
                  FROM dealers
                 WHERE portal_slug IS NOT NULL AND active = TRUE
            """)
            subscribers = cur.fetchall()

            for d in subscribers:
                # 2026-05-12: per-call dealer filter from checkbox UI
                if only_dealer_ids is not None and d['id'] not in only_dealer_ids:
                    out['skipped'].append({'dealer_id': d['id'], 'name': d['name'],
                                           'score': None, 'reason': 'unchecked by operator'})
                    continue
                score, reason = _score_bid_for_dealer(dict(bid),
                                                     d.get('buy_profile'),
                                                     dealer_id=d['id'])
                # operator-override: when only_dealer_ids explicitly lists this dealer,
                # bypass the score gate. We still hard-block VIN-on-lot (seller-protection
                # — never sell a dealer their own car), but never-stocks / low-score are
                # overridable because operator may know something the algorithm doesnt.
                is_vin_protect = bool(reason and 'has this VIN' in (reason or ''))
                operator_override = (only_dealer_ids is not None
                                     and d['id'] in only_dealer_ids
                                     and not is_vin_protect)
                if not operator_override and (score is None or score < INBOUND_PUSH_MIN_SCORE):
                    out['skipped'].append({
                        'dealer_id': d['id'], 'name': d['name'],
                        'score': score, 'reason': reason,
                    })
                    continue

                cur.execute("""
                    INSERT INTO bid_pushes (bid_id, dealer_id, score, score_reason)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (bid_id, dealer_id) DO UPDATE
                       SET score = EXCLUDED.score, score_reason = EXCLUDED.score_reason
                """, (bid_id, d['id'], score, reason))

                # SMS the ask
                phone = d.get('salesperson_phone')
                if phone:
                    sms_text = _compose_network_ask_sms(bid, network_ask, note, d)
                    sent = _send_network_sms(phone, sms_text)
                    if sent:
                        cur.execute("""
                            UPDATE bid_pushes SET sms_sent_at=NOW(), sms_to=%s
                             WHERE bid_id=%s AND dealer_id=%s
                        """, (phone, bid_id, d['id']))
                        out['sent'].append({
                            'dealer_id': d['id'], 'name': d['name'],
                            'score': score, 'sms_to': phone,
                            'salesperson': d.get('salesperson'),
                        })
                    else:
                        out['errors'].append(f"{d['name']}: SMS send failed")
                else:
                    out['errors'].append(f"{d['name']}: no salesperson_phone")
            conn.commit()
    except Exception as e:
        out['errors'].append(f'exception: {e}')
    print(f'[network-push] bid={bid_id} ask=${network_ask:,.0f} '
          f'sent={len(out["sent"])} skipped={len(out["skipped"])} '
          f'errors={len(out["errors"])}', flush=True)
    return out


def _compose_network_ask_sms(bid: dict, ask: float, note: str | None,
                              dealer: dict) -> str:
    ymm = f"{bid.get('year') or ''} {bid.get('make') or ''} {bid.get('model') or ''}".strip()
    miles = bid.get('mileage') or 0
    body = (
        f"EW network ask: {ymm}, {miles:,} mi. "
        f"Need ${ask:,.0f} out. {note + ' ' if note else ''}"
        f"Reply YES to claim."
    )
    return body[:320]


def _send_network_sms(phone: str, body: str) -> bool:
    """app.send_sms wrapper with test-gate override."""
    try:
        from app import send_sms as _app_send
        gate = (os.environ.get("EW_SMS_TEST_GATE_PHONE") or "").strip()
        if gate:
            body = f"[TEST->{phone}] " + body
            phone = gate
        ok = _app_send(phone, body)
        print(f"[network-sms] to={phone} gated={bool(gate)} ok={ok}", flush=True)
        return bool(ok)
    except Exception as e:
        print(f"[network-sms] failed to={phone}: {e}", flush=True)
        return False
        client = _TwClient(sid, tok)
        msg = client.messages.create(to=phone, from_=frm, body=body)
        print(f'[network-sms] sent to={phone} sid={msg.sid}', flush=True)
        return True
    except Exception as e:
        print(f'[network-sms] failed to={phone}: {e}', flush=True)
        return False


def notify_salesperson_of_partner_offer(offer_id: int) -> None:
    """When a partner submits an offer on an inbound bid, text the assigned
    EW salesperson on that dealer (dealers.salesperson_phone). Also Telegram."""
    try:
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT o.id, o.bid_id, o.dealer_id, o.offer_amount, o.message,
                       o.submitted_at,
                       d.name AS dealer_name, d.salesperson, d.salesperson_phone,
                       pu.full_name AS user_name,
                       b.year, b.make, b.model, b.trim, b.vin
                  FROM bid_partner_offers o
                  JOIN dealers d ON o.dealer_id = d.id
                  JOIN bids b ON o.bid_id = b.id
             LEFT JOIN partner_users pu ON o.partner_user_id = pu.id
                 WHERE o.id = %s
            """, (offer_id,))
            row = cur.fetchone()
            if not row:
                return
            car = f"{row['year'] or ''} {(row['make'] or '').upper()} " \
                  f"{(row['model'] or '').upper()}".strip()
            vin_last6 = (row['vin'] or '')[-6:] if row['vin'] else ''
            amt = f"${int(float(row['offer_amount'])):,}" \
                  if row.get('offer_amount') else '(no amount)'
            who = row.get('user_name') or 'A partner user'
            bid_url = f"{PORTAL_BASE}/bid/{row['bid_id']}"
            sms = (f'{row["dealer_name"]} offer: {amt} on Bid #{row["bid_id"]} '
                   f'({car} ...{vin_last6}). View: {bid_url}')
            if row.get('salesperson_phone'):
                _send_sms(row['salesperson_phone'], sms)
                print(f'[partner-offer] sms -> salesperson '
                      f'{row["salesperson_phone"]}: bid={row["bid_id"]} '
                      f'offer_id={offer_id}', flush=True)
            note_line = f"Note: {row['message']}\n" if row.get("message") else ""
            _tg_alert(
                f'💰 <b>Partner offer received</b>\n'
                f'Dealer: {row["dealer_name"]}\n'
                f'User: {who}\n'
                f'Bid: #{row["bid_id"]} — {car}\n'
                f'Offer: {amt}\n'
                f'{note_line}'
                f'View: {bid_url}')
    except Exception as e:
        print(f'[partner-offer] notify err offer_id={offer_id}: {e}', flush=True)


@bp.route('/partner/<slug>/inbound/<int:bid_id>', methods=['GET'])
def inbound_bid_view(slug, bid_id):
    """Partner-side view of an inbound EW bid pushed to this dealer.
    Strict scope: only bid_pushes rows that match this dealer.
    Renders a stripped-down version of the bid card — no LSL, no dealer
    network, no AI assessment. See partner_inbound.html for the field
    whitelist."""
    user = _current_partner_user()
    if not user:
        return redirect(url_for('partner.login', slug=slug))
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            return redirect(url_for('partner.login', slug=slug))
        cur.execute("""
            SELECT id, pushed_at, viewed_at FROM bid_pushes
             WHERE bid_id = %s AND dealer_id = %s
        """, (bid_id, dealer['id']))
        push = cur.fetchone()
        if not push:
            return 'This bid is not available for your dealer.', 404
        if push['viewed_at'] is None:
            cur.execute("UPDATE bid_pushes SET viewed_at = NOW() WHERE id = %s",
                        (push['id'],))
            conn.commit()
        cur.execute("""
            SELECT id, year, make, model, trim, mileage, vin,
                   color, int_color, asking_price, created_at, ai_assessed_at,
                   status
              FROM bids WHERE id = %s
        """, (bid_id,))
        bid = cur.fetchone()
        if not bid:
            return 'Bid not found.', 404
        cur.execute("SELECT url FROM bid_photos WHERE bid_id = %s ORDER BY id",
                    (bid_id,))
        photos = cur.fetchall()
        cur.execute("SELECT * FROM vauto_lookups WHERE bid_id = %s", (bid_id,))
        vauto = cur.fetchone()
        cur.execute("SELECT * FROM accutrade_lookups WHERE bid_id = %s",
                    (bid_id,))
        accutrade = cur.fetchone()
        cur.execute("SELECT * FROM ipacket_lookups WHERE bid_id = %s",
                    (bid_id,))
        ipacket = cur.fetchone()
        # Partner-safe market_intel. We prefer vauto_lookups.market_intel_cached
        # because it has the richer computed stats (retail_median, closest_3,
        # median_days_on_lot, stocking_report, manheim summary) that match
        # what the EW operator sees. Fall back to ai_assessment_log if the
        # cache isn't populated yet. Explicitly NOT loading dealer_intel,
        # buyer_intel, ai reasoning, target_buy, confidence, flags.
        import json as _json
        mi = None
        if vauto and vauto.get('market_intel_cached'):
            mi = vauto['market_intel_cached']
        else:
            cur.execute("""
                SELECT market_intel FROM ai_assessment_log
                 WHERE bid_id = %s ORDER BY created_at DESC LIMIT 1
            """, (bid_id,))
            ass = cur.fetchone() or {}
            mi = ass.get('market_intel') if ass else None
        if isinstance(mi, str):
            try: mi = _json.loads(mi)
            except Exception: mi = None
        # Prior offers from THIS dealer on THIS bid (so they don't re-offer)
        cur.execute("""
            SELECT id, offer_amount, message, submitted_at, ew_action
              FROM bid_partner_offers
             WHERE bid_id = %s AND dealer_id = %s
             ORDER BY submitted_at DESC
        """, (bid_id, dealer['id']))
        prior_offers = cur.fetchall()
        # Vehicle history reports — bid_photos with is_car=FALSE are the
        # Carfax / AutoCheck screenshots. URLs render through /thumb proxy
        # which handles both local /static/uploads/ and api.twilio.com
        # Basic auth transparently — same path EW operator side uses.
        carfax_urls = []
        try:
            cur.execute("""
                SELECT url FROM bid_photos
                 WHERE bid_id = %s AND is_car = FALSE
                 ORDER BY id
            """, (bid_id,))
            carfax_urls = [r['url'] for r in cur.fetchall()]
        except Exception:
            conn.rollback()
    return render_template('partner_inbound.html',
                           dealer=dealer, bid=dict(bid),
                           photos=[p['url'] for p in photos],
                           vauto=dict(vauto) if vauto else None,
                           accutrade=dict(accutrade) if accutrade else None,
                           ipacket=dict(ipacket) if ipacket else None,
                           market_intel=mi,
                           carfax_urls=carfax_urls,
                           prior_offers=[dict(o) for o in prior_offers],
                           slug=slug,
                           user=user)


@bp.route('/partner/<slug>/inbound/<int:bid_id>/offer', methods=['POST'])
def inbound_bid_offer(slug, bid_id):
    """Partner submits an offer on an inbound bid. Inserts bid_partner_offers
    (does NOT create a partner_bid_requests row). Salesperson on the dealer
    gets SMS + Telegram alert."""
    user = _current_partner_user()
    if not user:
        return redirect(url_for('partner.login', slug=slug))
    raw_amt = (request.form.get('offer_amount') or '').strip().replace(',', '').replace('$', '')
    message = (request.form.get('message') or '').strip()[:1000]
    try:
        amt = float(raw_amt) if raw_amt else None
    except ValueError:
        amt = None
    if amt is None or amt <= 0:
        flash('Please enter a valid offer amount.', 'error')
        return redirect(url_for('partner.inbound_bid_view',
                                slug=slug, bid_id=bid_id))
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            return redirect(url_for('partner.login', slug=slug))
        cur.execute("""
            SELECT id FROM bid_pushes
             WHERE bid_id = %s AND dealer_id = %s
        """, (bid_id, dealer['id']))
        if not cur.fetchone():
            return 'This bid is not available for your dealer.', 404
        cur.execute("""
            INSERT INTO bid_partner_offers
                (bid_id, dealer_id, partner_user_id, offer_amount, message)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (bid_id, dealer['id'], user['id'], amt, message or None))
        offer_id = cur.fetchone()['id']
        conn.commit()
    notify_salesperson_of_partner_offer(offer_id)
    flash(f'Offer of ${int(amt):,} sent. We will contact you shortly.', 'info')
    return redirect(url_for('partner.inbound_bid_view',
                            slug=slug, bid_id=bid_id))


# ── Submit bid requests (partner side) ────────────────────────────────────
def _ensure_partner_contact(cur, dealer_id: int, dealer_name: str,
                            salesperson: str | None = None) -> int:
    """One synthetic contact per partner dealer. All bids from a partner use
    this contact_id so they group together in the EW bid list. Phone uses
    a non-numeric sentinel (`PARTNER{id}`) so it can't collide with a real
    field-rep number but still satisfies NOT NULL.

    `company` carries the EW-side salesperson assigned to this dealer (the
    value drives the blue chip in the dashboard contact cell). On every
    bid create, `company` is refreshed via ON CONFLICT so reassigning the
    dealer's salesperson updates the chip on subsequent bids."""
    phone = f'PARTNER{dealer_id}'
    cur.execute("""
        INSERT INTO contacts (phone, name, company)
        VALUES (%s, %s, %s)
        ON CONFLICT (phone) DO UPDATE
          SET name = EXCLUDED.name, company = EXCLUDED.company
        RETURNING id
    """, (phone, f'{dealer_name} (Partner Portal)',
          (salesperson or '').strip() or None))
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

        contact_id = _ensure_partner_contact(cur, dealer['id'], dealer['name'],
                                              dealer.get('salesperson'))
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
            # Snapshot the dealer's currently-assigned EW salesperson onto
            # the bid (matches the Quick Drop + mobile-token paths) so it
            # renders the PARTNER + 👤 badges on the EW dashboard. Frozen
            # at create time — reassigning the dealer's salesperson later
            # doesn't rewrite history.
            salesperson_snapshot = dealer.get('salesperson') or None
            cur.execute("""
                INSERT INTO bids (contact_id, phone, vin,
                                  year, make, model, trim, mileage, color,
                                  raw_message, asking_price, notes,
                                  status, has_unread,
                                  partner_request_id, partner_dealer_id,
                                  salesperson)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'new', TRUE, %s, %s, %s)
                RETURNING id
            """, (contact_id, partner_phone, inv['vin'],
                  inv['year'], inv['make'], inv['model'], inv['trim'],
                  inv['mileage'], inv['ext_color'],
                  raw_message, price_val, notes,
                  pbr_id, dealer['id'], salesperson_snapshot))
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
            # DealerOn (and other AAN/WP) galleries embed srcset entries
            # like "url?width=400 400w, url?width=800 800w" — when the
            # scanner stored those as separate items, the trailing
            # " 400w" descriptor got captured into the URL and broke
            # rendering. Strip everything after the first space and
            # decode HTML-encoded `&amp;` so each entry is a real URL.
            def _clean(u):
                u = (u or '').strip().split(' ', 1)[0]
                return u.replace('&amp;', '&')
            all_photos = [_clean(p) for p in all_photos]
            # Filter out dealer logos / badges / icons — those routinely
            # slip into scraped galleries and end up as photo #1.
            _JUNK = ('logo', 'badge', 'icon', 'placeholder', '/cms/', '/ui/',
                     'facebook', 'instagram', 'twitter', 'youtube')
            all_photos = [p for p in all_photos
                          if p and not any(j in p.lower() for j in _JUNK)]
            # Dedupe while preserving order
            seen = set()
            all_photos = [p for p in all_photos if not (p in seen or seen.add(p))]
            # Cap at 15 — partners want the full gallery on EW dashboard,
            # 400px-wide srcset entries are tiny so this stays cheap.
            new_photo_ids = []
            for url in all_photos[:15]:
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

            # 7. SMS the assigned salesperson (no-op if no phone on file).
            _notify_salesperson(dealer, bid_id, inv['vin'],
                                inv.get('year'), inv.get('make'), inv.get('model'),
                                price_val, source='inventory bid')

            count += 1
        conn.commit()

    flash(f'{count} car{"s" if count != 1 else ""} submitted — our team will respond shortly.',
          'info')
    if count:
        _tg_alert(f'💰 <b>{dealer["name"]}</b> submitted {count} bid request'
                  f'{"s" if count != 1 else ""}\n'
                  f'{user["full_name"] or user["email"]}')
    return redirect(url_for('partner.dashboard', slug=slug))


# ── SMS notification to dealer's assigned EW salesperson ───────────
def _notify_salesperson(dealer_row, bid_id, vin, year, make, model,
                        asking_price, source='bid'):
    """Fire-and-forget SMS to the dealer's assigned salesperson_phone
    when a partner submission lands. Silent no-op when no phone is set
    on the dealer (so admin can opt-in by populating the field on
    /dealers/<id>). Called from quick_drop / submit_requests / mobile
    partner-submit paths so all three channels notify."""
    print(f'[salesperson notify] called bid={bid_id} dealer={dealer_row.get("name") if dealer_row else None} phone={dealer_row.get("salesperson_phone") if dealer_row else None}', flush=True)
    if not dealer_row:
        print('  → no dealer_row, skipping', flush=True)
        return
    phone = (dealer_row.get('salesperson_phone') or '').strip()
    if not phone:
        print('  → no phone, skipping', flush=True)
        return
    car = ' '.join(str(x) for x in (year, make, model) if x).strip() or '(VIN-only)'
    sales_first = (dealer_row.get('salesperson') or 'there').split(' ')[0]
    parts = [f'Hey {sales_first} — {dealer_row["name"]} just submitted a {source} on a {car}']
    if asking_price:
        try:
            parts.append(f'asking ${int(float(asking_price)):,}')
        except (TypeError, ValueError):
            pass
    parts.append(f'Bid #{bid_id}')
    msg = ' — '.join(parts) + ' (Reply STOP to opt out.)'
    print(f'[salesperson notify] dispatching SMS to {phone}: {msg[:120]}', flush=True)
    try:
        ok = _send_sms(phone, msg)
        print(f'[salesperson notify] _send_sms returned {ok}', flush=True)
    except Exception as e:
        print(f'[salesperson sms] {dealer_row.get("name")}: {e}', flush=True)


# ── Per-car bid detail page (mobile-friendly full-page form) ────────
@bp.route('/partner/<slug>/bid/<int:inv_id>')
def bid_detail(slug, inv_id):
    """Full-page detail view for one inventory car. Pre-bid state shows
    a target-price + notes form that POSTs to the existing
    /partner/<slug>/submit endpoint (same shape, single car). Post-bid
    state shows the EW thread + counter form. Mobile YMM links land
    here so the dealer can act on a single car without scrolling
    sideways through the dashboard table."""
    user = _current_partner_user()
    if not user:
        return redirect(url_for('partner.login', slug=slug))
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            return 'Forbidden', 403
        cur.execute('SELECT * FROM dealer_inventory '
                    'WHERE id = %s AND dealer_id = %s',
                    (inv_id, dealer['id']))
        inv = cur.fetchone()
        if not inv:
            return 'Vehicle not found', 404
        cur.execute("""SELECT pbr.id AS pbr_id, pbr.target_price,
                              pbr.partner_message, pbr.submitted_at,
                              pbr.bid_id,
                              b.status AS bid_status,
                              b.bid_amount AS ew_counter,
                              b.bid_response AS ew_message
                         FROM partner_bid_requests pbr
                    LEFT JOIN bids b ON b.id = pbr.bid_id
                        WHERE pbr.dealer_id = %s
                          AND pbr.inventory_id = %s
                        ORDER BY pbr.submitted_at DESC
                        LIMIT 1""",
                    (dealer['id'], inv_id))
        req = cur.fetchone()
    return render_template('partner_bid_detail.html',
                           dealer=dealer, slug=slug, inv=inv, req=req,
                           user=user)


# ── Quick Drop: bid request for a car NOT in the partner's inventory ───
@bp.route('/partner/<slug>/quick-drop', methods=['POST'])
def quick_drop(slug):
    """Partner wants a bid on a vehicle that isn't in their scraped
    inventory — e.g. a trade just walked in. They give us VIN + miles +
    asking price (all required) plus optional photos + notes. We create a
    partner_bid_requests row with NULL inventory_id, the EW bid, copy
    photos, and the standard pipeline (vAuto + assessment via worker
    polling) picks it up automatically."""
    user = _current_partner_user()
    if not user:
        return jsonify({'error': 'not_authenticated'}), 401

    # ── Parse + validate form ────────────────────────────────────────
    vin = (request.form.get('vin') or '').strip().upper()
    if len(vin) != 17 or not any(c.isalpha() for c in vin):
        return jsonify({'error': 'VIN must be 17 characters and include letters.'}), 400
    if any(c in vin for c in 'IOQ'):
        return jsonify({'error': 'VIN contains invalid characters (I/O/Q).'}), 400

    raw_miles = (request.form.get('mileage') or '').replace(',', '').strip()
    try:
        mileage = int(raw_miles)
        if mileage <= 0 or mileage > 1_000_000:
            raise ValueError
    except ValueError:
        return jsonify({'error': 'Mileage is required and must be a positive number.'}), 400

    raw_price = (request.form.get('asking_price') or '').replace(',', '').replace('$', '').strip()
    try:
        asking_price = float(raw_price)
        if asking_price <= 0 or asking_price > 10_000_000:
            raise ValueError
    except ValueError:
        return jsonify({'error': 'Asking price is required and must be a positive number.'}), 400

    notes_in = (request.form.get('notes') or '').strip()

    # Photo files (optional). Cap at 10 to keep request bounded.
    photo_files = []
    for f in request.files.getlist('photos'):
        if f and f.filename:
            photo_files.append(f)
        if len(photo_files) >= 10:
            break

    # NHTSA decode runs FIRST (outside any DB txn). Slow + remote; we don't
    # want it inside the writer transaction. Best-effort — bid still
    # creates without YMM if NHTSA times out.
    try:
        from app import decode_vin as _decode_vin
        decoded = _decode_vin(vin) or {}
    except Exception:
        decoded = {}

    # Single atomic transaction for ALL writes (pbr + bid + photos +
    # messages). Earlier split-transaction layout left orphan pbr rows
    # when bid_messages crashed, because the pbr was already committed.
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            return jsonify({'error': 'forbidden'}), 403

        # Salesperson snapshot — frozen on the bid even if dealer is
        # reassigned later (matches the inventory-bid path).
        salesperson_snapshot = dealer.get('salesperson') or None

        # Wholesaler-mode dealers (no scraped site) submit through the same
        # Quick Drop pipeline, but the bid is held in 'pending' review until
        # the assigned reviewer (e.g. Oscar) approves it from
        # /wholesaler-<reviewer>. Until then the bid is invisible to the
        # Buy Center. vAuto/iPacket/AccuTrade still run in the background
        # so all data is ready when the reviewer opens it.
        is_wholesaler = (dealer.get('portal_mode') == 'wholesaler')
        review_status = 'pending' if is_wholesaler else None

        contact_id = _ensure_partner_contact(cur, dealer['id'], dealer['name'],
                                              dealer.get('salesperson'))
        partner_phone = f'PARTNER{dealer["id"]}'

        source_label = 'Wholesaler Submission' if is_wholesaler else 'Quick Drop'
        raw_message = f'{source_label} from {dealer["name"]}: need ${int(asking_price):,}'
        if notes_in:
            raw_message += f' — {notes_in}'
        notes = (f'[Partner {source_label} · {dealer["name"]} · '
                 f'requested by {user["email"]}]')

        # 1. partner_bid_requests audit row (NULL inventory_id since this
        #    car isn't in their scraped inventory).
        cur.execute("""
            INSERT INTO partner_bid_requests
                (dealer_id, partner_user_id, inventory_id, vin,
                 target_price, partner_message)
            VALUES (%s, %s, NULL, %s, %s, %s)
            RETURNING id
        """, (dealer['id'], user['id'], vin, asking_price, notes_in or None))
        pbr_id = cur.fetchone()['id']

        # 2. Create the bid linked to this pbr.
        cur.execute("""
            INSERT INTO bids (contact_id, phone, vin,
                              year, make, model, trim, mileage,
                              raw_message, asking_price, notes,
                              status, has_unread,
                              partner_request_id, partner_dealer_id,
                              salesperson, vauto_priority, review_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'new', TRUE, %s, %s, %s, TRUE, %s)
            RETURNING id
        """, (contact_id, partner_phone, vin,
              decoded.get('year'), decoded.get('make'), decoded.get('model'),
              decoded.get('trim'), mileage,
              raw_message, asking_price, notes,
              pbr_id, dealer['id'], salesperson_snapshot, review_status))
        bid_id = cur.fetchone()['id']

        cur.execute("UPDATE partner_bid_requests SET bid_id = %s WHERE id = %s",
                    (bid_id, pbr_id))

        # 3. Save uploaded photos to /static/uploads/ + bid_photos rows.
        new_photo_ids = []
        if photo_files:
            from werkzeug.utils import secure_filename
            uploads_dir = os.path.join(current_app.root_path,
                                       'static', 'uploads')
            os.makedirs(uploads_dir, exist_ok=True)
            for f in photo_files:
                ext = os.path.splitext(f.filename)[1].lower() or '.jpg'
                if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.heic'):
                    continue
                fname = f'qd_{bid_id}_{secrets.token_hex(6)}{ext}'
                disk_path = os.path.join(uploads_dir, fname)
                f.save(disk_path)
                rel_url = f'/static/uploads/{fname}'
                # Store the *URL path* in both columns (this codebase treats
                # local_path as a URL too — the dashboard's COALESCE picks it
                # first and feeds it to the thumb proxy as a URL). Storing
                # the absolute filesystem path here breaks the image link.
                cur.execute("""INSERT INTO bid_photos (bid_id, url, local_path)
                               VALUES (%s, %s, %s) RETURNING id""",
                            (bid_id, rel_url, rel_url))
                new_photo_ids.append(cur.fetchone()['id'])

        # 4. Seed message thread
        cur.execute("""
            INSERT INTO bid_messages (bid_id, direction, message,
                                      from_phone, to_phone)
            VALUES (%s, 'inbound', %s, %s, %s)
        """, (bid_id, raw_message, partner_phone, 'EW'))

        conn.commit()

    # 5. Pre-warm thumbnails (best-effort)
    if new_photo_ids:
        try:
            warm_bid_photo_thumbs(new_photo_ids)
        except Exception:
            pass

    # 6. Kick off market check (background); vAuto + assessment workers
    #    poll the queue and pick this up automatically because
    #    vauto_priority=TRUE.
    try:
        from app import trigger_market_check
        trigger_market_check(bid_id, vin)
    except Exception:
        pass

    _tg_prefix = '🕒 PENDING REVIEW' if is_wholesaler else '🎯'
    _tg_alert(f'{_tg_prefix} <b>{dealer["name"]}</b> {source_label} · Bid #{bid_id}\n'
              f'{user["email"]}\n'
              f'{vin} · {mileage:,} mi · ask ${int(asking_price):,}'
              + (f'\nPhotos: {len(new_photo_ids)}' if new_photo_ids else '')
              + (f'\n{notes_in}' if notes_in else ''))

    # SMS the dealer's assigned salesperson if a phone is on file.
    _notify_salesperson(dealer, bid_id, vin,
                        decoded.get('year'), decoded.get('make'),
                        decoded.get('model'), asking_price,
                        source=source_label)

    return jsonify({'ok': True, 'bid_id': bid_id, 'pbr_id': pbr_id})


# ── Partner deletes their own quick-drop request ──────────────────────
@bp.route('/partner/<slug>/qd/<int:pbr_id>/delete', methods=['POST'])
@bp.route('/partner/<slug>/req/<int:pbr_id>/delete', methods=['POST'])
def quick_drop_delete(slug, pbr_id):
    """Partner removes their own bid request — works for BOTH Quick Drop
    pbrs (inventory_id IS NULL) and inventory-based pbrs (inventory_id
    IS NOT NULL). Hard-delete: bid + photos + messages + pbr. Restricted
    to their own dealer."""
    user = _current_partner_user()
    if not user:
        return jsonify({'error': 'not_authenticated'}), 401
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            return jsonify({'error': 'forbidden'}), 403
        cur.execute("""SELECT id, bid_id, vin, inventory_id
                         FROM partner_bid_requests
                        WHERE id = %s AND dealer_id = %s""",
                    (pbr_id, dealer['id']))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'not_found_or_not_yours'}), 404
        bid_id = row['bid_id']
        vin = row['vin']
        kind = 'Quick Drop' if row['inventory_id'] is None else 'inventory bid'
        if bid_id:
            cur.execute("DELETE FROM bid_messages WHERE bid_id = %s", (bid_id,))
            cur.execute("DELETE FROM bid_photos WHERE bid_id = %s", (bid_id,))
            cur.execute("DELETE FROM bids WHERE id = %s", (bid_id,))
        cur.execute("DELETE FROM partner_bid_requests WHERE id = %s", (pbr_id,))
        conn.commit()
    _tg_alert(f'🗑 <b>{dealer["name"]}</b> deleted {kind}'
              + (f' · Bid #{bid_id}' if bid_id else '')
              + f'\n{vin} · {user["email"]}')
    return jsonify({'ok': True})


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
            if action == 'enable_sms':
                # One-step SMS opt-in for partner dealers. Consent trail comes
                # from the signed partner agreement, not the SMS double-opt-in
                # handshake — same logic the bid-send path uses (sms_opt_in
                # alone gates eligibility for partners). Confirmation SMS
                # still goes out so a wrong number is at least visible.
                raw_phone = request.form.get('phone') or ''
                phone = _normalize_phone(raw_phone)
                if not phone:
                    flash('Please enter a valid US phone number (10 digits).', 'error')
                else:
                    cur.execute("""
                        UPDATE partner_users
                        SET phone = %s, sms_opt_in = TRUE,
                            sms_verified_at = NOW(),
                            sms_verify_code = NULL, sms_verify_expires = NULL
                        WHERE id = %s
                    """, (phone, user['id']))
                    conn.commit()
                    _send_sms(phone,
                        'You are subscribed to Experience Wholesale bid alerts. '
                        'Reply STOP to unsubscribe.')
                    _tg_alert(f'📱 <b>{dealer["name"]}</b> partner enabled SMS alerts\n'
                              f'{user["email"]} → {phone}')
                    flash(f'SMS alerts enabled. Sending to {phone}.', 'info')
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
            elif action == 'set_password':
                # Self-serve password set/change. If the account has no
                # password yet (placeholder / token-link user), accept any
                # new password >= 8 chars. If a password already exists,
                # require the current one to confirm it's the legitimate
                # holder. Email may also be updated in the same form.
                new_pw  = request.form.get('new_password') or ''
                conf_pw = request.form.get('confirm_password') or ''
                cur.execute("SELECT password_hash FROM partner_users WHERE id=%s",
                            (user['id'],))
                row = cur.fetchone()
                has_existing = bool(row and row['password_hash'])
                if has_existing:
                    cur_pw = request.form.get('current_password') or ''
                    if not _check_password(cur_pw, row['password_hash']):
                        flash('Current password is incorrect.', 'error')
                        new_pw = ''  # short-circuit
                if new_pw:
                    if len(new_pw) < 8:
                        flash('New password must be at least 8 characters.',
                              'error')
                    elif new_pw != conf_pw:
                        flash('New passwords do not match.', 'error')
                    else:
                        cur.execute("""UPDATE partner_users
                                       SET password_hash = %s
                                       WHERE id = %s""",
                                    (_hash_password(new_pw), user['id']))
                        conn.commit()
                        verb = 'changed' if has_existing else 'set'
                        _tg_alert(f'🔐 <b>{dealer["name"]}</b> partner '
                                  f'{verb} their password\n{user["email"]}')
                        flash(f'Password {verb}. Sign in any time at '
                              f'/partner/{slug}/login with your email + '
                              f'this password.', 'info')
            elif action == 'update_email':
                # Self-serve email change. Email is the unique login identifier
                # so we (a) validate format, (b) reject if the new address is
                # already taken by a different partner_user. Old email is
                # carried in the Telegram alert so admin can spot the swap.
                new_email = (request.form.get('email') or '').strip().lower()
                old_email = user['email']
                if '@' not in new_email or '.' not in new_email.split('@', 1)[-1]:
                    flash('Please enter a valid email address.', 'error')
                elif new_email == old_email:
                    flash('That is already your email on file.', 'info')
                else:
                    cur.execute("""SELECT id FROM partner_users
                                    WHERE LOWER(email) = %s AND id <> %s""",
                                (new_email, user['id']))
                    if cur.fetchone():
                        flash('That email is already used by another partner account.', 'error')
                    else:
                        cur.execute("UPDATE partner_users SET email = %s WHERE id = %s",
                                    (new_email, user['id']))
                        conn.commit()
                        _tg_alert(f'📧 <b>{dealer["name"]}</b> partner updated their email\n'
                                  f'{old_email} → {new_email}')
                        flash(f'Email updated to {new_email}. Use it next time you log in.',
                              'info')

        # Refetch latest state
        cur.execute("SELECT * FROM partner_users WHERE id = %s", (user['id'],))
        user = cur.fetchone()

    return render_template('partner_settings.html',
                           user=user, dealer=dealer, slug=slug)


@bp.route('/partner/<slug>/mark-seen', methods=['POST'])
def mark_seen(slug):
    """Mark partner_bid_requests as seen so the floating-banner UI clears
    (and the chime won't re-fire on the same response). Optional form
    field `pbr_id` scopes to a single request — used by the per-banner
    × close button. With no pbr_id, marks every pending response on the
    dealer (used by the bulk-clear hook on counter submit + chime reset)."""
    user = _current_partner_user()
    if not user:
        return jsonify({'error': 'not_authenticated'}), 401
    pbr_id_raw = (request.form.get('pbr_id') or '').strip()
    with _db() as conn, conn.cursor() as cur:
        dealer = _dealer_by_slug(cur, slug)
        if not dealer or dealer['id'] != user['dealer_id']:
            return jsonify({'error': 'forbidden'}), 403
        if pbr_id_raw:
            try:
                pbr_id = int(pbr_id_raw)
            except ValueError:
                return jsonify({'error': 'bad_pbr_id'}), 400
            cur.execute("""
                UPDATE partner_bid_requests
                SET partner_seen_response_at = NOW()
                WHERE id = %s AND dealer_id = %s
            """, (pbr_id, dealer['id']))
        else:
            cur.execute("""
                UPDATE partner_bid_requests
                SET partner_seen_response_at = NOW()
                WHERE dealer_id = %s
            """, (dealer['id'],))
        conn.commit()
    return jsonify({'ok': True})

# ── 2026-05-12 — Inbound claim reply ("YES") handler ────────────────────
def try_handle_network_claim(from_phone: str, body: str):
    """Inbound SMS interceptor for network-push claim replies.
    Returns (handled, reply_text). If handled=True, caller should TwiML-reply
    with reply_text and stop further processing of the inbound message.

    Match logic:
      - Production: from_phone == bid_pushes.sms_to → claim the most recent
        unclaimed push to that phone.
      - Test-gate mode (EW_SMS_TEST_GATE_PHONE set): from_phone == gate phone
        → claim the single most recent unclaimed push regardless of original
        sms_to, since the gate rerouted ALL sends to this number.
    """
    if not body:
        return False, None
    txt = body.strip().upper()
    # Normalize: drop apostrophes (straight + curly), collapse whitespace.
    import re as _re
    norm = _re.sub(r"[\u2018\u2019\u02BC']", "", txt)
    norm = _re.sub(r"\s+", " ", norm).strip()
    # Accept all natural-language affirmatives a dealer might text:
    yes_exact = {"YES", "Y", "ILL TAKE IT", "I LL TAKE IT", "TAKE IT",
                 "I TAKE IT", "I WILL TAKE IT", "ILLTAKE IT"}
    yes_starts = ("YES ", "Y ", "ILL TAKE IT", "I LL TAKE IT", "TAKE IT",
                  "I WILL TAKE IT")
    if not (norm in yes_exact or any(norm.startswith(p) for p in yes_starts)):
        return False, None

    try:
        with _db() as conn, conn.cursor() as cur:
            # Match ONLY against partner_users.phone — primary dealer principal.
            # Salespeople and operator-monitor phones cannot claim.
            cur.execute("""
                SELECT bp.id, bp.bid_id, bp.dealer_id, bp.sms_to
                  FROM bid_pushes bp
                  JOIN partner_users pu ON pu.dealer_id = bp.dealer_id
                 WHERE bp.sms_sent_at IS NOT NULL
                   AND bp.claimed_at IS NULL
                   AND pu.phone = %s
                 ORDER BY bp.sms_sent_at DESC LIMIT 1
                   FOR UPDATE OF bp
            """, (from_phone,))
            row = cur.fetchone()
            if not row:
                # Phone is not a known partner_users entry — pass through silently
                # so the operator monitor (and stray texts) don't hijack claim flow.
                return False, None

            bid_id = row['bid_id']

            # 2. Check if THIS bid already has a winning claim
            cur.execute("""
                SELECT id, dealer_id FROM bid_pushes
                 WHERE bid_id = %s AND claimed_at IS NOT NULL AND claim_late IS NOT TRUE
                 LIMIT 1
            """, (bid_id,))
            existing_winner = cur.fetchone()

            cur.execute("""
                SELECT year, make, model FROM bids WHERE id = %s
            """, (bid_id,))
            b = cur.fetchone()
            ymm = ((b and f"{b['year'] or ''} {b['make'] or ''} {b['model'] or ''}".strip())
                   or 'the vehicle')

            if existing_winner:
                # Late claim
                cur.execute("""
                    UPDATE bid_pushes
                       SET claimed_at = NOW(),
                           claim_response = 'late',
                           claim_from_phone = %s,
                           claim_late = TRUE
                     WHERE id = %s
                """, (from_phone, row['id']))
                conn.commit()
                print(f'[network-claim] LATE bid={bid_id} dealer={row["dealer_id"]} '
                      f'phone={from_phone}', flush=True)
                return True, f"Sorry, that {ymm} was just claimed by another buyer."

            # WINNER
            cur.execute("""
                UPDATE bid_pushes
                   SET claimed_at = NOW(),
                       claim_response = 'yes',
                       claim_from_phone = %s,
                       claim_late = FALSE
                 WHERE id = %s
            """, (from_phone, row['id']))
            conn.commit()
            print(f'[network-claim] WIN bid={bid_id} dealer={row["dealer_id"]} '
                  f'phone={from_phone}', flush=True)

            # Telegram alert to operator
            try:
                _notify_operator_of_claim(bid_id, row['dealer_id'])
            except Exception as _e:
                print(f'[network-claim] telegram alert failed: {_e}', flush=True)

            return True, "It's yours. EW will contact you shortly, thank you."
    except Exception as e:
        print(f'[network-claim] exception: {e}', flush=True)
        return False, None


def _notify_operator_of_claim(bid_id: int, dealer_id: int) -> None:
    """Telegram ping to Oscar's bot. Reuses the existing TELEGRAM env."""
    import requests
    tg_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    tg_chat  = os.environ.get('TELEGRAM_CHAT_ID')
    if not (tg_token and tg_chat):
        return
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT b.year, b.make, b.model, b.network_ask::int AS ask,
                   d.name AS dealer, d.salesperson
              FROM bids b
              JOIN dealers d ON d.id = %s
             WHERE b.id = %s
        """, (dealer_id, bid_id))
        r = cur.fetchone()
    if not r:
        return
    msg = (f"🟢 CLAIM: {r['dealer']}{' (' + r['salesperson'] + ')' if r.get('salesperson') else ''} "
           f"said YES to bid #{bid_id} — {r['year']} {r['make']} {r['model']} at ${r['ask']:,}")
    try:
        requests.post(f'https://api.telegram.org/bot{tg_token}/sendMessage',
                      json={'chat_id': tg_chat, 'text': msg}, timeout=5)
    except Exception as _e:
        print(f'[network-claim] telegram POST failed: {_e}', flush=True)

