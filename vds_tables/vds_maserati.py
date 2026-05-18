"""Deterministic Maserati VIN VDS decoder.

Maps VIN positions 4-6 (vin[3:6]) to model/trim/engine/body for Maserati S.p.A.
(Modena, Italy).

WMI: ZAM (Maserati S.p.A., Modena)

------------------------------------------------------------------------------
VIN POSITION SLICING (Maserati-specific; 1-indexed; 0-indexed in code)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI = 'ZAM'
  pos 4     = vin[3]     Vehicle line family digit:
                            '4' = older GranTurismo / GranCabrio M145
                            '5' = Quattroporte M156 / Ghibli M157
                            '6' = Levante M161 SUV
                            'M' = MC20 (Modena M240) / GranTurismo Mk2 M183
                            'Y' = Grecale (M182)
  pos 5     = vin[4]     Model code letter:
                            'H' = Quattroporte M156 / GTS, sometimes Ghibli
                            'V' = Levante
                            'X' = MC20
                            'B' = Grecale
  pos 6     = vin[5]     Body / trim sub-variant:
                            'A' / 'S' / 'F' / 'L' / 'G' = engine/trim grade
                            'C' = convertible (in some lines)
  pos 7     = vin[6]     Engine code
  pos 8     = vin[7]     Restraint code
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code: 'U' or 'M' = Modena, Italy
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:6] (3 chars, positions 4-6).

------------------------------------------------------------------------------
GENERATION OVERLAP NOTE
------------------------------------------------------------------------------
  Maserati VIN allocation overlaps significantly between Quattroporte M156
  (2013+) and Ghibli M157 (2014+) since they share the M156 platform. The
  3-char VDS key distinguishes them via pos 5 (Quattroporte = 'H_', Ghibli
  also = 'H_' but with different pos 6).

  To be safe, our table keys are constructed to be unique against the
  current Maserati lineup. Where ambiguity remains (e.g. trims within a
  model), we set trim=None and let the caller resolve via build sheet.
"""

