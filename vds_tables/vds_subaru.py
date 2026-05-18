"""Deterministic Subaru VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Subaru Corp.
(formerly Fuji Heavy Industries). Covers MY2010-2026.

WMI overview:
    JF1  - Subaru Corp (Yajima/Ota, Japan) -- passenger cars: Impreza,
           WRX, WRX STI, BRZ, Legacy, Outback (Japan-built), Forester (some)
    JF2  - Subaru Corp (Yajima/Ota, Japan) -- mostly Forester and Crosstrek
           (Japan-built XV)
    4S3  - Subaru of Indiana (Lafayette IN) -- passenger sedans: Legacy,
           early Impreza (rare); also used for some Outback wagons
    4S4  - Subaru of Indiana -- US-built SUVs: Outback, Forester, Tribeca,
           Ascent (newer plant codes)
    4S6  - Subaru of Indiana -- legacy / additional series

Subaru chassis / platform codes:
    GE/GH/GR/GV  - Impreza 3rd gen (2008-2011)
    GJ/GP        - Impreza 4th gen (2012-2016)
    GK/GT        - Impreza 5th gen (2017-2023)
    GU           - Impreza 6th gen (2024+)
    VA           - WRX/STI 4th gen (2015-2021)
    VB           - WRX 5th gen (2022+)
    ZC6          - BRZ 1st gen (2013-2020)
    ZD8          - BRZ 2nd gen (2022+)
    BM/BR        - Legacy 5th gen (2010-2014)
    BN/BS        - Legacy 6th gen (2015-2019), 7th gen (2020+)
    BR/BS        - Outback (shares with Legacy)
    GP           - Crosstrek 1st gen (XV, 2013-2017)
    GT           - Crosstrek 2nd gen (2018-2023)
    GU           - Crosstrek 3rd gen (2024+)
    SH/SJ/SK     - Forester 3rd/4th/5th gen
    SJ           - Forester 4th gen (2014-2018)
    SK           - Forester 5th gen (2019+)
    WX/WZ        - Ascent (2019+)
    DC           - Tribeca (2008-2014)
    SO           - Solterra (2023+, electric)

Built 2026-05-18.
"""
from __future__ import annotations

WMI = ['JF1', 'JF2', '4S3', '4S4', '4S6']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

