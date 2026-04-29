"""
One-shot seeder for the `vauto_session` table.

Reads a curl bash dump (the kind you get from Chrome DevTools "Copy as cURL"),
parses the cookies + auth headers, and upserts the row.

Usage:
    # Tonight: paste a fresh curl from a working Chrome on Worker-2 into
    # /tmp/vauto_curl.txt, then:
    python seed_vauto_session.py /tmp/vauto_curl.txt --label oscarpas \
                                  --refreshed-by manual

    # Tomorrow: cookie_keeper service replaces this with auto-extraction.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

# Adding the project dir so we can import vauto_api
sys.path.insert(0, str(Path(__file__).parent))
from vauto_api import VautoSession  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('curl_file', help='Path to a file containing a curl bash command')
    ap.add_argument('--label', default='oscarpas',
                    help='Session label (the worker references this)')
    ap.add_argument('--refreshed-by', default='manual',
                    choices=('manual', 'cookie_keeper'),
                    help='Who/what populated this row')
    ap.add_argument('--db-url', default=os.environ.get(
        'DATABASE_URL',
        'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'),
        help='Postgres URL (defaults to env DATABASE_URL)')
    args = ap.parse_args()

    text = Path(args.curl_file).read_text(encoding='utf-8')
    sess = VautoSession.from_curl(text)

    if not sess.cookies:
        print('ERROR: no cookies parsed. Check the curl file format.',
              file=sys.stderr)
        sys.exit(2)
    if not sess.entity_id:
        print('ERROR: no entity_id parsed (need currententityid header).',
              file=sys.stderr)
        sys.exit(2)
    if 'vAutoAuth' not in sess.cookies:
        print('WARNING: vAutoAuth cookie missing — session may be incomplete.',
              file=sys.stderr)

    print(f'Parsed {len(sess.cookies)} cookies, entity_id={sess.entity_id[:20]}...')

    conn = psycopg2.connect(args.db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO vauto_session
                    (label, cookies, entity_id, platform_user_id,
                     user_agent, refreshed_by, refreshed_at)
                VALUES (%s, %s::jsonb, %s, %s, %s, %s, NOW())
                ON CONFLICT (label) DO UPDATE SET
                    cookies          = EXCLUDED.cookies,
                    entity_id        = EXCLUDED.entity_id,
                    platform_user_id = EXCLUDED.platform_user_id,
                    user_agent       = EXCLUDED.user_agent,
                    refreshed_by     = EXCLUDED.refreshed_by,
                    refreshed_at     = NOW()
                RETURNING id, refreshed_at
            """, (
                args.label,
                json.dumps(sess.cookies),
                sess.entity_id,
                sess.platform_user_id,
                sess.user_agent,
                args.refreshed_by,
            ))
            row = cur.fetchone()
            conn.commit()
            print(f'OK — vauto_session id={row[0]} label={args.label} '
                  f'refreshed_at={row[1]}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
