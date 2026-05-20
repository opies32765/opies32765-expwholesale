"""dealer_intel_newsletter.py — Dealer DB Graph System: Daily Newsletter

Long-form daily brief for one dealer. Replaces the dashboard-style
3-card layout with a narrative article: yesterday's activity, this
week's price drops, peer behaviour (anonymized — peers never named),
and acquisition memos.

Pipeline:
    1. Day-over-day diff:    sold yesterday, new today, price-drops 7d
    2. Pattern memory:       historical price-drop → sale outcomes
    3. Smart peer set:       dealers that share 2+ top makes AND sit
                             within 2x avg-price-tier band of this dealer
    4. Per-car peer benchmarks: for each Encore unit, what are like-
                             units (same year+make+model+miles-band) doing
                             at peer dealers — anonymized as "peer A/B/C"
    5. Acquisition memos:    specific VINs at peers in segments where
                             this dealer is underweight
    6. Gemini Flash call:    newspaper-style article, JSON-structured
                             sections with sub-headers

Anonymization rule (HARD): the peer-data payload sent to Gemini contains
ZERO dealer names. Only "peer A", "peer B", etc. The model literally
cannot reveal a name it never received.

CLI:
    python3 dealer_intel_newsletter.py [--dealer-slug encore] [--dry-run]
"""
from __future__ import annotations
import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, '/opt/expwholesale')

import psycopg2
import psycopg2.extras

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')
GEMINI_MODEL = os.environ.get(
    'DEALER_INTEL_NEWSLETTER_MODEL', 'gemini-2.5-flash')

log = logging.getLogger('dealer_intel_newsletter')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)


