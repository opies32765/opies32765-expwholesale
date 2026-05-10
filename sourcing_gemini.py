"""
EW sourcing-bot — extraction-only LLM module (Option 2).

Haiku now does ONE thing: parse the user's SMS into structured JSON
{intent, spec_update, spec_clear}. Python (sourcing_bot.py) handles all
state transitions and reply composition via deterministic templates.

This eliminates the prompt-tuning treadmill: Haiku can't write awkward
phrasings or violate phrasing rules because Haiku doesn't write
the user-facing replies anymore. Speed: 1 Haiku call per turn (~1.5s)
vs the old 1-2 calls (1.5-3s).
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
    global _anthropic_client
    if _anthropic_client is None:
        try:
            import anthropic
            api_key = os.environ.get('ANTHROPIC_API_KEY')
            if not api_key:
                _anthropic_client = False
            else:
                _anthropic_client = anthropic.Anthropic(api_key=api_key)
        except Exception as e:
            print(f'[sourcing-llm] init failed: {e}', flush=True)
            _anthropic_client = False
    return _anthropic_client if _anthropic_client else None


SYSTEM_PROMPT = """You are an SMS-message structured-data extractor for a wholesale auto sourcing system. You ONLY output JSON. You do NOT write replies to the user — Python composes those.

Output a single JSON object, no markdown, no prose:

{
  "intent": "sourcing|interested|not_interested|more_results|drop_it|stop|extend_wishlist|name_provided|unclear",
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
  "spec_clear": ["field_name", ...]
}

INTENT GUIDE
- "sourcing": user is specifying or refining a vehicle (most common; default for any new search criteria)
- "interested": user said yes / "send it" / "the green one" / picked a specific match
- "not_interested": user said no / "none of those" / "next" after seeing matches
- "more_results": user asked to see more after "want to see the rest?" ("yes", "ok", "more", "show more", "next")
- "extend_wishlist": user said yes to a "want me to flag it?" offer
- "drop_it": user wants to cancel current search ("drop it", "nevermind", "cancel")
- "stop": full opt-out ("stop", "unsubscribe", "quit")
- "name_provided": user said only their name in response to a name ask ("Mike", "this is Mike Stark", "Oscar")
- "unclear": can't tell — be honest, don't guess

CONTEXT-SENSITIVE INTENT
The user's prior message (CONVERSATION_HISTORY) and BOT_LAST_QUESTION matter. If the bot asked "want me to flag it?" and user says "yes", that's "extend_wishlist". If bot asked "any of these work?" and user says "yes", that's "more_results" (probably means show more). If bot asked "what's your name?" and user says "Mike", that's "name_provided".

SPEC EXTRACTION
- "model" is the model name only (e.g. "911", "Carrera", "MC20", "296", "Bentayga"), NOT trim
- "trim" is whatever comes after model (e.g. "Carrera GTS", "Turbo S", "GT3 Touring")
- Lowercase makes ("porsche", not "Porsche")
- "MC" alone is incomplete — leave model=null, will trigger model-suggestion in Python
- Year range: "21-23 fine" → year_min=2021, year_max=2023; "2025" → both 2025; "any year" → spec_clear: ["year_min","year_max"]
- Colors: list form, lowercase ("red" → ext_color: ["red"])

PIVOT
When user changes vehicle entirely (was discussing 296, now says "how about a 458"), set new make/model/trim in spec_update AND list any prior soft filters in spec_clear: ["year_min","year_max","ext_color","int_color","trim","miles_max"].

BROADEN
When user explicitly relaxes a filter ("any color", "any year", "open on trim"), put the relevant field name(s) in spec_clear.

NEVER put "customer_name" in spec_clear — it's identity, not request-specific.

If both spec_update and spec_clear touch the same field, that's fine — Python will apply spec_clear first then spec_update on top.

