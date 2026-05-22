#!/usr/bin/env python3
"""LSL_SYNC_PICKERS_2026_05_21
Mirror LSL crm.db's supplier universe (last 12mo activity) + active sales
reps into EW Postgres so the bid-card Book-in-LSL form can autocomplete
without re-hitting LSL on every keystroke.

Reads /opt/livesaleslog/crm.db read-only. Idempotent UPSERTs into:
  - lsl_suppliers
  - lsl_sales_reps

Run on demand:    /opt/expwholesale/venv/bin/python /opt/expwholesale/lsl_sync_pickers.py
Cron-friendly:    non-fatal exit on errors; logs to stderr.

LSL field names are mirrored verbatim from camelCase to snake_case.
"""
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

CRM_DB = '/opt/livesaleslog/crm.db'
PG_DSN = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale',
)
EW_DEALER_ID = 10010   # Experience Wholesale in LSL


def log(msg):
    print(f'[{datetime.now(timezone.utc).isoformat(timespec="seconds")}] {msg}',
          file=sys.stderr, flush=True)


def sync_suppliers(sqc, pgc):
    """Pull every supplier we've bought from in the last 12 months,
    enriched with contact info from the suppliers table."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime('%Y-%m-%d')

    sqc.execute('''
        SELECT supplier_id,
               MAX(supplier_name)       AS supplier_name,
               COUNT(*)                 AS deals_12mo,
               SUM(purchase_cost)       AS spent_12mo,
               MAX(sold_at)             AS last_bought_at,
               MIN(sold_at)             AS first_bought_at
          FROM deals
         WHERE supplier_id IS NOT NULL
           AND supplier_name IS NOT NULL AND supplier_name <> ''
           AND sold_at >= ?
           AND type IN ('Booked','Delivered')
         GROUP BY supplier_id
    ''', (cutoff,))
    activity = {r['supplier_id']: dict(r) for r in sqc.fetchall()}

    if not activity:
        log('no 12mo supplier activity; nothing to sync')
        return 0

    ids = tuple(activity.keys())
    placeholders = ','.join('?' * len(ids))
    sqc.execute(f'''
        SELECT id, name, status, primary_contact, primary_contact_email,
               primary_contact_mobile, email, office, address1, city,
               state, postal_code, approved, trusted, is_blocked
          FROM suppliers
         WHERE id IN ({placeholders})
    ''', ids)
    contacts = {r['id']: dict(r) for r in sqc.fetchall()}

    rows = []
    for sid, a in activity.items():
        c = contacts.get(sid, {})
        rows.append((
            sid,
            (c.get('name') or a['supplier_name'] or '').strip(),
            c.get('status'),
            c.get('primary_contact'),
            c.get('primary_contact_email'),
            c.get('primary_contact_mobile'),
            c.get('email'),
            c.get('office'),
            c.get('address1'),
            c.get('city'),
            c.get('state'),
            c.get('postal_code'),
            bool(c.get('approved')),
            bool(c.get('trusted')),
            bool(c.get('is_blocked')),
            int(a['deals_12mo'] or 0),
            float(a['spent_12mo'] or 0),
            a['last_bought_at'],
            a['first_bought_at'],
        ))

    cur = pgc.cursor()
    psycopg2.extras.execute_values(cur, '''
        INSERT INTO lsl_suppliers
            (id, name, status, primary_contact, primary_contact_email,
             primary_contact_mobile, email, office, address1, city, state,
             postal_code, approved, trusted, is_blocked,
             deals_12mo, spent_12mo, last_bought_at, first_bought_at)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            status = EXCLUDED.status,
            primary_contact = EXCLUDED.primary_contact,
            primary_contact_email = EXCLUDED.primary_contact_email,
            primary_contact_mobile = EXCLUDED.primary_contact_mobile,
            email = EXCLUDED.email,
            office = EXCLUDED.office,
            address1 = EXCLUDED.address1,
            city = EXCLUDED.city,
            state = EXCLUDED.state,
            postal_code = EXCLUDED.postal_code,
            approved = EXCLUDED.approved,
            trusted = EXCLUDED.trusted,
            is_blocked = EXCLUDED.is_blocked,
            deals_12mo = EXCLUDED.deals_12mo,
            spent_12mo = EXCLUDED.spent_12mo,
            last_bought_at = EXCLUDED.last_bought_at,
            first_bought_at = EXCLUDED.first_bought_at,
            synced_at = NOW()
    ''', rows)
    pgc.commit()
    return len(rows)


def sync_sales_reps(sqc, pgc):
    """Active LSL users at EW dealer 10010, enriched with 12mo deal count
    on any role (sales_person / sales_manager / booked_by / buyer)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime('%Y-%m-%d')

    sqc.execute('''
        SELECT name, COUNT(*) AS n, MAX(sold_at) AS last_active FROM (
          SELECT sales_person  AS name, sold_at FROM deals WHERE sold_at >= ? AND sales_person  IS NOT NULL
          UNION ALL
          SELECT sales_manager AS name, sold_at FROM deals WHERE sold_at >= ? AND sales_manager IS NOT NULL
          UNION ALL
          SELECT booked_by     AS name, sold_at FROM deals WHERE sold_at >= ? AND booked_by     IS NOT NULL
          UNION ALL
          SELECT buyer_name    AS name, sold_at FROM deals WHERE sold_at >= ? AND buyer_name    IS NOT NULL
        ) WHERE name <> '' GROUP BY name
    ''', (cutoff, cutoff, cutoff, cutoff))
    activity = {r['name'].strip().lower(): dict(r) for r in sqc.fetchall()}

    sqc.execute('''
        SELECT id, first_name, last_name, email, telephone, status, dealer_id
          FROM users WHERE status = 'Active' AND dealer_id = ?
    ''', (EW_DEALER_ID,))
    users = [dict(r) for r in sqc.fetchall()]

    rows = []
    for u in users:
        full = f"{(u.get('first_name') or '').strip()} {(u.get('last_name') or '').strip()}".strip().lower()
        a = activity.get(full, {})
        rows.append((
            u['id'],
            u.get('first_name'),
            u.get('last_name'),
            u.get('email'),
            u.get('telephone'),
            u.get('status'),
            u.get('dealer_id'),
            int(a.get('n') or 0),
            a.get('last_active'),
        ))

    cur = pgc.cursor()
    psycopg2.extras.execute_values(cur, '''
        INSERT INTO lsl_sales_reps
            (id, first_name, last_name, email, telephone, status,
             dealer_id, deals_12mo, last_active)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            first_name = EXCLUDED.first_name,
            last_name  = EXCLUDED.last_name,
            email      = EXCLUDED.email,
            telephone  = EXCLUDED.telephone,
            status     = EXCLUDED.status,
            dealer_id  = EXCLUDED.dealer_id,
            deals_12mo = EXCLUDED.deals_12mo,
            last_active = EXCLUDED.last_active,
            synced_at  = NOW()
    ''', rows)
    pgc.commit()
    return len(rows)


