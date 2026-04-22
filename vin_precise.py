"""
vin_precise.py — Hybrid VIN decoder with trim-level precision.

Three layers:
  1. Brand-specific VDS lookup tables — deterministic for Porsche/Ferrari/
     Mercedes-AMG where VIN positions 4-8 encode trim specifically.
  2. auto.dev API (free tier: 1K VINs/mo) — paid fallback when VDS tables
     don't hit. Returns trim + style with high confidence.
  3. NHTSA vPIC — free baseline. We strip ambiguous separators
     ("Carrera (2WD), Carrera 4 (4WD)" → "Carrera (2WD)") as last resort.

Returns:
    {
        "vin":      str,
        "year":     int,
        "make":     str,
        "model":    str,
        "trim":     str,
        "style":    str | None,     # auto.dev only
        "trim_confidence": "deterministic" | "high" | "medium" | "low",
        "source":   "porsche_vds" | "ferrari_vds" | "mbz_amg_vds" |
                    "auto.dev"    | "nhtsa"
    }

Unknown premium VINs (Porsche/Ferrari/AMG that miss our tables) are logged
to the `vds_unknown` table so the tables can be extended over time.
"""

from __future__ import annotations
import os
import re
import json

try:
    import requests as _http
except ImportError:  # pragma: no cover
    _http = None


# ═══ BRAND-SPECIFIC VDS TABLES ═══════════════════════════════════════════════
# Keys are tuples of (WMI, vds_slice, body_position) so lookup is O(1).
# Seed data — compiled from:
#  - Wikibooks VIN Codes (Ferrari complete through MY2025, Mercedes-AMG ~70%)
#  - Bring a Trailer / Cars & Bids confirmed auction records
#  - Rennlist / MBWorld forum threads
#  - Internal EW / DIA inventory cross-reference
# Hit rate targets: Porsche 70-85%, Ferrari 95%+, Mercedes-AMG 70%.
# Extend via the vds_unknown log queue in the admin UI.

# ── Porsche (WP0 = 911 / Boxster / Cayman, WP1 = Cayenne, WP2 = Panamera) ──
# Keys: (wmi, positions_4_5, position_6)
PORSCHE_VDS = {
    # 911 (WP0) — 991/992 generations
    ("WP0", "AA", "2"): "Carrera (Coupe)",
    ("WP0", "AB", "2"): "Carrera S (Coupe)",
    ("WP0", "AC", "2"): "Carrera 4 (Coupe)",
    ("WP0", "AD", "2"): "Carrera 4S (Coupe)",
    ("WP0", "CA", "2"): "Turbo (Coupe)",
    ("WP0", "CB", "2"): "Turbo S (Coupe)",
    ("WP0", "AE", "2"): "GT3 RS",
    ("WP0", "AF", "2"): "GT3",
    ("WP0", "AG", "2"): "Carrera GTS (Coupe)",
    ("WP0", "AH", "2"): "Carrera 4 GTS (Coupe)",
    ("WP0", "AJ", "2"): "GT3 Touring",
    # 911 Cabriolet / Targa (position 6 = "3" or "4")
    ("WP0", "AA", "3"): "Carrera Cabriolet",
    ("WP0", "AB", "3"): "Carrera S Cabriolet",
    ("WP0", "AC", "3"): "Carrera 4 Cabriolet",
    ("WP0", "AD", "3"): "Carrera 4S Cabriolet",
    ("WP0", "CA", "3"): "Turbo Cabriolet",
    ("WP0", "CB", "3"): "Turbo S Cabriolet",
    ("WP0", "AA", "4"): "Targa 4",
    ("WP0", "AB", "4"): "Targa 4S",
    # Boxster / Cayman (WP0 positions 4-5 = BA/CA)
    ("WP0", "BA", "2"): "Cayman",
    ("WP0", "BB", "2"): "Cayman S",
    ("WP0", "BC", "2"): "Cayman GTS",
    ("WP0", "BD", "2"): "Cayman GT4",
    ("WP0", "CA", "1"): "Boxster",
    ("WP0", "CB", "1"): "Boxster S",
    ("WP0", "CC", "1"): "Boxster GTS",
    ("WP0", "CD", "1"): "Boxster Spyder",

    # Cayenne (WP1) — 9YA / 9YB / E3 generations
    ("WP1", "AA", "2"): "Cayenne (base)",
    ("WP1", "AB", "2"): "Cayenne S",
    ("WP1", "AC", "2"): "Cayenne Turbo",
    ("WP1", "AD", "2"): "Cayenne GTS",
    ("WP1", "AE", "2"): "Cayenne Coupe",
    ("WP1", "AF", "2"): "Cayenne Turbo GT",
    ("WP1", "AG", "2"): "Cayenne E-Hybrid",
    ("WP1", "AH", "2"): "Cayenne Turbo S E-Hybrid",
    ("WP1", "AJ", "2"): "Cayenne S E-Hybrid",
    ("WP1", "BA", "2"): "Macan",
    ("WP1", "BB", "2"): "Macan S",
    ("WP1", "BC", "2"): "Macan GTS",
    ("WP1", "BD", "2"): "Macan Turbo",

    # Panamera (WP0 Panamera-WMI varies; some 2020+ are WP0AF)
    ("WP0", "FA", "2"): "Panamera",
    ("WP0", "FB", "2"): "Panamera 4",
    ("WP0", "FC", "2"): "Panamera 4S",
    ("WP0", "FD", "2"): "Panamera Turbo",
    ("WP0", "FE", "2"): "Panamera Turbo S",
    ("WP0", "FF", "2"): "Panamera GTS",

    # Taycan (WP0 — new EV platform)
    ("WP0", "LA", "2"): "Taycan",
    ("WP0", "LB", "2"): "Taycan 4S",
    ("WP0", "LC", "2"): "Taycan GTS",
    ("WP0", "LD", "2"): "Taycan Turbo",
    ("WP0", "LE", "2"): "Taycan Turbo S",
}

