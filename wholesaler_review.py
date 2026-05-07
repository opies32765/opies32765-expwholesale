"""Wholesaler Submission Review Queue.

Wholesaler-mode partner dealers (no scraped site) submit vehicles through
Quick Drop / mobile. Those bids land with `review_status='pending'` and are
invisible to the EW Buy Center until the assigned salesperson approves
them from /wholesaler-<reviewer>.

Routes:
  /wholesaler-<reviewer>                       — pending queue (admin auth)
  /wholesaler-<reviewer>/<bid_id>/approve      — push to Buy Center
  /wholesaler-<reviewer>/<bid_id>/reject       — kill, optional reason
  /wholesaler-<reviewer>/<bid_id>/edit-asking  — adjust asking_price before approve

Auth: relies on EW admin login (session['logged_in']). The path is NOT
under /admin/ on purpose — user wants to be able to clone the route per
salesperson without naming things 'admin'. EW's global before_request
hook still gates anything outside _PUBLIC_PREFIXES, so /wholesaler-*
inherits admin auth automatically.

Reviewer mapping is a small dict (`REVIEWERS`) keyed by URL token. To add
another salesperson, drop a new entry — no other code changes needed.
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

bp = Blueprint('wholesaler_review', __name__)


# ── Reviewer config ───────────────────────────────────────────────────────
# `salesperson_match` is a list of substrings (case-insensitive) that match
# against `dealers.salesperson`. A dealer is queued for this reviewer when
# any substring is in the salesperson string. Empty list = match all (used
# for /wholesaler-all if you want a master queue).
REVIEWERS = {
    'oscar': {
        'display_name': 'Oscar',
        'salesperson_match': ['oscar'],
        # Public signup-page contact card + outbound salesperson_phone on
        # auto-provisioned wholesaler dealers (so this reviewer gets the
        # SMS when one of their wholesalers submits a bid).
        'full_name': 'Oscar Pastrana',
        'email': 'oscar@experience-wholesale.com',
        'phone': '+14074309675',
        'phone_display': '(407) 430-9675',
    },
    # 'jordan': {
    #     'display_name': 'Jordan',
    #     'salesperson_match': ['jordan'],
    #     'full_name': 'Jordan ...',
    #     'email': '...',
    #     'phone': '+1...',
    #     'phone_display': '(...) ...-....',
    # },
}


# In-process IP rate limit for the public /signup endpoint. One signup per
# IP per hour. Cheap, dies on restart — fine for a low-traffic public form.
_SIGNUP_RATE_LIMIT_SECONDS = 3600
_signup_last_ip = {}  # {ip: epoch_seconds}


def _db():
    db_url = os.environ.get('DATABASE_URL', 'postgresql:///expwholesale')
    conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def _require_admin():
    """Mirror app.py's auth gate. The global before_request hook already
    blocks unauthenticated requests, but this is defense-in-depth in case
    /wholesaler- is ever added to _PUBLIC_PREFIXES by accident."""
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return None


def _resolve_reviewer(slug: str):
    cfg = REVIEWERS.get((slug or '').lower())
    if not cfg:
        return None
    return cfg


def _salesperson_filter_sql(matches: list[str]):
    """Build a WHERE-clause fragment + params that selects bids whose
    dealer is assigned to a salesperson matching any of `matches`. Returns
    (sql_fragment, params). If matches is empty, returns ('TRUE', [])
    (no salesperson filter — used for an 'all reviewers' queue)."""
    if not matches:
        return 'TRUE', []
    likes = []
    params = []
    for m in matches:
        likes.append('LOWER(b.salesperson) LIKE %s')
        params.append(f'%{m.lower()}%')
    return '(' + ' OR '.join(likes) + ')', params


# ── Buy-Center-style dashboard (primary view) ─────────────────────────────
@bp.route('/wholesaler-<reviewer>')
def dashboard(reviewer):
    """Mimics the EW Buy Center layout — sortable table, photo thumbnails,
    vA badges, live worker chips, 3s polling — scoped to wholesaler-pending
    bids assigned to this reviewer. Per-row Approve / Pass actions in
    place of the EW status workflow.

    The card-stack view is preserved at /wholesaler-<reviewer>/queue."""
    redir = _require_admin()
    if redir is not None:
        return redir
    cfg = _resolve_reviewer(reviewer)
    if not cfg:
        abort(404)

    status_filter = (request.args.get('status') or 'pending').lower()
    if status_filter not in ('pending', 'approved', 'rejected', 'all'):
        status_filter = 'pending'
    dealer_filter = (request.args.get('dealer') or 'all').lower()

    sp_sql, sp_params = _salesperson_filter_sql(cfg['salesperson_match'])
    d_sp_sql = sp_sql.replace('b.salesperson', 'd.salesperson')

    # Build review_status WHERE clause + recency window for approved/rejected
    rs_clauses = []
    if status_filter == 'pending':
        rs_clauses.append("b.review_status = 'pending'")
    elif status_filter == 'approved':
        rs_clauses.append("b.review_status = 'approved'")
        rs_clauses.append("b.review_at > NOW() - INTERVAL '7 days'")
    elif status_filter == 'rejected':
        rs_clauses.append("b.review_status = 'rejected'")
        rs_clauses.append("b.review_at > NOW() - INTERVAL '7 days'")
    else:  # 'all'
        rs_clauses.append("b.review_status IS NOT NULL")
    if dealer_filter not in (None, '', 'all'):
        try:
            int(dealer_filter)
            rs_clauses.append("b.partner_dealer_id = %s")
        except ValueError:
            dealer_filter = 'all'

    rs_sql = ' AND '.join(rs_clauses)

    # Param order must match the WHERE clause: rs_clauses (which may
    # include partner_dealer_id) come BEFORE sp_sql in the SQL, so their
    # values come first in the param list.
    bid_params = []
    if dealer_filter not in (None, '', 'all'):
        bid_params.append(int(dealer_filter))
    bid_params.extend(sp_params)

    with _db() as conn, conn.cursor() as cur:
        # Bids — same shape as /api/bids, scoped to this reviewer.
        cur.execute(f"""
            SELECT b.id, b.phone, b.vin, b.year, b.make, b.model, b.mileage,
                   b.raw_message, b.status, b.created_at, b.bid_amount,
                   b.ai_price, b.asking_price, b.has_unread,
                   b.partner_dealer_id, b.partner_request_id, b.salesperson,
                   b.review_status, b.review_by, b.review_at, b.review_note,
                   d.name AS partner_dealer_name
            FROM bids b
            LEFT JOIN dealers d ON b.partner_dealer_id = d.id
            WHERE {rs_sql}
              AND {sp_sql}
            ORDER BY b.created_at DESC
            LIMIT 200
        """, bid_params)
        bids = cur.fetchall()

        # Photo counts + first photo per bid (mirror /api/bids).
        bid_ids = [b['id'] for b in bids]
        photo_counts = {}
        first_photos = {}
        if bid_ids:
            cur.execute("""SELECT bid_id, COUNT(*) AS cnt FROM bid_photos
                           WHERE bid_id = ANY(%s) GROUP BY bid_id""", (bid_ids,))
            photo_counts = {r['bid_id']: int(r['cnt']) for r in cur.fetchall()}
            cur.execute("""SELECT DISTINCT ON (bid_id) bid_id,
                                  COALESCE(local_path, url) AS src
                           FROM bid_photos WHERE bid_id = ANY(%s)
                           ORDER BY bid_id, id""", (bid_ids,))
            first_photos = {r['bid_id']: r['src'] for r in cur.fetchall()}

        # vA-done set — same definition as Buy Center (vauto + accutrade row exist)
        vauto_done = set()
        if bid_ids:
            cur.execute("""SELECT v.bid_id FROM vauto_lookups v
                           JOIN accutrade_lookups a ON a.bid_id = v.bid_id
                           WHERE v.bid_id = ANY(%s)""", (bid_ids,))
            vauto_done = {r['bid_id'] for r in cur.fetchall()}

        # Active worker chips
        active_workers = {}
        if bid_ids:
            cur.execute("""SELECT DISTINCT ON (bid_id) bid_id, worker_id, job_type,
                                  status, claimed_at, completed_at
                           FROM worker_jobs WHERE bid_id = ANY(%s)
                           ORDER BY bid_id, claimed_at DESC""", (bid_ids,))
            for r in cur.fetchall():
                active_workers.setdefault(r['bid_id'], []).append({
                    'worker_id': r['worker_id'],
                    'job_type': r['job_type'],
                    'status': r.get('status', ''),
                    'completed': r.get('completed_at') is not None,
                })

        # Stats — pending count, today's pending, approved-7d, rejected-7d, wholesalers count
        stats = {}
        cur.execute(f"""SELECT COUNT(*) AS cnt FROM bids b
                        WHERE b.review_status = 'pending' AND {sp_sql}""", sp_params)
        stats['pending'] = int(cur.fetchone()['cnt'])

        cur.execute(f"""SELECT COUNT(*) AS cnt FROM bids b
                        WHERE b.review_status = 'pending'
                          AND b.created_at::date = CURRENT_DATE
                          AND {sp_sql}""", sp_params)
        stats['today'] = int(cur.fetchone()['cnt'])

        cur.execute(f"""SELECT COUNT(*) AS cnt FROM bids b
                        WHERE b.review_status = 'approved'
                          AND b.review_at > NOW() - INTERVAL '7 days'
                          AND {sp_sql}""", sp_params)
        stats['approved_7d'] = int(cur.fetchone()['cnt'])

        cur.execute(f"""SELECT COUNT(*) AS cnt FROM bids b
                        WHERE b.review_status = 'rejected'
                          AND b.review_at > NOW() - INTERVAL '7 days'
                          AND {sp_sql}""", sp_params)
        stats['rejected_7d'] = int(cur.fetchone()['cnt'])

        cur.execute(f"""SELECT COUNT(*) AS cnt FROM dealers d
                        WHERE d.portal_mode = 'wholesaler'
                          AND d.active = TRUE
                          AND {d_sp_sql}""", sp_params)
        stats['wholesalers'] = int(cur.fetchone()['cnt'])

        # Signup-page traffic for this reviewer (today, ET-anchored).
        cur.execute("""SELECT
                         COUNT(*)                                          AS hits,
                         COUNT(DISTINCT ip)                                AS unique_ips,
                         COUNT(*) FILTER (WHERE outcome = 'submit_ok')     AS submits
                       FROM signup_visits
                       WHERE reviewer = %s
                         AND hit_at::date = CURRENT_DATE""", (reviewer,))
        sv_row = cur.fetchone() or {}
        stats['signup_hits_today']    = int(sv_row.get('hits') or 0)
        stats['signup_unique_today']  = int(sv_row.get('unique_ips') or 0)
        stats['signup_submits_today'] = int(sv_row.get('submits') or 0)

        # Wholesaler dropdown options
        cur.execute(f"""SELECT d.id, d.name FROM dealers d
                        WHERE d.portal_mode = 'wholesaler'
                          AND d.active = TRUE
                          AND {d_sp_sql}
                        ORDER BY d.name""", sp_params)
        wholesalers = cur.fetchall()

    from app import time_ago
    return render_template('wholesaler_dashboard.html',
                           reviewer_slug=reviewer,
                           reviewer_name=cfg['display_name'],
                           bids=bids,
                           stats=stats,
                           status_filter=status_filter,
                           dealer_filter=dealer_filter,
                           wholesalers=wholesalers,
                           photo_counts=photo_counts,
                           first_photos=first_photos,
                           vauto_done=vauto_done,
                           active_workers=active_workers,
                           time_ago=time_ago)


# ── Polling endpoint for the dashboard table ─────────────────────────────
@bp.route('/api/wholesaler-<reviewer>/bids')
def api_bids(reviewer):
    redir = _require_admin()
    if redir is not None:
        return redir
    cfg = _resolve_reviewer(reviewer)
    if not cfg:
        abort(404)

    since_id = request.args.get('since_id', 0, type=int)
    status_filter = (request.args.get('status') or 'pending').lower()
    if status_filter not in ('pending', 'approved', 'rejected', 'all'):
        status_filter = 'pending'
    dealer_filter = (request.args.get('dealer') or 'all').lower()

    sp_sql, sp_params = _salesperson_filter_sql(cfg['salesperson_match'])
    d_sp_sql = sp_sql.replace('b.salesperson', 'd.salesperson')

    rs_clauses = []
    if status_filter == 'pending':
        rs_clauses.append("b.review_status = 'pending'")
    elif status_filter == 'approved':
        rs_clauses.append("b.review_status = 'approved'")
        rs_clauses.append("b.review_at > NOW() - INTERVAL '7 days'")
    elif status_filter == 'rejected':
        rs_clauses.append("b.review_status = 'rejected'")
        rs_clauses.append("b.review_at > NOW() - INTERVAL '7 days'")
    else:
        rs_clauses.append("b.review_status IS NOT NULL")
    bid_params = []
    if dealer_filter not in (None, '', 'all'):
        try:
            bid_params.append(int(dealer_filter))
            rs_clauses.append("b.partner_dealer_id = %s")
        except ValueError:
            pass
    bid_params.extend(sp_params)

    rs_sql = ' AND '.join(rs_clauses)

    with _db() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT b.id, b.phone, b.vin, b.year, b.make, b.model, b.mileage,
                   b.raw_message, b.status, b.created_at, b.bid_amount,
                   b.ai_price, b.asking_price, b.has_unread,
                   b.partner_dealer_id, b.partner_request_id, b.salesperson,
                   b.review_status, b.review_by, b.review_at, b.review_note,
                   d.name AS partner_dealer_name
            FROM bids b
            LEFT JOIN dealers d ON b.partner_dealer_id = d.id
            WHERE {rs_sql} AND {sp_sql}
            ORDER BY b.created_at DESC LIMIT 200
        """, bid_params)
        rows = cur.fetchall()

        bid_list = []
        for r in rows:
            bid_list.append({
                'id': r['id'],
                'phone': r['phone'],
                'vin': r['vin'],
                'year': r['year'], 'make': r['make'], 'model': r['model'],
                'mileage': r['mileage'],
                'raw_message': r['raw_message'],
                'status': r['status'],
                'created_at': r['created_at'].isoformat() if r['created_at'] else None,
                'asking_price': float(r['asking_price']) if r['asking_price'] else None,
                'ai_price': float(r['ai_price']) if r['ai_price'] else None,
                'bid_amount': float(r['bid_amount']) if r['bid_amount'] else None,
                'partner_dealer_id': r.get('partner_dealer_id'),
                'partner_dealer_name': r.get('partner_dealer_name'),
                'partner_request_id': r.get('partner_request_id'),
                'salesperson': r.get('salesperson'),
                'review_status': r.get('review_status'),
                'review_by': r.get('review_by'),
                'review_at': r['review_at'].isoformat() if r.get('review_at') else None,
                'review_note': r.get('review_note'),
                'is_new': r['id'] > since_id,
                'has_unread': bool(r.get('has_unread')),
            })

        bid_ids = [b['id'] for b in bid_list]
        photo_counts = {}
        first_photos = {}
        if bid_ids:
            cur.execute("""SELECT bid_id, COUNT(*) AS cnt FROM bid_photos
                           WHERE bid_id = ANY(%s) GROUP BY bid_id""", (bid_ids,))
            photo_counts = {r['bid_id']: int(r['cnt']) for r in cur.fetchall()}
            cur.execute("""SELECT DISTINCT ON (bid_id) bid_id,
                                  COALESCE(local_path, url) AS src
                           FROM bid_photos WHERE bid_id = ANY(%s)
                           ORDER BY bid_id, id""", (bid_ids,))
            first_photos = {r['bid_id']: r['src'] for r in cur.fetchall()}

        vauto_done = []
        if bid_ids:
            cur.execute("""SELECT v.bid_id FROM vauto_lookups v
                           JOIN accutrade_lookups a ON a.bid_id = v.bid_id
                           WHERE v.bid_id = ANY(%s)""", (bid_ids,))
            vauto_done = [r['bid_id'] for r in cur.fetchall()]

        active_workers = {}
        if bid_ids:
            cur.execute("""SELECT DISTINCT ON (bid_id) bid_id, worker_id, job_type,
                                  status, claimed_at, completed_at
                           FROM worker_jobs WHERE bid_id = ANY(%s)
                           ORDER BY bid_id, claimed_at DESC""", (bid_ids,))
            for r in cur.fetchall():
                active_workers.setdefault(r['bid_id'], []).append({
                    'worker_id': r['worker_id'],
                    'job_type': r['job_type'],
                    'status': r.get('status', ''),
                    'completed': r.get('completed_at') is not None,
                })

        stats = {}
        cur.execute(f"""SELECT COUNT(*) AS cnt FROM bids b
                        WHERE b.review_status = 'pending' AND {sp_sql}""", sp_params)
        stats['pending'] = int(cur.fetchone()['cnt'])
        cur.execute(f"""SELECT COUNT(*) AS cnt FROM bids b
                        WHERE b.review_status = 'pending'
                          AND b.created_at::date = CURRENT_DATE
                          AND {sp_sql}""", sp_params)
        stats['today'] = int(cur.fetchone()['cnt'])
        cur.execute(f"""SELECT COUNT(*) AS cnt FROM bids b
                        WHERE b.review_status = 'approved'
                          AND b.review_at > NOW() - INTERVAL '7 days'
                          AND {sp_sql}""", sp_params)
        stats['approved_7d'] = int(cur.fetchone()['cnt'])
        cur.execute(f"""SELECT COUNT(*) AS cnt FROM bids b
                        WHERE b.review_status = 'rejected'
                          AND b.review_at > NOW() - INTERVAL '7 days'
                          AND {sp_sql}""", sp_params)
        stats['rejected_7d'] = int(cur.fetchone()['cnt'])
        cur.execute(f"""SELECT COUNT(*) AS cnt FROM dealers d
                        WHERE d.portal_mode = 'wholesaler' AND d.active = TRUE
                          AND {d_sp_sql}""", sp_params)
        stats['wholesalers'] = int(cur.fetchone()['cnt'])

    return jsonify({
        'bids': bid_list,
        'stats': stats,
        'photo_counts': photo_counts,
        'first_photos': first_photos,
        'vauto_done': vauto_done,
        'active_workers': active_workers,
    })