def _jsonable(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return o


# ── Smart peer matching ─────────────────────────────────────────────────

def compute_peer_set(cur, dealer_id, max_peers=6):
    """Return list of peer dealer_ids that share 2+ of this dealer's top
    5 makes AND have avg_ask within ~2x band. Anonymous; names not used
    after this step (caller maps to 'peer A/B/C' for the Gemini payload).
    """
    # Self profile
    cur.execute("""
        SELECT make, COUNT(*) AS n FROM dealer_inventory
         WHERE dealer_id=%s AND status='active' AND make IS NOT NULL
         GROUP BY make ORDER BY n DESC LIMIT 5
    """, (dealer_id,))
    self_makes = {r['make'].upper() for r in cur.fetchall()}
    cur.execute("""
        SELECT ROUND(AVG(price)::numeric, 0) AS avg_ask
          FROM dealer_inventory WHERE dealer_id=%s AND status='active'
    """, (dealer_id,))
    row = cur.fetchone()
    self_avg = float(row['avg_ask']) if row and row['avg_ask'] else None

    # Candidate peers — all other dealers with any active inventory
    cur.execute("""
        SELECT d.id, d.name, COUNT(*) AS active,
               ROUND(AVG(price)::numeric, 0) AS avg_ask
          FROM dealer_inventory di
          JOIN dealers d ON d.id = di.dealer_id
         WHERE di.dealer_id <> %s AND di.status='active'
         GROUP BY d.id, d.name
        HAVING COUNT(*) >= 20
    """, (dealer_id,))
    candidates = [dict(r) for r in cur.fetchall()]
    if self_avg is None:
        return []

    scored = []
    for c in candidates:
        cid = c['id']
        cur.execute("""
            SELECT make, COUNT(*) AS n FROM dealer_inventory
             WHERE dealer_id=%s AND status='active' AND make IS NOT NULL
             GROUP BY make ORDER BY n DESC LIMIT 5
        """, (cid,))
        their_makes = {r['make'].upper() for r in cur.fetchall()}
        overlap = len(self_makes & their_makes)
        if overlap < 2:
            continue
        c_avg = float(c['avg_ask']) if c['avg_ask'] else None
        if not c_avg:
            continue
        # Price-tier band — within 0.5x..2.0x of self avg
        ratio = c_avg / self_avg
        if ratio < 0.5 or ratio > 2.5:
            continue
        # Lower deviation from self_avg + higher make overlap = better peer
        score = overlap * 10 - abs(math.log(ratio)) * 5
        scored.append((score, cid, c))

    scored.sort(key=lambda x: -x[0])
    return [c[1] for c in scored[:max_peers]]


# ── Day-over-day diffs ─────────────────────────────────────────────────

def load_diff_snapshot(cur, dealer_id):
    """Yesterday + last-7d activity for the lede. All counts + 1-3
    specific examples per category so Gemini has citations."""
    out = {}
    cur.execute("""
        SELECT id, vin, year, make, model, trim, mileage, price,
               first_seen_at::date AS first_seen, ext_color
          FROM dealer_inventory
         WHERE dealer_id=%s AND status='active'
           AND first_seen_at::date = CURRENT_DATE
         ORDER BY first_seen_at DESC LIMIT 5
    """, (dealer_id,))
    out['new_today'] = [dict(r) for r in cur.fetchall()]
    cur.execute("""
        SELECT id, vin, year, make, model, trim, mileage, price,
               first_seen_at::date AS first_seen, ext_color
          FROM dealer_inventory
         WHERE dealer_id=%s AND status='active'
           AND first_seen_at::date >= CURRENT_DATE - 7
           AND first_seen_at::date <> CURRENT_DATE
         ORDER BY first_seen_at DESC LIMIT 10
    """, (dealer_id,))
    out['new_7d'] = [dict(r) for r in cur.fetchall()]
    cur.execute("""
        SELECT id, vin, year, make, model, mileage, price, sold_at::date AS sold_on
          FROM dealer_inventory
         WHERE dealer_id=%s AND status='sold'
           AND sold_at::date BETWEEN CURRENT_DATE - 7 AND CURRENT_DATE
         ORDER BY sold_at DESC LIMIT 10
    """, (dealer_id,))
    out['sold_7d'] = [dict(r) for r in cur.fetchall()]
    cur.execute("""
        SELECT id, vin, year, make, model, trim, mileage, price,
               last_price, price_drop_amount, last_price_change_at::date AS dropped_on
          FROM dealer_inventory
         WHERE dealer_id=%s AND status='active'
           AND last_price_change_at IS NOT NULL
           AND last_price_change_at >= CURRENT_DATE - 7
         ORDER BY last_price_change_at DESC LIMIT 15
    """, (dealer_id,))
    out['price_drops_7d'] = [dict(r) for r in cur.fetchall()]
    return out


# ── Pattern memory: did past drops trigger sales? ──────────────────────

def load_pattern_memory(cur, dealer_id):
    """For each historical price drop (sold cars only), did the unit sell
    quickly afterward? Bucket by drop %."""
    cur.execute("""
        SELECT
          CASE
            WHEN price_drop_amount IS NULL OR last_price IS NULL
                 OR last_price = 0 THEN NULL
            WHEN (price_drop_amount::float / last_price::float) >= 0.08 THEN '8%%+'
            WHEN (price_drop_amount::float / last_price::float) >= 0.05 THEN '5-8%%'
            WHEN (price_drop_amount::float / last_price::float) >= 0.03 THEN '3-5%%'
            ELSE '<3%%'
          END AS drop_bucket,
          COUNT(*) AS units,
          COUNT(*) FILTER (
            WHERE sold_at IS NOT NULL
              AND last_price_change_at IS NOT NULL
              AND (sold_at - last_price_change_at) < INTERVAL '14 days'
          ) AS sold_within_14d,
          ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - last_price_change_at))/86400.0)
                FILTER (WHERE sold_at IS NOT NULL
                          AND last_price_change_at IS NOT NULL)::numeric, 1)
                AS avg_days_to_sell_after_drop
          FROM dealer_inventory
         WHERE dealer_id=%s AND status='sold'
           AND last_price_change_at IS NOT NULL
           AND price_drop_amount IS NOT NULL AND price_drop_amount > 0
         GROUP BY drop_bucket
    """, (dealer_id,))
    rows = [dict(r) for r in cur.fetchall() if r['drop_bucket']]
    return {'price_drop_outcomes': rows}


# ── Peer comparisons (anonymized) ───────────────────────────────────────

def load_peer_activity(cur, dealer_id, peer_ids):
    """For the smart-peer set: AGGREGATE 7d activity across peers by make
    (NOT per-peer breakdown — gives Gemini cleaner narrative inputs).
    Output includes only makes where the focal dealer has active stock,
    so the brief stays relevant to what THIS dealer carries."""
    if not peer_ids:
        return {'peer_count': 0, 'peer_activity_by_make': []}
    # Restrict to makes this dealer actually stocks (no point reporting
    # peer Lamborghini activity to a dealer with no Lamborghinis)
    cur.execute("SELECT DISTINCT make FROM dealer_inventory "
                "WHERE dealer_id=%s AND status='active' AND make IS NOT NULL",
                (dealer_id,))
    my_makes = [r['make'] for r in cur.fetchall()]
    if not my_makes:
        return {'peer_count': len(peer_ids), 'peer_activity_by_make': []}
    cur.execute("""
        WITH new_by_make AS (
          SELECT make, COUNT(*) AS n FROM dealer_inventory
           WHERE dealer_id = ANY(%s) AND status='active'
             AND first_seen_at >= NOW() - INTERVAL '7 days'
             AND make = ANY(%s)
           GROUP BY make
        ), sold_by_make AS (
          SELECT make, COUNT(*) AS n,
                 ROUND(AVG(price)::numeric, 0) AS avg_last_price
            FROM dealer_inventory
           WHERE dealer_id = ANY(%s) AND status='sold'
             AND sold_at >= NOW() - INTERVAL '7 days'
             AND make = ANY(%s)
           GROUP BY make
        ), all_makes AS (
          SELECT make FROM new_by_make UNION SELECT make FROM sold_by_make
        )
        SELECT a.make,
               COALESCE(n.n, 0) AS new_listings_7d,
               COALESCE(s.n, 0) AS sold_7d,
               s.avg_last_price
          FROM all_makes a
          LEFT JOIN new_by_make n  ON n.make = a.make
          LEFT JOIN sold_by_make s ON s.make = a.make
         WHERE COALESCE(n.n, 0) + COALESCE(s.n, 0) > 0
         ORDER BY (COALESCE(n.n, 0) + COALESCE(s.n, 0)) DESC
    """, (peer_ids, my_makes, peer_ids, my_makes))
    activity = [dict(r) for r in cur.fetchall()]
    return {
        'peer_count': len(peer_ids),
        'peer_activity_by_make': activity,
    }


def load_ew_acquisition_tight_match(cur, dealer_id, days=7, limit=10):
    """For up to N of this dealer's WATCH-LIST units (price_drop / sell_now
    chips), find tight EW acquisition matches in the last N days:
    SAME year, SAME make, SAME model, mileage within ±15K. Used to give
    the dealer a current wholesale-exit reference number on cars the
    system is nudging them to move."""
    cur.execute("""
        SELECT s.dealer_inventory_id, s.chip, s.days_on_lot, s.asking_price,
               di.vin, di.year, di.make, di.model, di.trim, di.mileage
          FROM dealer_intel_snapshot s
          JOIN dealer_inventory di ON di.id = s.dealer_inventory_id
         WHERE s.dealer_id = %s
           AND s.chip IN ('price_drop','sell_now')
           AND di.year IS NOT NULL AND di.make IS NOT NULL
           AND di.model IS NOT NULL
         ORDER BY s.days_on_lot DESC NULLS LAST LIMIT %s
    """, (dealer_id, limit))
    targets = [dict(r) for r in cur.fetchall()]
    out = []
    # Normalize model names because dealer_inventory uses trims
    # ("S 580", "Gle 350", "Amg® C 43") while bids uses class suffix
    # ("S-Class", "GLE-Class"). Strategy:
    #   1. Strip "Amg®"/"AMG" prefix
    #   2. Strip "-Class" suffix
    #   3. Take first meaningful token (skip if it's another noise word)
    #   4. Upper
    import re as _re
    def _norm_model(m):
        if not m:
            return ''
        s = m
        # Strip AMG prefix variants
        s = _re.sub(r'^\s*A\.?M\.?G\.?[®\s]*', '', s, flags=_re.IGNORECASE)
        # Strip "-Class" suffix
        s = s.replace('-Class', '').replace('-class', '')
        tokens = s.strip().split()
        return tokens[0].upper() if tokens else ''
    for t in targets:
        t_norm = _norm_model(t['model'])
        if not t_norm:
            continue
        cur.execute("""
            SELECT b.ai_price, b.bid_amount, b.mileage AS bid_mileage,
                   b.created_at::date AS bid_date,
                   b.model AS bid_model
              FROM bids b
             WHERE UPPER(b.make) = UPPER(%s)
               AND UPPER(
                     SPLIT_PART(
                       REGEXP_REPLACE(REPLACE(b.model, '-Class', ''),
                                       '^\\s*A\\.?M\\.?G\\.?[®\\s]*', '',
                                       'i'),
                       ' ', 1
                     )
                   ) = %s
               AND b.year = %s
               AND b.ai_price IS NOT NULL
               AND b.created_at > NOW() - (INTERVAL '1 day' * %s)
               AND ABS(COALESCE(b.mileage, 0) - COALESCE(%s, 0)) <= 15000
             ORDER BY b.created_at DESC LIMIT 5
        """, (t['make'], t_norm, t['year'], days, t.get('mileage') or 0))
        matches = [{
            'ai_price': float(r['ai_price']) if r['ai_price'] is not None else None,
            'bid_amount': float(r['bid_amount']) if r['bid_amount'] is not None else None,
            'their_mileage': r['bid_mileage'],
            'date': str(r['bid_date']),
        } for r in cur.fetchall()]
        if matches:
            avg_ai = sum(m['ai_price'] for m in matches if m['ai_price']) / len(
                [m for m in matches if m['ai_price']])
            out.append({
                'vin': t['vin'],
                'ymm': f"{t['year']} {t['make']} {t['model']}{(' ' + t['trim']) if t.get('trim') else ''}",
                'mileage': t['mileage'],
                'asking_price': float(t['asking_price']) if t['asking_price'] else None,
                'dol': int(t['days_on_lot']) if t['days_on_lot'] is not None else None,
                'chip': t['chip'],
                'ew_recent_match_count': len(matches),
                'ew_recent_avg_ai_price': round(avg_ai),
                'ew_recent_matches': matches,
                'match_window_days': days,
            })
    return out


def load_per_car_peer_benchmarks(cur, dealer_id, peer_ids, limit=8):
    """For up to N of this dealer's longest-DOL active units, find
    comparable units at peer dealers (same year ±1, same make, same
    model word match, miles band ±20K). Anonymized — no dealer names.

    Network-wide search (all non-self dealers), not restricted to the
    smartest-peer set. Tight-match per-car comparisons benefit from a
    wider lens than the macro 'peers like you' section uses."""
    # Use all non-self dealers for car-level price comparisons
    cur.execute("SELECT DISTINCT dealer_id FROM dealer_inventory "
                "WHERE dealer_id <> %s AND status='active'", (dealer_id,))
    all_peers = [r['dealer_id'] for r in cur.fetchall()]
    if not all_peers:
        return []
    # Pick this dealer's most-actionable units (highest DOL first, with
    # data populated). Cap to limit so prompt stays bounded.
    cur.execute("""
        SELECT id, vin, year, make, model, trim, mileage, price,
               COALESCE(verified_days_on_lot
                        + GREATEST(0,(CURRENT_DATE - verified_at::date)::int),
                        (CURRENT_DATE - first_seen_at::date)::int) AS dol
          FROM dealer_inventory
         WHERE dealer_id=%s AND status='active'
           AND make IS NOT NULL AND model IS NOT NULL
           AND year IS NOT NULL
         ORDER BY dol DESC NULLS LAST
         LIMIT %s
    """, (dealer_id, limit))
    targets = [dict(r) for r in cur.fetchall()]
    # Stable anonymization across all peers seen in this call so the
    # SAME dealer always maps to the SAME tag within one report run.
    anon_counter = [0]
    anon_map = {}
    def _tag(did):
        if did not in anon_map:
            anon_map[did] = f'peer_{chr(ord("A") + anon_counter[0])}'
            anon_counter[0] += 1
        return anon_map[did]
    # Same normalization as EW tight-match — handles Mercedes AMG prefix,
    # -Class suffix, and trim variants so cross-dealer model strings line
    # up (e.g. "Cle 300" matches "CLE", "Amg® C 43" matches "C-Class").
    import re as _re_mn
    def _norm_model_py(m):
        if not m:
            return ''
        s = _re_mn.sub(r'^\s*A\.?M\.?G\.?[®\s]*', '', m, flags=_re_mn.IGNORECASE)
        s = s.replace('-Class', '').replace('-class', '')
        tokens = s.strip().split()
        return tokens[0].upper() if tokens else ''
    out = []
    for t in targets:
        t_norm = _norm_model_py(t['model'])
        if not t_norm:
            continue
        cur.execute("""
            SELECT dealer_id, year, make, model, mileage, price,
                   COALESCE(verified_days_on_lot
                            + GREATEST(0,(CURRENT_DATE - verified_at::date)::int),
                            (CURRENT_DATE - first_seen_at::date)::int) AS dol
              FROM dealer_inventory
             WHERE dealer_id = ANY(%s) AND status='active'
               AND UPPER(make) = UPPER(%s)
               AND UPPER(
                     SPLIT_PART(
                       REGEXP_REPLACE(REPLACE(model, '-Class', ''),
                                       '^\\s*A\\.?M\\.?G\\.?[®\\s]*', '',
                                       'i'),
                       ' ', 1
                     )
                   ) = %s
               AND ABS(COALESCE(year,0) - %s) <= 1
               AND ABS(COALESCE(mileage,0) - %s) <= 20000
             ORDER BY price ASC LIMIT 5
        """, (all_peers, t['make'], t_norm, t['year'], t.get('mileage') or 0))
        comps = []
        for r in cur.fetchall():
            comps.append({
                'peer_tag': _tag(r['dealer_id']),
                'year': r['year'], 'make': r['make'], 'model': r['model'],
                'mileage': r['mileage'], 'price': float(r['price']) if r['price'] else None,
                'dol': int(r['dol']) if r['dol'] is not None else None,
            })
        if comps:
            out.append({
                'your_vehicle': {
                    'vin': t['vin'], 'year': t['year'], 'make': t['make'],
                    'model': t['model'], 'trim': t['trim'],
                    'mileage': t['mileage'],
                    'price': float(t['price']) if t['price'] else None,
                    'dol': int(t['dol']) if t['dol'] is not None else None,
                },
                'peer_comps': comps,
            })
    return out


def load_acquisition_candidates(cur, dealer_id, peer_ids, limit=4):
    """For segments this dealer is underweight in, list specific VINs
    at peer dealers (anonymized) the dealer could source. Peers ranked
    by their DOL on the segment (faster turnover = stronger signal).

    Network-wide candidate search (same reason as per_car comps) — the
    peer_ids smart-set isn't used here; we want to surface the BEST
    sourceable VIN regardless of overall-mix similarity."""
    cur.execute("SELECT DISTINCT dealer_id FROM dealer_inventory "
                "WHERE dealer_id <> %s AND status='active'", (dealer_id,))
    all_peers = [r['dealer_id'] for r in cur.fetchall()]
    if not all_peers:
        return []
    # Underweight segments: from dealer_intel_segments where active_count
    # is 0 or 1, joined to network_segment_performance with peers_selling>=3
    cur.execute("""
        SELECT s.segment_key, s.make, s.year_band, s.mileage_band,
               n.dealers_selling, n.sold_volume AS sold_30d, n.avg_dol_days,
               n.heat_score
          FROM network_segment_performance n
          LEFT JOIN dealer_intel_segments s
                 ON s.segment_key = n.segment_key
                AND s.dealer_id = %s
         WHERE n.snapshot_date = CURRENT_DATE
           AND n.dealers_selling >= 3
           AND n.sold_volume >= 5
           AND COALESCE(s.active_count, 0) <= 1
         ORDER BY n.heat_score DESC NULLS LAST LIMIT %s
    """, (dealer_id, limit))
    segs = [dict(r) for r in cur.fetchall()]
    anon_counter = [0]
    anon_map = {}
    def _tag(did):
        if did not in anon_map:
            anon_map[did] = f'peer_{chr(ord("A") + anon_counter[0])}'
            anon_counter[0] += 1
        return anon_map[did]
    out = []
    for s in segs:
        # Find 1-3 active VINs in this segment at peer dealers
        yb = s['year_band']
        mb = s['mileage_band']
        year_lo, year_hi = {
            'pre-2015': (1990, 2014),
            '2015-2019': (2015, 2019),
            '2020-2023': (2020, 2023),
            '2024+': (2024, 2099),
        }.get(yb, (1990, 2099))
        miles_lo, miles_hi = {
            '0-40k': (0, 40000),
            '40k-80k': (40000, 80000),
            '80k+': (80000, 999999),
        }.get(mb, (0, 999999))
        cur.execute("""
            SELECT dealer_id, year, make, model, mileage, price,
                   COALESCE(verified_days_on_lot
                            + GREATEST(0,(CURRENT_DATE - verified_at::date)::int),
                            (CURRENT_DATE - first_seen_at::date)::int) AS dol
              FROM dealer_inventory
             WHERE dealer_id = ANY(%s) AND status='active'
               AND UPPER(make) = UPPER(%s)
               AND year BETWEEN %s AND %s
               AND mileage BETWEEN %s AND %s
             ORDER BY price ASC LIMIT 3
        """, (all_peers, s['make'], year_lo, year_hi, miles_lo, miles_hi))
        candidates = [{
            'peer_tag': _tag(r['dealer_id']),
            'year': r['year'], 'make': r['make'], 'model': r['model'],
            'mileage': r['mileage'],
            'price': float(r['price']) if r['price'] else None,
            'dol': int(r['dol']) if r['dol'] is not None else None,
        } for r in cur.fetchall()]
        if candidates:
            out.append({
                'segment': f"{s['make']} {yb} {mb}",
                'network_stats': {
                    'dealers_selling': int(s['dealers_selling']),
                    'sold_30d': int(s['sold_30d']),
                    'avg_dol_at_sale': float(s['avg_dol_days']) if s['avg_dol_days'] else None,
                },
                'candidates_at_peers': candidates,
            })
    return out


def load_ew_buy_activity_7d(cur, dealer_id, top_n=8):
    """What EW has been BIDDING ON in the last 7 days, model-level
    (year-band + make + model), filtered to makes this dealer actually
    works. Specific models = better column — "BMW M3" beats "BMW"."""
    cur.execute("""
        WITH dm AS (
          SELECT DISTINCT UPPER(make) AS make
            FROM dealer_inventory
           WHERE dealer_id=%s AND make IS NOT NULL
             AND (status='active'
                  OR (status='sold'
                      AND sold_at > NOW() - INTERVAL '90 days'))
        )
        SELECT
          UPPER(b.make)  AS make,
          UPPER(b.model) AS model,
          CASE
            WHEN b.year < 2015 THEN 'pre-2015'
            WHEN b.year < 2020 THEN '2015-2019'
            WHEN b.year < 2024 THEN '2020-2023'
            ELSE '2024+'
          END AS year_band,
          COUNT(*) AS bid_count,
          ROUND(AVG(b.ai_price)::numeric, 0) AS avg_ai_price
          FROM bids b
          JOIN dm ON dm.make = UPPER(b.make)
         WHERE b.ai_price IS NOT NULL
           AND b.ai_price > 5000
           AND b.created_at > NOW() - INTERVAL '7 days'
           AND b.model IS NOT NULL
         GROUP BY 1, 2, 3
        HAVING COUNT(*) >= 2
         ORDER BY bid_count DESC
         LIMIT %s
    """, (dealer_id, top_n))
    return [dict(r) for r in cur.fetchall()]


def load_ew_sell_activity_7d(cur, dealer_id, top_n=8):
    """What EW has SOLD in the last 7 days, model-level."""
    cur.execute("""
        WITH dm AS (
          SELECT DISTINCT UPPER(make) AS make
            FROM dealer_inventory
           WHERE dealer_id=%s AND make IS NOT NULL
             AND (status='active'
                  OR (status='sold'
                      AND sold_at > NOW() - INTERVAL '90 days'))
        )
        SELECT
          UPPER(l.make_name)  AS make,
          UPPER(l.model_name) AS model,
          CASE
            WHEN l.year < 2015 THEN 'pre-2015'
            WHEN l.year < 2020 THEN '2015-2019'
            WHEN l.year < 2024 THEN '2020-2023'
            ELSE '2024+'
          END AS year_band,
          COUNT(*) AS sale_count,
          ROUND(AVG(l.sale_price)::numeric, 0) AS avg_sale_price
          FROM lsl_training l
          JOIN dm ON dm.make = UPPER(l.make_name)
         WHERE l.sale_price IS NOT NULL
           AND l.sale_price > 5000
           AND l.sold_at > NOW() - INTERVAL '7 days'
           AND l.model_name IS NOT NULL
         GROUP BY 1, 2, 3
        HAVING COUNT(*) >= 2
         ORDER BY sale_count DESC
         LIMIT %s
    """, (dealer_id, top_n))
    return [dict(r) for r in cur.fetchall()]


def load_source_opportunities(cur, dealer_id, top_n=4):
    """Sourcing pitch: segments where EW has been actively bidding AND
    the make aligns with this dealer's identity (they already sell or
    stock the make). Brand-fit matters — pitching Ford to a luxury
    dealer is noise. Filter to makes where this dealer has EITHER
    current active stock OR sold-history in the last 90 days."""
    cur.execute("""
        WITH dealer_makes AS (
          -- Makes the dealer actively works (stocks or has sold recently)
          SELECT DISTINCT UPPER(make) AS make
            FROM dealer_inventory
           WHERE dealer_id=%s AND make IS NOT NULL
             AND (status='active'
                  OR (status='sold'
                      AND sold_at > NOW() - INTERVAL '90 days'))
        ), self_seg AS (
          SELECT
            UPPER(make) AS make,
            CASE
              WHEN year < 2015 THEN 'pre-2015'
              WHEN year < 2020 THEN '2015-2019'
              WHEN year < 2024 THEN '2020-2023'
              ELSE '2024+'
            END AS year_band,
            COUNT(*) AS active_here
            FROM dealer_inventory
           WHERE dealer_id=%s AND status='active'
             AND make IS NOT NULL
           GROUP BY 1, 2
        ), ew_buys AS (
          SELECT
            UPPER(make)  AS make,
            UPPER(model) AS model,
            CASE
              WHEN year < 2015 THEN 'pre-2015'
              WHEN year < 2020 THEN '2015-2019'
              WHEN year < 2024 THEN '2020-2023'
              ELSE '2024+'
            END AS year_band,
            COUNT(*) AS ew_bid_count,
            ROUND(AVG(ai_price)::numeric, 0) AS avg_ai_price
            FROM bids
           WHERE ai_price IS NOT NULL AND ai_price > 5000
             AND created_at > NOW() - INTERVAL '7 days'
             AND make IS NOT NULL AND model IS NOT NULL
           GROUP BY 1, 2, 3
        ), net AS (
          SELECT make, year_band, dealers_selling, sold_volume,
                 avg_dol_days, heat_score
            FROM network_segment_performance
           WHERE snapshot_date = CURRENT_DATE
        )
        SELECT
          e.make,
          e.model,
          e.year_band,
          e.ew_bid_count,
          e.avg_ai_price,
          COALESCE(s.active_here, 0) AS your_active,
          n.dealers_selling,
          n.sold_volume               AS net_sold_30d,
          n.avg_dol_days              AS net_avg_dol
          FROM ew_buys e
          JOIN dealer_makes dm ON dm.make = e.make
          LEFT JOIN self_seg s
            ON s.make = e.make AND s.year_band = e.year_band
          LEFT JOIN net n
            ON UPPER(n.make) = e.make AND n.year_band = e.year_band
         WHERE e.ew_bid_count >= 2
         ORDER BY e.ew_bid_count DESC, COALESCE(n.heat_score, 0) DESC
         LIMIT %s
    """, (dealer_id, dealer_id, top_n))
    return [dict(r) for r in cur.fetchall()]


def load_whats_working(cur, dealer_id, days=7):
    """Encore's last-7d sold cars, grouped to surface 'what you did well'.
    Uses verified DOL where available."""
    cur.execute("""
        SELECT make, COUNT(*) AS sold_n,
               ROUND(AVG(
                 COALESCE(
                   verified_days_on_lot
                   + GREATEST(0,(sold_at::date - verified_at::date)::int),
                   EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400.0
                 )
               )::numeric, 1) AS avg_dol
          FROM dealer_inventory
         WHERE dealer_id=%s AND status='sold'
           AND sold_at > NOW() - (INTERVAL '1 day' * %s)
         GROUP BY make
         ORDER BY sold_n DESC, avg_dol ASC
         LIMIT 5
    """, (dealer_id, days))
    return [dict(r) for r in cur.fetchall()]


def load_sample_sizes(cur, dealer_id, peer_count):
    cur.execute("SELECT COUNT(*) AS n FROM bids WHERE ai_price IS NOT NULL "
                "AND created_at > NOW() - INTERVAL '60 days'")
    ew_bids_60d = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM lsl_training "
                "WHERE sold_at > NOW() - INTERVAL '180 days'")
    ew_sales_180d = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM dealer_inventory "
                "WHERE dealer_id <> %s AND status='sold' "
                "AND sold_at > NOW() - INTERVAL '30 days'",
                (dealer_id,))
    peer_sold_30d = cur.fetchone()['n']
    return {
        'ew_bids_60d': ew_bids_60d,
        'ew_sales_180d': ew_sales_180d,
        'peer_dealers_in_set': peer_count,
        'peer_sold_30d_network': peer_sold_30d,
    }


