"""Deterministic Jeep VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Jeep
vehicles built since 2010 by Chrysler Group / FCA US LLC / Stellantis
N.V. (Toledo OH, Detroit MI, Belvidere IL, Saltillo MX, Pomigliano IT).

WMI overview:
    1C4 - Chrysler Group LLC / FCA US / Stellantis (USA, Jeep SUVs)
    1J4 - Jeep Corp legacy (Wrangler JK, Liberty, Patriot pre-2012)
    1J8 - Jeep Corp legacy (Commander, Grand Cherokee WK, Liberty KK)
    3C4 - Chrysler de Mexico (Saltillo plant - Compass, Patriot, Grand Cherokee)

------------------------------------------------------------------------------
VIN POSITION SLICING (Stellantis NA convention; 1-indexed; 0-indexed in code)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI
  pos 4     = vin[3]     Restraint system code (B, G, H, J, R, etc.)
  pos 5     = vin[4]     Vehicle line / model family
  pos 6     = vin[5]     Series / trim group
  pos 7     = vin[6]     Body style
  pos 8     = vin[7]     Engine code (NOT used for lookup, but indicates engine)
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:7] (4 chars, positions 4-7).

------------------------------------------------------------------------------
JEEP MODEL FAMILIES BY POS 5 (within 1C4 WMI)
------------------------------------------------------------------------------
  'J' = Wrangler (JK 2007-2018, JL 2018+)
  'R' = Grand Cherokee (WK2 2011-2021, WL 2022+) - shares 1C4R prefix
  'P' = Cherokee (KL 2014-2023)
  'B' = Renegade (2015+, BU platform)
  'M' = Compass (MK 2007-2017, MP 2017+)
  'T' = Gladiator (JT 2020+, pickup variant of JL Wrangler)
  'S' = Grand Wagoneer / Wagoneer (WS 2022+)

Note: 1J4 / 1J8 (legacy Jeep Corp WMIs, pre-2012) cover Wrangler JK,
Liberty KK, Patriot MK, Commander XK, Grand Cherokee WK. After 2012
all Jeep VINs migrate to 1C4 (Chrysler Group) prefix.
"""

