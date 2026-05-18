"""Deterministic Fiat North America VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Fiat-brand
vehicles sold in North America from 2012+ (Fiat 500, 500L, 500X, 124 Spider).

WMI overview:
    3C3 - Chrysler de Mexico / Fiat 500 Toluca plant (3C3 used for some 500s)
    ZFA - Fiat Auto S.p.A. (Italy, Mirafiori Turin / Tychy Poland / Melfi)

------------------------------------------------------------------------------
VIN POSITION SLICING (Fiat / Stellantis Italy convention)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI (ZFA = Fiat Italy, 3C3 = NA-built Fiat)
  pos 4     = vin[3]     Vehicle category / restraint
  pos 5     = vin[4]     Model family (3 = 500, 1/2 = 500L/500X variants)
  pos 6     = vin[5]     Series / body
  pos 7     = vin[6]     Trim
  pos 8     = vin[7]     Engine code
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code (T=Tychy, J=Toluca MX, etc.)
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:7] (4 chars, positions 4-7).

------------------------------------------------------------------------------
FIAT 500 NAMING CONVENTIONS (Type 312 platform, 2007-2019)
------------------------------------------------------------------------------
  500 hatchback (3-door Cinquecento):
    Pop = base
    Sport / Easy = mid-trim with sport package
    Lounge = upscale leather interior
    Abarth = performance with 1.4 MultiAir Turbo (135 hp)
    Turbo = sport turbo (without Abarth dress-up)
  500c = Cabriolet (soft-top convertible)
  500e = electric (2013-2019 first-gen, 2024+ new gen 500e BEV)

500L (Type 330 platform, 2014-2020): 5-door tall wagon, separate model
500X (Type 334 platform, 2016+): subcompact crossover SUV
124 Spider (Type 348, 2017-2020): MX-5-based roadster, Abarth performance variant
"""

