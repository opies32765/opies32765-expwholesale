#!/usr/bin/env python3
"""
ew_recon_transport_sync.py — EW Recon Stage-0 transport sync (Phase 2).

Single-shot, idempotent. Reads the Dealer Direct ops sheet ONCE (the exact same
public-CSV reader app.py uses — HR3: one GET, no retry, no loop), mirrors every
VIN row into recon_transport, flags is_our_unit (VIN in EW bids OR LSL
inventory), and for OUR cars tracks them as stage-0 recon_units and AUTO-OPENS
Intake the moment a tracked unit flips to "delivered" (the delivered->recon seam).

FORWARD-TRACKING ONLY: it never mass-creates recon_units for the historical
"delivered" backlog — it only hands off cars it watched move pending/in_transit
-> delivered. The full sheet is still mirrored to recon_transport for visibility.

Self-contained: does NOT import app.py (avoids the import-time boot-DDL freeze).
Gated on the RECON_ENABLED sentinel. Standby-guarded (no-op on a PG replica).
Run by cron */15, or by hand. NEVER retries the sheet fetch.
"""
import os, sys, csv, io, json, secrets, urllib.request
from datetime import datetime, timezone
import sqlite3
import psycopg2, psycopg2.extras

SENTINEL = '/opt/expwholesale/RECON_ENABLED'
DB_DSN = os.environ.get('DATABASE_URL') or \
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'
LSL_DB = os.environ.get('LSL_DB_PATH', '/opt/livesaleslog/crm.db')
SHEET_ID = '1k9_7E3ZKrK-a-cxtlrnGkX100o0NLrw9MFU0zHjbAzs'
STORE_ID = 1


def log(m):
    print('[recon-transport-sync] ' + m, flush=True)


def _token():
    return secrets.token_urlsafe(16)


def _audit(cur, unit_id, action, detail):
    cur.execute("INSERT INTO recon_audit (unit_id,entity,action,actor,detail) "
                "VALUES (%s,'transport',%s,'transport_sync',%s)",
                (unit_id, action, json.dumps(detail or {})))


def fetch_sheet():
    """Verbatim copy of app._fetch_transport_tracker parsing. ONE GET, no retry."""
    url = 'https://docs.google.com/spreadsheets/d/%s/export?format=csv' % SHEET_ID
    raw = urllib.request.urlopen(
        urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}),
        timeout=20).read().decode('utf-8', 'replace')
    rows = list(csv.reader(io.StringIO(raw)))

    def g(r, i):
        return (r[i].strip() if i < len(r) and r[i] else '')

    pending, in_transit, delivered, mode = [], [], [], 'pending'
    for r in rows:
        if not any((x or '').strip() for x in r):
            continue
        c0 = (r[0] or '').strip()
        if c0.lower().startswith('vin') and g(r, 2).lower() == 'make':
            continue
        cl = c0.lower()
        if 'in transit' in cl:
            mode = 'in_transit'; continue
        if 'delivered' in cl:
            mode = 'delivered'; continue
        if not g(r, 2) and not g(r, 3):
            continue
        car = {'vin': g(r, 0), 'year': g(r, 1), 'make': g(r, 2), 'model': g(r, 3),
               'ymm': (g(r, 1) + ' ' + g(r, 2) + ' ' + g(r, 3)).strip(),
               'pickup': g(r, 4), 'delivery': g(r, 5), 'est_pickup': g(r, 6),
               'est_delivery': g(r, 7), 'status': g(r, 8), 'company': g(r, 9)}
        {'pending': pending, 'in_transit': in_transit,
         'delivered': delivered}.get(mode, pending).append(car)
    return {'pending': pending, 'in_transit': in_transit, 'delivered': delivered}


def our_vins(cur, sl):
    s = set()
    cur.execute("SELECT DISTINCT UPPER(vin) v FROM bids "
                "WHERE vin IS NOT NULL AND length(vin)=17")
    for r in cur.fetchall():
        s.add(r['v'])
    try:
        for row in sl.execute("SELECT DISTINCT UPPER(vin_no) v FROM inventory "
                              "WHERE vin_no IS NOT NULL AND length(vin_no)=17"):
            if row[0]:
                s.add(row[0])
    except Exception as e:
        log('lsl vin read warn: %s' % e)
    return s


