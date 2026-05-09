"""nhtsa_decode.py — NHTSA vPIC VIN decoder + Postgres cache.

Free, public, no API key. Used by:
  - Bid intake fallback (when vauto + accutrade both fail/disagree)
  - Dealer scanner (one decode per scraped VIN, cached forever)
  - Matcher tiered fallback (Tier 2/3 needs canonical Y/M/M)

Cache: nhtsa_decode_cache table (1 row per 17-char VIN). Idempotent inserts
via ON CONFLICT — repeat decodes hit the cache, not the network.

Latency: ~150-300ms per uncached call. Cache hit is <1ms.
Rate limit: NHTSA tolerates ~10-15 req/s in practice. Use a 0.1s sleep
between bulk-backfill requests to stay polite.
"""
from __future__ import annotations
import os
import time
import json
from typing import Optional

import requests
import psycopg2
import psycopg2.extras


VPIC_URL = ('https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/'
            '{vin}?format=json{model_year}')
_TIMEOUT = 8


def _conn():
    return psycopg2.connect(os.environ.get(
        'DATABASE_URL',
        'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'))


def _get_cached(vin: str, conn=None) -> Optional[dict]:
    own = conn is None
    if own: conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT vin, year, make, model, trim, body_class, drive_type, error
            FROM nhtsa_decode_cache WHERE vin = %s
        """, (vin.upper(),))
        return cur.fetchone()
    finally:
        if own: conn.close()


def _store(vin: str, decoded: dict, raw: dict, error: Optional[str],
           conn=None):
    own = conn is None
    if own: conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO nhtsa_decode_cache
              (vin, year, make, model, trim, body_class, drive_type,
               raw_json, decoded_at, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW(), %s)
            ON CONFLICT (vin) DO UPDATE SET
              year = EXCLUDED.year,
              make = EXCLUDED.make,
              model = EXCLUDED.model,
              trim = EXCLUDED.trim,
              body_class = EXCLUDED.body_class,
              drive_type = EXCLUDED.drive_type,
              raw_json = EXCLUDED.raw_json,
              decoded_at = NOW(),
              error = EXCLUDED.error
        """, (vin.upper(),
              decoded.get('year'), decoded.get('make'),
              decoded.get('model'), decoded.get('trim'),
              decoded.get('body_class'), decoded.get('drive_type'),
              json.dumps(raw), error))
        conn.commit()
    finally:
        if own: conn.close()


# ── Field plucker — vPIC returns a denormalized list of {Variable, Value} —

def _pluck(results: list[dict]) -> dict:
    """Pull the YMMT fields out of vPIC's flat response. Most fields are
    one-of, but Trim has 3 variants (Trim, Trim2, Series2) — combine them
    when present."""
    by_var = {r.get('Variable', ''): (r.get('Value') or '').strip()
              for r in (results or [])}
    def _g(k): return by_var.get(k) or None
    trim = _g('Trim')
    trim2 = _g('Trim2')
    series2 = _g('Series2')
    combined_trim = ' '.join(t for t in [trim, trim2, series2] if t) or None
    year_str = _g('Model Year')
    try:
        year = int(year_str) if year_str else None
    except (TypeError, ValueError):
        year = None
    return {
        'year': year,
        'make': _g('Make'),
        'model': _g('Model'),
        'trim': combined_trim,
        'body_class': _g('Body Class'),
        'drive_type': _g('Drive Type'),
    }


# ── Public API ─────────────────────────────────────────────────────────────

def decode_vin(vin: str, *, model_year: int | None = None,
               force_refresh: bool = False, conn=None) -> dict:
    """Return canonical YMMT for a VIN. Hits cache first.

    Returns {year, make, model, trim, body_class, drive_type, error, source}
    where source is 'cache' or 'live' or 'cache_error'.

    Empty fields are None (not empty string) so downstream COALESCE works.
    """
    vin = (vin or '').strip().upper()
    if len(vin) != 17:
        return {'error': f'invalid VIN length {len(vin)}', 'source': 'reject'}

    if not force_refresh:
        cached = _get_cached(vin, conn=conn)
        if cached:
            cached['source'] = 'cache'
            return cached

    # Live fetch
    qs_year = f'&modelyear={int(model_year)}' if model_year else ''
    url = VPIC_URL.format(vin=vin, model_year=qs_year)
    decoded, raw, err = {}, {}, None
    try:
        r = requests.get(url, timeout=_TIMEOUT,
                         headers={'User-Agent': 'EW/dealer-match/1.0'})
        r.raise_for_status()
        raw = r.json()
        decoded = _pluck(raw.get('Results') or [])
        if not decoded.get('make') and not decoded.get('model'):
            err = 'NHTSA returned empty Make/Model'
    except requests.RequestException as e:
        err = f'request: {e}'
    except (ValueError, KeyError) as e:
        err = f'parse: {e}'

    _store(vin, decoded, raw, err, conn=conn)
    decoded['error'] = err
    decoded['source'] = 'live'
    return decoded


def decode_many(vins: list[str], sleep_sec: float = 0.1) -> list[dict]:
    """Bulk decode (used for one-time dealer_inventory backfill).
    Hits cache for already-decoded VINs at zero network cost.
    """
    out = []
    conn = _conn()
    try:
        for v in vins:
            out.append(decode_vin(v, conn=conn))
            # Sleep only when we actually went live
            if out[-1].get('source') == 'live':
                time.sleep(sleep_sec)
    finally:
        conn.close()
    return out


# ── CLI for bulk-backfill ──────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='NHTSA VIN decoder')
    p.add_argument('--vin', help='single VIN to decode')
    p.add_argument('--backfill-dealer-inventory', action='store_true',
                   help='decode every dealer_inventory VIN that lacks nhtsa_*')
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--force', action='store_true',
                   help='re-decode even if cached')
    args = p.parse_args()

    if args.vin:
        r = decode_vin(args.vin, force_refresh=args.force)
        print(json.dumps(r, indent=2, default=str))

    elif args.backfill_dealer_inventory:
        conn = _conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        sql = ("SELECT id, vin FROM dealer_inventory "
               "WHERE vin IS NOT NULL AND length(vin)=17 "
               "  AND nhtsa_decoded_at IS NULL "
               "ORDER BY id DESC")
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        cur.execute(sql)
        rows = cur.fetchall()
        print(f'[backfill] {len(rows)} dealer_inventory rows to decode',
              flush=True)
        for i, r in enumerate(rows, 1):
            d = decode_vin(r['vin'], conn=conn)
            cur.execute("""
                UPDATE dealer_inventory SET
                  nhtsa_year = %s, nhtsa_make = %s, nhtsa_model = %s,
                  nhtsa_trim = %s, nhtsa_body_class = %s,
                  nhtsa_drive_type = %s, nhtsa_decoded_at = NOW(),
                  nhtsa_decode_error = %s
                WHERE id = %s
            """, (d.get('year'), d.get('make'), d.get('model'),
                  d.get('trim'), d.get('body_class'),
                  d.get('drive_type'), d.get('error'), r['id']))
            if i % 50 == 0:
                conn.commit()
                print(f'  [progress] {i}/{len(rows)}', flush=True)
        conn.commit()
        conn.close()
        print(f'[backfill] DONE')

    else:
        p.print_help()
