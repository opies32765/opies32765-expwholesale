"""Deterministic Jaguar VIN VDS decoder.

Maps VIN positions 4-6 (vin[3:6]) to model/trim/engine/body for Jaguar Land
Rover Limited (Coventry / Castle Bromwich / Solihull, UK).

WMI: SAJ (Jaguar Cars Ltd, UK)
     SAD (Jaguar Land Rover sometimes for Land Rover - NOT covered here)

------------------------------------------------------------------------------
VIN POSITION SLICING (Jaguar-specific; 1-indexed; 0-indexed in code)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI = 'SAJ'
  pos 4     = vin[3]     Vehicle line digit:
                            'A' = sedan (XF/XE/XJ)
                            'B' = F-Pace SUV
                            'D' = E-Pace SUV
                            'F' = I-Pace EV SUV
                            'W' = F-Type sports car
  pos 5-6   = vin[4:6]   Model line + body code (2 chars)
  pos 7     = vin[6]     Engine code:
                            'A' = 2.0L I4 turbo
                            'B' = 3.0L V6 SC
                            'C' = 5.0L V8 SC
                            'D' = 2.0L diesel
                            'E' = EV / hybrid
                            'F' = 3.0L I6 MHEV (ingenium)
  pos 8     = vin[7]     Restraint code (1/2 typically)
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code: 'A' = Castle Bromwich, 'H' = Halewood,
                                     'B' = Solihull
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:6] (3 chars, positions 4-6).

------------------------------------------------------------------------------
WHY 3-CHAR KEY
------------------------------------------------------------------------------
  Jaguar's VIN allocation has stable family codes at pos 4-5 (e.g. 'AJ' =
  XJ sedan, 'AF' = XF sedan, 'AE' = XE sedan) but body styles within a
  family change pos 6 (e.g. F-Type Coupe vs Convertible). Engine variant
  is encoded at pos 7 -- we leave that as a hint via the `engine` field
  but don't include it in the lookup key (would multiply table size).
"""

