"""dealer_match_v2.py — tiered like-vehicle matcher for the EW Dealer Network card.

REPLACES the body of `find_dealer_matches()` in `/opt/expwholesale/dealer_match.py`.
Same function signature → callers stay unchanged.

WHAT'S DIFFERENT FROM v1:
  1. Drops the ±2 year band — Year is EXACT.
  2. Trim equality is ALWAYS enforced (not optional based on trim_confidence).
     - Bid trim missing → only match dealer rows where trim is NULL/blank/'base'.
     - Bid trim present → exact match after body-word stripping.
  3. Uses canonical YMMT columns when both sides are decoded:
       Tier 1: bid.canon_* == dealer.nhtsa_*  (trim included)
       Tier 2: same Y/M/M (drop trim — confidence drop)
       Tier 3: free-text fallback (legacy v1 behavior, body-stripped)
     First tier with ≥1 match wins. Each result row carries `match_tier`.

Returns the SAME dict shape as v1 with added `match_tier` per row + summary
`tier_used`/`tier_confidence` in the top-level result.
"""
from __future__ import annotations
import re
from typing import Any


# Body-class words to strip from trim string before comparison. Trims like
# "Carrera S Coupe" should equal "Carrera S" stored elsewhere as the trim
# without body suffix. Order matters — longer first.
_BODY_NOISE = [
    'sport utility vehicle', 'pickup truck truck', 'pickup truck',
    'sport utility', 'crew cab', 'quad cab', 'extended cab', 'regular cab',
    'cabriolet', 'convertible', 'roadster', 'hatchback', 'targa',
    'spider', 'spyder', 'coupe', 'sedan', 'wagon', 'minivan', 'van',
    'truck', 'suv',
    '4 door', '4 dr.', '4 dr', '2 door', '2 dr.', '2 dr', '4d', '2d',
    '4matic', '4 matic', 'xdrive', 'quattro', '4wd', 'awd', 'rwd', 'fwd',
]

# Trim values that should be treated as "base / no trim" — both sides match.
_BASE_TRIM_TOKENS = {'', 'base', 'standard', 'std', 'l', 'le'}


def _norm_trim(s: str | None) -> str:
    """Strip body-class noise + lowercase + collapse whitespace.
    Empty/None/base sentinel → '' (so two 'base' bids match)."""
    if not s:
        return ''
    s = s.lower().strip()
    # Strip body words from end (greedy, longest first)
    changed = True
    while changed:
        changed = False
        for noise in _BODY_NOISE:
            if s.endswith(' ' + noise):
                s = s[: -(len(noise) + 1)].rstrip()
                changed = True
                break
            if s == noise:
                s = ''
                changed = True
                break
    s = re.sub(r'\s+', ' ', s).strip()
    if s in _BASE_TRIM_TOKENS:
        return ''
    return s


def _norm_text(s: str | None) -> str:
    if not s: return ''
    return re.sub(r'\s+', ' ', str(s).upper().strip())


# ── Tiered queries ─────────────────────────────────────────────────────────

_AGE_EXPR = ("COALESCE("
             "di.verified_at - (di.verified_days_on_lot || ' days')::interval, "
             "di.source_added_at, di.first_seen_at)")

_DAYS_ON_LOT_EXPR = (
    f"GREATEST(0, ((NOW() AT TIME ZONE 'America/New_York')::date "
    f"- ({_AGE_EXPR} AT TIME ZONE 'America/New_York')::date)::int)")

_PRICE_DROP_DAYS_AGO_EXPR = (
    "CASE WHEN di.price_drop_at IS NOT NULL THEN "
    "GREATEST(0, ((NOW() AT TIME ZONE 'America/New_York')::date "
    "- (di.price_drop_at AT TIME ZONE 'America/New_York')::date)::int) "
    "END")


