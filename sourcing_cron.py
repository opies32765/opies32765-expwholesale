"""
EW sourcing-bot — wishlist cron jobs.

Run every 15 minutes via system cron. One pass does:

1. WISHLIST SCAN — for every active wishlist row, run inventory search
   against current dealer scans. Any newly-matching inventory IDs (not in
   notified_inventory_ids and not in pending_alert_inventory_ids) get
   queued as pending alerts.

2. PENDING ALERT DISPATCH — any wishlist row with pending alerts AND
   current ET time is inside Mon-Fri 09:00-17:00 → fire one SMS per row
   (combined matches), append to conversation, move IDs from pending →
   notified.

3. DAY-30 RE-PING — wishlist rows with wishlist_until in the past and
   reping_sent_at unset → send "still searching?" SMS, set reping_sent_at.

4. EXPIRY ARCHIVE — wishlist rows where reping_sent_at > 7 days ago and
   no user reply since → archive with reason='expired_no_extend'.

Manual run:
    /opt/expwholesale/venv/bin/python /opt/expwholesale/sourcing_cron.py
"""
import os
import sys
import json
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo('America/New_York')
except ImportError:
    import pytz
    _ET = pytz.timezone('America/New_York')

# Make sourcing modules importable when run via cron.
sys.path.insert(0, '/opt/expwholesale')


def _ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _is_business_hours(dt_et=None):
    """Mon-Fri 09:00-17:00 America/New_York."""
    if dt_et is None:
        dt_et = datetime.now(_ET)
    if dt_et.weekday() >= 5:
        return False
    h = dt_et.hour
    return 9 <= h < 17


def _format_match_desc(m):
    parts = []
    if m.get('year'): parts.append(str(m['year']))
    if m.get('model'): parts.append(m['model'])
    if m.get('trim'): parts.append(m['trim'])
    head = ' '.join(parts)
    ec, ic = m.get('ext_color'), m.get('int_color')
    if ec and ic: color = f"{ec} / {ic}"
    elif ec: color = ec
    elif ic: color = f"{ic} interior"
    else: color = ''
    miles = _fmt_miles_cron(m.get('mileage'))
    bits = [head]
    if color: bits.append(color)
    bits.append(miles)
    return ', '.join(bits)


def _format_request_desc(req):
    """Short description used in re-ping + wishlist save messages."""
    parts = []
    if req.get('year_min') or req.get('year_max'):
        if req.get('year_min') == req.get('year_max'):
            parts.append(str(req['year_min']))
        else:
            parts.append(f"{req.get('year_min','?')}-{req.get('year_max','?')}")
    if req.get('make'):
        parts.append(req['make'])
    if req.get('model'):
        parts.append(req['model'])
    if req.get('trim'):
        parts.append(req['trim'])
    s = ' '.join(parts) or 'your request'
    if req.get('ext_color'):
        s += f" in {req['ext_color'][0]}"
    return s


def scan_wishlists(db, cur):
    """Step 1: find new matches for every active wishlist row."""
    from sourcing_search import search as inv_search

    cur.execute("""
        SELECT * FROM sourcing_requests
         WHERE status = 'wishlist'
           AND (wishlist_until IS NULL OR wishlist_until > NOW())
    """)
    rows = cur.fetchall()
    print(f'[{_ts()}] [scan] {len(rows)} active wishlists', flush=True)

    queued_total = 0
    for r in rows:
        try:
            matches = inv_search(dict(r), limit=20)
        except Exception as e:
            print(f'[{_ts()}] [scan] search failed id={r["id"]}: {e}', flush=True)
            continue

        notified = set(r.get('notified_inventory_ids') or [])
        pending = set(r.get('pending_alert_inventory_ids') or [])
        seen = notified | pending
        new_ids = [m['id'] for m in matches if m['id'] not in seen]
        if new_ids:
            cur.execute("""UPDATE sourcing_requests
                              SET pending_alert_inventory_ids =
                                    pending_alert_inventory_ids || %s::int[],
                                  last_scan_at = NOW()
                            WHERE id = %s""",
                        (new_ids, r['id']))
            db.commit()
            queued_total += len(new_ids)
            print(f'[{_ts()}] [scan] id={r["id"]} queued {len(new_ids)} new matches', flush=True)
        else:
            cur.execute("UPDATE sourcing_requests SET last_scan_at=NOW() WHERE id=%s", (r['id'],))
            db.commit()
    print(f'[{_ts()}] [scan] queued total: {queued_total}', flush=True)


