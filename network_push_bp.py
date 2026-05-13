"""
network_push_bp.py — Drift-resistant Flask Blueprint for the Network Push
feature. Holds:
  - Admin buy-profile views (/admin/buy-profiles, detail, preview, rebuild API)
  - Network push API (preview, push, confirm-sold)
  - Template helpers (get_bid_network_sold, get_bid_network_claim,
                      get_network_claims_banner) registered as Jinja globals so
    bid.html and index.html can pull state on every render — even if app.py
    is overwritten by another agent.

Registered by wsgi.py on every worker boot. Same drift-recovery pattern as
the existing wholesaler_review blueprint.
"""
from __future__ import annotations
import os
import os
from flask import Blueprint, render_template, request, session

bp = Blueprint('network_push', __name__, url_prefix='/np')

INBOUND_PUSH_MIN_SCORE = int(os.environ.get('INBOUND_PUSH_MIN_SCORE', '60'))


def _db():
    """Get a DB cursor — lazy import to avoid circular dep on app.py."""
    from app import get_db
    return get_db()


# ============================================================
# ── Scoring helper (mirrors partner_portal._score_bid_for_dealer) ─────────
# ============================================================
def _score(bid: dict, profile: dict | None, dealer_id: int | None = None):
    """Returns (score, reason). score is None for hard skips (VIN-on-lot or
    never-stocks)."""
    if not profile or not (profile.get('makes')):
        return None, 'no profile'
    make = (bid.get('make') or '').upper().strip()
    if not make:
        return None, 'bid missing make'

    vin = (bid.get('vin') or '').strip().upper()
    if vin and dealer_id is not None:
        try:
            db = _db()
            cur = db.cursor()
            cur.execute("""
                SELECT 1 FROM dealer_inventory
                 WHERE dealer_id = %s AND UPPER(vin) = %s AND status = 'active'
                 LIMIT 1
            """, (dealer_id, vin))
            if cur.fetchone():
                db.close()
                return None, f'has this VIN on lot ({vin})'
            db.close()
        except Exception:
            pass

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


# ============================================================
# ── Template helpers — registered as Jinja globals via @app_context_processor
# ============================================================
def _get_bid_network_sold(bid_id):
    try:
        db = _db()
        cur = db.cursor()
        cur.execute("""
            SELECT bp.bid_id, bp.dealer_id, bp.claimed_at, bp.sold_confirmed_at,
                   bp.sold_confirmed_by, bp.score,
                   d.name AS dealer_name, d.salesperson AS dealer_salesperson,
                   b.network_ask::int AS ask,
                   (SELECT COUNT(*) FROM bid_pushes bp2
                     WHERE bp2.bid_id = bp.bid_id AND bp2.sold_confirmed_at IS NOT NULL
                       AND bp2.dealer_id != bp.dealer_id) AS losers_notified
              FROM bid_pushes bp
              JOIN dealers d ON d.id = bp.dealer_id
              JOIN bids b    ON b.id = bp.bid_id
             WHERE bp.bid_id = %s AND bp.sold_confirmed_at IS NOT NULL
               AND bp.claim_late IS NOT TRUE
               AND bp.claimed_at IS NOT NULL
             LIMIT 1
        """, (bid_id,))
        r = cur.fetchone()
        db.close()
        return dict(r) if r else None
    except Exception as _e:
        print(f'[net-helper get_bid_network_sold] {_e}', flush=True)
        return None


def _get_bid_network_claim(bid_id):
    try:
        db = _db()
        cur = db.cursor()
        cur.execute("""
            SELECT bp.bid_id, bp.dealer_id, bp.claimed_at, bp.score, bp.claim_from_phone,
                   d.name AS dealer_name, d.salesperson AS dealer_salesperson,
                   b.network_ask::int AS ask
              FROM bid_pushes bp
              JOIN dealers d ON d.id = bp.dealer_id
              JOIN bids    b ON b.id = bp.bid_id
             WHERE bp.bid_id = %s AND bp.claimed_at IS NOT NULL
               AND bp.claim_late IS NOT TRUE AND bp.sold_confirmed_at IS NULL
             LIMIT 1
        """, (bid_id,))
        r = cur.fetchone()
        db.close()
        return dict(r) if r else None
    except Exception as _e:
        print(f'[net-helper get_bid_network_claim] {_e}', flush=True)
        return None


