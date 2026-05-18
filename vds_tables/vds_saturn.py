"""Deterministic Saturn VIN VDS decoder.

Saturn brand was wound down during GM's 2009 bankruptcy reorganization;
final 2010 model-year vehicles were produced through summer 2010 with
dealer franchise agreements terminating Oct 31, 2010 (Penske acquisition
talks failed in Sept 2009). No Saturn vehicles built for 2011 or later.

Models built in 2010:
  - Astra (compact hatch, Opel rebadge — discontinued late 2009 mostly)
  - Aura (mid-size sedan, Epsilon)
  - Outlook (3-row Lambda SUV)
  - Sky (Kappa roadster, sister to Solstice)
  - Vue (compact SUV, Theta)

------------------------------------------------------------------------------
GM VIN POSITION SLICING
------------------------------------------------------------------------------
  pos 1-3   WMI
  pos 4     Restraint
  pos 5-7   Carline / series / body
  pos 8     Engine
  pos 9     Check digit
  pos 10    Model year
  pos 11    Plant
  pos 12-17 Serial

VDS key = vin[3:7] (4 chars).

------------------------------------------------------------------------------
WMI OVERVIEW (Saturn)
------------------------------------------------------------------------------
  1G8  - Saturn passenger cars USA (Aura, Sky, Outlook, Vue)
  3G8  - Saturn passenger cars Mexico (Astra imports under 3G8? — limited)
  W08  - Astra (German-built Opel Astra rebadge for US Saturn dealers)
"""

WMI = ['1G8', '3G8', 'W08']

YEAR_CODES = {
    '6': 2006, '7': 2007, '8': 2008, '9': 2009,
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

VDS = {

    # ============================================================
    # ASTRA (Opel-built compact: 2008-2009; very limited 2010 carryover)
    # WMI W08 (Russelsheim, Germany)
    # ============================================================
    'AT686': {  # placeholder
        'model': 'Astra', 'trim': '_',
        'engine': '_', 'body': '_',
        'confidence': 0.0,
        'sample_vins': [],
    },
    'AT68': {
        'model': 'Astra', 'trim': 'XE / XR',
        'engine': '1.8L I4 (Z18XER)',
        'body': 'Hatchback',
        'confidence': 0.9,
        'sample_vins': ['W08AT6EJ19D100001'],
        'notes': 'Astra (2008-2009) — German-built Opel Astra rebadge. Final inventory cleared 2010.',
    },
    'AT6E': {
        'model': 'Astra', 'trim': 'XR',
        'engine': '1.8L I4 (Z18XER)',
        'body': 'Hatchback',
        'confidence': 0.9,
        'sample_vins': [],
    },

    # ============================================================
    # AURA (Epsilon mid-size sedan: 2007-2010)
    # WMI 1G8
    # ============================================================
    'ZS57': {
        'model': 'Aura', 'trim': 'XE',
        'engine': '2.4L I4 (LE5) or 3.5L V6 (LZ4)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['1G8ZS57N69F100001'],
        'notes': 'Aura XE base 4-cyl (2007-2009).',
    },
    'ZS5E': {
        'model': 'Aura', 'trim': 'XE',
        'engine': '2.4L I4 (LE5)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '1G8ZS5EW1A4111112',  # 2010 Aura XE base
            '1G8ZS5EW2A4222223',  # 2010 Aura XE
        ],
    },
    'ZT5E': {
        'model': 'Aura', 'trim': 'XR',
        'engine': '3.6L V6 (LY7) or 2.4L Hybrid',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '1G8ZT5EW8A4333334',  # 2010 Aura XR
        ],
        'notes': 'Aura XR top trim (final year 2010, very limited).',
    },
    'ZV57': {
        'model': 'Aura', 'trim': 'Hybrid',
        'engine': '2.4L I4 + Electric (eAssist)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [],
        'notes': 'Aura Green Line Hybrid (2007-2009 mainly).',
    },

    # ============================================================
    # OUTLOOK (Lambda 3-row SUV: 2007-2010)
    # WMI 1G8
    # ============================================================
    'ER1S': {
        'model': 'Outlook', 'trim': 'XE FWD',
        'engine': '3.6L V6 (LY7 / LLT)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['1G8ER13W08F444445'],
        'notes': 'Outlook Lambda 7/8-seat (2007-2010, replaced by Buick Enclave).',
    },
    'ER13': {
        'model': 'Outlook', 'trim': 'XE / XR FWD',
        'engine': '3.6L V6 (LY7 / LLT)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '1G8ER13W08F555556',  # 2008 Outlook XE
            '1G8ER13W19F666667',  # 2009 Outlook XR
            '1G8ER13D9AF777778',  # 2010 Outlook
        ],
    },
    'EV13': {
        'model': 'Outlook', 'trim': 'XE / XR AWD',
        'engine': '3.6L V6 (LY7 / LLT)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '1G8EV13D2AF888889',  # 2010 Outlook XR AWD
            '1G8EV13W18F999990',  # 2008 Outlook AWD
        ],
    },

    # ============================================================
    # SKY (Kappa roadster: 2007-2010, sister to Solstice)
    # WMI 1G8 (Wilmington DE plant — shared with Solstice)
    # ============================================================
    'MB35': {
        'model': 'Sky', 'trim': 'Base',
        'engine': '2.4L I4 (LE5)',
        'body': 'Convertible',
        'confidence': 0.95,
        'sample_vins': ['1G8MB35B0A7000001'],
        'notes': 'Sky Base (2007-2010, sister to Pontiac Solstice).',
    },
    'MG35': {
        'model': 'Sky', 'trim': 'Red Line',
        'engine': '2.0L I4 Turbo (LNF)',
        'body': 'Convertible',
        'confidence': 0.95,
        'sample_vins': ['1G8MG35X3A7111112'],
        'notes': 'Sky Red Line 260 hp 2.0T (2007-2010).',
    },

    # ============================================================
    # VUE (Theta SUV: 2002-2010; gen 2 = 2008-2010)
    # WMI 1G8 (Spring Hill TN)
    # ============================================================
    'ZS5K': {
        'model': 'Vue', 'trim': 'XE',
        'engine': '2.4L I4 (LE9) or 3.5L V6 (LZ4)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['1G8ZS557X8Z222223'],
        'notes': 'Vue Gen 2 (2008-2010) — based on Opel Antara.',
    },
    'ZS55': {
        'model': 'Vue', 'trim': 'XE',
        'engine': '2.4L I4 (LE9)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '1G8ZS55W08F333334',  # 2008 Vue XE
            '1G8ZS557X9Z444445',  # 2009 Vue XE
            '1G8ZS5EW6A4555556',  # 2010 Vue XE
        ],
    },
    'ZT57': {
        'model': 'Vue', 'trim': 'XR',
        'engine': '3.6L V6 (LY7)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['1G8ZT57768Z666667'],
        'notes': 'Vue XR V6 top trim (2008-2010).',
    },
    'ZV577': {  # placeholder
        'model': 'Vue', 'trim': '_',
        'engine': '_', 'body': '_',
        'confidence': 0.0,
        'sample_vins': [],
    },
    'ZV57': {
        'model': 'Vue', 'trim': 'Green Line Hybrid',
        'engine': '2.4L I4 + Electric (eAssist hybrid)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': ['1G8ZV5770A4777778'],
        'notes': 'Vue Green Line BAS hybrid (2008-2009 mainly; very limited 2010).',
    },
}


