"""Porsche cross-market arbitrage nightly scanner.

Daily flow (intended to run from cron):
    1. Load Porsche anchor VINs from ymmt_catalog x ymmt_vin_cache.
    2. Open a porsche_arb_runs row with status='running'.
    3. For each anchor VIN (concurrency 4):
         a. strictYMM=True vehicleInfo prelude (mandatory — without this,
            high-trim cars like GT3 / Turbo S return ~1 comp instead of 200).
         b. fetch_competitive_set via direct BFF.
         c. Drop subject's own VIN from the comp set.
         d. Tag every comp with a US census-style region from dealer_state.
         e. Compute median / p25 / p75 / avg_dol per region (need >=3 to count).
         f. Upsert porsche_arb_regional_history (one row per anchor+region+date).
         g. For each comp = subject candidate:
              - home_region = region of this comp
              - best_other_region = region with highest median (count >= min_region_n)
              - raw_spread = best_other_median - effective_price
              - transport_estimate = $500 same region else $1500
              - net_spread = raw_spread - transport_estimate
              - flagged if net_spread >= min_spread + best_other_count gate
    4. Carfax pull (concurrency 2, capped 100 per run) on FLAGGED only.
    5. LSL anchor pull (best-effort, SQLite at /opt/livesaleslog/crm.db).
    6. Score 0-100 (per spec). Upsert porsche_arb_candidates.
    7. Finish porsche_arb_runs status='ok' with counters.

Bails on VAutoAuthError to preserve cookies for the bid pipeline
(exit code 2 — same convention as encore_comps_pipeline.py).

Usage:
    python3 porsche_arb_pipeline.py [--limit N] [--dry-run]
                                    [--skip-carfax] [--min-spread DOLLARS]
                                    [--min-region-n N]
"""
from __future__ import annotations
import argparse
import json
import logging
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

sys.path.insert(0, '/opt/expwholesale')

import psycopg2
import psycopg2.extras
import requests as _requests

from cookie_jar import CookieJar
from vauto_bff_direct import (
    fetch_competitive_set,
    fetch_price_guides,
    parse_competitive_set,
    VAutoAuthError as RBVAutoAuthError,
    VAutoServerError,
    VAutoBadRequestError,
)
from porsche_options import (
    canonicalize_kbb_fields,
    extract_comp_row_options,
    extract_vehicleinfo_options,
    option_jaccard,
    merge_option_sets,
)

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')
SESSION_PATH = '/opt/expwholesale/state/vauto_session.json'
VEHICLE_INFO_URL = (
    'https://slot2.bff.megazord.vauto.app.coxautoinc.com'
    '/api/appraisal/vehicleInfo?strictYMM=true'
)
CARFAX_URL = (
    'https://slot1.bff.megazord.vauto.app.coxautoinc.com/api/carfax/report'
)
LSL_DB_PATH = '/opt/livesaleslog/crm.db'

# State -> census region. DC handled into NE.
REGION_MAP = {}
for _st in 'CT ME MA NH NJ NY PA RI VT DC'.split():
    REGION_MAP[_st] = 'NE'
for _st in 'AL AR FL GA KY LA MS NC SC TN VA WV'.split():
    REGION_MAP[_st] = 'SE'
for _st in 'IA IL IN KS MI MN MO ND NE OH SD WI'.split():
    REGION_MAP[_st] = 'MW'
for _st in 'AZ NM OK TX'.split():
    REGION_MAP[_st] = 'SW'
for _st in 'AK CA CO HI ID MT NV OR UT WA WY'.split():
    REGION_MAP[_st] = 'W'

log = logging.getLogger('porsche_arb')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)

_auth_failed = threading.Event()


def conn():
    return psycopg2.connect(**DB)


# Phase 1: Anchor VIN load

