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
        'mmr_recent': {'rows': [], 'summary_wholesale': _summarize([]),
                       'summary_retail': _summarize([])},
        'national_asking': {'rows': [], 'summary': _summarize([])},
        'lsl_history': {'rows': [], 'summary_sale': _summarize([]),
                        'summary_purchase': _summarize([])},
        'errors': [],
    }

    make_n = _norm(make)
    model_n = _norm(model)
    miles_lo, miles_hi = _miles_band(miles)

    # 1) MMR recent
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
            out['mmr_recent']['rows'] = rows
            out['mmr_recent']['summary_wholesale'] = _summarize(
                [float(r['wholesale_avg']) for r in rows if r.get('wholesale_avg')])
            out['mmr_recent']['summary_retail'] = _summarize(
                [float(r['retail_avg']) for r in rows if r.get('retail_avg')])
    except Exception as e:
        out['errors'].append(f'mmr_recent: {type(e).__name__}: {e}')

    # 2) National asking
    try:
        with _pg_di() as c, c.cursor() as cur:
            sql = """
                SELECT year, make, model, trim, mileage, price, dealer_id
                  FROM inventory
                 WHERE year = %s
                   AND UPPER(make)   = %s
                   AND UPPER(model) LIKE %s
                   AND status = 'active'
                   AND price IS NOT NULL
                   AND price >= 2000
            """
            args = [year, make_n, f'%{model_n}%']
            if miles_lo is not None:
                sql += "   AND mileage BETWEEN %s AND %s\n"
                args.extend([miles_lo, miles_hi])
            sql += " ORDER BY price ASC LIMIT %s"
            args.append(limit_each)
            cur.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get('price') is not None:
                    r['price'] = float(r['price'])
            out['national_asking']['rows'] = rows
            out['national_asking']['summary'] = _summarize(
                [r['price'] for r in rows if r.get('price')])
    except Exception as e:
        out['errors'].append(f'national_asking: {type(e).__name__}: {e}')

    # 3) LSL deals
    try:
        c = _lsl_ro()
        if c is not None:
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
                 ORDER BY sold_at DESC
                 LIMIT ?
            """
            args = [f'%{make_n}%', f'%{model_n}%', f'%{year}%', limit_each]
            try:
                cur.execute(sql, args)
                rows = [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError as e:
                out['errors'].append(f'lsl_history schema: {e}')
                rows = []
            c.close()
            out['lsl_history']['rows'] = rows
            out['lsl_history']['summary_purchase'] = _summarize(
                [float(r['purchase_cost']) for r in rows if r.get('purchase_cost')])
            out['lsl_history']['summary_sale'] = _summarize(
                [float(r['sale_price']) for r in rows if r.get('sale_price')])
    except Exception as e:
        out['errors'].append(f'lsl_history: {type(e).__name__}: {e}')

    # Naive blended target (sanity baseline; Gemini synthesizes the real one)
    mmr_med = out['mmr_recent']['summary_wholesale'].get('median')
    nat_med = out['national_asking']['summary'].get('median')
    lsl_med = out['lsl_history']['summary_purchase'].get('median')
    naive_target = None
    wts: list[tuple[str, float, float]] = []
    if mmr_med: wts.append(('mmr', mmr_med, 0.5))
    if lsl_med: wts.append(('lsl', lsl_med, 0.35))
    if nat_med: wts.append(('nat_x0.92', nat_med * 0.92, 0.15))
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


def _parse_transcript(transcript: str) -> dict:
    """Use Gemini Flash to parse a spoken vehicle query.
    Falls back to a minimal parse if Gemini is unavailable."""
    from app import gemini_call  # lazy to avoid circular
    raw = gemini_call(
        _PARSE_PROMPT.replace('{transcript}', transcript),
        model='gemini-2.5-flash',
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

Our prior LSL purchases on this YMM:
  rows: {lsl_n}   median paid: {lsl_purch_median}   median sold: {lsl_sale_median}

Naive blended target (sanity baseline only): {naive_target}

OUTPUT FORMAT — first line MUST be JSON on a single line:
{{"target_buy":NNNNN,"range_low":NNNNN,"range_high":NNNNN,"confidence":"low"|"medium"|"high"}}

Then a blank line, then the spoken response (≤ 65 words) that EW will speak.
The spoken response should:
- Lead with the target buy number, e.g. "Target buy around sixty-eight five."
- Cite 1-2 supporting facts: MMR median, prior-paid median, or national spread.
- If sample sizes are thin (<3 comps total), explicitly call out lower confidence.
- End with: "Want the full appraisal? I'll need the VIN."

DO NOT include numbers like "$68,500" — TTS reads them better as words.
Use natural numbers ("sixty-eight thousand five hundred"). Round to nearest 500.
"""


