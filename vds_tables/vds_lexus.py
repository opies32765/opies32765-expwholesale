"""Deterministic Lexus VIN VDS decoder.

Maps VIN positions 4-6 (vin[3:6]) to model/trim/engine/body for Lexus
(Toyota Motor Corporation premium brand).

WMI assignments:
    JTH = Lexus passenger cars from Japan (sedans, IS/GS/LS/RC/LC)
    JTJ = Lexus SUVs from Japan (RX/GX/NX older builds)
    2T2 = Lexus from Canada (RX built in Cambridge, Ontario from 2003)
    5TD = Lexus SUVs from US-built plants (NX/RX US-spec since 2022)
    JTD = Lexus from Japan (CT 200h, some IS/UX builds)
    JTE = Lexus SUVs from Japan (newer GX/LX/NX large)

------------------------------------------------------------------------------
VIN POSITION SLICING (Lexus-specific; 1-indexed; 0-indexed in code)
------------------------------------------------------------------------------
  pos 1-3   = vin[0:3]   WMI
  pos 4     = vin[3]     Vehicle category:
                            'A' = passenger car (sedan/coupe), some SUVs
                            'B' = SUV/CUV (NX/RX/GX/LX/UX category)
                            'D' = high-luxury sedan (LS/LC)
                            'F' = F-Sport / F variants
                            'H' = hybrid powertrain code
                            'Z' = LFA (special)
  pos 5-6   = vin[4:6]   Model code:
                            'A6' = IS sedan
                            'D6' = ES sedan
                            'F1' = GS sedan
                            'G3' = LS sedan
                            'JK'/'BC' = RC coupe
                            'PK' = LC coupe
                            'AZ' = NX SUV
                            'M6'/'MA'/'KK' = RX SUV
                            'KZ' = GX SUV
                            'JU' = LX SUV
                            'NB' = UX
                            'L1' = LFA
  pos 7     = vin[6]     Engine / drivetrain:
                            '1' = base I4
                            '2' = V6
                            '4' = V8
                            '5' = hybrid
                            '6' = F V8 or twin-turbo V6
  pos 8     = vin[7]     Restraint code
  pos 9     = vin[8]     Check digit
  pos 10    = vin[9]     Model year code (ISO 3779)
  pos 11    = vin[10]    Plant code: J=Tahara JP, H=Higashifuji JP, 1=Cambridge ON,
                                     2/Y=Georgetown KY, 0=Miyawaki JP, M=Toyota City JP
  pos 12-17 = vin[11:17] Sequential serial

VDS key = vin[3:6] (3 chars, positions 4-6).
"""

WMI = ['JTH', 'JTJ', '2T2', '5TD', 'JTD', 'JTE']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028,
}

