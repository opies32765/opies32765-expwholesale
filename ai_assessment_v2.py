"""ai_assessment_v2.py — single-shot Gemini synthesis over the full market stack.

Pivots away from the bucket-weighted baseline + ±15% offset architecture.
Now Gemini sees:
  - Equipment / condition: photos, Carfax-OCR, AutoCheck-OCR, iPacket window sticker
  - Market stack: MMR transactions, rBook competitive set, partner-dealer inventory,
                  partner sold history (turnover + days-to-sell)
  - Our history: LSL avg sale_price + avg_gross + top buyer candidates
  - Reference data: book values (rBook, MMR, BlackBook, KBB, JDP, AccuTrade) and
                    velocity label (HOT/STEADY/SLOW) — context, not anchors

Returns absolute target_buy ($) + confidence range + reasoning + flags.
NO bucket classification, NO percentage offsets, NO hard-coded margins.

Storage: maps onto existing ai_assessment_log columns where semantically
clean (final_price, confidence_low/high, llm_reasoning) plus two new
JSONB columns (flags_v2, inputs_v2). Old bucket-anchor columns (bucket,
baseline_price, breakdown, llm_adjustment_pct) stay null going forward.
"""

from __future__ import annotations
import json
import re
from typing import Any


PROMPT_TEMPLATE_V2 = """You are evaluating a used vehicle for wholesale acquisition by Experience Wholesale (EW).

EW buys cars and resells them to retail dealers, who then retail them to consumers. Your job is to recommend a target wholesale BUY price for this vehicle, balancing:
  • What buyer dealers will actually pay us (auction floor + demand premium/discount)
  • What we historically capture as gross margin (LSL avg gross is our PVR target)
  • This specific car's condition vs the comparable set (photos, Carfax, AutoCheck, options)

═══ THIS VEHICLE ═══

{vehicle_summary}

{equipment_section}

{market_stack}

{thalist_asks_section}

{book_values_section}

{purchase_history_section}

{ml_section}

{velocity_section}

═══ KEY QUESTIONS TO ASK YOURSELF ═══

1. Is the retail-to-MMR spread healthy (>$5k)? Strong retail demand → buyer dealer has cushion → we can pay closer to or above MMR.
2. Is the spread thin (<$2k)? Soft retail → pay below MMR for safety.
3. Does this car's condition (Carfax flags, mileage vs comps, options package) suggest premium or discount vs comps?
4. What's our LSL avg gross for this YMM? That's our PVR target on this deal.
5. Is velocity HOT? Buyer dealers will pay up to lock supply. SLOW/STALE? Pay down for cushion.
6. **MILEAGE GAP CHECK**: If subject mileage is >2x the avg comp mileage shown in rBook, the rBook median is NOT a fair price anchor — it represents a near-new cohort, not yours. In that case:
   • Use **vAuto's Adjusted MMR** (which is already auto-adjusted for miles + condition) as your primary anchor.
   • Or use the **3 closest-mile rBook comps** (a better-matched cohort).
   • Don't over-discount from the raw rBook median — the market sets a tighter mileage curve than naively pricing each 1k miles linearly.
   • The Manheim MMR median already reflects mixed-mileage auction reality, so it's a more honest floor than rBook for high-mile subjects.
7. {asking_constraint}

═══ YOUR OUTPUT ═══

Return ONLY this JSON (no markdown, no commentary):
{{
  "target_buy": <integer dollars>,
  "confidence_low": <integer dollars>,
  "confidence_high": <integer dollars>,
  "reasoning": "<2-3 sentences identifying the KEY drivers>",
  "flags": [<short snake_case tags>]
}}

Rules:
  • target_buy is a dollar integer (no commas, no "$")
  • confidence_low ≤ target_buy ≤ confidence_high
  • reasoning identifies the 2-3 most important drivers (not a summary of all data)
  • flags: short tags like "clean_carfax", "hot_velocity", "low_miles_vs_comps",
    "accident_history", "options_premium", "stale_market", "high_lsl_demand", etc.
"""


# ── Section composers ────────────────────────────────────────────────────

