"""dealer_intel_network.py — Dealer DB Graph System: Layer 1

Computes network-wide segment performance ONCE per nightly run. Same
data is used by every dealer-level intel pass to identify acquisition
blind spots (segments hot at peers, underweight at this dealer).

Excludes the dealer that's currently being analyzed only at the
join-time level (LEFT JOIN in dealer_intel.py), so this module always
processes the FULL peer-dealer fleet without per-dealer exclusion.

Window: last N days (default 30). DOL formula matches dealer_db.
best_age_days_filter (verified_days_on_lot + days-since-verification).

Heat score: rewards segments with multiple distinct dealers turning
multiple units fast. Tunable so the surfaced list isn't dominated by
narrow specs that one dealer ran a fire sale on.

    heat = sold_volume * sqrt(dealers_selling) / max(15, avg_dol)

CLI:
    python3 dealer_intel_network.py [--window-days 30] [--dry-run]

Runs BEFORE dealer_intel.py in the cron chain.
"""
from __future__ import annotations
import argparse
import logging
import math
import sys
from datetime import date

sys.path.insert(0, '/opt/expwholesale')

import psycopg2
import psycopg2.extras

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')

log = logging.getLogger('dealer_intel_network')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)


def year_band(year):
    if year is None:
        return 'unknown'
    if year < 2015:
        return 'pre-2015'
    if year < 2020:
        return '2015-2019'
    if year < 2024:
        return '2020-2023'
    return '2024+'


def mileage_band(miles):
    if miles is None or miles <= 0:
        return 'unknown'
    if miles < 40000:
        return '0-40k'
    if miles < 80000:
        return '40k-80k'
    return '80k+'


def segment_key(make, year, miles):
    mk = (make or 'UNKNOWN').strip().lower()
    return f'{mk}|{year_band(year)}|{mileage_band(miles)}'


def compute_heat(sold_volume, dealers_selling, avg_dol):
    """Higher = more interesting acquisition target. Multi-dealer
    activity + fast turnover beats single-dealer one-off runs."""
    if sold_volume <= 0 or dealers_selling <= 0:
        return 0.0
    dol_eff = max(15.0, float(avg_dol) if avg_dol else 60.0)
    return round(sold_volume * math.sqrt(dealers_selling) / dol_eff, 2)


def run(window_days=30, dry_run=False):
    today = date.today()
    with psycopg2.connect(**DB) as db:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Pull every active + sold-in-window unit across ALL dealers
            # (we'll roll up into segments in Python so we don't have to
            # encode the year/mileage CASE into SQL).
            cur.execute("""
                SELECT id, dealer_id, year, make, model, mileage,
                       status, sold_at, verified_days_on_lot, verified_at,
                       first_seen_at,
                       CASE
                         WHEN status='sold' AND sold_at IS NOT NULL THEN
                           COALESCE(
                             verified_days_on_lot
                             + GREATEST(0, (sold_at::date - verified_at::date)::int),
                             GREATEST(0, EXTRACT(EPOCH FROM
                               (sold_at - first_seen_at))/86400.0)::int)
                         WHEN status='active' THEN
                           CASE
                             WHEN verified_days_on_lot IS NOT NULL
                                  AND verified_at IS NOT NULL THEN
                               verified_days_on_lot
                               + GREATEST(0,
                                   (CURRENT_DATE - verified_at::date)::int)
                             ELSE
                               GREATEST(0,
                                 (CURRENT_DATE - first_seen_at::date)::int)
                           END
                         ELSE NULL
                       END AS dol
                  FROM dealer_inventory
                 WHERE (status='active'
                        OR (status='sold' AND sold_at IS NOT NULL
                            AND sold_at > NOW() - (INTERVAL '1 day' * %s)))
            """, (window_days,))
            rows = cur.fetchall()

            segments = {}
            for r in rows:
                k = segment_key(r['make'], r['year'], r['mileage'])
                seg = segments.setdefault(k, {
                    'segment_key': k,
                    'make': (r['make'] or 'UNKNOWN').strip(),
                    'year_band': year_band(r['year']),
                    'mileage_band': mileage_band(r['mileage']),
                    'sold_dealers': set(),
                    'sold_dols': [],
                    'active_count': 0,
                    'active_dealers': set(),
                })
                if r['status'] == 'sold':
                    seg['sold_dealers'].add(r['dealer_id'])
                    if r['dol'] is not None:
                        seg['sold_dols'].append(int(r['dol']))
                elif r['status'] == 'active':
                    seg['active_count'] += 1
                    seg['active_dealers'].add(r['dealer_id'])

            rollups = []
            for k, seg in segments.items():
                sold_n = len(seg['sold_dols'])
                avg_dol = round(sum(seg['sold_dols']) / sold_n, 1) if sold_n else None
                median_dol = None
                if seg['sold_dols']:
                    s = sorted(seg['sold_dols'])
                    n = len(s)
                    median_dol = float(s[n // 2] if n % 2
                                        else (s[n // 2 - 1] + s[n // 2]) / 2.0)
                rollups.append({
                    **seg,
                    'sold_volume': sold_n,
                    'avg_dol_days': avg_dol,
                    'median_dol_days': median_dol,
                    'dealers_selling': len(seg['sold_dealers']),
                    'dealers_with_active': len(seg['active_dealers']),
                    'heat_score': compute_heat(sold_n, len(seg['sold_dealers']),
                                                avg_dol),
                })

            log.info('computed %d segments across %d dealers · window=%dd',
                     len(rollups),
                     len({r['dealer_id'] for r in rows}),
                     window_days)

            if dry_run:
                top = sorted(rollups, key=lambda x: -x['heat_score'])[:10]
                log.info('top 10 by heat:')
                for s in top:
                    log.info('  heat=%6.2f %s · %d dealers · %d sold · %sd avg DOL · %d active',
                             s['heat_score'], s['segment_key'],
                             s['dealers_selling'], s['sold_volume'],
                             s['avg_dol_days'], s['active_count'])
                return 0

            for s in rollups:
                cur.execute("""
                    INSERT INTO network_segment_performance
                      (snapshot_date, window_days, segment_key, make,
                       year_band, mileage_band, dealers_selling, sold_volume,
                       avg_dol_days, median_dol_days, active_count,
                       dealers_with_active, heat_score, computed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (snapshot_date, segment_key) DO UPDATE
                      SET dealers_selling     = EXCLUDED.dealers_selling,
                          sold_volume         = EXCLUDED.sold_volume,
                          avg_dol_days        = EXCLUDED.avg_dol_days,
                          median_dol_days     = EXCLUDED.median_dol_days,
                          active_count        = EXCLUDED.active_count,
                          dealers_with_active = EXCLUDED.dealers_with_active,
                          heat_score          = EXCLUDED.heat_score,
                          window_days         = EXCLUDED.window_days,
                          computed_at         = NOW()
                """, (
                    today, window_days, s['segment_key'], s['make'],
                    s['year_band'], s['mileage_band'], s['dealers_selling'],
                    s['sold_volume'], s['avg_dol_days'], s['median_dol_days'],
                    s['active_count'], s['dealers_with_active'],
                    s['heat_score'],
                ))
            db.commit()
            log.info('persisted %d network segments', len(rollups))
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--window-days', type=int, default=30)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    try:
        return run(args.window_days, args.dry_run)
    except Exception as e:
        log.exception('failed: %s', e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
