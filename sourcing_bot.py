"""
EW sourcing-bot — AI-driven SMS sourcing flow.

Step 3: Gemini turn integration. Each inbound user msg from a gated phone
runs through Gemini-Flash, which returns spec_update + intent + reply +
ready_to_search + next_state. We persist the conversation, merge spec
updates, and update status. Search execution itself (when ready_to_search
fires) lands in step 4 — for now it just transitions to 'searching' and
sends the model's reply (which will be something like "on it, one sec").

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
_ACTIVE_DAYS = 7


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


# ── Main turn handler — reusable for both new + continuing requests ───────
def _run_gemini_and_persist(db, cur, request_id, row, user_text, num_media=0,
                             send_sms=None, phone=None):
    """
    Append user msg → Gemini turn → merge spec + reply + state → send SMS.
    Returns the parsed Gemini response (or None on hard failure).
    """
    from sourcing_gemini import turn as gemini_turn, merge_spec

    # Append user msg into the row (for the prompt build).
    row['conversation'] = _append_msg(row.get('conversation'), 'user', user_text)

    parsed = gemini_turn(row, user_text)
    if not parsed:
        # Fallback ack so the user isn't left hanging.
        fallback = "got it — one sec."
        row['conversation'] = _append_msg(row['conversation'], 'bot', fallback)
        cur.execute("""UPDATE sourcing_requests
                          SET conversation=%s::jsonb,
                              last_msg_at=NOW(),
                              last_inbound_at=NOW()
                        WHERE id=%s""",
                    (json.dumps(row['conversation']), request_id))
        db.commit()
        if send_sms and phone:
            send_sms(phone, fallback)
        print(f'[sourcing] gemini-fallback id={request_id}', flush=True)
        return None

    spec_update = parsed.get('spec_update') or {}
    reply = parsed.get('reply') or "got it."
    next_state = parsed.get('next_state') or row.get('status', 'gathering')
    ready = bool(parsed.get('ready_to_search'))

    # Don't let Gemini hand us an invalid status.
    valid_states = ('gathering', 'searching', 'presented', 'matched',
                    'wishlist', 'archived')
    if next_state not in valid_states:
        next_state = row.get('status', 'gathering')

    # Append bot reply with the raw Gemini blob attached for debugging.
    row['conversation'] = _append_msg(row['conversation'], 'bot', reply,
                                       raw={'gemini': parsed})

    # Merge spec updates into the row (mutates), then build the UPDATE.
    changes = merge_spec(row, spec_update)
    set_clauses, params = _build_spec_update_sql(changes)
    set_clauses += [
        "conversation = %s::jsonb",
        "status = %s",
        "last_msg_at = NOW()",
        "last_inbound_at = NOW()",
    ]
    params += [json.dumps(row['conversation']), next_state]
    sql = f"UPDATE sourcing_requests SET {', '.join(set_clauses)} WHERE id = %s"
    params.append(request_id)
    cur.execute(sql, params)
    db.commit()

    if send_sms and phone and reply:
        send_sms(phone, reply)

    print(f'[sourcing] turn id={request_id} intent={parsed.get("intent")} '
          f'ready={ready} next_state={next_state} '
          f'changes={list(changes.keys())}', flush=True)

    # Step 4: ready_to_search → run inventory search + presentation turn.
    # The interim Gemini reply (e.g. "on it, one sec") was already saved to
    # the conversation but NOT sent — we suppress it and send the
    # presentation result as the single SMS for this inbound.
    if ready:
        try:
            from sourcing_search import search_with_fallback, to_match_descs
            matches, fallback_level = search_with_fallback(row, limit=20)
            if fallback_level != 'exact' and matches:
                print(f"[sourcing] id={request_id} fallback={fallback_level} matches={len(matches)}", flush=True)
        except Exception as _se:
            print(f"[sourcing] search error id={request_id}: {_se}", flush=True)
            matches = []
            fallback_level = 'none' 

        match_descs = to_match_descs(matches)
        from sourcing_gemini import turn as gemini_turn
        parsed2 = gemini_turn(row, "", search_results=match_descs)

        if parsed2:
            reply2 = parsed2.get("reply") or "one sec."
            next_state2 = parsed2.get("next_state") or ("presented" if matches else "gathering")
        else:
            if matches:
                m = match_descs[0]
                reply2 = (f"got 1: {m['year']} {m['model']} "
                          f"{m.get('trim') or ''}, "
                          f"{m.get('ext_color') or '?'} / {m.get('int_color') or '?'}, "
                          f"{int((m.get('mileage') or 0)/1000)}k mi. interested?")
                next_state2 = "presented"
            else:
                reply2 = ("nothing matching in our scans right now. want me "
                          "to flag it and text you the second one shows up?")
                next_state2 = "presented"

        valid_states = ("gathering", "searching", "presented", "matched",
                        "wishlist", "archived")
        if next_state2 not in valid_states:
            next_state2 = "presented" if matches else "gathering"

        # Drop the interim bot turn so user only sees the final presentation.
        if row["conversation"] and row["conversation"][-1].get("role") == "bot":
            row["conversation"] = row["conversation"][:-1]
        row["conversation"] = _append_msg(row["conversation"], "bot", reply2,
                                           raw={"gemini": parsed2,
                                                "match_count": len(matches)})

        if next_state2 == 'wishlist':
            cur.execute(
                "UPDATE sourcing_requests SET conversation=%s::jsonb, status=%s, "
                "last_msg_at=NOW(), last_scan_at=NOW(), "
                "wishlist_until = COALESCE(wishlist_until, NOW() + INTERVAL '30 days') "
                "WHERE id=%s",
                (json.dumps(row["conversation"]), next_state2, request_id),
            )
        else:
            cur.execute(
                "UPDATE sourcing_requests SET conversation=%s::jsonb, status=%s, "
                "last_msg_at=NOW(), last_scan_at=NOW() WHERE id=%s",
                (json.dumps(row["conversation"]), next_state2, request_id),
            )
        db.commit()

        if send_sms and phone:
            send_sms(phone, reply2)

        print(f"[sourcing] presented id={request_id} matches={len(matches)} "
              f"next_state={next_state2}", flush=True)
        parsed["_post_search"] = parsed2
        parsed["_match_count"] = len(matches)

    return parsed


# ── Main router ───────────────────────────────────────────────────────────
def try_handle_sourcing(from_phone, body, db, cur, intake_log_id=None,
                         num_media=0, send_sms=None):
    """
    Returns True if this inbound was handled by the sourcing flow.
    Returns False to fall through to the existing bid-reply logic.
    """
    if not _phone_allowed(from_phone):
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
        _run_gemini_and_persist(db, cur, active['id'], row, body or '',
                                 num_media=num_media, send_sms=send_sms,
                                 phone=from_phone)
        return True

    # 2. Cold inbound from a gated phone.
    if _looks_like_bid_intake(body, num_media):
        return False

    if not _looks_like_sourcing(body):
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
    _run_gemini_and_persist(db, cur, new_id, row, body or '',
                             num_media=num_media, send_sms=send_sms,
                             phone=from_phone)
    print(f'[sourcing] NEW id={new_id} phone={from_phone}', flush=True)
    return True
