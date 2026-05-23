"""vauto_saved_appraisals_pipeline.py — paginate vAuto's
/Va/Appraisal/ListData.ashx, parse the 93-column row format, upsert into
vauto_saved_appraisals table. Run as cron + manual.

Endpoint discovered via HAR capture 2026-05-22.
"""
from __future__ import annotations
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import requests

from vauto_enrichment import _get_jar

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vauto-saved")

LIST_URL = "https://provision.vauto.app.coxautoinc.com/Va/Appraisal/ListData.ashx"
PAGE_SIZE = 100  # vAuto's max we know works; default is 20, tested 100 worked

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
)

DDL = """
CREATE TABLE IF NOT EXISTS vauto_saved_appraisals (
    appraisal_id            TEXT PRIMARY KEY,
    vin                     TEXT,
    stock_number            TEXT,
    year                    INTEGER,
    make                    TEXT,
    model                   TEXT,
    series                  TEXT,
    series_detail           TEXT,
    odometer                INTEGER,
    appraised_value         NUMERIC,
    initial_appraised_value NUMERIC,
    original_appraised_value NUMERIC,
    appraiser_name          TEXT,
    initial_appraiser_name  TEXT,
    salesperson_name        TEXT,
    dealer_name             TEXT,
    entity_id               TEXT,
    retail_wholesale        CHAR(1),
    red_black               TEXT,
    cost_to_market          NUMERIC,
    appraisal_status        TEXT,
    appraisal_status_name   TEXT,
    appraisal_created_at    TIMESTAMP WITH TIME ZONE,
    appraisal_last_modified_at TIMESTAMP WITH TIME ZONE,
    exterior_color          TEXT,
    body_description        TEXT,
    drivetrain_type         TEXT,
    days_in_inventory       INTEGER,
    appraisal_score         INTEGER,
    profit_objective        NUMERIC,
    profit_time_alignment   TEXT,
    inventory_id            TEXT,
    fetched_at              TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vauto_saved_ymm
    ON vauto_saved_appraisals (year, make, model, series);
CREATE INDEX IF NOT EXISTS idx_vauto_saved_lastmod
    ON vauto_saved_appraisals (appraisal_last_modified_at DESC);
CREATE INDEX IF NOT EXISTS idx_vauto_saved_vin
    ON vauto_saved_appraisals (vin);
"""


def _ensure_schema():
    with psycopg2.connect(DB_URL) as c, c.cursor() as cur:
        cur.execute(DDL)
        c.commit()


def _parse_listdata_response(body: str) -> dict:
    """vAuto returns JS-flavored JSON with `new Date(NNN)` literals.
    Strip them to integer ms-epoch before parsing.
    """
    cleaned = re.sub(r"new Date\((-?\d+)\)", r"\1", body)
    return json.loads(cleaned)


def _epoch_to_dt(ms):
    if ms is None or ms == 0:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
    except Exception:
        return None


def _row_to_record(cols: list[str], row: list) -> dict:
    """Map 93-column row to our subset of fields."""
    d = dict(zip(cols, row))
    return {
        "appraisal_id":              d.get("Id"),
        "vin":                       d.get("Vin"),
        "stock_number":              d.get("StockNumber"),
        "year":                      d.get("ModelYear"),
        "make":                      d.get("Make"),
        "model":                     d.get("Model"),
        "series":                    d.get("Series"),
        "series_detail":             d.get("SeriesDetail"),
        "odometer":                  d.get("Odometer"),
        "appraised_value":           d.get("AppraisedValue"),
        "initial_appraised_value":   d.get("InitialAppraisedValue"),
        "original_appraised_value":  d.get("OriginalAppraisedValue"),
        "appraiser_name":            d.get("AppraiserName"),
        "initial_appraiser_name":    d.get("InitialAppraiserName"),
        "salesperson_name":          d.get("SalespersonName"),
        "dealer_name":               d.get("DealerName"),
        "entity_id":                 d.get("EntityId"),
        "retail_wholesale":          d.get("RetailWholesale"),
        "red_black":                 d.get("RedBlack"),
        "cost_to_market":            d.get("CostToMarket"),
        "appraisal_status":          d.get("AppraisalStatus"),
        "appraisal_status_name":     d.get("AppraisalStatusName"),
        "appraisal_created_at":      _epoch_to_dt(d.get("AppraisalCreated")),
        "appraisal_last_modified_at":_epoch_to_dt(d.get("AppraisalLastModified")),
        "exterior_color":            d.get("AppraisalExteriorColor"),
        "body_description":          d.get("AppraisalBodyDescription"),
        "drivetrain_type":           d.get("AppraisalDrivetrainType"),
        "days_in_inventory":         d.get("DaysInInventory"),
        "appraisal_score":           d.get("AppraisalScore"),
        "profit_objective":          d.get("ProfitObjective"),
        "profit_time_alignment":     d.get("ProfitTimeAlignment"),
        "inventory_id":              d.get("InventoryId"),
    }


