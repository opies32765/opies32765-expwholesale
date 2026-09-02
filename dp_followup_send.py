#!/usr/bin/env python3
"""dp_followup_send.py — DealerPrice SECOND touch.

DP_FOLLOWUP_SENDER_2026_09_02.

The launch campaign (`dp_launch_2026_08`, dp_outreach_send.py) went to 718
addresses on 2026-08-05: 588 delivered, 65 opened, 31 clicked, 3 applied.
This is the follow-up to the ones that ACTUALLY LANDED.

Why this is a separate file and not an edit to dp_outreach_send.py:
that module's docstring calls its body "the exact script the operator
reviewed and approved". It is the RECORD of what 718 dealers were sent.
Editing it would rewrite history and make the launch render
unreproducible. New campaign, new module.

════════════════════════════════════════════════════════════════════════════
SAFETY — identical to the launch sender, deliberately
════════════════════════════════════════════════════════════════════════════
  1. ALLOWLIST. This script only ever mails the operator's own three
     addresses. It never reads dp_outreach_targets. The real send goes
     through `dp_campaign.py --followup`, which has the throttle, the
     suppression trigger, the resume-by-construction query and MAX_LIMIT.
  2. --send required. Default is a dry run that renders and writes nothing.
  3. MAX_RECIPIENTS ceiling.
════════════════════════════════════════════════════════════════════════════

Copy decisions (second touch, not a re-send):
  * The ask is SMALLER, not louder. The launch email's one CTA was
    "Apply Now". A dealer who read that and did nothing is not going to
    fill in a licence number this time either. So the primary ask here is
    "text us one car" — zero commitment, no form, no account — and the
    apply button is secondary.
  * NO SPEED CLAIM. The obvious stat — 9.6 min average from submission to
    a price across the 1,270 cars run since 2026-08-05 — measures the AI
    ASSESSMENT landing, which is internal and downstream. A dealer who is
    not on the enrichment allowlist never sees a number from the system at
    all (ENRICHMENT_SMS_DENY_BY_DEFAULT_2026_07_28): they get a receipt and
    a human calls. Those 1,270 bids also came from only 27 distinct phones,
    i.e. mostly our own reps. Promising "a number in 15 minutes" would be a
    promise the operator has not made and the system does not keep. What
    the email says instead is exactly what happens: a text back confirming
    we have it, then Joe, Todd or Gregg with the number.
  * No guilt ("we didn't hear back"), no false scarcity, no discount.
  * Same visual system as the launch email so it reads as the same sender.

Deliverability: unchanged from the launch — DP_EMAIL_FROM
(info@experience-wholesale.net; dealerprice.net is NOT a verified Resend
sender, see DP_EMAIL_IDENTITY_2026_07_28), logo on the verified host,
List-Unsubscribe + One-Click, table layout with inline CSS.
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
    # NOT oscar@experience-wholesale.net — that domain has Cloudflare Email
    # Routing MX records but no route for `oscar`, so it answers 550 5.1.1
    # "Address does not exist". Having MX is not having a mailbox. Tried
    # 2026-09-02, hard bounced, row and auto-suppression cleaned up.
}
# guard 3
MAX_RECIPIENTS = 5

CAMPAIGN = 'dp_followup_2026_09'
CTA_URL = 'https://dealerprice.net'
LOGO = 'https://experience-wholesale.net/static/ew-logo-email.png'

SUBJECT = 'Send us one car — no account, no application'

# Names deliberately EMPTY: the test must render exactly what the list gets.
RECIPIENTS = [
    ('oscar@experience-wholesale.com', ''),
]


def build_html(first, token):
    """The follow-up email.

    Structure: one line of context, the small ask, the mechanics, the
    measured turnaround, then two CTAs in priority order (text first,
    apply second).
    """
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
    <p style="margin:0 0 16px 0;font-size:15px;line-height:1.62;color:#475569">
      Last month we wrote to say we&rsquo;d rebuilt how we bid cars &mdash; we call it
      <b style="color:#0f172a">DealerPrice</b>. It&rsquo;s been running four weeks now.
    </p>
    <p style="margin:0 0 20px 0;font-size:15px;line-height:1.62;color:#475569">
      Forget the paperwork for a second. <b style="color:#0f172a">Just send us one car.</b>
      No account, no application, nothing to sign &mdash; text us a VIN and we&rsquo;ll
      work it up and come back to you with a number.
    </p>
  </td></tr>

  <tr><td style="padding:0 34px 0 34px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-left:4px solid #b91c2c;border-radius:10px">
      <tr><td style="padding:16px 19px">
        <div style="font-size:12px;font-weight:800;color:#b91c2c;letter-spacing:.5px">TEXT US A CAR</div>
        <div style="font-size:21px;font-weight:800;color:#0f172a;margin:5px 0 8px 0">%(sms)s</div>
        <p style="margin:0;font-size:14.5px;line-height:1.6;color:#475569">
          The <b>VIN and mileage</b> is enough. Or just <b>photograph the VIN plate and
          the odometer</b> &mdash; we&rsquo;ll read them off the picture. Add photos of the
          car if you have them handy.
        </p>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:11px 34px 0 34px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-left:4px solid #1e3a8a;border-radius:10px">
      <tr><td style="padding:16px 19px">
        <div style="font-size:12px;font-weight:800;color:#1e3a8a;letter-spacing:.5px">WHAT HAPPENS NEXT</div>
        <p style="margin:6px 0 0 0;font-size:14.5px;line-height:1.6;color:#475569">
          You get a text straight back confirming we have the car. Then
          <b style="color:#0f172a">Joe, Todd or Gregg</b> comes back to you with the
          number &mdash; a real buyer, not an auto-reply.
        </p>
      </td></tr>
    </table>
  </td></tr>

  <tr><td align="center" style="padding:28px 34px 6px 34px">
    <p style="margin:0 0 13px 0;font-size:16px;line-height:1.5;color:#0f172a;font-weight:700">
      Want your own submission page instead?
    </p>
    <p style="margin:0 0 16px 0;font-size:14.5px;line-height:1.6;color:#475569">
      Set up takes a few minutes and gets you a private link that doesn&rsquo;t expire.
    </p>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
      <td align="center" style="background:#b91c2c;border-radius:8px">
        <a href="%(cta)s" style="display:inline-block;padding:14px 32px;font-size:15px;font-weight:700;color:#ffffff;text-decoration:none">Get Set Up &rarr;</a>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:22px 34px 30px 34px">
    <p style="margin:0;font-size:14px;line-height:1.62;color:#475569">
      Got a car you&rsquo;re unsure on? <b>Just reply to this email</b> &mdash; it goes
      straight to our buying desk.
    </p>
    <p style="margin:15px 0 0 0;font-size:14px;color:#0f172a"><b>Experience Wholesale</b></p>
  </td></tr>

  <tr><td style="padding:15px 34px 22px 34px;background:#f8fafc;border-top:1px solid #eef0f3">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 10px 0"><tr>
      <td style="padding:0 8px 0 0"><a href="https://www.facebook.com/experiencewholesale/" style="text-decoration:none"><img src="https://experience-wholesale.net/static/icons/facebook.png" width="26" height="26" alt="Facebook" style="display:block;width:26px;height:26px;border:0"></a></td>
      <td style="padding:0 8px 0 0"><a href="https://www.linkedin.com/company/experiencewholesale" style="text-decoration:none"><img src="https://experience-wholesale.net/static/icons/linkedin.png" width="26" height="26" alt="LinkedIn" style="display:block;width:26px;height:26px;border:0"></a></td>
      <td style="padding:0 8px 0 0"><a href="https://www.instagram.com/experiencewholesale/" style="text-decoration:none"><img src="https://experience-wholesale.net/static/icons/instagram.png" width="26" height="26" alt="Instagram" style="display:block;width:26px;height:26px;border:0"></a></td>
    </tr></table>
    <p style="margin:0;font-size:11.5px;line-height:1.55;color:#94a3b8">
      Experience Wholesale &middot; 1210 S Andrews Ave, Pompano Beach, FL 33069<br>
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
        print('REFUSED - not in the allowlist: %s' % ', '.join(bad))
        return 2
    # ── guard 3 ────────────────────────────────────────────────────────────
    if len(RECIPIENTS) > MAX_RECIPIENTS:
        print('REFUSED - %d recipients exceeds MAX_RECIPIENTS=%d'
              % (len(RECIPIENTS), MAX_RECIPIENTS))
        return 2

    key = os.environ.get('RESEND_API_KEY', '')
    if args.send and not key:
        print('REFUSED - RESEND_API_KEY not set')
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
                  % (email, first, token[:10] + '...', len(html)))
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
