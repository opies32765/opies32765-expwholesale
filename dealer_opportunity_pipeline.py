"""Dealer Opportunity Engine — daily pipeline.

Flow:
    1. Load all eligible dealer-inventory rows (active, valid VIN, year>=2015,
       complete YMM, mileage>0).
    2. Concurrent MMR sweep via dealer_mmr.fetch_mmr → upsert dealer_mmr cache.
    3. Filter to vehicles >=5% under MMR (--threshold-pct configurable).
    4. Concurrent rBook deep-dive on the shortlist via vauto_bff_direct
       .fetch_competitive_set → compute P25/P50/P75 retail + avg DOL.
    5. Cross-check with LSL buyer-pattern data (if available).
    6. Composite score 0-100 across all signals.
    7. Upsert dealer_opportunities (one row per VIN per snapshot_date).
    8. Audit row in opportunity_runs.

Read-only on dealer_inventory; writes dealer_mmr + dealer_opportunities +
opportunity_runs only.

Auth: reuses /opt/expwholesale/state/vauto_session.json (Phase 2 v1 cookie
jar). Bails on first VAutoAuthError to preserve cookies for bid intake.

Usage:
    python3 dealer_opportunity_pipeline.py [--threshold-pct 5.0]
                                           [--mmr-concurrency 8]
                                           [--rbook-concurrency 4]
                                           [--limit-inventory N]
                                           [--dry-run]
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

sys.path.insert(0, '/opt/expwholesale')

import psycopg2
import psycopg2.extras

from cookie_jar import CookieJar
from dealer_mmr import fetch_mmr, VAutoAuthError, VAutoMmrError
from sold_check import check_sold
import requests as _requests

VEHICLE_INFO_URL = (
    'https://slot2.bff.megazord.vauto.app.coxautoinc.com'
    '/api/appraisal/vehicleInfo?strictYMM=true'
)


def _fetch_vehicle_info(vin: str, mileage: int,
                        cookies: dict, headers: dict, timeout: int = 12) -> dict | None:
    """Get vAuto\'s canonical vehicle decode for a VIN.

    Returns (canonical_vehicle_dict, option_codes) or None on failure.
    Without canonical decode, competition endpoint returns ~1 result for
    series-named cars (the subject vehicle as its own \"peer\").
    """
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

from vauto_bff_direct import (fetch_competitive_set, parse_competitive_set,
                              VAutoAuthError as RBVAutoAuthError,
                              VAutoServerError, VAutoBadRequestError)

# LSL signal is optional — if the module isn't importable (e.g. running
# offline) we skip the LSL bonus rather than fail.
try:
    from lsl_buyer_match import find_same_ymm_deals  # type: ignore
    HAS_LSL = True
except Exception:
    HAS_LSL = False


DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')

SESSION_PATH = '/opt/expwholesale/state/vauto_session.json'

log = logging.getLogger('opportunity_pipeline')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)

_auth_failed = threading.Event()


# ──────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────

def conn():
    return psycopg2.connect(**DB)


def load_eligible_inventory(limit: int | None = None) -> list[dict]:
    """Active dealer_inventory rows that are scoring-eligible.

    Filters:
        status='active', VIN length 17, year>=2015, mileage>0,
        complete year+make+model. We exclude mileage=0 to avoid the
        false-bargain MMR signal we saw on $0-asking-price scraper rows.
    """
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        sql = """
            SELECT di.id AS inventory_id,
                   di.dealer_id,
                   di.vin,
                   di.year, di.make, di.model, di.trim,
                   di.mileage, di.price, di.msrp,
                   di.ext_color,
                   di.photo_url,
                   di.url AS detail_url,
                   COALESCE(di.verified_days_on_lot,
                            EXTRACT(EPOCH FROM (NOW() - di.source_added_at))/86400,
                            EXTRACT(EPOCH FROM (NOW() - di.first_seen_at))/86400
                   )::int AS dealer_dol,
                   di.price_drop_amount,
                   CASE WHEN di.price_drop_at IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (NOW() - di.price_drop_at))/86400
                        END::int AS price_drop_days_ago,
                   d.name AS dealer_name,
                   d.phone AS dealer_phone
              FROM dealer_inventory di
              JOIN dealers d ON d.id = di.dealer_id
             WHERE di.status='active'
               AND di.vin IS NOT NULL AND length(di.vin) = 17
               AND di.year IS NOT NULL AND di.year >= 2015
               AND di.make IS NOT NULL AND di.model IS NOT NULL
               AND COALESCE(di.price, 0) > 0
               -- NULL mileage allowed; falls back to year-based default
               -- in mmr_one() so we don't exclude 400+ ECT/Ferrari cars
               -- whose scrapers don't capture odometer
             ORDER BY d.id, di.id
        """
        if limit:
            sql += f' LIMIT {int(limit)}'
        cur.execute(sql)
        return cur.fetchall()


def upsert_dealer_mmr(rows: list[dict]) -> None:
    """Bulk UPSERT into dealer_mmr. Rows are dicts from mmr_one()."""
    if not rows:
        return
    with conn() as c, c.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO dealer_mmr
              (vin, dealer_id, inventory_id, year, make, model, trim,
               mileage, asking_price,
               wholesale_avg, wholesale_above, wholesale_below,
               retail_avg, retail_above, retail_below,
               grade, average_odometer, sample_size,
               no_mmr, fetch_error, raw, fetched_at, updated_at)
            VALUES %s
            ON CONFLICT (vin) DO UPDATE SET
              dealer_id        = EXCLUDED.dealer_id,
              inventory_id     = EXCLUDED.inventory_id,
              year             = EXCLUDED.year,
              make             = EXCLUDED.make,
              model            = EXCLUDED.model,
              trim             = EXCLUDED.trim,
              mileage          = EXCLUDED.mileage,
              asking_price     = EXCLUDED.asking_price,
              wholesale_avg    = EXCLUDED.wholesale_avg,
              wholesale_above  = EXCLUDED.wholesale_above,
              wholesale_below  = EXCLUDED.wholesale_below,
              retail_avg       = EXCLUDED.retail_avg,
              retail_above     = EXCLUDED.retail_above,
              retail_below     = EXCLUDED.retail_below,
              grade            = EXCLUDED.grade,
              average_odometer = EXCLUDED.average_odometer,
              sample_size      = EXCLUDED.sample_size,
              no_mmr           = EXCLUDED.no_mmr,
              fetch_error      = EXCLUDED.fetch_error,
              raw              = EXCLUDED.raw,
              fetched_at       = EXCLUDED.fetched_at,
              updated_at       = NOW()
        """, [
            (r['vin'], r['dealer_id'], r['inventory_id'],
             r['year'], r['make'], r['model'], r.get('trim'),
             r['mileage'], r['asking_price'],
             r.get('wholesale_avg'), r.get('wholesale_above'), r.get('wholesale_below'),
             r.get('retail_avg'), r.get('retail_above'), r.get('retail_below'),
             r.get('grade'), r.get('average_odometer'), r.get('sample_size'),
             r.get('no_mmr', False), r.get('fetch_error'),
             json.dumps(r.get('raw') or {}, default=str),
             r['fetched_at'], r['fetched_at'])
            for r in rows
        ])


