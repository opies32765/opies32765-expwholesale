"""
lsl_buyer_match.py — same-Make/Model deals lookup against the EW sales ledger.

Reads /opt/livesaleslog/crm.db (mirror of LSL's /deal/list, EW dealerId=10010)
and returns RAW deal rows for the bid's year/make/model band, plus a ranked
list of "likely buyer candidates" derived from per-customer aggregation +
dealer_profile context.

Field names mirror LSL's `/deal/ListHeatSheet` schema verbatim
(snake_cased per the extract.py convention).  Internal pitch-scoring
helpers compute `pitch_score`, `bucket` (HOT/WARM/COLD comes from
dealer_profile), and a one-liner `reason` so the bid.html card can
render "Top buyer candidates" without re-doing the ranking in Jinja.

Sibling to dealer_match.py (partner-dealer scraped retail inventory).

Tables used:
  - deals — every EW transaction (28K+ rows, $29.4M lifetime, 5 years)
  - dealer_profile — per-counterparty aggregates (bucket, lifetime + 12mo
    + 90d revenue/profit/deal counts) — LEFT JOINed so missing rows just
    yield NULL contact fields (handled by the template).
"""

from __future__ import annotations
import os
import sqlite3
from typing import Any


LSL_DB_PATH = os.environ.get('LSL_DB_PATH', '/opt/livesaleslog/crm.db')


DEFAULT_LSL_BUYER_MATCH_CONFIG = {
    "year_tolerance": 0,
    "recent_days": 365,
    "max_deals": 10,
    # Recency window for the "very recent" 90d sub-count used by pitch
    # scoring + the prompt's "X in 90d" reason bit.
    "very_recent_days": 90,
    # Pitch score weights (tunable in /admin/ai-levers)
    "pitch_weights": {
        "deals_12mo_multiplier": 10,    # raw count of like-deals in 12mo
        "deals_90d_bonus":        5,    # extra weight for very recent deals
        "recency_bonus_max":     20,    # bonus for last-purchase recency (decays after 60d)
        "gross_per_1k":           1.0,  # avg gross / $1k contribution
        "lifetime_bonus_max":    10,    # bonus for high-lifetime-volume buyers (HOT bucket)
    },
    # Output limits for the buyer-candidate ranking
    "max_recent_buyers":  10,           # for prompt + UI long-tail list
    "max_pitch":           5,           # top-N ranked candidates
    # Min like-deals in 12mo to even be considered for pitch ranking
    "min_deals_for_pitch": 1,
}


def _cfg(config, key, default):
    if not config:
        return default
    lm = config.get('lsl_buyer_match') or {}
    return lm.get(key, default)


def _norm_make_pattern(make: str) -> str:
    if not make:
        return ''
    return str(make).strip().replace('-', '%').replace('_', '%')


# Body-style synonyms for LSL vehicle_info matching. LSL uses verbose body
# strings ("Convertible", "2D Coupe", "Sport Utility Vehicle"); bid.trim
# may use OEM terminology ("Cabriolet", "Suv", "Sedan"). Map them so we can
# exclude wrong-body comps (e.g. don't show 911 Coupes when subject is a
# 911 Cabriolet).
_BODY_STYLE_GROUPS = [
    # Convertibles: Cabriolet (Porsche/Audi/BMW), Spider/Spyder, Roadster,
    # Convertible
    ('convertible', ['cabriolet', 'convertible', 'spider', 'spyder', 'roadster']),
    ('coupe',       ['coupe', '2d coupe']),
    ('targa',       ['targa']),
    ('sedan',       ['sedan', '4d sedan']),
    ('suv',         ['suv', 'sport utility', 'crossover']),
    ('hatchback',   ['hatchback']),
    ('wagon',       ['wagon']),
    ('truck',       ['pickup', 'crew cab', 'extended cab', 'regular cab',
                     'super cab', 'super crew', 'quad cab']),
    ('van',         ['cargo van', 'passenger van', 'minivan']),
]


def _body_style_patterns(trim_text: str | None) -> list[str]:
    """Detect the bid's body style from trim/notes and return LIKE patterns
    that match equivalent LSL `vehicle_info` strings. Empty list when nothing
    detectable — caller should NOT add a body filter in that case."""
    if not trim_text:
        return []
    t = trim_text.lower()
    for _key, synonyms in _BODY_STYLE_GROUPS:
        for s in synonyms:
            if s in t:
                # Return ALL synonyms for this group as wildcard LIKE patterns
                return [f'%{x}%' for x in synonyms]
    return []


