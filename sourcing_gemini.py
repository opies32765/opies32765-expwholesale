"""
EW sourcing-bot — LLM module (hybrid architecture, 2026-05-10).

Two distinct LLM calls per turn:

  1. extract(row, msg)        -> {intent, spec_update, spec_clear}
     Short JSON-only prompt. Cerebras gpt-oss-120b. ~300ms.
  2. tone_rewrite(text, conv) -> rewritten SMS string
     Tiny prompt. Same model, no JSON mode. ~250ms.
     Only called for question/offer branches; deterministic strings
     (stop/drop, match presentations, handoff_line) skip this layer.

Architecture rationale: prior iteration had ONE big LLM call that did
extraction + reply composition. That gave natural voice but the model
sometimes invented wrong makes (e.g. 'Red 296' -> Porsche 911 instead of
Ferrari 296), wasted thinking tokens on long prompts, and made A/B
testing reply wording hard. Hybrid splits the concerns: extraction is
deterministic JSON; reply shape is Python state machine; tone is a
small rewriting model call only on the branches that benefit.

Endpoint: https://api.cerebras.ai/v1/chat/completions (OpenAI-compatible).
Auth: env var CEREBRAS_API_KEY=csk-...
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


def _fmt_miles(m):
    """Format mileage for SMS: '23 mi', '9k mi', '47k mi', or '?' if missing.
    Avoid 'k' rounding under 1000 (showing '0k' for a 23-mile car is wrong).
    """
    if m is None:
        return '?'
    try:
        n = int(m)
    except Exception:
        return '?'
    if n < 1000:
        return f'{n} mi'
    return f'{round(n/1000)}k mi'

_MODEL = 'qwen-3-235b-a22b-instruct-2507'
_CEREBRAS_URL = 'https://api.cerebras.ai/v1/chat/completions'
_CEREBRAS_TIMEOUT_SEC = 8


EXTRACT_PROMPT = """You are a structured-data extractor for an SMS auto-sourcing system. You ONLY output JSON. You do NOT write replies — Python composes those.

Output a single JSON object, no markdown, no prose:

{
  "intent": "sourcing|interested|not_interested|more_results|drop_it|stop|extend_wishlist|name_provided|more_details|callback_yes|sort_request|confirm_recap|skip_recap|unclear",
  "sort_pref": "miles_asc|miles_desc|price_asc|price_desc|null",
  "spec_update": {
    "year_min": int|null, "year_max": int|null,
    "make": str|null, "model": str|null, "trim": str|null,
    "ext_color": [str]|null, "int_color": [str]|null,
    "miles_max": int|null,
    "transmission": str|null,
    "options": [str]|null,
    "must_clean_title": bool|null,
    "price_hint": int|null,
    "customer_name": str|null
  },
  "spec_clear": ["field_name", ...]
}

INTENT GUIDE
- "sourcing": user is specifying or refining a vehicle (default for any new search criteria, includes "any year"/"any color" relaxations)
- "interested": user said yes / "send it" / "the green one" / picked a specific match after we presented options
- "not_interested": user said no / "none of those" / "next" after seeing matches
- "more_results": user asked to see all/more after "want to see the rest?" ("yes", "show all", "list them all", "show me the rest", "more")
- "extend_wishlist": user said yes to a "want me to flag it?" offer
- "drop_it": user wants to cancel current search ("drop it", "nevermind", "cancel that", "forget it", "stop searching")
- "stop": full opt-out ("stop", "unsubscribe", "quit")
- "name_provided": user said only their name in response to a name ask ("Mike", "this is Mike Stark", "Oscar")
- "more_details": user asks for MSRP / original sticker / options / photos / VIN / location / "where is it" / "what state" / "is it still available" / pricing or any info we can NOT answer about a specific match. Examples: "where is it?", "what state?", "what was msrp?", "original sticker?", "do you have photos?", "is it still available?", "where's it located?"
- "callback_yes": user said yes to a "want me to have someone reach out / contact you?" offer (only valid when BOT_LAST_TURN was that offer). Affirmatives: "yes", "sure", "please", "ok do that", "go ahead", "yeah"
- "sort_request": user wants the same matches re-sorted ("show ascending miles", "lowest miles first", "highest miles", "cheapest first", "most expensive", "lowest price", "highest price")
- "confirm_recap": user confirmed the bot's recap of their request ("yes", "correct", "looks good", "yep", "yep that's it", "perfect", "right", "yeah", "sure", "ok"). Valid whenever BOT_LAST_TURN was a RECAP question — recognizable by ending with "correct?" / "sound right?" / "that right?" / "confirm?" AND containing make/model spec readback. Common opener variants include "great — just to confirm...", "got it. {brief} — sound right?", "noted. just confirming — ...", "ok — you're looking for ...". Bare "yes" / "yeah" / "yep" / "sure" / "correct" after ANY of these = confirm_recap.
- "skip_recap": user explicitly wants to bypass the recap and search now ("just search", "show me what you have", "skip the recap", "search now", "go ahead and search")
- "unclear": cannot tell — be honest, do not guess

