"""bias_correction.py — segment-level AI bias correction.

Reads ai_accuracy reconciled bids, aggregates per (make, model, year_band,
mileage_band), computes signed bias per segment, applies graduated correction
to Gemini's target_buy at assessment time.

Core entry points:
  - refresh_segments(window_days=30) — aggregate ai_accuracy into bias_segments
  - apply_correction(bid, gemini_target) — returns dict for _run_assessment
  - get_active_config() — current ai_correction_config row
  - preview_impact(window_days=30) — what the active config would have done

Year bands (2-yr buckets): '2018-2019', '2020-2021', '2022-2023', '2024-2025'
Mileage bands: 0-30k, 30-50k, 50-80k, 80-110k, 110-150k, 150k+

Graduated correction tiers (default config, tunable via ai_correction_config):
  strong  : n>=15, |bias|>=4%, stddev<8%   -> 80% strength
  medium  : n>=8,  |bias|>=4%              -> 50%
  light   : n>=4,  |bias|>=6%              -> 25%
  else    : 0% (Gemini raw)

Lookup falls back from most-specific to broader if no qualifying cell:
  (make, model, year_band, mileage_band)
  -> (make, model, year_band, 'any')
  -> (make, model, 'any', 'any')
"""
from __future__ import annotations
import json
import os
from typing import Any

import psycopg2
import psycopg2.extras


EW_DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')


# Banding helpers --------------------------------------------------------

