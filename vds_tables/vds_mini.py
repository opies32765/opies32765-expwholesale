"""Deterministic MINI VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for BMW MINI USA.
Covers MY2010-2026.

WMI overview:
    WMW  - MINI (BMW AG, Oxford UK / Born NL) -- primary MINI WMI
    WBX  - some MINI Countryman / Paceman built at Magna Steyr (Graz, Austria)
           that share WBX with X1 generations; we cover MINI-specific keys only.

MINI generations:
    R-codes (BMW UKL1 platform):
      R55 Clubman (2008-2014)
      R56 Hardtop 3-door (2007-2013)
      R57 Convertible (2009-2015)
      R58 Coupe (2012-2015)
      R59 Roadster (2012-2015)
      R60 Countryman (2011-2016)
      R61 Paceman (2013-2016)
    F-codes (BMW UKL2 platform):
      F55 Hardtop 4-door (2014+)
      F56 Hardtop 3-door (2014+)
      F57 Convertible (2016+)
      F54 Clubman 6-door (2016+)
      F60 Countryman 2nd gen (2017+)
    J-codes:
      J01 Cooper SE / Cooper E (electric) 2024+

Key positions:
    1-3   WMI                     vin[0:3]
    4-7   Body / model line       vin[3:7]  <-- VDS key
    8     Restraint               vin[7]
    9     Check digit             vin[8]
    10    Model year              vin[9]

Built 2026-05-18.
"""
from __future__ import annotations

WMI = ['WMW', 'WBX']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

