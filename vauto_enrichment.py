"""vauto_enrichment.py — production glue for Contabo 1's enrichment_api.

Calls vauto_api directly when wave-1 finishes for a bid. Skips the legacy
EWEnrichRbook claim path entirely; falls back to it only on hard failure.

Intended location: /opt/expwholesale/vauto_enrichment.py (alongside
                   enrichment_api.py)

Wiring into enrichment_api.py — minimal change:

    from vauto_enrichment import enrich_bid_direct, log_direct_enrichment_result

    # In your wave-1-completion handler (where appraisal_url gets written
    # to vauto_lookups), spawn a thread:
    import threading
    def _direct_enrich(bid_id, vehicle_dict):
        result = enrich_bid_direct(bid_id, vehicle_dict)
        log_direct_enrichment_result(bid_id, result)
        if result['ok']:
            # Write competitive_set + book values to vauto_lookups
            db.execute(...)
            # Mark rbook job as done so EWEnrichRbook skips claim
            db.execute("UPDATE vauto_lookups SET rbook_completed_at=NOW(), "
                       "enrichment_state = enrichment_state || %s "
                       "WHERE bid_id=%s",
                       (json.dumps({'rbook': {'status': 'done',
                                              'duration_ms': result['ms'],
                                              'source': 'direct_api'}}),
                        bid_id))
        # else: do nothing — EWEnrichRbook on VM 120 will claim normally as fallback.

    threading.Thread(target=_direct_enrich, args=(bid_id, vehicle_dict),
                     daemon=True).start()
"""
from __future__ import annotations
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from typing import Any

from cookie_jar import CookieJar
from vauto_bff_direct import (
    fetch_competitive_set, fetch_price_guides,
    parse_competitive_set, parse_price_guides,
    VAutoAuthError, VAutoServerError, VAutoBadRequestError,
)


SESSION_PATH = os.environ.get('VAUTO_SESSION_PATH',
                              '/opt/expwholesale/state/vauto_session.json')
SESSION_MAX_AGE_SEC = int(os.environ.get('VAUTO_SESSION_MAX_AGE_SEC',
                                         12 * 3600))  # 12h
PARALLEL_TIMEOUT_SEC = 30

log = logging.getLogger('vauto_enrichment')


# ── Session loading ────────────────────────────────────────────────────

_jar_cache: dict = {'jar': None, 'mtime': 0}


def _get_jar() -> CookieJar:
    """Load cookie jar lazily, reload if file changed on disk (verifier
    refreshes it every cycle)."""
    try:
        mtime = os.path.getmtime(SESSION_PATH)
    except FileNotFoundError:
        raise RuntimeError(f'No vAuto session file at {SESSION_PATH}. '
                           f'Run prewarmer.py + verifier_cookie_export to '
                           f'produce it.')
    if _jar_cache['jar'] is None or mtime > _jar_cache['mtime']:
        jar = CookieJar(SESSION_PATH)
        jar.load()
        _jar_cache['jar'] = jar
        _jar_cache['mtime'] = mtime
        log.info('vauto_session loaded (age %ds)', int(jar.age_seconds()))
    return _jar_cache['jar']


def _session_too_stale(jar: CookieJar) -> bool:
    return jar.age_seconds() > SESSION_MAX_AGE_SEC


# ── Public entry point ─────────────────────────────────────────────────

def enrich_bid_direct(bid_id: int, vehicle: dict) -> dict:
    """Fetch rbook + priceGuides for a bid via direct BFF API calls.

    Returns:
        {
            'ok': bool,
            'ms': int,                    # wall time
            'competitive_set': dict | None,  # parse_competitive_set output
            'price_guides':    dict | None,  # parse_price_guides output
            'error': str | None,          # populated when ok=False
            'fallback_legacy': bool,      # True = caller should fall back
        }

    Does NOT raise. Errors are returned in the dict so the caller can
    decide whether to fall back to the legacy claim path.
    """
    t0 = time.monotonic()
    result: dict = {
        'ok': False, 'ms': 0,
        'competitive_set': None, 'price_guides': None,
        'error': None, 'fallback_legacy': True,
    }

    try:
        jar = _get_jar()
    except Exception as e:
        result['error'] = f'session load: {e}'
        return result

    if _session_too_stale(jar):
        result['error'] = (f'session stale ({jar.age_seconds():.0f}s old, '
                           f'max {SESSION_MAX_AGE_SEC}s)')
        return result

    cookies = jar.get_cookies()
    headers = jar.get_headers()
    appraisal_id = jar.get_session_appraisal_id() or 'unused'

    # Fire both calls in parallel
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            rb_fut = pool.submit(fetch_competitive_set, vehicle, cookies,
                                 headers, appraisal_id=appraisal_id)
            pg_fut = pool.submit(fetch_price_guides, vehicle, cookies,
                                 headers, appraisal_id=appraisal_id)
            rb_resp = rb_fut.result(timeout=PARALLEL_TIMEOUT_SEC)
            pg_resp = pg_fut.result(timeout=PARALLEL_TIMEOUT_SEC)
    except VAutoAuthError as e:
        result['error'] = f'auth: {e}'
        log.warning('vauto auth failed for bid %d — session needs refresh', bid_id)
        return result
    except VAutoServerError as e:
        result['error'] = f'server: {e}'
        return result
    except (VAutoBadRequestError, FutTimeout) as e:
        result['error'] = f'{type(e).__name__}: {e}'
        return result
    except Exception as e:
        result['error'] = f'unexpected: {type(e).__name__}: {e}'
        return result

    result['competitive_set'] = parse_competitive_set(rb_resp)
    result['price_guides'] = parse_price_guides(pg_resp)
    result['ok'] = True
    result['fallback_legacy'] = False
    result['ms'] = int((time.monotonic() - t0) * 1000)
    return result


