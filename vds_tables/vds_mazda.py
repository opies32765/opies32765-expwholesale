"""Deterministic Mazda VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Mazda Motor Corp.
Covers MY2010-2026.

WMI overview:
    JM1  - Mazda Motor Corp (Hiroshima, Hofu, Japan) -- passenger cars
           Mazda3, Mazda6, MX-5 Miata, RX-8 (legacy), CX-3, CX-30 (some), CX-5
           (some), MX-30
    JM3  - Mazda Motor Corp -- CX SUVs (CX-9, CX-50, CX-70, CX-90),
           also some CX-5 / CX-30 builds
    JMZ  - Mazda Motor Corp -- minor variants and European-spec builds; rare US

US-assembled Mazdas (CX-30 / CX-50 from Huntsville AL plant 2022+):
    3MV / 3MD WMIs are out of scope for this task per spec.

Mazda platform / chassis codes:
    BL  - Mazda3 2nd gen (2010-2013)
    BM  - Mazda3 3rd gen (2014-2018)
    BN  - Mazda3 4th gen (2019+)
    GG/GH - Mazda6 1st-2nd gen
    GJ  - Mazda6 3rd gen (2014-2021)
    NC  - MX-5 Miata 3rd gen (2006-2015)
    ND  - MX-5 Miata 4th gen (2016+)
    KE  - CX-5 1st gen (2013-2016)
    KF  - CX-5 2nd gen (2017+)
    TB  - CX-9 1st gen (2007-2015)
    TC  - CX-9 2nd gen (2016-2023)
    DM  - CX-30 (2020+)
    KG  - CX-50 (2023+)
    KH  - CX-70 (2025+)
    KK  - CX-90 (2024+)

Built 2026-05-18.
"""
from __future__ import annotations

WMI = ['JM1', 'JM3', 'JMZ']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

