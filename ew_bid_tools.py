"""EW bid-count-by-period voice tool for Anna. dashboard_stats only knows today + all-time; this counts
bids created in any period (yesterday, this/last week, this/last month, ytd, all) so 'how many did we
bid yesterday' works. Loaded into ew_mcp namespace for the /api/ew-voice/tool dispatcher."""
import os as _os, psycopg2, psycopg2.extras, asyncio as _a

_ALIAS = {
    "today": "today", "yesterday": "yesterday",
    "this_week": "this_week", "this week": "this_week", "last_7_days": "this_week", "week": "this_week",
    "last_week": "last_week", "last week": "last_week",
    "this_month": "month", "this month": "month", "mtd": "month", "month to date": "month", "month": "month",
    "last_month": "last_month", "last month": "last_month", "previous month": "last_month",
    "ytd": "year", "year to date": "year", "this_year": "year", "this year": "year", "year": "year",
    "all": "all", "all time": "all", "alltime": "all", "total": "all", "lifetime": "all",
}


def _dburl():
    u = _os.environ.get("DATABASE_URL")
    if not u:
        raise RuntimeError("DATABASE_URL not set")
    return u


def _count(p):
    sql = """
      with b as (select status, (created_at at time zone 'America/New_York')::date d from bids),
           td as (select (now() at time zone 'America/New_York')::date t)
      select status, count(*) n from b, td
      where (%(p)s='all')
         or (%(p)s='today'      and b.d = td.t)
         or (%(p)s='yesterday'  and b.d = td.t - 1)
         or (%(p)s='this_week'  and b.d >= date_trunc('week', td.t)::date)
         or (%(p)s='last_week'  and b.d >= date_trunc('week', td.t - 7)::date and b.d < date_trunc('week', td.t)::date)
         or (%(p)s='month'      and date_trunc('month', b.d) = date_trunc('month', td.t))
         or (%(p)s='last_month' and date_trunc('month', b.d) = date_trunc('month', td.t - interval '1 month'))
         or (%(p)s='year'       and date_trunc('year',  b.d) = date_trunc('year',  td.t))
      group by status
    """
    with psycopg2.connect(_dburl()) as c:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"p": p})
            return [dict(r) for r in cur.fetchall()]


async def bids_count(period: str = "today") -> dict:
    """How many bids were placed/created on the EW dashboard in a period, with a breakdown by status.
    USE for 'how many cars did we bid yesterday', 'how many bids this week/last month', bid volume for a
    period. period: today, yesterday, this week, last week, this month, last month, ytd, or all."""
    raw = (period or "today").strip().lower()
    p = _ALIAS.get(raw, raw)
    if p not in ("today", "yesterday", "this_week", "last_week", "month", "last_month", "year", "all"):
        p = "today"
    rows = await _a.to_thread(_count, p)
    by_status = {r["status"]: int(r["n"]) for r in rows}
    label = {"today": "today", "yesterday": "yesterday", "this_week": "this week", "last_week": "last week",
             "month": "this month", "last_month": "last month", "year": "this year", "all": "all time"}[p]
    return {"period": label, "total_bids": sum(by_status.values()), "by_status": by_status}