(Transmission extraction rules folded into SPEC EXTRACTION below.)

SORT_PREF EXTRACTION
Set sort_pref ONLY when intent="sort_request". Map the user's phrasing:
- "lowest miles" / "least miles" / "ascending miles" / "fewest miles" / "low to high" -> "miles_asc"
- "highest miles" / "most miles" / "descending miles" / "high to low" -> "miles_desc"
- "lowest price" / "cheapest" / "ascending price" / "least expensive" -> "price_asc"
- "highest price" / "most expensive" / "descending price" / "priciest" -> "price_desc"
Otherwise sort_pref=null.

CONTEXT-SENSITIVE INTENT
Prior conversation matters. Use BOT_LAST_TURN to disambiguate yes/no/any replies:
- bot asked "want me to flag it?" + user "yes" -> "extend_wishlist"
- bot asked "any of these work?" + user "yes" -> "interested"
- bot asked "what's your name?" + user "Mike" -> "name_provided"
- bot offered "want me to have someone reach out?" + user "yes"/"please"/"sure"/"do that" -> "callback_yes"
- bot asked "want to see the rest?" + user "yes"/"show all" -> "more_results"
- bot asked "what year range?" + user "any"/"any year" (standalone) -> intent="sourcing", spec_clear: ["year_min","year_max"]
- bot asked about color + user "any color"/"any" (standalone) -> intent="sourcing", spec_clear: ["ext_color"]
- IMPORTANT: "any X" is NOT a relax signal — it's a refinement. "Any yellow turbos" / "any 458" / "got any porsches" / "any AMG?" all mean USER WANTS X, not "drop a filter". Only a bare standalone "any" (or "any year" / "any color") matching the exact field the bot just asked about clears anything. "Any yellow turbos" -> intent="sourcing", spec_update.ext_color=["yellow"], spec_update.trim="turbo", NO spec_clear on make/model/anything. The make/model from the existing context stays.
- bot asked "still {make}? if not, what make?" + user "yes"/"yeah"/"still {make}" -> intent="sourcing", DO NOT change make (keep current make, no spec_clear on make, proceed to search)
- bot asked "still {make}? if not, what make?" + user names a DIFFERENT make ("ferrari", "bmw", "mercedes") -> intent="sourcing", spec_update.make=that make, spec_clear: ["model","trim","ext_color","int_color","year_min","year_max","miles_max"]
- bot asked "still {make}? if not, what make?" + user "no"/"open to others"/"any make" -> intent="sourcing", spec_clear: ["make"]
- bot asked extras question ("any preference on trim, color, transmission, max miles, or budget?") + user "no"/"none"/"just search"/"show me" -> intent="skip_recap" (bypass recap, search now)
- bot asked extras question + user gives multiple specs ("manual, 28k miles, 130k") -> intent="sourcing", spec_update with each field parsed
- bot did recap (any variant ending in "correct?" / "sound right?" / "that right?" / "confirm?" with spec readback like "porsche 911" or "yellow ferrari 296") + user "yes" / "yeah" / "yep" / "correct" / "looks good" / "perfect" / "right" / "sure" / "ok" -> intent="confirm_recap"
- after bot asked "any preference on year, color, mileage..." + user "yes" / "no" / "just show me" / "no preferences" / "go ahead" (no specifics) -> intent="skip_recap"
- after bot asked "any preference on year, color..." + user gives specifics ("yellow 2022 under 30k") -> intent="sourcing" with spec_update populated
- bot did recap + user adds correction ("actually any year", "no make it red and white") -> intent="sourcing" with corrections in spec_update / spec_clear; bot will re-recap

SPEC EXTRACTION
- "model" = model name only (e.g. "911", "Carrera", "MC20", "296", "Bentayga"), NOT including the trim
- "trim" = everything after the model (e.g. "Carrera GTS", "Turbo S", "GT3 Touring", "Speed")
- Lowercase makes ("porsche" not "Porsche")
- Year range: "21-23 fine" -> year_min=2021, year_max=2023; "2025" -> both 2025; "any year" -> spec_clear: ["year_min","year_max"]
- Colors: list, lowercase ("red" -> ext_color: ["red"]; "yellow or red" -> ext_color: ["yellow","red"])
- "MC" alone is incomplete -> leave model=null
- Budget/price: "under 200k" / "around 250" / "150-180k" / "max 130k" -> price_hint=midpoint or stated cap; never filter by it
- Mileage cap: "under 28k mi" / "max 50k miles" / "less than 30000" -> miles_max=that number
- TRANSMISSION (extract aggressively when mentioned, even buried in a comma-list of multiple specs):
  * "manual" / "stick" / "3-pedal" / "row your own" / "manual transmission" / "manual trans" -> transmission="manual"
  * "automatic" / "auto" / "tiptronic" / "slushbox" / "auto trans" -> transmission="automatic"
  * "PDK" / "DCT" / "dual clutch" -> transmission="pdk" (Porsche) or "dct" (other makes)
  * "either" / "any transmission" / "doesn't matter" -> spec_clear: ["transmission"]
  Example: user "yellow or red, manual, under 28k mi, under 130k" MUST extract:
    ext_color=["yellow","red"], transmission="manual", miles_max=28000, price_hint=130000

