"""
EW sourcing-bot — AI-driven SMS sourcing flow (hybrid architecture).

Per-turn pipeline:
    user msg -> sourcing_gemini.extract()           # JSON: intent + spec
              -> merge_spec into row
              -> _decide(row, parsed, ...)          # Python state machine
              -> (reply_template, next_state, did_search, branch)
              -> if branch in _REWRITE_BRANCHES:
                   sourcing_gemini.tone_rewrite(reply_template, recent_conv)
              -> persist + send

The LLM never writes the user-facing reply text directly. Python composes
the SHAPE; tone_rewrite paraphrases the SHAPE in EW voice when stylistic
variation helps. Deterministic strings (stop/drop acks, match
presentations, handoff_line) bypass the rewrite for predictability.

Public entry point: try_handle_sourcing(from_phone, body, db, cur, ...)
returns True if handled, False to fall through to existing bid logic.
"""
import os
import json
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────
# Falls back to PHASE2_PHONE_GATE so we have a single source of truth for
# the Phase-2 / sourcing wholesaler whitelist. Either env accepts digit-only
# (4074309675), E.164 (+14074309675), or formatted (407-430-9675) — all
# normalize to the same 10-digit key for comparison. '*' opens to all.
_GATE = (os.getenv('SOURCING_PHONE_GATE')
         or os.getenv('PHASE2_PHONE_GATE')
         or '+14074309675')
_GATED_PHONES_RAW = {p.strip() for p in _GATE.replace(',', ' ').split() if p.strip()}
_GATE_OPEN = '*' in _GATED_PHONES_RAW


def _digits(p):
    """Normalize a phone to its last 10 digits ('+14074309675'→'4074309675')."""
    d = ''.join(c for c in (p or '') if c.isdigit())
    if len(d) == 11 and d[0] == '1':
        d = d[1:]
    return d


_GATED_DIGITS = {_digits(p) for p in _GATED_PHONES_RAW if _digits(p)}
# Threads are now effectively indefinite — wholesalers should be able to
# come back weeks/months later and pick up where they left off, like a real
# broker would remember a regular client. The re_engagement branch primes
# returning users with their last narrative_brief; vehicle pivots snapshot
# prior interests into vehicle_interests JSONB. Nothing auto-archives on
# time silence. Manual archive still works via drop_it / stop intents and
# via the dashboard close button.
_ACTIVE_DAYS = 36500


# ── Heuristic intent detection (still used to decide when to OPEN a new
# request — once a request exists, every inbound goes to Gemini) ─────────
_SOURCING_HINTS = (
    'looking for', 'looking 4', 'lookin for',
    'need a ', 'need an ',
    'got any', 'do you have', 'do u have',
    'find me', 'find a ', 'find an ',
    'searching for', 'search for', 'looking 2 buy',
    'in the market', 'wanted: ', 'wts ',
    'help me find',
)


def _looks_like_bid_intake(body, num_media=0):
    if num_media:
        return True
    if not body:
        return False
    import re
    if re.search(r'\b[A-HJ-NPR-Z0-9]{17}\b', body, re.IGNORECASE):
        return True
    return False


def _looks_like_sourcing(body):
    if not body:
        return False
    bl = body.lower()
    return any(h in bl for h in _SOURCING_HINTS)


def _is_plausible_sourcing(body):
    """Cold-start guard. True only if the body is plausibly a real sourcing
    request, not a bare number, single-word ack, or stray punctuation.

    Added 2026-05-10 after bid 1129's mileage SMS '12765' was misclassified
    as a sourcing request. Tightened-then-relaxed same evening when 'Red 296'
    (a real Ferrari sourcing ask) was being rejected and falling through to
    the share-reply path.

    Rejects: '', '12765', 'ok', 'thx', '?', '5/10/2026', 'thanks', 'hi'.
    Accepts: 'looking for a 911 gts', 'porsche 911 gt3 2022',
    'got any maserati mc20', 'Red 296', 'porsche 911', 'any 458'.
    """
    if not body:
        return False
    s = body.strip()
    if len(s) < 4:
        return False
    import re as _re
    alpha_tokens = _re.findall(r'[A-Za-z]{3,}', s)
    if not alpha_tokens:
        return False
    bl = s.lower()
    # Strong signals.
    if any(h in bl for h in _SOURCING_HINTS):
        return True
    if len(alpha_tokens) >= 2:
        return True
    if _re.search(r'\b(19|20)\d{2}\b', s):
        return True
    # 2-4 digit non-year token alongside an alpha word: classic
    # "color + model number" shape — Red 296, porsche 911, any 458, AMG 63.
    # Excludes 5+ digit mileage-shaped tokens like "12765".
    for tok in _re.findall(r'\b\d{2,4}\b', s):
        n = int(tok)
        if 1900 <= n <= 2100:
            continue
        return True
    return False


def _has_recent_sms_bid(cur, phone):
    """True if this phone has a SMS-driven bid created in the last 60s.
    Used to yield routing precedence to the bid-stitch path so that bare-
    mileage / bare-VIN follow-ups to a fresh bid don't get hijacked into
    sourcing. Mirrors the same window used by the stitch logic in app.py."""
    try:
        cur.execute("""
            SELECT 1 FROM bids
            WHERE phone = %s
              AND created_at > NOW() - INTERVAL '60 seconds'
              AND driver_token IS NOT NULL
            LIMIT 1
        """, (phone,))
        return cur.fetchone() is not None
    except Exception as _e:
        print(f'[sourcing] recent-bid check error: {_e}', flush=True)
        return False


def _phone_allowed(phone):
    # _GATE_OPEN is the env-only emergency "open to all" switch (a literal
    # '*' in SOURCING_PHONE_GATE / PHASE2_PHONE_GATE). Otherwise we consult
    # gate_helpers, which UNIONs the env baseline with the gated_phones DB
    # table (30s cache, refreshed by /admin/phone-gates writes).
    if _GATE_OPEN:
        return True
    try:
        import gate_helpers
        return _digits(phone) in gate_helpers.gate_digits('sourcing')
    except Exception as e:
        print(f'[sourcing-gate] gate_helpers failed, falling back to env-only: {e}', flush=True)
        return _digits(phone) in _GATED_DIGITS


