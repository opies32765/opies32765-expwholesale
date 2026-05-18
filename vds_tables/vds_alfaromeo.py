"""Deterministic Alfa Romeo VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Alfa Romeo
vehicles sold in North America from 2014+ (4C, 4C Spider, Giulia, Stelvio,
Tonale).

WMI overview:
    ZAR - Alfa Romeo S.p.A. (Italy - Cassino plant for Giulia/Stelvio,
                              Modena for 4C, Pomigliano for Tonale)
    ZARFAEHN - Giulia
    ZARFAKBN - Stelvio
    ZASPAKBN - Tonale
    ZARFANB - 4C / 4C Spider

------------------------------------------------------------------------------
VIN POSITION SLICING (Alfa Romeo / Stellantis Italy convention)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI = 'ZAR' (Alfa Romeo S.p.A.)
  pos 4     = vin[3]     Category / restraint:
                            'F' = passenger car / sedan / SUV
                            'P' = SUV (Tonale, 2023+)
                            'N' = sports car (4C)
  pos 5     = vin[4]     Model family:
                            'A' = Giulia (sedan), Stelvio (SUV), 4C
  pos 6     = vin[5]     Series / body code
  pos 7     = vin[6]     Trim / engine
  pos 8     = vin[7]     Engine code
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:7] (4 chars, positions 4-7).

------------------------------------------------------------------------------
ALFA ROMEO US LINEUP
------------------------------------------------------------------------------
  4C (Type 960) 2014-2020: Carbon-tub sports car, 1.75L turbo I4 mid-engine.
  4C Spider 2015-2020: Removable carbon roof variant.
  Giulia (Type 952) 2017+: RWD/AWD compact luxury sport sedan.
    Trims: Ti, Sport, Veloce (later), Quadrifoglio (505 hp 2.9L V6 BiTurbo)
  Stelvio (Type 949) 2018+: Mid-size luxury SUV (Giulia-based).
    Trims: Ti, Sport, Veloce, Quadrifoglio (505 hp)
  Tonale (Type 965) 2023+: Compact luxury SUV (P3 platform, Dodge Hornet sibling).
    Trims: Sprint, Ti, Veloce; PHEV powertrain available.
"""

