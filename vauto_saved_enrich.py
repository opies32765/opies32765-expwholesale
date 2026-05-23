"""vauto_saved_enrich.py — for each row in vauto_saved_appraisals that
lacks mmr/rbook, call vAuto BFF (priceGuides + competition/vehicles) to
pull the book values. Reuses enrich_bid_direct's underlying helpers.

Designed to be idempotent + resumable. Skips rows that already have mmr.
Concurrency=4. ~3,860 cars × 2-3s/call ≈ 30-45 min full pass.
"""
from __future__ import annotations
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2
import psycopg2.extras
import requests

from vauto_enrichment import _get_jar, VEHICLE_INFO_URL
from vauto_bff_direct import (
    fetch_competitive_set, fetch_price_guides,
    parse_competitive_set, parse_price_guides,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vauto-saved-enrich")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
)
ADD_COLS = """
ALTER TABLE vauto_saved_appraisals
  ADD COLUMN IF NOT EXISTS mmr_value INTEGER,
  ADD COLUMN IF NOT EXISTS mmr_odometer INTEGER,
  ADD COLUMN IF NOT EXISTS rbook_n INTEGER,
  ADD COLUMN IF NOT EXISTS rbook_median NUMERIC,
  ADD COLUMN IF NOT EXISTS rbook_p25 NUMERIC,
  ADD COLUMN IF NOT EXISTS rbook_p75 NUMERIC,
  ADD COLUMN IF NOT EXISTS enriched_at TIMESTAMP WITH TIME ZONE;
"""


def _ensure_schema():
    with psycopg2.connect(DB_URL) as c, c.cursor() as cur:
        cur.execute(ADD_COLS)
        c.commit()


def _fetch_vehicle_info(jar, vin: str, odometer: int):
    try:
        r = requests.post(
            VEHICLE_INFO_URL,
            json={"vin": vin, "odometer": int(odometer or 0), "odometerUom": "Miles"},
            headers=jar.get_headers(),
            cookies=jar.get_cookies(),
            timeout=15,
        )
        if r.status_code != 200:
            return None, None
        data = r.json()
        return data.get("vehicleInfo"), data.get("optionCodes")
    except Exception as e:
        log.warning(f"vehicleInfo {vin}: {e}")
        return None, None