def sync_customers(sqc, pgc):
    """Counterparties EW SOLD to in last 12 months. Most rows are
    wholesale (customer_name == supplier_name, no customer_id); a small
    minority are retail Individuals/Leads with a real customer_id uuid.
    Mirror both shapes into lsl_customers; counterparty_kind disambiguates."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime('%Y-%m-%d')

    sqc.execute('''
        SELECT customer_id,
               MAX(customer_name)            AS customer_name,
               MAX(customer_type)            AS customer_type,
               MAX(supplier_id)              AS supplier_id,
               COUNT(*)                      AS deals_12mo,
               SUM(sale_price)               AS sold_12mo,
               MAX(sold_at)                  AS last_sold_at,
               MIN(sold_at)                  AS first_sold_at,
               MAX(CASE WHEN customer_name = supplier_name THEN 1 ELSE 0 END)
                                             AS same_as_supplier
          FROM deals
         WHERE customer_name IS NOT NULL AND customer_name <> ''
           AND sold_at >= ?
           AND type IN ('Booked','Delivered')
         GROUP BY customer_id, customer_name
    ''', (cutoff,))
    activity = [dict(r) for r in sqc.fetchall()]
    if not activity:
        log('no 12mo customer activity; nothing to sync')
        return 0

    # Pull contact info for retail customers (those with a customer_id uuid)
    retail_ids = tuple(r['customer_id'] for r in activity if r['customer_id'])
    contacts = {}
    if retail_ids:
        placeholders = ','.join('?' * len(retail_ids))
        sqc.execute(f'''
            SELECT customer_id, email, mobile, full_address, lead_status,
                   is_blocked, type
              FROM customers
             WHERE customer_id IN ({placeholders})
        ''', retail_ids)
        contacts = {r['customer_id']: dict(r) for r in sqc.fetchall()}

    rows = []
    for a in activity:
        cid = a['customer_id']
        sid = a['supplier_id']
        if cid:
            kind = (contacts.get(cid, {}).get('type') or a['customer_type'] or 'Individual').strip()
            kind_norm = 'individual' if kind == 'Individual' else ('lead' if kind == 'Lead' else 'other')
            c = contacts.get(cid, {})
        else:
            # wholesale-flip row (same dealer on both sides)
            kind_norm = 'wholesale'
            c = {}
        rows.append((
            kind_norm,
            cid,
            sid if not cid else None,
            (a['customer_name'] or '').strip(),
            a['customer_type'],
            c.get('email'),
            c.get('mobile'),
            c.get('full_address'),
            c.get('lead_status'),
            bool(c.get('is_blocked')),
            int(a['deals_12mo'] or 0),
            float(a['sold_12mo'] or 0),
            a['last_sold_at'],
            a['first_sold_at'],
        ))

    cur = pgc.cursor()
    # Wipe-and-replace is simplest (no surrogate-id keep across runs);
    # SERIAL ids are not referenced by anything else yet.
    cur.execute('TRUNCATE lsl_customers RESTART IDENTITY')
    psycopg2.extras.execute_values(cur, '''
        INSERT INTO lsl_customers
            (counterparty_kind, customer_id, supplier_id, name, customer_type,
             email, mobile, full_address, lead_status, is_blocked,
             deals_12mo, sold_12mo, last_sold_at, first_sold_at)
        VALUES %s
    ''', rows)
    pgc.commit()
    return len(rows)


def main():
    if not os.path.exists(CRM_DB):
        log(f'crm.db not found at {CRM_DB}')
        return 1
    sq = sqlite3.connect(f'file:{CRM_DB}?mode=ro&immutable=1', uri=True)
    sq.row_factory = sqlite3.Row
    sqc = sq.cursor()
    pgc = psycopg2.connect(PG_DSN)
    try:
        n_sup  = sync_suppliers(sqc, pgc)
        n_rep  = sync_sales_reps(sqc, pgc)
        n_cust = sync_customers(sqc, pgc)
        log(f'synced: suppliers={n_sup}  sales_reps={n_rep}  customers={n_cust}')
    finally:
        sq.close()
        pgc.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