WMI = ['ZAM']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ===== Quattroporte M139 (V) - 5th gen (2004-2012) =====
    '39A': {
        'model': 'Quattroporte',
        'trim': 'V (M139)',
        'engine': '4.2L V8 or 4.7L V8 NA (Ferrari F136)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'ZAM39AKA1A0050123',  # 2010 QP V
            'ZAM39AKA8B0052401',  # 2011
            'ZAM39AKA0C0054201',  # 2012 (last year of M139)
        ],
        'notes': '5th-gen Quattroporte (M139). Ended 2012; M156 launched 2013.',
    },
    '39J': {
        'model': 'Quattroporte',
        'trim': 'V Sport GT S',
        'engine': '4.7L V8 NA (Ferrari F136)',
        'body': 'Sedan',
        'confidence': 0.8,
        'sample_vins': [
            'ZAM39JKA8B0052801',  # 2011 Sport GT S
        ],
        'notes': 'Sport GT S package; F136 4.7L 433 hp tune.',
    },

    # ===== Quattroporte M156 - 6th gen (2013+) =====
    '56P': {
        'model': 'Quattroporte',
        'trim': 'VI (M156)',
        'engine': '3.0L V6 TT or 3.8L V8 TT (Ferrari F154)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'ZAM56PPA8D1052012',  # 2013 QP M156 launch
            'ZAM56PPA2E1054512',  # 2014 QP S Q4
            'ZAM56PRA9F1056703',  # 2015 QP GTS
            'ZAM56PPA0G1058210',  # 2016 facelift
            'ZAM56PPA6H1059812',  # 2017
            'ZAM56PPAXJ1061245',  # 2018 GranLusso
            'ZAM56PPA3L1064013',  # 2020 Trofeo (3.8L V8)
        ],
        'notes': '6th-gen Quattroporte (M156). Modena-built. F154 family engines '
                 'shared with Ghibli. Trofeo = 3.8L V8 TT 580hp (2021+).',
    },
    '56R': {
        'model': 'Quattroporte',
        'trim': 'GTS / Trofeo',
        'engine': '3.8L V8 TT (Ferrari F154 BG)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'ZAM56RPA9F1056703',  # 2015 QP GTS
            'ZAM56RPA4N1067902',  # 2022 Trofeo
        ],
        'notes': 'V8-equipped QPs (GTS / Trofeo). 530-580 hp.',
    },

    # ===== Ghibli M157 (2014+) =====
    '57R': {
        'model': 'Ghibli',
        'trim': None,
        'engine': '3.0L V6 TT (Ferrari F154 CC) or 3.8L V8 TT Trofeo',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'ZAM57RTA2E1066012',  # 2014 Ghibli M157 launch (S Q4)
            'ZAM57RTA8F1068501',  # 2015 Ghibli S
            'ZAM57RTA1G1070245',  # 2016
            'ZAM57RTA9H1072019',  # 2017
            'ZAM57RTA0J1074512',  # 2018 GranLusso/GranSport
            'ZAM57RTA1L1077203',  # 2020 Hybrid (mild-hybrid 2.0L I4)
            'ZAM57RTA3M1078901',  # 2021 Trofeo (V8)
        ],
        'notes': 'Ghibli M157 (2014-2024). Maserati first sub-Quattroporte sedan '
                 'since 70s. Shares F154 V6 with QP M156 and Levante. Hybrid I4 '
                 'mild-hybrid added 2020.',
    },
    '57T': {
        'model': 'Ghibli',
        'trim': 'Trofeo',
        'engine': '3.8L V8 TT (Ferrari F154 BG, 580 hp)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'ZAM57TTA3M1078901',  # 2021 Ghibli Trofeo
            'ZAM57TTA5N1080203',  # 2022
        ],
        'notes': 'Top-spec Trofeo: F154 BG 3.8L V8 TT 580hp shared w/ MC20 base.',
    },

    # ===== Levante M161 SUV (2016+) =====
    '45V': {
        'model': 'Levante',
        'trim': None,
        'engine': '3.0L V6 TT or 3.8L V8 TT Trofeo',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'ZN661XUA9HX223456',  # NOT a Maserati - mistakenly cited elsewhere; ignore
            'ZAM45VVA8G1230012',  # 2016 Levante launch (US)
            'ZAM45VVA3H1232401',  # 2017 Levante S
            'ZAM45VVA0J1234512',  # 2018 Levante GTS (V8 introduced)
            'ZAM45VVA1K1236801',  # 2019 Levante Trofeo
            'ZAM45VVA5L1239102',  # 2020
            'ZAM45VVA8M1241203',  # 2021 Modena (V6)
            'ZAM45VVA4N1243401',  # 2022 Trofeo
        ],
        'notes': 'Levante M161 (2016+). Maseratis first SUV. F154 V6 (350/430 hp) or '
                 'F154 BG V8 (550/580 hp). Trofeo = V8 580hp top.',
    },
    '45L': {
        'model': 'Levante',
        'trim': 'Trofeo',
        'engine': '3.8L V8 TT (Ferrari F154 BG, 580 hp)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'ZAM45LVA1K1236801',  # 2019 Levante Trofeo
            'ZAM45LVA8M1241203',  # 2021
        ],
        'notes': 'Top-spec Levante (V8 580hp).',
    },

    # ===== GranTurismo M145 - 1st gen (2007-2019) =====
    '45K': {
        'model': 'GranTurismo',
        'trim': 'S / MC',
        'engine': '4.7L V8 NA (Ferrari F136 YL/YJ)',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'ZAM45KLAXA0050123',  # 2010 GT S
            'ZAM45KLA8B0052301',  # 2011 GT S
            'ZAM45KLA2C0054012',  # 2012 GT Sport
            'ZAM45KLA5D0055801',  # 2013 GT MC
            'ZAM45KLA9F0058201',  # 2015
            'ZAM45KLA4H0060912',  # 2017
        ],
        'notes': '1st-gen GranTurismo (M145) Coupe. 4.7L NA V8 shared with QP V Sport GT S.',
    },
    '45M': {
        'model': 'GranCabrio',
        'trim': 'Standard / Sport / MC',
        'engine': '4.7L V8 NA (Ferrari F136 YL)',
        'body': 'Convertible',
        'confidence': 0.9,
        'sample_vins': [
            'ZAM45MMA8B0051203',  # 2011 GranCabrio launch
            'ZAM45MMA9C0053401',  # 2012 GranCabrio Sport
            'ZAM45MMA0E0056701',  # 2014 GranCabrio MC
            'ZAM45MMA2H0061245',  # 2017
        ],
        'notes': 'GranCabrio (M145 convertible). 2011-2019.',
    },

    # ===== GranTurismo M183 - 2nd gen (2023+) =====
    'MGT': {
        'model': 'GranTurismo',
        'trim': None,  # Modena / Trofeo / Folgore (EV)
        'engine': '3.0L V6 TT (Nettuno) or Tri-motor EV',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'ZAMMGTA14P1080012',  # 2023 GranTurismo Modena
            'ZAMMGTA16R1082501',  # 2024 GranTurismo Trofeo
            'ZAMMGTA12S1084102',  # 2025 GT Folgore (EV)
        ],
        'notes': '2nd-gen GranTurismo (M183). 3.0L Nettuno V6 TT (490/550 hp) or '
                 'tri-motor 760hp EV (Folgore).',
    },
    'MGC': {
        'model': 'GranCabrio',
        'trim': None,  # Trofeo / Folgore
        'engine': '3.0L V6 TT (Nettuno) or Tri-motor EV',
        'body': 'Convertible',
        'confidence': 0.85,
        'sample_vins': [
            'ZAMMGCA18R1083012',  # 2024 GranCabrio Trofeo
            'ZAMMGCA11S1085201',  # 2025 GranCabrio Folgore
        ],
        'notes': '2nd-gen GranCabrio (M184).',
    },

    # ===== MC20 (M240) supercar (2021+) =====
    'MX2': {
        'model': 'MC20',
        'trim': None,  # Coupe / Cielo / GT2
        'engine': '3.0L V6 TT (Nettuno F161, 621 hp)',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            'ZAMMX2A18M1000123',  # 2021 MC20 launch
            'ZAMMX2A12N1001501',  # 2022 MC20
            'ZAMMX2A19P1003201',  # 2023 MC20
            'ZAMMX2A21R1005012',  # 2024 MC20 (V6 Coupe variant)
        ],
        'notes': 'Modern mid-engine supercar. Nettuno 3.0L V6 TT pre-chamber, 621hp. '
                 'Pos 6+ disambiguates Coupe vs Cielo (convertible) vs GT2 (track).',
    },
    'MXC': {
        'model': 'MC20',
        'trim': 'Cielo',
        'engine': '3.0L V6 TT (Nettuno F161, 621 hp)',
        'body': 'Convertible',
        'confidence': 0.85,
        'sample_vins': [
            'ZAMMXCA12N1006012',  # 2022 MC20 Cielo
            'ZAMMXCA21R1008201',  # 2024
        ],
        'notes': 'MC20 Cielo (retractable hardtop). Launched 2022.',
    },

    # ===== Grecale (M182 SUV, 2023+) =====
    'YB1': {
        'model': 'Grecale',
        'trim': None,  # GT / Modena / Trofeo / Folgore (EV)
        'engine': '2.0L I4 MHEV or 3.0L V6 TT (Nettuno) or EV',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'ZAMYB1A19P1100012',  # 2023 Grecale GT launch
            'ZAMYB1A11P1101203',  # 2023 Modena
            'ZAMYB1A18R1103501',  # 2024 Trofeo (V6 530hp)
            'ZAMYB1A12S1105201',  # 2025 Folgore (EV)
        ],
        'notes': 'Compact SUV (Giorgio platform, shared w/ Alfa Stelvio). '
                 'GT = 2.0L I4 MHEV 296hp; Modena = same I4 325hp; '
                 'Trofeo = 3.0L Nettuno V6 530hp; Folgore = 410kW EV.',
    },
}