def _append_msg(conversation, role, text, raw=None):
    entry = {
        'role': role,
        'ts': datetime.now(timezone.utc).isoformat(),
        'text': text,
    }
    if raw is not None:
        entry['raw'] = raw
    return (conversation or []) + [entry]


def _find_active(cur, phone):
    cur.execute("""
        SELECT * FROM sourcing_requests
        WHERE phone = %s
          AND status <> 'archived'
          AND last_msg_at > NOW() - INTERVAL '%s days'
        ORDER BY last_msg_at DESC
        LIMIT 1
    """, (phone, _ACTIVE_DAYS))
    return cur.fetchone()


_STOP_KEYWORDS = ('stop', 'unsubscribe', 'quit', 'cancel')
_DROP_KEYWORDS = ('drop it', 'dropit', 'drop that', 'nevermind', 'never mind')


def _handle_stop_or_drop(cur, phone, body_lc, active):
    if active and any(k == body_lc or body_lc.startswith(k + ' ') for k in _DROP_KEYWORDS):
        new_conv = _append_msg(active.get('conversation'), 'user', body_lc)
        cur.execute("""UPDATE sourcing_requests
                          SET status='archived',
                              archived_at=NOW(),
                              archive_reason='dropped',
                              conversation=%s::jsonb,
                              last_msg_at=NOW(),
                              last_inbound_at=NOW()
                        WHERE id=%s""",
                    (json.dumps(new_conv), active['id']))
        return True
    if active and body_lc in _STOP_KEYWORDS:
        cur.execute("""UPDATE sourcing_requests
                          SET status='archived',
                              archived_at=NOW(),
                              archive_reason='stopped'
                        WHERE id=%s""", (active['id'],))
        return True
    return False


# ── Spec persistence ──────────────────────────────────────────────────────
_DB_SCALAR = ('year_min', 'year_max', 'make', 'model', 'trim',
              'miles_max', 'must_clean_title', 'price_hint')
_DB_LIST = ('ext_color', 'int_color', 'options')


def _build_spec_update_sql(changes):
    """changes is {field: value}. Returns (set_clauses, params) for SQL."""
    sets = []
    params = []
    for k, v in changes.items():
        sets.append(f"{k} = %s")
        params.append(v)
    return sets, params


# ── Reply composition (pure functions; Python decides shape, not LLM) ─────

def _format_match(m):
    from sourcing_gemini import _fmt_miles
    ec = m.get('ext_color')
    ic = m.get('int_color')
    if ec and ic:
        color = f"{ec} / {ic}"
    elif ec:
        color = ec
    elif ic:
        color = f"{ic} interior"
    else:
        color = ''
    head_parts = [str(m.get('year') or ''), str(m.get('model') or ''),
                  str(m.get('trim') or '')]
    head = ' '.join(p for p in head_parts if p).strip()
    pieces = [head]
    if color:
        pieces.append(color)
    pieces.append(_fmt_miles(m.get('mileage')))
    return ', '.join(pieces)


def _short_desc(row):
    parts = []
    if row.get('year_min') or row.get('year_max'):
        if row.get('year_min') == row.get('year_max'):
            parts.append(str(row.get('year_min')))
        else:
            parts.append(f"{row.get('year_min','?')}-{row.get('year_max','?')}")
    for k in ('make', 'model', 'trim'):
        if row.get(k):
            parts.append(row[k])
    s = ' '.join(parts) or 'your search'
    if row.get('ext_color'):
        s += f" in {row['ext_color'][0]}"
    return s


def _present_results(matches, price_hint_set=False):
    """Format the top 3 matches followed by the AUTO callback offer.

    Per simplified architecture (2026-05-11): after presenting matches we
    immediately pivot to 'want staff to reach out?' — no menu of pick-one
    options, no 'which works best?' question. Wholesaler either confirms
    (any non-spec response) or refines (sends a new spec, which loops back
    to recap)."""
    body = '\n'.join(_format_match(m) for m in matches[:3])
    n = len(matches)
    if n > 3:
        return (f"{body}\ngot {n} total. want someone from our staff to "
                f"reach out, or want to see the rest?")
    return f"{body}\nwant someone from our staff to reach out about this?"


def _no_match_offer(row):
    """Build a relaxation menu based on what filters are set in the spec."""
    opts = []
    if row.get('ext_color'):
        opts.append("other colors")
    if row.get('trim'):
        opts.append("other trims")
    if row.get('year_min') or row.get('year_max'):
        opts.append("other years")
    if row.get('miles_max'):
        opts.append("a higher mileage cap")
    if opts:
        return (f"nothing matching that right now. want me to look at "
                f"{', '.join(opts)}, or flag it for when one shows up?")
    return ("nothing matching right now. want me to flag it for when one "
            "shows up?")


_AMBIGUOUS_TRIMS = {
    'gts', 'turbo', 'turbo s', 's', 'gt', 'gt s', 'touring',
    'sport', 'sport+',
}

# Phrases that mean "tell me the price". Detected in user_text so when
# intent='interested' AND the user asked for price in the same breath
# ("yes how much" / "interested, what's the cost"), the handoff reply
# explicitly addresses pricing instead of silently dropping it.
_PRICE_QUESTION_PATTERNS = (
    r'how much', r"how much'?s", r'what.{0,10}cost', r"what'?s\s+the\s+cost",
    r'what.{0,10}price', r"what'?s\s+the\s+price", r'asking price',
    r'price tag', r'sticker price', r'\bmsrp\b',
    r"what'?s\s+it\s+go(?:ing)?\s+for", r"what.{0,15}they\s+go(?:ing)?\s+for",
    r'\bprice\b', r'\bcost\b', r'\bbudget\b',
)


def _mentions_price_question(text):
    """Cheap regex check for 'how much'/'what's the cost'/etc. in user text."""
    if not text:
        return False
    import re as _re
    s = (text or '').strip().lower()
    for pat in _PRICE_QUESTION_PATTERNS:
        if _re.search(pat, s):
            return True
    return False


