"""bill_daily_briefing.py — proactive morning briefing for Bill.

Runs from cron (Mon-Fri 8:00 AM ET) OR on demand via the briefing_now
MCP tool. Pulls structured data, renders a short spoken paragraph
(target 60-90 seconds = ~150-250 words), and queues it for the
/v-edge poller. If user is in away mode, dispatches via that channel
using the same logic as bill_watcher.py.

Sentinel routing:
  bill_watchlists row id=3 (name='Daily Briefing', active=FALSE) is the
  routing target. The existing /api/ew-voice/pending endpoint reads
  bill_watchlist_hits and JOINs against bill_watchlists to scope by user.
  We insert with watchlist_id=3, bid_id=0 (no FK to bids).

Number style: spoken English per /opt/expwholesale/c3_voice_agent/system_prompt.txt
  #1 RULE — drop trailing zeros, no "K", no "$", no commas, no chunked digits.

Usage:
  python3 bill_daily_briefing.py                 # generate + insert + dispatch
  python3 bill_daily_briefing.py --dry-run       # print only, no DB writes
  python3 bill_daily_briefing.py --user oscar    # alt user (default oscar)
  python3 bill_daily_briefing.py --for-date 2026-05-20  # historical (debug)
"""
import argparse, logging, os, sys, traceback
from datetime import datetime, date, timedelta
import psycopg2
import psycopg2.extras

LOG_FILE = "/var/log/bill-briefing.log"
SENTINEL_WATCHLIST_NAME = "Daily Briefing"

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [briefing] %(message)s",
)
log = logging.getLogger("bill_briefing")


# ─── spoken-english number formatter ──────────────────────────────────
# Mirrors the #1 RULE — never digits, never K, never $, drop trailing zeros.

_UNDER_TWENTY = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]


def _say_under_thousand(n: int) -> str:
    if n == 0:
        return ""
    if n < 20:
        return _UNDER_TWENTY[n]
    if n < 100:
        t, o = divmod(n, 10)
        if o == 0:
            return _TENS[t]
        return f"{_TENS[t]}-{_UNDER_TWENTY[o]}"
    h, rem = divmod(n, 100)
    if rem == 0:
        return f"{_UNDER_TWENTY[h]} hundred"
    return f"{_UNDER_TWENTY[h]} hundred {_say_under_thousand(rem)}"


def say_money(n) -> str:
    """Convert a dollar amount (int/float/Decimal) to spoken English.
    Drops trailing zeros per the #1 RULE."""
    if n is None:
        return "unknown"
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return "unknown"
    if n < 0:
        return "negative " + say_money(-n)
    if n == 0:
        return "zero"
    millions, rem = divmod(n, 1_000_000)
    thousands, rest = divmod(rem, 1_000)
    parts = []
    if millions:
        parts.append(f"{_say_under_thousand(millions)} million")
    if thousands:
        parts.append(f"{_say_under_thousand(thousands)} thousand")
    if rest:
        parts.append(_say_under_thousand(rest))
    return " ".join(parts).strip()


def say_int(n) -> str:
    """Plain spoken int (for counts, miles, etc.)."""
    if n is None:
        return "unknown"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "unknown"
    if n < 0:
        return "negative " + say_int(-n)
    if n == 0:
        return "zero"
    millions, rem = divmod(n, 1_000_000)
    thousands, rest = divmod(rem, 1_000)
    parts = []
    if millions:
        parts.append(f"{_say_under_thousand(millions)} million")
    if thousands:
        parts.append(f"{_say_under_thousand(thousands)} thousand")
    if rest:
        parts.append(_say_under_thousand(rest))
    return " ".join(parts).strip()


# ─── data gathering ───────────────────────────────────────────────────

def _connect():
    return psycopg2.connect(DB_URL)


def _safe_make(s: str) -> str:
    """Normalize an all-caps make like 'MERCEDES-BENZ' to 'Mercedes-Benz'."""
    if not s:
        return ""
    parts = s.replace("_", " ").split("-")
    cleaned = [p.strip().title() for p in parts]
    return "-".join(cleaned)


