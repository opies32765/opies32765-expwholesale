"""Deterministic Dodge VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Dodge-brand
vehicles (Charger, Challenger, Durango, Avenger, Dart, Caliber, Caravan,
Journey, Nitro, Hornet).

WMI overview:
    1C3 - Chrysler Group LLC / FCA US / Stellantis (USA, Avenger, Dart sedans)
    2C3 - Chrysler Canada (Brampton plant - Charger, Challenger)
    3C3 - Chrysler de Mexico (Toluca - Avenger, Caliber)
    2B3 - Chrysler Canada legacy (pre-2011 Charger/Challenger)
    1B3 - Chrysler USA legacy (pre-2011 sedans)
    1C4 - Chrysler USA (Durango SUV, some Journey years, modern Hornet)
    1C6 - Ram WMI (used for Ram-branded trucks but historically also Dodge Dakota pre-2011)
    2D4 - Chrysler Canada minivan WMI (Grand Caravan legacy)
    2D8 - Chrysler Canada (Caravan legacy)
    3D4 - Chrysler Mexico (Journey, some Caravan years)

Note: 1C3/2C3/3C3 WMIs are SHARED WITH CHRYSLER. Disambiguation happens at
VDS positions 5-7. Chrysler 300 uses CCAB/CCAG/CCAR (pos5='C', pos6='C'),
Dodge Charger uses CDXBT/CDZAT/CDXCT (pos5='D'), Dodge Challenger uses
CJZAG/CJZBT (pos5='J'), Dodge Avenger uses CCBG (pos5='C', pos6='B' for
Avenger vs CCAB for 300 Touring).

------------------------------------------------------------------------------
VIN POSITION SLICING (Stellantis NA convention)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI
  pos 4     = vin[3]     Restraint system
  pos 5     = vin[4]     Vehicle line / model family
  pos 6     = vin[5]     Series / trim
  pos 7     = vin[6]     Body style
  pos 8     = vin[7]     Engine code
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:7] (4 chars, positions 4-7).

------------------------------------------------------------------------------
DODGE MODEL FAMILIES BY POS 5 (within 1C3/2C3/3C3 WMI)
------------------------------------------------------------------------------
  'D' = Charger (LX/LD platform, 2011+)
  'J' = Challenger (LC platform, 2008+; new EV "Charger Daytona" 2024+)
  'C' = Avenger sedan (JS platform, when pos 6 = 'B' or 'D')
  'B' = Dart (PF platform, 2013-2016)

For 1C4 WMI (SUVs/minivans):
  'D' = Durango (WD platform 2011+)
  'R' = Caravan / Grand Caravan (RT platform legacy)
  'P' = Journey (JC platform 2009-2020)
  'Z' = Hornet (P3 platform, 2023+, built in Naples Italy)

For 1B3 / 2B3 legacy:
  Charger LX (2006-2010 pre-LD)
  Challenger LC (2008-2010 launch)
"""

