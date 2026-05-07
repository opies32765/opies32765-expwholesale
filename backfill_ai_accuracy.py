#!/usr/bin/env python3
"""
Backfill ai_accuracy from LSL crm.db.

Runs ON Contabo 1 (62.146.226.100):
  /opt/livesaleslog/crm.db   -- SQLite, 28k+ wholesale deals over 5yrs
  localhost:5433/expwholesale -- Postgres, ai_accuracy table

Maps every LSL wholesale acquisition with a non-null buy cost into ai_accuracy
using bid_id = -lsl_deal_id (negative -> never collides with real bids 1,2,3,...).

Idempotent: ON CONFLICT (bid_id) DO UPDATE refreshes cost + purchased_at.

Source mapping (verified by inspecting LSL schema 2026-05-06):
  deals.id              -> lsl_deal_id (positive)
  deals.code            -> lsl_deal_code
  deals.vin_no          -> vin
  deals.purchase_cost   -> actual_purchase_cost (REAL -> int dollars)
  deals.sold_at         -> actual_purchased_at (ISO8601 text -> tstz)
  deals.vehicle_info    -> year (parsed from leading "YYYY ...")
  inventory.vehicle_make_name (joined on stock_no)  -> make
  inventory.group_model_name / vehicle_series_name  -> model (make-prefix stripped)
  inventory.usage       -> mileage

The deal->inventory join uses stock_no (deal.stock_no = inventory.stock_no).
inventory.deal_id is unreliable (mostly mismatched), but stock_no is 1:1.

Filters:
  vin_no IS NOT NULL AND length(vin_no)=17
  purchase_cost BETWEEN 500 AND 500000
  parsed year BETWEEN 2000 AND 2027
"""
import os
import re
import sqlite3
import sys
from collections import Counter

import psycopg2
import psycopg2.extras

LSL_DB = "/opt/livesaleslog/crm.db"
PG_DSN = "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale"

YEAR_RE = re.compile(r"^\s*(20[0-2][0-9])\b")

# Common LSL make typos -> normalized form expected by ai_accuracy / Gemini
MAKE_NORMALIZE = {
    "Bmw": "BMW",
    "Bmw ": "BMW",
    "Gmc": "GMC",
    "Mini": "MINI",
    "Mercedes Benz": "Mercedes-Benz",
}


def normalize_make(make: str | None) -> str | None:
    if not make:
        return None
    m = make.strip()
    return MAKE_NORMALIZE.get(m, m)


def strip_make_prefix(model: str | None, make: str | None) -> str | None:
    """group_model_name embeds make as prefix ("Mercedes-Benz GLE-Class ...").
    Strip it so model column is just "GLE-Class ...".
    """
    if not model:
        return None
    model = model.strip()
    if not make:
        return model or None
    # try exact + normalized make prefixes
    candidates = {make, normalize_make(make) or make}
    for prefix in candidates:
        if prefix and model.lower().startswith(prefix.lower() + " "):
            return model[len(prefix) + 1 :].strip() or None
    return model or None


def parse_year(vehicle_info: str | None) -> int | None:
    if not vehicle_info:
        return None
    m = YEAR_RE.match(vehicle_info)
    if not m:
        return None
    y = int(m.group(1))
    return y if 2000 <= y <= 2027 else None


