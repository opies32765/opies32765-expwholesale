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


try:
    from recon_routes import bp as _recon_bp
    if 'recon' not in app.blueprints:
        app.register_blueprint(_recon_bp)
        print('[wsgi] recon blueprint registered (RECON_PHASE1_2026_06_24)', flush=True)
except Exception as _e:
    print(f'[wsgi] recon register failed: {_e}', flush=True)


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


# comp_msrp processor — env-gated daemon for parallel MSRP lookups
# alongside VM 121's oscar-worker-2. Only fires when COMP_MSRP_DAEMON=1
# is set in the environment (systemd dropin comp-msrp-daemon.conf).
try:
    from app import _start_comp_msrp_processor as _start_cmp
    _start_cmp()
except Exception as _cmp_e:
    print(f'[wsgi] comp_msrp daemon start failed: {_cmp_e}', flush=True)


try:
    from dealerprice_network import bp as _dpn_bp
    if "dealerprice_network" not in app.blueprints:
        app.register_blueprint(_dpn_bp)
        print("[wsgi] dealerprice_network blueprint registered (DEALERPRICE_NETWORK_2026_06_30)", flush=True)
except Exception as _e:
    print(f"[wsgi] dealerprice_network register failed: {_e}", flush=True)


# DEALER_INTAKE_2026_08_23 — per-dealer upload link.
# An EW employee makes a link at /admin/intake-links; the dealer opens
# /dealer-intake/<token>, drops in any list (xlsx/csv/pdf/screenshot/phone
# photo/paste), confirms the VINs and miles we read, and the cars land as
# bids via app._bulk_commit_core — the SAME path /admin/bulk_upload uses.
try:
    import dealer_intake as _di
    from app import (get_db as _di_get_db,
                     gemini_call as _di_gemini,
                     send_sms as _di_sms,
                     _bulk_commit_core as _di_commit_core,
                     THALIST_ALERT_PHONE as _di_alert_phone)

    def _di_vision(prompt, img, mime):
        # gemini_call is local-9B-first via local_brain_shim; real Gemini is
        # only the fallback. Same call the operator's bulk upload makes.
        return _di_gemini(prompt, image_bytes=img, mime=mime,
                          model='gemini-2.5-pro', max_tokens=4096,
                          temperature=0)

    def _di_commit(rows, source_name, delay_seconds, client_ip, uploaded_by,
                   contact_phone, contact_name, creation_source):
        # contact_phone/name go into the bid NOTES only. The bid's own phone
        # column stays the synthetic 'bulk:<slug>' key, so a dealer upload
        # never becomes an outbound-SMS surface.
        return _di_commit_core(
            rows, source_name,
            delay_seconds=delay_seconds,
            client_ip=client_ip,
            uploaded_by=uploaded_by,
            creation_source=creation_source,
            notes_label='Dealer Upload',
            contact_extra=' '.join(
                p for p in (contact_name, contact_phone) if p).strip())

    if 'dealer_intake' not in app.blueprints:
        _di.init(app,
                 get_db=_di_get_db,
                 vision_fn=_di_vision,
                 text_fn=_di_gemini,
                 commit_rows=_di_commit,
                 send_sms=_di_sms,
                 alert_phone=_di_alert_phone)
        print('[wsgi] dealer_intake blueprint registered '
              '(DEALER_INTAKE_2026_08_23)', flush=True)
except Exception as _di_e:
    # NEVER take the app down for this — the intake link is additive.
    print(f'[wsgi] dealer_intake register failed: '
          f'{type(_di_e).__name__}: {_di_e}', flush=True)
