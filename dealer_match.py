"""
dealer_match.py — Cross-check a bid against our partner-dealer network.

Pulls from the Dealer DB tables (dealers, dealer_inventory, dealer_sold_signals,
dealer_inventory_history) to answer three questions for every bid:

  1. Is this exact / similar vehicle CURRENTLY LISTED at any partner dealer?
     (tells us the retail market ceiling + where a same unit sits)

  2. Have partner dealers SOLD this vehicle recently — and how fast?
     (turnover pattern — "sold 2 in 60 days, avg 3 days on lot" = strong demand)

  3. Which dealers are the best PITCH CANDIDATES if we buy this car?
     (ranked by historical sales of like-vehicles × turnover speed × current stock)

Returns a structured dict ready to render into the bid-card UI and inject as
context into the AI assessment prompt.
"""

from __future__ import annotations
from typing import Any


DEFAULT_DEALER_MATCH_CONFIG = {
    # How many years either side of the bid's year to consider a "like vehicle"
    "year_tolerance": 2,
    # Window for "recent sales" stat (also used by pitch scoring)
    "recent_days": 90,
    # Minimum confidence on a sold-signal to count it as a real sale
    "min_sold_confidence": 0.70,
    # Weights for the pitch-score formula — tunable in admin UI:
    #   score = sold_count*sold_mult + min(fast_bonus_max, max(0, 30 - avg_days_to_sell))
    #         + active_count*active_mult
    "pitch_weights": {
        "sold_count_multiplier":   10,
        "fast_turnover_bonus_max": 30,
        "active_count_multiplier":  2,
    },
    # Max rows returned per section (UI + prompt budget)
    "max_active":   5,
    "max_sales":   10,
    "max_pitch":    3,
}


def _cfg(config, key, default):
    if not config:
        return default
    dm = config.get('dealer_match') or {}
    return dm.get(key, default)


