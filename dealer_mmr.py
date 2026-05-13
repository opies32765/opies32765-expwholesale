"""dealer_mmr.py — Manheim Market Report (MMR) direct-API client for
dealer-inventory enrichment.

Hits vAuto's vaweb BFF (/api/PricingData/Manheim) using the existing
production cookie jar at /opt/expwholesale/state/vauto_session.json. No
ManheimId required, no Playwright required. ~1s per VIN.

Returned data per VIN (when successful):
    wholesale_avg     — MMR wholesale baseline at Average condition (auction floor)
    wholesale_above   — MMR wholesale at Above-Average condition (clean car)
    wholesale_below   — MMR wholesale at Below-Average condition (rough)
    retail_avg        — Manheim's retail-equivalent at Average condition
    retail_above      — retail at Above
    retail_below      — retail at Below
    grade             — derived condition grade like "4.6"
    average_odometer  — average mileage of the comp pool used by Manheim
    sample_size       — count of underlying real sales (often 0 — the
                        FinalPrices number comes from Manheim's book model,
                        not necessarily fresh sales)

Returns None for any of the price fields when vAuto has no MMR data for
the VIN (e.g. rare trims, just-released models, unmatched VINs).

Auth model: same as vauto_bff_direct.py — reads cookies + entity headers
from CookieJar('/opt/expwholesale/state/vauto_session.json'). When the
session expires, every call returns VAutoAuthError; refresh the jar via
the existing prewarmer pipeline.
"""
from __future__ import annotations
import logging
import time
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

VAWEB_BFF = 'https://bff.vaweb.vauto.app.coxautoinc.com'
MMR_URL = f'{VAWEB_BFF}/api/PricingData/Manheim'

PRICE_GUIDE_MANHEIM = 12  # vAuto's internal enum for Manheim


class VAutoAuthError(Exception):
    """401/403 — caller should refresh cookies and retry."""


class VAutoMmrError(Exception):
    """Non-auth failure (5xx, 4xx, network)."""


def fetch_mmr(vehicle: dict, cookies: dict[str, str], headers: dict[str, str],
              appraisal_id: str, condition: str = 'Average',
              timeout: int = 20) -> dict:
    """Pull MMR for a single vehicle.

    `vehicle` must have: vin, year, make, model, mileage. Optional: trim
    (forwarded as Series, helps disambiguate). Returns the parsed result
    dict — see module docstring for keys.

    `appraisal_id` is the vAuto appraisal token from the cookie jar's
    captured-at session. vAuto accepts a non-matching appraisal_id as
    long as the session is valid and the Vehicle body is well-formed.

    Raises VAutoAuthError on 401/403, VAutoMmrError on anything else.
    """
    body = {
        'AppraisalId': appraisal_id,
        'Vehicle': {
            'Vin': vehicle['vin'],
            'Odometer': vehicle.get('mileage') or 0,
            'ModelYear': vehicle.get('year'),
            'Make': vehicle.get('make'),
            'Model': vehicle.get('model'),
            'Series': vehicle.get('trim') or vehicle.get('series'),
        },
        'PriceGuide': PRICE_GUIDE_MANHEIM,
        'PostalCode': vehicle.get('postal_code'),
        'AddDeducts': [{'FieldId': 'C', 'OptionId': condition, 'IsDefault': True}],
        'OwningEntityId': headers.get('currententityid'),
    }
    t0 = time.monotonic()
    try:
        r = requests.post(MMR_URL, headers=headers, cookies=cookies, json=body,
                          timeout=timeout)
    except requests.RequestException as e:
        raise VAutoMmrError(f'network error: {e}') from e
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if r.status_code in (401, 403):
        raise VAutoAuthError(f'{r.status_code} on MMR fetch — refresh cookies '
                             f'({elapsed_ms}ms)')
    if not r.ok:
        raise VAutoMmrError(f'{r.status_code} on MMR fetch: {r.text[:200]}')

    try:
        data = r.json()
    except ValueError as e:
        raise VAutoMmrError(f'non-JSON response: {r.text[:200]}') from e

    return _parse_mmr_response(data, elapsed_ms)


def _parse_mmr_response(data: dict, elapsed_ms: int) -> dict:
    """Pull MMR fields out of the vAuto vaweb /api/PricingData/Manheim
    response into a flat dict suitable for storage and scoring."""
    fp = data.get('FinalPrices') or {}
    ws = fp.get('Wholesale') or {}
    rt = fp.get('Retail') or {}

    # vAuto returns 0 (int) when it has the row but no MMR data; coerce
    # those to None so downstream consumers can distinguish "no data" from
    # "$0 wholesale" (which would always be a bug).
    def _nz(v):
        if v in (0, '0', None, ''):
            return None
        return v

    return {
        'wholesale_avg':   _nz(ws.get('Average')),
        'wholesale_above': _nz(ws.get('Above')),
        'wholesale_below': _nz(ws.get('Below')),
        'retail_avg':      _nz(rt.get('Average')),
        'retail_above':    _nz(rt.get('Above')),
        'retail_below':    _nz(rt.get('Below')),
        'grade':           data.get('AverageGrade'),
        'average_odometer': _nz(data.get('AverageOdometer')),
        'average_price':   _nz(data.get('AveragePrice')),
        'sample_size':     data.get('SampleSize') or 0,
        'complete':        bool(data.get('Complete')),
        'requires_subscription': bool(data.get('RequiresSubscription')),
        'used_best_fit':   bool(data.get('UsedBestFit')),
        'elapsed_ms':      elapsed_ms,
    }
