"""lsl_training_export.py — refresh /opt/expwholesale's lsl_training table
from /opt/livesaleslog/crm.db.

v2 (2026-05-08): also joins the new inventory_blackbook + inventory_appraisal
tables produced by extract_inventory_appraisal.py. Adds 13 BB columns and
8 vAuto appraisal/market-depth columns to the training matrix.

Joins LSL deals + inventory by vin_no, parses year out of vehicle_info,
computes derived ratios, and bulk-inserts into PG. Runs daily at 4 AM ET
via /etc/cron.d/lsl_training_export.

Usage:
    /opt/expwholesale/venv/bin/python /opt/expwholesale/lsl_training_export.py
    /opt/expwholesale/venv/bin/python /opt/expwholesale/lsl_training_export.py --dry-run
"""
from __future__ import annotations
import argparse
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

import psycopg2
import psycopg2.extras


LSL_DB_PATH = os.environ.get('LSL_DB_PATH', '/opt/livesaleslog/crm.db')
EW_DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')


# Pull deal + inventory + Black Book + appraisal in one JOIN.
# LEFT JOINs everywhere so deals without enrichment still come through —
# trees handle NULLs via -1 sentinel in train_per_make.py.
SOURCE_SQL = """
SELECT
    d.id                            AS deal_id,
    UPPER(TRIM(d.vin_no))           AS vin,
    i.id                            AS inventory_id,
    d.stock_no                      AS stock_no,

    d.vehicle_info                  AS vehicle_info,
    i.group_model_trim_year         AS raw_gmty,
    d.make_name                     AS make_name,
    i.group_model_name              AS raw_model_name,
    i.vehicle_series_name           AS raw_series_name,
    i.group_model_trim              AS raw_trim_name,
    i.exterior_color                AS exterior_color,
    i.interior_color                AS interior_color,
    i.usage                         AS odometer,

    i.original_msrp                 AS original_msrp,
    i.msrp                          AS msrp,
    i.est_wholesale_price           AS est_wholesale_price,
    i.market_asking_price           AS market_asking_price,
    i.asking_price                  AS asking_price,
    i.base_appraised_value          AS base_appraised_value,
    i.mileage_adjustment_value      AS mileage_adjustment_value,

    d.sale_type                     AS sale_type,
    d.vehicle_sale_type             AS vehicle_sale_type,
    d.type                          AS deal_type,
    d.status                        AS deal_status,
    d.sales_person                  AS sales_person,
    d.sales_manager                 AS sales_manager,

    d.customer_name                 AS customer_name,
    d.supplier_name                 AS supplier_name,
    d.source_name                   AS source_name,

    d.sold_at                       AS sold_at,
    d.created_at                    AS created_at_lsl,
    d.days_on_lot                   AS days_on_lot,
    d.days_since_purchase           AS days_since_purchase,

    d.purchase_cost                 AS purchase_cost,
    d.sale_price                    AS sale_price,
    d.front_value                   AS front_value,
    d.deal_total_value              AS deal_total_value,
    d.transport_fee                 AS transport_fee,
    d.recon_cost                    AS recon_cost,

    -- ── Black Book (inventory_blackbook, populated 2024+) ─────────────
    bb.base_wholesale_average       AS bb_base_wholesale_avg,
    bb.adjusted_wholesale_average   AS bb_adj_wholesale_avg,
    bb.adjusted_wholesale_clean     AS bb_adj_wholesale_clean,
    bb.adjusted_private_average     AS bb_adj_private_avg,
    bb.adjusted_private_clean       AS bb_adj_private_clean,
    bb.adjusted_retail_average      AS bb_adj_retail_avg,
    bb.adjusted_retail_clean        AS bb_adj_retail_clean,
    bb.adjusted_trade_in_average    AS bb_adj_trade_in_avg,
    bb.adjusted_trade_in_clean      AS bb_adj_trade_in_clean,
    bb.msrp                         AS bb_msrp,
    bb.retail_equipped              AS bb_retail_equipped,
    bb.wholesale_adjustment         AS bb_wholesale_adjustment,
    bb.last_checked_at              AS bb_last_checked_at,

    -- ── vAuto appraisal snapshot at acquisition ───────────────────────
    a.appraisal_id                  AS app_id,
    a.appraised_at                  AS app_appraised_at,
    a.appraised_value               AS app_appraised_value,
    a.market_asking_price           AS app_market_asking_price,
    a.available_in_market           AS app_available_in_market,
    a.original_available_in_market  AS app_original_available_in_market,
    a.avg_days_on_market            AS app_avg_days_on_market,
    a.avg_days_supply               AS app_avg_days_supply,
    a.n_appraisals_total            AS app_n_total

FROM deals d
LEFT JOIN inventory i             ON i.vin_no = d.vin_no
LEFT JOIN inventory_blackbook bb  ON bb.inventory_id = i.id
LEFT JOIN inventory_appraisal a   ON a.inventory_id = i.id
WHERE d.purchase_cost IS NOT NULL
  AND d.purchase_cost > 0
  AND d.vin_no IS NOT NULL
  AND length(d.vin_no) >= 11
"""


