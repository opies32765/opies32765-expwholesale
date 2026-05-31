"""YMMT catalog matcher — resolve messy (year, make, model, trim) strings
to a canonical row in ymmt_catalog.

Pattern mirrors claude_trim_match.py: lazy thread-safe Anthropic client,
forever cache, falls open on infra failure so callers never starve.

Public API:
    resolve_ymmt(year, make, raw_model, raw_trim, db_conn=None) -> dict
       {ymmt_id, year, make, model, trim, body_style, market_segment,
        confidence, source, reason}
       source in {'exact','fuzzy','cache','claude_sonnet_4_6',
                  'claude_error','disabled','no_candidates','fuzzy_degraded'}
"""
from __future__ import annotations
import os
import re
import json
import threading

ANTHROPIC_MODEL = 'claude-sonnet-4-6'
CLAUDE_TIMEOUT_SEC = 12
MAX_TOKENS = 250
FUZZY_ACCEPT = 0.70
TOP_K_FOR_LLM = 12

_client = None
_client_lock = threading.Lock()
_JSON_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```")

_NOISE_TOKENS = {
    '4matic', '4matic®', 'xdrive', 'quattro', 'awd', 'rwd', 'fwd', '4wd', '2wd',
    '4dr', '2dr', '4door', '2door', 'sedan', 'wagon', 'hatchback',
    'suv', 'pickup', 'crew', 'cab', 'crewcab',
    'class', '-class',
    'used', 'new', 'cpo', 'certified', 'preowned',
    'stk', 'stock',
}
_PUNCT_RE = re.compile(r"[®©™/\\\-_,;:|()\[\]\"\']")
_WS_RE = re.compile(r"\s+")
_STOCK_RE = re.compile(r"\bstk?\s*\w+\b", re.I)
# Split glued letter+digit trims so "GLS450" tokenizes the same way the
# catalog stores "GLS 450". Requires 1+ letters and 3+ digits so M3/GT3/
# 911/i7/c43 stay intact (those have <3 trailing digits) while G550, E450,
# C350, M850 split cleanly.
_SPLIT_GLUED_RE = re.compile(r"([a-z]+)(\d{3,})")


def _norm_tokens(s):
    if not s:
        return []
    s = str(s).lower()
    s = _STOCK_RE.sub(' ', s)
    s = _PUNCT_RE.sub(' ', s)
    s = _SPLIT_GLUED_RE.sub(r'\1 \2', s)
    s = _WS_RE.sub(' ', s).strip()
    return [t for t in s.split() if t and t not in _NOISE_TOKENS]


def _norm_for_key(s):
    return ' '.join(_norm_tokens(s))


def _score(raw_tokens, cand_tokens, raw_trim_tokens, cand_trim_tokens):
    if not cand_tokens:
        return 0.0
    rs, cs = set(raw_tokens), set(cand_tokens)
    if not rs or not cs:
        return 0.0
    inter = rs & cs
    union = rs | cs
    jac = len(inter) / len(union)
    bonus = 0.0
    if cand_trim_tokens:
        present = sum(1 for t in cand_trim_tokens if t in rs)
        bonus = 0.10 * (present / max(1, len(cand_trim_tokens)))
    if cand_trim_tokens and not raw_trim_tokens:
        missing = [t for t in cand_trim_tokens if t not in rs]
        if missing:
            bonus -= 0.15
    return min(1.0, jac + bonus)


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                try:
                    from anthropic import Anthropic
                    _client = Anthropic()
                except Exception as e:
                    print(f'[ymmt_match] anthropic init failed: {e}', flush=True)
                    _client = False
    return _client if _client else None


_SYSTEM = ("You output ONLY a single JSON object. No preamble, no markdown. "
           "First char is '{' and last is '}'.")

_USER_PROMPT = """Pick the catalog row that best matches the raw vehicle description.

RAW vehicle:
  year:  {year}
  make:  {make}
  model: "{raw_model}"
  trim:  "{raw_trim}"

CANDIDATES (id, year, model, trim):
{candidates}

Rules:
- Pick ONE candidate id if any clearly refers to the same factory variant.
- Ignore drivetrain (4MATIC, xDrive, AWD), body style (Coupe, Sedan, SUV), door count, country symbols, stock numbers.
- Trim hierarchy matters: "GLS 450" != "GLS 580" != "AMG GLS 63" != "Maybach GLS 600".
- "Maybach GLS 600" should match raw "Maybach 600" or "Mercedes-Maybach GLS 600 4MATIC".
- "AMG GLS 63" should match raw "63 AMG", "GLS 63 AMG", or "GLS63".
- "GLS 450" should match raw "450 4MATIC" or "GLS450 4MATIC".
- If raw trim is empty AND multiple candidates exist for that model, return id=null.
- If no candidate is a clear factory-variant match, return id=null.

Return ONLY: {{"id": <int or null>, "confidence": <0..1>, "reason": "<one short sentence>"}}
"""


def _parse_json(txt):
    if not txt:
        return None
    txt = txt.strip()
    if '```' in txt:
        txt = _FENCE_RE.sub('', txt)
    m = _JSON_RE.search(txt)
    if m:
        txt = m.group(0)
    try:
        return json.loads(txt)
    except Exception:
        return None


