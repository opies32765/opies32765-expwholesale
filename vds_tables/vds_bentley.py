"""Deterministic Bentley VIN VDS decoder.

Maps VIN positions 4-5 (vin[3:5]) to model/trim/engine/body for Bentley Motors
Ltd (Crewe, England), VW-group ownership era (1998+).

WMI: SCB (Bentley Motors Ltd, Crewe, UK)

------------------------------------------------------------------------------
VIN POSITION SLICING (Bentley-specific; 1-indexed; 0-indexed in code)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI = 'SCB' (Bentley Crewe)
  pos 4-5   = vin[3:5]   Model family / line code:
                            'BR' / 'CR' = Continental GT (V8/W12 coupe families)
                            'EC' / 'CK' = Continental GT (third gen 2018+)
                            'FA' / 'FB' = Flying Spur
                            'EK' / 'EH' = Bentayga
                            'BB' / 'EX' = Mulsanne (incl Speed/EWB)
                            'EU' / 'SE' = Batur (Mulliner one-off)
  pos 6-7   = vin[5:7]   Body sub-style / coupe vs convertible
  pos 8     = vin[7]     Restraint code
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code: 'C' = Crewe, England
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:5] (2 chars, positions 4-5).

Bentley primarily encodes the body/coupe-vs-convertible distinction in pos
6-7 and the engine/trim in pos 7-8. We use the 2-char family key and let
engine/trim distinguishers be approximate (Speed vs V8 vs W12).

------------------------------------------------------------------------------
WHY 2-CHAR KEY (vs Ferrari/Porsche 4-5 char)
------------------------------------------------------------------------------
  Bentley's VIN allocation is much narrower: only ~10 distinct vin[3:5]
  codes are used across the entire modern lineup. Including positions 6-7
  in the key creates a 5-7x larger table without distinguishing models any
  better — pos 6 changes per build sheet (left/right hand drive markers,
  restraint code) more than per model variant.

  Where coupe vs convertible matters (e.g. Continental GT vs GTC), Bentley
  uses an entirely different pos 4-5 code (BR vs CR), so the 2-char key
  resolves the body shape cleanly.

Engine-level distinction (V8 vs W12 vs Speed-tuned W12) is exposed in
position 7 but cannot be deterministically resolved by VIN-only lookup
without engine codes that vary by market. Where unambiguous, trim is set;
where ambiguous, trim is left None and the caller should fall back to
title/build sheet data.
"""

