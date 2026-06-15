#!/usr/bin/env python3
"""ew_accutrade_drop_sweeper.py - self-healing for the "dropped enrichment leg
-> permanently-stuck unpriced bid" bug. Cron (restart-immune), every 5 min.

PASS A (AccuTrade-drop): arm accutrade_retry_at for bids where the worker
dropped a completing AccuTrade leg (vAuto landed, NO usable AccuTrade row,
never assessed, retry never armed) so /api/accutrade/pending re-runs AccuTrade.
One-shot per bid via accutrade_autoretried.

PASS B (deadline degraded-assess): for ANY bid with core vAuto data
(raw_json + rbook + manheim) that the require_all gate never fired and that is
still unpriced after DEADLINE_MIN - fire /api/bid/<id>/assess so it prices on
whatever data landed instead of waiting forever on a dropped accu OR ipacket
leg. Restart-proof replacement for the in-process 300s threading.Timer.

Built 2026-06-15 - see EW_FOLLOWUPS.md "AccuTrade-drop". The gate at
_maybe_fire_assessment requires has_vauto AND has_accu AND rb AND mh AND
has_ipkt; a single dropped leg (no accutrade/ipacket row) stalls it forever and
the existing assessment_watchdog skips it (it requires an ipacket row).

Usage:  ew_accutrade_drop_sweeper.py [--dry-run] [--limit N] [--age-min N] [--deadline-min N]
"""
from __future__ import annotations
import argparse, os
import psycopg2, psycopg2.extras
try:
    import requests
