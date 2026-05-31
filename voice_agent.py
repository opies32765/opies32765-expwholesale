"""voice_agent.py — EW voice bot ("EW") for YMM-based valuation.

Day 2 surface:
  - GET  /mobile/ewbot               → mobile-first HTML w/ mic button
  - POST /api/voice/query            → transcript → Flash parse → comps → Flash reply
  - GET  /api/voice/health           → DB liveness probe

Isolation contract (won't interfere with the bid pipeline):
- READ-ONLY on dealer_mmr, dealer_intelligence.inventory, LSL crm.db
- WRITES only to voice_valuations (new table)
- No edits to vauto/accutrade/ipacket/bids paths
- Disable instantly by commenting `app.register_blueprint(voice_bp)` in app.py
"""
from __future__ import annotations

import json as _json
import os
import re
import secrets
import sqlite3
import time
from typing import Any, Optional

from flask import Blueprint, jsonify, render_template, request

import psycopg2
import psycopg2.extras

voice_bp = Blueprint('voice', __name__)

# ── Connections (lazy, per-request) ────────────────────────────────────
DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
DI_DATABASE_URL = os.environ.get(
    'DI_DATABASE_URL',
    'postgresql://scraper:DealerScraper2026@localhost/dealer_intelligence')
LSL_DB_PATH = os.environ.get('LSL_DB_PATH', '/opt/livesaleslog/crm.db')


def _pg_ew():
    c = psycopg2.connect(DATABASE_URL)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


def _pg_di():
    c = psycopg2.connect(DI_DATABASE_URL)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


def _lsl_ro():
    if not os.path.exists(LSL_DB_PATH):
        return None
    c = sqlite3.connect(f'file:{LSL_DB_PATH}?mode=ro', uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


# ── Helpers ────────────────────────────────────────────────────────────
def _short_id(n: int = 6) -> str:
    """URL-safe short id. 31^6 = 887M collision space; collisions retried at insert."""
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(n))


def _miles_band(miles: Optional[int]) -> tuple[Optional[int], Optional[int]]:
    if not miles or miles < 0:
        return (None, None)
    return (int(miles * 0.8), int(miles * 1.2))


def _norm(s: Optional[str]) -> Optional[str]:
    return s.strip().upper() if isinstance(s, str) and s.strip() else None


def _summarize(values: list[float]) -> dict:
    if not values:
        return {'n': 0, 'median': None, 'p25': None, 'p75': None,
                'min': None, 'max': None, 'mean': None}
    vs = sorted(values)
    n = len(vs)

    def pct(p: float) -> float:
        idx = max(0, min(n - 1, int(round(p * (n - 1)))))
        return vs[idx]
    return {
        'n': n,
        'median': pct(0.5),
        'p25':    pct(0.25),
        'p75':    pct(0.75),
        'min':    vs[0],
        'max':    vs[-1],
        'mean':   sum(vs) / n,
    }


# VIN extraction. Speech-to-text often inserts spaces or punctuation
# between VIN chars. Strip everything non-VIN-valid, then scan for a
# 17-char window of legal VIN chars (A-HJ-NPR-Z0-9; I/O/Q never appear).
_VIN_CHARS = re.compile(r'[A-HJ-NPR-Z0-9]', re.IGNORECASE)


_VIN_LETTER_VALUES = {
    'A': 1, 'B': 2, 'C': 3, 'D': 4, 'E': 5, 'F': 6, 'G': 7, 'H': 8,
    'J': 1, 'K': 2, 'L': 3, 'M': 4, 'N': 5, 'P': 7, 'R': 9,
    'S': 2, 'T': 3, 'U': 4, 'V': 5, 'W': 6, 'X': 7, 'Y': 8, 'Z': 9,
}
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def _vin_checksum_ok(vin: str) -> bool:
    """SAE J853 VIN check-digit validation. NA-spec VINs (sold in US/CA/MX)
    must pass; many BMW/Mercedes/etc imports also satisfy it. Rejects
    random 17-char English-word slices."""
    if len(vin) != 17:
        return False
    total = 0
    for i, ch in enumerate(vin):
        if ch.isdigit():
            v = int(ch)
        elif ch in _VIN_LETTER_VALUES:
            v = _VIN_LETTER_VALUES[ch]
        else:
            return False
        total += v * _VIN_WEIGHTS[i]
    expected = total % 11
    expected_char = 'X' if expected == 10 else str(expected)
    return vin[8] == expected_char


_VIN_KEYWORD = re.compile(r'\bv\s*i?\s*\.?\s*i?\s*\.?\s*n\b', re.I)
# Don't trigger when VIN appears inside a question/conditional ("if I give you the VIN…")
_VIN_QUESTION_GUARD = re.compile(
    r'\b(?:if|can you|could you|would you|do you|will you|how do i|how would|what if)\b'
    r'(?:[^.?!]*?)\bv\s*i?\s*\.?\s*i?\s*\.?\s*n\b',
    re.I,
)


_VIN_ASK_PHRASES = re.compile(
    r"\b(read me the vin|give me the vin|need (?:the |a )?vin|"
    r"got (?:the |a )?vin|got it handy|read it off|read the vin|"
    r"you got it handy|i'?ll need (?:the |a )?vin|spell it out|"
    r"vin handy|have a vin|have the vin|you got a vin|"
    r"do you have (?:the |a )?vin|got a vin)\b",
    re.I,
)


def _near_vin_attempt(text: str, prior_bot_reply: str = "") -> Optional[int]:
    """Detect a partial/truncated VIN attempt. Returns the compact-char
    count if 10-16 VIN-legal chars found, else None.

    Triggers when EITHER:
      (a) transcript contains literal 'VIN' keyword (operator-initiated), or
      (b) the prior bot reply was asking for a VIN (conversational context —
          e.g., bot said "I need the VIN" and operator just reads chars).
    """
    if not text:
        return None
    upper = text.upper()
    if _VIN_QUESTION_GUARD.search(upper):
        return None

    has_vin_keyword = bool(_VIN_KEYWORD.search(upper))
    bot_just_asked  = bool(_VIN_ASK_PHRASES.search(prior_bot_reply or ''))
    if not (has_vin_keyword or bot_just_asked):
        return None

    # FALSE-POSITIVE GUARDS 2026-05-21: strip mileage/MSRP/dollar phrases
    # FIRST so their digits + label-letters don't get counted as VIN chars.
    # Then test the remaining text.
    stripped = re.sub(
        r'\b(?:\d{1,3}[,]?\d{3}|\d{4,6}|\d+\s*K)\s*'
        r'(?:miles?|mileage|odo(?:meter)?|mi)\b',
        ' ', upper, flags=re.I)
    stripped = re.sub(
        r'\b(?:msrp|sticker|window\s+sticker|asking|cash|offer|price)\s+'
        r'(?:of\s+|at\s+)?[$]?\d[\d,]*\s*(?:k|thousand)?\b',
        ' ', stripped, flags=re.I)
    # Also strip standalone "X miles" / "X k" without label
    stripped = re.sub(r'\b\d[\d,]*\s*K\b', ' ', stripped, flags=re.I)
    # Now strip remaining filler words that aren't VIN-related
    stripped = re.sub(r'\b(?:WITH|AND|HAS|HAVE|HAD|WORTH|THE|MILES?|MILEAGE)\b',
                      ' ', stripped, flags=re.I)
    upper = stripped

    for noise in _VIN_NOISE_TOKENS:
        upper = upper.replace(noise, ' ')
    for _ in range(5):
        new = _VIN_LEADING_NOISE.sub('', upper)
        if new == upper: break
        upper = new
    compact = ''.join(_VIN_CHARS.findall(upper))
    n = len(compact)
    # Require ≥6 digits — real VINs have plenty of digits (positions
    # 10-17 are all numeric). Pure-letter strings with a few digits are
    # almost always English words, not VIN attempts.
    digit_count = sum(1 for ch in compact if ch.isdigit())
    if 10 <= n <= 16 and digit_count >= 6:
        return n
    return None


_VIN_NOISE_TOKENS = (
    'VIN NUMBER', 'V I N NUMBER', 'THE VIN IS', 'VIN IS', 'V I N IS',
    'VIN', 'V I N', 'NUMBER IS', "VIN'S", 'IT IS', 'THE NUMBER',
)
_VIN_LEADING_NOISE = re.compile(
    r'^(?:THE|IT|IS|A|AN|OF|MY|HERE|AND|UM|UH|SO|OK|OKAY|YEAH|HEY|'
    r'ALRIGHT|RIGHT|SURE|YES|NO|THEN|ACTUALLY)[\s,\.\-]+',
    re.I,
)


def _extract_vin(text: str, prior_bot_reply: str = "") -> Optional[str]:
    """Find a real VIN inside a noisy STT transcript.
    Order matters: strip leading filler FIRST, then locate the VIN
    keyword AFTER stripping (so m.end() indexes into the stripped
    string). Previously bugged on 'Okay, VIN is WBA33EJ07RCS36802' —
    m.end() was computed pre-strip, so tail extraction skipped chars.
    """
    if not text:
        return None
    upper = text.upper()

    # Guard against "if I give you the VIN can you..." patterns
    if _VIN_QUESTION_GUARD.search(upper):
        return None

    # Strip leading filler words ("Okay,", "Yeah", "Alright") so the
    # subsequent regex matches at the right offsets.
    for _ in range(5):
        new = _VIN_LEADING_NOISE.sub('', upper)
        if new == upper: break
        upper = new

    m = _VIN_KEYWORD.search(upper)
    bot_just_asked = bool(_VIN_ASK_PHRASES.search(prior_bot_reply or ''))
    if not m and not bot_just_asked:
        return None

    # Take only the text AFTER the first VIN keyword if it's present;
    # otherwise scan the whole stripped utterance.
    tail = upper[m.end():] if m else upper
    compact = ''.join(_VIN_CHARS.findall(tail))
    if len(compact) < 17:
        return None

    year_codes = set('ABCDEFGHJKLMNPRSTVWXY123456789')
    international_wmi = set('JKLMNPRSTUVWXYZ')  # non-NA WMI first chars
    candidates: list[tuple[int, str]] = []
    for i in range(0, len(compact) - 16):
        cand = compact[i:i+17]
        if cand[9] not in year_codes:
            continue
        if cand[0] not in 'ABCDEFGHJKLMNPRSTUVWXYZ123456789':
            continue
        n_digits = sum(c.isdigit() for c in cand)
        n_letters = 17 - n_digits
        # Real VINs have at least 3 letters AND at least 4 digits
        if n_letters < 3 or n_digits < 4:
            continue
        # VIN_GUARD_2026_05_21 — require CHECKSUM-VALID only. The prior
        # 'international WMI plausible' fallback was admitting fake VINs
        # built by concatenating words across spaces (e.g. transcript
        # "What is a 2024 BMW M2 with..." → "WHATSA2024BMWM2WT" passing
        # because W is in the international-WMI set).
        if not _vin_checksum_ok(cand):
            continue
        score = 10
        candidates.append((score, cand))

    if not candidates:
        return None
    # Highest scoring (checksum-valid wins) — ties broken by last occurrence
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _strip_json(s: str) -> Optional[dict]:
    """Pull the first {...} JSON object out of an LLM response that may be
    wrapped in ```json or have extra prose."""
    if not s:
        return None
    m = re.search(r'\{.*\}', s, re.DOTALL)
    if not m:
        return None
    try:
        return _json.loads(m.group(0))
    except _json.JSONDecodeError:
        return None


