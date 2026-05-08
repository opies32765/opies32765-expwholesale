#!/usr/bin/env python3
"""Hourly cron: backfill carfax_share_url for any vauto_lookups row that
has a VIN but no share URL yet. Quiet in normal operation; logs errors
only.

Install:
  /opt/expwholesale/scripts/backfill_carfax_share.py
  Crontab: */5 * * * * /usr/bin/python3 /opt/expwholesale/scripts/backfill_carfax_share.py >> /var/log/ew_carfax_backfill.log 2>&1
"""
import json
import os
import sys
import time
from datetime import datetime

import psycopg2
import psycopg2.extras
import requests

DB_URL = os.environ.get('DATABASE_URL',
                        'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
BFF = 'https://slot1.bff.megazord.vauto.app.coxautoinc.com'
LOOKBACK_HOURS = 48  # backfill bids from last 48h
MAX_PER_RUN = 50     # don't hammer Cox if there's a huge backlog


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Load session
    cur.execute("SELECT cookies, entity_id, platform_user_id, NOW() - refreshed_at AS age FROM vauto_session WHERE label='oscarpas'")
    sess_row = cur.fetchone()
    if not sess_row:
        log("ERROR: no vauto_session row")
        sys.exit(1)
    age = sess_row['age'].total_seconds() if sess_row['age'] else None
    if age is None or age > 600:  # 10 minutes
        log(f"WARN: session is {age}s old — cookie keeper may be down")

    cookies = sess_row['cookies'] if isinstance(sess_row['cookies'], dict) else json.loads(sess_row['cookies'])
    s = requests.Session()
    s.cookies.update(cookies)
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
        'Accept': 'application/json,text/html,*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'appraisalentityid': sess_row['entity_id'],
        'currententityid': sess_row['entity_id'],
        'platformuserid': sess_row['platform_user_id'],
        'Origin': 'https://provision.vauto.app.coxautoinc.com',
        'Referer': 'https://provision.vauto.app.coxautoinc.com/',
    })

    # Find candidates
    cur.execute(f"""
        SELECT vl.bid_id, vl.vin
          FROM vauto_lookups vl
          JOIN bids b ON b.id = vl.bid_id
         WHERE vl.vin IS NOT NULL
           AND vl.carfax_share_url IS NULL
           AND b.created_at > NOW() - INTERVAL '{LOOKBACK_HOURS} hours'
         ORDER BY vl.bid_id DESC
         LIMIT {MAX_PER_RUN}
    """)
    candidates = cur.fetchall()
    if not candidates:
        log(f"OK no backfill needed ({LOOKBACK_HOURS}h lookback)")
        conn.close()
        return

    log(f"backfilling {len(candidates)} bids")
    cur2 = conn.cursor()
    n_ok = 0
    n_no_carfax = 0
    n_err = 0
    for r in candidates:
        vin = r['vin']
        bid_id = r['bid_id']
        try:
            rep = s.get(f'{BFF}/api/carfax/report?vin={vin}', timeout=15)
            if rep.status_code != 200:
                if rep.status_code in (401, 403):
                    log(f"AUTH FAIL on bid {bid_id}: {rep.status_code} — session may need refresh")
                    break
                n_err += 1
                continue
            gateway = (rep.json().get('report') or {}).get('url')
            if not gateway:
                n_no_carfax += 1
                # Mark as 'no carfax' to avoid re-querying — use empty string
                cur2.execute("UPDATE vauto_lookups SET carfax_share_url = '' WHERE bid_id=%s",
                             (bid_id,))
                conn.commit()
                continue
            r2 = s.get(gateway, allow_redirects=False, timeout=15)
            if r2.status_code in (301, 302, 303, 307, 308):
                target = r2.headers.get('Location') or ''
                if 'connect.carfax.com' in target:
                    cur2.execute("UPDATE vauto_lookups SET carfax_share_url=%s WHERE bid_id=%s",
                                 (target, bid_id))
                    conn.commit()
                    n_ok += 1
                    continue
            n_err += 1
        except requests.RequestException as e:
            log(f"network error on bid {bid_id}: {e}")
            n_err += 1
        time.sleep(0.3)  # polite throttle

    log(f"DONE: {n_ok} stored, {n_no_carfax} no-record, {n_err} errors")
    conn.close()


if __name__ == '__main__':
    main()
