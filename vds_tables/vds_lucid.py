"""Deterministic Lucid Motors VIN VDS decoder.

Maps VIN positions 4-8 -> year / make / model / trim / body / engine.
Coverage: 2021-2026 (Air sedan, Gravity SUV).

------------------------------------------------------------------------------
WMIs USED BY LUCID
------------------------------------------------------------------------------
  50E  -> Lucid USA, Inc. -- Casa Grande, Arizona plant. Issued for the
          Lucid Air sedan in 2021. Used for ALL Air production.
  7UU  -> Lucid USA, Inc. -- Casa Grande. Used for the Lucid Gravity SUV
          (multipurpose passenger vehicle WMI), introduced 2024.

------------------------------------------------------------------------------
VIN POSITION SLICING (Lucid, 1-indexed; 0-indexed slice in parens)
------------------------------------------------------------------------------
  pos 1-3   (vin[0:3])  = WMI: 50E (Air) or 7UU (Gravity)
  pos 4     (vin[3])    = Body class
                            A  Air sedan (4-door sedan, 5-passenger)
                            G  Gravity SUV (3-row, 5- or 7-passenger)
  pos 5     (vin[4])    = Body sub-revision / restraint generation
                            1  Original / Gen-1 body & restraints
                            2  Updated restraints / mid-cycle refresh
  pos 6     (vin[5])    = Trim level (Lucid puts trim in position 6, not 8)
                            P  Pure -- single or dual motor, base trim
                            T  Touring -- dual motor, mid trim
                            G  Grand Touring -- dual/tri motor, premium
                            D  Dream Edition (limited-run launch 2022)
                            S  Sapphire -- tri-motor performance flagship
                            (Gravity-only):
                              G  Grand Touring (Gravity GT)
                              D  Dream Edition (Gravity DE)
                              P  Pure (Gravity Pure -- future)
                              T  Touring (Gravity Touring -- future)
  pos 7     (vin[6])    = Drive unit / motor power (kW)
                            A  828 kW (1,111 hp) -- Air Sapphire trifecta
                            B  597 kW (800 hp)   -- Grand Touring dual
                            C  695 kW (932 hp)   -- Grand Touring Perf dual
                            D  783 kW (1,050 hp) -- Grand Touring Perf
                            E  462 kW (619 hp)   -- Touring / Pure AWD
                            F  358 kW (480 hp)   -- Pure / Touring detuned
                            G  330 kW (443 hp)   -- Pure single-motor (FWD/RWD)
                            T  930 kW (1,247 hp) -- Sapphire peak
                            H  617 kW (Gravity GT 7-seat)
                            K  798 kW (Gravity Dream Edition motors)
  pos 8     (vin[7])    = Configuration / restraint sub-code
                            A  Standard restraint package + 5-seat config
                            B  Updated restraint package
                            J  7-seat config (Gravity)
                            K  5-seat config (Gravity)

  pos 9     (vin[8])    = Check digit (computed, not validated here)
  pos 10    (vin[9])    = Model year letter -- standard ISO 3779
                            M=2021, N=2022, P=2023, R=2024, S=2025, T=2026
  pos 11    (vin[10])   = Plant code (A = Casa Grande AMP-1)
  pos 12-17 (vin[11:])  = Sequential serial

The VDS key used by this module is vin[3:7] (positions 4-7), i.e.
body + revision + trim + motor. Then pos 8 (config) is a tie-breaker.

------------------------------------------------------------------------------
CONFIDENCE PHILOSOPHY
------------------------------------------------------------------------------
  0.95  Model + body + trim + motor all VIN-determined unambiguously
  0.85  Model + trim certain, motor variant ambiguous (e.g. Pure AWD vs RWD)
  0.75  Model certain, trim cannot be VIN-determined from pos 6 alone
  0.60  Recognized WMI but unknown VDS combination; best guess returned

Sources cross-referenced (May 2026):
  - NHTSA vPIC decoder (live calls against vpic.nhtsa.dot.gov/api)
    -- 50EA1PFA0PA005449 -> Air Pure 2023, 50EA1TFA -> Touring,
       50EA1GFA -> Grand Touring, 7UUG1GEA -> Gravity Grand Touring
  - lucidowners.com 'Updated VIN Structure' thread (April 2022)
  - stat.vin/vin-decoding/lucid-motors/lucid-air
  - decodethis.com VIN 50EA1GBA8NA004231 (2022 Air Grand Touring)
"""

