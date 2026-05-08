"""train_purchase_predictor.py — XGBoost regressor: features → actual purchase_cost.

Trains on ai_accuracy table. Uses ONLY features that pass strict sanity
validation (no garbage from old xlsx-format rbook rows). Audits feature
coverage first; drops sparse features.

Saves model to /opt/expwholesale/ml_models/purchase_predictor.json.

Usage:
    train_purchase_predictor.py [--eval]    # train, optional 5-fold CV
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
import xgboost as xgb
from sklearn.model_selection import KFold

EW_DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
LSL_DB_PATH = '/opt/livesaleslog/crm.db'
MODEL_DIR  = '/opt/expwholesale/ml_models'
MODEL_PATH = os.path.join(MODEL_DIR, 'purchase_predictor.json')
META_PATH  = os.path.join(MODEL_DIR, 'purchase_predictor_meta.json')


# Sanity caps — anything outside these bounds is treated as missing.
SANITY = {
    'price':    (1_000, 2_000_000),
    'mileage':  (0, 500_000),
    'msrp':     (5_000, 1_000_000),
    'days':     (0, 1000),
    'spread':   (-200_000, 200_000),
    'year':     (2000, 2030),
}

def _ok(val, kind):
    if val is None: return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    lo, hi = SANITY[kind]
    if not (lo <= v <= hi): return None
    return v


# ── Step 1: Pull raw feature data with sanity-checked SQL ─────────────────

def fetch_training_set() -> pd.DataFrame:
    con = psycopg2.connect(EW_DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = con.cursor()
    cur.execute("""
        SELECT
            a.bid_id, a.vin,
            a.year, a.make, a.model, a.mileage,
            a.actual_purchase_cost,
            a.ai_recommendation,
            (v.manheim_transactions->'summary'->>'adjusted_mmr')::int  AS mmr_adjusted,
            (v.manheim_transactions->'summary'->>'base_mmr')::int      AS mmr_base,
            jsonb_array_length(COALESCE(v.manheim_transactions->'transactions', '[]'::jsonb)) AS mmr_n_tx,
            v.manheim_transactions->'transactions' AS mmr_txns_raw,
            v.rbook_competitive_set->>'source'                         AS rbook_source,
            v.rbook_competitive_set->'rows'                            AS rbook_rows_raw,
            ip.total_msrp  AS subject_msrp,
            ip.base_price  AS subject_base_price
        FROM ai_accuracy a
        LEFT JOIN vauto_lookups v    ON v.bid_id = a.bid_id
        LEFT JOIN ipacket_lookups ip ON ip.bid_id = a.bid_id
        WHERE a.actual_purchase_cost > 0
    """)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()

    # LSL aggregation per row (same logic as lsl_buyer_match)
    lsl_con = sqlite3.connect(LSL_DB_PATH)
    lsl_cur = lsl_con.cursor()
    for r in rows:
        if r.get('year') and r.get('make') and r.get('model'):
            try:
                lsl_cur.execute("""
                    SELECT COUNT(*),
                           AVG(sale_price),
                           AVG(sale_price - purchase_cost),
                           AVG(purchase_cost)
                    FROM deals
                    WHERE LOWER(make_name) = LOWER(?)
                      AND LOWER(vehicle_info) LIKE LOWER(?)
                      AND substr(vehicle_info, 1, 4) = ?
                      AND sale_price BETWEEN 1000 AND 2000000
                      AND purchase_cost BETWEEN 1000 AND 2000000
                """, (r['make'], f'%{r["model"]}%', str(r['year'])))
                lsl_row = lsl_cur.fetchone()
                if lsl_row:
                    n, avg_sale, avg_gross, avg_purch = lsl_row
                    r['lsl_n_deals']      = n or 0
                    r['lsl_avg_sale']     = int(avg_sale)  if avg_sale  else None
                    r['lsl_avg_gross']    = int(avg_gross) if avg_gross else None
                    r['lsl_avg_purchase'] = int(avg_purch) if avg_purch else None
            except Exception:
                pass
    lsl_con.close()

    # Compute MMR median + rbook stats from raw JSONB lists, sanity-filtered
    for r in rows:
        # MMR median from raw transaction list (more accurate than summary)
        txns = r.get('mmr_txns_raw') or []
        prices = []
        if isinstance(txns, list):
            for t in txns:
                if isinstance(t, dict):
                    p = _ok(t.get('sale_price'), 'price')
                    if p: prices.append(p)
        prices.sort()
        r['mmr_median'] = prices[len(prices)//2] if prices else None

        # rBook stats — apply STRICT sanity (old xlsx had stock-numbers in price)
        rb = r.get('rbook_rows_raw') or []
        clean_rows = []
        if isinstance(rb, list):
            for v in rb:
                if not isinstance(v, dict): continue
                price = _ok(v.get('price'), 'price')
                miles = _ok(v.get('mileage'), 'mileage')
                if price and miles:  # both required
                    clean_rows.append({'price': price, 'mileage': miles,
                                       'days_on_lot': v.get('days_on_lot')})
        if clean_rows:
            asks = sorted(c['price'] for c in clean_rows)
            r['rbook_median'] = asks[len(asks)//2]
            r['rbook_n_clean'] = len(clean_rows)
            dol = [_ok(c.get('days_on_lot'), 'days') for c in clean_rows]
            dol = [d for d in dol if d is not None]
            r['rbook_avg_dol'] = sum(dol)/len(dol) if dol else None
            mi = _ok(r.get('mileage'), 'mileage')
            if mi:
                clean_rows.sort(key=lambda c: abs(c['mileage'] - mi))
                top3 = clean_rows[:3]
                r['closest_comp_mileage_delta_avg'] = (
                    sum(abs(c['mileage'] - mi) for c in top3) / len(top3))
                r['closest_comp_price_avg'] = sum(c['price'] for c in top3) / len(top3)

        # Sanity-cap simple fields too
        r['year']         = _ok(r.get('year'), 'year')
        r['mileage']      = _ok(r.get('mileage'), 'mileage')
        r['mmr_adjusted'] = _ok(r.get('mmr_adjusted'), 'price')
        r['mmr_base']     = _ok(r.get('mmr_base'), 'price')
        r['subject_msrp'] = _ok(r.get('subject_msrp'), 'msrp')
        r['subject_base_price'] = _ok(r.get('subject_base_price'), 'msrp')
        r['retail_mmr_spread'] = (
            r['rbook_median'] - r['mmr_median']
            if r.get('rbook_median') and r.get('mmr_median') else None)

    df = pd.DataFrame(rows).drop(columns=['mmr_txns_raw', 'rbook_rows_raw'],
                                  errors='ignore')
    return df


# ── Step 2: Audit feature coverage ────────────────────────────────────────

CANDIDATE_FEATURES = [
    'year', 'mileage',
    'mmr_median', 'mmr_adjusted', 'mmr_base', 'mmr_n_tx',
    'rbook_median', 'rbook_n_clean', 'rbook_avg_dol',
    'closest_comp_mileage_delta_avg', 'closest_comp_price_avg',
    'lsl_avg_sale', 'lsl_avg_gross', 'lsl_avg_purchase', 'lsl_n_deals',
    'subject_msrp', 'subject_base_price',
    'retail_mmr_spread',
]

def audit(df: pd.DataFrame) -> list[str]:
    print(f'\n=== FEATURE COVERAGE AUDIT (n={len(df)}) ===')
    keep = []
    for col in CANDIDATE_FEATURES:
        if col not in df.columns:
            print(f'  {col:38s} MISSING from query')
            continue
        non_null = df[col].notna().sum()
        pct = 100.0 * non_null / len(df)
        flag = 'KEEP' if pct >= 30 else 'DROP'
        print(f'  {col:38s} {non_null:>4d}/{len(df)} non-null ({pct:5.1f}%) → {flag}')
        if pct >= 30:
            keep.append(col)
    print(f'\nKeeping {len(keep)} features for training.\n')
    return keep


# ── Step 3: Build feature matrix + train ──────────────────────────────────

TOP_MAKES = ['AUDI','BMW','MERCEDES-BENZ','PORSCHE','LAND ROVER','LEXUS',
             'CADILLAC','GENESIS','TESLA','VOLVO','ACURA','INFINITI',
             'MASERATI','JAGUAR','FERRARI','BENTLEY','LAMBORGHINI',
             'TOYOTA','HONDA','FORD','CHEVROLET','GMC','NISSAN']

def build_matrix(df: pd.DataFrame, kept_features: list[str]):
    feats = []
    feat_names = []
    for c in kept_features:
        feats.append(pd.to_numeric(df[c], errors='coerce').fillna(-1).values)
        feat_names.append(c)
    # One-hot makes (uppercase normalize)
    make_upper = df['make'].fillna('').str.upper()
    for m in TOP_MAKES:
        feats.append((make_upper == m).astype(int).values)
        feat_names.append(f'make_{m.replace(" ","_").replace("-","_")}')
    feats.append((~make_upper.isin(TOP_MAKES)).astype(int).values)
    feat_names.append('make_other')
    X = np.column_stack(feats).astype(np.float32)
    return X, feat_names


def train(df: pd.DataFrame, do_cv: bool):
    kept = audit(df)
    if len(kept) < 3:
        print('TOO FEW USABLE FEATURES — aborting.')
        sys.exit(1)
    X, feat_names = build_matrix(df, kept)
    y = df['actual_purchase_cost'].astype(float).values
    print(f'training matrix: {X.shape}, features={len(feat_names)}, labels={len(y)}')

    cv_mae = cv_mape = None
    if do_cv and len(y) >= 25:
        kf = KFold(n_splits=5, shuffle=True, random_state=42)
        fold_maes, fold_mapes = [], []
        for fold, (tr_idx, te_idx) in enumerate(kf.split(X), 1):
            mod = xgb.XGBRegressor(
                n_estimators=300, max_depth=4, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8,
                random_state=42, n_jobs=-1, verbosity=0)
            mod.fit(X[tr_idx], y[tr_idx])
            pred = mod.predict(X[te_idx])
            mae  = float(np.mean(np.abs(pred - y[te_idx])))
            mape = float(100.0 * np.mean(np.abs(pred - y[te_idx]) / np.maximum(y[te_idx], 1)))
            fold_maes.append(mae); fold_mapes.append(mape)
            print(f'  fold {fold}: MAE=${mae:,.0f}  MAPE={mape:.2f}%  (n_test={len(te_idx)})')
        cv_mae  = float(np.mean(fold_maes))
        cv_mape = float(np.mean(fold_mapes))
        print(f'\n  5-fold avg: MAE=${cv_mae:,.0f}  MAPE={cv_mape:.2f}%')

    print('\nfitting final model on full dataset...')
    model = xgb.XGBRegressor(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1, verbosity=0)
    model.fit(X, y)

    os.makedirs(MODEL_DIR, exist_ok=True)
    model.save_model(MODEL_PATH)
    meta = {
        'features':         feat_names,
        'numeric_features': kept,
        'top_makes':        TOP_MAKES,
        'sanity':           {k: list(v) for k, v in SANITY.items()},
        'n_train':          int(len(y)),
        'cv_mae':           cv_mae,
        'cv_mape_pct':      cv_mape,
        'trained_at':       pd.Timestamp.now(tz='UTC').isoformat(),
    }
    with open(META_PATH, 'w') as f:
        json.dump(meta, f, indent=2)
    print(f'saved: {MODEL_PATH}')
    print(f'meta:  {META_PATH}')

    importance = sorted(zip(feat_names, model.feature_importances_),
                        key=lambda x: -x[1])
    print('\nTop 12 features by importance:')
    for name, imp in importance[:12]:
        print(f'  {name:35s} {imp:.4f}')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--eval', action='store_true', help='Run 5-fold CV')
    args = p.parse_args()

    df = fetch_training_set()
    print(f'fetched {len(df)} matched bid↔purchase pairs')
    if len(df) < 30:
        print('TOO FEW SAMPLES — aborting.'); sys.exit(1)
    train(df, do_cv=args.eval)