except Exception:
    requests = None

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')
APP_HOST = os.environ.get('EW_APP_HOST', 'http://127.0.0.1:9001')
EW_USERNAME = os.environ.get('EW_USERNAME', 'admin')
EW_PASSWORD = os.environ.get('EW_PASSWORD', 'Sedecrem3')
TIMEOUT_SEC = 120
TG_BOT = os.environ.get('TELEGRAM_BOT_TOKEN', '8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '7985611488')

# --- PASS A predicate: AccuTrade-drop stuck state ------------------------------
WHERE_STUCK_ACCU = """
      FROM bids b
      JOIN vauto_lookups vl ON vl.bid_id = b.id
     WHERE b.ai_price IS NULL
       AND (b.ai_assessment IS NULL OR b.ai_assessment = '')
       AND b.mileage IS NOT NULL
       AND b.vin IS NOT NULL AND length(b.vin) = 17
       AND vl.raw_json IS NOT NULL
       AND vl.rbook_completed_at   IS NOT NULL
       AND vl.manheim_completed_at IS NOT NULL
       AND (b.needs_verification_at IS NULL OR b.needs_verification_cleared_at IS NOT NULL)
       AND b.accutrade_retry_at IS NULL
       AND COALESCE(b.accutrade_autoretried, FALSE) = FALSE
       AND (b.vauto_claimed_at IS NULL OR b.vauto_claimed_at < NOW() - INTERVAL '5 minutes')
       AND b.created_at < NOW() - (%(age)s * INTERVAL '1 minute')
       AND b.created_at > NOW() - INTERVAL '3 days'
       AND NOT EXISTS (
             SELECT 1 FROM accutrade_lookups al
              WHERE al.bid_id = b.id
                AND (al.guaranteed_offer IS NOT NULL OR al.trade_in IS NOT NULL
                     OR al.trade_market IS NOT NULL
                     OR (COALESCE(al.not_available, false) = true
                         AND COALESCE(al.unavailable_reason, '')
                             NOT ILIKE '%%mileage_did_not_commit%%')))
"""

# --- PASS B predicate: any dropped-leg stall past the deadline -----------------
# Core vAuto data present, gate never fired (ai_assessed_at NULL - if every leg
# had landed the gate would have claimed it), still unpriced, past the deadline.
WHERE_DEADLINE = """
      FROM bids b
      JOIN vauto_lookups vl ON vl.bid_id = b.id
     WHERE b.ai_assessed_at IS NULL
       AND b.ai_price IS NULL
       AND (b.ai_assessment IS NULL OR b.ai_assessment = '')
       AND b.mileage IS NOT NULL
       AND b.vin IS NOT NULL AND length(b.vin) = 17
       AND vl.raw_json IS NOT NULL
       AND vl.rbook_completed_at   IS NOT NULL
       AND vl.manheim_completed_at IS NOT NULL
       AND (b.needs_verification_at IS NULL OR b.needs_verification_cleared_at IS NOT NULL)
       AND b.created_at < NOW() - (%(deadline)s * INTERVAL '1 minute')
       AND b.created_at > NOW() - INTERVAL '3 days'
"""


def telegram(msg):
    if requests and TG_BOT and TG_CHAT:
        try:
            requests.post("https://api.telegram.org/bot%s/sendMessage" % TG_BOT,
                          data={'chat_id': TG_CHAT, 'text': msg}, timeout=10)
        except Exception as e:
            print("telegram failed: %s" % e, flush=True)


def login_session():
    s = requests.Session()
    try:
        r = s.post("%s/login" % APP_HOST,
                   data={'username': EW_USERNAME, 'password': EW_PASSWORD},
                   timeout=15, allow_redirects=False)
    except Exception as e:
        print("login request failed: %s" % e, flush=True)
        return None
    if r.status_code == 302 and r.headers.get('Location', '').endswith('/'):
        return s
    print("login failed status=%s" % r.status_code, flush=True)
    return None


def missing_legs(row):
    return ','.join([m for m, ok in (('accu', row['has_accu']), ('ipkt', row['has_ipkt'])) if not ok]) or 'none'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=int(os.environ.get('ACCU_DROP_LIMIT', '5')))
    ap.add_argument('--age-min', type=int, default=int(os.environ.get('ACCU_DROP_AGE_MIN', '20')))
    ap.add_argument('--deadline-min', type=int, default=int(os.environ.get('ACCU_DROP_DEADLINE_MIN', '35')))
    args = ap.parse_args()
    P = {'age': args.age_min, 'deadline': args.deadline_min, 'lim': args.limit}

    conn = psycopg2.connect(**DB)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT pg_is_in_recovery() AS rec")
    if cur.fetchone()['rec']:
        print("standby (in recovery) - skip", flush=True)
        conn.close()
        return

    # ---------- PASS A: arm AccuTrade retry ----------
    if args.dry_run:
        cur.execute("SELECT b.id, b.year, b.make, b.model " + WHERE_STUCK_ACCU +
                    " ORDER BY b.id DESC LIMIT %(lim)s", P)
        rows = cur.fetchall()
        print("DRY-RUN PASS A: %d bid(s) would be armed for AccuTrade retry:" % len(rows), flush=True)
        for r in rows:
            print("  arm #%s %s %s %s" % (r['id'], r['year'], r['make'], r['model']), flush=True)
    else:
        cur.execute(
            "WITH eligible AS ( SELECT b.id " + WHERE_STUCK_ACCU +
            " ORDER BY b.id DESC FOR UPDATE OF b SKIP LOCKED LIMIT %(lim)s )"
            " UPDATE bids SET accutrade_autoretried=TRUE, accutrade_retry_at=NOW(),"
            "   accutrade_retry_claimed_at=NULL, updated_at=NOW()"
            " FROM eligible WHERE bids.id=eligible.id"
            " RETURNING bids.id, bids.year, bids.make, bids.model", P)
        armed = cur.fetchall()
        if armed:
            ids = [r['id'] for r in armed]
            cur.execute("DELETE FROM accutrade_lookups WHERE bid_id = ANY(%s)"
                        " AND guaranteed_offer IS NULL AND trade_in IS NULL AND trade_market IS NULL", (ids,))
        conn.commit()
        if armed:
            msg = "EW sweeper PASS A armed %d AccuTrade retry: %s" % (
                len(armed), ', '.join("#%s %s %s %s" % (r['id'], r['year'], r['make'], r['model']) for r in armed))
            print(msg, flush=True)
            telegram(msg)
        else:
            print("PASS A: no AccuTrade-drop bids", flush=True)

    # ---------- PASS B: deadline degraded-assess ----------
    cur.execute(
        "SELECT b.id, b.year, b.make, b.model,"
        " EXISTS(SELECT 1 FROM accutrade_lookups a WHERE a.bid_id=b.id AND (a.trade_in IS NOT NULL OR a.guaranteed_offer IS NOT NULL OR a.not_available=true)) AS has_accu,"
        " EXISTS(SELECT 1 FROM ipacket_lookups i WHERE i.bid_id=b.id) AS has_ipkt "
        + WHERE_DEADLINE + " ORDER BY b.id DESC LIMIT %(lim)s", P)
    due = cur.fetchall()
    conn.close()

    if args.dry_run:
        print("DRY-RUN PASS B: %d bid(s) past %dmin deadline would be degraded-assessed:" % (len(due), args.deadline_min), flush=True)
        for r in due:
            print("  assess #%s %s %s %s (missing: %s)" % (r['id'], r['year'], r['make'], r['model'], missing_legs(r)), flush=True)
        return

    if not due:
        print("PASS B: no deadline-exceeded bids", flush=True)
        return
    sess = login_session()
    if not sess:
        telegram("EW sweeper PASS B: login failed, %d bid(s) waiting" % len(due))
        return
    fired = []
    for r in due:
        try:
            resp = sess.post("%s/api/bid/%s/assess" % (APP_HOST, r['id']), timeout=TIMEOUT_SEC)
            miss = missing_legs(r)
            print("PASS B assess #%s -> %s (missing: %s)" % (r['id'], resp.status_code, miss), flush=True)
            if resp.status_code == 200:
                fired.append((r['id'], miss))
        except Exception as e:
            print("PASS B assess #%s FAILED: %s" % (r['id'], e), flush=True)
    if fired:
        telegram("EW sweeper PASS B degraded-assessed %d stuck bid(s): %s" % (
            len(fired), ', '.join("#%s(%s missing)" % (bid, miss) for bid, miss in fired)))


if __name__ == '__main__':
    main()
