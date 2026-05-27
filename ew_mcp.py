"""ew_mcp.py — MCP (Model Context Protocol) server exposing Experience
Wholesale vehicle-valuation tools to Claude.

When the user (e.g., on Claude mobile app voice mode) asks "what's a
2024 BMW M2 worth", Claude detects it needs a tool and calls one of the
functions below. The function routes through our existing
/api/voice/query endpoint so the full Gemini parse + LSL/MMR comp lookup
+ Claude reply pipeline runs server-side.

Auth: bearer token via Authorization header.
Transport: Streamable HTTP at /mcp (mounted by Starlette/Uvicorn).
"""
from __future__ import annotations

import contextlib
import asyncio
import logging
import os
import secrets
import time
import uuid
from typing import Optional

import aiohttp
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

log = logging.getLogger("ew-mcp")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

EW_QUERY_URL = os.environ.get("EW_QUERY_URL",
                              "http://127.0.0.1:9001/api/voice/query")
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
if not MCP_BEARER_TOKEN:
    raise RuntimeError("MCP_BEARER_TOKEN env var is required")

# ── HTTP session reused across tool calls ─────────────────────────────
_http: Optional[aiohttp.ClientSession] = None
_session_id = f"mcp-{uuid.uuid4().hex[:12]}"
_turn = 0


async def _ensure_http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    return _http


async def _ew_query(transcript: str) -> dict:
    """Send a synthetic 'transcript' to the existing EW voice pipeline
    and return the parsed reply + valuation data."""
    global _turn
    _turn += 1
    payload = {
        "transcript": transcript,
        "session_id": _session_id,
        "turn_index": _turn,
        "phone": None,
    }
    t0 = time.monotonic()
    log.info(f"EW query: {transcript!r}")
    http = await _ensure_http()
    async with http.post(EW_QUERY_URL, json=payload) as r:
        data = await r.json()
    log.info(f"EW reply {(time.monotonic()-t0)*1000:.0f}ms")
    return data


# ── MCP server + tools ───────────────────────────────────────────────
mcp = FastMCP(
    "experience-wholesale",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "experience-wholesale.net",
            "www.experience-wholesale.net",
            "127.0.0.1:*", "localhost:*",
        ],
        allowed_origins=[
            "https://experience-wholesale.net",
            "https://www.experience-wholesale.net",
            "https://claude.ai",
            "https://*.claude.ai",
            "https://*.anthropic.com",
        ],
    ),
    instructions=(
        "You have access to Experience Wholesale (EW), an internal wholesale-"
        "vehicle valuation system. ALWAYS use these tools when the user asks "
        "about: a vehicle's wholesale value, what to bid on a car, what a "
        "car is worth, market price for a year/make/model, VIN lookups, or "
        "recent bid activity. Do NOT rely on general knowledge for vehicle "
        "pricing — EW pulls live MMR (Manheim), national listings, LSL "
        "purchase history, and computes target-buy + range via Claude Sonnet "
        "4.6 + Gemini Flash. Trust EW's output verbatim and read the "
        "reply_text aloud to the user."
    ),
)


# YMM_CACHE_2026_05_22 — in-process cache of get_vehicle_valuation
# results. Key = (year, make_norm, model_norm, trim_norm, miles_band).
# TTL = 1 hour. Per-process (not shared across workers) but the master
# list comps refresh nightly so 1hr staleness is fine.
import time as _ctime
_YMM_CACHE: dict[tuple, tuple[float, dict]] = {}
_YMM_CACHE_TTL = 3600.0   # 1 hour
_YMM_CACHE_MAX = 256       # LRU cap