def _cache_get(db_conn, year, make_u, raw_model_n, raw_trim_n):
    if db_conn is None:
        return None
    try:
        with db_conn.cursor() as cur:
            cur.execute("""SELECT ymmt_id, confidence, source, reason
                             FROM ymmt_resolve_cache
                            WHERE year=%s AND make=%s
                              AND raw_model_n=%s AND raw_trim_n=%s
                            LIMIT 1""",
                        (year, make_u, raw_model_n, raw_trim_n))
            row = cur.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                return {'ymmt_id': row['ymmt_id'],
                        'confidence': float(row['confidence'] or 0),
                        'source': 'cache',
                        'reason': row['reason'] or ''}
            return {'ymmt_id': row[0], 'confidence': float(row[1] or 0),
                    'source': 'cache', 'reason': row[3] or ''}
    except Exception as e:
        print(f'[ymmt_match] cache read err: {e!r}', flush=True)
        try:
            db_conn.rollback()
        except Exception:
            pass
        return None


def _cache_put(db_conn, year, make_u, raw_model_n, raw_trim_n, result):
    if db_conn is None:
        return
    try:
        with db_conn.cursor() as cur:
            cur.execute("""INSERT INTO ymmt_resolve_cache
                            (year, make, raw_model_n, raw_trim_n,
                             ymmt_id, confidence, source, reason)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                          ON CONFLICT (year, make, raw_model_n, raw_trim_n)
                          DO UPDATE SET ymmt_id=EXCLUDED.ymmt_id,
                                        confidence=EXCLUDED.confidence,
                                        source=EXCLUDED.source,
                                        reason=EXCLUDED.reason,
                                        decided_at=NOW()""",
                        (year, make_u, raw_model_n, raw_trim_n,
                         result.get('ymmt_id'),
                         float(result.get('confidence') or 0),
                         result.get('source') or 'unknown',
                         (result.get('reason') or '')[:500]))
            db_conn.commit()
    except Exception as e:
        print(f'[ymmt_match] cache write err: {e!r}', flush=True)
        try:
            db_conn.rollback()
        except Exception:
            pass


def _load_candidates(db_conn, year, make_u):
    if db_conn is None:
        return []
    try:
        with db_conn.cursor() as cur:
            cur.execute("""SELECT id, year, make, model, trim,
                                  body_style, market_segment
                             FROM ymmt_catalog
                            WHERE UPPER(make)=%s AND year BETWEEN %s AND %s
                              AND is_active=true""",
                        (make_u, year - 1, year + 1))
            rows = [dict(r) if isinstance(r, dict) else
                    {'id': r[0], 'year': r[1], 'make': r[2], 'model': r[3],
                     'trim': r[4], 'body_style': r[5], 'market_segment': r[6]}
                    for r in cur.fetchall()]
            if rows:
                return rows
            cur.execute("""SELECT id, year, make, model, trim,
                                  body_style, market_segment
                             FROM ymmt_catalog
                            WHERE UPPER(make)=%s AND is_active=true""",
                        (make_u,))
            return [dict(r) if isinstance(r, dict) else
                    {'id': r[0], 'year': r[1], 'make': r[2], 'model': r[3],
                     'trim': r[4], 'body_style': r[5], 'market_segment': r[6]}
                    for r in cur.fetchall()]
    except Exception as e:
        print(f'[ymmt_match] candidate load err: {e!r}', flush=True)
        try:
            db_conn.rollback()
        except Exception:
            pass
        return []


def _ask_claude(year, make, raw_model, raw_trim, candidates):
    cand_lines = '\n'.join(
        f"  - id={c['id']} year={c['year']} model={c['model']!r} trim={c['trim']!r}"
        for c in candidates[:TOP_K_FOR_LLM])
    prompt = _USER_PROMPT.format(
        year=year, make=make, raw_model=raw_model or '',
        raw_trim=raw_trim or '', candidates=cand_lines)
    # GEMINI_MIGRATION_2026_05_29: Gemini (Vertex) replaces Anthropic here.
    try:
        from gemini_helper import gemini_text
    except Exception as e:
        print(f'[gemini_ymmt] gemini_helper import failed: {e}', flush=True)
        return {'ymmt_id': None, 'confidence': 0.0,
                'source': 'disabled', 'reason': 'gemini unavailable'}
    try:
        txt = gemini_text(_SYSTEM + "\n\n" + prompt,
                          model='gemini-3.5-flash', max_tokens=1024, temperature=0.0, thinking_budget=0) or ""
        parsed = _parse_json(txt)
        if not parsed:
            return {'ymmt_id': None, 'confidence': 0.0,
                    'source': 'claude_error',
                    'reason': f'parse_failed: {txt[:150]!r}'}
        ymmt_id = parsed.get('id')
        if ymmt_id is not None:
            try:
                ymmt_id = int(ymmt_id)
            except Exception:
                ymmt_id = None
        return {'ymmt_id': ymmt_id,
                'confidence': float(parsed.get('confidence') or 0.0),
                'source': 'claude_sonnet_4_6',
                'reason': str(parsed.get('reason') or '')[:500]}
    except Exception as e:
        print(f'[ymmt_match] api err: {e!r}', flush=True)
        return {'ymmt_id': None, 'confidence': 0.0,
                'source': 'claude_error', 'reason': f'api_error: {e}'}


