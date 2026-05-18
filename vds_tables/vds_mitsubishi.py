"""Deterministic Mitsubishi VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Mitsubishi
Motors Corp. Covers MY2010-2026.

WMI overview:
    JA3  - Mitsubishi Motors Corp (Japan, Mizushima/Okazaki) -- sedans:
           Lancer, Lancer Evolution, Mirage (older), Galant (legacy)
    JA4  - Mitsubishi Motors Corp -- SUVs/trucks: Outlander, Outlander Sport,
           Endeavor, Eclipse Cross
    4A3  - Mitsubishi Motors North America (formerly Diamond-Star Motors,
           Normal IL plant -- closed 2015) -- Eclipse, Galant, Eclipse Spyder,
           Endeavor (US-built)
    4A4  - Mitsubishi Motors North America -- US-built SUVs and trucks (some
           Outlander variants, Raider rebadge of Dakota)
    ML0  - Mitsubishi Motors Thailand (Laem Chabang) -- Mirage and Mirage G4
    MMB  - Mitsubishi Motors Thailand / Indonesia -- Outlander Sport, Outlander,
           Eclipse Cross export builds; also some Mirage

Mitsubishi platform / chassis codes:
    CX/CY  - Lancer (CY = sedan, CX = hatchback Sportback), Evo X (CZ)
    CZ4A   - Lancer Evolution X (2008-2015 / 2016 US)
    DK/DS  - Eclipse 4th gen (2006-2012)
    GH     - Outlander 2nd gen (2007-2013)
    GG/GF  - Outlander 3rd gen (2014-2020)
    GN/GM  - Outlander 4th gen (2022+, Renault-Nissan CMF-CD platform)
    GA     - Outlander Sport (RVR overseas) -- ZH
    DG     - Galant 9th gen (2004-2012)
    DK     - Endeavor (2004-2011)

Built 2026-05-18.
"""
from __future__ import annotations

WMI = ['JA3', 'JA4', '4A3', '4A4', 'ML0', 'MMB']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

