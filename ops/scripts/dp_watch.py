#!/usr/bin/env python3
"""DEALERPRICE WATCHDOG (DP_WATCH_2026_07_29)

Alerts oscar@orlandoaisolutions.com by text + email when dealerprice.net is
broken, or when its link back to EW is.

Runs on C1, NOT on the DealerPrice box: a watcher living on the machine it
watches goes down with it. From here we see the site the way a dealer does.

Deliberately a cron, not a daemon — nothing to crash, restart, or babysit, and
a missed run self-heals on the next tick.

Checks, in order of what a dealer would notice:
  1. the site loads
  2. the apply page loads (this is the page that earns money)
  3. /api/stats answers with a real deal count
  4. /api/live-cars answers with cars
  5. the EW->DP sync is fresh — deals_stat.updatedAt is how we detect the
     dp_sync cron dying, which is the "connection to EW" failing quietly:
     the site stays up and simply freezes, which nobody would spot by eye.

Alert discipline: FAIL_STREAK consecutive bad runs before alerting (one blip
is not an outage), one alert per incident, and an explicit RECOVERED message so
nobody is left wondering. State in STATE_FILE.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get('DP_WATCH_BASE', 'https://dealerprice.net')
STATE_FILE = os.environ.get('DP_WATCH_STATE', '/var/lib/dp_watch.json')
ALERT_EMAIL = os.environ.get('DP_WATCH_EMAIL', 'oscar@orlandoaisolutions.com')
ALERT_PHONE = os.environ.get('DP_WATCH_PHONE', '4074309675')
FAIL_STREAK = int(os.environ.get('DP_WATCH_STREAK', '2'))
SYNC_MAX_AGE_MIN = int(os.environ.get('DP_WATCH_SYNC_AGE', '20'))
TIMEOUT = 20


def get(path):
    req = urllib.request.Request(BASE + path, headers={'User-Agent': 'dp-watch/1.0'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.getcode(), r.read().decode('utf-8', 'replace')


def check():
    """Return a list of human-readable problems. Empty list = healthy."""
    bad = []

    for path, label in (('/', 'home page'), ('/apply', 'apply page')):
        try:
            code, _ = get(path)
            if code != 200:
                bad.append('%s returned HTTP %s' % (label, code))
        except Exception as e:
            bad.append('%s unreachable (%s)' % (label, type(e).__name__))

    # stats + the freshness of the EW -> DealerPrice sync
    try:
        code, body = get('/api/stats')
        d = json.loads(body)
        if not d.get('dealsTotal'):
            bad.append('stats API returned no deal count (DP database down?)')
        ua = d.get('updatedAt')
        if ua:
            try:
                from datetime import datetime, timezone
                t = datetime.fromisoformat(ua.replace('Z', '+00:00'))
                age = (datetime.now(timezone.utc) - t).total_seconds() / 60.0
                if age > SYNC_MAX_AGE_MIN:
                    bad.append('EW sync stale: last update %d min ago '
                               '(dp_sync cron on dp-web?)' % age)
            except Exception:
                pass
        else:
            bad.append('stats API has no updatedAt (sync never ran?)')
    except Exception as e:
        bad.append('stats API failed (%s)' % type(e).__name__)

    try:
        code, body = get('/api/live-cars')
        d = json.loads(body)
        if not d.get('count'):
            bad.append('ticker feed is empty')
    except Exception as e:
        bad.append('ticker API failed (%s)' % type(e).__name__)

    return bad


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {'streak': 0, 'alerted': False}


def save_state(s):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(s, f)
    except Exception as e:
        sys.stderr.write('state write failed: %s\n' % e)


def send_sms(body):
    try:
        from twilio.rest import Client
        sid = os.environ.get('TWILIO_ACCOUNT_SID')
        tok = os.environ.get('TWILIO_AUTH_TOKEN')
        frm = os.environ.get('TWILIO_PHONE')
        if not (sid and tok and frm):
            sys.stderr.write('twilio creds missing\n')
            return False
        Client(sid, tok).messages.create(to='+1' + ALERT_PHONE, from_=frm, body=body)
        return True
    except Exception as e:
        sys.stderr.write('sms failed: %s\n' % e)
        return False


def send_email(subject, html):
    try:
        import resend
        key = os.environ.get('RESEND_API_KEY')
        if not key:
            sys.stderr.write('resend key missing\n')
            return False
        resend.api_key = key
        resend.Emails.send({
            'from': 'DealerPrice Watchdog <info@experience-wholesale.net>',
            'to': [ALERT_EMAIL],
            'subject': subject,
            'html': html,
        })
        return True
    except Exception as e:
        sys.stderr.write('email failed: %s\n' % e)
        return False


def main():
    problems = check()
    st = load_state()
    stamp = time.strftime('%b %-d %-I:%M %p')

    if problems:
        st['streak'] = st.get('streak', 0) + 1
        if st['streak'] >= FAIL_STREAK and not st.get('alerted'):
            lines = '\n'.join('- ' + p for p in problems)
            send_sms('DEALERPRICE ALERT (%s)\n%s\n%s' % (stamp, lines, BASE))
            send_email(
                'DealerPrice issue detected',
                '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;color:#0f172a">'
                '<p style="margin:0 0 12px"><b style="color:#b42318">dealerprice.net is reporting problems.</b></p>'
                '<ul>%s</ul>'
                '<p style="margin:12px 0 0;font-size:12px;color:#94a3b8">Checked %s from C1. '
                'You will get a follow-up when it recovers.</p></div>'
                % (''.join('<li>%s</li>' % p for p in problems), stamp))
            st['alerted'] = True
            print('ALERTED: %s' % problems)
        else:
            print('problem (streak %d/%d): %s' % (st['streak'], FAIL_STREAK, problems))
    else:
        if st.get('alerted'):
            send_sms('DEALERPRICE RECOVERED (%s) - all checks passing.' % stamp)
            send_email('DealerPrice recovered',
                       '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px">'
                       '<p><b style="color:#15803d">dealerprice.net is healthy again.</b></p>'
                       '<p style="font-size:12px;color:#94a3b8">Recovered %s.</p></div>' % stamp)
            print('RECOVERED')
        st['streak'] = 0
        st['alerted'] = False
        print('ok')

    save_state(st)


if __name__ == '__main__':
    main()