# ── Card-stack queue (alternate view, kept as fallback) ───────────────────
@bp.route('/wholesaler-<reviewer>/queue')
def queue(reviewer):
    redir = _require_admin()
    if redir is not None:
        return redir
    cfg = _resolve_reviewer(reviewer)
    if not cfg:
        abort(404)

    sp_sql, sp_params = _salesperson_filter_sql(cfg['salesperson_match'])

    # Pending = waiting for review. Recent decisions = approved/rejected
    # in the last 7d so the reviewer can see their own activity.
    with _db() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT b.id, b.vin, b.year, b.make, b.model, b.trim, b.mileage,
                   b.asking_price, b.ai_price, b.notes, b.raw_message,
                   b.salesperson, b.partner_dealer_id, b.created_at,
                   d.name AS dealer_name, d.brand AS dealer_brand,
                   (SELECT COUNT(*) FROM bid_photos bp WHERE bp.bid_id = b.id) AS photo_count,
                   (SELECT COALESCE(local_path, url) FROM bid_photos bp
                      WHERE bp.bid_id = b.id ORDER BY id LIMIT 1) AS first_photo,
                   v.bid_id IS NOT NULL AS vauto_done,
                   v.mmr, v.black_book, v.kbb, v.kbb_com, v.jd_power, v.rbook
            FROM bids b
            LEFT JOIN dealers d ON d.id = b.partner_dealer_id
            LEFT JOIN vauto_lookups v ON v.bid_id = b.id
            WHERE b.review_status = 'pending'
              AND {sp_sql}
            ORDER BY b.created_at ASC
        """, sp_params)
        pending = cur.fetchall()

        cur.execute(f"""
            SELECT b.id, b.vin, b.year, b.make, b.model, b.asking_price,
                   b.review_status, b.review_at, b.review_by, b.review_note,
                   b.status, b.bid_amount,
                   d.name AS dealer_name
            FROM bids b
            LEFT JOIN dealers d ON d.id = b.partner_dealer_id
            WHERE b.review_status IN ('approved', 'rejected')
              AND b.review_at > NOW() - INTERVAL '7 days'
              AND {sp_sql}
            ORDER BY b.review_at DESC
            LIMIT 50
        """, sp_params)
        recent = cur.fetchall()

    return render_template('wholesaler_review.html',
                           reviewer_slug=reviewer,
                           reviewer_name=cfg['display_name'],
                           pending=pending,
                           recent=recent)


# ── Approve ──────────────────────────────────────────────────────────────
@bp.route('/wholesaler-<reviewer>/<int:bid_id>/approve', methods=['POST'])
def approve(reviewer, bid_id):
    try:
        return _approve_impl(reviewer, bid_id)
    except Exception as e:
        # Return JSON so the front-end can surface a meaningful message
        # instead of choking on the Flask HTML 500 page.
        import traceback
        print(f'[wholesaler_review.approve] {type(e).__name__}: {e}\n{traceback.format_exc()}', flush=True)
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500


def _approve_impl(reviewer, bid_id):
    redir = _require_admin()
    if redir is not None:
        return redir
    cfg = _resolve_reviewer(reviewer)
    if not cfg:
        abort(404)

    # Optional asking-price adjustment before push to Buy Center. If the
    # reviewer wants to change the wholesaler's number before bidders see
    # it, post `asking_price=NEW`.
    new_ask_raw = (request.form.get('asking_price') or '').replace(',', '').replace('$', '').strip()
    new_ask = None
    if new_ask_raw:
        try:
            new_ask = float(new_ask_raw)
            if new_ask <= 0 or new_ask > 10_000_000:
                raise ValueError
        except ValueError:
            return jsonify({'error': 'Invalid asking_price'}), 400

    sp_sql, sp_params = _salesperson_filter_sql(cfg['salesperson_match'])
    # UPDATE has no `b` alias — strip it so the salesperson filter targets
    # the bare column on the table being updated.
    sp_sql_bare = sp_sql.replace('b.salesperson', 'salesperson')

    with _db() as conn, conn.cursor() as cur:
        # Match the salesperson filter so reviewers can only approve bids
        # they own. RETURNING guards against double-approval races.
        # Approve = the deal is done (bought). Flip status to 'bought' so it
        # surfaces in the EW Buy Center as a closed deal record. The
        # negotiation already happened in the wholesaler dashboard via
        # /api/bid/<id>/reply counters.
        if new_ask is not None:
            cur.execute(f"""
                UPDATE bids
                SET review_status = 'approved',
                    status        = 'bought',
                    review_by     = %s,
                    review_at     = NOW(),
                    bid_amount    = COALESCE(%s, bid_amount),
                    updated_at    = NOW()
                WHERE id = %s
                  AND review_status = 'pending'
                  AND {sp_sql_bare}
                RETURNING id, vin, partner_dealer_id, partner_request_id
            """, [cfg['display_name'], new_ask, bid_id] + sp_params)
        else:
            cur.execute(f"""
                UPDATE bids
                SET review_status = 'approved',
                    status        = 'bought',
                    review_by     = %s,
                    review_at     = NOW(),
                    updated_at    = NOW()
                WHERE id = %s
                  AND review_status = 'pending'
                  AND {sp_sql_bare}
                RETURNING id, vin, partner_dealer_id, partner_request_id
            """, [cfg['display_name'], bid_id] + sp_params)
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Not found, not pending, or not assigned to you'}), 404
        conn.commit()

    # Telegram notify (best-effort) — reviewer pushed it through.
    try:
        from partner_portal import _tg_alert
        _tg_alert(f'✅ <b>{cfg["display_name"]}</b> approved Bid #{bid_id}'
                  + (f' · adjusted ask ${int(new_ask):,}' if new_ask else ''))
    except Exception:
        pass

    # Owner-portal push fan-out fires now that the bid is visible.
    # _fire_owner_new_bid was suppressed at create time for pending bids.
    try:
        from app import _fire_owner_new_bid
        _fire_owner_new_bid(bid_id)
    except Exception:
        pass

    return jsonify({'ok': True, 'bid_id': bid_id})


# ── Reject ────────────────────────────────────────────────────────────────
@bp.route('/wholesaler-<reviewer>/<int:bid_id>/reject', methods=['POST'])
def reject(reviewer, bid_id):
    try:
        return _reject_impl(reviewer, bid_id)
    except Exception as e:
        import traceback
        print(f'[wholesaler_review.reject] {type(e).__name__}: {e}\n{traceback.format_exc()}', flush=True)
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500


def _reject_impl(reviewer, bid_id):
    redir = _require_admin()
    if redir is not None:
        return redir
    cfg = _resolve_reviewer(reviewer)
    if not cfg:
        abort(404)

    note = (request.form.get('note') or '').strip()[:500] or None

    sp_sql, sp_params = _salesperson_filter_sql(cfg['salesperson_match'])
    sp_sql_bare = sp_sql.replace('b.salesperson', 'salesperson')

    with _db() as conn, conn.cursor() as cur:
        cur.execute(f"""
            UPDATE bids
            SET review_status = 'rejected',
                review_by     = %s,
                review_at     = NOW(),
                review_note   = %s,
                status        = 'passed',
                updated_at    = NOW()
            WHERE id = %s
              AND review_status = 'pending'
              AND {sp_sql_bare}
            RETURNING id, vin, partner_dealer_id, phone
        """, [cfg['display_name'], note, bid_id] + sp_params)
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Not found, not pending, or not assigned to you'}), 404

        # If a reason was given, drop it into the bid message thread so the
        # wholesaler sees it on their dashboard. Outbound = from EW's
        # perspective, which is what the partner thread renders as
        # "EW responded".
        if note:
            cur.execute("""
                INSERT INTO bid_messages (bid_id, direction, message,
                                          from_phone, to_phone)
                VALUES (%s, 'outbound', %s, %s, %s)
            """, (bid_id, f'Pass — {note}', 'EW', row.get('phone') or 'wholesaler'))

        conn.commit()

    try:
        from partner_portal import _tg_alert
        _tg_alert(f'❌ <b>{cfg["display_name"]}</b> rejected Bid #{bid_id}'
                  + (f'\n{note}' if note else ''))
    except Exception:
        pass

    return jsonify({'ok': True, 'bid_id': bid_id})


# ── Wholesaler list ──────────────────────────────────────────────────────
@bp.route('/wholesaler-<reviewer>/wholesalers')
def wholesaler_list(reviewer):
    redir = _require_admin()
    if redir is not None:
        return redir
    cfg = _resolve_reviewer(reviewer)
    if not cfg:
        abort(404)

    sp_sql, sp_params = _salesperson_filter_sql(cfg['salesperson_match'])
    # _salesperson_filter_sql aliases on `b.` — rewrite for the dealers query.
    d_sp_sql = sp_sql.replace('b.salesperson', 'd.salesperson')

    with _db() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.id, d.name, d.portal_slug, d.salesperson,
                   d.salesperson_phone, d.dashboard_token, d.mobile_token,
                   d.brand, d.created_at,
                   (SELECT COUNT(*) FROM bids b
                      WHERE b.partner_dealer_id = d.id
                        AND b.review_status = 'pending') AS pending_count,
                   (SELECT COUNT(*) FROM bids b
                      WHERE b.partner_dealer_id = d.id
                        AND b.review_status = 'approved') AS approved_count,
                   (SELECT MAX(b.created_at) FROM bids b
                      WHERE b.partner_dealer_id = d.id) AS last_submission_at
            FROM dealers d
            WHERE d.portal_mode = 'wholesaler'
              AND d.active = TRUE
              AND {d_sp_sql}
            ORDER BY d.name ASC
        """, sp_params)
        wholesalers = cur.fetchall()

    return render_template('wholesaler_list.html',
                           reviewer_slug=reviewer,
                           reviewer_name=cfg['display_name'],
                           wholesalers=wholesalers,
                           portal_base=os.environ.get('PORTAL_BASE',
                                                      'https://experience-wholesale.net'))


