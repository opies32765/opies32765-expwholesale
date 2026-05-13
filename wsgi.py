"""WSGI entry point that survives app.py drift.

Multiple agents edit /opt/expwholesale/app.py concurrently. The
wholesaler_review blueprint registration in app.py keeps getting wiped
when other instances scp their own copy of app.py. This wrapper imports
app.py (so all the existing routes load normally) and then idempotently
re-registers the wholesaler_review blueprint at WSGI import time —
which gunicorn invokes once per worker boot.

systemd points gunicorn at `wsgi:app` instead of `app:app` via the
drop-in /etc/systemd/system/expwholesale.service.d/wsr_register.conf.
That drop-in is owned by this work; other instances aren't editing it.

Net effect: even if app.py loses my registration, gunicorn workers
still come up with /wholesaler-<reviewer>/* routes registered.
"""
from app import app

try:
    from wholesaler_review import bp as _wsr_bp
    if 'wholesaler_review' not in app.blueprints:
        app.register_blueprint(_wsr_bp)
        print('[wsgi] wholesaler_review blueprint registered (drift recovery)', flush=True)
except Exception as _e:
    print(f'[wsgi] wholesaler_review register failed: {_e}', flush=True)

try:
    from network_push_bp import bp as _np_bp
    if "network_push" not in app.blueprints:
        app.register_blueprint(_np_bp)
        print("[wsgi] network_push blueprint registered (drift recovery)", flush=True)
except Exception as _e:
    print(f"[wsgi] network_push register failed: {_e}", flush=True)


# Pre-warm ML models so the first bid card render doesn't pay the
# 5s pandas/xgboost import + 700-1900ms per-make cold load. Each worker
# imports this module once on boot via gunicorn.
try:
    import time as _t
    _t0 = _t.monotonic()
    from ml_predict import preload_all as _ml_preload
    _n_models = _ml_preload()
    print(f'[wsgi] ml_predict pre-warmed: {_n_models} models in '
          f'{(_t.monotonic()-_t0)*1000:.0f}ms', flush=True)
except Exception as _ml_e:
    print(f'[wsgi] ml_predict preload failed: {_ml_e}', flush=True)