def log_direct_enrichment_result(bid_id: int, result: dict) -> None:
    """One-line outcome log. Wire to your existing logger."""
    if result['ok']:
        cs = result['competitive_set'] or {}
        pg = result['price_guides'] or {}
        log.info('bid %d direct: %dms, %d comp rows, manheim=%s',
                 bid_id, result['ms'], len(cs.get('rows') or []),
                 'yes' if 'manheim' in pg else 'no')
    else:
        log.warning('bid %d direct FAILED: %s — falling back to legacy',
                    bid_id, result['error'])


def build_vehicle_dict_from_bid(conn, bid_id: int) -> dict | None:
    """Build a vauto_api-compatible vehicle dict from the bids row.

    Returns None if the bid doesn't exist or lacks vin/year/make/model.
    Optional fields (bodyType, driveTrain, engine, etc.) are left out —
    vAuto fills them in from VIN if missing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT vin, year, make, model, mileage, color "
            "FROM bids WHERE id = %s", (bid_id,))
        row = cur.fetchone()
    if not row:
        return None
    # Both RealDictCursor and tuple-cursor compat
    if hasattr(row, 'keys'):
        vin, year, make, model, mileage, color = (
            row['vin'], row['year'], row['make'], row['model'],
            row['mileage'], row.get('color') if hasattr(row, 'get') else row['color'])
    else:
        vin, year, make, model, mileage, color = row[:6]
    if not vin or not year or not make or not model:
        return None
    # Normalize make/model casing — vAuto's BFF treats 'HONDA' != 'Honda'
    # AND 'BMW' != 'Bmw'. Bids table stores uppercase from SMS intake;
    # vAuto wants title case for normal makes, all-caps for acronym makes.
    ACRONYM_MAKES = {
        'BMW', 'GMC', 'KIA', 'AMC', 'AMG', 'BYD', 'MG', 'JAC', 'JCB',
        'FAW', 'INFINITI',  # vAuto stores INFINITI all-caps
    }
    def _norm_make(m):
        if not m: return None
        upper = m.upper()
        return upper if upper in ACRONYM_MAKES else m.title()
    return {
        'vin':                 vin,
        'odometer':            int(mileage) if mileage else 0,
        'odometerUom':         'Miles',
        'year':                int(year),
        'make':                _norm_make(make),
        'model':               model,
        'series':              None,
        'bodyCabStyle':        None,
        'bodyType':            None,
        'driveTrainType':      None,
        'engineCylinderCount': None,
        'engineDisplacement':  None,
        'transmissionType':    None,
        'interiorColor':       None,
        'exteriorBaseColor':   color,
        'engineFuelIntake':    None,
        'transmissionGearCount': None,
        'bodyDoorCount':       None,
    }


def kick_direct_enrichment(bid_id: int, db_conn_factory) -> None:
    """Full direct-enrichment lifecycle for one bid. Designed to run in a
    daemon thread spawned from the wave-1-completion handler.

    db_conn_factory: a callable that returns a fresh DB connection
                     (e.g. `app.get_db`). We use a fresh conn because the
                     request-bound one is closed by the time the thread runs.

    Idempotent: the final UPDATE filters on `rbook_completed_at IS NULL`,
    so if the legacy EWEnrichRbook worker beats us to it, our write is
    a no-op.
    """
    try:
        # Fetch vehicle data
        try:
            with db_conn_factory() as conn:
                vehicle = build_vehicle_dict_from_bid(conn, bid_id)
        except Exception as e:
            log.warning('bid %d vehicle fetch failed: %s', bid_id, e)
            return
        if not vehicle:
            log.info('bid %d direct skipped — no vin/year/make/model yet',
                     bid_id)
            return

        # Direct API
        result = enrich_bid_direct(bid_id, vehicle)
        log_direct_enrichment_result(bid_id, result)
        if not result['ok']:
            return  # legacy EWEnrichRbook will handle it

        # Write results — idempotent on rbook_completed_at IS NULL
        cs = result['competitive_set'] or {}
        pg_raw = (result.get('_raw_pg') or {})  # raw priceGuides response
        pg = result['price_guides'] or {}
        try:
            with db_conn_factory() as conn:
                with conn.cursor() as cur:
                    state_patch = {'rbook': {
                        'status': 'done',
                        'duration_ms': result['ms'],
                        'source': 'direct_api',
                        'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S+00:00',
                                                     time.gmtime()),
                    }}
                    cur.execute("""
                        UPDATE vauto_lookups
                        SET rbook_competitive_set = %s::jsonb,
                            rbook_completed_at = NOW(),
                            api_price_guides = %s::jsonb,
                            api_refreshed_at = NOW(),
                            enrichment_state = COALESCE(enrichment_state,
                                                        '{}'::jsonb) || %s::jsonb
                        WHERE bid_id = %s
                          AND rbook_completed_at IS NULL
                    """, (
                        json.dumps(cs),
                        json.dumps(pg) if pg else None,
                        json.dumps(state_patch),
                        bid_id,
                    ))
                    rows = cur.rowcount
                conn.commit()
            log.info('bid %d direct write: rows=%d', bid_id, rows)

            # Best-effort: poke the assess gate so Gemini fires immediately
            # if rbook+manheim are now both done.
            try:
                from app import _maybe_fire_assessment
                _maybe_fire_assessment(bid_id, source='direct_api_rbook')
            except Exception as gate_err:
                log.debug('bid %d gate poke err: %s', bid_id, gate_err)
        except Exception as e:
            log.warning('bid %d direct write failed: %s', bid_id, e)
    except Exception as e:
        log.exception('bid %d kick_direct_enrichment unhandled: %s', bid_id, e)