# Stopwords / body-style-derived tokens we should NOT enforce as trim filters
# (single-letter packages like "S", drive-train acronyms, body-style words).
_TRIM_STOPWORDS = {
    'awd', 'fwd', 'rwd', '4wd', '2wd', '4x4', '4x2',
    'crew', 'cab', 'extended', 'regular', 'super', 'quad', 'pickup', 'truck',
    'sedan', 'coupe', 'convertible', 'cabriolet', 'spider', 'spyder',
    'roadster', 'targa', 'wagon', 'hatchback', 'suv', 'sport', 'utility',
    'crossover', 'minivan', 'van',
    '2d', '4d', 'auto', 'manual', 'and', 'with', 'edition', 'package', 'pkg',
}


def _trim_filter_token(trim_text: str | None) -> str | None:
    """Pick the most distinguishing trim word (e.g. 'XLT', 'Raptor', 'Carrera',
    'Lariat') so LSL matches a same-trim deal — not a Raptor when subject is
    an XLT.  Returns the first non-stopword, non-body-style token of length
    >=3 from the trim string, or None if nothing usable."""
    if not trim_text:
        return None
    for raw in trim_text.replace('-', ' ').replace('/', ' ').split():
        token = raw.strip().strip('.,').lower()
        if len(token) < 2:
            continue
        if token in _TRIM_STOPWORDS:
            continue
        # 2-letter tokens only kept if upper-case (likely an actual trim like
        # 'GT', 'SS', 'RS', 'XL'). Single letter ('S') is too noisy.
        if len(token) == 2 and not raw.isupper():
            continue
        return token
    return None


