"""EW Recon (ewrecon) voice tools for Anna — READ + WRITE access to the recon/transport pipeline.
Recon & transport $ auto-sync from each LSL deal into recon_units (lsl_recon_cost / lsl_transport_cost).
Loaded into ew_mcp namespace so the /api/ew-voice/tool dispatcher can call them by name."""
import os as _os, psycopg2, psycopg2.extras, asyncio as _a, datetime as _dt


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


def _exec(sql, args=()):
    with psycopg2.connect(_dburl()) as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            n = cur.rowcount
            row = cur.fetchone() if cur.description else None
        c.commit()
    return n, row


def _money(v):
    try:
        return int(round(float(v)))
    except Exception:
        return 0


def _d(v):
    return v.isoformat() if isinstance(v, (_dt.date, _dt.datetime)) else v


def _find(key):
    """Resolve a stock#/VIN/YMM to recon units (most recent first)."""
    like = f"%{key}%"
    return _rows("""
        select u.id, u.stock_no, u.vin, u.year, u.make, u.model, u.trim,
               d.name as stage, d.code as stage_code, u.status, u.sub_status,
               coalesce(u.lsl_recon_cost,0) as recon_cost,
               coalesce(u.lsl_transport_cost,0) as transport_cost,
               coalesce(u.recon_estimate_total,0) as recon_estimate,
               coalesce(u.recon_actual_total,0) as recon_actual,
               u.out_for_recon_at, u.out_for_recon_to,
               u.est_pickup_date, u.est_delivery_date, u.transport_company,
               u.bought_from, u.sold_to, u.entered_recon_at, u.acquired_at, u.frontline_ready_at
        from recon_units u left join recon_step_defs d on d.id=u.current_step_id
        where upper(u.stock_no)=upper(%s) or upper(u.vin)=upper(%s)
           or upper(u.stock_no) like upper(%s) or upper(u.vin) like upper(%s)
           or upper(u.year||' '||u.make||' '||u.model||' '||coalesce(u.trim,'')) like upper(%s)
        order by u.id desc limit 8
    """, (key, key, like, like, like))


def _fmt(r):
    out = {
        "stock_no": r["stock_no"], "vin": r["vin"],
        "vehicle": f'{r["year"]} {r["make"]} {r["model"]}' + (f' {r["trim"]}' if r.get("trim") else ""),
        "recon_stage": r["stage"], "status": r["status"],
        "recon_cost": _money(r["recon_cost"]), "transport_cost": _money(r["transport_cost"]),
        "recon_estimate": _money(r["recon_estimate"]), "recon_actual": _money(r.get("recon_actual")),
    }
    base = r.get("entered_recon_at") or r.get("acquired_at")
    if base and not r.get("frontline_ready_at"):
        try:
            out["days_in_recon"] = (_dt.datetime.now(base.tzinfo) - base).days
        except Exception:
            pass
    if r.get("out_for_recon_at"):
        out["out_for_recon"] = True
        out["out_for_recon_to"] = r.get("out_for_recon_to")
        try:
            days = (_dt.datetime.now(r["out_for_recon_at"].tzinfo) - r["out_for_recon_at"]).days
            out["days_out_for_recon"] = days
        except Exception:
            pass
    if r.get("est_delivery_date"):
        out["est_delivery_date"] = _d(r["est_delivery_date"])
    if r.get("transport_company"):
        out["carrier"] = r["transport_company"]
    return out


async def recon_status(query: str = "", stock_no: str = "", vin: str = "") -> dict:
    """EW Recon: recon/reconditioning + transport status and CHARGES for a specific vehicle. USE when a
    caller asks whether a car has recon, what the recon or transport charge/estimate is, what recon STAGE
    or step a car is in, whether it's out for recon (and how long), or its transport ETA. Recon & transport
    dollars auto-sync from the LSL deal. query/stock_no/vin: a stock number, VIN, or year/make/model.
    Returns each matching car's stage, status, recon_cost, transport_cost, out-for-recon status and ETA."""
    key = (stock_no or vin or query or "").strip()
    if not key:
        return {"error": "provide a stock number, VIN, or vehicle description"}
    rows = await _a.to_thread(_find, key)
    if not rows:
        return {"matches": 0, "_must_say": f"No vehicle in EW Recon matches '{key}'. Say so; do not guess."}
    cars = [_fmt(r) for r in rows]
    if len(rows) == 1:  # attach recent notes for the single-car case
        notes = await _a.to_thread(_rows,
            "select body, category, author, created_at from recon_notes where unit_id=%s order by id desc limit 5",
            (rows[0]["id"],))
        if notes:
            cars[0]["notes"] = [{"note": n["body"], "category": n["category"], "by": n["author"]} for n in notes]
    return {"matches": len(rows), "cars": cars}