# Broader specific-question detector — generalizes _mentions_price_question
# across the full vocabulary of "I want detail X on this car" questions
# (location, options, photos, VIN, carfax, miles). Used to fold a tailored
# 'staff will cover X' clause into callback_ack / handoff_line replies so
# the bot doesn't drop the specifics the user asked about.
_TOPIC_PATTERNS = (
    ('price',    _PRICE_QUESTION_PATTERNS),
    ('location', (r'\bwhere\b', r'\blocation\b', r'\bwhat\s+state\b',
                  r'\bwhat\s+city\b', r'\bzip\s*code\b', r'\bnearest\b')),
    ('options',  (r'\boption(?:s|\s+list|\s+sheet)?\b', r'\bequipment\b',
                  r'\bfeatures?\b', r'\bpackage[ds]?\b', r'\bspec[s]?\b',
                  r'\bbuild\s+sheet\b', r'\bsticker\b', r'\bwindow\s+sticker\b')),
    ('photos',   (r'\bphotos?\b', r'\bpic(?:tures?|s)?\b', r'\bimages?\b',
                  r'\bsee\s+it\b')),
    ('vin',      (r'\bvin\b', r'\bvin\s*#', r'\bvin\s*number\b')),
    ('history',  (r'\bcarfax\b', r'\bautocheck\b', r'\bhistory\b',
                  r'\baccidents?\b', r'\btitle\s+status\b', r'\bclean\s+title\b')),
    ('miles',    (r'\bhow\s+many\s+miles\b', r'\bactual\s+mileage\b',
                  r'\bodometer\b', r'\breal\s+miles\b')),
)


def _specific_question_topics(text):
    """Return a list of topic tags (['price','location','options',...]) for
    every specific-info question detected in the user's text. Order follows
    _TOPIC_PATTERNS (price first since it's most common). Each topic
    appears at most once even if multiple patterns hit."""
    if not text:
        return []
    import re as _re
    s = (text or '').strip().lower()
    found = []
    for topic, patterns in _TOPIC_PATTERNS:
        for pat in patterns:
            if _re.search(pat, s):
                found.append(topic)
                break
    return found


def _topic_clarifier(topics):
    """Build the trailing 'staff will cover X' clause appended to
    callback_ack / handoff_line based on which specific topics the user
    asked about. Returns None if no topics — caller uses the plain ack.
    Pricing gets a special disclosure ('not something we quote here')
    per the never-quote-prices rule."""
    if not topics:
        return None
    # Labels kept short and single-word where possible — when stitched into
    # "staff will cover X, Y, and Z", multi-word labels with their own "and"
    # ("options and equipment") read awkwardly alongside other items.
    label_map = {
        'price':    'pricing',
        'location': 'location',
        'options':  'options',
        'photos':   'photos',
        'vin':      'the VIN',
        'history':  'carfax',
        'miles':    'mileage',
    }
    labels = [label_map[t] for t in topics if t in label_map]
    if not labels:
        return None
    if topics == ['price']:
        return "pricing's not something we quote here — staff will cover it when they reach out."
    if 'price' in topics:
        others = [label_map[t] for t in topics if t != 'price']
        if len(others) == 1:
            others_text = others[0]
        elif len(others) == 2:
            others_text = f"{others[0]} and {others[1]}"
        else:
            others_text = f"{', '.join(others[:-1])}, and {others[-1]}"
        return (f"on pricing — not something we quote here, but staff will cover "
                f"{others_text} (and pricing) when they reach out.")
    if len(labels) == 1:
        return f"staff will cover {labels[0]} when they reach out."
    if len(labels) == 2:
        return f"staff will cover {labels[0]} and {labels[1]} when they reach out."
    return f"staff will cover {', '.join(labels[:-1])}, and {labels[-1]} when they reach out."


_MAKE_SPECIFIC_TRIMS = {
    'carrera', 'gt3', 'gt4', 'targa', 'panamera', 'macan', 'cayenne',
    'amg', 'g63', 'g550', 'g-class', 'g class',
    'm3', 'm4', 'm5', 'm8', 'x6m',
    'rs', 'r8',
}


def _is_ambiguous_trim_pivot(row, parsed, user_text):
    """True if user's message is a bare trim name without a fresh make AND
    the trim spans multiple makes — bot should probe 'still {make}?' before
    pivoting.

    Tightened 2026-05-10: probe was firing too eagerly when the user was
    REFINING the existing search (e.g. 'any yellow turbos' or '911 turbo
    in yellow' after we already had make=porsche). Now we skip the probe
    when the user this turn also gave a model, color, or year — those are
    refinement signals on the existing make, not bare-trim pivots."""
    su = (parsed or {}).get('spec_update') or {}
    new_make = (su.get('make') or '').strip().lower()
    if new_make:
        return False  # user named the make this turn — no ambiguity
    # Refinement signals: user is adding detail to the existing search, not
    # pivoting to a different vehicle. Skip the probe.
    if su.get('model') or su.get('ext_color') or su.get('int_color'):
        return False
    if su.get('year_min') or su.get('year_max'):
        return False
    new_trim = (su.get('trim') or '').strip().lower()
    body_lc = (user_text or '').strip().lower()
    if not row.get('make'):
        return False
    candidate = (new_trim or body_lc).strip()
    if not candidate:
        return False
    if candidate in _MAKE_SPECIFIC_TRIMS:
        return False
    if candidate in _AMBIGUOUS_TRIMS:
        return True
    for trim in _AMBIGUOUS_TRIMS:
        if trim in candidate.split():
            return True
    return False


# Branch labels returned by _decide(). _REWRITE_BRANCHES picks which ones
# get sent through the tone-rewrite layer; the rest go out verbatim.
# Simplified architecture (2026-05-11) — all replies are templated.
# Tone-rewrite layer is gone; the templates ARE the voice. _REWRITE_BRANCHES
# kept as an empty set so the rewrite call in _run_turn becomes a no-op
# without restructuring the call site.
_REWRITE_BRANCHES = set()


