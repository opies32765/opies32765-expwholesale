"""dealer_submit_alert.py — DEALER_SUBMIT_ALERT_2026_09_02

Text the assigned rep when THEIR dealer submits a car for a bid.

Operator ask 2026-09-02: "joe wants sam to get [a text] the moment that dealer
submits a car... we did it with joe and autobuy already". First subscriber is
Sam (239-595-4021) on Performance Luxury Sport, approved that morning.

WHY THIS IS A SECOND SCRIPT AND NOT AN EDIT TO autobuy_alert.py
    Joe's alert has fired 8 times, every one delivered. It is working
    production plumbing for a partner who relies on it. Generalising it in
    place would put that at risk to add a second subscriber. This is a copy of
    its proven design — standalone cron, claim-then-send ledger, bounded
    retries, send_sms() not Twilio — made table-driven, running beside it.
    autobuy_alert.py is untouched.

MATCHING — the part that would have failed silently
    autobuy_alert.py matches on `b.source_supplier_id = 5005`. That column is
    set on only 15 of the last 1,368 bids, and Performance Luxury Sport's
    member row has lsl_supplier_id NULL, so copying that predicate would have
    matched nothing: Sam gets no texts, no error, nobody notices for weeks.
    So the PRIMARY key here is the submitting PHONE (last 10 digits), which is
    how intake identifies a dealer on every path — DealerPrice link and raw
    SMS alike. supplier_id and a name pattern are optional extra ORs.

Install:
  * * * * * /opt/expwholesale/venv/bin/python /opt/expwholesale/dealer_submit_alert.py >> /var/log/dealer_submit_alert.log 2>&1

Design notes, inherited deliberately from autobuy_alert.py:
  * Standalone cron, NOT a hook in the bid/assessment path — it cannot block
    or delay enrichment (see the EW hard rule) and cannot be starved by a
    stalled assessment.
  * Fires on submission, not on pricing. No valuation in the text: numbers
    stay on the bid page behind a login.
  * app.send_sms() so bot_mute / STOP / magic numbers are still honoured.
  * BOUNDED retry: transient failures retry, permanent ones give up.
  * LOOKBACK_HOURS so switching a subscriber on never backfills a week of
    old cars onto someone's phone.
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
    """TWILIO_CREDS_2026_08_17 — a cron process does NOT inherit the systemd
    unit's Environment=, and send_sms() returns False *silently* when these are
    empty. Without them this job runs every minute, sends nothing, and the rep
    gets nothing forever. Read from the unit file; never hardcode (hardcoded
    creds got autobuy_alert.py rejected by GitHub secret scanning). Abort loudly
    if missing, because the silent-False failure mode is the whole problem.
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
        print('[submit-alert] WARN could not read %s: %s' % (_UNIT, exc),
              flush=True)
    missing = [k for k in _TWILIO_KEYS if not os.environ.get(k)]
    if missing:
        sys.exit('[submit-alert] FATAL missing %s — send_sms() would return '
                 'False silently. Aborting so this is visible instead of a '
                 'stream of no-ops.' % ', '.join(missing))


_load_twilio_env()

import app  # noqa: E402

BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'https://experience-wholesale.net')
LOOKBACK_HOURS = 24
MAX_ATTEMPTS = 3
BATCH = 25

DDL = """
CREATE TABLE IF NOT EXISTS dealer_submit_alerts (
    id           serial PRIMARY KEY,
    label        text NOT NULL,
    member_id    integer,
    supplier_id  integer,
    name_ilike   text,
    match_phone  text,
    notify_phone text NOT NULL,
    notify_name  text,
    active       boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dealer_submit_alerts_sent (
    bid_id   integer NOT NULL,
    alert_id integer NOT NULL,
    phone    text,
    attempts integer NOT NULL DEFAULT 0,
    sent_ok  boolean NOT NULL DEFAULT false,
    last_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bid_id, alert_id)
);
"""


def _ts():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _fmt_phone(p):
    d = ''.join(c for c in (p or '') if c.isdigit())[-10:]
    return '%s-%s-%s' % (d[:3], d[3:6], d[6:]) if len(d) == 10 else (p or '')


def compose(r, label):
    """LINK_NOT_LAST_2026_08_17: the URL must NOT be the final line. A trailing
    URL makes the handset render a link-preview card as a SECOND bubble. The
    operator verified that on a real phone — keep plain text after the link."""
    bits = [str(r['year'] or '').strip(), (r['make'] or '').strip().title(),
            (r['model'] or '').strip(), (r['trim'] or '').strip()]
    car = ' '.join(b for b in bits if b) or (r['vin'] or '')
    if r.get('mileage'):
        car += ' - %s mi' % format(int(r['mileage']), ',')

    who = (r.get('contact_name') or '').strip().split(' ')[0].title()
    contact = ' '.join(x for x in (who, _fmt_phone(r.get('contact_phone'))) if x)

    body = '%s just submitted Bid #%s\n%s/bid/%s\n%s' % (
        label, r['bid_id'], BASE_URL, r['bid_id'], car)
    if contact.strip():
        body += '\n' + contact
    return body


