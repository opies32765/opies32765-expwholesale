#!/usr/bin/env python3
"""partner_activity_refresh.py — per-partner relationship activity summary.

Walks the 16 (or N-filtered) partner dealers in `dealers` and computes:
  - Outbound activity (pushes):   partner_bid_requests, partner_sms_sent,
                                   bids.partner_dealer_id/oscar_partner_dealer_id,
                                   bids.network_pushed_at
  - Inbound activity (offers):    bid_partner_offers
  - Real purchases (LSL ledger):  /opt/livesaleslog/crm.db deals
                                   matched against dealers.lsl_aliases

UPSERTs results into partner_activity_summary so the Bill MCP tools and
the daily silence-nudge cron can read pre-aggregated state.

CLI:
    python3 partner_activity_refresh.py [--dealer-id N] [--dry-run] [-v]

Cron wrapper: /usr/local/bin/partner_activity_cron.sh (weekdays 07:00 ET).
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras


DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
)
LSL_DB_PATH = os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")

# EW canonical profit/gross formula — mirrors lsl_buyer_match.py and
# /opt/livesaleslog/dashboard/app.py. Stays consistent with how Bill quotes
# numbers in find_best_buyer / lsl_customer_history.
_LSL_GROSS_EXPR = (
    "(COALESCE(sale_price,0) - COALESCE(purchase_cost,0) "
    "- COALESCE(total_supp_costs,0))"
)


log = logging.getLogger("partner_activity_refresh")


# ── Partner-dealer enumeration ───────────────────────────────────────────

# All 16 onboarded partner dealers per the operator brief. We use this
# explicit allowlist so we don't accidentally summarise random EW lot
# entries that happen to be in `dealers` but aren't partner counterparties.
PARTNER_DEALER_IDS = {1, 2, 3, 4, 6, 7, 8, 9, 15, 16, 17, 18, 19, 20, 22, 55}


def _connect_pg():
    return psycopg2.connect(DB_URL)


def _connect_lsl():
    if not os.path.exists(LSL_DB_PATH):
        return None
    c = sqlite3.connect(f"file:{LSL_DB_PATH}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ── Per-partner aggregation ──────────────────────────────────────────────


def _aware_utc(ts):
    """Coerce a naive sqlite/timestamp string to aware UTC datetime."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts).strip()
    if not s:
        return None
    # sqlite stores e.g. "2026-04-12 14:33:01" — try a couple of common formats
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(s.split("+", 1)[0], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _max_ts(*items):
    cleaned = [t for t in items if t is not None]
    if not cleaned:
        return None
    return max(cleaned)


def _pg_push_signals(cur, dealer_id: int, now_utc: datetime) -> dict:
    """Outbound pushes: partner_bid_requests + partner_sms_sent + bids
    columns that attribute a bid to a partner dealer + network_pushed_at
    on any bid that resolved to this dealer.

    Note: bids.network_pushed_at by itself isn't keyed to a specific dealer
    — it just records that the bid was pushed to the partner network. We
    fold those into the "no specific dealer" silence calc only if the bid
    has partner_dealer_id matching. For pure broadcast pushes we ignore at
    the per-dealer level (they don't tell us we touched THIS dealer).
    """
    d30 = now_utc - timedelta(days=30)
    d90 = now_utc - timedelta(days=90)

    # partner_bid_requests — explicit dealer_id, submitted_at
    cur.execute("""
        SELECT MAX(submitted_at) AS last_at,
               COUNT(*) FILTER (WHERE submitted_at >= %s) AS n30,
               COUNT(*) FILTER (WHERE submitted_at >= %s) AS n90
          FROM partner_bid_requests
         WHERE dealer_id = %s
    """, (d30, d90, dealer_id))
    pbr = cur.fetchone() or {}

    # partner_sms_sent — explicit dealer_id, sent_at
    cur.execute("""
        SELECT MAX(sent_at) AS last_at,
               COUNT(*) FILTER (WHERE sent_at >= %s) AS n30,
               COUNT(*) FILTER (WHERE sent_at >= %s) AS n90
          FROM partner_sms_sent
         WHERE dealer_id = %s
    """, (d30, d90, dealer_id))
    sms = cur.fetchone() or {}

    # partner_alerts_sent — explicit dealer_id, sent_at (push alert pings)
    cur.execute("""
        SELECT MAX(sent_at) AS last_at,
               COUNT(*) FILTER (WHERE sent_at >= %s) AS n30,
               COUNT(*) FILTER (WHERE sent_at >= %s) AS n90
          FROM partner_alerts_sent
         WHERE dealer_id = %s
    """, (d30, d90, dealer_id))
    pas = cur.fetchone() or {}

    # bids.partner_dealer_id / oscar_partner_dealer_id — attributed bids
    # touching this partner (push direction = we routed it to them)
    cur.execute("""
        SELECT
          MAX(GREATEST(
                COALESCE(network_pushed_at, created_at::timestamptz),
                COALESCE(oscar_pushed_to_ew_at, created_at::timestamptz),
                created_at::timestamptz)) AS last_at,
          COUNT(*) FILTER (
              WHERE COALESCE(network_pushed_at, created_at::timestamptz) >= %s)
              AS n30,
          COUNT(*) FILTER (
              WHERE COALESCE(network_pushed_at, created_at::timestamptz) >= %s)
              AS n90
          FROM bids
         WHERE partner_dealer_id = %s OR oscar_partner_dealer_id = %s
    """, (d30, d90, dealer_id, dealer_id))
    bid_attr = cur.fetchone() or {}

    last_push = _max_ts(pbr.get("last_at"), sms.get("last_at"),
                        pas.get("last_at"), bid_attr.get("last_at"))
    n30 = (pbr.get("n30") or 0) + (sms.get("n30") or 0) + \
          (pas.get("n30") or 0) + (bid_attr.get("n30") or 0)
    n90 = (pbr.get("n90") or 0) + (sms.get("n90") or 0) + \
          (pas.get("n90") or 0) + (bid_attr.get("n90") or 0)

    return {"last_push_at": last_push, "pushes_30d": n30, "pushes_90d": n90}


def _pg_offer_signals(cur, dealer_id: int, now_utc: datetime) -> dict:
    """Inbound offers from this partner. bid_partner_offers.submitted_at
    is timestamp-without-tz; we treat it as UTC for comparison."""
    d30 = now_utc - timedelta(days=30)
    d90 = now_utc - timedelta(days=90)

    cur.execute("""
        SELECT MAX(submitted_at) AS last_at,
               COUNT(*) FILTER (WHERE submitted_at >= %s) AS n30,
               COUNT(*) FILTER (WHERE submitted_at >= %s) AS n90
          FROM bid_partner_offers
         WHERE dealer_id = %s
    """, (d30.replace(tzinfo=None), d90.replace(tzinfo=None), dealer_id))
    row = cur.fetchone() or {}
    last_at = row.get("last_at")
    # Promote naive → aware for downstream GREATEST() comparisons
    if last_at is not None and last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    return {
        "last_offer_at": last_at,
        "offers_30d": row.get("n30") or 0,
        "offers_90d": row.get("n90") or 0,
    }


def _lsl_purchase_signals(lsl: sqlite3.Connection | None, aliases: list[str],
                          now_utc: datetime) -> dict:
    """Real purchases this partner made FROM Experience (we sold TO them).

    LSL `deals.customer_name` = who bought it from us. We match using a
    case-insensitive LIKE against any of the dealer's aliases.
    """
    if lsl is None or not aliases:
        return {"last_purchase_at": None, "purchases_30d": 0,
                "purchases_90d": 0, "purchases_365d": 0,
                "total_gross_365d": 0.0}
    cur = lsl.cursor()
    # Build OR-of-LIKE clauses for each alias
    clauses = " OR ".join(["UPPER(customer_name) LIKE UPPER(?)" for _ in aliases])
    params: list = [f"%{a}%" for a in aliases]
    d30 = (now_utc - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    d90 = (now_utc - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    d365 = (now_utc - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")

    # Last purchase + count windows + 365d gross
    cur.execute(
        f"""
        SELECT
          MAX(sold_at) AS last_at,
          SUM(CASE WHEN sold_at >= ? THEN 1 ELSE 0 END) AS n30,
          SUM(CASE WHEN sold_at >= ? THEN 1 ELSE 0 END) AS n90,
          SUM(CASE WHEN sold_at >= ? THEN 1 ELSE 0 END) AS n365,
          COALESCE(SUM(CASE WHEN sold_at >= ?
                            THEN {_LSL_GROSS_EXPR} ELSE 0 END), 0) AS gross_365d
          FROM deals
         WHERE ({clauses})
           AND sale_price IS NOT NULL AND sale_price > 0
        """,
        [d30, d90, d365, d365, *params],
    )
    row = cur.fetchone()
    cur.close()
    if row is None:
        return {"last_purchase_at": None, "purchases_30d": 0,
                "purchases_90d": 0, "purchases_365d": 0,
                "total_gross_365d": 0.0}
    return {
        "last_purchase_at": _aware_utc(row["last_at"]),
        "purchases_30d": int(row["n30"] or 0),
        "purchases_90d": int(row["n90"] or 0),
        "purchases_365d": int(row["n365"] or 0),
        "total_gross_365d": float(row["gross_365d"] or 0.0),
    }


def _compute_silent_days(now_utc: datetime, last_push, last_offer,
                        last_purchase) -> int | None:
    last = _max_ts(last_push, last_offer, last_purchase)
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0, (now_utc - last).days)


def refresh_one(pg_cur, lsl, dealer_id: int, dealer_name: str,
                aliases: list[str], now_utc: datetime,
                dry_run: bool = False) -> dict:
    push = _pg_push_signals(pg_cur, dealer_id, now_utc)
    offer = _pg_offer_signals(pg_cur, dealer_id, now_utc)
    purch = _lsl_purchase_signals(lsl, aliases, now_utc)
    silent = _compute_silent_days(now_utc,
                                  push["last_push_at"],
                                  offer["last_offer_at"],
                                  purch["last_purchase_at"])
    row = {
        "dealer_id": dealer_id,
        "dealer_name": dealer_name,
        **push,
        **offer,
        **purch,
        "silent_days": silent,
    }
    if not dry_run:
        pg_cur.execute("""
            INSERT INTO partner_activity_summary
              (dealer_id, dealer_name, last_push_at, pushes_30d, pushes_90d,
               last_offer_at, offers_30d, offers_90d,
               last_purchase_at, purchases_30d, purchases_90d,
               purchases_365d, total_gross_365d, silent_days,
               computed_at)
            VALUES (%(dealer_id)s, %(dealer_name)s,
                    %(last_push_at)s, %(pushes_30d)s, %(pushes_90d)s,
                    %(last_offer_at)s, %(offers_30d)s, %(offers_90d)s,
                    %(last_purchase_at)s, %(purchases_30d)s, %(purchases_90d)s,
                    %(purchases_365d)s, %(total_gross_365d)s, %(silent_days)s,
                    NOW())
            ON CONFLICT (dealer_id) DO UPDATE SET
                dealer_name      = EXCLUDED.dealer_name,
                last_push_at     = EXCLUDED.last_push_at,
                pushes_30d       = EXCLUDED.pushes_30d,
                pushes_90d       = EXCLUDED.pushes_90d,
                last_offer_at    = EXCLUDED.last_offer_at,
                offers_30d       = EXCLUDED.offers_30d,
                offers_90d       = EXCLUDED.offers_90d,
                last_purchase_at = EXCLUDED.last_purchase_at,
                purchases_30d    = EXCLUDED.purchases_30d,
                purchases_90d    = EXCLUDED.purchases_90d,
                purchases_365d   = EXCLUDED.purchases_365d,
                total_gross_365d = EXCLUDED.total_gross_365d,
                silent_days      = EXCLUDED.silent_days,
                computed_at      = NOW()
        """, row)
    return row


# ── Main ─────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dealer-id", type=int, default=None,
                    help="Only refresh this one dealer (default: all 16 partners)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and print but do not UPSERT")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    now_utc = datetime.now(timezone.utc)

    lsl = _connect_lsl()
    if lsl is None:
        log.warning("LSL crm.db not available at %s — purchase signals will be 0",
                    LSL_DB_PATH)

    n = 0
    with _connect_pg() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ids = ([args.dealer_id] if args.dealer_id
                   else sorted(PARTNER_DEALER_IDS))
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(
                f"SELECT id, name, lsl_aliases FROM dealers WHERE id IN ({placeholders})",
                tuple(ids),
            )
            dealers = cur.fetchall()
            for d in dealers:
                aliases_raw = d.get("lsl_aliases") or []
                # jsonb arrives as Python list already via psycopg2
                if isinstance(aliases_raw, str):
                    try:
                        aliases_raw = json.loads(aliases_raw)
                    except Exception:
                        aliases_raw = []
                aliases = [a for a in (aliases_raw or []) if a]
                # Always include the canonical dealer name as a final
                # fallback alias (handles dealers with no explicit aliases).
                if d["name"] not in aliases:
                    aliases.append(d["name"])
                row = refresh_one(cur, lsl, d["id"], d["name"], aliases,
                                  now_utc, dry_run=args.dry_run)
                n += 1
                log.info(
                    "  [%s] %s — silent=%s push30=%d offer30=%d purch90=%d gross365=$%.0f",
                    d["id"], d["name"],
                    row["silent_days"] if row["silent_days"] is not None else "—",
                    row["pushes_30d"], row["offers_30d"], row["purchases_90d"],
                    row["total_gross_365d"] or 0,
                )
        if not args.dry_run:
            conn.commit()

    if lsl is not None:
        lsl.close()

    log.info("partner_activity_refresh complete (%s dealers%s)",
             n, " — DRY RUN" if args.dry_run else "")


if __name__ == "__main__":
    main()
