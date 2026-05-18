"""Trend computation for dealer_inventory_comps.

For each inventory row of `dealer_id`, compute 4 trend windows
(7d, 14d, 30d, 60d). Each window is a JSON dict with three pieces:

    {
      "own":      {"delta": -1200, "pct": -3.1, "from": 47400, "to": 46200,
                   "from_date": "2026-04-18"},
      "market":   {"delta":   -350, "pct": -0.9, "from": 38600, "to": 38250,
                   "from_date": "2026-04-18", "source": "mmr"},
      "verdict":  "outpacing_drop" | "matching_drop" | "fighting_drop" |
                  "outpacing_rise" | "matching_rise" | "fighting_rise" |
                  "flat" | "no_market_data" | "no_own_data"
    }

"Own" = dealer's asking price for this VIN. Anchored:
    - "from" = closest dealer_inventory_history.price for this inv_id with
      observed_at <= (today - N) days. Fallback: oldest available.
    - "to"   = current dealer_inventory.price.

"Market" = MMR wholesale value. Anchored:
    - "from" = closest dealer_inventory_comp_history.mmr_wholesale_avg
      for this inv_id with snapshot_date <= (today - N) days. Source can
      be either 'daily_run' (post-launch) or 'manheim_tx_backfill'
      (synthesized from ManheimTransactions sale dates on day 1).
    - "to"   = today's mmr_comp_value from dealer_inventory_comps.

Verdict explanations:
    - outpacing_drop  = own dropped FASTER than market (you're cutting hard)
    - matching_drop   = own dropped IN LINE with market drift
    - fighting_drop   = own stayed flat/rose while market dropped
    - outpacing_rise  = own rose FASTER than market
    - matching_rise   = own rose in line with rising market
    - fighting_rise   = own dropped while market rose (unusual)
    - flat            = both within +/- 0.5%
    - no_market_data  = no historical MMR anchor for that window
    - no_own_data     = no historical asking-price anchor

Threshold used for "in line": +/- 1 percentage point of market change.
"""
from __future__ import annotations
import json
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')

WINDOWS = [7, 14, 30, 60]


def _conn():
    return psycopg2.connect(**DB)


def _pct(delta, base):
    if not base or base == 0:
        return None
    return round(100.0 * delta / base, 2)


def _verdict(own_pct, mkt_pct):
    if own_pct is None and mkt_pct is None:
        return 'no_market_data'
    if own_pct is None:
        return 'no_own_data'
    if mkt_pct is None:
        # Only own data — describe own movement only
        if abs(own_pct) < 0.5:
            return 'flat'
        return 'no_market_data'
    # Both present
    own_flat = abs(own_pct) < 0.5
    mkt_flat = abs(mkt_pct) < 0.5
    if own_flat and mkt_flat:
        return 'flat'
    diff = own_pct - mkt_pct  # negative = own dropped harder than market
    in_line = abs(diff) <= 1.0
    if mkt_pct < -0.5:
        if own_pct < mkt_pct - 1.0:
            return 'outpacing_drop'
        if own_pct > mkt_pct + 1.0:
            return 'fighting_drop'
        return 'matching_drop'
    if mkt_pct > 0.5:
        if own_pct > mkt_pct + 1.0:
            return 'outpacing_rise'
        if own_pct < mkt_pct - 1.0:
            return 'fighting_rise'
        return 'matching_rise'
    # Market flat, own moved
    if own_pct < -0.5:
        return 'outpacing_drop'
    if own_pct > 0.5:
        return 'outpacing_rise'
    return 'flat'