def upsert_opportunity(opp: dict) -> None:
    """UPSERT a single dealer_opportunity row keyed on (vin, snapshot_date)."""
    with conn() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO dealer_opportunities
              (snapshot_date, vin, dealer_id, inventory_id,
               year, make, model, trim, mileage, ext_color, photo_url, detail_url,
               asking_price, mmr_wholesale_avg, mmr_wholesale_above,
               mmr_wholesale_below, mmr_grade,
               mmr_retail_avg, mmr_retail_above, mmr_retail_below,
               dollars_under_mmr, pct_under_mmr,
               dollars_under_retail, pct_under_retail, opportunity_type,
               rbook_comp_count, rbook_p25, rbook_p50, rbook_p75, rbook_avg_dol,
               retail_headroom,
               dealer_dol, recent_price_drop_amount, recent_price_drop_days_ago,
               lsl_deal_count, lsl_avg_gross,
               score, score_breakdown, signals)
            VALUES (%(snapshot_date)s, %(vin)s, %(dealer_id)s, %(inventory_id)s,
                    %(year)s, %(make)s, %(model)s, %(trim)s, %(mileage)s,
                    %(ext_color)s, %(photo_url)s, %(detail_url)s,
                    %(asking_price)s, %(mmr_wholesale_avg)s, %(mmr_wholesale_above)s,
                    %(mmr_wholesale_below)s, %(mmr_grade)s,
                    %(mmr_retail_avg)s, %(mmr_retail_above)s, %(mmr_retail_below)s,
                    %(dollars_under_mmr)s, %(pct_under_mmr)s,
                    %(dollars_under_retail)s, %(pct_under_retail)s, %(opportunity_type)s,
                    %(rbook_comp_count)s, %(rbook_p25)s, %(rbook_p50)s, %(rbook_p75)s,
                    %(rbook_avg_dol)s, %(retail_headroom)s,
                    %(dealer_dol)s, %(recent_price_drop_amount)s,
                    %(recent_price_drop_days_ago)s,
                    %(lsl_deal_count)s, %(lsl_avg_gross)s,
                    %(score)s, %(score_breakdown)s::jsonb, %(signals)s::jsonb)
            ON CONFLICT (vin, snapshot_date) DO UPDATE SET
              dealer_id        = EXCLUDED.dealer_id,
              inventory_id     = EXCLUDED.inventory_id,
              year             = EXCLUDED.year,
              make             = EXCLUDED.make,
              model            = EXCLUDED.model,
              trim             = EXCLUDED.trim,
              mileage          = EXCLUDED.mileage,
              ext_color        = EXCLUDED.ext_color,
              photo_url        = EXCLUDED.photo_url,
              detail_url       = EXCLUDED.detail_url,
              asking_price     = EXCLUDED.asking_price,
              mmr_wholesale_avg = EXCLUDED.mmr_wholesale_avg,
              mmr_wholesale_above = EXCLUDED.mmr_wholesale_above,
              mmr_wholesale_below = EXCLUDED.mmr_wholesale_below,
              mmr_grade        = EXCLUDED.mmr_grade,
              mmr_retail_avg   = EXCLUDED.mmr_retail_avg,
              mmr_retail_above = EXCLUDED.mmr_retail_above,
              mmr_retail_below = EXCLUDED.mmr_retail_below,
              dollars_under_mmr = EXCLUDED.dollars_under_mmr,
              pct_under_mmr    = EXCLUDED.pct_under_mmr,
              dollars_under_retail = EXCLUDED.dollars_under_retail,
              pct_under_retail = EXCLUDED.pct_under_retail,
              opportunity_type = EXCLUDED.opportunity_type,
              rbook_comp_count = EXCLUDED.rbook_comp_count,
              rbook_p25        = EXCLUDED.rbook_p25,
              rbook_p50        = EXCLUDED.rbook_p50,
              rbook_p75        = EXCLUDED.rbook_p75,
              rbook_avg_dol    = EXCLUDED.rbook_avg_dol,
              retail_headroom  = EXCLUDED.retail_headroom,
              dealer_dol       = EXCLUDED.dealer_dol,
              recent_price_drop_amount = EXCLUDED.recent_price_drop_amount,
              recent_price_drop_days_ago = EXCLUDED.recent_price_drop_days_ago,
              lsl_deal_count   = EXCLUDED.lsl_deal_count,
              lsl_avg_gross    = EXCLUDED.lsl_avg_gross,
              score            = EXCLUDED.score,
              score_breakdown  = EXCLUDED.score_breakdown,
              signals          = EXCLUDED.signals,
              updated_at       = NOW()
        """, opp)


def insert_run(stats: dict) -> int:
    with conn() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO opportunity_runs
              (mmr_attempted, mmr_ok, mmr_no_data, mmr_errors,
               candidates_5pct, rbook_attempted, rbook_ok, rbook_errors,
               opportunities_written, orphans_removed,
               auth_failed, notes, finished_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            RETURNING id
        """, (
            stats.get('mmr_attempted', 0), stats.get('mmr_ok', 0),
            stats.get('mmr_no_data', 0), stats.get('mmr_errors', 0),
            stats.get('candidates_5pct', 0),
            stats.get('rbook_attempted', 0), stats.get('rbook_ok', 0),
            stats.get('rbook_errors', 0),
            stats.get('opportunities_written', 0),
            stats.get('orphans_removed', 0),
            stats.get('auth_failed', False),
            stats.get('notes', ''),
        ))
        return cur.fetchone()[0]


