#!/usr/bin/env python3
"""ew_listing_autokick_sweeper.py - fast (90s) self-heal for the bid listing.

Operator ask (2026-06-30): every bid should be FULLY populated within ~90s
(all_enriched_at stamped = vAuto + rBook + MMR + AccuTrade-terminal + iPacket-row,
per _maybe_fire_assessment). If a bid is past 90s and STILL not fully populated
because the *AccuTrade* leg never landed, arm an AccuTrade-only retry ONCE so
/api/accutrade/pending re-runs it. "Reprocess once, then it's a different issue."

iPACKET HARD RULE (operator, account-ban risk): this sweeper NEVER pulls or
re-pulls iPacket. It ONLY arms accutrade_retry_at, which drives the AccuTrade-ONLY
decoupled queue (/api/accutrade/pending) - no iPacket involved. A bid missing ONLY
iPacket (AccuTrade already present) does NOT match this predicate, so nothing
happens to it here; it stays covered by the normal pipeline + the 35-min PASS-B
degraded-assess in ew_accutrade_drop_sweeper.py. vAuto is REQUIRED-present
(rbook+manheim done), so we never re-trigger the full pipeline (which would
re-pull iPacket).

One-shot per bid via accutrade_autoretried - SHARED with ew_accutrade_drop_sweeper,
so the fast (90s) and slow (20min) sweepers never double-kick the same bid. After
this fires, the bid leaves /api/accutrade/pending after exactly one fresh pass
(the submit clears accutrade_retry_at); if AccuTrade still drops, PASS-B prices it
degraded at 35min. Restart-proof: cron, not an in-process timer (the old 300s
threading.Timer died on every --preload restart).

Window: bids created between AGE_SEC (90s) and WINDOW_MIN (30min) ago. Older bids
are the drop_sweeper's domain. Cron every minute -> effective kick ~90-150s.

Usage: ew_listing_autokick_sweeper.py [--dry-run] [--limit N] [--age-sec N] [--window-min N]
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
TG_BOT = os.environ.get('TELEGRAM_BOT_TOKEN', '8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '7985611488')

# AccuTrade is the missing leg: vAuto fully landed (raw_json + rbook + manheim),
# bid not yet fully enriched (all_enriched_at NULL) and not yet assessed, and NO
# USABLE accutrade row (real value, or a terminal not_available that is NOT the
# retryable mileage_did_not_commit transient - mirrors the _maybe_fire_assessment
# gate). One-shot unused; live status; recent window. iPacket is intentionally
# NOT referenced anywhere in this predicate or action.
WHERE_ACCU_MISSING = """
      FROM bids b
      JOIN vauto_lookups vl ON vl.bid_id = b.id
     WHERE b.all_enriched_at IS NULL
       AND b.ai_price IS NULL
       AND (b.ai_assessment IS NULL OR b.ai_assessment = '')
       AND b.mileage IS NOT NULL
       AND b.vin IS NOT NULL AND length(b.vin) = 17
       AND vl.raw_json IS NOT NULL
       AND vl.rbook_completed_at   IS NOT NULL
       AND vl.manheim_completed_at IS NOT NULL
       AND (b.needs_verification_at IS NULL OR b.needs_verification_cleared_at IS NOT NULL)
       AND b.accutrade_retry_at IS NULL
       AND COALESCE(b.accutrade_autoretried, FALSE) = FALSE
       AND COALESCE(b.status,'') NOT IN
           ('dead','duplicate','archived','rejected','sold','passed','cancelled','bought')
       AND b.created_at < NOW() - (%(age_sec)s * INTERVAL '1 second')
       AND b.created_at > NOW() - (%(window_min)s * INTERVAL '1 minute')
       AND NOT EXISTS (
             SELECT 1 FROM accutrade_lookups al
              WHERE al.bid_id = b.id
                AND (al.guaranteed_offer IS NOT NULL OR al.trade_in IS NOT NULL
                     OR al.trade_market IS NOT NULL
                     OR (COALESCE(al.not_available, false) = true
                         AND COALESCE(al.unavailable_reason, '')
                             NOT ILIKE '%%mileage_did_not_commit%%')))
"""


def telegram(msg):
    if requests and TG_BOT and TG_CHAT:
        try:
            requests.post("https://api.telegram.org/bot%s/sendMessage" % TG_BOT,
                          data={'chat_id': TG_CHAT, 'text': msg}, timeout=10)
        except Exception as e:
            print("telegram failed: %s" % e, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=int(os.environ.get('AUTOKICK_LIMIT', '10')))
    ap.add_argument('--age-sec', type=int, default=int(os.environ.get('AUTOKICK_AGE_SEC', '90')))
    ap.add_argument('--window-min', type=int, default=int(os.environ.get('AUTOKICK_WINDOW_MIN', '30')))
    args = ap.parse_args()
    P = {'age_sec': args.age_sec, 'window_min': args.window_min, 'lim': args.limit}

    conn = psycopg2.connect(**DB)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT pg_is_in_recovery() AS rec")
    if cur.fetchone()['rec']:
        print("standby (in recovery) - skip", flush=True)
        conn.close()
        return

    if args.dry_run:
        cur.execute("SELECT b.id, b.year, b.make, b.model, "
                    "ROUND(EXTRACT(EPOCH FROM (NOW()-b.created_at))) AS age_s "
                    + WHERE_ACCU_MISSING + " ORDER BY b.id DESC LIMIT %(lim)s", P)
        rows = cur.fetchall()
        print("DRY-RUN: %d bid(s) would get an AccuTrade fast-kick (>=%ss, <%smin):"
              % (len(rows), args.age_sec, args.window_min), flush=True)
        for r in rows:
            print("  arm #%s %s %s %s (age %ss)"
                  % (r['id'], r['year'], r['make'], r['model'], r['age_s']), flush=True)
        conn.close()
        return

    cur.execute(
        "WITH eligible AS ( SELECT b.id " + WHERE_ACCU_MISSING +
        " ORDER BY b.id DESC FOR UPDATE OF b SKIP LOCKED LIMIT %(lim)s )"
        " UPDATE bids SET accutrade_autoretried=TRUE, accutrade_retry_at=NOW(),"
        "   accutrade_retry_claimed_at=NULL, updated_at=NOW()"
        " FROM eligible WHERE bids.id=eligible.id"
        " RETURNING bids.id, bids.year, bids.make, bids.model", P)
    armed = cur.fetchall()
    if armed:
        ids = [r['id'] for r in armed]
        # clear the mileage_did_not_commit placeholder row so /api/accutrade/pending
        # serves a clean retry (never deletes a row carrying real values)
        cur.execute("DELETE FROM accutrade_lookups WHERE bid_id = ANY(%s)"
                    " AND guaranteed_offer IS NULL AND trade_in IS NULL AND trade_market IS NULL", (ids,))
    conn.commit()
    conn.close()
    if armed:
        msg = "EW autokick(%ss) armed %d AccuTrade retry: %s" % (
            args.age_sec, len(armed),
            ', '.join("#%s %s %s %s" % (r['id'], r['year'], r['make'], r['model']) for r in armed))
        print(msg, flush=True)
        telegram(msg)
    else:
        print("autokick: no AccuTrade-missing bids in the %ss-%smin window"
              % (args.age_sec, args.window_min), flush=True)


if __name__ == '__main__':
    main()