# ── Ferrari (WMI = ZFF) — position 6-7 encodes model ──
# Source: Wikibooks Ferrari VIN Codes (complete through MY2025).
FERRARI_VDS = {
    "79": "488 GTB",            "80": "488 Spider",
    "90": "488 Pista",          "91": "488 Pista Spider",
    "89": "Portofino",          "02": "Portofino M",
    "98": "Roma",               "09": "Roma Spider",
    "95": "SF90 Stradale",      "96": "SF90 Spider",     "07": "SF90 XX Stradale",
    "83": "812 Superfast",      "97": "812 GTS",
    "03": "812 Competizione",   "04": "812 Competizione A",
    "99": "296 GTB",            "01": "296 GTS",
    "92": "F8 Tributo",         "93": "F8 Spider",
    "82": "GTC4Lusso",
    "05": "Daytona SP3",        "06": "Purosangue",
    "10": "12Cilindri",         "11": "12Cilindri Spider",
    "77": "LaFerrari",          "78": "LaFerrari Aperta",
    "73": "California T",
    "69": "458 Italia",         "70": "458 Spider",       "71": "458 Speciale",
    "72": "458 Speciale A",
    "65": "FF",                 "66": "F12berlinetta",    "67": "F12tdf",
}

# ── Mercedes-AMG — WMI W1K/WDD/W1N/4JG/55S, positions 6-7 ──
# Partial — covers most 2018+ AMG variants; extend as unknowns log.
MBZ_AMG_VDS = {
    "5B": "AMG CLA35 4Matic",   "5D": "AMG CLA45 4Matic",
    "8G": "AMG C63",            "8H": "AMG C63 S",
    "6B": "AMG E53 4Matic",     "8K": "AMG E63 S 4Matic",
    "8L": "AMG E63 S Wagon",
    "7J": "AMG S63 4Matic",     "8C": "AMG S63 E Performance",
    "7H": "AMG G63",            "5A": "AMG G63",
    "6E": "AMG GLC43 4Matic",   "8J": "AMG GLC63 4Matic",
    "6F": "AMG GLE53 4Matic",   "8M": "AMG GLE63 S 4Matic",
    "6G": "AMG GLS63 4Matic",
    "7A": "AMG GT",             "7B": "AMG GT S",
    "7C": "AMG GT C",           "7D": "AMG GT R",
    "7E": "AMG GT 4-Door Coupe 53",  "7F": "AMG GT 4-Door Coupe 63 S",
    "7G": "AMG GT Black Series",
}

