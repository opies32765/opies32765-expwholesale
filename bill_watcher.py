"""bill_watcher.py — Phase C: poll new bids vs active bill_watchlists.

Architecture:
  - Loops every WATCH_INTERVAL seconds
  - Pulls bids with id > last_seen_bid_id AND NOT in already-matched
  - Evaluates each new bid against every active bill_watchlists row
  - On match, inserts into bill_watchlist_hits (UNIQUE prevents dupe)
  - The web layer queries bill_watchlist_hits WHERE notified_at IS NULL
    to expose pending notifications to the voice agent / Telegram fallback.

State:
  - last_seen_bid_id is persisted in a tiny key/value row in the DB so the
    watcher survives restarts without re-firing on already-processed bids.
"""
import os, sys, time, json, logging, signal, traceback
import psycopg2, psycopg2.extras

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [watcher] %(message)s")
log = logging.getLogger("bill_watcher")

DB_URL = os.environ.get("DATABASE_URL",
    "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
WATCH_INTERVAL = int(os.environ.get("WATCH_INTERVAL_SEC", "30"))
STATE_KEY = "bill_watcher_last_bid_id"

_running = True


def _connect():
    return psycopg2.connect(DB_URL)


def _bootstrap_state():
    """Create state table if not exists. Set initial cursor to current max
    bid id so we don't fire on historical bids on first start."""
    with _connect() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bill_watcher_state (
              key   TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("SELECT value FROM bill_watcher_state WHERE key=%s",
                    (STATE_KEY,))
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM bids")
            max_id = cur.fetchone()[0]
            cur.execute("""INSERT INTO bill_watcher_state (key, value)
                           VALUES (%s, %s)""", (STATE_KEY, str(max_id)))
            log.info(f"initialized state at bid_id={max_id}")
            return max_id
        log.info(f"resuming from bid_id={row[0]}")
        return int(row[0])


def _save_state(last_id):
    with _connect() as c, c.cursor() as cur:
        cur.execute("""UPDATE bill_watcher_state
                          SET value=%s, updated_at=NOW()
                        WHERE key=%s""", (str(last_id), STATE_KEY))


def _load_active_watchlists():
    with _connect() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT id, created_by, name, description,
                                  conditions, pitch_for
                             FROM bill_watchlists
                            WHERE active = TRUE""")
            return [dict(r) for r in cur.fetchall()]


def _load_new_bids(since_id):
    with _connect() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT id, year, make, model, trim, mileage,
                                  color, vin, ai_price, asking_price, status,
                                  created_at
                             FROM bids
                            WHERE id > %s
                              AND COALESCE(status, '') NOT IN
                                  ('cancelled','archived','dead','duplicate')
                            ORDER BY id ASC
                            LIMIT 200""", (since_id,))
            return [dict(r) for r in cur.fetchall()]


def _matches(bid, conds):
    """Return True if bid satisfies all conditions in conds (jsonb)."""
    def low(s):
        return (s or "").strip().lower()
    if (ma := conds.get("make_any")) and low(bid.get("make")) not in ma:
        return False
    if (mo := conds.get("model_any")):
        bm = low(bid.get("model"))
        # Allow loose contains (e.g. "Corvette" matches "Corvette Stingray")
        if not any(low(x) in bm or bm in low(x) for x in mo):
            return False
    if (co := conds.get("color_any")) and bid.get("color"):
        if low(bid.get("color")) not in co:
            return False
    elif conds.get("color_any") and not bid.get("color"):
        # bid has no color recorded — be lenient, allow (color often missing)
        pass
    yr = bid.get("year")
    if yr is None:
        if conds.get("year_min") or conds.get("year_max") or conds.get("year_exact"):
            return False
    else:
        if (y := conds.get("year_exact")) is not None and int(yr) != int(y):
            return False
        if (y := conds.get("year_min")) is not None and int(yr) < int(y):
            return False
        if (y := conds.get("year_max")) is not None and int(yr) > int(y):
            return False
    if (mm := conds.get("mileage_max")) is not None:
        m = bid.get("mileage")
        if m is not None and int(m) > int(mm):
            return False
    if (pm := conds.get("price_max")) is not None:
        for k in ("ai_price", "asking_price"):
            v = bid.get(k)
            if v is not None:
                try:
                    if float(v) > float(pm):
                        return False
                except Exception:
                    pass
                break
    if (tc := conds.get("trim_contains")):
        if low(tc) not in low(bid.get("trim")):
            return False
    return True


def _format_message(bid, watch):
    parts = []
    parts.append("Heads up — that " if watch.get("pitch_for") else "Heads up — your ")
    year = bid.get("year")
    make = (bid.get("make") or "").title()
    model = bid.get("model") or ""
    trim = bid.get("trim") or ""
    miles = bid.get("mileage")
    ai = bid.get("ai_price")
    parts.append(f"{year} {make} {model}".strip())
    if trim:
        parts.append(f" {trim}".rstrip())
    parts.append(" you wanted just hit. ")
    parts.append(f"Bid {bid['id']}")
    if miles:
        parts.append(f", {int(miles):,} miles")
    if ai:
        try:
            parts.append(f", AI marked it at ${int(float(ai)):,}")
        except Exception:
            pass
    parts.append(".")
    if watch.get("pitch_for"):
        parts.append(f" That's for {watch['pitch_for']}.")
    return "".join(parts)


def _record_hit(watchlist_id, bid_id, message):
    """Insert a hit row. UNIQUE (watchlist_id, bid_id) prevents dupes."""
    with _connect() as c, c.cursor() as cur:
        try:
            cur.execute("""INSERT INTO bill_watchlist_hits
                            (watchlist_id, bid_id, matched_at, message)
                           VALUES (%s, %s, NOW(), %s)
                           ON CONFLICT (watchlist_id, bid_id) DO NOTHING
                           RETURNING id""", (watchlist_id, bid_id, message))
            row = cur.fetchone()
            if row is None:
                return None  # dupe
            hit_id = row[0]
            cur.execute("""UPDATE bill_watchlists
                              SET match_count = match_count + 1,
                                  last_matched_at = NOW(),
                                  updated_at = NOW()
                            WHERE id = %s""", (watchlist_id,))
            return hit_id
        except Exception as e:
            log.exception(f"_record_hit failed: {e}")
            return None




def _get_user_away_state(user):
    """Return (channel, phone) if user is currently away, else (None, None)."""
    with _connect() as c, c.cursor() as cur:
        cur.execute("""SELECT away_channel, away_phone, away_until
                         FROM bill_user_state
                        WHERE user_name = %s
                          AND away_channel IS NOT NULL
                          AND (away_until IS NULL OR away_until > NOW())""",
                    (user.lower(),))
        row = cur.fetchone()
        if not row:
            return None, None
        return row[0], row[1]


def _dispatch_outbound_call(phone, message, hit_id):
    """Place an outbound Twilio call that reads the message via TwiML."""
    import os as _os
    sid = _os.environ.get("TWILIO_ACCOUNT_SID")
    tok = _os.environ.get("TWILIO_AUTH_TOKEN")
    frm = _os.environ.get("TWILIO_PHONE")
    if not (sid and tok and frm):
        log.warning(f"twilio env missing — cannot dispatch call for hit {hit_id}")
        return False
    try:
        from twilio.rest import Client
        client = Client(sid, tok)
        # Inline TwiML — speaks the message twice (so user can catch it
        # mid-pickup), then hangs up.
        import xml.sax.saxutils as _x
        safe = _x.escape(message)
        twiml = (
            f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            f"<Response>"
            f"<Pause length=\"1\"/>"
            f"<Say voice=\"alice\">{safe}</Say>"
            f"<Pause length=\"1\"/>"
            f"<Say voice=\"alice\">Again: {safe}</Say>"
            f"</Response>"
        )
        call = client.calls.create(to=phone, from_=frm, twiml=twiml)
        log.info(f"  CALL placed sid={call.sid} to={phone} hit_id={hit_id}")
        return True
    except Exception as e:
        log.exception(f"twilio call failed for hit {hit_id}: {e}")
        return False


def _dispatch_sms(phone, message, hit_id):
    import os as _os
    sid = _os.environ.get("TWILIO_ACCOUNT_SID")
    tok = _os.environ.get("TWILIO_AUTH_TOKEN")
    frm = _os.environ.get("TWILIO_PHONE")
    if not (sid and tok and frm):
        log.warning(f"twilio env missing — cannot SMS for hit {hit_id}")
        return False
    try:
        from twilio.rest import Client
        client = Client(sid, tok)
        msg = client.messages.create(to=phone, from_=frm, body=message)
        log.info(f"  SMS sent sid={msg.sid} to={phone} hit_id={hit_id}")
        return True
    except Exception as e:
        log.exception(f"twilio sms failed for hit {hit_id}: {e}")
        return False


def _dispatch_notification(hit_id, user, message):
    """If user is in away mode, dispatch via the configured channel and
    mark notified_at. If not away, leave for /v-edge poller to grab."""
    channel, phone = _get_user_away_state(user)
    if not channel:
        return  # not away; voice_session poller will pick it up
    ok = False
    if channel == "call":
        ok = _dispatch_outbound_call(phone, message, hit_id)
    elif channel == "sms":
        ok = _dispatch_sms(phone, message, hit_id)
    else:
        log.warning(f"unknown channel {channel!r}; falling back to voice_session")
        return
    if ok:
        with _connect() as c, c.cursor() as cur:
            cur.execute("""UPDATE bill_watchlist_hits
                              SET notified_at = NOW(),
                                  notify_via = %s
                            WHERE id = %s""", (channel, hit_id))


def tick(last_id):
    bids = _load_new_bids(last_id)
    if not bids:
        return last_id
    watches = _load_active_watchlists()
    log.info(f"tick: {len(bids)} new bids vs {len(watches)} active watches")
    max_id = last_id
    for bid in bids:
        if bid["id"] > max_id:
            max_id = bid["id"]
        for w in watches:
            conds = w.get("conditions") or {}
            try:
                if _matches(bid, conds):
                    msg = _format_message(bid, w)
                    hit_id = _record_hit(w["id"], bid["id"], msg)
                    if hit_id:
                        log.info(
                            f"  HIT watchlist_id={w['id']} bid_id={bid['id']} "
                            f"hit_id={hit_id} created_by={w['created_by']} "
                            f"msg={msg!r}")
                        _dispatch_notification(hit_id, w["created_by"], msg)
            except Exception as e:
                log.exception(f"match error for w={w['id']} bid={bid['id']}: {e}")
    if max_id > last_id:
        _save_state(max_id)
    return max_id


def _sigterm(*_):
    global _running
    log.info("SIGTERM — shutting down")
    _running = False


# --- bid_alerts pass (added 2026-06-03) -------------------------------------
# "Text me when a [year][make][model] hits the bid listing."
# Independent of the bill_watchlists cursor: rescans the last
# BID_ALERT_WINDOW_MIN of status='new' bids each tick and relies on the
# bid_alert_hits UNIQUE(alert_id,bid_id) to fire EXACTLY once per (alert,bid).
# Only fires for bids that arrived at/after the alert was created (no
# retroactive texts). Gated to the operator cell via gate_type='bid_alert'.
BID_ALERT_WINDOW_MIN = int(os.environ.get("BID_ALERT_WINDOW_MIN", "30"))
_BID_ALERT_GATE = {"ts": 0.0, "digits": set()}


def _bid_alert_gate_digits():
    # OPENGATE_2026_07_07: open to every caller by default (operator sign-off
    # 2026-07-07 — "if its gated open it for everyone and anyone"). Set
    # BID_ALERT_GATE_DIGITS to a comma/space list to lock it back down to
    # specific numbers for testing; leave unset/"*" for open access.
    raw = os.environ.get("BID_ALERT_GATE_DIGITS", "*").strip()
    if raw == "*" or raw == "":
        return None  # None = allow all
    out = set()
    for tok in raw.replace(",", " ").split():
        d = "".join(ch for ch in tok if ch.isdigit())
        if len(d) >= 10:
            out.add(d[-10:])
    return out


def _load_active_bid_alerts():
    with _connect() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, created_by, notify_phone, phone_digits, make, model, trim_contains, year_min, year_max, price_max, label, created_at FROM bid_alerts WHERE active = TRUE")
            return [dict(r) for r in cur.fetchall()]


def _load_recent_bids_for_alerts():
    with _connect() as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, COALESCE(canon_year, year) AS year,"
                " COALESCE(canon_make, make) AS make,"
                " COALESCE(canon_model, model) AS model,"
                " COALESCE(canon_trim, trim) AS trim,"
                " mileage, ai_price, asking_price, created_at"
                " FROM bids WHERE status = 'new'"
                " AND created_at > NOW() - (%s || ' minutes')::interval"
                " AND COALESCE(canon_make, make) IS NOT NULL"
                # ALERT_AFTER_ENRICH_2026_07_04: text the rep only after the
                # bid is fully enriched + assessed (ai fields set = all legs
                # landed and YMM reconciled). 10-min fallback so a stuck
                # assessment can never silently kill alerts.
                " AND (ai_price IS NOT NULL OR ai_assessed_at IS NOT NULL"
                "      OR created_at < NOW() - INTERVAL '10 minutes')"
                " ORDER BY id ASC LIMIT 500", (str(BID_ALERT_WINDOW_MIN),))
            return [dict(r) for r in cur.fetchall()]


