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


def _fetch_today_candidates(cur) -> list[dict]:
    """Today's candidates. If today has no rows yet (early-morning before
    the pipeline runs), fall back to the most-recent snapshot_date so the
    operator can still review yesterday's picks."""
    cur.execute("""
        SELECT MAX(snapshot_date) AS d FROM porsche_arb_candidates
    """)
    latest = (cur.fetchone() or {}).get("d")
    if not latest:
        return []

    cur.execute("""
        SELECT *
          FROM porsche_arb_candidates
         WHERE snapshot_date = %s
         ORDER BY arb_score_v2 DESC NULLS LAST,
                  arb_score DESC NULLS LAST,
                  net_spread DESC NULLS LAST
    """, (latest,))
    rows = [_normalize_row(r) for r in cur.fetchall()]

    # Attach per-VIN merged option canonical set for chip display.
    vins = [r.get('subject_vin') for r in rows if r.get('subject_vin')]
    options_by_vin = _fetch_options(cur, vins)
    for r in rows:
        v = (r.get('subject_vin') or '').upper()
        r['options'] = options_by_vin.get(v, {})
    return rows


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
    """Render the operator dashboard."""
    with _conn() as c, c.cursor() as cur:
        today = _fetch_today_candidates(cur)
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
    v2_scored = sum(1 for r in today if r.get("arb_score_v2") is not None)
    snapshot_date = today[0]["snapshot_date"] if today else None

    # Unique models + regions for filter chips
    models = sorted({(r.get("subject_model") or "").strip()
                     for r in today if r.get("subject_model")})
    regions = sorted({(r.get("home_region") or "").strip()
                      for r in today if r.get("home_region")})

    return render_template(
        "porsche_arb.html",
        today_candidates=today,
        flagged_count_today=len(flagged),
        total_count_today=len(today),
        top_score_today=top_score,
        top_score_v2=top_score_v2,
        v2_scored_count=v2_scored,
        recent_runs=runs,
        regional_history=history,
        snapshot_date=snapshot_date,
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
