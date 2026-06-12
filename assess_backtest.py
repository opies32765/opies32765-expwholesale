"""9B assessment back-test (2026-06-11). For each vehicle that was assessed on the
dash AND later found bought in LSL (ai_accuracy.actual_purchase_cost>0), replay EW's
REAL assessment prompt (ai_assessment_v2.build_prompt) built from that bid's STORED
point-in-time context (bid + vauto/accutrade/ipacket lookups + market/dealer/buyer
intel + photos) to the 9B (direct brain, temp 0, pure 9B — no Gemini), parse target_buy,
and compare |9B-actual| vs |prior_LLM(gemini_raw_target)-actual| against LSL purchase_cost.

Caveats (honest): the prompt TEMPLATE evolved since older assessments, and not every
live input (some vAuto/AccuTrade) is logged, so this is directional, not a byte-identical
model swap. MAXPHOTOS=0 = text/market-only (the template itself says price from market data).
"""
import os, sys, json, base64, re, time, mimetypes, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import statistics as st
import psycopg2, psycopg2.extras
from ai_assessment_v2 import build_prompt, parse_response

CONC = int(os.environ.get("CONC", "5"))
LIMIT = int(os.environ.get("LIMIT", "0"))         # 0 = all
MAXPHOTOS = int(os.environ.get("MAXPHOTOS", "0"))  # 0 = text/market-only
URL = "https://brain.experience-wholesale.net/v1/chat/completions"

KEY = None
for ln in open("/etc/ew-brain.env"):
    if ln.startswith("EW_BRAIN_KEY="): KEY = ln.strip().split("=", 1)[1]
DSN = os.environ.get("DATABASE_URL")
if not DSN:
    for ln in open("/etc/default/expwholesale-mcp"):
        if ln.strip().startswith("DATABASE_URL="):
            DSN = ln.strip().split("=", 1)[1].strip().strip('"').strip("'")

def db(): return psycopg2.connect(DSN, connect_timeout=10)

