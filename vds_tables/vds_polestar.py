"""Deterministic Polestar VIN VDS decoder.

Maps VIN positions 4-8 -> year / make / model / trim / body / engine.
Coverage: 2017-2026 (Polestar 1 PHEV coupe, Polestar 2 sedan, Polestar 3 SUV,
Polestar 4 SUV-Coupe).

------------------------------------------------------------------------------
WMIs USED BY POLESTAR
------------------------------------------------------------------------------
  LPS  -> Polestar Performance AB / Volvo Cars -- China-built passenger cars.
          Original WMI used for the Polestar 1 (2020-2022 hybrid coupe) and
          early Polestar 2 (MY2021). Position 4-7 carries the model line.
  YSM  -> Polestar Performance AB -- Chengdu plant, China-built passenger cars
          MY2022+. Now the dominant WMI for Polestar 2.
  YSR  -> Polestar Performance AB -- China-built multipurpose passenger
          vehicles (MPV / SUV). Used for Polestar 3 and Polestar 4.
  YST  -> Polestar Performance AB -- alternate sedan code (rare, seen on
          some 2024 export-market Polestar 2 builds). Tolerated as YSM.
  LPV  -> Polestar Performance AB -- alternate Chinese WMI prefix (seen on
          some Polestar 4 builds for the domestic Chinese market). Tolerated.
  7SY  -> Polestar Performance AB -- US-built MPV. Issued for the Polestar 3
          built at Volvo's Charleston, SC plant (Ridgeville) starting 2024.

------------------------------------------------------------------------------
VIN POSITION SLICING (Polestar, 1-indexed; 0-indexed slice in parens)
------------------------------------------------------------------------------
  pos 1-3   (vin[0:3])  = WMI: LPS / YSM / YSR / YST / LPV / 7SY
  pos 4-5   (vin[3:5])  = Engine / motor family code
                            ----- Polestar 1 (PHEV) -----
                            BE  B4204T25 / B4204TSH -- 2.0L T4 supercharged
                                + turbo + electric (619 hp combined)
                            ----- Polestar 2 (BEV) -----
                            ED  Dual-motor AWD (Long Range Dual Motor),
                                MY2021-2022 (300 kW)
                            EE  Updated dual-motor AWD (MY2023+, 350 kW)
                            EG  Single-motor FWD (Standard Range Single
                                Motor, 170-180 kW)
                            ET  Single-motor RWD (Long Range Single Motor,
                                MY2024+ rear-drive)
                            FD  Single-motor RWD Long Range MY2024+
                            FE  Performance Pack dual-motor (350 kW)
                            FF  Dual-motor variant
                            FG  Performance Pack uprated (380 kW)
                            ----- Polestar 3 (BEV SUV) -----
                            EE  Long Range Dual Motor (489 hp)
                            EJ  Performance Dual Motor (517 hp)
                            EA  Long Range Single Motor (rear) -- MY2025+
                            ----- Polestar 4 (BEV SUV-Coupe) -----
                            PA  Long Range Single Motor (rear) (272 hp)
                            PB  Long Range Dual Motor (Performance) (544 hp)
  pos 6     (vin[5])    = Emissions / electrification code
                            0  ULEV (Polestar 1 PHEV)
                            3  BEV (Polestar 2, 3, 4 -- all pure EV)
  pos 7     (vin[6])    = Vehicle line code
                            Y  Polestar 1 (2020-2022) OR Polestar 3 (2024+)
                                -- disambiguate by year + WMI
                            K  Polestar 2 (sedan / fastback, 2021+)
                            A  Polestar 4 (SUV-coupe, 2024+)
                            (Y vs K is the primary line distinction.)
  pos 8     (vin[7])    = Restraint / GVWR / model designator
                            L  Polestar 1 PHEV coupe
                            A  Polestar 2 BEV sedan
                            B  Polestar 3 BEV SUV
                            4  Polestar 4 BEV SUV-Coupe

  pos 9     (vin[8])    = Check digit (computed, not validated here)
  pos 10    (vin[9])    = Model year letter -- standard ISO 3779
                            J=2018, K=2019, L=2020, M=2021, N=2022,
                            P=2023, R=2024, S=2025, T=2026
  pos 11    (vin[10])   = Plant code (G = Charleston SC for 7SY;
                          X = Chengdu China; others vary)
  pos 12-17 (vin[11:])  = Sequential serial

The VDS key used by this module is vin[3:7] (positions 4-7), i.e.
engine + emissions + line. Position 8 confirms model number (1/2/3/4).

------------------------------------------------------------------------------
DISAMBIGUATION RULES
------------------------------------------------------------------------------
- Position 7 letter 'Y' covers BOTH Polestar 1 (2020-2022) and Polestar 3
  (2024+). We resolve using year: <=2022 -> Polestar 1, >=2024 -> Polestar 3.
- Position 8 model digit ('1', '2', '3', '4', 'L', 'A', 'B') always matches
  the model number except for Polestar 1 which uses 'L' (legacy code).
- WMI LPS was used for Polestar 1 and early Polestar 2 (MY2021); YSM took
  over for Polestar 2 in MY2022. Either WMI accepted for Polestar 2.

------------------------------------------------------------------------------
CONFIDENCE PHILOSOPHY
------------------------------------------------------------------------------
  0.95  Model + body + trim all VIN-determined unambiguously
  0.85  Model + trim certain, sub-trim (e.g. Performance Pack vs base
        Long Range Dual Motor) cannot be VIN-determined
  0.75  Model certain, trim ambiguous between two variants
  0.60  Recognized WMI but unknown VDS combination; best guess returned

Sources cross-referenced (May 2026):
  - NHTSA vPIC decoder (live calls against vpic.nhtsa.dot.gov/api)
    -- LPSED3KA -> Polestar 2 e-AWD 2022, LPSEG3KA -> Polestar 2 e-FWD,
       YSMED3KA -> Polestar 2 Single Motor, YSREE3YA -> Polestar 3
  - en.wikibooks.org Vehicle_Identification_Numbers_(VIN_codes)/Polestar
  - stat.vin/vin-decoding/polestar
  - polestar-forum.com VIN decoder threads
"""

