"""assessment_watchdog.py — re-fire AI assessment for bids where the
gunicorn worker died mid-Gemini call.

A bid enters the "stuck ANALYZING" state when:
  1. `_maybe_fire_assessment` atomically sets `ai_assessed_at=NOW()`.
  2. The daemon thread running `_run_assessment` (which calls Gemini)
     is killed by SIGTERM / SIGHUP / worker recycle before completion.
  3. Daemon threads don't survive worker death and Python exception
     handlers don't run for OS signals, so `_release_assessment_claim`
     never fires. `ai_assessed_at` stays set, `ai_assessment`/`ai_price`
     stay NULL, and the gate at `_maybe_fire_assessment` permanently
     blocks re-fire (`if row['ai_assessed_at'] is not None: return False`).

Definition of orphaned: claim older than ORPHAN_THRESHOLD_MIN with no
result. Gemini 2.5 Pro never takes > ~60s. Anything older is dead.

Recovery: clear the claim, POST /api/bid/<id>/assess to fire a fresh
Gemini call. The endpoint runs synchronously; we just wait for it.

Designed to be idempotent. Safe to run every 5 minutes via cron.
First run on 2026-05-20 also serves as the bulk recovery for the 21
bids accumulated over the prior 10 days.
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import requests

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')
APP_HOST = os.environ.get('EW_APP_HOST', 'http://127.0.0.1:9001')
ORPHAN_THRESHOLD_MIN = int(os.environ.get('ORPHAN_THRESHOLD_MIN', '2'))
TIMEOUT_SEC = 120

# /api/bid/<id>/assess sits behind the global @before_request login gate.
# We log in once with the same creds the human operator uses and reuse
# the session cookie for every refire in the run. Defaults mirror
# EW_USERNAME / EW_PASSWORD fallbacks in app.py.
EW_USERNAME = os.environ.get('EW_USERNAME', 'admin')
EW_PASSWORD = os.environ.get('EW_PASSWORD', 'Sedecrem3')

TG_BOT = os.environ.get('TELEGRAM_BOT_TOKEN',
                         '8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '7985611488')

log = logging.getLogger('assessment_watchdog')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)


def telegram(msg):
    try:
        requests.post(f'https://api.telegram.org/bot{TG_BOT}/sendMessage',
                      data={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'},
                      timeout=10)
    except Exception as e:
        log.warning('telegram failed: %s', e)


def find_orphans(cur, threshold_min, limit=50):
    """Returns bids that should have been assessed by now but weren't.

    Catches both failure modes:
      1. Stuck claim: ai_assessed_at IS NOT NULL but Gemini never wrote
         a result (worker SIGTERMed mid-call).
      2. Cleared claim, never re-fired: ai_assessed_at IS NULL but full
         enrichment landed long enough ago that something is wrong.

    Only fires when ALL enrichment is present (mirrors the gate in
    `_maybe_fire_assessment` with require_all=True). Excludes bids the
    operator has flagged for verification but not yet cleared.
    """
    cur.execute("""
        SELECT b.id, b.year, b.make, b.model, b.ai_assessed_at,
               EXTRACT(EPOCH FROM (NOW() - COALESCE(
                   b.ai_assessed_at,
                   vl.manheim_completed_at,
                   vl.rbook_completed_at,
                   b.created_at
               )))/60.0 AS age_min
          FROM bids b
          JOIN vauto_lookups vl ON vl.bid_id = b.id
         WHERE b.ai_price IS NULL
           AND (b.ai_assessment IS NULL OR b.ai_assessment = '')
           AND b.mileage IS NOT NULL
           AND vl.rbook_completed_at IS NOT NULL
           AND vl.manheim_completed_at IS NOT NULL
           AND EXISTS (SELECT 1 FROM accutrade_lookups WHERE bid_id = b.id)
           AND EXISTS (SELECT 1 FROM ipacket_lookups   WHERE bid_id = b.id)
           AND COALESCE(
                   b.ai_assessed_at,
                   vl.manheim_completed_at,
                   vl.rbook_completed_at,
                   b.created_at
               ) < NOW() - (%s || ' minutes')::interval
           AND (b.needs_verification_at IS NULL
                OR b.needs_verification_cleared_at IS NOT NULL)
         ORDER BY 1 ASC
         LIMIT %s
    """, (str(threshold_min), limit))
    return [dict(r) for r in cur.fetchall()]


def reset_claim(db, cur, bid_id):
    """Idempotently clear ai_assessed_at. Only updates when no result
    has been written (mirrors `_release_assessment_claim` guard). No-op
    if the claim is already NULL — the watchdog also catches the case
    where the claim was already cleared but nobody re-fired the assess.
    """
    cur.execute("""
        UPDATE bids SET ai_assessed_at = NULL
         WHERE id = %s
           AND ai_price IS NULL
           AND (ai_assessment IS NULL OR ai_assessment = '')
           AND ai_assessed_at IS NOT NULL
    """, (bid_id,))
    db.commit()


def login_session():
    """Return a requests.Session() with the admin login cookie set, or
    None if login failed. The endpoint we POST to is gated by the global
    @before_request login check that the human UI also goes through."""
    s = requests.Session()
    try:
        r = s.post(f'{APP_HOST}/login',
                   data={'username': EW_USERNAME, 'password': EW_PASSWORD},
                   timeout=15, allow_redirects=False)
    except requests.RequestException as e:
        log.error('login request failed: %s', e)
        return None
    # Successful login = 302 redirect to /. Invalid creds = 200 with the
    # login form re-rendered. Anything else is unexpected.
    if r.status_code == 302 and r.headers.get('Location', '').endswith('/'):
        return s
    log.error('login failed status=%s loc=%s', r.status_code, r.headers.get('Location'))
    return None


def fire_assessment(session, bid_id):
    url = f'{APP_HOST}/api/bid/{bid_id}/assess'
    try:
        r = session.post(url, timeout=TIMEOUT_SEC)
        return r.status_code, r.text[:200]
    except requests.RequestException as e:
        return None, f'{type(e).__name__}: {e}'


def run(dry_run=False, alert_threshold=3, limit=50):
    with psycopg2.connect(**DB) as db:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            orphans = find_orphans(cur, ORPHAN_THRESHOLD_MIN, limit=limit)
            log.info('orphan_count=%d threshold_min=%d',
                     len(orphans), ORPHAN_THRESHOLD_MIN)
            if not orphans:
                return 0

            session = None
            if not dry_run:
                session = login_session()
                if session is None:
                    log.error('login failed — aborting recovery')
                    telegram('⚠️ EW assessment watchdog: login failed, '
                             f'{len(orphans)} orphans unrecovered')
                    return 1

            for o in orphans:
                desc = f"bid={o['id']} {o['year']} {o['make']} {o['model']} age={o['age_min']:.1f}min"
                if dry_run:
                    log.info('[dry-run] would recover %s', desc)
                    continue

                reset_claim(db, cur, o['id'])
                status, body = fire_assessment(session, o['id'])
                if status == 200:
                    log.info('recovered %s', desc)
                else:
                    log.warning('refire-failed %s status=%s body=%s',
                                desc, status, body)
                # Tiny pause so we don't slam gunicorn with 21 concurrent
                # Gemini calls all at once on the bulk-recovery first run.
                time.sleep(2)

            # Telegram alert only on bulk events. Single-bid recoveries
            # happen often enough during dev that we don't want a chat
            # full of one-liners.
            if not dry_run and len(orphans) >= alert_threshold:
                names = ', '.join(f"#{o['id']}" for o in orphans[:10])
                if len(orphans) > 10:
                    names += f', +{len(orphans)-10} more'
                telegram(
                    f'<b>EW assessment watchdog</b>\n'
                    f'recovered {len(orphans)} orphaned bid'
                    f"{'s' if len(orphans) != 1 else ''}: {names}\n"
                    f'log: /var/log/ew_assessment_watchdog.log'
                )
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true',
                    help='List orphans without recovering')
    p.add_argument('--alert-threshold', type=int, default=3,
                    help='Min orphans for telegram alert (default 3)')
    p.add_argument('--limit', type=int, default=50,
                    help='Max orphans to recover per run (default 50)')
    args = p.parse_args()
    try:
        return run(args.dry_run, args.alert_threshold, args.limit)
    except Exception as e:
        log.exception('watchdog failed: %s', e)
        telegram(f'⚠️ EW assessment watchdog CRASHED: {e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
