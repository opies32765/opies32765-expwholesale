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
_GATE = os.getenv('SOURCING_PHONE_GATE', '+14074309675')
_GATED_PHONES = {p.strip() for p in _GATE.split(',') if p.strip()}
_GATE_OPEN = '*' in _GATED_PHONES
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
    if _GATE_OPEN:
        return True
    return phone in _GATED_PHONES


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
    """Format the top 3 matches + a follow-up question.

    When the result set is >3 AND we don't yet have a price_hint, add a
    budget ask to the trailer so the user can narrow by price (which sorts
    by proximity to price_hint) instead of paginating through everything.
    Per never-quote-prices rule we don't show prices in the matches; the
    budget hint is used as a server-side sort key only."""
    body = '\n'.join(_format_match(m) for m in matches[:3])
    n = len(matches)
    if n > 3:
        if not price_hint_set:
            return (f"{body}\ngot {n} total — any budget to narrow down, "
                    f"or want to see the rest?")
        return f"{body}\ngot {n} total — want to see the rest?"
    if n > 1:
        return f"{body}\nany of these work?"
    return f"{body}\ninterested?"


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
)


def _mentions_price_question(text):
    """Cheap regex check for 'how much'/'what's the cost'/etc. in user text.
    Used to fold a pricing note into handoff replies when the user asked
    about price in the same message that signaled interest."""
    if not text:
        return False
    import re as _re
    s = (text or '').strip().lower()
    for pat in _PRICE_QUESTION_PATTERNS:
        if _re.search(pat, s):
            return True
    return False


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
_REWRITE_BRANCHES = {
    'gathering_make_model',
    'gathering_model_for_make',
    'gathering_year',
    'gathering_extras',
    'no_match_offer',
    'ambiguous_trim_probe',
    'interested_no_name',
    'interested_no_name_price',
    'more_details_offer',
    'callback_name_ask',
    'extend_wishlist_no_name',
    'more_details_declined',
    're_engagement',
}
# Branches whose wording is intentionally exact (recap text, time-aware
# acks, match presentations, stop/drop) bypass _REWRITE_BRANCHES — see
# _run_turn for the gate.


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

    # Stop / drop intents — deterministic, no rewrite.
    if intent == 'stop':
        return ("ok, stopped. text us anytime.", 'archived', False, 'stop')
    if intent == 'drop_it':
        return ("ok, dropped. text us anytime.", 'archived', False, 'drop')

    # Re-engagement: known matched/wishlist customer comes back with a
    # greeting or unclear short message ("hi", "hello", "any updates",
    # "anything new"). Cold fallback would read robotic for a returning
    # customer. If they haven't texted in >24h AND we have a narrative_brief
    # on the row, prime the convo with the last vehicle context so they
    # don't have to re-explain themselves.
    if (intent == 'unclear' and has_name
        and status in ('matched', 'wishlist')
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

    # ── Callback flow (3-step) ────────────────────────────────────────────
    # Step 3: User just gave name after we asked for it in callback flow.
    if last_branch == 'callback_name_ask' and has_name and intent in ('name_provided', 'sourcing', 'unclear'):
        return (callback_ack(row['customer_name']), 'matched', False, 'callback_ack')

    # Step 2: User said yes to "want me to have someone reach out?".
    if last_branch == 'more_details_offer' and intent == 'callback_yes':
        if has_name:
            return (callback_ack(row['customer_name']), 'matched', False, 'callback_ack')
        return ("what's your name so we know who to reach out to?",
                'presented', False, 'callback_name_ask')

    # User declined the callback offer.
    if last_branch == 'more_details_offer' and intent == 'not_interested':
        return ("ok — let me know if there's something else you want to look at.",
                'presented', False, 'more_details_declined')

    # Step 1: User asked WHERE / MSRP / options / details of a match.
    # If we already have their name (matched/wishlist customer asking about
    # a NEW vehicle), skip the "want me to reach out?" offer entirely and
    # send the time-aware ack directly. They've already opted in once;
    # making them re-confirm for every car is friction.
    if intent == 'more_details':
        if has_name:
            return (callback_ack(row['customer_name']), 'matched', False, 'callback_ack')
        return ("honestly not sure on that one — want me to have someone reach out with the details?",
                'presented', False, 'more_details_offer')

    # Name capture after an interest signal — uses handoff_line, not callback_ack.
    if intent == 'name_provided' and has_name:
        return (handoff_line(row['customer_name']), 'matched', False, 'name_provided')

    # Wishlist save.
    if intent == 'extend_wishlist':
        desc = _short_desc(row)
        if has_name:
            return (f"done, {name_first}. saved: {desc}. text 'drop it' anytime.",
                    'wishlist', False, 'extend_wishlist_with_name')
        return (f"done. saved: {desc}. text 'drop it' anytime. and what's your name so we know who to text?",
                'wishlist', False, 'extend_wishlist_no_name')

    # User signaled interest in a specific match.
    if intent == 'interested':
        if has_name:
            # If the same message asked about price ("yes how much"),
            # acknowledge it explicitly in the handoff so the user doesn't
            # feel ignored on that beat. Per the never-quote-prices rule we
            # don't share a number — staff covers pricing on callback.
            if _mentions_price_question(user_text):
                base = handoff_line(row['customer_name'])
                return (f"{base} pricing's not something we quote here — staff will cover it when they reach out.",
                        'matched', False, 'interested_with_name_price')
            return (handoff_line(row['customer_name']), 'matched', False, 'interested_with_name')
        if _mentions_price_question(user_text):
            return ("on pricing — that's not something we share here, but if you want it we'll have staff reach out with the full picture. what's your name so we know who to text?",
                    'presented', False, 'interested_no_name_price')
        return ("if you want it we'll get back to you with all the details. what's your name so we know who to text?",
                'presented', False, 'interested_no_name')

    # Ambiguous bare-trim pivot — ask before searching. Wording chosen so
    # a bare "yes" reads as "still {make}" (the safe default), not as
    # "open to other makes" (which would clear the make and force the user
    # to re-specify everything). Earlier wording "still {make}, or open to
    # other makes?" tripped the extractor into clearing make on "yes".
    if _is_ambiguous_trim_pivot(row, parsed, user_text):
        return (f"still {row['make']}? if not, what make?",
                status, False, 'ambiguous_trim_probe')

    # Spec-gathering ladder. No "got it" prefix — the rewrite layer can vary
    # the opener; users complained about repetitive acks across consecutive
    # turns. Bare questions read tighter.
    if not has_make:
        return ("what make and model are you looking at?",
                'gathering', False, 'gathering_make_model')
    if not has_model:
        # Use the taxonomy view to suggest the top models we actually carry
        # for this make. Beats a generic "what porsche model?" — the user
        # sees the menu instantly. Fallback to bare question if lookup fails.
        try:
            from sourcing_search import models_for_make
            top = models_for_make(row['make'], limit=6)
            if top:
                names = ', '.join(m['model'] for m in top[:5])
                more = ' (+more)' if len(top) > 5 else ''
                return (f"what {row['make']} model? we've got {names}{more}.",
                        'gathering', False, 'gathering_model_for_make')
        except Exception as _e:
            print(f'[sourcing] models_for_make lookup err: {_e}', flush=True)
        return (f"what {row['make']} model?",
                'gathering', False, 'gathering_model_for_make')
    if not has_year:
        return ("what year range?",
                'gathering', False, 'gathering_year')

    # ── Recap flow ────────────────────────────────────────────────────────
    # After the spec-gathering ladder is satisfied (make+model+year all set
    # or relaxed), the bot probes for granular detail (trim, color,
    # transmission, miles, budget) and REPLAYS the spec back for confirm.
    # Only after explicit "yes" do we run search.
    #
    # CRITICAL: this whole block is gated on `search_results is None` —
    # i.e. only fires on the FIRST _decide pass (before search has run).
    # On the second pass (caller fed us match results), we skip this and
    # fall through to the present_matches / no_match_offer branches below.
    # Without this gate, intent='confirm_recap' would loop back to the
    # "locked in..." interim message even with results in hand, and the
    # user would never see the matches (the bug observed on bid 27 / SF90).
    recap_done = bool(row.get('recap_confirmed_at'))

    if not recap_done and search_results is None:
        # Step 1: ask the omnibus extras question (one chance, marked by
        # last_branch == 'gathering_extras' so we don't re-ask).
        if last_branch not in ('gathering_extras', 'recap_pending'):
            return ("any preference on trim, color, transmission, max miles, "
                    "or budget? or just want to see what we have?",
                    'gathering', False, 'gathering_extras')

        # Step 2: user explicitly bypassed the recap.
        if intent == 'skip_recap':
            return (f"got it — {build_recap(row)}. searching now.",
                    'searching', False, 'recap_skipped')

        # Step 3: user confirmed the recap. Return the interim "locked in"
        # message; _run_turn then runs search and re-enters _decide with
        # results, which falls through to present_matches below.
        if intent == 'confirm_recap' and last_branch == 'recap_pending':
            return (f"locked in — {build_recap(row)}. one sec.",
                    'searching', False, 'recap_confirmed')

        # Step 4: user just answered the extras question OR is correcting a
        # prior recap → build/rebuild the recap and ask to confirm.
        return (f"ok to recap — {build_recap(row)}. sound right?",
                'gathering', False, 'recap_pending')

    # Sort request — return the existing search results re-sorted.
    if intent == 'sort_request' and search_results:
        return (_present_results(search_results, price_hint_set=bool(row.get('price_hint'))),
                'presented', True, 'present_matches_sorted')

    # "Show me the rest" / "list them all". Twilio drops/delays SMS that span
    # too many segments (>10 segments = ~1600 chars often fails on carrier
    # delivery), so cap at 10 vehicles per SMS and tell the user how many
    # remain. They can narrow down rather than chase a 50-row list.
    if intent == 'more_results' and search_results:
        n = len(search_results)
        cap = 10
        body = '\n'.join(_format_match(m) for m in search_results[:cap])
        if n > cap:
            tail = (f"\nthat's {cap} of {n}. narrow down by year, color, or miles "
                    f"and i'll show the rest?")
            return (body + tail, 'presented', True, 'present_all')
        if n > 1:
            return (f"{body}\nany of these?", 'presented', True, 'present_all')
        return (f"{body}\ninterested?", 'presented', True, 'present_all')

    # Hard minimums met — search and present.
    if search_results is not None:
        if search_results:
            return (_present_results(search_results, price_hint_set=bool(row.get('price_hint'))),
                    'presented', True, 'present_matches')
        # 0 matches: check inventory_taxonomy for sibling models in same make,
        # so we can offer concrete alternatives rather than just "different model?".
        siblings = []
        try:
            from sourcing_search import models_for_make
            cur_model = (row.get('model') or '').lower()
            siblings_full = models_for_make(row.get('make'), limit=8)
            siblings = [m['model'] for m in siblings_full
                        if m.get('model') and m['model'].lower() != cur_model][:4]
        except Exception as _e:
            print(f'[sourcing] sibling models lookup err: {_e}', flush=True)
        return (_no_match_offer_relax_aware(row, siblings),
                'presented', True, 'no_match_offer')

    # Catch-all: extractor didn't surface a clear intent and we couldn't
    # search either. Don't go silent — acknowledge + open-ended question
    # so the user knows they were heard. Was 'got it.' which read robotic.
    return ("not sure i caught that — what are you looking for?",
            status, False, 'fallback')


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
    year_ok = bool(row.get('year_min') or row.get('year_max') or 'year' in relaxed)
    recap_authorized = bool(
        row.get('recap_confirmed_at')
        or branch in ('recap_confirmed', 'recap_skipped')
        or row.get('status') in ('searching', 'presented', 'matched', 'wishlist')
    )
    # Search-eligible statuses include 'matched' and 'wishlist' — once a
    # user is at handoff or on the wishlist, they should still be able to
    # refine ("any black sf90 instead?") and get fresh results, not get
    # frozen at fallback. Status moves back to 'presented' downstream when
    # the new result set is delivered. Without 'matched'/'wishlist' here,
    # any post-handoff refinement falls to "not sure i caught that".
    matches = []
    if (row.get('make') and row.get('model') and year_ok and recap_authorized
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
    set_clauses, params = _build_spec_update_sql(changes)
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
    if branch in ('recap_confirmed', 'recap_skipped'):
        from sourcing_gemini import build_recap as _bld
        narrative = _bld(row)
        set_clauses.append("narrative_brief = %s")
        params.append(narrative)
        set_clauses.append("recap_confirmed_at = NOW()")
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
    if not _is_plausible_sourcing(body):
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