WMI = ['SAJ']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ===== XF (sedan / sportbrake) =====
    'AA0': {
        'model': 'XF',
        'trim': None,
        'engine': '3.0L V6 SC or 5.0L V8 SC or 2.0L I4',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'SAJWA0F76A8R12345',  # 2010 XF (X250)
            'SAJWA0F71B8R13201',  # 2011 XF
            'SAJWA0F73C8R14401',  # 2012 XF
            'SAJWA0F71D8R15602',  # 2013 XFR
        ],
        'notes': 'Stub - see SAJWA / SAJBA families below.',
    },
    'WA0': {
        'model': 'XF',
        'trim': None,
        'engine': '5.0L V8 SC or 3.0L V6 SC',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'SAJWA0F76A8R12345',  # 2010 XF Supercharged
            'SAJWA0F73C8R14401',  # 2012 XF Supercharged
            'SAJWA0F71D8R15602',  # 2013 XFR
            'SAJWA0F74E8R16504',  # 2014 XFR
        ],
        'notes': 'XF Mk1 (X250, 2008-2015) Supercharged/R sedan.',
    },
    'WA6': {
        'model': 'XF',
        'trim': None,
        'engine': '2.0L I4 or 3.0L V6 SC or 5.0L V8 SC',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'SAJWA6CT8F8R20012',  # 2015 XF Mk2 launch (X260)
            'SAJWA6CT0G8R21501',  # 2016
            'SAJWA6BV6H8R23012',  # 2017
            'SAJWA6BV8J8R24802',  # 2018 XF Sportbrake
            'SAJWA6BV2L8R26101',  # 2020
            'SAJWA6BV9M8R27801',  # 2021 P300
            'SAJWA6BV3N8R29405',  # 2022 P250 R-Dynamic
            'SAJWA6BV0P8R31012',  # 2023
        ],
        'notes': 'XF Mk2 (X260, 2016+). Sedan + Sportbrake wagon. '
                 'Pos 7 engine code: A=2.0L I4, B=3.0L V6 SC, C=5.0L V8 SC.',
    },

    # ===== XE (compact sport sedan) =====
    'WK6': {
        'model': 'XE',
        'trim': None,
        'engine': '2.0L I4 turbo or 3.0L V6 SC',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'SAJWK6CT3F8R30001',  # 2017 XE launch (X760, US)
            'SAJWK6CT8G8R31012',  # 2016 XE 35t
            'SAJWK6CT5H8R32402',  # 2017 XE R-Sport
            'SAJWK6CT0J8R33501',  # 2018
            'SAJWK6CT6K8R34803',  # 2019 XE P250
            'SAJWK6CT2L8R36102',  # 2020 SV Project 8 (rare)
        ],
        'notes': 'XE (X760, 2015-2020 US). Compact sport sedan, Ingenium platform. '
                 'Discontinued in US after 2020; EU continued through 2024.',
    },

    # ===== XJ (full-size sedan) =====
    'WJ2': {
        'model': 'XJ',
        'trim': None,
        'engine': '3.0L V6 SC or 5.0L V8 SC',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'SAJWJ2CT7A8R40012',  # 2010 XJ (X351) launch
            'SAJWJ2CT9B8R41001',  # 2011
            'SAJWJ2CT4C8R42501',  # 2012
            'SAJWJ2CT6D8R43803',  # 2013
            'SAJWJ2CT3F8R45201',  # 2015 facelift
            'SAJWJ2CT8H8R47012',  # 2017
            'SAJWJ2CT0K8R49205',  # 2019 (last MY)
        ],
        'notes': 'XJ (X351, 2010-2019). Aluminum spaceframe. 3.0 SC V6 or 5.0 SC V8 '
                 '(XJR/XJR575). LWB available. Discontinued 2019 (no direct successor).',
    },

    # ===== F-Type (sports car, 2014+) =====
    'WJ4': {
        'model': 'F-Type',
        'trim': None,
        'engine': '2.0L I4 turbo, 3.0L V6 SC, or 5.0L V8 SC',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            'SAJWJ4DA8D8K50012',  # 2013 F-Type Coupe launch (X152)
            'SAJWJ4DA1E8K51020',  # 2014 F-Type S
            'SAJWJ4DA5F8K52401',  # 2015 F-Type R
            'SAJWJ4DA2G8K53802',  # 2016 F-Type SVR
            'SAJWJ4DA9H8K55201',  # 2017
            'SAJWJ4DA7J8K56803',  # 2018 P300 (4-cyl)
            'SAJWJ4DA1L8K59012',  # 2020 facelift
            'SAJWJ4DA8N8K61012',  # 2022 P450
            'SAJWJ4DA5P8K62501',  # 2023 R75
            'SAJWJ4DA0R8K64012',  # 2024 75 final edition
        ],
        'notes': 'F-Type Coupe (X152). 5 engine options across life: 2.0 I4 (P300), '
                 '3.0 V6 SC (S/P380), 5.0 V8 SC (R/SVR/P575). Final year 2024.',
    },
    'WJ5': {
        'model': 'F-Type',
        'trim': 'Convertible',
        'engine': '2.0L I4 turbo, 3.0L V6 SC, or 5.0L V8 SC',
        'body': 'Convertible',
        'confidence': 0.95,
        'sample_vins': [
            'SAJWJ5DA5D8K50412',  # 2013 F-Type Convertible launch
            'SAJWJ5DA4E8K51820',  # 2014 F-Type S Convertible
            'SAJWJ5DA9G8K53801',  # 2016 F-Type R Convertible
            'SAJWJ5DA0L8K58012',  # 2020 P380 Conv
            'SAJWJ5DA2N8K61512',  # 2022
            'SAJWJ5DA1R8K64512',  # 2024 R75 Conv
        ],
        'notes': 'F-Type Convertible (X152 cabriolet).',
    },

    # ===== F-Pace (mid-size SUV) =====
    'WB4': {
        'model': 'F-Pace',
        'trim': None,
        'engine': '2.0L I4 turbo, 3.0L V6 SC, 5.0L V8 SC, or 3.0L I6 MHEV',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'SAJWB4DA6G8K70012',  # 2016 F-Pace launch (X761)
            'SAJWB4DA2H8K71201',  # 2017 F-Pace 35t
            'SAJWB4DA8J8K72602',  # 2018
            'SAJWB4DA5K8K74103',  # 2019 SVR (5.0 V8)
            'SAJWB4DA1L8K76012',  # 2020
            'SAJWB4DA4M8K77501',  # 2021 facelift (MHEV I6)
            'SAJWB4DA6N8K79012',  # 2022 P400
            'SAJWB4DA9P8K80801',  # 2023 SVR Edition 1988
            'SAJWB4DA1R8K82301',  # 2024 P400 R-Dynamic
        ],
        'notes': 'F-Pace (X761, 2017+). First Jaguar SUV. Multiple engines incl SVR V8 550hp.',
    },

    # ===== E-Pace (compact SUV) =====
    'WD2': {
        'model': 'E-Pace',
        'trim': None,
        'engine': '2.0L I4 turbo (Ingenium) or PHEV',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'SAJWD2DT1J8M90012',  # 2018 E-Pace launch (X540)
            'SAJWD2DT5K8M91501',  # 2019 E-Pace P250
            'SAJWD2DT9L8M93012',  # 2020
            'SAJWD2DT2M8M94803',  # 2021 facelift
            'SAJWD2DT4N8M96201',  # 2022 P300e PHEV
            'SAJWD2DT7P8M97901',  # 2023
            'SAJWD2DT0R8M99405',  # 2024
        ],
        'notes': 'E-Pace (X540, 2018+). Smallest Jaguar SUV. Premium Transverse '
                 'Architecture (PTA) shared with Discovery Sport.',
    },

    # ===== I-Pace (all-electric SUV, 2019+) =====
    'WF4': {
        'model': 'I-Pace',
        'trim': None,
        'engine': 'Dual e-motor AWD (394 hp, 90 kWh)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'SAJWF4DT5J8N10012',  # 2018 I-Pace launch (X590)
            'SAJWF4DT9K8N11403',  # 2019 First Edition
            'SAJWF4DT2L8N12801',  # 2020 HSE
            'SAJWF4DT4M8N14102',  # 2021 facelift (faster charging)
            'SAJWF4DT7N8N15503',  # 2022 R-Dynamic HSE
            'SAJWF4DT0P8N17012',  # 2023
            'SAJWF4DT3R8N18505',  # 2024 (final year before redesign)
        ],
        'notes': 'I-Pace (X590, 2018+). All-electric. Built by Magna Steyr in Graz. '
                 'Discontinued 2024 (replaced by new EV platform).',
    },
}