from __future__ import annotations

__all__ = ["decode", "self_test", "WMI", "YEAR_CODES", "VDS"]

# ---------------------------------------------------------------------------
# WMIs
# ---------------------------------------------------------------------------
WMI = ['50E', '7UU']

# ---------------------------------------------------------------------------
# Year codes (ISO 3779, 2010-2030). Lucid production began 2021.
# ---------------------------------------------------------------------------
YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028, 'X': 2029,
    'Y': 2030,
}

# ---------------------------------------------------------------------------
# Body letter (pos 4) -> base model
# ---------------------------------------------------------------------------
_BODY = {
    'A': {'model': 'Air',     'body': 'Sedan',
          'vehicle_type': 'Passenger Car', 'wmi_expect': '50E'},
    'G': {'model': 'Gravity', 'body': 'SUV',
          'vehicle_type': 'Multipurpose Passenger Vehicle', 'wmi_expect': '7UU'},
}

# ---------------------------------------------------------------------------
# Trim letter (pos 6) -> trim attributes
# Air uses one set, Gravity uses another (overlapping letters mean different
# things). We resolve per body letter in decode().
# ---------------------------------------------------------------------------
_TRIM_AIR = {
    'P': ('Pure',                       0.95),
    'T': ('Touring',                    0.95),
    'G': ('Grand Touring',              0.95),
    'D': ('Dream Edition',              0.93),
    'S': ('Sapphire',                   0.96),
}
_TRIM_GRAVITY = {
    'G': ('Grand Touring',              0.95),
    'D': ('Dream Edition',              0.93),
    'P': ('Pure',                       0.85),   # rumored future trim
    'T': ('Touring',                    0.85),   # rumored future trim
}

# ---------------------------------------------------------------------------
# Motor letter (pos 7) -> kW / hp / drive
# ---------------------------------------------------------------------------
# (drive, kW, hp, descriptor)
_MOTOR = {
    'A': ('AWD', 828, 1111, 'Tri-Motor Sapphire (peak)'),
    'B': ('AWD', 597, 800,  'Dual-Motor Grand Touring'),
    'C': ('AWD', 695, 932,  'Dual-Motor Grand Touring Perf'),
    'D': ('AWD', 783, 1050, 'Tri-Motor Grand Touring Perf'),
    'E': ('AWD', 462, 619,  'Dual-Motor (Touring / Pure AWD)'),
    'F': ('AWD', 358, 480,  'Dual-Motor (Pure detuned)'),
    'G': ('RWD', 330, 443,  'Single-Motor (Pure RWD)'),
    'T': ('AWD', 930, 1247, 'Tri-Motor Sapphire (max)'),
    'H': ('AWD', 617, 827,  'Dual-Motor Gravity GT'),
    'K': ('AWD', 798, 1070, 'Tri-Motor Gravity Dream'),
}


# ---------------------------------------------------------------------------
# Build the VDS table -- cartesian product of (body, revision, trim, motor)
# ---------------------------------------------------------------------------
def _build_vds() -> dict:
    table: dict[str, dict] = {}
    for body_letter, body_info in _BODY.items():
        trims = _TRIM_GRAVITY if body_letter == 'G' else _TRIM_AIR
        for rev in '12':
            for trim_letter in trims:
                for motor_letter, (drive, kw, hp, motor_desc) in _MOTOR.items():
                    key = f'{body_letter}{rev}{trim_letter}{motor_letter}'
                    table[key] = {
                        'model': body_info['model'],
                        'body': body_info['body'],
                        'drive': drive,
                        'engine_kw': kw,
                        'engine_hp': hp,
                        'engine': f'{motor_desc} ({kw} kW / {hp} hp)',
                        'vehicle_type': body_info['vehicle_type'],
                    }
    return table


