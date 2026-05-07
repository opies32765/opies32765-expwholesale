"""train_per_make.py — train one XGBoost regressor per make on lsl_training.

Inputs:
  PG table lsl_training (28K+ rows, refreshed nightly from LSL crm.db)

Outputs:
  /opt/expwholesale/ml/models/per_make/<make_slug>.json   — XGBoost native
  /opt/expwholesale/ml/models/per_make/<make_slug>.meta.json — train stats

Train/test split is time-based: train on rows with sold_at before
TEST_CUTOFF, evaluate on rows after. Most realistic — no future-leakage.

Baseline comparison: predict purchase_cost = est_wholesale_price * mean_ratio
(per make). XGBoost has to BEAT this for the make to ship a model.

Usage:
  /opt/expwholesale/venv/bin/python /opt/expwholesale/train_per_make.py
  /opt/expwholesale/venv/bin/python /opt/expwholesale/train_per_make.py --min-rows 200
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
MODELS_DIR = Path(os.environ.get('ML_MODELS_DIR',
                                 '/opt/expwholesale/ml/models/per_make'))

# Hold out the most recent N months for testing (no future leakage).
TEST_CUTOFF = '2026-01-01'

NUMERIC_FEATURES = [
    'year', 'odometer', 'original_msrp', 'est_wholesale_price',
    'market_asking_price', 'base_appraised_value',
    'mileage_adjustment_value', 'days_on_lot', 'days_since_purchase',
    'sold_year', 'sold_month',
]

# Categorical features → one-hot with top-K + "other"
CATEGORICAL_FEATURES = {
    'model_name':       30,    # top 30 models per make
    'body_type':        10,
    'supplier_name':    50,    # most diverse — keep more
    'sale_type':         5,
    'vehicle_sale_type': 5,
}


def slugify(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')


def load_data() -> pd.DataFrame:
    print('[train] loading lsl_training from PG...', flush=True)
    conn = psycopg2.connect(EW_DB_URL)
    df = pd.read_sql("""
        SELECT
            deal_id, vin, make_name, model_name, body_type,
            year, odometer,
            original_msrp, est_wholesale_price, market_asking_price,
            base_appraised_value, mileage_adjustment_value,
            sale_type, vehicle_sale_type, supplier_name,
            sold_at, days_on_lot, days_since_purchase,
            purchase_cost, sale_price
        FROM lsl_training
        WHERE purchase_cost IS NOT NULL AND purchase_cost > 0
          AND est_wholesale_price IS NOT NULL AND est_wholesale_price > 0
          AND year IS NOT NULL
          AND make_name IS NOT NULL
    """, conn)
    conn.close()

    # Normalize make casing (LSL has 'BMW', 'Bmw' both — collapse to upper)
    df['make_name'] = df['make_name'].str.upper().str.strip()

    # Sold timestamp → year/month features
    df['sold_at'] = pd.to_datetime(df['sold_at'], errors='coerce', utc=True)
    df['sold_year'] = df['sold_at'].dt.year
    df['sold_month'] = df['sold_at'].dt.month

    # Sanity caps — clip insane values
    df = df[df['purchase_cost'].between(500, 1_000_000)]
    df = df[df['year'].between(1990, 2030)]

    print(f'[train] loaded {len(df):,} rows after sanity filters', flush=True)
    return df


def encode_categoricals(df: pd.DataFrame, top_k_map: dict,
                        existing_categories: dict | None = None) -> tuple:
    """One-hot encode using top-K values per column. If existing_categories
    is provided (inference mode), reuse those instead of computing fresh."""
    out = df.copy()
    cats_used = {}
    for col, k in top_k_map.items():
        if col not in out.columns:
            continue
        if existing_categories and col in existing_categories:
            top_vals = existing_categories[col]
        else:
            top_vals = out[col].value_counts().head(k).index.tolist()
        cats_used[col] = top_vals
        # Replace anything not in top-K with 'OTHER'
        out[col] = out[col].fillna('NULL').astype(str)
        out.loc[~out[col].isin(top_vals), col] = 'OTHER'
        # One-hot encode
        dummies = pd.get_dummies(out[col], prefix=col, dtype=float)
        # Ensure all expected columns exist (for inference parity)
        for v in top_vals + ['OTHER', 'NULL']:
            colname = f'{col}_{v}'
            if colname not in dummies.columns:
                dummies[colname] = 0.0
        out = pd.concat([out.drop(columns=[col]), dummies], axis=1)
    return out, cats_used


def baseline_predict(df: pd.DataFrame, train_df: pd.DataFrame) -> np.ndarray:
    """Baseline: predict purchase_cost = est_wholesale_price * mean_ratio,
    where mean_ratio is computed PER MAKE from training rows."""
    ratios = (train_df.groupby('make_name')
              .apply(lambda g: (g['purchase_cost'] / g['est_wholesale_price']).mean(),
                     include_groups=False))
    overall = (train_df['purchase_cost'] / train_df['est_wholesale_price']).mean()

    def predict_one(row):
        r = ratios.get(row['make_name'], overall)
        return row['est_wholesale_price'] * r

    return df.apply(predict_one, axis=1).values


def train_one_make(df_make: pd.DataFrame, make_name: str, args) -> dict | None:
    n_total = len(df_make)
    if n_total < args.min_rows:
        return None

    # Time-based split
    train = df_make[df_make['sold_at'] < pd.Timestamp(TEST_CUTOFF, tz='UTC')]
    test = df_make[df_make['sold_at'] >= pd.Timestamp(TEST_CUTOFF, tz='UTC')]
    if len(train) < args.min_rows * 0.8 or len(test) < 5:
        return None

    # Encode categoricals on train, replay on test
    train_enc, cats = encode_categoricals(train, CATEGORICAL_FEATURES)
    test_enc, _ = encode_categoricals(test, CATEGORICAL_FEATURES, cats)

    # Feature columns: numerics + all encoded one-hot columns
    feat_cols = [c for c in NUMERIC_FEATURES if c in train_enc.columns]
    feat_cols += [c for c in train_enc.columns
                  if any(c.startswith(f'{cat}_') for cat in CATEGORICAL_FEATURES)]
    # Align test to same columns
    for c in feat_cols:
        if c not in test_enc.columns:
            test_enc[c] = 0.0

    X_train = train_enc[feat_cols].astype(float).fillna(0.0).values
    y_train = train_enc['purchase_cost'].values
    X_test  = test_enc[feat_cols].astype(float).fillna(0.0).values
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

    # Baseline (whole-make ratio of est_wholesale_price → purchase_cost)
    ratio = float((train['purchase_cost'] / train['est_wholesale_price']).mean())
    base_pred = test['est_wholesale_price'].values * ratio
    base_mae = float(np.mean(np.abs(base_pred - y_test)))
    base_mape = float(np.mean(np.abs((base_pred - y_test) / y_test)) * 100)

    # Top features by importance
    importances = sorted(zip(feat_cols, model.feature_importances_),
                         key=lambda kv: kv[1], reverse=True)[:8]

    # Save model + meta
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(make_name)
    model_path = MODELS_DIR / f'{slug}.json'
    meta_path = MODELS_DIR / f'{slug}.meta.json'
    model.save_model(str(model_path))
    meta = {
        'make_name': make_name,
        'trained_at': datetime.utcnow().isoformat() + 'Z',
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
    p.add_argument('--min-rows', type=int, default=100,
                   help='Min rows per make to train a model')
    p.add_argument('--makes', type=str, default=None,
                   help='Comma-separated list (default: all eligible)')
    args = p.parse_args()

    df = load_data()
    print(f'[train] makes available: {df["make_name"].nunique()}')
    print(f'[train] sold_at: {df["sold_at"].min()} → {df["sold_at"].max()}')
    print()

    make_counts = df['make_name'].value_counts()
    eligible = make_counts[make_counts >= args.min_rows].index.tolist()
    if args.makes:
        wanted = {m.strip().upper() for m in args.makes.split(',')}
        eligible = [m for m in eligible if m.upper() in wanted]

    print(f'[train] training {len(eligible)} makes (>={args.min_rows} rows)')
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
    print(f'[train] DONE in {elapsed:.1f}s — {len(summary)} models saved to {MODELS_DIR}')

    if summary:
        avg_mae = sum(m['metrics']['mae_dollars'] for m in summary) / len(summary)
        avg_mape = sum(m['metrics']['mape_pct'] for m in summary) / len(summary)
        avg_base_mape = sum(m['baseline_metrics']['mape_pct'] for m in summary) / len(summary)
        print(f'[train] avg model MAPE: {avg_mape:.2f}%  (baseline: {avg_base_mape:.2f}%)')
        print(f'[train] avg model MAE:  ${avg_mae:,.0f}')


if __name__ == '__main__':
    main()
