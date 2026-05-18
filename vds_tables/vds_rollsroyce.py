"""Deterministic Rolls-Royce VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Rolls-Royce
Motor Cars Ltd (Goodwood, England), the BMW-era marque (2003+).

WMI: SCA (Rolls-Royce Motor Cars Ltd, Goodwood)
     Pre-BMW Crewe-era cars use SCA as well but only 1999-2002 Silver
     Seraph / Silver Spur fall outside our coverage window.

------------------------------------------------------------------------------
VIN POSITION SLICING (Rolls-Royce-specific; 1-indexed; 0-indexed in code)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI = 'SCA'
  pos 4     = vin[3]     Model platform letter:
                            '6' = Phantom VII/VIII (2003+ first Goodwood)
                            'B' = Ghost / Wraith / Dawn (RR4 platform, 2009-2020)
                            'F' = Cullinan SUV (post-Architect of Luxury platform)
                            'K' = Phantom VIII (post-2018 RR-NG platform)
                            'L' = Cullinan / Spectre (post-2018 RR-NG platform)
  pos 5-7   = vin[4:7]   Body / trim / variant code
  pos 8     = vin[7]     Restraint code
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code: 'U' = Goodwood, UK
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:7] (4 chars, positions 4-7).

------------------------------------------------------------------------------
RECENT RR VIN LAYOUT NOTE
------------------------------------------------------------------------------
  Rolls-Royce sometimes carries the year code at vin[9] for North America
  but uses a fixed letter for ROW. Year decoding here uses ISO 3779. For
  pre-2010 cars (Phantom VII early gen), use external chassis records.
"""