_BODY_PATTERNS = [
    ('Sport Utility', 'SUV'),
    ('Crew Cab',      'Crew Cab'),
    ('Quad Cab',      'Quad Cab'),
    ('Extended Cab',  'Extended Cab'),
    ('Regular Cab',   'Regular Cab'),
    ('Convertible',   'Convertible'),
    ('Cabriolet',     'Convertible'),
    ('Roadster',      'Convertible'),
    ('Spyder',        'Convertible'),
    ('Spider',        'Convertible'),
    ('Targa',         'Targa'),
    ('Hatchback',     'Hatchback'),
    ('Coupe',         'Coupe'),
    ('Sedan',         'Sedan'),
    ('Wagon',         'Wagon'),
    ('Minivan',       'Minivan'),
    ('Pickup',        'Pickup'),
    ('Truck',         'Truck'),
    ('Van',           'Van'),
]


def _parse_body_type(vehicle_info):
    if not vehicle_info: return None
    up = vehicle_info.upper()
    for needle, canonical in _BODY_PATTERNS:
        if needle.upper() in up: return canonical
    return None


def _parse_year_from_vehicle_info(vi):
    if not vi: return None
    m = re.match(r'^\s*(\d{4})\b', vi)
    if not m: return None
    y = int(m.group(1))
    return y if 1990 <= y <= 2030 else None


def _strip_make_prefix(text, make):
    if not text: return None
    if make and text.upper().startswith(make.upper() + ' '):
        return text[len(make) + 1:].strip() or None
    return text.strip() or None


def _safe_div(num, den):
    if num is None or den is None: return None
    try:
        if float(den) == 0: return None
        return round(float(num) / float(den), 4)
    except (TypeError, ValueError):
        return None


def transform_row(r):
    out = dict(r)
    out['year'] = _parse_year_from_vehicle_info(out.get('vehicle_info'))
    make = out.get('make_name')
    out['model_name'] = _strip_make_prefix(out.pop('raw_model_name', None), make)
    out['series_name'] = _strip_make_prefix(out.pop('raw_series_name', None), make)
    out['trim_name'] = _strip_make_prefix(out.pop('raw_trim_name', None), make)
    out.pop('raw_gmty', None)
    out['body_type'] = _parse_body_type(out.get('vehicle_info'))
    out['purchase_to_wholesale_ratio'] = _safe_div(
        out.get('purchase_cost'), out.get('est_wholesale_price'))
    out['purchase_to_retail_ratio'] = _safe_div(
        out.get('purchase_cost'), out.get('market_asking_price'))
    out['sale_to_purchase_ratio'] = _safe_div(
        out.get('sale_price'), out.get('purchase_cost'))
    pc = out.get('purchase_cost') or 0
    sp = out.get('sale_price') or 0
    out['gross_dollars'] = round(float(sp) - float(pc), 2) if (pc and sp) else None

    # Indicator: do we have a Black Book record at all?
    out['bb_present'] = 1 if out.get('bb_adj_wholesale_avg') is not None else 0
    # Supply trend: how much has this model's market depleted since
    # vAuto first observed it? Positive = supply tightened.
    aim = out.get('app_available_in_market')
    oaim = out.get('app_original_available_in_market')
    out['app_supply_depletion'] = (
        oaim - aim if (aim is not None and oaim is not None and oaim >= aim)
        else None)
    return out


