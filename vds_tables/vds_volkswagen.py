"""Deterministic Volkswagen VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/engine/body for Volkswagen AG
US production. Covers MY2010-2026.

WMI overview (per task spec):
    WVW  - Volkswagen AG (Wolfsburg / Emden / EU): Golf, GTI, Golf R,
           Sportwagen, Alltrack, e-Golf, CC, Eos, Arteon, MK8 GTI/R, some
           Touareg and Tiguan early.
    WV1  - Volkswagen commercial (Crafter, Caddy -- EU only)
    WV2  - Volkswagen vans (Multivan, Eurovan / T-series)
    1VW  - VW Chattanooga, USA -- Passat NMS (B7) for US market
    3VW  - VW Puebla, Mexico -- Jetta, Beetle, Rabbit (legacy)

Notes on out-of-scope coverage:
  - Atlas / 2nd-gen Tiguan / ID.4: use 1V2 / 3VV WMIs (Chattanooga/Puebla MQB)
    which are not in this module's WMI scope.
  - Routan minivan: uses 2V4 (Windsor) -- also out of scope.

Key positions (1-indexed; 0-indexed slices):
    1-3   WMI                     vin[0:3]
    4     Series / model line     vin[3]
    5-6   Body type / equipment   vin[4:6]
    7     Restraint / safety      vin[6]
    8     Engine code             vin[7]
    9     Check digit             vin[8]
    10    Model year (ISO 3779)   vin[9]
    11    Plant code              vin[10]
    12-17 Sequential serial       vin[11:17]

VDS key = vin[3:7] (4 chars). The same VDS code can mean different models
under different WMIs (e.g. WVW AN7A = Arteon, 1VW AN7A would be a Passat
variant). We disambiguate via (WMI, VDS_key) tuples in WMI_VDS where needed.

Built 2026-05-18.
"""
from __future__ import annotations

WMI = ['WVW', 'WV1', 'WV2', '1VW', '3VW']

YEAR_CODES = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