def _safe_model(s: str) -> str:
    if not s:
        return ""
    return s.strip()


_REGION_NAMES = {
    "NE": "the Northeast",
    "MA": "the Mid-Atlantic",
    "SE": "the Southeast",
    "MW": "the Midwest",
    "SW": "the Southwest",
    "MT": "the Mountain region",
    "PNW": "the Pacific Northwest",
    "PSW": "the Pacific Southwest",
    "W": "the West Coast",
    "WC": "the West Coast",
}


def _say_region(code: str) -> str:
    """Convert census-style region codes to spoken names."""
    if not code:
        return ""
    return _REGION_NAMES.get(code.strip().upper(), code.strip())


def gather(target_date: date) -> dict:
    """Pull all data sources. Returns a dict for rendering."""
    out = {"target_date": target_date.isoformat()}
    with _connect() as c, c.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor) as cur:

        # 1. Bids in last 14 hours (overnight east-coast window).
        cur.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(DISTINCT NULLIF(make,'')) AS makes
              FROM bids
             WHERE created_at > NOW() - INTERVAL '14 hours'
               AND COALESCE(status,'') NOT IN
                   ('cancelled','archived','dead','duplicate')
        """)
        row = cur.fetchone()
        out["overnight_total"] = int(row["total"] or 0)
        out["overnight_makes"] = int(row["makes"] or 0)

        # Top makes overnight (skip blank).
        cur.execute("""
            SELECT make, COUNT(*) AS n
              FROM bids
             WHERE created_at > NOW() - INTERVAL '14 hours'
               AND COALESCE(make,'') <> ''
               AND COALESCE(status,'') NOT IN
                   ('cancelled','archived','dead','duplicate')
             GROUP BY make
             ORDER BY n DESC
             LIMIT 3
        """)
        out["overnight_top_makes"] = [dict(r) for r in cur.fetchall()]

        # Top 3 bids by ai_price overnight.
        cur.execute("""
            SELECT id, year, make, model, trim, mileage, ai_price
              FROM bids
             WHERE created_at > NOW() - INTERVAL '14 hours'
               AND COALESCE(status,'') NOT IN
                   ('cancelled','archived','dead','duplicate')
               AND ai_price IS NOT NULL
             ORDER BY ai_price DESC
             LIMIT 3
        """)
        out["overnight_top_bids"] = [dict(r) for r in cur.fetchall()]

        # 3. Dealer-watch opportunities — latest snapshot, top 3 by score.
        cur.execute("""
            SELECT o.year, o.make, o.model, o.trim, o.asking_price,
                   o.dollars_under_mmr, o.score, d.name AS dealer_name,
                   o.snapshot_date
              FROM dealer_opportunities o
              JOIN dealers d ON d.id = o.dealer_id
             WHERE o.snapshot_date = (SELECT MAX(snapshot_date) FROM dealer_opportunities)
             ORDER BY o.score DESC NULLS LAST
             LIMIT 3
        """, (target_date,))
        out["dealer_opps"] = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT COUNT(*) AS n
              FROM dealer_opportunities
             WHERE snapshot_date = %s
        """, (target_date,))
        out["dealer_opps_total"] = int(cur.fetchone()["n"] or 0)

        # 4. Stale bids — 'reviewing' >5 days.
        cur.execute("""
            SELECT COUNT(*) AS n
              FROM bids
             WHERE status = 'reviewing'
               AND created_at < NOW() - INTERVAL '5 days'
        """)
        out["stale_count"] = int(cur.fetchone()["n"] or 0)

        cur.execute("""
            SELECT id, year, make, model, ai_price,
                   EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0 AS days_old
              FROM bids
             WHERE status = 'reviewing'
               AND created_at < NOW() - INTERVAL '5 days'
               AND ai_price IS NOT NULL
             ORDER BY ai_price DESC
             LIMIT 3
        """)
        out["stale_top"] = [dict(r) for r in cur.fetchall()]

        # 5. Watchlist matches yesterday.
        cur.execute("""
            SELECT COUNT(*) AS n
              FROM bill_watchlist_hits h
              JOIN bill_watchlists w ON w.id = h.watchlist_id
             WHERE h.matched_at::date = (%s::date - INTERVAL '1 day')
               AND w.name <> %s
        """, (target_date, SENTINEL_WATCHLIST_NAME))
        out["watchlist_hits_yest"] = int(cur.fetchone()["n"] or 0)

        # 6. Partner offers in last 14h (best-effort).
        try:
            cur.execute("""
                SELECT COUNT(*) AS n
                  FROM bid_partner_offers
                 WHERE submitted_at > NOW() - INTERVAL '14 hours'
            """)
            out["partner_offers_overnight"] = int(cur.fetchone()["n"] or 0)
        except Exception:
            out["partner_offers_overnight"] = None

        try:
            cur.execute("""
                SELECT COUNT(*) AS n
                  FROM partner_bid_requests
                 WHERE submitted_at > NOW() - INTERVAL '14 hours'
            """)
            out["partner_requests_overnight"] = int(cur.fetchone()["n"] or 0)
        except Exception:
            out["partner_requests_overnight"] = None

    return out


