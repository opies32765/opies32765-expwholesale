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
from urllib.parse import urlparse, parse_qs

import requests

from cookie_jar import CookieJar
from vauto_bff_direct import (
    fetch_competitive_set, fetch_price_guides,
    parse_competitive_set, parse_price_guides,
    VAutoAuthError, VAutoServerError, VAutoBadRequestError,
)


VEHICLE_INFO_URL = ('https://slot2.bff.megazord.vauto.app.coxautoinc.com'
                    '/api/appraisal/vehicleInfo?strictYMM=true')


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

def enrich_bid_direct(bid_id: int, vehicle: dict,
                      appraisal_id: str,
                      option_codes=None) -> dict:
    """Fetch rbook + priceGuides for a bid via direct BFF API calls.

    Args:
        bid_id: EW bid id (used only for logging).
        vehicle: CANONICAL vehicle dict from
                 `/api/appraisal/vehicleInfo?strictYMM=true`. The caller is
                 responsible for sourcing this from vAuto — never synthesize
                 model/series/bodyType from the bids table.
        appraisal_id: REAL vAuto appraisalId (parsed from appraisal_url
                      Id= query param). Required — passing 'unused' or the
                      session's static appraisalId yields wrong comp counts
                      for series-named vehicles.
        option_codes: Pass-through from vehicleInfo.optionCodes.

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

    # Fire both calls in parallel. competitive_set uses the bid's REAL
    # appraisalId + canonical vehicle + vAuto-default criteriaOptions
    # (built inside fetch_competitive_set from the canonical decode).
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            rb_fut = pool.submit(fetch_competitive_set, vehicle, cookies,
                                 headers, appraisal_id=appraisal_id,
                                 option_codes=option_codes)
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


def build_vehicle_dict_from_bid(conn, bid_id: int):
    """Build a CANONICAL vehicle dict for the BFF call.

    The bids table stores operator-typed marketing names (e.g. "296",
    "840i", "Defender") which vAuto's BFF treats as literal-string
    filters and fails to match peers. The canonical model for these VINs
    is "296 GTS" / "8 Series" / "Defender 110" — different from what we
    store. So we ASK vAuto for its canonical decode via
    `/api/appraisal/vehicleInfo?strictYMM=true` and pass that verbatim.

    Returns: (vehicle_dict, option_codes) on success.
             None if the bid is missing vin/odometer or vehicleInfo
             refuses to decode the VIN.

    Constraints:
      - Never synthesize model/series/bodyType from the bids row.
      - If vehicleInfo returns 4xx/5xx, return None — the caller defers
        to the legacy claim path (VM 120 EWEnrichRbook).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT vin, mileage FROM bids WHERE id = %s", (bid_id,))
        row = cur.fetchone()
    if not row:
        return None
    if hasattr(row, 'keys'):
        vin = row['vin']
        mileage = row['mileage']
    else:
        vin, mileage = row[0], row[1]
    if not vin:
        return None
    odometer = int(mileage) if mileage else 0

    # Load session for cookies+headers
    try:
        jar = _get_jar()
    except Exception as e:
        log.warning('bid %d vehicleInfo: session load failed: %s', bid_id, e)
        return None
    if _session_too_stale(jar):
        log.warning('bid %d vehicleInfo: session stale (%.0fs)',
                    bid_id, jar.age_seconds())
        return None

    def _post_vehicle_info():
        return requests.post(
            VEHICLE_INFO_URL,
            json={'vin': vin, 'odometer': odometer, 'odometerUom': 'Miles'},
            headers=jar.get_headers(),
            cookies=jar.get_cookies(),
            timeout=15,
        )

    try:
        r = _post_vehicle_info()
    except Exception as e:
        log.warning('bid %d vehicleInfo POST failed: %s', bid_id, e)
        return None
    # Single retry on 401 — vAuto BFF occasionally emits a transient
    # "Entity and/or User is Null" 401 on otherwise-healthy sessions
    # (verified bid 1192, 2026-05-12 07:59 — surrounding bids 1190/1191/
    # 1194/1195 used the same session and succeeded). One immediate
    # retry catches this without invoking the 7-minute legacy fallback.
    if r.status_code == 401:
        log.warning('bid %d vehicleInfo 401 (body=%s) — retrying once',
                    bid_id, r.text[:120])
        try:
            r = _post_vehicle_info()
        except Exception as e:
            log.warning('bid %d vehicleInfo retry failed: %s', bid_id, e)
            return None
    if r.status_code != 200:
        log.warning('bid %d vehicleInfo status=%d body=%s',
                    bid_id, r.status_code, r.text[:200])
        return None
    try:
        data = r.json()
    except Exception as e:
        log.warning('bid %d vehicleInfo non-json: %s', bid_id, e)
        return None
    vehicle = data.get('vehicleInfo')
    if not vehicle or not vehicle.get('year') or not vehicle.get('make') \
            or not vehicle.get('model'):
        log.info('bid %d vehicleInfo: empty/incomplete decode for vin=%s',
                 bid_id, vin)
        return None
    option_codes = data.get('optionCodes')
    return (vehicle, option_codes)


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
        # Read appraisal_url first — if no real captured URL, defer to
        # legacy EWEnrichRbook (VM 120). The direct path REQUIRES a real
        # appraisalId; we don't fall back to the cookie session's static
        # one because vAuto returns wrong comp counts for series-named
        # vehicles when the appraisalId mismatches the vehicle.
        try:
            with db_conn_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT appraisal_url FROM vauto_lookups "
                        "WHERE bid_id=%s", (bid_id,))
                    row = cur.fetchone()
        except Exception as e:
            log.warning('bid %d appraisal_url fetch failed: %s', bid_id, e)
            return
        appraisal_url = None
        if row:
            appraisal_url = row[0] if not hasattr(row, 'keys') else row['appraisal_url']
        if not appraisal_url or appraisal_url == '__not_found__':
            log.info('bid %d direct deferred — no appraisal_url (legacy '
                     'EWEnrichRbook will pick up)', bid_id)
            return
        try:
            qs = parse_qs(urlparse(appraisal_url).query)
            appraisal_id = (qs.get('Id') or [None])[0]
        except Exception as e:
            log.warning('bid %d appraisal_url parse failed: %s', bid_id, e)
            return
        if not appraisal_id:
            log.warning('bid %d appraisal_url has no Id= param: %s',
                        bid_id, appraisal_url[:160])
            return

        # Canonical vehicleInfo decode (replaces bids-table read).
        try:
            with db_conn_factory() as conn:
                built = build_vehicle_dict_from_bid(conn, bid_id)
        except Exception as e:
            log.warning('bid %d vehicle fetch failed: %s', bid_id, e)
            return
        if not built:
            log.info('bid %d direct skipped — vehicleInfo unavailable, '
                     'legacy EWEnrichRbook will retry', bid_id)
            return
        vehicle, option_codes = built

        # 2026-05-11: inject bids.canon_trim into the vehicle dict so
        # _default_criteria_options() emits a Trim criteriaOption to vAuto.
        # The AccuTrade overseer writes canon_trim with high confidence,
        # which closes the Carrera-vs-GTS / F-150-vs-F-250 comp-bleed at
        # the source (vs the downstream VIN-prefix-5 post-filter).
        try:
            with db_conn_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT canon_trim, canon_confidence "
                                "FROM bids WHERE id=%s", (bid_id,))
                    crow = cur.fetchone()
            if crow:
                ct = (crow[0] if not hasattr(crow, 'keys') else crow['canon_trim'])
                cc = (crow[1] if not hasattr(crow, 'keys') else crow['canon_confidence'])
                if ct and cc and float(cc) >= 0.7:
                    vehicle = dict(vehicle)
                    vehicle['canon_trim'] = ct
                    log.info('bid %d injected canon_trim=%r (conf=%.2f) '
                             'into vAuto comp request', bid_id, ct, float(cc))
        except Exception as _ct_err:
            log.warning('bid %d canon_trim inject failed: %s', bid_id, _ct_err)

        # Direct API — uses REAL appraisalId + canonical vehicle +
        # vAuto-default criteriaOptions (Series/BodyType/ModelYear + Trim
        # if canon_trim is set with high confidence).
        result = enrich_bid_direct(bid_id, vehicle, appraisal_id,
                                   option_codes=option_codes)
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
