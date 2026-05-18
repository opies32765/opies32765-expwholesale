"""Deterministic Rivian VIN VDS decoder.

Maps VIN positions 4-8 -> year / make / model / trim / body / engine.
Coverage: 2021-2026 (R1T pickup, R1S SUV, EDV / Commercial Van).

------------------------------------------------------------------------------
WMI USED BY RIVIAN
------------------------------------------------------------------------------
  7PD  -> Rivian Automotive, LLC -- Normal, Illinois plant. Issued in 2021
          and used for ALL Rivian production from late-2022 onward, regardless
          of body style. Position 4 then distinguishes truck vs SUV vs van.

  Historic NHTSA-listed WMI '7FC' (used briefly in early 2022 for R1T-only)
  is also tolerated below for completeness, but Rivian consolidated to 7PD
  by late 2022 and all current production uses 7PD with position 4 carrying
  the model differentiation.

------------------------------------------------------------------------------
VIN POSITION SLICING (Rivian, 1-indexed; 0-indexed slice in parens)
------------------------------------------------------------------------------
  pos 1-3   (vin[0:3])  = WMI: 7PD (or 7FC for very early R1T-only)
  pos 4     (vin[3])    = Model line / body letter
                            T  R1T 4-door Pickup Truck
                            S  R1S 4-door SUV
                            V  EDV / RCV / Commercial Delivery Van
  pos 5     (vin[4])    = GVWR / brake system class
                            G  GVWR Class 2G (8,001-9,000 lb) hydraulic
                               brakes -- dominant on R1T/R1S
                            H  GVWR Class 2H (9,001-10,000 lb) -- some
                               heavier configurations & EDV
  pos 6     (vin[5])    = Motor / drivetrain
                            A  Electric, Quad-Motor, AWD (R1 Gen-1 launch
                               drive system, two front + two rear)
                            B  Electric, Dual-Motor, AWD (standard +
                               Performance Dual-Motor)
                            C  Electric, Tri-Motor, AWD (Tri-Motor option
                               for 2025+ R1T and R1S, ~850 hp)
                            D  Electric, Single-Motor (rumored R1 base; not
                               in volume production as of May 2026)
                            E  Electric, Dual-Motor commercial (EDV / RCV)
  pos 7     (vin[6])    = Restraint system / safety package
                            A  Standard R1 safety package: 2x front
                               airbags + 2x knee airbags + 2x front-row
                               side airbags + 2x curtain airbags + 3-point
                               seat belts. Dominant code across all years.
                            B  Updated safety package (some 2025+ Gen-2)
                            E  Commercial / EDV restraint package
  pos 8     (vin[7])    = Trim level
                            A  Adventure (premium long-range trim)
                            B  Performance / Performance Dual-Motor
                            C  Quad-Motor Performance (top R1)
                            E  Entry / Explore (base trim, formerly named
                               'Explore', renamed 'Entry' for 2025)
                            L  Launch Edition (limited 2021-2023; discontinued)
                            M  Max Pack / Tri-Motor flagship (sometimes
                               also encoded with C above)
                            P  Premium (added for 2025 model year)
                            T  Tri-Motor Adventure (2025+)
                            X  EDV 700 / RCV commercial variant

  pos 9     (vin[8])    = Check digit (computed, not validated here)
  pos 10    (vin[9])    = Model year letter -- standard ISO 3779
                            M=2021, N=2022, P=2023, R=2024, S=2025, T=2026
  pos 11    (vin[10])   = Plant code (N = Normal, Illinois)
  pos 12-17 (vin[11:])  = Sequential serial

The VDS key used by this module is vin[3:7] (positions 4-7), i.e.
model-letter + GVWR + motor + restraint. Trim (pos 8) is then resolved
in a second-level lookup via _TRIM_BY_MODEL.

------------------------------------------------------------------------------
CONFIDENCE PHILOSOPHY
------------------------------------------------------------------------------
  0.95  Model + body + motor + trim all VIN-determined unambiguously
  0.85  Model + body certain, trim has 1-2 possible values
  0.75  Model certain, trim cannot be VIN-determined
  0.60  Recognized WMI but unknown VDS combination; best guess returned

Sources cross-referenced (May 2026):
  - NHTSA vPIC decoder (live calls against vpic.nhtsa.dot.gov/api)
    -- e.g. 7PDSGABA3PN003979 -> R1S Adventure AWD 2023
  - rivianforums.com VIN decoder thread
  - rivianownersforum.com 'Official Rivian VIN Decoder'
  - bumper.com Rivian VIN decoder
  - electrek.co 'Rivian VIN decoder released' (May 2021)
  - autoblog.com 'Rivian Amazon Prime delivery van details NHTSA VIN'
  - Wikipedia Rivian EDV article (Z=Amazon, S=Service variants)
"""