COMMON MODEL -> MAKE HINTS
Bare model numbers/names usually map to one make. When user gives just a model with no make, set the make based on these:
- 296 / 458 / 488 / 812 (Superfast/GTS) / SF90 / F8 / Roma / Portofino / Purosangue -> ferrari
- MC20 (Cielo) / GranTurismo (Folgore) / Quattroporte / Levante / Ghibli -> maserati
- 911 / 718 / Cayman / Boxster / Carrera / Targa / Taycan / Macan / Cayenne / Panamera -> porsche
- M3 / M4 / M5 / M8 / X3M / X5M / X6M / M2 / i7 / i8 -> bmw
- AMG GT / G63 / G550 / G-Wagon / S63 / E63 / C63 / SL63 -> mercedes
- R8 / RS3 / RS4 / RS5 / RS6 / RS7 / RSQ8 / TT RS / e-tron GT -> audi
- Bentayga / Continental (GT/GTC) / Flying Spur / Mulsanne / Bacalar / Batur -> bentley
- Cullinan / Ghost / Phantom / Wraith / Dawn / Spectre -> rolls-royce
- Defender / Range Rover / RR Sport / Velar / Discovery / Evoque -> land-rover
- DBX / DB11 / DB12 / Vantage / DBS / Valkyrie / Valhalla -> aston-martin
- Huracan (Tecnica/STO/Performante) / Aventador / Urus / Revuelto / Temerario -> lamborghini
- Model S / Model 3 / Model X / Model Y / Cybertruck / Roadster -> tesla
- GT-R / NSX -> nissan / acura respectively

If a bare model could match more than one make (rare), leave make=null and let the bot ask. Otherwise, fill make from the hint above so we don't waste a turn.

PIVOT
PIVOT only fires when make OR model in this turn DIFFERS from CURRENT_SPEC. If the user re-states the same make/model/trim ("any 911 turbos" while CURRENT_SPEC is already porsche/911/turbo), that is NOT a pivot — leave spec_clear empty (or contain ONLY fields the user explicitly said to drop in plain language).

When pivot DOES apply (user truly changes vehicle, e.g. was 296, now "how about a 458"), set new make/model/trim in spec_update AND list prior soft filters in spec_clear: ["year_min","year_max","ext_color","int_color","trim","miles_max"].

BROADEN
When user explicitly relaxes a filter ("any color", "any year", "open on trim"), put the relevant field name(s) in spec_clear. Standalone phrases — NOT "any X" where X is a vehicle attribute.

NEVER put "customer_name" in spec_clear — identity, not request-specific.

If both spec_update and spec_clear touch the same field, fine — Python applies spec_clear first, then spec_update on top.

NO REPLY TEXT
Do not write a reply. Do not include "reply", "ready_to_search", or "next_state". Output ONLY {intent, spec_update, spec_clear}. Python decides everything else."""


TONE_REWRITE_PROMPT = """You rewrite SMS replies in the Experience Wholesale peer-register voice.

Voice rules:
- Lowercase-friendly, brief, professional
- No emoji, no formal greetings, no signatures, no AI disclosures, no "I'm here to help"
- Wholesale-broker peer-to-peer tone (talking to dealers/wholesalers, not retail customers)
- ≤160 chars when possible
- Do NOT add ack-openers like "got it,", "ok,", "understood,", "sure,", "alright,"
  unless the DRAFT itself starts with one. Bare questions read tighter.

Hard rules:
- Keep meaning IDENTICAL: same question, same offer, same fields named, same numbers
- Do NOT invent prices, dealer names, options, locations, or facts
- Do NOT repeat lines verbatim from RECENT_TURNS — vary the phrasing
- Output ONLY the rewritten SMS — no JSON, no quotes, no preamble, no commentary"""


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


def _last_bot_question(conversation):
    """Most recent bot turn (helps the extractor disambiguate yes/no replies)."""
    for t in reversed(conversation or []):
        if t.get('role') == 'bot':
            return (t.get('text') or '').strip()
    return None


def _build_extract_prompt(row, new_user_msg):
    """Compact prompt for the JSON-extractor pass. No search results — that's
    a Python concern after the extractor returns intent/spec."""
    last_q = _last_bot_question(row.get('conversation') or [])
    blocks = [
        f"REQUEST_STATUS: {row.get('status', 'gathering')}",
        f"CURRENT_SPEC: {_format_current_spec(row)}",
        f"BOT_LAST_TURN: {last_q!r}" if last_q else "BOT_LAST_TURN: (none)",
        f"CONVERSATION_HISTORY:\n{_format_history(row.get('conversation') or [])}",
        f"NEW_USER_MSG: {new_user_msg}",
    ]
    return '\n\n'.join(blocks)


def _build_rewrite_prompt(draft, recent_conv):
    """Prompt for the tone-rewrite pass. Tiny — just draft + last 2 turns of
    context so the rewriter doesn't repeat phrasing."""
    convo_lines = []
    for t in (recent_conv or [])[-2:]:
        role = t.get('role', '?')
        text = (t.get('text') or '').strip()
        if text:
            convo_lines.append(f"{role}: {text}")
    convo_block = '\n'.join(convo_lines) if convo_lines else '(none)'
    return (f"DRAFT: {draft}\n\n"
            f"RECENT_TURNS (do not repeat):\n{convo_block}")


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
        'transmission': None,
    }


