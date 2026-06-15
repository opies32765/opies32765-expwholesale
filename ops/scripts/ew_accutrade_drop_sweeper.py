#!/usr/bin/env python3
"""ew_accutrade_drop_sweeper.py — arm accutrade_retry_at for bids where the
worker dropped a completing AccuTrade leg (vAuto landed, NO usable AccuTrade
row, never assessed, retry never armed) so the proven /api/accutrade/pending
worker path re-runs ONLY AccuTrade. One-shot per bid via accutrade_autoretried.
Restart-immune (cron, not an in-app thread). Never blanks the customer page
(vauto/iPacket/assessment untouched). Built 2026-06-15 — see EW_FOLLOWUPS.md
"AccuTrade-drop" entry.

Usage:  ew_accutrade_drop_sweeper.py [--dry-run] [--limit N] [--age-min N]
"""
from __future__ import annotations
import argparse, os, sys
import psycopg2, psycopg2.extras
try:
    import requests
except Exception:
    requests = None

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')
TG_BOT = os.environ.get('TELEGRAM_BOT_TOKEN', '8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '7985611488')

# Shared predicate: a bid stuck in the AccuTrade-drop state. Mirrors the
# has_accu assess-gate + /api/accutrade/pending "usable row" predicate exactly,
# so a bid that already has good values or a terminal failure is never re-armed.
WHERE_STUCK = """
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
                AND (al.guaranteed_offer IS NOT NULL
                     OR al.trade_in IS NOT NULL
                     OR al.trade_market IS NOT NULL
                     OR (COALESCE(al.not_available, false) = true
                         AND COALESCE(al.unavailable_reason, '')
                             NOT ILIKE '%%mileage_did_not_commit%%')))
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=int(os.environ.get('ACCU_DROP_LIMIT', '5')))
    ap.add_argument('--age-min', type=int, default=int(os.environ.get('ACCU_DROP_AGE_MIN', '20')))
    args = ap.parse_args()
    params = {'age': args.age_min, 'lim': args.limit}

    conn = psycopg2.connect(**DB); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT pg_is_in_recovery() AS rec")
    if cur.fetchone()['rec']:
        print("standby (in recovery) - skip", flush=True); conn.close(); return

    if args.dry_run:
        cur.execute("SELECT b.id, b.year, b.make, b.model, b.created_at " + WHERE_STUCK +
                    " ORDER BY b.id DESC LIMIT %(lim)s", params)
        rows = cur.fetchall()
        print("DRY-RUN: %d bid(s) would be armed for AccuTrade retry:" % len(rows), flush=True)
        for r in rows:
            print("  #%s %s %s %s (created %s)" % (r['id'], r['year'], r['make'], r['model'], r['created_at']), flush=True)
        conn.close(); return

    cur.execute("""
        WITH eligible AS (
            SELECT b.id """ + WHERE_STUCK + """
             ORDER BY b.id DESC
             FOR UPDATE OF b SKIP LOCKED
             LIMIT %(lim)s
        )
        UPDATE bids
           SET accutrade_autoretried = TRUE,
               accutrade_retry_at = NOW(),
               accutrade_retry_claimed_at = NULL,
               updated_at = NOW()
          FROM eligible WHERE bids.id = eligible.id
        RETURNING bids.id, bids.year, bids.make, bids.model
    """, params)
    armed = cur.fetchall()
    if armed:
        ids = [r['id'] for r in armed]
        cur.execute("""DELETE FROM accutrade_lookups
                        WHERE bid_id = ANY(%s)
                          AND guaranteed_offer IS NULL AND trade_in IS NULL AND trade_market IS NULL""", (ids,))
    conn.commit(); conn.close()

    if armed:
        msg = "EW accutrade-drop sweeper armed %d bid(s): %s" % (
            len(armed), ', '.join("#%s %s %s %s" % (r['id'], r['year'], r['make'], r['model']) for r in armed))
        print(msg, flush=True)
        if requests and TG_BOT and TG_CHAT:
            try:
                requests.post("https://api.telegram.org/bot%s/sendMessage" % TG_BOT,
                              data={'chat_id': TG_CHAT, 'text': msg}, timeout=10)
            except Exception as e:
                print("telegram failed: %s" % e, flush=True)
    else:
        print("no stuck bids", flush=True)

if __name__ == '__main__':
    main()
