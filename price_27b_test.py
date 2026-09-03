"""price_27b_test.py — would the 27B price better than the 9B?

Runs ON C1. Read-only: no DB writes, no service changes.

OPERATOR'S PROPOSAL 2026-09-03: "keep everything the 9b does but only offload
the price to the 27b." So the prompt is IDENTICAL — same build_prompt, same
auction comps, same enrichment. Only the model answering it changes.

Worth testing even though it contradicts the 2026-08-23 eval ("9B WINS pricing,
27B REGRESSES VIN"): that eval predates auction comps. Pricing now involves
READING a table of real sales, and the 27B measured 100% vs the 9B's 65% on
vAuto read-back. The task changed, so the old answer may not hold.

Three columns, all scored against what EW actually paid:
    9B  no comps   (the old baseline, from /tmp/ab_comps_60.log)
    9B  w/ comps   (what is live today, same log)
    27B w/ comps   (this run)

⚠ The 27B on :18001 is Anna's LIVE voice brain (--max-num-seqs 8). Requests are
issued ONE AT A TIME with a pause between, so a pricing backtest cannot make a
customer wait on hold.
"""
import json, re, sys, time, urllib.request
import statistics as st
import psycopg2, psycopg2.extras

from ai_assessment_v2 import build_prompt, parse_response
from comps_lookup import for_bid, _conn

URL27 = 'http://127.0.0.1:18001/v1/chat/completions'
MODEL27 = 'anna'
GAP = 1.5          # seconds between calls — shared capacity with Anna
LOG = '/tmp/ab_comps_60.log'


def ask27(prompt):
    body = {'model': MODEL27,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': 1200, 'temperature': 0.0,
            'response_format': {'type': 'json_object'},
            'chat_template_kwargs': {'enable_thinking': False}}
    req = urllib.request.Request(
        URL27, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.loads(r.read().decode())['choices'][0]['message']['content']


def num(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


# ---- prior 9B results, keyed by bid id -----------------------------------
rx = re.compile(r'^\s*(\d+)\s+(.+?)\s{2,}([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([+-][\d,]+)\s+(yes|NO)')
prior = {}
for ln in open(LOG):
    m = rx.match(ln)
    if m:
        i, car, paid, off, on, mv, cite = m.groups()
        f = lambda x: int(x.replace(',', '').replace('+', ''))
        prior[int(i)] = (car.strip(), f(paid), f(off), f(on))
print('9B results loaded for %d bids' % len(prior), flush=True)

conn = _conn()
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT * FROM bids WHERE id = ANY(%s)", (list(prior),))
bids = {r['id']: dict(r) for r in cur.fetchall()}

rows = []
print('\n%6s %-30s %9s %9s %9s %9s' %
      ('bid', 'vehicle', 'EW paid', '9B-noC', '9B+comps', '27B+comps'), flush=True)
for bid_id, (car, paid, off, on9) in sorted(prior.items(), reverse=True):
    b = bids.get(bid_id)
    if not b:
        continue
    try:
        ac = for_bid(b, conn=conn)
        prompt = build_prompt(b, auction_comps=ac)
        r = parse_response(ask27(prompt)) or {}
        on27 = num(r.get('target_buy'))
        if on27 is None:
            print('%6d %-30s  (27B returned no target_buy)' % (bid_id, car[:30]), flush=True)
            continue
        rows.append((bid_id, car, paid, off, on9, on27))
        print('%6d %-30s %9s %9s %9s %9s' %
              (bid_id, car[:30], format(paid, ','), format(off, ','),
               format(on9, ','), format(on27, ',')), flush=True)
        time.sleep(GAP)
    except Exception as e:
        print('%6d  err: %s' % (bid_id, str(e)[:70]), flush=True)
        time.sleep(GAP)

if rows:
    def mae(idx):
        return st.mean([abs(r[idx] - r[2]) for r in rows])

    def med(idx):
        return st.median([abs(r[idx] - r[2]) for r in rows])

    def pctsd(idx):
        return st.pstdev([(r[idx] - r[2]) / r[2] for r in rows if r[2] > 0])

    print('\n%d cars scored against what EW actually paid' % len(rows))
    print('%-14s %10s %10s %10s' % ('', 'MAE', 'median', 'sd of %err'))
    for lab, i in (('9B  no comps', 3), ('9B  w/ comps', 4), ('27B w/ comps', 5)):
        print('%-14s %10s %10s %9.1f%%' %
              (lab, '$' + format(int(mae(i)), ','), '$' + format(int(med(i)), ','),
               pctsd(i) * 100))
    w27 = sum(1 for r in rows if abs(r[5] - r[2]) < abs(r[4] - r[2]))
    print('\n  27B beats the live 9B on %d/%d cars' % (w27, len(rows)))
conn.close()