def _row_get(row, key, idx):
    """Tolerate both dict-rows (RealDictCursor) and tuple-rows."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    return row[idx]


def _own_anchor(cur, inv_id, window_days, today):
    """Closest dealer_inventory_history price at-or-before today - N days.
    Returns (price, observed_date) or (None, None)."""
    threshold = today - timedelta(days=window_days)
    cur.execute("""
        SELECT price, observed_at::date AS d
          FROM dealer_inventory_history
         WHERE inventory_id = %s
           AND price IS NOT NULL AND price > 0
           AND observed_at::date <= %s
         ORDER BY observed_at DESC
         LIMIT 1
    """, (inv_id, threshold))
    row = cur.fetchone()
    if row is not None:
        return float(_row_get(row, 'price', 0)), _row_get(row, 'd', 1)
    cur.execute("""
        SELECT price, observed_at::date AS d
          FROM dealer_inventory_history
         WHERE inventory_id = %s
           AND price IS NOT NULL AND price > 0
         ORDER BY observed_at ASC
         LIMIT 1
    """, (inv_id,))
    row = cur.fetchone()
    if row is not None:
        d = _row_get(row, 'd', 1)
        if d and d < today:
            return float(_row_get(row, 'price', 0)), d
    return None, None


def _market_anchor(cur, inv_id, window_days, today):
    """Closest mmr_wholesale_avg at-or-before today - N days. Prefers
    daily_run rows; falls back to manheim_tx_backfill synthesized rows."""
    threshold = today - timedelta(days=window_days)
    cur.execute("""
        SELECT mmr_wholesale_avg, snapshot_date, source
          FROM dealer_inventory_comp_history
         WHERE dealer_inventory_id = %s
           AND mmr_wholesale_avg IS NOT NULL
           AND snapshot_date <= %s
         ORDER BY snapshot_date DESC,
                  CASE WHEN source='daily_run' THEN 0 ELSE 1 END
         LIMIT 1
    """, (inv_id, threshold))
    row = cur.fetchone()
    if row is not None:
        return (float(_row_get(row, 'mmr_wholesale_avg', 0)),
                _row_get(row, 'snapshot_date', 1),
                _row_get(row, 'source', 2))
    return None, None, None


def _build_window(cur, inv_id, window_days, today, current_price, current_mmr):
    own_from, own_from_date = _own_anchor(cur, inv_id, window_days, today)
    mkt_from, mkt_from_date, mkt_src = _market_anchor(cur, inv_id, window_days, today)

    own = None
    if own_from is not None and current_price is not None:
        d = float(current_price) - own_from
        own = {
            'from': round(own_from, 2),
            'to': float(current_price),
            'from_date': own_from_date.isoformat() if own_from_date else None,
            'delta': round(d, 2),
            'pct': _pct(d, own_from),
        }
    market = None
    if mkt_from is not None and current_mmr is not None:
        d = float(current_mmr) - mkt_from
        market = {
            'from': round(mkt_from, 2),
            'to': float(current_mmr),
            'from_date': mkt_from_date.isoformat() if mkt_from_date else None,
            'source': mkt_src,
            'delta': round(d, 2),
            'pct': _pct(d, mkt_from),
        }
    verdict = _verdict(own.get('pct') if own else None,
                       market.get('pct') if market else None)
    return {
        'window_days': window_days,
        'own': own,
        'market': market,
        'verdict': verdict,
    }


def compute_and_write_trends(dealer_id, today=None):
    today = today or date.today()
    n = 0
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Pull today's comps rows for dealer
        cur.execute("""
            SELECT dic.dealer_inventory_id AS inv_id,
                   dic.mmr_comp_value     AS current_mmr,
                   di.price               AS current_price
              FROM dealer_inventory_comps dic
              JOIN dealer_inventory di ON di.id = dic.dealer_inventory_id
             WHERE dic.snapshot_date = %s
               AND di.dealer_id = %s
        """, (today, dealer_id))
        rows = cur.fetchall()

        upd_cur = c.cursor()
        for r in rows:
            inv_id = r['inv_id']
            current_price = r['current_price']
            current_mmr = r['current_mmr']
            windows = {}
            for w in WINDOWS:
                windows[w] = _build_window(cur, inv_id, w, today,
                                           current_price, current_mmr)
            upd_cur.execute("""
                UPDATE dealer_inventory_comps
                   SET price_trend_7d  = %s::jsonb,
                       price_trend_14d = %s::jsonb,
                       price_trend_30d = %s::jsonb,
                       price_trend_60d = %s::jsonb,
                       computed_at     = NOW()
                 WHERE dealer_inventory_id = %s
                   AND snapshot_date = %s
            """, (
                json.dumps(windows[7], default=str),
                json.dumps(windows[14], default=str),
                json.dumps(windows[30], default=str),
                json.dumps(windows[60], default=str),
                inv_id, today,
            ))
            n += 1
        c.commit()
    return n


if __name__ == '__main__':
    import argparse, sys
    p = argparse.ArgumentParser()
    p.add_argument('--dealer-id', type=int, required=True)
    p.add_argument('--date', type=str, default=None,
                   help='YYYY-MM-DD; defaults to today')
    args = p.parse_args()
    d = date.fromisoformat(args.date) if args.date else date.today()
    n = compute_and_write_trends(args.dealer_id, d)
    print(f'computed trends for {n} units')
    sys.exit(0)
