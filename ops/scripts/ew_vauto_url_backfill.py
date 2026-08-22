#!/usr/bin/env python3
"""ew_vauto_url_backfill.py — VAUTO_URL_SERVERSIDE_2026_08_21.

Any bid whose vAuto appraisal permalink the worker failed to capture gets it
filled in from the server, and the direct rBook + Manheim enrichment is kicked
so the car is priced on real comps instead of none.

WHY
    Capturing the permalink used to be a one-shot race inside the worker: save
    the appraisal, then find it again through the ProVision UI within ~9s. On
    2026-08-21 vAuto stopped surfacing a just-saved appraisal that fast and the
    capture went from ~99% to 0/5 overnight. Nothing retried (the documented
    "VM 120" fallback has been dead since May and the VM no longer exists) and
    nothing alerted, so five cars were priced with 0 mmr_tx and 0 rbook.

    The appraisals were never missing — only un-findable for a few minutes. So
    this stops racing and simply looks them up later, server-side.

WHAT IT DOES  (every 5 min, cron)
    1. find bids whose vauto_lookups row still has no usable appraisal_url
    2. resolve VIN -> permalink via vauto_appraisal_lookup (no browser)
    3. POST it to the worker's OWN endpoint, /api/vauto/url_capture_result,
       which writes appraisal_url and kicks kick_direct_enrichment +
       kick_direct_manheim. Reusing that endpoint means zero duplicated
       downstream wiring.
    4. after MAX_ATTEMPTS, alert ONCE per bid on Telegram *and email*. The
       2026-08-21 home-standby outage sat unseen for 18h because its monitor
       was Telegram-only; an alert channel the operator does not read is not
       monitoring.

SAFETY
    - exits immediately on a standby (pg_is_in_recovery) — only the primary sweeps
    - flock, so a slow run can never overlap the next tick
    - bounded: MAX_PER_RUN bids per tick, and only bids from the last LOOKBACK_DAYS
    - waits GRACE_MINUTES so the worker keeps first crack at its own capture
    - the lookup verifies the VIN column before accepting a row, so a fuzzy
      search can never bolt another car's appraisal onto a bid
    - off switch: rm /opt/expwholesale/VAUTO_URL_BACKFILL_ON
"""
import json
import os
import socket
import sys
import time
import urllib.request

sys.path.insert(0, '/opt/expwholesale')
os.chdir('/opt/expwholesale')

import psycopg2
import psycopg2.extras
import requests

from vauto_appraisal_lookup import resolve_appraisal_url

DB_URL = os.environ.get('DATABASE_URL') or (
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
EW_LOCAL = 'http://127.0.0.1:9001'
ENABLE_FLAG = '/opt/expwholesale/VAUTO_URL_BACKFILL_ON'

LOOKBACK_DAYS = 4
GRACE_MINUTES = 4       # let the worker's own capture win first
MAX_PER_RUN = 25
MAX_ATTEMPTS = 6        # ~30 min at a 5-min tick, then alert once
REQUEUE_AFTER = 3       # `no_rows` this many times => the appraisal was never
                        # saved; re-run the vAuto leg ONCE (see _requeue_vauto)

TG_BOT = '8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT = '7985611488'
ALERT_FROM = 'EW Ops Alerts <alerts@experience-wholesale.net>'
ALERT_TO = os.environ.get('EW_ALERT_TO', 'opies32765@gmail.com')


def log(msg):
    print('[%s] %s' % (time.strftime('%Y-%m-%dT%H:%M:%S%z'), msg), flush=True)


def db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor,
                            connect_timeout=10)


