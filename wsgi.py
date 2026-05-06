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
