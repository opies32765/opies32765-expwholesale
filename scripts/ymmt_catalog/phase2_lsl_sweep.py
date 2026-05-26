#!/usr/bin/env python3
"""Phase 2b — sweep LSL deals (SQLite) for VIN coverage.
For each ymmt_catalog row not yet in ymmt_vin_cache, look in LSL.deals.
"""
import sqlite3, psycopg2, psycopg2.extras, os, re, sys

LSL_DB = '/opt/livesaleslog/crm.db'
PG_URL = 'postgresql://expuser:ExpWholesale2026!@127.0.0.1:5433/expwholesale'

def main():
    if not os.path.exists(LSL_DB):
        print('LSL DB not found')
        return 1
    lc = sqlite3.connect(f'file:{LSL_DB}?mode=ro', uri=True, timeout=5)
    pc = psycopg2.connect(PG_URL)
    # Pull catalog YMMTs not yet cached
    with pc.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as pcur:
        pcur.execute('''
            SELECT c.year, c.make, c.model, c.trim
              FROM ymmt_catalog c
              LEFT JOIN ymmt_vin_cache v
                ON v.year = c.year
               AND UPPER(v.make) = UPPER(c.make)
               AND UPPER(v.model) = UPPER(c.model)
               AND v.trim = c.trim
             WHERE v.vin IS NULL
        ''')
        uncovered = pcur.fetchall()
    print(f'uncovered YMMTs to sweep against LSL: {len(uncovered)}')
    found = 0
    with pc.cursor() as pcur:
        for i, r in enumerate(uncovered):
            year = r['year']; make = r['make']; model = r['model']; trim = r['trim'] or ''
            lcur = lc.cursor()
            # Prefer trim match
            q = '''
                SELECT vin_no, vehicle_info
                  FROM deals
                 WHERE UPPER(make_name)    LIKE UPPER(?)
                   AND UPPER(vehicle_info) LIKE UPPER(?)
                   AND vehicle_info        LIKE ?
                   AND vin_no IS NOT NULL
                   AND length(vin_no) = 17
                   {trim_clause}
                 ORDER BY sold_at DESC LIMIT 1
            '''
            tc = ''
            params = [f'%{make}%', f'%{model}%', f'%{year}%']
            if trim:
                tc = 'AND UPPER(vehicle_info) LIKE UPPER(?)'
                params.append(f'%{trim}%')
            try:
                lcur.execute(q.format(trim_clause=tc), params)
                row = lcur.fetchone()
                if not row and trim:
                    # Fallthrough to no-trim
                    lcur.execute(q.format(trim_clause=''), params[:3])
                    row = lcur.fetchone()
                if row and row[0] and re.match(r'^[A-HJ-NPR-Z0-9]{17}$', row[0]):
                    pcur.execute('''
                        INSERT INTO ymmt_vin_cache
                          (year, make, model, trim, vin, source, confidence, status)
                        VALUES (%s, %s, %s, %s, %s, 'db_lsl', 'medium', 'found')
                        ON CONFLICT (year, make, model, trim) DO NOTHING
                    ''', (year, make, model, trim, row[0]))
                    found += 1
            except Exception as e:
                print(f'  err {year} {make} {model}: {e}')
            if (i+1) % 500 == 0:
                pc.commit()
                print(f'  swept {i+1}/{len(uncovered)} - found {found} so far')
        pc.commit()
    print(f'LSL sweep DONE. found={found} new VINs')
    lc.close(); pc.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
