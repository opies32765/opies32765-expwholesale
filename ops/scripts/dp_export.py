import sqlite3, json, subprocess
try:
    con = sqlite3.connect("file:/opt/livesaleslog/crm.db?mode=ro&immutable=1", uri=True)
    deals_total = con.execute("select count(*) from deals").fetchone()[0]
    con.close()
except Exception:
    deals_total = None

def psql(q):
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-p", "5433", "-d", "expwholesale", "-tAc", q],
                       capture_output=True, text=True, timeout=25)
    return r.stdout.strip()

# Ticker feed: most-recent qualifying bids, DEDUPED by VIN (one card per VIN,
# keeping the latest bid for that VIN), then ordered bought-last + newest-first.
cars = psql("""
select coalesce(json_agg(t), '[]') from (
  select bid_id, vin, year, make, model, trim, mileage, status, created_at
  from (
    select distinct on (coalesce(nullif(vin, ''), id::text))
           id as bid_id, vin, year, make, model, trim, mileage, status, created_at
    from (
      select id, vin, year, make, model, trim, mileage, status, created_at
      from bids
      where year is not null and make is not null and model is not null
        and status in ('reviewing', 'new', 'bid_sent', 'curating', 'bought')
      order by created_at desc
      limit 200
    ) recent
    order by coalesce(nullif(vin, ''), id::text), created_at desc, id desc
  ) d
  order by (status = 'bought') asc, created_at desc
  limit 28
) t
""")
print(json.dumps({"deals_total": deals_total, "deals_since": 2019, "live_cars": cars}))