# ── voice_ymm_master lookup (pre-built nightly intelligence) ────────────
def _lookup_voice_master(year: int, make: str, model: str,
                        miles: Optional[int] = None) -> Optional[dict]:
    """Fuzzy match against voice_ymm_master. Returns the row if found,
    else None. Tries exact (year, make, model) first; falls back to a
    prefix match on model (e.g., 'Silverado 1500' matches when query
    was 'Silverado 1500 LTZ')."""
    make_u = (make or '').strip().upper()
    model_u = (model or '').strip().upper()
    miles_band = (int(miles or 0) // 5000) * 5000
    try:
        with _pg_ew() as c, c.cursor() as cur:
            # Tier 1: exact model match at closest miles_band
            cur.execute("""
                SELECT * FROM voice_ymm_master
                 WHERE year = %s
                   AND UPPER(make)  = %s
                   AND UPPER(model) = %s
                   AND expires_at > NOW()
                 ORDER BY ABS(miles_band - %s) ASC, refreshed_at DESC
                 LIMIT 1
            """, (year, make_u, model_u, miles_band))
            row = cur.fetchone()
            if row: return dict(row)
            # Tier 2: model prefix match (handles 'Silverado 1500' ⊂ 'Silverado 1500 LTD')
            cur.execute("""
                SELECT * FROM voice_ymm_master
                 WHERE year = %s
                   AND UPPER(make)  = %s
                   AND (UPPER(model) LIKE %s OR %s LIKE UPPER(model) || '%%')
                   AND expires_at > NOW()
                 ORDER BY ABS(miles_band - %s) ASC, refreshed_at DESC
                 LIMIT 1
            """, (year, make_u, f'{model_u}%', model_u, miles_band))
            row = cur.fetchone()
            if row: return dict(row)
    except Exception as e:
        print(f'[voice] master lookup err: {e}', flush=True)
    return None


def _context_from_master(row: dict, year: int, make: str, model: str,
                        miles: Optional[int], msrp: Optional[int]) -> dict:
    """Convert voice_ymm_master row → ctx dict matching
    get_valuation_context's output shape so downstream code is unchanged."""
    has_live = bool(row.get('mmr_wholesale_avg'))
    def _j(s):
        if not s: return []
        if isinstance(s, list): return s
        try: return _json.loads(s)
        except Exception: return []

    rb_summary = {
        'n': row.get('rbook_listing_count') or 0,
        'median': row.get('rbook_median_retail'),
        'p25': row.get('rbook_p25'),
        'p75': row.get('rbook_p75'),
        'mean': row.get('rbook_median_retail'),  # approx
        'min': row.get('rbook_p25'),
        'max': row.get('rbook_p75'),
    }
    return {
        'inputs': {'year': year, 'make': make, 'model': model,
                   'trim': None, 'miles': miles, 'msrp': msrp},
        'live_vauto': ({
            'scaffold': {'source': 'voice_ymm_master_cache',
                         'refreshed_at': row.get('vauto_refreshed_at').isoformat()
                                         if row.get('vauto_refreshed_at') else None},
            'live_rbook': {
                'n': row.get('rbook_listing_count') or 0,
                'summary': rb_summary,
                'closest_anchor': _j(row.get('rbook_closest_anchor')),
                'rows_sample': [],
            },
            'live_mmr': {
                'average_auction_price': row.get('mmr_wholesale_avg'),
                'average_odometer':      row.get('mmr_avg_odometer'),
            },
        } if has_live else None),
        'mmr_recent': {'rows': [], 'summary_wholesale': _summarize([]),
                       'summary_retail': _summarize([])},
        'lsl_history': {
            'rows': _j(row.get('lsl_recent_deals_sample')),
            'summary_purchase': {
                'n': row.get('lsl_count_90d') or 0,
                'median': float(row['lsl_median_purchase']) if row.get('lsl_median_purchase') else None,
                'mean':   float(row['lsl_avg_purchase_cost']) if row.get('lsl_avg_purchase_cost') else None,
                'p25': None, 'p75': None, 'min': None, 'max': None,
            },
            'summary_sale': {
                'n': row.get('lsl_count_90d') or 0,
                'median': float(row['lsl_avg_sale_price']) if row.get('lsl_avg_sale_price') else None,
                'mean':   float(row['lsl_avg_sale_price']) if row.get('lsl_avg_sale_price') else None,
                'p25': None, 'p75': None, 'min': None, 'max': None,
            },
        },
        'prior_bids': {
            'rows': [],
            'summary_mmr': {
                'n': row.get('prior_bid_count_90d') or 0,
                'median': row.get('prior_bid_avg_mmr'),
                'mean':   row.get('prior_bid_avg_mmr'),
                'p25': None, 'p75': None, 'min': None, 'max': None,
            },
            'summary_rbook': {
                'n': row.get('prior_bid_count_90d') or 0,
                'median': row.get('prior_bid_avg_rbook'),
                'mean':   row.get('prior_bid_avg_rbook'),
                'p25': None, 'p75': None, 'min': None, 'max': None,
            },
        },
        'partner_inventory_top3': _j(row.get('partner_active_sample')),
        'lsl_recent_deals_top3':  _j(row.get('lsl_recent_deals_sample')),
        'prior_bids_recent_top3': _j(row.get('prior_bids_sample')),
        # ── Rich aggregations from voice_ymm_master v2 ─────────────────
        'rbook_pool': {
            'count':         row.get('rbook_pool_count') or 0,
            'top_comps':     _j(row.get('rbook_top_comps')),
            'median_dol':    row.get('rbook_median_dol'),
            'certified_pct': row.get('rbook_certified_pct'),
            'pending_pct':   row.get('rbook_pending_pct'),
        },
        'manheim_pool': {
            'count':         row.get('manheim_pool_count') or 0,
            'recent_sales':  _j(row.get('manheim_recent_sales')),
            'median_sale':   row.get('manheim_median_sale'),
            'avg_condition': float(row['manheim_avg_condition']) if row.get('manheim_avg_condition') else None,
            'last_sale_at':  row.get('manheim_last_sale_at').isoformat() if row.get('manheim_last_sale_at') else None,
        },
        'dealer_inv': {
            'active_full':  _j(row.get('dealer_inv_active_full')),
            'sold_recent':  _j(row.get('dealer_inv_sold_full')),
            'price_drops':  _j(row.get('dealer_inv_price_drops')),
            'active_dol_min':  row.get('dealer_active_min_dol'),
            'active_dol_max':  row.get('dealer_active_max_dol'),
            'active_dol_p50':  row.get('dealer_active_p50_dol'),
            'sold_avg_dol':    row.get('dealer_sold_avg_dol'),
        },
        'naive_target': None,
        'errors': [],
        'source': 'voice_ymm_master',  # marker so caller knows
    }


# ── In-memory vAuto BFF cache (5-min TTL) ───────────────────────────────
# Common YMMs queried back-to-back skip the vAuto round trip entirely.
# Per-worker process (10 gunicorn workers → 10 caches); fine at our scale.
import threading as _vth
_VAUTO_CACHE = {}
_VAUTO_CACHE_LOCK = _vth.Lock()
_VAUTO_CACHE_TTL_SEC = 300


def _vauto_cache_key(year, make, model, miles):
    """Bucket miles to nearest 500 so 5000 + 5300 share a cache entry."""
    m_band = (int(miles or 0) // 500) * 500
    return (int(year), (make or '').strip().upper(),
            (model or '').strip().upper(), m_band)


def _vauto_cache_get(key):
    """Postgres-backed cache so all 10 gunicorn workers share hits."""
    cache_key_str = _json.dumps(list(key))
    try:
        with _pg_ew() as c, c.cursor() as cur:
            cur.execute("""
                SELECT payload FROM voice_vauto_cache
                 WHERE cache_key = %s AND expires_at > NOW()
            """, (cache_key_str,))
            row = cur.fetchone()
        if row:
            return row['payload']
    except Exception as e:
        print(f'[voice] cache get failed: {e}', flush=True)
    return None


def _vauto_cache_put(key, value):
    if not value:
        return
    cache_key_str = _json.dumps(list(key))
    try:
        with _pg_ew() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO voice_vauto_cache (cache_key, payload, expires_at)
                VALUES (%s, %s, NOW() + INTERVAL '5 minutes')
                ON CONFLICT (cache_key) DO UPDATE SET
                  payload = EXCLUDED.payload,
                  expires_at = EXCLUDED.expires_at
            """, (cache_key_str, _json.dumps(value, default=str)))
            c.commit()
    except Exception as e:
        print(f'[voice] cache put failed: {e}', flush=True)


# ── Live vAuto lookup with a USER-PROVIDED VIN (no bid created) ─────────
def _fetch_live_vauto_with_vin(vin: str, miles: Optional[int] = None) -> Optional[dict]:
    """Real-time vAuto query keyed on a specific VIN the operator just
    read out loud. Returns the same shape as _fetch_live_vauto_for_ymm
    but uses THIS VIN as the canonical decode (no scaffold needed).
    Does NOT create a bid — just pulls live comps + MMR for the spoken VIN.
    """
    import time as _t
    t0 = _t.time()
    try:
        from vauto_enrichment import _get_jar, VEHICLE_INFO_URL
        from vauto_bff_direct import (
            fetch_competitive_set, fetch_price_guides,
            parse_competitive_set, parse_price_guides,
        )
        import requests as _req
        jar = _get_jar()
        # 1) vehicleInfo decode for the user's actual VIN
        r = _req.post(
            VEHICLE_INFO_URL,
            json={'vin': vin, 'odometer': int(miles or 0), 'odometerUom': 'Miles'},
            headers=jar.get_headers(), cookies=jar.get_cookies(),
            timeout=15,
        )
        if r.status_code != 200:
            print(f'[voice] vin-query vehicleInfo status={r.status_code} vin={vin}',
                  flush=True)
            return None
        data = r.json()
        vehicle = data.get('vehicleInfo')
        option_codes = data.get('optionCodes')
        if not vehicle or not vehicle.get('year'):
            return None
        if miles:
            vehicle = dict(vehicle)
            vehicle['odometer'] = int(miles)

        # 2) Borrow any recent bid's appraisal_id as a session wrapper
        with _pg_ew() as c, c.cursor() as cur:
            cur.execute("""
                SELECT v.appraisal_url FROM vauto_lookups v
                 WHERE v.appraisal_url LIKE '%Id=%'
                 ORDER BY v.looked_up_at DESC LIMIT 1
            """)
            w = cur.fetchone()
        if not w:
            return None
        from urllib.parse import parse_qs, urlparse
        appraisal_id = (parse_qs(urlparse(w['appraisal_url']).query).get('Id') or [None])[0]
        if not appraisal_id:
            return None

        # 3) Parallel BFF calls
        cookies = jar.get_cookies(); headers = jar.get_headers()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_comp = ex.submit(fetch_competitive_set, vehicle, cookies, headers,
                               appraisal_id=appraisal_id, option_codes=option_codes,
                               timeout=12)
            f_g = ex.submit(fetch_price_guides, vehicle, cookies, headers,
                            appraisal_id=appraisal_id, timeout=12)
            comp_resp = f_comp.result(timeout=15)
            guides_resp = f_g.result(timeout=15)

        comp = parse_competitive_set(comp_resp or {})
        rows = comp.get('rows', []) or []
        prices = [r['price'] for r in rows
                  if r.get('price') and 2000 <= float(r['price']) <= 1_000_000]
        closest_anchor = None
        if miles and rows:
            valid = [r for r in rows
                     if r.get('mileage') and r.get('price')
                     and 2000 <= float(r['price']) <= 1_000_000]
            if valid:
                a = min(valid, key=lambda r: abs(int(r['mileage']) - int(miles)))
                closest_anchor = {
                    'price': int(a['price']),
                    'mileage': int(a['mileage']),
                    'days_on_lot': a.get('days_on_lot'),
                    'dealer': a.get('dealer'),
                    'trim':   a.get('trim'),
                    'is_certified': a.get('is_certified'),
                }
        guides = parse_price_guides(guides_resp or {})
        manheim = guides.get('manheim') or {}
        return {
            'subject_vin': vin,
            'subject_year': vehicle.get('year'),
            'subject_make': vehicle.get('make'),
            'subject_model': vehicle.get('model'),
            'subject_trim': vehicle.get('trim'),
            'live_rbook': {
                'n': len(rows),
                'summary': _summarize([float(p) for p in prices]),
                'closest_anchor': closest_anchor,
                'rows_sample': rows[:5],
            },
            'live_mmr': {
                'average_auction_price': manheim.get('average_auction_price'),
                'average_odometer':      manheim.get('average_odometer'),
            },
            'elapsed_ms': int((_t.time() - t0) * 1000),
        }
    except Exception as e:
        print(f'[voice] vin-query exception: {e}', flush=True)
        return None


# ── VIN phonetic readback (NATO alphabet for unambiguous TTS) ───────────
_NATO = {
    '0': 'zero', '1': 'one', '2': 'two', '3': 'three', '4': 'four',
    '5': 'five', '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine',
    'A': 'Alpha', 'B': 'Bravo', 'C': 'Charlie', 'D': 'Delta', 'E': 'Echo',
    'F': 'Foxtrot', 'G': 'Golf', 'H': 'Hotel', 'J': 'Juliet', 'K': 'Kilo',
    'L': 'Lima', 'M': 'Mike', 'N': 'November', 'P': 'Papa', 'R': 'Romeo',
    'S': 'Sierra', 'T': 'Tango', 'U': 'Uniform', 'V': 'Victor', 'W': 'Whiskey',
    'X': 'X-ray', 'Y': 'Yankee', 'Z': 'Zulu',
}


def _vin_phonetic(vin: str) -> str:
    """Convert a 17-char VIN to NATO phonetic for TTS readback.
    Example: 'WBA33EJ07RCS36802' → 'Whiskey Bravo Alpha three three Echo
    Juliet zero seven Romeo Charlie Sierra three six eight zero two'."""
    return ' '.join(_NATO.get(c, c) for c in vin.upper())


def _phone_spoken(phone_e164: str) -> str:
    """+14074309675 → 'four oh seven, four three oh, nine six seven five'."""
    digits = re.sub(r'\D', '', phone_e164 or '')
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) != 10:
        return phone_e164 or '(no phone)'
    speak = {'0': 'oh', '1': 'one', '2': 'two', '3': 'three', '4': 'four',
             '5': 'five', '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine'}
    a = ' '.join(speak[c] for c in digits[0:3])
    b = ' '.join(speak[c] for c in digits[3:6])
    c_ = ' '.join(speak[c] for c in digits[6:10])
    return f'{a}, {b}, {c_}'


# ── Confirmation detection (operator agreeing to full bid creation) ─────
_CONFIRM_RE = re.compile(
    r'\b(yes|yeah|yep|yup|sure|do it|run it|fire it|send it|go ahead|'
    r'full appraisal|full bid|full report|go for it|please do|let\'?s do it)\b',
    re.I,
)
_DECLINE_RE = re.compile(r"\b(no|nope|not yet|skip|don'?t|nah|hold off)\b", re.I)


def _is_confirmation(transcript: str) -> bool:
    if not transcript or len(transcript.split()) > 8:
        return False
    if _DECLINE_RE.search(transcript):
        return False
    return bool(_CONFIRM_RE.search(transcript))


# ── Live vAuto BFF lookup (rBook + MMR in ~3s) ──────────────────────────
def _fetch_live_vauto_for_ymm(
    year: int, make: str, model: str,
    miles: Optional[int] = None,
    trim: Optional[str] = None,
) -> Optional[dict]:
    """Real-time vAuto BFF for a YMM + miles query (no VIN needed from
    the operator). Strategy: borrow a recent same-YMM bid's appraisalId
    + canonical vehicleInfo decode as the scaffold, then override miles
    with the voice query value so vAuto centers comps at the right band.

    Returns:
      {
        scaffold: {bid_id, vin_last4, miles},
        live_rbook: {n, summary, closest_anchor, rows_sample},
        live_mmr:   {average_auction_price, average_odometer},
        elapsed_ms,
      }
    or None when no scaffold bid exists, cookies are stale, or BFF errors.
    Always degrades gracefully — never throws.
    """
    import time as _t
    t0 = _t.time()

    # 1) Find the closest-mile same-YMM bid we've already processed.
    # EXACT-match first (so "M2" doesn't slurp "M235i" into the scaffold);
    # fall back to LIKE if no exact match exists (handles "7 Series" vs
    # the bid stored as "760i", etc).
    miles_for_sort = int(miles or 0)
    make_n = (make or '').strip().upper()
    model_n = (model or '').strip().upper()
    # TIER 1: prior bid of the same YMM — has both VIN + native appraisal_id
    bid_row = None
    try:
        with _pg_ew() as c, c.cursor() as cur:
            for model_clause, model_arg in (
                ('UPPER(b.model) = %s', model_n),                 # exact
                ('UPPER(b.model) LIKE %s', f'%{model_n}%'),       # fuzzy
                ('UPPER(b.model) LIKE %s', f'{model_n}%'),        # prefix
            ):
                sql = f"""
                    SELECT b.id, b.vin, b.mileage, b.model, v.appraisal_url,
                           'tier1_bid' AS tier
                      FROM bids b
                      JOIN vauto_lookups v ON v.bid_id = b.id
                     WHERE b.year = %s
                       AND UPPER(b.make) = %s
                       AND {model_clause}
                       AND v.appraisal_url IS NOT NULL
                       AND v.appraisal_url LIKE '%%Id=%%'
                       AND b.vin IS NOT NULL AND length(b.vin) = 17
                     ORDER BY ABS(COALESCE(b.mileage, 0) - %s) ASC,
                              v.looked_up_at DESC
                     LIMIT 1
                """
                cur.execute(sql, (year, make_n, model_arg, miles_for_sort))
                bid_row = cur.fetchone()
                if bid_row:
                    print(f'[voice] live-vauto scaffold T1: bid={bid_row["id"]} '
                          f'model={bid_row["model"]!r} miles={bid_row["mileage"]}',
                          flush=True)
                    break
    except Exception as e:
        print(f'[voice] T1 scaffold lookup failed: {e}', flush=True)

    # TIER 2: borrow a VIN from our daily-scanned dealer_inventory (EW DB,
    # 1,600 active VINs across 15 partner/target dealers, refreshed daily).
    # vAuto's BFF derives the competitive set from the `vehicle` payload —
    # the appraisal_id is just a session/audit container. We reuse the most
    # recent bid's appraisal_id as the wrapper and swap in the YMM-matched VIN.
    if not bid_row:
        try:
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT vin, mileage
                      FROM dealer_inventory
                     WHERE year = %s
                       AND UPPER(make)   = %s
                       AND UPPER(model) LIKE %s
                       AND vin IS NOT NULL
                       AND length(vin) = 17
                       AND status = 'active'
                     ORDER BY ABS(COALESCE(mileage, 0) - %s) ASC,
                              last_seen_at DESC
                     LIMIT 1
                """, (year, make_n, f'%{model_n}%', miles_for_sort))
                di_row = cur.fetchone()
                if di_row:
                    # Reuse the most recent bid's appraisal_id (any YMM).
                    cur.execute("""
                        SELECT b.id, v.appraisal_url
                          FROM bids b JOIN vauto_lookups v ON v.bid_id = b.id
                         WHERE v.appraisal_url LIKE '%Id=%'
                         ORDER BY v.looked_up_at DESC LIMIT 1
                    """)
                    wrapper = cur.fetchone()
                    if wrapper:
                        bid_row = {
                            'id':            wrapper['id'],
                            'vin':           di_row['vin'],
                            'mileage':       di_row['mileage'],
                            'model':         model,
                            'appraisal_url': wrapper['appraisal_url'],
                            'tier':          'tier2_ew_dealer_inventory',
                        }
                        print(f'[voice] live-vauto scaffold T2: vin={di_row["vin"]} '
                              f'(from dealer_inventory, borrowed appraisal '
                              f'from bid {wrapper["id"]})', flush=True)
        except Exception as e:
            print(f'[voice] T2 scaffold lookup failed: {e}', flush=True)

    if not bid_row:
        return None  # no scaffold available; caller falls back to historical

    # 2) Extract appraisalId from the captured URL
    from urllib.parse import parse_qs, urlparse
    try:
        qs = parse_qs(urlparse(bid_row['appraisal_url']).query)
        appraisal_id = (qs.get('Id') or [None])[0]
    except Exception:
        appraisal_id = None
    if not appraisal_id:
        return None

    # 3) Canonical vehicle dict (via vehicleInfo BFF) + cookies.
    # Tier 1 path uses build_vehicle_dict_from_bid (reads VIN from bids row).
    # Tier 2 path needs to decode the borrowed-from-dealer-inventory VIN —
    # call vehicleInfo directly with that VIN since the wrapper bid_id
    # points to a different YMM.
    try:
        from vauto_enrichment import _get_jar, VEHICLE_INFO_URL
        jar = _get_jar()
        if bid_row.get('tier') == 'tier2_ew_dealer_inventory':
            import requests as _req
            r = _req.post(
                VEHICLE_INFO_URL,
                json={'vin': bid_row['vin'],
                      'odometer': int(bid_row.get('mileage') or 0),
                      'odometerUom': 'Miles'},
                headers=jar.get_headers(),
                cookies=jar.get_cookies(),
                timeout=15,
            )
            if r.status_code != 200:
                print(f'[voice] T2 vehicleInfo status={r.status_code} '
                      f'vin={bid_row["vin"]}', flush=True)
                return None
            data = r.json()
            vehicle = data.get('vehicleInfo')
            option_codes = data.get('optionCodes')
            if not vehicle or not vehicle.get('year'):
                print(f'[voice] T2 vehicleInfo empty decode for {bid_row["vin"]}',
                      flush=True)
                return None
        else:
            from vauto_enrichment import build_vehicle_dict_from_bid
            with _pg_ew() as conn:
                built = build_vehicle_dict_from_bid(conn, bid_row['id'])
            if not built:
                return None
            vehicle, option_codes = built
    except Exception as e:
        print(f'[voice] vehicleInfo decode failed: {e}', flush=True)
        return None

    # Override odometer with voice query miles (so vAuto's competitive
    # set is centered around what the operator's actually evaluating).
    if miles:
        vehicle = dict(vehicle)
        vehicle['odometer'] = int(miles)

    try:
        cookies = jar.get_cookies()
        headers = jar.get_headers()
    except Exception as e:
        print(f'[voice] cookie load failed: {e}', flush=True)
        return None

    # 4) Parallel BFF calls — rBook competitive set + Manheim MMR
    from concurrent.futures import ThreadPoolExecutor
    from vauto_bff_direct import (
        fetch_competitive_set, fetch_price_guides,
        parse_competitive_set, parse_price_guides,
    )
    comp_resp, guides_resp = None, None
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_comp = ex.submit(fetch_competitive_set, vehicle, cookies, headers,
                               appraisal_id=appraisal_id, option_codes=option_codes,
                               timeout=12)
            f_guides = ex.submit(fetch_price_guides, vehicle, cookies, headers,
                                 appraisal_id=appraisal_id, timeout=12)
            comp_resp = f_comp.result(timeout=15)
            guides_resp = f_guides.result(timeout=15)
    except Exception as e:
        print(f'[voice] BFF call failed: {e}', flush=True)
        # Continue with whatever we did get

    # 5) Parse + summarize
    comp = parse_competitive_set(comp_resp or {})
    rows = comp.get('rows', []) or []
    prices = [r['price'] for r in rows
              if r.get('price') and 2000 <= float(r['price']) <= 1_000_000]
    closest_anchor = None
    if miles and rows:
        valid = [r for r in rows
                 if r.get('mileage') and r.get('price')
                 and 2000 <= float(r['price']) <= 1_000_000]
        if valid:
            closest_anchor = min(valid,
                                 key=lambda r: abs(int(r['mileage']) - int(miles)))
            closest_anchor = {
                'price':       int(closest_anchor['price']),
                'mileage':     int(closest_anchor['mileage']),
                'days_on_lot': closest_anchor.get('days_on_lot'),
                'dealer':      closest_anchor.get('dealer'),
                'trim':        closest_anchor.get('trim'),
                'is_certified': closest_anchor.get('is_certified'),
            }

    guides = parse_price_guides(guides_resp or {})
    manheim = guides.get('manheim') or {}

    return {
        'scaffold': {
            'bid_id': bid_row['id'],
            'vin_last4': (bid_row['vin'] or '')[-4:],
            'miles': bid_row['mileage'],
        },
        'live_rbook': {
            'n': len(rows),
            'summary': _summarize([float(p) for p in prices]),
            'closest_anchor': closest_anchor,
            'rows_sample': rows[:5],
        },
        'live_mmr': {
            'average_auction_price': manheim.get('average_auction_price'),
            'average_odometer':      manheim.get('average_odometer'),
        },
        'elapsed_ms': int((_t.time() - t0) * 1000),
    }


# ── Core: YMM-keyed comp context (unchanged from Day 1) ─────────────────
def get_valuation_context(
    year: int,
    make: str,
    model: str,
    trim: Optional[str] = None,
    miles: Optional[int] = None,
    msrp: Optional[int] = None,
    limit_each: int = 30,
) -> dict:
    out: dict[str, Any] = {
        'inputs': {'year': year, 'make': make, 'model': model,
                   'trim': trim, 'miles': miles, 'msrp': msrp},
        # Real-time vAuto BFF — populated below. rBook competitive set
        # + Manheim MMR for this exact YMM+miles. THE primary source.
        'live_vauto': None,
        'mmr_recent': {'rows': [], 'summary_wholesale': _summarize([]),
                       'summary_retail': _summarize([])},
        'lsl_history': {'rows': [], 'summary_sale': _summarize([]),
                        'summary_purchase': _summarize([])},
        # 'prior_bids' = our own per-bid recorded MMR + rBook integers from
        # vauto_lookups. Historical fallback when vAuto is rate-limited or
        # cookies are stale.
        'prior_bids': {'rows': [], 'summary_mmr': _summarize([]),
                       'summary_rbook': _summarize([])},
        # ROW-LEVEL specifics so Sonnet can name actual cars
        'partner_inventory_top3': [],
        'lsl_recent_deals_top3': [],
        'prior_bids_recent_top3': [],
        'errors': [],
    }

    # Normalize once for all downstream queries
    make_n = _norm(make)
    model_n = _norm(model)
    miles_lo, miles_hi = _miles_band(miles)

    # ── Parallel fan-out: all 7 sub-queries fire concurrently. Bounded by
    # the slowest (live_vauto, ~2-3s). Sequential was ~3-4.5s; parallel
    # is ~2-3s. Each worker swallows its own exceptions and returns a
    # (key, value-or-error) tuple so partial failures don't crash the
    # whole context build.
    def _w_live():
        try:
            key = _vauto_cache_key(year, make_n, model_n, miles)
            hit = _vauto_cache_get(key)
            if hit:
                print(f'[voice] vauto-cache HIT {key}', flush=True)
                return ('live_vauto', hit)
            val = _fetch_live_vauto_for_ymm(year, make, model,
                                            miles=miles, trim=trim)
            if val:
                _vauto_cache_put(key, val)
            return ('live_vauto', val)
        except Exception as e:
            return ('live_vauto:err', f'{type(e).__name__}: {e}')

    def _w_partner_inv():
        try:
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT d.name AS dealer_name, d.portal_slug,
                           di.year, di.make, di.model, di.trim, di.mileage,
                           di.price, di.url, di.first_seen_at::date AS first_seen,
                           di.last_seen_at::date AS last_seen,
                           (CURRENT_DATE - COALESCE(di.source_added_at,
                                                    di.first_seen_at)::date) AS days_on_lot
                      FROM dealer_inventory di
                      JOIN dealers d ON d.id = di.dealer_id
                     WHERE di.year = %s
                       AND UPPER(di.make)   = %s
                       AND UPPER(di.model) LIKE %s
                       AND di.status = 'active'
                       AND di.price IS NOT NULL
                     ORDER BY ABS(COALESCE(di.mileage, 0) - %s) ASC,
                              di.last_seen_at DESC
                     LIMIT 3
                """, (year, make_n, f'%{model_n}%', int(miles or 0)))
                rows = [dict(r) for r in cur.fetchall()]
                for r in rows:
                    r['first_seen'] = r['first_seen'].isoformat() if r.get('first_seen') else None
                    r['last_seen']  = r['last_seen'].isoformat()  if r.get('last_seen')  else None
                return ('partner_inventory_top3', rows)
        except Exception as e:
            return ('partner_inv:err', f'{type(e).__name__}: {e}')

    def _w_prior_recent():
        try:
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT b.id AS bid_id, b.year, b.make, b.model, b.trim,
                           b.mileage, b.bid_amount AS our_bid,
                           b.asking_price, b.created_at::date AS bid_date,
                           v.mmr, v.rbook
                      FROM bids b
                      JOIN vauto_lookups v ON v.bid_id = b.id
                     WHERE b.year = %s
                       AND UPPER(b.make)   = %s
                       AND UPPER(b.model) LIKE %s
                       AND b.created_at > NOW() - INTERVAL '90 days'
                     ORDER BY ABS(COALESCE(b.mileage, 0) - %s) ASC,
                              b.created_at DESC
                     LIMIT 3
                """, (year, make_n, f'%{model_n}%', int(miles or 0)))
                rows = [dict(r) for r in cur.fetchall()]
                for r in rows:
                    if r.get('bid_date'):
                        r['bid_date'] = r['bid_date'].isoformat()
                    if r.get('our_bid') is not None:
                        r['our_bid'] = float(r['our_bid'])
                    if r.get('asking_price') is not None:
                        r['asking_price'] = float(r['asking_price'])
                return ('prior_bids_recent_top3', rows)
        except Exception as e:
            return ('prior_bids_recent:err', f'{type(e).__name__}: {e}')

    def _w_lsl_recent():
        try:
            c = _lsl_ro()
            if c is None:
                return ('lsl_recent_deals_top3', [])
            cur = c.cursor()
            cur.execute("""
                SELECT stock_no, vehicle_info, customer_name, supplier_name,
                       sale_price, purchase_cost, front_value, sold_at
                  FROM deals
                 WHERE UPPER(make_name)    LIKE UPPER(?)
                   AND UPPER(vehicle_info) LIKE UPPER(?)
                   AND vehicle_info        LIKE ?
                   AND purchase_cost IS NOT NULL
                   AND purchase_cost > 0
                   AND sold_at > date('now', '-90 days')
                 ORDER BY sold_at DESC LIMIT 3
            """, (f'%{make_n}%', f'%{model_n}%', f'%{year}%'))
            rows = [dict(r) for r in cur.fetchall()]
            c.close()
            return ('lsl_recent_deals_top3', rows)
        except Exception as e:
            return ('lsl_recent:err', f'{type(e).__name__}: {e}')

    def _w_mmr_cache():
        try:
            with _pg_ew() as c, c.cursor() as cur:
                sql = """
                    SELECT vin, year, make, model, trim, mileage, asking_price,
                           wholesale_avg, retail_avg, sample_size, grade,
                           fetched_at
                      FROM dealer_mmr
                     WHERE year = %s
                       AND UPPER(make)   = %s
                       AND UPPER(model) LIKE %s
                       AND wholesale_avg IS NOT NULL
                """
                args = [year, make_n, f'%{model_n}%']
                if miles_lo is not None:
                    sql += "   AND mileage BETWEEN %s AND %s\n"
                    args.extend([miles_lo, miles_hi])
                sql += " ORDER BY fetched_at DESC LIMIT %s"
                args.append(limit_each)
                cur.execute(sql, args)
                rows = [dict(r) for r in cur.fetchall()]
                for r in rows:
                    if r.get('fetched_at'):
                        r['fetched_at'] = r['fetched_at'].isoformat()
                return ('mmr_recent', {
                    'rows': rows,
                    'summary_wholesale': _summarize(
                        [float(r['wholesale_avg']) for r in rows if r.get('wholesale_avg')]),
                    'summary_retail': _summarize(
                        [float(r['retail_avg']) for r in rows if r.get('retail_avg')]),
                })
        except Exception as e:
            return ('mmr_cache:err', f'{type(e).__name__}: {e}')

    def _w_prior_full():
        try:
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT b.id AS bid_id, b.year, b.make, b.model, b.mileage,
                           v.mmr, v.rbook, v.looked_up_at, b.bid_amount
                      FROM bids b
                      JOIN vauto_lookups v ON v.bid_id = b.id
                     WHERE b.year = %s
                       AND UPPER(b.make)   = %s
                       AND UPPER(b.model) LIKE %s
                       AND (v.mmr IS NOT NULL OR v.rbook IS NOT NULL)
                     ORDER BY v.looked_up_at DESC LIMIT %s
                """, (year, make_n, f'%{model_n}%', limit_each * 2))
                rows = [dict(r) for r in cur.fetchall()]
                for r in rows:
                    if r.get('looked_up_at'):
                        r['looked_up_at'] = r['looked_up_at'].isoformat()
                    if r.get('bid_amount') is not None:
                        r['bid_amount'] = float(r['bid_amount'])
                mmrs = [float(r['mmr']) for r in rows if r.get('mmr') and r['mmr'] > 1000]
                rbooks = [float(r['rbook']) for r in rows if r.get('rbook') and r['rbook'] > 1000]
                return ('prior_bids', {
                    'rows': rows[:20],
                    'summary_mmr':   _summarize(mmrs),
                    'summary_rbook': _summarize(rbooks),
                })
        except Exception as e:
            return ('prior_bids:err', f'{type(e).__name__}: {e}')

    def _w_lsl_full():
        try:
            c = _lsl_ro()
            if c is None:
                return ('lsl_history', None)
            cur = c.cursor()
            sql = """
                SELECT stock_no, vin_no, vehicle_info, make_name,
                       sale_price, purchase_cost, front_value, sold_at
                  FROM deals
                 WHERE UPPER(make_name)    LIKE UPPER(?)
                   AND UPPER(vehicle_info) LIKE UPPER(?)
                   AND vehicle_info        LIKE ?
                   AND purchase_cost IS NOT NULL
                   AND purchase_cost > 0
                   AND sold_at > date('now', '-90 days')
                 ORDER BY sold_at DESC LIMIT ?
            """
            args = [f'%{make_n}%', f'%{model_n}%', f'%{year}%', limit_each]
            cur.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
            c.close()
            return ('lsl_history', {
                'rows': rows,
                'summary_purchase': _summarize(
                    [float(r['purchase_cost']) for r in rows if r.get('purchase_cost')]),
                'summary_sale': _summarize(
                    [float(r['sale_price']) for r in rows if r.get('sale_price')]),
            })
        except Exception as e:
            return ('lsl_full:err', f'{type(e).__name__}: {e}')

    def _w_lsl_vs_mmr():
        """Cross-reference: what % of MMR do we typically PAY on this YMM?
        Joins LSL deals (purchase_cost + VIN) against bids+vauto_lookups
        (MMR captured at appraisal time). Returns the historical 'we pay
        X% of wholesale' ratio so Sonnet can project today's bid."""
        try:
            # 1) Get same-YMM deal VINs + prices from LSL
            c = _lsl_ro()
            if c is None:
                return ('lsl_vs_mmr', None)
            cur = c.cursor()
            cur.execute("""
                SELECT vin_no, purchase_cost, sale_price, sold_at
                  FROM deals
                 WHERE UPPER(make_name)    LIKE UPPER(?)
                   AND UPPER(vehicle_info) LIKE UPPER(?)
                   AND vehicle_info        LIKE ?
                   AND purchase_cost > 1000
                   AND vin_no IS NOT NULL AND length(vin_no) = 17
                   AND sold_at > date('now', '-90 days')
                 ORDER BY sold_at DESC LIMIT 200
            """, (f'%{make_n}%', f'%{model_n}%', f'%{year}%'))
            lsl_rows = [{'vin': r[0], 'purchase_cost': float(r[1]),
                         'sale_price': float(r[2] or 0), 'sold_at': r[3]}
                        for r in cur.fetchall()]
            c.close()
            if not lsl_rows:
                return ('lsl_vs_mmr', None)

            vins = [r['vin'] for r in lsl_rows]
            # 2) Pull MMR + rBook at bid time from postgres
            with _pg_ew() as conn, conn.cursor() as cur2:
                cur2.execute("""
                    SELECT b.vin, v.mmr, v.rbook, b.bid_amount
                      FROM bids b
                      JOIN vauto_lookups v ON v.bid_id = b.id
                     WHERE b.vin = ANY(%s)
                       AND (v.mmr IS NOT NULL OR v.rbook IS NOT NULL)
                """, (vins,))
                bid_map = {row['vin']: dict(row) for row in cur2.fetchall()}

            # 3) Compute ratios
            ratios_paid_vs_mmr = []
            ratios_sold_vs_mmr = []
            ratios_paid_vs_rbook = []
            joined_count = 0
            for lr in lsl_rows:
                br = bid_map.get(lr['vin'])
                if not br: continue
                joined_count += 1
                if br.get('mmr') and br['mmr'] > 1000:
                    ratios_paid_vs_mmr.append(lr['purchase_cost'] / br['mmr'])
                    if lr['sale_price'] > 0:
                        ratios_sold_vs_mmr.append(lr['sale_price'] / br['mmr'])
                if br.get('rbook') and br['rbook'] > 1000:
                    ratios_paid_vs_rbook.append(lr['purchase_cost'] / br['rbook'])

            if joined_count == 0:
                return ('lsl_vs_mmr', None)

            def _med(xs): return sorted(xs)[len(xs)//2] if xs else None
            def _avg(xs): return sum(xs)/len(xs) if xs else None
            return ('lsl_vs_mmr', {
                'sample_size': joined_count,
                'avg_paid_pct_of_mmr':   _avg(ratios_paid_vs_mmr),
                'median_paid_pct_of_mmr': _med(ratios_paid_vs_mmr),
                'avg_sold_pct_of_mmr':   _avg(ratios_sold_vs_mmr),
                'avg_paid_pct_of_rbook': _avg(ratios_paid_vs_rbook),
                'sample_pairs': [
                    {'vin': lr['vin'][-6:],
                     'paid': int(lr['purchase_cost']),
                     'sold': int(lr['sale_price']) if lr['sale_price'] else None,
                     'mmr_at_bid': bid_map[lr['vin']].get('mmr'),
                     'rbook_at_bid': bid_map[lr['vin']].get('rbook'),
                     'sold_at': lr['sold_at']}
                    for lr in lsl_rows if lr['vin'] in bid_map
                ][:5],
            })
        except Exception as e:
            return ('lsl_vs_mmr:err', f'{type(e).__name__}: {e}')

    def _w_accutrade():
        """AccuTrade aggregation per YMM (last 90d) — guaranteed_offer
        (AccuTrade's 'we'd pay this' target), trade_in, market_avg,
        and local_comps pool."""
        try:
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT b.id, b.mileage,
                           a.guaranteed_offer, a.trade_in, a.trade_market,
                           a.retail, a.market_avg, a.local_comps
                      FROM bids b
                      JOIN accutrade_lookups a ON a.bid_id = b.id
                     WHERE b.year = %s
                       AND UPPER(b.make)   = %s
                       AND UPPER(b.model) LIKE %s
                       AND (a.not_available IS NOT TRUE OR a.not_available IS NULL)
                       AND b.created_at > NOW() - INTERVAL '90 days'
                """, (year, make_n, f'%{model_n}%'))
                rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                return ('accutrade', None)
            gos = [r['guaranteed_offer'] for r in rows
                   if r.get('guaranteed_offer') and r['guaranteed_offer'] > 1000]
            tis = [r['trade_in'] for r in rows
                   if r.get('trade_in') and r['trade_in'] > 1000]
            mas = [r['market_avg'] for r in rows
                   if r.get('market_avg') and r['market_avg'] > 1000]
            # Flatten local_comps across bids
            local_pool = []
            seen_vins = set()
            for r in rows:
                lc = r.get('local_comps')
                if not lc: continue
                comps = lc.get('rows') if isinstance(lc, dict) else (lc if isinstance(lc, list) else [])
                for c_ in (comps or []):
                    if not isinstance(c_, dict): continue
                    vin = (c_.get('vin') or '').strip().upper() or None
                    if vin and vin in seen_vins: continue
                    if vin: seen_vins.add(vin)
                    price = c_.get('price') or c_.get('listing_price')
                    if not price: continue
                    try: price = int(float(price))
                    except Exception: continue
                    if not (2000 <= price <= 1_000_000): continue
                    local_pool.append({
                        'vin': vin, 'price': price,
                        'mileage': c_.get('mileage') or c_.get('odometer'),
                        'days_on_lot': c_.get('days_on_lot') or c_.get('dol'),
                        'dealer': c_.get('dealer') or c_.get('dealer_name'),
                    })
            local_pool.sort(key=lambda r: abs((r.get('mileage') or 0) - (miles or 0)))
            return ('accutrade', {
                'pool_count':      len(rows),
                'avg_guaranteed':  int(sum(gos) / len(gos)) if gos else None,
                'avg_trade_in':    int(sum(tis) / len(tis)) if tis else None,
                'avg_market':      int(sum(mas) / len(mas)) if mas else None,
                'local_comps_top': local_pool[:20],
            })
        except Exception as e:
            return ('accutrade:err', f'{type(e).__name__}: {e}')

    from concurrent.futures import ThreadPoolExecutor as _TPE
    from concurrent.futures import wait as _futwait, FIRST_EXCEPTION as _FE
    _workers = [_w_live, _w_partner_inv, _w_prior_recent, _w_lsl_recent,
                _w_mmr_cache, _w_prior_full, _w_lsl_full, _w_accutrade,
                _w_lsl_vs_mmr]
    # HARD 8s budget: any worker that hasn't returned by then is abandoned
    # (returns ("<key>:timeout", None) so the rest of the pipeline still
    # gets the data that DID come back). Fixes the 60s "stacked timeouts"
    # on cold cache misses (e.g. 2026-05-25 Toyota Highlander Platinum).
    import time as _tperf
    _t_fanout = _tperf.monotonic()
    _ex = _TPE(max_workers=len(_workers))
    _futs = {_ex.submit(fn): fn for fn in _workers}
    done, not_done = _futwait(_futs.keys(), timeout=8.0)
    results = []
    for f in done:
        try:
            results.append(f.result())
        except Exception as e:
            results.append(("worker_exception", f"{type(e).__name__}: {e}"))
    for f in not_done:
        # Worker didn't finish in 8s. Try to cancel; collect a sentinel.
        worker_name = getattr(_futs[f], "__name__", "?")
        try:
            f.cancel()
        except Exception:
            pass
        results.append((f"{worker_name}:timeout", None))
    _ex.shutdown(wait=False)
    _elapsed_ms = int((_tperf.monotonic() - _t_fanout) * 1000)
    out.setdefault("errors", []).append(f"fanout_elapsed_ms={_elapsed_ms}, abandoned={len(not_done)}")
    for key, value in results:
        if ':err' in key:
            out['errors'].append(f'{key}: {value}')
        elif key == 'live_vauto':
            if value:
                out['live_vauto'] = value
        elif key == 'accutrade':
            if value:
                out['accutrade'] = value
        elif key == 'lsl_vs_mmr':
            if value:
                out['lsl_vs_mmr'] = value
        elif key in ('mmr_recent', 'lsl_history', 'prior_bids') and value:
            out[key] = value
        elif key in ('partner_inventory_top3', 'prior_bids_recent_top3',
                     'lsl_recent_deals_top3'):
            out[key] = value or []


    # Naive blended target (sanity baseline; Sonnet synthesizes the real one).
    # Prefers live vAuto when available; falls back to prior_bids / LSL.
    live = out.get('live_vauto') or {}
    live_mmr_avg = (live.get('live_mmr') or {}).get('average_auction_price')
    live_rbook_med = (live.get('live_rbook') or {}).get('summary', {}).get('median')
    mmr_med = out['mmr_recent']['summary_wholesale'].get('median')
    pb_mmr_med = out['prior_bids']['summary_mmr'].get('median')
    pb_rb_med  = out['prior_bids']['summary_rbook'].get('median')
    lsl_med = out['lsl_history']['summary_purchase'].get('median')
    naive_target = None
    wts: list[tuple[str, float, float]] = []
    if live_mmr_avg:   wts.append(('live_mmr',  live_mmr_avg, 0.45))
    if live_rbook_med: wts.append(('live_rb_x0.92', live_rbook_med * 0.92, 0.20))
    if pb_mmr_med:     wts.append(('prior_mmr', pb_mmr_med, 0.15))
    if mmr_med:        wts.append(('mmr_cache', mmr_med, 0.10))
    if lsl_med:        wts.append(('lsl',       lsl_med, 0.10))
    total_w = sum(w for _, _, w in wts)
    if total_w:
        naive_target = int(sum(v * w for _, v, w in wts) / total_w)
    out['naive_target'] = naive_target

    return out


# ── Gemini Flash: parse spoken transcript → YMM ─────────────────────────
_PARSE_PROMPT = """\
You are EW, a wholesale-vehicle valuation assistant. Parse this spoken query
into a JSON object describing the vehicle. Return ONLY JSON, no prose.

Schema (all string/integer; use null when unknown):
{
  "year":    integer or null,         // 4-digit
  "make":    string or null,          // canonical brand: "GMC", "Mercedes-Benz", "Land Rover", "BMW"
  "model":   string or null,          // base model: "Hummer", "GLC-Class", "Range Rover"
  "trim":    string or null,          // e.g. "3X", "AMG", "Dynamic SE"
  "miles":   integer or null,         // raw odometer
  "msrp":    integer or null,         // sticker price if mentioned
  "notes":   string or null,          // condition/damage/options as a short phrase
  "intent":  "valuation"|"refine"|"vin"|"sms"|"unknown",
  "missing_fields": ["year"|"make"|"model"|"miles"]   // ONLY required fields the user didn't supply; empty array if all present
}

Rules:
- If the transcript clearly names a year + make + model, intent="valuation" and missing_fields=[].
- If user says "what about [N] miles" or otherwise references a prior car, intent="refine" and only fill the changed field(s).
- If user reads a 17-character VIN, intent="vin" and put the VIN in notes.
- If user asks to text/send the report, intent="sms".
- Speech-to-text often mangles model names: "GLC" might come through as "G L C", "M5" as "M 5", "Hummer EV" as "Humor EV". Normalize.
- Make should match canonical brand names used in dealer feeds. Examples:
  * "GMC" not "G M C"
  * "Mercedes-Benz" not "Mercedes" or "Benz"
  * "Land Rover" not "Range Rover" (Range Rover is a model)
  * "BMW" not "B M W"
- Required fields for a valuation: year, make, model. Miles is helpful but not required.

Transcript: "{transcript}"
"""


def _load_session_history(session_id: Optional[str], limit: int = 5) -> list[dict]:
    """Pull the last N turns in this session for multi-turn context.
    Lets the LLM resolve refinement references ('what about 25k miles')."""
    if not session_id:
        return []
    try:
        with _pg_ew() as c, c.cursor() as cur:
            cur.execute("""
                SELECT raw_transcript, parsed_year, parsed_make, parsed_model,
                       parsed_trim, parsed_miles, parsed_msrp, reasoning,
                       target_buy, range_low, range_high, confidence,
                       turn_index, created_at
                  FROM voice_valuations
                 WHERE session_id = %s
                 ORDER BY created_at DESC
                 LIMIT %s
            """, (session_id, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f'[voice] history load err: {e}', flush=True)
        return []


def _history_block(history: list[dict]) -> str:
    """Compact prior-turns block for prompts (chronological order)."""
    if not history:
        return ""
    lines = ["", "PRIOR TURNS IN THIS CONVERSATION (oldest first):"]
    for r in reversed(history):
        ymm = ' '.join(str(x) for x in [r.get('parsed_year'), r.get('parsed_make'),
                                         r.get('parsed_model'), r.get('parsed_trim')] if x)
        miles = f"{r['parsed_miles']:,}mi" if r.get('parsed_miles') else ''
        msrp = f"MSRP ${r['parsed_msrp']:,}" if r.get('parsed_msrp') else ''
        target = f" → target ${r['target_buy']:,}" if r.get('target_buy') else ''
        user_line = (r.get('raw_transcript') or '').strip()
        lines.append(f'- user: "{user_line}"')
        if ymm or miles or msrp:
            lines.append(f"  parsed: {ymm} {miles} {msrp}{target}".rstrip())
    return '\n'.join(lines) + '\n'


def _parse_transcript(transcript: str, history: list[dict] = None) -> dict:
    """Parse a spoken vehicle query. Carries prior-turn context so
    refinement queries ('what about 25k miles') keep the same vehicle.

    HISTORY_RESET_2026_05_21 — when the transcript clearly names a NEW
    make+model that differs from the most recent prior turn, drop history.
    Otherwise Gemini stays anchored to the previous car.
    """
    from app import gemini_call  # lazy to avoid circular
    # Fresh-YMM detection: if quick parse extracts make+model and the
    # most recent prior turn has a different make+model, history is stale.
    if history:
        try:
            q = _quick_parse_ymm(transcript) or {}
            new_mk = (q.get('make') or '').strip().lower()
            new_mdl = (q.get('model') or '').strip().lower()
            prior = history[0] if history else {}
            old_mk = (prior.get('parsed_make') or '').strip().lower()
            old_mdl = (prior.get('parsed_model') or '').strip().lower()
            if new_mk and new_mdl and old_mk and old_mdl:
                if new_mk != old_mk or new_mdl != old_mdl:
                    print(f'[voice] history reset: new "{new_mk} {new_mdl}" '
                          f'!= prior "{old_mk} {old_mdl}"', flush=True)
                    history = []
        except Exception as _e:
            print(f'[voice] history-reset check err: {_e}', flush=True)
    prompt = _PARSE_PROMPT.replace('{transcript}', transcript)
    if history:
        prompt += _history_block(history)
    raw = gemini_call(
        prompt,
        model='gemini-3.5-flash',
        max_tokens=500,
        temperature=0.2,
        disable_thinking=True,
    )
    parsed = _strip_json(raw or '') or {}
    parsed.setdefault('year', None)
    parsed.setdefault('make', None)
    parsed.setdefault('model', None)
    parsed.setdefault('trim', None)
    parsed.setdefault('miles', None)
    parsed.setdefault('msrp', None)
    parsed.setdefault('notes', None)
    parsed.setdefault('intent', 'unknown')
    parsed.setdefault('missing_fields', [])
    # MODEL_STOPWORDS_2026_05_21 — Gemini Flash often leaks trailing query
    # words into the model field ("G63 Worth", "M2 Value"). Strip them.
    _STOP_TAIL = re.compile(
        r'\s*\b(worth|value|price|cost|trade|trade[-\s]?in|appraisal|that|please|please?)\b\s*\??\s*$',
        re.IGNORECASE)
    for k in ('model', 'trim'):
        v = parsed.get(k)
        if isinstance(v, str):
            cleaned = _STOP_TAIL.sub('', v).strip()
            parsed[k] = cleaned or None
    return parsed


# ── Gemini Flash: synthesize spoken reply from comp context ─────────────
_REPLY_PROMPT = """\
You are EW, a senior wholesale-vehicle buyer speaking out loud to an operator
who just asked for a valuation. Write a SHORT spoken response (≤ 65 words, no
bullet points, no markdown, no asterisks — this will be read aloud by TTS).

Vehicle: {year} {make} {model} {trim_part}{miles_part}{msrp_part}
Operator notes: {notes_or_none}

Comp context (use what's available; if a source is empty, don't mention it):

MMR wholesale (our cache, recent):
  rows: {mmr_n}   median: {mmr_median}   p25: {mmr_p25}   p75: {mmr_p75}

National asking (1.27M active listings):
  rows: {nat_n}   median: {nat_median}   p25: {nat_p25}   p75: {nat_p75}

Our prior LSL purchases on this YMM (multiple years of history — possibly old):
  rows: {lsl_n}   median paid: {lsl_purch_median}   median sold: {lsl_sale_median}

Naive blended target (sanity baseline only): {naive_target}

REASONING GUIDANCE (apply judgment — comps alone may not be enough):

1. ANCHOR ON MSRP when given. For near-new (≤5K miles) vehicles, wholesale typically lands at:
     - Mainstream brands: ~75-82% of MSRP
     - Luxury (BMW 7, Mercedes S, etc.): ~80-90% of MSRP
     - Exotics / low-volume (McLaren, Lambo, Aston, Ferrari, Porsche GT): ~70-85% of MSRP
   Apply additional 5-10% off per year of age. Apply additional 1% off per 10K miles past 10K.

2. WEIGHT RECENT > OLD. LSL purchase history spans 5+ years — a 2017 car's median-paid
   could reflect 2018 prices, NOT today's market. If LSL is the only signal and the
   vehicle is >3 years old, discount your reliance on it.

3. NATIONAL ASKING is retail, not wholesale. Wholesale target is typically 88-93% of
   national asking median for mainstream, 85-90% for luxury/exotic.

4. MMR is the gold standard when present. If MMR sample ≥3 rows, anchor there primarily.

5. When confidence is genuinely low (no MMR + thin LSL + thin national), say so AND
   give a wider range (range_high - range_low ≥ 10% of target_buy).

OUTPUT FORMAT — first line MUST be JSON on a single line:
{{"target_buy":NNNNN,"range_low":NNNNN,"range_high":NNNNN,"confidence":"low"|"medium"|"high"}}

Then a blank line, then the spoken response (≤ 65 words) that EW will speak.
The spoken response should:
- Lead with the target buy number ("Target buy around one-oh-five.").
- Cite ONE concrete anchor: an MSRP percentage, MMR median, or national-asking spread.
- If confidence is low, name the gap ("thin comp set, leaning on MSRP anchor").
- End with: "Want the full appraisal? I'll need the VIN."

DO NOT include "$" or commas in the spoken response — TTS reads numbers better as words.
Use natural numbers ("one hundred five thousand"). Round to nearest 500.
"""


def _format_money(n: Optional[float]) -> str:
    return f'${int(n):,}' if n is not None else 'n/a'


def _call_claude_sonnet(prompt: str, max_tokens: int = 1500) -> Optional[str]:
    """Single-shot Sonnet call (kept for legacy paths)."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        resp = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=max_tokens,
            temperature=0.3,
            messages=[{'role': 'user', 'content': prompt}],
        )
        if not resp.content:
            return None
        return resp.content[0].text.strip()
    except Exception as e:
        print(f'[voice] sonnet call failed: {type(e).__name__}: {e}', flush=True)
        return None