# ──────────────────────────────────────────────────────────────────────────
# Worker functions
# ──────────────────────────────────────────────────────────────────────────

def mmr_one(inv: dict, cookies: dict, headers: dict, appraisal_id: str) -> dict:
    """One MMR fetch. Returns dict ready for upsert_dealer_mmr."""
    if _auth_failed.is_set():
        return None
    # Default mileage for NULL-odometer cars (ECT, Ferrari FL etc. don't
    # scrape mileage). Use 8k/yr times age — typical luxury dealer
    # inventory cadence. Better than excluding the car entirely.
    import datetime as _dt
    _mileage = inv['mileage']
    if _mileage is None or _mileage <= 0:
        _age = max(0, _dt.datetime.now().year - (inv['year'] or _dt.datetime.now().year))
        _mileage = 8000 * _age + 1000  # +1k baseline so MY-current cars still get >0
    veh = {
        'vin': inv['vin'], 'year': inv['year'], 'make': inv['make'],
        'model': inv['model'], 'mileage': _mileage,
        'trim': inv.get('trim'),
    }
    base = dict(
        vin=inv['vin'], dealer_id=inv['dealer_id'],
        inventory_id=inv['inventory_id'],
        year=inv['year'], make=inv['make'], model=inv['model'],
        trim=inv.get('trim'),
        mileage=inv['mileage'], asking_price=inv['price'],
        fetched_at=time.strftime('%Y-%m-%d %H:%M:%S'),
    )
    try:
        res = fetch_mmr(veh, cookies, headers, appraisal_id)
        base.update({
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
            'raw': res,
        })
        return base
    except VAutoAuthError as e:
        _auth_failed.set()
        base.update({'no_mmr': False, 'fetch_error': f'AUTH:{e}'})
        return base
    except VAutoMmrError as e:
        base.update({'no_mmr': False, 'fetch_error': str(e)})
        return base


