#!/usr/bin/env python3
"""
Daily scan runner — loops every active dealer, scans each serially.

Designed for cron. Logs to stdout (cron redirects to /var/log/ew-dealer-scans.log).
One scan at a time to stay polite to dealer sites and stay under Gemini RPM.

Usage:
    venv/bin/python scan_all_dealers.py
    venv/bin/python scan_all_dealers.py --dealer-id 1,2   # specific subset
"""
import argparse
import sys
import time
from datetime import datetime

import dealer_scanner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dealer-id', help='Comma-separated dealer ids to scan (default: all active)')
    args = ap.parse_args()

    with dealer_scanner.get_conn() as conn, conn.cursor() as cur:
        if args.dealer_id:
            ids = tuple(int(x) for x in args.dealer_id.split(',') if x.strip().isdigit())
            cur.execute('SELECT id, name FROM dealers WHERE id = ANY(%s) AND active=TRUE ORDER BY id',
                        (list(ids),))
        else:
            cur.execute('SELECT id, name FROM dealers WHERE active=TRUE ORDER BY id')
        dealers = cur.fetchall()

    print(f'[{datetime.now().isoformat(timespec="seconds")}] scan_all: {len(dealers)} dealers',
          flush=True)

    totals = {'new': 0, 'sold': 0, 'missing': 0, 'colors': 0,
              'price_drops': 0, 'ok': 0, 'blocked': 0, 'error': 0}
    started = time.time()

    for d in dealers:
        d_start = time.time()
        print(f'\n--- dealer {d["id"]}: {d["name"]} ---', flush=True)
        try:
            scanner = dealer_scanner.DealerScanner.from_dealer_id(d['id'])
            stats = scanner.run()
            status = stats.get('status', '?')
            totals[status] = totals.get(status, 0) + 1
            totals['new']         += stats.get('new_count', 0)
            totals['sold']        += stats.get('sold_count', 0)
            totals['missing']     += stats.get('missing_count', 0)
            totals['colors']      += stats.get('colors_detected', 0)
            totals['price_drops'] += stats.get('price_drop_count', 0)
            print(f'  status={status} tier={stats.get("tier","?")} platform={stats.get("platform_detected","?")}'
                  f' vehicles={stats.get("vehicles_found",0)} new={stats.get("new_count",0)}'
                  f' sold={stats.get("sold_count",0)} drops={stats.get("price_drop_count",0)}'
                  f' colors={stats.get("colors_detected",0)} took={int(time.time()-d_start)}s',
                  flush=True)
            if stats.get('error'):
                print(f'  error: {stats["error"]}', flush=True)
        except Exception as e:
            totals['error'] += 1
            print(f'  EXCEPTION: {type(e).__name__}: {e}', flush=True)

    elapsed = int(time.time() - started)
    print(f'\n[{datetime.now().isoformat(timespec="seconds")}] scan_all complete in {elapsed}s',
          flush=True)
    print(f'  totals: new={totals["new"]} sold={totals["sold"]} missing={totals["missing"]}'
          f' drops={totals["price_drops"]} colors={totals["colors"]}', flush=True)
    print(f'  outcomes: ok={totals.get("ok",0)} blocked={totals.get("blocked",0)}'
          f' error={totals.get("error",0)}', flush=True)


if __name__ == '__main__':
    sys.exit(main() or 0)