# ── Conversational Sonnet with tool use ─────────────────────────────────
_SYSTEM_PROMPT = """\
You are EW, a senior wholesale-vehicle buyer talking to your colleague (the operator)
over a hands-free voice link. Your tone is direct, fluent, and casual — like two
buyers at the auction lane swapping notes. The operator's voice is being transcribed,
your replies are being read aloud by TTS.

Hard constraints (ignore at your peril):
- Output is SPOKEN. No markdown, no bullets, no asterisks, no headers.
- TARGET: 55-80 words across FOUR substantive sentences (see required
  structure below). Going UNDER four sentences is a failure.
- Lead the FIRST sentence with the target number. No preamble.
- Numbers spoken: "one-ten" / "eighty-eight five" — no $ or commas.
REQUIRED REPLY STRUCTURE — four sentences, each one MANDATORY:

  Sentence 1: The target. e.g., "I'd be at one-sixty-eight."
  Sentence 2: MMR average + average odometer. e.g., "MMR's at one-seventy-four
              with average miles of twenty thousand."
              (Pull MMR from live_vauto_mmr.average_auction_price and
              average_odometer. NOTE: live_vauto_mmr has NO sample size
              field — it's an aggregate from vAuto. DO NOT invent a
              count like "on twelve sales" — vAuto only returns the avg.
              If you need a sample size, cite prior_bids_mmr.n which IS
              the number of bids we've made on this YMM.)
  Sentence 3: Retail comp depth + days-on-lot. e.g., "Two-forty-nine retail
              listings, median sixty-eight days on lot — soft market."
              CRITICAL: live_vauto_rbook.n is the count of RETAIL LISTINGS
              from rBook, NOT auction sales. Always call them "retail
              listings" or just "listings" — never "auction sales".
              (Pull n from live_vauto_rbook.n; DOL from
              rbook_aggregated_pool.median_dol or
              dealer_inventory_full.active_dol_p50.)
  Sentence 4: Our own history OR specific anchor. e.g., "We've bought
              eighteen of these in the last 90 days, last one at one-
              seventy-three, sold at one-seventy-eight five." or
              "ABZ Motors has one at twenty-nine K miles asking one-
              eighty-four, sat for forty-two days."

NEVER skip a sentence. If a data point is missing, say so explicitly
("no live MMR" / "no prior bids") rather than dropping the sentence.

COUNTS ARE NON-NEGOTIABLE — but use the RIGHT count for the RIGHT thing:
  - rBook listings → "X retail listings" (use live_vauto_rbook.n)
  - Our prior bids on this YMM → "we've bid X of these" (use prior_bids_mmr.n)
  - Our LSL purchases → "we've bought X" (use lsl_purchase.n)
  - Manheim auction sales → ONLY if manheim_auction_pool.sale_count > 0
  - Live MMR → "MMR's at X with avg miles of Y" — DO NOT cite a sample
    size for live_vauto_mmr (it doesn't have one)

GROUND YOUR TARGET in real data — never guess. Acceptable buy = MMR ×
historical_pay_ratio.median_paid_pct_of_mmr (if available, e.g., 0.96)
adjusted for mileage delta. If no pay ratio, target = MMR ± mileage adjust
(roughly ±$1K per 5K miles of delta from average odometer). Do NOT go
more than 10% under MMR without specific justification (LSL avg paid,
soft market, etc.).
- Only go longer when the operator explicitly asks why / explain / tell me more.
- Never force a closing phrase. End when you're done.

CALL `get_valuation_context` IMMEDIATELY when the operator gives you year + make
+ model. Don't ask for trim or MSRP first — call the tool, see what you got,
THEN decide if you need more info. Trim/MSRP are optional refinements.

The tool returns:

PRIMARY signals (real-time vAuto BFF query, ~2-4s; populated when we've ever
bid on this YMM before so the system can scaffold the appraisalId):
  - live_vauto_rbook — TODAY's competitive set from vAuto: n listings, summary
    (median/p25/p75 of retail asking), and closest_mile_anchor (the single
    listing whose mileage is closest to the operator's). Use the anchor as
    your specific reference point, the summary for spread.
  - live_vauto_mmr — TODAY's Manheim MMR auction average + average odometer.
    THE wholesale anchor. Subtract a couple grand if your subject is at
    HIGHER miles than mmr.average_odometer; add a couple if lower.

FALLBACK signals (used when live_vauto returns null — usually means we've
never bid on this YMM, or cookies are stale):
  - prior_bids_mmr / prior_bids_rbook — historical MMR + rBook integers from
    every car of this YMM we've ever bid on
  - mmr_cache_wholesale — older cached MMR for partner-portal inventory
  - lsl_purchase / lsl_sale — what WE'VE paid + sold over 5 years

HISTORICAL PAY RATIO (when present) — our own track record:
  - historical_pay_ratio.sample_size — number of CLOSED LSL deals where
    we also have the MMR captured at bid time
  - historical_pay_ratio.median_paid_pct_of_mmr — the typical % of MMR
    we actually paid. e.g., 0.94 means "we paid 94% of wholesale on average."
  - historical_pay_ratio.avg_sold_pct_of_mmr — what % of MMR we resold for
  - historical_pay_ratio.sample_pairs — actual deal pairs (paid vs MMR at bid)
  USE THIS — multiply today's live MMR × median_paid_pct_of_mmr to ground
  your target in our own buying behavior. Example:
  "We've historically paid 94% of MMR on these — at one-seventy-four MMR,
  that puts us at one-sixty-three to one-sixty-five."

ACCUTRADE pool (when present) — JD Power AccuTrade's per-VIN appraisal data
aggregated across our 90-day bid history:
  - accutrade_pool.appraisal_count — how many AccuTrade appraisals we have
  - accutrade_pool.avg_guaranteed_offer — AccuTrade's "this is what we'd
    PAY at the lane" number, averaged. Sharp wholesale anchor. Cite when
    present: "AccuTrade guarantees one-sixty on these on average."
  - accutrade_pool.avg_market_value — AccuTrade's market estimate
  - accutrade_pool.local_comps_top — local dealer comps AccuTrade pulled

RICH POOLS — when present (from master cache), these are aggregations
across ALL our recent appraisals for this exact YMM. Lean on these for
sharper anchors than the per-bid `live_vauto_*` summaries:

  - rbook_aggregated_pool.comp_count — total unique retail listings we've
    seen across our last 90d of appraisals. comp_count > 100 means a
    deep market — your range can be tighter.
  - rbook_aggregated_pool.median_dol — typical days-on-lot for retail
    comps. >60 days = soft market; <30 = hot.
  - rbook_aggregated_pool.certified_pct / pending_pct — share of comps
    that are CPO / already pending. Pending >20% means cars are moving.
  - rbook_aggregated_pool.top_comps_closest_mile — top 20 closest-mile
    listings with dealer, price, miles, DOL. Cite when one's striking.
  - manheim_auction_pool — actual wholesale auction sales. median_sale,
    avg_condition (Manheim CR grade 1-5), recent_sales array with
    individual sales. CITE these — "Manheim sold one last week at X."
  - dealer_inventory_full — every same-YMM car our 15 daily-scanned
    partner dealers currently have, plus what they sold in 30d, plus
    active price drops. Use to cite: "Charlie has one at X for Y days"
    or "Partners sold 3 in the last 30 at avg X."

ROW-LEVEL specifics — surface these by NAME when they're informative:
  - partner_inventory_top3: same-YMM cars on partner dealer lots TODAY.
    When a closest-mile match has been sitting >30 days, mention it by name:
    "TXT Charlie's been sitting on a fifteen-thousand-mile one at sixty-five
    for forty-two days." Use dealer_name + days_on_lot + price + miles.
  - lsl_recent_deals_top3: cars WE'VE actually bought in the last 60 days
    with sold_at + purchase_cost + vehicle_info. Cite when relevant:
    "we bought one last month at fifty-nine, sold for sixty-two five."
  - prior_bids_recent_top3: bids we MADE on this YMM in the last 90 days
    with bid_date + our_bid + miles + mmr + rbook. Cite when relevant:
    "we bid one a few weeks back at sixty-one with twelve K miles."

Don't dump all three sources every turn — pick the one that's most useful
for this specific question and weave it in conversationally.

How to value:
  1. If live_vauto_mmr.average_auction_price exists, anchor there. Adjust for
     mileage delta vs live_vauto_mmr.average_odometer.
  2. Cross-check with live_vauto_rbook.closest_mile_anchor — that's a real
     listing at near-target miles.
  3. Wholesale target ≈ MMR avg with mileage adjustment (or rBook closest-mile
     minus ~10-12% for the retail-to-wholesale gap).
  4. If live_vauto IS null AND prior_bids/lsl/cache also thin, ASK for the
     VIN. Frame it as: you need a VIN to pull good data, do they have one
     handy? Example: "I can't pull live comps on that one without a VIN —
     you got it on hand? Just read it off and I'll get you a real number."
     When the operator reads the VIN, the system queries vAuto LIVE with
     that VIN (no bid created yet). You'll see the data and answer with
     a fresh target. Then you offer the FULL appraisal (which pulls
     AccuTrade + iPacket + Carfax + the AI report) as a follow-up.
  5. If live_vauto IS null but prior_bids/lsl HAS data, use that history and
     say so explicitly ("historical signal only — couldn't reach live vAuto").

Re-call the tool when the operator changes a material spec (miles, trim).

How to value (judgment, not a formula):
- Anchor on MMR median if you have ≥3 comps.
- If MMR is thin, anchor on MSRP × depreciation: ~80-90% of sticker on near-new
  luxury, 70-85% on exotics, less per year of age + ~1% per 10K miles past 10K.
- National asking is retail; subtract ~10-12% to ballpark wholesale.
- LSL data spans 5 years — for cars older than 3 model years, discount how much
  you lean on it.
- Be honest about uncertainty. "Tight comp set" / "I'd want to see photos" /
  "stretch a couple grand for low miles."

When you have a target, just say it ("I'd be at one-oh-five for that one — MMR's
wholesale-averaging eighty-nine, but at twenty-five hundred miles it's basically
new"). When you don't, ask what you'd need ("how many miles? any options?").

If the operator reads a VIN, the system has a separate path that fires the full
appraisal pipeline — don't try to value from the VIN yourself; just acknowledge
that the full report is running.

Be a buyer, not a calculator."""


