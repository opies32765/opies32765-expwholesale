"""autobuy_alert.py — AUTOBUY_ALERT_2026_08_17

Text Joe when Autobuy submits a car for a bid. That's it.

Operator ask 2026-08-17. Autobuy (LSL supplier 5005 — 3,648 cars / $128.4M
bought from them, dormant since 2025-11-27) applied, was approved, and
submitted bid 5924 minutes later.

Install:
  * * * * * /opt/expwholesale/venv/bin/python /opt/expwholesale/autobuy_alert.py >> /var/log/autobuy_alert.log 2>&1

Design notes (deliberate):
  * Standalone cron, NOT a hook in the bid/assessment path. If this script
    breaks, intake and enrichment are untouched; and it cannot be starved by a
    stalled assessment the way last night's muted worker alerts were.
  * Fires on submission; does not wait for pricing. No valuation in the text —
    the numbers stay on the bid page behind Joe's login.
  * Uses app.send_sms() (never Twilio directly) so bot_mute / STOP / magic
    numbers are all still honoured.
  * BOUNDED retry (MAX_ATTEMPTS): a transient failure is retried, a permanent
    one gives up instead of looping forever. Never one-shot — see the Twilio
    creds note below for why that mattered.
"""
import os
import sys
import time
import traceback

sys.path.insert(0, '/opt/expwholesale')
os.environ.setdefault(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')

_TWILIO_KEYS = ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE')
_UNIT = '/etc/systemd/system/expwholesale.service'


def _load_twilio_env():
    """TWILIO_CREDS_2026_08_17 -- a cron process does NOT inherit the systemd
    unit's Environment=, and send_sms() returns False *silently* (no log line,
    no exception) when these are empty. Without them this job would run every
    minute, send nothing, and Joe would get nothing forever.

    Read them from the unit file rather than hardcoding: hardcoded creds got
    this file REJECTED by GitHub secret scanning on the first push, so the
    literals must never come back. Aborts loudly if they are missing, because
    the silent-False failure mode is the whole problem.
    """
    try:
        with open(_UNIT, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith('Environment='):
                    continue
                kv = line[len('Environment='):].strip().strip('"')
                if '=' not in kv:
                    continue
                k, v = kv.split('=', 1)
                if k in _TWILIO_KEYS:
                    os.environ.setdefault(k, v.strip().strip('"'))
    except Exception as exc:
        print('[autobuy-alert] WARN could not read %s: %s' % (_UNIT, exc),
              flush=True)
    missing = [k for k in _TWILIO_KEYS if not os.environ.get(k)]
    if missing:
        sys.exit('[autobuy-alert] FATAL missing %s -- send_sms() would return '
                 'False silently. Aborting so this is visible instead of a '
                 'nightly stream of no-ops.' % ', '.join(missing))


_load_twilio_env()

import app  # noqa: E402  — get_db() and send_sms()

# Joe Humphries. 352-209-9696 is his working cell (1,227 bids submitted from
# it) and is the only one of his two known numbers on the enrichment
# allowlist. His other, 754-233-1911 from lsl_sales_reps, is likely an office
# DID -- do not swap without checking.
JOE = '3522099696'

# Autobuy = LSL supplier 5005. DealerPrice submissions self-attribute, so this
# is stamped automatically (verified on bid 5924).
AUTOBUY_SUPPLIER_ID = 5005

BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'https://experience-wholesale.net')
LOOKBACK_HOURS = 24   # backfill guard: never text about anything older
MAX_ATTEMPTS = 3
BATCH = 25


def _ts():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _fmt_phone(p):
    d = ''.join(c for c in (p or '') if c.isdigit())[-10:]
    return '%s-%s-%s' % (d[:3], d[3:6], d[6:]) if len(d) == 10 else (p or '')


def compose(r):
    """LINK_NOT_LAST_2026_08_17: the URL must NOT be the final line. A trailing
    URL makes iMessage/SMS clients render a link-preview card, which lands as a
    SECOND bubble on Joe's phone. Keeping plain text after the link keeps it to
    one bubble. Do not move the link back to the bottom."""
    bits = [str(r['year'] or '').strip(), (r['make'] or '').strip().title(),
            (r['model'] or '').strip(), (r['trim'] or '').strip()]
    car = ' '.join(b for b in bits if b) or (r['vin'] or '')
    if r.get('mileage'):
        car += ' - %s mi' % format(int(r['mileage']), ',')

    who = (r.get('contact_name') or '').strip().split(' ')[0].title()
    contact = ' '.join(x for x in (who, _fmt_phone(r.get('contact_phone'))) if x)

    body = 'Autobuy just submitted Bid #%s\n%s/bid/%s\n%s' % (
        r['bid_id'], BASE_URL, r['bid_id'], car)
    if contact.strip():
        body += '\n' + contact
    return body


def main():
    with app.get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS autobuy_alerts_sent (
                bid_id   INTEGER PRIMARY KEY,
                phone    TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                sent_ok  BOOLEAN NOT NULL DEFAULT FALSE,
                last_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        conn.commit()

        cur.execute("""
            SELECT b.id AS bid_id, b.year, b.make, b.model, b.trim,
                   b.mileage, b.vin,
                   m.contact_name, m.contact_phone
              FROM bids b
              LEFT JOIN dealerprice_members m
                ON right(regexp_replace(coalesce(m.contact_phone,''),'[^0-9]','','g'),10)
                 = right(regexp_replace(coalesce(b.phone,''),'[^0-9]','','g'),10)
               AND coalesce(m.contact_phone,'') <> ''
              LEFT JOIN autobuy_alerts_sent s ON s.bid_id = b.id
             WHERE (b.source_supplier_id = %s
                    OR b.source_supplier_name ILIKE '%%autobuy%%')
               AND b.created_at > NOW() - INTERVAL '{hrs} hours'
               AND COALESCE(s.sent_ok, FALSE) = FALSE
               AND COALESCE(s.attempts, 0) < {max_att}
             ORDER BY b.created_at
             LIMIT {batch}
        """.format(hrs=LOOKBACK_HOURS, max_att=MAX_ATTEMPTS, batch=BATCH),
            (AUTOBUY_SUPPLIER_ID,))
        rows = cur.fetchall()
        if not rows:
            return

        for r in rows:
            bid_id = r['bid_id']
            try:
                ok = bool(app.send_sms(JOE, compose(r)))
                cur.execute("""
                    INSERT INTO autobuy_alerts_sent
                        (bid_id, phone, attempts, sent_ok, last_at)
                    VALUES (%s, %s, 1, %s, now())
                    ON CONFLICT (bid_id) DO UPDATE
                       SET attempts = autobuy_alerts_sent.attempts + 1,
                           sent_ok  = EXCLUDED.sent_ok,
                           last_at  = now()""",
                            (bid_id, JOE, ok))
                conn.commit()
                print('[%s] bid=%s sent=%s' % (_ts(), bid_id, ok), flush=True)
                if not ok:
                    print('[%s] bid=%s SEND RETURNED FALSE -- check bot_mute '
                          'on %s, STOP status, and Twilio creds'
                          % (_ts(), bid_id, JOE), flush=True)
            except Exception:
                conn.rollback()
                print('[%s] bid=%s FAILED\n%s'
                      % (_ts(), bid_id, traceback.format_exc()), flush=True)


def preview(to_phone, bid_id):
    """Compose a real alert with THIS module's compose() and send it to an
    arbitrary number without touching the ledger. Used to eyeball wording on a
    phone before wiring the cron -- the point is that it exercises the exact
    production code path, not a copy that can drift."""
    with app.get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT b.id AS bid_id, b.year, b.make, b.model, b.trim,
                   b.mileage, b.vin, m.contact_name, m.contact_phone
              FROM bids b
              LEFT JOIN dealerprice_members m
                ON right(regexp_replace(coalesce(m.contact_phone,''),'[^0-9]','','g'),10)
                 = right(regexp_replace(coalesce(b.phone,''),'[^0-9]','','g'),10)
               AND coalesce(m.contact_phone,'') <> ''
             WHERE b.id = %s""", (bid_id,))
        r = cur.fetchone()
    if not r:
        print('no such bid %s' % bid_id)
        return
    body = compose(r)
    print('--- MESSAGE (%d chars) ---\n%s\n--- to %s ---' % (len(body), body, to_phone))
    print('send_sms ->', app.send_sms(to_phone, body))


if __name__ == '__main__':
    if '--preview-to' in sys.argv:
        _to = sys.argv[sys.argv.index('--preview-to') + 1]
        _bid = int(sys.argv[sys.argv.index('--bid') + 1])
        preview(_to, _bid)
    else:
        main()