def _cerebras_call(system_prompt, user_prompt, max_tokens=2500,
                   temperature=0.3, json_mode=True, label='extract'):
    """Single Cerebras chat-completions call. Pure stdlib + requests, no SDK.
    Returns the assistant text or None on failure (auth missing, HTTP error,
    timeout, empty body).

    json_mode=True forces response_format=json_object (extract pass).
    json_mode=False returns plain text (rewrite pass).

    reasoning_effort='low' on every call: gpt-oss-120b emits chain-of-thought
    tokens before the visible answer; low effort keeps latency tight without
    hurting quality on this kind of structured / short-form work."""
    api_key = os.environ.get('CEREBRAS_API_KEY')
    if not api_key:
        print(f'[sourcing-llm:{label}] CEREBRAS_API_KEY not set', flush=True)
        return None
    import requests
    import time as _time
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': _MODEL,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': user_prompt},
        ],
    }
    # reasoning_effort is gpt-oss-specific (suppresses chain-of-thought
    # tokens). Other Cerebras models (qwen-3-*, llama-*, glm-*) reject the
    # param with HTTP 400 'wrong_api_format'. Only set when targeting gpt-oss.
    if _MODEL.startswith('gpt-oss'):
        payload['reasoning_effort'] = 'low'
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    # Retry on transient errors (429 queue_exceeded, 502/503/504 transient
    # backend). Cerebras can burst-cap on shared accounts; most 429s clear
    # within ~1s. Don't retry on 4xx that aren't 429 (bad request, auth,
    # model_not_found) — those are deterministic failures.
    _RETRYABLE_STATUS = {429, 502, 503, 504}
    max_attempts = 3
    resp = None
    _dt = 0
    for attempt in range(1, max_attempts + 1):
        _t0 = _time.monotonic()
        try:
            resp = requests.post(_CEREBRAS_URL, headers=headers, json=payload,
                                 timeout=_CEREBRAS_TIMEOUT_SEC)
        except Exception as e:
            _dt = (_time.monotonic() - _t0) * 1000
            print(f'[sourcing-llm:{label}] request error after {_dt:.0f}ms attempt {attempt}/{max_attempts}: {e}', flush=True)
            if attempt < max_attempts:
                _time.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            return None
        _dt = (_time.monotonic() - _t0) * 1000
        if resp.status_code in _RETRYABLE_STATUS and attempt < max_attempts:
            print(f'[sourcing-llm:{label}] HTTP {resp.status_code} after {_dt:.0f}ms attempt {attempt}/{max_attempts}, retrying...', flush=True)
            _time.sleep(0.5 * (2 ** (attempt - 1)))
            continue
        break
    if resp is None or resp.status_code != 200:
        code = resp.status_code if resp is not None else 'no-response'
        body = (resp.text[:300] if resp is not None else '')
        print(f'[sourcing-llm:{label}] HTTP {code} final after {_dt:.0f}ms: {body}', flush=True)
        return None
    try:
        data = resp.json()
    except Exception as e:
        print(f'[sourcing-llm:{label}] JSON parse error: {e} body={resp.text[:300]!r}', flush=True)
        return None
    try:
        u = data.get('usage') or {}
        print(f'[sourcing-llm:{label}] {_dt:.0f}ms in={u.get("prompt_tokens")} out={u.get("completion_tokens")} model={_MODEL}', flush=True)
    except Exception:
        pass
    choices = data.get('choices') or []
    if not choices:
        return None
    msg = (choices[0] or {}).get('message') or {}
    content = msg.get('content')
    return content.strip() if content else None


_TRANS_PATTERNS = (
    (r'\b(manual|stick|3.?pedal|row your own|manual trans(?:mission)?)\b', 'manual'),
    (r'\bp\s*d\s*k\b', 'pdk'),
    (r'\bd\s*c\s*t\b', 'dct'),
    (r'\b(dual.?clutch)\b', 'dct'),
    (r'\b(automatic|tiptronic|slushbox|auto\s+trans|auto-?box)\b', 'automatic'),
)