# WMI-keyed VDS lookup. We always go through this map first; if no match,
# we fall back to the generic VDS table. This handles the case where the
# same 4-char code (e.g. 'AN7A') belongs to different models under WVW vs 1VW.
WMI_VDS = {
    # ============================================================
    # JETTA SEDAN (3VW Puebla) -- A5/A6/A7 generations.
    # vin[3:7] patterns: AZ7A (S/SE 2.5L), AX7A (S 1.4T), BV7A (SE/SEL 1.8T),
    # 4T7A (GLI 2.0T), CB7B (A7 SE/SEL 1.4T), AB7B (A7 S), 5T7B (A7 GLI 2.0T)
    # ============================================================
    ('3VW', 'AZ7A'): {
        'model': 'Jetta', 'trim': 'S / SE',
        'engine': '2.5L I5 NA or 2.0L I4 NA',
        'body': 'Sedan', 'chassis': 'A5/A6',
        'confidence': 1.0,
        'sample_vins': [
            '3VWAZ7AJ0AM123456',  # 2010 Jetta SE 2.5
            '3VWAZ7AJ4CM124501',  # 2012 Jetta S 2.0
        ],
    },
    ('3VW', 'BZ7A'): {
        'model': 'Jetta', 'trim': 'SEL',
        'engine': '2.5L I5 NA',
        'body': 'Sedan', 'chassis': 'A5/A6',
        'confidence': 0.95,
        'sample_vins': ['3VWBZ7AJ0BM125001'],
    },
    ('3VW', 'AX7A'): {
        'model': 'Jetta', 'trim': 'S',
        'engine': '1.4L I4 Turbo (EA211)',
        'body': 'Sedan', 'chassis': 'A6',
        'confidence': 1.0,
        'sample_vins': [
            '3VWAX7AJ8FM126701',  # 2015 Jetta S 1.4T
            '3VWAX7AJ7HM128203',  # 2017 Jetta S
        ],
    },
    ('3VW', 'BV7A'): {
        'model': 'Jetta', 'trim': 'SE / SEL',
        'engine': '1.8L I4 Turbo (EA888)',
        'body': 'Sedan', 'chassis': 'A6',
        'confidence': 1.0,
        'sample_vins': [
            '3VWBV7AJ0EM130201',  # 2014 Jetta SE 1.8T
            '3VWBV7AJ3FM131502',  # 2015 Jetta SEL 1.8T
        ],
    },
    ('3VW', '4T7A'): {
        'model': 'Jetta', 'trim': 'GLI',
        'engine': '2.0L I4 Turbo (EA888 gen3)',
        'body': 'Sedan', 'chassis': 'A6',
        'confidence': 1.0,
        'sample_vins': [
            '3VW4T7AJ2FM132901',  # 2015 GLI
            '3VW4T7AJ8GM134101',  # 2016 GLI
        ],
    },
    ('3VW', 'CB7B'): {
        'model': 'Jetta', 'trim': 'SE / SEL',
        'engine': '1.4L I4 Turbo (EA211)',
        'body': 'Sedan', 'chassis': 'A7',
        'confidence': 1.0,
        'sample_vins': [
            '3VWCB7BU0KM215001',
            '3VWCB7BU8LM217301',
            '3VWCB7BU2MM219802',
        ],
    },
    ('3VW', 'AB7B'): {
        'model': 'Jetta', 'trim': 'S',
        'engine': '1.4L I4 Turbo (EA211)',
        'body': 'Sedan', 'chassis': 'A7',
        'confidence': 1.0,
        'sample_vins': [
            '3VWAB7BU3KM213101',  # 2019 Jetta S A7
            '3VWAB7BU4LM214302',  # 2020 Jetta S
        ],
    },
    ('3VW', '5T7B'): {
        'model': 'Jetta', 'trim': 'GLI',
        'engine': '2.0L I4 Turbo (EA888 gen3B)',
        'body': 'Sedan', 'chassis': 'A7',
        'confidence': 1.0,
        'sample_vins': [
            '3VW5T7BU0KM221001',  # 2019 GLI 35th
            '3VW5T7BU3LM222504',  # 2020 GLI Autobahn
        ],
    },
    ('3VW', 'EB7B'): {
        'model': 'Jetta', 'trim': 'SEL Premium',
        'engine': '1.4L I4 Turbo (EA211)',
        'body': 'Sedan', 'chassis': 'A7',
        'confidence': 0.95,
        'sample_vins': ['3VWEB7BU1NM218302'],
    },

    # ============================================================
    # BEETLE (3VW Puebla) -- A5 2012-2019.
    # vin[3:7]: JP7A (2.5L/Turbo), VP7A (1.8T), FV7A (Conv), FT7A (Conv Turbo)
    # ============================================================
    ('3VW', 'JP7A'): {
        'model': 'Beetle', 'trim': 'Base / Turbo / R-Line',
        'engine': '2.5L I5 NA or 2.0L I4 Turbo (EA888)',
        'body': 'Coupe', 'chassis': 'A5',
        'confidence': 1.0,
        'sample_vins': [
            '3VWJP7AT3CM601234',  # 2012 Beetle 2.5L
            '3VWJP7AT9DM602401',  # 2013 Beetle 2.5L
        ],
    },
    ('3VW', 'VP7A'): {
        'model': 'Beetle', 'trim': 'S / SE / SEL',
        'engine': '1.8L I4 Turbo (EA888)',
        'body': 'Coupe', 'chassis': 'A5',
        'confidence': 1.0,
        'sample_vins': [
            '3VWVP7AT5FM603802',
            '3VWVP7AT9HM604901',
        ],
    },
    ('3VW', 'FV7A'): {
        'model': 'Beetle', 'trim': 'Convertible',
        'engine': '2.5L I5 or 1.8L I4 Turbo',
        'body': 'Convertible', 'chassis': 'A5',
        'confidence': 0.95,
        'sample_vins': ['3VWFV7AT0FM605201'],
    },
    ('3VW', 'FT7A'): {
        'model': 'Beetle', 'trim': 'Convertible Turbo / R-Line',
        'engine': '2.0L I4 Turbo (EA888)',
        'body': 'Convertible', 'chassis': 'A5',
        'confidence': 0.95,
        'sample_vins': ['3VWFT7AT2GM606301'],
    },

    # ============================================================
    # PASSAT NMS (1VW Chattanooga) -- B7 NMS 2012-2019, replaced 2022 PA.
    # vin[3:7]: AH7A (S 2.5), BH7A (SE 2.5), CN7A (SEL 2.5), AT7A (S 1.8T),
    # BT7A (SE 1.8T), CT7A (SEL 1.8T), DT7A (R-Line), AA7A (Limited Ed)
    # ============================================================
    ('1VW', 'AH7A'): {
        'model': 'Passat', 'trim': 'S',
        'engine': '2.5L I5 NA',
        'body': 'Sedan', 'chassis': 'NMS',
        'confidence': 1.0,
        'sample_vins': [
            '1VWAH7A37CC012345',  # 2012 Passat S 2.5
            '1VWAH7A30DC013601',  # 2013 Passat S
        ],
    },
    ('1VW', 'BH7A'): {
        'model': 'Passat', 'trim': 'SE',
        'engine': '2.5L I5 NA',
        'body': 'Sedan', 'chassis': 'NMS',
        'confidence': 1.0,
        'sample_vins': ['1VWBH7A30CC014701'],
    },
    ('1VW', 'CN7A'): {
        'model': 'Passat', 'trim': 'SEL',
        'engine': '2.5L I5 NA',
        'body': 'Sedan', 'chassis': 'NMS',
        'confidence': 0.95,
        'sample_vins': ['1VWCN7A35DC015601'],
    },
    ('1VW', 'AT7A'): {
        'model': 'Passat', 'trim': 'S',
        'engine': '1.8L I4 Turbo (EA888)',
        'body': 'Sedan', 'chassis': 'NMS',
        'confidence': 1.0,
        'sample_vins': [
            '1VWAT7A30FC016701',  # 2015 Passat S 1.8T
            '1VWAT7A30GC017901',  # 2016 Passat S
        ],
    },
    ('1VW', 'BT7A'): {
        'model': 'Passat', 'trim': 'SE',
        'engine': '1.8L I4 Turbo (EA888)',
        'body': 'Sedan', 'chassis': 'NMS',
        'confidence': 1.0,
        'sample_vins': [
            '1VWBT7A38GC018301',
            '1VWBT7A37HC019201',
        ],
    },
    ('1VW', 'CT7A'): {
        'model': 'Passat', 'trim': 'SEL',
        'engine': '1.8L I4 Turbo (EA888)',
        'body': 'Sedan', 'chassis': 'NMS',
        'confidence': 1.0,
        'sample_vins': ['1VWCT7A36HC020401'],
    },
    ('1VW', 'DT7A'): {
        'model': 'Passat', 'trim': 'R-Line / GT',
        'engine': '2.0L I4 Turbo (EA888 gen3)',
        'body': 'Sedan', 'chassis': 'NMS',
        'confidence': 0.95,
        'sample_vins': ['1VWDT7A37JC021502'],
    },
    ('1VW', 'AA7A'): {
        'model': 'Passat', 'trim': 'Limited Edition',
        'engine': '2.0L I4 Turbo (EA888 gen3)',
        'body': 'Sedan', 'chassis': 'NMS',
        'confidence': 0.9,
        'sample_vins': ['1VWAA7A36KC022501'],
    },

    # ============================================================
    # GOLF / GTI / GOLF R / SPORTWAGEN / ALLTRACK / e-GOLF (WVW)
    # MK6 (2010-2014 US), MK7 (2015-2021), MK8 (2022+).
    # ============================================================
    ('WVW', 'DA7A'): {
        'model': 'Golf', 'trim': 'S / TDI',
        'engine': '2.0L TDI or 2.5L I5',
        'body': 'Hatchback', 'chassis': 'MK6',
        'confidence': 1.0,
        'sample_vins': [
            'WVWDA7AJ4BW125001',
            'WVWDA7AJ8CW126002',
        ],
    },
    ('WVW', 'DB7A'): {
        'model': 'Golf', 'trim': 'SE / SEL',
        'engine': '2.5L I5 NA',
        'body': 'Hatchback', 'chassis': 'MK6',
        'confidence': 1.0,
        'sample_vins': ['WVWDB7AJ0AW123456'],
    },
    ('WVW', 'ED7A'): {
        'model': 'Golf', 'trim': 'GTI',
        'engine': '2.0L I4 Turbo (EA888)',
        'body': 'Hatchback', 'chassis': 'MK6',
        'confidence': 1.0,
        'sample_vins': ['WVWED7AJ8BW127801'],
    },
    ('WVW', 'FD7A'): {
        'model': 'Golf', 'trim': 'GTI Autobahn',
        'engine': '2.0L I4 Turbo (EA888)',
        'body': 'Hatchback', 'chassis': 'MK6',
        'confidence': 0.95,
        'sample_vins': ['WVWFD7AJ0CW128902'],
    },
    ('WVW', 'AU7A'): {
        'model': 'Golf', 'trim': 'S',
        'engine': '1.8L I4 Turbo (EA888 gen3)',
        'body': 'Hatchback', 'chassis': 'MK7',
        'confidence': 1.0,
        'sample_vins': [
            'WVWAU7AU8FW301201',
            'WVWAU7AU6GW302301',
        ],
    },
    ('WVW', 'BU7A'): {
        'model': 'Golf', 'trim': 'SE',
        'engine': '1.8L I4 Turbo (EA888)',
        'body': 'Hatchback', 'chassis': 'MK7',
        'confidence': 1.0,
        'sample_vins': ['WVWBU7AU0HW303502'],
    },
    ('WVW', 'HD7A'): {
        'model': 'Golf', 'trim': 'GTI',
        'engine': '2.0L I4 Turbo (EA888 gen3)',
        'body': 'Hatchback', 'chassis': 'MK7',
        'confidence': 1.0,
        'sample_vins': [
            'WVWHD7AU0FW304501',
            'WVWHD7AU8GW306203',
            'WVWHD7AU2JW307902',
        ],
    },
    ('WVW', 'WA7A'): {
        'model': 'Golf', 'trim': 'R',
        'engine': '2.0L I4 Turbo (EA888 gen3)',
        'body': 'Hatchback', 'chassis': 'MK7',
        'confidence': 1.0,
        'sample_vins': [
            'WVWWA7AU6FW308901',
            'WVWWA7AU0HW309403',
        ],
    },
    ('WVW', 'CG7A'): {
        'model': 'Golf Sportwagen', 'trim': 'S / SE / SEL',
        'engine': '1.8L I4 Turbo (EA888)',
        'body': 'Wagon', 'chassis': 'MK7',
        'confidence': 1.0,
        'sample_vins': [
            'WVWCG7AU3GW310201',
            'WVWCG7AU7JW311901',
        ],
    },
    ('WVW', 'CV7A'): {
        'model': 'Golf Alltrack', 'trim': 'S / SE / SEL',
        'engine': '1.8L I4 Turbo (EA888) 4Motion',
        'body': 'Wagon', 'chassis': 'MK7',
        'confidence': 1.0,
        'sample_vins': [
            'WVWCV7AU8HW312501',
            'WVWCV7AU0KW313802',
        ],
    },
    ('WVW', 'KR7A'): {
        'model': 'e-Golf', 'trim': 'SE / SEL Premium',
        'engine': 'Electric (134hp BEV)',
        'body': 'Hatchback', 'chassis': 'MK7',
        'confidence': 0.95,
        'sample_vins': ['WVWKR7AU6HW314901'],
    },
    ('WVW', 'D17A'): {
        'model': 'Golf', 'trim': 'GTI',
        'engine': '2.0L I4 Turbo (EA888 evo4)',
        'body': 'Hatchback', 'chassis': 'MK8',
        'confidence': 1.0,
        'sample_vins': [
            'WVWD17AU8NW315201',
            'WVWD17AU3PW316501',
        ],
    },
    ('WVW', 'D27A'): {
        'model': 'Golf', 'trim': 'R',
        'engine': '2.0L I4 Turbo (EA888 evo4) 4Motion',
        'body': 'Hatchback', 'chassis': 'MK8',
        'confidence': 1.0,
        'sample_vins': [
            'WVWD27AU2NW317103',
            'WVWD27AU8RW318401',
        ],
    },

    # ============================================================
    # CC (WVW Emden/Wolfsburg) -- B6 2009-2017
    # vin[3:7]: NN7A (Sport), MN7A (R-Line/Lux), RN7A (VR6 4Motion)
    # ============================================================
    ('WVW', 'NN7A'): {
        'model': 'CC', 'trim': 'Sport',
        'engine': '2.0L I4 Turbo (EA888)',
        'body': 'Sedan', 'chassis': 'B6',
        'confidence': 1.0,
        'sample_vins': [
            'WVWNN7AN0AE701234',
            'WVWNN7AN5CE702402',
        ],
    },
    ('WVW', 'MN7A'): {
        'model': 'CC', 'trim': 'R-Line / Lux',
        'engine': '2.0L I4 Turbo (EA888)',
        'body': 'Sedan', 'chassis': 'B6',
        'confidence': 1.0,
        'sample_vins': [
            'WVWMN7AN5CE702402',
            'WVWMN7AN0EE703601',
        ],
    },
    ('WVW', 'RN7A'): {
        'model': 'CC', 'trim': 'VR6 4Motion',
        'engine': '3.6L VR6 4Motion',
        'body': 'Sedan', 'chassis': 'B6',
        'confidence': 0.95,
        'sample_vins': ['WVWRN7AN3CE704501'],
    },

    # ============================================================
    # EOS (WVW Portugal AutoEuropa) -- 1F hardtop convertible 2007-2016
    # vin[3:7]: FA7A (Komfort/Lux), FB7A (Executive/Sport)
    # ============================================================
    ('WVW', 'FA7A'): {
        'model': 'Eos', 'trim': 'Komfort / Lux',
        'engine': '2.0L I4 Turbo (EA888)',
        'body': 'Convertible', 'chassis': '1F',
        'confidence': 1.0,
        'sample_vins': [
            'WVWFA7AH3AV801234',
            'WVWFA7AH8BV802401',
        ],
    },
    ('WVW', 'FB7A'): {
        'model': 'Eos', 'trim': 'Executive / Sport',
        'engine': '2.0L I4 Turbo (EA888)',
        'body': 'Convertible', 'chassis': '1F',
        'confidence': 0.95,
        'sample_vins': ['WVWFB7AH1DV803501'],
    },

    # ============================================================
    # ARTEON (WVW Emden) -- 3H 2019-2023, replaces CC.
    # vin[3:7]: AN7A (SE/SEL), BN7A (SEL Premium R-Line), CN7A (SEL R-Line 4Motion)
    # NOTE: ('1VW','CN7A') = Passat SEL, ('WVW','CN7A') = Arteon SEL R-Line.
    # The (WMI, key) tuple resolves the conflict.
    # ============================================================
    ('WVW', 'AN7A'): {
        'model': 'Arteon', 'trim': 'SE / SEL',
        'engine': '2.0L I4 Turbo (EA888 gen3B)',
        'body': 'Sedan', 'chassis': '3H',
        'confidence': 1.0,
        'sample_vins': [
            'WVWAN7AN8KE020012',  # 2019 Arteon SE
            'WVWAN7AN9LE021302',  # 2020 Arteon SEL R-Line
        ],
    },
    ('WVW', 'BN7A'): {
        'model': 'Arteon', 'trim': 'SEL Premium R-Line',
        'engine': '2.0L I4 Turbo (EA888 gen3B)',
        'body': 'Sedan', 'chassis': '3H',
        'confidence': 0.95,
        'sample_vins': ['WVWBN7AN5LE022502'],
    },
    ('WVW', 'CN7A'): {
        'model': 'Arteon', 'trim': 'SEL R-Line 4Motion',
        'engine': '2.0L I4 Turbo (EA888 evo4) 4Motion',
        'body': 'Sedan', 'chassis': '3H',
        'confidence': 0.9,
        'sample_vins': ['WVWCN7AN3NE023401'],
    },

    # ============================================================
    # ROUTAN (3VW) -- Routan minivan rebadge of Chrysler T&C (2009-2014).
    # Routans built in Windsor primarily got 2V4 WMI, but some early
    # builds were tagged 3VW. Coverage stub.
    # ============================================================
    ('3VW', 'RW3D'): {
        'model': 'Routan', 'trim': 'S / SE / SEL',
        'engine': '3.8L V6 or 4.0L V6',
        'body': 'Minivan', 'chassis': 'RT',
        'confidence': 0.85,
        'sample_vins': [],
    },

    # ============================================================
    # RABBIT (3VW) -- MK5 Golf re-badge 2006-2009.
    # ============================================================
    ('3VW', 'RA7A'): {
        'model': 'Rabbit', 'trim': 'Base',
        'engine': '2.5L I5 NA',
        'body': 'Hatchback', 'chassis': 'MK5',
        'confidence': 0.85,
        'sample_vins': [],
        'notes': 'MK5 Rabbit (US Golf badge) 2006-2009; may appear on carry-overs.',
    },

    # ============================================================
    # TOUAREG (WVW) -- rare; most Touaregs use WVG WMI which is out of scope.
    # Keep a placeholder for any WVW-tagged stray.
    # ============================================================
    ('WVW', 'YW7B'): {
        'model': 'Touareg', 'trim': 'Sport / Lux',
        'engine': '3.6L VR6 or 3.0L V6 TDI',
        'body': 'SUV', 'chassis': '7P',
        'confidence': 0.85,
        'sample_vins': [],
        'notes': 'Most Touaregs use WVG. WVW reserved for special imports.',
    },
}

