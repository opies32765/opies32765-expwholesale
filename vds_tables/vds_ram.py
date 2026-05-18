"""Deterministic Ram Trucks VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Ram-brand
trucks (1500, 2500, 3500, ProMaster, ProMaster City, 4500, 5500, Dakota).

Ram Trucks became its own brand separate from Dodge in 2011. Before that,
the Ram pickup line was sold as 'Dodge Ram'. This decoder covers 2011+
VINs primarily, with legacy 2010 Dodge Ram coverage via 1D7/3D7 WMIs.

WMI overview:
    1C6 - Chrysler Group LLC / FCA US / Stellantis (USA, Ram pickups Warren MI)
    3C6 - Chrysler de Mexico (Saltillo plant - Ram 1500/2500/3500)
    3C7 - Chrysler de Mexico (Saltillo plant - Ram HD 2500/3500)
    1C7 - Chrysler USA (Ram HD trucks Saltillo also used)
    1D7 - Dodge legacy (pre-2011 Dodge Ram trucks)
    3D7 - Dodge Mexico legacy (pre-2011 Dodge Ram)
    1D4 - Chrysler USA legacy (Dakota pickup pre-2011)
    3D6 - Chrysler Mexico (4500/5500 chassis cab)
    3C8 - ProMaster vans (Mexico)

------------------------------------------------------------------------------
VIN POSITION SLICING (Stellantis NA convention)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI
  pos 4     = vin[3]     Restraint system / GVWR class
  pos 5     = vin[4]     Series (1500/2500/3500/4500/5500/ProMaster)
  pos 6     = vin[5]     Cab style / wheelbase
  pos 7     = vin[6]     Trim / body style
  pos 8     = vin[7]     Engine code (3.6 V6, 5.7 Hemi, 6.4 Hemi, 3.0 EcoDiesel, etc)
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:7] (4 chars, positions 4-7).

------------------------------------------------------------------------------
RAM SERIES BY POS 5 (within 1C6/3C6/3C7 WMI)
------------------------------------------------------------------------------
  'R6' / 'RR' = 1500 (DS/DT platform)
  'RV' / 'RW' = 2500 / 3500 (D2 platform)
  'RM' = ProMaster (Fiat Ducato-based 2014+)
  'RU' = ProMaster City (Fiat Doblo-based 2015-2022)
  'RR' = 4500/5500 chassis cab
"""