def _vehicle_summary(bid: dict, nhtsa: dict | None = None,
                     tesla: dict | None = None) -> str:
    parts = [str(bid.get('year') or ''), bid.get('make') or '',
             bid.get('model') or '', bid.get('trim') or '']
    head = ' '.join(p for p in parts if p).strip() or 'Unknown vehicle'
    miles = bid.get('mileage')
    color = bid.get('color')
    vin = bid.get('vin')
    notes = bid.get('notes')
    asking = bid.get('asking_price')

    lines = [f"VEHICLE: {head}"]
    lines.append(f"VIN: {vin or 'N/A'}")
    lines.append(f"Mileage: {f'{miles:,}' if miles else 'unknown'}")
    if color: lines.append(f"Color: {color}")
    if asking: lines.append(f"Asking price (from seller): ${asking:,}")
    if notes: lines.append(f"Notes: {notes}")

    if nhtsa:
        if nhtsa.get('trim_ambiguous'):
            alts = nhtsa.get('trim_alternatives') or []
            lines.append(f"NHTSA trim ambiguous — candidates: {' / '.join(alts)}")
        for k, label in (('engine_hp', 'HP'), ('drive_type', 'Drive'),
                         ('engine_cylinders', 'Cyl'), ('fuel_type', 'Fuel')):
            v = nhtsa.get(k)
            if v: lines.append(f"  {label}: {v}")
    if tesla and tesla.get('fleet_specs'):
        lines.append(f"Tesla Fleet API specs available: {bool(tesla.get('fleet_specs'))}")
    return '\n'.join(lines)


def _equipment_section(ipacket: dict | None, photos: list,
                       carfax_text: str = '', autocheck_text: str = '') -> str:
    out = ['═══ EQUIPMENT & CONDITION ═══']
    if photos:
        out.append(f"Photos: {len(photos)} attached (review for visible condition, color match, dash/odometer, panel gaps).")
    if ipacket and (ipacket.get('total_msrp') or ipacket.get('base_price')):
        msrp = ipacket.get('total_msrp')
        base = ipacket.get('base_price')
        ec = ipacket.get('exterior_color')
        ic = ipacket.get('interior_color')
        ext = []
        if msrp: ext.append(f"Total MSRP ${msrp:,}")
        if base: ext.append(f"Base price ${base:,}")
        if ec: ext.append(f"Exterior {ec}")
        if ic: ext.append(f"Interior {ic}")
        out.append("iPacket window sticker: " + ' · '.join(ext))
    if carfax_text:
        out.append("Carfax (OCR text):")
        out.append(carfax_text[:2000].strip())
    if autocheck_text:
        out.append("AutoCheck (OCR text):")
        out.append(autocheck_text[:2000].strip())
    if len(out) == 1:
        out.append("(no equipment/condition data — Gemini must rely on photos alone)")
    return '\n'.join(out)


