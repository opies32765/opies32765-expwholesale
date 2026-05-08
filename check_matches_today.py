"""One-shot: run dealer_match.find_dealer_matches() for every bid created today,
print a table showing which bids would trigger a partner-dealer match alert."""
from dealer_match import find_dealer_matches
import psycopg2
import psycopg2.extras

conn = psycopg2.connect(
    'postgresql://expuser:ExpWholesale2026!@localhost/expwholesale',
    cursor_factory=psycopg2.extras.RealDictCursor,
)
cur = conn.cursor()
cur.execute(
    "SELECT id, year, make, model, trim, trim_confidence "
    "FROM bids WHERE created_at::date = CURRENT_DATE ORDER BY id"
)
bids = cur.fetchall()
print(f'Checking {len(bids)} bids from today vs 3 partner dealers...\n')

for b in bids:
    res = find_dealer_matches(
        conn, b['year'], b['make'], b['model'],
        trim=b['trim'], trim_confidence=b['trim_confidence'] or 'low',
    )
    a = len(res.get('active', []))
    s = len(res.get('recent_sales', []))
    p = len(res.get('top_pitch', []))
    flag = '   <-- MATCH' if (a or s or p) else ''
    trim = b['trim'] or ''
    print(
        f'  bid {b["id"]:<3} | {(b["year"] or "?")!s:<4} {(b["make"] or "?"):<10} '
        f'{(b["model"] or "?"):<13} {trim[:20]:<20} | '
        f'active={a} recent_sales={s} pitch={p}{flag}'
    )

print('\n=== detail on any matches ===')
for b in bids:
    res = find_dealer_matches(
        conn, b['year'], b['make'], b['model'],
        trim=b['trim'], trim_confidence=b['trim_confidence'] or 'low',
    )
    active = res.get('active', [])
    if not active:
        continue
    print(f'\nBid {b["id"]} ({b["year"]} {b["make"]} {b["model"]} {b["trim"] or ""}):')
    for a in active[:4]:
        print(
            f'  @ {a.get("dealer_name")}: {a.get("year")} {a.get("make")} {a.get("model")} '
            f'{a.get("trim") or ""} · ${a.get("price") or "—"} · '
            f'{a.get("days_on_lot") or "?"}d on lot'
        )

conn.close()
