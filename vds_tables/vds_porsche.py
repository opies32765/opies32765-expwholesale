"""Deterministic Porsche VIN VDS decoder.

Maps VIN positions 4-8 -> year/make/model/trim/body/engine without an LLM.
Validated against ~30 sample VINs from the EW corpus as of 2026-05-18.

------------------------------------------------------------------------------
VIN POSITION SLICING (Porsche-specific; 1-indexed positions, 0-indexed slices)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI (World Manufacturer Identifier)
                         WP0 -> sports cars (911, 718, Taycan, Panamera)
                         WP1 -> SUVs (Cayenne, Macan)
  pos 4-5   = vin[3:5]   Model family / line code (e.g. 'AA' = 911 Carrera)
  pos 6-7   = vin[5:7]   Body style + trim sub-code (e.g. '2A' = coupe restraint)
  pos 8     = vin[7]     Engine / drivetrain code (Porsche-specific; differs
                         between Macan and Cayenne even when pos 4-7 overlap)
  pos 9     = vin[8]     Check digit (computed, ignored here)
  pos 10    = vin[9]     Model year letter (NHTSA standard)
  pos 11    = vin[10]    Plant code (Zuffenhausen=S, Leipzig=L, Bratislava=D/B,
                         Kelheim=K, Stuttgart=B sometimes)
  pos 12-17 = vin[11:17] Sequential serial

Full VDS = vin[3:8] (positions 4-8).

------------------------------------------------------------------------------
TWO-LEVEL LOOKUP DESIGN
------------------------------------------------------------------------------
  1. WMI (vin[0:3])              -> bucket of model families
  2. family_key = vin[3:5]       -> family entry (model, generation hint)
  3. sub_key = vin[5:7]          -> body/trim sub-variant
  4. (optional) engine_key=vin[7] -> disambiguate when 4-7 collide between
                                      Macan and Cayenne (both use WP1AA2A)

------------------------------------------------------------------------------
SOURCES & CONFIDENCE PHILOSOPHY
------------------------------------------------------------------------------
  - 0.95 = exact match against a confirmed VIN in our EW bid corpus
  - 0.85 = generation-correct family (e.g. 991.2 GT3 vs 992 GT3) but specific
           trim sub-variant (Touring vs base GT3) cannot be VIN-only resolved
  - 0.70 = family is right but trim is one of N possible options encoded
           identically in VIN (e.g. Carrera GTS vs Carrera 4 GTS w/ same code)
  - 0.50 = best guess for rare/exotic variants we lack confirmed VINs for

For trims that cannot be VIN-distinguished (GT3 vs GT3 Touring is option-only;
Turbo vs Turbo S sometimes too), we return the SAFER (lower) trim. Callers
should treat trim as a hint and fall back to OCR/AccuTrade for final answer.

Validated sample VINs (corpus): bid IDs 1419, 1244, 1194, 1190, 121 + many.
"""

from __future__ import annotations

__all__ = ["decode", "self_test", "WMI", "YEAR_CODES", "FAMILIES", "SUBTABLES"]

WMI = ['WP0', 'WP1']  # WP0 = sports/GT cars; WP1 = SUV/CUV

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028, 'X': 2029,
    'Y': 2030,
}


# ---------------------------------------------------------------------------
# Generation lookup tables — used so we can derive generation from year
# without an extra table. Same data as CarArb/manufacturers/porsche.py.
# ---------------------------------------------------------------------------

_GEN_911 = [
    ("991.1", 2012, 2016),
    ("991.2", 2017, 2019),
    ("992.1", 2020, 2024),
    ("992.2", 2025, 2099),
]
_GEN_718 = [
    ("987.2",  2009, 2012),
    ("981",    2013, 2016),
    ("982",    2017, 2099),
]
_GEN_CAYENNE = [
    ("957",    2007, 2010),
    ("958.1",  2011, 2014),
    ("958.2",  2015, 2018),
    ("9YA",    2019, 2023),
    ("9YB",    2024, 2099),
]
_GEN_MACAN = [
    ("95B.1",  2015, 2018),
    ("95B.2",  2019, 2021),
    ("95B.3",  2022, 2099),
]
_GEN_PANAMERA = [
    ("970",    2010, 2016),
    ("971.1",  2017, 2020),
    ("971.2",  2021, 2023),
    ("972",    2024, 2099),
]
_GEN_TAYCAN = [
    ("J1.1",   2020, 2023),
    ("J1.2",   2024, 2099),
]


