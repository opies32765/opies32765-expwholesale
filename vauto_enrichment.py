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

    # body-style fallback — vAuto BFF treats body-suffix model names
    # ('RS 6 Avant', 'S550 Coupe') as strict filters that return ~1 row.
    # When initial comp set is suspiciously small AND model contains a
    # space, retry with the last word stripped. Use the larger result.
    _initial_rows = len((result['competitive_set'] or {}).get('rows') or [])
    _model = vehicle.get('model') or ''
    if _initial_rows < 5 and ' ' in _model:
        _stripped = _model.rsplit(' ', 1)[0].strip()
        if _stripped and _stripped != _model:
            log.info('bid %d rbook %d rows on model=%r; retry with %r',
                     bid_id, _initial_rows, _model, _stripped)
            try:
                _v_retry = dict(vehicle)
                _v_retry['model'] = _stripped
                _rb_retry = fetch_competitive_set(
                    _v_retry, cookies, headers, appraisal_id=appraisal_id)
                _retry_parsed = parse_competitive_set(_rb_retry)
                _retry_rows = len(_retry_parsed.get('rows') or [])
                if _retry_rows > _initial_rows:
                    result['competitive_set'] = _retry_parsed
                    log.info('bid %d retry won: %d rows (vs %d)',
                             bid_id, _retry_rows, _initial_rows)
            except Exception as _retry_e:
                log.warning('bid %d body-strip retry err: %s',
                            bid_id, _retry_e)

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

        # Pre-compute market_intel and cache it on vauto_lookups so
        # bid_detail can skip the heavy rbook_competitive_set JSONB read.
        # ~152 KB column → 120ms+ TOAST decompression on every page load.
        market_intel_json = None
        try:
            from market_intel import compute_market_intel
            mi = compute_market_intel(
                {'year': vehicle.get('year'), 'make': vehicle.get('make'),
                 'model': vehicle.get('model'),
                 'mileage': vehicle.get('odometer'),
                 'vin': vehicle.get('vin')},
                None,  # manheim transactions — direct API doesn't have these yet
                cs,    # rbook competitive set we just fetched
                None,
            )
            if mi:
                market_intel_json = json.dumps(mi)
        except Exception as mi_err:
            log.debug('bid %d market_intel pre-compute failed: %s', bid_id, mi_err)

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
                    # UPSERT so direct API can complete even when no
                    # vauto_lookups row exists yet (intake hook no longer
                    # creates a placeholder; phase 1 workers now see the
                    # bid as eligible until one path or the other writes).
                    # Filter on rbook_completed_at IS NULL still applies
                    # so we don't clobber a worker's completed result.
                    cur.execute("""
                        INSERT INTO vauto_lookups
                            (bid_id, vin,
                             rbook_competitive_set, rbook_completed_at,
                             api_price_guides, api_refreshed_at,
                             market_intel_cached, enrichment_state,
                             looked_up_at)
                        VALUES (%s,
                                (SELECT vin FROM bids WHERE id=%s),
                                %s::jsonb, NOW(),
                                %s::jsonb, NOW(),
                                %s::jsonb, %s::jsonb, NOW())
                        ON CONFLICT (bid_id) DO UPDATE
                          SET rbook_competitive_set =
                                EXCLUDED.rbook_competitive_set,
                              rbook_completed_at = NOW(),
                              api_price_guides = EXCLUDED.api_price_guides,
                              api_refreshed_at = NOW(),
                              market_intel_cached =
                                EXCLUDED.market_intel_cached,
                              enrichment_state = COALESCE(
                                vauto_lookups.enrichment_state,
                                '{}'::jsonb) || EXCLUDED.enrichment_state
                          WHERE vauto_lookups.rbook_completed_at IS NULL
                    """, (
                        bid_id, bid_id,
                        json.dumps(cs),
                        json.dumps(pg) if pg else None,
                        market_intel_json,
                        json.dumps(state_patch),
                    ))
                    rows = cur.rowcount
                conn.commit()
            log.info('bid %d direct write: rows=%d (market_intel cached=%s)',
                     bid_id, rows, 'yes' if market_intel_json else 'no')

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