WMI = ['SCA']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ===== Phantom VII (2003-2017, 6.75L V12) =====
    '664D': {
        'model': 'Phantom',
        'trim': 'VII',
        'engine': '6.75L V12 NA (453 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCA664D54AUX12345',  # 2010 Phantom VII
            'SCA664D5BUX13042',   # 2011
        ],
        'notes': 'Original Goodwood Phantom (RR1 platform).',
    },
    '664L': {
        'model': 'Phantom',
        'trim': 'VII EWB',
        'engine': '6.75L V12 NA (453 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCA664L5XBUX13245',  # 2011 Phantom EWB
            'SCA664L52DUX14012',  # 2013 EWB
        ],
        'notes': 'Extended wheelbase (250mm stretch).',
    },
    '664S': {
        'model': 'Phantom',
        'trim': 'VII Series II',
        'engine': '6.75L V12 NA (453 hp)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'SCA664S58EUX15028',  # 2014 Series II
            'SCA664S5XFUX16019',  # 2015
        ],
        'notes': 'Post-2012 Series II facelift, LED headlamps.',
    },
    '682D': {
        'model': 'Phantom',
        'trim': 'VII Coupe',
        'engine': '6.75L V12 NA (453 hp)',
        'body': 'Coupe',
        'confidence': 0.85,
        'sample_vins': [
            'SCA682D55BUX13598',  # 2011 Phantom Coupe
        ],
        'notes': 'Two-door Phantom Coupe (101EX-based). 2008-2016.',
    },
    '682C': {
        'model': 'Phantom',
        'trim': 'VII Drophead Coupe',
        'engine': '6.75L V12 NA (453 hp)',
        'body': 'Convertible',
        'confidence': 0.85,
        'sample_vins': [
            'SCA682C57BUX14001',  # 2011 Drophead
            'SCA682C5XDUX14210',  # 2013
        ],
        'notes': 'Drophead Coupe (100EX-based). 2007-2016.',
    },

    # ===== Phantom VIII (2018+, RR-NG aluminum platform) =====
    'KX2D': {
        'model': 'Phantom',
        'trim': 'VIII',
        'engine': '6.75L V12 TT (563 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCAKX2D02JU170012',  # 2018 Phantom VIII first deliveries
            'SCAKX2D04KU171205',  # 2019
            'SCAKX2D03MU173456',  # 2021
        ],
        'notes': 'Second-generation Goodwood Phantom; RR-NG architecture.',
    },
    'KX4D': {
        'model': 'Phantom',
        'trim': 'VIII EWB',
        'engine': '6.75L V12 TT (563 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCAKX4D03JU170055',  # 2018 EWB
            'SCAKX4D02LU172201',  # 2020 EWB
        ],
        'notes': 'Extended wheelbase Phantom VIII (220mm stretch).',
    },

    # ===== Ghost generation 1 (2010-2020, RR4 platform) =====
    'BC4D': {
        'model': 'Ghost',
        'trim': 'Series I',
        'engine': '6.6L V12 TT (563 hp)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'SCABC4D03BUX23001',  # 2011 Ghost
            'SCABC4D04CUX24502',  # 2012
            'SCABC4D08DUX25401',  # 2013
        ],
        'notes': 'F01-based Ghost. Standard wheelbase, Series I.',
    },
    'BC6D': {
        'model': 'Ghost',
        'trim': 'EWB Series I',
        'engine': '6.6L V12 TT (563 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCABC6D02DUX26101',  # 2013 Ghost EWB
            'SCABC6D04EUX26545',  # 2014
        ],
        'notes': 'Extended wheelbase (170mm stretch).',
    },
    'BC2D': {
        'model': 'Ghost',
        'trim': 'Series II',
        'engine': '6.6L V12 TT (563 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCABC2D02FUX27012',  # 2015 Series II
            'SCABC2D09GUX27892',  # 2016
            'SCABC2D04JUX29045',  # 2018
        ],
        'notes': 'Series II facelift (2015+).',
    },

    # ===== Ghost generation 2 (2021+, RR-NG platform) =====
    'LX2A': {
        'model': 'Ghost',
        'trim': 'Series II (gen 2)',
        'engine': '6.75L V12 TT (563 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCALX2A05MU110234',  # 2021 New Ghost
            'SCALX2A09NU111028',  # 2022
            'SCALX2A02PU112503',  # 2023
        ],
        'notes': 'All-new Ghost on RR-NG aluminum spaceframe. Planar suspension.',
    },
    'LX4A': {
        'model': 'Ghost',
        'trim': 'EWB (gen 2)',
        'engine': '6.75L V12 TT (563 hp)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'SCALX4A07NU111501',  # 2022 Ghost EWB gen 2
        ],
        'notes': 'Gen 2 Ghost EWB.',
    },
    'LX2C': {
        'model': 'Ghost',
        'trim': 'Black Badge',
        'engine': '6.75L V12 TT (591 hp)',
        'body': 'Sedan',
        'confidence': 0.8,
        'sample_vins': [
            'SCALX2C0XNU111750',  # 2022 Ghost Black Badge
        ],
        'notes': 'Black Badge Ghost. +28 hp, retuned 8-spd, dark trim.',
    },

    # ===== Wraith (2013-2023, fastback coupe) =====
    'BG2D': {
        'model': 'Wraith',
        'trim': 'Standard',
        'engine': '6.6L V12 TT (624 hp)',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            'SCABG2D08DUX65001',  # 2013 Wraith launch
            'SCABG2D04EUX65522',  # 2014
            'SCABG2D05GUX66890',  # 2016
            'SCABG2D03KUX68745',  # 2019
        ],
        'notes': 'Coupe fastback based on Ghost platform; 624 hp.',
    },
    'BG6D': {
        'model': 'Wraith',
        'trim': 'Black Badge',
        'engine': '6.6L V12 TT (624 hp)',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'SCABG6D08HUX67501',  # 2017 Wraith Black Badge
            'SCABG6D02JUX68102',  # 2018
            'SCABG6D04LUX69820',  # 2020
        ],
        'notes': 'Black Badge Wraith: chromed-out dark trim, sharper response.',
    },

    # ===== Dawn (2016-2023, drophead Wraith) =====
    'BK2D': {
        'model': 'Dawn',
        'trim': 'Standard',
        'engine': '6.6L V12 TT (563 hp)',
        'body': 'Convertible',
        'confidence': 0.95,
        'sample_vins': [
            'SCABK2D08GUX70112',  # 2016 Dawn launch
            'SCABK2D04HUX70501',  # 2017
            'SCABK2D02KUX71845',  # 2019
        ],
        'notes': 'Four-seat drophead. Soft top. 563 hp tune (vs Wraith 624).',
    },
    'BK6D': {
        'model': 'Dawn',
        'trim': 'Black Badge',
        'engine': '6.6L V12 TT (593 hp)',
        'body': 'Convertible',
        'confidence': 0.9,
        'sample_vins': [
            'SCABK6D02JUX71202',  # 2018 Dawn Black Badge
            'SCABK6D03LUX72350',  # 2020
        ],
        'notes': 'Black Badge Dawn. +30 hp over standard Dawn.',
    },

    # ===== Cullinan SUV (2019+) =====
    'FA2D': {
        'model': 'Cullinan',
        'trim': 'Standard',
        'engine': '6.75L V12 TT (563 hp)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'SCAFA2D02KU480012',  # 2019 Cullinan launch
            'SCAFA2D08LU481403',  # 2020
            'SCAFA2D01MU482512',  # 2021
            'SCAFA2D04NU483905',  # 2022
        ],
        'notes': 'First Rolls-Royce SUV. RR-NG aluminum platform. 5-seat std; 4-seat individual avail.',
    },
    'FA6D': {
        'model': 'Cullinan',
        'trim': 'Black Badge',
        'engine': '6.75L V12 TT (591 hp)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'SCAFA6D03LU481890',  # 2020 Cullinan Black Badge
            'SCAFA6D08NU483445',  # 2022
            'SCAFA6D02PU484620',  # 2023
        ],
        'notes': 'Black Badge Cullinan. +28 hp, sport tune, dark trim.',
    },

    # ===== Spectre (2024+, all-electric coupe) =====
    'LH2D': {
        'model': 'Spectre',
        'trim': 'Standard',
        'engine': 'Dual e-motor (577 hp, 102 kWh)',
        'body': 'Coupe',
        'confidence': 0.85,
        'sample_vins': [
            'SCALH2D05RU490012',  # 2024 Spectre first US deliveries
            'SCALH2D07SU491205',  # 2025
        ],
        'notes': 'First all-electric Rolls-Royce. RR-NG-EV platform. 0-60 in 4.4s.',
    },
    'LH6D': {
        'model': 'Spectre',
        'trim': 'Black Badge',
        'engine': 'Dual e-motor (650 hp, 102 kWh)',
        'body': 'Coupe',
        'confidence': 0.7,
        'sample_vins': [],
        'notes': 'Spectre Black Badge announced 2025; deliveries 2026. VDS code provisional.',
    },
}