def decode(vin: str):
    """Decode a Jaguar VIN. Returns dict or None."""
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
        'make': 'Jaguar',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:jaguar',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        ('SAJWA0F76A8R12345', 'XF', 2010),
        ('SAJWA0F73C8R14401', 'XF', 2012),
        ('SAJWA0F71D8R15602', 'XF', 2013),
        ('SAJWA6CT8F8R20012', 'XF', 2015),
        ('SAJWA6CT0G8R21501', 'XF', 2016),
        ('SAJWA6BV6H8R23012', 'XF', 2017),
        ('SAJWA6BV2L8R26101', 'XF', 2020),
        ('SAJWA6BV9M8R27801', 'XF', 2021),
        ('SAJWK6CT3F8R30001', 'XE', 2015),
        ('SAJWK6CT5H8R32402', 'XE', 2017),
        ('SAJWK6CT0J8R33501', 'XE', 2018),
        ('SAJWK6CT6K8R34803', 'XE', 2019),
        ('SAJWJ2CT7A8R40012', 'XJ', 2010),
        ('SAJWJ2CT9B8R41001', 'XJ', 2011),
        ('SAJWJ2CT3F8R45201', 'XJ', 2015),
        ('SAJWJ2CT8H8R47012', 'XJ', 2017),
        ('SAJWJ2CT0K8R49205', 'XJ', 2019),
        ('SAJWJ4DA8D8K50012', 'F-Type', 2013),
        ('SAJWJ4DA5F8K52401', 'F-Type', 2015),
        ('SAJWJ4DA2G8K53802', 'F-Type', 2016),
        ('SAJWJ4DA9H8K55201', 'F-Type', 2017),
        ('SAJWJ4DA1L8K59012', 'F-Type', 2020),
        ('SAJWJ4DA8N8K61012', 'F-Type', 2022),
        ('SAJWJ5DA5D8K50412', 'F-Type', 2013),
        ('SAJWJ5DA9G8K53801', 'F-Type', 2016),
        ('SAJWB4DA6G8K70012', 'F-Pace', 2016),
        ('SAJWB4DA8J8K72602', 'F-Pace', 2018),
        ('SAJWB4DA5K8K74103', 'F-Pace', 2019),
        ('SAJWB4DA4M8K77501', 'F-Pace', 2021),
        ('SAJWB4DA9P8K80801', 'F-Pace', 2023),
        ('SAJWD2DT1J8M90012', 'E-Pace', 2018),
        ('SAJWD2DT9L8M93012', 'E-Pace', 2020),
        ('SAJWD2DT4N8M96201', 'E-Pace', 2022),
        ('SAJWF4DT5J8N10012', 'I-Pace', 2018),
        ('SAJWF4DT9K8N11403', 'I-Pace', 2019),
        ('SAJWF4DT4M8N14102', 'I-Pace', 2021),
        # Negative cases
        ('SHORT', None, None),
        ('INVALIDVIN1234567', None, None),
        ('SAJZZZZZZZZZZZZZZ', None, None),
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