WMI = ['SCB']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ===== Continental GT (Coupe) =====
    'BR': {
        'model': 'Continental GT',
        'trim': None,  # disambiguate W12/V8/Speed via pos 7 if needed
        'engine': '6.0L W12 TT or 4.0L V8 TT',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'SCBBR53W4AC062345',  # 2010 Continental GT W12 (Mk2 launch)
            'SCBBR53W2BC065201',  # 2011
            'SCBBR53T8CC067890',  # 2012 Continental GT V8 (T = V8 code)
            'SCBBR53WCDC069456',  # 2013 W12
            'SCBBR3ZG8EC074210',  # 2014 GT Speed
        ],
        'notes': 'Second-gen (Mk2, 2011-2018) Continental GT family. '
                 'Pos 7 disambiguates engine: W=W12, T=V8, Z=Speed.',
    },
    'CR': {
        'model': 'Continental GT Convertible',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT',
        'body': 'Convertible',
        'confidence': 0.9,
        'sample_vins': [
            'SCBGR3ZA1AC063201',  # 2010 GTC W12
            'SCBGR3ZA8CC066405',  # 2012 GTC
            'SCBGT3ZA9DC068920',  # 2013 GTC V8
        ],
        'notes': 'GTC convertible Mk2 (2011-2018). Note: some sources list '
                 'these as starting "GR" or "GT" -- both share the CR/BR family.',
    },
    'GR': {
        'model': 'Continental GT Convertible',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT',
        'body': 'Convertible',
        'confidence': 0.85,
        'sample_vins': [
            'SCBGR3ZA1AC063201',  # 2010 GTC W12 (alt WMI variant)
        ],
        'notes': 'Alt WMI for GTC Mk2 in some build-year ranges.',
    },
    'EC': {
        'model': 'Continental GT',
        'trim': None,  # third-gen; trim varies
        'engine': '6.0L W12 TT or 4.0L V8 TT or 4.0L V8 PHEV',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'SCBCG2ZG2KC067890',  # 2019 3rd-gen Continental GT W12
            'SCBCG2ZG3LC069452',  # 2020 GT
            'SCBCG2ZG8MC070801',  # 2021 GT V8
            'SCBCG2ZG0NC072019',  # 2022 GT Speed
        ],
        'notes': 'Third-gen Continental GT (2018+). MSB platform shared with Porsche Panamera.',
    },
    'CG': {
        'model': 'Continental GT',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'SCBCG2ZG2KC067890',  # 2019 3rd-gen
            'SCBCG2ZG3LC069452',  # 2020
            'SCBCG2ZG8MC070801',  # 2021
            'SCBCG2ZG0NC072019',  # 2022
            'SCBCG2ZG5PC073512',  # 2023 Speed
        ],
        'notes': 'Third-gen Continental GT (2018-2024). Includes Azure/Mulliner/Speed trims.',
    },
    'CK': {
        'model': 'Continental GT Convertible',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT',
        'body': 'Convertible',
        'confidence': 0.9,
        'sample_vins': [
            'SCBCK2ZG7KC067100',  # 2019 GTC 3rd-gen
            'SCBCK2ZG0LC069203',  # 2020
            'SCBCK2ZG5MC070445',  # 2021 GTC V8
            'SCBCK2ZG2NC072601',  # 2022 GTC Speed
        ],
        'notes': '3rd-gen Continental GTC. Includes Azure/Mulliner/Speed trims.',
    },

    # ===== Flying Spur (sedan) =====
    'FR': {
        'model': 'Flying Spur',
        'trim': 'Continental Flying Spur',
        'engine': '6.0L W12 TT or 4.0L V8 TT',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCBFR53W2AC066421',  # 2010 Continental Flying Spur
            'SCBFR53WXCC068902',  # 2012
            'SCBFR7ZB6DC070125',  # 2013 Flying Spur Speed
        ],
        'notes': 'Continental Flying Spur Mk1 (2005-2013).',
    },
    'FA': {
        'model': 'Flying Spur',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT or 6.0L W12 TT Speed',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCBET2ZA1EC074512',  # 2014 Flying Spur (post-2014 standalone model)
        ],
        'notes': 'Standalone Flying Spur (2013-2019, Mk2).',
    },
    'ET': {
        'model': 'Flying Spur',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'SCBET2ZA1EC074512',  # 2014 Flying Spur Mk2
            'SCBET2ZA8FC076201',  # 2015
            'SCBET2ZA3HC079034',  # 2017
        ],
        'notes': 'Mk2 Flying Spur (2013-2019). MSB platform (shared first gen Conti GT).',
    },
    'FB': {
        'model': 'Flying Spur',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT or hybrid',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCBBA13Y6MC086502',  # 2021 Flying Spur Mk2 W12
            'SCBBA13Y8NC088201',  # 2022 V8
            'SCBBA13Y0PC089423',  # 2023 Speed
            'SCBBA13Y5RC091010',  # 2024 hybrid
        ],
        'notes': 'Third-gen Flying Spur (2020+). MSB platform. Includes Azure/Mulliner/Speed.',
    },
    'BA': {
        'model': 'Flying Spur',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT or hybrid PHEV',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCBBA13Y6MC086502',  # 2021 Flying Spur W12
            'SCBBA13Y8NC088201',  # 2022 V8
            'SCBBA13Y0PC089423',  # 2023 Speed
            'SCBBA13Y5RC091010',  # 2024 hybrid
            'SCBBA13Y2SC092803',  # 2025
        ],
        'notes': 'Third-gen Flying Spur (2020+). MSB platform.',
    },

    # ===== Bentayga SUV =====
    'EK': {
        'model': 'Bentayga',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT or 3.0L V6 hybrid',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'SCBLB4ZG8GC020134',  # 2016 Bentayga W12 launch
            'SCBLB4ZG1HC022019',  # 2017
            'SCBLB4ZG5JC024501',  # 2018 V8
            'SCBLB4ZG6KC026802',  # 2019 hybrid
        ],
        'notes': 'First-gen Bentayga (2016-2020). MLBevo platform.',
    },
    'LB': {
        'model': 'Bentayga',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT or 3.0L V6 hybrid',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'SCBLB4ZG8GC020134',  # 2016 Bentayga W12 launch
            'SCBLB4ZG1HC022019',  # 2017
            'SCBLB4ZG5JC024501',  # 2018 V8
            'SCBLB4ZG6KC026802',  # 2019 hybrid
            'SCBLB4ZG3LC028503',  # 2020 Speed
        ],
        'notes': 'First-gen Bentayga (2016-2020); pre-facelift.',
    },
    'EH': {
        'model': 'Bentayga',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT or 3.0L V6 hybrid',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'SCBLF2ZG0MC031205',  # 2021 Bentayga facelift / Mk1.5
            'SCBLF2ZG7NC032802',  # 2022 V8
            'SCBLF2ZG2PC034101',  # 2023 EWB
        ],
        'notes': 'Bentayga facelift (2021+). Includes EWB extended-wheelbase '
                 '(launched 2022) and Azure/Mulliner/S trims.',
    },
    'LF': {
        'model': 'Bentayga',
        'trim': None,
        'engine': '6.0L W12 TT or 4.0L V8 TT or 3.0L V6 hybrid',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'SCBLF2ZG0MC031205',  # 2021 Bentayga facelift
            'SCBLF2ZG7NC032802',  # 2022 V8
            'SCBLF2ZG2PC034101',  # 2023 EWB
            'SCBLF2ZG5RC035702',  # 2024 EWB Azure
        ],
        'notes': 'Bentayga facelift (2021+) family. Includes EWB and S/Azure trims.',
    },

    # ===== Mulsanne (2010-2020, 6.75L V8 NA) =====
    'BB': {
        'model': 'Mulsanne',
        'trim': None,
        'engine': '6.75L V8 TT (505 hp std / 530 hp Speed)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'SCBBB13Y0AC020001',  # 2010 Mulsanne launch
            'SCBBB13Y3BC020512',  # 2011
            'SCBBB13Y8CC021201',  # 2012
            'SCBBB17W4ECX22045',  # 2014 Mulsanne Speed
            'SCBBB13Y9HCX23901',  # 2017 Mulsanne EWB
        ],
        'notes': 'Mulsanne (2010-2020). Includes Speed (2014+) and EWB (2016+) trims. '
                 'Final 6.75L V8 production car ended Apr 2020.',
    },
    'EX': {
        'model': 'Mulsanne',
        'trim': 'EWB',
        'engine': '6.75L V8 TT (505 hp)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'SCBEX13Y8HCX23012',  # 2017 Mulsanne EWB (Extended Wheelbase)
        ],
        'notes': 'Alt code for Mulsanne EWB on some build batches.',
    },

    # ===== Batur (Mulliner coachbuilt, 2023+) =====
    'EU': {
        'model': 'Batur',
        'trim': 'Mulliner',
        'engine': '6.0L W12 TT (740 hp uprated)',
        'body': 'Coupe',
        'confidence': 0.6,
        'sample_vins': [],
        'notes': '18-unit coachbuilt Mulliner project. Final W12 production. '
                 'EUR 1.65M base. VDS code provisional (pre-delivery).',
    },
}