_TOOL_GET_CONTEXT = {
    "name": "get_valuation_context",
    "description": (
        "Pulls live MMR wholesale + national asking + LSL purchase history "
        "for a year/make/model. Returns counts + medians/quartiles. Call "
        "this when you have enough vehicle info to value (YMM required, "
        "miles helpful). If the tool returns thin data, try a broader "
        "model name (e.g., '760i' returned nothing → try '7 Series')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "year":  {"type": "integer", "description": "4-digit model year"},
            "make":  {"type": "string",  "description": "Canonical brand: 'GMC', 'BMW', 'Mercedes-Benz', 'Land Rover', 'McLaren'"},
            "model": {"type": "string",  "description": "Model name AS THE OPERATOR SAID IT — don't canonicalize. If they say '760i' pass '760i', if they say 'M2' pass 'M2', if they say 'Hummer EV' pass 'Hummer EV'. The DB stores models with the specific sub-trim, so 'M2' will find prior M2 bids but '2 Series' will not."},
            "trim":  {"type": "string"},
            "miles": {"type": "integer"},
            "msrp":  {"type": "integer"},
        },
        "required": ["year", "make", "model"],
    },
}


def _context_compact(ctx: dict) -> dict:
    """Strip the full row lists down to summary stats the LLM can reason
    over without burning tokens on hundreds of comp rows."""
    pb = ctx.get('prior_bids') or {}
    live = ctx.get('live_vauto')
    out: dict = {
        "inputs": ctx.get("inputs", {}),
        # FALLBACK historical signals (densest when YMM is something we
        # bid on often)
        "prior_bids_mmr":   pb.get("summary_mmr",   _summarize([])),
        "prior_bids_rbook": pb.get("summary_rbook", _summarize([])),
        "mmr_cache_wholesale": ctx["mmr_recent"]["summary_wholesale"],
        "lsl_purchase":    ctx["lsl_history"]["summary_purchase"],
        "lsl_sale":        ctx["lsl_history"]["summary_sale"],
        "naive_target":    ctx.get("naive_target"),
        "errors":          ctx.get("errors") or [],
    }
    # PRIMARY: real-time vAuto. Surface separately and prominently.
    if live:
        out["live_vauto_rbook"] = {
            "n":              live["live_rbook"]["n"],
            "summary":        live["live_rbook"]["summary"],
            "closest_mile_anchor": live["live_rbook"]["closest_anchor"],
        }
        out["live_vauto_mmr"] = {
            "average_auction_price": live["live_mmr"]["average_auction_price"],
            "average_odometer":      live["live_mmr"]["average_odometer"],
        }
        out["live_vauto_meta"] = {
            "scaffold_bid_id": live["scaffold"]["bid_id"],
            "scaffold_miles":  live["scaffold"]["miles"],
            "elapsed_ms":      live.get("elapsed_ms"),
        }
    else:
        out["live_vauto_rbook"] = None
        out["live_vauto_mmr"]   = None

    # ROW-LEVEL specifics so Sonnet can name actual cars / dealers / dates.
    out["partner_inventory_top3"]  = ctx.get("partner_inventory_top3")  or []
    out["lsl_recent_deals_top3"]   = ctx.get("lsl_recent_deals_top3")   or []
    out["prior_bids_recent_top3"]  = ctx.get("prior_bids_recent_top3")  or []

    # ── v2 master rich data (only present when source=voice_ymm_master) ─
    if ctx.get('rbook_pool'):
        rp = ctx['rbook_pool']
        out['rbook_aggregated_pool'] = {
            'comp_count':    rp.get('count'),
            'median_dol':    rp.get('median_dol'),
            'certified_pct': rp.get('certified_pct'),
            'pending_pct':   rp.get('pending_pct'),
            'top_comps_closest_mile': rp.get('top_comps') or [],
        }
    if ctx.get('manheim_pool'):
        mp = ctx['manheim_pool']
        out['manheim_auction_pool'] = {
            'sale_count':    mp.get('count'),
            'median_sale':   mp.get('median_sale'),
            'avg_condition': mp.get('avg_condition'),
            'last_sale_at':  mp.get('last_sale_at'),
            'recent_sales':  mp.get('recent_sales') or [],
        }
    if ctx.get('dealer_inv'):
        di = ctx['dealer_inv']
        out['dealer_inventory_full'] = {
            'active_listings':   di.get('active_full') or [],
            'sold_last_30d':     di.get('sold_recent') or [],
            'price_drops':       di.get('price_drops') or [],
            'active_dol_min':    di.get('active_dol_min'),
            'active_dol_p50':    di.get('active_dol_p50'),
            'active_dol_max':    di.get('active_dol_max'),
            'sold_avg_dol':      di.get('sold_avg_dol'),
        }
    # ── LSL-vs-MMR ratio — what % of wholesale we typically PAY on this YMM ─
    if ctx.get('lsl_vs_mmr'):
        lvm = ctx['lsl_vs_mmr']
        out['historical_pay_ratio'] = {
            'sample_size':       lvm.get('sample_size'),
            'avg_paid_pct_of_mmr':    lvm.get('avg_paid_pct_of_mmr'),
            'median_paid_pct_of_mmr': lvm.get('median_paid_pct_of_mmr'),
            'avg_sold_pct_of_mmr':    lvm.get('avg_sold_pct_of_mmr'),
            'sample_pairs':           lvm.get('sample_pairs') or [],
        }
    # AccuTrade aggregated across all our prior bids (last 90d)
    if ctx.get('accutrade'):
        at = ctx['accutrade']
        out['accutrade_pool'] = {
            'appraisal_count':   at.get('pool_count'),
            'avg_guaranteed_offer': at.get('avg_guaranteed'),  # what AccuTrade says we'd PAY
            'avg_trade_in':      at.get('avg_trade_in'),
            'avg_market_value':  at.get('avg_market'),
            'local_comps_count': len(at.get('local_comps_top') or []),
            'local_comps_top':   at.get('local_comps_top') or [],
        }
    return out


_QUICK_MAKES = {
    'BMW': 'BMW', 'GMC': 'GMC', 'MERCEDES': 'Mercedes-Benz',
    'MERCEDES-BENZ': 'Mercedes-Benz', 'BENZ': 'Mercedes-Benz',
    'HONDA': 'Honda', 'FORD': 'Ford', 'CHEVY': 'Chevrolet',
    'CHEVROLET': 'Chevrolet', 'TOYOTA': 'Toyota', 'TESLA': 'Tesla',
    'AUDI': 'Audi', 'VW': 'Volkswagen', 'VOLKSWAGEN': 'Volkswagen',
    'PORSCHE': 'Porsche', 'FERRARI': 'Ferrari', 'LAMBORGHINI': 'Lamborghini',
    'LAMBO': 'Lamborghini', 'MCLAREN': 'McLaren', 'ASTON': 'Aston Martin',
    'BENTLEY': 'Bentley', 'ROLLS': 'Rolls-Royce', 'ROLLS-ROYCE': 'Rolls-Royce',
    'LAND': 'Land Rover', 'RANGE': 'Land Rover', 'JEEP': 'Jeep', 'RAM': 'Ram',
    'LEXUS': 'Lexus', 'INFINITI': 'INFINITI', 'ACURA': 'Acura',
    'NISSAN': 'Nissan', 'MAZDA': 'Mazda', 'SUBARU': 'Subaru',
    'HYUNDAI': 'Hyundai', 'KIA': 'Kia', 'GENESIS': 'Genesis',
    'VOLVO': 'Volvo', 'CADILLAC': 'Cadillac', 'CADDY': 'Cadillac',
    'LINCOLN': 'Lincoln', 'BUICK': 'Buick', 'DODGE': 'Dodge',
    'CHRYSLER': 'Chrysler', 'MITSUBISHI': 'Mitsubishi', 'MINI': 'Mini',
    'MASERATI': 'Maserati', 'ALFA': 'Alfa Romeo', 'JAGUAR': 'Jaguar',
}
_QUICK_STOP_WORDS = {'WITH', 'MILES', 'MILE', 'MI', 'MSRP', 'STICKER',
                     'WINDOW', 'CASH', 'OFFER', 'ASKING', 'AT', 'FOR'}


def _quick_parse_ymm(transcript: str) -> Optional[dict]:
    """Cheap regex/keyword parse of a transcript. Returns dict with
    year/make/model/miles/msrp if all of year+make+model are confidently
    extracted, else None. Lets us skip Sonnet's tool-decision step when
    YMM is unambiguous — saves 2-4s per valuation turn."""
    if not transcript:
        return None
    upper = transcript.upper()

    # Year (4-digit, 1980-2029)
    m_yr = re.search(r'\b(19[89]\d|20[0-2]\d)\b', upper)
    if not m_yr:
        return None
    year = int(m_yr.group(1))

    # Tokenize, scan for first make keyword
    tokens = re.findall(r"[A-Z0-9][A-Z0-9'\-]*", upper)
    make_canonical = None
    model_start = None
    for i, t in enumerate(tokens):
        if t in _QUICK_MAKES:
            make_canonical = _QUICK_MAKES[t]
            # Multi-word make handling
            if t in ('MERCEDES',) and i+1 < len(tokens) and tokens[i+1] == 'BENZ':
                model_start = i + 2
            elif t in ('LAND', 'RANGE') and i+1 < len(tokens) and tokens[i+1] == 'ROVER':
                # "Range Rover" is part of the model (e.g., "Range Rover Sport"); make=Land Rover
                model_start = i if t == 'RANGE' else (i + 2)
            elif t == 'ASTON' and i+1 < len(tokens) and tokens[i+1] == 'MARTIN':
                model_start = i + 2
            elif t == 'ALFA' and i+1 < len(tokens) and tokens[i+1] == 'ROMEO':
                model_start = i + 2
            elif t == 'ROLLS' and i+1 < len(tokens) and tokens[i+1] == 'ROYCE':
                model_start = i + 2
            else:
                model_start = i + 1
            break
    if not make_canonical or model_start is None:
        return None

    # Model: 1-4 tokens after the make, stopping at filler/numeric tokens
    model_tokens = []
    for j in range(model_start, min(model_start + 4, len(tokens))):
        t = tokens[j]
        if t in _QUICK_STOP_WORDS:
            break
        if t.isdigit() and len(t) >= 4:  # likely miles/msrp/year
            break
        # Preserve all-caps short tokens (M2, X5, 760i, GLE) as-is;
        # title-case longer words
        if len(t) <= 4 or t.upper() == t and any(c.isdigit() for c in t):
            model_tokens.append(t)
        else:
            model_tokens.append(t.title())
    if not model_tokens:
        return None
    model = ' '.join(model_tokens)

    # Miles
    miles = None
    m_mi = re.search(r'(\d{1,3}[,\s]?\d{3}|\d{4,6})\s*(?:k|thousand|miles|mile|mi)\b',
                     transcript, re.I)
    if m_mi:
        n = int(re.sub(r'\D', '', m_mi.group(1)))
        token = m_mi.group(0).lower()
        if ('k' in token or 'thousand' in token) and n < 1000:
            n *= 1000
        if 100 <= n <= 999999:
            miles = n

    # MSRP / window sticker
    msrp = None
    m_ms = re.search(
        r'(?:MSRP|sticker|window\s+sticker)\s+(?:of\s+|at\s+)?[$]?'
        r'(\d{1,3}[,\s]?\d{3}|\d{4,7})\s*(?:k|thousand)?',
        transcript, re.I,
    )
    if m_ms:
        n = int(re.sub(r'\D', '', m_ms.group(1)))
        tok = m_ms.group(0).lower()
        if ('k' in tok or 'thousand' in tok) and n < 1000:
            n *= 1000
        if 1000 <= n <= 9999999:
            msrp = n

    return {'year': year, 'make': make_canonical, 'model': model,
            'miles': miles, 'msrp': msrp}


