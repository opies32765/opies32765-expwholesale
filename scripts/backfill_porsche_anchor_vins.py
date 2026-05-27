#!/opt/expwholesale/venv/bin/python3
"""backfill_porsche_anchor_vins.py — Phase 1 unblocker for the Porsche
arbitrage scanner.

For every (year, 'Porsche', model, trim) in ymmt_catalog that does NOT yet
have a status='found' row in ymmt_vin_cache, call ew_mcp.find_vin_for_ymm
and cache the outcome. Writes a status='not_found' row when nothing can be
located so the nightly job doesn't re-attempt the same dead-ends every run.

Re-run safe: idempotent against ymmt_vin_cache PK
(year, make, model, trim) using INSERT ... ON CONFLICT.

Run:
    sudo bash -c 'set -a; . /etc/default/expwholesale-mcp; set +a;
                  /opt/expwholesale/venv/bin/python3 \
                  /opt/expwholesale/scripts/backfill_porsche_anchor_vins.py'
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback

# Ensure ew_mcp can be imported as a module
sys.path.insert(0, "/opt/expwholesale")

import psycopg2
import psycopg2.extras

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
)

# Hard requirement before we import ew_mcp (it raises at import time
# without the bearer token).
if not os.environ.get("MCP_BEARER_TOKEN"):
    sys.stderr.write(
        "MCP_BEARER_TOKEN missing — source /etc/default/expwholesale-mcp first.\n"
    )
    sys.exit(2)

import ew_mcp  # noqa: E402  — must come after env check


MISSING_SQL = """
SELECT yc.year, yc.make, yc.model, yc.trim
  FROM ymmt_catalog yc
  LEFT JOIN ymmt_vin_cache v
    ON v.year = yc.year
   AND lower(v.make)  = lower(yc.make)
   AND lower(v.model) = lower(yc.model)
   AND lower(coalesce(v.trim, '')) = lower(coalesce(yc.trim, ''))
   AND v.status = 'found'
 WHERE lower(yc.make) = 'porsche'
   AND v.vin IS NULL
 ORDER BY yc.year, yc.model, yc.trim
"""

UPSERT_FOUND_SQL = """
INSERT INTO ymmt_vin_cache
       (year, make, model, trim, vin, source, status, found_at, attempts, last_try_at)
VALUES (%s,   %s,   %s,    %s,   %s,  %s,     'found', now(),  1,        now())
ON CONFLICT (year, make, model, trim) DO UPDATE
   SET vin         = EXCLUDED.vin,
       source      = EXCLUDED.source,
       status      = 'found',
       found_at    = now(),
       attempts    = ymmt_vin_cache.attempts + 1,
       last_try_at = now()
"""

UPSERT_NOTFOUND_SQL = """
INSERT INTO ymmt_vin_cache
       (year, make, model, trim, vin,  source, status,      attempts, last_try_at)
VALUES (%s,   %s,   %s,    %s,   NULL, NULL,  'not_found',  1,        now())
ON CONFLICT (year, make, model, trim) DO UPDATE
   SET status      = CASE WHEN ymmt_vin_cache.status = 'found'
                          THEN ymmt_vin_cache.status
                          ELSE 'not_found' END,
       attempts    = ymmt_vin_cache.attempts + 1,
       last_try_at = now()
"""


async def main() -> int:
    t_start = time.monotonic()
    with psycopg2.connect(DB_URL) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(MISSING_SQL)
            missing = cur.fetchall()

    total = len(missing)
    print(f"[backfill] {total} Porsche trims missing an anchor VIN")
    if not total:
        print("[backfill] nothing to do — done")
        return 0

    found = 0
    not_found = 0
    errors = 0
    source_counter: dict[str, int] = {}
    sample_misses: list[str] = []

    for i, row in enumerate(missing, start=1):
        year = int(row["year"])
        make = row["make"]
        model = row["model"]
        trim = row["trim"] or ""
        label = f"{year} {make} {model} {trim}".strip()

        try:
            result = await ew_mcp.find_vin_for_ymm(year, make, model, trim)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"[{i:3}/{total}] ERR  {label}: {type(exc).__name__}: {exc}",
                  flush=True)
            traceback.print_exc(limit=2)
            continue

        vin = (result or {}).get("vin")
        source = (result or {}).get("source")

        try:
            with psycopg2.connect(DB_URL) as conn:
                with conn.cursor() as cur:
                    if vin and len(vin) == 17:
                        cur.execute(
                            UPSERT_FOUND_SQL,
                            (year, make, model, trim, vin, source),
                        )
                        found += 1
                        source_counter[source or "?"] = (
                            source_counter.get(source or "?", 0) + 1
                        )
                        print(
                            f"[{i:3}/{total}] OK   {label} -> {vin} "
                            f"(via {source})",
                            flush=True,
                        )
                    else:
                        cur.execute(
                            UPSERT_NOTFOUND_SQL, (year, make, model, trim)
                        )
                        not_found += 1
                        if len(sample_misses) < 5:
                            errs = (result or {}).get("errors") or []
                            sample_misses.append(
                                f"{label}  err={errs[:2]}"
                            )
                        print(
                            f"[{i:3}/{total}] MISS {label}  errors="
                            f"{(result or {}).get('errors')}",
                            flush=True,
                        )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"[{i:3}/{total}] DB-ERR {label}: {exc}", flush=True)

    elapsed = time.monotonic() - t_start
    print()
    print("=" * 60)
    print(f"[backfill] attempted : {total}")
    print(f"[backfill] found     : {found}")
    print(f"[backfill] not_found : {not_found}")
    print(f"[backfill] errors    : {errors}")
    print(f"[backfill] elapsed   : {elapsed:.1f}s")
    if source_counter:
        print("[backfill] sources   :")
        for k, v in sorted(source_counter.items(), key=lambda kv: -kv[1]):
            print(f"             {k:24s} {v}")
    if sample_misses:
        print("[backfill] sample misses:")
        for m in sample_misses:
            print(f"             {m}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
