#!/usr/bin/env python3
"""
Sourcing-bot REPL — chat with the bot from the command line. No Twilio
SMS spend, no Twilio phone number involved.

Pipeline path: runs the exact same _run_turn that the Twilio webhook
calls — extract -> merge_spec -> _decide -> search -> compose/rewrite ->
persist. Only difference: send_sms=None so no SMS leaves the box.

The conversation persists to a dedicated test row in sourcing_requests
(phone='test:cli' by default) so it shows up on the dashboard Active
Sourcing banner just like a real user. Use /reset to archive and start
a fresh row.

Usage from C1:
    python3 /opt/expwholesale/scripts/sourcing_repl.py
    python3 /opt/expwholesale/scripts/sourcing_repl.py --reset

REPL commands:
    /show       Dump current spec row (make/model/trim/colors/etc.)
    /history    Print every turn in the conversation
    /reset      Archive the current row and start a new one
    /quit       Exit (Ctrl-D and Ctrl-C also work)
    <anything else>   Sent as a user message to the bot
"""
import os
import sys
import json
import argparse
import shlex
import subprocess


# Make app importable when running from /opt/expwholesale/scripts.
sys.path.insert(0, '/opt/expwholesale')


def _load_systemd_env():
    """Load DATABASE_URL, CEREBRAS_API_KEY, etc. from the expwholesale
    systemd unit so the script can run as the operator without manually
    exporting env vars. Mirrors what run_sourcing_cron.sh does."""
    try:
        out = subprocess.check_output(
            ['systemctl', 'show', 'expwholesale', '--property=Environment'],
            text=True,
        )
    except Exception as e:
        print(f'[env] systemctl show failed: {e}', file=sys.stderr)
        return
    line = out.replace('Environment=', '', 1).strip()
    for tok in shlex.split(line):
        if '=' in tok:
            k, v = tok.split('=', 1)
            if k not in os.environ:
                os.environ[k] = v


_load_systemd_env()

TEST_PHONE = 'test:cli'


def _fetch_test_row(cur, *, create_if_missing=True, db=None):
    cur.execute(
        "SELECT * FROM sourcing_requests "
        "WHERE phone = %s AND status <> 'archived' "
        "ORDER BY id DESC LIMIT 1",
        (TEST_PHONE,),
    )
    r = cur.fetchone()
    if r:
        return dict(r)
    if not create_if_missing:
        return None
    cur.execute(
        "INSERT INTO sourcing_requests "
        "(phone, status, conversation, last_msg_at, last_inbound_at) "
        "VALUES (%s, 'gathering', '[]'::jsonb, NOW(), NOW()) RETURNING id",
        (TEST_PHONE,),
    )
    row_id = cur.fetchone()['id']
    if db is not None:
        db.commit()
    cur.execute("SELECT * FROM sourcing_requests WHERE id = %s", (row_id,))
    return dict(cur.fetchone())


def _archive_test_row(db, cur):
    cur.execute(
        "UPDATE sourcing_requests "
        "SET status='archived', archived_at=NOW(), archive_reason='cli_reset' "
        "WHERE phone=%s AND status<>'archived'",
        (TEST_PHONE,),
    )
    db.commit()


def _show_row(row):
    pretty = {k: row.get(k) for k in (
        'id', 'status', 'make', 'model', 'trim', 'ext_color', 'int_color',
        'year_min', 'year_max', 'miles_max', 'price_hint', 'transmission',
        'customer_name', 'narrative_brief', 'recap_confirmed_at', 'seen_at',
        'relaxations', 'vehicle_interests',
    )}
    print(json.dumps(pretty, default=str, indent=2))


def _show_history(row):
    conv = row.get('conversation') or []
    if not conv:
        print('(no turns yet)')
        return
    for i, t in enumerate(conv, 1):
        role = t.get('role', '?')
        text = (t.get('text', '') or '').replace('\n', ' / ')
        raw = t.get('raw') or {}
        branch = raw.get('branch', '')
        suffix = f'  [{branch}]' if branch else ''
        print(f'{i:3d}. {role:4s}: {text}{suffix}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reset', action='store_true',
                    help='Archive any existing test row first (start fresh).')
    args = ap.parse_args()

    # TTY warning (non-fatal) — if stdin isn't a terminal, the input()
    # loop will exit at first EOF. That's fine for piped testing (we read
    # the piped lines then cleanly stop), but if a user SSH'd in and saw
    # an immediate exit, it'd be confusing. Warn them up front so the
    # "Connection closed" isn't a mystery.
    if not sys.stdin.isatty():
        print('[warning] stdin is not a TTY. Interactive REPL will exit at '
              'first EOF. If you are SSHing, pass -tt to force a PTY:',
              file=sys.stderr)
        print('  ssh -tt root@62.146.226.100 python3 '
              '/opt/expwholesale/scripts/sourcing_repl.py', file=sys.stderr)

    from app import get_db
    from sourcing_bot import _run_turn

    db = get_db()
    cur = db.cursor()

    if args.reset:
        _archive_test_row(db, cur)
        print('[reset] archived existing test row')

    row = _fetch_test_row(cur, db=db)
    prior = len(row.get('conversation') or [])
    print(f'[ready] test row id={row["id"]} phone={TEST_PHONE} ({prior} prior turn(s))')
    print('Commands: /show /history /reset /quit. Anything else = user message.')
    print()

    while True:
        try:
            line = input('you> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        if line in ('/quit', '/exit', '/q'):
            break
        if line == '/show':
            r = _fetch_test_row(cur, create_if_missing=False)
            if r:
                _show_row(r)
            else:
                print('(no active row)')
            continue
        if line == '/history':
            r = _fetch_test_row(cur, create_if_missing=False)
            if r:
                _show_history(r)
            else:
                print('(no active row)')
            continue
        if line == '/reset':
            _archive_test_row(db, cur)
            row = _fetch_test_row(cur, db=db)
            print(f'[reset] new row id={row["id"]}')
            continue

        # Confusion catcher: user typed reset-shaped text but without the
        # slash. Treat as the command, with a one-line nudge so they learn
        # the syntax. Without this, "bot --reset" / "--reset" / "reset"
        # would silently go to the bot as a regular message (and the
        # extractor would invent specs from random tokens).
        _RESET_CONFUSIONS = ('bot --reset', '--reset', '/--reset',
                             'reset', '\\reset', 'reset()')
        if line.lower().strip() in _RESET_CONFUSIONS:
            print(f"[hint] inside the REPL the command is `/reset` (leading slash). "
                  f"Treating this as /reset for you.")
            _archive_test_row(db, cur)
            row = _fetch_test_row(cur, db=db)
            print(f'[reset] new row id={row["id"]}')
            continue

        # Real turn — same pipeline as Twilio inbound.
        # Re-fetch row each turn so we see fresh state (other Claude / staff
        # changes show up).
        row = _fetch_test_row(cur, db=db)
        _run_turn(db, cur, row['id'], row, line, num_media=0,
                  send_sms=None, phone=TEST_PHONE)
        # Re-fetch to get the bot's freshly-persisted reply.
        cur.execute(
            "SELECT conversation FROM sourcing_requests WHERE id = %s",
            (row['id'],),
        )
        result = cur.fetchone() or {}
        conv = result.get('conversation') or []
        if not conv:
            continue
        last = conv[-1]
        if last.get('role') == 'bot':
            branch = (last.get('raw') or {}).get('branch', '')
            text = last.get('text', '')
            print(f'bot> {text}')
            if branch:
                print(f'     [{branch}]')
        print()

    try:
        db.close()
    except Exception:
        pass


if __name__ == '__main__':
    main()