def _heuristic_transmission(text):
    """Server-side fallback: detect transmission keywords in raw user text
    when the extractor missed it. Models occasionally drop fields in dense
    comma-lists ('yellow, manual, 28k, 130k')."""
    if not text:
        return None
    s = text.lower()
    for pat, value in _TRANS_PATTERNS:
        if re.search(pat, s):
            return value
    return None


# Lowercased color words the regex backstop recognizes. Order matters:
# longer/more-specific phrases first so 'jet black' beats 'black' alone.
_BACKSTOP_COLOR_WORDS = (
    'red', 'blue', 'black', 'white', 'grey', 'gray', 'silver', 'green',
    'yellow', 'orange', 'purple', 'brown', 'beige', 'gold', 'tan',
    'champagne', 'ivory', 'burgundy', 'maroon', 'navy', 'teal',
)


def _regex_only_extract(user_text):
    """Cheap regex-only extract used when the LLM is unavailable (rate
    limit, timeout, outage). Catches the most common single-message
    refinements (color, year, mileage cap, transmission) so the bot can
    keep limping forward through a rate-limit window. Returns the same
    shape as extract() — {intent, spec_update, spec_clear, sort_pref} —
    or None if nothing useful was parsed.

    Marked with '_heuristic_only' in the response so downstream code can
    log/treat it differently if needed."""
    if not user_text:
        return None
    s = user_text.lower()
    su = _empty_spec_update()
    matched = False

    # Color(s)
    found_colors = []
    for c in _BACKSTOP_COLOR_WORDS:
        if re.search(rf'\b{c}\b', s) and c not in found_colors:
            found_colors.append(c)
    if found_colors:
        su['ext_color'] = found_colors
        matched = True

    # Year — single year or range like "2022-2024" / "21-23"
    yr_range = re.search(r'\b(?:20)?(\d{2,4})\s*[-to]+\s*(?:20)?(\d{2,4})\b', s)
    if yr_range:
        y1 = int(yr_range.group(1))
        y2 = int(yr_range.group(2))
        if y1 < 100: y1 += 2000
        if y2 < 100: y2 += 2000
        if 1990 <= y1 <= 2100 and 1990 <= y2 <= 2100:
            su['year_min'] = min(y1, y2)
            su['year_max'] = max(y1, y2)
            matched = True
    else:
        yr_single = re.search(r'\b(19|20)\d{2}\b', s)
        if yr_single:
            y = int(yr_single.group(0))
            su['year_min'] = y
            su['year_max'] = y
            matched = True

    # Mileage cap: "under 30k mi" / "max 50k miles" / "less than 28000"
    miles_pat = re.search(
        r'(?:under|less than|max|<=?|below|up to)?\s*(\d{1,3})\s*[k]?\s*(?:mi|miles|mile)\b',
        s,
    )
    if miles_pat:
        n = int(miles_pat.group(1))
        if 'k' in miles_pat.group(0).lower():
            n *= 1000
        if 100 <= n <= 999999:
            su['miles_max'] = n
            matched = True

    # Transmission via the existing helper
    tx = _heuristic_transmission(user_text)
    if tx:
        su['transmission'] = tx
        matched = True

    if not matched:
        return None
    return {
        'intent': 'sourcing',
        'spec_update': su,
        'spec_clear': [],
        'sort_pref': None,
        '_heuristic_only': True,
    }


def extract(row, new_user_msg):
    """JSON-only extraction pass. Returns dict
    {intent, sort_pref, spec_update, spec_clear} or None on failure. No reply
    text — Python composes that downstream.

    Failure path: when Cerebras returns nothing (rate limit, outage,
    timeout, parse failure), we try the regex-only backstop so simple
    refinements ('any white ones', '2024', 'under 30k mi') still progress
    instead of hitting the silent fallback ack."""
    user_block = _build_extract_prompt(row, new_user_msg)
    raw = _cerebras_call(EXTRACT_PROMPT, user_block,
                         max_tokens=2000, json_mode=True, label='extract')
    parsed = _parse_response(raw)
    if not parsed:
        backstop = _regex_only_extract(new_user_msg)
        if backstop:
            print(f'[sourcing-llm:extract] LLM unavailable, regex backstop hit: '
                  f'{[(k,v) for k,v in (backstop["spec_update"] or {}).items() if v not in (None,[],False)]}',
                  flush=True)
            return backstop
        return None
    su = parsed.get('spec_update') or {}
    norm_su = _empty_spec_update()
    for k in norm_su:
        if k in su:
            norm_su[k] = su[k]
    # Heuristic backstop on transmission — extractor sometimes misses it
    # in dense comma-lists. Only fill if extractor returned None.
    if not norm_su.get('transmission'):
        tx = _heuristic_transmission(new_user_msg)
        if tx:
            norm_su['transmission'] = tx
            print(f'[sourcing-llm:extract] transmission backstop -> {tx!r}', flush=True)
    parsed['spec_update'] = norm_su
    parsed.setdefault('intent', 'unclear')
    parsed.setdefault('spec_clear', [])
    # Normalize sort_pref to one of the four valid values or None.
    sp = parsed.get('sort_pref')
    if sp not in ('miles_asc', 'miles_desc', 'price_asc', 'price_desc'):
        parsed['sort_pref'] = None
    return parsed