async def recon_board() -> dict:
    """EW Recon board overview: how many vehicles are in each recon/transport stage right now, plus how
    many are out for recon and in transit. USE for 'how many cars are in recon', 'what's in transit',
    'how many are out for recon', a recon pipeline summary."""
    by_stage = await _a.to_thread(_rows, """
        select coalesce(d.name,'Unknown') as stage, count(*) n
        from recon_units u left join recon_step_defs d on d.id=u.current_step_id
        where u.exited_at is null and not coalesce(u.not_available,false)
        group by d.name, d.sort_order order by d.sort_order nulls last
    """)
    out_cnt = await _a.to_thread(_rows, "select count(*) n from recon_units where out_for_recon_at is not null and out_for_recon_to is not null")
    return {"by_stage": {r["stage"]: r["n"] for r in by_stage},
            "out_for_recon": out_cnt[0]["n"] if out_cnt else 0,
            "total_active": sum(r["n"] for r in by_stage)}


async def recon_out_for_recon() -> dict:
    """EW Recon: list the vehicles currently OUT for recon (shipped to a recon vendor), with the vendor
    and how many days they've been out. USE for 'what's out for recon', 'which cars are at the recon shop'."""
    rows = await _a.to_thread(_rows, """
        select stock_no, year, make, model, out_for_recon_to, out_for_recon_at
        from recon_units where out_for_recon_at is not null and out_for_recon_to is not null
        order by out_for_recon_at asc limit 100
    """)
    cars = []
    for r in rows:
        d = None
        try:
            d = (_dt.datetime.now(r["out_for_recon_at"].tzinfo) - r["out_for_recon_at"]).days
        except Exception:
            pass
        cars.append({"stock_no": r["stock_no"],
                     "vehicle": f'{r["year"]} {r["make"]} {r["model"]}',
                     "vendor": r["out_for_recon_to"], "days_out": d})
    return {"count": len(cars), "cars": cars}


# ─── WRITE tools ───

async def recon_add_note(query: str = "", note: str = "", stock_no: str = "", category: str = "general") -> dict:
    """EW Recon WRITE: add a note to a vehicle's recon record. query/stock_no: stock#, VIN, or vehicle.
    note: the note text to save. category: 'general' or 'recon'. Confirm the car and note back to the
    caller before saving. Returns which car it was saved to."""
    key = (stock_no or query or "").strip()
    if not key or not note.strip():
        return {"error": "need a vehicle and note text"}
    rows = await _a.to_thread(_find, key)
    if not rows:
        return {"matches": 0, "_must_say": f"No vehicle in EW Recon matches '{key}'."}
    if len(rows) > 1 and not (stock_no or "").strip():
        return {"ambiguous": [f'{r["stock_no"]} {r["year"]} {r["make"]} {r["model"]}' for r in rows[:5]],
                "_must_say": "Multiple cars match — ask the caller which stock number."}
    u = rows[0]
    cat = "recon" if str(category).lower().startswith("recon") else "general"
    await _a.to_thread(_exec,
        "insert into recon_notes (unit_id, body, category, author, created_at) values (%s,%s,%s,%s, now())",
        (u["id"], note.strip(), cat, "Anna (voice)"))
    return {"ok": True, "stock_no": u["stock_no"],
            "vehicle": f'{u["year"]} {u["make"]} {u["model"]}', "note_saved": note.strip(), "category": cat}


