"""
EW sourcing-bot — LLM turn module.

(File still named sourcing_gemini.py for compatibility but now uses
Anthropic Haiku 4.5 to isolate SMS workload from the Vertex AI quota
shared with bid-assessment / OCR / vision.)

One call per inbound SMS. Returns:
  spec_update / intent / reply / ready_to_search / next_state.
"""
import json
import os
import re
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo('America/New_York')
except ImportError:
    import pytz
    _TZ = pytz.timezone('America/New_York')


_HISTORY_TURNS = 10
_MODEL = 'claude-haiku-4-5-20251001'

_anthropic_client = None


def _client():
    """Lazy-init the Anthropic client."""
    global _anthropic_client
    if _anthropic_client is None:
        try:
            import anthropic
            api_key = os.environ.get('ANTHROPIC_API_KEY')
            if not api_key:
                print('[sourcing-llm] ANTHROPIC_API_KEY not set', flush=True)
                _anthropic_client = False
            else:
                _anthropic_client = anthropic.Anthropic(api_key=api_key)
        except Exception as e:
            print(f'[sourcing-llm] init failed: {e}', flush=True)
            _anthropic_client = False
    return _anthropic_client if _anthropic_client else None


SYSTEM_PROMPT = """You are an SMS assistant for Experience Wholesale, a wholesale auto broker. You text dealers and wholesalers in a peer register: brief, lowercase-friendly, professional, no emoji, no formal greetings, no AI disclosures.

YOUR JOB
Help the user source a vehicle. Gather just enough spec to search, present matches without revealing price or source, and either close the loop with a handoff or save a 30-day wishlist.

ABSOLUTE RULES
- NEVER mention or quote a price in any reply.
- NEVER name the source dealer, city, state, or any URL.
- NEVER say you are an AI or assistant.
- If the user states a price/budget, ACK they said it but DO NOT filter by it. We use it as a soft sort hint only.
- Outbound match descriptions only include: year, model+trim, ext_color / int_color, mileage, up to 3 key options.
- Keep replies under ~160 chars when possible. Don't ramble.

EXTRACTION RULES
- "model" should be the model name only (e.g. "911", "Carrera", "Bentayga"), NOT including the trim. If the user says "porsche 911 carrera gts", extract model="911" and trim="carrera gts".
- "trim" can be everything after the model (e.g. "carrera gts", "turbo s", "denali").
- Make it lowercase-friendly when extracting (e.g. make="porsche" not "Porsche").

HARD SEARCH MINIMUMS
year (range OK), make, model. Ask only for what's missing. Do NOT ask budget. Do NOT push for color/miles/options before the first search — most users want to see what's available first.

PRESENTING MATCHES
Format (≤3 per message):
{year} {model} {trim}, {ext_color} / {int_color}, {miles/1000}k mi
If >3 total, append: "got {n} total — want to see the rest?"

CLOSING WHEN USER IS INTERESTED IN A SPECIFIC MATCH
Use the CURRENT_TIME provided below. Business hours: Mon-Fri 09:00-17:00 ET.
- Mon-Fri 09:00-17:00 → "give us a bit — we'll come back with details"
- After 17:00 Sun-Thu (and the user is interested) → "we'll come back to you tomorrow with details"
- After 17:00 Friday, all day Sat, all day Sun before 17:00 → "we'll get back to you one way or the other on monday"

NO MATCHES (search_results.count == 0)
"nothing matching in our scans right now. want me to flag it and text you the second one shows up?"
On user yes: "done. saved: {short_desc}. text 'drop it' anytime."

NAME CAPTURE
After the user expresses interest in a specific match (intent=interested) OR agrees to flag it for the wishlist (intent=extend_wishlist), if customer_name is not yet known, append " — what's your name so we know who to call back?" or similar to your reply. ONE sentence. Don't ask for name during the spec-gathering phase.

When the user provides their name (typically a single short message like "mike" or "this is mike stark"), extract it into spec_update.customer_name. Reply briefly: "thanks {first_name}, talk soon." or similar. Don't repeat the handoff line.

OUTPUT
Return ONLY a JSON object, no markdown, no prose. Schema:
{
  "spec_update": {
    "year_min": int|null, "year_max": int|null,
    "make": str|null, "model": str|null, "trim": str|null,
    "ext_color": [str]|null, "int_color": [str]|null,
    "miles_max": int|null,
    "options": [str]|null,
    "must_clean_title": bool|null,
    "price_hint": int|null,
    "customer_name": str|null
  },
  "intent": "sourcing|interested|not_interested|more_results|drop_it|extend_wishlist|unclear",
  "reply": "the SMS text to send back, or null",
  "ready_to_search": true|false,
  "next_state": "gathering|searching|presented|matched|wishlist|archived"
}

ready_to_search rules:
- Set true ONLY when (after applying spec_update) make AND model AND (year_min OR year_max) are all known AND status is 'gathering'.
- Set false otherwise.

Set spec_update fields to null when the user did NOT provide that field this turn. Only fill in NEW info."""


def _now_ny():
    return datetime.now(_TZ)


def _format_current_time():
    n = _now_ny()
    return f"{n.strftime('%A %Y-%m-%d %H:%M')} ET"


