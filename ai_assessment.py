"""
ai_assessment.py — Hybrid bucket-weighted baseline + LLM adjustment.

Philosophy:
  1. DETERMINISTIC FIRST — classify the car into a segment (bucket) and compute
     a baseline price using weighted book values specific to that bucket. This
     is reproducible, auditable, and the client controls the weights.
  2. LLM SECOND, CONSTRAINED — given the baseline + all qualitative data
     (Carfax, AutoCheck, photos, iPacket, AccuTrade, DIA comps), the LLM
     returns a percentage adjustment and reasoning, NOT a cold price. The
     adjustment is capped so the AI can't wander arbitrarily.

Config lives in ai_config table (versioned) so levers can be tuned in the
admin UI without code changes.

Every assessment run writes to ai_assessment_log for audit + before/after
comparison across config versions.
"""

from __future__ import annotations
from typing import Any


# ── Bucket classification ────────────────────────────────────────────────────

def classify_bucket(bid: dict, config: dict) -> dict:
    """Return the first bucket whose rules match the bid. Rules are evaluated
    in config order; last bucket should have rules.catch_all=True.

    Rule types (can be combined — ALL present rules must match):
      - makes: list of uppercase makes (exact match after uppercase)
      - model_patterns: list of uppercase substrings (any match = hit)
      - min_asking_price: asking_price must be >= this (if asking_price known)
      - min_year: vehicle year >= this
      - max_year: vehicle year <= this
      - catch_all: always matches (put last)

    Input `bid` should have: make, model, year, asking_price (any may be None).
    Returns the full bucket dict (including name, display_name, weights).
    """
    make = (bid.get('make') or '').strip().upper()
    model = (bid.get('model') or '').strip().upper()
    year = bid.get('year')
    asking = bid.get('asking_price')

    for bucket in config.get('buckets', []):
        rules = bucket.get('rules', {}) or {}
        if rules.get('catch_all'):
            return bucket

        # All specified rules must match
        makes = rules.get('makes')
        if makes and make not in [m.upper() for m in makes]:
            continue

        patterns = rules.get('model_patterns')
        if patterns:
            if not any(p.upper() in model for p in patterns):
                continue

        min_price = rules.get('min_asking_price')
        if min_price is not None:
            # If asking_price unknown, use KBB/MMR/rBook as a proxy later — for
            # now, only match if asking_price is known and >= min.
            try:
                if asking is None or float(asking) < float(min_price):
                    continue
            except (ValueError, TypeError):
                continue

        min_y = rules.get('min_year')
        if min_y is not None:
            try:
                if year is None or int(year) < int(min_y):
                    continue
            except (ValueError, TypeError):
                continue

        max_y = rules.get('max_year')
        if max_y is not None:
            try:
                if year is None or int(year) > int(max_y):
                    continue
            except (ValueError, TypeError):
                continue

        return bucket

    # Fallback if no catch_all was defined (shouldn't happen with defaults)
    return {
        "name": "mainstream_sub50k",
        "display_name": "Mainstream (fallback)",
        "weights": {"mmr": 0.4, "jd_power": 0.4, "rbook": 0.2}
    }


# ── Baseline calculation ─────────────────────────────────────────────────────

def _get_source_value(source: str, vauto: dict, accutrade: dict) -> int | None:
    """Extract a dollar value from vauto/accutrade data for a weight source key.
    Returns int or None if unavailable (0 / dash / None).
    Source keys supported:
      vAuto: rbook, mmr, black_book, kbb, kbb_com, jd_power
      AccuTrade: accutrade_target_auction, accutrade_instant_offer,
                 accutrade_target_retail, accutrade_market_pricing
      Future placeholders: bat_comps (Bring-a-Trailer) — returns None until wired
    """
    if not source:
        return None
    v = vauto or {}
    a = accutrade or {}

    vauto_map = {
        'rbook': v.get('rbook'),
        'mmr': v.get('mmr'),
        'black_book': v.get('black_book'),
        'kbb': v.get('kbb'),
        'kbb_com': v.get('kbb_com'),
        'jd_power': v.get('jd_power'),
    }
    at_map = {
        'accutrade_target_auction': a.get('trade_in'),      # AccuTrade Target Auction
        'accutrade_instant_offer':  a.get('guaranteed_offer'),
        'accutrade_target_retail':  a.get('trade_market'),  # AccuTrade Target Retail
        'accutrade_market_pricing': a.get('retail'),        # AccuTrade "Market Pricing"
        'accutrade_wholesale_avg':  a.get('market_avg'),
    }

    if source in vauto_map:
        val = vauto_map[source]
    elif source in at_map:
        val = at_map[source]
    else:
        return None  # placeholder keys like bat_comps stay unavailable

    try:
        iv = int(val) if val is not None else None
    except (ValueError, TypeError):
        return None
    if iv is None or iv <= 0:
        return None
    return iv


