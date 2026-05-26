#!/usr/bin/env python3
"""Phase 3 slicer — pull uncovered YMMTs and split into N batches.
Each batch is written to /tmp/phase3_batch_<N>.csv for an agent worker.
"""
import psycopg2, psycopg2.extras, sys, csv

PG_URL = 'postgresql://expuser:ExpWholesale2026!@127.0.0.1:5433/expwholesale'
N_WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 10

def main():
    c = psycopg2.connect(PG_URL)
    with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute('''
            SELECT c.id, c.year, c.make, c.model, c.trim
              FROM ymmt_catalog c
              LEFT JOIN ymmt_vin_cache v
                ON v.year = c.year
               AND UPPER(v.make) = UPPER(c.make)
               AND UPPER(v.model) = UPPER(c.model)
               AND v.trim = c.trim
             WHERE v.vin IS NULL
             ORDER BY
               CASE WHEN UPPER(c.make) IN ('TOYOTA','HONDA','FORD','CHEVROLET','GMC','RAM','JEEP','NISSAN','HYUNDAI','KIA') THEN 0
                    WHEN UPPER(c.make) IN ('BMW','MERCEDES-BENZ','AUDI','LEXUS','ACURA','INFINITI') THEN 1
                    ELSE 2 END,
               c.year DESC, c.id
        ''')
        rows = cur.fetchall()
    c.close()
    n = len(rows)
    print(f'uncovered YMMTs: {n}')
    if n == 0:
        print('nothing to do')
        return 0
    per = (n + N_WORKERS - 1) // N_WORKERS
    for i in range(N_WORKERS):
        chunk = rows[i*per:(i+1)*per]
        if not chunk: continue
        path = f'/tmp/phase3_batch_{i:02d}.csv'
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['id','year','make','model','trim'])
            for r in chunk:
                w.writerow([r['id'], r['year'], r['make'], r['model'], r['trim'] or ''])
        print(f'  batch {i:02d}: {len(chunk)} YMMTs -> {path}')
    return 0

if __name__ == '__main__':
    sys.exit(main())
