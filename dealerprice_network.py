"""
dealerprice_network.py — DealerPrice "Become part of the Dealer Network" gate.
DEALERPRICE_NETWORK_2026_06_30.

Adds the vetting/application funnel for dealerprice.net WITHOUT touching the
live bid/enrichment path. Two halves:

  • Public (X-Auth, login-exempt via the /api/dealerprice/ prefix):
      POST /api/dealerprice/apply           — a dealer applies to the network
      POST /api/dealerprice/check-existing   — Q0 "already an EW dealer?" lookup

  • Operator (behind the app-level require_login, NOT under /api/):
      GET  /network/applications                  — review queue
      GET  /network/application/<id>              — full vetting packet
      GET  /network/application/<id>/doc/<which>  — serve the PRIVATE license/Tax-ID image
      POST /network/application/<id>/approve      — mint member token + invite the dealer
      POST /network/application/<id>/reject
      POST /network/application/<id>/needs-info

HARD RULES honored: no FK to bids (HR1 — can never block enrichment); LSL is
read-only (HR6); no cloud LLM (HR4); C1-only (HR5); no import-time DDL — the
tables ship via ops/migrations/2026-06-30_dealer_network.sql (HR8). Uploaded
license/Tax-ID docs are stored OUTSIDE /static and served only through the
login-gated /network/.../doc route.

Registered by wsgi.py on every gunicorn worker boot (drift-resistant), the same
pattern as recon / wholesaler_review / network_push.
"""
from __future__ import annotations
import os
import re
import json
import time
import base64
import secrets
from datetime import datetime, timezone, date

from flask import (Blueprint, render_template, request, jsonify, abort,
                   session, redirect, url_for, send_file, current_app)

bp = Blueprint('dealerprice_network', __name__)

SECRET = (os.environ.get('EW_DEALERPRICE_SECRET') or '').strip()
LSL_DB = os.environ.get('LSL_DB_PATH', '/opt/livesaleslog/crm.db')
PRIV_DOC_ROOT = os.environ.get('DP_DOC_ROOT', '/opt/expwholesale/private/dealer_docs')
# public base for the magic link we text/email an approved dealer
DP_PUBLIC_BASE = os.environ.get('DP_PUBLIC_BASE', 'https://dealerprice.net')

DEALER_TYPES = ['Exotic', 'High-Volume Commodity', 'Niche / Specialty',
                'Wholesale', 'Large-Volume Mix', 'Subprime']

# ── DP_EMAIL_IDENTITY_2026_07_28 ─────────────────────────────────────────────
# Dealer-facing mail had been borrowing recon's sender, so an approved dealer's
# welcome arrived from recon@ (the reconditioning address). DealerPrice now has
# its own identity. info@experience-wholesale.net is on the Resend-VERIFIED
# experience-wholesale.net domain (DKIM resend._domainkey + send. return-path
# already live), so this needs no new DNS.
#
# ⚠ info@experience-wholesale.net is send-only today — Cloudflare Email Routing
#   has no route for it (probed: 550 5.1.1 Address does not exist). Replies are
#   carried entirely by the Reply-To list below. If you want the from-address
#   itself to receive, add a CF Email Routing rule for info@.
DP_EMAIL_FROM = os.environ.get('DP_EMAIL_FROM',
                               'Experience Wholesale <info@experience-wholesale.net>')

# Everyone who should see a dealer's reply, once testing is over.
DP_REPLY_TO_ALL = [a.strip() for a in os.environ.get(
    'DP_REPLY_TO_ALL',
    'oscar@experience-wholesale.com,joe@experience-wholesale.com,'
    'todd@experience-wholesale.com,gregg@doubleclutch.com').split(',') if a.strip()]

# TESTING GATE (operator, 2026-07-28): while testing, only the first recipient
# gets replies — joe/todd/gregg are held back so test traffic doesn't hit them.
#   gate ABSENT  = testing  -> oscar only
#   gate PRESENT = live     -> all four
# ⛔ REMINDER: `touch /opt/expwholesale/DP_REPLY_ALL_LIVE` when testing is done.
DP_REPLY_ALL_GATE = os.environ.get('DP_REPLY_ALL_GATE',
                                   '/opt/expwholesale/DP_REPLY_ALL_LIVE')


def _dp_reply_to():
    """The Reply-To list, narrowed to the first address while the gate is off."""
    if not DP_REPLY_TO_ALL:
        return []
    return DP_REPLY_TO_ALL if os.path.exists(DP_REPLY_ALL_GATE) else DP_REPLY_TO_ALL[:1]


# ── DP_ONBOARD_TESTING_2026_07_28 ────────────────────────────────────────────
# While testing the new welcome email/SMS, EVERY approval goes to the operator
# instead of the real dealer — a wrong-looking welcome must never reach a dealer.
#   gate ABSENT  = TEST -> email+SMS redirected to the operator, subject prefixed
#   gate PRESENT = LIVE -> the actual dealer is contacted
# ⛔ REMINDER: `touch /opt/expwholesale/DP_ONBOARD_LIVE` when the wording is signed off.
DP_ONBOARD_GATE = os.environ.get('DP_ONBOARD_GATE', '/opt/expwholesale/DP_ONBOARD_LIVE')
DP_TEST_EMAIL = os.environ.get('DP_TEST_EMAIL', 'opies32765@gmail.com')
DP_TEST_PHONE = os.environ.get('DP_TEST_PHONE',
                               os.environ.get('EW_TEST_USER_PHONE', '')).strip()
EW_SMS_NUMBER = os.environ.get('EW_SMS_NUMBER', '(754) 247-1123')


def _dp_onboard_live():
    return os.path.exists(DP_ONBOARD_GATE)


# ── DP_APPLY_ALERT_2026_07_28 ────────────────────────────────────────────────
# The three partners (+ the operator) want a TEXT the moment a dealer applies,
# so a packet doesn't sit unreviewed. Same gate pattern as the onboarding mail:
#   gate ABSENT  = testing -> only the FIRST number in the list (the operator)
#   gate PRESENT = live    -> everyone in the list
# ⛔ REMINDER: fill in the three partner mobiles, then
#    `touch /opt/expwholesale/DP_APPLY_ALERT_LIVE`.
# Deliberately seeded with ONLY the operator's verified number — texting a
# guessed number would page a stranger.
DP_APPLY_ALERT_PHONES = [p.strip() for p in os.environ.get(
    'DP_APPLY_ALERT_PHONES', '4074309675').split(',') if p.strip()]
DP_APPLY_ALERT_GATE = os.environ.get('DP_APPLY_ALERT_GATE',
                                     '/opt/expwholesale/DP_APPLY_ALERT_LIVE')


def _dp_apply_alert_phones():
    """Who gets the new-application text; first entry only until the gate is on."""
    if not DP_APPLY_ALERT_PHONES:
        return []
    return (DP_APPLY_ALERT_PHONES if os.path.exists(DP_APPLY_ALERT_GATE)
            else DP_APPLY_ALERT_PHONES[:1])


# Logo for the partner MMS. Served from the Resend-verified .net host, publicly
# reachable with no login - Twilio has to be able to fetch it.
DP_ALERT_LOGO = os.environ.get(
    'DP_ALERT_LOGO', 'https://experience-wholesale.net/static/ew-logo-sms.png')
# ew-logo-sms.png is the SMALL variant (390x128, 15KB) - the email one is
# 1170px and rendered huge in the message bubble.


def _possessive(name):
    """Turner Motors -> Turner Motors'  |  Bozard -> Bozard's
    (Currently unused - kept for copy that needs a possessive.)"""
    n = (name or '').strip()
    return n + ("'" if n.endswith('s') else "'s")


def _dp_alert_send(to_e164, body):
    """Send the partner alert as an MMS with the EW car logo, falling back to a
    plain SMS if Twilio rejects the media (bad URL, carrier, size...). The alert
    matters more than the picture, so a media failure must never lose the message.

    NOTE: this talks to Twilio directly rather than via app.send_sms, so it also
    re-applies NO_DATA_REQUEST_2026_06_12 here - going around send_sms would
    otherwise silently go around that operator rule too.
    """
    import re as _re
    if body and _re.search(
            r'(send|text|reply|provide|verify|confirm)[^.!?]{0,40}'
            r'(vin|mileage|miles|odometer)', body, _re.I):
        print('[dp-alert] suppressed by NO_DATA_REQUEST: %r' % body[:80], flush=True)
        return False
    sid = os.environ.get('TWILIO_ACCOUNT_SID')
    tok = os.environ.get('TWILIO_AUTH_TOKEN')
    frm = os.environ.get('TWILIO_PHONE')
    if not (sid and tok and frm):
        from app import send_sms
        return send_sms(to_e164, body)
    try:
        from twilio.rest import Client
        kw = dict(to=to_e164, from_=frm, body=body)
        # Off by default: an attached image becomes a SECOND bubble. The logo
        # still shows, via og:image on the /a/<id> link-preview card.
        if os.environ.get('DP_ALERT_MMS', '').strip().lower() in ('1', 'true', 'yes'):
            kw['media_url'] = [DP_ALERT_LOGO]
        Client(sid, tok).messages.create(**kw)
        return True
    except Exception as e:
        print('[dp-alert] MMS failed (%s) - falling back to plain SMS' % e, flush=True)
        try:
            from app import send_sms
            return send_sms(to_e164, body)
        except Exception as e2:
            print('[dp-alert] SMS fallback failed: %s' % e2, flush=True)
            return False


# DP_APPLY_RECEIPT_2026_07_29 — the applicant hears back the moment they
# apply, by text AND email, and both say the same thing the success screen on
# the site says: a decision will arrive by email. Three channels, one promise.
#
# Under DP_REHEARSAL these go to the team (or the operator alone in solo mode)
# instead of the applicant, exactly like the approval and decline mail, so a
# rehearsal never contacts a real dealer.


def _dp_receipt_html(first, dealership):
    logo = 'https://experience-wholesale.net/static/ew-logo-email.png'
    greet = ('Hi %s,' % first) if first else 'Hello,'
    return """\
<!--[if mso]><style>body,table,td{font-family:Arial,sans-serif !important}</style><![endif]-->
<div style="background:#f4f5f7;padding:28px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb">

  <tr><td align="center" style="padding:30px 30px 20px 30px;border-bottom:1px solid #eef0f3">
    <img src="%(logo)s" width="240" alt="Experience Wholesale"
         style="display:block;width:240px;max-width:70%%;height:auto;border:0">
  </td></tr>

  <tr><td style="padding:30px 34px 34px 34px">
    <p style="margin:0 0 16px 0;font-size:15px;line-height:1.62;color:#0f172a">%(greet)s</p>
    <p style="margin:0 0 16px 0;font-size:15px;line-height:1.62;color:#475569">
      We&rsquo;ve received your application to the Experience Wholesale dealer network
      for <b style="color:#0f172a">%(dealership)s</b>.
    </p>
    <p style="margin:0 0 16px 0;font-size:15px;line-height:1.62;color:#0f172a">
      Our team will review it and <b>you&rsquo;ll receive an email with our decision</b>.
    </p>
    <p style="margin:0;font-size:15px;line-height:1.62;color:#475569">
      Nothing else is needed from you right now.
    </p>
    <p style="margin:26px 0 0 0;font-size:15px;line-height:1.62;color:#0f172a">
      &mdash; The Experience Wholesale Team
    </p>
  </td></tr>

</table></div>""" % {'logo': logo, 'greet': greet, 'dealership': dealership}


def _dp_apply_receipt(app_id, dealership, contact_name, email, phone, tcpa=False):
    """Text + email the applicant that we have their application. Best-effort:
    a delivery problem must never fail the submission itself."""
    first = (_s(contact_name) or '').split(' ')[0]
    dealership = _s(dealership) or 'your dealership'

    to_email = _s(email)
    digits = _digits(phone)
    to_phone = ('+1' + digits) if len(digits) == 10 else None
    if _dp_rehearsing():
        to_email = _dp_rehearsal_email()
        rp = [_digits(p) for p in _dp_rehearsal_phones()]
        to_phone = ('+1' + rp[0]) if rp and len(rp[0]) == 10 else None

    # No VIN/mileage wording here on purpose - NO_DATA_REQUEST_2026_06_12 blocks
    # any outbound text that looks like it is asking a dealer for car data.
    body = ('Thanks%s - Experience Wholesale received your dealer application for %s. '
            'We will email you with our decision.'
            % ((' ' + first) if first else '', dealership))
    # TCPA_REQUIRED_2026_07_30 — never text without consent, even if a row
    # somehow reaches here without it (rows predating the gate, or a direct API
    # post). The email below still goes; only the text is withheld.
    if to_phone and not tcpa:
        print('[dp-network] apply receipt sms SUPPRESSED app=%s - no TCPA consent'
              % app_id, flush=True)
        to_phone = None
    if to_phone:
        try:
            from app import send_sms
            send_sms(to_phone, body)
            print('[dp-network] apply receipt sms -> %s (rehearsal=%s)'
                  % (to_phone, _dp_rehearsing()), flush=True)
        except Exception as e:
            print('[dp-network] apply receipt sms: %s' % e, flush=True)

    if to_email:
        try:
            _email(to_email, 'We received your application - Experience Wholesale',
                   _dp_receipt_html(first, dealership))
        except Exception as e:
            print('[dp-network] apply receipt email: %s' % e, flush=True)


def _dp_apply_alert_sms(app_id, dealership, contact, hist, is_existing):
    """Text the partners that a new dealer applied. Best-effort; never raises
    into the apply path (an alert must not fail a dealer's application).

    NOTE: this goes to EW's OWN partners, not to a submitter, so the ledger
    figures are fine here — ENRICHMENT_SMS_DENY_BY_DEFAULT_2026_07_28 governs
    what we text to DEALERS, which is a different audience entirely.
    """
    h = hist or {}
    if h.get('tx_count'):
        who = ('Known EW dealer: %d bought / %d sold, $%s gross with us.'
               % (h.get('bought_cars') or 0, h.get('sold_cars') or 0,
                  '{:,.0f}'.format(h.get('total_gross') or 0)))
        # For a RETURNING dealer the most useful fact is how long it has been -
        # a warm relationship and a cold one read very differently.
        last = _s(h.get('last_activity'))
        if last:
            who += ' Last deal %s.' % last[:7]
    elif is_existing:
        who = 'Says they are an existing dealer, but no transactions on our ledger.'
    else:
        who = 'New applicant - no history with us.'
    # No application number in the copy: it is internal plumbing, means nothing to
    # a partner, and the link already carries it.
    #
    # The link sits in the MIDDLE on purpose. iOS renders a separate link-preview
    # bubble when a URL is the last thing in a message, which made every alert
    # arrive as two bubbles. Putting the ledger line after the link fixes that
    # without padding the copy with filler. Do not move the URL to the end.
    body = ('New dealer application - DealerPrice\n%s%s\n%s/a/%d\n%s'
            % (dealership or '(no name)',
               (' - %s' % contact) if contact else '',
               os.environ.get('PUBLIC_BASE_URL', 'https://experience-wholesale.net'),
               app_id,
               who))
    live = os.path.exists(DP_APPLY_ALERT_GATE)
    for ph in _dp_apply_alert_phones():
        digits = _digits(ph)
        if len(digits) != 10:
            print('[dp-network] apply-alert skip bad phone %r' % ph, flush=True)
            continue
        try:
            _dp_alert_send('+1' + digits, body)
            print('[dp-network] apply-alert -> %s (live=%s)' % (digits, live), flush=True)
        except Exception as e:
            print('[dp-network] apply-alert %s failed: %s' % (digits, e), flush=True)


def _invite_html(name, link):
    """The dealer welcome email. Table-based + inline CSS (Outlook/Gmail safe),
    600px, logo served from the Resend-verified experience-wholesale.net so it
    loads without auth. DP_WELCOME_V2_2026_07_28."""
    logo = 'https://experience-wholesale.net/static/ew-logo-email.png'
    return """\
<!--[if mso]><style>body,table,td{font-family:Arial,sans-serif !important}</style><![endif]-->
<div style="background:#f4f5f7;padding:28px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb">

  <tr><td align="center" style="padding:30px 30px 20px 30px;border-bottom:1px solid #eef0f3">
    <img src="%(logo)s" width="240" alt="Experience Wholesale"
         style="display:block;width:240px;max-width:70%%;height:auto;border:0">
  </td></tr>

  <tr><td style="padding:30px 34px 6px 34px">
    <div style="display:inline-block;background:#e7f7ee;color:#15803d;border-radius:999px;padding:5px 13px;font-size:12px;font-weight:700;letter-spacing:.3px">APPROVED</div>
    <h1 style="margin:15px 0 8px 0;font-size:23px;line-height:1.28;color:#0f172a;font-weight:800">Welcome to the network, %(name)s.</h1>
    <p style="margin:0;font-size:15px;line-height:1.62;color:#475569">
      You&rsquo;re cleared to submit vehicles to Experience Wholesale for a bid.
      There are <b>two ways</b> to send us a car &mdash; use whichever is faster for you.
    </p>
  </td></tr>

  <tr><td style="padding:22px 34px 0 34px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-left:4px solid #b91c2c;border-radius:10px">
      <tr><td style="padding:17px 19px">
        <div style="font-size:12px;font-weight:800;color:#b91c2c;letter-spacing:.5px">OPTION 1 &mdash; TEXT US</div>
        <div style="font-size:21px;font-weight:800;color:#0f172a;margin:5px 0 9px 0">%(sms)s</div>
        <p style="margin:0 0 10px 0;font-size:14px;line-height:1.6;color:#475569">Text the car straight from your phone. Any of these work:</p>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%" style="font-size:14px;line-height:1.6;color:#334155">
          <tr><td style="padding:3px 0" valign="top" width="22">&bull;</td><td style="padding:3px 0">Type the <b>VIN and mileage</b></td></tr>
          <tr><td style="padding:3px 0" valign="top">&bull;</td><td style="padding:3px 0">Or just <b>photograph the VIN plate and the odometer</b> &mdash; we read them automatically</td></tr>
          <!-- SCREENSHOT_OPTION_2026_07_30 -->
          <tr><td style="padding:3px 0" valign="top">&bull;</td><td style="padding:3px 0">Or <b>screenshot the VIN and miles</b> from vAuto, your DMS or inventory system &mdash; we read that too</td></tr>
          <tr><td style="padding:3px 0" valign="top">&bull;</td><td style="padding:3px 0">Add as many <b>photos of the car</b> as you want</td></tr>
        </table>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:13px 34px 0 34px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%"
           style="background:#fffbeb;border:1px solid #fde68a;border-radius:10px">
      <tr><td style="padding:15px 18px;font-size:14px;line-height:1.62;color:#78350f">
        <b>&#9201; Send everything for one car within 60 seconds.</b><br>
        Every message you send within <b>60 seconds</b> of your first one is treated as the
        <b>same vehicle</b> &mdash; the VIN, the mileage and all photos land on one bid.
        Wait longer than that and the next message starts a <b>new car</b>. So snap your
        pictures first, then send them together.
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:13px 34px 0 34px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-left:4px solid #1e3a8a;border-radius:10px">
      <tr><td style="padding:17px 19px">
        <div style="font-size:12px;font-weight:800;color:#1e3a8a;letter-spacing:.5px">OPTION 2 &mdash; YOUR PRIVATE LINK</div>
        <p style="margin:7px 0 13px 0;font-size:14px;line-height:1.6;color:#475569">
          Your own submission page &mdash; no time limit, take as long as you like.
          Bookmark it; it&rsquo;s yours and it doesn&rsquo;t expire.
        </p>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td align="center" style="background:#b91c2c;border-radius:8px">
            <a href="%(link)s" style="display:inline-block;padding:13px 27px;font-size:15px;font-weight:700;color:#ffffff;text-decoration:none">Submit a vehicle &rarr;</a>
          </td></tr>
        </table>
        <p style="margin:12px 0 0 0;font-size:12px;line-height:1.5;color:#94a3b8;word-break:break-all">%(link)s</p>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:24px 34px 0 34px">
    <div style="font-size:12px;font-weight:800;color:#0f172a;letter-spacing:.5px;padding-bottom:4px">WHAT HAPPENS NEXT</div>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%" style="font-size:14px;line-height:1.58;color:#475569">
      <tr>
        <td width="30" valign="top" style="padding:9px 0"><div style="width:22px;height:22px;border-radius:50%%;background:#0f172a;color:#fff;font-size:12px;font-weight:700;text-align:center;line-height:22px">1</div></td>
        <td style="padding:9px 0">However you send it, you&rsquo;ll get a <b>text back confirming your bid number</b>. Hang on to that number.</td>
      </tr>
      <tr>
        <td width="30" valign="top" style="padding:9px 0;border-top:1px solid #eef0f3"><div style="width:22px;height:22px;border-radius:50%%;background:#0f172a;color:#fff;font-size:12px;font-weight:700;text-align:center;line-height:22px">2</div></td>
        <td style="padding:9px 0;border-top:1px solid #eef0f3">We work the car on our end &mdash; history, condition, market.</td>
      </tr>
      <tr>
        <td width="30" valign="top" style="padding:9px 0;border-top:1px solid #eef0f3"><div style="width:22px;height:22px;border-radius:50%%;background:#b91c2c;color:#fff;font-size:12px;font-weight:700;text-align:center;line-height:22px">3</div></td>
        <td style="padding:9px 0;border-top:1px solid #eef0f3"><b>An Experience Wholesale team member will get back to you</b> with where we are on it. A real person, not an automated number.</td>
      </tr>
    </table>
  </td></tr>

  <tr><td style="padding:22px 34px 30px 34px">
    <p style="margin:0;font-size:14px;line-height:1.62;color:#475569">
      Questions on a car or a number? <b>Just reply to this email</b> &mdash; it goes straight to our buying desk.
    </p>
    <p style="margin:15px 0 0 0;font-size:14px;color:#0f172a"><b>Experience Wholesale</b></p>
  </td></tr>

  <tr><td style="padding:15px 34px 22px 34px;background:#f8fafc;border-top:1px solid #eef0f3">
    <p style="margin:0;font-size:11.5px;line-height:1.55;color:#94a3b8">
      You received this because your dealership was approved for the Experience Wholesale
      dealer network. Your submission link is private &mdash; don&rsquo;t forward it.
    </p>
  </td></tr>

</table></div>""" % {'logo': logo, 'name': name, 'link': link, 'sms': EW_SMS_NUMBER}


