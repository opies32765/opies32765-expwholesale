"""ab_auction_comps.py — does the 9B actually USE the auction comps?

Runs ON C1. Read-only: no DB writes, no service changes.

Injection is not answer. The CWK 4B ignored facts it was handed, and this same
brain invents corroborating detail for fields it was never given. So before
trusting the comps block we ask the model the SAME bid twice -- once with the
section populated, once with it empty -- and measure whether the number moves
and whether the reasoning mentions the sales.

Usage:  python3 ab_auction_comps.py [N]
"""
import json, os, re, sys, urllib.request
import psycopg2, psycopg2.extras

from ai_assessment_v2 import build_prompt, parse_response
from comps_lookup import for_bid, _conn

URL = 'https://brain.experience-wholesale.net/v1/chat/completions'
KEY = [l.strip().split('=', 1)[1] for l in open('/etc/ew-brain.env')
       if l.startswith('EW_BRAIN_KEY=')][0]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10


def ask(prompt):
    body = {'model': 'ew-brain',
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': prompt}]}],
            'max_tokens': 1200, 'temperature': 0.0,
            'response_format': {'type': 'json_object'},
            'chat_template_kwargs': {'enable_thinking': False}}
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + KEY,
                 'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())['choices'][0]['message']['content']


def num(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


CITES = re.compile(r'auction|hammer|sold at|comp|no-?sale|resistance', re.I)

conn = _conn()
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
    SELECT b.*, a.actual_purchase_cost, a.ai_recommendation
      FROM ai_accuracy a JOIN bids b ON b.id = a.bid_id
     WHERE a.actual_purchase_cost > 0 AND b.mileage > 0
       AND b.created_at >= DATE '2026-07-24'
  ORDER BY b.created_at DESC
""")
rows = [dict(r) for r in cur.fetchall()]

print(f'scanning {len(rows)} bought bids for ones WITH comps...', flush=True)
subjects = []
for b in rows:
    ac = for_bid(b, conn=conn)
    if ac['ok'] and ac['sold']:
        subjects.append((b, ac))
    if len(subjects) >= N:
        break
print(f'running A/B on {len(subjects)}\n', flush=True)

moved = cited = 0
deltas = []
print(f"{'bid':>6} {'car':<34} {'paid':>9} {'no comps':>9} {'w/ comps':>9} {'move':>8}  cites?")
for b, ac in subjects:
    try:
        p_off = build_prompt(b, auction_comps=None)
        p_on = build_prompt(b, auction_comps=ac)
        r_off = parse_response(ask(p_off)) or {}
        r_on = parse_response(ask(p_on)) or {}
        t_off, t_on = num(r_off.get('target_buy')), num(r_on.get('target_buy'))
        if t_off is None or t_on is None:
            continue
        d = t_on - t_off
        deltas.append(d)
        moved += 1 if d else 0
        why = (r_on.get('reasoning') or '')
        c = bool(CITES.search(why))
        cited += 1 if c else 0
        car = f"{b['year']} {b['make']} {b['model']} {b.get('trim') or ''}"[:34]
        comps = [x['price'] for x in ac['sold']]
        print(f"{b['id']:>6} {car:<34} {int(b['actual_purchase_cost']):>9,} "
              f"{t_off:>9,} {t_on:>9,} {d:>+8,}  {'yes' if c else 'NO'}"
              f"   comps {min(comps):,}-{max(comps):,}")
    except Exception as e:
        print(f"  bid {b['id']} err: {str(e)[:70]}")

n = len(deltas)
if n:
    import statistics as st
    print(f"\n  n={n}   number moved on {moved}/{n}   reasoning cites comps on {cited}/{n}")
    print(f"  mean move {st.mean(deltas):+,.0f}   median {st.median(deltas):+,.0f}   "
          f"max |move| {max(abs(d) for d in deltas):,}")
conn.close()