# (WMI, vin[3:7]) -> entry. Primary lookup.
WMI_VDS = {
    # ============================================================
    # R56 HARDTOP 3-DOOR (2007-2013)
    # vin[3:7]: MF33 Cooper / MF73 Cooper S / MF93 JCW
    # ============================================================
    ('WMW', 'MF33'): {
        'model': 'Cooper', 'trim': 'Hardtop',
        'engine': '1.6L I4 NA (N12 / N16)',
        'body': 'Hatchback', 'chassis': 'R56',
        'confidence': 1.0,
        'sample_vins': [
            'WMWMF33529TZ12345',  # 2009 Cooper R56
            'WMWMF3C53AT123456',  # 2010 Cooper
            'WMWMF3C56BT124501',  # 2011 Cooper
        ],
    },
    ('WMW', 'MF3C'): {
        'model': 'Cooper', 'trim': 'Hardtop',
        'engine': '1.6L I4 NA (N16)',
        'body': 'Hatchback', 'chassis': 'R56',
        'confidence': 1.0,
        'sample_vins': [
            'WMWMF3C53AT123456',
            'WMWMF3C56BT124501',
            'WMWMF3C59CT125602',  # 2012 Cooper
        ],
    },
    ('WMW', 'MF7C'): {
        'model': 'Cooper S', 'trim': 'Hardtop',
        'engine': '1.6L I4 Turbo (N14 / N18)',
        'body': 'Hatchback', 'chassis': 'R56',
        'confidence': 1.0,
        'sample_vins': [
            'WMWMF7C56AT126701',  # 2010 Cooper S
            'WMWMF7C50BT127801',  # 2011 Cooper S
        ],
    },
    ('WMW', 'MF9C'): {
        'model': 'Cooper JCW', 'trim': 'Hardtop JCW',
        'engine': '1.6L I4 Turbo (N14 JCW tune)',
        'body': 'Hatchback', 'chassis': 'R56',
        'confidence': 1.0,
        'sample_vins': ['WMWMF9C58CT128901'],
    },

    # ============================================================
    # R57 CONVERTIBLE (2009-2015)
    # vin[3:7]: MR33 Cooper Conv / MR73 Cooper S Conv / MR93 JCW Conv
    # ============================================================
    ('WMW', 'MR3C'): {
        'model': 'Cooper', 'trim': 'Convertible',
        'engine': '1.6L I4 NA (N16)',
        'body': 'Convertible', 'chassis': 'R57',
        'confidence': 1.0,
        'sample_vins': [
            'WMWMR3C58AT130201',  # 2010 Cooper Convertible
            'WMWMR3C59CT131301',  # 2012 Cooper Convertible
        ],
    },
    ('WMW', 'MR7C'): {
        'model': 'Cooper S', 'trim': 'Convertible',
        'engine': '1.6L I4 Turbo (N18)',
        'body': 'Convertible', 'chassis': 'R57',
        'confidence': 1.0,
        'sample_vins': ['WMWMR7C54DT132401'],
    },
    ('WMW', 'MR9C'): {
        'model': 'Cooper JCW', 'trim': 'Convertible JCW',
        'engine': '1.6L I4 Turbo (N18 JCW)',
        'body': 'Convertible', 'chassis': 'R57',
        'confidence': 0.95,
        'sample_vins': ['WMWMR9C57ET133502'],
    },

    # ============================================================
    # R55 CLUBMAN (2008-2014, 4+1 door barn-door wagon)
    # vin[3:7]: MM33 / MM3C Cooper Clubman / MM7C Cooper S / MM9C JCW
    # ============================================================
    ('WMW', 'MM3C'): {
        'model': 'Cooper Clubman', 'trim': 'Clubman',
        'engine': '1.6L I4 NA (N16)',
        'body': 'Wagon', 'chassis': 'R55',
        'confidence': 1.0,
        'sample_vins': [
            'WMWMM3C58AT134601',  # 2010 Clubman
            'WMWMM3C50BT135701',  # 2011 Clubman
        ],
    },
    ('WMW', 'MM7C'): {
        'model': 'Cooper S Clubman', 'trim': 'Clubman S',
        'engine': '1.6L I4 Turbo (N18)',
        'body': 'Wagon', 'chassis': 'R55',
        'confidence': 1.0,
        'sample_vins': ['WMWMM7C55CT136801'],
    },
    ('WMW', 'MM9C'): {
        'model': 'Cooper JCW Clubman', 'trim': 'Clubman JCW',
        'engine': '1.6L I4 Turbo (N18 JCW)',
        'body': 'Wagon', 'chassis': 'R55',
        'confidence': 0.95,
        'sample_vins': [],
    },

    # ============================================================
    # R58 COUPE (2012-2015) -- 2-seat hardtop
    # vin[3:7]: SX3C Cooper Coupe / SX7C Cooper S Coupe / SX9C JCW
    # ============================================================
    ('WMW', 'SX3C'): {
        'model': 'Cooper Coupe', 'trim': 'Coupe',
        'engine': '1.6L I4 NA (N16)',
        'body': 'Coupe', 'chassis': 'R58',
        'confidence': 0.95,
        'sample_vins': ['WMWSX3C50DT137901'],
    },
    ('WMW', 'SX7C'): {
        'model': 'Cooper S Coupe', 'trim': 'Coupe S',
        'engine': '1.6L I4 Turbo (N18)',
        'body': 'Coupe', 'chassis': 'R58',
        'confidence': 0.95,
        'sample_vins': ['WMWSX7C57CT138012'],
    },

    # ============================================================
    # R59 ROADSTER (2012-2015) -- 2-seat soft-top
    # vin[3:7]: SY3C / SY7C / SY9C
    # ============================================================
    ('WMW', 'SY3C'): {
        'model': 'Cooper Roadster', 'trim': 'Roadster',
        'engine': '1.6L I4 NA (N16)',
        'body': 'Convertible', 'chassis': 'R59',
        'confidence': 0.95,
        'sample_vins': ['WMWSY3C58DT139201'],
    },
    ('WMW', 'SY7C'): {
        'model': 'Cooper S Roadster', 'trim': 'Roadster S',
        'engine': '1.6L I4 Turbo (N18)',
        'body': 'Convertible', 'chassis': 'R59',
        'confidence': 0.95,
        'sample_vins': ['WMWSY7C50ET140301'],
    },

    # ============================================================
    # R60 COUNTRYMAN 1st gen (2011-2016) -- ALL4 AWD available
    # vin[3:7]: ZB3C Cooper Countryman / ZC3C ALL4 / ZC5C S ALL4 / ZB9C JCW
    # ============================================================
    ('WMW', 'ZB3C'): {
        'model': 'Cooper Countryman', 'trim': 'Countryman',
        'engine': '1.6L I4 NA (N16)',
        'body': 'SUV', 'chassis': 'R60',
        'confidence': 1.0,
        'sample_vins': [
            'WMWZB3C56BWP41234',  # 2011 Countryman
            'WMWZB3C53CWP42301',  # 2012 Countryman
        ],
    },
    ('WMW', 'ZB5C'): {
        'model': 'Cooper S Countryman', 'trim': 'Countryman S',
        'engine': '1.6L I4 Turbo (N18)',
        'body': 'SUV', 'chassis': 'R60',
        'confidence': 1.0,
        'sample_vins': [
            'WMWZB5C50CWP43501',  # 2012 Countryman S
            'WMWZB5C58DWP44602',  # 2013 Countryman S
        ],
    },
    ('WMW', 'ZC3C'): {
        'model': 'Cooper Countryman', 'trim': 'Countryman ALL4',
        'engine': '1.6L I4 NA (N16) ALL4',
        'body': 'SUV', 'chassis': 'R60',
        'confidence': 0.95,
        'sample_vins': ['WMWZC3C53EWP45701'],
    },
    ('WMW', 'ZC5C'): {
        'model': 'Cooper S Countryman', 'trim': 'Countryman S ALL4',
        'engine': '1.6L I4 Turbo (N18) ALL4',
        'body': 'SUV', 'chassis': 'R60',
        'confidence': 1.0,
        'sample_vins': [
            'WMWZC5C50EWP46801',
            'WMWZC5C57FWP47902',
        ],
    },
    ('WMW', 'ZC9C'): {
        'model': 'Cooper JCW Countryman', 'trim': 'Countryman JCW ALL4',
        'engine': '1.6L I4 Turbo (N18 JCW) ALL4',
        'body': 'SUV', 'chassis': 'R60',
        'confidence': 0.95,
        'sample_vins': ['WMWZC9C55FWP48012'],
    },

    # ============================================================
    # R61 PACEMAN (2013-2016) -- coupe-roof Countryman
    # vin[3:7]: SS3C / SS5C / SS9C
    # ============================================================
    ('WMW', 'SS3C'): {
        'model': 'Cooper Paceman', 'trim': 'Paceman',
        'engine': '1.6L I4 NA (N16)',
        'body': 'SUV', 'chassis': 'R61',
        'confidence': 0.9,
        'sample_vins': [],
    },
    ('WMW', 'SS5C'): {
        'model': 'Cooper S Paceman', 'trim': 'Paceman S ALL4',
        'engine': '1.6L I4 Turbo (N18) ALL4',
        'body': 'SUV', 'chassis': 'R61',
        'confidence': 0.95,
        'sample_vins': ['WMWSS5C56EWP49105'],
    },

    # ============================================================
    # F56 HARDTOP 3-DOOR (2014-2024, UKL2 platform, B-series engines)
    # vin[3:7]: XU1C Cooper / XU7C Cooper S / XU9C JCW
    # ============================================================
    ('WMW', 'XU1C'): {
        'model': 'Cooper', 'trim': 'Hardtop 2-Door',
        'engine': '1.5L I3 Turbo (B38)',
        'body': 'Hatchback', 'chassis': 'F56',
        'confidence': 1.0,
        'sample_vins': [
            'WMWXU1C55F2A50012',  # 2015 Cooper F56
            'WMWXU1C50G2A51123',  # 2016 Cooper
        ],
    },
    ('WMW', 'XU3C'): {
        'model': 'Cooper', 'trim': 'Hardtop 2-Door',
        'engine': '1.5L I3 Turbo (B38)',
        'body': 'Hatchback', 'chassis': 'F56',
        'confidence': 0.95,
        'sample_vins': [],
    },
    ('WMW', 'XU7C'): {
        'model': 'Cooper S', 'trim': 'Hardtop 2-Door S',
        'engine': '2.0L I4 Turbo (B48)',
        'body': 'Hatchback', 'chassis': 'F56',
        'confidence': 1.0,
        'sample_vins': [
            'WMWXU7C57G2A52234',  # 2016 Cooper S
            'WMWXU7C50H2A53301',  # 2017 Cooper S
        ],
    },
    ('WMW', 'XU9C'): {
        'model': 'Cooper JCW', 'trim': 'Hardtop JCW',
        'engine': '2.0L I4 Turbo (B48 JCW)',
        'body': 'Hatchback', 'chassis': 'F56',
        'confidence': 1.0,
        'sample_vins': ['WMWXU9C56H2A54401'],
    },

    # ============================================================
    # F55 HARDTOP 4-DOOR (2014+)
    # vin[3:7]: XS1C Cooper 4dr / XS7C Cooper S 4dr / XS9C JCW
    # ============================================================
    ('WMW', 'XS1C'): {
        'model': 'Cooper', 'trim': 'Hardtop 4-Door',
        'engine': '1.5L I3 Turbo (B38)',
        'body': 'Hatchback', 'chassis': 'F55',
        'confidence': 1.0,
        'sample_vins': [
            'WMWXS1C57G2A55501',  # 2016 Cooper 4-Door
            'WMWXS1C50K2A56602',  # 2019 Cooper 4-Door
        ],
    },
    ('WMW', 'XS7C'): {
        'model': 'Cooper S', 'trim': 'Hardtop 4-Door S',
        'engine': '2.0L I4 Turbo (B48)',
        'body': 'Hatchback', 'chassis': 'F55',
        'confidence': 1.0,
        'sample_vins': [
            'WMWXS7C56G2A57703',
            'WMWXS7C50J2A58801',
        ],
    },

    # ============================================================
    # F57 CONVERTIBLE (2016+)
    # vin[3:7]: WG3C Cooper Conv / WG5C Cooper S Conv / WG9C JCW Conv
    # ============================================================
    ('WMW', 'WG3C'): {
        'model': 'Cooper', 'trim': 'Convertible',
        'engine': '1.5L I3 Turbo (B38)',
        'body': 'Convertible', 'chassis': 'F57',
        'confidence': 1.0,
        'sample_vins': [
            'WMWWG3C57H2A60012',  # 2017 Cooper Convertible
            'WMWWG3C58J2A61102',  # 2018 Cooper Convertible
        ],
    },
    ('WMW', 'WG5C'): {
        'model': 'Cooper S', 'trim': 'Convertible S',
        'engine': '2.0L I4 Turbo (B48)',
        'body': 'Convertible', 'chassis': 'F57',
        'confidence': 1.0,
        'sample_vins': [
            'WMWWG5C56H2A62201',
            'WMWWG5C57K2A63302',
        ],
    },
    ('WMW', 'WG9C'): {
        'model': 'Cooper JCW', 'trim': 'Convertible JCW',
        'engine': '2.0L I4 Turbo (B48 JCW)',
        'body': 'Convertible', 'chassis': 'F57',
        'confidence': 0.95,
        'sample_vins': ['WMWWG9C50L2A64403'],
    },

    # ============================================================
    # F54 CLUBMAN 6-DOOR (2016+, longer than 3-door, barn doors retained)
    # vin[3:7]: LU1C Clubman / LU7C Clubman S / LU9C Clubman JCW / LV5C ALL4 S
    # ============================================================
    ('WMW', 'LU1C'): {
        'model': 'Cooper Clubman', 'trim': 'Clubman',
        'engine': '1.5L I3 Turbo (B38)',
        'body': 'Wagon', 'chassis': 'F54',
        'confidence': 1.0,
        'sample_vins': [
            'WMWLU1C55H2F65501',  # 2017 Clubman F54
            'WMWLU1C57J2F66602',  # 2018 Clubman
        ],
    },
    ('WMW', 'LU3C'): {
        'model': 'Cooper Clubman', 'trim': 'Clubman ALL4',
        'engine': '1.5L I3 Turbo (B38) ALL4',
        'body': 'Wagon', 'chassis': 'F54',
        'confidence': 0.95,
        'sample_vins': [],
    },
    ('WMW', 'LU5C'): {
        'model': 'Cooper S Clubman', 'trim': 'Clubman S ALL4',
        'engine': '2.0L I4 Turbo (B48) ALL4',
        'body': 'Wagon', 'chassis': 'F54',
        'confidence': 1.0,
        'sample_vins': [
            'WMWLU5C50K2F67701',
            'WMWLU5C58L2F68802',
        ],
    },
    ('WMW', 'LU7C'): {
        'model': 'Cooper S Clubman', 'trim': 'Clubman S',
        'engine': '2.0L I4 Turbo (B48)',
        'body': 'Wagon', 'chassis': 'F54',
        'confidence': 1.0,
        'sample_vins': [
            'WMWLU7C57H2F69901',
        ],
    },
    ('WMW', 'LU9C'): {
        'model': 'Cooper JCW Clubman', 'trim': 'Clubman JCW ALL4',
        'engine': '2.0L I4 Turbo (B48 JCW) ALL4',
        'body': 'Wagon', 'chassis': 'F54',
        'confidence': 0.95,
        'sample_vins': ['WMWLU9C50L2F70012'],
    },

    # ============================================================
    # F60 COUNTRYMAN 2nd gen (2017+) -- Magna Steyr Austria, WMI WBX or WMW
    # vin[3:7]: 53DA Cooper / 53FA Cooper ALL4 / 53FU Cooper S ALL4 / 53HU JCW
    # Many F60 Countryman VINs use WMI WMW; some Austria-built use WBX.
    # ============================================================
    ('WMW', '53DA'): {
        'model': 'Cooper Countryman', 'trim': 'Countryman',
        'engine': '1.5L I3 Turbo (B38)',
        'body': 'SUV', 'chassis': 'F60',
        'confidence': 1.0,
        'sample_vins': [
            'WMW53DA57H3D71103',  # 2017 Countryman F60
            'WMW53DA50J3D72204',  # 2018 Countryman
        ],
    },
    ('WMW', '53FA'): {
        'model': 'Cooper Countryman', 'trim': 'Countryman ALL4',
        'engine': '1.5L I3 Turbo (B38) ALL4',
        'body': 'SUV', 'chassis': 'F60',
        'confidence': 1.0,
        'sample_vins': ['WMW53FA50K3D73305'],
    },
    ('WMW', '53DU'): {
        'model': 'Cooper S Countryman', 'trim': 'Countryman S',
        'engine': '2.0L I4 Turbo (B48)',
        'body': 'SUV', 'chassis': 'F60',
        'confidence': 1.0,
        'sample_vins': ['WMW53DU58H3D74401'],
    },
    ('WMW', '53FU'): {
        'model': 'Cooper S Countryman', 'trim': 'Countryman S ALL4',
        'engine': '2.0L I4 Turbo (B48) ALL4',
        'body': 'SUV', 'chassis': 'F60',
        'confidence': 1.0,
        'sample_vins': [
            'WMW53FU56K3D75502',
            'WMW53FU58L3D76603',
        ],
    },
    ('WMW', '53HU'): {
        'model': 'Cooper JCW Countryman', 'trim': 'Countryman JCW ALL4',
        'engine': '2.0L I4 Turbo (B48 JCW) ALL4',
        'body': 'SUV', 'chassis': 'F60',
        'confidence': 0.95,
        'sample_vins': ['WMW53HU54L3D77704'],
    },
    # F60 Countryman built at Magna Steyr can have WBX WMI
    ('WBX', '53FU'): {
        'model': 'Cooper S Countryman', 'trim': 'Countryman S ALL4',
        'engine': '2.0L I4 Turbo (B48) ALL4',
        'body': 'SUV', 'chassis': 'F60',
        'confidence': 0.95,
        'sample_vins': [],
        'notes': 'Magna Steyr (Graz) built F60 Countryman, occasional WBX WMI.',
    },

    # ============================================================
    # J01 COOPER SE / COOPER E (electric, 2020+ for SE, 2024+ J-platform)
    # SE F56-derived electric, vin[3:7]: 7N7C / 7N9C
    # ============================================================
    ('WMW', '7N7C'): {
        'model': 'Cooper SE', 'trim': 'SE Hardtop Electric',
        'engine': 'Electric (181hp BEV)',
        'body': 'Hatchback', 'chassis': 'F56e',
        'confidence': 1.0,
        'sample_vins': [
            'WMW7N7C55L2J80012',  # 2020 Cooper SE
            'WMW7N7C58M2J81204',  # 2021 Cooper SE
        ],
    },
    ('WMW', '7N9C'): {
        'model': 'Cooper SE', 'trim': 'SE Signature / Iconic',
        'engine': 'Electric (181hp BEV)',
        'body': 'Hatchback', 'chassis': 'F56e',
        'confidence': 0.95,
        'sample_vins': ['WMW7N9C50N2J82301'],
    },
    # New J01 platform electric Cooper E / SE (2024+)
    ('WMW', '93FX'): {
        'model': 'Cooper E', 'trim': 'Cooper E',
        'engine': 'Electric (181hp BEV)',
        'body': 'Hatchback', 'chassis': 'J01',
        'confidence': 0.85,
        'sample_vins': [],
        'notes': 'J01 platform new Cooper E (China-built), MY2024+.',
    },
    ('WMW', '93GX'): {
        'model': 'Cooper SE', 'trim': 'Cooper SE',
        'engine': 'Electric (215hp BEV)',
        'body': 'Hatchback', 'chassis': 'J01',
        'confidence': 0.85,
        'sample_vins': [],
        'notes': 'J01 platform Cooper SE (China-built), MY2024+.',
    },
}

