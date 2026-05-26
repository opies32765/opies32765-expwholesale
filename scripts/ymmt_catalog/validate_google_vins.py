#!/usr/bin/env python3
"""Validate all google_search VINs against NHTSA DecodeVINValuesBatch API.
Mark mismatches as not_found with source='nhtsa_failed_decode'.
"""
import psycopg2, psycopg2.extras, urllib.request, urllib.parse, json, time, sys

PG = 'postgresql://expuser:ExpWholesale2026!@127.0.0.1:5433/expwholesale'
BATCH_SIZE = 50
SLEEP_BETWEEN = 0.3

c = psycopg2.connect(PG)
c.autocommit = False
cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
    SELECT year, make, model, trim, vin
      FROM ymmt_vin_cache
     WHERE source = 'google_search'
       AND vin IS NOT NULL
       AND length(vin) = 17
       AND status = 'found'
     ORDER BY year, make, model, trim
""")
rows = cur.fetchall()
total = len(rows)
print(f'Validating {total} google_search VINs against NHTSA...', flush=True)

ok = 0
bad = 0
api_errors = 0

for i in range(0, total, BATCH_SIZE):
    chunk = rows[i:i+BATCH_SIZE]
    # NHTSA batch: 'VIN1; VIN2; VIN3' format
    payload = '; '.join(r['vin'] for r in chunk)
    try:
        data = urllib.parse.urlencode({'format': 'json', 'data': payload}).encode()
        req = urllib.request.Request(
            'https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/',
            data=data,
            method='POST')
        with urllib.request.urlopen(req, timeout=30) as resp:
            j = json.loads(resp.read())
    except Exception as e:
        api_errors += 1
        print(f'  API error batch {i//BATCH_SIZE}: {e}', flush=True)
        time.sleep(2)
        continue

    results = j.get('Results') or []
    # Results aligned with chunk by position
    for row, res in zip(chunk, results):
        decoded_year = str(res.get('ModelYear') or '').strip()
        decoded_make = (res.get('Make') or '').strip().upper()
        decoded_model = (res.get('Model') or '').strip().upper()
        cat_year = str(row['year']).strip()
        cat_make = row['make'].strip().upper()
        cat_model = row['model'].strip().upper()

        year_ok = decoded_year == cat_year or abs(int(decoded_year or 0) - int(cat_year)) <= 1
        make_ok = cat_make in decoded_make or decoded_make in cat_make if decoded_make else False
        model_ok = any(w in decoded_model for w in cat_model.split()) if decoded_model else False

        if year_ok and make_ok and model_ok and decoded_year:
            cur.execute("""
                UPDATE ymmt_vin_cache
                   SET decoded_match = true
                 WHERE year = %s AND UPPER(make) = UPPER(%s)
                   AND UPPER(model) = UPPER(%s) AND trim = %s
            """, (row['year'], row['make'], row['model'], row['trim']))
            ok += 1
        else:
            # Mark as not_found with explicit reason
            cur.execute("""
                UPDATE ymmt_vin_cache
                   SET status = 'not_found',
                       vin = NULL,
                       source = 'nhtsa_validation_failed',
                       source_url = NULL,
                       decoded_match = false
                 WHERE year = %s AND UPPER(make) = UPPER(%s)
                   AND UPPER(model) = UPPER(%s) AND trim = %s
                   AND source = 'google_search'
            """, (row['year'], row['make'], row['model'], row['trim']))
            bad += 1

    c.commit()
    if (i // BATCH_SIZE) % 20 == 0:
        print(f'  progress {i+len(chunk)}/{total}: ok={ok} bad={bad} api_err={api_errors}', flush=True)
    time.sleep(SLEEP_BETWEEN)

print(f'DONE: ok={ok} bad={bad} api_err={api_errors}', flush=True)
