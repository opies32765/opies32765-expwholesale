#!/usr/bin/env python3
"""dp_outreach_send.py — DealerPrice launch outreach sender.

DP_OUTREACH_SENDER_2026_08_04.

The tracking layer for this campaign was fully built (tokens, open pixel,
signed click-through, bounce/complaint webhook, unsubscribe + suppression,
dashboard) but NOTHING ever created a dp_outreach_email row -- every reference
in dealerprice_network.py is a SELECT or an UPDATE. There was no sender, no
send route, and no send script anywhere on the box. This is that sender.

════════════════════════════════════════════════════════════════════════════
SAFETY -- read before changing anything
════════════════════════════════════════════════════════════════════════════
There are 752 live dealer addresses one table away from this code. Three
independent guards stand between this script and an accidental mass send:

  1. ALLOWLIST. Every recipient is checked against an explicit hard-coded set.
     An address not in it is REFUSED, loudly. The script never reads
     dp_outreach_targets at all -- there is no code path from here to the
     dealer list.
  2. --send required. Without it this is a dry run: it renders, prints, and
     writes nothing.
  3. MAX_RECIPIENTS. A hard ceiling; exceeding it aborts before any send.

Do not "temporarily" widen these to do the real campaign. The real send needs
its own reviewed path with throttling, suppression checks and a resume cursor.
════════════════════════════════════════════════════════════════════════════

Deliverability notes:
  * Sends as DP_EMAIL_FROM (info@experience-wholesale.net). dealerprice.net is
    NOT a verified Resend sender and is rejected -- see
    DP_EMAIL_IDENTITY_2026_07_28.
  * Logo is served from experience-wholesale.net, the Resend-verified host, so
    it loads without auth. Same asset the approved/welcome email uses.
  * List-Unsubscribe + One-Click are set. Mailbox providers materially favour
    senders that honour it, and this is a cold campaign.
  * Table-based layout + inline CSS: Outlook and Gmail both mangle modern CSS.
"""
import os
import sys
import argparse
import secrets

sys.path.insert(0, '/opt/expwholesale')
os.chdir('/opt/expwholesale')

import dealerprice_network as DPN

# ── guard 1: the only addresses this script may ever contact ────────────────
ALLOWLIST = {
    'oscar@orlandoaisolutions.net',
    'opies32765@gmail.com',
    'oscar@experience-wholesale.com',
}
# guard 3
MAX_RECIPIENTS = 5

CAMPAIGN = 'dp_launch_2026_08'
CTA_URL = 'https://dealerprice.net'
LOGO = 'https://experience-wholesale.net/static/ew-logo-email.png'

SUBJECT = 'A faster way to get a bid on a car'

# Names deliberately EMPTY: the test must render exactly what the 736 will get.
RECIPIENTS = [
    ('oscar@orlandoaisolutions.net',   ''),
    ('opies32765@gmail.com',           ''),
    ('oscar@experience-wholesale.com', ''),
]


