#!/usr/bin/env python3
"""partner_silence_watcher.py — proactive "you haven't talked to X" nudges.

Once a day (via cron), scans partner_activity_summary and queues a Bill
notification for any partner where:
  - silent_days >= MIN_SILENT_DAYS (default 14), AND
  - purchases_365d >= MIN_PURCHASES_365D (default 2)
    (i.e. it's a real relationship, not just a logo on the roster), AND
  - we haven't sent a nudge for this same dealer in the last
    NUDGE_COOLDOWN_DAYS (default 7).

The nudge is dropped into the existing bill_watchlist_hits queue (using
a sentinel watchlist row keyed to user 'oscar') so it surfaces through
the same `/v-edge/voice/pending` pipeline Bill already polls for new
alerts. We also keep a parallel audit row in partner_silence_nudges_sent.

CLI:
    python3 partner_silence_watcher.py            # send queued nudges
    python3 partner_silence_watcher.py --dry-run  # print but don't send
    python3 partner_silence_watcher.py --min-silent 21 --min-purchases 4
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras


DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
)

DEFAULT_MIN_SILENT = 14
DEFAULT_MIN_PURCHASES = 2
DEFAULT_COOLDOWN = 7
SENTINEL_WATCHLIST_NAME = "partner_silence_nudges"
SENTINEL_OWNER = "oscar"

# Synthetic bid_id space — well outside any real bids.id. Each dealer gets
# a deterministic synthetic key so the UNIQUE (watchlist_id, bid_id)
# constraint prevents duplicate fires when the cron retries.
SYNTHETIC_BID_OFFSET = 9_000_000_000  # plus dealer_id + day-of-year hash


log = logging.getLogger("partner_silence_watcher")


# ── Helpers ──────────────────────────────────────────────────────────────


def _spoken_dollars(amount: float | int | None) -> str:
    """Render a dollar amount in plain spoken English per Bill's #1 RULE.

    Examples:
       $385,503.75 -> "three hundred eighty-five thousand"  (rounded, no cents)
       $124,114    -> "one hundred twenty-four thousand"
       $32,590     -> "thirty-two thousand"
       $4,000      -> "four thousand"
    """
    if amount is None:
        return "zero"
    n = int(round(float(amount)))
    if n == 0:
        return "zero"
    if n < 0:
        return "negative " + _spoken_dollars(-n)

    ones = ["", "one", "two", "three", "four", "five", "six", "seven",
            "eight", "nine", "ten", "eleven", "twelve", "thirteen",
            "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
            "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty",
            "seventy", "eighty", "ninety"]

    def _say_below_thousand(x: int) -> str:
        if x == 0:
            return ""
        parts = []
        h, rem = divmod(x, 100)
        if h:
            parts.append(f"{ones[h]} hundred")
        if rem:
            if rem < 20:
                parts.append(ones[rem])
            else:
                t, u = divmod(rem, 10)
                parts.append(tens[t] + (f"-{ones[u]}" if u else ""))
        return " ".join(parts)

    # We render at thousands resolution. Drop sub-thousand for amounts
    # >= $10K. Sub-$10K keep the precise hundreds.
    if n >= 1_000_000:
        millions, rem = divmod(n, 1_000_000)
        thousands = rem // 1_000
        out = f"{_say_below_thousand(millions)} million"
        if thousands:
            out += f" {_say_below_thousand(thousands)} thousand"
        return out
    if n >= 10_000:
        thousands = n // 1_000
        return f"{_say_below_thousand(thousands)} thousand"
    if n >= 1_000:
        thousands, rem = divmod(n, 1_000)
        out = f"{_say_below_thousand(thousands)} thousand"
        if rem:
            out += f" {_say_below_thousand(rem)}"
        return out
    return _say_below_thousand(n)


def _spoken_count(n: int) -> str:
    """Render a small integer 1-19 as a word, else fall back to dollars-style."""
    ones = ["zero", "one", "two", "three", "four", "five", "six", "seven",
            "eight", "nine", "ten", "eleven", "twelve", "thirteen",
            "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
            "nineteen"]
    if 0 <= n < 20:
        return ones[n]
    return _spoken_dollars(n)  # tens / hundreds, no "thousand" issue for typical counts


def build_nudge_message(dealer_name: str, silent_days: int,
                        purchases_365d: int,
                        total_gross_365d: float | int) -> str:
    """Compose the spoken-English nudge per #1 RULE (no chunked shorthand,
    no bare digits, no 'K')."""
    days_word = _spoken_count(silent_days) if silent_days < 100 \
                else _spoken_dollars(silent_days)
    if purchases_365d == 1:
        bought_clause = "They bought one from us in the last year"
    else:
        bought_clause = f"They bought {_spoken_count(purchases_365d)} from us in the last year"
    if total_gross_365d and total_gross_365d > 0:
        bought_clause += f", worth {_spoken_dollars(total_gross_365d)}"
    return (
        f"Heads up — you haven't pushed anything to {dealer_name} in "
        f"{days_word} days. {bought_clause}. "
        f"Want me to flag any active candidates?"
    )


# ── Sentinel watchlist ──────────────────────────────────────────────────


def _ensure_sentinel_watchlist(cur) -> int:
    """Return the watchlist_id for partner-silence nudges, creating it
    once if it doesn't exist. We don't actually run watchlist matching
    on this row — it just serves as the FK target so notifications can
    flow through bill_watchlist_hits."""
    cur.execute("""
        SELECT id FROM bill_watchlists
         WHERE lower(created_by) = %s AND name = %s
         LIMIT 1
    """, (SENTINEL_OWNER, SENTINEL_WATCHLIST_NAME))
    row = cur.fetchone()
    if row:
        return row[0] if not isinstance(row, dict) else row["id"]
    cur.execute("""
        INSERT INTO bill_watchlists
            (created_by, name, description, conditions, pitch_for,
             active, match_count, created_at, updated_at)
        VALUES (%s, %s, %s, %s::jsonb, %s, FALSE, 0, NOW(), NOW())
        RETURNING id
    """, (
        SENTINEL_OWNER, SENTINEL_WATCHLIST_NAME,
        "[system] Partner-silence nudge queue. Do not edit. "
        "Hits are inserted by partner_silence_watcher.py to surface "
        "aging dealer-partner relationships to Bill.",
        '{"_system": "partner_silence_nudges"}',
        None,
    ))
    wid = cur.fetchone()[0]
    log.info("created sentinel watchlist id=%s for partner-silence nudges", wid)
    return wid


def _synthetic_bid_id(dealer_id: int) -> int:
    """Deterministic synthetic bid_id per dealer per UTC day so the
    UNIQUE (watchlist_id, bid_id) constraint dedupes same-day retries
    but still allows a fresh nudge to fire on a later day."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    # Mix dealer_id with day-of-year-stamp so dealer 6 on May 26 != May 27.
    return SYNTHETIC_BID_OFFSET + (int(today) * 1000) + dealer_id