COLUMNS = [
    'deal_id', 'vin', 'inventory_id', 'stock_no',
    'vehicle_info', 'year', 'make_name', 'model_name', 'series_name',
    'trim_name', 'body_type', 'exterior_color', 'interior_color', 'odometer',
    'original_msrp', 'msrp', 'est_wholesale_price', 'market_asking_price',
    'asking_price', 'base_appraised_value', 'mileage_adjustment_value',
    'sale_type', 'vehicle_sale_type', 'deal_type', 'deal_status',
    'sales_person', 'sales_manager',
    'customer_name', 'supplier_name', 'source_name',
    'sold_at', 'created_at_lsl', 'days_on_lot', 'days_since_purchase',
    'purchase_cost', 'sale_price', 'front_value', 'deal_total_value',
    'transport_fee', 'recon_cost',
    'purchase_to_wholesale_ratio', 'purchase_to_retail_ratio',
    'sale_to_purchase_ratio', 'gross_dollars',

    # NEW v2 columns
    'bb_present',
    'bb_base_wholesale_avg', 'bb_adj_wholesale_avg', 'bb_adj_wholesale_clean',
    'bb_adj_private_avg', 'bb_adj_private_clean',
    'bb_adj_retail_avg', 'bb_adj_retail_clean',
    'bb_adj_trade_in_avg', 'bb_adj_trade_in_clean',
    'bb_msrp', 'bb_retail_equipped', 'bb_wholesale_adjustment',
    'bb_last_checked_at',
    'app_id', 'app_appraised_at', 'app_appraised_value',
    'app_market_asking_price',
    'app_available_in_market', 'app_original_available_in_market',
    'app_avg_days_on_market', 'app_avg_days_supply', 'app_n_total',
    'app_supply_depletion',
]


# Schema migration — add v2 columns if not present (idempotent)
ALTER_DDL = """
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_present                       INTEGER;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_base_wholesale_avg            REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_adj_wholesale_avg             REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_adj_wholesale_clean           REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_adj_private_avg               REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_adj_private_clean             REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_adj_retail_avg                REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_adj_retail_clean              REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_adj_trade_in_avg              REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_adj_trade_in_clean            REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_msrp                          REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_retail_equipped               REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_wholesale_adjustment          REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS bb_last_checked_at               TIMESTAMPTZ;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS app_id                           INTEGER;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS app_appraised_at                 TIMESTAMPTZ;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS app_appraised_value              REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS app_market_asking_price          REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS app_available_in_market          REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS app_original_available_in_market REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS app_avg_days_on_market           REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS app_avg_days_supply              REAL;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS app_n_total                      INTEGER;
ALTER TABLE lsl_training ADD COLUMN IF NOT EXISTS app_supply_depletion             REAL;
"""