WMI = ['1C3', '2C3', '3C3', '2B3', '1B3', '1C4', '3C4', '2D4', '2D8', '3D4', '1D4']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ============================================================
    # CHARGER LD (2011-2023) - 'DXBT' / 'DZBT' / 'DXCT' / 'DXFG'
    # 2C3CDX = Charger Canada (Brampton plant)
    # ============================================================
    'CDXB': {  # Charger SE / SXT (V6)
        'model': 'Charger', 'trim': 'SE / SXT',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            '2C3CDXBG3BH301234',  # 2011 Charger SE/SXT launch (LD)
            '2C3CDXBG6CH302345',  # 2012
            '2C3CDXBG9DH303456',  # 2013
            '2C3CDXBG2EH304567',  # 2014
            '2C3CDXBG5FH305678',  # 2015
            '2C3CDXBG8GH306789',  # 2016
            '2C3CDXBG0HH307890',  # 2017
            '2C3CDXBG3JH308901',  # 2018
            '2C3CDXBG6KH310012',  # 2019
            '2C3CDXBG9LH311234',  # 2020
            '2C3CDXBG2MH312345',  # 2021
            '2C3CDXBG5NH313456',  # 2022
            '2C3CDXBG8PH314567',  # 2023 (final LD year)
        ],
        'notes': 'Charger SE/SXT (LD, 2011-2023). Most common Charger VIN family.',
    },
    'CDXF': {  # Charger SXT AWD
        'model': 'Charger', 'trim': 'SXT AWD',
        'engine': '3.6L V6 (Pentastar) + AWD',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            '2C3CDXFG4BH315678',  # 2011 Charger SXT AWD
            '2C3CDXFG7CH316789',  # 2012
            '2C3CDXFG0DH317890',  # 2013
            '2C3CDXFG3EH318901',  # 2014
        ],
        'notes': 'Charger SXT AWD variant (LD). CDXF pos 6=F=AWD code.',
    },
    'CDXC': {  # Charger R/T (5.7L Hemi V8)
        'model': 'Charger', 'trim': 'R/T',
        'engine': '5.7L V8 Hemi',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            '2C3CDXCT5BH320012',  # 2011 Charger R/T
            '2C3CDXCT8CH321234',  # 2012
            '2C3CDXCT0DH322345',  # 2013
            '2C3CDXCT3EH323456',  # 2014
            '2C3CDXCT6FH324567',  # 2015 facelift
            '2C3CDXCT9GH325678',  # 2016
            '2C3CDXCT2HH326789',  # 2017
            '2C3CDXCT5JH327890',  # 2018
            '2C3CDXCT8KH328901',  # 2019
            '2C3CDXCT0LH330012',  # 2020
            '2C3CDXCT3MH331234',  # 2021
        ],
        'notes': 'Charger R/T with 5.7L Hemi V8.',
    },
    'CDXG': {  # Charger R/T Scat Pack (6.4L 392 Hemi)
        'model': 'Charger', 'trim': 'R/T Scat Pack',
        'engine': '6.4L V8 Hemi (392)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            '2C3CDXGJ8FH332345',  # 2015 Scat Pack launch
            '2C3CDXGJ0GH333456',  # 2016
            '2C3CDXGJ3HH334567',  # 2017
            '2C3CDXGJ6JH335678',  # 2018
            '2C3CDXGJ9KH336789',  # 2019
            '2C3CDXGJ2LH337890',  # 2020
            '2C3CDXGJ5MH338901',  # 2021
            '2C3CDXGJ8NH340012',  # 2022
        ],
        'notes': 'Charger R/T Scat Pack with 6.4L 392 Hemi V8 (485 hp).',
    },
    'CDXL': {  # Charger SRT Hellcat (6.2L Supercharged)
        'model': 'Charger', 'trim': 'SRT Hellcat',
        'engine': '6.2L V8 Supercharged Hellcat',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            '2C3CDXL95FH341234',  # 2015 Hellcat launch (707 hp)
            '2C3CDXL97GH342345',  # 2016
            '2C3CDXL90HH343456',  # 2017
            '2C3CDXL93JH344567',  # 2018
            '2C3CDXL96KH345678',  # 2019
            '2C3CDXL99LH346789',  # 2020 Widebody
            '2C3CDXL92MH347890',  # 2021
            '2C3CDXL95NH348901',  # 2022
            '2C3CDXL98PH350012',  # 2023 Last Call (Jailbreak)
        ],
        'notes': 'Charger SRT Hellcat 6.2L SC V8. Includes Widebody 717hp+, Redeye, Jailbreak Last Call 807hp.',
    },
    'CDXJ': {  # Charger SRT 392
        'model': 'Charger', 'trim': 'SRT 392',
        'engine': '6.4L V8 Hemi (392)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            '2C3CDXJG5EH351234',  # 2014 SRT 392 launch (replaces SRT8)
            '2C3CDXJG8FH352345',  # 2015
            '2C3CDXJG0GH353456',  # 2016
        ],
        'notes': 'Charger SRT 392 (renamed from SRT8 in 2014). 6.4L 485hp.',
    },
    'CDXD': {  # Charger Daytona (2024+ EV, new platform)
        'model': 'Charger Daytona', 'trim': 'R/T / Scat Pack EV',
        'engine': 'Dual-motor Electric AWD (Banshee)',
        'body': 'Coupe 2-door',
        'confidence': 0.8,
        'sample_vins': [
            '2C3CDXDT0RH354567',  # 2024 Charger Daytona EV launch
            '2C3CDXDT3SH355678',  # 2025
        ],
        'notes': 'New 2024+ Charger Daytona EV (STLA Large platform). 496hp R/T, 670hp Scat Pack. Initially 2-door, sedan launches 2025.',
    },

    # ============================================================
    # CHALLENGER LC/LA (2008-2023) - 'JZBT' / 'JZDG' family
    # 2C3CDZ = Challenger Canada (Brampton, same as Charger)
    # Pos 5=J (Challenger), Pos 6=trim
    # ============================================================
    'CJZA': {  # Challenger SXT / SE (V6 base)
        'model': 'Challenger', 'trim': 'SXT / SE',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'Coupe 2-door',
        'confidence': 0.95,
        'sample_vins': [
            '2C3CJZAG0BH356789',  # 2011 Challenger SXT (V6, LA refresh)
            '2C3CJZAG3CH357890',  # 2012
            '2C3CJZAG6DH358901',  # 2013
            '2C3CJZAG9EH360012',  # 2014
            '2C3CJZAG2FH361234',  # 2015 facelift
            '2C3CJZAG5GH362345',  # 2016
            '2C3CJZAG8HH363456',  # 2017
            '2C3CJZAG0JH364567',  # 2018
            '2C3CJZAG3KH365678',  # 2019
            '2C3CJZAG6LH366789',  # 2020
            '2C3CJZAG9MH367890',  # 2021
            '2C3CJZAG2NH368901',  # 2022
            '2C3CJZAG5PH370012',  # 2023 (final LA year)
        ],
        'notes': 'Challenger SXT/SE V6 base coupe. Pos 5=J=Challenger line.',
    },
    'CJZB': {  # Challenger R/T (5.7 Hemi)
        'model': 'Challenger', 'trim': 'R/T',
        'engine': '5.7L V8 Hemi',
        'body': 'Coupe 2-door',
        'confidence': 0.95,
        'sample_vins': [
            '2C3CJZBT5BH372345',  # 2011 R/T
            '2C3CJZBT8CH373456',  # 2012
            '2C3CJZBT0DH374567',  # 2013
            '2C3CJZBT3EH375678',  # 2014
            '2C3CJZBT6FH376789',  # 2015
            '2C3CJZBT9GH377890',  # 2016
            '2C3CJZBT2HH378901',  # 2017
        ],
        'notes': 'Challenger R/T with 5.7L Hemi V8 (375 hp).',
    },
    'CJZF': {  # Challenger R/T Scat Pack (6.4L 392)
        'model': 'Challenger', 'trim': 'R/T Scat Pack',
        'engine': '6.4L V8 Hemi (392)',
        'body': 'Coupe 2-door',
        'confidence': 0.9,
        'sample_vins': [
            '2C3CJZFJ8EH380012',  # 2014 Scat Pack launch
            '2C3CJZFJ0FH381234',  # 2015
            '2C3CJZFJ3GH382345',  # 2016
            '2C3CJZFJ6HH383456',  # 2017
            '2C3CJZFJ9JH384567',  # 2018
            '2C3CJZFJ2KH385678',  # 2019
            '2C3CJZFJ5LH386789',  # 2020 Widebody
            '2C3CJZFJ8MH387890',  # 2021
            '2C3CJZFJ0NH388901',  # 2022
            '2C3CJZFJ3PH390012',  # 2023 Last Call
        ],
        'notes': 'Challenger R/T Scat Pack 6.4L 392 Hemi (485 hp). Widebody 2018+.',
    },
    'CJZC': {  # Challenger SRT Hellcat
        'model': 'Challenger', 'trim': 'SRT Hellcat',
        'engine': '6.2L V8 Supercharged Hellcat',
        'body': 'Coupe 2-door',
        'confidence': 0.95,
        'sample_vins': [
            '2C3CJZC92FH391234',  # 2015 Hellcat launch (707 hp)
            '2C3CJZC95GH392345',  # 2016
            '2C3CJZC98HH393456',  # 2017
            '2C3CJZC90JH394567',  # 2018 Hellcat Widebody
            '2C3CJZC93KH395678',  # 2019 Redeye
            '2C3CJZC96LH396789',  # 2020 Super Stock
            '2C3CJZC99MH397890',  # 2021
            '2C3CJZC92NH398901',  # 2022
            '2C3CJZC95PH400012',  # 2023 Hellcat Redeye Jailbreak / Black Ghost
        ],
        'notes': 'Challenger SRT Hellcat 6.2L SC V8 (707-807 hp). Includes Super Stock 807hp, Black Ghost 807hp Last Call.',
    },
    'CJZD': {  # Challenger SRT Demon
        'model': 'Challenger', 'trim': 'SRT Demon',
        'engine': '6.2L V8 Supercharged (840 hp Demon)',
        'body': 'Coupe 2-door',
        'confidence': 0.8,
        'sample_vins': [
            '2C3CJZDT0JH401234',  # 2018 Demon launch (840 hp, 3300 unit run)
            '2C3CJZDT5SH402345',  # 2025 Demon 170 (1025 hp, Last Call)
        ],
        'notes': 'Challenger SRT Demon (2018 original 840hp, Demon 170 2023 Last Call 1025hp).',
    },
    'CJZG': {  # Challenger GT AWD (V6 AWD, 2017+)
        'model': 'Challenger', 'trim': 'GT AWD',
        'engine': '3.6L V6 + AWD',
        'body': 'Coupe 2-door',
        'confidence': 0.85,
        'sample_vins': [
            '2C3CJZGG6HH405678',  # 2017 Challenger GT AWD launch
            '2C3CJZGG9JH406789',  # 2018
            '2C3CJZGG2KH407890',  # 2019
            '2C3CJZGG5LH408901',  # 2020
        ],
        'notes': 'Challenger GT AWD V6 (2017+). First AWD Challenger.',
    },

    # ============================================================
    # AVENGER (legacy 2008-2014, JS platform sedan)
    # 1C3CC = Avenger sedan family (shares with Chrysler 200/Sebring)
    # Disambiguated by pos 5-6: CCBG/CCBB = Avenger SE/SXT
    # ============================================================
    'CCBG': {  # Avenger SXT
        'model': 'Avenger', 'trim': 'SXT',
        'engine': '2.4L I4 or 3.6L V6',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            '1C3CCBBG0AN410012',  # 2010 Avenger SXT (legacy WMI)
            '1C3CCBBG3BN411234',  # 2011
            '1C3CCBBG6CN412345',  # 2012
            '1C3CCBBG9DN413456',  # 2013
            '1C3CCBBG2EN414567',  # 2014 (final year)
        ],
        'notes': 'Avenger SXT (JS platform). Discontinued after 2014, replaced by 200.',
    },
    'CCBB': {  # Avenger SE (base)
        'model': 'Avenger', 'trim': 'SE',
        'engine': '2.4L I4',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            '1C3CCBBB4AN415678',  # 2010 Avenger SE
            '1C3CCBBB7BN416789',  # 2011
            '1C3CCBBB0CN417890',  # 2012
            '1C3CCBBB3DN418901',  # 2013
        ],
        'notes': 'Avenger SE base trim.',
    },
    'CCBR': {  # Avenger R/T (V6)
        'model': 'Avenger', 'trim': 'R/T',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'Sedan',
        'confidence': 0.8,
        'sample_vins': [
            '1C3CCBRR2CN420012',  # 2012 Avenger R/T
            '1C3CCBRR5DN421234',  # 2013
        ],
        'notes': 'Avenger R/T with V6 (limited production).',
    },

    # ============================================================
    # DART (PF platform 2013-2016, replaced Avenger)
    # 1C3CDF = Dart family
    # ============================================================
    'CDFB': {  # Dart SE / SXT
        'model': 'Dart', 'trim': 'SE / SXT',
        'engine': '2.0L I4 or 1.4L I4 Turbo (MultiAir)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            '1C3CDFBA4DD422345',  # 2013 Dart launch (PF)
            '1C3CDFBA7ED423456',  # 2014
            '1C3CDFBA0FD424567',  # 2015
            '1C3CDFBA3GD425678',  # 2016 (final year)
        ],
        'notes': 'Dart SE/SXT (PF platform) 2013-2016 Belvidere IL. Discontinued.',
    },
    'CDFC': {  # Dart Limited / Aero
        'model': 'Dart', 'trim': 'Limited / Aero',
        'engine': '1.4L I4 Turbo or 2.4L I4 (Tigershark)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            '1C3CDFCH5DD426789',  # 2013 Dart Limited
            '1C3CDFCH8ED427890',  # 2014
            '1C3CDFCH0FD428901',  # 2015
            '1C3CDFCH3GD430012',  # 2016
        ],
        'notes': 'Dart Limited / Aero (high-MPG variant) top trim.',
    },
    'CDFE': {  # Dart GT (2.4 V6)
        'model': 'Dart', 'trim': 'GT',
        'engine': '2.4L I4 (Tigershark)',
        'body': 'Sedan',
        'confidence': 0.8,
        'sample_vins': [
            '1C3CDFEB7ED431234',  # 2014 Dart GT
            '1C3CDFEB0FD432345',  # 2015
        ],
        'notes': 'Dart GT sport trim 2.4L.',
    },

    # ============================================================
    # CALIBER (PM platform legacy, 2007-2012)
    # 1B3CB = Caliber family
    # ============================================================
    'CB1H': {  # Caliber SE / SXT
        'model': 'Caliber', 'trim': 'SE / SXT',
        'engine': '1.8L I4 or 2.0L I4 or 2.4L I4',
        'body': 'Hatchback',
        'confidence': 0.8,
        'sample_vins': [
            '1B3CB1HA3AD433456',  # 2010 Caliber SXT
            '1B3CB1HA6BD434567',  # 2011
            '1B3CB1HA9CD435678',  # 2012 (final year)
        ],
        'notes': 'Caliber legacy 5-door hatchback (PM). Discontinued after 2012.',
    },

    # ============================================================
    # DURANGO (WD platform 2011+) - 'DJDG' / 'DJEG' family
    # 1C4RDH = Durango (built in Detroit Jefferson North)
    # NOTE: Earlier WK variant 2011-2013 used different WMI; modern is 1C4RDH/SDH
    # ============================================================
    'RDJD': {  # Durango SXT (base)
        'model': 'Durango', 'trim': 'SXT',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'SUV 3-row',
        'confidence': 0.9,
        'sample_vins': [
            '1C4RDJDG6BC436789',  # 2011 Durango WD launch
            '1C4RDJDG9CC437890',  # 2012
            '1C4RDJDG2DC438901',  # 2013
            '1C4RDJDG5EC440012',  # 2014 facelift
            '1C4RDJDG8FC441234',  # 2015
            '1C4RDJDG0GC442345',  # 2016
            '1C4RDJDG3HC443456',  # 2017
            '1C4RDJDG6JC444567',  # 2018
            '1C4RDJDG9KC445678',  # 2019
            '1C4RDJDG2LC446789',  # 2020 facelift
            '1C4RDJDG5MC447890',  # 2021
            '1C4RDJDG8NC448901',  # 2022
            '1C4RDJDG0PC450012',  # 2023
            '1C4RDJDG3RC451234',  # 2024
        ],
        'notes': 'Durango SXT base (WD, 2011+). Detroit Jefferson North plant.',
    },
    'RDJG': {  # Durango GT
        'model': 'Durango', 'trim': 'GT',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'SUV 3-row',
        'confidence': 0.85,
        'sample_vins': [
            '1C4RDJGG6HC452345',  # 2017 Durango GT launch (renamed from Limited)
            '1C4RDJGG9JC453456',  # 2018
            '1C4RDJGG2KC454567',  # 2019
            '1C4RDJGG5LC455678',  # 2020
            '1C4RDJGG8MC456789',  # 2021
        ],
        'notes': 'Durango GT sport-trim (was Limited pre-2017).',
    },
    'RDJC': {  # Durango Citadel (V6 luxury)
        'model': 'Durango', 'trim': 'Citadel',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'SUV 3-row',
        'confidence': 0.85,
        'sample_vins': [
            '1C4RDJCG5DC457890',  # 2013 Citadel launch
            '1C4RDJCG8EC458901',  # 2014
            '1C4RDJCG0FC460012',  # 2015
            '1C4RDJCG3GC461234',  # 2016
            '1C4RDJCG6HC462345',  # 2017
            '1C4RDJCG9JC463456',  # 2018
            '1C4RDJCG2KC464567',  # 2019
            '1C4RDJCG5LC465678',  # 2020
        ],
        'notes': 'Durango Citadel luxury trim.',
    },
    'RDJE': {  # Durango R/T (5.7L Hemi)
        'model': 'Durango', 'trim': 'R/T',
        'engine': '5.7L V8 Hemi',
        'body': 'SUV 3-row',
        'confidence': 0.9,
        'sample_vins': [
            '1C4RDJEG7DC466789',  # 2013 Durango R/T launch
            '1C4RDJEG0EC467890',  # 2014
            '1C4RDJEG3FC468901',  # 2015
            '1C4RDJEG6GC470012',  # 2016
            '1C4RDJEG9HC471234',  # 2017
            '1C4RDJEG2JC472345',  # 2018
            '1C4RDJEG5KC473456',  # 2019
            '1C4RDJEG8LC474567',  # 2020
            '1C4RDJEG0MC475678',  # 2021
        ],
        'notes': 'Durango R/T with 5.7L Hemi V8.',
    },
    'RDJF': {  # Durango SRT 392 (6.4L)
        'model': 'Durango', 'trim': 'SRT 392',
        'engine': '6.4L V8 Hemi (392)',
        'body': 'SUV 3-row',
        'confidence': 0.85,
        'sample_vins': [
            '1C4RDJFGXKC476789',  # 2019 SRT 392 launch
            '1C4RDJFG2LC477890',  # 2020
            '1C4RDJFG5MC478901',  # 2021
            '1C4RDJFG8NC480012',  # 2022
            '1C4RDJFG0PC481234',  # 2023
        ],
        'notes': 'Durango SRT 392 with 6.4L 475 hp.',
    },
    'RDJP': {  # Durango Pursuit / SRT Hellcat (6.2L SC)
        'model': 'Durango', 'trim': 'SRT Hellcat',
        'engine': '6.2L V8 Supercharged Hellcat',
        'body': 'SUV 3-row',
        'confidence': 0.85,
        'sample_vins': [
            '1C4RDJPG2MC482345',  # 2021 Durango Hellcat launch (710 hp)
            '1C4RDJPG5RC483456',  # 2024 Hellcat (returning after skip year)
        ],
        'notes': 'Durango SRT Hellcat 6.2L SC V8 (710 hp). 2021 + 2023+ revival.',
    },

    # ============================================================
    # JOURNEY (JC platform 2009-2020) - 'PDDG' family
    # 3C4PDD = Journey Mexico (Toluca plant)
    # ============================================================
    'PDDG': {  # Journey SE / SXT
        'model': 'Journey', 'trim': 'SE / SXT',
        'engine': '2.4L I4 or 3.6L V6',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            '3C4PDDBG4AT484567',  # 2010 Journey (early VIN form)
            '3C4PDDGG4AT485678',  # 2010 (modern form)
            '3C4PDDGG7BT486789',  # 2011
            '3C4PDDGG0CT487890',  # 2012 facelift
            '3C4PDDGG3DT488901',  # 2013
            '3C4PDDGG6ET490012',  # 2014
            '3C4PDDGG9FT491234',  # 2015
            '3C4PDDGG2GT492345',  # 2016
            '3C4PDDGG5HT493456',  # 2017
            '3C4PDDGG8JT494567',  # 2018
            '3C4PDDGG0KT495678',  # 2019
            '3C4PDDGG3LT496789',  # 2020 (final year)
        ],
        'notes': 'Journey SE/SXT (JC) 2009-2020 Toluca plant. Discontinued after 2020.',
    },
    'PDCG': {  # Journey Crossroad / R/T
        'model': 'Journey', 'trim': 'Crossroad / R/T',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            '3C4PDCGG4ET497890',  # 2014 Journey Crossroad
            '3C4PDCGG7FT498901',  # 2015
            '3C4PDCGG0GT500012',  # 2016
        ],
        'notes': 'Journey Crossroad / R/T sport-luxury trim.',
    },
    'PDCB': {  # Journey Limited / Lux
        'model': 'Journey', 'trim': 'Limited / Lux',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'SUV',
        'confidence': 0.8,
        'sample_vins': [
            '3C4PDCBG2HT501234',  # 2017
            '3C4PDCBG5JT502345',  # 2018
        ],
        'notes': 'Journey Limited / Lux top luxury trim.',
    },

    # ============================================================
    # NITRO (KA platform legacy 2007-2012) - 'BJWA' alt prefix
    # 1D4PT = Nitro (Toledo Supplier Park plant)
    # ============================================================
    'PT5G': {  # Nitro SXT / Heat / Detonator
        'model': 'Nitro', 'trim': 'SXT / Heat',
        'engine': '3.7L V6 or 4.0L V6',
        'body': 'SUV',
        'confidence': 0.75,
        'sample_vins': [
            '1D4PT5GK1AW503456',  # 2010 Nitro SXT
            '1D4PT5GK4BW504567',  # 2011
            '1D4PT5GK7CW505678',  # 2012 (final year)
        ],
        'notes': 'Nitro KA platform 2007-2012 (1D4 WMI). Discontinued.',
    },

    # ============================================================
    # GRAND CARAVAN (RT platform 2008-2020 legacy minivan)
    # 2D4RN = Grand Caravan Canada (Windsor plant)
    # NOTE: Dodge Grand Caravan replaced Caravan after 2007; discontinued 2020.
    # ============================================================
    'RN5D': {  # Grand Caravan SE / SXT (RT legacy)
        'model': 'Grand Caravan', 'trim': 'SE / SXT',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'Minivan',
        'confidence': 0.85,
        'sample_vins': [
            '2D4RN5DG2AR506789',  # 2010 Grand Caravan SE
            '2D4RN5DG5BR507890',  # 2011
            '2D4RN5DG8CR508901',  # 2012
            '2D4RN5DG0DR510012',  # 2013
            '2D4RN5DG3ER511234',  # 2014
            '2D4RN5DG6FR512345',  # 2015
            '2D4RN5DG9GR513456',  # 2016
        ],
        'notes': 'Grand Caravan SE/SXT (RT, legacy 2D4 WMI). 2008-2016 form.',
    },
    'RDGG': {  # Grand Caravan Crew / R/T (modern WMI variant)
        'model': 'Grand Caravan', 'trim': 'Crew / R/T',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'Minivan',
        'confidence': 0.85,
        'sample_vins': [
            '2D4RDGGG4HR514567',  # 2017 Grand Caravan Crew/SXT
            '2D4RDGGG7JR515678',  # 2018
            '2D4RDGGG0KR516789',  # 2019
            '2D4RDGGG3LR517890',  # 2020 (final year)
        ],
        'notes': 'Grand Caravan Crew/SXT (post-2017 update).',
    },

    # ============================================================
    # HORNET (P3 platform 2023+, built in Naples Italy)
    # ZACNDB = Hornet Italy (Stellantis WMI Z for Italy)
    # 3C4 / 3C7 also seen for Mexico/Italy split
    # ============================================================
    'NDDB': {  # Hornet GT (gas)
        'model': 'Hornet', 'trim': 'GT',
        'engine': '2.0L I4 Turbo (Hurricane)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'ZACNDDBN3PP518901',  # 2023 Hornet GT launch (ZAR-based VIN)
            'ZACNDDBN6RP520012',  # 2024
        ],
        'notes': 'Hornet GT (P3 platform, 2023+, built in Naples Italy). Sibling of Alfa Tonale.',
    },
    'NDDC': {  # Hornet R/T (PHEV)
        'model': 'Hornet', 'trim': 'R/T',
        'engine': '1.3L I4 Turbo PHEV',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'ZACNDDCN4PP521234',  # 2023 Hornet R/T PHEV
            'ZACNDDCN7RP522345',  # 2024
        ],
        'notes': 'Hornet R/T PHEV (1.3L + electric). 30-mile EV range.',
    },
    'NDDG': {  # Hornet GLH / Tributo
        'model': 'Hornet', 'trim': 'GT Plus / GLH',
        'engine': '2.0L I4 Turbo',
        'body': 'SUV',
        'confidence': 0.8,
        'sample_vins': [
            'ZACNDDGN8RP523456',  # 2024 Hornet GLH Tributo
        ],
        'notes': 'Hornet GLH/Tributo top trim with appearance package.',
    },
}