def _fetch_row(db_conn, ymmt_id):
    if db_conn is None or not ymmt_id:
        return None
    try:
        with db_conn.cursor() as cur:
            cur.execute("""SELECT year, make, model, trim, body_style, market_segment
                             FROM ymmt_catalog WHERE id=%s""", (ymmt_id,))
            row = cur.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                return {'year': row['year'], 'make': row['make'],
                        'model': row['model'], 'trim': row['trim'],
                        'body_style': row['body_style'],
                        'market_segment': row['market_segment']}
            return {'year': row[0], 'make': row[1], 'model': row[2],
                    'trim': row[3], 'body_style': row[4], 'market_segment': row[5]}
    except Exception as e:
        print(f'[ymmt_match] row fetch err: {e!r}', flush=True)
        try:
            db_conn.rollback()
        except Exception:
            pass
        return None


def resolve_ymmt(year, make, raw_model, raw_trim, db_conn=None):
    """Resolve a messy (year, make, model, trim) to ymmt_catalog row."""
    base = {'ymmt_id': None, 'confidence': 0.0,
            'source': 'no_candidates', 'reason': '',
            'year': year, 'make': make, 'model': None, 'trim': None,
            'body_style': None, 'market_segment': None}
    if not year or not make:
        base['reason'] = 'missing year or make'
        return base
    try:
        year = int(year)
    except Exception:
        base['reason'] = f'bad year: {year!r}'
        return base
    make_u = str(make).upper().strip()
    raw_model_n = _norm_for_key(raw_model)
    raw_trim_n = _norm_for_key(raw_trim)

    cached = _cache_get(db_conn, year, make_u, raw_model_n, raw_trim_n)
    if cached is not None:
        if cached.get('ymmt_id'):
            row = _fetch_row(db_conn, cached['ymmt_id'])
            if row:
                cached.update(row)
        return {**base, **cached}

    candidates = _load_candidates(db_conn, year, make_u)
    if not candidates:
        base['reason'] = f'no catalog rows for {make}'
        _cache_put(db_conn, year, make_u, raw_model_n, raw_trim_n, base)
        return base

    raw_combo_tokens = _norm_tokens(f"{raw_model or ''} {raw_trim or ''}")
    raw_trim_tokens = _norm_tokens(raw_trim)
    scored = []
    for c in candidates:
        cand_combo = _norm_tokens(f"{c['model']} {c['trim']}")
        cand_trim_n = _norm_tokens(c['trim'])
        s = _score(raw_combo_tokens, cand_combo, raw_trim_tokens, cand_trim_n)
        if c['year'] == year:
            s += 0.02
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    best_score, best = scored[0]
    # Tie threshold is 0.01 (tighter than the same-year +0.02 bonus) so that
    # the same (model, trim) at adjacent years doesn't register as a tie.
    tied = len(scored) >= 2 and abs(scored[0][0] - scored[1][0]) < 0.01

    if best_score >= FUZZY_ACCEPT and not tied:
        result = {'ymmt_id': best['id'], 'confidence': round(best_score, 3),
                  'source': 'fuzzy',
                  'reason': f"fuzzy match score={best_score:.2f}",
                  'year': best['year'], 'make': best['make'],
                  'model': best['model'], 'trim': best['trim'],
                  'body_style': best['body_style'],
                  'market_segment': best['market_segment']}
        _cache_put(db_conn, year, make_u, raw_model_n, raw_trim_n, result)
        return {**base, **result}

    top_k = [c for (_, c) in scored[:TOP_K_FOR_LLM]]
    llm = _ask_claude(year, make, raw_model, raw_trim, top_k)
    if llm.get('ymmt_id'):
        row = _fetch_row(db_conn, llm['ymmt_id']) or {}
        llm.update(row)
    elif llm['source'] == 'disabled' and best_score >= 0.5:
        llm = {'ymmt_id': best['id'], 'confidence': round(best_score, 3),
               'source': 'fuzzy_degraded',
               'reason': f"anthropic disabled; fallback fuzzy {best_score:.2f}",
               'year': best['year'], 'make': best['make'],
               'model': best['model'], 'trim': best['trim'],
               'body_style': best['body_style'],
               'market_segment': best['market_segment']}

    if llm['source'] not in ('claude_error',):
        _cache_put(db_conn, year, make_u, raw_model_n, raw_trim_n, llm)
    return {**base, **llm}
