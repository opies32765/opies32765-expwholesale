"""ml_predict.py — load per-make XGBoost models and predict purchase_cost
for a new bid.

Usage:
    from ml_predict import predict_for_bid
    result = predict_for_bid(vehicle_dict)
    # result: {'prediction': 27500, 'mae_dollars': 1684, 'mape_pct': 8.1,
    #          'within_10pct': 81, 'n_train': 661, 'source': 'xgboost',
    #          'baseline': 28100}

Falls back to baseline (per-make ratio of est_wholesale_price → purchase_cost)
when no model exists for the make OR when the trained model didn't beat the
baseline on its test set.
"""
from __future__ import annotations
import json
import re
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb


MODELS_DIR = Path('/opt/expwholesale/ml/models/per_make')
NUMERIC_FEATURES = [
    'year', 'odometer', 'original_msrp', 'est_wholesale_price',
    'market_asking_price', 'base_appraised_value',
    'mileage_adjustment_value', 'days_on_lot', 'days_since_purchase',
    'sold_year', 'sold_month',
]
CATEGORICAL_FEATURES = ['model_name', 'body_type', 'supplier_name',
                        'sale_type', 'vehicle_sale_type']

# Per-process model cache. Loads on first use, kept warm in memory.
_cache: dict = {}
_cache_lock = threading.Lock()


def _slugify(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')


def _load_make(make_name: str) -> dict | None:
    """Return {model, meta} for a make, or None if not available."""
    key = make_name.upper().strip()
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    slug = _slugify(make_name)
    model_path = MODELS_DIR / f'{slug}.json'
    meta_path = MODELS_DIR / f'{slug}.meta.json'
    if not model_path.exists() or not meta_path.exists():
        with _cache_lock:
            _cache[key] = None
        return None
    model = xgb.XGBRegressor()
    model.load_model(str(model_path))
    with open(meta_path) as fp:
        meta = json.load(fp)
    out = {'model': model, 'meta': meta}
    with _cache_lock:
        _cache[key] = out
    return out


def _build_feature_row(bid: dict, meta: dict) -> np.ndarray | None:
    """Mirror the training transform on a single bid dict."""
    row = {}
    # Numerics — pass through, default 0
    for col in NUMERIC_FEATURES:
        v = bid.get(col)
        try:
            row[col] = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            row[col] = 0.0
    # Sold-year / month from sold_at if not set
    if not row.get('sold_year') and bid.get('sold_at'):
        try:
            ts = pd.to_datetime(bid['sold_at'], utc=True, errors='coerce')
            if pd.notna(ts):
                row['sold_year'] = ts.year
                row['sold_month'] = ts.month
        except Exception:
            pass
    # If still no sold_year, use today (we're predicting "what we'd pay now")
    if not row.get('sold_year'):
        from datetime import datetime
        row['sold_year'] = datetime.utcnow().year
        row['sold_month'] = datetime.utcnow().month

    # Categoricals — replay top-K + OTHER
    cats = meta.get('categories', {})
    for col in CATEGORICAL_FEATURES:
        val = (bid.get(col) or 'NULL')
        if val == '': val = 'NULL'
        val = str(val)
        top_vals = cats.get(col, [])
        if val not in top_vals:
            val = 'OTHER'
        # One-hot expand: every column the model expects
        for v in top_vals + ['OTHER', 'NULL']:
            row[f'{col}_{v}'] = 1.0 if v == val else 0.0

    feat_cols = meta['feature_columns']
    arr = np.array([[row.get(c, 0.0) for c in feat_cols]], dtype=float)
    return arr


def _baseline_predict(bid: dict, meta: dict) -> int | None:
    ratio = (meta.get('baseline_metrics') or {}).get('ratio')
    ewp = bid.get('est_wholesale_price') or 0
    if not ratio or not ewp:
        return None
    return int(round(float(ewp) * float(ratio)))


def predict_for_bid(bid: dict) -> dict | None:
    """Predict purchase_cost for a bid dict.

    Required bid keys (anything missing → returns None):
      make_name, year, odometer, est_wholesale_price

    Optional bid keys (improve accuracy):
      model_name, body_type, supplier_name, original_msrp,
      market_asking_price, base_appraised_value, mileage_adjustment_value,
      days_on_lot, sold_at

    Returns dict on success, None if the make has no model AND no baseline:
      {
        'prediction': int (predicted purchase_cost in $),
        'source': 'xgboost' | 'baseline',
        'mae_dollars': int (model's holdout MAE — uncertainty proxy),
        'mape_pct': float (model's holdout MAPE),
        'within_10pct': float (% of holdout predictions within ±10%),
        'n_train': int (training sample size),
        'make_name': str (normalized),
        'baseline_prediction': int (baseline value for comparison),
      }
    """
    make = (bid.get('make_name') or '').upper().strip()
    if not make:
        return None
    if not bid.get('est_wholesale_price'):
        return None

    loaded = _load_make(make)
    if not loaded:
        return None
    meta = loaded['meta']
    model = loaded['model']

    # Compute model + baseline predictions
    base_pred = _baseline_predict(bid, meta)
    try:
        X = _build_feature_row(bid, meta)
        xgb_pred = int(round(float(model.predict(X)[0])))
    except Exception as e:
        return {
            'prediction': base_pred,
            'source': 'baseline',
            'mae_dollars': (meta.get('baseline_metrics') or {}).get('mae_dollars'),
            'mape_pct': (meta.get('baseline_metrics') or {}).get('mape_pct'),
            'n_train': meta.get('n_train'),
            'make_name': make,
            'baseline_prediction': base_pred,
            'error': f'xgb_predict: {e}',
        }

    # Pick the better source: only use XGBoost if it beat baseline on holdout
    m = meta['metrics']
    b = meta.get('baseline_metrics') or {}
    if b.get('mape_pct') and m.get('mape_pct') and m['mape_pct'] >= b['mape_pct']:
        # Baseline is at-least-as-good; prefer it (simpler, more robust)
        return {
            'prediction': base_pred or xgb_pred,
            'source': 'baseline',
            'mae_dollars': b.get('mae_dollars'),
            'mape_pct': b.get('mape_pct'),
            'n_train': meta['n_train'],
            'make_name': make,
            'baseline_prediction': base_pred,
            'xgboost_prediction': xgb_pred,
        }
    return {
        'prediction': xgb_pred,
        'source': 'xgboost',
        'mae_dollars': m.get('mae_dollars'),
        'mape_pct': m.get('mape_pct'),
        'within_10pct': m.get('within_10pct'),
        'n_train': meta['n_train'],
        'make_name': make,
        'baseline_prediction': base_pred,
    }