# Deterministic rotation of opener phrases for the recap question so the
# bot doesn't say "great — just to confirm..." identically every turn. Index
# = turn count modulo len, so same input from the same row position always
# produces the same opener (predictable for debugging).
_RECAP_OPENERS = (
    "great — just to confirm, you're looking for {brief}. correct?",
    "got it. {brief} — sound right?",
    "ok — you're looking for {brief}. that right?",
    "noted. just confirming — {brief}. correct?",
)


def _pick_recap_phrase(row, brief):
    conv_len = len(row.get('conversation') or [])
    template = _RECAP_OPENERS[conv_len % len(_RECAP_OPENERS)]
    return template.format(brief=brief)


# Steer-toward-task openers for users who chit-chat instead of giving a
# spec ("hi" / "how are you" / "thanks"). Always asks for make+model so
# they know what to give us. Rotates to avoid sounding like a broken
# record on consecutive non-spec messages.
_COLD_OPEN_VARIANTS = (
    "hey — what make and model are you looking for?",
    "what car can i help you find? make and model is enough to start.",
    "what are you in the market for? drop a make and model.",
    "hey — give me a make and model and i'll see what we've got.",
)


def _pick_cold_open(row):
    n = len(row.get('conversation') or [])
    return _COLD_OPEN_VARIANTS[n % len(_COLD_OPEN_VARIANTS)]


def _last_bot_branch(conversation):
    """Branch label of the most recent bot turn, or None. Used by _decide
    to track multi-step flow position (callback ack, name ask, etc)."""
    for t in reversed(conversation or []):
        if t.get('role') == 'bot':
            return ((t.get('raw') or {}).get('branch'))
    return None


def _no_match_offer_relax_aware(row, sibling_models=None):
    """No-match offer that:
      - lists narrowing buckets the user can drop (skips any already relaxed)
      - if we have OTHER models in stock for the same make, lists them
      - offers a generic pivot ('a different make') as final fallback
      - offers to flag for the wishlist
    Both the narrow_opts AND the sibling clause are preserved — earlier
    versions dropped narrow_opts when siblings were populated, which is
    why the 'yellow 911 turbo' search lost its 'other colors' option."""
    relaxed = set(row.get('relaxations') or [])
    narrow_opts = []
    if row.get('ext_color') and 'ext_color' not in relaxed:
        narrow_opts.append("other colors")
    if row.get('trim') and 'trim' not in relaxed:
        narrow_opts.append("other trims")
    if (row.get('year_min') or row.get('year_max')) and 'year' not in relaxed:
        narrow_opts.append("other years")
    if row.get('miles_max') and 'miles_max' not in relaxed:
        narrow_opts.append("a higher mileage cap")

    options = []
    if narrow_opts:
        options.append(', '.join(narrow_opts))
    if sibling_models and row.get('make'):
        names = ', '.join(sibling_models[:3])
        options.append(f"another {row['make']} ({names})")
    options.append("a different make")
    options.append("flag it for when one shows up")

    return (f"no exact match right now. want me to try "
            f"{', '.join(options[:-1])}, or {options[-1]}?")


