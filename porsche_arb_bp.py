"""porsche_arb_bp.py — operator-facing dashboard for the Porsche
Arbitrage Scanner. Renders /porsche-arb with today's flagged candidates
plus per-card 30d regional trend mini-charts, status workflow buttons,
and links out to the source listing.

Companion to the daily pipeline that populates porsche_arb_candidates /
porsche_arb_regional_history / porsche_arb_runs. Read-mostly: only POST
endpoint is /porsche-arb/<id>/status which updates workflow status.

PORSCHE_ARB_2026_05_26
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
from flask import Blueprint, jsonify, render_template, request, redirect, url_for

bp = Blueprint("porsche_arb", __name__)

_VALID_STATUSES = ("new", "called", "pursuing", "passed", "acquired", "snoozed")


def _conn():
    """Use the same DATABASE_URL the rest of EW uses (5433 in production)."""
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
    )
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _normalize_row(r: dict) -> dict:
    """Make a candidate row JSON/template-friendly: cast Decimals to
    float, isoformat any datetimes, ensure flag_reasons is a list."""
    out = dict(r)
    for k, v in list(out.items()):
        if v is None:
            continue
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif type(v).__name__ == "Decimal":
            try:
                out[k] = float(v)
            except Exception:
                out[k] = None
    if out.get("flag_reasons") is None:
        out["flag_reasons"] = []
    # net_spread for sort fallback
    if out.get("net_spread") is None:
        out["net_spread"] = 0.0
    return out


def _fetch_today_candidates(cur) -> tuple[list[dict], object]:
    """Today's candidates. If today has no rows yet (early-morning before
    the pipeline runs), fall back to the most-recent snapshot_date so the
    operator can still review yesterday's picks.

    Returns (rows, snapshot_date). PORSCHE_ARB_PHASE2_2026_05_27 — sorts
    by arb_score_v3 first so the option-aware ranking surfaces."""
    cur.execute("""
        SELECT MAX(snapshot_date) AS d FROM porsche_arb_candidates
    """)
    latest = (cur.fetchone() or {}).get("d")
    if not latest:
        return [], None

    # Cap server-render at top 100 — page choked on 2,800+ cards before this.
    cur.execute("""
        SELECT *
          FROM porsche_arb_candidates
         WHERE snapshot_date = %s
           AND flagged = TRUE
         ORDER BY arb_score_v3 DESC NULLS LAST,
                  arb_score_v2 DESC NULLS LAST,
                  arb_score DESC NULLS LAST,
                  net_spread DESC NULLS LAST
         LIMIT 100
    """, (latest,))
    rows = [_normalize_row(r) for r in cur.fetchall()]

    # Attach per-VIN merged option canonical set for chip display.
    vins = [r.get('subject_vin') for r in rows if r.get('subject_vin')]
    options_by_vin = _fetch_options(cur, vins)
    # Attach detail-scrape (photos + classifier flags + msrp)
    detail_by_vin = _fetch_detail_scrapes(cur, vins)
    # Build the set of regions we'll need to query comps for
    regions_of_interest: set = set()
    for r in rows:
        if r.get('home_region'):
            regions_of_interest.add(r['home_region'])
        if r.get('best_other_region'):
            regions_of_interest.add(r['best_other_region'])
    anchor_vins = list({r.get('anchor_vin') for r in rows if r.get('anchor_vin')})
    comps_by_anchor = _fetch_comp_breakdown(
        cur, anchor_vins, regions_of_interest, latest)
    for r in rows:
        v = (r.get('subject_vin') or '').upper()
        r['options'] = options_by_vin.get(v, {})
        r['detail_scrape'] = detail_by_vin.get(v) or {}
        anchor = r.get('anchor_vin')
        all_comps_for_anchor = comps_by_anchor.get(anchor) or {}
        r['comps_home'] = all_comps_for_anchor.get(r.get('home_region')) or []
        r['comps_away'] = all_comps_for_anchor.get(r.get('best_other_region')) or []
    return rows, latest


def _fetch_options(cur, vins: list[str]) -> dict[str, dict]:
    """Bulk-fetch options from porsche_arb_options, merging across sources.
    Returns {VIN_UPPER: {canonical_key: True, ...}}."""
    if not vins:
        return {}
    upper = list({v.upper() for v in vins if v})
    cur.execute("""
        SELECT subject_vin, options_jsonb
          FROM porsche_arb_options
         WHERE subject_vin = ANY(%s)
    """, (upper,))
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        v = r['subject_vin'].upper()
        merged = out.setdefault(v, {})
        for k, val in (r['options_jsonb'] or {}).items():
            if val:
                merged[k] = True
    return out


def _fetch_detail_scrapes(cur, vins: list[str]) -> dict[str, dict]:
    """Bulk-fetch porsche_arb_detail_scrape rows for the subject VINs.
    PORSCHE_ARB_PHASE2_2026_05_27. Returns
    {VIN_UPPER: {photos: [...], top3: [...], options: {...},
                  msrp: int|None, confidence: float|None,
                  uri_host: str|None, sold_out: bool}}.
    """
    if not vins:
        return {}
    upper = list({v.upper() for v in vins if v})
    cur.execute("""
        SELECT subject_vin, uri_host, photos_jsonb, classifier_jsonb,
               classifier_confidence, msrp_estimate, sold_out,
               LENGTH(raw_text) AS raw_text_len
          FROM porsche_arb_detail_scrape
         WHERE subject_vin = ANY(%s)
    """, (upper,))
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        photos = r.get('photos_jsonb') or {}
        cls = r.get('classifier_jsonb') or {}
        # Only retain the TRUE flags from classifier_jsonb for the chip strip
        true_flags = {k: True for k, v in cls.items()
                       if isinstance(v, bool) and v is True}
        out[r['subject_vin'].upper()] = {
            'photos': photos.get('urls') or [],
            'top3': photos.get('top3') or [],
            'options': true_flags,
            'msrp': float(r['msrp_estimate']) if r.get('msrp_estimate') else None,
            'confidence': float(r['classifier_confidence']) if r.get('classifier_confidence') else None,
            'uri_host': r.get('uri_host'),
            'sold_out': bool(r.get('sold_out')),
            'raw_text_len': r.get('raw_text_len') or 0,
        }
    return out


def _fetch_comp_breakdown(cur, anchor_vins: list[str],
                           regions_of_interest: set,
                           snapshot_date) -> dict[str, dict]:
    """For each anchor VIN, return {region: [comp rows sorted by price]}
    restricted to the regions any candidate cares about (home +
    best_other across the whole page). Capped to 8 comps per (anchor,
    region) so the page doesn't blow up.

    Returns {anchor_vin: {region_name: [...comps]}}.
    """
    out: dict[str, dict] = {}
    if not anchor_vins or not regions_of_interest or not snapshot_date:
        return out
    cur.execute("""
        SELECT anchor_vin, comp_vin, year, model, trim, mileage,
               price, effective_price, days_on_lot, is_certified,
               exterior_color, dealer_name, dealer_city, dealer_state,
               region, detail_uri, pending_sale
          FROM porsche_arb_regional_comps
         WHERE anchor_vin = ANY(%s)
           AND snapshot_date = %s
           AND region = ANY(%s)
           AND COALESCE(price, effective_price) IS NOT NULL
         ORDER BY anchor_vin, region, price
    """, (anchor_vins, snapshot_date, list(regions_of_interest)))
    counts: dict[tuple, int] = {}
    for r in cur.fetchall():
        anchor_vin = r['anchor_vin']
        region = r['region']
        # Cap 8 per anchor+region (keep cheapest 8)
        key = (anchor_vin, region)
        if counts.get(key, 0) >= 8:
            continue
        counts[key] = counts.get(key, 0) + 1
        bucket = out.setdefault(anchor_vin, {}).setdefault(region, [])
        bucket.append({
            'vin': r['comp_vin'],
            'year': r['year'],
            'model': r['model'],
            'trim': r['trim'],
            'mileage': r['mileage'],
            'price': float(r['price']) if r['price'] is not None else None,
            'effective_price': float(r['effective_price']) if r['effective_price'] else None,
            'days_on_lot': r['days_on_lot'],
            'is_certified': bool(r['is_certified']) if r['is_certified'] is not None else None,
            'pending_sale': bool(r['pending_sale']) if r['pending_sale'] is not None else None,
            'exterior_color': r['exterior_color'],
            'dealer_name': r['dealer_name'],
            'dealer_city': r['dealer_city'],
            'dealer_state': r['dealer_state'],
            'region': r['region'],
            'detail_uri': r['detail_uri'],
        })
    return out


def _fetch_regional_history(cur, anchor_vins: list[str]) -> dict[str, list[dict]]:
    """Last 30d of per-region medians, grouped by anchor_vin. Used for
    each card's tiny inline trend chart (home line + best-other line)."""
    if not anchor_vins:
        return {}
    cur.execute("""
        SELECT anchor_vin, snapshot_date, region, n, median_price
          FROM porsche_arb_regional_history
         WHERE anchor_vin = ANY(%s)
           AND snapshot_date >= CURRENT_DATE - INTERVAL '30 days'
         ORDER BY anchor_vin, snapshot_date, region
    """, (list(anchor_vins),))
    out: dict[str, list[dict]] = {}
    for r in cur.fetchall():
        v = r["anchor_vin"]
        out.setdefault(v, []).append({
            "d": r["snapshot_date"].isoformat() if r.get("snapshot_date") else None,
            "region": r.get("region"),
            "median": float(r["median_price"]) if r.get("median_price") is not None else None,
            "n": r.get("n"),
        })
    return out


