#!/opt/expwholesale/venv/bin/python3
"""WARM_BID_CACHE_2026_05_20: periodic PG-page-cache warmer.

Run every 5 min via cron. For every recent active bid, forces PG to read
the heap + TOAST pages that bid_detail will read on first click — keeping
them hot in shared_buffers + OS page cache.

Strategy: use octet_length() on the big JSONB columns. PG must de-TOAST
the data to compute length, which loads the pages into cache. But Python
only receives small integers, so no wasted bandwidth/parsing. For small
tables we SELECT bid_id (forces heap page read).

Total runtime target: <2s for ~200 active bids. Safe to run concurrently
with workers — pure read, MVCC handles snapshots.

Logs to stderr (cron captures to /var/log/ew_warm_cache.log).
"""
import os
import sys
import time

import psycopg2

DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale',
)

# Per-table warm query. Each query MUST touch the rows bid_detail reads
# (heap pages) and the JSONB columns it de-TOASTs (TOAST pages). We use
# octet_length() to force TOAST reads while returning tiny integers.
#
# Column lists mirror app.py bid_detail handler. If those JSONB columns
# change in bid_detail, update here too.
TOUCH_QUERIES = [
    # bids: most cols are inline, no big TOAST. Read a few cols to ensure
    # the heap page loads.
    ('bids',
     "SELECT id, vin, year, make, model FROM bids WHERE id = ANY(%s)"),
    # vauto_lookups: heaviest — market_intel_cached (10MB), api_price_guides
    # (1.8MB) and api_carfax all live in TOAST. Force read via octet_length.
    ('vauto_lookups',
     "SELECT bid_id, "
     "  COALESCE(octet_length(market_intel_cached::text), 0), "
     "  COALESCE(octet_length(api_price_guides::text), 0), "
     "  COALESCE(octet_length(api_carfax::text), 0) "
     "FROM vauto_lookups WHERE bid_id = ANY(%s)"),
    # ai_assessment_log: breakdown + dealer_intel + buyer_intel are JSONB.
    ('ai_assessment_log',
     "SELECT bid_id, "
     "  COALESCE(octet_length(breakdown::text), 0), "
     "  COALESCE(octet_length(dealer_intel::text), 0), "
     "  COALESCE(octet_length(buyer_intel::text), 0) "
     "FROM ai_assessment_log WHERE bid_id = ANY(%s)"),
    # Small tables — SELECT id is enough to warm heap pages.
    ('accutrade_lookups',
     "SELECT bid_id FROM accutrade_lookups WHERE bid_id = ANY(%s)"),
    ('ipacket_lookups',
     "SELECT bid_id FROM ipacket_lookups WHERE bid_id = ANY(%s)"),
    ('bid_photos',
     "SELECT bid_id, url, local_path FROM bid_photos WHERE bid_id = ANY(%s)"),
    ('bid_messages',
     "SELECT bid_id, message FROM bid_messages WHERE bid_id = ANY(%s)"),
    ('bid_partner_offers',
     "SELECT bid_id FROM bid_partner_offers WHERE bid_id = ANY(%s)"),
    ('valuations',
     "SELECT bid_id FROM valuations WHERE bid_id = ANY(%s)"),
]


def main():
    t0 = time.time()
    try:
        conn = psycopg2.connect(DB_URL)
    except Exception as e:
        print(f'[warm] db connect failed: {e}', file=sys.stderr, flush=True)
        return 1
    cur = conn.cursor()

    cur.execute("""
        SELECT id FROM bids
         WHERE status IN ('reviewing','new','bid_sent')
           AND created_at > NOW() - INTERVAL '7 days'
         ORDER BY updated_at DESC NULLS LAST, created_at DESC
    """)
    bid_ids = [r[0] for r in cur.fetchall()]

    if not bid_ids:
        conn.close()
        print('[warm] no active bids to warm', flush=True)
        return 0

    breakdown = []
    total_rows = 0
    for table, q in TOUCH_QUERIES:
        t1 = time.time()
        try:
            cur.execute(q, (bid_ids,))
            rows = cur.fetchall()
            total_rows += len(rows)
            breakdown.append(f'{table}={int((time.time()-t1)*1000)}ms/{len(rows)}r')
        except Exception as e:
            breakdown.append(f'{table}=ERR:{type(e).__name__}:{str(e)[:60]}')
            conn.rollback()

    conn.close()

    total_ms = int((time.time() - t0) * 1000)
    print(
        f'[warm] bids={len(bid_ids)} rows={total_rows} total_ms={total_ms} '
        + ' '.join(breakdown),
        flush=True,
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
