"""train_hybrid.py — per-(make, base_model) XGBoost with per-make fallback.

Trains TWO tiers:
  Tier 1: per-(make, base_model) for groups with n_total >= MIN_ROWS_MODEL.
  Tier 2: per-make for everything else (matches train_per_make_v2 outputs).

Inference picks the most-specific available — try (make, base_model) first,
fall back to per-make.

Saves to:
  /opt/expwholesale/ml/models_hybrid/per_make_model/{make-slug}/{model-slug}.json
  /opt/expwholesale/ml/models_hybrid/per_make/{make-slug}.json

Uses the same v2 features (Black Book + market depth) and NaN-aware XGBoost.
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

# Make model_normalizer importable
sys.path.insert(0, '/opt/expwholesale')
from model_normalizer import normalize_model


EW_DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
ROOT_DIR = Path(os.environ.get('ML_HYBRID_DIR',
                               '/opt/expwholesale/ml/models_hybrid_v2'))
PER_MM_DIR  = ROOT_DIR / 'per_make_model'
PER_MAKE_DIR = ROOT_DIR / 'per_make'

TEST_CUTOFF = '2025-05-01'  # 12-month holdout (doubled vs v1's 6-month)

NUMERIC_FEATURES = [
    # v1
    'year', 'odometer', 'original_msrp', 'est_wholesale_price',
    'market_asking_price', 'base_appraised_value',
    'mileage_adjustment_value', 'days_on_lot', 'days_since_purchase',
    'sold_year', 'sold_month',
    # v2 BB
    'bb_present',
    'bb_base_wholesale_avg', 'bb_adj_wholesale_avg', 'bb_adj_wholesale_clean',
    'bb_adj_private_avg', 'bb_adj_private_clean',
    'bb_adj_retail_avg', 'bb_adj_retail_clean',
    'bb_adj_trade_in_avg', 'bb_adj_trade_in_clean',
    'bb_msrp', 'bb_retail_equipped', 'bb_wholesale_adjustment',
    # v2 market depth
    'app_appraised_value', 'app_market_asking_price',
    'app_available_in_market', 'app_original_available_in_market',
    'app_avg_days_on_market', 'app_avg_days_supply',
    'app_n_total', 'app_supply_depletion',
]

CATEGORICAL_FEATURES = {
    'body_type':         10,
    'supplier_name':     50,
    'sale_type':          5,
    'vehicle_sale_type':  5,
    # NOTE: model_name dropped from per-(make, base_model) — within a single
    # base_model, all rows have the same base. But for the per-make fallback
    # tier we still want it.
}

CATEGORICAL_FEATURES_PER_MAKE = dict(CATEGORICAL_FEATURES,
                                     model_name=30,
                                     base_model=30)


def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')


def load_data():
    print('[hybrid] loading lsl_training from PG...', flush=True)
    conn = psycopg2.connect(EW_DB_URL)
    df = pd.read_sql("""
        SELECT
            deal_id, vin, make_name, model_name, body_type,
            year, odometer,
            original_msrp, est_wholesale_price, market_asking_price,
            base_appraised_value, mileage_adjustment_value,
            sale_type, vehicle_sale_type, supplier_name,
            sold_at, days_on_lot, days_since_purchase,
            purchase_cost, sale_price,
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

    # Add base_model via the normalizer
    df['base_model'] = df.apply(
        lambda r: normalize_model(r['make_name'], r['model_name']), axis=1)
    df['base_model'] = df['base_model'].fillna('UNKNOWN')

    print(f'[hybrid] loaded {len(df):,} rows after sanity filters', flush=True)
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