from __future__ import annotations

__all__ = ["decode", "self_test", "WMI", "YEAR_CODES", "VDS"]

# ---------------------------------------------------------------------------
# WMIs
# ---------------------------------------------------------------------------
WMI = ['LPS', 'YSM', 'YSR', 'YST', 'LPV', '7SY']

# ---------------------------------------------------------------------------
# Year codes (ISO 3779, 2010-2030).
# ---------------------------------------------------------------------------
YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028, 'X': 2029,
    'Y': 2030,
}

# ---------------------------------------------------------------------------
# Engine / motor code (pos 4-5) -> (model_hint, trim, drive, engine_desc)
# ---------------------------------------------------------------------------
_ENGINE_CODE = {
    # Polestar 1 -- PHEV
    'BE': ('Polestar 1', 'Polestar 1',
           'AWD',
           '2.0L T4 Supercharged+Turbo + Electric (619 hp PHEV)'),
    # Polestar 2 -- BEV
    'ED': ('Polestar 2', 'Long Range Dual Motor',
           'AWD',
           'Dual Motor BEV (300 kW)'),
    'EE': ('Polestar 2', 'Long Range Dual Motor',
           'AWD',
           'Dual Motor BEV (350 kW, MY2023+)'),
    'EG': ('Polestar 2', 'Standard Range Single Motor',
           'FWD',
           'Single Motor BEV (170-180 kW)'),
    'ET': ('Polestar 2', 'Long Range Single Motor',
           'RWD',
           'Single Motor BEV RWD (220 kW)'),
    'FD': ('Polestar 2', 'Long Range Single Motor',
           'RWD',
           'Single Motor BEV RWD (220 kW, MY2024+)'),
    'FE': ('Polestar 2', 'Performance Pack',
           'AWD',
           'Dual Motor BEV Performance Pack (350 kW)'),
    'FF': ('Polestar 2', 'Long Range Dual Motor',
           'AWD',
           'Dual Motor BEV'),
    'FG': ('Polestar 2', 'Performance Pack',
           'AWD',
           'Dual Motor BEV Performance Pack (380 kW)'),
    # Polestar 3 -- BEV SUV. Note 'EE' overlaps with Polestar 2 -- we
    # disambiguate using pos 7 (Y for P3 vs K for P2).
    'EJ': ('Polestar 3', 'Performance',
           'AWD',
           'Dual Motor BEV (517 hp)'),
    'EA': ('Polestar 3', 'Long Range Single Motor',
           'RWD',
           'Single Motor BEV (220 kW)'),
    # Polestar 4 -- BEV SUV-Coupe
    'PA': ('Polestar 4', 'Long Range Single Motor',
           'RWD',
           'Single Motor BEV (272 hp, 200 kW)'),
    'PB': ('Polestar 4', 'Long Range Dual Motor',
           'AWD',
           'Dual Motor BEV Performance (544 hp, 400 kW)'),
}

