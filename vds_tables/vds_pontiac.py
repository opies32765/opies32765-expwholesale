"""Deterministic Pontiac VIN VDS decoder.

Pontiac was wound down during GM's 2009 bankruptcy reorganization; final 2010
model-year vehicles were produced through summer 2010 with dealer franchise
agreements terminating by Oct 31, 2010. No Pontiac vehicles built for 2011 or
later.

Models built in 2010:
  - G3 (subcompact hatch, rebadged Chevy Aveo)
  - G5 (compact coupe, rebadged Chevy Cobalt G3)
  - G6 (mid-size sedan/coupe/convertible, final year)
  - Vibe (compact hatch, Toyota Matrix rebadge built at NUMMI)
  - G8 (RWD V6/V8 sedan, Holden Commodore rebadge — discontinued early 2010)
  - Solstice (roadster — production ended mid-2010)

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
WMI OVERVIEW (Pontiac)
------------------------------------------------------------------------------
  1G2  - Pontiac passenger cars USA
  2G2  - Pontiac passenger cars Canada
  6G2  - Pontiac G8 (Australia-built Holden, imported)

NUMMI-built Vibe carries Toyota WMI 5Y2 typically.
"""

WMI = ['1G2', '2G2', '6G2', '5Y2']

YEAR_CODES = {
    '6': 2006, '7': 2007, '8': 2008, '9': 2009,
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

VDS = {

    # ============================================================
    # G3 (Aveo-based subcompact: 2009-2010)
    # WMI 1G2 / KL1 (Korea-built imports)
    # ============================================================
    'TB66': {
        'model': 'G3', 'trim': 'Base / 1SA',
        'engine': '1.6L I4 (LXV)',
        'body': 'Hatchback',
        'confidence': 0.9,
        'sample_vins': ['1G2TB66E29L100001'],
        'notes': 'G3 Hatchback (2009-2010) — rebadged Chevy Aveo5.',
    },

    # ============================================================
    # G5 (Cobalt-based compact coupe: 2007-2010)
    # WMI 1G2
    # ============================================================
    'AL1S': {
        'model': 'G5', 'trim': 'Base / GT',
        'engine': '2.2L I4 (LE5)',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            '1G2AL15F2A7100001',  # 2010 G5 base
            '1G2AL15F8A7111112',  # 2010 G5 GT
        ],
        'notes': 'G5 Coupe (2007-2010, final year 2010) — rebadged Cobalt Coupe.',
    },
    'AL15': {
        'model': 'G5', 'trim': 'Base / GT',
        'engine': '2.2L I4 (LE5)',
        'body': 'Coupe',
        'confidence': 1.0,
        'sample_vins': [
            '1G2AL15F2A7100001',  # 2010 G5
            '1G2AL15F4A7222223',
        ],
    },

    # ============================================================
    # G6 (Epsilon: 2005-2010, sedan/coupe/convertible)
    # WMI 1G2
    # ============================================================
    'ZA5E': {
        'model': 'G6', 'trim': 'Base / Value Leader',
        'engine': '2.4L I4 (LE9) or 3.5L V6 (LZ4)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '1G2ZA5E07A4100001',  # 2010 G6 Sedan base
            '1G2ZA5E08A4111112',
        ],
        'notes': 'G6 Sedan final year 2010.',
    },
    'ZB5E': {
        'model': 'G6', 'trim': 'SE',
        'engine': '2.4L I4 (LE9)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['1G2ZB5E08A4222223'],
    },
    'ZC5E': {
        'model': 'G6', 'trim': 'GT',
        'engine': '3.5L V6 (LZ4) or 3.6L V6 (LY7)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['1G2ZC5E03A4333334'],
    },
    'ZG1G': {
        'model': 'G6', 'trim': 'GT Convertible',
        'engine': '3.5L V6 (LZ4)',
        'body': 'Convertible',
        'confidence': 0.9,
        'sample_vins': [],
        'notes': 'G6 retractable hardtop convertible (2006-2009; very limited 2010 inventory).',
    },
    'ZH1L': {
        'model': 'G6', 'trim': 'GT Coupe',
        'engine': '3.5L V6 (LZ4)',
        'body': 'Coupe',
        'confidence': 0.85,
        'sample_vins': [],
        'notes': 'G6 Coupe ended 2009 for most trims.',
    },

    # ============================================================
    # VIBE (NUMMI / Toyota Matrix rebadge: 2003-2010)
    # WMI 5Y2 (NUMMI plant, Fremont CA — shared with Toyota)
    # ============================================================
    'SR6E': {
        'model': 'Vibe', 'trim': 'Base / 1.8L',
        'engine': '1.8L I4 (Toyota 2ZR-FE)',
        'body': 'Hatchback',
        'confidence': 0.95,
        'sample_vins': ['5Y2SR6E69A7444445'],
        'notes': 'Vibe Base 1.8L (Toyota engine).',
    },
    'SR6S': {
        'model': 'Vibe', 'trim': '1.8L',
        'engine': '1.8L I4 (Toyota 2ZR-FE)',
        'body': 'Hatchback',
        'confidence': 0.95,
        'sample_vins': ['5Y2SR6S07A7555556'],
    },
    'SP6E': {
        'model': 'Vibe', 'trim': '2.4L GT',
        'engine': '2.4L I4 (Toyota 2AZ-FE)',
        'body': 'Hatchback',
        'confidence': 0.9,
        'sample_vins': [],
        'notes': 'Vibe GT 2.4L AWD optional (final year 2010).',
    },
    'SP6S': {
        'model': 'Vibe', 'trim': '2.4L',
        'engine': '2.4L I4 (Toyota 2AZ-FE)',
        'body': 'Hatchback',
        'confidence': 0.85,
        'sample_vins': [],
    },

    # ============================================================
    # G8 (Zeta RWD sedan: 2008-2009 mainly; some early 2010 inventory)
    # WMI 6G2 (Australia, GM Holden, Adelaide)
    # ============================================================
    'EC58': {
        'model': 'G8', 'trim': 'Base / GT',
        'engine': '3.6L V6 (LY7) or 6.0L V8 (L76)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [],
        'notes': 'G8 final year 2009; very limited 2010 inventory before brand wind-down.',
    },
    'EW57': {
        'model': 'G8', 'trim': 'GXP',
        'engine': '6.2L V8 (LS3)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': ['6G2EW57Y19L666667'],
        'notes': 'G8 GXP 415 hp LS3 (2009 only).',
    },

    # ============================================================
    # SOLSTICE (Kappa roadster: 2006-2010)
    # WMI 1G2 (Wilmington DE)
    # ============================================================
    'MB35': {
        'model': 'Solstice', 'trim': 'Base / Premium',
        'engine': '2.4L I4 (LE5)',
        'body': 'Convertible',
        'confidence': 0.95,
        'sample_vins': ['1G2MB35B8A7777778'],
        'notes': 'Solstice Base (2006-2010). Plant ended mid-2010.',
    },
    'MG35': {
        'model': 'Solstice', 'trim': 'GXP',
        'engine': '2.0L I4 Turbo (LNF)',
        'body': 'Convertible',
        'confidence': 0.95,
        'sample_vins': ['1G2MG35X1A7888889'],
        'notes': 'Solstice GXP 260 hp 2.0T (2007-2010).',
    },
    'MM35': {
        'model': 'Solstice Coupe', 'trim': 'Targa',
        'engine': '2.4L I4 (LE5) or 2.0L Turbo (LNF)',
        'body': 'Coupe',
        'confidence': 0.85,
        'sample_vins': [],
        'notes': 'Solstice Coupe (Targa removable top) — limited 2009-2010, ~1100 produced.',
    },
}


def decode(vin: str):
    """Decode a Pontiac VIN. Returns dict or None."""
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
        'make': 'Pontiac',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:pontiac',
    }


def self_test():
    """Validate decoder. Returns (passed, failed)."""
    cases = [
        ('1G2TB66E29L100001', 'G3', 2009),
        ('1G2AL15F2A7100001', 'G5', 2010),
        ('1G2ZA5E07A4100001', 'G6', 2010),
        ('1G2ZC5E03A4333334', 'G6', 2010),
        ('5Y2SR6E69A7444445', 'Vibe', 2010),
        ('5Y2SR6S07A7555556', 'Vibe', 2010),
        ('6G2EW57Y19L666667', 'G8', 2009),
        ('1G2MB35B8A7777778', 'Solstice', 2010),
        ('1G2MG35X1A7888889', 'Solstice', 2010),
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
        m = entry['model']
        by_model.setdefault(m, []).append(code)
    print(f'Total VDS entries: {len(VDS)}')
    print(f'Distinct models: {len(by_model)}')
    for model in sorted(by_model):
        print(f'  {model}: {len(by_model[model])} codes')


if __name__ == '__main__':
    p, f = self_test()
    print(f'{p} passed, {f} failed')
    print()
    coverage_report()
