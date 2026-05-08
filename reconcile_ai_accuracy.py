"""reconcile_ai_accuracy.py — match AI assessments to actual LSL purchases.

For each bid with an ai_assessment_log entry AND a closing deal in LSL,
upsert a row in ai_accuracy with the delta between AI recommendation and
actual purchase price.

Sources:
  EW Postgres:    bids (vin, year, make, model, mileage)
                  ai_assessment_log (final_price, confidence_low/high)
  LSL SQLite:     deals (vin_no, purchase_cost, created_at)

Usage:
  reconcile_ai_accuracy.py              # backfill all history
  reconcile_ai_accuracy.py --days 7    # incremental, last 7d of LSL purchases
  reconcile_ai_accuracy.py --bid 932   # one specific bid

Cron (Contabo 1, daily 6 AM ET — after LSL extract.py refreshes):
  0 6 * * *  /opt/expwholesale/venv/bin/python3 /opt/expwholesale/reconcile_ai_accuracy.py --days 7 >> /var/log/ai_accuracy.log 2>&1
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

EW_DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
LSL_DB_PATH = '/opt/livesaleslog/crm.db'


def fetch_lsl_purchases(since_iso: str | None) -> dict[str, dict]:
    """Return {vin: deal_row} from LSL where purchase_cost > 0.
    If since_iso is set, only return deals created at/after that ISO timestamp.
    Multiple deals on same VIN: keep the newest by created_at."""
    con = sqlite3.connect(LSL_DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    sql = ("SELECT id, code, vin_no, vehicle_info, purchase_cost, sale_price, "
           "msrp, status, type, supplier_name, source_name, sold_at, created_at, "
           "modified_at, days_since_purchase "
           "FROM deals WHERE vin_no IS NOT NULL AND length(vin_no) >= 11 "
           "AND purchase_cost IS NOT NULL AND purchase_cost > 0")
    params = []
    if since_iso:
        sql += " AND (created_at >= ? OR modified_at >= ?)"
        params = [since_iso, since_iso]
    sql += " ORDER BY created_at ASC"
    cur.execute(sql, params)
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        vin = (r['vin_no'] or '').strip().upper()
        if not vin: continue
        # Newer record overwrites older for same VIN — terminal state wins
        out[vin] = dict(r)
    con.close()
    return out


def fetch_bid_assessments(bid_filter: int | None = None) -> list[dict]:
    """Return latest ai_assessment_log row per bid_id, joined with bids (vin/ymm)."""
    con = psycopg2.connect(EW_DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = con.cursor()
    where_extra = ' AND b.id = %s' if bid_filter else ''
    params = [bid_filter] if bid_filter else []
    cur.execute(f"""
        SELECT b.id AS bid_id, b.vin, b.year, b.make, b.model, b.mileage,
               l.id AS log_id, l.final_price, l.confidence_low, l.confidence_high,
               l.created_at AS ai_assessed_at
        FROM bids b
        JOIN LATERAL (
            SELECT id, final_price, confidence_low, confidence_high, created_at
            FROM ai_assessment_log
            WHERE bid_id = b.id AND final_price IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
        ) l ON TRUE
        WHERE b.vin IS NOT NULL AND length(b.vin) = 17{where_extra}
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def upsert_accuracy(rows: list[dict]) -> int:
    if not rows:
        return 0
    con = psycopg2.connect(EW_DB_URL)
    cur = con.cursor()
    cur.executemany("""
        INSERT INTO ai_accuracy (
            bid_id, vin, year, make, model, mileage,
            ai_recommendation, ai_confidence_low, ai_confidence_high,
            ai_assessment_log_id, ai_assessed_at,
            actual_purchase_cost, actual_purchased_at,
            lsl_deal_id, lsl_deal_code,
            delta, delta_pct, abs_delta_pct, in_confidence_range,
            reconciled_at
        ) VALUES (
            %(bid_id)s, %(vin)s, %(year)s, %(make)s, %(model)s, %(mileage)s,
            %(ai_recommendation)s, %(ai_confidence_low)s, %(ai_confidence_high)s,
            %(ai_assessment_log_id)s, %(ai_assessed_at)s,
            %(actual_purchase_cost)s, %(actual_purchased_at)s,
            %(lsl_deal_id)s, %(lsl_deal_code)s,
            %(delta)s, %(delta_pct)s, %(abs_delta_pct)s, %(in_confidence_range)s,
            NOW()
        )
        ON CONFLICT (bid_id) DO UPDATE SET
            vin=EXCLUDED.vin, year=EXCLUDED.year, make=EXCLUDED.make,
            model=EXCLUDED.model, mileage=EXCLUDED.mileage,
            ai_recommendation=EXCLUDED.ai_recommendation,
            ai_confidence_low=EXCLUDED.ai_confidence_low,
            ai_confidence_high=EXCLUDED.ai_confidence_high,
            ai_assessment_log_id=EXCLUDED.ai_assessment_log_id,
            ai_assessed_at=EXCLUDED.ai_assessed_at,
            actual_purchase_cost=EXCLUDED.actual_purchase_cost,
            actual_purchased_at=EXCLUDED.actual_purchased_at,
            lsl_deal_id=EXCLUDED.lsl_deal_id, lsl_deal_code=EXCLUDED.lsl_deal_code,
            delta=EXCLUDED.delta, delta_pct=EXCLUDED.delta_pct,
            abs_delta_pct=EXCLUDED.abs_delta_pct,
            in_confidence_range=EXCLUDED.in_confidence_range,
            reconciled_at=NOW()
    """, rows)
    con.commit()
    affected = cur.rowcount
    con.close()
    return affected


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--days', type=int, default=None,
                   help='Only reconcile LSL purchases from last N days (incremental)')
    p.add_argument('--bid', type=int, default=None,
                   help='One specific bid_id (debug)')
    args = p.parse_args()

    since = None
    if args.days:
        since_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
        since = since_dt.strftime('%Y-%m-%dT%H:%M:%S')
        print(f'[reconcile] LSL window: since {since}', flush=True)
    else:
        print('[reconcile] FULL backfill', flush=True)

    print('[reconcile] fetching LSL purchases...', flush=True)
    lsl_by_vin = fetch_lsl_purchases(since)
    print(f'[reconcile]   {len(lsl_by_vin)} LSL purchase records', flush=True)

    print('[reconcile] fetching bid assessments...', flush=True)
    bids = fetch_bid_assessments(args.bid)
    print(f'[reconcile]   {len(bids)} bids with AI assessments', flush=True)

    matched = []
    for b in bids:
        vin = (b['vin'] or '').strip().upper()
        deal = lsl_by_vin.get(vin)
        if not deal:
            continue
        ai_rec = b['final_price']
        actual = int(deal['purchase_cost'] or 0)
        if actual <= 0 or not ai_rec:
            continue
        delta = actual - ai_rec
        delta_pct = round(100.0 * delta / ai_rec, 2) if ai_rec else None
        abs_delta_pct = abs(delta_pct) if delta_pct is not None else None
        in_range = (
            b.get('confidence_low') is not None
            and b.get('confidence_high') is not None
            and b['confidence_low'] <= actual <= b['confidence_high']
        )
        # Parse LSL created_at (sqlite TEXT) → datetime
        purchased_at = None
        for k in ('sold_at', 'created_at', 'modified_at'):
            v = deal.get(k)
            if v:
                try:
                    purchased_at = datetime.fromisoformat(v.replace('Z', '+00:00'))
                    break
                except Exception:
                    pass
        matched.append({
            'bid_id':                  b['bid_id'],
            'vin':                     vin,
            'year':                    b.get('year'),
            'make':                    b.get('make'),
            'model':                   b.get('model'),
            'mileage':                 b.get('mileage'),
            'ai_recommendation':       ai_rec,
            'ai_confidence_low':       b.get('confidence_low'),
            'ai_confidence_high':      b.get('confidence_high'),
            'ai_assessment_log_id':    b.get('log_id'),
            'ai_assessed_at':          b.get('ai_assessed_at'),
            'actual_purchase_cost':    actual,
            'actual_purchased_at':     purchased_at,
            'lsl_deal_id':             deal.get('id'),
            'lsl_deal_code':           deal.get('code'),
            'delta':                   delta,
            'delta_pct':               delta_pct,
            'abs_delta_pct':           abs_delta_pct,
            'in_confidence_range':     in_range,
        })

    print(f'[reconcile] matched {len(matched)} bid↔purchase pairs', flush=True)
    n_upserted = upsert_accuracy(matched)
    print(f'[reconcile] upserted {n_upserted} ai_accuracy rows', flush=True)

    # Quick stats summary
    if matched:
        deltas_pct = [m['abs_delta_pct'] for m in matched if m['abs_delta_pct'] is not None]
        deltas_pct_signed = [m['delta_pct'] for m in matched if m['delta_pct'] is not None]
        in_range = sum(1 for m in matched if m['in_confidence_range'])
        if deltas_pct:
            deltas_pct.sort()
            mid = deltas_pct[len(deltas_pct) // 2]
            avg_signed = sum(deltas_pct_signed) / len(deltas_pct_signed) if deltas_pct_signed else 0
            print(f'[reconcile] STATS — median |delta_pct|: {mid:.2f}% · '
                  f'mean signed delta_pct: {avg_signed:+.2f}% · '
                  f'in confidence range: {in_range}/{len(matched)} '
                  f'({100.0*in_range/len(matched):.1f}%)', flush=True)


if __name__ == '__main__':
    main()
