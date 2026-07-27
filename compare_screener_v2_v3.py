#!/usr/bin/env python3
"""compare_screener_v2_v3.py — what the badge will actually say differently, on REAL bids.

Run on C1:  sudo -u postgres venv/bin/python compare_screener_v2_v3.py

Loads both library versions by file path and scores the live bids that currently carry an
MMR, so the change is judged on the exact rows the operator is looking at rather than on
the training window.
"""
import importlib.util, os, sys, collections, statistics as st
import psycopg2, psycopg2.extras

def load(path, name):
    # Explicit SourceFileLoader: spec_from_file_location returns None for a path that does
    # not end in .py (the v2 copy is a .bak), because it cannot infer the loader.
    from importlib.machinery import SourceFileLoader
    spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

os.environ["EW_SCREENER_MODEL"] = "/opt/expwholesale/screener_model_v2.json"
v2 = load("/opt/expwholesale/ew_screener_lib.py.bak.20260727-v2", "scr_v2")
os.environ["EW_SCREENER_MODEL"] = "/opt/expwholesale/screener_model_v3.json"
v3 = load("/opt/expwholesale/ew_screener_lib.py", "scr_v3")

conn = psycopg2.connect(os.environ.get("SCREENER_DSN", "dbname=expwholesale port=5433"))
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
# MMR is not a column on bids — the bid page reads it from vauto_lookups (app.py ~4255),
# so the comparison must join the same way the render does.
cur.execute("""
    select b.id, b.year, b.make, b.model, b.mileage, v.mmr, b.ai_price
      from bids b
      join vauto_lookups v on v.bid_id = b.id
     where v.mmr is not null and v.mmr > 0
       and b.ai_price is not null and b.ai_price > 0
     order by b.id desc limit 400
""")
rows = [dict(r) for r in cur]
conn.close()
print("scoring %d live bids that have both an MMR and an AI price\n" % len(rows))

moves = collections.Counter()
dollar_deltas = []
examples = []
for r in rows:
    a = v2.score_bid(mmr=float(r["mmr"]), bid_price=float(r["ai_price"]), mileage=r["mileage"])
    b = v3.score_bid(mmr=float(r["mmr"]), bid_price=float(r["ai_price"]), mileage=r["mileage"])
    if not a or not b:
        continue
    moves[(a["recommendation"], b["recommendation"])] += 1
    dollar_deltas.append((a["expected_gross_dollars"], b["typical_front_dollars"]))
    if len(examples) < 12:
        examples.append((r, a, b))

print("=== RECOMMENDATION MOVES  (v2 -> v3) ===")
tot = sum(moves.values())
for (x, y), n in moves.most_common():
    tag = "  (unchanged)" if x == y else "  <-- CHANGED"
    print("   %-5s -> %-5s  %5d  %5.1f%%%s" % (x, y, n, 100*n/tot, tag))
changed = sum(n for (x, y), n in moves.items() if x != y)
print("   %d of %d bids change recommendation (%.1f%%)" % (changed, tot, 100*changed/tot))

print("\n=== DOLLAR FIGURE ON THE BADGE ===")
o = [d[0] for d in dollar_deltas]; n_ = [d[1] for d in dollar_deltas]
print("   v2 'exp gross'      median $%d   mean $%d" % (st.median(o), st.mean(o)))
print("   v3 'typical front'  median $%d   mean $%d" % (st.median(n_), st.mean(n_)))
print("   the badge now quotes %.0f%% of what it used to" % (100*st.mean(n_)/st.mean(o)))

print("\n=== SAMPLE BIDS ===")
print("   %-6s %-26s %8s %8s %6s   %-22s %-22s" % ("bid", "vehicle", "mmr", "bid", "miles", "v2 badge", "v3 badge"))
for r, a, b in examples:
    veh = ("%s %s %s" % (r["year"] or "", r["make"] or "", r["model"] or "")).strip()[:26]
    print("   %-6s %-26s %8s %8s %6s   %-22s %-22s" % (
        r["id"], veh, int(r["mmr"]), int(r["ai_price"]),
        (r["mileage"] or 0),
        "%s $%d" % (a["recommendation"], a["expected_gross_dollars"]),
        "%s $%d" % (b["recommendation"], b["typical_front_dollars"])))