def dispatch_pending(db, cur, send_sms):
    """Step 2: send queued alerts inside business hours."""
    if not _is_business_hours():
        print(f'[{_ts()}] [dispatch] outside business hours, skipping', flush=True)
        return

    cur.execute("""
        SELECT * FROM sourcing_requests
         WHERE status = 'wishlist'
           AND array_length(pending_alert_inventory_ids, 1) > 0
    """)
    rows = cur.fetchall()
    print(f'[{_ts()}] [dispatch] {len(rows)} rows with pending alerts', flush=True)

    for r in rows:
        pending_ids = r.get('pending_alert_inventory_ids') or []
        if not pending_ids:
            continue

        # Re-fetch the inventory rows (they might have gone inactive since
        # we queued them; only alert on ones still active).
        cur.execute("""
            SELECT id, year, make, model, trim, ext_color, int_color, mileage
              FROM dealer_inventory
             WHERE id = ANY(%s) AND status = 'active'
             LIMIT 3
        """, (pending_ids,))
        matches = cur.fetchall()

        if not matches:
            # All queued matches went inactive — clear pending without alert.
            cur.execute("""UPDATE sourcing_requests
                              SET pending_alert_inventory_ids = '{}'::int[]
                            WHERE id = %s""", (r['id'],))
            db.commit()
            continue

        descs = '\n'.join(_format_match_desc(m) for m in matches)
        n_total = len(pending_ids)
        more = (f"\n(got {n_total} total — reply 'more' to see the rest)"
                if n_total > 3 else '')
        msg = f"new match for your saved search:\n{descs}{more}\ninterested?"

        sent = bool(send_sms(r['phone'], msg))
        if not sent:
            print(f'[{_ts()}] [dispatch] sms failed id={r["id"]}', flush=True)
            continue

        # Append to conversation, move pending → notified.
        ts_iso = datetime.now(timezone.utc).isoformat()
        new_conv = (r.get('conversation') or []) + [{
            'role': 'bot', 'ts': ts_iso, 'text': msg,
            'raw': {'wishlist_alert': True, 'inventory_ids': [m['id'] for m in matches]},
        }]
        cur.execute("""UPDATE sourcing_requests
                          SET conversation = %s::jsonb,
                              notified_inventory_ids = notified_inventory_ids || %s::int[],
                              pending_alert_inventory_ids = '{}'::int[],
                              last_msg_at = NOW(),
                              status = 'presented'
                        WHERE id = %s""",
                    (json.dumps(new_conv), pending_ids, r['id']))
        db.commit()
        print(f'[{_ts()}] [dispatch] alerted id={r["id"]} ({len(matches)} matches, {n_total} queued)', flush=True)


def reping_30day(db, cur, send_sms):
    """Step 3: ping wishlists past their 30-day window asking to extend."""
    if not _is_business_hours():
        return
    cur.execute("""
        SELECT * FROM sourcing_requests
         WHERE status = 'wishlist'
           AND wishlist_until IS NOT NULL
           AND wishlist_until < NOW()
           AND reping_sent_at IS NULL
    """)
    rows = cur.fetchall()
    for r in rows:
        desc = _format_request_desc(r)
        msg = (f"still want us to keep searching for {desc}? "
               f"reply yes to keep going another 30 days, or 'drop it' to stop.")
        if not send_sms(r['phone'], msg):
            continue
        ts_iso = datetime.now(timezone.utc).isoformat()
        new_conv = (r.get('conversation') or []) + [{
            'role': 'bot', 'ts': ts_iso, 'text': msg,
            'raw': {'reping_30d': True},
        }]
        cur.execute("""UPDATE sourcing_requests
                          SET conversation = %s::jsonb,
                              reping_sent_at = NOW(),
                              last_msg_at = NOW()
                        WHERE id = %s""",
                    (json.dumps(new_conv), r['id']))
        db.commit()
        print(f'[{_ts()}] [reping] id={r["id"]} {desc!r}', flush=True)


def expire_archive(db, cur):
    """Step 4: archive wishlists where re-ping went unanswered for 7 days."""
    cur.execute("""
        UPDATE sourcing_requests
           SET status = 'archived',
               archived_at = NOW(),
               archive_reason = 'expired_no_extend'
         WHERE status = 'wishlist'
           AND reping_sent_at IS NOT NULL
           AND reping_sent_at < NOW() - INTERVAL '7 days'
           AND last_inbound_at < reping_sent_at
        RETURNING id
    """)
    rows = cur.fetchall()
    db.commit()
    if rows:
        print(f'[{_ts()}] [expire] archived {len(rows)} stale wishlists', flush=True)


def refresh_taxonomy(db, cur):
    """Step 0: refresh inventory_taxonomy materialized view so the bot's
    models_for_make / count_for_make_model / find_make_for_model helpers
    see fresh inventory data. CONCURRENT refresh allows reads during the
    refresh — view stays available, no read-side blocking. Cheap (<100ms
    on current 1k-row inventory)."""
    try:
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY inventory_taxonomy")
        db.commit()
        print(f'[{_ts()}] [taxonomy] refreshed', flush=True)
    except Exception as e:
        # Don't let a refresh failure block the rest of the cron — the bot
        # still works on stale taxonomy data; it just won't see new models.
        print(f'[{_ts()}] [taxonomy] refresh failed: {e}', flush=True)
        try: db.rollback()
        except Exception: pass


def main():
    # Late imports so module loads cleanly even if app.py has issues.
    from app import get_db, send_sms

    db = get_db()
    cur = db.cursor()
    try:
        refresh_taxonomy(db, cur)
        scan_wishlists(db, cur)
        dispatch_pending(db, cur, send_sms)
        reping_30day(db, cur, send_sms)
        expire_archive(db, cur)
    finally:
        try: db.close()
        except Exception: pass


if __name__ == '__main__':
    main()