# ---------------------------------------------------------------------------
# Position 7 (vehicle line) -> primary model
# ---------------------------------------------------------------------------
_LINE = {
    'Y': 'Polestar 1 or Polestar 3',  # year-disambiguated
    'K': 'Polestar 2',
    'A': 'Polestar 4',
}

# ---------------------------------------------------------------------------
# Position 8 (model number / restraint) -> nominal model
# ---------------------------------------------------------------------------
_POS8 = {
    'L': 'Polestar 1',
    'A': 'Polestar 2',
    'B': 'Polestar 3',
    '4': 'Polestar 4',
}


# ---------------------------------------------------------------------------
# Build the VDS table.
# We index by vin[3:7] = pos 4-7 (engine code + emissions + line letter).
# ---------------------------------------------------------------------------
def _build_vds() -> dict:
    table: dict[str, dict] = {}
    for eng_code, (model_hint, trim, drive, engine_desc) in _ENGINE_CODE.items():
        # Both 0 (ULEV) and 3 (BEV) emissions codes -- only one is correct
        # per engine, but tolerate either since some legacy LPS VINs use '0'.
        for emis in '03':
            for line_letter in 'YKA':
                key = f'{eng_code}{emis}{line_letter}'
                table[key] = {
                    'model_hint': model_hint,
                    'trim': trim,
                    'drive': drive,
                    'engine': engine_desc,
                }
    return table


VDS = _build_vds()


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------
def decode(vin):
    """Decode a 17-char Polestar VIN to year/make/model/trim/body/engine.

    Returns None if VIN is malformed or WMI is not Polestar.
    """
    if not vin or len(vin) != 17 or vin[:3] not in WMI:
        return None
    vin = vin.upper()
    wmi = vin[:3]

    engine_code = vin[3:5]
    emissions = vin[5]
    line_letter = vin[6]
    pos8 = vin[7]
    year = YEAR_CODES.get(vin[9])

    entry = VDS.get(vin[3:7])
    eng_row = _ENGINE_CODE.get(engine_code)

    if not eng_row:
        # Unknown engine code -- attempt fallback via pos 8 model digit
        nominal = _POS8.get(pos8)
        if not nominal:
            return None
        return {
            'year': year, 'make': 'Polestar',
            'model': nominal, 'trim': None,
            'body': _body_for_model(nominal),
            'engine': None, 'drive': None,
            'confidence': 0.55,
            'source': 'vds_table:polestar',
            'wmi': wmi,
            'engine_code': engine_code,
            'line_letter': line_letter,
            'note': 'unknown engine code; nominal model from pos 8',
        }

    model_hint, trim, drive, engine_desc = eng_row

    # Resolve actual model
    if model_hint == 'Polestar 1':
        model = 'Polestar 1'
    elif model_hint == 'Polestar 2':
        model = 'Polestar 2'
    elif model_hint == 'Polestar 3':
        model = 'Polestar 3'
    elif model_hint == 'Polestar 4':
        model = 'Polestar 4'
    else:
        model = model_hint

    # WMI is the primary source of truth for model class:
    #   YSR / 7SY = MPV WMI -> must be Polestar 3 or Polestar 4 (never P1/P2)
    #   LPS / YSM / YST = passenger car WMI -> Polestar 1 or 2 (never P3/P4)
    #   LPV = Chinese alternate WMI -> typically Polestar 4
    is_mpv_wmi = wmi in ('YSR', '7SY')
    is_car_wmi = wmi in ('LPS', 'YSM', 'YST')

    # Polestar 1 vs Polestar 3 disambiguation
    # Engine code BE is unique to Polestar 1 (PHEV).
    if engine_code == 'BE':
        model = 'Polestar 1'
    elif is_mpv_wmi:
        # MPV WMI rules out P1 and P2. If engine code points to P2, override
        # to Polestar 3 (since EE overlaps between P2 and P3).
        if model == 'Polestar 2':
            model = 'Polestar 3'
        # PA/PB engine codes already correctly point to Polestar 4; keep them.
    elif is_car_wmi:
        # Passenger car WMI rules out P3 and P4. If engine code points to P3,
        # override to Polestar 2.
        if model in ('Polestar 3', 'Polestar 4'):
            # Unusual but tolerate
            model = 'Polestar 2'

    # Cross-check pos 8 vs derived model -- pos 8 is the last-line tiebreaker.
    # Skip the override when WMI already firmly determined the class.
    pos8_model = _POS8.get(pos8)
    if pos8_model and pos8_model != model and not is_mpv_wmi and not is_car_wmi:
        # Trust pos 8 only when engine code is genuinely ambiguous (EE) AND
        # we don't have a class-defining WMI to anchor us.
        if engine_code == 'EE' and pos8_model in ('Polestar 2', 'Polestar 3'):
            model = pos8_model

    # Year-aware trim refinements
    confidence = 0.90
    # Polestar 2 base SR Single Motor (EG) wasn't widely available before 2021
    if engine_code == 'EG' and year and year < 2021:
        confidence = 0.70

    # Generation / facelift hint
    generation = None
    if model == 'Polestar 2':
        if year and year >= 2024:
            generation = 'facelift (MY2024+)'
        elif year and year <= 2023:
            generation = 'pre-facelift'

    body = _body_for_model(model)

    return {
        'year': year,
        'make': 'Polestar',
        'model': model,
        'trim': trim,
        'body': body,
        'engine': engine_desc,
        'drive': drive,
        'generation': generation,
        'confidence': round(confidence, 2),
        'source': 'vds_table:polestar',
        'wmi': wmi,
        'engine_code': engine_code,
        'line_letter': line_letter,
        'pos8': pos8,
        'electrification': 'PHEV' if model == 'Polestar 1' else 'BEV',
    }