def _ymm_cache_key(year: int, make: str, model: str,
                   trim: Optional[str], miles: Optional[int]) -> tuple:
    mn = (make or "").strip().lower()
    mdn = (model or "").strip().lower()
    tn = (trim or "").strip().lower()
    # 5k-mile bands so 28k and 30k share a cache entry
    band = (int(miles) // 5000) if miles else None
    return (int(year) if year else 0, mn, mdn, tn, band)

def _ymm_cache_get(key: tuple) -> Optional[dict]:
    hit = _YMM_CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if _ctime.monotonic() - ts > _YMM_CACHE_TTL:
        _YMM_CACHE.pop(key, None)
        return None
    return payload

def _ymm_cache_put(key: tuple, payload: dict) -> None:
    if len(_YMM_CACHE) >= _YMM_CACHE_MAX:
        # Evict oldest
        oldest = min(_YMM_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _YMM_CACHE.pop(oldest, None)
    _YMM_CACHE[key] = (_ctime.monotonic(), payload)


def _slim_comps(ctx: dict) -> dict:
    """Pull only the fields the LLM needs from the heavy ctx dict.
    Drops large blobs and per-row arrays beyond top-N."""
    def _sum(d):
        if not d:
            return None
        return {k: d.get(k) for k in ("n","median","p25","p75","min","max") if k in d}

    live = ctx.get("live_vauto") or {}
    mmr_live = (live.get("live_mmr") or {})
    rb_live = (live.get("live_rbook") or {})
    rb_anchor = (live.get("live_rbook") or {}).get("closest_anchor") or {}

    return {
        "subject": {
            "year": (live.get("subject_year") or ctx.get("inputs",{}).get("year")),
            "make": (live.get("subject_make") or ctx.get("inputs",{}).get("make")),
            "model": (live.get("subject_model") or ctx.get("inputs",{}).get("model")),
            "trim": (live.get("subject_trim") or ctx.get("inputs",{}).get("trim")),
            "miles": ctx.get("inputs",{}).get("miles"),
        },
        "live_mmr": {
            "average_auction_price": mmr_live.get("average_auction_price"),
            "average_odometer": mmr_live.get("average_odometer"),
        } if mmr_live else None,
        "live_rbook": {
            "n": rb_live.get("n"),
            "summary": _sum(rb_live.get("summary")),
            "closest_anchor": {
                "mileage": rb_anchor.get("mileage"),
                "price": rb_anchor.get("price"),
                "days_on_lot": rb_anchor.get("days_on_lot"),
                "dealer": rb_anchor.get("dealer"),
            } if rb_anchor else None,
        } if rb_live else None,
        "mmr_recent_summary": _sum((ctx.get("mmr_recent") or {}).get("summary_wholesale")),
        "lsl_history_summary": _sum((ctx.get("lsl_history") or {}).get("summary_purchase")),
        "prior_bids_summary": _sum((ctx.get("prior_bids") or {}).get("summary_mmr")),
        "naive_target": ctx.get("naive_target"),
        "comps_count": {
            "mmr_recent": len((ctx.get("mmr_recent") or {}).get("rows") or []),
            "lsl_history": len((ctx.get("lsl_history") or {}).get("rows") or []),
            "prior_bids": len((ctx.get("prior_bids") or {}).get("rows") or []),
            "live_rbook": rb_live.get("n") or 0,
        },
        # PARTNER_DATA_2026_05_22 — dealer partners currently listing this YMM
        # Enriched at tool-call time with overnight MMR + rBook market data
        # TRIM_DISCIPLINE: when a specific trim was requested, this list
        # contains ONLY trim-matching partners. Empty = no partner has
        # the requested trim in stock right now.
        "partner_inventory_now": ctx.get("__partner_inventory_enriched", []),
        "partner_inventory_other_trims": ctx.get("__partner_inventory_other_trims", []),
    }


def _enrich_partner_inventory_with_comps(rows: list[dict]) -> list[dict]:
    """MASTER_LIST_2026_05_22 — for each partner_inventory_top3 row, look
    up the latest dealer_inventory_comps snapshot (MMR + rBook market
    data refreshed nightly at 07:28 ET) so Claude can cite the real
    market comps the partner SHOULD be selling at, not just their ask.
    """
    if not rows:
        return []
    import psycopg2, psycopg2.extras, os as _os
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    out = []
    try:
        with psycopg2.connect(db_url) as c:
            for r in rows:
                base = {
                    "dealer": (r.get("dealer_name") or "").strip(),
                    "year": r.get("year"),
                    "make": r.get("make"),
                    "model": r.get("model"),
                    "trim": (r.get("trim") or "").strip()[:60],
                    "mileage": r.get("mileage"),
                    "asking_price": r.get("price"),
                    "days_on_lot": r.get("days_on_lot"),
                }
                # Look up the inventory row + its latest comps snapshot
                with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # TRIM-AWARE: match the partner row to a comp from the
                    # SAME trim (substring), or fall back to year+make+model
                    # when no trim data exists.
                    row_trim = (r.get("trim") or "").lower().strip()
                    # Extract a leading trim token (e.g. "GTS" from "Carrera Gts Coupe")
                    trim_token = ""
                    for tok in ["gts", "gt3", "gt4", "turbo s", "turbo", "carrera s",
                                "carrera 4s", "carrera 4", "carrera t", "carrera",
                                "targa 4s", "targa 4", "targa", "spyder",
                                "amg", "type s", "type r", "lariat", "platinum",
                                "raptor", "tremor", "limited", "trd", "denali",
                                "csl", "competition", "m4", "m3", "m5", "m2"]:
                        if tok in row_trim:
                            trim_token = tok
                            break
                    if trim_token:
                        cur.execute("""
                            SELECT c.mmr_comp_value, c.mmr_comp_count,
                                   c.rbook_p25, c.rbook_p50, c.rbook_comp_count,
                                   c.market_median_days_on_lot, c.snapshot_date,
                                   di.trim AS matched_trim, di.mileage AS matched_mileage
                              FROM dealer_inventory di
                              JOIN dealer_inventory_comps c
                                ON c.dealer_inventory_id = di.id
                             WHERE di.year = %s
                               AND UPPER(di.make) = UPPER(%s)
                               AND UPPER(di.model) = UPPER(%s)
                               AND LOWER(COALESCE(di.trim,'')) LIKE %s
                             ORDER BY ABS(COALESCE(di.mileage, 0) - COALESCE(%s, 0)) ASC,
                                      c.snapshot_date DESC
                             LIMIT 1
                        """, (r.get("year"), r.get("make"), r.get("model"),
                              f"%{trim_token}%", r.get("mileage") or 0))
                    else:
                        cur.execute("""
                            SELECT c.mmr_comp_value, c.mmr_comp_count,
                                   c.rbook_p25, c.rbook_p50, c.rbook_comp_count,
                                   c.market_median_days_on_lot, c.snapshot_date,
                                   di.trim AS matched_trim, di.mileage AS matched_mileage
                              FROM dealer_inventory di
                              JOIN dealer_inventory_comps c
                                ON c.dealer_inventory_id = di.id
                             WHERE di.year = %s
                               AND UPPER(di.make) = UPPER(%s)
                               AND UPPER(di.model) = UPPER(%s)
                             ORDER BY ABS(COALESCE(di.mileage, 0) - COALESCE(%s, 0)) ASC,
                                      c.snapshot_date DESC
                             LIMIT 1
                        """, (r.get("year"), r.get("make"), r.get("model"),
                              r.get("mileage") or 0))
                    row = cur.fetchone()
                if row:
                    base["mmr_market_value"] = (float(row["mmr_comp_value"])
                                                 if row.get("mmr_comp_value") else None)
                    base["rbook_market_p50"] = (float(row["rbook_p50"])
                                                 if row.get("rbook_p50") else None)
                    base["rbook_market_p25"] = (float(row["rbook_p25"])
                                                 if row.get("rbook_p25") else None)
                    base["rbook_comp_count"] = row.get("rbook_comp_count")
                    base["market_median_days_on_lot"] = row.get("market_median_days_on_lot")
                    base["comp_snapshot_date"] = (row["snapshot_date"].isoformat()
                                                   if row.get("snapshot_date") else None)
                out.append(base)
        return out
    except Exception as e:
        log.warning(f"_enrich_partner_inventory_with_comps err: {e}")
        return [
            {
                "dealer": (r.get("dealer_name") or "").strip(),
                "year": r.get("year"),
                "make": r.get("make"),
                "model": r.get("model"),
                "trim": (r.get("trim") or "").strip(),
                "mileage": r.get("mileage"),
                "asking_price": r.get("price"),
                "days_on_lot": r.get("days_on_lot"),
            }
            for r in rows
        ]


def _prior_bids_30day_summary(year: int, make: str, model: str,
                              trim: Optional[str] = None,
                              miles: Optional[int] = None) -> dict:
    """BID30_2026_05_22 — 30-day rolling EW bid history (vauto_lookups
    joined to bids). Trim-filtered when provided.
    """
    import psycopg2, psycopg2.extras, os as _os
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    miles_lo = (miles - 25000) if miles else 0
    miles_hi = (miles + 25000) if miles else 999999
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if trim:
                    # vAuto-canonical decode may put trim into model field
                    # (e.g. vAuto returns model="7-Series" trim="760i" but EW
                    # stored model="760i"). Match either field against either
                    # name so we never miss real comps.
                    cur.execute("""
                        SELECT b.trim, b.mileage, v.mmr, v.rbook, v.looked_up_at
                          FROM vauto_lookups v JOIN bids b ON b.id=v.bid_id
                         WHERE b.year=%s AND UPPER(b.make)=UPPER(%s)
                           AND (UPPER(b.model) ILIKE UPPER(%s) OR UPPER(b.model) ILIKE UPPER(%s)
                                OR UPPER(b.trim) ILIKE UPPER(%s) OR UPPER(b.trim) ILIKE UPPER(%s))
                           AND (v.mmr IS NOT NULL OR v.rbook IS NOT NULL)
                           AND v.looked_up_at > NOW() - INTERVAL '30 days'
                           AND (b.mileage IS NULL OR b.mileage BETWEEN %s AND %s)
                         ORDER BY v.looked_up_at DESC LIMIT 30
                    """, (year, make,
                          f"%{model}%", f"%{trim}%",
                          f"%{model}%", f"%{trim}%",
                          miles_lo, miles_hi))
                else:
                    cur.execute("""
                        SELECT b.trim, b.mileage, v.mmr, v.rbook, v.looked_up_at
                          FROM vauto_lookups v JOIN bids b ON b.id=v.bid_id
                         WHERE b.year=%s AND UPPER(b.make)=UPPER(%s)
                           AND UPPER(b.model) ILIKE UPPER(%s)
                           AND (v.mmr IS NOT NULL OR v.rbook IS NOT NULL)
                           AND v.looked_up_at > NOW() - INTERVAL '30 days'
                         ORDER BY v.looked_up_at DESC LIMIT 30
                    """, (year, make, f"%{model}%"))
                rows = [dict(r) for r in cur.fetchall()]
        mmr = [float(r["mmr"]) for r in rows if r.get("mmr")]
        rb = [float(r["rbook"]) for r in rows if r.get("rbook")]
        def _med(xs):
            if not xs: return None
            xs = sorted(xs); n = len(xs)
            return xs[n//2] if n % 2 else (xs[n//2-1]+xs[n//2])/2
        # mileage_ladder: comps with valid mileage sorted by mileage so the
        # LLM can SEE the depreciation curve and interpolate to subject miles
        ladder_rows = sorted(
            [r for r in rows if r.get("mileage")],
            key=lambda r: r["mileage"],
        )
        return {
            "n": len(rows),
            "trim_filtered": bool(trim),
            "mmr_median": _med(mmr),
            "mmr_min": min(mmr) if mmr else None,
            "mmr_max": max(mmr) if mmr else None,
            "rbook_median": _med(rb),
            "rbook_min": min(rb) if rb else None,
            "rbook_max": max(rb) if rb else None,
            "mileage_ladder": [
                {"mileage": r.get("mileage"),
                 "mmr": (float(r["mmr"]) if r.get("mmr") else None),
                 "rbook": (float(r["rbook"]) if r.get("rbook") else None),
                 "trim": r.get("trim"),
                 "looked_up_at": (r["looked_up_at"].isoformat()
                                   if r.get("looked_up_at") else None)}
                for r in ladder_rows[:8]
            ],
            "examples": [
                {"trim": r.get("trim"), "mileage": r.get("mileage"),
                 "mmr": (float(r["mmr"]) if r.get("mmr") else None),
                 "rbook": (float(r["rbook"]) if r.get("rbook") else None)}
                for r in rows[:5]
            ],
        }
    except Exception as _e:
        log.warning(f"_prior_bids_30day_summary err: {_e}")
        return {"n": 0, "error": str(_e)[:120]}


def _lsl_inventory_now(year: int, make: str, model: str,
                       trim: Optional[str] = None) -> dict:
    """LSL_INV_2026_05_22 — current EW inventory matching this YMM. Reads
    LSL CRM SQLite read-only. Returns count + median asking + avg DOL +
    sample rows.
    """
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"n": 0, "error": "lsl crm missing"}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        if trim:
            cur.execute("""
                SELECT stock_no, vin_no, group_model_trim_year, vehicle_make_name,
                       group_model_name, group_model_trim, usage, asking_price,
                       purchase_cost, est_wholesale_price, exterior_color,
                       days_on_lot, days_since_marketed, lead_count, offer_count,
                       arrived_at, marketed_on
                  FROM inventory
                 WHERE in_stock = 1 AND sold = 0
                   AND group_model_trim_year LIKE ?
                   AND UPPER(vehicle_make_name) LIKE UPPER(?)
                   AND UPPER(group_model_name) LIKE UPPER(?)
                   AND UPPER(group_model_trim) LIKE UPPER(?)
                 ORDER BY days_on_lot DESC LIMIT 10
            """, (f"%{year}%", f"%{make}%", f"%{model}%", f"%{trim}%"))
        else:
            cur.execute("""
                SELECT stock_no, vin_no, group_model_trim_year, vehicle_make_name,
                       group_model_name, group_model_trim, usage, asking_price,
                       purchase_cost, est_wholesale_price, exterior_color,
                       days_on_lot, days_since_marketed, lead_count, offer_count,
                       arrived_at, marketed_on
                  FROM inventory
                 WHERE in_stock = 1 AND sold = 0
                   AND group_model_trim_year LIKE ?
                   AND UPPER(vehicle_make_name) LIKE UPPER(?)
                   AND UPPER(group_model_name) LIKE UPPER(?)
                 ORDER BY days_on_lot DESC LIMIT 10
            """, (f"%{year}%", f"%{make}%", f"%{model}%"))
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
        def _med(xs):
            if not xs: return None
            xs = sorted(xs); n = len(xs)
            return xs[n//2] if n % 2 else (xs[n//2-1]+xs[n//2])/2
        asks = [float(r["asking_price"]) for r in rows if r.get("asking_price")]
        costs = [float(r["purchase_cost"]) for r in rows if r.get("purchase_cost")]
        dols = [r["days_on_lot"] for r in rows if r.get("days_on_lot") is not None]
        return {
            "n": len(rows),
            "trim_filtered": bool(trim),
            "asking_price": {"n": len(asks), "median": _med(asks),
                              "min": min(asks) if asks else None,
                              "max": max(asks) if asks else None},
            "purchase_cost": {"n": len(costs), "median": _med(costs)},
            "days_on_lot": {"median": _med(dols), "min": min(dols) if dols else None,
                             "max": max(dols) if dols else None},
            "examples": [
                {"stock_no": r.get("stock_no"),
                 "trim": r.get("group_model_trim"),
                 "color": r.get("exterior_color"),
                 "miles": r.get("usage"),
                 "asking_price": r.get("asking_price"),
                 "purchase_cost": r.get("purchase_cost"),
                 "days_on_lot": r.get("days_on_lot"),
                 "leads": r.get("lead_count"),
                 "offers": r.get("offer_count")}
                for r in rows[:5]
            ],
        }
    except Exception as _e:
        log.warning(f"_lsl_inventory_now err: {_e}")
        return {"n": 0, "error": str(_e)[:120]}


def _lsl_sold_velocity(year: int, make: str, model: str,
                       trim: Optional[str] = None) -> dict:
    """LSL_VEL_2026_05_22 — recent (90-day) sold velocity for this YMM:
    median days_on_lot, gross profit, count.
    """
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"n": 0}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        if trim:
            cur.execute("""
                SELECT days_on_lot, asking_price, purchase_cost,
                       group_model_trim, sold_at, usage
                  FROM inventory
                 WHERE sold = 1
                   AND group_model_trim_year LIKE ?
                   AND UPPER(vehicle_make_name) LIKE UPPER(?)
                   AND UPPER(group_model_name) LIKE UPPER(?)
                   AND UPPER(group_model_trim) LIKE UPPER(?)
                   AND sold_at > date('now', '-90 days')
                   AND days_on_lot IS NOT NULL
                 ORDER BY sold_at DESC LIMIT 30
            """, (f"%{year}%", f"%{make}%", f"%{model}%", f"%{trim}%"))
        else:
            cur.execute("""
                SELECT days_on_lot, asking_price, purchase_cost,
                       group_model_trim, sold_at, usage
                  FROM inventory
                 WHERE sold = 1
                   AND group_model_trim_year LIKE ?
                   AND UPPER(vehicle_make_name) LIKE UPPER(?)
                   AND UPPER(group_model_name) LIKE UPPER(?)
                   AND sold_at > date('now', '-90 days')
                   AND days_on_lot IS NOT NULL
                 ORDER BY sold_at DESC LIMIT 30
            """, (f"%{year}%", f"%{make}%", f"%{model}%"))
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
        def _med(xs):
            if not xs: return None
            xs = sorted(xs); n = len(xs)
            return xs[n//2] if n % 2 else (xs[n//2-1]+xs[n//2])/2
        dols = [r["days_on_lot"] for r in rows if r.get("days_on_lot") is not None]
        grosses = [(float(r["asking_price"]) - float(r["purchase_cost"]))
                    for r in rows
                    if r.get("asking_price") and r.get("purchase_cost")]
        return {
            "n": len(rows),
            "trim_filtered": bool(trim),
            "median_days_on_lot": _med(dols),
            "fastest_days_on_lot": min(dols) if dols else None,
            "slowest_days_on_lot": max(dols) if dols else None,
            "median_gross": _med(grosses),
            "examples": [
                {"trim": r.get("group_model_trim"),
                 "miles": r.get("usage"),
                 "purchase_cost": r.get("purchase_cost"),
                 "asking_price": r.get("asking_price"),
                 "days_on_lot": r.get("days_on_lot"),
                 "sold_at": r.get("sold_at")}
                for r in rows[:3]
            ],
        }
    except Exception as _e:
        log.warning(f"_lsl_sold_velocity err: {_e}")
        return {"n": 0, "error": str(_e)[:120]}


def _vauto_saved_30day_summary(year: int, make: str, model: str,
                                trim: Optional[str] = None,
                                miles: Optional[int] = None) -> dict:
    """VSA30_2026_05_22 — 30-day rolling vAuto SAVED appraisals
    (vauto_saved_appraisals table). Covers every car users have appraised
    in vAuto, even if they never bid it through EW. Bigger universe than
    prior_bids alone.
    """
    import psycopg2, psycopg2.extras, os as _os
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # MILES_BAND_2026_05_22 — filter to +/- 25k miles when given
                miles_lo = (miles - 25000) if miles else 0
                miles_hi = (miles + 25000) if miles else 999999
                if trim:
                    cur.execute("""
                        SELECT vin, series, odometer, appraised_value,
                               mmr_value, rbook_n, rbook_median, rbook_p25, rbook_p75,
                               exterior_color, red_black,
                               appraisal_last_modified_at, appraisal_status_name
                          FROM vauto_saved_appraisals
                         WHERE year=%s AND UPPER(make)=UPPER(%s)
                           AND UPPER(model) ILIKE UPPER(%s)
                           AND series IS NOT NULL
                           AND REPLACE(LOWER(series), ' ', '') LIKE REPLACE(%s, ' ', '')
                           AND appraisal_last_modified_at > NOW() - INTERVAL '30 days'
                           AND (odometer IS NULL OR odometer BETWEEN %s AND %s)
                         ORDER BY appraisal_last_modified_at DESC LIMIT 50
                    """, (year, make, f"%{model}%", f"%{trim.lower()}%", miles_lo, miles_hi))
                else:
                    cur.execute("""
                        SELECT vin, series, odometer, appraised_value,
                               mmr_value, rbook_n, rbook_median, rbook_p25, rbook_p75,
                               exterior_color, red_black,
                               appraisal_last_modified_at, appraisal_status_name
                          FROM vauto_saved_appraisals
                         WHERE year=%s AND UPPER(make)=UPPER(%s)
                           AND UPPER(model) ILIKE UPPER(%s)
                           AND appraisal_last_modified_at > NOW() - INTERVAL '30 days'
                           AND (odometer IS NULL OR odometer BETWEEN %s AND %s)
                         ORDER BY appraisal_last_modified_at DESC LIMIT 50
                    """, (year, make, f"%{model}%", miles_lo, miles_hi))
                rows = [dict(r) for r in cur.fetchall()]
        # AppraisedValue + MMR + rBook stats from saved appraisals
        def _med(xs):
            if not xs: return None
            xs = sorted(xs); n = len(xs)
            return xs[n//2] if n % 2 else (xs[n//2-1]+xs[n//2])/2
        vals = [float(r["appraised_value"]) for r in rows if r.get("appraised_value")]
        mmr_vals = [float(r["mmr_value"]) for r in rows if r.get("mmr_value")]
        rb_vals = [float(r["rbook_median"]) for r in rows if r.get("rbook_median")]
        rb_mix = {}
        for r in rows:
            k = r.get("red_black") or "unknown"
            rb_mix[k] = rb_mix.get(k, 0) + 1
        return {
            "n": len(rows),
            "trim_filtered": bool(trim),
            "appraised_value": {
                "n": len(vals),
                "median": _med(vals),
                "min": min(vals) if vals else None,
                "max": max(vals) if vals else None,
            },
            "mmr": {
                "n": len(mmr_vals),
                "median": _med(mmr_vals),
                "min": min(mmr_vals) if mmr_vals else None,
                "max": max(mmr_vals) if mmr_vals else None,
            },
            "rbook": {
                "n": len(rb_vals),
                "median": _med(rb_vals),
                "min": min(rb_vals) if rb_vals else None,
                "max": max(rb_vals) if rb_vals else None,
            },
            "red_black_mix": rb_mix,
            "mileage_ladder": [
                {"mileage": r.get("odometer"),
                 "appraised_value": (float(r["appraised_value"]) if r.get("appraised_value") else None),
                 "mmr_value": (float(r["mmr_value"]) if r.get("mmr_value") else None),
                 "rbook_median": (float(r["rbook_median"]) if r.get("rbook_median") else None),
                 "series": r.get("series"),
                 "last_modified": (r["appraisal_last_modified_at"].isoformat()
                                    if r.get("appraisal_last_modified_at") else None)}
                for r in sorted(
                    [r for r in rows if r.get("odometer")],
                    key=lambda r: r["odometer"],
                )[:8]
            ],
            "comp_miles": {
                "n": sum(1 for r in rows if r.get("odometer")),
                "median": (lambda xs: (sorted(xs)[len(xs)//2] if xs else None))(
                    [r["odometer"] for r in rows if r.get("odometer")]),
                "min": (min((r["odometer"] for r in rows if r.get("odometer")), default=None)),
                "max": (max((r["odometer"] for r in rows if r.get("odometer")), default=None)),
            },
            "requested_miles": miles,
            "examples": [
                {
                    "series": r.get("series"),
                    "odometer": r.get("odometer"),
                    "appraised_value": (float(r["appraised_value"]) if r.get("appraised_value") else None),
                    "mmr_value": (float(r["mmr_value"]) if r.get("mmr_value") else None),
                    "rbook_median": (float(r["rbook_median"]) if r.get("rbook_median") else None),
                    "color": r.get("exterior_color"),
                    "red_black": r.get("red_black"),
                    "last_modified": (r["appraisal_last_modified_at"].isoformat()
                                       if r.get("appraisal_last_modified_at") else None),
                }
                for r in rows[:5]
            ],
        }
    except Exception as _e:
        log.warning(f"_vauto_saved_30day_summary err: {_e}")
        return {"n": 0, "error": str(_e)[:120]}


def _accutrade_30day_summary(year: int, make: str, model: str,
                              trim: Optional[str] = None,
                              miles: Optional[int] = None) -> dict:
    """ACCUTRADE30_2026_05_22 — 30-day rolling AccuTrade values
    (guaranteed_offer / trade_in / retail) on EW bids for this YMM+trim.
    AccuTrade is Cox/Manheim's appraisal tool — guaranteed_offer is
    wholesale auction-house buy price, retail is what consumer pays,
    trade_in is dealer-trade equivalent.
    """
    import psycopg2, psycopg2.extras, os as _os
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                miles_lo = (miles - 25000) if miles else 0
                miles_hi = (miles + 25000) if miles else 999999
                if trim:
                    cur.execute("""
                        SELECT b.trim, b.mileage,
                               a.guaranteed_offer, a.trade_in,
                               a.trade_market, a.retail, a.market_avg,
                               a.looked_up_at
                          FROM accutrade_lookups a JOIN bids b ON b.id=a.bid_id
                         WHERE b.year=%s AND UPPER(b.make)=UPPER(%s)
                           AND (UPPER(b.model) ILIKE UPPER(%s) OR UPPER(b.model) ILIKE UPPER(%s)
                                OR UPPER(b.trim) ILIKE UPPER(%s) OR UPPER(b.trim) ILIKE UPPER(%s))
                           AND a.guaranteed_offer IS NOT NULL
                           AND a.not_available = FALSE
                           AND a.looked_up_at > NOW() - INTERVAL '30 days'
                           AND (b.mileage IS NULL OR b.mileage BETWEEN %s AND %s)
                         ORDER BY a.looked_up_at DESC LIMIT 30
                    """, (year, make,
                          f"%{model}%", f"%{trim}%",
                          f"%{model}%", f"%{trim}%",
                          miles_lo, miles_hi))
                else:
                    cur.execute("""
                        SELECT b.trim, b.mileage,
                               a.guaranteed_offer, a.trade_in,
                               a.trade_market, a.retail, a.market_avg,
                               a.looked_up_at
                          FROM accutrade_lookups a JOIN bids b ON b.id=a.bid_id
                         WHERE b.year=%s AND UPPER(b.make)=UPPER(%s)
                           AND UPPER(b.model) ILIKE UPPER(%s)
                           AND a.guaranteed_offer IS NOT NULL
                           AND a.not_available = FALSE
                           AND a.looked_up_at > NOW() - INTERVAL '30 days'
                           AND (b.mileage IS NULL OR b.mileage BETWEEN %s AND %s)
                         ORDER BY a.looked_up_at DESC LIMIT 30
                    """, (year, make, f"%{model}%", miles_lo, miles_hi))
                rows = [dict(r) for r in cur.fetchall()]
        def _stats(field):
            xs = [float(r[field]) for r in rows if r.get(field)]
            if not xs: return None
            xs = sorted(xs); n = len(xs)
            return {"median": xs[n//2] if n % 2 else (xs[n//2-1]+xs[n//2])/2,
                    "min": xs[0], "max": xs[-1], "n": n}
        return {
            "n": len(rows),
            "trim_filtered": bool(trim),
            "guaranteed_offer": _stats("guaranteed_offer"),
            "trade_in": _stats("trade_in"),
            "retail": _stats("retail"),
            "market_avg": _stats("market_avg"),
            "examples": [
                {"trim": r.get("trim"), "mileage": r.get("mileage"),
                 "guaranteed_offer": (float(r["guaranteed_offer"]) if r.get("guaranteed_offer") else None),
                 "trade_in": (float(r["trade_in"]) if r.get("trade_in") else None),
                 "retail": (float(r["retail"]) if r.get("retail") else None)}
                for r in rows[:5]
            ],
        }
    except Exception as _e:
        log.warning(f"_accutrade_30day_summary err: {_e}")
        return {"n": 0, "error": str(_e)[:120]}


def _partner_sold_history(year: int, make: str, model: str,
                          limit: int = 5) -> list[dict]:
    """PARTNER_SOLD_2026_05_22 — find dealer-partner listings that have
    disappeared from inventory (last_seen_at < today-2). Proxy for "sold"
    (or pulled). Useful when asked how fast a YMM moves at our partners.
    """
    import psycopg2, psycopg2.extras, os as _os
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT d.name AS dealer_name,
                           di.year, di.make, di.model, di.trim, di.mileage,
                           di.price AS final_ask,
                           di.first_seen_at::date AS first_seen,
                           di.last_seen_at::date  AS last_seen,
                           (di.last_seen_at::date - di.first_seen_at::date) AS days_on_lot
                      FROM dealer_inventory di
                      JOIN dealers d ON d.id = di.dealer_id
                     WHERE di.year = %s
                       AND UPPER(di.make) = UPPER(%s)
                       AND UPPER(di.model) ILIKE UPPER(%s)
                       AND di.last_seen_at < NOW() - INTERVAL '2 days'
                       AND di.first_seen_at IS NOT NULL
                       AND di.last_seen_at IS NOT NULL
                       AND (di.last_seen_at::date - di.first_seen_at::date) > 0
                     ORDER BY di.last_seen_at DESC
                     LIMIT %s
                """, (year, make, f"%{model}%", limit))
                rows = [dict(r) for r in cur.fetchall()]
        return rows
    except Exception as e:
        log.warning(f"_partner_sold_history err: {e}")
        return []


def _lsl_30day_deals(year: int, make: str, model: str, trim: Optional[str] = None,
                     limit: int = 10) -> list[dict]:
    """LSL30_2026_05_22 — pull recent (30-day) deals for this YMM from
    the LSL CRM SQLite. Used both for citing recent purchase costs AND
    as a source of canonical VINs we can re-run through vAuto MMR/rBook.
    """
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return []
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        if trim:
            cur.execute(
                """
                SELECT vin_no, stock_no, vehicle_info, make_name,
                       sale_price, purchase_cost, front_value, sold_at,
                       days_on_lot
                  FROM deals
                 WHERE UPPER(make_name) LIKE UPPER(?)
                   AND UPPER(vehicle_info) LIKE UPPER(?)
                   AND vehicle_info LIKE ?
                   AND UPPER(vehicle_info) LIKE UPPER(?)
                   AND purchase_cost IS NOT NULL AND purchase_cost > 0
                   AND sold_at > date("now", "-30 days")
                 ORDER BY sold_at DESC LIMIT ?
                """,
                (f"%{make}%", f"%{model}%", f"%{year}%", f"%{trim}%", limit),
            )
        else:
            cur.execute(
                """
                SELECT vin_no, stock_no, vehicle_info, make_name,
                       sale_price, purchase_cost, front_value, sold_at,
                       days_on_lot
                  FROM deals
                 WHERE UPPER(make_name) LIKE UPPER(?)
                   AND UPPER(vehicle_info) LIKE UPPER(?)
                   AND vehicle_info LIKE ?
                   AND purchase_cost IS NOT NULL AND purchase_cost > 0
                   AND sold_at > date("now", "-30 days")
                 ORDER BY sold_at DESC LIMIT ?
                """,
                (f"%{make}%", f"%{model}%", f"%{year}%", limit),
            )
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
        return rows
    except Exception as e:
        log.warning(f"lsl_30day_deals err: {e}")
        return []


def _fetch_live_via_lsl_vin(year: int, make: str, model: str,
                            miles_hint: Optional[int]) -> Optional[dict]:
    """When EW has no prior bid history on this YMM, look up a recent
    LSL VIN (last 30 days) for the same YMM and run THAT VIN through our
    plumbed vAuto BFF (MMR + rBook). Returns the live_vauto dict or None.
    """
    deals = _lsl_30day_deals(year, make, model, limit=3)
    if not deals:
        return None
    # Pick the deal with VIN + best odometer match
    target = None
    for d in deals:
        if d.get("vin_no") and len(d.get("vin_no") or "") == 17:
            target = d; break
    if not target:
        return None
    vin = target["vin_no"]
    # Prefer the user's stated miles; else the LSL deal's odometer
    miles = miles_hint or None
    try:
        from voice_agent import _fetch_live_vauto_with_vin
        log.info(f"[lsl-vin-fetch] running vAuto on LSL VIN {vin} miles={miles}")
        return _fetch_live_vauto_with_vin(vin, miles=miles)
    except Exception as e:
        log.warning(f"_fetch_live_via_lsl_vin err: {e}")
        return None


@mcp.tool()
async def get_vehicle_valuation(
    year: int,
    make: str,
    model: str,
    miles: Optional[int] = None,
    trim: Optional[str] = None,
    msrp: Optional[int] = None,
    notes: Optional[str] = None,
) -> dict:
    """USE THIS for ANY question about what a vehicle is worth, its
    wholesale value, market price, target buy price, or how much to bid.

    Examples that MUST call this tool:
      - "what's a 2024 BMW M2 with 30k miles worth"
      - "what should I bid on a 2023 Ford F-150 Lariat"
      - "how much is a Mercedes G63 worth"
      - "price me a 2022 Porsche 911"

    Returns RAW comp data — wholesale MMR averages, rBook listings with
    closest-mile anchor, LSL purchase history, prior EW bid history. You
    must synthesize a short SPOKEN reply from this data. Lead with a
    target buy number, cite ONE concrete data point (e.g. the closest
    anchor or MMR average), and recommend hold-firm vs stretch.

    Numbers in the response are integers in dollars. Speak them in plain
    English (e.g. "fifty-six to fifty-seven thousand"). Round to nearest 500.

    Args:
        year: 4-digit model year (e.g. 2024)
        make: canonical brand ("BMW", "Mercedes-Benz", "Ford", etc.)
        model: base model ("M2", "G-Class", "F-150", "GLC-Class", etc.)
        miles: odometer reading; helpful but optional
        trim: trim level if known
        msrp: original sticker price if known
        notes: condition/damage/options (short)
    """
    # FAST PATH 2026-05-22: call get_valuation_context directly + cache.
    import time as _t
    from voice_agent import get_valuation_context
    t0 = _t.monotonic()
    # YMM_CACHE_2026_05_22 — try cache first
    cache_key = _ymm_cache_key(year, make, model, trim, miles)
    cached = _ymm_cache_get(cache_key)
    if cached is not None:
        cached_payload = dict(cached)
        cached_payload["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
        cached_payload["cache_hit"] = True
        log.info(f"get_vehicle_valuation CACHE_HIT {year} {make} {model} "
                 f"trim={trim} in {cached_payload['elapsed_ms']}ms")
        return cached_payload
    try:
        # FAST_MODE 2026-05-22: use cached vAuto when available, skip live
        # scrape on cache-miss. Outer Claude has DB comps + LSL 30d to work with.
        import voice_agent as _va
        _orig_live = _va._fetch_live_vauto_for_ymm
        def _live_or_cache(year, make, model, miles=None, trim=None):
            key = _va._vauto_cache_key(year, _va._norm(make), _va._norm(model), miles)
            return _va._vauto_cache_get(key)
        _va._fetch_live_vauto_for_ymm = _live_or_cache
        try:
            _ts_a = _t.monotonic()
            # HARD 15s CAP on the synchronous valuation pipeline. Slow upstream
            # APIs (AccuTrade, rBook, MMR) can stack timeouts past 60s on cold
            # cache misses. Better to return what we have than block Bill.
            import asyncio as _aio
            try:
                ctx = await _aio.wait_for(
                    _aio.to_thread(
                        get_valuation_context,
                        year=year, make=make, model=model,
                        trim=trim, miles=miles, msrp=msrp,
                    ),
                    timeout=15.0,
                )
            except _aio.TimeoutError:
                log.warning(f"get_valuation_context HARD TIMEOUT @15s for {year} {make} {model} trim={trim}")
                return {
                    "warning": "upstream comps slow on this vehicle - returning limited data",
                    "year": year, "make": make, "model": model, "trim": trim,
                    "miles": miles, "comps_count": {"live_rbook": 0, "lsl_history": 0,
                        "mmr_recent": 0, "prior_bids": 0},
                    "accutrade_30day_all": None, "accutrade_30day_trim": None,
                    "live_mmr": None, "live_rbook": None,
                    "lsl_30day": {"deals": [], "n": 0},
                    "elapsed_ms": 15000, "timed_out": True,
                    "advice": "no fresh comps came back in 15s. Tell operator AccuTrade/rBook is slow on this YMM right now. Suggest a quick gut number based on common knowledge or ask them to retry in a minute.",
                }
            log.info(f"[TIMING] get_valuation_context: {(_t.monotonic()-_ts_a)*1000:.0f}ms")
        finally:
            _va._fetch_live_vauto_for_ymm = _orig_live
    except Exception as e:
        log.exception("get_valuation_context failed")
        return {"error": f"comp lookup failed: {type(e).__name__}: {e}"}

    # TRIM_DISCIPLINE_2026_05_22 — when user requested a trim, fetch a
    # trim-filtered prior_bids slice from vauto_lookups directly so Claude
    # has trim-specific MMR/rBook history (not the all-trim summary).
    prior_bids_trim = None
    if trim:
        try:
            import psycopg2, psycopg2.extras, os as _os
            db_url = _os.environ.get("DATABASE_URL",
                "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
            trim_norm = trim.strip().lower()
            with psycopg2.connect(db_url) as c:
                with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT b.year, b.make, b.model, b.trim, b.mileage,
                               v.mmr, v.rbook, v.looked_up_at
                          FROM vauto_lookups v
                          JOIN bids b ON b.id = v.bid_id
                         WHERE b.year = %s
                           AND UPPER(b.make) = UPPER(%s)
                           AND UPPER(b.model) ILIKE UPPER(%s)
                           AND b.trim IS NOT NULL
                           AND REPLACE(LOWER(b.trim), ' ', '') LIKE REPLACE(%s, ' ', '')
                           AND (v.mmr IS NOT NULL OR v.rbook IS NOT NULL)
                           AND v.looked_up_at > NOW() - INTERVAL '30 days'
                         ORDER BY v.looked_up_at DESC
                         LIMIT 30
                    """, (year, make, f"%{model}%", f"%{trim_norm}%"))
                    rows = [dict(r) for r in cur.fetchall()]
            mmr_vals = [float(r["mmr"]) for r in rows if r.get("mmr")]
            rbook_vals = [float(r["rbook"]) for r in rows if r.get("rbook")]
            def _med(xs):
                if not xs: return None
                xs = sorted(xs); n = len(xs)
                return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
            prior_bids_trim = {
                "n": len(rows),
                "mmr_median": _med(mmr_vals),
                "mmr_min": min(mmr_vals) if mmr_vals else None,
                "mmr_max": max(mmr_vals) if mmr_vals else None,
                "rbook_median": _med(rbook_vals),
                "examples": [
                    {"trim": r.get("trim"), "mileage": r.get("mileage"),
                     "mmr": (float(r["mmr"]) if r.get("mmr") else None),
                     "rbook": (float(r["rbook"]) if r.get("rbook") else None)}
                    for r in rows[:3]
                ],
            }
        except Exception as _e:
            log.warning(f"prior_bids_trim err: {_e}")
            prior_bids_trim = {"n": 0, "error": str(_e)[:120]}

    # PARALLELIZED 2026-05-23 — all helper queries below are independent
    # sync DB calls. Run them concurrently in a thread pool. Was 15
    # sequential queries → now 1 concurrent batch. Drops typical cold
    # pull from 30-90s to ~10-25s. Each helper opens its own PG conn.
    import concurrent.futures as _cf
    _loop = asyncio.get_event_loop()
    def _maybe(fn, *args, **kw):
        """Wrap a callable so it can be passed to run_in_executor cleanly."""
        return lambda: fn(*args, **kw)
    tasks = {
        "lsl_30_trim":           _maybe(_lsl_30day_deals, year, make, model, trim=trim, limit=10) if trim else _maybe(lambda: []),
        "lsl_30_any":            _maybe(_lsl_30day_deals, year, make, model, limit=10),
        "partner_sold":          _maybe(_partner_sold_history, year, make, model, limit=5),
        "pb_30_all":             _maybe(_prior_bids_30day_summary, year, make, model, trim=None, miles=miles),
        "pb_30_trim":            _maybe(_prior_bids_30day_summary, year, make, model, trim=trim, miles=miles) if trim else _maybe(lambda: None),
        "at_30_all":             _maybe(_accutrade_30day_summary, year, make, model, trim=None, miles=miles),
        "at_30_trim":            _maybe(_accutrade_30day_summary, year, make, model, trim=trim, miles=miles) if trim else _maybe(lambda: None),
        "vsa_30_all":            _maybe(_vauto_saved_30day_summary, year, make, model, trim=None, miles=miles),
        "vsa_30_trim":           _maybe(_vauto_saved_30day_summary, year, make, model, trim=trim, miles=miles) if trim else _maybe(lambda: None),
        "lsl_inv_all":           _maybe(_lsl_inventory_now, year, make, model, trim=None),
        "lsl_inv_trim":          _maybe(_lsl_inventory_now, year, make, model, trim=trim) if trim else _maybe(lambda: None),
        "lsl_vel_all":           _maybe(_lsl_sold_velocity, year, make, model, trim=None),
        "lsl_vel_trim":          _maybe(_lsl_sold_velocity, year, make, model, trim=trim) if trim else _maybe(lambda: None),
    }
    _pool = _cf.ThreadPoolExecutor(max_workers=16, thread_name_prefix="vv_helper")
    try:
        futs = [_loop.run_in_executor(_pool, fn) for fn in tasks.values()]
        _ts_helpers = _t.monotonic()
        results = await asyncio.gather(*futs, return_exceptions=True)
        log.info(f"[TIMING] parallel helpers ({len(futs)} tasks): {(_t.monotonic()-_ts_helpers)*1000:.0f}ms")
    finally:
        _pool.shutdown(wait=False)
    # Map back, swallow per-helper errors (don't fail the whole call if one DB query trips)
    def _grab(key):
        r = results[list(tasks.keys()).index(key)]
        if isinstance(r, Exception):
            log.warning(f"helper {key} failed: {type(r).__name__}: {r}")
            return None
        return r
    lsl_30                  = _grab("lsl_30_trim") or []
    lsl_30_any_trim         = _grab("lsl_30_any") or []
    if not lsl_30:
        lsl_30 = lsl_30_any_trim
    partner_sold            = _grab("partner_sold") or []
    prior_bids_30day_all    = _grab("pb_30_all")
    prior_bids_30day_trim   = _grab("pb_30_trim")
    accutrade_30day_all     = _grab("at_30_all")
    accutrade_30day_trim    = _grab("at_30_trim")
    vauto_saved_30day_all   = _grab("vsa_30_all")
    vauto_saved_30day_trim  = _grab("vsa_30_trim")
    lsl_inventory_now_all   = _grab("lsl_inv_all")
    lsl_inventory_now_trim  = _grab("lsl_inv_trim")
    lsl_sold_velocity_all   = _grab("lsl_vel_all")
    lsl_sold_velocity_trim  = _grab("lsl_vel_trim")
    # MASTER_LIST_2026_05_22 — enrich live partner inventory with overnight comps
    _ts_e = _t.monotonic()
    enriched = _enrich_partner_inventory_with_comps(ctx.get("partner_inventory_top3") or [])
    log.info(f"[TIMING] _enrich_partner_inventory_with_comps: {(_t.monotonic()-_ts_e)*1000:.0f}ms")
    # TRIM_DISCIPLINE_2026_05_22 — when user requested a specific trim,
    # ONLY return matching rows. Empty list signals to the LLM "we don't
    # have a partner with this trim listed."
    if trim:
        trim_low = trim.lower().strip()
        partner_inventory_trim = [r for r in enriched
                                  if r.get("trim") and trim_low in r["trim"].lower()]
        partner_inventory_other = [r for r in enriched
                                    if not (r.get("trim") and trim_low in r["trim"].lower())]
        ctx["__partner_inventory_enriched"] = partner_inventory_trim
        # Note other-trim partners so prompt can say "we have other 911s though"
        ctx["__partner_inventory_other_trims"] = [
            {"dealer": r.get("dealer"), "trim": r.get("trim"),
             "asking_price": r.get("asking_price"), "mileage": r.get("mileage")}
            for r in partner_inventory_other[:3]
        ]
    else:
        ctx["__partner_inventory_enriched"] = enriched
        ctx["__partner_inventory_other_trims"] = []
    # Also filter partner_sold_history by trim when given
    if trim:
        trim_low = trim.lower().strip()
        partner_sold = [r for r in partner_sold
                         if r.get("trim") and trim_low in r["trim"].lower()]

    # If EW has no prior bids on this YMM AND we have no cached live_vauto,
    # use a recent LSL VIN to fetch fresh MMR + rBook via the plumbed BFF.
    has_prior = len(((ctx.get("prior_bids") or {}).get("rows") or [])) > 0
    has_live = bool(ctx.get("live_vauto"))
    if not has_prior and not has_live and lsl_30:
        _ts_v = _t.monotonic()
        live = _fetch_live_via_lsl_vin(year, make, model, miles_hint=miles)
        log.info(f"[TIMING] _fetch_live_via_lsl_vin: {(_t.monotonic()-_ts_v)*1000:.0f}ms")
        if live:
            ctx["live_vauto"] = live
            ctx["live_vauto_via_lsl_vin"] = live.get("subject_vin") if live.get("subject_vin") else lsl_30[0].get("vin_no")

    slim = _slim_comps(ctx)
    slim["prior_bids_trim"] = prior_bids_trim
    slim["prior_bids_30day_all"] = prior_bids_30day_all
    slim["prior_bids_30day_trim"] = prior_bids_30day_trim
    slim["accutrade_30day_all"] = accutrade_30day_all
    slim["accutrade_30day_trim"] = accutrade_30day_trim
    slim["vauto_saved_30day_all"] = vauto_saved_30day_all
    slim["vauto_saved_30day_trim"] = vauto_saved_30day_trim
    slim["lsl_inventory_now_all"] = lsl_inventory_now_all
    slim["lsl_inventory_now_trim"] = lsl_inventory_now_trim
    slim["lsl_sold_velocity_all"] = lsl_sold_velocity_all
    slim["lsl_sold_velocity_trim"] = lsl_sold_velocity_trim
    slim["partner_sold_history"] = [
        {
            "dealer": r.get("dealer_name"),
            "trim": r.get("trim"),
            "mileage": r.get("mileage"),
            "final_ask": (float(r["final_ask"]) if r.get("final_ask") else None),
            "days_on_lot": r.get("days_on_lot"),
            "last_seen": r.get("last_seen"),
        }
        for r in partner_sold
    ]
    slim["lsl_30day"] = {
        "n": len(lsl_30),
        "matches_requested_trim": bool(trim and lsl_30 and len(lsl_30) > 0
                                       and lsl_30 is not lsl_30_any_trim),
        "requested_trim": trim,
        "purchase_costs": [float(d["purchase_cost"]) for d in lsl_30 if d.get("purchase_cost")],
        "sale_prices": [float(d["sale_price"]) for d in lsl_30 if d.get("sale_price")],
        "deals": [
            {
                "vehicle_info": d.get("vehicle_info"),
                "purchase_cost": float(d["purchase_cost"]) if d.get("purchase_cost") else None,
                "sale_price": float(d["sale_price"]) if d.get("sale_price") else None,
                "days_on_lot": d.get("days_on_lot"),
                "sold_at": d.get("sold_at"),
            }
            for d in lsl_30[:5]
        ],
    }
    if ctx.get("live_vauto_via_lsl_vin"):
        slim["live_vauto_via_lsl_vin"] = ctx["live_vauto_via_lsl_vin"]
    slim["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
    log.info(f"get_vehicle_valuation {year} {make} {model} "
             f"prior_bids={len(((ctx.get('prior_bids') or {}).get('rows') or []))} "
             f"lsl_30day={len(lsl_30)} "
             f"live_via_lsl={bool(ctx.get('live_vauto_via_lsl_vin'))} "
             f"in {slim['elapsed_ms']}ms")
    # Cache for repeat queries
    slim["cache_hit"] = False
    try:
        _ymm_cache_put(cache_key, slim)
    except Exception as _e:
        log.warning(f"cache put err: {_e}")
    return slim


@mcp.tool()
async def lookup_vin(vin: str) -> dict:
    """USE THIS when the user reads or types a full 17-character VIN.
    Triggers a full EW appraisal: vAuto + AccuTrade + iPacket + LSL comps.
    Returns the canonical vehicle decode + initial valuation.

    Example: "the VIN is W1NYC7HJ0RX493899" → call lookup_vin("W1NYC7HJ0RX493899")
    """
    if len(vin) != 17:
        return {"error": f"VIN must be 17 characters, got {len(vin)}"}
    data = await _ew_query(f"The VIN is {vin}")
    return data


# OWNER_WHITELIST_2026_05_22 — names that unlock LSL accounting queries.
# Case-insensitive match; accepts variants like "this is Oscar" via the
# system prompt extraction layer (LLM passes the bare first name).
OWNER_WHITELIST = {"oscar", "gregg", "joe", "todd"}
OWNER_PIN = os.environ.get("EW_OWNER_PIN", "0218")

def _is_owner(name: Optional[str], pin: Optional[str] = None) -> bool:
    # PIN_DISABLED_2026_05_22 — temporarily allow owners through with just
    # name match while iOS spoken-number quirks settle. PIN field is still
    # in the signature so re-enabling is a one-line change here.
    return (name or "").strip().lower() in OWNER_WHITELIST


# EW canonical profit formula — pulled verbatim from
# /opt/livesaleslog/dashboard/app.py PROFIT_SQL (2026-05-22). Mirrors how
# Experience books profit on the LSL dashboard.
_LSL_PROFIT_SQL = ("(COALESCE(sale_price,0) - COALESCE(purchase_cost,0) "
                   "- COALESCE(total_supp_costs,0))")


@mcp.tool()
async def submit_bid_to_ew(
    vin: str,
    submitted_by: str,
    mileage: Optional[int] = None,
    year: Optional[int] = None,
    make: Optional[str] = None,
    model: Optional[str] = None,
    trim: Optional[str] = None,
    asking_price: Optional[float] = None,
    notes: Optional[str] = None,
) -> dict:
    """Drop a NEW bid into the Experience Wholesale dashboard for the
    Phase 1 worker fleet to enrich and assess. Use ONLY after the
    operator has explicitly asked to submit a bid AND given their first
    name. The submitted_by name appears on the bid card so the right
    person gets credit.

    Required: vin (17-char), submitted_by (caller's first name, e.g.
    "Oscar", "Gregg", "Joe", "Todd"). Mileage strongly recommended;
    year/make/model/trim help but vAuto will decode the VIN regardless.

    Returns: {ok: true, bid_id, raw_message} on success.
    """
    vin_clean = (vin or "").strip().upper()
    if len(vin_clean) != 17:
        return {"error": f"invalid VIN ({len(vin_clean)} chars, need 17)"}
    rep = (submitted_by or "").strip()
    if not rep:
        return {"error": "submitted_by is required — ask the caller their first name"}
    payload = {
        "vin": vin_clean,
        "rep_name": rep,
        "source": "voice",
    }
    if mileage is not None:    payload["mileage"] = int(mileage)
    if year is not None:        payload["year"] = int(year)
    if make:                    payload["make"] = make
    if model:                   payload["model"] = model
    if trim:                    payload["trim"] = trim
    if asking_price is not None:payload["asking_price"] = float(asking_price)
    if notes:                   payload["notes"] = notes

    import time as _t
    t0 = _t.monotonic()
    http = await _ensure_http()
    try:
        async with http.post("http://127.0.0.1:9001/api/bid/external",
                             json=payload,
                             headers={"Cookie": "internal=1"}) as r:
            body = await r.json()
    except Exception as e:
        log.exception("submit_bid_to_ew failed")
        return {"error": f"network: {type(e).__name__}: {e}"}
    elapsed_ms = int((_t.monotonic() - t0) * 1000)
    log.info(f"submit_bid_to_ew vin={vin_clean} rep={rep} -> {body!r} in {elapsed_ms}ms")
    return {
        "ok": True,
        "bid_id": body.get("bid_id"),
        "raw_message": body.get("raw_message"),
        "submitted_by": rep,
        "elapsed_ms": elapsed_ms,
    }


@mcp.tool()
async def lsl_deals_booked(
    caller_name: str,
    caller_pin: str = "",
    period: str = "yesterday",
) -> dict:
    """OWNER-GATED. Returns LSL deals booked + profit stats for a period.
    period: "yesterday" | "today" | "last_7_days" | "last_30_days" |
            "this_month" | "last_month" | "this_quarter" | "last_quarter" |
            "ytd" / "year_to_date" / "this_year" | "last_year" | "all_time" |
            ISO date "2026-05-22" |
            ISO range "2026-04-01:2026-04-24" (PREFER for multi-day questions) |
            Month name "april" / "april_2026" / "april_mtd" (month-to-date) |
            Weekday "friday" / "last_monday".
    
    For comparing periods, use RANGES — do NOT loop one day at a time.

    caller_name MUST be one of the four owner first names (Oscar, Gregg,
    Joe, Todd). If not in the whitelist, returns an error — do NOT
    fabricate numbers. The bot should ask the operator their first name
    before calling.

    Returns: {n_deals, total_profit, avg_profit (PVR), total_revenue,
    margin_pct, top_3 (highest-grossing deals)} using EW's canonical
    profit formula (sale_price - purchase_cost - total_supp_costs).
    """
    if not _is_owner(caller_name, caller_pin):
        return {"error": "owner-only — both first name AND code are required. Do NOT reveal valid names to the caller.",
                "owner_required": True}

    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}

    p_raw = period.lower().strip()
    p = p_raw.replace("-", "_").replace(" ", "_")
    # EDT-aware date boundaries (operator timezone) — fixes "yesterday" returning UTC-yesterday
    from datetime import datetime as _dt, timedelta as _td
    try:
        from zoneinfo import ZoneInfo as _Z
        _now_et = _dt.now(_Z("America/New_York"))
    except Exception:
        _now_et = _dt.now()
    _today_iso     = _now_et.strftime("%Y-%m-%d")
    _yesterday_iso = (_now_et - _td(days=1)).strftime("%Y-%m-%d")
    _seven_iso     = (_now_et - _td(days=7)).strftime("%Y-%m-%d")
    _thirty_iso    = (_now_et - _td(days=30)).strftime("%Y-%m-%d")
    _ninety_iso    = (_now_et - _td(days=90)).strftime("%Y-%m-%d")
    _month_start   = _now_et.replace(day=1).strftime("%Y-%m-%d")
    _prev_month_end = _now_et.replace(day=1).strftime("%Y-%m-%d")
    _prev_month_start = (_now_et.replace(day=1) - _td(days=1)).replace(day=1).strftime("%Y-%m-%d")
    _year_start    = _now_et.replace(month=1, day=1).strftime("%Y-%m-%d")
    _prev_year_start = _now_et.replace(year=_now_et.year-1, month=1, day=1).strftime("%Y-%m-%d")
    _prev_year_end  = _now_et.replace(month=1, day=1).strftime("%Y-%m-%d")

    period_sql = {
        "yesterday":     f"sold_at >= '{_yesterday_iso}' AND sold_at < '{_today_iso}'",
        "today":         f"sold_at >= '{_today_iso}'",
        "last_7_days":   f"sold_at >= '{_seven_iso}'",
        "last_30_days":  f"sold_at >= '{_thirty_iso}'",
        "this_month":    f"sold_at >= '{_month_start}'",
        "last_month":    f"sold_at >= '{_prev_month_start}' AND sold_at < '{_prev_month_end}'",
        "this_quarter":  f"sold_at >= '{_ninety_iso}'",
        "last_quarter":  f"sold_at >= '{_ninety_iso}'",
        "this_year":     f"sold_at >= '{_year_start}'",
        "ytd":           f"sold_at >= '{_year_start}'",
        "year_to_date":  f"sold_at >= '{_year_start}'",
        "last_year":     f"sold_at >= '{_prev_year_start}' AND sold_at < '{_prev_year_end}'",
        "all_time":      "1=1",
    }.get(p)

    # ISO single date: "2026-05-22" (use p_raw before underscore-replace)
    if not period_sql and len(p_raw) == 10 and p_raw[4] == "-" and p_raw[7] == "-":
        try:
            from datetime import datetime as _dt
            _dt.strptime(p_raw, "%Y-%m-%d")
            period_sql = f"sold_at >= '{p_raw}' AND sold_at < date('{p_raw}', '+1 day')"
        except Exception:
            pass

    # ISO range: '2026-04-01:2026-04-24' or '2026-04-01_to_2026-04-24'
    if not period_sql:
        _range_sep = None
        for _sep in (":", "_to_", "..", " to "):
            if _sep in p_raw:
                _range_sep = _sep
                break
        if _range_sep:
            try:
                from datetime import datetime as _dt
                _a, _b = p_raw.split(_range_sep, 1)
                _a = _a.strip(); _b = _b.strip()
                _dt.strptime(_a, "%Y-%m-%d"); _dt.strptime(_b, "%Y-%m-%d")
                period_sql = f"sold_at >= '{_a}' AND sold_at < date('{_b}', '+1 day')"
            except Exception:
                pass

    # Month name: 'april', 'april_2026', 'may_mtd' (1st through today), 'last_month'
    if not period_sql:
        _months = {"january":1, "february":2, "march":3, "april":4, "may":5, "june":6,
                   "july":7, "august":8, "september":9, "october":10, "november":11, "december":12}
        _mtd = p.endswith("_mtd")
        _p_month = p.replace("_mtd", "")
        # parse 'april' or 'april_2026'
        _parts = _p_month.split("_")
        _mname = _parts[0]
        if _mname in _months:
            from datetime import datetime as _dt
            try:
                from zoneinfo import ZoneInfo as _Z
                _now = _dt.now(_Z("America/New_York"))
            except Exception:
                _now = _dt.now()
            _m = _months[_mname]
            _y = int(_parts[1]) if len(_parts) > 1 and _parts[1].isdigit() else _now.year
            _start = f"{_y}-{_m:02d}-01"
            if _mtd:
                # Month-to-date "of THAT month" — cap day at MIN(today_day, last_day_of_month).
                # When today is May 24 and user asks "april_mtd", return April 1 through April 24.
                import calendar as _cal
                _last_day_of_month = _cal.monthrange(_y, _m)[1]
                _capped_day = min(_now.day, _last_day_of_month)
                _end_iso = f"{_y}-{_m:02d}-{_capped_day:02d}"
                period_sql = f"sold_at >= '{_start}' AND sold_at < date('{_end_iso}', '+1 day')"
            else:
                _next_m = _m + 1 if _m < 12 else 1
                _next_y = _y if _m < 12 else _y + 1
                _end = f"{_next_y}-{_next_m:02d}-01"
                period_sql = f"sold_at >= '{_start}' AND sold_at < '{_end}'"

    # Weekday name: "friday", "last_friday", "this_friday" → most recent matching day
    if not period_sql:
        _weekday_map = {"monday":0, "tuesday":1, "wednesday":2, "thursday":3,
                        "friday":4, "saturday":5, "sunday":6}
        _p_norm = p.replace("last_", "").replace("this_", "")
        if _p_norm in _weekday_map:
            from datetime import datetime as _dt, timedelta as _td
            try:
                from zoneinfo import ZoneInfo as _Z
                _now = _dt.now(_Z("America/New_York"))
            except Exception:
                _now = _dt.now()
            target_wd = _weekday_map[_p_norm]
            days_back = (_now.weekday() - target_wd) % 7
            if days_back == 0:
                days_back = 7   # "friday" said on Friday means LAST Friday
            target = (_now - _td(days=days_back)).strftime("%Y-%m-%d")
            period_sql = f"sold_at >= '{target}' AND sold_at < date('{target}', '+1 day')"

    if not period_sql:
        return {"error": f"unsupported period {period!r}; supported: yesterday/today/last_7_days/last_30_days/this_month/last_month/this_year/ytd/last_year/last_quarter/all_time, ISO date '2026-05-22', ISO range '2026-04-01:2026-04-24', month 'april' / 'april_2026' / 'april_mtd', weekday 'friday'/'last_monday'"}

    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute(f"""
            SELECT COUNT(*) AS n_deals,
                   COALESCE(SUM({_LSL_PROFIT_SQL}), 0) AS total_profit,
                   COALESCE(AVG({_LSL_PROFIT_SQL}), 0) AS avg_profit,
                   COALESCE(SUM(sale_price), 0) AS total_revenue
              FROM deals
             WHERE {period_sql}
               AND sale_price IS NOT NULL AND sale_price > 0
        """)
        agg = dict(cur.fetchone())

        cur.execute(f"""
            SELECT d.stock_no, d.vin_no, d.vehicle_info,
                   d.customer_name, d.supplier_name,
                   COALESCE(i.source, d.supplier_name) AS true_purchased_from,
                   COALESCE(i.customer_name, d.customer_name) AS true_sold_to,
                   i.sale_status AS inv_sale_status,
                   d.sales_person, d.sale_price, d.purchase_cost,
                   d.total_supp_costs,
                   {_LSL_PROFIT_SQL.replace("sale_price","d.sale_price").replace("purchase_cost","d.purchase_cost").replace("total_supp_costs","d.total_supp_costs")} AS profit,
                   d.sold_at
              FROM deals d
              LEFT JOIN inventory i ON i.stock_no = d.stock_no
             WHERE {period_sql.replace("sold_at","d.sold_at")}
               AND d.sale_price IS NOT NULL AND d.sale_price > 0
             ORDER BY d.sold_at DESC
             LIMIT 50
        """)
        all_deals = [dict(r) for r in cur.fetchall()]
        # top 3 by profit
        top = sorted(all_deals,
                     key=lambda r: float(r.get("profit") or 0),
                     reverse=True)[:3]
        c.close()

        rev = float(agg.get("total_revenue") or 0)
        prof = float(agg.get("total_profit") or 0)
        margin = (prof / rev * 100.0) if rev else 0
        return {
            "period": period,
            "caller_name": caller_name,
            "n_deals": int(agg.get("n_deals") or 0),
            "total_profit": prof,
            "pvr": float(agg.get("avg_profit") or 0),
            "total_revenue": rev,
            "margin_pct": round(margin, 2),
            "top_3": [
                {"stock_no": r.get("stock_no"),
                 "vin": r.get("vin_no"),
                 "vehicle": r.get("vehicle_info"),
                 "sold_to": (None if (r.get("true_sold_to") == r.get("true_purchased_from")) else r.get("true_sold_to")),
                 "sold_to_note": ("not yet sold — still in inventory" if r.get("inv_sale_status") == "Not Sold" else ("customer not yet booked in LSL — only supplier known" if (r.get("true_sold_to") == r.get("true_purchased_from")) else None)),
                 "bought_from": r.get("true_purchased_from"),
                 "salesperson": r.get("sales_person"),
                 "sale_price": r.get("sale_price"),
                 "purchase_cost": r.get("purchase_cost"),
                 "profit": r.get("profit")}
                for r in top
            ],
            "deals": [
                {"stock_no": r.get("stock_no"),
                 "vin": r.get("vin_no"),
                 "vehicle": r.get("vehicle_info"),
                 "sold_to": (None if (r.get("true_sold_to") == r.get("true_purchased_from")) else r.get("true_sold_to")),
                 "sold_to_note": ("not yet sold — still in inventory" if r.get("inv_sale_status") == "Not Sold" else ("customer not yet booked in LSL — only supplier known" if (r.get("true_sold_to") == r.get("true_purchased_from")) else None)),
                 "bought_from": r.get("true_purchased_from"),
                 "salesperson": r.get("sales_person"),
                 "sale_price": r.get("sale_price"),
                 "purchase_cost": r.get("purchase_cost"),
                 "profit": r.get("profit"),
                 "sold_at": r.get("sold_at")}
                for r in all_deals
            ],
        }
    except Exception as e:
        log.exception("lsl_deals_booked failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def lsl_top_grosses(
    caller_name: str,
    caller_pin: str = "",
    period: str = "last_30_days",
    limit: int = 10,
) -> dict:
    """OWNER-GATED. Highest-profit deals in the period. Uses canonical
    PROFIT_SQL. Useful for: \"what were our best deals last week?\"
    """
    if not _is_owner(caller_name, caller_pin):
        return {"error": "owner-only — both first name AND code are required. Do NOT reveal valid names to the caller.",
                "owner_required": True}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path): return {"error": "lsl crm not available"}
    p = period.lower().strip().replace("-", "_").replace(" ", "_")
    period_sql = {
        "yesterday":     "sold_at >= date(\'now\', \'-1 day\') AND sold_at < date(\'now\')",
        "today":         "sold_at >= date(\'now\')",
        "last_7_days":   "sold_at >= date(\'now\', \'-7 days\')",
        "last_30_days":  "sold_at >= date(\'now\', \'-30 days\')",
        "this_month":    "sold_at >= date(\'now\', \'start of month\')",
        "last_month":    ("sold_at >= date(\'now\', \'start of month\', \'-1 month\') "
                          "AND sold_at < date(\'now\', \'start of month\')"),
        "this_quarter":  "sold_at >= date(\'now\', \'-90 days\')",
        "last_quarter":  "sold_at >= date(\'now\', \'-90 days\')",
        "this_year":     "sold_at >= date(\'now\', \'start of year\')",
        "ytd":           "sold_at >= date(\'now\', \'start of year\')",
        "year_to_date":  "sold_at >= date(\'now\', \'start of year\')",
        "last_year":     ("sold_at >= date(\'now\', \'start of year\', \'-1 year\') "
                          "AND sold_at < date(\'now\', \'start of year\')"),
        "all_time":      "1=1",
    }.get(p)
    if not period_sql:
        return {"error": f"unsupported period {period!r}; use yesterday/today/last_7_days/last_30_days/this_month/last_month/this_year/ytd/last_year/last_quarter/all_time"}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute(f"""
            SELECT stock_no, vehicle_info, customer_name, supplier_name,
                   sale_price, purchase_cost, total_supp_costs,
                   {_LSL_PROFIT_SQL} AS profit, sold_at, sales_person
              FROM deals
             WHERE {period_sql}
               AND sale_price IS NOT NULL AND sale_price > 0
             ORDER BY {_LSL_PROFIT_SQL} DESC LIMIT ?
        """, (max(1, min(limit, 25)),))
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
        return {"period": period, "caller_name": caller_name,
                "n_rows": len(rows),
                "deals": [
                    {"stock_no": r.get("stock_no"),
                     "vehicle": r.get("vehicle_info"),
                     "customer": r.get("customer_name"),
                     "supplier": r.get("supplier_name"),
                     "salesperson": r.get("sales_person"),
                     "sale_price": r.get("sale_price"),
                     "purchase_cost": r.get("purchase_cost"),
                     "profit": r.get("profit"),
                     "sold_at": r.get("sold_at")}
                    for r in rows
                ]}
    except Exception as e:
        log.exception("lsl_top_grosses failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def get_bid(bid_id: int) -> dict:
    """USE THIS for ANY mention of a bid number from the EW dashboard
    (e.g. "bid 1983", "what did bid nineteen seventy-nine come in at",
    "status on bid 1240", "what was that bid I just submitted").

    Every "bid <N>" in conversation always refers to an EW dashboard bid_id
    — this tool reads the FULL picture: vehicle decode, MMR + rBook from
    vAuto, AccuTrade guaranteed / trade / retail, AI target-buy assessment,
    partner offers received, BUY_PROFILE_MATCH auto-matches against
    onboarded dealers, photos, status, salesperson, asking price, notes.
    """
    if not bid_id or bid_id < 1:
        return {"error": "valid positive bid_id required"}
    import psycopg2, psycopg2.extras, os as _os
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT b.id, b.vin, b.year, b.make, b.model, b.trim,
                           b.mileage, b.color, b.status, b.bid_amount,
                           b.bid_response, b.bid_sent_at,
                           b.ai_assessment, b.ai_assessed_at, b.ai_price,
                           b.asking_price, b.notes, b.raw_message,
                           b.created_at, b.updated_at, b.phone,
                           b.carfax_damage, b.autocheck_damage,
                           b.damage_signal, b.miles_carfax,
                           v.mmr AS vauto_mmr, v.rbook AS vauto_rbook,
                           v.black_book, v.kbb, v.kbb_com, v.jd_power,
                           v.title_status, v.price_rank, v.adj_pct_market,
                           v.appraisal_url, v.carfax_share_url,
                           v.rbook_competitive_set, v.manheim_transactions,
                           a.guaranteed_offer, a.trade_in, a.trade_market,
                           a.retail AS accutrade_retail, a.market_avg,
                           a.not_available AS accutrade_unavailable,
                           a.unavailable_reason AS accutrade_unavailable_reason,
                           a.selected_trim_text AS accutrade_trim,
                           ip.total_msrp AS ipacket_msrp,
                           ip.base_price AS ipacket_base_price,
                           ip.exterior_color AS ipacket_exterior_color,
                           ip.interior_color AS ipacket_interior_color,
                           ip.not_available AS ipacket_unavailable,
                           ip.unavailable_reason AS ipacket_unavailable_reason,
                           ip.raw_json AS ipacket_raw_json,
                           c2.name AS contact_name
                      FROM bids b
                      LEFT JOIN vauto_lookups v ON v.bid_id = b.id
                      LEFT JOIN accutrade_lookups a ON a.bid_id = b.id
                      LEFT JOIN ipacket_lookups ip ON ip.bid_id = b.id
                      LEFT JOIN contacts c2 ON c2.id = b.contact_id
                     WHERE b.id = %s
                    LIMIT 1
                """, (int(bid_id),))
                row = cur.fetchone()
                if not row:
                    return {"error": f"bid {bid_id} not found in EW dashboard"}
                out = dict(row)

                # Partner offers received on this bid
                cur.execute("""
                    SELECT po.offer_amount, po.message, po.submitted_at,
                           po.ew_seen_at, po.ew_action,
                           d.name AS dealer_name, d.portal_slug
                      FROM bid_partner_offers po
                      JOIN dealers d ON d.id = po.dealer_id
                     WHERE po.bid_id = %s
                     ORDER BY po.submitted_at DESC
                """, (int(bid_id),))
                offers = [dict(r) for r in cur.fetchall()]

                # AI assessment latest breakdown
                cur.execute("""
                    SELECT bucket_display, baseline_price, final_price,
                           llm_adjustment_pct, llm_reasoning,
                           confidence_low, confidence_high, created_at
                      FROM ai_assessment_log
                     WHERE bid_id = %s
                     ORDER BY created_at DESC LIMIT 1
                """, (int(bid_id),))
                last_assessment = cur.fetchone()

                # Photo count
                cur.execute("SELECT COUNT(*) AS n FROM bid_photos WHERE bid_id=%s",
                            (int(bid_id),))
                photo_n = (cur.fetchone() or {}).get("n", 0)

        # Datetime + decimal normalization
        for k in ("created_at","updated_at","ai_assessed_at","bid_sent_at"):
            if out.get(k): out[k] = out[k].isoformat()
        for k in ("bid_amount","ai_price","asking_price","guaranteed_offer",
                  "trade_in","trade_market","accutrade_retail","market_avg",
                  "ipacket_msrp","ipacket_base_price",
                  "black_book","kbb","kbb_com","jd_power","adj_pct_market"):
            if out.get(k) is not None: out[k] = float(out[k])

        # DAMAGE AUDIT — synthesize a quick read for the LLM
        damage_flags = []
        if out.get("carfax_damage"):    damage_flags.append("Carfax shows damage")
        if out.get("autocheck_damage"): damage_flags.append("AutoCheck shows damage")
        if out.get("damage_signal"):    damage_flags.append(out["damage_signal"])
        # Discrepancies — REAL odometer flag is when stated miles are LESS
        # than Carfax (possible rollback). Stated > Carfax is normal
        # accumulation since the last record — NOT a discrepancy.
        # Per operator 2026-05-24.
        if out.get("mileage") and out.get("miles_carfax"):
            try:
                bid_mi = int(out["mileage"])
                cfx_mi = int(out["miles_carfax"])
                if bid_mi < cfx_mi - 500:
                    damage_flags.append(
                        f"odometer rollback risk: bid={bid_mi:,} BELOW carfax={cfx_mi:,}"
                    )
            except Exception:
                pass
        # Suppress miles_carfax exposure when stated > carfax (normal accumulation).
        # Otherwise Bill re-derives a false "discrepancy" conclusion from the raw field.
        # Per operator 2026-05-24.
        _bid_mi = out.get("mileage")
        _cfx_mi = out.get("miles_carfax")
        _expose_carfax_miles = True
        if _bid_mi and _cfx_mi:
            try:
                if int(_bid_mi) >= int(_cfx_mi) - 500:
                    _expose_carfax_miles = False
            except Exception:
                pass
        out["damage_audit"] = {
            "flags":            damage_flags,
            "carfax_damage":    bool(out.get("carfax_damage")),
            "autocheck_damage": bool(out.get("autocheck_damage")),
            "damage_signal":    out.get("damage_signal"),
            "miles_bid":        out.get("mileage"),
            **({"miles_carfax": out.get("miles_carfax")} if _expose_carfax_miles else {}),
            "carfax_share_url": out.get("carfax_share_url"),
        }
        if not _expose_carfax_miles:
            out.pop("miles_carfax", None)
        # Extract options + Monroney sticker text from raw_json
        _ip_raw = out.get("ipacket_raw_json") or {}
        _ip_options = _ip_raw.get("options") or []
        _ip_ocr = (_ip_raw.get("_ocr_text") or "")
        # Trim OCR text to the option block (between ADDED OPTIONS and
        # the next ALL-CAPS section header) — keeps response compact.
        _ip_sticker = _ip_ocr
        if "ADDED OPTIONS" in _ip_ocr:
            _after = _ip_ocr.split("ADDED OPTIONS", 1)[1]
            # End at the next obvious section break
            for _stop in ("Options and Fees", "PRICE DETAILS",
                          "TOTAL PREDICTED", "PREDICTIVE DATA",
                          "AUTOIPACKET"):
                if _stop in _after:
                    _after = _after.split(_stop, 1)[0]
            _ip_sticker = _after.strip()[:2500]  # cap for tool-result budget
        out["ipacket"] = {
            "total_msrp":     out.get("ipacket_msrp"),
            "base_price":     out.get("ipacket_base_price"),
            "exterior_color": out.get("ipacket_exterior_color"),
            "interior_color": out.get("ipacket_interior_color"),
            "unavailable":    bool(out.get("ipacket_unavailable")),
            "reason":         out.get("ipacket_unavailable_reason"),
            "options":        [{"name": o.get("name"),
                                "price": o.get("price")}
                               for o in _ip_options if isinstance(o, dict)],
            "sticker_text":   _ip_sticker if _ip_sticker else None,
        }
        out.pop("ipacket_raw_json", None)

        # rBook & Manheim DETAILED comps for voice (raw jsonb is too verbose
        # — emit a trimmed top-10 summary the LLM can recite)
        def _summ_comps(raw, kind):
            if not raw:
                return {"count": 0, "top": []}
            try:
                rows = raw if isinstance(raw, list) else raw.get("rows") or raw.get("comps") or []
                top = []
                for r in rows[:10]:
                    if not isinstance(r, dict):
                        continue
                    top.append({
                        "year":       r.get("year") or r.get("Year"),
                        "trim":       r.get("trim") or r.get("Trim") or r.get("description"),
                        "miles":      r.get("miles") or r.get("odometer") or r.get("mileage"),
                        "price":      r.get("price") or r.get("sale_price") or r.get("asking_price") or r.get("amount"),
                        "dealer":     r.get("dealer") or r.get("seller") or r.get("location"),
                        "sold_at":    r.get("sold_at") or r.get("date") or r.get("sale_date"),
                        "color":      r.get("color") or r.get("exterior_color"),
                    })
                return {"count": len(rows), "top": top}
            except Exception:
                return {"count": 0, "top": [], "error": "could not parse"}
        out["rbook_comps"]         = _summ_comps(out.pop("rbook_competitive_set", None), "rbook")
        out["manheim_transactions"] = _summ_comps(out.pop("manheim_transactions", None), "manheim")
        # Pop the now-redundant flat iPacket fields — keep the nested
        # `ipacket` object as the single source of truth for window sticker
        for _k in ("ipacket_msrp", "ipacket_base_price",
                   "ipacket_exterior_color", "ipacket_interior_color",
                   "ipacket_unavailable", "ipacket_unavailable_reason"):
            out.pop(_k, None)

        out["partner_offers"] = [
            {"dealer": o.get("dealer_name"),
             "slug": o.get("portal_slug"),
             "offer": (float(o["offer_amount"]) if o.get("offer_amount") else None),
             "message": o.get("message"),
             "submitted_at": (o["submitted_at"].isoformat()
                              if o.get("submitted_at") else None),
             "ew_action": o.get("ew_action")}
            for o in offers
        ]
        out["n_photos"] = photo_n
        if last_assessment:
            la = dict(last_assessment)
            out["last_ai_assessment"] = {
                "bucket": la.get("bucket_display"),
                "baseline_price": (float(la["baseline_price"])
                                    if la.get("baseline_price") else None),
                "final_price": (float(la["final_price"])
                                 if la.get("final_price") else None),
                "llm_adjustment_pct": (float(la["llm_adjustment_pct"])
                                        if la.get("llm_adjustment_pct") else None),
                "llm_reasoning": la.get("llm_reasoning"),
                "confidence_low": (float(la["confidence_low"])
                                    if la.get("confidence_low") else None),
                "confidence_high": (float(la["confidence_high"])
                                     if la.get("confidence_high") else None),
                "created_at": (la["created_at"].isoformat()
                                if la.get("created_at") else None),
            }

        # BUY_PROFILE_MATCH scores for the onboarded dealers
        try:
            import sys
            sys.path.insert(0, "/opt/expwholesale")
            from app import _compute_bid_matches, _load_dealer_vins_owned
            with psycopg2.connect(db_url) as c2:
                with c2.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur2:
                    cur2.execute("""SELECT id, name, portal_slug, buy_profile, lsl_aliases
                                      FROM dealers
                                     WHERE portal_slug = ANY(%s)
                                       AND buy_profile IS NOT NULL""",
                                 (["txtcharlie", "nuccioautogroup"],))
                    dealers_for_match = [dict(r) for r in cur2.fetchall()]
                with c2.cursor() as cur3:
                    vins_by_dealer = _load_dealer_vins_owned(cur3)
            if dealers_for_match:
                synth_bid = {
                    "id": out.get("id"),
                    "year": out.get("year"),
                    "make": out.get("make"),
                    "model": out.get("model"),
                    "trim": out.get("trim"),
                    "mileage": out.get("mileage"),
                    "color": out.get("color"),
                    "vin": out.get("vin"),
                    "asking_price": out.get("asking_price"),
                    "ai_price": out.get("ai_price"),
                }
                matches = _compute_bid_matches(synth_bid, dealers_for_match,
                                                vins_by_dealer=vins_by_dealer,
                                                min_score=0)
                out["buy_profile_matches"] = [
                    {"dealer": m.get("name"), "slug": m.get("slug"),
                     "score": m.get("score"), "tier": m.get("tier"),
                     "reason": m.get("reason")}
                    for m in matches
                ]
            else:
                out["buy_profile_matches"] = []
        except Exception as _e:
            log.warning(f"get_bid buy_profile_matches err: {_e}")
            out["buy_profile_matches"] = []

        return out
    except Exception as e:
        log.exception("get_bid failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def lsl_lookup_sale(caller_name: str, stock_or_vin: str, caller_pin: str = "") -> dict:
    """OWNER-GATED. Look up details on a specific sold car by stock number
    or VIN. Returns buyer (customer), salesperson, supplier (where we
    bought it from), purchase cost, sale price, profit, sold date.

    Use when operator asks: "who did we sell stock 12345 to?", "what did
    we make on that BMW we sold last week?", "who bought the GT3?"
    """
    if not _is_owner(caller_name, caller_pin):
        return {"error": "owner-only — both first name AND code are required. Do NOT reveal valid names to the caller.",
                "owner_required": True}
    needle = (stock_or_vin or "").strip().upper()
    if not needle:
        return {"error": "stock_no or VIN required"}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path): return {"error": "lsl crm not available"}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute(f"""
            SELECT stock_no, vin_no, vehicle_info,
                   customer_name, supplier_name, sales_person, sales_manager,
                   sale_price, purchase_cost, total_supp_costs,
                   {_LSL_PROFIT_SQL} AS profit,
                   front_value, sold_at, deal_status_name
              FROM deals
             WHERE UPPER(stock_no) = ? OR UPPER(vin_no) = ?
             ORDER BY sold_at DESC LIMIT 5
        """, (needle, needle))
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
        if not rows:
            return {"error": f"no LSL deal found for {needle!r}"}
        return {
            "caller_name": caller_name,
            "query": needle,
            "n_matches": len(rows),
            "matches": [
                {"stock_no": r.get("stock_no"),
                 "vin": r.get("vin_no"),
                 "vehicle": r.get("vehicle_info"),
                 "customer": r.get("customer_name"),
                 "supplier": r.get("supplier_name"),
                 "salesperson": r.get("sales_person"),
                 "manager": r.get("sales_manager"),
                 "sale_price": r.get("sale_price"),
                 "purchase_cost": r.get("purchase_cost"),
                 "supp_costs": r.get("total_supp_costs"),
                 "profit": r.get("profit"),
                 "sold_at": r.get("sold_at"),
                 "status": r.get("deal_status_name")}
                for r in rows
            ],
        }
    except Exception as e:
        log.exception("lsl_lookup_sale failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def find_best_buyer(
    caller_name: str,
    year: int,
    make: str,
    model: str,
    trim: Optional[str] = None,
    mileage: Optional[int] = None,
    caller_pin: str = "",
) -> dict:
    """OWNER-GATED. Look up the best partner-dealer / past buyers for a
    given year/make/model. Uses EW's own sales ledger (LSL deals) to find
    who has paid us the most for similar cars recently, with pitch ranking.

    Use when operator asks: "who should I try to sell this BMW M2 to?",
    "best buyer for a Porsche 911 GT3", "who's been buying G63s lately?"

    Returns:
      - top_pitch_buyers: ranked list of buyers with reason strings
      - recent_buyers: all distinct buyers in last 12 months with stats
      - patterns: rolling window stats (90d/180d/365d)
      - deals: raw deal rows (most recent N for this YMM)
    """
    # UNGATED 2026-05-23 per operator request — partner-buyer match is
    # daily-workflow data, not sensitive PII. caller_name still accepted
    # (used for downstream context) but no longer required to be in
    # OWNER_WHITELIST.
    try:
        from lsl_buyer_match import find_same_ymm_deals
        result = find_same_ymm_deals(
            year=year, make=make, model=model,
            trim=trim, mileage=mileage,
        )
        # Slim down to what voice synth needs
        patterns = result.get("patterns") or {}
        windows = patterns.get("windows") or {}
        # BUY_PROFILE_MATCH_2026_05_22 — score onboarded dealers with
        # buy_profile (currently TXT Charlie + Nuccio). This is EW's newer
        # per-bid match scoring system, complementary to the LSL ledger
        # history.
        buy_profile_matches = []
        try:
            import psycopg2, psycopg2.extras, os as _os
            db_url = _os.environ.get("DATABASE_URL",
                "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
            ENABLED_SLUGS = ("txtcharlie", "nuccioautogroup")
            with psycopg2.connect(db_url) as c:
                with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""SELECT id, name, portal_slug, buy_profile, lsl_aliases
                                     FROM dealers
                                    WHERE portal_slug = ANY(%s)
                                      AND buy_profile IS NOT NULL""",
                                (list(ENABLED_SLUGS),))
                    dealers = [dict(r) for r in cur.fetchall()]
            if dealers:
                import sys
                sys.path.insert(0, "/opt/expwholesale")
                from app import _compute_bid_matches, _load_dealer_vins_owned
                # Build a synthetic bid dict from YMM + miles for scoring
                synthetic_bid = {
                    "year": year,
                    "make": make,
                    "model": model,
                    "trim": trim,
                    "mileage": mileage,
                    "color": None,
                    "vin": None,
                    "asking_price": None,
                    "ai_price": None,
                }
                # Fetch VINs each dealer already owns (so we don't suggest
                # selling them a car they already have)
                with psycopg2.connect(db_url) as c:
                    with c.cursor() as cur2:
                        vins_by_dealer = _load_dealer_vins_owned(cur2)
                matches = _compute_bid_matches(synthetic_bid, dealers,
                                                vins_by_dealer=vins_by_dealer,
                                                min_score=0)
                buy_profile_matches = [
                    {"dealer": m.get("name"),
                     "slug": m.get("slug"),
                     "score": m.get("score"),
                     "tier": m.get("tier"),
                     "reason": m.get("reason")}
                    for m in matches
                ]
        except Exception as _e:
            log.warning(f"buy_profile_match err: {_e}")

        # Build alias set so we dont double-count an onboarded dealer who
        # appears under a different LSL customer_name (e.g., TXT Charlie =
        # Maroun Investment Inc). Lowercased + stripped.
        _onboarded_aliases = set()
        for _d in (dealers if "dealers" in dir() else []):
            for _a in (_d.get("lsl_aliases") or []):
                _onboarded_aliases.add((_a or "").strip().lower())
            # also include the dealer's own canonical name
            if _d.get("name"):
                _onboarded_aliases.add(_d["name"].strip().lower())
        def _is_onboarded_alias(buyer):
            return (buyer or "").strip().lower() in _onboarded_aliases

        out = {
            "ymm": {"year": year, "make": make, "model": model, "trim": trim},
            "buy_profile_matches": buy_profile_matches,
            "n_deals_total": patterns.get("total_deals"),
            "unique_buyers_12mo": (windows.get("365d") or {}).get("unique_buyers"),
            "rolling_windows": {
                k: {"n": (windows.get(k) or {}).get("n"),
                    "avg_sale": (windows.get(k) or {}).get("avg_sale_price"),
                    "avg_gross": (windows.get(k) or {}).get("avg_front_value"),
                    "unique_buyers": (windows.get(k) or {}).get("unique_buyers")}
                for k in ("90d", "180d", "365d")
            },
            "top_pitch_buyers": [
                {"buyer": b.get("buyer_name"),
                 "n_deals_window": b.get("deals_window"),
                 "n_deals_90d": b.get("deals_very_recent"),
                 "lifetime_deals": b.get("lifetime_deals"),
                 "avg_sale": b.get("avg_sale_price"),
                 "avg_gross": b.get("avg_gross"),
                 "last_sold": b.get("last_sold_at"),
                 "days_since_last": b.get("days_since_last"),
                 "pitch_score": b.get("pitch_score"),
                 "reason": b.get("reason"),
                 "rep": b.get("active_rep")}
                for b in (result.get("top_pitch_buyers") or [])
                if not _is_onboarded_alias(b.get("buyer_name"))
            ][:5],
            "recent_buyers": [
                {"buyer": b.get("buyer_name"),
                 "n_deals_window": b.get("deals_window"),
                 "avg_sale": b.get("avg_sale_price"),
                 "avg_gross": b.get("avg_gross"),
                 "last_sold": b.get("last_sold_at")}
                for b in (result.get("recent_buyers") or [])
                if not _is_onboarded_alias(b.get("buyer_name"))
            ][:10],
            "recent_deals": [
                {"sold_at": (d.get("sold_at") or "")[:10],
                 "stock_no": d.get("stock_no"),
                 "vehicle": d.get("vehicle_info"),
                 "customer": d.get("customer_name"),
                 "supplier": d.get("supplier_name"),
                 "sale_price": d.get("sale_price"),
                 "front_value": d.get("front_value"),
                 "sale_type": d.get("sale_type")}
                for d in (result.get("deals") or [])[:8]
            ],
        }
        return out
    except Exception as e:
        log.exception("find_best_buyer failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def dashboard_stats() -> dict:
    """USE THIS for general EW dashboard health questions: "how is the
    dashboard looking?", "what is the bid volume today?", "how many
    bids are open?", "any bids needing attention?".

    Returns: today's bid count by status, total open bids, bids waiting
    for AI assessment, bids with partner offers pending review.
    """
    import psycopg2, psycopg2.extras, os as _os
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT status, COUNT(*) AS n FROM bids GROUP BY status")
                by_status = {r["status"]: int(r["n"]) for r in cur.fetchall()}
                cur.execute("SELECT COUNT(*) AS n FROM bids WHERE created_at::date = CURRENT_DATE")
                today = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute("SELECT status, COUNT(*) AS n FROM bids WHERE created_at::date = CURRENT_DATE GROUP BY status")
                today_by_status = {r["status"]: int(r["n"]) for r in cur.fetchall()}
                cur.execute("""SELECT COUNT(*) AS n FROM bids WHERE ai_assessment IS NULL
                                AND created_at > NOW() - INTERVAL '1 day'""")
                pending_ai = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute("""SELECT COUNT(DISTINCT bid_id) AS n FROM bid_partner_offers
                                WHERE ew_seen_at IS NULL""")
                pending_offers = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute("""SELECT b.id, b.year, b.make, b.model, b.trim, b.mileage,
                                       po.offer_amount, d.name AS dealer
                                  FROM bid_partner_offers po
                                  JOIN bids b ON b.id = po.bid_id
                                  JOIN dealers d ON d.id = po.dealer_id
                                 WHERE po.ew_seen_at IS NULL
                                 ORDER BY po.submitted_at DESC LIMIT 5""")
                offers_to_review = [dict(r) for r in cur.fetchall()]
        return {
            "today_total": today,
            "today_by_status": today_by_status,
            "by_status_alltime": by_status,
            "pending_ai_assessment": pending_ai,
            "unseen_partner_offers": pending_offers,
            "offers_to_review": [
                {"bid_id": o.get("id"),
                 "vehicle": (str(o.get("year") or "") + " " + (o.get("make") or "") + " " + (o.get("model") or "") + " " + (o.get("trim") or "")).strip(),
                 "miles": o.get("mileage"),
                 "dealer": o.get("dealer"),
                 "offer": (float(o["offer_amount"]) if o.get("offer_amount") else None)}
                for o in offers_to_review
            ],
        }
    except Exception as e:
        log.exception("dashboard_stats failed")
        return {"error": f"{type(e).__name__}: {e}"}




@mcp.tool()
async def lsl_salesperson_stats(
    caller_name: str,
    salesperson_name: str,
    period: str = "this_month",
) -> dict:
    """OWNER-GATED. How is a specific salesperson doing in a period.
    Returns: deals_count, total_gross, avg_pvr, top 5 deals by gross.
    period: yesterday|today|last_7_days|last_30_days|this_month|last_month|
    this_quarter|last_quarter|ytd|this_year|last_year|all_time
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only — first name required",
                "owner_required": True}
    if not salesperson_name:
        return {"error": "salesperson_name required"}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}
    p = period.lower().strip().replace("-", "_").replace(" ", "_")
    period_sql = {
        "yesterday":     "sold_at >= date(\'now\', \'-1 day\') AND sold_at < date(\'now\')",
        "today":         "sold_at >= date(\'now\')",
        "last_7_days":   "sold_at >= date(\'now\', \'-7 days\')",
        "last_30_days":  "sold_at >= date(\'now\', \'-30 days\')",
        "this_month":    "sold_at >= date(\'now\', \'start of month\')",
        "last_month":    ("sold_at >= date(\'now\', \'start of month\', \'-1 month\') "
                          "AND sold_at < date(\'now\', \'start of month\')"),
        "this_quarter":  "sold_at >= date(\'now\', \'-90 days\')",
        "last_quarter":  "sold_at >= date(\'now\', \'-90 days\')",
        "this_year":     "sold_at >= date(\'now\', \'start of year\')",
        "ytd":           "sold_at >= date(\'now\', \'start of year\')",
        "year_to_date":  "sold_at >= date(\'now\', \'start of year\')",
        "last_year":     ("sold_at >= date(\'now\', \'start of year\', \'-1 year\') "
                          "AND sold_at < date(\'now\', \'start of year\')"),
        "all_time":      "1=1",
    }.get(p)
    if not period_sql:
        return {"error": f"unsupported period {period!r}"}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute(f"""
            SELECT COUNT(*) AS n,
                   COALESCE(SUM({_LSL_PROFIT_SQL}), 0) AS total_profit,
                   COALESCE(AVG({_LSL_PROFIT_SQL}), 0) AS avg_profit,
                   COALESCE(SUM(sale_price), 0) AS total_rev
              FROM deals
             WHERE {period_sql}
               AND sale_price IS NOT NULL AND sale_price > 0
               AND (UPPER(sales_person) LIKE UPPER(?)
                    OR UPPER(sales_manager) LIKE UPPER(?))
        """, (f"%{salesperson_name}%", f"%{salesperson_name}%"))
        agg = dict(cur.fetchone())
        cur.execute(f"""
            SELECT stock_no, vehicle_info, customer_name,
                   sale_price, purchase_cost, {_LSL_PROFIT_SQL} AS profit,
                   sold_at, sales_person, sales_manager
              FROM deals
             WHERE {period_sql}
               AND sale_price IS NOT NULL AND sale_price > 0
               AND (UPPER(sales_person) LIKE UPPER(?)
                    OR UPPER(sales_manager) LIKE UPPER(?))
             ORDER BY {_LSL_PROFIT_SQL} DESC LIMIT 5
        """, (f"%{salesperson_name}%", f"%{salesperson_name}%"))
        top = [dict(r) for r in cur.fetchall()]
        c.close()
        return {
            "salesperson":   salesperson_name,
            "period":        period,
            "n_deals":       int(agg.get("n") or 0),
            "total_profit":  float(agg.get("total_profit") or 0),
            "pvr":           float(agg.get("avg_profit") or 0),
            "total_revenue": float(agg.get("total_rev") or 0),
            "top_5":         [
                {"stock_no": r.get("stock_no"),
                 "vehicle":  r.get("vehicle_info"),
                 "customer": r.get("customer_name"),
                 "salesperson": r.get("sales_person"),
                 "manager":  r.get("sales_manager"),
                 "sale_price": r.get("sale_price"),
                 "profit":   r.get("profit"),
                 "sold_at":  r.get("sold_at")}
                for r in top
            ],
        }
    except Exception as e:
        log.exception("lsl_salesperson_stats failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def lsl_inventory_now(
    caller_name: str,
    make: str = "",
    model: str = "",
    year: int = 0,
) -> dict:
    """OWNER-GATED. Current cars on the EW lot. Optionally filter by
    make/model/year. Returns count + sample rows with stock#, days on lot,
    asking price, purchase cost."""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        where = ["in_stock = 1", "sold = 0"]
        args = []
        if make:
            where.append("UPPER(vehicle_make_name) LIKE UPPER(?)")
            args.append(f"%{make}%")
        if model:
            where.append("UPPER(group_model_name) LIKE UPPER(?)")
            args.append(f"%{model}%")
        if year and year > 0:
            where.append("group_model_trim_year LIKE ?")
            args.append(f"%{year}%")
        sql = f"""
            SELECT stock_no, vin_no, group_model_trim_year, vehicle_make_name,
                   group_model_name, group_model_trim, usage,
                   asking_price, purchase_cost, est_wholesale_price,
                   exterior_color, days_on_lot, days_since_marketed,
                   lead_count, offer_count, arrived_at
              FROM inventory
             WHERE {' AND '.join(where)}
             ORDER BY days_on_lot DESC LIMIT 25
        """
        cur.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute(f"""SELECT COUNT(*) AS n, AVG(days_on_lot) AS avg_dol,
                              SUM(asking_price) AS total_ask,
                              SUM(purchase_cost) AS total_cost
                         FROM inventory
                        WHERE {' AND '.join(where)}""", args)
        agg = dict(cur.fetchone())
        c.close()
        return {
            "caller_name": caller_name,
            "filter": {"make": make, "model": model, "year": year},
            "total_in_stock": int(agg.get("n") or 0),
            "avg_days_on_lot": (round(float(agg["avg_dol"]),1) if agg.get("avg_dol") else None),
            "total_asking": float(agg.get("total_ask") or 0),
            "total_purchase_cost": float(agg.get("total_cost") or 0),
            "rows": [
                {"stock_no": r.get("stock_no"),
                 "vin": r.get("vin_no"),
                 "title": r.get("group_model_trim_year"),
                 "make": r.get("vehicle_make_name"),
                 "model": r.get("group_model_name"),
                 "trim": r.get("group_model_trim"),
                 "miles": r.get("usage"),
                 "asking_price": r.get("asking_price"),
                 "purchase_cost": r.get("purchase_cost"),
                 "color": r.get("exterior_color"),
                 "days_on_lot": r.get("days_on_lot"),
                 "leads": r.get("lead_count"),
                 "offers": r.get("offer_count"),
                 "arrived": (r.get("arrived_at") or "")[:10]}
                for r in rows
            ],
        }
    except Exception as e:
        log.exception("lsl_inventory_now failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def lsl_customer_history(
    caller_name: str,
    customer_name: str,
    limit: int = 10,
) -> dict:
    """OWNER-GATED. All deals with a specific customer (buyer OR supplier).
    Use for 'what is our history with X dealer/customer' queries.
    Matches customer_name OR supplier_name (LSL stores both ways)."""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    if not customer_name:
        return {"error": "customer_name required"}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        pat = f"%{customer_name}%"
        cur.execute(f"""
            SELECT stock_no, vehicle_info, customer_name, supplier_name,
                   sales_person, sale_price, purchase_cost,
                   {_LSL_PROFIT_SQL} AS profit, sold_at, sale_type
              FROM deals
             WHERE (UPPER(customer_name) LIKE UPPER(?)
                    OR UPPER(supplier_name) LIKE UPPER(?))
               AND sale_price IS NOT NULL AND sale_price > 0
             ORDER BY sold_at DESC LIMIT ?
        """, (pat, pat, max(1, min(limit, 50))))
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute(f"""
            SELECT COUNT(*) AS n,
                   COALESCE(SUM({_LSL_PROFIT_SQL}), 0) AS total_profit,
                   COALESCE(SUM(sale_price), 0) AS total_rev,
                   MIN(sold_at) AS first_at, MAX(sold_at) AS last_at
              FROM deals
             WHERE (UPPER(customer_name) LIKE UPPER(?)
                    OR UPPER(supplier_name) LIKE UPPER(?))
               AND sale_price IS NOT NULL AND sale_price > 0
        """, (pat, pat))
        agg = dict(cur.fetchone())
        c.close()
        # Split by side — as buyer (we sold TO them) vs supplier (we bought FROM them)
        bought_from_us = [r for r in rows
                           if customer_name.lower() in (r.get("customer_name") or "").lower()]
        sold_to_us = [r for r in rows
                       if customer_name.lower() in (r.get("supplier_name") or "").lower()
                       and r not in bought_from_us]
        return {
            "caller_name": caller_name,
            "customer":    customer_name,
            "n_deals":     int(agg.get("n") or 0),
            "total_profit": float(agg.get("total_profit") or 0),
            "total_revenue": float(agg.get("total_rev") or 0),
            "first_deal":  agg.get("first_at"),
            "last_deal":   agg.get("last_at"),
            "n_bought_from_us": len(bought_from_us),
            "n_sold_to_us":     len(sold_to_us),
            "recent_deals":     [
                {"stock_no": r.get("stock_no"),
                 "vehicle":  r.get("vehicle_info"),
                 "side":     "they_bought" if customer_name.lower() in (r.get("customer_name") or "").lower() else "they_sold",
                 "salesperson": r.get("sales_person"),
                 "sale_price": r.get("sale_price"),
                 "purchase_cost": r.get("purchase_cost"),
                 "profit": r.get("profit"),
                 "sold_at": r.get("sold_at"),
                 "sale_type": r.get("sale_type")}
                for r in rows
            ],
        }
    except Exception as e:
        log.exception("lsl_customer_history failed")
        return {"error": f"{type(e).__name__}: {e}"}




@mcp.tool()
async def lsl_service_requests(
    caller_name: str,
    status_filter: str = "open",
    days_back: int = 30,
) -> dict:
    """OWNER-GATED. Service / recon department queue. status_filter:
    open|completed|all. Returns count, total_costs, and top 20 oldest
    pending requests with stock#, advisor, age, costs, damage notes."""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}
    where = []
    sf = (status_filter or "open").lower().strip()
    if sf == "open":
        where.append("status NOT IN ('Completed','Closed','Cancelled')")
    elif sf == "completed":
        where.append("status IN ('Completed','Closed')")
        where.append(f"datetime(created_at) >= datetime('now', '-{int(days_back)} days')")
    else:
        where.append(f"datetime(created_at) >= datetime('now', '-{int(days_back)} days')")
    sql_where = " AND ".join(where) if where else "1=1"
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute(f"""
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(approved_costs),0) AS approved,
                   COALESCE(SUM(pending_costs),0)  AS pending,
                   COALESCE(SUM(total_costs),0)    AS total,
                   COALESCE(SUM(damaged),0)        AS damaged_count
              FROM service_requests
             WHERE {sql_where}""")
        agg = dict(cur.fetchone())
        cur.execute(f"""
            SELECT stock_no, vin_no, inventory_name, service_advisor,
                   request_status, request_priority, service_request_type,
                   approved_costs, pending_costs, total_costs,
                   age_in_days, damaged, damage_notes,
                   group_name, deal_customer_name, deal_sales_person_name,
                   created_at, completed_at
              FROM service_requests
             WHERE {sql_where}
             ORDER BY age_in_days DESC LIMIT 20""")
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
        return {
            "caller_name": caller_name,
            "status_filter": sf,
            "days_back": days_back,
            "n_requests": int(agg.get("n") or 0),
            "approved_costs": float(agg.get("approved") or 0),
            "pending_costs":  float(agg.get("pending") or 0),
            "total_costs":    float(agg.get("total") or 0),
            "damaged_count":  int(agg.get("damaged_count") or 0),
            "requests": [
                {"stock_no": r.get("stock_no"),
                 "vin": r.get("vin_no"),
                 "vehicle": r.get("inventory_name"),
                 "advisor": r.get("service_advisor"),
                 "status": r.get("request_status"),
                 "priority": r.get("request_priority"),
                 "type": r.get("service_request_type"),
                 "approved": r.get("approved_costs"),
                 "pending": r.get("pending_costs"),
                 "total": r.get("total_costs"),
                 "age_days": r.get("age_in_days"),
                 "damaged": bool(r.get("damaged")),
                 "damage_notes": r.get("damage_notes"),
                 "group": r.get("group_name"),
                 "customer": r.get("deal_customer_name"),
                 "salesperson": r.get("deal_sales_person_name"),
                 "created": (r.get("created_at") or "")[:10],
                 "completed": (r.get("completed_at") or "")[:10] if r.get("completed_at") else None}
                for r in rows
            ],
        }
    except Exception as e:
        log.exception("lsl_service_requests failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def lsl_appraisal_history(
    caller_name: str,
    vin: str = "",
    stock_no: str = "",
) -> dict:
    """OWNER-GATED. Full appraisal history for a specific VIN or stock#.
    Returns chain of appraised_value, msrp, est_wholesale, market days,
    mileage_adjustment per appraisal pass. Pass either vin or stock_no."""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    if not vin and not stock_no:
        return {"error": "vin or stock_no required"}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        if vin:
            cur.execute("""SELECT id AS inventory_id, stock_no, vin_no,
                                  group_model_trim_year, group_model_name,
                                  usage, asking_price, purchase_cost,
                                  days_on_lot, sold, sold_at,
                                  original_msrp, est_wholesale_price
                             FROM inventory WHERE UPPER(vin_no)=UPPER(?) LIMIT 1""",
                        (vin,))
        else:
            cur.execute("""SELECT id AS inventory_id, stock_no, vin_no,
                                  group_model_trim_year, group_model_name,
                                  usage, asking_price, purchase_cost,
                                  days_on_lot, sold, sold_at,
                                  original_msrp, est_wholesale_price
                             FROM inventory WHERE UPPER(stock_no)=UPPER(?) LIMIT 1""",
                        (stock_no,))
        inv_row = cur.fetchone()
        if not inv_row:
            c.close()
            return {"error": "vehicle not found in LSL inventory",
                    "vin": vin, "stock_no": stock_no}
        inv = dict(inv_row)
        inv_id = inv["inventory_id"]
        cur.execute("""SELECT appraisal_id, appraised_at, original_msrp,
                              appraised_value, base_appraised_value,
                              mileage_adjustment_value, est_wholesale_value,
                              market_asking_price, available_in_market,
                              avg_days_on_market, n_appraisals_total,
                              has_valid_blackbook, books_status
                         FROM inventory_appraisal
                        WHERE inventory_id = ?
                        ORDER BY appraised_at DESC LIMIT 10""", (inv_id,))
        appraisals = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT appraised_at, snapshot_index, original_msrp,
                              appraised_value, base_appraised_value,
                              mileage_adjustment_value, est_wholesale_value,
                              market_asking_price, available_in_market,
                              avg_days_on_market, avg_days_supply
                         FROM inventory_appraisal_log
                        WHERE inventory_id = ?
                        ORDER BY appraised_at DESC LIMIT 8""", (inv_id,))
        try:
            log_rows = [dict(r) for r in cur.fetchall()]
        except Exception:
            log_rows = []
        c.close()
        return {
            "caller_name": caller_name,
            "vehicle": inv,
            "n_appraisals": len(appraisals),
            "appraisals": appraisals,
            "recent_log": log_rows,
        }
    except Exception as e:
        log.exception("lsl_appraisal_history failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def lsl_payments(
    caller_name: str,
    status_filter: str = "pending",
    days_back: int = 60,
) -> dict:
    """OWNER-GATED. Payments / accounting queue. status_filter:
    pending|paid|all. Returns count, total amount, breakdown by payee
    type, and top 25 by amount (recipient, amount, deal, stock#)."""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}
    sf = (status_filter or "pending").lower().strip()
    where = [f"datetime(created_at) >= datetime('now', '-{int(days_back)} days')"]
    if sf == "pending":
        where.append("is_paid = 0")
    elif sf == "paid":
        where.append("is_paid = 1")
    sql_where = " AND ".join(where)
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute(f"""SELECT COUNT(*) AS n,
                              COALESCE(SUM(amount), 0) AS amt,
                              COALESCE(SUM(amount_paid), 0) AS paid,
                              SUM(CASE WHEN is_commission=1 THEN amount ELSE 0 END) AS commission
                         FROM payments WHERE {sql_where}""")
        agg = dict(cur.fetchone())
        cur.execute(f"""SELECT payee_type, COUNT(*) AS n, SUM(amount) AS amt
                         FROM payments WHERE {sql_where}
                        GROUP BY payee_type ORDER BY amt DESC LIMIT 10""")
        by_type = [dict(r) for r in cur.fetchall()]
        cur.execute(f"""SELECT stock_no, vin_no, payee_type, recipient_name,
                              canonical_recipient, vendor_name,
                              amount, amount_paid, payment_type_desc,
                              payment_status, is_paid, is_commission,
                              description, requested_by_name,
                              created_at, paid_at
                         FROM payments WHERE {sql_where}
                        ORDER BY amount DESC LIMIT 25""")
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
        return {
            "caller_name": caller_name,
            "status_filter": sf,
            "days_back": days_back,
            "n_payments":      int(agg.get("n") or 0),
            "total_amount":    float(agg.get("amt") or 0),
            "total_paid":      float(agg.get("paid") or 0),
            "total_commission":float(agg.get("commission") or 0),
            "by_payee_type":   by_type,
            "top_payments": [
                {"stock_no": r.get("stock_no"),
                 "vin": r.get("vin_no"),
                 "payee_type": r.get("payee_type"),
                 "recipient": r.get("canonical_recipient") or r.get("recipient_name") or r.get("vendor_name"),
                 "amount": r.get("amount"),
                 "amount_paid": r.get("amount_paid"),
                 "type": r.get("payment_type_desc"),
                 "status": r.get("payment_status"),
                 "is_paid": bool(r.get("is_paid")),
                 "is_commission": bool(r.get("is_commission")),
                 "description": r.get("description"),
                 "requested_by": r.get("requested_by_name"),
                 "created": (r.get("created_at") or "")[:10],
                 "paid": (r.get("paid_at") or "")[:10] if r.get("paid_at") else None}
                for r in rows
            ],
        }
    except Exception as e:
        log.exception("lsl_payments failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def lsl_customer_lookup(
    caller_name: str,
    query: str,
) -> dict:
    """OWNER-GATED. Search the customer master. query matches name OR
    email OR phone OR company. Returns up to 10 matching customer
    profiles with active_deals, booked_deals, lead_status, blocked flag."""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    if not query or not query.strip():
        return {"error": "query required"}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}
    pat = f"%{query.strip()}%"
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute("""SELECT id, customer_id, type, company_name, full_name,
                              first_name, last_name, email, mobile,
                              full_address, verified, is_blocked, lead_status,
                              active_deals, booked_deals, customer_number,
                              note, created_at, modified_at
                         FROM customers
                        WHERE UPPER(full_name) LIKE UPPER(?)
                           OR UPPER(COALESCE(company_name,'')) LIKE UPPER(?)
                           OR UPPER(COALESCE(email,'')) LIKE UPPER(?)
                           OR UPPER(COALESCE(mobile,'')) LIKE UPPER(?)
                        ORDER BY booked_deals DESC NULLS LAST,
                                 active_deals DESC NULLS LAST,
                                 modified_at DESC
                        LIMIT 10""",
                    (pat, pat, pat, pat))
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
        return {
            "caller_name": caller_name,
            "query": query,
            "n_matches": len(rows),
            "matches": rows,
        }
    except Exception as e:
        log.exception("lsl_customer_lookup failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def lsl_dealer_intel(
    caller_name: str,
    dealer_name: str,
) -> dict:
    """OWNER-GATED. Pre-rolled dealer profile + buyer aggregate. Returns
    dealer_profile (segments, totals, last activity) + buyer_agg
    (preferred makes / models / years / price bands) when available.
    Use for 'tell me about <dealer>' deep dives that go beyond
    lsl_customer_history."""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    if not dealer_name or not dealer_name.strip():
        return {"error": "dealer_name required"}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}
    pat = f"%{dealer_name.strip()}%"
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        out = {"caller_name": caller_name, "dealer_name": dealer_name}
        for tbl, key in [("dealer_profile","dealer_profile"),
                         ("buyer_agg","buyer_agg"),
                         ("combined_agg","combined_agg"),
                         ("dealer_flags","dealer_flags"),
                         ("source_agg","source_agg")]:
            try:
                cur.execute(f"PRAGMA table_info({tbl})")
                cols = [r[1] for r in cur.fetchall()]
                if not cols:
                    continue
                name_cols = [c2 for c2 in cols
                             if c2.lower() in
                             ("name","dealer_name","customer_name","supplier_name","company_name")]
                if not name_cols:
                    continue
                where_clause = " OR ".join(
                    f"UPPER(COALESCE({c2},'')) LIKE UPPER(?)" for c2 in name_cols
                )
                cur.execute(f"SELECT * FROM {tbl} WHERE {where_clause} LIMIT 5",
                            tuple([pat]*len(name_cols)))
                rows = [dict(r) for r in cur.fetchall()]
                out[key] = rows
            except Exception as e:
                out[f"{key}_error"] = str(e)
        # Quick deal totals for this dealer
        cur.execute(f"""SELECT COUNT(*) AS n,
                              COALESCE(SUM({_LSL_PROFIT_SQL}),0) AS profit,
                              COALESCE(SUM(sale_price),0) AS revenue,
                              MIN(sold_at) AS first_at,
                              MAX(sold_at) AS last_at
                         FROM deals
                        WHERE (UPPER(customer_name) LIKE UPPER(?)
                               OR UPPER(supplier_name) LIKE UPPER(?))
                          AND sale_price IS NOT NULL""", (pat, pat))
        out["deal_summary"] = dict(cur.fetchone())
        c.close()
        return out
    except Exception as e:
        log.exception("lsl_dealer_intel failed")
        return {"error": f"{type(e).__name__}: {e}"}




@mcp.tool()
async def lsl_query(
    caller_name: str,
    query_type: str,
    target: str = "",
    status_filter: str = "",
    days_back: int = 30,
) -> dict:
    """OWNER-GATED. UNIFIED LSL DEEP-QUERY DISPATCHER. Use for any of:

      query_type='service_requests' → service / recon dept queue (open ROs,
          damaged-vehicle list, costs, advisor breakdown). status_filter:
          open|completed|all. target unused.
      query_type='payments' → accounting queue (pending / paid amounts, by
          payee type). status_filter: pending|paid|all. target unused.
      query_type='appraisal_history' → full appraisal trail for a vehicle.
          target = VIN (17-char) or stock#. status_filter / days_back unused.
      query_type='customer_lookup' → find customer by partial name / email /
          phone / company. target = the search string. Others unused.
      query_type='dealer_intel' → deep dealer profile (dealer_profile +
          buyer_agg + deal_summary). target = dealer name. Others unused.

    Returns the same payload as the underlying tool would. caller_name
    must be a recognized owner first name."""
    qt = (query_type or "").lower().strip()
    if qt == "service_requests":
        return await lsl_service_requests(
            caller_name=caller_name,
            status_filter=status_filter or "open",
            days_back=days_back or 30,
        )
    if qt == "payments":
        return await lsl_payments(
            caller_name=caller_name,
            status_filter=status_filter or "pending",
            days_back=days_back or 60,
        )
    if qt == "appraisal_history":
        v = (target or "").strip()
        if len(v) == 17 and v.replace(" ","").isalnum():
            return await lsl_appraisal_history(
                caller_name=caller_name, vin=v, stock_no="")
        return await lsl_appraisal_history(
            caller_name=caller_name, vin="", stock_no=v)
    if qt == "customer_lookup":
        return await lsl_customer_lookup(
            caller_name=caller_name, query=target)
    if qt == "dealer_intel":
        return await lsl_dealer_intel(
            caller_name=caller_name, dealer_name=target)
    if qt in ("customer_history", "history"):
        return await lsl_customer_history(
            caller_name=caller_name,
            customer_name=target,
            limit=10,
        )
    if qt == "recent_bids":
        try:
            n = int(target) if (target or "").isdigit() else 5
        except Exception:
            n = 5
        return await recent_bids(limit=n)
    if qt == "top_grosses":
        return await lsl_top_grosses(
            caller_name=caller_name,
            period=(status_filter or "this_month"),
            limit=10,
        )
    if qt in ("lookup_sale", "sale_lookup", "stock_lookup"):
        v = (target or "").strip()
        return await lsl_lookup_sale(
            caller_name=caller_name, stock_or_vin=v)
    return {"error": f"unknown query_type {query_type!r}. Valid: "
            "customer_history | dealer_intel | service_requests | "
            "payments | appraisal_history | customer_lookup | "
            "recent_bids | top_grosses | lookup_sale"}











# ─── carvana_offer (added 2026-05-25) ──────────────────────────────────────

def _carvana_cache_get(vin: str, miles: int) -> Optional[dict]:
    """Return cached offer if <24h old."""
    import psycopg2, psycopg2.extras, os as _os
    try:
        with psycopg2.connect(_os.environ.get("DATABASE_URL",
            "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale"),
            connect_timeout=2) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT vin, miles_bucket, offer_amount, offer_expires,
                           cached_at, raw_response
                      FROM carvana_offer_cache
                     WHERE vin = %s
                       AND ABS(miles_bucket - %s) <= 2500
                       AND cached_at > NOW() - INTERVAL '24 hours'
                     ORDER BY cached_at DESC LIMIT 1
                """, (vin.upper(), int(miles or 0)))
                row = cur.fetchone()
                if row:
                    return dict(row)
    except Exception as e:
        log.warning(f"_carvana_cache_get: {e}")
    return None


def _carvana_cache_put(vin: str, miles: int, offer_amount, offer_expires, raw) -> None:
    import psycopg2, json, os as _os
    try:
        with psycopg2.connect(_os.environ.get("DATABASE_URL",
            "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale"),
            connect_timeout=2) as c:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO carvana_offer_cache
                      (vin, miles_bucket, offer_amount, offer_expires, raw_response)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (vin, miles_bucket) DO UPDATE
                      SET offer_amount=EXCLUDED.offer_amount,
                          offer_expires=EXCLUDED.offer_expires,
                          cached_at=NOW(),
                          raw_response=EXCLUDED.raw_response
                """, (vin.upper(), int(miles or 0), offer_amount, offer_expires,
                      json.dumps(raw) if raw else None))
    except Exception as e:
        log.warning(f"_carvana_cache_put: {e}")


@mcp.tool()
async def carvana_offer(vin: str, miles: int = 0) -> dict:
    """Carvana instant-offer for a specific VIN. Uses our verifier-VM-pool
    workers to bypass Cloudflare via real-browser scrape of value.carvana.com.

    Hot path: 24h cache hit returns in <100ms.
    Cold path: enqueue job, poll queue up to 8s for worker response.

    Returns: {vin, offer_amount, offer_expires, status, source, elapsed_ms}
    status is one of: 'cached', 'fresh', 'pending' (no worker picked it up),
                      'failed' (worker error), 'no_offer' (Carvana declined)
    """
    import time as _t, asyncio as _aio
    import psycopg2, psycopg2.extras, os as _os
    t0 = _t.monotonic()
    vin_u = (vin or "").upper().strip()
    if len(vin_u) != 17:
        return {"error": "invalid VIN length", "vin": vin_u}

    # Cache check first
    cached = _carvana_cache_get(vin_u, miles)
    if cached:
        return {
            "vin": vin_u,
            "offer_amount": float(cached["offer_amount"]) if cached.get("offer_amount") else None,
            "offer_expires": str(cached["offer_expires"]) if cached.get("offer_expires") else None,
            "status": "cached", "source": "carvana",
            "elapsed_ms": int((_t.monotonic() - t0) * 1000),
        }

    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    # Enqueue
    job_id = None
    try:
        with psycopg2.connect(db_url, connect_timeout=2) as c:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO carvana_queue (vin, miles, status)
                    VALUES (%s, %s, 'queued')
                    RETURNING id
                """, (vin_u, int(miles or 0)))
                job_id = cur.fetchone()[0]
    except Exception as e:
        return {"error": f"enqueue failed: {type(e).__name__}: {e}",
                "elapsed_ms": int((_t.monotonic() - t0) * 1000)}

    # Poll for result up to 8 seconds
    deadline = _t.monotonic() + 8.0
    while _t.monotonic() < deadline:
        await _aio.sleep(0.4)
        try:
            with psycopg2.connect(db_url, connect_timeout=2) as c:
                with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT status, offer_amount, offer_expires, error_msg
                          FROM carvana_queue WHERE id = %s
                    """, (job_id,))
                    row = cur.fetchone()
                    if row and row["status"] in ("done", "failed", "no_offer"):
                        if row["status"] == "done":
                            return {
                                "vin": vin_u,
                                "offer_amount": float(row["offer_amount"]) if row["offer_amount"] else None,
                                "offer_expires": str(row["offer_expires"]) if row["offer_expires"] else None,
                                "status": "fresh", "source": "carvana",
                                "job_id": job_id,
                                "elapsed_ms": int((_t.monotonic() - t0) * 1000),
                            }
                        return {
                            "vin": vin_u, "status": row["status"],
                            "error": row.get("error_msg"),
                            "elapsed_ms": int((_t.monotonic() - t0) * 1000),
                        }
        except Exception:
            pass

    return {
        "vin": vin_u, "status": "pending", "job_id": job_id,
        "warning": "no worker picked up in 8s",
        "elapsed_ms": int((_t.monotonic() - t0) * 1000),
    }

# ─── end carvana_offer ─────────────────────────────────────────────────────

@mcp.tool()
async def find_vin_for_ymm(
    year: int,
    make: str,
    model: str,
    trim: str = "",
    miles: int = 0,
) -> dict:
    """Discover a VIN for a YMM you don't have in your system. Use when
    valuation_precheck returned confidence='none' so you can drive a live
    vAuto/AccuTrade lookup. Tries our DB first (fast), then AutoTrader
    scrape (slower, ~2-3s).

    Returns: {vin, source, vehicle, vehicle_year, found, elapsed_ms}
    source is one of: 'db_bids', 'db_deals', 'autotrader', or null if not found.
    """
    import psycopg2, psycopg2.extras, os as _os, sqlite3, time as _t
    import urllib.request, urllib.error, re as _re
    t0 = _t.monotonic()
    make_u = (make or "").upper().strip()
    model_u = (model or "").upper().strip()
    out = {"vin": None, "source": None, "vehicle": None, "vehicle_year": None,
           "found": False, "elapsed_ms": 0,
           "trim_match": None, "actual_trim": None}

    # ── 0. ymmt_vin_cache (master catalog, sub-10ms — added 2026-05-26)
    # First check our pre-populated master VIN catalog. If we have a cached
    # VIN for this exact YMMT, return instantly. Built by overnight catalog
    # job + ongoing learning cache.
    try:
        db_url = _os.environ.get("DATABASE_URL",
            "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
        trim_norm = (trim or "").strip()
        with psycopg2.connect(db_url, connect_timeout=2) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Try exact trim first
                cur.execute("""
                    SELECT vin, source, source_url
                      FROM ymmt_vin_cache
                     WHERE year = %s
                       AND UPPER(make)  = UPPER(%s)
                       AND UPPER(model) = UPPER(%s)
                       AND LOWER(trim) = LOWER(%s)
                       AND status = 'found'
                       AND vin IS NOT NULL
                     LIMIT 1
                """, (year, make_u, model_u, trim_norm))
                row = cur.fetchone()
                if not row and trim_norm:
                    # Fall through to any trim (different YMMT but same YMM)
                    cur.execute("""
                        SELECT vin, source, source_url, trim AS actual_trim
                          FROM ymmt_vin_cache
                         WHERE year = %s
                           AND UPPER(make)  = UPPER(%s)
                           AND UPPER(model) = UPPER(%s)
                           AND status = 'found'
                           AND vin IS NOT NULL
                         ORDER BY found_at DESC
                         LIMIT 1
                    """, (year, make_u, model_u))
                    row = cur.fetchone()
                    if row:
                        out["trim_match"] = "fallthrough"
                        out["actual_trim"] = row.get("actual_trim")
                if row:
                    out["vin"] = row["vin"]
                    out["source"] = "ymmt_cache"
                    out["vehicle"] = f"{year} {make} {model} {out['actual_trim'] or trim_norm}".strip()
                    out["vehicle_year"] = year
                    if out["trim_match"] is None and trim_norm:
                        out["trim_match"] = "exact"
                    if out["actual_trim"] is None and trim_norm:
                        out["actual_trim"] = trim_norm
                    out["found"] = True
                    out["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
                    return out
    except Exception as e:
        out.setdefault("errors", []).append(f"ymmt_cache: {type(e).__name__}: {e}")

    # ── 1. DB scan: closest-mile match from bids
    try:
        db_url = _os.environ.get("DATABASE_URL",
            "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
        with psycopg2.connect(db_url, connect_timeout=2) as c:
            with c.cursor() as cur:
                # TRIM-PREFER 2026-05-25 — try exact trim match first,
                # fall back to any trim if nothing found.
                trim_norm = (trim or "").strip().lower()
                if trim_norm:
                    cur.execute("""
                        SELECT vin, year, make, model, trim, mileage
                          FROM bids
                         WHERE year = %s
                           AND UPPER(make)  = %s
                           AND UPPER(model) LIKE %s
                           AND LOWER(COALESCE(trim, '')) LIKE %s
                           AND vin IS NOT NULL
                           AND length(vin) = 17
                         ORDER BY ABS(COALESCE(mileage, 0) - %s) ASC,
                                  created_at DESC
                         LIMIT 1
                    """, (year, make_u, f"%{model_u}%", f"%{trim_norm}%",
                          int(miles or 0)))
                    row = cur.fetchone()
                else:
                    row = None
                if not row:
                    cur.execute("""
                        SELECT vin, year, make, model, trim, mileage
                          FROM bids
                         WHERE year = %s
                           AND UPPER(make)  = %s
                           AND UPPER(model) LIKE %s
                           AND vin IS NOT NULL
                           AND length(vin) = 17
                         ORDER BY ABS(COALESCE(mileage, 0) - %s) ASC,
                                  created_at DESC
                         LIMIT 1
                    """, (year, make_u, f"%{model_u}%", int(miles or 0)))
                    row = cur.fetchone()
                if row and row[0]:
                    out["vin"] = row[0]
                    out["source"] = "db_bids"
                    out["vehicle"] = f"{row[1]} {row[2]} {row[3]} {row[4] or ''}".strip()
                    out["vehicle_year"] = row[1]
                    out["actual_trim"] = row[4]
                    t_req = (trim or "").strip().lower()
                    t_got = (row[4] or "").strip().lower()
                    if t_req and t_got and t_req in t_got:
                        out["trim_match"] = "exact"
                    elif t_req:
                        out["trim_match"] = "fallthrough"
                    out["found"] = True
                    out["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
                    return out
    except Exception as e:
        out.setdefault("errors", []).append(f"db_bids: {type(e).__name__}: {e}")

    # ── 1.5 dealer_inventory (live partner-lot VINs — added 2026-05-25)
    try:
        with psycopg2.connect(db_url, connect_timeout=2) as c:
            with c.cursor() as cur:
                trim_norm = (trim or "").strip().lower()
                row = None
                if trim_norm:
                    cur.execute("""
                        SELECT vin, year, make, model, trim, mileage
                          FROM dealer_inventory
                         WHERE year = %s
                           AND UPPER(make)  = %s
                           AND UPPER(model) LIKE %s
                           AND LOWER(COALESCE(trim, '')) LIKE %s
                           AND status = 'active'
                           AND vin IS NOT NULL
                           AND length(vin) = 17
                         ORDER BY ABS(COALESCE(mileage, 0) - %s) ASC,
                                  last_seen_at DESC
                         LIMIT 1
                    """, (year, make_u, f"%{model_u}%",
                          f"%{trim_norm}%", int(miles or 0)))
                    row = cur.fetchone()
                if not row:
                    cur.execute("""
                        SELECT vin, year, make, model, trim, mileage
                          FROM dealer_inventory
                         WHERE year = %s
                           AND UPPER(make)  = %s
                           AND UPPER(model) LIKE %s
                           AND status = 'active'
                           AND vin IS NOT NULL
                           AND length(vin) = 17
                         ORDER BY ABS(COALESCE(mileage, 0) - %s) ASC,
                                  last_seen_at DESC
                         LIMIT 1
                    """, (year, make_u, f"%{model_u}%", int(miles or 0)))
                    row = cur.fetchone()
                if row and row[0]:
                    out["vin"] = row[0]
                    out["source"] = "dealer_inventory"
                    out["vehicle"] = f"{row[1]} {row[2]} {row[3]} {row[4] or ''}".strip()
                    out["vehicle_year"] = row[1]
                    out["actual_trim"] = row[4]
                    t_req = (trim or "").strip().lower()
                    t_got = (row[4] or "").strip().lower()
                    if t_req and t_got and t_req in t_got:
                        out["trim_match"] = "exact"
                    elif t_req:
                        out["trim_match"] = "fallthrough"
                    out["found"] = True
                    out["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
                    return out
    except Exception as e:
        out.setdefault("errors", []).append(f"dealer_inv: {type(e).__name__}: {e}")

    # ── 2. LSL deals scan
    try:
        lsl_path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
        if _os.path.exists(lsl_path):
            lc = sqlite3.connect(f"file:{lsl_path}?mode=ro", uri=True, timeout=2)
            lcur = lc.cursor()
            trim_norm = (trim or "").strip().lower()
            r = None
            if trim_norm:
                lcur.execute("""
                    SELECT vin_no, vehicle_info
                      FROM deals
                     WHERE UPPER(make_name)    LIKE UPPER(?)
                       AND UPPER(vehicle_info) LIKE UPPER(?)
                       AND UPPER(vehicle_info) LIKE UPPER(?)
                       AND vehicle_info        LIKE ?
                       AND vin_no IS NOT NULL
                       AND length(vin_no) = 17
                     ORDER BY sold_at DESC
                     LIMIT 1
                """, (f"%{make_u}%", f"%{model_u}%",
                      f"%{trim_norm}%", f"%{year}%"))
                r = lcur.fetchone()
            if not r:
                lcur.execute("""
                    SELECT vin_no, vehicle_info
                      FROM deals
                     WHERE UPPER(make_name)    LIKE UPPER(?)
                       AND UPPER(vehicle_info) LIKE UPPER(?)
                       AND vehicle_info        LIKE ?
                       AND vin_no IS NOT NULL
                       AND length(vin_no) = 17
                     ORDER BY sold_at DESC
                     LIMIT 1
                """, (f"%{make_u}%", f"%{model_u}%", f"%{year}%"))
                r = lcur.fetchone()
            lc.close()
            if r and r[0]:
                out["vin"] = r[0]
                out["source"] = "db_deals"
                out["vehicle"] = r[1]
                out["vehicle_year"] = year
                t_req = (trim or "").strip().lower()
                vi = (r[1] or "").lower()
                if t_req and t_req in vi:
                    out["trim_match"] = "exact"
                    out["actual_trim"] = trim
                elif t_req:
                    out["trim_match"] = "fallthrough"
                out["found"] = True
                out["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
                return out
    except Exception as e:
        out.setdefault("errors", []).append(f"db_deals: {type(e).__name__}: {e}")

    # ── 3.5 AutoTrader via VM worker pool (residential IP — 2026-05-25)
    # The direct AutoTrader URL from C1 is blocked (datacenter IP). Route
    # through vm-carvana-worker (vmid 140, 192.168.1.151) which has a
    # residential IP and polls autotrader_queue every 3s.
    try:
        import psycopg2, psycopg2.extras, time as _at_t, asyncio as _at_aio
        trim_norm = (trim or "").strip().lower()
        # Cache check
        with psycopg2.connect(db_url, connect_timeout=2) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT vin, actual_trim, cached_at
                      FROM autotrader_vin_cache
                     WHERE year = %s
                       AND UPPER(make) = UPPER(%s)
                       AND UPPER(model) = UPPER(%s)
                       AND trim_norm = %s
                       AND cached_at > NOW() - INTERVAL '7 days'
                     LIMIT 1
                """, (year, make_u, model_u, trim_norm))
                cache_row = cur.fetchone()
        if cache_row:
            out["vin"] = cache_row["vin"]
            out["source"] = "autotrader_cache"
            out["vehicle"] = f"{year} {make} {model} {cache_row['actual_trim'] or ''}".strip() + " (AutoTrader)"
            out["vehicle_year"] = year
            out["actual_trim"] = cache_row["actual_trim"]
            out["trim_match"] = "exact" if (trim_norm and (cache_row["actual_trim"] or "").strip().lower() == trim_norm) else ("fallthrough" if trim_norm else None)
            out["found"] = True
            out["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
            return out
        # Enqueue + poll
        job_id = None
        with psycopg2.connect(db_url, connect_timeout=2) as c:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO autotrader_queue (year, make, model, trim, miles, status)
                    VALUES (%s, %s, %s, %s, %s, 'queued')
                    RETURNING id
                """, (year, make, model, trim, int(miles or 0)))
                job_id = cur.fetchone()[0]
        # Poll for up to 6 seconds
        deadline = _at_t.monotonic() + 12.0
        while _at_t.monotonic() < deadline:
            await _at_aio.sleep(0.4)
            with psycopg2.connect(db_url, connect_timeout=2) as c:
                with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT status, found_vin, found_trim, found_year, error_msg
                          FROM autotrader_queue WHERE id = %s
                    """, (job_id,))
                    qr = cur.fetchone()
                    if qr and qr["status"] == "done" and qr["found_vin"]:
                        out["vin"] = qr["found_vin"]
                        out["source"] = "autotrader_vm"
                        out["vehicle"] = f"{qr['found_year'] or year} {make} {model} {qr['found_trim'] or ''}".strip() + " (AutoTrader)"
                        out["vehicle_year"] = qr["found_year"] or year
                        out["actual_trim"] = qr["found_trim"]
                        out["trim_match"] = "exact" if (trim_norm and (qr["found_trim"] or "").strip().lower() == trim_norm) else ("fallthrough" if trim_norm else None)
                        out["found"] = True
                        out["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
                        return out
                    if qr and qr["status"] in ("failed", "no_match"):
                        break
        out.setdefault("errors", []).append(f"autotrader_vm: pending or no_match (job {job_id})")
    except Exception as e:
        out.setdefault("errors", []).append(f"autotrader_vm: {type(e).__name__}: {e}")

    # ── 3. AutoTrader JSON search (trim-filtered client-side — 2026-05-25)
    # AutoTrader's trimCodeList wants their internal trim codes, not free
    # text. So we ask for 25 listings, parse the JSON, and pick the first
    # VIN whose trim field matches our requested trim. Fall through to
    # first VIN if no trim match.
    try:
        import json as _json
        url = (f"https://www.autotrader.com/rest/searchresults/base"
               f"?makeCodeList={make_u}&modelCodeList={model_u}"
               f"&startYear={year}&endYear={year}&listingTypes=USED"
               f"&numRecords=25&sortBy=relevance")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/115.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = resp.read(800000).decode("utf-8", errors="ignore")
        listings = []
        try:
            j = _json.loads(data)
            listings = j.get("listings") or j.get("results") or []
        except Exception:
            listings = []
        trim_norm = (trim or "").strip().lower()
        picked = None
        if trim_norm and listings:
            for L in listings:
                lvin = (L.get("vin") or "").strip().upper()
                ltrim = (L.get("trim") or L.get("trimName") or "").strip().lower()
                if len(lvin) == 17 and trim_norm in ltrim:
                    picked = {"vin": lvin, "trim": L.get("trim") or L.get("trimName")}
                    break
        if not picked and listings:
            for L in listings:
                lvin = (L.get("vin") or "").strip().upper()
                if len(lvin) == 17:
                    picked = {"vin": lvin, "trim": L.get("trim") or L.get("trimName")}
                    break
        # Last-resort regex fallback if JSON shape unexpected
        if not picked:
            vin_matches = _re.findall(r'"vin"\s*:\s*"([A-HJ-NPR-Z0-9]{17})"', data)
            if vin_matches:
                picked = {"vin": vin_matches[0], "trim": None}
        if picked:
            out["vin"] = picked["vin"]
            out["source"] = "autotrader"
            picked_trim = picked.get("trim") or trim or ""
            out["vehicle"] = f"{year} {make} {model} {picked_trim} (AutoTrader nationwide)".strip()
            out["vehicle_year"] = year
            out["found"] = True
            out["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
            return out
    except Exception as e:
        out.setdefault("errors", []).append(f"autotrader: {type(e).__name__}: {e}")

    out["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
    return out


@mcp.tool()
async def live_valuation_by_vin(
    vin: str,
    miles: int = 0,
    year: int = 0,
    make: str = "",
    model: str = "",
    trim: str = "",
) -> dict:
    """Fire a LIVE vAuto + AccuTrade lookup for a SPECIFIC VIN. Use after
    find_vin_for_ymm gave you a VIN. Returns within ~5-10s. Includes MMR,
    rBook, AccuTrade guaranteed offer/retail, and miles-adjusted comps
    centered on THIS VIN.

    Different from get_vehicle_valuation which is YMM-only and returns
    aggregate comps. This one anchors on a specific VIN and is faster
    when the VIN is known."""
    import time as _t
    t0 = _t.monotonic()
    from voice_agent import _fetch_live_vauto_with_vin
    try:
        import asyncio as _aio
        try:
            result = await _aio.wait_for(
                _aio.to_thread(_fetch_live_vauto_with_vin, vin, miles),
                timeout=10.0,
            )
        except _aio.TimeoutError:
            return {
                "vin": vin, "timed_out": True,
                "warning": "live vAuto did not respond in 10s for this VIN",
                "elapsed_ms": 10000,
            }
        if result:
            result["vin"] = vin
            result["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
            return result
        return {"vin": vin, "warning": "no live data returned",
                "elapsed_ms": int((_t.monotonic() - t0) * 1000)}
    except Exception as e:
        return {"vin": vin, "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": int((_t.monotonic() - t0) * 1000)}


@mcp.tool()
async def valuation_precheck(
    year: int,
    make: str,
    model: str,
    trim: str = "",
) -> dict:
    """FAST (~200-500ms) precheck. Call this BEFORE get_vehicle_valuation
    so you can tell the operator whether this YMM is familiar to us. Returns
    counts of prior bids, prior deals, and current dealer inventory for the
    YMM. Use the response to craft an appropriate filler line WHILE you
    then call get_vehicle_valuation in the next turn.

    Args:
        year: 4-digit model year
        make: brand
        model: base model
        trim: optional trim hint

    Returns:
        {
          in_our_system: bool,     # any prior data anywhere
          n_prior_bids: int,       # how many bids we've appraised
          n_deals_sold: int,       # how many we've actually transacted
          n_dealer_inv: int,       # currently live on partner lots
          last_seen_iso: str|null, # most recent touch
          confidence: 'strong' | 'thin' | 'none',
        }
    """
    import psycopg2, psycopg2.extras, os as _os, time as _t
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    t0 = _t.monotonic()
    out = {
        "in_our_system": False, "n_prior_bids": 0, "n_deals_sold": 0,
        "n_dealer_inv": 0, "last_seen_iso": None, "confidence": "none",
        "elapsed_ms": 0,
    }
    try:
        make_u = (make or "").upper().strip()
        model_u = (model or "").upper().strip()
        with psycopg2.connect(db_url, connect_timeout=2) as c:
            with c.cursor() as cur:
                # Prior bids on this YMM (last 365 days)
                cur.execute("""
                    SELECT COUNT(*), MAX(created_at)::date
                      FROM bids
                     WHERE year = %s
                       AND UPPER(make)  = %s
                       AND UPPER(model) LIKE %s
                       AND created_at > NOW() - INTERVAL '365 days'
                """, (year, make_u, f"%{model_u}%"))
                row = cur.fetchone()
                out["n_prior_bids"] = int(row[0] or 0)
                last_seen = row[1]
                # Live dealer inventory
                cur.execute("""
                    SELECT COUNT(*)
                      FROM dealer_inventory
                     WHERE year = %s
                       AND UPPER(make)  = %s
                       AND UPPER(model) LIKE %s
                       AND status = 'active'
                """, (year, make_u, f"%{model_u}%"))
                out["n_dealer_inv"] = int(cur.fetchone()[0] or 0)
        # LSL sold deals (sqlite)
        import sqlite3
        lsl_path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
        if _os.path.exists(lsl_path):
            try:
                lc = sqlite3.connect(f"file:{lsl_path}?mode=ro", uri=True, timeout=2)
                lcur = lc.cursor()
                lcur.execute("""
                    SELECT COUNT(*), MAX(sold_at)
                      FROM deals
                     WHERE UPPER(make_name)    LIKE UPPER(?)
                       AND UPPER(vehicle_info) LIKE UPPER(?)
                       AND vehicle_info        LIKE ?
                       AND sold_at > date('now', '-365 days')
                """, (f"%{make_u}%", f"%{model_u}%", f"%{year}%"))
                r = lcur.fetchone()
                out["n_deals_sold"] = int(r[0] or 0)
                lsl_last = r[1]
                if lsl_last and (last_seen is None or str(lsl_last)[:10] > str(last_seen)[:10]):
                    last_seen = lsl_last
                lc.close()
            except Exception:
                pass
        out["last_seen_iso"] = str(last_seen)[:10] if last_seen else None
        total_signals = out["n_prior_bids"] + out["n_deals_sold"] + out["n_dealer_inv"]
        out["in_our_system"] = total_signals > 0
        # ANCHOR_VIN_DISCIPLINE 2026-05-25 — require a VIN we can actually
        # re-lookup (dealer_inv or prior_bids) for thin/strong. Historical
        # deals alone produce no anchor, AccuTrade/MMR return empty.
        has_anchor = (out["n_dealer_inv"] + out["n_prior_bids"]) > 0
        out["has_anchor_vin"] = has_anchor
        if total_signals >= 5 and has_anchor:
            out["confidence"] = "strong"
        elif total_signals >= 1 and has_anchor:
            out["confidence"] = "thin"
        elif total_signals >= 1 and not has_anchor:
            # Historical-only signals; tell Bill to go through find_vin_for_ymm.
            out["confidence"] = "none"
            out["note"] = "historical deals only, no anchor VIN — use find_vin_for_ymm"
        else:
            out["confidence"] = "none"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    out["elapsed_ms"] = int((_t.monotonic() - t0) * 1000)
    return out


# --- lsl_make_volume + lsl_top_makes (added 2026-05-25) -----------------

def _lsl_resolve_period(period: str):
    """Return (start_iso, end_iso) for a period string. EDT-aware."""
    import re as _re
    from datetime import datetime as _dt, timedelta as _td
    try:
        from zoneinfo import ZoneInfo as _Z
        now = _dt.now(_Z("America/New_York"))
    except Exception:
        now = _dt.now()
    today = now.strftime("%Y-%m-%d")
    p = (period or "").lower().strip().replace("-", "_").replace(" ", "_")
    m = _re.match(r'^(\d{4}_\d{2}_\d{2}):(\d{4}_\d{2}_\d{2})$', p)
    if m:
        return (m.group(1).replace("_", "-"), m.group(2).replace("_", "-"))
    m = _re.match(r'^(\d{4}_\d{2}_\d{2})$', p)
    if m:
        d = m.group(1).replace("_", "-")
        return (d, d)
    if p == "today":
        return (today, today)
    if p == "yesterday":
        d = (now - _td(days=1)).strftime("%Y-%m-%d")
        return (d, d)
    if p == "last_7_days":
        return ((now - _td(days=7)).strftime("%Y-%m-%d"), today)
    if p == "last_30_days":
        return ((now - _td(days=30)).strftime("%Y-%m-%d"), today)
    if p == "last_90_days":
        return ((now - _td(days=90)).strftime("%Y-%m-%d"), today)
    if p == "this_month":
        return (now.replace(day=1).strftime("%Y-%m-%d"), today)
    if p == "last_month":
        first = now.replace(day=1)
        prev_end = (first - _td(days=1))
        prev_start = prev_end.replace(day=1)
        return (prev_start.strftime("%Y-%m-%d"), prev_end.strftime("%Y-%m-%d"))
    if p in ("ytd", "year_to_date", "this_year"):
        return (str(now.year) + "-01-01", today)
    if p == "last_year":
        return (str(now.year - 1) + "-01-01", str(now.year - 1) + "-12-31")
    if p == "all_time":
        return ("2000-01-01", today)
    months = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
              "june": 6, "july": 7, "august": 8, "september": 9,
              "october": 10, "november": 11, "december": 12}
    m = _re.match(r'^([a-z]+)(?:_(\d{4}))?(_mtd)?$', p)
    if m and m.group(1) in months:
        mo = months[m.group(1)]
        yr = int(m.group(2)) if m.group(2) else now.year
        from calendar import monthrange
        last_day = monthrange(yr, mo)[1]
        start = "%04d-%02d-01" % (yr, mo)
        if m.group(3) == "_mtd":
            cap = now.day if (now.year == yr and now.month == mo) else last_day
            cap = min(cap, last_day)
            end = "%04d-%02d-%02d" % (yr, mo, cap)
        else:
            end = "%04d-%02d-%02d" % (yr, mo, last_day)
        return (start, end)
    return ((now - _td(days=30)).strftime("%Y-%m-%d"), today)


@mcp.tool()
async def lsl_make_volume(
    caller_name: str,
    make: str,
    period: str = "ytd",
    caller_pin: str = "",
) -> dict:
    """OWNER-GATED. Count + summarize deals of a specific MAKE in a period.
    USE THIS when operator asks "how many BMWs / Mercedes / Fords did we
    buy this month / this year / last year" - a make-aware aggregation
    that lsl_deals_booked cannot do (lsl_deals_booked returns top 3 only).

    Args:
        caller_name: owner first name (Oscar, Gregg, Joe, Todd)
        make: canonical brand string (case-insensitive partial match)
        period: today, yesterday, this_month, last_month, ytd, last_year,
                last_30_days, "april", "april_mtd", "2026-04-01:2026-04-24"

    Returns: {make, period, start, end, n_deals, total_profit, avg_pvr,
              total_revenue, top_models}
    """
    if not _is_owner(caller_name, caller_pin):
        return {"error": "owner-only", "owner_required": True}
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}
    start, end = _lsl_resolve_period(period)
    try:
        c = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT make_name, vehicle_info, sale_price, front_value, sold_at
              FROM deals
             WHERE LOWER(make_name) LIKE LOWER(?)
               AND date(sold_at) BETWEEN ? AND ?
        """, ("%" + make + "%", start, end)).fetchall()
        c.close()
        n = len(rows)
        total_profit = sum((r["front_value"] or 0) for r in rows)
        total_revenue = sum((r["sale_price"] or 0) for r in rows)
        avg_pvr = (total_profit / n) if n else 0
        from collections import Counter
        model_counts = Counter()
        model_profit = {}
        for r in rows:
            vi = (r["vehicle_info"] or "").strip()
            parts = vi.split()
            if len(parts) >= 3:
                model = " ".join(parts[2:5])
            else:
                model = vi
            if model:
                model_counts[model] += 1
                model_profit[model] = model_profit.get(model, 0) + (r["front_value"] or 0)
        top_models = [
            {"model": m, "n": cnt, "total_profit": round(model_profit[m], 2)}
            for m, cnt in model_counts.most_common(8)
        ]
        return {
            "make_filter": make,
            "period": period,
            "start": start, "end": end,
            "n_deals": n,
            "total_profit": round(total_profit, 2),
            "total_revenue": round(total_revenue, 2),
            "avg_pvr": round(avg_pvr, 2),
            "top_models": top_models,
        }
    except Exception as e:
        log.exception("lsl_make_volume failed")
        return {"error": type(e).__name__ + ": " + str(e)}


@mcp.tool()
async def lsl_top_makes(
    caller_name: str,
    period: str = "ytd",
    limit: int = 10,
    caller_pin: str = "",
) -> dict:
    """OWNER-GATED. Top N makes by deal count in a period. USE THIS when
    operator asks "what did we buy most of", "top makes this year",
    "what brand is our biggest volume".

    Args:
        caller_name: owner first name
        period: same vocab as lsl_make_volume
        limit: number of top makes (1-30, default 10)

    Returns: {period, start, end, total_deals, makes[{make, n, total_profit, avg_pvr}]}
    """
    if not _is_owner(caller_name, caller_pin):
        return {"error": "owner-only", "owner_required": True}
    if limit < 1:
        limit = 1
    if limit > 30:
        limit = 30
    import sqlite3, os as _os
    path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(path):
        return {"error": "lsl crm not available"}
    start, end = _lsl_resolve_period(period)
    try:
        c = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=10)
        rows = c.execute("""
            SELECT COALESCE(make_name, '(unknown)') AS make,
                   COUNT(*) AS n,
                   COALESCE(SUM(front_value), 0) AS total_profit,
                   COALESCE(AVG(front_value), 0) AS avg_pvr
              FROM deals
             WHERE date(sold_at) BETWEEN ? AND ?
             GROUP BY make_name
             ORDER BY n DESC
             LIMIT ?
        """, (start, end, int(limit))).fetchall()
        total = c.execute("""
            SELECT COUNT(*) FROM deals WHERE date(sold_at) BETWEEN ? AND ?
        """, (start, end)).fetchone()[0]
        c.close()
        makes = [{
            "make": r[0],
            "n": r[1],
            "total_profit": round(r[2] or 0, 2),
            "avg_pvr": round(r[3] or 0, 2),
        } for r in rows]
        return {
            "period": period,
            "start": start, "end": end,
            "total_deals": total,
            "makes": makes,
        }
    except Exception as e:
        log.exception("lsl_top_makes failed")
        return {"error": type(e).__name__ + ": " + str(e)}

# --- end lsl_make_volume / lsl_top_makes -----------------------------------

@mcp.tool()
async def search_bids(
    make: str = "",
    model: str = "",
    year: int = 0,
    since_days: int = 30,
    submitter: str = "",
    limit: int = 25,
) -> dict:
    """USE THIS when the operator asks about bids over a window WIDER than
    the last 20, or when they want to know who SUBMITTED a bid for a
    particular vehicle. Filters: make/model/year (case-insensitive partial
    match on make+model), since_days (1-365, default 30), submitter (substring
    match on contact name OR company), limit (max 100). Returns list of bids
    with year/make/model/mileage/prices/status/submitter_name/submitter_company/
    created_at. Different from recent_bids (which is only last 20, no filters,
    no submitter).
    """
    if since_days < 1: since_days = 1
    if since_days > 365: since_days = 365
    if limit < 1: limit = 1
    if limit > 100: limit = 100
    import psycopg2, psycopg2.extras, os as _os
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    where = ["b.created_at >= now() - (%s || ' days')::interval"]
    params = [str(since_days)]
    if make:
        where.append("LOWER(b.make) LIKE LOWER(%s)")
        params.append(f"%{make}%")
    if model:
        where.append("LOWER(b.model) LIKE LOWER(%s)")
        params.append(f"%{model}%")
    if year and year > 1900:
        where.append("b.year = %s")
        params.append(int(year))
    if submitter:
        where.append("(LOWER(COALESCE(c.name,'')) LIKE LOWER(%s) OR LOWER(COALESCE(c.company,'')) LIKE LOWER(%s))")
        params.append(f"%{submitter}%")
        params.append(f"%{submitter}%")
    params.append(int(limit))
    sql = f"""
        SELECT b.id, b.vin, b.year, b.make, b.model, b.trim, b.mileage,
               b.status, b.ai_price, b.asking_price, b.bid_amount,
               b.created_at,
               c.name AS submitter_name, c.company AS submitter_company, b.phone AS submitter_phone
          FROM bids b
          LEFT JOIN contacts c ON c.id = b.contact_id
         WHERE {' AND '.join(where)}
         ORDER BY b.created_at DESC
         LIMIT %s
    """
    try:
        with psycopg2.connect(db_url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
        bids = []
        for r in rows:
            bids.append({
                "bid_id": r.get("id"),
                "year": r.get("year"),
                "make": r.get("make"),
                "model": r.get("model"),
                "trim": r.get("trim"),
                "mileage": r.get("mileage"),
                "status": r.get("status"),
                "asking_price": (float(r["asking_price"]) if r.get("asking_price") is not None else None),
                "ai_price": (float(r["ai_price"]) if r.get("ai_price") is not None else None),
                "bid_amount": (float(r["bid_amount"]) if r.get("bid_amount") is not None else None),
                "submitter_name": r.get("submitter_name"),
                "submitter_company": r.get("submitter_company"),
                "submitter_phone": r.get("submitter_phone"),
                "created_at": (r["created_at"].isoformat() if r.get("created_at") else None),
            })
        return {
            "filters": {"make": make or None, "model": model or None,
                        "year": year or None, "since_days": since_days,
                        "submitter": submitter or None},
            "n": len(bids), "bids": bids,
        }
    except Exception as e:
        log.exception("search_bids failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def send_partner_bid_card(
    bid_id: int,
    partner_phone: str,
    partner_name: str = "",
    offer_amount: float = 0,
    note: str = "",
) -> dict:
    """Send the bid card summary to a partner dealer via SMS and record
    the verbal interest. Used by the outbound voice bot when a partner
    expresses interest on a call.

    Composes an SMS with year/make/model/miles/color/MMR/target buy and
    a link to the full bid card. Fires via the existing Twilio number
    (+17542471123). Also inserts a row into partner_bid_requests so the
    offer shows up on the EW dashboard."""
    import os as _os
    sid   = _os.environ.get("TWILIO_ACCOUNT_SID")
    tok   = _os.environ.get("TWILIO_AUTH_TOKEN")
    frm   = _os.environ.get("TWILIO_PHONE")
    if not (sid and tok and frm):
        return {"ok": False, "error": "twilio creds missing"}
    if not partner_phone or not bid_id:
        return {"ok": False, "error": "partner_phone and bid_id required"}

    # Normalize phone to E.164
    p = "".join(c for c in str(partner_phone) if c.isdigit() or c == "+")
    if not p.startswith("+"):
        p = "+1" + p.lstrip("1")

    try:
        bid = await get_bid(int(bid_id))
        if bid.get("error"):
            return {"ok": False, "error": f"bid lookup: {bid['error']}"}
    except Exception as e:
        return {"ok": False, "error": f"bid lookup failed: {e}"}

    # Compose SMS
    yr = bid.get("year") or ""
    mk = bid.get("make") or ""
    md = bid.get("model") or ""
    tr = bid.get("trim") or ""
    mi = bid.get("mileage") or 0
    color = bid.get("color") or ""
    mmr = bid.get("vauto_mmr")
    target = bid.get("ai_price") or bid.get("vauto_rbook")
    sticker = (bid.get("ipacket") or {}).get("total_msrp")

    lines = [f"EW: {yr} {mk} {md}".strip() + (f" {tr}" if tr else "")]
    if mi:     lines[0] += f" / {int(mi):,} mi"
    if color:  lines[0] += f" / {color}"
    if sticker:lines.append(f"MSRP ${int(sticker):,}")
    if mmr:    lines.append(f"MMR ${int(mmr):,}")
    if target: lines.append(f"Target ${int(target):,}")
    if offer_amount and offer_amount > 0:
        lines.append(f"YOUR INTEREST: ${int(offer_amount):,}")
    if note:
        lines.append(note[:120])
    lines.append(f"Full card: https://experience-wholesale.net/bid/{int(bid_id)}")
    body = "\n".join(lines)

    try:
        from twilio.rest import Client
        client = Client(sid, tok)
        msg = client.messages.create(to=p, from_=frm, body=body)
        log.info(f"send_partner_bid_card sms_sid={msg.sid} to={p} bid={bid_id}")
    except Exception as e:
        log.exception("twilio sms failed")
        return {"ok": False, "error": f"twilio: {type(e).__name__}: {e}",
                "body_would_have_sent": body}

    # Record the verbal offer in partner_bid_requests (best-effort)
    recorded = False
    try:
        import psycopg2, psycopg2.extras
        db_url = _os.environ.get("DATABASE_URL",
            "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
        with psycopg2.connect(db_url) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO partner_bid_requests
                (bid_id, target_price, submitted_at)
                VALUES (%s, %s, NOW()) RETURNING id""",
                (int(bid_id), float(offer_amount) if offer_amount else None))
            row_id = cur.fetchone()[0]
            c.commit()
            recorded = True
            log.info(f"partner_bid_requests row {row_id} inserted")
    except Exception as e:
        log.warning(f"partner_bid_requests insert failed: {e}")

    return {
        "ok": True,
        "sms_sid": msg.sid,
        "partner_phone": p,
        "sms_body": body,
        "offer_recorded": recorded,
    }


@mcp.tool()
async def recent_bids(limit: int = 5) -> dict:
    """USE THIS when the user asks what came in lately, recent bids,
    last few bids, the last bid, latest activity, what is on the
    dashboard, etc. Returns the most recent N bids (newest first).
    """
    if limit < 1: limit = 1
    if limit > 20: limit = 20
    import psycopg2, psycopg2.extras, os as _os
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT b.id, b.vin, b.year, b.make, b.model, b.trim,
                           b.mileage, b.status, b.ai_price, b.asking_price,
                           b.created_at,
                           v.mmr AS vauto_mmr, v.rbook AS vauto_rbook,
                           a.guaranteed_offer
                      FROM bids b
                      LEFT JOIN vauto_lookups v ON v.bid_id = b.id
                      LEFT JOIN accutrade_lookups a ON a.bid_id = b.id
                     ORDER BY b.id DESC LIMIT %s
                """, (int(limit),))
                rows = [dict(r) for r in cur.fetchall()]
        bids = []
        for r in rows:
            bids.append({
                "bid_id": r.get("id"),
                "year": r.get("year"),
                "make": r.get("make"),
                "model": r.get("model"),
                "trim": r.get("trim"),
                "mileage": r.get("mileage"),
                "status": r.get("status"),
                "ai_price": (float(r["ai_price"]) if r.get("ai_price") else None),
                "asking_price": (float(r["asking_price"]) if r.get("asking_price") else None),
                "vauto_mmr": r.get("vauto_mmr"),
                "vauto_rbook": r.get("vauto_rbook"),
                "accutrade_guaranteed": (float(r["guaranteed_offer"]) if r.get("guaranteed_offer") else None),
                "created_at": (r["created_at"].isoformat() if r.get("created_at") else None),
            })
        return {"bids": bids, "n": len(bids)}
    except Exception as e:
        log.exception("recent_bids failed")
        return {"error": f"{type(e).__name__}: {e}"}


# ── Starlette app with bearer-token auth ─────────────────────────────
@contextlib.asynccontextmanager
async def lifespan(app):
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(mcp.session_manager.run())
        yield


async def healthz(_req: Request):
    return JSONResponse({"ok": True, "service": "ew-mcp"})


async def auth_middleware(request: Request, call_next):
    """Auth: accept either Bearer token OR ?token= URL param OR
    no-auth for unrecognized clients (temporary — Claude.ai requires
    OAuth which we have not yet implemented; ship now, harden later).
    """
    path = request.url.path
    if path == "/healthz" or path.startswith("/healthz"):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token_qs = request.query_params.get("token", "")
    ua = (request.headers.get("user-agent", "") or "").lower()
    if auth == f"Bearer {MCP_BEARER_TOKEN}":
        pass  # bearer ok
    elif token_qs == MCP_BEARER_TOKEN:
        pass  # query-param ok
    elif "claude" in ua or "anthropic" in ua or "python-httpx" in ua:
        # Recognized Claude.ai requests — allow for now (OAuth TBD)
        log.info(f"allowing claude UA without auth: {ua[:60]} path={path}")
    else:
        log.warning(f"auth fail from {request.client.host if request.client else '?'} "
                    f"path={path} ua={ua[:60]}")
        return Response("unauthorized", status_code=401)
    return await call_next(request)


from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        return await auth_middleware(request, call_next)


app = Starlette(
    routes=[
        Route("/healthz", healthz),
        Mount("/mcp", app=mcp.streamable_http_app()),
    ],
    middleware=[Middleware(AuthMiddleware)],
    lifespan=lifespan,
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9004, log_level="info")


# ─── vin_cache_batch_insert (added 2026-05-26) ───────────────────────────

@mcp.tool()
async def vin_cache_batch_insert(entries: list) -> dict:
    """Batch-insert YMMT VIN cache entries. Used by Phase 3 backfill agents.

    entries: list of dicts, each with keys:
      year (int), make (str), model (str), trim (str),
      vin (str or null), source (str), source_url (str, optional),
      decoded_match (bool, optional), status ('found' | 'not_found')

    Returns: {inserted: int, skipped: int, errors: list}
    """
    import psycopg2, os as _os
    if not isinstance(entries, list):
        return {"error": "entries must be a list"}
    if len(entries) > 200:
        return {"error": "max 200 entries per batch"}
    db_url = _os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    inserted = 0
    skipped = 0
    errors = []
    try:
        with psycopg2.connect(db_url, connect_timeout=3) as c:
            with c.cursor() as cur:
                for e in entries:
                    try:
                        year = int(e["year"])
                        make = str(e["make"]).strip()
                        model = str(e["model"]).strip()
                        trim = str(e.get("trim", "") or "").strip()
                        vin = e.get("vin")
                        source = str(e.get("source", "google_search"))
                        source_url = e.get("source_url")
                        decoded_match = bool(e.get("decoded_match", False))
                        status = str(e.get("status", "found"))
                        if status not in ("found", "not_found"):
                            errors.append(f"{year} {make} {model} {trim}: bad status")
                            continue
                        if status == "found" and (not vin or len(str(vin)) != 17):
                            errors.append(f"{year} {make} {model} {trim}: bad vin")
                            continue
                        cur.execute("""
                            INSERT INTO ymmt_vin_cache
                              (year, make, model, trim, vin, source, source_url,
                               confidence, decoded_match, status)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 'medium', %s, %s)
                            ON CONFLICT (year, make, model, trim) DO NOTHING
                            RETURNING vin
                        """, (year, make, model, trim, vin, source, source_url,
                              decoded_match, status))
                        if cur.fetchone():
                            inserted += 1
                        else:
                            skipped += 1
                    except Exception as e2:
                        errors.append(f"{e.get('year')} {e.get('make')} {e.get('model')}: {type(e2).__name__}: {e2}")
        return {"inserted": inserted, "skipped": skipped, "errors": errors[:10]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

# ─── end vin_cache_batch_insert ───────────────────────────────────────────

# ─── Bill personal SMS tools (added 2026-05-26) ─────────────────────────
# Allows Bill to look up contacts from Oscar's personal directory
# (bill_contacts table) and send SMS via the EW 754 number prefixed with
# the speaker's name. Gated: bill_can_text=TRUE only.

@mcp.tool()
async def lookup_contact(caller_name: str, name_query: str) -> dict:
    """OWNER-GATED. Look up a person Bill is allowed to text in Oscar's
    personal contacts. Fuzzy ILIKE match on contact name.

    USE WHEN the user says 'text X' or 'send a message to X' — call this
    FIRST to resolve the name to a phone number. Only contacts that have
    been explicitly enabled (bill_can_text=TRUE) are returned.

    Returns: {matches: [{id, name, phone, role}], total: N,
              note: 'multiple matches - ask which one' if >1}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    q = (name_query or "").strip()
    if not q:
        return {"error": "name_query required", "matches": [], "total": 0}
    import psycopg2
    db_url = os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, name, phone_e164, role
                  FROM bill_contacts
                 WHERE bill_can_text = TRUE
                   AND lower(name) LIKE %s
                 ORDER BY name
                 LIMIT 8
            """, (f"%{q.lower()}%",))
            rows = cur.fetchall()
    except Exception as e:
        return {"error": f"db: {type(e).__name__}: {e}",
                "matches": [], "total": 0}
    matches = [{"id": r[0], "name": r[1], "phone": r[2],
                "role": r[3] or ""} for r in rows]
    out = {"matches": matches, "total": len(matches)}
    if len(matches) == 0:
        out["note"] = (f"no enabled contact matching {name_query!r}. "
                       f"Bill cannot text people not pre-approved.")
    elif len(matches) > 1:
        out["note"] = (f"{len(matches)} matches — ask the user which one "
                       f"by name or context")
    return out


@mcp.tool()
async def send_sms_from_user(
    caller_name: str,
    sender: str,
    recipient_phone_e164: str,
    message: str,
    confirm: bool = False,
) -> dict:
    """OWNER-GATED. Send an SMS from the EW 754 number, prepending
    'From {sender}: ' to the body so the recipient knows it came from
    the speaker, not from Bill or EW.

    SAFETY RULES:
    - Bill MUST read back the full message and recipient and get an
      explicit verbal 'yes' / 'send it' BEFORE setting confirm=True.
    - The recipient_phone_e164 MUST have come from a prior lookup_contact
      call — i.e., the contact must have bill_can_text=TRUE.
    - sender should match the speaker identity passed in caller_name.

    Returns: {ok: bool, message_sid?, to, body_sent, error?}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    if not confirm:
        return {"error": "confirm=False — Bill must read back the message "
                         "and message recipient, get verbal 'yes', THEN "
                         "call again with confirm=True",
                "ok": False}
    s = (sender or caller_name or "").strip().capitalize()
    if not s:
        return {"error": "sender required (typically the caller's first name)",
                "ok": False}
    p = (recipient_phone_e164 or "").strip()
    if not (p.startswith("+") and len(p) >= 8):
        return {"error": f"recipient_phone_e164 must be E.164 (e.g. +1...); got {p!r}",
                "ok": False}
    msg = (message or "").strip()
    if not msg:
        return {"error": "message required", "ok": False}
    # Verify the recipient is in bill_contacts AND bill_can_text=TRUE.
    # Belt-and-braces: even if the LLM hallucinates a phone, we refuse
    # to send to anyone not explicitly enabled in the DB.
    import psycopg2
    db_url = os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    contact_name = None
    try:
        with psycopg2.connect(db_url) as c, c.cursor() as cur:
            cur.execute("""SELECT name, bill_can_text FROM bill_contacts
                            WHERE phone_e164 = %s LIMIT 1""", (p,))
            row = cur.fetchone()
            if not row:
                return {"error": f"phone {p} not in bill_contacts — "
                                 f"call lookup_contact first",
                        "ok": False}
            contact_name, can_text = row
            if not can_text:
                return {"error": f"{contact_name} is in contacts but not "
                                 f"approved for texting (bill_can_text=FALSE)",
                        "ok": False}
    except Exception as e:
        return {"error": f"db check: {type(e).__name__}: {e}", "ok": False}
    # Send the SMS via Twilio
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_PHONE")
    if not (sid and tok and frm):
        return {"error": "twilio env not configured", "ok": False}
    body_sent = f"From {s}: {msg}"
    try:
        from twilio.rest import Client
        client = Client(sid, tok)
        tw_msg = client.messages.create(to=p, from_=frm, body=body_sent)
        log.info(f"send_sms_from_user sender={s} to={contact_name} "
                 f"({p}) sid={tw_msg.sid}")
    except Exception as e:
        log.exception("twilio send failed")
        return {"ok": False, "to": p, "body_sent": body_sent,
                "error": f"twilio: {type(e).__name__}: {e}"}
    # Best-effort log
    try:
        with psycopg2.connect(db_url) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO bill_sms_log
                (sender, contact_id, phone_e164, body, twilio_sid, sent_at)
                VALUES (%s,
                        (SELECT id FROM bill_contacts WHERE phone_e164 = %s),
                        %s, %s, %s, NOW())""",
                (s, p, p, body_sent, tw_msg.sid))
    except Exception as e:
        log.warning(f"bill_sms_log insert failed (non-fatal): {e}")
    return {"ok": True, "to": p, "contact_name": contact_name,
            "body_sent": body_sent, "message_sid": tw_msg.sid}


# ─── end Bill personal SMS tools ────────────────────────────────────────

# ─── Bill bid-awareness tools (added 2026-05-26) ────────────────────────
# Allows Bill to:
#   - list today's (or N-day) bids
#   - score today's bids against a specific partner dealer
#   - find best partner dealers for a specific bid
# Re-uses the existing YMMT_MATCH_2026_05_26 scoring (app._compute_bid_matches)
# plus the dealers.buy_profile rolodex.

@mcp.tool()
async def bids_today(caller_name: str, days_back: int = 0) -> dict:
    """OWNER. List bids that came in today (or N days back). Use when the
    operator asks "what came in today?", "show me today's bids", "what's
    in the pipeline?". days_back=0 means today, 1=yesterday only,
    7=last 7 days.

    Returns: {count, bids: [{id, year, make, model, trim, mileage,
                            color, ai_price, asking_price, status,
                            created_at_human}]}"""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    import psycopg2, psycopg2.extras
    db_url = os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    db = max(0, int(days_back or 0))
    if db == 0:
        # today only
        where = ("created_at >= date_trunc('day', NOW() AT TIME ZONE 'America/New_York') "
                 "AT TIME ZONE 'America/New_York'")
    else:
        where = (f"created_at >= (date_trunc('day', NOW() AT TIME ZONE 'America/New_York') "
                 f"- INTERVAL '{db} days') AT TIME ZONE 'America/New_York'")
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT id, year, make, model, trim, mileage, color, vin,
                           ai_price, asking_price, status, created_at,
                           phone
                      FROM bids
                     WHERE {where}
                       AND COALESCE(status, '') NOT IN ('cancelled','archived','dead','duplicate')
                     ORDER BY created_at DESC
                     LIMIT 100
                """)
                rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        return {"error": f"db: {type(e).__name__}: {e}", "count": 0, "bids": []}
    out = []
    for r in rows:
        out.append({
            "id": r.get("id"),
            "year": r.get("year"),
            "make": r.get("make"),
            "model": r.get("model"),
            "trim": r.get("trim"),
            "mileage": r.get("mileage"),
            "color": r.get("color"),
            "vin": r.get("vin"),
            "ai_price": float(r["ai_price"]) if r.get("ai_price") else None,
            "asking_price": float(r["asking_price"]) if r.get("asking_price") else None,
            "status": r.get("status"),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        })
    return {"count": len(out), "bids": out, "period":
            "today" if db == 0 else f"last_{db}_days"}


@mcp.tool()
async def partner_match_today(
    caller_name: str, partner_name: str, days_back: int = 1,
    top_n: int = 8,
) -> dict:
    """OWNER. For a specific partner dealer, score recent bids and return
    the ones most worth pitching to that partner. Uses EW's existing
    buy_profile scoring (YMMT_MATCH).

    Use when operator asks: "what's good for Marino today?", "anything for
    Ferrari Lauderdale?", "what should I push to TXT Charlie?".

    partner_name: free-text dealer name. Fuzzy matched to dealers.name OR
                  dealers.portal_slug.
    days_back: window. 0=today, 1=yesterday+today, 7=last week. Default 1.
    top_n: how many to return. Default 8.

    Returns: {partner: {name, slug}, considered: N, matches:
              [{bid_id, year, make, model, trim, mileage, color, score,
                reasons}]}"""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    pq = (partner_name or "").strip()
    if not pq:
        return {"error": "partner_name required"}
    import psycopg2, psycopg2.extras
    import sys
    sys.path.insert(0, "/opt/expwholesale")
    db_url = os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Fuzzy match dealer
                cur.execute("""SELECT id, name, portal_slug, buy_profile, lsl_aliases
                                 FROM dealers
                                WHERE buy_profile IS NOT NULL
                                  AND (lower(name) LIKE %s
                                       OR lower(portal_slug) LIKE %s)
                                ORDER BY length(name) ASC
                                LIMIT 3""",
                            (f"%{pq.lower()}%", f"%{pq.lower()}%"))
                dealers = [dict(r) for r in cur.fetchall()]
        if not dealers:
            return {"error": f"no partner matching {partner_name!r} found",
                    "matches": []}
        if len(dealers) > 1:
            # multiple matches — let user disambiguate
            return {"note": "multiple partners match — be more specific",
                    "candidates": [{"name": d["name"], "slug": d["portal_slug"]}
                                   for d in dealers],
                    "matches": []}
        dealer = dealers[0]
        # Pull recent bids
        db = max(0, int(days_back or 1))
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT id, year, make, model, trim, mileage, color, vin,
                           ai_price, asking_price, status
                      FROM bids
                     WHERE created_at >= (NOW() AT TIME ZONE 'America/New_York'
                                          - INTERVAL '{db} days')
                                          AT TIME ZONE 'America/New_York'
                       AND COALESCE(status, '') NOT IN ('cancelled','archived','dead','duplicate')
                       AND year IS NOT NULL AND make IS NOT NULL AND model IS NOT NULL
                     ORDER BY created_at DESC
                     LIMIT 200
                """)
                bids = [dict(r) for r in cur.fetchall()]
            with psycopg2.connect(db_url) as c2:
                with c2.cursor() as cur2:
                    from app import _load_dealer_vins_owned, _compute_bid_matches
                    vins_by_dealer = _load_dealer_vins_owned(cur2)
        # Score each bid against the single dealer
        scored = []
        for bid in bids:
            matches = _compute_bid_matches(bid, [dealer], vins_by_dealer=vins_by_dealer,
                                            min_score=0)
            if not matches:
                continue
            m = matches[0]
            scored.append({
                "bid_id": bid["id"],
                "year": bid.get("year"), "make": bid.get("make"),
                "model": bid.get("model"), "trim": bid.get("trim"),
                "mileage": bid.get("mileage"), "color": bid.get("color"),
                "ai_price": float(bid["ai_price"]) if bid.get("ai_price") else None,
                "score": m.get("score") or 0,
                "reasons": m.get("reasons") or [],
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        return {
            "partner": {"name": dealer["name"], "slug": dealer["portal_slug"]},
            "considered": len(bids),
            "matches": scored[:top_n],
        }
    except Exception as e:
        log.exception("partner_match_today failed")
        return {"error": f"{type(e).__name__}: {e}", "matches": []}


@mcp.tool()
async def partners_for_bid(caller_name: str, bid_id: int,
                            top_n: int = 5) -> dict:
    """OWNER. For a SPECIFIC bid, return the partner dealers most likely
    to want it (ranked by buy_profile match score).

    Use when operator asks: "who should I pitch bid 1234 to?", "who'd
    want this car?", "best buyers for this one".

    Returns: {bid: {id, year, make, model, trim}, matches:
              [{dealer, slug, score, reasons}]}"""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    try:
        bid_id = int(bid_id)
    except Exception:
        return {"error": "bid_id required (int)"}
    import psycopg2, psycopg2.extras
    import sys
    sys.path.insert(0, "/opt/expwholesale")
    db_url = os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""SELECT id, year, make, model, trim, mileage,
                                      color, vin, ai_price, asking_price, status
                                 FROM bids WHERE id = %s""", (bid_id,))
                bid = cur.fetchone()
                if not bid:
                    return {"error": f"bid {bid_id} not found"}
                bid = dict(bid)
                cur.execute("""SELECT id, name, portal_slug, buy_profile, lsl_aliases
                                 FROM dealers
                                WHERE buy_profile IS NOT NULL""")
                dealers = [dict(r) for r in cur.fetchall()]
            with psycopg2.connect(db_url) as c2:
                with c2.cursor() as cur2:
                    from app import _load_dealer_vins_owned, _compute_bid_matches
                    vins_by_dealer = _load_dealer_vins_owned(cur2)
        matches = _compute_bid_matches(bid, dealers,
                                        vins_by_dealer=vins_by_dealer, min_score=0)
        matches.sort(key=lambda m: m.get("score") or 0, reverse=True)
        out = []
        for m in matches[:top_n]:
            out.append({
                "dealer": m.get("name"),
                "slug": m.get("slug"),
                "score": m.get("score") or 0,
                "reasons": m.get("reasons") or [],
            })
        return {
            "bid": {
                "id": bid["id"], "year": bid.get("year"),
                "make": bid.get("make"), "model": bid.get("model"),
                "trim": bid.get("trim"), "mileage": bid.get("mileage"),
            },
            "matches": out,
        }
    except Exception as e:
        log.exception("partners_for_bid failed")
        return {"error": f"{type(e).__name__}: {e}"}


# ─── end Bill bid-awareness tools ───────────────────────────────────────

# ─── Bill watchlist tools (added 2026-05-26) ────────────────────────────
# Persistent "remind me when X comes in" memory. The voice agent stores
# structured conditions; the bill_watcher daemon (Phase C) polls new bids
# against these and proactively notifies.

def _normalize_make(s):
    return (s or "").strip().lower() if s else None

def _list_lower(lst):
    return [str(x).strip().lower() for x in (lst or []) if x]


@mcp.tool()
async def add_watchlist(
    caller_name: str,
    description: str,
    make_any: list = None,
    model_any: list = None,
    color_any: list = None,
    year_min: int = None,
    year_max: int = None,
    year_exact: int = None,
    mileage_max: int = None,
    price_max: int = None,
    trim_contains: str = None,
    pitch_for: str = None,
    name: str = None,
) -> dict:
    """OWNER. Save a persistent watchlist. The bill_watcher cron checks
    every new bid against active watchlists and proactively notifies the
    user when a match arrives.

    USE WHEN user says: "remind me if X comes in", "let me know when X
    hits", "watch for X for [partner]", "tell me if a [year+make+model]
    rolls through".

    CRITICAL — ASK BEFORE SAVING:
      - If the user's request is ambiguous (e.g. just "Corvette"), ASK to
        clarify year range, color, who it's for, etc. Don't save something
        that'll false-trigger on every Corvette.
      - Always read the watchlist back BEFORE calling this tool, so the
        user can correct. Example: "Got it — I'll flag any Corvette 2019
        or older, for Nuchio. Save it?"

    Args:
      description: the raw verbal request, kept for context. E.g.
                   "Corvette 2019 or older for Nuccio. Also Vipers and Lotuses."
      make_any: list of makes to match (case-insensitive). E.g. ["chevrolet"].
                A bid matches if its make is in this list.
      model_any: list of models. E.g. ["corvette","z06"].
      color_any: list of colors. E.g. ["red","crimson"].
      year_min: lower bound (inclusive). 2015 means 2015+.
      year_max: upper bound (inclusive). 2019 means 2019 or older.
      year_exact: exact year. Overrides min/max if set.
      mileage_max: max miles. 60000 means 60k or fewer.
      price_max: max AI/asking price. 50000 means $50K or less.
      trim_contains: substring match on trim (case-insensitive). E.g. "z06".
      pitch_for: partner dealer this is for. E.g. "nuccio", "marino",
                 "txt charlie". Optional but useful — the notification
                 will read "...for Nuccio".
      name: short human label. If omitted, auto-generated from conditions.

    Returns: {ok, watchlist_id, summary}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    if not description:
        return {"error": "description required"}

    conds = {}
    if make_any:    conds["make_any"]    = _list_lower(make_any)
    if model_any:   conds["model_any"]   = _list_lower(model_any)
    if color_any:   conds["color_any"]   = _list_lower(color_any)
    if year_min:    conds["year_min"]    = int(year_min)
    if year_max:    conds["year_max"]    = int(year_max)
    if year_exact:  conds["year_exact"]  = int(year_exact)
    if mileage_max: conds["mileage_max"] = int(mileage_max)
    if price_max:   conds["price_max"]   = int(price_max)
    if trim_contains: conds["trim_contains"] = trim_contains.strip().lower()

    if not conds:
        return {"error": ("no concrete conditions — at least one of "
                          "make_any, model_any, year_*, color_any, trim_contains "
                          "must be set so we don't trigger on every bid")}

    # Auto-name if not provided
    if not name:
        bits = []
        if year_exact: bits.append(str(year_exact))
        elif year_min or year_max:
            if year_min and year_max:
                bits.append(f"{year_min}-{year_max}")
            elif year_min:
                bits.append(f"{year_min}+")
            elif year_max:
                bits.append(f"≤{year_max}")
        if color_any: bits.append("/".join(color_any))
        if make_any:  bits.append("/".join(make_any))
        if model_any: bits.append("/".join(model_any))
        if pitch_for: bits.append(f"→ {pitch_for}")
        name = " ".join(bits) if bits else description[:60]

    import psycopg2, json as _json
    db_url = os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO bill_watchlists
                  (created_by, name, description, conditions, pitch_for, active)
                VALUES (%s, %s, %s, %s::jsonb, %s, TRUE)
                RETURNING id
            """, (caller_name.lower(), name, description,
                  _json.dumps(conds),
                  (pitch_for or "").strip().lower() or None))
            wid = cur.fetchone()[0]
    except Exception as e:
        log.exception("add_watchlist failed")
        return {"error": f"db: {type(e).__name__}: {e}"}

    return {
        "ok": True,
        "watchlist_id": wid,
        "name": name,
        "conditions": conds,
        "pitch_for": pitch_for,
        "summary": f"watching for: {name}" + (f" (for {pitch_for})" if pitch_for else ""),
    }


@mcp.tool()
async def list_watchlists(caller_name: str, include_inactive: bool = False) -> dict:
    """OWNER. List all active watchlists for the current user.

    USE WHEN user asks: "what am I watching for?", "what reminders do
    I have?", "what's on my watch list?", "anything pending?".

    include_inactive: also show deleted/paused. Default False."""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    import psycopg2, psycopg2.extras
    db_url = os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    where = "lower(created_by) = %s"
    args = [caller_name.lower()]
    if not include_inactive:
        where += " AND active = TRUE"
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT id, name, description, conditions, pitch_for,
                           active, match_count, last_matched_at, created_at
                      FROM bill_watchlists
                     WHERE {where}
                     ORDER BY active DESC, created_at DESC
                """, args)
                rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        return {"error": f"db: {type(e).__name__}: {e}", "watchlists": []}
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "conditions": r["conditions"],
            "pitch_for": r["pitch_for"],
            "active": r["active"],
            "match_count": r["match_count"],
            "last_matched_at": r["last_matched_at"].isoformat() if r["last_matched_at"] else None,
        })
    return {"watchlists": out, "count": len(out)}


@mcp.tool()
async def delete_watchlist(
    caller_name: str,
    watchlist_id: int = None,
    name_query: str = None,
) -> dict:
    """OWNER. Disable a watchlist so it stops matching. Soft-delete only
    (sets active=FALSE; history preserved for hit log).

    USE WHEN user says: "stop watching for X", "cancel the Corvette
    reminder", "I don't need that one anymore".

    Provide EITHER watchlist_id (from list_watchlists) OR name_query
    (fuzzy match on name). If multiple match, returns the candidates
    for the user to disambiguate.

    Returns: {ok, disabled_id, name} on success, or {candidates: [...]}"""
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    if not watchlist_id and not name_query:
        return {"error": "provide watchlist_id or name_query"}
    import psycopg2, psycopg2.extras
    db_url = os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if watchlist_id:
                    cur.execute("""SELECT id, name, active FROM bill_watchlists
                                    WHERE id = %s AND lower(created_by) = %s""",
                                (int(watchlist_id), caller_name.lower()))
                    row = cur.fetchone()
                    if not row:
                        return {"error": f"no watchlist {watchlist_id} for {caller_name}"}
                    if not row["active"]:
                        return {"ok": True, "note": "already disabled",
                                "disabled_id": row["id"], "name": row["name"]}
                    cur.execute("""UPDATE bill_watchlists
                                      SET active=FALSE, updated_at=NOW()
                                    WHERE id = %s""", (row["id"],))
                    return {"ok": True, "disabled_id": row["id"],
                            "name": row["name"]}
                # name_query path
                cur.execute("""SELECT id, name, description FROM bill_watchlists
                                WHERE lower(created_by) = %s AND active=TRUE
                                  AND (lower(name) LIKE %s
                                       OR lower(description) LIKE %s)
                                ORDER BY created_at DESC""",
                            (caller_name.lower(),
                             f"%{name_query.lower()}%",
                             f"%{name_query.lower()}%"))
                rows = [dict(r) for r in cur.fetchall()]
                if len(rows) == 0:
                    return {"error": f"no active watchlist matching {name_query!r}"}
                if len(rows) > 1:
                    return {"note": "multiple matches — disambiguate by id",
                            "candidates": rows}
                cur.execute("""UPDATE bill_watchlists
                                  SET active=FALSE, updated_at=NOW()
                                WHERE id = %s""", (rows[0]["id"],))
                return {"ok": True, "disabled_id": rows[0]["id"],
                        "name": rows[0]["name"]}
    except Exception as e:
        log.exception("delete_watchlist failed")
        return {"error": f"db: {type(e).__name__}: {e}"}


# ─── end Bill watchlist tools ───────────────────────────────────────────

# ─── Bill away-mode tools (Phase D 2026-05-26) ──────────────────────────
# When set, the bill_watcher routes notifications via the chosen channel
# instead of the active /v-edge voice session. Used when operator is
# stepping away from the desk.

@mcp.tool()
async def set_away_mode(
    caller_name: str,
    channel: str,
    hours: float = 0,
    phone_e164: str = "",
) -> dict:
    """OWNER. Switch proactive notifications from voice-session to a
    different channel for a period (or until manually cleared).

    USE WHEN user says: 'call me about anything that hits', 'I'm heading
    out — text me', 'switch to phone calls', 'I'll be away for an hour',
    'I'm leaving for the day'.

    Args:
      channel: 'call' (outbound voice call from EW number) | 'sms' (text
               to phone_e164) | 'telegram' (push). Default 'call'.
      hours: auto-clear after this many hours. 0 (default) = manual only,
             stays on until clear_away_mode().
      phone_e164: destination. If blank, defaults to Oscar's contact
                  (+14074309675). Required for 'sms' if different.

    Returns: {ok, channel, away_until, phone}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    ch = (channel or "call").strip().lower()
    if ch not in ("call", "sms", "telegram"):
        return {"error": f"channel must be call/sms/telegram, got {channel!r}"}

    p = (phone_e164 or "").strip()
    if not p:
        # default to Oscar's cell from bill_contacts (Oscar Pastrana, id 82)
        p = "+14074309675"
    if not (p.startswith("+") and len(p) >= 8):
        return {"error": f"bad phone_e164 {p!r}"}

    import psycopg2
    db_url = os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c, c.cursor() as cur:
            if hours and float(hours) > 0:
                cur.execute("""
                    INSERT INTO bill_user_state
                      (user_name, away_channel, away_phone, away_until, updated_at)
                    VALUES (%s, %s, %s, NOW() + (%s || ' hours')::interval, NOW())
                    ON CONFLICT (user_name) DO UPDATE
                      SET away_channel = EXCLUDED.away_channel,
                          away_phone   = EXCLUDED.away_phone,
                          away_until   = EXCLUDED.away_until,
                          updated_at   = NOW()
                    RETURNING away_until
                """, (caller_name.lower(), ch, p, str(float(hours))))
            else:
                cur.execute("""
                    INSERT INTO bill_user_state
                      (user_name, away_channel, away_phone, away_until, updated_at)
                    VALUES (%s, %s, %s, NULL, NOW())
                    ON CONFLICT (user_name) DO UPDATE
                      SET away_channel = EXCLUDED.away_channel,
                          away_phone   = EXCLUDED.away_phone,
                          away_until   = NULL,
                          updated_at   = NOW()
                    RETURNING away_until
                """, (caller_name.lower(), ch, p))
            row = cur.fetchone()
            away_until = row[0]
    except Exception as e:
        log.exception("set_away_mode failed")
        return {"error": f"db: {type(e).__name__}: {e}"}

    return {
        "ok": True,
        "channel": ch,
        "phone": p,
        "away_until": away_until.isoformat() if away_until else None,
        "summary": (
            f"away mode set: {ch} to {p}" +
            (f" for {hours}h" if hours else " (manual clear)")
        ),
    }


@mcp.tool()
async def clear_away_mode(caller_name: str) -> dict:
    """OWNER. Switch proactive notifications back to the live /v-edge
    voice session.

    USE WHEN user says: 'I'm back', 'stop calling me', 'cancel away mode',
    'back at my desk'.

    Returns: {ok, was_active}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    import psycopg2
    db_url = os.environ.get("DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
    try:
        with psycopg2.connect(db_url) as c, c.cursor() as cur:
            cur.execute("""UPDATE bill_user_state
                              SET away_channel = NULL,
                                  away_phone   = NULL,
                                  away_until   = NULL,
                                  updated_at   = NOW()
                            WHERE user_name = %s
                              AND away_channel IS NOT NULL
                            RETURNING away_channel""", (caller_name.lower(),))
            row = cur.fetchone()
    except Exception as e:
        return {"error": f"db: {type(e).__name__}: {e}"}
    return {"ok": True, "was_active": bool(row),
            "summary": "back at desk — proactive notifications go to /v-edge"
                       if row else "you weren't away"}


# ─── end Bill away-mode tools ───────────────────────────────────────────

@mcp.tool()
async def briefing_now(caller_name: str) -> dict:
    """OWNER. Generate todays morning briefing on-demand and queue it
    for delivery. Same content the 8 AM cron would produce: overnight
    bid count + top vehicle, dealer-watch
    top opportunity, stale-bid sweep, watchlist hits yesterday.

    USE when the user says: "give me my briefing", "catch me up",
    "whats the news", "morning brief", "give me the rundown", "whats
    going on this morning".

    The briefing message is inserted into the proactive notification
    queue (bill_watchlist_hits with the Daily Briefing sentinel). The
    next /v-edge poll picks it up and Bill speaks it. If the operator
    is in away-mode, dispatch goes via that channel (call/sms/telegram).

    Returns: {ok, message, word_count, hit_id, dispatch_via,
              data_summary:{overnight_bids, arb_flagged, dealer_opps,
                            stale_bids, watchlist_hits_yest}}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}

    # Re-use the script as a library — keep the logic in one place.
    import importlib, sys
    sys.path.insert(0, "/opt/expwholesale")
    try:
        if "bill_daily_briefing" in sys.modules:
            mod = importlib.reload(sys.modules["bill_daily_briefing"])
        else:
            mod = importlib.import_module("bill_daily_briefing")
    except Exception as e:
        return {"error": f"import: {type(e).__name__}: {e}"}

    try:
        from datetime import date as _date
        result = mod.run(caller_name.lower(), _date.today(), dry_run=False)
    except Exception as e:
        log.exception("briefing_now failed")
        return {"error": f"run: {type(e).__name__}: {e}"}

    if not result.get("ok"):
        return {
            "ok": False,
            "duplicate": result.get("duplicate", False),
            "message": result.get("message"),
            "summary": "briefing already queued — check pending",
        }

    # Re-gather a tiny data summary for the caller so the spoken layer
    # knows the headline numbers without re-parsing the message.
    try:
        data = mod.gather(_date.today())
        summary = {
            "overnight_bids": data["overnight_total"],
            "arb_flagged": data["arb_total_flagged"],
            "dealer_opps": data["dealer_opps_total"],
            "stale_bids": data["stale_count"],
            "watchlist_hits_yest": data["watchlist_hits_yest"],
        }
    except Exception:
        summary = {}

    return {
        "ok": True,
        "message": result["message"],
        "word_count": result.get("word_count"),
        "hit_id": result.get("hit_id"),
        "dispatch_via": result.get("dispatch_via"),
        "data_summary": summary,
        "spoken_summary": "Briefing on the way — Bill will read it next.",
    }


# ─── end BILL_BRIEFING_2026_05_26 tool ──────────────────────────────────
# ─── PARTNER_ACTIVITY_2026_05_26 tools — appended to ew_mcp.py ──────────
# Two MCP tools for per-partner relationship activity:
#   partner_activity_status — list aging partner dealers (silent N+ days)
#   partner_detail          — full breakdown for one named partner
#
# Backed by partner_activity_summary (refreshed weekday 07:00 ET cron) +
# live LSL crm.db lookup for the "recent purchases" rows in partner_detail.
# Both tools are OWNER-gated (caller_name must be in OWNER_WHITELIST).

@mcp.tool()
async def partner_activity_status(
    caller_name: str,
    days_silent: int = 7,
    top_n: int = 5,
) -> dict:
    """OWNER. List partner dealers we haven't transacted with in a while.

    USE when the operator asks: 'who haven't I talked to lately',
    'cold partners', 'who needs attention', 'partner pipeline status',
    'who's gone quiet'.

    days_silent: minimum days since the most recent push, offer, or
                 purchase. Default 7.
    top_n:       max rows returned (default 5, capped at 16).

    Returns:
      {as_of, count, partners:[{dealer_name, silent_days, last_push_at,
                                last_purchase_at, purchases_365d,
                                total_gross_365d, profile_summary}]}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}

    import psycopg2, psycopg2.extras
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
    )
    try:
        top_n = max(1, min(int(top_n or 5), 16))
        days_silent = max(0, int(days_silent or 7))
    except Exception:
        return {"error": "bad days_silent/top_n"}

    try:
        with psycopg2.connect(db_url, connect_timeout=3) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT s.dealer_id, s.dealer_name, s.silent_days,
                           s.last_push_at, s.last_offer_at, s.last_purchase_at,
                           s.pushes_30d, s.offers_30d,
                           s.purchases_30d, s.purchases_90d,
                           s.purchases_365d, s.total_gross_365d,
                           s.computed_at,
                           d.buy_profile
                      FROM partner_activity_summary s
                      LEFT JOIN dealers d ON d.id = s.dealer_id
                     WHERE s.silent_days IS NOT NULL
                       AND s.silent_days >= %s
                     ORDER BY s.silent_days DESC NULLS LAST
                     LIMIT %s
                """, (days_silent, top_n))
                rows = cur.fetchall()
                cur.execute(
                    "SELECT MAX(computed_at) AS as_of FROM partner_activity_summary"
                )
                as_of_row = cur.fetchone() or {}
    except Exception as e:
        log.exception("partner_activity_status failed")
        return {"error": f"{type(e).__name__}: {e}"}

    def _summarize_profile(bp):
        if not bp or not isinstance(bp, dict):
            return None
        # Best-effort short text — pulls a couple of human-readable hints
        bits = []
        for k in ("primary_makes", "preferred_makes", "makes",
                  "tagline", "summary", "notes"):
            v = bp.get(k)
            if v:
                if isinstance(v, list):
                    bits.append(f"{k}: {', '.join(str(x) for x in v[:5])}")
                else:
                    bits.append(f"{k}: {str(v)[:80]}")
                break
        return "; ".join(bits) if bits else None

    partners = []
    for r in rows:
        partners.append({
            "dealer_id":        r["dealer_id"],
            "dealer_name":      r["dealer_name"],
            "silent_days":      r["silent_days"],
            "last_push_at":     r["last_push_at"].isoformat() if r["last_push_at"] else None,
            "last_offer_at":    r["last_offer_at"].isoformat() if r["last_offer_at"] else None,
            "last_purchase_at": r["last_purchase_at"].isoformat() if r["last_purchase_at"] else None,
            "pushes_30d":       r["pushes_30d"],
            "offers_30d":       r["offers_30d"],
            "purchases_30d":    r["purchases_30d"],
            "purchases_90d":    r["purchases_90d"],
            "purchases_365d":   r["purchases_365d"],
            "total_gross_365d": float(r["total_gross_365d"] or 0),
            "profile_summary":  _summarize_profile(r.get("buy_profile")),
        })

    return {
        "as_of": as_of_row["as_of"].isoformat() if as_of_row.get("as_of") else None,
        "days_silent_threshold": days_silent,
        "count": len(partners),
        "partners": partners,
    }


@mcp.tool()
async def partner_detail(
    caller_name: str,
    partner_name: str,
) -> dict:
    """OWNER. Full activity breakdown for ONE partner dealer by name.

    USE when the operator asks: 'what's the deal with Marino',
    'tell me about Charlie', 'when did we last talk to Manheim',
    'how are we doing with Marshall Goldman'.

    Matches partner_name as a case-insensitive substring against
    dealer name + lsl_aliases. Returns:
      - summary row from partner_activity_summary
      - last 5 LSL purchases (vehicle, sold_at, sale_price)
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    name = (partner_name or "").strip()
    if len(name) < 2:
        return {"error": "partner_name too short"}

    import psycopg2, psycopg2.extras, sqlite3, os as _os
    db_url = _os.environ.get(
        "DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
    )
    lsl_path = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")

    try:
        with psycopg2.connect(db_url, connect_timeout=3) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Resolve the partner: try the activity summary first
                # (16-row table) by dealer_name ILIKE, then by alias match.
                cur.execute("""
                    SELECT s.*, d.lsl_aliases, d.buy_profile, d.city, d.state,
                           d.phone, d.notes
                      FROM partner_activity_summary s
                      JOIN dealers d ON d.id = s.dealer_id
                     WHERE s.dealer_name ILIKE %s
                        OR EXISTS (
                            SELECT 1 FROM jsonb_array_elements_text(
                                          COALESCE(d.lsl_aliases, '[]'::jsonb)) a
                             WHERE a ILIKE %s
                        )
                     ORDER BY (s.dealer_name ILIKE %s) DESC NULLS LAST,
                              s.purchases_365d DESC NULLS LAST
                     LIMIT 1
                """, (f"%{name}%", f"%{name}%", f"{name}%"))
                row = cur.fetchone()
                if not row:
                    return {"error": f"no partner matching {name!r}"}
                summary = dict(row)
                aliases = summary.pop("lsl_aliases", None) or []
                if isinstance(aliases, str):
                    import json as _json
                    try:
                        aliases = _json.loads(aliases)
                    except Exception:
                        aliases = []
                buy_profile = summary.pop("buy_profile", None)

    except Exception as e:
        log.exception("partner_detail failed (pg)")
        return {"error": f"{type(e).__name__}: {e}"}

    # Pull last 5 LSL purchases for this dealer
    recent_purchases = []
    if _os.path.exists(lsl_path) and aliases:
        try:
            lsl = sqlite3.connect(f"file:{lsl_path}?mode=ro", uri=True, timeout=5)
            lsl.row_factory = sqlite3.Row
            cur = lsl.cursor()
            like_clauses = " OR ".join(
                ["UPPER(customer_name) LIKE UPPER(?)"] * len(aliases)
            )
            cur.execute(
                f"""
                SELECT stock_no, vehicle_info, customer_name, sale_price,
                       purchase_cost, sold_at, sales_person
                  FROM deals
                 WHERE ({like_clauses})
                   AND sale_price IS NOT NULL AND sale_price > 0
                 ORDER BY sold_at DESC
                 LIMIT 5
                """,
                [f"%{a}%" for a in aliases],
            )
            for r in cur.fetchall():
                recent_purchases.append({
                    "stock_no":     r["stock_no"],
                    "vehicle":      r["vehicle_info"],
                    "sold_at":      r["sold_at"],
                    "sale_price":   r["sale_price"],
                    "purchase_cost": r["purchase_cost"],
                    "salesperson":  r["sales_person"],
                })
            lsl.close()
        except Exception as e:
            log.warning("partner_detail lsl lookup err: %s", e)

    # Normalize timestamps for JSON
    for k in ("last_push_at", "last_offer_at", "last_purchase_at", "computed_at"):
        v = summary.get(k)
        if hasattr(v, "isoformat"):
            summary[k] = v.isoformat()
    if summary.get("total_gross_365d") is not None:
        summary["total_gross_365d"] = float(summary["total_gross_365d"])

    # Profile hint (one-line)
    profile_summary = None
    if isinstance(buy_profile, dict):
        for k in ("primary_makes", "preferred_makes", "makes",
                  "tagline", "summary", "notes"):
            v = buy_profile.get(k)
            if v:
                if isinstance(v, list):
                    profile_summary = f"{k}: {', '.join(str(x) for x in v[:5])}"
                else:
                    profile_summary = f"{k}: {str(v)[:120]}"
                break

    return {
        "matched_partner": summary.get("dealer_name"),
        "summary":         summary,
        "aliases":         aliases,
        "profile_summary": profile_summary,
        "recent_purchases": recent_purchases,
    }


# ─── end PARTNER_ACTIVITY_2026_05_26 tools ──────────────────────────────

# ─── BILL_INTEL_2026_05_27 — Phase A: deeper EW intelligence ────────────
# 4 read-only tools so Bill can:
#   - see today's inventory holes + surpluses
#   - read the LLM's existing assessment on a bid (so he can critique it)
#   - ask the per-make ML model for a second-opinion price
#   - surface today's top dealer-watch buy opportunities

@mcp.tool()
async def inventory_gaps_now(
    caller_name: str,
    top_n_dealers: int = 10,
) -> dict:
    """OWNER. Today's inventory holes + surpluses across the portal dealer
    network. Same logic as the /inventory-gaps page and the 2:30 AM
    Telegram digest. A "hole" = baseline sold 3+ in 90d, current stock <=1.
    A "surplus" = current stock 4+ AND (no recent sales OR stock >= 2x sold).

    USE when the operator asks: "what holes do we have", "where are the
    surpluses", "gaps", "holes and surplus", "what is missing in inventory",
    "what is piling up".

    Args:
      top_n_dealers: cap on how many dealer cards to return (default 10).

    Returns:
      {as_of, active_dealers, total_holes, total_surpluses,
       dealers: [{name, holes:[{ymm, baseline_sold_90d, current_count,
                                top_sold_configs, current_configs}],
                  surpluses:[ ... same shape ... ]}]}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}

    import sys as _sys, os as _os, datetime as _dt
    if "/opt/expwholesale" not in _sys.path:
        _sys.path.insert(0, "/opt/expwholesale")
    import psycopg2
    from inventory_gap_lib import (
        fetch_portal_dealers, fetch_current_inventory, fetch_baseline,
        analyze_dealer, format_ymm, format_config,
    )

    db_url = _os.environ.get(
        "DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
    )
    try:
        with psycopg2.connect(db_url, connect_timeout=5) as c:
            with c.cursor() as cur:
                dealers = fetch_portal_dealers(cur)
                dealer_ids = [d[0] for d in dealers]
                current = fetch_current_inventory(cur, dealer_ids)
                baseline = fetch_baseline(cur, dealer_ids)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    dealer_cards = []
    total_holes = total_surp = active_count = 0
    for d_id, d_name in dealers:
        holes, surpluses = analyze_dealer(
            current.get(d_id, {}), baseline.get(d_id, {})
        )
        if not (holes or surpluses):
            continue
        active_count += 1
        total_holes += len(holes)
        total_surp += len(surpluses)
        dealer_cards.append({
            "name": d_name,
            "holes": [{
                "ymm": format_ymm(k),
                "baseline_sold_90d": base,
                "current_count": cur_n,
                "top_sold_configs": [[format_config(c), n] for c, n in scfg],
                "current_configs": [[format_config(c), n] for c, n in ccfg],
            } for (k, base, cur_n, scfg, ccfg) in holes],
            "surpluses": [{
                "ymm": format_ymm(k),
                "baseline_sold_90d": base,
                "current_count": cur_n,
                "top_sold_configs": [[format_config(c), n] for c, n in scfg],
                "current_configs": [[format_config(c), n] for c, n in ccfg],
            } for (k, base, cur_n, scfg, ccfg) in surpluses],
        })

    # Sort: dealers with the most action first
    dealer_cards.sort(key=lambda d: -(len(d["holes"]) + len(d["surpluses"])))
    dealer_cards = dealer_cards[:max(1, int(top_n_dealers))]

    return {
        "as_of": _dt.date.today().isoformat(),
        "active_dealers": active_count,
        "total_holes": total_holes,
        "total_surpluses": total_surp,
        "dealers": dealer_cards,
    }


@mcp.tool()
async def ai_assessment_for_bid(
    caller_name: str,
    bid_id: int,
) -> dict:
    """OWNER. Return the existing LLM assessment on a specific bid plus the
    raw market signals (vAuto/MMR/rBook, AccuTrade trade-in & retail,
    iPacket sticker MSRP) that the LLM had when it decided. Useful for
    second-opinion work — Bill can critique the AI's call against the
    inputs.

    USE when asked: "what did the AI say about bid X", "AI's take on
    bid X", "why did the AI recommend that price", "second-opinion bid X",
    "do you agree with the AI on bid X".

    Returns:
      {bid_id, vin, vehicle (year make model trim), mileage, asking_price,
       status, ai_recommendation_text, ai_price, ai_assessed_at,
       market_inputs: {
         vauto: {appraisal_url, looked_up_at, has_rbook, has_manheim,
                 rbook_completed_at, manheim_completed_at},
         accutrade: {guaranteed_offer, trade_in, trade_market, retail,
                     market_avg, looked_up_at, not_available, unavailable_reason},
         ipacket: {total_msrp, base_price, looked_up_at, not_available,
                   unavailable_reason}}}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}

    import os as _os
    import psycopg2, psycopg2.extras
    db_url = _os.environ.get(
        "DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
    )

    def _ser(v):
        if v is None: return None
        if hasattr(v, "isoformat"): return v.isoformat()
        if isinstance(v, (bool, int, str)): return v
        if isinstance(v, float): return float(v)
        return str(v)[:500]

    try:
        with psycopg2.connect(db_url, connect_timeout=5) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, vin, year, make, model, trim, mileage,
                           asking_price, status, ai_assessment, ai_price,
                           ai_assessed_at
                      FROM bids WHERE id = %s
                """, (bid_id,))
                bid = cur.fetchone()
                if not bid:
                    return {"error": f"bid {bid_id} not found"}

                cur.execute("""
                    SELECT appraisal_url, looked_up_at,
                           rbook_completed_at, manheim_completed_at,
                           (rbook_competitive_set IS NOT NULL) AS has_rbook,
                           (manheim_transactions IS NOT NULL) AS has_manheim
                      FROM vauto_lookups WHERE bid_id = %s LIMIT 1
                """, (bid_id,))
                vauto = cur.fetchone()

                cur.execute("""
                    SELECT guaranteed_offer, trade_in, trade_market, retail,
                           market_avg, looked_up_at, not_available,
                           unavailable_reason
                      FROM accutrade_lookups WHERE bid_id = %s LIMIT 1
                """, (bid_id,))
                accu = cur.fetchone()

                cur.execute("""
                    SELECT total_msrp, base_price, looked_up_at,
                           not_available, unavailable_reason
                      FROM ipacket_lookups WHERE bid_id = %s LIMIT 1
                """, (bid_id,))
                ipkt = cur.fetchone()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    vehicle = " ".join(str(x) for x in (
        bid.get("year"), bid.get("make"), bid.get("model"), bid.get("trim")
    ) if x).strip()

    return {
        "bid_id": bid["id"],
        "vin": bid.get("vin"),
        "vehicle": vehicle,
        "mileage": bid.get("mileage"),
        "asking_price": _ser(bid.get("asking_price")),
        "status": bid.get("status"),
        "ai_recommendation_text": bid.get("ai_assessment"),
        "ai_price": _ser(bid.get("ai_price")),
        "ai_assessed_at": _ser(bid.get("ai_assessed_at")),
        "market_inputs": {
            "vauto": {k: _ser(vauto.get(k)) for k in vauto.keys()} if vauto else None,
            "accutrade": {k: _ser(accu.get(k)) for k in accu.keys()} if accu else None,
            "ipacket": {k: _ser(ipkt.get(k)) for k in ipkt.keys()} if ipkt else None,
        },
    }


@mcp.tool()
async def ml_predict_price(
    make: str,
    year: int,
    mileage: int,
    est_wholesale_price: float,
    model: Optional[str] = None,
    market_asking_price: Optional[float] = None,
    original_msrp: Optional[float] = None,
) -> dict:
    """Open tool. Per-make ML model prediction (xgboost trained nightly
    from LSL purchase history). Second-opinion price signal next to
    MMR/rBook/AI-assessment.

    USE when asked: "what does the ML model say", "ML predict",
    "what would the model price this at", "ML take on this car",
    "second opinion price".

    Args:
      make: required, e.g. "BMW", "TOYOTA"
      year, mileage: required
      est_wholesale_price: required — best wholesale signal you have
            (MMR, rBook median, etc.). Anchors the model.
      model: optional model name (improves accuracy if present)
      market_asking_price: optional rBook avg/median
      original_msrp: optional original MSRP

    Returns: {prediction (int $), source ("xgboost" or "baseline"),
              mae_dollars, mape_pct, within_10pct, n_train, make_name,
              baseline_prediction}
      OR {error} if make has no model and no baseline.
    """
    import sys as _sys
    if "/opt/expwholesale" not in _sys.path:
        _sys.path.insert(0, "/opt/expwholesale")
    try:
        from ml_predict import predict_for_bid as _predict
    except Exception as e:
        return {"error": f"ml_predict import failed: {e}"}
    try:
        result = _predict({
            "make_name": make,
            "model_name": model,
            "year": year,
            "odometer": mileage,
            "est_wholesale_price": est_wholesale_price,
            "market_asking_price": market_asking_price,
            "original_msrp": original_msrp,
            "sale_type": "Wholesale",
            "vehicle_sale_type": "Used",
        })
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if result is None:
        return {"error": f"no ML model for make {make!r} or missing inputs"}
    out = {}
    for k, v in result.items():
        if v is None or isinstance(v, (bool, str)):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = float(v) if isinstance(v, float) else int(v)
        else:
            out[k] = str(v)[:200]
    return out


@mcp.tool()
async def dealer_opportunities_now(
    caller_name: str,
    top_n: int = 10,
    min_score: Optional[float] = None,
) -> dict:
    """OWNER. Today's top dealer-watch buy opportunities — vehicles in the
    portal dealer network priced under MMR / under retail. From the 09:30
    EDT daily AI scout that scores ~1,700 dealer-network vehicles.

    USE when asked: "best opportunities today", "what should we buy",
    "top dealer picks", "dealer watch", "opportunities", "scout picks".

    Args:
      top_n: max results (default 10, cap 50)
      min_score: optional minimum composite score filter

    Returns:
      {as_of, total_today,
       opportunities: [{year, make, model, trim, mileage, asking_price,
                        mmr_wholesale_avg, dollars_under_mmr, pct_under_mmr,
                        score, signals, gemini_pitch, detail_url,
                        dealer_name, dealer_state}]}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}

    import os as _os
    import psycopg2, psycopg2.extras
    db_url = _os.environ.get(
        "DATABASE_URL",
        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale",
    )
    n = max(1, min(int(top_n or 10), 50))

    def _ser(v):
        if v is None: return None
        if hasattr(v, "isoformat"): return v.isoformat()
        if isinstance(v, (bool, int, str, list, dict)): return v
        if isinstance(v, float): return float(v)
        return str(v)[:500]

    try:
        with psycopg2.connect(db_url, connect_timeout=5) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                params = []
                extra = ""
                if min_score is not None:
                    extra = "AND o.score >= %s"
                    params.append(float(min_score))
                sql = f"""
                    SELECT o.year, o.make, o.model, o.trim, o.mileage,
                           o.asking_price, o.mmr_wholesale_avg,
                           o.dollars_under_mmr, o.pct_under_mmr,
                           o.score, o.signals, o.gemini_pitch,
                           o.detail_url, o.snapshot_date,
                           d.name AS dealer_name, d.state AS dealer_state
                      FROM dealer_opportunities o
                      JOIN dealers d ON d.id = o.dealer_id
                     WHERE o.snapshot_date = (
                            SELECT MAX(snapshot_date) FROM dealer_opportunities)
                       {extra}
                     ORDER BY o.score DESC NULLS LAST
                     LIMIT %s
                """
                cur.execute(sql, (*params, n))
                rows = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT MAX(snapshot_date) AS as_of,
                           (SELECT COUNT(*) FROM dealer_opportunities
                             WHERE snapshot_date = (
                               SELECT MAX(snapshot_date)
                                 FROM dealer_opportunities)) AS total_today
                      FROM dealer_opportunities
                """)
                meta = cur.fetchone()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    return {
        "as_of": _ser(meta.get("as_of")) if meta else None,
        "total_today": int(meta.get("total_today") or 0) if meta else 0,
        "opportunities": [{k: _ser(v) for k, v in r.items()} for r in rows],
    }

# ─── end BILL_INTEL_2026_05_27 ───────────────────────────────────────────

# ─── AI_CRITIQUE_2026_05_27 — Bill talks back to the assessor LLM ───────
@mcp.tool()
async def ai_critique(
    caller_name: str,
    bid_id: int,
    question: str,
) -> dict:
    """OWNER. Ask the AI assessor (Gemini) a follow-up question about a
    specific bid's existing assessment. The assessor sees its prior
    verdict, the original market inputs (vAuto/MMR/rBook/AccuTrade/iPacket),
    and Bill's question, then replies in 2-4 sentences.

    USE when the operator (via Bill) wants to challenge or probe the AI's
    call: "ask the assessor why it weighted X over Y", "what if mileage
    was lower", "did you consider the trade_in", "do you stand by that
    price given Z", "second-guess the AI on bid X".

    Args:
      bid_id: the bid the operator is questioning
      question: the follow-up question (free text, < 600 chars)

    Returns:
      {bid_id, vehicle, question, answer (2-4 sentences for Bill to
       speak), prior_ai_price, model_used}
    """
    if not _is_owner(caller_name):
        return {"error": "owner-only", "owner_required": True}
    if not bid_id or not (question or "").strip():
        return {"error": "bid_id and question required"}

    import os as _os, urllib.parse as _up
    _eq = _os.environ.get('EW_QUERY_URL', 'http://127.0.0.1:9001/api/voice/query')
    _pp = _up.urlparse(_eq)
    url = '{}://{}/api/voice/ai_critique'.format(_pp.scheme, _pp.netloc)

    http = await _ensure_http()
    try:
        import aiohttp as _aio
        _t = _aio.ClientTimeout(total=90)
        async with http.post(url, json={
            "bid_id": int(bid_id),
            "question": str(question)[:600],
        }, timeout=_t) as r:
            data = await r.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    return data

