"""Claude Sonnet 4.6 trim equivalence judge.

Used as a Tier-1.5 filter inside dealer_match.find_dealer_matches: when
canonical Y/M/M matches a dealer-inventory row but the trim string differs
from the bid's canon_trim, ask Sonnet whether the two strings refer to the
same factory trim variant. This catches cases the hand-coded body-strip
regex can't (e.g. "Scat Pack" vs "R/T Scat Pack" → YES; "Scat Pack" vs
"SRT Hellcat Widebody Jailbreak" → NO).

Mirrors claude_vin_decoder.py: lazy thread-safe client, JSON-strict prompt,
forever cache keyed by (make, model, sorted-pair-of-trims).

Public API:
    is_same_trim(make, model, year, subj_trim, cand_trim, db_conn=None)
      → {'match': bool, 'confidence': float, 'reason': str, 'source': str}
        source ∈ {'cache', 'claude_sonnet_4_6', 'claude_error', 'disabled'}

If ANTHROPIC_API_KEY is unset or the Anthropic SDK import fails, every
call returns match=True conf=0.0 source='disabled' — i.e. the filter
becomes a no-op so dealer_match never starves the card on infra failure.
"""
from __future__ import annotations
import os
import re
import json
import threading

ANTHROPIC_MODEL = 'claude-sonnet-4-6'
CLAUDE_TIMEOUT_SEC = 10
MAX_TOKENS = 200

_client = None
_client_lock = threading.Lock()
_JSON_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)

_SYSTEM = ("You output ONLY a single JSON object. No preamble, no explanation, "
           "no markdown, no commentary. The very first character of your response "
           "is '{' and the very last character is '}'.")

_USER_PROMPT = """For a {year} {make} {model}, do these two trim descriptors refer to the SAME factory trim variant?

SUBJECT trim: "{subj}"
CANDIDATE trim: "{cand}"

Rules:
- "Same variant" means same engine + factory option package (same MSRP tier, same powertrain).
- Body words (Coupe, Sedan, Wagon, Cabriolet, Hatchback, SUV, etc.) are NOT trim — ignore.
- Drivetrain (AWD, RWD, FWD, 4MATIC, xDrive, Quattro, 4WD) is NOT trim — ignore.
- Door-count (4dr, 2dr, 4 door, etc.) is NOT trim — ignore.
- Hyphens, slashes, spacing, capitalization, common abbreviations are insignificant ("R/T"="RT", "WB"="Widebody").
- Trim hierarchy DOES matter: "M3" != "M3 Competition" != "M3 CS"; "Scat Pack" != "SRT Hellcat"; "Trail Boss" != "LT".
- A higher trim DOES match a lower trim if the lower name is fully contained AND no powertrain word is added ("R/T Scat Pack" == "Scat Pack" because Scat Pack is an R/T variant; but "M3 Competition" != "M3" because Competition changes output).
- If either trim is empty/unknown, return match=false unless BOTH are empty.
- When uncertain, return match=false with the lower confidence and a one-line reason.

Worked examples:
  Dodge Charger: "Scat Pack" vs "R/T Scat Pack" → {{"match":true,"confidence":0.95,"reason":"Scat Pack is the R/T 6.4L variant; same factory trim."}}
  Dodge Charger: "Scat Pack" vs "SRT Hellcat Widebody Jailbreak" → {{"match":false,"confidence":0.98,"reason":"6.4L NA vs supercharged 6.2L — different powertrain and trim."}}
  BMW M3: "Competition" vs "" → {{"match":false,"confidence":0.85,"reason":"Competition is a distinct higher-output variant."}}
  Ford F-150: "Lariat" vs "XLT" → {{"match":false,"confidence":0.98,"reason":"Different trim tiers — XLT is below Lariat."}}
  Porsche 911: "Carrera S" vs "Carrera S Coupe" → {{"match":true,"confidence":0.97,"reason":"Coupe is body — same trim."}}

Return ONLY this JSON: {{"match":<true|false>,"confidence":<float 0-1>,"reason":"<one short sentence>"}}
"""


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                try:
                    from anthropic import Anthropic
                    _client = Anthropic()
                except Exception as e:
                    print(f'[claude_trim] init failed: {e}', flush=True)
                    _client = False
    return _client if _client else None


def _norm_for_key(s):
    """Whitespace + case normalize for cache key. Does NOT strip body words
    or trim hierarchy — that's the AI's job. Just collapses cosmetic noise."""
    if not s:
        return ''
    return ' '.join(str(s).lower().strip().split())


def _pair_key(make, model, a, b):
    """Sorted pair so (X,Y) and (Y,X) hit the same cache row."""
    a_norm, b_norm = _norm_for_key(a), _norm_for_key(b)
    if a_norm > b_norm:
        a_norm, b_norm = b_norm, a_norm
    return (str(make or '').upper().strip(), str(model or '').upper().strip(),
            a_norm, b_norm)


