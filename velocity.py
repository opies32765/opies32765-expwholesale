"""
velocity.py — Partner-dealer network velocity score for AI assessment.

Computes how fast a YMM band is moving at the partner-dealer network over a
lookback window, and emits a labeled signal Gemini can use to lean its
adjustment up (fast mover, low aging risk) or down (stale, needs margin).

Reads from dealer_inventory (populated by the partner-dealer scanner cron at
6 AM ET) — uses verified_days_on_lot when available (vAuto-verified gold
standard), falls back to (sold_at - source_added_at) for sold rows and
(NOW() - source_added_at) for active rows.
"""

from __future__ import annotations


DEFAULT_VELOCITY_CONFIG = {
    "lookback_days":     90,
    "year_tolerance":     2,
    "mileage_band":   25000,
    "hot_max_days":      21,    # median days-to-sell ≤ this AND sold≥3 → HOT
    "steady_max_days":   45,    # ≤ this → STEADY
    "slow_max_days":     75,    # ≤ this → SLOW; > this → STALE
    "stale_dol_floor":   60,    # active median days-on-lot > this → STALE override
    "min_sample_size":    3,    # fewer total comps than this → NO_SIGNAL
}


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    if lo == hi:
        return sorted_vals[lo]
    return int(round(sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)))


def _model_root(model):
    """Strip suffix noise so 'GLS-Class' / 'GLS 450' / 'Gls' all match.
    Takes the first alphanumeric token, uppercases it. Returns '' if empty."""
    if not model:
        return ''
    import re as _re
    # Split on space, hyphen, slash; take first non-empty token
    parts = _re.split(r'[\s\-/]+', str(model).strip())
    first = next((p for p in parts if p), '')
    # Strip remaining non-alphanumeric (e.g. periods)
    first = _re.sub(r'[^A-Za-z0-9]', '', first)
    return first.upper()


