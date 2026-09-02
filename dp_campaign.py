#!/usr/bin/env python3
"""dp_campaign.py — the DealerPrice outreach campaign sender.

    python3 dp_campaign.py --limit 10               # DRY RUN, shows who would get it
    python3 dp_campaign.py --limit 10 --send        # actually sends 10
    python3 dp_campaign.py --limit 50 --send        # next 50, skips anyone already sent
    python3 dp_campaign.py --status                 # progress so far

RESUMABLE BY CONSTRUCTION. Every run skips targets that already have a
dp_outreach_email row, so "send the next 50" is the same command again. A crash
at recipient 400 costs nothing: re-run and it picks up at 401. There is no
cursor to corrupt and no state file to go stale — the sent rows ARE the state.

The email body is imported from dp_outreach_send.build_html, the exact script
the operator reviewed and approved. Copying the HTML into a second file would
guarantee the two drift.

Guards:
  * DRY RUN unless --send. The dry run does every lookup and render, so it
    proves the batch before a single message leaves.
  * --limit is REQUIRED for --send. There is no "send everything" invocation;
    728 in one command should not be one typo away.
  * suppression is checked in the query AND enforced by the database trigger
    (trg_dp_block_suppressed). Belt and braces: a removed or unsubscribed
    dealer cannot be mailed even if this query were wrong.
  * throttle between sends; Resend allows 10/sec and there is no reason to
    approach it.
  * one failure is recorded and skipped, never aborts the batch.
"""
import argparse
import os
import sys
import time
import secrets

sys.path.insert(0, '/opt/expwholesale')
os.chdir('/opt/expwholesale')

import psycopg2
import psycopg2.extras
import dealerprice_network as DPN
from dp_outreach_send import build_html, SUBJECT, CAMPAIGN
# DP_FOLLOWUP_2026_09_02: the second touch is a DIFFERENT email and a
# DIFFERENT campaign tag. Imported under aliases so --followup swaps all
# three together (copy, subject, campaign) and they can never half-swap.
from dp_followup_send import (build_html as fu_build_html,
                              SUBJECT as FU_SUBJECT,
                              CAMPAIGN as FU_CAMPAIGN)

THROTTLE_S = 0.6            # ~1.6/sec against a 10/sec ceiling
MAX_LIMIT = 250             # one command should never be able to send the lot


def db():
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        sys.exit('DATABASE_URL is not set.')
    c = psycopg2.connect(dsn)
    c.set_client_encoding('UTF8')
    return c


def pick(cur, limit, order):
    """Targets that are live, not suppressed, and have never been sent to."""
    ob = {'profit': 'COALESCE(t.total_profit,0) DESC',
          'low': 'COALESCE(t.total_profit,0) ASC',
          'random': 'random()'}[order]
    cur.execute("""
        SELECT t.id, t.name, t.email, t.total_profit
          FROM dp_outreach_targets t
         WHERE t.removed_at IS NULL
           AND t.email IS NOT NULL AND t.email <> ''
           -- DP_DEFER_2026-08-05: a target held for a scheduled window is not
           -- eligible yet. NULL (the default) means "send whenever".
           AND (t.send_after IS NULL OR t.send_after <= now())
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_suppression s
                            WHERE s.email = lower(t.email))
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_email e
                            WHERE lower(e.email) = lower(t.email))
         ORDER BY """ + ob + """
         LIMIT %s""", (limit,))
    return cur.fetchall()


def pick_deferred(cur, limit):
    """Only targets explicitly deferred to a window that has now opened."""
    cur.execute("""
        SELECT t.id, t.name, t.email, t.total_profit
          FROM dp_outreach_targets t
         WHERE t.removed_at IS NULL
           AND t.email IS NOT NULL AND t.email <> ''
           AND t.send_after IS NOT NULL AND t.send_after <= now()
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_suppression s
                            WHERE s.email = lower(t.email))
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_email e
                            WHERE lower(e.email) = lower(t.email))
         ORDER BY COALESCE(t.total_profit,0) DESC
         LIMIT %s""", (limit,))
    return cur.fetchall()