UPSERT_SQL = """
INSERT INTO vauto_saved_appraisals (
    appraisal_id, vin, stock_number, year, make, model, series, series_detail,
    odometer, appraised_value, initial_appraised_value, original_appraised_value,
    appraiser_name, initial_appraiser_name, salesperson_name, dealer_name, entity_id,
    retail_wholesale, red_black, cost_to_market,
    appraisal_status, appraisal_status_name,
    appraisal_created_at, appraisal_last_modified_at,
    exterior_color, body_description, drivetrain_type,
    days_in_inventory, appraisal_score, profit_objective, profit_time_alignment,
    inventory_id, fetched_at
) VALUES (
    %(appraisal_id)s, %(vin)s, %(stock_number)s, %(year)s, %(make)s, %(model)s,
    %(series)s, %(series_detail)s, %(odometer)s, %(appraised_value)s,
    %(initial_appraised_value)s, %(original_appraised_value)s,
    %(appraiser_name)s, %(initial_appraiser_name)s, %(salesperson_name)s,
    %(dealer_name)s, %(entity_id)s, %(retail_wholesale)s, %(red_black)s,
    %(cost_to_market)s, %(appraisal_status)s, %(appraisal_status_name)s,
    %(appraisal_created_at)s, %(appraisal_last_modified_at)s,
    %(exterior_color)s, %(body_description)s, %(drivetrain_type)s,
    %(days_in_inventory)s, %(appraisal_score)s, %(profit_objective)s,
    %(profit_time_alignment)s, %(inventory_id)s, NOW()
)
ON CONFLICT (appraisal_id) DO UPDATE SET
    vin = EXCLUDED.vin,
    stock_number = EXCLUDED.stock_number,
    year = EXCLUDED.year,
    make = EXCLUDED.make,
    model = EXCLUDED.model,
    series = EXCLUDED.series,
    series_detail = EXCLUDED.series_detail,
    odometer = EXCLUDED.odometer,
    appraised_value = EXCLUDED.appraised_value,
    initial_appraised_value = EXCLUDED.initial_appraised_value,
    original_appraised_value = EXCLUDED.original_appraised_value,
    appraiser_name = EXCLUDED.appraiser_name,
    initial_appraiser_name = EXCLUDED.initial_appraiser_name,
    salesperson_name = EXCLUDED.salesperson_name,
    dealer_name = EXCLUDED.dealer_name,
    entity_id = EXCLUDED.entity_id,
    retail_wholesale = EXCLUDED.retail_wholesale,
    red_black = EXCLUDED.red_black,
    cost_to_market = EXCLUDED.cost_to_market,
    appraisal_status = EXCLUDED.appraisal_status,
    appraisal_status_name = EXCLUDED.appraisal_status_name,
    appraisal_created_at = EXCLUDED.appraisal_created_at,
    appraisal_last_modified_at = EXCLUDED.appraisal_last_modified_at,
    exterior_color = EXCLUDED.exterior_color,
    body_description = EXCLUDED.body_description,
    drivetrain_type = EXCLUDED.drivetrain_type,
    days_in_inventory = EXCLUDED.days_in_inventory,
    appraisal_score = EXCLUDED.appraisal_score,
    profit_objective = EXCLUDED.profit_objective,
    profit_time_alignment = EXCLUDED.profit_time_alignment,
    inventory_id = EXCLUDED.inventory_id,
    fetched_at = NOW()
"""


def fetch_page(jar, start: int, day_span: int = 30) -> dict:
    body = (
        f"start={start}&limit=&"
        f"sorts=%5B%7B%22sort%22%3A%22AppraisalLastModified%22%2C%22dir%22%3A%22DESC%22%7D%5D&"
        f"_pageSize={PAGE_SIZE}&_sortBy=AppraisalLastModified%20DESC&"
        f"LastModifiedDaySpan={day_span}&"
        f"_mandatoryFilters=&QuickSearch=&"
        f"gridSrcName=appraisalDetail&switchReport="
    )
    headers = dict(jar.get_headers())
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    headers["Accept"] = "*/*"
    headers["Referer"] = "https://provision.vauto.app.coxautoinc.com/Va/Appraisal/List.aspx"
    r = requests.post(
        LIST_URL,
        data=body,
        headers=headers,
        cookies=jar.get_cookies(),
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ListData.ashx HTTP {r.status_code}: {r.text[:200]}")
    return _parse_listdata_response(r.text)


def run(day_span: int = 30, max_pages: int = 50):
    _ensure_schema()
    jar = _get_jar()
    log.info(f"cookies captured_at={jar.captured_at} age={jar.age_seconds():.0f}s")

    total_inserted = 0
    total_seen = 0
    page_n = 0
    start = 0
    while page_n < max_pages:
        page_n += 1
        t0 = time.monotonic()
        try:
            data = fetch_page(jar, start=start, day_span=day_span)
        except Exception as e:
            log.error(f"page start={start} failed: {e}")
            break
        rows = data.get("rows") or []
        cols = data.get("columns") or []
        total_count = data.get("totalCount") or 0
        if not rows:
            log.info(f"page {page_n} start={start}: empty — done")
            break
        log.info(f"page {page_n} start={start}: {len(rows)} rows (of {total_count} total) in {(time.monotonic()-t0)*1000:.0f}ms")

        # Map + upsert
        with psycopg2.connect(DB_URL) as c:
            with c.cursor() as cur:
                for row in rows:
                    rec = _row_to_record(cols, row)
                    if not rec.get("appraisal_id"):
                        continue
                    cur.execute(UPSERT_SQL, rec)
                    total_inserted += 1
            c.commit()
        total_seen += len(rows)
        start += len(rows)
        if start >= total_count:
            log.info(f"reached totalCount={total_count}")
            break
        # Be polite — small pause between pages
        time.sleep(0.5)

    log.info(f"DONE: pages={page_n} seen={total_seen} upserted={total_inserted}")
    return total_inserted


if __name__ == "__main__":
    day_span = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    max_pages = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    n = run(day_span=day_span, max_pages=max_pages)
    print(f"upserted {n} appraisals")