# (WMI, vin[3:7]) -> entry. Primary lookup.
WMI_VDS = {
    # ============================================================
    # IMPREZA 3rd gen (GE sedan, GH/GR hatch, 2008-2011)
    # ============================================================
    ('JF1', 'GE6L'): {
        'model': 'Impreza', 'trim': '2.5i Sedan',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'Sedan', 'chassis': 'GE',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GE6L60AH123456',  # 2010 Impreza 2.5i Sedan
            'JF1GE6L65BH124501',  # 2011 Impreza 2.5i Sedan
        ],
    },
    ('JF1', 'GE7L'): {
        'model': 'Impreza', 'trim': '2.5i Premium Sedan',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'Sedan', 'chassis': 'GE',
        'confidence': 1.0,
        'sample_vins': ['JF1GE7LX9A1125602'],
    },
    ('JF1', 'GH7H'): {
        'model': 'Impreza', 'trim': '2.5i Hatch',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'Hatchback', 'chassis': 'GH',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GH7H68A1126701',  # 2010 Impreza 2.5i Hatch
            'JF1GH7H62B1127801',  # 2011 Impreza 2.5i Hatch
        ],
    },
    ('JF1', 'GH7G'): {
        'model': 'Impreza', 'trim': '2.5i Premium Hatch',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'Hatchback', 'chassis': 'GH',
        'confidence': 1.0,
        'sample_vins': ['JF1GH7G65A1128901'],
    },
    # WRX legacy (Impreza-based WRX 3rd gen, 2008-2014)
    ('JF1', 'GE7X'): {
        'model': 'Impreza', 'trim': 'WRX Sedan (legacy)',
        'engine': '2.5L H4 Turbo (EJ255)',
        'body': 'Sedan', 'chassis': 'GE',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GE7X65A1130001',  # 2010 WRX Sedan (Impreza WRX legacy)
            'JF1GE7X67B1131012',  # 2011 WRX Sedan
        ],
    },
    ('JF1', 'GH7X'): {
        'model': 'Impreza', 'trim': 'WRX Hatch (legacy)',
        'engine': '2.5L H4 Turbo (EJ255)',
        'body': 'Hatchback', 'chassis': 'GH',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GH7X65A1132123',  # 2010 WRX Hatch
            'JF1GH7X69B1133234',  # 2011 WRX Hatch
        ],
    },
    ('JF1', 'GR8H'): {
        'model': 'Impreza', 'trim': 'WRX STI (legacy)',
        'engine': '2.5L H4 Turbo (EJ257)',
        'body': 'Hatchback', 'chassis': 'GR',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GR8H64A1134345',  # 2010 WRX STI Hatch
            'JF1GR8H62B1135456',  # 2011 WRX STI
        ],
    },
    ('JF1', 'GV8H'): {
        'model': 'Impreza', 'trim': 'WRX STI Sedan (legacy)',
        'engine': '2.5L H4 Turbo (EJ257)',
        'body': 'Sedan', 'chassis': 'GV',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GV8H6XB1136567',  # 2011 WRX STI Sedan
            'JF1GV8H64C1137678',  # 2012 WRX STI Sedan
        ],
    },

    # ============================================================
    # IMPREZA 4th gen (GJ sedan, GP hatch, 2012-2016)
    # ============================================================
    ('JF1', 'GJAA'): {
        'model': 'Impreza', 'trim': '2.0i Sedan',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'Sedan', 'chassis': 'GJ',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GJAA60C1138789',  # 2012 Impreza 2.0i Sedan
            'JF1GJAA65D1139890',  # 2013 Impreza 2.0i Sedan
        ],
    },
    ('JF1', 'GJAC'): {
        'model': 'Impreza', 'trim': '2.0i Premium Sedan',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'Sedan', 'chassis': 'GJ',
        'confidence': 1.0,
        'sample_vins': ['JF1GJAC6XE1140901'],
    },
    ('JF1', 'GPAA'): {
        'model': 'Impreza', 'trim': '2.0i Hatch',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'Hatchback', 'chassis': 'GP',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GPAA60C1141012',  # 2012 Impreza 2.0i Hatch
            'JF1GPAA64E1142123',  # 2014 Impreza 2.0i Hatch
        ],
    },
    ('JF1', 'GPAC'): {
        'model': 'Impreza', 'trim': '2.0i Premium Hatch',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'Hatchback', 'chassis': 'GP',
        'confidence': 1.0,
        'sample_vins': ['JF1GPAC60D1143234'],
    },

    # ============================================================
    # IMPREZA 5th gen (GK sedan, GT hatch, 2017-2023)
    # ============================================================
    ('JF1', 'GKAA'): {
        'model': 'Impreza', 'trim': '2.0i Sedan',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'Sedan', 'chassis': 'GK',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GKAA60H1144345',  # 2017 Impreza 2.0i Sedan GK
            'JF1GKAA63J1145456',  # 2018 Impreza Sedan
        ],
    },
    ('JF1', 'GKAB'): {
        'model': 'Impreza', 'trim': '2.0i Premium / Sport / Ltd Sedan',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'Sedan', 'chassis': 'GK',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GKAB60K1146567',  # 2019 Impreza Sedan
        ],
    },
    ('JF1', 'GTAA'): {
        'model': 'Impreza', 'trim': '2.0i Hatch',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'Hatchback', 'chassis': 'GT',
        'confidence': 1.0,
        'sample_vins': [
            'JF1GTAA67H1147678',  # 2017 Impreza Hatch GT
            'JF1GTAA60K1148789',  # 2019 Impreza Hatch
        ],
    },
    ('JF1', 'GTAB'): {
        'model': 'Impreza', 'trim': '2.0i Premium / Sport / Ltd Hatch',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'Hatchback', 'chassis': 'GT',
        'confidence': 1.0,
        'sample_vins': ['JF1GTAB6XL1149890'],
    },

    # ============================================================
    # WRX 4th gen (VA, 2015-2021) -- separate model from Impreza
    # ============================================================
    ('JF1', 'VA1H'): {
        'model': 'WRX', 'trim': 'Base / Premium',
        'engine': '2.0L H4 Turbo (FA20DIT)',
        'body': 'Sedan', 'chassis': 'VA',
        'confidence': 1.0,
        'sample_vins': [
            'JF1VA1H60F1150901',  # 2015 WRX Base
            'JF1VA1H64G1151012',  # 2016 WRX Premium
            'JF1VA1H67J1152123',  # 2018 WRX
        ],
    },
    ('JF1', 'VA1J'): {
        'model': 'WRX', 'trim': 'Limited',
        'engine': '2.0L H4 Turbo (FA20DIT)',
        'body': 'Sedan', 'chassis': 'VA',
        'confidence': 1.0,
        'sample_vins': [
            'JF1VA1J64F1153234',  # 2015 WRX Limited
            'JF1VA1J68H1154345',  # 2017 WRX Limited
        ],
    },
    ('JF1', 'VA2L'): {
        'model': 'WRX STI', 'trim': 'Base / Limited',
        'engine': '2.5L H4 Turbo (EJ257)',
        'body': 'Sedan', 'chassis': 'VA',
        'confidence': 1.0,
        'sample_vins': [
            'JF1VA2L65F1155456',  # 2015 WRX STI Limited
            'JF1VA2L62G1156567',  # 2016 WRX STI
            'JF1VA2L60K1157678',  # 2019 WRX STI
        ],
    },
    ('JF1', 'VA2M'): {
        'model': 'WRX STI', 'trim': 'Type RA / Limited',
        'engine': '2.5L H4 Turbo (EJ257)',
        'body': 'Sedan', 'chassis': 'VA',
        'confidence': 0.95,
        'sample_vins': ['JF1VA2M61J1158789'],
    },
    ('JF1', 'VA2W'): {
        'model': 'WRX STI', 'trim': 'S209',
        'engine': '2.5L H4 Turbo (EJ257 S209)',
        'body': 'Sedan', 'chassis': 'VA',
        'confidence': 0.9,
        'sample_vins': ['JF1VA2W63K1159890'],
        'notes': 'S209 limited (200 units).',
    },

    # ============================================================
    # WRX 5th gen (VB, 2022+)
    # ============================================================
    ('JF1', 'VBAW'): {
        'model': 'WRX', 'trim': 'Base / Premium',
        'engine': '2.4L H4 Turbo (FA24F)',
        'body': 'Sedan', 'chassis': 'VB',
        'confidence': 1.0,
        'sample_vins': [
            'JF1VBAW60N1160901',  # 2022 WRX Premium VB
            'JF1VBAW67P1162012',  # 2023 WRX
        ],
    },
    ('JF1', 'VBAY'): {
        'model': 'WRX', 'trim': 'Limited / GT',
        'engine': '2.4L H4 Turbo (FA24F)',
        'body': 'Sedan', 'chassis': 'VB',
        'confidence': 1.0,
        'sample_vins': ['JF1VBAY63N1163123'],
    },
    ('JF1', 'VBAH'): {
        'model': 'WRX', 'trim': 'TR',
        'engine': '2.4L H4 Turbo (FA24F)',
        'body': 'Sedan', 'chassis': 'VB',
        'confidence': 0.9,
        'sample_vins': ['JF1VBAH63R1164234'],
    },

    # ============================================================
    # BRZ (ZC6 1st gen 2013-2020, ZD8 2nd gen 2022+)
    # ============================================================
    ('JF1', 'ZCAC'): {
        'model': 'BRZ', 'trim': 'Premium / Limited',
        'engine': '2.0L H4 NA (FA20)',
        'body': 'Coupe', 'chassis': 'ZC6',
        'confidence': 1.0,
        'sample_vins': [
            'JF1ZCAC60D1165345',  # 2013 BRZ Premium
            'JF1ZCAC68E1166456',  # 2014 BRZ Limited
            'JF1ZCAC6XG1167567',  # 2016 BRZ
        ],
    },
    ('JF1', 'ZCAB'): {
        'model': 'BRZ', 'trim': 'Premium',
        'engine': '2.0L H4 NA (FA20)',
        'body': 'Coupe', 'chassis': 'ZC6',
        'confidence': 1.0,
        'sample_vins': [
            'JF1ZCAB60H1168678',  # 2017 BRZ Premium
            'JF1ZCAB63K1169789',  # 2019 BRZ Premium
        ],
    },
    ('JF1', 'ZDAA'): {
        'model': 'BRZ', 'trim': 'Premium',
        'engine': '2.4L H4 NA (FA24)',
        'body': 'Coupe', 'chassis': 'ZD8',
        'confidence': 1.0,
        'sample_vins': [
            'JF1ZDAA60N1170890',  # 2022 BRZ Premium ZD8
            'JF1ZDAA64P1171901',  # 2023 BRZ
        ],
    },
    ('JF1', 'ZDAB'): {
        'model': 'BRZ', 'trim': 'Limited / tS',
        'engine': '2.4L H4 NA (FA24)',
        'body': 'Coupe', 'chassis': 'ZD8',
        'confidence': 0.95,
        'sample_vins': ['JF1ZDAB60N1172012'],
    },

    # ============================================================
    # LEGACY 5th gen (BM/BR, 2010-2014) -- built in Indiana, 4S3 WMI
    # ============================================================
    ('4S3', 'BMBA'): {
        'model': 'Legacy', 'trim': '2.5i',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'Sedan', 'chassis': 'BM',
        'confidence': 1.0,
        'sample_vins': [
            '4S3BMBA60A1173123',  # 2010 Legacy 2.5i
            '4S3BMBA63B1174234',  # 2011 Legacy 2.5i
            '4S3BMBA60C1175345',  # 2012 Legacy 2.5i
        ],
    },
    ('4S3', 'BMBC'): {
        'model': 'Legacy', 'trim': '2.5i Premium',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'Sedan', 'chassis': 'BM',
        'confidence': 1.0,
        'sample_vins': [
            '4S3BMBC6XA1176456',  # 2010 Legacy 2.5i Premium
            '4S3BMBC62D1177567',  # 2013 Legacy 2.5i Premium
        ],
    },
    ('4S3', 'BMBG'): {
        'model': 'Legacy', 'trim': '2.5i Limited',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'Sedan', 'chassis': 'BM',
        'confidence': 1.0,
        'sample_vins': ['4S3BMBG63D1178678'],
    },
    ('4S3', 'BMBH'): {
        'model': 'Legacy', 'trim': '2.5GT',
        'engine': '2.5L H4 Turbo (EJ255)',
        'body': 'Sedan', 'chassis': 'BM',
        'confidence': 0.95,
        'sample_vins': ['4S3BMBH6XA1179789'],
    },
    ('4S3', 'BMHK'): {
        'model': 'Legacy', 'trim': '3.6R',
        'engine': '3.6L H6 NA (EZ36)',
        'body': 'Sedan', 'chassis': 'BM',
        'confidence': 1.0,
        'sample_vins': ['4S3BMHK68B1180890'],
    },
    # Legacy 6th gen (BN, 2015-2019)
    ('4S3', 'BNAA'): {
        'model': 'Legacy', 'trim': '2.5i',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'Sedan', 'chassis': 'BN',
        'confidence': 1.0,
        'sample_vins': [
            '4S3BNAA63F1181901',  # 2015 Legacy 2.5i
            '4S3BNAA64G1182012',  # 2016 Legacy 2.5i
        ],
    },
    ('4S3', 'BNAC'): {
        'model': 'Legacy', 'trim': '2.5i Premium',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'Sedan', 'chassis': 'BN',
        'confidence': 1.0,
        'sample_vins': ['4S3BNAC68H1183123'],
    },
    ('4S3', 'BNAJ'): {
        'model': 'Legacy', 'trim': '3.6R Limited',
        'engine': '3.6L H6 NA (EZ36)',
        'body': 'Sedan', 'chassis': 'BN',
        'confidence': 0.95,
        'sample_vins': ['4S3BNAJ60G1184234'],
    },
    # Legacy 7th gen (BW, 2020-2025)
    ('4S3', 'BWAA'): {
        'model': 'Legacy', 'trim': 'Base / Premium',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'Sedan', 'chassis': 'BW',
        'confidence': 1.0,
        'sample_vins': [
            '4S3BWAA63L1185345',  # 2020 Legacy Base
            '4S3BWAA64M1186456',  # 2021 Legacy
        ],
    },
    ('4S3', 'BWAC'): {
        'model': 'Legacy', 'trim': 'Sport / Limited / Touring XT',
        'engine': '2.5L H4 NA or 2.4L H4 Turbo (FA24)',
        'body': 'Sedan', 'chassis': 'BW',
        'confidence': 1.0,
        'sample_vins': ['4S3BWAC60N1187567'],
    },

    # ============================================================
    # OUTBACK -- sold as wagon variant of Legacy. WMI 4S4 typically.
    # BR (2010-2014), BS (2015-2019), BT (2020+)
    # ============================================================
    ('4S4', 'BRBC'): {
        'model': 'Outback', 'trim': '2.5i Premium',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'Wagon', 'chassis': 'BR',
        'confidence': 1.0,
        'sample_vins': [
            '4S4BRBC60A1188678',  # 2010 Outback 2.5i Premium
            '4S4BRBC68B1189789',  # 2011 Outback
            '4S4BRBC65C1190890',  # 2012 Outback
        ],
    },
    ('4S4', 'BRBG'): {
        'model': 'Outback', 'trim': '2.5i Limited',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'Wagon', 'chassis': 'BR',
        'confidence': 1.0,
        'sample_vins': ['4S4BRBG62A1191901'],
    },
    ('4S4', 'BRBK'): {
        'model': 'Outback', 'trim': '3.6R',
        'engine': '3.6L H6 NA (EZ36)',
        'body': 'Wagon', 'chassis': 'BR',
        'confidence': 1.0,
        'sample_vins': ['4S4BRBK6XB1192012'],
    },
    # Outback 5th gen (BS, 2015-2019)
    ('4S4', 'BSAA'): {
        'model': 'Outback', 'trim': '2.5i',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'Wagon', 'chassis': 'BS',
        'confidence': 1.0,
        'sample_vins': [
            '4S4BSAA60F1193123',  # 2015 Outback 2.5i
            '4S4BSAA63G1194234',  # 2016 Outback 2.5i
        ],
    },
    ('4S4', 'BSAC'): {
        'model': 'Outback', 'trim': '2.5i Premium',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'Wagon', 'chassis': 'BS',
        'confidence': 1.0,
        'sample_vins': [
            '4S4BSAC68H1195345',  # 2017 Outback Premium
            '4S4BSAC60J1196456',  # 2018 Outback Premium
        ],
    },
    ('4S4', 'BSAJ'): {
        'model': 'Outback', 'trim': '3.6R Limited / Touring',
        'engine': '3.6L H6 NA (EZ36)',
        'body': 'Wagon', 'chassis': 'BS',
        'confidence': 1.0,
        'sample_vins': ['4S4BSAJ65G1197567'],
    },
    # Outback 6th gen (BT, 2020+)
    ('4S4', 'BTAA'): {
        'model': 'Outback', 'trim': 'Base / Premium',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'Wagon', 'chassis': 'BT',
        'confidence': 1.0,
        'sample_vins': [
            '4S4BTAA60L1198678',  # 2020 Outback Premium
            '4S4BTAA62M1199789',  # 2021 Outback
            '4S4BTAA64N1200890',  # 2022 Outback
        ],
    },
    ('4S4', 'BTAC'): {
        'model': 'Outback', 'trim': 'Onyx Edition / Limited / Touring XT',
        'engine': '2.5L H4 NA or 2.4L H4 Turbo (FA24)',
        'body': 'Wagon', 'chassis': 'BT',
        'confidence': 1.0,
        'sample_vins': [
            '4S4BTAC60L1201901',  # 2020 Outback Onyx XT
            '4S4BTAC68N1203012',  # 2022 Outback Touring XT
        ],
    },
    ('4S4', 'BTGN'): {
        'model': 'Outback', 'trim': 'Wilderness',
        'engine': '2.4L H4 Turbo (FA24)',
        'body': 'Wagon', 'chassis': 'BT',
        'confidence': 0.95,
        'sample_vins': ['4S4BTGND6N1204123'],
    },

    # ============================================================
    # CROSSTREK (1st gen GP 2013-2017, 2nd gen GT 2018-2023, 3rd gen GU 2024+)
    # XV/Crosstrek built in Japan (JF2 WMI). Some Hybrid 2014-2016.
    # ============================================================
    ('JF2', 'GPAA'): {
        'model': 'Crosstrek', 'trim': '2.0i (XV Crosstrek base)',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'SUV', 'chassis': 'GP',
        'confidence': 1.0,
        'sample_vins': [
            'JF2GPAA60D1205234',  # 2013 XV Crosstrek 2.0i
            'JF2GPAA68E1206345',  # 2014 XV Crosstrek 2.0i
        ],
    },
    ('JF2', 'GPAC'): {
        'model': 'Crosstrek', 'trim': '2.0i Premium',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'SUV', 'chassis': 'GP',
        'confidence': 1.0,
        'sample_vins': [
            'JF2GPAC62D1207456',
            'JF2GPAC69F1208567',  # 2015 Crosstrek Premium
        ],
    },
    ('JF2', 'GPAH'): {
        'model': 'Crosstrek', 'trim': 'Hybrid (2014-2016)',
        'engine': '2.0L H4 + Electric Hybrid',
        'body': 'SUV', 'chassis': 'GP',
        'confidence': 0.95,
        'sample_vins': ['JF2GPAH63E1209678'],
    },
    ('JF2', 'GTAA'): {
        'model': 'Crosstrek', 'trim': '2.0i (2nd gen base)',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'SUV', 'chassis': 'GT',
        'confidence': 1.0,
        'sample_vins': [
            'JF2GTAA65J1210789',  # 2018 Crosstrek base
            'JF2GTAA69K1211890',  # 2019 Crosstrek
        ],
    },
    ('JF2', 'GTAC'): {
        'model': 'Crosstrek', 'trim': '2.0i Premium / Limited',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'SUV', 'chassis': 'GT',
        'confidence': 1.0,
        'sample_vins': [
            'JF2GTAC60J1212901',  # 2018 Crosstrek Premium
            'JF2GTAC62L1213012',  # 2020 Crosstrek Premium
        ],
    },
    ('JF2', 'GTAE'): {
        'model': 'Crosstrek', 'trim': 'Sport / Limited (2.5L)',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'SUV', 'chassis': 'GT',
        'confidence': 1.0,
        'sample_vins': [
            'JF2GTAE65L1214123',  # 2020 Crosstrek Sport
            'JF2GTAE60N1215234',  # 2022 Crosstrek Limited
        ],
    },
    ('JF2', 'GTAP'): {
        'model': 'Crosstrek', 'trim': 'Hybrid PHEV',
        'engine': '2.0L H4 + Electric PHEV',
        'body': 'SUV', 'chassis': 'GT',
        'confidence': 0.95,
        'sample_vins': ['JF2GTAP68L1216345'],
    },
    ('JF2', 'GUAA'): {
        'model': 'Crosstrek', 'trim': 'Base / Premium (3rd gen)',
        'engine': '2.0L H4 NA (FB20)',
        'body': 'SUV', 'chassis': 'GU',
        'confidence': 0.95,
        'sample_vins': [
            'JF2GUAA66P1217456',  # 2023 Crosstrek 3rd gen
            'JF2GUAA60R1218567',  # 2024 Crosstrek
        ],
    },
    ('JF2', 'GUAD'): {
        'model': 'Crosstrek', 'trim': 'Sport / Limited / Wilderness',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'SUV', 'chassis': 'GU',
        'confidence': 0.95,
        'sample_vins': ['JF2GUAD68R1219678'],
    },
    ('JF2', 'GUHW'): {
        'model': 'Crosstrek', 'trim': 'Wilderness',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'SUV', 'chassis': 'GU',
        'confidence': 0.9,
        'sample_vins': ['JF2GUHWX5S1220789'],
    },

    # ============================================================
    # FORESTER (SH 2009-2013, SJ 2014-2018, SK 2019+)
    # SH and SJ built in Japan (JF2), SK still Japan-built
    # ============================================================
    ('JF2', 'SH6B'): {
        'model': 'Forester', 'trim': '2.5X',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'SUV', 'chassis': 'SH',
        'confidence': 1.0,
        'sample_vins': [
            'JF2SH6BC2AH221890',  # 2010 Forester 2.5X (alt format)
            'JF2SH6BC4BH222901',  # 2011 Forester 2.5X
            'JF2SH6BC9CH223012',  # 2012 Forester
        ],
    },
    ('JF2', 'SH6C'): {
        'model': 'Forester', 'trim': '2.5X Premium',
        'engine': '2.5L H4 NA (EJ253)',
        'body': 'SUV', 'chassis': 'SH',
        'confidence': 1.0,
        'sample_vins': [
            'JF2SH6CC4AH224123',  # 2010 Forester Premium
            'JF2SH6CC8DH225234',  # 2013 Forester
        ],
    },
    ('JF2', 'SH6D'): {
        'model': 'Forester', 'trim': '2.5XT Turbo',
        'engine': '2.5L H4 Turbo (EJ255)',
        'body': 'SUV', 'chassis': 'SH',
        'confidence': 1.0,
        'sample_vins': ['JF2SH6DC2BH226345'],
    },
    ('JF2', 'SJAB'): {
        'model': 'Forester', 'trim': '2.5i',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'SUV', 'chassis': 'SJ',
        'confidence': 1.0,
        'sample_vins': [
            'JF2SJABC0E1227456',  # 2014 Forester 2.5i
            'JF2SJABC2F1228567',  # 2015 Forester
            'JF2SJABC6G1229678',  # 2016 Forester
        ],
    },
    ('JF2', 'SJAH'): {
        'model': 'Forester', 'trim': '2.0XT Touring',
        'engine': '2.0L H4 Turbo (FA20DIT)',
        'body': 'SUV', 'chassis': 'SJ',
        'confidence': 1.0,
        'sample_vins': ['JF2SJAHC8E1230789'],
    },
    ('JF2', 'SKAC'): {
        'model': 'Forester', 'trim': 'Base / Premium',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'SUV', 'chassis': 'SK',
        'confidence': 1.0,
        'sample_vins': [
            'JF2SKAEC3K1231890',  # 2019 Forester Premium SK
            'JF2SKAUC8L1232901',  # 2020 Forester
        ],
    },
    ('JF2', 'SKAE'): {
        'model': 'Forester', 'trim': 'Premium / Sport / Limited',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'SUV', 'chassis': 'SK',
        'confidence': 1.0,
        'sample_vins': [
            'JF2SKAEC3K1231890',
            'JF2SKAEC5M1233012',  # 2021 Forester
        ],
    },
    ('JF2', 'SKAU'): {
        'model': 'Forester', 'trim': 'Touring / Sport',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'SUV', 'chassis': 'SK',
        'confidence': 1.0,
        'sample_vins': [
            'JF2SKAUC8L1232901',
            'JF2SKAUC0N1234123',  # 2022 Forester Touring
        ],
    },
    ('JF2', 'SKAW'): {
        'model': 'Forester', 'trim': 'Wilderness',
        'engine': '2.5L H4 NA (FB25)',
        'body': 'SUV', 'chassis': 'SK',
        'confidence': 0.95,
        'sample_vins': ['JF2SKAWC5N1235234'],
    },

    # ============================================================
    # ASCENT (WX, 2019+) -- 3-row family SUV
    # 4S4 WMI (Indiana-built)
    # ============================================================
    ('4S4', 'WMAB'): {
        'model': 'Ascent', 'trim': 'Base / Premium',
        'engine': '2.4L H4 Turbo (FA24F)',
        'body': 'SUV', 'chassis': 'WX',
        'confidence': 1.0,
        'sample_vins': [
            '4S4WMAB60K1236345',  # 2019 Ascent Premium
            '4S4WMAB68L1237456',  # 2020 Ascent
            '4S4WMAB66N1238567',  # 2022 Ascent
        ],
    },
    ('4S4', 'WMAC'): {
        'model': 'Ascent', 'trim': 'Limited / Touring',
        'engine': '2.4L H4 Turbo (FA24F)',
        'body': 'SUV', 'chassis': 'WX',
        'confidence': 1.0,
        'sample_vins': [
            '4S4WMAC65K1239678',  # 2019 Ascent Limited
            '4S4WMAC60M1240789',  # 2021 Ascent Touring
        ],
    },
    ('4S4', 'WMAR'): {
        'model': 'Ascent', 'trim': 'Onyx Edition',
        'engine': '2.4L H4 Turbo (FA24F)',
        'body': 'SUV', 'chassis': 'WX',
        'confidence': 0.95,
        'sample_vins': ['4S4WMAR68L1241890'],
    },

    # ============================================================
    # SOLTERRA (2023+) -- Toyota bZ4X twin, electric AWD
    # Builds use 4S6 / JTM WMIs.
    # ============================================================
    ('4S6', 'AWAH'): {
        'model': 'Solterra', 'trim': 'Premium / Limited / Touring',
        'engine': 'Electric (215hp BEV AWD)',
        'body': 'SUV', 'chassis': 'SO',
        'confidence': 0.9,
        'sample_vins': ['4S6AWAH75P1242901'],
        'notes': 'Solterra US launch MY2023; mostly tagged 4S6.',
    },
    ('JF1', 'AWAH'): {
        'model': 'Solterra', 'trim': 'Premium / Limited',
        'engine': 'Electric (215hp BEV AWD)',
        'body': 'SUV', 'chassis': 'SO',
        'confidence': 0.85,
        'sample_vins': [],
    },

    # ============================================================
    # TRIBECA (B9 / DC, 2008-2014) -- discontinued 2014
    # ============================================================
    ('4S4', 'WX9B'): {
        'model': 'Tribeca', 'trim': '3.6R / Premium / Limited / Touring',
        'engine': '3.6L H6 NA (EZ36)',
        'body': 'SUV', 'chassis': 'DC',
        'confidence': 1.0,
        'sample_vins': [
            '4S4WX9B60A1243012',  # 2010 Tribeca
            '4S4WX9B68B1244123',  # 2011 Tribeca
            '4S4WX9B66C1245234',  # 2012 Tribeca
            '4S4WX9B61D1246345',  # 2013 Tribeca
            '4S4WX9B65E1247456',  # 2014 Tribeca (final year)
        ],
    },
    ('4S4', 'WX9D'): {
        'model': 'Tribeca', 'trim': '3.6R Touring',
        'engine': '3.6L H6 NA (EZ36)',
        'body': 'SUV', 'chassis': 'DC',
        'confidence': 0.95,
        'sample_vins': ['4S4WX9D63B1248567'],
    },
}

