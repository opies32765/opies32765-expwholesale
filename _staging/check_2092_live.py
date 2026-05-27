"""Run the LIVE _compute_bid_matches for bid 2092 and see what comes back."""
import sys, psycopg2, psycopg2.extras
sys.path.insert(0, '/opt/expwholesale')
DSN = 'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'
conn = psycopg2.connect(DSN, cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

cur.execute("""SELECT b.id, b.year, b.make, b.model, b.trim, b.canon_trim,
                      b.color, b.mileage, b.asking_price, b.ymmt_id,
                      yc.model AS ymmt_model, yc.trim AS ymmt_trim
                 FROM bids b LEFT JOIN ymmt_catalog yc ON yc.id=b.ymmt_id
                WHERE b.id=2092""")
bid = dict(cur.fetchone())
print('BID 2092:', bid)
print(f"  ymmt_id={bid['ymmt_id']!r}  ymmt_model={bid['ymmt_model']!r}  ymmt_trim={bid['ymmt_trim']!r}")
print()

cur.execute("""SELECT id, name, portal_slug, buy_profile FROM dealers
               WHERE portal_slug IS NOT NULL AND buy_profile IS NOT NULL
                 AND portal_slug != 'ect' ORDER BY id""")
dealers = [dict(r) for r in cur.fetchall()]
print(f'dealers loaded: {len(dealers)}')

from app import _bp_score, _compute_bid_matches, _load_dealer_vins_owned
vins = _load_dealer_vins_owned(cur)
matches = _compute_bid_matches(bid, dealers, vins_by_dealer=vins)
print(f'\n_compute_bid_matches returned {len(matches)} matches')
for m in matches:
    print(f"  T{'1' if m['score']>=80 else '2'} {m['score']:>3} {m['name']:30s} -> {m['reason']}")

print('\n=== manual _bp_score for each dealer ===')
for d in dealers:
    try:
        s, why = _bp_score(bid, d['id'], d.get('buy_profile'), vins.get(d['id']))
    except Exception as e:
        s, why = None, f'ERR {e}'
    tag = f"T{'1' if s and s>=80 else '2'}" if s else 'skip'
    print(f"  {tag:>4} {(str(s) if s is not None else '-'):>4}  {d['name']:30s} -> {why}")