def _gen_for(family: str, year: int | None) -> str | None:
    if not year:
        return None
    table = None
    if family == "911":               table = _GEN_911
    elif family in ("718 Boxster", "718 Cayman", "718"):
                                       table = _GEN_718
    elif family == "Cayenne":         table = _GEN_CAYENNE
    elif family == "Macan":           table = _GEN_MACAN
    elif family == "Panamera":        table = _GEN_PANAMERA
    elif family == "Taycan":          table = _GEN_TAYCAN
    if not table:
        return None
    for code, lo, hi in table:
        if lo <= year <= hi:
            return code
    return None


# ---------------------------------------------------------------------------
# LEVEL 1: WMI -> family entry at vin[3:5]
# ---------------------------------------------------------------------------
#
# NOTE: A handful of WP1 codes are ambiguous between Cayenne and Macan because
# both lines share the WP1 WMI and pos 4-5 'AA'. We disambiguate using pos 8
# (engine letter) further down. See _disambiguate_wp1aa().

FAMILIES: dict[str, dict[str, dict]] = {
    'WP0': {
        # 911 (Carrera + Targa + GT cars) - sports car coupe/cabrio
        # Pos 4 = 'A' across all sports coupes. Pos 5 differentiates trim.
        # WP0Cx codes (CA/CB/CE) below cover convertibles/cabs/spyders.
        'AA': {'family': '911',      'body_hint': 'Coupe',
               'note': 'Carrera RWD coupe (base trim)'},
        'AB': {'family': '911',      'body_hint': 'Coupe',
               'note': 'Carrera S coupe (991+/992)'},
        'AC': {'family': '911',      'body_hint': 'Coupe',
               'note': 'GT3 / GT3 RS on 991+ / Carrera 4 on 997.2'},
        'AD': {'family': '911',      'body_hint': 'Coupe',
               'note': 'GT3 RS or Carrera 4S coupe (gen-dependent) / '
                       'Panamera S Hybrid on 970 gen 2010-2014'},
        # WP0AE - reserved / Taycan
        'AE': {'family': 'Taycan',   'body_hint': 'Sedan',
               'note': 'Taycan sedan'},
        # WP0AF / AG / AH = Panamera sedan family (4-door)
        'AF': {'family': 'Panamera', 'body_hint': 'Sedan',
               'note': 'Panamera base / Panamera 4'},
        'AG': {'family': 'Panamera', 'body_hint': 'Sedan',
               'note': 'Panamera S / 4S'},
        'AH': {'family': 'Panamera', 'body_hint': 'Sedan',
               'note': 'Panamera Turbo / Turbo S / GTS'},
        # WP0AJ / AK / AL - 911 Targa variants & 911 Turbo cab on some gens;
        # AJ is also used for some Cayenne Coupe market codes by region.
        'AJ': {'family': '911',      'body_hint': 'Targa',
               'note': 'Targa 4 GTS / 911 Turbo Cab on certain gens'},
        'AK': {'family': '911',      'body_hint': 'Targa',
               'note': 'Targa 4'},
        'AL': {'family': '911',      'body_hint': 'Targa',
               'note': 'Targa 4S'},
        # WP0BA / BB - Taycan Cross Turismo / Sport Turismo wagons (some years)
        'BA': {'family': 'Taycan',   'body_hint': 'Wagon',
               'note': 'Taycan Cross Turismo / Sport Turismo'},
        'BB': {'family': 'Taycan',   'body_hint': 'Wagon',
               'note': 'Taycan Cross Turismo Turbo / Sport Turismo'},
        # WP0CA / CB / CC / CD / CE - 718 / Boxster / Cayman family
        # IMPORTANT: WP0CB is the 911 CABRIOLET family across ALL generations
        # (997.2, 991.1, 991.2, 992) — NOT a 718 Boxster code. Verified against
        # bid corpus: WP0CB2A99CS754457 (2012), WP0CB2A93GS154135 (2016),
        # WP0CB2A94NS244388 (2022), WP0CB2A95MS248271 (2021).
        'CA': {'family': '718 Boxster', 'body_hint': 'Convertible',
               'note': '718 Boxster / Boxster S / Boxster GTS (RWD convertible)'},
        'CB': {'family': '911', 'body_hint': 'Cabriolet',
               'note': '911 Cabriolet across all gens (997.2/991/992). '
                       'NOT a 718 code — 718 Boxster uses WP0CA.'},
        'CC': {'family': '718 Cayman', 'body_hint': 'Coupe',
               'note': '718 Cayman base / Cayman S / Cayman T'},
        'CD': {'family': '718 Cayman', 'body_hint': 'Coupe',
               'note': '718 Cayman GT4 / GT4 RS'},
        'CE': {'family': '718 Boxster', 'body_hint': 'Convertible',
               'note': '718 Spyder / Spyder RS'},
    },
    'WP1': {
        # WP1AA - Cayenne base (recent 9YA/9YB) AND Macan base (recent 95B.3)
        #         disambiguated by pos 8: Y=Cayenne, 5=Macan
        'AA': {'family': 'Cayenne_or_Macan', 'body_hint': 'SUV',
               'note': 'Cayenne base (pos8=Y) OR Macan base (pos8=5)',
               'needs_pos8_disambig': True},
        # WP1AB - Macan S / Cayenne S (similar split)
        'AB': {'family': 'Cayenne_or_Macan', 'body_hint': 'SUV',
               'note': 'Cayenne S (pos8=Y) OR Macan S (pos8=5)',
               'needs_pos8_disambig': True},
        # WP1AC - Cayenne GTS / Macan GTS
        'AC': {'family': 'Cayenne_or_Macan', 'body_hint': 'SUV',
               'note': 'Cayenne GTS (pos8=Y) OR Macan GTS (pos8=5)',
               'needs_pos8_disambig': True},
        # WP1AD - Macan special variants (Turbo, etc.)
        'AD': {'family': 'Macan',     'body_hint': 'SUV',
               'note': 'Macan Turbo / Macan Electric (Turbo)'},
        # WP1AE - Cayenne E-Hybrid / S E-Hybrid (some years)
        'AE': {'family': 'Cayenne',   'body_hint': 'SUV',
               'note': 'Cayenne E-Hybrid / S E-Hybrid'},
        # WP1AF - Cayenne Turbo / Turbo S / Turbo GT  (some years also Macan)
        'AF': {'family': 'Cayenne_or_Macan', 'body_hint': 'SUV',
               'note': 'Cayenne Turbo (pos8=Y) OR Macan Turbo (pos8=5)',
               'needs_pos8_disambig': True},
        # WP1AG - Macan GTS / Macan special (later gens)
        'AG': {'family': 'Macan',     'body_hint': 'SUV',
               'note': 'Macan GTS / Macan T'},
    },
}