# ── Main scan ────────────────────────────────────────────────────────────


def scan_and_queue(min_silent: int, min_purchases: int,
                   cooldown_days: int, dry_run: bool) -> list[dict]:
    """Find candidates and queue nudges. Returns the list (dicts) of
    candidates considered, each annotated with a 'sent' flag and the
    composed 'message'."""
    out: list[dict] = []
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT dealer_id, dealer_name, silent_days,
                       purchases_365d, total_gross_365d,
                       last_push_at, last_purchase_at
                  FROM partner_activity_summary
                 WHERE silent_days IS NOT NULL
                   AND silent_days >= %s
                   AND purchases_365d >= %s
                 ORDER BY silent_days DESC
            """, (min_silent, min_purchases))
            candidates = cur.fetchall()

            sentinel_wid = _ensure_sentinel_watchlist(cur) if not dry_run else -1

            for c in candidates:
                # Cooldown check — has a nudge gone out for this dealer
                # in the last N days?
                cur.execute("""
                    SELECT MAX(sent_at) AS last_sent
                      FROM partner_silence_nudges_sent
                     WHERE dealer_id = %s
                       AND sent_at >= NOW() - INTERVAL '%s days'
                       AND dry_run = FALSE
                """, (c["dealer_id"], cooldown_days))
                last_sent = (cur.fetchone() or {}).get("last_sent")
                if last_sent is not None:
                    out.append({**c, "sent": False, "reason": "in_cooldown",
                                "last_sent": last_sent.isoformat(),
                                "message": None})
                    continue

                msg = build_nudge_message(
                    c["dealer_name"], c["silent_days"],
                    c["purchases_365d"], c["total_gross_365d"] or 0.0,
                )

                if dry_run:
                    out.append({**c, "sent": False, "reason": "dry_run",
                                "message": msg})
                    continue

                # Queue via bill_watchlist_hits (Bill's existing pickup path)
                synth_bid = _synthetic_bid_id(c["dealer_id"])
                cur.execute("""
                    INSERT INTO bill_watchlist_hits
                        (watchlist_id, bid_id, matched_at, message)
                    VALUES (%s, %s, NOW(), %s)
                    ON CONFLICT (watchlist_id, bid_id) DO NOTHING
                    RETURNING id
                """, (sentinel_wid, synth_bid, msg))
                hit_row = cur.fetchone()
                hit_id = hit_row["id"] if hit_row else None

                # Audit row
                cur.execute("""
                    INSERT INTO partner_silence_nudges_sent
                        (dealer_id, sent_at, silent_days_at_send,
                         purchases_365d_at_send, total_gross_365d_at_send,
                         message, bill_hit_id, dry_run)
                    VALUES (%s, NOW(), %s, %s, %s, %s, %s, FALSE)
                """, (c["dealer_id"], c["silent_days"], c["purchases_365d"],
                      c["total_gross_365d"], msg, hit_id))

                out.append({**c, "sent": hit_id is not None,
                            "reason": "queued" if hit_id else "duplicate",
                            "hit_id": hit_id, "message": msg})

        if not dry_run:
            conn.commit()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-silent", type=int, default=DEFAULT_MIN_SILENT,
                    help=f"Min silent_days threshold (default {DEFAULT_MIN_SILENT})")
    ap.add_argument("--min-purchases", type=int, default=DEFAULT_MIN_PURCHASES,
                    help=f"Min purchases_365d (default {DEFAULT_MIN_PURCHASES})")
    ap.add_argument("--cooldown-days", type=int, default=DEFAULT_COOLDOWN,
                    help=f"Per-dealer cooldown window (default {DEFAULT_COOLDOWN})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and print but do not queue or audit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    results = scan_and_queue(args.min_silent, args.min_purchases,
                             args.cooldown_days, args.dry_run)

    queued = sum(1 for r in results if r.get("sent"))
    skipped = sum(1 for r in results
                  if r.get("reason") in ("in_cooldown", "duplicate", "dry_run"))
    log.info("partner_silence_watcher complete: %d candidates, %d queued, %d skipped%s",
             len(results), queued, skipped,
             " — DRY RUN" if args.dry_run else "")

    for r in results:
        log.info(
            "  [%s] %s silent=%s purch365=%s gross365=$%.0f -> %s",
            r["dealer_id"], r["dealer_name"], r["silent_days"],
            r["purchases_365d"], r["total_gross_365d"] or 0,
            r["reason"],
        )
        if r.get("message"):
            log.info("    MSG: %s", r["message"])


if __name__ == "__main__":
    main()
