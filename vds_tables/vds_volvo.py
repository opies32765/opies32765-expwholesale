"""Deterministic Volvo VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Volvo Cars.
Covers MY2010-2026.

WMI overview:
    YV1  - Volvo Cars AB / Volvo Personvagnar (Torslanda/Gent passenger cars)
           Most US-market Volvo sedans, wagons, coupes
    YV4  - Volvo Cars XC-series SUVs (XC40, XC60, XC70, XC90) and some V wagons
    LVS  - Volvo Cars (Chengdu plant, China) -- some S60 / S90 long wheelbase
    LVY  - Volvo Cars (Chengdu) -- newer electric / specific exports

Volvo platform codes:
    P1 (S40/V50/C30/C70) -- pre-2014
    P2 (XC90 Mk1, V70/XC70 Mk2, S80 Mk2) -- pre-2016
    P3 (S60 Mk2, S80 Mk2 facelift, V60, XC60 Mk1, XC70 Mk3) -- 2010-2018
    SPA (S60/V60 Mk3, S90, V90, XC60 Mk2, XC90 Mk2) -- 2015+
    CMA (XC40, C40 Recharge) -- 2018+
    SPA2 (EX90) -- 2024+
    SEA (EX30) -- 2024+

Notes on Polestar:
    Polestar Engineered S60/V60/XC60 trims (pre-2017) are covered here.
    Polestar 1/2/3 cars (2019+) became their own brand and use WMI LPS /
    LYV (Geely group). Out of scope for this Volvo decoder.

Key positions:
    1-3   WMI                     vin[0:3]
    4-7   VDS                     vin[3:7]
    8     Restraint               vin[7]
    9     Check digit             vin[8]
    10    Model year              vin[9]
    11    Plant                   vin[10]
    12-17 Sequential serial       vin[11:17]

Built 2026-05-18.
"""
from __future__ import annotations

WMI = ['YV1', 'YV4', 'LVS', 'LVY']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

