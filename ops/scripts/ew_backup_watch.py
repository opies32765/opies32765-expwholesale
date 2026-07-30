#!/usr/bin/env python3
"""EW BACKUP WATCHDOG (EW_BACKUP_WATCH_2026_07_30)

Alerts when EW's backups stop happening — or start happening badly.

Why this exists: the off-site snapshot failed silently for two days
(2026-07-28/29). pg_dump hit `permission denied for sequence
track_geofence_id_seq`, the script exited non-zero and logged it, and nobody was
watching the log. Two days of off-site DB backups simply did not exist. A backup
you are not verifying is not a backup.

Checks:
  1. AGE  — newest off-site snapshot on C3. Runs 06:00 and 18:00, so anything
            older than 18h means a run was missed or failed.
  2. SIZE — snapshots run ~740MB. A tiny file means the tar or the dump inside
            it bailed. The retired DO droplet was silently writing 255KB stubs
            for a while; size is the only thing that catches that.
  3. AGE  — newest local nightly pg_dump (03:00 daily, ~640MB).
  4. SIZE — same reasoning, locally.
  5. LOG  — an explicit ERROR in the snapshot log since the last success.

Alert discipline matches dp_watch: FAIL_STREAK consecutive bad runs before
alerting, one alert per incident, explicit RECOVERED message.
"""
import json
import os
import re
import subprocess
import sys
import time

C3_HOST = os.environ.get('EW_BW_C3', 'root@147.93.176.207')
C3_DIR = os.environ.get('EW_BW_C3_DIR', '/var/backups/ew_snapshots')
LOCAL_DIR = os.environ.get('EW_BW_LOCAL_DIR', '/var/backups/expwholesale')
SNAP_LOG = os.environ.get('EW_BW_LOG', '/var/log/ew_remote_snapshot.log')
STATE_FILE = os.environ.get('EW_BW_STATE', '/var/lib/ew_backup_watch.json')

ALERT_EMAIL = os.environ.get('EW_BW_EMAIL', 'oscar@orlandoaisolutions.com')
ALERT_PHONE = os.environ.get('EW_BW_PHONE', '4074309675')

OFFSITE_MAX_AGE_H = float(os.environ.get('EW_BW_OFFSITE_AGE', '18'))
OFFSITE_MIN_MB = float(os.environ.get('EW_BW_OFFSITE_MIN_MB', '200'))
LOCAL_MAX_AGE_H = float(os.environ.get('EW_BW_LOCAL_AGE', '30'))
LOCAL_MIN_MB = float(os.environ.get('EW_BW_LOCAL_MIN_MB', '100'))
FAIL_STREAK = int(os.environ.get('EW_BW_STREAK', '2'))


def newest_offsite():
    """(age_hours, size_mb, name) for the newest C3 snapshot, or None."""
    try:
        r = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', C3_HOST,
             "ls -t %s/*.tar 2>/dev/null | head -1 | xargs -r stat -c '%%Y %%s %%n'" % C3_DIR],
            capture_output=True, text=True, timeout=45)
        out = (r.stdout or '').strip()
        if not out:
            return None
        mtime, size, name = out.split(None, 2)
        return ((time.time() - float(mtime)) / 3600.0, float(size) / 1048576.0, name)
    except Exception as e:
        sys.stderr.write('offsite check failed: %s\n' % e)
        return None


def newest_local():
    """(age_hours, size_mb, name) for the newest local pg dump, or None."""
    try:
        best = None
        for f in os.listdir(LOCAL_DIR):
            if not f.startswith('expwholesale_') or not f.endswith('.dump'):
                continue
            p = os.path.join(LOCAL_DIR, f)
            st = os.stat(p)
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, st.st_size, f)
        if not best:
            return None
        return ((time.time() - best[0]) / 3600.0, best[1] / 1048576.0, best[2])
    except Exception as e:
        sys.stderr.write('local check failed: %s\n' % e)
        return None


def log_errors_since_success():
    """True if the snapshot log's last outcome was a failure."""
    try:
        with open(SNAP_LOG, errors='replace') as f:
            tail = f.readlines()[-400:]
        last_ok = last_err = -1
        for i, line in enumerate(tail):
            if re.search(r'complete \|', line):
                last_ok = i
            if 'ERROR' in line:
                last_err = i
        return last_err > last_ok
    except Exception:
        return False


def check():
    bad = []

    off = newest_offsite()
    if off is None:
        bad.append('cannot read off-site snapshots on C3 (host down or SSH broken)')
    else:
        age, mb, name = off
        if age > OFFSITE_MAX_AGE_H:
            bad.append('off-site snapshot is %.1fh old (expected every 12h) — %s' % (age, name))
        if mb < OFFSITE_MIN_MB:
            bad.append('off-site snapshot is only %.0fMB — looks like a stub, not a backup (%s)'
                       % (mb, name))

    loc = newest_local()
    if loc is None:
        bad.append('no local pg_dump found in %s' % LOCAL_DIR)
    else:
        age, mb, name = loc
        if age > LOCAL_MAX_AGE_H:
            bad.append('local pg_dump is %.1fh old (expected nightly) — %s' % (age, name))
        if mb < LOCAL_MIN_MB:
            bad.append('local pg_dump is only %.0fMB — dump likely failed (%s)' % (mb, name))

    if log_errors_since_success():
        bad.append('snapshot log shows an ERROR with no success after it '
                   '(check %s)' % SNAP_LOG)

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
            'from': 'EW Backup Watchdog <info@experience-wholesale.net>',
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
            send_sms('EW BACKUP PROBLEM (%s)\n%s'
                     % (stamp, '\n'.join('- ' + p for p in problems)))
            send_email(
                'EW backup problem',
                '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;color:#0f172a">'
                '<p style="margin:0 0 12px"><b style="color:#b42318">EW backups need attention.</b></p>'
                '<ul>%s</ul>'
                '<p style="margin:12px 0 0">Off-site runs 06:00 and 18:00 to C3; local pg_dump runs 03:00.</p>'
                '<p style="margin:8px 0 0;font-size:12px;color:#94a3b8">Checked %s from C1. '
                'You will get a follow-up when it recovers.</p></div>'
                % (''.join('<li>%s</li>' % p for p in problems), stamp))
            st['alerted'] = True
            print('ALERTED: %s' % problems)
        else:
            print('problem (streak %d/%d): %s' % (st['streak'], FAIL_STREAK, problems))
    else:
        if st.get('alerted'):
            send_sms('EW BACKUP RECOVERED (%s) - off-site and local backups are current.' % stamp)
            send_email('EW backups recovered',
                       '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px">'
                       '<p><b style="color:#15803d">EW backups are current again.</b></p>'
                       '<p style="font-size:12px;color:#94a3b8">Recovered %s.</p></div>' % stamp)
            print('RECOVERED')
        st['streak'] = 0
        st['alerted'] = False
        print('ok')

    save_state(st)


if __name__ == '__main__':
    main()