def _train_xgb(train, test, cat_features, label='?'):
    """Common training routine. Returns (model, metrics, baseline_metrics,
    feat_cols, cats_used, top_features)."""
    train_enc, cats = encode_categoricals(train, cat_features)
    test_enc, _ = encode_categoricals(test, cat_features, cats)

    feat_cols = [c for c in NUMERIC_FEATURES if c in train_enc.columns]
    feat_cols += [c for c in train_enc.columns
                  if any(c.startswith(f'{cat}_') for cat in cat_features)]
    for c in feat_cols:
        if c not in test_enc.columns:
            test_enc[c] = 0.0

    X_train = train_enc[feat_cols].astype(float).values
    y_train = train_enc['purchase_cost'].values
    X_test  = test_enc[feat_cols].astype(float).values
    y_test  = test_enc['purchase_cost'].values

    model = xgb.XGBRegressor(
        n_estimators=400, max_depth=5, learning_rate=0.05,
        subsample=0.85, colsample_bytree=0.85,
        reg_alpha=0.1, reg_lambda=1.0,
        early_stopping_rounds=20, eval_metric='mae',
        tree_method='hist', n_jobs=4, verbosity=0,
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

    pred = model.predict(X_test)
    mae = float(np.mean(np.abs(pred - y_test)))
    mape = float(np.mean(np.abs((pred - y_test) / y_test)) * 100)
    r2 = 1 - float(np.sum((pred - y_test) ** 2) /
                   max(1.0, float(np.sum((y_test - y_test.mean()) ** 2))))
    w5 = float(np.mean(np.abs((pred - y_test) / y_test) <= 0.05) * 100)
    w10 = float(np.mean(np.abs((pred - y_test) / y_test) <= 0.10) * 100)

    ratio = float((train['purchase_cost'] / train['est_wholesale_price']).mean())
    base_pred = test['est_wholesale_price'].values * ratio
    base_mae = float(np.mean(np.abs(base_pred - y_test)))
    base_mape = float(np.mean(np.abs((base_pred - y_test) / y_test)) * 100)

    importances = sorted(zip(feat_cols, model.feature_importances_),
                         key=lambda kv: kv[1], reverse=True)[:10]

    metrics = {
        'mae_dollars': round(mae, 0), 'mape_pct': round(mape, 2),
        'r2': round(r2, 4), 'within_5pct': round(w5, 1),
        'within_10pct': round(w10, 1),
    }
    baseline = {
        'mae_dollars': round(base_mae, 0), 'mape_pct': round(base_mape, 2),
        'ratio': round(ratio, 4),
    }
    return model, metrics, baseline, feat_cols, cats, importances


def train_one_group(df, label, args, cat_features=CATEGORICAL_FEATURES,
                    save_path=None, meta_extra=None):
    n_total = len(df)
    if n_total < args.min_rows:
        return None

    train = df[df['sold_at'] < pd.Timestamp(TEST_CUTOFF, tz='UTC')]
    test = df[df['sold_at'] >= pd.Timestamp(TEST_CUTOFF, tz='UTC')]
    if len(train) < args.min_rows * 0.8 or len(test) < 5:
        return None

    try:
        model, metrics, baseline, feat_cols, cats, importances = _train_xgb(
            train, test, cat_features, label=label)
    except Exception as e:
        print(f'  {label} TRAIN FAILED: {e}')
        return None

    save_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(save_path))
    meta = {
        'trained_at': datetime.utcnow().isoformat() + 'Z',
        'trainer_version': 'hybrid-v1',
        'n_total': int(n_total),
        'n_train': int(len(train)),
        'n_test': int(len(test)),
        'test_cutoff': TEST_CUTOFF,
        'feature_columns': feat_cols,
        'categories': cats,
        'metrics': metrics,
        'baseline_metrics': baseline,
        'top_features': [{'name': n, 'importance': float(imp)}
                         for n, imp in importances],
    }
    if meta_extra:
        meta.update(meta_extra)
    save_path.with_suffix('.meta.json').write_text(json.dumps(meta, indent=2, default=str))
    return meta


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--min-rows', type=int, default=300,
                   help='Min total rows to qualify for per-(make, base_model)')
    p.add_argument('--min-rows-fallback', type=int, default=100,
                   help='Min total rows for the per-make fallback tier')
    p.add_argument('--makes', type=str, default=None)
    args = p.parse_args()

    df = load_data()
    print(f'[hybrid] makes available: {df["make_name"].nunique()}')
    print(f'[hybrid] sold_at: {df["sold_at"].min()} → {df["sold_at"].max()}')

    # ── TIER 1: per-(make, base_model) where n >= MIN_ROWS_MODEL ────────────
    print(f'\n[hybrid] TIER 1 — per-(make, base_model) with n >= {args.min_rows}')
    print(f'{"make":<14} {"base_model":<24} {"n":>5} {"MAPE":>7} {"R²":>6} '
          f'{"<10%":>5} | {"baseMAPE":>8}')
    print('-' * 100)

    mm_counts = df.groupby(['make_name', 'base_model']).size().reset_index(name='n')
    mm_counts = mm_counts[mm_counts['n'] >= args.min_rows]
    mm_counts = mm_counts.sort_values('n', ascending=False)

    if args.makes:
        wanted = {m.strip().upper() for m in args.makes.split(',')}
        mm_counts = mm_counts[mm_counts['make_name'].isin(wanted)]

    tier1_summary = []
    t0 = time.monotonic()
    for _, row in mm_counts.iterrows():
        mk, base = row['make_name'], row['base_model']
        sub = df[(df['make_name'] == mk) & (df['base_model'] == base)]
        save_path = PER_MM_DIR / slugify(mk) / f'{slugify(base)}.json'
        meta = train_one_group(sub, f'{mk}/{base}', args,
                               save_path=save_path,
                               meta_extra={'make_name': mk, 'base_model': base,
                                           'tier': 'per_make_model'})
        if not meta: continue
        m = meta['metrics']; b = meta['baseline_metrics']
        print(f'{mk[:14]:<14} {base[:24]:<24} {meta["n_total"]:>5,} '
              f'{m["mape_pct"]:>6.2f}% {m["r2"]:>6.3f} {m["within_10pct"]:>4.0f}% '
              f'| {b["mape_pct"]:>7.2f}%')
        tier1_summary.append(meta)

    # ── TIER 2: per-make (covers everything that didn't qualify) ────────────
    print(f'\n[hybrid] TIER 2 — per-make fallback, n >= {args.min_rows_fallback}')
    print(f'{"make":<20} {"n":>5} {"MAPE":>7} {"R²":>6} {"<10%":>5} | {"baseMAPE":>8}')
    print('-' * 80)
    args.min_rows = args.min_rows_fallback  # reuse for fallback tier
    make_counts = df['make_name'].value_counts()
    eligible_makes = make_counts[make_counts >= args.min_rows_fallback].index.tolist()

    tier2_summary = []
    for mk in eligible_makes:
        sub = df[df['make_name'] == mk]
        save_path = PER_MAKE_DIR / f'{slugify(mk)}.json'
        meta = train_one_group(sub, mk, args,
                               cat_features=CATEGORICAL_FEATURES_PER_MAKE,
                               save_path=save_path,
                               meta_extra={'make_name': mk, 'tier': 'per_make'})
        if not meta: continue
        m = meta['metrics']; b = meta['baseline_metrics']
        print(f'{mk:<20} {meta["n_total"]:>5,} '
              f'{m["mape_pct"]:>6.2f}% {m["r2"]:>6.3f} {m["within_10pct"]:>4.0f}% '
              f'| {b["mape_pct"]:>7.2f}%')
        tier2_summary.append(meta)

    elapsed = time.monotonic() - t0
    print()
    print(f'[hybrid] DONE in {elapsed:.1f}s')
    print(f'  TIER 1: {len(tier1_summary)} per-(make, base_model) models')
    print(f'  TIER 2: {len(tier2_summary)} per-make fallback models')

    if tier1_summary:
        avg_mape = sum(m['metrics']['mape_pct'] for m in tier1_summary) / len(tier1_summary)
        beats = sum(1 for m in tier1_summary
                    if m['metrics']['mape_pct'] < m['baseline_metrics']['mape_pct'])
        print(f'  TIER 1 avg MAPE: {avg_mape:.2f}%  beats baseline: {beats}/{len(tier1_summary)}')
    if tier2_summary:
        avg_mape = sum(m['metrics']['mape_pct'] for m in tier2_summary) / len(tier2_summary)
        beats = sum(1 for m in tier2_summary
                    if m['metrics']['mape_pct'] < m['baseline_metrics']['mape_pct'])
        print(f'  TIER 2 avg MAPE: {avg_mape:.2f}%  beats baseline: {beats}/{len(tier2_summary)}')


if __name__ == '__main__':
    main()
