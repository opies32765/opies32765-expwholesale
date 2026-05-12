"""vauto_api.py — direct calls to vAuto's BFF, replacing the Playwright
scrapers for rbook + price-guide enrichment.

Two endpoints, both under https://slot2.bff.megazord.vauto.app.coxautoinc.com:

  POST /api/competition/vehicles            — rbook competitive set
  POST /api/priceGuides?useSavedFields=true — Manheim/BlackBook/KBB/NADA blocks

Captured + verified working: 2026-05-07. See replay.py + replay_crossbid.py
for proof. Production rbook scrape was 99s; direct call is ~0.7s.

Usage (with cookie_jar):
    from cookie_jar import CookieJar
    from vauto_api import (fetch_competitive_set, fetch_price_guides,
                           parse_competitive_set, parse_price_guides)

    jar = CookieJar('vauto_session.json')
    jar.load()
    rb = fetch_competitive_set(vehicle_dict, jar.get_cookies(),
                               jar.get_headers())
    parsed = parse_competitive_set(rb)
    rows = parsed['rows']  # ~150 retail listings
"""
from __future__ import annotations
import requests
from typing import Optional


BFF_BASE = 'https://slot2.bff.megazord.vauto.app.coxautoinc.com'
COMPETITION_URL = f'{BFF_BASE}/api/competition/vehicles'
PRICE_GUIDES_URL = f'{BFF_BASE}/api/priceGuides?useSavedFields=true'

# Default market criteria — matches what vAuto's UI sends with default
# settings. Distance 10000 mi is effectively "all of US".
DEFAULT_MARKET_CRITERIA = {
    'distance': 10000,
    'isAutoDistance': False,
    'minOdometer': None,
    'maxOdometer': None,
    'marketMode': 'Recent',
    'postalCode': None,
}

# All possible price-guide buckets vAuto knows about. Setting all to None
# means "give me whatever you have" — vAuto fills in only the ones the
# dealer is subscribed to.
ALL_PRICE_GUIDE_OPTIONS = {
    'BlackBook': None, 'BlackBookCanada': None, 'Galves': None,
    'KelleyBlueBook': None, 'KbbOnline': None, 'Manheim': None,
    'Naaa': None, 'Nada': None, 'Tim': None, 'Radar': None,
    'None': None,
}
DEFAULT_AVAILABLE_GUIDES = ['Radar', 'BlackBook', 'KelleyBlueBook',
                            'KbbOnline', 'Manheim', 'Nada']


# ── Exceptions ─────────────────────────────────────────────────────────

class VAutoAuthError(Exception):
    """401/403 — caller should refresh cookies and retry."""


class VAutoServerError(Exception):
    """5xx — vAuto's backend is having issues."""


class VAutoBadRequestError(Exception):
    """4xx (other than auth) — payload likely malformed."""


# ── Internal helpers ───────────────────────────────────────────────────

def _post(url: str, payload: dict, headers: dict, cookies: dict,
          timeout: int) -> requests.Response:
    """Single POST with error classification."""
    r = requests.post(url, json=payload, headers=headers, cookies=cookies,
                      timeout=timeout)
    if r.status_code in (401, 403):
        raise VAutoAuthError(f'auth failed: {r.status_code}')
    if 500 <= r.status_code < 600:
        raise VAutoServerError(f'server error {r.status_code}: {r.text[:200]}')
    if 400 <= r.status_code < 500:
        raise VAutoBadRequestError(f'bad request {r.status_code}: {r.text[:200]}')
    r.raise_for_status()
    return r


# ── Public API ─────────────────────────────────────────────────────────