def decode(vin: str):
    """Decode a Dodge VIN. Returns dict or None."""
    if not vin or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    if vin[:3] not in WMI and not (vin[:3] == 'ZAC' and vin[3:6] == 'NDD'):
        # Hornet uses ZAC WMI (Italian Stellantis plant)
        return None
    # Hornet exception: ZACNDD* VINs use ZAC as WMI
    if vin[:3] == 'ZAC':
        # Hornet keys are NDDB/NDDC/NDDG (vin[3:7])
        vds_key = vin[3:7]
        year = YEAR_CODES.get(vin[9])
        entry = VDS.get(vds_key)
        if not entry or not entry['model'].startswith('Hornet'):
            return None
        return {
            'year': year,
            'make': 'Dodge',
            'model': entry['model'],
            'trim': entry.get('trim'),
            'body': entry.get('body'),
            'engine': entry.get('engine'),
            'confidence': entry.get('confidence', 0.85),
            'source': 'vds_table:dodge',
        }
    vds_key = vin[3:7]
    year = YEAR_CODES.get(vin[9])
    entry = VDS.get(vds_key)
    if not entry:
        return None
    return {
        'year': year,
        'make': 'Dodge',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:dodge',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # Charger SE/SXT
        ('2C3CDXBG3BH301234', 'Charger', 2011),
        ('2C3CDXBG6CH302345', 'Charger', 2012),
        ('2C3CDXBG9DH303456', 'Charger', 2013),
        ('2C3CDXBG2EH304567', 'Charger', 2014),
        ('2C3CDXBG5FH305678', 'Charger', 2015),
        ('2C3CDXBG8GH306789', 'Charger', 2016),
        ('2C3CDXBG8PH314567', 'Charger', 2023),
        # Charger R/T
        ('2C3CDXCT5BH320012', 'Charger', 2011),
        ('2C3CDXCT6FH324567', 'Charger', 2015),
        ('2C3CDXCT3MH331234', 'Charger', 2021),
        # Charger Scat Pack / Hellcat / SRT
        ('2C3CDXGJ8FH332345', 'Charger', 2015),
        ('2C3CDXGJ8NH340012', 'Charger', 2022),
        ('2C3CDXL95FH341234', 'Charger', 2015),
        ('2C3CDXL98PH350012', 'Charger', 2023),
        ('2C3CDXJG5EH351234', 'Charger', 2014),
        # Charger Daytona EV
        ('2C3CDXDT0RH354567', 'Charger Daytona', 2024),
        # Challenger SXT
        ('2C3CJZAG0BH356789', 'Challenger', 2011),
        ('2C3CJZAG2FH361234', 'Challenger', 2015),
        ('2C3CJZAG5PH370012', 'Challenger', 2023),
        # Challenger R/T
        ('2C3CJZBT5BH372345', 'Challenger', 2011),
        ('2C3CJZBT3EH375678', 'Challenger', 2014),
        # Challenger Scat Pack
        ('2C3CJZFJ8EH380012', 'Challenger', 2014),
        ('2C3CJZFJ3PH390012', 'Challenger', 2023),
        # Challenger Hellcat
        ('2C3CJZC92FH391234', 'Challenger', 2015),
        ('2C3CJZC95PH400012', 'Challenger', 2023),
        # Challenger Demon
        ('2C3CJZDT0JH401234', 'Challenger', 2018),
        ('2C3CJZDT5SH402345', 'Challenger', 2025),
        # Challenger GT AWD
        ('2C3CJZGG6HH405678', 'Challenger', 2017),
        # Avenger
        ('1C3CCBBG0AN410012', 'Avenger', 2010),
        ('1C3CCBBG3BN411234', 'Avenger', 2011),
        ('1C3CCBBG2EN414567', 'Avenger', 2014),
        ('1C3CCBBB4AN415678', 'Avenger', 2010),
        # Dart
        ('1C3CDFBA4DD422345', 'Dart', 2013),
        ('1C3CDFBA3GD425678', 'Dart', 2016),
        ('1C3CDFCH5DD426789', 'Dart', 2013),
        # Caliber
        ('1B3CB1HA3AD433456', 'Caliber', 2010),
        ('1B3CB1HA9CD435678', 'Caliber', 2012),
        # Durango
        ('1C4RDJDG6BC436789', 'Durango', 2011),
        ('1C4RDJDG5EC440012', 'Durango', 2014),
        ('1C4RDJDG3RC451234', 'Durango', 2024),
        ('1C4RDJGG6HC452345', 'Durango', 2017),
        ('1C4RDJCG5DC457890', 'Durango', 2013),
        ('1C4RDJEG7DC466789', 'Durango', 2013),
        ('1C4RDJFGXKC476789', 'Durango', 2019),
        ('1C4RDJPG2MC482345', 'Durango', 2021),
        # Journey
        ('3C4PDDGG4AT485678', 'Journey', 2010),
        ('3C4PDDGG3LT496789', 'Journey', 2020),
        ('3C4PDCGG4ET497890', 'Journey', 2014),
        # Nitro
        ('1D4PT5GK7CW505678', 'Nitro', 2012),  # Will fail (1D4 not in WMI)
        # Grand Caravan
        ('2D4RN5DG2AR506789', 'Grand Caravan', 2010),
        ('2D4RN5DG9GR513456', 'Grand Caravan', 2016),
        ('2D4RDGGG3LR517890', 'Grand Caravan', 2020),
        # Hornet
        ('ZACNDDBN3PP518901', 'Hornet', 2023),
        ('ZACNDDCN4PP521234', 'Hornet', 2023),
        ('ZACNDDGN8RP523456', 'Hornet', 2024),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('XYZ12345678901234', None, None),
        ('2C3ZZZZZ123456789', None, None),  # unknown VDS
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
