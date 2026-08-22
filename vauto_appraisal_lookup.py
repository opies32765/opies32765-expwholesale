"""vauto_appraisal_lookup.py — resolve a saved vAuto appraisal permalink from a
VIN, server-side, with NO browser.

VAUTO_URL_SERVERSIDE_2026_08_21.

WHY THIS EXISTS
    The worker used to be the only way to get `vauto_lookups.appraisal_url`: it
    saved the appraisal, then had roughly NINE SECONDS to find it again through
    the ProVision UI (goto List.aspx, sleep 3, click Go once, re-read the DOM
    three times). On 2026-08-21 vAuto stopped surfacing a just-saved appraisal
    inside that window and every capture failed — 0 of 5 bids, after ~99% for a
    week. Without appraisal_url, `kick_direct_enrichment` returns early
    ("direct deferred — no appraisal_url"), so rBook comps and Manheim
    transactions never load and the AI prices the car on ZERO market data.
    The documented fallback (legacy EWEnrichRbook on "VM 120") has been dead
    since 2026-05-13 and that VM no longer exists.

    The appraisals were never missing. They were saved correctly and are
    findable minutes later — just not in a nine-second window. So the fix is to
    stop racing: resolve the permalink whenever we like, from the server.

HOW
    ProVision's Quick-Search is one cookie-authenticated form POST to
    /Va/Appraisal/ListData.ashx (captured off the live page). C1 already keeps a
    pooled vAuto session for its BFF calls, so it can make that same call. No
    Playwright, no worker, no timing window.

TWO TRAPS, both hit while building this:
  1. The response is JAVASCRIPT, not JSON — it embeds `new Date(1787325980308)`
     literals, so json.loads() dies at the row payload. Sanitise them first.
     (A VIN with no results parses fine, which makes this look intermittent.)
  2. `rows` are positional ARRAYS. The index of "Id" must be read from the
     `columns` list, never hardcoded — it is column 0 of 93 today, and that is
     not a promise.

Every result is verified against the VIN column before it is returned, so a
sloppy search can never attach another car's appraisal to a bid.
"""
from __future__ import annotations

import json
import re

LIST_DATA_URL = 'https://provision.vauto.app.coxautoinc.com/Va/Appraisal/ListData.ashx'
APPRAISAL_URL_FMT = ('https://provision.vauto.app.coxautoinc.com'
                     '/Va/Appraisal/Default.aspx?Id=%s&AppraisalStatus=InProcess')

_DATE_LITERAL = re.compile(r'new Date\((-?\d+)\)')

__all__ = ['resolve_appraisal_url', 'search_appraisals', 'LIST_DATA_URL']


def _decode(text):
    """ListData.ashx emits JS date literals inside otherwise-JSON. Strip them."""
    return json.loads(_DATE_LITERAL.sub(r'\1', text))


def _column_names(payload):
    out = []
    for c in payload.get('columns') or []:
        if isinstance(c, dict):
            out.append(c.get('dataIndex') or c.get('name') or c.get('field') or '')
        else:
            out.append(str(c))
    return out


def search_appraisals(session, vin, day_span=90, page_size=20, timeout=30):
    """Raw Quick-Search. Returns a list of dicts (column name -> value)."""
    body = {
        'sorts': '[{"sort":"AppraisalLastModified","dir":"DESC"}]',
        '_pageSize': str(page_size),
        '_sortBy': 'AppraisalLastModified DESC',
        'LastModifiedDaySpan': str(day_span),
        '_mandatoryFilters': '',
        'QuickSearch': vin,
        'IsExactWordMatch': 'false',
        'gridSrcName': 'appraisalDetail',
        'switchReport': '',
    }
    r = session.post(LIST_DATA_URL, data=body, timeout=timeout, headers={
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': '*/*',
    })
    r.raise_for_status()
    payload = _decode(r.text)
    names = _column_names(payload)
    rows = payload.get('rows') or []
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
        elif isinstance(row, list) and names:
            out.append({n: row[i] for i, n in enumerate(names) if i < len(row) and n})
    return out


def resolve_appraisal_url(session, vin, day_span=90):
    """VIN -> (appraisal_url, meta) or (None, meta).

    meta always carries `reason` so a caller can log WHY it got nothing:
      ok | no_rows | vin_mismatch | no_id | error:<type>

    The VIN check is the point: Quick-Search is a fuzzy contains-match, so a
    row is only accepted when its own Vin column equals the VIN we asked for.
    """
    vin = (vin or '').strip().upper()
    meta = {'vin': vin, 'reason': 'error:unset', 'n_rows': 0}
    if len(vin) != 17:
        meta['reason'] = 'error:bad_vin'
        return None, meta
    try:
        rows = search_appraisals(session, vin, day_span=day_span)
    except Exception as e:
        meta['reason'] = 'error:%s' % type(e).__name__
        meta['detail'] = str(e)[:200]
        return None, meta
    meta['n_rows'] = len(rows)
    if not rows:
        meta['reason'] = 'no_rows'
        return None, meta
    exact = [r for r in rows if str(r.get('Vin') or '').strip().upper() == vin]
    if not exact:
        meta['reason'] = 'vin_mismatch'
        meta['saw'] = [str(r.get('Vin') or '')[:17] for r in rows[:3]]
        return None, meta
    # rows come back AppraisalLastModified DESC, so the newest appraisal wins.
    row = exact[0]
    apid = row.get('Id')
    if not apid:
        meta['reason'] = 'no_id'
        return None, meta
    meta['reason'] = 'ok'
    meta['appraisal_id'] = apid
    meta['ymm'] = ' '.join(str(row.get(k) or '') for k in ('ModelYear', 'Make', 'Model')).strip()
    return APPRAISAL_URL_FMT % apid, meta