def _yr(car):
    try:
        return int(car.get('year')) if car.get('year') else None
    except Exception:
        return None


def upsert_transport(cur, vin, sub, car, is_our, now):
    cur.execute("SELECT id, sub_status FROM recon_transport WHERE vin=%s "
                "ORDER BY id DESC LIMIT 1", (vin,))
    row = cur.fetchone()
    vals = (sub, car.get('year') or None, car.get('make') or None,
            car.get('model') or None, car.get('ymm') or None,
            car.get('pickup') or None, car.get('delivery') or None,
            car.get('est_pickup') or None, car.get('est_delivery') or None,
            car.get('status') or None, car.get('company') or None, is_our)
    if row:
        changed = row['sub_status'] != sub
        cur.execute("""UPDATE recon_transport SET
              sub_status=%s, year=%s, make=%s, model=%s, ymm=%s,
              pickup_loc=%s, delivery_loc=%s, est_pickup=%s, est_delivery=%s,
              row_status_flag=%s, company=%s, is_our_unit=%s,
              sub_changed_at=CASE WHEN %s THEN %s ELSE sub_changed_at END,
              delivered_seen_at=CASE WHEN %s='delivered' AND delivered_seen_at IS NULL
                                     THEN %s ELSE delivered_seen_at END,
              last_synced_at=%s
            WHERE id=%s""",
            vals + (changed, now, sub, now, now, row['id']))
    else:
        cur.execute("""INSERT INTO recon_transport
              (vin, sub_status, year, make, model, ymm, pickup_loc, delivery_loc,
               est_pickup, est_delivery, row_status_flag, company, is_our_unit,
               first_seen_at, sub_changed_at, delivered_seen_at, last_synced_at, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ops_sheet')""",
            (vin,) + vals + (now, now, now if sub == 'delivered' else None, now))


def _open_unit(cur, vin):
    cur.execute("""SELECT id, status FROM recon_units
                   WHERE vin=%s AND status IN
                     ('in_transit_stage0','in_recon','frontline_ready','on_hold')
                   ORDER BY id DESC LIMIT 1""", (vin,))
    return cur.fetchone()


def ensure_stage0(cur, vin, sub, car, tstep, now):
    """Create or refresh a stage-0 (in transit) unit for one of OUR cars."""
    u = _open_unit(cur, vin)
    if u:
        if u['status'] == 'in_transit_stage0':
            cur.execute("""UPDATE recon_units SET sub_status=%s,
                  in_transit_at=CASE WHEN %s='in_transit' AND in_transit_at IS NULL
                                     THEN %s ELSE in_transit_at END,
                  transport_company=COALESCE(%s, transport_company), updated_at=now()
                WHERE id=%s""",
                (sub, sub, now, car.get('company') or None, u['id']))
        return False  # already existed (transit unit, or already in recon)
    cur.execute("""INSERT INTO recon_units
          (vin, year, make, model, vehicle_class, status, sub_status,
           current_step_id, current_step_entered_at, acquired_at, in_transit_at,
           transport_company, source, recon_token)
        VALUES (%s,%s,%s,%s,'highline','in_transit_stage0',%s,%s,%s,%s,%s,%s,
                'transport_sync',%s) RETURNING id""",
        (vin, _yr(car), car.get('make') or None, car.get('model') or None, sub,
         tstep, now, now, now if sub == 'in_transit' else None,
         car.get('company') or None, _token()))
    uid = cur.fetchone()['id']
    cur.execute("""INSERT INTO recon_step_events
          (unit_id, step_id, entered_at, moved_by, move_reason, auto)
        VALUES (%s,%s,%s,'transport_sync','transport_enter',TRUE) RETURNING id""",
        (uid, tstep, now))
    ev = cur.fetchone()['id']
    cur.execute("""INSERT INTO recon_unit_active_steps (unit_id, step_id, event_id, entered_at)
        VALUES (%s,%s,%s,%s) ON CONFLICT (unit_id, step_id) DO NOTHING""",
        (uid, tstep, ev, now))
    _audit(cur, uid, 'stage0_create', {'vin': vin, 'sub': sub})
    return True