def _fetch_recent_runs(cur, limit: int = 5) -> list[dict]:
    cur.execute("""
        SELECT id, run_date, started_at, finished_at, status,
               anchor_vins_attempted, anchor_vins_succeeded,
               total_comps_returned, candidates_flagged,
               carfax_pulls, error_log, note
          FROM porsche_arb_runs
         ORDER BY started_at DESC
         LIMIT %s
    """, (limit,))
    out = []
    for r in cur.fetchall():
        d = dict(r)
        for k in ("started_at", "finished_at"):
            if d.get(k):
                d[k + "_iso"] = d[k].isoformat()
                d[k + "_h"] = d[k].strftime("%Y-%m-%d %H:%M")
        if d.get("run_date"):
            d["run_date"] = d["run_date"].isoformat()
        out.append(d)
    return out


@bp.route("/porsche-arb")
def porsche_arb_page():
    # NEW 2026-05-27: model filter via ?model=Cayenne etc. Empty = all.
    _q_model = (request.args.get("model") or "").strip().lower()
    """Render the operator dashboard."""
    with _conn() as c, c.cursor() as cur:
        today, snapshot_date = _fetch_today_candidates(cur)
        anchor_vins = list({r["anchor_vin"] for r in today if r.get("anchor_vin")})
        history = _fetch_regional_history(cur, anchor_vins)
        runs = _fetch_recent_runs(cur, limit=5)

    # Aggregates for the header
    flagged = [r for r in today if r.get("flagged")]
    top_score = max((r.get("arb_score") or 0) for r in flagged) if flagged else None
    top_score_v2 = max(
        (r.get("arb_score_v2") or 0) for r in flagged
        if r.get("arb_score_v2") is not None
    ) if any(r.get("arb_score_v2") is not None for r in flagged) else None
    top_score_v3 = max(
        (r.get("arb_score_v3") or 0) for r in flagged
        if r.get("arb_score_v3") is not None
    ) if any(r.get("arb_score_v3") is not None for r in flagged) else None
    v2_scored = sum(1 for r in today if r.get("arb_score_v2") is not None)
    v3_scored = sum(1 for r in today if r.get("arb_score_v3") is not None)
    detail_scraped = sum(1 for r in today if (r.get('detail_scrape') or {}).get('confidence') is not None)

    # Unique models — query the DB for ALL models in latest snapshot (not just the 100 fetched)
    with _conn() as _c2, _c2.cursor() as _cur2:
        _cur2.execute("""SELECT DISTINCT subject_model
                            FROM porsche_arb_candidates
                           WHERE flagged = TRUE
                             AND snapshot_date = (SELECT MAX(snapshot_date) FROM porsche_arb_candidates WHERE flagged = TRUE)
                           ORDER BY subject_model""")
        models = [r["subject_model"] for r in _cur2.fetchall() if r.get("subject_model")]
    regions = sorted({(r.get("home_region") or "").strip()
                      for r in today if r.get("home_region")})

    return render_template(
        "porsche_arb.html",
        today_candidates=today,
        flagged_count_today=len(flagged),
        total_count_today=len(today),
        top_score_today=top_score,
        top_score_v2=top_score_v2,
        top_score_v3=top_score_v3,
        v2_scored_count=v2_scored,
        v3_scored_count=v3_scored,
        detail_scraped_count=detail_scraped,
        recent_runs=runs,
        regional_history=history,
        snapshot_date=snapshot_date.isoformat() if snapshot_date else None,
        models=models,
        regions=regions,
    )