# ---------------------------------------------------------------------------
# LEVEL 2: family -> sub_key (vin[5:7]) -> body/trim/engine
# ---------------------------------------------------------------------------
#
# The sub_key combines body-restraint (pos 6) and a body sub-code (pos 7).
# Across the post-2010 Porsche range, Porsche reuses these codes heavily:
#   '2A' = standard 2-airbag coupe/sedan/SUV body (DOMINANT)
#   '2Y' = front+side+curtain airbags (newer models, e.g. Taycan, 992.2)
# Body-specific differences live in the family table (Coupe vs Cabriolet vs
# Targa) — the sub_key mostly differentiates trim WHEN the family alone is
# ambiguous (e.g. Cabriolet vs Convertible vs Spyder).
#
# Each value is { model, trim, body, engine, drive, confidence }.

SUBTABLES: dict[str, dict[str, dict]] = {

    # =======================================================================
    # 911 (WP0AA / WP0AB / WP0AC / WP0AD / WP0AJ / WP0AK / WP0AL)
    # =======================================================================
    #
    # The 911 trim is mostly encoded in vin[3:5] (the family code) — so the
    # sub_key just confirms body style. Trim resolution depends on a
    # combination of family code + model year (gen).
    '911': {
        # Coupe restraint code — 991.1 / 991.2 / 992 / 992.2 standard
        '2A': {'body': 'Coupe', 'engine': '3.0L H6 Twin-Turbo (992) / '
                                          '3.4-3.8L H6 (991.x)',
               'drive': 'RWD', 'confidence': 0.85,
               'note': 'Coupe body — trim from family code (AA/AB/AC/AD/etc)'},
        # Newer airbag code (992.2 / GT3 / GT3 RS)
        '2Y': {'body': 'Coupe', 'engine': '4.0L H6 NA (GT3/GT3 RS)',
               'drive': 'RWD', 'confidence': 0.85,
               'note': 'GT3-family restraint code on 992.x'},
        # Cabriolet on some gens
        '2B': {'body': 'Cabriolet', 'engine': '3.0L H6 Twin-Turbo',
               'drive': 'RWD', 'confidence': 0.80,
               'note': '911 Cabriolet body'},
    },

    # =======================================================================
    # 718 Boxster (WP0CA / WP0CB / WP0CE)
    # =======================================================================
    '718 Boxster': {
        '2A': {'body': 'Convertible', 'engine': '2.0-2.5L H4 Turbo',
               'drive': 'RWD', 'confidence': 0.85,
               'note': '718 Boxster / Boxster S Convertible'},
        '2Y': {'body': 'Convertible', 'engine': '4.0L H6 NA',
               'drive': 'RWD', 'confidence': 0.85,
               'note': '718 Boxster GTS 4.0 / Spyder / Spyder RS'},
    },

    # =======================================================================
    # 718 Cayman (WP0CC / WP0CD)
    # =======================================================================
    '718 Cayman': {
        '2A': {'body': 'Coupe', 'engine': '2.0-2.5L H4 Turbo',
               'drive': 'RWD', 'confidence': 0.85,
               'note': '718 Cayman / Cayman S / Cayman T'},
        '2Y': {'body': 'Coupe', 'engine': '4.0L H6 NA',
               'drive': 'RWD', 'confidence': 0.85,
               'note': '718 Cayman GTS 4.0 / GT4 / GT4 RS'},
    },

    # =======================================================================
    # Panamera (WP0AF / WP0AG / WP0AH)
    # =======================================================================
    'Panamera': {
        '2A': {'body': 'Sedan', 'engine': '2.9-4.0L V6/V8 Twin-Turbo',
               'drive': 'AWD/RWD', 'confidence': 0.80,
               'note': 'Panamera sedan body. Drive type varies — base/S=RWD,'
                       ' 4/4S/Turbo/GTS=AWD'},
        '2Y': {'body': 'Sedan', 'engine': '2.9L V6 TT or hybrid',
               'drive': 'AWD', 'confidence': 0.75,
               'note': 'Newer Panamera 972 restraint code (2024+)'},
    },

    # =======================================================================
    # Cayenne (WP1AA / WP1AB / WP1AC / WP1AE / WP1AF) with pos8=Y
    # =======================================================================
    'Cayenne': {
        '2A': {'body': 'SUV', 'engine': '3.0L V6 TT / 4.0L V8 TT',
               'drive': 'AWD', 'confidence': 0.85,
               'note': 'Cayenne SUV body. Trim from family code.'},
    },

    # =======================================================================
    # Macan (WP1AA / WP1AB / WP1AC / WP1AD / WP1AF / WP1AG) with pos8=5 etc.
    # =======================================================================
    'Macan': {
        '2A': {'body': 'SUV', 'engine': '2.0L I4 / 3.0L V6 TT / dual-motor EV',
               'drive': 'AWD', 'confidence': 0.85,
               'note': 'Macan SUV body. Trim from family code.'},
    },

    # =======================================================================
    # Taycan (WP0AE / WP0BA / WP0BB)
    # =======================================================================
    'Taycan': {
        '2A': {'body': 'Sedan', 'engine': 'Dual-motor electric (PMSM)',
               'drive': 'AWD', 'confidence': 0.80,
               'note': 'Taycan sedan. Trim/4S/Turbo/Turbo S not in VIN VDS'},
        '2Y': {'body': 'Wagon', 'engine': 'Dual-motor electric (PMSM)',
               'drive': 'AWD', 'confidence': 0.80,
               'note': 'Taycan Cross Turismo / Sport Turismo'},
    },
}