# NO_CACHE_2026_07_17 — the operator review pages (/network/...) re-run live LSL
# roster + deal-ledger lookups on every load, so a stale browser/CDN copy shows
# out-of-date match data (bit us: a corrected packet kept showing the old copy
# on refresh because the response carried no Cache-Control at all). Force
# no-store on every response from this blueprint (review pages AND the JSON
# APIs — none of them should ever be cached).
@bp.after_request
def _no_store(resp):
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# ── small coercion helpers ──────────────────────────────────────────────────
def _s(v):
    return ('' if v is None else str(v)).strip()


def _digits(v):
    return re.sub(r'[^0-9]', '', _s(v))


def _int(v):
    d = _digits(v)
    try:
        return int(d) if d else None
    except ValueError:
        return None


def _num(v):
    s = re.sub(r'[^0-9.]', '', _s(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _b(v):
    return v in (True, 'true', 'on', '1', 1, 'yes', 'YES')


def _date(v):
    s = _s(v)
    if not s:
        return None
    if re.fullmatch(r'\d{4}-\d{2}', s):
        s += '-01'
    try:
        datetime.strptime(s, '%Y-%m-%d')
        return s
    except ValueError:
        return None


# ── DB / LSL ────────────────────────────────────────────────────────────────
def _db():
    from app import get_db
    return get_db()


def _lsl_conn():
    import sqlite3
    c = sqlite3.connect('file:%s?mode=ro' % LSL_DB, uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


_LEGAL_SUFFIXES = ('incorporated', 'corporation', 'company', 'llc', 'inc', 'corp', 'ltd', 'llp', 'co')


def _normalize_name(name):
    """Collapse a dealership name to bare alphanumerics (+ drop a trailing
    legal suffix) so 'AutoStreetUSA' and LSL's stored 'Auto Street Usa'
    compare equal despite spacing/punctuation/case drift that raw SQL
    LIKE patterns miss (a dealer types their own stylization; LSL has
    whatever was typed in at onboarding — they rarely match verbatim)."""
    s = re.sub(r'[^a-z0-9]', '', (name or '').lower())
    for suf in _LEGAL_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf) + 2:
            s = s[:-len(suf)]
            break
    return s


_JUNK_PHONES = {str(dig) * 10 for dig in range(10)} | {'1234567890'}


def _norm_phone10(v):
    """Digits-only, normalized to 10 digits (drops a leading US country '1')
    so '5616850133' and '+1(561)-685-0133' compare equal. Returns '' for
    degenerate placeholder numbers (LSL has real rows with '999-999-9999'
    filler) so junk can never satisfy the phone-match fallback below."""
    d = _digits(v)
    if len(d) == 11 and d.startswith('1'):
        d = d[1:]
    if len(d) == 10 and d in _JUNK_PHONES:
        return ''
    return d


def _supplier_match_dict(row, matched_via=None):
    """Review-packet match dict for a suppliers row, ENRICHED with the vetting
    signals LSL already holds — verified dealer license / tax cert / W9,
    onboard date, active/blocked status — the data that actually tells the
    operator whether a rostered dealer is safe, beyond just name/city/phone."""
    d = {'matched': True, 'source': 'suppliers', 'name': _s(row['name']),
         'supplier_id': row['id'],
         'contact': _s(row['primary_contact']),
         'phone': _s(row['office'] or row['primary_contact_mobile']),
         'city': _s(row['city']), 'state': _s(row['state']),
         'email': _s(row['email']),
         'address': _s(row['full_address'] or row['address1']),
         'status': _s(row['status']),
         'is_blocked': bool(row['is_blocked']),
         'approved': bool(row['approved']),
         'trusted': bool(row['trusted']),
         'has_license': bool(_s(row['license_url'])),
         'has_tax_cert': bool(_s(row['tax_cert_url'])),
         'license_exp': _s(row['license_expiration'])[:10],
         'tax_cert_exp': _s(row['tax_cert_expiration'])[:10]}
    # onboard date + W9 status live only inside the raw LSL payload
    try:
        rj = json.loads(row['raw_json'] or '{}')
        d['onboarded'] = (_s(rj.get('createdAt'))[:10] or None)
        d['has_w9'] = bool(rj.get('w9Status') or rj.get('w9FileLocation'))
        d['verified'] = bool(rj.get('verified'))
    except Exception:
        pass
    if matched_via:
        d['matched_via'] = matched_via
    return d


def _roster_match(name, phone=None):
    """Is this dealership/referrer already an EW counterparty? Read-only LSL
    lookup against suppliers (sellers/wholesalers) then customers (buyers).
    Tries exact/prefix/contains on the raw name first (cheap, covers the
    common case), then falls back to a normalized-name compare and a
    phone-number compare over the full suppliers table (~2.6k rows, cheap
    to scan in Python) — a dealer's own stylization of their name often
    doesn't literally substring-match LSL's stored form, but their phone
    number never drifts. Returns {} when unknown, else an enriched match
    dict for the review packet."""
    name = _s(name)
    phone10 = _norm_phone10(phone) if phone else ''
    if len(name) < 3 and len(phone10) != 10:
        return {}
    try:
        c = _lsl_conn()
        try:
            r = None
            if len(name) >= 3:
                r = c.execute(
                    "SELECT * FROM suppliers WHERE name=? COLLATE NOCASE LIMIT 1",
                    (name,)).fetchone()
                if not r:
                    r = c.execute(
                        "SELECT * FROM suppliers WHERE name LIKE ? "
                        "ORDER BY length(name) LIMIT 1", (name + '%',)).fetchone()
                if not r and len(name) >= 5:                 # contains fallback (partial dealership / referrer)
                    r = c.execute(
                        "SELECT * FROM suppliers WHERE name LIKE ? "
                        "ORDER BY length(name) LIMIT 1", ('%' + name + '%',)).fetchone()
            if r:
                return _supplier_match_dict(r)

            # normalized-name / phone fallback — catches e.g. "AutoStreetUSA"
            # vs LSL's "Auto Street Usa" (same dealer, different spacing).
            norm_target = _normalize_name(name) if len(name) >= 3 else ''
            if norm_target or len(phone10) == 10:
                for row in c.execute("SELECT * FROM suppliers WHERE name<>''"):
                    if norm_target and _normalize_name(row['name']) == norm_target:
                        return _supplier_match_dict(row, 'normalized_name')
                    if len(phone10) == 10 and phone10 in (
                            _norm_phone10(row['office']), _norm_phone10(row['primary_contact_mobile'])):
                        return _supplier_match_dict(row, 'phone')

            if len(name) >= 3:
                r = c.execute(
                    "SELECT company_name, full_name, mobile FROM customers "
                    "WHERE company_name=? COLLATE NOCASE OR full_name=? COLLATE NOCASE "
                    "LIMIT 1", (name, name)).fetchone()
                if r:
                    return {'matched': True, 'source': 'customers',
                            'name': _s(r['company_name'] or r['full_name']),
                            'contact': _s(r['full_name']), 'phone': _s(r['mobile'])}
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] roster_match: %s' % e, flush=True)
    return {}


def _roster_search(q, limit=8):
    """Typeahead for the Q0 existing-dealer path — distinct supplier names."""
    q = _s(q)
    if len(q) < 2:
        return []
    out = []
    try:
        c = _lsl_conn()
        try:
            rows = c.execute(
                "SELECT DISTINCT name, city, state FROM suppliers "
                "WHERE name LIKE ? AND name<>'' "
                "ORDER BY (name LIKE ?) DESC, length(name) LIMIT ?",
                ('%' + q + '%', q + '%', limit)).fetchall()
            out = [{'name': r['name'], 'city': _s(r['city']), 'state': _s(r['state'])}
                   for r in rows]
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] roster_search: %s' % e, flush=True)
    return out


def _lsl_history_agg(c, swhere, sparams, bwhere, bparams):
    """DEAD CODE — no caller. ⛔ DO NOT RESURRECT AS-IS (DIRECTION_SPLIT_2026_07_28).
    It treats a `deals.supplier_*` clause as the cars EW BOUGHT and a
    `customer_name` clause as the cars EW SOLD. Both are inverted: on a wholesale
    deal row `supplier_id` is the dealer EW SOLD TO, and `customer_name` is a
    mirror of that same buyer (verified 29,182/29,182). Using this would report
    every sale as a purchase and then double-count it. See _lsl_history.

    Aggregate the deals ledger for a supplier-side + buyer-side WHERE clause.
    Returns the history dict, or {} if the clauses matched nothing."""
    s = c.execute(
        "SELECT count(*) n, COALESCE(SUM(purchase_cost),0) paid, "
        "COALESCE(SUM(front_value),0) gross, MIN(sold_at) f, MAX(sold_at) l "
        "FROM deals WHERE " + swhere, sparams).fetchone()
    b = c.execute(
        "SELECT count(*) n, COALESCE(SUM(sale_price),0) spent, "
        "MIN(sold_at) f, MAX(sold_at) l "
        "FROM deals WHERE " + bwhere, bparams).fetchone()
    sn, bn = (s['n'] or 0), (b['n'] or 0)
    if sn + bn == 0:
        return {}
    names = [r['nm'] for r in c.execute(
        "SELECT supplier_name nm FROM deals WHERE " + swhere + " "
        "UNION SELECT customer_name nm FROM deals WHERE " + bwhere + " "
        "LIMIT 6", list(sparams) + list(bparams)).fetchall() if r['nm']]
    firsts = [d for d in (s['f'], b['f']) if d]
    lasts = [d for d in (s['l'], b['l']) if d]
    return {
        'matched': True,
        'names': names,
        'total_deals': sn + bn,
        'source_deals': sn, 'source_paid': int(s['paid'] or 0), 'source_gross': int(s['gross'] or 0),
        'buyer_deals': bn, 'buyer_spent': int(b['spent'] or 0),
        'first_deal': (min(firsts)[:10] if firsts else None),
        'last_deal': (max(lasts)[:10] if lasts else None),
    }


def _lsl_history(name, supplier_id=None, matched_name=None):
    """VERIFIED two-sided transaction history for a dealer resolved to a
    suppliers.id. Requires supplier_id. Pure read-only (HR6).

    ══ DIRECTION_SPLIT_2026_07_28 ═══════════════════════════════════════════
    The previous version unioned two OPPOSITE-direction legs into a single
    "cars EW bought from them" number, because it read `deals.supplier_id` as
    "the dealer EW bought from". It is not. Verified by joining deals→inventory
    on VIN across 31,139 wholesale rows:

        deals.supplier_name == inventory.customer_name (the BUYER)   90.9%
        deals.source_name   == inventory.source        (the SELLER)  94.1%
        deals.supplier_name == inventory.source        (the SELLER)   2.7%

    So on a wholesale deal row, `supplier_id` identifies the dealer EW SOLD the
    car TO. The dealer EW bought it FROM lives in `source_name` /
    inventory.purchased_from_id. Corroboration: the two legs are DISJOINT
    (Maroun 503504 → 61 payments VINs vs 82 deals VINs, overlap 0), and every
    payments-leg VIN carries inventory.purchased_from_id == that same
    supplier_id while its inventory.customer_name is a different dealer.

      • CARS EW BOUGHT FROM them = payments(vendor_id=id, type='Purchased',
        payee_type='Supplier')  — EW paid them; money out.
      • CARS EW SOLD TO them    = deals(supplier_id=id) — EW invoiced them;
        front_value = gross EW made on that sale.

    ⛔ Audit rules (2026-07-17) that still hold, one with a corrected reason:
      1. NEVER match `customer_name` — it is a mirror, but of the BUYER, not of
         the supplier. Matching it would double-count each sale against the
         dealer we sold to. (The original note had the direction inverted.)
      2. `payee_type` Customer/Bank = a consumer/lender payment, NOT dealer
         activity (a stranger's $31k Mustang mis-attributed to a same-named
         dealer). Still excluded.
      3. NAME as an identity key collides (43 dealer names → multiple ids; one
         switchboard phone → 13 rooftops). supplier_id only; a name never
         counts on its own.
    """
    if not supplier_id:
        return {}
    try:
        c = _lsl_conn()
        try:
            # cars EW bought — payments leg. payee_type='Supplier' is the
            # entity-space discriminator: payments.vendor_id references
            # suppliers.id ONLY for Supplier-payee rows. Customer/Bank vendor_ids
            # live in a DIFFERENT id-space — that's exactly how a private
            # individual 'Oscar Pastrana' (payee_type=Customer) collided with the
            # same-numbered dealer. Audit-verified: of Supplier-payee vendor_ids
            # in suppliers, all but 2 (legit DBA/parent variants) name-match, and
            # every real dealer purchase has a suppliers row — so the id alone is
            # reliable under this filter; no name guard needed.
            prows = c.execute(
                "SELECT vin_no, amount, stock_no, title_status, created_at "
                "FROM payments WHERE vendor_id=? AND type='Purchased' "
                "AND payee_type='Supplier'",
                (supplier_id,)).fetchall()
            # cars EW SOLD TO them — deals.supplier_id is the BUYER (see docstring)
            srows = c.execute(
                "SELECT vin_no, purchase_cost, sale_price, front_value, sold_at, stock_no "
                "FROM deals WHERE supplier_id=?", (supplier_id,)).fetchall()

            # SOURCE_LEG_2026_07_30 — cars EW bought from them that were never
            # booked as a Purchased payment. Keyed on inventory.purchased_from_id
            # (an ID, per audit rule 3 — never on source_name, which collides).
            # Without this the buy side is payments-only and 1,856 of 1,999
            # source dealers read "0 bought".
            brows_raw = c.execute(
                "SELECT d.vin_no, d.purchase_cost, d.sale_price, d.front_value, "
                "       d.sold_at, d.stock_no "
                "FROM deals d JOIN inventory i ON i.vin_no = d.vin_no "
                "WHERE i.purchased_from_id=?", (supplier_id,)).fetchall()
            # one row per VIN (a car can carry several deal rows); keep the latest
            _bby = {}
            for r in brows_raw:
                v = _s(r['vin_no'])
                if not v:
                    continue
                if v not in _bby or (_s(r['sold_at']) or '') > (_s(_bby[v]['sold_at']) or ''):
                    _bby[v] = r
            brows = list(_bby.values())

            pay_vins = set(_s(r['vin_no']) for r in prows if _s(r['vin_no']))
            src_vins = set(_bby.keys())
            # SELF_DEAL_2026_07_30 — a deal row can name the same dealer as BOTH
            # the source and the customer (EW bought the car from them and their
            # name is also in the customer field). That is ONE transaction, so it
            # must not count as a purchase AND a sale. The car belongs to the buy
            # leg — it is the direction the money actually moved — so drop those
            # VINs from the sell leg. Keyed on the id pair, never on names.
            srows = [r for r in srows if _s(r['vin_no']) not in src_vins]
            sold_vins = set(_s(r['vin_no']) for r in srows if _s(r['vin_no']))
            # union: the payments leg is a strict SUBSET of the source leg in
            # every case measured, so this never double-counts a car.
            bought_vins = pay_vins | src_vins
            # money EW paid: payment amounts, plus purchase_cost for source-leg
            # cars that have no payment row (else those cars count as $0).
            src_only_paid = int(sum(r['purchase_cost'] or 0
                                    for v, r in _bby.items() if v not in pay_vins))
            src_dates = sorted((_s(r['sold_at']))[:10] for r in brows if _s(r['sold_at']))
            if not bought_vins and not sold_vins:
                return {}
            pay_dates = sorted((_s(r['created_at']))[:10] for r in prows if _s(r['created_at']))
            # buy first/last must span BOTH buy legs, not payments only
            buy_dates = sorted(pay_dates + src_dates)
            sell_dates = sorted((_s(r['sold_at']))[:10] for r in srows if _s(r['sold_at']))

            # ── what EW made on this relationship ────────────────────────────
            # sell side: front_value on the deals we invoiced THEM for. Dedupe by
            # VIN first — a VIN can carry >1 deal row and summing raw double-counts.
            sell_by_vin = {}
            sell_novin = []
            for r in srows:
                v = _s(r['vin_no'])
                if not v:
                    sell_novin.append(r)
                elif v not in sell_by_vin or (_s(r['sold_at']) or '') > (_s(sell_by_vin[v]['sold_at']) or ''):
                    sell_by_vin[v] = r
            sell_rows = list(sell_by_vin.values()) + sell_novin
            sold_gross = int(sum(r['front_value'] or 0 for r in sell_rows))
            sold_revenue = int(sum(r['sale_price'] or 0 for r in sell_rows))

            # buy side: the cars EW bought FROM them and then resold to someone
            # else — EW's gross is the front_value on THAT resale deal. Exclude
            # any VIN already counted on the sell leg so a buy-back can't
            # double-count the same front_value.
            resale_vins = [v for v in bought_vins if v not in sold_vins]
            buy_resale_gross, buy_resale_cars = 0, 0
            if resale_vins:
                rby = {}
                rph = ','.join('?' * len(resale_vins))
                for r in c.execute(
                        "SELECT vin_no, front_value, sold_at FROM deals "
                        "WHERE vin_no IN (%s)" % rph, resale_vins):
                    v = _s(r['vin_no'])
                    if v and (v not in rby or (_s(r['sold_at']) or '') > (_s(rby[v]['sold_at']) or '')):
                        rby[v] = r
                buy_resale_cars = len(rby)
                buy_resale_gross = int(sum(r['front_value'] or 0 for r in rby.values()))
            total_gross = sold_gross + buy_resale_gross

            # per-VIN car list (for the expandable "all cars" panel on the packet)
            cars = []
            for r in prows:
                cars.append({'order': _s(r['stock_no']), 'vin': _s(r['vin_no']),
                             'amount': int(r['amount'] or 0),
                             'date': (_s(r['created_at']))[:10] or None,
                             'dir': 'buy', 'gross': 0, 'kind': 'EW bought from them'})
            # SOURCE_LEG_2026_07_30 — buys with no payment row (skip any VIN the
            # payments leg already listed so a car never appears twice).
            for v, r in _bby.items():
                if v in pay_vins:
                    continue
                # BUY_GROSS_2026_07_30 — show what EW actually MADE on the car.
                # This was hard-coded to 0, so every car a dealer sold us showed
                # "EW gross —" and the packet read as though we made nothing on
                # 18 cars. front_value on the resale IS our gross on that unit.
                cars.append({'order': _s(r['stock_no']), 'vin': v,
                             'amount': int(r['purchase_cost'] or 0),
                             'date': (_s(r['sold_at']))[:10] or None,
                             'dir': 'buy', 'gross': int(r['front_value'] or 0),
                             'kind': 'EW bought from them'})
            for r in sell_rows:
                cars.append({'order': _s(r['stock_no']), 'vin': _s(r['vin_no']),
                             'amount': int(r['sale_price'] or 0),
                             'date': (_s(r['sold_at']))[:10] or None,
                             'dir': 'sell', 'gross': int(r['front_value'] or 0),
                             'kind': 'EW sold to them'})
            cars.sort(key=lambda x: x['date'] or '', reverse=True)
            # attach year/make/model per VIN: inventory (in-stock cars) then
            # deals.vehicle_info (a clean full description) which wins when present
            vlist = [car['vin'] for car in cars if car['vin']]
            vmap = {}
            if vlist:
                vph = ','.join('?' * len(vlist))
                for r in c.execute(
                        "SELECT vin_no, group_model_trim_year y, vehicle_make_name mk, "
                        "vehicle_series_name sr, group_model_name gm FROM inventory "
                        "WHERE vin_no IN (%s)" % vph, vlist):
                    desc = ' '.join(_s(x) for x in
                                    (r['y'], r['mk'], (r['sr'] or r['gm'])) if _s(x))
                    if _s(r['vin_no']) and desc:
                        vmap[_s(r['vin_no'])] = desc
                for r in c.execute(
                        "SELECT vin_no, vehicle_info FROM deals WHERE vin_no IN (%s)" % vph, vlist):
                    if _s(r['vin_no']) and _s(r['vehicle_info']):
                        vmap[_s(r['vin_no'])] = _s(r['vehicle_info'])
            for car in cars:
                car['vehicle'] = vmap.get(car['vin'], '')
            all_dates = [car['date'] for car in cars if car['date']]
            return {
                'matched': True,
                # ── EW BOUGHT FROM them ──
                'bought_cars': len(bought_vins),
                'bought_paid': int(sum(r['amount'] or 0 for r in prows)) + src_only_paid,
                'buy_first': buy_dates[0] if buy_dates else None,
                'buy_last': buy_dates[-1] if buy_dates else None,
                'titles_pending': sum(1 for r in prows if _s(r['title_status']) != 'Yes'),
                # ── EW SOLD TO them ──
                'sold_cars': len(sell_rows),
                'sold_revenue': sold_revenue,
                'sold_gross': sold_gross,
                'sell_first': sell_dates[0] if sell_dates else None,
                'sell_last': sell_dates[-1] if sell_dates else None,
                # ── what EW made on the whole relationship ──
                'buy_resale_cars': buy_resale_cars,
                'buy_resale_gross': buy_resale_gross,
                'total_gross': total_gross,
                'tx_count': len(bought_vins) + len(sell_rows),
                'first_activity': min(all_dates) if all_dates else None,
                'last_activity': max(all_dates) if all_dates else None,
                'cars': cars,
                # legacy aliases — older callers/templates still read these
                'payments_cars': len(pay_vins),
                'payments_paid': int(sum(r['amount'] or 0 for r in prows)),
                'pay_first': pay_dates[0] if pay_dates else None,
                'pay_last': pay_dates[-1] if pay_dates else None,
            }
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] lsl_history: %s' % e, flush=True)
    return {}