def _default_criteria_options(vehicle: dict) -> list[dict]:
    """Default criteriaOptions matching vAuto's UI defaults.

    vAuto's UI auto-selects Series+BodyType+ModelYear when those fields are
    populated on the canonical decode. We mirror that here. Verified
    empirically (2026-05-08): when paired with a CANONICAL `vehicle` dict
    sourced from `/api/appraisal/vehicleInfo?strictYMM=true`, this filter
    set produces a healthy comp count for series-named vehicles (BMW 840i:
    41 comps, Defender 110: 68, Ferrari 296 GTS: 29) — vs. 1 row when the
    operator-typed marketing model name was sent and `criteriaOptions=[]`.

    Only emits filters for fields the canonical decode populated; missing
    fields are silently skipped.
    """
    out: list[dict] = []
    series = vehicle.get('series')
    if series not in (None, ''):
        out.append({'fieldId': 'Series',
                    'optionId': str(series),
                    'isSelected': True})
    body_type = vehicle.get('bodyType')
    if body_type not in (None, ''):
        out.append({'fieldId': 'BodyType',
                    'optionId': str(body_type),
                    'isSelected': True})
    year = vehicle.get('year')
    if year not in (None, ''):
        out.append({'fieldId': 'ModelYear',
                    'optionId': str(year),
                    'isSelected': True})
    # 2026-05-11: REVERTED — `Trim` criteriaOption causes vAuto BFF to
    # return HTTP 500 (Cox doesn't accept 'Trim' as a valid fieldId, or
    # rejects multi-value trim strings like "GT3 / GT3 Touring"). The
    # downstream VIN-prefix-5 post-filter in market_intel.py + app.py
    # remains the working strict-peer mechanism. Keep the canon_trim
    # signal flowing for display + dealer match purposes only.
    return out


def fetch_competitive_set(vehicle: dict, cookies: dict[str, str],
                          headers: dict[str, str],
                          market_criteria: Optional[dict] = None,
                          criteria_options: Optional[list[dict]] = None,
                          list_price: int = 0,
                          appraisal_id: str = 'unused',
                          option_codes=None,
                          timeout: int = 30) -> dict:
    """rbook competitive set. Returns full response dict.

    Response shape:
        {
            'myVehicle': {35 fields including vin, vehicleTitle, body, ...},
            'competitiveSetVehicles': [{34 fields}, ...]  # ~150 retail listings
        }

    `appraisal_id` should be the bid's REAL vAuto appraisalId (parsed from
    the captured appraisal_url's `Id=...` query param). The default
    `'unused'` is retained as a tripwire for callers that haven't been
    updated — pass a real id from production paths.

    `option_codes` should be passed through verbatim from the
    `vehicleInfo.optionCodes` response of `/api/appraisal/vehicleInfo` —
    vAuto uses these for option-aware comp matching.
    """
    if criteria_options is None:
        criteria_options = _default_criteria_options(vehicle)
    payload = {
        'appraisalId': appraisal_id,
        'vehicle': vehicle,
        'marketCriteria': market_criteria or DEFAULT_MARKET_CRITERIA,
        'criteriaOptions': criteria_options,
        'listPrice': list_price,
        'shouldShowMarketInfoForMyVehicle': True,
        # 2026-05-11: tried flipping to True (vAuto's "smart" trim-aware
        # mode) to fix the Carrera-vs-GTS bleed. vAuto returned HTTP 500
        # on every call — either requires extra fields our payload lacks,
        # or it's a paid Cox tier we don't have. Reverted. The actual fix
        # for loose comps lives in the VIN-prefix-5 post-filter applied
        # in market_intel.py + app.py rbook closest_3 paths.
        'useSmarterCompetitiveSet': False,
        'optionCodes': option_codes,
    }
    r = _post(COMPETITION_URL, payload, headers, cookies, timeout)
    return r.json()


def fetch_price_guides(vehicle: dict, cookies: dict[str, str],
                       headers: dict[str, str],
                       appraisal_id: str = 'unused',
                       postal_code: Optional[str] = None,
                       timeout: int = 30) -> dict:
    """All price guides in one call. Returns full response dict.

    Response shape (top-level keys):
        blackBook, kbb, kbbOnline, nada, manheim,
        blackBookCanada, galves, naaa, tim,
        defaultPriceGuideAdjustment, defaultPriceGuideAdjustmentType,
        priceGuideAdjustmentComment

    Each present block has:
        priceGuide, versions, currentVersionId, selectedCondition,
        defaultDisposition, fields, pricings, messages

    The manheim block additionally has:
        averageAuctionPrice, averageOdometer, evbh, avgEvbh,
        excludeBuildAdjust
    """
    payload = {
        'appraisalId': appraisal_id,
        'vehicle': vehicle,
        'priceGuideOptions': dict(ALL_PRICE_GUIDE_OPTIONS),
        'availablePriceGuides': list(DEFAULT_AVAILABLE_GUIDES),
        'postalCode': postal_code,
    }
    r = _post(PRICE_GUIDES_URL, payload, headers, cookies, timeout)
    return r.json()