def _open_lsl_ro():
    uri = f'file:{LSL_DB_PATH}?mode=ro&immutable=1'
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def find_same_ymm_deals(year, make, model, mileage=None, config=None, trim=None) -> dict:
    """Return raw EW deal rows + per-buyer candidate ranking for the bid's
    year/make/model band.

    Output:
      {
        'deals':            [...],   # raw deal rows, LSL field names
        'patterns':         {...},   # count, unique buyers, avg sale, avg gross
                                     #   plus 'windows' (3/6/12 mo) + total_deals_90d
        'recent_buyers':    [...],   # all distinct buyers in 12mo with stats
        'top_pitch_buyers': [...],   # top-N ranked with reason strings
        'config_used':      {...},
      }
    Never raises — empty structure on any error.
    """
    empty = {
        'deals': [], 'patterns': {},
        'recent_buyers': [], 'top_pitch_buyers': [],
        'config_used': {},
    }
    if not (year and make and model):
        return empty

    year_tol = int(_cfg(config, 'year_tolerance', 0))
    recent_days = int(_cfg(config, 'recent_days', 365))
    max_deals = int(_cfg(config, 'max_deals', 10))
    very_recent = int(_cfg(config, 'very_recent_days', 90))
    weights = _cfg(config, 'pitch_weights',
                   DEFAULT_LSL_BUYER_MATCH_CONFIG['pitch_weights'])
    max_recent = int(_cfg(config, 'max_recent_buyers', 10))
    max_pitch = int(_cfg(config, 'max_pitch', 5))
    min_deals = int(_cfg(config, 'min_deals_for_pitch', 1))

    make_pat = _norm_make_pattern(make)
    model_pat = f'%{str(model).strip()}%'
    try:
        y = int(year)
    except (ValueError, TypeError):
        return empty
    y_lo, y_hi = y - year_tol, y + year_tol
    year_prefix_clause = ' OR '.join(["vehicle_info LIKE ?" for _ in range(y_lo, y_hi + 1)])
    year_prefix_args = [f'{yp} %' for yp in range(y_lo, y_hi + 1)]

    # Body-style filter — keep only same-body comps so a 911 Cabriolet bid
    # doesn't get matched to 911 Coupes / Targas / Turbo S etc.
    body_patterns = _body_style_patterns(trim)
    if body_patterns:
        body_clause = '(' + ' OR '.join(['UPPER(vehicle_info) LIKE UPPER(?)' for _ in body_patterns]) + ')'
        body_args = body_patterns
    else:
        body_clause = '1=1'
        body_args = []

    # Trim filter — F-150 XLT shouldn't match F-150 Raptor/Tremor/Limited.
    # Pick the most distinguishing trim word and require it in vehicle_info.
    trim_tok = _trim_filter_token(trim)
    if trim_tok:
        trim_clause = 'UPPER(vehicle_info) LIKE UPPER(?)'
        trim_args = [f'%{trim_tok}%']
    else:
        trim_clause = '1=1'
        trim_args = []

    try:
        conn = _open_lsl_ro()
        cur = conn.cursor()

        sql_deals = f"""
        SELECT
            stock_no, vin_no, vehicle_info, make_name,
            customer_name, supplier_name, sales_person, sales_manager,
            sale_type, vehicle_sale_type, status, type,
            sale_price, purchase_cost, front_value, total_fi,
            total_supp_costs, recon_cost, deal_total_value,
            transport_fee, inventory_pack, mcd_live_fee, broker_fee,
            buyer_fee, sell_fee, write_down, referral_fee,
            days_on_lot, days_since_purchase, days_since_booked,
            sold_at, promise_date, delivery_date, modified_at
        FROM deals
        WHERE UPPER(make_name) LIKE UPPER(?)
          AND UPPER(vehicle_info) LIKE UPPER(?)
          AND ({year_prefix_clause})
          AND {body_clause}
          AND {trim_clause}
          AND sold_at > date('now', ? || ' days')
          AND sale_price > 0
          AND customer_name IS NOT NULL AND TRIM(customer_name) != ''
        ORDER BY sold_at DESC
        LIMIT ?
        """
        cur.execute(sql_deals, (
            make_pat, model_pat, *year_prefix_args, *body_args, *trim_args, f'-{recent_days}', max_deals,
        ))
        deals = [dict(r) for r in cur.fetchall()]

        sql_patterns = f"""
        SELECT
            COUNT(*) AS total_deals,
            COUNT(DISTINCT customer_name) AS unique_buyers,
            ROUND(AVG(sale_price)) AS avg_sale_price,
            ROUND(AVG(front_value)) AS avg_front_value,
            ROUND(AVG(transport_fee)) AS avg_transport_fee,
            ROUND(AVG(sale_price - purchase_cost)) AS avg_sale_minus_cost,
            MIN(sold_at) AS first_sold_at,
            MAX(sold_at) AS last_sold_at
        FROM deals
        WHERE UPPER(make_name) LIKE UPPER(?)
          AND UPPER(vehicle_info) LIKE UPPER(?)
          AND ({year_prefix_clause})
          AND {body_clause}
          AND {trim_clause}
          AND sold_at > date('now', ? || ' days')
          AND sale_price > 0
        """
        cur.execute(sql_patterns,
                    (make_pat, model_pat, *year_prefix_args, *body_args, *trim_args, f'-{recent_days}'))
        patterns = dict(cur.fetchone() or {})

        # 3/6/12-month rolling windows — count + avg sale + avg gross at each.
        # Gemini and the bid.html ledger card both render these so the user
        # can see whether a "12-month avg gross" is being held up by activity
        # in the last 90 days vs ancient deals.
        windows = {}
        for label, days in (('90d', 90), ('180d', 180), ('365d', 365)):
            cur.execute(f"""
                SELECT
                    COUNT(*)                                       AS n,
                    COUNT(DISTINCT customer_name)                  AS unique_buyers,
                    ROUND(AVG(sale_price))                         AS avg_sale_price,
                    ROUND(AVG(front_value))                        AS avg_front_value,
                    ROUND(AVG(sale_price - purchase_cost))         AS avg_sale_minus_cost
                FROM deals
                WHERE UPPER(make_name) LIKE UPPER(?)
                  AND UPPER(vehicle_info) LIKE UPPER(?)
                  AND ({year_prefix_clause})
                  AND {body_clause}
                  AND {trim_clause}
                  AND sold_at > date('now', ? || ' days')
                  AND sale_price > 0
                  AND purchase_cost > 0
                  AND customer_name IS NOT NULL
                  AND TRIM(customer_name) != ''
            """, (make_pat, model_pat, *year_prefix_args, *body_args, *trim_args, f'-{days}'))
            windows[label] = dict(cur.fetchone() or {})
        patterns['windows'] = windows
        # Backwards-compat: keep the old flat key the existing card reads.
        patterns['total_deals_90d'] = (windows.get('90d') or {}).get('n', 0)

        # ── Per-buyer aggregation (12mo) joined with dealer_profile for
        #    lifetime stats + contact info. Powers "Top buyer candidates"
        #    + "Other recent buyers" in the bid card.
        # NOTE: avg_gross here is an internal computation (sale_price -
        # purchase_cost). Kept under that name to match the template's
        # `b.avg_gross` / `r.avg_gross` references.
        sql_recent = f"""
        SELECT
            d.customer_name AS buyer_name,
            COUNT(*) AS deals_window,
            SUM(CASE WHEN d.sold_at > date('now', ? || ' days') THEN 1 ELSE 0 END)
                AS deals_very_recent,
            ROUND(AVG(d.front_value)) AS avg_gross,
            ROUND(AVG(d.sale_price)) AS avg_sale_price,
            MAX(d.sold_at) AS last_sold_at,
            CAST(julianday('now') - julianday(MAX(d.sold_at)) AS INTEGER) AS days_since_last,
            -- dealer_profile lifetime context
            dp.buyer_deals AS lifetime_deals,
            dp.buyer_deals_12mo AS lifetime_deals_12mo,
            dp.buyer_avg_profit AS lifetime_avg_gross,
            dp.bucket AS bucket,
            dp.norm_name AS norm_name,
            dp.active_rep AS active_rep,
            dp.cust_email AS cust_email,
            dp.cust_mobile AS cust_mobile,
            dp.best_phone AS best_phone,
            dp.best_email AS best_email
        FROM deals d
        LEFT JOIN dealer_profile dp ON dp.display_name = d.customer_name
        WHERE UPPER(d.make_name) LIKE UPPER(?)
          AND UPPER(d.vehicle_info) LIKE UPPER(?)
          AND ({year_prefix_clause})
          AND {body_clause.replace('vehicle_info', 'd.vehicle_info')}
          AND {trim_clause.replace('vehicle_info', 'd.vehicle_info')}
          AND d.sold_at > date('now', ? || ' days')
          AND d.sale_price > 0 AND d.purchase_cost > 0
          AND d.customer_name IS NOT NULL AND TRIM(d.customer_name) != ''
        GROUP BY d.customer_name
        ORDER BY deals_window DESC, avg_gross DESC
        LIMIT ?
        """
        cur.execute(sql_recent, (
            f'-{very_recent}',
            make_pat, model_pat,
            *year_prefix_args,
            *body_args,
            *trim_args,
            f'-{recent_days}',
            max_recent,
        ))
        recent_rows = [dict(r) for r in cur.fetchall()]

        # Pull each buyer's individual matching deals so the card can show
        # the exact vehicles they bought from us (not just aggregates).
        buyer_names = [r['buyer_name'] for r in recent_rows]
        if buyer_names:
            placeholders = ','.join('?' * len(buyer_names))
            sql_buyer_deals = f"""
            SELECT customer_name, stock_no, vehicle_info,
                   sale_price, front_value, purchase_cost,
                   sold_at, sale_type
            FROM deals
            WHERE customer_name IN ({placeholders})
              AND UPPER(make_name) LIKE UPPER(?)
              AND UPPER(vehicle_info) LIKE UPPER(?)
              AND ({year_prefix_clause})
              AND {body_clause}
              AND {trim_clause}
              AND sold_at > date('now', ? || ' days')
              AND sale_price > 0 AND purchase_cost > 0
            ORDER BY customer_name, sold_at DESC
            """
            cur.execute(sql_buyer_deals, (
                *buyer_names, make_pat, model_pat,
                *year_prefix_args, *body_args, *trim_args, f'-{recent_days}',
            ))
            deals_by_buyer = {}
            for d in cur.fetchall():
                deals_by_buyer.setdefault(d['customer_name'], []).append({
                    'sold_at':      d['sold_at'],
                    'stock_no':     d['stock_no'],
                    'vehicle_info': d['vehicle_info'],
                    'sale_price':   d['sale_price'],
                    'front_value':  d['front_value'],
                    'sale_type':    d['sale_type'],
                })
            for r in recent_rows:
                r['deals'] = deals_by_buyer.get(r['buyer_name'], [])

        conn.close()

        # ── Pitch scoring (Python side, easier to tune) ──────────────────
        deals_12mo_mult = float(weights.get('deals_12mo_multiplier', 10))
        deals_90d_bonus = float(weights.get('deals_90d_bonus', 5))
        recency_max     = float(weights.get('recency_bonus_max', 20))
        gross_per_1k    = float(weights.get('gross_per_1k', 1.0))
        lifetime_max    = float(weights.get('lifetime_bonus_max', 10))

        scored = []
        for r in recent_rows:
            n_window = int(r.get('deals_window') or 0)
            if n_window < min_deals:
                continue
            n_90d = int(r.get('deals_very_recent') or 0)
            avg_gross = float(r.get('avg_gross') or 0)
            avg_paid = float(r.get('avg_sale_price') or 0)
            days_since = r.get('days_since_last')
            lifetime_12mo = int(r.get('lifetime_deals_12mo') or 0)

            # Recency bonus: full credit for last 30d, decays linearly to 0 at 180d
            if days_since is None:
                rec_bonus = 0.0
            elif days_since <= 30:
                rec_bonus = recency_max
            elif days_since >= 180:
                rec_bonus = 0.0
            else:
                rec_bonus = recency_max * (180 - days_since) / 150.0

            # Lifetime bonus: linear up to 100 deals/12mo (HOT cap)
            if lifetime_12mo <= 0:
                life_bonus = 0.0
            else:
                life_bonus = lifetime_max * min(1.0, lifetime_12mo / 100.0)

            score = (
                n_window * deals_12mo_mult
                + n_90d   * deals_90d_bonus
                + rec_bonus
                + (avg_gross / 1000.0) * gross_per_1k
                + life_bonus
            )

            # Reason one-liner — appears in UI + prompt.
            bits = [f"{n_window} deals 12mo"]
            if n_90d:
                bits.append(f"{n_90d} in 90d")
            if avg_paid and avg_gross:
                bits.append(f"paid avg ${int(avg_paid):,} · gross ${int(avg_gross):,}")
            elif avg_gross:
                bits.append(f"avg gross ${int(avg_gross):,}")
            elif avg_paid:
                bits.append(f"paid avg ${int(avg_paid):,}")
            if days_since is not None:
                if days_since <= 7:
                    bits.append(f"last {days_since}d ago — HOT")
                else:
                    bits.append(f"last {days_since}d ago")
            if lifetime_12mo:
                bits.append(f"{lifetime_12mo}/yr lifetime")
            if r.get('bucket'):
                bits.append(r['bucket'])

            r2 = dict(r)
            r2['pitch_score'] = round(score, 1)
            r2['reason'] = ' · '.join(bits)
            scored.append(r2)

        scored.sort(key=lambda x: -x['pitch_score'])
        top_pitch = scored[:max_pitch]

        return {
            'deals':            deals,
            'patterns':         patterns,
            'recent_buyers':    recent_rows,
            'top_pitch_buyers': top_pitch,
            'config_used': {
                'year_tolerance':   year_tol,
                'recent_days':      recent_days,
                'very_recent_days': very_recent,
                'pitch_weights':    weights,
                'make_pattern':     make_pat,
                'model_pattern':    model_pat,
            },
        }
    except Exception as e:
        print(f'lsl_buyer_match error: {type(e).__name__}: {e}', flush=True)
        return empty


