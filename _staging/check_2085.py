"""Investigate bid 2085 scoring."""
import sys
sys.path.insert(0, '/opt/expwholesale')
import psycopg2, psycopg2.extras
conn = psycopg2.connect('postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale',
                        cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

# Get bid 2085 + auto-tag if needed via resolve_ymmt
cur.execute("""SELECT b.id, b.year, b.make, b.model, b.trim, b.color, b.mileage,
                      b.asking_price, b.ymmt_id, yc.model AS ymmt_model, yc.trim AS ymmt_trim
                 FROM bids b LEFT JOIN ymmt_catalog yc ON yc.id=b.ymmt_id WHERE b.id=2085""")
bid = dict(cur.fetchone())
print(f"Bid 2085: {bid['year']} {bid['make']} {bid['model']} / {bid['trim']}")
print(f"  ymmt_id={bid['ymmt_id']} ymmt_model={bid['ymmt_model']!r} ymmt_trim={bid['ymmt_trim']!r}")

# Resolve right now (simulate what auto-tag would do)
from ymmt_match import resolve_ymmt
r = resolve_ymmt(bid['year'], bid['make'], bid['model'], bid['trim'], db_conn=conn)
print(f"\nresolve_ymmt -> id={r.get('ymmt_id')} model={r.get('model')!r} trim={r.get('trim')!r} src={r.get('source')} conf={r.get('confidence')}")
print(f"  reason: {r.get('reason')}")

# What catalog rows exist for 2025 Ford F-150 STX?
cur.execute("""SELECT id, year, model, trim FROM ymmt_catalog
               WHERE UPPER(make)='FORD' AND model='F-150' AND year BETWEEN 2024 AND 2026
               ORDER BY year DESC, trim""")
print("\n=== Ford F-150 catalog rows 2024-2026 ===")
for row in cur.fetchall():
    print(f"  id={row['id']} {row['year']} {row['model']} / {row['trim']}")

# Score against all dealers
cur.execute("SELECT id, name, portal_slug, buy_profile FROM dealers WHERE portal_slug IS NOT NULL AND buy_profile IS NOT NULL AND portal_slug != 'ect' ORDER BY id")
dealers = [dict(r) for r in cur.fetchall()]

from app import _bp_score, _load_dealer_vins_owned
vins = _load_dealer_vins_owned(cur)

# Use the resolved info as if bid was tagged
bid_for_score = dict(bid)
if r.get('ymmt_id'):
    bid_for_score['ymmt_id'] = r['ymmt_id']
    bid_for_score['ymmt_model'] = r.get('model')
    bid_for_score['ymmt_trim'] = r.get('trim')

print("\n=== scoring vs all 14 dealers ===")
for d in dealers:
    try:
        s, why = _bp_score(bid_for_score, d['id'], d.get('buy_profile'), vins.get(d['id']))
    except Exception as e:
        s, why = None, f"ERR {e}"
    if s is not None and s >= 60:
        print(f"  T{'1' if s>=80 else '2'} {s:>3}  {d['name']:32s} -> {why}")
    elif s is not None:
        print(f"  -    {s:>3}  {d['name']:32s} -> {why}")
    else:
        print(f"  skip      {d['name']:32s} -> {why}")