def _market_stack(market_intel: dict | None, dealer_intel: dict | None,
                  buyer_intel: dict | None, subject_miles: int | None = None) -> str:
    out = ['═══ MARKET STACK ═══']

    # Retail signal
    out.append('\n▸ RETAIL SIGNAL (what dealers ask):')
    rb = (market_intel or {}).get('rbook') or {}
    if rb.get('retail_median'):
        out.append(f"  rBook competitive set: median ${rb['retail_median']:,} "
                   f"(n={rb.get('n_visible', 0)} visible{', vAuto reports ' + str(rb['count_text']) + ' total' if rb.get('count_text') else ''})")
        if rb.get('retail_min') and rb.get('retail_max'):
            out.append(f"    range ${rb['retail_min']:,} – ${rb['retail_max']:,}")
        if rb.get('median_days_on_lot'):
            out.append(f"    median days on lot at retail: {rb['median_days_on_lot']}")
    sr = rb.get('stocking_report')
    if sr:
        bits = [f"{k}={v}" for k, v in sr.items() if k != 'raw_text' and v]
        if bits:
            out.append(f"  Stocking Report (vAuto): {' · '.join(bits)}")

    # Per-row rBook comps. Three closest-in-miles get highlighted.
    closest_3 = rb.get('closest_3') or []
    all_rows  = rb.get('all_rows') or []
    if closest_3 and subject_miles:
        out.append(f"\n  3 CLOSEST-MILE rBOOK COMPS (subject: {subject_miles:,} mi):")
        for i, r in enumerate(closest_3, 1):
            miles = r.get('mileage') or 0
            delta = miles - subject_miles
            sign = '+' if delta >= 0 else ''
            price = r.get('price')
            dol = r.get('days_on_lot')
            dealer = r.get('dealer') or '?'
            city_state = ', '.join(filter(None, [r.get('dealer_city'), r.get('dealer_state')]))
            tag = ''
            if r.get('is_certified'): tag += ' [CPO]'
            if r.get('pending_sale'): tag += ' [pending]'
            msrp = (r.get('msrp_lookup') or {}).get('msrp')
            msrp_str = f' MSRP=${msrp:,}' if msrp else ''
            price_str = f'${price:,}' if isinstance(price, (int, float)) and price else 'no price'
            dol_str = f'{dol}d on lot' if dol is not None else 'DOL?'
            out.append(f"    #{i} {miles:,} mi ({sign}{delta:,}) · {price_str} · "
                       f"{dol_str}{msrp_str} · {dealer} {city_state}{tag}")
    if all_rows and len(all_rows) > 3:
        out.append(f"\n  ALL rBOOK COMPS ({len(all_rows)} rows — miles, asking price, days on lot):")
        # Compact list: just miles/price/DOL per row, sorted by miles
        rows_sorted = sorted(all_rows, key=lambda r: r.get('mileage') or 0)
        for r in rows_sorted:
            m = r.get('mileage') or 0
            p = r.get('price') or 0
            d = r.get('days_on_lot')
            tag = ''
            if r.get('is_certified'): tag += ' CPO'
            if r.get('pending_sale'): tag += ' pending'
            d_str = f'{d}d' if d is not None else '?d'
            out.append(f"    {m:>7,} mi · ${p:>7,} · {d_str:<5}{tag}")
        out.append("\n  ⚠ DAYS-ON-LOT GUIDANCE: a 60+ day comp signals the seller is "
                   "asking too much (market hasn't accepted the price). A 5-15 day "
                   "comp signals correct market pricing. Weight LOW-DOL comps more "
                   "heavily when triangulating retail value.")

    di = dealer_intel or {}
    di_active = di.get('active') or []
    if di_active:
        out.append(f"  Partner-dealer inventory: {len(di_active)} like-vehicles currently listed at our scanned dealers")
        for a in di_active[:5]:
            price = f"${a['price']:,}" if a.get('price') else 'no price'
            dol = f"{a['days_on_lot']}d" if a.get('days_on_lot') is not None else '?d'
            out.append(f"    • {a.get('dealer_name', '?')} {a.get('city', '')},{a.get('state', '')}: {price} · {dol} on lot")

    di_sales = di.get('recent_sales') or []
    if di_sales:
        out.append(f"  Partner-dealer recent sales: {len(di_sales)} like-cars sold")
        for s in di_sales[:5]:
            out.append(f"    • {s.get('dealer_name', '?')}: ${s.get('price') or 0:,} · sold in {s.get('days_to_sell') or '?'} days")

    if not (rb.get('retail_median') or di_active or di_sales):
        out.append('  (no retail signal — rare/exotic or partner DB miss)')

    # Wholesale signal
    out.append('\n▸ WHOLESALE SIGNAL (what cars actually clear at auction):')
    mh = (market_intel or {}).get('manheim') or {}
    if mh.get('mmr_median'):
        out.append(f"  MMR auction floor: median ${mh['mmr_median']:,} from {mh.get('n_transactions', 0)} hammer prices")
        if mh.get('date_range'):
            out.append(f"    window: {mh['date_range']}")
        if mh.get('mmr_min') and mh.get('mmr_max'):
            out.append(f"    range ${mh['mmr_min']:,} – ${mh['mmr_max']:,}")
        if mh.get('mmr_p25') and mh.get('mmr_p75'):
            out.append(f"    p25–p75 ${mh['mmr_p25']:,} – ${mh['mmr_p75']:,}")
    if mh.get('adjusted_mmr'):
        out.append(f"  vAuto Adjusted MMR (auto-adjusted for miles + condition): ${mh['adjusted_mmr']:,}")

    # Closest-mile MMR sale + ALL MMR transactions
    closest_mmr = mh.get('closest_1')
    mmr_txns = mh.get('transactions') or []
    if closest_mmr and subject_miles:
        cm_miles = closest_mmr.get('odometer') or 0
        cm_delta = cm_miles - subject_miles
        cm_sign = '+' if cm_delta >= 0 else ''
        cm_price = closest_mmr.get('sale_price')
        cm_date = closest_mmr.get('date_sold') or '?'
        cm_grade = closest_mmr.get('cr_grade') or '?'
        out.append(f"\n  CLOSEST-MILE MMR SALE (subject: {subject_miles:,} mi):")
        out.append(f"    {cm_miles:,} mi ({cm_sign}{cm_delta:,}) · "
                   f"${cm_price:,} · sold {cm_date} · CR={cm_grade}")
    if mmr_txns and len(mmr_txns) > 1:
        out.append(f"\n  ALL MMR TRANSACTIONS ({len(mmr_txns)} sales):")
        for t in sorted(mmr_txns, key=lambda x: x.get('odometer') or 0):
            m = t.get('odometer') or 0
            p = t.get('sale_price') or 0
            d = t.get('date_sold') or ''
            g = t.get('cr_grade') or ''
            g_str = f' CR={g}' if g else ''
            out.append(f"    {m:>7,} mi · ${p:>7,} · {d}{g_str}")

    if not mh.get('mmr_median'):
        out.append('  (no Manheim transactions — rare/exotic — fall back to LSL + rBook)')

    # Implied buyer-dealer gross (the spread)
    if (market_intel or {}).get('implied_buyer_gross'):
        spread = market_intel['implied_buyer_gross']
        pct = market_intel.get('buyer_margin_pct')
        out.append(f"\n▸ IMPLIED BUYER DEALER GROSS: retail median − MMR median = ${spread:,}"
                   + (f" ({pct}% of retail)" if pct else ''))
        out.append(f"  This is the empirical retail margin a buyer dealer captures if they pay us at MMR.")

    # Our history
    out.append('\n▸ OUR HISTORY (LSL ledger, last 12 months):')
    bi = buyer_intel or {}
    p = bi.get('patterns') or {}
    if p.get('total_deals'):
        avg_sale = p.get('avg_sale_price')
        avg_gross = p.get('avg_gross')
        avg_purchase = (int(avg_sale) - int(avg_gross)) if (avg_sale and avg_gross) else None
        out.append(f"  Like-deals: {p['total_deals']} across {p.get('unique_buyers', 0)} unique buyers")
        if avg_sale: out.append(f"  Avg sale price (what buyer dealers paid us): ${int(avg_sale):,}")
        if avg_purchase: out.append(f"  Avg purchase cost (what we historically paid): ${avg_purchase:,}")
        if avg_gross: out.append(f"  Avg gross (PVR captured): ${int(avg_gross):,} ← our PVR target")
        if p.get('total_deals_90d'):
            out.append(f"  Deals last 90d: {p['total_deals_90d']}")
    top_buyers = bi.get('top_pitch_buyers') or []
    if top_buyers:
        out.append(f"  Top buyer candidates for THIS car:")
        for b in top_buyers[:3]:
            out.append(f"    • {b.get('buyer_name', '?')}: {b.get('reason', '')}")
    if not p.get('total_deals'):
        out.append('  (no LSL like-deals in 12mo — first time selling this YMM)')

    return '\n'.join(out)