def _enrich_one(row: dict, jar, appraisal_id_session: str) -> dict:
    """Pull MMR + rBook for one saved appraisal. Returns updated values dict."""
    vin = row.get("vin")
    odometer = row.get("odometer") or 0
    if not vin or len(vin) != 17:
        return {"appraisal_id": row["appraisal_id"], "skip": True, "reason": "no_vin"}

    vehicle, option_codes = _fetch_vehicle_info(jar, vin, odometer)
    if not vehicle or not vehicle.get("year"):
        return {"appraisal_id": row["appraisal_id"], "skip": True, "reason": "vehicleInfo_failed"}
    if odometer:
        vehicle = dict(vehicle)
        vehicle["odometer"] = int(odometer)

    cookies = jar.get_cookies()
    headers = jar.get_headers()
    out = {"appraisal_id": row["appraisal_id"]}

    # MMR (priceGuides) — keyword args (fetch_price_guides does NOT take option_codes)
    try:
        pg = fetch_price_guides(vehicle, cookies, headers,
                                appraisal_id=appraisal_id_session)
        if pg:
            parsed = parse_price_guides(pg) or {}
            mmr = parsed.get("manheim") or {}
            out["mmr_value"] = mmr.get("average_price") or mmr.get("average_auction_price")
            out["mmr_odometer"] = mmr.get("average_odometer")
    except Exception as e:
        log.warning(f"price_guides {vin}: {e}")

    # rBook (competitive_set) — keyword args; appraisal_id is NOT positional 4
    try:
        cs = fetch_competitive_set(vehicle, cookies, headers,
                                    appraisal_id=appraisal_id_session,
                                    option_codes=option_codes)
        if cs:
            parsed = parse_competitive_set(cs) or {}
            cs_rows = parsed.get("rows") or []
            prices = sorted([float(r["price"]) for r in cs_rows
                              if r.get("price")])
            out["rbook_n"] = len(cs_rows)
            if prices:
                n = len(prices)
                out["rbook_median"] = prices[n//2] if n % 2 else (prices[n//2-1] + prices[n//2]) / 2
                out["rbook_p25"] = prices[max(0, int(n*0.25))]
                out["rbook_p75"] = prices[min(n-1, int(n*0.75))]
    except Exception as e:
        log.warning(f"competitive_set {vin}: {e}")

    return out


UPDATE_SQL = """
UPDATE vauto_saved_appraisals SET
    mmr_value     = %(mmr_value)s,
    mmr_odometer  = %(mmr_odometer)s,
    rbook_n       = %(rbook_n)s,
    rbook_median  = %(rbook_median)s,
    rbook_p25     = %(rbook_p25)s,
    rbook_p75     = %(rbook_p75)s,
    enriched_at   = NOW()
WHERE appraisal_id = %(appraisal_id)s
"""


def run(only_unenriched: bool = True, concurrency: int = 4, max_n: int = 4000):
    _ensure_schema()
    jar = _get_jar()
    log.info(f"cookies age={jar.age_seconds():.0f}s")

    # Borrow an appraisal_id_session from any recent bid (same trick as
    # _fetch_live_vauto_with_vin uses).
    with psycopg2.connect(DB_URL) as c, c.cursor() as cur:
        cur.execute("""SELECT v.appraisal_url FROM vauto_lookups v
                        WHERE v.appraisal_url LIKE '%Id=%'
                        ORDER BY v.looked_up_at DESC LIMIT 1""")
        row = cur.fetchone()
    if not row:
        log.error("no appraisal_url found in vauto_lookups — can't proceed")
        return
    from urllib.parse import parse_qs, urlparse
    appraisal_id_session = parse_qs(urlparse(row[0]).query).get("Id", [None])[0]
    log.info(f"using session appraisalId: {appraisal_id_session[:20]}...")

    # Pull rows to enrich
    where = "enriched_at IS NULL" if only_unenriched else "TRUE"
    with psycopg2.connect(DB_URL) as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT appraisal_id, vin, odometer, year, make, model
                  FROM vauto_saved_appraisals
                 WHERE {where}
                   AND vin IS NOT NULL
                   AND LENGTH(vin) = 17
                   AND appraisal_last_modified_at > NOW() - INTERVAL '30 days'
                 ORDER BY appraisal_last_modified_at DESC
                 LIMIT %s
            """, (max_n,))
            rows = [dict(r) for r in cur.fetchall()]
    log.info(f"enriching {len(rows)} rows (only_unenriched={only_unenriched}) "
             f"with concurrency={concurrency}")

    t0 = time.monotonic()
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(_enrich_one, r, jar, appraisal_id_session): r for r in rows}
        with psycopg2.connect(DB_URL) as c:
            with c.cursor() as cur:
                for fut in as_completed(futs):
                    res = fut.result()
                    if not res.get("skip"):
                        cur.execute(UPDATE_SQL, {
                            "appraisal_id": res["appraisal_id"],
                            "mmr_value": res.get("mmr_value"),
                            "mmr_odometer": res.get("mmr_odometer"),
                            "rbook_n": res.get("rbook_n"),
                            "rbook_median": res.get("rbook_median"),
                            "rbook_p25": res.get("rbook_p25"),
                            "rbook_p75": res.get("rbook_p75"),
                        })
                    done += 1
                    if done % 50 == 0:
                        c.commit()
                        log.info(f"progress: {done}/{len(rows)} in {time.monotonic()-t0:.0f}s")
            c.commit()
    log.info(f"DONE: {done}/{len(rows)} in {time.monotonic()-t0:.0f}s")


if __name__ == "__main__":
    import sys
    max_n = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    run(max_n=max_n)