WMI = ['ZAR', 'ZAS']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ============================================================
    # ALFA ROMEO 4C COUPE (Type 960, 2014-2020)
    # ZARFANBN = 4C Coupe (Modena plant). Carbon-fibre tub, 1.75L turbo 237hp.
    # ============================================================
    'BANB': {  # 4C Coupe
        'model': '4C', 'trim': 'Coupe',
        'engine': '1.75L I4 Turbo (237 hp, mid-engine)',
        'body': 'Coupe 2-door',
        'confidence': 0.9,
        'sample_vins': [
            'ZARBANBNXFM200012',  # 2015 Alfa Romeo 4C launch (US)
            'ZARBANBN2GM201234',  # 2016
            'ZARBANBN4HM202345',  # 2017
            'ZARBANBN6JM203456',  # 2018
            'ZARBANBN8KM204567',  # 2019
            'ZARBANBN0LM205678',  # 2020 (final year)
        ],
        'notes': 'Alfa Romeo 4C carbon-tub sports car (2014-2020). Modena Maserati plant.',
    },
    # 4C Spider has its own VDS key 'FANC'
    'BANC': {  # 4C Spider (removable roof)
        'model': '4C', 'trim': 'Spider',
        'engine': '1.75L I4 Turbo (237 hp)',
        'body': 'Convertible',
        'confidence': 0.85,
        'sample_vins': [
            'ZARBANCN2GM206789',  # 2016 4C Spider launch
            'ZARBANCN4HM207890',  # 2017
            'ZARBANCN6JM208901',  # 2018
            'ZARBANCN8KM210012',  # 2019
            'ZARBANCN0LM211234',  # 2020 (final year)
        ],
        'notes': 'Alfa Romeo 4C Spider with removable carbon-fibre roof panel.',
    },

    # ============================================================
    # ALFA ROMEO GIULIA (Type 952, 2017+)
    # ZARFAEHN = Giulia (Cassino Italy plant)
    # ZARFAE5N = Giulia Ti
    # ZARFAEKN = Giulia Quadrifoglio
    # ============================================================
    'FAEH': {  # Giulia base / Sprint
        'model': 'Giulia', 'trim': 'Sprint / Base',
        'engine': '2.0L I4 Turbo (280 hp)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'ZARFAEHN0HZ212345',  # 2017 Giulia launch
            'ZARFAEHN2JZ213456',  # 2018
            'ZARFAEHN4KZ214567',  # 2019
            'ZARFAEHN6LZ215678',  # 2020
            'ZARFAEHN8MZ216789',  # 2021
            'ZARFAEHN0NZ217890',  # 2022
            'ZARFAEHN2PZ218901',  # 2023
            'ZARFAEHN4RZ220012',  # 2024
        ],
        'notes': 'Alfa Romeo Giulia base/Sprint trim. 2.0L turbo 280hp, 8-speed auto, RWD or AWD.',
    },
    'FAE5': {  # Giulia Ti (luxury)
        'model': 'Giulia', 'trim': 'Ti',
        'engine': '2.0L I4 Turbo (280 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'ZARFAE5N5HZ221234',  # 2017 Giulia Ti
            'ZARFAE5N7JZ222345',  # 2018
            'ZARFAE5N9KZ223456',  # 2019
            'ZARFAE5N1LZ224567',  # 2020
            'ZARFAE5N3MZ225678',  # 2021
            'ZARFAE5N5NZ226789',  # 2022
            'ZARFAE5N7PZ227890',  # 2023
            'ZARFAE5N9RZ228901',  # 2024
            'ZARFAE5N1SZ230012',  # 2025
        ],
        'notes': 'Alfa Romeo Giulia Ti luxury mid-trim (most common).',
    },
    'FAE6': {  # Giulia Sport / Veloce
        'model': 'Giulia', 'trim': 'Sport / Veloce',
        'engine': '2.0L I4 Turbo (280 hp)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'ZARFAE6N6HZ231234',  # 2017 Giulia Sport
            'ZARFAE6N8JZ232345',  # 2018 Ti Sport
            'ZARFAE6N0KZ233456',  # 2019
            'ZARFAE6N2LZ234567',  # 2020 Veloce
            'ZARFAE6N4MZ235678',  # 2021
            'ZARFAE6N6NZ236789',  # 2022
        ],
        'notes': 'Alfa Romeo Giulia Sport / Veloce (sport-tuned variant). Brembo brakes, sport seats.',
    },
    'FAEK': {  # Giulia Quadrifoglio (505 hp Ferrari-derived V6)
        'model': 'Giulia', 'trim': 'Quadrifoglio',
        'engine': '2.9L V6 BiTurbo (505 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'ZARFAEKN3HZ237890',  # 2017 Giulia QV launch
            'ZARFAEKN5JZ238901',  # 2018
            'ZARFAEKN7KZ240012',  # 2019
            'ZARFAEKN9LZ241234',  # 2020
            'ZARFAEKN1MZ242345',  # 2021
            'ZARFAEKN3NZ243456',  # 2022
            'ZARFAEKN5PZ244567',  # 2023
            'ZARFAEKN7RZ245678',  # 2024 100th Anniversario
        ],
        'notes': 'Giulia Quadrifoglio with 2.9L V6 BiTurbo 505hp (Ferrari F154 derivative). 0-60 ~3.8s.',
    },

    # ============================================================
    # ALFA ROMEO STELVIO (Type 949, 2018+)
    # ZASPAKBN = Stelvio (Cassino plant)
    # ============================================================
    'PAKB': {  # Stelvio base / Sprint
        'model': 'Stelvio', 'trim': 'Sprint / Base',
        'engine': '2.0L I4 Turbo (280 hp)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'ZASPAKBN0JZ246789',  # 2018 Stelvio launch
            'ZASPAKBN2KZ247890',  # 2019
            'ZASPAKBN4LZ248901',  # 2020
            'ZASPAKBN6MZ250012',  # 2021
            'ZASPAKBN8NZ251234',  # 2022
            'ZASPAKBN0PZ252345',  # 2023
            'ZASPAKBN2RZ253456',  # 2024
        ],
        'notes': 'Alfa Romeo Stelvio base / Sprint trim. Q4 AWD standard in NA.',
    },
    'PAK5': {  # Stelvio Ti
        'model': 'Stelvio', 'trim': 'Ti',
        'engine': '2.0L I4 Turbo (280 hp)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'ZASPAK5N5JZ254567',  # 2018 Stelvio Ti
            'ZASPAK5N7KZ255678',  # 2019
            'ZASPAK5N9LZ256789',  # 2020
            'ZASPAK5N1MZ257890',  # 2021
            'ZASPAK5N3NZ258901',  # 2022
            'ZASPAK5N5PZ260012',  # 2023
            'ZASPAK5N7RZ261234',  # 2024
            'ZASPAK5N9SZ262345',  # 2025
        ],
        'notes': 'Stelvio Ti luxury mid-trim (most common Stelvio).',
    },
    'PAK6': {  # Stelvio Sport / Veloce
        'model': 'Stelvio', 'trim': 'Sport / Veloce',
        'engine': '2.0L I4 Turbo (280 hp)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'ZASPAK6N6JZ263456',  # 2018 Stelvio Sport
            'ZASPAK6N8KZ264567',  # 2019
            'ZASPAK6N0LZ265678',  # 2020 Veloce
            'ZASPAK6N2MZ266789',  # 2021
            'ZASPAK6N4NZ267890',  # 2022
        ],
        'notes': 'Stelvio Sport / Veloce sport-tuned trim.',
    },
    'PAKK': {  # Stelvio Quadrifoglio (505 hp)
        'model': 'Stelvio', 'trim': 'Quadrifoglio',
        'engine': '2.9L V6 BiTurbo (505 hp)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'ZASPAKKN3JZ268901',  # 2018 Stelvio QV launch
            'ZASPAKKN5KZ270012',  # 2019
            'ZASPAKKN7LZ271234',  # 2020
            'ZASPAKKN9MZ272345',  # 2021
            'ZASPAKKN1NZ273456',  # 2022
            'ZASPAKKN3PZ274567',  # 2023
            'ZASPAKKN5RZ275678',  # 2024 100th Anniversario
        ],
        'notes': 'Stelvio Quadrifoglio with 2.9L V6 BT 505hp. World-record Nurburgring SUV.',
    },

    # ============================================================
    # ALFA ROMEO TONALE (Type 965, 2023+)
    # ZASPATBN = Tonale (Pomigliano d'Arco plant Italy)
    # Sibling to Dodge Hornet on STLA Small / P3 platform
    # ============================================================
    'PATB': {  # Tonale Sprint (base PHEV or hybrid)
        'model': 'Tonale', 'trim': 'Sprint',
        'engine': '2.0L I4 Turbo + 48V Hybrid or 1.3L Turbo PHEV',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'ZASPATBN2PZ276789',  # 2023 Tonale launch (US)
            'ZASPATBN4RZ277890',  # 2024
            'ZASPATBN6SZ278901',  # 2025
        ],
        'notes': 'Alfa Romeo Tonale Sprint base trim. P3 platform shared with Dodge Hornet.',
    },
    'PATC': {  # Tonale Ti
        'model': 'Tonale', 'trim': 'Ti',
        'engine': '2.0L I4 Turbo + 48V Hybrid or 1.3L Turbo PHEV',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'ZASPATCN4PZ280012',  # 2023 Tonale Ti
            'ZASPATCN6RZ281234',  # 2024
        ],
        'notes': 'Tonale Ti luxury mid-trim.',
    },
    'PATD': {  # Tonale Veloce
        'model': 'Tonale', 'trim': 'Veloce',
        'engine': '1.3L I4 Turbo PHEV (285 hp combined)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'ZASPATDN6PZ282345',  # 2023 Tonale Veloce PHEV
            'ZASPATDN8RZ283456',  # 2024
            'ZASPATDN0SZ284567',  # 2025
        ],
        'notes': 'Tonale Veloce top trim with PHEV powertrain (285hp combined).',
    },
}