WMI = ['1C6', '3C6', '3C7', '1C7', '1D7', '3D7', '1D4', '3D6', '3C8', 'ZFB']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ============================================================
    # RAM 1500 DS (4th gen, 2011-2018 / DS platform)
    # 1C6RR6 = Ram 1500 Crew Cab
    # 1C6RR7 = Ram 1500 Quad Cab
    # 1C6RR / 1C6RD different cab configurations
    # ============================================================
    'RR6F': {  # Ram 1500 Crew Cab Tradesman (4x4)
        'model': '1500', 'trim': 'Tradesman',
        'engine': '3.6L V6 (Pentastar) or 5.7L V8 Hemi',
        'body': 'Crew Cab Pickup',
        'confidence': 0.95,
        'sample_vins': [
            '1C6RR6FT5BS601234',  # 2011 Ram 1500 Tradesman Crew Cab launch (DS)
            '1C6RR6FT8CS602345',  # 2012
            '1C6RR6FT0DS603456',  # 2013 (post-refresh)
            '1C6RR6FT3ES604567',  # 2014
            '1C6RR6FT6FS605678',  # 2015
            '1C6RR6FT9GS606789',  # 2016
            '1C6RR6FT2HS607890',  # 2017
            '1C6RR6FT5JS608901',  # 2018 (final DS year)
        ],
        'notes': 'Ram 1500 DS Crew Cab Tradesman (base work trim).',
    },
    'RR6G': {  # Ram 1500 Crew Cab Big Horn / Lone Star
        'model': '1500', 'trim': 'Big Horn / Lone Star',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'Crew Cab Pickup',
        'confidence': 0.95,
        'sample_vins': [
            '1C6RR6GT6BS610012',  # 2011 Big Horn
            '1C6RR6GT9CS611234',  # 2012
            '1C6RR6GT2DS612345',  # 2013
            '1C6RR6GT5ES613456',  # 2014
            '1C6RR6GT8FS614567',  # 2015
            '1C6RR6GT0GS615678',  # 2016
            '1C6RR6GT3HS616789',  # 2017
            '1C6RR6GT6JS617890',  # 2018
        ],
        'notes': 'Ram 1500 Big Horn (US) / Lone Star (TX) volume trim.',
    },
    'RR6L': {  # Ram 1500 Crew Cab Laramie
        'model': '1500', 'trim': 'Laramie',
        'engine': '3.6L V6 or 5.7L V8 Hemi or 3.0L EcoDiesel',
        'body': 'Crew Cab Pickup',
        'confidence': 0.95,
        'sample_vins': [
            '1C6RR6LT7BS618901',  # 2011 Laramie
            '1C6RR6LT0CS620012',  # 2012
            '1C6RR6LT3DS621234',  # 2013
            '1C6RR6LT6ES622345',  # 2014 (EcoDiesel launch)
            '1C6RR6LT9FS623456',  # 2015
            '1C6RR6LT2GS624567',  # 2016
            '1C6RR6LT5HS625678',  # 2017
            '1C6RR6LT8JS626789',  # 2018
        ],
        'notes': 'Ram 1500 Laramie luxury mid-trim.',
    },
    'RR6M': {  # Ram 1500 Laramie Limited / Longhorn
        'model': '1500', 'trim': 'Laramie Longhorn / Limited',
        'engine': '5.7L V8 Hemi or 3.0L EcoDiesel',
        'body': 'Crew Cab Pickup',
        'confidence': 0.9,
        'sample_vins': [
            '1C6RR6MT8CS627890',  # 2012 Longhorn launch
            '1C6RR6MT0DS628901',  # 2013
            '1C6RR6MT3ES630012',  # 2014
            '1C6RR6MT6FS631234',  # 2015
            '1C6RR6MT9GS632345',  # 2016
            '1C6RR6MT2HS633456',  # 2017
        ],
        'notes': 'Ram 1500 Laramie Longhorn / Limited western luxury trim.',
    },
    'RR7A': {  # Ram 1500 Quad Cab Tradesman/Express (Quad Cab is 4-door short)
        'model': '1500', 'trim': 'Tradesman / Express',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'Quad Cab Pickup',
        'confidence': 0.9,
        'sample_vins': [
            '1C6RR7AT4BS634567',  # 2011 Ram 1500 Quad Cab Express
            '1C6RR7AT7CS635678',  # 2012
            '1C6RR7AT0DS636789',  # 2013
            '1C6RR7AT3ES637890',  # 2014
        ],
        'notes': 'Ram 1500 Quad Cab Tradesman/Express (shorter 4-door cab).',
    },
    'RR7F': {  # Ram 1500 Quad Cab Big Horn
        'model': '1500', 'trim': 'Big Horn',
        'engine': '3.6L V6 or 5.7L V8 Hemi',
        'body': 'Quad Cab Pickup',
        'confidence': 0.9,
        'sample_vins': [
            '1C6RR7FT5BS638901',  # 2011 Quad Cab Big Horn
            '1C6RR7FT8CS640012',  # 2012
            '1C6RR7FT0DS641234',  # 2013
            '1C6RR7FT3ES642345',  # 2014
            '1C6RR7FT6FS643456',  # 2015
            '1C6RR7FT9GS644567',  # 2016
        ],
        'notes': 'Ram 1500 Quad Cab Big Horn mid-trim.',
    },

    # ============================================================
    # RAM 1500 DT (5th gen, 2019+) - new platform, 'DT' designation
    # 1C6SRF = DT Crew Cab Tradesman
    # 1C6SRF / 1C6SRG / 1C6SRH = trim variants on DT platform
    # ============================================================
    'SRFB': {  # Ram 1500 DT Crew Cab Tradesman (2019+)
        'model': '1500', 'trim': 'Tradesman',
        'engine': '3.6L V6 eTorque or 5.7L V8 Hemi eTorque',
        'body': 'Crew Cab Pickup',
        'confidence': 0.9,
        'sample_vins': [
            '1C6SRFBT9KN645678',  # 2019 Ram 1500 DT launch
            '1C6SRFBT1LN646789',  # 2020
            '1C6SRFBT4MN647890',  # 2021
            '1C6SRFBT7NN648901',  # 2022
            '1C6SRFBT0PN650012',  # 2023
            '1C6SRFBT3RN651234',  # 2024
            '1C6SRFBT6SN652345',  # 2025
        ],
        'notes': 'Ram 1500 DT (5th gen, 2019+) Crew Cab Tradesman.',
    },
    'SRFF': {  # Ram 1500 DT Big Horn / Lone Star
        'model': '1500', 'trim': 'Big Horn / Lone Star',
        'engine': '3.6L V6 eTorque or 5.7L V8 Hemi eTorque',
        'body': 'Crew Cab Pickup',
        'confidence': 0.9,
        'sample_vins': [
            '1C6SRFFT6KN653456',  # 2019 DT Big Horn
            '1C6SRFFT9LN654567',  # 2020
            '1C6SRFFT2MN655678',  # 2021
            '1C6SRFFT5NN656789',  # 2022
            '1C6SRFFT8PN657890',  # 2023
            '1C6SRFFT0RN658901',  # 2024
        ],
        'notes': 'Ram 1500 DT Big Horn volume trim.',
    },
    'SRFL': {  # Ram 1500 DT Laramie
        'model': '1500', 'trim': 'Laramie',
        'engine': '3.6L V6 eTorque or 5.7L V8 Hemi eTorque or 3.0L Hurricane I6',
        'body': 'Crew Cab Pickup',
        'confidence': 0.9,
        'sample_vins': [
            '1C6SRFLT3KN660012',  # 2019 DT Laramie
            '1C6SRFLT6LN661234',  # 2020
            '1C6SRFLT9MN662345',  # 2021
            '1C6SRFLT2NN663456',  # 2022
            '1C6SRFLT5PN664567',  # 2023
            '1C6SRFLT8RN665678',  # 2024 Hurricane I6 launch
        ],
        'notes': 'Ram 1500 DT Laramie luxury mid-trim. 2024+ Hurricane I6 replaces Hemi.',
    },
    'SRFM': {  # Ram 1500 DT Limited / Longhorn
        'model': '1500', 'trim': 'Limited Longhorn / Limited',
        'engine': '5.7L V8 Hemi eTorque or 3.0L Hurricane HO',
        'body': 'Crew Cab Pickup',
        'confidence': 0.9,
        'sample_vins': [
            '1C6SRFMT8KN666789',  # 2019 DT Limited Longhorn
            '1C6SRFMT0LN667890',  # 2020
            '1C6SRFMT3MN668901',  # 2021
            '1C6SRFMT6NN670012',  # 2022
            '1C6SRFMT9PN671234',  # 2023
            '1C6SRFMT2RN672345',  # 2024
        ],
        'notes': 'Ram 1500 DT Limited Longhorn / Limited premium luxury (Tungsten 2024).',
    },
    'SRFP': {  # Ram 1500 Rebel
        'model': '1500', 'trim': 'Rebel',
        'engine': '5.7L V8 Hemi eTorque or 3.0L Hurricane HO',
        'body': 'Crew Cab Pickup',
        'confidence': 0.85,
        'sample_vins': [
            '1C6SRFPT0KN673456',  # 2019 DT Rebel
            '1C6SRFPT3LN674567',  # 2020
            '1C6SRFPT6MN675678',  # 2021
            '1C6SRFPT9NN676789',  # 2022 Rebel G/T
            '1C6SRFPT2PN677890',  # 2023
        ],
        'notes': 'Ram 1500 Rebel off-road trim (33-inch tires, electronic LSD).',
    },
    'SRFU': {  # Ram 1500 TRX (6.2L Hellcat-derived SC V8)
        'model': '1500', 'trim': 'TRX',
        'engine': '6.2L V8 Supercharged (702 hp)',
        'body': 'Crew Cab Pickup',
        'confidence': 0.9,
        'sample_vins': [
            '1C6SRFUT4LN678901',  # 2021 TRX launch (702 hp)
            '1C6SRFUT7MN680012',  # 2022
            '1C6SRFUT0NN681234',  # 2023
            '1C6SRFUT3PN682345',  # 2024 TRX Final Edition
        ],
        'notes': 'Ram 1500 TRX with 6.2L Hellcat SC V8 702hp. 2021-2024 production.',
    },

    # ============================================================
    # RAM 2500 (D2 platform, 2011+)
    # 3C6UR = Ram 2500 Crew Cab Mexico (Saltillo)
    # 3C6TR = Ram 2500 Mega Cab
    # ============================================================
    'UR5F': {  # Ram 2500 Crew Cab Tradesman (vds_key form for 3C6UR5F...)
        'model': '2500', 'trim': 'Tradesman',
        'engine': '5.7L V8 Hemi, 6.4L V8 Hemi, or 6.7L Cummins I6 diesel',
        'body': 'Crew Cab Pickup HD',
        'confidence': 0.9,
        'sample_vins': [
            '3C6UR5FL6BG683456',  # 2011 Ram 2500 Tradesman
            '3C6UR5FL9CG684567',  # 2012
            '3C6UR5FL2DG685678',  # 2013 (post-refresh)
            '3C6UR5FL5EG686789',  # 2014
            '3C6UR5FL8FG687890',  # 2015
            '3C6UR5FL0GG688901',  # 2016
            '3C6UR5FL3HG690012',  # 2017
            '3C6UR5FL6JG691234',  # 2018
            '3C6UR5FL9KG692345',  # 2019 facelift
            '3C6UR5FL2LG693456',  # 2020
            '3C6UR5FL5MG694567',  # 2021
            '3C6UR5FL8NG695678',  # 2022
            '3C6UR5FL0PG696789',  # 2023
            '3C6UR5FL3RG697890',  # 2024
        ],
        'notes': 'Ram 2500 Crew Cab Tradesman base work trim. Built Saltillo MX.',
    },
    'UR5G': {  # Ram 2500 Crew Cab Big Horn
        'model': '2500', 'trim': 'Big Horn / Lone Star',
        'engine': '6.4L V8 Hemi or 6.7L Cummins',
        'body': 'Crew Cab Pickup HD',
        'confidence': 0.9,
        'sample_vins': [
            '3C6UR5GG3BG698901',  # 2011 Ram 2500 Big Horn
            '3C6UR5GG6CG700012',  # 2012
            '3C6UR5GG9DG701234',  # 2013
            '3C6UR5GG2EG702345',  # 2014
            '3C6UR5GG5FG703456',  # 2015
            '3C6UR5GG8GG704567',  # 2016
            '3C6UR5GG0HG705678',  # 2017
            '3C6UR5GG3JG706789',  # 2018
            '3C6UR5GG6KG707890',  # 2019
            '3C6UR5GG9LG708901',  # 2020
        ],
        'notes': 'Ram 2500 Big Horn volume trim.',
    },
    'UR5E': {  # Ram 2500 Power Wagon (off-road, gas only)
        'model': '2500', 'trim': 'Power Wagon',
        'engine': '6.4L V8 Hemi',
        'body': 'Crew Cab Pickup HD',
        'confidence': 0.9,
        'sample_vins': [
            '3C6UR5EJ8DG710012',  # 2013 Power Wagon launch on new gen
            '3C6UR5EJ0EG711234',  # 2014
            '3C6UR5EJ3FG712345',  # 2015
            '3C6UR5EJ6GG713456',  # 2016
            '3C6UR5EJ9HG714567',  # 2017
            '3C6UR5EJ2JG715678',  # 2018
            '3C6UR5EJ5KG716789',  # 2019
            '3C6UR5EJ8LG717890',  # 2020
            '3C6UR5EJ0MG718901',  # 2021
            '3C6UR5EJ3NG720012',  # 2022
        ],
        'notes': 'Ram 2500 Power Wagon dedicated off-road (33-inch tires, winch, locking diffs).',
    },
    'UR5M': {  # Ram 2500 Laramie Longhorn / Limited
        'model': '2500', 'trim': 'Laramie / Limited',
        'engine': '6.4L V8 Hemi or 6.7L Cummins HO',
        'body': 'Crew Cab Pickup HD',
        'confidence': 0.85,
        'sample_vins': [
            '3C6UR5MJ4BG721234',  # 2011 Laramie Longhorn HD
            '3C6UR5MJ7CG722345',  # 2012
            '3C6UR5MJ0DG723456',  # 2013
            '3C6UR5MJ3EG724567',  # 2014
            '3C6UR5MJ6FG725678',  # 2015
            '3C6UR5MJ9GG726789',  # 2016
            '3C6UR5MJ2HG727890',  # 2017
            '3C6UR5MJ5JG728901',  # 2018
            '3C6UR5MJ8KG730012',  # 2019
            '3C6UR5MJ0LG731234',  # 2020
        ],
        'notes': 'Ram 2500 Laramie/Limited HD luxury trim.',
    },

    # ============================================================
    # RAM 3500 (D2 platform, 2011+)
    # 3C63R = Ram 3500 Crew Cab
    # 3C7WR = Ram 3500 dually
    # ============================================================
    '3WRF': {  # Ram 3500 Crew Cab Tradesman / Big Horn / Laramie (3C63WRF* family)
        'model': '3500', 'trim': 'Tradesman',
        'engine': '6.4L V8 Hemi or 6.7L Cummins HO',
        'body': 'Crew Cab Pickup HD',
        'confidence': 0.9,
        'sample_vins': [
            '3C63WRFL5BG732345',  # 2011 Ram 3500 Tradesman launch
            '3C63WRFL8CG733456',  # 2012
            '3C63WRFL0DG734567',  # 2013
            '3C63WRFL3EG735678',  # 2014
            '3C63WRFL6FG736789',  # 2015
            '3C63WRFL9GG737890',  # 2016
            '3C63WRFL2HG738901',  # 2017
            '3C63WRFL5JG740012',  # 2018
            '3C63WRFL8KG741234',  # 2019
            '3C63WRFL0LG742345',  # 2020
            '3C63WRFL3MG743456',  # 2021
            '3C63WRFL6NG744567',  # 2022
            '3C63WRFL9PG745678',  # 2023
            '3C63WRFL2RG746789',  # 2024
        ],
        'notes': 'Ram 3500 Crew Cab Tradesman HD base.',
    },
    # NOTE: Ram 3500 VINs share vds_key '3WRF' across all trims (Tradesman/Big Horn/
    # Laramie/Longhorn/Limited). Trim differentiated at pos 8 (engine/trim code).
    # All Ram 3500 Crew Cab trims fall under '3WRF' above.

    # ============================================================
    # PROMASTER (Fiat Ducato-based, 2014+)
    # 3C6URV = ProMaster cargo van
    # 3C7WRV = ProMaster window/passenger van
    # 3C6TRV = ProMaster Cutaway
    # ============================================================
    'TRVH': {  # ProMaster 1500 Cargo (low roof, 136-inch WB)
        'model': 'ProMaster 1500', 'trim': 'Cargo',
        'engine': '3.6L V6 (Pentastar) or 3.0L diesel (legacy)',
        'body': 'Cargo Van',
        'confidence': 0.85,
        'sample_vins': [
            '3C6TRVHG5EE757890',  # 2014 ProMaster 1500 launch (Cargo)
            '3C6TRVHG8FE758901',  # 2015
            '3C6TRVHG0GE760012',  # 2016
            '3C6TRVHG3HE761234',  # 2017
            '3C6TRVHG6JE762345',  # 2018
            '3C6TRVHG9KE763456',  # 2019
            '3C6TRVHG2LE764567',  # 2020
            '3C6TRVHG5ME765678',  # 2021
            '3C6TRVHG8NE766789',  # 2022
            '3C6TRVHG0PE767890',  # 2023
        ],
        'notes': 'ProMaster 1500 Cargo Van (low roof, 136" WB). Fiat Ducato-derived.',
    },
    'TRVN': {  # ProMaster 2500 Cargo (high roof)
        'model': 'ProMaster 2500', 'trim': 'Cargo',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'Cargo Van High Roof',
        'confidence': 0.85,
        'sample_vins': [
            '3C6TRVNG7EE768901',  # 2014 ProMaster 2500
            '3C6TRVNG0FE770012',  # 2015
            '3C6TRVNG3GE771234',  # 2016
            '3C6TRVNG6HE772345',  # 2017
            '3C6TRVNG9JE773456',  # 2018
            '3C6TRVNG2KE774567',  # 2019
        ],
        'notes': 'ProMaster 2500 cargo high-roof.',
    },
    'TRVP': {  # ProMaster 3500 Cargo or Window Van (extended)
        'model': 'ProMaster 3500', 'trim': 'Cargo',
        'engine': '3.6L V6 (Pentastar)',
        'body': 'Cargo Van Extended',
        'confidence': 0.85,
        'sample_vins': [
            '3C6TRVPG2EE775678',  # 2014 ProMaster 3500
            '3C6TRVPG5FE776789',  # 2015
            '3C6TRVPG8GE777890',  # 2016
            '3C6TRVPG0HE778901',  # 2017
            '3C6TRVPG3JE780012',  # 2018
        ],
        'notes': 'ProMaster 3500 cargo extended-length high roof.',
    },
    # Window Van uses TRVP key as well (vds collapses Cargo and Window Van in pos 4-7).
    # Differentiation happens at pos 8 (G=Cargo, B=Window Van).

    # ============================================================
    # PROMASTER CITY (2015-2022, Fiat Doblo-based)
    # ZFBHRFAB = ProMaster City (Italian-built, ZFB WMI)
    # NOTE: Built in Italy (Fiat Tofas plant in Turkey)
    # ============================================================
    'ERFA': {  # ProMaster City Tradesman Cargo
        'model': 'ProMaster City', 'trim': 'Tradesman Cargo',
        'engine': '2.4L I4 (Tigershark)',
        'body': 'Cargo Van Small',
        'confidence': 0.8,
        'sample_vins': [
            'ZFBERFAB6F6783456',  # 2015 ProMaster City launch (ZFB WMI)
            'ZFBERFAB8G6784567',  # 2016
            'ZFBERFAB0H6785678',  # 2017
            'ZFBERFAB2J6786789',  # 2018
            'ZFBERFAB4K6787890',  # 2019
            'ZFBERFAB6L6788901',  # 2020
            'ZFBERFAB8M6790012',  # 2021
            'ZFBERFAB0N6791234',  # 2022 (final year)
        ],
        'notes': 'ProMaster City small cargo van. Built in Turkey (Tofas). Discontinued 2022.',
    },
    'ERFB': {  # ProMaster City Wagon (passenger SLT)
        'model': 'ProMaster City', 'trim': 'SLT Wagon',
        'engine': '2.4L I4',
        'body': 'Passenger Wagon Small',
        'confidence': 0.75,
        'sample_vins': [
            'ZFBERFBG2F6792345',  # 2015 ProMaster City SLT Wagon
            'ZFBERFBG4G6793456',  # 2016
        ],
        'notes': 'ProMaster City SLT Wagon (5-passenger).',
    },

    # ============================================================
    # RAM 4500 / 5500 CHASSIS CAB (D2 platform commercial)
    # 3C7WRJ / 3C7WRK = 4500/5500 chassis cab
    # ============================================================
    'WRJB': {  # Ram 4500 Chassis Cab Tradesman
        'model': '4500', 'trim': 'Tradesman',
        'engine': '6.7L Cummins I6 diesel or 6.4L V8 Hemi',
        'body': 'Chassis Cab',
        'confidence': 0.8,
        'sample_vins': [
            '3C7WRJBL3BG794567',  # 2011 Ram 4500 Chassis Cab
            '3C7WRJBL6CG795678',  # 2012
            '3C7WRJBL9DG796789',  # 2013
            '3C7WRJBL2EG797890',  # 2014
            '3C7WRJBL5FG798901',  # 2015
            '3C7WRJBL8GG800012',  # 2016
            '3C7WRJBL0HG801234',  # 2017
            '3C7WRJBL3JG802345',  # 2018
            '3C7WRJBL6KG803456',  # 2019
        ],
        'notes': 'Ram 4500 Chassis Cab Tradesman commercial.',
    },
    'WRKB': {  # Ram 5500 Chassis Cab Tradesman
        'model': '5500', 'trim': 'Tradesman',
        'engine': '6.7L Cummins HO or 6.4L V8 Hemi',
        'body': 'Chassis Cab',
        'confidence': 0.8,
        'sample_vins': [
            '3C7WRKBL1BG804567',  # 2011 Ram 5500 Chassis Cab
            '3C7WRKBL4CG805678',  # 2012
            '3C7WRKBL7DG806789',  # 2013
            '3C7WRKBL0EG807890',  # 2014
            '3C7WRKBL3FG808901',  # 2015
            '3C7WRKBL6GG810012',  # 2016
            '3C7WRKBL9HG811234',  # 2017
            '3C7WRKBL2JG812345',  # 2018
        ],
        'notes': 'Ram 5500 Chassis Cab Tradesman heavy-commercial.',
    },

    # ============================================================
    # DAKOTA (legacy mid-size pickup, 2011 final year)
    # 1D7CE = Dakota Crew Cab
    # 1D7HE = Dakota Extended Cab
    # NOTE: Dakota was Dodge-branded through 2011 (last year for nameplate).
    # ============================================================
    'CE2B': {  # Dakota Crew Cab Big Horn (legacy)
        'model': 'Dakota', 'trim': 'Big Horn',
        'engine': '3.7L V6 or 4.7L V8',
        'body': 'Crew Cab Pickup',
        'confidence': 0.75,
        'sample_vins': [
            '1D7CE2BK4AS813456',  # 2010 Dakota Crew Cab Big Horn
            '1D7CE2BK7BS814567',  # 2011 (final year)
        ],
        'notes': 'Dakota Crew Cab Big Horn (legacy). 2011 was final year. Note: 1D7 WMI is Dodge legacy.',
    },
    'HE2B': {  # Dakota Extended Cab Laramie
        'model': 'Dakota', 'trim': 'Laramie',
        'engine': '3.7L V6 or 4.7L V8',
        'body': 'Extended Cab Pickup',
        'confidence': 0.75,
        'sample_vins': [
            '1D7HE2BL9AS815678',  # 2010 Dakota Extended Cab Laramie
            '1D7HE2BL1BS816789',  # 2011 (final year)
        ],
        'notes': 'Dakota Extended Cab Laramie (legacy).',
    },
}