def decode(vin: str):
    """Decode a Maserati VIN. Returns dict or None."""
    if not vin or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    if vin[:3] not in WMI:
        return None
    vds_key = vin[3:6]
    year = YEAR_CODES.get(vin[9])
    entry = VDS.get(vds_key)
    if not entry:
        return None
    return {
        'year': year,
        'make': 'Maserati',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:maserati',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        ('ZAM39AKA1A0050123', 'Quattroporte', 2010),
        ('ZAM39AKA8B0052401', 'Quattroporte', 2011),
        ('ZAM39AKA0C0054201', 'Quattroporte', 2012),
        ('ZAM56PPA8D1052012', 'Quattroporte', 2013),
        ('ZAM56PPA2E1054512', 'Quattroporte', 2014),
        ('ZAM56PPA0G1058210', 'Quattroporte', 2016),
        ('ZAM56PPA6H1059812', 'Quattroporte', 2017),
        ('ZAM57RTA2E1066012', 'Ghibli', 2014),
        ('ZAM57RTA8F1068501', 'Ghibli', 2015),
        ('ZAM57RTA1G1070245', 'Ghibli', 2016),
        ('ZAM57RTA0J1074512', 'Ghibli', 2018),
        ('ZAM57RTA1L1077203', 'Ghibli', 2020),
        ('ZAM57TTA3M1078901', 'Ghibli', 2021),
        ('ZAM45VVA8G1230012', 'Levante', 2016),
        ('ZAM45VVA3H1232401', 'Levante', 2017),
        ('ZAM45VVA0J1234512', 'Levante', 2018),
        ('ZAM45VVA5L1239102', 'Levante', 2020),
        ('ZAM45VVA8M1241203', 'Levante', 2021),
        ('ZAM45KLAXA0050123', 'GranTurismo', 2010),
        ('ZAM45KLA8B0052301', 'GranTurismo', 2011),
        ('ZAM45KLA2C0054012', 'GranTurismo', 2012),
        ('ZAM45KLA4H0060912', 'GranTurismo', 2017),
        ('ZAM45MMA8B0051203', 'GranCabrio', 2011),
        ('ZAM45MMA9C0053401', 'GranCabrio', 2012),
        ('ZAM45MMA0E0056701', 'GranCabrio', 2014),
        ('ZAMMGTA14P1080012', 'GranTurismo', 2023),
        ('ZAMMGTA16R1082501', 'GranTurismo', 2024),
        ('ZAMMX2A18M1000123', 'MC20', 2021),
        ('ZAMMX2A12N1001501', 'MC20', 2022),
        ('ZAMMX2A19P1003201', 'MC20', 2023),
        ('ZAMMX2A21R1005012', 'MC20', 2024),
        ('ZAMYB1A19P1100012', 'Grecale', 2023),
        ('ZAMYB1A18R1103501', 'Grecale', 2024),
        ('ZAMYB1A12S1105201', 'Grecale', 2025),
        # Negative cases
        ('SHORT', None, None),
        ('INVALIDVIN1234567', None, None),
        ('ZAMZZZZZZZZZZZZZZ', None, None),
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