def main(dry_run=False):
    t0 = time.monotonic()
    print(f'[lsl_training v2] starting refresh at {datetime.utcnow().isoformat()}Z',
          flush=True)

    sl = sqlite3.connect(f'file:{LSL_DB_PATH}?mode=ro', uri=True)
    sl.row_factory = sqlite3.Row
    cur_sl = sl.cursor()
    cur_sl.execute(SOURCE_SQL)
    raw = cur_sl.fetchall()
    sl.close()
    n_raw = len(raw)
    print(f'[lsl_training] read {n_raw:,} rows from LSL crm.db', flush=True)

    transformed_by_id = {}
    n_dup = 0
    n_err = 0
    for r in raw:
        try:
            row = transform_row(dict(r))
            did = row.get('deal_id')
            if did in transformed_by_id:
                n_dup += 1
                existing = transformed_by_id[did]
                if (row.get('purchase_cost') or 0) >= (existing.get('purchase_cost') or 0):
                    transformed_by_id[did] = row
            else:
                transformed_by_id[did] = row
        except Exception as e:
            n_err += 1
            print(f'[lsl_training] transform err on deal_id={r["deal_id"]}: {e}',
                  flush=True)
    transformed = list(transformed_by_id.values())
    n_t = len(transformed)
    print(f'[lsl_training] transformed {n_t:,} rows '
          f'({n_err} errors, {n_dup} dedup)', flush=True)

    # Coverage stats over the new columns
    n_bb = sum(1 for r in transformed if r.get('bb_present'))
    n_app = sum(1 for r in transformed if r.get('app_appraised_value') is not None)
    print(f'[lsl_training] BB present:  {n_bb:,} ({100*n_bb/n_t:.1f}%)')
    print(f'[lsl_training] app present: {n_app:,} ({100*n_app/n_t:.1f}%)')

    if dry_run:
        print('[lsl_training] DRY RUN — printing 1 sample row, not writing')
        if transformed:
            for k, v in transformed[0].items():
                print(f'  {k:30} {v}')
        return

    pg = psycopg2.connect(EW_DB_URL)
    cur_pg = pg.cursor()
    # NOTE: Schema migration (ALTER_DDL above) is informational only.
    # expuser lacks ALTER privs; ALTERs were applied once as postgres
    # superuser via tools/alter_lsl_training.sql. If new columns are
    # added in the future, run that as postgres before deploying this file.

    # TRUNCATE + bulk-insert
    cur_pg.execute('TRUNCATE lsl_training')
    rows_for_insert = [tuple(r.get(c) for c in COLUMNS) for r in transformed]
    psycopg2.extras.execute_values(
        cur_pg,
        f'INSERT INTO lsl_training ({", ".join(COLUMNS)}) VALUES %s',
        rows_for_insert,
        page_size=1000,
    )
    pg.commit()

    cur_pg.execute("""
        SELECT COUNT(*) AS n,
               COUNT(*) FILTER (WHERE est_wholesale_price IS NOT NULL) AS has_wholesale,
               COUNT(*) FILTER (WHERE market_asking_price IS NOT NULL) AS has_retail,
               COUNT(*) FILTER (WHERE bb_present = 1) AS has_bb,
               COUNT(*) FILTER (WHERE app_appraised_value IS NOT NULL) AS has_app,
               COUNT(DISTINCT make_name) AS distinct_makes,
               MIN(sold_at) AS oldest_sold_at,
               MAX(sold_at) AS newest_sold_at
        FROM lsl_training
    """)
    s = cur_pg.fetchone()
    pg.close()

    print(f'[lsl_training v2] DONE in {time.monotonic()-t0:.1f}s')
    print(f'  rows:           {s[0]:,}')
    print(f'  has_wholesale:  {s[1]:,} ({100*s[1]/s[0]:.1f}%)')
    print(f'  has_retail:     {s[2]:,} ({100*s[2]/s[0]:.1f}%)')
    print(f'  has_blackbook:  {s[3]:,} ({100*s[3]/s[0]:.1f}%)')
    print(f'  has_appraisal:  {s[4]:,} ({100*s[4]/s[0]:.1f}%)')
    print(f'  distinct makes: {s[5]:,}')
    print(f'  date range:     {s[6]} -> {s[7]}')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true',
                   help="Read from SQLite + transform but don't write to PG")
    args = p.parse_args()
    main(dry_run=args.dry_run)
