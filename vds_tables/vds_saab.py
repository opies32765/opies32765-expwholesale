"""Deterministic Saab VIN VDS decoder (legacy GM-era).

GM sold Saab to Spyker Cars in early 2010 after failed talks with Koenigsegg.
Saab Automobile filed bankruptcy in Dec 2011. This module covers only the
GM-era models that overlapped with the 2010-2011 ownership transition:

  - 9-3 (Sedan/SportCombi/Convertible) - built through 2012 MY in low volumes
  - 9-5 (Gen 2 Sedan launched 2010 MY) - built through 2011
  - 9-7X (mid-size SUV based on GMC Envoy) - discontinued 2009 mostly

After Dec 2011, the brand was sold to NEVS (National Electric Vehicle Sweden);
no GM-era Saab production after 2011 model year.

------------------------------------------------------------------------------
GM VIN POSITION SLICING
------------------------------------------------------------------------------
  pos 1-3   WMI
  pos 4     Restraint
  pos 5     Carline / model (3 = 9-3, 5 = 9-5)
  pos 6-7   Body / trim
  pos 8     Engine
  pos 9     Check digit
  pos 10    Model year
  pos 11    Plant
  pos 12-17 Serial

VDS key = vin[3:7] (4 chars).

------------------------------------------------------------------------------
WMI OVERVIEW (Saab)
------------------------------------------------------------------------------
  YS3  - Saab AB / Saab Automobile AB (Trollhattan, Sweden)
"""

WMI = ['YS3']

YEAR_CODES = {
    '6': 2006, '7': 2007, '8': 2008, '9': 2009,
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

VDS = {

    # ============================================================
    # 9-3 (Epsilon: 2003-2012)
    # WMI YS3
    # ============================================================
    'FB49': {
        'model': '9-3', 'trim': 'Linear / 2.0T Sedan',
        'engine': '2.0L I4 Turbo (LK9 / B207E)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['YS3FB49S2A1100001'],
        'notes': '9-3 Sport Sedan (2003-2012). Final year for most trims 2011.',
    },
    'FB45': {
        'model': '9-3', 'trim': '2.0T Sedan',
        'engine': '2.0L I4 Turbo (B207E)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            'YS3FB45S271100001',  # 2007 9-3 2.0T
            'YS3FB45SX91100002',  # 2009 9-3 2.0T
            'YS3FB45SXA1111112',  # 2010 9-3 2.0T
        ],
    },
    'FB46': {
        'model': '9-3', 'trim': 'Aero Sedan',
        'engine': '2.8L V6 Turbo (B284 / LP9)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['YS3FB46S0A1222223'],
        'notes': '9-3 Aero V6 Turbo 280 hp.',
    },
    'FH49': {
        'model': '9-3 SportCombi', 'trim': '2.0T Wagon',
        'engine': '2.0L I4 Turbo (B207E)',
        'body': 'Wagon',
        'confidence': 0.95,
        'sample_vins': ['YS3FH49S6A1333334'],
        'notes': '9-3 SportCombi Wagon (2006-2012).',
    },
    'FH45': {
        'model': '9-3 SportCombi', 'trim': '2.0T Wagon',
        'engine': '2.0L I4 Turbo (B207E)',
        'body': 'Wagon',
        'confidence': 0.95,
        'sample_vins': ['YS3FH45S2B1444445'],
    },
    'FH46': {
        'model': '9-3 SportCombi', 'trim': 'Aero Wagon V6',
        'engine': '2.8L V6 Turbo (B284 / LP9)',
        'body': 'Wagon',
        'confidence': 0.9,
        'sample_vins': [],
    },
    'FD49': {
        'model': '9-3', 'trim': '2.0T Convertible',
        'engine': '2.0L I4 Turbo (B207E)',
        'body': 'Convertible',
        'confidence': 0.95,
        'sample_vins': ['YS3FD49Y4A1555556'],
        'notes': '9-3 Convertible (2004-2011).',
    },
    'FD45': {
        'model': '9-3', 'trim': '2.0T Convertible',
        'engine': '2.0L I4 Turbo (B207E)',
        'body': 'Convertible',
        'confidence': 1.0,
        'sample_vins': [
            'YS3FD45Y691666667',  # 2009 9-3 Convertible
            'YS3FD45Y1A1777778',  # 2010 9-3 Convertible
            'YS3FD45Y8B1888889',  # 2011 9-3 Convertible
        ],
    },
    'FD46': {
        'model': '9-3', 'trim': 'Aero Convertible V6',
        'engine': '2.8L V6 Turbo (B284 / LP9)',
        'body': 'Convertible',
        'confidence': 0.9,
        'sample_vins': [],
    },

    # ============================================================
    # 9-5 (Gen 2 NG: 2010-2011 only — launched late, brand died early)
    # WMI YS3
    # ============================================================
    'GR45': {
        'model': '9-5', 'trim': '2.0T Sedan (Gen 2)',
        'engine': '2.0L I4 Turbo (LDK / A20NHT)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            'YS3GR45BBA1999990',  # 2010 9-5 2.0T (launch)
            'YS3GR45BBB1000001',  # 2011 9-5 2.0T
        ],
        'notes': 'Gen 2 9-5 Sedan (2010-2011 only). Saab brand collapsed Dec 2011.',
    },
    'GR46': {
        'model': '9-5', 'trim': 'Aero V6 (Gen 2)',
        'engine': '2.8L V6 Turbo (LP9)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['YS3GR46BBA1111112'],
        'notes': 'Gen 2 9-5 Aero V6 — top trim, very limited production.',
    },
    'GS45': {
        'model': '9-5 SportCombi', 'trim': 'Wagon (Gen 2)',
        'engine': '2.0L I4 Turbo (LDK)',
        'body': 'Wagon',
        'confidence': 0.85,
        'sample_vins': [],
        'notes': '9-5 SportCombi Gen 2 — launched in Europe 2011 but never reached US.',
    },

    # ============================================================
    # 9-7X (GMT360 mid-size SUV: 2005-2009; very limited 2010 carryover)
    # WMI 1G5 (Moraine OH — same plant as Chevy TrailBlazer)
    # NOTE: 9-7X uses 1G5 WMI not YS3 due to US plant. We catch it here for
    # the brand association even though the WMI lookup goes elsewhere.
    # ============================================================
    'B0FE': {
        'model': '9-7X', 'trim': 'Aero / 4.2i',
        'engine': '4.2L I6 (LL8) or 5.3L V8 (LH6)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [],
        'notes': '9-7X SUV (2005-2009). Built in Moraine OH alongside TrailBlazer. '
                 'Final year 2009; minimal 2010 inventory before brand sale.',
    },
}


def decode(vin: str):
    """Decode a Saab VIN. Returns dict or None."""
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
        'make': 'Saab',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:saab',
    }


def self_test():
    """Validate decoder. Returns (passed, failed)."""
    cases = [
        ('YS3FB45S271100001', '9-3', 2007),
        ('YS3FB45SXA1111112', '9-3', 2010),
        ('YS3FB46S0A1222223', '9-3', 2010),
        ('YS3FH49S6A1333334', '9-3 SportCombi', 2010),
        ('YS3FD45Y1A1777778', '9-3', 2010),
        ('YS3FD45Y8B1888889', '9-3', 2011),
        ('YS3GR45BBA1999990', '9-5', 2010),
        ('YS3GR45BBB1000001', '9-5', 2011),
        ('YS3GR46BBA1111112', '9-5', 2010),
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