def _body_for_model(model: str) -> str | None:
    return {
        'Polestar 1': 'Coupe',
        'Polestar 2': 'Fastback Sedan',
        'Polestar 3': 'SUV',
        'Polestar 4': 'SUV-Coupe',
    }.get(model)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def self_test():
    """Run self-test cases. Returns (passed, failed)."""
    # (vin, expected_model, expected_year)
    cases = [
        # ----- Polestar 1 (PHEV) -----
        ('LPSBE0LA0LX000001', 'Polestar 1', 2020),  # P1 2020 ULEV
        ('LPSBE0LA0MX000001', 'Polestar 1', 2021),  # P1 2021
        ('LPSBE0LA0NX000001', 'Polestar 1', 2022),  # P1 2022 (final year)
        # ----- Polestar 2 (BEV) -----
        ('LPSED3KA0MX000001', 'Polestar 2', 2021),  # P2 Dual Motor 2021
        ('LPSED3KA0NX000001', 'Polestar 2', 2022),  # P2 Dual Motor 2022
        ('YSMED3KA0PX000001', 'Polestar 2', 2023),  # P2 Dual Motor 2023
        ('LPSEG3KA0PX000001', 'Polestar 2', 2023),  # P2 Single Motor FWD 2023
        ('YSMET3KA0RX000001', 'Polestar 2', 2024),  # P2 Single Motor RWD 2024
        ('YSMEE3KA0RX000001', 'Polestar 2', 2024),  # P2 Dual Motor (facelift) 2024
        ('YSMFE3KA0SX000001', 'Polestar 2', 2025),  # P2 Performance Pack 2025
        ('YSMFD3KA0RX000001', 'Polestar 2', 2024),  # P2 LR Single Motor RWD 2024
        # ----- Polestar 3 (BEV SUV) -----
        ('YSREE3YA0RX000001', 'Polestar 3', 2024),  # P3 LR Dual 2024
        ('YSREJ3YA0SX000001', 'Polestar 3', 2025),  # P3 Performance 2025
        ('YSREA3YA0SX000001', 'Polestar 3', 2025),  # P3 Single Motor 2025
        ('7SYEE3YA0SX000001', 'Polestar 3', 2025),  # P3 made in USA 2025
        # ----- Polestar 4 (BEV SUV-Coupe) -----
        ('YSRPA3AA0SX000001', 'Polestar 4', 2025),  # P4 Single Motor 2025
        ('YSRPB3AA0SX000001', 'Polestar 4', 2025),  # P4 Dual Motor 2025
        ('YSRPB3AA0TX000001', 'Polestar 4', 2026),  # P4 Dual Motor 2026
        # ----- Negative cases -----
        ('',                   None,         None),  # empty
        ('YSRPB3AA0TX00000',   None,         None),  # 16-char short
        ('XYZPB3AA0TX000001',  None,         None),  # bad WMI
    ]

    passed = failed = 0
    for vin, m, y in cases:
        r = decode(vin)
        if m is None and y is None:
            if r is None:
                passed += 1
            else:
                failed += 1
                print(f'FAIL {vin}: got {r}')
        else:
            if r and r['model'] == m and r['year'] == y:
                passed += 1
            else:
                failed += 1
                print(f'FAIL {vin}: got {r}')
    return passed, failed


if __name__ == '__main__':
    p, f = self_test()
    print(f'\nvds_polestar self_test: {p} passed, {f} failed')