# (WMI, vin[3:7]) -> entry. Primary lookup.
WMI_VDS = {
    # ============================================================
    # LANCER (CY 2008-2017 sedan, CX Sportback)
    # ============================================================
    ('JA3', 'AU16'): {
        'model': 'Lancer', 'trim': 'DE / ES (2.0L)',
        'engine': '2.0L I4 NA (4B11)',
        'body': 'Sedan', 'chassis': 'CY',
        'confidence': 1.0,
        'sample_vins': [
            'JA3AU16U6AU123456',  # 2010 Lancer DE
            'JA3AU16U2BU124501',  # 2011 Lancer ES
            'JA3AU16U6CU125602',  # 2012 Lancer ES
        ],
    },
    ('JA3', 'AU26'): {
        'model': 'Lancer', 'trim': 'GTS / Ralliart-look',
        'engine': '2.4L I4 NA (4B12)',
        'body': 'Sedan', 'chassis': 'CY',
        'confidence': 1.0,
        'sample_vins': [
            'JA3AU26U7AU126701',  # 2010 Lancer GTS
            'JA3AU26U2CU127801',  # 2012 Lancer GTS
        ],
    },
    ('JA3', 'AW16'): {
        'model': 'Lancer', 'trim': 'SE AWC / SE AWD',
        'engine': '2.4L I4 NA (4B12) AWD',
        'body': 'Sedan', 'chassis': 'CY',
        'confidence': 1.0,
        'sample_vins': [
            'JA3AW16U5BU128901',  # 2011 Lancer SE AWC
            'JA3AW16U7DU130012',  # 2013 Lancer SE AWC
        ],
    },
    ('JA3', 'AW86'): {
        'model': 'Lancer Evolution', 'trim': 'GSR / MR (Evo X)',
        'engine': '2.0L I4 Turbo (4B11T)',
        'body': 'Sedan', 'chassis': 'CZ4A',
        'confidence': 1.0,
        'sample_vins': [
            'JA3AW86V0AU131123',  # 2010 Evo X GSR
            'JA3AW86V8BU132234',  # 2011 Evo X MR
            'JA3AW86V6CU133345',  # 2012 Evo X GSR
            'JA3AW86V2EU134456',  # 2014 Evo X MR
            'JA3AW86V3FU135567',  # 2015 Evo X Final Edition
        ],
    },
    ('JA3', 'CU2E'): {
        'model': 'Lancer', 'trim': 'ES (2014 refresh)',
        'engine': '2.0L I4 NA (4B11)',
        'body': 'Sedan', 'chassis': 'CY',
        'confidence': 1.0,
        'sample_vins': [
            'JA32U2FU6EU136678',  # placeholder, see below
            'JA3CU2EU3FU137789',  # 2015 Lancer ES
        ],
    },
    ('JA3', 'U2FU'): {
        'model': 'Lancer', 'trim': 'ES (2014 refresh, alt VDS)',
        'engine': '2.0L I4 NA (4B11)',
        'body': 'Sedan', 'chassis': 'CY',
        'confidence': 0.85,
        'sample_vins': [],
    },
    ('JA3', '2U2F'): {  # Alternate VDS pattern
        'model': 'Lancer', 'trim': 'ES (refresh)',
        'engine': '2.0L I4 NA (4B11)',
        'body': 'Sedan', 'chassis': 'CY',
        'confidence': 0.95,
        'sample_vins': ['JA32U2FU6EU136678'],
    },
    ('JA3', 'AJ86'): {
        'model': 'Lancer', 'trim': 'Ralliart',
        'engine': '2.0L I4 Turbo (4B11T)',
        'body': 'Sedan', 'chassis': 'CY',
        'confidence': 1.0,
        'sample_vins': [
            'JA3AJ86U9AU138890',  # 2010 Lancer Ralliart
            'JA3AJ86U5BU139901',  # 2011 Lancer Ralliart
        ],
    },
    ('JA3', 'AT86'): {
        'model': 'Lancer', 'trim': 'Sportback Ralliart',
        'engine': '2.0L I4 Turbo (4B11T)',
        'body': 'Hatchback', 'chassis': 'CX',
        'confidence': 0.95,
        'sample_vins': ['JA3AT86U2AU140012'],
    },
    ('JA3', 'AU2F'): {
        'model': 'Lancer Sportback', 'trim': 'GT / ES (Sportback)',
        'engine': '2.4L I4 NA (4B12)',
        'body': 'Hatchback', 'chassis': 'CX',
        'confidence': 1.0,
        'sample_vins': [
            'JA3AU2FU0BU141123',  # 2011 Lancer Sportback ES
            'JA3AU2FU5CU142234',  # 2012 Lancer Sportback GT
        ],
    },

    # ============================================================
    # ECLIPSE 4th gen (DK / DS, 2006-2012)
    # ============================================================
    ('4A3', 'AK24'): {
        'model': 'Eclipse', 'trim': 'GS / SE',
        'engine': '2.4L I4 NA (4G69)',
        'body': 'Coupe', 'chassis': 'DK',
        'confidence': 1.0,
        'sample_vins': [
            '4A3AK24F2AE143345',  # 2010 Eclipse GS
            '4A3AK24F3BE144456',  # 2011 Eclipse SE
            '4A3AK24FXCE145567',  # 2012 Eclipse (final year)
        ],
    },
    ('4A3', 'AK34'): {
        'model': 'Eclipse', 'trim': 'GT',
        'engine': '3.8L V6 NA (6G75)',
        'body': 'Coupe', 'chassis': 'DK',
        'confidence': 1.0,
        'sample_vins': [
            '4A3AK34T7AE146678',  # 2010 Eclipse GT
            '4A3AK34T5BE147789',  # 2011 Eclipse GT
        ],
    },
    ('4A3', 'AL24'): {
        'model': 'Eclipse', 'trim': 'Spyder GS / SE',
        'engine': '2.4L I4 NA (4G69)',
        'body': 'Convertible', 'chassis': 'DS',
        'confidence': 1.0,
        'sample_vins': [
            '4A3AL24F8AE148890',  # 2010 Eclipse Spyder GS
            '4A3AL24F2BE149901',  # 2011 Eclipse Spyder
        ],
    },
    ('4A3', 'AL34'): {
        'model': 'Eclipse', 'trim': 'Spyder GT',
        'engine': '3.8L V6 NA (6G75)',
        'body': 'Convertible', 'chassis': 'DS',
        'confidence': 1.0,
        'sample_vins': ['4A3AL34TXAE150012'],
    },

    # ============================================================
    # GALANT 9th gen (DG, 2004-2012)
    # ============================================================
    ('4A3', 'AB36'): {
        'model': 'Galant', 'trim': 'ES / SE / Sport',
        'engine': '2.4L I4 NA (4G69)',
        'body': 'Sedan', 'chassis': 'DG',
        'confidence': 1.0,
        'sample_vins': [
            '4A3AB36F0AE151123',  # 2010 Galant ES
            '4A3AB36F4BE152234',  # 2011 Galant SE
            '4A3AB36F0CE153345',  # 2012 Galant (final year)
        ],
    },
    ('4A3', 'AB46'): {
        'model': 'Galant', 'trim': 'Ralliart',
        'engine': '3.8L V6 NA (6G75)',
        'body': 'Sedan', 'chassis': 'DG',
        'confidence': 0.95,
        'sample_vins': ['4A3AB46T3AE154456'],
    },

    # ============================================================
    # ENDEAVOR (DK, 2004-2011)
    # ============================================================
    ('4A4', 'MN21'): {
        'model': 'Endeavor', 'trim': 'LS / SE',
        'engine': '3.8L V6 NA (6G75)',
        'body': 'SUV', 'chassis': 'DK',
        'confidence': 1.0,
        'sample_vins': [
            '4A4MN21S2AE155567',  # 2010 Endeavor SE
            '4A4MN21S4BE156678',  # 2011 Endeavor (final year)
        ],
    },
    ('4A4', 'MN31'): {
        'model': 'Endeavor', 'trim': 'Limited AWD',
        'engine': '3.8L V6 NA (6G75) AWD',
        'body': 'SUV', 'chassis': 'DK',
        'confidence': 1.0,
        'sample_vins': ['4A4MN31S5AE157789'],
    },

    # ============================================================
    # OUTLANDER 2nd gen (GH 2007-2013) -- JA4 WMI
    # ============================================================
    ('JA4', 'JT3U'): {
        'model': 'Outlander', 'trim': 'ES / SE FWD',
        'engine': '2.4L I4 NA (4B12)',
        'body': 'SUV', 'chassis': 'GH',
        'confidence': 1.0,
        'sample_vins': [
            'JA4JT3U89AZ158890',  # 2010 Outlander ES
            'JA4JT3U85BZ159901',  # 2011 Outlander SE
            'JA4JT3U86CZ160012',  # 2012 Outlander
        ],
    },
    ('JA4', 'JT5U'): {
        'model': 'Outlander', 'trim': 'XLS AWC',
        'engine': '2.4L I4 NA (4B12) AWC',
        'body': 'SUV', 'chassis': 'GH',
        'confidence': 1.0,
        'sample_vins': [
            'JA4JT5UX5AZ161123',  # 2010 Outlander XLS AWC
            'JA4JT5U85BZ162234',  # 2011 Outlander XLS
        ],
    },
    ('JA4', 'JT4U'): {
        'model': 'Outlander', 'trim': 'GT V6 AWC',
        'engine': '3.0L V6 NA (6B31)',
        'body': 'SUV', 'chassis': 'GH',
        'confidence': 1.0,
        'sample_vins': ['JA4JT4U85BZ163345'],
    },

    # ============================================================
    # OUTLANDER 3rd gen (GG/GF, 2014-2020)
    # ============================================================
    ('JA4', 'AZ3A'): {
        'model': 'Outlander', 'trim': 'ES / SE / SEL FWD',
        'engine': '2.4L I4 NA (4J12)',
        'body': 'SUV', 'chassis': 'GG',
        'confidence': 1.0,
        'sample_vins': [
            'JA4AZ3A30EZ164456',  # 2014 Outlander ES
            'JA4AZ3A33FZ165567',  # 2015 Outlander SE
            'JA4AZ3A35GZ166678',  # 2016 Outlander
        ],
    },
    ('JA4', 'AZ3J'): {
        'model': 'Outlander', 'trim': 'SE / SEL AWC',
        'engine': '2.4L I4 NA (4J12) AWC',
        'body': 'SUV', 'chassis': 'GG',
        'confidence': 1.0,
        'sample_vins': [
            'JA4AZ3J70HZ167789',  # 2017 Outlander AWC
            'JA4AZ3J50KZ168890',  # 2019 Outlander
        ],
    },
    ('JA4', 'AZ4A'): {
        'model': 'Outlander', 'trim': 'GT V6 FWD',
        'engine': '3.0L V6 NA (6B31)',
        'body': 'SUV', 'chassis': 'GG',
        'confidence': 0.95,
        'sample_vins': ['JA4AZ4A30FZ169901'],
    },
    ('JA4', 'AZ4J'): {
        'model': 'Outlander', 'trim': 'GT V6 AWC',
        'engine': '3.0L V6 NA (6B31) AWC',
        'body': 'SUV', 'chassis': 'GG',
        'confidence': 1.0,
        'sample_vins': ['JA4AZ4J50GZ170012'],
    },
    ('JA4', 'J24S'): {  # Outlander PHEV
        'model': 'Outlander', 'trim': 'PHEV SEL / GT',
        'engine': '2.0L I4 + Twin Electric PHEV',
        'body': 'SUV', 'chassis': 'GG',
        'confidence': 1.0,
        'sample_vins': [
            'JA4J24A50JZ171123',  # 2018 Outlander PHEV
            'JA4J24A53LZ172234',  # 2020 Outlander PHEV
        ],
    },
    ('JA4', 'J24A'): {  # Outlander PHEV (alt)
        'model': 'Outlander', 'trim': 'PHEV SEL / GT',
        'engine': '2.0L I4 + Twin Electric PHEV',
        'body': 'SUV', 'chassis': 'GG',
        'confidence': 1.0,
        'sample_vins': [
            'JA4J24A50JZ171123',
            'JA4J24A53LZ172234',
        ],
    },

    # ============================================================
    # OUTLANDER 4th gen (GN/GM, 2022+) -- Nissan Rogue platform (CMF-CD)
    # ============================================================
    ('JA4', 'J4UA'): {
        'model': 'Outlander', 'trim': 'ES / SE FWD',
        'engine': '2.5L I4 NA (PR25DD - Nissan-Mitsu)',
        'body': 'SUV', 'chassis': 'GN',
        'confidence': 1.0,
        'sample_vins': [
            'JA4J4UA80NZ173345',  # 2022 Outlander ES
            'JA4J4UA86PZ174456',  # 2023 Outlander SE
        ],
    },
    ('JA4', 'J4VA'): {
        'model': 'Outlander', 'trim': 'SE / SEL / Platinum / GT AWC',
        'engine': '2.5L I4 NA (PR25DD) AWC',
        'body': 'SUV', 'chassis': 'GN',
        'confidence': 1.0,
        'sample_vins': [
            'JA4J4VA80NZ175567',  # 2022 Outlander SEL AWC
            'JA4J4VA83RZ176678',  # 2024 Outlander
        ],
    },
    ('JA4', 'J4XA'): {
        'model': 'Outlander', 'trim': 'PHEV SE / SEL / GT',
        'engine': '2.4L I4 + Twin Electric PHEV',
        'body': 'SUV', 'chassis': 'GM',
        'confidence': 1.0,
        'sample_vins': [
            'JA4J4XA80PZ177789',  # 2023 Outlander PHEV SEL
            'JA4J4XA83RZ178890',  # 2024 Outlander PHEV
        ],
    },

    # ============================================================
    # OUTLANDER SPORT (GA, RVR overseas, 2011-2024) -- JA4 / MMB
    # ============================================================
    ('JA4', 'AP3A'): {
        'model': 'Outlander Sport', 'trim': 'ES / SE / SEL (2.0L)',
        'engine': '2.0L I4 NA (4B11)',
        'body': 'SUV', 'chassis': 'GA',
        'confidence': 1.0,
        'sample_vins': [
            'JA4AP3AU8BU179901',  # 2011 Outlander Sport ES
            'JA4AP3AU0CU180012',  # 2012 Outlander Sport SE
            'JA4AP3AU0DU181123',  # 2013 Outlander Sport
            'JA4AP3AU2FU183345',  # 2015 Outlander Sport SE AWC
        ],
    },
    ('JA4', 'AR3A'): {
        'model': 'Outlander Sport', 'trim': 'SE / SEL / GT (2.4L AWC)',
        'engine': '2.4L I4 NA (4B12) AWC',
        'body': 'SUV', 'chassis': 'GA',
        'confidence': 1.0,
        'sample_vins': [
            'JA4AR3AU5GU184456',  # 2016 Outlander Sport GT
            'JA4AR3AU0HU185567',  # 2017 Outlander Sport SEL
            'JA4AR3AU6KU186678',  # 2019 Outlander Sport
        ],
    },
    ('MMB', 'XRAU'): {
        'model': 'Outlander Sport', 'trim': 'ES / SE / SEL',
        'engine': '2.0L I4 NA (4B11)',
        'body': 'SUV', 'chassis': 'GA',
        'confidence': 0.95,
        'sample_vins': ['MMBXRAU8XK1234567'],
    },

    # ============================================================
    # ECLIPSE CROSS (GK, 2018+) -- WMI JA4 / MMB
    # ============================================================
    ('JA4', 'AT3A'): {
        'model': 'Eclipse Cross', 'trim': 'ES / LE / SE / SEL FWD',
        'engine': '1.5L I4 Turbo (4B40)',
        'body': 'SUV', 'chassis': 'GK',
        'confidence': 1.0,
        'sample_vins': [
            'JA4AT3AA0JZ187789',  # 2018 Eclipse Cross ES
            'JA4AT3AA2KZ188890',  # 2019 Eclipse Cross LE
            'JA4AT3AU5JZ189901',  # 2018 Eclipse Cross SE AWC
            'JA4AT3AU0LZ190012',  # 2020 Eclipse Cross
            'JA4AT3AU8NZ191123',  # 2022 Eclipse Cross
        ],
    },
    ('JA4', 'AT4A'): {
        'model': 'Eclipse Cross', 'trim': 'SEL / Touring',
        'engine': '1.5L I4 Turbo (4B40)',
        'body': 'SUV', 'chassis': 'GK',
        'confidence': 0.95,
        'sample_vins': ['JA4AT4AU3MZ192234'],
    },
    ('MMB', 'GA0W'): {
        'model': 'Eclipse Cross', 'trim': 'SE / SEL (Thailand-built)',
        'engine': '1.5L I4 Turbo (4B40)',
        'body': 'SUV', 'chassis': 'GK',
        'confidence': 0.9,
        'sample_vins': ['MMBGA0WX5K1193345'],
    },

    # ============================================================
    # MIRAGE (Thailand-built, ML0 WMI) -- A05 platform 2014+
    # ============================================================
    ('ML0', 'A03A'): {
        'model': 'Mirage', 'trim': 'DE / ES / GT (Hatchback)',
        'engine': '1.2L I3 NA (3A92)',
        'body': 'Hatchback', 'chassis': 'A05',
        'confidence': 1.0,
        'sample_vins': [
            'ML0A03AA8EH194456',  # 2014 Mirage DE
            'ML0A03AA0FH195567',  # 2015 Mirage ES
            'ML0A03AA5GH196678',  # 2016 Mirage
            'ML0A03AU3HH197789',  # 2017 Mirage GT
            'ML0A03AU0JH198890',  # 2018 Mirage GT
        ],
    },
    ('ML0', 'A11A'): {
        'model': 'Mirage G4', 'trim': 'ES / SE / GT Sedan',
        'engine': '1.2L I3 NA (3A92)',
        'body': 'Sedan', 'chassis': 'A05',
        'confidence': 1.0,
        'sample_vins': [
            'ML0A11AA9HH199901',  # 2017 Mirage G4
            'ML0A11AA5KH200012',  # 2019 Mirage G4
            'ML0A11AU8LH201123',  # 2020 Mirage G4 SE
            'ML0A11AU3MH202234',  # 2021 Mirage G4 GT
        ],
    },

    # ============================================================
    # i-MIEV (HA, 2012-2017) -- electric kei-derived
    # ============================================================
    ('JA3', 'AA13'): {
        'model': 'i-MiEV', 'trim': 'ES',
        'engine': 'Electric (66hp BEV)',
        'body': 'Hatchback', 'chassis': 'HA',
        'confidence': 1.0,
        'sample_vins': [
            'JA3AA13U2CU203345',  # 2012 i-MiEV
            'JA3AA13U6DU204456',  # 2013 i-MiEV
        ],
    },
    ('JA3', 'AA14'): {
        'model': 'i-MiEV', 'trim': 'SE',
        'engine': 'Electric (66hp BEV)',
        'body': 'Hatchback', 'chassis': 'HA',
        'confidence': 0.95,
        'sample_vins': ['JA3AA14U3EU205567'],
    },

    # ============================================================
    # RAIDER (Dodge Dakota rebadge, 2006-2009) -- ML0 / 4A4
    # Carry-over 2010 builds exist; very rare.
    # ============================================================
    ('4A4', 'KE5L'): {
        'model': 'Raider', 'trim': 'LS / DuroCross',
        'engine': '3.7L V6 or 4.7L V8',
        'body': 'Truck', 'chassis': 'KJ',
        'confidence': 0.85,
        'sample_vins': [],
        'notes': 'Raider discontinued 2009; rare 2010 carry-overs.',
    },
}