def tone_rewrite(draft, recent_conv=None):
    """Rewrite a templated reply in EW peer-register voice. Returns the
    rewritten string, or the original draft on failure (graceful degrade —
    we never want a tone-rewrite blip to leave the user with nothing).
    Only call this for branches where stylistic variation helps; skip for
    deterministic strings (stop/drop, match presentations, handoff_line)."""
    if not draft or not draft.strip():
        return draft
    user_block = _build_rewrite_prompt(draft, recent_conv)
    raw = _cerebras_call(TONE_REWRITE_PROMPT, user_block,
                         max_tokens=300, json_mode=False, label='rewrite')
    if not raw:
        return draft  # fall back to deterministic draft
    s = raw.strip()
    # Strip any accidental wrapper quotes / backticks.
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    if s.startswith('```'):
        s = s.lstrip('`').strip()
        if s.endswith('```'):
            s = s[:-3].strip()
    # Sanity: rewrite shouldn't more than double the length; if it does, the
    # model probably rambled — fall back to draft.
    if len(s) > max(160, len(draft) * 2):
        print(f'[sourcing-llm:rewrite] length sanity reject ({len(s)} chars > {len(draft)*2}); using draft', flush=True)
        return draft
    return s or draft


def _eta_phrase(now_et=None):
    """Pick the concrete ETA bucket the bot promises wholesalers.

    Simplified 2026-05-11 per user spec — only two windows now:
      - Friday after 4pm + all weekend  -> "sometime on monday"
      - Everything else                 -> "within 24 hours one way or another"
    Same phrasing used by both handoff_line (after 'interested' signal) and
    callback_ack (after where/MSRP/options question)."""
    if now_et is None:
        now_et = datetime.now(_TZ)
    h = now_et.hour
    weekday = now_et.weekday()  # 0=Mon, 6=Sun
    if weekday == 5 or weekday == 6:
        return "sometime on monday"
    if weekday == 4 and h >= 16:
        return "sometime on monday"
    return "within 24 hours one way or another"


def handoff_line(name, now_et=None):
    """Reply sent after the user signals interest in a specific match.
    Deterministic — exact wording matters for wholesaler ETA expectations.
    Used directly (no tone-rewrite)."""
    first = (name or '').strip().split()[0] if name else None
    eta = _eta_phrase(now_et)
    if first:
        return f"thanks {first.lower()} — someone will contact you back {eta}."
    return f"someone will contact you back {eta}."


def callback_ack(name, now_et=None):
    """Reply sent after the user asks 'where is it / how much / what
    options / etc.' Same ETA window as handoff_line — staff covers the
    specifics when they reach out."""
    first = (name or '').strip().split()[0] if name else None
    eta = _eta_phrase(now_et)
    if first:
        return f"thanks {first.lower()} — someone will contact you at this number {eta}."
    return f"someone will contact you at this number {eta}."


# Back-compat alias: old code paths called turn() expecting the all-in-one
# response shape. Hybrid splits that into extract() + Python compose; if any
# stragglers reference turn() we route to extract().
def turn(row, new_user_msg, search_results=None, fallback_level=None):
    return extract(row, new_user_msg)


# ── Spec merge helper ─────────────────────────────────────────────────────
_SCALAR_FIELDS = ('year_min', 'year_max', 'make', 'model', 'trim',
                  'miles_max', 'must_clean_title', 'price_hint',
                  'customer_name', 'transmission')
_LIST_FIELDS = ('ext_color', 'int_color', 'options')

# When spec_clear includes any of these field names, we persist the
# corresponding relaxation tag onto the row (sourcing_requests.relaxations).
# This is what stops the "any year -> what year?" loop: future turns see
# 'year' in row.relaxations and skip the gathering_year branch + skip the
# year filter in inventory search.
_SPEC_CLEAR_TO_RELAXATION = {
    'year_min': 'year', 'year_max': 'year',
    'ext_color': 'ext_color',
    'int_color': 'int_color',
    'trim': 'trim',
    'miles_max': 'miles_max',
    'transmission': 'transmission',
}