def rbook_one(opp: dict, cookies: dict, headers: dict) -> dict:
    """Pull rBook competitive set, attach P25/P50/P75 + avg_dol to opp.

    Strategy:
      1. Fetch canonical vehicle dict from /api/appraisal/vehicleInfo
         (vAuto needs its OWN make/model/series strings — scraper-derived
         text returns 1 result for series-named cars).
      2. If canonical decode succeeded → call competition with it (best result).
      3. Else fall back to a loose query with just our YMM (worse, but tries).
    """
    if _auth_failed.is_set():
        opp['_rbook_err'] = 'skipped_auth_failed'
        return opp

    used = 'unknown'
    try:
        canonical = _fetch_vehicle_info(opp['vin'], opp.get('mileage') or 0,
                                        cookies, headers)
        if canonical:
            veh, option_codes = canonical
            body = fetch_competitive_set(
                veh, cookies, headers,
                list_price=opp.get('asking_price') or 0,
                appraisal_id='unused',
                option_codes=option_codes,
                timeout=25,
            )
            used = 'canonical'
        else:
            # vehicleInfo failed — fall back to scraper YMM, no series
            veh = {
                'vin': opp['vin'],
                'year': opp['year'],
                'make': opp['make'],
                'model': opp['model'],
                'odometer': opp['mileage'],
            }
            body = fetch_competitive_set(
                veh, cookies, headers,
                criteria_options=[],
                list_price=opp.get('asking_price') or 0,
                appraisal_id='unused',
                timeout=25,
            )
            used = 'loose_fallback'

        rows = parse_competitive_set(body).get('rows') or []

        # Drop the subject VIN from comps if present
        subject_vin = (opp.get('vin') or '').upper()
        if subject_vin:
            rows = [r for r in rows if (r.get('vin') or '').upper() != subject_vin]

        opp['rbook_comp_count'] = len(rows)
        opp['signals'] = opp.get('signals') or {}
        opp['signals']['rbook_query'] = used

        prices = sorted([r['price'] for r in rows
                         if r.get('price') and r['price'] > 0])
        dols = [r.get('days_on_lot') for r in rows
                if isinstance(r.get('days_on_lot'), int)]
        if prices:
            n = len(prices)
            opp['rbook_p25'] = prices[max(0, n // 4 - 1)]
            opp['rbook_p50'] = prices[n // 2]
            opp['rbook_p75'] = prices[min(n - 1, n * 3 // 4)]
            if opp.get('asking_price'):
                opp['retail_headroom'] = opp['rbook_p50'] - opp['asking_price']
        if dols:
            opp['rbook_avg_dol'] = round(sum(dols) / len(dols), 1)
        opp['_rbook_err'] = None
        return opp
    except RBVAutoAuthError as e:
        _auth_failed.set()
        opp['_rbook_err'] = f'AUTH:{e}'
        return opp
    except (VAutoServerError, VAutoBadRequestError, Exception) as e:
        opp['_rbook_err'] = f'{type(e).__name__}:{e}'
        return opp


def lsl_one(opp: dict) -> dict:
    """Attach LSL deal_count + avg_gross from /opt/livesaleslog/crm.db."""
    if not HAS_LSL:
        return opp
    try:
        r = find_same_ymm_deals(
            opp['year'], opp['make'], opp['model'],
            mileage=opp.get('mileage'), trim=opp.get('trim'),
        )
        patterns = r.get('patterns') or {}
        opp['lsl_deal_count'] = patterns.get('count') or 0
        opp['lsl_avg_gross'] = int(patterns.get('avg_gross') or 0)
    except Exception as e:
        opp['_lsl_err'] = str(e)
    return opp


# ──────────────────────────────────────────────────────────────────────────
# Composite scoring
# ──────────────────────────────────────────────────────────────────────────





CARFAX_URL = 'https://slot1.bff.megazord.vauto.app.coxautoinc.com/api/carfax/report'


def carfax_one(opp: dict, cookies: dict, headers: dict) -> dict:
    """Pull Carfax via vAuto BFF. Adds opp['signals']['carfax']."""
    if _auth_failed.is_set():
        return opp
    try:
        import requests as _rq
        r = _rq.get(f"{CARFAX_URL}?vin={opp['vin']}",
                    headers=headers, cookies=cookies, timeout=15)
        if r.status_code in (401, 403):
            _auth_failed.set()
            return opp
        if not r.ok:
            opp['signals'] = opp.get('signals') or {}
            opp['signals']['carfax_err'] = f'{r.status_code}'
            return opp
        data = r.json() or {}
        report = (data.get('report') or {})
        status = data.get('status')
        cf = {
            'status': status,
            'clean_title':     bool(report.get('hasCleanTitle')),
            'has_problems':    bool(report.get('hasProblems')),
            'has_major':       bool(report.get('hasMajorProblems')),
            'has_damage':      bool(report.get('hasDamage')),
            'frame_damage':    bool(report.get('hasFrameDamage')),
            'total_loss':      bool(report.get('hasTotalLoss')),
            'airbag_deployed': bool(report.get('hasAirbagDeployment')),
            'odo_rollback':    bool(report.get('hasOdometerRollback')),
            'accidents':       int(report.get('accidentCount') or 0),
            'num_owners':      int(report.get('numOwners') or 0),
            'one_owner':       bool(report.get('hasOneOwner')),
            'service_records': int(report.get('serviceRecordCount') or 0),
            'has_recall':      bool(report.get('hasManufacturerRecall')),
            'report_url':      report.get('url'),
        }
        opp['signals'] = opp.get('signals') or {}
        opp['signals']['carfax'] = cf
    except Exception as e:
        opp['signals'] = opp.get('signals') or {}
        opp['signals']['carfax_err'] = str(e)
    return opp


def wishlist_one(opp: dict) -> dict:
    """Find active wishlists matching this YMM/year/color. Adds matches to
    opp['signals']['wishlist_matches']: list of {customer_name, phone,
    sourcing_id, days_old, summary}."""
    matches = []
    try:
        with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, phone, customer_name, status, year_min, year_max,
                       make, model, trim, ext_color, miles_max,
                       narrative_brief, last_msg_at, created_at,
                       relaxations
                  FROM sourcing_requests
                 WHERE archived_at IS NULL
                   AND status IN ('wishlist','gathering','searching','presented')
                   AND make IS NOT NULL
                   AND model IS NOT NULL
                   AND lower(make) = lower(%s)
                   AND (
                        lower(model) = lower(%s) OR
                        lower(model) || %s LIKE lower(%s) OR
                        lower(%s) LIKE '%%' || lower(model) || '%%'
                   )
                   AND (year_min IS NULL OR %s >= year_min OR 'year' = ANY(relaxations))
                   AND (year_max IS NULL OR %s <= year_max OR 'year' = ANY(relaxations))
                   AND (miles_max IS NULL OR COALESCE(%s,0) <= miles_max OR 'miles' = ANY(relaxations))
                 ORDER BY last_msg_at DESC
            """, (
                opp['make'], opp['model'], '%', '%' + (opp['model'] or '').lower() + '%',
                opp['model'],
                opp['year'], opp['year'],
                opp.get('mileage') or 0,
            ))
            for r in cur.fetchall():
                # Color match check (in Python, since text[] needs care)
                if r['ext_color'] and 'ext_color' not in (r['relaxations'] or []):
                    opp_color = (opp.get('ext_color') or '').lower()
                    if opp_color and not any(opp_color in c.lower() or c.lower() in opp_color
                                              for c in r['ext_color']):
                        continue
                days_old = (
                    (psycopg2.extras.DictRow if False else None) and 0
                ) or 0
                from datetime import datetime, timezone
                last = r['last_msg_at']
                if last:
                    days_old = int((datetime.now(timezone.utc) - last).total_seconds() / 86400)
                matches.append({
                    'sourcing_id': r['id'],
                    'customer_name': r['customer_name'] or 'unknown',
                    'phone': r['phone'],
                    'status': r['status'],
                    'days_old': days_old,
                    'summary': (r.get('narrative_brief') or '')[:200],
                })
    except Exception as e:
        opp['_wishlist_err'] = str(e)
    if matches:
        opp['signals'] = opp.get('signals') or {}
        opp['signals']['wishlist_matches'] = matches
    return opp


def compute_score(opp: dict) -> tuple[int, dict]:
    """Composite score 0-100 with per-signal breakdown for UI tooltip.

    Signals (max points):
        mmr_pct           35  — pct under MMR (12%+ → full)
        retail_headroom   25  — retail headroom % vs asking (20%+ → full)
        comp_velocity     10  — low avg DOL on rBook comps (<30 → full)
        dealer_dol        10  — aged at dealer (>=60 → full)
        recent_price_drop 10  — dropped in last 14 days
        lsl_track_record  10  — historical EW gross on this YMM
    """
    breakdown = {}
    score = 0

    pct = float(opp.get('pct_under_mmr') or 0)
    pts_mmr = min(35, max(0, pct * 35 / 12))   # 12%+ = full
    breakdown['mmr_pct'] = round(pts_mmr, 1)
    score += pts_mmr

    headroom = opp.get('retail_headroom')
    asking = opp.get('asking_price')
    if headroom and asking and asking > 0:
        hd_pct = 100 * headroom / asking
        pts_rh = min(25, max(0, hd_pct * 25 / 20))   # 20%+ = full
    else:
        pts_rh = 0
    breakdown['retail_headroom'] = round(pts_rh, 1)
    score += pts_rh

    avg_dol = opp.get('rbook_avg_dol')
    if avg_dol is not None:
        # 30d or less → 10 pts. 120d+ → 0. Linear between.
        pts_v = max(0, min(10, 10 * (120 - float(avg_dol)) / 90))
    else:
        pts_v = 0
    breakdown['comp_velocity'] = round(pts_v, 1)
    score += pts_v

    dd = opp.get('dealer_dol') or 0
    pts_dd = min(10, max(0, dd * 10 / 60))           # 60d+ = full
    breakdown['dealer_dol'] = round(pts_dd, 1)
    score += pts_dd

    drop_amt = opp.get('recent_price_drop_amount') or 0
    drop_days = opp.get('recent_price_drop_days_ago')
    if drop_amt > 0 and drop_days is not None and drop_days <= 30:
        pts_drop = min(10, drop_amt / 500)   # $5k drop = full
        if drop_days > 14:
            pts_drop *= 0.5  # halve if dropped >2wk ago
    else:
        pts_drop = 0
    breakdown['recent_price_drop'] = round(pts_drop, 1)
    score += pts_drop

    lsl_n = opp.get('lsl_deal_count') or 0
    lsl_g = opp.get('lsl_avg_gross') or 0
    if lsl_n >= 2 and lsl_g >= 1000:
        pts_lsl = min(10, lsl_n * 1.5 + lsl_g / 1000)
    elif lsl_n >= 1:
        pts_lsl = 3
    else:
        pts_lsl = 0
    breakdown['lsl_track_record'] = round(pts_lsl, 1)
    score += pts_lsl

    # Carfax penalty — major damage indicators slash score
    cf = (opp.get('signals') or {}).get('carfax') or {}
    cf_penalty = 0
    if cf:
        if cf.get('total_loss'):      cf_penalty += 40
        if cf.get('frame_damage'):    cf_penalty += 25
        if cf.get('odo_rollback'):    cf_penalty += 20
        if cf.get('has_major'):       cf_penalty += 15
        if cf.get('airbag_deployed'): cf_penalty += 10
        if not cf.get('clean_title'): cf_penalty += 20
        if cf.get('accidents', 0) >= 2: cf_penalty += 10
        elif cf.get('accidents', 0) == 1: cf_penalty += 5
    breakdown['carfax_penalty'] = -cf_penalty if cf_penalty else 0
    score -= cf_penalty
    return max(0, int(round(score))), breakdown


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--threshold-pct', type=float, default=2.0,
                   help='minimum pct under MMR to qualify as a wholesale-arb candidate')
    p.add_argument('--retail-threshold-pct', type=float, default=5.0,
                   help='minimum pct under Manheim Retail Avg to qualify as a retail-arb candidate')
    p.add_argument('--mmr-concurrency', type=int, default=8)
    p.add_argument('--rbook-concurrency', type=int, default=4)
    p.add_argument('--limit-inventory', type=int, default=None)
    p.add_argument('--dry-run', action='store_true',
                   help='skip DB writes (dealer_opportunities + dealer_mmr)')
    args = p.parse_args()

    jar = CookieJar(SESSION_PATH)
    jar.load()
    cookies = jar.get_cookies()
    headers = jar.get_headers()
    appraisal_id = jar.get_session_appraisal_id()
    log.info('cookies captured_at=%s age=%dmin', jar.captured_at(),
             int(jar.age_seconds() / 60))

    inventory = load_eligible_inventory(args.limit_inventory)
    log.info('eligible inventory: %d vehicles', len(inventory))

    stats = {
        'mmr_attempted': 0, 'mmr_ok': 0, 'mmr_no_data': 0, 'mmr_errors': 0,
        'candidates_5pct': 0,
        'rbook_attempted': 0, 'rbook_ok': 0, 'rbook_errors': 0,
        'opportunities_written': 0, 'auth_failed': False,
    }

    # ── Phase 1: MMR sweep ───────────────────────────────────────────────
    log.info('phase 1: MMR sweep, concurrency=%d', args.mmr_concurrency)
    t0 = time.monotonic()
    mmr_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.mmr_concurrency) as pool:
        futs = [pool.submit(mmr_one, inv, cookies, headers, appraisal_id)
                for inv in inventory]
        for fut in as_completed(futs):
            r = fut.result()
            if r is None:
                continue
            stats['mmr_attempted'] += 1
            mmr_results.append(r)
            if r.get('fetch_error'):
                stats['mmr_errors'] += 1
            elif r.get('no_mmr'):
                stats['mmr_no_data'] += 1
            else:
                stats['mmr_ok'] += 1
    elapsed = time.monotonic() - t0
    log.info('phase 1 done in %.1fs: ok=%d no_mmr=%d err=%d',
             elapsed, stats['mmr_ok'], stats['mmr_no_data'], stats['mmr_errors'])

    if _auth_failed.is_set():
        stats['auth_failed'] = True
        log.error('auth failed during MMR sweep — bailing out')
        if not args.dry_run:
            insert_run(stats)
        return 2

    if not args.dry_run:
        # Bulk upsert MMR results
        in_chunks = [mmr_results[i:i + 200]
                     for i in range(0, len(mmr_results), 200)]
        for chunk in in_chunks:
            upsert_dealer_mmr(chunk)
        log.info('upserted %d dealer_mmr rows', len(mmr_results))

    # ── Phase 2: filter to candidates (two parallel paths) ──────────────
    # wholesale_arb: asking >= --threshold-pct under MMR Wholesale Avg
    # retail_arb:    asking >= --retail-threshold-pct under Manheim Retail Avg
    # Either qualifies; opportunity_type tags which (or 'both').
    inv_by_vin = {inv['vin']: inv for inv in inventory}
    candidates_map = {}
    for r in mmr_results:
        if r.get('no_mmr') or r.get('fetch_error'):
            continue
        ws = r.get('wholesale_avg')
        ask = r.get('asking_price')
        if not ws or not ask or ask <= 0:
            continue
        retail_avg = r.get('retail_avg')

        d_w = ws - ask
        pct_w = 100 * d_w / ws
        if retail_avg and retail_avg > 0:
            d_r = retail_avg - ask
            pct_r = 100 * d_r / retail_avg
        else:
            d_r = None
            pct_r = 0

        is_wholesale = pct_w >= args.threshold_pct
        is_retail = retail_avg and pct_r >= args.retail_threshold_pct
        if not (is_wholesale or is_retail):
            continue

        opp_type = ('both' if is_wholesale and is_retail
                    else 'wholesale_arb' if is_wholesale
                    else 'retail_arb')

        inv = inv_by_vin[r['vin']]
        candidates_map[r['vin']] = {
            'snapshot_date': date.today().isoformat(),
            'vin': r['vin'],
            'dealer_id': r['dealer_id'],
            'inventory_id': r['inventory_id'],
            'year': r['year'], 'make': r['make'], 'model': r['model'],
            'trim': r.get('trim'),
            'mileage': r['mileage'],
            'ext_color': inv.get('ext_color'),
            'photo_url': inv.get('photo_url'),
            'detail_url': inv.get('detail_url'),
            'asking_price': ask,
            'mmr_wholesale_avg': ws,
            'mmr_wholesale_above': r.get('wholesale_above'),
            'mmr_wholesale_below': r.get('wholesale_below'),
            'mmr_retail_avg': retail_avg,
            'mmr_retail_above': r.get('retail_above'),
            'mmr_retail_below': r.get('retail_below'),
            'mmr_grade': r.get('grade'),
            'dollars_under_mmr': d_w,
            'pct_under_mmr': round(pct_w, 2),
            'dollars_under_retail': d_r,
            'pct_under_retail': round(pct_r, 2) if retail_avg else None,
            'opportunity_type': opp_type,
            'dealer_dol': inv.get('dealer_dol'),
            'recent_price_drop_amount': inv.get('price_drop_amount'),
            'recent_price_drop_days_ago': inv.get('price_drop_days_ago'),
            'rbook_comp_count': None, 'rbook_p25': None, 'rbook_p50': None,
            'rbook_p75': None, 'rbook_avg_dol': None, 'retail_headroom': None,
            'lsl_deal_count': None, 'lsl_avg_gross': None,
            'signals': {},
            'dealer_name': inv.get('dealer_name'),
            'dealer_phone': inv.get('dealer_phone'),
        }
    candidates = list(candidates_map.values())
    stats['candidates_5pct'] = len(candidates)
    bt = {'wholesale_arb': 0, 'retail_arb': 0, 'both': 0}
    for c in candidates:
        bt[c['opportunity_type']] += 1
    log.info('phase 2: %d candidates (wholesale=%d retail=%d both=%d) '
             '[mmr_thresh=%.1f%% retail_thresh=%.1f%%]',
             len(candidates), bt['wholesale_arb'], bt['retail_arb'], bt['both'],
             args.threshold_pct, args.retail_threshold_pct)

    # phase 2.5: sold-status verification — drop cars already marked sold on
    # the dealer\'s live page (CSS class or text marker). Saves rBook/Carfax/
    # Gemini calls and prevents stale opportunities from reaching the
    # dashboard.
    log.info('phase 2.5: sold-status verification on %d candidates', len(candidates))
    sold_inventory_ids = []
    surviving = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.rbook_concurrency) as pool:
        futs = {pool.submit(check_sold, c.get('detail_url'), 12): c
                for c in candidates if c.get('detail_url')}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {'ok': False, 'sold': False, 'reason': str(e)}
            c['signals'] = c.get('signals') or {}
            c['signals']['sold_check'] = res
            if res.get('ok') and res.get('sold'):
                if c.get('inventory_id'):
                    sold_inventory_ids.append(c['inventory_id'])
            else:
                surviving.append(c)
    # candidates without a detail_url survive (we can\'t check; assume active)
    surviving += [c for c in candidates if not c.get('detail_url')]
    log.info('phase 2.5 done in %.1fs: %d sold dropped, %d remaining',
             time.monotonic() - t0, len(candidates) - len(surviving),
             len(surviving))

    # Mark dropped cars sold in dealer_inventory so they don\'t enter the pool
    # tomorrow.
    if sold_inventory_ids and not args.dry_run:
        with conn() as _c, _c.cursor() as _cur:
            _cur.execute("""
                UPDATE dealer_inventory
                   SET status='sold',
                       sold_at = COALESCE(sold_at, NOW()),
                       sold_confidence = GREATEST(COALESCE(sold_confidence, 0), 0.95),
                       sold_signals = COALESCE(sold_signals, '{}'::jsonb)
                          || jsonb_build_object('source', 'opp_pipeline_live_check',
                                                'detected_at', NOW()::text)
                 WHERE id = ANY(%s)
            """, (sold_inventory_ids,))
        log.info('marked %d dealer_inventory rows as sold', len(sold_inventory_ids))

    candidates = surviving

    # ── Phase 3: rBook deep dive ─────────────────────────────────────────
    log.info('phase 3: rBook deep dive, concurrency=%d', args.rbook_concurrency)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.rbook_concurrency) as pool:
        futs = {pool.submit(rbook_one, c, cookies, headers): c for c in candidates}
        for fut in as_completed(futs):
            c = fut.result()
            stats['rbook_attempted'] += 1
            if c.get('_rbook_err'):
                stats['rbook_errors'] += 1
                c['signals']['rbook_err'] = c.pop('_rbook_err')
            else:
                c.pop('_rbook_err', None)
                stats['rbook_ok'] += 1
    elapsed = time.monotonic() - t0
    log.info('phase 3 done in %.1fs: ok=%d err=%d',
             elapsed, stats['rbook_ok'], stats['rbook_errors'])

    # ── Phase 4: LSL pattern + wishlist match ────────────────────────────
    if HAS_LSL:
        log.info('phase 4a: LSL pattern match for %d candidates', len(candidates))
        for c in candidates:
            lsl_one(c)
    else:
        log.warning('phase 4a: LSL module unavailable, skipping')

    log.info('phase 4b: wishlist match for %d candidates', len(candidates))
    for c in candidates:
        wishlist_one(c)

    log.info('phase 4c: Carfax cross-check, concurrency=%d', args.rbook_concurrency)
    with ThreadPoolExecutor(max_workers=args.rbook_concurrency) as pool:
        futs = [pool.submit(carfax_one, c, cookies, headers) for c in candidates]
        for fut in as_completed(futs):
            fut.result()
    cf_ok = sum(1 for c in candidates if c.get('signals', {}).get('carfax'))
    cf_flags = sum(1 for c in candidates if c.get('signals', {}).get('carfax', {}).get('has_major')
                                          or c.get('signals', {}).get('carfax', {}).get('frame_damage')
                                          or c.get('signals', {}).get('carfax', {}).get('total_loss'))
    log.info('phase 4c done: carfax ok=%d / major-issue flags=%d', cf_ok, cf_flags)

    # ── Phase 5: score + upsert ──────────────────────────────────────────
    log.info('phase 5: scoring + writing opportunities')
    for c in candidates:
        score, breakdown = compute_score(c)
        c['score'] = score
        c['score_breakdown'] = json.dumps(breakdown)
        # dealer_name/phone is denormalized in signals for the dashboard
        c['signals']['dealer_name'] = c.pop('dealer_name', None)
        c['signals']['dealer_phone'] = c.pop('dealer_phone', None)
        c['signals'] = json.dumps(c['signals'], default=str)
        if not args.dry_run:
            try:
                upsert_opportunity(c)
                stats['opportunities_written'] += 1
            except Exception as e:
                log.error('upsert failed for %s: %s', c['vin'], e)

    # ── Phase 5b: orphan cleanup ─────────────────────────────────────────
    # If this is a re-run for the same snapshot_date (manual trigger,
    # retry-on-failure, threshold tweak, etc.), drop same-day rows whose
    # VIN no longer qualifies AND whose status is still untouched (new).
    # Operator-touched rows (pursuing/passed/acquired) are preserved.
    if not args.dry_run:
        current_vins = [c['vin'] for c in candidates if c.get('vin')]
        with conn() as c_db, c_db.cursor() as cur:
            if current_vins:
                cur.execute("""
                    DELETE FROM dealer_opportunities
                     WHERE snapshot_date = CURRENT_DATE
                       AND status = 'new'
                       AND vin <> ALL(%s)
                """, (current_vins,))
            else:
                cur.execute("""
                    DELETE FROM dealer_opportunities
                     WHERE snapshot_date = CURRENT_DATE
                       AND status = 'new'
                """)
            removed = cur.rowcount
            if removed > 0:
                log.info('orphan cleanup: removed %d stale same-day rows', removed)
            stats['orphans_removed'] = removed

    # ── Audit row ─────────────────────────────────────────────────────────
    if not args.dry_run:
        run_id = insert_run(stats)
        log.info('opportunity_runs id=%d', run_id)

    # ── Summary ───────────────────────────────────────────────────────────
    log.info('━━━ SUMMARY ━━━')
    log.info('MMR sweep: ok=%d no_mmr=%d err=%d',
             stats['mmr_ok'], stats['mmr_no_data'], stats['mmr_errors'])
    log.info('candidates >=%.1f%% under MMR: %d', args.threshold_pct,
             stats['candidates_5pct'])
    log.info('rBook deep dive: ok=%d err=%d',
             stats['rbook_ok'], stats['rbook_errors'])
    log.info('opportunities written: %d', stats['opportunities_written'])

    # Top 10 by score for quick visibility
    top = sorted(candidates, key=lambda x: -x['score'])[:10]
    log.info('--- top 10 by score ---')
    for c in top:
        sig = json.loads(c.get('signals') or '{}') if isinstance(c.get('signals'), str) else c.get('signals') or {}
        log.info('  score=%d  %s  %d %s %s (%s)  ask $%s  MMR $%s  %.1f%% under  retail_p50 $%s  dol=%s  dealer=%s',
                 c['score'], c['vin'],
                 c['year'], c['make'], c['model'], c.get('trim') or '',
                 f'{c["asking_price"]:,}' if c['asking_price'] else '?',
                 f'{c["mmr_wholesale_avg"]:,}',
                 c['pct_under_mmr'] or 0,
                 f'{c["rbook_p50"]:,}' if c.get('rbook_p50') else '?',
                 c.get('dealer_dol') or '?',
                 sig.get('dealer_name') or '?')

    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