def _build_message_history(history: list[dict], current_user_text: str) -> list[dict]:
    """Reshape prior voice_valuations turns into Anthropic chat messages."""
    msgs = []
    # Replay oldest → newest. We have user transcripts + bot replies (reasoning).
    for r in reversed(history):
        user = (r.get('raw_transcript') or '').strip()
        if user:
            msgs.append({"role": "user", "content": user})
        bot = (r.get('reasoning') or '').strip()
        if bot:
            msgs.append({"role": "assistant", "content": bot})
    msgs.append({"role": "user", "content": current_user_text})
    return msgs


def _converse(history: list[dict], current_user_text: str,
              max_steps: int = 4) -> tuple[str, Optional[dict]]:
    """Run a multi-step conversation with Sonnet + the valuation tool.
    Returns (final_spoken_text, last_tool_result_or_None).
    Stops when Sonnet returns a final text response or after max_steps."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    except Exception as e:
        print(f'[voice] anthropic init failed: {e}', flush=True)
        return ("I'm having trouble reaching my brain right now — try again in a sec.",
                None)

    messages = _build_message_history(history, current_user_text)
    last_ctx: Optional[dict] = None

    for step in range(max_steps):
        try:
            resp = client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=600,
                temperature=0.4,
                system=_SYSTEM_PROMPT,
                tools=[_TOOL_GET_CONTEXT],
                messages=messages,
            )
            print(f'[voice] conv step={step} stop={resp.stop_reason} '
                  f'blocks={[b.type for b in resp.content]}', flush=True)
        except Exception as e:
            print(f'[voice] sonnet conv step {step} failed: {e}', flush=True)
            return ("Sorry, dropped my train of thought. What were we looking at?", last_ctx)

        # End-of-turn: text response, no more tool calls
        if resp.stop_reason in ('end_turn', 'stop_sequence'):
            text_parts = [b.text for b in resp.content if b.type == 'text']
            spoken = ' '.join(t.strip() for t in text_parts).strip()
            return spoken, last_ctx

        # Tool-use turn: execute any tools, append results, loop
        if resp.stop_reason == 'tool_use':
            tool_results = []
            # Append the assistant turn verbatim so the tool_use IDs match
            messages.append({"role": "assistant", "content": resp.content})
            for b in resp.content:
                if b.type == 'tool_use':
                    if b.name == 'get_valuation_context':
                        try:
                            args = b.input or {}
                            ctx = get_valuation_context(
                                year=int(args.get('year') or 0),
                                make=str(args.get('make') or ''),
                                model=str(args.get('model') or ''),
                                trim=args.get('trim'),
                                miles=args.get('miles'),
                                msrp=args.get('msrp'),
                            )
                            last_ctx = ctx
                            payload = _context_compact(ctx)
                        except Exception as e:
                            payload = {"error": f"{type(e).__name__}: {e}"}
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": _json.dumps(payload, default=str),
                        })
                    else:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": "Unknown tool",
                            "is_error": True,
                        })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unknown stop reason — return what we have
        text_parts = [b.text for b in resp.content if getattr(b, 'type', None) == 'text']
        spoken = ' '.join(t.strip() for t in text_parts).strip() or "Let me try that again."
        return spoken, last_ctx

    # Hit step limit — return last assistant text we have
    return ("Got tangled up — want to give me the year, make, model one more time?", last_ctx)


def _generate_reply(parsed: dict, ctx: dict) -> tuple[str, dict]:
    """Call Claude Sonnet, return (spoken_reply_text, structured_dict).
    Structured dict has target_buy/range_low/range_high/confidence."""
    trim = parsed.get('trim') or ''
    miles = parsed.get('miles')
    msrp = parsed.get('msrp')
    notes = parsed.get('notes')

    fmt = lambda s: s.format(
        year=parsed.get('year'),
        make=parsed.get('make'),
        model=parsed.get('model'),
        trim_part=(trim + ' ') if trim else '',
        miles_part=f"with {miles:,} miles " if miles else '',
        msrp_part=f"MSRP ${msrp:,} " if msrp else '',
        notes_or_none=notes or 'none',
        mmr_n=ctx['mmr_recent']['summary_wholesale']['n'],
        mmr_median=_format_money(ctx['mmr_recent']['summary_wholesale']['median']),
        mmr_p25=_format_money(ctx['mmr_recent']['summary_wholesale']['p25']),
        mmr_p75=_format_money(ctx['mmr_recent']['summary_wholesale']['p75']),
        nat_n=ctx['national_asking']['summary']['n'],
        nat_median=_format_money(ctx['national_asking']['summary']['median']),
        nat_p25=_format_money(ctx['national_asking']['summary']['p25']),
        nat_p75=_format_money(ctx['national_asking']['summary']['p75']),
        lsl_n=ctx['lsl_history']['summary_purchase']['n'],
        lsl_purch_median=_format_money(ctx['lsl_history']['summary_purchase']['median']),
        lsl_sale_median=_format_money(ctx['lsl_history']['summary_sale']['median']),
        naive_target=_format_money(ctx.get('naive_target')),
    )
    prompt = fmt(_REPLY_PROMPT)

    # Claude Sonnet 4.6 for valuation reasoning. 3-5s typical, applies
    # MSRP-anchor + depreciation heuristics reliably, formats JSON
    # cleanly. Replaces Gemini Flash (too rote) and Gemini Pro (too slow
    # at 19s and was truncating output mid-sentence).
    raw = _call_claude_sonnet(prompt, max_tokens=1500)
    if not raw:
        # Fallback to Gemini Flash if Sonnet errors — never leave the
        # operator with a dead mic.
        from app import gemini_call
        raw = gemini_call(prompt, model='gemini-3.5-flash',
                          max_tokens=1500, temperature=0.3,
                          disable_thinking=True)
    if not raw:
        return ("Sorry, I couldn't get a valuation right now. Try again in a moment.",
                {'target_buy': None, 'range_low': None, 'range_high': None,
                 'confidence': 'low'})

    # Expect: first line JSON, then blank line, then spoken text.
    lines = raw.strip().split('\n', 1)
    structured = _strip_json(lines[0]) or {}
    spoken = (lines[1] if len(lines) > 1 else raw).strip()
    spoken = re.sub(r'^\s*\n+', '', spoken)
    structured.setdefault('target_buy', None)
    structured.setdefault('range_low', None)
    structured.setdefault('range_high', None)
    structured.setdefault('confidence', 'medium')
    return spoken, structured


# ── Persistence ─────────────────────────────────────────────────────────
def _persist_valuation(transcript: str, parsed: dict, ctx: dict,
                       spoken: str, structured: dict,
                       session_id: Optional[str], turn_index: int,
                       llm_ms: int) -> tuple[int, str]:
    """Insert a voice_valuations row. Returns (id, short_id)."""
    short = _short_id()
    live = ctx.get('live_vauto') or {}
    snapshot = {
        'mmr_recent_summary': ctx['mmr_recent']['summary_wholesale'],
        'lsl_summary_purchase': ctx['lsl_history']['summary_purchase'],
        'lsl_summary_sale': ctx['lsl_history']['summary_sale'],
        'live_rbook_summary': (live.get('live_rbook') or {}).get('summary'),
        'live_mmr': live.get('live_mmr'),
        'live_rbook_n': (live.get('live_rbook') or {}).get('n'),
        'closest_mile_anchor': (live.get('live_rbook') or {}).get('closest_anchor'),
        'prior_mmr': ctx['prior_bids']['summary_mmr'],
        'prior_rbook': ctx['prior_bids']['summary_rbook'],
        'naive_target': ctx.get('naive_target'),
        'mmr_n': ctx['mmr_recent']['summary_wholesale']['n'],
        'lsl_n': ctx['lsl_history']['summary_purchase']['n'],
    }
    with _pg_ew() as c, c.cursor() as cur:
        # Retry once on short_id collision (~1-in-887M, but cheap to handle).
        for _ in range(3):
            try:
                cur.execute("""
                    INSERT INTO voice_valuations
                      (short_id, raw_transcript, parsed_year, parsed_make,
                       parsed_model, parsed_trim, parsed_miles, parsed_msrp,
                       parsed_notes, target_buy, range_low, range_high,
                       confidence, reasoning, comps_snapshot, session_id,
                       turn_index, llm_router, llm_ms)
                    VALUES
                      (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (
                    short, transcript,
                    parsed.get('year'), parsed.get('make'), parsed.get('model'),
                    parsed.get('trim'), parsed.get('miles'), parsed.get('msrp'),
                    parsed.get('notes'),
                    structured.get('target_buy'),
                    structured.get('range_low'),
                    structured.get('range_high'),
                    structured.get('confidence'),
                    spoken, _json.dumps(snapshot),
                    session_id, turn_index,
                    'sonnet-4.6-conv', llm_ms,
                ))
                row = cur.fetchone()
                c.commit()
                return row['id'], short
            except psycopg2.errors.UniqueViolation:
                c.rollback()
                short = _short_id()
        raise RuntimeError("short_id collision after 3 retries")


# ── WebSocket streaming STT ─────────────────────────────────────────────
# Real-time Google Cloud Speech streaming. Client opens WS, streams PCM
# 16-bit @ 16kHz mono in real-time; server forwards to Google's
# streaming_recognize and emits interim + final transcripts back.
# Eliminates the ~1-2s gap between user-stops-talking and transcript-ready.
def init_voice_ws(app):
    """Called from app.py after voice_bp registration to attach the WS
    route. Kept lazy because flask-sock binds to the actual Flask app
    instance, not a blueprint."""
    try:
        from flask_sock import Sock
    except ImportError:
        print('[voice] flask-sock not installed; streaming STT disabled',
              flush=True)
        return None

    sock = Sock(app)

    @sock.route('/api/voice/stt/ws')
    def stt_ws(ws):
        """Bidirectional WebSocket for streaming STT.

        Client → server:
          - Text frame {"type":"start","sample_rate":16000} to begin
          - Binary frames containing raw PCM 16-bit little-endian audio
          - Text frame {"type":"stop"} when user is done

        Server → client:
          - {"type":"interim","transcript":"...","stability":0.8}
          - {"type":"final","transcript":"...","confidence":0.95}
          - {"type":"error","message":"..."}
        """
        try:
            from google.cloud import speech
        except ImportError:
            try:
                ws.send(_json.dumps({'type': 'error',
                                     'message': 'google-cloud-speech not installed'}))
            except Exception: pass
            return

        import queue as _q
        import threading as _th

        audio_queue = _q.Queue()
        stopped = [False]

        def request_generator():
            """Yield audio chunks as they arrive. Returns when client
            sends stop or WS disconnects."""
            while not stopped[0]:
                try:
                    chunk = audio_queue.get(timeout=15)
                except _q.Empty:
                    break
                if chunk is None:
                    break
                yield speech.StreamingRecognizeRequest(audio_content=chunk)

        sample_rate = 16000
        first_msg = None
        try:
            first_msg = ws.receive(timeout=10)
        except Exception as e:
            print(f'[voice] stt-ws no first msg: {e}', flush=True)
            return
        if first_msg and isinstance(first_msg, str):
            try:
                first = _json.loads(first_msg)
                if first.get('sample_rate'):
                    sample_rate = int(first['sample_rate'])
            except Exception: pass

        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=sample_rate,
            language_code='en-US',
            model='latest_short',
            enable_automatic_punctuation=True,
            use_enhanced=True,
        )
        # SERVER_VAD_2026_05_21 — single_utterance=True so Google's VAD
        # detects end-of-utterance and closes the gRPC stream cleanly.
        # Way more reliable than client RMS thresholding. Each WS session
        # = one utterance; client opens a new WS for the next turn.
        streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=True,
            single_utterance=True,
        )

        client_speech = speech.SpeechClient()

        # Spawn a thread to consume STT responses and push to WS
        ws_lock = _th.Lock()
        def _safe_send(payload):
            try:
                with ws_lock:
                    ws.send(_json.dumps(payload))
            except Exception:
                stopped[0] = True

        # Track the most recent interim so we can synthesize a final on
        # stream-end if Google didn't emit an explicit one (rare but
        # happens with short utterances).
        last_interim = {'text': '', 'sent_final': False}

        def consume_responses():
            try:
                responses = client_speech.streaming_recognize(
                    streaming_config, request_generator())
                for response in responses:
                    if stopped[0]: break
                    # Google can signal end-of-single-utterance via
                    # `speech_event_type`. When that arrives the stream
                    # will close imminently — flush our final.
                    set = getattr(response, 'speech_event_type', 0)
                    if set:  # any non-zero event is end-of-utterance
                        print(f'[voice] google speech_event_type={set} '
                              f'(end-of-utterance)', flush=True)
                    for result in response.results:
                        if not result.alternatives: continue
                        alt = result.alternatives[0]
                        transcript = alt.transcript.strip()
                        if not transcript: continue
                        if result.is_final:
                            last_interim['sent_final'] = True
                            last_interim['text'] = transcript
                            _safe_send({
                                'type': 'final',
                                'transcript': transcript,
                                'confidence': float(alt.confidence or 0),
                            })
                        else:
                            last_interim['text'] = transcript
                            _safe_send({
                                'type': 'interim',
                                'transcript': transcript,
                                'stability': float(result.stability or 0),
                            })
                # Stream closed (Google detected end-of-utterance). If we
                # have an interim but never sent a final, commit it now.
                if not last_interim['sent_final'] and last_interim['text']:
                    print(f'[voice] no final from Google — '
                          f'committing last interim as final: '
                          f'{last_interim["text"][:60]!r}', flush=True)
                    _safe_send({
                        'type': 'final',
                        'transcript': last_interim['text'],
                        'confidence': 0.85,
                    })
                # Signal end-of-utterance to client so it can stop
                # streaming PCM and call send(transcript).
                _safe_send({'type': 'utterance_end'})
                stopped[0] = True
            except Exception as e:
                print(f'[voice] stt-ws consumer err: {e}', flush=True)
                _safe_send({'type': 'error', 'message': str(e)[:200]})

        consumer_thread = _th.Thread(target=consume_responses, daemon=True)
        consumer_thread.start()

        # Receive audio chunks until client signals stop or disconnects
        try:
            while not stopped[0]:
                msg = ws.receive(timeout=20)
                if msg is None: break
                if isinstance(msg, str):
                    try:
                        ctrl = _json.loads(msg)
                        if ctrl.get('type') == 'stop':
                            stopped[0] = True
                            audio_queue.put(None)
                            break
                    except Exception:
                        continue
                elif isinstance(msg, (bytes, bytearray)):
                    audio_queue.put(bytes(msg))
        except Exception as e:
            print(f'[voice] stt-ws receive err: {e}', flush=True)
        finally:
            stopped[0] = True
            audio_queue.put(None)
            consumer_thread.join(timeout=3)

    print('[voice] streaming STT WS endpoint registered at /api/voice/stt/ws',
          flush=True)
    return sock


# ── Streaming helpers ───────────────────────────────────────────────────
import base64 as _base64

def _sse(data: dict) -> str:
    """Format a dict as a Server-Sent Events frame."""
    return f"data: {_json.dumps(data)}\n\n"


_SENT_BOUNDARY = re.compile(r'[.!?](?=\s|$|["\')\]])')


def _chunk_next_sentence(buf: str) -> tuple[Optional[str], str]:
    """Return (complete_sentence, remaining_buffer) or (None, buf).
    A sentence ends on . ! ? followed by whitespace, end-of-string, or
    a closing quote/paren. Keeps buffer short — caller invokes in a loop."""
    m = _SENT_BOUNDARY.search(buf)
    if not m:
        return None, buf
    sentence = buf[:m.end()].strip()
    remaining = buf[m.end():].lstrip()
    return sentence, remaining


def _tts_chunk(text: str) -> Optional[bytes]:
    """Synthesize one sentence via Google Cloud TTS. Returns MP3 bytes
    or None on failure (caller skips and continues)."""
    if not text or not text.strip():
        return None
    try:
        from google.cloud import texttospeech
        client = texttospeech.TextToSpeechClient()
        si = texttospeech.SynthesisInput(text=text)
        v = texttospeech.VoiceSelectionParams(language_code='en-US',
                                              name='en-US-Neural2-F')
        ac = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.05,
        )
        resp = client.synthesize_speech(input=si, voice=v, audio_config=ac)
        return resp.audio_content
    except Exception as e:
        print(f'[voice] tts chunk failed: {type(e).__name__}: {e}', flush=True)
        return None


def _emit_sentence(text: str, final: bool = False):
    """Generate SSE event for a single sentence: TTS + emit text+audio.
    Synchronous version — used for instant (one-shot) responses."""
    audio = _tts_chunk(text)
    payload = {'type': 'sentence', 'text': text, 'final': final}
    if audio:
        payload['audio_b64'] = _base64.b64encode(audio).decode('ascii')
    return _sse(payload)


def _stream_cerebras_chat(messages: list[dict], system: str,
                          max_tokens: int = 1500,
                          temperature: float = 0.4,
                          model: str = 'qwen-3-235b-a22b-instruct-2507'):
    """Yield text chunks from Cerebras's OpenAI-compatible streaming
    endpoint. ~5-10x faster than Sonnet (~300ms-1s end-to-end for short
    replies). Used for the quick-path valuation reply when YMM is clear
    and we have pre-loaded context.

    Convert Anthropic-shape `messages` (no system role) + separate `system`
    parameter into OpenAI-shape with a system role at the top.
    """
    import requests as _r
    key = os.environ.get('CEREBRAS_API_KEY')
    if not key:
        raise RuntimeError('CEREBRAS_API_KEY not set')
    oai_messages = [{'role': 'system', 'content': system}]
    for m in messages:
        c = m.get('content')
        if isinstance(c, list):
            # Anthropic content-block format — flatten text blocks
            text = ' '.join(b.get('text', '') for b in c
                            if isinstance(b, dict) and b.get('type') == 'text')
            oai_messages.append({'role': m['role'], 'content': text})
        else:
            oai_messages.append({'role': m['role'], 'content': c or ''})

    resp = _r.post(
        'https://api.cerebras.ai/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}',
                 'Content-Type': 'application/json'},
        json={'model': model, 'messages': oai_messages,
              'max_tokens': max_tokens, 'temperature': temperature,
              'stream': True},
        timeout=30, stream=True,
    )
    if resp.status_code != 200:
        raise RuntimeError(f'cerebras HTTP {resp.status_code}: {resp.text[:300]}')

    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith('data: '):
            continue
        payload = raw[6:].strip()
        if payload == '[DONE]':
            break
        try:
            obj = _json.loads(payload)
        except Exception:
            continue
        for choice in obj.get('choices') or []:
            delta = choice.get('delta') or {}
            content = delta.get('content')
            if content:
                yield content


class _ParallelTTSEmitter:
    """Submit sentences as Sonnet streams them; emit SSE events in
    submission order as TTS completes. Sentence 1's TTS runs in parallel
    with sentence 2/3/4's TTS — total wait is dominated by the slowest
    single TTS call rather than the sum.

    Usage:
        e = _ParallelTTSEmitter()
        for chunk in sonnet_stream:
            for sent in extract_sentences(buf):
                e.submit(sent)
                yield from e.flush_ready_in_order()   # non-blocking drain at head
        yield from e.flush_all()                       # blocking final drain
    """
    def __init__(self, max_workers: int = 4):
        from concurrent.futures import ThreadPoolExecutor
        self._ex = ThreadPoolExecutor(max_workers=max_workers)
        self._pending = []   # list of (future, text, final), in submission order
        self._next_to_emit = 0  # index of next future expected to emit

    def submit(self, text: str, final: bool = False):
        f = self._ex.submit(_tts_chunk, text)
        self._pending.append((f, text, final))

    def _build_sse(self, text, final, audio):
        payload = {'type': 'sentence', 'text': text, 'final': final}
        if audio:
            payload['audio_b64'] = _base64.b64encode(audio).decode('ascii')
        return _sse(payload)

    def flush_ready_in_order(self):
        """Yield SSE events for any sentences at the head of the queue
        whose TTS is already DONE. Non-blocking — stops at the first
        not-yet-done future to preserve audio order."""
        while self._next_to_emit < len(self._pending):
            f, text, final = self._pending[self._next_to_emit]
            if not f.done():
                return
            try:
                audio = f.result()
            except Exception as e:
                print(f'[voice] parallel tts err: {e}', flush=True)
                audio = None
            yield self._build_sse(text, final, audio)
            self._next_to_emit += 1

    def flush_all(self):
        """Block until every remaining sentence's TTS is done, yield in order."""
        while self._next_to_emit < len(self._pending):
            f, text, final = self._pending[self._next_to_emit]
            try:
                audio = f.result(timeout=10)
            except Exception as e:
                print(f'[voice] parallel tts err: {e}', flush=True)
                audio = None
            yield self._build_sse(text, final, audio)
            self._next_to_emit += 1

    def shutdown(self):
        try: self._ex.shutdown(wait=False)
        except Exception: pass