# ─── rendering ────────────────────────────────────────────────────────

def render(data: dict) -> str:
    """Render gathered data to a short spoken paragraph."""
    parts = []

    # Greeting — keep it human.
    parts.append("Good morning. Here's your briefing.")

    # 1. Overnight bids.
    n = data["overnight_total"]
    if n == 0:
        parts.append("Quiet overnight — no new bids came in.")
    else:
        verb = "came in" if n > 1 else "came in"
        parts.append(f"{say_int(n).capitalize()} new {('bids' if n > 1 else 'bid')} {verb} overnight.")
        # Top by ai_price.
        top = data["overnight_top_bids"]
        if top:
            r = top[0]
            yr = r.get("year") or ""
            mk = _safe_make(r.get("make") or "")
            md = _safe_model(r.get("model") or "")
            price = r.get("ai_price")
            if price:
                parts.append(
                    f"The biggest is bid {r['id']}, a "
                    f"{yr} {mk} {md} marked at {say_money(price)}."
                )


    # 3. Dealer opportunities.
    opps = data["dealer_opps"]
    if opps:
        top = opps[0]
        yr = top.get("year") or ""
        mk = _safe_make(top.get("make") or "")
        md = _safe_model(top.get("model") or "")
        under = top.get("dollars_under_mmr")
        dealer = (top.get("dealer_name") or "").strip()
        parts.append(
            f"On the dealer watch, top opportunity is a "
            f"{yr} {mk} {md}"
            + (f", {say_money(under)} under M-M-R" if under else "")
            + (f" at {dealer}" if dealer else "")
            + "."
        )

    # 4. Stale bids — only mention if there are a lot.
    stale = data["stale_count"]
    if stale >= 5:
        parts.append(
            f"Heads up — {say_int(stale)} bids are still stuck in reviewing "
            f"more than five days old. Worth a sweep."
        )

    # 5. Watchlist matches yesterday.
    wl = data["watchlist_hits_yest"]
    if wl > 0:
        parts.append(
            f"Your watchlists hit "
            f"{say_int(wl)} "
            f"{'time' if wl == 1 else 'times'} yesterday."
        )

    # 6. Partner activity (only if explicit).
    po = data.get("partner_offers_overnight")
    if po and po > 0:
        parts.append(
            f"Partners sent in {say_int(po)} "
            f"{'offer' if po == 1 else 'offers'} overnight."
        )

    # Sign-off.
    parts.append("That's the rundown. Let me know what you want to dig into.")

    return " ".join(parts)


# ─── dispatch ─────────────────────────────────────────────────────────