# ── Onboard a new wholesaler ─────────────────────────────────────────────
def _slugify(name: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '', (name or '').lower())
    return s[:32] or f'w{secrets.token_hex(3)}'


def _normalize_phone(raw: str) -> str | None:
    if not raw:
        return None
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 10:
        return f'+1{digits}'
    if len(digits) == 11 and digits.startswith('1'):
        return f'+{digits}'
    return None


@bp.route('/wholesaler-<reviewer>/new', methods=['GET', 'POST'])
def onboard(reviewer):
    redir = _require_admin()
    if redir is not None:
        return redir
    cfg = _resolve_reviewer(reviewer)
    if not cfg:
        abort(404)

    if request.method == 'GET':
        return render_template('wholesaler_onboard.html',
                               reviewer_slug=reviewer,
                               reviewer_name=cfg['display_name'])

    # POST — create the wholesaler.
    name = (request.form.get('name') or '').strip()
    if not name or len(name) > 200:
        return jsonify({'error': 'Name is required (max 200 chars).'}), 400

    # salesperson_phone is the number that gets SMS'd on every wholesaler
    # bid submission (via partner_portal._notify_salesperson). Default to
    # the reviewer's own phone from REVIEWERS so the alert reaches the
    # reviewer, not the wholesaler. Form override still works if the
    # reviewer wants to delegate to another rep.
    phone = _normalize_phone(request.form.get('phone')) or cfg.get('phone')
    primary_color = (request.form.get('primary_color') or '').strip() or '#0D68C5'
    if not re.match(r'^#[0-9a-fA-F]{3,6}$', primary_color):
        primary_color = '#0D68C5'
    logo_url = (request.form.get('logo_url') or '').strip() or None
    if logo_url and not logo_url.lower().startswith(('http://', 'https://')):
        logo_url = None

    # Slug auto-generated from name; collide-safe by appending a short hex.
    base_slug = _slugify(name)
    portal_slug = base_slug

    dashboard_token = secrets.token_urlsafe(16)
    mobile_token = secrets.token_urlsafe(16)

    brand = {'primary_color': primary_color}
    if logo_url:
        brand['logo_url'] = logo_url

    with _db() as conn, conn.cursor() as cur:
        # Slug uniqueness — append a 4-char hex suffix on collision. Loops
        # rarely; one collision is already astronomically unlikely.
        for attempt in range(5):
            cur.execute("SELECT id FROM dealers WHERE LOWER(portal_slug) = %s",
                        (portal_slug.lower(),))
            if cur.fetchone() is None:
                break
            portal_slug = f'{base_slug}{secrets.token_hex(2)}'
        else:
            return jsonify({'error': 'Could not generate a unique slug.'}), 500

        # Persist. portal_mode='wholesaler' is the on/off switch for the
        # rest of the system: dashboard hides inventory, Quick Drop sets
        # review_status='pending', bid is invisible to /api/bids.
        cur.execute("""
            INSERT INTO dealers
                (name, url, active, portal_mode, portal_slug,
                 salesperson, salesperson_phone,
                 dashboard_token, mobile_token, brand,
                 salesperson_set_at)
            VALUES (%s, NULL, TRUE, 'wholesaler', %s,
                    %s, %s, %s, %s, %s::jsonb, NOW())
            RETURNING id
        """, (name, portal_slug,
              cfg['display_name'], phone,
              dashboard_token, mobile_token,
              psycopg2.extras.Json(brand)))
        dealer_id = cur.fetchone()['id']

        # Placeholder partner_user — required so the dashboard token route
        # can resolve a session without forcing the wholesaler to set a
        # password. Email is a `.invite` placeholder; can be edited later.
        cur.execute("""
            INSERT INTO partner_users (dealer_id, email)
            VALUES (%s, %s)
        """, (dealer_id, f'pending+{portal_slug}@wholesaler.invite'))

        conn.commit()

    portal_base = os.environ.get('PORTAL_BASE', 'https://experience-wholesale.net')
    dashboard_url = f'{portal_base}/partner/{portal_slug}/d/{dashboard_token}'
    mobile_url    = f'{portal_base}/mobile?p={mobile_token}'

    # Telegram so you have a record of the spin-up.
    try:
        from partner_portal import _tg_alert
        _tg_alert(f'➕ <b>{cfg["display_name"]}</b> onboarded wholesaler '
                  f'<b>{name}</b>\nDashboard: {dashboard_url}')
    except Exception:
        pass

    # Honor JSON requests (form posted via fetch) by returning the links.
    if (request.headers.get('Accept') or '').startswith('application/json') \
            or (request.headers.get('X-Requested-With') == 'fetch'):
        return jsonify({
            'ok': True,
            'dealer_id': dealer_id,
            'name': name,
            'portal_slug': portal_slug,
            'dashboard_url': dashboard_url,
            'mobile_url': mobile_url,
        })

    # Otherwise redirect to the wholesaler list with a flash highlight.
    return redirect(url_for('wholesaler_review.wholesaler_list',
                            reviewer=reviewer) + f'#dealer-{dealer_id}')