async def recon_ship_out(query: str = "", vendor: str = "", stock_no: str = "") -> dict:
    """EW Recon WRITE: mark a vehicle as shipped OUT for recon to a vendor (starts the out-for-recon timer).
    query/stock_no: stock#, VIN, or vehicle. vendor: the recon shop/vendor name. Confirm the car and vendor
    with the caller before doing this."""
    key = (stock_no or query or "").strip()
    if not key or not vendor.strip():
        return {"error": "need a vehicle and a vendor name"}
    rows = await _a.to_thread(_find, key)
    if not rows:
        return {"matches": 0, "_must_say": f"No vehicle in EW Recon matches '{key}'."}
    if len(rows) > 1 and not (stock_no or "").strip():
        return {"ambiguous": [f'{r["stock_no"]} {r["year"]} {r["make"]} {r["model"]}' for r in rows[:5]],
                "_must_say": "Multiple cars match — ask the caller which stock number."}
    u = rows[0]
    await _a.to_thread(_exec,
        "update recon_units set out_for_recon_at=now(), out_for_recon_to=%s, updated_at=now() where id=%s",
        (vendor.strip(), u["id"]))
    return {"ok": True, "stock_no": u["stock_no"],
            "vehicle": f'{u["year"]} {u["make"]} {u["model"]}', "shipped_out_to": vendor.strip()}


async def recon_mark_returned(query: str = "", stock_no: str = "") -> dict:
    """EW Recon WRITE: mark a vehicle RETURNED from recon (clears the out-for-recon timer/vendor).
    query/stock_no: stock#, VIN, or vehicle. Confirm the car with the caller first."""
    key = (stock_no or query or "").strip()
    if not key:
        return {"error": "need a vehicle"}
    rows = await _a.to_thread(_find, key)
    if not rows:
        return {"matches": 0, "_must_say": f"No vehicle in EW Recon matches '{key}'."}
    if len(rows) > 1 and not (stock_no or "").strip():
        return {"ambiguous": [f'{r["stock_no"]} {r["year"]} {r["make"]} {r["model"]}' for r in rows[:5]],
                "_must_say": "Multiple cars match — ask the caller which stock number."}
    u = rows[0]
    await _a.to_thread(_exec,
        "update recon_units set out_for_recon_at=null, out_for_recon_to=null, updated_at=now() where id=%s",
        (u["id"],))
    return {"ok": True, "stock_no": u["stock_no"],
            "vehicle": f'{u["year"]} {u["make"]} {u["model"]}', "marked": "returned from recon"}


# staging steps need destination-path logic (board wizard) — not safe to set blindly by voice
_STAGING = {"dealer_to_dealer", "dealer_to_home", "indiv_to_dealer", "indiv_to_home"}


def _resolve_step(to_stage):
    """Fuzzy-match a spoken stage to an active recon_step_defs row (by code or name)."""
    t = (to_stage or "").strip().lower()
    if not t:
        return None
    steps = _rows("select id, code, name from recon_step_defs where active order by sort_order")
    for s in steps:  # exact code/name
        if t == s["code"].lower() or t == (s["name"] or "").lower():
            return s
    for s in steps:  # contains
        if t in s["code"].lower() or t in (s["name"] or "").lower():
            return s
    # common phrasings
    alias = {"in transit": "in_transit_dealer", "transit": "in_transit_dealer", "arrived": "arrived_dealer",
             "at the dealer": "arrived_dealer", "home": "arrived_home", "home base": "arrived_home",
             "ready": "ready", "ready for pickup": "ready", "picked up": "picked_up", "new": "all",
             "recon": "recon", "in recon": "recon"}
    code = alias.get(t)
    if code:
        for s in steps:
            if s["code"] == code:
                return s
    return None


def _do_move(unit_id, target_id, from_id):
    with psycopg2.connect(_dburl()) as c:
        with c.cursor() as cur:
            cur.execute("update recon_step_events set exited_at=now(),"
                        " duration_sec=extract(epoch from (now()-entered_at))::bigint"
                        " where unit_id=%s and exited_at is null", (unit_id,))
            cur.execute("delete from recon_unit_active_steps where unit_id=%s", (unit_id,))
            cur.execute("update recon_units set current_step_id=%s, current_step_entered_at=now(), updated_at=now() where id=%s",
                        (target_id, unit_id))
            cur.execute("insert into recon_step_events (unit_id, step_id, entered_at, moved_by, move_reason, from_step_id)"
                        " values (%s,%s,now(),%s,'move',%s) returning id", (unit_id, target_id, "Anna (voice)", from_id))
            eid = cur.fetchone()[0]
            cur.execute("insert into recon_unit_active_steps (unit_id, step_id, event_id, entered_at)"
                        " values (%s,%s,%s,now()) on conflict (unit_id, step_id) do nothing", (unit_id, target_id, eid))
        c.commit()