def _alert_to_conds(a):
    conds = {}
    if a.get("make"):
        conds["make_any"] = [str(a["make"]).strip().lower()]
    if a.get("model"):
        conds["model_any"] = [str(a["model"]).strip().lower()]
    if a.get("year_min") is not None:
        conds["year_min"] = a["year_min"]
    if a.get("year_max") is not None:
        conds["year_max"] = a["year_max"]
    if a.get("price_max") is not None:
        conds["price_max"] = a["price_max"]
    if a.get("trim_contains"):
        conds["trim_contains"] = a["trim_contains"]
    return conds


def _format_alert_message(bid, a):
    want = " ".join(str(x) for x in [a.get("make"), a.get("model")] if x).strip()
    y = bid.get("year") or ""
    mk = bid.get("make") or ""
    if mk:
        mk = str(mk).title()
    md = bid.get("model") or ""
    tr = bid.get("trim") or ""
    veh = " ".join(str(x) for x in [y, mk, md, tr] if x).strip()
    msg = ("Hey, it's Bill at Experience Wholesale - you asked me to flag any "
           + want + ". One just hit our bid dashboard: Bid #" + str(bid["id"])
           + ", a " + veh)
    miles = bid.get("mileage")
    if miles:
        try:
            msg = msg + " with " + format(int(miles), ",") + " miles"
        except Exception:
            pass
    msg = msg + ". https://experience-wholesale.net/bid/" + str(bid["id"])
    return msg