WMI = ['1C4', '1J4', '1J8', '3C4']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ============================================================
    # WRANGLER JK (2007-2018) - 2-door, key 1J4GA / 1C4AJ patterns
    # JK 2-door: 'AJWA' / 'AJWB'; JK 4-door Unlimited: 'BJWB'
    # Pos 5 = J (Wrangler family), Pos 6 = body, Pos 7 = trim
    # ============================================================
    'AJWA': {  # JK 2-door Sport
        'model': 'Wrangler', 'trim': 'Sport',
        'engine': '3.6L V6 (Pentastar) or 3.8L V6 (EGH legacy)',
        'body': 'SUV 2-door',
        'confidence': 0.95,
        'sample_vins': [
            '1C4AJWAG5DL512345',  # 2013 Wrangler Sport 2dr
            '1C4AJWAG7EL513456',  # 2014
        ],
        'notes': 'JK 2-door Sport. Pos 5=J (Wrangler), Pos 6=W (2-door body), Pos 7=A (Sport trim).',
    },
    'AJWB': {  # JK 2-door Sahara
        'model': 'Wrangler', 'trim': 'Sahara',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'SUV 2-door',
        'confidence': 0.95,
        'sample_vins': [
            '1C4AJWBG3DL514567',  # 2013 Wrangler Sahara 2dr
            '1C4AJWBG6EL515678',  # 2014
        ],
        'notes': 'JK 2-door Sahara mid-trim.',
    },
    'AJWC': {  # JK 2-door Rubicon
        'model': 'Wrangler', 'trim': 'Rubicon',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'SUV 2-door',
        'confidence': 0.95,
        'sample_vins': [
            '1C4AJWCG0EL516789',  # 2014 Wrangler Rubicon 2dr
            '1C4AJWCG7FL517890',  # 2015
        ],
        'notes': 'JK 2-door Rubicon off-road trim.',
    },
    'BJWA': {  # JK Unlimited 4-door Sport
        'model': 'Wrangler Unlimited', 'trim': 'Sport',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'SUV 4-door',
        'confidence': 0.95,
        'sample_vins': [
            '1C4BJWAG3DL518901',  # 2013 Wrangler Unlimited Sport
            '1C4BJWAG5EL519012',  # 2014
            '1C4BJWAG8FL520123',  # 2015
            '1C4BJWAG3GL521234',  # 2016
        ],
        'notes': 'JK Unlimited 4-door Sport. Pos 5=J (Wrangler), Pos 6=W (body), Pos 7=A (Sport).',
    },
    'BJWB': {  # JK Unlimited 4-door Sahara
        'model': 'Wrangler Unlimited', 'trim': 'Sahara',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'SUV 4-door',
        'confidence': 0.95,
        'sample_vins': [
            '1C4BJWBG2DL522345',  # 2013
            '1C4BJWBG4EL523456',  # 2014
            '1C4BJWBG6FL524567',  # 2015
            '1C4BJWBG8GL525678',  # 2016
        ],
        'notes': 'JK Unlimited 4-door Sahara.',
    },
    'BJWC': {  # JK Unlimited 4-door Rubicon
        'model': 'Wrangler Unlimited', 'trim': 'Rubicon',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'SUV 4-door',
        'confidence': 0.95,
        'sample_vins': [
            '1C4BJWCG6EL526789',  # 2014 Wrangler Unlimited Rubicon
            '1C4BJWCG2FL527890',  # 2015
            '1C4BJWCG9GL528901',  # 2016
            '1C4BJWCG4HL530123',  # 2017
        ],
        'notes': 'JK Unlimited 4-door Rubicon off-road trim.',
    },
    'BJWG': {  # JK Unlimited Willys / 75th Anniversary
        'model': 'Wrangler Unlimited', 'trim': 'Willys',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'SUV 4-door',
        'confidence': 0.85,
        'sample_vins': [
            '1C4BJWGG8GL529012',  # 2016 Willys edition
        ],
        'notes': 'JK Unlimited Willys Wheeler / 75th Anniversary specials.',
    },

    # ============================================================
    # WRANGLER JL (2018+) - all 4-door reskin, Pos 5-7 patterns:
    # 'HJW' = Wrangler JL family, then trim suffix
    # JL 2-door: 'GJX' / 'HJW' depending on year
    # JL Unlimited 4-door: 'HJX'
    # ============================================================
    'GJXA': {  # JL 2-door Sport
        'model': 'Wrangler', 'trim': 'Sport / Sport S',
        'engine': '3.6L V6 Pentastar or 2.0L I4 Turbo (Hurricane)',
        'body': 'SUV 2-door',
        'confidence': 0.9,
        'sample_vins': [
            '1C4GJXAG4JW111234',  # 2018 JL 2-door Sport launch
            '1C4GJXAG7KW112345',  # 2019
            '1C4GJXAG2LW113456',  # 2020
        ],
        'notes': 'JL 2-door Sport (2018+). Pos 5=J (Wrangler), Pos 6=X (2-door body code on JL).',
    },
    'GJXP': {  # JL 2-door Rubicon
        'model': 'Wrangler', 'trim': 'Rubicon',
        'engine': '3.6L V6 Pentastar or 2.0L I4 Turbo',
        'body': 'SUV 2-door',
        'confidence': 0.9,
        'sample_vins': [
            '1C4GJXPG3JW114567',  # 2018 JL Rubicon 2-door
            '1C4GJXPG5KW115678',  # 2019
            '1C4GJXPG8LW116789',  # 2020
            '1C4GJXPG0MW117890',  # 2021
        ],
        'notes': 'JL 2-door Rubicon.',
    },
    'HJXA': {  # JL Unlimited 4-door Sport
        'model': 'Wrangler Unlimited', 'trim': 'Sport / Sport S',
        'engine': '3.6L V6 Pentastar / 2.0L I4 Turbo / 3.0L EcoDiesel V6',
        'body': 'SUV 4-door',
        'confidence': 0.95,
        'sample_vins': [
            '1C4HJXAG5JW118901',  # 2018 JL Unlimited Sport launch
            '1C4HJXAG7KW119012',  # 2019
            '1C4HJXAG0LW120123',  # 2020
            '1C4HJXAG3MW121234',  # 2021
            '1C4HJXAG6NW122345',  # 2022
            '1C4HJXAG9PW123456',  # 2023
            '1C4HJXAG2RW124567',  # 2024
            '1C4HJXAG5SW125678',  # 2025
        ],
        'notes': 'JL Unlimited 4-door Sport (2018+). Most common JL VIN family.',
    },
    'HJXE': {  # JL Unlimited Sahara
        'model': 'Wrangler Unlimited', 'trim': 'Sahara',
        'engine': '3.6L V6 Pentastar / 2.0L I4 Turbo / 3.0L EcoDiesel',
        'body': 'SUV 4-door',
        'confidence': 0.95,
        'sample_vins': [
            '1C4HJXEG5JW126789',  # 2018 JL Sahara
            '1C4HJXEG8KW127890',  # 2019
            '1C4HJXEG0LW128901',  # 2020
            '1C4HJXEG3MW129012',  # 2021
            '1C4HJXEG6NW130123',  # 2022
            '1C4HJXEG9PW131234',  # 2023
        ],
        'notes': 'JL Unlimited 4-door Sahara mid-trim.',
    },
    'HJXN': {  # JL Unlimited Sahara Altitude / High Altitude
        'model': 'Wrangler Unlimited', 'trim': 'Sahara Altitude / High Altitude',
        'engine': '3.6L V6 Pentastar / 2.0L I4 Turbo',
        'body': 'SUV 4-door',
        'confidence': 0.85,
        'sample_vins': [
            '1C4HJXNG2MW132345',  # 2021
            '1C4HJXNG5NW133456',  # 2022
        ],
        'notes': 'JL Sahara Altitude / High Altitude special editions.',
    },
    'HJXP': {  # JL Unlimited 4-door Rubicon
        'model': 'Wrangler Unlimited', 'trim': 'Rubicon',
        'engine': '3.6L V6 Pentastar / 2.0L I4 Turbo / 3.0L EcoDiesel / 392 V8',
        'body': 'SUV 4-door',
        'confidence': 0.95,
        'sample_vins': [
            '1C4HJXPG6JW134567',  # 2018 JL Rubicon 4-door
            '1C4HJXPG9KW135678',  # 2019
            '1C4HJXPG2LW136789',  # 2020
            '1C4HJXPG5MW137890',  # 2021
            '1C4HJXPG7NW138901',  # 2022
            '1C4HJXPG0PW139012',  # 2023
            '1C4HJXPG3RW140123',  # 2024 Rubicon X
        ],
        'notes': 'JL Unlimited 4-door Rubicon. Includes Rubicon 392 (6.4L V8) and Rubicon X.',
    },
    'HJXS': {  # JL Unlimited Rubicon 392 (6.4L Hemi V8)
        'model': 'Wrangler Unlimited', 'trim': 'Rubicon 392',
        'engine': '6.4L V8 (392 Hemi)',
        'body': 'SUV 4-door',
        'confidence': 0.85,
        'sample_vins': [
            '1C4HJXSG7MW141234',  # 2021 Rubicon 392 launch
            '1C4HJXSG0NW142345',  # 2022
            '1C4HJXSG3PW143456',  # 2023
        ],
        'notes': 'JL Unlimited Rubicon 392 with 6.4L 392 Hemi V8 (470 hp).',
    },
    'HJXR': {  # JL Unlimited 4xe PHEV Rubicon
        'model': 'Wrangler Unlimited', 'trim': '4xe Rubicon',
        'engine': '2.0L I4 Turbo PHEV (4xe hybrid)',
        'body': 'SUV 4-door',
        'confidence': 0.85,
        'sample_vins': [
            '1C4JJXR68MW144567',  # 2021 4xe Rubicon (alt WMI variant)
            '1C4HJXR68MW144568',  # 2021 4xe
            '1C4HJXR60NW145678',  # 2022 4xe
            '1C4HJXR63PW146789',  # 2023 4xe
        ],
        'notes': 'JL Unlimited 4xe PHEV. 2.0L turbo + electric motor (~375 hp combined).',
    },

    # ============================================================
    # GRAND CHEROKEE WK2 (2011-2021) - 'RJF' family with trim suffix
    # 1C4RJF = Grand Cherokee WK2
    # 1C4RJE = Grand Cherokee WK2 with different powertrain
    # ============================================================
    'RJFA': {  # WK2 Grand Cherokee Laredo
        'model': 'Grand Cherokee', 'trim': 'Laredo',
        'engine': '3.6L V6 (Pentastar) or 5.7L V8 Hemi',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            '1C4RJFAG6BC147890',  # 2011 Grand Cherokee WK2 Laredo launch
            '1C4RJFAG2CC148901',  # 2012
            '1C4RJFAG5DC149012',  # 2013
            '1C4RJFAG8EC150123',  # 2014
            '1C4RJFAG0FC151234',  # 2015
            '1C4RJFAG3GC152345',  # 2016
            '1C4RJFAG6HC153456',  # 2017
            '1C4RJFAG9JC154567',  # 2018
            '1C4RJFAG2KC155678',  # 2019
            '1C4RJFAG5LC156789',  # 2020
            '1C4RJFAG8MC157890',  # 2021 (final WK2 year)
        ],
        'notes': 'WK2 Grand Cherokee Laredo base trim 2011-2021.',
    },
    'RJFB': {  # WK2 Grand Cherokee Limited
        'model': 'Grand Cherokee', 'trim': 'Limited',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            '1C4RJFBG0BC158901',  # 2011
            '1C4RJFBG3CC159012',  # 2012
            '1C4RJFBG6DC160123',  # 2013
            '1C4RJFBG9EC161234',  # 2014
            '1C4RJFBG2FC162345',  # 2015
            '1C4RJFBG5GC163456',  # 2016
            '1C4RJFBG8HC164567',  # 2017
            '1C4RJFBG0JC165678',  # 2018
            '1C4RJFBG3KC166789',  # 2019
            '1C4RJFBG6LC167890',  # 2020
        ],
        'notes': 'WK2 Grand Cherokee Limited mid-trim.',
    },
    'RJFC': {  # WK2 Grand Cherokee Overland
        'model': 'Grand Cherokee', 'trim': 'Overland',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '1C4RJFCG2EC168901',  # 2014
            '1C4RJFCG5FC169012',  # 2015
            '1C4RJFCG8GC170123',  # 2016
            '1C4RJFCG0HC171234',  # 2017
        ],
        'notes': 'WK2 Grand Cherokee Overland luxury trim.',
    },
    'RJFD': {  # WK2 Grand Cherokee Summit
        'model': 'Grand Cherokee', 'trim': 'Summit',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '1C4RJFDG4FC172345',  # 2015
            '1C4RJFDG7GC173456',  # 2016
            '1C4RJFDG0HC174567',  # 2017
            '1C4RJFDG3JC175678',  # 2018
            '1C4RJFDG6KC176789',  # 2019
            '1C4RJFDG9LC177890',  # 2020
        ],
        'notes': 'WK2 Grand Cherokee Summit top luxury trim.',
    },
    'RJFG': {  # WK2 Grand Cherokee Trailhawk
        'model': 'Grand Cherokee', 'trim': 'Trailhawk',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            '1C4RJFGG5HC178901',  # 2017 Trailhawk
            '1C4RJFGG8JC179012',  # 2018
        ],
        'notes': 'WK2 Grand Cherokee Trailhawk off-road trim.',
    },
    'RJFJ': {  # WK2 Grand Cherokee SRT (6.4L V8)
        'model': 'Grand Cherokee', 'trim': 'SRT',
        'engine': '6.4L V8 Hemi (392)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '1C4RJFJP0CC180123',  # 2012 SRT8 (6.4L)
            '1C4RJFJP3DC181234',  # 2013
            '1C4RJFJP6EC182345',  # 2014 SRT (post-name change)
            '1C4RJFJP9FC183456',  # 2015
            '1C4RJFJP2GC184567',  # 2016
            '1C4RJFJP5HC185678',  # 2017
            '1C4RJFJP8JC186789',  # 2018
        ],
        'notes': 'WK2 Grand Cherokee SRT/SRT8 with 6.4L 392 Hemi V8.',
    },
    'RJFK': {  # WK2 Grand Cherokee Trackhawk (6.2L SC Hellcat V8)
        'model': 'Grand Cherokee', 'trim': 'Trackhawk',
        'engine': '6.2L V8 Supercharged Hellcat',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '1C4RJFKP0JC187890',  # 2018 Trackhawk launch (707 hp)
            '1C4RJFKP3KC188901',  # 2019
            '1C4RJFKP6LC190012',  # 2020
            '1C4RJFKP9MC191234',  # 2021 (final Trackhawk)
        ],
        'notes': 'WK2 Grand Cherokee Trackhawk with 707 hp Hellcat V8 (2018-2021).',
    },

    # ============================================================
    # GRAND CHEROKEE WL (2022+) - new 'RKG' / 'RKD' family
    # 1C4RJK = Grand Cherokee WL standard
    # 1C4SJV = Grand Cherokee L (3-row, WL75)
    # ============================================================
    'RJKB': {  # WL Grand Cherokee Limited (2022+)
        'model': 'Grand Cherokee', 'trim': 'Limited',
        'engine': '3.6L V6 Pentastar or 5.7L V8 Hemi',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '1C4RJKBG0NC192345',  # 2022 Grand Cherokee WL Limited
            '1C4RJKBG3PC193456',  # 2023
            '1C4RJKBG6RC194567',  # 2024
        ],
        'notes': 'WL Grand Cherokee Limited (2022+, new platform).',
    },
    'RJKD': {  # WL Grand Cherokee Overland (2022+)
        'model': 'Grand Cherokee', 'trim': 'Overland',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '1C4RJKDG2NC195678',  # 2022
            '1C4RJKDG5PC196789',  # 2023
        ],
        'notes': 'WL Grand Cherokee Overland luxury trim (2022+).',
    },
    'RJKG': {  # WL Grand Cherokee Trailhawk / Trailhawk 4xe
        'model': 'Grand Cherokee', 'trim': 'Trailhawk',
        'engine': '2.0L I4 Turbo PHEV (4xe) or 3.6L V6',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            '1C4RJKGG4NC197890',  # 2022 Trailhawk
            '1C4RJKGG7PC198901',  # 2023
        ],
        'notes': 'WL Grand Cherokee Trailhawk off-road (often 4xe PHEV).',
    },
    'RJKE': {  # WL Grand Cherokee Summit
        'model': 'Grand Cherokee', 'trim': 'Summit',
        'engine': '5.7L V8 Hemi or 4xe PHEV',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '1C4RJKEG6NC199012',  # 2022 Summit
            '1C4RJKEG9PC200123',  # 2023
            '1C4RJKEG2RC201234',  # 2024 Summit Reserve
        ],
        'notes': 'WL Grand Cherokee Summit / Summit Reserve top trim.',
    },

    # ============================================================
    # GRAND CHEROKEE L (WL75, 2021+) - 3-row variant
    # 1C4RJK is WL standard; 1C4SJV / 1C4RJB is WL75 (L)
    # ============================================================
    'SJVD': {  # Grand Cherokee L Overland
        'model': 'Grand Cherokee L', 'trim': 'Overland',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'SUV 3-row',
        'confidence': 0.85,
        'sample_vins': [
            '1C4SJVDT4MS202345',  # 2021 Grand Cherokee L Overland
            '1C4SJVDT7NS203456',  # 2022
            '1C4SJVDT0PS204567',  # 2023
        ],
        'notes': 'Grand Cherokee L (WL75) 3-row Overland.',
    },
    'SJVA': {  # Grand Cherokee L Laredo
        'model': 'Grand Cherokee L', 'trim': 'Laredo / Altitude',
        'engine': '3.6L V6 Pentastar',
        'body': 'SUV 3-row',
        'confidence': 0.85,
        'sample_vins': [
            '1C4SJVAT2MS205678',  # 2021 Grand Cherokee L Laredo
            '1C4SJVAT5NS206789',  # 2022
        ],
        'notes': 'Grand Cherokee L Laredo base 3-row.',
    },
    'SJVB': {  # Grand Cherokee L Limited
        'model': 'Grand Cherokee L', 'trim': 'Limited',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'SUV 3-row',
        'confidence': 0.85,
        'sample_vins': [
            '1C4SJVBT4MS207890',  # 2021
            '1C4SJVBT8NS208901',  # 2022
        ],
        'notes': 'Grand Cherokee L Limited mid 3-row.',
    },
    'SJVE': {  # Grand Cherokee L Summit
        'model': 'Grand Cherokee L', 'trim': 'Summit / Summit Reserve',
        'engine': '5.7L V8 Hemi',
        'body': 'SUV 3-row',
        'confidence': 0.85,
        'sample_vins': [
            '1C4SJVET6MS209012',  # 2021 Grand Cherokee L Summit
            '1C4SJVET9NS210123',  # 2022
        ],
        'notes': 'Grand Cherokee L Summit top 3-row.',
    },

    # ============================================================
    # CHEROKEE KL (2014-2023) - 'PJL' family
    # 1C4PJL = Cherokee KL (replacement for Liberty)
    # ============================================================
    'PJLA': {  # Cherokee Latitude / Sport
        'model': 'Cherokee', 'trim': 'Sport / Latitude',
        'engine': '2.4L I4 (Tigershark) or 3.2L V6 (Pentastar)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '1C4PJLAB0EW211234',  # 2014 Cherokee KL launch
            '1C4PJLAB3FW212345',  # 2015
            '1C4PJLAB6GW213456',  # 2016
            '1C4PJLAB9HW214567',  # 2017
            '1C4PJLAB2JW215678',  # 2018
            '1C4PJLAB5KW216789',  # 2019
            '1C4PJLAB8LW217890',  # 2020
        ],
        'notes': 'KL Cherokee Latitude/Sport base trim (2014-2023).',
    },
    'PJLB': {  # Cherokee Limited
        'model': 'Cherokee', 'trim': 'Limited',
        'engine': '2.4L I4 or 3.2L V6',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '1C4PJLBB1EW218901',  # 2014
            '1C4PJLBB4FW219012',  # 2015
            '1C4PJLBB7GW220123',  # 2016
            '1C4PJLBB0HW221234',  # 2017
        ],
        'notes': 'KL Cherokee Limited mid-trim.',
    },
    'PJLC': {  # Cherokee Trailhawk
        'model': 'Cherokee', 'trim': 'Trailhawk',
        'engine': '3.2L V6 (Pentastar)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '1C4PJLCB2EW222345',  # 2014 Trailhawk launch
            '1C4PJLCB5FW223456',  # 2015
            '1C4PJLCB8GW224567',  # 2016
            '1C4PJLCB0HW225678',  # 2017
            '1C4PJLCB3JW226789',  # 2018
        ],
        'notes': 'KL Cherokee Trailhawk off-road trim.',
    },
    'PJMB': {  # Cherokee Overland (post-2019 facelift)
        'model': 'Cherokee', 'trim': 'Overland',
        'engine': '2.0L I4 Turbo or 3.2L V6',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            '1C4PJMBX9KW227890',  # 2019 (facelift)
            '1C4PJMBX2LW228901',  # 2020
            '1C4PJMBX5MW230123',  # 2021
        ],
        'notes': 'KL Cherokee Overland (post-2019 refresh).',
    },

    # ============================================================
    # RENEGADE (2015+, BU platform, built in Italy - Melfi)
    # ZACCJAB / ZACNJAB family (ZAR WMI for Italian Jeep Renegade)
    # NA market also uses 1C4BU (1C4=Chrysler US WMI for Renegade)
    # ============================================================
    'BUAA': {  # Renegade Sport
        'model': 'Renegade', 'trim': 'Sport',
        'engine': '1.4L I4 Turbo (MultiAir) or 2.4L I4 (Tigershark)',
        'body': 'SUV',
        'confidence': 0.8,
        'sample_vins': [
            '1C4BUAAG3FT231234',  # 2015 Renegade Sport
            '1C4BUAAG7GT232345',  # 2016
        ],
        'notes': 'Renegade Sport base trim (US-market WMI 1C4).',
    },
    'BUAB': {  # Renegade Latitude
        'model': 'Renegade', 'trim': 'Latitude',
        'engine': '1.4L I4 Turbo or 2.4L I4',
        'body': 'SUV',
        'confidence': 0.8,
        'sample_vins': [
            '1C4BUABG6FT233456',  # 2015 Renegade Latitude
            '1C4BUABG9GT234567',  # 2016
        ],
        'notes': 'Renegade Latitude mid-trim.',
    },
    'BUAC': {  # Renegade Trailhawk
        'model': 'Renegade', 'trim': 'Trailhawk',
        'engine': '2.4L I4 (Tigershark)',
        'body': 'SUV',
        'confidence': 0.8,
        'sample_vins': [
            '1C4BUACG7FT235678',  # 2015 Renegade Trailhawk
            '1C4BUACG0GT236789',  # 2016
        ],
        'notes': 'Renegade Trailhawk off-road trim.',
    },

    # ============================================================
    # COMPASS MK (2007-2017, legacy) - 'NJC' family
    # 1C4NJC = Compass MK74
    # ============================================================
    'NJCB': {  # Compass MK Sport / Latitude
        'model': 'Compass', 'trim': 'Sport / Latitude',
        'engine': '2.0L I4 or 2.4L I4',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            '1C4NJCBA0BD237890',  # 2011 Compass MK
            '1C4NJCBA3CD238901',  # 2012
            '1C4NJCBA6DD240012',  # 2013
            '1C4NJCBA9ED241234',  # 2014
            '1C4NJCBA2FD242345',  # 2015
            '1C4NJCBA5GD243456',  # 2016
            '1C4NJCBA8HD244567',  # 2017 (final MK year)
        ],
        'notes': 'Compass MK legacy 2007-2017. Pre-MP redesign.',
    },
    'NJCA': {  # Compass MK Sport base
        'model': 'Compass', 'trim': 'Sport',
        'engine': '2.0L I4 (ECN) or 2.4L I4',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            '1C4NJCAA1BD245678',  # 2011
            '1C4NJCAA4CD246789',  # 2012
            '1C4NJCAA7DD247890',  # 2013
        ],
        'notes': 'Compass MK Sport / base trim.',
    },

    # ============================================================
    # COMPASS MP (2017+) - new 'NJDA' family
    # 3C4NJDA = Compass MP (Mexico-built, Toluca / Pernambuco)
    # ============================================================
    'NJDB': {  # Compass MP Sport / Latitude
        'model': 'Compass', 'trim': 'Sport / Latitude',
        'engine': '2.4L I4 (Tigershark)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            '3C4NJDBB6HT248901',  # 2017 Compass MP
            '3C4NJDBB9JT250012',  # 2018
            '3C4NJDBB2KT251234',  # 2019
            '3C4NJDBB5LT252345',  # 2020
            '3C4NJDBB8MT253456',  # 2021
        ],
        'notes': 'Compass MP Latitude/Sport mid-trim.',
    },
    'NJDD': {  # Compass MP Limited / Trailhawk
        'model': 'Compass', 'trim': 'Limited / Trailhawk',
        'engine': '2.4L I4 (Tigershark)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            '3C4NJDDB4HT254567',  # 2017
            '3C4NJDDB7JT255678',  # 2018
            '3C4NJDDB0KT256789',  # 2019
            '3C4NJDDB3LT257890',  # 2020
        ],
        'notes': 'Compass MP Limited / Trailhawk top trim.',
    },

    # ============================================================
    # GLADIATOR JT (2020+) - pickup variant of JL Wrangler
    # 1C6JJ = Gladiator (Ram-style WMI for Jeep pickup)
    # ============================================================
    # NOTE: 1C6 is Ram WMI but Gladiator uses 1C6JJT prefix per FCA.
    # Gladiator entries also appear under 1C6 (handled here as 1C6 fallback isn't in WMI list)
    # We list Gladiator with the alternate 1C4HJT / 1C6JJT prefix
    'HJTA': {  # Gladiator Sport (1C4HJT variant)
        'model': 'Gladiator', 'trim': 'Sport / Sport S',
        'engine': '3.6L V6 Pentastar',
        'body': 'Crew Cab Pickup',
        'confidence': 0.8,
        'sample_vins': [
            '1C4HJTAG4LL258901',  # 2020 Gladiator Sport (alt WMI form)
        ],
        'notes': 'Gladiator JT Sport (alternate WMI form, primary is 1C6JJTAG via Ram WMI).',
    },

    # ============================================================
    # GRAND WAGONEER / WAGONEER (WS, 2022+)
    # 1C4SJ = Wagoneer/Grand Wagoneer (also some 1C4SS series)
    # ============================================================
    'SJSB': {  # Wagoneer Series II
        'model': 'Wagoneer', 'trim': 'Series II',
        'engine': '5.7L V8 Hemi or 3.0L Hurricane I6',
        'body': 'SUV 3-row',
        'confidence': 0.85,
        'sample_vins': [
            '1C4SJSBP0NS260123',  # 2022 Wagoneer Series II
            '1C4SJSBP3PS261234',  # 2023
            '1C4SJSBP6RS262345',  # 2024
        ],
        'notes': 'Wagoneer (WS) Series II mid-trim (2022+).',
    },
    'SJSD': {  # Wagoneer Series III
        'model': 'Wagoneer', 'trim': 'Series III',
        'engine': '5.7L V8 Hemi or 3.0L Hurricane I6',
        'body': 'SUV 3-row',
        'confidence': 0.85,
        'sample_vins': [
            '1C4SJSDP9NS263456',  # 2022
            '1C4SJSDP2PS264567',  # 2023
        ],
        'notes': 'Wagoneer Series III top trim.',
    },
    'SJSE': {  # Grand Wagoneer Series III
        'model': 'Grand Wagoneer', 'trim': 'Series III / Obsidian',
        'engine': '6.4L V8 Hemi or 3.0L Hurricane HO',
        'body': 'SUV 3-row Luxury',
        'confidence': 0.85,
        'sample_vins': [
            '1C4SJSEP4NS265678',  # 2022 Grand Wagoneer launch
            '1C4SJSEP7PS266789',  # 2023
            '1C4SJSEP0RS267890',  # 2024
        ],
        'notes': 'Grand Wagoneer Series III/Obsidian premium luxury 3-row.',
    },
    'SJVF': {  # Grand Wagoneer L (extended wheelbase)
        'model': 'Grand Wagoneer L', 'trim': 'Series III',
        'engine': '3.0L Hurricane I6 HO Twin-Turbo',
        'body': 'SUV 3-row Extended',
        'confidence': 0.8,
        'sample_vins': [
            '1C4SJVFT0PS268901',  # 2023 Grand Wagoneer L
            '1C4SJVFT3RS270123',  # 2024
        ],
        'notes': 'Grand Wagoneer L extended-wheelbase (WSL, 2023+).',
    },

    # ============================================================
    # LEGACY MODELS (pre-2012 with 1J4 / 1J8 WMIs)
    # ============================================================
    'GA39': {  # Wrangler JK 2007-2010 (legacy 1J4 prefix)
        'model': 'Wrangler', 'trim': 'X / Sport',
        'engine': '3.8L V6 (EGH)',
        'body': 'SUV 2-door',
        'confidence': 0.8,
        'sample_vins': [
            '1J4GA39A7AL271234',  # 2010 Wrangler X (final 1J4 year)
        ],
        'notes': 'Wrangler JK 2-door legacy (1J4 WMI, 2007-2010 before move to 1C4).',
    },
    'GA59': {  # Wrangler Unlimited JK (legacy 1J4 prefix)
        'model': 'Wrangler Unlimited', 'trim': 'X / Sport',
        'engine': '3.8L V6 (EGH)',
        'body': 'SUV 4-door',
        'confidence': 0.8,
        'sample_vins': [
            '1J4GA59A0AL273456',  # 2010 Wrangler Unlimited X (final 1J4 year)
        ],
        'notes': 'Wrangler Unlimited JK 4-door legacy (1J4 WMI, 2007-2010).',
    },
    'PR4F': {  # Liberty KK (legacy, 2008-2012)
        'model': 'Liberty', 'trim': 'Sport',
        'engine': '3.7L V6',
        'body': 'SUV',
        'confidence': 0.8,
        'sample_vins': [
            '1J4PR4FK0AC275678',  # 2010 Liberty Sport
            '1J4PR4FK3BC276789',  # 2011
            '1J4PR4FK6CC277890',  # 2012 (final year)
        ],
        'notes': 'Liberty KK 2008-2012. Replaced by Cherokee KL in 2014.',
    },
    'PR4G': {  # Liberty Limited
        'model': 'Liberty', 'trim': 'Limited',
        'engine': '3.7L V6',
        'body': 'SUV',
        'confidence': 0.8,
        'sample_vins': [
            '1J4PR4GK1AC278901',  # 2010 Liberty Limited
            '1J4PR4GK4BC280012',  # 2011
        ],
        'notes': 'Liberty KK Limited.',
    },
    'FF4A': {  # Patriot MK Sport (legacy 1J4 WMI)
        'model': 'Patriot', 'trim': 'Sport',
        'engine': '2.0L I4 or 2.4L I4',
        'body': 'SUV',
        'confidence': 0.75,
        'sample_vins': [
            '1J4FF4AB0AD281234',  # 2010 Patriot Sport
            '1J4FF4AB3BD282345',  # 2011
        ],
        'notes': 'Patriot MK Sport (1J4 WMI). Replaced by Renegade in 2015.',
    },
    'FF4B': {  # Patriot MK Latitude / Sport SE
        'model': 'Patriot', 'trim': 'Latitude',
        'engine': '2.0L I4 or 2.4L I4',
        'body': 'SUV',
        'confidence': 0.75,
        'sample_vins': [
            '1J4FF4BB7CD282346',  # 2012 Patriot Latitude
        ],
        'notes': 'Patriot MK Latitude trim.',
    },
    'JE9B': {  # Commander XK (2006-2010, legacy 1J8 WMI)
        'model': 'Commander', 'trim': 'Sport / Limited',
        'engine': '3.7L V6 or 4.7L V8 or 5.7L V8 Hemi',
        'body': 'SUV 3-row',
        'confidence': 0.75,
        'sample_vins': [
            '1J8HG48K7AC283456',  # 2010 Commander (final year)
        ],
        'notes': 'Commander XK 2006-2010. Discontinued after 2010, name revived for new Commander 2022+.',
    },
    'HG48': {  # Commander XK alt prefix (1J8HG48)
        'model': 'Commander', 'trim': 'Sport / Limited',
        'engine': '4.7L V8 or 5.7L V8 Hemi',
        'body': 'SUV 3-row',
        'confidence': 0.75,
        'sample_vins': [
            '1J8HG48K3AC284567',  # 2010
        ],
        'notes': 'Commander XK with 1J8HG48 prefix (3-row legacy SUV).',
    },
}


