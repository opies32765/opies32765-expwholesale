#!/usr/bin/env python3
"""
ew_recon_lsl_sync.py — EW Recon LSL inventory auto-sync (task #10).

"TODAY + seen-once" model (robust to back-dated / late-entered buys):
  * WINDOW = cars STOCKED today, OR whose DEAL was booked today, OR an unsold
    INDIVIDUAL (private-party) buy from the last few days — matches the LSL deals
    page plus private-party holds that sit unsold (a car stocked yesterday but
    sold this morning, or an individual buy being retailed, still belongs on the
    board). crm.db created_at is ISO-8601, so the date() filter is tz-shifted.
  * Each today car is recorded in recon_seen the first time we touch it.
  * INSERT a car only if it is NOT already on the board AND has never been seen
    (so a today car that gets entered into LSL hours after it was bought still
    imports the moment it appears, and a car the user REMOVED from the board is
    never resurrected).
  * UPDATE buying-from / sold-to / disposition on today cars already on the board
    (a buyer that lands after we pulled the car refreshes), never the step/status.

Single pass, no retry (HR3). Sentinel-gated, standby-guarded. Run every ~5 min.
"""
import os, sys, re, secrets, sqlite3
from datetime import datetime, timezone
import psycopg2, psycopg2.extras

SENTINEL = '/opt/expwholesale/RECON_ENABLED'
DB_DSN = os.environ.get('DATABASE_URL') or \
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'
LSL_DB = os.environ.get('LSL_DB_PATH', '/opt/livesaleslog/crm.db')
# dealership tz vs the crm.db UTC timestamps (June -> EDT = UTC-4)
TZ_OFFSET = os.environ.get('RECON_TZ_OFFSET', '-4 hours')
EXOTIC = {'maserati', 'bentley', 'ferrari', 'lamborghini', 'rolls-royce',
          'aston martin', 'mclaren', 'bugatti'}
OPEN = ('in_transit_stage0', 'in_recon', 'frontline_ready', 'on_hold')


def log(m):
    print('[recon-lsl-sync] ' + m, flush=True)


def main():
    if not os.path.exists(SENTINEL):
        log('RECON_ENABLED absent — dark, exit'); return 0
    db = psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = db.cursor()
    cur.execute("SELECT pg_is_in_recovery() AS rec")
    if cur.fetchone()['rec']:
        log('standby — no-op'); db.close(); return 0
    cur.execute("SELECT id FROM recon_step_defs WHERE store_id=1 AND code='all'")
    row = cur.fetchone()
    if not row:
        log('no "all" step — run migrations'); db.close(); return 1
    allstep = row['id']

    cur.execute("SELECT vin FROM recon_seen")
    seen = {r['vin'] for r in cur.fetchall()}

    crm = sqlite3.connect('file:%s?mode=ro' % LSL_DB, uri=True, timeout=5)
    crm.row_factory = sqlite3.Row
    rows = list(crm.execute("""
        SELECT id, stock_no, vin_no, group_model_trim_year, vehicle_make_name,
               group_model_name, exterior_color, usage, purchase_cost, source,
               purchased_from_type, customer_name, deal_sales_person_name,
               deal_status_name, disposition_intention, datetime(created_at) AS cdt
          FROM inventory
         WHERE date(created_at, ?) = date('now', ?)
            OR vin_no IN (SELECT vin_no FROM deals
                           WHERE date(created_at, ?) = date('now', ?) AND vin_no IS NOT NULL)
            OR (lower(purchased_from_type) = 'individual' AND deal_status_name = 'Available'
                AND date(created_at, ?) >= date('now', ?, '-3 days'))
         ORDER BY datetime(created_at)""",
        (TZ_OFFSET, TZ_OFFSET, TZ_OFFSET, TZ_OFFSET, TZ_OFFSET, TZ_OFFSET)))
    crm.close()

    ins = upd = 0
    for r in rows:
        vin = (r['vin_no'] or '').strip().upper()
        if len(vin) != 17:
            continue
        mk = r['vehicle_make_name']
        md = (r['group_model_name'] or '').replace(mk or '', '').strip() or r['group_model_name']
        ym = re.search(r'(19|20)\d\d', r['group_model_trim_year'] or '')
        year = int(ym.group(0)) if ym else None
        bft = 'Individual' if (r['purchased_from_type'] or '').lower() == 'individual' else 'Dealer'
        cls = 'exotic' if (mk or '').lower() in EXOTIC else 'highline'
        try:
            acq = datetime.fromisoformat((r['cdt'] or '').replace('Z', '')).replace(tzinfo=timezone.utc)
        except Exception:
            acq = None

        cur.execute("SELECT id FROM recon_units WHERE vin=%s AND status IN %s "
                    "ORDER BY id DESC LIMIT 1", (vin, OPEN))
        ex = cur.fetchone()
        # record that we've seen this today car (so a later removal won't resurrect it)
        cur.execute("INSERT INTO recon_seen (vin) VALUES (%s) ON CONFLICT (vin) DO NOTHING", (vin,))

        if ex:
            cur.execute("""UPDATE recon_units SET sold_to=%s, sold_to_salesperson=%s, deal_status=%s,
               bought_from=COALESCE(%s,bought_from), buying_from_type=COALESCE(%s,buying_from_type),
               lsl_disposition=COALESCE(%s,lsl_disposition), lsl_inventory_ref=COALESCE(lsl_inventory_ref,%s),
               updated_at=now() WHERE id=%s""",
               (r['customer_name'], r['deal_sales_person_name'], r['deal_status_name'],
                r['source'], bft, r['disposition_intention'], r['id'], ex['id']))
            upd += 1
            continue
        if vin in seen:
            continue  # was on the board before and the user removed it — leave it off
        now = datetime.now(timezone.utc)
        cur.execute("""INSERT INTO recon_units (vin, stock_no, year, make, model, exterior_color, miles,
           purchase_cost, vehicle_class, is_exotic, status, current_step_id, current_step_entered_at,
           acquired_at, bought_from, buying_from_type, sold_to, sold_to_salesperson, deal_status,
           lsl_disposition, lsl_inventory_ref, source, recon_token)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'in_recon',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'lsl_sync',%s)
           RETURNING id""",
           (vin, r['stock_no'], year, mk, md, r['exterior_color'], r['usage'], r['purchase_cost'], cls,
            cls == 'exotic', allstep, now, acq or now, r['source'], bft, r['customer_name'],
            r['deal_sales_person_name'], r['deal_status_name'], r['disposition_intention'], r['id'],
            secrets.token_urlsafe(16)))
        uid = cur.fetchone()['id']
        cur.execute("""INSERT INTO recon_step_events (unit_id, step_id, entered_at, moved_by, move_reason)
           VALUES (%s,%s,%s,'lsl_sync','lsl_into_all') RETURNING id""", (uid, allstep, now))
        ev = cur.fetchone()['id']
        cur.execute("""INSERT INTO recon_unit_active_steps (unit_id, step_id, event_id, entered_at)
           VALUES (%s,%s,%s,%s) ON CONFLICT (unit_id, step_id) DO NOTHING""", (uid, allstep, ev, now))
        ins += 1

    db.commit()
    db.close()
    log('done window=today(%s) rows=%d inserted=%d updated=%d' % (TZ_OFFSET, len(rows), ins, upd))
    return 0


if __name__ == '__main__':
    sys.exit(main())
