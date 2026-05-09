"""train_per_make.py v2 — per-make XGBoost trainer with Black Book + appraisal features.

v2 (2026-05-08): adds 13 BB columns + 8 vAuto appraisal/market-depth columns
introduced by extract_inventory_appraisal.py + lsl_training_export.py v2.

Saves models to /opt/expwholesale/ml/models_v2/per_make/ (NOT models/per_make/)
so the v1 production models are untouched. Direct comparison via
compare_v1_v2.py.

Usage:
  /opt/expwholesale/venv/bin/python /opt/expwholesale/train_per_make_v2.py
  /opt/expwholesale/venv/bin/python /opt/expwholesale/train_per_make_v2.py --min-rows 200
  /opt/expwholesale/venv/bin/python /opt/expwholesale/train_per_make_v2.py --makes AUDI,BMW
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
import xgboost as xgb


EW_DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
MODELS_DIR = Path(os.environ.get('ML_MODELS_DIR_V2',
                                 '/opt/expwholesale/ml/models_v2/per_make'))

TEST_CUTOFF = '2026-01-01'

# v1 features — preserved
NUMERIC_FEATURES_V1 = [
    'year', 'odometer', 'original_msrp', 'est_wholesale_price',
    'market_asking_price', 'base_appraised_value',
    'mileage_adjustment_value', 'days_on_lot', 'days_since_purchase',
    'sold_year', 'sold_month',
]

# v2 NEW features — Black Book (9 books + 4 meta)
NUMERIC_FEATURES_BB = [
    'bb_present',
    'bb_base_wholesale_avg',
    'bb_adj_wholesale_avg', 'bb_adj_wholesale_clean',
    'bb_adj_private_avg', 'bb_adj_private_clean',
    'bb_adj_retail_avg', 'bb_adj_retail_clean',
    'bb_adj_trade_in_avg', 'bb_adj_trade_in_clean',
    'bb_msrp', 'bb_retail_equipped', 'bb_wholesale_adjustment',
]

# v2 NEW features — vAuto market depth
NUMERIC_FEATURES_APP = [
    'app_appraised_value', 'app_market_asking_price',
    'app_available_in_market', 'app_original_available_in_market',
    'app_avg_days_on_market', 'app_avg_days_supply',
    'app_n_total', 'app_supply_depletion',
]

NUMERIC_FEATURES = (NUMERIC_FEATURES_V1
                    + NUMERIC_FEATURES_BB
                    + NUMERIC_FEATURES_APP)

CATEGORICAL_FEATURES = {
    'model_name':       30,
    'body_type':        10,
    'supplier_name':    50,
    'sale_type':         5,
    'vehicle_sale_type': 5,
}


def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')


def load_data():
    print('[train v2] loading lsl_training from PG...', flush=True)
    conn = psycopg2.connect(EW_DB_URL)
    df = pd.read_sql(f"""
        SELECT
            deal_id, vin, make_name, model_name, body_type,
            year, odometer,
            original_msrp, est_wholesale_price, market_asking_price,
            base_appraised_value, mileage_adjustment_value,
            sale_type, vehicle_sale_type, supplier_name,
            sold_at, days_on_lot, days_since_purchase,
            purchase_cost, sale_price,
            -- v2 cols
            bb_present,
            bb_base_wholesale_avg, bb_adj_wholesale_avg, bb_adj_wholesale_clean,
            bb_adj_private_avg, bb_adj_private_clean,
            bb_adj_retail_avg, bb_adj_retail_clean,
            bb_adj_trade_in_avg, bb_adj_trade_in_clean,
            bb_msrp, bb_retail_equipped, bb_wholesale_adjustment,
            app_appraised_value, app_market_asking_price,
            app_available_in_market, app_original_available_in_market,
            app_avg_days_on_market, app_avg_days_supply,
            app_n_total, app_supply_depletion
        FROM lsl_training
        WHERE purchase_cost IS NOT NULL AND purchase_cost > 0
          AND est_wholesale_price IS NOT NULL AND est_wholesale_price > 0
          AND year IS NOT NULL
          AND make_name IS NOT NULL
    """, conn)
    conn.close()

    df['make_name'] = df['make_name'].str.upper().str.strip()
    df['sold_at'] = pd.to_datetime(df['sold_at'], errors='coerce', utc=True)
    df['sold_year'] = df['sold_at'].dt.year
    df['sold_month'] = df['sold_at'].dt.month

    df = df[df['purchase_cost'].between(500, 1_000_000)]
    df = df[df['year'].between(1990, 2030)]

    print(f'[train v2] loaded {len(df):,} rows after sanity filters', flush=True)
    n_bb = (df['bb_present'] == 1).sum()
    n_app = df['app_appraised_value'].notna().sum()
    print(f'[train v2] BB-enriched: {n_bb:,} ({100*n_bb/len(df):.1f}%)', flush=True)
    print(f'[train v2] app-enriched: {n_app:,} ({100*n_app/len(df):.1f}%)', flush=True)
    return df


def encode_categoricals(df, top_k_map, existing_categories=None):
    out = df.copy()
    cats_used = {}
    for col, k in top_k_map.items():
        if col not in out.columns: continue
        if existing_categories and col in existing_categories:
            top_vals = existing_categories[col]
        else:
            top_vals = out[col].value_counts().head(k).index.tolist()
        cats_used[col] = top_vals
        out[col] = out[col].fillna('NULL').astype(str)
        out.loc[~out[col].isin(top_vals), col] = 'OTHER'
        dummies = pd.get_dummies(out[col], prefix=col, dtype=float)
        for v in top_vals + ['OTHER', 'NULL']:
            colname = f'{col}_{v}'
            if colname not in dummies.columns:
                dummies[colname] = 0.0
        out = pd.concat([out.drop(columns=[col]), dummies], axis=1)
    return out, cats_used


def train_one_make(df_make, make_name, args):
    n_total = len(df_make)
    if n_total < args.min_rows:
        return None

    train = df_make[df_make['sold_at'] < pd.Timestamp(TEST_CUTOFF, tz='UTC')]
    test = df_make[df_make['sold_at'] >= pd.Timestamp(TEST_CUTOFF, tz='UTC')]
    if len(train) < args.min_rows * 0.8 or len(test) < 5:
        return None

    train_enc, cats = encode_categoricals(train, CATEGORICAL_FEATURES)
    test_enc, _ = encode_categoricals(test, CATEGORICAL_FEATURES, cats)

    feat_cols = [c for c in NUMERIC_FEATURES if c in train_enc.columns]
    feat_cols += [c for c in train_enc.columns
                  if any(c.startswith(f'{cat}_') for cat in CATEGORICAL_FEATURES)]
    for c in feat_cols:
        if c not in test_enc.columns:
            test_enc[c] = 0.0

    # v2.1 (2026-05-08): switched to NaN-aware. The v1 trainer used
    # fillna(0.0) which makes XGBoost treat "missing BB" as "BB=$0" — a
    # nonsense value that the trees learn to ignore. XGBoost natively
    # routes NaN to its own split direction, which is the correct
    # treatment for missing-by-design columns (BB pre-2024).
    # Note: the original v1 numeric-only features still backfill to 0
    # via the categorical-encoding pipeline if absent there. Only the
    # genuinely-nullable v2 columns benefit from NaN propagation.
    X_train = train_enc[feat_cols].astype(float).values
    y_train = train_enc['purchase_cost'].values
    X_test  = test_enc[feat_cols].astype(float).values
    y_test  = test_enc['purchase_cost'].values

    model = xgb.XGBRegressor(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_alpha=0.1,
        reg_lambda=1.0,
        early_stopping_rounds=20,
        eval_metric='mae',
        tree_method='hist',
        n_jobs=4,
        verbosity=0,
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

    pred_test = model.predict(X_test)
    mae = float(np.mean(np.abs(pred_test - y_test)))
    mape = float(np.mean(np.abs((pred_test - y_test) / y_test)) * 100)
    r2 = 1 - float(np.sum((pred_test - y_test) ** 2) /
                   max(1.0, float(np.sum((y_test - y_test.mean()) ** 2))))
    within_5 = float(np.mean(np.abs((pred_test - y_test) / y_test) <= 0.05) * 100)
    within_10 = float(np.mean(np.abs((pred_test - y_test) / y_test) <= 0.10) * 100)

    ratio = float((train['purchase_cost'] / train['est_wholesale_price']).mean())
    base_pred = test['est_wholesale_price'].values * ratio
    base_mae = float(np.mean(np.abs(base_pred - y_test)))
    base_mape = float(np.mean(np.abs((base_pred - y_test) / y_test)) * 100)

    importances = sorted(zip(feat_cols, model.feature_importances_),
                         key=lambda kv: kv[1], reverse=True)[:12]

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(make_name)
    model_path = MODELS_DIR / f'{slug}.json'
    meta_path = MODELS_DIR / f'{slug}.meta.json'
    model.save_model(str(model_path))
    meta = {
        'make_name': make_name,
        'trained_at': datetime.utcnow().isoformat() + 'Z',
        'trainer_version': 'v2.1-nan',
        'n_total': int(n_total),
        'n_train': int(len(train)),
        'n_test': int(len(test)),
        'test_cutoff': TEST_CUTOFF,
        'feature_columns': feat_cols,
        'categories': cats,
        'metrics': {
            'mae_dollars': round(mae, 0),
            'mape_pct': round(mape, 2),
            'r2': round(r2, 4),
            'within_5pct': round(within_5, 1),
            'within_10pct': round(within_10, 1),
        },
        'baseline_metrics': {
            'mae_dollars': round(base_mae, 0),
            'mape_pct': round(base_mape, 2),
            'ratio': round(ratio, 4),
        },
        'top_features': [{'name': n, 'importance': float(imp)} for n, imp in importances],
    }
    with open(meta_path, 'w') as fp:
        json.dump(meta, fp, indent=2, default=str)

    return meta


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--min-rows', type=int, default=100)
    p.add_argument('--makes', type=str, default=None)
    args = p.parse_args()

    df = load_data()
    print(f'[train v2] makes available: {df["make_name"].nunique()}')
    print(f'[train v2] sold_at: {df["sold_at"].min()} → {df["sold_at"].max()}')
    print()

    make_counts = df['make_name'].value_counts()
    eligible = make_counts[make_counts >= args.min_rows].index.tolist()
    if args.makes:
        wanted = {m.strip().upper() for m in args.makes.split(',')}
        eligible = [m for m in eligible if m.upper() in wanted]

    print(f'[train v2] training {len(eligible)} makes (>={args.min_rows} rows)')
    print()
    print(f'{"make":20} {"n":>5} {"MAE":>8} {"MAPE":>7} {"R²":>6} '
          f'{"<5%":>5} {"<10%":>5} | {"base_MAE":>8} {"base_MAPE":>9}')
    print('-' * 100)

    summary = []
    t_start = time.monotonic()
    for make in eligible:
        df_m = df[df['make_name'] == make]
        try:
            meta = train_one_make(df_m, make, args)
            if not meta:
                continue
            m = meta['metrics']
            b = meta['baseline_metrics']
            print(f'{make:20} {meta["n_total"]:>5,} '
                  f'${m["mae_dollars"]:>7,.0f} {m["mape_pct"]:>6.1f}% '
                  f'{m["r2"]:>6.3f} {m["within_5pct"]:>4.0f}% {m["within_10pct"]:>4.0f}% '
                  f'| ${b["mae_dollars"]:>7,.0f} {b["mape_pct"]:>8.1f}%')
            summary.append(meta)
        except Exception as e:
            print(f'{make:20} TRAIN FAILED: {e}')

    elapsed = time.monotonic() - t_start
    print()
    print(f'[train v2] DONE in {elapsed:.1f}s — {len(summary)} models saved to {MODELS_DIR}')

    if summary:
        avg_mae = sum(m['metrics']['mae_dollars'] for m in summary) / len(summary)
        avg_mape = sum(m['metrics']['mape_pct'] for m in summary) / len(summary)
        avg_base_mape = sum(m['baseline_metrics']['mape_pct'] for m in summary) / len(summary)
        beats = sum(1 for m in summary
                    if m['metrics']['mape_pct'] < m['baseline_metrics']['mape_pct'])
        print(f'[train v2] avg model MAPE: {avg_mape:.2f}%  (baseline: {avg_base_mape:.2f}%)')
        print(f'[train v2] avg model MAE:  ${avg_mae:,.0f}')
        print(f'[train v2] beats baseline: {beats}/{len(summary)}')


if __name__ == '__main__':
    main()
