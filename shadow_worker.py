#!/usr/bin/env python3
"""shadow_worker.py - vAuto/Carfax/AutoCheck API SHADOW parity harness.

READ-ONLY on all live data. Writes ONLY to shadow.vauto_api_shadow.
Runs the chromeless Cox BFF API path alongside the live browser pipeline so we
can compare API-sourced vs browser-sourced enrichment per bid.

SAFETY (enforced by code AND by the ew_shadow_ro DB role which has no UPDATE/
DELETE on any live table):
  - NEVER claims bids (no UPDATE bids.vauto_claimed_*); selection is a lock-free
    anti-join against this table's own rows.
  - NEVER writes vauto_lookups / accutrade_lookups / ipacket_lookups / workers.
  - NEVER imports app or vauto_enrichment (assess-gate unreachable).
  - Reads the vAuto session read-only; never refreshes it (back off on 401).
"""
import os, sys, json, time, signal, logging
import requests
import psycopg2, psycopg2.extras
from psycopg2.extras import Json

sys.path.insert(0, '/opt/expwholesale')
from vauto_bff_direct import fetch_competitive_set  # pure HTTP->json, no app/db

BFF1 = 'https://slot1.bff.megazord.vauto.app.coxautoinc.com'   # carfax
BFF2 = 'https://slot2.bff.megazord.vauto.app.coxautoinc.com'   # appraisal/vehicleInfo/priceGuides/autocheck

SESSION_LABEL = os.environ.get('VAUTO_SESSION_LABEL', 'worker_pool')
POLL_INTERVAL = float(os.environ.get('POLL_INTERVAL', '3'))
DB_URL = os.environ['DATABASE_URL']

logging.basicConfig(level=logging.INFO, format='%(asctime)s shadow %(levelname)s %(message)s')
log = logging.getLogger('shadow')

_run = True
def _stop(*a):
    global _run
    _run = False
signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def db():
    c = psycopg2.connect(DB_URL)
    c.autocommit = True
    return c