def pick_followup(cur, limit, order, dest_campaign):
    """DP_FOLLOWUP_2026_09_02 — the ones the LAUNCH email actually LANDED on.

    "Landed" is Resend's `delivered` webhook and nothing softer. A row still
    sitting at `sent` never produced a delivery event, and a soft bounce is a
    car that came back — neither is a mailbox we know a human opened.

    Excluded, in order of how badly each would embarrass us:
      * applied  — they already signed up. Re-pitching a customer is the one
                   mistake in this batch a dealer would actually notice.
      * unsubscribed / complained — non-negotiable, and the suppression
                   trigger would refuse them anyway. Belt and braces.
      * suppressed — hard bounces and manual removals.
      * removed_at — an operator took them off the list by hand.
      * already in THIS campaign — resume-by-construction, same as pick().
        Re-running the command after a crash picks up where it stopped.
    """
    ob = {'profit': 'COALESCE(t.total_profit,0) DESC',
          'low': 'COALESCE(t.total_profit,0) ASC',
          'random': 'random()'}[order]
    cur.execute("""
        WITH landed AS (
            SELECT DISTINCT ON (lower(e.email)) lower(e.email) AS email,
                   e.status, e.applied_at, e.unsubscribed_at, e.complained_at
              FROM dp_outreach_email e
             WHERE e.campaign = %s
             ORDER BY lower(e.email), e.id DESC
        )
        SELECT t.id, t.name, t.email, t.total_profit
          FROM dp_outreach_targets t
          JOIN landed l ON l.email = lower(t.email)
         WHERE t.removed_at IS NULL
           AND t.email IS NOT NULL AND t.email <> ''
           AND l.status = 'delivered'
           AND l.applied_at IS NULL
           AND l.unsubscribed_at IS NULL
           AND l.complained_at IS NULL
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_suppression s
                            WHERE s.email = lower(t.email))
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_email f
                            WHERE lower(f.email) = lower(t.email)
                              AND f.campaign = %s)
           -- e.applied_at is only backfilled by _dpo_link_applications(),
           -- which runs when somebody LOADS the outreach dashboard. Nobody
           -- looks at it overnight, so a dealer who applies at 11pm would
           -- still be in tomorrow's batch. Read the applications table
           -- directly and the gap closes regardless of dashboard traffic.
           AND NOT EXISTS (SELECT 1 FROM dealer_applications a
                            WHERE lower(a.contact_email) = lower(t.email))
         ORDER BY """ + ob + """
         LIMIT %s""", (CAMPAIGN, dest_campaign, limit))
    return cur.fetchall()


def pick_retries(cur, limit, after_hours, max_attempts):
    """Addresses whose LATEST attempt was a soft bounce, old enough to have
    settled, and not already retried to death."""
    cur.execute("""
        WITH latest AS (
            SELECT DISTINCT ON (lower(e.email))
                   lower(e.email) AS email, e.status, e.bounce_type,
                   e.bounced_at, e.error
              FROM dp_outreach_email e
             ORDER BY lower(e.email), e.id DESC
        ), tries AS (
            SELECT lower(email) AS email, count(*) AS n
              FROM dp_outreach_email GROUP BY 1
        )
        SELECT t.id, t.name, t.email, t.total_profit,
               tr.n AS attempts, l.error
          FROM dp_outreach_targets t
          JOIN latest l ON l.email = lower(t.email)
          JOIN tries  tr ON tr.email = lower(t.email)
         WHERE t.removed_at IS NULL
           AND l.status = 'bounced'
           AND l.bounce_type = 'soft'
           AND l.bounced_at < now() - (%s || ' hours')::interval
           AND tr.n < %s
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_suppression s
                            WHERE s.email = lower(t.email))
         ORDER BY COALESCE(t.total_profit,0) DESC
         LIMIT %s""", (after_hours, max_attempts, limit))
    return cur.fetchall()