# ── PERSON-LEVEL HISTORY MERGE (PERSON_MERGE_2026_07_21) ─────────────────────
# Bug management flagged (Sam Beatty / The Naples Source): dealer history was
# scoped to ONE suppliers row, so an individual who worked at multiple rooftops
# showed a fragmented, wrong picture ("on file since 2025-05-09" but "first
# purchase 2026-03-08"). Two fixes:
#   1) first_activity = earliest across BOTH legs (deals sold_at + payments
#      created_at), not payments-only (payments.paid_at is NULL so its created_at
#      is a data-entry date, later than the real first deal).
#   2) merge the same INDIVIDUAL across stores, keyed on contact NAME + PHONE:
#        CONFIRMED  exact full name + matching phone   -> auto-merge
#        STRONG     exact full name + same state       -> auto-merge
#        REVIEW     typo / first-initial-only / diff-state / diff-phone -> ask
#      The first-name guard means family (Sam vs Dave/Adam Beatty) is NEVER
#      auto-merged. Additive: wraps the audited single-store _lsl_history.
# Chosen by operator 2026-07-21 ("confirm weak matches", "name + phone").

def _person_tokens(nm):
    """Lowercase alpha tokens of a person name: 'Sam  Beatty' -> ['sam','beatty']."""
    import re as _re
    return [t for t in _re.sub(r'[^a-z ]', ' ', _s(nm).lower()).split() if t]


def _editdist(a, b, cap=2):
    """Bounded Levenshtein (returns cap+1 once it exceeds cap). Cheap for names."""
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < best:
                best = cur[j]
        if best > cap:
            return cap + 1
        prev = cur
    return prev[lb]


def _same_person_name(a_tokens, b_name):
    """Compare applicant contact-name tokens to a store's primary_contact.
    Returns 'exact' | 'fuzzy' (last name typo) | 'initial' (last = initial) | ''.
    FIRST names must agree — this is the family guard (Sam != Dave/Adam)."""
    b = _person_tokens(b_name)
    if len(a_tokens) < 2 or len(b) < 1:
        return ''
    af, al = a_tokens[0], a_tokens[-1]
    if b[0] != af:                       # different first name -> not this person
        return ''
    if len(b) < 2:                       # store contact is just a first name
        return ''
    bl = b[-1]
    if bl == al:
        return 'exact'
    if len(bl) <= 2 and bl[:1] == al[:1]:
        return 'initial'                 # 'Sam B' vs 'Sam Beatty'
    if len(al) >= 4 and len(bl) >= 4 and _editdist(al, bl) <= 2:
        return 'fuzzy'                    # 'Beatty' vs 'Beety'
    return ''


_US_STATES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
    'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY', 'district of columbia': 'DC'}


def _norm_state(s):
    """FL / Florida / florida -> 'FL' so state comparison survives LSL's mixed
    storage of full names vs abbreviations."""
    s = _s(s).strip().lower()
    if not s:
        return ''
    if len(s) == 2:
        return s.upper()
    return _US_STATES.get(s, s[:2].upper())


def _person_phone(c, norm_name):
    """Best known phone for a person, from dealer_profile (person-level agg)."""
    try:
        r = c.execute("SELECT best_phone FROM dealer_profile WHERE norm_name=? "
                      "AND best_phone IS NOT NULL AND best_phone<>'' LIMIT 1",
                      (norm_name,)).fetchone()
        return _norm_phone10(r['best_phone']) if r else ''
    except Exception:
        return ''


def _resolve_person_stores(c, contact_name, contact_phone, primary_sid, primary_state):
    """Other suppliers rows that are the SAME INDIVIDUAL as the applicant contact.
    Returns (merge:list[dict], review:list[dict]) — merge = auto-include
    (CONFIRMED/STRONG), review = operator-confirm (weak). Read-only."""
    atoks = _person_tokens(contact_name)
    aphone = _norm_phone10(contact_phone)
    merge, review = [], []
    if len(atoks) < 2:
        return merge, review
    seen = set()
    for row in c.execute(
            "SELECT id, name, primary_contact, city, state, office, "
            "primary_contact_mobile FROM suppliers "
            "WHERE primary_contact IS NOT NULL AND primary_contact<>''"):
        sid = row['id']
        if sid == primary_sid or sid in seen:
            continue
        kind = _same_person_name(atoks, row['primary_contact'])
        if not kind:
            continue
        seen.add(sid)
        sphone = (_norm_phone10(row['office']) or _norm_phone10(row['primary_contact_mobile'])
                  or _person_phone(c, _normalize_name(row['primary_contact'])))
        # phone is an UPGRADE signal only — a match confirms; a mismatch does NOT
        # veto (the same person legitimately has different lines office vs cell at
        # different rooftops, and family is already separated by the first-name
        # guard above). States are normalized (FL == Florida) before comparing.
        phone_ok = bool(aphone) and bool(sphone) and aphone == sphone
        sn, ps = _norm_state(row['state']), _norm_state(primary_state)
        states_differ = bool(sn) and bool(ps) and sn != ps
        item = {'id': sid, 'name': _s(row['name']), 'contact': _s(row['primary_contact']),
                'city': _s(row['city']), 'state': _s(row['state'])}
        if kind == 'exact' and phone_ok:
            item['tier'] = 'confirmed'
            merge.append(item)
        elif kind == 'exact' and not states_differ:
            item['tier'] = 'strong'
            merge.append(item)
        elif kind == 'exact':
            item['tier'] = 'review'
            item['reason'] = 'different state'
            review.append(item)
        else:
            item['tier'] = 'review'
            item['reason'] = ('name typo' if kind == 'fuzzy' else 'first-initial only')
            review.append(item)
    return merge, review


def _load_person_links(base_sid):
    """Operator's persisted decisions on weak person-matches for this applicant's
    primary rooftop: {linked_sid: 'confirmed'|'rejected'}. Lets a one-click
    'same person' / 'not them' STICK across views (table dealerprice_person_links,
    PG :5433). Empty on any error (feature degrades to display-only)."""
    out = {}
    if not base_sid:
        return out
    try:
        db = _db(); cur = db.cursor()
        try:
            cur.execute("SELECT linked_sid, decision FROM dealerprice_person_links "
                        "WHERE primary_sid=%s", (base_sid,))
            for r in cur.fetchall():
                out[int(r['linked_sid'])] = _s(r['decision'])
        finally:
            db.close()
    except Exception as e:
        print('[dp-network] person_links load: %s' % e, flush=True)
    return out


def _lsl_history_person(name, name_match, contact_name=None, contact_phone=None):
    """Person-level VERIFIED history: the matched store's ledger UNIONED with the
    other rooftops the SAME individual (contact name + phone) has worked at, so a
    dealer who moved stores shows ONE true history. Wraps the audited single-store
    _lsl_history per rooftop and merges; adds first_activity (earliest across BOTH
    legs), a per-store breakdown, a review list of weak matches to confirm, and
    applies the operator's persisted confirm/reject decisions. Read-only (HR6)."""
    nm = name_match or {}
    base_sid = nm.get('supplier_id')
    if not base_sid:
        return _lsl_history(name, None)
    merge, review = [], []
    try:
        c = _lsl_conn()
        try:
            merge, review = _resolve_person_stores(
                c, contact_name or nm.get('contact'), contact_phone,
                base_sid, nm.get('state'))
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] person_stores: %s' % e, flush=True)

    # apply the operator's persisted one-click decisions:
    #   confirmed -> promote a weak match into the auto-merge
    #   rejected  -> drop it (from review, or override an auto-merge)
    links = _load_person_links(base_sid)
    decided = []
    if links:
        keep = []
        for m in merge:
            if links.get(m['id']) == 'rejected':
                decided.append({'id': m['id'], 'name': m['name'], 'decision': 'rejected'})
            else:
                keep.append(m)
        merge = keep
        newrev = []
        for it in review:
            dec = links.get(it['id'])
            if dec == 'confirmed':
                it2 = dict(it); it2['tier'] = 'confirmed'; it2['manual'] = True
                merge.append(it2)
                decided.append({'id': it['id'], 'name': it['name'], 'decision': 'confirmed'})
            elif dec == 'rejected':
                decided.append({'id': it['id'], 'name': it['name'], 'decision': 'rejected'})
            else:
                newrev.append(it)
        review = newrev

    order = [{'id': base_sid, 'name': nm.get('name'), 'contact': nm.get('contact'),
              'state': nm.get('state'), 'tier': 'primary'}] + merge
    hists = []
    for st in order:
        h = _lsl_history(name, st['id'])
        if h and h.get('matched') and h.get('tx_count'):
            h['_store'] = st
            hists.append(h)

    if not hists:
        base = _lsl_history(name, base_sid) or {}
        if review:
            base['review'] = review
        return base

    # union car rows across rooftops, dedupe by VIN *within each direction*
    # (DIRECTION_SPLIT_2026_07_28 — the same VIN can legitimately appear once as
    # a buy and once as a sell; collapsing them would erase one leg)
    byvin, misc = {}, []
    for h in hists:
        for car in h.get('cars', []):
            v = _s(car.get('vin'))
            if not v:
                misc.append(car)
                continue
            k = (car.get('dir') or 'buy', v)
            if k not in byvin or (car.get('date') or '~') < (byvin[k].get('date') or '~'):
                byvin[k] = car
    mcars = list(byvin.values()) + misc
    mcars.sort(key=lambda x: x.get('date') or '', reverse=True)
    dates = sorted(car['date'] for car in mcars if car.get('date'))
    buy_cars = [c for c in mcars if c.get('dir') != 'sell']
    sell_cars = [c for c in mcars if c.get('dir') == 'sell']

    def _sfirst(h):
        ds = [c['date'] for c in h.get('cars', []) if c.get('date')]
        return min(ds) if ds else None
    stores = [{'id': h['_store']['id'], 'name': _s(h['_store'].get('name')),
               'contact': _s(h['_store'].get('contact')),
               'bought': h.get('bought_cars', 0), 'sold': h.get('sold_cars', 0),
               'gross': h.get('total_gross', 0), 'first': _sfirst(h),
               'tier': h['_store'].get('tier'),
               'manual': bool(h['_store'].get('manual'))} for h in hists]

    # single rooftop, no weak matches: return it as-is but with both-leg dates
    if len(hists) == 1 and not review and not decided:
        h = dict(hists[0])
        h['first_activity'] = dates[0] if dates else h.get('pay_first')
        h['last_activity'] = dates[-1] if dates else h.get('pay_last')
        h['primary_sid'] = base_sid
        h.pop('_store', None)
        return h

    buy_dates = sorted(c['date'] for c in buy_cars if c.get('date'))
    sell_dates = sorted(c['date'] for c in sell_cars if c.get('date'))
    sold_gross = sum(int(c.get('gross') or 0) for c in sell_cars)
    buy_resale_gross = sum(h.get('buy_resale_gross', 0) for h in hists)
    return {
        'matched': True,
        # ── EW BOUGHT FROM them ──
        'bought_cars': len(buy_cars),
        'bought_paid': sum(int(c.get('amount') or 0) for c in buy_cars),
        'buy_first': buy_dates[0] if buy_dates else None,
        'buy_last': buy_dates[-1] if buy_dates else None,
        'titles_pending': sum(h.get('titles_pending', 0) for h in hists),
        # ── EW SOLD TO them ──
        'sold_cars': len(sell_cars),
        'sold_revenue': sum(int(c.get('amount') or 0) for c in sell_cars),
        'sold_gross': sold_gross,
        'sell_first': sell_dates[0] if sell_dates else None,
        'sell_last': sell_dates[-1] if sell_dates else None,
        # ── what EW made on the whole relationship ──
        'buy_resale_cars': sum(h.get('buy_resale_cars', 0) for h in hists),
        'buy_resale_gross': buy_resale_gross,
        'total_gross': sold_gross + buy_resale_gross,
        'tx_count': len(buy_cars) + len(sell_cars),
        'first_activity': dates[0] if dates else None,
        'last_activity': dates[-1] if dates else None,
        'cars': mcars,
        'merged_store_count': len(hists),
        'stores': stores,
        'review': review,
        'primary_sid': base_sid,
        'manual_links': decided,
        # legacy aliases
        'payments_cars': sum(h.get('payments_cars', 0) for h in hists),
        'payments_paid': sum(h.get('payments_paid', 0) for h in hists),
        'pay_first': dates[0] if dates else None,
        'pay_last': dates[-1] if dates else None,
    }
# ── end PERSON_MERGE_2026_07_21 ──────────────────────────────────────────────


def _auto_classify(lsl_hist):
    """Assign a classification from the VERIFIED ledger — NOT self-declaration
    or a bare roster match (operator directive 2026-07-17, 12-month window):
      • current_partner  — real transactions, last activity within 12 months
      • previous_partner — real history, but nothing in 12+ months
      • new_applicant    — no verified transactions (whatever they claimed)
    The operator can still override manually on the packet."""
    h = lsl_hist or {}
    if not (h.get('tx_count') or 0):
        return 'new_applicant'
    last = h.get('last_activity')
    if last:
        try:
            from datetime import date, datetime as _dt
            d = _dt.strptime(str(last)[:10], '%Y-%m-%d').date()
            return 'current_partner' if (date.today() - d).days <= 365 else 'previous_partner'
        except Exception:
            pass
    return 'current_partner'   # has real transactions, date unknown → treat as current


# ── private document storage (NOT under /static) ────────────────────────────
def _save_doc(app_id, which, data_url):
    """Persist a base64 data-url (license / tax-id image or PDF) to a private,
    0600 file under PRIV_DOC_ROOT/<app_id>/. Returns the absolute path or None."""
    if not data_url:
        return None
    media = 'image/jpeg'
    s = data_url
    if isinstance(s, str) and s.startswith('data:'):
        try:
            head, s = s.split(',', 1)
            media = head.split(';')[0].split(':', 1)[1] or media
        except Exception:
            return None
    try:
        raw = base64.b64decode(s)
    except Exception:
        return None
    if not raw or len(raw) > 18_000_000:           # guard: empty / >18MB
        return None
    ext = {'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png',
           'image/webp': 'webp', 'application/pdf': 'pdf'}.get(media.lower(), 'bin')
    d = os.path.join(PRIV_DOC_ROOT, str(app_id))
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(PRIV_DOC_ROOT, 0o700)
        os.chmod(d, 0o700)
    except Exception:
        pass
    path = os.path.join(d, '%s.%s' % (which, ext))
    with open(path, 'wb') as f:
        f.write(raw)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def _doc_mime(path):
    ext = (path or '').rsplit('.', 1)[-1].lower()
    return {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp',
            'pdf': 'application/pdf'}.get(ext, 'application/octet-stream')