# (WMI, vin[3:7]) -> entry
WMI_VDS = {
    # ============================================================
    # S60 SEDAN
    # P3 (Mk2, 2011-2018, FY100, Gent BE / Ghent / Torslanda)
    # SPA (Mk3, 2019+, Charleston SC USA-built)
    # ============================================================
    ('YV1', 'FS31'): {
        'model': 'S60', 'trim': 'T5 / T6',
        'engine': '2.5L I5 Turbo or 3.0L I6 Turbo',
        'body': 'Sedan', 'chassis': 'P3',
        'confidence': 1.0,
        'sample_vins': [
            'YV1FS31R5B2123456',  # 2011 S60 T6
            'YV1FS31R7C2124501',  # 2012 S60 T6
            'YV1FS31R0D2125602',  # 2013 S60 T6
        ],
    },
    ('YV1', 'FS30'): {
        'model': 'S60', 'trim': 'T5',
        'engine': '2.5L I5 Turbo (B5254T)',
        'body': 'Sedan', 'chassis': 'P3',
        'confidence': 1.0,
        'sample_vins': [
            'YV1FS30N0C2126701',  # 2012 S60 T5
            'YV1FS30N3D2127801',  # 2013 S60 T5
        ],
    },
    ('YV1', 'FS40'): {
        'model': 'S60', 'trim': 'T5 AWD',
        'engine': '2.5L I5 Turbo AWD',
        'body': 'Sedan', 'chassis': 'P3',
        'confidence': 1.0,
        'sample_vins': ['YV1FS40N5E2128901'],
    },
    ('YV1', 'FS48'): {
        'model': 'S60', 'trim': 'Polestar',
        'engine': '3.0L I6 Turbo (B6304T4) AWD',
        'body': 'Sedan', 'chassis': 'P3',
        'confidence': 0.95,
        'sample_vins': ['YV1FS48F2F1130201'],
    },
    ('YV1', 'A22T'): {
        'model': 'S60', 'trim': 'T5 / T6 Inscription',
        'engine': '2.0L I4 Turbo (B4204T) or T6 SC+Turbo',
        'body': 'Sedan', 'chassis': 'SPA',
        'confidence': 1.0,
        'sample_vins': [
            'YV1A22TK0K1131302',  # 2019 S60 T5 SPA
            'YV1A22TM0L1132501',  # 2020 S60 T6
        ],
    },
    ('YV1', 'A22M'): {
        'model': 'S60', 'trim': 'T5 Momentum',
        'engine': '2.0L I4 Turbo (B4204T)',
        'body': 'Sedan', 'chassis': 'SPA',
        'confidence': 1.0,
        'sample_vins': [
            'YV1A22MK1M1133602',  # 2021 S60 T5 Momentum
        ],
    },
    ('YV1', 'H60V'): {
        'model': 'S60', 'trim': 'Recharge T8 eAWD Polestar Engineered',
        'engine': '2.0L I4 Turbo + Electric PHEV AWD',
        'body': 'Sedan', 'chassis': 'SPA',
        'confidence': 0.95,
        'sample_vins': ['YV1H60VL5N1134701'],
    },
    ('YV1', 'H60T'): {
        'model': 'S60', 'trim': 'Recharge T8 eAWD',
        'engine': '2.0L I4 SC+Turbo + Electric PHEV',
        'body': 'Sedan', 'chassis': 'SPA',
        'confidence': 0.95,
        'sample_vins': ['YV1H60TL4M1135801'],
    },

    # ============================================================
    # S80 SEDAN (legacy P2 Mk1, P3 Mk2 -- 2007-2016)
    # ============================================================
    ('YV1', 'AS98'): {
        'model': 'S80', 'trim': 'T6 / Inscription',
        'engine': '3.0L I6 Turbo (B6304T2) or 3.2L NA',
        'body': 'Sedan', 'chassis': 'P3',
        'confidence': 1.0,
        'sample_vins': [
            'YV1AS982091136901',  # 2009 S80 T6 (carries over to 2010)
            'YV1AS982XA1137001',  # 2010 S80 T6
            'YV1AS982XB1138101',  # 2011 S80 T6
        ],
    },
    ('YV1', 'AS96'): {
        'model': 'S80', 'trim': '3.2 / I6',
        'engine': '3.2L I6 NA (B6324S)',
        'body': 'Sedan', 'chassis': 'P3',
        'confidence': 1.0,
        'sample_vins': [
            'YV1AS960XB1139201',  # 2011 S80 3.2
            'YV1AS961XC1140301',  # 2012 S80 3.2
        ],
    },
    ('YV1', 'AH98'): {
        'model': 'S80', 'trim': 'T6 AWD',
        'engine': '3.0L I6 Turbo AWD',
        'body': 'Sedan', 'chassis': 'P3',
        'confidence': 0.95,
        'sample_vins': ['YV1AH982XD1141401'],
    },

    # ============================================================
    # S90 SEDAN (SPA Mk2, 2017+, Daqing China then Chengdu)
    # ============================================================
    ('YV1', 'A22V'): {
        'model': 'S90', 'trim': 'T5 / T6 Momentum / Inscription',
        'engine': '2.0L I4 Turbo or T6 SC+Turbo',
        'body': 'Sedan', 'chassis': 'SPA',
        'confidence': 0.95,
        'sample_vins': ['YV1A22VL5H1142503'],
    },
    ('LVS', 'A02V'): {
        'model': 'S90', 'trim': 'T5 / T6 (LWB)',
        'engine': '2.0L I4 Turbo or T6 SC+Turbo',
        'body': 'Sedan', 'chassis': 'SPA',
        'confidence': 1.0,
        'sample_vins': [
            'LVSA02VK5H1143604',  # 2017 S90 T5 LWB
            'LVSA02VL3J1144702',  # 2018 S90 T6
            'LVSA02VM0L1145805',  # 2020 S90
        ],
    },
    ('LVS', 'H02V'): {
        'model': 'S90', 'trim': 'Recharge T8 eAWD Inscription',
        'engine': '2.0L I4 SC+Turbo + Electric PHEV AWD',
        'body': 'Sedan', 'chassis': 'SPA',
        'confidence': 0.95,
        'sample_vins': ['LVSH02VL4N1146903'],
    },

    # ============================================================
    # V60 WAGON (P3 Mk1 2011-2018, SPA Mk2 2018+)
    # ============================================================
    ('YV1', 'FW31'): {
        'model': 'V60', 'trim': 'T5 / T6',
        'engine': '2.5L I5 Turbo or 3.0L I6 Turbo',
        'body': 'Wagon', 'chassis': 'P3',
        'confidence': 1.0,
        'sample_vins': [
            'YV1FW31N0E2147012',  # 2014 V60 T5 (P3 wagon)
            'YV1FW31R8F2148124',  # 2015 V60 T6 R-Design
        ],
    },
    ('YV1', 'FW48'): {
        'model': 'V60', 'trim': 'Polestar',
        'engine': '3.0L I6 Turbo (B6304T4) AWD',
        'body': 'Wagon', 'chassis': 'P3',
        'confidence': 0.95,
        'sample_vins': ['YV1FW48F0F1149201'],
    },
    ('YV1', 'FZ40'): {
        'model': 'V60 Cross Country', 'trim': 'T5 AWD',
        'engine': '2.5L I5 Turbo AWD',
        'body': 'Wagon', 'chassis': 'P3',
        'confidence': 0.95,
        'sample_vins': ['YV1FZ40N7G1150302'],
    },
    ('YV1', 'A22Z'): {
        'model': 'V60', 'trim': 'T5 / T6 Inscription',
        'engine': '2.0L I4 Turbo or T6 SC+Turbo',
        'body': 'Wagon', 'chassis': 'SPA',
        'confidence': 1.0,
        'sample_vins': [
            'YV1A22ZK5K1151403',  # 2019 V60 T5
            'YV1A22ZL2L1152504',  # 2020 V60
        ],
    },
    ('YV1', 'A23Z'): {
        'model': 'V60 Cross Country', 'trim': 'T5 AWD',
        'engine': '2.0L I4 Turbo AWD',
        'body': 'Wagon', 'chassis': 'SPA',
        'confidence': 1.0,
        'sample_vins': [
            'YV1A23ZK6K1153605',  # 2019 V60 CC T5
            'YV1A23ZM4M1154706',  # 2021 V60 CC
        ],
    },

    # ============================================================
    # V70 WAGON (P3 Mk3, 2008-2016 -- US discontinued 2010)
    # ============================================================
    ('YV1', 'BW61'): {
        'model': 'V70', 'trim': '3.2 / T6',
        'engine': '3.2L I6 NA or 3.0L I6 Turbo',
        'body': 'Wagon', 'chassis': 'P3',
        'confidence': 1.0,
        'sample_vins': [
            'YV1BW610A91155801',  # 2009 V70 3.2 (carries to 2010 US)
            'YV1BW6100A1156901',  # 2010 V70
        ],
    },
    ('YV1', 'BW98'): {
        'model': 'V70', 'trim': 'T6 AWD',
        'engine': '3.0L I6 Turbo AWD',
        'body': 'Wagon', 'chassis': 'P3',
        'confidence': 0.95,
        'sample_vins': ['YV1BW983XA1157901'],
    },

    # ============================================================
    # V90 WAGON (SPA Mk1, 2017+)
    # ============================================================
    ('YV1', 'A22W'): {
        'model': 'V90', 'trim': 'T5 / T6 Inscription',
        'engine': '2.0L I4 Turbo or T6 SC+Turbo',
        'body': 'Wagon', 'chassis': 'SPA',
        'confidence': 0.95,
        'sample_vins': ['YV1A22WL5H1158012'],
    },
    ('YV1', 'A23W'): {
        'model': 'V90 Cross Country', 'trim': 'T5 / T6 AWD',
        'engine': '2.0L I4 Turbo AWD or T6 SC+Turbo',
        'body': 'Wagon', 'chassis': 'SPA',
        'confidence': 1.0,
        'sample_vins': [
            'YV1A23WK5H1159123',  # 2017 V90 CC T5
            'YV1A23WK6J1160234',  # 2018 V90 CC T5
            'YV1A23WK3L1161345',  # 2020 V90 CC T6
        ],
    },

    # ============================================================
    # XC40 (CMA, 2018+, Ghent BE / Luqiao CN)
    # ============================================================
    ('YV4', '162U'): {
        'model': 'XC40', 'trim': 'T4 / T5 Momentum',
        'engine': '2.0L I4 Turbo (B4204T) or T5 AWD',
        'body': 'SUV', 'chassis': 'CMA',
        'confidence': 1.0,
        'sample_vins': [
            'YV4162UK0J2162401',  # 2018 XC40 T5 AWD
            'YV4162UL5K2163502',  # 2019 XC40 T5
            'YV4162UM0L2164603',  # 2020 XC40
        ],
    },
    ('YV4', '162T'): {
        'model': 'XC40', 'trim': 'T4 Momentum FWD',
        'engine': '2.0L I4 Turbo (B4204T)',
        'body': 'SUV', 'chassis': 'CMA',
        'confidence': 0.95,
        'sample_vins': ['YV4162TK1J2165704'],
    },
    ('YV4', '162V'): {
        'model': 'XC40', 'trim': 'T5 R-Design / Inscription',
        'engine': '2.0L I4 Turbo (B4204T) AWD',
        'body': 'SUV', 'chassis': 'CMA',
        'confidence': 1.0,
        'sample_vins': [
            'YV4162VK2L2166801',
            'YV4162VL5M2167902',
        ],
    },
    ('YV4', 'ER2T'): {
        'model': 'XC40 Recharge', 'trim': 'Pure Electric Twin Motor',
        'engine': 'Electric (402hp BEV AWD)',
        'body': 'SUV', 'chassis': 'CMA',
        'confidence': 0.95,
        'sample_vins': ['YV4ER2TM4M2168012'],
    },
    ('YV4', 'ER2U'): {
        'model': 'XC40 Recharge', 'trim': 'Pure Electric Single Motor',
        'engine': 'Electric (248hp BEV)',
        'body': 'SUV', 'chassis': 'CMA',
        'confidence': 0.9,
        'sample_vins': ['YV4ER2UN5N2169102'],
    },

    # ============================================================
    # XC60 (P3 Mk1 2010-2017, SPA Mk2 2018+)
    # ============================================================
    ('YV4', '902T'): {
        'model': 'XC60', 'trim': '3.2 / T6',
        'engine': '3.2L I6 NA or 3.0L I6 Turbo AWD',
        'body': 'SUV', 'chassis': 'P3',
        'confidence': 1.0,
        'sample_vins': [
            'YV4902T28A1170012',  # 2010 XC60 3.2
            'YV4902T20B1171123',  # 2011 XC60
            'YV4902T29C1172234',  # 2012 XC60 T6
            'YV4902T0XD1173345',  # 2013 XC60 T6
        ],
    },
    ('YV4', '952T'): {
        'model': 'XC60', 'trim': 'T5 / T6 R-Design',
        'engine': '2.5L I5 Turbo or 3.0L I6 Turbo',
        'body': 'SUV', 'chassis': 'P3',
        'confidence': 1.0,
        'sample_vins': [
            'YV4952T25E1174456',
            'YV4952T26F1175567',
            'YV4952T20G1176678',
        ],
    },
    ('YV4', 'A22R'): {
        'model': 'XC60', 'trim': 'T5 / T6 Inscription',
        'engine': '2.0L I4 Turbo or T6 SC+Turbo',
        'body': 'SUV', 'chassis': 'SPA',
        'confidence': 1.0,
        'sample_vins': [
            'YV4A22RK5J1177789',  # 2018 XC60 T5
            'YV4A22RL3L1178890',  # 2020 XC60 T6
            'YV4A22RM1N1179901',  # 2022 XC60
        ],
    },
    ('YV4', 'H60R'): {
        'model': 'XC60', 'trim': 'Recharge T8 eAWD',
        'engine': '2.0L I4 SC+Turbo + Electric PHEV',
        'body': 'SUV', 'chassis': 'SPA',
        'confidence': 1.0,
        'sample_vins': [
            'YV4H60RL5M1180012',  # 2021 XC60 Recharge T8
            'YV4H60RN8N1181123',  # 2022 XC60 Recharge
        ],
    },

    # ============================================================
    # XC70 (P3 Mk3, 2008-2016 -- discontinued 2016 US)
    # ============================================================
    ('YV4', '902Z'): {
        'model': 'XC70', 'trim': '3.2 / T6',
        'engine': '3.2L I6 NA or 3.0L I6 Turbo AWD',
        'body': 'Wagon', 'chassis': 'P3',
        'confidence': 1.0,
        'sample_vins': [
            'YV4902Z90A1182301',  # 2010 XC70 3.2 (vin[9]='A' = 2010)
            'YV4902Z92B1183412',  # 2011 XC70
            'YV4902Z03C1184523',  # 2012 XC70 T6
        ],
    },
    ('YV4', '952Z'): {
        'model': 'XC70', 'trim': 'T5 / T6 R-Design',
        'engine': '2.5L I5 Turbo or 3.0L I6 Turbo',
        'body': 'Wagon', 'chassis': 'P3',
        'confidence': 0.95,
        'sample_vins': ['YV4952Z4F1185634'],
    },

    # ============================================================
    # XC90 (P2 Mk1 2003-2014, SPA Mk2 2016+)
    # ============================================================
    ('YV4', 'CZ85'): {
        'model': 'XC90', 'trim': '3.2 / V8',
        'engine': '3.2L I6 NA or 4.4L V8 (Yamaha)',
        'body': 'SUV', 'chassis': 'P2',
        'confidence': 1.0,
        'sample_vins': [
            'YV4CZ852991186012',  # 2009 XC90 3.2 (carries to 2010)
            'YV4CZ852XA1187123',  # 2010 XC90 3.2
            'YV4CZ852XB1188234',  # 2011 XC90
        ],
    },
    ('YV4', 'CY98'): {
        'model': 'XC90', 'trim': 'V8 / R-Design',
        'engine': '4.4L V8 (Yamaha B8444S)',
        'body': 'SUV', 'chassis': 'P2',
        'confidence': 0.95,
        'sample_vins': ['YV4CY982XB1189345'],
    },
    ('YV4', 'A22P'): {
        'model': 'XC90', 'trim': 'T5 / T6 Momentum / Inscription',
        'engine': '2.0L I4 Turbo or T6 SC+Turbo',
        'body': 'SUV', 'chassis': 'SPA',
        'confidence': 1.0,
        'sample_vins': [
            'YV4A22PK1G1190456',  # 2016 XC90 T6 Momentum
            'YV4A22PL3H1191567',  # 2017 XC90 T6 Inscription
            'YV4A22PM5J1192678',  # 2018 XC90
            'YV4A22PN6L1193789',  # 2020 XC90 T6
        ],
    },
    ('YV4', 'BR0P'): {
        'model': 'XC90', 'trim': 'Recharge T8 eAWD Inscription',
        'engine': '2.0L I4 SC+Turbo + Electric PHEV',
        'body': 'SUV', 'chassis': 'SPA',
        'confidence': 1.0,
        'sample_vins': [
            'YV4BR0PL1L1194890',  # 2020 XC90 Recharge
            'YV4BR0PM7M1195901',  # 2021 XC90 Recharge T8
        ],
    },

    # ============================================================
    # C30 HATCHBACK (P1, 2008-2013)
    # ============================================================
    ('YV1', 'MK67'): {
        'model': 'C30', 'trim': 'T5',
        'engine': '2.5L I5 Turbo (B5254T)',
        'body': 'Hatchback', 'chassis': 'P1',
        'confidence': 1.0,
        'sample_vins': [
            'YV1MK672A91196012',  # 2009 C30 T5 (carries 2010)
            'YV1MK672XA1197123',  # 2010 C30
            'YV1MK672XB1198234',  # 2011 C30 R-Design
        ],
    },

    # ============================================================
    # C70 CONVERTIBLE (P1 retractable hardtop, 2006-2013)
    # ============================================================
    ('YV1', 'MC68'): {
        'model': 'C70', 'trim': 'T5',
        'engine': '2.5L I5 Turbo (B5254T)',
        'body': 'Convertible', 'chassis': 'P1',
        'confidence': 1.0,
        'sample_vins': [
            'YV1MC68299J199345',  # 2009 C70 T5 (10 carry)
            'YV1MC682XA1200456',  # 2010 C70 T5
            'YV1MC682XB1201567',  # 2011 C70
        ],
    },

    # ============================================================
    # EX30 (SEA platform, 2024+) -- China-built, Chengdu/Zhangjiakou
    # ============================================================
    ('LVY', 'A12C'): {
        'model': 'EX30', 'trim': 'Twin Motor Performance',
        'engine': 'Electric (422hp BEV AWD)',
        'body': 'SUV', 'chassis': 'SEA',
        'confidence': 0.9,
        'sample_vins': ['LVYA12CR0R1202678'],
        'notes': 'EX30 launched 2024 in US, MY2024+.',
    },
    ('LVY', 'A12B'): {
        'model': 'EX30', 'trim': 'Single Motor Extended Range',
        'engine': 'Electric (268hp BEV RWD)',
        'body': 'SUV', 'chassis': 'SEA',
        'confidence': 0.85,
        'sample_vins': [],
    },

    # ============================================================
    # EX90 (SPA2, 2024+) -- Charleston SC USA-built
    # ============================================================
    ('YV4', 'BR0R'): {
        'model': 'EX90', 'trim': 'Twin Motor Plus / Ultra',
        'engine': 'Electric (402hp BEV AWD)',
        'body': 'SUV', 'chassis': 'SPA2',
        'confidence': 0.9,
        'sample_vins': ['YV4BR0RR3R1203789'],
        'notes': 'EX90 launched MY2024 from Charleston SC.',
    },
    ('YV4', 'BR0S'): {
        'model': 'EX90', 'trim': 'Twin Motor Performance',
        'engine': 'Electric (510hp BEV AWD)',
        'body': 'SUV', 'chassis': 'SPA2',
        'confidence': 0.85,
        'sample_vins': [],
    },
}