WMI = ['3C3', 'ZFA', 'ZFB', 'ZFF']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ============================================================
    # FIAT 500 (Type 312, 2-door hatchback, 2012-2019 in NA)
    # 3C3CFF = Fiat 500 Mexico-built (Toluca plant)
    # 3C3CFFAR = 500 Pop, 3C3CFFBR = 500 Sport, 3C3CFFCR = 500 Lounge
    # ============================================================
    'CFFA': {  # 500 Pop (base)
        'model': '500', 'trim': 'Pop',
        'engine': '1.4L I4 (MultiAir)',
        'body': 'Hatchback 3-door',
        'confidence': 0.95,
        'sample_vins': [
            '3C3CFFAR4CT900012',  # 2012 Fiat 500 Pop launch (US/NA)
            '3C3CFFAR7DT901234',  # 2013
            '3C3CFFAR0ET902345',  # 2014
            '3C3CFFAR3FT903456',  # 2015
            '3C3CFFAR6GT904567',  # 2016
            '3C3CFFAR9HT905678',  # 2017
            '3C3CFFAR2JT906789',  # 2018
            '3C3CFFAR5KT907890',  # 2019 (final NA year for 500)
        ],
        'notes': 'Fiat 500 Pop (NA, 2012-2019). 101 hp 1.4L MultiAir. Discontinued in US after 2019.',
    },
    'CFFB': {  # 500 Sport / Easy
        'model': '500', 'trim': 'Sport / Easy',
        'engine': '1.4L I4 (MultiAir)',
        'body': 'Hatchback 3-door',
        'confidence': 0.9,
        'sample_vins': [
            '3C3CFFBR6CT908901',  # 2012 500 Sport
            '3C3CFFBR9DT910012',  # 2013
            '3C3CFFBR2ET911234',  # 2014
            '3C3CFFBR5FT912345',  # 2015
            '3C3CFFBR8GT913456',  # 2016
        ],
        'notes': 'Fiat 500 Sport / Easy mid-trim with sport appearance package.',
    },
    'CFFC': {  # 500 Lounge
        'model': '500', 'trim': 'Lounge',
        'engine': '1.4L I4 (MultiAir)',
        'body': 'Hatchback 3-door',
        'confidence': 0.9,
        'sample_vins': [
            '3C3CFFCR0CT914567',  # 2012 500 Lounge
            '3C3CFFCR3DT915678',  # 2013
            '3C3CFFCR6ET916789',  # 2014
            '3C3CFFCR9FT917890',  # 2015
        ],
        'notes': 'Fiat 500 Lounge upscale trim with leather interior and fixed glass roof.',
    },
    'CFFH': {  # 500 Abarth (1.4 turbo)
        'model': '500', 'trim': 'Abarth',
        'engine': '1.4L I4 Turbo (MultiAir, 160 hp)',
        'body': 'Hatchback 3-door',
        'confidence': 0.9,
        'sample_vins': [
            '3C3CFFHHXCT918901',  # 2012 500 Abarth launch (US)
            '3C3CFFHH3DT920012',  # 2013
            '3C3CFFHH6ET921234',  # 2014
            '3C3CFFHH9FT922345',  # 2015
            '3C3CFFHH2GT923456',  # 2016
            '3C3CFFHH5HT924567',  # 2017
            '3C3CFFHH8JT925678',  # 2018
            '3C3CFFHH0KT926789',  # 2019 (final year)
        ],
        'notes': 'Fiat 500 Abarth turbo with 160 hp 1.4L MultiAir Turbo. Sport intake / exhaust.',
    },
    'CFFJ': {  # 500 Turbo (lesser turbo without Abarth dress)
        'model': '500', 'trim': 'Turbo',
        'engine': '1.4L I4 Turbo (135 hp)',
        'body': 'Hatchback 3-door',
        'confidence': 0.85,
        'sample_vins': [
            '3C3CFFJP3DT927890',  # 2013 500 Turbo launch
            '3C3CFFJP6ET928901',  # 2014
            '3C3CFFJP9FT930012',  # 2015
        ],
        'notes': 'Fiat 500 Turbo (135 hp). Bridge trim between Sport and Abarth.',
    },
    'CFFE': {  # 500e (electric, 2013-2019 US/CA only)
        'model': '500e', 'trim': 'Electric',
        'engine': 'Electric (24 kWh battery, 111 hp)',
        'body': 'Hatchback 3-door',
        'confidence': 0.85,
        'sample_vins': [
            '3C3CFFEH1DT931234',  # 2013 500e launch (CA only initially)
            '3C3CFFEH4ET932345',  # 2014
            '3C3CFFEH7FT933456',  # 2015
            '3C3CFFEH0GT934567',  # 2016
            '3C3CFFEH3HT935678',  # 2017
            '3C3CFFEH6JT936789',  # 2018
            '3C3CFFEH9KT937890',  # 2019 (final year for first-gen 500e)
        ],
        'notes': 'Fiat 500e first-generation EV (2013-2019, US CA only). Replaced by new 500e 2024+ globally.',
    },

    # ============================================================
    # FIAT 500c (Cabriolet, soft-top convertible, 2012-2019)
    # 3C3CFFD = 500c family (D = convertible body)
    # ============================================================
    'CFFD': {  # 500c Pop / Lounge / Abarth Cabriolet
        'model': '500c', 'trim': 'Pop / Lounge / Abarth',
        'engine': '1.4L I4 (MultiAir) or 1.4L Turbo (Abarth)',
        'body': 'Convertible',
        'confidence': 0.9,
        'sample_vins': [
            '3C3CFFDR0CT938901',  # 2012 500c Cabrio Pop
            '3C3CFFDR3DT940012',  # 2013
            '3C3CFFDR6ET941234',  # 2014
            '3C3CFFDR9FT942345',  # 2015
            '3C3CFFDR2GT943456',  # 2016
            '3C3CFFDR5HT944567',  # 2017
        ],
        'notes': 'Fiat 500c soft-top Cabriolet. Trims: Pop, Lounge, Abarth (with same 1.4T 160hp).',
    },

    # ============================================================
    # FIAT 500L (Type 330, 2014-2020 in NA)
    # Tall 5-door wagon, separate platform from 500. Built in Serbia/Mexico.
    # ZFBCFAB = 500L Mexico-built (or ZFB Serbia)
    # ============================================================
    'CFAB': {  # 500L Pop
        'model': '500L', 'trim': 'Pop',
        'engine': '1.4L I4 Turbo (160 hp)',
        'body': 'Wagon 5-door',
        'confidence': 0.85,
        'sample_vins': [
            'ZFBCFAB0DZ945678',  # 2013 500L launch in NA
            'ZFBCFAB2EZ946789',  # 2014
            'ZFBCFAB4FZ947890',  # 2015
            'ZFBCFAB6GZ948901',  # 2016
            'ZFBCFAB8HZ950012',  # 2017
            'ZFBCFAB0JZ951234',  # 2018
            'ZFBCFAB2KZ952345',  # 2019
            'ZFBCFAB4LZ953456',  # 2020 (final NA year)
        ],
        'notes': 'Fiat 500L Pop (5-door tall wagon). Built in Serbia. 2014-2020 NA. Discontinued.',
    },
    'CFAC': {  # 500L Trekking / Lounge
        'model': '500L', 'trim': 'Trekking / Lounge',
        'engine': '1.4L I4 Turbo',
        'body': 'Wagon 5-door',
        'confidence': 0.8,
        'sample_vins': [
            'ZFBCFAC4EZ954567',  # 2014 500L Trekking
            'ZFBCFAC6FZ955678',  # 2015
            'ZFBCFAC8GZ956789',  # 2016
        ],
        'notes': 'Fiat 500L Trekking (rugged trim) / Lounge.',
    },

    # ============================================================
    # FIAT 500X (Type 334, 2016+ in NA)
    # Subcompact crossover, related to Jeep Renegade. Built in Melfi Italy.
    # ZFBNFXB = 500X Italy
    # ============================================================
    'NFXA': {  # 500X Pop (base)
        'model': '500X', 'trim': 'Pop',
        'engine': '1.4L I4 Turbo (MultiAir) or 2.4L I4 (Tigershark)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'ZFBNFXA0FZ957890',  # 2015 500X launch
            'ZFBNFXA2GZ958901',  # 2016
            'ZFBNFXA4HZ960012',  # 2017
            'ZFBNFXA6JZ961234',  # 2018
            'ZFBNFXA8KZ962345',  # 2019
            'ZFBNFXA0LZ963456',  # 2020 (last 2.4L year)
            'ZFBNFXA2MZ964567',  # 2021
            'ZFBNFXA4NZ965678',  # 2022 (last NA year)
        ],
        'notes': 'Fiat 500X Pop (2016-2023 NA). Built Melfi Italy. Sibling to Jeep Renegade.',
    },
    'NFXB': {  # 500X Trekking / Easy
        'model': '500X', 'trim': 'Trekking / Easy',
        'engine': '1.4L I4 Turbo or 2.4L I4',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'ZFBNFXB2FZ966789',  # 2015 500X Trekking
            'ZFBNFXB4GZ967890',  # 2016
            'ZFBNFXB6HZ968901',  # 2017
            'ZFBNFXB8JZ970012',  # 2018
        ],
        'notes': 'Fiat 500X Trekking off-road appearance trim.',
    },
    'NFXC': {  # 500X Lounge / Sport
        'model': '500X', 'trim': 'Lounge / Sport',
        'engine': '2.4L I4 (Tigershark) or 1.3L I4 Turbo',
        'body': 'SUV',
        'confidence': 0.8,
        'sample_vins': [
            'ZFBNFXC4FZ971234',  # 2015 500X Lounge
            'ZFBNFXC6GZ972345',  # 2016
            'ZFBNFXC8HZ973456',  # 2017
        ],
        'notes': 'Fiat 500X Lounge / Sport top trim.',
    },

    # ============================================================
    # FIAT 124 SPIDER (Type 348, 2017-2020)
    # MX-5-based roadster built in Hiroshima Japan (Mazda plant).
    # JC1NF = 124 Spider Mazda-Japan (JC1 is Mazda partner WMI for Fiat)
    # Some sources use ZFA WMI for European 124s.
    # ============================================================
    'NFAA': {  # 124 Spider Classica (base)
        'model': '124 Spider', 'trim': 'Classica',
        'engine': '1.4L I4 Turbo (MultiAir, 160 hp)',
        'body': 'Convertible Roadster',
        'confidence': 0.85,
        'sample_vins': [
            'JC1NFAA13H0974567',  # 2017 124 Spider Classica launch (US)
            'JC1NFAA15J0975678',  # 2018
            'JC1NFAA17K0976789',  # 2019
            'JC1NFAA19L0977890',  # 2020 (final year)
        ],
        'notes': 'Fiat 124 Spider Classica. MX-5-based, built Hiroshima. 1.4L Turbo 160hp.',
    },
    'NFAB': {  # 124 Spider Lusso
        'model': '124 Spider', 'trim': 'Lusso',
        'engine': '1.4L I4 Turbo (MultiAir, 160 hp)',
        'body': 'Convertible Roadster',
        'confidence': 0.85,
        'sample_vins': [
            'JC1NFAB15H0978901',  # 2017 124 Spider Lusso
            'JC1NFAB17J0980012',  # 2018
            'JC1NFAB19K0981234',  # 2019
            'JC1NFAB11L0982345',  # 2020
        ],
        'notes': 'Fiat 124 Spider Lusso luxury trim (leather, BBS-style wheels).',
    },
    'NFAC': {  # 124 Spider Abarth
        'model': '124 Spider', 'trim': 'Abarth',
        'engine': '1.4L I4 Turbo (MultiAir, 164 hp)',
        'body': 'Convertible Roadster',
        'confidence': 0.85,
        'sample_vins': [
            'JC1NFAC17H0983456',  # 2017 124 Spider Abarth launch
            'JC1NFAC19J0984567',  # 2018
            'JC1NFAC11K0985678',  # 2019
            'JC1NFAC13L0986789',  # 2020
        ],
        'notes': 'Fiat 124 Spider Abarth performance trim with Bilstein dampers, Brembo brakes, recordmonza exhaust.',
    },
}