def load_anchors(limit=None):
    """Load Porsche anchors from ymmt_catalog joined to ymmt_vin_cache.

    Same VIN may be reused across multiple trims when the catalog has many
    trims but only one anchor VIN was discovered — we still probe per (year,
    model, trim) but the comp set itself is keyed off the canonical VIN.
    """
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        sql = """
            SELECT yc.year, yc.model, yc.trim, v.vin
              FROM ymmt_catalog yc
              JOIN ymmt_vin_cache v
                ON yc.year = v.year
               AND lower(yc.make) = lower(v.make)
               AND lower(yc.model) = lower(v.model)
               AND yc.trim = v.trim
             WHERE lower(yc.make) = 'porsche'
               AND v.status = 'found'
               AND v.vin IS NOT NULL
               AND length(v.vin) = 17
             ORDER BY yc.year DESC, yc.model, yc.trim
        """
        if limit:
            sql += f' LIMIT {int(limit)}'
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def open_run():
    with conn() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO porsche_arb_runs (status) VALUES ('running') RETURNING id
        """)
        rid = cur.fetchone()[0]
        c.commit()
        return rid


def finish_run(run_id, *, status, attempted, succeeded, total_comps,
               flagged, carfax_pulls, error_log=None, note=None):
    with conn() as c, c.cursor() as cur:
        cur.execute("""
            UPDATE porsche_arb_runs
               SET finished_at = NOW(),
                   status = %s,
                   anchor_vins_attempted = %s,
                   anchor_vins_succeeded = %s,
                   total_comps_returned = %s,
                   candidates_flagged = %s,
                   carfax_pulls = %s,
                   error_log = %s,
                   note = %s
             WHERE id = %s
        """, (status, attempted, succeeded, total_comps, flagged,
              carfax_pulls, error_log, note, run_id))
        c.commit()


# Phase 2: rBook fetch per anchor (canonical strictYMM prelude)

def _fetch_vehicle_info(vin, cookies, headers, timeout=12):
    """Canonical vAuto decode via strictYMM=true. Returns (vehicleInfo,
    optionCodes) or None. Critical for high-trim Porsches — without this
    prelude, fetch_competitive_set returns ~1 row for GT3, Turbo S, etc."""
    try:
        r = _requests.post(
            VEHICLE_INFO_URL,
            json={'vin': vin, 'odometer': 0, 'odometerUom': 'Miles'},
            headers=headers, cookies=cookies, timeout=timeout,
        )
    except _requests.RequestException:
        return None
    if r.status_code in (401, 403):
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
    return vi, data.get('optionCodes')


def rbook_for_anchor(anchor, cookies, headers):
    """One anchor -> canonical decode -> competitive set -> comp rows."""
    if _auth_failed.is_set():
        return {'anchor': anchor, 'err': 'AUTH_PREEMPT', 'rows': []}
    vin = anchor['vin']
    try:
        decoded = _fetch_vehicle_info(vin, cookies, headers)
        if not decoded:
            return {'anchor': anchor,
                    'err': 'NO_CANONICAL_DECODE',
                    'rows': []}
        vehicle, option_codes = decoded
        if anchor.get('year') and not vehicle.get('year'):
            vehicle['year'] = anchor['year']
        body = fetch_competitive_set(
            vehicle, cookies, headers,
            list_price=0,
            appraisal_id='unused',
            option_codes=option_codes,
            timeout=25,
        )
        parsed = parse_competitive_set(body)
        rows = parsed.get('rows') or []
        sv = vin.upper()
        rows = [r for r in rows if (r.get('vin') or '').upper() != sv]
        return {'anchor': anchor,
                'rows': rows,
                'decoded_series': vehicle.get('series'),
                'decoded_vehicle': vehicle,  # full decode for phase 3.5
                'err': None}
    except RBVAutoAuthError as e:
        _auth_failed.set()
        return {'anchor': anchor, 'err': f'AUTH:{e}', 'rows': []}
    except (VAutoServerError, VAutoBadRequestError, Exception) as e:
        return {'anchor': anchor, 'err': f'{type(e).__name__}:{e}', 'rows': []}


# Phase 3: Regional aggregation + candidate scoring

def _percentiles(values):
    """Return (p25, p50, p75). Inputs assumed nonempty."""
    s = sorted(values)
    n = len(s)
    return (
        s[max(0, n // 4 - 1)],
        s[n // 2],
        s[min(n - 1, n * 3 // 4)],
    )


def _avg(values):
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def compute_regional_stats(rows):
    """region -> {n, median, p25, p75, avg_dol}. Only regions with >= 3 comps."""
    by_region = {}
    for r in rows:
        st = (r.get('dealer_state') or '').strip().upper()
        region = REGION_MAP.get(st)
        if not region:
            continue
        price = r.get('effective_price') or r.get('price')
        if not price or price <= 0:
            continue
        by_region.setdefault(region, []).append(r)
    stats = {}
    for region, region_rows in by_region.items():
        if len(region_rows) < 3:
            continue
        prices = [(r.get('effective_price') or r.get('price'))
                  for r in region_rows
                  if (r.get('effective_price') or r.get('price'))]
        p25, p50, p75 = _percentiles(prices)
        dols = [r.get('days_on_lot') for r in region_rows
                if isinstance(r.get('days_on_lot'), int) and r['days_on_lot'] >= 0]
        stats[region] = {
            'n': len(region_rows),
            'median': p50,
            'p25': p25,
            'p75': p75,
            'avg_dol': _avg(dols),
        }
    return stats


def upsert_regional_history(snapshot_date, anchor, stats):
    if not stats:
        return 0
    n = 0
    with conn() as c, c.cursor() as cur:
        for region, s in stats.items():
            cur.execute("""
                INSERT INTO porsche_arb_regional_history
                  (anchor_vin, year, model, trim, snapshot_date, region,
                   n, median_price, p25_price, p75_price, avg_dol)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (anchor_vin, snapshot_date, region) DO UPDATE SET
                  n = EXCLUDED.n,
                  median_price = EXCLUDED.median_price,
                  p25_price = EXCLUDED.p25_price,
                  p75_price = EXCLUDED.p75_price,
                  avg_dol = EXCLUDED.avg_dol,
                  year = EXCLUDED.year,
                  model = EXCLUDED.model,
                  trim = EXCLUDED.trim
            """, (
                anchor['vin'], anchor.get('year'), anchor.get('model'),
                anchor.get('trim'), snapshot_date, region,
                s['n'], s['median'], s['p25'], s['p75'], s['avg_dol'],
            ))
            n += 1
        c.commit()
    return n


def upsert_regional_comps(snapshot_date, anchor, rows):
    """PORSCHE_ARB_PHASE2_2026_05_27: persist every individual comp row
    that fed the regional median. Operator clicks through to "the cars
    we compared this against" — previously only the median was kept and
    individual comps were thrown away.

    UNIQUE on (anchor_vin, snapshot_date, comp_vin) so re-runs of the
    same day idempotently update prices/days. ~50K rows/day at full fleet.
    """
    if not rows:
        return 0
    n = 0
    anchor_vin = anchor['vin']
    with conn() as c, c.cursor() as cur:
        for r in rows:
            vin = (r.get('vin') or '').strip().upper()
            if not vin:
                continue
            st = (r.get('dealer_state') or '').strip().upper()
            region = REGION_MAP.get(st)
            try:
                cur.execute("""
                    INSERT INTO porsche_arb_regional_comps
                      (anchor_vin, snapshot_date, comp_vin,
                       year, make, model, trim,
                       body_style, drivetrain, transmission,
                       exterior_color, interior, mileage,
                       price, effective_price, days_on_lot,
                       is_certified, pending_sale,
                       carfax_one_owner, carfax_clean_title,
                       dealer_name, dealer_city, dealer_state,
                       dealer_postal, distance, region, detail_uri)
                    VALUES (%s,%s,%s, %s,%s,%s,%s,
                            %s,%s,%s, %s,%s,%s,
                            %s,%s,%s, %s,%s,
                            %s,%s, %s,%s,%s,
                            %s,%s,%s,%s)
                    ON CONFLICT (anchor_vin, snapshot_date, comp_vin) DO UPDATE SET
                      year = EXCLUDED.year,
                      make = EXCLUDED.make,
                      model = EXCLUDED.model,
                      trim = EXCLUDED.trim,
                      body_style = EXCLUDED.body_style,
                      drivetrain = EXCLUDED.drivetrain,
                      transmission = EXCLUDED.transmission,
                      exterior_color = EXCLUDED.exterior_color,
                      interior = EXCLUDED.interior,
                      mileage = EXCLUDED.mileage,
                      price = EXCLUDED.price,
                      effective_price = EXCLUDED.effective_price,
                      days_on_lot = EXCLUDED.days_on_lot,
                      is_certified = EXCLUDED.is_certified,
                      pending_sale = EXCLUDED.pending_sale,
                      carfax_one_owner = EXCLUDED.carfax_one_owner,
                      carfax_clean_title = EXCLUDED.carfax_clean_title,
                      dealer_name = EXCLUDED.dealer_name,
                      dealer_city = EXCLUDED.dealer_city,
                      dealer_state = EXCLUDED.dealer_state,
                      dealer_postal = EXCLUDED.dealer_postal,
                      distance = EXCLUDED.distance,
                      region = EXCLUDED.region,
                      detail_uri = EXCLUDED.detail_uri
                """, (
                    anchor_vin, snapshot_date, vin,
                    r.get('year'), r.get('make'), r.get('model'), r.get('trim'),
                    r.get('body'), r.get('drivetrain'), r.get('transmission'),
                    r.get('color'), r.get('interior'), r.get('mileage'),
                    r.get('price'), r.get('effective_price'),
                    r.get('days_on_lot'),
                    r.get('is_certified'), r.get('pending_sale'),
                    r.get('carfax_one_owner'), r.get('carfax_clean_title'),
                    r.get('dealer'), r.get('dealer_city'), r.get('dealer_state'),
                    r.get('dealer_postal'), r.get('distance'), region,
                    r.get('detail_uri'),
                ))
                n += 1
            except Exception as e:
                log.warning('regional_comps upsert failed for %s: %s', vin, e)
        c.commit()
    return n


def build_candidates(anchor, rows, stats, min_spread, min_region_n):
    """For each comp = candidate, compute spread vs the best other region."""
    out = []
    if not stats:
        return out
    for r in rows:
        st = (r.get('dealer_state') or '').strip().upper()
        home_region = REGION_MAP.get(st)
        if not home_region or home_region not in stats:
            continue
        home = stats[home_region]
        asking = r.get('effective_price') or r.get('price')
        if not asking or asking <= 0:
            continue

        best_other = None
        best_other_stats = None
        for region, s in stats.items():
            if region == home_region:
                continue
            if s['n'] < min_region_n:
                continue
            if best_other_stats is None or s['median'] > best_other_stats['median']:
                best_other = region
                best_other_stats = s

        flag_reasons = []
        raw_spread = None
        transport_estimate = None
        net_spread = None
        spread_pct = None
        flagged = False

        if best_other_stats:
            raw_spread = float(best_other_stats['median']) - float(asking)
            transport_estimate = 1500.0
            net_spread = raw_spread - transport_estimate
            if asking > 0:
                spread_pct = round(net_spread / float(asking) * 100.0, 3)
            if (net_spread >= min_spread
                    and best_other_stats['n'] >= min_region_n
                    and asking > 0):
                flagged = True
                flag_reasons.append(f'net_spread>={int(min_spread)}')
                if home.get('avg_dol') and home['avg_dol'] >= 60:
                    flag_reasons.append('home_aged>=60d')

        out.append({
            'anchor': anchor,
            'comp_row': r,
            'home_region': home_region,
            'home_region_median': home['median'],
            'home_region_count': home['n'],
            'best_other_region': best_other,
            'best_other_median': (best_other_stats['median']
                                   if best_other_stats else None),
            'best_other_count': (best_other_stats['n']
                                  if best_other_stats else None),
            'raw_spread': raw_spread,
            'transport_estimate': transport_estimate,
            'net_spread': net_spread,
            'spread_pct': spread_pct,
            'home_avg_dol': home.get('avg_dol'),
            'flagged': flagged,
            'flag_reasons': flag_reasons,
        })
    return out


# Phase 4: Carfax pull on FLAGGED only

def carfax_one(cand, cookies, headers):
    """Pull Carfax via vAuto BFF for one flagged candidate."""
    if _auth_failed.is_set():
        cand['carfax_err'] = 'AUTH_PREEMPT'
        return cand
    vin = cand['comp_row'].get('vin')
    if not vin:
        cand['carfax_err'] = 'NO_VIN'
        return cand
    try:
        r = _requests.get(f'{CARFAX_URL}?vin={vin}',
                          headers=headers, cookies=cookies, timeout=15)
        if r.status_code in (401, 403):
            _auth_failed.set()
            cand['carfax_err'] = f'AUTH:{r.status_code}'
            return cand
        if not r.ok:
            cand['carfax_err'] = f'HTTP:{r.status_code}'
            return cand
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
        cand['carfax'] = cf

        penalty = 0
        if cf.get('total_loss'):       penalty += 40
        if cf.get('frame_damage'):     penalty += 25
        if cf.get('odo_rollback'):     penalty += 20
        if cf.get('has_major'):        penalty += 15
        if cf.get('airbag_deployed'):  penalty += 10
        if not cf.get('clean_title'):  penalty += 20
        if cf.get('accidents', 0) >= 2:
            penalty += 10
        cand['carfax_penalty'] = penalty
    except Exception as e:
        cand['carfax_err'] = f'{type(e).__name__}:{e}'
    return cand


# Phase 5: LSL anchor lookup

def _lsl_lookup(year, model):
    """Best-effort 365-day LSL pattern lookup for same year+make+model."""
    if not model:
        return None
    try:
        c = sqlite3.connect(LSL_DB_PATH)
        try:
            cur = c.cursor()
            year_token = f'{year}' if year else ''
            cur.execute("""
                SELECT COUNT(*),
                       AVG(sale_price),
                       AVG(CASE WHEN julianday('now') - julianday(sold_at) <= 30  THEN total_value END),
                       AVG(CASE WHEN julianday('now') - julianday(sold_at) <= 90  THEN total_value END),
                       AVG(total_value)
                  FROM deals
                 WHERE lower(make_name) LIKE '%porsche%'
                   AND vehicle_info LIKE ?
                   AND (? = '' OR vehicle_info LIKE ?)
                   AND date(sold_at) >= date('now','-365 days')
            """, (f'%{model}%', year_token, f'%{year_token}%'))
            row = cur.fetchone()
            n_deals = int(row[0] or 0)
            if n_deals == 0:
                return None
            return {
                'n_deals': n_deals,
                'avg_sale': round(row[1], 2) if row[1] is not None else None,
                'avg_gross_30': round(row[2], 2) if row[2] is not None else None,
                'avg_gross_90': round(row[3], 2) if row[3] is not None else None,
                'avg_gross_365': round(row[4], 2) if row[4] is not None else None,
            }
        finally:
            c.close()
    except Exception as e:
        log.warning('LSL lookup failed for %s %s: %s', year, model, e)
        return None


# Phase 6: Score 0-100

def compute_score(cand):
    """0-100 composite. See spec for per-signal weights."""
    spread_pct = cand.get('spread_pct') or 0.0
    net_spread = cand.get('net_spread') or 0.0
    best_other_count = cand.get('best_other_count') or 0
    home_avg_dol = cand.get('home_avg_dol')

    # spread_pct (0-35 pts): 5%=10, 10%=20, 20%+=35
    if spread_pct <= 0:
        pts_pct = 0
    elif spread_pct >= 20:
        pts_pct = 35
    elif spread_pct >= 10:
        pts_pct = 20 + (spread_pct - 10) * 15 / 10
    elif spread_pct >= 5:
        pts_pct = 10 + (spread_pct - 5) * 10 / 5
    else:
        pts_pct = spread_pct * 10 / 5
    pts_pct = max(0, min(35, pts_pct))

    # net_spread absolute (0-15): $10K=5, $20K=10, $40K+=15
    if net_spread <= 0:
        pts_abs = 0
    elif net_spread >= 40000:
        pts_abs = 15
    elif net_spread >= 20000:
        pts_abs = 10 + (net_spread - 20000) * 5 / 20000
    elif net_spread >= 10000:
        pts_abs = 5 + (net_spread - 10000) * 5 / 10000
    else:
        pts_abs = net_spread * 5 / 10000
    pts_abs = max(0, min(15, pts_abs))

    # region_certainty (0-10): n=5 -> 2, n=10 -> 5, n=25+ -> 10
    if best_other_count >= 25:
        pts_cert = 10
    elif best_other_count >= 10:
        pts_cert = 5 + (best_other_count - 10) * 5 / 15
    elif best_other_count >= 5:
        pts_cert = 2 + (best_other_count - 5) * 3 / 5
    else:
        pts_cert = best_other_count * 2 / 5
    pts_cert = max(0, min(10, pts_cert))

    # low_home_dol (0-10): <30=10, 30-60=5, 60+=0
    if home_avg_dol is None:
        pts_dol = 0
    elif home_avg_dol < 30:
        pts_dol = 10
    elif home_avg_dol < 60:
        pts_dol = 5
    else:
        pts_dol = 0

    # carfax_clean (0-10): clean_title AND no accidents
    cf = cand.get('carfax') or {}
    if cf and cf.get('clean_title') and cf.get('accidents', 0) == 0:
        pts_cf = 10
    elif cf and cf.get('clean_title'):
        pts_cf = 5
    else:
        pts_cf = 0

    # lsl_pattern (0-10): avg_gross_365 >= $5K -> full
    lsl = cand.get('lsl_anchor') or {}
    g365 = lsl.get('avg_gross_365') or 0
    if g365 >= 5000:
        pts_lsl = 10
    elif g365 >= 2500:
        pts_lsl = 5
    elif g365 > 0:
        pts_lsl = 2
    else:
        pts_lsl = 0

    score = pts_pct + pts_abs + pts_cert + pts_dol + pts_cf + pts_lsl
    score -= cand.get('carfax_penalty') or 0
    return max(0, min(100, int(round(score))))


# Phase 7: Upsert candidates

def upsert_candidates(snapshot_date, candidates):
    """UPSERT one row per (snapshot_date, subject_vin). Dedupe by VIN within
    the run — same comp VIN may flow from multiple anchors; keep highest score.
    """
    n = 0
    seen = {}
    for cand in candidates:
        comp = cand['comp_row']
        vin = (comp.get('vin') or '').upper()
        if not vin:
            continue
        existing = seen.get(vin)
        if existing and (existing.get('arb_score') or 0) >= (cand.get('arb_score') or 0):
            continue
        seen[vin] = cand
    with conn() as c, c.cursor() as cur:
        for vin, cand in seen.items():
            comp = cand['comp_row']
            anchor = cand['anchor']
            cur.execute("""
                INSERT INTO porsche_arb_candidates
                  (snapshot_date, anchor_vin, anchor_year, anchor_model, anchor_trim,
                   subject_vin, subject_year, subject_make, subject_model, subject_trim,
                   subject_mileage, subject_color, subject_interior,
                   asking_price, effective_price, days_on_lot,
                   is_certified, pending_sale,
                   dealer_name, dealer_city, dealer_state, dealer_postal, detail_uri,
                   carfax_one_owner, carfax_clean_title,
                   home_region, home_region_median, home_region_count,
                   best_other_region, best_other_median, best_other_count,
                   raw_spread, transport_estimate, net_spread, spread_pct,
                   carfax_jsonb, carfax_penalty, lsl_anchor_jsonb,
                   arb_score, flagged, flag_reasons, status,
                   option_jaccard_to_anchor,
                   like_filtered_other_region, like_filtered_other_median,
                   like_filtered_other_count, like_filtered_net_spread,
                   like_filtered_spread_pct, arb_score_v2, arb_score_v3)
                VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,
                        %s,%s,%s, %s,%s,
                        %s,%s,%s,%s,%s, %s,%s,
                        %s,%s,%s, %s,%s,%s,
                        %s,%s,%s,%s,
                        %s::jsonb,%s,%s::jsonb,
                        %s,%s,%s,'new',
                        %s, %s,%s,%s, %s,%s, %s, %s)
                ON CONFLICT (snapshot_date, subject_vin) DO UPDATE SET
                  anchor_vin = EXCLUDED.anchor_vin,
                  anchor_year = EXCLUDED.anchor_year,
                  anchor_model = EXCLUDED.anchor_model,
                  anchor_trim = EXCLUDED.anchor_trim,
                  subject_year = EXCLUDED.subject_year,
                  subject_make = EXCLUDED.subject_make,
                  subject_model = EXCLUDED.subject_model,
                  subject_trim = EXCLUDED.subject_trim,
                  subject_mileage = EXCLUDED.subject_mileage,
                  subject_color = EXCLUDED.subject_color,
                  subject_interior = EXCLUDED.subject_interior,
                  asking_price = EXCLUDED.asking_price,
                  effective_price = EXCLUDED.effective_price,
                  days_on_lot = EXCLUDED.days_on_lot,
                  is_certified = EXCLUDED.is_certified,
                  pending_sale = EXCLUDED.pending_sale,
                  dealer_name = EXCLUDED.dealer_name,
                  dealer_city = EXCLUDED.dealer_city,
                  dealer_state = EXCLUDED.dealer_state,
                  dealer_postal = EXCLUDED.dealer_postal,
                  detail_uri = EXCLUDED.detail_uri,
                  carfax_one_owner = EXCLUDED.carfax_one_owner,
                  carfax_clean_title = EXCLUDED.carfax_clean_title,
                  home_region = EXCLUDED.home_region,
                  home_region_median = EXCLUDED.home_region_median,
                  home_region_count = EXCLUDED.home_region_count,
                  best_other_region = EXCLUDED.best_other_region,
                  best_other_median = EXCLUDED.best_other_median,
                  best_other_count = EXCLUDED.best_other_count,
                  raw_spread = EXCLUDED.raw_spread,
                  transport_estimate = EXCLUDED.transport_estimate,
                  net_spread = EXCLUDED.net_spread,
                  spread_pct = EXCLUDED.spread_pct,
                  carfax_jsonb = EXCLUDED.carfax_jsonb,
                  carfax_penalty = EXCLUDED.carfax_penalty,
                  lsl_anchor_jsonb = EXCLUDED.lsl_anchor_jsonb,
                  arb_score = EXCLUDED.arb_score,
                  flagged = EXCLUDED.flagged,
                  flag_reasons = EXCLUDED.flag_reasons,
                  option_jaccard_to_anchor = EXCLUDED.option_jaccard_to_anchor,
                  like_filtered_other_region = EXCLUDED.like_filtered_other_region,
                  like_filtered_other_median = EXCLUDED.like_filtered_other_median,
                  like_filtered_other_count = EXCLUDED.like_filtered_other_count,
                  like_filtered_net_spread = EXCLUDED.like_filtered_net_spread,
                  like_filtered_spread_pct = EXCLUDED.like_filtered_spread_pct,
                  arb_score_v2 = EXCLUDED.arb_score_v2,
                  arb_score_v3 = EXCLUDED.arb_score_v3,
                  updated_at = NOW()
            """, (
                snapshot_date, anchor['vin'], anchor.get('year'),
                anchor.get('model'), anchor.get('trim'),
                vin, comp.get('year'), comp.get('make'),
                comp.get('model'), comp.get('trim'),
                comp.get('mileage'), comp.get('color'), comp.get('interior'),
                comp.get('price'), comp.get('effective_price'),
                comp.get('days_on_lot'),
                comp.get('is_certified'), comp.get('pending_sale'),
                comp.get('dealer'), comp.get('dealer_city'),
                comp.get('dealer_state'), comp.get('dealer_postal'),
                comp.get('detail_uri'),
                comp.get('carfax_one_owner'), comp.get('carfax_clean_title'),
                cand.get('home_region'), cand.get('home_region_median'),
                cand.get('home_region_count'),
                cand.get('best_other_region'), cand.get('best_other_median'),
                cand.get('best_other_count'),
                cand.get('raw_spread'), cand.get('transport_estimate'),
                cand.get('net_spread'), cand.get('spread_pct'),
                json.dumps(cand.get('carfax')) if cand.get('carfax') else None,
                cand.get('carfax_penalty') or 0,
                json.dumps(cand.get('lsl_anchor')) if cand.get('lsl_anchor') else None,
                cand.get('arb_score'), cand.get('flagged'),
                cand.get('flag_reasons') or [],
                cand.get('option_jaccard_to_anchor'),
                cand.get('like_filtered_other_region'),
                cand.get('like_filtered_other_median'),
                cand.get('like_filtered_other_count'),
                cand.get('like_filtered_net_spread'),
                cand.get('like_filtered_spread_pct'),
                cand.get('arb_score_v2'),
                cand.get('arb_score_v3'),
            ))
            n += 1
        c.commit()
    return n


# Phase 3.5: Option enrichment (PORSCHE_ARB_OPTIONS_2026_05_26)
#
# vAuto's priceGuides endpoint was the original target per the audit but
# returns ONLY Condition + Style fields for Porsches (verified 2026-05-26;
# 2 fields, no equipment). So this phase is split into two sources:
#   1. 'kbb_equipment' — scaffolded for if/when Cox enables equipment
#      data on our entity tier. Today returns empty options.
#   2. 'comp_row_signals' — workhorse. Extracts CPO, drivetrain,
#      transmission, premium interior, non-standard color from the
#      structured fields vAuto already returns per comp.
# See porsche_options.py for the full rationale.

def _options_recent_in_db(vin: str, source: str, max_age_days: int = 7) -> bool:
    """Check the cache to avoid redundant KBB pulls within max_age_days."""
    with conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM porsche_arb_options
             WHERE subject_vin = %s
               AND source = %s
               AND fetched_at > NOW() - (%s || ' days')::interval
             LIMIT 1
        """, (vin, source, str(max_age_days)))
        return cur.fetchone() is not None


