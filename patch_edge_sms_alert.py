"""patch_edge_sms_alert.py — send the Edge weekly-pull failure alert by SMS.

Runs ON C1. Idempotent, backs the file up, aborts if the anchor isn't unique.
Marker: EDGE_PULL_SMS_ALERT_2026_09_03

Why: the job currently alerts over Telegram only. A missed week is PERMANENTLY
unrecoverable (EDGE deletes at 6 weeks), and the operator's own history has a
home-LXC standby sitting dead for 18 hours behind a Telegram-only alert —
"an alert channel he doesn't read is not monitoring".

Two things this deliberately does NOT do:
  * It does not read TWILIO_* from os.environ. recon_home_auto.py carries the
    scar: "Cron does NOT inherit the systemd unit's Environment= and send_sms()
    returns ... silently." Credentials are read from /etc/default/expwholesale-mcp.
  * It does not replace the Telegram ping. Both fire; SMS is the one that gets read.
"""
import io, os, shutil, sys, time

PATH = '/opt/expwholesale/edge_weekly_pull.py'
MARK = 'EDGE_PULL_SMS_ALERT_2026_09_03'
STAMP = time.strftime('%Y%m%d-%H%M%S')

SMS_FN = '''

def _envfile(path="/etc/default/expwholesale-mcp"):
    """''' + MARK + ''': read creds from the FILE, never os.environ.

    A cron/systemd job does not inherit the unit's Environment=, and Twilio then
    fails silently — which is the exact failure mode this alert exists to catch.
    """
    out = {}
    try:
        for ln in open(path):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def alert_sms(text):
    """Best-effort SMS to the ops phone. Same number as ew_backup_watch.py."""
    try:
        env = _envfile()
        sid = env.get("TWILIO_ACCOUNT_SID")
        tok = env.get("TWILIO_AUTH_TOKEN")
        frm = env.get("TWILIO_PHONE")
        to = os.environ.get("EW_BW_PHONE") or env.get("EW_BW_PHONE") or ALERT_PHONE
        if not (sid and tok and frm and to):
            log.warning("sms alert: missing twilio creds or destination")
            return False
        if not to.startswith("+"):
            to = "+1" + to
        from twilio.rest import Client
        Client(sid, tok).messages.create(to=to, from_=frm, body=text[:1500])
        log.info("sms alert sent to %s", to[:-4] + "XXXX")
        return True
    except Exception as e:
        log.warning("sms alert failed: %s", e)
        return False
'''


def main():
    src = io.open(PATH, encoding='utf-8').read()
    if MARK in src:
        print('  already patched')
        return 0

    # find the ops phone already used elsewhere so both alerts land in one place
    bw = io.open('/opt/expwholesale/ops/scripts/ew_backup_watch.py', encoding='utf-8').read()
    ph = None
    for ln in bw.split('\n'):
        if ln.startswith('ALERT_PHONE'):
            ph = ln.split("'")[-2] if "'" in ln else None
            break
    if not ph:
        print('  COULD NOT FIND ALERT_PHONE in ew_backup_watch.py — ABORT')
        return 1

    anchor = 'def alert(text: str) -> None:'
    if src.count(anchor) != 1:
        print('  anchor not unique (%d) — ABORT' % src.count(anchor))
        return 1

    shutil.copy2(PATH, PATH + '.bak.' + STAMP + '-presms')

    # constant + helper, inserted just above alert()
    src = src.replace(
        anchor,
        'ALERT_PHONE = "%s"  # %s — same ops number as ew_backup_watch.py\n\n'
        '%s\n\n%s' % (ph, MARK, SMS_FN.strip(), anchor),
        1)

    # make every existing alert() call fan out to SMS as well
    src = src.replace(
        'def alert(text: str) -> None:',
        'def alert(text: str) -> None:', 1)
    i = src.index('def alert(text: str) -> None:')
    j = src.index('\ndef ', i + 10)
    body = src[i:j]
    if 'alert_sms(' not in body:
        body = body.rstrip() + '\n    alert_sms(text)   # ' + MARK + '\n'
        src = src[:i] + body + src[j:]

    io.open(PATH, 'w', encoding='utf-8').write(src)
    print('  patched — alert() now also sends SMS to %sXXXX' % ph[:6])
    return 0


sys.exit(main())