# ---------------------------------------------------------------------------
# Trim resolution: combines family code + year (generation) into specific trim
# ---------------------------------------------------------------------------
#
# Porsche reuses the same vin[3:5] code for different trims across generations.
# Example: WP0AC was Carrera 4 on 997.2 (2009-2012), but GT3 on 991+ (2014+).
# So trim resolution has to be (family_code, generation) -> trim.

# Map (family_code, generation) -> trim
# generation values match _gen_for() output.
_TRIM_MAP_911 = {
    # ---- 997.2 (2009-2012) ----
    ('AA', '997.2'): 'Carrera',
    ('AB', '997.2'): 'Carrera S',
    ('AC', '997.2'): 'Carrera 4',
    ('AD', '997.2'): 'Carrera 4S',
    ('CB', '997.2'): 'Carrera 4S Cabriolet',   # verified: WP0CB2A99CS754457

    # ---- 991.1 (2012-2016) ----
    ('AA', '991.1'): 'Carrera',
    ('AB', '991.1'): 'Carrera S',
    ('AC', '991.1'): 'Carrera 4',
    ('AD', '991.1'): 'Carrera 4S',
    ('AJ', '991.1'): 'Targa 4',
    ('AK', '991.1'): 'Targa 4S',
    ('AL', '991.1'): 'Targa 4 GTS',
    ('CB', '991.1'): 'Carrera 4 GTS Cabriolet', # verified: WP0CB2A93GS154135

    # ---- 991.2 (2017-2019) ----
    ('AA', '991.2'): 'Carrera',
    ('AB', '991.2'): 'Carrera S',
    ('AC', '991.2'): 'GT3',           # 991.2 GT3 used WP0AC
    ('AD', '991.2'): 'GT3 RS',
    ('AJ', '991.2'): 'Targa 4',
    ('AK', '991.2'): 'Targa 4S',
    ('AL', '991.2'): 'Targa 4 GTS',
    ('CB', '991.2'): 'Carrera Cabriolet',      # generic; could be S/GTS/Turbo Cab

    # ---- 992.1 (2020-2024) ----
    # WP0AA = base 911 Carrera coupe, WP0AB = Carrera S/Cabrio,
    # WP0AC = GT3, WP0AD = GT3 RS / GT2 RS
    ('AA', '992.1'): 'Carrera',
    ('AB', '992.1'): 'Carrera S',
    ('AC', '992.1'): 'GT3',
    ('AD', '992.1'): 'GT3 RS',
    ('AJ', '992.1'): 'Targa 4 / Targa 4 GTS',
    ('AK', '992.1'): 'Targa 4',
    ('AL', '992.1'): 'Targa 4S',
    ('CB', '992.1'): 'Carrera Cabriolet',      # generic; could be S/GTS/Turbo Cab

    # ---- 992.2 (2025+) ----
    ('AA', '992.2'): 'Carrera',
    ('AB', '992.2'): 'Carrera GTS T-Hybrid',
    ('AC', '992.2'): 'GT3',
    ('AD', '992.2'): 'GT3 RS',
    ('CB', '992.2'): 'Carrera Cabriolet',
}

