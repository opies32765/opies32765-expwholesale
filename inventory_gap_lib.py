"""inventory_gap_lib.py — shared logic for nightly Telegram scan and live web page.

Source of truth for the portal-dealer inventory-gap analysis. Both
inventory_gap_scan.py (cron) and the /inventory-gaps Flask route import
from here so behavior stays identical.
"""
from collections import defaultdict


def year_bucket(yr):
    if yr is None:
        return "older"
    try:
        yr = int(yr)
    except (TypeError, ValueError):
        return "older"
    if yr >= 2021:
        return str(yr)
    return "older"


def fetch_portal_dealers(cur):
    cur.execute(
        "SELECT id, name FROM dealers "
        "WHERE portal_slug IS NOT NULL AND active = TRUE "
        "ORDER BY name"
    )
    # Normalize to list of (id, name) tuples regardless of cursor factory.
    rows = cur.fetchall()
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append((r["id"], r["name"]))
        else:
            out.append((r[0], r[1]))
    return out


def fetch_current_inventory(cur, dealer_ids):
    """dict[dealer_id] -> dict[(yb, make, model)] = current active count."""
    cur.execute(
        """
        SELECT dealer_id, year, make, model
          FROM dealer_inventory
         WHERE status = 'active'
           AND dealer_id = ANY(%s)
           AND make IS NOT NULL
           AND model IS NOT NULL
        """,
        (dealer_ids,),
    )
    out = defaultdict(lambda: defaultdict(int))
    for row in cur.fetchall():
        if isinstance(row, dict):
            d_id, yr, mk, md = row["dealer_id"], row["year"], row["make"], row["model"]
        else:
            d_id, yr, mk, md = row
        key = (year_bucket(yr), (mk or "").strip().upper(), (md or "").strip().upper())
        if not key[1] or not key[2]:
            continue
        out[d_id][key] += 1
    return out


def fetch_baseline(cur, dealer_ids):
    """90-day sales-velocity baseline: distinct sold VINs per (dealer, yb, make, model)."""
    cur.execute(
        """
        SELECT di.dealer_id,
               di.year,
               di.make,
               di.model,
               COUNT(DISTINCT di.vin) AS sold_count
          FROM dealer_inventory di
         WHERE di.dealer_id = ANY(%s)
           AND di.make IS NOT NULL
           AND di.model IS NOT NULL
           AND di.vin IS NOT NULL
           AND di.sold_at IS NOT NULL
           AND di.sold_at >= NOW() - INTERVAL '90 days'
         GROUP BY di.dealer_id, di.year, di.make, di.model
        """,
        (dealer_ids,),
    )
    out = defaultdict(lambda: defaultdict(int))
    for row in cur.fetchall():
        if isinstance(row, dict):
            d_id, yr, mk, md, sold_count = (
                row["dealer_id"], row["year"], row["make"], row["model"], row["sold_count"],
            )
        else:
            d_id, yr, mk, md, sold_count = row
        key = (year_bucket(yr), (mk or "").strip().upper(), (md or "").strip().upper())
        if not key[1] or not key[2]:
            continue
        out[d_id][key] += int(sold_count or 0)
    return out


def format_ymm(key):
    yb, mk, md = key
    return f"{yb} {mk} {md}".strip()


def analyze_dealer(current, baseline):
    """Return (holes, surpluses) — each a list of (key, baseline_sold, current_count).

    HOLE:    baseline_sold >= 3 AND current <= 1
    SURPLUS: current >= 4 AND (baseline_sold == 0 OR current >= baseline_sold * 2)
    """
    all_keys = set(current.keys()) | set(baseline.keys())
    holes, surplus = [], []
    for k in all_keys:
        cur_n = current.get(k, 0)
        base = baseline.get(k, 0)
        if base >= 3 and cur_n <= 1:
            holes.append((k, base, cur_n))
        if cur_n >= 4 and (base == 0 or cur_n >= base * 2):
            surplus.append((k, base, cur_n))
    holes.sort(key=lambda x: -x[1])
    surplus.sort(key=lambda x: -(x[2] - x[1]))
    return holes[:5], surplus[:5]
