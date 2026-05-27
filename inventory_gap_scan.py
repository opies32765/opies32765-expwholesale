#!/usr/bin/env python3
"""inventory_gap_scan.py — nightly EW inventory holes vs surplus per portal dealer.

Compares each portal dealer's CURRENT active inventory against a 90-day
rolling baseline (avg daily distinct-VIN count, per YMM bucket) and sends
a single Telegram digest. Cron: 02:30 EDT, after scan_all_dealers.py.

Usage:
  python3 inventory_gap_scan.py            # live (Telegram)
  python3 inventory_gap_scan.py --dry-run  # print only
"""
import argparse
import logging
import os
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras

LOG_FILE = "/var/log/ew-inventory-gap.log"

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
)

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM",
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7985611488")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [gap-scan] %(message)s",
)
log = logging.getLogger("gap_scan")


def year_bucket(yr):
    if yr is None:
        return "older"
    try:
        yr = int(yr)
    except (TypeError, ValueError):
        return "older"
    # current year on box; use a fixed cutoff = anything older than 5y -> "older"
    # 2026 today; bucket exact for 2021+
    if yr >= 2021:
        return str(yr)
    return "older"


def fetch_portal_dealers(cur):
    cur.execute(
        "SELECT id, name FROM dealers "
        "WHERE portal_slug IS NOT NULL AND active = TRUE "
        "ORDER BY name"
    )
    return cur.fetchall()


def fetch_current_inventory(cur, dealer_ids):
    """Return dict[dealer_id] -> Counter[(yb, make, model)] = current count."""
    cur.execute(
        """
        SELECT dealer_id, year, make, model
          FROM dealer_inventory
         WHERE status = 'active'
           AND dealer_id = ANY(%s)
           AND make IS NOT NULL
           AND model IS NOT NULL
        """,
        (dealer_ids,),
    )
    out = defaultdict(lambda: defaultdict(int))
    for d_id, yr, mk, md in cur.fetchall():
        key = (year_bucket(yr), (mk or "").strip().upper(), (md or "").strip().upper())
        if not key[1] or not key[2]:
            continue
        out[d_id][key] += 1
    return out


def fetch_baseline(cur, dealer_ids):
    """90-day avg daily distinct-VIN count per (dealer, yb, make, model).

    For each VIN we approximate "days active in window" as:
      min(last_seen_at, now) - max(first_seen_at, window_start)
    in days, clamped >=0. Sum that across VINs, divide by 90 = avg daily count.
    """
    cur.execute(
        """
        WITH w AS (
          SELECT NOW() - INTERVAL '90 days' AS start_ts,
                 NOW()                       AS end_ts
        )
        SELECT di.dealer_id,
               di.year,
               di.make,
               di.model,
               SUM(
                 GREATEST(0,
                   EXTRACT(EPOCH FROM (
                     LEAST(COALESCE(di.sold_at, di.last_seen_at, w.end_ts), w.end_ts)
                     - GREATEST(di.first_seen_at, w.start_ts)
                   )) / 86400.0
                 )
               ) AS vin_days
          FROM dealer_inventory di, w
         WHERE di.dealer_id = ANY(%s)
           AND di.make IS NOT NULL
           AND di.model IS NOT NULL
           AND COALESCE(di.sold_at, di.last_seen_at, w.end_ts) >= w.start_ts
           AND di.first_seen_at <= w.end_ts
         GROUP BY di.dealer_id, di.year, di.make, di.model
        """,
        (dealer_ids,),
    )
    out = defaultdict(lambda: defaultdict(float))
    for d_id, yr, mk, md, vin_days in cur.fetchall():
        key = (year_bucket(yr), (mk or "").strip().upper(), (md or "").strip().upper())
        if not key[1] or not key[2]:
            continue
        out[d_id][key] += float(vin_days or 0) / 90.0
    return out


def format_ymm(key):
    yb, mk, md = key
    return f"{yb} {mk} {md}".strip()


def analyze_dealer(current, baseline):
    """Return (holes, surpluses) lists of (key, baseline_avg, current_count)."""
    all_keys = set(current.keys()) | set(baseline.keys())
    holes, surplus = [], []
    for k in all_keys:
        cur_n = current.get(k, 0)
        base = baseline.get(k, 0.0)
        # Hole
        if base >= 2 and cur_n <= max(0, base * 0.3):
            holes.append((k, base, cur_n))
        # Surplus
        if cur_n >= max(3, base * 1.5) and cur_n >= base + 2:
            surplus.append((k, base, cur_n))
    holes.sort(key=lambda x: -x[1])
    surplus.sort(key=lambda x: -(x[2] - x[1]))
    return holes[:5], surplus[:5]


def build_message(dealers, results):
    lines = ["EW Inventory Gap Scan (90d baseline)", ""]
    any_content = False
    for d_id, d_name in dealers:
        holes, surplus = results.get(d_id, ([], []))
        if not holes and not surplus:
            continue
        any_content = True
        lines.append(f"— {d_name} —")
        for key, base, cur_n in holes:
            lines.append(f"  HOLE  {format_ymm(key)}: now {cur_n} (baseline {base:.1f})")
        for key, base, cur_n in surplus:
            lines.append(f"  SURP  {format_ymm(key)}: now {cur_n} (baseline {base:.1f}, +{cur_n - base:.1f})")
        lines.append("")
    if not any_content:
        lines.append("(no holes or surpluses crossed thresholds)")
    return "\n".join(lines).rstrip()


def send_telegram(message):
    import requests
    # Telegram cap ~4096 chars; chunk if needed.
    chunks = [message[i:i + 3800] for i in range(0, len(message), 3800)] or [""]
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
            timeout=10,
        )
        r.raise_for_status()
    log.info(f"telegram sent ({len(message)} chars in {len(chunks)} chunk(s))")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print instead of telegram")
    args = ap.parse_args()

    if not args.dry_run:
        # also tee logs to file
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(logging.Formatter("%(asctime)s [gap-scan] %(message)s"))
        logging.getLogger().addHandler(fh)

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    dealers = fetch_portal_dealers(cur)
    dealer_ids = [d[0] for d in dealers]
    log.info(f"scanning {len(dealer_ids)} portal dealers")

    current = fetch_current_inventory(cur, dealer_ids)
    baseline = fetch_baseline(cur, dealer_ids)

    results = {}
    for d_id, _ in dealers:
        results[d_id] = analyze_dealer(current.get(d_id, {}), baseline.get(d_id, {}))

    message = build_message(dealers, results)

    if args.dry_run:
        print(message)
    else:
        send_telegram(message)

    conn.close()


if __name__ == "__main__":
    main()