# ── notifications ───────────────────────────────────────────────────────────
def _tg(msg):
    try:
        from app import _tg_worker_alert
        _tg_worker_alert(msg)
    except Exception as e:
        print('[dp-network] tg: %s' % e, flush=True)


def _email(to_addr, subject, html):
    """Send dealer-facing mail under DealerPrice's OWN identity via Resend
    (DP_EMAIL_IDENTITY_2026_07_28). Previously this borrowed recon's sender, so
    dealers got mail from recon@. Reply-To carries the whole team (gated during
    testing — see _dp_reply_to). Best-effort: never raises into the caller."""
    if not to_addr:
        return False
    key = os.environ.get('RESEND_API_KEY', '')
    if not key:
        print('[dp-network:STUB] to=%s subj=%s' % (to_addr, subject), flush=True)
        return False
    try:
        import resend
        resend.api_key = key
        # Resend wants a LIST for multiple recipients; a single comma-joined
        # string is silently one bad address. Accept either form from callers.
        to_list = ([a.strip() for a in to_addr.split(',') if a.strip()]
                   if isinstance(to_addr, str) else list(to_addr))
        payload = {'from': DP_EMAIL_FROM, 'to': to_list,
                   'subject': subject, 'html': html}
        rt = _dp_reply_to()
        if rt:
            payload['reply_to'] = rt
        resend.Emails.send(payload)
        gated = '' if os.path.exists(DP_REPLY_ALL_GATE) else ' [reply-to GATED]'
        print('[dp-network:EMAIL] to=%s reply_to=%s%s' % (to_addr, ','.join(rt), gated), flush=True)
        return True
    except Exception as e:
        print('[dp-network:EMAIL-FAIL] %s: %s' % (type(e).__name__, e), flush=True)
        return False


# -- DP_INTENT_2026_07_29 ----------------------------------------------------
# Joe's ask: capture WHAT the dealer wants from EW up front, so intent can be
# categorised and a BUYER can be handed to the existing dealer-scanner fleet
# automatically once vetting passes.
INTENT_LABELS = {
    'sell': 'Submit & sell cars',
    'buy':  'Buy cars from EW',
    'both': 'Buy & sell',
}


# WEBSITE_NUDGE_2026_07_30 — one place that decides what a usable site URL is.
# Deliberately permissive about form: dealers type "smithmotors.com",
# "www.smithmotors.com", "https://smithmotors.com/inventory" and all are fine.
# Strict about exactly one thing — there must be a dot and a plausible TLD, so
# a bare "smithmotors" cannot reach the scanner and fail forever in silence.
_URL_RE = re.compile(r'^(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(/[^\s]*)?$', re.I)


def _url_ok(v):
    v = (_s(v) or '').strip()
    if not v or re.search(r'\s', v):
        return False
    return bool(_URL_RE.match(v))


def _norm_url(v):
    """Tidy to a storable URL. Returns '' for empty; never raises."""
    v = (_s(v) or '').strip().rstrip('/')
    if not v:
        return ''
    if not re.match(r'^https?://', v, re.I):
        v = 'https://' + v
    try:
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(v)
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, '')).rstrip('/')
    except Exception:
        return v


def _dp_intent(v):
    """Normalise whatever the site posts to sell/buy/both, else None."""
    v = (_s(v) or '').strip().lower()
    return v if v in INTENT_LABELS else None


# Gate, same pattern as the other DealerPrice rollouts. While ABSENT the approve
# step only LOGS what it would have registered - it never touches the scanner
# fleet. REMINDER: `touch /opt/expwholesale/DP_AUTOSCAN_LIVE` after testing.
DP_AUTOSCAN_GATE = os.environ.get('DP_AUTOSCAN_GATE',
                                  '/opt/expwholesale/DP_AUTOSCAN_LIVE')


def _dp_register_scanner(app_id, a, member_id=None):
    """After vetting, put a BUYER into the dealer-scanner DB so their lot starts
    being scanned. Returns a short human note for the packet; never raises.

    A website is deliberately NOT required - plenty of applicants are wholesalers
    with no site at all, and that is a perfectly good dealer. No site simply means
    there is nothing to scan, which we record rather than treat as a failure.
    """
    intent = _dp_intent(a.get('intent'))
    if intent not in ('buy', 'both'):
        return None                      # sellers have no lot for us to scan
    # CONSENT IS REQUIRED. The dealer has to have ticked "monitor my inventory"
    # on the intent step; we do not read anyone's site on the strength of them
    # merely wanting to buy cars. No tick, no scan - recorded, not an error.
    if not a.get('monitor_consent'):
        return 'dealer did not opt in to inventory monitoring - not scanning'
    site = _s(a.get('website'))
    if not site:
        return 'opted in but gave no website - nothing to scan (normal for a wholesaler)'
    if not site.startswith('http'):
        site = 'https://' + site

    live = os.path.exists(DP_AUTOSCAN_GATE)
    db = _db(); cur = db.cursor()
    try:
        # Already known to the scanner? Link, never duplicate. dealers.url is
        # unique, but matching first also lets the packet say "already dealer #N".
        cur.execute("SELECT id, name FROM dealers WHERE url=%s LIMIT 1", (site,))
        row = cur.fetchone()
        if row:
            note = 'already in scanner as dealer #%s (%s)' % (row['id'], row['name'])
            cur.execute("""UPDATE dealer_applications
                              SET scanner_dealer_id=%s, scanner_registered_at=now(),
                                  scanner_note=%s WHERE id=%s""",
                        (row['id'], note, app_id))
            if member_id:
                cur.execute("UPDATE dealerprice_members SET scanner_dealer_id=%s WHERE id=%s",
                            (row['id'], member_id))
            db.commit()
            return note

        if not live:
            note = 'WOULD register %s for scanning (gate off)' % site
            cur.execute("UPDATE dealer_applications SET scanner_note=%s WHERE id=%s",
                        (note, app_id))
            db.commit()
            print('[dp-autoscan] %s app=%s' % (note, app_id), flush=True)
            return note

        cur.execute("""INSERT INTO dealers (name, url, notes, phone)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (url) DO UPDATE SET name=EXCLUDED.name
                       RETURNING id""",
                    (_s(a.get('dealership_name')) or site, site,
                     'Auto-added from DealerPrice application #%s (intent=%s)' % (app_id, intent),
                     _s(a.get('contact_phone'))))
        did = cur.fetchone()['id']
        note = 'registered for scanning as dealer #%s' % did
        cur.execute("""UPDATE dealer_applications
                          SET scanner_dealer_id=%s, scanner_registered_at=now(),
                              scanner_note=%s WHERE id=%s""", (did, note, app_id))
        if member_id:
            cur.execute("UPDATE dealerprice_members SET scanner_dealer_id=%s WHERE id=%s",
                        (did, member_id))
        db.commit()
        print('[dp-autoscan] app=%s -> dealer #%s (%s)' % (app_id, did, site), flush=True)
        return note
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print('[dp-autoscan] app=%s FAILED: %s' % (app_id, e), flush=True)
        return 'scanner registration failed: %s' % e
    finally:
        try:
            db.close()
        except Exception:
            pass


# DP_REHEARSAL_2026_07_29 — while this gate file exists, an approval sends the
# dealer welcome to the PARTNERS instead of the dealer, so the team can see
# exactly what a dealer receives without a real dealer being contacted. This is
# a walkthrough aid, NOT the onboarding gate: remove the file when finished.
#   /opt/expwholesale/DP_REHEARSAL   present -> partners   absent -> real dealer
DP_REHEARSAL_GATE = os.environ.get('DP_REHEARSAL_GATE',
                                   '/opt/expwholesale/DP_REHEARSAL')
# Approval mail goes to the partners INDIVIDUALLY, not to info@ - info@ fans out
# to all four including the operator, and he is out of the approval loop.
DP_REHEARSAL_EMAIL = os.environ.get(
    'DP_REHEARSAL_EMAIL',
    'joe@experience-wholesale.com,todd@experience-wholesale.com,gregg@doubleclutch.com')

# Who sees an APPROVAL. Kept separate from DP_APPLY_ALERT_PHONES on purpose: the
# operator still wants to hear that a dealer APPLIED, but not to sit in the
# approval loop, so the two lists are allowed to differ.
DP_APPROVAL_PHONES = [p.strip() for p in os.environ.get(
    'DP_APPROVAL_PHONES', '3522099696,5613018622,5166803500').split(',') if p.strip()]

# DP_SOLO_TEST_2026_07_29 — while this gate file exists, EVERY rehearsal
# notification (approval text + email, decline letter) goes to the operator and
# nobody else. For dry-running the flow end to end without three other phones
# lighting up. Sits INSIDE rehearsal: applicants are still never contacted.
#   /opt/expwholesale/DP_SOLO_TEST   present -> operator only
DP_SOLO_GATE = os.environ.get('DP_SOLO_GATE', '/opt/expwholesale/DP_SOLO_TEST')
DP_OPERATOR_PHONE = os.environ.get('DP_OPERATOR_PHONE', '4074309675').strip()
DP_OPERATOR_EMAIL = os.environ.get('DP_OPERATOR_EMAIL', 'opies32765@gmail.com').strip()


def _dp_solo():
    return os.path.exists(DP_SOLO_GATE)


def _dp_rehearsal_phones():
    """Who hears about an approval during a rehearsal."""
    return [DP_OPERATOR_PHONE] if _dp_solo() else DP_APPROVAL_PHONES


def _dp_rehearsal_email():
    """Who reads a rehearsal email (approval welcome / decline letter)."""
    return DP_OPERATOR_EMAIL if _dp_solo() else DP_REHEARSAL_EMAIL


def _dp_rehearsing():
    return os.path.exists(DP_REHEARSAL_GATE)


def _invite_member(m):
    """Text + email an approved dealer their private portal link (hex token =
    SMS-safe; /d/<token> = encrypted-looking + bookmarkable).

    DP_WELCOME_V2_2026_07_28 — both messages now teach the TWO submission paths
    (text the 754 line, or the private link) and the 60-second same-car window.
    While DP_ONBOARD_GATE is absent everything is redirected to the operator so a
    draft welcome can never reach a real dealer."""
    link = '%s/d/%s' % (DP_PUBLIC_BASE.rstrip('/'), m['token'])
    name = _s(m.get('dealership_name')) or 'there'
    live = _dp_onboard_live()

    # ── SMS ──
    phone = _digits(m.get('contact_phone'))
    to_phone = ('+1' + phone) if len(phone) == 10 else None
    # TCPA_REQUIRED_2026_07_30 — the welcome text is the single most important
    # message we send (it carries the portal link), which makes it exactly the
    # one we must not send without consent. Read it from the application; a
    # member with no linked application, or one that predates the consent gate,
    # is treated as NO consent and gets the email only. Fails CLOSED on error.
    if to_phone:
        _ok = False
        try:
            _db2 = _db(); _c2 = _db2.cursor()
            _c2.execute("SELECT tcpa_consent FROM dealer_applications WHERE id=%s",
                        (m.get('application_id'),))
            _r2 = _c2.fetchone()
            _ok = bool(_r2 and _r2['tcpa_consent'])
            _db2.close()
        except Exception as _e2:
            print('[dp-network] invite tcpa lookup failed (treating as NO consent): %s'
                  % _e2, flush=True)
        if not _ok:
            print('[dp-network] invite sms SUPPRESSED member=%s app=%s - no TCPA consent'
                  % (m.get('id'), m.get('application_id')), flush=True)
            to_phone = None
    if not live:
        to_phone = DP_TEST_PHONE or None      # TEST: operator only
    # Rehearsal: the whole team sees the dealer-facing text, the dealer gets nothing.
    rehearse_to = []
    if _dp_rehearsing():
        rehearse_to = ['+1' + _digits(p) for p in _dp_rehearsal_phones()
                       if len(_digits(p)) == 10]
        to_phone = None
    # ⚠ WORDING IS CONSTRAINED by NO_DATA_REQUEST_2026_06_12 (operator): app.py's
    # send_sms() silently DROPS any message matching
    #   (send|text|reply|provide|verify|confirm)[^.!?]{0,40}(vin|mileage|miles|odometer)
    # so a dealer is never texted asking for VIN/miles. This onboarding text is
    # instructional, not a request, but the guard can't tell — so keep a sentence
    # break (. ! ?) between any of those verbs and the words VIN/miles/odometer.
    # The guard is correct; do NOT weaken it to make copy fit. Re-test wording
    # against that regex before changing this string.
    # WELCOME_LINK_MIDDLE_2026_07_30 — the link must NOT be the last thing in
    # the body. A trailing URL makes the messaging app render a link-preview
    # card as a SEPARATE bubble, which arrives looking like an empty message
    # (reported by a real dealer). Same fix as the partner alert above; the
    # numbered steps are swapped so the link sits in the middle and the body
    # ends on text. Do not move the URL back to the end.
    sms_body = (
        "Hi %s - you're approved for the Experience Wholesale dealer network.\n\n"
        % name +
        "Two ways to get us a car.\n\n"
        "1) Your private page, no time limit:\n%s\n\n" % link +
        "2) Or just text this number. Type the VIN and mileage, snap a photo of "
        "the VIN plate and the odometer, or screenshot them from vAuto or your "
        "DMS - we read any of those. Add as many car photos as you like. "
        "Everything for ONE car must land within 60 seconds; after that, the "
        "next message starts a new car.")
    if not live:
        sms_body = '[TEST->%s] %s' % (_s(m.get('contact_phone')) or 'no-phone', sms_body)
    for _t in (rehearse_to or ([to_phone] if to_phone else [])):
        try:
            from app import send_sms
            send_sms(_t, sms_body)
            print('[dp-network] invite sms -> %s (live=%s rehearsal=%s)'
                  % (_t, live, bool(rehearse_to)), flush=True)
        except Exception as e:
            print('[dp-network] invite sms %s: %s' % (_t, e), flush=True)
    if not (rehearse_to or to_phone):
        print('[dp-network] invite sms SKIPPED (live=%s, no destination)' % live, flush=True)

    # ── email ──
    to_email = _s(m.get('contact_email'))
    subject = 'Approved — Experience Wholesale Dealer Network'
    if _dp_rehearsing():
        to_email = _dp_rehearsal_email()       # team, or operator alone in solo
    if not live:
        subject = '[TEST→%s] %s' % (to_email or 'no-email', subject)
        to_email = DP_TEST_EMAIL              # TEST: operator only
    _email(to_email, subject, _invite_html(name, link))


# ── auth ────────────────────────────────────────────────────────────────────
def _bad_secret():
    """Return a JSON 401 if X-Auth is wrong, else None. Returning a response
    (not abort) keeps us off the app's HTML error-handler path, which 500s on
    /api/ routes — matches the existing /api/dealerprice/bid pattern."""
    if not SECRET or (request.headers.get('X-Auth') or '').strip() != SECRET:
        return jsonify({'error': 'bad auth'}), 401
    return None


# DP_APPROVER_2026_07_29 — the dashboard has ONE shared login, so the session
# cannot tell Joe from Todd from Gregg. Rather than build auth, the Approve form
# asks who is clicking and we stamp that. Falls back to the session/user or
# 'operator' for any other action.
# Operator is deliberately NOT on this list (2026-07-29): approving a dealer is
# the partners' call, not his.
DP_APPROVERS = [a.strip() for a in os.environ.get(
    'DP_APPROVERS', 'Joe,Todd,Gregg').split(',') if a.strip()]


def _reviewer():
    # An explicit "approving as" beats the shared login every time.
    try:
        who = (request.form.get('approved_by') or '').strip()
        if who and who in DP_APPROVERS:
            return who
    except Exception:
        pass
    return (session.get('user') or session.get('username')
            or session.get('reviewer') or 'operator')


# ── dashboard nav badge: pending-application count (cached 15s, drift-resistant
#    via @bp.app_context_processor like recon_enabled) ────────────────────────
_PENDING_CACHE = {'t': 0.0, 'n': 0}


@bp.app_context_processor
def _inject_dp_network():
    def dealer_apps_pending():
        now = time.time()
        if now - _PENDING_CACHE['t'] < 15:
            return _PENDING_CACHE['n']
        try:
            db = _db(); cur = db.cursor()
            cur.execute("SELECT count(*) AS n FROM dealer_applications WHERE status='pending'")
            _PENDING_CACHE['n'] = cur.fetchone()['n']
            _PENDING_CACHE['t'] = now
            db.close()
        except Exception:
            pass
        return _PENDING_CACHE['n']
    return {'dealer_apps_pending': dealer_apps_pending}


CLASS_LABELS = {'current_partner': 'Current Partner',
                'previous_partner': 'Previous Partner',
                'new_applicant': 'New Applicant'}


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC API  (dealerprice.net -> EW, shared-secret)
# ════════════════════════════════════════════════════════════════════════════
@bp.route('/api/dealerprice/check-existing', methods=['POST'])
def api_dp_check_existing():
    """Q0 existing-dealer typeahead -> candidate roster names."""
    r = _bad_secret()
    if r:
        return r
    data = request.get_json(silent=True) or {}
    return jsonify({'ok': True, 'matches': _roster_search(data.get('name') or data.get('q'))})