# ── Prompt + Gemini ─────────────────────────────────────────────────────

PROMPT_TEMPLATE = """Write a short, punchy daily column for a luxury used-car wholesale dealer.
EW (Experience Wholesale) is the platform — a wholesale broker that BUYS cars from
dealers and SELLS to other dealers. This column is from EW addressed to the dealer.

VOICE: short Slack-message-from-a-smart-trader tone. Tight, specific, no fluff.
Sentences not bullets. Each section is 2-4 sentences MAX. Whole column should be
readable in 60 seconds. Address dealer as "you", refer to EW as "we"/"us".

KEY ANGLES:
- LEAD with the dealer's WINS — what they've been selling and how fast.
- SIGNAL — what EW has been buying and selling A LOT of in the last 7 days,
  at MODEL level (not just make). "BMW M3 2024+ — 8 bought at ~$72K" beats "BMW".
- Stop there. Don't pitch sourcing, don't restate aging-inventory tactics.
  Dealers know what to do with the data — just give it cleanly.

STRICT RULES:
1. NEVER name peer dealers.
2. NEVER reveal EW's margin/gross. Cite buy prices and sell prices separately;
   do NOT subtract them or imply spread.
3. Use only data from the inputs. No invented stats. Cite SPECIFIC MODELS in
   the Hot At EW section — "BMW M3", "Porsche 911", "Mercedes E-Class" — not
   just makes.
4. Tone: collaborative trader, not marketing copy.

OUTPUT JSON:
  - headline:        short, includes day-of-week and date
  - lede:            ONE sentence — today's lot snapshot (new / sold / drops counts)
  - body_sections:   array — use exactly these titles in this order, omit any
                     with no input data:
                       * "What's Working" — whats_working_7d. The dealer's
                         strongest makes this week with sold count and avg DOL.
                         2-3 sentences max.
                       * "Hot At EW This Week" — ew_buy_activity_7d +
                         ew_sell_activity_7d. Cite SPECIFIC MODELS, not just
                         makes. Pair buy + sell when the same model appears in
                         both lists. 3-5 sentences max.

INPUTS (JSON):
"""

