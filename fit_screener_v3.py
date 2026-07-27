#!/usr/bin/env python3
"""fit_screener_v3.py — refit the bid-page deal screener on EW's REAL profit metric.

Run on C1:  python3 /opt/expwholesale/fit_screener_v3.py [--write]

WHAT WAS WRONG WITH v2
----------------------
1. WRONG METRIC. v2 was fit on `gross_dollars`, which is just sale_price - purchase_cost.
   EW's actual per-deal profit is `front_value` (it nets out recon, pack, fees). front_value
   runs ~55% of gross_dollars in every single year -- 2026: $1,551 vs $2,859. So the badge
   overstated what a deal is worth by roughly 80%.
2. WRONG WINDOW. v2 blended 2019-2026. The loss rate has fallen steadily (7.29% in 2019 ->
   2.36% in 2026 on front_value), so a 7-year blend is ~1.6x too pessimistic about risk while
   simultaneously being too optimistic about dollars. Both errors flattered the recommendation.

WHAT IS DELIBERATELY UNCHANGED
------------------------------
Same statistic (MEDIAN -- v2's grand_gross of 1750.0 is exactly the all-years median of
gross_dollars, confirmed), same tier definitions, same model shape, same decision logic.
Only the metric and the window move, so any difference in output is attributable.

THRESHOLDS ARE NOW RELATIVE, NOT ABSOLUTE
-----------------------------------------
v2 hardcoded "PASS if loss >= 0.07" and "BUY if loss <= 0.045" against a 3.92% base rate --
i.e. 1.79x and 1.15x the base. Hardcoding those numbers is what makes a model rot: when the
base rate falls to ~3.2%, "loss <= 0.045" becomes almost always true and everything turns
BUY. This version stores the MULTIPLES and derives the cutoffs from the fitted base rate, so
the screener keeps its meaning as the market moves.
"""
import json, os, sys, statistics as st, collections, datetime
import psycopg2, psycopg2.extras

WINDOW_START = os.environ.get("SCREENER_WINDOW_START", "2025-01-01")
TEST_FROM    = os.environ.get("SCREENER_TEST_FROM", "2026-01-01")   # time split for validation
MIN_CELL     = 40          # below this a tier falls back to the grand value
PASS_MULT    = 1.79        # v2: 0.07 / 0.0392
BUY_MULT     = 1.15        # v2: 0.045 / 0.0392
THIN_FRAC    = 0.60        # v2: exp_dollars < 0.6 * grand -> "thin dollars"
# Default connects over the LOCAL SOCKET as whatever user runs the script, so it works via
# `sudo -u postgres ... ` with peer auth and no password anywhere. Override with SCREENER_DSN.
DSN = os.environ.get("SCREENER_DSN", "dbname=expwholesale port=5433")

def band(v):
    return ("a <15k" if v < 15000 else "b 15-30k" if v < 30000 else "c 30-50k" if v < 50000
            else "d 50-100k" if v < 100000 else "e 100k+")
def miles(o):
    if not o: return "unk"
    return "1 <30k" if o < 30000 else "2 30-60k" if o < 60000 else "3 60-100k" if o < 100000 else "4 100k+"
def ratio_tier(r):
    # NB the 1.25 split. A single ">1.08" bucket hid real structure: 1.08-1.25 is genuinely
    # low risk, but 1.25-1.50 runs 4.29% loss -- the worst of the over-anchor range. Lumping
    # them together let a bid at 125% of MMR inherit the "safest tier" label.
    return ("1 <0.90" if r < .90 else "2 0.90-0.98" if r < .98 else "3 0.98-1.02" if r < 1.02
            else "4 1.02-1.08" if r < 1.08 else "5 1.08-1.25" if r < 1.25 else "6 >1.25")