# Generic fallback table (none required for MINI).
VDS: dict = {}


def decode(vin: str):
    """Decode a MINI VIN.

    Returns dict with year/make/model/trim/body/engine/chassis/confidence/source,
    or None if VIN is malformed or unknown.
    """
    if not vin or not isinstance(vin, str) or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    if vin[:3] not in WMI:
        return None
    wmi = vin[:3]
    vds_key = vin[3:7]
    year = YEAR_CODES.get(vin[9])
    entry = WMI_VDS.get((wmi, vds_key))
    if not entry:
        entry = VDS.get(vds_key)
    if not entry:
        return None
    return {
        'year': year,
        'make': 'MINI',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'chassis': entry.get('chassis'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:mini',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # R56 Hardtop
        ('WMWMF3C53AT123456', 'Cooper', 2010),
        ('WMWMF7C56AT126701', 'Cooper S', 2010),
        ('WMWMF9C58CT128901', 'Cooper JCW', 2012),
        # R55 Clubman
        ('WMWMM3C58AT134601', 'Cooper Clubman', 2010),
        # R57 Convertible
        ('WMWMR3C58AT130201', 'Cooper', 2010),
        # R60 Countryman 1st gen
        ('WMWZB3C56BWP41234', 'Cooper Countryman', 2011),
        ('WMWZB5C50CWP43501', 'Cooper S Countryman', 2012),
        ('WMWZC5C50EWP46801', 'Cooper S Countryman', 2014),
        # F56 Hardtop
        ('WMWXU1C55F2A50012', 'Cooper', 2015),
        ('WMWXU7C57G2A52234', 'Cooper S', 2016),
        ('WMWXU9C56H2A54401', 'Cooper JCW', 2017),
        # F55 Hardtop 4-Door
        ('WMWXS1C57G2A55501', 'Cooper', 2016),
        ('WMWXS7C56G2A57703', 'Cooper S', 2016),
        # F57 Convertible
        ('WMWWG3C57H2A60012', 'Cooper', 2017),
        ('WMWWG5C56H2A62201', 'Cooper S', 2017),
        # F54 Clubman 6-Door
        ('WMWLU1C55H2F65501', 'Cooper Clubman', 2017),
        ('WMWLU5C50K2F67701', 'Cooper S Clubman', 2019),
        # F60 Countryman 2nd gen
        ('WMW53DA57H3D71103', 'Cooper Countryman', 2017),
        ('WMW53FU56K3D75502', 'Cooper S Countryman', 2019),
        # Cooper SE electric
        ('WMW7N7C55L2J80012', 'Cooper SE', 2020),
        ('WMW7N7C58M2J81204', 'Cooper SE', 2021),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('XYZXU1C55F2A50012', None, None),  # bad WMI
        ('WMWZZZZ8KE0200120', None, None),  # unknown VDS
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


if __name__ == '__main__':
    p, f = self_test()
    print(f'{p} passed, {f} failed')
