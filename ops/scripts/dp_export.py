import sqlite3, json, subprocess, re

LSL = "file:/opt/livesaleslog/crm.db?mode=ro&immutable=1"

# ── vehicle-name tidy (same rules as ew_live_deals_feed.py) ──────────────────
MAKES = {'MCLAREN': 'McLaren', 'BMW': 'BMW', 'GMC': 'GMC', 'RAM': 'Ram', 'KIA': 'Kia',
         'MINI': 'Mini', 'LAND ROVER': 'Land Rover', 'ROLLS-ROYCE': 'Rolls-Royce',
         'ALFA ROMEO': 'Alfa Romeo', 'INFINITI': 'Infiniti', 'MERCEDES-BENZ': 'Mercedes-Benz'}
SUFFIX = re.compile(r'\s+(Sport Utility Vehicle|Pickup Truck|Passenger Van|Cargo Van|'
                    r'Station Wagon|Sport Utility|Minivan|Sedan|Coupe|Convertible|'
                    r'Hatchback|Wagon|Van|SUV)\s*$', re.I)
ACRON = {'Gmc': 'GMC', 'Bmw': 'BMW', 'Amg': 'AMG', 'Srt': 'SRT', 'Gti': 'GTI',
         'Sti': 'STI', 'Trd': 'TRD', 'Zr2': 'ZR2', 'Ev': 'EV'}


def cap(x):
    return MAKES.get((x or '').upper(), ' '.join(w.capitalize() for w in (x or '').split()))


def clean(v):
    v = (v or '').strip()
    for _ in range(3):
        v2 = SUFFIX.sub('', v).strip()
        if v2 == v:
            break
        v = v2
    v = re.sub(r'\s+', ' ', v).strip()
    v = re.sub(r'\s+\d{1,2}$', '', v)
    return re.sub(r"[A-Za-z0-9]+", lambda m: ACRON.get(m.group(0), m.group(0)), v)


US = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
         "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())

deals_total = dealers_served = states_covered = None
sold_events = []
try:
    con = sqlite3.connect(LSL, uri=True)
    deals_total = con.execute("select count(*) from deals").fetchone()[0]
    # "Dealer partners" = counterparties on file in LSL's supplier book.
    dealers_served = con.execute("select count(*) from suppliers").fetchone()[0]
    # "States covered" = US states those counterparties sit in. Non-US codes
    # (ON, QC, BC, PR...) are excluded so the label stays true.
    st = [r[0] for r in con.execute(
        "select distinct upper(trim(state)) from suppliers "
        "where state is not null and length(trim(state))=2")]
    states_covered = len([s for s in st if s in US])

    # SOLD events for the activity toast. Every LSL deal carries both a purchase
    # and a sale price - a deal IS a completed buy-then-sell - so a deal row is
    # reported as the sale. The buy side comes from ai_accuracy below.
    for (vi, ts) in con.execute(
            "select vehicle_info, created_at from deals "
            "where vehicle_info is not null and length(vehicle_info) > 6 "
            "  and coalesce(sale_price,0) > 0 "
            "order by created_at desc limit 60"):
        v = clean(vi)
        if v:
            sold_events.append({"vehicle": v, "action": "Sold", "at": ts})
    con.close()
except Exception:
    pass


def psql(q):
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-p", "5433", "-d", "expwholesale",
                        "-tAc", q], capture_output=True, text=True, timeout=25)
    return r.stdout.strip()


# ── ticker: 7 days of BOUGHT + BIDDING, deduped by VIN (TICKER_7DAY_2026_07_29)
# BOUGHT comes from ai_accuracy (VIN-matches bids to LSL deals.purchase_cost),
# NOT bids.status='bought' - that flag is a barely-used dashboard state.
cars = psql("""
with buys as (
  select distinct on (coalesce(nullif(a.vin,''), a.bid_id::text))
         a.bid_id, a.year, a.make, a.model, b.trim, a.mileage,
         'bought'::text as status, a.actual_purchased_at as created_at
  from ai_accuracy a
  join bids b on b.id = a.bid_id
  where a.actual_purchased_at > now() - interval '7 days'
    and a.year is not null and a.make is not null and a.model is not null
  order by coalesce(nullif(a.vin,''), a.bid_id::text), a.actual_purchased_at desc
),
live as (
  select distinct on (coalesce(nullif(b.vin,''), b.id::text))
         b.id as bid_id, b.year, b.make, b.model, b.trim, b.mileage,
         b.status, b.created_at
  from bids b
  where b.created_at > now() - interval '7 days'
    and b.year is not null and b.make is not null and b.model is not null
    and b.status in ('reviewing', 'new', 'bid_sent', 'curating')
    and not exists (select 1 from buys q where q.bid_id = b.id)
  order by coalesce(nullif(b.vin,''), b.id::text), b.created_at desc
)
select coalesce(json_agg(t), '[]') from (
  select * from (select * from buys order by created_at desc limit 30) bb
  union all
  select * from (select * from live order by created_at desc limit 30) ll
  order by created_at desc
) t
""")

# ── BOUGHT events for the activity toast ────────────────────────────────────
bought_json = psql("""
select coalesce(json_agg(t), '[]') from (
  select distinct on (coalesce(nullif(a.vin,''), a.bid_id::text))
         (a.year::text || ' ' || a.make || ' ' || a.model) as vehicle,
         'Bought'::text as action,
         a.actual_purchased_at as at
  from ai_accuracy a
  where a.actual_purchased_at > now() - interval '30 days'
    and a.year is not null and a.make is not null and a.model is not null
  order by coalesce(nullif(a.vin,''), a.bid_id::text), a.actual_purchased_at desc
) t
""")
try:
    bought_events = json.loads(bought_json) if bought_json else []
except Exception:
    bought_events = []
for e in bought_events:
    e['vehicle'] = clean(' '.join(
        [p if i != 1 else cap(p) for i, p in enumerate(e['vehicle'].split(' ', 2))]))

activity = (bought_events + sold_events)
activity.sort(key=lambda e: str(e.get('at') or ''), reverse=True)
activity = activity[:60]

print(json.dumps({"deals_total": deals_total, "deals_since": 2019,
                  "dealers_served": dealers_served, "states_covered": states_covered,
                  "live_cars": cars, "activity": activity}))
