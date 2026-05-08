"""SMS safety net — runs every 60s, finds SMS bid_photos with no local_path
and re-runs the download + OCR pipeline that the background thread should
have done but may have been killed mid-flight.

Install via cron:
  * * * * * /opt/expwholesale/venv/bin/python /opt/expwholesale/sms_safety_net.py >> /var/log/sms_safety_net.log 2>&1

Idempotent. Safe to run while bg thread also runs (it skips rows that
already have local_path).
"""
import os, sys, requests, uuid, time, traceback
sys.path.insert(0, '/opt/expwholesale')
os.environ.setdefault('DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')

# Twilio creds — could be read from systemd Environment if we wanted
TWILIO_SID = os.environ.get('TWILIO_ACCOUNT_SID',
    'REDACTED_USE_ENV')
TWILIO_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN',
    'REDACTED_USE_ENV')

UPLOAD_BASE = '/opt/expwholesale/static/uploads/sms'

import app  # imports extract_vin_from_file, extract_mileage_from_file, get_db


def _ts():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def find_stuck():
    """Return list of (bp.id, bp.bid_id, bp.url) for SMS photos that are
    older than 60s and still have no local_path."""
    with app.get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT bp.id, bp.bid_id, bp.url, b.vin, b.mileage
              FROM bid_photos bp
              JOIN bids b ON b.id = bp.bid_id
             WHERE bp.is_sms_intake = TRUE
               AND bp.local_path IS NULL
               AND bp.created_at > NOW() - INTERVAL '24 hours'
               AND bp.created_at < NOW() - INTERVAL '60 seconds'
             ORDER BY bp.created_at DESC
             LIMIT 20
        """)
        return cur.fetchall()


def process(row):
    bp_id, bid_id, url, existing_vin, existing_miles = (
        row['id'], row['bid_id'], row['url'], row.get('vin'), row.get('mileage'))
    print(f'[{_ts()}] processing bid_photos #{bp_id} bid #{bid_id}', flush=True)

    try:
        r = requests.get(url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=30,
                         allow_redirects=True)
        if r.status_code != 200 or not r.content:
            print(f'[{_ts()}]   twilio fetch HTTP {r.status_code}, {len(r.content)}b — skipping', flush=True)
            return False
        mime = r.headers.get('Content-Type', 'image/jpeg').split(';')[0]
        ext = '.jpg' if 'jpeg' in mime else ('.png' if 'png' in mime else '.bin')
        os.makedirs(f'{UPLOAD_BASE}/{bid_id}', exist_ok=True)
        fname = f'sms-{bid_id}-{uuid.uuid4().hex[:8]}{ext}'
        fpath = f'{UPLOAD_BASE}/{bid_id}/{fname}'
        with open(fpath, 'wb') as f:
            f.write(r.content)

        local_url = f'/static/uploads/sms/{bid_id}/{fname}'

        vin = None
        if not existing_vin:
            try:
                vin = app.extract_vin_from_file(r.content, mime)
                if vin:
                    print(f'[{_ts()}]   VIN extracted: {vin}', flush=True)
            except Exception as e:
                print(f'[{_ts()}]   VIN extract failed: {e}', flush=True)

        miles = None
        if not existing_miles:
            try:
                miles_raw = app.extract_mileage_from_file(r.content, mime)
                if miles_raw and str(miles_raw).isdigit() and 100 <= int(miles_raw) <= 999999:
                    miles = int(miles_raw)
                    print(f'[{_ts()}]   miles extracted: {miles}', flush=True)
            except Exception as e:
                print(f'[{_ts()}]   miles extract failed: {e}', flush=True)

        with app.get_db() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE bid_photos
                   SET local_path = %s,
                       vin_extracted = COALESCE(vin_extracted, %s)
                 WHERE id = %s AND local_path IS NULL
            """, (local_url, vin, bp_id))
            if vin and not existing_vin:
                cur.execute("""
                    UPDATE bids SET vin = %s
                     WHERE id = %s AND (vin IS NULL OR vin = '')
                """, (vin, bid_id))
            if miles and not existing_miles:
                cur.execute("""
                    UPDATE bids SET mileage = %s
                     WHERE id = %s AND mileage IS NULL
                """, (miles, bid_id))
            conn.commit()
        print(f'[{_ts()}]   bid #{bid_id} updated: vin={vin or "(kept)"} miles={miles or "(kept)"}', flush=True)
        return True
    except Exception as e:
        print(f'[{_ts()}]   error: {e}', flush=True)
        traceback.print_exc()
        return False


def main():
    rows = find_stuck()
    if not rows:
        return
    print(f'[{_ts()}] safety-net found {len(rows)} stuck SMS photo(s)', flush=True)
    for row in rows:
        process(row)


if __name__ == '__main__':
    main()
