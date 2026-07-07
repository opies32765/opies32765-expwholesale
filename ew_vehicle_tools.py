"""EW dashboard vehicle tools for Anna: list all dashboard vehicles, and full per-vehicle detail
(MSRP + options from iPacket, Carfax/AutoCheck damage, vAuto MMR, AccuTrade, book values, AI price).
READ ONLY — reuses the stored enrichment (ipacket_lookups etc.); NEVER triggers an iPacket pull."""
import os as _os, json as _json, psycopg2, psycopg2.extras, asyncio as _a


def _dburl():
    u = _os.environ.get("DATABASE_URL")
    if not u:
        raise RuntimeError("DATABASE_URL not set")
    return u


def _rows(sql, args=()):
    with psycopg2.connect(_dburl()) as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]


def _resolve_bid(query, vin, bid_id):
    if bid_id:
        return int(bid_id)
    q = (vin or query or "").strip()
    if q.isdigit() and len(q) <= 7:
        return int(q)  # a bid id
    like = f"%{q}%"
    rows = _rows("""select id from bids
                    where vin ilike %s
                       or (coalesce(year::text,'')||' '||coalesce(make,'')||' '||coalesce(model,'')||' '||coalesce(trim,'')) ilike %s
                    order by created_at desc limit 1""", (like, like))
    return rows[0]["id"] if rows else None


def _num(v):
    try:
        return round(float(v)) if v is not None else None
    except Exception:
        return v


async def vehicle_detail(query: str = "", vin: str = "", bid_id: int = 0) -> dict:
    """Full detail on a specific vehicle on the EW dashboard: its ORIGINAL MSRP and factory options (from
    iPacket), Carfax and AutoCheck damage findings, vAuto MMR / book values, AccuTrade guaranteed & retail,
    the AI buy price, miles, colors and status. USE when a caller asks about a specific car's MSRP, options,
    Carfax, AutoCheck, damage, or 'tell me about that vehicle'. query/vin/bid_id: a VIN, year/make/model, or
    bid id. (Reads stored data only — does not pull a new iPacket sticker.)"""
    bid = await _a.to_thread(_resolve_bid, query, vin, bid_id)
    if not bid:
        return {"matches": 0, "_must_say": f"No vehicle on the dashboard matches '{query or vin}'."}
    import ew_mcp  # lazy: ew_mcp imports this module at load, so import get_bid at call time
    d = await ew_mcp.get_bid(bid)
    if not isinstance(d, dict) or d.get("error"):
        return d if isinstance(d, dict) else {"error": "lookup failed"}
    # Pull the BEST iPacket row directly (get_bid's join can pick a null-MSRP row when a bid has several).
    ip = await _a.to_thread(_rows,
        """select total_msrp, base_price, exterior_color, interior_color, raw_json, not_available
             from ipacket_lookups where bid_id=%s
            order by (total_msrp is not null) desc, looked_up_at desc nulls last, id desc limit 1""", (bid,))
    ipr = ip[0] if ip else {}
    options = None
    raw = ipr.get("raw_json") or d.get("ipacket_raw_json")
    if raw:
        try:
            rj = raw if isinstance(raw, dict) else _json.loads(raw)
            options = rj.get("options")
            if isinstance(options, list) and len(options) > 40:
                options = options[:40]
        except Exception:
            pass
    if ipr:
        d["ipacket_msrp"] = ipr.get("total_msrp") if ipr.get("total_msrp") is not None else d.get("ipacket_msrp")
        d["ipacket_base_price"] = ipr.get("base_price") if ipr.get("base_price") is not None else d.get("ipacket_base_price")
        d["ipacket_exterior_color"] = ipr.get("exterior_color") or d.get("ipacket_exterior_color")
        d["ipacket_interior_color"] = ipr.get("interior_color") or d.get("ipacket_interior_color")
        if ipr.get("total_msrp") is not None:
            d["ipacket_unavailable"] = False
    out = {
        "bid_id": d.get("id"), "vin": d.get("vin"),
        "vehicle": " ".join(str(d.get(k)) for k in ("year", "make", "model", "trim") if d.get(k)),
        "status": d.get("status"), "miles": d.get("mileage"),
        "ai_buy_price": _num(d.get("ai_price")), "asking_price": _num(d.get("asking_price")),
        "msrp": _num(d.get("ipacket_msrp")), "ipacket_base_price": _num(d.get("ipacket_base_price")),
        "ipacket_options": options,
        "exterior_color": d.get("ipacket_exterior_color") or d.get("color"),
        "interior_color": d.get("ipacket_interior_color"),
        "carfax_damage": bool(d.get("carfax_damage")), "autocheck_damage": bool(d.get("autocheck_damage")),
        "damage_note": d.get("damage_signal"), "carfax_url": d.get("carfax_share_url"),
        "miles_carfax": d.get("miles_carfax"),
        "vauto_mmr": _num(d.get("vauto_mmr")), "black_book": _num(d.get("black_book")), "kbb": _num(d.get("kbb")),
        "accutrade_guaranteed": _num(d.get("guaranteed_offer")), "accutrade_retail": _num(d.get("accutrade_retail")),
        "accutrade_trade_in": _num(d.get("trade_in")),
        "photos": d.get("photo_count") or d.get("photos"),
    }
    if d.get("ipacket_unavailable"):
        out["msrp"] = None
        out["ipacket_note"] = "iPacket sticker not available for this car"
    if out["msrp"] is None and out.get("accutrade_retail"):
        out["_must_say"] = ("No factory MSRP on file for this car (iPacket sticker not captured). "
                            "Do NOT call the AccuTrade retail value the MSRP — they are different.")
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


async def list_dashboard(status: str = "", limit: int = 25) -> dict:
    """List vehicles on the EW dashboard, most recent first, optionally filtered by status (new, reviewing,
    bid_sent, bought, passed, etc.). USE for 'what's on the dashboard', 'show me the vehicles', 'what's in
    reviewing'. Returns each car's bid id, vehicle, status, and AI buy price."""
    try:
        lim = max(1, min(int(limit or 25), 100))
    except Exception:
        lim = 25
    st = (status or "").strip().lower()
    if st:
        rows = await _a.to_thread(_rows,
            """select id, year, make, model, trim, status, ai_price from bids
               where status ilike %s order by created_at desc limit %s""", (f"%{st}%", lim))
    else:
        rows = await _a.to_thread(_rows,
            """select id, year, make, model, trim, status, ai_price from bids
               order by created_at desc limit %s""", (lim,))
    cars = [{"bid_id": r["id"],
             "vehicle": " ".join(str(r[k]) for k in ("year", "make", "model", "trim") if r.get(k)),
             "status": r["status"], "ai_buy_price": _num(r["ai_price"])} for r in rows]
    return {"count": len(cars), "status_filter": st or "all", "vehicles": cars}