conn = db(); conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""SELECT a.bid_id, a.actual_purchase_cost, a.ai_recommendation,
                      l.gemini_raw_target, l.final_price
                 FROM ai_accuracy a JOIN ai_assessment_log l ON l.id = a.ai_assessment_log_id
                WHERE a.actual_purchase_cost > 0 AND a.ai_recommendation > 0
                ORDER BY a.bid_id DESC""")
rows = cur.fetchall(); conn.close()
if LIMIT: rows = rows[:LIMIT]
print(f"backtest: {len(rows)} vehicles, conc={CONC}, maxphotos={MAXPHOTOS}", flush=True)

def load_ctx(bid_id):
    c = db(); c.autocommit = True; cu = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    def one(q):
        cu.execute(q, (bid_id,)); r = cu.fetchone(); return dict(r) if r else None
    bid = one("SELECT * FROM bids WHERE id=%s")
    vauto = one("SELECT * FROM vauto_lookups WHERE bid_id=%s ORDER BY looked_up_at DESC LIMIT 1")
    accu = one("SELECT * FROM accutrade_lookups WHERE bid_id=%s ORDER BY looked_up_at DESC LIMIT 1")
    ipkt = one("SELECT * FROM ipacket_lookups WHERE bid_id=%s ORDER BY looked_up_at DESC LIMIT 1")
    cu.execute("SELECT market_intel,dealer_intel,buyer_intel FROM ai_assessment_log WHERE bid_id=%s ORDER BY created_at DESC LIMIT 1", (bid_id,))
    log = cu.fetchone() or {}
    cu.execute("SELECT * FROM bid_photos WHERE bid_id=%s ORDER BY id", (bid_id,))
    photos = [dict(r) for r in cu.fetchall()]
    c.close()
    return bid, vauto, accu, ipkt, dict(log), photos

def imgs_for(photos):
    out = []
    for p in photos[:MAXPHOTOS]:
        lp = p.get("local_path")
        if not lp: continue
        path = os.path.join("/opt/expwholesale", lp.lstrip("/"))
        if os.path.exists(path):
            mime = mimetypes.guess_type(path)[0] or "image/jpeg"
            out.append((mime, base64.b64encode(open(path, "rb").read()).decode()))
    return out

def ask9b(prompt, images):
    content = [{"type": "text", "text": prompt}]
    for mime, b64 in images:
        content.append({"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}})
    body = {"model": "ew-brain", "messages": [{"role": "user", "content": content}],
            "max_tokens": 2000, "temperature": 0.0, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
          headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY, "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        j = json.loads(r.read().decode())
    return (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""

done = 0; lock = threading.Lock(); t0 = time.time()
def work(row):
    global done
    bid_id = row["bid_id"]; actual = float(row["actual_purchase_cost"])
    prior = row["gemini_raw_target"] or row["ai_recommendation"] or row["final_price"]
    rec = {"bid_id": bid_id, "actual": actual, "prior": int(prior) if prior else None}
    try:
        bid, vauto, accu, ipkt, log, photos = load_ctx(bid_id)
        if not bid:
            rec["err"] = "no bid"
        else:
            rec["ymm"] = f'{bid.get("year")} {bid.get("make")} {bid.get("model")}'
            prompt = build_prompt(bid, vauto=vauto, accutrade=accu, ipacket=ipkt, photos=photos,
                                  market_intel=log.get("market_intel"),
                                  dealer_intel=log.get("dealer_intel"),
                                  buyer_intel=log.get("buyer_intel"))
            raw = ask9b(prompt, imgs_for(photos))
            r = parse_response(raw)
            tb = r.get("target_buy") if r else None
            rec["nine_b"] = int(tb) if tb else None
    except Exception as e:
        rec["err"] = str(e)[:140]
    with lock:
        done += 1
        if done % 20 == 0:
            print(f"...{done}/{len(rows)}  {time.time()-t0:.0f}s", flush=True)
    return rec

results = []
with ThreadPoolExecutor(max_workers=CONC) as ex:
    for fut in as_completed([ex.submit(work, r) for r in rows]):
        results.append(fut.result())

json.dump(results, open("/tmp/assess_backtest_report.json", "w"), default=str, indent=1)
pairs = [x for x in results if x.get("nine_b") and x.get("prior") and x.get("actual")]
def apct(pred, act): return abs(pred - act) / act * 100.0
prior_err = [apct(x["prior"], x["actual"]) for x in pairs]
nine_err = [apct(x["nine_b"], x["actual"]) for x in pairs]
nine_win = sum(1 for x in pairs if apct(x["nine_b"], x["actual"]) < apct(x["prior"], x["actual"]))
print("\n==== ASSESSMENT BACK-TEST: 9B vs prior LLM vs ACTUAL purchase ====", flush=True)
print(f"vehicles scored: {len(pairs)} / {len(results)} (rest = parse fail / error)")
if pairs:
    print(f"PRIOR LLM : mean abs%err={st.mean(prior_err):.1f}%  median={st.median(prior_err):.1f}%")
    print(f"9B (pure) : mean abs%err={st.mean(nine_err):.1f}%  median={st.median(nine_err):.1f}%")
    print(f"9B closer to actual on {nine_win}/{len(pairs)} = {100*nine_win/len(pairs):.0f}% of vehicles")
    for tag, errs in (("PRIOR", prior_err), ("9B   ", nine_err)):
        w3 = 100*sum(1 for e in errs if e <= 3)/len(errs)
        w5 = 100*sum(1 for e in errs if e <= 5)/len(errs)
        w10 = 100*sum(1 for e in errs if e <= 10)/len(errs)
        print(f"  {tag}: within3%={w3:.0f}%  within5%={w5:.0f}%  within10%={w10:.0f}%")
    print("\n-- sample (bid | ymm | actual | prior | 9B) --")
    for x in sorted(pairs, key=lambda z: z["bid_id"], reverse=True)[:12]:
        print(f"  {x['bid_id']} | {x.get('ymm')} | act ${x['actual']:,.0f} | prior ${x['prior']:,} | 9B ${x['nine_b']:,}")
print("report: /tmp/assess_backtest_report.json", flush=True)