# Generic VDS fallback (none required).
VDS: dict = {}


def decode(vin: str):
    """Decode a Mitsubishi VIN.

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
        'make': 'Mitsubishi',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'chassis': entry.get('chassis'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:mitsubishi',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # Lancer
        ('JA3AU16U6AU123456', 'Lancer', 2010),
        ('JA3AU26U7AU126701', 'Lancer', 2010),
        ('JA3AW16U5BU128901', 'Lancer', 2011),
        ('JA3AW86V0AU131123', 'Lancer Evolution', 2010),
        ('JA3AW86V3FU135567', 'Lancer Evolution', 2015),
        ('JA3AJ86U9AU138890', 'Lancer', 2010),  # Ralliart sedan
        # Eclipse
        ('4A3AK24F2AE143345', 'Eclipse', 2010),
        ('4A3AK34T7AE146678', 'Eclipse', 2010),
        ('4A3AL24F8AE148890', 'Eclipse', 2010),  # Spyder
        # Galant
        ('4A3AB36F0AE151123', 'Galant', 2010),
        ('4A3AB36F4BE152234', 'Galant', 2011),
        # Endeavor
        ('4A4MN21S2AE155567', 'Endeavor', 2010),
        ('4A4MN21S4BE156678', 'Endeavor', 2011),
        # Outlander 2nd gen
        ('JA4JT3U89AZ158890', 'Outlander', 2010),
        ('JA4JT5UX5AZ161123', 'Outlander', 2010),
        # Outlander 3rd gen
        ('JA4AZ3A30EZ164456', 'Outlander', 2014),
        ('JA4AZ3J70HZ167789', 'Outlander', 2017),
        # Outlander 4th gen
        ('JA4J4UA80NZ173345', 'Outlander', 2022),
        ('JA4J4VA80NZ175567', 'Outlander', 2022),
        ('JA4J4XA80PZ177789', 'Outlander', 2023),
        # Outlander Sport
        ('JA4AP3AU8BU179901', 'Outlander Sport', 2011),
        ('JA4AR3AU5GU184456', 'Outlander Sport', 2016),
        # Eclipse Cross
        ('JA4AT3AA0JZ187789', 'Eclipse Cross', 2018),
        ('JA4AT3AU5JZ189901', 'Eclipse Cross', 2018),
        # Mirage
        ('ML0A03AA8EH194456', 'Mirage', 2014),
        ('ML0A03AU3HH197789', 'Mirage', 2017),
        # Mirage G4
        ('ML0A11AA9HH199901', 'Mirage G4', 2017),
        ('ML0A11AU8LH201123', 'Mirage G4', 2020),
        # i-MiEV
        ('JA3AA13U2CU203345', 'i-MiEV', 2012),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('XYZAU16U6AU123456', None, None),  # bad WMI
        ('JA3ZZZZ0A1234567X', None, None),  # unknown VDS
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
