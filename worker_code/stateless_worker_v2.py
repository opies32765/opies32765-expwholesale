#!/usr/bin/env python3
"""Chromeless first-stage worker for EW — v2 with full vAuto enrichment.

For each pending bid:
  1. Claim atomically (FOR UPDATE SKIP LOCKED)
  2. GET /api/appraisal/default → fresh appraisalId
  3. POST /api/appraisal/vehicleInfo?strictYMM=true → decoded vehicle
  4. POST /api/priceGuides → 5-6 book values
  5. Carfax share URL via /api/carfax/report → 302 → connect.carfax.com
  6. Store everything in vauto_lookups (appraisal_url, carfax_share_url,
     rbook=null for now, mmr=manheim, kbb, kbb_com=kbbOnline, jd_power=null,
     black_book, etc.)
  7. Release claim

Skips appraisalId-creation if vauto_lookups already has appraisal_url for this bid.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time

import psycopg2
import psycopg2.extras
import requests

DB_URL = os.environ.get('DATABASE_URL',
                        'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
SESSION_LABEL = os.environ.get('VAUTO_SESSION_LABEL', 'oscarpas')
BFF1 = 'https://slot1.bff.megazord.vauto.app.coxautoinc.com'
BFF2 = 'https://slot2.bff.megazord.vauto.app.coxautoinc.com'
DEFAULT_POLL = float(os.environ.get('POLL_INTERVAL', '3'))

log = logging.getLogger('stateless_worker_v2')
_running = True


def _shutdown(signum, frame):
    global _running
    log.info('shutdown signal %s', signum)
    _running = False


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def get_session(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT cookies, entity_id, platform_user_id,
                   EXTRACT(EPOCH FROM (NOW() - refreshed_at)) AS age_s
            FROM vauto_session WHERE label=%s
        """, (SESSION_LABEL,))
        row = cur.fetchone()
        if not row:
            return None
        cookies = row['cookies']
        if isinstance(cookies, str):
            cookies = json.loads(cookies)
        return {'cookies': cookies, 'entity_id': row['entity_id'],
                'platform_user_id': row['platform_user_id'], 'age_s': row['age_s']}


def make_http(sess):
    s = requests.Session()
    s.cookies.update(sess['cookies'])
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
        'Accept': 'application/json,text/html,*/*',
        'appraisalentityid': sess['entity_id'],
        'currententityid': sess['entity_id'],
        'platformuserid': sess['platform_user_id'],
        'Origin': 'https://provision.vauto.app.coxautoinc.com',
        'Referer': 'https://provision.vauto.app.coxautoinc.com/',
    })
    return s


def create_appraisal(http) -> str | None:
    try:
        r = http.get(f'{BFF2}/api/appraisal/default', timeout=15)
        if r.status_code != 200:
            return None
        return (r.json().get('appraisal') or {}).get('appraisalId')
    except Exception as e:
        log.warning('create_appraisal failed: %s', e)
        return None


def decode_vin(http, vin: str) -> dict | None:
    try:
        r = http.post(f'{BFF2}/api/appraisal/vehicleInfo?strictYMM=true',
                      json={'vin': vin}, timeout=15)
        if r.status_code != 200:
            return None
        return r.json().get('vehicleInfo')
    except Exception as e:
        log.warning('decode_vin failed: %s', e)
        return None


PRICE_GUIDE_OPTS = {
    'BlackBook': None, 'KelleyBlueBook': None, 'KbbOnline': None,
    'Manheim': None, 'Nada': None, 'Galves': None, 'Tim': None,
    'Naaa': None, 'Radar': None, 'BlackBookCanada': None, 'None': None,
}
AVAILABLE_GUIDES = ['Radar', 'BlackBook', 'Manheim', 'KelleyBlueBook', 'KbbOnline', 'Nada']