def _get_sentinel_watchlist_id() -> int:
    with _connect() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id FROM bill_watchlists WHERE name=%s LIMIT 1",
            (SENTINEL_WATCHLIST_NAME,),
        )
        row = cur.fetchone()
        if row:
            return int(row[0])
        # Auto-create if missing (idempotent for fresh installs).
        cur.execute(
            """INSERT INTO bill_watchlists
                 (created_by, name, description, conditions, active)
               VALUES (%s, %s, %s, %s::jsonb, FALSE)
               RETURNING id""",
            ("oscar", SENTINEL_WATCHLIST_NAME,
             "Sentinel watchlist for proactive daily morning briefing.",
             "{}"),
        )
        return int(cur.fetchone()[0])


def _enqueue(user: str, message: str, sentinel_id: int) -> int:
    """Insert briefing into the bill_watchlist_hits queue.
    bid_id is 0 (no FK to bids — see schema check). Re-uses the existing
    /api/ew-voice/pending poller path."""
    # The sentinel watchlist's created_by must match the user the poller
    # scopes for, since /api/ew-voice/pending filters by w.created_by.
    with _connect() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE bill_watchlists SET created_by=%s WHERE id=%s "
            "AND lower(created_by) <> lower(%s)",
            (user.lower(), sentinel_id, user.lower()),
        )
        # Each day's briefing gets a unique synthetic bid_id so the
        # UNIQUE (watchlist_id, bid_id) constraint doesn't block re-runs
        # within the same day during testing. Use negative epoch-of-day.
        synth_bid_id = -int(
            (datetime.now(tz=None).timestamp()
             - datetime(2026, 1, 1).timestamp())
        )
        cur.execute(
            """INSERT INTO bill_watchlist_hits
                  (watchlist_id, bid_id, matched_at, message)
               VALUES (%s, %s, NOW(), %s)
               ON CONFLICT (watchlist_id, bid_id) DO NOTHING
               RETURNING id""",
            (sentinel_id, synth_bid_id, message),
        )
        row = cur.fetchone()
        if not row:
            return -1
        hit_id = int(row[0])
        cur.execute(
            """UPDATE bill_watchlists
                  SET match_count = match_count + 1,
                      last_matched_at = NOW(),
                      updated_at = NOW()
                WHERE id = %s""",
            (sentinel_id,),
        )
        return hit_id


def _get_user_away_state(user: str):
    """Returns (channel, phone) if user is away, else (None, None)."""
    with _connect() as c, c.cursor() as cur:
        cur.execute(
            """SELECT away_channel, away_phone, away_until
                 FROM bill_user_state
                WHERE user_name = %s
                  AND away_channel IS NOT NULL
                  AND (away_until IS NULL OR away_until > NOW())""",
            (user.lower(),),
        )
        row = cur.fetchone()
        if not row:
            return None, None
        return row[0], row[1]


def _dispatch_telegram(message: str, hit_id: int) -> bool:
    """Fallback if voice/sms/call unavailable: Telegram via OrlandoAI bot."""
    import requests
    # Reuse the same bot pattern from dealer_opportunities_cron.sh
    bot = os.environ.get(
        "TELEGRAM_BOT_TOKEN",
        "8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM",
    )
    chat = os.environ.get("TELEGRAM_CHAT_ID", "7985611488")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            data={"chat_id": chat, "text": f"Morning briefing:\n\n{message}"},
            timeout=10,
        )
        r.raise_for_status()
        log.info(f"telegram dispatched hit_id={hit_id} status={r.status_code}")
        return True
    except Exception as e:
        log.exception(f"telegram dispatch failed: {e}")
        return False


def _dispatch_sms(phone: str, message: str, hit_id: int) -> bool:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_PHONE")
    if not (sid and tok and frm):
        log.warning("twilio env missing — SMS skipped")
        return False
    try:
        from twilio.rest import Client
        client = Client(sid, tok)
        # SMS limit ~1600 chars; trim if needed.
        body = message if len(message) < 1500 else (message[:1490] + "...")
        msg = client.messages.create(to=phone, from_=frm, body=body)
        log.info(f"SMS sent sid={msg.sid} to={phone} hit_id={hit_id}")
        return True
    except Exception as e:
        log.exception(f"twilio sms failed: {e}")
        return False