def _claim_alert_hit(alert_id, bid_id, message):
    with _connect() as c, c.cursor() as cur:
        try:
            cur.execute("INSERT INTO bid_alert_hits (alert_id, bid_id, message) VALUES (%s, %s, %s) ON CONFLICT (alert_id, bid_id) DO NOTHING RETURNING id",
                        (alert_id, bid_id, message))
            row = cur.fetchone()
            return row[0] if row else None
        except Exception as e:
            log.exception("_claim_alert_hit failed: %s" % e)
            return None


def _mark_alert(alert_id, bid_id, notified, skip_reason=None):
    with _connect() as c, c.cursor() as cur:
        if notified:
            cur.execute("UPDATE bid_alert_hits SET notified_at=NOW() WHERE alert_id=%s AND bid_id=%s", (alert_id, bid_id))
            cur.execute("UPDATE bid_alerts SET match_count=match_count+1, last_matched_at=NOW(), updated_at=NOW() WHERE id=%s", (alert_id,))
        else:
            cur.execute("UPDATE bid_alert_hits SET skip_reason=%s WHERE alert_id=%s AND bid_id=%s", (skip_reason, alert_id, bid_id))


def _scan_bid_alerts():
    alerts = _load_active_bid_alerts()
    if not alerts:
        return
    bids = _load_recent_bids_for_alerts()
    if not bids:
        return
    gate = _bid_alert_gate_digits()
    for bid in bids:
        bcreated = bid.get("created_at")
        for a in alerts:
            acreated = a.get("created_at")
            # TZFIX_2026_07_04: bids.created_at is naive, bid_alerts.created_at is
            # timestamptz — comparing raised TypeError and killed EVERY scan.
            if bcreated and acreated:
                _b = bcreated.replace(tzinfo=None) if getattr(bcreated, "tzinfo", None) else bcreated
                _a = acreated.replace(tzinfo=None) if getattr(acreated, "tzinfo", None) else acreated
                if _b < _a:
                    continue
            try:
                if not _matches(bid, _alert_to_conds(a)):
                    continue
            except Exception as e:
                log.exception("alert match error a=%s bid=%s: %s" % (a.get("id"), bid.get("id"), e))
                continue
            body = _format_alert_message(bid, a)
            hit_id = _claim_alert_hit(a["id"], bid["id"], body)
            if not hit_id:
                continue
            if gate is not None and a.get("phone_digits") not in gate:
                _mark_alert(a["id"], bid["id"], False, "gated_off")
                log.info("  BID_ALERT gated_off alert=%s bid=%s phone=%s" % (a["id"], bid["id"], a.get("notify_phone")))
                continue
            ok = _dispatch_sms(a["notify_phone"], body, hit_id)
            if ok:
                _mark_alert(a["id"], bid["id"], True)
                log.info("  BID_ALERT sent alert=%s bid=%s to=%s" % (a["id"], bid["id"], a.get("notify_phone")))
            else:
                _mark_alert(a["id"], bid["id"], False, "send_sms_false")


def main():
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    last_id = _bootstrap_state()
    log.info(f"bill_watcher up. interval={WATCH_INTERVAL}s last_bid_id={last_id}")
    while _running:
        try:
            last_id = tick(last_id)
        except Exception:
            log.exception("tick failed")
        try:
            _scan_bid_alerts()
        except Exception:
            log.exception("bid_alerts scan failed")
        for _ in range(WATCH_INTERVAL):
            if not _running:
                break
            time.sleep(1)
    log.info("watcher exited cleanly")


if __name__ == "__main__":
    main()