def _select_active(extra_where: str, params: list, max_active: int) -> tuple:
    sql = f"""
        SELECT di.id, di.dealer_id, d.name AS dealer_name, d.city, d.state,
               di.vin, di.year, di.make, di.model, di.trim,
               di.price, di.mileage, di.url, di.photo_url,
               di.first_seen_at, di.last_seen_at,
               di.source_added_at, di.verified_at, di.verified_days_on_lot,
               di.price_drop_amount, di.price_drop_at,
               {_PRICE_DROP_DAYS_AGO_EXPR} AS price_drop_days_ago,
               {_DAYS_ON_LOT_EXPR} AS days_on_lot
        FROM dealer_inventory di
        JOIN dealers d ON di.dealer_id = d.id
        WHERE di.status = 'active'
          AND d.active = TRUE
          AND {extra_where}
        ORDER BY {_AGE_EXPR} ASC
        LIMIT %s
    """
    return sql, params + [max_active]


# Tier 1 — full canonical match (year + make + model + trim). Both sides
# require nhtsa_* (dealer) and canon_* (bid) populated. Strictest tier.
def _tier1_canon_full(year, make, model, trim):
    where = """di.nhtsa_year IS NOT NULL
               AND di.nhtsa_year = %s
               AND UPPER(di.nhtsa_make) = %s
               AND UPPER(di.nhtsa_model) = %s
               AND COALESCE(LOWER(di.nhtsa_trim), '') = %s"""
    params = [year, _norm_text(make), _norm_text(model), _norm_trim(trim)]
    return where, params


# Tier 2 — canonical Y/M/M only (trim dropped).
# Used when bid has no trim OR trim disagreement is suspected.
def _tier2_canon_ymm(year, make, model):
    where = """di.nhtsa_year IS NOT NULL
               AND di.nhtsa_year = %s
               AND UPPER(di.nhtsa_make) = %s
               AND UPPER(di.nhtsa_model) = %s"""
    params = [year, _norm_text(make), _norm_text(model)]
    return where, params


# SQL helper — strip trailing body / door-count / drivetrain words from dealer
# trim so "Carrera S Coupe" matches normalized bid trim "Carrera S" (exact),
# but "SRT Hellcat Redeye Widebody" does NOT match bid "SRT Hellcat" (those
# extra words are trim variants, not body noise).
_SQL_TRIM_STRIP = lambda col: f"""TRIM(BOTH FROM
    regexp_replace(
        regexp_replace(
            regexp_replace(
                LOWER(regexp_replace(COALESCE({col}, ''), E'\\\\s+', ' ', 'g')),
                E'\\\\s+(sport utility vehicle|pickup truck truck|pickup truck|sport utility|sport utility veh|crew cab|quad cab|extended cab|regular cab|sedan|coupe|convertible|cabriolet|roadster|hatchback|wagon|minivan|suv|spider|spyder|targa)\\\\s*$',
                '', 'g'),
            E'\\\\s+(4d|2d|4 dr|2 dr|4dr|2dr|4 door|2 door)\\\\s*$',
            '', 'g'),
        E'\\\\s+(awd|rwd|fwd|4wd|4matic|xdrive|quattro)\\\\s*$',
        '', 'g'))"""


# Tier 3 — free-text fallback. Used when canonical fields not available
# on either side. Matches v1 behavior except: year is EXACT (not ±2),
# trim is EXACT after body-strip (not prefix-LIKE).
def _tier3_freetext(year, make, model, trim):
    norm_trim = _norm_trim(trim)
    if norm_trim:
        where = f"""UPPER(di.make) = %s
                   AND UPPER(di.model) = %s
                   AND di.year = %s
                   AND {_SQL_TRIM_STRIP("di.trim")} = %s"""
        params = [_norm_text(make), _norm_text(model), year, norm_trim]
    else:
        # Bid has NO trim → strict: only match dealer rows whose trim is
        # empty/null/base AFTER body-stripping (so a dealer trim of just
        # "Coupe" or "Sedan" still matches base, but "Demon 170" / "SRT
        # Hellcat" / any actual trim-variant does NOT match a base bid).
        where = f"""UPPER(di.make) = %s
                    AND UPPER(di.model) = %s
                    AND di.year = %s
                    AND {_SQL_TRIM_STRIP("di.trim")} = ''"""
        params = [_norm_text(make), _norm_text(model), year]
    return where, params


# ── Public function (drop-in replacement for v1 find_dealer_matches) ────

DEFAULT_CONFIG = {'max_active': 5, 'max_sales': 10, 'max_pitch': 3,
                  'recent_days': 90, 'min_sold_confidence': 0.70}


