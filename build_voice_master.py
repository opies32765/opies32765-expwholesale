#!/usr/bin/env python3
"""build_voice_master.py — populate voice_ymm_master from LSL + dealer
inventory + vAuto live + prior bids.

Run modes:
  - one-shot: ./build_voice_master.py            (refresh all)
  - one YMM:  ./build_voice_master.py --year 2024 --make BMW --model M2
  - dry-run:  ./build_voice_master.py --dry-run  (no DB writes)

Run from /opt/expwholesale so voice_agent + vauto_bff_direct imports work.
Designed for nightly cron (~3AM EDT) plus on-demand operator refresh.
"""
from __future__ import annotations

import argparse
import json as _json
import os
import re
import sqlite3
import sys
import time
import traceback
from collections import Counter
from typing import Optional

sys.path.insert(0, '/opt/expwholesale')

import psycopg2
import psycopg2.extras


DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')
LSL_DB_PATH = '/opt/livesaleslog/crm.db'


def _connect_ew():
    c = psycopg2.connect(**DB)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


def _connect_lsl():
    if not os.path.exists(LSL_DB_PATH):
        return None
    c = sqlite3.connect(f'file:{LSL_DB_PATH}?mode=ro', uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ── Step 1: seed YMM list from LSL purchases (last 90 days) ─────────────
def collect_seed_ymms(days_back: int = 90, limit: int = 500) -> list[dict]:
    """Return ranked YMM seed list from LSL purchases.

    Strategy: pull every deal in last N days, regex-extract year/make/model
    from vehicle_info ('2024 Gmc Hummer EV 3X Suv'), bucket by YMM, rank
    by purchase frequency. Top N go into the master table."""
    c = _connect_lsl()
    if c is None:
        print('[seed] LSL crm.db not found — aborting', flush=True)
        return []
    cur = c.cursor()
    cur.execute("""
        SELECT vehicle_info, make_name, purchase_cost, sale_price,
               front_value, sold_at
          FROM deals
         WHERE sold_at > date('now', ?)
           AND vehicle_info IS NOT NULL
           AND purchase_cost > 0
        ORDER BY sold_at DESC
    """, (f'-{days_back} days',))
    rows = cur.fetchall()
    c.close()

    # YMM keyed by (year, make_canonical, model_canonical)
    ymm_bucket: dict[tuple, list[dict]] = {}
    STOP = {'4D', '2D', '4WD', '2WD', 'AWD', 'RWD', 'FWD', 'COUPE',
            'SEDAN', 'CONVERTIBLE', 'WAGON', 'HATCHBACK', 'TRUCK',
            'SPORT', 'UTILITY', 'VEHICLE', 'SUV', 'PICKUP', 'CREW',
            'CAB', 'DOUBLE', 'EXTENDED', 'REGULAR'}
    for r in rows:
        info = (r['vehicle_info'] or '').strip()
        if not info:
            continue
        # Year is always the first token
        m_yr = re.match(r'^(19[89]\d|20\d\d)\s+(.+)$', info)
        if not m_yr:
            continue
        try:
            year = int(m_yr.group(1))
        except Exception:
            continue
        rest = m_yr.group(2).strip()

        # Use LSL.make_name as canonical (handles "Land Rover", "Mercedes-Benz",
        # "Aston Martin", "Alfa Romeo" — multi-word makes).
        make_canon = (r['make_name'] or '').strip()
        if not make_canon:
            continue

        # Strip the make from the front of `rest` so what remains is
        # model + trim. LSL stores vehicle_info with the same casing as
        # make_name (usually).
        make_words = make_canon.split()
        rest_words = rest.split()
        # Case-insensitive prefix match — pop make-length words off the front
        if (len(rest_words) >= len(make_words) and
                all(rest_words[i].upper() == make_words[i].upper()
                    for i in range(len(make_words)))):
            rest_words = rest_words[len(make_words):]
        elif rest_words and rest_words[0].upper() == make_canon.upper().replace(' ', '').replace('-', '')[:len(rest_words[0])]:
            rest_words = rest_words[1:]
        else:
            # Make doesn't match prefix — fall back to skipping one token
            rest_words = rest_words[1:]

        # Take 1-3 tokens as model, stopping at STOP words
        model_clean = []
        for t in rest_words:
            t_clean = re.sub(r'[^A-Za-z0-9-]', '', t)
            if not t_clean:
                continue
            if t_clean.upper() in STOP:
                break
            model_clean.append(t_clean)
            if len(model_clean) >= 3:
                break
        if not model_clean:
            continue
        model = ' '.join(model_clean)
        key = (year, make_canon, model)
        ymm_bucket.setdefault(key, []).append({
            'purchase_cost': float(r['purchase_cost'] or 0),
            'sale_price':    float(r['sale_price'] or 0),
            'front_value':   float(r['front_value'] or 0),
            'sold_at':       r['sold_at'],
        })

    # Rank by frequency; tie-break by recency
    # Rank by frequency DESC, then most recent purchase DESC
    ranked = sorted(
        ymm_bucket.items(),
        key=lambda kv: (len(kv[1]),
                        max((d['sold_at'] or '') for d in kv[1])),
        reverse=True,
    )
    out = []
    for rank, ((year, make, model), deals) in enumerate(ranked[:limit], 1):
        purchases = [d['purchase_cost'] for d in deals if d['purchase_cost'] > 0]
        sales     = [d['sale_price']    for d in deals if d['sale_price']    > 0]
        fronts    = [d['front_value']   for d in deals if d['front_value']   != 0]
        out.append({
            'year': year, 'make': make, 'model': model,
            'rank': rank,
            'count': len(deals),
            'avg_purchase': sum(purchases) / len(purchases) if purchases else None,
            'median_purchase': sorted(purchases)[len(purchases)//2] if purchases else None,
            'avg_sale':     sum(sales) / len(sales) if sales else None,
            'avg_front':    sum(fronts) / len(fronts) if fronts else None,
            'last_date':    max(d['sold_at'] for d in deals)[:10] if deals else None,
            'sample':       deals[:5],
        })
    return out


# ── Step 2: enrich one YMM with all data sources ────────────────────────
def enrich_ymm(seed: dict, dry_run: bool = False) -> dict:
    """Pull all surrounding intel for one YMM: partner inventory + sold +
    drops, vAuto live, prior bids. Returns a dict ready to UPSERT."""
    year = seed['year']
    make = seed['make']
    model = seed['model']
    make_u = make.strip().upper()
    model_u = model.strip().upper()

    # Use LSL avg miles as the "typical" mileage band. Defaults to 15K if
    # we don't have a good signal.
    # Note: LSL deals don't carry miles directly, so fall back to 15K.
    miles_target = 15000
    miles_band = (miles_target // 5000) * 5000

    row = {
        'year': year, 'make': make, 'model': model,
        'miles_band': miles_band,
        'seed_source': 'lsl_90d',
        'seed_rank': seed['rank'],
        'lsl_count_90d': seed['count'],
        'lsl_avg_purchase_cost': seed['avg_purchase'],
        'lsl_median_purchase':   seed['median_purchase'],
        'lsl_avg_sale_price':    seed['avg_sale'],
        'lsl_avg_front':         seed['avg_front'],
        'lsl_last_purchase_date': seed['last_date'],
        'lsl_recent_deals_sample': _json.dumps(seed['sample'], default=str),
    }

    # PARTNER INVENTORY enrichment from dealer_inventory
    try:
        with _connect_ew() as c, c.cursor() as cur:
            cur.execute("""
                SELECT d.name AS dealer, di.year, di.make, di.model, di.trim,
                       di.mileage, di.price, di.url, di.status,
                       di.first_seen_at, di.last_seen_at, di.source_added_at,
                       di.price_drop_amount, di.price_drop_at, di.last_price,
                       (CURRENT_DATE - COALESCE(di.source_added_at,
                                                di.first_seen_at)::date) AS dol
                  FROM dealer_inventory di
                  JOIN dealers d ON d.id = di.dealer_id
                 WHERE di.year = %s
                   AND UPPER(di.make)   = %s
                   AND UPPER(di.model) LIKE %s
            """, (year, make_u, f'%{model_u}%'))
            inv = [dict(r) for r in cur.fetchall()]

        active = [r for r in inv if r['status'] == 'active' and r.get('price')]
        sold30 = [r for r in inv if r['status'] == 'sold' and r.get('last_seen_at')
                  and (time.time() - r['last_seen_at'].timestamp()) < 30*86400]
        drops  = [r for r in active if r.get('price_drop_amount') and r['price_drop_amount'] > 0]

        row['partner_active_count'] = len(active)
        if active:
            prices = [r['price'] for r in active if r.get('price')]
            row['partner_active_avg_price'] = sum(prices) / len(prices) if prices else None
            milages = [r['mileage'] for r in active if r.get('mileage')]
            row['partner_active_avg_miles'] = int(sum(milages) / len(milages)) if milages else None
            dols = [r['dol'] for r in active if r.get('dol') is not None]
            row['partner_active_avg_dol'] = (sum(dols) / len(dols)) if dols else None
            # Sample: closest 3 to miles_target
            active_sorted = sorted(active,
                                   key=lambda r: abs((r.get('mileage') or 0) - miles_target))
            row['partner_active_sample'] = _json.dumps([{
                'dealer': r['dealer'], 'price': float(r['price']),
                'mileage': r.get('mileage'), 'days_on_lot': r.get('dol'),
                'trim': r.get('trim'), 'url': r.get('url'),
            } for r in active_sorted[:3]], default=str)
        else:
            row['partner_active_sample'] = '[]'

        row['partner_sold_30d_count'] = len(sold30)
        row['partner_sold_30d_sample'] = _json.dumps([{
            'dealer': r['dealer'], 'last_seen': r['last_seen_at'].isoformat() if r.get('last_seen_at') else None,
            'price': float(r['price']) if r.get('price') else None,
            'mileage': r.get('mileage'), 'trim': r.get('trim'),
        } for r in sold30[:3]], default=str)

        row['partner_with_price_drop'] = len(drops)
        row['partner_total_drop_amount'] = sum(r['price_drop_amount'] for r in drops) if drops else None
    except Exception as e:
        print(f'[enrich] partner_inv err for {year} {make} {model}: {e}', flush=True)
        row['partner_active_sample'] = '[]'
        row['partner_sold_30d_sample'] = '[]'

    # ── DEALER INVENTORY FULL — all active + recent sold + price drops ─
    try:
        with _connect_ew() as c, c.cursor() as cur:
            cur.execute("""
                SELECT d.name AS dealer, di.year, di.make, di.model, di.trim,
                       di.mileage, di.price, di.last_price, di.url, di.status,
                       di.price_drop_amount, di.price_drop_at,
                       di.first_seen_at, di.last_seen_at, di.source_added_at,
                       (CURRENT_DATE - COALESCE(di.source_added_at,
                                                di.first_seen_at)::date) AS dol
                  FROM dealer_inventory di
                  JOIN dealers d ON d.id = di.dealer_id
                 WHERE di.year = %s
                   AND UPPER(di.make)   = %s
                   AND UPPER(di.model) LIKE %s
            """, (year, make_u, f'%{model_u}%'))
            all_inv = [dict(r) for r in cur.fetchall()]

        for r in all_inv:
            for k in ('first_seen_at', 'last_seen_at', 'price_drop_at'):
                if r.get(k):
                    r[k] = r[k].isoformat()
            for k in ('price', 'last_price', 'price_drop_amount'):
                if r.get(k) is not None:
                    r[k] = float(r[k])

        active_all = [r for r in all_inv if r['status'] == 'active' and r.get('price')]
        sold_recent = [r for r in all_inv if r['status'] == 'sold'
                       and r.get('last_seen_at')
                       and r['last_seen_at'] > (
                           __import__('datetime').datetime.utcnow().date()
                           - __import__('datetime').timedelta(days=30)).isoformat()]
        drops = [r for r in active_all if r.get('price_drop_amount') and r['price_drop_amount'] > 0]

        row['dealer_inv_active_full']  = _json.dumps(active_all,  default=str)
        row['dealer_inv_sold_full']    = _json.dumps(sold_recent, default=str)
        row['dealer_inv_price_drops']  = _json.dumps(drops,       default=str)

        # DOL stats
        active_dols = sorted([r['dol'] for r in active_all if r.get('dol') is not None])
        if active_dols:
            row['dealer_active_min_dol'] = active_dols[0]
            row['dealer_active_max_dol'] = active_dols[-1]
            row['dealer_active_p50_dol'] = active_dols[len(active_dols)//2]
        sold_dols = [r['dol'] for r in sold_recent if r.get('dol') is not None]
        if sold_dols:
            row['dealer_sold_avg_dol'] = int(sum(sold_dols) / len(sold_dols))
    except Exception as e:
        print(f'[enrich] dealer_inv_full err for {year} {make} {model}: {e}', flush=True)

    # ── rBook + MANHEIM AGGREGATION from vauto_lookups (90d) ────────────
    try:
        with _connect_ew() as c, c.cursor() as cur:
            cur.execute("""
                SELECT b.id AS bid_id, b.mileage, b.created_at,
                       v.rbook_competitive_set, v.manheim_transactions
                  FROM bids b
                  JOIN vauto_lookups v ON v.bid_id = b.id
                 WHERE b.year = %s
                   AND UPPER(b.make)   = %s
                   AND UPPER(b.model) LIKE %s
                   AND b.created_at > NOW() - INTERVAL '90 days'
                   AND (v.rbook_competitive_set IS NOT NULL
                        OR v.manheim_transactions IS NOT NULL)
            """, (year, make_u, f'%{model_u}%'))
            vauto_rows = [dict(r) for r in cur.fetchall()]

        # Flatten rBook comps across all bids; dedup by VIN; keep closest-mile top 20
        rb_all = []
        rb_seen_vins = set()
        cert_count = 0; pend_count = 0; cf_clean = 0
        dol_pool = []
        for vr in vauto_rows:
            rbcs = vr.get('rbook_competitive_set') or {}
            for r in (rbcs.get('rows') or []):
                if not isinstance(r, dict): continue
                vin = (r.get('vin') or '').strip().upper() or None
                if vin and vin in rb_seen_vins: continue
                if vin: rb_seen_vins.add(vin)
                price = r.get('price') or r.get('effective_price')
                if not price or not (2000 <= float(price) <= 1_000_000): continue
                rb_all.append({
                    'vin': vin, 'price': int(float(price)),
                    'mileage': r.get('mileage'),
                    'days_on_lot': r.get('days_on_lot'),
                    'dealer': r.get('dealer'), 'state': r.get('dealer_state'),
                    'trim': r.get('trim'),
                    'is_certified': r.get('is_certified'),
                    'pending_sale': r.get('pending_sale'),
                    'carfax_clean_title': r.get('carfax_clean_title'),
                })
                if r.get('is_certified'): cert_count += 1
                if r.get('pending_sale'): pend_count += 1
                if r.get('carfax_clean_title'): cf_clean += 1
                if r.get('days_on_lot') is not None:
                    try: dol_pool.append(int(r['days_on_lot']))
                    except Exception: pass

        # Closest-mile top 20 (sorted by abs delta to typical mileage)
        rb_sorted = sorted(rb_all, key=lambda r: abs((r.get('mileage') or 0) - miles_target))
        row['rbook_pool_count'] = len(rb_all)
        row['rbook_top_comps']  = _json.dumps(rb_sorted[:20], default=str)
        if dol_pool:
            dol_pool.sort()
            row['rbook_median_dol'] = dol_pool[len(dol_pool)//2]
        if rb_all:
            row['rbook_certified_pct']    = round(100 * cert_count / len(rb_all), 1)
            row['rbook_pending_pct']      = round(100 * pend_count / len(rb_all), 1)
            row['rbook_carfax_clean_pct'] = round(100 * cf_clean  / len(rb_all), 1)

        # Flatten Manheim transactions
        mh_all = []
        for vr in vauto_rows:
            mht = vr.get('manheim_transactions') or {}
            # MH_KEY_FIX_2026_05_27: inner key is 'transactions', not 'rows'
            for r in (mht.get('transactions') or mht.get('rows') or []):
                if not isinstance(r, dict): continue
                price = r.get('sale_price') or r.get('price')
                if not price or not (1000 <= float(price) <= 1_000_000): continue
                mh_all.append({
                    'price': int(float(price)),
                    'mileage': r.get('odometer') or r.get('mileage'),
                    'sale_date': r.get('date_sold') or r.get('sale_date') or r.get('date'),
                    'condition_grade': r.get('condition'),
                    'auction': r.get('auction'), 'region': r.get('region'),
                    'color': r.get('color'),
                })
        # Sort by sale date desc, keep last 10
        mh_all.sort(key=lambda r: (r.get('sale_date') or ''), reverse=True)
        row['manheim_pool_count'] = len(mh_all)
        row['manheim_recent_sales'] = _json.dumps(mh_all[:10], default=str)
        if mh_all:
            prices_sorted = sorted([r['price'] for r in mh_all])
            row['manheim_median_sale'] = prices_sorted[len(prices_sorted)//2]
            grades = [float(r['condition_grade']) for r in mh_all
                      if r.get('condition_grade') and str(r['condition_grade']).replace('.','').isdigit()]
            if grades:
                row['manheim_avg_condition'] = round(sum(grades) / len(grades), 2)
            row['manheim_last_sale_at'] = mh_all[0].get('sale_date')[:10] if mh_all[0].get('sale_date') else None
    except Exception as e:
        print(f'[enrich] vauto-agg err for {year} {make} {model}: {e}', flush=True)

    # ── ACCUTRADE AGGREGATION from accutrade_lookups (90d) ──────────────
    # ACCUTRADE_AGG_2026_05_27 — voice_ymm_master had accutrade_* columns
    # but no populator before today. Fills accutrade_pool_count, the 3
    # avg-dollar fields (guaranteed/trade_in/market), and a closest-mile
    # local_comps sample.
    try:
        with _connect_ew() as c, c.cursor() as cur:
            cur.execute("""
                SELECT b.mileage, a.guaranteed_offer, a.trade_in,
                       a.market_avg, a.local_comps
                  FROM bids b
                  JOIN accutrade_lookups a ON a.bid_id = b.id
                 WHERE b.year = %s
                   AND UPPER(b.make)   = %s
                   AND UPPER(b.model) LIKE %s
                   AND b.created_at > NOW() - INTERVAL '90 days'
                   AND a.not_available IS NOT TRUE
            """, (year, make_u, f'%{model_u}%'))
            accu_rows = [dict(r) for r in cur.fetchall()]

        if accu_rows:
            row['accutrade_pool_count'] = len(accu_rows)

            def _avg_int(field):
                vals = [float(r[field]) for r in accu_rows
                        if r.get(field) is not None
                        and 1000 <= float(r[field]) <= 1_000_000]
                return int(sum(vals) / len(vals)) if vals else None

            row['accutrade_avg_guaranteed'] = _avg_int('guaranteed_offer')
            row['accutrade_avg_trade_in']   = _avg_int('trade_in')
            row['accutrade_avg_market']     = _avg_int('market_avg')

            # Flatten local_comps across all bids.
            # Sample shape: [{"make": "KIA", "model": "SORENTO", "year": 2024,
            #                 "miles": 52175, "price": 21777, "text": "..."}]
            # Some rows are UI-scrape artifacts (null make/model) — skip those.
            comps_all = []
            for ar in accu_rows:
                lc = ar.get('local_comps')
                if not lc:
                    continue
                if isinstance(lc, str):
                    try:
                        lc = _json.loads(lc)
                    except Exception:
                        continue
                rows_in = lc.get('rows') if isinstance(lc, dict) else lc
                if not isinstance(rows_in, list):
                    continue
                for r in rows_in:
                    if not isinstance(r, dict):
                        continue
                    price = r.get('price')
                    mk = r.get('make')
                    if not price or not mk:
                        continue
                    try:
                        p_int = int(float(price))
                    except (TypeError, ValueError):
                        continue
                    if not (1000 <= p_int <= 1_000_000):
                        continue
                    comps_all.append({
                        'price': p_int,
                        'miles': r.get('miles'),
                        'year':  r.get('year'),
                        'make':  mk,
                        'model': r.get('model'),
                        'text':  (r.get('text') or '')[:200],
                    })
            comps_sorted = sorted(
                comps_all,
                key=lambda c: abs((c.get('miles') or 0) - miles_target),
            )
            row['accutrade_local_comps_top'] = _json.dumps(
                comps_sorted[:10], default=str
            )
        else:
            row['accutrade_pool_count'] = 0
            row['accutrade_local_comps_top'] = '[]'
    except Exception as e:
        print(f'[enrich] accutrade-agg err for {year} {make} {model}: {e}',
              flush=True)


    # PRIOR BIDS enrichment
    try:
        with _connect_ew() as c, c.cursor() as cur:
            cur.execute("""
                SELECT b.id, b.mileage, b.bid_amount, b.created_at,
                       v.mmr, v.rbook
                  FROM bids b
                  JOIN vauto_lookups v ON v.bid_id = b.id
                 WHERE b.year = %s
                   AND UPPER(b.make)   = %s
                   AND UPPER(b.model) LIKE %s
                   AND b.created_at > NOW() - INTERVAL '90 days'
                 ORDER BY b.created_at DESC LIMIT 50
            """, (year, make_u, f'%{model_u}%'))
            bids = [dict(r) for r in cur.fetchall()]
        if bids:
            mmrs   = [r['mmr']   for r in bids if r.get('mmr')   and r['mmr']   > 1000]
            rbooks = [r['rbook'] for r in bids if r.get('rbook') and r['rbook'] > 1000]
            offers = [float(r['bid_amount']) for r in bids if r.get('bid_amount')]
            row['prior_bid_count_90d'] = len(bids)
            row['prior_bid_avg_mmr']   = int(sum(mmrs) / len(mmrs)) if mmrs else None
            row['prior_bid_avg_rbook'] = int(sum(rbooks) / len(rbooks)) if rbooks else None
            row['prior_bid_avg_our_offer'] = (sum(offers) / len(offers)) if offers else None
            row['prior_bids_sample'] = _json.dumps([{
                'bid_id': r['id'],
                'mileage': r.get('mileage'),
                'our_bid': float(r['bid_amount']) if r.get('bid_amount') else None,
                'mmr': r.get('mmr'), 'rbook': r.get('rbook'),
                'date': r['created_at'].isoformat() if r.get('created_at') else None,
            } for r in bids[:3]], default=str)
        else:
            row['prior_bid_count_90d'] = 0
            row['prior_bids_sample'] = '[]'
    except Exception as e:
        print(f'[enrich] prior_bids err for {year} {make} {model}: {e}', flush=True)
        row['prior_bids_sample'] = '[]'

    # LIVE vAUTO (via voice_agent's existing helper)
    try:
        from voice_agent import _fetch_live_vauto_for_ymm
        live = _fetch_live_vauto_for_ymm(year=year, make=make, model=model,
                                         miles=miles_target)
        if live:
            mmr = live.get('live_mmr') or {}
            rb  = live.get('live_rbook') or {}
            rb_sum = rb.get('summary') or {}
            row['mmr_wholesale_avg'] = int(mmr.get('average_auction_price') or 0) or None
            row['mmr_avg_odometer']  = int(mmr.get('average_odometer')      or 0) or None
            row['rbook_listing_count'] = rb.get('n') or 0
            row['rbook_median_retail'] = int(rb_sum.get('median') or 0) or None
            row['rbook_p25']         = int(rb_sum.get('p25') or 0)    or None
            row['rbook_p75']         = int(rb_sum.get('p75') or 0)    or None
            row['rbook_closest_anchor'] = _json.dumps(rb.get('closest_anchor'),
                                                     default=str) if rb.get('closest_anchor') else None
            row['vauto_refreshed_at'] = 'now'
    except Exception as e:
        print(f'[enrich] vauto err for {year} {make} {model}: {e}', flush=True)

    return row


def pre_bake_narrative(row: dict, dry_run: bool = False) -> dict:
    """Run Sonnet + Google Cloud TTS to pre-generate the spoken response
    for this YMM at its typical mileage band. Stored in the master row;
    voice bot plays it back instantly on cache hit."""
    if dry_run:
        return row
    # Build a synthetic ctx from the row we just enriched
    try:
        import voice_agent as v
        ctx = v._context_from_master(
            row, year=row['year'], make=row['make'], model=row['model'],
            miles=row.get('miles_band'), msrp=None,
        )
        compact = v._context_compact(ctx)
        prompt = (
            f"The operator just asked: \"What's a {row['year']} {row['make']} {row['model']} "
            f"with around {row.get('miles_band'):,} miles worth?\"\n\n"
            f"[Pre-loaded valuation context — no need to call the tool]:\n"
            f"{_json.dumps(compact, default=str)[:4500]}\n\n"
            f"Give the spoken response per the system prompt rules. "
            f"Lead with the wholesale target, cite ONE concrete anchor, "
            f"end naturally."
        )
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        resp = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=220, temperature=0.4,
            system=v._SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': prompt}],
        )
        narrative = ' '.join(b.text for b in resp.content if b.type == 'text').strip()
        if not narrative:
            return row

        # TTS the narrative
        from google.cloud import texttospeech as tts_mod
        tts_client = tts_mod.TextToSpeechClient()
        si = tts_mod.SynthesisInput(text=narrative)
        voice = tts_mod.VoiceSelectionParams(language_code='en-US',
                                              name='en-US-Neural2-F')
        ac = tts_mod.AudioConfig(audio_encoding=tts_mod.AudioEncoding.MP3,
                                  speaking_rate=1.05)
        tts_resp = tts_client.synthesize_speech(input=si, voice=voice, audio_config=ac)
        import base64
        row['sonnet_narrative'] = narrative
        row['tts_audio_b64']    = base64.b64encode(tts_resp.audio_content).decode('ascii')
    except Exception as e:
        print(f"[pre-bake] err for {row['year']} {row['make']} {row['model']}: {e}",
              flush=True)
    return row


# ── Step 3: UPSERT into voice_ymm_master ───────────────────────────────
def upsert(row: dict, dry_run: bool = False):
    if dry_run:
        print(f'[dry-run] would upsert {row["year"]} {row["make"]} {row["model"]} '
              f'(band={row["miles_band"]}, lsl_n={row.get("lsl_count_90d")}, '
              f'partner_active={row.get("partner_active_count")}, '
              f'mmr=${row.get("mmr_wholesale_avg")}, rbook_n={row.get("rbook_listing_count")})',
              flush=True)
        return
    cols = [c for c in row.keys() if c != 'vauto_refreshed_at']
    placeholders = ','.join(['%s'] * len(cols))
    col_list = ','.join(cols)
    update_clause = ','.join(f'{c}=EXCLUDED.{c}' for c in cols if c not in
                              ('year', 'make', 'model', 'miles_band'))
    update_clause += ', vauto_refreshed_at=NOW(), refreshed_at=NOW(), '
    update_clause += "expires_at = NOW() + INTERVAL '36 hours'"
    sql = f"""
        INSERT INTO voice_ymm_master ({col_list}, vauto_refreshed_at, refreshed_at, expires_at)
        VALUES ({placeholders}, NOW(), NOW(), NOW() + INTERVAL '36 hours')
        ON CONFLICT (year, make, model, miles_band) DO UPDATE SET {update_clause}
    """
    args = [row[c] for c in cols]
    with _connect_ew() as c, c.cursor() as cur:
        cur.execute(sql, args)
        c.commit()


# ── Main ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days-back', type=int, default=90)
    parser.add_argument('--limit',     type=int, default=200,
                        help='top N YMMs to refresh')
    parser.add_argument('--dry-run',   action='store_true')
    parser.add_argument('--year',  type=int)
    parser.add_argument('--make')
    parser.add_argument('--model')
    parser.add_argument('--pre-bake', action='store_true',
                        help='Generate Sonnet narrative + TTS audio per YMM (adds ~7s/YMM)')
    args = parser.parse_args()

    t0 = time.time()

    if args.year and args.make and args.model:
        seeds = [{'year': args.year, 'make': args.make, 'model': args.model,
                  'rank': 1, 'count': 0, 'avg_purchase': None,
                  'median_purchase': None, 'avg_sale': None,
                  'avg_front': None, 'last_date': None, 'sample': []}]
    else:
        print(f'[seed] collecting YMMs from LSL last {args.days_back} days...',
              flush=True)
        seeds = collect_seed_ymms(days_back=args.days_back, limit=args.limit)
        print(f'[seed] LSL ranked: {len(seeds)} YMMs', flush=True)

        # Also seed from frequently-BID YMMs (bids table last 90d).
        # Catches cars we look at often but haven't purchased — e.g., BMW M2,
        # cars in partner inventory we appraise but skip.
        existing_keys = {(s['year'], s['make'].upper(), s['model'].upper())
                         for s in seeds}
        try:
            with _connect_ew() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT year, make, model, COUNT(*) AS bid_count
                      FROM bids
                     WHERE year IS NOT NULL AND make IS NOT NULL AND model IS NOT NULL
                       AND created_at > NOW() - INTERVAL '%s days'
                     GROUP BY year, make, model
                     HAVING COUNT(*) >= 2
                     ORDER BY bid_count DESC
                     LIMIT %s
                """, (args.days_back, args.limit))
                bid_rows = list(cur.fetchall())
            added = 0
            for br in bid_rows:
                key = (br['year'], (br['make'] or '').upper(), (br['model'] or '').upper())
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                seeds.append({
                    'year': br['year'],
                    'make': br['make'].strip().title() if br['make'].isupper() else br['make'].strip(),
                    'model': br['model'].strip(),
                    'rank': len(seeds) + 1,
                    'count': 0, 'avg_purchase': None, 'median_purchase': None,
                    'avg_sale': None, 'avg_front': None,
                    'last_date': None, 'sample': [],
                })
                # also mark seed_source on this seed (handled in enrich)
                added += 1
                if len(seeds) >= args.limit: break
            print(f'[seed] +{added} frequent-bid YMMs added (total {len(seeds)})',
                  flush=True)
        except Exception as e:
            print(f'[seed] frequent-bid lookup failed: {e}', flush=True)

    ok, errs = 0, 0
    for i, seed in enumerate(seeds, 1):
        try:
            row = enrich_ymm(seed, dry_run=args.dry_run)
            if args.pre_bake and not args.dry_run:
                # Only pre-bake when we have at least SOME signal worth
                # generating from. Skip empty rows to save API spend.
                has_signal = (row.get('mmr_wholesale_avg')
                              or row.get('rbook_pool_count') and row['rbook_pool_count'] > 5
                              or row.get('lsl_count_90d') and row['lsl_count_90d'] >= 2)
                if has_signal:
                    row = pre_bake_narrative(row, dry_run=False)
            upsert(row, dry_run=args.dry_run)
            print(f'[{i}/{len(seeds)}] {row["year"]} {row["make"]} {row["model"]:25} '
                  f'lsl={row.get("lsl_count_90d")} '
                  f'partner_active={row.get("partner_active_count")} '
                  f'mmr=${row.get("mmr_wholesale_avg")} '
                  f'rbook={row.get("rbook_listing_count")}', flush=True)
            ok += 1
        except Exception as e:
            errs += 1
            print(f'[{i}/{len(seeds)}] ERR {seed["year"]} {seed["make"]} {seed["model"]}: {e}',
                  flush=True)
            traceback.print_exc()

    elapsed = int(time.time() - t0)
    print(f'\n[done] {ok} ok, {errs} errors in {elapsed}s', flush=True)


if __name__ == '__main__':
    main()
