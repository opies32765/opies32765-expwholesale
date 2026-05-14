#!/usr/bin/env python3
# /usr/local/bin/ew_stuck_job_sweeper.py
#
# Auto-release vauto worker_jobs that have been in_progress longer than
# STUCK_THRESHOLD_SECONDS without completing. Runs on a 1-min cron.
#
# What "stuck" means: vauto p95 = 74s, max-ever = 88s in last 7 days
# (worker_jobs durations table). 90s is safely above p95 — anything past
# that is almost certainly hung (Cox session expiry, modal stuck, slow
# vAuto render). Manual /admin/workers release is the existing recourse;
# this automates it.
#
# Action per stuck job:
#   1. UPDATE worker_jobs SET status='released_auto_stuck', completed_at=NOW(),
#      duration_ms, error='auto-released after Ns'
#   2. UPDATE bids SET vauto_claimed_by=NULL, vauto_claimed_at=NULL
#      → bid is re-claimable by the next worker poll (~5s)
#   3. Telegram alert per release with bid_id + worker_id + age
#
# Only sweeps job_type='vauto'. Other types (verify) have much longer
# natural durations (p95 218s, occasional 3h+).
import os
import sys
import time
import psycopg2
import psycopg2.extras

DB_URL = os.environ.get('DATABASE_URL',
                        'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
STUCK_THRESHOLD_SECONDS = int(os.environ.get('EW_STUCK_THRESHOLD_SECONDS', '90'))
TG_BOT = '8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT = '7985611488'


def tg(msg):
    """Best-effort Telegram alert. Never crashes the sweep."""
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({'chat_id': TG_CHAT, 'text': msg}).encode()
        url = f'https://api.telegram.org/bot{TG_BOT}/sendMessage'
        urllib.request.urlopen(url, data=data, timeout=8).read()
    except Exception:
        pass


def main():
    conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    # Find in-progress vauto jobs older than threshold.
    # status IS NULL is the unfinished state (only set on completion or release).
    cur.execute("""
        SELECT id AS job_id, bid_id, worker_id, job_type,
               EXTRACT(EPOCH FROM (NOW() - claimed_at))::int AS age_sec
          FROM worker_jobs
         WHERE completed_at IS NULL
           AND status IS NULL
           AND job_type = 'vauto'
           AND claimed_at < NOW() - (%s || ' seconds')::interval
         ORDER BY id ASC
    """, (STUCK_THRESHOLD_SECONDS,))
    stuck = cur.fetchall()

    if not stuck:
        conn.close()
        return 0

    released = []
    bids_to_reset = set()
    for j in stuck:
        try:
            cur.execute("""
                UPDATE worker_jobs
                   SET completed_at = NOW(),
                       status = 'released_auto_stuck',
                       duration_ms = EXTRACT(EPOCH FROM (NOW()-claimed_at))::int*1000,
                       error = %s
                 WHERE id = %s AND completed_at IS NULL
            """, (f'auto-released after {j["age_sec"]}s (>{STUCK_THRESHOLD_SECONDS}s threshold)',
                  j['job_id']))
            if cur.rowcount:
                released.append(j)
                if j['bid_id']:
                    bids_to_reset.add(j['bid_id'])
        except Exception as e:
            print(f'[stuck-sweep] failed to release job {j["job_id"]}: {e}', flush=True)
            conn.rollback()
            continue

    # Reset claim state on each affected bid so the next worker poll re-claims.
    for bid_id in bids_to_reset:
        cur.execute("""
            UPDATE bids
               SET vauto_claimed_by = NULL,
                   vauto_claimed_at = NULL
             WHERE id = %s
        """, (bid_id,))

    conn.commit()
    conn.close()

    # Telegram alert
    if released:
        lines = [f"bid {j['bid_id']} on {j['worker_id']} (age {j['age_sec']}s)"
                 for j in released]
        tg(f"🔧 EW auto-released {len(released)} stuck vauto job(s) "
           f"(>{STUCK_THRESHOLD_SECONDS}s):\n" + "\n".join(lines))
        for j in released:
            print(f'[stuck-sweep] released job {j["job_id"]} '
                  f'bid={j["bid_id"]} worker={j["worker_id"]} age={j["age_sec"]}s',
                  flush=True)

    return len(released)


if __name__ == '__main__':
    n = main()
    sys.exit(0 if n >= 0 else 1)
