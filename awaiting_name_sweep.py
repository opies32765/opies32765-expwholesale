#!/usr/bin/env python3
"""Awaiting-name sweep — Phase 3 housekeeping cron.

Runs every 15 minutes. Two jobs against bids stuck in `awaiting_name=TRUE`:

  1. NUDGE — bids whose name_asked_at is more than 1 hour old AND have not
     been nudged yet (name_nudged_at IS NULL). Sends a single follow-up SMS
     and stamps name_nudged_at so we don't spam.

  2. ARCHIVE — bids whose name_asked_at is more than 24 hours old, with no
     name reply. Flipped to status='passed', awaiting_name=FALSE,
     oscar_archived_at=NOW(). Bid stays in the DB (staff can review) but is
     out of the active dashboard.

Never sends nudge AND archive in the same run: the 24h check runs first,
so a bid that just crossed the 24h line is archived (not re-nudged then
archived a second later).

Reads DB + Twilio creds from env (same as gunicorn). Errors are logged but
do not abort the sweep — one stuck row never poisons the rest.
"""

import os
import sys
import time

# Reuse the app module so we share send_sms / get_db / settings — no
# duplicate Twilio client setup.
sys.path.insert(0, '/opt/expwholesale')
import app  # noqa: E402


NUDGE_SMS = (
    "still need a name to push your bid through — even a first name works."
)


def _now_iso():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())


def _log(msg):
    print(f'[awaiting-name-sweep {_now_iso()}] {msg}', flush=True)


def sweep():
    db = app.get_db()
    cur = db.cursor()
    archived = 0
    nudged = 0
    archive_errors = 0
    nudge_errors = 0

    # ── 1. ARCHIVE first (24h+ no reply) ──
    # Done BEFORE nudge so a bid that just aged into 24h gets archived
    # rather than nudged-then-archived a tick later.
    try:
        cur.execute("""
            SELECT id, phone
              FROM bids
             WHERE awaiting_name = TRUE
               AND name_asked_at < NOW() - INTERVAL '24 hours'
        """)
        rows = cur.fetchall() or []
    except Exception as e:
        _log(f'archive query error: {e}')
        rows = []

    for row in rows:
        bid_id = row['id']
        phone = row.get('phone')
        try:
            cur.execute("""
                UPDATE bids
                   SET status = 'passed',
                       awaiting_name = FALSE,
                       oscar_archived_at = COALESCE(oscar_archived_at, NOW()),
                       updated_at = NOW()
                 WHERE id = %s
                   AND awaiting_name = TRUE
            """, (bid_id,))
            db.commit()
            archived += 1
            _log(f'archived bid={bid_id} phone={phone!r} reason=no_name_after_24h')
        except Exception as e:
            archive_errors += 1
            _log(f'archive error bid={bid_id}: {e}')
            try:
                db.rollback()
            except Exception:
                pass

    # ── 2. NUDGE bids 1h+ stale, never nudged ──
    try:
        cur.execute("""
            SELECT id, phone
              FROM bids
             WHERE awaiting_name = TRUE
               AND name_asked_at < NOW() - INTERVAL '1 hour'
               AND name_nudged_at IS NULL
        """)
        rows = cur.fetchall() or []
    except Exception as e:
        _log(f'nudge query error: {e}')
        rows = []

    for row in rows:
        bid_id = row['id']
        phone = row.get('phone')
        if not phone or phone.startswith('field:'):
            # Field-rep bids never came through SMS; skip nudge.
            continue
        try:
            sent = app.send_sms(phone, NUDGE_SMS)
            if sent:
                cur.execute(
                    "UPDATE bids SET name_nudged_at = NOW() WHERE id = %s",
                    (bid_id,),
                )
                db.commit()
                nudged += 1
                _log(f'nudged bid={bid_id} phone={phone!r}')
            else:
                _log(f'nudge SMS failed bid={bid_id} phone={phone!r} '
                     f'(send_sms returned False; will retry next run)')
        except Exception as e:
            nudge_errors += 1
            _log(f'nudge error bid={bid_id}: {e}')
            try:
                db.rollback()
            except Exception:
                pass

    try:
        db.close()
    except Exception:
        pass

    if archived or nudged or archive_errors or nudge_errors:
        _log(f'summary archived={archived} nudged={nudged} '
             f'archive_errors={archive_errors} nudge_errors={nudge_errors}')


if __name__ == '__main__':
    sweep()
