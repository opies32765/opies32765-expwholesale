#!/usr/bin/env python3
"""dp_scan_watch.py — DP_SCAN_WATCH_2026_07_29

Operator ask: when a dealer that DealerPrice auto-onboarded has a scanning
problem, email me — don't let it rot silently.

Deliberately a WATCHDOG rather than a hook inside dealer_scanner.py: it catches
the failure mode the scanner itself cannot report, which is a scan that never
ran at all (never scheduled, wrong platform, site unreachable from the start).
A hook only fires when a scan actually executes.

Flags a DealerPrice-onboarded dealer when:
  * last_scan_status is anything other than 'ok'
  * it has NEVER been scanned and was registered more than GRACE_H ago
  * its last successful scan is older than STALE_H

Emails ONE digest, and only when there is something to say — a quiet fleet
sends no mail. Re-alerts are throttled per dealer via a small state file so a
persistent problem nags daily rather than every run.

Run: /opt/expwholesale/venv/bin/python /opt/expwholesale/ops/scripts/dp_scan_watch.py
"""
import os
import json
import time
import psycopg2
import psycopg2.extras
import urllib.request

DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
RESEND_KEY = os.environ.get('RESEND_API_KEY', '')
ALERT_TO = [a.strip() for a in os.environ.get(
    'DP_SCAN_ALERT_TO', 'opies32765@gmail.com').split(',') if a.strip()]
FROM = 'Experience Wholesale <info@experience-wholesale.net>'

GRACE_H = int(os.environ.get('DP_SCAN_GRACE_H', '12'))    # new dealer settle time
STALE_H = int(os.environ.get('DP_SCAN_STALE_H', '48'))    # no good scan since
RENOTIFY_H = int(os.environ.get('DP_SCAN_RENOTIFY_H', '24'))
STATE = os.environ.get('DP_SCAN_WATCH_STATE', '/opt/expwholesale/ops/.dp_scan_watch.json')

SQL = """
SELECT d.id, d.name, d.url, d.platform, d.active,
       d.last_scan_at, d.last_scan_status,
       d.created_at,
       a.id  AS application_id,
       a.dealership_name,
       a.contact_name, a.contact_phone, a.contact_email
  FROM dealers d
  JOIN dealer_applications a ON a.scanner_dealer_id = d.id
 WHERE d.active
"""


def _load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, 'w') as f:
            json.dump(st, f)
    except Exception as e:
        print('[dp-scan-watch] state save failed: %s' % e, flush=True)


def _hours_since(ts, now):
    return None if not ts else (now - ts).total_seconds() / 3600.0


def main():
    conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()
    cur.execute(SQL)
    rows = cur.fetchall()
    cur.execute("SELECT now() AS now")
    now = cur.fetchone()['now']
    conn.close()

    problems = []
    for d in rows:
        age_h = _hours_since(d['created_at'], now)
        scan_h = _hours_since(d['last_scan_at'], now)
        status = (d['last_scan_status'] or '').lower()

        if d['last_scan_at'] is None:
            if age_h is not None and age_h > GRACE_H:
                problems.append((d, 'never scanned (registered %.0fh ago)' % age_h))
            continue
        if status and status != 'ok':
            problems.append((d, 'last scan status = %s' % d['last_scan_status']))
        elif scan_h is not None and scan_h > STALE_H:
            problems.append((d, 'no scan for %.0fh' % scan_h))

    if not problems:
        print('[dp-scan-watch] %d DealerPrice dealers, all healthy' % len(rows), flush=True)
        return

    # throttle: only re-nag about the same dealer every RENOTIFY_H
    st = _load_state()
    nowts = time.time()
    fresh = [(d, why) for d, why in problems
             if nowts - float(st.get(str(d['id']), 0)) > RENOTIFY_H * 3600]
    if not fresh:
        print('[dp-scan-watch] %d problem(s), all recently alerted - staying quiet'
              % len(problems), flush=True)
        return

    lines = []
    for d, why in fresh:
        lines.append(
            '<tr>'
            '<td style="padding:6px 10px;border-bottom:1px solid #eee"><b>%s</b><br>'
            '<span style="color:#666;font-size:12px">%s</span></td>'
            '<td style="padding:6px 10px;border-bottom:1px solid #eee;color:#b42318">%s</td>'
            '<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:12px">'
            '%s<br>%s</td>'
            '<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:12px">'
            '<a href="https://experience-wholesale.net/network/application/%s">packet</a></td>'
            '</tr>' % (
                d.get('dealership_name') or d.get('name') or '?',
                d.get('url') or '',
                why,
                d.get('contact_name') or '',
                d.get('contact_phone') or d.get('contact_email') or '',
                d.get('application_id')))

    html = (
        '<p><b>%d DealerPrice-onboarded dealer(s) have a scanning problem.</b></p>'
        '<p style="color:#555;font-size:13px">These were auto-registered when their '
        'application was approved, so a failure here means we are not seeing their '
        'inventory.</p>'
        '<table style="border-collapse:collapse;font-family:sans-serif;font-size:13px">'
        '<tr style="text-align:left;color:#666">'
        '<th style="padding:6px 10px">Dealer</th><th style="padding:6px 10px">Problem</th>'
        '<th style="padding:6px 10px">Contact</th><th style="padding:6px 10px"></th></tr>'
        '%s</table>'
        '<p style="color:#888;font-size:12px">Re-alerts are throttled to once every '
        '%dh per dealer.</p>' % (len(fresh), ''.join(lines), RENOTIFY_H))

    if not RESEND_KEY:
        print('[dp-scan-watch] NO RESEND KEY - would have emailed %d problem(s)'
              % len(fresh), flush=True)
        return

    req = urllib.request.Request(
        'https://api.resend.com/emails',
        data=json.dumps({
            'from': FROM,
            'to': ALERT_TO,
            'subject': '[EW] %d DealerPrice dealer scan issue(s)' % len(fresh),
            'html': html,
        }).encode(),
        headers={'Authorization': 'Bearer ' + RESEND_KEY,
                 'Content-Type': 'application/json'},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        for d, _why in fresh:
            st[str(d['id'])] = nowts
        _save_state(st)
        print('[dp-scan-watch] emailed %d problem(s) to %s'
              % (len(fresh), ','.join(ALERT_TO)), flush=True)
    except Exception as e:
        print('[dp-scan-watch] email FAILED: %s' % e, flush=True)


if __name__ == '__main__':
    main()