def ensure_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vauto_url_backfill_log (
            bid_id       integer PRIMARY KEY,
            attempts     integer     NOT NULL DEFAULT 0,
            first_seen   timestamptz NOT NULL DEFAULT NOW(),
            last_attempt timestamptz,
            last_reason  text,
            resolved_at  timestamptz,
            alerted_at   timestamptz
        )""")
    cur.execute("ALTER TABLE vauto_url_backfill_log "
                "ADD COLUMN IF NOT EXISTS requeued_at timestamptz")


def _requeue_vauto(cur, bid_id):
    """`no_rows` means the appraisal was NEVER SAVED — a different fault from a
    permalink we merely failed to capture. In the worker, `clickSave()` only runs
    when `clickActions()` returns "clicked", the whole thing sits in a bare
    try/except, and neither branch logs anything: a silent no-save.

    It is intermittent, so one clean re-run of the vAuto leg usually fixes it.
    Make the bid claimable again the LIGHTEST way that works: the claim gate
    (PHASE1_MILES_GATE_2026_05_15) skips bids whose vauto_lookups row has
    raw_json set or appraisal_url='__not_found__', so clear just those two and
    release the claim. Deliberately NOT a DELETE — that would throw away the
    Carfax/AutoCheck JSON and screenshots on the row. AccuTrade and iPacket are
    untouched, so no iPacket re-pull (account-ban risk).

    Once per bid, ever.
    """
    cur.execute("""UPDATE vauto_lookups
                      SET raw_json = NULL,
                          appraisal_url = NULLIF(appraisal_url, '__not_found__')
                    WHERE bid_id = %s""", (bid_id,))
    cur.execute("""UPDATE bids
                      SET vauto_claimed_by = NULL, vauto_claimed_at = NULL,
                          vauto_priority = TRUE
                    WHERE id = %s""", (bid_id,))
    cur.execute("UPDATE vauto_url_backfill_log SET requeued_at = NOW() WHERE bid_id = %s",
                (bid_id,))


def tg(msg):
    try:
        requests.post('https://api.telegram.org/bot%s/sendMessage' % TG_BOT,
                      data={'chat_id': TG_CHAT, 'text': msg}, timeout=10)
    except Exception as e:
        log('telegram failed (non-fatal): %s' % e)


def mail(subject, body):
    """Second, independent channel. Telegram is easy to miss; mail survives a
    muted phone. Never let a failed alert break the sweep."""
    key = os.environ.get('RESEND_API_KEY', '')
    if not key:
        log('no RESEND_API_KEY — email skipped')
        return
    html = (
        '<div style="font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;color:#1a1a1a;max-width:560px">'
        '<div style="border-left:4px solid #b3261e;padding:2px 0 2px 14px">'
        '<div style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#b3261e;'
        'font-weight:600">EW vAuto appraisal backfill</div>'
        '<div style="font-size:19px;font-weight:600;margin-top:2px">%s</div></div>'
        '<p>%s</p>'
        '<p style="font-size:12.5px;color:#666;margin-top:20px">ew_vauto_url_backfill on C1. '
        'Without an appraisal permalink a bid gets no rBook comps and no Manheim '
        'transactions, and the AI prices it on no market data.</p></div>') % (subject, body)
    try:
        requests.post('https://api.resend.com/emails',
                      headers={'Authorization': 'Bearer %s' % key,
                               'Content-Type': 'application/json'},
                      data=json.dumps({'from': ALERT_FROM,
                                       'to': [a.strip() for a in ALERT_TO.split(',') if a.strip()],
                                       'subject': subject, 'html': html}),
                      timeout=15)
    except Exception as e:
        log('email failed (non-fatal): %s' % e)


def post_capture(bid_id, vin, url):
    r = requests.post(EW_LOCAL + '/api/vauto/url_capture_result',
                      json={'bid_id': bid_id, 'vin': vin, 'appraisal_url': url},
                      timeout=60)
    return r.status_code, (r.text or '')[:200]


def main():
    if not os.path.exists(ENABLE_FLAG):
        return 0

    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT pg_is_in_recovery() AS standby')
    if cur.fetchone()['standby']:
        log('standby — not sweeping')
        return 0
    ensure_schema(cur)
    conn.commit()

    # '' and '__not_found__' both mean "the worker tried and came back empty",
    # which is exactly the case worth rescuing — the appraisal does exist.
    cur.execute("""
        SELECT b.id AS bid_id, COALESCE(NULLIF(v.vin,''), b.vin) AS vin
          FROM bids b
          JOIN vauto_lookups v ON v.bid_id = b.id
     LEFT JOIN vauto_url_backfill_log l ON l.bid_id = b.id
         WHERE b.created_at > NOW() - INTERVAL '%s days'
           AND v.looked_up_at < NOW() - INTERVAL '%s minutes'
           AND (v.appraisal_url IS NULL OR v.appraisal_url IN ('', '__not_found__'))
           AND COALESCE(NULLIF(v.vin,''), b.vin) IS NOT NULL
           AND l.resolved_at IS NULL
           AND COALESCE(l.attempts, 0) < %s
         ORDER BY b.id DESC
         LIMIT %s
    """ % (LOOKBACK_DAYS, GRACE_MINUTES, MAX_ATTEMPTS, MAX_PER_RUN))
    todo = cur.fetchall()
    if not todo:
        return 0
    log('%d bid(s) missing an appraisal permalink' % len(todo))

    sess = None
    try:
        import app as A
        sess = A._capi_session()
    except Exception as e:
        log('could not load pooled vAuto session: %s' % e)
    if sess is None:
        log('no pooled vAuto session — nothing to do this tick')
        return 0

    fixed = failed = 0
    for row in todo:
        bid_id, vin = row['bid_id'], (row['vin'] or '').strip().upper()
        url, meta = resolve_appraisal_url(sess, vin)
        cur.execute("""
            INSERT INTO vauto_url_backfill_log (bid_id, attempts, last_attempt, last_reason)
            VALUES (%s, 1, NOW(), %s)
            ON CONFLICT (bid_id) DO UPDATE
               SET attempts = vauto_url_backfill_log.attempts + 1,
                   last_attempt = NOW(), last_reason = EXCLUDED.last_reason
            RETURNING attempts""", (bid_id, meta.get('reason')))
        attempts = cur.fetchone()['attempts']
        conn.commit()

        if not url:
            failed += 1
            log('bid %s %s -> %s (attempt %d/%d)'
                % (bid_id, vin, meta.get('reason'), attempts, MAX_ATTEMPTS))
            if meta.get('reason') == 'no_rows' and attempts >= REQUEUE_AFTER:
                cur.execute("SELECT requeued_at FROM vauto_url_backfill_log WHERE bid_id=%s",
                            (bid_id,))
                if not cur.fetchone()['requeued_at']:
                    _requeue_vauto(cur, bid_id)
                    conn.commit()
                    log('bid %s: no appraisal exists in vAuto — vAuto leg re-queued '
                        '(once only; AccuTrade + iPacket untouched)' % bid_id)
                    continue
            if attempts >= MAX_ATTEMPTS:
                cur.execute("SELECT alerted_at FROM vauto_url_backfill_log WHERE bid_id=%s",
                            (bid_id,))
                if not cur.fetchone()['alerted_at']:
                    _extra = ('' if meta.get('reason') != 'no_rows' else
                              ' NOTE: reason=no_rows means the appraisal was never SAVED in'
                              ' vAuto (not just un-findable), and the one automatic re-run of'
                              ' the vAuto leg did not fix it — this one needs a human.')
                    msg = ('EW: bid #%s (%s) still has NO vAuto appraisal permalink after %d '
                           'tries (last: %s). It has no rBook comps and no Manheim data, so its '
                           'price is not grounded in the market.%s'
                           % (bid_id, vin, attempts, meta.get('reason'), _extra))
                    tg('\U0001f534 ' + msg)
                    mail('vAuto permalink unresolved — bid #%s' % bid_id, msg)
                    cur.execute("UPDATE vauto_url_backfill_log SET alerted_at=NOW() "
                                "WHERE bid_id=%s", (bid_id,))
                    conn.commit()
            continue

        code, body = post_capture(bid_id, vin, url)
        if code == 200:
            fixed += 1
            cur.execute("UPDATE vauto_url_backfill_log SET resolved_at=NOW() WHERE bid_id=%s",
                        (bid_id,))
            conn.commit()
            log('bid %s %s -> RESOLVED %s (%s)' % (bid_id, vin, meta.get('appraisal_id'),
                                                   meta.get('ymm')))
        else:
            failed += 1
            log('bid %s post failed HTTP %s %s' % (bid_id, code, body))

    log('done: %d resolved, %d still missing' % (fixed, failed))
    conn.close()
    return 0


if __name__ == '__main__':
    lock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        lock.bind('\0ew_vauto_url_backfill')   # abstract socket = self-releasing flock
    except OSError:
        sys.exit(0)                            # previous run still going
    sys.exit(main())
