"""vin_prefix_trim.py — VIN-prefix → trim lookup for trim-blind makes.

NHTSA doesn't return Trim for some manufacturers because trim isn't encoded
in the VIN's standard VDS bytes (Ford encodes trim in option codes, not the
VIN; Porsche encodes body+trim family in chars 4-5 but NHTSA doesn't decode
them). This module fills the gap with a hand-curated dict.

Used by `canonicalize_bid.py` as the second-tier lookup after NHTSA.

Coverage philosophy: prioritize makes where wrong-trim costs us money
(Porsche, Ferrari, Lamborghini, exotics). Mainstream makes where trim is
in option codes (Ford F-series XL/XLT/Lariat) deliberately leave trim as
None and let the AccuTrade overseer / iPacket OCR fill it later.

Confidence:
  0.95 = single trim certainty from VIN-prefix (Porsche WP0AC → GT3 family)
  0.85 = narrow trim family (Porsche WP0CA → 718 Boxster, but not S/Spyder)
  0.70 = body+drivetrain confirmed, trim still ambiguous
  None = no entry, no fallback
"""

# (prefix, trim, confidence, note)
# Prefix is the first N chars of the VIN — we match longest-first.
_VIN_PREFIX_TABLE: list[tuple[str, str, float, str]] = [
    # ── Porsche 911 (WP0Ax) ───────────────────────────────────────────────
    ('WP0AA', 'Carrera', 0.90, 'Porsche 911 base Carrera RWD coupe/cabriolet'),
    ('WP0AB', 'Carrera S', 0.90, 'Porsche 911 Carrera S RWD'),
    ('WP0AC', 'GT3', 0.85, 'Porsche 911 GT3 / GT3 Touring'),
    ('WP0AD', 'GT3 RS', 0.95, 'Porsche 911 GT3 RS'),
    ('WP0AE', 'Carrera 4', 0.90, 'Porsche 911 Carrera 4 AWD'),
    ('WP0AF', 'Carrera 4S', 0.90, 'Porsche 911 Carrera 4S AWD'),
    ('WP0AG', 'Turbo', 0.85, 'Porsche 911 Turbo / Turbo S'),
    ('WP0AH', 'GTS', 0.85, 'Porsche 911 GTS variants (Carrera GTS / Targa GTS)'),
    ('WP0AJ', 'Targa 4 GTS', 0.85, 'Porsche 911 Targa 4 GTS'),
    ('WP0AK', 'Targa 4', 0.85, 'Porsche 911 Targa 4'),
    ('WP0AL', 'Targa 4S', 0.85, 'Porsche 911 Targa 4S'),
    # ── Porsche 718 / Cayman / Boxster (WP0Cx) ────────────────────────────
    ('WP0CA', '718 Boxster', 0.85, 'Porsche 718 Boxster base'),
    ('WP0CB', '718 Boxster Cabriolet', 0.80, 'Porsche 718 Boxster / Boxster S Cabriolet variants'),
    ('WP0CC', '718 Cayman', 0.85, 'Porsche 718 Cayman / Cayman S'),
    ('WP0CD', '718 Cayman GT4', 0.90, 'Porsche 718 Cayman GT4 / GT4 RS'),
    ('WP0CE', '718 Spyder', 0.90, 'Porsche 718 Spyder / Spyder RS'),
    # ── Porsche Macan (WP1Ax) ─────────────────────────────────────────────
    ('WP1AA', 'Macan', 0.85, 'Porsche Macan base'),
    ('WP1AB', 'Macan S', 0.85, 'Porsche Macan S'),
    ('WP1AC', 'Macan GTS', 0.90, 'Porsche Macan GTS'),
    ('WP1AF', 'Macan Turbo', 0.90, 'Porsche Macan Turbo / Turbo Electric'),
    # ── Porsche Cayenne (WP1A — different family) ────────────────────────
    # Note: Cayenne shares the WP1 WMI with Macan; distinguish on model year
    # length + chars 7-8 (more complex). For now, list only the well-known
    # high-confidence performance variants.
    ('WP1AC2A', 'Cayenne GTS', 0.85, 'Porsche Cayenne GTS Coupe'),
    ('WP1AF2A', 'Cayenne Turbo', 0.90, 'Porsche Cayenne Turbo GT / Turbo S E-Hybrid'),
    # ── Porsche Panamera (WP0Ag — 4-door sedan family) ───────────────────
    ('WP0AG2A', 'Panamera', 0.80, 'Porsche Panamera base/4'),
    ('WP0AH2A', 'Panamera GTS', 0.85, 'Porsche Panamera GTS / Turbo S'),
    # ── Ferrari (ZFF) — model is in char 4-5, trim usually one per VIN ──
    ('ZFF79', '488 GTB', 0.85, 'Ferrari 488 GTB'),
    ('ZFF80', '488 Spider', 0.85, 'Ferrari 488 Spider'),
    ('ZFF82', 'F8 Tributo', 0.90, 'Ferrari F8 Tributo'),
    ('ZFF83', 'F8 Spider', 0.90, 'Ferrari F8 Spider'),
    ('ZFF96', '296 GTB', 0.90, 'Ferrari 296 GTB'),
    ('ZFF97', '296 GTS', 0.90, 'Ferrari 296 GTS'),
    # ── Lamborghini (ZHW) ─────────────────────────────────────────────────
    ('ZHWUC1', 'Huracan', 0.80, 'Lamborghini Huracan'),
    ('ZHWEH4', 'Urus', 0.85, 'Lamborghini Urus'),
    ('ZPBUA1', 'Huracan Evo', 0.85, 'Lamborghini Huracan Evo'),
    # ── McLaren (SBM) ─────────────────────────────────────────────────────
    ('SBM14', '720S', 0.90, 'McLaren 720S'),
    ('SBM13', '600LT', 0.90, 'McLaren 600LT'),
    # ── BMW M-cars — trim IS in VIN, but prefix shifts per chassis gen.
    # Verified 2026-05-11 against current production VINs:
    #   G80 M3 Sedan     → WBS33   (had wrong "WBS83" before — that's M5)
    #   G82 M4 Coupe     → WBS43
    #   G90 M5 Sedan     → WBS83   (NEW for 2025+, was an earlier-gen code)
    #   G87 M2 Coupe     → WBS23
    #   X3 M F97         → 5YMTS
    #   X4 M F98         → 5YMUS
    #   X5 M F95         → 5YMJV
    #   X6 M F96         → 5YMKV
    # Confidence 0.80 (not 0.90) because BMW reuses prefix codes across
    # chassis generations — a 2017 VIN with the same prefix may decode
    # differently. NHTSA is still the primary signal; this dict is a
    # fallback when NHTSA's Trim field is blank.
    ('WBS23', 'M2', 0.80, 'BMW M2 G87 Coupe'),
    ('WBS33', 'M3', 0.80, 'BMW M3 G80 Sedan / Competition / CS'),
    ('WBS43', 'M4', 0.80, 'BMW M4 G82 Coupe / Competition / CSL'),
    ('WBS83', 'M5', 0.80, 'BMW M5 G90 Sedan (2025+)'),
    ('5YMTS', 'X3 M', 0.85, 'BMW X3 M F97 / Competition'),
    ('5YMUS', 'X4 M', 0.85, 'BMW X4 M F98 / Competition'),
    ('5YMJV', 'X5 M', 0.85, 'BMW X5 M F95 / Competition'),
    ('5YMKV', 'X6 M', 0.85, 'BMW X6 M F96 / Competition'),
    # ── Mercedes AMG (W1K / 4JG) — AMG trim level discriminated by 4-7 ──
    ('W1K2J', 'C 63 AMG', 0.85, 'Mercedes-AMG C 63'),
    ('W1KZG', 'E 63 AMG', 0.85, 'Mercedes-AMG E 63'),
    ('W1KAF', 'S 63 AMG', 0.85, 'Mercedes-AMG S 63'),
    ('4JGED', 'GLE 63 AMG', 0.85, 'Mercedes-AMG GLE 63'),
    ('4JGFD', 'G 63 AMG', 0.90, 'Mercedes-AMG G 63'),
    # ── Audi RS / R8 (WUA) ────────────────────────────────────────────────
    ('WUAEF', 'R8', 0.90, 'Audi R8'),
    ('WUAPE', 'RS6 Avant', 0.90, 'Audi RS6 Avant'),
    # ── Ford F-Series body/cab — trim NOT in VIN, but body IS ────────────
    # These give us body/cab info (helps dealer match + display) but trim
    # stays None — needs AccuTrade overseer or iPacket sticker.
    # We RETURN None for trim but DO populate via canon_source='vin_body'.
    # (Skipped here: handled separately below in lookup function.)
]

