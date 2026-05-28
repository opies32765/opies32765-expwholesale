"""inventory_gap_lib.py — shared logic for nightly Telegram scan and live web page.

Source of truth for the portal-dealer inventory-gap analysis. Both
inventory_gap_scan.py (cron) and the /inventory-gaps Flask route import
from here so behavior stays identical.

Granular per-config breakdowns (INV_GAPS_GRANULAR_2026_05_27): under each
(year, make, model) hole or surplus we surface the dominant
(trim, color_bucket, mile_bucket, price_bucket) sub-configurations.

V2 fixes (INV_GAPS_GRANULAR_V2_2026_05_27):
- trim_norm strips body_style cruft (`4d Sport Utility`, `2d Coupe`, etc.)
  scrapers conflate into the trim column. Result is the real trim, or empty.
- color_bucket switched to word-level matching with i18n (Nero=BLACK,
  Bianco=WHITE, Grigio=GRAY, Rosso=RED, Argento=SILVER, etc.) so exotic
  manufacturer color names stop falling into OTHER.
- price_bucket extended above $150k for exotics ($150-250 / $250-400 /
  $400-600 / $600k+) — a $589k Purosangue is no longer "$150k+".
- mile_bucket adds <5k for delivery-mile exotics.
"""
import re
from collections import defaultdict, Counter


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


# Body-style fragments that scrapers leak into dealer_inventory.trim.
# Example contaminated values seen: "4d Sport Utility", "GT 4d Sport Utility",
# "GT3 2d Coupe", "AMG® G 65 4d Sport Utility". Strip the `\d+d <BodyStyle>`
# tail; keep whatever real trim text precedes it.
_BODYSTYLE_RE = re.compile(
    r"\s*\d+\s*[Dd]\s+("
    r"COUPE|CONVERTIBLE|SEDAN|SPORT UTILITY|HATCHBACK|WAGON|PICKUP|VAN|"
    r"MINIVAN|CABRIOLET|ROADSTER|CROSSOVER"
    r")\b.*$",
    re.IGNORECASE,
)


def trim_norm(t):
    if not t:
        return ""
    s = str(t).strip()
    s = _BODYSTYLE_RE.sub("", s).strip()
    s = s.upper()
    return s[:24] if s else ""


# Word-level color rules. Each entry is (canonical_bucket, set_of_words).
# Iteration order matters — first hit wins. So BLACK is checked before WHITE
# so "Crystal Black Pearl" → BLACK (the BLACK word is present) before WHITE
# would have grabbed it via "PEARL".
_COLOR_RULES = [
    ("BLACK",  {"BLACK", "OBSIDIAN", "MIDNIGHT", "ONYX", "JET", "EBONY",
                "NERO", "NOIR", "SCHWARZ", "BELUGA", "BASALT", "NOCTIS"}),
    ("WHITE",  {"WHITE", "IVORY", "SNOW", "ALABASTER",
                "BIANCO", "BLANC", "WEISS", "GLACIER", "ARCTIC", "POLAR",
                "MOONLIGHT", "DIAMOND", "MYTHOS"}),
    ("GRAY",   {"GRAY", "GREY", "GUNMETAL", "GRAPHITE", "NARDO", "CHARCOAL",
                "ANTHRACITE", "GRIGIO", "GRIS", "GRAU", "AGATE", "CHALK",
                "BROOKLYN", "ICE", "STORM", "TEMPEST"}),
    ("SILVER", {"SILVER", "PLATINUM", "CHROME", "ALUMIN",
                "ARGENTO", "ARGENT", "IRIDIUM", "DOLOMITE"}),
    ("BLUE",   {"BLUE", "NAVY", "COBALT", "AZURE", "SAPPHIRE", "TEAL",
                "BLU", "BLEU", "BLAU", "GENTIAN", "SHARK", "ELEOS"}),
    ("RED",    {"RED", "CRIMSON", "GUARDS", "BURGUNDY", "MAROON", "WINE",
                "GARNET", "CARMINE", "ROSSO", "ROUGE", "ROT", "CORSA",
                "RUBINO"}),
    ("GREEN",  {"GREEN", "EMERALD", "FOREST", "OLIVE", "JADE",
                "VERDE", "VERT", "GRUN", "GRÜN"}),
    ("BROWN",  {"BROWN", "TAN", "BEIGE", "CHOCOLATE", "COFFEE", "KHAKI",
                "BRONZE", "MOCHA", "COGNAC", "TRUFFLE", "MARRON", "BRUNO"}),
    ("YELLOW", {"YELLOW", "GOLD", "CHAMPAGNE", "GIALLO", "JAUNE", "GELB",
                "MODENA"}),
    ("ORANGE", {"ORANGE", "COPPER", "RUST", "ARANCIO"}),
    ("PURPLE", {"PURPLE", "VIOLET", "PLUM", "MAGENTA", "VIOLA"}),
]