def _snapshot_vehicle_interest(row):
    """Build a snapshot dict of the row's current vehicle interest, used to
    push onto vehicle_interests JSONB before a pivot wipes the spec.
    Returns None if the prior interest wasn't substantive (no make/model)."""
    if not row.get('make') or not row.get('model'):
        return None
    return {
        'make': row.get('make'),
        'model': row.get('model'),
        'trim': row.get('trim'),
        'year_min': row.get('year_min'),
        'year_max': row.get('year_max'),
        'ext_color': list(row.get('ext_color') or []),
        'int_color': list(row.get('int_color') or []),
        'miles_max': row.get('miles_max'),
        'transmission': row.get('transmission'),
        'price_hint': row.get('price_hint'),
        'narrative_brief': row.get('narrative_brief'),
        'status_at_pivot': row.get('status'),
        'recap_confirmed_at': (row.get('recap_confirmed_at').isoformat()
                               if row.get('recap_confirmed_at') else None),
        'captured_at': datetime.now(_TZ).isoformat(),
    }


def build_recap(row):
    """Produce the natural-language recap string the bot replays back to the
    user before searching. Maps row fields to the broker peer-register voice
    used in the rest of the system.

    Example output:
      'yellow or red 1997-1999 porsche 911 turbo manual, max 28k mi, around 130k'
    """
    parts = []
    # Color(s) lead — "yellow or red" / "red"
    ec = row.get('ext_color') or []
    if ec:
        if len(ec) == 1:
            parts.append(ec[0])
        elif len(ec) == 2:
            parts.append(f"{ec[0]} or {ec[1]}")
        else:
            parts.append(f"{', '.join(ec[:-1])} or {ec[-1]}")
    # Year range
    ym, yx = row.get('year_min'), row.get('year_max')
    if ym and yx:
        parts.append(f"{ym}-{yx}" if ym != yx else str(ym))
    elif ym:
        parts.append(f"{ym}+")
    elif yx:
        parts.append(f"up to {yx}")
    # Make / model / trim
    if row.get('make'):
        parts.append(row['make'])
    if row.get('model'):
        parts.append(row['model'])
    if row.get('trim'):
        parts.append(row['trim'])
    # Transmission
    if row.get('transmission'):
        parts.append(row['transmission'])
    head = ' '.join(parts) if parts else 'a vehicle'

    extras = []
    if row.get('miles_max'):
        m = int(row['miles_max'])
        extras.append(f"max {m//1000}k mi" if m >= 1000 else f"max {m} mi")
    if row.get('price_hint'):
        p = int(row['price_hint'])
        extras.append(f"around {p//1000}k" if p >= 1000 else f"around {p}")
    ic = row.get('int_color') or []
    if ic:
        extras.append(f"{ic[0]} interior" if len(ic) == 1
                      else f"{' or '.join(ic)} interior")
    if extras:
        return f"{head}, {', '.join(extras)}"
    return head