def _decide(row, parsed, search_results, user_text):
    """Pure function: pick (reply_template, next_state, did_search, branch)
    from the merged spec + extracted intent. NO I/O, NO LLM call here.

    branch is a short label used by _run_turn to decide whether to apply
    the tone-rewrite layer and for log filtering."""
    from sourcing_gemini import handoff_line, callback_ack, build_recap

    intent = (parsed or {}).get('intent') or 'unclear'
    relaxed = set(row.get('relaxations') or [])
    has_make  = bool(row.get('make'))
    has_model = bool(row.get('model'))
    # year-or-relaxed: counts as "satisfied" if user has explicitly opted out.
    has_year  = bool(row.get('year_min') or row.get('year_max')) or 'year' in relaxed
    has_name  = bool(row.get('customer_name'))
    name_first = (row.get('customer_name') or '').strip().split()[0].lower() if has_name else None
    status = row.get('status', 'gathering')
    last_branch = _last_bot_branch(row.get('conversation'))

    # Whether the extractor returned any new spec/clear data this turn.
    # Used below to distinguish "user is refining the search" from "user is
    # responding to our auto-callback offer". customer_name is identity
    # (user volunteering a name) NOT a search-filter change, so it
    # doesn't count toward spec_changed — otherwise giving a name after
    # present_matches falls through to default recap instead of routing
    # to the callback flow.
    spec_update = parsed.get('spec_update') or {}
    spec_clear = parsed.get('spec_clear') or []
    _NON_FILTER_FIELDS = {'customer_name'}
    spec_changed = (
        any(v not in (None, [], False)
            for k, v in spec_update.items() if k not in _NON_FILTER_FIELDS)
        or bool(spec_clear)
    )
    # no_match_offer is intentionally NOT in this tuple — when no matches
    # exist, there's no callback path, only the watch-list flow handled by
    # the dedicated `last_branch == 'no_match_offer'` block below.
    present_branches = ('present_matches', 'present_matches_sorted',
                        'present_all')

    # ── Hard stops ────────────────────────────────────────────────────────
    if intent == 'stop':
        return ("ok, stopped. text us anytime.", 'archived', False, 'stop')
    if intent == 'drop_it':
        return ("ok, dropped. text us anytime.", 'archived', False, 'drop')

    # ── Re-engagement for returning matched/wishlist users ───────────────
    if (intent == 'unclear' and has_name
        and status in ('matched', 'wishlist')
        and not spec_changed
        and len((user_text or '').strip()) <= 60):
        hours_since = None
        last_msg = row.get('last_msg_at')
        if last_msg:
            try:
                from datetime import datetime as _dt, timezone as _tz
                lm = last_msg if not isinstance(last_msg, str) else _dt.fromisoformat(last_msg)
                if lm.tzinfo is None:
                    lm = lm.replace(tzinfo=_tz.utc)
                hours_since = (_dt.now(_tz.utc) - lm).total_seconds() / 3600
            except Exception:
                hours_since = None
        if hours_since and hours_since > 24 and row.get('narrative_brief'):
            return (f"hey {name_first} — last we talked you were on {row['narrative_brief']}. anything new on that or different today?",
                    status, False, 're_engagement')
        return (f"hey {name_first} — anything else you want to look at?",
                status, False, 're_engagement')

    # ── Auto-callback flow after present ─────────────────────────────────
    # When the bot's last reply was a match presentation (or no-match offer)
    # AND user responds without a spec change, treat it as accepting the
    # auto-callback question. Refinements (spec changes) fall through to
    # the recap path below for re-confirmation.
    if last_branch in present_branches and not spec_changed:
        if intent == 'not_interested':
            return ("ok — let me know if you want to look at something else.",
                    'presented', False, 'declined_callback')
        # Anything else = "yes, want callback" (callback_yes, interested,
        # more_details, sourcing-with-no-data, name_provided, unclear).
        topics = _specific_question_topics(user_text)
        clarifier = _topic_clarifier(topics)
        if has_name:
            base = callback_ack(row['customer_name'])
            if clarifier:
                return (f"{base} {clarifier}", 'matched', False, 'callback_ack_specific')
            return (base, 'matched', False, 'callback_ack')
        # No name yet — ask.
        return ("what's your name so we can have someone reach out?",
                'presented', False, 'callback_name_ask')

    # ── Name capture after callback name ask ─────────────────────────────
    if last_branch == 'callback_name_ask' and has_name:
        return (callback_ack(row['customer_name']), 'matched', False, 'callback_ack')

    # ── Recap confirm → either drill for extras OR trigger search ────────
    # On FIRST confirm with a bare spec (just make+model, no year/color/
    # mileage/budget), drill for extras before searching — the user said
    # "yes to my porsche 911" but we don't know what kind. After they give
    # extras, the default-recap path fires again with the richer brief.
    # If extras already exist OR user explicitly skips, fall through to
    # the interim "locked in" → search → present pipeline.
    # Drill ONCE per VEHICLE SESSION for extras, regardless of how rich
    # the initial spec is. Even if the user volunteered "porsche 911 turbo
    # s" up front, we still surface the year/color/mileage/budget menu on
    # the first confirm — narrows wide result sets and gives a uniform UX.
    # "Session" = since the last make pivot. Scanning the full history was
    # broken for long-lived rows: a drill from months ago would suppress
    # drill on a brand-new vehicle search. We now scan backwards and stop
    # at the most recent make change.
    already_drilled = False
    _curr_make = (row.get('make') or '').lower()
    for _msg in reversed(row.get('conversation') or []):
        _raw = _msg.get('raw') or {}
        _br = _raw.get('branch')
        if _br in ('gathering_extras', 'recap_skipped', 'recap_confirmed'):
            already_drilled = True
            break
        # Pivot detection: a prior extract that proposed a different make
        # marks the boundary of the current vehicle session.
        _extr = _raw.get('extract') or {}
        _su = _extr.get('spec_update') or {}
        _mk = (_su.get('make') or '').lower() if isinstance(_su, dict) else ''
        if _mk and _curr_make and _mk != _curr_make:
            break
    if intent == 'confirm_recap' and last_branch == 'recap_pending' and search_results is None:
        if not already_drilled:
            return ("great. any preference on year, color, mileage, transmission, "
                    "or budget? or just want to see what we've got?",
                    'gathering', False, 'gathering_extras')
        return ("locked in. one sec.", 'searching', False, 'recap_confirmed')

    # After we asked for extras, a non-spec response ("yes" / "no preference"
    # / "just show me") triggers the search. Spec response falls through to
    # default recap with the updated brief.
    if last_branch == 'gathering_extras' and not spec_changed:
        if intent in ('not_interested', 'drop_it'):
            return ("ok — let me know if you change your mind.",
                    'presented', False, 'declined_extras')
        return ("locked in. one sec.", 'searching', False, 'recap_skipped')

    # ── After no_match_offer: route based on user's reply ───────────────
    # Offer is compound ("flag for watch list, OR look at something else?")
    # so a bare "yes" is ambiguous about which option they picked. Routing:
    #   * Explicit watch-list keyword (flag/alert/watch/notify) → wishlist
    #   * Explicit pivot keyword (else/different/another) → cold_open_redirect
    #   * Bare "yes" / "sure" / "ok" with no keyword → wishlist_clarify
    #     (ask one binary follow-up before committing)
    #   * Explicit "no" / not_interested → cold_open_redirect (restate)
    #   * Anything else with no spec change → cold_open_redirect (restate)
    if last_branch == 'no_match_offer' and not spec_changed:
        text_lc = (user_text or '').strip().lower()
        # Numbered shortcuts first — "1" / "1." / "one" → watch list,
        # "2" / "2." / "two" → look at something else. These bypass all
        # keyword/intent heuristics so the user can answer unambiguously
        # with a single digit.
        first_tok = text_lc.split()[0].rstrip('.') if text_lc else ''
        if first_tok in ('1', 'one', '#1'):
            brief = build_recap(row) or 'that'
            if has_name:
                return (f"saved {brief} to the watch list, {name_first}. "
                        f"we'll contact you when one shows up.",
                        'wishlist', False, 'wishlist_saved_with_name')
            return ("great — what's your name so we know who to contact "
                    "when one shows up?",
                    'wishlist', False, 'wishlist_name_ask')
        if first_tok in ('2', 'two', '#2'):
            return ("ok — what would you like to look at instead?",
                    'gathering', False, 'cold_open_redirect')
        _WATCH_WORDS = ('watch list', 'watchlist', 'wishlist', 'wish list',
                        'put it on', 'put me on', 'alert me', 'alert',
                        'notify', 'flag it', 'flag', 'let me know',
                        'when one', 'when it', 'when you find',
                        'when you get', 'when you have', 'first one',
                        'contact me when')
        _ELSE_WORDS = ('something else', 'different', 'another car',
                       'another one', 'other car', 'instead', 'else',
                       'no thanks', 'no thx', 'no thank you')
        _BARE_YES = {'yes', 'yeah', 'yep', 'yup', 'sure', 'ok', 'okay',
                     'k', 'kk', 'y', 'fine', 'sounds good', 'please',
                     'do it', 'go ahead'}
        is_bare_yes = text_lc in _BARE_YES
        has_watch_kw = any(w in text_lc for w in _WATCH_WORDS)
        has_else_kw = any(w in text_lc for w in _ELSE_WORDS)
        # Explicit watch-list signal
        if has_watch_kw and not has_else_kw:
            brief = build_recap(row) or 'that'
            if has_name:
                return (f"saved {brief} to the watch list, {name_first}. "
                        f"we'll contact you when one shows up.",
                        'wishlist', False, 'wishlist_saved_with_name')
            return ("great — what's your name so we know who to contact "
                    "when one shows up?",
                    'wishlist', False, 'wishlist_name_ask')
        # Explicit pivot signal
        if has_else_kw and not has_watch_kw:
            return ("ok — what would you like to look at instead?",
                    'gathering', False, 'cold_open_redirect')
        # Bare yes — they meant one of the two options but didn't say
        # which. Re-prompt with the same numbered menu so the answer
        # space is unambiguous.
        if is_bare_yes:
            brief = build_recap(row) or 'that'
            return (f"which one? reply 1 to flag {brief} for the watch "
                    f"list, or 2 to look at something else.",
                    'presented', False, 'wishlist_clarify')
        # Decline
        if intent in ('not_interested', 'drop_it'):
            return ("ok — what would you like to look at instead?",
                    'gathering', False, 'cold_open_redirect')
        # Anything else: assume pivot and restate
        return ("ok — what would you like to look at instead?",
                'gathering', False, 'cold_open_redirect')

    # ── After wishlist_clarify: 1/2 or yes/no on watch-list ──────────────
    if last_branch == 'wishlist_clarify' and not spec_changed:
        text_lc = (user_text or '').strip().lower()
        first_tok = text_lc.split()[0].rstrip('.') if text_lc else ''
        if first_tok in ('2', 'two', '#2'):
            return ("ok — what would you like to look at instead?",
                    'gathering', False, 'cold_open_redirect')
        if first_tok in ('1', 'one', '#1'):
            brief = build_recap(row) or 'that'
            if has_name:
                return (f"saved {brief} to the watch list, {name_first}. "
                        f"we'll contact you when one shows up.",
                        'wishlist', False, 'wishlist_saved_with_name')
            return ("great — what's your name so we know who to contact "
                    "when one shows up?",
                    'wishlist', False, 'wishlist_name_ask')
        _NO_WORDS = ('no', 'nope', 'nah', 'not', 'else', 'different',
                     'another', 'instead', 'look at')
        is_no = (intent in ('not_interested', 'drop_it')
                 or any(w in text_lc.split() for w in _NO_WORDS)
                 or 'something else' in text_lc)
        if is_no:
            return ("ok — what would you like to look at instead?",
                    'gathering', False, 'cold_open_redirect')
        # Anything that looks like yes (bare yes, watch-list keyword,
        # extend_wishlist intent) → commit to watch list.
        brief = build_recap(row) or 'that'
        if has_name:
            return (f"saved {brief} to the watch list, {name_first}. "
                    f"we'll contact you when one shows up.",
                    'wishlist', False, 'wishlist_saved_with_name')
        return ("great — what's your name so we know who to contact "
                "when one shows up?",
                'wishlist', False, 'wishlist_name_ask')

    # ── After wishlist_name_ask: user gave name, save and confirm ────────
    if last_branch == 'wishlist_name_ask' and has_name:
        brief = row.get('narrative_brief') or build_recap(row) or 'that'
        return (f"thanks {name_first} — saved {brief} to the watch list. "
                f"we'll contact you when one shows up.",
                'wishlist', False, 'wishlist_saved_with_name')

    # ── Wishlist explicit save (legacy intent='extend_wishlist' from
    # other contexts than no_match_offer — e.g. user volunteers a flag) ──
    if intent == 'extend_wishlist':
        desc = _short_desc(row)
        if has_name:
            return (f"done, {name_first}. saved: {desc}. text 'drop it' anytime.",
                    'wishlist', False, 'extend_wishlist_with_name')
        return (f"done. saved: {desc}. text 'drop it' anytime. and what's your name so we know who to text?",
                'wishlist', False, 'extend_wishlist_no_name')

    # ── Sort request (after presenting matches) ──────────────────────────
    if intent == 'sort_request' and search_results:
        return (_present_results(search_results, price_hint_set=bool(row.get('price_hint'))),
                'presented', True, 'present_matches_sorted')

    # ── "Show me the rest" pagination ────────────────────────────────────
    if intent == 'more_results' and search_results:
        n = len(search_results)
        cap = 10
        body = '\n'.join(_format_match(m) for m in search_results[:cap])
        tail = (f"\nthat's {cap} of {n}. " if n > cap else f"\n")
        tail += "want someone from our staff to reach out about any of these?"
        return (body + tail, 'presented', True, 'present_all')

    # ── Search results in hand (second decide pass) ──────────────────────
    if search_results is not None:
        if search_results:
            return (_present_results(search_results, price_hint_set=bool(row.get('price_hint'))),
                    'presented', True, 'present_matches')
        # 0 matches — ask ONE binary question first (watch list yes/no).
        # If yes → wishlist_name_ask. If anything else → assume they'd
        # rather pivot, and explicitly restate the second path ("what would
        # you like to look at instead?") so they're never confused about
        # what "yes" / "no" committed them to.
        brief = build_recap(row) or 'that'
        return (f"no exact match for {brief}.\n"
                f"  1. flag it for the watch list (we'll contact you when "
                f"one shows up)\n"
                f"  2. look at something else\n"
                f"reply 1 or 2.",
                'presented', True, 'no_match_offer')

    # ── Cold open: no spec at all yet ────────────────────────────────────
    if not has_make and not has_model:
        return (_pick_cold_open(row),
                'gathering', False, 'cold_open')

    # ── DEFAULT: recap + ask to confirm ──────────────────────────────────
    # Every other path lands here. Build a readback from current row state
    # and ask the user to confirm. They either say "yes" (-> search) or
    # send another message that adjusts the spec (-> loop back here with
    # an updated recap). Single LLM call per turn (the extract pass);
    # everything else is Python templating.
    brief = build_recap(row)
    return (_pick_recap_phrase(row, brief),
            'gathering', False, 'recap_pending')