def color_bucket(c):
    if not c:
        return "UNKNOWN"
    raw = str(c).strip().upper()
    if not raw:
        return "UNKNOWN"
    # Split on whitespace AND common punctuation; strip ® and similar.
    tokens = set(re.split(r"[\s\-/®©™,.()\[\]]+", raw))
    tokens.discard("")
    if not tokens:
        return "UNKNOWN"
    for canon, needles in _COLOR_RULES:
        if tokens & needles:
            return canon
    return "OTHER"


def mile_bucket(m):
    if m is None:
        return "? mi"
    try:
        m = int(m)
    except (TypeError, ValueError):
        return "? mi"
    if m < 5_000:    return "0-5k mi"
    if m < 20_000:   return "5-20k mi"
    if m < 40_000:   return "20-40k mi"
    if m < 60_000:   return "40-60k mi"
    if m < 80_000:   return "60-80k mi"
    if m < 100_000:  return "80-100k mi"
    return "100k+ mi"


def price_bucket(p):
    if p is None:
        return "$?"
    try:
        p = int(p)
    except (TypeError, ValueError):
        return "$?"
    if p < 25_000:    return "<$25k"
    if p < 50_000:    return "$25-50k"
    if p < 75_000:    return "$50-75k"
    if p < 100_000:   return "$75-100k"
    if p < 150_000:   return "$100-150k"
    if p < 250_000:   return "$150-250k"
    if p < 400_000:   return "$250-400k"
    if p < 600_000:   return "$400-600k"
    return "$600k+"


def fetch_portal_dealers(cur):
    cur.execute(
        "SELECT id, name FROM dealers "
        "WHERE portal_slug IS NOT NULL AND active = TRUE "
        "ORDER BY name"
    )
    rows = cur.fetchall()
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append((r["id"], r["name"]))
        else:
            out.append((r[0], r[1]))
    return out


def _config_tuple(trim, color, miles, price):
    # Operator scope (2026-05-27 trim_color_only): show only trim + color in
    # the granular sub-table. miles/price still computed for any caller that
    # wants them (and to keep buckets/normalizers exercised + tested), but
    # not part of the aggregation key — that prevents the same (trim, color)
    # from splitting across multiple rows just because mileage or price
    # buckets differ.
    return (trim_norm(trim), color_bucket(color))


def _empty_bucket():
    return {"count": 0, "configs": Counter()}


def fetch_current_inventory(cur, dealer_ids):
    """dict[dealer_id] -> dict[(yb, make, model)] = {'count': N, 'configs': Counter}"""
    cur.execute(
        """
        SELECT dealer_id, year, make, model, trim, ext_color, mileage, price
          FROM dealer_inventory
         WHERE status = 'active'
           AND dealer_id = ANY(%s)
           AND make IS NOT NULL
           AND model IS NOT NULL
        """,
        (dealer_ids,),
    )
    out = defaultdict(lambda: defaultdict(_empty_bucket))
    for row in cur.fetchall():
        if isinstance(row, dict):
            d_id = row["dealer_id"]; yr = row["year"]; mk = row["make"]; md = row["model"]
            tr = row["trim"]; cl = row["ext_color"]; mi = row["mileage"]; pr = row["price"]
        else:
            d_id, yr, mk, md, tr, cl, mi, pr = row
        key = (year_bucket(yr), (mk or "").strip().upper(), (md or "").strip().upper())
        if not key[1] or not key[2]:
            continue
        bucket = out[d_id][key]
        bucket["count"] += 1
        bucket["configs"][_config_tuple(tr, cl, mi, pr)] += 1
    return out