def _sonnet_filter_rows(rows, eff_make, eff_model, eff_year, eff_trim, db_conn, bid_id=None, kind='active'):
    """Drop rows whose trim Sonnet says is NOT equivalent to eff_trim.

    Tier-1.5 filter. Skipped when eff_trim is empty (we'd be asking Sonnet
    to compare "" against something — pointless and the SQL already handles
    base-trim semantics). Falls open on any failure so the matcher never
    starves the card on infra problems.

    Returns (filtered_rows, kept_n, dropped_n, applied_bool). Each kept row
    is annotated with sonnet_trim_{match,confidence,reason}.
    """
    if not rows or not eff_trim:
        return rows, len(rows), 0, False
    try:
        import claude_trim_match
    except Exception as e:
        print(f'[dm-diag] sonnet import err bid_id={bid_id}: {e!r}', flush=True)
        return rows, len(rows), 0, False
    filtered = []
    for r in rows:
        dealer_trim = (r.get('nhtsa_trim') if isinstance(r, dict) else None)                       or (r.get('trim') if isinstance(r, dict) else '') or ''
        try:
            d = claude_trim_match.is_same_trim(
                eff_make, eff_model, eff_year, eff_trim, dealer_trim,
                db_conn=db_conn)
        except Exception as e:
            print(f'[dm-diag] sonnet call err bid_id={bid_id} kind={kind}: {e!r}',
                  flush=True)
            # Fall open for THIS row — keep it so we don't drop good matches on infra error.
            filtered.append(r)
            continue
        r['sonnet_trim_match'] = bool(d.get('match'))
        r['sonnet_trim_confidence'] = float(d.get('confidence') or 0.0)
        r['sonnet_trim_reason'] = d.get('reason') or ''
        r['sonnet_trim_source'] = d.get('source') or ''
        if d.get('match'):
            filtered.append(r)
    kept = len(filtered)
    dropped = len(rows) - kept
    print(f'[dm-diag] sonnet filter bid_id={bid_id} kind={kind} '
          f'kept={kept} dropped={dropped}', flush=True)
    return filtered, kept, dropped, True