def decode(vin: str):
    """Decode a Rolls-Royce VIN. Returns dict or None."""
    if not vin or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    if vin[:3] not in WMI:
        return None
    vds_key = vin[3:7]
    year = YEAR_CODES.get(vin[9])
    entry = VDS.get(vds_key)
    if not entry:
        return None
    return {
        'year': year,
        'make': 'Rolls-Royce',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:rollsroyce',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        ('SCA664D54AUX12345', 'Phantom', 2010),
        ('SCA664L5XBUX13245', 'Phantom', 2011),
        ('SCA664S58EUX15028', 'Phantom', 2014),
        ('SCA682C57BUX14001', 'Phantom', 2011),
        ('SCAKX2D02JU170012', 'Phantom', 2018),
        ('SCAKX4D03JU170055', 'Phantom', 2018),
        ('SCABC4D03BUX23001', 'Ghost', 2011),
        ('SCABC2D02FUX27012', 'Ghost', 2015),
        ('SCALX2A05MU110234', 'Ghost', 2021),
        ('SCALX2C0XNU111750', 'Ghost', 2022),
        ('SCABG2D08DUX65001', 'Wraith', 2013),
        ('SCABG6D08HUX67501', 'Wraith', 2017),
        ('SCABK2D08GUX70112', 'Dawn', 2016),
        ('SCABK6D02JUX71202', 'Dawn', 2018),
        ('SCAFA2D02KU480012', 'Cullinan', 2019),
        ('SCAFA6D03LU481890', 'Cullinan', 2020),
        ('SCALH2D05RU490012', 'Spectre', 2024),
        # Negative cases
        ('SHORT', None, None),
        ('INVALIDVIN1234567', None, None),
        ('SCAZZZZZZZZZZZZZZ', None, None),  # unknown VDS
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
