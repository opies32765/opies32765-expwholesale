"""
vAuto API enrichment worker.

Runs alongside (not replacing) the existing Selenium worker. Each instance
polls vauto_lookups for rows that haven't been API-enriched yet and:

  1. Extracts the appraisal_id from appraisal_url (the Selenium worker
     populates this on first-pass).
  2. Calls vauto_api.lookup_vehicle → fresh JSON for Carfax flags +
     priceGuides + Manheim.
  3. Updates vauto_lookups.api_carfax / api_price_guides / api_refreshed_at.
  4. Constructs and stores the saved-vAuto URL — replacing Beelink-115's
     URL-capture side-job entirely.

Concurrency: many workers can run simultaneously. Each claims rows via
`SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` so 5 workers polling at the
same instant grab different bids with zero races.

Failure handling:
  - VautoAuthError (401/403) → mark session stale, sleep 60s, retry. The
    cookie_keeper (or manual seed) will refresh the row in vauto_session.
  - VautoApiError (any other) → log, mark this bid's api_refreshed_at to
    NOW() so we don't loop on it. Selenium's data still stands.

Usage:
    python api_worker.py --worker-id api-1
    python api_worker.py --worker-id api-2 --poll-interval 5
    # Run 3-5 instances on Contabo 1 via systemd template unit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests as http_requests

sys.path.insert(0, str(Path(__file__).parent))
from vauto_api import (  # noqa: E402
    VautoSession, VautoAuthError, VautoApiError,
    lookup_vehicle, appraisal_id_from_url, saved_appraisal_url,
)


log = logging.getLogger('api_worker')

DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale',
)
EW_SERVER = os.environ.get('EW_SERVER', 'http://localhost:9000')
SESSION_LABEL = os.environ.get('VAUTO_SESSION_LABEL', 'oscarpas')

# Refresh ceiling — re-enrich a bid if its API data is older than this.
# 6 hours covers a normal day; book values can drift but rarely meaningfully
# fast on the same VIN.
REFRESH_AFTER_HOURS = int(os.environ.get('REFRESH_AFTER_HOURS', '6'))

# Stop running after this many consecutive auth errors — operator should
# refresh the cookie_keeper / vauto_session row before restarting.
MAX_AUTH_FAILURES = 5


_running = True


def _shutdown_handler(signum, frame):
    global _running
    log.info('shutdown signal %s — draining', signum)
    _running = False


signal.signal(signal.SIGINT, _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)


def claim_one(conn, worker_id: str) -> dict | None:
    """Atomically claim one vauto_lookups row that needs enrichment.
    Returns a dict with bid_id, vin, appraisal_url, year/make/model/etc.
    Returns None if no work."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            WITH eligible AS (
                SELECT vl.id
                FROM vauto_lookups vl
                JOIN bids b ON b.id = vl.bid_id
                WHERE vl.appraisal_url IS NOT NULL
                  AND vl.appraisal_url <> ''
                  AND vl.appraisal_url <> '__not_found__'
                  AND (vl.api_refreshed_at IS NULL
                       OR vl.api_refreshed_at < NOW() - INTERVAL '{REFRESH_AFTER_HOURS} hours')
                ORDER BY vl.api_refreshed_at NULLS FIRST, vl.looked_up_at DESC
                FOR UPDATE OF vl SKIP LOCKED
                LIMIT 1
            )
            UPDATE vauto_lookups vl
               SET api_refreshed_at = NOW()  -- claim by stamping; final write replaces
              FROM eligible e, bids b
             WHERE vl.id = e.id
               AND b.id = vl.bid_id
            RETURNING vl.id, vl.bid_id, vl.vin, vl.appraisal_url,
                      b.year, b.make, b.model, b.trim, b.mileage
        """)
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None


def save_enrichment(conn, lookup_id: int, bid_id: int,
                    api_carfax: dict | None,
                    api_price_guides: dict | None,
                    saved_url: str | None) -> None:
    """Persist the API-derived data on the vauto_lookups row."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE vauto_lookups
               SET api_carfax       = %s::jsonb,
                   api_price_guides = %s::jsonb,
                   api_refreshed_at = NOW(),
                   appraisal_url    = COALESCE(NULLIF(%s, ''), appraisal_url)
             WHERE id = %s
        """, (
            json.dumps(api_carfax) if api_carfax is not None else None,
            json.dumps(api_price_guides) if api_price_guides is not None else None,
            saved_url or '',
            lookup_id,
        ))
        conn.commit()


def heartbeat(worker_id: str, ok: bool, paused: bool = False,
              consecutive_failures: int = 0) -> None:
    """Best-effort heartbeat to the EW dispatcher. Never raises."""
    payload = {
        'worker_id': worker_id,
        'priority': 'primary',
        'role': 'api_worker',
        'chrome_alive': True,  # n/a but the column expects a bool
        'paused': paused,
        'consecutive_failures': consecutive_failures,
        'last_claim_status': 'ok' if ok else 'failed',
    }
    try:
        http_requests.post(f'{EW_SERVER}/api/vauto/heartbeat',
                           json=payload, timeout=5)
    except Exception:
        pass  # heartbeat failures shouldn't crash the worker


def process_one(conn, sess: VautoSession, work: dict, worker_id: str) -> tuple[bool, bool]:
    """Returns (ok, auth_error)."""
    bid_id = work['bid_id']
    vin = work['vin']
    appraisal_id = appraisal_id_from_url(work['appraisal_url'])
    if not appraisal_id:
        log.warning('bid=%s no appraisal_id parseable from %r — skipping',
                    bid_id, work['appraisal_url'])
        return False, False

    vehicle = {
        'vin': vin,
        'odometer': work.get('mileage') or 0,
        'year': work.get('year'),
        'make': work.get('make'),
        'model': work.get('model'),
        'series': work.get('trim'),
    }

    t0 = time.monotonic()
    try:
        result = lookup_vehicle(sess, appraisal_id, vehicle)
    except VautoAuthError as e:
        log.error('bid=%s auth error: %s', bid_id, e)
        return False, True
    except VautoApiError as e:
        log.warning('bid=%s api error: %s', bid_id, e)
        return False, False

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    raw = result.get('raw') or {}
    save_enrichment(
        conn,
        lookup_id=work['id'],
        bid_id=bid_id,
        api_carfax=raw.get('carfaxStatus'),
        api_price_guides=raw.get('priceGuides'),
        saved_url=saved_appraisal_url(appraisal_id),
    )

    log.info('bid=%s vin=%s ok in %dms', bid_id, vin, elapsed_ms)
    return True, False


def run(worker_id: str, poll_interval: float):
    log.info('starting api_worker id=%s poll=%.1fs db=%s',
             worker_id, poll_interval, DB_URL.split('@')[-1])

    consecutive_auth = 0

    while _running:
        try:
            conn = psycopg2.connect(DB_URL)
            conn.autocommit = False
        except Exception as e:
            log.error('db connect failed: %s — retrying in 10s', e)
            time.sleep(10)
            continue

        try:
            sess = VautoSession.from_db(conn)
        except VautoApiError as e:
            log.error('cannot load vauto_session: %s — retry 30s', e)
            conn.close()
            heartbeat(worker_id, ok=False, paused=True,
                      consecutive_failures=consecutive_auth)
            time.sleep(30)
            continue

        try:
            while _running:
                work = claim_one(conn, worker_id)
                if not work:
                    heartbeat(worker_id, ok=True)
                    time.sleep(poll_interval)
                    continue

                ok, is_auth = process_one(conn, sess, work, worker_id)
                if is_auth:
                    consecutive_auth += 1
                    heartbeat(worker_id, ok=False, paused=True,
                              consecutive_failures=consecutive_auth)
                    if consecutive_auth >= MAX_AUTH_FAILURES:
                        log.error('hit %d consecutive auth failures — '
                                  'pausing 5 min for cookie refresh',
                                  consecutive_auth)
                        time.sleep(300)
                        consecutive_auth = 0
                    else:
                        time.sleep(30)
                    break  # reload session
                else:
                    consecutive_auth = 0
                    heartbeat(worker_id, ok=ok)
        finally:
            conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--worker-id', default=os.environ.get('WORKER_ID', 'api-1'))
    ap.add_argument('--poll-interval', type=float, default=2.0,
                    help='Seconds between poll attempts when queue is empty')
    ap.add_argument('--log-level', default='INFO')
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    )

    run(args.worker_id, args.poll_interval)


if __name__ == '__main__':
    main()