def _format_money(n: Optional[float]) -> str:
    return f'${int(n):,}' if n is not None else 'n/a'


def _generate_reply(parsed: dict, ctx: dict) -> tuple[str, dict]:
    """Call Gemini Flash, return (spoken_reply_text, structured_dict).
    Structured dict has target_buy/range_low/range_high/confidence."""
    from app import gemini_call

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

    raw = gemini_call(prompt, model='gemini-2.5-flash',
                      max_tokens=600, temperature=0.4, disable_thinking=True)
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
    snapshot = {
        'mmr_recent_summary': ctx['mmr_recent']['summary_wholesale'],
        'national_summary': ctx['national_asking']['summary'],
        'lsl_summary_purchase': ctx['lsl_history']['summary_purchase'],
        'lsl_summary_sale': ctx['lsl_history']['summary_sale'],
        'naive_target': ctx.get('naive_target'),
        'mmr_n': ctx['mmr_recent']['summary_wholesale']['n'],
        'nat_n': ctx['national_asking']['summary']['n'],
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
                    'flash', llm_ms,
                ))
                row = cur.fetchone()
                c.commit()
                return row['id'], short
            except psycopg2.errors.UniqueViolation:
                c.rollback()
                short = _short_id()
        raise RuntimeError("short_id collision after 3 retries")


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
    parsed = _parse_transcript(transcript)

    # If parser couldn't extract enough, ask a follow-up (do NOT call Pro).
    missing = parsed.get('missing_fields') or []
    if missing or not (parsed.get('year') and parsed.get('make') and parsed.get('model')):
        miss_label = ', '.join(missing) if missing else 'year, make, and model'
        reply = f"I didn't catch the {miss_label}. Could you say that again?"
        return jsonify({
            'reply_text': reply,
            'parsed': parsed,
            'structured': None,
            'needs_followup': True,
            'used_model': 'flash',
            'valuation_id': None,
            'short_id': None,
            'elapsed_ms': int((time.time() - t0) * 1000),
        })

    # Got enough — fetch comps, generate spoken reply
    ctx = get_valuation_context(
        year=int(parsed['year']),
        make=str(parsed['make']),
        model=str(parsed['model']),
        trim=parsed.get('trim'),
        miles=parsed.get('miles'),
        msrp=parsed.get('msrp'),
    )
    spoken, structured = _generate_reply(parsed, ctx)
    elapsed = int((time.time() - t0) * 1000)

    try:
        vid, short = _persist_valuation(
            transcript, parsed, ctx, spoken, structured,
            session_id, turn_index, elapsed,
        )
    except Exception as e:
        return jsonify({'error': f'persist_failed: {type(e).__name__}: {e}',
                        'reply_text': spoken,
                        'parsed': parsed, 'structured': structured,
                        'needs_followup': False, 'used_model': 'flash',
                        'elapsed_ms': elapsed}), 500

    return jsonify({
        'valuation_id': vid,
        'short_id': short,
        'reply_text': spoken,
        'parsed': parsed,
        'structured': structured,
        'needs_followup': False,
        'used_model': 'flash',
        'comps_counts': {
            'mmr': ctx['mmr_recent']['summary_wholesale']['n'],
            'national': ctx['national_asking']['summary']['n'],
            'lsl': ctx['lsl_history']['summary_purchase']['n'],
        },
        'elapsed_ms': elapsed,
    })


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
