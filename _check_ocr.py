import sys, os, re
sys.path.insert(0, '.')
from app import _google_vision_ocr
import psycopg2, psycopg2.extras

conn = psycopg2.connect("postgresql://expuser:ExpWholesale2026!@localhost/expwholesale")
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""SELECT b.id, b.vin, b.mileage, b.created_at, p.url, p.vin_extracted
               FROM bids b LEFT JOIN bid_photos p ON p.bid_id=b.id
               WHERE b.created_at > NOW() - INTERVAL '15 minutes'
               ORDER BY b.created_at DESC, p.id ASC""")
rows = cur.fetchall()
if not rows:
    print("No bids in last 15 min")
for r in rows:
    bid_id = r['id']
    vin = r['vin']
    miles = r['mileage']
    url = r['url']
    print(f"Bid {bid_id}: stored VIN={vin}  Miles={miles}")
    if url and url.startswith('/static/uploads/'):
        path = os.path.join('.', url.lstrip('/'))
        if os.path.exists(path):
            with open(path, 'rb') as f:
                raw = f.read()
            text = _google_vision_ocr(raw)
            found_vin = None
            if text:
                m = re.search(r'\b[A-HJ-NPR-Z0-9]{17}\b', text.upper())
                if m:
                    found_vin = m.group(0)
            match = '✓ SAME' if found_vin == vin else ('✗ DIFFERENT' if found_vin else '✗ MISSED')
            print(f"  photo {url}")
            print(f"  Google Vision detected: {found_vin}  [{match}]")
conn.close()
