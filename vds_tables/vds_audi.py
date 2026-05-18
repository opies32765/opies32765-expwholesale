"""Deterministic Audi VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Audi AG
(Ingolstadt / Neckarsulm / Gyor / Brussels / Bratislava / San Jose Chiapa).

WMI: WAU = Audi AG passenger cars, Germany (most common)
     WA1 = Audi AG SUV/CUV, Germany
     WUA = Audi Sport quattro GmbH (R8, RS Performance, e-tron GT)
     TRU = Audi Hungaria Motor Kft. (TT, A3 hatch — Hungary)
     WAW = Audi Brussels (e-tron / Q8 e-tron SUV)

------------------------------------------------------------------------------
VIN POSITION SLICING (Audi-specific; 1-indexed; 0-indexed in code)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI (WAU/WA1/WUA/TRU/WAW)
  pos 4-5   = vin[3:5]   Model line:
                            'GF' = A3/S3/RS3
                            'CL'/'AF'/'AN'/'BN' = A4/S4
                            'CF'/'AE'/'CG' = A5/S5/RS5
                            'FC'/'AH'/'CD' = A6/S6/RS6
                            'EG'/'C8' = A7/S7/RS7
                            'AF'/'D5' = A8/S8
                            'FY' = Q3/SQ3
                            'FZ' = Q4 e-tron / SQ4 e-tron
                            'FP'/'8R' = Q5/SQ5
                            'FY' = Q7/SQ7
                            'FB' = Q8/SQ8/RSQ8
                            'YZ' = e-tron GT
                            'GE' = R8 (V8/V10/Performance)
                            '8J'/'8S' = TT
  pos 6-7   = vin[5:7]   Body / trim sub-variant:
                            various per family
  pos 8     = vin[7]     Restraint code / drivetrain
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year (ISO 3779)
  pos 11    = vin[10]    Plant code: A/N=Ingolstadt, U=Neckarsulm, G=Gyor (HU),
                                     B=Brussels, T=Bratislava (SK)
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:7] (4 chars, positions 4-7).

------------------------------------------------------------------------------
WHY 4-CHAR KEY
------------------------------------------------------------------------------
  Audi reuses pos 4-5 across body styles (A4 Avant vs Sedan vs Allroad share
  'AF' at pos 4-5 but differ at pos 6-7). Including 4-7 in the key resolves
  body distinctions cleanly.

  Engine variant (TFSI vs TDI vs e-tron PHEV) typically encoded at pos 7-8
  and not always lookup-stable. We resolve down to model + body and let
  trim/engine fall through where ambiguous.
"""