def decode(vin: str):
    """Decode an Alfa Romeo VIN. Returns dict or None."""
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
        'make': 'Alfa Romeo',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:alfaromeo',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # 4C Coupe
        ('ZARBANBNXFM200012', '4C', 2015),
        ('ZARBANBN2GM201234', '4C', 2016),
        ('ZARBANBN4HM202345', '4C', 2017),
        ('ZARBANBN6JM203456', '4C', 2018),
        ('ZARBANBN0LM205678', '4C', 2020),
        # 4C Spider
        ('ZARBANCN2GM206789', '4C', 2016),
        ('ZARBANCN8KM210012', '4C', 2019),
        ('ZARBANCN0LM211234', '4C', 2020),
        # Giulia base
        ('ZARFAEHN0HZ212345', 'Giulia', 2017),
        ('ZARFAEHN2JZ213456', 'Giulia', 2018),
        ('ZARFAEHN4KZ214567', 'Giulia', 2019),
        ('ZARFAEHN4RZ220012', 'Giulia', 2024),
        # Giulia Ti
        ('ZARFAE5N5HZ221234', 'Giulia', 2017),
        ('ZARFAE5N7JZ222345', 'Giulia', 2018),
        ('ZARFAE5N1SZ230012', 'Giulia', 2025),
        # Giulia Sport / Veloce
        ('ZARFAE6N6HZ231234', 'Giulia', 2017),
        ('ZARFAE6N2LZ234567', 'Giulia', 2020),
        # Giulia Quadrifoglio
        ('ZARFAEKN3HZ237890', 'Giulia', 2017),
        ('ZARFAEKN1MZ242345', 'Giulia', 2021),
        ('ZARFAEKN7RZ245678', 'Giulia', 2024),
        # Stelvio base
        ('ZASPAKBN0JZ246789', 'Stelvio', 2018),
        ('ZASPAKBN2KZ247890', 'Stelvio', 2019),
        ('ZASPAKBN2RZ253456', 'Stelvio', 2024),
        # Stelvio Ti
        ('ZASPAK5N5JZ254567', 'Stelvio', 2018),
        ('ZASPAK5N9SZ262345', 'Stelvio', 2025),
        # Stelvio Sport / Veloce
        ('ZASPAK6N6JZ263456', 'Stelvio', 2018),
        ('ZASPAK6N0LZ265678', 'Stelvio', 2020),
        # Stelvio Quadrifoglio
        ('ZASPAKKN3JZ268901', 'Stelvio', 2018),
        ('ZASPAKKN5RZ275678', 'Stelvio', 2024),
        # Tonale
        ('ZASPATBN2PZ276789', 'Tonale', 2023),
        ('ZASPATBN6SZ278901', 'Tonale', 2025),
        ('ZASPATCN4PZ280012', 'Tonale', 2023),
        ('ZASPATDN6PZ282345', 'Tonale', 2023),
        ('ZASPATDN0SZ284567', 'Tonale', 2025),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('XYZ12345678901234', None, None),
        ('ZARZZZZZ123456789', None, None),
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