def load_session(conn, label):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT cookies, entity_id, platform_user_id,
                              EXTRACT(EPOCH FROM (now()-refreshed_at)) AS age_s
                         FROM vauto_session WHERE label=%s""", (label,))
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
        'appraisalentityid': sess['entity_id'] or '',
        'currententityid': sess['entity_id'] or '',
        'platformuserid': sess['platform_user_id'] or '',
        'Origin': 'https://provision.vauto.app.coxautoinc.com',
        'Referer': 'https://provision.vauto.app.coxautoinc.com/',
    })
    # Clean {name: value} cookie dict for callees that take a cookies= dict
    # (the live response jar accumulates duplicate AWSALB cookies across calls,
    # which breaks dict(http.cookies); the original DB session dict is unique).
    s.clean_cookies = dict(sess['cookies'])
    return s

class AuthErr(Exception):
    pass

def _get(http, url, **kw):
    r = http.get(url, timeout=kw.pop('timeout', 20), **kw)
    if r.status_code in (401, 403):
        raise AuthErr('%s %s' % (r.status_code, url[:60]))
    return r

def _post(http, url, payload):
    r = http.post(url, json=payload, timeout=25)
    if r.status_code in (401, 403):
        raise AuthErr('%s %s' % (r.status_code, url[:60]))
    return r

def create_appraisal(http):
    r = _get(http, BFF2 + '/api/appraisal/default', timeout=15)
    if r.status_code != 200:
        return None
    return (r.json().get('appraisal') or {}).get('appraisalId')

def decode_vin(http, vin, miles=None):
    body = {'vin': vin}
    if miles:
        body.update({'odometer': int(miles), 'odometerUom': 'Miles'})
    r = _post(http, BFF2 + '/api/appraisal/vehicleInfo?strictYMM=true', body)
    if r.status_code != 200:
        return None, None
    j = r.json()
    return j.get('vehicleInfo'), j.get('optionCodes')

_PG_OPTS = {'BlackBook': None, 'KelleyBlueBook': None, 'KbbOnline': None, 'Manheim': None,
            'Nada': None, 'Galves': None, 'Tim': None, 'Naaa': None, 'Radar': None,
            'BlackBookCanada': None, 'None': None}
_GUIDES = ['Radar', 'BlackBook', 'Manheim', 'KelleyBlueBook', 'KbbOnline', 'Nada']

def fetch_price_guides(http, apid, vehicle):
    r = _post(http, BFF2 + '/api/priceGuides', {'appraisalId': apid, 'vehicle': vehicle,
              'priceGuideOptions': _PG_OPTS, 'availablePriceGuides': _GUIDES})
    return r.json() if r.status_code == 200 else None

_PREF = {'blackBook': [('Wholesale', 'Clean'), ('Wholesale', 'Average'), ('Wholesale', 'ExtraClean'), ('Wholesale', 'Rough')],
         'kbb': [('Wholesale', 'Good'), ('Wholesale', 'Excellent'), ('Wholesale', 'Outstanding'), ('Wholesale', 'Fair')],
         'kbbOnline': [('TradeIn', 'Good'), ('TradeIn', 'Excellent'), ('TradeIn', 'VeryGood'), ('TradeIn', 'Fair')],
         'nada': [('TradeIn', 'Average'), ('TradeIn', 'Clean'), ('TradeIn', 'Rough')],
         'manheim': [('Wholesale', 'Average'), ('Wholesale', 'Above'), ('Wholesale', 'Below')]}

def book_val(book, name=None):
    if not isinstance(book, dict):
        return None
    ps = book.get('pricings') or []
    if not ps:
        return None
    def tot(p):
        try:
            return int(round(float(p.get('basePrice') or 0) + float(p.get('odometerAdjustment') or 0) + float(p.get('historicalAdjustment') or 0)))
        except Exception:
            return None
    for disp, cond in (_PREF.get(name) or []):
        for p in ps:
            if p.get('disposition') == disp and p.get('condition') == cond:
                v = tot(p)
                if v and v > 0:
                    return v
    for p in ps:
        v = tot(p)
        if v and v > 0:
            return v
    return None

def fetch_carfax(http, vin):
    r = _get(http, BFF1 + '/api/carfax/report?vin=' + vin, timeout=15)
    if r.status_code != 200:
        return None, None
    rep = (r.json().get('report') or {})
    share = None
    gw = rep.get('url')
    if gw:
        try:
            r2 = http.get(gw, allow_redirects=False, timeout=15)
            if r2.status_code in (301, 302, 303, 307, 308):
                tgt = r2.headers.get('Location') or ''
                if 'connect.carfax.com' in tgt:
                    share = tgt
        except Exception:
            pass
    return rep, share

def fetch_autocheck(http, vin):
    try:
        r = _get(http, BFF2 + '/api/autocheck/getReport?vin=' + vin, timeout=25)
        return (r.status_code == 200, len(r.text or ''))
    except AuthErr:
        raise
    except Exception:
        return (False, 0)

def select_bid(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT b.id AS bid_id, b.vin, b.trim AS live_trim,
                   b.canon_trim AS live_canon_trim, b.mileage AS miles
            FROM bids b
            LEFT JOIN shadow.vauto_api_shadow s ON s.bid_id=b.id
            WHERE b.vin IS NOT NULL AND length(b.vin)=17
              AND b.created_at > now() - interval '7 days'
              AND s.bid_id IS NULL
            ORDER BY b.created_at DESC
            LIMIT 1
        """)
        return cur.fetchone()

def process(conn, http, bid):
    t0 = time.time()
    row = {'bid_id': bid['bid_id'], 'vin': bid['vin'], 'session_label': SESSION_LABEL,
           'live_trim': bid.get('live_trim'), 'live_canon_trim': bid.get('live_canon_trim')}
    err = []
    try:
        apid = create_appraisal(http)
        veh, opts = decode_vin(http, bid['vin'], bid.get('miles'))
        if veh:
            row['api_series_trim'] = veh.get('series')
            row['api_series_detail'] = veh.get('seriesDetail')
            row['api_vehicle_info'] = Json(veh)
            row['api_option_codes'] = Json(opts) if opts else None
        pg = fetch_price_guides(http, apid or 'unused', veh) if veh else None
        if pg:
            row['api_price_guides'] = Json(pg)
            row['api_black_book'] = book_val(pg.get('blackBook'), 'blackBook')
            row['api_kbb'] = book_val(pg.get('kbb'), 'kbb')
            row['api_kbb_com'] = book_val(pg.get('kbbOnline'), 'kbbOnline')
            row['api_nada'] = book_val(pg.get('nada'), 'nada')
            _mh = pg.get('manheim') or {}
            row['api_mmr'] = book_val(_mh, 'manheim') or (int(_mh.get('averageAuctionPrice') or 0) or None)
        rep, share = fetch_carfax(http, bid['vin'])
        if rep is not None:
            row['api_carfax_ok'] = bool(rep.get('hasReport'))
            row['api_carfax_has_one_owner'] = rep.get('hasOneOwner')
            row['api_carfax_num_owners'] = rep.get('numOwners')
            row['api_carfax_accident_count'] = rep.get('accidentCount')
            row['api_carfax_has_clean_title'] = rep.get('hasCleanTitle')
            row['api_carfax_has_frame_damage'] = rep.get('hasFrameDamage')
            row['api_carfax_has_total_loss'] = rep.get('hasTotalLoss')
            row['api_carfax_has_damage'] = rep.get('hasDamage')
            row['api_carfax_service_records'] = rep.get('serviceRecordCount')
            row['api_carfax_share_url'] = share
            row['api_carfax_report'] = Json(rep)
        ok, ln = fetch_autocheck(http, bid['vin'])
        row['api_autocheck_ok'] = ok
        row['api_autocheck_len'] = ln
        if veh:
            try:
                cs = fetch_competitive_set(veh, http.clean_cookies, dict(http.headers), appraisal_id=(apid or 'unused'))
                comps = (cs or {}).get('competitiveSetVehicles') or []
                row['api_comps_count_raw'] = len(comps)
            except AuthErr:
                raise
            except Exception as e:
                err.append('comps:' + str(e)[:80])
    except AuthErr:
        raise
    except Exception as e:
        err.append(str(e)[:200])
    row['latency_ms'] = int((time.time() - t0) * 1000)
    row['error'] = '; '.join(err) if err else None
    insert_row(conn, row)
    return row