MBZ_AMG_WMIS = {"W1K", "WDD", "W1N", "4JG", "55S", "WDC"}


def _porsche_trim(vin: str):
    wmi = vin[0:3]
    if wmi not in ("WP0", "WP1", "WP2"):
        return None
    return PORSCHE_VDS.get((wmi, vin[3:5], vin[5]))


def _ferrari_trim(vin: str):
    if vin[0:3] != "ZFF":
        return None
    return FERRARI_VDS.get(vin[5:7])


def _mbz_amg_trim(vin: str):
    if vin[0:3] not in MBZ_AMG_WMIS:
        return None
    return MBZ_AMG_VDS.get(vin[5:7])


BRAND_DECODERS = [
    ("porsche_vds",  _porsche_trim),
    ("ferrari_vds",  _ferrari_trim),
    ("mbz_amg_vds",  _mbz_amg_trim),
]

# VINs we'd like to learn about — WMIs where VDS should be decodable
PREMIUM_WMIS_TO_LOG = {
    "WP0", "WP1", "WP2",              # Porsche
    "ZFF",                             # Ferrari
    "ZHW",                             # Lamborghini
    "ZAM", "ZAR",                      # Maserati / Alfa
    "SCA", "SCB",                      # Bentley / Rolls
    "SBM",                             # McLaren
    "WAU",                             # Audi (RS variants worth logging)
    "WBS", "WBX",                      # BMW M (some M models)
} | MBZ_AMG_WMIS


# ═══ auto.dev API ═══════════════════════════════════════════════════════════

