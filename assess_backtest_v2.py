"""9B assessment back-test v2 (2026-06-11) — with the cheap improvement levers:
(1) response_format json_object  -> forces valid JSON, recovers parse failures.
(2) anchor-clamp                 -> any 9B target >CLAMP_PCT from EW's computed
                                    market target (market_intel.target_buy / MMR)
                                    is pulled back into band, killing outliers.
Reports prior LLM, 9B raw, 9B clamped, AND EW's pure formula target — each vs the
actual LSL purchase_cost — so we can see how much the LLM layer even matters.
"""
import os, sys, json, base64, re, time, mimetypes, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import statistics as st
import psycopg2, psycopg2.extras
from ai_assessment_v2 import build_prompt, parse_response

CONC = int(os.environ.get("CONC", "6"))
LIMIT = int(os.environ.get("LIMIT", "0"))
MAXPHOTOS = int(os.environ.get("MAXPHOTOS", "0"))
CLAMP_PCT = float(os.environ.get("CLAMP_PCT", "0.18"))
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
cur.execute("""SELECT a.bid_id, a.actual_purchase_cost, a.ai_recommendation, l.gemini_raw_target
                 FROM ai_accuracy a JOIN ai_assessment_log l ON l.id = a.ai_assessment_log_id
                WHERE a.actual_purchase_cost > 0 AND a.ai_recommendation > 0
                ORDER BY a.bid_id DESC""")
rows = cur.fetchall(); conn.close()
if LIMIT: rows = rows[:LIMIT]
print(f"backtest v2: {len(rows)} vehicles, conc={CONC}, maxphotos={MAXPHOTOS}, clamp={CLAMP_PCT}", flush=True)

def _num(x):
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    if isinstance(x, dict):
        for k in ("target_buy", "mmr", "value", "avg", "amount"):
            if x.get(k): return _num(x[k])
    if isinstance(x, str):
        d = re.sub(r"[^\d.]", "", x)
        return float(d) if d else None
    return None

def anchor_of(mi):
    if not isinstance(mi, dict): return None
    for k in ("target_buy", "manheim", "lsl_avg_front_value", "rbook"):
        v = _num(mi.get(k))
        if v and v > 500: return v
    return None

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
            "max_tokens": 2000, "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
          headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY, "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        j = json.loads(r.read().decode())
    return (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""

done = 0; lock = threading.Lock(); t0 = time.time()
def work(row):
    global done
    bid_id = row["bid_id"]; actual = float(row["actual_purchase_cost"])
    prior = row["gemini_raw_target"] or row["ai_recommendation"]
    rec = {"bid_id": bid_id, "actual": actual, "prior": int(prior) if prior else None}
    try:
        bid, vauto, accu, ipkt, log, photos = load_ctx(bid_id)
        if not bid:
            rec["err"] = "no bid"
        else:
            rec["ymm"] = f'{bid.get("year")} {bid.get("make")} {bid.get("model")}'
            anchor = anchor_of(log.get("market_intel"))
            rec["anchor"] = int(anchor) if anchor else None
            prompt = build_prompt(bid, vauto=vauto, accutrade=accu, ipacket=ipkt, photos=photos,
                                  market_intel=log.get("market_intel"),
                                  dealer_intel=log.get("dealer_intel"),
                                  buyer_intel=log.get("buyer_intel"))
            raw = ask9b(prompt, imgs_for(photos))
            r = parse_response(raw)
            tb = r.get("target_buy") if r else None
            tb = int(tb) if tb else None
            rec["nine_b"] = tb
            if tb and anchor:
                lo, hi = anchor * (1 - CLAMP_PCT), anchor * (1 + CLAMP_PCT)
                rec["nine_b_clamped"] = int(min(max(tb, lo), hi))
            else:
                rec["nine_b_clamped"] = tb
    except Exception as e:
        rec["err"] = str(e)[:140]
    with lock:
        done += 1
        if done % 25 == 0:
            print(f"...{done}/{len(rows)}  {time.time()-t0:.0f}s", flush=True)
    return rec

results = []
with ThreadPoolExecutor(max_workers=CONC) as ex:
    for fut in as_completed([ex.submit(work, r) for r in rows]):
        results.append(fut.result())

json.dump(results, open("/tmp/assess_backtest_v2_report.json", "w"), default=str, indent=1)
def apct(pred, act): return abs(pred - act) / act * 100.0
def stats(key):
    xs = [x for x in results if x.get(key) and x.get("actual")]
    errs = [apct(x[key], x["actual"]) for x in xs]
    return xs, errs
print("\n==== BACK-TEST v2 (forced-JSON + clamp) vs ACTUAL ====", flush=True)
base = [x for x in results if x.get("prior") and x.get("actual")]
prior_errs = [apct(x["prior"], x["actual"]) for x in base]
print(f"vehicles with prior+actual: {len(base)}  (parse-OK 9B: {len([x for x in results if x.get('nine_b')])}/{len(results)})")
for label, key in (("PRIOR LLM", "prior"), ("9B raw", "nine_b"), ("9B CLAMPED", "nine_b_clamped"), ("EW formula", "anchor")):
    xs, errs = stats(key)
    if not errs: continue
    w10 = 100*sum(1 for e in errs if e <= 10)/len(errs)
    win = sum(1 for x in xs if x.get("prior") and apct(x[key], x["actual"]) < apct(x["prior"], x["actual"]))
    print(f"{label:11s}: n={len(errs):3d}  mean={st.mean(errs):4.1f}%  median={st.median(errs):4.1f}%  within10%={w10:3.0f}%  beats-prior={100*win/len(xs):3.0f}%")
print("report: /tmp/assess_backtest_v2_report.json", flush=True)