def compute_velocity(db_conn, year, make, model, mileage=None, config=None):
    """Compute velocity stats for a year/make/model band.

    Returns a dict with sold/active distributions + a categorical label, or
    None if the inputs are insufficient to query (no make+model).
    """
    if not make or not model:
        return None

    cfg = {**DEFAULT_VELOCITY_CONFIG, **((config or {}).get('velocity') or {})}
    lookback = int(cfg['lookback_days'])
    yr_tol = int(cfg['year_tolerance'])
    mi_band = int(cfg['mileage_band'])

    yr_lo = (year - yr_tol) if year else None
    yr_hi = (year + yr_tol) if year else None
    mi_lo = (mileage - mi_band) if mileage else None
    mi_hi = (mileage + mi_band) if mileage else None

    # Normalize model so 'GLS-Class' (NHTSA) matches dealer-stored 'Gls' / 'GLS 450'.
    # Match condition: bid root equals dealer root (both reduced to first token).
    model_root = _model_root(model)
    model_pattern = model_root + '%' if model_root else model + '%'

    cur = db_conn.cursor()

    # ── SOLD rows in band, with computed days_to_sell ─────────────────────
    # Prefer verified_days_on_lot when set; else compute from timestamps.
    cur.execute("""
        SELECT
          COALESCE(
            verified_days_on_lot,
            GREATEST(0, EXTRACT(EPOCH FROM (sold_at - COALESCE(source_added_at, first_seen_at))) / 86400)::int
          ) AS dts
        FROM dealer_inventory
        WHERE status = 'sold'
          AND make ILIKE %s
          AND model ILIKE %s   -- normalized first-token prefix match
          AND (%s::int IS NULL OR year BETWEEN %s AND %s)
          AND (%s::int IS NULL OR mileage BETWEEN %s AND %s)
          AND sold_at IS NOT NULL
          AND sold_at > NOW() - (%s::text || ' days')::interval
          AND COALESCE(source_added_at, first_seen_at) IS NOT NULL
    """, (make, model_pattern, year, yr_lo, yr_hi, mileage, mi_lo, mi_hi, lookback))

    sold_days = sorted(
        int(r['dts']) for r in cur.fetchall()
        if r.get('dts') is not None and 0 <= int(r['dts']) <= 365
    )

    # ── ACTIVE rows in band — current days on lot ──────────────────────────
    cur.execute("""
        SELECT
          COALESCE(
            verified_days_on_lot,
            GREATEST(0, EXTRACT(EPOCH FROM (NOW() - COALESCE(source_added_at, first_seen_at))) / 86400)::int
          ) AS dol
        FROM dealer_inventory
        WHERE status = 'active'
          AND make ILIKE %s
          AND model ILIKE %s   -- normalized first-token prefix match
          AND (%s::int IS NULL OR year BETWEEN %s AND %s)
          AND (%s::int IS NULL OR mileage BETWEEN %s AND %s)
          AND COALESCE(source_added_at, first_seen_at) IS NOT NULL
    """, (make, model_pattern, year, yr_lo, yr_hi, mileage, mi_lo, mi_hi))

    active_days = sorted(
        int(r['dol']) for r in cur.fetchall()
        if r.get('dol') is not None and 0 <= int(r['dol']) <= 9999
    )

    # ── Distinct-dealer count for label context ─────────────────────────────
    cur.execute("""
        SELECT COUNT(DISTINCT dealer_id) AS n_dealers FROM dealer_inventory
        WHERE make ILIKE %s AND model ILIKE %s
    """, (make, model_pattern))
    n_dealers_row = cur.fetchone()
    n_dealers = int(n_dealers_row['n_dealers']) if n_dealers_row else 0

    # ── Label decision ──────────────────────────────────────────────────────
    sample_size = len(sold_days) + len(active_days)
    median_dts = sold_days[len(sold_days) // 2] if sold_days else None
    median_dol = active_days[len(active_days) // 2] if active_days else None

    label = 'NO_SIGNAL'
    note = None

    if sample_size < int(cfg['min_sample_size']):
        label = 'NO_SIGNAL'
        note = f'Only {sample_size} comps in the partner network — band too small to be reliable.'
    elif median_dts is None:
        # No sold history but active cars exist → status by aging
        if median_dol is not None and median_dol > int(cfg['stale_dol_floor']):
            label = 'STALE'
            note = 'No recent sales; active cars aging past stale floor.'
        else:
            label = 'NO_SIGNAL'
            note = 'No recent sales in window — cannot assess velocity yet.'
    else:
        if median_dts <= int(cfg['hot_max_days']) and len(sold_days) >= 3:
            label = 'HOT'
        elif median_dts <= int(cfg['steady_max_days']):
            label = 'STEADY'
        elif median_dts <= int(cfg['slow_max_days']):
            label = 'SLOW'
        else:
            label = 'STALE'

        # Stale override: even if median sold-time is OK, if there are many
        # active cars sitting much longer than the median that sold, it
        # signals the easy ones moved and the rest are stuck.
        if (median_dol is not None and len(active_days) >= 3
                and median_dol > int(cfg['stale_dol_floor'])
                and median_dol > (median_dts * 2)):
            label = 'STALE'
            note = (f'Sold cars moved in {median_dts}d but active cars aging '
                    f'{median_dol}d — easy ones gone, remaining stuck.')

    return {
        'label': label,
        'note': note,
        'sold_count': len(sold_days),
        'active_count': len(active_days),
        'sample_size': sample_size,
        'n_dealers_carrying': n_dealers,
        'median_days_to_sell': median_dts,
        'p25_days_to_sell': _percentile(sold_days, 25),
        'p75_days_to_sell': _percentile(sold_days, 75),
        'fastest_days': sold_days[0] if sold_days else None,
        'slowest_days': sold_days[-1] if sold_days else None,
        'median_active_days_on_lot': median_dol,
        'oldest_active_days': active_days[-1] if active_days else None,
        'lookback_days': lookback,
        'year_tolerance': yr_tol,
        'mileage_band': mi_band,
    }


# ── Label → guidance for the LLM ─────────────────────────────────────────────
_LABEL_GUIDANCE = {
    'HOT':       'fast turnover, low aging risk — confidence to bid closer to retail anchor',
    'STEADY':    'normal turnover — bid the baseline, no velocity premium or discount',
    'SLOW':      'below-average turnover — leave margin for slower flip',
    'STALE':     'poor turnover, high aging risk — discount aggressively for capital cost',
    'NO_SIGNAL': 'too few comps in partner network to score velocity — fall back to other signals',
}


def format_for_prompt(velocity, year=None, make=None, model=None, mileage=None):
    """Render a velocity dict into the assessment prompt block. Returns '' if no
    velocity (so the caller can append unconditionally)."""
    if not velocity:
        return ''
    v = velocity
    lines = []
    lines.append('═══ PARTNER NETWORK VELOCITY ═══')
    ymm = ' '.join(str(p) for p in (year, make, model) if p) or 'unknown'
    band_bits = []
    if v.get('year_tolerance'):
        band_bits.append(f"±{v['year_tolerance']} yrs")
    if mileage and v.get('mileage_band'):
        band_bits.append(f"±{v['mileage_band']:,} mi (target {mileage:,})")
    band_str = ', '.join(band_bits) or 'no band filter'
    lines.append(f'Band: {ymm}, {band_str}')
    lines.append(
        f"Last {v['lookback_days']}d across "
        f"{v.get('n_dealers_carrying', 0)} partner dealer(s) carrying this make/model:"
    )

    if v.get('sold_count'):
        sold_line = (
            f"  Sold: {v['sold_count']} — median {v['median_days_to_sell']}d to sell"
        )
        bits = []
        if v.get('p25_days_to_sell') is not None:
            bits.append(f"p25 {v['p25_days_to_sell']}d")
        if v.get('p75_days_to_sell') is not None:
            bits.append(f"p75 {v['p75_days_to_sell']}d")
        if v.get('fastest_days') is not None and v.get('slowest_days') is not None:
            bits.append(f"range {v['fastest_days']}–{v['slowest_days']}d")
        if bits:
            sold_line += ' (' + ' · '.join(bits) + ')'
        lines.append(sold_line)
    else:
        lines.append('  Sold: 0 in window')

    if v.get('active_count'):
        active_line = (
            f"  Active: {v['active_count']} currently listed"
        )
        if v.get('median_active_days_on_lot') is not None:
            active_line += f" — median {v['median_active_days_on_lot']}d on lot"
        if v.get('oldest_active_days') is not None and v['oldest_active_days'] > 60:
            active_line += f" · oldest {v['oldest_active_days']}d"
        lines.append(active_line)

    label = v.get('label') or 'NO_SIGNAL'
    guidance = _LABEL_GUIDANCE.get(label, '')
    lines.append(f"Velocity: {label} — {guidance}")
    if v.get('note'):
        lines.append(f"Note: {v['note']}")
    return '\n'.join(lines)