def _get_network_claims_banner():
    try:
        db = _db()
        cur = db.cursor()
        cur.execute("""
            SELECT bp.bid_id, bp.dealer_id, bp.claimed_at, bp.score,
                   d.name AS dealer_name, d.salesperson AS dealer_salesperson,
                   b.year, b.make, b.model, b.network_ask::int AS ask
              FROM bid_pushes bp
              JOIN dealers d ON d.id = bp.dealer_id
              JOIN bids    b ON b.id = bp.bid_id
             WHERE bp.claimed_at IS NOT NULL
               AND bp.claim_late IS NOT TRUE
               AND bp.sold_confirmed_at IS NULL
             ORDER BY bp.claimed_at DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        db.close()
        return rows
    except Exception as _e:
        print(f'[net-helper banner] {_e}', flush=True)
        return []


# ============================================================
# ── Admin buy-profile routes
# ============================================================
@bp.route('/admin/buy-profiles')
def admin_buy_profiles():
    db = _db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, name, portal_slug, buy_profile, buy_profile_built_at,
               receive_inbound_pushes, always_show_in_push
          FROM dealers
         WHERE portal_slug IS NOT NULL
         ORDER BY id
    """)
    dealers = []
    for r in cur.fetchall():
        r = dict(r)
        p = r.get('buy_profile') or {}
        sample = p.get('sample') or {}
        bands = p.get('bands') or {}
        makes = p.get('makes') or {}
        top_makes = sorted([(k, v.get('share') or 0) for k, v in makes.items()],
                           key=lambda x: -x[1])[:5]
        r['_summary'] = {
            'active': sample.get('active_n'),
            'sold_180d': sample.get('sold_n_180d'),
            'days_scanned': sample.get('days_scanned'),
            'year_min': bands.get('year_min'),
            'year_max': bands.get('year_max'),
            'price_p10': bands.get('price_p10'),
            'price_p90': bands.get('price_p90'),
            'top_makes': top_makes,
            'makes_count': len(makes),
        }
        dealers.append(r)
    db.close()
    return render_template('admin_buy_profiles.html', dealers=dealers)


