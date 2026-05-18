"""Per-unit market comps for Partner Portal — initial dealer: Encore Motorcars.

Daily flow (run from cron 10:30 ET via scripts/encore_comps_cron.sh):
    1. Load that dealer's active inventory.
    2. For each VIN, in parallel (mmr=4, rbook=2):
         a. dealer_mmr.fetch_mmr           -> MMR wholesale/retail summary
         b. vauto_bff_direct.fetch_manheim_transactions
                                           -> ~5wk of real auction sales
                                              (day-1 history backfill + ongoing)
         c. _fetch_vehicle_info + fetch_competitive_set
                                           -> rBook comps + per-comp DOL
    3. Upsert into dealer_inventory_comps (latest snapshot per inv_id).
    4. Append two rows to dealer_inventory_comp_history:
         - source='daily_run' (today's snapshot)
         - source='manheim_tx_backfill' rows synthesized from each unique
           sale date inside the ManheimTransactions window (day-1 only;
           later runs no-op if rows already exist via UNIQUE constraint).
    5. Recompute price_trend_{7d,14d,30d,60d} JSONB on each comps row
       by reading history + dealer_inventory_history.

Scope: --dealer-slug. Currently only the encore cron is installed.
Reuses the Phase-2-v1 vAuto cookie jar; bails on VAutoAuthError to
preserve cookies for the bid pipeline (same rule as
dealer_opportunity_pipeline.py).

Usage:
    python3 encore_comps_pipeline.py [--dealer-slug encore]
                                     [--mmr-concurrency 4]
                                     [--rbook-concurrency 2]
                                     [--limit N]
                                     [--skip-backfill]
                                     [--dry-run]
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

sys.path.insert(0, '/opt/expwholesale')

import psycopg2
import psycopg2.extras
import requests as _requests

from cookie_jar import CookieJar
from dealer_mmr import fetch_mmr, VAutoAuthError, VAutoMmrError
from vauto_bff_direct import (
    fetch_competitive_set,
    parse_competitive_set,
    fetch_manheim_transactions,
    VAutoAuthError as RBVAutoAuthError,
    VAutoServerError,
    VAutoBadRequestError,
)

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')
SESSION_PATH = '/opt/expwholesale/state/vauto_session.json'
VEHICLE_INFO_URL = (
    'https://slot2.bff.megazord.vauto.app.coxautoinc.com'
    '/api/appraisal/vehicleInfo?strictYMM=true'
)

log = logging.getLogger('encore_comps')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)

_auth_failed = threading.Event()


def conn():
    return psycopg2.connect(**DB)


def load_dealer_inventory(slug: str, limit: int | None = None):
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute('SELECT id, name, portal_slug FROM dealers WHERE portal_slug = %s', (slug,))
        dealer = cur.fetchone()
        if not dealer:
            raise SystemExit(f'no dealer with portal_slug={slug!r}')
        sql = """
            SELECT id AS inventory_id, dealer_id, vin, year, make, model, trim,
                   mileage, price, ext_color, first_seen_at, source_added_at
              FROM dealer_inventory
             WHERE dealer_id = %s
               AND status = 'active'
               AND vin IS NOT NULL AND length(vin) = 17
               AND year IS NOT NULL AND year >= 2010
               AND make IS NOT NULL AND model IS NOT NULL
               AND COALESCE(price, 0) > 0
             ORDER BY id
        """
        if limit:
            sql += f' LIMIT {int(limit)}'
        cur.execute(sql, (dealer['id'],))
        rows = cur.fetchall()
        return dict(dealer), [dict(r) for r in rows]


def _fetch_vehicle_info(vin, mileage, cookies, headers, timeout=12):
    """vAuto canonical vehicle decode (Series, body, drivetrain). Without
    this the competition endpoint returns ~1 result for series-named cars."""
    try:
        r = _requests.post(
            VEHICLE_INFO_URL,
            json={'vin': vin, 'odometer': mileage or 0, 'odometerUom': 'Miles'},
            headers=headers, cookies=cookies, timeout=timeout,
        )
    except _requests.RequestException:
        return None
    if r.status_code == 401:
        _auth_failed.set()
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    vi = data.get('vehicleInfo')
    if not vi or not vi.get('year') or not vi.get('make') or not vi.get('model'):
        return None
    return (vi, data.get('optionCodes'))


def _default_mileage(year, raw_mileage):
    if raw_mileage and raw_mileage > 0:
        return raw_mileage
    age = max(0, datetime.now().year - (year or datetime.now().year))
    return 8000 * age + 1000


def mmr_one(inv, cookies, headers, appraisal_id):
    if _auth_failed.is_set():
        return None
    _mileage = _default_mileage(inv.get('year'), inv.get('mileage'))
    veh = {
        'vin': inv['vin'], 'year': inv['year'], 'make': inv['make'],
        'model': inv['model'], 'mileage': _mileage,
        'trim': inv.get('trim'),
    }
    base = {'inv': inv, 'fetched_at': datetime.utcnow()}
    try:
        res = fetch_mmr(veh, cookies, headers, appraisal_id)
        base['mmr'] = {
            'wholesale_avg': res.get('wholesale_avg'),
            'wholesale_above': res.get('wholesale_above'),
            'wholesale_below': res.get('wholesale_below'),
            'retail_avg': res.get('retail_avg'),
            'retail_above': res.get('retail_above'),
            'retail_below': res.get('retail_below'),
            'grade': res.get('grade'),
            'average_odometer': res.get('average_odometer'),
            'sample_size': res.get('sample_size'),
            'no_mmr': res.get('wholesale_avg') is None,
            'fetch_error': None,
        }
        return base
    except VAutoAuthError as e:
        _auth_failed.set()
        base['mmr'] = {'no_mmr': False, 'fetch_error': f'AUTH:{e}'}
        return base
    except VAutoMmrError as e:
        base['mmr'] = {'no_mmr': False, 'fetch_error': str(e)}
        return base


def mtx_one(inv, cookies, headers):
    """ManheimTransactions backfill — ~5wk of auction sales per VIN."""
    if _auth_failed.is_set():
        return None
    _mileage = _default_mileage(inv.get('year'), inv.get('mileage'))
    veh = {
        'vin': inv['vin'], 'year': inv['year'], 'make': inv['make'],
        'model': inv['model'], 'mileage': _mileage,
        'odometer': _mileage,
        'trim': inv.get('trim'),
    }
    try:
        body = fetch_manheim_transactions(veh, cookies, headers, timeout=25)
        return {'inv': inv, 'mtx': body, 'err': None}
    except RBVAutoAuthError as e:
        _auth_failed.set()
        return {'inv': inv, 'mtx': None, 'err': f'AUTH:{e}'}
    except (VAutoServerError, VAutoBadRequestError, Exception) as e:
        return {'inv': inv, 'mtx': None, 'err': f'{type(e).__name__}:{e}'}


def rbook_one(inv, cookies, headers):
    if _auth_failed.is_set():
        return None
    used = 'unknown'
    asking_price = inv.get('price') or 0
    try:
        decoded = _fetch_vehicle_info(inv['vin'], inv.get('mileage') or 0,
                                      cookies, headers)
        if decoded:
            veh, option_codes = decoded
            body = fetch_competitive_set(
                veh, cookies, headers,
                list_price=asking_price,
                appraisal_id='unused',
                option_codes=option_codes,
                timeout=25,
            )
            used = 'canonical'
        else:
            veh = {
                'vin': inv['vin'], 'year': inv['year'],
                'make': inv['make'], 'model': inv['model'],
                'odometer': inv.get('mileage') or 0,
            }
            body = fetch_competitive_set(
                veh, cookies, headers,
                criteria_options=[],
                list_price=asking_price,
                appraisal_id='unused',
                timeout=25,
            )
            used = 'loose_fallback'

        rows = parse_competitive_set(body).get('rows') or []
        sv = (inv.get('vin') or '').upper()
        rows = [r for r in rows if (r.get('vin') or '').upper() != sv]

        prices = sorted([r['price'] for r in rows
                         if r.get('price') and r['price'] > 0])
        dols = [r.get('days_on_lot') for r in rows
                if isinstance(r.get('days_on_lot'), int) and r['days_on_lot'] >= 0]
        out = {'inv': inv, 'used': used, 'comp_count': len(rows)}
        if prices:
            n = len(prices)
            out['p25'] = prices[max(0, n // 4 - 1)]
            out['p50'] = prices[n // 2]
            out['p75'] = prices[min(n - 1, n * 3 // 4)]
        else:
            out['p25'] = out['p50'] = out['p75'] = None
        if dols:
            out['median_dol'] = sorted(dols)[len(dols) // 2]
            out['avg_dol'] = round(sum(dols) / len(dols), 1)
        else:
            out['median_dol'] = None
            out['avg_dol'] = None
        out['err'] = None
        return out
    except RBVAutoAuthError as e:
        _auth_failed.set()
        return {'inv': inv, 'err': f'AUTH:{e}'}
    except (VAutoServerError, VAutoBadRequestError, Exception) as e:
        return {'inv': inv, 'err': f'{type(e).__name__}:{e}'}


def _mtx_window_days(tx):
    dates = [t.get('date_sold') for t in tx if t.get('date_sold')]
    if not dates:
        return None
    try:
        ds = sorted(datetime.fromisoformat(d[:10]) for d in dates)
        return (ds[-1] - ds[0]).days
    except Exception:
        return None


def _pick_last_mmr_sales(tx, subject_mileage):
    """From a ManheimTransactions list, return:
        - latest: most-recent sale (any mileage)
        - close_mileage: most-recent sale within +/- 25% of subject_mileage
    Each is {sale_price, odometer, date_sold, condition, region} or None."""
    if not tx:
        return None, None
    rows = []
    for t in tx:
        ds = t.get('date_sold')
        sp = t.get('sale_price')
        if not ds or not sp:
            continue
        try:
            d = datetime.fromisoformat(ds[:10])
        except Exception:
            continue
        rows.append((d, t))
    if not rows:
        return None, None
    rows.sort(key=lambda x: x[0], reverse=True)
    latest = rows[0][1]
    close = None
    if subject_mileage and subject_mileage > 0:
        lo, hi = subject_mileage * 0.75, subject_mileage * 1.25
        for _, t in rows:
            odo = t.get('odometer')
            if odo and lo <= odo <= hi:
                close = t
                break
    return latest, close


def write_today_comps(today, mmr_by_inv, rbook_by_inv, mtx_by_inv):
    """UPSERT one row per (inv_id, today) into dealer_inventory_comps."""
    n = 0
    with conn() as c, c.cursor() as cur:
        for inv_id, mmr in mmr_by_inv.items():
            rb = rbook_by_inv.get(inv_id) or {}
            mt = mtx_by_inv.get(inv_id) or {}
            mmr_summary = mmr.get('mmr') or {}
            mmr_val = mmr_summary.get('wholesale_avg')
            mmr_sample = mmr_summary.get('sample_size')
            mtx_body = (mt.get('mtx') or {})
            tx = (mtx_body.get('transactions') or [])
            window_days = _mtx_window_days(tx)
            subject_mileage = (mmr.get('inv') or {}).get('mileage')
            latest_sale, close_sale = _pick_last_mmr_sales(tx, subject_mileage)
            comps_raw = {
                'rbook_used': rb.get('used'),
                'manheim_id': mtx_body.get('manheim_id'),
                'mtx_count': mtx_body.get('total_row_count'),
                'mtx_window_days': window_days,
                'rbook_p75': rb.get('p75'),
                'rbook_avg_dol': rb.get('avg_dol'),
                'last_mmr_sale_latest': latest_sale,
                'last_mmr_sale_close_miles': close_sale,
                'subject_mileage': subject_mileage,
            }
            cur.execute("""
                INSERT INTO dealer_inventory_comps
                  (dealer_inventory_id, snapshot_date,
                   mmr_comp_value, mmr_comp_count,
                   rbook_p25, rbook_p50, rbook_comp_count,
                   market_median_days_on_lot, market_median_source,
                   comps_raw, computed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (dealer_inventory_id, snapshot_date) DO UPDATE SET
                  mmr_comp_value            = EXCLUDED.mmr_comp_value,
                  mmr_comp_count            = EXCLUDED.mmr_comp_count,
                  rbook_p25                 = EXCLUDED.rbook_p25,
                  rbook_p50                 = EXCLUDED.rbook_p50,
                  rbook_comp_count          = EXCLUDED.rbook_comp_count,
                  market_median_days_on_lot = EXCLUDED.market_median_days_on_lot,
                  market_median_source      = EXCLUDED.market_median_source,
                  comps_raw                 = EXCLUDED.comps_raw,
                  computed_at               = NOW()
            """, (
                inv_id, today,
                mmr_val, mmr_sample,
                rb.get('p25'), rb.get('p50'), rb.get('comp_count'),
                rb.get('median_dol'),
                'rbook_competitive_set' if rb.get('median_dol') is not None else None,
                json.dumps(comps_raw, default=str),
            ))
            n += 1
        c.commit()
    return n


def append_today_history(today, dealer, inv_list, mmr_by_inv, rbook_by_inv, mtx_by_inv):
    n = 0
    with conn() as c, c.cursor() as cur:
        for inv in inv_list:
            inv_id = inv['inventory_id']
            mmr = (mmr_by_inv.get(inv_id) or {}).get('mmr') or {}
            rb = rbook_by_inv.get(inv_id) or {}
            mtx_body = ((mtx_by_inv.get(inv_id) or {}).get('mtx') or {})
            tx = mtx_body.get('transactions') or []
            cur.execute("""
                INSERT INTO dealer_inventory_comp_history
                  (dealer_inventory_id, vin, snapshot_date, asking_price,
                   mmr_wholesale_avg, mmr_retail_avg, mmr_sample_size,
                   rbook_p25, rbook_p50, rbook_p75, rbook_comp_count, rbook_avg_dol,
                   manheim_tx_count, manheim_tx_window_days, source, raw)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'daily_run',%s::jsonb)
                ON CONFLICT (dealer_inventory_id, snapshot_date, source) DO UPDATE SET
                  asking_price        = EXCLUDED.asking_price,
                  mmr_wholesale_avg   = EXCLUDED.mmr_wholesale_avg,
                  mmr_retail_avg      = EXCLUDED.mmr_retail_avg,
                  mmr_sample_size     = EXCLUDED.mmr_sample_size,
                  rbook_p25           = EXCLUDED.rbook_p25,
                  rbook_p50           = EXCLUDED.rbook_p50,
                  rbook_p75           = EXCLUDED.rbook_p75,
                  rbook_comp_count    = EXCLUDED.rbook_comp_count,
                  rbook_avg_dol       = EXCLUDED.rbook_avg_dol,
                  manheim_tx_count    = EXCLUDED.manheim_tx_count,
                  manheim_tx_window_days = EXCLUDED.manheim_tx_window_days,
                  raw                 = EXCLUDED.raw,
                  computed_at         = NOW()
            """, (
                inv_id, inv['vin'], today,
                inv.get('price'),
                mmr.get('wholesale_avg'), mmr.get('retail_avg'), mmr.get('sample_size'),
                rb.get('p25'), rb.get('p50'), rb.get('p75'),
                rb.get('comp_count'), rb.get('avg_dol'),
                mtx_body.get('total_row_count'),
                _mtx_window_days(tx),
                json.dumps({'rbook_used': rb.get('used'),
                            'manheim_id': mtx_body.get('manheim_id')}),
            ))
            n += 1
        c.commit()
    return n


def backfill_mmr_history(inv_list, mtx_by_inv):
    """Synthesize one history row per (vin, date_sold) from the
    ManheimTransactions window so the MMR-side trend chips have anchor
    points immediately on day 1. Averages sale_price within each unique
    sale date."""
    n = 0
    with conn() as c, c.cursor() as cur:
        for inv in inv_list:
            mt = mtx_by_inv.get(inv['inventory_id'])
            if not mt or not mt.get('mtx'):
                continue
            tx = (mt['mtx'].get('transactions') or [])
            if not tx:
                continue
            buckets = {}
            for t in tx:
                sp = t.get('sale_price')
                ds = t.get('date_sold')
                if not sp or not ds:
                    continue
                day = ds[:10]
                buckets.setdefault(day, []).append(float(sp))
            for day, prices in buckets.items():
                avg = sum(prices) / len(prices)
                cur.execute("""
                    INSERT INTO dealer_inventory_comp_history
                      (dealer_inventory_id, vin, snapshot_date,
                       mmr_wholesale_avg, mmr_sample_size, source, raw)
                    VALUES (%s,%s,%s,%s,%s,'manheim_tx_backfill',%s::jsonb)
                    ON CONFLICT (dealer_inventory_id, snapshot_date, source) DO UPDATE SET
                      mmr_wholesale_avg = EXCLUDED.mmr_wholesale_avg,
                      mmr_sample_size   = EXCLUDED.mmr_sample_size,
                      raw               = EXCLUDED.raw,
                      computed_at       = NOW()
                """, (inv['inventory_id'], inv['vin'], day,
                      round(avg, 2), len(prices),
                      json.dumps({'sale_prices': prices,
                                  'source_count': len(prices)})))
                n += 1
        c.commit()
    return n


def seed_from_cache(dealer, inv_list):
    """Day-1 fallback when cookies are bad: read whatever's already in
    dealer_mmr / vauto_lookups for the inventory VINs and shape it like
    a fresh pipeline run. ManheimTransactions / rBook from vauto_lookups
    if they exist (typically only for VINs that were also EW bids)."""
    mmr_by_inv = {}
    rbook_by_inv = {}
    mtx_by_inv = {}
    vins = [i['vin'] for i in inv_list]
    inv_by_vin = {i['vin']: i for i in inv_list}
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT vin, wholesale_avg, wholesale_above, wholesale_below,
                   retail_avg, retail_above, retail_below, grade,
                   average_odometer, sample_size, fetched_at
              FROM dealer_mmr
             WHERE vin = ANY(%s)
        """, (vins,))
        for r in cur.fetchall():
            inv = inv_by_vin.get(r['vin'])
            if not inv:
                continue
            mmr_by_inv[inv['inventory_id']] = {
                'inv': inv, 'fetched_at': r['fetched_at'],
                'mmr': {
                    'wholesale_avg': r['wholesale_avg'],
                    'wholesale_above': r['wholesale_above'],
                    'wholesale_below': r['wholesale_below'],
                    'retail_avg': r['retail_avg'],
                    'retail_above': r['retail_above'],
                    'retail_below': r['retail_below'],
                    'grade': r['grade'],
                    'average_odometer': r['average_odometer'],
                    'sample_size': r['sample_size'],
                    'no_mmr': r['wholesale_avg'] is None,
                    'fetch_error': None,
                },
            }
        cur.execute("""
            SELECT vl.vin, vl.rbook_competitive_set, vl.manheim_transactions
              FROM vauto_lookups vl
             WHERE vl.vin = ANY(%s)
               AND vl.rbook_competitive_set IS NOT NULL
        """, (vins,))
        for r in cur.fetchall():
            inv = inv_by_vin.get(r['vin'])
            if not inv:
                continue
            rb_rows = ((r['rbook_competitive_set'] or {}).get('rows') or [])
            rb_rows = [x for x in rb_rows
                       if (x.get('vin') or '').upper() != r['vin'].upper()]
            prices = sorted([x['price'] for x in rb_rows
                             if x.get('price') and x['price'] > 0])
            dols = [x.get('days_on_lot') for x in rb_rows
                    if isinstance(x.get('days_on_lot'), int) and x['days_on_lot'] >= 0]
            entry = {'inv': inv, 'used': 'cache', 'comp_count': len(rb_rows),
                     'err': None}
            if prices:
                n = len(prices)
                entry['p25'] = prices[max(0, n // 4 - 1)]
                entry['p50'] = prices[n // 2]
                entry['p75'] = prices[min(n - 1, n * 3 // 4)]
            else:
                entry['p25'] = entry['p50'] = entry['p75'] = None
            if dols:
                entry['median_dol'] = sorted(dols)[len(dols) // 2]
                entry['avg_dol'] = round(sum(dols) / len(dols), 1)
            else:
                entry['median_dol'] = None
                entry['avg_dol'] = None
            rbook_by_inv[inv['inventory_id']] = entry
            mt_body = r['manheim_transactions']
            if mt_body and (mt_body.get('transactions') or mt_body.get('total_row_count')):
                mtx_by_inv[inv['inventory_id']] = {'inv': inv, 'mtx': mt_body, 'err': None}
    return mmr_by_inv, rbook_by_inv, mtx_by_inv


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dealer-slug', default='encore')
    p.add_argument('--mmr-concurrency', type=int, default=4)
    p.add_argument('--rbook-concurrency', type=int, default=2)
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--skip-backfill', action='store_true',
                   help='skip ManheimTransactions sweep (use for non-day-1 runs '
                        'after history is established)')
    p.add_argument('--from-cache', action='store_true',
                   help='skip all live fetches; seed today from dealer_mmr + '
                        'vauto_lookups cache (use when cookies are bad)')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    dealer, inv_list = load_dealer_inventory(args.dealer_slug, args.limit)
    log.info('dealer=%s id=%d inventory=%d', dealer['name'], dealer['id'], len(inv_list))
    if not inv_list:
        log.warning('no eligible inventory — exiting clean')
        return 0

    today = date.today()
    mmr_by_inv = {}
    mtx_by_inv = {}
    rbook_by_inv = {}

    if args.from_cache:
        log.info('--from-cache: reading dealer_mmr + vauto_lookups')
        mmr_by_inv, rbook_by_inv, mtx_by_inv = seed_from_cache(dealer, inv_list)
        log.info('cache seed: mmr=%d rbook=%d mtx=%d',
                 len(mmr_by_inv), len(rbook_by_inv), len(mtx_by_inv))
        if args.dry_run:
            return 0
        today_n = write_today_comps(today, mmr_by_inv, rbook_by_inv, mtx_by_inv)
        hist_n = append_today_history(today, dealer, inv_list,
                                      mmr_by_inv, rbook_by_inv, mtx_by_inv)
        bf_n = backfill_mmr_history(inv_list, mtx_by_inv)
        log.info('today_comps=%d history=%d mmr_backfill=%d',
                 today_n, hist_n, bf_n)
        from encore_comps_trends import compute_and_write_trends
        trend_n = compute_and_write_trends(dealer['id'], today)
        log.info('trends computed for %d units', trend_n)
        return 0

    jar = CookieJar(SESSION_PATH)
    jar.load()
    cookies = jar.get_cookies()
    headers = jar.get_headers()
    appraisal_id = jar.get_session_appraisal_id()
    log.info('cookies captured_at=%s age=%dmin', jar.captured_at(),
             int(jar.age_seconds() / 60))

    log.info('phase 1: MMR sweep, concurrency=%d', args.mmr_concurrency)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.mmr_concurrency) as pool:
        futs = {pool.submit(mmr_one, inv, cookies, headers, appraisal_id): inv
                for inv in inv_list}
        for fut in as_completed(futs):
            r = fut.result()
            if r is None:
                continue
            mmr_by_inv[r['inv']['inventory_id']] = r
    log.info('phase 1 done in %.1fs: rows=%d', time.monotonic() - t0, len(mmr_by_inv))

    if _auth_failed.is_set():
        log.error('auth failed in phase 1 — preserving cookies, exiting 2')
        return 2

    if not args.skip_backfill:
        log.info('phase 2: ManheimTransactions sweep, concurrency=%d', args.mmr_concurrency)
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.mmr_concurrency) as pool:
            futs = {pool.submit(mtx_one, inv, cookies, headers): inv for inv in inv_list}
            for fut in as_completed(futs):
                r = fut.result()
                if r is None:
                    continue
                mtx_by_inv[r['inv']['inventory_id']] = r
        log.info('phase 2 done in %.1fs: rows=%d', time.monotonic() - t0, len(mtx_by_inv))
        if _auth_failed.is_set():
            log.error('auth failed in phase 2 — partial run, exiting 2')
            return 2

    log.info('phase 3: rBook sweep, concurrency=%d', args.rbook_concurrency)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.rbook_concurrency) as pool:
        futs = {pool.submit(rbook_one, inv, cookies, headers): inv for inv in inv_list}
        for fut in as_completed(futs):
            r = fut.result()
            if r is None:
                continue
            rbook_by_inv[r['inv']['inventory_id']] = r
    log.info('phase 3 done in %.1fs: rows=%d', time.monotonic() - t0, len(rbook_by_inv))

    if args.dry_run:
        log.info('dry-run: skipping writes')
        return 0

    today_n = write_today_comps(today, mmr_by_inv, rbook_by_inv, mtx_by_inv)
    hist_n = append_today_history(today, dealer, inv_list,
                                  mmr_by_inv, rbook_by_inv, mtx_by_inv)
    if not args.skip_backfill:
        bf_n = backfill_mmr_history(inv_list, mtx_by_inv)
        log.info('mmr backfill rows upserted=%d', bf_n)

    log.info('today_comps upserted=%d history rows upserted=%d', today_n, hist_n)

    from encore_comps_trends import compute_and_write_trends
    trend_n = compute_and_write_trends(dealer['id'], today)
    log.info('trends computed for %d units', trend_n)

    return 0


if __name__ == '__main__':
    sys.exit(main())