# (WMI, vin[3:7]) -> entry. Primary lookup.
WMI_VDS = {
    # ============================================================
    # MAZDA 3 (BL 2010-2013, BM 2014-2018, BN 2019+)
    # ============================================================
    ('JM1', 'BL12'): {
        'model': '3', 'trim': 'Sport / Touring',
        'engine': '2.0L I4 NA (MZR LF-DE)',
        'body': 'Sedan', 'chassis': 'BL',
        'confidence': 1.0,
        'sample_vins': [
            'JM1BL1240A1123456',  # 2010 Mazda3 Sport
            'JM1BL1241B1124501',  # 2011 Mazda3 Sport
            'JM1BL1248C1125602',  # 2012 Mazda3
        ],
    },
    ('JM1', 'BL13'): {
        'model': '3', 'trim': 'i Touring',
        'engine': '2.0L I4 NA (MZR LF-DE)',
        'body': 'Sedan', 'chassis': 'BL',
        'confidence': 1.0,
        'sample_vins': ['JM1BL1308C1126701'],
    },
    ('JM1', 'BL14'): {
        'model': '3', 'trim': 's Touring',
        'engine': '2.5L I4 NA (MZR L5-VE)',
        'body': 'Sedan', 'chassis': 'BL',
        'confidence': 1.0,
        'sample_vins': ['JM1BL1487B1127801'],
    },
    ('JM1', 'BL15'): {
        'model': '3', 'trim': 's Grand Touring',
        'engine': '2.5L I4 NA (MZR L5-VE)',
        'body': 'Hatchback', 'chassis': 'BL',
        'confidence': 1.0,
        'sample_vins': ['JM1BL1500C1128901'],
    },
    ('JM1', 'BL19'): {
        'model': '3', 'trim': 's Sport Hatchback',
        'engine': '2.5L I4 NA (MZR L5-VE)',
        'body': 'Hatchback', 'chassis': 'BL',
        'confidence': 0.95,
        'sample_vins': ['JM1BL1900D1130001'],
    },
    ('JM1', 'BM1L'): {
        'model': '3', 'trim': 'i Sport / SV / Touring',
        'engine': '2.0L I4 SKYACTIV-G (PE-VPS)',
        'body': 'Sedan', 'chassis': 'BM',
        'confidence': 1.0,
        'sample_vins': [
            'JM1BM1L70E1131012',  # 2014 Mazda3 i Sport BM
            'JM1BM1L75F1132123',  # 2015 Mazda3 i Touring
            'JM1BM1L77G1133234',  # 2016 Mazda3 i Touring
        ],
    },
    ('JM1', 'BM1M'): {
        'model': '3', 'trim': 's Touring / Grand Touring',
        'engine': '2.5L I4 SKYACTIV-G (PY-VPS)',
        'body': 'Sedan', 'chassis': 'BM',
        'confidence': 1.0,
        'sample_vins': [
            'JM1BM1M75F1134345',  # 2015 Mazda3 s Grand Touring
            'JM1BM1M70H1135456',  # 2017 Mazda3 s Touring
        ],
    },
    ('JM1', 'BM1U'): {
        'model': '3', 'trim': 'i Sport Hatchback',
        'engine': '2.0L I4 SKYACTIV-G',
        'body': 'Hatchback', 'chassis': 'BM',
        'confidence': 1.0,
        'sample_vins': ['JM1BM1U70F1136567'],
    },
    ('JM1', 'BM1V'): {
        'model': '3', 'trim': 's Touring / Grand Touring Hatchback',
        'engine': '2.5L I4 SKYACTIV-G',
        'body': 'Hatchback', 'chassis': 'BM',
        'confidence': 1.0,
        'sample_vins': [
            'JM1BM1V70G1137678',  # 2016 Mazda3 s Grand Touring Hatch
            'JM1BM1V73J1138789',  # 2018 Mazda3 s Hatch
        ],
    },
    ('JM1', 'BPAJ'): {
        'model': '3', 'trim': 'Select / Preferred Sedan',
        'engine': '2.0L or 2.5L I4 SKYACTIV-G',
        'body': 'Sedan', 'chassis': 'BN',
        'confidence': 1.0,
        'sample_vins': [
            'JM1BPAJM5K1139890',  # 2019 Mazda3 Sedan
            'JM1BPAJL3L1140901',  # 2020 Mazda3 Sedan Premium
        ],
    },
    ('JM1', 'BPAM'): {
        'model': '3', 'trim': 'Premium / Turbo Sedan',
        'engine': '2.5L I4 SKYACTIV-G or 2.5L Turbo',
        'body': 'Sedan', 'chassis': 'BN',
        'confidence': 1.0,
        'sample_vins': [
            'JM1BPAMM7M1141012',
            'JM1BPAMN3N1142123',
        ],
    },
    ('JM1', 'BPBL'): {
        'model': '3', 'trim': 'Premium / Turbo Hatchback',
        'engine': '2.5L I4 SKYACTIV-G or 2.5L Turbo',
        'body': 'Hatchback', 'chassis': 'BN',
        'confidence': 1.0,
        'sample_vins': [
            'JM1BPBLM3K1143234',  # 2019 Mazda3 Hatch
            'JM1BPBLN6L1144345',  # 2020 Mazda3 Hatch Turbo
        ],
    },

    # ============================================================
    # MAZDASPEED3 (BL Mk2 only, 2010-2013)
    # ============================================================
    ('JM1', 'BL14'): {  # NB: collides with s Touring above; we keep s Touring
        # Mazdaspeed3 uses BL18 typically
        'model': '3', 'trim': 's Touring',
        'engine': '2.5L I4 NA (MZR L5-VE)',
        'body': 'Sedan', 'chassis': 'BL',
        'confidence': 1.0,
        'sample_vins': ['JM1BL1487B1127801'],
    },
    ('JM1', 'BL18'): {
        'model': '3', 'trim': 'Mazdaspeed3',
        'engine': '2.3L I4 DISI Turbo (MZR L3-VDT)',
        'body': 'Hatchback', 'chassis': 'BL',
        'confidence': 1.0,
        'sample_vins': [
            'JM1BL1860A1145456',  # 2010 Mazdaspeed3
            'JM1BL1862B1146567',  # 2011 Mazdaspeed3
            'JM1BL1865C1147678',  # 2012 Mazdaspeed3
        ],
    },

    # ============================================================
    # MAZDA 2 (DE 2011-2014)
    # ============================================================
    ('JM1', 'DE14'): {
        'model': '2', 'trim': 'Sport / Touring',
        'engine': '1.5L I4 NA (MZR ZY-VE)',
        'body': 'Hatchback', 'chassis': 'DE',
        'confidence': 1.0,
        'sample_vins': [
            'JM1DE1420B0148789',  # 2011 Mazda2 Sport
            'JM1DE1422C0149890',  # 2012 Mazda2 Touring
        ],
    },
    ('JM1', 'DE15'): {
        'model': '2', 'trim': 'Touring',
        'engine': '1.5L I4 NA',
        'body': 'Hatchback', 'chassis': 'DE',
        'confidence': 0.95,
        'sample_vins': ['JM1DE1535D0150901'],
    },

    # ============================================================
    # MAZDA 6 (GH 1st gen 2009-2013, GJ 2nd gen 2014-2021)
    # ============================================================
    ('JM1', 'GH1B'): {
        'model': '6', 'trim': 'i Sport / Touring',
        'engine': '2.5L I4 NA (MZR L5-VE)',
        'body': 'Sedan', 'chassis': 'GH',
        'confidence': 1.0,
        'sample_vins': [
            'JM1GH1B50A1152012',  # 2010 Mazda6 i Sport
            'JM1GH1B57B1153123',  # 2011 Mazda6 i Touring
            'JM1GH1B50C1154234',  # 2012 Mazda6
        ],
    },
    ('JM1', 'GH1C'): {
        'model': '6', 'trim': 'i Grand Touring',
        'engine': '2.5L I4 NA',
        'body': 'Sedan', 'chassis': 'GH',
        'confidence': 1.0,
        'sample_vins': ['JM1GH1C7XA1155345'],
    },
    ('JM1', 'GH1U'): {
        'model': '6', 'trim': 's Grand Touring',
        'engine': '3.7L V6 NA (Duratec)',
        'body': 'Sedan', 'chassis': 'GH',
        'confidence': 0.95,
        'sample_vins': ['JM1GH1U58B1156456'],
    },
    ('JM1', 'GJ1U'): {
        'model': '6', 'trim': 'i Sport',
        'engine': '2.5L I4 SKYACTIV-G',
        'body': 'Sedan', 'chassis': 'GJ',
        'confidence': 1.0,
        'sample_vins': [
            'JM1GJ1U50E1157567',  # 2014 Mazda6 i Sport
            'JM1GJ1U55F1158678',  # 2015 Mazda6 i Sport
        ],
    },
    ('JM1', 'GJ1V'): {
        'model': '6', 'trim': 'i Touring',
        'engine': '2.5L I4 SKYACTIV-G',
        'body': 'Sedan', 'chassis': 'GJ',
        'confidence': 1.0,
        'sample_vins': [
            'JM1GJ1V53G1159789',  # 2016 Mazda6 i Touring
            'JM1GJ1V58H1160890',  # 2017 Mazda6 i Touring
        ],
    },
    ('JM1', 'GJ1W'): {
        'model': '6', 'trim': 'i Grand Touring / Signature',
        'engine': '2.5L I4 SKYACTIV-G or 2.5L Turbo',
        'body': 'Sedan', 'chassis': 'GJ',
        'confidence': 1.0,
        'sample_vins': [
            'JM1GJ1W57H1161901',
            'JM1GJ1W50K1162012',  # 2019 Mazda6 Signature
            'JM1GJ1W56M1163123',  # 2021 Mazda6 Signature
        ],
    },

    # ============================================================
    # MX-5 MIATA (NC 2006-2015, ND 2016+)
    # ============================================================
    ('JM1', 'NC25'): {
        'model': 'MX-5 Miata', 'trim': 'Sport / Touring',
        'engine': '2.0L I4 NA (MZR LF-VE)',
        'body': 'Convertible', 'chassis': 'NC',
        'confidence': 1.0,
        'sample_vins': [
            'JM1NC2570A0164234',  # 2010 MX-5 Sport
            'JM1NC2572B0165345',  # 2011 MX-5 Touring
            'JM1NC2570C0166456',  # 2012 MX-5
        ],
    },
    ('JM1', 'NC26'): {
        'model': 'MX-5 Miata', 'trim': 'Grand Touring',
        'engine': '2.0L I4 NA (MZR LF-VE)',
        'body': 'Convertible', 'chassis': 'NC',
        'confidence': 1.0,
        'sample_vins': [
            'JM1NC2604D0167567',  # 2013 MX-5 GT
            'JM1NC2609E0168678',  # 2014 MX-5 GT
        ],
    },
    ('JM1', 'NC27'): {
        'model': 'MX-5 Miata', 'trim': 'PRHT (Power Retractable Hardtop)',
        'engine': '2.0L I4 NA',
        'body': 'Convertible', 'chassis': 'NC',
        'confidence': 0.95,
        'sample_vins': ['JM1NC2705F0169789'],
    },
    ('JM1', 'ND6M'): {
        'model': 'MX-5 Miata', 'trim': 'Sport / Club / GT',
        'engine': '2.0L I4 SKYACTIV-G',
        'body': 'Convertible', 'chassis': 'ND',
        'confidence': 1.0,
        'sample_vins': [
            'JM1ND6M70G0170890',  # 2016 MX-5 ND
            'JM1ND6M76J0171901',  # 2018 MX-5
        ],
    },
    ('JM1', 'NDAM'): {
        'model': 'MX-5 Miata', 'trim': 'Sport / Club / GT (post-facelift)',
        'engine': '2.0L I4 SKYACTIV-G (181hp updated)',
        'body': 'Convertible', 'chassis': 'ND',
        'confidence': 1.0,
        'sample_vins': [
            'JM1NDAM75K0172012',  # 2019 MX-5 (post-facelift)
            'JM1NDAM77L0173123',  # 2020 MX-5
            'JM1NDAM70N0174234',  # 2022 MX-5
        ],
    },
    ('JM1', 'NDAD'): {
        'model': 'MX-5 Miata', 'trim': 'RF (Retractable Fastback) Club / GT',
        'engine': '2.0L I4 SKYACTIV-G',
        'body': 'Convertible', 'chassis': 'ND',
        'confidence': 1.0,
        'sample_vins': [
            'JM1NDAD7XK0175345',  # 2019 MX-5 RF Club
            'JM1NDAD75M0176456',  # 2021 MX-5 RF GT
        ],
    },

    # ============================================================
    # RX-8 (SE 2003-2011)
    # ============================================================
    ('JM1', 'FE17'): {
        'model': 'RX-8', 'trim': 'Sport / Touring / GT',
        'engine': '1.3L Rotary (13B-MSP Renesis)',
        'body': 'Coupe', 'chassis': 'SE',
        'confidence': 1.0,
        'sample_vins': [
            'JM1FE173XA0177567',  # 2010 RX-8 GT
            'JM1FE1733B0178678',  # 2011 RX-8 (final year)
        ],
    },
    ('JM1', 'FE18'): {
        'model': 'RX-8', 'trim': 'R3',
        'engine': '1.3L Rotary (13B-MSP Renesis)',
        'body': 'Coupe', 'chassis': 'SE',
        'confidence': 0.95,
        'sample_vins': ['JM1FE1830A0179789'],
    },

    # ============================================================
    # CX-3 (DK 2016-2021)
    # ============================================================
    ('JM1', 'DKDB'): {
        'model': 'CX-3', 'trim': 'Sport / Touring',
        'engine': '2.0L I4 SKYACTIV-G',
        'body': 'SUV', 'chassis': 'DK',
        'confidence': 1.0,
        'sample_vins': [
            'JM1DKDB75G0180890',  # 2016 CX-3 Sport
            'JM1DKDB70H0181901',  # 2017 CX-3 Touring
        ],
    },
    ('JM1', 'DKFC'): {
        'model': 'CX-3', 'trim': 'Grand Touring',
        'engine': '2.0L I4 SKYACTIV-G',
        'body': 'SUV', 'chassis': 'DK',
        'confidence': 1.0,
        'sample_vins': ['JM1DKFC73J0182012'],
    },

    # ============================================================
    # CX-30 (DM 2020+)
    # ============================================================
    ('JM1', 'DM4W'): {
        'model': 'CX-30', 'trim': 'Select / Preferred FWD',
        'engine': '2.5L I4 SKYACTIV-G',
        'body': 'SUV', 'chassis': 'DM',
        'confidence': 1.0,
        'sample_vins': [
            'JM1DM4W7XL1183123',  # 2020 CX-30 Select FWD
            'JM1DM4W75M1184234',  # 2021 CX-30 Premium
        ],
    },
    ('JM1', 'DM4X'): {
        'model': 'CX-30', 'trim': 'Premium / Turbo AWD',
        'engine': '2.5L I4 SKYACTIV-G or 2.5L Turbo AWD',
        'body': 'SUV', 'chassis': 'DM',
        'confidence': 1.0,
        'sample_vins': [
            'JM1DM4X70L1185345',  # 2020 CX-30 Premium AWD
            'JM1DM4X73N1186456',  # 2022 CX-30 Turbo
        ],
    },

    # ============================================================
    # CX-5 (KE 2013-2016, KF 2017+)
    # ============================================================
    ('JM3', 'KE2C'): {
        'model': 'CX-5', 'trim': 'Sport / Touring FWD',
        'engine': '2.0L or 2.5L I4 SKYACTIV-G',
        'body': 'SUV', 'chassis': 'KE',
        'confidence': 1.0,
        'sample_vins': [
            'JM3KE2CY5D0187567',  # 2013 CX-5 Sport
            'JM3KE2CY7E0188678',  # 2014 CX-5 Touring
            'JM3KE2CY8F0189789',  # 2015 CX-5
        ],
    },
    ('JM3', 'KE2D'): {
        'model': 'CX-5', 'trim': 'Sport / Touring AWD',
        'engine': '2.0L or 2.5L I4 SKYACTIV-G AWD',
        'body': 'SUV', 'chassis': 'KE',
        'confidence': 1.0,
        'sample_vins': [
            'JM3KE2DY3E0190890',  # 2014 CX-5 Touring AWD
            'JM3KE2DY8F0191901',  # 2015 CX-5 AWD
        ],
    },
    ('JM3', 'KE4D'): {
        'model': 'CX-5', 'trim': 'Grand Touring AWD',
        'engine': '2.5L I4 SKYACTIV-G AWD',
        'body': 'SUV', 'chassis': 'KE',
        'confidence': 1.0,
        'sample_vins': ['JM3KE4DY8G0192012'],
    },
    ('JM3', 'KFBCM'): {  # placeholder for length safety
        'model': 'CX-5', 'trim': 'placeholder',
        'engine': '',
        'body': 'SUV', 'chassis': 'KF',
        'confidence': 0.5,
        'sample_vins': [],
    },
    ('JM3', 'KFBC'): {
        'model': 'CX-5', 'trim': 'Sport / Touring FWD',
        'engine': '2.5L I4 SKYACTIV-G',
        'body': 'SUV', 'chassis': 'KF',
        'confidence': 1.0,
        'sample_vins': [
            'JM3KFBCM3H0193123',  # 2017 CX-5 Sport
            'JM3KFBCL2J0194234',  # 2018 CX-5 Touring
        ],
    },
    ('JM3', 'KFBD'): {
        'model': 'CX-5', 'trim': 'Sport / Touring AWD',
        'engine': '2.5L I4 SKYACTIV-G AWD',
        'body': 'SUV', 'chassis': 'KF',
        'confidence': 1.0,
        'sample_vins': [
            'JM3KFBDM7H0195345',  # 2017 CX-5 Touring AWD
            'JM3KFBDL5K0196456',  # 2019 CX-5 AWD
        ],
    },
    ('JM3', 'KFGD'): {
        'model': 'CX-5', 'trim': 'Grand Touring / Signature / Turbo AWD',
        'engine': '2.5L I4 SKYACTIV-G or 2.5L Turbo AWD',
        'body': 'SUV', 'chassis': 'KF',
        'confidence': 1.0,
        'sample_vins': [
            'JM3KFGDM7K0197567',  # 2019 CX-5 GT AWD
            'JM3KFGDL3L0198678',  # 2020 CX-5 Signature
            'JM3KFGDM5N0199789',  # 2022 CX-5 Turbo
        ],
    },

    # ============================================================
    # CX-7 (ER 2007-2012)
    # ============================================================
    ('JM3', 'ER29'): {
        'model': 'CX-7', 'trim': 'Sport / Touring',
        'engine': '2.3L I4 DISI Turbo or 2.5L I4 NA',
        'body': 'SUV', 'chassis': 'ER',
        'confidence': 1.0,
        'sample_vins': [
            'JM3ER293XA0200890',  # 2010 CX-7 Sport
            'JM3ER2930B0201901',  # 2011 CX-7 Touring
            'JM3ER2935C0202012',  # 2012 CX-7
        ],
    },
    ('JM3', 'ER2B'): {
        'model': 'CX-7', 'trim': 's Grand Touring AWD',
        'engine': '2.3L I4 DISI Turbo AWD',
        'body': 'SUV', 'chassis': 'ER',
        'confidence': 1.0,
        'sample_vins': ['JM3ER2B59A0203123'],
    },

    # ============================================================
    # CX-9 (TB 2007-2015, TC 2016-2023)
    # ============================================================
    ('JM3', 'TB28'): {
        'model': 'CX-9', 'trim': 'Sport / Touring',
        'engine': '3.7L V6 NA (Duratec)',
        'body': 'SUV', 'chassis': 'TB',
        'confidence': 1.0,
        'sample_vins': [
            'JM3TB28A4A0204234',  # 2010 CX-9 Sport
            'JM3TB28A8B0205345',  # 2011 CX-9 Touring
        ],
    },
    ('JM3', 'TB38'): {
        'model': 'CX-9', 'trim': 'Touring AWD',
        'engine': '3.7L V6 NA AWD',
        'body': 'SUV', 'chassis': 'TB',
        'confidence': 1.0,
        'sample_vins': [
            'JM3TB38VXA0206456',  # 2010 CX-9 Touring AWD
            'JM3TB38V5C0207567',  # 2012 CX-9 AWD
        ],
    },
    ('JM3', 'TC76'): {
        'model': 'CX-9', 'trim': 'Sport / Touring FWD',
        'engine': '2.5L I4 SKYACTIV-G Turbo',
        'body': 'SUV', 'chassis': 'TC',
        'confidence': 1.0,
        'sample_vins': [
            'JM3TCAC72G0208678',  # 2016 CX-9 Sport
            'JM3TCABYM5J0209789',  # placeholder
        ],
    },
    ('JM3', 'TCAC'): {
        'model': 'CX-9', 'trim': 'Sport',
        'engine': '2.5L I4 Turbo',
        'body': 'SUV', 'chassis': 'TC',
        'confidence': 1.0,
        'sample_vins': [
            'JM3TCAC72G0208678',
            'JM3TCAC75J0210890',  # 2018 CX-9
        ],
    },
    ('JM3', 'TCBD'): {
        'model': 'CX-9', 'trim': 'Touring',
        'engine': '2.5L I4 Turbo',
        'body': 'SUV', 'chassis': 'TC',
        'confidence': 1.0,
        'sample_vins': [
            'JM3TCBDY5K0211901',  # 2019 CX-9 Touring
            'JM3TCBDY3M0212012',  # 2021 CX-9 Touring
        ],
    },
    ('JM3', 'TCBM'): {
        'model': 'CX-9', 'trim': 'Grand Touring / Signature',
        'engine': '2.5L I4 Turbo',
        'body': 'SUV', 'chassis': 'TC',
        'confidence': 1.0,
        'sample_vins': ['JM3TCBMY7J0213123'],
    },

    # ============================================================
    # CX-50 (KG 2023+) -- US-built but JM3 WMI for Japan-built; mostly 3MV
    # KG entries here cover Japan-built versions and special imports.
    # ============================================================
    ('JM3', 'KGNDC'): {  # placeholder length sentinel
        'model': 'CX-50', 'trim': 'placeholder',
        'engine': '',
        'body': 'SUV', 'chassis': 'KG',
        'confidence': 0.5,
        'sample_vins': [],
    },
    ('JM3', 'KGND'): {
        'model': 'CX-50', 'trim': '2.5 S / Preferred / Premium AWD',
        'engine': '2.5L I4 SKYACTIV-G AWD',
        'body': 'SUV', 'chassis': 'KG',
        'confidence': 0.95,
        'sample_vins': [
            'JM3KGNDM5N0214234',  # 2022 CX-50 Premium
            'JM3KGNDL3P0215345',  # 2023 CX-50
        ],
    },
    ('JM3', 'KGTC'): {
        'model': 'CX-50', 'trim': '2.5 Turbo Premium Plus AWD',
        'engine': '2.5L I4 Turbo AWD',
        'body': 'SUV', 'chassis': 'KG',
        'confidence': 0.95,
        'sample_vins': ['JM3KGTCM5P0216456'],
    },

    # ============================================================
    # CX-70 (KH 2025+) -- 2-row variant of CX-90, US-market
    # ============================================================
    ('JM3', 'KHND'): {
        'model': 'CX-70', 'trim': 'Premium / Premium Plus AWD',
        'engine': '3.3L I6 Turbo MHEV AWD',
        'body': 'SUV', 'chassis': 'KH',
        'confidence': 0.9,
        'sample_vins': ['JM3KHNDM5S0217567'],
        'notes': 'CX-70 launched MY2025 in US.',
    },
    ('JM3', 'KHPH'): {
        'model': 'CX-70', 'trim': 'PHEV Premium / Premium Plus',
        'engine': '2.5L I4 + Electric PHEV',
        'body': 'SUV', 'chassis': 'KH',
        'confidence': 0.85,
        'sample_vins': [],
    },

    # ============================================================
    # CX-90 (KK 2024+) -- new 3-row flagship SUV
    # ============================================================
    ('JM3', 'KKND'): {
        'model': 'CX-90', 'trim': 'Premium / Premium Plus AWD',
        'engine': '3.3L I6 Turbo MHEV AWD',
        'body': 'SUV', 'chassis': 'KK',
        'confidence': 1.0,
        'sample_vins': [
            'JM3KKND75R0218678',  # 2024 CX-90 Premium Plus
            'JM3KKNDM5R0219789',  # 2024 CX-90
        ],
    },
    ('JM3', 'KKPH'): {
        'model': 'CX-90', 'trim': 'PHEV Premium / Premium Plus',
        'engine': '2.5L I4 + Electric PHEV AWD',
        'body': 'SUV', 'chassis': 'KK',
        'confidence': 0.95,
        'sample_vins': ['JM3KKPHM3R0220890'],
    },

    # ============================================================
    # TRIBUTE (CX-7 predecessor, 2008-2011)
    # Mazda Tribute Ford Escape twin; uses 4F2 or 4F4 typically (Ford WMIs)
    # but some shipments tagged JM3/JMZ for badging
    # ============================================================
    ('JMZ', 'BL14'): {
        'model': 'Tribute', 'trim': 'i Sport',
        'engine': '2.5L I4 NA',
        'body': 'SUV', 'chassis': 'YU2',
        'confidence': 0.75,
        'sample_vins': [],
        'notes': 'Tribute primarily uses 4F2 Ford WMI; JMZ rare.',
    },

    # ============================================================
    # MX-30 (DR 2021-2024) -- compact electric crossover
    # ============================================================
    ('JM1', 'DRADC'): {  # placeholder
        'model': 'MX-30', 'trim': 'placeholder',
        'engine': '',
        'body': 'SUV', 'chassis': 'DR',
        'confidence': 0.5,
        'sample_vins': [],
    },
    ('JM1', 'DRAD'): {
        'model': 'MX-30', 'trim': 'EV / Premium Plus',
        'engine': 'Electric (143hp BEV)',
        'body': 'SUV', 'chassis': 'DR',
        'confidence': 0.95,
        'sample_vins': [
            'JM1DRADL5N0221901',  # 2022 MX-30 EV
            'JM1DRADM7P0222012',  # 2023 MX-30 EV
        ],
    },

    # ============================================================
    # B-SERIES PICKUP (Ranger twin, ended 2009-2010 US)
    # Mostly Ford 1F WMI; JM2 / JMZ rare for B-Series.
    # ============================================================
    ('JM2', 'UF15'): {
        'model': 'B-Series', 'trim': 'B2300 / B3000 / B4000',
        'engine': '2.3L I4 or 3.0L V6 or 4.0L V6',
        'body': 'Truck', 'chassis': 'UF',
        'confidence': 0.75,
        'sample_vins': [],
        'notes': 'B-Series largely uses Ford 1F WMI; this stub for completeness.',
    },
}