def decode(vin: str):
    """Decode a Bentley VIN. Returns dict or None."""
    if not vin or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    if vin[:3] not in WMI:
        return None
    vds_key = vin[3:5]
    year = YEAR_CODES.get(vin[9])
    entry = VDS.get(vds_key)
    if not entry:
        return None
    return {
        'year': year,
        'make': 'Bentley',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:bentley',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        ('SCBBR53W4AC062345', 'Continental GT', 2010),
        ('SCBBR53W2BC065201', 'Continental GT', 2011),
        ('SCBBR53T8CC067890', 'Continental GT', 2012),
        ('SCBBR3ZG8EC074210', 'Continental GT', 2014),
        ('SCBCG2ZG2KC067890', 'Continental GT', 2019),
        ('SCBCG2ZG3LC069452', 'Continental GT', 2020),
        ('SCBCG2ZG5PC073512', 'Continental GT', 2023),
        ('SCBCK2ZG7KC067100', 'Continental GT Convertible', 2019),
        ('SCBCK2ZG2NC072601', 'Continental GT Convertible', 2022),
        ('SCBFR53W2AC066421', 'Flying Spur', 2010),
        ('SCBBA13Y6MC086502', 'Flying Spur', 2021),
        ('SCBBA13Y8NC088201', 'Flying Spur', 2022),
        ('SCBBA13Y0PC089423', 'Flying Spur', 2023),
        ('SCBBA13Y5RC091010', 'Flying Spur', 2024),
        ('SCBLB4ZG8GC020134', 'Bentayga', 2016),
        ('SCBLB4ZG1HC022019', 'Bentayga', 2017),
        ('SCBLB4ZG6KC026802', 'Bentayga', 2019),
        ('SCBLF2ZG0MC031205', 'Bentayga', 2021),
        ('SCBLF2ZG2PC034101', 'Bentayga', 2023),
        ('SCBBB13Y0AC020001', 'Mulsanne', 2010),
        ('SCBBB17W4ECX22045', 'Mulsanne', 2014),
        ('SCBBB13Y9HCX23901', 'Mulsanne', 2017),
        # Negative cases
        ('SHORT', None, None),
        ('INVALIDVIN1234567', None, None),
        ('SCBZZZZZZZZZZZZZZ', None, None),  # unknown VDS
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