def fetch_baseline(cur, dealer_ids):
    """90-day sales-velocity baseline. Dedups by VIN to mirror prior
    COUNT(DISTINCT vin) semantics — same VIN relisted twice = 1 sale."""
    cur.execute(
        """
        SELECT dealer_id, year, make, model, trim, ext_color, mileage, price, vin
          FROM dealer_inventory
         WHERE dealer_id = ANY(%s)
           AND make IS NOT NULL
           AND model IS NOT NULL
           AND vin IS NOT NULL AND vin <> ''
           AND sold_at IS NOT NULL
           AND sold_at >= NOW() - INTERVAL '90 days'
        """,
        (dealer_ids,),
    )
    seen = set()
    out = defaultdict(lambda: defaultdict(_empty_bucket))
    for row in cur.fetchall():
        if isinstance(row, dict):
            d_id = row["dealer_id"]; yr = row["year"]; mk = row["make"]; md = row["model"]
            tr = row["trim"]; cl = row["ext_color"]; mi = row["mileage"]; pr = row["price"]; vn = row["vin"]
        else:
            d_id, yr, mk, md, tr, cl, mi, pr, vn = row
        key = (year_bucket(yr), (mk or "").strip().upper(), (md or "").strip().upper())
        if not key[1] or not key[2]:
            continue
        dedup = (d_id, key, vn)
        if dedup in seen:
            continue
        seen.add(dedup)
        bucket = out[d_id][key]
        bucket["count"] += 1
        bucket["configs"][_config_tuple(tr, cl, mi, pr)] += 1
    return out


def format_ymm(key):
    yb, mk, md = key
    return f"{yb} {mk} {md}".strip()


def format_config(cfg):
    """Pretty-print a (trim, color) tuple. Empty trim is omitted so result
    is either 1 field (color only) or 2 fields (trim · color)."""
    tr, cl = cfg
    parts = []
    if tr:
        parts.append(tr)
    parts.append(cl)
    return " · ".join(parts)


def _top_configs(counter, n=3):
    return counter.most_common(n)


def analyze_dealer(current, baseline):
    """Return (holes, surpluses) — each a list of:
        (key, base_sold, current_count, sold_configs_top3, current_configs_top3)

    sold_configs_top3 / current_configs_top3 are [(config_tuple, count), ...].

    HOLE:    baseline_sold >= 3 AND current <= 1
    SURPLUS: current >= 4 AND (baseline_sold == 0 OR current >= baseline_sold * 2)
    """
    all_keys = set(current.keys()) | set(baseline.keys())
    holes, surplus = [], []
    for k in all_keys:
        cur_bucket = current.get(k, _empty_bucket())
        base_bucket = baseline.get(k, _empty_bucket())
        cur_n = cur_bucket["count"]
        base = base_bucket["count"]
        sold_cfgs = _top_configs(base_bucket["configs"], 3)
        cur_cfgs = _top_configs(cur_bucket["configs"], 3)
        if base >= 3 and cur_n <= 1:
            holes.append((k, base, cur_n, sold_cfgs, cur_cfgs))
        if cur_n >= 4 and (base == 0 or cur_n >= base * 2):
            surplus.append((k, base, cur_n, sold_cfgs, cur_cfgs))
    holes.sort(key=lambda x: -x[1])
    surplus.sort(key=lambda x: -(x[2] - x[1]))
    return holes[:5], surplus[:5]
