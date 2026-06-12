"""Build a small CONFIRMED odometer->miles eval set (2026-06-12). Run EW's miles OCR
on a sample of intake images; keep reads that MATCH the bid's recorded mileage (so the
image truly shows that odometer). Used to check the multi-task LoRA didn't dent miles.
Output /tmp/miles_eval_labels.json  [{image, miles, bid}]
"""
import os, json, glob, re, mimetypes, threading, random
from concurrent.futures import ThreadPoolExecutor, as_completed
import psycopg2, psycopg2.extras
import app
from app import extract_mileage_from_file

ROOT = "/opt/expwholesale/static/uploads/sms"
SAMPLE = int(os.environ.get("SAMPLE", "320"))
CONC = int(os.environ.get("CONC", "6"))
dsn = os.environ["DATABASE_URL"]
c = psycopg2.connect(dsn); cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT id, mileage FROM bids WHERE mileage IS NOT NULL AND mileage > 100")
MI = {r["id"]: int(r["mileage"]) for r in cur.fetchall()}; c.close()

files = []
for ext in ("jpg", "jpeg", "png", "webp"):
    files += glob.glob(f"{ROOT}/*/*.{ext}")
def bidof(p):
    m = re.search(r"/sms/(\d+)/", p); return int(m.group(1)) if m else None
files = [f for f in files if bidof(f) in MI]
random.seed(7); random.shuffle(files); files = files[:SAMPLE]
print(f"sampling {len(files)} images that belong to bids with a recorded mileage", flush=True)

out = []; lock = threading.Lock(); done = 0
def work(p):
    global done
    bid = bidof(p); truth = MI.get(bid)
    res = None
    try:
        mime = mimetypes.guess_type(p)[0] or "image/jpeg"
        m = extract_mileage_from_file(open(p, "rb").read(), mime)
        if m and str(m).isdigit():
            m = int(m)
            if truth and abs(m - truth) <= max(200, truth * 0.02):   # confirmed odometer
                res = {"image": p, "miles": truth, "bid": bid}
    except Exception:
        pass
    with lock:
        done += 1
        if done % 40 == 0:
            print(f"...{done}/{len(files)} kept={len(out)}", flush=True)
    return res

with ThreadPoolExecutor(max_workers=CONC) as ex:
    for fut in as_completed([ex.submit(work, f) for f in files]):
        r = fut.result()
        if r: out.append(r)

json.dump(out, open("/tmp/miles_eval_labels.json", "w"))
print(f"DONE: {len(out)} confirmed odometer->miles labels -> /tmp/miles_eval_labels.json", flush=True)