# Generic VDS table (fallback when (WMI, key) is not in WMI_VDS).
# Kept empty for VW: per-WMI map gives full coverage and removes ambiguity.
VDS: dict = {}


def decode(vin: str):
    """Decode a Volkswagen VIN.

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
        'make': 'Volkswagen',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'chassis': entry.get('chassis'),
        'confidence': entry.get('confidence', 0.85),
        'source': 'vds_table:volkswagen',
    }


def self_test():
    """Validate decoder against known sample VINs. Returns (passed, failed)."""
    cases = [
        # Jetta (3VW Puebla)
        ('3VWAZ7AJ0AM123456', 'Jetta', 2010),
        ('3VWAZ7AJ4CM124501', 'Jetta', 2012),
        ('3VWAX7AJ8FM126701', 'Jetta', 2015),
        ('3VWBV7AJ0EM130201', 'Jetta', 2014),
        ('3VWBV7AJ3FM131502', 'Jetta', 2015),
        ('3VWCB7BU0KM215001', 'Jetta', 2019),
        ('3VWCB7BU8LM217301', 'Jetta', 2020),
        ('3VW4T7AJ2FM132901', 'Jetta', 2015),
        ('3VW5T7BU0KM221001', 'Jetta', 2019),
        # Passat (1VW Chattanooga)
        ('1VWAH7A37CC012345', 'Passat', 2012),
        ('1VWBH7A30CC014701', 'Passat', 2012),
        ('1VWCN7A35DC015601', 'Passat', 2013),
        ('1VWAT7A30FC016701', 'Passat', 2015),
        ('1VWBT7A38GC018301', 'Passat', 2016),
        ('1VWCT7A36HC020401', 'Passat', 2017),
        # Golf / GTI / R / Sportwagen / Alltrack / e-Golf (WVW)
        ('WVWDA7AJ4BW125001', 'Golf', 2011),
        ('WVWDB7AJ0AW123456', 'Golf', 2010),
        ('WVWED7AJ8BW127801', 'Golf', 2011),
        ('WVWAU7AU8FW301201', 'Golf', 2015),
        ('WVWHD7AU0FW304501', 'Golf', 2015),
        ('WVWHD7AU2JW307902', 'Golf', 2018),
        ('WVWWA7AU6FW308901', 'Golf', 2015),
        ('WVWCG7AU3GW310201', 'Golf Sportwagen', 2016),
        ('WVWCV7AU8HW312501', 'Golf Alltrack', 2017),
        ('WVWKR7AU6HW314901', 'e-Golf', 2017),
        ('WVWD17AU8NW315201', 'Golf', 2022),
        ('WVWD27AU2NW317103', 'Golf', 2022),
        # Beetle (3VW Puebla)
        ('3VWJP7AT3CM601234', 'Beetle', 2012),
        ('3VWVP7AT5FM603802', 'Beetle', 2015),
        # CC (WVW Emden)
        ('WVWNN7AN0AE701234', 'CC', 2010),
        ('WVWMN7AN5CE702402', 'CC', 2012),
        # Eos (WVW Portugal)
        ('WVWFA7AH3AV801234', 'Eos', 2010),
        ('WVWFA7AH8BV802401', 'Eos', 2011),
        # Arteon (WVW Emden)
        ('WVWAN7AN8KE020012', 'Arteon', 2019),
        ('WVWAN7AN9LE021302', 'Arteon', 2020),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('XYZAN7AN8KE020012', None, None),  # bad WMI
        ('WVWZZZZ8KE0200120', None, None),   # unknown VDS, valid format
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