def _book_values_section(vauto: dict | None, accutrade: dict | None) -> str:
    out = ['═══ BOOK VALUES (reference, not anchor) ═══']
    rows = []
    v = vauto or {}
    a = accutrade or {}
    for label, val in (
        ('rBook',           v.get('rbook')),
        ('Manheim MMR',     v.get('mmr')),
        ('Black Book',      v.get('black_book')),
        ('KBB',             v.get('kbb')),
        ('KBB.com',         v.get('kbb_com')),
        ('J.D. Power',      v.get('jd_power')),
        ('AccuTrade Target Auction', a.get('trade_in')),
        ('AccuTrade Instant Offer',  a.get('guaranteed_offer')),
        ('AccuTrade Target Retail',  a.get('trade_market')),
        ('AccuTrade Market',         a.get('retail')),
    ):
        if val:
            try: rows.append(f"  {label}: ${int(val):,}")
            except (ValueError, TypeError): pass
    if rows:
        out.extend(rows)
    else:
        out.append('  (no book values captured)')

    if v.get('title_status'):
        out.append(f"  Title status: {v['title_status']}")
    return '\n'.join(out)


def _ml_section(ml: dict | None) -> str:
    """Per-make XGBoost prediction as a calibration anchor for Gemini.
    Source: ml_predict.predict_for_bid() against trained models in
    /opt/expwholesale/ml/models/per_make/. Returns empty string if no
    model exists for the make (silent skip — Gemini reasons without it)."""
    if not ml or not ml.get('prediction'):
        return ''
    out = ['═══ ML MODEL CALIBRATION ANCHOR ═══']
    pred = ml['prediction']
    src_label = 'XGBoost' if ml.get('source') == 'xgboost' else 'baseline ratio'
    n_train = ml.get('n_train') or 0
    out.append(f'  Model: {src_label}, trained on n={n_train:,} historical '
               f'EW {ml.get("make_name", "?")} purchases')
    out.append(f'  Predicted EW purchase price: ${pred:,}')
    if ml.get('mape_pct') is not None:
        out.append(f'  Holdout MAPE: {ml["mape_pct"]:.1f}%'
                   + (f'  (within ±10% on {ml["within_10pct"]:.0f}% of recent test bids)'
                      if ml.get('within_10pct') else ''))
    if ml.get('baseline_prediction') and ml.get('baseline_prediction') != pred:
        out.append(f'  (Simple per-make baseline ratio said: ${ml["baseline_prediction"]:,})')
    out.append('')
    out.append('  Use this as a CALIBRATION ANCHOR — it is what a model trained on')
    out.append('  EW past purchases of this make predicts. Treat as a strong prior,')
    out.append('  but override if your reasoning surfaces vehicle-specific factors')
    out.append('  (carfax, options, miles vs comp set, scarcity) that this model')
    out.append('  cannot see. State your reasoning if you diverge >10% from this.')
    return '\n'.join(out)