def _dispatch_call(phone: str, message: str, hit_id: int) -> bool:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_PHONE")
    if not (sid and tok and frm):
        log.warning("twilio env missing — call skipped")
        return False
    try:
        from twilio.rest import Client
        import xml.sax.saxutils as _x
        client = Client(sid, tok)
        safe = _x.escape(message)
        twiml = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response>"
            f'<Pause length="1"/>'
            f'<Say voice="alice">{safe}</Say>'
            f"</Response>"
        )
        call = client.calls.create(to=phone, from_=frm, twiml=twiml)
        log.info(f"CALL placed sid={call.sid} to={phone} hit_id={hit_id}")
        return True
    except Exception as e:
        log.exception(f"twilio call failed: {e}")
        return False


def _mark_notified(hit_id: int, channel: str):
    with _connect() as c, c.cursor() as cur:
        cur.execute(
            """UPDATE bill_watchlist_hits
                  SET notified_at = NOW(),
                      notify_via = %s
                WHERE id = %s""",
            (channel, hit_id),
        )


def dispatch(user: str, hit_id: int, message: str) -> str:
    """Honor away mode if active; otherwise leave for /v-edge poller.
    Returns the channel actually used (or 'voice_session' for queue)."""
    channel, phone = _get_user_away_state(user)
    if not channel:
        return "voice_session"  # /v-edge will pick it up
    ok = False
    if channel == "call":
        ok = _dispatch_call(phone, message, hit_id)
    elif channel == "sms":
        ok = _dispatch_sms(phone, message, hit_id)
    elif channel == "telegram":
        ok = _dispatch_telegram(message, hit_id)
    else:
        log.warning(f"unknown away channel {channel!r}")
        ok = _dispatch_telegram(message, hit_id)
        channel = "telegram"
    if ok:
        _mark_notified(hit_id, channel)
        return channel
    return f"failed:{channel}"


# ─── orchestration ────────────────────────────────────────────────────

def run(user: str, target_date: date, dry_run: bool) -> dict:
    log.info(
        f"briefing run start user={user} date={target_date} dry_run={dry_run}"
    )
    data = gather(target_date)
    message = render(data)
    word_count = len(message.split())
    log.info(f"rendered words={word_count} chars={len(message)}")

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": message,
            "word_count": word_count,
            "data_summary": {
                "overnight_bids": data["overnight_total"],
                "dealer_opps": data["dealer_opps_total"],
                "stale_bids": data["stale_count"],
                "watchlist_hits_yest": data["watchlist_hits_yest"],
            },
        }

    sentinel_id = _get_sentinel_watchlist_id()
    hit_id = _enqueue(user, message, sentinel_id)
    if hit_id < 0:
        log.warning("duplicate briefing — skipping dispatch")
        return {"ok": False, "duplicate": True, "message": message}

    channel = dispatch(user, hit_id, message)
    log.info(f"briefing dispatched hit_id={hit_id} channel={channel}")
    return {
        "ok": True,
        "hit_id": hit_id,
        "dispatch_via": channel,
        "message": message,
        "word_count": word_count,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="oscar")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--for-date", default=None,
                    help="YYYY-MM-DD (debug)")
    args = ap.parse_args()

    # Add file handler so cron runs are persisted.
    try:
        fh = logging.FileHandler(LOG_FILE)
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [briefing] %(message)s"))
        log.addHandler(fh)
    except PermissionError:
        pass  # fall back to stdout only

    target = (datetime.strptime(args.for_date, "%Y-%m-%d").date()
              if args.for_date else date.today())

    try:
        result = run(args.user.lower(), target, args.dry_run)
    except Exception as e:
        log.exception(f"briefing run failed: {e}")
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("─── DRY RUN — would dispatch the following ──────────────")
        print(result["message"])
        print(f"\n[words={result['word_count']}]")
        print(f"[data_summary={result['data_summary']}]")
    else:
        print(f"OK hit_id={result.get('hit_id')} "
              f"channel={result.get('dispatch_via')} "
              f"words={result.get('word_count')}")
        print()
        print(result["message"])


if __name__ == "__main__":
    main()