def _format_current_spec(row):
    parts = []
    if row.get('year_min') or row.get('year_max'):
        if row.get('year_min') == row.get('year_max'):
            parts.append(f"year={row['year_min']}")
        else:
            parts.append(f"year={row.get('year_min','?')}-{row.get('year_max','?')}")
    for k in ('make', 'model', 'trim'):
        if row.get(k):
            parts.append(f"{k}={row[k]}")
    if row.get('ext_color'):
        parts.append(f"ext_color={row['ext_color']}")
    if row.get('int_color'):
        parts.append(f"int_color={row['int_color']}")
    if row.get('miles_max'):
        parts.append(f"miles_max={row['miles_max']}")
    if row.get('options'):
        parts.append(f"options={row['options']}")
    if row.get('price_hint'):
        parts.append(f"price_hint={row['price_hint']}")
    if row.get('customer_name'):
        parts.append(f"customer_name={row['customer_name']}")
    return ', '.join(parts) if parts else 'EMPTY'


def _format_history(conversation):
    if not conversation:
        return '(no prior turns)'
    last = conversation[-_HISTORY_TURNS:]
    lines = []
    for turn in last:
        role = turn.get('role', '?')
        text = turn.get('text', '')
        lines.append(f"{role}: {text}")
    return '\n'.join(lines)


def _build_user_prompt(row, new_user_msg, search_results=None):
    blocks = [
        f"CURRENT_TIME: {_format_current_time()}",
        f"REQUEST_STATUS: {row.get('status', 'gathering')}",
        f"CURRENT_SPEC: {_format_current_spec(row)}",
        f"CONVERSATION_HISTORY:\n{_format_history(row.get('conversation') or [])}",
    ]
    if search_results is not None:
        blocks.append(f"SEARCH_RESULTS_AVAILABLE: count={len(search_results)}")
        if search_results:
            descs = []
            for m in search_results[:3]:
                d = (f"{m.get('year','')} {m.get('model','')} "
                     f"{m.get('trim') or ''}, "
                     f"{m.get('ext_color') or '?'} / {m.get('int_color') or '?'}, "
                     f"{int((m.get('mileage') or 0)/1000)}k mi").strip()
                descs.append(d)
            blocks.append("TOP_MATCHES:\n" + '\n'.join(descs))
    blocks.append(f"NEW_USER_MSG: {new_user_msg or '(internal: search complete, present results)'}")
    return '\n\n'.join(blocks)


def _parse_response(raw):
    if not raw:
        return None
    s = raw.strip()
    if s.startswith('```'):
        s = re.sub(r'^```(?:json)?\s*', '', s)
        s = re.sub(r'\s*```\s*$', '', s)
    start = s.find('{')
    end = s.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        print(f'[sourcing-llm] JSON parse failed: {e} raw={s[:200]!r}', flush=True)
        return None


def _empty_spec_update():
    return {
        'year_min': None, 'year_max': None,
        'make': None, 'model': None, 'trim': None,
        'ext_color': None, 'int_color': None,
        'miles_max': None, 'options': None,
        'must_clean_title': None, 'price_hint': None,
        'customer_name': None,
    }


def _haiku_call(user_prompt, max_tokens=700, temperature=0.3):
    cli = _client()
    if not cli:
        return None
    try:
        # Prompt caching on the system block — system prompt is ~700 tokens
        # and identical across turns, so cache reads at 10% rate save real $$.
        # 5-minute TTL is fine for SMS conversation cadence.
        resp = cli.messages.create(
            model=_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            system=[{
                'type': 'text',
                'text': SYSTEM_PROMPT,
                'cache_control': {'type': 'ephemeral'},
            }],
            messages=[{'role': 'user', 'content': user_prompt}],
        )
        if not resp.content:
            return None
        # content is a list of blocks; collect text from text blocks.
        text_parts = [b.text for b in resp.content if getattr(b, 'type', None) == 'text']
        return '\n'.join(text_parts).strip() if text_parts else None
    except Exception as e:
        print(f'[sourcing-llm] Haiku call failed: {e}', flush=True)
        return None


def turn(row, new_user_msg, search_results=None):
    """Run one Haiku turn. Returns dict or None on hard failure."""
    user_block = _build_user_prompt(row, new_user_msg, search_results)
    raw = _haiku_call(user_block)
    parsed = _parse_response(raw)
    if not parsed:
        return None

    su = parsed.get('spec_update') or {}
    norm_su = _empty_spec_update()
    for k in norm_su:
        if k in su:
            norm_su[k] = su[k]
    parsed['spec_update'] = norm_su

    parsed.setdefault('intent', 'unclear')
    parsed.setdefault('ready_to_search', False)
    parsed.setdefault('next_state', row.get('status', 'gathering'))
    if 'reply' not in parsed:
        parsed['reply'] = None
    return parsed


# ── Spec merge helper (unchanged from previous) ──────────────────────────
_SCALAR_FIELDS = ('year_min', 'year_max', 'make', 'model', 'trim',
                  'miles_max', 'must_clean_title', 'price_hint',
                  'customer_name')
_LIST_FIELDS = ('ext_color', 'int_color', 'options')


def merge_spec(row, spec_update):
    changes = {}
    for k in _SCALAR_FIELDS:
        v = spec_update.get(k)
        if v is not None and v != '':
            row[k] = v
            changes[k] = v
    for k in _LIST_FIELDS:
        v = spec_update.get(k)
        if v is not None and isinstance(v, list) and len(v) > 0:
            v_norm = [str(x).strip().lower() for x in v if x is not None and str(x).strip()]
            if v_norm:
                row[k] = v_norm
                changes[k] = v_norm
    return changes