# ── Main turn handler — reusable for both new + continuing requests ───────
def _run_turn(db, cur, request_id, row, user_text, num_media=0,
              send_sms=None, phone=None):
    """One inbound turn:
      1. extract via Cerebras (JSON)
      2. merge spec into row
      3. _decide picks reply shape + next_state (does search if needed)
      4. tone_rewrite if branch is rewrite-eligible
      5. persist + send

    Returns dict {intent, branch, did_search, match_count} for logging,
    or None on hard extract failure (already handled with a graceful ack)."""
    from sourcing_gemini import extract as llm_extract, tone_rewrite, merge_spec
    from sourcing_search import search as inv_search, to_match_descs

    # Append user msg into the row (extract reads conversation history).
    row['conversation'] = _append_msg(row.get('conversation'), 'user', user_text)

    parsed = llm_extract(row, user_text)
    if not parsed:
        # Hard failure: extractor returned nothing usable. Send a graceful
        # ack instead of going silent. Keep the user-side conversation
        # intact so they can retry without confusion.
        fallback = "got it — one sec."
        row['conversation'] = _append_msg(row['conversation'], 'bot', fallback,
                                          raw={'extract': None, 'fallback': True})
        cur.execute("""UPDATE sourcing_requests
                          SET conversation=%s::jsonb,
                              last_msg_at=NOW(),
                              last_inbound_at=NOW()
                        WHERE id=%s""",
                    (json.dumps(row['conversation']), request_id))
        db.commit()
        if send_sms and phone:
            send_sms(phone, fallback)
        print(f'[sourcing] extract-failed id={request_id} body={user_text!r}', flush=True)
        return None

    # Merge spec onto the row (mutates row[...]). Also persists relaxations
    # to row['relaxations'] when spec_clear includes a relaxable field.
    changes = merge_spec(row, parsed.get('spec_update') or {},
                        spec_clear=parsed.get('spec_clear') or [])

    # First decision pass — without search results. Tells us if we have
    # enough spec to search or need to ask another question.
    reply, next_state, did_search, branch = _decide(row, parsed, None, user_text)
    # Capture the first-pass branch so the persist gate sees 'recap_confirmed'
    # / 'recap_skipped' even after the search-triggered second pass
    # overwrites `branch` with 'present_matches' / 'no_match_offer'. Without
    # this, recap_confirmed_at never gets persisted and the recap loop
    # fires again on the next turn.
    first_pass_branch = branch

    # Decide if we should run inventory search this turn. Search is gated
    # on:
    # - make + model present
    # - year either set OR explicitly relaxed (relaxations contains 'year')
    # - status hasn't moved off of search-eligible
    # - user intent benefits from search results
    # - AND recap has been confirmed OR user explicitly skipped recap OR
    #   we're already past recap (status='searching'/'presented'). The recap
    #   gate is what makes the bot ask "ok to recap...?" before just dumping
    #   matches.
    relaxed = set(row.get('relaxations') or [])
    recap_authorized = bool(
        row.get('recap_confirmed_at')
        or branch in ('recap_confirmed', 'recap_skipped')
        or first_pass_branch in ('recap_confirmed', 'recap_skipped')
        or row.get('status') in ('searching', 'presented', 'matched', 'wishlist')
    )
    # Year is no longer required to be set OR relaxed — sourcing_search
    # handles missing year by simply not adding a year filter to the
    # WHERE clause. The recap-confirm gate above already prevents
    # premature search; we don't need a separate year_ok check.
    matches = []
    if (row.get('make') and row.get('model') and recap_authorized
        and row.get('status') in ('gathering', 'searching', 'presented',
                                   'matched', 'wishlist')
        and parsed.get('intent') in ('sourcing', 'more_results', 'sort_request',
                                      'confirm_recap', 'skip_recap', 'unclear', None)):
        sort_pref = parsed.get('sort_pref')
        limit = 50 if parsed.get('intent') == 'more_results' else 20
        try:
            matches = inv_search(row, limit=limit, sort_pref=sort_pref)
        except Exception as _se:
            print(f'[sourcing] search error id={request_id}: {_se}', flush=True)
            matches = []
        match_descs = to_match_descs(matches)
        reply, next_state, did_search, branch = _decide(row, parsed, match_descs, user_text)

    # Tone rewrite (only on rewrite-eligible branches).
    final_reply = reply
    if branch in _REWRITE_BRANCHES:
        rewritten = tone_rewrite(reply, row.get('conversation'))
        if rewritten and rewritten.strip():
            final_reply = rewritten.strip()

    # Append bot reply with the raw extract blob attached for debugging.
    row['conversation'] = _append_msg(
        row['conversation'], 'bot', final_reply,
        raw={'extract': parsed, 'branch': branch,
             'rewritten': branch in _REWRITE_BRANCHES,
             'match_count': len(matches) if did_search else None}
    )

    # Persist: spec changes + conversation + status + maybe wishlist_until.
    # Strip fields that get set explicitly below (or need special casts) to
    # avoid Postgres "multiple assignments to same column" errors. status
    # in particular gets set both by merge_spec (on pivot reset) and by us
    # below from next_state — without this filter, the SET clause repeats.
    _SQL_SPECIAL_HANDLED = {
        'status', 'narrative_brief', 'recap_confirmed_at',
        'last_msg_at', 'last_inbound_at', 'last_scan_at',
        'wishlist_until', 'vehicle_interests',
    }
    sql_changes = {k: v for k, v in changes.items() if k not in _SQL_SPECIAL_HANDLED}
    set_clauses, params = _build_spec_update_sql(sql_changes)
    # vehicle_interests is JSONB — needs explicit cast so a JSON-string
    # param doesn't get stored as text. Only emit if it changed this turn.
    if 'vehicle_interests' in changes:
        set_clauses.append("vehicle_interests = %s::jsonb")
        v = changes['vehicle_interests']
        params.append(v if isinstance(v, str) else json.dumps(v))
    set_clauses += [
        "conversation = %s::jsonb",
        "status = %s",
        "last_msg_at = NOW()",
        "last_inbound_at = NOW()",
    ]
    params += [json.dumps(row['conversation']), next_state]
    if did_search:
        set_clauses.append("last_scan_at = NOW()")
    if next_state == 'wishlist':
        set_clauses.append("wishlist_until = COALESCE(wishlist_until, NOW() + INTERVAL '30 days')")
    # When user confirms or skips the recap, lock in the narrative_brief so
    # staff (and the search) have the canonical statement of intent. Once
    # set, the recap gate stays open for this vehicle until a pivot resets
    # both narrative_brief and recap_confirmed_at via merge_spec.
    # We check the FIRST-PASS branch too because the second decide pass
    # (after search) typically replaces the branch with present_matches.
    if (branch in ('recap_confirmed', 'recap_skipped')
        or first_pass_branch in ('recap_confirmed', 'recap_skipped')):
        from sourcing_gemini import build_recap as _bld
        narrative = _bld(row)
        set_clauses.append("narrative_brief = %s")
        params.append(narrative)
        set_clauses.append("recap_confirmed_at = NOW()")
    # User picked "something else" after a no_match_offer. Wipe the prior
    # vehicle spec so the next message starts a clean recap loop rather than
    # inheriting (e.g.) year=2011 from the failed 911 search.
    if branch == 'cold_open_redirect' or first_pass_branch == 'cold_open_redirect':
        for _col in ('make', 'model', 'trim', 'year_min', 'year_max',
                     'miles_max', 'price_hint', 'transmission',
                     'ext_color', 'int_color'):
            set_clauses.append(f"{_col} = NULL")
        set_clauses.append("relaxations = '{}'::text[]")
        set_clauses.append("narrative_brief = NULL")
        set_clauses.append("recap_confirmed_at = NULL")
    sql = f"UPDATE sourcing_requests SET {', '.join(set_clauses)} WHERE id = %s"
    params.append(request_id)
    cur.execute(sql, params)
    db.commit()

    if send_sms and phone and final_reply:
        send_sms(phone, final_reply)

    print(f'[sourcing] turn id={request_id} intent={parsed.get("intent")} '
          f'branch={branch} did_search={did_search} matches={len(matches)} '
          f'next_state={next_state} changes={list(changes.keys())} '
          f'rewritten={branch in _REWRITE_BRANCHES}', flush=True)

    return {
        'intent': parsed.get('intent'),
        'branch': branch,
        'did_search': did_search,
        'match_count': len(matches),
        'next_state': next_state,
    }