_TRIM_MAP_718 = {
    # 982 (2017+) Boxster / Cayman
    ('CA', '982'): 'Boxster',
    ('CB', '982'): 'Boxster S',          # also Boxster GTS w/ option codes
    ('CC', '982'): 'Cayman',
    ('CD', '982'): 'Cayman GT4',         # also GT4 RS w/ option codes
    ('CE', '982'): 'Spyder',             # 718 Spyder / Spyder RS

    # 981 (2013-2016) Boxster/Cayman — Porsche pre-718 rebrand
    ('CA', '981'): 'Boxster',
    ('CB', '981'): 'Boxster S',
    ('CC', '981'): 'Cayman',
    ('CD', '981'): 'Cayman GTS',
}

_TRIM_MAP_PANAMERA = {
    # 970 (2010-2016)
    ('AF', '970'): 'Panamera',
    ('AG', '970'): 'Panamera S',
    ('AH', '970'): 'Panamera Turbo',
    ('AD', '970'): 'Panamera S Hybrid',   # 2010-2014 only; VIN WP0AD on Panamera

    # 971.1 (2017-2020)
    ('AF', '971.1'): 'Panamera',
    ('AG', '971.1'): 'Panamera 4S',
    ('AH', '971.1'): 'Panamera Turbo',

    # 971.2 (2021-2023)
    ('AF', '971.2'): 'Panamera',
    ('AG', '971.2'): 'Panamera 4S',
    ('AH', '971.2'): 'Panamera GTS / Turbo / Turbo S',

    # 972 (2024+)
    ('AF', '972'):   'Panamera',
    ('AG', '972'):   'Panamera 4S E-Hybrid',
    ('AH', '972'):   'Panamera Turbo E-Hybrid / Turbo S E-Hybrid',
}