@bp.route('/api/dealerprice/apply', methods=['POST'])
def api_dp_apply():
    """A dealer applies to the network. Lands as a dealer_applications row for
    operator review. New dealers MUST include license + tax-id (number + image)
    up front. NEVER auto-provisions (impersonation guard) — approval is an
    operator action in /network/applications."""
    r = _bad_secret()
    if r:
        return r
    d = request.get_json(silent=True) or {}

    is_existing = _b(d.get('is_existing'))
    dealership = _s(d.get('dealership_name'))
    cname = _s(d.get('contact_name'))
    cemail = _s(d.get('contact_email')).lower()
    cphone = _digits(d.get('contact_phone'))

    # base requirements for everyone. Lot address joined this list 2026-07-29:
    # it is how a dealer gets verified against the license and how anyone finds
    # them, so an application without one cannot really be vetted.
    # ADDRESS_FOR_EVERYONE_2026_07_30: the LABEL is now just 'Address'. It read
    # as "lot" = retail car lot, which a wholesaler working out of an office does
    # not have, so they skipped a field they could actually answer. The db column
    # stays lot_address on purpose - nothing downstream has to move. The
    # existing-dealer path was validated against this list but never rendered a
    # field for it, so "Welcome back" could not be submitted at all; the field
    # now exists on both paths.
    miss = [lbl for k, lbl in (('dealership_name', 'Dealership name'),
                               ('contact_name', 'Your name'),
                               ('contact_email', 'Email'),
                               ('contact_phone', 'Mobile'),
                               ('lot_address', 'Address')) if not _s(d.get(k))]
    if miss:
        return jsonify({'ok': False, 'error': '%s required.' % ', '.join(miss)}), 400

    # Inventory monitoring is opt-in and stays optional - a dealer who only wants
    # to BUY is a perfectly good dealer whether or not they have a site to read.
    # But if they DO tick it, the URL is not optional: _dp_register_scanner has
    # nothing to scan without it and would record "opted in but gave no website"
    # and quietly never scan them, which looks to the dealer like we agreed to.
    # Whatever they typed, store it tidy - the scanner matches dealers.url
    # exactly, so "Smithmotors.com/" and "https://smithmotors.com" must not
    # become two different dealers.
    _site_raw = _s(d.get('website'))
    if _site_raw:
        d['website'] = _norm_url(_site_raw)

    if _b(d.get('monitor_consent')):
        if not _s(d.get('website')):
            return jsonify({'ok': False,
                            'error': 'Add your website so we know which inventory to '
                                     'look at - or untick inventory monitoring.'}), 400
        if not _url_ok(d.get('website')):
            return jsonify({'ok': False,
                            'error': '"%s" does not look like a web address. Please enter '
                                     'the full address, like yourdealership.com.'
                                     % _site_raw}), 400

    # NEW dealers: license + tax-id (number + image) + attestation up front
    if not is_existing:
        if not _s(d.get('license_number')) or not _s(d.get('tax_id')):
            return jsonify({'ok': False, 'error': 'Dealer license number and Tax ID are required.'}), 400
        if not d.get('license_image') or not d.get('taxid_image'):
            return jsonify({'ok': False, 'error': 'A photo of your dealer license and Tax ID / resale certificate is required.'}), 400
        if not _b(d.get('attestation')):
            return jsonify({'ok': False, 'error': 'Please confirm the information is accurate.'}), 400

    # TCPA_REQUIRED_2026_07_30 — applies to EVERY path, new and existing. The
    # box was rendered and stored but never enforced, so applications arrived
    # with consent=false and were texted anyway (app #28). Consent is what makes
    # the receipt text and the approval link legal to send, and onboarding is
    # SMS-first, so this is required rather than optional.
    if not _b(d.get('tcpa_consent')):
        return jsonify({'ok': False,
                        'error': 'Please agree to receive text messages so we can '
                                 'send your application updates.'}), 400

    types = d.get('dealer_types')
    if isinstance(types, list):
        types = ', '.join(_s(x) for x in types if _s(x))
    else:
        types = _s(types)

    name_match = _roster_match(dealership, cphone)
    lsl_hist = _lsl_history_person(dealership, name_match, _s(d.get('contact_name')), cphone)
    referrer = _s(d.get('referrer_name'))
    referrer_match = _roster_match(referrer) if referrer and referrer.lower() not in ('none', 'n/a') else {}

    # keep an audit copy of the submission WITHOUT the big base64 blobs
    audit = {k: v for k, v in d.items() if k not in ('license_image', 'taxid_image', 'photos')}

    from psycopg2.extras import Json
    db = _db(); cur = db.cursor()
    try:
        # ── DEDUP GUARD (NO_DUPES_2026_07_17) — never create a second
        # application row for the same dealer. Match on normalized dealership
        # name OR phone OR email. An already-APPROVED (or member-provisioned)
        # match wins: return it untouched — don't dupe, don't downgrade. Any
        # non-approved matches (pending/needs_info/rejected) are superseded by
        # this fresh submission and deleted (+ their private doc dirs), so the
        # review queue holds exactly ONE row per dealer, always the latest.
        norm_new = _normalize_name(dealership)
        phone_new = _norm_phone10(cphone)
        cur.execute("SELECT id, status, dealership_name, contact_phone, "
                    "contact_email, member_id FROM dealer_applications")
        approved_hit = None
        dupe_ids = []
        for row in cur.fetchall():
            if not ((norm_new and _normalize_name(row['dealership_name']) == norm_new)
                    or (len(phone_new) == 10 and _norm_phone10(row['contact_phone']) == phone_new)
                    or (cemail and _s(row['contact_email']).lower() == cemail)):
                continue
            if row['status'] == 'approved' or row['member_id']:
                approved_hit = row
            else:
                dupe_ids.append(row['id'])
        if approved_hit:
            db.close()
            return jsonify({'ok': True, 'application_id': approved_hit['id'],
                            'status': approved_hit['status'], 'existing': True,
                            'already': True,
                            'message': 'You already have an account with us — no need to reapply.'}), 200
        for did in dupe_ids:
            cur.execute("DELETE FROM dealer_applications WHERE id=%s "
                        "AND status<>'approved' AND member_id IS NULL", (did,))
            import shutil
            shutil.rmtree(os.path.join(PRIV_DOC_ROOT, str(did)), ignore_errors=True)

        cur.execute("""
            INSERT INTO dealer_applications (
                status, is_existing, dealership_name, dba, dealer_group, franchises,
                entity_type, entity_state, years_in_business, years_at_location,
                units_per_month, units_annual, avg_investment_band, avg_investment_num,
                credit_line, floorplan_provider, floorplan_line, dealer_types,
                primary_makes, price_tier, license_number, license_state, license_exp,
                tax_id, bond_provider, bond_amount, physical_lot, lot_address, website,
                reputation_url, auction_access, payment_ready, bank_reference,
                trade_reference, referrer_name, contact_name, contact_email,
                contact_phone, attestation, tcpa_consent, notes, name_match,
                referrer_match, raw_payload, intent, monitor_consent)
            VALUES ('pending',%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,
                    %s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,
                    %s,%s,%s,%s, %s,%s,%s,%s,%s)
            RETURNING id
        """, (
            is_existing, dealership, _s(d.get('dba')), _s(d.get('dealer_group')),
            _s(d.get('franchises')), _s(d.get('entity_type')), _s(d.get('entity_state')),
            _int(d.get('years_in_business')), _int(d.get('years_at_location')),
            _int(d.get('units_per_month')), _int(d.get('units_annual')),
            _s(d.get('avg_investment_band')), _num(d.get('avg_investment_num') or d.get('avg_investment')),
            _num(d.get('credit_line')), _s(d.get('floorplan_provider')), _num(d.get('floorplan_line')),
            types, _s(d.get('primary_makes')), _s(d.get('price_tier')),
            _s(d.get('license_number')), _s(d.get('license_state')), _date(d.get('license_exp')),
            _s(d.get('tax_id')), _s(d.get('bond_provider')), _num(d.get('bond_amount')),
            (_b(d.get('physical_lot')) if d.get('physical_lot') is not None else None),
            _s(d.get('lot_address')), _s(d.get('website')), _s(d.get('reputation_url')),
            _s(d.get('auction_access')), _s(d.get('payment_ready')), _s(d.get('bank_reference')),
            _s(d.get('trade_reference')), referrer, cname, cemail, cphone,
            _b(d.get('attestation')), _b(d.get('tcpa_consent')), _s(d.get('notes')),
            Json(name_match or None), Json(referrer_match or None), Json(audit),
            _dp_intent(d.get('intent')), _b(d.get('monitor_consent')),
        ))
        app_id = cur.fetchone()['id']

        lic = _save_doc(app_id, 'license', d.get('license_image'))
        tax = _save_doc(app_id, 'taxid', d.get('taxid_image'))
        # classify from the VERIFIED ledger (12-month recency), never from
        # self-declaration/roster — see _auto_classify. Operator can override.
        classification = _auto_classify(lsl_hist)
        cur.execute("UPDATE dealer_applications SET license_doc_path=%s, taxid_doc_path=%s, classification=%s, lsl_history=%s WHERE id=%s",
                    (lic, tax, classification, Json(lsl_hist or None), app_id))
        db.commit()
    except Exception as e:
        db.rollback(); db.close()
        print('[dp-network] apply insert: %s' % e, flush=True)
        return jsonify({'ok': False, 'error': 'Could not submit your application — please try again.'}), 500
    db.close()

    # Tell the applicant we have it (text + email). Wrapped: a receipt problem
    # must never turn a successful submission into an error for the dealer.
    try:
        _dp_apply_receipt(app_id, dealership, cname, cemail, cphone,
                          tcpa=_b(d.get('tcpa_consent')))
    except Exception as _e_r:
        print('[dp-network] apply receipt: %s' % _e_r, flush=True)

    tag = 'EXISTING ✓' if is_existing else 'NEW'
    mtag = (' · roster:%s' % name_match['name']) if name_match.get('matched') else ''
    if lsl_hist.get('tx_count'):
        ltag = '\n📊 LSL: EW bought <b>%d</b> / sold <b>%d</b> · EW gross <b>$%s</b>' % (
            lsl_hist.get('bought_cars') or 0, lsl_hist.get('sold_cars') or 0,
            '{:,.0f}'.format(lsl_hist.get('total_gross') or 0))
    else:
        ltag = '\n📊 no prior LSL transaction history'
    _tg('🪪 <b>New Dealer-Network application</b> #%d (%s)\n%s%s%s\n%s · %s\nReview: /network/applications'
        % (app_id, tag, dealership or '?', mtag, ltag, cname, cemail or cphone))
    # DP_APPLY_ALERT_2026_07_28 — text the partners too. Wrapped so a failed
    # alert can never turn a dealer's successful application into an error.
    try:
        _dp_apply_alert_sms(app_id, dealership, cname, lsl_hist, is_existing)
    except Exception as e:
        print('[dp-network] apply-alert fanout: %s' % e, flush=True)
    return jsonify({'ok': True, 'application_id': app_id, 'status': 'pending', 'existing': is_existing})


# ════════════════════════════════════════════════════════════════════════════
# OPERATOR REVIEW  (behind app-level require_login; NOT under /api/)
# ════════════════════════════════════════════════════════════════════════════
@bp.route('/a/<int:app_id>')
def dp_short_application(app_id):
    """Short link for the partner alert.

    Why this is a PAGE and not a 302: a bare redirect sent the link-preview
    crawler (iMessage/WhatsApp) to the login screen, so the message rendered a
    grey card that just said "Login" - useless, and it looked broken. This
    returns proper OpenGraph tags so the preview reads as a dealer application,
    then forwards a real visitor on to the packet.

    Deliberately carries NO dealer data - the applicant's name is not in the
    preview, because anything here is visible to the carrier's crawler and to
    anyone the text is forwarded to. The packet itself stays login-gated.
    """
    target = url_for('dealerprice_network.network_application', app_id=app_id)
    html = """<!doctype html><html><head><meta charset="utf-8">
<title>New dealer application &middot; Experience Wholesale</title>
<meta name="description" content="A dealer has applied to the DealerPrice network. Sign in to review the vetting packet.">
<meta property="og:site_name" content="Experience Wholesale">
<meta property="og:title" content="New dealer application">
<meta property="og:description" content="A dealer has applied to the DealerPrice network. Tap to review the vetting packet.">
<meta property="og:image" content="https://experience-wholesale.net/static/ew-logo-sms.png">
<meta name="twitter:card" content="summary">
<meta name="robots" content="noindex,nofollow">
<meta http-equiv="refresh" content="0;url=%s">
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#0b0f19;color:#e2e8f0;display:grid;place-items:center;height:100vh;margin:0}
a{color:#7dd3fc}</style></head>
<body><div style="text-align:center">
<p>Opening the dealer application&hellip;</p>
<p><a href="%s">Continue</a></p>
</div><script>location.replace(%s);</script></body></html>""" % (
        target, target, __import__('json').dumps(target))
    return html, 200, {'Content-Type': 'text/html; charset=utf-8',
                       'Cache-Control': 'no-store'}


@bp.route('/network/applications')
def network_applications():
    db = _db(); cur = db.cursor()
    cur.execute("""SELECT id, created_at, status, is_existing, dealership_name,
                          dealer_types, units_per_month, avg_investment_band,
                          credit_line, license_number, contact_name, contact_email,
                          contact_phone, name_match, member_id, classification, lsl_history
                     FROM dealer_applications
                    ORDER BY (status='pending') DESC, created_at DESC LIMIT 300""")
    rows = cur.fetchall()
    cur.execute("SELECT status, count(*) AS n FROM dealer_applications GROUP BY status")
    counts = {r['status']: r['n'] for r in cur.fetchall()}
    db.close()
    # Recompute the roster match + unified transaction count LIVE per row so the
    # queue badge is current (the stored lsl_history is deals-only + goes stale
    # as new purchases land). Only for existing/matched dealers — a genuinely
    # new applicant has nothing to look up. tx_count = cars bought + cars sold.
    for r in rows:
        r['tx_count'] = r['bought_cars'] = r['sold_cars'] = r['total_gross'] = 0
        if not (r.get('is_existing') or r.get('name_match')):
            continue                      # genuinely-new applicant — nothing to look up
        m = _roster_match(r['dealership_name'], r.get('contact_phone'))
        h = _lsl_history_person(r['dealership_name'], m, r.get('contact_name'), r.get('contact_phone')) or {}
        # DIRECTION_SPLIT_2026_07_28 — the queue shows both legs + what EW made
        r['tx_count'] = h.get('tx_count') or 0
        r['bought_cars'] = h.get('bought_cars') or 0
        r['sold_cars'] = h.get('sold_cars') or 0
        r['total_gross'] = h.get('total_gross') or 0
        if m:
            r['name_match'] = m
    return render_template('network/applications.html', rows=rows, counts=counts,
                           types=DEALER_TYPES, class_labels=CLASS_LABELS)


@bp.route('/network/pending-count')
def network_pending_count():
    """Live count for the dashboard nav chip (DP_LIVE_CHIP_2026_07_30).

    Mirrors /api/recon/new-count. Deliberately under /network/ (login-gated),
    not /api/dealerprice/ which is a PUBLIC prefix for the site bridge.
    Reuses the same 15s-cached helper the Jinja global uses, so polling every
    20s from N open dashboards cannot turn into N queries per second.
    """
    try:
        n = _inject_dp_network()['dealer_apps_pending']()
    except Exception as e:
        print('[dp-network] pending-count: %s' % e, flush=True)
        n = 0
    return jsonify(count=n)


@bp.route('/network/members')
def network_members():
    """Onboarded-dealer roster + their bid activity (the per-dealer tracking)."""
    db = _db(); cur = db.cursor()
    try:
        cur.execute("""SELECT m.*,
                          (SELECT count(*) FROM bids b WHERE b.dp_member_id=m.id) AS bid_count,
                          (SELECT max(b.created_at) FROM bids b WHERE b.dp_member_id=m.id) AS last_bid
                         FROM dealerprice_members m
                        ORDER BY m.approved_at DESC LIMIT 500""")
        rows = cur.fetchall()
    except Exception as e:
        print('[dp-network] members list: %s' % e, flush=True)
        cur.execute("SELECT m.*, 0 AS bid_count, NULL AS last_bid FROM dealerprice_members m ORDER BY approved_at DESC LIMIT 500")
        rows = cur.fetchall()
    db.close()
    return render_template('network/members.html', rows=rows)


@bp.route('/network/application/<int:app_id>')
def network_application(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("SELECT * FROM dealer_applications WHERE id=%s", (app_id,))
    a = cur.fetchone()
    if not a:
        db.close(); abort(404)
    member = None
    rep_history = []
    if a.get('member_id'):
        cur.execute("SELECT * FROM dealerprice_members WHERE id=%s", (a['member_id'],))
        member = cur.fetchone()
        if member:
            try:
                cur.execute("""SELECT rep, prev_rep, assigned_by, assigned_at
                                 FROM dealerprice_rep_assignments
                                WHERE member_id=%s ORDER BY id DESC LIMIT 6""",
                            (member['id'],))
                rep_history = [dict(r) for r in cur.fetchall()]
            except Exception as e:
                print('[dp-network] rep history: %s' % e, flush=True)
    db.close()
    member_bids = _member_bids(member['id']) if member else []
    # Re-run the match live so matcher improvements apply retroactively to old
    # applications without a backfill migration; then feed the resolved
    # supplier_id into the history lookup so it can read the payments ledger.
    a['name_match'] = _roster_match(a.get('dealership_name'), a.get('contact_phone')) or a.get('name_match')
    lsl_hist = _lsl_history_person(a.get('dealership_name'), a.get('name_match'), a.get('contact_name'), a.get('contact_phone'))
    return render_template('network/application.html', a=a, member=member, member_bids=member_bids,
                           approvers=DP_APPROVERS,
                           approve_err=request.args.get('err'),
                           decline_live=os.path.exists(DP_DECLINE_GATE),
                           sales_reps=DP_SALES_REPS,
                           rep_history=rep_history,
                           class_labels=CLASS_LABELS, lsl_hist=lsl_hist)


@bp.route('/network/application/<int:app_id>/doc/<which>')
def network_application_doc(app_id, which):
    """Serve the PRIVATE license / tax-id file. Operator-only (require_login)."""
    if which not in ('license', 'taxid'):
        abort(404)
    db = _db(); cur = db.cursor()
    cur.execute("SELECT license_doc_path, taxid_doc_path FROM dealer_applications WHERE id=%s", (app_id,))
    r = cur.fetchone(); db.close()
    if not r:
        abort(404)
    path = r['license_doc_path'] if which == 'license' else r['taxid_doc_path']
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype=_doc_mime(path),
                     download_name='%s-%s.%s' % (which, app_id, path.rsplit('.', 1)[-1]))