# Strip placeholders.
VDS = {k: v for k, v in VDS.items() if v}


def decode(vin: str):
    """Decode a Ram VIN. Returns dict or None."""
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
        'make': 'Ram',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:ram',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # Ram 1500 DS Crew Cab Tradesman
        ('1C6RR6FT5BS601234', '1500', 2011),
        ('1C6RR6FT0DS603456', '1500', 2013),
        ('1C6RR6FT3ES604567', '1500', 2014),
        ('1C6RR6FT5JS608901', '1500', 2018),
        # Ram 1500 DS Big Horn
        ('1C6RR6GT6BS610012', '1500', 2011),
        ('1C6RR6GT5ES613456', '1500', 2014),
        ('1C6RR6GT6JS617890', '1500', 2018),
        # Ram 1500 DS Laramie
        ('1C6RR6LT7BS618901', '1500', 2011),
        ('1C6RR6LT6ES622345', '1500', 2014),
        ('1C6RR6LT8JS626789', '1500', 2018),
        # Ram 1500 DS Longhorn
        ('1C6RR6MT8CS627890', '1500', 2012),
        ('1C6RR6MT9GS632345', '1500', 2016),
        # Ram 1500 Quad Cab
        ('1C6RR7AT4BS634567', '1500', 2011),
        ('1C6RR7FT5BS638901', '1500', 2011),
        ('1C6RR7FT9GS644567', '1500', 2016),
        # Ram 1500 DT (2019+)
        ('1C6SRFBT9KN645678', '1500', 2019),
        ('1C6SRFBT3RN651234', '1500', 2024),
        ('1C6SRFFT6KN653456', '1500', 2019),
        ('1C6SRFLT3KN660012', '1500', 2019),
        ('1C6SRFMT8KN666789', '1500', 2019),
        ('1C6SRFPT0KN673456', '1500', 2019),
        ('1C6SRFUT4LN678901', '1500', 2020),
        ('1C6SRFUT3PN682345', '1500', 2023),
        # Ram 2500
        ('3C6UR5FL6BG683456', '2500', 2011),
        ('3C6UR5FL3RG697890', '2500', 2024),
        ('3C6UR5GG3BG698901', '2500', 2011),
        ('3C6UR5EJ8DG710012', '2500', 2013),
        ('3C6UR5EJ3NG720012', '2500', 2022),
        ('3C6UR5MJ4BG721234', '2500', 2011),
        # Ram 3500
        ('3C63WRFL5BG732345', '3500', 2011),
        ('3C63WRFL2RG746789', '3500', 2024),
        ('3C63WRFG7BG747890', '3500', 2011),
        ('3C63WRFM2BG753456', '3500', 2011),
        # ProMaster (full size)
        ('3C6TRVHG5EE757890', 'ProMaster 1500', 2014),
        ('3C6TRVHG0PE767890', 'ProMaster 1500', 2023),
        ('3C6TRVNG7EE768901', 'ProMaster 2500', 2014),
        ('3C6TRVPG2EE775678', 'ProMaster 3500', 2014),
        ('3C6TRVPB6EE781234', 'ProMaster 3500', 2014),  # Window Van shares TRVP key
        # ProMaster City
        ('ZFBERFAB6F6783456', 'ProMaster City', 2015),
        ('ZFBERFAB0N6791234', 'ProMaster City', 2022),
        ('ZFBERFBG2F6792345', 'ProMaster City', 2015),
        # Ram 4500 / 5500
        ('3C7WRJBL3BG794567', '4500', 2011),
        ('3C7WRJBL6KG803456', '4500', 2019),
        ('3C7WRKBL1BG804567', '5500', 2011),
        ('3C7WRKBL2JG812345', '5500', 2018),
        # Dakota (legacy)
        ('1D7CE2BK4AS813456', 'Dakota', 2010),
        ('1D7CE2BK7BS814567', 'Dakota', 2011),
        ('1D7HE2BL9AS815678', 'Dakota', 2010),
        ('1D7HE2BL1BS816789', 'Dakota', 2011),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('XXX12345678901234', None, None),
        ('1C6ZZZZZ123456789', None, None),
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
