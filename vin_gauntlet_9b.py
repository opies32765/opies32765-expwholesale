"""PURE-9B gauntlet (2026-06-11). Every texted-in image goes ONLY to the local 9B
via a DIRECT brain POST (temp 0, no Google Vision, no Gemini, no shim, no fallback).
No `import app`, so nothing can route around the 9B. Ground truth = bids.vin with an
INLINE ISO-3779 check digit. Measures exactly what the 9B reads by itself.
"""
import os, sys, json, time, re, glob, ast, base64, mimetypes, threading, urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2

CONC = int(os.environ.get("CONC", "6"))
URL = "https://brain.experience-wholesale.net/v1/chat/completions"
ROOT = "/opt/expwholesale/static/uploads/sms"

KEY = None
for ln in open("/etc/ew-brain.env"):
    if ln.startswith("EW_BRAIN_KEY="):
        KEY = ln.strip().split("=", 1)[1]

# Pull the prompts straight from app.py source — NO import, so no shim/gemini_call.
src = open("/opt/expwholesale/app.py").read()
P = {}
for n in ast.parse(src).body:
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name) and t.id in ("CARFAX_PROMPT", "VIN_PROMPT"):
                try: P[t.id] = ast.literal_eval(n.value)
                except Exception: pass
CARFAX = P.get("CARFAX_PROMPT"); VINP = P.get("VIN_PROMPT")

# Inline ISO-3779 check digit (no app dependency).
_T = {**{str(d): d for d in range(10)}, 'A':1,'B':2,'C':3,'D':4,'E':5,'F':6,'G':7,'H':8,
      'J':1,'K':2,'L':3,'M':4,'N':5,'P':7,'R':9,'S':2,'T':3,'U':4,'V':5,'W':6,'X':7,'Y':8,'Z':9}
_W = [8,7,6,5,4,3,2,10,0,9,8,7,6,5,4,3,2]
def vok(v):
    if not v or len(v) != 17: return False
    v = v.upper()
    if not re.match(r'^[A-HJ-NPR-Z0-9]{17}$', v): return False
    try: s = sum(_T[c]*w for c, w in zip(v, _W))
    except KeyError: return False
    r = s % 11; cd = 'X' if r == 10 else str(r)
    return v[8] == cd

def ask9b(prompt, b64, mime, maxtok):
    content = [{"type": "text", "text": prompt},
               {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}}]
    body = {"model": "ew-brain", "messages": [{"role": "user", "content": content}],
            "max_tokens": maxtok, "temperature": 0.0, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
          headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY,
                   "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        j = json.loads(r.read().decode())
    return (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""

def pick_vin(txt):
    t = txt.upper()
    cands = re.findall(r'"VIN"\s*:\s*"([A-Z0-9]{11,20})"', t) + re.findall(r'[A-HJ-NPR-Z0-9]{17}', t)
    for c in cands:
        if vok(c): return c
    return None

files = []
for ext in ("jpg", "jpeg", "png", "webp", "heic"):
    files += glob.glob(f"{ROOT}/*/*.{ext}")
def bidof(p):
    m = re.search(r"/sms/(\d+)/", p); return int(m.group(1)) if m else None
files = [f for f in files if bidof(f)]
files.sort(key=lambda p: (bidof(p), p), reverse=True)
print(f"PURE-9B gauntlet: {len(files)} images, conc={CONC}, prompts={list(P)}", flush=True)

bids = sorted({bidof(f) for f in files})
_dsn = os.environ.get("DATABASE_URL")
if not _dsn:
    for _ln in open("/etc/default/expwholesale-mcp"):
        _ln = _ln.strip()
        if _ln.startswith("DATABASE_URL="):
            _dsn = _ln.split("=", 1)[1].strip().strip('"').strip("'")
c = psycopg2.connect(_dsn, connect_timeout=10); cur = c.cursor()
cur.execute("SELECT id, vin FROM bids WHERE id = ANY(%s)", (bids,))
GT = {r[0]: ((r[1] or "").strip().upper() or None) for r in cur.fetchall()}; c.close()

done = 0; lock = threading.Lock(); t0 = time.time()
def work(path):
    global done
    bid = bidof(path); dbv = GT.get(bid); dbok = vok(dbv)
    rec = {"bid_id": bid, "file": path.split("/sms/")[-1], "db_vin": dbv}
    try:
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        vin = None; via = None
        if CARFAX:
            try:
                vin = pick_vin(ask9b(CARFAX, b64, mime, 800)); via = "carfax" if vin else None
            except Exception as e: rec["e1"] = str(e)[:80]
        if not vin and VINP:
            try:
                vin = pick_vin(ask9b(VINP, b64, mime, 220)); via = "vinprompt" if vin else None
            except Exception as e: rec["e2"] = str(e)[:80]
        rec["vin"] = vin; rec["via"] = via
    except Exception as e:
        rec["err"] = str(e)[:120]; rec["vin"] = None
    okv = vok(rec.get("vin"))
    if okv and dbok: rec["vcat"] = "MATCH" if rec["vin"] == dbv else "MISMATCH"
    elif okv and not dbok: rec["vcat"] = "RECOVERED"
    elif (not okv) and dbok: rec["vcat"] = "MISS"
    else: rec["vcat"] = "NONE"
    with lock:
        done += 1
        if done % 25 == 0:
            print(f"...{done}/{len(files)}  {time.time()-t0:.0f}s", flush=True)
    return rec

results = []
with ThreadPoolExecutor(max_workers=CONC) as ex:
    for fut in as_completed([ex.submit(work, f) for f in files]):
        results.append(fut.result())

json.dump(results, open("/tmp/vin_9b_report.json", "w"), default=str, indent=1)
byb = defaultdict(list)
for x in results: byb[x["bid_id"]].append(x)
ok = miss = nodb = 0; recov = set(); missb = []
for b, xs in byb.items():
    dbvin = any(vok(x.get("db_vin")) for x in xs)
    got = any(x["vcat"] in ("MATCH", "RECOVERED") for x in xs)
    if any(x["vcat"] == "RECOVERED" for x in xs): recov.add(b)
    if got: ok += 1
    elif dbvin: miss += 1; missb.append(b)
    else: nodb += 1
cc = Counter(x["vcat"] for x in results)
print("\n==== PURE-9B GAUNTLET (no Vision, no Gemini, direct brain) ====", flush=True)
print("images:", len(results), " elapsed:", f"{time.time()-t0:.0f}s")
print("per-image:", dict(cc))
print("prompt that won:", dict(Counter(x.get("via") for x in results if x.get("via"))))
withvin = ok + miss
print(f"per-BID: read-OK={ok}  MISS={miss}  no-db-vin={nodb}  total={len(byb)}")
print(f"per-bid read rate among bids WITH a vin: {ok}/{withvin} = {100*ok/withvin:.0f}%" if withvin else "no vins")
print(f"recovered (db had no vin): {len(recov)} -> {sorted(recov, reverse=True)}")
print("MISS bids:", sorted(missb, reverse=True))
print("\nreport: /tmp/vin_9b_report.json", flush=True)
