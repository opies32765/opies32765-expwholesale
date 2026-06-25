#!/usr/bin/env python3
"""
ew_recon_cost_sync.py — refresh per-car recon + transport cost from the LSL deal.

Recon cost is entered on the LSL deal LATER (post-purchase, when the work's done),
so this keeps each board car's cost current. recon = recon_cost field else the
'Recon - $X' supp-cost line; transport = transport_fee field else 'Transport - $X'.
Cron */5. Sentinel-gated, standby-guarded, single pass, read-only on LSL (HR6/HR3).
"""
import os, sys, re, json, sqlite3
import psycopg2, psycopg2.extras

SENTINEL = '/opt/expwholesale/RECON_ENABLED'
DB_DSN = os.environ.get('DATABASE_URL') or \
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'
LSL_DB = os.environ.get('LSL_DB_PATH', '/opt/livesaleslog/crm.db')
OPEN = ('in_transit_stage0', 'in_recon', 'frontline_ready', 'on_hold')
RECON_RX = re.compile(r'Recon\s*-\s*\$?([\d,]+\.?\d*)', re.I)
TRANS_RX = re.compile(r'Transport\s*-\s*\$?([\d,]+\.?\d*)', re.I)


def money(s):
    try:
        return float(str(s).replace(',', '').replace('$', '')) if s not in (None, '') else 0.0
    except Exception:
        return 0.0


def log(m):
    print('[recon-cost-sync] ' + m, flush=True)


def main():
    if not os.path.exists(SENTINEL):
        log('RECON_ENABLED absent — dark'); return 0
    db = psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = db.cursor()
    cur.execute("SELECT pg_is_in_recovery() AS rec")
    if cur.fetchone()['rec']:
        log('standby — no-op'); db.close(); return 0
    cur.execute("SELECT id, stock_no, vin FROM recon_units WHERE status IN %s", (OPEN,))
    units = cur.fetchall()

    crm = sqlite3.connect('file:%s?mode=ro' % LSL_DB, uri=True, timeout=5)
    crm.row_factory = sqlite3.Row
    upd = 0
    for u in units:
        r = None
        if u['stock_no']:
            r = crm.execute("SELECT recon_cost, transport_fee, supp_costs_desc, raw_json "
                            "FROM deals WHERE stock_no=? ORDER BY created_at DESC LIMIT 1",
                            (u['stock_no'],)).fetchone()
        if not r and u['vin']:
            r = crm.execute("SELECT recon_cost, transport_fee, supp_costs_desc, raw_json "
                            "FROM deals WHERE vin_no=? ORDER BY created_at DESC LIMIT 1",
                            (u['vin'],)).fetchone()
        if not r:
            continue
        desc = r['supp_costs_desc'] or ''
        rm = RECON_RX.search(desc)
        tm = TRANS_RX.search(desc)
        recon = max(money(r['recon_cost']), money(rm.group(1)) if rm else 0.0)
        trans = max(money(r['transport_fee']), money(tm.group(1)) if tm else 0.0)
        try:
            att = int((json.loads(r['raw_json']) or {}).get('totalAttachments') or 0) if r['raw_json'] else 0
        except Exception:
            att = 0
        cur.execute("UPDATE recon_units SET lsl_recon_cost=%s, lsl_transport_cost=%s, "
                    "lsl_attachments=%s, cost_synced_at=now() WHERE id=%s",
                    (recon, trans, att, u['id']))
        upd += 1
    crm.close()
    db.commit()
    db.close()
    log('done units=%d updated=%d' % (len(units), upd))
    return 0


if __name__ == '__main__':
    sys.exit(main())