PICK = """
    SELECT b.id AS bid_id, b.year, b.make, b.model, b.trim, b.mileage, b.vin,
           m.contact_name, m.contact_phone
      FROM bids b
      LEFT JOIN dealerprice_members m
        ON right(regexp_replace(coalesce(m.contact_phone,''),'[^0-9]','','g'),10)
         = right(regexp_replace(coalesce(b.phone,''),'[^0-9]','','g'),10)
       AND coalesce(m.contact_phone,'') <> ''
      LEFT JOIN dealer_submit_alerts_sent s
        ON s.bid_id = b.id AND s.alert_id = %(alert_id)s
     WHERE b.created_at > NOW() - INTERVAL '%(hrs)s hours'
       AND COALESCE(s.sent_ok, FALSE) = FALSE
       AND COALESCE(s.attempts, 0) < %(max_att)s
       AND (
             -- the reliable one: how intake identifies a dealer on EVERY path
             (%(match_phone)s <> '' AND
              right(regexp_replace(coalesce(b.phone,''),'[^0-9]','','g'),10)
                = right(%(match_phone)s,10))
             -- rarely populated (15 of the last 1,368 bids), kept as a bonus
          OR (%(supplier_id)s IS NOT NULL AND b.source_supplier_id = %(supplier_id)s)
          OR (%(name_ilike)s <> '' AND b.source_supplier_name ILIKE %(name_ilike)s)
           )
     ORDER BY b.created_at
     LIMIT %(batch)s
"""


def main(dry=False):
    with app.get_db() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()

        cur.execute("SELECT * FROM dealer_submit_alerts WHERE active ORDER BY id")
        alerts = cur.fetchall()
        if not alerts:
            return

        for a in alerts:
            digits = ''.join(c for c in (a['match_phone'] or '') if c.isdigit())
            cur.execute(PICK, {
                'alert_id': a['id'], 'hrs': LOOKBACK_HOURS,
                'max_att': MAX_ATTEMPTS, 'batch': BATCH,
                'match_phone': digits,
                'supplier_id': a['supplier_id'],
                'name_ilike': a['name_ilike'] or '',
            })
            rows = cur.fetchall()
            for r in rows:
                body = compose(r, a['label'])
                if dry:
                    print('[DRY] alert=%s -> %s\n%s\n(%d chars)\n'
                          % (a['id'], a['notify_phone'], body, len(body)),
                          flush=True)
                    continue
                try:
                    ok = bool(app.send_sms(a['notify_phone'], body))
                    cur.execute("""
                        INSERT INTO dealer_submit_alerts_sent
                            (bid_id, alert_id, phone, attempts, sent_ok, last_at)
                        VALUES (%s,%s,%s,1,%s,now())
                        ON CONFLICT (bid_id, alert_id) DO UPDATE
                           SET attempts = dealer_submit_alerts_sent.attempts + 1,
                               sent_ok  = EXCLUDED.sent_ok,
                               last_at  = now()""",
                                (r['bid_id'], a['id'], a['notify_phone'], ok))
                    conn.commit()
                    print('[%s] alert=%s bid=%s sent=%s'
                          % (_ts(), a['id'], r['bid_id'], ok), flush=True)
                    if not ok:
                        print('[%s] alert=%s bid=%s SEND RETURNED FALSE — check '
                              'bot_mute on %s, STOP status, Twilio creds'
                              % (_ts(), a['id'], r['bid_id'], a['notify_phone']),
                              flush=True)
                except Exception:
                    conn.rollback()
                    print('[%s] alert=%s bid=%s FAILED\n%s'
                          % (_ts(), a['id'], r['bid_id'],
                             traceback.format_exc()), flush=True)


def preview(to_phone, bid_id, label):
    """Compose with THIS module's compose() and send to an arbitrary number
    without touching the ledger — exercises the production code path rather
    than a copy that can drift."""
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
    body = compose(r, label)
    print('--- MESSAGE (%d chars) ---\n%s\n--- to %s ---'
          % (len(body), body, to_phone))
    if to_phone != 'NOSEND':
        print('send_sms ->', app.send_sms(to_phone, body))


if __name__ == '__main__':
    if '--preview-to' in sys.argv:
        _to = sys.argv[sys.argv.index('--preview-to') + 1]
        _bid = int(sys.argv[sys.argv.index('--bid') + 1])
        _lab = (sys.argv[sys.argv.index('--label') + 1]
                if '--label' in sys.argv else 'Dealer')
        preview(_to, _bid, _lab)
    else:
        main(dry='--dry' in sys.argv)
