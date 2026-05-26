"""Hit live /api/bids and report whether match_dealers is in the payload."""
import json, urllib.request
r = urllib.request.urlopen('http://localhost:9000/api/bids?status=all', timeout=10)
d = json.load(r)
bids = d.get('bids', [])
print(f"top-level keys: {list(d.keys())}")
print(f"bids returned: {len(bids)}")
got, empty, missing = 0, 0, 0
for b in bids[:30]:
    md = b.get('match_dealers')
    bid_id = b.get('id')
    ymm = f"{b.get('year')} {b.get('make')} {b.get('model')}"
    if md is None:
        missing += 1
        tag = 'KEY MISSING'
    elif md == []:
        empty += 1
        tag = '[]'
    else:
        got += 1
        tag = ', '.join(f"{m.get('name')}({m.get('score')})" for m in md)
    print(f"  bid={bid_id:<6} {ymm:<32} -> {tag}")
print(f"\nsummary: {got} with matches, {empty} empty, {missing} missing the key")