async def recon_move(query: str = "", to_stage: str = "", stock_no: str = "") -> dict:
    """EW Recon WRITE: MOVE a vehicle to a different recon/transport stage (advance it through the pipeline).
    query/stock_no: stock#, VIN, or vehicle. to_stage: the target stage, e.g. 'recon', 'ready', 'ready for
    pickup', 'picked up', 'arrived at dealer', 'arrived home', 'in transit'. Confirm the car and the target
    stage with the caller before moving. Returns the from -> to stage."""
    key = (stock_no or query or "").strip()
    if not key or not to_stage.strip():
        return {"error": "need a vehicle and a target stage"}
    rows = await _a.to_thread(_find, key)
    if not rows:
        return {"matches": 0, "_must_say": f"No vehicle in EW Recon matches '{key}'."}
    if len(rows) > 1 and not (stock_no or "").strip():
        return {"ambiguous": [f'{r["stock_no"]} {r["year"]} {r["make"]} {r["model"]}' for r in rows[:5]],
                "_must_say": "Multiple cars match — ask the caller which stock number."}
    u = rows[0]
    target = await _a.to_thread(_resolve_step, to_stage)
    if not target:
        return {"error": f"unknown stage '{to_stage}'",
                "_must_say": "Ask the caller to name a valid stage (recon, ready, picked up, arrived at dealer, arrived home, in transit)."}
    if target["code"] in _STAGING:
        return {"_must_say": f"Moving to a staging step ({target['name']}) needs the board wizard; can't do that by voice."}
    if target["id"] == u.get("current_step_id"):
        return {"ok": True, "no_change": True, "stock_no": u["stock_no"], "stage": target["name"]}
    await _a.to_thread(_do_move, u["id"], target["id"], u.get("current_step_id"))
    return {"ok": True, "stock_no": u["stock_no"], "vehicle": f'{u["year"]} {u["make"]} {u["model"]}',
            "from_stage": u.get("stage"), "to_stage": target["name"]}


async def recon_spend(period: str = "month") -> dict:
    """EW Recon: total recon and transport SPEND for a period, with average per vehicle. period: week,
    month, year, or all. Use for 'how much did we spend on recon this month', 'recon spend', transport spend."""
    p = (period or "month").strip().lower()
    if p in ("this_month", "mtd", "month to date"):
        p = "month"
    elif p in ("ytd", "year to date"):
        p = "year"
    elif p in ("this_week", "last_7_days", "week"):
        p = "week"
    try:
        import recon_routes as _rr
        rep = await _a.to_thread(_rr._recon_report, p)
        return {"period": p, "report": _round_nums(rep)}
    except Exception:
        rows = await _a.to_thread(_rows, """
            select coalesce(sum(lsl_recon_cost),0) recon, coalesce(sum(lsl_transport_cost),0) transport,
                   count(*) n
            from recon_units
            where (%s='all') or (%s='year'  and date_trunc('year', coalesce(acquired_at,created_at))=date_trunc('year', now()))
               or (%s='month' and date_trunc('month',coalesce(acquired_at,created_at))=date_trunc('month',now()))
               or (%s='week'  and coalesce(acquired_at,created_at) >= now()-interval '7 days')
        """, (p, p, p, p))
        r = rows[0] if rows else {"recon": 0, "transport": 0, "n": 0}
        n = r["n"] or 1
        return {"period": p, "recon_spend": _money(r["recon"]), "transport_spend": _money(r["transport"]),
                "vehicles": r["n"], "avg_recon_per_vehicle": _money(float(r["recon"]) / n),
                "avg_transport_per_vehicle": _money(float(r["transport"]) / n)}
