#!/usr/bin/env python3
"""cookie_bridge.py — sync vauto_session TABLE -> vauto_session.json FILE.

The Phase 2 BFF API (cookie_jar.py) reads cookies from
/opt/expwholesale/state/vauto_session.json. The verifier + EW workers push
cookies to the postgres `vauto_session` TABLE via /api/vauto/refresh_cookies.

These two stores are not connected. This bridge reads the freshest table row
that has a valid vAutoAuth and writes it to the file in the format cookie_jar
expects. Runs every 30s via systemd timer.

Atomic write via mkstemp+os.replace so cookie_jar.py never sees a partial file.
Preserves session_appraisal_id across rewrites (table doesn't carry it).
"""
import argparse
import json
import os
import sys
import tempfile

import psycopg2
import psycopg2.extras

DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
TARGET = os.environ.get(
    'EW_VAUTO_SESSION_FILE', '/opt/expwholesale/state/vauto_session.json')
# Labels to prefer (newest-with-valid-vAutoAuth wins). Order matters: tried
# in priority order. Adding worker labels here as workers start pushing.
LABEL_PRIORITY = (os.environ.get('EW_BRIDGE_LABELS')
                  or 'oscarpas,vm-worker-1,vm-worker-2,vm-worker-3,'
                     'vm-worker-4,vm-worker-5,vm-worker-6,vm-worker-7,'
                     'vm-worker-8,vm-worker-9,vm-worker-10,vm-worker-11,'
                     'vm-worker-12,vm-worker-13').split(',')

# Fallback domain for cookies that the table doesn't carry domain for.
# cookie_jar.get_cookies() only uses name+value, so domain is cosmetic for
# the BFF — but we set it to a plausible value for cleanliness.
DEFAULT_DOMAIN = '.coxautoinc.com'

# session_appraisal_id is a real Cox appraisalId captured once by prewarmer.py.
# It's account-stable per cookie_jar docstring. Table does NOT carry it.
# We preserve from existing file; fall back to env var on first install.
FALLBACK_APPRAISAL_ID = os.environ.get(
    'EW_VAUTO_SESSION_APPRAISAL_ID',
    'qWNKSOaUPCW6x4lPKnM8iojBTMhHy415I2iIv9GiCZ4=')


def pick_freshest(conn):
    """Find the most-recently-refreshed row with vAutoAuth among preferred labels.
    Returns (label, cookies_dict, entity_id, platform_user_id, user_agent, refreshed_at)
    or None if no valid source."""
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT label, cookies, entity_id, platform_user_id, user_agent, refreshed_at
              FROM vauto_session
             WHERE label = ANY(%s)
               AND cookies ? 'vAutoAuth'
             ORDER BY refreshed_at DESC
             LIMIT 1
        """, (LABEL_PRIORITY,))
        row = cur.fetchone()
    return row


def transform(row, existing_sai):
    """Build the file payload from a table row."""
    cookies_dict = row['cookies']
    cookies_list = [
        {
            'name': name,
            'value': value,
            'domain': DEFAULT_DOMAIN,
            'path': '/',
            'expires': -1,
            'secure': True,
            'httpOnly': False,
            'sameSite': 'Lax',
        }
        for name, value in cookies_dict.items()
    ]
    headers = {
        'platformuserid': row['platform_user_id'] or '',
        'appraisalentityid': row['entity_id'] or '',
        'currententityid': row['entity_id'] or '',
        'accept': 'application/json',
        'content-type': 'application/json',
        'referer': 'https://provision.vauto.app.coxautoinc.com/',
        'user-agent': row['user_agent'] or
                      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/147.0.0.0 Safari/537.36',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-ch-ua': '"Chromium";v="147", "Not-A.Brand";v="99"',
    }
    return {
        'captured_at': row['refreshed_at'].strftime('%Y-%m-%dT%H:%M:%S'),
        'cookies': cookies_list,
        'headers': headers,
        'session_appraisal_id': existing_sai or FALLBACK_APPRAISAL_ID,
    }


def read_existing_sai(path):
    """Recover session_appraisal_id from current file so we don't lose it."""
    try:
        with open(path, encoding='utf-8') as fp:
            return json.load(fp).get('session_appraisal_id')
    except Exception:
        return None


def write_atomic(path, payload):
    """Write JSON atomically — tempfile + os.replace. cookie_jar.py reads
    via mtime check and never sees a partial."""
    target_dir = os.path.dirname(path) or '.'
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='vauto_session_', suffix='.tmp',
                               dir=target_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            json.dump(payload, fp, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o640)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='Print what would be written without touching disk')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    conn = psycopg2.connect(DB_URL)
    try:
        row = pick_freshest(conn)
    finally:
        conn.close()
    if row is None:
        print('[bridge] no vauto_session row with valid vAutoAuth among '
              f'labels {LABEL_PRIORITY}', file=sys.stderr)
        return 2

    existing_sai = read_existing_sai(TARGET)
    payload = transform(row, existing_sai)

    if args.dry_run:
        print(f'[bridge] DRY-RUN — would write to {TARGET}:')
        print(f'  source label:    {row["label"]}')
        print(f'  refreshed_at:    {row["refreshed_at"]}')
        print(f'  cookies count:   {len(payload["cookies"])}')
        print(f'  vAutoAuth present: '
              f'{any(c["name"] == "vAutoAuth" for c in payload["cookies"])}')
        print(f'  headers keys:    {list(payload["headers"].keys())}')
        print(f'  session_appraisal_id: {payload["session_appraisal_id"]}')
        print(f'  captured_at:     {payload["captured_at"]}')
        return 0

    write_atomic(TARGET, payload)
    if args.verbose:
        print(f'[bridge] wrote {len(payload["cookies"])} cookies '
              f'(source label={row["label"]}, refreshed_at={row["refreshed_at"]})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