def merge_spec(row, spec_update, spec_clear=None):
    """Merge non-null values from spec_update; explicitly null any field listed
    in spec_clear. spec_clear takes precedence so 'any color' / 'any year'
    cleanly drops filters.

    Side effect: when spec_clear includes a relaxable field, append the
    corresponding tag to row['relaxations']. If make/model is being CHANGED
    (pivot to a new vehicle), reset relaxations to []. Returns dict of
    {column: new_value} changes for the SQL UPDATE — if relaxations changed,
    'relaxations' is in changes."""
    changes = {}
    spec_clear = set(spec_clear or [])

    # Pivot detection: if make changes, reset all relaxations.
    new_make = (spec_update.get('make') or '').strip().lower()
    cur_make = ((row.get('make') or '').strip().lower())
    pivoted = bool(new_make and cur_make and new_make != cur_make)

    if pivoted:
        # On pivot: snapshot the prior vehicle interest into vehicle_interests
        # (so we don't lose Oscar's Porsche 911 Turbo interest when he moves
        # on to a Ferrari 296). Then reset relaxations (they were about the
        # OLD vehicle) AND if the user didn't specify a year for the new
        # vehicle this turn, treat year as "open" so the bot pivots straight
        # into a search instead of asking "what year?" again.
        snapshot = _snapshot_vehicle_interest(row)
        if snapshot:
            current_interests = list(row.get('vehicle_interests') or [])
            current_interests.append(snapshot)
            row['vehicle_interests'] = current_interests
            changes['vehicle_interests'] = json.dumps(current_interests)
        new_relax = []
        spec_set_year = bool(spec_update.get('year_min') or spec_update.get('year_max'))
        if not spec_set_year:
            new_relax.append('year')
        row['relaxations'] = new_relax
        changes['relaxations'] = new_relax
        # On pivot, the recap from the old vehicle no longer applies. Clear
        # narrative_brief and recap_confirmed_at so the gathering ladder
        # re-asks extras / re-recaps for the new vehicle.
        row['narrative_brief'] = None
        changes['narrative_brief'] = None
        row['recap_confirmed_at'] = None
        changes['recap_confirmed_at'] = None
        # If we were in a terminal state (matched / wishlist) and user is
        # now asking about a different vehicle, reset to 'gathering' so the
        # search-trigger gate in _run_turn passes. _decide will move it to
        # 'presented' (or 'matched' again) downstream after search.
        # Pivots from ANY active state reset to 'gathering' so the recap
        # loop fires for the new vehicle. The user wants a confirm step
        # before searching every new spec.
        if row.get('status') in ('presented', 'matched', 'wishlist', 'searching'):
            row['status'] = 'gathering'
            changes['status'] = 'gathering'

    # Defense against extractor over-applying the PIVOT rule on non-pivot
    # turns. The prompt asks for clearing 6 soft filters at once when the
    # user changes vehicles. If we see that pattern but our pivot
    # detection (make/model unchanged) disagrees, strip those pivot-shaped
    # clears and keep only fields the user explicitly relaxed. Without
    # this, "any 911 turbos" while already on porsche 911 turbo wipes
    # year/color/miles/transmission filters AND adds them to relaxations,
    # disabling them for the rest of the conversation.
    _PIVOT_BATCH_FIELDS = {'year_min', 'year_max', 'ext_color', 'int_color',
                           'trim', 'miles_max', 'transmission', 'price_hint'}
    sc_set = set(spec_clear)
    if not pivoted and len(sc_set & _PIVOT_BATCH_FIELDS) >= 3:
        # Likely extractor error — strip the pivot-shaped clears so they
        # don't relax filters the user never asked to relax. Keep any
        # OTHER spec_clear entries (e.g. 'make' alone is fine).
        kept = [f for f in spec_clear if f not in _PIVOT_BATCH_FIELDS]
        print(f'[merge_spec] stripped pivot-shaped spec_clear (not pivoting); '
              f'dropped={[f for f in spec_clear if f in _PIVOT_BATCH_FIELDS]} '
              f'kept={kept}', flush=True)
        spec_clear = set(kept)

    # spec_update wins when both spec_clear and spec_update touch the same
    # field (clear-old-then-set-new pattern). Bug from 2026-05-10 testing:
    # previously the spec_clear branch had `continue` which skipped the
    # spec_update assignment, so 'turbo' got nulled and the search
    # returned every 911.
    for k in _SCALAR_FIELDS:
        v = spec_update.get(k)
        spec_update_has_value = (v is not None and v != '')
        if k in spec_clear and not spec_update_has_value:
            row[k] = None
            changes[k] = None
            continue
        if spec_update_has_value:
            row[k] = v
            changes[k] = v
    for k in _LIST_FIELDS:
        v = spec_update.get(k)
        v_norm = None
        if v is not None and isinstance(v, list) and len(v) > 0:
            v_norm = [str(x).strip().lower() for x in v
                      if x is not None and str(x).strip()] or None
        if k in spec_clear and not v_norm:
            row[k] = None
            changes[k] = None
            continue
        if v_norm:
            row[k] = v_norm
            changes[k] = v_norm

    # Maintain row.relaxations:
    #   1. REMOVE any relaxation whose underlying field is being set this
    #      turn via spec_update — the user is explicitly re-filtering on
    #      it, so the old "any X" relaxation no longer applies.
    #   2. ADD relaxations from spec_clear entries that aren't being
    #      simultaneously set in spec_update (clear-and-replace doesn't
    #      count as a relaxation).
    # Without #1, a stale relaxation can survive forever (e.g. 'year' got
    # added on a buggy turn, then even when the user types 'any 2025' the
    # search keeps ignoring the year filter because the relaxation tag
    # persists. Adding #1 makes the invariant: relaxations only contains
    # tags the user CURRENTLY wants relaxed.
    if not pivoted:
        cur_relax = set(row.get('relaxations') or [])
        new_relax = set(cur_relax)

        # Step 1: spec_update setting a field UN-relaxes its tag.
        for fld in _SCALAR_FIELDS:
            v = spec_update.get(fld)
            if v not in (None, '', False):
                tag = _SPEC_CLEAR_TO_RELAXATION.get(fld)
                if tag and tag in new_relax:
                    new_relax.discard(tag)
        for fld in _LIST_FIELDS:
            v = spec_update.get(fld)
            if v and isinstance(v, list) and any(str(x).strip() for x in v if x is not None):
                tag = _SPEC_CLEAR_TO_RELAXATION.get(fld)
                if tag and tag in new_relax:
                    new_relax.discard(tag)

        # Step 2: spec_clear entries that aren't simultaneously set in
        # spec_update become relaxations.
        for fld in spec_clear:
            tag = _SPEC_CLEAR_TO_RELAXATION.get(fld)
            if not tag:
                continue
            if fld in _SCALAR_FIELDS:
                v = spec_update.get(fld)
                if v is not None and v != '':
                    continue  # clear-and-replace, not a relaxation
            elif fld in _LIST_FIELDS:
                v = spec_update.get(fld)
                if v and isinstance(v, list) and any(str(x).strip() for x in v if x is not None):
                    continue
            new_relax.add(tag)

        if new_relax != cur_relax:
            row['relaxations'] = sorted(new_relax)
            changes['relaxations'] = sorted(new_relax)

    return changes