def _upsert_options(vin: str, source: str, options: dict,
                    raw: list | None = None, msrp: float | None = None) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO porsche_arb_options
              (subject_vin, source, options_jsonb, options_raw, msrp, fetched_at)
            VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, NOW())
            ON CONFLICT (subject_vin, source) DO UPDATE SET
              options_jsonb = EXCLUDED.options_jsonb,
              options_raw   = EXCLUDED.options_raw,
              msrp          = EXCLUDED.msrp,
              fetched_at    = NOW()
        """, (vin, source, json.dumps(options or {}),
              json.dumps(raw or []) if raw is not None else None,
              msrp))
        c.commit()


def _fetch_options_kbb(vin: str, vehicle: dict,
                       cookies: dict, headers: dict,
                       appraisal_id: str = 'unused') -> tuple[dict, list, float | None]:
    """Call fetch_price_guides and pull KBB equipment options.

    For Porsches this currently returns an EMPTY canonical dict because
    Cox's KBB block only exposes Condition + Style fields. The function
    still runs to (a) log the empty case, (b) capture msrp if present,
    (c) future-proof against Cox enabling equipment data later.
    """
    try:
        resp = fetch_price_guides(vehicle, cookies, headers,
                                  appraisal_id=appraisal_id, timeout=12)
    except RBVAutoAuthError:
        _auth_failed.set()
        raise
    except (VAutoServerError, VAutoBadRequestError, Exception):
        return {}, [], None
    kbb = (resp or {}).get('kbb') or {}
    fields = kbb.get('fields') or []
    canonical, raw = canonicalize_kbb_fields(fields)
    # MSRP is not exposed in the KBB block today; pricings[] sometimes
    # has a "Original MSRP" line but it's not reliable. Leave None.
    return canonical, raw, None


def _enrich_one(vin: str, vehicle: dict, comp_row: dict | None,
                cookies: dict, headers: dict,
                appraisal_id: str, skip_kbb: bool = False,
                anchor_meta: dict | None = None) -> dict:
    """Combined enrichment for one VIN. Returns the merged canonical
    options dict that will be used for Jaccard similarity.

    For anchors (comp_row=None), uses the decoded vehicle dict +
    anchor_meta (year/model/trim) to derive transmission + drivetrain
    inference. For candidates (vehicle=None, comp_row=<row>), uses the
    comp_row's structured fields directly.
    """
    if _auth_failed.is_set():
        return {}

    kbb_canonical: dict = {}
    if not skip_kbb and vehicle and not _options_recent_in_db(vin, 'kbb_equipment'):
        try:
            kbb_canonical, kbb_raw, kbb_msrp = _fetch_options_kbb(
                vin, vehicle, cookies, headers, appraisal_id)
            _upsert_options(vin, 'kbb_equipment', kbb_canonical, kbb_raw, kbb_msrp)
        except RBVAutoAuthError:
            return {}

    # Always extract comp-row signals — this is the workhorse source.
    comp_signals: dict = {}
    if comp_row:
        comp_signals = extract_comp_row_options(comp_row)
        # Cache per-VIN; if a VIN appears as a comp from multiple anchors
        # the data is identical, so dedupe via UPSERT.
        if comp_signals:
            try:
                _upsert_options(vin, 'comp_row_signals', comp_signals, None, None)
            except Exception as e:
                log.warning('comp_row_signals upsert failed for %s: %s', vin, e)

    # Anchor-only path: extract from vehicleInfo decode + anchor trim
    # (a stripper Carrera vs Carrera 4 GTS shouldn't both be drivetrain=None).
    vi_signals: dict = {}
    if vehicle and not comp_row:
        vi_signals = extract_vehicleinfo_options(vehicle, anchor_meta)
        if vi_signals:
            try:
                _upsert_options(vin, 'comp_row_signals', vi_signals, None, None)
            except Exception as e:
                log.warning('anchor vi_signals upsert failed for %s: %s', vin, e)

    return merge_option_sets(kbb_canonical, comp_signals, vi_signals)


def _load_options_from_db(vins: list[str]) -> dict[str, dict]:
    """Bulk-load merged options for a list of VINs from porsche_arb_options."""
    if not vins:
        return {}
    out: dict[str, dict] = {}
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT subject_vin, source, options_jsonb
              FROM porsche_arb_options
             WHERE subject_vin = ANY(%s)
        """, (list({v.upper() for v in vins if v}),))
        for row in cur.fetchall():
            v = row['subject_vin'].upper()
            existing = out.get(v, {})
            opts = row['options_jsonb'] or {}
            out[v] = merge_option_sets(existing, opts)
    return out