GEMINI_SCHEMA = {
    "type": "object",
    "required": ["headline", "lede", "body_sections"],
    "properties": {
        "headline": {"type": "string"},
        "lede":     {"type": "string"},
        "body_sections": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "body"],
                "properties": {
                    "title": {"type": "string"},
                    "body":  {"type": "string"},
                }
            }
        }
    }
}


def _gemini_client():
    try:
        from google import genai
        os.environ.setdefault(
            'GOOGLE_APPLICATION_CREDENTIALS',
            '/opt/expwholesale/google_vision_key.json')
        return genai.Client(
            vertexai=True, project='my-project-dia-492415', location='global')
    except Exception as e:
        log.error('gemini init failed: %s', e)
        return None


def call_gemini(payload):
    client = _gemini_client()
    if not client:
        return None, {}, 'no client'
    from google.genai import types
    # Plain concatenation — avoids str.format choking on '{title, body}'
    # in the prompt's instruction text.
    prompt = (PROMPT_TEMPLATE.split('INPUTS (JSON):')[0]
              + 'INPUTS (JSON):\n'
              + json.dumps(payload, default=_jsonable))
    cfg = types.GenerateContentConfig(
        max_output_tokens=4096,
        temperature=0.55,    # slight warmth for narrative
        response_mime_type='application/json',
        response_schema=GEMINI_SCHEMA,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    t0 = time.time()
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt, config=cfg)
        elapsed = int((time.time() - t0) * 1000)
        meta = {
            'generation_ms': elapsed,
            'prompt_tokens': getattr(resp.usage_metadata,
                                      'prompt_token_count', None)
                              if hasattr(resp, 'usage_metadata') else None,
            'output_tokens': getattr(resp.usage_metadata,
                                      'candidates_token_count', None)
                              if hasattr(resp, 'usage_metadata') else None,
        }
        text = resp.text or ''
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as je:
            log.error('non-JSON output: %s', je)
            log.error('text: %s', text[:400])
            return None, meta, 'parse-fail'
        # SAFETY: scrub any accidental "peer_A" / "peer A" leakage
        # (defense in depth — prompt forbids it but checking output).
        import re as _re
        leak_re = _re.compile(r'\bpeer[_\s]?[A-Z]\b')
        for k in ('headline', 'lede'):
            if parsed.get(k):
                parsed[k] = leak_re.sub('a peer dealer', parsed[k])
        for sec in parsed.get('body_sections') or []:
            for kk in ('title', 'body'):
                if sec.get(kk):
                    sec[kk] = leak_re.sub('a peer dealer', sec[kk])
        return parsed, meta, None
    except Exception as e:
        return None, {}, str(e)


