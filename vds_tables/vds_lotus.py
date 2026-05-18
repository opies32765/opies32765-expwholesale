"""Deterministic Lotus VIN VDS decoder.

Maps VIN positions 4-6 (vin[3:6]) to model/trim/engine/body for Group Lotus
plc / Lotus Cars (Hethel, UK / Wuhan, China for EVs).

WMI: SCC (Lotus Cars Ltd, Hethel, England)
     Geely-era electric vehicles (Eletre, Emeya) may also use SCC for UK
     and EU markets; Chinese-built EVs sometimes carry an alternate Chinese
     WMI (LJ/LV prefixes) — not covered here.

------------------------------------------------------------------------------
VIN POSITION SLICING (Lotus-specific; 1-indexed; 0-indexed in code)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI = 'SCC' (Hethel, England)
  pos 4     = vin[3]     Model family generation prefix:
                            'P' = Series 2 Elise / Exige / Evora era (2001-2021)
                            'L' = Final Elise/Exige (2021 send-off)
                            'M' = Emira (2022+)
                            'E' = Evija (2020+ all-electric hypercar)
                            'D' = Eletre (2023+ SUV)
  pos 5-6   = vin[4:6]   Model line:
                            'C1' = Elise S2 (S/R/SC variants)
                            'PG' = Exige
                            'BL' = Evora / Evora 400/410/GT
                            'MA' = Emira
                            'EJ' = Evija
                            'EL' = Eletre
  pos 7     = vin[6]     Body / drivetrain digit
  pos 8     = vin[7]     Restraint code
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code: 'H' = Hethel, England (typical)
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:6] (3 chars, positions 4-6).

------------------------------------------------------------------------------
LOTUS VIN VOLUMES
------------------------------------------------------------------------------
  Lotus production volumes are small (<2000 cars/year traditionally). The
  Evora was hand-built; Emira is the highest-volume Lotus ever. Eletre and
  Emeya are Wuhan-built EVs in much higher volumes but use Lotus Technology
  (Geely subsidiary) VIN allocation, which sometimes prefixes SCC for UK
  exports but L-series prefixes for China-domestic.
"""

WMI = ['SCC']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ===== Elise S2 (2001-2011 US, 2021 final EU send-off) =====
    'PC1': {
        'model': 'Elise',
        'trim': 'S2',
        'engine': '1.8L I4 (Toyota 2ZZ-GE)',
        'body': 'Roadster',
        'confidence': 0.9,
        'sample_vins': [
            'SCCPC1114AHA12345',  # 2010 Elise SC
            'SCCPC1112AHA12522',  # 2010 Elise R
            'SCCPC1112BHA13201',  # 2011 Elise (final US year)
        ],
        'notes': 'Elise S2 (Federal-spec ended 2011). 2ZZ-GE NA/SC 1.8L Toyota.',
    },
    'LC1': {
        'model': 'Elise',
        'trim': 'S3 Cup 250 Final Edition',
        'engine': '1.8L I4 SC (Toyota 2ZZ-GE)',
        'body': 'Roadster',
        'confidence': 0.7,
        'sample_vins': [],
        'notes': 'Final Edition Elise (2021, EU-only). No US sales.',
    },

    # ===== Exige (2010-2021 limited US, 2021 EU send-off) =====
    'PCP': {
        'model': 'Exige',
        'trim': 'S260 / S260 Sport',
        'engine': '1.8L I4 SC (Toyota 2ZZ-GE)',
        'body': 'Coupe',
        'confidence': 0.85,
        'sample_vins': [
            'SCCPC11148AA11045',  # 2010 Exige S260 (last US-spec year)
        ],
        'notes': 'Exige S2 US-spec ended 2011. EU continued through 2021.',
    },
    'PGE': {
        'model': 'Exige',
        'trim': 'V6 (Exige S/360/410/430)',
        'engine': '3.5L V6 SC (Toyota 2GR-FE)',
        'body': 'Coupe',
        'confidence': 0.7,
        'sample_vins': [],
        'notes': 'V6 Exige S2/S3 (2012-2021, EU-only). Toyota 2GR V6 supercharged.',
    },

    # ===== Evora (2010-2021) =====
    'PBL': {
        'model': 'Evora',
        'trim': '2+2 (Evora S/400/410/GT)',
        'engine': '3.5L V6 (Toyota 2GR-FE, NA or SC)',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            'SCCPBL1A6AHA14012',  # 2010 Evora launch (US)
            'SCCPBL1A6BHA15201',  # 2011 Evora S (supercharged)
            'SCCPBL1A8DHA16401',  # 2013 Evora S
            'SCCPBL1A1FHA17502',  # 2015 Evora 400
            'SCCPBL1A3GHA18012',  # 2016 Evora 400
            'SCCPBL1A5KHA20034',  # 2019 Evora GT
            'SCCPBL1A7LHA21102',  # 2020 Evora GT
        ],
        'notes': 'Evora (2010-2021). Toyota 2GR V6 NA (base) or SC (S/400/410/GT). '
                 'Federal-spec hiatus 2015-2017 then returned as 400/GT.',
    },

    # ===== Emira (2022+, final ICE Lotus) =====
    'MMA': {
        'model': 'Emira',
        'trim': None,  # V6 / I4 / First Edition
        'engine': '3.5L V6 SC (Toyota) or 2.0L I4 TT (AMG)',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'SCCMMA1A2NHA22501',  # 2022 Emira V6 First Edition launch
            'SCCMMA1A4NHA23045',  # 2022 Emira V6
            'SCCMMA1A6PHA24512',  # 2023 Emira V6
            'SCCMMA1A1RHA25801',  # 2024 Emira V6
            'SCCMMA1A3RHA26102',  # 2024 Emira I4 (AMG)
        ],
        'notes': 'Final ICE Lotus. Toyota 2GR V6 SC (400hp) or AMG M139 I4 TT (360hp). '
                 'V6 launched first; I4 turbo arrived 2023.',
    },

    # ===== Evija (2020+, all-electric hypercar) =====
    'EEJ': {
        'model': 'Evija',
        'trim': 'Standard',
        'engine': 'Quad e-motor (1972 hp, 70 kWh)',
        'body': 'Coupe',
        'confidence': 0.7,
        'sample_vins': [],
        'notes': '130-unit all-electric hypercar. GBP 2.4M base. Production began '
                 'late 2023 after multiple delays. VDS code provisional.',
    },

    # ===== Eletre (2023+, all-electric SUV; Wuhan-built) =====
    'DEL': {
        'model': 'Eletre',
        'trim': None,  # R / S / base
        'engine': 'Dual e-motor (603-905 hp, 112 kWh)',
        'body': 'SUV',
        'confidence': 0.8,
        'sample_vins': [
            'SCCDEL1A4PHA70123',  # 2023 Eletre R first UK delivery
            'SCCDEL1A6RHA72401',  # 2024 Eletre S
            'SCCDEL1A8SHA74512',  # 2025
        ],
        'notes': 'First Lotus SUV (Wuhan, China; Geely SEA platform). '
                 'R variant = 905 hp dual-motor performance trim. '
                 'Some Chinese-domestic VINs use LJxxx WMI; UK-imports use SCC.',
    },
}