@bp.route("/porsche-arb/<int:cand_id>/status", methods=["POST"])
def porsche_arb_set_status(cand_id: int):
    """Update workflow status (+ optional note) for one candidate. Accepts
    either application/x-www-form-urlencoded (HTML form post) or JSON."""
    data = request.get_json(silent=True) or {}
    if request.form:
        data = {**data, **request.form.to_dict()}

    new_status = (data.get("status") or "").strip().lower()
    if new_status not in _VALID_STATUSES:
        if request.is_json:
            return jsonify({"ok": False, "error": "bad status"}), 400
        return ("bad status", 400)

    note = data.get("status_note")
    if isinstance(note, str):
        note = note.strip() or None

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE porsche_arb_candidates
                   SET status = %s,
                       status_note = COALESCE(%s, status_note),
                       status_updated_at = NOW(),
                       updated_at = NOW()
                 WHERE id = %s
                RETURNING id, status
            """, (new_status, note, cand_id))
            row = cur.fetchone()
            c.commit()
    except Exception as e:
        if request.is_json:
            return jsonify({"ok": False, "error": str(e)[:200]}), 500
        return (f"db error: {e}", 500)

    if not row:
        if request.is_json:
            return jsonify({"ok": False, "error": "not found"}), 404
        return ("not found", 404)

    if request.is_json:
        return jsonify({"ok": True, "id": row["id"], "status": row["status"]})
    # Plain HTML form post → redirect back to dashboard
    return redirect(url_for("porsche_arb.porsche_arb_page"))