def enrich_options_phase(anchors: list[dict],
                         results: list[dict],
                         all_candidates: list[dict],
                         cookies: dict, headers: dict,
                         appraisal_id: str,
                         flagged_cap: int = 200,
                         concurrency: int = 4,
                         skip_kbb: bool = False) -> tuple[int, int]:
    """Phase 3.5 — options enrichment.

    For every FLAGGED candidate (up to flagged_cap), and for every anchor
    VIN, fetch KBB equipment (no-op today) + extract comp-row signals.
    Returns (anchor_enriched_count, candidate_enriched_count).

    skip_kbb=True is used during testing to avoid the BFF call overhead
    when we already know KBB returns empty for Porsches.
    """
    # Build vehicle dict map for anchors (rbook returned decoded_vehicle).
    # Same anchor VIN may appear under multiple (year, model, trim) entries
    # in ymmt_catalog — keep the first non-error decode.
    anchor_vehicle = {}
    for r in results:
        a = r.get('anchor') or {}
        vin = a.get('vin')
        if (vin and r.get('rows') is not None and not r.get('err')
                and vin not in anchor_vehicle):
            anchor_vehicle[vin] = r.get('decoded_vehicle') or {
                'year':  a.get('year'),
                'make':  'Porsche',
                'model': a.get('model'),
                'trim':  a.get('trim'),
                'series': r.get('decoded_series'),
            }

    # Anchor enrichment (one-shot — anchors are stable across the run)
    # Build anchor_meta lookup (year/model/trim) for vehicleInfo
    # transmission/drivetrain inference.
    anchor_meta_by_vin = {}
    for r in results:
        a = r.get('anchor') or {}
        vin = a.get('vin')
        if vin:
            anchor_meta_by_vin[vin] = {
                'year': a.get('year'), 'model': a.get('model'),
                'trim': a.get('trim'),
            }
    anchor_count = 0
    anchor_vins = list(anchor_vehicle.keys())
    if anchor_vins:
        log.info('phase 3.5: enriching %d anchor VINs', len(anchor_vins))
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = {pool.submit(_enrich_one,
                                vin, anchor_vehicle[vin], None,
                                cookies, headers, appraisal_id, skip_kbb,
                                anchor_meta_by_vin.get(vin)): vin
                    for vin in anchor_vins}
            for fut in as_completed(futs):
                try:
                    fut.result()
                    anchor_count += 1
                except RBVAutoAuthError:
                    log.error('auth failed during anchor enrichment')
                    break
                except Exception as e:
                    log.warning('anchor enrich error: %s', e)
        log.info('phase 3.5 anchors done in %.1fs: %d/%d',
                 time.monotonic() - t0, anchor_count, len(anchor_vins))

    if _auth_failed.is_set():
        return anchor_count, 0

    # Candidate enrichment — FLAGGED only, sorted by net_spread DESC,
    # deduped by VIN, capped at flagged_cap.
    flagged_sorted = sorted(
        [c for c in all_candidates if c.get('flagged')],
        key=lambda c: (c.get('net_spread') or 0), reverse=True,
    )
    seen_vin = set()
    unique_flagged = []
    for c in flagged_sorted:
        vin = (c['comp_row'].get('vin') or '').upper()
        if not vin or vin in seen_vin:
            continue
        seen_vin.add(vin)
        unique_flagged.append(c)
    unique_flagged = unique_flagged[:flagged_cap]

    cand_count = 0
    if unique_flagged:
        log.info('phase 3.5: enriching %d flagged candidate VINs (cap=%d)',
                 len(unique_flagged), flagged_cap)
        t0 = time.monotonic()
        # Build a vehicle dict per candidate from the comp_row.
        def _cand_vehicle(comp):
            return {
                'year':  comp.get('year'),
                'make':  comp.get('make') or 'Porsche',
                'model': comp.get('model'),
                'trim':  comp.get('trim'),
                'bodyType': comp.get('body'),
            }
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = []
            for cand in unique_flagged:
                vin = cand['comp_row']['vin'].upper()
                # For comp candidates we DON'T have a canonical decoded
                # vehicle (would require an extra strictYMM vehicleInfo
                # call per VIN). Skip the KBB call on candidates by
                # default — it returns empty for Porsches anyway and
                # would be 200+ extra requests per run. Just extract
                # comp-row signals.
                futs.append(pool.submit(_enrich_one, vin, None,
                                        cand['comp_row'], cookies, headers,
                                        appraisal_id, True))
            for fut in as_completed(futs):
                try:
                    fut.result()
                    cand_count += 1
                except RBVAutoAuthError:
                    log.error('auth failed during candidate enrichment')
                    break
                except Exception as e:
                    log.warning('candidate enrich error: %s', e)
        log.info('phase 3.5 candidates done in %.1fs: %d/%d',
                 time.monotonic() - t0, cand_count, len(unique_flagged))

    # ALSO extract comp-row signals for ALL comps that will be used as
    # potential pool members for like-filtered comparisons. We're not
    # storing these in DB (would blow up the table) — keep them in
    # memory on the candidate's comp_row.
    for r in results:
        for row in r.get('rows') or []:
            if not row.get('_options'):
                row['_options'] = extract_comp_row_options(row)

    return anchor_count, cand_count