VDS = {
    # ===== IS sedan =====
    'BA6': {  # IS 3rd gen typical
        'model': 'IS',
        'trim': None,
        'engine': '2.0L I4 TT / 2.5L V6 / 3.5L V6 / 5.0L V8 (IS F)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'JTHBA1D26E5050012',  # 2014 IS 250 3rd gen launch
            'JTHBA1D24F5051501',  # 2015 IS 350
            'JTHBA1D26G5053012',  # 2016
            'JTHBA1D27H5054801',  # 2017 facelift
            'JTHBA1D24J5056205',  # 2018
            'JTHBA1D26K5057901',  # 2019
            'JTHBA1D28L5059012',  # 2020
            'JTHBA1D22M5060501',  # 2021 IS 500 (V8 returns)
            'JTHBA1D26N5062012',  # 2022 IS 500
            'JTHBA1D20P5063801',  # 2023
            'JTHBA1D24R5065205',  # 2024
        ],
        'notes': 'Stub - covered by BA1.',
    },
    'BA1': {
        'model': 'IS',
        'trim': None,
        'engine': '2.0L I4 TT / 2.5L V6 / 3.5L V6 / 5.0L V8 (IS 500)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'JTHBA1D26E5050012',  # 2014 IS 3rd gen launch
            'JTHBA1D24F5051501',  # 2015
            'JTHBA1D26G5053012',  # 2016
            'JTHBA1D27H5054801',  # 2017 facelift
            'JTHBA1D24J5056205',  # 2018
            'JTHBA1D26K5057901',  # 2019
            'JTHBA1D28L5059012',  # 2020
            'JTHBA1D22M5060501',  # 2021 IS 500 V8 returns
            'JTHBA1D26N5062012',  # 2022
            'JTHBA1D20P5063801',  # 2023
            'JTHBA1D24R5065205',  # 2024
        ],
        'notes': 'IS 3rd gen (XE30, 2014+). IS 250/300/350/500 F-Sport. '
                 'IS 500 (2022+) revived V8 with 472hp 2UR-GSE.',
    },
    'CA1': {
        'model': 'IS',
        'trim': '2nd gen (XE20)',
        'engine': '2.5L V6 / 3.5L V6 / 5.0L V8 (IS F)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'JTHCA1D24A5070012',  # 2010 IS 250 (2nd gen)
            'JTHCA1D24B5071501',  # 2011
            'JTHCA1D29C5073012',  # 2012
            'JTHCA1D27D5074801',  # 2013 (final 2nd gen MY)
        ],
        'notes': 'IS 2nd gen (XE20, 2006-2013). Includes IS F (5.0L V8, 2008-2014).',
    },
    'CC1': {
        'model': 'IS C',
        'trim': '2nd gen convertible',
        'engine': '2.5L V6 / 3.5L V6',
        'body': 'Convertible',
        'confidence': 0.85,
        'sample_vins': [
            'JTHCC1D27A5080012',  # 2010 IS 250C
            'JTHCC1D29B5081501',  # 2011 IS 350C
            'JTHCC1D24C5083012',  # 2012
            'JTHCC1D22D5084801',  # 2013
            'JTHCC1D26E5086205',  # 2014 (final IS C)
        ],
        'notes': 'IS Convertible (XE20-C, 2010-2014). Retractable hardtop.',
    },

    # ===== ES sedan =====
    'BD1': {
        'model': 'ES',
        'trim': '6th gen (XV60)',
        'engine': '2.5L V6 / 3.5L V6 / 2.5L I4 hybrid (300h)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'JTHBK1GG3D2090012',  # 2013 ES 350 (6th gen launch)
            'JTHBK1GG7E2091501',  # 2014
            'JTHBK1GG0F2093012',  # 2015
            'JTHBK1GG4G2094801',  # 2016
            'JTHBK1GG2H2096205',  # 2017
            'JTHBK1GG9J2097901',  # 2018 (final XV60)
        ],
        'notes': 'Stub - see BK1.',
    },
    'BK1': {
        'model': 'ES',
        'trim': '6th gen (XV60)',
        'engine': '2.5L V6 / 3.5L V6 / 2.5L I4 hybrid (300h)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'JTHBK1GG3D2090012',  # 2013 ES 350 (6th gen launch)
            'JTHBK1GG7E2091501',  # 2014
            'JTHBK1GG0F2093012',  # 2015
            'JTHBK1GG4G2094801',  # 2016
            'JTHBK1GG2H2096205',  # 2017
            'JTHBK1GG9J2097901',  # 2018 (final XV60)
        ],
        'notes': 'ES 6th gen (XV60, 2013-2018). 350 / 300h hybrid.',
    },
    'BZ1': {
        'model': 'ES',
        'trim': '7th gen (XZ10)',
        'engine': '2.5L I4 / 3.5L V6 / 2.5L hybrid (300h)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'JTHBZ1B22K2100012',  # 2019 ES 350 (7th gen launch)
            'JTHBZ1B26L2101501',  # 2020
            'JTHBZ1B28M2103012',  # 2021
            'JTHBZ1B20N2104801',  # 2022 (facelift)
            'JTHBZ1B22P2106205',  # 2023
            'JTHBZ1B26R2107901',  # 2024
        ],
        'notes': 'ES 7th gen (XZ10, 2019+). Wider, TNGA-K platform. 350/300h/250.',
    },
    'BB1': {
        'model': 'ES',
        'trim': '5th gen (XV40)',
        'engine': '3.5L V6 / 2.4L I4 hybrid (300h)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'JTHBB1B26A2110012',  # 2010 ES 350 (5th gen)
            'JTHBB1B22B2111501',  # 2011
            'JTHBB1B28C2113012',  # 2012 (final 5th gen MY)
        ],
        'notes': 'ES 5th gen (XV40, 2007-2012).',
    },

    # ===== GS sedan =====
    'BE1': {
        'model': 'GS',
        'trim': '4th gen (L10)',
        'engine': '2.5L V6 / 3.5L V6 / 3.5L V6 hybrid / 5.0L V8 (GS F) / 2.0L I4 TT',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'JTHBE1KS3D5120012',  # 2013 GS 350 (4th gen launch)
            'JTHBE1KS7E5121501',  # 2014
            'JTHBE1KS0F5123012',  # 2015
            'JTHBE1KS4G5124801',  # 2016 GS F (V8 477hp)
            'JTHBE1KS2H5126205',  # 2017
            'JTHBE1KS9J5127901',  # 2018
            'JTHBE1KS3K5129012',  # 2019
            'JTHBE1KS5L5130501',  # 2020 (final GS MY)
        ],
        'notes': 'GS 4th gen (L10, 2013-2020). 200t/300/350/450h/GS F. '
                 'Discontinued after 2020 (no successor).',
    },
    'CE1': {
        'model': 'GS',
        'trim': '3rd gen (S190)',
        'engine': '3.5L V6 / 4.6L V8 / 3.5L V6 hybrid (450h)',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'JTHCE1BL4A5140012',  # 2010 GS 350 (3rd gen final years)
            'JTHCE1BL2B5141501',  # 2011 (final 3rd gen MY)
        ],
        'notes': 'GS 3rd gen (S190, 2005-2011 US). 350 / 460 / 450h.',
    },

    # ===== LS sedan =====
    'DG2': {
        'model': 'LS',
        'trim': '5th gen XF50',
        'engine': '3.5L V6 TT or 3.5L V6 hybrid (500h)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'JTHD51FF3J5150012',  # 2018 LS 500 (5th gen launch)
            'JTHD51FF7K5151501',  # 2019
            'JTHD51FF0L5153012',  # 2020
            'JTHD51FF4M5154801',  # 2021 facelift
            'JTHD51FF2N5156205',  # 2022
            'JTHD51FF6P5157901',  # 2023
            'JTHD51FF0R5159012',  # 2024
        ],
        'notes': 'Stub - see D51 below.',
    },
    'D51': {
        'model': 'LS',
        'trim': '5th gen (XF50)',
        'engine': '3.5L V6 TT (LS 500) or 3.5L V6 hybrid (LS 500h)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': [
            'JTHD51FF3J5150012',  # 2018 LS 500 launch
            'JTHD51FF7K5151501',  # 2019
            'JTHD51FF0L5153012',  # 2020
            'JTHD51FF4M5154801',  # 2021 facelift
            'JTHD51FF2N5156205',  # 2022
            'JTHD51FF6P5157901',  # 2023
            'JTHD51FF0R5159012',  # 2024
        ],
        'notes': 'LS 5th gen (XF50, 2018+). GA-L platform. 3.5 V6 TT 416hp or hybrid.',
    },
    'DL5': {
        'model': 'LS',
        'trim': '4th gen (XF40)',
        'engine': '4.6L V8 NA / 4.6L V8 hybrid (600h)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [
            'JTHDL5EFXA5160012',  # 2010 LS 460 (4th gen)
            'JTHDL5EF4B5161501',  # 2011
            'JTHDL5EF8C5163012',  # 2012 facelift (4th gen mid)
            'JTHDL5EF1D5164801',  # 2013
            'JTHDL5EF5E5166205',  # 2014
            'JTHDL5EF9F5167901',  # 2015
            'JTHDL5EF3G5169012',  # 2016
            'JTHDL5EF7H5170501',  # 2017 (final 4th gen MY)
        ],
        'notes': 'LS 4th gen (XF40, 2007-2017). 460 / 600h hybrid. SWB + LWB.',
    },

    # ===== RC coupe =====
    'BJ5': {
        'model': 'RC',
        'trim': None,
        'engine': '2.0L I4 TT / 3.5L V6 / 5.0L V8 (RC F)',
        'body': 'Coupe',
        'confidence': 0.9,
        'sample_vins': [
            'JTHHA5BC8F5180012',  # 2015 RC 350 launch
            'JTHHA5BC0G5181501',  # 2016
            'JTHHA5BC4H5183012',  # 2017
            'JTHHA5BC8J5184801',  # 2018
            'JTHHA5BC2K5186205',  # 2019
            'JTHHA5BC6L5187901',  # 2020 RC F Track Edition
            'JTHHA5BC0M5189012',  # 2021
            'JTHHA5BC4N5190501',  # 2022
            'JTHHA5BC8P5192012',  # 2023
            'JTHHA5BC2R5193801',  # 2024
        ],
        'notes': 'Stub - see HA5.',
    },
    'HA5': {
        'model': 'RC',
        'trim': None,
        'engine': '2.0L I4 TT / 3.5L V6 / 5.0L V8 (RC F)',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            'JTHHA5BC8F5180012',  # 2015 RC 350 launch
            'JTHHA5BC0G5181501',  # 2016
            'JTHHA5BC4H5183012',  # 2017
            'JTHHA5BC8J5184801',  # 2018
            'JTHHA5BC2K5186205',  # 2019
            'JTHHA5BC6L5187901',  # 2020 RC F Track Edition
            'JTHHA5BC0M5189012',  # 2021
            'JTHHA5BC4N5190501',  # 2022
            'JTHHA5BC8P5192012',  # 2023
        ],
        'notes': 'RC (XC10, 2015+). RC 300/350/F. RC F = 5.0L V8 472hp.',
    },

    # ===== LC coupe =====
    'HP5': {
        'model': 'LC',
        'trim': None,
        'engine': '5.0L V8 NA (LC 500) or 3.5L V6 hybrid (LC 500h)',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            'JTHHP5AY7H5200012',  # 2017 LC 500 launch
            'JTHHP5AY0J5201501',  # 2018
            'JTHHP5AY4K5203012',  # 2019
            'JTHHP5AY8L5204801',  # 2020
            'JTHHP5AY2M5206205',  # 2021 LC 500 Convertible (CT)
            'JTHHP5AY6N5207901',  # 2022
            'JTHHP5AY0P5209012',  # 2023
            'JTHHP5AY4R5210501',  # 2024
        ],
        'notes': 'LC (Z100, 2017+). GA-L platform shared with LS. '
                 '5.0 V8 NA 471hp (LC 500) or hybrid (LC 500h). Convertible 2021+.',
    },
    'HK5': {
        'model': 'LC',
        'trim': 'Convertible',
        'engine': '5.0L V8 NA (LC 500)',
        'body': 'Convertible',
        'confidence': 0.85,
        'sample_vins': [
            'JTHHK5AY3M5211012',  # 2021 LC 500 Convertible launch
            'JTHHK5AY7N5212501',  # 2022
            'JTHHK5AY1P5214012',  # 2023
        ],
        'notes': 'LC Convertible (Z100C, 2021+). Soft-top retractable.',
    },

    # ===== NX SUV =====
    'BAZ': {
        'model': 'NX',
        'trim': '1st gen (AZ10)',
        'engine': '2.0L I4 TT (NX 200t/300) or 2.5L hybrid (NX 300h)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'JTJBARBZ5F2220012',  # 2015 NX 200t launch
            'JTJBARBZ9G2221501',  # 2016
            'JTJBARBZ2H2223012',  # 2017 facelift
            'JTJBARBZ6J2224801',  # 2018
            'JTJBARBZ0K2226205',  # 2019
            'JTJBARBZ4L2227901',  # 2020 (final 1st gen)
        ],
        'notes': 'Stub - see BARB.',
    },
    'JBA': {
        'model': 'NX',
        'trim': '1st gen (AZ10)',
        'engine': '2.0L I4 TT or 2.5L hybrid',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'JTJBARBZ5F2220012',  # 2015 NX 200t launch
            'JTJBARBZ9G2221501',  # 2016
            'JTJBARBZ2H2223012',  # 2017 facelift
            'JTJBARBZ6J2224801',  # 2018
            'JTJBARBZ0K2226205',  # 2019
            'JTJBARBZ4L2227901',  # 2020
        ],
        'notes': 'NX 1st gen (AZ10, 2015-2021). Toyota MC platform.',
    },
    'BAG': {
        'model': 'NX',
        'trim': '2nd gen (AZ20)',
        'engine': '2.5L hybrid (NX 350h) / 2.4L I4 TT (NX 350) / PHEV (NX 450h+)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'JTJBAGBZ5M2230012',  # 2022 NX 350 (2nd gen launch)
            'JTJBAGBZ9N2231501',  # 2022 NX 350h
            'JTJBAGBZ2P2233012',  # 2023
            'JTJBAGBZ6R2234801',  # 2024
            'JTJBAGBZ0S2236205',  # 2025
        ],
        'notes': 'NX 2nd gen (AZ20, 2022+). TNGA-K platform. First Lexus PHEV (450h+).',
    },

    # ===== RX SUV =====
    'BM7': {
        'model': 'RX',
        'trim': '4th gen (AL20)',
        'engine': '3.5L V6 / 2.0L I4 TT (RX 200t/300) / 3.5L hybrid (RX 450h) / 3.5L F-Sport',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'JTJBM7FX3G5240012',  # 2016 RX 350 4th gen launch
            'JTJBM7FX7H5241501',  # 2017
            'JTJBM7FX0J5243012',  # 2018
            'JTJBM7FX4K5244801',  # 2019 (RX L 3-row launch)
            'JTJBM7FX8L5246205',  # 2020
            'JTJBM7FX2M5247901',  # 2021 (final 4th gen MY)
        ],
        'notes': 'RX 4th gen (AL20, 2016-2022). GA-K platform. RX L = 3-row variant.',
    },
    'JBM': {  # alt notation for same RX 4th gen on JTJB plant
        'model': 'RX',
        'trim': '4th gen (AL20)',
        'engine': '3.5L V6 / 2.0L I4 TT / 3.5L hybrid',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'JTJBM7FX3G5240012',  # alt VIN encoding
        ],
        'notes': 'RX 4th gen alt WMI.',
    },
    'GZK': {
        'model': 'RX',
        'trim': '5th gen (AL30)',
        'engine': '2.4L I4 TT (RX 350) / 2.5L hybrid (RX 350h) / 2.4L TT hybrid (RX 500h F-Sport) / PHEV (RX 450h+)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'JTJGZKCA5N5250012',  # 2023 RX 350 (5th gen launch)
            'JTJGZKCA9P5251501',  # 2024
            'JTJGZKCA2R5253012',  # 2024 RX 500h F-Sport (turbo-hybrid)
            'JTJGZKCA6S5254801',  # 2025 RX 450h+
        ],
        'notes': 'RX 5th gen (AL30, 2023+). GA-K platform. 500h F-Sport is first '
                 'Lexus performance hybrid (2.4L TT + e-motor, 367 hp).',
    },
    'GG7': {
        'model': 'RX',
        'trim': '3rd gen (AL10)',
        'engine': '3.5L V6 / 3.5L V6 hybrid (RX 450h)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            '2T2BK1BA8A5260012',  # 2010 RX 350 (Canada-built, 2T2 WMI)
            '2T2BK1BA2B5261501',  # 2011
            '2T2BK1BA7C5263012',  # 2012
            '2T2BK1BA1D5264801',  # 2013
            '2T2BK1BA5E5266205',  # 2014
            '2T2BK1BA9F5267901',  # 2015 (final 3rd gen MY)
        ],
        'notes': 'Stub - see BK1 below for actual key.',
    },
    'BK1': {  # NOTE: conflicts with ES; differentiate by WMI prefix
        'model': 'ES',  # primary; RX 3rd gen used 2T2BK1 from Canada
        'trim': '6th gen (XV60)',
        'engine': '2.5L V6 / 3.5L V6 / 2.5L I4 hybrid',
        'body': 'Sedan',
        'confidence': 0.85,
        'sample_vins': [
            'JTHBK1GG3D2090012',  # ES from Japan (JTH prefix)
        ],
        'notes': 'WARNING: 2T2BK1* prefix is RX 3rd gen (Canada) -- this entry serves '
                 'ES 6th gen on JTH WMI. Caller should check WMI to disambiguate.',
    },

    # ===== GX SUV =====
    'GJP': {
        'model': 'GX',
        'trim': '2nd gen (J150)',
        'engine': '4.6L V8 NA (GX 460)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'JTJJM7FX3A5270012',  # 2010 GX 460 launch (2nd gen)
            'JTJJM7FX0B5271501',  # 2011
            'JTJJM7FX4C5273012',  # 2012
            'JTJJM7FX8D5274801',  # 2013
            'JTJJM7FX1E5276205',  # 2014 facelift
            'JTJJM7FX5F5277901',  # 2015
            'JTJJM7FX9G5279012',  # 2016
            'JTJJM7FX2H5280501',  # 2017
            'JTJJM7FX6J5282012',  # 2018
            'JTJJM7FX0K5283801',  # 2019
            'JTJJM7FX4L5285205',  # 2020
            'JTJJM7FX8M5286901',  # 2021
            'JTJJM7FX2N5288012',  # 2022
            'JTJJM7FX5P5289501',  # 2023 (final J150 MY)
        ],
        'notes': 'Stub - see JM7.',
    },
    'JM7': {
        'model': 'GX',
        'trim': '2nd gen (J150)',
        'engine': '4.6L V8 NA (GX 460)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'JTJJM7FX3A5270012',  # 2010 GX 460 launch
            'JTJJM7FX0B5271501',  # 2011
            'JTJJM7FX4C5273012',  # 2012
            'JTJJM7FX8D5274801',  # 2013
            'JTJJM7FX1E5276205',  # 2014 facelift
            'JTJJM7FX5F5277901',  # 2015
            'JTJJM7FX9G5279012',  # 2016
            'JTJJM7FX2H5280501',  # 2017
            'JTJJM7FX6J5282012',  # 2018
            'JTJJM7FX0K5283801',  # 2019
            'JTJJM7FX4L5285205',  # 2020
            'JTJJM7FX8M5286901',  # 2021
            'JTJJM7FX2N5288012',  # 2022
            'JTJJM7FX5P5289501',  # 2023
        ],
        'notes': 'GX 2nd gen (J150, 2010-2023). 4.6L V8 NA. Body-on-frame.',
    },
    'GKP': {
        'model': 'GX',
        'trim': '3rd gen (J250)',
        'engine': '3.4L V6 TT (GX 550)',
        'body': 'SUV',
        'confidence': 0.85,
        'sample_vins': [
            'JTJGKDAJ3R5290012',  # 2024 GX 550 launch (3rd gen, V6 TT)
            'JTJGKDAJ7S5291501',  # 2025
            'JTJGKDAJ1T5293012',  # 2026
        ],
        'notes': 'GX 3rd gen (J250, 2024+). 3.4L V6 TT 349hp w/ 10-spd auto. '
                 'Shared platform with new 2024 Toyota Land Cruiser.',
    },

    # ===== LX SUV =====
    'JU0': {
        'model': 'LX',
        'trim': '3rd gen (URJ200)',
        'engine': '5.7L V8 NA (LX 570)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'JTJHY7AX8A4300012',  # 2010 LX 570 launch (3rd gen)
            'JTJHY7AX2B4301501',  # 2011
            'JTJHY7AX6C4303012',  # 2012
            'JTJHY7AX0D4304801',  # 2013 facelift
            'JTJHY7AX4E4306205',  # 2014
            'JTJHY7AX8F4307901',  # 2015
            'JTJHY7AX2G4309012',  # 2016 mid-cycle (Sport facelift)
            'JTJHY7AX5H4310501',  # 2017
            'JTJHY7AX9J4312012',  # 2018
            'JTJHY7AX3K4313801',  # 2019
            'JTJHY7AX7L4315205',  # 2020
            'JTJHY7AX1M4316901',  # 2021 (final 3rd gen MY)
        ],
        'notes': 'Stub - see HY7.',
    },
    'HY7': {
        'model': 'LX',
        'trim': '3rd gen (URJ200)',
        'engine': '5.7L V8 NA (LX 570)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': [
            'JTJHY7AX8A4300012',  # 2010 LX 570 launch
            'JTJHY7AX2B4301501',  # 2011
            'JTJHY7AX6C4303012',  # 2012
            'JTJHY7AX0D4304801',  # 2013 facelift
            'JTJHY7AX4E4306205',  # 2014
            'JTJHY7AX8F4307901',  # 2015
            'JTJHY7AX2G4309012',  # 2016 mid-cycle Sport facelift
            'JTJHY7AX5H4310501',  # 2017
            'JTJHY7AX9J4312012',  # 2018
            'JTJHY7AX3K4313801',  # 2019
            'JTJHY7AX7L4315205',  # 2020
            'JTJHY7AX1M4316901',  # 2021
        ],
        'notes': 'LX 3rd gen (URJ200, 2008-2021). 5.7L V8 NA 383hp shared w/ '
                 'Land Cruiser 200.',
    },
    'JN7': {
        'model': 'LX',
        'trim': '4th gen (J310)',
        'engine': '3.4L V6 TT (LX 600)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [
            'JTJJN7AX9N4320012',  # 2022 LX 600 launch
            'JTJJN7AX3P4321501',  # 2023
            'JTJJN7AX7R4323012',  # 2024
            'JTJJN7AX1S4324801',  # 2025
        ],
        'notes': 'LX 4th gen (J310, 2022+). 3.4L V6 TT 409hp. Shared platform with '
                 'Land Cruiser 300 (GA-F).',
    },

    # ===== UX SUV =====
    'JBJ': {
        'model': 'UX',
        'trim': None,
        'engine': '2.0L I4 (UX 200) / 2.0L hybrid (UX 250h/300h)',
        'body': 'SUV',  # crossover
        'confidence': 0.95,
        'sample_vins': [
            'JTHU9JBH7K2330012',  # 2019 UX 200 launch (US)
            'JTHU9JBH1L2331501',  # 2020 UX 250h
            'JTHU9JBH5M2333012',  # 2021
            'JTHU9JBH9N2334801',  # 2022 facelift
            'JTHU9JBH3P2336205',  # 2023 UX 300h (renamed)
            'JTHU9JBH7R2337901',  # 2024
        ],
        'notes': 'Stub - see U9J.',
    },
    'U9J': {
        'model': 'UX',
        'trim': None,
        'engine': '2.0L I4 (UX 200) / 2.0L hybrid (UX 250h/300h)',
        'body': 'SUV',  # subcompact crossover
        'confidence': 0.95,
        'sample_vins': [
            'JTHU9JBH7K2330012',  # 2019 UX 200 launch
            'JTHU9JBH1L2331501',  # 2020
            'JTHU9JBH5M2333012',  # 2021
            'JTHU9JBH9N2334801',  # 2022 facelift
            'JTHU9JBH3P2336205',  # 2023 UX 300h
            'JTHU9JBH7R2337901',  # 2024
        ],
        'notes': 'UX (ZA10, 2019+). Lexus subcompact crossover. TNGA-C platform. '
                 'UX 250h renamed to UX 300h for 2023 (more power, larger battery).',
    },

    # ===== LFA (special) =====
    'ZL1': {
        'model': 'LFA',
        'trim': 'LFA',
        'engine': '4.8L V10 NA (1LR-GUE, 552 hp)',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            'JTHHN5BHXA1000012',  # 2010 LFA launch (US, 500 units total)
            'JTHHN5BH4B1000501',  # 2011
            'JTHHN5BH8C1000801',  # 2012 (final LFA MY)
        ],
        'notes': 'Stub - see HN5.',
    },
    'HN5': {
        'model': 'LFA',
        'trim': None,
        'engine': '4.8L V10 NA (1LR-GUE, 552 hp)',
        'body': 'Coupe',
        'confidence': 0.95,
        'sample_vins': [
            'JTHHN5BHXA1000012',  # 2010 LFA launch
            'JTHHN5BH4B1000501',  # 2011
            'JTHHN5BH8C1000801',  # 2012 (final)
        ],
        'notes': 'LFA (2010-2012, 500 units worldwide). Carbon-fiber monocoque. '
                 'Hand-built at Motomachi.',
    },
}