def _purchase_history_section(ph: dict | None) -> str:
    """Per-YMM retrieval. Shows EW's actual reconciled purchase history AND
    the AI's own track record on this YMM (mean signed error, median |error|).
    Gemini sees its own scoreboard for cars like this one.
    Tiered match: same YMM + similar mileage first, broadens if sparse."""
    if not ph or not ph.get('n'):
        return ''
    out = ['═══ HISTORICAL PURCHASE RETRIEVAL ═══']
    n_hp = ph['n']
    sample_note = ' (small sample — use as weak signal, do NOT anchor)' if n_hp < 5 else ''
    out.append(f"  Match tier: {ph.get('tier', '?')}  ·  n = {n_hp} historical purchases{sample_note}")
    if ph.get('mean'):
        out.append(f"  Average actual purchase: ${ph['mean']:,}")
    if ph.get('median'):
        out.append(f"  Median:  ${ph['median']:,}")
    if ph.get('stddev'):
        out.append(f"  StdDev:  ${ph['stddev']:,}")
    if ph.get('min') and ph.get('max'):
        out.append(f"  Range:   ${ph['min']:,} – ${ph['max']:,}")
    if ph.get('avg_mileage'):
        out.append(f"  Avg mileage of those deals: {ph['avg_mileage']:,}")

    # AI track record on this same set — direct calibration signal
    signed = ph.get('ai_mean_signed_pct')
    abs_pct = ph.get('ai_median_abs_pct')
    in_range = ph.get('ai_in_range_n') or 0
    n = ph['n']
    if signed is not None or abs_pct is not None:
        out.append(f"\n  ▸ AI track record on these {n} matches:")
        if abs_pct is not None:
            out.append(f"    Median |error|:    {abs_pct:.2f}%")
        if signed is not None:
            sign = '+' if signed > 0 else ''
            direction = 'AI under-recommended' if signed > 0 else 'AI over-recommended' if signed < 0 else 'unbiased'
            out.append(f"    Mean signed error: {sign}{signed:.2f}%  ←  {direction}")
        if n > 0:
            out.append(f"    In confidence range: {in_range}/{n} ({100.0*in_range/n:.0f}%)")

    samples = ph.get('samples') or []
    if samples:
        out.append(f"\n  Recent matches (newest first, up to 5):")
        for s in samples[:5]:
            ymm = ' '.join(str(x) for x in [s.get('year'), s.get('make'), s.get('model')] if x)
            mi  = f"{int(s['mileage']):,}mi" if s.get('mileage') else 'unknown mi'
            ap  = f"${int(s['actual_purchase_cost']):,}" if s.get('actual_purchase_cost') else '—'
            ai_rec = f" (AI rec was ${int(s['ai_recommendation']):,}" if s.get('ai_recommendation') else ''
            if s.get('delta_pct') is not None and s.get('ai_recommendation'):
                d_pct = s['delta_pct']
                ai_rec += f", off by {'+' if d_pct > 0 else ''}{d_pct:.1f}%)"
            elif ai_rec:
                ai_rec += ')'
            d = s.get('purchased_date') or '?'
            out.append(f"    • bid #{s.get('bid_id')}  {ymm}  {mi}  paid {ap}{ai_rec}  on {d}")

    out.append("\n  Use this as a CALIBRATION ANCHOR — it's what EW has actually paid.")
    out.append("  If your reasoning shows AI tends to under/over-recommend by X% on this YMM,")
    out.append("  consider adjusting your target_buy in that direction.")
    out.append("  Tier 1 (YMM+miles) is the strongest signal; broader tiers are weaker.")
    return '\n'.join(out)


