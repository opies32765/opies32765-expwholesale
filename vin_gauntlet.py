"""Full production gauntlet (2026-06-11): run the REAL extract_vin_from_file chain
(9B Pro + Google Vision + Flash + carfax fallback + retry, all temp-0) on EVERY
texted-in intake image, parallel, per-bid scored with winning-layer attribution.
The definitive 'what production reads' number + a repeatable regression gate.
"""
import os, sys, json, time, re, glob, io, contextlib, mimetypes, threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2, psycopg2.extras

import app  # installs local_brain_shim; gives the real extractors
from app import extract_vin_from_file, vin_check_digit_valid
try:
    from app import extract_mileage_from_file
except Exception:
    extract_mileage_from_file = None

CONC = int(os.environ.get("CONC", "5"))
ROOT = "/opt/expwholesale/static/uploads/sms"
files = []
for ext in ("jpg", "jpeg", "png", "webp", "heic"):
    files += glob.glob(f"{ROOT}/*/*.{ext}")
def _bidof(p):
    m = re.search(r"/sms/(\d+)/", p); return int(m.group(1)) if m else None
files = [f for f in files if _bidof(f)]
files.sort(key=lambda p: (_bidof(p), p), reverse=True)
print(f"gauntlet: {len(files)} images, conc={CONC}", flush=True)

bids = sorted({_bidof(f) for f in files})
c = psycopg2.connect(os.environ["DATABASE_URL"]); cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT id, vin, mileage FROM bids WHERE id = ANY(%s)", (bids,))
GT = {r["id"]: r for r in cur.fetchall()}; c.close()

def _n(v): return (str(v).strip().upper() if v else "") or None
def _v(v): return bool(v and len(v) == 17 and vin_check_digit_valid(v))

done = 0; lock = threading.Lock(); t0 = time.time()
def work(path):
    global done
    bid = _bidof(path); gt = GT.get(bid, {})
    dbv = _n(gt.get("vin")); dbok = _v(dbv)
    rec = {"bid_id": bid, "file": path.split("/sms/")[-1], "db_vin": dbv}
    try:
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        img = open(path, "rb").read()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            vin = extract_vin_from_file(img, mime)
            miles = extract_mileage_from_file(img, mime) if extract_mileage_from_file else None
        log = buf.getvalue()
        m = re.search(r"\[OCR\] VIN via ([^\(:]+)", log)
        rec["via"] = m.group(1).strip() if m else None
        vin = _n(vin)
        if vin:
            mm = re.search(r"[A-HJ-NPR-Z0-9]{17}", vin); vin = mm.group(0) if mm else vin
        rec["vin"] = vin; rec["miles"] = miles
    except Exception as e:
        rec["err"] = str(e)[:160]; rec["vin"] = None
    vok = _v(rec.get("vin")); rec["vin_valid"] = vok
    if vok and dbok: rec["vcat"] = "MATCH" if rec["vin"] == dbv else "MISMATCH"
    elif vok and not dbok: rec["vcat"] = "RECOVERED"
    elif (not vok) and dbok: rec["vcat"] = "MISS"
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

json.dump(results, open("/tmp/vin_gauntlet_report.json", "w"), default=str, indent=1)
byb = defaultdict(list)
for x in results: byb[x["bid_id"]].append(x)
ok = miss = nodb = 0; rec_bids = set(); miss_bids = []
for b, xs in byb.items():
    dbvin = any(_v(x.get("db_vin")) for x in xs)
    got = any(x["vcat"] in ("MATCH", "RECOVERED") for x in xs)
    if any(x["vcat"] == "RECOVERED" for x in xs): rec_bids.add(b)
    if got: ok += 1
    elif dbvin: miss += 1; miss_bids.append(b)
    else: nodb += 1
cc = Counter(x["vcat"] for x in results)
print("\n==== FULL GAUNTLET ====", flush=True)
print("images:", len(results), " elapsed:", f"{time.time()-t0:.0f}s")
print("per-image:", dict(cc))
print("winning layer:", dict(Counter(x.get("via") for x in results if x.get("via"))))
withvin = ok + miss
print(f"per-BID: read-OK={ok}  MISS(had vin,none read)={miss}  no-db-vin={nodb}  total={len(byb)}")
print(f"per-bid read rate among bids WITH a vin: {ok}/{withvin} = {100*ok/withvin:.0f}%")
print(f"recovered (db had no vin): {len(rec_bids)} bids -> {sorted(rec_bids, reverse=True)}")
print("MISS bids:", sorted(miss_bids, reverse=True))
print("\n-- MISMATCH (different valid VIN — investigate) --")
for x in results:
    if x["vcat"] == "MISMATCH":
        print(f"  bid {x['bid_id']} db={x['db_vin']} got={x['vin']} file={x['file']}")
print("\nreport: /tmp/vin_gauntlet_report.json", flush=True)