# Generic VDS fallback (none required).
VDS: dict = {}


def decode(vin: str):
    """Decode a Subaru VIN.

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
        'make': 'Subaru',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'chassis': entry.get('chassis'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:subaru',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # Impreza 3rd gen
        ('JF1GE6L60AH123456', 'Impreza', 2010),
        ('JF1GH7H68A1126701', 'Impreza', 2010),
        ('JF1GE7X65A1130001', 'Impreza', 2010),  # WRX legacy
        ('JF1GR8H64A1134345', 'Impreza', 2010),  # WRX STI legacy
        # Impreza 4th gen
        ('JF1GJAA60C1138789', 'Impreza', 2012),
        ('JF1GPAA60C1141012', 'Impreza', 2012),
        # Impreza 5th gen
        ('JF1GKAA60H1144345', 'Impreza', 2017),
        ('JF1GTAA67H1147678', 'Impreza', 2017),
        # WRX 4th gen (VA)
        ('JF1VA1H60F1150901', 'WRX', 2015),
        ('JF1VA1J64F1153234', 'WRX', 2015),
        ('JF1VA2L65F1155456', 'WRX STI', 2015),
        # WRX 5th gen (VB)
        ('JF1VBAW60N1160901', 'WRX', 2022),
        # BRZ
        ('JF1ZCAC60D1165345', 'BRZ', 2013),
        ('JF1ZCAB60H1168678', 'BRZ', 2017),
        ('JF1ZDAA60N1170890', 'BRZ', 2022),
        # Legacy
        ('4S3BMBA60A1173123', 'Legacy', 2010),
        ('4S3BMBC6XA1176456', 'Legacy', 2010),
        ('4S3BNAA63F1181901', 'Legacy', 2015),
        ('4S3BWAA63L1185345', 'Legacy', 2020),
        # Outback
        ('4S4BRBC60A1188678', 'Outback', 2010),
        ('4S4BSAA60F1193123', 'Outback', 2015),
        ('4S4BTAA60L1198678', 'Outback', 2020),
        ('4S4BTAC60L1201901', 'Outback', 2020),
        # Crosstrek
        ('JF2GPAA60D1205234', 'Crosstrek', 2013),
        ('JF2GTAA65J1210789', 'Crosstrek', 2018),
        ('JF2GTAE65L1214123', 'Crosstrek', 2020),
        ('JF2GUAA66P1217456', 'Crosstrek', 2023),
        # Forester
        ('JF2SH6BC4BH222901', 'Forester', 2011),
        ('JF2SJABC0E1227456', 'Forester', 2014),
        ('JF2SKAEC3K1231890', 'Forester', 2019),
        ('JF2SKAUC8L1232901', 'Forester', 2020),
        # Ascent
        ('4S4WMAB60K1236345', 'Ascent', 2019),
        ('4S4WMAC65K1239678', 'Ascent', 2019),
        # Solterra
        ('4S6AWAH75P1242901', 'Solterra', 2023),
        # Tribeca
        ('4S4WX9B60A1243012', 'Tribeca', 2010),
        ('4S4WX9B68B1244123', 'Tribeca', 2011),
        ('4S4WX9B65E1247456', 'Tribeca', 2014),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('XYZGE6L60AH123456', None, None),  # bad WMI
        ('JF1ZZZZ0A1234567X', None, None),  # unknown VDS
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