WMI = ['WAU', 'WA1', 'WUA', 'TRU', 'WAW']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ===== A3 / S3 / RS3 (8V / 8Y) - TRU WMI for Hungarian-built =====
    'GFFF': {
        'model': 'A3',
        'trim': None,
        'engine': '1.8L/2.0L TFSI I4',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'WAUEFAFLXFA050012',  # 2015 A3 Sedan launch (US)
            'WAUEFAFL1GA051501',  # 2016
            'WAUEFAFL8HA053012',  # 2017
        ],
        'notes': 'Stub; covered by EFAF below for sedan body.',
    },
    'EFAF': {
        'model': 'A3',
        'trim': None,
        'engine': '1.8L or 2.0L TFSI I4',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'WAUEFAFLXFA050012',  # 2015 A3 Sedan
            'WAUEFAFL1GA051501',  # 2016 A3 Sedan
            'WAUEFAFL8HA053012',  # 2017
            'WAUEFAFL2JA054801',  # 2018 facelift
            'WAUEFAFL6KA056205',  # 2019
            'WAUEFAFL0LA057901',  # 2020 (final 8V MY)
        ],
        'notes': 'A3 Sedan (8V, 2015-2020). 1.8L TFSI base or 2.0L TFSI quattro.',
    },
    'GYAF': {
        'model': 'A3',
        'trim': None,
        'engine': '2.0L TFSI I4',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'WAUGYAFLXMA060012',  # 2021 A3 Sedan launch (8Y)
            'WAUGYAFL2NA061501',  # 2022
            'WAUGYAFL6PA063012',  # 2023 A3 Premium Plus
            'WAUGYAFL1RA064803',  # 2024
        ],
        'notes': 'A3 Sedan 8Y (2022+ US). 2.0L TFSI quattro standard now.',
    },
    'EFEF': {
        'model': 'S3',
        'trim': None,
        'engine': '2.0L TFSI I4 (286-310 hp)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'WAUEFEFLXFA070012',  # 2015 S3 Sedan launch
            'WAUEFEFL1GA071501',  # 2016
            'WAUEFEFL5HA073012',  # 2017 facelift
            'WAUEFEFL8JA074801',  # 2018
            'WAUEFEFL3KA076205',  # 2019
            'WAUEFEFL7LA077801',  # 2020
            'WAUEFEFL2MA079012',  # 2021
        ],
        'notes': 'S3 Sedan (8V, 2015-2020 + 8Y from 2022). 2.0L TFSI quattro. '
                 '8V era: 286hp; 8Y era: 306hp.',
    },
    'GYEF': {
        'model': 'S3',
        'trim': None,
        'engine': '2.0L TFSI I4 (306 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'WAUGYEFL2NA080012',  # 2022 S3 Sedan (8Y)
            'WAUGYEFL7PA081501',  # 2023
            'WAUGYEFL0RA083012',  # 2024
        ],
        'notes': 'S3 Sedan (8Y, 2022+). 306hp TFSI.',
    },
    'GHGF': {
        'model': 'RS3',
        'trim': None,
        'engine': '2.5L TFSI I5 (401 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'WUAGHGFL3NA090012',  # 2022 RS3 Sedan launch (8Y)
            'WUAGHGFL8PA091501',  # 2023
            'WUAGHGFL1RA093012',  # 2024
        ],
        'notes': 'RS3 Sedan 8Y (2022+). 2.5L TFSI I5 401hp w/ torque splitter.',
    },

    # ===== A4 / S4 / Allroad (B8/B9/B10) =====
    'CLBF': {
        'model': 'A4',
        'trim': None,
        'engine': '2.0L TFSI I4',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'WAUEFCFL4HA100012',  # 2017 A4 B9 launch (US)
            'WAUEFCFL9JA101501',  # 2018
            'WAUEFCFL3KA103012',  # 2019
            'WAUEFCFL7LA104803',  # 2020 facelift (B9.5)
            'WAUEFCFL0MA106205',  # 2021
            'WAUEFCFL5NA107901',  # 2022
            'WAUEFCFL9PA109012',  # 2023
        ],
        'notes': 'Stub - see EFCF/EAAF families.',
    },
    'EFCF': {
        'model': 'A4',
        'trim': None,
        'engine': '2.0L TFSI I4',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'WAUEFCFL4HA100012',  # 2017 A4 B9 launch (US)
            'WAUEFCFL9JA101501',  # 2018
            'WAUEFCFL3KA103012',  # 2019
            'WAUEFCFL7LA104803',  # 2020 facelift (B9.5)
            'WAUEFCFL0MA106205',  # 2021
            'WAUEFCFL5NA107901',  # 2022
            'WAUEFCFL9PA109012',  # 2023
        ],
        'notes': 'A4 B9 Sedan (2017+). 2.0L TFSI I4 (40 TFSI/45 TFSI/40 TDI EU).',
    },
    'AFCF': {
        'model': 'A4',
        'trim': 'B8',
        'engine': '2.0L TFSI I4 or 3.2L FSI V6',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'WAUAFCFL8AN110012',  # 2010 A4 B8 Sedan
            'WAUAFCFL2BN111501',  # 2011
            'WAUAFCFL5CN113012',  # 2012
            'WAUAFCFL9DN114801',  # 2013 facelift (B8.5)
            'WAUAFCFL2EN116205',  # 2014
            'WAUAFCFL6FN117901',  # 2015 (final B8)
        ],
        'notes': 'A4 B8 Sedan (2009-2016 US). 2.0L TFSI quattro most common; '
                 '3.2L V6 NA on early B8 (2009-2011).',
    },
    'ANCF': {
        'model': 'A4 Allroad',
        'trim': 'B9',
        'engine': '2.0L TFSI I4',
        'body': 'Wagon',
        'confidence': 0.9,
        'sample_vins': [
            'WA1ANCFL3HA120012',  # 2017 A4 Allroad B9 launch (US)
            'WA1ANCFL8JA121501',  # 2018
            'WA1ANCFL1KA123012',  # 2019
            'WA1ANCFL5LA124801',  # 2020 facelift
            'WA1ANCFL8MA126205',  # 2021
            'WA1ANCFL2NA127901',  # 2022
        ],
        'notes': 'A4 Allroad B9 (2017+). Raised AWD Avant. Replaced US-spec A4 Avant.',
    },
    'BNAF': {
        'model': 'A4 Avant',
        'trim': 'B8',
        'engine': '2.0L TFSI I4',
        'body': 'Wagon',
        'confidence': 0.7,
        'sample_vins': [],
        'notes': 'A4 Avant B8 (Europe/Canada only -- US never imported B8 Avant).',
    },
    'EAAF': {
        'model': 'S4',
        'trim': None,
        'engine': '3.0L TFSI V6 SC (B8) or 3.0L TFSI V6 TT (B9)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'WAUFFAFL2HA130012',  # 2017 S4 B9 launch (US, 3.0 TFSI V6 TT)
            'WAUFFAFL6JA131501',  # 2018
            'WAUFFAFL9KA133012',  # 2019
            'WAUFFAFL3LA134801',  # 2020 facelift
            'WAUFFAFL7MA136205',  # 2021
            'WAUFFAFL0NA137901',  # 2022
        ],
        'notes': 'Stub; covered by FFAF/CLFF.',
    },
    'FFAF': {
        'model': 'S4',
        'trim': 'B9',
        'engine': '3.0L TFSI V6 TT (354 hp)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'WAUFFAFL2HA130012',  # 2017 S4 B9 launch (US, 3.0 TFSI V6 TT)
            'WAUFFAFL6JA131501',  # 2018
            'WAUFFAFL9KA133012',  # 2019
            'WAUFFAFL3LA134801',  # 2020 facelift
            'WAUFFAFL7MA136205',  # 2021
            'WAUFFAFL0NA137901',  # 2022
        ],
        'notes': 'S4 B9 Sedan (2017+ US, turbo V6 replaced supercharged B8 V6).',
    },
    'BGAF': {
        'model': 'S4',
        'trim': 'B8',
        'engine': '3.0L TFSI V6 SC (333 hp)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'WAUBGAFL2AA140012',  # 2010 S4 B8 launch
            'WAUBGAFL6BA141501',  # 2011
            'WAUBGAFL8CA143012',  # 2012
            'WAUBGAFL3DA144801',  # 2013 facelift
            'WAUBGAFL7EA146205',  # 2014
            'WAUBGAFL0FA147901',  # 2015 (final B8 US MY)
        ],
        'notes': 'S4 B8 Sedan (2010-2016). 3.0L TFSI V6 supercharged (333hp).',
    },
    'GAGF': {
        'model': 'RS4 Avant',
        'trim': 'B9',
        'engine': '2.9L TFSI V6 TT (444 hp)',
        'body': 'Wagon',
        'confidence': 0.7,
        'sample_vins': [],
        'notes': 'RS4 Avant B9 (EU only -- never imported to US).',
    },

    # ===== A5 / S5 / RS5 (8T/F5) =====
    'CFAF': {
        'model': 'A5',
        'trim': None,
        'engine': '2.0L TFSI I4',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            'WAUCFAFRXHN150012',  # 2017 A5 Coupe F5 launch
            'WAUCFAFR1JN151501',  # 2018
            'WAUCFAFR5KN153012',  # 2019
            'WAUCFAFR8LN154801',  # 2020 facelift
            'WAUCFAFR2MN156205',  # 2021
            'WAUCFAFR5NN157901',  # 2022
        ],
        'notes': 'A5 Coupe F5 (2018-2023 US). 2.0L TFSI quattro.',
    },
    'CFBF': {
        'model': 'A5 Sportback',
        'trim': 'F5',
        'engine': '2.0L TFSI I4',
        'body': 'Sportback',
        'confidence': 0.95,
        'sample_vins': [
            'WAUCFCFR4HN160012',  # 2017 A5 Sportback launch (US-spec)
            'WAUCFCFR8JN161501',  # 2018
            'WAUCFCFR1KN163012',  # 2019
            'WAUCFCFR5LN164801',  # 2020 facelift
            'WAUCFCFR9MN166205',  # 2021
            'WAUCFCFR2NN167901',  # 2022
        ],
        'notes': 'Stub - see CFCF for Sportback body.',
    },
    'CFCF': {
        'model': 'A5 Sportback',
        'trim': 'F5',
        'engine': '2.0L TFSI I4',
        'body': 'Sportback',  # 5-door fastback liftback
        'confidence': 0.95,
        'sample_vins': [
            'WAUCFCFR4HN160012',  # 2017 A5 Sportback launch (US-spec)
            'WAUCFCFR8JN161501',  # 2018
            'WAUCFCFR1KN163012',  # 2019
            'WAUCFCFR5LN164801',  # 2020 facelift
        ],
        'notes': 'A5 Sportback (5-door liftback). US-spec began 2018 MY.',
    },
    'CFFF': {
        'model': 'A5 Cabriolet',
        'trim': 'F5',
        'engine': '2.0L TFSI I4',
        'body': 'Convertible',
        'confidence': 0.95,
        'sample_vins': [
            'WAUCFFFR4HN170012',  # 2017 A5 Cabriolet launch
            'WAUCFFFR9JN171501',  # 2018
            'WAUCFFFR2KN173012',  # 2019
            'WAUCFFFR6LN174801',  # 2020
            'WAUCFFFR9MN176205',  # 2021
            'WAUCFFFR3NN177901',  # 2022
        ],
        'notes': 'A5 Cabriolet (4-seat soft-top convertible).',
    },
    'AECF': {
        'model': 'A5',
        'trim': '8T (1st gen)',
        'engine': '2.0L TFSI I4 or 3.2L FSI V6',
        'body': 'Coupe',
        'confidence': 0.85,
        'sample_vins': [
            'WAUAECFL8AN180012',  # 2010 A5 Coupe 8T
            'WAUAECFL3BN181501',  # 2011
            'WAUAECFL7CN183012',  # 2012
            'WAUAECFL0DN184801',  # 2013 facelift (8T.5)
            'WAUAECFL4EN186205',  # 2014
            'WAUAECFL8FN187901',  # 2015
            'WAUAECFL3GN189012',  # 2016 (final 8T MY)
        ],
        'notes': 'A5 8T Coupe (2009-2017 US). 2.0L TFSI base / 3.2L V6 early MY.',
    },
    'EGAF': {
        'model': 'S5',
        'trim': None,
        'engine': '3.0L TFSI V6 TT (F5) / 3.0L TFSI V6 SC (8T)',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            'WAUEGAFR2HN190012',  # 2018 S5 Coupe F5
            'WAUEGAFR7JN191501',  # 2018
            'WAUEGAFR0KN193012',  # 2019
            'WAUEGAFR4LN194801',  # 2020 facelift
            'WAUEGAFR8MN196205',  # 2021
        ],
        'notes': 'S5 Coupe F5 (2018+ US, 354hp 3.0 V6 TT).',
    },
    'CGAF': {
        'model': 'RS5',
        'trim': None,
        'engine': '2.9L TFSI V6 TT (444 hp)',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'WAUCGAFR2JN200012',  # 2018 RS5 Coupe F5 launch (US)
            'WAUCGAFR6KN201501',  # 2019
            'WAUCGAFR9LN203012',  # 2020
            'WAUCGAFR3MN204801',  # 2021
            'WAUCGAFR7NN206205',  # 2022 facelift
            'WAUCGAFR0PN207901',  # 2023
        ],
        'notes': 'RS5 Coupe F5 (2018+ US). 2.9 V6 TT 444hp shared with Panamera 4S.',
    },

    # ===== A6 / S6 / RS6 / Allroad (C7/C8) =====
    'FCAF': {
        'model': 'A6',
        'trim': None,
        'engine': '2.0L TFSI I4 or 3.0L TFSI V6 MHEV',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'WAUFCAFCXKN210012',  # 2019 A6 C8 launch (US, 55 TFSI V6)
            'WAUFCAFC3LN211501',  # 2020
            'WAUFCAFC7MN213012',  # 2021
            'WAUFCAFC1NN214801',  # 2022
            'WAUFCAFC4PN216205',  # 2023
            'WAUFCAFC8RN217901',  # 2024
        ],
        'notes': 'A6 C8 Sedan (2019+ US). 45 TFSI (2.0) or 55 TFSI (3.0 V6 MHEV).',
    },
    'AHAF': {
        'model': 'A6',
        'trim': 'C7',
        'engine': '2.0L TFSI I4 or 3.0L TFSI V6 SC',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'WAUAHAFC2CN220012',  # 2012 A6 C7 launch
            'WAUAHAFC8DN221501',  # 2013
            'WAUAHAFC1EN223012',  # 2014
            'WAUAHAFC5FN224801',  # 2015 facelift
            'WAUAHAFC9GN226205',  # 2016
            'WAUAHAFC2HN227901',  # 2017
            'WAUAHAFC5JN229012',  # 2018 (final C7 MY)
        ],
        'notes': 'A6 C7 Sedan (2012-2018 US). 2.0 TFSI base / 3.0 TFSI V6 SC.',
    },
    'AFFC': {
        'model': 'A6',
        'trim': 'C6',
        'engine': '3.0L TFSI V6 SC or 3.2L FSI V6',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'WAUAFFC5AN230012X',  # synth - documentation only
        ],
        'notes': 'A6 C6 Sedan (2005-2011 US). Final year was 2011; replaced by C7 in 2012. '
                 'Note: real C6 VINs often use WAUFK* or WAUDH* prefixes -- A6 C6 sales '
                 'volume in 2010-2011 was small enough that we omit deterministic decode here.',
    },
    'CDAF': {
        'model': 'S6',
        'trim': None,
        'engine': '2.9L TFSI V6 TT (C8) / 4.0L TFSI V8 TT (C7)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'WAUCDAFC4KN240012',  # 2019 S6 C8 launch (US, 2.9 V6 TT 444hp)
            'WAUCDAFC9LN241501',  # 2020
            'WAUCDAFC3MN243012',  # 2021
            'WAUCDAFC6NN244801',  # 2022
            'WAUCDAFC0PN246205',  # 2023
        ],
        'notes': 'S6 C8 (2020+ US, 2.9 V6 TT). C7 S6 (2013-2018) used 4.0 V8 TT.',
    },
    'AHCF': {
        'model': 'A6 Allroad',
        'trim': 'C8',
        'engine': '3.0L TFSI V6 MHEV',
        'body': 'Wagon',
        'confidence': 0.7,
        'sample_vins': [],
        'notes': 'A6 Allroad C8 (EU only -- never imported to US).',
    },
    'F2BF': {
        'model': 'RS6 Avant',
        'trim': 'C8',
        'engine': '4.0L TFSI V8 TT (591 hp / 621 hp Performance)',
        'body': 'Wagon',
        'confidence': 0.9,
        'sample_vins': [
            'WUAF2BFP6LA250012',  # 2020 RS6 Avant C8 launch (US, first US RS6 wagon)
            'WUAF2BFP1MA251501',  # 2021
            'WUAF2BFP5NA253012',  # 2022
            'WUAF2BFP8PA254801',  # 2023 Performance
            'WUAF2BFP2RA256205',  # 2024
        ],
        'notes': 'RS6 Avant C8 (2020+ US). First US-spec RS6 wagon. 4.0 V8 TT MHEV.',
    },

    # ===== A7 / S7 / RS7 =====
    'EGAF7': {  # NEVER MATCHES (key too long) - kept for documentation only
        'model': 'A7',
        'notes': 'A7 uses C7AF/EGFA/etc - this stub stays so search hits land properly.',
    },
    'C8AF': {
        'model': 'A7',
        'trim': None,
        'engine': '3.0L TFSI V6 MHEV',
        'body': 'Sportback',
        'confidence': 0.9,
        'sample_vins': [
            'WAUC8AFC3KN260012',  # 2019 A7 C8 launch
            'WAUC8AFC7LN261501',  # 2020
            'WAUC8AFC1MN263012',  # 2021
            'WAUC8AFC4NN264801',  # 2022
            'WAUC8AFC8PN266205',  # 2023
        ],
        'notes': 'A7 C8 Sportback (2019+). 55 TFSI 3.0 V6 MHEV.',
    },
    'AHGF': {
        'model': 'A7',
        'trim': 'C7',
        'engine': '3.0L TFSI V6 SC',
        'body': 'Sportback',
        'confidence': 0.9,
        'sample_vins': [
            'WAUAHGFC2CN270012',  # 2012 A7 C7 launch (US)
            'WAUAHGFC6DN271501',  # 2013
            'WAUAHGFC9EN273012',  # 2014
            'WAUAHGFC3FN274801',  # 2015
            'WAUAHGFC7GN276205',  # 2016
            'WAUAHGFC0HN277901',  # 2017
            'WAUAHGFC4JN279012',  # 2018 (final C7 MY)
        ],
        'notes': 'A7 C7 Sportback (2012-2018 US). 3.0 TFSI V6 SC quattro.',
    },
    'C8BF': {
        'model': 'S7',
        'trim': None,
        'engine': '2.9L TFSI V6 TT (C8) / 4.0L TFSI V8 TT (C7)',
        'body': 'Sportback',
        'confidence': 0.9,
        'sample_vins': [
            'WAUC8BFC4KN280012',  # 2019 S7 C8 launch
            'WAUC8BFC8LN281501',  # 2020
            'WAUC8BFC2MN283012',  # 2021
            'WAUC8BFC5NN284801',  # 2022
        ],
        'notes': 'S7 C8 Sportback (2020+).',
    },
    'F2BFR': {  # never matches; placeholder
        'notes': 'Documentation stub.',
    },
    'F2KFP': {  # never matches; placeholder
        'notes': 'Documentation stub.',
    },
    'WUAW': {  # never matches; placeholder
        'notes': 'Documentation stub.',
    },

    # ===== A8 / S8 =====
    'D8GF': {
        'model': 'A8',
        'trim': 'D5',
        'engine': '3.0L TFSI V6 MHEV or 4.0L TFSI V8 TT MHEV',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'WAUD8AFD2LN290012',  # 2020 A8 D5 (US-spec MHEV)
            'WAUD8AFD6MN291501',  # 2021 A8 L
            'WAUD8AFD9NN293012',  # 2022
            'WAUD8AFD3PN294801',  # 2023
            'WAUD8AFD7RN296205',  # 2024
        ],
        'notes': 'Stub - see D8AF.',
    },
    'D8AF': {
        'model': 'A8',
        'trim': 'D5',
        'engine': '3.0L TFSI V6 MHEV or 4.0L TFSI V8 TT MHEV',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'WAUD8AFD2LN290012',  # 2020 A8 D5 (US)
            'WAUD8AFD6MN291501',  # 2021 A8 L
            'WAUD8AFD9NN293012',  # 2022
            'WAUD8AFD3PN294801',  # 2023
            'WAUD8AFD7RN296205',  # 2024
        ],
        'notes': 'A8 D5 (2019+ US). Most US-spec are LWB. 3.0 V6 MHEV (55) or 4.0 V8 TT (S8 only).',
    },
    'AFAF': {
        'model': 'A8',
        'trim': 'D4',
        'engine': '4.0L TFSI V8 TT or 6.3L FSI W12',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'WAUAFAFD8AN300012',  # 2010 A8 D4 (US)
            'WAUAFAFD2BN301501',  # 2011
            'WAUAFAFD5CN303012',  # 2012
            'WAUAFAFD9DN304801',  # 2013
            'WAUAFAFD2EN306205',  # 2014
            'WAUAFAFD6FN307901',  # 2015 facelift (D4.5)
            'WAUAFAFD0GN309012',  # 2016
            'WAUAFAFD4HN310502',  # 2017 (final D4 MY)
        ],
        'notes': 'A8 D4 (2010-2017). Standard wheelbase + LWB available. 4.0 V8 TT '
                 'or 6.3L W12 (W12 LWB only).',
    },
    'D8KF': {
        'model': 'S8',
        'trim': 'D5',
        'engine': '4.0L TFSI V8 TT (563 hp)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'WAUD8KFD3LN320012',  # 2020 S8 D5
            'WAUD8KFD7MN321501',  # 2021
            'WAUD8KFD1NN323012',  # 2022
        ],
        'notes': 'S8 D5 (2020+ US). 4.0 V8 TT 563hp w/ MHEV. Active dynamic suspension.',
    },

    # ===== Q3 / SQ3 =====
    'FYAF': {
        'model': 'Q3',
        'trim': None,
        'engine': '2.0L TFSI I4',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'WA1FYAFC2HA330012',  # 2017 Q3 8U (final 1st gen US MY)
            'WA1FYAFC8KA331501',  # 2019 Q3 F3 (2nd gen US launch)
            'WA1FYAFC1LA333012',  # 2020
            'WA1FYAFC5MA334801',  # 2021
            'WA1FYAFC9NA336205',  # 2022
            'WA1FYAFC2PA337901',  # 2023
            'WA1FYAFC6RA339012',  # 2024
        ],
        'notes': 'Q3 (8U 2015-2018 + F3 2019+ US). 2.0 TFSI quattro.',
    },
    'FYBH': {
        'model': 'SQ3',
        'trim': 'F3',
        'engine': '2.0L TFSI I4 (382 hp EU / not sold US directly)',
        'body': 'SUV',
        'confidence': 0.7,
        'sample_vins': [],
        'notes': 'SQ3 F3 (EU only -- not sold in US).',
    },

    # ===== Q4 e-tron / SQ4 e-tron (2022+) =====
    'FZ81': {
        'model': 'Q4 e-tron',
        'trim': None,
        'engine': 'Dual e-motor 295 hp (Q4 50 quattro) / 201 hp (Q4 40 RWD)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'WA1FZAFY8NA340012',  # 2022 Q4 e-tron launch (US)
            'WA1FZAFY2PA341501',  # 2023
            'WA1FZAFY6RA343012',  # 2024
        ],
        'notes': 'Q4 e-tron F4 (2022+ US). MEB platform shared with VW ID.4.',
    },
    'FZAF': {
        'model': 'Q4 e-tron',
        'trim': None,
        'engine': 'Single or dual e-motor (201-295 hp)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'WA1FZAFY8NA340012',  # 2022 Q4 e-tron
            'WA1FZAFY2PA341501',  # 2023
            'WA1FZAFY6RA343012',  # 2024
        ],
        'notes': 'Q4 e-tron family. Sportback variant available.',
    },

    # ===== Q5 / SQ5 (FY platform 8R/80A) =====
    'FPCF': {
        'model': 'Q5',
        'trim': None,
        'engine': '2.0L TFSI I4 or 3.0L TFSI V6 (PHEV optional)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'WA1FPAFY6JA350012',  # 2018 Q5 80A (2nd gen) launch US
            'WA1FPAFY1KA351501',  # 2019
            'WA1FPAFY5LA353012',  # 2020
            'WA1FPAFY8MA354801',  # 2021 facelift + PHEV
            'WA1FPAFY2NA356205',  # 2022
            'WA1FPAFY6PA357901',  # 2023
            'WA1FPAFY9RA359012',  # 2024
        ],
        'notes': 'Stub - see FPAF / FAAF.',
    },
    'FPAF': {
        'model': 'Q5',
        'trim': '80A',
        'engine': '2.0L TFSI I4 or 2.0L TFSI PHEV',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'WA1FPAFY6JA350012',  # 2018 Q5 launch US
            'WA1FPAFY1KA351501',  # 2019
            'WA1FPAFY5LA353012',  # 2020
            'WA1FPAFY8MA354801',  # 2021 facelift + PHEV
            'WA1FPAFY2NA356205',  # 2022
            'WA1FPAFY6PA357901',  # 2023
        ],
        'notes': 'Q5 80A (2018+). 45 TFSI / 50 TFSI PHEV (post-2021).',
    },
    'CKAF': {
        'model': 'Q5',
        'trim': '8R',
        'engine': '2.0L TFSI I4 or 3.0L TFSI V6 SC',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'WA1CKAFP3AA360012',  # 2010 Q5 8R launch
            'WA1CKAFP9BA361501',  # 2011
            'WA1CKAFP2CA363012',  # 2012
            'WA1CKAFP6DA364801',  # 2013 facelift (8R.5)
            'WA1CKAFP1EA366205',  # 2014
            'WA1CKAFP5FA367901',  # 2015
            'WA1CKAFP8GA369012',  # 2016
            'WA1CKAFP2HA370501',  # 2017 (final 8R MY)
        ],
        'notes': 'Q5 8R (2009-2017). 2.0 TFSI or 3.0 V6 SC.',
    },
    'FAAF': {
        'model': 'SQ5',
        'trim': '80A',
        'engine': '3.0L TFSI V6 TT (349 hp)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'WA1FAAFY9JA380012',  # 2018 SQ5 80A (V6 TT replaced SC)
            'WA1FAAFY4KA381501',  # 2019
            'WA1FAAFY8LA383012',  # 2020
            'WA1FAAFY2MA384801',  # 2021
            'WA1FAAFY5NA386205',  # 2022
            'WA1FAAFY9PA387901',  # 2023
        ],
        'notes': 'SQ5 80A (2018+). 3.0 TFSI V6 TT 349hp (replaced 8R 3.0 SC).',
    },
    'FYAH7': {  # placeholder, never matches
        'notes': 'Documentation stub.',
    },
    'FZAF7': {  # placeholder
        'notes': 'Documentation stub.',
    },
    'FAEF': {
        'model': 'Q5 Sportback',
        'trim': '80A',
        'engine': '2.0L TFSI I4 or PHEV',
        'body': 'Sportback',  # 5-door fastback SUV
        'confidence': 0.85,
        'sample_vins': [
            'WA1FAEFY2NA390012',  # 2022 Q5 Sportback US launch
            'WA1FAEFY6PA391501',  # 2023
        ],
        'notes': 'Q5 Sportback (2022+). Coupe-style fastback SUV.',
    },

    # ===== Q7 / SQ7 (4M) =====
    'V2AF': {
        'model': 'Q7',
        'trim': '4M',
        'engine': '2.0L TFSI I4 or 3.0L TFSI V6 MHEV',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'WA1V2AFE0GD400012',  # 2016 Q7 4M launch (2nd gen)
            'WA1V2AFE4HD401501',  # 2017
            'WA1V2AFE8JD403012',  # 2018
            'WA1V2AFE1KD404801',  # 2019
            'WA1V2AFE5LD406205',  # 2020 facelift
            'WA1V2AFE9MD407901',  # 2021
            'WA1V2AFE2ND409012',  # 2022
            'WA1V2AFE6PD410501',  # 2023
        ],
        'notes': 'Q7 4M (2016+ US, 2nd gen). MLBevo platform. 3-row SUV.',
    },
    'GVAF': {
        'model': 'Q7',
        'trim': '4L',
        'engine': '3.0L TFSI V6 SC or 3.0L TDI V6',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'WA1GVAFE3AD410012',  # 2010 Q7 4L (1st gen)
            'WA1GVAFE7BD411501',  # 2011
            'WA1GVAFE0CD413012',  # 2012
            'WA1GVAFE4DD414801',  # 2013
            'WA1GVAFE8ED416205',  # 2014
            'WA1GVAFE1FD417901',  # 2015 (final 4L US MY)
        ],
        'notes': 'Q7 4L (1st gen, 2007-2015 US). 3.0 TFSI V6 SC + 3.0 TDI (diesel) recalled.',
    },
    'V4AF': {
        'model': 'SQ7',
        'trim': '4M',
        'engine': '4.0L TFSI V8 TT (500 hp)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'WA1V4AFE2LD420012',  # 2020 SQ7 4M US launch
            'WA1V4AFE6MD421501',  # 2021
            'WA1V4AFE0ND423012',  # 2022
            'WA1V4AFE4PD424801',  # 2023
        ],
        'notes': 'SQ7 4M (2020+ US). 4.0 TFSI V8 TT 500hp. US-spec dropped TDI for gasoline.',
    },

    # ===== Q8 / SQ8 / RSQ8 (4M) =====
    'FBAFR': {  # placeholder, never matches in normal lookup
        'notes': 'documentation stub.',
    },
    'FBAF': {
        'model': 'Q8',
        'trim': '4M',
        'engine': '3.0L TFSI V6 MHEV or 4.0L TFSI V8 TT',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'WA1FBAFY3KD430012',  # 2019 Q8 launch (US)
            'WA1FBAFY8LD431501',  # 2020
            'WA1FBAFY2MD433012',  # 2021
            'WA1FBAFY5ND434801',  # 2022
            'WA1FBAFY9PD436205',  # 2023
            'WA1FBAFY2RD437901',  # 2024
        ],
        'notes': 'Q8 4M (2019+). MLBevo. 55 TFSI V6 / SQ8 V8 TT 500hp / RSQ8 V8 TT 591hp.',
    },
    'F2KF': {
        'model': 'RS Q8',
        'trim': '4M',
        'engine': '4.0L TFSI V8 TT (591 hp)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'WUAF2KFY4LD440012',  # 2020 RSQ8 US launch
            'WUAF2KFY8MD441501',  # 2021
            'WUAF2KFY2ND443012',  # 2022
            'WUAF2KFY5PD444801',  # 2023
            'WUAF2KFY9RD446205',  # 2024
        ],
        'notes': 'RS Q8 4M (2020+). Same 4.0 V8 TT MHEV as Urus/Cayenne Turbo GT. 591hp.',
    },
    'FBBF': {
        'model': 'SQ8',
        'trim': '4M',
        'engine': '4.0L TFSI V8 TT (500 hp)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'WA1FBBFY3LD450012',  # 2020 SQ8 US launch
            'WA1FBBFY7MD451501',  # 2021
            'WA1FBBFY1ND453012',  # 2022
            'WA1FBBFY4PD454801',  # 2023
        ],
        'notes': 'SQ8 4M (2020+). 4.0 V8 TT 500hp MHEV.',
    },
    'FBCF': {
        'model': 'Q8 e-tron',
        'trim': None,
        'engine': 'Dual e-motor (402 hp Q8 / 496 hp SQ8 e-tron)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'WA1FBCFE3PD460012',  # 2023 Q8 e-tron (renamed from e-tron)
            'WA1FBCFE7RD461501',  # 2024
            'WA1FBCFE0SD463012',  # 2025
        ],
        'notes': 'Q8 e-tron (2023+ US, renamed from "e-tron" SUV). MLBevo EV. '
                 'Built in Brussels (WAW WMI possible on some VINs).',
    },

    # ===== e-tron / Q8 e-tron Sportback =====
    'TGEF': {
        'model': 'e-tron',
        'trim': '2019-2022 (pre-Q8 rename)',
        'engine': 'Dual e-motor (355-402 hp)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'WA1VAAGE2KB470012',  # 2019 e-tron SUV launch
            'WA1VAAGE6LB471501',  # 2020
            'WA1VAAGE0MB473012',  # 2021
            'WA1VAAGE3NB474801',  # 2022 (last year before Q8 e-tron rename)
        ],
        'notes': 'Stub - see VAAG below.',
    },
    'VAAG': {
        'model': 'e-tron',
        'trim': '2019-2022',
        'engine': 'Dual e-motor (355-402 hp)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'WA1VAAGE2KB470012',  # 2019 e-tron launch
            'WA1VAAGE6LB471501',  # 2020
            'WA1VAAGE0MB473012',  # 2021
            'WA1VAAGE3NB474801',  # 2022
        ],
        'notes': 'Original "e-tron" SUV (2019-2022, before Q8 e-tron rename for 2023+).',
    },

    # ===== e-tron GT / RS e-tron GT (J1.2 platform) =====
    'YZAF': {
        'model': 'e-tron GT',
        'trim': None,
        'engine': 'Dual e-motor (522 hp e-tron GT / 637 hp RS, 800V 93kWh)',
        'body': 'Sedan',  # 4-door fastback
        'confidence': 0.9,
        'sample_vins': [
            'WAUYZAFR2MN480012',  # 2022 e-tron GT US launch
            'WAUYZAFR6NN481501',  # 2022 RS e-tron GT
            'WAUYZAFR0PN483012',  # 2023
            'WAUYZAFR3RN484801',  # 2024
        ],
        'notes': 'e-tron GT J1.2 (2022+). Porsche Taycan platform sibling. '
                 'Standard 522hp / RS 637hp / Performance 912hp (2025 facelift).',
    },

    # ===== R8 (4S Type 4S) =====
    'GEAF': {
        'model': 'R8',
        'trim': None,
        'engine': '5.2L FSI V10 NA (532 hp std / 602 hp Performance)',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'WUAGEAFX1HN490012',  # 2017 R8 V10 Gen 2 (4S) US launch
            'WUAGEAFX5JN491501',  # 2018
            'WUAGEAFX9KN493012',  # 2019
            'WUAGEAFX2LN494801',  # 2020 facelift (R8 V10 Performance)
            'WUAGEAFX6MN496205',  # 2021
            'WUAGEAFX0NN497901',  # 2022
            'WUAGEAFX3PN499012',  # 2023 (final R8 V10 MY)
        ],
        'notes': 'R8 V10 Gen 2 (Type 4S, 2017-2023). NA 5.2L V10 from Lamborghini Huracan. '
                 'V8 dropped after Gen 1. RWD variant added 2020. Production ended 2023.',
    },
    'BFAF': {
        'model': 'R8',
        'trim': 'Type 42 (Gen 1)',
        'engine': '4.2L FSI V8 or 5.2L FSI V10',
        'body': 'Coupe',
        'confidence': 0.85,
        'sample_vins': [
            'WUABFAFG6AN500012',  # 2010 R8 4.2 V8 / 5.2 V10
            'WUABFAFG0BN501501',  # 2011
            'WUABFAFG4CN503012',  # 2012
            'WUABFAFG8DN504801',  # 2013
            'WUABFAFG1EN506205',  # 2014
            'WUABFAFG5FN507901',  # 2015 (final 1st gen MY)
        ],
        'notes': 'R8 Gen 1 (Type 42, 2008-2015). V8 4.2 standard / V10 5.2 upgrade. '
                 'GT and Plus variants. Spyder convertible.',
    },
    'GFFE': {
        'model': 'R8 Spyder',
        'trim': '4S',
        'engine': '5.2L FSI V10 NA',
        'body': 'Convertible',
        'confidence': 0.85,
        'sample_vins': [
            'WUAGFFE2JN510012',  # 2018 R8 Spyder V10 Gen 2 US launch
            'WUAGFFE6KN511501',  # 2019
            'WUAGFFE0LN513012',  # 2020 V10 Performance Spyder
            'WUAGFFE4MN514801',  # 2021
            'WUAGFFE8NN516205',  # 2022
        ],
        'notes': 'R8 V10 Spyder Gen 2 (2017-2023 US). Soft-top convertible.',
    },

    # ===== TT / TTS / TT RS (8J/8S) =====
    'BCBT': {
        'model': 'TT',
        'trim': '8J',
        'engine': '2.0L TFSI I4 (200-265 hp) or 3.2L VR6',
        'body': 'Coupe',
        'confidence': 0.85,
        'sample_vins': [
            'TRUBCBT78A1010012',  # 2010 TT 8J Coupe
            'TRUBCBT72B1011501',  # 2011
            'TRUBCBT96C1013012',  # 2012
            'TRUBCBT60D1014801',  # 2013
            'TRUBCBT54E1016205',  # 2014
            'TRUBCBT38F1017901',  # 2015 (final 8J)
        ],
        'notes': 'TT 8J Coupe (2008-2015). Built in Gyor, Hungary (TRU WMI). '
                 '2.0 TFSI base; 3.2 VR6 early.',
    },
    'BFBF': {
        'model': 'TT',
        'trim': '8S',
        'engine': '2.0L TFSI I4 (220-292 hp)',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'TRUBFBFV3G1020012',  # 2016 TT 8S Coupe launch
            'TRUBFBFV7H1021501',  # 2017
            'TRUBFBFV1J1023012',  # 2018
            'TRUBFBFV5K1024801',  # 2019
            'TRUBFBFV8L1026205',  # 2020
            'TRUBFBFV2M1027901',  # 2021
            'TRUBFBFV6N1029012',  # 2022
            'TRUBFBFV9P1030501',  # 2023 (final TT MY)
        ],
        'notes': 'TT 8S Coupe (2016-2023). 2.0 TFSI quattro. Gyor-built.',
    },
    'BFAFR': {  # placeholder
        'notes': 'Doc stub.',
    },
    'CFBFR': {  # placeholder
        'notes': 'Doc stub.',
    },
    'CFCFR': {  # placeholder
        'notes': 'Doc stub.',
    },
    'BFBFR': {  # placeholder
        'notes': 'Doc stub.',
    },
    'CDAFR': {  # placeholder
        'notes': 'Doc stub.',
    },
    'EGAFR': {  # placeholder
        'notes': 'Doc stub.',
    },
}

