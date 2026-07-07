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
    stage_map = {r["stage"]: r["n"] for r in by_stage}
    sublet = await _a.to_thread(_rows, "select count(*) n from recon_units where out_for_recon_at is not null and out_for_recon_to is not null")
    return {"by_stage": stage_map,
            "in_recon": stage_map.get("Recon", 0),           # cars in the Recon stage (what "in/out for recon" usually means)
            "sublet_to_outside_shop": sublet[0]["n"] if sublet else 0,  # subset shipped to an external recon vendor
            "total_active": sum(r["n"] for r in by_stage)}


async def recon_out_for_recon() -> dict:
    """EW Recon: list the vehicles currently in RECON (the Recon stage of the pipeline) with how many days
    they've been in recon; if a car has also been sublet out to an outside shop, that vendor is noted. USE
    for 'what's in recon', 'which cars are out for recon', 'cars at the recon center', 'how many in recon'."""
    rows = await _a.to_thread(_rows, """
        select u.stock_no, u.year, u.make, u.model, u.entered_recon_at, u.acquired_at,
               u.out_for_recon_at, u.out_for_recon_to
        from recon_units u join recon_step_defs d on d.id=u.current_step_id
        where d.code='recon' order by coalesce(u.entered_recon_at, u.acquired_at) asc limit 200
    """)
    cars = []
    for r in rows:
        base = r.get("entered_recon_at") or r.get("acquired_at")
        days = None
        try:
            days = (_dt.datetime.now(base.tzinfo) - base).days if base else None
        except Exception:
            pass
        car = {"stock_no": r["stock_no"], "vehicle": f'{r["year"]} {r["make"]} {r["model"]}', "days_in_recon": days}
        if r.get("out_for_recon_at"):
            car["sublet_to"] = r.get("out_for_recon_to")
        cars.append(car)
    return {"in_recon_count": len(cars), "cars": cars}


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


async def recon_move(query: str = "", to_stage: str = "", stock_no: str = "", confirm: bool = False) -> dict:
    """EW Recon WRITE: MOVE a vehicle to a different recon/transport stage. TWO STEPS — the FIRST call
    (confirm not set) does NOT move anything; it returns a confirmation prompt you MUST read to the caller
    and get an explicit yes. Only after they confirm, call again with confirm=true to actually move it.
    NEVER guess the target stage — if the caller did not clearly name a destination stage, ask them first
    instead of calling this. to_stage: e.g. 'recon', 'ready for pickup', 'picked up', 'arrived at dealer',
    'arrived home', 'in transit'. query/stock_no: stock#, VIN, or vehicle."""
    key = (stock_no or query or "").strip()
    if not key or not (to_stage or "").strip():
        return {"error": "need both a vehicle and a target stage",
                "_must_say": "Ask the caller which car and which stage — do not move anything until both are clear."}
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
                "_must_say": "That's not a valid stage — ask the caller to name one (recon, ready for pickup, picked up, arrived at dealer, arrived home, in transit)."}
    if target["code"] in _STAGING:
        return {"_must_say": f"Moving to a staging step ({target['name']}) needs the board wizard; can't do that by voice."}
    if target["id"] == u.get("current_step_id"):
        return {"ok": True, "no_change": True, "stock_no": u["stock_no"],
                "_must_say": f"{u['stock_no']} is already in {target['name']}; nothing to move."}
    veh = f'{u["year"]} {u["make"]} {u["model"]}'
    if not confirm:
        return {"needs_confirmation": True, "stock_no": u["stock_no"], "vehicle": veh,
                "from_stage": u.get("stage"), "to_stage": target["name"],
                "_must_say": (f"Ask the caller to confirm, and wait for a yes: move the {veh}, stock "
                              f"{u['stock_no']}, from {u.get('stage')} to {target['name']}?"),
                "_hint_not_spoken": "After the caller says yes, call recon_move again with confirm=true."}
    await _a.to_thread(_do_move, u["id"], target["id"], u.get("current_step_id"))
    return {"ok": True, "moved": True, "stock_no": u["stock_no"], "vehicle": veh,
            "from_stage": u.get("stage"), "to_stage": target["name"]}


_PERIOD_ALIAS = {
    "this_month": "month", "mtd": "month", "month to date": "month", "month_to_date": "month",
    "this month": "month", "current month": "month",
    "last_month": "last_month", "last month": "last_month", "previous month": "last_month", "prior month": "last_month",
    "ytd": "year", "year to date": "year", "this_year": "year", "this year": "year",
    "last_year": "last_year", "last year": "last_year", "previous year": "last_year",
    "this_week": "week", "last_7_days": "week", "last 7 days": "week", "this week": "week",
    "all_time": "all", "alltime": "all", "all time": "all", "lifetime": "all", "total": "all",
}


async def recon_spend(period: str = "month") -> dict:
    """EW Recon: total recon and transport SPEND for a period, with average per vehicle. period: this
    month, last month, this year, last year, this week, or all. Use for 'how much did we spend on recon
    last month', 'recon spend', 'transport spend'."""
    raw = (period or "month").strip().lower()
    p = _PERIOD_ALIAS.get(raw, raw)
    if p not in ("month", "last_month", "year", "last_year", "week", "all"):
        p = "month"
    rows = await _a.to_thread(_rows, """
        with base as (select coalesce(acquired_at, created_at) t, coalesce(lsl_recon_cost,0) rc,
                             coalesce(lsl_transport_cost,0) tc from recon_units)
        select coalesce(sum(rc),0) recon, coalesce(sum(tc),0) transport, count(*) n from base
        where (%(p)s='all')
           or (%(p)s='year'       and date_trunc('year',  t) = date_trunc('year',  now()))
           or (%(p)s='last_year'  and date_trunc('year',  t) = date_trunc('year',  now() - interval '1 year'))
           or (%(p)s='month'      and date_trunc('month', t) = date_trunc('month', now()))
           or (%(p)s='last_month' and date_trunc('month', t) = date_trunc('month', now() - interval '1 month'))
           or (%(p)s='week'       and t >= now() - interval '7 days')
    """, {"p": p})
    r = rows[0] if rows else {"recon": 0, "transport": 0, "n": 0}
    n = r["n"] or 1
    label = {"month": "this month", "last_month": "last month", "year": "this year",
             "last_year": "last year", "week": "this week", "all": "all time"}[p]
    return {"period": label, "recon_spend": _money(r["recon"]), "transport_spend": _money(r["transport"]),
            "vehicles": r["n"], "avg_recon_per_vehicle": _money(float(r["recon"]) / n),
            "avg_transport_per_vehicle": _money(float(r["transport"]) / n)}
