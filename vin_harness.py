"""EW VIN/miles regression harness (2026-06-11). Pull the last N texted-in intake
images and run the REAL production extractors (now 9B-backed via the shim) on each,
scored by ISO-3779 check digit + DB ground truth. Repeatable — re-run after any
extractor change to catch regressions instead of playing whack-a-mole.

Run:  cd /opt/expwholesale && set -a; . /etc/default/expwholesale-mcp; set +a; \
      GOOGLE_APPLICATION_CREDENTIALS=/opt/expwholesale/google_vision_key.json \
      HARNESS_N=100 venv/bin/python /opt/expwholesale/vin_harness.py
Report: /tmp/vin_harness_report.json
"""
import os, sys, json, io, time, re, mimetypes, contextlib
from collections import Counter
import psycopg2, psycopg2.extras

import app  # installs local_brain_shim; gives the real extractors
from app import extract_vin_from_file, vin_check_digit_valid
try:
    from app import extract_mileage_from_file
except Exception:
    extract_mileage_from_file = None

N = int(os.environ.get("HARNESS_N", "100"))
c = psycopg2.connect(os.environ["DATABASE_URL"])
cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
    SELECT bp.id AS photo_id, bp.bid_id, bp.local_path,
           b.vin AS db_vin, b.mileage AS db_miles
      FROM bid_photos bp JOIN bids b ON b.id = bp.bid_id
     WHERE bp.local_path IS NOT NULL
     ORDER BY bp.id DESC LIMIT %s
""", (N,))
rows = cur.fetchall()
print(f"harness: {len(rows)} photos pulled", flush=True)

def _norm(v):
    return (str(v).strip().upper() if v else "") or None

results = []
t0 = time.time()
for i, r in enumerate(rows):
    # local_path is a URL path (/static/uploads/sms/<bid>/<photo>.ext) rooted at the app dir
    lp = r["local_path"] or ""
    path = os.path.join("/opt/expwholesale", lp.lstrip("/")) if lp else None
    rec = {"photo_id": r["photo_id"], "bid_id": r["bid_id"],
           "db_vin": _norm(r["db_vin"]), "db_miles": r["db_miles"]}
    if not path or not os.path.exists(path):
        rec["vcat"] = "FILE_MISSING"; results.append(rec); continue
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    img = open(path, "rb").read()
    buf = io.StringIO()
    vin = miles = None
    try:
        with contextlib.redirect_stdout(buf):
            vin = extract_vin_from_file(img, mime)
            if extract_mileage_from_file:
                miles = extract_mileage_from_file(img, mime)
    except Exception as e:
        rec["vcat"] = "ERR"; rec["err"] = str(e)[:160]; results.append(rec); continue
    log = buf.getvalue()
    m = re.search(r"\[OCR\] VIN via ([^\(:]+)", log)
    rec["via"] = m.group(1).strip() if m else None
    vin = _norm(vin)
    rec["vin"] = vin
    rec["miles"] = miles
    vin_valid = bool(vin and len(vin) == 17 and vin_check_digit_valid(vin))
    rec["vin_valid"] = vin_valid
    dbv = rec["db_vin"]
    dbv_valid = bool(dbv and len(dbv) == 17 and vin_check_digit_valid(dbv))
    if vin_valid and dbv_valid:
        rec["vcat"] = "MATCH" if vin == dbv else "MISMATCH"
    elif vin_valid and not dbv_valid:
        rec["vcat"] = "RECOVERED"
    elif (not vin_valid) and dbv_valid:
        rec["vcat"] = "MISS"
    else:
        rec["vcat"] = "NONE"
    results.append(rec)
    if i % 10 == 0:
        print(f"...{i+1}/{len(rows)}  {time.time()-t0:.0f}s", flush=True)

json.dump(results, open("/tmp/vin_harness_report.json", "w"), default=str, indent=1)
cc = Counter(x["vcat"] for x in results)
print("\n==== VIN HARNESS SUMMARY ====", flush=True)
print("images:", len(results), " elapsed:", f"{time.time()-t0:.0f}s")
print("VIN categories:", dict(cc))
print("winning layer:", dict(Counter(x.get("via") for x in results if x.get("via"))))
print("\n-- MISS (bid HAD a valid VIN, 9B failed) --")
for x in results:
    if x["vcat"] == "MISS":
        print(f"  bid {x['bid_id']} db={x['db_vin']} got={x.get('vin')}")
print("\n-- MISMATCH (9B got a DIFFERENT valid VIN — investigate) --")
for x in results:
    if x["vcat"] == "MISMATCH":
        print(f"  bid {x['bid_id']} db={x['db_vin']} got={x['vin']}")
print("\n-- RECOVERED (valid VIN the bid never had) --")
for x in results:
    if x["vcat"] == "RECOVERED":
        print(f"  bid {x['bid_id']} got={x['vin']} via={x.get('via')}")
print("\nreport: /tmp/vin_harness_report.json", flush=True)