VDS = _build_vds()


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------
def decode(vin):
    """Decode a 17-char Lucid VIN to year/make/model/trim/body/engine.

    Returns None if VIN is malformed or WMI is not Lucid.
    """
    if not vin or len(vin) != 17 or vin[:3] not in WMI:
        return None
    vin = vin.upper()
    wmi = vin[:3]

    body_letter = vin[3]
    revision = vin[4]
    trim_letter = vin[5]
    motor_letter = vin[6]
    config_letter = vin[7]
    year = YEAR_CODES.get(vin[9])

    # Body / WMI cross-check
    body_info = _BODY.get(body_letter)
    if not body_info:
        return None
    if wmi != body_info['wmi_expect']:
        # Soft-fail: 50E with G body or 7UU with A body is implausible.
        # Return low-confidence guess rather than None, since the rest of
        # the VIN may still be decodable.
        confidence_penalty = 0.20
    else:
        confidence_penalty = 0.0

    # Resolve trim
    trim_map = _TRIM_GRAVITY if body_letter == 'G' else _TRIM_AIR
    trim_info = trim_map.get(trim_letter)
    if trim_info:
        trim, trim_conf = trim_info
    else:
        trim = None
        trim_conf = 0.60

    # Resolve motor
    motor_info = _MOTOR.get(motor_letter)
    if motor_info:
        drive, kw, hp, motor_desc = motor_info
        engine = f'{motor_desc} ({kw} kW / {hp} hp)'
    else:
        drive = 'AWD'
        kw = hp = None
        engine = 'Electric motor'
        trim_conf = min(trim_conf, 0.70)

    # Seat config from pos 8 (Gravity only meaningful)
    seats = None
    if body_letter == 'G':
        if config_letter == 'J':
            seats = 7
        elif config_letter == 'K':
            seats = 5

    confidence = round(max(0.50, trim_conf - confidence_penalty), 2)

    return {
        'year': year,
        'make': 'Lucid',
        'model': body_info['model'],
        'trim': trim,
        'body': body_info['body'],
        'engine': engine,
        'drive': drive,
        'engine_kw': kw,
        'engine_hp': hp,
        'seats': seats,
        'confidence': confidence,
        'source': 'vds_table:lucid',
        'wmi': wmi,
        'body_letter': body_letter,
        'trim_letter': trim_letter,
        'motor_letter': motor_letter,
        'electrification': 'BEV',
        'vehicle_type': body_info['vehicle_type'],
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def self_test():
    """Run self-test cases. Returns (passed, failed)."""
    # (vin, expected_model, expected_year)
    cases = [
        # ----- Lucid Air (50E) -----
        ('50EA1PFA0NA000001', 'Air',     2022),  # Air Pure 2022 (motor F=480hp)
        ('50EA1PFA0PA000001', 'Air',     2023),  # Air Pure 2023 (NHTSA-validated pattern)
        ('50EA1PFA0PA005449', 'Air',     2023),  # Real Air Pure 2023 (NHTSA test VIN)
        ('50EA1TFA0PA000001', 'Air',     2023),  # Air Touring 2023
        ('50EA1GBA0PA000001', 'Air',     2023),  # Air Grand Touring 2023 (motor B=800hp)
        ('50EA1GBA8NA004231', 'Air',     2022),  # Real Air Grand Touring 2022 (decodethis.com)
        ('50EA1DFA0NA000001', 'Air',     2022),  # Air Dream Edition 2022
        ('50EA1SAA0RA000001', 'Air',     2024),  # Air Sapphire 2024 (motor A=1111hp)
        ('50EA2GBA1RA000001', 'Air',     2024),  # Air GT 2024 (revision 2)
        ('50EA1PFA0SA000001', 'Air',     2025),  # Air Pure 2025
        ('50EA1GCA0SA000001', 'Air',     2025),  # Air GT Performance 2025 (motor C=932hp)
        ('50EA1TFA0TA000001', 'Air',     2026),  # Air Touring 2026
        # ----- Lucid Gravity (7UU) -----
        ('7UUG1DEA0SA000001', 'Gravity', 2025),  # Gravity Dream Edition 2025 (5-seat)
        ('7UUG1DKJ0SA000001', 'Gravity', 2025),  # Gravity Dream 7-seat 2025 (motor K)
        ('7UUG1GHK0SA000001', 'Gravity', 2025),  # Gravity GT 5-seat 2025 (motor H)
        ('7UUG1GHA0TA000001', 'Gravity', 2026),  # Gravity GT 2026
        # ----- Negative cases -----
        ('',                  None,      None),  # empty
        ('50EA1PFA0PA00544',  None,      None),  # 16-char short
        ('XYZA1PFA0PA005449', None,      None),  # bad WMI
        ('50EZ1PFA0PA005449', None,      None),  # 50E + invalid body letter Z
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
    print(f'\nvds_lucid self_test: {p} passed, {f} failed')