# ── Signup-page visit log + dashboard ─────────────────────────────────────
# Reviewer-scoped visitor analytics for the public /wholesaler-<reviewer>/signup
# form. Real client IPs (CF-Connecting-IP) get geolocated lazily via
# ip-api.com on demand from the dashboard — no background worker needed.

def _geofill_pending(reviewer: str, max_lookups: int = 50):
    """Resolve country/region/city for any signup_visits rows (for this
    reviewer) where country IS NULL. Uses ip-api.com /batch (free, 45
    req/min, no key, HTTP only). Caps at max_lookups per call so a
    page-load with hundreds of pending IPs doesn't time out."""
    try:
        import json
        import urllib.request
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ip FROM signup_visits
                WHERE reviewer = %s
                  AND country IS NULL
                  AND ip IS NOT NULL
                  AND ip <> ''
                ORDER BY ip
                LIMIT %s
            """, (reviewer, max_lookups))
            ips = [r['ip'] for r in cur.fetchall()]
            if not ips:
                return 0

            payload = json.dumps([{'query': ip,
                                   'fields': 'status,country,regionName,city,query'}
                                  for ip in ips]).encode()
            req = urllib.request.Request(
                'http://ip-api.com/batch',
                data=payload,
                headers={'Content-Type': 'application/json',
                         'User-Agent': 'EW-WholesalerSignup/1.0'},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                results = json.loads(resp.read())

            updated = 0
            for r in results:
                if not isinstance(r, dict):
                    continue
                ip = r.get('query')
                if not ip:
                    continue
                if r.get('status') == 'success':
                    cur.execute("""
                        UPDATE signup_visits
                        SET country = %s, region = %s, city = %s
                        WHERE ip = %s AND country IS NULL
                    """, (r.get('country'), r.get('regionName'), r.get('city'), ip))
                else:
                    # Mark as resolved-but-unknown so we don't keep re-looking-up
                    cur.execute("""
                        UPDATE signup_visits
                        SET country = '?'
                        WHERE ip = %s AND country IS NULL
                    """, (ip,))
                updated += cur.rowcount
            conn.commit()
            return updated
    except Exception as e:
        print(f'[signup_visits] geofill failed: {type(e).__name__}: {e}', flush=True)
        return 0


def _ua_short(ua: str) -> str:
    """Tiny UA pretty-printer. Just the obvious browser/OS hints."""
    if not ua:
        return ''
    ua_l = ua.lower()
    if 'curl/' in ua_l:    return 'curl'
    if 'wget' in ua_l:     return 'wget'
    if 'cloudflare' in ua_l: return 'CF probe'
    if 'bot' in ua_l or 'crawler' in ua_l or 'spider' in ua_l: return 'bot'
    browser = 'browser'
    if 'edg/' in ua_l:        browser = 'Edge'
    elif 'chrome/' in ua_l:   browser = 'Chrome'
    elif 'firefox/' in ua_l:  browser = 'Firefox'
    elif 'safari/' in ua_l and 'chrome/' not in ua_l: browser = 'Safari'
    os_ = ''
    if 'iphone' in ua_l:    os_ = 'iPhone'
    elif 'ipad' in ua_l:    os_ = 'iPad'
    elif 'android' in ua_l: os_ = 'Android'
    elif 'mac os' in ua_l:  os_ = 'Mac'
    elif 'windows' in ua_l: os_ = 'Win'
    elif 'linux' in ua_l:   os_ = 'Linux'
    return f'{browser} · {os_}' if os_ else browser


@bp.route('/wholesaler-<reviewer>/signup-visits')
def signup_visits(reviewer):
    redir = _require_admin()
    if redir is not None:
        return redir
    cfg = _resolve_reviewer(reviewer)
    if not cfg:
        abort(404)

    # Top up geolocation for any pending IPs (best-effort, capped).
    _geofill_pending(reviewer, max_lookups=50)

    with _db() as conn, conn.cursor() as cur:
        # Aggregate stats
        cur.execute("""
            SELECT
              COUNT(*)                                                       AS total,
              COUNT(*) FILTER (WHERE hit_at::date = CURRENT_DATE)            AS today,
              COUNT(*) FILTER (WHERE hit_at > NOW() - INTERVAL '7 days')     AS last_7d,
              COUNT(DISTINCT ip)                                             AS unique_ips_total,
              COUNT(DISTINCT ip) FILTER (WHERE hit_at::date = CURRENT_DATE)  AS unique_ips_today,
              COUNT(*) FILTER (WHERE outcome = 'submit_ok')                  AS submits_total,
              COUNT(*) FILTER (WHERE outcome = 'submit_returning')           AS returning_total,
              COUNT(*) FILTER (WHERE outcome = 'rate_limited')               AS rate_limited
            FROM signup_visits
            WHERE reviewer = %s
        """, (reviewer,))
        agg = cur.fetchone() or {}

        # Top countries / cities (last 30d)
        cur.execute("""
            SELECT country, region, city, COUNT(DISTINCT ip) AS uniq, COUNT(*) AS hits
            FROM signup_visits
            WHERE reviewer = %s
              AND country IS NOT NULL AND country <> '?'
              AND hit_at > NOW() - INTERVAL '30 days'
            GROUP BY country, region, city
            ORDER BY uniq DESC, hits DESC
            LIMIT 15
        """, (reviewer,))
        top_locations = cur.fetchall()

        # Recent visits — last 200
        cur.execute("""
            SELECT id, hit_at, method, ip, user_agent,
                   country, region, city, outcome, dealer_id, error_msg
            FROM signup_visits
            WHERE reviewer = %s
            ORDER BY hit_at DESC
            LIMIT 200
        """, (reviewer,))
        visits = cur.fetchall()

    from app import time_ago
    return render_template(
        'wholesaler_visits.html',
        reviewer_slug=reviewer,
        reviewer_name=cfg['display_name'],
        agg=agg,
        top_locations=top_locations,
        visits=visits,
        ua_short=_ua_short,
        time_ago=time_ago,
    )


# ── Hard-delete a wholesaler (and all their bids/photos/messages) ────────
# Reviewer-scoped: an admin can only delete wholesalers under their own
# salesperson_match. Cascades via FK handle partner_users +
# partner_alert_config + partner_bid_requests (ON DELETE CASCADE on
# dealer_id). Bids are deleted explicitly because bids.partner_dealer_id
# uses ON DELETE SET NULL — without this we'd leave orphan bid rows.
@bp.route('/wholesaler-<reviewer>/wholesaler/<int:dealer_id>/delete',
          methods=['POST'])
def wholesaler_delete(reviewer, dealer_id):
    redir = _require_admin()
    if redir is not None:
        return redir
    cfg = _resolve_reviewer(reviewer)
    if not cfg:
        abort(404)

    sp_sql, sp_params = _salesperson_filter_sql(cfg['salesperson_match'])
    d_sp_sql_bare = sp_sql.replace('b.salesperson', 'salesperson')

    try:
        with _db() as conn, conn.cursor() as cur:
            # Defense in depth: only delete dealers in wholesaler portal mode
            # AND assigned to this reviewer's salesperson.
            cur.execute(f"""
                SELECT id, name FROM dealers
                WHERE id = %s
                  AND portal_mode = 'wholesaler'
                  AND {d_sp_sql_bare}
            """, [dealer_id] + sp_params)
            d = cur.fetchone()
            if not d:
                return jsonify({'error': 'Not found or not your wholesaler.'}), 404

            cur.execute("""
                DELETE FROM bid_messages
                WHERE bid_id IN (SELECT id FROM bids WHERE partner_dealer_id = %s)
            """, (dealer_id,))
            cur.execute("""
                DELETE FROM bid_photos
                WHERE bid_id IN (SELECT id FROM bids WHERE partner_dealer_id = %s)
            """, (dealer_id,))
            cur.execute("DELETE FROM bids WHERE partner_dealer_id = %s",
                        (dealer_id,))
            bids_deleted = cur.rowcount

            cur.execute("DELETE FROM dealers WHERE id = %s", (dealer_id,))
            conn.commit()
    except Exception as e:
        import traceback
        print(f'[wholesaler_review.delete] {type(e).__name__}: {e}\n{traceback.format_exc()}', flush=True)
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500

    try:
        from partner_portal import _tg_alert
        _tg_alert(f'\U0001f5d1️ <b>{cfg["display_name"]}</b> deleted wholesaler '
                  f'<b>{d["name"]}</b> (id={dealer_id}) · {bids_deleted} bid(s) removed')
    except Exception:
        pass

    return jsonify({'ok': True, 'dealer_id': dealer_id,
                    'name': d['name'], 'bids_deleted': bids_deleted})


# ── Public self-serve signup ─────────────────────────────────────────────
# Anyone with the link can sign up — no admin auth. App.py adds
# `/wholesaler-` to _PUBLIC_PREFIXES; the rest of the routes in this file
# remain admin-only via _require_admin().

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _client_ip() -> str:
    # CF-Connecting-IP is the canonical real-client IP when Cloudflare is
    # in front (it's set by CF and stripped from any client-supplied
    # value, so it's safe to trust). Fall back to X-Forwarded-For first
    # hop, then nginx remote_addr.
    cf = (request.headers.get('CF-Connecting-IP') or '').strip()
    if cf:
        return cf
    xff = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return xff or (request.remote_addr or '0.0.0.0')


def _log_visit(reviewer: str, method: str, outcome: str,
               dealer_id: int | None = None, error_msg: str | None = None):
    """Best-effort visit log. Failures are swallowed — this MUST never
    block the signup endpoint. Geolocation columns left NULL; the
    /signup-visits dashboard fills them lazily via ip-api.com."""
    try:
        ua = (request.headers.get('User-Agent') or '')[:500]
        ip = _client_ip()
        with _db() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signup_visits
                    (reviewer, method, ip, user_agent, outcome,
                     dealer_id, error_msg)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (reviewer, method, ip, ua, outcome,
                  dealer_id, (error_msg or '')[:500] or None))
            conn.commit()
    except Exception as e:
        print(f'[signup_visits] log failed: {type(e).__name__}: {e}', flush=True)


@bp.route('/wholesaler-<reviewer>/signup', methods=['GET', 'POST'])
def signup(reviewer):
    cfg = _resolve_reviewer(reviewer)
    if not cfg:
        abort(404)

    if request.method == 'GET':
        _log_visit(reviewer, 'GET', 'view')
        return render_template('wholesaler_signup.html',
                               reviewer_slug=reviewer,
                               reviewer_name=cfg['display_name'],
                               reviewer_full_name=cfg.get('full_name', cfg['display_name']),
                               reviewer_email=cfg.get('email', ''),
                               reviewer_phone_display=cfg.get('phone_display', ''))

    # ── POST ─────────────────────────────────────────────────────────────
    # Note: the IP rate limit lives further down, AFTER the validation +
    # dedupe lookup. That way existing wholesalers can always re-submit
    # this form to recover their portal link — they only get throttled
    # when actually creating a new row. Admins (session.logged_in) bypass
    # the throttle entirely so Oscar can test freely.
    import time
    is_admin = bool(session.get('logged_in'))

    full_name   = (request.form.get('full_name') or '').strip()
    business    = (request.form.get('business_name') or '').strip()
    email       = (request.form.get('email') or '').strip().lower()
    phone_raw   = (request.form.get('phone') or '').strip()
    contact_pref = (request.form.get('contact_pref') or 'both').strip().lower()

    def _bad(msg):
        _log_visit(reviewer, 'POST', 'validation_error', error_msg=msg)
        return jsonify({'error': msg}), 400
    if not full_name or len(full_name) > 120:
        return _bad('Your name is required.')
    if not email or not _EMAIL_RE.match(email) or len(email) > 200:
        return _bad('A valid email is required.')
    phone = _normalize_phone(phone_raw)
    if not phone:
        return _bad('A valid US phone number is required.')
    if contact_pref not in ('sms', 'email', 'both'):
        contact_pref = 'both'
    if business and len(business) > 200:
        return _bad('Business name is too long.')

    # Dealer name = business if provided, otherwise the person's name.
    dealer_name = business or full_name

    sms_opt_in       = contact_pref in ('sms', 'both')
    email_bid_alerts = contact_pref in ('email', 'both')

    base_slug = _slugify(dealer_name)
    portal_slug = base_slug
    dashboard_token = secrets.token_urlsafe(16)
    mobile_token = secrets.token_urlsafe(16)

    # Pull reviewer SMS routing from REVIEWERS — this is what makes the
    # auto-provisioned dealer notify the reviewer (not the wholesaler) on
    # new bid submissions, via dealers.salesperson_phone in
    # partner_portal._notify_salesperson.
    reviewer_phone = cfg.get('phone') or None

    with _db() as conn, conn.cursor() as cur:
        # Dedupe: if either phone or email already exists on a partner_user,
        # return their existing portal link instead of failing. Friendlier
        # for re-signups + prevents accidental dupe dealer rows.
        cur.execute("""
            SELECT pu.dealer_id, d.portal_slug, d.dashboard_token, d.mobile_token, d.name
            FROM partner_users pu
            JOIN dealers d ON d.id = pu.dealer_id
            WHERE LOWER(pu.email) = %s OR pu.phone = %s
            LIMIT 1
        """, (email, phone))
        existing = cur.fetchone()
        if existing and existing.get('dashboard_token'):
            # Returning wholesaler — give them their existing portal back.
            # No rate-limit hit (this isn't a new signup) and no
            # Telegram/SMS noise (Oscar already knows about this one).
            _log_visit(reviewer, 'POST', 'submit_returning',
                       dealer_id=existing['dealer_id'])
            portal_base = os.environ.get('PORTAL_BASE', 'https://experience-wholesale.net')
            return jsonify({
                'ok': True,
                'returning': True,
                'name': existing['name'],
                'portal_slug': existing['portal_slug'],
                'dashboard_url': f'{portal_base}/partner/{existing["portal_slug"]}/d/{existing["dashboard_token"]}',
                'mobile_url':    f'{portal_base}/mobile?p={existing["mobile_token"]}',
            })

        # ── New-signup gate: enforce IP rate limit ONLY when about to
        # actually create a new dealer/partner_user. Returning wholesalers
        # already short-circuited above. Admins bypass entirely.
        ip = _client_ip()
        now = time.time()
        if not is_admin:
            last = _signup_last_ip.get(ip, 0)
            if now - last < _SIGNUP_RATE_LIMIT_SECONDS:
                wait_min = int((_SIGNUP_RATE_LIMIT_SECONDS - (now - last)) / 60) + 1
                _log_visit(reviewer, 'POST', 'rate_limited',
                           error_msg=f'wait {wait_min}m')
                return jsonify({'error': f'Too many signups from this network. Try again in {wait_min} min.'}), 429

        # Slug uniqueness — append short hex on collision.
        for _ in range(5):
            cur.execute("SELECT id FROM dealers WHERE LOWER(portal_slug) = %s",
                        (portal_slug.lower(),))
            if cur.fetchone() is None:
                break
            portal_slug = f'{base_slug}{secrets.token_hex(2)}'
        else:
            return jsonify({'error': 'Could not generate a unique portal URL — please try again.'}), 500

        cur.execute("""
            INSERT INTO dealers
                (name, url, active, portal_mode, portal_slug,
                 salesperson, salesperson_phone,
                 dashboard_token, mobile_token, brand,
                 salesperson_set_at)
            VALUES (%s, NULL, TRUE, 'wholesaler', %s,
                    %s, %s, %s, %s, %s::jsonb, NOW())
            RETURNING id
        """, (dealer_name, portal_slug,
              cfg['display_name'], reviewer_phone,
              dashboard_token, mobile_token,
              psycopg2.extras.Json({'primary_color': '#0D68C5'})))
        dealer_id = cur.fetchone()['id']

        # Real partner_user — full_name + phone + comms prefs all set so the
        # wholesaler doesn't need to visit /settings to start receiving
        # bid responses on their preferred channel.
        cur.execute("""
            INSERT INTO partner_users
                (dealer_id, email, full_name, phone,
                 sms_opt_in, sms_verified_at, email_bid_alerts)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (dealer_id, email, full_name, phone,
              sms_opt_in, datetime.now(timezone.utc) if sms_opt_in else None,
              email_bid_alerts))

        conn.commit()

    if not is_admin:
        _signup_last_ip[ip] = now

    portal_base = os.environ.get('PORTAL_BASE', 'https://experience-wholesale.net')
    dashboard_url = f'{portal_base}/partner/{portal_slug}/d/{dashboard_token}'
    mobile_url    = f'{portal_base}/mobile?p={mobile_token}'

    try:
        from partner_portal import _tg_alert
        _tg_alert(
            f'\U0001f195 <b>New wholesaler signup</b> ({cfg["display_name"]})\n'
            f'<b>{full_name}</b>'
            + (f' · {business}' if business else '')
            + f'\n{phone} · {email}'
            f'\nContact pref: <b>{contact_pref.upper()}</b>'
            f'\nDashboard: {dashboard_url}'
        )
    except Exception:
        pass

    # SMS to the reviewer too (in addition to Telegram). Best-effort —
    # failure here never blocks the signup.
    if reviewer_phone:
        try:
            from app import send_sms
            sms_body = (
                f'New EW wholesaler signup: {full_name}'
                + (f' ({business})' if business else '')
                + f' — {phone} — pref {contact_pref.upper()}'
            )
            send_sms(reviewer_phone, sms_body[:300])
        except Exception:
            pass

    _log_visit(reviewer, 'POST', 'submit_ok', dealer_id=dealer_id)

    return jsonify({
        'ok': True,
        'returning': False,
        'name': dealer_name,
        'portal_slug': portal_slug,
        'dashboard_url': dashboard_url,
        'mobile_url': mobile_url,
        'contact_pref': contact_pref,
    })