def build_html(first, token):
    """The launch email — deliberately short.

    A cold dealer needs two things: that this is fast, and how they would do
    it. Everything else has been cut, including the value props (operator,
    2026-08-04) — the email is now purely mechanical: what DealerPrice is, the
    two ways to send a car, and how to get set up. One CTA, asserted at patch
    time so a second button cannot creep back in.
    """
    # DP_GREETING_NEUTRAL_2026_08_04: no name -> "Hi there,". Uniform across
    # the whole list today; see the module docstring for why.
    greeting = ('Hi %s,' % first) if (first or '').strip() else 'Hi there,'
    track = DPN.DP_TRACK_BASE.rstrip('/')
    cta = DPN._dpt_wrap(token, CTA_URL)
    unsub = '%s/e/u/%s' % (track, token)
    pixel = '%s/e/o/%s.gif' % (track, token)

    return """\
<!--[if mso]><style>body,table,td{font-family:Arial,sans-serif !important}</style><![endif]-->
<div style="background:#f4f5f7;padding:28px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb">

  <tr><td align="center" style="padding:30px 30px 20px 30px;border-bottom:1px solid #eef0f3">
    <img src="%(logo)s" width="240" alt="Experience Wholesale"
         style="display:block;width:240px;max-width:70%%;height:auto;border:0">
  </td></tr>

  <tr><td style="padding:30px 34px 0 34px">
    <p style="margin:0 0 14px 0;font-size:15.5px;line-height:1.62;color:#0f172a">%(greeting)s</p>
    <p style="margin:0 0 18px 0;font-size:15px;line-height:1.62;color:#475569">
      We&rsquo;ve spent the last few months building a more robust system for bidding
      cars &mdash; and a good deal more efficient than what we had before.
      It went live this week. We call it <b style="color:#0f172a">DealerPrice</b>.
    </p>
    <p style="margin:0 0 12px 0;font-size:15.5px;line-height:1.6;color:#0f172a;font-weight:700">
      Sending us a car takes about a minute. Two ways:
    </p>
  </td></tr>

  <tr><td style="padding:0 34px 0 34px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-left:4px solid #b91c2c;border-radius:10px">
      <tr><td style="padding:16px 19px">
        <div style="font-size:12px;font-weight:800;color:#b91c2c;letter-spacing:.5px">TEXT US</div>
        <div style="font-size:21px;font-weight:800;color:#0f172a;margin:5px 0 8px 0">%(sms)s</div>
        <p style="margin:0;font-size:14.5px;line-height:1.6;color:#475569">
          Text the <b>VIN and mileage</b>. Or just <b>photograph the VIN plate and the
          odometer</b> &mdash; we&rsquo;ll read them off the picture. Add photos of the
          car if you have them.
        </p>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:11px 34px 0 34px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-left:4px solid #1e3a8a;border-radius:10px">
      <tr><td style="padding:16px 19px">
        <div style="font-size:12px;font-weight:800;color:#1e3a8a;letter-spacing:.5px">YOUR OWN PRIVATE LINK</div>
        <p style="margin:6px 0 0 0;font-size:14.5px;line-height:1.6;color:#475569">
          Your own submission page &mdash; no time limit, take as long as you like.
          Bookmark it; it&rsquo;s yours and it doesn&rsquo;t expire.
        </p>
      </td></tr>
    </table>
  </td></tr>

  <tr><td align="center" style="padding:28px 34px 6px 34px">
    <p style="margin:0 0 13px 0;font-size:16px;line-height:1.5;color:#0f172a;font-weight:700">
      Ready to get set up?
    </p>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
      <td align="center" style="background:#b91c2c;border-radius:8px">
        <a href="%(cta)s" style="display:inline-block;padding:14px 32px;font-size:15px;font-weight:700;color:#ffffff;text-decoration:none">Get started &rarr;</a>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:22px 34px 30px 34px">
    <p style="margin:0;font-size:14px;line-height:1.62;color:#475569">
      Questions on a car or a number? <b>Just reply to this email</b> &mdash; it goes
      straight to our buying desk.
    </p>
    <p style="margin:15px 0 0 0;font-size:14px;color:#0f172a"><b>Experience Wholesale</b></p>
  </td></tr>

  <tr><td style="padding:15px 34px 22px 34px;background:#f8fafc;border-top:1px solid #eef0f3">
    <p style="margin:0;font-size:11.5px;line-height:1.55;color:#94a3b8">
      You received this because your dealership has done business with Experience
      Wholesale. <a href="%(unsub)s" style="color:#94a3b8;text-decoration:underline">Unsubscribe</a>
      and we won&rsquo;t email you about this again.
    </p>
  </td></tr>

</table></div><img src="%(pixel)s" width="1" height="1" alt="" style="display:block;border:0">""" % {
        'logo': LOGO, 'greeting': greeting, 'cta': cta, 'unsub': unsub,
        'pixel': pixel, 'sms': DPN.EW_SMS_NUMBER}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--send', action='store_true',
                    help='actually send (default is a dry run)')
    ap.add_argument('--dump', metavar='PATH',
                    help='write the rendered HTML of the first recipient here')
    args = ap.parse_args()

    # ── guard 1 ────────────────────────────────────────────────────────────
    bad = [e for e, _ in RECIPIENTS if e.lower() not in ALLOWLIST]
    if bad:
        print('REFUSED — not in the allowlist: %s' % ', '.join(bad))
        return 2
    # ── guard 3 ────────────────────────────────────────────────────────────
    if len(RECIPIENTS) > MAX_RECIPIENTS:
        print('REFUSED — %d recipients exceeds MAX_RECIPIENTS=%d'
              % (len(RECIPIENTS), MAX_RECIPIENTS))
        return 2

    key = os.environ.get('RESEND_API_KEY', '')
    if args.send and not key:
        print('REFUSED — RESEND_API_KEY not set')
        return 2

    print('campaign : %s' % CAMPAIGN)
    print('from     : %s' % DPN.DP_EMAIL_FROM)
    print('reply-to : %s' % ','.join(DPN._dp_reply_to()))
    print('subject  : %s' % SUBJECT)
    print('mode     : %s' % ('LIVE SEND' if args.send else 'dry run'))
    print('-' * 72)

    db = DPN._db()
    cur = db.cursor()
    rc = 0
    for i, (email, first) in enumerate(RECIPIENTS):
        token = secrets.token_urlsafe(16)
        html = build_html(first, token)

        if args.dump and i == 0:
            with open(args.dump, 'w', encoding='utf-8') as f:
                f.write(html)
            print('  [dumped first render -> %s]' % args.dump)

        if not args.send:
            print('DRY  %-34s first=%-6s token=%s  html=%db'
                  % (email, first, token[:10] + '…', len(html)))
            continue

        try:
            cur.execute("""INSERT INTO dp_outreach_email
                               (email, token, subject, campaign, status, created_at)
                           VALUES (%s, %s, %s, %s, 'queued', now())
                        RETURNING id""",
                        (email, token, SUBJECT, CAMPAIGN))
            eid = cur.fetchone()['id']
            db.commit()
        except Exception as e:
            db.rollback()
            print('FAIL %-34s db insert: %s' % (email, e))
            rc = 1
            continue

        try:
            import resend
            resend.api_key = key
            payload = {
                'from': DPN.DP_EMAIL_FROM,
                'to': [email],
                'subject': SUBJECT,
                'html': html,
                'headers': {
                    'List-Unsubscribe': '<%s/e/u/%s>' % (
                        DPN.DP_TRACK_BASE.rstrip('/'), token),
                    'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
                },
            }
            rt = DPN._dp_reply_to()
            if rt:
                payload['reply_to'] = rt
            resp = resend.Emails.send(payload)
            pid = (resp or {}).get('id')
            cur.execute("""UPDATE dp_outreach_email
                              SET status='sent', sent_at=now(), provider_id=%s
                            WHERE id=%s""", (pid, eid))
            db.commit()
            print('SENT %-34s first=%-6s email_id=%-4s provider=%s'
                  % (email, first, eid, pid))
        except Exception as e:
            db.rollback()
            cur.execute("UPDATE dp_outreach_email SET status='failed', error=%s "
                        "WHERE id=%s", (str(e)[:400], eid))
            db.commit()
            print('FAIL %-34s %s: %s' % (email, type(e).__name__, e))
            rc = 1

    db.close()
    return rc


if __name__ == '__main__':
    sys.exit(main())