def parse_sold_at(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    return s or None


def fetch_lsl_rows() -> list[tuple]:
    """Return list of mapped rows ready for ai_accuracy insert."""
    if not os.path.exists(LSL_DB):
        sys.exit(f"FATAL: LSL DB not found at {LSL_DB}")

    sq = sqlite3.connect(LSL_DB)
    sq.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT
                d.id           AS deal_id,
                d.code         AS deal_code,
                d.vin_no       AS vin,
                d.vehicle_info AS vehicle_info,
                d.purchase_cost AS purchase_cost,
                d.sold_at      AS sold_at,
                i.vehicle_make_name AS make,
                COALESCE(i.group_model_name, i.vehicle_series_name) AS model,
                CAST(i.usage AS INTEGER) AS mileage
            FROM deals d
            LEFT JOIN inventory i ON i.stock_no = d.stock_no
            WHERE d.vin_no IS NOT NULL
              AND length(d.vin_no) = 17
              AND d.purchase_cost IS NOT NULL
              AND d.purchase_cost BETWEEN 500 AND 500000
        """
        cur = sq.execute(sql)

        rows: list[tuple] = []
        skipped_no_year = 0
        skipped_dup_id = 0
        seen_ids: set[int] = set()

        for r in cur:
            deal_id = r["deal_id"]
            if deal_id in seen_ids:
                # stock_no LEFT JOIN can produce dupes if inventory has multiple rows
                skipped_dup_id += 1
                continue
            seen_ids.add(deal_id)

            year = parse_year(r["vehicle_info"])
            if year is None:
                skipped_no_year += 1
                continue

            make = normalize_make(r["make"])
            model = strip_make_prefix(r["model"], r["make"])
            cost = int(round(float(r["purchase_cost"])))
            mileage = r["mileage"] if r["mileage"] and r["mileage"] > 0 else None

            rows.append(
                (
                    -int(deal_id),               # bid_id (negative)
                    r["vin"].strip().upper(),    # vin
                    year,                        # year
                    make,                        # make
                    model,                       # model
                    mileage,                     # mileage
                    cost,                        # actual_purchase_cost
                    parse_sold_at(r["sold_at"]), # actual_purchased_at
                    int(deal_id),                # lsl_deal_id (positive)
                    r["deal_code"],              # lsl_deal_code
                )
            )

        print(f"LSL scan: kept {len(rows)} rows, "
              f"skipped {skipped_no_year} (year unparseable), "
              f"{skipped_dup_id} (duplicate deal id from stock_no join)")
        return rows
    finally:
        sq.close()


UPSERT_SQL = """
    INSERT INTO ai_accuracy (
        bid_id, vin, year, make, model, mileage,
        actual_purchase_cost, actual_purchased_at,
        lsl_deal_id, lsl_deal_code
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (bid_id) DO UPDATE SET
        vin = EXCLUDED.vin,
        year = EXCLUDED.year,
        make = EXCLUDED.make,
        model = EXCLUDED.model,
        mileage = EXCLUDED.mileage,
        actual_purchase_cost = EXCLUDED.actual_purchase_cost,
        actual_purchased_at = EXCLUDED.actual_purchased_at,
        lsl_deal_id = EXCLUDED.lsl_deal_id,
        lsl_deal_code = EXCLUDED.lsl_deal_code
"""


def upsert(rows: list[tuple]) -> None:
    if not rows:
        print("nothing to upsert")
        return

    pg = psycopg2.connect(PG_DSN)
    pg.autocommit = False
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ai_accuracy WHERE bid_id < 0")
            before_synthetic = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM ai_accuracy")
            before_total = cur.fetchone()[0]

            psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=500)

            cur.execute("SELECT COUNT(*) FROM ai_accuracy WHERE bid_id < 0")
            after_synthetic = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM ai_accuracy")
            after_total = cur.fetchone()[0]

            cur.execute(
                """
                SELECT make, COUNT(*) AS n
                FROM ai_accuracy WHERE bid_id < 0
                GROUP BY make ORDER BY n DESC LIMIT 5
                """
            )
            top_makes = cur.fetchall()

        pg.commit()
    finally:
        pg.close()

    inserted = after_synthetic - before_synthetic
    updated = len(rows) - inserted
    print(
        f"Upserted {len(rows)} rows "
        f"(new={inserted}, updated={updated}). "
        f"ai_accuracy total: {before_total} -> {after_total}."
    )
    print("Top 5 makes by count: " + ", ".join(f"{m}={n}" for m, n in top_makes))


def main() -> None:
    rows = fetch_lsl_rows()
    # quick sanity preview
    if rows:
        print("First 3 rows preview (year/make/model/cost):")
        for r in rows[:3]:
            print(f"  {r[2]} {r[3]} {r[4]!r} ${r[6]:,} (deal {r[8]})")
    upsert(rows)


if __name__ == "__main__":
    main()