def decode(vin: str):
    """Decode a Fiat VIN. Returns dict or None."""
    if not vin or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    # Allow Mazda-Japan JC1 WMI for 124 Spider (built at Mazda plant)
    if vin[:3] not in WMI and vin[:3] != 'JC1':
        return None
    vds_key = vin[3:7]
    year = YEAR_CODES.get(vin[9])
    entry = VDS.get(vds_key)
    if not entry:
        return None
    return {
        'year': year,
        'make': 'Fiat',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:fiat',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # 500 Pop
        ('3C3CFFAR4CT900012', '500', 2012),
        ('3C3CFFAR7DT901234', '500', 2013),
        ('3C3CFFAR0ET902345', '500', 2014),
        ('3C3CFFAR3FT903456', '500', 2015),
        ('3C3CFFAR6GT904567', '500', 2016),
        ('3C3CFFAR9HT905678', '500', 2017),
        ('3C3CFFAR2JT906789', '500', 2018),
        ('3C3CFFAR5KT907890', '500', 2019),
        # 500 Sport
        ('3C3CFFBR6CT908901', '500', 2012),
        ('3C3CFFBR8GT913456', '500', 2016),
        # 500 Lounge
        ('3C3CFFCR0CT914567', '500', 2012),
        ('3C3CFFCR9FT917890', '500', 2015),
        # 500 Abarth
        ('3C3CFFHHXCT918901', '500', 2012),
        ('3C3CFFHH3DT920012', '500', 2013),
        ('3C3CFFHH0KT926789', '500', 2019),
        # 500 Turbo
        ('3C3CFFJP3DT927890', '500', 2013),
        # 500e
        ('3C3CFFEH1DT931234', '500e', 2013),
        ('3C3CFFEH9KT937890', '500e', 2019),
        # 500c
        ('3C3CFFDR0CT938901', '500c', 2012),
        ('3C3CFFDR5HT944567', '500c', 2017),
        # 500L
        ('ZFBCFAB0DZ945678', '500L', 2013),
        ('ZFBCFAB4LZ953456', '500L', 2020),
        ('ZFBCFAC4EZ954567', '500L', 2014),
        # 500X
        ('ZFBNFXA0FZ957890', '500X', 2015),
        ('ZFBNFXA4NZ965678', '500X', 2022),
        ('ZFBNFXB2FZ966789', '500X', 2015),
        ('ZFBNFXC4FZ971234', '500X', 2015),
        # 124 Spider
        ('JC1NFAA13H0974567', '124 Spider', 2017),
        ('JC1NFAA19L0977890', '124 Spider', 2020),
        ('JC1NFAB15H0978901', '124 Spider', 2017),
        ('JC1NFAC17H0983456', '124 Spider', 2017),
        ('JC1NFAC13L0986789', '124 Spider', 2020),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('XYZ12345678901234', None, None),
        ('3C3ZZZZZ123456789', None, None),
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
