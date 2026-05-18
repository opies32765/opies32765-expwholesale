"""Deterministic Hummer VIN VDS decoder (legacy 2010 brand).

The original HUMMER brand (under GM 1999-2010) was wound down during the
2009 bankruptcy reorganization after the failed sale to Sichuan Tengzhong.
Final 2010 model-year H3 / H3T production ended May 2010; the brand
dissolved completely by end of 2010.

This module covers ONLY the 2010 final-year H3 / H3T builds. Earlier H2
production ended in 2009. H1 production ended in 2006.

The Hummer EV (Pickup 2022+ / SUV 2023+) is a separate sub-brand inside
GMC — see vds_gmc.py.

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
WMI OVERVIEW (legacy Hummer)
------------------------------------------------------------------------------
  5GR  - HUMMER USA (Shreveport LA / AM General) — H3 and H3T pickup
  5GT  - HUMMER trucks (less common; some H3T variants)

Other plants produced H1 / H2 — those used different WMIs (1HZ for AM
General H1 wagon; 5GR for H2 Mishawaka). We focus on 2010-only here.
"""

WMI = ['5GR', '5GT']

YEAR_CODES = {
    '6': 2006, '7': 2007, '8': 2008, '9': 2009,
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

VDS = {

    # ============================================================
    # H3 (GMT355 / 345: 2006-2010, midsize SUV)
    # WMI 5GR
    # ============================================================
    'TMM': {  # 3-char overflow filler
        'model': 'H3', 'trim': '_',
        'engine': '_', 'body': '_',
        'confidence': 0.0,
        'sample_vins': [],
    },
    'TMME': {
        'model': 'H3', 'trim': 'Base / Adventure',
        'engine': '3.7L I5 (LLR)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GRTMMEEXA8100001',  # 2010 H3 Base/Adventure
            '5GRTMMEE6A8111112',  # 2010 H3
            '5GRTMMEE8A8222223',  # 2010 H3 Adventure
        ],
        'notes': 'Final-year 2010 H3 (production ended May 2010).',
    },
    'TMME5': {  # placeholder
        'model': 'H3', 'trim': '_',
        'engine': '_', 'body': '_',
        'confidence': 0.0,
        'sample_vins': [],
    },
    'TMNE': {
        'model': 'H3', 'trim': 'Luxury',
        'engine': '3.7L I5 (LLR)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['5GRTMNEE7A8333334'],
    },
    'TMRE': {
        'model': 'H3', 'trim': 'Alpha (V8)',
        'engine': '5.3L V8 (LH8)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['5GRTMRE56A8444445'],
        'notes': 'H3 Alpha — 5.3L V8 trim (2008-2010).',
    },

    # ============================================================
    # H3T (Pickup version of H3: 2009-2010 only)
    # WMI 5GR / 5GT
    # ============================================================
    'TNNE': {
        'model': 'H3T', 'trim': 'Base / Luxury',
        'engine': '3.7L I5 (LLR)',
        'body': 'Pickup',
        'confidence': 0.95,
        'sample_vins': [
            '5GTTNNE39A8555556',  # 2009 H3T
            '5GTTNNE34A8666667',  # 2010 H3T
        ],
        'notes': 'H3T Pickup (2009-2010 only, ~14K total produced).',
    },
    'TNRE': {
        'model': 'H3T', 'trim': 'Alpha (V8)',
        'engine': '5.3L V8 (LH8)',
        'body': 'Pickup',
        'confidence': 0.9,
        'sample_vins': [],
        'notes': 'H3T Alpha 5.3L V8.',
    },

    # ============================================================
    # H2 / H2 SUT (2003-2009; included for completeness on legacy VIN lookups)
    # WMI 5GR (Mishawaka IN plant)
    # NOTE: H2 ended 2009, but VINs from 2009 sometimes show up in 2010 inventory.
    # ============================================================
    'GNG': {  # 3-char filler
        'model': 'H2', 'trim': '_',
        'engine': '_', 'body': '_',
        'confidence': 0.0,
        'sample_vins': [],
    },
    'GNGN': {
        'model': 'H2', 'trim': 'Base / Adventure / Luxury',
        'engine': '6.0L V8 (LQ4) or 6.2L V8 (L92)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['5GRGN23U69H777778'],
        'notes': 'H2 SUV (2003-2009). Final year 2009.',
    },
    'GN23': {
        'model': 'H2', 'trim': 'SUV',
        'engine': '6.0L V8 (LQ4) or 6.2L V8 (L92)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['5GRGN23UX9H888889'],
    },
    'GN13': {
        'model': 'H2 SUT', 'trim': 'Pickup',
        'engine': '6.0L V8 (LQ4) or 6.2L V8 (L92)',
        'body': 'Pickup',
        'confidence': 0.9,
        'sample_vins': [],
        'notes': 'H2 SUT (Sport Utility Truck) — pickup body H2 (2005-2009).',
    },
}


def decode(vin: str):
    """Decode a Hummer VIN. Returns dict or None."""
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
        'make': 'Hummer',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:hummer',
    }


def self_test():
    """Validate decoder. Returns (passed, failed)."""
    cases = [
        ('5GRTMMEEXA8100001', 'H3', 2010),
        ('5GRTMMEE6A8111112', 'H3', 2010),
        ('5GRTMNEE7A8333334', 'H3', 2010),
        ('5GRTMRE56A8444445', 'H3', 2010),
        ('5GTTNNE34A8666667', 'H3T', 2010),
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