def find_dealer_matches(db_conn, year, make, model,
                        trim=None, trim_confidence='low', config=None):
    """Query partner dealer data for like-vehicles to the bid.

    trim_confidence semantics:
      - 'deterministic' / 'high' — require trim similarity (first word of bid's
        trim must appear in the dealer_inventory trim). Prevents a base Cayenne
        from matching against Cayenne Turbo GT.
      - 'medium' / 'low' / None — model-only match (current legacy behavior).

    Returns structure unchanged; adds 'match_strategy' to config_used for UI.

    Never raises: returns empty structure on any DB error.
    """
    empty_result = {
        'active': [], 'recent_sales': [], 'patterns': [],
        'top_pitch': [], 'config_used': {}
    }

    if not (year and make and model):
        return empty_result

    year_tol = int(_cfg(config, 'year_tolerance', 2))
    recent_days = int(_cfg(config, 'recent_days', 90))
    min_conf = float(_cfg(config, 'min_sold_confidence', 0.7))
    weights = _cfg(config, 'pitch_weights',
                   DEFAULT_DEALER_MATCH_CONFIG['pitch_weights'])
    sold_mult = float(weights.get('sold_count_multiplier', 10))
    fast_bonus = float(weights.get('fast_turnover_bonus_max', 30))
    active_mult = float(weights.get('active_count_multiplier', 2))
    max_active = int(_cfg(config, 'max_active', 5))
    max_sales = int(_cfg(config, 'max_sales', 10))
    max_pitch = int(_cfg(config, 'max_pitch', 3))

    make_u = str(make).strip().upper()
    model_u = str(model).strip().upper()
    y = int(year)
    y_lo, y_hi = y - year_tol, y + year_tol

    # Trim-similarity gate — only applied when bid's trim_confidence is high.
    # Extract the first significant word of the bid's trim (lowercase) to
    # require presence in the dealer_inventory trim. Examples:
    #   bid trim "Cayenne (base)" → key_word = "cayenne"  (too common, falls
    #     through to model-only match — correct; base trim has no identifier)
    #   bid trim "Cayenne Turbo GT" → key_word = "turbo"
    #   bid trim "Carrera S (Coupe)" → key_word = "carrera" or "s"
    # Choose the MOST DIFFERENTIATING token: skip the model name itself + common
    # fillers. If no differentiating token remains, fall back to model match.
    trim_gate_sql = ''
    trim_gate_args = []
    use_trim_gate = str(trim_confidence or '').lower() in ('deterministic', 'high')
    match_strategy = 'model_only'
    if use_trim_gate and trim:
        # Strip parens, split by non-letter, drop the model name + tiny stopwords
        import re as _re
        cleaned = _re.sub(r'[()]', ' ', str(trim)).lower()
        tokens = [t for t in _re.split(r'[^a-z0-9]+', cleaned) if t]
        stop = {'coupe', 'cabriolet', 'sedan', 'suv', 'base',
                model_u.lower(), make_u.lower()}
        diff_tokens = [t for t in tokens if t not in stop and len(t) >= 2]
        if diff_tokens:
            # Match any of the differentiating tokens in dealer_inventory.trim
            trim_gate_sql = (" AND ({})"
                             .format(" OR ".join(
                                 ["LOWER(COALESCE(di.trim,'')) LIKE %s"] * len(diff_tokens))))
            trim_gate_args = [f'%{t}%' for t in diff_tokens]
            match_strategy = f'trim_tokens:{",".join(diff_tokens)}'
        else:
            # Trim reduced to model+stopwords (e.g. "Cayenne (base)") →
            # explicitly exclude dealer_inventory rows whose trim contains any
            # premium markers (Turbo, GT, S, AMG, RS) so base doesn't match S.
            premium_markers = ['turbo', 'gts', 'gt3', 'gt4', ' gt ',
                               'hybrid', 'amg', ' rs ', 's e-hybrid']
            trim_gate_sql = (" AND NOT (" + " OR ".join(
                ["LOWER(COALESCE(di.trim,'')) LIKE %s"] * len(premium_markers)) + ")")
            trim_gate_args = [f'%{m}%' for m in premium_markers]
            match_strategy = 'base_trim_exclusion'

    try:
        cur = db_conn.cursor()

        # ── A. Active listings of like-vehicles at partner dealers ──────────
        cur.execute(f"""
            SELECT di.id, di.dealer_id, d.name AS dealer_name, d.city, d.state,
                   di.year, di.make, di.model, di.trim,
                   di.price, di.mileage, di.url, di.photo_url,
                   di.first_seen_at, di.last_seen_at,
                   di.price_drop_amount, di.price_drop_at,
                   EXTRACT(EPOCH FROM (NOW() - di.first_seen_at)) / 86400.0 AS days_on_lot
            FROM dealer_inventory di
            JOIN dealers d ON di.dealer_id = d.id
            WHERE di.status = 'active'
              AND d.active = TRUE
              AND UPPER(di.make) = %s
              AND UPPER(di.model) = %s
              AND di.year BETWEEN %s AND %s
              {trim_gate_sql}
            ORDER BY di.first_seen_at ASC
            LIMIT %s
        """, tuple([make_u, model_u, y_lo, y_hi] + trim_gate_args + [max_active]))
        active_rows = []
        for r in cur.fetchall():
            d = dict(r)
            dol = d.get('days_on_lot')
            d['days_on_lot'] = int(dol) if dol is not None else None
            pd = d.get('price_drop_at')
            if pd:
                import datetime
                try:
                    age = (datetime.datetime.now(pd.tzinfo) - pd).days
                    d['price_drop_days_ago'] = age
                except Exception:
                    d['price_drop_days_ago'] = None
            else:
                d['price_drop_days_ago'] = None
            for k in ('first_seen_at', 'last_seen_at', 'price_drop_at'):
                if d.get(k) and hasattr(d[k], 'isoformat'):
                    d[k] = d[k].isoformat()
            active_rows.append(d)

        # ── B. Recent sales of like-vehicles in last N days ─────────────────
        cur.execute(f"""
            SELECT dss.dealer_id, d.name AS dealer_name,
                   di.year, di.make, di.model, di.trim, di.price, di.mileage,
                   dss.detected_at, dss.signal_type, dss.confidence,
                   di.first_seen_at,
                   EXTRACT(EPOCH FROM (dss.detected_at - di.first_seen_at)) / 86400.0
                       AS days_to_sell
            FROM dealer_sold_signals dss
            JOIN dealer_inventory di ON dss.inventory_id = di.id
            JOIN dealers d ON dss.dealer_id = d.id
            WHERE UPPER(di.make) = %s
              AND UPPER(di.model) = %s
              AND di.year BETWEEN %s AND %s
              AND dss.detected_at > NOW() - (%s || ' days')::interval
              AND dss.confidence >= %s
              AND d.active = TRUE
              {trim_gate_sql}
            ORDER BY dss.detected_at DESC
            LIMIT %s
        """, tuple([make_u, model_u, y_lo, y_hi, recent_days, min_conf] +
                   trim_gate_args + [max_sales]))
        sales_rows = []
        for r in cur.fetchall():
            d = dict(r)
            dts = d.get('days_to_sell')
            d['days_to_sell'] = int(dts) if dts is not None else None
            for k in ('detected_at', 'first_seen_at'):
                if d.get(k) and hasattr(d[k], 'isoformat'):
                    d[k] = d[k].isoformat()
            sales_rows.append(d)

        # ── C. Pitch-score aggregation per dealer ───────────────────────────
        # Trim gate in the CTE uses 'di' alias for both inline subqueries.
        # The alias `dealer_inventory` in the actives CTE doesn't have 'di'
        # prefix — need to substitute. For simplicity, only apply gate when
        # the clause is non-empty.
        actives_trim_gate = trim_gate_sql.replace('di.trim', 'trim')
        cur.execute(f"""
            WITH sales AS (
              SELECT dss.dealer_id,
                     COUNT(*) AS sold_count,
                     AVG(EXTRACT(EPOCH FROM (dss.detected_at - di.first_seen_at)) / 86400.0)
                       AS avg_days_to_sell,
                     MAX(dss.detected_at) AS last_sold_at
              FROM dealer_sold_signals dss
              JOIN dealer_inventory di ON dss.inventory_id = di.id
              WHERE UPPER(di.make) = %s
                AND UPPER(di.model) = %s
                AND di.year BETWEEN %s AND %s
                AND dss.detected_at > NOW() - (%s || ' days')::interval
                AND dss.confidence >= %s
                {trim_gate_sql}
              GROUP BY dss.dealer_id
            ),
            actives AS (
              SELECT dealer_id, COUNT(*) AS active_count
              FROM dealer_inventory
              WHERE UPPER(make) = %s
                AND UPPER(model) = %s
                AND year BETWEEN %s AND %s
                AND status = 'active'
                {actives_trim_gate}
              GROUP BY dealer_id
            )
            SELECT d.id AS dealer_id, d.name AS dealer_name, d.city, d.state,
                   COALESCE(s.sold_count, 0)::int  AS sold_count,
                   ROUND(COALESCE(s.avg_days_to_sell, 0)::numeric)::int
                     AS avg_days_to_sell,
                   COALESCE(a.active_count, 0)::int AS active_count,
                   s.last_sold_at
            FROM dealers d
            LEFT JOIN sales   s ON s.dealer_id = d.id
            LEFT JOIN actives a ON a.dealer_id = d.id
            WHERE d.active = TRUE
              AND (s.sold_count IS NOT NULL OR a.active_count IS NOT NULL)
        """, tuple([make_u, model_u, y_lo, y_hi, recent_days, min_conf] +
                   trim_gate_args +
                   [make_u, model_u, y_lo, y_hi] +
                   trim_gate_args))
        pattern_rows = []
        for r in cur.fetchall():
            d = dict(r)
            if d.get('last_sold_at') and hasattr(d['last_sold_at'], 'isoformat'):
                d['last_sold_at'] = d['last_sold_at'].isoformat()
            pattern_rows.append(d)

        # ── Pitch score + reason (Python side, easier to tune) ──────────────
        scored = []
        for p in pattern_rows:
            sc = p['sold_count']
            avg = p['avg_days_to_sell']
            ac = p['active_count']
            fast_bonus_pts = max(0.0, min(fast_bonus, 30.0 - avg)) if (sc > 0 and avg > 0) else 0.0
            score = sc * sold_mult + fast_bonus_pts + ac * active_mult
            # Reason one-liner for UI
            bits = []
            if sc > 0 and avg > 0:
                bits.append(f"{sc} sold in last {recent_days}d, avg {avg}d to sell")
            elif sc > 0:
                bits.append(f"{sc} sold in last {recent_days}d")
            if ac > 0:
                bits.append(f"{ac} active now")
            if sc == 0 and ac == 0:
                bits.append("no recent history")
            reason = " · ".join(bits)
            p2 = dict(p)
            p2['pitch_score'] = round(score, 1)
            p2['reason'] = reason
            scored.append(p2)
        scored.sort(key=lambda x: (-x['pitch_score'], -x['sold_count'], x['avg_days_to_sell'] or 999))
        top_pitch = [s for s in scored if s['pitch_score'] > 0][:max_pitch]

        return {
            'active':       active_rows,
            'recent_sales': sales_rows,
            'patterns':     pattern_rows,
            'top_pitch':    top_pitch,
            'config_used': {
                'year_tolerance': year_tol,
                'recent_days': recent_days,
                'min_sold_confidence': min_conf,
                'pitch_weights': weights,
                'match_strategy': match_strategy,
                'trim_confidence': trim_confidence,
            },
        }
    except Exception as e:
        print(f'find_dealer_matches error: {e}', flush=True)
        return empty_result