# ── Persist ─────────────────────────────────────────────────────────────

def upsert_newsletter(cur, dealer_id, today, parsed, meta, ss, raw_payload):
    cur.execute("""
        INSERT INTO dealer_intel_newsletter
          (dealer_id, snapshot_date, headline, lede, body_sections,
           diff_snapshot, pattern_memory, peer_data, acquisition_data,
           sample_sizes, model_name, prompt_tokens, output_tokens,
           raw_response, generation_ms, computed_at)
        VALUES (%s, %s, %s, %s, %s::jsonb,
                %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                %s::jsonb, %s, %s, %s, %s::jsonb, %s, NOW())
        ON CONFLICT (dealer_id, snapshot_date) DO UPDATE
          SET headline       = EXCLUDED.headline,
              lede           = EXCLUDED.lede,
              body_sections  = EXCLUDED.body_sections,
              diff_snapshot  = EXCLUDED.diff_snapshot,
              pattern_memory = EXCLUDED.pattern_memory,
              peer_data      = EXCLUDED.peer_data,
              acquisition_data = EXCLUDED.acquisition_data,
              sample_sizes   = EXCLUDED.sample_sizes,
              model_name     = EXCLUDED.model_name,
              prompt_tokens  = EXCLUDED.prompt_tokens,
              output_tokens  = EXCLUDED.output_tokens,
              raw_response   = EXCLUDED.raw_response,
              generation_ms  = EXCLUDED.generation_ms,
              computed_at    = NOW()
    """, (
        dealer_id, today, parsed.get('headline'), parsed.get('lede'),
        json.dumps(parsed.get('body_sections', [])),
        json.dumps(raw_payload.get('diff') or {}, default=_jsonable),
        json.dumps(raw_payload.get('pattern_memory') or {}, default=_jsonable),
        json.dumps(raw_payload.get('peers') or {}, default=_jsonable),
        json.dumps(raw_payload.get('acquisitions') or [], default=_jsonable),
        json.dumps(ss),
        GEMINI_MODEL,
        meta.get('prompt_tokens'), meta.get('output_tokens'),
        json.dumps(parsed, default=_jsonable),
        meta.get('generation_ms'),
    ))


