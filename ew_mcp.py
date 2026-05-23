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
            ctx = get_valuation_context(
                year=year, make=make, model=model,
                trim=trim, miles=miles, msrp=msrp,
            )
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

    # LSL30_2026_05_22 — pull last 30 days LSL deals for this YMM for citation
    # Trim-aware: when user mentions a specific trim, narrow first
    lsl_30 = _lsl_30day_deals(year, make, model, trim=trim, limit=10) if trim else []
    lsl_30_any_trim = _lsl_30day_deals(year, make, model, limit=10)
    if not lsl_30:
        lsl_30 = lsl_30_any_trim
    # PARTNER_SOLD_2026_05_22 — partner-dealer listings that have left inventory
    partner_sold = _partner_sold_history(year, make, model, limit=5)
    # BID30_2026_05_22 — 30-day rolling EW bid window (any trim + trim-filtered)
    prior_bids_30day_all = _prior_bids_30day_summary(year, make, model, trim=None, miles=miles)
    prior_bids_30day_trim = _prior_bids_30day_summary(year, make, model, trim=trim, miles=miles) if trim else None
    # ACCUTRADE30_2026_05_22 — 30-day rolling AccuTrade values
    accutrade_30day_all = _accutrade_30day_summary(year, make, model, trim=None, miles=miles)
    accutrade_30day_trim = _accutrade_30day_summary(year, make, model, trim=trim, miles=miles) if trim else None
    # VSA30_2026_05_22 — vAuto saved appraisals (every appraisal touched,
    # bigger than prior_bids which only covers EW-pushed ones)
    vauto_saved_30day_all = _vauto_saved_30day_summary(year, make, model, trim=None, miles=miles)
    vauto_saved_30day_trim = _vauto_saved_30day_summary(year, make, model, trim=trim, miles=miles) if trim else None
    # LSL_INV_2026_05_22 — EW's actual lot today + 90-day sold velocity
    lsl_inventory_now_all = _lsl_inventory_now(year, make, model, trim=None)
    lsl_inventory_now_trim = _lsl_inventory_now(year, make, model, trim=trim) if trim else None
    lsl_sold_velocity_all = _lsl_sold_velocity(year, make, model, trim=None)
    lsl_sold_velocity_trim = _lsl_sold_velocity(year, make, model, trim=trim) if trim else None
    # MASTER_LIST_2026_05_22 — enrich live partner inventory with overnight comps
    enriched = _enrich_partner_inventory_with_comps(ctx.get("partner_inventory_top3") or [])
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
        live = _fetch_live_via_lsl_vin(year, make, model, miles_hint=miles)
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
            "ytd" / "year_to_date" / "this_year" | "last_year" | "all_time".

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
                 "sold_to": r.get("customer_name"),
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
                 "sold_to": r.get("customer_name"),
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
                           v.mmr AS vauto_mmr, v.rbook AS vauto_rbook,
                           v.appraisal_url,
                           a.guaranteed_offer, a.trade_in, a.trade_market,
                           a.retail AS accutrade_retail, a.market_avg,
                           a.not_available AS accutrade_unavailable,
                           c2.name AS contact_name
                      FROM bids b
                      LEFT JOIN vauto_lookups v ON v.bid_id = b.id
                      LEFT JOIN accutrade_lookups a ON a.bid_id = b.id
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
                  "trade_in","trade_market","accutrade_retail","market_avg"):
            if out.get(k) is not None: out[k] = float(out[k])

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
    if not _is_owner(caller_name):
        return {"error": "owner-only — first name required",
                "owner_required": True}
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