# ── Prompt builder (feeds LLM) ───────────────────────────────────────────────

def format_for_prompt(matches, max_sample_lines=8):
    """Render dealer matches as a plain-text block for inclusion in the
    Gemini assessment prompt. Returns empty string when no matches."""
    if not matches:
        return ''
    active = matches.get('active') or []
    sales  = matches.get('recent_sales') or []
    patterns = matches.get('patterns') or []
    pitch = matches.get('top_pitch') or []
    cfg = matches.get('config_used') or {}

    if not active and not sales and not pitch:
        return ''

    lines = ['═══ DEALER NETWORK INTEL (live data from scanned partner dealers) ═══']

    if active:
        lines.append('\nCURRENTLY LISTED AT PARTNER DEALERS:')
        for a in active[:max_sample_lines]:
            dol = a.get('days_on_lot')
            dol_str = f'{dol} days on lot' if dol is not None else 'days unknown'
            price = a.get('price')
            price_str = f'${price:,}' if price else 'no price'
            drop = ''
            if a.get('price_drop_amount') and a.get('price_drop_days_ago') is not None:
                drop = f' (price dropped ${a["price_drop_amount"]:,} {a["price_drop_days_ago"]}d ago)'
            loc = f'{a.get("city") or ""}, {a.get("state") or ""}'.strip(', ')
            ymm = ' '.join(str(x) for x in [a.get('year'), a.get('make'),
                                             a.get('model'), a.get('trim') or '']).strip()
            lines.append(f'  • {a.get("dealer_name")} ({loc}): {ymm} · {price_str} · {dol_str}{drop}')

    if patterns:
        # Aggregate lines for readability (one per dealer)
        sold_present = [p for p in patterns if p.get('sold_count', 0) > 0]
        if sold_present:
            lines.append(f'\nRECENT SALES (last {cfg.get("recent_days", 90)} days):')
            for p in sold_present[:max_sample_lines]:
                sold = p['sold_count']
                avg = p['avg_days_to_sell']
                lines.append(f'  • {p["dealer_name"]}: {sold} sold · avg {avg} days on lot')

    if pitch:
        lines.append('\nPITCH CANDIDATES (ranked by fit × turnover × stock):')
        for i, tp in enumerate(pitch, 1):
            lines.append(f'  {i}. {tp["dealer_name"]} — {tp["reason"]}')

    lines.append(
        '\nUse this to inform your percentage adjustment:\n'
        '  - Fast turnover at a partner dealer = stronger baseline confidence → smaller downward adjustment\n'
        '  - Slow turnover / heavy stock of this model → more caution → larger downward adjustment\n'
        '  - Active listings reveal retail ceiling; our wholesale bid must leave room for partner markup'
    )

    return '\n'.join(lines)
