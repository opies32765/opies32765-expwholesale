"""score_live_bids.py (C1) — run the screener over live EW bids.

READ-ONLY. Writes nothing to the database and touches no app code. Per the EW hard rule that
nothing downstream may block enrichment, this is a pure reporting pass: if it fails, nothing
else is affected.

The screener answers what the price model cannot: not "what is this car worth" (v3 already
tracks MMR to within ~3%) but "is this bid worth making". Fitted on 20,171 LSL deals and
validated on 8,645 strictly-later ones, where BUY-flagged deals realised 7.9% gross vs 3.7%
for PASS, and PASS deals lost money 32% of the time vs 6% for BUY.

  venv/bin/python score_live_bids.py [--days 30] [--csv /tmp/screener_live.csv]
"""
import os, sys, json, argparse, statistics as st, collections
import psycopg2, psycopg2.extras

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=30)
ap.add_argument("--csv", default="/tmp/screener_live.csv")
ap.add_argument("--model", default="/opt/expwholesale/screener_model.json")
A = ap.parse_args()

# ---- load the fitted screener (JSON keys are "tier|band" strings) ----
raw = json.load(open(A.model))
MODEL = dict(base={tuple(k.split("|")): tuple(v) for k, v in raw["base"].items()},
             grand=raw["grand"], age=raw["age"], miles=raw["miles"],
             loss={tuple(k.split("|")): v for k, v in raw["loss"].items()})

TIERS = [(0.90, "1 <0.90"), (0.98, "2 0.90-0.98"), (1.02, "3 0.98-1.02"),
         (1.08, "4 1.02-1.08"), (99.0, "5 >1.08")]
def ratio_tier(r):
    for hi, lab in TIERS:
        if r < hi: return lab
    return TIERS[-1][1]
def price_band(v):
    return ("a <15k" if v < 15000 else "b 15-30k" if v < 30000 else "c 30-50k" if v < 50000
            else "d 50-100k" if v < 100000 else "e 100k+")
def age_tier(a):
    if a is None: return "unk"
    return "1 0-2yr" if a <= 2 else "2 3-5yr" if a <= 5 else "3 6-9yr" if a <= 9 else "4 10yr+"
def miles_tier(o):
    if not o: return "unk"
    return "1 <30k" if o < 30000 else "2 30-60k" if o < 60000 else "3 60-100k" if o < 100000 else "4 100k+"

def score(wholesale, bid, year, odo):
    if not wholesale or not bid or wholesale <= 0 or bid <= 0: return None
    r = bid / wholesale
    age = (2026 - year) if year else None
    base, n, sd = MODEL["base"].get((ratio_tier(r), price_band(bid)), (MODEL["grand"], 0, 6.0))
    exp = base + MODEL["age"].get(age_tier(age), 0) + MODEL["miles"].get(miles_tier(odo), 0)
    loss = MODEL["loss"].get((age_tier(age), miles_tier(odo)), 0.04)
    rec = "BUY" if exp >= 6.0 and loss <= 0.06 else ("PASS" if exp < 3.5 or loss > 0.08 else "THIN")
    # price discipline gates the verdict; a good age/mileage profile must not outvote a bad price
    if r > 1.08: rec = "PASS"
    elif r > 1.02 and rec == "BUY": rec = "THIN"
    return dict(ratio=round(r, 3), exp_gross_pct=round(exp, 1),
                exp_gross_dollars=int(bid * exp / 100), walk_away=int(wholesale * 1.02),
                loss_risk=round(100 * loss, 1), rec=rec, n_comparable=n)

DSN = os.environ.get("DATABASE_URL")
if not DSN:
    for ln in open("/etc/default/expwholesale-mcp"):
        if ln.strip().startswith("DATABASE_URL="):
            DSN = ln.strip().split("=", 1)[1].strip().strip('"').strip("'")
conn = psycopg2.connect(DSN, connect_timeout=10); conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
  SELECT b.id, b.year, b.make, b.model, b.mileage, b.ai_price, b.created_at::date AS d,
         v.mmr, a.actual_purchase_cost
    FROM bids b
    LEFT JOIN LATERAL (SELECT mmr FROM vauto_lookups x WHERE x.bid_id=b.id
                        ORDER BY looked_up_at DESC LIMIT 1) v ON TRUE
    LEFT JOIN ai_accuracy a ON a.bid_id = b.id
   WHERE b.created_at > now() - (%s || ' days')::interval
     AND v.mmr > 0 AND b.ai_price > 0
   ORDER BY b.id DESC""", (A.days,))
rows = [dict(r) for r in cur.fetchall()]
conn.close()

out, buckets = [], collections.defaultdict(list)
for r in rows:
    s = score(float(r["mmr"]), float(r["ai_price"]), r["year"], r["mileage"])
    if not s: continue
    rec = dict(bid=r["id"], date=str(r["d"]),
               vehicle=f"{r['year'] or ''} {r['make'] or ''} {r['model'] or ''}".strip(),
               miles=r["mileage"], mmr=int(r["mmr"]), ai_price=int(r["ai_price"]),
               actual=int(r["actual_purchase_cost"]) if r["actual_purchase_cost"] else "", **s)
    out.append(rec); buckets[s["rec"]].append(rec)

import csv as _csv
with open(A.csv, "w", newline="") as f:
    w = _csv.DictWriter(f, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)

print("=== SCREENER over %d live bids (last %d days) ===\n" % (len(out), A.days))
print("  %-6s %6s %8s %14s %12s %11s" % ("verdict", "n", "% of all", "med exp gross%", "med bid/MMR", "med loss risk"))
for k in ("BUY", "THIN", "PASS"):
    v = buckets.get(k, [])
    if v:
        print("  %-6s %6d %7.0f%% %13.1f%% %11.3f %10.1f%%"
              % (k, len(v), 100*len(v)/len(out), st.median([x["exp_gross_pct"] for x in v]),
                 st.median([x["ratio"] for x in v]), st.median([x["loss_risk"] for x in v])))

# where we have the outcome, does the verdict line up with reality?
known = [x for x in out if x["actual"] != ""]
if known:
    print("\n  --- of those, %d already have a KNOWN purchase price ---" % len(known))
    for k in ("BUY", "THIN", "PASS"):
        v = [x for x in known if x["rec"] == k]
        if v:
            over = sum(1 for x in v if x["ai_price"] > x["actual"])
            print("    %-5s n=%-4d  AI bid above what EW paid on %d (%.0f%%)"
                  % (k, len(v), over, 100*over/len(v)))
print("\n  csv -> %s" % A.csv)
