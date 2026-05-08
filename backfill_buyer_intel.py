"""
backfill_buyer_intel.py — re-run lsl_buyer_match.find_same_ymm_deals for every
ai_assessment_log row whose buyer_intel still has the old shape (recent_buyers /
top_pitch_buyers).  Replaces buyer_intel JSONB with the new shape (deals).
"""
import json
import os
import sys

sys.path.insert(0, "/opt/expwholesale")

import psycopg2
import psycopg2.extras

from lsl_buyer_match import find_same_ymm_deals

DB_URL = os.environ.get("DATABASE_URL", "postgresql://expuser:ExpWholesale2026!@localhost/expwholesale")

conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

# Find old-shape rows. ai_assessment_log links to bids by bid_id.
cur.execute("""
    SELECT a.id, a.bid_id, a.buyer_intel,
           b.year, b.make, b.model, b.mileage
    FROM ai_assessment_log a
    JOIN bids b ON b.id = a.bid_id
    WHERE a.buyer_intel IS NOT NULL
""")
rows = cur.fetchall()
print(f"scanned {len(rows)} ai_assessment_log rows with buyer_intel")

old_shape = []
for r in rows:
    bi = r["buyer_intel"]
    if isinstance(bi, str):
        try:
            bi = json.loads(bi)
        except Exception:
            continue
    if not isinstance(bi, dict):
        continue
    if "top_pitch_buyers" in bi or "recent_buyers" in bi:
        old_shape.append(r)

print(f"old-shape rows to backfill: {len(old_shape)}")

ok = 0
fail = 0
for i, r in enumerate(old_shape, 1):
    try:
        bi_new = find_same_ymm_deals(
            r["year"], r["make"], r["model"],
            mileage=r.get("mileage"),
        )
        cur.execute(
            "UPDATE ai_assessment_log SET buyer_intel = %s::jsonb WHERE id = %s",
            (json.dumps(bi_new), r["id"]),
        )
        ok += 1
        if i % 50 == 0:
            conn.commit()
            print(f"  {i}/{len(old_shape)} ... ok={ok} fail={fail}")
    except Exception as e:
        fail += 1
        print(f"  bid {r['bid_id']} ({r['year']} {r['make']} {r['model']}) FAIL: {e}")

conn.commit()
print(f"\nbackfill done — updated {ok} rows, {fail} failures")