def insert_row(conn, row):
    cols = list(row.keys())
    ph = ','.join(['%s'] * len(cols))
    sql = 'INSERT INTO shadow.vauto_api_shadow (' + ','.join(cols) + ') VALUES (' + ph + ') ON CONFLICT (bid_id) DO NOTHING'
    with conn.cursor() as cur:
        cur.execute(sql, [row[c] for c in cols])

def run_loop():
    conn = db()
    sess = None
    sess_t = 0
    http = None
    while _run:
        try:
            if sess is None or (time.time() - sess_t) > 60:
                sess = load_session(conn, SESSION_LABEL)
                sess_t = time.time()
                if not sess:
                    log.error('no session label=%s; sleeping', SESSION_LABEL)
                    time.sleep(15)
                    continue
                if sess['age_s'] and sess['age_s'] > 7200:
                    log.warning('session age %.0fs label=%s', sess['age_s'], SESSION_LABEL)
                http = make_http(sess)
            bid = select_bid(conn)
            if not bid:
                time.sleep(POLL_INTERVAL)
                continue
            r = process(conn, http, bid)
            log.info('bid=%s vin=%s series=%r comps=%s carfax_ok=%s ac=%s %dms err=%s',
                     r['bid_id'], r['vin'], r.get('api_series_trim'), r.get('api_comps_count_raw'),
                     r.get('api_carfax_ok'), r.get('api_autocheck_ok'), r.get('latency_ms'), r.get('error'))
            time.sleep(POLL_INTERVAL)
        except AuthErr as e:
            log.warning('auth %s - backoff 30s, reload session (NO refresh)', e)
            sess = None
            time.sleep(30)
        except Exception as e:
            log.error('loop err: %s', e)
            time.sleep(10)
    log.info('shutdown')

def run_vin(vin, bid_id, miles=None):
    conn = db()
    sess = load_session(conn, SESSION_LABEL)
    if not sess:
        print('NO SESSION for label=' + SESSION_LABEL)
        return
    print('session age_s=%.0f label=%s' % (sess['age_s'] or -1, SESSION_LABEL))
    http = make_http(sess)
    bid = {'bid_id': bid_id, 'vin': vin, 'live_trim': None, 'live_canon_trim': None, 'miles': miles}
    try:
        r = process(conn, http, bid)
    except AuthErr as e:
        print('AUTH ERROR:', e)
        return
    skip = ('api_vehicle_info', 'api_carfax_report', 'api_price_guides', 'api_option_codes')
    print(json.dumps({k: v for k, v in r.items() if k not in skip}, default=str, indent=2))

if __name__ == '__main__':
    if '--vin' in sys.argv:
        vin = sys.argv[sys.argv.index('--vin') + 1]
        bid_id = int(sys.argv[sys.argv.index('--bid-id') + 1]) if '--bid-id' in sys.argv else 0
        run_vin(vin, bid_id)
    elif '--once' in sys.argv:
        conn = db()
        sess = load_session(conn, SESSION_LABEL)
        if not sess:
            print('NO SESSION')
            sys.exit(1)
        http = make_http(sess)
        bid = select_bid(conn)
        if not bid:
            print('NO ELIGIBLE BID')
            sys.exit(0)
        r = process(conn, http, bid)
        print('processed bid', r['bid_id'], 'series', r.get('api_series_trim'), 'err', r.get('error'))
    else:
        run_loop()