# Generic VDS fallback (none required -- WMI_VDS is canonical for Volvo).
VDS: dict = {}


def decode(vin: str):
    """Decode a Volvo VIN.

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
        'make': 'Volvo',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'chassis': entry.get('chassis'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:volvo',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # S60 P3
        ('YV1FS31R5B2123456', 'S60', 2011),
        ('YV1FS30N0C2126701', 'S60', 2012),
        ('YV1FS40N5E2128901', 'S60', 2014),
        ('YV1FS48F2F1130201', 'S60', 2015),
        # S60 SPA
        ('YV1A22TK0K1131302', 'S60', 2019),
        ('YV1A22MK1M1133602', 'S60', 2021),
        # S80
        ('YV1AS982XA1137001', 'S80', 2010),
        ('YV1AS960XB1139201', 'S80', 2011),
        # S90 LWB (LVS)
        ('LVSA02VK5H1143604', 'S90', 2017),
        ('LVSA02VL3J1144702', 'S90', 2018),
        # V60
        ('YV1FW31N0E2147012', 'V60', 2014),
        ('YV1A22ZK5K1151403', 'V60', 2019),
        ('YV1A23ZK6K1153605', 'V60 Cross Country', 2019),
        # V70
        ('YV1BW6100A1156901', 'V70', 2010),
        ('YV1BW983XA1157901', 'V70', 2010),
        # V90
        ('YV1A23WK5H1159123', 'V90 Cross Country', 2017),
        # XC40
        ('YV4162UK0J2162401', 'XC40', 2018),
        ('YV4162UL5K2163502', 'XC40', 2019),
        ('YV4ER2TM4M2168012', 'XC40 Recharge', 2021),
        # XC60 P3
        ('YV4902T28A1170012', 'XC60', 2010),
        ('YV4902T20B1171123', 'XC60', 2011),
        ('YV4902T29C1172234', 'XC60', 2012),
        ('YV4952T25E1174456', 'XC60', 2014),
        # XC60 SPA
        ('YV4A22RK5J1177789', 'XC60', 2018),
        ('YV4H60RL5M1180012', 'XC60', 2021),
        # XC70
        ('YV4902Z90A1182301', 'XC70', 2010),
        ('YV4902Z03C1184523', 'XC70', 2012),
        # XC90 P2
        ('YV4CZ852XA1187123', 'XC90', 2010),
        ('YV4CZ852XB1188234', 'XC90', 2011),
        # XC90 SPA
        ('YV4A22PK1G1190456', 'XC90', 2016),
        ('YV4A22PL3H1191567', 'XC90', 2017),
        ('YV4BR0PL1L1194890', 'XC90', 2020),
        # C30, C70
        ('YV1MK672XA1197123', 'C30', 2010),
        ('YV1MC682XA1200456', 'C70', 2010),
        # EX30
        ('LVYA12CR0R1202678', 'EX30', 2024),
        # EX90
        ('YV4BR0RR3R1203789', 'EX90', 2024),
        # Negative
        ('', None, None),
        ('SHORT', None, None),
        ('XYZ902T28A1170012', None, None),  # bad WMI
        ('YV4ZZZZZZA1234567', None, None),  # unknown VDS
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