# ── Output normalizers ─────────────────────────────────────────────────

def _parse_competition_json(body: dict) -> list[dict]:
    """Convert vAuto's /api/competition/vehicles response into the snake_case
    row schema that the rest of EW expects.

    MUST stay in sync with /opt/expwholesale/enrichment_rbook.py:557 — the
    bid card template + AI prompts read these specific field names. If the
    production parser changes, this function must change with it.
    """
    rows: list[dict] = []
    if not body:
        return rows
    competitors = body.get('competitiveSetVehicles') or []
    seen_vins: set = set()
    for v in competitors:
        if not isinstance(v, dict):
            continue
        vin = (v.get('vin') or '').strip().upper() or None
        if vin and vin in seen_vins:
            continue
        if vin:
            seen_vins.add(vin)

        # vehicleTitle is "2025 Audi Q8 55 Prestige quattro" — split into pieces
        title = (v.get('vehicleTitle') or '').strip()
        title_parts = title.split(None, 3)
        year = None
        try:
            year = int(title_parts[0]) if title_parts else None
        except (ValueError, IndexError):
            year = None
        make = title_parts[1] if len(title_parts) > 1 else None
        model = title_parts[2] if len(title_parts) > 2 else None
        trim = title_parts[3] if len(title_parts) > 3 else None

        rows.append({
            'vin':                  vin,
            'year':                 year,
            'make':                 make,
            'model':                model,
            'trim':                 trim,
            'color':                v.get('exteriorBaseColor'),
            'interior':             v.get('interiorDescription'),
            'price':                v.get('price'),
            'effective_price':      v.get('effectivePrice'),
            'mileage':              v.get('odometer'),
            'days_on_lot':          v.get('daysInInventory'),
            'dealer':               v.get('sellerName'),
            'dealer_city':          v.get('sellerCity'),
            'dealer_state':         v.get('sellerRegion'),
            'dealer_postal':        v.get('sellerPostalCode'),
            'distance':             v.get('distance'),
            'rank':                 v.get('rank'),
            'pending_sale':         v.get('pendingSale'),
            'is_certified':         v.get('isCertified'),
            'carfax_one_owner':     v.get('carfaxOneOwner'),
            'carfax_clean_title':   v.get('carfaxCleanTitle'),
            'body':                 v.get('body'),
            'engine':               v.get('engine'),
            'transmission':         v.get('transmission'),
            'drivetrain':           v.get('driveTrain'),
            'detail_uri':           v.get('detailUri'),
        })
    return rows


def parse_competitive_set(response: dict) -> dict:
    """Normalize rbook response into the production shape.

    Returns the same dict shape that `enrichment_rbook.scrape()` returns —
    drop-in compatible with what the existing /api/enrichment/submit handler
    writes into `vauto_lookups.rbook_competitive_set`.
    """
    rows = _parse_competition_json(response)
    return {
        'rows': rows,
        'count_text': str(len(rows)),
        'stocking_report': None,
        'panel_found': True,
        'n_visible': len(rows),
        'source': 'competition_api_direct',
    }


def parse_price_guides(response: dict) -> dict:
    """Pull the most-used fields out of the priceGuides response.

    Returns a dict with one key per guide that's present, containing the
    fields production cares about. Null/missing guides are omitted.
    """
    out: dict = {}

    m = response.get('manheim')
    if m:
        out['manheim'] = {
            'average_auction_price': m.get('averageAuctionPrice'),
            'average_odometer': m.get('averageOdometer'),
            'pricings': m.get('pricings') or [],
            'fields': m.get('fields') or [],
            'price_guide': m.get('priceGuide'),
            'evbh': m.get('evbh'),
            'avg_evbh': m.get('avgEvbh'),
        }

    for src_key, dst_key in [('blackBook', 'blackBook'),
                             ('kbb', 'kbb'),
                             ('kbbOnline', 'kbbOnline'),
                             ('nada', 'nada')]:
        g = response.get(src_key)
        if g:
            out[dst_key] = {
                'price_guide': g.get('priceGuide'),
                'pricings': g.get('pricings') or [],
                'fields': g.get('fields') or [],
            }

    return out