# ── Main router ───────────────────────────────────────────────────────────
def try_handle_sourcing(from_phone, body, db, cur, intake_log_id=None,
                         num_media=0, send_sms=None):
    """
    Returns True if this inbound was handled by the sourcing flow.
    Returns False to fall through to the existing bid-reply logic.
    """
    # SOURCING_KILL_SWITCH_2026_05_15: file-flag emergency disable.
    # Touch /tmp/sourcing_disabled to disable instantly; rm to re-enable.
    import os as _os_ks
    if _os_ks.path.exists("/tmp/sourcing_disabled"):
        return False
    if not _phone_allowed(from_phone):
        return False

    # Yield-to-bid-stitch precedence (added 2026-05-10): if this phone has a
    # bid created in the last 60s, the inbound is almost certainly a stitch
    # follow-up (bare mileage, VIN paste, photo) regardless of sourcing
    # state. Bid stitch needs first crack — otherwise sourcing eats the
    # follow-up like it did to bid 1129's "12765".
    if _has_recent_sms_bid(cur, from_phone):
        print(f'[sourcing] yielding to bid intake (recent bid <60s for {from_phone})', flush=True)
        return False

    body_lc = (body or '').strip().lower()
    active = _find_active(cur, from_phone)

    # Stop / drop-it always wins.
    if _handle_stop_or_drop(cur, from_phone, body_lc, active):
        db.commit()
        if send_sms and active:
            send_sms(from_phone, "ok, dropped. text us anytime.")
        print(f'[sourcing] {from_phone} dropped/stopped active={active["id"] if active else None}', flush=True)
        return True

    # 1. Mid-conversation continuation — but yield to bid intake.
    # If the gated phone is mid-sourcing AND fires off a VIN/photo, that's
    # clearly a new bid, not a sourcing reply. Let bid flow handle it.
    if active:
        if _looks_like_bid_intake(body, num_media):
            print(f"[sourcing] yielding to bid intake (VIN/photo) despite active sourcing id={active['id']}", flush=True)
            return False
        row = dict(active)
        _run_turn(db, cur, active['id'], row, body or '',
                  num_media=num_media, send_sms=send_sms,
                  phone=from_phone)
        return True

    # 2. Cold inbound from a gated phone — must be a CLEAR sourcing request.
    # Tightened 2026-05-10: previously any non-bid, non-empty body opened a
    # request, which created ghost rows from "12765" / "ok" / "?" etc.
    # Now requires _is_plausible_sourcing() — at least one alpha word, not
    # pure digits, length >= 4, OR an explicit hint phrase.
    if _looks_like_bid_intake(body, num_media):
        return False
    # Plausibility filter is only needed when the gate is OPEN (anyone can
    # text in). For an explicit whitelist of approved wholesalers, treat
    # every inbound as intentional — even a bare "hello" — and let the
    # cold_open branch greet them and lead them into the sourcing flow.
    if _GATE_OPEN and not _is_plausible_sourcing(body):
        print(f'[sourcing] cold inbound rejected (implausible body): {body!r}', flush=True)
        return False

    # New request — create the row first, then run a Gemini turn against it.
    cur.execute("""INSERT INTO sourcing_requests
                       (phone, status, conversation, last_msg_at, last_inbound_at)
                   VALUES (%s, 'gathering', '[]'::jsonb, NOW(), NOW())
                   RETURNING id""",
                (from_phone,))
    new_id = cur.fetchone()['id']
    db.commit()

    row = {
        'id': new_id,
        'phone': from_phone,
        'status': 'gathering',
        'conversation': [],
    }
    _run_turn(db, cur, new_id, row, body or '',
              num_media=num_media, send_sms=send_sms,
              phone=from_phone)
    print(f'[sourcing] NEW id={new_id} phone={from_phone}', flush=True)
    return True