def fetch_price_guides(http, appraisal_id: str, vehicle: dict) -> dict | None:
    try:
        r = http.post(f'{BFF2}/api/priceGuides', json={
            'appraisalId': appraisal_id,
            'vehicle': vehicle,
            'priceGuideOptions': PRICE_GUIDE_OPTS,
            'availablePriceGuides': AVAILABLE_GUIDES,
        }, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning('priceGuides failed: %s', e)
    return None


def extract_book_value(book: dict) -> int | None:
    """Pull a single representative price from a book's response."""
    if not book or not isinstance(book, dict):
        return None
    pricings = book.get('pricings') or []
    for p in pricings:
        for f in p.get('fields') or []:
            if f.get('id') == 'BasePrice' and f.get('value'):
                try:
                    return int(round(float(f['value'])))
                except (ValueError, TypeError):
                    pass
        # Fallback: look for any 'finalPrice' or 'totalPrice'
        for k in ('finalPrice', 'totalPrice', 'basePrice'):
            v = p.get(k)
            if v:
                try:
                    return int(round(float(v)))
                except (ValueError, TypeError):
                    pass
    return None


def fetch_carfax_share_url(http, vin: str) -> str | None:
    try:
        r = http.get(f'{BFF1}/api/carfax/report?vin={vin}', timeout=15)
        if r.status_code != 200:
            return None
        gateway = ((r.json().get('report') or {}).get('url'))
        if not gateway:
            return None
        r2 = http.get(gateway, allow_redirects=False, timeout=15)
        if r2.status_code in (301, 302, 303, 307, 308):
            target = r2.headers.get('Location') or ''
            if 'connect.carfax.com' in target:
                return target
    except Exception as e:
        log.warning('carfax fetch failed for %s: %s', vin, e)
    return None


def claim_one_bid(conn, worker_id: str):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH eligible AS (
                SELECT b.id
                FROM bids b
                LEFT JOIN vauto_lookups vl ON vl.bid_id = b.id
                WHERE b.vin IS NOT NULL AND b.vin <> '' AND LENGTH(b.vin) = 17
                  AND b.created_at > NOW() - INTERVAL '7 days'
                  AND (vl.bid_id IS NULL
                       OR vl.appraisal_url IS NULL OR vl.appraisal_url = ''
                       OR vl.carfax_share_url IS NULL)
                  AND (b.vauto_claimed_by IS NULL
                       OR b.vauto_claimed_at < NOW() - INTERVAL '5 minutes')
                ORDER BY b.created_at DESC
                FOR UPDATE OF b SKIP LOCKED
                LIMIT 1
            )
            UPDATE bids b
               SET vauto_claimed_by = %s, vauto_claimed_at = NOW()
              FROM eligible e
             WHERE b.id = e.id
            RETURNING b.id AS bid_id, b.vin
        """, (worker_id,))
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None


SAVED_APPRAISAL_URL_TEMPLATE = ('https://provision.vauto.app.coxautoinc.com/Va/Appraisal/'
                                'Default.aspx?Id={appraisal_id}&AppraisalStatus=InProcess')


def upsert_lookup(conn, bid_id, vin, appraisal_id, vehicle, books, carfax_url):
    appraisal_url = SAVED_APPRAISAL_URL_TEMPLATE.format(appraisal_id=appraisal_id) if appraisal_id else None

    rbook = extract_book_value(books.get('rBook') if books else None)  # likely null
    black_book = extract_book_value(books.get('blackBook') if books else None)
    kbb = extract_book_value(books.get('kbb') if books else None)
    kbb_com = extract_book_value(books.get('kbbOnline') if books else None)
    nada = extract_book_value(books.get('nada') if books else None)
    manheim = extract_book_value(books.get('manheim') if books else None)
    jd_power = extract_book_value(books.get('jdPower') if books else None)  # likely null

    api_price_guides = json.dumps(books) if books else None
    raw_json_blob = json.dumps({'vehicle': vehicle, 'appraisalId': appraisal_id}) if vehicle else None

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO vauto_lookups (
                bid_id, vin, appraisal_url, carfax_share_url,
                rbook, black_book, kbb, kbb_com, jd_power, mmr,
                api_price_guides, raw_json, looked_up_at, api_refreshed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, NOW(), NOW())
            ON CONFLICT (bid_id) DO UPDATE SET
                vin = COALESCE(EXCLUDED.vin, vauto_lookups.vin),
                appraisal_url = COALESCE(NULLIF(EXCLUDED.appraisal_url, ''), vauto_lookups.appraisal_url),
                carfax_share_url = COALESCE(NULLIF(EXCLUDED.carfax_share_url, ''),
                                            NULLIF(vauto_lookups.carfax_share_url, ''),
                                            EXCLUDED.carfax_share_url),
                rbook = COALESCE(EXCLUDED.rbook, vauto_lookups.rbook),
                black_book = COALESCE(EXCLUDED.black_book, vauto_lookups.black_book),
                kbb = COALESCE(EXCLUDED.kbb, vauto_lookups.kbb),
                kbb_com = COALESCE(EXCLUDED.kbb_com, vauto_lookups.kbb_com),
                jd_power = COALESCE(EXCLUDED.jd_power, vauto_lookups.jd_power),
                mmr = COALESCE(EXCLUDED.mmr, vauto_lookups.mmr),
                api_price_guides = COALESCE(EXCLUDED.api_price_guides, vauto_lookups.api_price_guides),
                raw_json = COALESCE(EXCLUDED.raw_json, vauto_lookups.raw_json),
                api_refreshed_at = NOW(),
                looked_up_at = NOW()
        """, (bid_id, vin, appraisal_url, carfax_url or '',
              rbook, black_book, kbb, kbb_com, jd_power, manheim,
              api_price_guides, raw_json_blob))
        cur.execute("UPDATE bids SET vauto_claimed_by=NULL, vauto_claimed_at=NULL WHERE id=%s",
                    (bid_id,))
        conn.commit()