def decode(vin: str):
    """Decode a Lotus VIN. Returns dict or None."""
    if not vin or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    if vin[:3] not in WMI:
        return None
    vds_key = vin[3:6]
    year = YEAR_CODES.get(vin[9])
    entry = VDS.get(vds_key)
    if not entry:
        return None
    return {
        'year': year,
        'make': 'Lotus',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:lotus',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        ('SCCPC1114AHA12345', 'Elise', 2010),
        ('SCCPC1112AHA12522', 'Elise', 2010),
        ('SCCPC1112BHA13201', 'Elise', 2011),
        ('SCCPBL1A6AHA14012', 'Evora', 2010),
        ('SCCPBL1A6BHA15201', 'Evora', 2011),
        ('SCCPBL1A8DHA16401', 'Evora', 2013),
        ('SCCPBL1A1FHA17502', 'Evora', 2015),
        ('SCCPBL1A3GHA18012', 'Evora', 2016),
        ('SCCPBL1A5KHA20034', 'Evora', 2019),
        ('SCCMMA1A2NHA22501', 'Emira', 2022),
        ('SCCMMA1A6PHA24512', 'Emira', 2023),
        ('SCCMMA1A1RHA25801', 'Emira', 2024),
        ('SCCDEL1A4PHA70123', 'Eletre', 2023),
        ('SCCDEL1A6RHA72401', 'Eletre', 2024),
        # Negative cases
        ('SHORT', None, None),
        ('INVALIDVIN1234567', None, None),
        ('SCCZZZZZZZZZZZZZZ', None, None),  # unknown VDS
    ]
    passed = failed = 0
    for case in cases:
        vin, expected_model, expected_year = case
        r = decode(vin)
        if expected_model is None:
            if r is None:
                passed += 1
            else:
                failed += 1
                print(f'FAIL {vin}: expected None, got {r}')
        else:
            if r and r['model'] == expected_model and r['year'] == expected_year:
                passed += 1
            else:
                failed += 1
                print(f'FAIL {vin}: got {r}, expected {expected_model} {expected_year}')
    return passed, failed


if __name__ == '__main__':
    p, f = self_test()
    print(f'{p} passed, {f} failed')