# Phase 6.5: Like-filtered re-scoring
#
# For each flagged candidate:
#   1. Compute option_jaccard vs the anchor's options.
#   2. Build like-comp pool from OTHER regions where comp's Jaccard to
#      candidate >= 0.5. Use trim+drivetrain+transmission as a STRICT
#      fallback gate when both sides have empty option dicts.
#   3. If like-pool >= 3 entries, compute new median per region; pick
#      best other region's median; recompute spread.
#   4. arb_score_v2 = same scoring formula as v1 but with like-filtered
#      comps swapped in. carfax_penalty preserved.

LIKE_JACCARD_THRESHOLD = 0.5
LIKE_MIN_POOL = 3


def _strict_trim_match(a_row: dict, b_row: dict) -> bool:
    """Hard fallback when option dicts are empty: require exact match on
    trim+drivetrain+transmission_type+is_certified."""
    if (a_row.get('trim') or '').strip().lower() != (b_row.get('trim') or '').strip().lower():
        return False
    if (a_row.get('drivetrain') or '').strip().upper() != (b_row.get('drivetrain') or '').strip().upper():
        return False
    a_trans = (a_row.get('transmission') or '').strip().lower()
    b_trans = (b_row.get('transmission') or '').strip().lower()
    a_manual = 'manual' in a_trans
    b_manual = 'manual' in b_trans
    if a_manual != b_manual:
        return False
    # is_certified within tolerance (CPO adds ~$2-4K — material)
    if bool(a_row.get('is_certified')) != bool(b_row.get('is_certified')):
        return False
    return True


def build_trim_index(results: list[dict]) -> dict[str, list[dict]]:
    """Pre-build a trim → [rows] index across ALL anchors. This turns
    the O(N×M) pool-search into O(N + small_trim_bucket) per candidate.
    Also pre-extracts _options on each row so build_like_pool doesn't
    have to recompute."""
    idx: dict[str, list[dict]] = {}
    by_anchor: dict[str, list[dict]] = {}
    for r in results:
        if r.get('err'):
            continue
        anchor_vin = (r.get('anchor') or {}).get('vin')
        for row in r.get('rows') or []:
            if not (row.get('effective_price') or row.get('price')):
                continue
            trim_key = (row.get('trim') or '').strip().lower()
            year = row.get('year')
            # Bucket by (trim, year) — most precise. Also keep a trim-only
            # bucket for cross-year matches within tolerance.
            if trim_key:
                idx.setdefault(trim_key, []).append(row)
            if anchor_vin:
                by_anchor.setdefault(anchor_vin, []).append(row)
            # Pre-extract options once
            if not row.get('_options'):
                row['_options'] = extract_comp_row_options(row)
    return idx, by_anchor


def build_like_pool(candidate: dict, trim_idx: dict, by_anchor: dict,
                    anchor_options: dict | None = None) -> tuple[list[dict], dict]:
    """For a single flagged candidate, build a like-for-like comp pool
    drawn from OTHER regions across all anchor results. Returns
    (like_rows, candidate_options_used).

    Uses a pre-built trim-keyed index from build_trim_index() to keep
    per-candidate cost ~O(trim_bucket_size) instead of O(total_comps).
    """
    cand_row = candidate.get('comp_row') or {}
    cand_vin = (cand_row.get('vin') or '').upper()
    cand_trim = (cand_row.get('trim') or '').strip().lower()
    cand_year = cand_row.get('year')

    cand_options = cand_row.get('_options') or extract_comp_row_options(cand_row)
    cand_row['_options'] = cand_options

    candidate_anchor_vin = candidate['anchor'].get('vin')

    # Candidate pool: same trim + same anchor's other comps (deduped)
    candidates_to_check: list[dict] = []
    seen_vins: set[str] = {cand_vin}
    if cand_trim and cand_trim in trim_idx:
        for row in trim_idx[cand_trim]:
            row_vin = (row.get('vin') or '').upper()
            if not row_vin or row_vin in seen_vins:
                continue
            seen_vins.add(row_vin)
            candidates_to_check.append(row)
    # Also include all of the candidate's OWN anchor's comps (in case
    # the parser truncated trim — anchor relevance is the stronger
    # signal anyway).
    for row in by_anchor.get(candidate_anchor_vin, []):
        row_vin = (row.get('vin') or '').upper()
        if not row_vin or row_vin in seen_vins:
            continue
        seen_vins.add(row_vin)
        candidates_to_check.append(row)

    pool: list[dict] = []
    for row in candidates_to_check:
        # Year tolerance
        row_year = row.get('year')
        if cand_year and row_year and abs(int(row_year) - int(cand_year)) > 1:
            continue
        # Apply option/strict gate
        row_options = row.get('_options') or {}
        jacc = option_jaccard(cand_options, row_options)
        strict = _strict_trim_match(cand_row, row)
        if jacc < LIKE_JACCARD_THRESHOLD and not strict:
            continue
        pool.append(row)
    return pool, cand_options


def compute_like_filtered_stats(pool: list[dict], exclude_region: str | None
                                ) -> tuple[str | None, float | None, int]:
    """Per-region aggregation across the like-filtered pool, picking the
    region with the highest median that ISN'T exclude_region and has
    >= LIKE_MIN_POOL entries."""
    by_region: dict[str, list[float]] = {}
    for row in pool:
        st = (row.get('dealer_state') or '').strip().upper()
        region = REGION_MAP.get(st)
        if not region or region == exclude_region:
            continue
        price = row.get('effective_price') or row.get('price')
        if not price:
            continue
        by_region.setdefault(region, []).append(float(price))

    best_region = None
    best_median = None
    best_count = 0
    for region, prices in by_region.items():
        if len(prices) < LIKE_MIN_POOL:
            continue
        med = _percentiles(prices)[1]
        if best_median is None or med > best_median:
            best_region = region
            best_median = med
            best_count = len(prices)
    return best_region, best_median, best_count


def compute_score_v2(cand: dict) -> int | None:
    """Same scoring math as v1 but using like_filtered_* fields. Returns
    None when there isn't enough like-filtered data to score."""
    if cand.get('like_filtered_other_median') is None:
        return None

    asking = cand.get('comp_row', {}).get('effective_price') or cand.get('comp_row', {}).get('price') or 0
    if not asking:
        return None
    like_spread = float(cand['like_filtered_other_median']) - float(asking)
    transport = cand.get('transport_estimate') or 1500.0
    like_net = like_spread - transport
    like_pct = like_net / float(asking) * 100.0 if asking else 0.0
    like_count = cand.get('like_filtered_other_count') or 0
    home_avg_dol = cand.get('home_avg_dol')

    # spread_pct (0-35)
    if like_pct <= 0:
        pts_pct = 0
    elif like_pct >= 20:
        pts_pct = 35
    elif like_pct >= 10:
        pts_pct = 20 + (like_pct - 10) * 15 / 10
    elif like_pct >= 5:
        pts_pct = 10 + (like_pct - 5) * 10 / 5
    else:
        pts_pct = like_pct * 10 / 5
    pts_pct = max(0, min(35, pts_pct))

    # net_spread abs (0-15)
    if like_net <= 0:
        pts_abs = 0
    elif like_net >= 40000:
        pts_abs = 15
    elif like_net >= 20000:
        pts_abs = 10 + (like_net - 20000) * 5 / 20000
    elif like_net >= 10000:
        pts_abs = 5 + (like_net - 10000) * 5 / 10000
    else:
        pts_abs = like_net * 5 / 10000
    pts_abs = max(0, min(15, pts_abs))

    # region_certainty (0-10)
    if like_count >= 25:
        pts_cert = 10
    elif like_count >= 10:
        pts_cert = 5 + (like_count - 10) * 5 / 15
    elif like_count >= 5:
        pts_cert = 2 + (like_count - 5) * 3 / 5
    else:
        pts_cert = like_count * 2 / 5
    pts_cert = max(0, min(10, pts_cert))

    # low_home_dol (0-10)
    if home_avg_dol is None:
        pts_dol = 0
    elif home_avg_dol < 30:
        pts_dol = 10
    elif home_avg_dol < 60:
        pts_dol = 5
    else:
        pts_dol = 0

    cf = cand.get('carfax') or {}
    if cf and cf.get('clean_title') and cf.get('accidents', 0) == 0:
        pts_cf = 10
    elif cf and cf.get('clean_title'):
        pts_cf = 5
    else:
        pts_cf = 0

    lsl = cand.get('lsl_anchor') or {}
    g365 = lsl.get('avg_gross_365') or 0
    if g365 >= 5000:
        pts_lsl = 10
    elif g365 >= 2500:
        pts_lsl = 5
    elif g365 > 0:
        pts_lsl = 2
    else:
        pts_lsl = 0

    score = pts_pct + pts_abs + pts_cert + pts_dol + pts_cf + pts_lsl
    score -= cand.get('carfax_penalty') or 0
    return max(0, min(100, int(round(score))))