NO REPLY
Do not write a reply. Do not include "reply" or "ready_to_search" or "next_state" fields. Output ONLY the three fields above. Python decides everything else."""


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


def _last_bot_question(conversation):
    """Find the most recent bot message that ended with a question mark."""
    for t in reversed(conversation or []):
        if t.get('role') == 'bot':
            text = (t.get('text') or '').strip()
            if text.endswith('?'):
                return text
            return text  # last bot turn even if not a question
    return None


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
    if row.get('customer_name'):
        parts.append(f"customer_name={row['customer_name']}")
    return ', '.join(parts) if parts else 'EMPTY'


def _build_user_prompt(row, new_user_msg):
    last_q = _last_bot_question(row.get('conversation') or [])
    blocks = [
        f"REQUEST_STATUS: {row.get('status', 'gathering')}",
        f"CURRENT_SPEC: {_format_current_spec(row)}",
        f"BOT_LAST_QUESTION: {last_q!r}" if last_q else "BOT_LAST_QUESTION: (none)",
        f"CONVERSATION_HISTORY:\n{_format_history(row.get('conversation') or [])}",
        f"NEW_USER_MSG: {new_user_msg}",
    ]
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


def _haiku_call(user_prompt, max_tokens=300, temperature=0.1):
    cli = _client()
    if not cli:
        return None
    try:
        import time as _time
        _t0 = _time.monotonic()
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
        _dt = (_time.monotonic() - _t0) * 1000
        try:
            u = resp.usage
            print(f'[sourcing-llm] {_dt:.0f}ms in={u.input_tokens} out={u.output_tokens} '
                  f'cache_read={getattr(u, "cache_read_input_tokens", 0)} '
                  f'cache_create={getattr(u, "cache_creation_input_tokens", 0)}', flush=True)
        except Exception:
            print(f'[sourcing-llm] {_dt:.0f}ms', flush=True)
        if not resp.content:
            return None
        text_parts = [b.text for b in resp.content if getattr(b, 'type', None) == 'text']
        return '\n'.join(text_parts).strip() if text_parts else None
    except Exception as e:
        print(f'[sourcing-llm] Haiku call failed: {e}', flush=True)
        return None


def extract(row, new_user_msg):
    """Single Haiku turn: extract structured data only.
    Returns dict {intent, spec_update, spec_clear} or None on failure."""
    user_block = _build_user_prompt(row, new_user_msg)
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
    parsed.setdefault('spec_clear', [])
    return parsed


# ── Spec merge helper (unchanged) ─────────────────────────────────────────
_SCALAR_FIELDS = ('year_min', 'year_max', 'make', 'model', 'trim',
                  'miles_max', 'must_clean_title', 'price_hint',
                  'customer_name')
_LIST_FIELDS = ('ext_color', 'int_color', 'options')


def merge_spec(row, spec_update, spec_clear=None):
    """Merge spec_clear (null specified fields) then spec_update (overwrite
    with non-null values). spec_update wins when both touch the same field.
    customer_name is identity — never cleared."""
    changes = {}
    spec_clear = set(spec_clear or [])
    spec_clear.discard('customer_name')
    for k in _SCALAR_FIELDS:
        if k in spec_clear:
            row[k] = None
            changes[k] = None
        v = spec_update.get(k)
        if v is not None and v != '':
            row[k] = v
            changes[k] = v
    for k in _LIST_FIELDS:
        if k in spec_clear:
            row[k] = None
            changes[k] = None
        v = spec_update.get(k)
        if v is not None and isinstance(v, list) and len(v) > 0:
            v_norm = [str(x).strip().lower() for x in v if x is not None and str(x).strip()]
            if v_norm:
                row[k] = v_norm
                changes[k] = v_norm
    return changes


# ── Mileage formatter (used by Python templates in sourcing_bot.py) ───────
def _fmt_miles(m):
    """'23 mi' / '9k mi' / '?'"""
    if m is None:
        return '?'
    try:
        n = int(m)
    except Exception:
        return '?'
    if n < 1000:
        return f'{n} mi'
    return f'{round(n/1000)}k mi'


# ── Time-aware handoff line ──────────────────────────────────────────────
def handoff_line(name, now_et=None):
    """Pick the right closing message based on Mon-Fri 9-5 ET rules."""
    if now_et is None:
        now_et = datetime.now(_TZ)
    first = (name or '').strip().split()[0] if name else None
    h = now_et.hour
    weekday = now_et.weekday()  # 0=Mon, 6=Sun

    # Mon-Fri 9am-5pm
    if weekday < 5 and 9 <= h < 17:
        line = "give us a bit, we'll come back with details"
    # Friday after 5pm OR Saturday all day OR Sunday before 5pm
    elif (weekday == 4 and h >= 17) or weekday == 5 or (weekday == 6 and h < 17):
        line = "we'll get back to you one way or the other on monday"
    # Sun-Thu after 5pm
    elif h >= 17 and weekday in (6, 0, 1, 2, 3):
        line = "back to you tomorrow with details"
    # Pre-9am weekday
    else:
        line = "back to you in a bit"

    if first:
        return f"thanks {first} — {line}."
    return f"got it — {line}."
