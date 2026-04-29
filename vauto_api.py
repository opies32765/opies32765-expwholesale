"""
vAuto JSON API client.

Replaces Selenium DOM scraping for the data-extraction half of every bid.
Hits the Cox/vAuto BFF directly with cookies + entity headers.

Discovered 2026-04-28 via DevTools HAR capture from Worker-2 — the Provision
UI is a thin client over a JSON BFF. All 6 book values, Carfax status,
Manheim transactions, competition data come back as structured JSON in 1-3
seconds. No Chrome required for the data; only screenshots still need a
browser.

Auth model: cookies + 3 dealer-scoped headers. Cookies are session-only
(`vAutoAuth*` not persisted in Chrome's SQLite — RAM only) so a long-lived
Chrome session keeper is required to keep them current. The `cookie_keeper`
process exports them to Postgres every 60s for this client to consume.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)


VAUTO_BFF = 'https://slot1.bff.megazord.vauto.app.coxautoinc.com'
VAUTO_VAWEB_BFF = 'https://bff.vaweb.vauto.app.coxautoinc.com'

# Default request timeout. The BFF is fast; if a single endpoint exceeds
# this we want to fail fast and let the worker move on rather than hold a
# claim hostage. Selenium had 120s timeouts; we don't.
DEFAULT_TIMEOUT = 15


class VautoAuthError(Exception):
    """401/403 from vAuto. Cookies likely stale; cookie_keeper needs to
    refresh from the live Chrome session."""


class VautoApiError(Exception):
    """Any other non-2xx from vAuto, or unparseable response."""


@dataclass
class VautoSession:
    """Holds the auth state needed to make BFF calls.

    `cookies` is the full Chrome cookie jar dict. `entity_id` and
    `platform_user_id` are dealer-scoped — they identify which dealer
    "owns" the appraisal we're querying. For EW these come from the
    captured headers (jwaCvVdjsSFLY6C4O3LS63o-... / 871ccb54-8ee2-...).
    """
    cookies: dict[str, str] = field(default_factory=dict)
    entity_id: str = ''
    platform_user_id: str = ''
    user_agent: str = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/147.0.0.0 Safari/537.36')

    def headers(self, content_type: str = 'application/json') -> dict[str, str]:
        return {
            'accept': 'application/json',
            'accept-language': 'en-US,en;q=0.9',
            'content-type': content_type,
            'appraisalentityid': self.entity_id,
            'currententityid': self.entity_id,
            'platformuserid': self.platform_user_id,
            'origin': 'https://provision.vauto.app.coxautoinc.com',
            'referer': 'https://provision.vauto.app.coxautoinc.com/',
            'user-agent': self.user_agent,
            'sec-ch-ua': '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
        }

    @classmethod
    def from_db(cls, db_conn, label: str = 'oscarpas') -> 'VautoSession':
        """Load the latest session from Postgres. Workers call this on
        every bid; cookie_keeper writes new rows in the background.
        """
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT cookies, entity_id, platform_user_id, user_agent
                FROM vauto_session WHERE label = %s
            """, (label,))
            row = cur.fetchone()
        if not row:
            raise VautoApiError(
                f'No vauto_session row for label={label!r}. '
                f'Run seed_vauto_session.py first.'
            )
        # Row tuple or DictRow — handle both
        if hasattr(row, 'keys'):
            cookies = row['cookies']
            entity_id = row['entity_id']
            platform_user_id = row['platform_user_id']
            ua = row.get('user_agent') if hasattr(row, 'get') else row['user_agent']
        else:
            cookies, entity_id, platform_user_id, ua = row
        # cookies may come back as JSON string from JSONB depending on driver
        if isinstance(cookies, str):
            import json as _json
            cookies = _json.loads(cookies)
        return cls(
            cookies=cookies or {},
            entity_id=entity_id or '',
            platform_user_id=platform_user_id or '',
            user_agent=ua or cls.user_agent,
        )

    @classmethod
    def from_curl(cls, curl_text: str) -> 'VautoSession':
        """Parse a `curl 'https://...' -H ... -b '...' --data-raw '...'`
        block and return a session. Used to seed the session from a manually
        captured curl during onboarding / cookie refresh.
        """
        cookies: dict[str, str] = {}
        entity_id = ''
        platform_user_id = ''
        # Header lines: -H 'name: value'
        # Cookie line: -b 'name=value; name=value'
        in_b = False
        for line in curl_text.splitlines():
            line = line.strip().rstrip('\\').strip()
            if line.startswith("-b '"):
                cookie_str = line[len("-b '"):].rstrip("'")
                for part in cookie_str.split(';'):
                    if '=' in part:
                        k, v = part.split('=', 1)
                        cookies[k.strip()] = v.strip()
            elif line.startswith("-H '"):
                hdr = line[len("-H '"):].rstrip("'")
                if ':' in hdr:
                    k, v = hdr.split(':', 1)
                    k = k.strip().lower()
                    v = v.strip()
                    if k == 'currententityid' and not entity_id:
                        entity_id = v
                    elif k == 'appraisalentityid' and not entity_id:
                        entity_id = v
                    elif k == 'platformuserid':
                        platform_user_id = v
        return cls(cookies=cookies, entity_id=entity_id,
                   platform_user_id=platform_user_id)