def heartbeat(conn, worker_id):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO workers (worker_id, role, priority, effective_priority,
                                 last_heartbeat, updated_at)
            VALUES (%s, 'api_worker', 'primary', 'primary', NOW(), NOW())
            ON CONFLICT (worker_id) DO UPDATE SET
                last_heartbeat = NOW(), updated_at = NOW()
        """, (worker_id,))
        conn.commit()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--worker-id', required=True)
    p.add_argument('--poll-interval', type=float, default=DEFAULT_POLL)
    args = p.parse_args()
    logging.basicConfig(format=f'%(asctime)s {args.worker_id} %(levelname)s %(message)s',
                        level=logging.INFO, stream=sys.stdout)
    log.info('starting v2 (poll every %ss)', args.poll_interval)

    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False

    last_hb = 0.0
    last_session = 0.0
    sess = None
    http = None

    while _running:
        try:
            now = time.time()
            if not sess or now - last_session > 60:
                sess = get_session(conn)
                if sess:
                    http = make_http(sess)
                last_session = now

            if now - last_hb > 30:
                heartbeat(conn, args.worker_id)
                last_hb = now

            if not sess or not http:
                log.warning('no session — sleeping')
                time.sleep(args.poll_interval * 5)
                continue

            bid = claim_one_bid(conn, args.worker_id)
            if not bid:
                time.sleep(args.poll_interval)
                continue

            t0 = time.time()
            log.info('claimed bid %s vin=%s', bid['bid_id'], bid['vin'])

            # 1. Create appraisal (gets appraisalId)
            appraisal_id = create_appraisal(http)
            # 2. Decode VIN
            vehicle = decode_vin(http, bid['vin']) if appraisal_id else None
            # 3. Get book values
            books = fetch_price_guides(http, appraisal_id, vehicle) if (appraisal_id and vehicle) else None
            # 4. Carfax share URL
            carfax_url = fetch_carfax_share_url(http, bid['vin'])

            upsert_lookup(conn, bid['bid_id'], bid['vin'], appraisal_id, vehicle, books, carfax_url)
            elapsed = time.time() - t0
            book_count = sum(1 for k, v in (books or {}).items() if v) if books else 0
            log.info('bid %s ENRICHED in %.2fs apid=%s books=%d carfax=%s',
                     bid['bid_id'], elapsed,
                     (appraisal_id[:20] + '...') if appraisal_id else 'none',
                     book_count,
                     'yes' if carfax_url else 'no')
        except psycopg2.OperationalError as e:
            log.error('DB error: %s', e)
            try: conn.close()
            except Exception: pass
            time.sleep(5)
            conn = psycopg2.connect(DB_URL); conn.autocommit = False
        except Exception as e:
            log.exception('worker loop error: %s', e)
            time.sleep(args.poll_interval * 2)

    log.info('exiting')
    conn.close()


if __name__ == '__main__':
    main()