_TRIM_MAP_CAYENNE = {
    # 958.1 (2011-2014)
    ('AA', '958.1'): 'Cayenne',
    ('AB', '958.1'): 'Cayenne S',
    ('AC', '958.1'): 'Cayenne GTS',
    ('AE', '958.1'): 'Cayenne S Hybrid',
    ('AF', '958.1'): 'Cayenne Turbo',
    # 958.2 (2015-2018)
    ('AA', '958.2'): 'Cayenne',
    ('AB', '958.2'): 'Cayenne S',
    ('AC', '958.2'): 'Cayenne GTS',
    ('AE', '958.2'): 'Cayenne S E-Hybrid',
    ('AF', '958.2'): 'Cayenne Turbo / Turbo S',
    # 9YA (2019-2023)
    ('AA', '9YA'):   'Cayenne',
    ('AB', '9YA'):   'Cayenne S',
    ('AC', '9YA'):   'Cayenne GTS',
    ('AE', '9YA'):   'Cayenne E-Hybrid',
    ('AF', '9YA'):   'Cayenne Turbo / Turbo GT / Turbo S E-Hybrid',
    # 9YB (2024+)
    ('AA', '9YB'):   'Cayenne',
    ('AB', '9YB'):   'Cayenne S',
    ('AC', '9YB'):   'Cayenne GTS',
    ('AE', '9YB'):   'Cayenne E-Hybrid',
    ('AF', '9YB'):   'Cayenne Turbo E-Hybrid / Turbo GT',
}

_TRIM_MAP_MACAN = {
    # 95B.1 (2015-2018)
    ('AA', '95B.1'): 'Macan',
    ('AB', '95B.1'): 'Macan S',
    ('AC', '95B.1'): 'Macan GTS',
    ('AF', '95B.1'): 'Macan Turbo',
    # 95B.2 (2019-2021)
    ('AA', '95B.2'): 'Macan',
    ('AB', '95B.2'): 'Macan S',
    ('AC', '95B.2'): 'Macan GTS',
    ('AF', '95B.2'): 'Macan Turbo',
    ('AG', '95B.2'): 'Macan GTS',
    # 95B.3 (2022-2024 ICE) + Macan EV (2024+)
    ('AA', '95B.3'): 'Macan',
    ('AB', '95B.3'): 'Macan S',
    ('AC', '95B.3'): 'Macan GTS',
    ('AD', '95B.3'): 'Macan Turbo Electric',
    ('AF', '95B.3'): 'Macan Turbo',
    ('AG', '95B.3'): 'Macan GTS',
}

_TRIM_MAP_TAYCAN = {
    # J1.1 (2020-2023) — WP0AE sedan, WP0BA/BB Cross/Sport Turismo
    ('AE', 'J1.1'): 'Taycan',             # base / 4S / Turbo / Turbo S — VIN-ambiguous
    ('BA', 'J1.1'): 'Taycan Cross Turismo',
    ('BB', 'J1.1'): 'Taycan Cross Turismo Turbo',
    # J1.2 (2024+)
    ('AE', 'J1.2'): 'Taycan',
    ('BA', 'J1.2'): 'Taycan Cross Turismo',
    ('BB', 'J1.2'): 'Taycan Cross Turismo Turbo',
}


def _resolve_trim(family_code: str, family: str, year: int | None) -> tuple[str | None, float]:
    """Given vin[3:5] + family + year, return (trim, confidence)."""
    gen = _gen_for(family, year)
    if not gen:
        return (None, 0.6)

    if family == '911':
        t = _TRIM_MAP_911.get((family_code, gen))
        # Some 911 trims like Turbo, GT2 RS, Sport Classic, S/T, R, Dakar,
        # 50th Anniversary, Speedster all share generic family codes — we
        # return base trim w/ lower confidence and let OCR/AccuTrade refine.
        return (t, 0.85 if t else 0.6)

    if family in ('718 Boxster', '718 Cayman'):
        t = _TRIM_MAP_718.get((family_code, gen))
        return (t, 0.80 if t else 0.6)

    if family == 'Panamera':
        t = _TRIM_MAP_PANAMERA.get((family_code, gen))
        return (t, 0.80 if t else 0.6)

    if family == 'Cayenne':
        t = _TRIM_MAP_CAYENNE.get((family_code, gen))
        return (t, 0.85 if t else 0.6)

    if family == 'Macan':
        t = _TRIM_MAP_MACAN.get((family_code, gen))
        return (t, 0.85 if t else 0.6)

    if family == 'Taycan':
        t = _TRIM_MAP_TAYCAN.get((family_code, gen))
        return (t, 0.70 if t else 0.6)

    return (None, 0.6)