def load():
    pw = os.environ.get("PGPASSWORD")
    conn = psycopg2.connect(DSN + (" password=%s" % pw if pw else ""))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        select sold_at, front_value, gross_dollars, purchase_cost, odometer, est_wholesale_price
          from lsl_training
         where sold_at >= %s
           and front_value is not null
           and purchase_cost > 0
           and est_wholesale_price > 0
    """, (WINDOW_START,))
    rows = []
    for r in cur:
        r = dict(r)
        r["ratio"] = float(r["purchase_cost"]) / float(r["est_wholesale_price"])
        r["fv"]    = float(r["front_value"])
        r["band"]  = band(float(r["purchase_cost"]))
        r["miles"] = miles(r["odometer"])
        r["rt"]    = ratio_tier(r["ratio"])
        rows.append(r)
    conn.close()
    return rows

def med(xs):   return float(st.median(xs))
def lossrate(rs): return sum(1 for r in rs if r["fv"] < 0) / len(rs) if rs else 0.0

def fit(rows):
    grand_gross = med([r["fv"] for r in rows])
    grand_loss  = lossrate(rows)
    by = lambda key: collections.defaultdict(list)

    band_gross = {}
    g = collections.defaultdict(list)
    for r in rows: g[r["band"]].append(r["fv"])
    for k, v in g.items():
        if len(v) >= MIN_CELL: band_gross[k] = round(med(v), 1)

    ratio_mult = {}
    g = collections.defaultdict(list)
    for r in rows: g[r["rt"]].append(r["fv"])
    for k, v in g.items():
        if len(v) < MIN_CELL: continue
        m = med(v) / grand_gross if grand_gross else 1.0
        # CAP over-anchor tiers at 1.0. Cars bought well ABOVE wholesale show a HIGHER median
        # front (the >1.25 bucket is full of exotics -- 1.50+ medians $1,800), so an uncapped
        # multiplier hands out a ~1.29x bonus for bidding 25% over MMR. That is a selection
        # effect, not a return to overpaying: EW paid up on cars it had good reason to want.
        # Encoding it would make the badge argue for overpaying on ANY car. Buying BELOW the
        # anchor keeps its uplift -- that cannot create the same perverse incentive.
        if k >= "4" and m > 1.0:
            m = 1.0
        ratio_mult[k] = m

    miles_loss = {}
    g = collections.defaultdict(list)
    for r in rows: g[r["miles"]].append(r)
    for k, v in g.items():
        if len(v) >= MIN_CELL: miles_loss[k] = lossrate(v)

    ratio_loss = {}
    g = collections.defaultdict(list)
    for r in rows: g[r["rt"]].append(r)
    for k, v in g.items():
        if len(v) >= MIN_CELL: ratio_loss[k] = lossrate(v)

    return {
        "_metric": "front_value",
        "_window_start": WINDOW_START,
        "_fit_rows": len(rows),
        "_fit_at": datetime.date.today().isoformat(),
        "grand_gross": round(grand_gross, 1),
        "grand_loss": grand_loss,
        "band_gross": band_gross,
        "ratio_gross_mult": ratio_mult,
        "miles_loss": miles_loss,
        "ratio_loss": ratio_loss,
        "pass_loss_mult": PASS_MULT,
        "buy_loss_mult": BUY_MULT,
        "thin_frac": THIN_FRAC,
    }

def score(M, purchase_cost, odometer, ratio):
    """Mirrors ew_screener_lib.score_bid exactly, so validation tests the shipped logic."""
    b = M["band_gross"].get(band(purchase_cost), M["grand_gross"])
    adj = M["ratio_gross_mult"].get(ratio_tier(ratio), 1.0)
    exp = int(b * adj)
    loss = max(M["miles_loss"].get(miles(odometer), M["grand_loss"]),
               M["ratio_loss"].get(ratio_tier(ratio), M["grand_loss"]))
    good = M["grand_gross"]
    pass_at = M["grand_loss"] * M["pass_loss_mult"]
    buy_at  = M["grand_loss"] * M["buy_loss_mult"]
    if loss >= pass_at or exp < good * M["thin_frac"]: rec = "PASS"
    elif exp >= good and loss <= buy_at:               rec = "BUY"
    else:                                             rec = "OK"
    if ratio > 1.08 and rec == "BUY": rec = "OK"
    return exp, loss, rec

def check_parity_with_lib():
    """The fit and the runtime MUST bucket identically.

    If they drift, every lookup misses and each bid silently falls back to the grand mean --
    no error, no log line, just a screener that quietly stopped discriminating. Cheap to
    check, so check it rather than trusting the comments.
    """
    try:
        sys.path.insert(0, "/opt/expwholesale")
        import ew_screener_lib as lib
    except Exception as e:
        print("!! could not import ew_screener_lib to verify parity: %s" % e); return
    bad = []
    for r in [0.5, 0.85, 0.89, 0.90, 0.95, 0.97, 0.98, 1.0, 1.01, 1.02, 1.05, 1.07,
              1.08, 1.10, 1.20, 1.24, 1.25, 1.30, 1.60, 2.0]:
        if lib._ratio_tier(r) != ratio_tier(r):
            bad.append((r, ratio_tier(r), lib._ratio_tier(r)))
    for v in [0, 15000, 29999, 30000, 59999, 60000, 99999, 100000, 250000]:
        if lib._miles_tier(v or None) != miles(v or None):
            bad.append((v, miles(v or None), lib._miles_tier(v or None)))
    for v in [1000, 14999, 15000, 29999, 30000, 49999, 50000, 99999, 100000, 500000]:
        if lib._price_band(v) != band(v):
            bad.append((v, band(v), lib._price_band(v)))
    if bad:
        print("!! TIER MISMATCH between fit and ew_screener_lib — model keys will not match:")
        for x in bad: print("     input=%s  fit=%r  lib=%r" % x)
        sys.exit(3)
    print("tier parity with ew_screener_lib: OK (ratio, miles, band)")

def main():
    check_parity_with_lib()
    rows = load()
    print("loaded %d deals sold since %s\n" % (len(rows), WINDOW_START))

    # ---- validation on a TIME split: fit on the past, judge on the future ----
    tr = [r for r in rows if str(r["sold_at"]) < TEST_FROM]
    te = [r for r in rows if str(r["sold_at"]) >= TEST_FROM]
    print("=== TIME-SPLIT VALIDATION  (train %d before %s -> test %d after) ===" % (len(tr), TEST_FROM, len(te)))
    if len(tr) >= 500 and len(te) >= 200:
        Mtr = fit(tr)
        scored = [(score(Mtr, float(r["purchase_cost"]), r["odometer"], r["ratio"]), r) for r in te]
        scored.sort(key=lambda t: t[0][0])
        n = len(scored); q = max(1, n // 5)
        print("  predicted-dollar quintiles vs ACTUAL median front_value:")
        print("    %-8s %6s %12s %12s" % ("quintile", "n", "predicted", "actual"))
        for i in range(5):
            chunk = scored[i*q:(i+1)*q] if i < 4 else scored[4*q:]
            if not chunk: continue
            print("    %-8s %6d %12s %12s" % (
                "Q%d" % (i+1), len(chunk),
                "$%d" % int(st.median([s[0][0] for s in chunk])),
                "$%d" % int(st.median([s[1]["fv"] for s in chunk]))))
        print("\n  actual loss rate by recommendation (this is the test that matters):")
        g = collections.defaultdict(list)
        for (exp, loss, rec), r in scored: g[rec].append(r)
        for rec in ("BUY", "OK", "PASS"):
            v = g.get(rec, [])
            if v:
                print("    %-5s n=%-5d actual loss %5.2f%%   median front $%d"
                      % (rec, len(v), 100*lossrate(v), int(st.median([x["fv"] for x in v]))))
    else:
        print("  not enough data either side of the split — skipped")

    # ---- final model on the whole window ----
    M = fit(rows)
    print("\n=== FITTED MODEL (v3, metric=front_value, window=%s+) ===" % WINDOW_START)
    print("  grand_gross (median front_value) : $%.0f      [v2 was $1750 of gross_dollars]" % M["grand_gross"])
    print("  grand_loss                       : %.2f%%     [v2 was 3.92%%]" % (100*M["grand_loss"]))
    print("  -> PASS when loss >= %.2f%% (%.2fx base) or exp < $%.0f"
          % (100*M["grand_loss"]*PASS_MULT, PASS_MULT, M["grand_gross"]*THIN_FRAC))
    print("  -> BUY  when exp >= $%.0f and loss <= %.2f%% (%.2fx base)"
          % (M["grand_gross"], 100*M["grand_loss"]*BUY_MULT, BUY_MULT))
    print("\n  band_gross (median front_value by purchase band):")
    for k in sorted(M["band_gross"]): print("     %-12s $%s" % (k, M["band_gross"][k]))
    nb = collections.Counter(r["band"] for r in rows)
    nm = collections.Counter(r["miles"] for r in rows)
    nr = collections.Counter(r["rt"] for r in rows)
    print("  miles_loss:")
    for k in sorted(M["miles_loss"]): print("     %-12s %.2f%%   n=%d" % (k, 100*M["miles_loss"][k], nm[k]))
    print("  ratio_loss:")
    for k in sorted(M["ratio_loss"]): print("     %-12s %.2f%%   n=%d" % (k, 100*M["ratio_loss"][k], nr[k]))
    print("  ratio_gross_mult (x median front_value):")
    for k in sorted(M["ratio_gross_mult"]):
        print("     %-12s %.3fx  -> $%-6d n=%d"
              % (k, M["ratio_gross_mult"][k], int(M["grand_gross"]*M["ratio_gross_mult"][k]), nr[k]))
    print("  band n: " + "  ".join("%s=%d" % (k, nb[k]) for k in sorted(nb)))

    # ---- what the mix of recommendations looks like on the recent book ----
    recs = collections.Counter()
    for r in rows:
        _, _, rec = score(M, float(r["purchase_cost"]), r["odometer"], r["ratio"])
        recs[rec] += 1
    print("\n  recommendation mix over the fit window: " +
          "  ".join("%s %d (%.0f%%)" % (k, v, 100*v/len(rows)) for k, v in recs.most_common()))

    if "--write" in sys.argv:
        # Overridable because the fit runs as `postgres` (peer auth, no password) and that
        # user cannot write into /opt/expwholesale. Write to /tmp, then install as root.
        out = os.environ.get("SCREENER_OUT", "/opt/expwholesale/screener_model_v3.json")
        json.dump(M, open(out, "w"), indent=1)
        print("\nwrote %s" % out)
    else:
        print("\n(dry run — pass --write to save screener_model_v3.json)")

if __name__ == "__main__":
    main()
