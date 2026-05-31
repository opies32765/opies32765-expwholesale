#!/usr/bin/env python3
"""ew_assess_claim_watchdog — recover assessment claims orphaned by a killed
daemon thread (gunicorn restart / SIGTERM / OOM).

A bid with ai_assessed_at SET but ai_price AND ai_assessment BOTH NULL for >5min
is stuck "ANALYZING" forever: _maybe_fire_assessment bails on `ai_assessed_at IS
NOT NULL`, so it never re-fires. This reaper resets the claim and re-runs
_run_assessment directly (NO customer SMS — that lives only in
_maybe_fire_assessment, verified). Runs every 5 min via cron.

Safe-scoped: only bids whose claim is 5min–3days old (never touches historical
work), caps at 25/run. Imports `app` ONLY when there is actually something to
re-fire (the common case is a single SELECT that exits immediately).
"""
import sys, os, glob

def _dburl():
    u = os.environ.get('DATABASE_URL')
    if u:
        return u
    for p in glob.glob('/proc/[0-9]*/cmdline'):
        try:
            cl = open(p, 'rb').read()
        except Exception:
            continue
        if b'wsgi:app' in cl and b'9001' in cl:
            pid = p.split('/')[2]
            for kv in open(f'/proc/{pid}/environ', 'rb').read().split(b'\x00'):
                if kv.startswith(b'DATABASE_URL='):
                    return kv.split(b'=', 1)[1].decode('utf-8', 'replace')
    return None

import psycopg2, psycopg2.extras
conn = psycopg2.connect(_dburl())
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
    SELECT id FROM bids
     WHERE ai_assessed_at IS NOT NULL
       AND ai_price IS NULL AND ai_assessment IS NULL
       AND ai_assessed_at < NOW() - INTERVAL '5 minutes'
       AND ai_assessed_at > NOW() - INTERVAL '3 days'
     ORDER BY id DESC LIMIT 25
""")
stuck = [r['id'] for r in cur.fetchall()]
if not stuck:
    conn.close()
    sys.exit(0)

print(f'[assess-watchdog] resetting+refiring {len(stuck)} stuck claim(s): {stuck}',
      flush=True)
cur.execute("UPDATE bids SET ai_assessed_at = NULL WHERE id = ANY(%s)", (stuck,))
conn.commit()
conn.close()

sys.path.insert(0, '/opt/expwholesale')
import app
for bid_id in stuck:
    try:
        app._run_assessment(bid_id)   # recompute + persist; no SMS
        print(f'[assess-watchdog] re-assessed bid {bid_id}', flush=True)
    except Exception as e:
        print(f'[assess-watchdog] bid {bid_id} re-assess err: {e}', flush=True)
