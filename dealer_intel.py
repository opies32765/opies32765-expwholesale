"""dealer_intel.py — Dealer DB Graph System: Intel layer

Runs daily AFTER encore_comps_pipeline.py finishes (chained from the
23:00 EST cron). Reads three tables that the rest of the system has
already filled:
    - dealer_inventory               (current active units + first_seen_at)
    - dealer_inventory_history       (per-scan price/mileage trajectory)
    - dealer_inventory_comp_history  (per-day MMR + rBook signals)
    - dealer_inventory_comps         (latest snapshot per VIN)

Writes two tables this module owns:
    - dealer_intel_segments  (per-dealer × make × year_band × mileage_band
                              rollups: avg DOL, sold volume, market drift)
    - dealer_intel_snapshot  (per-VIN action chip: keep / hold / price_drop
                              / sell_now + transparent reasoning text)

Surfaced on partner portal as a Dealer Intel banner (segments) plus a
per-row action chip (snapshot). Both gated by COMPS_ENABLED_SLUGS so
non-Encore dealers see nothing yet.

CLI:
    python3 dealer_intel.py [--dealer-slug encore]
                            [--window-days 16]
                            [--dry-run]
                            [--limit N]

Exit codes:
    0  - success
    1  - any DB/runtime error
    2  - dealer slug not found
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, '/opt/expwholesale')

import psycopg2
import psycopg2.extras

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')

log = logging.getLogger('dealer_intel')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)


# ── Segment classification helpers ──────────────────────────────────────

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


# ── Data load ───────────────────────────────────────────────────────────

def load_dealer(cur, slug):
    cur.execute("""SELECT id, name, portal_slug FROM dealers
                    WHERE portal_slug = %s LIMIT 1""", (slug,))
    return cur.fetchone()


def load_active_inventory(cur, dealer_id, limit=None):
    """Returns active units with a TRUE current DOL.

    DOL formula matches dealer_db.best_age_days_filter (which the portal
    cards already use):
        true_dol = verified_days_on_lot + (today - verified_at::date)
    The verifier records DOL as of when it ran (typically once at
    onboarding); the elapsed-since-verification days must be added back
    in. Falls back to scan-window (today - first_seen_at) only when no
    verification ever happened.
    """
    sql = """
        SELECT id, vin, year, make, model, trim, mileage, price,
               first_seen_at, last_seen_at, last_price_change_at,
               price_drop_amount, ext_color, int_color,
               verified_days_on_lot, verified_at,
               CASE
                 WHEN verified_days_on_lot IS NOT NULL AND verified_at IS NOT NULL THEN
                   verified_days_on_lot
                   + GREATEST(0, (CURRENT_DATE - verified_at::date)::int)
                 ELSE
                   GREATEST(0, (CURRENT_DATE - first_seen_at::date)::int)
               END AS dol_today
          FROM dealer_inventory
         WHERE dealer_id = %s AND status = 'active' AND vin IS NOT NULL AND vin <> ''
         ORDER BY first_seen_at ASC
    """
    if limit:
        sql += f' LIMIT {int(limit)}'
    cur.execute(sql, (dealer_id,))
    return [dict(r) for r in cur.fetchall()]


def load_sold_inventory(cur, dealer_id, window_days):
    """Cars sold within the window — used for segment DOL stats.

    DOL formula for sold cars: verified_days_on_lot + (sold_at - verified_at).
    This gives the TRUE days-on-lot at the moment of sale (the verifier
    captured the count earlier in the unit's life; the additional days
    until sale must be added back). Falls back to (sold_at - first_seen_at)
    only when the verifier never visited (rare).
    """
    cur.execute("""
        SELECT id, vin, year, make, model, mileage,
               first_seen_at, sold_at, price,
               COALESCE(
                   verified_days_on_lot
                   + GREATEST(0, (sold_at::date - verified_at::date)::int),
                   GREATEST(0, EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400.0)::int
               ) AS days_on_lot
          FROM dealer_inventory
         WHERE dealer_id = %s
           AND status = 'sold'
           AND sold_at IS NOT NULL
           AND sold_at > NOW() - (INTERVAL '1 day' * %s)
    """, (dealer_id, window_days))
    return [dict(r) for r in cur.fetchall()]


def load_comp_snapshot(cur, dealer_id):
    """Latest comp signals per inv_id, keyed by inventory_id.
    Reads dealer_inventory_comp_history (source='daily_run') because that
    table carries rBook P75, P25 + MMR wholesale; the latest-snapshot
    table dealer_inventory_comps only has P25/P50/mmr_comp_value (no P75)."""
    cur.execute("""
        SELECT DISTINCT ON (h.dealer_inventory_id)
               h.dealer_inventory_id,
               h.mmr_wholesale_avg,
               h.mmr_retail_avg,
               h.rbook_p25,
               h.rbook_p50,
               h.rbook_p75,
               h.rbook_avg_dol,
               h.rbook_comp_count,
               h.snapshot_date
          FROM dealer_inventory_comp_history h
          JOIN dealer_inventory di ON di.id = h.dealer_inventory_id
         WHERE di.dealer_id = %s
           AND h.source = 'daily_run'
         ORDER BY h.dealer_inventory_id, h.snapshot_date DESC
    """, (dealer_id,))
    return {r['dealer_inventory_id']: dict(r) for r in cur.fetchall()}


def load_mmr_trends(cur, dealer_id, ref_date):
    """For each inv_id, compute MMR 7d and 30d % change from history.
    Uses 'daily_run' OR 'manheim_tx_backfill' (whichever's closest to
    the target ref_date - N days)."""
    # IMPORTANT: only use source='daily_run' here. The 'manheim_tx_backfill'
    # rows are individual auction sale prices (high variance) — comparing
    # them apples-to-oranges against the daily aggregate produces wild
    # ±50% swings that are NOT meaningful trend signal. Until we have
    # 7-30 days of daily_run accumulation, the 7d/30d trends will be NULL
    # for most units — chip engine falls back to HOLD which is correct.
    cur.execute("""
        WITH hist AS (
            SELECT dealer_inventory_id, snapshot_date, mmr_wholesale_avg
              FROM dealer_inventory_comp_history h
              JOIN dealer_inventory di ON di.id = h.dealer_inventory_id
             WHERE di.dealer_id = %s
               AND di.status = 'active'
               AND h.mmr_wholesale_avg IS NOT NULL
               AND h.source = 'daily_run'
        ),
        latest AS (
            SELECT DISTINCT ON (dealer_inventory_id)
                   dealer_inventory_id, snapshot_date, mmr_wholesale_avg
              FROM hist
             ORDER BY dealer_inventory_id, snapshot_date DESC
        ),
        d7 AS (
            SELECT DISTINCT ON (dealer_inventory_id)
                   dealer_inventory_id, snapshot_date, mmr_wholesale_avg
              FROM hist
             WHERE snapshot_date <= %s::date - INTERVAL '7 days'
             ORDER BY dealer_inventory_id, snapshot_date DESC
        ),
        d30 AS (
            SELECT DISTINCT ON (dealer_inventory_id)
                   dealer_inventory_id, snapshot_date, mmr_wholesale_avg
              FROM hist
             WHERE snapshot_date <= %s::date - INTERVAL '30 days'
             ORDER BY dealer_inventory_id, snapshot_date DESC
        )
        SELECT latest.dealer_inventory_id,
               latest.mmr_wholesale_avg AS mmr_now,
               d7.mmr_wholesale_avg     AS mmr_7d_ago,
               d30.mmr_wholesale_avg    AS mmr_30d_ago
          FROM latest
          LEFT JOIN d7  ON d7.dealer_inventory_id = latest.dealer_inventory_id
          LEFT JOIN d30 ON d30.dealer_inventory_id = latest.dealer_inventory_id
    """, (dealer_id, ref_date, ref_date))
    out = {}
    for r in cur.fetchall():
        inv_id = r['dealer_inventory_id']
        mmr_now = r['mmr_now']
        d7 = r['mmr_7d_ago']
        d30 = r['mmr_30d_ago']
        def _pct(a, b):
            if a is None or b is None or float(b) <= 0:
                return None
            return round(float(a) / float(b) * 100.0 - 100.0, 2)
        out[inv_id] = {
            'mmr_now': float(mmr_now) if mmr_now is not None else None,
            'mmr_7d_pct': _pct(mmr_now, d7),
            'mmr_30d_pct': _pct(mmr_now, d30),
        }
    return out


# ── Segment computation ─────────────────────────────────────────────────

def compute_segments(active, sold, mmr_trends, window_days):
    """Build per-segment rollups keyed by segment_key. Returns list of
    dicts ready for INSERT."""
    segments = {}
    for v in active:
        k = segment_key(v.get('make'), v.get('year'), v.get('mileage'))
        seg = segments.setdefault(k, {
            'segment_key': k,
            'make': (v.get('make') or 'UNKNOWN').strip(),
            'year_band': year_band(v.get('year')),
            'mileage_band': mileage_band(v.get('mileage')),
            'active_count': 0,
            'sold_volume': 0,
            'dols_for_avg': [],
            'mmr_7d_samples': [],
            'mmr_30d_samples': [],
        })
        seg['active_count'] += 1
        tr = mmr_trends.get(v['id']) or {}
        if tr.get('mmr_7d_pct') is not None:
            seg['mmr_7d_samples'].append(tr['mmr_7d_pct'])
        if tr.get('mmr_30d_pct') is not None:
            seg['mmr_30d_samples'].append(tr['mmr_30d_pct'])

    for v in sold:
        k = segment_key(v.get('make'), v.get('year'), v.get('mileage'))
        seg = segments.setdefault(k, {
            'segment_key': k,
            'make': (v.get('make') or 'UNKNOWN').strip(),
            'year_band': year_band(v.get('year')),
            'mileage_band': mileage_band(v.get('mileage')),
            'active_count': 0,
            'sold_volume': 0,
            'dols_for_avg': [],
            'mmr_7d_samples': [],
            'mmr_30d_samples': [],
        })
        seg['sold_volume'] += 1
        if v.get('days_on_lot') is not None:
            seg['dols_for_avg'].append(float(v['days_on_lot']))

    out = []
    for k, seg in segments.items():
        dols = seg['dols_for_avg']
        avg_dol = round(sum(dols) / len(dols), 1) if dols else None
        median_dol = None
        if dols:
            s = sorted(dols)
            n = len(s)
            median_dol = round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0, 1)
        m7 = seg['mmr_7d_samples']
        m30 = seg['mmr_30d_samples']
        mmr_7d_pct = round(sum(m7) / len(m7), 2) if m7 else None
        mmr_30d_pct = round(sum(m30) / len(m30), 2) if m30 else None

        # Aging count: active units past 1.2× segment avg DOL
        # (computed in compute_per_vin_chips after segments, so we
        # backfill it later if needed; init to 0 here for now)
        aging_count = 0

        # Verdict + confidence
        vol = seg['sold_volume']
        if vol >= 5:
            confidence = 'high'
        elif vol >= 2:
            confidence = 'medium'
        else:
            confidence = 'low'

        # Verdict thresholds calibrated against VERIFIED true-DOL numbers
        # (typical luxury-wholesale lot: ~60-90 day avg to sell, max 600+).
        # 'strong' = clearly faster than typical · 'slow' = clearly slower
        # · 'avoid' = inventory but no sales · 'low_data' = first-time
        # segment with no track record.
        if vol == 0 and seg['active_count'] >= 2:
            verdict = 'avoid'    # have inventory, nothing's selling
        elif vol == 0:
            verdict = 'low_data'
        elif avg_dol is not None and avg_dol < 30:
            verdict = 'strong'
        elif avg_dol is not None and avg_dol < 90:
            verdict = 'normal'
        else:
            verdict = 'slow'

        # If market drift is bad, downgrade strong→normal etc.
        if mmr_7d_pct is not None and mmr_7d_pct < -3 and verdict == 'strong':
            verdict = 'normal'

        out.append({
            **seg,
            'avg_dol_days': avg_dol,
            'median_dol_days': median_dol,
            'aging_count': aging_count,
            'mmr_trend_7d_pct': mmr_7d_pct,
            'mmr_trend_30d_pct': mmr_30d_pct,
            'verdict': verdict,
            'confidence': confidence,
            'window_days': window_days,
        })
    return out


# ── Per-VIN chip computation ────────────────────────────────────────────

def _fmt_money(n):
    if n is None:
        return '—'
    try:
        return f'${int(round(float(n))):,}'
    except (TypeError, ValueError):
        return '—'


def _fmt_pct(p):
    if p is None:
        return '—'
    s = f'{p:+.1f}%'
    return s.replace('+', '+').replace('-', '−')


def compute_chip(v, comp, mmr, seg_lookup, today):
    """Returns (chip, confidence, reasoning_text, inputs_dict)."""
    inv_id = v['id']
    asking = v.get('price')
    rbook_p50 = comp.get('rbook_p50') if comp else None
    rbook_p75 = comp.get('rbook_p75') if comp else None
    mmr_now = mmr.get('mmr_now') if mmr else None
    mmr_7d = mmr.get('mmr_7d_pct') if mmr else None
    mmr_30d = mmr.get('mmr_30d_pct') if mmr else None

    # Days on lot — use dol_today which the SQL has already computed
    # using the canonical formula (verified_days_on_lot + days-since-
    # verification, falling back to scan-window). Matches the per-card
    # age that dealer_db.best_age_days_filter renders on the portal.
    dol = v.get('dol_today')
    if dol is not None:
        dol = int(dol)

    k = segment_key(v.get('make'), v.get('year'), v.get('mileage'))
    seg = seg_lookup.get(k) or {}
    seg_avg = seg.get('avg_dol_days')
    seg_vol = seg.get('sold_volume', 0) or 0
    seg_verdict = seg.get('verdict')

    inputs = {
        'days_on_lot': dol,
        'segment_key': k,
        'segment_avg_dol': float(seg_avg) if seg_avg is not None else None,
        'segment_sold_volume': seg_vol,
        'asking_price': int(asking) if asking is not None else None,
        'rbook_p50': float(rbook_p50) if rbook_p50 is not None else None,
        'rbook_p75': float(rbook_p75) if rbook_p75 is not None else None,
        'mmr_now': mmr_now,
        'mmr_trend_7d_pct': mmr_7d,
        'mmr_trend_30d_pct': mmr_30d,
    }

    # Confidence
    if seg_vol >= 5 and rbook_p50 is not None and mmr_now is not None:
        confidence = 'high'
    elif seg_vol >= 2 or (rbook_p50 is not None and mmr_now is not None):
        confidence = 'medium'
    else:
        confidence = 'low'

    # Rule evaluation. Each rule requires its inputs to be non-None;
    # missing data → falls through to HOLD with low confidence.
    over_age_15 = (dol is not None and seg_avg is not None and dol > 1.5 * seg_avg)
    over_age_12 = (dol is not None and seg_avg is not None and dol > 1.2 * seg_avg)
    under_age = (dol is not None and seg_avg is not None and dol < seg_avg)

    mmr_falling = (mmr_7d is not None and mmr_7d <= -5.0)
    # mmr_holding is treated as "no bad signal" — missing data → assumed
    # OK so we don't block the KEEP chip during the 7-day warmup window
    # where most units lack 7d comp history. Once daily_run accumulates
    # the trend gates real signal.
    mmr_holding = (mmr_7d is None or mmr_7d >= -2.0)

    priced_above_75 = (asking is not None and rbook_p75 is not None
                      and asking > float(rbook_p75))
    priced_above_50 = (asking is not None and rbook_p50 is not None
                      and asking > float(rbook_p50))
    priced_at_or_below_50 = (asking is not None and rbook_p50 is not None
                             and asking <= float(rbook_p50))

    # KEEP fires for "fresh + priced right + market not collapsing" units.
    # "not_aging" = we don't have evidence this is sitting too long
    # (missing segment data OR DOL is within 1.2× of segment avg).
    # This lets KEEP fire during the 16d scan-window warmup where many
    # units have dol=16d bounded by when scanning started.
    not_aging = (dol is None or seg_avg is None or dol <= 1.2 * seg_avg)

    chip = 'hold'
    if over_age_15 and mmr_falling and priced_above_75:
        chip = 'sell_now'
    elif over_age_12 and priced_above_50:
        chip = 'price_drop'
    elif not_aging and priced_at_or_below_50 and mmr_holding:
        chip = 'keep'

    # Reasoning text — must cite specific numbers so the dealer can verify
    bits = []
    if dol is not None and seg_avg is not None:
        bits.append(f'On lot {dol}d vs {seg["make"].title()} '
                    f'{year_band(v.get("year"))} typical {seg_avg}d')
    elif dol is not None:
        bits.append(f'On lot {dol}d (segment sample too thin yet)')
    if mmr_7d is not None:
        if mmr_7d <= -3.0:
            bits.append(f'MMR for similar units {_fmt_pct(mmr_7d)} last 7d (falling)')
        elif mmr_7d >= 3.0:
            bits.append(f'MMR {_fmt_pct(mmr_7d)} last 7d (rising)')
        else:
            bits.append(f'MMR {_fmt_pct(mmr_7d)} last 7d (steady)')
    if asking is not None and rbook_p50 is not None:
        if priced_above_75 and rbook_p75 is not None:
            bits.append(f'Asking {_fmt_money(asking)} above rBook P75 '
                        f'{_fmt_money(rbook_p75)}')
        elif priced_above_50:
            bits.append(f'Asking {_fmt_money(asking)} above rBook P50 '
                        f'{_fmt_money(rbook_p50)}')
        elif priced_at_or_below_50:
            bits.append(f'Asking {_fmt_money(asking)} at/below rBook P50 '
                        f'{_fmt_money(rbook_p50)}')

    if not bits:
        bits = ['Not enough comp data yet — holding']
    reasoning_text = '. '.join(bits) + '.'

    return chip, confidence, reasoning_text, inputs


# ── Persist ─────────────────────────────────────────────────────────────

def upsert_segments(cur, dealer_id, today, segments):
    n = 0
    for s in segments:
        cur.execute("""
            INSERT INTO dealer_intel_segments
              (dealer_id, snapshot_date, segment_key, make, year_band,
               mileage_band, window_days, sold_volume, avg_dol_days,
               median_dol_days, active_count, aging_count,
               mmr_trend_7d_pct, mmr_trend_30d_pct, verdict, confidence,
               computed_at)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (dealer_id, snapshot_date, segment_key) DO UPDATE
              SET sold_volume       = EXCLUDED.sold_volume,
                  avg_dol_days      = EXCLUDED.avg_dol_days,
                  median_dol_days   = EXCLUDED.median_dol_days,
                  active_count      = EXCLUDED.active_count,
                  aging_count       = EXCLUDED.aging_count,
                  mmr_trend_7d_pct  = EXCLUDED.mmr_trend_7d_pct,
                  mmr_trend_30d_pct = EXCLUDED.mmr_trend_30d_pct,
                  verdict           = EXCLUDED.verdict,
                  confidence        = EXCLUDED.confidence,
                  window_days       = EXCLUDED.window_days,
                  computed_at       = NOW()
        """, (
            dealer_id, today, s['segment_key'], s['make'], s['year_band'],
            s['mileage_band'], s['window_days'], s['sold_volume'],
            s['avg_dol_days'], s['median_dol_days'], s['active_count'],
            s['aging_count'], s['mmr_trend_7d_pct'], s['mmr_trend_30d_pct'],
            s['verdict'], s['confidence'],
        ))
        n += 1
    return n


def upsert_snapshot(cur, dealer_id, today, vehicle, chip, confidence,
                    reasoning_text, inputs):
    cur.execute("""
        INSERT INTO dealer_intel_snapshot
          (dealer_inventory_id, dealer_id, vin, snapshot_date,
           chip, confidence, reasoning_text, inputs,
           days_on_lot, segment_avg_dol, segment_volume,
           asking_price, rbook_p50, rbook_p75,
           mmr_now, mmr_trend_7d_pct, mmr_trend_30d_pct,
           computed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (dealer_inventory_id) DO UPDATE
          SET snapshot_date     = EXCLUDED.snapshot_date,
              chip              = EXCLUDED.chip,
              confidence        = EXCLUDED.confidence,
              reasoning_text    = EXCLUDED.reasoning_text,
              inputs            = EXCLUDED.inputs,
              days_on_lot       = EXCLUDED.days_on_lot,
              segment_avg_dol   = EXCLUDED.segment_avg_dol,
              segment_volume    = EXCLUDED.segment_volume,
              asking_price      = EXCLUDED.asking_price,
              rbook_p50         = EXCLUDED.rbook_p50,
              rbook_p75         = EXCLUDED.rbook_p75,
              mmr_now           = EXCLUDED.mmr_now,
              mmr_trend_7d_pct  = EXCLUDED.mmr_trend_7d_pct,
              mmr_trend_30d_pct = EXCLUDED.mmr_trend_30d_pct,
              computed_at       = NOW()
    """, (
        vehicle['id'], dealer_id, vehicle.get('vin'), today,
        chip, confidence, reasoning_text, json.dumps(inputs),
        inputs.get('days_on_lot'), inputs.get('segment_avg_dol'),
        inputs.get('segment_sold_volume'),
        inputs.get('asking_price'),
        inputs.get('rbook_p50'), inputs.get('rbook_p75'),
        inputs.get('mmr_now'),
        inputs.get('mmr_trend_7d_pct'), inputs.get('mmr_trend_30d_pct'),
    ))


# ── Main ────────────────────────────────────────────────────────────────

def run(dealer_slug, window_days=16, dry_run=False, limit=None):
    today = date.today()
    with psycopg2.connect(**DB) as db:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            dealer = load_dealer(cur, dealer_slug)
            if not dealer:
                log.error('dealer slug %r not found', dealer_slug)
                return 2
            dealer_id = dealer['id']
            log.info('dealer=%s id=%d slug=%s window=%dd dry=%s',
                     dealer['name'], dealer_id, dealer_slug, window_days,
                     dry_run)

            active = load_active_inventory(cur, dealer_id, limit=limit)
            sold = load_sold_inventory(cur, dealer_id, window_days)
            comps = load_comp_snapshot(cur, dealer_id)
            mmr_trends = load_mmr_trends(cur, dealer_id, today)

            log.info('loaded active=%d sold=%d comps=%d mmr_trends=%d',
                     len(active), len(sold), len(comps), len(mmr_trends))

            segments = compute_segments(active, sold, mmr_trends, window_days)
            seg_lookup = {s['segment_key']: s for s in segments}
            log.info('computed %d segments', len(segments))

            # Compute aging_count per segment using true DOL vs segment avg.
            for v in active:
                dol = v.get('dol_today')
                if dol is None:
                    continue
                k = segment_key(v.get('make'), v.get('year'), v.get('mileage'))
                seg = seg_lookup.get(k)
                if seg and seg.get('avg_dol_days') is not None:
                    if dol > 1.2 * seg['avg_dol_days']:
                        seg['aging_count'] += 1

            chip_counts = {'keep': 0, 'hold': 0, 'price_drop': 0, 'sell_now': 0}
            chip_rows = []
            for v in active:
                comp = comps.get(v['id'], {})
                mmr = mmr_trends.get(v['id'], {})
                chip, conf, reasoning, inputs = compute_chip(
                    v, comp, mmr, seg_lookup, today)
                chip_counts[chip] += 1
                chip_rows.append((v, chip, conf, reasoning, inputs))

            log.info('chip distribution: %s', chip_counts)

            if dry_run:
                log.info('dry-run — skipping writes')
                # Print a few sample chips for sanity
                for v, chip, conf, reasoning, inputs in chip_rows[:5]:
                    log.info('  [%s/%s] %s %s %s — %s',
                             chip, conf, v.get('year'), v.get('make'),
                             v.get('model'), reasoning)
                return 0

            seg_n = upsert_segments(cur, dealer_id, today, segments)
            for v, chip, conf, reasoning, inputs in chip_rows:
                upsert_snapshot(cur, dealer_id, today, v, chip, conf,
                                reasoning, inputs)
            db.commit()
            log.info('persisted segments=%d snapshots=%d', seg_n,
                     len(chip_rows))
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dealer-slug', default='encore')
    p.add_argument('--window-days', type=int, default=16)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--limit', type=int, default=None)
    args = p.parse_args()
    try:
        return run(args.dealer_slug, args.window_days, args.dry_run,
                   args.limit)
    except Exception as e:
        log.exception('failed: %s', e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