def year_band(year):
    if year is None:
        return 'any'
    try:
        y = int(year)
    except (TypeError, ValueError):
        return 'any'
    lo = (y // 2) * 2
    return f'{lo}-{lo+1}'


def mileage_band(miles):
    if miles is None:
        return 'any'
    try:
        m = int(miles)
    except (TypeError, ValueError):
        return 'any'
    if m < 30_000:   return '0-30k'
    if m < 50_000:   return '30-50k'
    if m < 80_000:   return '50-80k'
    if m < 110_000:  return '80-110k'
    if m < 150_000:  return '110-150k'
    return '150k+'


def _norm_make(s):
    return (s or '').upper().strip()


def _norm_model(s):
    return (s or '').upper().strip()


# Config -----------------------------------------------------------------

DEFAULT_CONFIG = {
    'enabled': False,
    'window_days': 30,
    'tiers': [
        {'name': 'strong', 'min_n': 15, 'min_abs_bias': 4.0,
         'max_stddev': 8.0, 'strength': 0.80},
        {'name': 'medium', 'min_n': 8,  'min_abs_bias': 4.0,
         'max_stddev': None, 'strength': 0.50},
        {'name': 'light',  'min_n': 4,  'min_abs_bias': 6.0,
         'max_stddev': None, 'strength': 0.25},
    ],
    'segment_overrides': [],
}


def get_active_config(conn=None):
    own = conn is None
    if own:
        conn = psycopg2.connect(EW_DB_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, active, config FROM ai_correction_config "
                    "WHERE active = TRUE ORDER BY saved_at DESC LIMIT 1")
        r = cur.fetchone()
        cur.close()
    finally:
        if own:
            conn.close()
    if not r:
        return {'id': None, 'active': False, 'config': dict(DEFAULT_CONFIG)}
    cfg = r[2] if isinstance(r[2], dict) else json.loads(r[2])
    out = dict(DEFAULT_CONFIG)
    out.update(cfg)
    return {'id': r[0], 'active': r[1], 'config': out}


# Refresh ----------------------------------------------------------------

def refresh_segments(window_days=30, conn=None):
    """Aggregate ai_accuracy into bias_segments at three granularity levels."""
    own = conn is None
    if own:
        conn = psycopg2.connect(EW_DB_URL)
    try:
        cur = conn.cursor()
        # Dedup by VIN — multiple assessments of the same car are not
        # independent data points. Keep the LAST assessment per VIN
        # (closest to the actual purchase decision).
        # Also: stale-actual filter — both dates must be in window.
        # Combined signal: prefer acquired-purchase delta when available,
        # fall back to operator-estimate delta. Either qualifies a row.
        # Time window: actual_purchased_at OR client_estimate_at within window.
        cur.execute("""
            SELECT make, model, year, mileage,
                   COALESCE(delta_pct, estimate_delta_pct) AS delta_pct,
                   abs_delta_pct,
                   CASE WHEN delta_pct IS NOT NULL THEN 'actual' ELSE 'estimate' END AS source
              FROM (
                SELECT DISTINCT ON (vin)
                       make, model, year, mileage,
                       delta_pct, abs_delta_pct, estimate_delta_pct, vin
                  FROM ai_accuracy
                 WHERE (delta_pct IS NOT NULL OR estimate_delta_pct IS NOT NULL)
                   AND bid_id > 0
                   AND vin IS NOT NULL AND vin <> ''
                   AND reconciled_at > NOW() - (%s || ' days')::interval
                   AND COALESCE(actual_purchased_at, client_estimate_at)
                       > NOW() - (%s || ' days')::interval
                 ORDER BY vin, ai_assessed_at DESC NULLS LAST
              ) AS deduped
        """, (window_days, window_days))
        rows = cur.fetchall()
        n_source = len(rows)

        from collections import defaultdict
        from statistics import mean, median, stdev

        agg = defaultdict(list)
        n_actual_total = n_estimate_total = 0
        for make, model_, year_, miles, dp, ap, source in rows:
            mk = _norm_make(make)
            md = _norm_model(model_)
            if not mk or not md:
                continue
            if source == 'actual':
                n_actual_total += 1
            else:
                n_estimate_total += 1
            yb = year_band(year_)
            mb = mileage_band(miles)
            # Each tuple now carries (delta_pct, abs_pct, source) so we
            # can split per-source in the segment loop below
            agg[(mk, md, yb, mb)].append((float(dp), float(ap or 0), source))
            agg[(mk, md, yb, 'any')].append((float(dp), float(ap or 0), source))
            agg[(mk, md, 'any', 'any')].append((float(dp), float(ap or 0), source))
        print(f'[bias_correction] window={window_days}d signal mix: actual={n_actual_total} estimate={n_estimate_total}', flush=True)

        cur.execute("DELETE FROM bias_segments WHERE window_days = %s",
                    (window_days,))
        n_written = 0
        for (mk, md, yb, mb), tuples in agg.items():
            n = len(tuples)
            if n < 2:
                continue
            deltas = [t[0] for t in tuples]
            abss = [t[1] for t in tuples if t[1] > 0]
            bias = round(mean(deltas), 2)
            sd = round(stdev(deltas), 2) if n >= 2 else None
            ap_med = round(median(abss), 2) if abss else None
            # Per-source split for the admin panel
            actuals = [t[0] for t in tuples if t[2] == 'actual']
            estimates = [t[0] for t in tuples if t[2] == 'estimate']
            n_a = len(actuals)
            n_e = len(estimates)
            bias_a = round(mean(actuals), 2) if actuals else None
            bias_e = round(mean(estimates), 2) if estimates else None
            cur.execute("""
                INSERT INTO bias_segments
                  (make, model, year_band, mileage_band, n, bias_pct,
                   abs_pct, stddev_pct, window_days,
                   n_actual, n_estimate, bias_pct_actual, bias_pct_estimate)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (mk, md, yb, mb, n, bias, ap_med, sd, window_days,
                  n_a, n_e, bias_a, bias_e))
            n_written += 1

        conn.commit()
        cur.close()
        return {'ok': True, 'n_segments_written': n_written,
                'window_days': window_days, 'n_source_rows': n_source}
    finally:
        if own:
            conn.close()


# Lookup -----------------------------------------------------------------

def _segment_key(mk, md, yb, mb):
    return f'{mk}|{md}|{yb}|{mb}'


def _lookup_all_levels(make, model, year, mileage, window_days=30, conn=None):
    """Returns list of segment dicts at all 3 fallback levels (most-specific
    first), each with `lookup_level` + `segment_key` keys. Caller picks which
    one qualifies for a tier."""
    mk = _norm_make(make)
    md = _norm_model(model)
    if not mk or not md:
        return []
    yb = year_band(year)
    mb = mileage_band(mileage)

    own = conn is None
    if own:
        conn = psycopg2.connect(EW_DB_URL)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        results = []
        for try_yb, try_mb, level in [
            (yb,    mb,    'full'),
            (yb,    'any', 'year_only'),
            ('any', 'any', 'make_model_only'),
        ]:
            cur.execute("""
                SELECT make, model, year_band, mileage_band, n, bias_pct,
                       abs_pct, stddev_pct
                  FROM bias_segments
                 WHERE make = %s AND model = %s
                   AND year_band = %s AND mileage_band = %s
                   AND window_days = %s
            """, (mk, md, try_yb, try_mb, window_days))
            r = cur.fetchone()
            if r:
                row = dict(r)
                row['lookup_level'] = level
                row['segment_key'] = _segment_key(
                    row['make'], row['model'], row['year_band'],
                    row['mileage_band'])
                results.append(row)
        cur.close()
        return results
    finally:
        if own:
            conn.close()


def lookup_bias(make, model, year, mileage, window_days=30, conn=None):
    """Returns the most-specific segment with data. Backward-compat wrapper —
    apply_correction now uses _lookup_all_levels directly so it can fall back
    when the narrowest level doesn't qualify for a tier."""
    levels = _lookup_all_levels(make, model, year, mileage, window_days, conn)
    return levels[0] if levels else None


# Tier matching + correction --------------------------------------------

def _match_tier(seg, tiers):
    n = int(seg.get('n') or 0)
    bias = abs(float(seg.get('bias_pct') or 0))
    sd = seg.get('stddev_pct')
    sd_val = float(sd) if sd is not None else None

    for tier in tiers:
        if n < int(tier.get('min_n', 0)):
            continue
        if bias < float(tier.get('min_abs_bias', 0)):
            continue
        max_sd = tier.get('max_stddev')
        if max_sd is not None and (sd_val is None or sd_val > float(max_sd)):
            continue
        return tier
    return None


def _normalize_overrides(overrides):
    """Accept either old shape (list of segment_key strings) or new shape
    (list of dicts {key, type, adjustment_pct?, set_at?, set_by?}).
    Returns normalized list of dicts."""
    if not overrides:
        return []
    out = []
    for o in overrides:
        if isinstance(o, str):
            # Legacy: bare segment_key string = force_zero
            mk_md = '|'.join(o.split('|')[:2]) if '|' in o else o
            out.append({'key': mk_md, 'type': 'force_zero'})
        elif isinstance(o, dict) and o.get('key'):
            out.append(o)
    return out


def _make_model_key(make, model):
    return f'{_norm_make(make)}|{_norm_model(model)}'


def _get_override(make, model, overrides):
    """Returns the matching override dict for this make+model, or None."""
    norm = _normalize_overrides(overrides)
    if not norm:
        return None
    target = _make_model_key(make, model)
    for o in norm:
        if (o.get('key') or '').upper() == target:
            return o
    return None


def _is_overridden(segment_key, overrides):
    """Backward-compat wrapper: any kind of override counts."""
    norm = _normalize_overrides(overrides)
    target_mk = '|'.join(segment_key.split('|')[:2])
    return any((o.get('key') or '').upper() == target_mk.upper() for o in norm)


def apply_correction(bid, gemini_target, conn=None):
    out = {
        'gemini_raw': int(gemini_target),
        'corrected_target': int(gemini_target),
        'bias_pct': None,
        'strength_applied': 0.0,
        'tier': 'none',
        'segment_key': None,
        'segment_n': None,
        'config_version': None,
        'reason': '',
    }

    own = conn is None
    if own:
        conn = psycopg2.connect(EW_DB_URL)
    try:
        cfg_row = get_active_config(conn=conn)
        out['config_version'] = cfg_row.get('id')
        cfg = cfg_row['config']

        if not cfg.get('enabled'):
            out['reason'] = 'correction disabled in active config'
            return out

        wd = int(cfg.get('window_days', 30))
        overrides = cfg.get('segment_overrides') or []
        tiers = cfg.get('tiers') or []

        # CHECK MANUAL OVERRIDE FIRST — operator's per-make/model setting
        # wins over data-driven tier matching.
        ov = _get_override(bid.get('make'), bid.get('model'), overrides)
        if ov:
            mk_md = _make_model_key(bid.get('make'), bid.get('model'))
            out['segment_key'] = mk_md
            if ov.get('type') == 'force_zero':
                out['tier'] = 'override:zero'
                out['reason'] = f'manual override on {mk_md}: force NO correction'
                return out
            if ov.get('type') == 'fixed_adjustment':
                adj_pct = float(ov.get('adjustment_pct') or 0)
                if adj_pct == 0:
                    out['reason'] = f'manual override on {mk_md}: adjustment 0%'
                    return out
                corrected = int(round(gemini_target * (1.0 + adj_pct / 100.0)))
                out['corrected_target'] = corrected
                out['strength_applied'] = 1.0
                out['tier'] = 'override:fixed'
                out['bias_pct'] = adj_pct  # surface the user's chosen adjustment
                direction = 'up' if adj_pct > 0 else 'down'
                out['reason'] = (f'manual override on {mk_md}: fixed adjustment '
                                 f'{direction} {abs(adj_pct):.1f}% '
                                 f'(${gemini_target:,} -> ${corrected:,})')
                return out

        # No override — try data-driven tier matching across 3 fallback levels
        levels = _lookup_all_levels(bid.get('make'), bid.get('model'),
                                    bid.get('year'), bid.get('mileage'),
                                    window_days=wd, conn=conn)
        if not levels:
            out['reason'] = 'no qualifying segment for this YMM'
            return out

        seg = None
        tier = None
        for candidate in levels:
            t = _match_tier(candidate, tiers)
            if t:
                seg = candidate
                tier = t
                break

        narrowest = levels[0]
        out['segment_key'] = (seg or narrowest)['segment_key']
        out['segment_n'] = (seg or narrowest)['n']
        out['bias_pct'] = float((seg or narrowest)['bias_pct'])

        if not seg:
            out['reason'] = (f'narrowest segment {narrowest["segment_key"]} '
                             f'n={narrowest["n"]} bias={narrowest["bias_pct"]}% — '
                             f'no tier matched at any fallback level')
            return out

        strength = float(tier['strength'])
        bias_pct = float(seg['bias_pct'])
        adjust_factor = 1.0 + (strength * bias_pct / 100.0)
        corrected = int(round(gemini_target * adjust_factor))

        out['corrected_target'] = corrected
        out['strength_applied'] = strength
        out['tier'] = tier['name']
        direction = 'up' if bias_pct > 0 else 'down'
        out['reason'] = (f'tier={tier["name"]} segment={seg["segment_key"]} '
                         f'n={seg["n"]} bias={bias_pct}% '
                         f'-> adjusted {direction} by {strength*abs(bias_pct):.2f}% '
                         f'(${gemini_target:,} -> ${corrected:,})')
        return out
    finally:
        if own:
            conn.close()


def log_correction(bid_id, result, conn=None):
    own = conn is None
    if own:
        conn = psycopg2.connect(EW_DB_URL)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ai_correction_log
              (bid_id, gemini_raw, corrected_target, bias_pct, strength_applied,
               segment_key, segment_n, tier, config_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (bid_id, result.get('gemini_raw'), result.get('corrected_target'),
              result.get('bias_pct'), result.get('strength_applied'),
              result.get('segment_key'), result.get('segment_n'),
              result.get('tier'), result.get('config_version')))
        conn.commit()
        cur.close()
    finally:
        if own:
            conn.close()


# Preview (admin UI helper) ---------------------------------------------

def preview_impact(window_days=30, conn=None):
    own = conn is None
    if own:
        conn = psycopg2.connect(EW_DB_URL)
    try:
        cfg_row = get_active_config(conn=conn)
        cfg = cfg_row['config']
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Count stale matches (excluded for being outside the window)
        cur.execute("""
            SELECT COUNT(DISTINCT vin) AS n
              FROM ai_accuracy
             WHERE delta_pct IS NOT NULL AND bid_id > 0
               AND vin IS NOT NULL AND vin <> ''
               AND reconciled_at > NOW() - (%s || ' days')::interval
               AND (actual_purchased_at IS NULL OR
                    actual_purchased_at <= NOW() - (%s || ' days')::interval)
        """, (window_days, window_days))
        n_stale = cur.fetchone()['n']
        # Dedup-by-VIN — keep latest assessment per unique VIN. One car =
        # one data point regardless of how many times we re-bid on it.
        cur.execute("""
            SELECT bid_id, make, model, year, mileage,
                   ai_recommendation, actual_purchase_cost, delta_pct
              FROM (
                SELECT DISTINCT ON (a.vin)
                       a.bid_id, a.make, a.model, a.year, a.mileage,
                       a.ai_recommendation, a.actual_purchase_cost, a.delta_pct,
                       a.vin
                  FROM ai_accuracy a
                 WHERE a.delta_pct IS NOT NULL
                   AND a.bid_id > 0
                   AND a.vin IS NOT NULL AND a.vin <> ''
                   AND a.reconciled_at > NOW() - (%s || ' days')::interval
                   AND a.actual_purchased_at > NOW() - (%s || ' days')::interval
                 ORDER BY a.vin, a.ai_assessed_at DESC NULLS LAST
              ) deduped
        """, (window_days, window_days))
        sims = cur.fetchall()
        cur.close()

        n_total = len(sims)
        n_corrected = 0
        sum_shift = 0.0
        sum_base = 0.0
        improved = 0
        worsened = 0
        unchanged = 0

        for s in sims:
            ai_rec = s['ai_recommendation']
            actual = s['actual_purchase_cost']
            if not ai_rec or not actual:
                continue
            r = apply_correction({
                'make': s['make'], 'model': s['model'],
                'year': s['year'], 'mileage': s['mileage'],
            }, ai_rec, conn=conn)
            corr = r['corrected_target']
            if corr == ai_rec:
                continue
            n_corrected += 1
            sum_shift += (corr - ai_rec)
            sum_base += ai_rec
            old_err = abs(actual - ai_rec)
            new_err = abs(actual - corr)
            if new_err < old_err - 1:
                improved += 1
            elif new_err > old_err + 1:
                worsened += 1
            else:
                unchanged += 1

        avg_shift_pct = (round(100.0 * sum_shift / sum_base, 2)
                         if sum_base else 0.0)

        return {
            'window_days': window_days,
            'config_version': cfg_row.get('id'),
            'enabled': cfg.get('enabled'),
            'n_total_bids': n_total,
            'n_stale_excluded': int(n_stale),
            'n_corrected': n_corrected,
            'avg_shift_pct': avg_shift_pct,
            'avg_shift_dollars': round(sum_shift / n_corrected) if n_corrected else 0,
            'improved': improved,
            'worsened': worsened,
            'unchanged': unchanged,
            'improvement_pct': round(100.0 * improved / n_corrected, 1) if n_corrected else 0.0,
        }
    finally:
        if own:
            conn.close()


# CLI --------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh-segments', action='store_true')
    ap.add_argument('--preview', action='store_true')
    ap.add_argument('--window', type=int, default=30)
    args = ap.parse_args()

    if args.refresh_segments:
        r = refresh_segments(window_days=args.window)
        print(json.dumps(r, indent=2, default=str))
    elif args.preview:
        r = preview_impact(window_days=args.window)
        print(json.dumps(r, indent=2, default=str))
    else:
        ap.print_help()