@bp.route('/admin/dealer/<int:dealer_id>/buy-profile')
def admin_dealer_buy_profile(dealer_id):
    db = _db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, name, portal_slug, buy_profile, buy_profile_built_at,
               receive_inbound_pushes, always_show_in_push, salesperson
          FROM dealers WHERE id = %s
    """, (dealer_id,))
    row = cur.fetchone()
    db.close()
    if not row:
        return "Not found", 404
    d = dict(row)
    p = d.get('buy_profile') or {}
    makes = p.get('makes') or {}
    makes_sorted = sorted([(k, v) for k, v in makes.items()],
                          key=lambda x: -(x[1].get('share') or 0))
    return render_template('admin_dealer_buy_profile.html',
                           d=d, profile=p, makes=makes_sorted,
                           sample=p.get('sample') or {},
                           bands=p.get('bands') or {},
                           behavioral=p.get('behavioral') or {},
                           overrides=p.get('overrides') or {})


@bp.route('/admin/buy-profiles/preview', methods=['GET', 'POST'])
def admin_buy_profile_preview():
    bid = {'year': None, 'make': '', 'model': '', 'miles': None, 'price': None}
    routing = None
    if request.method == 'POST':
        try:
            bid['year'] = int(request.form.get('year') or 0) or None
            bid['make'] = (request.form.get('make') or '').strip()
            bid['model'] = (request.form.get('model') or '').strip()
            bid['miles'] = int(request.form.get('miles') or 0) or None
            bid['price'] = int(request.form.get('price') or 0) or None
        except (TypeError, ValueError):
            pass
        if bid['make']:
            db = _db()
            cur = db.cursor()
            cur.execute("""
                SELECT id, name, buy_profile FROM dealers
                 WHERE portal_slug IS NOT NULL AND buy_profile IS NOT NULL
                 ORDER BY id
            """)
            routing = []
            for r in cur.fetchall():
                r = dict(r)
                s_bid = {'year': bid['year'], 'make': bid['make'],
                         'mileage': bid['miles'], 'asking_price': bid['price']}
                s, why = _score(s_bid, r['buy_profile'] or {}, dealer_id=r['id'])
                tier = ('T1' if (s is not None and s >= 80)
                        else 'T2' if (s is not None and s >= 60)
                        else 'skip')
                routing.append({'id': r['id'], 'name': r['name'],
                                'score': s, 'tier': tier, 'why': why})
            db.close()
            routing.sort(key=lambda x: (x['score'] if x['score'] is not None
                                         else -9999), reverse=True)
    return render_template('admin_buy_profile_preview.html',
                           bid=bid, routing=routing)


@bp.route('/api/admin/buy-profile/rebuild', methods=['POST'])
def api_admin_buy_profile_rebuild():
    db = _db()
    cur = db.cursor()
    cur.execute("SELECT rebuild_all_buy_profiles()")
    n = list(cur.fetchone().values())[0]
    db.commit()
    db.close()
    return {'rebuilt': n}


# ============================================================
# ── Network push API routes
# ============================================================
@bp.route('/api/bid/<int:bid_id>/network-push/preview', methods=['GET'])
def api_bid_network_push_preview(bid_id):
    try:
        ask = float(request.args.get('ask') or 0)
    except (TypeError, ValueError):
        ask = 0
    db = _db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, year, make, model, mileage, vin,
               COALESCE(asking_price, ai_price, bid_amount) AS asking_price
          FROM bids WHERE id = %s
    """, (bid_id,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return {'error': 'bid not found'}, 404
    # FETCH ALL PARTNER DEALERS — not just subscribed. Operator chooses via checkbox.
    cur.execute("""
        SELECT id, name, salesperson, salesperson_phone, buy_profile,
               COALESCE(always_show_in_push, FALSE) AS always_show,
               COALESCE(receive_inbound_pushes, FALSE) AS subscribed
          FROM dealers
         WHERE portal_slug IS NOT NULL AND active = TRUE
         ORDER BY id
    """)
    candidates = cur.fetchall()
    db.close()
    sent, skipped = [], []
    for d in candidates:
        score, reason = _score(dict(bid), d.get('buy_profile'), dealer_id=d['id'])
        target = {
            'dealer_id': d['id'], 'name': d['name'],
            'score': score, 'reason': reason,
            'sms_to': d.get('salesperson_phone'),
            'salesperson': d.get('salesperson'),
            'manual_pick': False,
            'subscribed': d.get('subscribed', False),
        }
        is_vin_protect = bool(reason and 'has this VIN' in (reason or ''))
        if score is None:
            if d.get('always_show') and not is_vin_protect:
                target['manual_pick'] = True
                target['score'] = 0
                sent.append(target)
            else:
                skipped.append(target)
            continue
        if score >= INBOUND_PUSH_MIN_SCORE:
            sent.append(target)
        elif d.get('always_show'):
            target['manual_pick'] = True
            sent.append(target)
        else:
            skipped.append(target)
    return {'preview': True, 'ask': ask, 'sent': sent,
            'skipped': skipped, 'errors': []}


@bp.route('/api/bid/<int:bid_id>/network-push', methods=['POST'])
def api_bid_network_push(bid_id):
    try:
        ask = float(request.form.get('network_ask') or 0)
    except (TypeError, ValueError):
        return {'error': 'invalid network_ask'}, 400
    if ask <= 0:
        return {'error': 'network_ask must be > 0'}, 400
    note = (request.form.get('note') or '').strip() or None
    pushed_by = session.get('username') or 'bidder'

    raw_ids = (request.form.get('dealer_ids') or '').strip()
    only_ids = None
    if raw_ids:
        try:
            only_ids = [int(x) for x in raw_ids.split(',') if x.strip().isdigit()]
        except Exception:
            only_ids = None

    # We need a variant of push_bid_with_network_ask that fetches ALL partners,
    # not just receive_inbound_pushes=TRUE. Call a local one.
    return _push_with_all_partners(bid_id, ask, note, pushed_by, only_ids)


def _push_with_all_partners(bid_id, ask, note, pushed_by, only_ids):
    """Manual-push variant that scans all 14 partners (not just subscribed) so
    operator-checked dealers can still receive the SMS even if they're not on
    the auto-push subscription list."""
    out = {'sent': [], 'skipped': [], 'errors': []}
    try:
        db = _db()
        cur = db.cursor()
        cur.execute("""
            UPDATE bids
               SET network_ask = %s, network_ask_note = %s,
                   network_pushed_at = NOW(), network_pushed_by = %s,
                   network_push_count = COALESCE(network_push_count, 0) + 1
             WHERE id = %s
         RETURNING id, year, make, model, mileage, vin,
                   COALESCE(asking_price, ai_price, bid_amount) AS asking_price
        """, (ask, note, pushed_by, bid_id))
        bid = cur.fetchone()
        if not bid:
            db.close()
            out['errors'].append(f'bid {bid_id} not found')
            return out

        cur.execute("""
            SELECT id, name, portal_slug, dashboard_token, salesperson,
                   salesperson_phone, buy_profile
              FROM dealers
             WHERE portal_slug IS NOT NULL AND active = TRUE
        """)
        candidates = cur.fetchall()

        from partner_portal import _compose_network_ask_sms, _send_network_sms

        for d in candidates:
            score, reason = _score(dict(bid), d.get('buy_profile'), dealer_id=d['id'])
            is_vin_protect = bool(reason and 'has this VIN' in (reason or ''))
            # Operator-override: explicitly listed in dealer_ids → bypass score
            operator_override = (only_ids is not None and d['id'] in only_ids
                                 and not is_vin_protect)
            # Otherwise gate by score (legacy auto behavior also handled here)
            if only_ids is None:
                # No checkbox filter — push to algorithmic matches only
                if score is None or score < INBOUND_PUSH_MIN_SCORE:
                    out['skipped'].append({'dealer_id': d['id'], 'name': d['name'],
                                           'score': score, 'reason': reason})
                    continue
            else:
                if d['id'] not in only_ids:
                    out['skipped'].append({'dealer_id': d['id'], 'name': d['name'],
                                           'score': score,
                                           'reason': 'unchecked by operator'})
                    continue
                if not operator_override and (score is None
                                              or score < INBOUND_PUSH_MIN_SCORE):
                    out['skipped'].append({'dealer_id': d['id'], 'name': d['name'],
                                           'score': score, 'reason': reason})
                    continue

            cur.execute("""
                INSERT INTO bid_pushes (bid_id, dealer_id, score, score_reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (bid_id, dealer_id) DO UPDATE
                   SET score = EXCLUDED.score, score_reason = EXCLUDED.score_reason
            """, (bid_id, d['id'], score if score is not None else 0, reason))

            # Build recipient set: salesperson_phone + every partner_users.phone
            cur.execute("""
                SELECT DISTINCT phone FROM partner_users
                 WHERE dealer_id = %s AND phone IS NOT NULL AND phone <> ''
            """, (d['id'],))
            pu_phones = [r['phone'] for r in cur.fetchall()]
            recipients = []
            for p in [d.get('salesperson_phone')] + pu_phones:
                p = (p or '').strip()
                if p and p not in recipients:
                    recipients.append(p)

            # Operator CC — receives a copy of every push (env-tunable)
            cc_phone = (os.environ.get('EW_NETWORK_PUSH_CC_PHONE')
                        or '+14074309675').strip()
            test_gate = (os.environ.get('EW_SMS_TEST_GATE_PHONE') or '').strip()
            # Skip CC when test gate is on and CC == gate (gate already routes everything to that number)
            if cc_phone and not (test_gate and cc_phone == test_gate):
                if cc_phone not in recipients:
                    recipients.append(cc_phone)

            if not recipients:
                out['errors'].append(f"{d['name']}: no recipient phones")
                continue

            sms_text = _compose_network_ask_sms(bid, ask, note, d)
            sent_count = 0
            sent_phones = []
            for phone in recipients:
                if _send_network_sms(phone, sms_text):
                    sent_count += 1
                    sent_phones.append(phone)
                else:
                    out['errors'].append(f"{d['name']} → {phone}: SMS send failed")

            # Record one bid_pushes row — sms_to stores the FIRST successful recipient
            # (salesperson_phone preferred). Reply matching also checks partner_users.phone.
            if sent_count > 0:
                primary_to = sent_phones[0]
                cur.execute("""
                    UPDATE bid_pushes SET sms_sent_at=NOW(), sms_to=%s
                     WHERE bid_id=%s AND dealer_id=%s
                """, (primary_to, bid_id, d['id']))
                out['sent'].append({
                    'dealer_id': d['id'], 'name': d['name'],
                    'score': score if score is not None else 0,
                    'sms_to': primary_to,
                    'sms_recipients': sent_phones,
                    'salesperson': d.get('salesperson'),
                })
        db.commit()
        db.close()
    except Exception as e:
        out['errors'].append(f'exception: {e}')
        try: db.close()
        except Exception: pass
    print(f'[network-push bp] bid={bid_id} ask=${ask:,.0f} '
          f'sent={len(out["sent"])} skipped={len(out["skipped"])} '
          f'errors={len(out["errors"])}', flush=True)
    return out


@bp.route('/api/bid/<int:bid_id>/confirm-sold', methods=['POST'])
def api_bid_confirm_sold(bid_id):
    db = _db()
    cur = db.cursor()
    cur.execute("""
        UPDATE bid_pushes
           SET sold_confirmed_at = NOW(),
               sold_confirmed_by = %s
         WHERE bid_id = %s
           AND claimed_at IS NOT NULL
           AND claim_late IS NOT TRUE
         RETURNING dealer_id
    """, (session.get('username') or 'admin', bid_id))
    w = cur.fetchone()
    if not w:
        db.close()
        return {'ok': False, 'error': 'no active claim'}
    winner_id = w['dealer_id']
    cur.execute("UPDATE bids SET status='bought', updated_at=NOW() WHERE id=%s",
                (bid_id,))
    cur.execute("SELECT year, make, model FROM bids WHERE id=%s", (bid_id,))
    b = cur.fetchone()
    ymm = (b and f"{b['year'] or ''} {b['make'] or ''} {b['model'] or ''}".strip()) or 'that vehicle'

    cur.execute("""
        SELECT bp.id AS push_id, bp.dealer_id, bp.sms_to,
               d.name AS dealer_name, d.salesperson_phone
          FROM bid_pushes bp
          JOIN dealers d ON d.id = bp.dealer_id
         WHERE bp.bid_id = %s
           AND bp.sms_sent_at IS NOT NULL
           AND bp.dealer_id != %s
    """, (bid_id, winner_id))
    losers = cur.fetchall()

    from partner_portal import _send_network_sms
    broadcast_sent = 0
    broadcast_failed = 0
    sold_body = (f"Update: the {ymm} you saw earlier just sold to another buyer. "
                 f"Thanks for taking a look — we'll text you when the next one hits.")

    cc_phone = (os.environ.get('EW_NETWORK_PUSH_CC_PHONE')
                or '+14074309675').strip()
    test_gate = (os.environ.get('EW_SMS_TEST_GATE_PHONE') or '').strip()

    for L in losers:
        # Build full recipient set per loser dealer:
        # primary user(s) + salesperson + operator CC, deduped.
        cur.execute("""
            SELECT DISTINCT phone FROM partner_users
             WHERE dealer_id = %s AND phone IS NOT NULL AND phone <> ''
        """, (L['dealer_id'],))
        pu_phones = [r['phone'] for r in cur.fetchall()]
        recipients = []
        for p in [L.get('salesperson_phone')] + pu_phones:
            p = (p or '').strip()
            if p and p not in recipients:
                recipients.append(p)
        if cc_phone and not (test_gate and cc_phone == test_gate):
            if cc_phone not in recipients:
                recipients.append(cc_phone)

        if not recipients:
            broadcast_failed += 1
            continue

        any_sent = False
        for phone in recipients:
            if _send_network_sms(phone, sold_body):
                any_sent = True
                broadcast_sent += 1
            else:
                broadcast_failed += 1
        if any_sent:
            cur.execute("""
                UPDATE bid_pushes SET sold_confirmed_at = NOW()
                 WHERE id = %s
            """, (L['push_id'],))
    db.commit()
    db.close()
    print(f'[confirm-sold bp] bid={bid_id} winner={winner_id} '
          f'broadcast_sent={broadcast_sent} failed={broadcast_failed}', flush=True)
    return {'ok': True, 'dealer_id': winner_id,
            'broadcast_sent': broadcast_sent,
            'broadcast_failed': broadcast_failed}


# ============================================================
# ── Register Jinja template helpers as app-wide globals
# ============================================================
@bp.record_once
def _register_helpers(state):
    app = state.app
    app.jinja_env.globals.update(
        get_bid_network_sold=_get_bid_network_sold,
        get_bid_network_claim=_get_bid_network_claim,
        get_network_claims_banner=_get_network_claims_banner,
    )