def compute_baseline(bucket: dict, vauto: dict, accutrade: dict) -> dict:
    """Compute the weighted-baseline price for this bucket.

    Handles missing book values by renormalizing weights among available sources.
    E.g. if a bucket uses 40% MMR + 40% JDP + 20% rBook but JDP is N/A, the
    weights become 50% MMR + 25% rBook (proportional to what was there) so
    baseline isn't artificially low from multiplying by zero.

    Returns:
      {
        'baseline_price': int or None,
        'breakdown': [
          {'source': 'mmr', 'value': 32200, 'weight_pct': 40, 'effective_pct': 50.0,
           'contribution': 16100, 'available': True},
          {'source': 'jd_power', 'value': None, 'weight_pct': 40, 'effective_pct': 0,
           'contribution': 0, 'available': False},
          ...
        ],
        'total_weight_available_pct': 60,  # out of original 100
        'note': None or 'JDP N/A — renormalized'
      }
    """
    weights = bucket.get('weights', {}) or {}
    rows = []

    # Gather each configured weight
    for source, pct in weights.items():
        val = _get_source_value(source, vauto, accutrade)
        rows.append({
            'source': source,
            'value': val,
            'weight_pct': round(float(pct) * 100, 1),
            'available': val is not None,
        })

    total_orig = sum(r['weight_pct'] for r in rows)
    avail_total = sum(r['weight_pct'] for r in rows if r['available'])

    if avail_total == 0 or not rows:
        return {
            'baseline_price': None,
            'breakdown': rows,
            'total_weight_available_pct': 0,
            'note': 'No book values available for this bucket'
        }

    baseline_sum = 0.0
    for r in rows:
        if r['available']:
            effective_pct = (r['weight_pct'] / avail_total) * 100  # renormalized to 100
            contribution = int(round(r['value'] * effective_pct / 100))
            r['effective_pct'] = round(effective_pct, 1)
            r['contribution'] = contribution
            baseline_sum += contribution
        else:
            r['effective_pct'] = 0.0
            r['contribution'] = 0

    baseline = int(round(baseline_sum))

    note = None
    if avail_total < total_orig:
        missing = [r['source'] for r in rows if not r['available']]
        note = f"Renormalized: {', '.join(missing)} N/A (had {avail_total}% of {total_orig}%)"

    return {
        'baseline_price': baseline,
        'breakdown': rows,
        'total_weight_available_pct': round(avail_total, 1),
        'note': note
    }


# ── LLM prompt builder (adjustment-only, not cold pricing) ───────────────────

ADJUSTMENT_PROMPT_TEMPLATE = """You are a wholesale vehicle buyer reviewing a bid.

SEGMENT: {bucket_display}
DETERMINISTIC BASELINE (from weighted book values): ${baseline:,}
BASELINE BREAKDOWN:
{breakdown_text}

Your job: review the qualitative data below and output a PERCENTAGE ADJUSTMENT
to the baseline — NOT a new price. Stay within ±{cap}%. If data is favorable
(clean Carfax, low miles for age, pristine photos), adjust UP. If unfavorable
(accidents, fleet title, rough condition, over-miles), adjust DOWN.

Return ONLY this JSON (no markdown, no commentary):
{{
  "adjustment_pct": -5.0,
  "confidence_low_pct": -8.0,
  "confidence_high_pct": -2.0,
  "reasoning": "2 owners, 1 accident (minor front-end per Carfax), condition B+..."
}}

Rules:
- adjustment_pct must be between -{cap} and +{cap}
- confidence_low_pct <= adjustment_pct <= confidence_high_pct
- reasoning must be 1-3 sentences explaining the KEY factors (not a summary of all data)
- No $ figures in reasoning — only references to factors (Carfax, AutoCheck, photos, etc.)
"""


def build_adjustment_prompt(bucket: dict, baseline_result: dict, cap_pct: float) -> str:
    """Compose the Gemini prompt asking for a pct adjustment, not a cold price."""
    breakdown_lines = []
    for r in baseline_result.get('breakdown', []):
        if r.get('available'):
            breakdown_lines.append(
                f"  - {r['source']}: ${r['value']:,} × {r.get('effective_pct', r['weight_pct'])}% = ${r['contribution']:,}"
            )
        else:
            breakdown_lines.append(f"  - {r['source']}: N/A (skipped, weight redistributed)")
    breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "  (no book values)"

    return ADJUSTMENT_PROMPT_TEMPLATE.format(
        bucket_display=bucket.get('display_name', bucket.get('name', 'unknown')),
        baseline=baseline_result.get('baseline_price') or 0,
        breakdown_text=breakdown_text,
        cap=cap_pct,
    )


def apply_adjustment(baseline: int | None, adjustment_pct: float,
                     confidence_low_pct: float, confidence_high_pct: float,
                     cap_pct: float) -> dict:
    """Apply the LLM's adjustment to the baseline, clamping to ±cap_pct.
    Returns {final_price, confidence_low, confidence_high, clamped}.
    """
    if baseline is None or baseline <= 0:
        return {'final_price': None, 'confidence_low': None, 'confidence_high': None,
                'clamped': False}

    orig_adj = float(adjustment_pct)
    clamped_adj = max(-cap_pct, min(cap_pct, orig_adj))
    clamped_low = max(-cap_pct, min(cap_pct, float(confidence_low_pct)))
    clamped_high = max(-cap_pct, min(cap_pct, float(confidence_high_pct)))

    final = int(round(baseline * (1 + clamped_adj / 100)))
    lo = int(round(baseline * (1 + clamped_low / 100)))
    hi = int(round(baseline * (1 + clamped_high / 100)))

    return {
        'final_price': final,
        'confidence_low': min(lo, hi),
        'confidence_high': max(lo, hi),
        'clamped': clamped_adj != orig_adj,
        'effective_adjustment_pct': clamped_adj,
    }
