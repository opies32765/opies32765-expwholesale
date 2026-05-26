"""Replicate exactly what app.py:2536 does on the dashboard polling endpoint:
load profiles for the 2 allowlisted dealers, score the 20 most recent bids,
print which ones would show a green ✓ badge.
"""
import sys
sys.path.insert(0, '/opt/expwholesale')
import psycopg2, psycopg2.extras
DB = 'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'

SLUGS = ('txtcharlie', 'nuccioautogroup')

conn = psycopg2.connect(DB, cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

# Mirror app.py:2536 exactly
cur.execute("""SELECT id, name, portal_slug, buy_profile
                 FROM dealers
                WHERE portal_slug = ANY(%s)
                  AND buy_profile IS NOT NULL""", (list(SLUGS),))
dealers = [dict(r) for r in cur.fetchall()]
print(f"Profiles loaded: {[d['name'] for d in dealers]}")

# Load _bp_score from app.py
from app import _bp_score, _compute_bid_matches, _load_dealer_vins_owned
vins = _load_dealer_vins_owned(cur)

cur.execute("""SELECT id, year, make, model, trim, color, mileage, vin,
                      asking_price, status, created_at
                 FROM bids
                ORDER BY id DESC LIMIT 20""")
bids = cur.fetchall()

print(f"\n{'BID':<6} {'YMM':<32} {'STATUS':<14} {'MATCHES'}")
print('-' * 100)
for b in bids:
    matches = _compute_bid_matches(dict(b), dealers, vins_by_dealer=vins)
    ymm = f"{b['year']} {b['make']} {b['model']}"[:31]
    if matches:
        m_str = ', '.join(f"{m['name']}({m['score']})" for m in matches)
    else:
        m_str = '(no match)'
    print(f"{b['id']:<6} {ymm:<32} {(b['status'] or '?'):<14} {m_str}")