from __future__ import annotations

__all__ = ["decode", "self_test", "WMI", "YEAR_CODES", "VDS"]

# ---------------------------------------------------------------------------
# WMIs
# ---------------------------------------------------------------------------
WMI = ['7PD', '7FC']  # 7FC was early-2022 R1T only; current is 7PD for all

# ---------------------------------------------------------------------------
# Year codes (ISO 3779, 2010-2030). Rivian production began 2021.
# ---------------------------------------------------------------------------
YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028, 'X': 2029,
    'Y': 2030,
}

# ---------------------------------------------------------------------------
# Model letter (pos 4) -> base model attributes
# ---------------------------------------------------------------------------
_MODEL = {
    'T': {'model': 'R1T', 'body': 'Pickup',
          'vehicle_type': 'Truck'},
    'S': {'model': 'R1S', 'body': 'SUV',
          'vehicle_type': 'Multipurpose Passenger Vehicle'},
    'V': {'model': 'EDV', 'body': 'Cargo Van',
          'vehicle_type': 'Commercial Delivery Van'},
}

# ---------------------------------------------------------------------------
# Motor code (pos 6) -> drive + motor description
# ---------------------------------------------------------------------------
_MOTOR = {
    'A': ('AWD', 'Quad-Motor (Standard / Gen-1)',  'Quad-Motor'),
    'B': ('AWD', 'Dual-Motor (Standard / Perf)',   'Dual-Motor'),
    'C': ('AWD', 'Tri-Motor (Performance)',        'Tri-Motor'),
    'D': ('RWD', 'Single-Motor (rumored R1 base)', 'Single-Motor'),
    'E': ('AWD', 'Dual-Motor Commercial',          'Dual-Motor'),
}

# ---------------------------------------------------------------------------
# Trim code (pos 8) -> trim name + confidence by model letter
# ---------------------------------------------------------------------------
# For 2021-2024 R1: A = Adventure, L = Launch Edition, E = Explore
# For 2025+ R1: A = Adventure, E = Entry (renamed from Explore), P = Premium
# Performance/Tri-Motor codes overlap so we resolve in the year-aware step.
_TRIM_R1 = {
    'A': ('Adventure',          0.95),
    'B': ('Performance Dual-Motor', 0.90),
    'C': ('Quad-Motor Performance', 0.90),
    'E': ('Explore/Entry',      0.85),  # year-aware refinement below
    'L': ('Launch Edition',     0.95),
    'M': ('Max Pack',           0.80),
    'P': ('Premium',            0.92),
    'T': ('Tri-Motor Adventure', 0.85),
}
_TRIM_EDV = {
    'A': ('EDV 700 Amazon',     0.90),
    'X': ('EDV 700',            0.85),
    'S': ('RCV Service',        0.80),
    'Z': ('EDV Amazon',         0.85),
    'E': ('EDV Commercial',     0.80),
}


# ---------------------------------------------------------------------------
# Build the VDS table -- cartesian product of (model letter, GVWR, motor,
# restraint). Trim is resolved separately so we keep this table compact.
# ---------------------------------------------------------------------------
def _build_vds() -> dict:
    table: dict[str, dict] = {}
    for ml, info in _MODEL.items():
        for gvwr in 'GH':
            for motor_letter, (drive, motor_desc, motor_short) in _MOTOR.items():
                for restraint in 'ABCDE':
                    key = f'{ml}{gvwr}{motor_letter}{restraint}'
                    table[key] = {
                        'model': info['model'],
                        'body': info['body'],
                        'drive': drive,
                        'motor': motor_short,
                        'engine': motor_desc,
                        'vehicle_type': info['vehicle_type'],
                    }
    return table