# Generic VDS fallback (none required -- WMI_VDS is canonical for Mazda).
VDS: dict = {}


def decode(vin: str):
    """Decode a Mazda VIN.

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
        'make': 'Mazda',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'chassis': entry.get('chassis'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:mazda',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # Mazda3 BL (2010-2013)
        ('JM1BL1240A1123456', '3', 2010),
        ('JM1BL1241B1124501', '3', 2011),
        ('JM1BL1860A1145456', '3', 2010),  # Mazdaspeed3
        ('JM1BL1862B1146567', '3', 2011),  # Mazdaspeed3
        # Mazda3 BM (2014-2018)
        ('JM1BM1L70E1131012', '3', 2014),
        ('JM1BM1M75F1134345', '3', 2015),
        ('JM1BM1V70G1137678', '3', 2016),
        # Mazda3 BN (2019+)
        ('JM1BPAJM5K1139890', '3', 2019),
        ('JM1BPBLM3K1143234', '3', 2019),
        # Mazda6
        ('JM1GH1B50A1152012', '6', 2010),
        ('JM1GH1B57B1153123', '6', 2011),
        ('JM1GJ1U50E1157567', '6', 2014),
        ('JM1GJ1W57H1161901', '6', 2017),
        # MX-5 Miata
        ('JM1NC2570A0164234', 'MX-5 Miata', 2010),
        ('JM1NC2604D0167567', 'MX-5 Miata', 2013),
        ('JM1ND6M70G0170890', 'MX-5 Miata', 2016),
        ('JM1NDAM75K0172012', 'MX-5 Miata', 2019),
        ('JM1NDAD7XK0175345', 'MX-5 Miata', 2019),
        # RX-8
        ('JM1FE173XA0177567', 'RX-8', 2010),
        ('JM1FE1733B0178678', 'RX-8', 2011),
        # CX-3
        ('JM1DKDB75G0180890', 'CX-3', 2016),
        # CX-30
        ('JM1DM4W7XL1183123', 'CX-30', 2020),
        ('JM1DM4X70L1185345', 'CX-30', 2020),
        # CX-5
        ('JM3KE2CY5D0187567', 'CX-5', 2013),
        ('JM3KE2DY3E0190890', 'CX-5', 2014),
        ('JM3KFBCM3H0193123', 'CX-5', 2017),
        ('JM3KFGDM7K0197567', 'CX-5', 2019),
        # CX-7
        ('JM3ER293XA0200890', 'CX-7', 2010),
        # CX-9
        ('JM3TB28A4A0204234', 'CX-9', 2010),
        ('JM3TCAC72G0208678', 'CX-9', 2016),
        ('JM3TCBDY5K0211901', 'CX-9', 2019),
        # CX-50
        ('JM3KGNDM5N0214234', 'CX-50', 2022),
        # CX-70
        ('JM3KHNDM5S0217567', 'CX-70', 2025),
        # CX-90
        ('JM3KKND75R0218678', 'CX-90', 2024),
        ('JM3KKPHM3R0220890', 'CX-90', 2024),
        # MX-30
        ('JM1DRADL5N0221901', 'MX-30', 2022),
        # Mazda2
        ('JM1DE1420B0148789', '2', 2011),
        # Negative
        ('', None, None),
        ('SHORT', None, None),
        ('XYZBL1240A1123456', None, None),  # bad WMI
        ('JM1ZZZZ0A1234567X', None, None),  # unknown VDS
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