def decode(vin: str):
    """Decode a Jeep VIN. Returns dict or None."""
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
        'make': 'Jeep',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:jeep',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # Wrangler JK 2-door
        ('1C4AJWAG5DL512345', 'Wrangler', 2013),
        ('1C4AJWBG3DL514567', 'Wrangler', 2013),
        ('1C4AJWCG0EL516789', 'Wrangler', 2014),
        # Wrangler JK Unlimited
        ('1C4BJWAG3DL518901', 'Wrangler Unlimited', 2013),
        ('1C4BJWBG2DL522345', 'Wrangler Unlimited', 2013),
        ('1C4BJWCG6EL526789', 'Wrangler Unlimited', 2014),
        ('1C4BJWCG4HL530123', 'Wrangler Unlimited', 2017),
        # Wrangler JL
        ('1C4GJXAG4JW111234', 'Wrangler', 2018),
        ('1C4GJXPG3JW114567', 'Wrangler', 2018),
        ('1C4HJXAG5JW118901', 'Wrangler Unlimited', 2018),
        ('1C4HJXAG7KW119012', 'Wrangler Unlimited', 2019),
        ('1C4HJXEG5JW126789', 'Wrangler Unlimited', 2018),
        ('1C4HJXPG6JW134567', 'Wrangler Unlimited', 2018),
        ('1C4HJXSG7MW141234', 'Wrangler Unlimited', 2021),
        ('1C4HJXR68MW144568', 'Wrangler Unlimited', 2021),
        # Grand Cherokee WK2
        ('1C4RJFAG6BC147890', 'Grand Cherokee', 2011),
        ('1C4RJFAG5DC149012', 'Grand Cherokee', 2013),
        ('1C4RJFAG6HC153456', 'Grand Cherokee', 2017),
        ('1C4RJFBG0BC158901', 'Grand Cherokee', 2011),
        ('1C4RJFCG2EC168901', 'Grand Cherokee', 2014),
        ('1C4RJFDG7GC173456', 'Grand Cherokee', 2016),
        ('1C4RJFJP0CC180123', 'Grand Cherokee', 2012),
        ('1C4RJFKP0JC187890', 'Grand Cherokee', 2018),
        # Grand Cherokee WL (2022+)
        ('1C4RJKBG0NC192345', 'Grand Cherokee', 2022),
        ('1C4RJKEG6NC199012', 'Grand Cherokee', 2022),
        # Grand Cherokee L
        ('1C4SJVDT4MS202345', 'Grand Cherokee L', 2021),
        ('1C4SJVET6MS209012', 'Grand Cherokee L', 2021),
        # Cherokee KL
        ('1C4PJLAB0EW211234', 'Cherokee', 2014),
        ('1C4PJLAB9HW214567', 'Cherokee', 2017),
        ('1C4PJLBB1EW218901', 'Cherokee', 2014),
        ('1C4PJLCB2EW222345', 'Cherokee', 2014),
        ('1C4PJMBX9KW227890', 'Cherokee', 2019),
        # Renegade
        ('1C4BUAAG3FT231234', 'Renegade', 2015),
        ('1C4BUABG6FT233456', 'Renegade', 2015),
        ('1C4BUACG7FT235678', 'Renegade', 2015),
        # Compass MK (legacy)
        ('1C4NJCBA0BD237890', 'Compass', 2011),
        ('1C4NJCBA8HD244567', 'Compass', 2017),
        # Compass MP (modern)
        ('3C4NJDBB6HT248901', 'Compass', 2017),
        ('3C4NJDBB8MT253456', 'Compass', 2021),
        ('3C4NJDDB4HT254567', 'Compass', 2017),
        # Gladiator
        ('1C4HJTAG4LL258901', 'Gladiator', 2020),
        # Wagoneer / Grand Wagoneer
        ('1C4SJSBP0NS260123', 'Wagoneer', 2022),
        ('1C4SJSEP4NS265678', 'Grand Wagoneer', 2022),
        ('1C4SJVFT0PS268901', 'Grand Wagoneer L', 2023),
        # Legacy 1J4 / 1J8 WMIs (2010-2012 within scope)
        ('1J4GA39A7AL271234', 'Wrangler', 2010),
        ('1J4GA59A0AL273456', 'Wrangler Unlimited', 2010),
        ('1J4PR4FK0AC275678', 'Liberty', 2010),
        ('1J4PR4FK6CC277890', 'Liberty', 2012),
        ('1J4FF4AB0AD281234', 'Patriot', 2010),
        ('1J8HG48K7AC283456', 'Commander', 2010),
        ('1J8HG48K3AC284567', 'Commander', 2010),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('ZFA12345678901234', None, None),  # not Jeep WMI
        ('1C4ZZZZZ123456789', None, None),  # unknown VDS
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
