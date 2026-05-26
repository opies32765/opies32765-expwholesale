"""Verify YMMT_MATCH_2026_05_26 patch produces correct results."""
import sys
sys.path.insert(0, '/opt/expwholesale')
import psycopg2, psycopg2.extras
DB = 'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'
conn = psycopg2.connect(DB, cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

from app import _bp_score, _compute_bid_matches, _load_dealer_vins_owned

# All 15 dealers now (no allowlist)
cur.execute("SELECT id, name, portal_slug, buy_profile FROM dealers WHERE portal_slug IS NOT NULL AND buy_profile IS NOT NULL ORDER BY id")
dealers = [dict(r) for r in cur.fetchall()]
print(f"dealers scored against: {len(dealers)}")
vins = _load_dealer_vins_owned(cur)

# Pull recent bids with their ymmt info
cur.execute("""SELECT b.id, b.year, b.make, b.model, b.trim, b.color, b.mileage, b.vin,
                      b.asking_price, b.ymmt_id, yc.model AS ymmt_model, yc.trim AS ymmt_trim
                 FROM bids b LEFT JOIN ymmt_catalog yc ON yc.id = b.ymmt_id
                 WHERE b.id IN (2025, 2026, 2022, 2014, 2013, 2008)
                 ORDER BY b.id DESC""")
for b in cur.fetchall():
    b = dict(b)
    print(f"\n=== Bid {b['id']}: {b['year']} {b['make']} {b['model']} / {b['trim']}")
    print(f"    ymmt: model={b['ymmt_model']!r} trim={b['ymmt_trim']!r}")
    matches = _compute_bid_matches(b, dealers, vins_by_dealer=vins)
    if matches:
        for m in matches:
            print(f"    ✓ {m['name']:30s} score={m['score']}  {m['reason'][:70]}")
    else:
        print(f"    (no matches)")