# ── Endpoints ───────────────────────────────────────────────────────────
@voice_bp.route('/api/voice/query', methods=['POST'])
def api_voice_query():
    """Day-2: spoken transcript → Flash parse → comps → Flash reply.
    Body: { transcript: str, session_id?: str, turn_index?: int }
    Returns: { reply_text, structured, parsed, needs_followup, used_model, valuation_id, short_id }
    """
    body = request.get_json(silent=True) or {}
    transcript = (body.get('transcript') or '').strip()
    if not transcript:
        return jsonify({'error': 'transcript required'}), 400
    session_id = body.get('session_id')
    turn_index = int(body.get('turn_index') or 0)

    t0 = time.time()

    # Peek at session state BEFORE the bare-phone gate so AWAITING_PHONE
    # routes correctly into the state machine instead of being intercepted.
    def _peek_state(sid):
        if not sid: return None
        try:
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT parsed_notes FROM voice_valuations
                     WHERE session_id = %s AND parsed_notes LIKE 'STATE=%%'
                     ORDER BY created_at DESC LIMIT 1
                """, (sid,))
                row = cur.fetchone()
            if not row: return None
            tokens = dict(p.split('=', 1) for p in row['parsed_notes'].split('|') if '=' in p)
            return tokens
        except Exception:
            return None
    _peek = _peek_state(session_id)
    _in_awaiting_phone = _peek and _peek.get('STATE') == 'AWAITING_PHONE'

    # ── Bare phone number (operator dictating their callback) ────────
    # Catches things like "4074309675" or "407 430 9675" that the user
    # said in response to "What's your number?" — without this guard,
    # the refinement carry-over below would re-run a valuation using
    # the prior turn's YMM. Save the phone client-side and short-circuit.
    # SKIPPED when state machine is in AWAITING_PHONE so the structured
    # confirmation flow handles it instead.
    _phone_only = re.sub(r'\D', '', transcript)
    _word_chars = re.sub(r'[\d\s\-\(\)\.\+]', '', transcript).strip()
    if (not _in_awaiting_phone
        and (10 <= len(_phone_only) <= 11)
        and len(_word_chars) <= 4):
        # Pure phone-shape transcript. Treat as phone capture.
        digits = _phone_only[-10:]
        phone_pretty = f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
        return jsonify({
            'reply_text': f"Got it — I'll text reports to {phone_pretty}. What car do you want a valuation on?",
            'parsed': {'intent': 'set_phone', 'phone_e164': '+1' + digits},
            'structured': None,
            'phone_captured': '+1' + digits,
            'needs_followup': False,
            'used_model': 'rules',
            'valuation_id': None,
            'short_id': None,
            'elapsed_ms': int((time.time() - t0) * 1000),
        })

    # Load convo history once for use by both state machine + conversation path
    history = _load_session_history(session_id, limit=8)

    # ── STATE-MACHINE for VIN + phone confirmation ──────────────────────
    # State token is stored in voice_valuations.parsed_notes as one of:
    #   STATE=VIN_CONFIRM|VIN=XXXXX            (asked operator to confirm VIN)
    #   STATE=OFFER_PENDING|VIN=XXXXX          (offered full appraisal, awaiting yes/no)
    #   STATE=PHONE_CONFIRM|VIN=XXXXX|PHONE=+1...  (read phone back, awaiting confirm)
    def _load_state(sid):
        if not sid: return None
        try:
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT parsed_notes FROM voice_valuations
                     WHERE session_id = %s AND parsed_notes LIKE 'STATE=%%'
                     ORDER BY created_at DESC LIMIT 1
                """, (sid,))
                row = cur.fetchone()
            if not row: return None
            tokens = dict(p.split('=', 1) for p in row['parsed_notes'].split('|') if '=' in p)
            return tokens
        except Exception as e:
            print(f'[voice] state load failed: {e}', flush=True)
            return None

    def _persist_state_turn(transcript, parsed_notes, spoken):
        try:
            short = _short_id()
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    INSERT INTO voice_valuations
                      (short_id, raw_transcript, parsed_notes, session_id,
                       turn_index, llm_router, reasoning)
                    VALUES (%s, %s, %s, %s, %s, 'state-machine', %s)
                    RETURNING id
                """, (short, transcript, parsed_notes, session_id, turn_index, spoken))
                vid = cur.fetchone()['id']
                c.commit()
                return vid, short
        except Exception as e:
            print(f'[voice] state persist failed: {e}', flush=True)
            return None, None

    state = _load_state(session_id)
    is_yes = _is_confirmation(transcript)
    is_no  = bool(_DECLINE_RE.search(transcript)) and len(transcript.split()) <= 6

    # STATE_RESET_2026_05_21 — if bot is parked in VIN_CONFIRM/AWAITING_VIN
    # but the operator asks a fresh YMM question, drop the stale state and
    # let the normal flow take over. Detection: _quick_parse_ymm returns a
    # plausible {year, make, model} AND the transcript isn't a bare yes/no.
    if state and state.get('STATE') in ('VIN_CONFIRM', 'AWAITING_VIN'):
        if not is_yes and not is_no:
            try:
                qymm = _quick_parse_ymm(transcript) or {}
            except Exception:
                qymm = {}
            if qymm.get('year') and qymm.get('make') and qymm.get('model'):
                print(f'[voice] STATE_RESET: dropping {state.get("STATE")} '
                      f'for new YMM {qymm.get("year")} {qymm.get("make")} '
                      f'{qymm.get("model")}', flush=True)
                state = None

    # GATE 1: bot asked operator to confirm the VIN it heard. Yes → proceed
    # to vAuto lookup. No → ask to re-read.
    if state and state.get('STATE') == 'VIN_CONFIRM':
        vin_pending = state.get('VIN')
        if is_no:
            spoken = "No problem — read me the VIN again, take your time."
            _persist_state_turn(transcript, 'STATE=AWAITING_VIN', spoken)
            return jsonify({'reply_text': spoken, 'parsed': {'intent': 'vin_reread'},
                            'needs_followup': True, 'used_model': 'rules',
                            'elapsed_ms': int((time.time() - t0) * 1000)})
        if is_yes and vin_pending:
            # Pull miles from this turn or a prior turn if mentioned
            miles_hint = None
            for src in (transcript, ' '.join((h.get('raw_transcript') or '') for h in history[:3])):
                m = re.search(r'(\d{1,3}[,\s]?\d{3}|\d{4,6})\s*(?:k|miles|mi)\b', src, re.I)
                if m:
                    try:
                        miles_hint = int(re.sub(r'\D', '', m.group(1)))
                        if 'k' in m.group(0).lower() and miles_hint < 1000:
                            miles_hint *= 1000
                        break
                    except Exception:
                        pass
            live = _fetch_live_vauto_with_vin(vin_pending, miles=miles_hint)
            if not live:
                spoken = ("I couldn't reach vAuto on that VIN — cookies may be stale. "
                          "Want me to fire the full appraisal anyway? It'll catch what live can't.")
                _persist_state_turn(transcript,
                                    f'STATE=OFFER_PENDING|VIN={vin_pending}', spoken)
                return jsonify({'reply_text': spoken,
                                'parsed': {'intent': 'vin_no_live', 'vin': vin_pending},
                                'needs_followup': False, 'used_model': 'rules',
                                'elapsed_ms': int((time.time() - t0) * 1000)})

            # Build Sonnet ask
            mmr_avg = (live.get('live_mmr') or {}).get('average_auction_price')
            mmr_odo = (live.get('live_mmr') or {}).get('average_odometer')
            rb_sum  = (live.get('live_rbook') or {}).get('summary') or {}
            anchor  = (live.get('live_rbook') or {}).get('closest_anchor')
            n_rb    = (live.get('live_rbook') or {}).get('n') or 0
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
                anchor_str = (
                    f"closest-mile anchor: {anchor['mileage']:,} mi at ${anchor['price']:,} "
                    f"({anchor.get('dealer') or 'unknown'}, {anchor.get('days_on_lot') or '?'} DOL)"
                    if anchor else "no close-mile anchor"
                )
                vin_prompt = f"""\
LIVE vAuto data for VIN {vin_pending} ({live.get('subject_year')} {live.get('subject_make')} {live.get('subject_model')}{(' ' + (live.get('subject_trim') or ''))}):
- MMR: ${mmr_avg:,} at {mmr_odo:,} avg miles
- rBook: {n_rb} listings, median ${rb_sum.get('median') or 0:,.0f}, p75 ${rb_sum.get('p75') or 0:,.0f}
- {anchor_str}
Spoken miles: {miles_hint or 'not specified'}