@bp.route('/network/application/<int:app_id>/approve', methods=['POST'])
def network_application_approve(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("SELECT * FROM dealer_applications WHERE id=%s", (app_id,))
    a = cur.fetchone()
    if not a:
        db.close(); abort(404)
    # Already decided either way? The Decision card is hidden once a decision
    # exists, but the endpoint has to enforce it too - a stale tab is still a
    # live form. Reversing a decline is deliberately not self-serve.
    if a.get('member_id') or a.get('status') == 'rejected':
        db.close()
        return redirect(url_for('dealerprice_network.network_application', app_id=app_id))
    # DP_APPROVER_2026_07_29: an approval MUST say who made it. The browser also
    # blocks this, but client-side validation is a courtesy, not a control - a
    # stale tab or a disabled-JS browser would otherwise get silently recorded
    # as 'operator', which is exactly the ambiguity the picker exists to remove.
    who = (request.form.get('approved_by') or '').strip()
    if who not in DP_APPROVERS:
        db.close()
        return redirect(url_for('dealerprice_network.network_application',
                                app_id=app_id, err='pick'))
    # Optional owner chosen on the approve row. Silently ignored if it is not a
    # known rep - a bad value must not block the approval itself.
    rep = (request.form.get('sales_rep') or '').strip()
    if rep not in DP_SALES_REPS:
        rep = None
    from psycopg2.extras import Json
    # hex token (no -/_): survives SMS auto-linkifiers + looks like a secure key
    token = secrets.token_hex(16)
    try:
        cur.execute("""INSERT INTO dealerprice_members
                         (application_id, dealership_name, contact_name, contact_email,
                          contact_phone, token, is_existing, lsl_match, approved_by, intent,
                          sales_rep, sales_rep_assigned_by, sales_rep_assigned_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               CASE WHEN %s IS NULL THEN NULL ELSE now() END)
                       RETURNING id""",
                    (app_id, a['dealership_name'], a['contact_name'], a['contact_email'],
                     a['contact_phone'], token, a['is_existing'],
                     Json(a.get('name_match') or None), _reviewer(),
                     a.get('intent'), rep, (who if rep else None), rep))
        member_id = cur.fetchone()['id']
        if rep:
            cur.execute("""INSERT INTO dealerprice_rep_assignments
                             (member_id, rep, prev_rep, assigned_by)
                           VALUES (%s,%s,NULL,%s)""", (member_id, rep, who))
        cur.execute("""UPDATE dealer_applications SET status='approved', member_id=%s,
                          reviewer=%s, reviewed_at=now(),
                          review_notes=COALESCE(%s, review_notes) WHERE id=%s""",
                    (member_id, _reviewer(), _s(request.form.get('review_notes')) or None, app_id))
        db.commit()
        cur.execute("SELECT * FROM dealerprice_members WHERE id=%s", (member_id,))
        m = cur.fetchone()
    except Exception as e:
        db.rollback(); db.close()
        print('[dp-network] approve: %s' % e, flush=True)
        abort(500)
    db.close()
    try:
        _invite_member(m)
    except Exception as _e_inv:
        print('[dp-network] invite: %s' % _e_inv, flush=True)
    # DP_INTENT_2026_07_29: a BUYER goes into the scanner fleet once vetted.
    # Wrapped so a scanner problem can never fail an approval.
    try:
        _dp_register_scanner(app_id, a, m['id'])
    except Exception as e:
        print('[dp-network] approve invite: %s' % e, flush=True)
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


# DP_DECLINE_2026_07_29 — the decline letter goes to the DEALER. Gated OFF by
# default because it is outbound to a real business: while DP_DECLINE_LIVE is
# absent the decline still records normally and the letter is only logged, so
# the copy can be reviewed before anything leaves the building.
#   /opt/expwholesale/DP_DECLINE_LIVE   present -> letter sends
# Under DP_REHEARSAL the letter goes to the partners instead of the dealer, so
# the team can read exactly what an applicant receives.
DP_DECLINE_GATE = os.environ.get('DP_DECLINE_GATE',
                                 '/opt/expwholesale/DP_DECLINE_LIVE')


def _dp_decline_letter(a):
    """The applicant's decline letter. Same shell as the welcome email
    (table-based, inline CSS, 600px, logo off the Resend-verified host) so both
    read as the same company.

    Deliberately gives NO reason. The internal review note is internal: it is
    the team's shorthand, it invites an argument nobody wants to have, and on a
    credit-adjacent decision a stated reason is a liability. Short, courteous,
    door left open.
    """
    logo = 'https://experience-wholesale.net/static/ew-logo-email.png'
    first = (_s(a.get('contact_name')) or '').split(' ')[0]
    greet = ('Hi %s,' % first) if first else 'Hello,'
    dealership = _s(a.get('dealership_name')) or 'your dealership'
    subject = 'Your Experience Wholesale dealer network application'
    html = """\
<!--[if mso]><style>body,table,td{font-family:Arial,sans-serif !important}</style><![endif]-->
<div style="background:#f4f5f7;padding:28px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb">

  <tr><td align="center" style="padding:30px 30px 20px 30px;border-bottom:1px solid #eef0f3">
    <img src="%(logo)s" width="240" alt="Experience Wholesale"
         style="display:block;width:240px;max-width:70%%;height:auto;border:0">
  </td></tr>

  <tr><td style="padding:30px 34px 34px 34px">
    <p style="margin:0 0 16px 0;font-size:15px;line-height:1.62;color:#0f172a">%(greet)s</p>
    <p style="margin:0 0 16px 0;font-size:15px;line-height:1.62;color:#475569">
      Thank you for your interest in the Experience Wholesale dealer network, and for
      taking the time to apply on behalf of <b style="color:#0f172a">%(dealership)s</b>.
    </p>
    <p style="margin:0;font-size:15px;line-height:1.62;color:#0f172a">
      Unfortunately, at this time we are not able to take on your account.
    </p>
    <p style="margin:26px 0 0 0;font-size:15px;line-height:1.62;color:#0f172a">
      &mdash; The Experience Wholesale Team
    </p>
  </td></tr>

</table></div>""" % {'logo': logo, 'greet': greet, 'dealership': dealership}
    return subject, html


def _dp_decline_notify(a, who, reason):
    """Send the applicant their decline letter. Best-effort: a mail problem must
    never undo a decline that is already committed."""
    subject, html = _dp_decline_letter(a)
    name = _s(a.get('dealership_name')) or '(no name)'
    to_addr = _s(a.get('contact_email'))

    if _dp_rehearsing():
        to_addr = _dp_rehearsal_email()    # team (or operator alone); never the applicant
        subject = '[rehearsal] ' + subject

    if not os.path.exists(DP_DECLINE_GATE):
        print('[dp-network:DECLINE] #%s %s declined by %s (reason=%r) - letter '
              'NOT sent, DP_DECLINE_LIVE gate is off. Would have gone to: %s'
              % (a.get('id'), name, who, reason or '', to_addr or '(no email)'),
              flush=True)
        return False
    if not to_addr:
        print('[dp-network:DECLINE] #%s %s has no contact_email - no letter'
              % (a.get('id'), name), flush=True)
        return False
    print('[dp-network:DECLINE] #%s %s declined by %s - letter -> %s'
          % (a.get('id'), name, who, to_addr), flush=True)
    return _email(to_addr, subject, html)


# DP_SALES_REP_2026_07_29 — once a dealer is approved, somebody owns the
# relationship. Unlike approve/decline this is deliberately REVERSIBLE: accounts
# move between reps, so the control stays on the page and every move is logged
# to dealerprice_rep_assignments rather than overwriting history.
DP_SALES_REPS = [r.strip() for r in os.environ.get(
    'DP_SALES_REPS',
    # Joe added 2026-07-30 (operator): he owns accounts too, and was the only
    # one of the three partners missing here while already in DP_APPROVERS.
    # Ordered to match DP_APPROVERS so the partners read Joe/Todd/Gregg in both.
    'Walt,SteveK,Jordan,Patty,Alan,Jenny,Sam B,Joe,Todd,Gregg').split(',') if r.strip()]


@bp.route('/network/application/<int:app_id>/assign-rep', methods=['POST'])
def network_application_assign_rep(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("SELECT id, sales_rep FROM dealerprice_members WHERE application_id=%s",
                (app_id,))
    m = cur.fetchone()
    if not m:
        # No member yet means the dealer is not approved - nothing to assign.
        db.close()
        return redirect(url_for('dealerprice_network.network_application', app_id=app_id))
    rep = (request.form.get('sales_rep') or '').strip()
    # '' is a legitimate value: it unassigns. Anything else must be a real rep.
    if rep and rep not in DP_SALES_REPS:
        db.close()
        return redirect(url_for('dealerprice_network.network_application',
                                app_id=app_id, err='rep'))
    prev = m.get('sales_rep')
    if rep == (prev or ''):
        db.close()
        return redirect(url_for('dealerprice_network.network_application', app_id=app_id))
    by = _reviewer()
    try:
        cur.execute("""UPDATE dealerprice_members
                          SET sales_rep=%s, sales_rep_assigned_by=%s,
                              sales_rep_assigned_at=now()
                        WHERE id=%s""", (rep or None, by, m['id']))
        cur.execute("""INSERT INTO dealerprice_rep_assignments
                         (member_id, rep, prev_rep, assigned_by)
                       VALUES (%s,%s,%s,%s)""", (m['id'], rep or None, prev, by))
        db.commit()
        print('[dp-network] rep: member %s %r -> %r by %s'
              % (m['id'], prev, rep, by), flush=True)
    except Exception as e:
        db.rollback()
        print('[dp-network] assign-rep: %s' % e, flush=True)
    db.close()
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


@bp.route('/network/application/<int:app_id>/reject', methods=['POST'])
def network_application_reject(app_id):
    """Decline an application. Mirrors approve: it must be signed by a named
    partner, and it cannot be clicked twice or clicked on something already
    decided. Nothing is sent to the dealer."""
    db = _db(); cur = db.cursor()
    cur.execute("SELECT * FROM dealer_applications WHERE id=%s", (app_id,))
    a = cur.fetchone()
    if not a:
        db.close(); abort(404)
    # Already decided? Do not let a decline overwrite an approval, and do not
    # re-fire the notification on a double click.
    if a.get('member_id') or a.get('status') == 'rejected':
        db.close()
        return redirect(url_for('dealerprice_network.network_application', app_id=app_id))
    # DP_APPROVER_2026_07_29: same rule as approve - say who you are.
    who = (request.form.get('approved_by') or '').strip()
    if who not in DP_APPROVERS:
        db.close()
        return redirect(url_for('dealerprice_network.network_application',
                                app_id=app_id, err='pick'))
    reason = _s(request.form.get('review_notes')) or None
    try:
        cur.execute("""UPDATE dealer_applications SET status='rejected', reviewer=%s,
                          reviewed_at=now(), review_notes=COALESCE(%s, review_notes)
                        WHERE id=%s""", (who, reason, app_id))
        db.commit()
    except Exception as e:
        db.rollback(); db.close()
        print('[dp-network] decline: %s' % e, flush=True)
        abort(500)
    db.close()
    try:
        _dp_decline_notify(a, who, reason)
    except Exception as e:
        print('[dp-network] decline notify: %s' % e, flush=True)
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


@bp.route('/network/application/<int:app_id>/needs-info', methods=['POST'])
def network_application_needs_info(app_id):
    db = _db(); cur = db.cursor()
    cur.execute("""UPDATE dealer_applications SET status='needs_info', reviewer=%s,
                      reviewed_at=now(), review_notes=COALESCE(%s, review_notes)
                    WHERE id=%s""",
                (_reviewer(), _s(request.form.get('review_notes')) or None, app_id))
    db.commit(); db.close()
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


@bp.route('/network/application/<int:app_id>/delete', methods=['POST'])
def network_application_delete(app_id):
    """Hard-delete an application from the review queue (operator ask 2026-07-28).

    Safety rules this deliberately follows:
      • If the application had been approved and provisioned a member, the
        MEMBER ROW IS NOT DELETED. bids carry bids.dp_member_id, and dropping
        the member would orphan that tagging on live bids (HR1 — never reach
        into the bid path). Instead the member is REVOKED: status='revoked' and
        the token is replaced with a dead tombstone, so their magic link stops
        working immediately while historical bid attribution survives intact.
      • The private license / Tax-ID images are removed from disk — they are
        PII and there is no reason to keep them once the record is gone.
      • Deletion is irreversible, so it is announced to Telegram for the audit
        trail. dealerprice_person_links is left alone: it is keyed on supplier
        ids and is shared by every application for that dealer.
    """
    db = _db(); cur = db.cursor()
    cur.execute("""SELECT id, dealership_name, contact_name, status, member_id,
                          license_doc_path, taxid_doc_path
                     FROM dealer_applications WHERE id=%s""", (app_id,))
    a = cur.fetchone()
    if not a:
        db.close(); abort(404)

    # Members provisioned from this application. Look them up by application_id
    # — that FK (dealerprice_members_application_id_fkey, ON DELETE NO ACTION)
    # is what blocks the DELETE, and a member can reference the application even
    # when dealer_applications.member_id was never written back. Union both
    # directions so neither an orphan nor a back-pointer is missed.
    cur.execute("SELECT id FROM dealerprice_members WHERE application_id=%s", (app_id,))
    mids = set(r['id'] for r in cur.fetchall())
    if a.get('member_id'):
        mids.add(a['member_id'])

    revoked = []
    try:
        for mid in sorted(mids):
            # Revoke, and clear application_id so the FK stops blocking the
            # DELETE. The member ROW survives: bids.dp_member_id points at it and
            # dropping it would orphan bid attribution (HR1 — stay out of bids).
            cur.execute("""UPDATE dealerprice_members
                              SET status='revoked',
                                  token=%s,
                                  application_id=NULL
                            WHERE id=%s""",
                        ('revoked-%d-%s' % (mid, secrets.token_urlsafe(8)), mid))
            if cur.rowcount:
                revoked.append(mid)
        cur.execute("DELETE FROM dealer_applications WHERE id=%s", (app_id,))
        db.commit()
    except Exception as e:
        db.rollback(); db.close()
        print('[dp-network] delete app %s failed: %s' % (app_id, e), flush=True)
        return ("Could not delete application #%d — %s. Nothing was changed."
                % (app_id, e), 500)
    db.close()

    # private docs (PII) — remove the per-application directory
    for p in (a.get('license_doc_path'), a.get('taxid_doc_path')):
        try:
            if p and os.path.isfile(p):
                os.remove(p)
        except Exception as e:
            print('[dp-network] delete: doc unlink %s: %s' % (p, e), flush=True)
    try:
        d = os.path.join(PRIV_DOC_ROOT, str(app_id))
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    except Exception as e:
        print('[dp-network] delete: docdir %s' % e, flush=True)

    try:
        _tg('🗑 <b>Dealer-Network application DELETED</b> #%d — %s (%s)\nwas: %s%s\nby %s'
            % (app_id, _s(a.get('dealership_name')) or '?', _s(a.get('contact_name')) or '?',
               _s(a.get('status')) or '?',
               (' · member #%s REVOKED' % ', #'.join(str(m) for m in revoked)) if revoked else '',
               _reviewer()))
    except Exception as e:
        # the delete already succeeded — a failed alert must not 500 the operator
        print('[dp-network] delete: tg alert failed: %s' % e, flush=True)
    return redirect(url_for('dealerprice_network.network_applications'))


@bp.route('/network/application/<int:app_id>/classify', methods=['POST'])
def network_application_classify(app_id):
    """Operator override of the auto-classification."""
    c = _s(request.form.get('classification'))
    if c not in CLASS_LABELS:
        c = None
    db = _db(); cur = db.cursor()
    cur.execute("UPDATE dealer_applications SET classification=%s WHERE id=%s", (c, app_id))
    db.commit(); db.close()
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))



@bp.route('/network/application/<int:app_id>/person-link', methods=['POST'])
def network_application_person_link(app_id):
    """One-click confirm / reject of a weak person-match so the merge decision
    STICKS across views (PERSON_MERGE_2026_07_21). decision = confirmed | rejected
    | reset. Session-gated with the rest of /network/. Read-write on EW PG only —
    never touches LSL."""
    try:
        primary_sid = int(request.form.get('primary_sid') or 0)
        linked_sid = int(request.form.get('linked_sid') or 0)
    except (TypeError, ValueError):
        primary_sid = linked_sid = 0
    decision = _s(request.form.get('decision'))
    if primary_sid and linked_sid and decision in ('confirmed', 'rejected', 'reset'):
        db = _db(); cur = db.cursor()
        try:
            if decision == 'reset':
                cur.execute("DELETE FROM dealerprice_person_links "
                            "WHERE primary_sid=%s AND linked_sid=%s",
                            (primary_sid, linked_sid))
            else:
                cur.execute(
                    "INSERT INTO dealerprice_person_links "
                    "(primary_sid, linked_sid, decision, linked_name, decided_by) "
                    "VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (primary_sid, linked_sid) DO UPDATE SET "
                    "decision=EXCLUDED.decision, decided_by=EXCLUDED.decided_by, "
                    "decided_at=now()",
                    (primary_sid, linked_sid, decision,
                     _s(request.form.get('linked_name')), _reviewer()))
            db.commit()
        except Exception as e:
            print('[dp-network] person_link: %s' % e, flush=True)
        finally:
            db.close()
    return redirect(url_for('dealerprice_network.network_application', app_id=app_id))


# ── member token lookup / per-dealer bids ───────────────────────────────────
def _member_by_token(token, touch=False, count_submit=False):
    """Active member row for a token, or None. touch=update last_used_at;
    count_submit=also bump submit_count (used on the /bid path)."""
    token = _s(token)
    if not token:
        return None
    db = _db(); cur = db.cursor()
    try:
        cur.execute("SELECT * FROM dealerprice_members WHERE token=%s AND status='active' LIMIT 1", (token,))
        m = cur.fetchone()
        if m and (touch or count_submit):
            if count_submit:
                cur.execute("UPDATE dealerprice_members SET last_used_at=now(), submit_count=submit_count+1 WHERE id=%s", (m['id'],))
            else:
                cur.execute("UPDATE dealerprice_members SET last_used_at=now() WHERE id=%s", (m['id'],))
            db.commit()
        return m
    except Exception as e:
        print('[dp-network] member lookup: %s' % e, flush=True)
        return None
    finally:
        db.close()


def validate_member_token(token):
    """For the /bid path: validate the token + count a submit. Returns row|None."""
    return _member_by_token(token, count_submit=True)


def _member_bids(member_id, limit=200):
    """All EW bids tagged to this network member (newest first)."""
    db = _db(); cur = db.cursor()
    try:
        cur.execute("""SELECT id, year, make, model, trim, mileage, status, ai_price, created_at
                         FROM bids WHERE dp_member_id=%s ORDER BY id DESC LIMIT %s""", (member_id, limit))
        return cur.fetchall()
    except Exception as e:
        print('[dp-network] member_bids: %s' % e, flush=True)
        return []
    finally:
        db.close()


@bp.route('/api/dealerprice/member', methods=['GET', 'POST'])
def api_dp_member():
    """Validate a member token -> member info, for the /access magic link and
    the pre-filled Get-a-Bid form. Shared-secret; never exposes the token."""
    r = _bad_secret()
    if r:
        return r
    token = request.args.get('token') or (request.get_json(silent=True) or {}).get('token')
    m = _member_by_token(token, touch=True)
    if not m:
        return jsonify({'ok': False})
    return jsonify({'ok': True, 'member': {
        'member_id': m['id'],
        'dealership_name': m['dealership_name'],
        'contact_name': m['contact_name'],
        'contact_email': m['contact_email'],
        'contact_phone': m['contact_phone'],
    }})


# ═════════════════════════════════════════════════════════════════════════════
# DP_OUTREACH_TRACKING_2026_07_30 — open / click / bounce tracking, self-hosted.
#
# WHY SELF-HOSTED: the Resend API key is send-only (403 on /domains), so vendor
# open/click tracking and webhooks cannot be turned on programmatically. Doing it
# ourselves also puts the events straight into EW's own Postgres, so the tab is a
# plain SQL read with no vendor dependency. The ONE thing we cannot see without
# Resend is bounces/complaints — those arrive on the webhook below.
#
# Everything here is deliberately fail-open and fast: a tracking failure must
# never cost a dealer their click. The pixel always returns a GIF, the click
# always redirects.
# ═════════════════════════════════════════════════════════════════════════════
import base64 as _b64
import hashlib as _hashlib
import hmac as _hmac

# 1x1 transparent GIF.
_PIXEL = _b64.b64decode(b'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')

# Machine fetchers. An "open" from one of these is a PRE-FETCH, not a human:
# Apple Mail Privacy Protection loads every image for every recipient whether or
# not they opened it, which is why a blended open-rate is a fiction. Counted
# separately so the dashboard can lead with clicks and show opens honestly.
_PROXY_UA = ('googleimageproxy', 'yahoomailproxy', 'yandexmail',
             'microsoft office', 'msoffice', 'skypeuripreview',
             'proofpoint', 'barracuda', 'mimecast', 'symantec')


def _dpt_secret():
    """Signing key for click links. Falls back to the app secret."""
    return (os.environ.get('SECRET_KEY') or 'expwholesale2026secret!').encode()


def _dpt_sign(payload):
    return _hmac.new(_dpt_secret(), payload.encode(), _hashlib.sha256).hexdigest()[:16]


def _dpt_wrap(token, url):
    """Build a signed tracked link. UNSIGNED WOULD BE AN OPEN REDIRECT — anyone
    could hand out experience-wholesale.net links that bounce to a phishing page,
    borrowing our domain's reputation. The signature makes the destination
    tamper-proof."""
    enc = _b64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
    return '%s/e/c/%s?u=%s&s=%s' % (DP_TRACK_BASE.rstrip('/'), token, enc,
                                    _dpt_sign(token + enc))