# Backwards-compat alias for app.py imports.
find_lsl_buyers = find_same_ymm_deals


# ── Prompt builder ──────────────────────────────────────────────────────

def format_for_prompt(matches, max_lines=10):
    """Render LSL deal history as a plain-text prompt block, using LSL's
    exact field labels.  Empty string when there's no useful signal."""
    if not matches:
        return ''
    patterns = matches.get('patterns') or {}
    deals = matches.get('deals') or []

    total = int(patterns.get('total_deals') or 0)
    if not (total or deals):
        return ''

    lines = ['═══ EW SALES LEDGER — same Make/Model deals ═══']

    windows = patterns.get('windows') or {}
    win_labels = (('90d', '3 mo'), ('180d', '6 mo'), ('365d', '12 mo'))
    if any((windows.get(k) or {}).get('n') for k, _ in win_labels):
        lines.append('\nROLLING WINDOWS (count · avg Sale · avg Front Value):')
        for k, lab in win_labels:
            w = windows.get(k) or {}
            n = int(w.get('n') or 0)
            sp = int(w.get('avg_sale_price') or 0)
            fv = int(w.get('avg_front_value') or 0)
            uq = int(w.get('unique_buyers') or 0)
            if n:
                lines.append(
                    f'  Last {lab:>5}: {n:>3} deals · {uq} customers · '
                    f'avg Sale ${sp:,} · avg Front Value ${fv:,}'
                )
            else:
                lines.append(f'  Last {lab:>5}:   0 deals')

    if deals:
        lines.append('\nDeals (Sold At · Stock No · Vehicle Info · Customer · Supplier · Sale Price · Front Value · Total F&I · Sale Type):')
        for d in deals[:max_lines]:
            sold = (d.get('sold_at') or '')[:10]
            stock = d.get('stock_no') or ''
            vinfo = d.get('vehicle_info') or ''
            cust = d.get('customer_name') or ''
            sup = d.get('supplier_name') or ''
            sp = int(d.get('sale_price') or 0)
            fv = int(d.get('front_value') or 0)
            tfi = int(d.get('total_fi') or 0)
            stype = d.get('sale_type') or ''
            lines.append(
                f'  • {sold} · {stock} · {vinfo} · {cust} · {sup} · '
                f'${sp:,} · ${fv:,} · ${tfi:,} · {stype}'
            )

    return '\n'.join(lines)