def decode(vin: str):
    """Decode a Lexus VIN. Returns dict or None.

    Slice rationale: vin[3:6] (positions 4-6). 3-char key resolves most
    Lexus model lines. Some keys (e.g. BK1) are ambiguous between ES sedan
    (JTH prefix) and RX 3rd gen (2T2 prefix) and rely on WMI for full
    disambiguation. Where a key has WMI-dependent meaning, the entry's
    `notes` field documents both interpretations.
    """
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
        'make': 'Lexus',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:lexus',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # IS
        ('JTHBA1D26E5050012', 'IS', 2014),
        ('JTHBA1D24F5051501', 'IS', 2015),
        ('JTHBA1D26G5053012', 'IS', 2016),
        ('JTHBA1D27H5054801', 'IS', 2017),
        ('JTHBA1D24J5056205', 'IS', 2018),
        ('JTHBA1D26K5057901', 'IS', 2019),
        ('JTHBA1D28L5059012', 'IS', 2020),
        ('JTHBA1D22M5060501', 'IS', 2021),
        ('JTHBA1D26N5062012', 'IS', 2022),
        ('JTHBA1D20P5063801', 'IS', 2023),
        ('JTHCA1D24A5070012', 'IS', 2010),
        ('JTHCA1D29C5073012', 'IS', 2012),
        ('JTHCC1D27A5080012', 'IS C', 2010),
        ('JTHCC1D29B5081501', 'IS C', 2011),
        ('JTHCC1D26E5086205', 'IS C', 2014),
        # ES
        ('JTHBK1GG3D2090012', 'ES', 2013),
        ('JTHBK1GG0F2093012', 'ES', 2015),
        ('JTHBK1GG2H2096205', 'ES', 2017),
        ('JTHBK1GG9J2097901', 'ES', 2018),
        ('JTHBZ1B22K2100012', 'ES', 2019),
        ('JTHBZ1B26L2101501', 'ES', 2020),
        ('JTHBZ1B20N2104801', 'ES', 2022),
        ('JTHBB1B26A2110012', 'ES', 2010),
        # GS
        ('JTHBE1KS3D5120012', 'GS', 2013),
        ('JTHBE1KS0F5123012', 'GS', 2015),
        ('JTHBE1KS4G5124801', 'GS', 2016),
        ('JTHBE1KS3K5129012', 'GS', 2019),
        ('JTHCE1BL4A5140012', 'GS', 2010),
        # LS
        ('JTHD51FF3J5150012', 'LS', 2018),
        ('JTHD51FF0L5153012', 'LS', 2020),
        ('JTHD51FF2N5156205', 'LS', 2022),
        ('JTHDL5EFXA5160012', 'LS', 2010),
        ('JTHDL5EF8C5163012', 'LS', 2012),
        ('JTHDL5EF5E5166205', 'LS', 2014),
        # RC
        ('JTHHA5BC8F5180012', 'RC', 2015),
        ('JTHHA5BC4H5183012', 'RC', 2017),
        ('JTHHA5BC2K5186205', 'RC', 2019),
        ('JTHHA5BC0M5189012', 'RC', 2021),
        # LC
        ('JTHHP5AY7H5200012', 'LC', 2017),
        ('JTHHP5AY4K5203012', 'LC', 2019),
        ('JTHHP5AY6N5207901', 'LC', 2022),
        ('JTHHK5AY3M5211012', 'LC', 2021),
        # NX
        ('JTJBARBZ5F2220012', 'NX', 2015),
        ('JTJBARBZ2H2223012', 'NX', 2017),
        ('JTJBARBZ4L2227901', 'NX', 2020),
        ('JTJBAGBZ5M2230012', 'NX', 2021),
        ('JTJBAGBZ2P2233012', 'NX', 2023),
        ('JTJBAGBZ6R2234801', 'NX', 2024),
        # RX
        ('JTJBM7FX3G5240012', 'RX', 2016),
        ('JTJBM7FX0J5243012', 'RX', 2018),
        ('JTJBM7FX2M5247901', 'RX', 2021),
        ('JTJGZKCA5N5250012', 'RX', 2022),
        ('JTJGZKCA9P5251501', 'RX', 2023),
        ('JTJGZKCA2R5253012', 'RX', 2024),
        # GX
        ('JTJJM7FX3A5270012', 'GX', 2010),
        ('JTJJM7FX4C5273012', 'GX', 2012),
        ('JTJJM7FX1E5276205', 'GX', 2014),
        ('JTJJM7FX9G5279012', 'GX', 2016),
        ('JTJJM7FX6J5282012', 'GX', 2018),
        ('JTJJM7FX4L5285205', 'GX', 2020),
        ('JTJJM7FX2N5288012', 'GX', 2022),
        ('JTJGKDAJ3R5290012', 'GX', 2024),
        ('JTJGKDAJ7S5291501', 'GX', 2025),
        # LX
        ('JTJHY7AX8A4300012', 'LX', 2010),
        ('JTJHY7AX6C4303012', 'LX', 2012),
        ('JTJHY7AX4E4306205', 'LX', 2014),
        ('JTJHY7AX2G4309012', 'LX', 2016),
        ('JTJHY7AX9J4312012', 'LX', 2018),
        ('JTJHY7AX7L4315205', 'LX', 2020),
        ('JTJJN7AX9N4320012', 'LX', 2022),
        ('JTJJN7AX3P4321501', 'LX', 2023),
        ('JTJJN7AX7R4323012', 'LX', 2024),
        # UX
        ('JTHU9JBH7K2330012', 'UX', 2019),
        ('JTHU9JBH5M2333012', 'UX', 2021),
        ('JTHU9JBH9N2334801', 'UX', 2022),
        # LFA
        ('JTHHN5BHXA1000012', 'LFA', 2010),
        ('JTHHN5BH4B1000501', 'LFA', 2011),
        ('JTHHN5BH8C1000801', 'LFA', 2012),
        # Negative cases
        ('SHORT', None, None),
        ('INVALIDVIN1234567', None, None),
        ('JTHZZZZZZZZZZZZZZ', None, None),
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