def rescore_candidates(all_candidates: list[dict],
                       results: list[dict],
                       anchor_options_by_vin: dict[str, dict]) -> int:
    """Phase 6.5 — compute like_filtered_* + arb_score_v2 for every
    flagged candidate. Mutates all_candidates in place. Returns the
    count of candidates that got an arb_score_v2.

    Uses a pre-built trim-keyed index to avoid O(N²) pool search.
    """
    log.info('phase 6.5: building trim index across %d anchor result sets',
             len(results))
    t0 = time.monotonic()
    trim_idx, by_anchor = build_trim_index(results)
    log.info('phase 6.5: trim index has %d trims, %d anchors',
             len(trim_idx), len(by_anchor))

    rescored = 0
    thin_pool = 0
    flagged_total = sum(1 for c in all_candidates if c.get('flagged'))
    log.info('phase 6.5: scoring %d flagged candidates', flagged_total)
    for cand in all_candidates:
        if not cand.get('flagged'):
            continue
        # Anchor options for this candidate's anchor
        anchor_vin = (cand['anchor'].get('vin') or '').upper()
        anchor_options = anchor_options_by_vin.get(anchor_vin, {})

        pool, cand_options = build_like_pool(cand, trim_idx, by_anchor, anchor_options)
        # Always compute jaccard vs anchor (for display) — useful even
        # when the pool itself is thin
        cand['option_jaccard_to_anchor'] = option_jaccard(cand_options, anchor_options)
        cand['_options'] = cand_options

        if len(pool) < LIKE_MIN_POOL:
            cand['flag_reasons'] = list(cand.get('flag_reasons') or [])
            if 'thin_like_pool' not in cand['flag_reasons']:
                cand['flag_reasons'].append('thin_like_pool')
            thin_pool += 1
            continue

        region, median, count = compute_like_filtered_stats(
            pool, exclude_region=cand.get('home_region'))
        if region is None or median is None or count < LIKE_MIN_POOL:
            cand['flag_reasons'] = list(cand.get('flag_reasons') or [])
            if 'thin_like_pool' not in cand['flag_reasons']:
                cand['flag_reasons'].append('thin_like_pool')
            thin_pool += 1
            continue

        cand['like_filtered_other_region'] = region
        cand['like_filtered_other_median'] = median
        cand['like_filtered_other_count']  = count

        asking = (cand['comp_row'].get('effective_price')
                  or cand['comp_row'].get('price'))
        if asking:
            transport = cand.get('transport_estimate') or 1500.0
            like_net = float(median) - float(asking) - transport
            cand['like_filtered_net_spread'] = like_net
            if asking > 0:
                pct = round(like_net / float(asking) * 100.0, 3)
                # NUMERIC(6,3) max abs = 999.999. Clamp to keep upsert
                # safe; in practice anything over 100% is already noise.
                cand['like_filtered_spread_pct'] = max(-999.0, min(999.0, pct))
            else:
                cand['like_filtered_spread_pct'] = None

        cand['arb_score_v2'] = compute_score_v2(cand)
        if cand['arb_score_v2'] is not None:
            rescored += 1
    log.info('phase 6.5: done in %.1fs — rescored=%d thin_pool=%d',
             time.monotonic() - t0, rescored, thin_pool)
    return rescored


# Phase 8 (PORSCHE_ARB_PHASE2_2026_05_27): v3 rescore using
# union(KBB + comp_row + detail_scrape). The detail-scrape classifier
# recovers the high-impact options (PCCB, Sport Chrono, Burmester, PDCC,
# carbon roof, PTS, Weissach, Clubsport, Lightweight) that the comp-row
# signals can't see.
#
# Like-pool gate is identical math to v2 but operates on the merged
# option set. The home/best-other comp pool is sourced from
# porsche_arb_regional_comps which now contains every comp row, not just
# the regional median.

V3_HIGH_IMPACT_KEYS = (
    'pccb', 'sport_chrono', 'pdcc', 'burmester', 'carbon_roof',
    'pts_paint', 'weissach', 'clubsport', 'lightweight_package',
)
V3_HIGH_IMPACT_JACC = 0.6  # task-spec gate


def _v3_jaccard_high_impact(a: dict, b: dict) -> float:
    """Jaccard on HIGH-IMPACT options only (PCCB, Sport Chrono, PDCC,
    Burmester, carbon_roof, PTS, Weissach, Clubsport, Lightweight).

    If neither side has ANY high-impact key set → 1.0 (no info to
    discriminate — better than penalizing every comp at 0.0).
    """
    keys_a = {k for k in V3_HIGH_IMPACT_KEYS if (a or {}).get(k)}
    keys_b = {k for k in V3_HIGH_IMPACT_KEYS if (b or {}).get(k)}
    union = keys_a | keys_b
    if not union:
        return 1.0
    return round(len(keys_a & keys_b) / len(union), 3)


def _load_options_full_merged(vins: list[str]) -> dict[str, dict]:
    """Union all sources (kbb_equipment + comp_row_signals + detail_scrape)
    for each VIN, returning {VIN_UPPER: {option_key: True, ...}}."""
    if not vins:
        return {}
    upper = list({v.upper() for v in vins if v})
    out: dict[str, dict] = {}
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT subject_vin, source, options_jsonb
              FROM porsche_arb_options
             WHERE subject_vin = ANY(%s)
        """, (upper,))
        for row in cur.fetchall():
            v = row['subject_vin'].upper()
            existing = out.get(v, {})
            opts = row['options_jsonb'] or {}
            out[v] = merge_option_sets(existing, opts)
    return out


def _load_regional_comp_rows_for_anchor(
        anchor_vin: str, snapshot_date) -> list[dict]:
    """Pull all regional comp rows for a given anchor and snapshot from
    porsche_arb_regional_comps. Returns list of dicts with comp_vin +
    pricing + region."""
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT comp_vin AS vin, year, make, model, trim,
                   body_style, drivetrain, transmission,
                   exterior_color AS color, interior, mileage,
                   price, effective_price, days_on_lot,
                   is_certified, pending_sale,
                   dealer_name AS dealer, dealer_city, dealer_state,
                   dealer_postal, region, detail_uri
              FROM porsche_arb_regional_comps
             WHERE anchor_vin = %s AND snapshot_date = %s
        """, (anchor_vin, snapshot_date))
        return [dict(r) for r in cur.fetchall()]


def compute_score_v3(cand: dict) -> int | None:
    """Same formula as compute_score_v2 but reads v3_like_filtered_*
    fields. Falls back to v2 if v3 wasn't computed."""
    median = cand.get('v3_like_filtered_other_median')
    if median is None:
        return None
    asking = (cand.get('comp_row', {}).get('effective_price')
              or cand.get('comp_row', {}).get('price') or 0)
    if not asking:
        return None
    like_spread = float(median) - float(asking)
    transport = cand.get('transport_estimate') or 1500.0
    like_net = like_spread - transport
    like_pct = like_net / float(asking) * 100.0 if asking else 0.0
    like_count = cand.get('v3_like_filtered_other_count') or 0
    home_avg_dol = cand.get('home_avg_dol')

    if like_pct <= 0:
        pts_pct = 0
    elif like_pct >= 20:
        pts_pct = 35
    elif like_pct >= 10:
        pts_pct = 20 + (like_pct - 10) * 15 / 10
    elif like_pct >= 5:
        pts_pct = 10 + (like_pct - 5) * 10 / 5
    else:
        pts_pct = like_pct * 10 / 5
    pts_pct = max(0, min(35, pts_pct))

    if like_net <= 0:
        pts_abs = 0
    elif like_net >= 40000:
        pts_abs = 15
    elif like_net >= 20000:
        pts_abs = 10 + (like_net - 20000) * 5 / 20000
    elif like_net >= 10000:
        pts_abs = 5 + (like_net - 10000) * 5 / 10000
    else:
        pts_abs = like_net * 5 / 10000
    pts_abs = max(0, min(15, pts_abs))

    if like_count >= 25:
        pts_cert = 10
    elif like_count >= 10:
        pts_cert = 5 + (like_count - 10) * 5 / 15
    elif like_count >= 5:
        pts_cert = 2 + (like_count - 5) * 3 / 5
    else:
        pts_cert = like_count * 2 / 5
    pts_cert = max(0, min(10, pts_cert))

    if home_avg_dol is None:
        pts_dol = 0
    elif home_avg_dol < 30:
        pts_dol = 10
    elif home_avg_dol < 60:
        pts_dol = 5
    else:
        pts_dol = 0

    cf = cand.get('carfax') or {}
    if cf and cf.get('clean_title') and cf.get('accidents', 0) == 0:
        pts_cf = 10
    elif cf and cf.get('clean_title'):
        pts_cf = 5
    else:
        pts_cf = 0

    lsl = cand.get('lsl_anchor') or {}
    g365 = lsl.get('avg_gross_365') or 0
    if g365 >= 5000:
        pts_lsl = 10
    elif g365 >= 2500:
        pts_lsl = 5
    elif g365 > 0:
        pts_lsl = 2
    else:
        pts_lsl = 0

    score = pts_pct + pts_abs + pts_cert + pts_dol + pts_cf + pts_lsl
    score -= cand.get('carfax_penalty') or 0
    return max(0, min(100, int(round(score))))