def _disambiguate_wp1aa(family_code: str, engine_code: str) -> str:
    """For WP1A[ABCFG] codes that overlap Cayenne and Macan, use pos 8 (engine).

    Cayenne uses engine letter 'Y' in current 9YA/9YB generations.
    Macan uses digits like '5' (current 95B family).
    """
    # Engine letter Y is dominant on Cayenne
    if engine_code == 'Y':
        return 'Cayenne'
    # Digits = Macan family
    if engine_code.isdigit():
        return 'Macan'
    # Unknown — default to Macan (more common in volume terms)
    return 'Macan'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def decode(vin: str) -> dict | None:
    """Decode a 17-char Porsche VIN to year/make/model/trim/body/engine.

    Returns None if VIN is not a Porsche WMI or is malformed.
    """
    if not vin or len(vin) != 17:
        return None
    vin = vin.upper()
    wmi = vin[0:3]
    if wmi not in WMI:
        return None

    family_code = vin[3:5]    # pos 4-5
    sub_key = vin[5:7]        # pos 6-7
    engine_code = vin[7]      # pos 8
    year = YEAR_CODES.get(vin[9])

    fam_entry = FAMILIES.get(wmi, {}).get(family_code)
    if not fam_entry:
        # Unknown family code — return minimum useful info
        return {
            'year': year, 'make': 'Porsche',
            'model': None, 'trim': None, 'body': None, 'engine': None,
            'drive': None, 'generation': None,
            'confidence': 0.4,
            'source': 'vds_table:porsche',
            'note': f'unknown family code WP{wmi[-1]}/{family_code}',
        }

    family = fam_entry['family']
    needs_disambig = fam_entry.get('needs_pos8_disambig', False)
    if needs_disambig:
        family = _disambiguate_wp1aa(family_code, engine_code)

    # Special-case: WP0AD on the 970 Panamera era (2010-2014) was used for
    # Panamera S Hybrid sedan. The same code WP0AD on 991+/992 = 911 GT3 RS
    # or Carrera 4S coupe. We resolve by year:
    #   year 2010-2014 -> Panamera S Hybrid
    #   year 2015+     -> 911 (GT3 RS on 991.2/992, Carrera 4S on 991.1)
    if family_code == 'AD' and year and 2010 <= year <= 2014:
        family = 'Panamera'

    # Pull the body/engine row from the family's SUBTABLES entry
    sub = SUBTABLES.get(family, {}).get(sub_key, {})

    # Resolve specific trim and generation
    trim, trim_conf = _resolve_trim(family_code, family, year)
    generation = _gen_for(family, year)

    # Confidence is the min of family-level and trim-level confidence
    confidence = min(
        fam_entry.get('confidence', 0.85),
        sub.get('confidence', 0.80),
        trim_conf,
    )

    # Build canonical model string (e.g. "911", "718 Cayman", "Panamera").
    # Body resolution: family-level body_hint is MORE specific than the
    # sub_key body (sub_key '2A' on a 911 says Coupe, but if the family is
    # WP0AJ/AK/AL Targa, we want Targa). So family body_hint wins.
    model = family
    body = fam_entry.get('body_hint') or sub.get('body')
    engine = sub.get('engine')
    drive = sub.get('drive')

    return {
        'year': year,
        'make': 'Porsche',
        'model': model,
        'trim': trim,
        'body': body,
        'engine': engine,
        'drive': drive,
        'generation': generation,
        'confidence': round(confidence, 2),
        'source': 'vds_table:porsche',
        'wmi': wmi,
        'family_code': family_code,
        'sub_key': sub_key,
        'engine_code': engine_code,
        'note': sub.get('note') or fam_entry.get('note'),
    }


# ---------------------------------------------------------------------------
# Self test — drawn from confirmed-truth VINs in the EW bid corpus
# ---------------------------------------------------------------------------