def _velocity_section(velocity: dict | None) -> str:
    if not velocity:
        return ''
    out = ['═══ TURNOVER / VELOCITY ═══']
    label = velocity.get('label', 'NO_SIGNAL')
    out.append(f"  Label: {label}")
    if velocity.get('sold_count') is not None:
        out.append(f"  Like-cars sold (last 90d): {velocity['sold_count']}")
    if velocity.get('median_days_to_sell'):
        out.append(f"  Median days to sell: {velocity['median_days_to_sell']}")
    if velocity.get('active_count') is not None:
        out.append(f"  Currently active in partner inventory: {velocity['active_count']}")
    return '\n'.join(out)


# ── Public API ────────────────────────────────────────────────────────────

def _thalist_asks_section(asks: dict | None) -> str:
    """Build the Thalist wholesale-ask context block.

    `asks` is a dict from _get_thalist_asks_for_bid() in app.py:
      {n: int, p25: int|None, p50: int|None, p75: int|None,
       posts: [{title, asking_price, mileage, poster_company}, ...]}

    Returns an empty string when there\'s no matching data, so the
    prompt doesn\'t clutter with "no data" lines.
    """
    if not asks or not asks.get('n'):
        return ''
    n = asks['n']
    p25, p50, p75 = asks.get('p25'), asks.get('p50'), asks.get('p75')
    if not p50:
        return ''
    lines = [
        '═══ THALIST WHOLESALE ASKS (live wholesaler marketplace) ═══',
        '',
        f'{n} other wholesalers actively asking for same year/make/model:',
        f'  P25 ask: ${p25:,}     P50 ask: ${p50:,}     P75 ask: ${p75:,}',
        '',
        'These are ASKING prices (what sellers HOPE to get), not closed',
        'transactions. Treat as a CEILING reference — actual clearance',
        'typically runs 3-7% below median ask. MMR transactions remain',
        'your primary wholesale anchor.',
    ]
    # Show a few raw post lines if available (helps Gemini see breadth)
    posts = asks.get('posts') or []
    if posts:
        lines.append('')
        lines.append('Sample posts:')
        for p in posts[:5]:
            ask_s = f'${p.get("asking_price"):,}' if p.get('asking_price') else 'no price'
            miles_s = f'{int(p["mileage"]):,} mi' if p.get('mileage') else 'no miles'
            title = (p.get('title') or '')[:60]
            company = (p.get('poster_company') or '')[:30]
            lines.append(f'  • {ask_s} · {miles_s} · {title}'
                         f'{" @ " + company if company else ""}')
    return '\n'.join(lines)