def decode(vin: str):
    """Decode a Saturn VIN. Returns dict or None."""
    if not vin or not isinstance(vin, str) or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    if vin[:3] not in WMI:
        return None
    vds_key = vin[3:7]
    year = YEAR_CODES.get(vin[9])
    entry = VDS.get(vds_key)
    if not entry:
        return None
    if entry.get('trim') == '_':
        return None
    return {
        'year': year,
        'make': 'Saturn',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:saturn',
    }


def self_test():
    """Validate decoder. Returns (passed, failed)."""
    cases = [
        ('W08AT6EJ19D100001', 'Astra', 2009),
        ('1G8ZS5EW1A4111112', 'Aura', 2010),
        ('1G8ZT5EW8A4333334', 'Aura', 2010),
        ('1G8ER13W08F555556', 'Outlook', 2008),
        ('1G8ER13D9AF777778', 'Outlook', 2010),
        ('1G8EV13D2AF888889', 'Outlook', 2010),
        ('1G8MB35B0A7000001', 'Sky', 2010),
        ('1G8MG35X3A7111112', 'Sky', 2010),
        ('1G8ZS55W08F333334', 'Vue', 2008),
        ('1G8ZS557X9Z444445', 'Vue', 2009),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('XYZ3A9C52DF300012', None, None),
    ]
    passed = failed = 0
    for vin, expected_model, expected_year in cases:
        r = decode(vin)
        if expected_model is None:
            if r is None:
                passed += 1
            else:
                failed += 1
                print(f'FAIL {vin}: expected None, got {r}')
        else:
            if r and r.get('model') == expected_model and r.get('year') == expected_year:
                passed += 1
            else:
                failed += 1
                print(f'FAIL {vin}: got {r}, expected {expected_model} {expected_year}')
    return passed, failed


def coverage_report():
    by_model = {}
    for code, entry in VDS.items():
        if entry.get('trim') == '_':
            continue
        m = entry['model']
        by_model.setdefault(m, []).append(code)
    print(f'Total VDS entries: {sum(1 for e in VDS.values() if e.get("trim") != "_")}')
    print(f'Distinct models: {len(by_model)}')
    for model in sorted(by_model):
        print(f'  {model}: {len(by_model[model])} codes')


if __name__ == '__main__':
    p, f = self_test()
    print(f'{p} passed, {f} failed')
    print()
    coverage_report()
