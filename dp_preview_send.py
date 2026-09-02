#!/usr/bin/env python3
"""dp_preview_send.py — send ONE campaign's real email to the operator.

DP_PREVIEW_HARNESS_2026_09_02.

Both senders (dp_outreach_send.py, dp_followup_send.py) carry their own
hard-coded RECIPIENTS list. Editing that list every time the operator wants a
different preview means editing the file that IS the record of what a campaign
sent. This harness imports `build_html` instead and leaves both alone.

  python3 dp_preview_send.py --copy launch   --to oscar@experience-wholesale.com
  python3 dp_preview_send.py --copy followup --to opies32765@gmail.com --send

Guards, same shape as the senders it previews:
  1. ALLOWLIST — operator addresses only, checked before anything renders.
     There is no path from here to dp_outreach_targets.
  2. --send required; default is a dry run.
  3. Exactly ONE recipient per invocation.

Preview rows are tagged campaign='dp_preview_2026_09' rather than the real
campaign tag, so a preview can never inflate a campaign's attempted/delivered
counts on the outreach dashboard. They still get a real token, so the pixel
and the click-through work and the CTA can be tested for real.
"""
import os
import sys
import argparse
import secrets

sys.path.insert(0, '/opt/expwholesale')
os.chdir('/opt/expwholesale')

import dealerprice_network as DPN

ALLOWLIST = {
    'oscar@orlandoaisolutions.net',
    'opies32765@gmail.com',
    'oscar@experience-wholesale.com',
    # NOT oscar@experience-wholesale.net — Cloudflare Email Routing MX on the
    # domain but no route for `oscar`; answers 550 5.1.1 "Address does not
    # exist". Having MX is not having a mailbox (tried 2026-09-02).
}

PREVIEW_CAMPAIGN = 'dp_preview_2026_09'


def load(copy):
    if copy == 'launch':
        import dp_outreach_send as M
    elif copy == 'followup':
        import dp_followup_send as M
    else:
        sys.exit('unknown --copy %r' % copy)
    return M.build_html, M.SUBJECT, M.CAMPAIGN


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--copy', required=True, choices=['launch', 'followup'],
                    help='which campaign\'s email to render')
    ap.add_argument('--to', required=True, help='one operator address')
    ap.add_argument('--send', action='store_true')
    ap.add_argument('--dump', metavar='PATH')
    a = ap.parse_args()

    to = a.to.strip().lower()
    if to not in ALLOWLIST:
        print('REFUSED - %s is not in the allowlist' % to)
        return 2

    render, subject, real_campaign = load(a.copy)
    token = secrets.token_urlsafe(16)
    html = render('', token)

    print('copy     : %s  (real campaign tag: %s)' % (a.copy, real_campaign))
    print('subject  : %s' % subject)
    print('from     : %s' % DPN.DP_EMAIL_FROM)
    print('to       : %s' % to)
    print('tagged   : %s  (so it cannot pollute campaign stats)' % PREVIEW_CAMPAIGN)
    print('mode     : %s' % ('LIVE SEND' if a.send else 'dry run'))
    print('-' * 72)

    if a.dump:
        with open(a.dump, 'w', encoding='utf-8') as f:
            f.write(html)
        print('  [dumped -> %s]' % a.dump)

    if not a.send:
        print('DRY  %s  html=%db  token=%s...' % (to, len(html), token[:10]))
        return 0

    key = os.environ.get('RESEND_API_KEY', '')
    if not key:
        print('REFUSED - RESEND_API_KEY not set')
        return 2

    db = DPN._db()
    cur = db.cursor()
    cur.execute("""INSERT INTO dp_outreach_email
                       (email, token, subject, campaign, status, created_at)
                   VALUES (%s,%s,%s,%s,'queued',now()) RETURNING id""",
                (to, token, subject, PREVIEW_CAMPAIGN))
    eid = cur.fetchone()['id']
    db.commit()

    try:
        import resend
        resend.api_key = key
        payload = {
            'from': DPN.DP_EMAIL_FROM, 'to': [to], 'subject': subject,
            'html': html,
            'headers': {
                'List-Unsubscribe': '<%s/e/u/%s>' % (
                    DPN.DP_TRACK_BASE.rstrip('/'), token),
                'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'},
        }
        rt = DPN._dp_reply_to()
        if rt:
            payload['reply_to'] = rt
        resp = resend.Emails.send(payload)
        pid = (resp or {}).get('id')
        cur.execute("UPDATE dp_outreach_email SET status='sent', sent_at=now(), "
                    "provider_id=%s WHERE id=%s", (pid, eid))
        db.commit()
        print('SENT %s  email_id=%s  provider=%s' % (to, eid, pid))
        rc = 0
    except Exception as e:
        db.rollback()
        cur.execute("UPDATE dp_outreach_email SET status='failed', error=%s "
                    "WHERE id=%s", (str(e)[:400], eid))
        db.commit()
        print('FAIL %s  %s: %s' % (to, type(e).__name__, e))
        rc = 1
    db.close()
    return rc


if __name__ == '__main__':
    sys.exit(main())
