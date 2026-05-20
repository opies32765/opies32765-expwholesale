"""backfill_int_color.py — one-shot int_color backfill for DealerOn-hosted
dealers. Re-fetches each active VDP URL, parses the info__label/info__value
template, persists int_color (and ext_color where missing) into
dealer_inventory.

DEALERON_COLOR_2026_05_20: companion to the regex/extractors added to
dealer_scanner.py. Same regex, same per-VDP fetch path, but operates on
existing rows without re-running the full daily scan.

Usage:
    python3 backfill_int_color.py [--dealer-slug encore] [--dry-run]
    python3 backfill_int_color.py --all       # every active row, any dealer

Cost: zero (no AI), just HTTP fetches against dealer sites.
"""
from __future__ import annotations
import argparse
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, '/opt/expwholesale')

import psycopg2
import psycopg2.extras
import requests

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
      'AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/124.0.0.0 Safari/537.36')

_INT = re.compile(r'>Interior Color</span>\s*<span[^>]*?title="([^"]+)"', re.I)
_EXT = re.compile(r'>Exterior Color</span>\s*<span[^>]*?title="([^"]+)"', re.I)

log = logging.getLogger('backfill_int_color')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)


def fetch_and_parse(row):
    try:
        r = requests.get(row['url'], timeout=15,
                          headers={'User-Agent': UA}, allow_redirects=True)
        if r.status_code != 200:
            return row['id'], None, None, f'http={r.status_code}'
        html = r.text
        ic = _INT.search(html)
        ec = _EXT.search(html)
        ic_v = ic.group(1).strip() if ic else None
        ec_v = ec.group(1).strip() if ec else None
        return row['id'], ic_v, ec_v, None
    except Exception as e:
        return row['id'], None, None, f'{type(e).__name__}: {e}'


def run(dealer_slug=None, dry_run=False, all_dealers=False, include_sold=False):
    with psycopg2.connect(**DB) as db:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # include_sold: scrape sold cars too (URLs sometimes still
            # resolve for a window after sale — best-effort for the
            # newsletter's color_velocity_90d signal which reads sold history).
            sold_clause = "" if include_sold else "AND di.status='active'"
            if all_dealers:
                cur.execute(f"""
                    SELECT di.id, di.vin, di.dealer_id, d.name AS dealer_name, di.url, di.status
                      FROM dealer_inventory di
                      JOIN dealers d ON d.id = di.dealer_id
                     WHERE di.url IS NOT NULL {sold_clause}
                       AND (di.int_color IS NULL OR di.int_color = '')
                     ORDER BY di.dealer_id, di.id
                """)
            else:
                slug = dealer_slug or 'encore'
                cur.execute(f"""
                    SELECT di.id, di.vin, di.dealer_id, d.name AS dealer_name, di.url, di.status
                      FROM dealer_inventory di
                      JOIN dealers d ON d.id = di.dealer_id
                     WHERE di.url IS NOT NULL {sold_clause}
                       AND d.portal_slug = %s
                       AND (di.int_color IS NULL OR di.int_color = '')
                     ORDER BY di.id
                """, (slug,))
            rows = [dict(r) for r in cur.fetchall()]
            log.info('candidates=%d', len(rows))
            if not rows:
                return 0

            done = 0
            int_filled = 0
            ext_filled = 0
            errors = 0
            with ThreadPoolExecutor(max_workers=6) as pool:
                futs = {pool.submit(fetch_and_parse, r): r for r in rows}
                for fut in as_completed(futs):
                    inv_id, int_c, ext_c, err = fut.result()
                    if err:
                        errors += 1
                        log.warning('id=%d err=%s', inv_id, err)
                        continue
                    if not (int_c or ext_c):
                        continue
                    if dry_run:
                        done += 1
                        if int_c:
                            int_filled += 1
                        if ext_c:
                            ext_filled += 1
                        continue
                    sets = []
                    vals = []
                    if int_c:
                        sets.append('int_color=%s')
                        vals.append(int_c)
                        int_filled += 1
                    if ext_c:
                        # Only fill ext_color when DB is blank — don't
                        # clobber existing values (scanner may have
                        # captured something via JSON-LD already).
                        sets.append('ext_color = COALESCE(NULLIF(ext_color,\'\'), %s)')
                        vals.append(ext_c)
                        ext_filled += 1
                    sets.append('updated_at = NOW()')
                    vals.append(inv_id)
                    cur.execute(
                        f"UPDATE dealer_inventory SET {', '.join(sets)} "
                        f"WHERE id = %s", vals)
                    done += 1
            if not dry_run:
                db.commit()
            log.info('processed=%d int_filled=%d ext_filled=%d errors=%d',
                     done, int_filled, ext_filled, errors)
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dealer-slug', default=None,
                    help='specific dealer (e.g. encore). default: encore.')
    p.add_argument('--all', action='store_true',
                    help='all DealerOn-hosted dealers (any slug)')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--include-sold', action='store_true',
                    help='Also backfill sold cars (URL may be stale).')
    args = p.parse_args()
    try:
        return run(args.dealer_slug, args.dry_run, args.all, args.include_sold)
    except Exception as e:
        log.exception('failed: %s', e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