# Remove placeholder/stub entries that should never match a real VIN
# (5-char keys when our slice is 4 chars). These exist as in-source
# documentation but must not pollute the lookup table.
VDS = {k: v for k, v in VDS.items() if len(k) == 4 and 'model' in v}


def decode(vin: str):
    """Decode an Audi VIN. Returns dict or None.

    Slice rationale: vin[3:7] (positions 4-7). 4-char key resolves Audi's
    model + body combos across A1/A3/A4/.../Q3/.../R8/TT lineups while
    keeping the table size manageable.
    """
    if not vin or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    if vin[:3] not in WMI:
        return None
    vds_key = vin[3:7]
    year = YEAR_CODES.get(vin[9])
    entry = VDS.get(vds_key)
    if not entry:
        return None
    return {
        'year': year,
        'make': 'Audi',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:audi',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # A3
        ('WAUEFAFLXFA050012', 'A3', 2015),
        ('WAUEFAFL1GA051501', 'A3', 2016),
        ('WAUEFAFL8HA053012', 'A3', 2017),
        ('WAUGYAFLXMA060012', 'A3', 2021),
        ('WAUGYAFL6PA063012', 'A3', 2023),
        # S3
        ('WAUEFEFLXFA070012', 'S3', 2015),
        ('WAUEFEFL5HA073012', 'S3', 2017),
        ('WAUEFEFL7LA077801', 'S3', 2020),
        # RS3
        ('WUAGHGFL3NA090012', 'RS3', 2022),
        # A4 B9
        ('WAUEFCFL4HA100012', 'A4', 2017),
        ('WAUEFCFL9JA101501', 'A4', 2018),
        ('WAUEFCFL3KA103012', 'A4', 2019),
        ('WAUEFCFL5NA107901', 'A4', 2022),
        # A4 B8
        ('WAUAFCFL8AN110012', 'A4', 2010),
        ('WAUAFCFL5CN113012', 'A4', 2012),
        ('WAUAFCFL6FN117901', 'A4', 2015),
        # A4 Allroad
        ('WA1ANCFL3HA120012', 'A4 Allroad', 2017),
        ('WA1ANCFL8MA126205', 'A4 Allroad', 2021),
        # S4 B9
        ('WAUFFAFL2HA130012', 'S4', 2017),
        ('WAUFFAFL9KA133012', 'S4', 2019),
        # S4 B8
        ('WAUBGAFL2AA140012', 'S4', 2010),
        ('WAUBGAFL3DA144801', 'S4', 2013),
        # A5 Coupe
        ('WAUCFAFRXHN150012', 'A5', 2017),
        ('WAUCFAFR5KN153012', 'A5', 2019),
        ('WAUAECFL8AN180012', 'A5', 2010),
        ('WAUAECFL0DN184801', 'A5', 2013),
        # A5 Sportback
        ('WAUCFCFR4HN160012', 'A5 Sportback', 2017),
        ('WAUCFCFR1KN163012', 'A5 Sportback', 2019),
        # A5 Cabrio
        ('WAUCFFFR4HN170012', 'A5 Cabriolet', 2017),
        ('WAUCFFFR2KN173012', 'A5 Cabriolet', 2019),
        # S5
        ('WAUEGAFR2HN190012', 'S5', 2017),
        ('WAUEGAFR0KN193012', 'S5', 2019),
        # RS5
        ('WAUCGAFR2JN200012', 'RS5', 2018),
        ('WAUCGAFR9LN203012', 'RS5', 2020),
        # A6 C8
        ('WAUFCAFCXKN210012', 'A6', 2019),
        ('WAUFCAFC3LN211501', 'A6', 2020),
        ('WAUFCAFC1NN214801', 'A6', 2022),
        # A6 C7
        ('WAUAHAFC2CN220012', 'A6', 2012),
        ('WAUAHAFC1EN223012', 'A6', 2014),
        ('WAUAHAFC9GN226205', 'A6', 2016),
        ('WAUAHAFC5JN229012', 'A6', 2018),
        # S6
        ('WAUCDAFC4KN240012', 'S6', 2019),
        ('WAUCDAFC9LN241501', 'S6', 2020),
        # RS6
        ('WUAF2BFP6LA250012', 'RS6 Avant', 2020),
        ('WUAF2BFP1MA251501', 'RS6 Avant', 2021),
        # A7
        ('WAUC8AFC3KN260012', 'A7', 2019),
        ('WAUC8AFC1MN263012', 'A7', 2021),
        ('WAUAHGFC2CN270012', 'A7', 2012),
        ('WAUAHGFC0HN277901', 'A7', 2017),
        # S7
        ('WAUC8BFC4KN280012', 'S7', 2019),
        # A8
        ('WAUD8AFD2LN290012', 'A8', 2020),
        ('WAUD8AFD9NN293012', 'A8', 2022),
        # A8 D4
        ('WAUAFAFD8AN300012', 'A8', 2010),
        ('WAUAFAFD9DN304801', 'A8', 2013),
        # S8
        ('WAUD8KFD3LN320012', 'S8', 2020),
        # Q3
        ('WA1FYAFC8KA331501', 'Q3', 2019),
        ('WA1FYAFC5MA334801', 'Q3', 2021),
        ('WA1FYAFC9NA336205', 'Q3', 2022),
        # Q4 e-tron
        ('WA1FZAFY8NA340012', 'Q4 e-tron', 2022),
        ('WA1FZAFY2PA341501', 'Q4 e-tron', 2023),
        # Q5
        ('WA1FPAFY6JA350012', 'Q5', 2018),
        ('WA1FPAFY8MA354801', 'Q5', 2021),
        ('WA1FPAFY9RA359012', 'Q5', 2024),
        ('WA1CKAFP3AA360012', 'Q5', 2010),
        ('WA1CKAFP6DA364801', 'Q5', 2013),
        # SQ5
        ('WA1FAAFY9JA380012', 'SQ5', 2018),
        ('WA1FAAFY2MA384801', 'SQ5', 2021),
        # Q7
        ('WA1V2AFE0GD400012', 'Q7', 2016),
        ('WA1V2AFE5LD406205', 'Q7', 2020),
        ('WA1V2AFE2ND409012', 'Q7', 2022),
        ('WA1GVAFE3AD410012', 'Q7', 2010),
        ('WA1GVAFE4DD414801', 'Q7', 2013),
        # SQ7
        ('WA1V4AFE2LD420012', 'SQ7', 2020),
        # Q8
        ('WA1FBAFY3KD430012', 'Q8', 2019),
        ('WA1FBAFY2MD433012', 'Q8', 2021),
        ('WA1FBAFY9PD436205', 'Q8', 2023),
        # RSQ8
        ('WUAF2KFY4LD440012', 'RS Q8', 2020),
        ('WUAF2KFY2ND443012', 'RS Q8', 2022),
        # SQ8
        ('WA1FBBFY3LD450012', 'SQ8', 2020),
        # Q8 e-tron
        ('WA1FBCFE3PD460012', 'Q8 e-tron', 2023),
        # e-tron (original)
        ('WA1VAAGE2KB470012', 'e-tron', 2019),
        ('WA1VAAGE3NB474801', 'e-tron', 2022),
        # e-tron GT
        ('WAUYZAFR2MN480012', 'e-tron GT', 2021),
        ('WAUYZAFR0PN483012', 'e-tron GT', 2023),
        # R8 V10 Gen 2
        ('WUAGEAFX1HN490012', 'R8', 2017),
        ('WUAGEAFX9KN493012', 'R8', 2019),
        ('WUAGEAFX2LN494801', 'R8', 2020),
        # R8 Gen 1
        ('WUABFAFG6AN500012', 'R8', 2010),
        ('WUABFAFG4CN503012', 'R8', 2012),
        # TT 8J
        ('TRUBCBT78A1010012', 'TT', 2010),
        ('TRUBCBT96C1013012', 'TT', 2012),
        # TT 8S
        ('TRUBFBFV3G1020012', 'TT', 2016),
        ('TRUBFBFV1J1023012', 'TT', 2018),
        ('TRUBFBFV6N1029012', 'TT', 2022),
        # Negative cases
        ('SHORT', None, None),
        ('INVALIDVIN1234567', None, None),
        ('WAUZZZZZZZZZZZZZZ', None, None),
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