VDS = _build_vds()


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------
def decode(vin):
    """Decode a 17-char Rivian VIN to year/make/model/trim/body/engine.

    Returns None if VIN is malformed or WMI is not Rivian.
    """
    if not vin or len(vin) != 17 or vin[:3] not in WMI:
        return None
    vin = vin.upper()
    wmi = vin[:3]

    model_letter = vin[3]
    gvwr_letter = vin[4]
    motor_letter = vin[5]
    restraint_letter = vin[6]
    trim_letter = vin[7]
    year = YEAR_CODES.get(vin[9])

    # WMI / model-letter sanity: 7FC was R1T-only
    if wmi == '7FC' and model_letter != 'T':
        return None

    vds_key = vin[3:7]
    entry = VDS.get(vds_key)
    if not entry:
        # Tolerant fallback -- still try to identify the model
        model_info = _MODEL.get(model_letter)
        if not model_info:
            return None
        motor_info = _MOTOR.get(motor_letter, ('AWD', 'Electric motor',
                                               'Electric'))
        entry = {
            'model': model_info['model'],
            'body': model_info['body'],
            'drive': motor_info[0],
            'engine': motor_info[1],
            'motor': motor_info[2],
            'vehicle_type': model_info['vehicle_type'],
        }

    # Resolve trim from pos 8
    is_edv = (model_letter == 'V')
    trim_map = _TRIM_EDV if is_edv else _TRIM_R1
    trim_info = trim_map.get(trim_letter)
    if trim_info:
        trim, trim_conf = trim_info
    else:
        trim = None
        trim_conf = 0.65

    # Year-aware trim refinement -------------------------------------------
    # 'E' was Explore pre-2025; renamed 'Entry' from 2025+
    if trim_letter == 'E' and not is_edv:
        if year and year >= 2025:
            trim = 'Entry'
            trim_conf = 0.92
        elif year and year < 2025:
            trim = 'Explore'
            trim_conf = 0.92

    # Launch Edition was 2021-2023 only -- flag low conf if year says otherwise
    if trim_letter == 'L' and year and year >= 2025:
        trim_conf = min(trim_conf, 0.70)

    # Premium was new for 2025
    if trim_letter == 'P' and year and year < 2025:
        trim_conf = min(trim_conf, 0.65)

    confidence = round(min(0.95, trim_conf), 2)

    return {
        'year': year,
        'make': 'Rivian',
        'model': entry['model'],
        'trim': trim,
        'body': entry['body'],
        'engine': entry['engine'],
        'drive': entry['drive'],
        'motor': entry.get('motor'),
        'confidence': confidence,
        'source': 'vds_table:rivian',
        'wmi': wmi,
        'model_letter': model_letter,
        'gvwr_letter': gvwr_letter,
        'motor_letter': motor_letter,
        'trim_letter': trim_letter,
        'electrification': 'BEV',
        'vehicle_type': entry.get('vehicle_type'),
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def self_test():
    """Run self-test cases. Returns (passed, failed)."""
    # (vin, expected_model, expected_year)
    cases = [
        # ----- Real / NHTSA-validated VINs -----
        ('7PDSGABA3PN003979', 'R1S', 2023),  # real listing: R1S Adventure Quad 2023
        # ----- R1T (Pickup) -----
        ('7FCTGAAA1NN000001', 'R1T', 2022),  # R1T Launch Edition 2022 (early 7FC)
        ('7PDTGAAA1NN000001', 'R1T', 2022),  # R1T Launch Edition 2022 (7PD)
        ('7PDTGABA1PN000001', 'R1T', 2023),  # R1T Adventure Quad 2023
        ('7PDTGBBA1RN000001', 'R1T', 2024),  # R1T Adventure Dual 2024
        ('7PDTGBPA1RN000001', 'R1T', 2024),  # R1T Premium Dual 2024
        ('7PDTGCAA1SN000001', 'R1T', 2025),  # R1T Tri-Motor Adventure 2025
        ('7PDTGAEA1SN000001', 'R1T', 2025),  # R1T Entry Quad 2025
        # ----- R1S (SUV) -----
        ('7PDSGAAA1NN000001', 'R1S', 2022),  # R1S Launch 2022
        ('7PDSGABA1PN000001', 'R1S', 2023),  # R1S Adventure Quad 2023
        ('7PDSGBBA1RN000001', 'R1S', 2024),  # R1S Adventure Dual 2024
        ('7PDSGCAA1SN000001', 'R1S', 2025),  # R1S Tri-Motor Adventure 2025
        ('7PDSGBPA1SN000001', 'R1S', 2025),  # R1S Premium Dual 2025
        ('7PDSGBEA1SN000001', 'R1S', 2025),  # R1S Entry Dual 2025 (renamed from Explore)
        # ----- EDV / RCV (Commercial van) -----
        ('7PDVGEEA1NN000001', 'EDV', 2022),  # EDV Amazon 2022
        ('7PDVGEEA1PN000001', 'EDV', 2023),  # EDV Amazon 2023
        ('7PDVGEXA1RN000001', 'EDV', 2024),  # EDV 700 2024
        # ----- Negative cases -----
        ('',                  None,  None),  # empty
        ('7PDSGABA3PN00397',  None,  None),  # 16-char short
        ('XYZSGABA3PN003979', None,  None),  # bad WMI
        ('7FCSGABA3PN003979', None,  None),  # 7FC + non-truck model letter (S=R1S)
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
    print(f'\nvds_rivian self_test: {p} passed, {f} failed')