# ── Main ────────────────────────────────────────────────────────────────

def run(dealer_slug, dry_run=False):
    today = date.today()
    with psycopg2.connect(**DB) as db:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM dealers WHERE portal_slug=%s",
                        (dealer_slug,))
            dealer = cur.fetchone()
            if not dealer:
                log.error('dealer %r not found', dealer_slug)
                return 2
            dealer_id = dealer['id']
            log.info('dealer=%s id=%d dry=%s', dealer['name'],
                     dealer_id, dry_run)

            peer_ids = compute_peer_set(cur, dealer_id)
            log.info('peer_set size=%d (anonymized)', len(peer_ids))
            diff = load_diff_snapshot(cur, dealer_id)
            pattern = load_pattern_memory(cur, dealer_id)
            peers = load_peer_activity(cur, dealer_id, peer_ids)
            per_car = load_per_car_peer_benchmarks(cur, dealer_id, peer_ids)
            acquisitions = load_acquisition_candidates(cur, dealer_id, peer_ids)
            ew_recent = load_ew_acquisition_tight_match(cur, dealer_id, days=7)
            ew_buy_7d = load_ew_buy_activity_7d(cur, dealer_id)
            ew_sell_7d = load_ew_sell_activity_7d(cur, dealer_id)
            sourcing_opps = load_source_opportunities(cur, dealer_id)
            whats_working = load_whats_working(cur, dealer_id)
            ss = load_sample_sizes(cur, dealer_id, len(peer_ids))

            log.info('diff: new_today=%d new_7d=%d sold_7d=%d drops_7d=%d',
                     len(diff['new_today']), len(diff['new_7d']),
                     len(diff['sold_7d']), len(diff['price_drops_7d']))
            log.info('pattern: %s', pattern)
            log.info('per_car_peer_comps=%d acquisition_candidates=%d '
                     'ew_recent_tight_matches=%d',
                     len(per_car), len(acquisitions), len(ew_recent))

            payload = {
                'dealer_name': dealer['name'],
                'date': str(today),
                'diff': diff,
                'whats_working_7d': whats_working,
                'ew_buy_activity_7d': ew_buy_7d,
                'ew_sell_activity_7d': ew_sell_7d,
                'sourcing_opportunities': sourcing_opps,
                'pattern_memory': pattern,
                'peers': peers,
                'per_car_peer_comps': per_car,
                'acquisitions': acquisitions,
                'ew_recent_acquisitions_tight_match': ew_recent,
                'sample_sizes': ss,
            }

            parsed, meta, err = call_gemini(payload)
            if err or not parsed:
                log.error('gemini failed: %s', err)
                return 1
            log.info('gemini ok prompt_tokens=%s output_tokens=%s ms=%s',
                     meta.get('prompt_tokens'), meta.get('output_tokens'),
                     meta.get('generation_ms'))
            log.info('headline: %s', parsed.get('headline'))
            log.info('sections: %d', len(parsed.get('body_sections') or []))
            if dry_run:
                print(json.dumps(parsed, indent=2))
                return 0
            upsert_newsletter(cur, dealer_id, today, parsed, meta, ss, payload)
            db.commit()
            log.info('persisted')
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dealer-slug', default='encore')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    try:
        return run(args.dealer_slug, args.dry_run)
    except Exception as e:
        log.exception('failed: %s', e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