≤35 words spoken response:
1) Lead with the wholesale target.
2) Cite ONE specific data point (the anchor's dealer + DOL is great).
3) End: "Want me to fire the full appraisal? I'll text you the report."
Numbers spoken naturally."""
                resp = client.messages.create(
                    model='claude-sonnet-4-6', max_tokens=350, temperature=0.4,
                    system=_SYSTEM_PROMPT,
                    messages=[{'role': 'user', 'content': vin_prompt}],
                )
                spoken = ' '.join(b.text for b in resp.content if b.type == 'text').strip()
            except Exception as e:
                print(f'[voice] vin-after-confirm sonnet failed: {e}', flush=True)
                spoken = (f"Live MMR's at {int(mmr_avg or 0):,} on average miles. "
                         f"Want me to fire the full appraisal? I'll text you the report.")
            _persist_state_turn(transcript,
                                f'STATE=OFFER_PENDING|VIN={vin_pending}', spoken)
            return jsonify({'reply_text': spoken,
                            'parsed': {'intent': 'vin_valued', 'vin': vin_pending,
                                       'year': live.get('subject_year'),
                                       'make': live.get('subject_make'),
                                       'model': live.get('subject_model')},
                            'comps_counts': {'live': n_rb, 'live_mmr': 1 if mmr_avg else 0,
                                             'priors': 0, 'lsl': 0},
                            'needs_followup': False, 'used_model': 'sonnet-4.6-vin',
                            'elapsed_ms': int((time.time() - t0) * 1000)})

    # GATE 2: bot offered full appraisal. Yes → phone confirm. No → end politely.
    if state and state.get('STATE') == 'OFFER_PENDING':
        if is_no:
            spoken = "Got it — skipping the full appraisal. What else can I look at?"
            _persist_state_turn(transcript, 'STATE=IDLE', spoken)
            return jsonify({'reply_text': spoken, 'parsed': {'intent': 'declined'},
                            'needs_followup': True, 'used_model': 'rules',
                            'elapsed_ms': int((time.time() - t0) * 1000)})
        if is_yes:
            vin = state.get('VIN')
            stored_phone = (body.get('phone') or '').strip()
            digits = re.sub(r'\D', '', stored_phone)
            if len(digits) == 10:
                phone_e164 = '+1' + digits
            elif len(digits) == 11 and digits.startswith('1'):
                phone_e164 = '+' + digits
            else:
                phone_e164 = None
            if not phone_e164:
                spoken = "What's the phone number for the text? Read it slow."
                _persist_state_turn(transcript, f'STATE=AWAITING_PHONE|VIN={vin}', spoken)
                return jsonify({'reply_text': spoken, 'parsed': {'intent': 'need_phone'},
                                'needs_followup': True, 'needs_phone': True,
                                'used_model': 'rules',
                                'elapsed_ms': int((time.time() - t0) * 1000)})
            # We have a phone — read it back for confirmation
            spoken = (f"Got it. Reading your number back: {_phone_spoken(phone_e164)}. "
                      f"Sound right?")
            _persist_state_turn(transcript,
                                f'STATE=PHONE_CONFIRM|VIN={vin}|PHONE={phone_e164}', spoken)
            return jsonify({'reply_text': spoken,
                            'parsed': {'intent': 'phone_confirm', 'vin': vin,
                                       'phone': phone_e164},
                            'needs_followup': True, 'used_model': 'rules',
                            'elapsed_ms': int((time.time() - t0) * 1000)})

    # GATE 3: phone read-back. Yes → fire bid. No → ask for new phone.
    if state and state.get('STATE') == 'PHONE_CONFIRM':
        if is_no:
            vin = state.get('VIN')
            spoken = "OK — what's the right number then?"
            _persist_state_turn(transcript, f'STATE=AWAITING_PHONE|VIN={vin}', spoken)
            return jsonify({'reply_text': spoken,
                            'parsed': {'intent': 'phone_reread', 'vin': vin},
                            'needs_followup': True, 'needs_phone': True,
                            'used_model': 'rules',
                            'elapsed_ms': int((time.time() - t0) * 1000)})
        if is_yes:
            vin = state.get('VIN')
            phone_e164 = state.get('PHONE')
            bid_id = None
            try:
                with _pg_ew() as c, c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO bids (phone, vin, source, vauto_priority,
                                          status, raw_message, notes)
                        VALUES (%s, %s, 'voice', TRUE, 'new', %s, %s)
                        RETURNING id
                    """, (phone_e164, vin,
                          '[voice] confirmed bid (VIN+phone verified)',
                          f'Voice bid · session={session_id} · vin={vin} · phone={phone_e164}'))
                    bid_id = cur.fetchone()['id']
                    c.commit()
            except Exception as e:
                print(f'[voice] confirmed-bid insert failed: {e}', flush=True)
                spoken = "Couldn't kick that off — try again or contact dispatch."
                _persist_state_turn(transcript, 'STATE=ERROR', spoken)
                return jsonify({'reply_text': spoken, 'used_model': 'rules',
                                'elapsed_ms': int((time.time() - t0) * 1000)}), 500
            spoken = (f"On it. Full appraisal running on VIN ending {vin[-4:]}. "
                      f"I'll text the report to {_phone_spoken(phone_e164)} in about two minutes.")
            _persist_state_turn(transcript,
                                f'STATE=BID_FIRED|VIN={vin}|BID={bid_id}', spoken)
            return jsonify({'reply_text': spoken,
                            'parsed': {'intent': 'bid_fired', 'vin': vin,
                                       'phone': phone_e164},
                            'bid_id': bid_id,
                            'needs_followup': False, 'used_model': 'rules',
                            'elapsed_ms': int((time.time() - t0) * 1000)})

    # ── AWAITING_PHONE state: operator is dictating a phone after we asked
    if state and state.get('STATE') == 'AWAITING_PHONE':
        digits_only = re.sub(r'\D', '', transcript)
        word_chars = re.sub(r'[\d\s\-\(\)\.\+]', '', transcript).strip()
        if (10 <= len(digits_only) <= 11) and len(word_chars) <= 4:
            d = digits_only[-10:]
            phone_e164 = '+1' + d
            vin = state.get('VIN')
            spoken = (f"Got it. Reading back: {_phone_spoken(phone_e164)}. "
                      f"Sound right?")
            _persist_state_turn(transcript,
                                f'STATE=PHONE_CONFIRM|VIN={vin}|PHONE={phone_e164}', spoken)
            return jsonify({'reply_text': spoken,
                            'parsed': {'intent': 'phone_confirm', 'vin': vin,
                                       'phone': phone_e164},
                            'phone_captured': phone_e164,
                            'needs_followup': True, 'used_model': 'rules',
                            'elapsed_ms': int((time.time() - t0) * 1000)})

    # Legacy confirmation path — fires for VIN_OFFER state without a VIN_CONFIRM
    # gate (kept for backward compat with mid-flight sessions). Will be retired
    # once all session activity uses the new state machine above.
    if _is_confirmation(transcript):
        try:
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT id, parsed_notes
                      FROM voice_valuations
                     WHERE session_id = %s
                       AND parsed_notes LIKE 'VIN_OFFER=%%'
                     ORDER BY created_at DESC LIMIT 1
                """, (session_id,))
                offer_row = cur.fetchone()
        except Exception:
            offer_row = None
        if offer_row:
            pending_vin = offer_row['parsed_notes'].split('=', 1)[1].split('|')[0].strip()
            phone = (body.get('phone') or '').strip()
            digits = re.sub(r'\D', '', phone) if phone else ''
            if len(digits) == 10:
                phone_e164 = '+1' + digits
            elif len(digits) == 11 and digits.startswith('1'):
                phone_e164 = '+' + digits
            else:
                phone_e164 = None
            if not phone_e164:
                return jsonify({
                    'reply_text': "Sure thing — what's the phone number to text the full report to?",
                    'parsed': {'intent': 'vin_confirm', 'vin': pending_vin},
                    'structured': None,
                    'needs_followup': True,
                    'needs_phone': True,
                    'used_model': 'rules',
                    'valuation_id': None,
                    'short_id': None,
                    'elapsed_ms': int((time.time() - t0) * 1000),
                })
            # Fire the bid pipeline
            bid_id = None
            try:
                with _pg_ew() as c, c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO bids (phone, vin, source, vauto_priority,
                                          status, raw_message, notes)
                        VALUES (%s, %s, 'voice', TRUE, 'new', %s, %s)
                        RETURNING id
                    """, (
                        phone_e164, pending_vin,
                        f'[voice] confirmed full appraisal',
                        f'Voice-confirmed bid · session={session_id} · vin={pending_vin}',
                    ))
                    bid_id = cur.fetchone()['id']
                    c.commit()
            except Exception as e:
                print(f'[voice] confirm-bid insert failed: {e}', flush=True)
                return jsonify({
                    'reply_text': "Couldn't kick that off — try again or contact dispatch.",
                    'parsed': {'intent': 'vin_confirm', 'vin': pending_vin},
                    'needs_followup': False, 'used_model': 'rules',
                    'elapsed_ms': int((time.time() - t0) * 1000),
                }), 500
            reply = (f"On it. Full appraisal running on VIN ending {pending_vin[-4:]}. "
                     f"vAuto, AccuTrade, iPacket all spinning up — I'll text you the report in about two minutes.")
            return jsonify({
                'valuation_id': None, 'short_id': None,
                'reply_text': reply,
                'parsed': {'intent': 'vin_confirm', 'vin': pending_vin},
                'structured': None,
                'bid_id': bid_id,
                'needs_followup': False,
                'used_model': 'rules',
                'elapsed_ms': int((time.time() - t0) * 1000),
            })

    # ── VIN spoken — read back phonetically and ask to confirm BEFORE any
    # vAuto lookup or bid creation. STT routinely mangles VINs (homophones,
    # missed chars), so confirmation gates accuracy.
    prior_bot_reply = (history[0].get('reasoning') if history else '') or ''
    vin = _extract_vin(transcript, prior_bot_reply=prior_bot_reply)
    if vin:
        phonetic = _vin_phonetic(vin)
        spoken = (f"Got it — reading the VIN back: {phonetic}. "
                  f"Do I have that right?")
        _persist_state_turn(transcript,
                            f'STATE=VIN_CONFIRM|VIN={vin}', spoken)
        return jsonify({'reply_text': spoken,
                        'parsed': {'intent': 'vin_readback', 'vin': vin},
                        'needs_followup': True, 'used_model': 'rules',
                        'elapsed_ms': int((time.time() - t0) * 1000)})

    # Partial-VIN attempt — operator said the VIN keyword (or bot just
    # asked for one) + some chars but extractor couldn't form a valid
    # 17-char VIN. Don't fall through to Sonnet's conversational path;
    # ask for a re-read with the count.
    prior_bot = (history[0].get('reasoning') if history else '') or ''
    near_n = _near_vin_attempt(transcript, prior_bot_reply=prior_bot)
    if near_n is not None:
        spoken = (f"I only caught {near_n} of the seventeen characters. "
                  f"Read the VIN to me again — slow, one character at a time.")
        _persist_state_turn(transcript,
                            'STATE=AWAITING_VIN', spoken)
        return jsonify({'reply_text': spoken,
                        'parsed': {'intent': 'vin_partial', 'count': near_n},
                        'needs_followup': True, 'used_model': 'rules',
                        'elapsed_ms': int((time.time() - t0) * 1000)})

    # Legacy VIN handoff path — never reached now that we have the state
    # machine above. Retained as dead code for one release; will be removed
    # after operator-validated runs prove the new flow is solid.
    if False and vin:
        # Use the user-supplied miles if mentioned; else None (vAuto will
        # use its decoded default).
        miles_hint = None
        m = re.search(r'(\d{1,3}[,\s]?\d{3}|\d{4,6})\s*(?:k|miles|mi)\b', transcript, re.I)
        if m:
            try:
                miles_hint = int(re.sub(r'\D', '', m.group(1)))
                if 'k' in m.group(0).lower() and miles_hint < 1000:
                    miles_hint *= 1000
            except Exception:
                pass

        live = _fetch_live_vauto_with_vin(vin, miles=miles_hint)
        if not live:
            # vAuto didn't return — fall through to normal conversation
            # path so Sonnet asks for more info
            pass
        else:
            # Hand the VIN-keyed live data to Sonnet via a synthetic
            # tool-pre-loaded message so it can speak naturally.
            from concurrent.futures import ThreadPoolExecutor  # already imported up top, harmless dupe
            mmr_avg = (live.get('live_mmr') or {}).get('average_auction_price')
            mmr_odo = (live.get('live_mmr') or {}).get('average_odometer')
            rb_sum  = (live.get('live_rbook') or {}).get('summary') or {}
            anchor  = (live.get('live_rbook') or {}).get('closest_anchor')
            n_rb    = (live.get('live_rbook') or {}).get('n') or 0

            # Build a one-shot Sonnet ask: speak the valuation, then offer
            # full appraisal. No tool use here — we already have the data.
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
                anchor_str = (
                    f"closest-mile anchor: {anchor['mileage']:,} mi at ${anchor['price']:,} "
                    f"({anchor.get('dealer') or 'unknown dealer'}, "
                    f"{anchor.get('days_on_lot') or '?'} days on lot)"
                    if anchor else "no close-mile anchor"
                )
                vin_prompt = f"""\
The operator just read a VIN: {vin}
Subject vehicle (vAuto canonical decode): {live.get('subject_year')} {live.get('subject_make')} {live.get('subject_model')} {live.get('subject_trim') or ''}
Spoken miles: {miles_hint if miles_hint else 'not specified'}

LIVE vAuto data for this VIN:
- MMR auction average: ${mmr_avg:,} at {mmr_odo:,} avg miles
- rBook competitive set: {n_rb} listings, median ${rb_sum.get('median') or 0:,.0f}, p25 ${rb_sum.get('p25') or 0:,.0f}, p75 ${rb_sum.get('p75') or 0:,.0f}
- {anchor_str}

Give a SHORT spoken response (≤40 words):
1) Lead with the wholesale target you'd land at, anchored on MMR.
2) Cite ONE specific data point (the anchor's dealer + DOL is great if helpful).
3) END by offering: "Want me to fire the full appraisal? It pulls AccuTrade, iPacket, and the full AI report — I'll text it to you in about two minutes."

Numbers spoken naturally (no $, no commas, no "thousand" repetition).
"""
                resp = client.messages.create(
                    model='claude-sonnet-4-6',
                    max_tokens=350,
                    temperature=0.4,
                    system=_SYSTEM_PROMPT,
                    messages=[{'role': 'user', 'content': vin_prompt}],
                )
                spoken = ' '.join(b.text for b in resp.content if b.type == 'text').strip()
            except Exception as e:
                print(f'[voice] vin-reply sonnet failed: {e}', flush=True)
                spoken = (f"Looks like MMR's around {int(mmr_avg or 0):,} at miles. "
                         f"Want me to fire the full appraisal? I'll text you the full report.")

            # Persist with VIN_OFFER flag so the next-turn confirmation can find it
            try:
                short = _short_id()
                with _pg_ew() as c, c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO voice_valuations
                          (short_id, raw_transcript, parsed_notes, session_id,
                           turn_index, llm_router, reasoning)
                        VALUES (%s, %s, %s, %s, %s, 'sonnet-vin-offer', %s)
                        RETURNING id
                    """, (short, transcript, f'VIN_OFFER={vin}',
                          session_id, turn_index, spoken))
                    vid = cur.fetchone()['id']
                    c.commit()
            except Exception as e:
                print(f'[voice] vin persist failed: {e}', flush=True)
                vid = None; short = None

            return jsonify({
                'valuation_id': vid,
                'short_id': short,
                'reply_text': spoken,
                'parsed': {'intent': 'vin_offer', 'vin': vin,
                           'year':  live.get('subject_year'),
                           'make':  live.get('subject_make'),
                           'model': live.get('subject_model')},
                'structured': None,
                'comps_counts': {
                    'live': n_rb, 'live_mmr': 1 if mmr_avg else 0,
                    'priors': 0, 'lsl': 0,
                },
                'vin_offer_pending': True,
                'needs_followup': False,
                'used_model': 'sonnet-4.6-vin',
                'elapsed_ms': int((time.time() - t0) * 1000),
            })

    # Legacy VIN handoff (now only reached if _fetch_live_vauto_with_vin
    # failed — kept as last resort to at least kick the bid pipeline).
    if vin:
        phone = (body.get('phone') or '').strip()
        # Normalize US 10-digit → +1XXXXXXXXXX
        digits = re.sub(r'\D', '', phone) if phone else ''
        if len(digits) == 10:
            phone_e164 = '+1' + digits
        elif len(digits) == 11 and digits.startswith('1'):
            phone_e164 = '+' + digits
        elif phone.startswith('+') and len(digits) >= 10:
            phone_e164 = '+' + digits
        else:
            phone_e164 = None

        if not phone_e164:
            return jsonify({
                'reply_text': "Got the VIN. What's your phone number? I'll text the full appraisal when it's done.",
                'parsed': {'intent': 'vin', 'vin': vin},
                'structured': None,
                'needs_followup': True,
                'needs_phone': True,
                'used_model': 'flash+rules',
                'valuation_id': None,
                'short_id': None,
                'elapsed_ms': int((time.time() - t0) * 1000),
            })

        # Look up the most-recent same-session valuation for miles carry-over
        prior_miles = None
        prior_msrp = None
        prior = None
        try:
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT id, parsed_miles, parsed_msrp, parsed_year,
                           parsed_make, parsed_model
                      FROM voice_valuations
                     WHERE session_id = %s
                     ORDER BY created_at DESC LIMIT 1
                """, (session_id,))
                prior = cur.fetchone()
                if prior:
                    prior_miles = prior.get('parsed_miles')
                    prior_msrp  = prior.get('parsed_msrp')
        except Exception as e:
            print(f'[voice] prior lookup failed: {e}', flush=True)

        # Insert a bid row — the existing workers will pick it up on their
        # next vauto-pending poll. vauto_priority=TRUE bumps it to the
        # front of the queue. source='voice' so we can trace it back.
        bid_id = None
        try:
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    INSERT INTO bids (phone, vin, mileage, source,
                                      vauto_priority, status, raw_message,
                                      notes)
                    VALUES (%s, %s, %s, 'voice', TRUE, 'new', %s, %s)
                    RETURNING id
                """, (
                    phone_e164, vin, prior_miles,
                    f'[voice] {transcript[:300]}',
                    f'Voice-initiated bid · session={session_id} · prior_msrp={prior_msrp}',
                ))
                bid_id = cur.fetchone()['id']
                c.commit()
        except Exception as e:
            print(f'[voice] bid insert failed: {e}', flush=True)
            return jsonify({
                'reply_text': "Sorry, I couldn't start the full appraisal. Try again or contact dispatch.",
                'parsed': {'intent': 'vin', 'vin': vin},
                'structured': None,
                'needs_followup': False,
                'used_model': 'flash+rules',
                'valuation_id': None,
                'short_id': None,
                'elapsed_ms': int((time.time() - t0) * 1000),
            }), 500

        # Persist a voice_valuations row linked to this bid
        try:
            short = _short_id()
            with _pg_ew() as c, c.cursor() as cur:
                cur.execute("""
                    INSERT INTO voice_valuations
                      (short_id, raw_transcript, parsed_notes, session_id,
                       turn_index, bid_id, llm_router, sms_sent_to)
                    VALUES (%s, %s, %s, %s, %s, %s, 'vin-handoff', %s)
                    RETURNING id
                """, (short, transcript, f'VIN={vin}', session_id, turn_index,
                      bid_id, phone_e164))
                row = cur.fetchone()
                vid = row['id']
                c.commit()
        except Exception as e:
            print(f'[voice] vin persist failed: {e}', flush=True)
            vid, short = None, None

        last4 = vin[-4:]
        reply = (f"Got it. Running the full appraisal on VIN ending {last4}. "
                 f"I'll text you the report in about two minutes when vAuto, "
                 f"AccuTrade, and iPacket all land.")
        return jsonify({
            'valuation_id': vid,
            'short_id': short,
            'reply_text': reply,
            'parsed': {'intent': 'vin', 'vin': vin,
                       'prior_year':  prior.get('parsed_year')  if prior else None,
                       'prior_make':  prior.get('parsed_make')  if prior else None,
                       'prior_model': prior.get('parsed_model') if prior else None,
                       'prior_miles': prior_miles,
                       'prior_msrp':  prior_msrp},
            'structured': None,
            'bid_id': bid_id,
            'bid_link': f'/bid/{bid_id}' if bid_id else None,
            'needs_followup': False,
            'used_model': 'flash+rules',
            'elapsed_ms': int((time.time() - t0) * 1000),
        })

    # ── Conversational path (Sonnet w/ tool use) ─────────────────────
    # history already loaded above for state-machine use.
    spoken, ctx = _converse(history, transcript, max_steps=4)

    # Best-effort structured extraction from the spoken reply so we can
    # persist a target_buy for the SMS / saved-bid path.
    structured: dict = {'target_buy': None, 'range_low': None,
                        'range_high': None, 'confidence': None}
    m = re.search(r'(target|i\'?d be at|buy(?:ing)? at|around|comfortable at)[^\d]{0,30}([\d,]{2,3})\s*(thousand|k|grand)?',
                  spoken, re.I)
    if m:
        try:
            n = int(re.sub(r'\D', '', m.group(2)))
            if (m.group(3) or '').lower() in ('thousand', 'k', 'grand') or n < 1000:
                n *= 1000
            structured['target_buy'] = n
        except (TypeError, ValueError):
            pass

    elapsed = int((time.time() - t0) * 1000)

    # Persist — best effort. ctx may be None if Sonnet just chatted
    # without calling the tool (e.g., it asked for clarification).
    try:
        vid, short = _persist_valuation(
            transcript,
            parsed={
                'year':  (ctx['inputs'].get('year')  if ctx else None),
                'make':  (ctx['inputs'].get('make')  if ctx else None),
                'model': (ctx['inputs'].get('model') if ctx else None),
                'trim':  (ctx['inputs'].get('trim')  if ctx else None),
                'miles': (ctx['inputs'].get('miles') if ctx else None),
                'msrp':  (ctx['inputs'].get('msrp')  if ctx else None),
                'notes': None,
                'intent': 'valuation' if ctx else 'chat',
            },
            ctx=ctx or {
                'mmr_recent': {'summary_wholesale': _summarize([]), 'summary_retail': _summarize([])},
                'national_asking': {'summary': _summarize([])},
                'lsl_history': {'summary_purchase': _summarize([]), 'summary_sale': _summarize([])},
                'naive_target': None,
            },
            spoken=spoken,
            structured=structured,
            session_id=session_id,
            turn_index=turn_index,
            llm_ms=elapsed,
        )
    except Exception as e:
        print(f'[voice] persist failed: {e}', flush=True)
        vid, short = None, None

    return jsonify({
        'valuation_id': vid,
        'short_id': short,
        'reply_text': spoken,
        'parsed': {'year': ctx['inputs'].get('year') if ctx else None,
                   'make': ctx['inputs'].get('make') if ctx else None,
                   'model': ctx['inputs'].get('model') if ctx else None},
        'structured': structured,
        'needs_followup': not ctx,   # no comps fetched = bot is asking
        'used_model': 'sonnet-4.6-conv',
        'comps_counts': ({
            'live':     (ctx.get('live_vauto') or {}).get('live_rbook', {}).get('n', 0),
            'live_mmr': 1 if (ctx.get('live_vauto') or {}).get('live_mmr', {}).get('average_auction_price') else 0,
            'priors':   ctx['prior_bids']['summary_mmr']['n']
                        + ctx['prior_bids']['summary_rbook']['n'],
            'lsl':      ctx['lsl_history']['summary_purchase']['n'],
        } if ctx else None),
        'elapsed_ms': elapsed,
    })


@voice_bp.route('/api/voice/query/stream', methods=['POST'])
def api_voice_query_stream():
    """Server-Sent Events version of /api/voice/query.

    Events emitted (in order):
      data: {type: 'meta', session_id, vin?, intent?}
      data: {type: 'sentence', text: '...', audio_b64: '...', final?: bool}
      ... (multiple sentence events as Sonnet streams)
      data: {type: 'final', valuation_id, parsed, comps_counts, ...}

    State-machine paths (VIN_CONFIRM, OFFER_PENDING, etc.) emit a single
    sentence event then close — no real stream, but uniform client
    interface. Sonnet conversational paths stream sentence-by-sentence
    so audio starts playing 1-2s after the user stops talking.
    """
    body = request.get_json(silent=True) or {}
    transcript = (body.get('transcript') or '').strip()
    if not transcript:
        return jsonify({'error': 'transcript required'}), 400
    session_id = body.get('session_id')
    turn_index = int(body.get('turn_index') or 0)

    from flask import Response, stream_with_context

    def generate():
        try:
            t0 = time.time()

            # Delegate to the existing JSON handler internally for
            # state-machine paths (VIN_CONFIRM/OFFER_PENDING/PHONE_CONFIRM
            # /vin_readback/phone capture/garbage handling). Those return
            # fast — we just wrap their output as SSE events.
            #
            # We detect "should stream" by checking if the handler returns
            # a Sonnet conversational reply (used_model startswith 'sonnet-')
            # AND there's substantial text to stream. For now, we stream
            # ALL Sonnet conversational replies and emit instant replies
            # as single events.
            #
            # SIMPLIFICATION: rather than re-running the entire decision
            # logic, we INVOKE the existing JSON handler synchronously
            # (it's fast for state-machine paths) and for the conversational
            # path we run our own streaming Sonnet call.

            # 1) Peek at state — if it's a state-machine turn, just call
            #    the JSON handler and wrap the response as SSE.
            history = _load_session_history(session_id, limit=8)
            state = None
            if session_id:
                try:
                    with _pg_ew() as c, c.cursor() as cur:
                        cur.execute("""
                            SELECT parsed_notes FROM voice_valuations
                             WHERE session_id = %s AND parsed_notes LIKE 'STATE=%%'
                             ORDER BY created_at DESC LIMIT 1
                        """, (session_id,))
                        row = cur.fetchone()
                    if row:
                        state = dict(p.split('=', 1) for p in row['parsed_notes'].split('|') if '=' in p)
                except Exception:
                    pass

            prior_bot_reply = (history[0].get('reasoning') if history else '') or ''
            vin_candidate = _extract_vin(transcript, prior_bot_reply=prior_bot_reply)
            partial_vin = _near_vin_attempt(transcript, prior_bot_reply=prior_bot_reply)
            is_state_machine_turn = (
                (state and state.get('STATE') in (
                    'VIN_CONFIRM', 'OFFER_PENDING', 'PHONE_CONFIRM',
                    'AWAITING_PHONE', 'AWAITING_VIN', 'IDLE'))
                or vin_candidate
                or partial_vin
                or _is_confirmation(transcript)
                or _DECLINE_RE.search(transcript)
            )

            # Phone-only detection (same gate as the JSON handler)
            phone_only_digits = re.sub(r'\D', '', transcript)
            word_chars = re.sub(r'[\d\s\-\(\)\.\+]', '', transcript).strip()
            in_awaiting_phone = state and state.get('STATE') == 'AWAITING_PHONE'
            is_phone_only = (
                not in_awaiting_phone
                and (10 <= len(phone_only_digits) <= 11)
                and len(word_chars) <= 4
            )

            # For all the fast paths, call the JSON handler synchronously
            # and emit as one event.
            if is_state_machine_turn or is_phone_only:
                # Re-invoke the JSON handler in-process via test_client
                # so we don't duplicate the dispatch logic.
                from flask import current_app
                with current_app.test_client() as cli:
                    r = cli.post('/api/voice/query', json=body)
                    data = r.get_json() or {}
                spoken = (data.get('reply_text') or '').strip()
                if spoken:
                    yield _emit_sentence(spoken, final=True)
                yield _sse({'type': 'final', **{k: v for k, v in data.items()
                                                  if k != 'reply_text'},
                            'elapsed_ms': int((time.time() - t0) * 1000)})
                return

            # 2) STREAMING SONNET path — operator just asked a YMM-ish
            #    conversational question. Stream sentences as Sonnet writes
            #    them.
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
            except Exception as e:
                yield _sse({'type': 'error',
                            'message': f'anthropic init failed: {e}'})
                return

            messages = _build_message_history(history, transcript)
            ctx = None
            max_steps = 4
            full_text = ''
            sent_meta = False

            # 2a) QUICK-PARSE FAST PATH — when transcript has clean YMM,
            # skip Sonnet's tool-decision round trip entirely. Fetch comps
            # directly, then make ONE streaming Sonnet call with the comps
            # pre-loaded in the user message. Saves 2-4s per turn.
            quick = _quick_parse_ymm(transcript)
            if quick:
                print(f'[voice] QUICK fast-path: {quick}', flush=True)
                ctx = None
                # FAST LANE — master cache RE-ENABLED for speed.
                # Skips vAuto BFF (~2-3s) on hot YMMs. Master is refreshed
                # nightly so data is at most 24h old. Pre-baked Sonnet
                # narratives stay DISABLED — we still run live LLM with
                # fresh LSL/AccuTrade/historical-pay data.
                master_row = _lookup_voice_master(
                    quick['year'], quick['make'], quick['model'],
                    miles=quick.get('miles'),
                )
                if master_row:
                    print(f"[voice] master HIT id={master_row['id']} "
                          f"{master_row['year']} {master_row['make']} "
                          f"{master_row['model']}", flush=True)

                    # ── PRE-BAKED FAST LANE — DISABLED 2026-05-21 per
                    # operator request. Pre-baked narratives lose freshness
                    # and miss recent LSL deals. We keep master table for
                    # fast vAuto data caching but always run Sonnet live.
                    pre_text = None
                    pre_audio = None
                    operator_miles = quick.get('miles')
                    master_band = master_row.get('miles_band') or 15000
                    miles_close = True
                    if False and pre_text and pre_audio and miles_close:
                        elapsed = int((time.time() - t0) * 1000)
                        print(f'[voice] PRE-BAKED HIT — '
                              f'serving cached narrative + audio in {elapsed}ms',
                              flush=True)
                        yield _sse({'type': 'meta', 'status': 'comps_fetched'})
                        yield _sse({
                            'type': 'sentence',
                            'text':  pre_text,
                            'audio_b64': pre_audio,
                            'final': True,
                        })
                        # persist a row so /voice-history captures it
                        try:
                            ctx_for_persist = _context_from_master(
                                master_row, year=quick['year'],
                                make=quick['make'], model=quick['model'],
                                miles=operator_miles, msrp=quick.get('msrp'),
                            )
                            vid, short = _persist_valuation(
                                transcript,
                                parsed={'year': quick['year'], 'make': quick['make'],
                                        'model': quick['model'], 'trim': None,
                                        'miles': operator_miles, 'msrp': quick.get('msrp'),
                                        'notes': None, 'intent': 'valuation'},
                                ctx=ctx_for_persist, spoken=pre_text,
                                structured={'target_buy': None, 'range_low': None,
                                            'range_high': None, 'confidence': None},
                                session_id=session_id, turn_index=turn_index,
                                llm_ms=elapsed,
                            )
                        except Exception:
                            vid, short = None, None
                        yield _sse({
                            'type': 'final',
                            'valuation_id': vid, 'short_id': short,
                            'parsed': {'year': quick['year'], 'make': quick['make'],
                                       'model': quick['model']},
                            'comps_counts': {
                                'live':     master_row.get('rbook_listing_count') or 0,
                                'live_mmr': 1 if master_row.get('mmr_wholesale_avg') else 0,
                                'priors':   master_row.get('prior_bid_count_90d') or 0,
                                'lsl':      master_row.get('lsl_count_90d') or 0,
                            },
                            'used_model': 'pre-baked',
                            'elapsed_ms': elapsed,
                        })
                        return

                    ctx = _context_from_master(
                        master_row,
                        year=quick['year'], make=quick['make'],
                        model=quick['model'],
                        miles=quick.get('miles'), msrp=quick.get('msrp'),
                    )
                else:
                    # SLOW LANE: live vAuto BFF
                    try:
                        ctx = get_valuation_context(
                            year=quick['year'], make=quick['make'],
                            model=quick['model'], miles=quick.get('miles'),
                            msrp=quick.get('msrp'),
                        )
                    except Exception as e:
                        print(f'[voice] quick ctx fetch err: {e}', flush=True)
                        ctx = None

                if ctx:
                    yield _sse({'type': 'meta', 'status': 'comps_fetched'})
                    # Pre-load comps as both compact JSON AND an explicit
                    # MUST-CITE facts list. Sonnet's brevity priors mean
                    # raw JSON gets summarized away; named facts get cited.
                    compact = _context_compact(ctx)
                    compact_json = _json.dumps(compact, default=str)
                    if len(compact_json) > 4000:
                        compact_json = compact_json[:4000] + '...(truncated)'

                    # Build MUST-CITE facts list — these MUST appear in reply
                    facts = []
                    lv_rb = compact.get('live_vauto_rbook') or {}
                    lv_mmr = compact.get('live_vauto_mmr') or {}
                    pb_mmr = compact.get('prior_bids_mmr') or {}
                    lsl_p  = compact.get('lsl_purchase') or {}
                    at_pool = compact.get('accutrade_pool') or {}
                    rb_pool = compact.get('rbook_aggregated_pool') or {}
                    di_full = compact.get('dealer_inventory_full') or {}
                    if lv_mmr.get('average_auction_price') and lv_rb.get('n'):
                        facts.append(
                            f"Live MMR averages ${lv_mmr['average_auction_price']:,} "
                            f"at {lv_mmr.get('average_odometer',0):,} avg miles "
                            f"({lv_rb['n']} retail listings in the comp set)"
                        )
                    if pb_mmr.get('n'):
                        facts.append(
                            f"We've personally bid {pb_mmr['n']} of these in the last 90 days"
                            + (f" (avg MMR ${int(pb_mmr.get('median') or 0):,})"
                               if pb_mmr.get('median') else '')
                        )
                    if lsl_p.get('n'):
                        facts.append(
                            f"We've bought {lsl_p['n']} of these in the last 90 days"
                            + (f", avg paid ${int(lsl_p.get('median') or 0):,}"
                               if lsl_p.get('median') else '')
                        )
                    if at_pool.get('avg_guaranteed_offer'):
                        facts.append(
                            f"AccuTrade's guaranteed-offer averages ${at_pool['avg_guaranteed_offer']:,} "
                            f"across {at_pool.get('appraisal_count',0)} appraisals"
                        )
                    hpr = compact.get('historical_pay_ratio') or {}
                    if hpr.get('median_paid_pct_of_mmr') and hpr.get('sample_size'):
                        pct = round(hpr['median_paid_pct_of_mmr'] * 100, 1)
                        facts.append(
                            f"Historically across {hpr['sample_size']} matched deals, "
                            f"we paid {pct}% of MMR for these"
                        )
                    if di_full.get('active_dol_p50') is not None:
                        partner_n = len(di_full.get('active_listings') or [])
                        facts.append(
                            f"Our partner dealers have {partner_n} of these active, "
                            f"median {di_full['active_dol_p50']} days on lot"
                        )
                    elif rb_pool.get('median_dol') is not None:
                        facts.append(
                            f"Median days on lot across {rb_pool.get('comp_count','many')} aggregated comps: "
                            f"{rb_pool['median_dol']} days"
                        )

                    facts_block = ''
                    if facts:
                        facts_block = (
                            "\n\nMUST-CITE FACTS (incorporate ALL of these into your "
                            "spoken reply — naturally, in any order):\n"
                            + '\n'.join(f"  - {f}" for f in facts)
                        )

                    quick_messages = list(messages[:-1])  # drop the bare-transcript last user msg
                    # Build a directive-first user message: facts at top
                    # as REQUIRED, JSON as appendix. Sonnet treats top-of-message
                    # content as the priority signal.
                    facts_directive = ''
                    if facts:
                        facts_directive = (
                            "REQUIRED FACTS — your spoken reply must reference EVERY ONE of these "
                            "(use natural spoken language, in any order, but every fact must appear):\n"
                            + '\n'.join(f"  {i+1}. {f}" for i, f in enumerate(facts))
                            + "\n\nNow give the four-sentence spoken response following the system "
                              "prompt structure. The operator just asked:\n"
                        )
                    quick_messages.append({
                        "role": "user",
                        "content": (
                            f"{facts_directive}"
                            f"\"{transcript}\"\n\n"
                            f"[Full valuation context (reference only, the facts above are what to cite)]:\n"
                            f"{compact_json}"
                        ),
                    })
                    tts_emitter = _ParallelTTSEmitter(max_workers=4)
                    try:
                        # CEREBRAS Qwen-3-235B-Instruct — ~5-6s end-to-end
                        # vs ~8-12s on Sonnet. Cites every data point.
                        # Sonnet falls back if Cerebras 5xx's or is offline.
                        buf = ''
                        _cb_t0 = time.time()
                        try:
                            for text_chunk in _stream_cerebras_chat(
                                    quick_messages, _SYSTEM_PROMPT,
                                    max_tokens=1500, temperature=0.4):
                                buf += text_chunk
                                full_text += text_chunk
                                while True:
                                    sent, buf = _chunk_next_sentence(buf)
                                    if sent is None: break
                                    tts_emitter.submit(sent, final=False)
                                for ev in tts_emitter.flush_ready_in_order():
                                    yield ev
                            if buf.strip():
                                tts_emitter.submit(buf.strip(), final=True)
                            for ev in tts_emitter.flush_all():
                                yield ev
                            print(f'[voice] CEREBRAS-DONE {int((time.time()-_cb_t0)*1000)}ms '
                                  f'text_len={len(full_text)}', flush=True)
                        except Exception as cb_err:
                            print(f'[voice] CEREBRAS-FAIL {cb_err} '
                                  f'— Sonnet fallback', flush=True)
                            with client.messages.stream(
                                model='claude-sonnet-4-6',
                                max_tokens=600, temperature=0.4,
                                system=_SYSTEM_PROMPT,
                                messages=quick_messages,
                            ) as stream:
                                buf = ''
                                for text_chunk in stream.text_stream:
                                    buf += text_chunk
                                    full_text += text_chunk
                                    while True:
                                        sent, buf = _chunk_next_sentence(buf)
                                        if sent is None: break
                                        tts_emitter.submit(sent, final=False)
                                    for ev in tts_emitter.flush_ready_in_order():
                                        yield ev
                                if buf.strip():
                                    tts_emitter.submit(buf.strip(), final=True)
                            for ev in tts_emitter.flush_all():
                                yield ev
                    finally:
                        tts_emitter.shutdown()

                    # Persist + final event (same as tool-use path below)
                    elapsed = int((time.time() - t0) * 1000)
                    structured = {'target_buy': None, 'range_low': None,
                                  'range_high': None, 'confidence': None}
                    try:
                        vid, short = _persist_valuation(
                            transcript,
                            parsed={'year': quick['year'], 'make': quick['make'],
                                    'model': quick['model'], 'trim': None,
                                    'miles': quick.get('miles'), 'msrp': quick.get('msrp'),
                                    'notes': None, 'intent': 'valuation'},
                            ctx=ctx, spoken=full_text, structured=structured,
                            session_id=session_id, turn_index=turn_index, llm_ms=elapsed,
                        )
                    except Exception:
                        vid, short = None, None
                    yield _sse({
                        'type': 'final',
                        'valuation_id': vid, 'short_id': short,
                        'parsed': {'year': quick['year'], 'make': quick['make'],
                                   'model': quick['model']},
                        'comps_counts': {
                            'live':     (ctx.get('live_vauto') or {}).get('live_rbook', {}).get('n', 0),
                            'live_mmr': 1 if (ctx.get('live_vauto') or {}).get('live_mmr', {}).get('average_auction_price') else 0,
                            'priors':   ctx['prior_bids']['summary_mmr']['n']
                                        + ctx['prior_bids']['summary_rbook']['n'],
                            'lsl':      ctx['lsl_history']['summary_purchase']['n'],
                        },
                        'used_model': 'sonnet-4.6-stream-quick',
                        'elapsed_ms': elapsed,
                    })
                    return
                # ctx was None → fall through to the full tool-use flow

            for step in range(max_steps):
                # First call (and potentially intermediate tool-result calls)
                # are non-streaming so we can inspect stop_reason.
                # Last call (the text reply after tool result) is streamed.
                use_stream = (step > 0)  # stream the post-tool reply

                if not use_stream:
                    # Tool-decision step uses thinking too — better at
                    # deciding when comp lookup is worth it.
                    resp = client.messages.create(
                        model='claude-sonnet-4-6',
                        max_tokens=600, temperature=0.4,
                        # thinking disabled � too slow for voice UX
                        system=_SYSTEM_PROMPT, tools=[_TOOL_GET_CONTEXT],
                        messages=messages,
                    )
                    if resp.stop_reason == 'tool_use':
                        # Execute tool, append result, loop with streaming on
                        messages.append({"role": "assistant", "content": resp.content})
                        tool_results = []
                        for b in resp.content:
                            if b.type == 'tool_use':
                                if b.name == 'get_valuation_context':
                                    try:
                                        args = b.input or {}
                                        ctx = get_valuation_context(
                                            year=int(args.get('year') or 0),
                                            make=str(args.get('make') or ''),
                                            model=str(args.get('model') or ''),
                                            trim=args.get('trim'),
                                            miles=args.get('miles'),
                                            msrp=args.get('msrp'),
                                        )
                                        payload = _context_compact(ctx)
                                    except Exception as e:
                                        payload = {"error": f"{type(e).__name__}: {e}"}
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": b.id,
                                        "content": _json.dumps(payload, default=str),
                                    })
                        messages.append({"role": "user", "content": tool_results})
                        # Emit a quick "looking at comps..." meta so user knows we're working
                        if not sent_meta:
                            yield _sse({'type': 'meta', 'status': 'comps_fetched'})
                            sent_meta = True
                        continue
                    # end_turn — Sonnet wrote text directly without tool use.
                    # Emit that text as a single sentence (no streaming benefit
                    # since the whole reply is already in memory).
                    text_parts = [b.text for b in resp.content if b.type == 'text']
                    spoken = ' '.join(t.strip() for t in text_parts).strip()
                    full_text = spoken
                    if spoken:
                        # Chunk into sentences for parallel TTS
                        buf = spoken
                        while True:
                            sent, buf = _chunk_next_sentence(buf)
                            if sent is None: break
                            yield _emit_sentence(sent)
                        if buf.strip():
                            yield _emit_sentence(buf.strip(), final=True)
                    break

                # STREAMING step — post-tool-result Sonnet reply.
                # Submit sentences for parallel TTS as they arrive; emit
                # any ready-at-head sentences during streaming so audio
                # starts ASAP. Blocking final drain at the end.
                tts_emitter = _ParallelTTSEmitter(max_workers=4)
                with client.messages.stream(
                    model='claude-sonnet-4-6',
                    max_tokens=600, temperature=0.4,
                    # thinking disabled � too slow for voice UX
                    system=_SYSTEM_PROMPT, tools=[_TOOL_GET_CONTEXT],
                    messages=messages,
                ) as stream:
                    buf = ''
                    for text_chunk in stream.text_stream:
                        buf += text_chunk
                        full_text += text_chunk
                        while True:
                            sent, buf = _chunk_next_sentence(buf)
                            if sent is None: break
                            tts_emitter.submit(sent, final=False)
                        # Non-blocking flush of any ready-at-head sentences
                        for ev in tts_emitter.flush_ready_in_order():
                            yield ev
                    if buf.strip():
                        tts_emitter.submit(buf.strip(), final=True)
                # Block-drain any sentences still finishing TTS
                for ev in tts_emitter.flush_all():
                    yield ev
                tts_emitter.shutdown()
                break

            # 3) Persist + final event
            structured = {'target_buy': None, 'range_low': None,
                          'range_high': None, 'confidence': None}
            elapsed = int((time.time() - t0) * 1000)
            try:
                vid, short = _persist_valuation(
                    transcript,
                    parsed={
                        'year':  (ctx['inputs'].get('year')  if ctx else None),
                        'make':  (ctx['inputs'].get('make')  if ctx else None),
                        'model': (ctx['inputs'].get('model') if ctx else None),
                        'trim':  (ctx['inputs'].get('trim')  if ctx else None),
                        'miles': (ctx['inputs'].get('miles') if ctx else None),
                        'msrp':  (ctx['inputs'].get('msrp')  if ctx else None),
                        'notes': None,
                        'intent': 'valuation' if ctx else 'chat',
                    },
                    ctx=ctx or {
                        'mmr_recent': {'summary_wholesale': _summarize([]), 'summary_retail': _summarize([])},
                        'national_asking': {'summary': _summarize([])},
                        'lsl_history': {'summary_purchase': _summarize([]), 'summary_sale': _summarize([])},
                        'naive_target': None,
                    },
                    spoken=full_text,
                    structured=structured,
                    session_id=session_id,
                    turn_index=turn_index,
                    llm_ms=elapsed,
                )
            except Exception:
                vid, short = None, None

            yield _sse({
                'type': 'final',
                'valuation_id': vid,
                'short_id': short,
                'parsed': {'year': ctx['inputs'].get('year') if ctx else None,
                           'make': ctx['inputs'].get('make') if ctx else None,
                           'model': ctx['inputs'].get('model') if ctx else None},
                'comps_counts': ({
                    'live':     (ctx.get('live_vauto') or {}).get('live_rbook', {}).get('n', 0),
                    'live_mmr': 1 if (ctx.get('live_vauto') or {}).get('live_mmr', {}).get('average_auction_price') else 0,
                    'priors':   ctx['prior_bids']['summary_mmr']['n']
                                + ctx['prior_bids']['summary_rbook']['n'],
                    'lsl':      ctx['lsl_history']['summary_purchase']['n'],
                } if ctx else None),
                'used_model': 'sonnet-4.6-stream',
                'elapsed_ms': elapsed,
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            yield _sse({'type': 'error', 'message': f'{type(e).__name__}: {e}'})

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={
                        'Cache-Control': 'no-cache, no-transform',
                        'X-Accel-Buffering': 'no',
                        'Connection': 'keep-alive',
                    })


@voice_bp.route('/api/voice/tts', methods=['POST'])
def api_voice_tts():
    """Synthesize speech via Google Cloud TTS (Neural2 voice).
    Body: { text: str, voice?: str (default 'en-US-Neural2-D'), rate?: float }
    Returns audio/mpeg MP3 bytes. ~1-2s typical, ~$0.005/query at our scale."""
    body = request.get_json(silent=True) or {}
    text = (body.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'text required'}), 400
    if len(text) > 4000:
        text = text[:4000]
    voice_name = body.get('voice') or 'en-US-Neural2-D'
    rate = float(body.get('rate') or 1.05)
    try:
        from google.cloud import texttospeech
        client = texttospeech.TextToSpeechClient()
        si = texttospeech.SynthesisInput(text=text)
        v = texttospeech.VoiceSelectionParams(language_code='en-US', name=voice_name)
        ac = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=rate,
        )
        resp = client.synthesize_speech(input=si, voice=v, audio_config=ac)
        from flask import Response
        return Response(resp.audio_content, mimetype='audio/mpeg', headers={
            'Cache-Control': 'no-store',
        })
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500


@voice_bp.route('/api/voice/<int:valuation_id>/sms', methods=['POST'])
def api_voice_sms(valuation_id):
    """Send the spoken valuation summary as an SMS via Twilio.
    Body: { phone: str (E.164 or US 10-digit) }
    Reuses the EW Twilio account already configured in env."""
    body = request.get_json(silent=True) or {}
    phone_raw = (body.get('phone') or '').strip()
    if not phone_raw:
        return jsonify({'error': 'phone required'}), 400

    # Normalize to E.164. Accept "5551234567", "(555) 123-4567", "+15551234567".
    digits = re.sub(r'\D', '', phone_raw)
    if len(digits) == 10:
        phone = '+1' + digits
    elif len(digits) == 11 and digits.startswith('1'):
        phone = '+' + digits
    elif phone_raw.startswith('+') and len(digits) >= 10:
        phone = '+' + digits
    else:
        return jsonify({'error': 'invalid phone format'}), 400

    # Pull the valuation row
    try:
        with _pg_ew() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, short_id, parsed_year, parsed_make, parsed_model,
                       parsed_trim, parsed_miles, parsed_msrp,
                       target_buy, range_low, range_high, confidence, reasoning,
                       comps_snapshot
                  FROM voice_valuations
                 WHERE id = %s
            """, (valuation_id,))
            row = cur.fetchone()
    except Exception as e:
        return jsonify({'error': f'db: {type(e).__name__}: {e}'}), 500
    if not row:
        return jsonify({'error': 'valuation not found'}), 404

    # Format SMS body
    def _money(n): return f'${int(n):,}' if n is not None else 'n/a'
    snap = row.get('comps_snapshot') or {}
    ymm_line = ' '.join(str(x) for x in [row.get('parsed_year'),
                                          row.get('parsed_make'),
                                          row.get('parsed_model'),
                                          row.get('parsed_trim')] if x)
    if row.get('parsed_miles'):
        ymm_line += f" · {int(row['parsed_miles']):,} mi"

    lines = [
        'EW Voice Valuation',
        ymm_line,
        '─────────────',
        f'Target buy: {_money(row["target_buy"])}',
        f'Range: {_money(row["range_low"])} – {_money(row["range_high"])}',
        f'Confidence: {row.get("confidence") or "n/a"}',
    ]
    if isinstance(snap, dict):
        if snap.get('mmr_n'):  lines.append(f'MMR comps: {snap.get("mmr_n")}')
        if snap.get('nat_n'):  lines.append(f'National asks: {snap.get("nat_n")}')
        if snap.get('lsl_n'):  lines.append(f"We've paid (LSL): {snap.get('lsl_n')} prior")
    sms_body = '\n'.join(lines)

    # Send via Twilio
    try:
        from twilio.rest import Client as _TwClient
        client = _TwClient(os.environ['TWILIO_ACCOUNT_SID'],
                           os.environ['TWILIO_AUTH_TOKEN'])
        msg = client.messages.create(
            body=sms_body,
            from_=os.environ['TWILIO_PHONE'],
            to=phone,
        )
        sid = msg.sid
    except Exception as e:
        return jsonify({'error': f'twilio: {type(e).__name__}: {e}'}), 500

    # Stamp sent_at on the row
    try:
        with _pg_ew() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE voice_valuations SET sms_sent_at = NOW(), sms_sent_to = %s
                 WHERE id = %s
            """, (phone, valuation_id))
            c.commit()
    except Exception as e:
        # SMS already sent, log the persistence failure but don't error
        print(f'[voice] sms stamp failed for {valuation_id}: {e}', flush=True)

    return jsonify({'sent': True, 'to': phone, 'sid': sid,
                    'body_preview': sms_body[:200]})


@voice_bp.route('/api/voice/stt', methods=['POST'])
def api_voice_stt():
    """Transcribe iOS Safari MediaRecorder audio.

    Path A — Google Cloud Speech-to-Text (~0.8-1.5s for typical clips).
    Path B — Gemini Flash audio fallback (~2-3s) when GCloud rejects the
    mime type or returns empty.
    """
    f = request.files.get('audio')
    if not f:
        return jsonify({'error': 'audio file required (multipart field "audio")'}), 400
    blob = f.read()
    if not blob:
        return jsonify({'error': 'empty audio'}), 400
    mime = f.mimetype or 'audio/mp4'
    if not mime.startswith('audio/'):
        mime = 'audio/mp4'

    # ── Path A: Google Cloud Speech-to-Text ────────────────────────────
    # ~2x faster than Gemini for short clips. iOS Safari sends audio/mp4
    # (AAC). We try MP3/M4A encoding hints; if GCloud can't decode the
    # codec we fall through to Gemini.
    try:
        from google.cloud import speech as gcs
        gcs_client = gcs.SpeechClient()
        # Detect encoding from mime
        enc = gcs.RecognitionConfig.AudioEncoding.MP3  # safest broad default
        if 'webm' in mime:
            enc = gcs.RecognitionConfig.AudioEncoding.WEBM_OPUS
        elif 'mp4' in mime or 'aac' in mime or 'm4a' in mime:
            # AAC/MP4 audio — let GCloud auto-detect by passing ENCODING_UNSPECIFIED
            enc = gcs.RecognitionConfig.AudioEncoding.ENCODING_UNSPECIFIED
        config = gcs.RecognitionConfig(
            encoding=enc,
            language_code='en-US',
            model='latest_short',           # tuned for ≤60s clips
            enable_automatic_punctuation=True,
            use_enhanced=True,
        )
        audio = gcs.RecognitionAudio(content=blob)
        resp = gcs_client.recognize(config=config, audio=audio, timeout=8)
        results = list(resp.results)
        if results:
            transcript = ' '.join(r.alternatives[0].transcript for r in results
                                   if r.alternatives).strip()
            if transcript:
                return jsonify({'transcript': transcript, 'mime': mime,
                                'size_bytes': len(blob), 'engine': 'gcloud-stt'})
    except Exception as e:
        print(f'[voice] gcloud-stt err (will fall back to gemini): {e}', flush=True)

    # ── Path B: Gemini Flash fallback ──────────────────────────────────
    try:
        from app import _gemini
        client = _gemini()
        if not client:
            return jsonify({'error': 'no STT engine available'}), 503
        from google.genai import types
        contents = [
            types.Part.from_bytes(data=blob, mime_type=mime),
            "Transcribe this audio verbatim into English. "
            "Output ONLY the spoken words — no quotes, no labels, no formatting, no commentary. "
            "If you cannot make out the audio, respond with an empty string.",
        ]
        cfg = types.GenerateContentConfig(
            max_output_tokens=400, temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        resp = client.models.generate_content(
            model='gemini-3.5-flash', contents=contents, config=cfg,
        )
        transcript = (resp.text or '').strip().strip('"').strip()
        return jsonify({'transcript': transcript, 'mime': mime,
                        'size_bytes': len(blob), 'engine': 'gemini-flash'})
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}',
                        'mime': mime, 'size_bytes': len(blob)}), 500


@voice_bp.route('/model/ewbot', methods=['GET'])
@voice_bp.route('/ewbot',        methods=['GET'])
@voice_bp.route('/mobile/ewbot/', methods=['GET'])
@voice_bp.route('/bot',          methods=['GET'])
@voice_bp.route('/m/ewbot',      methods=['GET'])
def mobile_ewbot_alias():
    """301 PERMANENT redirect to canonical /mobile/ewbot so every variant
    funnels to the same URL bar. Preserves query string so cache-busts
    like ?v=stream still propagate."""
    from flask import redirect, request as _req
    qs = ('?' + _req.query_string.decode()) if _req.query_string else ''
    return redirect('/mobile/ewbot' + qs, code=301)


@voice_bp.route('/mobile/ewbot', methods=['GET'])
def mobile_ewbot():
    """Mobile-first voice UI. Browser STT + browser TTS for now.
    Aggressive no-cache: iOS Safari + Chrome aggressively cache HTML, which
    pinned operators to a stale build during Day 2. Force a fresh fetch on
    every visit until the bot is stable."""
    import datetime as _dt
    resp = render_template('mobile_ewbot.html',
                           build_version=_dt.datetime.now().strftime('%Y%m%d-%H%M%S'))
    from flask import make_response
    r = make_response(resp)
    r.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    r.headers['Pragma'] = 'no-cache'
    r.headers['Expires'] = '0'
    return r


@voice_bp.route('/api/voice/health', methods=['GET'])
def api_voice_health():
    out = {'ok': True, 'checks': {}}
    try:
        with _pg_ew() as c, c.cursor() as cur:
            cur.execute('SELECT 1')
            out['checks']['ew_db'] = 'ok'
    except Exception as e:
        out['ok'] = False
        out['checks']['ew_db'] = f'{type(e).__name__}: {e}'
    try:
        with _pg_di() as c, c.cursor() as cur:
            cur.execute('SELECT 1')
            out['checks']['di_db'] = 'ok'
    except Exception as e:
        out['ok'] = False
        out['checks']['di_db'] = f'{type(e).__name__}: {e}'
    out['checks']['lsl_db'] = 'ok' if os.path.exists(LSL_DB_PATH) else 'missing'
    return jsonify(out)