def build_prompt(bid: dict, *, vauto: dict | None = None,
                 accutrade: dict | None = None, ipacket: dict | None = None,
                 photos: list | None = None,
                 carfax_text: str = '', autocheck_text: str = '',
                 dealer_intel: dict | None = None,
                 buyer_intel: dict | None = None,
                 market_intel: dict | None = None,
                 velocity: dict | None = None,
                 nhtsa: dict | None = None,
                 tesla: dict | None = None,
                 purchase_history: dict | None = None,
                 ml_prediction: dict | None = None,
                 thalist_asks: dict | None = None) -> str:
    """Compose the v2 assessment prompt. All inputs optional; sections render
    with placeholders when data is missing."""
    asking = bid.get('asking_price')
    asking_constraint = (
        f"Seller is asking ${asking:,}. Use this as a CEILING — your target buy MUST be at or below asking."
        if asking else
        "No asking price stated — set target_buy purely from the data."
    )

    return PROMPT_TEMPLATE_V2.format(
        vehicle_summary=_vehicle_summary(bid, nhtsa, tesla),
        equipment_section=_equipment_section(ipacket, photos or [],
                                              carfax_text, autocheck_text),
        market_stack=_market_stack(market_intel, dealer_intel, buyer_intel,
                                   subject_miles=bid.get('mileage')),
        book_values_section=_book_values_section(vauto, accutrade),
        purchase_history_section=_purchase_history_section(purchase_history),
        ml_section=_ml_section(ml_prediction),
        velocity_section=_velocity_section(velocity),
        thalist_asks_section=_thalist_asks_section(thalist_asks),
        asking_constraint=asking_constraint,
    )


def parse_response(raw: str) -> dict | None:
    """Parse Gemini's JSON output. Returns None on failure (caller falls back).
    Handles common formatting quirks: leading/trailing markdown, ``` fences,
    extra commentary before/after the JSON object.
    """
    if not raw:
        return None
    s = raw.strip()
    # Strip ``` code fences
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```\s*$', '', s)
    first = s.find('{')
    if first < 0:
        return None
    last = s.rfind('}')

    # Repair truncated responses (Gemini Pro thinking-token cap): close
    # any open string + balance braces so we can still extract target_buy
    # from a partial JSON. Walk the candidate, track quote/brace state.
    if last < 0 or last <= first:
        body = s[first:]
        in_str, esc, depth = False, False, 0
        for ch in body:
            if esc: esc = False; continue
            if ch == '\\' and in_str: esc = True; continue
            if ch == '"': in_str = not in_str; continue
            if not in_str:
                if ch == '{': depth += 1
                elif ch == '}': depth = max(0, depth - 1)
        repair = body
        if in_str: repair += '"'
        repair += '}' * max(1, depth)
        candidate = repair
    else:
        candidate = s[first:last+1]

    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        candidate2 = re.sub(r',\s*([}\]])', r'\1', candidate)
        try:
            obj = json.loads(candidate2)
        except Exception:
            return None

    # Validate & coerce
    target = obj.get('target_buy')
    cl     = obj.get('confidence_low')
    ch     = obj.get('confidence_high')
    reasoning = obj.get('reasoning', '')
    flags  = obj.get('flags', [])

    def _to_int(x):
        if x is None: return None
        if isinstance(x, (int, float)): return int(x)
        if isinstance(x, str):
            digits = re.sub(r'[^\d-]', '', x)
            return int(digits) if digits else None
        return None

    target = _to_int(target)
    cl     = _to_int(cl)
    ch     = _to_int(ch)
    if target is None or target <= 0:
        return None
    # Snap range if inverted
    if cl is not None and ch is not None and cl > ch:
        cl, ch = ch, cl
    if cl is not None and target < cl:
        cl = target
    if ch is not None and target > ch:
        ch = target

    if not isinstance(flags, list):
        flags = [str(flags)] if flags else []
    flags = [str(f).strip().lower().replace(' ', '_') for f in flags if f][:10]

    return {
        'target_buy':       target,
        'confidence_low':   cl,
        'confidence_high':  ch,
        'reasoning':        str(reasoning).strip()[:1500],
        'flags':            flags,
    }