def rescore_candidates_v3(all_candidates: list[dict],
                           results: list[dict],
                           anchor_options_by_vin: dict[str, dict],
                           snapshot_date_override=None) -> int:
    """Phase 8 — v3 rescore.

    Uses merged option set (KBB + comp_row + detail_scrape) and filters
    the like-pool by HIGH-IMPACT Jaccard >= 0.6. Pool is sourced from
    porsche_arb_regional_comps (populated by upsert_regional_comps in
    Phase 3), so it sees ALL comps not just those from this run's
    anchors.

    Returns count of candidates that got an arb_score_v3.
    """
    t0 = time.monotonic()

    # Bulk-load the merged option set for every anchor + flagged
    # candidate (one DB hit per side).
    cand_vins = [(c.get('comp_row') or {}).get('vin')
                  for c in all_candidates if c.get('flagged')]
    cand_vins = [v for v in cand_vins if v]
    cand_options_by_vin = _load_options_full_merged(cand_vins)
    log.info('phase 8: loaded merged options for %d candidate VINs',
             len(cand_options_by_vin))

    # Pre-resolve snapshot date once (use today's by default; tests can override)
    if snapshot_date_override is not None:
        snapshot_date = snapshot_date_override
    else:
        from datetime import date as _date
        snapshot_date = _date.today()

    # Cache regional comp rows per anchor (lots of candidates share an
    # anchor — only fetch once per anchor).
    anchor_comps_cache: dict[str, list[dict]] = {}
    # Bulk load anchor → comp_vins map for all anchors in one shot, then
    # bulk-load all comp options in one query. Saves ~2 minutes vs the
    # naive per-anchor option load on a 2,800-candidate run.
    flagged_anchor_vins = list({
        (c['anchor'].get('vin') or '').upper()
        for c in all_candidates if c.get('flagged')
    })
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT anchor_vin, comp_vin AS vin, year, make, model, trim,
                   body_style, drivetrain, transmission,
                   exterior_color AS color, interior, mileage,
                   price, effective_price, days_on_lot,
                   is_certified, pending_sale,
                   dealer_name AS dealer, dealer_city, dealer_state,
                   dealer_postal, region, detail_uri
              FROM porsche_arb_regional_comps
             WHERE anchor_vin = ANY(%s) AND snapshot_date = %s
        """, ([v for v in flagged_anchor_vins if v], snapshot_date))
        for r in cur.fetchall():
            anchor_comps_cache.setdefault(r['anchor_vin'], []).append(dict(r))
    log.info('phase 8: loaded %d total regional comps across %d anchors',
             sum(len(v) for v in anchor_comps_cache.values()),
             len(anchor_comps_cache))

    # Bulk-load comp option sets for every distinct comp VIN
    all_comp_vins_global = list({
        (r.get('vin') or '').upper()
        for rows in anchor_comps_cache.values() for r in rows
        if r.get('vin')
    })
    comp_options_by_vin = _load_options_full_merged(all_comp_vins_global)
    log.info('phase 8: loaded option sets for %d unique comp VINs',
             len(comp_options_by_vin))

    rescored = 0
    thin_pool = 0
    for cand in all_candidates:
        if not cand.get('flagged'):
            continue
        comp_row = cand.get('comp_row') or {}
        cand_vin = (comp_row.get('vin') or '').upper()
        anchor_vin = (cand['anchor'].get('vin') or '').upper()

        # Anchor + candidate merged option sets
        anchor_options = anchor_options_by_vin.get(anchor_vin, {})
        cand_options = cand_options_by_vin.get(cand_vin, {})

        # Always compute jaccard vs anchor (high-impact gate metric for v3)
        cand['option_jaccard_to_anchor_v3'] = _v3_jaccard_high_impact(
            cand_options, anchor_options)

        anchor_comps = anchor_comps_cache.get(anchor_vin) or []
        if not anchor_comps:
            continue

        # Bucket by region for the best-other-median calc
        by_region: dict[str, list[float]] = {}
        for r in anchor_comps:
            region = r.get('region')
            if not region:
                continue
            if region == cand.get('home_region'):
                continue
            price = r.get('effective_price') or r.get('price')
            if not price:
                continue
            comp_vin = (r.get('vin') or '').upper()
            comp_opts = comp_options_by_vin.get(comp_vin, {})
            jacc = _v3_jaccard_high_impact(cand_options, comp_opts)
            if jacc < V3_HIGH_IMPACT_JACC:
                continue
            by_region.setdefault(region, []).append(float(price))

        best_region = None
        best_median = None
        best_count = 0
        for region, prices in by_region.items():
            if len(prices) < LIKE_MIN_POOL:
                continue
            med = _percentiles(prices)[1]
            if best_median is None or med > best_median:
                best_region = region
                best_median = med
                best_count = len(prices)

        if best_region is None:
            thin_pool += 1
            continue

        cand['v3_like_filtered_other_region'] = best_region
        cand['v3_like_filtered_other_median'] = best_median
        cand['v3_like_filtered_other_count'] = best_count

        asking = (comp_row.get('effective_price') or comp_row.get('price'))
        if asking:
            transport = cand.get('transport_estimate') or 1500.0
            v3_net = float(best_median) - float(asking) - transport
            cand['v3_like_filtered_net_spread'] = v3_net
            if asking > 0:
                cand['v3_like_filtered_spread_pct'] = round(
                    v3_net / float(asking) * 100.0, 3)

        cand['arb_score_v3'] = compute_score_v3(cand)
        if cand['arb_score_v3'] is not None:
            rescored += 1
    log.info('phase 8: done in %.1fs — rescored_v3=%d thin_pool=%d',
             time.monotonic() - t0, rescored, thin_pool)
    return rescored


# Main

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--limit', type=int, default=None,
                   help='cap anchor VINs processed')
    p.add_argument('--dry-run', action='store_true',
                   help='skip DB writes to porsche_arb_candidates + regional_history')
    p.add_argument('--skip-carfax', action='store_true')
    p.add_argument('--min-spread', type=float, default=10000.0,
                   help='min net spread $ to flag (default 10000)')
    p.add_argument('--min-region-n', type=int, default=5,
                   help='min comp count in best_other region (default 5)')
    p.add_argument('--rbook-concurrency', type=int, default=4)
    p.add_argument('--carfax-concurrency', type=int, default=2)
    p.add_argument('--carfax-cap', type=int, default=100,
                   help='hard cap on Carfax pulls per run')
    p.add_argument('--options-cap', type=int, default=200,
                   help='hard cap on KBB option pulls per run (default 200)')
    p.add_argument('--options-concurrency', type=int, default=4)
    p.add_argument('--skip-options', action='store_true',
                   help='skip Phase 3.5 option enrichment + Phase 6.5 like-filtered re-scoring')
    p.add_argument('--rescore-only', action='store_true',
                   help='re-run Phase 3.5 + 6.5 against today\'s existing '
                        'candidates without doing a fresh rBook sweep')
    # PORSCHE_ARB_PHASE2_2026_05_27
    p.add_argument('--skip-detail-scrape', action='store_true',
                   help='skip Phase 6.6 per-VIN detail-page scrape')
    p.add_argument('--detail-scrape-cap', type=int, default=200,
                   help='hard cap on detail-page scrapes per run (default 200)')
    p.add_argument('--detail-scrape-concurrency', type=int, default=4)
    args = p.parse_args()

    anchors = load_anchors(args.limit)
    log.info('loaded %d porsche anchor VINs', len(anchors))
    if not anchors:
        log.warning('no anchors -- exit clean')
        return 0

    run_id = open_run() if not args.dry_run else None
    if run_id:
        log.info('run_id=%d', run_id)

    snapshot_date = date.today()

    jar = CookieJar(SESSION_PATH); jar.load()
    cookies = jar.get_cookies(); headers = jar.get_headers()
    log.info('cookies captured_at=%s age=%dmin',
             jar.captured_at(), int(jar.age_seconds() / 60))

    # Phase 2: rBook sweep
    log.info('phase 2: rBook sweep, concurrency=%d', args.rbook_concurrency)
    t0 = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.rbook_concurrency) as pool:
        futs = {pool.submit(rbook_for_anchor, a, cookies, headers): a
                for a in anchors}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
    log.info('phase 2 done in %.1fs', time.monotonic() - t0)

    if _auth_failed.is_set():
        log.error('auth failed during phase 2 -- preserving cookies, exit 2')
        if run_id:
            finish_run(run_id, status='auth_failed',
                       attempted=len(anchors), succeeded=0,
                       total_comps=0, flagged=0, carfax_pulls=0,
                       error_log='auth failed during rBook sweep')
        return 2

    succeeded = sum(1 for r in results if not r.get('err'))
    total_comps = sum(len(r.get('rows') or []) for r in results)
    log.info('rBook: anchors_ok=%d/%d total_comps=%d',
             succeeded, len(anchors), total_comps)
    err_buckets = {}
    for r in results:
        if r.get('err'):
            tag = r['err'].split(':', 1)[0]
            err_buckets[tag] = err_buckets.get(tag, 0) + 1
    if err_buckets:
        log.warning('rBook err buckets: %s', err_buckets)

    # Phase 3: regional aggregation + candidate enumeration
    # PORSCHE_ARB_PHASE2_2026_05_27: also persist every comp row to
    # porsche_arb_regional_comps so the dashboard can click through.
    all_candidates = []
    regional_rows_written = 0
    regional_comps_written = 0
    for r in results:
        if r.get('err') or not r.get('rows'):
            continue
        anchor = r['anchor']
        stats = compute_regional_stats(r['rows'])
        if not args.dry_run:
            regional_rows_written += upsert_regional_history(
                snapshot_date, anchor, stats)
            regional_comps_written += upsert_regional_comps(
                snapshot_date, anchor, r['rows'])
        cands = build_candidates(anchor, r['rows'], stats,
                                 args.min_spread, args.min_region_n)
        all_candidates.extend(cands)
    log.info('regional_rows=%d regional_comps=%d candidates_total=%d',
             regional_rows_written, regional_comps_written, len(all_candidates))

    # Phase 3.5: option enrichment for anchor + flagged candidate VINs
    # (PORSCHE_ARB_OPTIONS_2026_05_26). See porsche_options.py.
    options_anchor_count = 0
    options_cand_count = 0
    appraisal_id = jar.get_session_appraisal_id() or 'unused'
    if not args.skip_options:
        try:
            options_anchor_count, options_cand_count = enrich_options_phase(
                anchors, results, all_candidates,
                cookies, headers, appraisal_id,
                flagged_cap=args.options_cap,
                concurrency=args.options_concurrency,
                skip_kbb=False,
            )
            log.info('phase 3.5 done: anchors_enriched=%d candidates_enriched=%d',
                     options_anchor_count, options_cand_count)
        except RBVAutoAuthError:
            log.warning('phase 3.5 auth failed -- continuing without option enrichment')
        if _auth_failed.is_set():
            log.warning('auth failed during phase 3.5 -- continuing with partial data')

    # Phase 4: Carfax pull on FLAGGED only (top net_spread first), capped
    flagged_sorted = sorted(
        [c for c in all_candidates if c.get('flagged')],
        key=lambda c: (c.get('net_spread') or 0), reverse=True,
    )
    carfax_pulls = 0
    if not args.skip_carfax and flagged_sorted:
        seen_vin = set()
        unique_flagged = []
        for c in flagged_sorted:
            vin = (c['comp_row'].get('vin') or '').upper()
            if not vin or vin in seen_vin:
                continue
            seen_vin.add(vin)
            unique_flagged.append(c)
        unique_flagged = unique_flagged[:args.carfax_cap]
        log.info('phase 4: Carfax pull on %d unique flagged candidates '
                 '(cap=%d), concurrency=%d',
                 len(unique_flagged), args.carfax_cap, args.carfax_concurrency)
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.carfax_concurrency) as pool:
            futs = [pool.submit(carfax_one, c, cookies, headers)
                    for c in unique_flagged]
            for fut in as_completed(futs):
                fut.result()
        cf_by_vin = {}
        cp_by_vin = {}
        for c in unique_flagged:
            v = (c['comp_row'].get('vin') or '').upper()
            if c.get('carfax'):
                cf_by_vin[v] = c['carfax']
                cp_by_vin[v] = c.get('carfax_penalty') or 0
                carfax_pulls += 1
        for c in all_candidates:
            v = (c['comp_row'].get('vin') or '').upper()
            if v in cf_by_vin and not c.get('carfax'):
                c['carfax'] = cf_by_vin[v]
                c['carfax_penalty'] = cp_by_vin.get(v, 0)
        log.info('phase 4 done in %.1fs: carfax_pulls=%d',
                 time.monotonic() - t0, carfax_pulls)
        if _auth_failed.is_set():
            log.warning('auth failed during Carfax -- continuing with partial data')

    # Phase 5: LSL anchor lookup on flagged (year+model from anchor)
    lsl_cache = {}
    lsl_hits = 0
    for c in all_candidates:
        if not c.get('flagged'):
            continue
        key = (c['anchor'].get('year'), c['anchor'].get('model'))
        if key not in lsl_cache:
            lsl_cache[key] = _lsl_lookup(*key)
        lsl = lsl_cache[key]
        if lsl:
            c['lsl_anchor'] = lsl
            lsl_hits += 1
    log.info('LSL anchors enriched: %d candidate rows', lsl_hits)

    # Phase 6: score everyone
    for c in all_candidates:
        c['arb_score'] = compute_score(c)

    # Phase 6.5: like-filtered re-score (PORSCHE_ARB_OPTIONS_2026_05_26)
    rescored_v2 = 0
    if not args.skip_options:
        # Load anchor options from DB (Phase 3.5 wrote them) and from
        # comp-row signal extraction we already did. We index by anchor VIN.
        anchor_vins_all = list({(a or {}).get('vin') for a in [r.get('anchor') for r in results] if a})
        anchor_options_db = _load_options_from_db([v for v in anchor_vins_all if v])
        # Augment with comp-row signals — we usually have the anchor VIN
        # present in its own comp set (parsed_competitive_set drops the
        # subject's own VIN but we still extracted signals during build).
        rescored_v2 = rescore_candidates(all_candidates, results, anchor_options_db)
        log.info('phase 6.5 done: %d candidates got arb_score_v2', rescored_v2)

    # Phase 6.6: per-VIN detail-page scrape + Haiku classifier
    # (PORSCHE_ARB_PHASE2_2026_05_27). The classifier output is mirrored
    # into porsche_arb_options(source='detail_scrape') so phase 8 below
    # can fold it into the union for v3 scoring. Hard cap 200 scrapes.
    detail_stats = {'total': 0, 'classified': 0, 'sold_out': 0,
                    'blocked': 0, 'errors': 0, 'avg_conf': 0.0,
                    'tier_buckets': {}}
    if (not args.skip_options and not args.skip_detail_scrape
            and not args.dry_run):
        try:
            from porsche_detail_scraper import (
                select_vins_to_scrape as _scrape_select,
                run_bulk as _scrape_run,
            )
            vins_to_scrape = _scrape_select(
                snapshot_date=str(snapshot_date),
                limit=args.detail_scrape_cap,
            )
            if vins_to_scrape:
                log.info('phase 6.6: scraping %d detail URLs '
                         '(cap=%d, concurrency=%d)',
                         len(vins_to_scrape), args.detail_scrape_cap,
                         args.detail_scrape_concurrency)
                t0 = time.monotonic()
                detail_stats = _scrape_run(
                    vins_to_scrape,
                    concurrency=args.detail_scrape_concurrency,
                )
                log.info('phase 6.6 done in %.1fs: classified=%d sold=%d '
                         'blocked=%d errors=%d avg_conf=%.2f tiers=%s',
                         time.monotonic() - t0,
                         detail_stats.get('classified', 0),
                         detail_stats.get('sold_out', 0),
                         detail_stats.get('blocked', 0),
                         detail_stats.get('errors', 0),
                         detail_stats.get('avg_conf', 0.0),
                         detail_stats.get('tier_buckets', {}))
            else:
                log.info('phase 6.6: nothing to scrape (all flagged VINs '
                         'have recent detail_scrape rows)')
        except Exception as e:
            log.warning('phase 6.6 error (continuing): %s', e)

    # Phase 8: v3 re-score using union(KBB + comp_row + detail_scrape)
    # PORSCHE_ARB_PHASE2_2026_05_27. Same scoring formula as v2 but the
    # like-pool Jaccard now sees the high-impact options recovered by the
    # detail-page classifier.
    rescored_v3 = 0
    if not args.skip_options:
        anchor_vins_all = list({(a or {}).get('vin')
                                 for a in [r.get('anchor') for r in results]
                                 if a})
        anchor_options_v3 = _load_options_from_db(
            [v for v in anchor_vins_all if v])
        rescored_v3 = rescore_candidates_v3(
            all_candidates, results, anchor_options_v3)
        log.info('phase 8 done: %d candidates got arb_score_v3', rescored_v3)

    flagged_count = sum(1 for c in all_candidates if c.get('flagged'))
    unique_vin_count = len({(c['comp_row'].get('vin') or '').upper()
                            for c in all_candidates
                            if c['comp_row'].get('vin')})

    if args.dry_run:
        log.info('dry-run: skipping writes')
        topn = sorted([c for c in all_candidates if c.get('flagged')],
                      key=lambda c: (c.get('arb_score_v2') or c.get('arb_score') or 0),
                      reverse=True)[:3]
        for i, c in enumerate(topn, 1):
            cr = c['comp_row']
            log.info('  #%d v1=%s v2=%s jacc=%s %s %s %s ask=$%s '
                     'best_other=%s@$%s net=$%s like_other=%s@$%s like_net=$%s',
                     i, c.get('arb_score'), c.get('arb_score_v2'),
                     c.get('option_jaccard_to_anchor'),
                     cr.get('year'), cr.get('model'), cr.get('trim'),
                     cr.get('effective_price') or cr.get('price'),
                     c.get('best_other_region'), c.get('best_other_median'),
                     int(c.get('net_spread') or 0),
                     c.get('like_filtered_other_region'),
                     c.get('like_filtered_other_median'),
                     int(c.get('like_filtered_net_spread') or 0))
        log.info('SUMMARY: anchors=%d total_comps=%d regional_comps=%d '
                 'flagged=%d would_write=%d carfax=%d lsl=%d '
                 'options_a=%d options_c=%d v2_scored=%d '
                 'detail_classified=%d v3_scored=%d',
                 len(anchors), total_comps, regional_comps_written,
                 flagged_count, unique_vin_count, carfax_pulls, lsl_hits,
                 options_anchor_count, options_cand_count, rescored_v2,
                 detail_stats.get('classified', 0), rescored_v3)
        return 0

    upserted = upsert_candidates(snapshot_date, all_candidates)
    log.info('upserted %d porsche_arb_candidates', upserted)

    finish_run(run_id, status='ok',
               attempted=len(anchors), succeeded=succeeded,
               total_comps=total_comps, flagged=flagged_count,
               carfax_pulls=carfax_pulls,
               note=f'regional_rows={regional_rows_written} '
                    f'regional_comps={regional_comps_written} '
                    f'rbook_err_buckets={err_buckets} '
                    f'lsl_hits={lsl_hits} '
                    f'options_a={options_anchor_count} '
                    f'options_c={options_cand_count} '
                    f'v2_scored={rescored_v2} '
                    f'detail_classified={detail_stats.get("classified", 0)} '
                    f'v3_scored={rescored_v3}')
    log.info('SUMMARY: anchors=%d ok=%d total_comps=%d regional_comps=%d '
             'candidates=%d flagged=%d carfax=%d lsl=%d '
             'options_a=%d options_c=%d v2_scored=%d '
             'detail_classified=%d v3_scored=%d upserted=%d',
             len(anchors), succeeded, total_comps, regional_comps_written,
             len(all_candidates), flagged_count, carfax_pulls, lsl_hits,
             options_anchor_count, options_cand_count, rescored_v2,
             detail_stats.get('classified', 0), rescored_v3, upserted)
    return 0


if __name__ == '__main__':
    sys.exit(main())