def _dpt_unwrap(token, enc, sig):
    """Verify + decode. Returns None on any tampering."""
    if not enc or not sig:
        return None
    if not _hmac.compare_digest(sig, _dpt_sign(token + enc)):
        print('[dp-track] BAD SIGNATURE on click token=%s' % token, flush=True)
        return None
    try:
        pad = '=' * (-len(enc) % 4)
        url = _b64.urlsafe_b64decode(enc + pad).decode()
    except Exception:
        return None
    # even signed, only ever redirect to our own properties
    if not re.match(r'^https?://([a-z0-9-]+\.)*(experience-wholesale\.net|dealerprice\.net)(/|$)',
                    url, re.I):
        print('[dp-track] refused off-domain redirect: %r' % url[:120], flush=True)
        return None
    return url


DP_TRACK_BASE = os.environ.get('DP_TRACK_BASE', 'https://experience-wholesale.net')


def _dpt_is_proxy(ua):
    ua = (ua or '').lower()
    return any(p in ua for p in _PROXY_UA)


def _dpt_client_ip():
    for h in ('CF-Connecting-IP', 'X-Real-IP', 'X-Forwarded-For'):
        v = (request.headers.get(h) or '').split(',')[0].strip()
        if v:
            return v[:64]
    return (request.remote_addr or '')[:64]


def _dpt_row(token):
    """Look up a send by its opaque token. Returns (db, cur, row) or (None,)*3."""
    try:
        db = _db(); cur = db.cursor()
        cur.execute("SELECT * FROM dp_outreach_email WHERE token=%s", (token,))
        r = cur.fetchone()
        if not r:
            db.close(); return None, None, None
        return db, cur, r
    except Exception as e:
        print('[dp-track] lookup %s: %s' % (token, e), flush=True)
        return None, None, None