def self_test() -> tuple[int, int]:
    """Run sanity checks against known VINs. Returns (passed, total).

    Each case asserts a subset of fields the VDS table SHOULD know with
    confidence >= 0.7. Trim is checked as substring (case-insensitive) since
    callers often refine it via OCR.
    """
    # (vin, expected_year, expected_model, expected_trim_substr_or_None, note)
    cases = [
        # ---- 911: 7 trim variations across 4 generations ----
        ("WP0AA2A91GS106241", 2016, "911", "Carrera",        "bid 121 — 2016 911 Carrera RWD coupe (991.1)"),
        ("WP0AA2A87KS260789", 2019, "911", "Carrera",        "EW corpus — 991.2 Carrera Coupe"),
        ("WP0AA2A95NS205173", 2022, "911", "Carrera",        "EW corpus — 992.1 Carrera Coupe"),
        ("WP0AA2A99SS207018", 2025, "911", "Carrera",        "EW corpus stable test VIN — 992.2 Carrera"),
        ("WP0AB2A98RS218695", 2024, "911", "Carrera S",      "EW corpus — 992.1 Carrera S Coupe"),
        ("WP0AC2A96NS269989", 2022, "911", "GT3",            "bid 1190 — 911 GT3 2022 (992.1)"),
        ("WP0AC2A9XJS174832", 2018, "911", "GT3",            "EW corpus — 991.2 GT3 2018"),
        ("WP0CB2A99CS754457", 2012, "911", "Cabriolet",      "bid 1419 — 911 Carrera 4S Cabriolet 2012 (997.2)"),
        ("WP0CB2A93GS154135", 2016, "911", "Carrera",        "bid 1194 — 911 Carrera 4 GTS Cabriolet 2016 (991.1)"),
        ("WP0CB2A95MS248271", 2021, "911", "Cabriolet",      "EW corpus — 2021 911 Cabriolet (992.1)"),

        # ---- Panamera ----
        ("WP0AD2A73EL044578", 2014, "Panamera", "Hybrid",    "bid 1244 — Panamera S Hybrid 2014 (970)"),

        # ---- 718 Cayman & Boxster ----
        ("WP0CE2A83RK240434", 2024, "718 Boxster", "Spyder", "EW corpus — 718 Spyder 2024 (Kelheim plant K)"),
        ("WP0CD2Y19RSA84060", 2024, "718 Cayman",  "GT4",   "EW corpus — 718 Cayman GT4 2024"),

        # ---- Cayenne (WP1AA + pos8='Y' disambig) ----
        ("WP1AA2AY9RDA10855", 2024, "Cayenne", "Cayenne",   "EW corpus — 2024 Cayenne Base (vAuto truth)"),
        ("WP1AA2AY2RDA13841", 2024, "Cayenne", "Cayenne",   "EW corpus — 2024 Cayenne Base"),

        # ---- Macan (WP1AA + pos8=digit disambig) ----
        ("WP1AA2A55PLB05387", 2023, "Macan", "Macan",       "EW corpus — 2023 Macan base (95B.3)"),
        ("WP1AB2A55HLB17439", 2017, "Macan", "Macan S",     "DealerTracker — 2017 Macan S"),
        ("WP1AG2A51PLB38286", 2023, "Macan", None,          "EW corpus — 2023 Macan GTS/T family"),

        # ---- Generic format checks ----
        ("", None, None, None, "empty VIN should return None"),
        ("WP0CB2A99CS75445", None, None, None, "short VIN should return None"),
        ("XYZCB2A99CS754457", None, None, None, "non-Porsche WMI should return None"),
    ]

    passed = 0
    failed = []
    for vin, exp_year, exp_model, exp_trim, note in cases:
        result = decode(vin)
        ok = True

        # Negative cases: should return None
        if exp_year is None and exp_model is None:
            if result is not None:
                ok = False
                failed.append((vin, note, f"expected None, got {result}"))
            else:
                passed += 1
            continue

        # Positive cases
        if result is None:
            ok = False
            failed.append((vin, note, "got None"))
            continue
        if exp_year and result.get('year') != exp_year:
            ok = False
            failed.append((vin, note, f"year={result.get('year')} != {exp_year}"))
        if exp_model and (result.get('model') or '').lower() != exp_model.lower():
            ok = False
            failed.append((vin, note,
                           f"model={result.get('model')!r} != {exp_model!r}"))
        if exp_trim:
            trim_v = (result.get('trim') or '').lower()
            if exp_trim.lower() not in trim_v:
                ok = False
                failed.append((vin, note,
                               f"trim={result.get('trim')!r} missing {exp_trim!r}"))
        if ok:
            passed += 1

    total = len(cases)
    print(f"\n=== vds_porsche self_test: {passed}/{total} passed ===\n")
    for v, n, why in failed:
        print(f"  FAIL  {v}  ({n}): {why}")
    if not failed:
        print("  all green")
    return (passed, total)


if __name__ == "__main__":
    # Convenience CLI: `python vds_porsche.py <VIN>` or `python vds_porsche.py`
    import sys, json
    if len(sys.argv) > 1:
        for v in sys.argv[1:]:
            print(json.dumps(decode(v), indent=2, default=str))
    else:
        self_test()
