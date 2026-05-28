#!/usr/bin/env python3
"""inventory_gap_scan.py — nightly EW inventory holes vs surplus Telegram digest.

Live analysis logic lives in inventory_gap_lib.py (shared with the
public /inventory-gaps web page). This script just wires the lib to
Telegram + cron logging, and surfaces the top sub-config per gap.

Usage:
  python3 inventory_gap_scan.py            # live (Telegram)
  python3 inventory_gap_scan.py --dry-run  # print only
"""
import argparse
import logging
import os

import psycopg2

from inventory_gap_lib import (
    fetch_portal_dealers,
    fetch_current_inventory,
    fetch_baseline,
    analyze_dealer,
    format_ymm,
    format_config,
)

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


def _top_config_line(label, cfgs):
    if not cfgs:
        return None
    parts = [f"{n}× {format_config(c)}" for c, n in cfgs[:2]]
    return f"      {label}: " + " | ".join(parts)


def build_message(dealers, results):
    lines = ["EW Inventory Gap Scan (90d sales velocity + granular configs)", ""]
    any_content = False
    for d_id, d_name in dealers:
        holes, surplus = results.get(d_id, ([], []))
        if not holes and not surplus:
            continue
        any_content = True
        lines.append(f"— {d_name} —")
        for key, base, cur_n, scfg, ccfg in holes:
            lines.append(f"  HOLE  {format_ymm(key)}: in stock {cur_n}, sold {base} in 90d")
            sub = _top_config_line("sold mix", scfg)
            if sub: lines.append(sub)
        for key, base, cur_n, scfg, ccfg in surplus:
            lines.append(f"  SURP  {format_ymm(key)}: in stock {cur_n}, sold {base} in 90d")
            sub = _top_config_line("stocked mix", ccfg)
            if sub: lines.append(sub)
        lines.append("")
    if not any_content:
        lines.append("(no holes or surpluses crossed thresholds)")
    return "\n".join(lines).rstrip()


def send_telegram(message):
    import requests
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