def handoff_to_intake(cur, vin, now, istep):
    """delivered -> Intake, ONLY for a unit we were already tracking in stage-0.
    Historical/untracked delivered cars are intentionally NOT created here."""
    u = _open_unit(cur, vin)
    if not u or u['status'] != 'in_transit_stage0':
        return False  # never tracked (historical) OR already in recon -> no-op
    uid = u['id']
    cur.execute("""UPDATE recon_step_events SET exited_at=%s,
          duration_sec=EXTRACT(EPOCH FROM (%s - entered_at))::bigint
        WHERE unit_id=%s AND exited_at IS NULL""", (now, now, uid))
    cur.execute("DELETE FROM recon_unit_active_steps WHERE unit_id=%s", (uid,))
    cur.execute("""UPDATE recon_units SET status='in_recon', sub_status='delivered',
          delivered_at=%s, entered_recon_at=%s, current_step_id=%s,
          current_step_entered_at=%s, updated_at=now() WHERE id=%s""",
        (now, now, istep, now, uid))
    cur.execute("""INSERT INTO recon_step_events
          (unit_id, step_id, entered_at, moved_by, move_reason, from_step_id, auto)
        VALUES (%s,%s,%s,'transport_sync','delivered_to_intake',
                (SELECT id FROM recon_step_defs WHERE store_id=%s AND code='transport'),TRUE)
        RETURNING id""", (uid, istep, now, STORE_ID))
    ev = cur.fetchone()['id']
    cur.execute("""INSERT INTO recon_unit_active_steps (unit_id, step_id, event_id, entered_at)
        VALUES (%s,%s,%s,%s) ON CONFLICT (unit_id, step_id) DO NOTHING""",
        (uid, istep, ev, now))
    _audit(cur, uid, 'delivered_handoff', {'vin': vin})
    return True


def main():
    if not os.path.exists(SENTINEL):
        log('RECON_ENABLED absent — dark, exit'); return 0
    try:
        data = fetch_sheet()  # ONE GET. on failure: abort, no retry (HR3).
    except Exception as e:
        log('sheet fetch failed (no retry): %s' % str(e)[:160]); return 0

    db = psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = db.cursor()
    cur.execute("SELECT pg_is_in_recovery() AS rec")
    if cur.fetchone()['rec']:
        log('standby (in recovery) — no-op'); db.close(); return 0

    sl = sqlite3.connect('file:%s?mode=ro' % LSL_DB, uri=True, timeout=5)
    ours = our_vins(cur, sl); sl.close()
    cur.execute("SELECT id, code FROM recon_step_defs WHERE store_id=%s "
                "AND code IN ('transport','intake')", (STORE_ID,))
    sids = {r['code']: r['id'] for r in cur.fetchall()}
    tstep, istep = sids.get('transport'), sids.get('intake')
    if not tstep or not istep:
        log('step defs missing (transport/intake) — run the migration'); db.close(); return 1
    now = datetime.now(timezone.utc)

    st = {'rows': 0, 'ours': 0, 'created': 0, 'handoff': 0}
    for sub, cars in (('pending', data['pending']),
                      ('in_transit', data['in_transit']),
                      ('delivered', data['delivered'])):
        for car in cars:
            vin = (car.get('vin') or '').strip().upper()
            if len(vin) != 17:
                continue
            st['rows'] += 1
            is_our = vin in ours
            if is_our:
                st['ours'] += 1
            upsert_transport(cur, vin, sub, car, is_our, now)
            if is_our:
                if sub in ('pending', 'in_transit'):
                    if ensure_stage0(cur, vin, sub, car, tstep, now):
                        st['created'] += 1
                elif sub == 'delivered':
                    if handoff_to_intake(cur, vin, now, istep):
                        st['handoff'] += 1
    db.commit()
    db.close()
    log('done rows=%(rows)d ours=%(ours)d stage0_created=%(created)d '
        'delivered_handoff=%(handoff)d' % st)
    return 0


if __name__ == '__main__':
    sys.exit(main())