def _dpt_event(cur, email_id, kind, url=None, source=None):
    cur.execute("""INSERT INTO dp_outreach_event (email_id, kind, url, ip, user_agent, source)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (email_id, kind, url, _dpt_client_ip(),
                 (request.headers.get('User-Agent') or '')[:400], source))


@bp.route('/e/o/<token>.gif')
def dpt_open(token):
    """Tracking pixel. ALWAYS returns a GIF — a tracking problem must never show
    a broken image in a dealer's email."""
    try:
        db, cur, r = _dpt_row(token)
        if r:
            proxy = _dpt_is_proxy(request.headers.get('User-Agent'))
            if proxy:
                cur.execute("""UPDATE dp_outreach_email
                                  SET proxy_opens = proxy_opens + 1
                                WHERE id=%s""", (r['id'],))
                _dpt_event(cur, r['id'], 'proxy_open', source='pixel')
            else:
                cur.execute("""UPDATE dp_outreach_email
                                  SET opens = opens + 1,
                                      first_open_at = COALESCE(first_open_at, now()),
                                      last_open_at = now()
                                WHERE id=%s""", (r['id'],))
                _dpt_event(cur, r['id'], 'open', source='pixel')
            db.commit(); db.close()
    except Exception as e:
        print('[dp-track] open %s: %s' % (token, e), flush=True)
    resp = current_app.response_class(_PIXEL, mimetype='image/gif')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@bp.route('/e/c/<token>')
def dpt_click(token):
    """Signed click-through. Records the click, then redirects."""
    url = _dpt_unwrap(token, request.args.get('u'), request.args.get('s'))
    if not url:
        return redirect(DP_TRACK_BASE)
    try:
        db, cur, r = _dpt_row(token)
        if r:
            cur.execute("""UPDATE dp_outreach_email
                              SET clicks = clicks + 1,
                                  first_click_at = COALESCE(first_click_at, now()),
                                  last_click_at = now(),
                                  -- a click proves a human, so it also settles
                                  -- the open question for this contact
                                  opens = GREATEST(opens, 1),
                                  first_open_at = COALESCE(first_open_at, now())
                            WHERE id=%s""", (r['id'],))
            _dpt_event(cur, r['id'], 'click', url=url, source='link')
            db.commit(); db.close()
    except Exception as e:
        print('[dp-track] click %s: %s' % (token, e), flush=True)
    return redirect(url)


@bp.route('/e/u/<token>')
def dpt_unsubscribe(token):
    """One-click unsubscribe. Required for CAN-SPAM and materially helps
    deliverability; mailbox providers favour senders that honour List-Unsubscribe.
    Suppression is written to its own table so it OUTLIVES any rebuild of the
    target list."""
    done = False
    try:
        db, cur, r = _dpt_row(token)
        if r:
            cur.execute("""UPDATE dp_outreach_email SET unsubscribed_at=now() WHERE id=%s""",
                        (r['id'],))
            cur.execute("""INSERT INTO dp_outreach_suppression (email, reason)
                           VALUES (%s,'unsubscribed') ON CONFLICT (email) DO NOTHING""",
                        ((r['email'] or '').lower(),))
            cur.execute("""UPDATE dp_outreach_targets SET status='unsubscribed'
                            WHERE lower(email)=%s""", ((r['email'] or '').lower(),))
            _dpt_event(cur, r['id'], 'unsubscribe', source='link')
            db.commit(); db.close(); done = True
    except Exception as e:
        print('[dp-track] unsub %s: %s' % (token, e), flush=True)
    return current_app.response_class(
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<div style="font:16px/1.6 -apple-system,Segoe UI,Arial;max-width:520px;'
        'margin:16vh auto;padding:0 22px;text-align:center;color:#1f2937">'
        '<h2 style="margin:0 0 10px">%s</h2><p style="color:#6b7280">%s</p></div>'
        % ('You’re unsubscribed' if done else 'Link not recognised',
           'You won’t receive further emails from Experience Wholesale about DealerPrice.'
           if done else 'We couldn’t match that link, so nothing was changed.'),
        mimetype='text/html')


@bp.route('/e/webhook/resend', methods=['POST'])
def dpt_resend_webhook():
    """Delivery / bounce / complaint events from Resend.

    This is the ONLY source for bounces — a pixel cannot see them. Auth is a
    shared secret in the URL (?k=) because the key we hold cannot create a
    properly-signed webhook via the API.
    """
    if (request.args.get('k') or '') != os.environ.get('DP_WEBHOOK_KEY', ''):
        return jsonify(ok=False), 403
    d = request.get_json(silent=True) or {}
    ev = (d.get('type') or '').strip()
    data = d.get('data') or {}
    mid = data.get('email_id') or data.get('id')
    to = data.get('to')
    if isinstance(to, list):
        to = to[0] if to else None
    try:
        db = _db(); cur = db.cursor()
        cur.execute("""SELECT * FROM dp_outreach_email
                        WHERE (provider_id=%s AND %s<>'')
                           OR (lower(email)=lower(%s) AND provider_id IS NULL)
                     ORDER BY id DESC LIMIT 1""", (mid, mid or '', to or ''))
        r = cur.fetchone()
        if not r:
            db.close()
            print('[dp-track] webhook %s: no match (mid=%s to=%s)' % (ev, mid, to), flush=True)
            return jsonify(ok=True, matched=False)
        eid = r['id']
        if ev == 'email.delivered':
            cur.execute("UPDATE dp_outreach_email SET status='delivered', delivered_at=now() "
                        "WHERE id=%s AND status NOT IN ('bounced','complained')", (eid,))
            _dpt_event(cur, eid, 'delivered', source='resend')
        elif ev in ('email.bounced', 'email.delivery_delayed'):
            hard = 'hard' if ev == 'email.bounced' else 'soft'
            cur.execute("UPDATE dp_outreach_email SET status='bounced', bounced_at=now(), "
                        "bounce_type=%s WHERE id=%s", (hard, eid))
            _dpt_event(cur, eid, 'bounce', source='resend')
            if hard == 'hard':
                cur.execute("INSERT INTO dp_outreach_suppression (email, reason) "
                            "VALUES (%s,'hard_bounce') ON CONFLICT (email) DO NOTHING",
                            ((r['email'] or '').lower(),))
        elif ev == 'email.complained':
            cur.execute("UPDATE dp_outreach_email SET status='complained', complained_at=now() "
                        "WHERE id=%s", (eid,))
            _dpt_event(cur, eid, 'complaint', source='resend')
            cur.execute("INSERT INTO dp_outreach_suppression (email, reason) "
                        "VALUES (%s,'complaint') ON CONFLICT (email) DO NOTHING",
                        ((r['email'] or '').lower(),))
        db.commit(); db.close()
    except Exception as e:
        print('[dp-track] webhook %s: %s' % (ev, e), flush=True)
    return jsonify(ok=True)


# ═════════════════════════════════════════════════════════════════════════════
# DP_OUTREACH_TAB_2026_07_30 — the Outreach tab in /network/*.
#
# Operator's ask: "see all emails sent, how many bounced, how many were opened,
# how many clicked around and how many have applied ... dynamic and always
# polling the info every so often."
#
# The unit of a row is an ADDRESS, not a dealership: 1,022 emailable dealerships
# collapse to 809 inboxes and one address carries 35 stores, so per-dealership
# sending would put 35 near-identical emails in one person's morning.
# ═════════════════════════════════════════════════════════════════════════════


def _dpo_stats(cur):
    """Everything the header tiles need, in one pass."""
    cur.execute("""
        SELECT
          -- REMOVED_NOT_COUNTED_2026_07_30: these MUST exclude removed rows.
          -- They are what a reviewer watches to confirm a removal registered;
          -- counting every row made the button look broken.
          (SELECT count(*) FROM dp_outreach_targets WHERE removed_at IS NULL)     AS targets,
          (SELECT coalesce(sum(store_count),0) FROM dp_outreach_targets
            WHERE removed_at IS NULL)                                             AS dealerships,
          (SELECT count(*) FROM dp_outreach_targets WHERE removed_at IS NOT NULL) AS removed,
          count(*)                                                                AS sent,
          count(*) FILTER (WHERE status='delivered')                              AS delivered,
          count(*) FILTER (WHERE status='bounced')                                AS bounced,
          count(*) FILTER (WHERE status='complained')                             AS complained,
          count(*) FILTER (WHERE status='failed')                                 AS failed,
          count(*) FILTER (WHERE opens > 0)                                       AS opened,
          count(*) FILTER (WHERE opens = 0 AND proxy_opens > 0)                   AS proxy_only,
          count(*) FILTER (WHERE clicks > 0)                                      AS clicked,
          count(*) FILTER (WHERE unsubscribed_at IS NOT NULL)                     AS unsubscribed,
          count(*) FILTER (WHERE applied_at IS NOT NULL)                          AS applied
        FROM dp_outreach_email
    """)
    s = dict(cur.fetchone() or {})
    sent = s.get('sent') or 0
    s['pending'] = (s.get('targets') or 0) - sent
    # Rates are quoted against DELIVERED, not sent — an open rate that silently
    # includes bounced mail flatters itself.
    base = (s.get('delivered') or 0) or sent
    s['open_pct'] = round(100.0 * (s.get('opened') or 0) / base, 1) if base else 0.0
    s['click_pct'] = round(100.0 * (s.get('clicked') or 0) / base, 1) if base else 0.0
    s['apply_pct'] = round(100.0 * (s.get('applied') or 0) / base, 1) if base else 0.0
    s['bounce_pct'] = round(100.0 * (s.get('bounced') or 0) / sent, 1) if sent else 0.0
    return s


def _dpo_link_applications(cur):
    """Attribute applications back to the campaign.

    Matched on the contact email. Deliberately conservative — an application is
    only credited when its address is one we actually emailed. A dealer who
    applies from a different address than the one we mailed will not be counted
    here, which understates rather than overstates.
    """
    cur.execute("""
        UPDATE dp_outreach_email e
           SET applied_at = a.created_at, application_id = a.id
          FROM dealer_applications a
         WHERE lower(a.contact_email) = lower(e.email)
           AND e.applied_at IS NULL
           AND e.sent_at IS NOT NULL
           AND a.created_at >= e.sent_at
    """)
    return cur.rowcount


@bp.route('/network/outreach')
def network_outreach():
    # QUIET_LIVE_2026_07_30 — the clocks came from a snapshot and went stale;
    # refresh them from LSL (cached 10 min) before anything is read.
    _dpo_refresh_quiet()
    db = _db(); cur = db.cursor()
    try:
        _dpo_link_applications(cur)
        db.commit()
    except Exception as e:
        db.rollback()
        print('[dp-outreach] link applications: %s' % e, flush=True)
    stats = _dpo_stats(cur)
    cur.execute("""SELECT subject, body, updated_at FROM dp_outreach_template WHERE id=1""")
    tpl = cur.fetchone()
    cur.execute("""
        SELECT t.id, t.name, t.email, t.phone, t.store_count, t.stores,
               t.total_profit, t.src_profit, t.buy_profit,
               t.src_deals, t.buy_deals, t.days_since, t.sold_days,
               t.email_original, t.email_edited_by, t.last_deal_at,
               t.status AS target_status,
               e.id AS email_id, e.status AS email_status, e.sent_at,
               e.opens, e.proxy_opens, e.clicks, e.first_open_at, e.last_click_at,
               e.bounced_at, e.unsubscribed_at, e.applied_at, e.application_id
          FROM dp_outreach_targets t
          LEFT JOIN LATERAL (
                SELECT * FROM dp_outreach_email x
                 WHERE lower(x.email) = lower(t.email)
                 ORDER BY x.id DESC LIMIT 1) e ON TRUE
         ORDER BY (e.clicks > 0) DESC NULLS LAST,
                  (e.opens  > 0) DESC NULLS LAST,
                  t.total_profit DESC NULLS LAST
    """)
    rows = cur.fetchall()
    db.close()
    return render_template('network/outreach.html', rows=rows, stats=stats,
                           tpl=tpl, reps=DP_SALES_REPS)


@bp.route('/network/outreach/stats')
def network_outreach_stats():
    """Polled by the page. Cheap: aggregates only, no per-row payload."""
    db = _db(); cur = db.cursor()
    try:
        _dpo_link_applications(cur)
        db.commit()
    except Exception:
        db.rollback()
    s = _dpo_stats(cur)
    cur.execute("""SELECT e.email, ev.kind, ev.ts
                     FROM dp_outreach_event ev
                     JOIN dp_outreach_email e ON e.id = ev.email_id
                    WHERE ev.kind IN ('open','click','bounce','complaint','unsubscribe')
                 ORDER BY ev.ts DESC LIMIT 12""")
    s['recent'] = [{'email': r['email'], 'kind': r['kind'],
                    'ts': r['ts'].strftime('%H:%M:%S') if r['ts'] else ''}
                   for r in cur.fetchall()]
    db.close()
    return jsonify(s)


# ── DP_OUTREACH_REVIEW_2026_07_30 — management removes names before the send ──
# Soft removal, and audited. A hard DELETE would throw away the dealer-vs-retail
# verification behind the row and could not be undone if the wrong name is cut
# the day before launch, so a removed target is flagged, excluded from sending,
# and restorable.


@bp.route('/network/outreach/remove', methods=['POST'])
def network_outreach_remove():
    tid = _int(request.form.get('id'))
    undo = _b(request.form.get('undo'))
    reason = _s(request.form.get('reason')) or None
    if not tid:
        return jsonify(ok=False, error='no id'), 400
    db = _db(); cur = db.cursor()
    try:
        # Never silently un-send. A target that already went out keeps its
        # history; removing it only stops any future send.
        if undo:
            cur.execute("""UPDATE dp_outreach_targets
                              SET removed_at=NULL, removed_by=NULL, removed_reason=NULL,
                                  status = CASE WHEN status='removed' THEN 'pending' ELSE status END
                            WHERE id=%s RETURNING name, email""", (tid,))
        else:
            cur.execute("""UPDATE dp_outreach_targets
                              SET removed_at=now(), removed_by=%s, removed_reason=%s,
                                  status = CASE WHEN status='pending' THEN 'removed' ELSE status END
                            WHERE id=%s RETURNING name, email""",
                        (_reviewer(), reason, tid))
        row = cur.fetchone()
        db.commit()
        if not row:
            db.close(); return jsonify(ok=False, error='not found'), 404
        print('[dp-outreach] %s target #%s %s (%s) by %s'
              % ('restored' if undo else 'REMOVED', tid, row['name'], row['email'], _reviewer()),
              flush=True)
        cur.execute("SELECT count(*) AS n FROM dp_outreach_targets WHERE removed_at IS NULL")
        left = cur.fetchone()['n']
        db.close()
        return jsonify(ok=True, removed=(not undo), remaining=left)
    except Exception as e:
        db.rollback(); db.close()
        print('[dp-outreach] remove %s: %s' % (tid, e), flush=True)
        return jsonify(ok=False, error=str(e)[:200]), 500


# ── EMAIL_EDIT_2026_07_30 — correct a contact address before the send ────────
# Addresses come out of LSL and some are stale, mistyped or a general inbox that
# will not reach the buyer. Management needs to fix one without a DBA. The
# original is kept so a "correction" that was actually a mistake is recoverable.


@bp.route('/network/outreach/email', methods=['POST'])
def network_outreach_email():
    tid = _int(request.form.get('id'))
    new = (_s(request.form.get('email')) or '').strip().lower()
    if not tid:
        return jsonify(ok=False, error='no id'), 400
    # Shape check only. Verifying an address really exists is not possible from
    # here (both of EW's own reply domains accept-all), so a bounce is what
    # actually proves it — which is exactly what the Bounced column is for.
    if not re.match(r'^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$', new):
        return jsonify(ok=False, error='That does not look like an email address.'), 400
    db = _db(); cur = db.cursor()
    try:
        cur.execute("SELECT email, email_original, sent_at FROM dp_outreach_targets WHERE id=%s",
                    (tid,))
        row = cur.fetchone()
        if not row:
            db.close(); return jsonify(ok=False, error='not found'), 404
        if (row['email'] or '').lower() == new:
            db.close(); return jsonify(ok=True, email=new, unchanged=True)
        # Never let two rows share an address — the send is keyed on it, and a
        # duplicate would mean one person receiving the campaign twice.
        cur.execute("SELECT name FROM dp_outreach_targets WHERE lower(email)=%s AND id<>%s",
                    (new, tid))
        clash = cur.fetchone()
        if clash:
            db.close()
            return jsonify(ok=False,
                           error='%s already uses that address.' % clash['name']), 409
        cur.execute("""UPDATE dp_outreach_targets
                          SET email_original = COALESCE(email_original, email),
                              email = %s, email_edited_at = now(), email_edited_by = %s
                        WHERE id=%s""", (new, _reviewer(), tid))
        db.commit()
        print('[dp-outreach] email #%s %r -> %r by %s'
              % (tid, row['email'], new, _reviewer()), flush=True)
        db.close()
        return jsonify(ok=True, email=new, was=row['email'])
    except Exception as e:
        db.rollback(); db.close()
        print('[dp-outreach] email edit %s: %s' % (tid, e), flush=True)
        return jsonify(ok=False, error=str(e)[:200]), 500

# ── QUIET_LIVE_2026_07_30 — recompute the quiet clocks from LSL ──────────────
# The clocks were carried over from the 2026-07-06 report and frozen there, so
# by launch they were 24 days stale and drifting: Scott Ales read 440d when the
# truth was 464d, and Ultimo Northbrook read 6d having actually dealt two days
# ago. A list management is deciding cut-offs on cannot be a month behind.
#
# Recomputed from LSL by store name (a contact can hold several rooftops, so the
# clock is the most recent deal across ALL of them):
#   last_any  — most recent deal in EITHER direction  -> days_since
#   last_sell — most recent car they SOLD us          -> sold_days
#
# One pass over deals, cached for 10 minutes. crm.db is a local file and the
# whole table is ~29k rows, so this is cheap; the cache exists to stop ten open
# dashboards each rebuilding it.

_QUIET_CACHE = {'t': 0.0}


def _dpo_refresh_quiet(force=False):
    """Recompute days_since / sold_days from LSL. Never raises into the page."""
    now = time.time()
    if not force and (now - _QUIET_CACHE['t']) < 600:
        return None
    _QUIET_CACHE['t'] = now
    try:
        c = _lsl_conn()
        last_any, last_sell = {}, {}

        def bump(d, k, v):
            if k and v and (k not in d or v > d[k]):
                d[k] = v

        # they SOLD us the car — source_name, corroborated by inventory elsewhere
        for r in c.execute("SELECT source_name, max(sold_at) d FROM deals "
                           "WHERE source_name IS NOT NULL AND source_name<>'' "
                           "GROUP BY source_name"):
            k = _normalize_name(r['source_name'])
            bump(last_sell, k, (r['d'] or '')[:10])
            bump(last_any, k, (r['d'] or '')[:10])
        # we SOLD them the car — customer/supplier side of the deal row
        for r in c.execute("SELECT supplier_name, max(sold_at) d FROM deals "
                           "WHERE supplier_name IS NOT NULL AND supplier_name<>'' "
                           "GROUP BY supplier_name"):
            bump(last_any, _normalize_name(r['supplier_name']), (r['d'] or '')[:10])
        # money we paid them — catches suppliers with no resale deal row yet
        for r in c.execute("SELECT vendor_name, max(created_at) d FROM payments "
                           "WHERE type='Purchased' AND vendor_name IS NOT NULL "
                           "GROUP BY vendor_name"):
            k = _normalize_name(r['vendor_name'])
            bump(last_sell, k, (r['d'] or '')[:10])
            bump(last_any, k, (r['d'] or '')[:10])
        c.close()

        today = datetime.now().date()

        def age(iso):
            try:
                y, m, d = (int(x) for x in iso.split('-')[:3])
                return (today - date(y, m, d)).days
            except Exception:
                return None

        db = _db(); cur = db.cursor()
        cur.execute("SELECT id, stores, name FROM dp_outreach_targets")
        rows = cur.fetchall()
        n = 0
        for r in rows:
            names = r['stores'] if isinstance(r['stores'], list) else []
            if not names:
                names = [r['name']]
            a = s = None
            for nm in names:
                k = _normalize_name(nm)
                va, vs = last_any.get(k), last_sell.get(k)
                if va and (a is None or va > a):
                    a = va
                if vs and (s is None or vs > s):
                    s = vs
            da, ds = (age(a) if a else None), (age(s) if s else None)
            # LAST_DEAL_DATE_2026_07_30 — the operator wants the actual last day
            # this dealer bought OR sold, not a derived age and not a sell-side
            # split. Store the date so the column shows a fact, not a subtraction.
            cur.execute("UPDATE dp_outreach_targets SET days_since=%s, sold_days=%s, "
                        "last_deal_at=%s WHERE id=%s", (da, ds, a, r['id']))
            n += cur.rowcount
        db.commit(); db.close()
        if n:
            print('[dp-outreach] quiet clocks refreshed from LSL: %d row(s) changed' % n,
                  flush=True)
        return n
    except Exception as e:
        print('[dp-outreach] quiet refresh: %s' % e, flush=True)
        return None


# ════════════════════════════════════════════════════════════════════════════
# DEALER_SCORECARD_2026_07_31 — permanent dealer profitability board + batting
#
# Management liked the historical-profit figures pulled from LSL for the
# outreach email. Those were a one-off snapshot in dp_outreach_targets. This
# makes them a standing screen that refreshes itself, and adds the metric they
# actually asked for: a dealer who submitted 30 cars that we only transacted once.
#
# Reads dp_dealer_scorecard, rebuilt by dealer_scorecard.py (cron, after the
# LSL nightly). Every number on this page traces back to _lsl_history's audited
# rules -- verified equal across 150 dealers on 2026-07-31.
#
# HARD RULES: read-only against LSL (HR6); nothing here can gate, delay or hide
# an enrichment leg -- the tag is a plain column write on a bid and the board is
# a separate table (HR1). This data is EW's edge and is INTERNAL ONLY -- it is
# never texted or emailed out (ENRICHMENT_SMS_DENY_BY_DEFAULT_2026_07_28).
# ════════════════════════════════════════════════════════════════════════════


def _roster_pick(q, limit=10):
    """Typeahead for tagging a bid with the dealer who set the car in.
    Resolves to a suppliers.id -- a typed name is never stored.

    Deliberately NOT _roster_search(): that one backs the public Q0 "are you
    already an EW dealer?" lookup and returns DISTINCT NAMES, which is right
    when a dealer is identifying themselves and wrong here. 43 dealer names map
    to more than one rooftop, so collapsing them would make it impossible for a
    rep to tag the correct store (audit rule 3).

    Each hit carries its own history so two same-named rooftops are
    distinguishable at the moment of choosing.
    """
    q = _s(q)
    if len(q) < 2:
        return []
    rows = []
    try:
        c = _lsl_conn()
        try:
            rows = c.execute(
                "SELECT id, name, city, state FROM suppliers "
                "WHERE name LIKE ? AND name<>'' "
                "ORDER BY (name LIKE ?) DESC, length(name) LIMIT ?",
                ('%' + q + '%', q + '%', limit)).fetchall()
        finally:
            c.close()
    except Exception as e:
        print('[dp-network] roster_pick: %s' % e, flush=True)
        return []

    out = [{'id': int(r['id']), 'name': _s(r['name']),
            'city': _s(r['city']), 'state': _s(r['state']),
            'bought': 0, 'sold': 0, 'last': None} for r in rows]
    if not out:
        return out
    # decorate with history so the rep can tell rooftops apart. Best-effort --
    # a dealer with no scorecard row is still a valid pick (a brand-new store).
    try:
        db = _db(); cur = db.cursor()
        try:
            cur.execute("SELECT supplier_id, bought_cars, sold_cars, last_activity "
                        "FROM dp_dealer_scorecard WHERE supplier_id = ANY(%s)",
                        ([o['id'] for o in out],))
            hist = {r['supplier_id']: r for r in cur.fetchall()}
            for o in out:
                h = hist.get(o['id'])
                if h:
                    o['bought'] = h['bought_cars'] or 0
                    o['sold'] = h['sold_cars'] or 0
                    o['last'] = h['last_activity'].isoformat() if h['last_activity'] else None
        finally:
            db.close()
    except Exception as e:
        print('[dp-network] roster_pick history: %s' % e, flush=True)
    return out


@bp.route('/api/network/roster-pick')
def api_network_roster_pick():
    """Login-gated (not under /api/dealerprice/, so the app-level require_login
    covers it and returns a JSON 401 rather than an HTML redirect)."""
    return jsonify({'ok': True, 'matches': _roster_pick(request.args.get('q'))})


@bp.route('/api/network/bid/<int:bid_id>/dealer-tag', methods=['POST'])
def api_network_bid_dealer_tag(bid_id):
    """Tag / untag the dealer who set this car in.

    This is the ONLY way the batting average gets a denominator for cars that
    come in through Joe rather than through a DealerPrice link. Dealers are
    used to texting him and he submits on their behalf, so the submission
    itself carries no dealer -- a human has to say who it was.

    Writes five plain columns on the bid and nothing else. It does not touch
    enrichment, the assessment, or any notify path, and it cannot fail into
    them (HR1).
    """
    sid = _int(request.form.get('supplier_id'))
    db = _db(); cur = db.cursor()
    try:
        if not sid:  # untag
            cur.execute("""UPDATE bids SET source_supplier_id=NULL,
                              source_supplier_name=NULL, source_tagged_by=NULL,
                              source_tagged_at=NULL, source_tag_origin=NULL
                            WHERE id=%s RETURNING id""", (bid_id,))
            if not cur.fetchone():
                return jsonify(ok=False, error='no such bid'), 404
            db.commit()
            return jsonify(ok=True, cleared=True)

        # Resolve the name from LSL rather than trusting what the browser sent,
        # so the stored name can never disagree with the id it sits next to.
        name = None
        try:
            c = _lsl_conn()
            try:
                r = c.execute("SELECT name FROM suppliers WHERE id=?", (sid,)).fetchone()
                name = _s(r['name']) if r else None
            finally:
                c.close()
        except Exception as e:
            print('[dp-network] dealer_tag lookup: %s' % e, flush=True)
        if not name:
            return jsonify(ok=False, error='unknown dealer id'), 400

        cur.execute("""UPDATE bids SET source_supplier_id=%s, source_supplier_name=%s,
                          source_tagged_by=%s, source_tagged_at=now(),
                          source_tag_origin='manual'
                        WHERE id=%s RETURNING id""",
                    (sid, name, _reviewer(), bid_id))
        if not cur.fetchone():
            return jsonify(ok=False, error='no such bid'), 404
        db.commit()
        return jsonify(ok=True, supplier_id=sid, name=name)
    except Exception as e:
        db.rollback()
        print('[dp-network] dealer_tag: %s' % e, flush=True)
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        db.close()


# Whitelist -- the sort key is interpolated into the ORDER BY, so it can never
# come straight from the query string.
SCORECARD_SORTS = {
    'dealer':  'lower(supplier_name)',
    'bought':  'bought_cars',
    'sold':    'sold_cars',
    'profit':  'total_gross',
    'last':    'last_activity',
    'submitted':   'submitted_cars',
    'won':     'acquired_cars',
    'batting': 'batting',
}


def _scorecard_rows(cur, q=None, scope='dealers', sort='batting', dirn='desc', limit=400):
    """Board rows.

    scope: dealers (DEFAULT -- licence on file) | all (includes the private
    individuals and marketplaces that also appear in suppliers) | active (dealt
    inside 12mo) | batting (has cars submitted) | cold (set cars in, we bought
    none).

    'dealers' is the default because suppliers is not a dealer list: of 2,645
    rows only ~1,967 have an uploaded licence, and the unlicenced remainder is
    mostly individuals who sold the desk one car. They carry $3.2M against the
    licenced $67.9M. 'all' still shows them -- 53 real franchise stores have no
    uploaded licence, so this filters the view, it never deletes a row.
    """
    where, params = [], []
    if q:
        where.append("lower(supplier_name) LIKE %s")
        params.append('%' + q.lower() + '%')
    if scope == 'dealers':
        where.append("is_dealer")
    elif scope == 'active':
        where.append("is_dealer AND last_activity > current_date - 365")
    elif scope == 'batting':
        where.append("submitted_cars > 0")
    elif scope == 'cold':
        where.append("submitted_cars > 0 AND acquired_cars = 0")
    sql = "SELECT * FROM dp_dealer_scorecard"
    if where:
        sql += " WHERE " + " AND ".join(where)
    col = SCORECARD_SORTS.get(sort, SCORECARD_SORTS['batting'])
    direction = 'ASC' if str(dirn).lower() == 'asc' else 'DESC'
    # total_gross is the tiebreak so equal-batting dealers rank by money, and
    # the order is stable between refreshes
    sql += (" ORDER BY %s %s NULLS LAST, total_gross DESC NULLS LAST LIMIT %%s"
            % (col, direction))
    params.append(limit)
    cur.execute(sql, params)
    return cur.fetchall()


def _scorecard_stats(cur):
    # Headline figures count DEALERS only. Including the unlicenced rows would
    # inflate the dealer count by ~1,167 individuals and marketplaces while
    # moving the money by 4%.
    cur.execute("""
        SELECT count(*) FILTER (WHERE is_dealer) dealers,
               count(*)                          rows_all,
               COALESCE(sum(total_gross)  FILTER (WHERE is_dealer),0) gross,
               COALESCE(sum(total_gross),0)                           gross_all,
               COALESCE(sum(bought_cars)  FILTER (WHERE is_dealer),0) bought,
               COALESCE(sum(sold_cars)    FILTER (WHERE is_dealer),0) sold,
               count(*) FILTER (WHERE is_dealer AND last_activity > current_date - 365) active_1yr,
               COALESCE(sum(submitted_cars),0)   submitted,
               COALESCE(sum(acquired_cars),0) acquired,
               count(*) FILTER (WHERE submitted_cars > 0) measured,
               count(*) FILTER (WHERE submitted_cars > 0 AND acquired_cars = 0) cold
          FROM dp_dealer_scorecard""")
    s = dict(cur.fetchone() or {})
    si = s.get('submitted') or 0
    # Fleet batting average. Deliberately computed from the totals rather than
    # averaging the per-dealer percentages -- averaging averages would let a
    # dealer who submitted one car swing the number as hard as one who submitted 300.
    s['batting'] = round(100.0 * (s.get('acquired') or 0) / si, 1) if si else None
    cur.execute("""SELECT finished_at, ok, dealers, secs, error,
                          fleet_profit, fleet_deals
                     FROM dp_dealer_scorecard_run
                    WHERE ok IS TRUE ORDER BY id DESC LIMIT 1""")
    run = cur.fetchone()
    s['run'] = run
    # FLEET_PROFIT_2026_07_31 -- what EW actually made, each deal counted once.
    # NEVER sum total_gross across dealers for a headline: a car bought from A
    # and sold to B credits the same front_value to both relationships. Each
    # row is right, the sum is not ($71.1M summed vs $35.4M true). The summed
    # figure is kept as gross_rel ("relationship value") because it is still
    # the right number for ranking a single dealer, just not for a total.
    s['gross_rel'] = s.pop('gross', 0)
    s['fleet_profit'] = (run or {}).get('fleet_profit')
    s['fleet_deals'] = (run or {}).get('fleet_deals')
    # newest run of ANY outcome -- so a failed rebuild after a good one is
    # visible rather than silently serving yesterday's numbers as current
    cur.execute("SELECT ok FROM dp_dealer_scorecard_run ORDER BY id DESC LIMIT 1")
    last = cur.fetchone()
    s['stale'] = bool(last and last.get('ok') is False)
    return s


@bp.route('/network/dealers')
def network_dealers():
    q = _s(request.args.get('q'))
    scope = _s(request.args.get('scope')) or 'dealers'
    sort = _s(request.args.get('sort')) or 'batting'
    if sort not in SCORECARD_SORTS:
        sort = 'batting'
    dirn = 'asc' if _s(request.args.get('dir')).lower() == 'asc' else 'desc'
    db = _db(); cur = db.cursor()
    try:
        rows = _scorecard_rows(cur, q=q, scope=scope, sort=sort, dirn=dirn)
        stats = _scorecard_stats(cur)
    finally:
        db.close()
    return render_template('network/dealers.html', rows=rows, stats=stats,
                           q=q, scope=scope, sort=sort, dir=dirn)


@bp.route('/network/dealers/stats')
def network_dealers_stats():
    """Polled by the page so the header stays live without re-rendering the
    whole table."""
    db = _db(); cur = db.cursor()
    try:
        s = _scorecard_stats(cur)
    finally:
        db.close()
    run = s.pop('run', None)
    s['refreshed'] = (run['finished_at'].strftime('%b %-d, %-I:%M %p')
                      if run and run.get('finished_at') else None)
    return jsonify(s)


@bp.route('/network/dealers/refresh', methods=['POST'])
def network_dealers_refresh():
    """Manual rebuild. The cron keeps it current; this is for when someone is
    looking at the board right after a deal books and wants it to catch up."""
    try:
        import dealer_scorecard
        n = dealer_scorecard.refresh(verbose=False)
        return jsonify(ok=True, dealers=n)
    except Exception as e:
        print('[dp-network] scorecard refresh: %s' % e, flush=True)
        return jsonify(ok=False, error=str(e)[:200]), 500


@bp.route('/network/dealer/<int:sid>')
def network_dealer_detail(sid):
    """Per-dealer drill-down. Reuses _lsl_history so the car-level list is the
    same one the application packet shows -- one implementation, one answer."""
    db = _db(); cur = db.cursor()
    try:
        cur.execute("SELECT * FROM dp_dealer_scorecard WHERE supplier_id=%s", (sid,))
        row = cur.fetchone()
        cur.execute("""SELECT id, vin, year, make, model, trim, created_at, status,
                              ai_price, bid_amount, source_tag_origin, source_tagged_by
                         FROM bids WHERE source_supplier_id=%s
                        ORDER BY created_at DESC LIMIT 300""", (sid,))
        submitted = cur.fetchall()
    finally:
        db.close()
    if not row:
        abort(404)
    hist = _lsl_history(row['supplier_name'], supplier_id=sid) or {}
    # which set-in cars EW actually ended up owning, so the drill-down can mark
    # each row hit/miss instead of just showing a percentage
    bought = set()
    for car in (hist.get('cars') or []):
        if car.get('dir') == 'buy' and car.get('vin'):
            bought.add(car['vin'].upper())
    for b in submitted:
        b['won'] = bool(b['vin'] and b['vin'].upper() in bought)
    return render_template('network/dealer_detail.html', row=row, hist=hist,
                           submitted=submitted, cars=(hist.get('cars') or [])[:400])


# ── DP_SUBMIT_ATTRIBUTION_2026_07_31 ─────────────────────────────────────────
def member_supplier_id(member):
    """LSL suppliers.id for a DealerPrice member, or None.

    Cached on dealerprice_members.lsl_supplier_id so the roster scan happens
    once per member rather than once per submitted car.

    STRICT: only a suppliers match counts. _roster_match also falls back to the
    customers table, but a customers hit is a retail buyer, not a dealer
    identity -- accepting it would attribute cars to the wrong entity space
    (the same id-space collision audit rule 2 warns about). No match means the
    bid stays untagged, which is honest; a guess would not be.
    """
    if not member:
        return None
    sid = member.get('lsl_supplier_id')
    if sid:
        return int(sid)
    match = _roster_match(_s(member.get('dealership_name')),
                          _s(member.get('contact_phone'))) or {}
    if match.get('source') != 'suppliers' or not match.get('supplier_id'):
        return None
    sid = int(match['supplier_id'])
    try:
        db = _db(); cur = db.cursor()
        try:
            cur.execute("UPDATE dealerprice_members SET lsl_supplier_id=%s "
                        "WHERE id=%s AND lsl_supplier_id IS NULL",
                        (sid, member['id']))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        print('[dp-network] cache member supplier: %s' % e, flush=True)
    return sid


def tag_bid_from_member(cur, bid_id, member):
    """Stamp the submitting dealer onto a bid. Called from the DealerPrice
    submit bridge in app.py, on the caller's cursor/transaction.

    Never raises into the submit path: a bid that cannot be attributed must
    still be created and still enrich. Attribution is bookkeeping, the car is
    the business (HR1).
    """
    try:
        sid = member_supplier_id(member)
        if not sid:
            return False
        cur.execute("""UPDATE bids
                          SET source_supplier_id=%s,
                              source_supplier_name=%s,
                              source_tagged_at=now(),
                              source_tag_origin='dealerprice'
                        WHERE id=%s AND source_supplier_id IS NULL""",
                    (sid, _s(member.get('dealership_name')) or None, bid_id))
        return True
    except Exception as e:
        print('[dp-network] tag_bid_from_member: %s' % e, flush=True)
        return False