def _autodev_decode(vin: str, timeout: float = 6.0):
    """Call auto.dev /api/vin/{vin}. Free tier: 1000 calls/month.
    Returns dict with year/make/model/trim/style or None on any failure."""
    if _http is None:
        return None
    key = os.environ.get("AUTODEV_API_KEY")
    if not key:
        return None
    try:
        r = _http.get(
            f"https://auto.dev/api/vin/{vin}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        j = r.json() or {}
        # Common fields — auto.dev's response shape:
        # {"year":"2023","make":"Porsche","model":"Cayenne","trim":"Base",
        #  "style":"Sport Utility 4D Cayenne ..."}
        out = {
            "year":  _to_int(j.get("year")),
            "make":  _to_str(j.get("make")),
            "model": _to_str(j.get("model")),
            "trim":  _to_str(j.get("trim")),
            "style": _to_str(j.get("style")),
        }
        # Some auto.dev responses nest under "styles":[{...}]
        if not out["trim"] and isinstance(j.get("styles"), list) and j["styles"]:
            s0 = j["styles"][0]
            if isinstance(s0, dict):
                out["trim"]  = out["trim"]  or _to_str(s0.get("trim"))
                out["style"] = out["style"] or _to_str(s0.get("name"))
        return out if out.get("trim") else None
    except Exception as e:
        print(f'auto.dev decode error: {e}', flush=True)
        return None


# ═══ Helpers ════════════════════════════════════════════════════════════════

def _to_int(v):
    try:
        return int(str(v).strip()) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _to_str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _strip_ambiguous_trim(t: str) -> str:
    """NHTSA returns 'Trim A / Trim B' or 'Trim A, Trim B' — take the first."""
    if not t:
        return t
    for sep in (" / ", ", ", " or ", "/"):
        if sep in t:
            return t.split(sep)[0].strip()
    return t


# ═══ Unknown-VDS logging (extend tables over time) ══════════════════════════

def _log_vds_unknown(db_conn, vin: str, year, make, model):
    """Insert into vds_unknown if this VIN is a premium-brand WMI and our
    tables don't cover it. Best-effort — swallow all errors."""
    try:
        wmi = (vin or "")[:3].upper()
        if wmi not in PREMIUM_WMIS_TO_LOG:
            return
        cur = db_conn.cursor()
        cur.execute("""
            INSERT INTO vds_unknown (vin, wmi, vds_slice, year, make, model, first_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (vin) DO NOTHING
        """, (vin, wmi, vin[3:8], year, make, model))
        db_conn.commit()
    except Exception as e:
        print(f'vds_unknown log error: {e}', flush=True)


# ═══ Public API ═════════════════════════════════════════════════════════════

def decode_vin_precise(vin: str, *, nhtsa_decoder=None, db_conn=None,
                       allow_paid: bool = True) -> dict:
    """Decode a VIN to precise trim.

    Order of attempts:
      1. Brand-specific VDS table (deterministic when it hits)
      2. auto.dev API (high confidence, costs 1 quota per unique VIN)
      3. NHTSA (fallback with trim-ambiguity strip)

    Args:
        vin: 17-char VIN
        nhtsa_decoder: optional callable(vin) → dict with year/make/model/trim.
                       Pass in app.py's existing decode_vin() to avoid import cycles.
        db_conn: optional live psycopg2 connection for vds_unknown logging
        allow_paid: if False, skip auto.dev (for batch jobs where cost matters)

    Returns dict with trim_confidence ∈ {deterministic, high, medium, low}.
    """
    result = {
        "vin": None, "year": None, "make": None, "model": None,
        "trim": None, "style": None,
        "trim_confidence": "low", "source": "none"
    }
    if not vin or not isinstance(vin, str):
        return result
    vin = vin.strip().upper()
    if len(vin) != 17:
        return result
    result["vin"] = vin

    # Always pull NHTSA for year/make/model baseline
    if nhtsa_decoder:
        try:
            base = nhtsa_decoder(vin) or {}
            result["year"]  = base.get("year")
            result["make"]  = base.get("make")
            result["model"] = base.get("model")
            result["trim"]  = base.get("trim")
            result["source"] = "nhtsa"
        except Exception as e:
            print(f'nhtsa call error in precise decoder: {e}', flush=True)

    # 1. Brand-specific VDS table — deterministic when hit
    for name, fn in BRAND_DECODERS:
        try:
            t = fn(vin)
        except Exception:
            t = None
        if t:
            result["trim"] = t
            result["trim_confidence"] = "deterministic"
            result["source"] = name
            return result

    # 2. auto.dev — paid fallback for non-table brands or when NHTSA is empty/ambiguous
    current_trim = result.get("trim") or ""
    ambiguous = (
        not current_trim
        or current_trim == (result.get("model") or "")
        or any(sep in current_trim for sep in (", ", " / ", " or ", "/"))
    )
    if allow_paid and ambiguous:
        ad = _autodev_decode(vin)
        if ad:
            # auto.dev is often more accurate on make/model spelling too
            if ad.get("make"):  result["make"]  = ad["make"]
            if ad.get("model"): result["model"] = ad["model"]
            if ad.get("year"):  result["year"]  = ad["year"]
            if ad.get("trim"):
                result["trim"]  = ad["trim"]
                result["style"] = ad.get("style")
                result["trim_confidence"] = "high"
                result["source"] = "auto.dev"
                # Log unknown-VDS for premium WMIs — we filled via paid API,
                # but our tables should extend to cover it eventually
                if db_conn:
                    _log_vds_unknown(db_conn, vin,
                                     result["year"], result["make"],
                                     result["model"])
                return result

    # 3. NHTSA strip-ambiguous fallback
    if result["trim"]:
        original = result["trim"]
        cleaned = _strip_ambiguous_trim(original)
        if cleaned != original:
            result["trim"] = cleaned
            result["trim_confidence"] = "medium"
        elif result["trim"] and result["trim"] != result.get("model"):
            result["trim_confidence"] = "medium"

    # Log VIN if premium-brand WMI but we couldn't get deterministic trim
    if db_conn and result["trim_confidence"] in ("low", "medium"):
        _log_vds_unknown(db_conn, vin,
                         result.get("year"), result.get("make"),
                         result.get("model"))
    return result