def find_dealer_matches(db_conn, year, make, model,
                        trim=None, trim_confidence=None,
                        bid_id=None, vin=None, config=None):
    """Tiered like-vehicle lookup against EW's 8-dealer network.

    Args:
      year, make, model, trim — bid's vehicle fields. Pass canon_* values when
        the bid intake has them; pass raw bid.* otherwise. Function will use
        whichever it gets.
      bid_id — optional. If passed AND bids.canon_* is populated, the matcher
        will read canon_* from the DB itself (preferred). Otherwise relies on
        the args.
      vin — currently informational; reserved for future tiered "exact VIN" pass.

    Returns:
      {'active': [...], 'recent_sales': [...], 'top_pitch': [...],
       'config_used': {...},
       'tier_used': 'tier1_canon' | 'tier2_canon_ymm' | 'tier3_freetext' | 'none',
       'tier_confidence': 'high' | 'medium' | 'low' | 'none'}
    """
    empty = {'active': [], 'recent_sales': [], 'patterns': [],
             'top_pitch': [], 'config_used': {}, 'tier_used': 'none',
             'tier_confidence': 'none'}
    # DIAGNOSTIC: tag every call so we can correlate worker behavior with
    # what params find_dealer_matches actually saw. Easy grep: `dm-diag`.
    print(f'[dm-diag] ENTER bid_id={bid_id} year={year!r} make={make!r} '
          f'model={model!r} trim={trim!r} cfg_has_dealer_match='
          f'{bool((config or {}).get("dealer_match"))}', flush=True)
    if not (year and make and model):
        print(f'[dm-diag] EMPTY bid_id={bid_id} reason=missing_ymm', flush=True)
        return empty

    cfg = {**DEFAULT_CONFIG, **(config or {}).get('dealer_match', {})}
    max_active = int(cfg.get('max_active', 5))

    # If bid_id passed, prefer canon_* from DB (more authoritative than args)
    canon_year = canon_make = canon_model = canon_trim = None
    canon_lookup_ok = None  # None = not attempted, True/False = result
    if bid_id is not None:
        try:
            # Use a fresh tuple cursor (caller's db_conn may be RealDictCursor)
            with db_conn.cursor() as _c:
                _c.execute("""
                    SELECT canon_year, canon_make, canon_model, canon_trim
                    FROM bids WHERE id = %s
                """, (bid_id,))
                row = _c.fetchone()
                if row:
                    # Tolerate either dict-row or tuple-row cursors.
                    if isinstance(row, dict):
                        canon_year  = row.get('canon_year')
                        canon_make  = row.get('canon_make')
                        canon_model = row.get('canon_model')
                        canon_trim  = row.get('canon_trim')
                    else:
                        canon_year, canon_make, canon_model, canon_trim = row
                canon_lookup_ok = True
        except Exception as _e:
            canon_lookup_ok = False
            print(f'[dm-diag] canon lookup err bid_id={bid_id}: {_e!r}',
                  flush=True)
            try: db_conn.rollback()
            except Exception: pass

    eff_year  = canon_year  or year
    eff_make  = canon_make  or make
    eff_model = canon_model or model
    eff_trim  = canon_trim if canon_trim is not None else trim

    # ── Try tiers in order; first non-empty wins ────────────────────────
    tier_attempts = []

    # Tier 1 only worthwhile when canon_* present on bid AND we expect
    # dealer rows to have nhtsa_* (after backfill, ~all rows do).
    has_canon_bid = canon_year and canon_make and canon_model
    if has_canon_bid:
        tier_attempts.append(('tier1_canon', 'high',
            _tier1_canon_full(eff_year, eff_make, eff_model, eff_trim)))
    # Tier 2 — drop trim, still canonical
    if has_canon_bid:
        tier_attempts.append(('tier2_canon_ymm', 'medium',
            _tier2_canon_ymm(eff_year, eff_make, eff_model)))
    # Tier 3 — free-text always available
    tier_attempts.append(('tier3_freetext', 'low',
        _tier3_freetext(eff_year, eff_make, eff_model, eff_trim)))

    chosen_tier = None
    chosen_conf = None
    active_rows = []
    tier_diag = []  # per-tier (name, row_count_or_err) for diagnostic
    for tier_name, conf, (where, params) in tier_attempts:
        sql, full_params = _select_active(where, params, max_active)
        # Use a fresh cursor per tier so errors don't poison the connection.
        # Also rollback any aborted transaction before trying the next tier.
        try:
            with db_conn.cursor() as _c:
                _c.execute(sql, tuple(full_params))
                raw_rows = _c.fetchall()
                # Build dicts whether cursor is RealDictCursor or tuple cursor
                cols = [d[0] for d in _c.description]
                rows = []
                for rr in raw_rows:
                    rows.append(rr if isinstance(rr, dict) else dict(zip(cols, rr)))
        except Exception as e:
            tier_diag.append((tier_name, f'err:{type(e).__name__}:{e!r}'))
            print(f'[dm-diag] tier {tier_name} err bid_id={bid_id}: '
                  f'{type(e).__name__}: {e!r}', flush=True)
            try: db_conn.rollback()
            except Exception: pass
            continue
        tier_diag.append((tier_name, f'rows={len(rows)}'))
        if rows:
            for r in rows:
                r['match_tier'] = tier_name
            active_rows = rows
            chosen_tier = tier_name
            chosen_conf = conf
            break

    if not chosen_tier:
        print(f'[dm-diag] EMPTY bid_id={bid_id} reason=no_tier_matched '
              f'canon_lookup_ok={canon_lookup_ok} has_canon={has_canon_bid} '
              f'eff_ymm={eff_year!r}/{eff_make!r}/{eff_model!r}/{eff_trim!r} '
              f'tiers={tier_diag} cfg.recent_days={cfg.get("recent_days")} '
              f'cfg.max_active={cfg.get("max_active")}', flush=True)
        return empty
    print(f'[dm-diag] OK bid_id={bid_id} tier={chosen_tier} '
          f'active={len(active_rows)}', flush=True)

    # ── Tier-1.5 Sonnet filter ─────────────────────────────────────────
    # Tier 1 is exact-trim so doesn't need filtering. Tier 2 drops trim entirely
    # (silent false positives like Scat Pack vs Hellcat). Tier 3 is SQL body-strip
    # which still leaks variants. Hand each candidate's trim to claude_trim_match
    # which caches per-pair forever in trim_match_cache. Falls open on any error.
    sonnet_active_kept = sonnet_active_dropped = 0
    sonnet_applied = False
    if chosen_tier in ('tier2_canon_ymm', 'tier3_freetext') and active_rows:
        active_rows, sonnet_active_kept, sonnet_active_dropped, sonnet_applied =             _sonnet_filter_rows(active_rows, eff_make, eff_model, eff_year,
                                eff_trim, db_conn, bid_id=bid_id, kind='active')
        if sonnet_applied:
            if active_rows:
                # Sonnet-filtered candidates are semantically same trim → high conf
                chosen_conf = 'high'
            else:
                # All candidates rejected → preserve tier label for the sales lookup,
                # but tag confidence as none so the card doesn't claim a match.
                chosen_conf = 'none'

    # Recent-sales tier query — same WHERE on whichever tier hit
    # (intentionally simpler — match on the tier that produced active rows
    # so the two tabs stay coherent)
    where_rs, params_rs = next(
        (q for n, _, q in tier_attempts if n == chosen_tier), (None, None))
    sales_rows = []
    if where_rs is not None:
        try:
            with db_conn.cursor() as _c:
                # Dedupe at SQL level: DISTINCT ON (inventory_id) keeps the
                # earliest detection per car. dealer_sold_signals re-fires
                # daily for cars still missing from the active feed, so a
                # single sale can have 5-10 rows. Earliest detected_at gives
                # the truest days_to_sell.
                _c.execute(f"""
                    SELECT DISTINCT ON (di.id)
                           di.id AS inventory_id,
                           dss.dealer_id, d.name AS dealer_name,
                           di.year, di.make, di.model, di.trim,
                           di.price, di.mileage, di.url,
                           dss.detected_at, dss.confidence,
                           GREATEST(0,
                             EXTRACT(EPOCH FROM (dss.detected_at - di.first_seen_at))::int
                             / 86400
                           )::int AS days_to_sell
                    FROM dealer_sold_signals dss
                    JOIN dealer_inventory di ON dss.inventory_id = di.id
                    JOIN dealers d ON dss.dealer_id = d.id
                    WHERE {where_rs}
                      AND dss.detected_at > NOW() - (%s || ' days')::interval
                      AND d.active = TRUE
                      AND dss.confidence >= %s
                    ORDER BY di.id, dss.detected_at ASC
                    LIMIT %s
                """, tuple(params_rs + [int(cfg['recent_days']),
                                         float(cfg['min_sold_confidence']),
                                         int(cfg['max_sales'])]))
                raw_rs = _c.fetchall()
                cols = [d[0] for d in _c.description]
                sales_rows = [r if isinstance(r, dict) else dict(zip(cols, r))
                              for r in raw_rs]
                # Re-sort to "most recent first" for display now that SQL
                # ORDER BY had to be (inventory_id, detected_at ASC) for the
                # DISTINCT ON dedupe.
                sales_rows.sort(key=lambda r: r.get('detected_at') or '',
                                reverse=True)
        except Exception as e:
            print(f'[dealer_match v2] sales tier {chosen_tier} err: {e}',
                  flush=True)
            try: db_conn.rollback()
            except Exception: pass

    # Filter sales_rows with Sonnet too — same Tier 2/3 leak applies here.
    sonnet_sales_kept = sonnet_sales_dropped = 0
    if chosen_tier in ('tier2_canon_ymm', 'tier3_freetext') and sales_rows:
        sales_rows, sonnet_sales_kept, sonnet_sales_dropped, _ =             _sonnet_filter_rows(sales_rows, eff_make, eff_model, eff_year,
                                eff_trim, db_conn, bid_id=bid_id, kind='sales')

    # ── Aggregate per-dealer sold patterns (restored from pre-strict v1) ──
    # Group deduped sales_rows by dealer_id → sold_count + avg_days_to_sell.
    # Drives the "Sold at partner dealers · last Nd" section on bid.html
    # and m_full.html.
    patterns = []
    if sales_rows:
        by_dealer = {}
        for s in sales_rows:
            did = s.get('dealer_id')
            if did is None:
                continue
            slot = by_dealer.setdefault(did, {
                'dealer_id':   did,
                'dealer_name': s.get('dealer_name'),
                'sold_count':  0,
                '_dts_sum':    0,
                '_dts_n':      0,
            })
            slot['sold_count'] += 1
            dts = s.get('days_to_sell')
            if dts is not None:
                slot['_dts_sum'] += int(dts)
                slot['_dts_n']   += 1
        for slot in by_dealer.values():
            avg = (slot['_dts_sum'] // slot['_dts_n']) if slot['_dts_n'] else None
            patterns.append({
                'dealer_id':        slot['dealer_id'],
                'dealer_name':      slot['dealer_name'],
                'sold_count':       slot['sold_count'],
                'avg_days_to_sell': avg,
            })
        # Hot dealers first: most sales, then fastest turnover.
        patterns.sort(key=lambda p: (-p['sold_count'],
                                     p['avg_days_to_sell'] if p['avg_days_to_sell'] is not None else 9999))

    return {
        'active': active_rows,
        'recent_sales': sales_rows,
        'patterns': patterns,
        'top_pitch': [],  # TODO: rank dealers by sold count + turnover
        'config_used': dict(cfg),
        'tier_used': chosen_tier,
        'tier_confidence': chosen_conf,
        'sonnet_filter': {
            'applied': sonnet_applied,
            'active_kept':    sonnet_active_kept,
            'active_dropped': sonnet_active_dropped,
            'sales_kept':     sonnet_sales_kept,
            'sales_dropped':  sonnet_sales_dropped,
        },
    }


# ── Prompt formatter (preserved from v1 — callers in app.py import this) ──

def format_for_prompt(matches, max_sample_lines=8):
    """Render dealer matches as a plain-text block for inclusion in the
    Gemini assessment prompt. Returns empty string when no matches."""
    if not matches:
        return ''
    active = matches.get('active') or []
    sales  = matches.get('recent_sales') or []
    pitch  = matches.get('top_pitch') or []
    cfg    = matches.get('config_used') or {}
    tier   = matches.get('tier_used') or 'unknown'

    if not active and not sales and not pitch:
        return ''

    header = '═══ DEALER NETWORK INTEL (live data from scanned partner dealers) ═══'
    lines = [header, f'  match tier: {tier}']

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

    if sales:
        lines.append(f'\nRECENT SALES (last {cfg.get("recent_days", 90)} days):')
        for s in sales[:max_sample_lines]:
            dts = s.get('days_to_sell')
            dts_str = f'sold in {dts}d' if dts is not None else 'sold'
            price = s.get('price')
            price_str = f'${price:,}' if price else 'no price'
            lines.append(f'  • {s.get("dealer_name")}: {price_str} · {dts_str}')

    if pitch:
        lines.append('\nPITCH CANDIDATES (ranked by fit × turnover × stock):')
        for i, tp in enumerate(pitch, 1):
            lines.append(f'  {i}. {tp.get("dealer_name", "?")} — {tp.get("reason", "")}')

    lines.append(
        '\nUse this to inform your percentage adjustment:\n'
        '  - Fast turnover at a partner dealer = stronger baseline confidence → smaller downward adjustment\n'
        '  - Slow turnover / heavy stock of this model → more caution → larger downward adjustment\n'
        '  - Active listings reveal retail ceiling; our wholesale bid must leave room for partner markup'
    )
    return '\n'.join(lines)


# ════════════════════════════════════════════════════════════════════════════
# Quick self-test (no DB connection)
# ════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    cases = [
        ('Carrera S Coupe',     'carrera s'),
        ('Carrera S',           'carrera s'),
        ('Carrera 4 Convertible','carrera 4'),
        ('Cayenne',             ''),                # base
        ('Base',                ''),
        ('GT 4d Sport Utility', 'gt'),
        ('S 63 E AMG Sedan',    's 63 e amg'),
        ('Premium Plus xDrive', 'premium plus'),
        (None,                  ''),
    ]
    print(f"{'INPUT':<28} {'EXPECTED':<18} {'GOT':<18} OK")
    for inp, expected in cases:
        got = _norm_trim(inp)
        ok = 'PASS' if got == expected else 'FAIL'
        print(f"{repr(inp):<28} {repr(expected):<18} {repr(got):<18} {ok}")
