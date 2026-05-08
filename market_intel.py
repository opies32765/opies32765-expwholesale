"""market_intel.py — work-back from market data to target buy price.

The wholesale-trader STACK view:

  Retail asking (rBook competitive set) ─► dealers' retail target
                       │
                       ├─ minus dealers' typical retail gross
                       ▼
  Manheim auction floor (MMR transactions)  ◄──── what buyer dealers pay
                       │
                       ├─ minus LSL 12mo Avg Front Value (per YMM band)
                       ▼
  Our target buy price

Three data sources, NO hard-coded percentages:
  rbook_data       — vauto_lookups.rbook_competitive_set (oscar-worker-2)
  manheim_data     — vauto_lookups.manheim_transactions (oscar-worker-1)
  buyer_intel      — lsl_buyer_match.find_lsl_buyers (LSL crm.db)
"""

from __future__ import annotations
from statistics import median
from typing import Any


def _median(nums):
    nums = [n for n in nums if isinstance(n, (int, float)) and n > 0]
    return median(nums) if nums else None


def compute_market_intel(bid: dict,
                         manheim_data: dict | None,
                         rbook_data: dict | None,
                         buyer_intel: dict | None) -> dict | None:
    """Compute the full stack: retail → MMR → our buy.

    Returns a dict that the prompt formatter + UI both consume:
      manheim:       n_transactions, mmr_median, mmr_p25, mmr_p75, ...
      rbook:         n_rows, retail_median, retail_p25, retail_p75, count_text,
                     stocking_report (Demand/Interest/Volume/etc.)
      lsl_avg_front_value: live per-bid value (LSL deals.front_value averaged)
      derived:       implied_buyer_gross, target_buy_via_mmr,
                     buyer_margin_pct (retail-side margin if buyer pays us MMR)
      reasoning_lines: bullets ready for Gemini prompt injection
    """
    out = {
        'manheim': {},
        'rbook': {},
        'lsl_avg_front_value': None,
        'target_buy': None,
        'implied_buyer_gross': None,
        'buyer_margin_pct': None,
        'reasoning_lines': [],
    }

    subject_miles = bid.get('mileage') if bid else None

    # Sanity caps. Old-format rbook xlsx parser leaked stock numbers into
    # the price field; reject anything outside plausible vehicle bounds.
    PRICE_OK   = lambda v: isinstance(v, (int, float)) and 1_000 <= v <= 2_000_000
    MILES_OK   = lambda v: isinstance(v, (int, float)) and 0    <= v <= 500_000
    DOL_OK     = lambda v: isinstance(v, (int, float)) and 0    <= v <  1_000

    # ── Manheim (wholesale auction floor) ────────────────────────────
    mmr_median = None
    if manheim_data and isinstance(manheim_data, dict):
        tx = manheim_data.get('transactions') or []
        prices = sorted(t.get('sale_price') for t in tx if PRICE_OK(t.get('sale_price')))
        n_tx = len(prices)
        summary = manheim_data.get('summary') or {}
        out['manheim'] = {
            'n_transactions': n_tx,
            'base_mmr':       summary.get('base_mmr'),
            'adjusted_mmr':   summary.get('adjusted_mmr'),
            'avg_odometer':   summary.get('avg_odometer'),
            'avg_cr_grade':   summary.get('avg_cr_grade'),
            'pricing_date':   summary.get('pricing_date'),
        }
        if n_tx > 0:
            mmr_median = prices[n_tx // 2]
            out['manheim']['mmr_median'] = mmr_median
            out['manheim']['mmr_min']    = prices[0]
            out['manheim']['mmr_max']    = prices[-1]
            if n_tx >= 4:
                out['manheim']['mmr_p25'] = prices[n_tx // 4]
                out['manheim']['mmr_p75'] = prices[(3 * n_tx) // 4]
            dates = [t.get('date_sold') for t in tx if t.get('date_sold')]
            if dates:
                out['manheim']['date_range'] = f'{min(dates)} → {max(dates)}'
            # Closest-in-miles transaction (and ALL rows for the prompt)
            tx_full = [t for t in tx
                       if MILES_OK(t.get('odometer')) and PRICE_OK(t.get('sale_price'))]
            out['manheim']['transactions'] = tx_full  # full list for AI prompt
            if subject_miles and tx_full:
                tx_sorted = sorted(tx_full, key=lambda t: abs(t.get('odometer') - subject_miles))
                out['manheim']['closest_1'] = tx_sorted[0]

    # ── rBook (retail asking from other dealers) ────────────────────
    retail_median = None
    if rbook_data and isinstance(rbook_data, dict):
        rows = rbook_data.get('rows') or []
        # SANITY: old-format xlsx parser leaked stock numbers into the price
        # field (caused $804M medians). Cap at $2M, miles at 500k.
        usable_rows = [r for r in rows
                       if PRICE_OK(r.get('price')) and MILES_OK(r.get('mileage'))]
        asks = sorted(r.get('price') for r in usable_rows)
        n_rows = len(asks)
        out['rbook'] = {
            'n_visible':       n_rows,
            'count_text':      rbook_data.get('count_text'),
            'stocking_report': rbook_data.get('stocking_report'),
            'all_rows':        usable_rows,  # full list for AI prompt
        }
        if n_rows > 0:
            retail_median = asks[n_rows // 2]
            out['rbook']['retail_median'] = retail_median
            out['rbook']['retail_min']    = asks[0]
            out['rbook']['retail_max']    = asks[-1]
            if n_rows >= 4:
                out['rbook']['retail_p25'] = asks[n_rows // 4]
                out['rbook']['retail_p75'] = asks[(3 * n_rows) // 4]
            # Mileage + days stats from row data
            miles_list = [r.get('mileage') for r in usable_rows]
            days_list = [r.get('days_on_lot') for r in usable_rows
                         if DOL_OK(r.get('days_on_lot'))]
            if miles_list: out['rbook']['avg_mileage'] = sum(miles_list) // len(miles_list)
            if days_list:  out['rbook']['median_days_on_lot'] = sorted(days_list)[len(days_list)//2]
            # Closest-N rBook comps by absolute mile delta (for UI card + prompt)
            if subject_miles:
                by_dist = sorted(usable_rows, key=lambda r: abs(r.get('mileage') - subject_miles))
                out['rbook']['closest_3'] = by_dist[:3]

    # ── LSL Avg Front Value (LSL native field, not derived) ─────────
    lsl_avg_front_value = None
    if buyer_intel and isinstance(buyer_intel, dict):
        patterns = buyer_intel.get('patterns') or {}
        lsl_avg_front_value = patterns.get('avg_front_value')
    out['lsl_avg_front_value'] = lsl_avg_front_value

    # ── Derived: implied buyer dealer gross (the empirical retail spread) ──
    # Note: target_buy is NO LONGER computed here. The Gemini synthesis
    # (ai_assessment_v2) consumes these data points and outputs the target.
    # Computing a deterministic target here would just duplicate (and contradict)
    # Gemini's call. Keep `target_buy` key in output for backwards-compat
    # template rendering — set to None.
    out['target_buy'] = None

    if mmr_median and retail_median:
        spread = int(retail_median) - int(mmr_median)
        out['implied_buyer_gross'] = spread
        if retail_median > 0:
            out['buyer_margin_pct'] = round(100.0 * spread / retail_median, 1)

    # ── Reasoning bullets for Gemini prompt ─────────────────────────
    lines = []

    if retail_median:
        n = out['rbook']['n_visible']
        cnt = out['rbook'].get('count_text') or ''
        lines.append(f"RETAIL ASKING (rBook {cnt}): median ${retail_median:,} "
                     f"from {n} visible comps, range ${out['rbook'].get('retail_min', 0):,}–${out['rbook'].get('retail_max', 0):,}")
        if out['rbook'].get('avg_mileage'):
            lines.append(f"  Avg comp mileage: {out['rbook']['avg_mileage']:,}")
        if out['rbook'].get('median_days_on_lot'):
            lines.append(f"  Median days on lot at retail: {out['rbook']['median_days_on_lot']}")

    sr = out['rbook'].get('stocking_report')
    if sr:
        grades = [(k, v) for k, v in sr.items() if k != 'raw_text' and v]
        if grades:
            lines.append(f"  Stocking Report: " + ' · '.join(f"{k}={v}" for k, v in grades))

    if mmr_median:
        ntx = out['manheim']['n_transactions']
        lines.append(f"MMR AUCTION FLOOR: median ${mmr_median:,} from {ntx} hammer prices "
                     f"(range ${out['manheim'].get('mmr_min', 0):,}–${out['manheim'].get('mmr_max', 0):,})")
        if out['manheim'].get('date_range'):
            lines.append(f"  Window: {out['manheim']['date_range']}")
        if out['manheim'].get('adjusted_mmr'):
            lines.append(f"  vAuto Adjusted MMR: ${out['manheim']['adjusted_mmr']:,}")

    if out['implied_buyer_gross']:
        bgp = out.get('buyer_margin_pct') or 0
        lines.append(
            f"RETAIL − MMR SPREAD: retail ${retail_median:,} − MMR ${mmr_median:,} "
            f"= **${out['implied_buyer_gross']:,}** ({bgp:.1f}% of retail)."
        )

    if lsl_avg_front_value:
        lines.append(f"LSL 12mo AVG FRONT VALUE: ${int(lsl_avg_front_value):,}")

    # No deterministic target_buy line. The Gemini synthesis layer
    # consumes these anchors and outputs the recommendation directly.

    out['reasoning_lines'] = lines

    # Return None if NO useful market data at all
    has_anything = (mmr_median or retail_median or lsl_avg_front_value or
                    out['manheim'].get('base_mmr'))
    return out if has_anything else None


def format_for_prompt(market: dict | None) -> str:
    """Render the market anchors as a prompt block. NO computed target —
    Gemini consumes these data points and decides."""
    if not market:
        return ''
    lines = market.get('reasoning_lines') or []
    if not lines:
        return ''

    out = ['═══ MARKET ANCHORS (rBook retail + MMR floor + LSL history) ═══']
    out.extend('  • ' + l for l in lines)
    return '\n'.join(out)