# Sort descending by prefix length so we match the most specific entry first
_VIN_PREFIX_TABLE.sort(key=lambda x: -len(x[0]))


# Body/cab-only lookups for Ford F-Series and similar — we return body info
# as a NOTE but trim stays None (the AccuTrade overseer / iPacket fills it).
_VIN_BODY_TABLE: dict[str, str] = {
    # Ford Super Duty F-250 / F-350
    '1FD7W': 'F-250 Crew Cab',
    '1FD8W': 'F-250 Super Cab',
    '1FT7W': 'F-150 Super Crew Cab',
    '1FT8W': 'F-150 Super Cab',
    '1FTRF': 'F-150 Regular Cab',
    '1FT8X': 'F-250 (legacy)',
    '1FD8X': 'F-350 Super Cab',
    '1FD7X': 'F-350 Crew Cab',
    # GM heavy duty
    '1GT49': 'Sierra HD Crew Cab',
    '1GT19': 'Sierra HD Reg Cab',
    '2GC4Y': 'Silverado HD Crew Cab',
}


def lookup_trim_from_vin_prefix(vin: str) -> dict:
    """Return canonical trim guess from VIN prefix. Always returns a dict:
        {trim, confidence, source, note, body}

    `source` is 'vin_prefix' when trim is set, 'vin_body' when only body is
    known (Ford F-series style), or None when no entry matches.
    """
    if not vin or len(vin) < 5:
        return {'trim': None, 'confidence': 0, 'source': None,
                'note': None, 'body': None}
    v = vin.upper()

    # Longest-prefix match wins (table is pre-sorted descending by length)
    for pfx, trim, conf, note in _VIN_PREFIX_TABLE:
        if v.startswith(pfx):
            return {'trim': trim, 'confidence': conf,
                    'source': 'vin_prefix', 'note': note, 'body': None}

    # Body-only fallback (Ford F-series etc.)
    pfx5 = v[:5]
    if pfx5 in _VIN_BODY_TABLE:
        return {'trim': None, 'confidence': 0,
                'source': 'vin_body',
                'note': f'VIN prefix encodes body: {_VIN_BODY_TABLE[pfx5]}',
                'body': _VIN_BODY_TABLE[pfx5]}

    return {'trim': None, 'confidence': 0, 'source': None,
            'note': None, 'body': None}
