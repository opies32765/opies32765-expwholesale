"""EW full-corpus 9B sweep (2026-06-11). Run EVERY texted-in intake image on disk
through the 9B via the reliable combined extractor (extract_carfax_info = 1 call),
parallelized, scored by ISO-3779 check digit + DB ground truth. Finds every image
the 9B can't read so we fix the system once, not per-bid.

Run:  cd /opt/expwholesale && set -a; . /etc/default/expwholesale-mcp; set +a; \
      GOOGLE_APPLICATION_CREDENTIALS=/opt/expwholesale/google_vision_key.json \
      CONC=6 venv/bin/python /opt/expwholesale/vin_harness_all.py
Report: /tmp/vin_harness_all_report.json
"""
import os, sys, json, time, re, glob, mimetypes, threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2, psycopg2.extras

import app  # installs local_brain_shim; gives the real extractors
from app import extract_carfax_info, vin_check_digit_valid

CONC = int(os.environ.get("CONC", "6"))
ROOT = "/opt/expwholesale/static/uploads/sms"

# All intake images on disk, newest bid dirs first.
files = []
for ext in ("jpg", "jpeg", "png", "webp", "heic"):
    files += glob.glob(f"{ROOT}/*/*.{ext}")
def _bidof(p):
    m = re.search(r"/sms/(\d+)/", p)
    return int(m.group(1)) if m else None
files = [f for f in files if _bidof(f) is not None]
files.sort(key=lambda p: (_bidof(p), p), reverse=True)
print(f"sweep: {len(files)} images on disk, conc={CONC}", flush=True)

# Batch ground truth for every bid we touch.
bids = sorted({_bidof(f) for f in files})
c = psycopg2.connect(os.environ["DATABASE_URL"])
cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT id, vin, mileage FROM bids WHERE id = ANY(%s)", (bids,))
GT = {r["id"]: r for r in cur.fetchall()}
c.close()

def _norm(v): return (str(v).strip().upper() if v else "") or None
def _valid(v): return bool(v and len(v) == 17 and vin_check_digit_valid(v))

done = 0; lock = threading.Lock(); t0 = time.time()
def work(path):
    global done
    bid = _bidof(path)
    gt = GT.get(bid, {})
    dbv = _norm(gt.get("vin")); dbv_ok = _valid(dbv)
    rec = {"bid_id": bid, "file": path.split("/sms/")[-1], "db_vin": dbv, "db_miles": gt.get("mileage")}
    try:
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        img = open(path, "rb").read()
        info = extract_carfax_info(img, mime) or {}
        vin = _norm(info.get("vin"))
        m = re.search(r"[A-HJ-NPR-Z0-9]{17}", vin) if vin else None
        vin = m.group(0) if m else vin
        rec["vin"] = vin
        rec["vin_valid"] = _valid(vin)
        rec["miles"] = info.get("mileage")
        rec["make"] = info.get("make"); rec["model"] = info.get("model")
    except Exception as e:
        rec["err"] = str(e)[:160]; rec["vin_valid"] = False
    v_ok = rec.get("vin_valid"); vin = rec.get("vin")
    if v_ok and dbv_ok:
        rec["vcat"] = "MATCH" if vin == dbv else "MISMATCH"
    elif v_ok and not dbv_ok:
        rec["vcat"] = "RECOVERED"
    elif (not v_ok) and dbv_ok:
        rec["vcat"] = "MISS"
    else:
        rec["vcat"] = "NONE"
    with lock:
        done += 1
        if done % 25 == 0:
            print(f"...{done}/{len(files)}  {time.time()-t0:.0f}s", flush=True)
    return rec

results = []
with ThreadPoolExecutor(max_workers=CONC) as ex:
    for fut in as_completed([ex.submit(work, f) for f in files]):
        results.append(fut.result())

json.dump(results, open("/tmp/vin_harness_all_report.json", "w"), default=str, indent=1)
cc = Counter(x["vcat"] for x in results)
print("\n==== FULL-CORPUS 9B SWEEP ====", flush=True)
print("images:", len(results), " elapsed:", f"{time.time()-t0:.0f}s")
print("VIN categories:", dict(cc))
nvalid = sum(1 for x in results if x.get("vin_valid"))
print(f"check-digit-valid VIN read on {nvalid}/{len(results)} images")
print("\n-- MISS (bid HAD a valid VIN, 9B combined extractor failed) --")
for x in results:
    if x["vcat"] == "MISS":
        print(f"  bid {x['bid_id']} db={x['db_vin']} got={x.get('vin')} file={x['file']}")
print("\n-- MISMATCH (9B got a DIFFERENT valid VIN — investigate) --")
for x in results:
    if x["vcat"] == "MISMATCH":
        print(f"  bid {x['bid_id']} db={x['db_vin']} got={x['vin']} file={x['file']}")
print("\nreport: /tmp/vin_harness_all_report.json", flush=True)