def _parse_json(txt):
    if not txt:
        return None
    txt = txt.strip()
    if '```' in txt:
        txt = re.sub(r'```(?:json)?\s*|\s*```', '', txt)
    m = _JSON_RE.search(txt)
    if m:
        txt = m.group(0)
    try:
        return json.loads(txt)
    except Exception:
        return None


def _cache_get(db_conn, make_u, model_u, a, b):
    if db_conn is None:
        return None
    try:
        with db_conn.cursor() as cur:
            cur.execute("""SELECT is_match, confidence, reason, source
                             FROM trim_match_cache
                            WHERE make=%s AND model=%s AND trim_a=%s AND trim_b=%s
                            LIMIT 1""", (make_u, model_u, a, b))
            row = cur.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                return {'match': bool(row['is_match']),
                        'confidence': float(row['confidence'] or 0),
                        'reason': row['reason'] or '',
                        'source': 'cache'}
            return {'match': bool(row[0]),
                    'confidence': float(row[1] or 0),
                    'reason': row[2] or '',
                    'source': 'cache'}
    except Exception as e:
        print(f'[claude_trim] cache read err: {e!r}', flush=True)
        try: db_conn.rollback()
        except Exception: pass
        return None


def _cache_put(db_conn, make_u, model_u, a, b, decision):
    if db_conn is None:
        return
    try:
        with db_conn.cursor() as cur:
            cur.execute("""INSERT INTO trim_match_cache
                              (make, model, trim_a, trim_b, is_match, confidence, reason, source)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                          ON CONFLICT (make, model, trim_a, trim_b) DO UPDATE
                            SET is_match=EXCLUDED.is_match,
                                confidence=EXCLUDED.confidence,
                                reason=EXCLUDED.reason,
                                source=EXCLUDED.source,
                                decided_at=NOW()""",
                        (make_u, model_u, a, b,
                         bool(decision['match']),
                         float(decision.get('confidence') or 0),
                         (decision.get('reason') or '')[:500],
                         decision.get('source') or 'claude_sonnet_4_6'))
            db_conn.commit()
    except Exception as e:
        print(f'[claude_trim] cache write err: {e!r}', flush=True)
        try: db_conn.rollback()
        except Exception: pass


def _ask_claude(make, model, year, subj_trim, cand_trim):
    client = _get_client()
    if client is None:
        return {'match': True, 'confidence': 0.0,
                'reason': 'anthropic disabled — filter is no-op',
                'source': 'disabled'}
    prompt = _USER_PROMPT.format(
        year=year or '', make=make or '', model=model or '',
        subj=subj_trim or '', cand=cand_trim or '')
    try:
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM,
            messages=[{'role': 'user', 'content': prompt}],
            timeout=CLAUDE_TIMEOUT_SEC,
        )
        txt = ''.join(b.text for b in resp.content if getattr(b, 'type', None) == 'text')
        parsed = _parse_json(txt)
        if not parsed or 'match' not in parsed:
            print(f'[claude_trim] parse failed: raw={txt[:200]!r}', flush=True)
            return {'match': True, 'confidence': 0.0,
                    'reason': 'parse_failed',
                    'source': 'claude_error'}
        return {'match': bool(parsed.get('match')),
                'confidence': float(parsed.get('confidence') or 0.0),
                'reason': str(parsed.get('reason') or '')[:500],
                'source': 'claude_sonnet_4_6'}
    except Exception as e:
        print(f'[claude_trim] api err: {e!r}', flush=True)
        return {'match': True, 'confidence': 0.0,
                'reason': f'api_error: {e}',
                'source': 'claude_error'}


def is_same_trim(make, model, year, subj_trim, cand_trim, db_conn=None):
    """Decide whether two trim strings refer to the same factory variant.

    Returns dict {match, confidence, reason, source}. Falls open
    (match=True conf=0) on any infra failure so the filter never starves
    legitimate matches.
    """
    # Fast paths — both empty = match; only one empty = no match (but let cache
    # observe so we don't waste API calls).
    subj_n = _norm_for_key(subj_trim)
    cand_n = _norm_for_key(cand_trim)
    if subj_n == cand_n:
        return {'match': True, 'confidence': 1.0,
                'reason': 'identical after normalization',
                'source': 'trivial'}
    if not subj_n and not cand_n:
        return {'match': True, 'confidence': 1.0,
                'reason': 'both empty',
                'source': 'trivial'}

    make_u, model_u, a, b = _pair_key(make, model, subj_trim, cand_trim)
    cached = _cache_get(db_conn, make_u, model_u, a, b)
    if cached:
        return cached

    decision = _ask_claude(make, model, year, subj_trim, cand_trim)
    if decision['source'] not in ('disabled', 'claude_error'):
        _cache_put(db_conn, make_u, model_u, a, b, decision)
    return decision