# ── Core HTTP wrapper ────────────────────────────────────────────────────────

def _request(method: str, url: str, sess: VautoSession,
             json_body: dict | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Execute a request against the vAuto BFF. Returns parsed JSON.
    Raises VautoAuthError on 401/403, VautoApiError otherwise."""
    t0 = time.monotonic()
    try:
        resp = requests.request(
            method, url,
            headers=sess.headers(),
            cookies=sess.cookies,
            json=json_body,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise VautoApiError(f'network error on {method} {url}: {e}') from e
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if resp.status_code in (401, 403):
        raise VautoAuthError(
            f'{resp.status_code} on {method} {url} — cookies stale '
            f'(elapsed {elapsed_ms}ms, body: {resp.text[:200]})'
        )
    if not resp.ok:
        raise VautoApiError(
            f'{resp.status_code} on {method} {url} — {resp.text[:300]}'
        )
    try:
        return resp.json()
    except ValueError as e:
        raise VautoApiError(
            f'non-JSON response from {url}: {resp.text[:200]}'
        ) from e


# ── Endpoint wrappers ────────────────────────────────────────────────────────

def get_price_guides(sess: VautoSession, appraisal_id: str,
                     vehicle: dict) -> dict:
    """All 6+ book values for a vehicle on an existing appraisal.

    `vehicle` must contain at minimum: vin, year, make, model, odometer.
    Returns the full BFF response — top-level keys are book names
    (blackBook, kbb, kbbOnline, nada, manheim, galves, naaa, tim, ...).
    """
    body = {
        'appraisalId': appraisal_id,
        'vehicle': _normalize_vehicle(vehicle),
        'priceGuideOptions': {
            'BlackBook': None, 'BlackBookCanada': None, 'Galves': None,
            'KelleyBlueBook': None, 'KbbOnline': None, 'Manheim': None,
            'Naaa': None, 'Nada': None, 'Tim': None, 'Radar': None,
            'None': None,
        },
        'postalCode': None,
        'availablePriceGuides': [
            'Radar', 'BlackBook', 'Manheim', 'KelleyBlueBook',
            'KbbOnline', 'Nada',
        ],
    }
    return _request(
        'POST',
        f'{VAUTO_BFF}/api/priceGuides?useSavedFields=true',
        sess, json_body=body,
    )


def get_carfax_status(sess: VautoSession, vin: str) -> dict:
    """Carfax flags for a VIN. Returns booleans like hasReport, hasProblems,
    hasTotalLoss, hasAirbagDeployment, hasOdometerProblems."""
    return _request(
        'GET',
        f'{VAUTO_BFF}/api/carfax/report?vin={vin}',
        sess,
    )


def get_appraisal(sess: VautoSession, appraisal_id: str) -> dict:
    """Full appraisal record. Includes status (InProcess / Saved / etc.),
    vehicle, sales history, comps."""
    return _request(
        'GET',
        f'{VAUTO_BFF}/api/appraisal?appraisalId={appraisal_id}',
        sess,
    )


def get_appraisal_history(sess: VautoSession, appraisal_id: str) -> list[dict]:
    """Change-log for the appraisal."""
    return _request(
        'GET',
        f'{VAUTO_BFF}/api/reports/appraisalHistory?appraisalId={appraisal_id}',
        sess,
    )


def get_manheim_pricing(sess: VautoSession, appraisal_id: str,
                        vehicle: dict, condition: str = 'Average') -> dict:
    """Manheim/MMR pricing. Different host (vaweb BFF) and PascalCase body."""
    body = {
        'AppraisalId': appraisal_id,
        'Vehicle': {
            'Vin': vehicle.get('vin'),
            'Odometer': vehicle.get('odometer'),
            'ModelYear': vehicle.get('year'),
            'Make': vehicle.get('make'),
            'Model': vehicle.get('model'),
            'Series': vehicle.get('series') or vehicle.get('trim'),
            'ExteriorColor': vehicle.get('exteriorBaseColor'),
        },
        'PriceGuide': 12,  # Manheim
        'PostalCode': None,
        'AddDeducts': [{
            'FieldId': 'C',
            'OptionId': condition,
            'IsDefault': True,
        }],
        'OwningEntityId': sess.entity_id,
    }
    return _request(
        'POST',
        f'{VAUTO_VAWEB_BFF}/api/PricingData/Manheim',
        sess, json_body=body,
    )


def get_manheim_transactions(sess: VautoSession, manheim_id: str,
                             page_size: int = 500) -> dict:
    """Manheim auction sale history for a vehicle (after we have its
    Manheim ID from get_manheim_pricing)."""
    body = {
        'ManheimId': manheim_id,
        'Region': 'NA',
        'SortColumn': 0,
        'SortDirection': 0,
        'FirstRecord': 0,
        'PageSize': page_size,
    }
    return _request(
        'POST',
        f'{VAUTO_VAWEB_BFF}/api/ManheimTransactions',
        sess, json_body=body,
    )


def get_competition_radar(sess: VautoSession, appraisal_id: str,
                          vehicle: dict) -> dict:
    """Competition / market position. Likely contains rBook days-in-stock
    info too — pending a separate spike to confirm."""
    body = {
        'appraisalId': appraisal_id,
        'vehicle': _normalize_vehicle(vehicle, with_odometer_uom=True),
    }
    return _request(
        'POST',
        f'{VAUTO_BFF}/api/competition/radarData?initialMarket=false',
        sess, json_body=body,
    )


# ── Saved-vAuto URL construction (no API call needed) ────────────────────────

def saved_appraisal_url(appraisal_id: str) -> str:
    """The Saved-vAuto URL the bid page links to. Pure construction —
    once we have the appraisalId we already know the URL. This replaces
    Beelink-115's URL-capture side-job entirely."""
    return (f'https://provision.vauto.app.coxautoinc.com/Va/Appraisal/'
            f'Default.aspx?Id={appraisal_id}&AppraisalStatus=Saved')


def in_process_appraisal_url(appraisal_id: str) -> str:
    """In-process variant. Used while the Selenium worker is still
    actively appraising; flips to saved_appraisal_url once it saves."""
    return (f'https://provision.vauto.app.coxautoinc.com/Va/Appraisal/'
            f'Default.aspx?Id={appraisal_id}&AppraisalStatus=InProcess')


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_vehicle(vehicle: dict, with_odometer_uom: bool = False) -> dict:
    """Build the `vehicle` sub-object in the shape the BFF expects.
    Accepts a loose dict with our internal keys; emits the BFF schema."""
    out = {
        'vin': vehicle.get('vin'),
        'odometer': vehicle.get('odometer') or vehicle.get('mileage'),
        'year': vehicle.get('year') or vehicle.get('modelYear'),
        'make': vehicle.get('make'),
        'model': vehicle.get('model'),
        'series': vehicle.get('series') or vehicle.get('trim'),
        'bodyCabStyle': vehicle.get('bodyCabStyle'),
        'bodyType': vehicle.get('bodyType'),
        'driveTrainType': vehicle.get('driveTrainType'),
        'engineCylinderCount': vehicle.get('engineCylinderCount'),
        'engineDisplacement': vehicle.get('engineDisplacement'),
        'transmissionType': vehicle.get('transmissionType'),
        'interiorColor': vehicle.get('interiorColor'),
        'exteriorBaseColor': vehicle.get('exteriorBaseColor'),
        'seriesDetail': vehicle.get('seriesDetail'),
        'bodyDoorCount': vehicle.get('bodyDoorCount'),
        'driveTrainWheelCount': vehicle.get('driveTrainWheelCount'),
        'engineAspiration': vehicle.get('engineAspiration'),
        'engineFuelType': vehicle.get('engineFuelType'),
        'engineFuelIntake': vehicle.get('engineFuelIntake'),
        'transmissionGearCount': vehicle.get('transmissionGearCount'),
    }
    if with_odometer_uom:
        out['odometerUom'] = vehicle.get('odometerUom') or 'Miles'
    return out


def appraisal_id_from_url(url: str) -> str | None:
    """Extract the appraisalId hash from a vAuto Default.aspx URL.
    Tolerates either AppraisalStatus=Saved or =InProcess."""
    if not url or 'Id=' not in url:
        return None
    try:
        # Default.aspx?Id=<hash>&AppraisalStatus=...
        after_id = url.split('Id=', 1)[1]
        # Stop at next & or end
        return after_id.split('&', 1)[0]
    except Exception:
        return None


# ── Book-value extraction (collapse BFF JSON → our flat schema) ──────────────

def extract_books(price_guides_response: dict,
                  condition: str = 'Average') -> dict[str, int | None]:
    """Reduce the rich priceGuides response to the 6 flat values our
    `vauto_lookups` schema stores. Returns dict with keys:
        rbook, mmr, kbb, kbb_com, jd_power, black_book

    The BFF returns each book as a structured object with `versions`,
    `fields`, `fieldOptions`, and `pricings` (the actual dollar amounts).
    We pick the dollar amount matching `condition` (Clean / Average / Rough
    / ExtraClean) and the default disposition.
    """
    out = {
        'rbook': None,
        'mmr': None,
        'kbb': None,
        'kbb_com': None,
        'jd_power': None,
        'black_book': None,
    }

    book_to_key = {
        'blackBook': 'black_book',
        'kbb': 'kbb',
        'kbbOnline': 'kbb_com',
        'nada': 'jd_power',  # NADA was rebranded to JDPower
        'manheim': 'mmr',
    }

    for book_key, our_key in book_to_key.items():
        book = price_guides_response.get(book_key)
        if not isinstance(book, dict):
            continue
        value = _extract_book_value(book, condition)
        if value is not None:
            out[our_key] = value

    # rBook = TIM (vAuto's internal index) maps closest to "rBook" in the UI.
    tim = price_guides_response.get('tim')
    if isinstance(tim, dict):
        out['rbook'] = _extract_book_value(tim, condition)

    return out


def _extract_book_value(book: dict, condition: str) -> int | None:
    """Walk a single book's response to find the dollar amount for the
    given condition (Clean / Average / Rough / ExtraClean) at the default
    disposition (Wholesale / Retail / TradeIn).

    Schema (observed):
        { fields: [{ fieldId, fieldOptions: [{ optionId, pricings: [...] }] }],
          defaultDisposition: 'Wholesale' }

    Pricings entries vary by book; common shape is
    {Disposition: 'Wholesale', Value: 28500} or similar. We cast a wide
    net and take the first numeric value matching disposition+condition.
    """
    default_disp = book.get('defaultDisposition') or 'Wholesale'
    fields = book.get('fields') or []
    for f in fields:
        if f.get('fieldId') != 'C':  # 'C' = Condition
            continue
        for opt in f.get('fieldOptions') or []:
            if opt.get('optionId') != condition:
                continue
            for p in opt.get('pricings') or []:
                disp = p.get('Disposition') or p.get('disposition')
                if disp and str(disp).lower() != default_disp.lower():
                    continue
                val = (p.get('Value') or p.get('value')
                       or p.get('Price') or p.get('price'))
                if isinstance(val, (int, float)) and val > 0:
                    return int(val)
    return None


# ── End-to-end "look up everything" convenience ──────────────────────────────

def lookup_vehicle(sess: VautoSession, appraisal_id: str,
                   vehicle: dict) -> dict:
    """One-shot lookup combining priceGuides + carfax_status + manheim.
    Returns a flat dict in the shape `/api/vauto/submit` expects.
    Each sub-call is wrapped so a single failure doesn't poison the rest.
    """
    out: dict[str, Any] = {
        'bid_id': None,  # caller fills
        'vin': vehicle.get('vin'),
        'rbook': None,
        'wholesale_avg': None,
        'retail_avg': None,
        'mmr_val': None,
        'kbb_val': None,
        'kbb_com_val': None,
        'jd_power_val': None,
        'black_book_val': None,
        'price_rank': None,
        'adj_pct_market': None,
        'title_status': None,
        'carfax_screenshot': None,
        'autocheck_screenshot': None,
        'raw': {},
        'appraisal_url': saved_appraisal_url(appraisal_id),
    }

    # 1. Price guides — the big one
    try:
        pg = get_price_guides(sess, appraisal_id, vehicle)
        out['raw']['priceGuides'] = pg
        books = extract_books(pg)
        out['rbook'] = books['rbook']
        out['mmr_val'] = books['mmr']
        out['kbb_val'] = books['kbb']
        out['kbb_com_val'] = books['kbb_com']
        out['jd_power_val'] = books['jd_power']
        out['black_book_val'] = books['black_book']
    except VautoAuthError:
        raise  # bubble up — cookie refresh needed
    except Exception as e:
        log.warning('priceGuides failed for %s: %s', appraisal_id, e)

    # 2. Carfax status (booleans, no screenshot)
    try:
        cf = get_carfax_status(sess, vehicle.get('vin'))
        out['raw']['carfaxStatus'] = cf
        report = cf.get('report') or {}
        # Title-status synthesis: if any of the bad flags fire, mark
        # accident; otherwise clean.
        if report.get('hasTotalLoss'):
            out['title_status'] = 'total loss'
        elif report.get('hasProblems'):
            out['title_status'] = 'accident'
        else:
            out['title_status'] = 'clean'
    except VautoAuthError:
        raise
    except Exception as e:
        log.warning('carfax/report failed for %s: %s', vehicle.get('vin'), e)

    return out