def status(cur):
    cur.execute("""
        SELECT (SELECT count(*) FROM dp_outreach_targets WHERE removed_at IS NULL) AS live,
               (SELECT count(*) FROM dp_outreach_suppression)                      AS suppressed,
               (SELECT count(*) FROM dp_outreach_email)                            AS attempted,
               (SELECT count(*) FROM dp_outreach_email WHERE status='sent')        AS sent,
               (SELECT count(*) FROM dp_outreach_email WHERE status='delivered')   AS delivered,
               (SELECT count(*) FROM dp_outreach_email WHERE status='bounced')     AS bounced,
               (SELECT count(*) FROM dp_outreach_email WHERE status='failed')      AS failed,
               (SELECT count(*) FROM dp_outreach_email WHERE opens > 0)            AS opened,
               (SELECT count(*) FROM dp_outreach_email WHERE clicks > 0)           AS clicked,
               (SELECT count(*) FROM dp_outreach_email
                 WHERE unsubscribed_at IS NOT NULL)                                AS unsubscribed""")
    r = dict(cur.fetchone())
    # DP_PENDING_COUNT_2026-08-05: see dealerprice_network.py. live - attempted
    # goes negative as bounces suppress addresses out of `live`.
    cur.execute("""
        SELECT count(*) AS n
          FROM dp_outreach_targets t
         WHERE t.removed_at IS NULL
           AND t.email IS NOT NULL AND t.email <> ''
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_suppression sp
                            WHERE sp.email = lower(t.email))
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_email e
                            WHERE lower(e.email) = lower(t.email))""")
    remaining = (cur.fetchone() or {}).get('n') or 0
    print('  live targets      %d' % r['live'])
    print('  suppressed        %d   (never mailed)' % r['suppressed'])
    print('  ── sent so far ──')
    print('  attempted         %d' % r['attempted'])
    print('    delivered       %d' % r['delivered'])
    print('    bounced         %d' % r['bounced'])
    print('    failed          %d' % r['failed'])
    print('    opened          %d' % r['opened'])
    print('    clicked         %d' % r['clicked'])
    print('    unsubscribed    %d' % r['unsubscribed'])
    cur.execute("""
        WITH latest AS (
            SELECT DISTINCT ON (lower(email)) lower(email) AS email,
                   status, bounce_type, bounced_at
              FROM dp_outreach_email ORDER BY lower(email), id DESC)
        SELECT count(*) FILTER (WHERE status='bounced' AND bounce_type='soft') AS soft,
               count(*) FILTER (WHERE status='bounced' AND bounce_type='hard') AS hard
          FROM latest""")
    b = dict(cur.fetchone())
    print('    soft bounces    %d   (retryable — see --retry-soft)' % (b['soft'] or 0))
    print('    hard bounces    %d   (suppressed, never retried)' % (b['hard'] or 0))
    print('  REMAINING         %d' % remaining)
    # DP_FOLLOWUP_2026_09_02: the totals above span EVERY campaign. Once a
    # second touch exists, one merged "attempted 718" number is a lie about
    # both. Break it out.
    cur.execute("""
        SELECT campaign,
               count(*)                                            AS attempted,
               count(*) FILTER (WHERE status='delivered')          AS delivered,
               count(*) FILTER (WHERE status='bounced')            AS bounced,
               count(*) FILTER (WHERE opens > 0)                   AS opened,
               count(*) FILTER (WHERE clicks > 0)                  AS clicked,
               count(*) FILTER (WHERE applied_at IS NOT NULL)      AS applied,
               min(sent_at)::date                                  AS first_send
          FROM dp_outreach_email GROUP BY 1 ORDER BY min(sent_at)""")
    camps = cur.fetchall()
    if len(camps) > 1:
        print('  ── by campaign ──')
        for c in camps:
            print('  %-22s %s  attempted %-5d delivered %-5d bounced %-4d '
                  'opened %-4d clicked %-4d applied %d'
                  % (c['campaign'], c['first_send'] or '          ',
                     c['attempted'], c['delivered'], c['bounced'],
                     c['opened'], c['clicked'], c['applied']))
    # what the follow-up would go to right now
    cur.execute("""
        WITH landed AS (
            SELECT DISTINCT ON (lower(e.email)) lower(e.email) AS email,
                   e.status, e.applied_at, e.unsubscribed_at, e.complained_at
              FROM dp_outreach_email e WHERE e.campaign = %s
             ORDER BY lower(e.email), e.id DESC)
        SELECT count(*) AS n
          FROM dp_outreach_targets t JOIN landed l ON l.email = lower(t.email)
         WHERE t.removed_at IS NULL AND t.email <> ''
           AND l.status='delivered' AND l.applied_at IS NULL
           AND l.unsubscribed_at IS NULL AND l.complained_at IS NULL
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_suppression s
                            WHERE s.email = lower(t.email))
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_email f
                            WHERE lower(f.email) = lower(t.email)
                              AND f.campaign = %s)
           AND NOT EXISTS (SELECT 1 FROM dealer_applications a
                            WHERE lower(a.contact_email) = lower(t.email))""",
                (CAMPAIGN, FU_CAMPAIGN))
    print('  FOLLOW-UP QUEUE   %d   (--followup)'
          % ((cur.fetchone() or {}).get('n') or 0))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int)
    ap.add_argument('--send', action='store_true')
    ap.add_argument('--order', choices=['profit', 'low', 'random'], default='profit')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--deferred', action='store_true',
                    help='send ONLY the deferred cohort whose window has opened')
    ap.add_argument('--followup', action='store_true',
                    help='SECOND touch: to the launch recipients it was '
                         'actually DELIVERED to, minus anyone who has since '
                         'applied')
    # DP_COPY_CHOICE_2026_09_02 — which email, and what to call the send.
    ap.add_argument('--copy', choices=['launch', 'followup'], default='followup',
                    help='which email body to send with --followup. '
                         '"launch" re-sends the original August email '
                         'verbatim; "followup" sends the rewrite (default)')
    ap.add_argument('--campaign-tag', dest='campaign_tag',
                    help='name this send (default depends on --copy). This is '
                         'what the dashboard groups by and what the resume '
                         'query dedupes on')
    ap.add_argument('--resume', action='store_true',
                    help='allow adding to a campaign tag that already has '
                         'rows (use when continuing a batched send)')
    ap.add_argument('--retry-soft', action='store_true',
                    help='resend to soft bounces instead of new targets')
    ap.add_argument('--retry-after-hours', type=int, default=6,
                    help='how long a soft bounce must have settled (default 6)')
    ap.add_argument('--max-attempts', type=int, default=2,
                    help='total sends per address before giving up (default 2)')
    a = ap.parse_args()

    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if a.status:
        status(cur); conn.close(); return 0

    if not a.limit:
        sys.exit('--limit is required (there is no send-everything mode)')
    if a.limit > MAX_LIMIT:
        sys.exit('--limit %d exceeds MAX_LIMIT %d' % (a.limit, MAX_LIMIT))

    key = os.environ.get('RESEND_API_KEY', '')
    if a.send and not key:
        sys.exit('RESEND_API_KEY is not set.')

    # DP_FOLLOWUP_2026_09_02: copy, subject and campaign tag move together.
    # DP_COPY_CHOICE_2026_09_02: --copy chooses which of the two bodies.
    render, subject, camp = build_html, SUBJECT, CAMPAIGN
    if a.followup:
        if a.retry_soft or a.deferred:
            sys.exit('--followup cannot be combined with --retry-soft/--deferred')
        if a.copy == 'launch':
            # the ORIGINAL August email, imported not copied, so it can never
            # drift from what the 718 actually received
            render, subject = build_html, SUBJECT
            camp = a.campaign_tag or 'dp_relaunch_2026_09'
        else:
            render, subject = fu_build_html, FU_SUBJECT
            camp = a.campaign_tag or FU_CAMPAIGN
        if camp == CAMPAIGN:
            sys.exit('refusing to send into %s — that is the August campaign; '
                     'its numbers would merge with this send. Pass '
                     '--campaign-tag with a new name.' % CAMPAIGN)
        # a tag that already has rows is either a resumed batch or a mistake;
        # make the operator say which
        cur.execute("SELECT count(*) AS n FROM dp_outreach_email WHERE campaign=%s",
                    (camp,))
        _n = (cur.fetchone() or {}).get('n') or 0
        if _n and not a.resume:
            sys.exit('campaign %s already has %d row(s). Add --resume to '
                     'continue that send, or --campaign-tag for a new one.'
                     % (camp, _n))
    elif a.campaign_tag or a.copy != 'followup':
        sys.exit('--copy/--campaign-tag only apply to --followup')

    if a.retry_soft:
        rows = pick_retries(cur, a.limit, a.retry_after_hours, a.max_attempts)
    elif a.deferred:
        rows = pick_deferred(cur, a.limit)
    elif a.followup:
        rows = pick_followup(cur, a.limit, a.order, camp)
    else:
        rows = pick(cur, a.limit, a.order)
    print('campaign : %s' % camp)
    print('subject  : %s' % subject)
    print('from     : %s' % DPN.DP_EMAIL_FROM)
    if a.followup:
        print('copy     : %s  (%s)'
              % (a.copy, 'the ORIGINAL August email, verbatim'
                 if a.copy == 'launch' else 'the rewrite'))
        print('mode     : FOLLOW-UP to delivered launch recipients '
              '(applied/unsub/complained/suppressed excluded)')
    if a.deferred:
        print('mode     : DEFERRED cohort (scheduled window)')
    elif a.retry_soft:
        print('mode     : RETRY soft bounces settled >%dh, max %d attempts'
              % (a.retry_after_hours, a.max_attempts))
    else:
        print('order    : %s' % a.order)
    print('mode     : %s' % ('LIVE SEND' if a.send else 'DRY RUN — nothing will be sent'))
    print('picked   : %d' % len(rows))
    print('-' * 78)
    if not rows:
        print('nothing left to send.'); conn.close(); return 0

    ok = fail = 0
    for i, r in enumerate(rows, 1):
        em = (r['email'] or '').strip()
        label = '%2d. %-34s %-30s' % (i, (r['name'] or '')[:33], em[:29])
        if not a.send:
            extra = ''
            if a.retry_soft:
                extra = '  attempt %s · %s' % (
                    (r.get('attempts') or 0) + 1,
                    (r.get('error') or 'no diagnostic')[:60])
            print('DRY  %s $%-12s%s' % (label,
                  format(int(r['total_profit'] or 0), ','), extra))
            continue

        token = secrets.token_urlsafe(16)
        html = render('', token)              # '' -> "Hi there," (uniform greeting)
        eid = None
        try:
            cur.execute("""INSERT INTO dp_outreach_email
                             (target_id, email, token, subject, campaign, status, created_at)
                           VALUES (%s,%s,%s,%s,%s,'queued',now())
                        RETURNING id""",
                        (r['id'], em, token, subject, camp))
            eid = cur.fetchone()['id']
            conn.commit()
        except Exception as e:
            conn.rollback()
            # the trigger refuses suppressed addresses — that is the safety net
            # doing its job, not an error worth stopping the batch for
            print('SKIP %s %s' % (label, str(e).split('\n')[0][:90]))
            fail += 1
            continue

        try:
            import resend
            resend.api_key = key
            payload = {
                'from': DPN.DP_EMAIL_FROM, 'to': [em],
                'subject': subject, 'html': html,
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
            cur.execute("UPDATE dp_outreach_targets SET sent_at=now(), status='sent' "
                        "WHERE id=%s", (r['id'],))
            conn.commit()
            print('SENT %s %s' % (label, pid))
            ok += 1
        except Exception as e:
            conn.rollback()
            cur.execute("UPDATE dp_outreach_email SET status='failed', error=%s WHERE id=%s",
                        (str(e)[:400], eid))
            conn.commit()
            print('FAIL %s %s' % (label, str(e)[:80]))
            fail += 1
        time.sleep(THROTTLE_S)

    print('-' * 78)
    if a.send:
        print('sent %d, failed/skipped %d' % (ok, fail))
        print()
        status(cur)
    else:
        print('DRY RUN — add --send to actually send these %d.' % len(rows))
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
